"""Cuadro para elegir la ruta remota y activarla o desactivarla."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.gui.responsive import fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_S
from app.gui.widgets import window_stylesheet
from app.utils import ruta_remota

TITULO = "Ruta remota"
ANCHO = 460


class RutaRemotaDialog(QDialog):
    """Una sola ruta y una casilla: lo demás lo resuelve el programa.

    El cuadro no decide nada por su cuenta. Guarda la elección en las
    preferencias de esta instalación y deja preparada la carpeta; quien lo
    abre es el que vuelve a leer la entrada y sincroniza la memoria.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITULO)
        self._density = fit_to_screen(self, ANCHO, 200)
        self._aplicar_hoja()
        # La hoja lleva el fragmento de la densidad, así que la rehace el
        # propio cuadro cuando cambia el tema.
        gestor_tema().cambiado.connect(self._al_cambiar_tema)
        self._guardados = ruta_remota.leer_ajustes()
        self._build_ui()
        self._pintar_estado()
        self._ajustar_alto()

    def _aplicar_hoja(self) -> None:
        """La hoja del cuadro, con los tonos y las medidas de ahora."""
        self.setStyleSheet(window_stylesheet(self._density.qss))

    def _al_cambiar_tema(self, _nombre: str) -> None:
        """Rehace la hoja del cuadro con los tonos del tema nuevo."""
        self._aplicar_hoja()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        margin = max(SPACE_S, self._density.window_margin)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(SPACE_S)

        intro = QLabel(
            "Carpeta remota donde se guardan los archivos de bitácoras y la "
            "memoria que se usa para inferir."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.activa_check = QCheckBox("Usar la carpeta remota")
        self.activa_check.setChecked(self._guardados.activa)
        self.activa_check.toggled.connect(self._pintar_estado)
        layout.addWidget(self.activa_check)

        fila = QHBoxLayout()
        fila.setSpacing(SPACE_S)
        self.ruta_edit = QLineEdit(self._guardados.ruta)
        self.ruta_edit.setPlaceholderText(
            "Carpeta del servidor, por ejemplo \\\\servidor\\BITS"
        )
        self.ruta_edit.setAccessibleName("Ruta remota")
        self.ruta_edit.textChanged.connect(self._pintar_estado)
        fila.addWidget(self.ruta_edit, 1)
        self.examinar = QPushButton("Examinar…")
        self.examinar.setToolTip("Elegir la carpeta remota")
        self.examinar.clicked.connect(self._examinar)
        fila.addWidget(self.examinar)
        layout.addLayout(fila)

        # Con el color del resto del texto: es lo que dice qué está pasando
        # con la carpeta, no una nota al margen.
        self.estado = QLabel()
        layout.addWidget(self.estado)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            "Guardar"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "Cancelar"
        )
        self.buttons.accepted.connect(self._guardar)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _ajustar_alto(self) -> None:
        """Deja el cuadro del alto de lo que contiene, sin hueco debajo.

        El estado cabe siempre en una línea, así que el alto no cambia al
        marcar la casilla ni al escribir la ruta.
        """
        self.ensurePolished()
        fit_to_screen(
            self, self.width(), self.layout().totalHeightForWidth(self.width())
        )

    def _pintar_estado(self, *_args) -> None:
        """Dice en una línea qué va a pasar con lo que hay escrito."""
        ruta = self.ruta_edit.text().strip()
        if not self.activa_check.isChecked():
            texto = "Desactivada: se trabaja con las carpetas de este equipo."
        elif not ruta:
            texto = "Escriba o elija la carpeta remota."
        elif self._guardados.activa and ruta == self._guardados.ruta:
            texto = (
                "Conectada."
                if ruta_remota.conectada(Path(ruta))
                else "Sin conexión: se trabaja con las carpetas de este equipo."
            )
        else:
            texto = "Al guardar se comprueba la conexión."
        self.estado.setText(texto)

    def _examinar(self) -> None:
        elegida = QFileDialog.getExistingDirectory(
            self, "Elegir la carpeta remota", self.ruta_edit.text().strip()
        )
        if elegida:
            self.ruta_edit.setText(str(Path(elegida)))

    def _guardar(self) -> None:
        activa = self.activa_check.isChecked()
        ruta = self.ruta_edit.text().strip()
        if activa and not ruta:
            QMessageBox.warning(
                self, TITULO,
                "Escriba o elija la carpeta remota antes de activarla.",
            )
            return
        if activa:
            carpeta = Path(ruta)
            if ruta_remota.conectada(carpeta, refrescar=True):
                try:
                    ruta_remota.preparar(carpeta)
                except OSError:
                    QMessageBox.warning(
                        self, TITULO,
                        "No se pudo escribir en esa carpeta. Compruebe que "
                        "tiene permiso para guardar archivos allí.",
                    )
                    return
            else:
                respuesta = QMessageBox.question(
                    self, TITULO,
                    "Esa carpeta no responde ahora.\n\n¿Guardarla de todos "
                    "modos? Mientras no haya conexión se trabaja con las "
                    "carpetas de este equipo.",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if respuesta != QMessageBox.StandardButton.Yes:
                    return
        if not ruta_remota.guardar_ajustes(activa, ruta):
            QMessageBox.warning(
                self, TITULO,
                "No se pudo guardar la preferencia. Compruebe que la carpeta "
                "del programa admite escritura.",
            )
            return
        self.accept()
