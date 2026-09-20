"""Editor visual de plantillas para BITS.

Los campos que se muestran son los definidos en la plantilla usada por el
pipeline (template/aircraft_log.json). No se pueden renombrar
ni cambiar sus reglas: solo se selecciona un campo y se dibuja el
rectángulo sobre la página para asignarle su posición.

Flujo de uso:
    1. Abrir PDF.
    2. Seleccionar un campo de la lista.
    3. Dibujar el rectángulo sobre el dato (✓ = colocado).
    4. Guardar plantilla JSON.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from loguru import logger
from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGridLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.branding import APPLICATION_DISPLAY_NAME
from app.gui.responsive import density_for, fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import FONT_CAPTION_PT, SPACE_S, paleta
from app.gui.widgets import (
    ZoomOverlay,
    data_table_qss,
    hide_overlay_when_tight,
    keep_overlay_clear_of_scrollbars,
    pintar_del_tema,
    window_stylesheet,
    zoom_overlay_qss,
)
from app.templates.manager import TEMPLATES_DIR, TemplateManager
from app.templates.schema import FieldTemplate, FieldType, Template

_HANDLE_SIZE = 12.0
_MIN_SIZE = 8.0

# Respaldo por si el JSON de plantilla no existe (idéntico a aircraft_log.json).
_FALLBACK_PRESETS: Dict[str, dict] = {
    "log_number": {
        "type": "ocr", "required": True, "regex": "^\\d{7}$",
        "postprocess": "digits",
        "min_ink_ratio": 0.02, "max_ink_ratio": 0.90, "min_components": 2,
    },
    "matricula": {
        "type": "ocr", "required": True,
        "regex": "^HP-\\d{4}(CMP|WWP)$", "postprocess": "matricula",
        "min_ink_ratio": 0.02, "max_ink_ratio": 0.90, "min_components": 2,
    },
    "pilot_signature": {
        "type": "signature", "required": True, "regex": "",
        "postprocess": "",
        "min_ink_ratio": 0.02, "max_ink_ratio": 0.90, "min_components": 2,
    },
    "captain_signature": {
        "type": "signature", "required": True, "regex": "",
        "postprocess": "",
        "min_ink_ratio": 0.02, "max_ink_ratio": 0.90, "min_components": 2,
    },
    "captain_license": {
        "type": "signature", "required": False, "regex": "",
        "postprocess": "",
        "min_ink_ratio": 0.02, "max_ink_ratio": 0.90, "min_components": 2,
    },
    "day": {
        "type": "ocr", "required": True, "regex": "",
        "postprocess": "day", "localize": "ink",
        "min_ink_ratio": 0.02, "max_ink_ratio": 0.90, "min_components": 2,
    },
    "month": {
        "type": "ocr", "required": True, "regex": "",
        "postprocess": "month", "localize": "ink",
        "min_ink_ratio": 0.02, "max_ink_ratio": 0.90, "min_components": 2,
    },
    "year": {
        "type": "ocr", "required": True, "regex": "",
        "postprocess": "year", "localize": "ink",
        "min_ink_ratio": 0.02, "max_ink_ratio": 0.90, "min_components": 2,
    },
    "technician_license": {
        "type": "signature", "required": False, "regex": "",
        "postprocess": "",
        "min_ink_ratio": 0.02, "max_ink_ratio": 0.90, "min_components": 2,
    },
    "technician_signature": {
        "type": "signature", "required": True, "regex": "",
        "postprocess": "",
        "min_ink_ratio": 0.02, "max_ink_ratio": 0.90, "min_components": 2,
    },
}


def _load_icon() -> QIcon:
    """Icono de la aplicación: .ico (multi-tamaño, ideal en Windows) o PNG."""
    assets = Path(__file__).resolve().parents[2] / "assets"
    for name in ("icon.ico", "icon.png"):
        path = assets / name
        if path.is_file():
            return QIcon(str(path))
    return QIcon()


class ResizableRectItem(QGraphicsRectItem):
    """Rectángulo de región editable: mover + redimensionar + etiqueta."""

    def __init__(self, rect: QRectF, field_id: str) -> None:
        super().__init__(rect)
        self.field_id = field_id
        self._active_handle: Optional[str] = None
        self._drag_start_rect: Optional[QRectF] = None

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setBrush(QBrush(QColor(0, 140, 220, 30)))
        self.setPen(QPen(QColor(0, 120, 215), 2))

        self._label = QGraphicsSimpleTextItem(field_id, self)
        self._label.setBrush(QBrush(QColor(20, 60, 100)))
        font = QFont()
        # Rótulo sobre la página, no cromo de ventana: va en el tamaño de
        # subtítulo para no taparla, pero sale de la misma escala.
        font.setPointSize(FONT_CAPTION_PT)
        font.setBold(True)
        self._label.setFont(font)
        self._label.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
        )
        self._update_label()

    def set_field_id(self, field_id: str) -> None:
        """Cambia el nombre del campo y lo refleja en la etiqueta."""
        self.field_id = field_id
        self._label.setText(field_id)
        self._update_label()

    # ── Geometría ───────────────────────────────────────────────────────

    def _handle_rects(self) -> Dict[str, QRectF]:
        r = self.rect()
        h = _HANDLE_SIZE
        return {
            "tl": QRectF(r.topLeft(), QSizeF(h, h)),
            "tr": QRectF(r.topRight() - QPointF(h, 0), QSizeF(h, h)),
            "bl": QRectF(r.bottomLeft() - QPointF(0, h), QSizeF(h, h)),
            "br": QRectF(r.bottomRight() - QPointF(h, h), QSizeF(h, h)),
        }

    def _handle_at(self, pos: QPointF) -> Optional[str]:
        for name, rect in self._handle_rects().items():
            if rect.contains(pos):
                return name
        return None

    def _update_label(self) -> None:
        self._label.setPos(self.rect().topLeft() - QPointF(0, 16))

    # ── Eventos ─────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        handle = self._handle_at(event.pos())
        if handle:
            self._active_handle = handle
            self._drag_start_rect = QRectF(self.rect())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._active_handle and self._drag_start_rect is not None:
            start = self._drag_start_rect
            pos = event.pos()
            r = QRectF(start)
            if self._active_handle in ("tl", "bl"):
                r.setLeft(min(pos.x(), start.right() - _MIN_SIZE))
            if self._active_handle in ("tr", "br"):
                r.setRight(max(pos.x(), start.left() + _MIN_SIZE))
            if self._active_handle in ("tl", "tr"):
                r.setTop(min(pos.y(), start.bottom() - _MIN_SIZE))
            if self._active_handle in ("bl", "br"):
                r.setBottom(max(pos.y(), start.top() + _MIN_SIZE))
            self.setRect(r)
            self._update_label()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._active_handle = None
        self._drag_start_rect = None
        super().mouseReleaseEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._update_label()
        return super().itemChange(change, value)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        super().paint(painter, option, widget)
        if self.isSelected():
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            painter.setPen(QPen(QColor(0, 120, 215), 1))
            for rect in self._handle_rects().values():
                painter.drawRect(rect)


class ZoomableGraphicsView(QGraphicsView):
    """QGraphicsView con zoom por Ctrl + rueda del ratón."""

    def __init__(self, scene, parent=None) -> None:
        super().__init__(scene, parent)
        self._zoom_callback = None

    def set_zoom_callback(self, callback) -> None:
        self._zoom_callback = callback

    def wheelEvent(self, event) -> None:
        if (event.modifiers() & Qt.KeyboardModifier.ControlModifier
                and self._zoom_callback is not None):
            delta = event.angleDelta().y()
            self._zoom_callback(1.25 if delta > 0 else 0.8)
            event.accept()
            return
        super().wheelEvent(event)


class EditorScene(QGraphicsScene):
    """Escena que crea rectángulos al arrastrar sobre el fondo."""

    field_created = Signal(QRectF)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._start: Optional[QPointF] = None
        self._drawing: Optional[QGraphicsRectItem] = None

    def mousePressEvent(self, event) -> None:
        item = None
        if self.views():
            item = self.itemAt(event.scenePos(), self.views()[0].transform())
        if isinstance(item, QGraphicsPixmapItem) or item is None:
            self._start = event.scenePos()
            self._drawing = QGraphicsRectItem(
                QRectF(self._start, QSizeF(0, 0))
            )
            self._drawing.setPen(
                QPen(QColor(0, 180, 80), 1, Qt.PenStyle.DashLine)
            )
            self.addItem(self._drawing)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drawing is not None and self._start is not None:
            rect = QRectF(self._start, event.scenePos()).normalized()
            self._drawing.setRect(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drawing is not None:
            rect = self._drawing.rect().normalized()
            self.removeItem(self._drawing)
            self._drawing = None
            self._start = None
            if rect.width() >= _MIN_SIZE and rect.height() >= _MIN_SIZE:
                self.field_created.emit(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class EditorWindow(QMainWindow):
    """Editor visual de plantillas con campos fijos del código."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            f"{APPLICATION_DISPLAY_NAME} - Editor de plantilla"
        )
        # El tamaño lo pone la pantalla: 1200x800 no entra en un portátil de
        # 1366x768 y la fila de botones de abajo se quedaba fuera.
        self._density = fit_to_screen(self, 1200, 800)
        self.setWindowIcon(_load_icon())
        self._apply_density_stylesheet()
        # La hoja lleva el fragmento de la densidad, así que la rehace la
        # ventana y no el módulo del tema.
        gestor_tema().cambiado.connect(self._on_tema_cambiado)

        self._pdf_path: Optional[Path] = None
        self._current_page = 0
        self._total_pages = 0
        self._image_size = (0, 0)  # (ancho, alto) en píxeles
        self._items: Dict[str, ResizableRectItem] = {}  # campo → rectángulo
        self._selected_id: Optional[str] = None
        self._editor_zoom = 1.0  # 1.0 = ajustado a la ventana

        # Campos tal como venían en la plantilla, para no perder al guardar
        # las propiedades que el editor no expone.
        self._base_fields: Dict[str, FieldTemplate] = {}
        self._presets: Dict[str, dict] = self._load_presets()

        self._build_ui()
        self._connect_signals()
        self._rebuild_field_list()
        self._install_zoom_shortcuts()

    # ── Campos fijos (usados en el código) ──────────────────────────────

    def _load_presets(self) -> Dict[str, dict]:
        """Campos de la plantilla usada por el pipeline (aircraft_log.json)."""
        path = TEMPLATES_DIR / "aircraft_log.json"
        try:
            template = TemplateManager().load(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"No se pudieron cargar los campos de {path}: "
                           f"{exc}; usando valores de respaldo")
            return dict(_FALLBACK_PRESETS)
        presets: Dict[str, dict] = {}
        for field in template.fields:
            # Se conserva el campo completo: al guardar se copia y solo se
            # reemplaza lo que el editor realmente edita. Reconstruirlo desde
            # el preset perdía silenciosamente todo lo que el editor no
            # muestra (longitudes, umbrales de firma, modo de lectura).
            self._base_fields[field.id] = field
            presets[field.id] = {
                "type": field.type.value,
                "required": field.required,
                "regex": field.regex or "",
                "postprocess": field.postprocess or "",
                "localize": field.localize or "",
                "min_ink_ratio": field.min_ink_ratio,
                "max_ink_ratio": field.max_ink_ratio,
                "min_components": field.min_components,
            }
        if not presets:
            presets = dict(_FALLBACK_PRESETS)
        logger.info(f"Campos del editor ({len(presets)}): "
                    f"{', '.join(presets)}")
        return presets

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar = QToolBar("Principal")
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        act_open = toolbar.addAction("Abrir PDF")
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.setToolTip("Abrir PDF (Ctrl+O)")
        act_open.triggered.connect(self._open_pdf)

        self.btn_prev = toolbar.addAction("Página anterior")
        self.btn_prev.setShortcut(QKeySequence(Qt.Key.Key_Left))
        self.btn_prev.setToolTip("Página anterior (flecha izquierda)")
        self.btn_prev.setEnabled(False)
        self.btn_prev.triggered.connect(self._prev_page)

        self.btn_next = toolbar.addAction("Página siguiente")
        self.btn_next.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self.btn_next.setToolTip("Página siguiente (flecha derecha)")
        self.btn_next.setEnabled(False)
        self.btn_next.triggered.connect(self._next_page)

        for accion, flecha in (
            (self.btn_prev, Qt.ArrowType.LeftArrow),
            (self.btn_next, Qt.ArrowType.RightArrow),
        ):
            boton = toolbar.widgetForAction(accion)
            boton.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            boton.setArrowType(flecha)
            boton.setAccessibleName(accion.text())

        toolbar.addSeparator()
        act_load = toolbar.addAction("Cargar plantilla")
        act_load.setToolTip("Cargar una plantilla JSON sobre el PDF abierto")
        act_load.triggered.connect(self._load_template)
        act_save = toolbar.addAction("Guardar plantilla")
        act_save.setShortcut(QKeySequence.StandardKey.Save)
        act_save.setToolTip("Guardar plantilla (Ctrl+S)")
        act_save.triggered.connect(self._save_template)
        toolbar.addSeparator()
        act_del = toolbar.addAction("Quitar campo")
        act_del.setShortcut(QKeySequence.StandardKey.Delete)
        act_del.setToolTip("Quitar el campo seleccionado (Supr)")
        act_del.triggered.connect(self._delete_selected)

        self.page_label = QLabel("Página 0 de 0")
        toolbar.addWidget(self.page_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        self.scene = EditorScene(self)
        self.view = ZoomableGraphicsView(self.scene)
        self.view.set_zoom_callback(self._zoom_editor)
        self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.view.setRenderHints(QPainter.RenderHint.Antialiasing)
        self.view.setMinimumWidth(self._density.editor_view_min_width)
        view_container = QWidget()
        view_layout = QGridLayout(view_container)
        view_layout.setContentsMargins(0, 0, 0, 0)
        view_layout.addWidget(self.view, 0, 0)

        # La misma píldora que la vista previa y el visor de PDF: el editor
        # tenía una copia a mano que además la ponía en otra esquina, así que
        # el mismo control estaba en tres sitios distintos según la ventana.
        zoom_overlay = ZoomOverlay(
            ("Acercar el lienzo", "Acercar lienzo", lambda: self._zoom_editor(1.25)),
            (
                "Ajustar la página a la ventana",
                "Ajustar página a la ventana",
                self._fit_editor,
            ),
            ("Alejar el lienzo", "Alejar lienzo", lambda: self._zoom_editor(0.8)),
            view_container,
        )
        self.btn_zoom_in = zoom_overlay.btn_in
        self.btn_zoom_fit = zoom_overlay.btn_fit
        self.btn_zoom_out = zoom_overlay.btn_out
        self.zoom_label = zoom_overlay.value_label

        zoom_holder = QWidget(view_container)
        zoom_holder_layout = QVBoxLayout(zoom_holder)
        zoom_holder_layout.setContentsMargins(8, 8, 8, 8)
        zoom_holder_layout.addWidget(zoom_overlay)
        # El recuadro de zoom flota sobre la página: ni decide el mínimo
        # del panel ni se dibuja a medias. Ver ``hide_overlay_when_tight``.
        zoom_holder.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        view_layout.addWidget(
            zoom_holder,
            0,
            0,
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        )
        zoom_holder.raise_()
        keep_overlay_clear_of_scrollbars(zoom_holder, self.view)
        hide_overlay_when_tight(zoom_holder)
        self._update_zoom_editor_controls()

        splitter.addWidget(view_container)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        margin = max(SPACE_S, self._density.window_margin)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(SPACE_S)

        hint = QLabel(
            "Seleccione un campo y dibuje un rectángulo sobre el dato.\n"
            "Dibújelo de nuevo para moverlo; Supr lo quita. "
            "✓ indica que ya está colocado."
        )
        hint.setWordWrap(True)
        pintar_del_tema(
            hint, lambda: f"color: {paleta().TEXT_SECONDARY};"
        )
        layout.addWidget(hint)

        layout.addWidget(QLabel("Campos de la plantilla"))
        self.field_list = QListWidget()
        self.field_list.currentItemChanged.connect(self._on_list_select)
        layout.addWidget(self.field_list, stretch=2)

        layout.addWidget(QLabel("Campo seleccionado:"))
        self.info_label = QLabel("Ninguno")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.info_label)

        layout.addStretch()
        splitter.addWidget(panel)
        self._side_panel = panel
        panel.setMaximumWidth(self._density.editor_panel_max_width)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([900, 320])

    def _connect_signals(self) -> None:
        self.scene.field_created.connect(self._add_field_rect)
        self.scene.selectionChanged.connect(self._on_scene_selection)

    def _apply_density_stylesheet(self) -> None:
        self.setStyleSheet(
            window_stylesheet(
                data_table_qss() + zoom_overlay_qss() + self._density.qss
            )
        )

    def _on_tema_cambiado(self, _nombre: str) -> None:
        """Rehace la hoja de la ventana con los tonos del tema nuevo."""
        self._apply_density_stylesheet()

    def _update_responsive_layout(self) -> None:
        density = density_for(self.height(), self._density)
        if density is self._density:
            return
        self._density = density
        self._apply_density_stylesheet()
        self.view.setMinimumWidth(density.editor_view_min_width)
        self._side_panel.setMaximumWidth(density.editor_panel_max_width)
        layout = self._side_panel.layout()
        margin = max(SPACE_S, density.window_margin)
        layout.setContentsMargins(margin, margin, margin, margin)

    def resizeEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().resizeEvent(event)
        if hasattr(self, "_side_panel"):
            self._update_responsive_layout()

    # ── Lista de campos ────────────────────────────────────────────────

    def _rebuild_field_list(self) -> None:
        self.field_list.clear()
        for field_id in self._presets:
            self._add_list_item(field_id)
        self._selected_id = None
        self._show_field_info(None)

    def _add_list_item(self, field_id: str) -> None:
        list_item = QListWidgetItem(field_id)
        list_item.setData(Qt.ItemDataRole.UserRole, field_id)
        self._update_list_item(list_item, field_id)
        self.field_list.addItem(list_item)

    def _update_list_item(self, list_item: QListWidgetItem,
                          field_id: str) -> None:
        props = self._presets.get(field_id, {})
        text = field_id
        if props.get("type"):
            text += f"  [{props['type']}]"
        if props.get("required"):
            text += " *"
        if field_id in self._items:
            text += "  ✓"
        list_item.setText(text)

    def _set_placed_marker(self, field_id: str) -> None:
        for i in range(self.field_list.count()):
            item = self.field_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == field_id:
                self._update_list_item(item, field_id)
                return

    # ── Acciones ────────────────────────────────────────────────────────

    def _open_pdf(self) -> None:
        _input_dir = Path(__file__).resolve().parents[2] / "input"
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir PDF", str(_input_dir), "PDF (*.pdf)"
        )
        if not path:
            return
        try:
            self._pdf_path = Path(path)
            self._current_page = 1
            self._total_pages = self._count_pages(self._pdf_path)
            self._render_current_page()
            logger.info(f"PDF abierto: {path} ({self._total_pages} páginas)")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error", str(exc))
            self._pdf_path = None

    def _render_current_page(self) -> None:
        if self._pdf_path is None:
            return
        try:
            from app.vision.pdf_loader import render_page
            import cv2

            image = render_page(self._pdf_path, self._current_page, dpi=150)
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            self._image_size = (w, h)

            pixmap = QPixmap.fromImage(
                QImage(rgb.data, w, h, ch * w,
                       QImage.Format.Format_RGB888).copy()
            )
            self.scene.clear()
            self._items.clear()
            self.field_list.clear()
            self.scene.addItem(QGraphicsPixmapItem(pixmap))
            self.scene.setSceneRect(QRectF(0, 0, w, h))
            self.view.fitInView(self.scene.sceneRect(),
                                Qt.AspectRatioMode.KeepAspectRatio)
            self._editor_zoom = 1.0
            self._update_zoom_editor_controls()
            self.page_label.setText(
                f"Página {self._current_page} de {self._total_pages}"
            )
            self._rebuild_field_list()
            self._update_nav_state()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error", str(exc))

    def _update_nav_state(self) -> None:
        """Habilita/deshabilita las flechas según la página actual."""
        has_pdf = self._pdf_path is not None
        self.btn_prev.setEnabled(has_pdf and self._current_page > 1)
        self.btn_next.setEnabled(
            has_pdf and self._current_page < self._total_pages
        )

    # ── Zoom ────────────────────────────────────────────────────────────

    def _update_zoom_editor_controls(self) -> None:
        """Sincroniza los controles de zoom con la página abierta."""
        has_image = self._pdf_path is not None and self._image_size != (0, 0)
        self.btn_zoom_in.setEnabled(has_image and self._editor_zoom < 4.0)
        self.btn_zoom_fit.setEnabled(has_image)
        self.btn_zoom_out.setEnabled(has_image and self._editor_zoom > 0.4)
        self.zoom_label.setText(f"{round(self._editor_zoom * 100)}%")

    def _zoom_editor(self, factor: float) -> None:
        """Aplica zoom relativo alrededor del centro del lienzo."""
        new_zoom = min(4.0, max(0.4, self._editor_zoom * factor))
        if new_zoom == self._editor_zoom:
            return
        self.view.scale(new_zoom / self._editor_zoom,
                        new_zoom / self._editor_zoom)
        self._editor_zoom = new_zoom
        self._update_zoom_editor_controls()

    def keyPressEvent(self, event) -> None:
        """Ctrl++ / Ctrl+- para hacer zoom sobre el lienzo."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            key = event.key()
            if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self._zoom_editor(1.25)
                event.accept()
                return
            if key in (Qt.Key.Key_Minus,):
                self._zoom_editor(0.8)
                event.accept()
                return
        super().keyPressEvent(event)

    def _install_zoom_shortcuts(self) -> None:
        """Atajos Ctrl++ / Ctrl+- independientes del widget con foco."""
        for seq, factor in (
            (QKeySequence("Ctrl++"), 1.25),
            (QKeySequence("Ctrl+="), 1.25),
            (QKeySequence("Ctrl+-"), 0.8),
        ):
            QShortcut(seq, self,
                      activated=lambda f=factor: self._zoom_editor(f))

    def _fit_editor(self) -> None:
        """Ajusta la página completa a la ventana."""
        if self._pdf_path is None or self._image_size == (0, 0):
            return
        self.view.fitInView(self.scene.sceneRect(),
                            Qt.AspectRatioMode.KeepAspectRatio)
        self._editor_zoom = 1.0
        self._update_zoom_editor_controls()

    def _prev_page(self) -> None:
        if self._pdf_path and self._current_page > 1:
            self._current_page -= 1
            self._render_current_page()

    def _next_page(self) -> None:
        if self._pdf_path and self._current_page < self._total_pages:
            self._current_page += 1
            self._render_current_page()

    # ── Asignación de campos ────────────────────────────────────────────

    def _add_field_rect(self, rect: QRectF) -> None:
        if self._selected_id is None:
            self._selected_id = self._first_unplaced()
            if self._selected_id is None:
                QMessageBox.information(
                    self, "Aviso",
                    "Todos los campos ya están colocados.\n"
                    "Selecciona uno para reposicionarlo o quita "
                    "el que quieras redibujar (Supr)."
                )
                return

        field_id = self._selected_id
        existing = self._items.get(field_id)
        if existing is not None:
            existing.setRect(rect)
            existing._update_label()
            logger.info(f"Campo reposicionado: {field_id}")
        else:
            item = ResizableRectItem(rect, field_id)
            self.scene.addItem(item)
            self._items[field_id] = item
            logger.info(f"Campo colocado: {field_id}")

        self._set_placed_marker(field_id)
        self._show_field_info(field_id)
        self._select_row(field_id)

    def _first_unplaced(self) -> Optional[str]:
        for field_id in self._presets:
            if field_id not in self._items:
                return field_id
        return None

    def _select_row(self, field_id: str) -> None:
        for i in range(self.field_list.count()):
            if self.field_list.item(i).data(
                    Qt.ItemDataRole.UserRole) == field_id:
                self.field_list.setCurrentRow(i)
                return

    def _delete_selected(self) -> None:
        if self._selected_id is None:
            return
        item = self._items.pop(self._selected_id, None)
        if item is not None:
            self.scene.removeItem(item)
        self._set_placed_marker(self._selected_id)
        logger.info(f"Campo retirado de la página: {self._selected_id}")

    # ── Selección e información ─────────────────────────────────────────

    def _on_list_select(self, current, _previous) -> None:
        if current is None:
            self._selected_id = None
            self._show_field_info(None)
            return
        field_id = current.data(Qt.ItemDataRole.UserRole)
        self._selected_id = field_id
        self._show_field_info(field_id)
        item = self._items.get(field_id)
        if item is None:
            return
        self.scene.clearSelection()
        item.setSelected(True)

    def _on_scene_selection(self) -> None:
        """Sincroniza la selección del lienzo con la lista de campos."""
        selected_id = None
        for item in self.scene.selectedItems():
            if isinstance(item, ResizableRectItem):
                selected_id = item.field_id
                break
        if selected_id is None:
            return
        row = None
        for i in range(self.field_list.count()):
            if self.field_list.item(i).data(
                    Qt.ItemDataRole.UserRole) == selected_id:
                row = i
                break
        if row is not None and row != self.field_list.currentRow():
            self.field_list.setCurrentRow(row)

    def _show_field_info(self, field_id: Optional[str]) -> None:
        if field_id is None:
            self.info_label.setText("Ninguno")
            return
        props = self._presets.get(field_id, {})
        lines = [
            f"<b>{field_id}</b>",
            f"Tipo: {props.get('type', '?')}",
            "Obligatorio: sí" if props.get("required")
            else "Obligatorio: no",
        ]
        if props.get("regex"):
            lines.append(f"Regex: {props['regex']}")
        if props.get("postprocess"):
            lines.append(f"Postproceso: {props['postprocess']}")
        if props.get("type") in ("signature", "checkbox"):
            lines.append(
                f"Tinta: {props['min_ink_ratio']}–{props['max_ink_ratio']} "
                f"(≥ {props['min_components']} trazos)"
            )
        if field_id in self._items:
            lines.append("<b>✓ colocado</b>")
        else:
            lines.append("(aún no colocado: dibuja el rectángulo)")
        self.info_label.setText("<br>".join(lines))

    # ── Plantilla ───────────────────────────────────────────────────────

    def _collect_template(self) -> Template:
        fields = []
        w, h = self._image_size
        for field_id, item in self._items.items():
            rect = item.sceneBoundingRect()
            props = self._presets.get(field_id, {})
            # Solo estas propiedades se editan aquí; el resto (longitudes,
            # modo de lectura, umbrales de firma) se hereda del campo
            # original para que guardar la geometría no las borre.
            edited = {
                "id": field_id,
                "type": FieldType(props.get("type", "ocr")),
                "required": bool(props.get("required")),
                "x": round(rect.left() / w, 4),
                "y": round(rect.top() / h, 4),
                "w": round(rect.width() / w, 4),
                "h": round(rect.height() / h, 4),
                "regex": props.get("regex") or None,
                "postprocess": props.get("postprocess") or None,
                "localize": props.get("localize") or None,
                "min_ink_ratio": props.get("min_ink_ratio", 0.02),
                "max_ink_ratio": props.get("max_ink_ratio", 0.90),
                "min_components": props.get("min_components", 2),
            }
            base = self._base_fields.get(field_id)
            fields.append(
                base.model_copy(update=edited) if base is not None
                else FieldTemplate(**edited)
            )
        return Template(name="Aircraft Log", page_size=list(self._image_size),
                        fields=fields)

    def _save_template(self) -> None:
        if not self._image_size or not self._items:
            QMessageBox.warning(
                self, "Aviso",
                "Coloque al menos un campo dibujando su rectángulo."
            )
            return
        name, ok = QInputDialog.getText(
            self, "Nombre de la plantilla",
            "Nombre de la plantilla:", text="Aircraft Log"
        )
        if not ok or not name.strip():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Guardar plantilla", str(TEMPLATES_DIR),
            "JSON (*.json)"
        )
        if not path:
            return
        try:
            template = self._collect_template()
            template.name = name.strip()
            TemplateManager().save(template, Path(path))
            QMessageBox.information(
                self, "Guardado",
                f"Plantilla guardada con {len(template.fields)} campos:\n{path}",
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error", str(exc))

    def _load_template(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Cargar plantilla", str(TEMPLATES_DIR), "JSON (*.json)"
        )
        if not path:
            return
        try:
            template = TemplateManager().load(Path(path))
            if not self._pdf_path:
                QMessageBox.warning(
                    self, "Aviso", "Abra primero el PDF de referencia."
                )
                return
            self._apply_template_to_scene(template)
            logger.info(f"Plantilla cargada: {template.name}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error", str(exc))

    def _apply_template_to_scene(self, template: Template) -> None:
        # Quitar rectángulos previos, conservando el fondo (pixmap).
        for item in list(self.scene.items()):
            if isinstance(item, ResizableRectItem):
                self.scene.removeItem(item)

        self._items.clear()
        self.field_list.clear()
        w, h = self._image_size

        # La plantilla abierta manda: sus campos son la base que se copia al
        # guardar, para no reemplazar sus reglas por las del preset.
        for field in template.fields:
            self._base_fields[field.id] = field

        # Campos que no están en los presets del código se conservan como
        # extras: visibles y seleccionables, pero con reglas por defecto.
        extras = [f for f in template.fields if f.id not in self._presets]
        for extra in extras:
            self._presets[extra.id] = {
                "type": extra.type.value,
                "required": extra.required,
                "regex": extra.regex or "",
                "postprocess": extra.postprocess or "",
                "min_ink_ratio": extra.min_ink_ratio,
                "max_ink_ratio": extra.max_ink_ratio,
                "min_components": extra.min_components,
            }
            logger.info(f"Campo extra cargado: {extra.id}")

        for field in template.fields:
            rect = QRectF(field.x * w, field.y * h,
                          field.w * w, field.h * h)
            item = ResizableRectItem(rect, field.id)
            self.scene.addItem(item)
            self._items[field.id] = item

        self._rebuild_field_list()
        for field_id in self._presets:
            self._set_placed_marker(field_id)
        self._selected_id = None
        self.field_list.setCurrentRow(-1)
        self._show_field_info(None)

    @staticmethod
    def _count_pages(pdf_path: Path) -> int:
        import pymupdf as fitz

        with fitz.open(str(pdf_path)) as doc:
            return len(doc)
