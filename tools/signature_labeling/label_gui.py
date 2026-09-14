#!/usr/bin/env python3
"""Ventana para etiquetar recortes de firma a mano (suite temporal).

Etiquetar cientos de recortes solo sale a cuenta si cada uno cuesta una tecla.
La ventana está construida alrededor de eso: una rejilla de recortes con un
cursor que se mueve con las flechas, tres teclas para decidir (``F`` firma,
``A`` ausente, ``D`` dudosa) y avance automático al siguiente. Debajo hay un
panel con el recorte seleccionado ampliado y el recuadro exacto del campo
dibujado encima, que es lo que hay que mirar para decidir si la escritura está
*dentro* del campo o es la del vecino.

El veredicto actual del detector está oculto a propósito: verlo antes de
decidir sesga la etiqueta, y estas etiquetas son la vara con la que después se
mide el detector. Se puede encender (casilla o tecla ``V``) para revisar en
qué se equivoca, una vez etiquetado el batch.

Uso::

    portable/python312/tools/python.exe tools/signature_labeling/label_gui.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.utils.portable import ensure_portable_env  # noqa: E402

ensure_portable_env()

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.gui.tokens import paleta  # noqa: E402
from app.gui.widgets import (  # noqa: E402
    TABLE_SELECTION_BG,
    pane_status_colors,
    scrollbars_qss,
    style_dark_pane,
)
from tools.signature_labeling.dataset import (  # noqa: E402
    LABEL_ABSENT,
    LABEL_PRESENT,
    LABEL_UNSURE,
    Dataset,
    Sample,
)

DEFAULT_DIR = ROOT / "output" / "firmas_dataset"

def label_colors() -> dict:
    """Cada etiqueta con su color de borde, en los tonos del tema puesto ahora.

    La rejilla se lee de un vistazo y un despiste (todo un bloque marcado
    igual por error) salta a la vista.
    """
    estados = pane_status_colors()
    return {
        LABEL_PRESENT: estados["OK"],
        LABEL_ABSENT: estados["ERROR"],
        LABEL_UNSURE: estados["WARNING"],
    }
LABEL_KEYS = {
    Qt.Key.Key_F: LABEL_PRESENT,
    Qt.Key.Key_A: LABEL_ABSENT,
    Qt.Key.Key_D: LABEL_UNSURE,
    Qt.Key.Key_1: LABEL_PRESENT,
    Qt.Key.Key_2: LABEL_ABSENT,
    Qt.Key.Key_3: LABEL_UNSURE,
}

CELL_WIDTH = 380
CELL_IMAGE_HEIGHT = 130
DETAIL_HEIGHT = 190

def _qss() -> str:
    """La hoja de la herramienta, en los tonos del tema puesto ahora."""
    c = paleta()
    return f"""
QWidget {{
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 10pt;
    color: {c.PANE_TEXT};
}}
QMainWindow, QWidget#barra, QWidget#detalle {{ background-color: {c.PANE_BG}; }}
QPushButton {{
    min-height: 26px;
    padding: 4px 12px;
    background-color: {c.PANE_CONTROL_BG};
    border: 1px solid {c.PANE_BORDER};
    border-radius: 6px;
}}
QPushButton:hover {{ background-color: {c.PANE_CONTROL_HOVER}; }}
QComboBox {{
    padding: 3px 6px;
    background-color: {c.PANE_CONTROL_BG};
    border: 1px solid {c.PANE_BORDER};
    border-radius: 6px;
}}
QComboBox QAbstractItemView {{
    background-color: {c.PANE_CONTROL_BG};
    selection-background-color: {TABLE_SELECTION_BG};
}}
QLabel#contador {{ font-weight: 600; }}
QLabel#ayuda {{ color: {c.TEXT_TERTIARY}; font-size: 9pt; }}
QLabel#pie {{ color: {c.TEXT_TERTIARY}; font-size: 9pt; }}
QFrame#celda {{
    background-color: {c.PANE_SURFACE_BG};
    border: 2px solid {c.PANE_BORDER};
    border-radius: 6px;
}}
QFrame#detalleMarco {{
    background-color: {c.PANE_SURFACE_BG};
    border: 1px solid {c.PANE_BORDER};
    border-radius: 6px;
}}
QLabel#pie, QLabel#celdaTexto {{ font-size: 9pt; }}
QScrollArea {{ border: 0; background-color: {c.PANE_BG}; }}
""" + scrollbars_qss("QScrollArea")


def _draw_field_box(image: QImage, rect: List[int], scale: float = 1.0) -> QImage:
    """Marca el rectángulo real del campo sobre el recorte guardado.

    El PNG trae margen de sobra para dar contexto; sin este recuadro no se
    puede saber si la escritura que se ve cae dentro del campo o pertenece a
    la columna de al lado.
    """
    canvas = image.convertToFormat(QImage.Format.Format_RGB888)
    painter = QPainter(canvas)
    pen = QPen(QColor(TABLE_SELECTION_BG))
    pen.setWidth(max(1, round(scale)))
    painter.setPen(pen)
    x0, y0, x1, y1 = rect
    painter.drawRect(x0, y0, max(1, x1 - x0 - 1), max(1, y1 - y0 - 1))
    painter.end()
    return canvas


class CropCell(QFrame):
    """Un recorte de la rejilla, con su borde de color según la etiqueta."""

    def __init__(self, sample: Sample, pixmap: QPixmap, caption: str, parent=None):
        super().__init__(parent)
        self.setObjectName("celda")
        self.sample = sample
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(3)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setPixmap(pixmap)
        self.image_label.setFixedHeight(CELL_IMAGE_HEIGHT)
        layout.addWidget(self.image_label)
        self.text_label = QLabel(caption)
        self.text_label.setObjectName("celdaTexto")
        layout.addWidget(self.text_label)
        self.setFixedWidth(CELL_WIDTH)
        self._on_click = None

    def set_click_handler(self, handler) -> None:
        self._on_click = handler

    def mousePressEvent(self, event) -> None:  # noqa: N802 - API de Qt
        if self._on_click is not None:
            self._on_click(self.sample.id)
        super().mousePressEvent(event)

    def paint_state(self, label: Optional[str], current: bool) -> None:
        c = paleta()
        color = label_colors().get(label or "", c.PANE_BORDER)
        width = 3 if current else 2
        if current:
            self.setStyleSheet(
                f"QFrame#celda {{ background-color: {c.PANE_SURFACE_BG};"
                f" border: {width}px solid {TABLE_SELECTION_BG};"
                f" border-radius: 6px; }}"
                f"QFrame#celda QLabel {{ color: {color}; }}"
            )
        else:
            self.setStyleSheet(
                f"QFrame#celda {{ background-color: {c.PANE_SURFACE_BG};"
                f" border: {width}px solid {color};"
                f" border-radius: 6px; }}"
            )


class LabelWindow(QMainWindow):
    """Rejilla de recortes + panel de detalle, gobernada por el teclado."""

    def __init__(self, dataset: Dataset, columns: Optional[int], page_size: int):
        super().__init__()
        self.dataset = dataset
        # Sin --columnas la rejilla se reparte sola: las celdas tienen ancho
        # fijo, así que en una pantalla ancha caben más y no tiene sentido
        # dejar media ventana vacía.
        self.fixed_columns = columns is not None
        self.columns = max(1, columns or 3)
        self.page_size = max(self.columns, page_size)
        self.page_index = 0
        self.cursor = 0
        self.view: List[Sample] = []
        self.cells: Dict[str, CropCell] = {}
        self._pixmaps: Dict[str, QPixmap] = {}
        self._details: Dict[str, object] = {}
        self._verdicts: Dict[str, str] = {}
        self._undo: List[tuple] = []
        self._dirty = False

        self.setWindowTitle("Etiquetado de firmas")
        self.setStyleSheet(_qss())
        self.resize(1360, 900)
        self._build_ui()

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self._save)

        self._apply_filters()

    # -- Construcción ------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        style_dark_pane(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)
        outer.addWidget(self._build_bar())

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget()
        style_dark_pane(self.grid_host)
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(4, 4, 4, 4)
        self.grid.setSpacing(8)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self.grid_host)
        outer.addWidget(self.scroll, 1)

        outer.addWidget(self._build_detail())
        self.setCentralWidget(central)

    def _build_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("barra")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.counter = QLabel()
        self.counter.setObjectName("contador")
        layout.addWidget(self.counter)
        layout.addSpacing(12)

        # El filtro por bitácora aparece solo cuando hay más de una: con un
        # único PDF sobraría un control que siempre dice lo mismo.
        books = sorted({sample.pdf for sample in self.dataset.samples})
        self.book_filter = QComboBox()
        self.book_filter.addItem("Todas", "")
        for book in books:
            self.book_filter.addItem(book, book)
        self.book_filter.currentIndexChanged.connect(self._apply_filters)
        if len(books) > 1:
            layout.addWidget(QLabel("Bitácora"))
            layout.addWidget(self.book_filter)

        layout.addWidget(QLabel("Campo"))
        self.field_filter = QComboBox()
        self.field_filter.addItem("Todos", "")
        for field_id in sorted({sample.field_id for sample in self.dataset.samples}):
            self.field_filter.addItem(field_id, field_id)
        self.field_filter.currentIndexChanged.connect(self._apply_filters)
        layout.addWidget(self.field_filter)

        layout.addWidget(QLabel("Estado"))
        self.state_filter = QComboBox()
        for text, value in (
            ("Todas", ""),
            ("Sin etiquetar", "pendiente"),
            ("Firma", LABEL_PRESENT),
            ("Ausente", LABEL_ABSENT),
            ("Dudosa", LABEL_UNSURE),
            ("Discrepan con el detector", "discrepancia"),
        ):
            self.state_filter.addItem(text, value)
        self.state_filter.currentIndexChanged.connect(self._apply_filters)
        layout.addWidget(self.state_filter)

        self.show_verdict = QCheckBox("Mostrar veredicto del detector (V)")
        self.show_verdict.setToolTip(
            "Se calcula al vuelo con los umbrales actuales de la plantilla.\n"
            "Déjelo apagado mientras etiqueta: ver la respuesta sesga la etiqueta."
        )
        self.show_verdict.toggled.connect(self._apply_filters)
        layout.addWidget(self.show_verdict)

        layout.addStretch(1)
        self.page_label = QLabel()
        layout.addWidget(self.page_label)
        previous_page = QPushButton("◀")
        previous_page.setToolTip("Página anterior (Re Pág)")
        previous_page.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        previous_page.clicked.connect(lambda: self._change_page(-1))
        layout.addWidget(previous_page)
        next_page = QPushButton("▶")
        next_page.setToolTip("Página siguiente (Av Pág)")
        next_page.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        next_page.clicked.connect(lambda: self._change_page(1))
        layout.addWidget(next_page)
        return bar

    def _build_detail(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("detalle")
        panel.setFixedHeight(DETAIL_HEIGHT)
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        frame = QFrame()
        frame.setObjectName("detalleMarco")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(6, 6, 6, 6)
        self.detail_image = QLabel()
        self.detail_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        frame_layout.addWidget(self.detail_image)
        layout.addWidget(frame, 1)

        side = QVBoxLayout()
        side.setSpacing(6)
        self.detail_title = QLabel()
        self.detail_title.setWordWrap(True)
        side.addWidget(self.detail_title)
        self.detail_state = QLabel()
        side.addWidget(self.detail_state)
        side.addStretch(1)
        help_text = QLabel(
            "F firma · A ausente · D dudosa\n"
            "← → ↑ ↓ moverse · Retroceso quitar etiqueta\n"
            "Ctrl+Z deshacer · Re/Av Pág cambiar de página\n"
            "V ver el veredicto del detector"
        )
        help_text.setObjectName("ayuda")
        side.addWidget(help_text)
        holder = QWidget()
        holder.setFixedWidth(330)
        holder.setLayout(side)
        layout.addWidget(holder)
        return panel

    # -- Datos de la vista -------------------------------------------------

    def _verdict(self, sample: Sample) -> str:
        """Veredicto del detector con los umbrales actuales de la plantilla."""
        if sample.id not in self._verdicts:
            from tools.signature_labeling.evaluate import verdict_for_sample

            self._verdicts[sample.id] = verdict_for_sample(self.dataset, sample)
        return self._verdicts[sample.id]

    def _matches(self, sample: Sample) -> bool:
        book = self.book_filter.currentData()
        if book and sample.pdf != book:
            return False
        field_id = self.field_filter.currentData()
        if field_id and sample.field_id != field_id:
            return False
        state = self.state_filter.currentData()
        label = self.dataset.labels.get(sample.id)
        if state == "pendiente":
            return label is None
        if state == "discrepancia":
            if label is None:
                return False
            from tools.signature_labeling.dataset import EXPECTED_VALUE

            return self._verdict(sample) != EXPECTED_VALUE.get(label)
        if state:
            return label == state
        return True

    def _apply_filters(self) -> None:
        current_id = (
            self.view[self.cursor].id
            if 0 <= self.cursor < len(self.view) else None
        )
        self.view = [s for s in self.dataset.samples if self._matches(s)]
        self.cursor = 0
        if current_id is not None:
            for index, sample in enumerate(self.view):
                if sample.id == current_id:
                    self.cursor = index
                    break
        self.page_index = self.cursor // self.page_size if self.view else 0
        self._rebuild_grid()

    # -- Rejilla -----------------------------------------------------------

    def _marked_pixmap(self, sample: Sample) -> QPixmap:
        """Recorte a tamaño real con el recuadro del campo dibujado."""
        image = QImage(str(self.dataset.crop_path(sample)))
        if image.isNull():
            return QPixmap()
        return QPixmap.fromImage(_draw_field_box(image, sample.rect))

    def _thumbnail(self, sample: Sample) -> QPixmap:
        """Miniatura de la rejilla (se guarda: se repinta en cada filtro)."""
        if sample.id not in self._pixmaps:
            self._pixmaps[sample.id] = self._marked_pixmap(sample).scaled(
                CELL_WIDTH - 16, CELL_IMAGE_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return self._pixmaps[sample.id]

    def _detail_pixmap(self, sample: Sample) -> QPixmap:
        """Recorte ampliado al tamaño del panel.

        No se guarda en memoria a tamaño real: los campos anchos ocupan casi
        medio megabyte cada uno y solo se mira uno a la vez. Se conserva el
        último por si el panel se repinta sin cambiar de recorte.
        """
        available = self.detail_image.size()
        key = (sample.id, available.width(), available.height())
        if self._details.get("clave") != key:
            self._details = {
                "clave": key,
                "pixmap": self._marked_pixmap(sample).scaled(
                    max(1, available.width()), max(1, available.height()),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ),
            }
        return self._details["pixmap"]

    def _caption(self, sample: Sample) -> str:
        text = f"{sample.pdf} · pág. {sample.page} · {sample.field_id}"
        if sample.alignment != "ok":
            text += "  ⚠ alineación"
        if self.show_verdict.isChecked():
            text += f"  ›  detector: {self._verdict(sample)}"
        return text

    def _rebuild_grid(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        self.cells.clear()

        start = self.page_index * self.page_size
        for position, sample in enumerate(self.view[start:start + self.page_size]):
            cell = CropCell(
                sample, self._thumbnail(sample), self._caption(sample),
            )
            cell.set_click_handler(self._select_by_id)
            self.grid.addWidget(
                cell, position // self.columns, position % self.columns
            )
            self.cells[sample.id] = cell
        self._refresh_states()

    def _refresh_states(self) -> None:
        current = (
            self.view[self.cursor].id
            if 0 <= self.cursor < len(self.view) else None
        )
        for sample_id, cell in self.cells.items():
            cell.paint_state(
                self.dataset.labels.get(sample_id), sample_id == current
            )
        counts = self.dataset.counts()
        self.counter.setText(
            f"{counts['etiquetadas']}/{counts['total']} etiquetadas   ·   "
            f"firma {counts[LABEL_PRESENT]}   ausente {counts[LABEL_ABSENT]}   "
            f"dudosa {counts[LABEL_UNSURE]}"
        )
        pages = max(1, (len(self.view) + self.page_size - 1) // self.page_size)
        self.page_label.setText(
            f"{len(self.view)} en la vista · página {self.page_index + 1}/{pages}"
        )
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        if not (0 <= self.cursor < len(self.view)):
            self.detail_image.clear()
            self.detail_title.setText("Nada que mostrar con este filtro")
            self.detail_state.clear()
            return
        sample = self.view[self.cursor]
        self.detail_image.setPixmap(self._detail_pixmap(sample))
        self.detail_title.setText(
            f"{sample.pdf} · página {sample.page}\n{sample.field_id}"
            + ("" if sample.alignment == "ok"
               else f"\nalineación: {sample.alignment}")
        )
        label = self.dataset.labels.get(sample.id)
        color = label_colors().get(label or "", paleta().TEXT_TERTIARY)
        text = label or "sin etiquetar"
        if self.show_verdict.isChecked():
            text += f"   ·   detector: {self._verdict(sample)}"
        self.detail_state.setText(f"<b style='color:{color}'>{text}</b>")

    # -- Interacción -------------------------------------------------------

    def _select_by_id(self, sample_id: str) -> None:
        for index, sample in enumerate(self.view):
            if sample.id == sample_id:
                self.cursor = index
                self._refresh_states()
                return

    def _move_cursor(self, delta: int) -> None:
        if not self.view:
            return
        self.cursor = max(0, min(len(self.view) - 1, self.cursor + delta))
        page = self.cursor // self.page_size
        if page != self.page_index:
            self.page_index = page
            self._rebuild_grid()
        else:
            self._refresh_states()
            self._ensure_visible()

    def _ensure_visible(self) -> None:
        if not (0 <= self.cursor < len(self.view)):
            return
        cell = self.cells.get(self.view[self.cursor].id)
        if cell is not None:
            self.scroll.ensureWidgetVisible(cell, 0, 40)

    def _change_page(self, delta: int) -> None:
        pages = max(1, (len(self.view) + self.page_size - 1) // self.page_size)
        page = max(0, min(pages - 1, self.page_index + delta))
        if page == self.page_index:
            return
        self.page_index = page
        self.cursor = min(len(self.view) - 1, page * self.page_size)
        self._rebuild_grid()

    def _set_label(self, label: Optional[str]) -> None:
        if not (0 <= self.cursor < len(self.view)):
            return
        sample = self.view[self.cursor]
        self._undo.append((sample.id, self.dataset.labels.get(sample.id)))
        if label is None:
            self.dataset.labels.pop(sample.id, None)
        else:
            self.dataset.labels[sample.id] = label
        self._touch()
        # Avanzar solo al etiquetar: al borrar se sigue mirando el mismo
        # recorte, que es lo que se quiere cuando uno se corrige.
        if label is not None and self.cursor < len(self.view) - 1:
            self._move_cursor(1)
        else:
            self._refresh_states()

    def _undo_last(self) -> None:
        if not self._undo:
            return
        sample_id, previous = self._undo.pop()
        if previous is None:
            self.dataset.labels.pop(sample_id, None)
        else:
            self.dataset.labels[sample_id] = previous
        self._select_by_id(sample_id)
        self._touch()
        self._refresh_states()

    def _touch(self) -> None:
        self._dirty = True
        self._save_timer.start()

    def _save(self) -> None:
        if not self._dirty:
            return
        self.dataset.save_labels()
        self._dirty = False

    def keyPressEvent(self, event) -> None:  # noqa: N802 - API de Qt
        key = event.key()
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_Z:
                self._undo_last()
                return
            if key == Qt.Key.Key_S:
                self._save()
                return
        if key in LABEL_KEYS:
            self._set_label(LABEL_KEYS[key])
            return
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete, Qt.Key.Key_0):
            self._set_label(None)
            return
        if key == Qt.Key.Key_V:
            self.show_verdict.toggle()
            return
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Space):
            self._move_cursor(1)
            return
        if key == Qt.Key.Key_Left:
            self._move_cursor(-1)
            return
        if key == Qt.Key.Key_Down:
            self._move_cursor(self.columns)
            return
        if key == Qt.Key.Key_Up:
            self._move_cursor(-self.columns)
            return
        if key == Qt.Key.Key_PageDown:
            self._change_page(1)
            return
        if key == Qt.Key.Key_PageUp:
            self._change_page(-1)
            return
        if key == Qt.Key.Key_Home:
            self._move_cursor(-len(self.view))
            return
        if key == Qt.Key.Key_End:
            self._move_cursor(len(self.view))
            return
        super().keyPressEvent(event)

    def _auto_columns(self) -> int:
        """Cuántas celdas caben de ancho ahora mismo."""
        if self.fixed_columns:
            return self.columns
        usable = self.scroll.viewport().width() - 8
        return max(1, usable // (CELL_WIDTH + self.grid.spacing()))

    def _refresh_columns(self) -> None:
        columns = self._auto_columns()
        if columns != self.columns:
            self.columns = columns
            self._rebuild_grid()

    def showEvent(self, event) -> None:  # noqa: N802 - API de Qt
        # Al construir la ventana el área de desplazamiento todavía no tiene
        # su ancho definitivo; el reparto en columnas se decide cuando ya lo
        # tiene, en cuanto Qt termina el primer trazado.
        super().showEvent(event)
        QTimer.singleShot(0, self._refresh_columns)

    def resizeEvent(self, event) -> None:  # noqa: N802 - API de Qt
        super().resizeEvent(event)
        self._refresh_columns()

    def closeEvent(self, event) -> None:  # noqa: N802 - API de Qt
        self._save()
        super().closeEvent(event)


def main() -> int:
    parser = argparse.ArgumentParser(description="Etiquetado manual de firmas")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                        help="carpeta de trabajo creada por extract.py")
    parser.add_argument(
        "--columnas", type=int,
        help="columnas de la rejilla (por defecto, las que quepan)",
    )
    parser.add_argument("--por-pagina", type=int, default=60, dest="page_size",
                        help="recortes por página de la rejilla")
    args = parser.parse_args()

    try:
        dataset = Dataset.load(args.dir)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not dataset.samples:
        print(f"El manifiesto de {args.dir} está vacío.", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName("Etiquetado de firmas")
    icon_path = ROOT / "assets" / "icon.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = LabelWindow(dataset, args.columnas, args.page_size)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
