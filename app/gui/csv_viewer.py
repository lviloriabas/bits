"""Visor de solo lectura para reportes CSV ya procesados."""

from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

from loguru import logger
from PySide6.QtCore import (
    QEvent,
    QObject,
    QRectF,
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
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QSplitter,
    QWidget,
)

from app.gui.csv_utils import (
    TEXTO_ELEGIR_EJECUCION,
    csv_field_id,
    find_csv_files,
    find_run_dirs,
    important_csv_columns,
    important_field_ids_for_csv,
    read_csv_file,
    template_field_ids_for_columns,
    template_for_csv,
    template_name_for_csv,
)
from app.gui.csv_model import (
    CHECK_COLUMN,
    CHECK_COLUMN_WIDTH,
    STATUS_COLORS,
    CsvTableModel,
)
from app.gui.depuracion_dialog import DEPURAR_TOOLTIP, DepurarPaginasDialog
from app.gui.export_options import ExportOptionsGroup
from app.gui.field_selector import ImportantFieldsDialog
from app.gui.responsive import ROOMY, Density, density_for, fit_to_screen
from app.gui.table_sort import ModelSortController
from app.gui.tokens import (
    FONT_CAPTION_PT,
    SPACE_S,
    TEXT_DISABLED,
    TEXT_SECONDARY,
    accent_color,
)
from app.reports.outputs import complete_csv_path
from app.reports.csv_reporter import CsvReporter
from app.gui.widgets import (
    DATA_TABLE_QSS,
    PANE_BG,
    PANE_BORDER,
    PANE_CONTROL_BG,
    PANE_CONTROL_HOVER,
    PANE_STATUS_COLORS,
    PANE_SURFACE_BG,
    PANE_TEXT,
    TABLE_RADIUS,
    TABLE_SELECTION_BG,
    ZOOM_OVERLAY_QSS,
    ElidedLabel,
    ZoomableScrollArea,
    ZoomOverlay,
    hide_overlay_when_tight,
    keep_overlay_clear_of_scrollbars,
    scrollbars_qss,
    style_data_table,
    window_stylesheet,
    style_dark_pane,
    style_pdf_surface,
)
from app.utils.important_fields import (
    IMPORTANT_FIELDS_FILENAME,
    ImportantFieldsStore,
)
from app.utils.io import PROCESSED_DIRNAME
# El mismo recuento que dejan los correctores al tocar un reporte: quitarle
# páginas cambia los totales y el JSON no puede quedar diciendo los de antes.
from app.validation.book_corrector import _recompute_summary
from app.validation.depuracion import depurar_claves
from app.validation.page_status import needs_review


_PROGRAM_DIR = Path(__file__).resolve().parents[2]
_ASSETS = _PROGRAM_DIR / "assets"
_RENDER_DPI = 150
_MIN_ZOOM = 0.4
_MAX_ZOOM = 4.0
# Filas que examina Qt al ajustar el ancho de una columna. Sin tope recorre
# todas: con miles de filas el ajuste solo costaba más que llenar la tabla.
_RESIZE_PRECISION = 64
# Reparto del ancho entre el visor de PDF y la tabla, el mismo de la vista
# previa y la tabla de la ventana principal.
_PDF_PANE_SHARE = 2
_TABLE_SHARE = 3
# Ejecuciones que lista el historial. Es la ventana de trabajo de un turno: lo
# de más atrás sigue estando en output/ y se abre con «Seleccionar carpeta».
_HISTORY_LIMIT = 25
# El mismo texto que en la ventana principal; el botón lo recupera cuando
# deja de explicar por qué no se puede exportar.
# Lo que dice el indicador de búsqueda mientras no hay nada que buscar.
_SEARCH_HINT = "La búsqueda abre cada coincidencia en el visor."
_EXPORT_TOOLTIP = (
    "Volver a generar CSV, JSON y PDF con las opciones actuales, sin "
    "reprocesar. Los PDF repetidos se numeran (-2, -3…)"
)
# El panel se estiliza a sí mismo para verse igual dentro y fuera del visor.
_PDF_PANE_QSS = (
    # El panel va en el mismo gris oscuro que la tabla a la que acompaña: en
    # blanco quedaba como un bloque luminoso al lado de ella.
    f"#embeddedPdfPane {{ background: {PANE_BG};"
    f" border: 1px solid {PANE_SURFACE_BG}; border-radius: {TABLE_RADIUS}px; }}"
    # Mismo radio que el marco que la contiene: quedaba en 4px, un cuadro
    # distinto al del resto de la aplicacion (QGroupBox, tablas, timeSummary).
    f"#pdfSurface {{ background: {PANE_SURFACE_BG};"
    f" border: 1px solid {PANE_BORDER}; border-radius: {TABLE_RADIUS}px; }}"
    # Sin fondo explícito, la etiqueta pinta el color de ventana y tapa la
    # superficie oscura justo cuando solo muestra el mensaje de estado.
    f"#pdfPage {{ color: {PANE_TEXT}; padding: 0; background: transparent; }}"
    f"#embeddedPdfPane QLabel {{ color: {PANE_TEXT}; background: transparent; }}"
    # Los campos suben un escalón sobre el panel para seguir leyéndose como
    # controles y no como parte del fondo.
    "#embeddedPdfPane QComboBox, #embeddedPdfPane QLineEdit,"
    "#embeddedPdfPane QPushButton, #embeddedPdfPane QToolButton {"
    f" background: {PANE_CONTROL_BG}; color: {PANE_TEXT};"
    f" border: 1px solid {PANE_BORDER}; border-radius: {TABLE_RADIUS}px; padding: 2px 6px; }}"
    "#embeddedPdfPane QComboBox:hover, #embeddedPdfPane QPushButton:hover,"
    "#embeddedPdfPane QToolButton:hover {"
    f" background: {PANE_CONTROL_HOVER}; }}"
    "#embeddedPdfPane QComboBox:disabled, #embeddedPdfPane QPushButton:disabled,"
    "#embeddedPdfPane QToolButton:disabled {"
    f" background: {PANE_BG}; color: {TEXT_DISABLED}; }}"
    # La lista desplegable es una ventana aparte y no hereda el fondo.
    "#embeddedPdfPane QComboBox QAbstractItemView {"
    f" background: {PANE_CONTROL_BG}; color: {PANE_TEXT};"
    f" selection-background-color: palette(highlight); }}"
    # El recuadro de zoom va al final: sus reglas y las del panel tienen la
    # misma especificidad y aquí gana la última.
) + scrollbars_qss("#embeddedPdfPane") + ZOOM_OVERLAY_QSS


def _folder_key(path: Path | str) -> str:
    """Identidad de una carpeta, para compararla venga de donde venga."""
    return str(Path(path).resolve()).casefold()


def _join_names(names: Iterable[str], limit: int = 3) -> str:
    """Enumera nombres de archivo sin desbordar el indicador."""
    name_list = list(names)
    shown = ", ".join(name_list[:limit])
    return shown if len(name_list) <= limit else f"{shown}…"


def _numeros_de_bitacora(rows: Iterable[dict[str, str]]) -> list[str]:
    """Bitácoras distintas que aparecen en esas filas, en orden de lectura.

    Se conserva el texto tal cual lo trae el CSV: es lo que el usuario ve en
    la tabla, y una bitácora ilegible tiene que poder nombrarse igual que
    una legible para que el recuento cuadre con lo que se va a borrar.
    """
    vistos: list[str] = []
    conocidos: set[str] = set()
    for row in rows:
        numero = (row.get("log_number") or "").strip()
        if not numero or numero in conocidos:
            continue
        conocidos.add(numero)
        vistos.append(numero)
    return vistos


def _lista_de_bitacoras(numeros: Sequence[str], limite: int = 12) -> str:
    """Enumera bitácoras para el cuadro de confirmación, sin desbordarlo.

    Con muchas se corta la lista y se dice cuántas quedan fuera: el cuadro
    tiene que caber en pantalla, y quien borra medio centenar de páginas
    necesita el número total más que los últimos cuarenta códigos.
    """
    if not numeros:
        return "sin número de bitácora legible"
    mostrados = list(numeros[:limite])
    resto = len(numeros) - len(mostrados)
    texto = ", ".join(mostrados)
    return texto if not resto else f"{texto} y {resto} más"


def _archived_base_name(name: str) -> str:
    """Nombre anterior a un sufijo de archivo ``-2``, ``-3``…"""
    path = Path(name)
    stem, separator, suffix = path.stem.rpartition("-")
    if separator and suffix.isdigit() and int(suffix) >= 2 and stem:
        return f"{stem}{path.suffix}"
    return path.name


def _companion_path(csv_path: Path) -> Path:
    """JSON consolidado que le corresponde a un CSV de ejecución."""
    csv_path = Path(csv_path)
    companion = csv_path.with_suffix(".json")
    if not companion.is_file() and csv_path.stem.casefold().endswith("_completo"):
        companion = csv_path.with_name(
            f"{csv_path.stem[:-len('_completo')]}.json"
        )
    return companion


@lru_cache(maxsize=4)
def _parsed_companion(path: str, _marca: tuple[int, int]) -> dict:
    """El JSON ya interpretado, recordado mientras el archivo no cambie.

    Abrir una ejecución lo pedía cinco veces (para los PDF de cada fila, los
    documentos, sus páginas, los estados de campo y el botón de exportar), y
    el de una ejecución grande son varios megabytes: se interpretaba entero
    cinco veces seguidas y eso solo era ya un segundo de espera. ``_marca``
    es el tamaño y la fecha del archivo, así que reescribirlo (al eliminar
    páginas) invalida lo recordado sin que nadie tenga que acordarse.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _companion_payload(csv_path: Path) -> dict:
    """Lee el JSON consolidado que acompaña al CSV; ``{}`` si no está."""
    companion = _companion_path(csv_path)
    try:
        info = companion.stat()
    except OSError:
        return {}
    return _parsed_companion(str(companion), (info.st_size, info.st_mtime_ns))


def source_pdf_paths_for_rows(
    csv_path: Path, rows: Iterable[dict[str, str]]
) -> list[Path | None]:
    """Resuelve el PDF fuente de cada fila usando el JSON consolidado.

    El CSV guarda solo el nombre del archivo. El JSON compañero conserva la
    ruta completa y el orden exacto reporte/página, por lo que también evita
    ambigüedad cuando se procesaron varios PDF con el mismo nombre.
    """
    row_list = list(rows)
    csv_path = Path(csv_path)
    flattened: list[tuple[str, str, Path]] = []
    try:
        for report in _companion_payload(csv_path).get("reportes", []):
            pdf_path = Path(str(report.get("pdf_path", "")))
            filename = str(
                report.get("source_name") or pdf_path.name
            ).casefold()
            for page in report.get("pages", []):
                flattened.append(
                    (filename, str(page.get("page_number", "")), pdf_path)
                )
    except (TypeError, AttributeError):
        flattened = []

    # La escritura consolidada recorre reportes y páginas en este mismo orden.
    if len(flattened) == len(row_list) and all(
        (
            source_name == row.get("file", "").casefold()
            or _archived_base_name(source_name).casefold()
            == row.get("file", "").casefold()
        )
        and source_page == row.get("page", "")
        for row, (source_name, source_page, _path) in zip(row_list, flattened)
    ):
        return [entry[2] for entry in flattened]

    by_key: dict[tuple[str, str], list[Path]] = {}
    for filename, page, pdf_path in flattened:
        by_key.setdefault((filename, page), []).append(pdf_path)
        base_name = _archived_base_name(filename).casefold()
        if base_name != filename:
            by_key.setdefault((base_name, page), []).append(pdf_path)
    resolved: list[Path | None] = []
    for row in row_list:
        candidates = by_key.get(
            (row.get("file", "").casefold(), row.get("page", "")), []
        )
        resolved.append(candidates[0] if len(candidates) == 1 else None)
    return resolved


def source_documents_for_csv(csv_path: Path) -> list[Path]:
    """PDF que originaron el CSV, en el orden en que se procesaron."""
    documents: list[Path] = []
    seen: set[str] = set()
    try:
        recorded = [
            str(report.get("pdf_path", ""))
            for report in _companion_payload(csv_path).get("reportes", [])
        ]
    except (TypeError, AttributeError):
        return []
    for raw in recorded:
        if not raw:
            continue
        path = Path(raw)
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            documents.append(path)
    return documents


def _source_page_requirements(csv_path: Path) -> dict[str, int]:
    """Mayor página local que debe existir en cada PDF de la ejecución.

    Sirve únicamente para desempatar copias históricas ``archivo.pdf``,
    ``archivo-2.pdf``… cuando el JSON fue escrito antes de que el original se
    archivara. No se exige que el PDF tenga exactamente esa cantidad porque
    la ejecución pudo procesar solo un rango.
    """
    requirements: dict[str, int] = {}
    try:
        reports = _companion_payload(csv_path).get("reportes", [])
        for report in reports:
            raw_path = str(report.get("pdf_path", ""))
            if not raw_path:
                continue
            pages = []
            for page in report.get("pages", []):
                try:
                    pages.append(int(page.get("page_number", 0)))
                except (TypeError, ValueError, AttributeError):
                    continue
            required = max(pages, default=0)
            path = Path(raw_path)
            requirements[str(path).casefold()] = required
            requirements.setdefault(path.name.casefold(), required)
    except (TypeError, AttributeError):
        return {}
    return requirements


def _documents_from_rows(rows: Iterable[dict[str, str]]) -> list[Path]:
    """Nombres de PDF declarados por el CSV cuando no hay JSON compañero."""
    documents: list[Path] = []
    seen: set[str] = set()
    for row in rows:
        name = (row.get("file") or "").strip()
        if not name.casefold().endswith(".pdf"):
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            documents.append(Path(name))
    return documents


def _source_search_folders(csv_path: Path) -> list[Path]:
    """Carpetas donde puede estar el PDF de origen de una ejecución.

    La de la ejecución y la entrada del programa, y también la de los archivos
    ya procesados: al terminar una ejecución sus PDF salen de ``input/`` para
    no confundirse con lo que falta, y ahí es donde están desde entonces.
    """
    csv_path = Path(csv_path)
    entrada = _PROGRAM_DIR / "input"
    return [
        csv_path.parent,
        csv_path.parent.parent,
        entrada / PROCESSED_DIRNAME,
        # Una ejecución histórica debe preferir el archivo ya apartado. En
        # input puede haber ahora otro PDF nuevo con exactamente el mismo
        # nombre, y ese todavía no pertenece al CSV que se está consultando.
        entrada,
    ]


def _archive_variant_index(candidate: Path, recorded: Path) -> int | None:
    """Número de ``archivo-2.pdf`` respecto de ``archivo.pdf``."""
    if candidate.suffix.casefold() != recorded.suffix.casefold():
        return None
    prefix = f"{recorded.stem}-".casefold()
    stem = candidate.stem.casefold()
    if not stem.startswith(prefix):
        return None
    suffix = stem[len(prefix):]
    if not suffix.isdigit() or int(suffix) < 2:
        return None
    return int(suffix)


def _candidate_page_count(
    path: Path, cache: dict[str, int | None]
) -> int | None:
    """Cuenta páginas una vez; ``None`` conserva un candidato ilegible."""
    key = str(path.resolve()).casefold()
    if key not in cache:
        from app.vision.pdf_loader import page_count

        try:
            cache[key] = max(0, page_count(path))
        except Exception:  # noqa: BLE001 - el render mostrará el error después
            cache[key] = None
    return cache[key]


def _locate_document(
    recorded: Path,
    folders: Iterable[Path],
    deep_folders: Iterable[Path],
    required_page: int = 0,
    page_counts: dict[str, int | None] | None = None,
) -> Path | None:
    """Busca un PDF de origen que pudo haberse movido desde el procesamiento.

    ``folders`` se revisa solo por nombre exacto; el recorrido recursivo queda
    reservado a ``deep_folders``, las carpetas que el usuario indicó a mano.
    Para ejecuciones antiguas también reconoce el sufijo que añadió el archivo
    de procesados. Si hay varias copias, la mayor página usada por el reporte
    descarta las que físicamente no pueden ser su fuente.
    """
    folders = [Path(folder) for folder in folders]
    deep_folders = [Path(folder) for folder in deep_folders]
    name = recorded.name
    if not name:
        return None

    exact: list[Path] = []
    variants: list[tuple[int, Path]] = []
    seen: set[str] = set()

    def add_exact(path: Path) -> None:
        if not path.is_file():
            return
        key = str(path.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            exact.append(path)

    def add_variant(path: Path) -> None:
        index = _archive_variant_index(path, recorded)
        if index is None or not path.is_file():
            return
        key = str(path.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            variants.append((index, path))

    # La ruta absoluta gana siempre que siga existiendo: identifica el archivo
    # sin inferencias incluso si hay homónimos en processed.
    if recorded.is_absolute():
        add_exact(recorded)
    for folder in folders:
        add_exact(folder / name)
        if folder.name.casefold() == PROCESSED_DIRNAME.casefold():
            try:
                for candidate in folder.iterdir():
                    add_variant(candidate)
            except OSError:
                pass
    for folder in deep_folders:
        try:
            for path in folder.rglob(name):
                add_exact(path)
            for path in folder.rglob(f"*{recorded.suffix}"):
                add_variant(path)
        except OSError:
            continue

    variants.sort(key=lambda item: item[0])
    if exact and not variants:
        return exact[0]
    if not exact and len(variants) == 1:
        return variants[0][1]
    if not exact and not variants:
        return None

    # Solo se abre cada candidato cuando de verdad hay ambigüedad. Un PDF de
    # prueba o dañado sigue localizándose; el visor será quien informe si no
    # puede renderizarlo.
    cache = page_counts if page_counts is not None else {}
    if required_page > 0:
        usable_exact = [
            path for path in exact
            if (count := _candidate_page_count(path, cache)) is None
            or count >= required_page
        ]
        if usable_exact:
            return usable_exact[0]
        usable_variants = []
        for index, path in variants:
            count = _candidate_page_count(path, cache)
            if count is None or count >= required_page:
                # La menor holgura es la mejor coincidencia física; el sufijo
                # solo desempata dos PDF con igual cantidad de páginas.
                slack = count - required_page if count is not None else 10**12
                usable_variants.append((slack, index, path))
        if usable_variants:
            return min(
                usable_variants,
                key=lambda item: (item[0], item[1], str(item[2]).casefold()),
            )[2]
    return exact[0] if exact else variants[0][1]


def resolve_source_documents(
    csv_path: Path,
    rows: Iterable[dict[str, str]],
    extra_folders: Iterable[Path] = (),
) -> tuple[list[Path | None], list[Path], list[str]]:
    """Ubica en disco los PDF de origen del CSV.

    Devuelve el PDF de cada fila, los documentos que sí están disponibles y el
    nombre de los que el CSV declara pero no se encontraron, para que el visor
    pueda mostrarlos todos o avisar cuáles faltan.
    """
    csv_path = Path(csv_path)
    row_list = list(rows)
    row_paths = source_pdf_paths_for_rows(csv_path, row_list)
    recorded = source_documents_for_csv(csv_path) or _documents_from_rows(row_list)
    requirements = _source_page_requirements(csv_path)
    if not requirements:
        for row in row_list:
            name = (row.get("file") or "").strip()
            try:
                page = int(row.get("page", ""))
            except ValueError:
                page = 0
            if name:
                requirements[name.casefold()] = max(
                    page, requirements.get(name.casefold(), 0)
                )
    extra = [Path(folder) for folder in extra_folders]
    folders = _source_search_folders(csv_path)
    folders.extend(extra)

    available: list[Path] = []
    seen: set[str] = set()
    missing: list[str] = []
    by_recorded: dict[str, Path] = {}
    by_name: dict[str, Path | None] = {}
    page_counts: dict[str, int | None] = {}
    for source in recorded:
        required_page = requirements.get(
            str(source).casefold(), requirements.get(source.name.casefold(), 0)
        )
        found = _locate_document(
            source,
            folders,
            extra,
            required_page=required_page,
            page_counts=page_counts,
        )
        if found is None:
            if source.name not in missing:
                missing.append(source.name)
            continue
        key = str(found.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            available.append(found)
        by_recorded[str(source).casefold()] = found
        # Un nombre repetido entre documentos distintos no identifica una fila.
        name = source.name.casefold()
        by_name[name] = None if name in by_name else found

    resolved: list[Path | None] = []
    for row, path in zip(row_list, row_paths):
        if path is not None:
            resolved.append(by_recorded.get(str(path).casefold()))
        else:
            resolved.append(by_name.get((row.get("file") or "").casefold()))
    return resolved, available, missing


def run_dir_for_csv(csv_path: Path) -> Path | None:
    """Carpeta de la ejecución a la que pertenece el CSV, si se reconoce.

    Las ejecuciones guardan el reporte en ``<corrida>/datos/``; las históricas
    lo dejaban en la raíz de la ejecución. Devuelve ``None`` cuando el CSV no
    está en la carpeta de su ejecución, que es el único sitio sobre el que se
    puede volver a exportar.
    """
    csv_path = Path(csv_path)
    parent = csv_path.parent
    run_dir = parent.parent if parent.name.casefold() == "datos" else parent
    # La carpeta tiene que ser la de esta ejecución y no una cualquiera donde
    # alguien haya dejado copias: volver a exportar limpia de ahí lo que la
    # ejecución regenera, y sobre una carpeta ajena eso borraría archivos que
    # no son suyos. Una ejecución siempre nombra igual su carpeta y su CSV.
    stem = csv_path.stem
    if stem.casefold().endswith("_completo"):
        stem = stem[: -len("_completo")]
    return run_dir if run_dir.name.casefold() == stem.casefold() else None


def reports_from_companion(csv_path: Path) -> list:
    """Reportes tal como los guardó la ejecución, sin buscar sus PDF.

    Es lo que hace falta para reescribir los datos de la ejecución (CSV, JSON
    y estadísticas), que salen del propio reporte y no de las páginas. Buscar
    los PDF aquí sería peor que inútil: descartar el reporte cuyo archivo ya
    no está borraría de la ejecución todo lo que se procesó desde él.
    """
    from app.models.schemas import ValidationReport

    return [
        ValidationReport.model_validate(entry)
        for entry in _companion_payload(Path(csv_path)).get("reportes", [])
    ]


def restore_run_columns(
    csv_path: Path,
    columns: list[str],
    rows: list[dict[str, str]],
) -> tuple[list[str], list[dict[str, str]]]:
    """Recupera columnas nuevas para un CSV histórico con JSON compañero."""
    missing = {"review", "disc_reason"}.difference(columns)
    if not missing:
        return columns, rows
    try:
        reports = reports_from_companion(csv_path)
    except Exception:  # noqa: BLE001 - compatibilidad opcional del visor
        return columns, rows
    pages = {
        (report.source_filename.casefold(), str(page.page_number)): page
        for report in reports
        for page in report.pages
    }
    matched = [
        pages.get(((row.get("file") or "").casefold(), row.get("page") or ""))
        for row in rows
    ]
    if not rows or any(page is None for page in matched):
        return columns, rows

    restored = list(columns)
    if "review" in missing:
        position = (
            restored.index("log_number") + 1
            if "log_number" in restored
            else 2
        )
        restored.insert(min(position, len(restored)), "review")
    if "disc_reason" in missing:
        position = (
            restored.index("disc") + 1
            if "disc" in restored
            else len(restored)
        )
        restored.insert(position, "disc_reason")
    for row, page in zip(rows, matched):
        if "review" in missing:
            row["review"] = str(needs_review(page)).lower()
        if "disc_reason" in missing:
            row["disc_reason"] = (
                page.discrepancy_note if page.discrepancy else ""
            )
    return restored, rows


def reports_for_csv(
    csv_path: Path, extra_folders: Iterable[Path] = ()
) -> tuple[list, list[str]]:
    """Reconstruye los reportes de la ejecución desde el JSON compañero.

    El JSON consolidado guarda los reportes completos, así que la ejecución se
    puede volver a exportar sin repetir el OCR. Cada reporte apunta al PDF
    que lo originó: si el archivo se movió, se busca igual que lo hace el
    visor y el reporte queda apuntando a donde está ahora. Devuelve también
    el nombre de los PDF que no aparecieron, porque sin ellos no se pueden
    rehacer las páginas.
    """
    csv_path = Path(csv_path)
    extra = [Path(folder) for folder in extra_folders]
    folders = _source_search_folders(csv_path)
    folders.extend(extra)

    reports = []
    missing: list[str] = []
    page_counts: dict[str, int | None] = {}
    for report in reports_from_companion(csv_path):
        located = _locate_document(
            Path(report.pdf_path),
            folders,
            extra,
            required_page=max(
                (page.page_number for page in report.pages), default=0
            ),
            page_counts=page_counts,
        )
        if located is None:
            name = Path(report.pdf_path).name
            if name not in missing:
                missing.append(name)
            continue
        report.pdf_path = str(located)
        reports.append(report)
    return reports, missing


def apply_csv_column_visibility(
    table: QAbstractItemView,
    columns: Iterable[str],
    important_field_ids: Iterable[str],
    important_only: bool,
    selected_columns: Iterable[str] | None = None,
    offset: int = 0,
) -> None:
    """Aplica el modo resumido/completo ocultando columnas de la tabla.

    ``offset`` son las columnas propias de la tabla que van delante de las
    del CSV: en el visor, la de las casillas, que no se oculta nunca porque
    no es un campo de la bitácora sino la forma de elegir páginas.
    """
    column_list = list(columns)
    visible = set(
        selected_columns
        if selected_columns is not None
        else important_csv_columns(column_list, important_field_ids)
    )
    for index, column in enumerate(column_list):
        table.setColumnHidden(
            index + offset, important_only and column not in visible
        )


def csv_path_for_view(path: Path) -> Path:
    """Usa el CSV completo como fuente visual cuando acompana al minimo."""
    path = Path(path)
    if path.stem.casefold().endswith("_completo"):
        return path
    complete = complete_csv_path(path)
    return complete if complete.is_file() else path


class CsvColumnModeButton(QToolButton):
    """Alterna con una etiqueta clara entre la vista resumida y la completa."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(True)
        self.setObjectName("csvColumnToggle")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(20, 20))
        self.setAccessibleName("Columnas visibles del CSV")
        self.toggled.connect(self._sync_visuals)
        self._sync_visuals(True)

    def _sync_visuals(self, important_only: bool) -> None:
        icon_name = "columns_important.svg" if important_only else "columns_all.svg"
        self.setIcon(QIcon(str(_ASSETS / icon_name)))
        self.setText(
            "Columnas importantes" if important_only else "Todas las columnas"
        )
        self.setToolTip(
            "Mostrando columnas importantes. Clic para mostrar todas las columnas."
            if important_only
            else "Mostrando el CSV completo. Clic para mostrar solo los "
                 "campos importantes."
        )
        self.setAccessibleDescription(self.toolTip())


class ImportantFieldsButton(QToolButton):
    """Abre el selector con una etiqueta legible, no con un glifo ambiguo."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText("Elegir columnas…")
        self.setToolTip("Seleccionar columnas importantes")
        self.setAccessibleName("Seleccionar columnas importantes")


def _document_labels(documents: list[Path]) -> list[str]:
    """Etiquetas del selector; desambigua los nombres de archivo repetidos."""
    counts: dict[str, int] = {}
    for document in documents:
        counts[document.name.casefold()] = counts.get(document.name.casefold(), 0) + 1
    return [
        document.name
        if counts[document.name.casefold()] == 1
        else f"{document.parent.name}\\{document.name}"
        for document in documents
    ]


class PdfPageLoader(QObject):
    """Rasteriza páginas del PDF en su propia QThread.

    Es el mismo reparto que ya usa la vista previa de la ventana principal
    (``PreviewLoader``): rasterizar una página cuesta ~90 ms, y hacerlo en el
    hilo de interfaz congelaba la ventana una vez por cada fila que el
    usuario recorría con las flechas.
    """

    requested = Signal(str, int)
    ready = Signal(str, int, object)

    def run(self, pdf_path: str, page: int) -> None:
        import cv2

        from app.vision.pdf_loader import render_page

        try:
            image = render_page(Path(pdf_path), page, dpi=_RENDER_DPI)
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            height, width, channels = rgb.shape
            qimage = QImage(
                rgb.data, width, height, channels * width,
                QImage.Format.Format_RGB888,
            ).copy()
        except Exception as exc:  # noqa: BLE001 - visor no crítico
            logger.warning(
                f"No se pudo rasterizar {pdf_path} p. {page}: {exc}"
            )
            self.ready.emit(pdf_path, page, None)
            return
        self.ready.emit(pdf_path, page, qimage)


class EmbeddedPdfViewer(QFrame):
    """Visor de los PDF que originaron el CSV abierto.

    La página se escala al espacio disponible en lugar de mostrarse a tamaño
    fijo, y el estado de los documentos de origen queda siempre a la vista:
    cuáles se encontraron y cuáles no.
    """

    relocateRequested = Signal()

    def apply_density(self, density: Density) -> None:
        """Aprieta los mínimos del panel cuando la ventana es baja."""
        self._density = density
        self.setMinimumWidth(density.pdf_pane_min_width)
        self.scroll.setMinimumHeight(density.pdf_pane_min_height)

    def __init__(
        self, parent: QWidget | None = None, density: Density = ROOMY
    ) -> None:
        """La densidad se recibe hecha, no se aplica después de construir.

        Los mínimos del panel entran en el cálculo del alto de la ventana, y
        cambiarlos con el panel ya montado deja el mínimo anterior guardado en
        el layout hasta el primer dibujado: la ventana se abría con el alto
        del panel holgado (cien píxeles de más) en una pantalla que ya se
        había medido como baja.
        """
        super().__init__(parent)
        self.setObjectName("embeddedPdfPane")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._documents: list[Path] = []
        self._missing: list[str] = []
        self._path: Path | None = None
        self._page = 1
        self._total = 0
        # Secuencia exacta de la ejecución: una entrada por fila del CSV. La
        # página local puede tener huecos aunque el PDF físico tenga más hojas.
        self._execution_pages: list[tuple[Path | None, int]] = []
        self._global_index = 0
        self._zoom = 1.0  # 1.0 = página ajustada al panel
        self._density = density
        self._base_image: QImage | None = None
        self._source: QPixmap | None = None
        self._overlay_template = None
        self._overlay_field_ids: set[str] | None = None
        self._overlay_visible = False
        self._refresh_pending = False
        # Páginas por documento: el recuento reabría el PDF en cada salto.
        self._page_counts: dict[str, int] = {}
        self._pending_render: tuple[str, int] | None = None
        self._loader: PdfPageLoader | None = None
        self._loader_thread: QThread | None = None
        self._build_ui()

    # ── Render en segundo plano ─────────────────────────────────────────

    def _ensure_loader(self) -> PdfPageLoader:
        """Cargador vivo en su hilo; se arranca la primera vez que hace falta."""
        if self._loader is None:
            self._loader = PdfPageLoader()
            self._loader_thread = QThread(self)
            self._loader.moveToThread(self._loader_thread)
            self._loader.requested.connect(self._loader.run)
            self._loader.ready.connect(self._on_page_ready)
        if self._loader_thread is not None and not self._loader_thread.isRunning():
            self._loader_thread.start()
        return self._loader

    def shutdown(self) -> None:
        """Detiene el hilo de render antes de que el panel se destruya.

        Destruir un ``QThread`` en marcha aborta el proceso, así que la
        ventana que contiene el panel llama aquí al cerrarse.
        """
        thread = self._loader_thread
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(5000)

    def closeEvent(self, event) -> None:  # noqa: N802 - API Qt
        self.shutdown()
        super().closeEvent(event)

    def _page_total(self, path: Path) -> int:
        """Páginas del documento, contadas una sola vez por archivo."""
        key = str(path)
        if key not in self._page_counts:
            from app.vision.pdf_loader import page_count

            try:
                self._page_counts[key] = max(0, page_count(path))
            except Exception:  # noqa: BLE001 - visor no crítico
                self._page_counts[key] = 0
        return self._page_counts[key]

    def _build_ui(self) -> None:
        """Misma disposición que la vista previa de la ventana principal.

        La página ocupa el panel entero con el recuadro de zoom flotando
        sobre ella y la paginación centrada en una barra debajo. El selector
        de documento se queda en su propia fila: ahí es un control con
        etiqueta, no el nombre de archivo de la ventana principal, y en el
        ancho que le toca al panel (dos quintos de la ventana) no cabe junto
        a una paginación centrada sin montarse encima de ella.
        """
        self.setStyleSheet(_PDF_PANE_QSS)
        style_dark_pane(self)
        self.setMinimumWidth(self._density.pdf_pane_min_width)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(SPACE_S)

        documents = QHBoxLayout()
        documents.setSpacing(SPACE_S)
        documents.addWidget(QLabel("PDF de origen:"))
        self.pdf_combo = QComboBox()
        self.pdf_combo.setEnabled(False)
        self.pdf_combo.setAccessibleName("PDF de origen del CSV")
        self.pdf_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.pdf_combo.setMinimumContentsLength(16)
        self.pdf_combo.currentIndexChanged.connect(self._on_document_changed)
        documents.addWidget(self.pdf_combo, 1)
        self.locate_button = QPushButton("Ubicar PDF…")
        self.locate_button.setToolTip(
            "Indicar la carpeta donde están ahora los PDF que no se encontraron"
        )
        self.locate_button.setVisible(False)
        self.locate_button.clicked.connect(self.relocateRequested)
        documents.addWidget(self.locate_button)
        layout.addLayout(documents)

        self.scroll = ZoomableScrollArea()
        self.scroll.setObjectName("pdfSurface")
        style_pdf_surface(self.scroll)
        self.scroll.set_zoom_callback(self._zoom_by)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setMinimumHeight(self._density.pdf_pane_min_height)
        self.image = QLabel()
        self.image.setObjectName("pdfPage")
        self.image.setWordWrap(True)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.image)
        self.scroll.viewport().installEventFilter(self)

        viewer_frame = QWidget()
        viewer_frame_layout = QGridLayout(viewer_frame)
        viewer_frame_layout.setContentsMargins(0, 0, 0, 0)
        viewer_frame_layout.addWidget(self.scroll, 0, 0)

        zoom_overlay = ZoomOverlay(
            ("Acercar la página", "Acercar la página", lambda: self._zoom_by(1.25)),
            (
                "Ajustar la página al panel",
                "Ajustar la página al panel",
                self.fit_page,
            ),
            ("Alejar la página", "Alejar la página", lambda: self._zoom_by(0.8)),
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
        # El recuadro de zoom flota sobre la página: ni decide el mínimo
        # del panel ni se dibuja a medias. Ver ``hide_overlay_when_tight``.
        zoom_holder.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        viewer_frame_layout.addWidget(
            zoom_holder,
            0,
            0,
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        )
        zoom_holder.raise_()
        keep_overlay_clear_of_scrollbars(zoom_holder, self.scroll)
        hide_overlay_when_tight(zoom_holder)
        layout.addWidget(viewer_frame, 1)

        self.pagination = QWidget()
        controls = QHBoxLayout(self.pagination)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(SPACE_S)
        self.prev = QToolButton()
        self.prev.setArrowType(Qt.ArrowType.LeftArrow)
        self.prev.setToolTip("Página anterior (flecha izquierda)")
        self.prev.setAccessibleName("Página anterior")
        self.prev.setShortcut(QKeySequence(Qt.Key.Key_Left))
        self.prev.clicked.connect(self._show_previous_page)
        controls.addWidget(self.prev)
        controls.addWidget(QLabel("Página"))
        self.page_edit = QLineEdit()
        self.page_edit.setValidator(QIntValidator(1, 1, self.page_edit))
        self.page_edit.setAccessibleName("Página del PDF")
        self.page_edit.setFixedWidth(48)
        self.page_edit.editingFinished.connect(self._jump)
        controls.addWidget(self.page_edit)
        self.total_pages = QLabel("de 0")
        controls.addWidget(self.total_pages)
        self.next = QToolButton()
        self.next.setArrowType(Qt.ArrowType.RightArrow)
        self.next.setToolTip("Página siguiente (flecha derecha)")
        self.next.setAccessibleName("Página siguiente")
        self.next.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self.next.clicked.connect(self._show_next_page)
        controls.addWidget(self.next)

        # La paginación se centra sobre todo el ancho de la página, igual que
        # en la ventana principal.
        nav_bar = QWidget()
        nav_bar_layout = QGridLayout(nav_bar)
        nav_bar_layout.setContentsMargins(0, 0, 0, 0)
        nav_bar_layout.addWidget(
            self.pagination, 0, 0, Qt.AlignmentFlag.AlignCenter
        )
        layout.addWidget(nav_bar)

        self.source_status = QLabel()
        self.source_status.setObjectName("pdfSourceStatus")
        self.source_status.setWordWrap(True)
        self.source_status.setAccessibleName("Estado de los PDF de origen")
        layout.addWidget(self.source_status)

        self._update_source_status()
        self._show_placeholder(
            "Seleccione una carpeta procesada o un CSV para ver sus páginas."
        )
        self._sync_controls()

    def load_paths(
        self,
        paths: Iterable[Path],
        missing: Iterable[str] = (),
        page_refs: Iterable[tuple[Path | None, int]] | None = None,
    ) -> None:
        """Publica los PDF y la secuencia de páginas de la ejecución."""
        documents: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            path = Path(path)
            if not path.is_file() or path.suffix.lower() != ".pdf":
                continue
            key = str(path.resolve()).casefold()
            if key not in seen:
                seen.add(key)
                documents.append(path)
        self._documents = documents
        self._missing = [str(name) for name in missing]
        self._path = None
        self._page = 1
        self._total = 0
        self._global_index = 0
        self._base_image = None
        self._source = None
        self._page_counts = {}
        self._pending_render = None

        if page_refs is None:
            # Uso independiente del panel: sin CSV, todos los PDF forman una
            # secuencia física completa.
            self._execution_pages = [
                (document, page)
                for document in documents
                for page in range(1, self._page_total(document) + 1)
            ]
        else:
            self._execution_pages = []
            for path, page in page_refs:
                try:
                    local_page = int(page)
                except (TypeError, ValueError):
                    local_page = 0
                self._execution_pages.append(
                    (Path(path) if path is not None else None, local_page)
                )
        # El límite es el número de páginas de la ejecución, no las hojas
        # físicas que puedan seguir presentes en los originales.
        self._total = len(self._execution_pages)

        labels = _document_labels(documents)
        self.pdf_combo.blockSignals(True)
        self.pdf_combo.clear()
        for index, document in enumerate(documents):
            self.pdf_combo.addItem(labels[index], str(document))
            self.pdf_combo.setItemData(
                index, str(document), Qt.ItemDataRole.ToolTipRole
            )
        self.pdf_combo.blockSignals(False)
        self.pdf_combo.setEnabled(bool(documents))
        self.locate_button.setVisible(bool(self._missing))
        self._update_source_status()

        first_available = next(
            (
                index
                for index, (path, page) in enumerate(
                    self._execution_pages, start=1
                )
                if path is not None and page > 0 and path.is_file()
            ),
            None,
        )
        if first_available is not None:
            self.show_page(first_available)
            return
        self._show_placeholder(
            "No se encontraron los PDF de origen de este CSV."
            if self._missing
            else "Este CSV no registra los PDF de los que proviene."
        )
        self._sync_controls()

    def _on_document_changed(self, index: int) -> None:
        if 0 <= index < len(self._documents):
            document = self._documents[index]
            position = next(
                (
                    position
                    for position, (path, _page) in enumerate(
                        self._execution_pages, start=1
                    )
                    if path == document
                ),
                None,
            )
            if position is not None:
                self.show_page(position)

    def _sync_page_edit(self, texto: str) -> None:
        """Pone el número en la caja salvo que se esté escribiendo en ella.

        La caja la refresca cualquier cosa que mueva la página: la fila que
        se elige en la tabla, el render que llega, el PDF que se carga. Si
        eso pisa lo que la persona lleva tecleado, el salto se hace a la
        página que ya estaba y parece que el selector no obedece.
        """
        if self.page_edit.hasFocus() and self.page_edit.isModified():
            return
        self.page_edit.setText(texto)
        self.page_edit.setModified(False)

    def _jump(self) -> None:
        try:
            page = int(self.page_edit.text())
        except ValueError:
            # Un número a medias (o ninguno) no es un destino: la caja vuelve
            # a decir dónde está el visor.
            self.page_edit.setModified(False)
            self._sync_page_edit(str(self._global_page()))
            return
        self.page_edit.setModified(False)
        self.show_page(page)

    def _global_page(self, path: Path | None = None, page: int | None = None) -> int:
        """Posición de una referencia dentro de la ejecución."""
        if path is None and page is None:
            return self._global_index
        wanted_path = Path(path) if path is not None else self._path
        wanted_page = self._page if page is None else int(page)
        if wanted_path is None:
            return 0
        return next(
            (
                index
                for index, (source, local_page) in enumerate(
                    self._execution_pages, start=1
                )
                if source == wanted_path and local_page == wanted_page
            ),
            0,
        )

    def _global_location(self, page: int) -> tuple[Path | None, int] | None:
        """Convierte una página de ejecución en su referencia fuente."""
        if self._total <= 0:
            return None
        index = min(max(1, int(page)), self._total)
        return self._execution_pages[index - 1]

    def _show_previous_page(self) -> None:
        current = self._global_page()
        if current > 1:
            self.show_page(current - 1)

    def _show_next_page(self) -> None:
        current = self._global_page()
        if current < self._total:
            self.show_page(current + 1)

    def show_page(self, page: int, path: Path | None = None) -> None:
        """Pide una página al hilo de render.

        Sin ``path``, ``page`` es la página global del lote. Con ``path`` es la
        página local de ese PDF, como la que registra cada fila del CSV. La
        navegación muestra siempre la posición global y la imagen anterior se
        mantiene a la vista hasta que llega la nueva.
        """
        if path is None:
            global_index = min(max(1, int(page)), self._total)
            location = self._global_location(page)
            if location is None:
                return
            path, page = location
        else:
            path = Path(path)
            global_index = self._global_page(path, page)
        self._global_index = global_index
        self._sync_page_edit(str(global_index))
        if path is None or not path.is_file() or int(page) <= 0:
            self._path = path
            self._page = int(page)
            self._pending_render = None
            self._show_placeholder(
                # Sin documento y sin página no hay hoja que buscar: la
                # posición existe en la secuencia, pero no salió de ningún
                # escaneo (la página separadora de un batch, por ejemplo).
                "Esta página no viene de ninguna hoja escaneada."
                if path is None and int(page) <= 0
                else "No se encontró el PDF de esta página."
            )
            self._sync_controls()
            return
        document_total = self._page_total(path)
        if not document_total or int(page) > document_total:
            self._path = path
            self._page = int(page)
            self._pending_render = None
            self._show_placeholder(f"No se pudo mostrar el PDF: {path.name}")
            self._sync_combo_to(path)
            self._sync_controls()
            return
        page = int(page)
        request = (str(path), page)
        same_page = self._source is not None and path == self._path and page == self._page
        self._path = path
        self._page = page
        self._sync_combo_to(path)
        self._sync_controls()
        if self._pending_render == request:
            return
        if same_page:
            return
        self._pending_render = request
        self._ensure_loader().requested.emit(*request)

    def _on_page_ready(
        self, pdf_path: str, page: int, qimage: QImage | None
    ) -> None:
        """Aplica el render solo si sigue siendo la página pedida."""
        if (pdf_path, page) != self._pending_render:
            return  # respuesta obsoleta: el usuario ya cambió de fila
        self._pending_render = None
        if qimage is None:
            self._show_placeholder(
                f"No se pudo mostrar el PDF: {Path(pdf_path).name}"
            )
            self._sync_controls()
            return
        self._base_image = qimage
        self._apply_field_overlay()
        self._sync_controls()

    def set_field_overlay(
        self,
        template,
        visible: bool,
        field_ids: Iterable[str] | None = None,
    ) -> None:
        """Configura y repinta los recuadros de la plantilla sobre la pagina."""
        self._overlay_template = template
        self._overlay_visible = bool(visible and template is not None)
        self._overlay_field_ids = (
            set(field_ids) if field_ids is not None else None
        )
        self._apply_field_overlay()

    def _apply_field_overlay(self) -> None:
        """Compone los recuadros sin volver a rasterizar el PDF."""
        qimage = self._base_image
        if qimage is None or qimage.isNull():
            return
        pixmap = QPixmap.fromImage(qimage)
        template = self._overlay_template
        if self._overlay_visible and template is not None:
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            accent = QColor(accent_color())
            pen = QPen(accent, 2)
            fill = QColor(accent)
            fill.setAlpha(38)
            label = QColor(accent)
            label.setAlpha(210)
            font = QFont(self.font())
            font.setPointSize(FONT_CAPTION_PT)
            font.setBold(True)
            painter.setFont(font)
            width, height = qimage.width(), qimage.height()
            for field in template.fields:
                if (
                    self._overlay_field_ids is not None
                    and field.id not in self._overlay_field_ids
                ):
                    continue
                rect = QRectF(
                    field.x * width,
                    field.y * height,
                    field.w * width,
                    field.h * height,
                )
                painter.setBrush(fill)
                painter.setPen(pen)
                painter.drawRect(rect)
                painter.setBrush(label)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawText(
                    rect.adjusted(2, 2, -2, -2),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    field.id,
                )
            painter.end()
        self._source = pixmap
        self._render_page()

    def _sync_combo_to(self, path: Path) -> None:
        index = next(
            (
                position
                for position, document in enumerate(self._documents)
                if document == path
            ),
            -1,
        )
        if index >= 0 and index != self.pdf_combo.currentIndex():
            self.pdf_combo.blockSignals(True)
            self.pdf_combo.setCurrentIndex(index)
            self.pdf_combo.blockSignals(False)

    def _fit_scale(self) -> float:
        """Factor que hace caber la página completa en el panel."""
        source = self._source
        if source is None or source.isNull():
            return 1.0
        viewport = self.scroll.viewport().size()
        return max(
            0.05,
            min(
                max(1, viewport.width() - 12) / source.width(),
                max(1, viewport.height() - 12) / source.height(),
            ),
        )

    def _render_page(self) -> None:
        """Escala la página al panel sin volver a rasterizar el PDF."""
        source = self._source
        if source is None or source.isNull():
            return
        scale = self._fit_scale() * self._zoom
        pixmap = source.scaled(
            max(1, round(source.width() * scale)),
            max(1, round(source.height() * scale)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # El widget debe dedicar todo su rectángulo al pixmap. Un padding de
        # estilo reduce ``contentsRect`` después de fijar este mismo tamaño y
        # Qt termina ocultando los bordes de la bitácora.
        self.image.setContentsMargins(0, 0, 0, 0)
        self.image.setText("")
        self.image.setPixmap(pixmap)
        self.image.setFixedSize(pixmap.size())
        self._sync_zoom_controls()

    def _show_placeholder(self, text: str) -> None:
        """Deja el panel con un mensaje centrado y sin página cargada."""
        self._base_image = None
        self._source = None
        self.image.setContentsMargins(12, 12, 12, 12)
        self.image.setPixmap(QPixmap())
        self.image.setText(text)
        self.image.setFixedSize(self.scroll.viewport().size())
        self._sync_zoom_controls()

    def fit_page(self) -> None:
        """Vuelve al ajuste de página completa."""
        self._zoom = 1.0
        self._render_page()

    def zoom_by(self, factor: float) -> None:
        """Acerca o aleja la página; lo usan los atajos de la ventana."""
        self._zoom_by(factor)

    def _zoom_by(self, factor: float) -> None:
        self._zoom = min(_MAX_ZOOM, max(_MIN_ZOOM, self._zoom * factor))
        self._render_page()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - API Qt
        """Reajusta la página ante cualquier cambio del área visible.

        El panel también cambia de tamaño sin que lo haga la ventana: basta
        con que el indicador de origen pase de una línea a dos.
        """
        if (
            watched is self.scroll.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            self._schedule_refresh()
        return super().eventFilter(watched, event)

    def _schedule_refresh(self) -> None:
        if self._refresh_pending:
            return
        self._refresh_pending = True
        QTimer.singleShot(0, self._refresh_view)

    def _refresh_view(self) -> None:
        self._refresh_pending = False
        if self._source is not None:
            self._render_page()
        else:
            self.image.setFixedSize(self.scroll.viewport().size())

    def _update_source_status(self) -> None:
        """Indicador permanente del origen de las páginas mostradas."""
        available = len(self._documents)
        missing = len(self._missing)
        names = _join_names(self._missing)
        if not available and not missing:
            # Vale tanto antes de abrir un CSV como para uno sin PDF anotados;
            # el detalle lo da el mensaje del área de página.
            text = "Sin PDF de origen para mostrar."
            color = TEXT_SECONDARY
        elif not available:
            text = (
                f"No se encontró el PDF de origen ({names})."
                if missing == 1
                else f"No se encontraron los {missing} PDF de origen ({names})."
            ) + " Use «Ubicar PDF…»."
            color = PANE_STATUS_COLORS["ERROR"]
        elif missing:
            text = (
                f"{available} de {available + missing} PDF de origen "
                f"disponibles · {'falta' if missing == 1 else 'faltan'} {names}."
            )
            color = PANE_STATUS_COLORS["WARNING"]
        else:
            text = (
                "1 PDF de origen disponible."
                if available == 1
                else f"{available} PDF de origen disponibles."
            )
            color = PANE_STATUS_COLORS["OK"]
        self.source_status.setText(text)
        # Gana a la regla general de QLabel del panel, que va sin id.
        self.source_status.setStyleSheet(f"#pdfSourceStatus {{ color: {color}; }}")

    def _sync_zoom_controls(self) -> None:
        has_page = self._source is not None
        self.btn_zoom_out.setEnabled(has_page and self._zoom > _MIN_ZOOM)
        self.btn_zoom_in.setEnabled(has_page and self._zoom < _MAX_ZOOM)
        self.btn_zoom_fit.setEnabled(has_page)
        self.zoom_label.setText(f"{round(self._zoom * 100)}%")

    def _sync_controls(self) -> None:
        global_page = self._global_page()
        self.total_pages.setText(f"de {self._total}")
        # El validador va con el total que anuncia la etiqueta. Si se queda
        # corto, Qt considera incompleto lo que se teclea y no llega a emitir
        # ``editingFinished``: la tecla Enter no hace nada.
        validator = self.page_edit.validator()
        if isinstance(validator, QIntValidator):
            validator.setTop(max(1, self._total))
        self.page_edit.setEnabled(self._total > 0)
        self.prev.setEnabled(global_page > 1)
        self.next.setEnabled(0 < global_page < self._total)
        self._sync_zoom_controls()


class CsvViewerWindow(QMainWindow):
    """Ventana independiente que visualiza CSV de ejecuciones procesadas."""

    def __init__(self, start_folder: Path) -> None:
        # Debe ser una ventana nativa sin dueño. Un QMainWindow parentado sigue
        # pareciendo independiente dentro de Qt, pero Windows lo excluye de la
        # barra de tareas y puede administrar su marco separado del contenido.
        super().__init__(None, Qt.WindowType.Window)
        self._start_folder = Path(start_folder)
        self._folder: Path | None = None
        self._columns: list[str] = []
        self._rows: list[dict[str, str]] = []
        self._important_field_ids: set[str] = set()
        self._selected_important_columns: set[str] = set()
        self._template_name: str | None = None
        self._template = None
        self._important_fields_store = ImportantFieldsStore(
            _PROGRAM_DIR / IMPORTANT_FIELDS_FILENAME
        )
        self._row_pdf_paths: list[Path | None] = []
        self._pdf_search_folders: list[Path] = []
        # El CSV mínimo (el que se abre por primera vez, según el orden de
        # find_csv_files) no trae columnas ``_status``: se recorta al
        # exportar. El color de las celdas sale entonces de aquí, el JSON
        # compañero, indexado igual que las filas del CSV.
        self._field_statuses: dict[tuple[str, str], dict[str, str]] | None = None
        # Cada coincidencia es la fila del CSV y la columna donde apareció el
        # texto: la columna es lo que se muestra y sobre lo que se posa el
        # cursor, para que se vea por qué esa fila coincide.
        self._search_matches: list[tuple[int, str]] = []
        self._search_position = -1
        self._search_query = ""
        # El criterio de orden se guarda por nombre de columna, no por
        # índice: al reescribir la ejecución sin unas páginas el CSV se
        # vuelve a leer entero, y la vista resumida puede dejar otras
        # columnas a la vista. Sobrevive a un borrado; cambiar de CSV lo
        # descarta, porque el criterio era de la ejecución anterior.
        self._sort_state: tuple[str, bool] | None = None
        self._loaded_csv_path: Path | None = None
        # ``csv_field_id`` arma un conjunto con todas las columnas en cada
        # llamada, y la tabla la llamaba una vez por celda: en una ejecución
        # grande eso es el coste dominante de abrir el CSV. El mapa se
        # calcula una sola vez por archivo y se consulta por columna.
        self._field_id_by_column: dict[str, str | None] = {}
        self._column_set: set[str] = set()
        self._has_status_columns = False

        self._splitter_adjusted = False
        self._outputs_worker = None
        # Qué escribió la última ejecución del hilo de salidas: exportar o
        # eliminar páginas. El aviso final de cada una no es el mismo.
        self._outputs_context = "export"
        # El indicador de abajo lleva dos cosas: el resumen de la tabla, que
        # se rehace en cada cambio de columnas, y el estado de la exportación,
        # que sobrevive a esos cambios hasta que se abre otro CSV.
        self._summary = "Seleccione una carpeta o un CSV para visualizarlo."
        self._export_note = ""

        self.setWindowTitle("Visor de resultados CSV")
        # Como la ventana principal: el tamaño lo pone la pantalla. Pedía
        # 1400x840 y en un portátil de 1366x768 se abría más grande que el
        # escritorio, con la fila de exportación fuera de la vista.
        self._density = fit_to_screen(self, 1400, 840)
        # La misma hoja que la ventana principal: tipografía, botones y el
        # radio de los cuadros salen de ahí, no del estilo nativo.
        self._apply_density_stylesheet()
        # El panel de PDF recibe la densidad al construirse, dentro de
        # _build_ui: apretarlo después dejaba el mínimo holgado guardado en el
        # layout y la ventana se abría cien píxeles más alta de lo que pedía.
        self._build_ui()
        self._install_zoom_shortcuts()

    def _install_zoom_shortcuts(self) -> None:
        """Atajos Ctrl++ / Ctrl+- sobre la página, como en la ventana principal.

        Las dos vistas enseñan una página de bitácora dentro del mismo
        recuadro; el teclado que la acerca tiene que ser el mismo.
        """
        for secuencia, factor in (
            (QKeySequence("Ctrl++"), 1.25),
            (QKeySequence("Ctrl+="), 1.25),
            (QKeySequence("Ctrl+-"), 0.8),
        ):
            QShortcut(
                secuencia, self,
                activated=lambda f=factor: self.pdf_viewer.zoom_by(f),
            )

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        margin = max(SPACE_S, self._density.window_margin)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(self._density.root_spacing)
        self._root_layout = layout

        history_row = QHBoxLayout()
        history_row.setSpacing(SPACE_S)
        history_row.addWidget(QLabel("Historial:"))
        self.history_combo = QComboBox()
        self.history_combo.setToolTip(
            "Ejecuciones procesadas, de la más reciente a la más antigua. Las "
            "más viejas, con «Seleccionar carpeta…»"
        )
        self.history_combo.setAccessibleName("Ejecuciones procesadas recientes")
        # «activated» solo lo emite quien elige con el ratón o el teclado, así
        # que volver a elegir la ejecución que ya está abierta la recarga y
        # sincronizar la lista desde el código no dispara una carga.
        self.history_combo.activated.connect(self._on_history_activated)
        history_row.addWidget(self.history_combo, 1)
        layout.addLayout(history_row)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(SPACE_S)
        folder_row.addWidget(QLabel("Origen:"))
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setPlaceholderText(
            "Seleccione una carpeta procesada o un archivo CSV"
        )
        self.folder_edit.setAccessibleName("Origen de los CSV mostrados")
        folder_row.addWidget(self.folder_edit, 1)
        browse = QPushButton("Seleccionar carpeta…")
        browse.setToolTip("Abrir una carpeta procesada y listar sus CSV")
        browse.clicked.connect(self.browse_for_folder)
        folder_row.addWidget(browse)
        browse_csv = QPushButton("Seleccionar CSV…")
        browse_csv.setToolTip("Abrir directamente un archivo CSV procesado")
        browse_csv.clicked.connect(self.browse_for_csv)
        folder_row.addWidget(browse_csv)
        layout.addLayout(folder_row)

        controls = QHBoxLayout()
        controls.setSpacing(SPACE_S)
        controls.addWidget(QLabel("Archivo CSV:"))
        self.csv_combo = QComboBox()
        self.csv_combo.setEnabled(False)
        self.csv_combo.currentIndexChanged.connect(self._on_csv_changed)
        controls.addWidget(self.csv_combo, 1)
        layout.addLayout(controls)

        view_controls = QHBoxLayout()
        view_controls.setSpacing(SPACE_S)
        self.fields_check = QCheckBox("Mostrar campos")
        self.fields_check.setEnabled(False)
        self.fields_check.setToolTip(
            "Dibuja los recuadros de los campos sobre la pagina del visor."
        )
        self.fields_check.toggled.connect(self._apply_pdf_overlay)
        view_controls.addWidget(self.fields_check)
        view_controls.addStretch()
        view_controls.addWidget(QLabel("Vista de la tabla:"))
        self.column_toggle = CsvColumnModeButton()
        self.column_toggle.setEnabled(False)
        self.column_toggle.setVisible(False)
        self.column_toggle.toggled.connect(self._apply_column_mode)
        view_controls.addWidget(self.column_toggle)
        self.important_fields_button = ImportantFieldsButton()
        self.important_fields_button.setEnabled(False)
        self.important_fields_button.setVisible(False)
        self.important_fields_button.clicked.connect(self._open_field_selector)
        view_controls.addWidget(self.important_fields_button)
        layout.addLayout(view_controls)

        search_row = QHBoxLayout()
        search_row.setSpacing(SPACE_S)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Bitácora, matrícula, archivo, página… cualquier texto del CSV"
        )
        self.search_edit.setAccessibleName("Texto que se busca en el CSV")
        self.search_edit.setToolTip(
            "Busca en las columnas visibles; con el CSV completo, también en "
            "las ocultas. Cada coincidencia abre su página en el visor."
        )
        self.search_edit.returnPressed.connect(self._find_in_csv)
        search_row.addWidget(self.search_edit, 1)
        search = QPushButton("Buscar")
        search.setToolTip("Buscar el texto; repetido, pasa a la coincidencia siguiente")
        search.clicked.connect(self._find_in_csv)
        search_row.addWidget(search)
        self.search_prev = QPushButton("‹")
        self.search_prev.setToolTip("Coincidencia anterior")
        self.search_prev.clicked.connect(lambda: self._move_search(-1))
        search_row.addWidget(self.search_prev)
        self.search_next = QPushButton("›")
        self.search_next.setToolTip("Coincidencia siguiente")
        self.search_next.clicked.connect(lambda: self._move_search(1))
        search_row.addWidget(self.search_next)
        self.search_context = ElidedLabel(_SEARCH_HINT)
        self.search_context.setStyleSheet(f"color: {TEXT_SECONDARY};")
        self.search_context.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.search_context.setMinimumWidth(0)
        search_row.addWidget(self.search_context, 1)
        layout.addLayout(search_row)

        self.export_options = ExportOptionsGroup(raiz=_PROGRAM_DIR)
        layout.addWidget(self.export_options)

        # Horizontal, y con el PDF a la izquierda igual que la vista previa de
        # la ventana principal: una página de bitácora es vertical, así
        # aprovecha todo el alto en lugar de una franja de pocos centímetros.
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter = splitter
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        splitter.splitterMoved.connect(self._on_splitter_moved)
        self.pdf_viewer = EmbeddedPdfViewer(density=self._density)
        self.pdf_viewer.relocateRequested.connect(self._relocate_source_pdfs)
        splitter.addWidget(self.pdf_viewer)
        # Con modelo propio: una ejecución grande son más de doscientas mil
        # celdas, y un ítem por celda dejaba la ventana sin responder tanto
        # al abrir el CSV como al ordenarlo.
        self.table = QTableView()
        self.table_model = CsvTableModel(self.table)
        self.table_model.set_lookups(self._status_for, self._comment_for)
        self.table.setModel(self.table_model)
        self.table.setAccessibleName("CSV procesado")
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QTableView.SelectionMode.ExtendedSelection
        )
        self.table.setWordWrap(False)
        self.table.setToolTip(
            "Una fila por página: al elegirla se abre en el visor y Supr la "
            "quita de la ejecución. Marque casillas para borrar varias "
            "sueltas."
        )
        # Supr solo mientras la tabla tiene el foco: es la que sabe qué
        # páginas hay elegidas, y desde el buscador o el cuadro de salidas la
        # tecla no tiene por qué borrar nada.
        delete_pages = QShortcut(QKeySequence.StandardKey.Delete, self.table)
        delete_pages.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_pages.activated.connect(self._delete_selected_pages)
        # La barra espaciadora marca las filas elegidas, como en cualquier
        # lista con casillas: con muchas seguidas es lo que evita apuntar a
        # una casilla de dos milímetros una por una.
        marcar = QShortcut(QKeySequence(Qt.Key.Key_Space), self.table)
        marcar.setContext(Qt.ShortcutContext.WidgetShortcut)
        marcar.activated.connect(self._toggle_checked_rows)
        style_data_table(self.table)
        # El delegado pinta la fila marcada entera; aquí se le dice en qué
        # columna vive la casilla que lo decide.
        self.table.setProperty("checkColumn", CHECK_COLUMN)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionsMovable(False)
        self.table.horizontalHeader().setFixedHeight(30)
        self.table.horizontalHeader().setResizeContentsPrecision(_RESIZE_PRECISION)
        self.table_sort = ModelSortController(self.table, self.table_model)
        self.table_sort.sortChanged.connect(self._remember_sort)
        seleccion = self.table.selectionModel()
        seleccion.currentRowChanged.connect(self._on_current_row_changed)
        # ``currentRowChanged`` cubre teclado y búsquedas, pero no se emite
        # al volver a pulsar la fila que ya estaba activa. El clic debe volver
        # a ubicar su bitácora aunque entre ambos el usuario haya navegado el
        # PDF con las flechas o el campo de página.
        self.table.clicked.connect(self._on_table_clicked)
        splitter.addWidget(self.table)
        # El mismo reparto de la ventana principal: la tabla lleva muchas
        # columnas y la página cabe entera en la parte que le toca. Los
        # factores solo gobiernan el espacio sobrante, así que el reparto se
        # aplica además a mano: si no, el panel arranca con lo que pida su
        # contenido y se lleva más de la mitad de la ventana.
        splitter.setStretchFactor(0, _PDF_PANE_SHARE)
        splitter.setStretchFactor(1, _TABLE_SHARE)
        layout.addWidget(splitter, 1)

        status_row = QHBoxLayout()
        status_row.setSpacing(SPACE_S)
        self.status_label = ElidedLabel(self._summary)
        self.status_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        status_row.addWidget(self.status_label, 1)
        self.btn_depurar = QPushButton("Depurar")
        self.btn_depurar.setEnabled(False)
        self.btn_depurar.setToolTip(DEPURAR_TOOLTIP)
        self.btn_depurar.clicked.connect(self._depurar_paginas)
        status_row.addWidget(self.btn_depurar)
        self.btn_export = QPushButton("Exportar")
        self.btn_export.setEnabled(False)
        self.btn_export.setToolTip(_EXPORT_TOOLTIP)
        self.btn_export.clicked.connect(self._exportar)
        status_row.addWidget(self.btn_export)
        layout.addLayout(status_row)

        # Al final: el historial se sincroniza con lo que hay abierto y para
        # eso necesita el resto de los controles ya construidos.
        self._refresh_history()

    def _apply_density_stylesheet(self) -> None:
        """Hoja de la ventana con el fragmento de medidas de la densidad."""
        self.setStyleSheet(
            window_stylesheet(DATA_TABLE_QSS + self._density.qss)
        )

    def _update_responsive_layout(self) -> None:
        """Aprieta o suelta las medidas según el alto que tenga la ventana."""
        density = density_for(self.height(), self._density)
        if density is self._density:
            return
        self._density = density
        self._apply_density_stylesheet()
        margin = max(SPACE_S, density.window_margin)
        self._root_layout.setContentsMargins(
            margin, margin, margin, margin
        )
        self._root_layout.setSpacing(density.root_spacing)
        self.pdf_viewer.apply_density(density)

    def showEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().showEvent(event)
        self._refresh_history()
        self._update_responsive_layout()
        QTimer.singleShot(0, self._balance_content_splitter)

    def resizeEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().resizeEvent(event)
        if hasattr(self, "content_splitter"):
            self._update_responsive_layout()
            QTimer.singleShot(0, self._balance_content_splitter)

    def _on_splitter_moved(self, _position: int, _index: int) -> None:
        """Una vez que el usuario reparte el espacio, se respeta su medida."""
        self._splitter_adjusted = True

    def _balance_content_splitter(self) -> None:
        """Reparte el ancho entre visor y tabla mientras nadie lo ajuste."""
        if self._splitter_adjusted:
            return
        available = max(
            0,
            self.content_splitter.width() - self.content_splitter.handleWidth(),
        )
        pane = available * _PDF_PANE_SHARE // (_PDF_PANE_SHARE + _TABLE_SHARE)
        self.content_splitter.setSizes([pane, available - pane])

    def closeEvent(self, event) -> None:  # noqa: N802 - API Qt
        """Detiene los hilos antes de cerrar.

        El panel es un hijo de la ventana: cerrarla no le entrega un evento
        de cierre, y dejar su hilo de render vivo abortaría el proceso al
        destruir la ventana. La exportación no se puede interrumpir a mitad
        de escritura, así que se la espera.
        """
        self.pdf_viewer.shutdown()
        worker = self._outputs_worker
        if worker is not None and worker.isRunning():
            worker.wait(30000)
        super().closeEvent(event)

    def _refresh_history(self) -> None:
        """Rellena el historial con las últimas ejecuciones de la carpeta base.

        Se rehace cada vez que la ventana se muestra: el visor vive abierto
        mientras se procesa, y una ejecución recién terminada tiene que estar
        en la lista sin cerrar nada.
        """
        runs = find_run_dirs(self._start_folder, _HISTORY_LIMIT)
        self.history_combo.clear()
        if runs:
            # La opción con la que abre. La primera ejecución no está
            # cargada todavía, y dejar su nombre visible hacía creer que ya
            # se había elegido aunque la tabla siguiera vacía hasta volver a
            # seleccionarla; el texto de relleno solo se veía en un
            # desplegable sin nada elegido, que no es lo que se ve aquí.
            self.history_combo.addItem(TEXTO_ELEGIR_EJECUCION)
        else:
            # Un desplegable vacío no dice nada; así se lee que todavía no
            # hay nada procesado, no que la lista falló.
            self.history_combo.addItem("No hay ejecuciones procesadas todavía")
        for run in runs:
            self.history_combo.addItem(run.name, str(run))
        self.history_combo.setCurrentIndex(0)
        self.history_combo.setEnabled(bool(runs))
        self._sync_history_selection()

    def _sync_history_selection(self) -> None:
        """Deja marcada en el historial la ejecución que se está viendo."""
        csv_path = self._current_csv_path()
        current = run_dir_for_csv(csv_path) if csv_path is not None else None
        current = current or self._folder
        key = _folder_key(current) if current is not None else None
        for index in range(self.history_combo.count()):
            data = self.history_combo.itemData(index)
            if key is not None and data and _folder_key(data) == key:
                self.history_combo.setCurrentIndex(index)
                return
        # Abierta con «Seleccionar carpeta…» desde fuera del historial: no
        # está en la lista, y dejar marcada otra haría creer que se ve esa.
        if self.history_combo.count():
            self.history_combo.setCurrentIndex(0)

    def _on_history_activated(self, index: int) -> None:
        """Abre la ejecución elegida en el historial."""
        run = self.history_combo.itemData(index)
        if run:
            self.load_folder(Path(run))

    def browse_for_folder(self) -> None:
        initial = self._folder or self._start_folder
        selected = QFileDialog.getExistingDirectory(
            self, "Seleccionar carpeta procesada", str(initial)
        )
        if selected:
            self.load_folder(Path(selected))

    def browse_for_csv(self) -> None:
        initial = self._folder or self._start_folder
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Seleccionar CSV procesado",
            str(initial),
            "Reportes CSV (*.csv *.CSV);;Todos los archivos (*)",
        )
        if selected:
            self.load_csv_file(Path(selected))

    def load_folder(self, folder: Path) -> bool:
        """Carga los CSV encontrados en ``folder`` y muestra el primero."""
        csv_paths = find_csv_files(folder)
        if not csv_paths:
            QMessageBox.information(
                self,
                "Sin archivos CSV",
                "No se encontraron archivos CSV en la carpeta seleccionada.",
            )
            return False
        self._show_csv_choices(Path(folder), csv_paths, csv_paths[0])
        return True

    def load_csv_file(self, csv_path: Path) -> bool:
        """Abre un CSV concreto y deja a mano los demás de su carpeta."""
        csv_path = Path(csv_path)
        if not csv_path.is_file():
            QMessageBox.information(
                self,
                "Archivo no disponible",
                f"No se encontró el archivo CSV:\n{csv_path}",
            )
            return False
        siblings = find_csv_files(csv_path.parent)
        if not any(path == csv_path for path in siblings):
            siblings = [csv_path, *siblings]
        self._show_csv_choices(csv_path.parent, siblings, csv_path)
        return True

    def _show_csv_choices(
        self, folder: Path, csv_paths: list[Path], current: Path
    ) -> None:
        """Publica los CSV disponibles y abre el indicado."""
        self._folder = Path(folder)
        self.folder_edit.setText(str(self._folder))
        self.folder_edit.setToolTip(str(self._folder))
        self._pdf_search_folders = []
        self.csv_combo.blockSignals(True)
        self.csv_combo.clear()
        for path in csv_paths:
            try:
                label = str(path.relative_to(self._folder))
            except ValueError:
                label = path.name
            self.csv_combo.addItem(label, str(path))
        index = next(
            (
                position
                for position, path in enumerate(csv_paths)
                if path == current
            ),
            0,
        )
        self.csv_combo.setCurrentIndex(index)
        self.csv_combo.blockSignals(False)
        self.csv_combo.setEnabled(True)
        self._load_csv(csv_paths[index])
        self._sync_history_selection()

    def _on_csv_changed(self, index: int) -> None:
        if index >= 0:
            path = self.csv_combo.itemData(index)
            if path:
                self._load_csv(Path(path))

    def _load_csv(self, path: Path) -> None:
        try:
            # El archivo principal es deliberadamente corto. Para consultar,
            # ordenar y elegir columnas se usa su companero completo, sin
            # cambiar cual archivo eligio la persona ni ninguna salida.
            columns, rows = read_csv_file(csv_path_for_view(path))
            columns, rows = restore_run_columns(path, columns, rows)
        except (OSError, ValueError, csv.Error) as exc:
            QMessageBox.critical(
                self, "No se pudo abrir el CSV", f"{path}\n\n{exc}"
            )
            return

        self._export_note = ""
        # Otro CSV es otra ejecución: su orden no es el que alguien puso
        # aquí. Reescribir el mismo archivo sin unas páginas sí lo conserva,
        # que es de lo que se trata al borrar.
        if self._loaded_csv_path != path:
            self._sort_state = None
        self._loaded_csv_path = path
        self._columns = columns
        self._field_id_by_column = {
            column: csv_field_id(column, columns) for column in columns
        }
        # La comprobación de pertenencia también corría por celda sobre una
        # lista; con el conjunto es constante.
        self._column_set = set(columns)
        self._has_status_columns = any(
            column.endswith("_status") for column in columns
        )
        self._rows = rows
        # No se lee aquí: el CSV completo trae sus columnas ``_status`` y
        # nunca lo consulta, así que armar el mapa de la ejecución entera
        # (una entrada por página y campo) era medio segundo tirado en cada
        # apertura. Se arma la primera vez que una celda lo necesita.
        self._field_statuses = None
        # Se limpia antes de poblar: la tabla emite selección mientras se
        # llena y las rutas de la ejecución anterior ya no corresponden.
        self._row_pdf_paths = []
        self._important_field_ids = important_field_ids_for_csv(path, columns)
        # La selección guardada manda sobre la inferida: es la lista que el
        # usuario editó para esta plantilla, aquí o en la ventana principal.
        self._template_name = template_name_for_csv(path)
        self._template = template_for_csv(path)
        stored = self._important_fields_store.load(self._template_name)
        self._selected_important_columns = (
            set(stored)
            if stored is not None
            else set(important_csv_columns(columns, self._important_field_ids))
        )
        self._populate_table()
        self.column_toggle.setEnabled(True)
        self.column_toggle.setVisible(bool(columns))
        self.important_fields_button.setEnabled(bool(columns))
        self.important_fields_button.setVisible(bool(columns))
        self.fields_check.setEnabled(self._template is not None)
        self.fields_check.setToolTip(
            "Dibuja los recuadros de los campos sobre la pagina del visor."
            if self._template is not None
            else "Este CSV no identifica una plantilla disponible."
        )
        self._apply_column_mode()
        self._load_pdf_paths(path)
        self._search_matches = []
        self._search_position = -1
        self._search_query = ""
        self.search_context.setText(_SEARCH_HINT)
        self._sync_search_controls()
        self._sync_export_button()
        self.setWindowTitle(f"Visor de resultados CSV - {path.name}")

    def _statuses_from_companion(self) -> dict[tuple[str, str], dict[str, str]]:
        """Mapa de estados del JSON compañero, leído una sola vez por CSV."""
        if self._field_statuses is None:
            path = self._loaded_csv_path
            self._field_statuses = (
                self._load_field_statuses(path) if path is not None else {}
            )
        return self._field_statuses

    @staticmethod
    def _load_field_statuses(path: Path) -> dict[tuple[str, str], dict[str, str]]:
        """Estado de cada campo por página, leído del JSON compañero.

        Respaldo del color de la tabla cuando el CSV abierto es el mínimo y
        no trae columnas ``_status``. Sin JSON compañero (o sin ``reportes``
        en él) devuelve un mapa vacío y la tabla queda sin colorear, igual
        que antes de este respaldo.
        """
        statuses: dict[tuple[str, str], dict[str, str]] = {}
        try:
            reports = reports_from_companion(path)
        except Exception:  # noqa: BLE001 - color no crítico para el visor
            return statuses
        for report in reports:
            filename = report.source_filename.casefold()
            for page in report.pages:
                key = (filename, str(page.page_number))
                statuses[key] = {
                    field.field_id: field.status.value for field in page.fields
                }
        return statuses

    def _populate_table(self) -> None:
        """Publica el CSV en la tabla y ajusta sus columnas.

        La tabla no guarda copia de nada: el modelo lee del CSV que ya está
        en memoria y pinta solo lo que se ve, así que esto es inmediato por
        grande que sea la ejecución.
        """
        self.table_model.set_content(self._columns, self._rows)
        self.table_sort.reset()
        self._apply_column_mode()
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(CHECK_COLUMN, CHECK_COLUMN_WIDTH)
        self._restore_sort()

    def _toggle_checked_rows(self) -> None:
        """Marca o desmarca de una vez las filas resaltadas."""
        modelo = self.table.selectionModel()
        if modelo is None:
            return
        self.table_model.toggle_rows(
            sorted(indice.row() for indice in modelo.selectedRows())
        )

    def _remember_sort(self) -> None:
        """Anota por qué columna quedó ordenada la tabla tras un clic."""
        column = self.table_sort.sorted_column
        name = self.table_model.name_of(column)
        self._sort_state = (name, self.table_sort.descending) if name else None

    def _restore_sort(self) -> None:
        """Devuelve la tabla recién llenada al orden que ya tenía puesto."""
        if self._sort_state is None:
            return
        name, descending = self._sort_state
        column = self.table_model.column_of(name)
        if column < 0:
            # El CSV nuevo no trae esa columna: no hay criterio que aplicar
            # y tampoco uno que recordar.
            self._sort_state = None
            return
        self.table_sort.restore(column, descending)

    def _load_pdf_paths(self, csv_path: Path) -> None:
        """Publica en el visor los PDF de los que proviene el CSV."""
        self._row_pdf_paths, documents, missing = resolve_source_documents(
            csv_path, self._rows, self._pdf_search_folders
        )
        page_refs = []
        for path, row in zip(self._row_pdf_paths, self._rows):
            try:
                page = int(row.get("page", ""))
            except ValueError:
                page = 0
            page_refs.append((path, page))
        self.pdf_viewer.load_paths(documents, missing, page_refs)

    def _relocate_source_pdfs(self) -> None:
        """Reintenta la búsqueda de los PDF faltantes en otra carpeta."""
        selected = QFileDialog.getExistingDirectory(
            self,
            "Buscar los PDF de origen",
            str(self._folder or self._start_folder),
        )
        if not selected:
            return
        folder = Path(selected)
        if folder not in self._pdf_search_folders:
            self._pdf_search_folders.append(folder)
        csv_path = self._current_csv_path()
        if csv_path is not None:
            self._load_pdf_paths(csv_path)

    def _current_csv_path(self) -> Path | None:
        data = self.csv_combo.currentData()
        return Path(data) if data else None

    # ── Exportación ─────────────────────────────────────────────────────

    def _sync_export_button(self) -> None:
        """Solo se exporta la ejecución que el CSV abierto permite rehacer."""
        csv_path = self._current_csv_path()
        if csv_path is None or not _companion_payload(csv_path).get("reportes"):
            reason = (
                "Este CSV no viene acompañado del JSON de su ejecución, así que "
                "no se pueden volver a generar las salidas."
            )
        elif run_dir_for_csv(csv_path) is None:
            reason = (
                "Este CSV no está en la carpeta de su ejecución, y las salidas "
                "solo se pueden volver a generar sobre ella."
            )
        else:
            reason = ""
        self.btn_export.setToolTip(reason or _EXPORT_TOOLTIP)
        libre = (
            self._outputs_worker is None
            or not self._outputs_worker.isRunning()
        )
        self.btn_export.setEnabled(not reason and libre)
        # Depurar reescribe la misma ejecución que exportar, así que depende de
        # lo mismo: sin el JSON al lado no hay páginas que quitar.
        self.btn_depurar.setToolTip(reason or DEPURAR_TOOLTIP)
        self.btn_depurar.setEnabled(not reason and libre)

    def _important_columns_for_export(self, template) -> list[str]:
        """Columnas del CSV mínimo, independientes del dataset completo."""
        columns = CsvReporter.columns_for_fields(
            [field.id for field in template.fields],
            skip_ids=frozenset(
                field.id
                for field in template.fields
                if field.type.value == "signature"
            ),
        )
        return [
            column
            for column in columns
            if column in self._selected_important_columns
        ]

    def _checked_source_rows(self) -> list[int]:
        """Filas marcadas con la casilla de la primera columna."""
        return [
            source_row
            for source_row in self.table_model.checked_source_rows()
            if 0 <= source_row < len(self._rows)
        ]

    def _selected_source_rows(self) -> list[int]:
        """Filas del CSV elegidas, en el orden en que están en el CSV.

        La tabla se puede ordenar por cualquier columna, así que la posición
        en pantalla no dice qué fila del CSV es: eso lo lleva cada celda de
        la primera columna, que es la que sobrevive al reordenamiento.

        Manda lo marcado con las casillas en cuanto haya una marcada: es una
        elección que sobrevive a recorrer la tabla, mientras que el resalte
        se pierde con el clic siguiente. Sin ninguna marcada valen las filas
        resaltadas, que es como funcionaba antes de las casillas.
        """
        marcadas = self._checked_source_rows()
        if marcadas:
            return marcadas
        model = self.table.selectionModel()
        if model is None:
            return []
        rows: set[int] = set()
        for index in model.selectedRows():
            source_row = self.table_model.source_row(index.row())
            if 0 <= source_row < len(self._rows):
                rows.add(source_row)
        return sorted(rows)

    def _page_keys(self, source_rows: Iterable[int]) -> set[tuple[str, int]]:
        """Identifica las páginas elegidas por su PDF y su número de página.

        El PDF se identifica por su ruta cuando se sabe cuál es y por su
        nombre cuando no: es lo único que trae el CSV, y con el archivo
        perdido tampoco hay nada más con qué distinguirlo.
        """
        keys: set[tuple[str, int]] = set()
        for source_row in source_rows:
            row = self._rows[source_row]
            try:
                page = int(row.get("page", ""))
            except ValueError:
                continue
            path = (
                self._row_pdf_paths[source_row]
                if source_row < len(self._row_pdf_paths)
                else None
            )
            name = row.get("file", "")
            if path is not None:
                keys.add((_folder_key(path), page))
            if name:
                keys.add((name.casefold(), page))
        return keys

    @staticmethod
    def _page_matches(
        pdf_path: Path, page_number: int, keys: set, by_name: bool = True
    ) -> bool:
        """¿Esta página del reporte es una de las elegidas?

        Por la ruta cuando coincide, y si no por el nombre del archivo, que
        es lo único que guarda el CSV: el JSON conserva la ruta con la que se
        proceso y el original pudo haberse movido desde entonces.
        """
        if (_folder_key(pdf_path), page_number) in keys:
            return True
        return by_name and (pdf_path.name.casefold(), page_number) in keys

    def _corrida_para_eliminar(self):
        """Ejecución abierta lista para reescribirse sin algunas páginas.

        Devuelve el CSV, su carpeta, la plantilla con la que se procesó y sus
        reportes, o ``None`` tras avisar por qué no se puede. Lo comparten el
        borrado por selección y la depuración: las dos quitan páginas de la
        misma ejecución y necesitan exactamente lo mismo para hacerlo.
        """
        csv_path = self._current_csv_path()
        run_dir = run_dir_for_csv(csv_path) if csv_path is not None else None
        if csv_path is None or run_dir is None:
            QMessageBox.information(
                self,
                "Eliminar páginas",
                "Solo se pueden eliminar páginas de una ejecución completa, la "
                "que guarda su CSV y su JSON en la misma carpeta. Este CSV "
                "está suelto y no tiene de dónde quitarlas.",
            )
            return None
        template = template_for_csv(csv_path)
        if template is None:
            QMessageBox.warning(
                self,
                "Plantilla no disponible",
                "No se encontró la plantilla con la que se procesó esta "
                "ejecución, y sin ella no se pueden volver a escribir sus datos.",
            )
            return None
        try:
            reports = reports_from_companion(csv_path)
        except Exception as exc:  # noqa: BLE001 - se muestra en la GUI
            logger.error(f"No se pudo leer el JSON de la ejecución: {exc}")
            QMessageBox.critical(
                self,
                "No se pudieron eliminar las páginas",
                f"El JSON de la ejecución no se pudo leer:\n\n{exc}",
            )
            return None
        if not reports:
            QMessageBox.warning(
                self,
                "Sin datos de la ejecución",
                "Esta ejecución no trae el JSON con sus páginas, así que no se "
                "puede reescribir sin él.",
            )
            return None
        return csv_path, run_dir, template, reports

    def _depurar_paginas(self) -> None:
        """Quita de la ejecución las páginas repetidas o en blanco.

        Escribe lo mismo que el borrado por selección (CSV, JSON y
        estadísticas) y por lo mismo no rehace los PDF: la entrega se compone
        al exportar, cuando ya no queda nada más que quitar.
        """
        if self._outputs_worker is not None and self._outputs_worker.isRunning():
            self._export_note = "hay una escritura en curso; espere a que termine"
            self._refresh_status()
            return
        corrida = self._corrida_para_eliminar()
        if corrida is None:
            return
        csv_path, run_dir, template, reports = corrida

        dialog = DepurarPaginasDialog(reports, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        # Se borra lo que quedó marcado página por página, no el criterio
        # entero: el cuadro deja conservar una aparición distinta de la
        # primera, y esa elección se perdería al recontar por criterio.
        remaining, quitadas = depurar_claves(reports, dialog.claves())
        if not quitadas:
            self._export_note = (
                "no se marcó ninguna página"
                if dialog.hay_depurables()
                else "no había páginas repetidas ni en blanco"
            )
            self._refresh_status()
            return
        if not remaining:
            QMessageBox.information(
                self,
                "Depurar páginas",
                "Quedaría una ejecución sin ninguna página. Para deshacerse de "
                "la ejecución entera, elimine su carpeta desde output/.",
            )
            return

        logger.info(
            f"Depurando {quitadas} página(s) de la ejecución {run_dir.name}"
        )
        self._start_outputs(
            remaining,
            self._export_options(csv_path, template, remaining, skip_pdfs=True),
            note=f"eliminando {quitadas} página(s)…",
            context="eliminar",
        )

    def _delete_selected_pages(self) -> None:
        """Quita de la ejecución las páginas seleccionadas en la tabla.

        Se reescriben los datos de la ejecución (CSV mínimo, CSV completo, JSON
        y estadísticas) sin esas páginas, que es lo que consulta el visor y
        de donde sale cualquier exportación posterior. Los PDF ya entregados
        no se tocan: rehacerlos aquí dejaría dos entregas distintas de la
        misma ejecución en la carpeta, así que se rehacen al exportar.
        """
        if self._outputs_worker is not None and self._outputs_worker.isRunning():
            self._export_note = "hay una escritura en curso; espere a que termine"
            self._refresh_status()
            return
        source_rows = self._selected_source_rows()
        if not source_rows:
            self._export_note = "seleccione en la tabla las páginas que quiere eliminar"
            self._refresh_status()
            return

        corrida = self._corrida_para_eliminar()
        if corrida is None:
            return
        csv_path, run_dir, template, reports = corrida

        keys = self._page_keys(source_rows)
        # Dos PDF distintos que se llaman igual no se distinguen por nombre,
        # así que a esos solo los identifica su ruta. Si el archivo se movió
        # y la ruta ya no coincide no se borra nada, que es preferible a
        # quitarle la página al otro.
        repeated = {
            name
            for name in (Path(report.pdf_path).name.casefold() for report in reports)
            if [Path(r.pdf_path).name.casefold() for r in reports].count(name) > 1
        }
        remaining = []
        removed = 0
        for report in reports:
            pdf_path = Path(report.pdf_path)
            by_name = pdf_path.name.casefold() not in repeated
            pages = [
                page
                for page in report.pages
                if not self._page_matches(
                    pdf_path, page.page_number, keys, by_name
                )
            ]
            removed += len(report.pages) - len(pages)
            if not pages:
                continue
            report.pages = pages
            _recompute_summary(report)
            remaining.append(report)
        if not removed:
            self._export_note = "las páginas seleccionadas no están en el JSON"
            self._refresh_status()
            return
        if not remaining:
            QMessageBox.information(
                self,
                "Eliminar páginas",
                "Quedaría una ejecución sin ninguna página. Para deshacerse de "
                "la ejecución entera, elimine su carpeta desde output/.",
            )
            return

        # Se nombran las bitácoras además de contarlas: el número de
        # páginas no dice cuáles son, y una selección hecha sobre la
        # tabla ordenada abarca filas que pueden haber quedado fuera
        # de la vista.
        numeros = _numeros_de_bitacora(self._rows[row] for row in source_rows)
        cuantas = (
            "1 bitácora" if len(numeros) == 1 else f"{len(numeros)} bitácoras"
        )
        detalle = (
            f"Son {cuantas}: {_lista_de_bitacoras(numeros)}.\n\n"
            if numeros
            else ""
        )
        answer = QMessageBox.warning(
            self,
            "Confirmar eliminación",
            f"Se eliminarán {removed} página(s) de «{run_dir.name}».\n\n"
            f"{detalle}"
            "Se reescriben el CSV, el JSON y las estadísticas de la ejecución "
            "sin ellas. Los PDF ya exportados las conservan hasta que vuelva "
            "a exportar.\n\n¿Desea continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        logger.info(
            f"Eliminando {removed} página(s) de la ejecución {run_dir.name}"
        )
        self._start_outputs(
            remaining,
            self._export_options(
                csv_path, template, remaining, skip_pdfs=True
            ),
            note=f"eliminando {removed} página(s)…",
            context="eliminar",
        )

    def _exportar(self) -> None:
        """Regenera CSV, JSON y PDFs de la ejecución abierta, sin repetir OCR."""
        if self._outputs_worker is not None and self._outputs_worker.isRunning():
            return
        csv_path = self._current_csv_path()
        if csv_path is None or run_dir_for_csv(csv_path) is None:
            return

        template = template_for_csv(csv_path)
        if template is None:
            QMessageBox.warning(
                self,
                "Plantilla no disponible",
                "No se encontró la plantilla con la que se procesó esta "
                "ejecución, y sin ella no se pueden volver a generar las "
                "salidas.",
            )
            return
        try:
            reports, missing = reports_for_csv(csv_path, self._pdf_search_folders)
        except Exception as exc:  # noqa: BLE001 - se muestra en la GUI
            logger.error(f"No se pudo leer el JSON de la ejecución: {exc}")
            QMessageBox.critical(
                self,
                "No se pudo exportar",
                f"El JSON de la ejecución no se pudo leer:\n\n{exc}",
            )
            return
        if missing or not reports:
            QMessageBox.warning(
                self,
                "Faltan los PDF de origen",
                "No se encontraron los PDF de los que proviene esta ejecución "
                f"({_join_names(missing) or 'ninguno disponible'}). Las "
                "páginas se rehacen desde ellos, así que indique dónde están "
                "con «Ubicar PDF…» antes de exportar.",
            )
            return

        self._start_outputs(
            reports,
            self._export_options(csv_path, template, reports),
            note="exportando salidas…",
            context="export",
        )

    def _start_outputs(
        self, reports: list, options, note: str, context: str
    ) -> None:
        """Escribe las salidas en su propio hilo, venga de donde venga.

        Exportar y eliminar páginas escriben la misma ejecución con la misma
        función; solo cambian las opciones (la eliminación no rehace PDFs) y
        el aviso que queda en la franja de estado.
        """
        from app.gui.worker import OutputsWorker

        worker = OutputsWorker(reports, options, parent=self)
        self._outputs_worker = worker
        self._outputs_context = context
        worker.succeeded.connect(self._on_outputs_written)
        worker.failed.connect(self._on_outputs_failed)
        worker.progress.connect(self._on_outputs_stage)
        worker.finished.connect(self._on_outputs_finished)
        worker.finished.connect(worker.deleteLater)
        self.btn_export.setEnabled(False)
        self._export_note = note
        self._refresh_status()
        worker.start()

    def _export_options(
        self, csv_path: Path, template, reports: list, skip_pdfs: bool = False
    ):
        """Opciones de salida de la ejecución abierta, escritas sobre su carpeta."""
        from app.core.config import AppConfig, config_for_pdf
        from app.reports.outputs import OutputOptions

        config = AppConfig()
        # La resolución se acota al escaneo, igual que al procesar: nunca se
        # interpola la página por encima del detalle que trae el PDF. Sin
        # PDFs que rehacer no se mira ninguno: el archivo puede no estar y
        # para reescribir los datos da igual a qué resolución se escaneó.
        dpi = (
            config.dpi
            if skip_pdfs
            else config_for_pdf(config, Path(reports[0].pdf_path)).dpi
        )
        return OutputOptions(
            skip_pdfs=skip_pdfs,
            template=template,
            output_root=_PROGRAM_DIR / "output",
            dpi=dpi,
            crop_padding=config.crop_padding,
            separar_por=tuple(self.export_options.separar_por() or ()),
            un_solo_pdf=self.export_options.un_solo_pdf(),
            discrepancias=self.export_options.discrepancias_check.isChecked(),
            errores=self.export_options.errores_check.isChecked(),
            debug=False,
            run_dir=run_dir_for_csv(csv_path),
            csv_date_mode=self.export_options.csv_date_mode(),
            important_csv_columns=tuple(
                self._important_columns_for_export(template)
            ),
        )

    def _on_outputs_stage(self, message: str, _percent: int) -> None:
        self._export_note = f"generando salidas… {message}"
        self._refresh_status()

    def _on_outputs_written(self, output_dir: Path) -> None:
        """Recarga el CSV: la escritura acaba de rehacerlo en disco."""
        eliminando = self._outputs_context == "eliminar"
        accion = "Páginas eliminadas" if eliminando else "Exportación completada"
        logger.info(f"{accion} en: {output_dir}")
        csv_path = self._current_csv_path()
        if csv_path is not None and csv_path.is_file():
            self._load_csv(csv_path)
        self._export_note = (
            "páginas eliminadas de la ejecución; vuelva a exportar para "
            "rehacer los PDF sin ellas"
            if eliminando
            else f"exportación terminada: {Path(output_dir).name}"
        )
        self._refresh_status()

    def _on_outputs_failed(self, message: str) -> None:
        eliminando = self._outputs_context == "eliminar"
        logger.error(f"Error generando outputs: {message}")
        self._export_note = (
            "error al eliminar las páginas" if eliminando else "error al exportar"
        )
        self._refresh_status()
        QMessageBox.critical(
            self,
            "Error al eliminar las páginas" if eliminando else "Error al exportar",
            message.splitlines()[0] if message else "Error desconocido",
        )

    def _on_outputs_finished(self) -> None:
        self._outputs_worker = None
        self._sync_export_button()

    def _on_current_row_changed(self, current, _previous) -> None:
        """Sigue con el visor la fila activa de la tabla."""
        self._show_visible_row_in_pdf(current.row() if current.isValid() else -1)

    def _on_table_clicked(self, index) -> None:
        """Reubica también una fila que ya era la selección actual."""
        self._show_visible_row_in_pdf(index.row())

    def _show_visible_row_in_pdf(self, row: int) -> None:
        """Traduce una fila visible a su fila original y muestra su página."""
        source_row = self.table_model.source_row(row) if row >= 0 else -1
        if 0 <= source_row < len(self._rows):
            self._show_row_in_pdf(source_row)

    def _show_row_in_pdf(self, source_row: int) -> str:
        """Muestra la página que originó la fila y describe su ubicación."""
        row = self._rows[source_row]
        try:
            page = int(row.get("page", ""))
        except ValueError:
            page = 0
        path = (
            self._row_pdf_paths[source_row]
            if source_row < len(self._row_pdf_paths)
            else None
        )
        if path is not None and page > 0 and path.is_file():
            # ``_row_pdf_paths`` y ``_execution_pages`` nacen juntas y en el
            # mismo orden. Navegar por la posición de la fila evita inferirla
            # otra vez a partir de (ruta, página), que podría repetirse.
            self.pdf_viewer.show_page(source_row + 1)
            return f"{path.name}, página {page}"
        return f"{row.get('file', 'PDF desconocido')}, página {page or '?'}"

    def _searchable_columns(self) -> list[str]:
        """Columnas donde busca el texto: las que la tabla está mostrando.

        Así lo que se busca es lo que se ve. En la vista resumida eso son los
        campos de la bitácora (número, matrícula, fecha, archivo, página) y
        al pasar al CSV completo entran también la confianza, el estado y el
        comentario de cada campo, que es cuando se quiere buscar por ellos.
        """
        visible = [
            column
            for column in self._columns
            if not self.table.isColumnHidden(self.table_model.column_of(column))
        ]
        return visible or list(self._columns)

    def _matches_for(self, needle: str) -> list[tuple[int, str]]:
        """Filas que contienen el texto, con la columna en la que aparece.

        Primero las que lo tienen completo en una celda y después las que lo
        llevan dentro de un valor más largo: escribir una bitácora entera
        lleva a esa bitácora, no a la primera fila que la mencione de paso.
        Dentro de cada grupo se respeta el orden del CSV.
        """
        needle = needle.casefold()
        columns = self._searchable_columns()
        exact: list[tuple[int, str]] = []
        partial: list[tuple[int, str]] = []
        for index, row in enumerate(self._rows):
            for column in columns:
                value = (row.get(column) or "").strip().casefold()
                if value == needle:
                    exact.append((index, column))
                    break
                if needle in value:
                    partial.append((index, column))
                    break
        return exact + partial

    def _find_in_csv(self) -> None:
        """Busca el texto escrito y lleva la tabla y el visor a la primera fila.

        Repetir la búsqueda con el mismo texto avanza a la coincidencia
        siguiente, igual que el botón ›: es lo que se espera al volver a
        pulsar Intro sobre lo que ya se buscó.
        """
        value = self.search_edit.text().strip()
        if value and value.casefold() == self._search_query and self._search_matches:
            self._move_search(1)
            return
        self._search_query = value.casefold()
        self._search_matches = []
        self._search_position = -1
        if not value:
            self.search_context.setText(_SEARCH_HINT)
            self._sync_search_controls()
            return
        if not self._rows:
            self.search_context.setText("Abra un CSV para buscar en él.")
            self._sync_search_controls()
            return
        self._search_matches = self._matches_for(value)
        if not self._search_matches:
            self.search_context.setText(f"«{value}»: sin coincidencias.")
            self._sync_search_controls()
            return
        self._search_position = 0
        self._show_search_match()

    def _move_search(self, offset: int) -> None:
        if not self._search_matches:
            return
        self._search_position = (
            self._search_position + offset
        ) % len(self._search_matches)
        self._show_search_match()

    def _show_search_match(self) -> None:
        source_row, column = self._search_matches[self._search_position]
        display_row = self.table_model.display_row(source_row)
        if display_row >= 0:
            target = self._column_to_focus(column)
            index = self.table_model.index(display_row, target)
            self.table.selectRow(display_row)
            self.table.setCurrentIndex(index)
            self.table.scrollTo(index)

        location = self._show_row_in_pdf(source_row)
        self.search_context.setText(
            f"Coincidencia {self._search_position + 1} de "
            f"{len(self._search_matches)} en «{column}» · {location}"
        )
        self._sync_search_controls()

    def _column_to_focus(self, column: str) -> int:
        """Columna sobre la que se posa el cursor al mostrar una coincidencia.

        La que trae el texto encontrado, salvo que esté oculta: entonces la
        primera visible, porque el cursor sobre una columna que no se ve deja
        la fila seleccionada sin decir dónde está lo que se buscó.
        """
        index = self.table_model.column_of(column)
        if index >= 0 and not self.table.isColumnHidden(index):
            return index
        return next(
            (
                position
                for position in range(1, self.table_model.columnCount())
                if not self.table.isColumnHidden(position)
            ),
            CHECK_COLUMN,
        )

    def _sync_search_controls(self) -> None:
        multiple = len(self._search_matches) > 1
        self.search_prev.setEnabled(multiple)
        self.search_next.setEnabled(multiple)

    def _open_field_selector(self) -> None:
        dialog = ImportantFieldsDialog(
            self._columns, self._selected_important_columns, self
        )
        dialog.selectionChanged.connect(self._set_important_columns)
        dialog.exec()

    def _set_important_columns(self, columns: set[str]) -> None:
        self._selected_important_columns = self._important_fields_store.save(
            self._template_name, columns, self._columns
        )
        self._apply_column_mode()

    def _apply_pdf_overlay(self, _checked: bool | None = None) -> None:
        """Sincroniza los recuadros con el modo y la seleccion de columnas."""
        template = self._template
        important_only = self.column_toggle.isChecked()
        field_ids = None
        if template is not None and important_only:
            field_ids = template_field_ids_for_columns(
                self._selected_important_columns,
                [field.id for field in template.fields],
                self._columns,
            )
        self.pdf_viewer.set_field_overlay(
            template,
            self.fields_check.isChecked(),
            field_ids,
        )

    def _status_for(self, row: dict[str, str], column: str) -> str | None:
        if column == "dup":
            return "WARNING" if row.get(column, "").lower() == "true" else None
        field_id = self._field_id_by_column.get(column)
        if not field_id:
            return None
        status = row.get(f"{field_id}_status", "").upper()
        if status in STATUS_COLORS:
            return status
        if not self._has_status_columns:
            # Solo el CSV mínimo necesita el respaldo: el completo trae sus
            # columnas ``_status`` y consultarlas por celda obligaba a armar
            # el mapa de la ejecución entera para nada.
            key = (row.get("file", "").casefold(), row.get("page", ""))
            status = (
                self._statuses_from_companion()
                .get(key, {})
                .get(field_id, "")
                .upper()
            )
            if status in STATUS_COLORS:
                return status
        if field_id.endswith("_signature"):
            value = row.get(field_id, "").strip().lower()
            return {"true": "OK", "unclear": "WARNING", "false": "ERROR"}.get(
                value
            )
        return None

    def _comment_for(self, row: dict[str, str], column: str) -> str:
        """Lo que anotó la validación sobre ese campo de esa página."""
        field_id = self._field_id_by_column.get(column)
        return row.get(f"{field_id}_comment", "") if field_id else ""

    def _apply_column_mode(self, _checked: bool | None = None) -> None:
        important_only = self.column_toggle.isChecked()
        apply_csv_column_visibility(
            self.table,
            self._columns,
            self._important_field_ids,
            important_only,
            self._selected_important_columns,
            offset=CHECK_COLUMN + 1,
        )
        visible = (
            sum(
                1
                for column in self._columns
                if column in self._selected_important_columns
            )
            if important_only
            else len(self._columns)
        )
        self._summary = (
            f"{len(self._rows)} fila(s) · {visible} de {len(self._columns)} "
            "columnas visibles · solo lectura"
        )
        self._refresh_status()
        self._apply_pdf_overlay()

    def _refresh_status(self) -> None:
        """Resumen de la tabla, con el estado de la exportación al final."""
        note = f" · {self._export_note}" if self._export_note else ""
        self.status_label.setText(f"{self._summary}{note}")
