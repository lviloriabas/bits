"""Comparación de imágenes antes de eliminar copias de Web Reports."""

from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QKeySequence, QPalette, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QVBoxLayout,
)

from app.airvault.correcciones import por_antiguedad
from app.gui.responsive import fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_M, SPACE_S, on_accent_text, paleta
from app.gui.widgets import (
    TABLE_RADIUS, ZoomableScrollArea, style_pdf_surface, window_stylesheet,
)


class _MiniaturaCopiaDelegate(QStyledItemDelegate):
    """Mantiene la casilla junto al nombre, debajo de la imagen."""

    def _rectangulos(self, option):
        contenido = option.rect.adjusted(SPACE_S, SPACE_S, -SPACE_S, -SPACE_S)
        estilo = option.widget.style()
        ancho = estilo.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, option, option.widget)
        alto = estilo.pixelMetric(QStyle.PixelMetric.PM_IndicatorHeight, option, option.widget)
        fila = max(alto, option.fontMetrics.height())
        ancho_texto = min(option.fontMetrics.horizontalAdvance(option.text),
                          max(0, contenido.width() - ancho - SPACE_S))
        izquierda = contenido.center().x() - (ancho + SPACE_S + ancho_texto) // 2
        etiqueta = QRect(izquierda, contenido.bottom() - fila + 1,
                         ancho + SPACE_S + ancho_texto, fila)
        casilla = QRect(izquierda, etiqueta.center().y() - alto // 2, ancho, alto)
        texto = QRect(casilla.right() + SPACE_S + 1, etiqueta.top(), ancho_texto, fila)
        imagen = QRect(contenido.left(), contenido.top(), contenido.width(),
                       max(0, etiqueta.top() - SPACE_S - contenido.top()))
        return imagen, casilla, texto

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        imagen, casilla, texto = self._rectangulos(opt)
        fondo = QStyleOptionViewItem(opt)
        fondo.text = ""
        fondo.icon = QIcon()
        fondo.features &= ~(QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
                            | QStyleOptionViewItem.ViewItemFeature.HasDecoration
                            | QStyleOptionViewItem.ViewItemFeature.HasDisplay)
        estilo = opt.widget.style()
        painter.save()
        estilo.drawControl(QStyle.ControlElement.CE_ItemViewItem, fondo, painter, opt.widget)
        painter.restore()
        opt.icon.paint(painter, imagen, Qt.AlignmentFlag.AlignCenter)
        marca = QStyleOptionViewItem(opt)
        marca.rect = casilla
        marca.state &= ~(QStyle.StateFlag.State_On | QStyle.StateFlag.State_Off
                         | QStyle.StateFlag.State_NoChange)
        marca.state |= (QStyle.StateFlag.State_On if opt.checkState == Qt.CheckState.Checked
                        else QStyle.StateFlag.State_Off)
        if not index.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            marca.state &= ~QStyle.StateFlag.State_Enabled
        painter.save()
        estilo.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
                             marca, painter, opt.widget)
        painter.restore()
        rol = (QPalette.ColorRole.HighlightedText if opt.state & QStyle.StateFlag.State_Selected
               else QPalette.ColorRole.Text)
        painter.save()
        painter.setFont(opt.font)
        painter.setPen(opt.palette.color(rol))
        painter.drawText(texto, Qt.AlignmentFlag.AlignCenter,
                         opt.fontMetrics.elidedText(opt.text, Qt.TextElideMode.ElideRight,
                                                   texto.width()))
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if not (index.flags() & Qt.ItemFlag.ItemIsUserCheckable
                and index.flags() & Qt.ItemFlag.ItemIsEnabled):
            return False
        if event.type() == QEvent.Type.KeyPress:
            return super().editorEvent(event, model, option, index)
        if event.type() not in (QEvent.Type.MouseButtonPress,
                               QEvent.Type.MouseButtonRelease,
                               QEvent.Type.MouseButtonDblClick):
            return False
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if event.button() != Qt.MouseButton.LeftButton or not self._rectangulos(opt)[1].contains(event.position().toPoint()):
            return False
        if event.type() == QEvent.Type.MouseButtonRelease:
            estado = (Qt.CheckState.Unchecked if opt.checkState == Qt.CheckState.Checked
                      else Qt.CheckState.Checked)
            return model.setData(index, estado, Qt.ItemDataRole.CheckStateRole)
        return True


class RevisionCopiasDialog(QDialog):
    respuesta = Signal(object)
    cancelar = Signal()

    def __init__(self, correccion, vistas, elegidas, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self._densidad = fit_to_screen(self, 1100, 850)
        self._predeterminadas = set()
        self._pendiente = False
        self.continuar_con_predeterminadas = False
        self._imagenes = []
        self._copias = []
        self._zoom = 1.0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*([self._densidad.window_margin] * 4))
        layout.setSpacing(SPACE_S)
        self.ayuda = QLabel()
        self.ayuda.setWordWrap(True)
        layout.addWidget(self.ayuda)
        contenido = QHBoxLayout()
        contenido.setSpacing(SPACE_S)
        self.lista = QListWidget()
        self.lista.setItemDelegate(_MiniaturaCopiaDelegate(self.lista))
        self.lista.setViewMode(QListWidget.ViewMode.IconMode)
        self.lista.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.lista.setMovement(QListWidget.Movement.Static)
        self.lista.setFlow(QListWidget.Flow.TopToBottom)
        self.lista.setWrapping(False)
        self.lista.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lista.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.lista.setSpacing(SPACE_S)
        contenido.addWidget(self.lista)
        detalle = QVBoxLayout()
        detalle.setSpacing(SPACE_S)
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
        detalle.addLayout(navegacion)
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
        detalle.addWidget(self.visor, 1)
        contenido.addLayout(detalle, 1)
        layout.addLayout(contenido, 1)
        botones = QHBoxLayout()
        botones.setSpacing(SPACE_S)
        self.predeterminadas = QPushButton("Corregir todas con la opción predeterminada")
        self.predeterminadas.setToolTip(
            "Continúa esta bitácora y las restantes sin pedir más revisiones. "
            "Conserva la copia más antigua y elimina las posteriores."
        )
        self.predeterminadas.clicked.connect(self.usar_predeterminadas)
        botones.addWidget(self.predeterminadas)
        botones.addStretch()
        self.omitir = QPushButton("Omitir esta bitácora")
        self.omitir.clicked.connect(lambda: self._responder(None))
        self.boton_cancelar = QPushButton("Cancelar")
        self.boton_cancelar.clicked.connect(self.reject)
        self.aplicar = QPushButton("Eliminar seleccionadas")
        self.aplicar.setObjectName("primaryButton")
        self.aplicar.clicked.connect(self.accept)
        botones.addWidget(self.boton_cancelar)
        botones.addWidget(self.omitir)
        botones.addWidget(self.aplicar)
        for boton in (self.anterior, self.siguiente, self.predeterminadas, self.boton_cancelar, self.omitir, self.aplicar):
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
        self.cargar(correccion, vistas, elegidas)

    def cargar(self, correccion, vistas, elegidas):
        """Cambia de bitácora sin cerrar ni recrear el visor."""
        self.setWindowTitle(f"Revisar copias de la bitácora {correccion.log_number}")
        self._pendiente = True
        self.continuar_con_predeterminadas = False
        vistas = list(vistas)
        original, _ = por_antiguedad([copia for copia, _imagen in vistas])
        if original is None and len(vistas) == 1:
            original = vistas[0][0]
        self._original = original.clave if original is not None else None
        extras = {copia.clave for copia, _imagen in vistas if copia.clave != self._original}
        self._predeterminadas = set(elegidas) & extras
        self._imagenes = []
        self._copias = []
        self._validas = original is not None
        self.ayuda.setText(
            "Seleccione una copia para verla en grande y marque las que desea eliminar. "
            "La original (la más antigua) se conserva siempre. Use Ctrl + rueda para ampliar la imagen."
        )
        self.lista.blockSignals(True)
        self.lista.clear()
        for copia, imagen in vistas:
            pixmap = QPixmap()
            valida = pixmap.loadFromData(imagen, "PNG")
            self._imagenes.append(pixmap)
            self._copias.append(copia)
            self._validas = self._validas and valida
            fecha = copia.cuando.strftime("%d/%m/%Y %H:%M") if copia.cuando else "Sin fecha"
            numero = len(self._copias)
            es_original = copia.clave == self._original
            nombre = "Original" if es_original else f"Copia {numero}"
            item = QListWidgetItem(QIcon(pixmap), nombre if valida else f"{nombre}: sin imagen")
            item.setToolTip(f"{copia.matricula}\n{fecha}" + ("\nLa original se conserva siempre." if es_original else ""))
            item.setData(Qt.ItemDataRole.UserRole, copia.clave)
            if not es_original:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if copia.clave in self._predeterminadas else Qt.CheckState.Unchecked)
            self.lista.addItem(item)
        self.lista.blockSignals(False)
        self.imagen.clear()
        self.datos_copia.clear()
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
        # La lista lateral desplaza las copias verticalmente.
        ancho = max(120, min(180, self.width() // 6))
        alto = max(80, min(140, self.height() // 6))
        self.lista.setIconSize(QSize(ancho - 2 * SPACE_M, alto))
        self.lista.setGridSize(QSize(ancho, alto + self.fontMetrics().height() + 2 * SPACE_M))
        self.lista.setFixedWidth(ancho + self.lista.verticalScrollBar().sizeHint().width() + 2 * SPACE_S)

    def _mover(self, paso):
        fila = self.lista.currentRow() + paso
        if 0 <= fila < self.lista.count():
            self.lista.setCurrentRow(fila)

    def _mostrar_actual(self, fila):
        if not 0 <= fila < self.lista.count():
            return
        copia = self._copias[fila]
        fecha = copia.cuando.strftime("%d/%m/%Y %H:%M") if copia.cuando else "Sin fecha"
        original = " (original)" if copia.clave == self._original else ""
        self.datos_copia.setText(f"Copia {fila + 1} de {self.lista.count()}{original}: {copia.matricula} - {fecha}")
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
        if self._pendiente and item is not None and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            item.setCheckState(Qt.CheckState.Checked if marcada else Qt.CheckState.Unchecked)
        else:
            self._actualizar()

    def usar_predeterminadas(self):
        if not self.predeterminadas.isEnabled():
            return
        self.continuar_con_predeterminadas = True
        self._responder(set(self._predeterminadas))

    def accept(self):
        if self.aplicar.isEnabled():
            self._responder(self.seleccionadas())

    def _responder(self, seleccion):
        if not self._pendiente:
            return
        self._pendiente = False
        self.ayuda.setText("Procesando la corrección. Puede cancelar o cerrar el visor para detenerla.")
        for i in range(self.lista.count()):
            item = self.lista.item(i)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        self._actualizar()
        self.respuesta.emit(seleccion)

    def reject(self):
        self._pendiente = False
        self.cancelar.emit()
        super().reject()

    def finalizar(self):
        """Cierra al terminar el trabajo sin solicitar una cancelación."""
        self._pendiente = False
        self.done(QDialog.DialogCode.Accepted)

    def _alternar(self):
        if not self._pendiente:
            return
        item = self.lista.currentItem()
        if item and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked)

    def seleccionadas(self):
        if self.continuar_con_predeterminadas:
            return set(self._predeterminadas)
        return {self.lista.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.lista.count())
                if self.lista.item(i).checkState() == Qt.CheckState.Checked
                and self.lista.item(i).data(Qt.ItemDataRole.UserRole) != self._original}

    def _actualizar(self, *_):
        for i in range(self.lista.count()):
            original = self.lista.item(i)
            if original.data(Qt.ItemDataRole.UserRole) == self._original and original.checkState() != Qt.CheckState.Unchecked:
                original.setCheckState(Qt.CheckState.Unchecked)
        self.aplicar.setEnabled(self._pendiente and self._validas and 0 < len(self.seleccionadas()) < self.lista.count())
        self.predeterminadas.setEnabled(self._pendiente and self._original is not None
                                       and 0 < len(self._predeterminadas) < self.lista.count())
        self.omitir.setEnabled(self._pendiente)
        item = self.lista.currentItem()
        self.eliminar_copia.blockSignals(True)
        self.eliminar_copia.setChecked(item is not None and item.checkState() == Qt.CheckState.Checked)
        self.eliminar_copia.setEnabled(self._pendiente and item is not None
                                      and bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable))
        self.eliminar_copia.blockSignals(False)
