"""Editor pequeño y autocontenido para la lista portable de aviones."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.gui.responsive import fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_S
from app.gui.widgets import window_stylesheet
from app.utils.fleet import FLEET_FILENAME, load_fleet, normalise_matricula


class FleetStore:
    """Lee y escribe ``fleet.json`` sin depender de la plantilla OCR."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[str]:
        return load_fleet(self.path)

    def save(self, matriculas: list[str]) -> None:
        values = sorted({normalise_matricula(value) for value in matriculas if value})
        self.path.write_text(
            json.dumps(
                {"version": 1, "matriculas": values},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

class FleetEditorDialog(QDialog):
    """Permite editar la lista sin abrir un editor de texto."""

    def __init__(self, store: FleetStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Lista de flota")
        self._density = fit_to_screen(self, 430, 440)
        self._aplicar_hoja()
        # La hoja lleva el fragmento de la densidad, así que la rehace el
        # propio cuadro cuando cambia el tema.
        gestor_tema().cambiado.connect(self._al_cambiar_tema)
        self._build_ui()
        self._load_values()

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
            "Mantenga aquí todas las matrículas de la flota. Si falta una, "
            "la lectura puede asignarse al avión equivocado."
        )
        intro.setWordWrap(True)
        intro.setObjectName("fleetReminder")
        layout.addWidget(intro)

        add_row = QHBoxLayout()
        add_row.setSpacing(SPACE_S)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Matrícula, por ejemplo HP-1234CMP")
        self.entry.textChanged.connect(self._filter_values)
        self.entry.returnPressed.connect(self._add_value)
        add_row.addWidget(self.entry, 1)
        add = QPushButton("Agregar")
        add.clicked.connect(self._add_value)
        add_row.addWidget(add)
        layout.addLayout(add_row)

        self.values = QListWidget()
        self.values.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.values, 1)

        self.remove_button = QPushButton("Quitar seleccionadas")
        self.remove_button.clicked.connect(self._remove_selected)

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
        self.buttons.accepted.connect(self._save_and_accept)
        self.buttons.rejected.connect(self.reject)

        action_row = QHBoxLayout()
        action_row.setSpacing(SPACE_S)
        action_row.addWidget(self.remove_button)
        action_row.addStretch()
        action_row.addWidget(self.buttons)
        layout.addLayout(action_row)

    def _load_values(self) -> None:
        self.values.clear()
        self.values.addItems(self.store.load())

    def _filter_values(self, text: str) -> None:
        query = self._search_key(text)
        self.values.clearSelection()
        for index in range(self.values.count()):
            item = self.values.item(index)
            item.setHidden(query not in self._search_key(item.text()))

    @staticmethod
    def _search_key(value: str) -> str:
        return "".join(
            character
            for character in value.upper()
            if not character.isspace() and character != "-"
        )

    def _add_value(self) -> None:
        value = normalise_matricula(self.entry.text())
        if not value:
            QMessageBox.warning(
                self,
                "Matrícula inválida",
                "Use el formato HP-1234CMP o HP-1234WWP.",
            )
            return
        if not self.values.findItems(value, Qt.MatchFlag.MatchExactly):
            self.values.addItem(value)
            self.values.sortItems()
        self.entry.clear()

    def _remove_selected(self) -> None:
        for item in self.values.selectedItems():
            self.values.takeItem(self.values.row(item))

    def _save_and_accept(self) -> None:
        values = [self.values.item(i).text() for i in range(self.values.count())]
        try:
            self.store.save(values)
        except OSError as exc:
            QMessageBox.critical(self, "No se pudo guardar", str(exc))
            return
        self.accept()
