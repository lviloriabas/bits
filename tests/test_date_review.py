"""Qué fechas pasan a revisión y cuáles se indexan tal como se leyeron.

Una lectura posterior a hoy se aparta y el libro intenta sustituirla. Una
fecha futura que sigue guardada requiere revisión. La antigüedad aparta una
página cuando su libro no respalda ese año con otra bitácora.
"""

from datetime import date

import pytest

from app.models.schemas import FieldResult, PageResult, Status
from app.utils.date_window import (
    is_usual,
    month_is_possible,
    review_start,
    usual_start,
)
from app.validation.date_review import review_date_window
from app.validation.date_corrector import correct_dates_by_book
from app.validation.page_status import ready_for_auto_index
from app.reports.organize import por_revisar
from test_date_corrector import _page, _report, _field_of

TODAY = date(2026, 9, 6)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY
    monkeypatch.setattr("app.utils.date_window.date", FixedDate)
    monkeypatch.setattr("app.validation.date_corrector.date", FixedDate)


@pytest.mark.parametrize("value,review", [
    ("2026/09/06", False), ("2026/09/01", False), ("2026/08/01", False),
    ("2026/08/31", False), ("2026/07/31", False), ("2026/05/11", False),
    ("2025/10/01", True), ("2025/09/30", True), ("2024/01/01", True),
    ("2023/12/31", True), ("2020/07/25", True), ("2001/05/04", True),
])
def test_temporal_review_keeps_the_read_date(value, review):
    page = PageResult(page_number=1, date=value)
    assert review_date_window(page) is review
    assert page.date_review is review
    assert page.date == value
    if review:
        assert por_revisar(page)


def test_only_the_current_year_is_normal_outside_january():
    # El periodo habitual sigue siendo estrecho porque ordena candidatos; el
    # de revisión admite solo el año de la ejecución fuera de enero.
    assert review_start(TODAY) == date(2026, 1, 1)
    assert not is_usual(date(2026, 5, 11), TODAY)
    page = PageResult(page_number=1, date="2026/05/11")
    assert not review_date_window(page)
    assert not page.date_review
    for value in ("2025/09/06", "2024/08/07"):
        page = PageResult(page_number=1, date=value)
        assert review_date_window(page)
        assert page.date_review


def test_january_accepts_the_previous_year_backlog():
    reference = date(2026, 1, 15)
    assert review_start(reference) == date(2025, 1, 1)
    page = PageResult(page_number=1, date="2025/12/31")
    assert not review_date_window(page, reference)
    assert not page.date_review


@pytest.mark.parametrize("year", ["28", "2028"])
@pytest.mark.parametrize("month_end", [False, True])
def test_saved_future_year_requires_review_even_if_book_supports_it(year, month_end):
    page = _page(1, "2147301", "31", "JUL", year)
    page.date = "2028/07/31"
    if month_end:
        _field_of(page, "day").inference_method = "month_end_policy"

    assert review_date_window(page, supported_years={2028})
    assert page.date_review
    assert not ready_for_auto_index(page)
    assert "Año futuro: 2028" in page.comment

    _field_of(page, "year").value = "26"
    page.date = "2026/07/31"
    assert not review_date_window(page)
    assert not page.date_review
    assert page.comment == ""


def test_saved_future_year_requires_review_without_month_or_day():
    page = _page(1, "2147301", None, None, "28")
    assert review_date_window(page)
    assert page.date_review
    assert "Año futuro: 2028" in page.comment


def test_a_year_that_cannot_be_the_delivery_is_reviewed():
    # Seis, diez o veinte años atrás no es una bitácora vieja en una entrega
    # del año en curso: es un año que nadie puede dar por bueno sin verlo.
    page = PageResult(page_number=1, date="2020/08/14")
    assert review_date_window(page)
    assert "Fecha muy antigua" in page.comment


def test_the_old_wording_is_cleaned_when_re_exporting_a_saved_run():
    # Una ejecución guardada con la regla anterior trae su aviso escrito. Al
    # volver a exportarla, la página no puede quedarse con el motivo de una
    # regla que ya no la aparta.
    page = PageResult(
        page_number=1, date="2026/07/31", date_review=True,
        comment="Fecha fuera del periodo habitual: 2026/07/31, anterior a 2026/08/01",
    )
    assert not review_date_window(page)
    assert not page.date_review
    assert page.comment == ""


def test_previous_december_is_normal_when_processing_in_january():
    reference = date(2026, 1, 2)
    assert usual_start(reference) == date(2025, 12, 1)
    assert is_usual(date(2025, 12, 1), reference)
    assert not is_usual(date(2025, 11, 30), reference)


def test_previous_month_is_complete_even_at_the_end_of_a_long_month():
    assert is_usual(date(2026, 2, 1), date(2026, 3, 31))
    assert is_usual(date(2024, 2, 29), date(2024, 3, 31))
    assert not month_is_possible(2026, 0, TODAY)
    assert not month_is_possible(2026, 13, TODAY)


def test_an_old_book_read_by_two_log_pages_indexes_alone():
    # Dos bitácoras del libro leyeron el mismo año: es un libro atrasado, no
    # un año mal leído, y sus datos no necesitan que nadie los teclee.
    pages = [_page(1, "2147301", "20", "AGO", "24"),
             _page(2, "2147302", "21", "AGO", "24")]
    correct_dates_by_book([_report(*pages)])
    assert not any(page.date_review for page in pages)
    assert [page.date for page in pages] == ["2024/08/20", "2024/08/21"]


def test_an_old_year_with_a_single_reading_is_still_reviewed():
    page = _page(1, "2147301", "20", "AGO", "24")
    correct_dates_by_book([_report(page)])
    assert page.date_review
    assert "Fecha muy antigua" in page.comment


def test_the_same_log_page_scanned_twice_is_one_reading():
    first = _page(1, "2147301", "20", "AGO", "24")
    copy = _page(1, "2147301", "20", "AGO", "24")
    correct_dates_by_book([_report(first), _report(copy, name="copia.pdf")])
    assert first.date_review and copy.date_review


def test_tomorrow_is_replaced_by_the_book_instead_of_going_to_review():
    # El día de mañana está mal leído, pero el mes y el año no: la página se
    # indexa con el último día que cabe y la lectura apartada queda a la
    # vista en las alternativas y en el comentario.
    page = _page(1, "2147301", "07", "SEP", "26")
    stats = correct_dates_by_book([_report(page)])
    day = _field_of(page, "day")
    assert stats["after_the_run"] == 1
    assert "07" in day.alternatives
    assert day.value == "30"
    assert day.inference_method == "month_end_fallback"
    assert "Fecha futura: 2026/09/07" in day.comment
    assert page.date == "2026/09/30"
    assert not page.date_review


def test_future_month_stays_pending_but_is_not_a_reason_to_review():
    # Sin mes no hay fecha, y el CSV la deduce de sus vecinas. La página solo
    # va a REVISAR si el índice se queda de verdad sin End Date, no por
    # haber leído un mes imposible.
    page = _page(1, "2147301", "01", "OCT", "26")
    correct_dates_by_book([_report(page)])
    assert _field_of(page, "month").value is None
    assert page.date is None
    assert not page.date_review


def test_unread_day_uses_month_end_as_fallback():
    page = _page(1, "2147301", None, "SEP", "26")
    correct_dates_by_book([_report(page)])
    assert page.date == "2026/09/30"
    assert not page.date_review


def test_saved_month_end_fallback_does_not_require_review():
    # El fin del mes actual es un respaldo admitido, no una lectura futura.
    page = PageResult(page_number=1, date="2026/09/30")
    page.add_field(FieldResult(
        page_number=1, field_id="day", field_type="ocr", value="30",
        confidence=.5, status=Status.WARNING, source="inferred",
        inference_method="month_end_fallback",
    ))
    assert not review_date_window(page)
    assert not page.date_review
    # Fuera de enero, el año anterior ya necesita revisión.
    page.date = "2025/07/31"
    assert review_date_window(page)


def test_month_end_policy_keeps_current_month_end():
    page = _page(1, "2147301", None, "SEP", "26")
    day = _field_of(page, "day")
    day.inference_method = "month_end_policy"
    day.source = "csv_date_policy"
    correct_dates_by_book([_report(page)])
    assert page.date == "2026/09/30"
    assert not page.date_review


def test_saved_month_end_day_keeps_its_source():
    page = _page(1, "2147301", "30", "SEP", "26")
    day = _field_of(page, "day")
    day.inference_method = "month_end_policy"
    day.source = "csv_date_policy"

    correct_dates_by_book([_report(page)])

    assert page.date == "2026/09/30"
    assert day.value == "30"
    assert day.inference_method == "month_end_policy"
    assert day.source == "csv_date_policy"
    assert not page.date_review


@pytest.mark.parametrize("value", ["2026/09/07", "2026/09/30", "2026/10/01"])
def test_saved_future_day_or_month_requires_review(value):
    page = PageResult(page_number=1, date=value)
    assert review_date_window(page)
    assert page.date_review
    assert "Fecha futura" in page.comment


@pytest.mark.parametrize("value", ["2026/09/07", "2026/10/31"])
def test_month_end_fallback_does_not_allow_other_future_dates(value):
    page = PageResult(page_number=1, date=value)
    page.add_field(FieldResult(
        page_number=1, field_id="day", field_type="ocr",
        value=value[-2:], inference_method="month_end_fallback", source="inferred",
    ))
    assert review_date_window(page)
    assert page.date_review


def test_correcting_the_date_clears_only_the_temporal_warning():
    page = PageResult(page_number=1, date="2020/07/25", comment="Revisar matrícula")
    review_date_window(page)
    review_date_window(page)
    assert page.comment.count("Fecha muy antigua") == 1
    page.date = "2026/08/25"
    review_date_window(page)
    assert not page.date_review
    assert page.comment == "Revisar matrícula"


def test_other_review_reasons_survive_the_temporal_check():
    page = PageResult(page_number=1, date="2026/08/25", date_review=True)
    review_date_window(page)
    assert page.date_review


def test_automatic_indexing_is_blocked_for_old_dates():
    page = _page(1, "2147301", "25", "JUL", "20")
    page.add_field(FieldResult(page_number=1, field_id="matricula", field_type="ocr",
                              value="HP-1534CMP", confidence=.9, status=Status.OK))
    correct_dates_by_book([_report(page)])
    assert not ready_for_auto_index(page)
    assert por_revisar(page)


def test_a_pending_future_reading_is_not_a_reason_to_review():
    # El año leído '28' no existe y el campo quedó vacío. Eso deja la página
    # sin fecha, que el CSV deduce de sus vecinas: apartarla además en
    # REVISAR era cobrar dos veces la misma lectura.
    page = _page(1, "2147301", "01", "ENE", None)
    _field_of(page, "year").raw_value = "28"
    assert not review_date_window(page)
    assert not page.date_review


def test_no_page_keeps_a_future_reading_but_month_end_fallback_is_allowed():
    pages = [_page(1, "2147301", "05", "SEP", "26"),
             _page(2, "2147302", "09", "SEP", "26"),
             _page(3, "2147303", None, "SEP", "26")]
    correct_dates_by_book([_report(*pages)])
    assert [page.date for page in pages] == [
        "2026/09/05", "2026/09/30", "2026/09/30",
    ]
    assert _field_of(pages[1], "day").inference_method == "month_end_fallback"
    assert _field_of(pages[2], "day").inference_method == "month_end_fallback"
    assert not any(page.date_review for page in pages)


@pytest.mark.parametrize("day,month", [("07", "SEP"), ("01", "OCT")])
def test_future_dates_are_never_saved_as_book_anchors(day, month):
    from app.validation.date_corrector import _confirmed_date
    page = _page(1, "2147301", day, month, "26")
    assert _confirmed_date(page) is None
