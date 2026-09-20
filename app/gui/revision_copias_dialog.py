"""Comparación de imágenes antes de eliminar copias de Web Reports."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout

from app.gui.responsive import fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_S, on_accent_text, paleta
from app.gui.widgets import TABLE_RADIUS, window_stylesheet


class RevisionCopiasDialog(QDialog):
    def __init__(self, correccion, vistas, elegidas, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Revisar copias de la bitácora {correccion.log_number}")
        self.resize(920, 650)
        layout = QVBoxLayout(self)
        ayuda = QLabel("Marque las copias que desea eliminar. Puede cambiar la selección antes de aplicar. Conserve al menos una.")
        ayuda.setWordWrap(True)
        layout.addWidget(ayuda)
        self.lista = QListWidget()
        self.lista.setViewMode(QListWidget.ViewMode.IconMode)
        self.lista.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.lista.setMovement(QListWidget.Movement.Static)
        self.lista.setIconSize(QSize(250, 360))
        self.lista.setSpacing(SPACE_S)
        self._validas = True
        for copia, imagen in vistas:
            pixmap = QPixmap()
            valida = pixmap.loadFromData(imagen, "PNG")
            self._validas = self._validas and valida
            fecha = copia.cuando.strftime("%d/%m/%Y %H:%M") if copia.cuando else "Sin fecha"
            item = QListWidgetItem(QIcon(pixmap), f"{copia.matricula}\n{fecha}\nUna imagen" if valida else "Imagen no disponible")
            item.setData(Qt.ItemDataRole.UserRole, copia.clave)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if copia.clave in elegidas else Qt.CheckState.Unchecked)
            self.lista.addItem(item)
        layout.addWidget(self.lista)
        botones = QHBoxLayout()
        botones.addStretch()
        omitir = QPushButton("Omitir esta bitácora")
        omitir.clicked.connect(self.reject)
        self.aplicar = QPushButton("Eliminar seleccionadas")
        self.aplicar.setObjectName("primaryButton")
        self.aplicar.clicked.connect(self.accept)
        for boton in (omitir, self.aplicar):
            boton.setAutoDefault(False)
            botones.addWidget(boton)
        layout.addLayout(botones)
        self.lista.itemChanged.connect(self._actualizar)
        self._atajos = []
        for tecla in ("Return", "Enter"):
            atajo = QShortcut(QKeySequence(tecla), self.lista)
            atajo.setContext(Qt.ShortcutContext.WidgetShortcut)
            atajo.activated.connect(self._alternar)
            self._atajos.append(atajo)
        gestor_tema().cambiado.connect(self._tema)
        self._tema()
        self._actualizar()
        fit_to_screen(self, 920, 650)

    def _tema(self, *_):
        colores = paleta()
        self.setStyleSheet(window_stylesheet(
            "QListWidget {"
            f"background-color: {colores.TABLE_BASE_BG}; color: {colores.TABLE_TEXT};"
            f"border: 1px solid {colores.PANE_BORDER}; border-radius: {TABLE_RADIUS}px;"
            f"selection-background-color: palette(highlight); selection-color: {on_accent_text()};"
            "} QListWidget::item { padding: 6px; }"
        ))

    def _alternar(self):
        item = self.lista.currentItem()
        if item:
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked)

    def seleccionadas(self):
        return {self.lista.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.lista.count())
                if self.lista.item(i).checkState() == Qt.CheckState.Checked}

    def _actualizar(self, *_):
        self.aplicar.setEnabled(self._validas and 0 < len(self.seleccionadas()) < self.lista.count())
