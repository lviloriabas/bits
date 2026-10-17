"""Supr quita de la ejecución las páginas seleccionadas en el visor.

Una bitácora que no debía entrar (una hoja repetida, una página en blanco
que el escáner metió de más) se quita de la ejecución sin repetir el OCR: se
reescriben el CSV, el CSV completo, el JSON y las estadísticas sin ella. Los
PDF ya entregados no se tocan aquí; se rehacen al exportar.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox

from PySide6.QtWidgets import QDialog

from app.gui import csv_viewer
from app.gui.csv_viewer import CsvViewerWindow
from app.gui.depuracion_dialog import DepurarPaginasDialog

from test_csv_viewer import _run_with_companion_json


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class DialogoDuplicados(DepurarPaginasDialog):
    """El cuadro real, con «Duplicados» marcado y aceptado sin ratón."""

    def exec(self) -> int:  # noqa: A003 - API Qt
        self.check_duplicados.setChecked(True)
        return QDialog.DialogCode.Accepted


def _pages_in_csv(csv_path: Path) -> list[str]:
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        return [row["page"] for row in csv.DictReader(handle)]


def _pages_in_json(csv_path: Path) -> list[int]:
    payload = json.loads(
        csv_path.with_suffix(".json").read_text(encoding="utf-8")
    )
    return [
        page["page_number"]
        for report in payload.get("reportes", [])
        for page in report.get("pages", [])
    ]


def _viewer_on_run(tmp_path: Path) -> tuple[CsvViewerWindow, Path, Path]:
    run, csv_path = _run_with_companion_json(tmp_path, FIXTURES / "test.pdf")
    viewer = CsvViewerWindow(tmp_path)
    assert viewer.load_csv_file(csv_path)
    return viewer, run, csv_path


def _wait_for_outputs(viewer: CsvViewerWindow, app: QApplication) -> None:
    worker = viewer._outputs_worker
    assert worker is not None, "no se lanzó la escritura de la ejecución"
    assert worker.wait(120000)
    app.processEvents()
    app.processEvents()


def test_supr_borra_de_la_corrida_las_paginas_seleccionadas(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    viewer, _run, csv_path = _viewer_on_run(tmp_path)
    try:
        assert _pages_in_csv(csv_path) == ["1", "2"]
        viewer.table.selectRow(0)

        with patch(
            "app.gui.csv_viewer.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Yes,
        ) as confirm:
            viewer._delete_selected_pages()
        _wait_for_outputs(viewer, app)

        # Se avisa de lo que se va a perder antes de tocar nada.
        assert "1 página(s)" in confirm.call_args.args[2]
        assert _pages_in_csv(csv_path) == ["2"]
        assert _pages_in_json(csv_path) == [2]
        # El JSON no puede seguir diciendo que la ejecución tiene dos páginas.
        payload = json.loads(
            csv_path.with_suffix(".json").read_text(encoding="utf-8")
        )
        assert payload["reportes"][0]["summary"]["total_pages"] == 1
        # Y la tabla se recarga con lo que quedó.
        assert viewer.table_model.rowCount() == 1
        assert "páginas excluidas" in viewer.status_label.text()
    finally:
        viewer.pdf_viewer.shutdown()
        viewer.close()
        app.processEvents()


def test_cancelar_la_confirmacion_no_borra_nada(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    viewer, _run, csv_path = _viewer_on_run(tmp_path)
    try:
        viewer.table.selectRow(0)

        with patch(
            "app.gui.csv_viewer.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.No,
        ):
            viewer._delete_selected_pages()

        assert viewer._outputs_worker is None
        assert _pages_in_csv(csv_path) == ["1", "2"]
    finally:
        viewer.pdf_viewer.shutdown()
        viewer.close()
        app.processEvents()


def test_no_se_puede_dejar_la_corrida_sin_ninguna_pagina(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    viewer, _run, csv_path = _viewer_on_run(tmp_path)
    try:
        viewer.table.selectAll()

        with patch("app.gui.csv_viewer.QMessageBox.information") as aviso, patch(
            "app.gui.csv_viewer.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Yes,
        ) as confirm:
            viewer._delete_selected_pages()

        assert aviso.called and not confirm.called
        assert viewer._outputs_worker is None
        assert _pages_in_csv(csv_path) == ["1", "2"]
    finally:
        viewer.pdf_viewer.shutdown()
        viewer.close()
        app.processEvents()


def test_sin_seleccion_solo_lo_dice(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    viewer, _run, csv_path = _viewer_on_run(tmp_path)
    try:
        viewer.table.clearSelection()

        with patch("app.gui.csv_viewer.QMessageBox.warning") as confirm:
            viewer._delete_selected_pages()

        assert not confirm.called
        assert viewer._outputs_worker is None
        assert "seleccione" in viewer.status_label.text().casefold()
        assert _pages_in_csv(csv_path) == ["1", "2"]
    finally:
        viewer.pdf_viewer.shutdown()
        viewer.close()
        app.processEvents()


def test_un_csv_suelto_no_es_una_corrida_de_la_que_borrar(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    viewer = CsvViewerWindow(tmp_path)
    suelto = tmp_path / "suelto.csv"
    suelto.write_text("file,page\na.pdf,1\n", encoding="utf-8")
    try:
        assert viewer.load_csv_file(suelto)
        viewer.table.selectRow(0)

        with patch("app.gui.csv_viewer.QMessageBox.information") as aviso:
            viewer._delete_selected_pages()

        assert aviso.called
        assert viewer._outputs_worker is None
        assert suelto.read_text(encoding="utf-8") == "file,page\na.pdf,1\n"
    finally:
        viewer.pdf_viewer.shutdown()
        viewer.close()
        app.processEvents()


def _run_with_two_pdfs_named_alike(tmp_path: Path) -> tuple[Path, Path]:
    """Ejecución con dos PDF distintos que se llaman igual, uno por carpeta."""
    from app.models.schemas import FieldResult, PageResult, ValidationReport
    from app.reports.csv_reporter import CsvReporter
    from app.templates.manager import TemplateManager

    template = TemplateManager().load(
        Path(__file__).resolve().parents[1] / "template" / "aircraft_log.json"
    )

    def _page(number: int, log: str) -> PageResult:
        page = PageResult(page_number=number)
        page.add_field(
            FieldResult(
                page_number=number,
                field_id="log_number",
                field_type="ocr",
                value=log,
                confidence=1.0,
                status="OK",
            )
        )
        return page

    reports = []
    for carpeta, log in (("lote_a", "2147337"), ("lote_b", "2147437")):
        pdf = tmp_path / carpeta / "bitacora.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_text("%PDF-1.4\n", encoding="utf-8")
        reports.append(
            ValidationReport(
                pdf_path=str(pdf),
                template_name=template.name,
                pages=[_page(1, log), _page(2, str(int(log) + 1))],
            )
        )

    run = tmp_path / "BITS 19 AUG 2026 06 00"
    data = run / "datos"
    data.mkdir(parents=True)
    csv_path = data / f"{run.name}.CSV"
    CsvReporter().write(reports, csv_path, template)
    (data / f"{run.name}.json").write_text(
        json.dumps({"reportes": [r.model_dump(mode="json") for r in reports]}),
        encoding="utf-8",
    )
    return run, csv_path


def test_el_nombre_repetido_no_borra_la_pagina_del_otro_pdf(tmp_path: Path):
    """Dos PDF con el mismo nombre: se borra el de la fila, no los dos."""
    app = QApplication.instance() or QApplication([])
    _run, csv_path = _run_with_two_pdfs_named_alike(tmp_path)
    viewer = CsvViewerWindow(tmp_path)
    try:
        assert viewer.load_csv_file(csv_path)
        assert _pages_in_json(csv_path) == [1, 2, 1, 2]
        # La primera fila es la página 1 del PDF de lote_a.
        viewer.table.selectRow(0)

        with patch(
            "app.gui.csv_viewer.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            viewer._delete_selected_pages()
        _wait_for_outputs(viewer, app)

        payload = json.loads(
            csv_path.with_suffix(".json").read_text(encoding="utf-8")
        )
        por_pdf = {
            Path(report["pdf_path"]).parent.name: [
                page["page_number"] for page in report["pages"]
            ]
            for report in payload["reportes"]
        }
        assert por_pdf == {"lote_a": [2], "lote_b": [1, 2]}
    finally:
        viewer.pdf_viewer.shutdown()
        viewer.close()
        app.processEvents()


def _repetir_la_bitacora_de_la_primera_pagina(csv_path: Path) -> None:
    """Deja la página 2 con el mismo log_number que la 1 en el JSON.

    La ejecución de prueba trae dos bitácoras distintas; para depurar hace
    falta una repetida, y el criterio la busca en el JSON consolidado, que es
    de donde salen los reportes que se reescriben.
    """
    destino = csv_path.with_suffix(".json")
    payload = json.loads(destino.read_text(encoding="utf-8"))
    paginas = payload["reportes"][0]["pages"]
    primera = next(
        campo["value"]
        for campo in paginas[0]["fields"]
        if campo["field_id"] == "log_number"
    )
    for campo in paginas[1]["fields"]:
        if campo["field_id"] == "log_number":
            campo["value"] = primera
    destino.write_text(json.dumps(payload), encoding="utf-8")


def test_depurar_quita_la_bitacora_repetida_y_conserva_la_primera(tmp_path: Path):
    """El botón «Depurar» reescribe la ejecución sin las páginas repetidas."""
    app = QApplication.instance() or QApplication([])
    viewer, _run, csv_path = _viewer_on_run(tmp_path)
    _repetir_la_bitacora_de_la_primera_pagina(csv_path)
    try:
        assert _pages_in_csv(csv_path) == ["1", "2"]

        with patch.object(csv_viewer, "DepurarPaginasDialog", DialogoDuplicados):
            viewer._depurar_paginas()
        _wait_for_outputs(viewer, app)

        # Se conserva la primera aparición, que es la que se entrega.
        assert _pages_in_csv(csv_path) == ["1"]
        assert _pages_in_json(csv_path) == [1]
        assert viewer.table_model.rowCount() == 1
        assert "páginas excluidas" in viewer.status_label.text()
    finally:
        viewer.pdf_viewer.shutdown()
        viewer.close()
        app.processEvents()


def test_depurar_una_corrida_limpia_no_reescribe_nada(tmp_path: Path):
    """Sin repetidas ni vacías no se lanza ninguna escritura."""
    app = QApplication.instance() or QApplication([])
    viewer, _run, csv_path = _viewer_on_run(tmp_path)
    try:
        with patch.object(csv_viewer, "DepurarPaginasDialog", DialogoDuplicados):
            viewer._depurar_paginas()

        assert viewer._outputs_worker is None
        assert _pages_in_csv(csv_path) == ["1", "2"]
        assert "no había páginas repetidas ni en blanco" in viewer.status_label.text()
    finally:
        viewer.pdf_viewer.shutdown()
        viewer.close()
        app.processEvents()
