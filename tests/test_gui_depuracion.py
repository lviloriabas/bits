"""El cuadro «Depurar páginas» y su botón en las dos vistas de CSV y PDF.

Comprueba lo que la ventana promete: que el conteo que se ve antes de borrar
sea el de la ejecución abierta, que no se pueda eliminar sin marcar nada y que
el botón espere a tener una ejecución guardada y ninguna escritura en curso.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from app.gui.csv_viewer import CsvViewerWindow
from app.gui.depuracion_dialog import DepurarPaginasDialog
from app.gui import main_window
from app.gui.main_window import MainWindow
from app.models.schemas import FieldResult, PageResult, ValidationReport
from app.validation.depuracion import depurar_claves

RAIZ = Path(__file__).resolve().parents[1]


def test_previews_cargan_las_paginas_y_no_cambian_la_seleccion(app, tmp_path):
    import pymupdf
    ruta = tmp_path / "original.pdf"
    with pymupdf.open() as pdf:
        for texto in ("Copia uno", "Copia dos", ""):
            page = pdf.new_page()
            page.insert_text((40, 40), texto)
        pdf.save(ruta)
    report = ValidationReport(pdf_path=str(ruta), template_name="fixture", pages=[
        pagina(1, "2147300"), pagina(2, "2147300"), pagina(3, blank=True),
    ])
    dialogo = DepurarPaginasDialog([report])
    try:
        item = dialogo.arbol_duplicados.topLevelItem(0).child(0)
        dialogo.arbol_duplicados.setCurrentItem(item)
        assert not dialogo.previa_elegida.pixmap().isNull()
        assert not dialogo.previa_comparada.pixmap().isNull()
        assert not dialogo.boton_eliminar.isEnabled()
        blanco = dialogo.arbol_blancas.topLevelItem(0)
        dialogo.arbol_blancas.setCurrentItem(blanco)
        assert not dialogo.previa_elegida.pixmap().isNull()
        assert dialogo.previa_comparada.text() == "Sin página para comparar"
    finally:
        dialogo.close()


def pagina(numero: int, log: str | None = None, blank: bool = False) -> PageResult:
    campos = []
    if log is not None:
        campos.append(
            FieldResult(
                page_number=numero,
                field_id="log_number",
                field_type="ocr",
                value=log,
            )
        )
    return PageResult(page_number=numero, blank=blank, fields=campos)


def corrida() -> list[ValidationReport]:
    """Un log_number repetido entre los dos PDF y una página en blanco."""
    return [
        ValidationReport(
            pdf_path="primero.pdf",
            template_name="fixture",
            pages=[pagina(1, "2147300"), pagina(2, blank=True)],
        ),
        ValidationReport(
            pdf_path="segundo.pdf",
            template_name="fixture",
            pages=[pagina(7, "2147300")],
        ),
    ]


def test_el_cuadro_enseña_cuantas_paginas_quita_cada_criterio(app):
    dialog = DepurarPaginasDialog(corrida())
    try:
        assert "1 página" in dialog.check_duplicados.text()
        assert "1 página" in dialog.check_blancas.text()
    finally:
        dialog.deleteLater()


def test_no_se_elimina_sin_marcar_ningun_criterio(app):
    dialog = DepurarPaginasDialog(corrida())
    try:
        assert not dialog.boton_eliminar.isEnabled()
        assert "Marque al menos un criterio" in dialog.total_label.text()

        dialog.check_duplicados.setChecked(True)

        assert dialog.boton_eliminar.isEnabled()
        assert dialog.resumen().total == 1
        assert "1 página" in dialog.total_label.text()
    finally:
        dialog.deleteLater()


def test_el_criterio_sin_paginas_queda_apagado(app):
    reports = [
        ValidationReport(
            pdf_path="unico.pdf",
            template_name="fixture",
            pages=[pagina(1, "2147300"), pagina(2, "2147301")],
        )
    ]
    dialog = DepurarPaginasDialog(reports)
    try:
        assert not dialog.check_duplicados.isEnabled()
        assert not dialog.check_blancas.isEnabled()
        assert not dialog.boton_eliminar.isEnabled()
        assert "no tiene páginas repetidas ni en blanco" in dialog.total_label.text()
    finally:
        dialog.deleteLater()


def test_marcar_los_dos_criterios_no_cuenta_dos_veces_la_misma_pagina(app):
    reports = [
        ValidationReport(
            pdf_path="unico.pdf",
            template_name="fixture",
            pages=[pagina(1, "2147300"), pagina(2, "2147300", blank=True)],
        )
    ]
    dialog = DepurarPaginasDialog(reports)
    try:
        dialog.check_duplicados.setChecked(True)
        dialog.check_blancas.setChecked(True)

        assert dialog.resumen().total == 1
    finally:
        dialog.deleteLater()


def test_el_boton_cancelar_esta_en_español(app):
    dialog = DepurarPaginasDialog(corrida())
    try:
        cancelar = dialog.buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        )
        assert cancelar.text() == "Cancelar"
        assert dialog.boton_eliminar.text() == "Eliminar"
    finally:
        dialog.deleteLater()


def test_la_ventana_principal_no_depura_sin_corrida_guardada(window):
    assert not window.btn_depurar.isEnabled()

    window._reports = corrida()
    window._sync_depurar_button()
    # Todavía sin carpeta: la escritura reutiliza la de la ejecución y sin
    # ella dejaría una segunda entrega de lo mismo.
    assert not window.btn_depurar.isEnabled()

    window._corrida_dir = RAIZ / "output" / "BITS 19 AUG 2026 05 00"
    window._sync_depurar_button()

    assert window.btn_depurar.isEnabled()

    window._last_run_cancelled = True
    window._sync_depurar_button()

    assert not window.btn_depurar.isEnabled()


class DialogoMarcado(DepurarPaginasDialog):
    """El cuadro real, con los dos criterios marcados y aceptado sin ratón."""

    def exec(self) -> int:  # noqa: A003 - API Qt
        self.check_duplicados.setChecked(True)
        self.check_blancas.setChecked(True)
        return QDialog.DialogCode.Accepted


def test_al_depurar_la_ventana_rehace_la_tabla_y_reescribe_la_corrida(app):
    """El recorrido entero: marcar, borrar, y lo que la pantalla enseña después."""
    from app.templates.schema import FieldTemplate, Template

    template = Template(
        name="fixture",
        fields=[FieldTemplate(id="log_number", x=0.1, y=0.1, w=0.2, h=0.1)],
    )
    window = MainWindow()
    escrituras = []
    try:
        window._processed_template = template
        window._reports = corrida()
        window._corrida_dir = RAIZ / "output" / "BITS 19 AUG 2026 05 00"
        window._sync_depurar_button()
        window._start_outputs = lambda reports, context, skip_pdfs=False: (
            escrituras.append((len(reports), context, skip_pdfs))
        )
        with patch.object(main_window, "DepurarPaginasDialog", DialogoMarcado):
            window._depurar_paginas()

        # De las tres páginas se van la repetida y la vacía; queda una sola,
        # y el PDF que se quedó sin ninguna sale de la ejecución.
        assert [r.pdf_path for r in window._reports] == ["primero.pdf"]
        assert [p.page_number for p in window._reports[0].pages] == [1]

        window._table_timer.stop()
        while window._table_pending:
            window._on_table_chunk()
        assert window.table.rowCount() == 1
        assert window.duplicates_label.text() == "Duplicados: 0"

        # La reescritura va sobre la ejecución y sin rehacer los PDF.
        assert escrituras == [(1, "depurar", True)]
    finally:
        window.close()
        app.processEvents()


def test_el_visor_de_csv_no_depura_un_csv_suelto(app, tmp_path):
    """Sin el JSON al lado no hay páginas que quitar, y el botón lo dice."""
    datos = tmp_path / "corrida" / "datos"
    datos.mkdir(parents=True)
    (datos / "ejecución.csv").write_text(
        "file,page,log_number\na.pdf,1,2147300\n", encoding="utf-8"
    )
    window = CsvViewerWindow(tmp_path)
    try:
        assert window.load_csv_file(datos / "ejecución.csv") is True
        assert not window.btn_depurar.isEnabled()
        assert "no viene acompañado del JSON" in window.btn_depurar.toolTip()
    finally:
        window.close()
        app.processEvents()


def test_el_cuadro_lista_las_apariciones_de_cada_bitacora_repetida(app):
    """El grupo entero, no solo la que sobra: hay que ver cuál se conserva."""
    dialog = DepurarPaginasDialog(corrida())
    try:
        assert dialog.arbol_duplicados.topLevelItemCount() == 1
        grupo = dialog.arbol_duplicados.topLevelItem(0)
        assert "2147300" in grupo.text(0)
        assert grupo.childCount() == 2
        assert "primero.pdf, página 1 (primera)" == grupo.child(0).text(0)
        assert "segundo.pdf, página 7" == grupo.child(1).text(0)
    finally:
        dialog.deleteLater()


def test_al_marcar_duplicados_se_elige_la_aparicion_sobrante(app):
    dialog = DepurarPaginasDialog(corrida())
    try:
        dialog.check_duplicados.setChecked(True)
        grupo = dialog.arbol_duplicados.topLevelItem(0)

        assert grupo.child(0).checkState(0) == Qt.CheckState.Unchecked
        assert grupo.child(1).checkState(0) == Qt.CheckState.Checked
        # (reporte, página): el segundo PDF es el índice 1.
        assert dialog.claves() == {(1, 7)}
    finally:
        dialog.deleteLater()


def test_se_puede_conservar_la_segunda_aparicion_en_vez_de_la_primera(app):
    """Lo que pidió el usuario: elegir cuál de las dos se va."""
    dialog = DepurarPaginasDialog(corrida())
    try:
        dialog.check_duplicados.setChecked(True)
        grupo = dialog.arbol_duplicados.topLevelItem(0)
        grupo.child(1).setCheckState(0, Qt.CheckState.Unchecked)
        grupo.child(0).setCheckState(0, Qt.CheckState.Checked)

        assert dialog.claves() == {(0, 1)}
        assert dialog.resumen().total == 1
    finally:
        dialog.deleteLater()


def test_la_pagina_en_blanco_se_puede_desmarcar_una_a_una(app):
    dialog = DepurarPaginasDialog(corrida())
    try:
        dialog.check_blancas.setChecked(True)
        assert dialog.claves() == {(0, 2)}

        dialog.arbol_blancas.topLevelItem(0).setCheckState(
            0, Qt.CheckState.Unchecked
        )

        assert dialog.claves() == set()
        assert not dialog.boton_eliminar.isEnabled()
    finally:
        dialog.deleteLater()


def test_depurar_borra_solo_la_aparicion_elegida():
    """La elección del cuadro llega intacta a los reportes."""
    reports = corrida()
    quedan, quitadas = depurar_claves(reports, {(0, 1)})

    assert quitadas == 1
    assert [p.page_number for p in quedan[0].pages] == [2]
    assert [p.page_number for p in quedan[1].pages] == [7]


def test_no_se_pueden_marcar_todas_las_apariciones_de_una_bitacora(app):
    """Marcar la última libre no borra el grupo: la marca vuelve atrás.

    Con el criterio encendido ya está marcada la segunda aparición; marcar
    también la primera dejaría la ejecución sin esa bitácora, así que el
    cuadro la devuelve a su sitio y lo dice en el pie.
    """
    dialog = DepurarPaginasDialog(corrida())
    try:
        dialog.check_duplicados.setChecked(True)
        grupo = dialog.arbol_duplicados.topLevelItem(0)

        grupo.child(0).setCheckState(0, Qt.CheckState.Checked)

        assert grupo.child(0).checkState(0) == Qt.CheckState.Unchecked
        assert grupo.child(1).checkState(0) == Qt.CheckState.Checked
        assert dialog.claves() == {(1, 7)}
        assert "tiene que quedar una página" in dialog.total_label.text()
    finally:
        dialog.deleteLater()


def test_marcar_todas_las_blancas_sigue_permitido(app):
    """El tope es de las bitácoras repetidas: las vacías se van todas."""
    dialog = DepurarPaginasDialog(corrida())
    try:
        dialog.check_blancas.setChecked(True)

        assert dialog.claves() == {(0, 2)}
        assert "tiene que quedar una página" not in dialog.total_label.text()
    finally:
        dialog.deleteLater()


def test_el_borrado_conserva_una_aparicion_aunque_lleguen_las_dos_marcadas(app):
    """Si la elección llegara con el grupo entero, se va solo la más nueva."""
    reports = corrida()

    quedan, quitadas = depurar_claves(reports, {(0, 1), (1, 7)})

    assert quitadas == 1
    assert [p.page_number for p in quedan[0].pages] == [1, 2]
    assert [r.pdf_path for r in quedan] == ["primero.pdf"]


def bitacora(numero: int, log: str, matricula: str, vuelo: str,
             fecha: str) -> PageResult:
    """Una página con los campos por los que se distingue una bitácora."""
    campos = [
        FieldResult(page_number=numero, field_id=campo,
                    field_type="ocr", value=valor)
        for campo, valor in (
            ("log_number", log),
            ("matricula", matricula),
            ("flight_number", vuelo),
        )
    ]
    return PageResult(page_number=numero, date=fecha, fields=campos)


def test_cada_aparicion_repetida_ensena_lo_que_trae_escrito(app):
    """Sin esto las dos se distinguen solo por el archivo y la página.

    Quitar la repetida dejó de hacerse solo justamente porque cuál sobra
    no se sabe sin mirarlas, así que el cuadro tiene que enseñar la
    matrícula, la fecha y el vuelo de cada una.
    """
    reports = [
        ValidationReport(
            pdf_path="primero.pdf",
            template_name="fixture",
            pages=[
                bitacora(1, "2147300", "HP-1717CMP", "CM103", "2026-08-30"),
            ],
        ),
        ValidationReport(
            pdf_path="segundo.pdf",
            template_name="fixture",
            pages=[
                bitacora(7, "2147300", "HP-1712CMP", "CM240", "2026-08-31"),
            ],
        ),
    ]
    dialog = DepurarPaginasDialog(reports)
    try:
        grupo = dialog.arbol_duplicados.topLevelItem(0)

        assert grupo.child(0).text(0) == (
            "primero.pdf, página 1 (primera) - HP-1717CMP, 2026-08-30, CM103"
        )
        assert grupo.child(1).text(0) == (
            "segundo.pdf, página 7 - HP-1712CMP, 2026-08-31, CM240"
        )
    finally:
        dialog.deleteLater()


def test_una_pagina_sin_nada_legible_conserva_su_nombre(app):
    """Un guion seguido de nada se lee como un dato, y no lo es."""
    dialog = DepurarPaginasDialog(corrida())
    try:
        grupo = dialog.arbol_duplicados.topLevelItem(0)

        assert grupo.child(1).text(0) == "segundo.pdf, página 7"
    finally:
        dialog.deleteLater()
