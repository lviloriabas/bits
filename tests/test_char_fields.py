"""Celdas de carácter de la banda de fecha (day_1..year_2).

Cubre: regla de postprocesado ``char``, unión de celdas en el pipeline,
no degradación del estado de página, plantilla con celdas y puertas CSV.
"""

from __future__ import annotations

import csv
from datetime import date
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.core import pipeline as pipeline_module
from app.core.config import AppConfig
from app.core.pipeline import (
    _combine_date_parts,
    _date_cell_overrides,
    _join_char_fields,
    process_page_image,
)
from app.models.schemas import FieldResult, PageResult, Status, ValidationReport
from app.reports.csv_reporter import CsvReporter
from app.templates.manager import TemplateManager
from app.templates.schema import FieldTemplate, Template
from app.utils.postprocess import apply_postprocess
from app.validation.validator import validate_page
from app.validation.book_corrector import _recompute_page_status
from app.vision.date_geometry import DateFieldGeometry


def _char_template() -> Template:
    return Template(
        name="fixture",
        fields=[
            FieldTemplate(id="day", required=True, x=0.2, y=0.1,
                          w=0.3, h=0.05, postprocess="day"),
            FieldTemplate(id="day_1", x=0.2, y=0.05, w=0.12, h=0.1,
                          postprocess="char", regex=r"^\d$"),
            FieldTemplate(id="day_2", x=0.34, y=0.02, w=0.12, h=0.16,
                          postprocess="char", regex=r"^\d$"),
            FieldTemplate(id="month", required=True, x=0.5, y=0.1,
                          w=0.2, h=0.05, postprocess="month"),
            FieldTemplate(id="month_1", x=0.5, y=0.05, w=0.1, h=0.1,
                          postprocess="char", regex=r"^[A-Z]$"),
            FieldTemplate(id="month_2", x=0.61, y=0.02, w=0.1, h=0.16,
                          postprocess="char", regex=r"^[A-Z]$"),
            FieldTemplate(id="month_3", x=0.72, y=0.02, w=0.1, h=0.16,
                          postprocess="char", regex=r"^[A-Z]$"),
            FieldTemplate(id="year", required=True, x=0.85, y=0.1,
                          w=0.12, h=0.05, postprocess="year"),
            FieldTemplate(id="year_1", x=0.85, y=0.05, w=0.1, h=0.1,
                          postprocess="char", regex=r"^\d$"),
            FieldTemplate(id="year_2", x=0.88, y=0.02, w=0.11, h=0.16,
                          postprocess="char", regex=r"^\d$"),
        ],
    )


def _char_field(fid: str, value, conf=0.7) -> FieldResult:
    return FieldResult(page_number=1, field_id=fid, field_type="ocr",
                       value=value, confidence=conf)


def _page_result(*fields: FieldResult) -> PageResult:
    page = PageResult(page_number=1)
    for field in fields:
        page.add_field(field)
    return page


def _config() -> AppConfig:
    return AppConfig(
        dpi=200,
        date_dpi=200,
        deskew=False,
        align=False,
        date_slot_ocr=False,
    )


def _page() -> np.ndarray:
    image = np.full((100, 120, 3), 255, dtype=np.uint8)
    image[0:20, 0:20] = 0
    return image


def test_char_postprocess_rules():
    value, note = apply_postprocess("day_1", "char", "5X")
    assert (value, note) == ("5", "")
    value, note = apply_postprocess("month_1", "char", "j")
    assert (value, note) == ("J", "")
    value, note = apply_postprocess("month_1", "char", "  ")
    assert (value, note) == ("", "empty char cell")
    value, note = apply_postprocess("day_1", "char", "||_")
    assert (value, note) == ("", "invalid char: ||_")
    value, note = apply_postprocess("day_1", "char", "二")
    assert (value, note) == ("", "invalid char: 二")


def test_join_rebuilds_day_from_cells():
    page = _page_result(
        _char_field("day", ""),
        _char_field("day_1", "2", 0.8),
        _char_field("day_2", "5", 0.9),
    )
    _join_char_fields(page)
    day = next(f for f in page.fields if f.field_id == "day")
    assert day.value == "25"
    assert day.raw_value == "25"
    assert day.source == "date_cells"
    assert day.inference_method == "date_cells"
    assert day.confidence == 0.85
    assert day.status is Status.OK


def test_complete_structured_cells_override_conflicting_direct_reading():
    page = _page_result(
        _char_field("day", "20", 0.9),
        _char_field("day_1", "3", 0.9),
        _char_field("day_2", "1", 0.9),
    )
    _join_char_fields(page)
    day = next(f for f in page.fields if f.field_id == "day")
    assert day.value == "31"
    assert day.source == "date_cells"
    assert day.alternatives == ["20"]


def test_weak_conflict_is_left_unresolved_instead_of_guessing():
    page = _page_result(
        _char_field("year", "21", 0.44),
        _char_field("year_1", "2", 0.56),
        _char_field("year_2", "2", 0.56),
    )

    _join_char_fields(page)

    year = next(f for f in page.fields if f.field_id == "year")
    assert year.value is None
    assert set(year.alternatives) == {"21", "22"}
    assert year.status is Status.ERROR


def test_join_replaces_weak_direct_reading():
    page = _page_result(
        _char_field("day", "5", 0.3),
        _char_field("day_1", "2", 0.8),
        _char_field("day_2", "5", 0.9),
    )
    _join_char_fields(page)
    day = next(f for f in page.fields if f.field_id == "day")
    assert day.value == "25"


def test_join_rejects_incomplete_day_when_first_slot_is_not_digit():
    page = _page_result(
        _char_field("day", ""),
        _char_field("day_1", "X", 0.8),
        _char_field("day_2", "5", 0.9),
    )
    _join_char_fields(page)
    day = next(f for f in page.fields if f.field_id == "day")
    assert day.value is None


def test_join_fingerprints_month_and_year():
    page = _page_result(
        _char_field("month", ""),
        _char_field("month_1", "J", 0.8),
        _char_field("month_2", "U", 0.7),
        _char_field("month_3", "L", 0.9),
        _char_field("year", ""),
        _char_field("year_1", "2", 0.8),
        _char_field("year_2", "6", 0.9),
    )
    _join_char_fields(page)
    by_id = {f.field_id: f for f in page.fields}
    assert by_id["month"].value == "JUL"
    assert by_id["year"].value == "26"


def test_join_normalizes_handwritten_cell_confusions():
    page = _page_result(
        _char_field("day", ""),
        _char_field("day_1", "Z", 0.8),
        _char_field("day_2", "7", 0.8),
        _char_field("month", ""),
        _char_field("month_1", "J", 0.8),
        _char_field("month_2", "0", 0.8),
        _char_field("month_3", "L", 0.8),
        _char_field("year", ""),
        _char_field("year_1", "z", 0.8),
        _char_field("year_2", "6", 0.8),
    )

    _join_char_fields(page)

    by_id = {f.field_id: f for f in page.fields}
    assert by_id["day"].value == "27"
    assert by_id["month"].value == "JUL"
    assert by_id["year"].value == "26"


def test_join_recovers_multilingual_shapes_as_numeric_candidates():
    page = _page_result(
        _char_field("day", ""),
        _char_field("day_1", "乙", 0.7),
        _char_field("day_2", "5", 0.9),
        _char_field("year", ""),
        _char_field("year_1", "2", 0.9),
        _char_field("year_2", "C", 0.7),
    )

    _join_char_fields(page)

    by_id = {f.field_id: f for f in page.fields}
    assert by_id["day"].value == "25"
    assert by_id["year"].value == "26"
    assert "20" in by_id["year"].alternatives


@patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
def test_join_chooses_possible_year_over_stronger_future_reading(_clock):
    page = _page_result(
        _char_field("year", ""),
        _char_field("year_1", "2", 0.9),
        _char_field("year_2", "B", 0.9),
    )

    _join_char_fields(page)

    year = next(field for field in page.fields if field.field_id == "year")
    assert year.value == "26"
    assert year.source == "date_cells"
    assert year.status is Status.OK


@patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
def test_join_does_not_promote_unambiguous_future_year(_clock):
    page = _page_result(
        _char_field("year", ""),
        _char_field("year_1", "2", 0.9),
        _char_field("year_2", "8", 0.9),
    )

    _join_char_fields(page)

    year = next(field for field in page.fields if field.field_id == "year")
    assert not year.value
    assert year.source != "date_cells"


def test_join_month_from_two_unambiguous_slots():
    page = _page_result(
        _char_field("month", ""),
        _char_field("month_1", "J", 0.6),
        _char_field("month_2", "", 0.0),
        _char_field("month_3", "L", 0.8),
    )

    _join_char_fields(page)

    month = next(f for f in page.fields if f.field_id == "month")
    assert month.value == "JUL"


def test_join_month_fuses_global_and_positional_evidence():
    month = _char_field("month", "", 0.62)
    month.raw_value = "I0L"
    page = _page_result(
        month,
        _char_field("month_1", "", 0.0),
        _char_field("month_2", "0", 0.64),
        _char_field("month_3", "2", 0.97),
    )

    _join_char_fields(page)

    assert month.value == "JUL"


def test_join_accepts_rare_numeric_month_only_from_cells():
    page = _page_result(
        _char_field("month", "7", 0.9),
        _char_field("month_1", "", 0.0),
        _char_field("month_2", "0", 0.8),
        _char_field("month_3", "7", 0.8),
    )

    _join_char_fields(page)

    month = next(f for f in page.fields if f.field_id == "month")
    assert month.value == "7"
    assert month.source == "date_cells"
    assert month.status is Status.WARNING
    assert "numeric handwritten month" in month.comment


def test_numeric_month_is_not_confirmed_when_other_slots_contain_noise():
    page = _page_result(
        _char_field("month", "2", 0.8),
        _char_field("month_1", "X", 0.8),
        _char_field("month_2", "0", 0.8),
        _char_field("month_3", "", 0.0),
    )

    _join_char_fields(page)

    month = next(f for f in page.fields if f.field_id == "month")
    assert month.value == "2"
    assert month.source != "date_cells"
    assert month.status is Status.OK


def test_pipeline_does_not_combine_unconfirmed_numeric_month():
    template = _char_template()
    config = _config()
    with patch.object(
        pipeline_module, "ocr_regions",
        return_value=[
            ("24", 0.9),
            ("2", 0.8), ("4", 0.8),
            ("02", 0.8),
            ("X", 0.8), ("0", 0.8), ("", 0.0),
            ("26", 0.9),
            ("2", 0.8), ("6", 0.8),
        ],
    ):
        page = process_page_image(
            _page(), 1, config, object(), template, None,
        )

    month = next(f for f in page.fields if f.field_id == "month")
    assert month.status is Status.ERROR
    assert page.date is None


def test_invalid_component_never_forms_a_page_date():
    page = _page_result(
        _char_field("day", "20"),
        _char_field("month", "JUL"),
        _char_field("year", "26"),
    )
    next(f for f in page.fields if f.field_id == "month").status = Status.ERROR

    _combine_date_parts(page)

    assert page.date is None


def test_char_cells_follow_detected_vertical_separators():
    template = _char_template()
    geometry = {
        "day": DateFieldGeometry(
            "day", (24, 10, 72, 30), separators=[49], score=1.0
        )
    }

    overrides = _date_cell_overrides(template, geometry, (100, 120, 3))

    assert overrides["day_1"].rect_pixels(120, 100) == (24, 10, 49, 30)
    assert overrides["day_2"].rect_pixels(120, 100) == (49, 10, 72, 30)


def test_join_ignores_missing_cells():
    page = _page_result(_char_field("day", "20", 0.9))
    _join_char_fields(page)
    day = next(f for f in page.fields if f.field_id == "day")
    assert day.value == "20"


def test_char_cells_do_not_degrade_page_status():
    template = _char_template()
    page = _page_result(
        _char_field("day", "25", 0.9),
        _char_field("day_1", "", 0.0),
        _char_field("day_2", "5", 0.9),
    )
    validate_page(page, template, _config())
    assert page.status is Status.OK
    empty = next(f for f in page.fields if f.field_id == "day_1")
    assert empty.status is Status.WARNING

    _recompute_page_status(page)
    assert page.status is Status.OK


def test_process_page_image_joins_date_cells(monkeypatch):
    monkeypatch.setattr("app.validation.date_review.reference_date",
                        lambda today=None: today or date(2026, 7, 31))
    template = _char_template()
    config = _config()
    with patch.object(
        pipeline_module, "ocr_regions",
        return_value=[
            ("", 0.0),
            ("2", 0.8), ("5", 0.9),
            ("", 0.0),
            ("J", 0.8), ("U", 0.7), ("L", 0.9),
            ("", 0.0),
            ("2", 0.8), ("6", 0.9),
        ],
    ):
        page = process_page_image(
            _page(), 1, config, object(), template, None,
        )

    by_id = {f.field_id: f for f in page.fields}
    assert by_id["day"].value == "25"
    assert by_id["month"].value == "JUL"
    assert by_id["year"].value == "26"
    assert by_id["day"].source == "date_cells"
    assert page.date == "2026/07/25"
    assert page.status is Status.OK


def test_template_defaults_include_char_cells():
    manager = TemplateManager()
    template = manager.load(
        Path(__file__).resolve().parents[1]
        / "template" / "aircraft_log.json"
    )
    expected = {f"day_{i}" for i in (1, 2)} | {
        f"month_{i}" for i in (1, 2, 3)
    } | {f"year_{i}" for i in (1, 2)}
    cells = {f.id: f for f in template.fields if f.id in expected}
    assert len(cells) == 7
    order = [f.id for f in template.fields]
    assert order.index("day") < order.index("day_1") < order.index("day_2")
    assert order.index("month") < order.index("month_1") < order.index("month_3")
    assert order.index("year") < order.index("year_1") < order.index("year_2")
    for cell in cells.values():
        assert cell.postprocess == "char"
        assert cell.localize == "ink"
        assert cell.required is False
        assert cell.min_length == 1
        assert cell.max_length == 1
        if cell.id.startswith("month"):
            assert cell.regex == r"^[A-Z]$"
        else:
            assert cell.regex == r"^\d$"


def test_csv_char_cell_gating():
    page = _page_result(
        _char_field("day_1", "2", 0.8),
        _char_field("day_2", "24", 0.8),
        _char_field("month_1", "J", 0.8),
        _char_field("month_2", "X1", 0.8),
        _char_field("year_1", "1", 0.8),
        _char_field("year_2", "O", 0.8),
    )
    report = ValidationReport(pdf_path="fixture.pdf", template_name="fixture",
                              pages=[page])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.csv"
        CsvReporter().write(report, path)
        with open(path, encoding="utf-8-sig", newline="") as fh:
            row = list(csv.DictReader(fh))[0]
    assert row["day_1"] == "2"
    assert row["day_2"] == ""
    assert row["month_1"] == "J"
    assert row["month_2"] == ""
    assert row["year_1"] == "1"
    assert row["year_2"] == ""
