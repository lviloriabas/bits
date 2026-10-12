"""Estado visible, zoom, grupos grandes y restauracion desde ambas vistas."""

from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox

from app.gui import csv_viewer, main_window
from app.gui.csv_viewer import CsvViewerWindow
from app.gui.depuracion_dialog import DepurarPaginasDialog
from app.gui.exclusion_worker import ExclusionWorker
from app.gui.main_window import MainWindow
from app.gui.theme import aplicar_tema
from app.gui.tokens import paleta
from app.models.schemas import FieldResult, PageResult, ValidationReport
from tests.test_exclusiones import ejecucion, excluir


def test_numero_de_bitacora_explicito_incluye_ceros_y_compara_tres_paginas(app, tmp_path):
    import pymupdf

    ruta = tmp_path / "tres.pdf"
    with pymupdf.open() as pdf:
        for n in range(3):
            page = pdf.new_page()
            page.insert_text((40, 40), f"Bitacora 0357645, copia {n+1}", fontsize=18)
        pdf.save(ruta)
    report = ValidationReport(pdf_path=str(ruta), template_name="fixture", pages=[
        PageResult(page_number=n, fields=[FieldResult(page_number=n, field_id="log_number",
                   field_type="ocr", value="0357645")]) for n in range(1, 4)
    ])
    dialogo = DepurarPaginasDialog([report])
    dialogo.show()
    app.processEvents()
    grupo = dialogo.arbol_duplicados.topLevelItem(0)
    assert "Número de bitácora: 0357645" in grupo.text(0)
    dialogo.arbol_duplicados.setCurrentItem(grupo.child(0))
    assert "Número de bitácora: 0357645" in dialogo.contexto.text()
    assert dialogo.comparar_combo.count() == 2
    dialogo.comparar_combo.setCurrentIndex(1)
    assert "Página 3" in dialogo.vista_comparada.title()
    assert "Se conservará" in dialogo.vista_elegida.title()
    assert not dialogo.boton_eliminar.isEnabled()
    grupo.child(0).setCheckState(0, Qt.CheckState.Checked)
    assert "Se excluirá" in dialogo.vista_elegida.title()
    assert "Se conservará" in dialogo.vista_comparada.title()
    assert dialogo.boton_eliminar.text() == "Excluir 1 página"
    ancho = dialogo.previa_elegida.pixmap().width()
    with patch("pymupdf.open", side_effect=AssertionError("No debe volver a abrir el PDF al cambiar una marca")):
        grupo.child(0).setCheckState(0, Qt.CheckState.Unchecked)
        assert "Se conservará" in dialogo.vista_elegida.title()
        grupo.child(0).setCheckState(0, Qt.CheckState.Checked)
    dialogo.vista_elegida.zoom.btn_in.click()
    assert dialogo.previa_elegida.pixmap().width() > ancho
    assert dialogo.claves() == {(0, 1)}
    dialogo.vista_elegida.zoom.btn_fit.click()
    assert dialogo.vista_elegida._zoom == 1.0
    for tema in ("claro", "oscuro"):
        aplicar_tema(tema)
        assert paleta().TABLE_BASE_BG in dialogo.arbol_duplicados.styleSheet()
    dialogo.close()


def test_blanco_y_duplicado_muestran_la_misma_decision_y_no_vacian_el_grupo(app):
    reporte = ValidationReport(pdf_path="ausente.pdf", template_name="fixture", pages=[
        PageResult(page_number=n, blank=True, fields=[FieldResult(page_number=n,
                   field_id="log_number", field_type="ocr", value="2357645")]) for n in (1, 2)
    ])
    dialogo = DepurarPaginasDialog([reporte])
    dialogo.check_blancas.setChecked(True)
    assert dialogo.resumen().total == 1
    grupo = dialogo.arbol_duplicados.topLevelItem(0)
    assert "Se conservará" in grupo.child(0).text(0)
    assert grupo.child(1).checkState(0) == Qt.CheckState.Checked
    dialogo.arbol_blancas.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
    assert len(dialogo.claves()) == 1
    dialogo.arbol_duplicados.setCurrentItem(grupo.child(0))
    assert "No se pudo abrir" in dialogo.previa_elegida.text()
    assert not dialogo.vista_elegida.zoom.btn_in.isEnabled()
    dialogo.close()


class RestaurarDialogo(DepurarPaginasDialog):
    def exec(self):
        assert self.boton_restaurar.isEnabled()
        self.boton_restaurar.click()
        return QDialog.DialogCode.Accepted


def ejecutar_directo(worker):
    worker.run()
    worker.finished.emit()


def test_restaura_desde_principal_y_desde_csv_despues_de_reabrir(app, tmp_path, monkeypatch):
    carpeta, reportes, opciones = ejecucion(tmp_path)
    quedan = excluir(carpeta, reportes, opciones, {(0, 2)})
    monkeypatch.setattr(ExclusionWorker, "start", ejecutar_directo)
    monkeypatch.setattr(main_window, "DepurarPaginasDialog", RestaurarDialogo)
    principal = MainWindow()
    principal._processed_template = opciones.template
    principal._reports = quedan
    principal._corrida_dir = carpeta
    principal._depurar_paginas()
    assert [p.page_number for p in principal._reports[0].pages] == [1, 2, 3]
    assert "restaurada" in principal.status_label.text()
    principal.close()

    excluir(carpeta, reportes, opciones, {(0, 2)})
    monkeypatch.setattr(csv_viewer, "DepurarPaginasDialog", RestaurarDialogo)
    monkeypatch.setattr(csv_viewer, "template_for_csv", lambda _path: opciones.template)
    visor = CsvViewerWindow(tmp_path)
    assert visor.load_csv_file(carpeta / "datos" / f"{carpeta.name}.CSV")
    assert len(visor._rows) == 2
    visor._depurar_paginas()
    assert len(visor._rows) == 3
    assert "restaurada" in visor._export_note
    visor.close()


def test_fallo_al_excluir_recupera_tambien_la_tabla_principal(app, tmp_path, monkeypatch):
    carpeta, reportes, opciones = ejecucion(tmp_path)
    principal = MainWindow()
    principal._processed_template = opciones.template
    principal._reports = reportes
    principal._corrida_dir = carpeta

    class ExcluirDialogo(DepurarPaginasDialog):
        def exec(self):
            self.check_duplicados.setChecked(True)
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window, "DepurarPaginasDialog", ExcluirDialogo)
    monkeypatch.setattr(ExclusionWorker, "start", ejecutar_directo)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    with patch("app.reports.outputs.write_outputs", side_effect=OSError("archivo ocupado")):
        principal._depurar_paginas()
    assert len(principal._reports[0].pages) == 3
    assert "Error al excluir" in principal.status_label.text()
    principal.close()


def test_csv_puede_recuperar_json_incompleto_sin_la_plantilla(app, tmp_path, monkeypatch):
    from app.reports import exclusiones

    carpeta, _reportes, _opciones = ejecucion(tmp_path)

    def interrumpir():
        (carpeta / "datos" / f"{carpeta.name}.json").write_bytes(b'{"reportes":')
        raise SystemExit()

    import pytest
    with pytest.raises(SystemExit):
        exclusiones.guardar_con_respaldo(carpeta, interrumpir)
    monkeypatch.setattr(ExclusionWorker, "start", ejecutar_directo)
    monkeypatch.setattr(csv_viewer, "DepurarPaginasDialog", RestaurarDialogo)
    monkeypatch.setattr(csv_viewer, "template_for_csv", lambda _path: None)
    visor = CsvViewerWindow(tmp_path)
    assert visor.load_csv_file(carpeta / "datos" / f"{carpeta.name}.CSV")
    assert visor.btn_depurar.isEnabled()
    visor._depurar_paginas()
    assert len(visor._rows) == 3
    assert "restaurada" in visor._export_note
    assert exclusiones.motivo_restauracion(carpeta)
    visor.close()
