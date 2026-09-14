"""Selección y filtrado de columnas del visor CSV."""

from __future__ import annotations

from pathlib import Path
import time

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtWidgets import QApplication

from app.gui.csv_viewer import (
    CsvColumnModeButton,
    CsvViewerWindow,
    EmbeddedPdfViewer,
    csv_path_for_view,
    reports_for_csv,
    resolve_source_documents,
    restore_run_columns,
    run_dir_for_csv,
    source_pdf_paths_for_rows,
)
from app.gui.tokens import paleta
from app.gui.widgets import pane_status_colors
from app.gui.csv_utils import (
    csv_field_id,
    find_csv_files,
    important_csv_columns,
    infer_important_field_ids,
    read_csv_file,
)
from app.templates.schema import FieldTemplate, Template


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _columns() -> list[str]:
    return [
        "file",
        "page",
        "log_number",
        "dup",
        "log_number_conf",
        "log_number_status",
        "matricula",
        "pilot_signature",
        "captain_license",
        "day",
        "month",
        "year",
        "day_1",
        "date",
        "time_ms",
    ]


def test_important_view_keeps_only_primary_values_and_run_columns():
    visible = important_csv_columns(
        _columns(),
        {"log_number", "matricula", "pilot_signature", "day", "month", "year"},
    )

    assert visible == [
        "file",
        "page",
        "log_number",
        "dup",
        "matricula",
        "pilot_signature",
        "date",
        "time_ms",
    ]


def test_complete_field_id_is_recovered_from_metadata_column():
    assert csv_field_id("log_number_status", _columns()) == "log_number"
    assert csv_field_id("pilot_signature", _columns()) == "pilot_signature"
    assert csv_field_id("dup", _columns()) is None
    assert csv_field_id("date", _columns()) is None


def test_fallback_importance_includes_signatures_but_not_cell_fields():
    assert infer_important_field_ids(_columns()) == {
        "log_number",
        "matricula",
        "captain_license",
        "pilot_signature",
    }


def test_find_and_read_csv_from_processed_data_folder(tmp_path: Path):
    run = tmp_path / "BITS TEST"
    data = run / "datos"
    data.mkdir(parents=True)
    csv_path = data / "BITS TEST.CSV"
    csv_path.write_text(
        "\ufefffile,page,matricula,date\nbitácora.pdf,1,HP-1234CMP,2026/08/13\n",
        encoding="utf-8",
    )

    assert find_csv_files(run) == [csv_path]
    columns, rows = read_csv_file(csv_path)
    assert columns == ["file", "page", "matricula", "date"]
    assert rows == [
        {
            "file": "bitácora.pdf",
            "page": "1",
            "matricula": "HP-1234CMP",
            "date": "2026/08/13",
        }
    ]


def test_legacy_discrepancy_header_uses_the_current_name(tmp_path: Path):
    csv_path = tmp_path / "run.csv"
    csv_path.write_text(
        "file,page,discrepancia\na.pdf,1,Falta firma de capitán\n",
        encoding="utf-8",
    )

    columns, rows = read_csv_file(csv_path)

    assert columns == ["file", "page", "disc_reason"]
    assert rows[0]["disc_reason"] == "Falta firma de capitán"


def test_viewer_restores_review_for_a_historical_run(tmp_path: Path):
    import json

    from app.models.schemas import FieldResult, PageResult, ValidationReport

    csv_path = tmp_path / "run.csv"
    csv_path.write_text(
        "file,page,log_number,dup,disc,discrepancia,matricula\n"
        "a.pdf,1,1234500,false,false,,HP-1848CMP\n"
        "a.pdf,2,1234501,false,true,Falta firma de capitán,HP-1848CMP\n",
        encoding="utf-8",
    )
    pages = []
    for number, log_number in ((1, "1234500"), (2, "1234501")):
        page = PageResult(
            page_number=number,
            discrepancy=number == 2,
            discrepancy_note=(
                "Falta firma de capitán" if number == 2 else ""
            ),
        )
        page.add_field(FieldResult(
            page_number=number, field_id="log_number", field_type="ocr",
            value=log_number, confidence=1.0,
        ))
        page.add_field(FieldResult(
            page_number=number, field_id="matricula", field_type="ocr",
            value="HP-1848CMP", confidence=1.0,
        ))
        pages.append(page)
    report = ValidationReport(
        pdf_path="a.pdf", template_name="fixture", pages=pages
    )
    csv_path.with_suffix(".json").write_text(
        json.dumps({"reportes": [report.model_dump(mode="json")]}),
        encoding="utf-8",
    )

    columns, rows = read_csv_file(csv_path)
    columns, rows = restore_run_columns(csv_path, columns, rows)

    assert columns[:7] == [
        "file", "page", "log_number", "review", "dup", "disc",
        "disc_reason",
    ]
    assert [row["review"] for row in rows] == ["false", "true"]


def test_column_mode_control_explains_what_the_click_will_change():
    app = QApplication.instance() or QApplication([])
    button = CsvColumnModeButton()

    assert button.text() == "Columnas importantes"
    assert not button.icon().isNull()
    assert "columnas importantes" in button.toolTip()

    button.setChecked(False)
    app.processEvents()
    assert button.text() == "Todas las columnas"
    assert "CSV completo" in button.toolTip()


def test_minimum_csv_uses_complete_companion_for_the_view(tmp_path: Path):
    minimum = tmp_path / "run.CSV"
    complete = tmp_path / "run_completo.CSV"
    minimum.write_text("file,page\na.pdf,1\n", encoding="utf-8")
    complete.write_text(
        "file,page,log_number,log_number_status\na.pdf,1,1234500,OK\n",
        encoding="utf-8",
    )

    assert csv_path_for_view(minimum) == complete
    assert csv_path_for_view(complete) == complete


def test_viewer_can_show_and_select_every_complete_column(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    run = tmp_path / "run"
    data = run / "datos"
    data.mkdir(parents=True)
    minimum = data / "run.CSV"
    complete = data / "run_completo.CSV"
    minimum.write_text("file,page\na.pdf,1\n", encoding="utf-8")
    complete.write_text(
        "file,page,log_number,log_number_status\na.pdf,1,1234500,OK\n",
        encoding="utf-8",
    )

    viewer = CsvViewerWindow(tmp_path)
    try:
        assert viewer.load_folder(run)
        assert viewer._columns == [
            "file", "page", "log_number", "log_number_status",
        ]
        assert viewer.table.isColumnHidden(
            viewer.table_model.column_of("log_number_status")
        )

        viewer.column_toggle.click()
        app.processEvents()

        assert not viewer.table.isColumnHidden(
            viewer.table_model.column_of("log_number_status")
        )
    finally:
        viewer.close()
        app.processEvents()


def test_pdf_overlay_can_be_shown_and_filtered():
    app = QApplication.instance() or QApplication([])
    viewer = EmbeddedPdfViewer()
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    template = Template(
        name="prueba",
        fields=[
            FieldTemplate(
                id="log_number", x=0.1, y=0.1, w=0.2, h=0.2
            )
        ],
    )
    try:
        viewer._base_image = image
        viewer.set_field_overlay(template, True)
        assert viewer._source is not None
        assert viewer._source.toImage().pixelColor(10, 15) != QColor(
            Qt.GlobalColor.white
        )

        viewer.set_field_overlay(template, True, set())
        assert viewer._source.toImage().pixelColor(10, 15) == QColor(
            Qt.GlobalColor.white
        )
    finally:
        viewer.close()
        app.processEvents()


def test_true_dup_uses_warning_color_convention():
    app = QApplication.instance() or QApplication([])
    viewer = CsvViewerWindow(Path("."))

    assert viewer._status_for({"dup": "true"}, "dup") == "WARNING"
    assert viewer._status_for({"dup": "false"}, "dup") is None

    viewer.close()
    app.processEvents()


def test_minimal_csv_recovers_field_color_from_companion_json(tmp_path: Path):
    """El CSV minimo toma el color y las columnas de su companero completo.

    ``find_csv_files`` abre por defecto el CSV mínimo (ordena antes que
    ``_completo``), pero la consulta no debe quedar limitada a ese recorte.
    """
    import json

    from app.models.schemas import FieldResult, PageResult, ValidationReport
    from app.reports.csv_reporter import CsvReporter
    from app.reports.dual_csv import write_minimal_csv
    from app.templates.manager import TemplateManager

    app = QApplication.instance() or QApplication([])
    template = TemplateManager().load(
        Path(__file__).resolve().parents[1] / "template" / "aircraft_log.json"
    )
    page = PageResult(page_number=1)
    page.add_field(
        FieldResult(
            page_number=1,
            field_id="log_number",
            field_type="ocr",
            value="2147337",
            confidence=0.4,
            status="ERROR",
        )
    )
    reports = [
        ValidationReport(
            pdf_path=str(FIXTURES / "test.pdf"),
            template_name=template.name,
            pages=[page],
        )
    ]
    run = tmp_path / "run"
    data = run / "datos"
    data.mkdir(parents=True)
    complete_path = data / "run_completo.CSV"
    minimal_path = data / "run.CSV"
    CsvReporter().write(reports, complete_path, template)
    write_minimal_csv(complete_path, minimal_path)
    (data / "run.json").write_text(
        json.dumps({"reportes": [r.model_dump(mode="json") for r in reports]}),
        encoding="utf-8",
    )

    viewer = CsvViewerWindow(tmp_path)
    assert viewer.load_csv_file(minimal_path)

    assert "log_number_status" in viewer._columns
    row = viewer._rows[0]
    assert viewer._status_for(row, "log_number") == "ERROR"

    viewer.close()
    app.processEvents()


def test_row_and_column_indicators_are_visible(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    run = tmp_path / "run"
    data = run / "datos"
    data.mkdir(parents=True)
    (data / "run.csv").write_text(
        "file,page,log_number\na.pdf,1,1234500\n",
        encoding="utf-8",
    )
    viewer = CsvViewerWindow(tmp_path)

    assert viewer.load_folder(run)
    modelo = viewer.table_model
    horizontal = [
        modelo.headerData(index, Qt.Orientation.Horizontal)
        for index in range(modelo.columnCount())
    ]

    # La primera columna es la de las casillas y va sin rótulo.
    assert horizontal == ["", "file", "page", "log_number"]
    assert modelo.rowCount() == 1
    # La cabecera se pinta explícitamente: sin fondo y color propios, sobre la
    # tabla oscura el estilo nativo dejaría los rótulos ilegibles.
    stylesheet = viewer.styleSheet()
    assert f"background-color: {paleta().TABLE_HEADER_BG}" in stylesheet
    assert f"color: {paleta().TABLE_TEXT}" in stylesheet
    viewer.close()
    app.processEvents()


def test_csv_tables_use_the_grey_of_the_interface(tmp_path: Path):
    """La tabla no puede volver al blanco de fábrica de Qt, aquí ni en la principal."""
    app = QApplication.instance() or QApplication([])
    from app.gui.main_window import MainWindow

    viewer = CsvViewerWindow(tmp_path)
    main = MainWindow()
    try:
        for table in (viewer.table, main.table):
            palette = table.palette()
            assert table.alternatingRowColors()
            for group in (
                QPalette.ColorGroup.Active,
                QPalette.ColorGroup.Inactive,
                QPalette.ColorGroup.Disabled,
            ):
                base = palette.color(group, QPalette.ColorRole.Base)
                alternate = palette.color(group, QPalette.ColorRole.AlternateBase)
                assert base == QColor(paleta().TABLE_BASE_BG)
                assert alternate == QColor(paleta().TABLE_ALTERNATE_BG)
                assert base != QColor("white")
        assert f"background-color: {paleta().TABLE_BASE_BG}" in viewer.styleSheet()
        assert f"background-color: {paleta().TABLE_BASE_BG}" in main.styleSheet()
        # El panel del PDF comparte el gris; en blanco resaltaba junto a la tabla.
        assert f"background: {paleta().PANE_BG}" in viewer.pdf_viewer.styleSheet()
    finally:
        main.close()
        viewer.close()
        app.processEvents()


def test_pdf_surface_is_not_the_white_of_qt(tmp_path: Path):
    """El fondo tras la página lo pinta el viewport, no la hoja de estilo.

    Es el mismo fallo que dejaba la tabla en blanco: ``QScrollArea`` pinta su
    viewport desde el rol ``Base``, así que una regla sobre ``#pdfSurface`` no
    lo alcanza y quedaba el blanco de fábrica alrededor de la página.
    """
    app = QApplication.instance() or QApplication([])
    viewer = EmbeddedPdfViewer()
    try:
        viewport = viewer.scroll.viewport()
        assert viewport.autoFillBackground()
        for group in (
            QPalette.ColorGroup.Active,
            QPalette.ColorGroup.Inactive,
            QPalette.ColorGroup.Disabled,
        ):
            base = viewport.palette().color(group, QPalette.ColorRole.Base)
            assert base == QColor(paleta().PANE_SURFACE_BG)
            assert base != QColor("white")
        # El panel que lo rodea acompaña a la tabla, y su texto se aclara para
        # seguir leyéndose sobre el gris oscuro.
        pane = viewer.palette()
        assert pane.color(QPalette.ColorRole.Window) == QColor(paleta().PANE_BG)
        assert pane.color(QPalette.ColorRole.WindowText) == QColor(paleta().PANE_TEXT)
    finally:
        viewer.close()
        app.processEvents()


def test_source_status_stays_readable_on_the_dark_pane(tmp_path: Path):
    """Los tonos de estado de la tabla son para relleno, no para texto."""
    app = QApplication.instance() or QApplication([])
    viewer = EmbeddedPdfViewer()
    try:
        viewer.load_paths([], ["falta.pdf"])
        stylesheet = viewer.source_status.styleSheet()
        assert pane_status_colors()["ERROR"].lower() in stylesheet.lower()
        # El rojo oscuro de las celdas quedaría casi invisible sobre el panel.
        assert "#cf222e" not in stylesheet.lower()
    finally:
        viewer.close()
        app.processEvents()


def test_column_control_is_hidden_until_a_csv_is_loaded(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    viewer = CsvViewerWindow(tmp_path)
    assert viewer.column_toggle.isHidden()

    run = tmp_path / "run"
    data = run / "datos"
    data.mkdir(parents=True)
    (data / "run.csv").write_text(
        "file,page,matricula,date\na.pdf,1,HP-1234CMP,2026/08/13\n",
        encoding="utf-8",
    )
    assert viewer.load_folder(run) is True
    app.processEvents()
    assert not viewer.column_toggle.isHidden()
    viewer.show()
    app.processEvents()
    # El PDF va a la izquierda y la tabla a la derecha, con el mismo reparto
    # (2:3) que la vista previa y la tabla de la ventana principal.
    assert viewer.content_splitter.widget(0) is viewer.pdf_viewer
    assert viewer.content_splitter.widget(1) is viewer.table
    pdf_width, csv_width = viewer.content_splitter.sizes()
    assert abs(pdf_width / (pdf_width + csv_width) - 0.4) <= 0.01


def test_viewer_is_an_unowned_normal_window(tmp_path: Path):
    """Windows solo da botón propio en la barra a ventanas sin dueño."""
    app = QApplication.instance() or QApplication([])
    viewer = CsvViewerWindow(tmp_path)
    try:
        assert viewer.parent() is None
        assert viewer.windowType() == Qt.WindowType.Window
    finally:
        viewer.close()
        app.processEvents()


def test_companion_json_resolves_source_pdf_per_csv_row(tmp_path: Path):
    source_a = tmp_path / "source-a" / "same.pdf"
    source_b = tmp_path / "source-b" / "same.pdf"
    data = tmp_path / "run" / "datos"
    data.mkdir(parents=True)
    csv_path = data / "run.csv"
    csv_path.write_text("file,page,log_number\nsame.pdf,1,1234500\nsame.pdf,1,2234500\n")
    csv_path.with_suffix(".json").write_text(
        __import__("json").dumps(
            {
                "reportes": [
                    {"pdf_path": str(source_a), "pages": [{"page_number": 1}]},
                    {"pdf_path": str(source_b), "pages": [{"page_number": 1}]},
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = [
        {"file": "same.pdf", "page": "1", "log_number": "1234500"},
        {"file": "same.pdf", "page": "1", "log_number": "2234500"},
    ]
    assert source_pdf_paths_for_rows(csv_path, rows) == [source_a, source_b]


def test_a_single_csv_can_be_opened_without_choosing_its_folder(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    data = tmp_path / "run" / "datos"
    data.mkdir(parents=True)
    (data / "run.csv").write_text(
        "file,page,log_number\na.pdf,1,1234500\n", encoding="utf-8"
    )
    (data / "run_completo.csv").write_text(
        "file,page,log_number\na.pdf,1,1234500\n", encoding="utf-8"
    )
    viewer = CsvViewerWindow(tmp_path)

    assert viewer.load_csv_file(data / "run_completo.csv") is True

    # El CSV elegido queda activo y sus vecinos siguen a un clic de distancia.
    assert viewer.csv_combo.currentText() == "run_completo.csv"
    assert viewer.csv_combo.count() == 2
    assert viewer.table_model.rowCount() == 1
    viewer.close()
    app.processEvents()


def test_source_documents_are_reported_as_missing_when_they_moved(tmp_path: Path):
    import json

    moved = tmp_path / "movidos" / "origen.pdf"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"%PDF-1.4\n")
    data = tmp_path / "run" / "datos"
    data.mkdir(parents=True)
    csv_path = data / "run.csv"
    csv_path.write_text("file,page,log_number\norigen.pdf,1,1234500\n")
    csv_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "reportes": [
                    {
                        "pdf_path": str(tmp_path / "viejo" / "origen.pdf"),
                        "pages": [{"page_number": 1}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rows = [{"file": "origen.pdf", "page": "1", "log_number": "1234500"}]

    row_paths, documents, missing = resolve_source_documents(csv_path, rows)
    assert (row_paths, documents, missing) == ([None], [], ["origen.pdf"])

    row_paths, documents, missing = resolve_source_documents(
        csv_path, rows, [moved.parent]
    )
    assert documents == [moved]
    assert missing == []
    assert row_paths == [moved]


def test_source_documents_fall_back_to_the_file_column_without_companion_json(
    tmp_path: Path,
):
    data = tmp_path / "run" / "datos"
    data.mkdir(parents=True)
    csv_path = data / "run.csv"
    csv_path.write_text("file,page\nsuelto.pdf,1\n")
    source = data.parent / "suelto.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    row_paths, documents, missing = resolve_source_documents(
        csv_path, [{"file": "suelto.pdf", "page": "1"}]
    )

    assert documents == [source]
    assert row_paths == [source]
    assert missing == []


def _viewer_for_search(tmp_path: Path) -> CsvViewerWindow:
    run = tmp_path / "run"
    data = run / "datos"
    data.mkdir(parents=True)
    (data / "run.csv").write_text(
        "file,page,log_number,matricula\n"
        "a.pdf,3,1234500,HP-1534CMP\n"
        "b.pdf,7,1234501,HP-1538CMP\n"
        "c.pdf,9,1234512,HP-1534CMP\n",
        encoding="utf-8",
    )
    viewer = CsvViewerWindow(tmp_path)
    assert viewer.load_folder(run)
    return viewer


def test_search_finds_the_logbook_by_its_number(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    viewer = _viewer_for_search(tmp_path)

    viewer.search_edit.setText("1234501")
    viewer._find_in_csv()

    assert viewer._search_matches == [(1, "log_number")]
    assert viewer.table.currentIndex().row() == 1
    assert "b.pdf, página 7" in viewer.search_context.text()
    viewer.close()
    app.processEvents()


def test_search_covers_any_column_of_the_csv(tmp_path: Path):
    """Matrícula, archivo, página: lo que muestra la tabla se puede buscar."""
    app = QApplication.instance() or QApplication([])
    viewer = _viewer_for_search(tmp_path)

    viewer.search_edit.setText("hp-1534cmp")
    viewer._find_in_csv()
    # Sin distinguir mayúsculas y con las dos filas de esa matrícula.
    assert [row for row, _column in viewer._search_matches] == [0, 2]
    assert "en «matricula»" in viewer.search_context.text()

    viewer.search_edit.setText("c.pdf")
    viewer._find_in_csv()
    assert viewer._search_matches == [(2, "file")]
    assert "c.pdf, página 9" in viewer.search_context.text()

    viewer.close()
    app.processEvents()


def test_search_puts_the_whole_value_before_the_partial_ones(tmp_path: Path):
    """Escribir la bitácora entera lleva a esa, no a la que la contiene."""
    app = QApplication.instance() or QApplication([])
    viewer = _viewer_for_search(tmp_path)

    viewer.search_edit.setText("123451")
    viewer._find_in_csv()

    # 1234512 la contiene; ninguna celda vale «123451».
    assert [row for row, _column in viewer._search_matches] == [2]

    viewer.search_edit.setText("9")
    viewer._find_in_csv()
    # La página 9 vale exactamente «9»; 1234512 solo la contiene.
    assert viewer._search_matches[0] == (2, "page")

    viewer.close()
    app.processEvents()


def test_repeating_the_search_walks_the_matches(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    viewer = _viewer_for_search(tmp_path)

    viewer.search_edit.setText("HP-1534CMP")
    viewer._find_in_csv()
    assert viewer._search_position == 0

    viewer._find_in_csv()
    assert viewer._search_position == 1
    assert viewer.table.currentIndex().row() == 2

    # Al vaciar el campo la búsqueda se olvida en vez de quedar a medias.
    viewer.search_edit.setText("")
    viewer._find_in_csv()
    assert viewer._search_matches == []
    assert not viewer.search_next.isEnabled()

    viewer.close()
    app.processEvents()


def test_search_without_matches_says_so(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    viewer = _viewer_for_search(tmp_path)

    viewer.search_edit.setText("XX-9999")
    viewer._find_in_csv()

    assert viewer._search_matches == []
    assert "sin coincidencias" in viewer.search_context.text()
    viewer.close()
    app.processEvents()


def _stub_pdf(monkeypatch, pages: int = 12, size: tuple[int, int] = (20, 30)):
    """Evita rasterizar un PDF real en las pruebas del visor."""
    import numpy as np
    from app.vision import pdf_loader

    monkeypatch.setattr(pdf_loader, "page_count", lambda _path: pages)
    monkeypatch.setattr(
        pdf_loader,
        "render_page",
        lambda _path, _page, dpi=150: np.zeros((*size, 3), dtype=np.uint8),
    )


def test_pdf_page_is_text_field_without_go_button(tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QPushButton

    app = QApplication.instance() or QApplication([])
    pdf_path = tmp_path / "book.pdf"
    pdf_path.touch()
    _stub_pdf(monkeypatch)
    viewer = EmbeddedPdfViewer()

    viewer.load_paths([pdf_path])
    viewer.page_edit.setText("7")
    viewer.page_edit.editingFinished.emit()

    assert viewer.pdf_combo.currentText() == "book.pdf"
    assert viewer._page == 7
    assert viewer.total_pages.text() == "de 12"
    assert "Ir" not in {
        button.text() for button in viewer.findChildren(QPushButton)
    }
    viewer.close()
    app.processEvents()


def test_every_source_pdf_is_selectable_and_switching_resets_the_page(
    tmp_path: Path, monkeypatch
):
    app = QApplication.instance() or QApplication([])
    first = tmp_path / "uno.pdf"
    second = tmp_path / "dos.pdf"
    first.touch()
    second.touch()
    _stub_pdf(monkeypatch)
    viewer = EmbeddedPdfViewer()

    viewer.load_paths([first, second])
    viewer.show_page(5)
    assert viewer._page == 5

    viewer.pdf_combo.setCurrentIndex(1)

    assert [
        viewer.pdf_combo.itemText(index)
        for index in range(viewer.pdf_combo.count())
    ] == ["uno.pdf", "dos.pdf"]
    assert viewer._path == second
    assert viewer._page == 1
    assert viewer.page_edit.text() == "13"
    assert viewer.total_pages.text() == "de 24"
    assert "2 PDF de origen disponibles" in viewer.source_status.text()
    viewer.close()
    app.processEvents()


def test_pdf_pagination_crosses_documents_and_uses_the_combined_total(
    tmp_path: Path, monkeypatch
):
    """La numeración del visor es continua entre todos los PDF de origen."""
    app = QApplication.instance() or QApplication([])
    from app.vision import pdf_loader

    first = tmp_path / "uno.pdf"
    second = tmp_path / "dos.pdf"
    first.touch()
    second.touch()
    totals = {first: 2, second: 3}
    monkeypatch.setattr(pdf_loader, "page_count", lambda path: totals[Path(path)])
    monkeypatch.setattr(
        pdf_loader,
        "render_page",
        lambda _path, _page, dpi=150: __import__("numpy").zeros(
            (20, 30, 3), dtype="uint8"
        ),
    )
    viewer = EmbeddedPdfViewer()

    viewer.load_paths([first, second])
    assert viewer.total_pages.text() == "de 5"

    viewer.show_page(3)
    assert viewer._path == second
    assert viewer._page == 1
    assert viewer.page_edit.text() == "3"

    # Las filas del CSV entregan una página local y el PDF que la originó.
    viewer.show_page(3, second)
    assert viewer._path == second
    assert viewer._page == 3
    assert viewer.page_edit.text() == "5"

    viewer._show_previous_page()
    assert viewer._path == second
    assert viewer._page == 2
    viewer.close()
    app.processEvents()


def test_csv_viewer_counts_execution_pages_and_clicks_their_source_page(
    tmp_path: Path, monkeypatch
):
    """Los huecos del PDF no cuentan como filas ni desplazan el clic del CSV."""
    import json

    app = QApplication.instance() or QApplication([])
    run = tmp_path / "run"
    data = run / "datos"
    data.mkdir(parents=True)
    first = run / "uno.pdf"
    second = run / "dos.pdf"
    first.touch()
    second.touch()
    csv_path = data / "run.csv"
    csv_path.write_text(
        "file,page,log_number\n"
        "uno.pdf,2,1234500\n"
        "uno.pdf,4,1234501\n"
        "dos.pdf,3,1234502\n",
        encoding="utf-8",
    )
    csv_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "reportes": [
                    {
                        "pdf_path": str(first),
                        "pages": [{"page_number": 2}, {"page_number": 4}],
                    },
                    {
                        "pdf_path": str(second),
                        "pages": [{"page_number": 3}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    from app.vision import pdf_loader

    monkeypatch.setattr(pdf_loader, "page_count", lambda _path: 5)
    monkeypatch.setattr(
        pdf_loader,
        "render_page",
        lambda _path, _page, dpi=150: __import__("numpy").zeros(
            (20, 30, 3), dtype="uint8"
        ),
    )
    viewer = CsvViewerWindow(tmp_path / "sin-historial")

    assert viewer.load_csv_file(csv_path)
    # Los originales suman diez hojas, pero la ejecución contiene tres.
    assert viewer.pdf_viewer.total_pages.text() == "de 3"

    viewer.table.clicked.emit(viewer.table_model.index(1, 0))
    assert viewer.pdf_viewer._path == first
    assert viewer.pdf_viewer._page == 4
    assert viewer.pdf_viewer.page_edit.text() == "2"

    viewer.table.clicked.emit(viewer.table_model.index(2, 0))
    assert viewer.pdf_viewer._path == second
    assert viewer.pdf_viewer._page == 3
    assert viewer.pdf_viewer.page_edit.text() == "3"
    viewer.pdf_viewer.shutdown()
    viewer.close()
    app.processEvents()


def test_clicking_the_current_row_locates_its_pdf_again(tmp_path: Path):
    """Un segundo clic en la misma bitácora no cambia la celda actual."""
    app = QApplication.instance() or QApplication([])
    viewer = CsvViewerWindow(tmp_path)
    source = tmp_path / "bitacora.pdf"
    source.touch()
    viewer._rows = [{"file": source.name, "page": "7"}]
    viewer._row_pdf_paths = [source]
    viewer.table_model.set_content(["file", "page"], viewer._rows)
    shown: list[tuple[int, Path | None]] = []
    viewer.pdf_viewer.show_page = lambda page, path=None: shown.append(
        (page, Path(path) if path is not None else None)
    )

    viewer.table.setCurrentIndex(viewer.table_model.index(0, 1))
    shown.clear()
    viewer.table.clicked.emit(viewer.table_model.index(0, 1))

    assert shown == [(1, None)]
    viewer.close()
    app.processEvents()


def test_missing_source_pdfs_are_announced_in_the_indicator(
    tmp_path: Path, monkeypatch
):
    app = QApplication.instance() or QApplication([])
    available = tmp_path / "uno.pdf"
    available.touch()
    _stub_pdf(monkeypatch)
    viewer = EmbeddedPdfViewer()

    viewer.load_paths([available], ["dos.pdf"])

    assert "1 de 2 PDF de origen disponibles" in viewer.source_status.text()
    assert "dos.pdf" in viewer.source_status.text()
    assert viewer.locate_button.isVisibleTo(viewer)

    viewer.load_paths([], ["uno.pdf", "dos.pdf"])

    assert "No se encontraron los 2 PDF de origen" in viewer.source_status.text()
    assert "No se encontraron los PDF de origen" in viewer.image.text()
    viewer.close()
    app.processEvents()


def _settle(app, passes: int = 3) -> None:
    """Deja que el visor termine de reajustarse tras cambiar de tamaño."""
    for _ in range(passes):
        app.processEvents()


def _await_page(app, viewer, timeout: float = 5.0) -> None:
    """Espera la página que el visor pidió a su hilo de render.

    El panel ya no rasteriza en el hilo de interfaz, así que la imagen llega
    por señal: la prueba tiene que dejar correr la cola de eventos.
    """
    deadline = time.monotonic() + timeout
    while viewer._pending_render is not None and time.monotonic() < deadline:
        app.processEvents()
        QThread.msleep(5)
    _settle(app)


def test_placeholder_message_keeps_contrast_over_the_page_surface():
    app = QApplication.instance() or QApplication([])
    viewer = EmbeddedPdfViewer()
    viewer.resize(520, 600)
    viewer.show()
    _settle(app)

    viewer.load_paths([], ["perdido.pdf"])
    _settle(app)

    viewport = viewer.scroll.viewport()
    center = viewport.mapTo(viewer, viewport.rect().center())
    color = viewer.grab().toImage().pixelColor(center.x(), center.y())
    # El texto del mensaje es claro: la superficie debe seguir siendo oscura.
    assert color.lightness() < 128
    viewer.close()
    app.processEvents()


def test_the_page_is_rasterised_outside_the_interface_thread(
    tmp_path: Path, monkeypatch
):
    """Rasterizar en el hilo de interfaz congelaba 90 ms por fila recorrida.

    La navegación (página, total, selector) se actualiza en el acto; la
    imagen llega después por señal, igual que en la vista previa principal.
    """
    app = QApplication.instance() or QApplication([])
    pdf_path = tmp_path / "book.pdf"
    pdf_path.touch()
    _stub_pdf(monkeypatch)
    viewer = EmbeddedPdfViewer()

    viewer.load_paths([pdf_path])

    assert viewer._page == 1
    assert viewer._total == 12
    assert viewer.total_pages.text() == "de 12"
    # La imagen todavía no está: el hilo de render la entrega más tarde.
    assert viewer._pending_render == (str(pdf_path), 1)

    _await_page(app, viewer)

    assert viewer._pending_render is None
    assert not viewer.image.pixmap().isNull()
    viewer.close()
    app.processEvents()


def test_the_page_count_is_read_once_per_document(tmp_path: Path, monkeypatch):
    """Cada salto de página reabría el PDF solo para contar sus páginas."""
    app = QApplication.instance() or QApplication([])
    from app.vision import pdf_loader

    counted: list[Path] = []
    monkeypatch.setattr(
        pdf_loader, "page_count", lambda path: counted.append(Path(path)) or 12
    )
    monkeypatch.setattr(
        pdf_loader,
        "render_page",
        lambda _path, _page, dpi=150: __import__("numpy").zeros(
            (20, 30, 3), dtype="uint8"
        ),
    )
    pdf_path = tmp_path / "book.pdf"
    pdf_path.touch()
    viewer = EmbeddedPdfViewer()

    viewer.load_paths([pdf_path])
    viewer.show_page(3)
    viewer.show_page(7)
    _await_page(app, viewer)

    assert counted == [pdf_path]
    viewer.close()
    app.processEvents()


def test_a_long_csv_is_ready_without_filling_cell_by_cell(tmp_path: Path):
    """La tabla lee del CSV en memoria: abrirla no cuesta una celda por dato."""
    app = QApplication.instance() or QApplication([])
    run = tmp_path / "run"
    data = run / "datos"
    data.mkdir(parents=True)
    columns = [f"c{index}" for index in range(20)]
    lines = ["file,page," + ",".join(columns)]
    lines.extend(
        f"a.pdf,{row},"
        + ",".join(f"v{row}_{index}" for index in range(len(columns)))
        for row in range(1, 401)
    )
    (data / "run.csv").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    viewer = CsvViewerWindow(tmp_path)

    assert viewer.load_folder(run)

    modelo = viewer.table_model
    # Entera desde el primer momento, sin tramos pendientes.
    assert modelo.rowCount() == 400
    assert modelo.index(0, 1).data() == "a.pdf"
    assert modelo.index(399, 2).data() == "400"
    assert modelo.index(399, 3).data() == "v400_0"
    assert [modelo.source_row(row) for row in range(400)] == list(range(400))
    viewer.close()
    app.processEvents()


def test_page_is_scaled_to_fill_the_available_panel(tmp_path: Path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    pdf_path = tmp_path / "book.pdf"
    pdf_path.touch()
    _stub_pdf(monkeypatch, pages=1, size=(1650, 1275))
    viewer = EmbeddedPdfViewer()
    viewer.resize(500, 700)
    viewer.show()
    _settle(app)

    viewer.load_paths([pdf_path])
    _await_page(app, viewer)

    viewport = viewer.scroll.viewport().size()
    pixmap = viewer.image.pixmap()
    # Sin ajuste la página se renderizaba a tamaño fijo y solo se veía una
    # franja del borde superior.
    assert pixmap.height() <= viewport.height()
    assert pixmap.height() > viewport.height() * 0.8
    assert pixmap.width() <= viewport.width()
    # El área útil del QLabel coincide con la imagen. Si el estilo vuelve a
    # reservar padding dentro del mismo tamaño, Qt recorta los cuatro bordes.
    assert viewer.image.contentsRect().size() == pixmap.size()

    viewer._zoom_by(2.0)
    assert viewer.image.pixmap().height() > pixmap.height()
    assert viewer.zoom_label.text() == "200%"

    viewer.fit_page()
    assert viewer.image.pixmap().height() == pixmap.height()
    viewer.close()
    app.processEvents()


def _run_with_companion_json(tmp_path: Path, pdf_path: Path) -> tuple[Path, Path]:
    """Ejecución mínima en disco: CSV, CSV completo y su JSON consolidado."""
    import json

    from app.models.schemas import FieldResult, PageResult, ValidationReport
    from app.reports.csv_reporter import CsvReporter
    from app.templates.manager import TemplateManager

    template = TemplateManager().load(
        Path(__file__).resolve().parents[1] / "template" / "aircraft_log.json"
    )

    def _page(number: int, log: str, matricula: str) -> PageResult:
        # La ejecución de prueba representa filas completas para AirVault;
        # la ausencia de fecha se prueba aparte como ruta de REVISAR.
        page = PageResult(page_number=number, date="2026/08/17")
        for field_id, value in (("log_number", log), ("matricula", matricula)):
            page.add_field(
                FieldResult(
                    page_number=number,
                    field_id=field_id,
                    field_type="ocr",
                    value=value,
                    confidence=1.0,
                    status="OK",
                )
            )
        # Entrada de vuelo con sus tres firmas leidas: sin ellas la pagina
        # seria una discrepancia y saldria del PDF de su matricula. Las dos
        # casillas del tecnico van vacias, que es lo que la hace de vuelo.
        for field_id, value in (
            ("technician_signature", "false"),
            ("technician_license", "false"), ("pilot_signature", "true"),
            ("captain_signature", "true"), ("captain_license", "true"),
        ):
            page.add_field(
                FieldResult(
                    page_number=number,
                    field_id=field_id,
                    field_type="signature",
                    value=value,
                    confidence=0.99,
                    status="OK",
                )
            )
        return page

    reports = [
        ValidationReport(
            pdf_path=str(pdf_path),
            template_name=template.name,
            pages=[_page(1, "2147337", "HP-1534CMP"), _page(2, "2147338", "HP-1538CMP")],
        )
    ]
    run = tmp_path / "BITS 17 AUG 2026 06 00"
    data = run / "datos"
    data.mkdir(parents=True)
    csv_path = data / f"{run.name}.CSV"
    CsvReporter().write(reports, csv_path, template)
    (data / f"{run.name}.json").write_text(
        json.dumps({"reportes": [r.model_dump(mode="json") for r in reports]}),
        encoding="utf-8",
    )
    return run, csv_path


def test_run_folder_is_recovered_from_the_csv_location(tmp_path: Path):
    run = tmp_path / "BITS TEST"
    assert run_dir_for_csv(run / "datos" / "BITS TEST.CSV") == run
    assert run_dir_for_csv(run / "datos" / "BITS TEST_completo.CSV") == run
    # Las ejecuciones históricas dejaban el reporte en la raíz de la ejecución.
    assert run_dir_for_csv(run / "BITS TEST.CSV") == run
    # Una copia suelta no identifica ninguna carpeta: volver a exportar sobre
    # ella borraría los archivos que la ejecución regenera, que ahí son ajenos.
    assert run_dir_for_csv(tmp_path / "Documentos" / "BITS TEST.CSV") is None
    assert run_dir_for_csv(run / "datos" / "copia.CSV") is None


def test_reports_are_rebuilt_from_the_companion_json(tmp_path: Path):
    pdf_path = FIXTURES / "test.pdf"
    _run, csv_path = _run_with_companion_json(tmp_path, pdf_path)

    reports, missing = reports_for_csv(csv_path)

    assert missing == []
    assert [Path(report.pdf_path) for report in reports] == [pdf_path]
    # El JSON guarda los reportes enteros: la ejecución se re-exporta sin OCR.
    assert [page.page_number for page in reports[0].pages] == [1, 2]
    assert reports[0].pages[0].fields[0].value == "2147337"


def test_moved_source_pdfs_are_relocated_or_reported_as_missing(tmp_path: Path):
    # Un nombre que no exista en input/, donde el visor también busca.
    moved = tmp_path / "otra carpeta" / "bitacora-movida.pdf"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"%PDF-1.4\n")
    _run, csv_path = _run_with_companion_json(
        tmp_path, tmp_path / "ya no" / moved.name
    )

    _reports, missing = reports_for_csv(csv_path)
    assert missing == [moved.name]

    # Con la carpeta indicada a mano («Ubicar PDF…») el reporte vuelve a
    # apuntar al archivo, que es de donde se rehacen las páginas.
    reports, missing = reports_for_csv(csv_path, [moved.parent])
    assert missing == []
    assert Path(reports[0].pdf_path) == moved


def test_export_is_offered_only_when_the_run_can_be_rebuilt(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    _run, csv_path = _run_with_companion_json(tmp_path, FIXTURES / "test.pdf")
    viewer = CsvViewerWindow(tmp_path)
    try:
        assert viewer.load_csv_file(csv_path) is True
        assert viewer.btn_export.isEnabled()

        # Un CSV suelto no trae el JSON de su ejecución y no se puede rehacer.
        loose = tmp_path / "suelto.csv"
        loose.write_text("file,page\na.pdf,1\n", encoding="utf-8")
        assert viewer.load_csv_file(loose) is True
        assert not viewer.btn_export.isEnabled()
        assert "JSON" in viewer.btn_export.toolTip()

        # Al volver a la ejecución el botón recupera su explicación de siempre.
        assert viewer.load_csv_file(csv_path) is True
        assert viewer.btn_export.isEnabled()
        assert "Volver a generar" in viewer.btn_export.toolTip()
    finally:
        viewer.close()
        app.processEvents()


def test_exporting_rewrites_the_run_without_reprocessing(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    run, csv_path = _run_with_companion_json(tmp_path, FIXTURES / "test.pdf")
    viewer = CsvViewerWindow(tmp_path)
    try:
        assert viewer.load_csv_file(csv_path) is True
        # Un archivo por matrícula y sin apartar las posibles discrepancias:
        # el cuadro arranca en un solo PDF y con ellas marcadas, así que aquí
        # se elige lo que comprueban las rutas de abajo.
        viewer.export_options.set_un_solo_pdf(False)
        viewer.export_options.matricula_check.setChecked(True)
        viewer.export_options.discrepancias_check.setChecked(False)
        viewer._exportar()
        worker = viewer._outputs_worker
        assert worker is not None
        assert worker.wait(120000)
        app.processEvents()
        app.processEvents()

        # Las mismas salidas que produce «Exportar» en la ventana principal,
        # escritas sobre la carpeta de la ejecución abierta.
        assert (run / "HP-1534CMP.pdf").is_file()
        assert (run / "HP-1538CMP.pdf").is_file()
        assert (run / "stats.json").is_file()
        assert csv_path.is_file()
        assert "exportación terminada" in viewer.status_label.text()
    finally:
        viewer.close()
        app.processEvents()


def test_el_numero_tecleado_en_el_visor_sobrevive_a_un_refresco(
    tmp_path: Path, monkeypatch
):
    """Lo que se escribe en la caja manda sobre los refrescos de fondo.

    La caja la reescribe cualquier cosa que mueva la página: la fila que se
    elige en la tabla, el PDF que se carga, el render que llega. Pisar lo
    tecleado hacía que Enter saltara a la página que ya estaba a la vista.
    """
    from PySide6.QtTest import QTest

    app = QApplication.instance() or QApplication([])
    pdf_path = tmp_path / "book.pdf"
    pdf_path.touch()
    _stub_pdf(monkeypatch)
    viewer = EmbeddedPdfViewer()
    viewer.load_paths([pdf_path])
    viewer.show()
    app.processEvents()

    viewer.page_edit.setFocus()
    viewer.page_edit.selectAll()
    QTest.keyClicks(viewer.page_edit, "9")
    viewer._sync_controls()
    viewer.show_page(viewer._global_index)

    assert viewer.page_edit.text() == "9"

    QTest.keyClick(viewer.page_edit, Qt.Key.Key_Return)
    app.processEvents()

    assert viewer._page == 9
    assert viewer.page_edit.text() == "9"

    # Sin edición a medias la caja vuelve a seguir a la página mostrada.
    viewer.show_page(4)
    assert viewer.page_edit.text() == "4"
    viewer.close()
    app.processEvents()


def test_la_caja_de_pagina_acepta_hasta_el_total_que_anuncia(
    tmp_path: Path, monkeypatch
):
    """El validador va con el total: si se queda corto, Enter no hace nada.

    Sin PDF a la vista la caja seguía admitiendo solo una página, así que Qt
    daba por incompleto cualquier número de dos cifras y no llegaba a emitir
    ``editingFinished``.
    """
    app = QApplication.instance() or QApplication([])
    pdf_path = tmp_path / "book.pdf"
    pdf_path.touch()
    _stub_pdf(monkeypatch)
    viewer = EmbeddedPdfViewer()

    viewer.load_paths(
        [pdf_path], page_refs=[(None, 0)] + [(pdf_path, page) for page in range(1, 12)]
    )

    assert viewer.total_pages.text() == "de 12"
    assert viewer.page_edit.validator().top() == 12
    viewer.close()
    app.processEvents()
