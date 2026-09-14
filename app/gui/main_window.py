"""Ventana principal de BITS.

Permite seleccionar los archivos (PDFs) y la plantilla, procesar el batch
completo con la configuración recomendada y definir las salidas
(discrepancia, PDFs por matrícula/mes, visualizar campos),
con barra de progreso, tiempos estimados/transcurridos y tiempo por
archivo. Los resultados OCR se pueden exportar de nuevo con otra separación
sin volver a ejecutar el procesamiento.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from loguru import logger
from PySide6.QtCore import (
    QObject,
    QProcess,
    QRectF,
    QSignalBlocker,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImage,
    QIntValidator,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.branding import APPLICATION_DISPLAY_NAME
from app.core.config import AppConfig
from app.core.page_range import FileSlice, PageRange, slice_batch, total_pages
from app.core.parallelism import available_cpu_threads, recommended_parallelism
from app.core.progress import with_page_counter
from app.gui.csv_utils import csv_display_name, template_field_ids_for_columns
from app.gui.csv_viewer import (
    CsvColumnModeButton,
    CsvViewerWindow,
    apply_csv_column_visibility,
)
from app.gui.cronometro import Cronometro, cronometro_qss, formato_reloj
from app.gui.eta import estimate_remaining_seconds, wall_ms_per_page
from app.gui.export_options import ExportOptionsGroup
from app.gui.field_selector import ImportantFieldsDialog
from app.gui.fleet_editor import FLEET_FILENAME, FleetEditorDialog, FleetStore
from app.gui.responsive import (
    COMPACT,
    COMPACT_HEIGHT,
    ROOMY,
    Density,
    density_for,
    fit_to_screen,
)
from app.gui.table_sort import ColumnSortController
from app.gui.airvault_window import AIRVAULT_TOOLTIP, AirVaultWindow
from app.gui.web_reports_window import (
    WEB_REPORTS_TOOLTIP,
    WebReportsWindow,
)
from app.gui import automatizacion as pasos_automaticos
from app.gui.automatizacion import (
    CadenaAutomatica,
    MenuAutomatizacion,
    OpcionesAutomatizacion,
)
from app.gui.csv_model import STATUS_COLORS as CSV_STATUS_COLORS
from app.gui.depuracion_dialog import DEPURAR_TOOLTIP, DepurarPaginasDialog
from app.gui.theme import aplicar_tema, gestor_tema
from app.gui.tokens import (
    CONTROL_HEIGHT,
    TEMA_CLARO,
    TEMA_OSCURO,
    CONTROL_HEIGHT_COMPACT,
    FONT_CAPTION_PT,
    FONT_SUBTITLE_PT,
    RADIUS_CARD,
    WEIGHT_STRONG,
    paleta,
    qss_vars,
    tema,
)
from app.gui.widgets import (
    ICON_SIZE,
    TABLE_RADIUS,
    ElidedLabel,
    MultiSelectMenu,
    ZoomableScrollArea,
    ZoomOverlay,
    al_cambiar_tema,
    configure_combo_box,
    configure_menu_button,
    data_table_qss,
    hide_overlay_when_tight,
    keep_overlay_clear_of_scrollbars,
    load_icon,
    pintar_del_tema,
    style_data_table,
    style_pdf_surface,
    window_stylesheet,
)
from app.gui.worker import OutputsWorker, PipelineWorker, PreprocessWorker
from app.models.schemas import PageResult, Status, ValidationReport
from app.reports.csv_reporter import CSV_DATE_MONTH_END, CsvReporter
from app.reports.json_reporter import JsonReporter
from app.templates.manager import TemplateManager
from app.templates.schema import Template
from app.utils.important_fields import (
    IMPORTANT_FIELDS_FILENAME,
    ImportantFieldsStore,
    default_important_columns,
)
from app.utils.io import (
    PROCESSED_DIRNAME,
    archive_processed_files,
    ensure_dir,
    send_to_trash,
)
from app.validation.depuracion import depurar_claves
from app.validation.duplicates import DuplicateLogPage, detect_duplicate_log_pages

SCRIPT_DIR = Path(__file__).resolve().parents[2]
PERF_CACHE = SCRIPT_DIR / "output" / ".performance.json"
_DEFAULT_MS_PER_PAGE = 2500.0  # costo nominal antes de la primera ejecución
_DEFAULT_REFERENCE_PAGE = 1
_DEFAULT_DESKEW = True
_DEFAULT_ALIGN = True
_DEFAULT_CROP_PREPROCESS = True

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Celdas (no filas) por tick del QTimer. El costo de llenar la tabla es por
# celda (medido: ~16 µs cada una), así que un presupuesto en filas escala con
# el número de columnas: 400 filas del CSV completo son 34.000 celdas, medio
# segundo de interfaz bloqueada por tick. Con un presupuesto en celdas cada
# tick cuesta lo mismo tenga la ejecución 3 columnas o 90.
_TABLE_CELL_CHUNK = 2000
# Espera entre comprobaciones mientras se detiene el trabajo para cerrar.
_SHUTDOWN_POLL_MS = 150
_PISTA_BUSQUEDA = "La búsqueda abre cada coincidencia en la vista previa."
_PREVIEW_EMPTY_MESSAGE = "Seleccione un archivo PDF para ver la vista previa"
# Lo que se espera a un hilo despues de romperle el pool por debajo. Con el
# pool roto la espera real es de milisegundos; el margen es para el hilo que
# estuviera escribiendo en disco justo en ese instante.
_CORTE_ESPERA_MS = 4000
# Reparto del ancho entre la vista previa y la tabla de resultados. La página
# de una bitácora es vertical y se lee entera o no se lee: pesa lo mismo que
# la tabla, que además se puede recorrer de lado.
_PREVIEW_SHARE = 1
_RESULTS_SHARE = 1

# Pausa tras el último evento de tamaño antes de reescalar la vista previa.
# Arrastrar el borde emite un evento por píxel y cada uno reescalaba la página
# entera con interpolación suave; así se reescala una vez al soltar.
_PREVIEW_RESIZE_MS = 80
_DUP_COLUMN = "dup"
_DISC_COLUMN = "disc"
_REVIEW_COLUMN = "review"
# Tamaño con el que se pide abrir la ventana principal. No es una promesa: la
# pantalla manda y ``fit_to_screen`` lo recorta a lo que haya de sitio.
_PREFERRED_WIDTH = 1280
_PREFERRED_HEIGHT = 900
# Columnas de una fila del panel de avance. El nombre lo fija la densidad
# (es lo único que se aprieta en pantallas bajas); el resto son medidas de
# texto que no dan de sí, y juntas son el ancho mínimo del panel: por debajo
# aparecería un desplazamiento lateral dentro de la propia fila.
_BAR_MIN_WIDTH = 140
_PAGES_COLUMN_WIDTH = 86
_SECS_COLUMN_WIDTH = 70
_FILE_ROW_SPACING = 8
# Alto de una fila del panel más el marco de la lista: es hasta donde puede
# encogerse la parte que se desplaza, y por debajo no se vería ni un archivo.
_TIMES_SCROLL_MIN_HEIGHT = 26
# El selector conserva su ancho habitual en pantallas holgadas. En la
# densidad compacta cede apenas lo necesario para que «Entrada» y «Salidas»
# entren en dos columnas dentro de un escritorio lógico de 1280 px.
_TEMPLATE_MIN_WIDTH = 200
_COMPACT_TEMPLATE_MIN_WIDTH = 180
def _preview_empty_html() -> str:
    """El rótulo de la vista previa vacía, en los tonos del tema de ahora.

    Va en HTML y no en la hoja de estilo porque son dos líneas con pesos
    distintos dentro de una sola etiqueta, así que el color viaja con el
    texto: se vuelve a armar cada vez que se pide.
    """
    c = paleta()
    return (
        f'<div style="font-size:{FONT_SUBTITLE_PT}pt; font-weight:600; '
        f'color:{c.TEXT};">Vista previa</div>'
        f'<div style="margin-top:8px; color:{c.TEXT_SECONDARY};">'
        "Seleccione un archivo PDF para comenzar.</div>"
    )


_COLORS = {
    Status.OK: CSV_STATUS_COLORS["OK"],
    Status.WARNING: CSV_STATUS_COLORS["WARNING"],
    Status.ERROR: CSV_STATUS_COLORS["ERROR"],
}


def _visible_preview_fields(
    template: Template,
    important_only: bool,
    important_ids: set[str] | None = None,
):
    """Campos que debe pintar el visor.

    Sin ``important_only`` se dibuja la plantilla completa. Con la vista
    simplificada manda ``important_ids``: los campos marcados en el selector
    de columnas importantes, incluidas las que la plantilla no declara
    ``required``. Solo cuando todavía no hay ninguna selección se recurre a
    ``required``, que es la importancia declarada por la propia plantilla.
    """
    if not important_only:
        return list(template.fields)
    if important_ids is None:
        return [field for field in template.fields if field.required]
    return [field for field in template.fields if field.id in important_ids]

# Los rótulos secundarios de la ventana. Todo en puntos: los «10px» que había
# aquí no seguían el escalado de Windows, así que en un monitor al 150 % el
# panel de tiempos se encogía mientras el resto de la ventana crecía.
_WINDOW_QSS_PLANTILLA = """
QLabel#previewPlaceholder {{
    color: {TEXT_SECONDARY};
    background-color: transparent;
    border: 1px solid {STROKE};
    border-radius: {TABLE_RADIUS}px;
}}
QWidget#fileProgressPane {{
    background-color: {TABLE_BASE_BG};
    border: 1px solid {STROKE};
    border-radius: {TABLE_RADIUS}px;
}}
QWidget#fileProgressPane QLabel {{
    border: 0;
    background-color: transparent;
}}
QScrollArea#fileProgressScroll,
QWidget#fileProgressViewport,
QWidget#fileProgressList {{
    background-color: {TABLE_BASE_BG};
    border: 0;
}}
QWidget#previewContext, QLabel#previewContext {{
    color: {TEXT_SECONDARY};
    font-weight: {WEIGHT_STRONG};
    padding: 4px 2px;
}}
#fileProgressPane QLabel#previewContext {{
    font-weight: 400;
    padding: 0;
}}
QLabel#fileProgressTitle {{
    font-weight: {WEIGHT_STRONG};
}}
QProgressBar#timeBar {{
    min-height: 18px;
    max-height: 18px;
    background-color: {CONTROL_BG};
    font-size: {FONT_CAPTION_PT}pt;
    font-weight: {WEIGHT_STRONG};
    color: {TEXT};
    border-radius: 4px;
}}
QProgressBar#timeBar::chunk {{
    border-radius: 3px;
}}
#filePages {{ color: {TEXT_SECONDARY}; }}
QLabel#fileProgressEmpty {{
    color: {TEXT_SECONDARY};
    padding: 12px;
}}
QToolButton#themeToggle {{
    padding: 0;
}}
"""


def _window_qss() -> str:
    """Los rótulos propios de esta ventana, en los tonos del tema de ahora.

    Va con ``format`` y no con una f de literal porque la plantilla se escribe
    una vez y se rellena cada vez que cambia el tema; escrita como f se habría
    resuelto al importar el módulo y se habría quedado con los grises de ese
    momento.
    """
    return (
        # ``TABLE_RADIUS`` es el radio de los controles con el nombre que usa
        # la tabla; vive en ``widgets`` y no en la paleta, así que se le pasa
        # aparte en vez de colarlo entre los colores.
        _WINDOW_QSS_PLANTILLA.format(TABLE_RADIUS=TABLE_RADIUS, **qss_vars())
        + cronometro_qss()
        + data_table_qss()
    )


class ResultsTableWidget(QTableWidget):
    """Tabla que explica su estado inicial sin agregar otro control."""

    def paintEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().paintEvent(event)
        if self.rowCount() != 0:
            return
        body = "Los resultados aparecerán aquí después de procesar."
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        area = self.viewport().rect().adjusted(24, 24, -24, -24)

        title_font = QFont(self.font())
        title_font.setPointSize(FONT_SUBTITLE_PT)
        title_font.setWeight(QFont.Weight.DemiBold)
        body_font = QFont(self.font())
        painter.setFont(title_font)
        title_height = painter.fontMetrics().height()
        painter.setFont(body_font)
        body_height = painter.boundingRect(
            QRectF(0, 0, area.width(), 100),
            Qt.AlignmentFlag.AlignHCenter
            | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            body,
        ).height()
        gap = 8
        block_height = title_height + gap + body_height
        top = area.center().y() - block_height // 2

        title_rect = QRectF(area.left(), top, area.width(), title_height)
        painter.setFont(title_font)
        painter.setPen(QColor(paleta().TEXT))
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            "Resultados",
        )

        body_rect = QRectF(
            area.left(),
            top + title_height + gap,
            area.width(),
            body_height,
        )
        painter.setFont(body_font)
        painter.setPen(QColor(paleta().TEXT_SECONDARY))
        painter.drawText(
            body_rect,
            Qt.AlignmentFlag.AlignHCenter
            | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            body,
        )


def _format_duration(seconds: float) -> str:
    """Formatea una duración de forma limpia: s, min u horas y minutos."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{round(seconds)} s"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{int(minutes)} min"
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    return f"{hours} h {mins} min"


# Cuánto pesa una hoja de la comprobación de VOID frente a una página leída, y
# qué parte de las páginas llega a esa comprobación. Son valores de arranque:
# la primera ejecución completa los sustituye por los medidos en el equipo.
_DEFAULT_REVIEW_WEIGHT = 1.5
_DEFAULT_REVIEW_RATIO = 0.6


def _load_performance() -> dict:
    try:
        import json

        with open(PERF_CACHE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - caché opcional
        return {}


def _load_ms_per_page() -> float:
    """Costo por unidad de página (ms) aprendido de la última ejecución.

    Una unidad es una página leída; cada hoja de la comprobación de VOID
    cuenta lo que pesa frente a ella (ver ``_load_review_model``).
    """
    try:
        value = float(
            _load_performance().get("ms_per_page", _DEFAULT_MS_PER_PAGE)
        )
        return min(max(value, 100.0), 60000.0)
    except (TypeError, ValueError):
        return _DEFAULT_MS_PER_PAGE


def _load_review_model() -> tuple[float, float]:
    """Peso de una hoja de VOID y proporción de páginas que la necesitan."""
    data = _load_performance()
    try:
        weight = float(data.get("review_weight", _DEFAULT_REVIEW_WEIGHT))
        ratio = float(data.get("review_ratio", _DEFAULT_REVIEW_RATIO))
    except (TypeError, ValueError):
        return _DEFAULT_REVIEW_WEIGHT, _DEFAULT_REVIEW_RATIO
    return min(max(weight, 0.1), 50.0), min(max(ratio, 0.0), 1.0)


def _save_performance(
    ms_per_page: float, review_weight: float, review_ratio: float
) -> None:
    try:
        import json

        PERF_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(PERF_CACHE, "w", encoding="utf-8") as fh:
            json.dump({
                "ms_per_page": round(ms_per_page, 1),
                "review_weight": round(review_weight, 3),
                "review_ratio": round(review_ratio, 3),
            }, fh)
    except Exception as exc:  # noqa: BLE001 - no crítico
        logger.warning(f"No se pudo guardar el cálculo de rendimiento: {exc}")


def _layout_spacing(layout: QLayout) -> int:
    """Separación entre filas del layout, sea rejilla o pila."""
    if isinstance(layout, QGridLayout):
        return layout.verticalSpacing()
    return layout.spacing()


def _layout_column_spacing(layout: QLayout) -> int:
    """Separación entre columnas; solo la tienen las rejillas."""
    if isinstance(layout, QGridLayout):
        return layout.horizontalSpacing()
    return layout.spacing()


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        else:
            sub = item.layout()
            if sub is not None:
                _clear_layout(sub)


def _load_app_icon() -> QIcon:
    """Icono de la aplicación: .ico (multi-tamaño, ideal en Windows) o PNG."""
    assets = SCRIPT_DIR / "assets"
    for name in ("icon.ico", "icon.png"):
        path = assets / name
        if path.is_file():
            return QIcon(str(path))
    return QIcon()


class QtLogSink(QObject):
    """Sink de Loguru que reenvía mensajes a la GUI vía señal."""

    message = Signal(str)

    def __call__(self, msg) -> None:
        self.message.emit(str(msg))


class PreviewLoader(QObject):
    """Renderiza páginas de vista previa en su propia QThread.

    La GUI pide páginas con ``requested`` y recibe el resultado en
    ``previewReady`` sin bloquear el hilo de interfaz.
    """

    requested = Signal(int, str, object)
    previewReady = Signal(int, str, object)

    def run(
        self, page_number: int, pdf_path: str, geometry: dict | None = None
    ) -> None:
        import cv2

        from app.utils.io import resolve_processed_path
        from app.vision.alignment import TransformResult, warp_with_transform
        from app.vision.pdf_loader import render_page
        from app.vision.preprocessing import rotate

        try:
            image = render_page(
                resolve_processed_path(Path(pdf_path)), page_number, dpi=150
            )
            if geometry:
                skew_angle = float(geometry.get("skew_angle", 0.0))
                if abs(skew_angle) > 0.0:
                    image = rotate(image, skew_angle)
                alignment = geometry.get("alignment")
                if alignment:
                    height, width = image.shape[:2]
                    target_width = round(
                        width * float(alignment.get("target_width_ratio", 1.0))
                    )
                    target_height = round(
                        height * float(alignment.get("target_height_ratio", 1.0))
                    )
                    image = warp_with_transform(
                        image,
                        TransformResult(
                            rot=float(alignment.get("rot", 0.0)),
                            tx=float(alignment.get("tx_ratio", 0.0)) * width,
                            ty=float(alignment.get("ty_ratio", 0.0)) * height,
                            scale=float(alignment.get("scale", 1.0)),
                        ),
                        (max(1, target_width), max(1, target_height)),
                    )
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qimage = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
            self.previewReady.emit(page_number, pdf_path, qimage)
        except Exception as exc:  # noqa: BLE001 - vista previa no crítica
            logger.warning(
                f"Vista previa no disponible ({pdf_path} p. {page_number}): {exc}"
            )
            self.previewReady.emit(page_number, pdf_path, None)


class MainWindow(QMainWindow):
    """Ventana principal de la aplicación."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APPLICATION_DISPLAY_NAME)
        # La ventana se abre con lo que dé la pantalla, no con una medida
        # fija: en un portátil de 1366x768 el alto que pedía no existe y la
        # franja de abajo se quedaba fuera del escritorio. La densidad que
        # devuelve es la de ese alto y hay que tenerla antes de construir
        # nada, porque de ella salen todos los márgenes.
        self._density = fit_to_screen(self, _PREFERRED_WIDTH, _PREFERRED_HEIGHT)
        self._controls_columns = 0
        self._density_layouts: list[tuple] = []
        # Ancho a partir del cual caben dos columnas de cuadros. Se mide con
        # la ventana ya construida; hasta entonces vale el que se pidió, que
        # solo decide el reparto con el que se dibuja la primera vez.
        self._two_column_width = _PREFERRED_WIDTH
        self._stacked_minimum = QSize(0, 0)
        self._side_by_side_minimum = QSize(0, 0)
        # Alto que pide el reparto holgado. Hasta medirlo vale el umbral de
        # siempre, que es el que decide la densidad con la que se construye.
        self._roomy_minimum = QSize(0, COMPACT_HEIGHT)
        # Mientras se mide el reparto compacto la ventana cambia de medidas
        # varias veces; los eventos de tamaño que eso provoca no deben
        # volver a entrar a decidir nada.
        self._measuring_layout = False
        self._shown_once = False
        self._apply_density_stylesheet()
        self.setWindowIcon(_load_app_icon())

        self._pdf_paths: list[Path] = []
        self._template_path: Path | None = None
        self._reports: list[ValidationReport] | None = None
        self._worker: PipelineWorker | None = None
        self._preprocess_worker: PreprocessWorker | None = None
        self._outputs_worker: OutputsWorker | None = None
        self._outputs_context: str | None = None
        self._corrida_dir: Path | None = None
        self._pending_export = False
        # Los pasos que encadena «Automático». Se leen del JSON portable al
        # arrancar y los comparten las ventanas de AirVault que se abran.
        self._automatizacion = OpcionesAutomatizacion(SCRIPT_DIR, self)
        # Si la ejecución en curso la lanzó ese botón. Es lo único que
        # distingue una exportación normal de un eslabón de la cadena.
        self._auto_en_marcha = False
        self._pending_csv_refresh = False
        self._preview_page = 1
        self._preview_total = 0
        self._preview_pdf: Path | None = None
        self._preview_documents: list[Path] = []
        self._preview_document_counts: list[int] = []
        self._preview_document_keys: list[str] = []
        self._row_pdfs: list[Path] = []
        self._preview_source_pixmap: QPixmap | None = None
        # Render sin recuadros: permite repintar el overlay al cambiar la
        # selección de campos sin volver a rasterizar la página.
        self._preview_base_image: QImage | None = None
        self._preview_zoom = 1.0  # 1.0 = ajustado a la altura disponible
        # Geometria de preprocesado por pagina (deskew + alineacion), no
        # la imagen: ver PreprocessWorker.
        self._preprocess_geometry: dict[tuple[str, int], dict] = {}
        self._preprocessed_active = False
        self._log_sink = QtLogSink()
        self._log_handler_id: int | None = None
        self._processed_template: Template | None = None
        self._template_cache: tuple[Path, int, Template] | None = None
        self._processed_dpi: int | None = None
        self._last_run_cancelled = False

        self._detected_dpi = 200
        self._detected_dpis: dict[str, int] = {}
        # Las opciones que antes se mostraban como avanzadas quedan fijas en
        # sus valores recomendados.
        self._selected_threads = available_cpu_threads()
        self._reference_page = _DEFAULT_REFERENCE_PAGE
        # Páginas de cada PDF de la entrada, alineado con ``_pdf_paths``: es
        # lo que convierte el rango global del batch en tramos por archivo.
        self._input_page_counts: list[int] = []
        # Lectura de la entrada en curso. Va en un hilo porque abre todos los
        # PDFs de ``input/``, y la generación distingue el recorrido vigente
        # del de unos archivos que ya se cambiaron.
        self._input_scan_worker: QThread | None = None
        self._input_scan_generation = 0
        self._input_scanning = False
        self._config: AppConfig | None = None
        self._ms_per_page = _load_ms_per_page()
        self._review_weight, self._review_ratio = _load_review_model()

        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._on_timer_tick)
        self._run_started: float | None = None
        self._done_global = 0
        self._total_global = 0
        self._last_done = 0
        # Comprobación de VOID por archivo (1-based): hojas revisadas, hojas
        # por revisar y archivos ya cerrados. ``_review_reserved`` solo se
        # enciende al procesar: reserva en la barra y en el restante la parte
        # prevista de los archivos que todavía no llegaron a esa etapa.
        self._review_done: dict[int, int] = {}
        self._review_total: dict[int, int] = {}
        self._files_closed: set[int] = set()
        self._file_pages_done: dict[int, int] = {}
        self._file_pages_total: dict[int, int] = {}
        self._review_reserved = False
        self._last_bar = 0
        self._spinner_idx = 0
        self._spinner_active = False

        self._log_buffer: list[str] = []
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(300)
        self._log_timer.timeout.connect(self._flush_log_buffer)
        self._log_timer.start()

        self._table_timer = QTimer(self)
        # Intervalo cero: cada tramo se ejecuta cuando la cola de eventos queda
        # vacía, así que la ventana atiende clics y repintados entre uno y otro
        # en vez de acumular 120 ms de espera por tramo.
        self._table_timer.setInterval(0)
        self._table_timer.timeout.connect(self._on_table_chunk)
        self._table_columns: list[str] = []
        self._table_pending: list = []
        # Lo que cada tramo necesita para armar sus filas. Se fija al empezar
        # el llenado y no cambia mientras dura, para que todas las filas de
        # una ejecución salgan con el mismo criterio.
        self._table_reporter = CsvReporter()
        self._table_fields: list[str] = []
        self._table_time_factor: float = 1.0
        self._table_date_mode: str = CSV_DATE_MONTH_END
        self._table_important_field_ids: set[str] = set()
        self._selected_important_columns: set[str] = set()
        self._important_fields_user_selected = False
        self._important_fields_store = ImportantFieldsStore(
            SCRIPT_DIR / IMPORTANT_FIELDS_FILENAME
        )
        self._csv_viewer: CsvViewerWindow | None = None
        # La ventana del indexado nace cuando se pide; hasta entonces la
        # ejecución que le tocaría se guarda aquí, para que abrirla después de
        # exportar la encuentre ya elegida.
        self._airvault_window: AirVaultWindow | None = None
        self._airvault_windows: list[AirVaultWindow] = []
        self._airvault_corrida: Path | None = None
        self._web_reports_window: WebReportsWindow | None = None

        self._preview_thread = QThread(self)
        self._preview_loader = PreviewLoader()
        self._preview_loader.moveToThread(self._preview_thread)
        self._preview_loader.requested.connect(self._preview_loader.run)
        self._preview_loader.previewReady.connect(self._on_preview_ready)
        self._preview_thread.finished.connect(self._preview_loader.deleteLater)
        self._preview_thread.start()
        self._preview_pending: tuple[int, str] | None = None
        self._preview_results: dict[tuple[str, int], object] = {}

        # Cierre ordenado: destruir un QThread en marcha aborta el proceso, así
        # que la ventana pide la parada y espera sin bloquear la interfaz.
        self._closing = False
        # Coincidencias de la búsqueda, como filas mostradas de la tabla y la
        # columna donde apareció el texto. Se guardan por fila mostrada
        # porque es la tabla la que se busca, no una lista aparte: así lo que
        # se encuentra es exactamente lo que se está viendo.
        self._coincidencias: list[tuple[int, int]] = []
        self._coincidencia = -1
        self._buscado = ""
        # Se pidio cortar por lo sano: ni el cierre ni la cancelacion
        # esperan ya a que terminen las paginas en vuelo.
        self._forzado = False
        self._cancel_pedido = False
        self._torn_down = False
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(_SHUTDOWN_POLL_MS)
        self._shutdown_timer.timeout.connect(self._on_shutdown_tick)

        # Reescalado de la vista previa tras un arrastre: un solo repintado
        # cuando el borde se queda quieto, no uno por evento de tamaño.
        self._resize_preview_timer = QTimer(self)
        self._resize_preview_timer.setSingleShot(True)
        self._resize_preview_timer.setInterval(_PREVIEW_RESIZE_MS)
        self._resize_preview_timer.timeout.connect(self._render_preview_pixmap)
        self._responsive_timer = QTimer(self)
        self._responsive_timer.setSingleShot(True)
        self._responsive_timer.setInterval(40)
        self._responsive_timer.timeout.connect(self._finish_resize_layout)

        self._bottom_splitter_adjusted = False
        self._content_splitter_adjusted = False
        self._file_rows: dict[int, dict] = {}
        self._row_ms: dict[int, float] = {}
        self._row_started: dict[int, float] = {}
        self._current_file_index = 0
        self._file_page_counts: list[int] = []

        self._build_ui()
        # El estilo ya esta instalado antes de construir la ventana. Pulir
        # aqui deja definitivas las metricas de los controles y evita medir
        # y reaplicar las dos densidades una segunda vez al mostrarla.
        self.ensurePolished()
        self._refresh_minimum_size()
        self._grow_to_fit_content()
        # Si la aplicación termina sin pasar por el cierre de la ventana
        # (cierre de sesión, ``quit()`` desde otro sitio), los hilos se paran
        # igual: destruirlos en marcha aborta el proceso.
        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self._teardown)
        self._attach_logger()
        self._refresh_templates()
        self._restore_important_columns()
        self._install_zoom_shortcuts()
        # La hoja de la ventana se compone con su fragmento de densidad, así
        # que no puede rehacerla el módulo del tema: la vuelve a pedir ella
        # misma cuando el tema cambia. Qt desconecta sola al cerrarse.
        gestor_tema().cambiado.connect(self._on_tema_cambiado)

    def load_initial_data(self) -> None:
        """Carga los datos del disco después de mostrar la ventana."""
        self._load_default_input()

    def _on_tema_cambiado(self, nombre: str) -> None:
        """Rehace lo de esta ventana que no alcanza la hoja de la aplicación.

        Son las cuatro cosas que esta ventana pinta por su cuenta: su propia
        hoja, el rótulo de la vista previa vacía (que lleva el color dentro
        del HTML), los iconos de Papelera y el toggle que muestra qué tema
        está puesto.
        """
        self._apply_density_stylesheet()
        # El rotulo va en RichText solo cuando lo que muestra es el mensaje de
        # vista previa vacia; los demas mensajes son texto plano y no llevan
        # color propio, asi que no hay nada que rehacerles.
        if self.preview_label.textFormat() == Qt.TextFormat.RichText:
            self.preview_label.setText(_preview_empty_html())
        self._tenir_papeleras()
        self._actualizar_toggle_tema(nombre)

    def _actualizar_toggle_tema(self, nombre: str) -> None:
        """Sincroniza estado, dibujo y ayuda del toggle con el tema activo."""
        claro = nombre == TEMA_CLARO
        with QSignalBlocker(self.tema_toggle):
            self.tema_toggle.setChecked(claro)
        activo = "claro" if claro else "oscuro"
        destino = "oscuro" if claro else "claro"
        icono = "theme_sun" if claro else "theme_moon"
        self.tema_toggle.setIcon(load_icon(icono, paleta().PANE_TEXT))
        ayuda = f"Tema {activo}. Clic para cambiar al tema {destino}."
        self.tema_toggle.setToolTip(ayuda)
        self.tema_toggle.setAccessibleName(f"Cambiar al tema {destino}")
        self.tema_toggle.setAccessibleDescription(ayuda)

    def _tenir_papeleras(self) -> None:
        """Vuelve a pintar el dibujo de la Papelera del color del texto.

        El dibujo lleva el color dentro del icono y no en la hoja de estilo,
        así que el repintado general no lo alcanza: con el tema claro se
        quedaba en el blanco del tema oscuro y desaparecía del botón.
        """
        icono = load_icon("trash", self._button_text_color())
        for boton in getattr(self, "_botones_papelera", ()):
            boton.setIcon(icono)

    # ── Adaptación a la pantalla ────────────────────────────────────────

    def _paint_progress_pane(self) -> None:
        """Pone el fondo de la tabla al panel de progreso por archivo.

        El área de desplazamiento y su viewport pintan desde la paleta y no
        desde la hoja, así que no basta con la regla de ``#fileProgressPane``:
        sin esto, el panel se queda con el fondo que decida el estilo nativo.
        """
        progress_palette = self.times_scroll.palette()
        fondo = QColor(paleta().TABLE_BASE_BG)
        progress_palette.setColor(QPalette.ColorRole.Base, fondo)
        progress_palette.setColor(QPalette.ColorRole.Window, fondo)
        for progress_surface in (
            self.times_scroll,
            self.times_scroll.viewport(),
            self.times_container,
        ):
            progress_surface.setPalette(progress_palette)
            progress_surface.setAutoFillBackground(True)

    def _apply_density_stylesheet(self) -> None:
        """Hoja de la ventana con el fragmento de medidas de la densidad.

        El fragmento va al final para ganarle a las reglas de la hoja base,
        que tienen su misma especificidad, y solo toca espacios y altos: los
        colores, las tipografías y el radio de 6 px salen de la base y son los
        mismos en las dos densidades.
        """
        qss = window_stylesheet(_window_qss() + self._density.qss)
        if self.styleSheet() != qss:
            self.setStyleSheet(qss)

    def _register_density_layout(self, layout: QLayout, stacked: bool) -> None:
        """Anota un layout de cuadro para re-medirlo al cambiar la densidad.

        Se guarda de paso lo que el cuadro pide por su cuenta, que es lo que
        vale con las medidas holgadas: la densidad solo aprieta, nunca separa
        más de lo que el cuadro ya tenía. Así la ventana grande se dibuja
        exactamente igual que antes de que existiera todo esto.

        ``stacked`` distingue los cuadros que apilan filas (a los que hay que
        apretarles también la separación vertical) de los que llevan sus
        controles en una sola línea.
        """
        entry = (
            layout,
            stacked,
            layout.contentsMargins(),
            _layout_spacing(layout),
            _layout_column_spacing(layout),
        )
        self._density_layouts.append(entry)
        self._apply_layout_density(*entry)

    def _apply_layout_density(
        self,
        layout: QLayout,
        stacked: bool,
        margins,
        spacing: int,
        column_spacing: int,
    ) -> None:
        limit = self._density.group_margin_v
        layout.setContentsMargins(
            margins.left(),
            min(margins.top(), limit),
            margins.right(),
            min(margins.bottom(), limit),
        )
        if not stacked:
            return
        tight = min(spacing, self._density.group_row_spacing)
        if isinstance(layout, QGridLayout):
            layout.setVerticalSpacing(tight)
            # Los seis botones de «Entrada» en una fila son buena parte del
            # ancho mínimo de la ventana; juntarlos un poco es lo que hace
            # que los dos cuadros quepan uno al lado del otro en 1280 px.
            layout.setHorizontalSpacing(
                min(column_spacing, self._density.group_column_spacing)
            )
        else:
            layout.setSpacing(tight)

    def _apply_density(self, density: Density) -> None:
        """Pasa la ventana entera al juego de medidas ``density``."""
        self._density = density
        self._apply_density_stylesheet()
        margin = density.window_margin
        self._root_layout.setContentsMargins(margin, margin, margin, margin)
        self._root_layout.setSpacing(density.root_spacing)
        self._controls_grid.setSpacing(density.group_spacing)
        for entry in self._density_layouts:
            self._apply_layout_density(*entry)
        self.preview_scroll.setMinimumSize(
            density.preview_min_width, density.preview_min_height
        )
        self.template_combo.setMinimumWidth(
            _COMPACT_TEMPLATE_MIN_WIDTH
            if density.compact
            else _TEMPLATE_MIN_WIDTH
        )
        self.log_view.setMaximumHeight(density.bottom_pane_height)
        self.log_view.setMinimumWidth(density.log_min_width)
        summary_height = (
            CONTROL_HEIGHT_COMPACT
            if density.compact
            else CONTROL_HEIGHT
        )
        self.time_summary.fijar_alto(summary_height)
        self.tema_toggle.setFixedWidth(summary_height)
        self.times_scroll.setMaximumHeight(density.bottom_pane_height)
        self.times_pane.setMinimumWidth(self._times_pane_min_width())
        self.bottom_splitter.setMinimumHeight(density.bottom_min_height)
        for row in self._file_rows.values():
            label = row["name"]
            label.setFixedWidth(density.name_column_width)
            # El nombre completo vive en el tooltip: es de donde se vuelve a
            # recortar, porque el texto visible ya viene con sus puntos.
            self._set_row_name(row, label.toolTip())

    def _refresh_minimum_size(self) -> None:
        """Mide los tres números que gobiernan el reparto y fija el suelo.

        Se miden en vez de escribirse porque dependen de la tipografía del
        sistema y del escalado de Windows, que no se conocen hasta que la
        aplicación corre en el equipo:

        * el mínimo con las medidas compactas apiladas, que es el suelo al
          que se puede encoger la ventana. Qt le pondría como mínimo el del
          reparto que tenga montado, y con las medidas holgadas eso son 905
          px de alto: la ventana no se dejaba arrastrar por debajo y nunca
          llegaba al alto en el que tenía que apretarse, así que quien la
          abría en un monitor grande no podía ponerla en media pantalla. El
          mínimo explícito manda sobre el del reparto y de apretarla a
          tiempo se encarga ``_update_responsive_layout``.
        * el ancho a partir del cual el reparto en dos columnas cabe.
        * el alto que pide el reparto holgado, que es el umbral por debajo
          del cual hay que apretarse. Antes ese umbral era un número escrito
          a mano (820 px) y se quedaba corto: entre 860 y 980 px de alto la
          ventana usaba medidas holgadas que no caben, y el layout, sin
          sitio, encogía los cuadros por debajo de su mínimo. Ahí es donde
          «Salidas» aparecía con las casillas montadas unas sobre otras y el
          botón de matrículas fuera de su marco.
        """
        density = self._density
        self._measuring_layout = True
        try:
            self._apply_density(ROOMY)
            roomy = self._layout_minimum(1)
            self._apply_density(COMPACT)
            stacked = self._layout_minimum(1)
            side_by_side = self._layout_minimum(2)
            self._apply_density(density)
        finally:
            self._measuring_layout = False
        self._stacked_minimum = stacked
        self._side_by_side_minimum = side_by_side
        self._roomy_minimum = roomy
        self._two_column_width = side_by_side.width()
        # El reparto se decide con el umbral recién medido, no con el que
        # valía antes de medir: si la ventana ya es bastante ancha para dos
        # columnas, dejar el apilado le cuesta el alto entero de la versión
        # de una columna, que en un escritorio de 1280 px no cabe.
        self._apply_controls_columns(self._controls_columns_for(self.width()))
        self._apply_minimum_size()

    def _apply_minimum_size(self) -> None:
        """Suelo de la ventana para el reparto que tiene montado ahora.

        El ancho es siempre el del reparto apilado, que es hasta donde se
        puede estrechar: al hacerlo los cuadros vuelven a una columna. El
        alto es el del reparto en curso, porque el de dos columnas necesita
        cien píxeles menos y sería una pena no dejar aprovecharlos.
        """
        floor = (
            self._side_by_side_minimum
            if self._controls_columns == 2
            else self._stacked_minimum
        )
        minimum = QSize(self._stacked_minimum.width(), floor.height())
        if self.minimumSize() != minimum:
            self.setMinimumSize(minimum)

    def _layout_minimum(self, columns: int) -> QSize:
        """Lo que pide el contenido con los cuadros repartidos así.

        El layout guarda el mínimo que calculó la última vez; sin invalidarlo
        devuelve el del reparto anterior y la medida no vale de nada.
        """
        self._apply_controls_columns(columns)
        # Invalidar solo el layout raíz no basta: el mínimo que devolvería
        # sigue siendo el que la rejilla calculó para el reparto anterior.
        self._controls_grid.invalidate()
        self._controls_grid.parentWidget().updateGeometry()
        layout = self.centralWidget().layout()
        layout.invalidate()
        layout.activate()
        return layout.minimumSize()

    def _grow_to_fit_content(self) -> None:
        """Estira la ventana si el tamaño pedido se queda corto.

        El tamaño de apertura es una preferencia, no una medida del
        contenido: según la tipografía del sistema el reparto puede pedir
        algún píxel más. Se le da, sin salirse del escritorio, que es el
        único límite que no se negocia.

        El alto que se pide es el del reparto holgado, no el del que esté
        montado: donde la pantalla lo permite, la ventana se abre con las
        medidas de siempre en vez de apretarse por unos pocos píxeles. Donde
        no lo permite, ``fit_to_screen`` recorta y ``_update_responsive_layout``
        aprieta, que es lo que corresponde.
        """
        needed = self.centralWidget().layout().minimumSize()
        fit_to_screen(
            self,
            max(self.width(), needed.width()),
            max(self.height(), needed.height(), self._roomy_minimum.height()),
        )

    def _update_responsive_layout(self) -> None:
        """Ajusta medidas y reparto de los cuadros al tamaño de la ventana.

        Se llama en cada cambio de tamaño, así que además del tamaño con el
        que se abre cubre lo que venga después: maximizar, restaurar o
        arrastrar la ventana a un monitor con otra resolución.
        """
        if self._measuring_layout:
            return
        density = density_for(
            self.height(), self._density, self._roomy_minimum.height()
        )
        if density is not self._density:
            self._apply_density(density)
        self._apply_controls_columns(self._controls_columns_for(self.width()))
        self._apply_minimum_size()

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        margin = self._density.window_margin
        root.setContentsMargins(margin, margin, margin, margin)
        root.setSpacing(self._density.root_spacing)
        self._root_layout = root

        controls = self._build_controls()
        root.addWidget(controls, 0)

        root.addLayout(self._build_progress_row())
        # Debajo de la barra porque cuenta lo mismo a otra escala: la barra
        # dice cuánto falta del paso en curso y la cadena, cuántos pasos
        # faltan de la entrega. Los cuatro últimos ocurren en la ventana de
        # AirVault y llegan aquí por su señal de avance.
        self.cadena = CadenaAutomatica(self._automatizacion)
        root.addWidget(self.cadena)
        root.addLayout(self._build_search_row())
        root.addWidget(self._build_splitter(), stretch=1)

        bottom = self._build_bottom_splitter()
        # Cuatro archivos visibles sin desplazar: por debajo de esto el panel
        # se queda en dos filas y deja de servir para seguir un batch. En
        # pantallas bajas se conforma con menos, que es preferible a no
        # caber.
        bottom.setMinimumHeight(self._density.bottom_min_height)
        root.addWidget(bottom)

    def _build_controls(self) -> QWidget:
        panel = QWidget()
        grid = QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(self._density.group_spacing)
        # Los dos cuadros forman la cabecera de trabajo y se reparten el ancho
        # por mitades para que ninguno pese visualmente más que el otro.
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self._controls_grid = grid
        self._input_group = self._build_input_group()
        self._options_group = self._build_options_group()
        # El reparto se decide antes del primer dibujado. Empezar siempre en
        # una columna y corregir después dejaba a la ventana estirada al alto
        # de la versión apilada, que es justo el que no cabía.
        self._apply_controls_columns(self._controls_columns_for(self.width()))
        return panel

    def _apply_controls_columns(self, columns: int) -> None:
        """Coloca los cuadros de arriba en una columna o en dos.

        Apilados ocupan más de la mitad del alto que da un portátil de
        1366x768 y no dejan sitio para la vista previa ni para la tabla.
        Cuando el alto escasea y el ancho sobra (que es exactamente lo que
        pasa en esas pantallas) «Entrada» y «Salidas» se ponen una al lado de
        la otra y el bloque pasa a medir lo que mide el más alto de los dos.

        Los controles secundarios viven dentro de «Salidas», donde no crean
        una tercera fila ni cambian de sitio al redimensionar la ventana.
        """
        if columns == self._controls_columns:
            return
        self._controls_columns = columns
        grid = self._controls_grid
        for widget in (
            self._input_group,
            self._options_group,
        ):
            grid.removeWidget(widget)
        if columns == 1:
            grid.addWidget(self._input_group, 0, 0, 1, 2)
            grid.addWidget(self._options_group, 1, 0, 1, 2)
        else:
            grid.addWidget(self._input_group, 0, 0)
            grid.addWidget(self._options_group, 0, 1)
        grid.invalidate()
        grid.parentWidget().updateGeometry()
        central = self.centralWidget()
        if central is not None and central.layout() is not None:
            central.layout().invalidate()

    def _controls_columns_for(self, width: int) -> int:
        """Columnas que le tocan a los cuadros de arriba con este ancho.

        Dos cuando el ancho llega al que se midió para ese reparto. Usar una
        sola columna en un monitor ancho dejaba casi toda cada fila vacía y
        le quitaba ese alto al visor, aunque ambos cuadros cabían en paralelo.
        """
        return 2 if width >= self._two_column_width else 1

    def _button_text_color(self) -> QColor:
        """Color con el que el estilo escribe el texto de los botones.

        Windows lo cambia con el tema del sistema, así que se pregunta en vez
        de fijarlo: es el único color de la fila de botones que no sale de las
        constantes de la aplicación.
        """
        return self.palette().color(QPalette.ColorRole.ButtonText)

    def _build_input_group(self) -> QGroupBox:
        group = QGroupBox("Entrada")
        grid = QGridLayout(group)
        grid.setContentsMargins(8, 5, 8, 5)
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self._register_density_layout(grid, stacked=True)

        grid.addWidget(QLabel("Archivos:"), 0, 0)
        self.input_edit = QLineEdit()
        self.input_edit.setReadOnly(True)
        self.input_edit.setPlaceholderText("Sin archivos seleccionados")
        self.input_edit.setToolTip("Archivos PDF que se van a procesar")
        self.input_edit.setAccessibleName("Archivos seleccionados")
        grid.addWidget(self.input_edit, 0, 1)

        btn_pick = QPushButton("Seleccionar…")
        btn_pick.setToolTip("Elegir uno o varios PDF de cualquier carpeta")
        btn_pick.clicked.connect(self._browse_pdfs)
        grid.addWidget(btn_pick, 0, 2)

        input_menu = QMenu(group)
        input_menu.setToolTipsVisible(True)
        btn_input = input_menu.addAction("Detectar input")
        btn_input.setToolTip("Detectar los PDF de la carpeta input/ del programa")
        btn_input.triggered.connect(self._load_default_input)

        input_menu.addSeparator()
        btn_clear_input = input_menu.addAction("Vaciar input")
        # Los dos botones que mandan archivos a la Papelera llevan el mismo
        # dibujo: es la única acción de la fila que borra algo y así se
        # distingue de «Detectar» o «Seleccionar» sin leer el texto. Va del
        # color del texto del botón para que se lea con el tema claro y con
        # el oscuro, y para que no cante al lado de la palabra.
        self._botones_papelera = [btn_clear_input]
        trash_icon = load_icon("trash", self._button_text_color())
        btn_clear_input.setIcon(trash_icon)
        btn_clear_input.setToolTip(
            "Mover los archivos de input/ a la Papelera. Los de "
            "input/processed se conservan"
        )
        btn_clear_input.triggered.connect(self._clear_input_folder)

        self.btn_clear_output = input_menu.addAction("Vaciar output")
        self.btn_clear_output.setIcon(trash_icon)
        self._botones_papelera.append(self.btn_clear_output)
        self.btn_clear_output.setToolTip(
            "Mover todas las ejecuciones de output/ a la Papelera de reciclaje"
        )
        self.btn_clear_output.triggered.connect(self._clear_output_folder)
        self.input_actions_button = QToolButton()
        self.input_actions_button.setText("Carpetas")
        configure_menu_button(self.input_actions_button, input_menu)
        grid.addWidget(self.input_actions_button, 0, 3)

        grid.addWidget(QLabel("Plantilla:"), 1, 0)
        self.template_combo = QComboBox()
        configure_combo_box(self.template_combo, 18)
        self.template_combo.setMinimumWidth(
            _COMPACT_TEMPLATE_MIN_WIDTH
            if self._density.compact
            else _TEMPLATE_MIN_WIDTH
        )
        self.template_combo.currentIndexChanged.connect(
            self._refresh_preview_template
        )
        grid.addWidget(self.template_combo, 1, 1)

        btn_tpl = QPushButton("Buscar…")
        btn_tpl.setToolTip("Seleccionar una plantilla JSON personalizada")
        btn_tpl.clicked.connect(self._browse_template)
        grid.addWidget(btn_tpl, 1, 2)

        template_menu = QMenu(group)
        template_menu.setToolTipsVisible(True)
        self.btn_editor = template_menu.addAction("Editor de plantillas…")
        self.btn_editor.setToolTip("Abrir el editor visual de plantillas")
        self.btn_editor.triggered.connect(self._open_template_editor)

        self.btn_csv_viewer = template_menu.addAction("Visor de CSV…")
        self.btn_csv_viewer.setToolTip(
            "Abrir una ventana independiente con el historial de ejecuciones "
            "procesadas y sus CSV"
        )
        self.btn_csv_viewer.triggered.connect(self._open_csv_viewer)
        self.template_actions_button = QToolButton()
        self.template_actions_button.setText("Herramientas")
        configure_menu_button(self.template_actions_button, template_menu)
        for menu_button in (
            self.input_actions_button,
            self.template_actions_button,
        ):
            menu_button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )
        grid.addWidget(self.template_actions_button, 1, 3)

        self.estimate_label = ElidedLabel(
            "Seleccione los archivos PDF que desea procesar."
        )
        pintar_del_tema(
            self.estimate_label, lambda: f"color: {paleta().TEXT_SECONDARY};"
        )
        self.estimate_label.setToolTip(
            "Muestra la cantidad de archivos, páginas y el tiempo estimado."
        )
        grid.addWidget(self.estimate_label, 2, 0, 1, 4)
        return group

    def _build_options_group(self) -> QGroupBox:
        # El cuadro de salidas es el mismo que muestra el visor de CSV; aquí
        # se le añaden las opciones que solo tienen sentido al procesar.
        group = self.export_options = ExportOptionsGroup(raiz=SCRIPT_DIR)
        layout = group.layout()
        self._register_density_layout(layout, stacked=True)
        self.matricula_check = group.matricula_check
        self.mes_check = group.mes_check
        self.discrepancias_check = group.discrepancias_check
        self.csv_date_mode_combo = group.csv_date_mode_combo
        self.csv_date_mode_combo.currentIndexChanged.connect(
            self._on_csv_date_mode_changed
        )

        tools_row = QHBoxLayout()
        tools_row.setSpacing(8)
        tools_row.addSpacing(group.controls_indent)
        view_menu = MultiSelectMenu(group)
        view_menu.setToolTipsVisible(True)
        self.fields_check = view_menu.addAction("Visualizar campos")
        self.fields_check.setCheckable(True)
        self.fields_check.setToolTip(
            "Dibuja los recuadros de los campos solo en la vista previa; los "
            "PDF exportados salen sin marcas."
        )
        self.fields_check.toggled.connect(self._on_fields_toggled)
        self.important_fields_check = view_menu.addAction(
            "Columnas importantes"
        )
        self.important_fields_check.setCheckable(True)
        self.important_fields_check.setEnabled(False)
        self.important_fields_check.setToolTip(
            "Muestra solo las columnas importantes. Sin marcar, se dibuja la "
            "plantilla completa."
        )
        self.important_fields_check.toggled.connect(self._on_fields_toggled)
        self.fields_check.toggled.connect(
            self.important_fields_check.setEnabled
        )
        important_fields_action = view_menu.addAction(
            "Elegir columnas importantes…"
        )
        important_fields_action.setToolTip(
            "Seleccionar las columnas importantes; la lista se guarda por "
            "plantilla y decide qué recuadros y columnas se muestran."
        )
        important_fields_action.triggered.connect(self._open_important_fields)
        self.view_button = QToolButton()
        self.view_button.setText("Campos")
        configure_menu_button(self.view_button, view_menu)
        menu_width = max(
            button.fontMetrics().horizontalAdvance(button.text())
            for button in (group.separation_button, self.view_button)
        ) + 44
        group.separation_button.setFixedWidth(menu_width)
        self.view_button.setFixedWidth(menu_width)
        self.view_button.setToolTip(
            "Elegir qué campos se muestran en la vista previa."
        )
        # Arriba, pegado a «Separación»: los dos abren un menú y miden lo
        # mismo, así que uno debajo del otro y en filas distintas se leían
        # como una columna rota.
        group.agregar_menu(self.view_button)

        self.fleet_check = QCheckBox("Verificar matrículas")
        self.fleet_check.setChecked(True)
        self.fleet_check.setToolTip(
            "Corrige la matrícula leída contra la lista de aviones: la que no "
            "esté se cambia por la más parecida y queda marcada para revisar."
        )
        tools_row.addWidget(self.fleet_check)
        fleet_button = QPushButton("Editar flota…")
        fleet_button.setToolTip(
            "Abre la lista de matrículas de la flota. Debe estar al día con "
            f"las altas y las bajas; se guarda en {FLEET_FILENAME}."
        )
        fleet_button.clicked.connect(self._open_fleet_editor)

        self.btn_airvault = QPushButton("Indexar en AirVault…")
        self.btn_airvault.setToolTip(AIRVAULT_TOOLTIP)
        self.btn_airvault.clicked.connect(lambda: self._open_airvault())
        self.btn_web_reports = QPushButton("Web Reports…")
        self.btn_web_reports.setToolTip(WEB_REPORTS_TOOLTIP)
        self.btn_web_reports.clicked.connect(self._open_web_reports)
        # Siempre en esta fila, con cualquier tamaño de ventana. Antes se
        # escondía en las medidas compactas y pasaba al menú «Herramientas»,
        # y en un escritorio remoto pequeño parecía que no existía.
        # Los tres que abren otra ventana, juntos y contra el margen
        # derecho; a la izquierda queda solo la casilla, que es lo que esta
        # fila elige. «Editar flota…» estaba en medio, entre la casilla y el
        # hueco elástico, así que la fila alternaba botón y opción sin que
        # el sitio de cada uno dijera nada. Van de menos a más alcance: la
        # lista local, la consulta y el indexado.
        tools_row.addStretch()
        for boton in (
            fleet_button,
            self.btn_web_reports,
            self.btn_airvault,
        ):
            tools_row.addWidget(boton)
        layout.addLayout(tools_row)
        self._fleet_row = tools_row
        return group

    def _open_airvault(self) -> None:
        """Abre una ventana libre o crea otra para trabajar en paralelo.

        Se construye la primera vez que se pide: quien no sube nada a
        AirVault no paga el recorrido del historial ni la ventana. Si la
        última ya tiene un hilo activo, se crea otra; cada ejecución conserva
        su estado, conexión y controles sin alterar las demás.
        """
        ventana = self._airvault_window
        if ventana is None or ventana.hilo() is not None:
            # No se le da ``parent``: en Windows una ventana nativa con dueño
            # no recibe una entrada propia en la barra de tareas. La
            # referencia de esta clase basta para conservarla viva y el
            # cierre ordenado se hace en ``_teardown``.
            ventana = AirVaultWindow(SCRIPT_DIR, self._automatizacion)
            ventana.setWindowIcon(self.windowIcon())
            ventana.abrir_corrida_paralela.connect(
                self._open_airvault_corrida
            )
            ventana.avance_automatico.connect(self._al_avanzar_airvault)
            self._airvault_windows.append(ventana)
            self._airvault_window = ventana
            if self._airvault_corrida is not None:
                ventana.fijar_corrida(self._airvault_corrida)
        ventana.show()
        ventana.raise_()
        ventana.activateWindow()

    def _open_web_reports(self) -> None:
        """Abre la consulta de limpieza de Log Page Audit."""
        ventana = self._web_reports_window
        if ventana is None:
            # Igual que AirVault, queda sin dueño para tener su propia
            # entrada en la barra de tareas de Windows.
            ventana = WebReportsWindow(SCRIPT_DIR)
            ventana.setWindowIcon(self.windowIcon())
            self._web_reports_window = ventana
        ventana.show()
        ventana.raise_()
        ventana.activateWindow()

    def _al_avanzar_airvault(self, paso: str, estado: str) -> None:
        """Lleva a la línea de pasos lo que ocurre en AirVault.

        Solo de la ejecución que esta ventana mandó subir. Se pueden tener
        varias ventanas de AirVault abiertas a la vez, cada una con su
        ejecución, y contar el avance de otra en esta línea diría que la
        entrega de aquí va por donde va la de al lado.
        """
        ventana = self.sender()
        if (
            self._airvault_corrida is None
            or ventana is None
            or ventana.corrida() != self._airvault_corrida
        ):
            return
        self.cadena.marcar(paso, estado)

    def _open_airvault_corrida(self, csv: str) -> None:
        """Abre otra ejecución sin tocar la ventana que ya está ocupada."""
        anterior = self._airvault_window
        self._airvault_window = None
        self._airvault_corrida = Path(csv)
        self._open_airvault()
        if self._airvault_window is anterior:
            return
        self._airvault_window.fijar_corrida(csv)

    def _effective_threads(self) -> int:
        """Reserva un hilo para la interfaz cuando hay mas de uno."""
        return max(1, self._selected_threads - 1)

    def _build_progress_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        # El detalle del proceso vive en la bitácora inferior. Se conserva
        # el último texto solo como estado interno para no mezclar mensajes
        # variables con la barra de progreso ni quitarle ancho.
        self.status_label = ElidedLabel("", parent=self)
        self.status_label.hide()

        self.busy_label = QLabel("", self)
        self.busy_label.hide()
        self.busy_label.setToolTip("Procesamiento en curso")

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        row.addWidget(self.progress, 1)

        summary_height = (
            CONTROL_HEIGHT_COMPACT
            if self._density.compact
            else CONTROL_HEIGHT
        )
        self.time_summary = Cronometro(summary_height)
        self.time_summary.setToolTip(
            "El tiempo restante se recalcula con las páginas completadas y el "
            "ritmo observado."
        )
        # Los dos nombres de siempre, que es como los pide el resto de la
        # ventana: el panel guarda las cifras por su clave y las rayas aparte.
        self.time_labels = self.time_summary.etiquetas
        self.time_dividers = self.time_summary.separadores
        row.addWidget(self.time_summary)
        self.btn_process = QPushButton("Procesar")
        self.btn_process.setDefault(True)
        self.btn_process.clicked.connect(self._start_processing)

        actions_menu = QMenu(self)
        actions_menu.setToolTipsVisible(True)
        self.btn_preprocess = actions_menu.addAction("Preprocesar")
        self.btn_preprocess.setToolTip(
            "Aplica corrección de inclinación y alineación sin ejecutar OCR."
        )
        self.btn_preprocess.triggered.connect(self._start_preprocessing)

        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setToolTip(self._CANCELAR_AYUDA)
        self.btn_cancel.clicked.connect(self._request_cancel)

        self.btn_export = actions_menu.addAction("Exportar")
        self.btn_export.setEnabled(False)
        self.btn_export.setToolTip(
            "Volver a generar CSV, JSON y PDF con las opciones actuales, sin "
            "reprocesar. Los PDF repetidos se numeran (-2, -3…)"
        )
        self.btn_export.triggered.connect(self._exportar)

        self.btn_depurar = actions_menu.addAction("Depurar")
        self.btn_depurar.setEnabled(False)
        self.btn_depurar.setToolTip(DEPURAR_TOOLTIP)
        self.btn_depurar.triggered.connect(self._depurar_paginas)

        self.more_actions_button = QToolButton()
        self.more_actions_button.setText("Más acciones")
        configure_menu_button(self.more_actions_button, actions_menu)
        self.more_actions_button.setToolTip(
            "Preprocesar, exportar o depurar."
        )

        # El de siempre, pero sin volver a pulsar nada entre paso y paso.
        # Se lleva el azul porque es el que hace la entrega entera; los
        # demás siguen ahí para hacer un solo tramo cuando hace falta.
        self.btn_automatico = QToolButton()
        self.btn_automatico.setText("Automático")
        self.btn_automatico.setObjectName("primaryButton")
        self.btn_automatico.setToolTip(
            "Ejecuta el flujo completo. La flecha permite elegir hasta qué "
            "paso continúa; «Cancelar» detiene el flujo."
        )
        self.menu_automatizacion = MenuAutomatizacion(
            self._automatizacion, self
        )
        configure_menu_button(
            self.btn_automatico, self.menu_automatizacion, split=True
        )
        # Sin ancho a mano. Los «+ 50 px» que llevaba aquí eran el parche de un
        # relleno que nunca llegaba a aplicarse (lo pisaba la regla de
        # «#primaryButton», que pesa más), así que ensanchaban el botón sin
        # mover el texto: quedaba hueco a la izquierda y el rótulo contra el
        # separador. Con el relleno ya puesto en la hoja, la medida que pide el
        # propio botón reserva la celda de la flecha y centra el texto.
        self.btn_automatico.clicked.connect(self._start_automatico)

        # El orden de la fila, ya con los cuatro construidos. El grupo va
        # pegado a la derecha, así que se lee de fuera hacia dentro: el
        # cajón de lo que no cabe en el extremo, luego el que corta, y los
        # dos que arrancan trabajo juntos al final, con el principal
        # cerrando contra el margen. Antes «Más» quedaba entre «Cancelar» y
        # «Automático», partiendo en dos el grupo que actúa para meter en
        # medio un desplegable que no hace nada por sí mismo.
        for boton in (
            self.more_actions_button,
            self.btn_cancel,
            self.btn_process,
            self.btn_automatico,
        ):
            row.addWidget(boton)
        return row

    def _on_tema_elegido(self, claro: bool) -> None:
        """Aplica a toda la aplicación el tema elegido con el toggle."""
        aplicar_tema(TEMA_CLARO if claro else TEMA_OSCURO)

    def _set_time_summary(
        self,
        elapsed: float | None = None,
        remaining: float | None = None,
        total: float | None = None,
    ) -> None:
        """Actualiza las tres métricas sin mezclar estados o estimaciones."""
        self.time_summary.actualizar(elapsed, remaining, total)

    def _build_splitter(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal)

        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)
        preview_layout.setContentsMargins(0, 0, 0, 0)

        self.preview_label = QLabel(_preview_empty_html())
        self.preview_label.setTextFormat(Qt.TextFormat.RichText)
        self.preview_label.setObjectName("previewPlaceholder")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setWordWrap(True)
        self.preview_label.setMinimumSize(
            self._density.preview_min_width, self._density.preview_min_height
        )
        self.preview_label.setAccessibleName("Vista previa de la página")

        self.preview_scroll = ZoomableScrollArea()
        self.preview_scroll.set_zoom_callback(self._zoom_preview)
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_scroll.setMinimumSize(
            self._density.preview_min_width, self._density.preview_min_height
        )
        style_pdf_surface(self.preview_scroll)
        self.preview_scroll.setWidget(self.preview_label)

        self.preview_pagination = QWidget()
        self.preview_pagination.setObjectName("previewPagination")
        nav = QHBoxLayout(self.preview_pagination)
        nav.setContentsMargins(0, 0, 0, 0)
        self.btn_prev = QToolButton()
        self.btn_prev.setArrowType(Qt.ArrowType.LeftArrow)
        self.btn_prev.setToolTip("Página anterior (flecha izquierda)")
        self.btn_prev.setAccessibleName("Página anterior")
        self.btn_prev.setShortcut(QKeySequence(Qt.Key.Key_Left))
        self.btn_prev.setEnabled(False)
        self.btn_prev.clicked.connect(self._prev_page)
        self.btn_next = QToolButton()
        self.btn_next.setArrowType(Qt.ArrowType.RightArrow)
        self.btn_next.setToolTip("Página siguiente (flecha derecha)")
        self.btn_next.setAccessibleName("Página siguiente")
        self.btn_next.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self.btn_next.setEnabled(False)
        self.btn_next.clicked.connect(self._next_page)
        self.preview_file_label = ElidedLabel("Ninguno")
        self.preview_file_label.setMaximumWidth(320)
        self.preview_file_label.setMinimumWidth(0)
        self.preview_file_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.preview_file_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.preview_file_label.setAccessibleName(
            "Archivo PDF activo en la vista previa"
        )
        self.preview_file_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.page_edit = QLineEdit()
        self.page_edit.setValidator(QIntValidator(1, 1, self.page_edit))
        self.page_edit.setToolTip(
            "Escriba el número de página del conjunto; el salto se aplica "
            "al terminar."
        )
        self.page_edit.setAccessibleName("Página actual")
        self.page_edit.setFixedWidth(48)
        self.page_edit.editingFinished.connect(self._jump_to_page_number)
        self.page_total_label = QLabel("de 0")
        nav.addWidget(self.btn_prev)
        nav.addWidget(QLabel("Página"))
        nav.addWidget(self.page_edit)
        nav.addWidget(self.page_total_label)
        nav.addWidget(self.btn_next)

        self.preview_file_indicator = QWidget()
        file_row = QHBoxLayout(self.preview_file_indicator)
        file_row.setContentsMargins(0, 0, 10, 0)
        file_row.setSpacing(4)
        self.preview_file_caption = QLabel("Archivo:")
        file_row.addWidget(self.preview_file_caption)
        file_row.addWidget(self.preview_file_label, 1)

        # Una sola barra: el archivo queda anclado a la izquierda y el
        # paginador se centra sin superponerse a nombres largos.
        self.preview_nav_bar = QWidget()
        self.preview_nav_bar.setObjectName("previewNavigationBar")
        nav_bar = QGridLayout(self.preview_nav_bar)
        nav_bar.setContentsMargins(0, 0, 0, 0)
        nav_bar.setHorizontalSpacing(0)
        nav_bar.setColumnStretch(0, 1)
        nav_bar.setColumnStretch(2, 1)
        nav_bar.setColumnMinimumWidth(
            2, self.preview_file_indicator.minimumSizeHint().width()
        )
        nav_bar.addWidget(
            self.preview_file_indicator,
            0,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        nav_bar.addWidget(
            self.preview_pagination,
            0,
            1,
            Qt.AlignmentFlag.AlignCenter,
        )

        viewer_frame = QWidget()
        viewer_frame_layout = QGridLayout(viewer_frame)
        viewer_frame_layout.setContentsMargins(0, 0, 0, 0)
        viewer_frame_layout.addWidget(self.preview_scroll, 0, 0)

        zoom_overlay = ZoomOverlay(
            (
                "Acercar la vista previa",
                "Acercar vista previa",
                lambda: self._zoom_preview(1.25),
            ),
            (
                "Ajustar la vista previa a la altura de la ventana",
                "Ajustar página a la ventana",
                self._fit_preview_vertical,
            ),
            (
                "Alejar la vista previa",
                "Alejar vista previa",
                lambda: self._zoom_preview(0.8),
            ),
            viewer_frame,
        )
        self.btn_zoom_in = zoom_overlay.btn_in
        self.btn_zoom_fit = zoom_overlay.btn_fit
        self.btn_zoom_out = zoom_overlay.btn_out
        self.zoom_label = zoom_overlay.value_label

        zoom_holder = QWidget(viewer_frame)
        zoom_holder_layout = QVBoxLayout(zoom_holder)
        zoom_holder_layout.setContentsMargins(8, 8, 8, 8)
        zoom_holder_layout.addWidget(zoom_overlay)
        # El recuadro de zoom flota sobre la página, así que no puede decidir
        # cuánto mide de mínimo el panel: era él quien acababa fijando el
        # suelo de la ventana entera. Ignorado en las dos medidas no cuenta
        # para ese mínimo; a cambio hay que esconderlo cuando la página se
        # queda más pequeña que él, que es lo que hace
        # ``hide_overlay_when_tight``: recortado a medias dejaría los botones
        # montados unos sobre otros.
        zoom_holder.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        # Abajo y centrado, el mismo sitio en las tres ventanas que llevan
        # zoom. A media altura y pegado a la izquierda caía justo encima de
        # la columna por la que se lee la bitácora.
        viewer_frame_layout.addWidget(
            zoom_holder,
            0,
            0,
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        )
        zoom_holder.raise_()
        keep_overlay_clear_of_scrollbars(zoom_holder, self.preview_scroll)
        self._zoom_holder = zoom_holder
        hide_overlay_when_tight(zoom_holder)

        page_area = QWidget()
        page_layout = QVBoxLayout(page_area)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)
        page_layout.addWidget(viewer_frame, stretch=1)
        page_layout.addWidget(self.preview_nav_bar)
        preview_layout.addWidget(page_area, stretch=1)
        self._update_preview_zoom_controls()

        self.table = ResultsTableWidget(0, 0)
        self.table.setAccessibleName("Resultados de validación")
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        style_data_table(self.table)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.horizontalHeader().setSectionsMovable(False)
        self.table.horizontalHeader().setFixedHeight(30)
        self.table_sort = ColumnSortController(self.table)
        self.table.cellDoubleClicked.connect(self._jump_to_page)

        table_panel = QWidget()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(8)
        table_layout.addWidget(self.table, 1)
        table_controls = QHBoxLayout()
        table_controls.setSpacing(8)
        table_controls.addStretch(1)
        self.duplicates_label = QLabel("Duplicados: 0")
        self.duplicates_label.setAccessibleName("Resumen de duplicados")
        self.duplicates_label.setToolTip(
            "No hay números de bitácora repetidos en los archivos procesados."
        )
        pintar_del_tema(
            self.duplicates_label, lambda: f"color: {paleta().TEXT_SECONDARY};"
        )
        table_controls.addWidget(self.duplicates_label)
        self.csv_columns_toggle = CsvColumnModeButton()
        self.csv_columns_toggle.setEnabled(False)
        self.csv_columns_toggle.setVisible(False)
        # Oculto no debe encoger la fila: sin esto, la barra de abajo de la
        # tabla queda mas baja que la de la vista previa (que siempre tiene
        # botones) y el panel derecho se ve mas corto que el izquierdo hasta
        # que se procesa un batch.
        toggle_policy = self.csv_columns_toggle.sizePolicy()
        toggle_policy.setRetainSizeWhenHidden(True)
        self.csv_columns_toggle.setSizePolicy(toggle_policy)
        self.csv_columns_toggle.toggled.connect(self._apply_csv_table_view)
        table_controls.addWidget(self.csv_columns_toggle)
        table_layout.addLayout(table_controls)

        splitter.addWidget(preview_widget)
        splitter.addWidget(table_panel)
        splitter.setStretchFactor(0, _PREVIEW_SHARE)
        splitter.setStretchFactor(1, _RESULTS_SHARE)
        splitter.setChildrenCollapsible(False)
        splitter.splitterMoved.connect(self._on_content_splitter_moved)
        self.content_splitter = splitter
        return splitter

    def _on_content_splitter_moved(self, _position: int, _index: int) -> None:
        """Una vez que el usuario reparte el ancho, se respeta su medida."""
        self._content_splitter_adjusted = True

    def _balance_content_splitter(self) -> None:
        """Reparte el ancho entre la vista previa y la tabla.

        Los factores de estiramiento solo gobiernan el espacio *sobrante*, y
        la tabla de resultados pide de ancho lo que sumen sus columnas: se lo
        quedaba casi todo y la bitácora se quedaba en su mínimo, una franja
        donde una página vertical no se lee. El reparto se aplica a mano,
        como en el visor de CSV, hasta que alguien mueva el separador.
        """
        if self._content_splitter_adjusted:
            return
        available = max(
            0,
            self.content_splitter.width() - self.content_splitter.handleWidth(),
        )
        pane = available * _PREVIEW_SHARE // (_PREVIEW_SHARE + _RESULTS_SHARE)
        self.content_splitter.setSizes([pane, available - pane])

    def _build_bottom_splitter(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.log_view.setAccessibleName("Registro de eventos")
        self.log_view.setMaximumHeight(self._density.bottom_pane_height)
        self.log_view.document().setMaximumBlockCount(2000)
        self.log_view.setMinimumWidth(self._density.log_min_width)
        splitter.addWidget(self.log_view)

        times = QWidget()
        times.setObjectName("fileProgressPane")
        times_layout = QVBoxLayout(times)
        times_layout.setContentsMargins(10, 8, 10, 8)
        times_layout.setSpacing(6)
        self.file_progress_title = QLabel("Progreso por archivo")
        self.file_progress_title.setObjectName("fileProgressTitle")
        times_layout.addWidget(self.file_progress_title)

        self.preview_context_label = QLabel(
            "Sin archivo activo"
        )
        self.preview_context_label.setObjectName("previewContext")
        self.preview_context_label.setWordWrap(True)
        times_layout.addWidget(self.preview_context_label)

        self.times_vbox = QVBoxLayout()
        self.times_vbox.setContentsMargins(0, 0, 0, 0)
        self.times_vbox.setSpacing(4)
        self.times_container = QWidget()
        self.times_container.setObjectName("fileProgressList")
        self.times_container.setLayout(self.times_vbox)

        self.times_scroll = QScrollArea()
        self.times_scroll.setObjectName("fileProgressScroll")
        self.times_scroll.viewport().setObjectName("fileProgressViewport")
        self.times_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.times_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.times_scroll.setWidgetResizable(True)
        self.times_scroll.setWidget(self.times_container)
        al_cambiar_tema(self.times_scroll, self._paint_progress_pane)
        self.times_scroll.setMaximumHeight(self._density.bottom_pane_height)
        # La lista es la parte elástica del panel: cuando el alto escasea es
        # ella la que se queda con una fila y se desplaza, en vez de robarles
        # píxeles a los rótulos, que no se desplazan ni se recortan.
        self.times_scroll.setMinimumHeight(_TIMES_SCROLL_MIN_HEIGHT)
        times_layout.addWidget(self.times_scroll, 1)

        self.empty_times_label = QLabel(
            "El progreso de cada archivo aparecerá aquí."
        )
        self.empty_times_label.setObjectName("fileProgressEmpty")
        self.empty_times_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_times_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        times_layout.addWidget(self.empty_times_label)
        self.times_scroll.hide()

        # Mitad y mitad, como la tabla y el visor del Visor de CSV: el panel
        # lleva cuatro columnas por archivo y la consola no necesita el resto.
        # El mínimo es la fila completa, para que nunca haya scroll lateral.
        self.times_pane = times
        times.setMinimumWidth(self._times_pane_min_width())
        splitter.addWidget(times)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.splitterMoved.connect(self._on_bottom_splitter_moved)
        self.bottom_splitter = splitter
        return splitter

    def _on_bottom_splitter_moved(self, _position: int, _index: int) -> None:
        """Una vez que el usuario reparte el espacio, se respeta su medida."""
        self._bottom_splitter_adjusted = True

    def _balance_bottom_splitter(self) -> None:
        """Reparte el ancho entre consola y panel mientras nadie lo ajuste."""
        if self._bottom_splitter_adjusted:
            return
        available = max(
            0, self.bottom_splitter.width() - self.bottom_splitter.handleWidth()
        )
        half = available // 2
        self.bottom_splitter.setSizes([half, available - half])
        self._resize_preview_placeholder()

    def _times_pane_min_width(self) -> int:
        """Ancho de una fila completa del panel de avance, con sus márgenes."""
        margins = self.times_pane.layout().contentsMargins()
        return (
            self._density.name_column_width
            + _BAR_MIN_WIDTH
            + _PAGES_COLUMN_WIDTH
            + _SECS_COLUMN_WIDTH
            + 3 * _FILE_ROW_SPACING
            + margins.left()
            + margins.right()
        )

    def _make_file_row(self) -> dict:
        """Fila del panel: nombre, barra con porcentaje, páginas y reloj."""
        row = QHBoxLayout()
        row.setSpacing(_FILE_ROW_SPACING)
        name = QLabel("")
        # Anchos fijos en las tres columnas de texto: con anchos mínimos cada
        # fila empezaba la barra en un sitio distinto según lo largo que
        # fuera el nombre, y el panel dejaba de leerse como una tabla.
        name.setFixedWidth(self._density.name_column_width)
        bar = QProgressBar()
        bar.setObjectName("timeBar")
        # La barra de 14 px recortaba el texto y el porcentaje no llegaba a
        # verse; con la altura de una línea cabe dentro de la propia barra.
        bar.setFixedHeight(20)
        bar.setMinimumWidth(_BAR_MIN_WIDTH)
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        # RAE: espacio entre la cifra y el signo. La hoja ya lo centra.
        bar.setFormat("%p %")
        pages = QLabel("–")
        pages.setObjectName("filePages")
        pages.setFixedWidth(_PAGES_COLUMN_WIDTH)
        pages.setAlignment(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter)
        pages.setToolTip("Páginas procesadas de las que tiene el archivo")
        secs = QLabel("–")
        secs.setFixedWidth(_SECS_COLUMN_WIDTH)
        secs.setAlignment(Qt.AlignmentFlag.AlignRight
                          | Qt.AlignmentFlag.AlignVCenter)
        secs.setToolTip("Tiempo que lleva el archivo")
        row.addWidget(name)
        row.addWidget(bar, 1)
        row.addWidget(pages)
        row.addWidget(secs)
        self.times_vbox.addLayout(row)
        return {"name": name, "bar": bar, "pages": pages, "secs": secs}

    @staticmethod
    def _set_row_name(row: dict, name: str, tooltip: str = "") -> None:
        """Escribe el nombre recortado a la columna, con el completo al pasar."""
        label = row["name"]
        label.setToolTip(tooltip or name)
        label.setText(
            label.fontMetrics().elidedText(
                name, Qt.TextElideMode.ElideMiddle, label.maximumWidth() - 2
            )
        )

    @staticmethod
    def _set_row_pages(row: dict, done: int, total: int) -> None:
        """Escribe el contador de páginas y la barra de la fila."""
        row["pages"].setText(f"{done}/{total} pág." if total else f"{done} pág.")
        row["bar"].setRange(0, 100)
        row["bar"].setValue(round(done * 100 / total) if total else 0)

    def _prepare_file_rows(self, paths: list[Path]) -> None:
        """Lista el batch completo antes de empezar, con sus páginas y 0 %.

        El planificador puede arrancar varios archivos a la vez, así que las
        filas no pueden aparecer a medida que cada uno empieza: la lista se
        muestra entera desde el principio y cada fila avanza por su cuenta.
        """
        self._clear_times()
        if not paths:
            return
        self._show_file_progress_rows(True)
        for index, path in enumerate(paths):
            row = self._make_file_row()
            self._set_row_name(row, path.name, str(path))
            self._set_row_pages(row, 0, self._pages_of_file(index))
            self._file_rows[index] = row
        self.times_vbox.addStretch()

    def _pages_of_file(self, index: int) -> int:
        """Páginas previstas del archivo ``index`` (0-based), 0 si no se sabe."""
        if 0 <= index < len(self._file_page_counts):
            return self._file_page_counts[index]
        return 0

    def _ensure_file_rows(self, total: int) -> None:
        """Crea una fila por archivo en el panel de tiempos (idempotente)."""
        if total == 0 or len(self._file_rows) == total:
            return
        self._show_file_progress_rows(True)
        _clear_layout(self.times_vbox)
        self._file_rows = {}
        self._row_ms = {}
        self._row_started = {}
        for index in range(total):
            self._file_rows[index] = self._make_file_row()
            self._set_row_pages(
                self._file_rows[index], 0, self._pages_of_file(index)
            )
        self.times_vbox.addStretch()

    def _set_file_page_counts(self, slices: list[FileSlice]) -> None:
        """Páginas previstas de cada archivo de la ejecución, ya recortadas."""
        self._file_page_counts = [item.count or 0 for item in slices]

    def _on_file_progress(self, index: int, done: int, total: int) -> None:
        """Avance real del archivo ``index`` (1-based), venga de donde venga.

        El planificador reparte páginas de un archivo o archivos completos
        según el tamaño del batch, y antes solo la primera estrategia movía
        las barras: la global se repartía en orden de archivo, así que con
        varios PDF en vuelo se llenaban los de arriba mientras avanzaban
        otros. Con el avance por archivo las dos estrategias se ven igual.
        """
        self._file_pages_done[index] = done
        self._file_pages_total[index] = total
        self._paint_file_row(index)

    def _paint_file_row(self, index: int) -> None:
        """Fila del archivo: el texto cuenta páginas y la barra, todo su trabajo.

        La barra suma la comprobación de VOID del archivo (la reservada o la
        ya conocida), así que no llega al 100 % con esa etapa pendiente ni
        vuelve a empezar cuando arranca.
        """
        row = self._file_rows.get(index - 1)
        if row is None:
            return
        pages = self._file_pages_total.get(index) or self._pages_of_file(index - 1)
        done = self._file_pages_done.get(index, 0)
        self._set_row_pages(row, done, pages)
        review_total = self._review_units_of_file(index)
        if review_total > 0 and pages:
            review_done = self._review_weight * self._review_done.get(index, 0)
            row["bar"].setValue(round(
                (min(done, pages) + review_done) * 100 / (pages + review_total)
            ))

    def _review_units_of_file(self, index: int) -> float:
        """Unidades de página que la comprobación de VOID suma a un archivo."""
        if index in self._review_total:
            return self._review_weight * self._review_total[index]
        if self._review_reserved and index not in self._files_closed:
            return (
                self._review_weight * self._review_ratio
                * self._pages_of_file(index - 1)
            )
        return 0.0

    def _review_units(self) -> tuple[float, float]:
        """Unidades de página (hechas, previstas) de la comprobación de VOID."""
        done = self._review_weight * sum(self._review_done.values())
        indices = set(self._review_total) | set(
            range(1, len(self._file_page_counts) + 1)
        )
        total = sum(self._review_units_of_file(index) for index in indices)
        return done, total

    def _expected_units(self, pages: int) -> float:
        """Trabajo previsto de ``pages`` páginas, con su comprobación de VOID."""
        return pages * (1.0 + self._review_weight * self._review_ratio)

    def _reset_review_progress(self, reserve: bool) -> None:
        self._review_done = {}
        self._review_total = {}
        self._files_closed = set()
        self._file_pages_done = {}
        self._file_pages_total = {}
        self._review_reserved = reserve
        self._last_bar = 0

    def _paint_progress(self, total_pages: int) -> None:
        """Barra del batch en unidades de página: lectura y comprobación de VOID.

        La parte prevista de VOID se reserva desde el principio y se ajusta
        cuando cada archivo dice cuántas hojas lleva, así que la barra no se
        llena con esa etapa pendiente ni retrocede cuando empieza.
        """
        review_done, review_total = self._review_units()
        maximum = max(1, round(total_pages + review_total))
        value = min(
            maximum,
            max(self._last_bar, round(self._done_global + review_done)),
        )
        self._last_bar = value
        if self.progress.maximum() != maximum:
            self.progress.setRange(0, maximum)
        self.progress.setValue(value)

    def _on_review_progress(self, index: int, done: int, total: int) -> None:
        """Avance de la comprobación de VOID de un archivo (hojas, no páginas)."""
        self._review_total[index] = max(0, total)
        self._review_done[index] = max(
            self._review_done.get(index, 0), min(done, total)
        )
        self._paint_file_row(index)
        if self._total_global > 0:
            self._paint_progress(self._total_global)

    # ── Logging ─────────────────────────────────────────────────────────

    def _attach_logger(self) -> None:
        from loguru import logger as lg

        self._log_handler_id = lg.add(
            self._log_sink,
            level="INFO",
            format="{time:HH:mm:ss} | {level} | {message}",
            enqueue=True,
        )
        self._log_sink.message.connect(self._on_log_message)
        logger.info("GUI iniciada")

    def _detach_logger(self) -> None:
        """Retira el destino de esta ventana antes de destruir sus widgets."""
        handler_id = self._log_handler_id
        self._log_handler_id = None
        if handler_id is None:
            return
        logger.remove(handler_id)
        self._log_sink.message.disconnect(self._on_log_message)

    def _on_log_message(self, message: str) -> None:
        """Acumula líneas de log y las descarga por batches: la GUI nunca
        procesa una señal Qt por mensaje ni un append por línea."""
        line = str(message).strip()
        if not line:
            return
        self._log_buffer.append(line)
        if len(self._log_buffer) >= 500:
            self._flush_log_buffer()

    def _flush_log_buffer(self) -> None:
        if not self._log_buffer:
            return
        batch = self._log_buffer
        self._log_buffer = []
        self.log_view.setUpdatesEnabled(False)
        try:
            for line in batch:
                self.log_view.append(line)
        finally:
            self.log_view.setUpdatesEnabled(True)
            scrollbar = self.log_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    # ── Entrada y estimación ────────────────────────────────────────────

    def _load_default_input(self) -> None:
        folder = SCRIPT_DIR / "input"
        if not folder.is_dir():
            self._set_input_paths([])
            return
        # La carpeta de lo ya procesado existe desde el primer arranque, no
        # desde la primera ejecución: es donde van a parar los archivos al
        # terminar, y así se ve de entrada dónde se guardan en vez de
        # aparecer un día sin avisar.
        try:
            ensure_dir(folder / PROCESSED_DIRNAME)
        except OSError as exc:  # noqa: BLE001 - la entrada sigue sirviendo
            logger.warning(f"No se pudo preparar input/{PROCESSED_DIRNAME}: {exc}")
        pdfs = sorted(
            p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"
        )
        self._set_input_paths(pdfs)
        if pdfs:
            logger.info(f"Entrada por defecto: {len(pdfs)} archivo(s) de {folder}")

    def _set_input_paths(self, paths: list[Path]) -> None:
        self._pdf_paths = []
        self._preprocess_geometry = {}
        self._preprocessed_active = False
        seen: set[str] = set()
        for p in paths:
            p = Path(p)
            key = str(p.resolve())
            if p.exists() and p.suffix.lower() == ".pdf" and key not in seen:
                seen.add(key)
                self._pdf_paths.append(p)
        self._refresh_input_summary()
        # Leer la entrada se va a un hilo. Es una pasada sola (el DPI y el
        # recuento de páginas salen del mismo handle) pero abre cada PDF, y
        # hacerlo aquí era lo que dejaba la ventana en «no responde» nada más
        # aparecer y otra vez al elegir archivos. Mientras llega, la ventana
        # ya está en pie: se ven los nombres y la vista previa se pide igual,
        # que también se renderiza aparte.
        self._start_input_scan()
        self._preview_selected_input()

    def _preview_selected_input(self) -> None:
        """Muestra la entrada seleccionada antes de iniciar el procesamiento.

        El render se realiza en ``PreviewLoader`` para que seleccionar PDFs no
        espere al OCR ni bloquee la interfaz. La misma vista se sustituye por
        la página solicitada por el resultado del procesamiento cuando este
        termina.
        """
        if not self._pdf_paths:
            self._preview_pending = None
            self._preview_pdf = None
            self._preview_page = 1
            self._preview_total = 0
            self._preview_source_pixmap = None
            self._preview_base_image = None
            self._set_preview_documents([])
            self._preview_zoom = 1.0
            self._show_preview_placeholder(_PREVIEW_EMPTY_MESSAGE)
            self._update_preview_zoom_controls()
            self._update_preview_nav()
            return
        self._preview_source_pixmap = None
        self._preview_base_image = None
        self._preview_zoom = 1.0
        self._show_preview_placeholder("Cargando vista previa…")
        self._update_preview_zoom_controls()
        self._show_preview_page(1, self._pdf_paths[0])

    def _show_preview_placeholder(self, text: str) -> None:
        """Muestra un mensaje legible ocupando toda la superficie del visor."""
        self.preview_label.setPixmap(QPixmap())
        if text == _PREVIEW_EMPTY_MESSAGE:
            self.preview_label.setTextFormat(Qt.TextFormat.RichText)
            self.preview_label.setText(_preview_empty_html())
        else:
            self.preview_label.setTextFormat(Qt.TextFormat.PlainText)
            self.preview_label.setText(text)
        # Al mostrar una página se fija el tamaño del QLabel al de la imagen.
        # Hay que quitar ese límite antes de volver al estado de texto, o el
        # mensaje puede quedar en un recuadro pequeño o con el tamaño de la
        # página anterior.
        self.preview_label.setMaximumSize(QSize(16777215, 16777215))
        viewport_size = self.preview_scroll.viewport().size()
        if viewport_size.width() > 0 and viewport_size.height() > 0:
            self.preview_label.setFixedSize(viewport_size)
        else:
            self.preview_label.setMinimumSize(
                self._density.preview_min_width, self._density.preview_min_height
            )

    def _resize_preview_placeholder(self) -> None:
        """Mantiene el mensaje del visor al tamaño disponible al redimensionar."""
        if self._preview_source_pixmap is not None or not self.preview_label.text():
            return
        viewport_size = self.preview_scroll.viewport().size()
        if viewport_size.width() > 0 and viewport_size.height() > 0:
            self.preview_label.setFixedSize(viewport_size)

    @staticmethod
    def _document_key(path: Path) -> str:
        """Identidad de un PDF para comparar documentos entre sí."""
        return str(Path(path).resolve()).casefold()

    def _known_page_count(self, path: Path) -> int | None:
        """Páginas de un PDF si ya se contaron, sin volver a abrirlo.

        La detección de DPI cuenta las páginas de toda la entrada y el visor
        las conserva por documento. Reabrir un PDF solo para repetir ese
        recuento cuesta en el hilo de la interfaz, que es donde se nota.
        """
        key = self._document_key(path)
        for known, count in zip(
            self._preview_document_keys, self._preview_document_counts
        ):
            if known == key and count > 0:
                return count
        for known, count in zip(self._pdf_paths, self._input_page_counts):
            if self._document_key(known) == key and count > 0:
                return count
        return None

    def _set_preview_documents(
        self, paths: list[Path], counts: list[int] | None = None
    ) -> None:
        """Actualiza la secuencia global de PDFs sin perder el activo.

        ``counts`` permite reutilizar un recuento de páginas ya hecho (el de
        la detección de DPI) en vez de reabrir cada PDF solo para contarlas.
        Con todos los recuentos dados no se abre ningún PDF, y por eso el
        lector de PDFs se importa solo si hace falta: traerlo arrastra
        PyMuPDF, NumPy y OpenCV, un cuarto de segundo que la ventana pagaba
        en el arranque sin usarlo para nada.
        """
        known: dict[str, int] = {}
        if counts is not None and len(counts) == len(paths):
            known = {str(Path(path)): count for path, count in zip(paths, counts)}

        unique: list[Path] = []
        document_counts: list[int] = []
        keys: list[str] = []
        seen: set[str] = set()
        for path in paths:
            path = Path(path)
            key = self._document_key(path)
            if path.is_file() and path.suffix.lower() == ".pdf" and key not in seen:
                seen.add(key)
                unique.append(path)
                keys.append(key)
                count = known.get(str(path))
                if count is None:
                    try:
                        from app.vision.pdf_loader import page_count

                        count = max(0, page_count(path))
                    except Exception:  # noqa: BLE001 - el visor omite PDFs inválidos
                        count = 0
                document_counts.append(max(0, count))
        self._preview_documents = unique
        self._preview_document_counts = document_counts
        # Las claves se calculan una vez: ``resolve()`` toca el disco y la
        # navegación las consultaba una vez por documento en cada página.
        self._preview_document_keys = keys
        active = (
            self._document_key(self._preview_pdf)
            if self._preview_pdf is not None
            else ""
        )
        if active and active not in seen:
            self._preview_pdf = None
            self._preview_page = 1
            self._preview_total = 0
        self._update_preview_nav()

    def _preview_document_index(self) -> int:
        """Posición del PDF activo dentro de la secuencia, o ``-1``."""
        if self._preview_pdf is None:
            return -1
        current = self._document_key(self._preview_pdf)
        try:
            return self._preview_document_keys.index(current)
        except ValueError:
            return -1

    def _preview_global_page(self) -> int:
        """Posición de la página actual dentro de todos los PDFs."""
        index = self._preview_document_index()
        if index < 0:
            return 0
        count = self._preview_document_counts[index]
        if count <= 0:
            return 0
        offset = sum(self._preview_document_counts[:index])
        return offset + min(max(1, self._preview_page), count)

    def _preview_location(self, global_page: int) -> tuple[Path, int] | None:
        """Convierte una página global en (PDF, página local)."""
        total = sum(self._preview_document_counts)
        if total <= 0:
            return None
        remaining = min(max(1, global_page), total)
        for path, count in zip(
            self._preview_documents, self._preview_document_counts
        ):
            if remaining <= count:
                return path, remaining
            remaining -= count
        return None

    def _sync_page_edit(self, texto: str) -> None:
        """Pone el número en la caja salvo que se esté escribiendo en ella.

        La navegación se refresca sola muchas veces mientras se mira una
        ejecución: la tabla se llena por tandas, la lectura de la entrada
        trae los totales, cada render acaba aquí. Si ese refresco pisa lo que
        la persona lleva tecleado, el salto se hace a la página que ya estaba
        y parece que el selector no obedece.
        """
        if self.page_edit.hasFocus() and self.page_edit.isModified():
            return
        self.page_edit.setText(texto)
        self.page_edit.setModified(False)

    def _jump_to_page_number(self) -> None:
        # Cerrar la ventana con el cursor dentro de la caja saca el foco de
        # ella, y eso emite un último ``editingFinished`` cuando el hilo de
        # render ya se soltó: pedir la página ahí revienta contra un objeto
        # que Qt ya destruyó.
        if self._preview_pdf is None or self._torn_down:
            return
        try:
            global_page = int(self.page_edit.text())
        except ValueError:
            self.page_edit.setModified(False)
            self._sync_page_edit(str(self._preview_global_page()))
            return
        location = self._preview_location(global_page)
        if location is None:
            return
        self.page_edit.setModified(False)
        self._show_preview_page(location[1], location[0])

    def _open_fleet_editor(self) -> None:
        dialog = FleetEditorDialog(FleetStore(SCRIPT_DIR / FLEET_FILENAME), self)
        if dialog.exec():
            logger.info(f"Lista de flota actualizada: {SCRIPT_DIR / FLEET_FILENAME}")

    def _open_important_fields(self) -> None:
        columns = self._table_columns or self._columns_for_template_preview()
        if not columns:
            QMessageBox.information(
                self,
                "Columnas importantes",
                "Cargue o procese un CSV para seleccionar sus columnas.",
            )
            return
        dialog = ImportantFieldsDialog(
            columns, self._current_important_columns(columns), self
        )
        # El cuadro solo pudo enseñar estas columnas, así que solo de ellas
        # puede decir si quedaron marcadas o no.
        dialog.selectionChanged.connect(
            lambda marcadas: self._set_important_columns(marcadas, columns)
        )
        dialog.exec()

    def _set_important_columns(
        self, columns: set[str], scope: list[str] | None = None
    ) -> None:
        """Aplica y recuerda la selección hecha en el selector."""
        self._important_fields_user_selected = True
        self._selected_important_columns = self._important_fields_store.save(
            self._template_key(), columns, scope
        )
        self._apply_csv_table_view()
        self._apply_preview_overlay()

    def _template_key(self) -> str | None:
        """Nombre de la plantilla bajo el que se recuerda la selección."""
        template = self._processed_template or self._load_template()
        return template.name if template is not None else None

    def _current_important_columns(
        self, columns: list[str] | None = None
    ) -> set[str]:
        """Columnas marcadas, o el conjunto por defecto si nunca se editó."""
        if self._important_fields_user_selected:
            return set(self._selected_important_columns)
        if columns is None:
            columns = self._table_columns or self._columns_for_template_preview()
        return self._default_important_columns(columns)

    def _restore_important_columns(self) -> None:
        """Recupera del disco la selección guardada para la plantilla activa."""
        stored = self._important_fields_store.load(self._template_key())
        self._important_fields_user_selected = stored is not None
        self._selected_important_columns = (
            set(stored) if stored is not None else self._current_important_columns()
        )
        self._apply_csv_table_view()

    def _current_important_field_ids(self, template: Template) -> set[str] | None:
        """Campos de la plantilla que corresponden a las columnas marcadas."""
        columns = self._table_columns or self._columns_for_template_preview(template)
        if not columns:
            return None  # sin columnas conocidas manda ``required``
        return template_field_ids_for_columns(
            self._current_important_columns(columns),
            [field.id for field in template.fields],
            columns,
        )

    def _columns_for_template_preview(
        self, template: Template | None = None
    ) -> list[str]:
        template = template or self._processed_template or self._load_template()
        if template is None:
            return []
        signature_ids = {
            field.id for field in template.fields if field.type.value == "signature"
        }
        return CsvReporter.columns_for_fields(
            [field.id for field in template.fields],
            skip_ids=frozenset(signature_ids),
        )

    def _default_important_columns(self, columns: list[str]) -> set[str]:
        """Incluye los identificadores y campos críticos disponibles."""
        return default_important_columns(columns)

    def _start_input_scan(self) -> None:
        """Manda a leer la entrada en un hilo y deja la ventana usable.

        Hasta que el hilo conteste no se sabe cuántas páginas hay y no se
        puede procesar. Son unas décimas y la ventana las pasa viva.
        """
        from app.gui.worker import InputScanWorker

        self._input_scan_generation += 1
        anterior = self._input_scan_worker
        if anterior is not None:
            try:
                if anterior.isRunning():
                    # El recorrido anterior es de otros archivos: lo que
                    # devuelva ya no vale, y su generación lo descarta.
                    anterior.requestInterruption()
            except RuntimeError:
                pass
        self._detected_dpis = {}
        # Provisionales, todos a cero: el visor los toma como sabidos y no
        # reabre ningún PDF por su cuenta mientras el hilo hace su pasada.
        self._input_page_counts = [0] * len(self._pdf_paths)
        self._set_preview_documents(self._pdf_paths, self._input_page_counts)
        if not self._pdf_paths:
            self._input_scan_worker = None
            self._input_scanning = False
            self._refresh_estimate()
            self._refresh_run_buttons()
            return
        self._input_scanning = True
        self._refresh_estimate()
        self._refresh_run_buttons()
        worker = InputScanWorker(
            self._pdf_paths, self._input_scan_generation, self
        )
        worker.scanned.connect(self._on_input_scanned)
        worker.finished.connect(worker.deleteLater)
        self._input_scan_worker = worker
        worker.start()

    def _on_input_scanned(self, generacion: int, leido: list) -> None:
        """Recoge lo que el hilo leyó de la entrada, si sigue siendo la actual."""
        if generacion != self._input_scan_generation:
            return
        self._input_scanning = False
        self._input_scan_worker = None
        self._detected_dpis = {}
        self._input_page_counts = []
        for ruta, dpi, paginas in leido:
            if dpi:
                self._detected_dpis[str(Path(ruta).resolve())] = int(dpi)
            self._input_page_counts.append(int(paginas))
        if self._pdf_paths:
            self._detected_dpi = self._detected_dpis.get(
                str(self._pdf_paths[0].resolve()), 200
            )
        self._set_preview_documents(self._pdf_paths, self._input_page_counts)
        # La vista previa se pidió antes de saber el total, así que su
        # navegación decía «de 0»; ahora ya se puede numerar.
        if self._preview_pdf is not None:
            total = self._known_page_count(self._preview_pdf)
            if total:
                self._preview_total = total
        self._update_preview_nav()
        self._refresh_estimate()
        self._refresh_run_buttons()

    def esperar_lectura_de_entrada(self, ms: int = 30000) -> bool:
        """Bloquea hasta que la entrada esté leída y aplicada.

        La lectura es asíncrona a propósito: la ventana no espera por ella.
        Quien sí necesita los números ya puestos (las pruebas, y cualquier
        recorrido sin interfaz) llama aquí, que espera al hilo y deja que la
        señal se entregue antes de volver.
        """
        worker = self._input_scan_worker
        if worker is not None:
            try:
                worker.wait(ms)
            except RuntimeError:
                pass
        QApplication.processEvents()
        return not self._input_scanning

    def _refresh_run_buttons(self) -> None:
        """Habilita procesar y preprocesar solo con la entrada ya leída.

        No los enciende por su cuenta si hay trabajo en marcha: cambiar de
        archivos mientras se procesa no debe devolver el botón de procesar,
        que ese trabajo apagó.
        """
        if self._closing:
            return
        listo = not self._input_scanning and not self._trabajo_en_marcha()
        for boton in (
            getattr(self, "btn_process", None),
            getattr(self, "btn_preprocess", None),
            getattr(self, "btn_automatico", None),
        ):
            if boton is not None:
                boton.setEnabled(listo)

    def _trabajo_en_marcha(self) -> bool:
        """¿Hay OCR, preprocesado o exportación corriendo ahora mismo?"""
        for worker in (
            self._worker, self._preprocess_worker, self._outputs_worker,
        ):
            if worker is None:
                continue
            try:
                if worker.isRunning():
                    return True
            except RuntimeError:
                # El objeto C++ ya se destruyó tras ``deleteLater``.
                continue
        return False

    def _refresh_input_summary(self) -> None:
        n = len(self._pdf_paths)
        if not n:
            self.input_edit.setText("")
            return
        names = ", ".join(p.name for p in self._pdf_paths)
        self.input_edit.setText(names)
        self.input_edit.setToolTip("\n".join(str(p) for p in self._pdf_paths))

    def _page_range(self) -> PageRange:
        """La interfaz procesa siempre el batch completo."""
        return PageRange()

    def _batch_total_pages(self) -> int:
        """Páginas de toda la entrada, antes de aplicar el rango."""
        return sum(self._input_page_counts)

    def _batch_slices(self) -> list[FileSlice]:
        """Tramos de la entrada que el rango deja dentro."""
        return slice_batch(
            self._pdf_paths, self._input_page_counts, self._page_range()
        )

    def _resolved_paths(self) -> list[Path]:
        """Archivos que aportan al menos una página al rango elegido."""
        return [item.path for item in self._batch_slices()]

    def _refresh_estimate(self) -> None:
        if self._input_scanning:
            # Todavía no se sabe cuántas páginas trae cada archivo, así que
            # no hay tiempo que estimar. Se dice, en vez de dejar el hueco
            # vacío o (peor) anunciar cero páginas.
            count = len(self._pdf_paths)
            unit = "archivo" if count == 1 else "archivos"
            self.estimate_label.setText(
                f"Leyendo {count} {unit} de la entrada…"
            )
            return
        slices = self._batch_slices()
        pages = total_pages(slices)
        if pages and self._ms_per_page:
            seconds = self._expected_units(pages) * self._ms_per_page / 1000.0
            page_unit = "página" if pages == 1 else "páginas"
            file_count = len(slices)
            file_unit = "archivo" if file_count == 1 else "archivos"
            self.estimate_label.setText(
                f"Tiempo estimado: {formato_reloj(seconds)} · "
                f"{pages} {page_unit} · {file_count} {file_unit}"
            )
        elif slices:
            self.estimate_label.setText("Estimación no disponible")
        elif self._pdf_paths:
            self.estimate_label.setText("La entrada no contiene páginas")
        else:
            self.estimate_label.setText(
                "Seleccione los archivos PDF que desea procesar."
            )

    # ── Acciones ────────────────────────────────────────────────────────

    def _refresh_templates(self) -> None:
        manager = TemplateManager()
        paths = manager.list_templates_with_fallback()
        with QSignalBlocker(self.template_combo):
            self.template_combo.clear()
            for path in paths:
                self.template_combo.addItem(path.stem, str(path))

    def _browse_pdfs(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Seleccionar archivos", str(SCRIPT_DIR / "input"), "PDF (*.pdf)"
        )
        if paths:
            self._set_input_paths([Path(p) for p in paths])
            logger.info(f"Entrada seleccionada: {len(paths)} archivo(s)")

    def _browse_template(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Seleccionar plantilla", str(SCRIPT_DIR / "template"),
            "JSON (*.json)"
        )
        if path:
            self._template_path = Path(path)
            self.template_combo.addItem(Path(path).stem, str(path))
            self.template_combo.setCurrentIndex(self.template_combo.count() - 1)
            logger.info(f"Plantilla seleccionada: {path}")

    def _clear_input_folder(self) -> None:
        """Mueve todos los archivos de input/ a la Papelera tras confirmar."""
        processing = self._worker is not None and self._worker.isRunning()
        exporting = (
            self._outputs_worker is not None and self._outputs_worker.isRunning()
        )
        if processing or exporting:
            QMessageBox.warning(
                self,
                "Procesamiento en curso",
                "No se puede vaciar input/ mientras se procesan archivos "
                "o se generan salidas.",
            )
            return

        folder = SCRIPT_DIR / "input"
        if not folder.is_dir():
            QMessageBox.information(
                self, "Vaciar input", "La carpeta input/ no existe."
            )
            return

        files = sorted(path for path in folder.iterdir() if path.is_file())
        if not files:
            QMessageBox.information(
                self, "Vaciar input", "La carpeta input/ ya está vacía."
            )
            return

        answer = QMessageBox.warning(
            self,
            "Confirmar vaciado",
            f"Se moverán {len(files)} archivo(s) de input/ a la "
            "Papelera de reciclaje.\n\n¿Desea continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        moved, failed = send_to_trash(files)
        self._load_default_input()
        logger.info(
            f"Archivos enviados a la Papelera: {len(moved)}; fallidos: {len(failed)}"
        )

        if failed:
            details = "\n".join(f"- {path.name}: {error}" for path, error in failed)
            QMessageBox.warning(
                self,
                "Vaciado incompleto",
                f"Se movieron {len(moved)} de {len(files)} archivo(s).\n\n"
                "No se pudieron mover:\n" + details,
            )
        else:
            QMessageBox.information(
                self,
                "Vaciado completado",
                f"{len(moved)} archivo(s) movido(s) a la Papelera de reciclaje.",
            )

    def _clear_output_folder(self) -> None:
        """Mueve todo el contenido de output/ a la Papelera tras confirmar."""
        processing = self._worker is not None and self._worker.isRunning()
        preprocessing = (
            self._preprocess_worker is not None
            and self._preprocess_worker.isRunning()
        )
        exporting = (
            self._outputs_worker is not None and self._outputs_worker.isRunning()
        )
        if processing or preprocessing or exporting:
            QMessageBox.warning(
                self,
                "Procesamiento en curso",
                "No se puede vaciar output/ mientras se procesan archivos "
                "o se generan salidas.",
            )
            return

        folder = SCRIPT_DIR / "output"
        if not folder.is_dir():
            QMessageBox.information(
                self, "Vaciar output", "La carpeta output/ no existe."
            )
            return

        contents = sorted(folder.iterdir(), key=lambda path: path.name.lower())
        if not contents:
            QMessageBox.information(
                self, "Vaciar output", "La carpeta output/ ya está vacía."
            )
            return

        answer = QMessageBox.warning(
            self,
            "Confirmar vaciado",
            f"Se moverán {len(contents)} elemento(s) de output/ a la "
            "Papelera de reciclaje. Esto incluye todas las ejecuciones "
            "exportadas.\n\n¿Desea continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        moved, failed = send_to_trash(contents)
        self._corrida_dir = None
        logger.info(
            f"Contenido de output enviado a la Papelera: {len(moved)}; "
            f"fallidos: {len(failed)}"
        )

        if failed:
            details = "\n".join(
                f"- {path.name}: {error}" for path, error in failed
            )
            QMessageBox.warning(
                self,
                "Vaciado incompleto",
                f"Se movieron {len(moved)} de {len(contents)} elemento(s).\n\n"
                "No se pudieron mover:\n" + details,
            )
        else:
            QMessageBox.information(
                self,
                "Vaciado completado",
                f"{len(moved)} elemento(s) de output/ movido(s) a la "
                "Papelera de reciclaje.",
            )

    def _open_template_editor(self) -> None:
        """Abre el editor visual usando el mismo Python de la aplicación."""
        editor_script = SCRIPT_DIR / "run_editor.py"
        if not editor_script.is_file():
            QMessageBox.warning(
                self,
                "Editor no disponible",
                f"No se encontró el editor de plantillas:\n{editor_script}",
            )
            return

        try:
            result = QProcess.startDetached(
                sys.executable,
                [str(editor_script)],
                str(SCRIPT_DIR),
            )
            started = result[0] if isinstance(result, (tuple, list)) else result
        except Exception as exc:  # noqa: BLE001 - acción no crítica
            logger.error(f"No se pudo abrir el editor de plantillas: {exc}")
            started = False

        if not started:
            QMessageBox.warning(
                self,
                "Editor no disponible",
                "No se pudo iniciar el editor de plantillas.",
            )
            return
        logger.info("Editor de plantillas abierto")

    def _open_csv_viewer(self) -> None:
        """Abre el visor de ejecuciones como una ventana independiente."""
        if self._csv_viewer is None:
            # Independiente también a nivel nativo: parentarla a la principal
            # hace que Windows oculte su botón de la barra de tareas.
            self._csv_viewer = CsvViewerWindow(SCRIPT_DIR / "output")
            self._csv_viewer.setWindowIcon(self.windowIcon())
        self._csv_viewer.show()
        self._csv_viewer.raise_()
        self._csv_viewer.activateWindow()

    def _separator_value(self) -> list[str] | None:
        """Devuelve las claves para generar_pdfs según las casillas."""
        return self.export_options.separar_por()

    def _csv_date_mode(self) -> str:
        """Política reversible usada únicamente al representar el CSV."""
        return self.export_options.csv_date_mode()

    def _on_csv_date_mode_changed(self, _index: int) -> None:
        """Actualiza tabla y CSV existente sin reprocesar OCR ni PDFs."""
        if not self._reports:
            return
        self._populate_table(self._reports)
        if self._outputs_worker is not None and self._outputs_worker.isRunning():
            self._pending_csv_refresh = True
            self.status_label.setText(
                "Cambio de fecha CSV en cola…"
            )
            return
        self._rewrite_current_csv()

    def _rewrite_current_csv(self) -> None:
        """Reescribe solo el CSV de la ejecución con la política seleccionada."""
        if not self._reports or self._corrida_dir is None:
            return
        template = self._processed_template or self._load_template()
        if template is None:
            logger.warning("No se pudo actualizar el CSV: falta la plantilla")
            return
        run_dir = Path(self._corrida_dir)
        csv_path = run_dir / "datos" / f"{run_dir.name}.CSV"
        try:
            from app.reports.dual_csv import write_minimal_csv
            from app.reports.outputs import complete_csv_path, marcar_revision

            full_csv_path = complete_csv_path(csv_path)
            importantes = self._important_columns_for_export(template)
            # La fecha decide si la página se queda sin ``End Date``, y con
            # ello si va a REVISAR: la columna ``review`` se vuelve a
            # resolver antes de reescribir para no contradecir a la entrega.
            marcar_revision(
                self._reports,
                template,
                date_mode=self._csv_date_mode(),
                important_columns=importantes,
            )
            CsvReporter().write(
                self._reports,
                full_csv_path,
                template,
                date_mode=self._csv_date_mode(),
            )
            write_minimal_csv(full_csv_path, csv_path, importantes)
        except Exception as exc:  # noqa: BLE001 - actualización opcional
            logger.error(f"No se pudo actualizar la fecha del CSV: {exc}")
            self.status_label.setText("Error al actualizar la fecha del CSV.")
            return
        self.status_label.setText(
            f"CSV actualizado: {self.csv_date_mode_combo.currentText()}"
        )
        logger.info(
            f"CSV actualizado sin OCR ({self._csv_date_mode()}): {csv_path}"
        )

    def _export_options(
        self,
        reuse_dir: bool = False,
        skip_pdfs: bool = False,
    ) -> OutputOptions:
        """Captura opciones y datos de la ejecución sin tocar el OCR.

        Args:
            reuse_dir: Si ``True`` (re-export), las salidas se escriben
                sobre la carpeta de la ejecución actual (``self._corrida_dir``)
                en vez de crear una carpeta nueva.
            skip_pdfs: Si ``True`` (ejecución cancelada), se guardan solo
                los datos (CSV, JSON, stats) sin generar PDFs.
        """
        template = self._processed_template or self._load_template()
        if template is None:
            raise ValueError("No hay una plantilla válida para exportar")
        from app.reports.outputs import OutputOptions

        return OutputOptions(
            template=template,
            output_root=SCRIPT_DIR / "output",
            dpi=self._processed_dpi or self._detected_dpi,
            crop_padding=(
                self._config.crop_padding if self._config is not None else 0.01
            ),
            separar_por=tuple(self._separator_value() or ()),
            un_solo_pdf=self.export_options.un_solo_pdf(),
            paginas_por_parte=self._paginas_por_parte(),
            discrepancias=self.discrepancias_check.isChecked(),
            errores=self.export_options.errores_check.isChecked(),
            # "Visualizar campos" pertenece únicamente a la vista previa.
            debug=False,
            run_dir=self._corrida_dir if reuse_dir else None,
            skip_pdfs=skip_pdfs,
            csv_date_mode=self._csv_date_mode(),
            read_day=self._csv_date_mode() != CSV_DATE_MONTH_END,
            important_csv_columns=tuple(
                self._important_columns_for_export(template)
            ),
        )

    def _paginas_por_parte(self) -> int:
        """Tope de páginas por parte, o cero si la entrega no se reparte."""
        grupo = self.export_options
        if not (
            self.export_options.un_solo_pdf()
            and grupo.partes_check.isChecked()
        ):
            return 0
        return int(grupo.partes_spin.value())

    def _important_columns_for_export(self, template: Template) -> list[str]:
        """Columnas del CSV mínimo, independientes del dataset completo."""
        columns = CsvReporter.columns_for_fields(
            [field.id for field in template.fields],
            skip_ids=frozenset(
                field.id
                for field in template.fields
                if field.type.value == "signature"
            ),
        )
        selected = self._current_important_columns(columns)
        return [column for column in columns if column in selected]

    def _load_template(self) -> Template | None:
        selected = self.template_combo.currentData()
        path = Path(selected) if selected else None
        if path is None or not path.exists():
            return None
        try:
            modified = path.stat().st_mtime_ns
            cached = self._template_cache
            if (
                cached is not None
                and cached[0] == path
                and cached[1] == modified
            ):
                return cached[2]
            template = TemplateManager().load(path)
            self._template_cache = (path, modified, template)
            return template
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"No se pudo cargar la plantilla: {exc}")
            return None

    def _current_processing_config(self) -> AppConfig:
        """Captura las opciones compartidas por preprocesamiento y OCR."""
        return AppConfig(
            dpi=200,
            deskew=_DEFAULT_DESKEW,
            align=_DEFAULT_ALIGN,
            ocr_engine="paddle",
            ocr_lang="en",
            date_engine_name="",
            ocr_rec_model="PP-OCRv5_mobile_rec",
            ocr_det_model="PP-OCRv6_medium_det",
            remove_printed=True,  # mapa del fondo impreso: siempre activo
            crop_preprocess=_DEFAULT_CROP_PREPROCESS,
            date_slot_ocr=False,
            date_dynamic_geometry=True,
            # A fin de mes el día no llega a ninguna salida, así que no se
            # lee: son tres recortes de OCR por página que nadie mira.
            read_day=self._csv_date_mode() != CSV_DATE_MONTH_END,
            verify_fleet=self.fleet_check.isChecked(),
            fleet_file=SCRIPT_DIR / FLEET_FILENAME,
            book_matriculas_file=SCRIPT_DIR / "book_matriculas.json",
            book_fechas_file=SCRIPT_DIR / "book_fechas.json",
        )

    def _start_preprocessing(self) -> None:
        """Ejecuta solo el preprocesamiento y actualiza la vista previa."""
        if not self._pdf_paths:
            QMessageBox.warning(
                self, "Aviso", "Seleccione al menos un PDF o use la carpeta input/."
            )
            return
        if (
            self._worker is not None and self._worker.isRunning()
        ) or (
            self._preprocess_worker is not None
            and self._preprocess_worker.isRunning()
        ) or (
            self._outputs_worker is not None and self._outputs_worker.isRunning()
        ):
            return

        slices = self._batch_slices()
        if not slices:
            QMessageBox.warning(self, "Aviso", self._empty_range_message())
            return
        resolved = [item.path for item in slices]

        self._config = self._current_processing_config()
        self._preprocess_geometry = {}
        self._preprocessed_active = False
        # El rango numera el batch completo, así que el worker recibe la
        # entrada entera y lo reparte él: recortar antes lo renumeraría.
        worker = PreprocessWorker(
            self._pdf_paths,
            Path(self.template_combo.currentData()),
            self._config,
            page_range=self._page_range(),
            reference_page=self._reference_page,
            parent=self,
        )
        self._preprocess_worker = worker
        worker.progress.connect(self._on_preprocess_progress)
        worker.page_ready.connect(self._on_preprocessed_page)
        worker.succeeded.connect(self._on_preprocess_succeeded)
        worker.failed.connect(self._on_preprocess_failed)
        worker.finished.connect(self._on_preprocess_thread_finished)
        worker.finished.connect(worker.deleteLater)

        self.btn_process.setEnabled(False)
        self.btn_preprocess.setEnabled(False)
        self.btn_automatico.setEnabled(False)
        self.btn_export.setEnabled(False)
        self._rearmar_cancelar()
        total = total_pages(slices)
        self._set_file_page_counts(slices)
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        self._total_global = total
        self._done_global = 0
        self._reset_review_progress(reserve=False)
        self._run_started = time.monotonic()
        self._spinner_active = True
        self._timer.start()
        self.status_label.setText("Preprocesando…")
        estimate = total * self._ms_per_page / 1000.0 if total else None
        self._set_time_summary(0.0, estimate, estimate)
        worker.start()

    def _empty_range_message(self) -> str:
        """Explica por qué la entrada completa no tiene páginas utilizables."""
        total = self._batch_total_pages()
        if not total:
            return "No hay archivos para procesar."
        return "La entrada no contiene páginas procesables."

    def _confirm_discard_results(self) -> bool:
        """Pide confirmación antes de borrar de la vista una ejecución previa.

        Los archivos guardados en ``output/`` no se tocan; lo que se pierde es
        la tabla, la vista previa y el avance por archivo en pantalla.
        """
        if not self._reports:
            return True
        answer = QMessageBox.question(
            self,
            "Procesar de nuevo",
            "Ya hay un procesamiento en pantalla.\n\n"
            "Al procesar de nuevo se borran de la vista la tabla, la vista "
            "previa y el avance por archivo de la ejecución actual. Los "
            "archivos ya guardados en output/ no se borran.\n\n"
            "¿Desea continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _clear_results_display(self) -> None:
        """Deja la pantalla sin resultados, sin tocar lo escrito en disco."""
        self._table_timer.stop()
        self._table_pending = []
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self._update_duplicate_summary([])
        self._sync_depurar_button()
        self.csv_columns_toggle.setEnabled(False)
        self.csv_columns_toggle.setVisible(False)
        self._clear_times()

    def _start_automatico(self) -> None:
        """Procesa y sigue solo hasta donde diga «Automatización…».

        No repite lo que ya hacen los botones sueltos: arranca el mismo
        procesamiento y se limita a marcar que, al terminar cada paso, el
        siguiente empieza sin esperar a que nadie pulse nada. Si el
        procesamiento no llega a arrancar (falta la entrada, la plantilla, o
        se rechaza descartar la ejecución anterior) la marca se retira y
        todo queda como estaba.
        """
        self._auto_en_marcha = True
        self.cadena.reiniciar()
        self._start_processing()
        if self._worker is None or not self._worker.isRunning():
            self._auto_en_marcha = False
            return
        # El pipeline empieza por la calibración: endereza y alinea el batch
        # entero antes de leer la primera página. Ese tramo es «preprocesar»,
        # y el paso siguiente lo enciende ``_on_progress`` en cuanto llega la
        # primera página contada.
        self.cadena.marcar(
            pasos_automaticos.PREPROCESAR, pasos_automaticos.EN_CURSO
        )
        pasos = self._pasos_automaticos()
        logger.info(f"Proceso automático: {pasos}")
        self.status_label.setText(f"Procesando… ({pasos})")

    def _pasos_automaticos(self) -> str:
        """Los pasos elegidos, en una línea, para la bitácora y el estado.

        Se leen de la misma línea de pasos que se enseña debajo de la barra,
        para que la bitácora no pueda contar una cadena distinta de la que
        se está mirando.
        """
        return " > ".join(
            pasos_automaticos.NOMBRES_CORTOS[paso].lower()
            for paso in pasos_automaticos.RECORRIDO
            if self.cadena.elegido(paso)
        )

    def _cortar_automatico(self, motivo: str = "") -> None:
        """Suelta la cadena para que el paso siguiente no arranque solo."""
        if not self._auto_en_marcha:
            return
        self._auto_en_marcha = False
        # La línea de pasos se queda con el paso cortado en rojo. Es la
        # única forma de saber después dónde se detuvo: el estado de abajo
        # lo pisa el mensaje siguiente y la bitácora hay que ir a leerla.
        self.cadena.cortar()
        if motivo:
            logger.info(f"Proceso automático interrumpido: {motivo}")

    def _seguir_automatico(self, contexto: str | None) -> None:
        """Arranca el eslabón que toca tras una escritura de salidas.

        Se llama con el hilo de salidas ya libre, no al escribirlas: el
        paso siguiente vuelve a escribir sobre la misma ejecución y con el
        anterior todavía en marcha se habría descartado sin hacer nada.
        """
        if not self._auto_en_marcha:
            return
        if self._last_run_cancelled or not self._reports:
            self._cortar_automatico("la ejecución quedó cancelada")
            return
        if contexto == "proceso":
            # Un batch que se lee entero sin pasar por la calibración (sin
            # alineación, o tan corto que no llega ningún aviso de página)
            # deja el primer paso en curso: aquí ya está terminado.
            self.cadena.marcar(
                pasos_automaticos.PREPROCESAR, pasos_automaticos.HECHO
            )
            self.cadena.marcar(
                pasos_automaticos.PROCESAR, pasos_automaticos.HECHO
            )
            self.cadena.marcar(
                pasos_automaticos.EXPORTAR, pasos_automaticos.EN_CURSO
            )
            self._exportar()
            return
        if contexto == "export":
            # Aquí se acaba lo que hace esta ventana. Lo que siga es de
            # AirVault, y esa ventana tiene su propia cadena y su bitácora,
            # pero manda aquí su avance para que la línea de pasos siga
            # contando hasta el final.
            self.cadena.marcar(
                pasos_automaticos.EXPORTAR, pasos_automaticos.HECHO
            )
            self._auto_en_marcha = False
            if self._automatizacion.subir:
                self._subir_automatico()

    def _subir_automatico(self) -> None:
        """Manda la entrega recién exportada a la ventana de AirVault."""
        if self._airvault_corrida is None:
            logger.info(
                "Proceso automático: no hay ejecución exportada que subir"
            )
            return
        self._open_airvault()
        ventana = self._airvault_window
        if ventana is None:
            return
        if ventana.corrida() != self._airvault_corrida:
            ventana.fijar_corrida(self._airvault_corrida)
        ventana.subir_automaticamente()

    def _start_processing(self) -> None:
        if not self._pdf_paths:
            QMessageBox.warning(
                self, "Aviso", "Seleccione al menos un PDF o use la carpeta input/."
            )
            return
        template = self._load_template()
        if template is None:
            QMessageBox.warning(self, "Aviso", "Seleccione una plantilla válida.")
            return
        if (self._worker is not None and self._worker.isRunning()) or (
            self._preprocess_worker is not None
            and self._preprocess_worker.isRunning()
        ) or (
            self._outputs_worker is not None and self._outputs_worker.isRunning()
        ):
            return

        slices = self._batch_slices()
        if not slices:
            QMessageBox.warning(self, "Aviso", self._empty_range_message())
            return
        resolved = [item.path for item in slices]
        if not self._confirm_discard_results():
            return

        self._config = self._current_processing_config()
        # Asociar la plantilla y el DPI al resultado evita que una edición de
        # controles durante el procesamiento cambie la exportación posterior.
        self._processed_template = template
        self._processed_dpi = self._config.dpi
        self._reports = None
        self._preview_results = {}
        self._corrida_dir = None
        self._pending_export = False
        self._pending_csv_refresh = False
        self._last_run_cancelled = False

        effective_threads = self._effective_threads()
        workers, threads = recommended_parallelism(effective_threads)

        # Igual que en el preprocesado: el worker recibe el batch completo y
        # aplica el rango, que está numerado sobre toda la entrada.
        self._worker = PipelineWorker(
            self._pdf_paths,
            Path(self.template_combo.currentData()),
            self._config,
            page_range=self._page_range(),
            reference_page=self._reference_page,
            workers=workers,
            cpu_threads=threads,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.file_started.connect(self._on_file_started)
        self._worker.file_progress.connect(self._on_file_progress)
        self._worker.review_progress.connect(self._on_review_progress)
        self._worker.file_finished.connect(self._on_file_finished)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        # Sin esto el hilo terminado quedaba retenido hasta la ejecución
        # siguiente, con todos los reportes del batch dentro.
        self._worker.finished.connect(self._on_pipeline_thread_finished)
        self._worker.finished.connect(self._worker.deleteLater)

        self.btn_process.setEnabled(False)
        self.btn_preprocess.setEnabled(False)
        self.btn_automatico.setEnabled(False)
        self.btn_export.setEnabled(False)
        self._rearmar_cancelar()
        total = total_pages(slices)
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        self._clear_results_display()

        self._total_global = total
        self._set_file_page_counts(slices)
        self._prepare_file_rows(resolved)
        self._done_global = 0
        self._last_done = 0
        self._reset_review_progress(reserve=True)
        self._paint_progress(total)
        self._run_started = time.monotonic()
        self._spinner_active = True
        self._timer.start()
        self.status_label.setText("Procesando…")
        estimate = (
            self._expected_units(self._total_global) * self._ms_per_page / 1000.0
        )
        self._set_time_summary(0.0, estimate, estimate)

        logger.info(
            f"Iniciando procesamiento: {len(resolved)} archivo(s), "
            f"{self._total_global} página(s) ({self._page_range().label()}), "
            f"200 DPI base / hasta 600 DPI en fechas por PDF, "
            f"{effective_threads} hilos efectivos "
            f"({workers} worker(s) x {threads})"
        )
        self._worker.start()

    # ── Slots ───────────────────────────────────────────────────────────

    _CANCELAR_AYUDA = (
        "Detener el procesamiento; las páginas ya leídas se guardan en el CSV"
    )

    def _rearmar_cancelar(self) -> None:
        """Devuelve el botón a «Cancelar» al empezar un trabajo nuevo."""
        self._cancel_pedido = False
        self.btn_cancel.setText("Cancelar")
        self.btn_cancel.setToolTip(self._CANCELAR_AYUDA)
        self.btn_cancel.setEnabled(True)

    def _request_cancel(self) -> None:
        """Pide la cancelación del pipeline; repetida, la corta en seco.

        La cancelación ordenada deja terminar las páginas que ya estaban
        leyéndose, y con páginas grandes eso tarda. El botón se queda por
        eso disponible: la segunda pulsación ofrece cortar sin esperarlas,
        que es la diferencia entre una espera larga y una ventana que
        parece colgada.
        """
        worker = self._worker
        message = "Cancelando… (las páginas en vuelo terminan y se guardan)"
        if worker is None or not worker.isRunning():
            worker = self._preprocess_worker
            message = "Cancelando preprocesamiento…"
        if worker is None or not worker.isRunning():
            return
        if self._cancel_pedido:
            if not self._confirmar_corte(
                "Cancelar a la fuerza",
                "La cancelación está esperando a que terminen las páginas "
                "en curso.",
            ):
                return
            self._cortar_trabajo_en_curso()
            self.btn_cancel.setEnabled(False)
            self.status_label.setText(
                "Cancelando sin esperar a las páginas en curso…"
            )
            return
        self._cancel_pedido = True
        # Cancelar es cancelar la entrega, no solo el paso en curso: sin
        # esto la cadena habría seguido exportando y subiendo lo que se
        # acababa de pedir detener.
        self._cortar_automatico("se canceló el procesamiento")
        worker.requestInterruption()
        self.btn_cancel.setText("Cancelar sin esperar")
        self.btn_cancel.setToolTip(
            "Cortar la lectura ahora mismo. Las páginas que se estaban "
            "leyendo se pierden; las ya leídas se conservan."
        )
        self.status_label.setText(
            f"{message}. Vuelva a pulsar para no esperar."
        )

    def _on_preprocess_progress(
        self, done: int, total: int, message: str
    ) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self._done_global = done
        self.status_label.setText(message)

    def _on_preprocessed_page(
        self, pdf_path: str, page_number: int, geometry
    ) -> None:
        """Guarda la geometría de una página y la muestra si está en pantalla.

        Se guardan unos pocos flotantes por página en vez de la imagen: el
        visor rasteriza bajo demanda y aplica esta misma geometría, igual que
        hace con las páginas ya procesadas.
        """
        key = (pdf_path, page_number)
        self._preprocess_geometry[key] = geometry
        if self._preview_pdf is not None and key == (
            str(self._preview_pdf),
            self._preview_page,
        ):
            self._preview_loader.requested.emit(page_number, pdf_path, geometry)

    def _on_preprocess_succeeded(self, cancelled: bool) -> None:
        elapsed = (
            time.monotonic() - self._run_started
            if self._run_started is not None
            else None
        )
        self._preprocessed_active = bool(self._preprocess_geometry)
        self._timer.stop()
        self._run_started = None
        self._spinner_active = False
        self.busy_label.setText("")
        self.btn_process.setEnabled(True)
        self.btn_preprocess.setEnabled(True)
        self.btn_automatico.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_export.setEnabled(bool(self._reports))
        self._sync_depurar_button()
        self.progress.setRange(0, max(1, self._total_global))
        self.progress.setValue(
            self._total_global if not cancelled else self._done_global
        )
        self._set_time_summary(
            elapsed,
            0.0 if not cancelled else None,
            elapsed if not cancelled else None,
        )
        self.status_label.setText(
            "Preprocesamiento cancelado."
            if cancelled
            else "Preprocesamiento terminado. Puede revisar las páginas."
        )

    def _on_preprocess_failed(self, message: str) -> None:
        elapsed = (
            time.monotonic() - self._run_started
            if self._run_started is not None
            else None
        )
        self._timer.stop()
        self._run_started = None
        self._spinner_active = False
        self.busy_label.setText("")
        self.btn_process.setEnabled(True)
        self.btn_preprocess.setEnabled(True)
        self.btn_automatico.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self._set_time_summary(elapsed, None, None)
        self.status_label.setText("Preprocesamiento con errores.")
        logger.error(f"Fallo de preprocesamiento: {message}")
        QMessageBox.critical(self, "Error de preprocesamiento", message)

    def _on_preprocess_thread_finished(self) -> None:
        self._preprocess_worker = None

    def _on_pipeline_thread_finished(self) -> None:
        self._worker = None

    def _on_file_started(self, index: int, total: int, name: str) -> None:
        """Activa la fila del archivo en curso en el panel de tiempos."""
        self._show_file_progress_rows(True)
        self._ensure_file_rows(total)
        row = self._file_rows.get(index - 1)
        if row is None:
            return
        font = row["name"].font()
        font.setBold(True)
        row["name"].setFont(font)
        self._set_row_name(row, name)
        self._set_row_pages(row, 0, self._pages_of_file(index - 1))
        row["secs"].setText("…")
        self._row_started[index - 1] = time.monotonic()
        self._current_file_index = index

    def _on_file_finished(self, index: int, report) -> None:
        """Cierra la fila del archivo con su tiempo real."""
        # Terminado el archivo, su comprobación de VOID ya no está pendiente:
        # la reserva deja su sitio a lo que de verdad llevó.
        self._files_closed.add(index)
        review_pages = getattr(report, "review_pages", 0) or 0
        if review_pages or index in self._review_total:
            total = max(review_pages, self._review_total.get(index, 0))
            self._review_total[index] = total
            self._review_done[index] = total
        row = self._file_rows.get(index - 1)
        if row is not None:
            font = row["name"].font()
            font.setBold(False)
            row["name"].setFont(font)
            self._row_ms[index - 1] = report.processing_ms
            self._set_row_pages(row, len(report.pages), len(report.pages))
            row["secs"].setText(formato_reloj(report.processing_ms / 1000.0))
        if self._current_file_index == index:
            self._current_file_index = 0
        if self._total_global > 0:
            self._paint_progress(self._total_global)

    def _on_timer_tick(self) -> None:
        if self._spinner_active:
            self.busy_label.setText(_SPINNER[self._spinner_idx % len(_SPINNER)])
            self._spinner_idx += 1
        elif self.busy_label.text():
            self.busy_label.setText("")
        if self._run_started is None:
            return
        elapsed = time.monotonic() - self._run_started
        remaining = None
        total = None
        if self._total_global > 0:
            # Lo pendiente incluye la comprobación de VOID: sin ella el
            # restante caía a cero al leer la última página, con esa etapa
            # todavía por delante.
            review_done, review_total = self._review_units()
            remaining = estimate_remaining_seconds(
                total_pages=self._total_global + review_total,
                completed_pages=self._done_global + review_done,
                elapsed_seconds=elapsed,
                cached_ms_per_page=self._ms_per_page,
            )
            total = elapsed + remaining
        self._set_time_summary(elapsed, remaining, total)
        # Reloj en vivo de cada archivo abierto: repartiendo un PDF por
        # proceso hay varios corriendo a la vez, no solo el último iniciado.
        for index, started in self._row_started.items():
            if index in self._row_ms:
                continue
            row = self._file_rows.get(index)
            if row is not None:
                row["secs"].setText(formato_reloj(time.monotonic() - started))

    def _on_progress(self, done: int, total: int, message: str) -> None:
        """Pinta el avance del batch: barra, contador del texto y ETA.

        El total es el del batch, fijado al arrancar la ejecución con los
        recuentos que la ventana ya tenía: el que llega en la señal es el del
        documento en curso o el de los archivos ya vistos, y encogía la barra
        al cambiar de archivo. Y el contador solo puede subir, porque con una
        docena de páginas en vuelo los avisos llegan desordenados.
        """
        total = self._total_global or total
        if total > 0:
            done = max(min(done, total), self._last_done)
            self._last_done = done
            self._done_global = done
            self._paint_progress(total)
        self.status_label.setText(with_page_counter(done, total, message))
        # La calibración avisa con done=0 (es una etapa, no páginas leídas);
        # la primera página contada es la señal de que el OCR ya empezó y de
        # que el preprocesamiento quedó atrás. ``marcar`` da por terminados
        # los pasos anteriores que estuvieran en curso.
        if (
            self._auto_en_marcha
            and done > 0
            and self.cadena.estado(pasos_automaticos.PROCESAR)
            != pasos_automaticos.EN_CURSO
        ):
            self.cadena.marcar(
                pasos_automaticos.PROCESAR, pasos_automaticos.EN_CURSO
            )

    def _on_succeeded(self, reports: list[ValidationReport]) -> None:
        elapsed = (
            time.monotonic() - self._run_started
            if self._run_started is not None
            else None
        )
        self._timer.stop()
        self._reports = reports
        self._preview_results = {
            (str(Path(report.pdf_path).resolve()), page.page_number): page
            for report in reports
            for page in report.pages
        }
        # Los recuentos ya los tiene la ventana desde la detección de DPI: sin
        # pasarlos, el visor reabría cada PDF del batch para volver a contarlos.
        processed = [Path(report.pdf_path) for report in reports]
        self._set_preview_documents(
            processed, [self._known_page_count(path) for path in processed]
        )
        self._corrida_dir = None
        self._pending_export = False
        # El botón Exportar queda disponible en cuanto el OCR termina, sin
        # esperar a que termine la generación de salidas de fondo: re-exportar
        # es independiente y se encola si ya hay una generación en curso.
        self.btn_process.setEnabled(False)
        self.btn_preprocess.setEnabled(False)
        self.btn_automatico.setEnabled(False)
        self.btn_export.setEnabled(True)
        self._sync_depurar_button()
        self.btn_cancel.setEnabled(False)
        self._update_performance(reports, elapsed)
        self._refresh_estimate()

        cancelled_any = any(getattr(r, "cancelled", False) for r in reports)
        if elapsed is not None:
            self._set_time_summary(
                elapsed,
                None if cancelled_any else 0.0,
                None if cancelled_any else elapsed,
            )
        self._run_started = None

        self._last_run_cancelled = cancelled_any
        total_pages = sum(len(r.pages) for r in reports)
        ok = sum(r.summary.get("ok_pages", 0) for r in reports)
        warn = sum(r.summary.get("warning_pages", 0) for r in reports)
        err = sum(r.summary.get("error_pages", 0) for r in reports)
        blank = sum(r.summary.get("blank_pages", 0) for r in reports)
        # Reloj de la ejecución, no la suma de relojes: con un proceso por
        # archivo los tiempos se solapan y sumarlos daba un total imposible,
        # varias veces mayor que lo que el usuario esperó. Se prefiere el
        # cronómetro de la ventana, que es el que estuvo a la vista.
        total_ms = (
            elapsed * 1000 if elapsed is not None
            else CsvReporter.run_wall_ms(reports)
        )
        calib_ms = sum(r.calibration_ms for r in reports)
        summary = (
            f"OK: {ok} | WARNING: {warn} | ERROR: {err} | "
            f"En blanco: {blank} | "
            f"Calibración: {calib_ms / 1000:.2f} s + "
            f"Procesado: {max(0.0, total_ms - calib_ms) / 1000:.2f} s = "
            f"Total: {total_ms / 1000:.2f} s"
        )
        state = "cancelado" if cancelled_any else "terminado"
        logger.info(
            f"Procesamiento {state} "
            f"({len(reports)} archivos, {total_pages} páginas). "
            f"{summary}"
        )

        if cancelled_any:
            # La ejecución se guarda hasta donde se canceló (CSV/JSON/stats),
            # sin generar PDFs; la pantalla queda limpia para poder
            # procesar los archivos restantes.
            self._clear_results_display()
            self.status_label.setText(
                "Procesamiento cancelado: guardando resultados parciales…"
            )
            # El rango de páginas no se toca: para seguir donde se cortó hay
            # que ajustarlo a mano antes de volver a procesar.
            self._timer.start()
            self._start_outputs(reports, context="proceso", skip_pdfs=True)
            return

        self._populate_table(reports)
        self._populate_times(reports)
        if reports and reports[0].pages:
            self._show_preview_page(
                reports[0].pages[0].page_number, Path(reports[0].pdf_path)
            )
        # Solo se guardan los datos. Los PDFs son la entrega, y componerlos
        # vuelve a abrir cada original y tarda tanto como para no imponerlo a
        # quien todavía va a cambiar la separacion: se hacen al exportar.
        self.status_label.setText("Guardando datos…")
        self._timer.start()
        self._start_outputs(reports, context="proceso", skip_pdfs=True)

    def _start_outputs(
        self,
        reports: list[ValidationReport],
        context: str,
        skip_pdfs: bool = False,
    ) -> None:
        """Inicia la escritura de salidas sin bloquear el hilo de la GUI.

        Args:
            context: ``"proceso"`` (automática tras el OCR) o ``"export"``
                (re-export manual).
            skip_pdfs: Si la ejecución fue cancelada, solo se guardan los
                datos (CSV/JSON/stats) sin generar PDFs.
        """
        if self._outputs_worker is not None and self._outputs_worker.isRunning():
            return

        self._outputs_context = context
        try:
            options = self._export_options(
                reuse_dir=context in ("export", "depurar"),
                skip_pdfs=skip_pdfs,
            )
        except Exception as exc:  # noqa: BLE001 - se muestra en la GUI
            self._on_outputs_failed(str(exc))
            self._on_outputs_thread_finished()
            return

        worker = OutputsWorker(reports, options, parent=self)
        self._outputs_worker = worker
        worker.succeeded.connect(self._on_outputs_written)
        worker.failed.connect(self._on_outputs_failed)
        worker.progress.connect(self._on_outputs_stage)
        worker.finished.connect(self._on_outputs_thread_finished)
        worker.finished.connect(worker.deleteLater)
        self.btn_process.setEnabled(False)
        self.btn_preprocess.setEnabled(False)
        self.btn_automatico.setEnabled(False)
        self.btn_export.setEnabled(False)
        # No pasa por _sync_depurar_button: el hilo todavía no arrancó y
        # desde ahí seguiría pareciendo que no hay ninguna escritura en curso.
        self.btn_depurar.setEnabled(False)
        self._spinner_active = True
        # La barra general representa siempre páginas del batch; la exportación
        # no debe sustituir ese total por una barra indeterminada.
        self.progress.setRange(0, max(1, self._total_global))
        self.progress.setValue(self._done_global or self._total_global)
        worker.start()

    def _on_outputs_stage(self, message: str, percent: int) -> None:
        """Actualiza la barra y el estado con la fase de exportación."""
        self.progress.setRange(0, max(1, self._total_global))
        self.progress.setValue(self._done_global or self._total_global)
        self.status_label.setText(f"Generando salidas… {message}")

    def _on_outputs_written(self, output_dir: Path) -> None:
        """Actualiza la interfaz cuando termina una exportación."""
        self._corrida_dir = Path(output_dir)
        # El indexado trabaja sobre el CSV mínimo de la ejecución recién
        # escrita, que es el que lleva las columnas que van a AirVault.
        self._airvault_corrida = (
            Path(output_dir) / "datos" / f"{Path(output_dir).name}.CSV"
        )
        # Se actualiza una ventana libre, nunca una que tenga un batch en
        # vuelo. Las demás ejecuciones continúan en paralelo.
        for ventana in reversed(self._airvault_windows):
            if ventana.hilo() is None:
                ventana.fijar_corrida(self._airvault_corrida)
                break
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        if self._outputs_context == "export":
            self.status_label.setText(f"Exportación terminada: {output_dir.name}")
            logger.info(f"Exportación completada en: {output_dir}")
        elif self._outputs_context == "depurar":
            self.status_label.setText(
                "Páginas eliminadas de la ejecución. Exporte para rehacer los "
                "PDF sin ellas."
            )
            logger.info(f"Ejecución depurada en: {output_dir}")
        elif self._last_run_cancelled:
            self.status_label.setText(
                "Procesamiento cancelado: resultados parciales guardados "
                f"en {output_dir.name} (sin PDF)."
            )
            logger.info(f"Ejecución cancelada guardada (datos sin PDF) en: {output_dir}")
        else:
            # Los archivos ya dieron todo lo que tenían que dar: el OCR está
            # hecho y los datos escritos. Se apartan aquí, y la ventana
            # reapunta los resultados a su nueva ruta, así que exportar
            # después sigue encontrando las páginas originales.
            self._archive_processed_inputs(Path(output_dir))
            self.status_label.setText(
                "Procesamiento terminado. Puede cambiar la separación y exportar."
            )
            logger.info(f"Outputs generados en: {output_dir}")

    def _archive_processed_inputs(self, run_dir: Path | None = None) -> None:
        """Saca de input/ los PDF de la ejecución que acaba de terminar.

        La entrada queda con lo que falta por procesar y nada más; lo hecho
        se guarda en ``input/processed``. La ventana sigue apuntando a los
        archivos allí, así que la vista previa y volver a exportar siguen
        funcionando sin que el usuario tenga que buscarlos.
        """
        moved, failed = archive_processed_files(
            [Path(report.pdf_path) for report in self._reports],
            SCRIPT_DIR / "input",
        )
        for path, error in failed:
            logger.warning(
                f"No se pudo apartar {path.name} a "
                f"input/{PROCESSED_DIRNAME}: {error}"
            )
        if not moved:
            return
        self._remap_document_paths(moved)
        if run_dir is not None:
            json_path = (
                Path(run_dir) / "datos" / f"{Path(run_dir).name}.json"
            )
            try:
                JsonReporter.relocate_consolidated_sources(json_path, moved)
            except (OSError, ValueError, TypeError) as exc:
                # La ejecución sigue siendo válida; el visor conserva su
                # búsqueda histórica por nombre y cantidad de páginas.
                logger.warning(
                    f"No se pudieron guardar las rutas definitivas de los "
                    f"PDF procesados en {json_path.name}: {exc}"
                )
        logger.info(
            f"{len(moved)} archivo(s) apartados en input/{PROCESSED_DIRNAME}"
        )

    def _remap_document_paths(self, moved: dict[Path, Path]) -> None:
        """Reapunta a su nueva ruta todo lo que la ventana guarda por archivo.

        Los resultados, la vista previa y las filas de la tabla se identifican
        por la ruta del PDF. Sin actualizarlas, apartar los archivos dejaría
        la vista previa en blanco y el re-export buscando donde ya no hay nada.
        """
        by_key = {
            self._document_key(source): destination
            for source, destination in moved.items()
        }

        def relocated(path) -> Path:
            return by_key.get(self._document_key(path), Path(path))

        for report in self._reports:
            destination = relocated(report.pdf_path)
            if destination != Path(report.pdf_path) and not report.source_name:
                report.source_name = Path(report.pdf_path).name
            report.pdf_path = str(destination)
        self._pdf_paths = [relocated(path) for path in self._pdf_paths]
        self._row_pdfs = [relocated(path) for path in self._row_pdfs]
        # Las filas ya construidas guardan además su PDF en el propio item de
        # la columna ``page``. Esa copia es la que usa el doble clic, incluso
        # después de ordenar la tabla; si conserva la ruta anterior, el visor
        # intenta abrir un archivo que acaba de moverse a ``processed/``.
        try:
            page_column = self._table_columns.index("page")
        except ValueError:
            page_column = -1
        if page_column >= 0:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, page_column)
                if item is None:
                    continue
                stored = item.data(Qt.ItemDataRole.UserRole + 1)
                if stored:
                    item.setData(
                        Qt.ItemDataRole.UserRole + 1,
                        str(relocated(stored)),
                    )
        self._preview_results = {
            (str(relocated(path).resolve()), page): result
            for (path, page), result in self._preview_results.items()
        }
        self._preprocess_geometry = {
            (str(relocated(path)), page): geometry
            for (path, page), geometry in self._preprocess_geometry.items()
        }
        if self._preview_pdf is not None:
            self._preview_pdf = relocated(self._preview_pdf)
        # Los recuentos de páginas ya están hechos y no cambian al mover el
        # archivo: se reutilizan para no reabrir el batch entero. Si de alguno
        # no se sabe, se dejan contar todos: un cero se leería como un PDF sin
        # páginas y dejaría ese documento sin paginación.
        documents = [Path(report.pdf_path) for report in self._reports]
        counts = [self._known_page_count(path) for path in documents]
        self._set_preview_documents(
            documents, counts if all(counts) else None
        )

    def _on_outputs_failed(self, message: str) -> None:
        """Registra un error de salidas sin interrumpir la interfaz."""
        context = self._outputs_context
        self._cortar_automatico("falló la generación de salidas")
        logger.error(f"Error generando outputs: {message}")
        if context in ("export", "depurar"):
            depurando = context == "depurar"
            titulo = (
                "Error al eliminar las páginas" if depurando
                else "Error al exportar"
            )
            self.status_label.setText(f"{titulo}.")
            details = message.splitlines()[0] if message else "Error desconocido"
            QMessageBox.critical(self, titulo, details)
        else:
            self.status_label.setText(
                "Procesamiento terminado con error al generar salidas."
            )

    def _on_outputs_thread_finished(self) -> None:
        """Libera los controles cuando el hilo de salidas ya terminó."""
        self._outputs_worker = None
        # Qué acababa de escribirse decide cuál es el eslabón siguiente de
        # la cadena, y el atributo se limpia aquí mismo.
        contexto = self._outputs_context
        self._outputs_context = None
        self._timer.stop()
        self._spinner_active = False
        self.busy_label.setText("")
        self.progress.setRange(0, 100)
        if self._closing:
            # La ventana está cerrando: ni se reactivan los botones ni se
            # encola otra exportación, que dejaría el cierre sin terminar.
            self._pending_export = False
            self._pending_csv_refresh = False
            self._cortar_automatico("se está cerrando la ventana")
            return
        self.btn_cancel.setEnabled(False)
        self.btn_process.setEnabled(True)
        self.btn_preprocess.setEnabled(True)
        self.btn_automatico.setEnabled(True)
        # Tras una ejecución cancelada no hay Exportar: da la opción de
        # procesar los archivos restantes en vez de hacer PDFs parciales.
        self.btn_export.setEnabled(bool(self._reports) and not self._last_run_cancelled)
        self._sync_depurar_button()
        if self._pending_export and not self._last_run_cancelled:
            self._pending_export = False
            # La reexportación ya escribirá el CSV con la selección más
            # reciente; no se necesita una escritura paralela adicional.
            self._pending_csv_refresh = False
            self._exportar()
        elif self._pending_export:
            self._pending_export = False
        elif self._pending_csv_refresh:
            self._pending_csv_refresh = False
            self._rewrite_current_csv()
        else:
            # La re-exportación encolada ya es el paso que la cadena pedía,
            # así que solo se encadena cuando no había ninguna esperando.
            self._seguir_automatico(contexto)

    def _update_performance(
        self, reports: list[ValidationReport], elapsed_seconds: float | None
    ) -> None:
        """Aprende throughput de pared; no suma tiempos de workers paralelos."""
        pages = sum(len(r.pages) for r in reports)
        review_pages = sum(getattr(r, "review_pages", 0) for r in reports)
        # Una ejecución cancelada no llega a la comprobación de VOID de todos
        # sus archivos: enseñaría que casi ninguna página la necesita.
        if pages and not any(getattr(r, "cancelled", False) for r in reports):
            pages_ms = sum(getattr(r, "pages_ms", 0.0) for r in reports)
            review_ms = sum(getattr(r, "review_ms", 0.0) for r in reports)
            if pages_ms > 0 and review_pages and review_ms > 0:
                weight = (review_ms / review_pages) / (pages_ms / pages)
                self._review_weight = min(max(weight, 0.1), 50.0)
            self._review_ratio = min(1.0, review_pages / pages)
        units = pages + self._review_weight * review_pages
        measured = wall_ms_per_page(elapsed_seconds or 0.0, units)
        if measured is not None:
            self._ms_per_page = max(1.0, measured)
            _save_performance(
                self._ms_per_page, self._review_weight, self._review_ratio
            )

    def _sync_depurar_button(self) -> None:
        """Solo se depura una ejecución ya guardada y sin escrituras en curso.

        Sin carpeta de ejecución la reescritura crearía una segunda entrega de
        lo mismo, así que el botón espera a que la escritura automática del
        procesamiento termine y deje su carpeta.
        """
        escribiendo = (
            self._outputs_worker is not None and self._outputs_worker.isRunning()
        )
        self.btn_depurar.setEnabled(
            bool(self._reports)
            and self._corrida_dir is not None
            and not self._last_run_cancelled
            and not escribiendo
        )

    def _depurar_paginas(self) -> None:
        """Quita de la ejecución las páginas repetidas o en blanco.

        Se reescriben los datos de la ejecución (CSV, JSON y estadísticas) sin
        ellas, igual que en el visor de CSV. Los PDF no se rehacen aquí: son
        la entrega y se componen al exportar, cuando ya no queda nada más que
        quitar.
        """
        if not self._reports or self._corrida_dir is None:
            return
        if self._outputs_worker is not None and self._outputs_worker.isRunning():
            return

        dialog = DepurarPaginasDialog(self._reports, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        # Se borra lo que quedó marcado página por página, no el criterio
        # entero: el cuadro deja conservar una aparición distinta de la
        # primera, y esa elección se perdería al recontar por criterio.
        remaining, quitadas = depurar_claves(self._reports, dialog.claves())
        if not quitadas:
            return
        if not remaining:
            QMessageBox.information(
                self,
                "Depurar páginas",
                "Quedaría una ejecución sin ninguna página. Para deshacerse de "
                "la ejecución entera, elimine su carpeta desde output/.",
            )
            return

        self._reports = remaining
        # La tabla, el contador de duplicados y el visor cuelgan de la lista
        # de reportes: si no se rehacen aquí, la pantalla seguiría enseñando
        # las páginas que acaban de salir de la ejecución.
        self._refresh_after_depuracion()
        logger.info(
            f"Depuradas {quitadas} página(s) de la ejecución "
            f"{Path(self._corrida_dir).name}"
        )
        self.status_label.setText(
            f"Eliminando {quitadas} página(s) de la ejecución…"
        )
        self._timer.start()
        self._start_outputs(remaining, context="depurar", skip_pdfs=True)

    def _refresh_after_depuracion(self) -> None:
        """Rehace tabla, documentos y vista previa con lo que quedó."""
        reports = self._reports or []
        self._preview_results = {
            (str(Path(report.pdf_path).resolve()), page.page_number): page
            for report in reports
            for page in report.pages
        }
        self._populate_table(reports)
        self._populate_times(reports)
        documentos = [Path(report.pdf_path) for report in reports]
        self._set_preview_documents(
            documentos, [self._known_page_count(path) for path in documentos]
        )
        if reports and reports[0].pages:
            self._show_preview_page(
                reports[0].pages[0].page_number, Path(reports[0].pdf_path)
            )

    def _exportar(self) -> None:
        """Regenera CSV, JSON y PDFs con las opciones actuales, sin
        reprocesar los archivos ya analizados."""
        if not self._reports:
            QMessageBox.information(self, "Exportar", "Primero procese los archivos.")
            return
        if self._outputs_worker is not None and self._outputs_worker.isRunning():
            # Ya hay una generación en curso (p. ej. la automática del
            # procesamiento): se encola y se ejecuta apenas termine.
            self._pending_export = True
            self.status_label.setText(
                "Exportación en cola… (termina la generación en curso)"
            )
            logger.info("Re-export en cola hasta que termine la generación en curso")
            return
        # Las casillas se capturan al hacer clic en Exportar, no al procesar:
        # cambiar la separación nunca vuelve a ejecutar OCR. El re-export se
        # escribe sobre la carpeta de la ejecución actual (self._corrida_dir).
        self.status_label.setText("Exportando salidas…")
        self._start_outputs(self._reports, context="export")

    def _on_failed(self, message: str) -> None:
        elapsed = (
            time.monotonic() - self._run_started
            if self._run_started is not None
            else None
        )
        self._timer.stop()
        self._run_started = None
        self._spinner_active = False
        self.busy_label.setText("")
        self.btn_process.setEnabled(True)
        self.btn_preprocess.setEnabled(True)
        self.btn_automatico.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self._cortar_automatico("el procesamiento terminó con errores")
        self._set_time_summary(elapsed, None, None)
        self.status_label.setText("Procesamiento con errores.")
        # Conserva en el panel de tiempos lo que alcanzó a procesarse.
        partial = getattr(self._worker, "reports", None)
        if partial:
            self._populate_times(list(partial))
        logger.error(f"Fallo: {message}")
        QMessageBox.critical(self, "Error de procesamiento", message)

    def _populate_table(self, reports: list[ValidationReport]) -> None:
        """Prepara las filas y las inserta por batches para no congelar la UI.

        El llenado completo de miles de filas bloquea el hilo de interfaz;
        con ``_table_timer`` se inserta un tramo de ``_TABLE_CELL_CHUNK``
        celdas por tick.

        Aquí solo se apunta qué página va en cada fila. Armar los valores
        (``row_for_page`` formatea una columna por campo, más confianza,
        estado, comentario y origen) se hace dentro de cada tramo: hacerlo
        de golpe para todo el batch dejaba la ventana congelada antes de que
        el troceado llegara a empezar.
        """
        self.table.setUpdatesEnabled(False)
        self.table_sort.suspend()
        # Las coincidencias apuntaban a filas de la tabla anterior; dejarlas
        # llevaría el visor a una página que ya no es la que se encontró.
        self._olvidar_busqueda()
        try:
            self.table.setRowCount(0)
            self._row_pdfs = []
            self._classify_discrepancies(reports)
            reporter = CsvReporter()
            fields = reporter.fields_for(reports, self._processed_template)
            columns = reporter.columns_for(reports, self._processed_template)
            duplicates = detect_duplicate_log_pages(reports)
            self._update_duplicate_summary(duplicates)
            duplicate_iter = iter(duplicates)
            # El mismo reparto de tiempo que escribe el CSV: la tabla y el
            # archivo no pueden mostrar dos números distintos por página.
            self._table_reporter = reporter
            self._table_fields = fields
            self._table_time_factor = reporter.run_time_factor(reports)
            # La política de fecha se congela al empezar: cambiarla a mitad
            # del llenado dejaría unas filas con un criterio y otras con otro.
            self._table_date_mode = self._csv_date_mode()
            pending: list[
                tuple[
                    int,
                    ValidationReport,
                    PageResult,
                    DuplicateLogPage,
                ]
            ] = []
            for report in reports:
                pdf_path = Path(report.pdf_path)
                for page in report.pages:
                    duplicate = next(duplicate_iter)
                    self._row_pdfs.append(pdf_path)
                    pending.append(
                        (len(self._row_pdfs) - 1, report, page, duplicate)
                    )

            self.table.setColumnCount(len(columns))
            self.table.setHorizontalHeaderLabels([
                csv_display_name(column) for column in columns
            ])
            self.table.setRowCount(len(pending))
            self._table_columns = columns
            self._table_important_field_ids = {
                field.id
                for field in (self._processed_template.fields
                              if self._processed_template is not None else [])
                if field.required
            }
            self._table_important_field_ids.add(_DUP_COLUMN)
            self._table_important_field_ids.add(_DISC_COLUMN)
            self._table_important_field_ids.add(_REVIEW_COLUMN)
            # La selección guardada se conserva completa: recortarla contra
            # las columnas de esta ejecución perdería las marcas de un CSV con
            # otras columnas la próxima vez que el usuario edite la lista.
            self._selected_important_columns = self._current_important_columns(
                columns
            )
            self.csv_columns_toggle.setEnabled(bool(columns))
            self.csv_columns_toggle.setVisible(bool(columns))
            self._apply_csv_table_view()
            self._table_pending = pending
            if pending:
                self.btn_prev.setEnabled(False)
                self.btn_next.setEnabled(False)
                self._table_timer.start()
            else:
                self.table_sort.reset()
        finally:
            self.table.setUpdatesEnabled(True)

    def _classify_discrepancies(self, reports: list[ValidationReport]) -> None:
        """Marca ``page.discrepancy`` antes de armar las filas de la tabla.

        La columna ``disc`` sale de esa marca. La escritura de salidas la
        calcula también, pero en el hilo de fondo y después de esta tabla: sin
        recorrerla aquí, la pantalla mostraría ``false`` en páginas que el CSV
        marca como discrepancia. Mientras ese hilo corre no se repite, porque
        estaría reescribiendo las mismas marcas que el otro hilo lee.
        """
        if self._processed_template is None:
            return
        if self._outputs_worker is not None and self._outputs_worker.isRunning():
            return
        from app.validation.discrepancias import clasificar_lote

        clasificar_lote(reports, self._processed_template)

    def _update_duplicate_summary(
        self, duplicates: list[DuplicateLogPage]
    ) -> None:
        """Actualiza el contador compacto y su detalle por hover."""
        repeated = [item for item in duplicates if item.duplicate]
        groups: dict[int, list[DuplicateLogPage]] = {}
        for item in duplicates:
            if item.log_number is not None:
                groups.setdefault(item.log_number, []).append(item)
        groups = {
            number: items for number, items in groups.items() if len(items) > 1
        }

        count = len(repeated)
        self.duplicates_label.setText(f"Duplicados: {count}")
        if not count:
            pintar_del_tema(
                self.duplicates_label,
                lambda: f"color: {paleta().TEXT_SECONDARY};",
            )
            self.duplicates_label.setToolTip(
                "No hay números de bitácora repetidos en los archivos "
                "procesados."
            )
            return

        pintar_del_tema(
            self.duplicates_label,
            lambda: f"color: {_COLORS[Status.WARNING]}; font-weight: 600;",
        )
        page_label = (
            "página duplicada" if count == 1 else "páginas duplicadas"
        )
        group_count = len(groups)
        group_label = (
            "número de bitácora"
            if group_count == 1
            else "números de bitácora"
        )
        lines = [f"{count} {page_label} en {group_count} {group_label}:"]
        for number, items in sorted(groups.items()):
            # Ahora que se marcan todas las apariciones, la lista sola no
            # dice cuál se queda al depurar. Se señala la primera, que es la
            # que el descarte automático conserva.
            locations = ", ".join(
                f"{Path(item.pdf_path).name} PDF p. {item.page_number}"
                + (" (se conserva)" if item.primera else "")
                for item in items
            )
            lines.append(
                f"{number:07d} (página {number % 100:02d} del libro): "
                f"{locations}"
            )
        self.duplicates_label.setToolTip("\n".join(lines))

    @staticmethod
    def _review_tooltip(value: str) -> str:
        if value.strip().lower() == "true":
            return (
                "La bitácora va al batch REVISAR: se sube con lo que se "
                "sepa de ella y se termina de indexar a mano."
            )
        return "La bitácora se indexa sola en su batch automático."

    @staticmethod
    def _discrepancy_tooltip(value: str) -> str:
        if value.strip().lower() == "true":
            return (
                "La bitácora quedó marcada por una discrepancia de firmas."
            )
        return "La bitácora cumple las firmas exigidas."

    @staticmethod
    def _duplicate_tooltip(duplicate: DuplicateLogPage) -> str:
        if duplicate.log_number is None:
            return "El número de bitácora está ausente o no es válido."
        if duplicate.duplicate:
            cual = (
                "es la primera de ellas"
                if duplicate.primera
                else "no es la primera"
            )
            return (
                "Este número de bitácora aparece más de una vez, y esta "
                f"página {cual}. Al depurar se "
                "conserva la primera."
            )
        return "Este número de bitácora no se repite."

    def _apply_csv_table_view(self, _checked: bool | None = None) -> None:
        """Alterna la tabla entre valores principales y todas las columnas."""
        apply_csv_column_visibility(
            self.table,
            self._table_columns,
            self._table_important_field_ids,
            self.csv_columns_toggle.isChecked(),
            self._selected_important_columns,
        )

    def _rows_per_chunk(self) -> int:
        """Filas que caben en el presupuesto de celdas de un tick."""
        return max(1, _TABLE_CELL_CHUNK // max(1, len(self._table_columns)))

    def _on_table_chunk(self) -> None:
        if not self._table_pending:
            self._table_timer.stop()
            if self._outputs_worker is not None and self._outputs_worker.isRunning():
                self.status_label.setText("Generando salidas…")
            self._update_preview_nav()
            self.table.viewport().update()
            return
        rows = self._rows_per_chunk()
        batch = self._table_pending[:rows]
        del self._table_pending[:rows]
        for row_index, report, page, duplicate in batch:
            values = self._table_reporter.row_for_page(
                report,
                page,
                self._table_fields,
                date_mode=self._table_date_mode,
                duplicate=duplicate.duplicate,
                time_factor=self._table_time_factor,
            )
            field_results = {field.field_id: field for field in page.fields}
            for col_index, column in enumerate(self._table_columns):
                value = values.get(column, "")
                item = QTableWidgetItem(str(value))
                if column == "page":
                    item.setData(Qt.ItemDataRole.UserRole, int(value))
                    item.setData(
                        Qt.ItemDataRole.UserRole + 1,
                        str(self._row_pdfs[row_index]),
                    )
                field_id = column.removesuffix("_conf")
                field = field_results.get(field_id)
                if column == _REVIEW_COLUMN:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setToolTip(self._review_tooltip(str(value)))
                elif column == _DISC_COLUMN:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setToolTip(self._discrepancy_tooltip(str(value)))
                elif column == _DUP_COLUMN:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setToolTip(self._duplicate_tooltip(duplicate))
                    if duplicate.duplicate:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                        item.setForeground(Qt.GlobalColor.white)
                        item.setBackground(_color_for(Status.WARNING))
                elif field is not None:
                    item.setToolTip(
                        f"Estado: {field.status.value}"
                        + (f"\n{field.comment}" if field.comment else "")
                    )
                    item.setForeground(Qt.GlobalColor.white)
                    item.setBackground(_color_for(field.status))
                self.table.setItem(row_index, col_index, item)
        self.table.viewport().update()
        if self._table_pending:
            done = len(batch)
            total = done + len(self._table_pending)
            self.status_label.setText(f"Construyendo tabla… {done}/{total}")
        else:
            self.table_sort.reset()

    # ── Tiempo por archivo ──────────────────────────────────────────────

    def _clear_times(self) -> None:
        self._show_file_progress_rows(False)
        self._file_rows = {}
        self._row_ms = {}
        self._row_started = {}
        self._current_file_index = 0
        _clear_layout(self.times_vbox)
        for widget in (self.times_container,):
            widget.update()

    def _populate_times(self, reports: list[ValidationReport]) -> None:
        """Estado final del panel: cada archivo al 100 % con sus páginas."""
        _clear_layout(self.times_vbox)
        self._file_rows = {}
        self._row_ms = {}
        self._row_started = {}
        self._current_file_index = 0
        self._show_file_progress_rows(bool(reports))
        if not reports:
            return
        for index, report in enumerate(reports):
            row = self._make_file_row()
            self._set_row_name(
                row, Path(report.pdf_path).name, str(report.pdf_path)
            )
            self._set_row_pages(row, len(report.pages), len(report.pages))
            row["secs"].setText(formato_reloj(report.processing_ms / 1000.0))
            self._file_rows[index] = row
            self._row_ms[index] = report.processing_ms
        self.times_vbox.addStretch()

    def _show_file_progress_rows(self, visible: bool) -> None:
        """Alterna entre la lista integrada y el mensaje vacío."""
        self.times_scroll.setVisible(visible)
        self.empty_times_label.setVisible(not visible)

    # ── Vista previa ───────────────────────────────────────────────────

    def _refresh_preview_template(self, _index: int = -1) -> None:
        """Redibuja las casillas de la plantilla sobre la vista actual.

        Cada plantilla recuerda sus propias columnas importantes, así que al
        cambiarla se recupera la selección guardada para la nueva.
        """
        self._restore_important_columns()
        self._apply_preview_overlay()

    def _on_fields_toggled(self, _checked: bool) -> None:
        self._apply_preview_overlay()

    def _build_search_row(self) -> QHBoxLayout:
        """La misma búsqueda que ofrece el visor de CSV, sobre este batch.

        Las dos ventanas enseñan a la vez la tabla y la página, así que la
        manera de encontrar una bitácora tiene que ser la misma en las dos.
        """
        row = QHBoxLayout()
        row.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Buscar bitácora, matrícula, archivo o página"
        )
        self.search_edit.setMinimumWidth(280)
        self.search_edit.setMaximumWidth(420)
        self.search_edit.setAccessibleName("Texto que se busca en la tabla")
        self.search_edit.setToolTip(
            "Busca en las columnas visibles; con el CSV completo, también en "
            "las ocultas. Cada coincidencia abre su página en la vista "
            "previa."
        )
        self.search_edit.returnPressed.connect(self._buscar_en_la_tabla)
        row.addWidget(self.search_edit)
        self.search_button = QPushButton("Buscar")
        self.search_button.setToolTip(
            "Buscar el texto; repetido, pasa a la coincidencia siguiente"
        )
        self.search_button.clicked.connect(self._buscar_en_la_tabla)
        row.addWidget(self.search_button)
        self.search_prev = QPushButton("‹")
        self.search_prev.setToolTip("Coincidencia anterior")
        self.search_prev.setEnabled(False)
        self.search_prev.clicked.connect(lambda: self._mover_busqueda(-1))
        row.addWidget(self.search_prev)
        self.search_next = QPushButton("›")
        self.search_next.setToolTip("Coincidencia siguiente")
        self.search_next.setEnabled(False)
        self.search_next.clicked.connect(lambda: self._mover_busqueda(1))
        row.addWidget(self.search_next)
        # La pista es una frase larga, y un QLabel pide de ancho mínimo la
        # frase entera: metida en el panel de la tabla, ese mínimo era el que
        # empujaba el separador y dejaba la bitácora en su franja más
        # estrecha. Aquí el texto cede: se recorta antes que robarle sitio a
        # la página, que es lo que de verdad hay que ver. Recortar es de
        # ``ElidedLabel``, que termina la frase en puntos suspensivos y deja
        # la entera en el tooltip; un QLabel a secas la cortaba a media
        # palabra contra el borde de la ventana, en cualquier tamaño.
        self.search_context = ElidedLabel(_PISTA_BUSQUEDA)
        pintar_del_tema(
            self.search_context, lambda: f"color: {paleta().TEXT_SECONDARY};"
        )
        self.search_context.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.search_context.setMinimumWidth(0)
        row.addWidget(self.search_context, 1)

        # El tema queda en el extremo de una fila de consulta, fuera del
        # grupo de botones que inicia o detiene trabajo. Su ancho es fijo para
        # que cambiar el dibujo no mueva ningún control vecino.
        self.tema_toggle = QToolButton()
        self.tema_toggle.setObjectName("themeToggle")
        self.tema_toggle.setCheckable(True)
        self.tema_toggle.setIconSize(ICON_SIZE)
        alto = (
            CONTROL_HEIGHT_COMPACT
            if self._density.compact
            else CONTROL_HEIGHT
        )
        self.tema_toggle.setFixedWidth(alto)
        self.tema_toggle.toggled.connect(self._on_tema_elegido)
        self._actualizar_toggle_tema(tema())
        row.addWidget(self.tema_toggle)
        return row

    def _columnas_buscables(self) -> list[int]:
        """Las columnas que la tabla está mostrando ahora mismo."""
        return [
            index
            for index in range(self.table.columnCount())
            if not self.table.isColumnHidden(index)
        ] or list(range(self.table.columnCount()))

    def _coincidencias_de(self, texto: str) -> list[tuple[int, int]]:
        """Filas que contienen el texto, con la columna donde aparece.

        Primero las que lo tienen completo en una celda y después las que lo
        llevan dentro de un valor más largo: escribir una bitácora entera
        lleva a esa bitácora, no a la primera fila que la mencione de paso.
        """
        texto = texto.casefold()
        columnas = self._columnas_buscables()
        exactas: list[tuple[int, int]] = []
        parciales: list[tuple[int, int]] = []
        for fila in range(self.table.rowCount()):
            for columna in columnas:
                item = self.table.item(fila, columna)
                if item is None:
                    continue
                valor = item.text().strip().casefold()
                if valor == texto:
                    exactas.append((fila, columna))
                    break
                if texto in valor:
                    parciales.append((fila, columna))
                    break
        return exactas + parciales

    def _buscar_en_la_tabla(self) -> None:
        """Busca lo escrito y lleva la tabla y la vista previa a la primera fila."""
        valor = self.search_edit.text().strip()
        if valor and valor.casefold() == self._buscado and self._coincidencias:
            self._mover_busqueda(1)
            return
        self._buscado = valor.casefold()
        self._coincidencias = []
        self._coincidencia = -1
        if not valor:
            self.search_context.setText(_PISTA_BUSQUEDA)
            self._sincronizar_busqueda()
            return
        if self._table_pending or not self.table.rowCount():
            self.search_context.setText(
                "Procese los archivos para buscar en sus bitácoras."
            )
            self._sincronizar_busqueda()
            return
        self._coincidencias = self._coincidencias_de(valor)
        if not self._coincidencias:
            self.search_context.setText(f"«{valor}»: sin coincidencias.")
            self._sincronizar_busqueda()
            return
        self._coincidencia = 0
        self._mostrar_coincidencia()

    def _mover_busqueda(self, salto: int) -> None:
        if not self._coincidencias:
            return
        self._coincidencia = (self._coincidencia + salto) % len(
            self._coincidencias
        )
        self._mostrar_coincidencia()

    def _mostrar_coincidencia(self) -> None:
        fila, columna = self._coincidencias[self._coincidencia]
        if fila >= self.table.rowCount():
            # La tabla se rehizo bajo los pies de la búsqueda.
            self._olvidar_busqueda()
            return
        self.table.selectRow(fila)
        self.table.setCurrentCell(fila, columna)
        item = self.table.item(fila, columna)
        if item is not None:
            self.table.scrollToItem(item)
        self._jump_to_page(fila, columna)
        nombre = (
            self._table_columns[columna]
            if columna < len(self._table_columns)
            else "la tabla"
        )
        self.search_context.setText(
            f"Coincidencia {self._coincidencia + 1} de "
            f"{len(self._coincidencias)} en «{nombre}»"
        )
        self._sincronizar_busqueda()

    def _olvidar_busqueda(self) -> None:
        """Descarta las coincidencias: la tabla que las sostenía ya no está."""
        self._coincidencias = []
        self._coincidencia = -1
        self._buscado = ""
        self.search_context.setText(_PISTA_BUSQUEDA)
        self._sincronizar_busqueda()

    def _sincronizar_busqueda(self) -> None:
        varias = len(self._coincidencias) > 1
        self.search_prev.setEnabled(varias)
        self.search_next.setEnabled(varias)

    def _jump_to_page(self, row: int, _col: int) -> None:
        if self._table_pending:
            return  # la tabla aún se está construyendo
        try:
            page_column = self._table_columns.index("page")
        except ValueError:
            return
        item = self.table.item(row, page_column)
        pdf_value = item.data(Qt.ItemDataRole.UserRole + 1) if item else None
        if item is not None and pdf_value:
            self._show_preview_page(
                int(item.data(Qt.ItemDataRole.UserRole)),
                Path(str(pdf_value)),
            )

    def _update_preview_nav(self) -> None:
        """Sincroniza la paginación global y el indicador de archivo."""
        has_pdf = self._preview_pdf is not None
        global_page = self._preview_global_page()
        global_total = sum(self._preview_document_counts)
        self.btn_prev.setEnabled(has_pdf and global_page > 1)
        self.btn_next.setEnabled(has_pdf and global_page < global_total)
        self._sync_page_edit(str(global_page) if has_pdf else "")
        validator = self.page_edit.validator()
        if isinstance(validator, QIntValidator):
            validator.setTop(max(1, global_total))
        self.page_edit.setEnabled(has_pdf and global_total > 0)
        self.page_total_label.setText(f"de {global_total}")
        index = self._preview_document_index()
        pdf_text = self._preview_pdf.name if has_pdf else "Ninguno"
        self.preview_file_label.setText(pdf_text)
        self.preview_file_label.setToolTip(
            str(self._preview_pdf) if self._preview_pdf is not None else ""
        )
        if has_pdf:
            context = (
                f"Archivo {index + 1} de {len(self._preview_documents)}: "
                f"{pdf_text} · Página {self._preview_page} de "
                f"{self._preview_total}"
            )
        else:
            context = "Sin archivo activo"
        self.preview_context_label.setText(context)

    def _prev_page(self) -> None:
        current = self._preview_global_page()
        if current > 1:
            location = self._preview_location(current - 1)
            if location is not None:
                self._show_preview_page(location[1], location[0])

    def _next_page(self) -> None:
        current = self._preview_global_page()
        if current < sum(self._preview_document_counts):
            location = self._preview_location(current + 1)
            if location is not None:
                self._show_preview_page(location[1], location[0])

    def _draw_template_boxes(
        self,
        pixmap: QPixmap,
        template,
        w: int,
        h: int,
        boxes: dict[str, list[float]] | None = None,
    ) -> None:
        """Dibuja los rectángulos efectivos sobre la página alineada."""
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(0, 120, 215), 2)
        font = QFont()
        # Rótulo sobre la página, no cromo de ventana: va en el tamaño de
        # subtítulo para no taparla, pero sale de la misma escala.
        font.setPointSize(FONT_CAPTION_PT)
        font.setBold(True)
        painter.setFont(font)
        important_only = self.important_fields_check.isChecked()
        important_ids = (
            self._current_important_field_ids(template) if important_only else None
        )
        for field in _visible_preview_fields(template, important_only, important_ids):
            coords = (boxes or {}).get(field.id)
            if coords is None or len(coords) != 4:
                coords = (field.x, field.y, field.w, field.h)
            x, y, width, height = coords
            rect = QRectF(x * w, y * h, width * w, height * h)
            painter.setBrush(QColor(0, 140, 220, 30))
            painter.setPen(pen)
            painter.drawRect(rect)
            painter.setBrush(QColor(0, 140, 220, 160))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawText(
                rect.adjusted(2, 2, -2, -2),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                field.id,
            )
        painter.end()

    def _current_preview_result(self):
        """Resultado de la página visible, si ya fue procesada."""
        if self._preview_pdf is None:
            return None
        key = (str(Path(self._preview_pdf).resolve()), self._preview_page)
        return self._preview_results.get(key)

    def _current_preview_geometry(self) -> dict | None:
        """Transformación que convierte la miniatura cruda en la procesada."""
        page = self._current_preview_result()
        if page is None:
            return None
        return {
            "skew_angle": float(page.skew_angle),
            "alignment": page.preview_alignment,
        }

    def _show_preview_page(
        self, page_number: int, pdf_path: Path | None = None
    ) -> None:
        """Pide la página al hilo de fondo y actualiza la navegación.

        El render ya no corre en el hilo de interfaz; mientras llega la
        imagen se conserva la anterior en pantalla.
        """
        pdf_path = pdf_path or self._preview_pdf
        if pdf_path is None:
            return
        if pdf_path != self._preview_pdf:
            # El total se consulta una sola vez por documento, y solo si no se
            # sabe ya: abrir el PDF aquí bloquea el hilo de la interfaz justo
            # al cambiar de bitácora.
            total = self._known_page_count(pdf_path)
            if total is None and self._input_scanning:
                # El hilo que lee la entrada está a punto de traer ese
                # número. Abrir el PDF aquí para adelantarlo es justo lo que
                # se sacó del hilo de la interfaz; se deja en cero y
                # ``_on_input_scanned`` lo pone.
                total = 0
            elif total is None:
                try:
                    from app.utils.io import resolve_processed_path
                    from app.vision.pdf_loader import page_count

                    total = page_count(resolve_processed_path(Path(pdf_path)))
                except Exception:  # noqa: BLE001 - no crítico
                    total = 0
            self._preview_total = total
        if self._preview_total:
            page_number = max(1, min(page_number, self._preview_total))
        self._preview_page = page_number
        self._preview_pdf = pdf_path
        self._update_preview_nav()
        self._preview_pending = (page_number, str(pdf_path))
        # Tras procesar, la geometría guardada en PageResult refleja exactamente
        # la alineación (incluido el anclaje por batch) usada por el OCR, así que
        # tiene prioridad: el preprocesado previo pudo usar otro anclaje.
        geometry = self._current_preview_geometry()
        if geometry is None and self._preprocessed_active:
            geometry = self._preprocess_geometry.get(
                (str(pdf_path), page_number)
            )
        self._preview_loader.requested.emit(
            page_number, str(pdf_path), geometry
        )

    def _on_preview_ready(
        self, page_number: int, pdf_path: str, qimage: QImage | None
    ) -> None:
        """Aplica el render solo si sigue siendo la página pedida."""
        if (page_number, pdf_path) != self._preview_pending:
            return  # respuesta obsoleta (el usuario ya navegó)
        if qimage is None:
            return
        self._set_preview_qimage(qimage)

    def _set_preview_qimage(self, qimage: QImage) -> None:
        """Guarda el render limpio y le aplica el overlay actual."""
        self._preview_base_image = qimage
        self._apply_preview_overlay()

    def _apply_preview_overlay(self) -> None:
        """Repinta los recuadros sobre el render ya disponible.

        Cambiar qué campos se muestran no cambia la página rasterizada, así
        que el overlay se rehace sin volver a pedirla al hilo de render.
        """
        qimage = self._preview_base_image
        if qimage is None:
            return
        pixmap = QPixmap.fromImage(qimage)
        if self.fields_check.isChecked():
            template = self._processed_template or self._load_template()
            if template is not None:
                page = self._current_preview_result()
                boxes = page.preview_boxes if page is not None else None
                if page is None and self._preview_pending is not None:
                    number, path = self._preview_pending
                    geometry = self._preprocess_geometry.get((path, number), {})
                    boxes = geometry.get("boxes")
                self._draw_template_boxes(
                    pixmap,
                    template,
                    qimage.width(),
                    qimage.height(),
                    boxes=boxes,
                )
        self._preview_source_pixmap = pixmap
        self._render_preview_pixmap()

    def _update_preview_zoom_controls(self) -> None:
        """Sincroniza los controles de zoom con la disponibilidad de una imagen."""
        has_image = self._preview_source_pixmap is not None
        self.btn_zoom_out.setEnabled(has_image and self._preview_zoom > 0.4)
        self.btn_zoom_fit.setEnabled(has_image)
        self.btn_zoom_in.setEnabled(has_image and self._preview_zoom < 4.0)
        self.zoom_label.setText(f"{round(self._preview_zoom * 100)}%")

    def _preview_fit_height(self) -> int:
        """Devuelve la altura disponible para el ajuste vertical."""
        height = self.preview_scroll.viewport().height()
        return max(1, height - 4)

    def _render_preview_pixmap(self) -> None:
        """Escala la imagen fuente sin perder resolución para zoom o resize."""
        source = self._preview_source_pixmap
        if source is None or source.isNull():
            return
        target_height = max(
            1, round(self._preview_fit_height() * self._preview_zoom)
        )
        pixmap = source.scaledToHeight(
            target_height, Qt.TransformationMode.SmoothTransformation
        )
        self.preview_label.setPixmap(pixmap)
        self.preview_label.setFixedSize(pixmap.size())
        self._update_preview_zoom_controls()

    def _fit_preview_vertical(self) -> None:
        """Restablece la vista para mostrar la página completa en vertical."""
        self._preview_zoom = 1.0
        self._render_preview_pixmap()

    def _zoom_preview(self, factor: float) -> None:
        """Aplica zoom relativo; las barras de desplazamiento permiten mover la página."""
        self._preview_zoom = min(4.0, max(0.4, self._preview_zoom * factor))
        self._render_preview_pixmap()

    def keyPressEvent(self, event) -> None:
        """Ctrl++ / Ctrl+- para hacer zoom sobre la vista previa."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            key = event.key()
            if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self._zoom_preview(1.25)
                event.accept()
                return
            if key in (Qt.Key.Key_Minus,):
                self._zoom_preview(0.8)
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
                      activated=lambda f=factor: self._zoom_preview(f))

    def showEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
        self._update_responsive_layout()
        QTimer.singleShot(0, self._balance_bottom_splitter)
        QTimer.singleShot(0, self._balance_content_splitter)
        QTimer.singleShot(0, self._resize_preview_placeholder)

    def resizeEvent(self, event) -> None:
        """Reajusta la vista también cuando cambia el tamaño de la ventana.

        Arrastrar el borde emite un evento por cada píxel, y cada uno reescala
        la página completa con interpolación suave. Se reescala una sola vez,
        cuando el arrastre se detiene: mientras tanto la imagen anterior sigue
        en pantalla, estirada por el propio layout.
        """
        super().resizeEvent(event)
        self._update_responsive_layout()
        self._responsive_timer.start()
        if self._preview_source_pixmap is not None:
            self._resize_preview_timer.start()
        else:
            self._resize_preview_placeholder()

    def _finish_resize_layout(self) -> None:
        """Equilibra una sola vez al terminar una ráfaga de redimensionado."""
        self._balance_bottom_splitter()
        self._balance_content_splitter()
        if self._preview_source_pixmap is None:
            self._resize_preview_placeholder()

    # ── Cierre ──────────────────────────────────────────────────────────

    def _running_workers(self) -> list[QThread]:
        """Hilos de trabajo todavía en marcha, si los hay."""
        running: list[QThread] = []
        # La ventana puede haberse destruido ya: el cierre se aplaza y vuelve
        # a preguntar, y para entonces su objeto de C++ puede no estar.
        indexados = []
        for ventana in self._airvault_windows:
            try:
                indexado = ventana.hilo()
            except RuntimeError:
                indexado = None
            if indexado is not None:
                indexados.append(indexado)
        consulta_web = None
        if self._web_reports_window is not None:
            try:
                consulta_web = self._web_reports_window.hilo()
            except RuntimeError:
                consulta_web = None
        for worker in (
            self._worker, self._preprocess_worker, self._outputs_worker,
            self._input_scan_worker, consulta_web, *indexados,
        ):
            if worker is None:
                continue
            try:
                if worker.isRunning():
                    running.append(worker)
            except RuntimeError:
                # El objeto C++ ya se destruyó tras ``deleteLater``.
                continue
        return running

    def closeEvent(self, event) -> None:
        """Cierra sin destruir hilos vivos, que abortaría el proceso.

        Cerrar la ventana con OCR, preprocesado o exportación en marcha
        destruía un ``QThread`` en ejecución y Windows mataba el programa
        (0xC0000409). Ahora el cierre pide la parada, deja la ventana viva
        atendiendo eventos mientras el trabajo en vuelo termina, y se
        completa solo cuando ya no queda ningún hilo corriendo.

        Esa espera puede ser larga. Cerrar por segunda vez ofrece cortarla:
        se rompen los pools de OCR, con lo que los hilos terminan solos, y
        se cierra sin destruir ninguno.
        """
        running = self._running_workers()
        if running:
            # Volver a cerrar mientras ya se estaba cerrando es la senal de
            # que la espera se hizo larga. Entonces se ofrece cortar: sin
            # esta salida, un OCR de paginas grandes deja la ventana
            # diciendo "cerrando" durante minutos y parece colgada.
            if self._closing and self._confirmar_corte(
                "Cerrar a la fuerza",
                "El programa esta esperando a que terminen las paginas en "
                "curso para cerrarse sin perderlas.",
            ):
                self._cortar_trabajo_en_curso()
                running = self._running_workers()
                if running:
                    self._esperar_a_los_hilos(running)
                self._teardown()
                super().closeEvent(event)
                return
            self._begin_shutdown(running)
            event.ignore()
            return
        self._teardown()
        super().closeEvent(event)

    def _confirmar_corte(self, titulo: str, situacion: str) -> bool:
        """Pregunta si se corta el trabajo en curso, avisando de lo que cuesta."""
        respuesta = QMessageBox.warning(
            self,
            titulo,
            f"{situacion}\n\n"
            "Si prefiere no esperar, se puede cortar ahora mismo. Las "
            "paginas que estaban leyendose en ese momento se pierden; las "
            "que ya estaban leidas se conservan.\n\n"
            "¿Desea cortar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return respuesta == QMessageBox.StandardButton.Yes

    def _cortar_trabajo_en_curso(self) -> None:
        """Rompe los pools de OCR para que el trabajo en vuelo se desenrede.

        No se destruyen los hilos: matar un QThread en marcha aborta el
        proceso entero. Se corta lo que los tiene esperando, que son los
        procesos de OCR, y entonces los hilos terminan solos en el acto.
        """
        from app.core.pipeline import abortar_pools

        self._forzado = True
        logger.warning("Corte solicitado: se abandonan las paginas en vuelo")
        for worker in self._running_workers():
            worker.requestInterruption()
        abortar_pools()

    def _esperar_a_los_hilos(self, running: list[QThread]) -> None:
        """Espera lo justo a que los hilos ya cortados terminen de salir."""
        for worker in running:
            if not worker.wait(_CORTE_ESPERA_MS):
                logger.error(
                    "Un hilo no termino tras el corte; se deja que Qt lo "
                    "recoja al salir"
                )

    def _begin_shutdown(self, running: list[QThread]) -> None:
        """Pide la parada del trabajo en curso y espera sin congelar la GUI."""
        if not self._closing:
            self._closing = True
            logger.info(
                f"Cierre solicitado: deteniendo {len(running)} tarea(s) en curso"
            )
            for worker in running:
                worker.requestInterruption()
            for button in (
                self.btn_process, self.btn_preprocess,
                self.btn_export, self.btn_cancel,
            ):
                button.setEnabled(False)
            self.status_label.setText(
                "Cerrando… se está deteniendo el trabajo en curso. "
                "Las páginas ya leídas se guardan. Vuelva a cerrar para no "
                "esperar."
            )
        if not self._shutdown_timer.isActive():
            self._shutdown_timer.start()

    def _on_shutdown_tick(self) -> None:
        """Cierra de verdad en cuanto el último hilo termina."""
        if self._running_workers():
            return
        self._shutdown_timer.stop()
        self.close()

    def _teardown(self) -> None:
        """Detiene temporizadores e hilos propios antes de destruir la ventana.

        Se ejecuta una sola vez: la llaman tanto el cierre de la ventana como
        ``aboutToQuit``, y la segunda pasada no debe tocar nada.
        """
        if self._torn_down:
            return
        self._torn_down = True
        self._detach_logger()
        for timer in (
            self._timer, self._table_timer, self._log_timer,
            self._shutdown_timer, self._resize_preview_timer,
        ):
            timer.stop()
        if self._csv_viewer is not None:
            self._csv_viewer.close()
        if self._web_reports_window is not None:
            try:
                self._web_reports_window.detener()
                self._web_reports_window.close()
            except RuntimeError:
                pass
        for ventana in self._airvault_windows:
            # Suelta los batches que una revisión sin indexar dejó tomados en
            # AirVault. Uno que queda tomado no da error: cuelga la próxima
            # vez que alguien lo abra en el Web Index.
            try:
                ventana.detener()
                ventana.close()
            except RuntimeError:
                # El objeto C++ ya se destruyó con la ventana principal.
                pass
        self._preview_thread.quit()
        self._preview_thread.wait(5000)
        # Red de seguridad: si algún hilo llegara vivo hasta aquí, esperarlo
        # es preferible a que Qt lo destruya en marcha y aborte el proceso.
        for worker in self._running_workers():
            worker.requestInterruption()
            worker.wait(10000)


def _color_for(status: Status):
    from PySide6.QtGui import QColor

    return QColor(_COLORS[status])
