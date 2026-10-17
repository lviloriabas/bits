"""Cuadro que pregunta cómo se exportan varias bitácoras a la vez."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from app.gui.memoria import VISOR, recordar
from app.gui.responsive import fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_S
from app.gui.widgets import window_stylesheet

TITULO = "Exportar bitácoras"
ANCHO = 420


class ExportarBitacorasDialog(QDialog):
    """Un PDF por bitácora o uno solo con todas, y después la carpeta.

    El cuadro solo elige la forma. La carpeta la pide quien lo abre al
    aceptarlo, con el selector de Windows, y la elección queda guardada
    para la vez siguiente: quien saca bitácoras sueltas casi siempre las
    quiere igual.
    """

    def __init__(self, descripcion: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITULO)
        self._density = fit_to_screen(self, ANCHO, 160)
        self._aplicar_hoja()
        gestor_tema().cambiado.connect(self._al_cambiar_tema)
        self._build_ui(descripcion)
        self.ensurePolished()
        self.layout().activate()
        fit_to_screen(
            self, self.width(), self.layout().totalHeightForWidth(self.width())
        )

    def _aplicar_hoja(self) -> None:
        self.setStyleSheet(window_stylesheet(self._density.qss))

    def _al_cambiar_tema(self, _nombre: str) -> None:
        self._aplicar_hoja()

    def _build_ui(self, descripcion: str) -> None:
        layout = QVBoxLayout(self)
        margin = max(SPACE_S, self._density.window_margin)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(SPACE_S)

        self.descripcion = QLabel(descripcion)
        self.descripcion.setWordWrap(True)
        layout.addWidget(self.descripcion)

        self.por_bitacora = QRadioButton("Un PDF por bitácora")
        self.por_bitacora.setToolTip(
            "Cada bitácora en su propio archivo, con su número como nombre"
        )
        self.por_bitacora.setChecked(True)
        layout.addWidget(self.por_bitacora)
        self.un_solo_pdf = QRadioButton("Un solo PDF")
        self.un_solo_pdf.setToolTip(
            "Todas en un archivo, ordenadas por número de bitácora"
        )
        layout.addWidget(self.un_solo_pdf)
        recordar(VISOR, "exportar_un_solo_pdf", self.un_solo_pdf)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Elegir carpeta…"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "Cancelar"
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def en_un_solo_pdf(self) -> bool:
        """¿Se eligió juntar todas las bitácoras en un archivo?"""
        return self.un_solo_pdf.isChecked()
