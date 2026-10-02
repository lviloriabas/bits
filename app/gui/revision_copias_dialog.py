"""Comparación de imágenes antes de eliminar copias de Web Reports."""

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QSizePolicy, QVBoxLayout,
)

from app.gui.responsive import fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_M, SPACE_S, on_accent_text, paleta
from app.gui.widgets import (
    TABLE_RADIUS, ZoomableScrollArea, style_pdf_surface, window_stylesheet,
)


class RevisionCopiasDialog(QDialog):
    def __init__(self, correccion, vistas, elegidas, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Revisar copias de la bitácora {correccion.log_number}")
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self._densidad = fit_to_screen(self, 1100, 850)
        self._predeterminadas = set(elegidas)
        self.continuar_con_predeterminadas = False
        self._imagenes = []
        self._copias = []
        self._zoom = 1.0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*([self._densidad.window_margin] * 4))
        layout.setSpacing(SPACE_S)
        ayuda = QLabel(
            "Seleccione una copia para verla en grande y marque las que desea eliminar. "
            "Conserve al menos una. Use Ctrl + rueda para ampliar la imagen."
        )
        ayuda.setWordWrap(True)
        layout.addWidget(ayuda)
        self.lista = QListWidget()
        self.lista.setViewMode(QListWidget.ViewMode.IconMode)
        self.lista.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.lista.setMovement(QListWidget.Movement.Static)
        self.lista.setFlow(QListWidget.Flow.LeftToRight)
        self.lista.setWrapping(False)
        self.lista.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lista.setSpacing(SPACE_S)
        self._validas = True
        for copia, imagen in vistas:
            pixmap = QPixmap()
            valida = pixmap.loadFromData(imagen, "PNG")
            self._imagenes.append(pixmap)
            self._copias.append(copia)
            self._validas = self._validas and valida
            fecha = copia.cuando.strftime("%d/%m/%Y %H:%M") if copia.cuando else "Sin fecha"
            numero = len(self._copias)
            item = QListWidgetItem(QIcon(pixmap), f"Copia {numero}" if valida else f"Copia {numero}: sin imagen")
            item.setToolTip(f"{copia.matricula}\n{fecha}")
            item.setData(Qt.ItemDataRole.UserRole, copia.clave)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if copia.clave in elegidas else Qt.CheckState.Unchecked)
            self.lista.addItem(item)
        layout.addWidget(self.lista)
        navegacion = QHBoxLayout()
        navegacion.setSpacing(SPACE_S)
        self.anterior = QPushButton("Anterior")
        self.anterior.clicked.connect(lambda: self._mover(-1))
        self.siguiente = QPushButton("Siguiente")
        self.siguiente.clicked.connect(lambda: self._mover(1))
        self.datos_copia = QLabel()
        self.datos_copia.setWordWrap(True)
        self.eliminar_copia = QCheckBox("Eliminar esta copia")
        self.eliminar_copia.toggled.connect(self._marcar_actual)
        navegacion.addWidget(self.anterior)
        navegacion.addWidget(self.datos_copia, 1)
        navegacion.addWidget(self.eliminar_copia)
        navegacion.addWidget(self.siguiente)
        layout.addLayout(navegacion)
        self.visor = ZoomableScrollArea()
        self.visor.setObjectName("visorCopia")
        self.visor.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.visor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.visor.setMinimumSize(0, 0)
        self.visor.set_zoom_callback(self._ampliar)
        self.imagen = QLabel()
        self.imagen.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.visor.setWidget(self.imagen)
        style_pdf_surface(self.visor)
        self.visor.viewport().installEventFilter(self)
        self.lista.viewport().installEventFilter(self)
        layout.addWidget(self.visor, 1)
        botones = QHBoxLayout()
        botones.setSpacing(SPACE_S)
        self.predeterminadas = QPushButton("Corregir todas con la opción predeterminada")
        self.predeterminadas.setToolTip(
            "Continúa esta bitácora y las restantes sin abrir el visor. "
            "Conserva la copia más antigua y elimina las posteriores."
        )
        self.predeterminadas.clicked.connect(self.usar_predeterminadas)
        botones.addWidget(self.predeterminadas)
        botones.addStretch()
        omitir = QPushButton("Omitir esta bitácora")
        omitir.clicked.connect(self.reject)
        self.aplicar = QPushButton("Eliminar seleccionadas")
        self.aplicar.setObjectName("primaryButton")
        self.aplicar.clicked.connect(self.accept)
        botones.addWidget(omitir)
        botones.addWidget(self.aplicar)
        for boton in (self.anterior, self.siguiente, self.predeterminadas, omitir, self.aplicar):
            boton.setAutoDefault(False)
        layout.addLayout(botones)
        self.lista.itemChanged.connect(self._actualizar)
        self.lista.currentRowChanged.connect(self._mostrar_actual)
        self._atajos = []
        for tecla in ("Return", "Enter"):
            atajo = QShortcut(QKeySequence(tecla), self.lista)
            atajo.setContext(Qt.ShortcutContext.WidgetShortcut)
            atajo.activated.connect(self._alternar)
            self._atajos.append(atajo)
        gestor_tema().cambiado.connect(self._tema)
        self._tema()
        self._acomodar_miniaturas()
        self.lista.setCurrentRow(0)
        self._actualizar()

    def _tema(self, *_):
        colores = paleta()
        self.setStyleSheet(window_stylesheet(
            "QListWidget {"
            f"background-color: {colores.TABLE_BASE_BG}; color: {colores.TABLE_TEXT};"
            f"border: 1px solid {colores.PANE_BORDER}; border-radius: {TABLE_RADIUS}px;"
            f"selection-background-color: palette(highlight); selection-color: {on_accent_text()};"
            f"}} QListWidget::item {{ padding: {SPACE_S}px; border-radius: {TABLE_RADIUS}px; }}"
            "QListWidget::item:selected {"
            f"background-color: palette(highlight); color: {on_accent_text()};"
            "}"
            "QScrollArea#visorCopia {"
            f"border: 1px solid {colores.PANE_BORDER}; border-radius: {TABLE_RADIUS}px;"
            "}"
            + self._densidad.qss
        ))

    def eventFilter(self, objeto, evento):
        if evento.type() == QEvent.Type.Resize:
            if objeto is self.visor.viewport():
                self._pintar_imagen()
            elif objeto is self.lista.viewport():
                self._acomodar_miniaturas()
        return super().eventFilter(objeto, evento)

    def _acomodar_miniaturas(self):
        # Todas siguen accesibles; con muchas copias la franja se desplaza.
        cantidad = max(1, self.lista.count())
        ancho = max(96, self.lista.viewport().width() // cantidad - SPACE_S)
        alto = max(64, min(104, self.height() // 8))
        self.lista.setIconSize(QSize(min(200, ancho - 2 * SPACE_S), alto))
        self.lista.setGridSize(QSize(ancho, alto + self.fontMetrics().height() + 2 * SPACE_M))
        self.lista.setFixedHeight(
            self.lista.gridSize().height() + self.lista.horizontalScrollBar().sizeHint().height() + 2 * SPACE_S
        )

    def _mover(self, paso):
        fila = self.lista.currentRow() + paso
        if 0 <= fila < self.lista.count():
            self.lista.setCurrentRow(fila)

    def _mostrar_actual(self, fila):
        if not 0 <= fila < self.lista.count():
            return
        copia = self._copias[fila]
        fecha = copia.cuando.strftime("%d/%m/%Y %H:%M") if copia.cuando else "Sin fecha"
        self.datos_copia.setText(f"Copia {fila + 1} de {self.lista.count()}: {copia.matricula} - {fecha}")
        self.anterior.setEnabled(fila > 0)
        self.siguiente.setEnabled(fila + 1 < self.lista.count())
        self._zoom = 1.0
        self._actualizar()
        self._pintar_imagen()
        self.visor.verticalScrollBar().setValue(0)
        self.visor.horizontalScrollBar().setValue(0)

    def _pintar_imagen(self):
        fila = self.lista.currentRow()
        if not 0 <= fila < len(self._imagenes):
            return
        pixmap = self._imagenes[fila]
        if pixmap.isNull():
            self.imagen.clear()
            self.imagen.setText("Imagen no disponible")
            self.imagen.adjustSize()
            return
        ancho = max(1, round((self.visor.viewport().width() - 2 * SPACE_S) * self._zoom))
        escalada = pixmap.scaledToWidth(ancho, Qt.TransformationMode.SmoothTransformation)
        self.imagen.setPixmap(escalada)
        self.imagen.resize(escalada.size())

    def _ampliar(self, factor):
        self._zoom = max(0.5, min(4.0, self._zoom * factor))
        self._pintar_imagen()

    def _marcar_actual(self, marcada):
        item = self.lista.currentItem()
        if item is not None:
            item.setCheckState(Qt.CheckState.Checked if marcada else Qt.CheckState.Unchecked)

    def usar_predeterminadas(self):
        self.continuar_con_predeterminadas = True
        self.accept()

    def _alternar(self):
        item = self.lista.currentItem()
        if item:
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked)

    def seleccionadas(self):
        if self.continuar_con_predeterminadas:
            return set(self._predeterminadas)
        return {self.lista.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.lista.count())
                if self.lista.item(i).checkState() == Qt.CheckState.Checked}

    def _actualizar(self, *_):
        self.aplicar.setEnabled(self._validas and 0 < len(self.seleccionadas()) < self.lista.count())
        self.predeterminadas.setEnabled(0 < len(self._predeterminadas) < self.lista.count())
        item = self.lista.currentItem()
        self.eliminar_copia.blockSignals(True)
        self.eliminar_copia.setChecked(item is not None and item.checkState() == Qt.CheckState.Checked)
        self.eliminar_copia.setEnabled(item is not None)
        self.eliminar_copia.blockSignals(False)
