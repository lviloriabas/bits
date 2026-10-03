"""La revisión conserva al menos una copia y no borra al cambiar marcas."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStyleOptionViewItem

from app.airvault.correcciones import Copia
from app.gui.revision_copias_dialog import RevisionCopiasDialog
from app.gui.theme import aplicar_tema, install_application_theme
from app.gui.tokens import TEMA_CLARO, TEMA_OSCURO, paleta, tema


def _vistas(cantidad):
    pixmap = QPixmap(800, 1100)
    pixmap.fill(Qt.GlobalColor.white)
    datos = QByteArray()
    buffer = QBuffer(datos)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return [
        (Copia(str(i), str(i), str(i), datetime(2026, 1, i + 1), "HP-9913CMP", 1, "LOG PAGE"), bytes(datos))
        for i in range(cantidad)
    ]


def test_se_pueden_cambiar_las_copias_antes_de_aplicar(app):
    pixmap = QPixmap(100, 70)
    pixmap.fill(Qt.GlobalColor.white)
    datos = QByteArray()
    buffer = QBuffer(datos)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    vistas = [(Copia(str(i), str(i), str(i), datetime(2026, 1, i + 1), "HP-9913CMP", 1, "LOG PAGE"), bytes(datos)) for i in range(3)]
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"), vistas, {"1"})
    try:
        assert dialogo.seleccionadas() == {"1"}
        assert dialogo.aplicar.isEnabled()
        dialogo.lista.item(2).setCheckState(Qt.CheckState.Checked)
        assert dialogo.seleccionadas() == {"1", "2"}
        dialogo.lista.setCurrentRow(1)
        dialogo._alternar()
        assert dialogo.seleccionadas() == {"2"}
        assert dialogo.aplicar.isEnabled()
        assert dialogo.result() == 0
    finally:
        dialogo.close()


def test_imagen_ilegible_bloquea_el_borrado(app):
    copia = Copia("a", "a", "1", datetime(2026, 1, 1), "HP-9913CMP", 1, "LOG PAGE")
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"), [(copia, b"error")], set())
    try:
        assert not dialogo.aplicar.isEnabled()
    finally:
        dialogo.close()


@pytest.mark.parametrize("cantidad", [2, 4, 12])
def test_todas_las_copias_se_pueden_ver_en_grande_y_conservan_las_marcas(app, cantidad):
    elegidas = {str(i) for i in range(1, cantidad)}
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"), _vistas(cantidad), elegidas)
    dialogo.resize(1100, 760)
    dialogo.show()
    app.processEvents()
    assert dialogo.lista.count() == cantidad
    assert dialogo.imagen.pixmap().width() > 700
    assert dialogo.imagen.pixmap().width() > dialogo.lista.iconSize().width() * 4
    assert not dialogo.anterior.isEnabled()
    for i in range(1, cantidad):
        dialogo.siguiente.click()
        app.processEvents()
        assert dialogo.lista.currentRow() == i
        assert f"Copia {i + 1} de {cantidad}" in dialogo.datos_copia.text()
        assert dialogo.eliminar_copia.isChecked()
        assert dialogo.seleccionadas() == elegidas
    assert not dialogo.siguiente.isEnabled()
    dialogo.eliminar_copia.setChecked(False)
    assert dialogo.seleccionadas() == elegidas - {str(cantidad - 1)}
    dialogo.lista.setCurrentRow(0)
    assert not dialogo.eliminar_copia.isChecked()
    dialogo._ampliar(1.25)
    assert dialogo.imagen.pixmap().width() > dialogo.visor.viewport().width()


def test_continuar_con_predeterminadas_descarta_las_marcas_manuales(app):
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"), _vistas(3), {"1", "2"})
    respuestas = []
    dialogo.respuesta.connect(respuestas.append)
    dialogo.show()
    assert dialogo.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
    dialogo.lista.item(0).setCheckState(Qt.CheckState.Checked)
    dialogo.lista.item(2).setCheckState(Qt.CheckState.Unchecked)
    assert dialogo.seleccionadas() == {"1"}
    dialogo.predeterminadas.click()
    assert dialogo.continuar_con_predeterminadas
    assert dialogo.seleccionadas() == {"1", "2"}
    assert respuestas == [{"1", "2"}]
    assert dialogo.isVisible()
    assert not dialogo.aplicar.isEnabled()


def test_la_lista_de_copias_es_vertical_y_esta_al_lado_del_visor(app):
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"), _vistas(12), {"1"})
    dialogo.show()
    app.processEvents()
    assert dialogo.lista.flow() == dialogo.lista.Flow.TopToBottom
    assert dialogo.lista.geometry().right() < dialogo.visor.geometry().left()
    assert dialogo.lista.verticalScrollBar().maximum() > 0
    assert dialogo.lista.horizontalScrollBar().maximum() == 0


def test_protege_la_original_y_no_repite_la_respuesta(app):
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"), _vistas(2), {"1"})
    respuestas = []
    dialogo.respuesta.connect(respuestas.append)
    dialogo.lista.item(0).setCheckState(Qt.CheckState.Checked)
    assert dialogo.lista.item(0).checkState() == Qt.CheckState.Unchecked
    assert dialogo.seleccionadas() == {"1"}
    dialogo.accept()
    dialogo.accept()
    assert respuestas == [{"1"}]


def test_la_original_no_se_marca_con_casilla_ni_teclado_y_no_depende_del_orden(app):
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"),
                                  list(reversed(_vistas(3))), {"0", "1", "2"})
    dialogo.show()
    app.processEvents()
    dialogo.lista.setCurrentRow(2)
    assert dialogo.lista.currentItem().text() == "Original"
    assert not dialogo.eliminar_copia.isEnabled()
    assert not dialogo.lista.currentItem().flags() & Qt.ItemFlag.ItemIsUserCheckable
    dialogo.eliminar_copia.setChecked(True)
    dialogo.lista.setFocus()
    QTest.keyClick(dialogo.lista, Qt.Key.Key_Space)
    QTest.keyClick(dialogo.lista, Qt.Key.Key_Return)
    assert not dialogo.eliminar_copia.isChecked()
    assert dialogo.lista.currentItem().checkState() == Qt.CheckState.Unchecked
    assert dialogo.seleccionadas() == {"1", "2"}


def test_la_casilla_se_pulsa_junto_al_nombre_sin_marcar_al_tocar_la_imagen(app):
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"), _vistas(3), {"1"})
    dialogo.show()
    app.processEvents()
    indice = dialogo.lista.model().index(1, 0)
    opt = QStyleOptionViewItem()
    opt.initFrom(dialogo.lista)
    opt.widget = dialogo.lista
    opt.rect = dialogo.lista.visualRect(indice)
    delegado = dialogo.lista.itemDelegate()
    delegado.initStyleOption(opt, indice)
    imagen, casilla, texto = delegado._rectangulos(opt)
    assert imagen.bottom() < casilla.top()
    assert abs(casilla.center().y() - texto.center().y()) <= 1
    assert casilla.right() < texto.left()
    assert opt.rect.contains(casilla)
    QTest.mouseClick(dialogo.lista.viewport(), Qt.MouseButton.LeftButton, pos=imagen.center())
    assert dialogo.seleccionadas() == {"1"}
    QTest.mouseClick(dialogo.lista.viewport(), Qt.MouseButton.LeftButton, pos=casilla.center())
    assert dialogo.seleccionadas() == set()
    QTest.keyClick(dialogo.lista, Qt.Key.Key_Space)
    assert dialogo.seleccionadas() == {"1"}


def test_una_sola_copia_no_se_puede_eliminar(app):
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"), _vistas(1), {"0"})
    assert dialogo.seleccionadas() == set()
    assert not dialogo.aplicar.isEnabled()
    assert not dialogo.predeterminadas.isEnabled()
    assert not dialogo.eliminar_copia.isEnabled()


def test_el_visor_abierto_acompana_los_dos_temas(app, monkeypatch):
    previo = tema()
    monkeypatch.setattr("app.gui.theme.guardar_tema", lambda _tema: True)
    install_application_theme(app)
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"), _vistas(2), {"1"})
    try:
        for nombre in (TEMA_CLARO, TEMA_OSCURO):
            aplicar_tema(nombre)
            assert paleta().TABLE_BASE_BG in dialogo.styleSheet()
            assert paleta().PANE_BORDER in dialogo.styleSheet()
            assert "border-radius: 6px" in dialogo.styleSheet()
            assert dialogo.aplicar.sizeHint().height() == dialogo.siguiente.sizeHint().height()
    finally:
        aplicar_tema(previo)
