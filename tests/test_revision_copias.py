"""La revisión conserva al menos una copia y no borra al cambiar marcas."""

from datetime import datetime
from types import SimpleNamespace

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QPixmap

from app.airvault.correcciones import Copia
from app.gui.revision_copias_dialog import RevisionCopiasDialog


def test_se_pueden_cambiar_las_copias_antes_de_aplicar(app):
    pixmap = QPixmap(100, 70)
    pixmap.fill(Qt.GlobalColor.white)
    datos = QByteArray()
    buffer = QBuffer(datos)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    vistas = [(Copia(str(i), str(i), str(i), datetime(2026, 1, i + 1), "HP-9913CMP", 1, "LOG PAGE"), bytes(datos)) for i in range(2)]
    dialogo = RevisionCopiasDialog(SimpleNamespace(log_number="2008159"), vistas, {"1"})
    try:
        assert dialogo.seleccionadas() == {"1"}
        assert dialogo.aplicar.isEnabled()
        dialogo.lista.item(0).setCheckState(Qt.CheckState.Checked)
        assert not dialogo.aplicar.isEnabled()
        dialogo.lista.setCurrentRow(1)
        dialogo._alternar()
        assert dialogo.seleccionadas() == {"0"}
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
