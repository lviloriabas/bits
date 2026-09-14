"""Selector reutilizable de columnas importantes para las dos vistas CSV."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.gui.responsive import fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_S
from app.gui.widgets import window_stylesheet


class ImportantFieldsDialog(QDialog):
    """Selector de columnas; solo cambia la presentación de la tabla."""

    selectionChanged = Signal(set)

    def __init__(
        self,
        columns: Iterable[str],
        selected: Iterable[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Seleccionar columnas importantes")
        # En una pantalla baja el alto pedido no cabe y los botones de
        # aceptar y cancelar quedan por debajo del borde.
        self._density = fit_to_screen(self, 420, 520)
        self._aplicar_hoja()
        # La hoja lleva el fragmento de la densidad, así que la rehace el
        # propio cuadro cuando cambia el tema.
        gestor_tema().cambiado.connect(self._al_cambiar_tema)
        self.columns = list(columns)
        self.checks: dict[str, QCheckBox] = {}
        # Marcar en bloque no debe emitir una selección por casilla: cada
        # emisión guarda el archivo y repinta el visor.
        self._bulk_update = False
        self._build_ui(set(selected))

    def _aplicar_hoja(self) -> None:
        """La hoja del cuadro, con los tonos y las medidas de ahora."""
        self.setStyleSheet(window_stylesheet(self._density.qss))

    def _al_cambiar_tema(self, _nombre: str) -> None:
        """Rehace la hoja del cuadro con los tonos del tema nuevo."""
        self._aplicar_hoja()

    def _build_ui(self, selected: set[str]) -> None:
        layout = QVBoxLayout(self)
        margin = max(SPACE_S, self._density.window_margin)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(SPACE_S)
        intro = QLabel(
            "Elija las columnas de la vista resumida y de los recuadros de la "
            "vista previa. La selección se guarda por plantilla y no "
            "modifica el CSV."
        )
        # Sin ajuste de línea el párrafo entero era el ancho mínimo del
        # diálogo: pedía 420 px y se abría con más de 1200.
        intro.setWordWrap(True)
        layout.addWidget(intro)
        bulk_row = QHBoxLayout()
        bulk_row.setSpacing(SPACE_S)
        select_all = QPushButton("Marcar todas")
        select_all.setToolTip("Marcar todas las columnas de la lista")
        select_all.clicked.connect(lambda: self._set_all(True))
        bulk_row.addWidget(select_all)
        clear_all = QPushButton("Desmarcar todas")
        clear_all.setToolTip("Desmarcar todas las columnas de la lista")
        clear_all.clicked.connect(lambda: self._set_all(False))
        bulk_row.addWidget(clear_all)
        bulk_row.addStretch()
        layout.addLayout(bulk_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: 0; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(SPACE_S, SPACE_S, SPACE_S, SPACE_S)
        body_layout.setSpacing(SPACE_S)
        for column in self.columns:
            check = QCheckBox(column)
            check.setChecked(column in selected)
            check.toggled.connect(self._emit_selection)
            self.checks[column] = check
            body_layout.addWidget(check)
        body_layout.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Close
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).setText(
            "Aplicar"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Close).setText(
            "Cerrar"
        )
        self.buttons.button(
            QDialogButtonBox.StandardButton.Apply
        ).clicked.connect(
            self._emit_selection
        )
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _set_all(self, checked: bool) -> None:
        self._bulk_update = True
        try:
            for check in self.checks.values():
                check.setChecked(checked)
        finally:
            self._bulk_update = False
        self._emit_selection()

    def _emit_selection(self, *_args) -> None:
        if self._bulk_update:
            return
        selected = {
            column for column, check in self.checks.items() if check.isChecked()
        }
        self.selectionChanged.emit(selected)
