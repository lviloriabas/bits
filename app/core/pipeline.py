"""Orquestador del pipeline de validación de bitácoras.

Flujo por página:
    render → blank → deskew → alineación → recorte por región
    → OCR/firma/checkbox (OCR en batch por página) → postproceso
    → reglas de validación.

El pipeline depende de interfaces (OcrEngine, Template), por lo que se
pueden añadir motores OCR, nuevos tipos de campo o paralelismo sin
modificar esta clase.

Paralelismo: con ``workers > 1`` las páginas se reparten entre procesos
worker (cada uno con su propio motor OCR), porque PaddleOCR no suelta el
GIL y los hilos no escalan: medido, seis hilos de Python repartiendo
páginas rinden lo mismo que uno solo. ``cpu_threads`` controla los hilos
internos de cada motor; el reparto lo calcula ``app.core.parallelism``.
"""

from __future__ import annotations

import re
import pickle
import tempfile
import threading
import time
import weakref
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    wait,
)
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np
from loguru import logger

from app.core.config import AppConfig, config_for_pdf
from app.core.page_range import PageRange, slice_batch
from app.core.parallelism import (
    available_memory_mb,
    core_topology,
    reserved_memory_mb,
    total_memory_mb,
)
from app.core.progress import PAGES_STAGE, VOID_STAGE
from app.models.schemas import (
    FieldResult,
    PageResult,
    Status,
    ValidationReport,
)
from app.ocr.engine import OcrEngine
from app.ocr.date_ocr import read_date_slots
from app.ocr.regional import ocr_regions
from app.ocr.month_evidence import resolve_month_cells
from app.ocr.month_retry import retry_month
from app.templates.schema import FieldType, Template
from app.utils.postprocess import (
    _OCR_LETTER_TO_DIGIT_DICT,
    AMBIGUOUS_MATRICULA_NOTE,
    FLIGHT_FUZZY_NOTE,
    NUMERIC_MONTH_NOTE,
    WEAK_MATRICULA_NOTE,
    apply_postprocess,
    combine_date,
    month_candidates,
    _canonical_month,
)
from app.validation.page_status import recompute_page_status
from app.validation.validator import validate_page
from app.vision.alignment import (
    TransformResult,
    apply_transform,
    compute_similarity_transform,
    load_template_reference,
    scale_transform_for_shape,
    warp_with_transform,
)
from app.vision.blank_detection import is_blank
from app.vision.book_background import (
    MIN_BACKGROUND_PAGES,
    REFERENCE_DPI as BACKGROUND_REFERENCE_DPI,
    build_background,
    confident_band,
)
from app.vision.checkbox import detect_checkbox
from app.vision.ink_extent import crop_to_ink, strip_date_label
from app.vision.pdf_loader import (
    PdfPageRenderer,
    RenderedRegion,
    render_page,
)
from app.vision.preprocessing import crop_region, deskew, rotate, upscale_for_ocr
from app.vision.signature import (
    SIGNATURE_PAD_X,
    SIGNATURE_PAD_Y,
    UNCLEAR,
    background_peak,
    detect_signature,
    review_with_background,
)
from app.vision.reticula import leer_reticula, plantilla_ajustada
from app.vision.date_geometry import (
    DateFieldGeometry,
    field_override,
    locate_date_grid,
)
from app.vision.date_slots import (
    build_slot_maps,
    crop_slots,
    localize_slot,
    scale_slot_map,
)

ProgressCallback = Callable[[int, int, str], None]

_ORDER = {Status.OK: 0, Status.WARNING: 1, Status.ERROR: 2}

# Notas de postproceso "blandas": no son fallos del campo, solo avisos.
_SOFT_NOTES = (
    WEAK_MATRICULA_NOTE,
    # La matrícula reconstruida desde caracteres confundibles es una lectura
    # normal: el texto sigue siendo el del recorte, solo se anota de dónde
    # salieron los dígitos. Sin esto la página quedaría en ERROR y no podría
    # votar en el consenso de su libro, que es justo donde más aporta.
    AMBIGUOUS_MATRICULA_NOTE,
    # Un vuelo ajustado al vocabulario de la bitácora ("Tek" -> TCK,
    # "7S8" -> 758) sigue siendo la lectura de ese recorte: la nota dice de
    # dónde salió, no que el campo esté mal.
    FLIGHT_FUZZY_NOTE,
)

_WORKER_STATE: dict = {}


def _engine_kwargs(config: AppConfig, cpu_threads: Optional[int]) -> dict:
    """Parámetros estables de motor compartidos por todos los PDFs."""
    kwargs = {}
    if cpu_threads is not None:
        kwargs["cpu_threads"] = cpu_threads
    if config.ocr_rec_model:
        kwargs["rec_model"] = config.ocr_rec_model
    if config.ocr_det_model:
        kwargs["det_model"] = config.ocr_det_model
    return kwargs


def _init_ocr_pool_worker(
    config: AppConfig,
    engine_name: str,
    lang: str,
    cpu_threads: Optional[int],
    date_engine_name: Optional[str],
) -> None:
    """Carga los motores una sola vez para todo el batch de PDFs."""
    from app.ocr.engine import create_engine

    _WORKER_STATE.clear()
    kwargs = _engine_kwargs(config, cpu_threads)
    _WORKER_STATE["engine"] = create_engine(engine_name, lang=lang, **kwargs)
    if date_engine_name and date_engine_name != engine_name:
        _WORKER_STATE["date_engine"] = create_engine(
            date_engine_name, lang=lang, **kwargs
        )


def _load_worker_job(state_path: str) -> None:
    """Carga el estado de un PDF una vez en cada proceso persistente."""
    if _WORKER_STATE.get("state_path") == state_path:
        return
    with open(state_path, "rb") as stream:
        state = pickle.load(stream)
    renderer = _WORKER_STATE.get("renderer")
    if renderer is not None:
        renderer.close()
    engine = _WORKER_STATE["engine"]
    date_engine = _WORKER_STATE.get("date_engine")
    _WORKER_STATE.clear()
    _WORKER_STATE.update(state)
    _WORKER_STATE["engine"] = engine
    if date_engine is not None:
        _WORKER_STATE["date_engine"] = date_engine
    _WORKER_STATE["renderer"] = PdfPageRenderer(_WORKER_STATE["pdf_path"])
    _WORKER_STATE["state_path"] = state_path


def _load_calibration_job(state_path: str) -> dict:
    """Carga una vez por proceso el estado de calibración de un PDF."""
    if _WORKER_STATE.get("calib_state_path") != state_path:
        with open(state_path, "rb") as stream:
            state = pickle.load(stream)
        renderer = _WORKER_STATE.get("renderer")
        if renderer is not None:
            renderer.close()
        _WORKER_STATE["calibration"] = state
        _WORKER_STATE["renderer"] = PdfPageRenderer(state["pdf_path"])
        _WORKER_STATE["calib_state_path"] = state_path
        # El estado de OCR queda invalidado: su documento acaba de cerrarse,
        # así que la primera página que llegue lo volverá a cargar.
        _WORKER_STATE.pop("state_path", None)
    return _WORKER_STATE["calibration"]


def _calibrate_page_worker(
    page_number: int, state_path: str
) -> Tuple[TransformResult, Optional[float]]:
    """Calibra una página dentro de un proceso worker.

    Es la misma secuencia que la ruta en serie (render a DPI de calibración,
    deskew y estimación de similitud contra la referencia) sin nada del motor
    OCR: la calibración no lee texto, solo mide dónde está la página.
    """
    state = _load_calibration_job(state_path)
    config: AppConfig = state["config"]
    renderer: PdfPageRenderer = _WORKER_STATE["renderer"]
    image = renderer.render_page(page_number, state["calib_dpi"])
    angle: Optional[float] = None
    if config.deskew:
        image, angle = deskew(image)
    transform = compute_similarity_transform(image, state["calib_ref"], config)
    transform.tx *= state["factor"]
    transform.ty *= state["factor"]
    return transform, angle


# Los pools que siguen abiertos ahora mismo. Cerrar la ventana o cancelar
# solo puede pedir la parada y esperar: las paginas en vuelo terminan una a
# una, y con paginas grandes eso son minutos. Quien no quiera esperar corta
# por aqui. Es una WeakSet para que un pool olvidado no sobreviva por estar
# apuntado desde el registro.
_POOLS_VIVOS: "weakref.WeakSet[OcrProcessPool]" = weakref.WeakSet()
# El registro lo escriben los hilos de trabajo, al abrir y cerrar su pool, y
# lo lee el hilo de la interfaz al cortar. Sin cerrojo, recorrerlo mientras
# otro hilo lo modifica levanta RuntimeError justo en el momento en que hace
# falta que el corte funcione.
_POOLS_CERROJO = threading.Lock()


def abortar_pools() -> int:
    """Corta todos los pools abiertos sin esperar a lo que estan haciendo.

    Devuelve cuantos se cortaron. Las paginas en vuelo se pierden y las
    llamadas que esperaban su resultado levantan excepcion, que es
    exactamente lo que hace falta para que el pipeline se desenrede solo en
    lugar de quedarse esperando. La usa la interfaz cuando alguien pide
    forzar un cierre o una cancelacion, y no debe llamarse en un
    procesamiento que se quiera terminar.
    """
    with _POOLS_CERROJO:
        pools = list(_POOLS_VIVOS)
    for pool in pools:
        pool.abortar()
    if pools:
        logger.warning(
            f"[Pool] Cortados {len(pools)} pool(s) de OCR por orden expresa"
        )
    return len(pools)


class OcrProcessPool:
    """Pool de motores OCR reutilizable durante un batch completo."""

    def __init__(
        self,
        max_workers: int,
        config: AppConfig,
        engine_name: str,
        lang: str,
        cpu_threads: Optional[int],
        date_engine_name: Optional[str] = None,
    ) -> None:
        self.max_workers = max(1, max_workers)
        self._temporary = tempfile.TemporaryDirectory(prefix="bits_ocr_")
        self.executor = ProcessPoolExecutor(
            max_workers=self.max_workers,
            initializer=_init_ocr_pool_worker,
            initargs=(
                config, engine_name, lang, cpu_threads, date_engine_name,
            ),
        )
        self._job_index = 0
        self._closed = False
        with _POOLS_CERROJO:
            _POOLS_VIVOS.add(self)

    def prepare(self, state: dict) -> Path:
        self._job_index += 1
        path = Path(self._temporary.name) / f"pdf_{self._job_index}.pickle"
        with path.open("wb") as stream:
            pickle.dump(state, stream, protocol=pickle.HIGHEST_PROTOCOL)
        return path

    @staticmethod
    def release(path: Path) -> None:
        path.unlink(missing_ok=True)

    def temporary_path(self, name: str) -> Path:
        """Ruta privada del pool para señales/estado efímero del batch."""
        return Path(self._temporary.name) / name

    def close(self, cancel_futures: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        with _POOLS_CERROJO:
            _POOLS_VIVOS.discard(self)
        self.executor.shutdown(
            wait=True, cancel_futures=cancel_futures
        )
        self._temporary.cleanup()

    def abortar(self) -> None:
        """Mata los procesos del pool en vez de esperar a que acaben.

        Es el cierre a la fuerza. Se matan los hijos primero para que las
        paginas en curso no sigan gastando CPU despues de que nadie vaya a
        recoger su resultado, y solo despues se suelta el executor sin
        esperarlo. El directorio temporal puede seguir tomado por un hijo que
        aun no murio: si no se deja borrar, se deja estar, que lo limpia
        Windows con el resto de la carpeta temporal.
        """
        if self._closed:
            return
        self._closed = True
        with _POOLS_CERROJO:
            _POOLS_VIVOS.discard(self)
        for proceso in list(getattr(self.executor, "_processes", {}).values()):
            try:
                proceso.terminate()
            except Exception:  # noqa: BLE001 - ya podia estar muerto
                pass
        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001 - el pool ya esta roto, da igual
            pass
        try:
            self._temporary.cleanup()
        except OSError:
            pass

    def __enter__(self) -> "OcrProcessPool":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close(cancel_futures=exc_type is not None)

    def __del__(self) -> None:
        try:
            self.close(cancel_futures=True)
        except Exception:
            pass


def _init_worker(config: AppConfig, template: Template, engine_name: str,
                 lang: str, cpu_threads: Optional[int],
                 reference: Optional[np.ndarray], pdf_path: Path,
                 transforms: Optional[List[TransformResult]] = None,
                 own_reliability: Optional[List[bool]] = None,
                 printed_mask: Optional[np.ndarray] = None,
                 slot_map: Optional[dict] = None,
                 date_engine_name: Optional[str] = None,
                 first_page: int = 1,
                 skew_angles: Optional[List[float]] = None) -> None:
    """Inicializa un proceso worker: crea su propio motor OCR y guarda el
    estado compartido (config, plantilla, referencia, anclas, PDF)."""
    from app.ocr.engine import create_engine

    _WORKER_STATE["config"] = config
    _WORKER_STATE["template"] = template
    _WORKER_STATE["reference"] = reference
    _WORKER_STATE["pdf_path"] = Path(pdf_path)
    old_renderer = _WORKER_STATE.get("renderer")
    if old_renderer is not None:
        old_renderer.close()
    _WORKER_STATE["renderer"] = PdfPageRenderer(pdf_path)
    _WORKER_STATE["transforms"] = list(transforms or [])
    _WORKER_STATE["own_reliability"] = list(own_reliability or [])
    _WORKER_STATE["skew_angles"] = list(skew_angles or [])
    _WORKER_STATE["first_page"] = max(1, first_page)
    _WORKER_STATE["slot_map"] = dict(slot_map or {})
    if printed_mask is not None:
        _WORKER_STATE["printed_mask"] = np.ascontiguousarray(printed_mask)
    kwargs = _engine_kwargs(config, cpu_threads)
    _WORKER_STATE["engine"] = create_engine(engine_name, lang=lang, **kwargs)
    if date_engine_name and date_engine_name != engine_name:
        _WORKER_STATE["date_engine"] = create_engine(
            date_engine_name, lang=lang, **kwargs
        )


def _process_page_worker(
    page_number: int, state_path: Optional[str] = None
) -> PageResult:
    """Procesa una página dentro de un proceso worker."""
    if state_path is not None:
        _load_worker_job(state_path)
    transforms = _WORKER_STATE.get("transforms") or []
    reliability = _WORKER_STATE.get("own_reliability") or []
    # Las anclas cubren solo el tramo procesado, así que se indexan desde su
    # primera página y no desde la página 1 del documento.
    index = page_number - _WORKER_STATE.get("first_page", 1)
    transform = transforms[index] if 0 <= index < len(transforms) else None
    if transform is not None:
        reliable: Optional[bool] = transform.reliable
    elif 0 <= index < len(reliability):
        reliable = reliability[index]
    else:
        reliable = None
    angles = _WORKER_STATE.get("skew_angles") or []
    skew_angle = angles[index] if 0 <= index < len(angles) else None
    return process_page(
        _WORKER_STATE["pdf_path"],
        page_number,
        _WORKER_STATE["config"],
        _WORKER_STATE["engine"],
        _WORKER_STATE["template"],
        _WORKER_STATE["reference"],
        transform=transform,
        transform_reliable=reliable,
        printed_mask=_WORKER_STATE.get("printed_mask"),
        slot_map=_WORKER_STATE.get("slot_map"),
        date_engine=_WORKER_STATE.get("date_engine"),
        renderer=_WORKER_STATE.get("renderer"),
        skew_angle=skew_angle,
    )


def process_page(
    pdf_path: Path,
    page_number: int,
    config: AppConfig,
    engine: OcrEngine,
    template: Template,
    reference: Optional[np.ndarray],
    transform: Optional[TransformResult] = None,
    transform_reliable: Optional[bool] = None,
    printed_mask: Optional[np.ndarray] = None,
    slot_map: Optional[dict] = None,
    date_engine: Optional[OcrEngine] = None,
    renderer: Optional[PdfPageRenderer] = None,
    skew_angle: Optional[float] = None,
) -> PageResult:
    """Renderiza y procesa una página del PDF (función de proceso worker)."""
    if renderer is None:
        with PdfPageRenderer(pdf_path) as opened:
            return process_page(
                pdf_path, page_number, config, engine, template, reference,
                transform, transform_reliable, printed_mask, slot_map,
                date_engine=date_engine, renderer=opened,
                skew_angle=skew_angle,
            )
    image = renderer.render_page(page_number, config.dpi)
    return process_page_image(image, page_number, config, engine, template,
                              reference, transform, transform_reliable,
                              printed_mask, slot_map, date_engine=date_engine,
                              date_renderer=renderer, skew_angle=skew_angle)


def process_page_image(
    image: np.ndarray,
    page_number: int,
    config: AppConfig,
    engine: OcrEngine,
    template: Template,
    reference: Optional[np.ndarray],
    transform: Optional[TransformResult] = None,
    transform_reliable: Optional[bool] = None,
    printed_mask: Optional[np.ndarray] = None,
    slot_map: Optional[dict] = None,
    date_engine: Optional[OcrEngine] = None,
    date_image: Optional[np.ndarray] = None,
    date_renderer: Optional[PdfPageRenderer] = None,
    skew_angle: Optional[float] = None,
) -> PageResult:
    """Procesa una imagen de página completa contra la plantilla.

    ``skew_angle`` es el ángulo que la calibración ya midió para esta página.
    Cuando viene dado se aplica tal cual y no se vuelve a buscar: detectarlo
    (Canny + Hough) cuesta 103-139 ms a DPI completo, y la calibración ya
    pagó esa búsqueda sobre la misma página a la mitad de resolución. El
    ángulo de una línea no depende de la escala; medido sobre 48 páginas de
    cuatro PDFs, ambas resoluciones coinciden en 0,0000°, y sobre páginas
    inclinadas a propósito la medida de calibración es incluso la más
    ajustada de las dos (error máximo 0,30° frente a 0,69°). Con None se
    detecta aquí, como antes.
    """
    t_start = time.perf_counter()
    page = PageResult(page_number=page_number)
    logger.info(f"[Página {page_number}] Inicio de procesamiento")

    # 1) Página en blanco
    if is_blank(image, config.blank_threshold):
        page.blank = True
        page.status = Status.WARNING
        page.comment = "Blank page detected"
        logger.warning(f"[Página {page_number}] En blanco")
        page.processing_ms = round((time.perf_counter() - t_start) * 1000, 1)
        return page

    # 2) Corrección de inclinación
    deskew_angle = 0.0
    if config.deskew:
        if skew_angle is None:
            image, deskew_angle = deskew(image)
        else:
            # Ángulo ya medido en la calibración: solo queda aplicarlo.
            deskew_angle = float(skew_angle)
            if abs(deskew_angle) > 0.0:
                image = rotate(image, deskew_angle)
        page.skew_angle = round(deskew_angle, 3)
        if date_image is not None and abs(deskew_angle) > 0.0:
            # La imagen de fecha se renderiza a otro DPI, pero debe conservar
            # exactamente el mismo ángulo que la página principal.
            date_image = rotate(date_image, deskew_angle)
        if abs(deskew_angle) > 0.05:
            logger.info(
                f"[Página {page_number}] Deskew: {deskew_angle:.2f}°"
            )

    # 3) Alineación con la plantilla (ancla estabilizada por batch si existe)
    alignment_transform: Optional[TransformResult] = None
    alignment_source_shape = image.shape
    if config.align and reference is not None:
        if transform is not None:
            own_reliable = (
                transform_reliable
                if transform_reliable is not None
                else transform.reliable
            )
            if transform.reliable:
                alignment_transform = transform
            quality = "ok" if own_reliable else "low"
        else:
            own = compute_similarity_transform(image, reference, config)
            if own.reliable:
                alignment_transform = own
            quality = "ok" if own.reliable else "low"
        if alignment_transform is not None:
            if image.shape[:2] == reference.shape[:2]:
                image = apply_transform(image, alignment_transform)
            else:
                image = warp_with_transform(
                    image,
                    alignment_transform,
                    (reference.shape[1], reference.shape[0]),
                )
            if date_image is not None:
                date_transform = scale_transform_for_shape(
                    alignment_transform, alignment_source_shape, date_image.shape
                )
                target_width = round(
                    reference.shape[1]
                    * date_image.shape[1] / max(alignment_source_shape[1], 1)
                )
                target_height = round(
                    reference.shape[0]
                    * date_image.shape[0] / max(alignment_source_shape[0], 1)
                )
                if date_image.shape[:2] == (target_height, target_width):
                    date_image = apply_transform(date_image, date_transform)
                else:
                    date_image = warp_with_transform(
                        date_image,
                        date_transform,
                        (max(1, target_width), max(1, target_height)),
                    )
        page.alignment_quality = quality
        if quality != "ok":
            logger.warning(f"[Página {page_number}] Alineación: {quality}")

    # El visor carga una miniatura de la página original. Guardamos la
    # transformación aplicada en coordenadas relativas para poder reproducir
    # exactamente deskew + alineación a cualquier resolución de vista previa.
    if alignment_transform is not None:
        source_height, source_width = alignment_source_shape[:2]
        height, width = image.shape[:2]
        page.preview_alignment = {
            "rot": float(alignment_transform.rot),
            "tx_ratio": float(alignment_transform.tx) / max(source_width, 1),
            "ty_ratio": float(alignment_transform.ty) / max(source_height, 1),
            "scale": float(alignment_transform.scale),
        }
        if width != source_width or height != source_height:
            page.preview_alignment.update({
                "target_width_ratio": float(width) / max(source_width, 1),
                "target_height_ratio": float(height) / max(source_height, 1),
            })

    # 3.4) Colocar los campos sobre las rayas impresas de *esta* página. Las
    # coordenadas de la plantilla miden desde el borde del lienzo, que depende
    # de cómo cayó la hoja en el escáner; la retícula impresa no. Medido sobre
    # doce páginas de ocho bitácoras, el borde de un campo se separaba de su
    # raya real entre 8 y 11 px según el libro, hasta el 22% del alto del
    # campo. Se mide después de alinear, así el resultado no depende de lo
    # bien que saliera la alineación. Si la retícula no se identifica, la
    # plantilla vuelve tal cual y los campos se quedan donde siempre.
    if template.reticula is not None:
        reticula = leer_reticula(template, image)
        page.reticula_rayas = len(reticula.y) + len(reticula.x)
        if reticula.fiable:
            template = plantilla_ajustada(template, reticula)
        else:
            logger.warning(
                f"[Página {page_number}] Retícula impresa no identificada: "
                f"los campos se leen en su posición de plantilla"
            )

    # 3.5) Preparar una copia sin fondo impreso para las casillas. No se usa
    # ni para OCR (si varias páginas repiten la misma escritura, el consenso
    # del fondo también la clasifica como impresa) ni para firmas (la máscara
    # abre huecos dentro de los trazos). Solo se construye si la plantilla
    # tiene casillas: es una copia de la página entera.
    analysis_image = image
    has_checkboxes = any(
        field.type is FieldType.CHECKBOX for field in template.fields
    )
    if printed_mask is not None and has_checkboxes:
        if printed_mask.shape != image.shape[:2]:
            mask = cv2.resize(
                printed_mask.astype(np.uint8),
                (image.shape[1], image.shape[0]),
                interpolation=cv2.INTER_NEAREST,
            ).astype(bool)
        else:
            mask = printed_mask
        analysis_image = image.copy()
        analysis_image[mask] = 255

    # 3.7) Render regional de la fecha. Incluye el margen completo de búsqueda
    # de la retícula: la geometría dinámica conserva así los 600 DPI nativos,
    # pero PyMuPDF rasteriza solo ~5% de la hoja.
    date_region: Optional[RenderedRegion] = None
    if (
        date_image is None
        and date_renderer is not None
        and config.date_dpi > config.dpi
    ):
        band_rect = _date_band_rect(
            template, {}, margin_x=0.045, margin_y=0.035
        )
        if band_rect is not None:
            date_region = date_renderer.render_aligned_region(
                page_number,
                config.date_dpi,
                band_rect,
                alignment_source_shape,
                target_shape=image.shape,
                deskew_angle=deskew_angle,
                alignment=alignment_transform,
            )

    # Geometría dinámica de la banda de fecha: ajusta las casillas a la
    # retícula impresa de esta página. Si la retícula no encaja, se usa la
    # plantilla. En el flujo B se detecta dentro del recorte de alta DPI y se
    # proyecta de vuelta al sistema normalizado de la página completa.
    date_geometries: dict[str, DateFieldGeometry] = {}
    date_overrides: dict = {}
    geom_shape = image.shape
    if config.date_dynamic_geometry:
        if date_region is not None:
            local_template = template.model_copy(update={
                "fields": [
                    date_region.local_field(field)
                    for field in template.fields
                ]
            })
            local_geometries = locate_date_grid(
                date_region.image,
                local_template,
                config.printed_ink_threshold,
                coordinate_shape=(
                    round(date_region.image.shape[0] / date_region.rect[3]),
                    round(date_region.image.shape[1] / date_region.rect[2]),
                ),
            )
            date_geometries = _date_geometries_from_region(
                local_geometries, date_region, image.shape
            )
        else:
            geom_source = date_image if date_image is not None else image
            geom_shape = geom_source.shape
            date_geometries = locate_date_grid(
                geom_source, template, config.printed_ink_threshold
            )
        for field_id, geometry in date_geometries.items():
            base_field = template.field(field_id)
            if base_field is not None:
                date_overrides[field_id] = field_override(
                    geometry, base_field, geom_shape
                )

    # Las siete celdas manuscritas heredan la retícula encontrada. Así el
    # OCR por carácter no usa rectángulos estáticos que puedan incluir el
    # rótulo impreso o cortar un trazo cuando la página se desplazó.
    char_overrides = _date_cell_overrides(
        template, date_geometries, geom_shape,
    )
    ocr_overrides = {**date_overrides, **char_overrides}

    # Rectángulos efectivos usados por esta página. Incluyen tanto la
    # geometría dinámica DD|MMM|AA como sus siete celdas individuales.
    # Los campos sin ajuste conservan su geometría de plantilla.
    page.preview_boxes = {
        field.id: [
            float(effective.x),
            float(effective.y),
            float(effective.w),
            float(effective.h),
        ]
        for field in template.fields
        for effective in (ocr_overrides.get(field.id, field),)
    }

    # 4-7) Campos: recorte → OCR (en batch)/firma/checkbox → postproceso
    ocr_fields = [
        field for field in template.fields
        if field.type not in (FieldType.SIGNATURE, FieldType.CHECKBOX)
        # El día que no se va a representar no se lee. Sigue en la plantilla
        # (la retícula de la banda se detecta con su casilla) y sigue
        # teniendo su campo en la página; lo que no se hace es su OCR.
        and (config.read_day or field.id not in DAY_FIELD_IDS)
    ]
    ocr_results = ocr_regions(engine, image, ocr_fields,
                              config.crop_padding,
                              preprocess=config.crop_preprocess,
                              dpi=config.dpi,
                              date_engine=date_engine,
                              date_image=date_image,
                              date_region=date_region,
                              overrides=ocr_overrides,
                              accept=_accept_line_reading)
    by_id = {field.id: result
             for field, result in zip(ocr_fields, ocr_results)}

    for field in template.fields:
        if field.type in (FieldType.SIGNATURE, FieldType.CHECKBOX):
            if field.type is FieldType.SIGNATURE:
                # La firma se lee sobre la página tal cual. La máscara de
                # fondo impreso se calcula por consenso a media resolución y
                # abre huecos dentro de los trazos; el detector ya descarta
                # la estructura impresa por su cuenta.
                result = detect_signature(
                    crop_region(image, field,
                                pad_x=SIGNATURE_PAD_X, pad_y=SIGNATURE_PAD_Y),
                    field, page_number, dpi=config.dpi,
                )
                if page.alignment_quality != "ok":
                    # La duda queda anotada, pero no borra la lectura. Sin
                    # ancla fiable la página no se transforma: el recorte cae
                    # donde lo pone la plantilla, que es donde ya caía. Medido
                    # sobre un libro que sí alinea, aplicar la transformación
                    # o no cambia el veredicto en 4 de 155 recortes, y siempre
                    # entre un veredicto firme y «unclear»: nunca convierte
                    # una firma presente en ausente ni al revés. La corrección
                    # es de unos pocos píxeles sobre una casilla de cientos.
                    #
                    # Bajar la confianza a 0.40 la dejaba por debajo de los
                    # dos umbrales del campo, así que las cinco firmas de la
                    # página se volvían ilegibles a la vez, el tipo de entrada
                    # quedaba indeciso y la página salía marcada como
                    # discrepancia hubiera lo que hubiera en ella.
                    result.status = Status.WARNING
                    result.inference_method = "alignment_low"
                    result.comment = (
                        f"{result.comment} | Alineación no confiable"
                    )
            else:
                result = detect_checkbox(
                    crop_region(analysis_image, field, config.crop_padding),
                    field, page_number,
                )
        else:
            raw_text, confidence = by_id.get(field.id, ("", 0.0))
            text, note = raw_text, ""
            recovered = False
            recovered_via = ""
            alternatives: List[str] = []
            if field.postprocess == "month":
                candidates = month_candidates(raw_text)
                if len(candidates) > 1:
                    alternatives = [_canonical_month(n) for n in candidates]
            is_date_field = field.postprocess in _DATE_FIELDS
            field_image = (
                date_region.image
                if is_date_field and date_region is not None
                else date_image
                if is_date_field and date_image is not None
                else image
            )
            field_dpi = (
                config.date_dpi
                if is_date_field and (
                    date_image is not None or date_region is not None
                )
                else config.dpi
            )
            # Geometría ajustada por la retícula impresa de esta página.
            crop_field = (
                date_overrides.get(field.id, field)
                if is_date_field else field
            )
            if is_date_field and date_region is not None:
                crop_field = date_region.local_field(crop_field)
            if raw_text and field.postprocess:
                text, note = apply_postprocess(
                    field.id, field.postprocess, raw_text
                )
                if note == WEAK_MATRICULA_NOTE:
                    confidence = round(confidence * 0.4, 3)
            # Redundancia por ranuras: la casilla de fecha está partida por
            # separadores verticales impresos; se lee carácter por carácter
            # con restricciones (dígitos / abreviatura de mes). Se prefiere
            # el mapa de la retícula detectada en esta página; si no, el
            # mapa global del fondo impreso.
            page_spec = None
            geometry = date_geometries.get(field.id)
            if geometry is not None:
                page_spec = geometry.slot_spec()
            elif slot_map is not None and field.id in slot_map:
                page_spec = slot_map[field.id]
                if field_image is not image and date_region is None:
                    page_spec = scale_slot_map(
                        page_spec, image.shape, field_image.shape
                    )
            if page_spec is not None and date_region is not None:
                page_spec = _slot_map_for_region(
                    page_spec, image.shape[1], date_region
                )
            if (
                config.date_slot_ocr
                and is_date_field
                and page_spec is not None
            ):
                fb = _slot_ocr_fallback(
                    crop_field, field_image, config.crop_padding, page_spec,
                    preprocess=config.crop_preprocess,
                    dpi=field_dpi,
                    engine=(
                        date_engine if date_engine is not None else engine
                    ),
                )
                if fb is not None:
                    fb_text, fb_conf = fb
                    candidate_text, fb_note = apply_postprocess(
                        field.id, field.postprocess, fb_text
                    )
                    if candidate_text and _accept_slot_candidate(
                        field.postprocess, text, confidence,
                        fb_text, fb_conf,
                    ):
                        if text and text != candidate_text:
                            alternatives.append(text)
                        text = candidate_text
                        confidence = fb_conf
                        note = fb_note
                        recovered = True
                        recovered_via = "ranuras"
                        logger.info(
                            f"[Página {page_number}] {field.id}: "
                            f"valor recuperado por ranuras de casilla "
                            f"({fb_text!r})"
                        )
                    elif candidate_text and candidate_text != text:
                        # Candidato válido pero no aceptado: queda como
                        # alternativa para el consenso por libro.
                        alternatives.append(candidate_text)
            status = Status.OK
            if note and not any(n in note for n in _SOFT_NOTES):
                status = Status.ERROR
            if NUMERIC_MONTH_NOTE in note:
                # El valor numérico solo queda ratificado después, si las
                # celdas posicionales contienen exclusivamente esos dígitos.
                status = Status.ERROR
            result = FieldResult(
                page_number=page_number,
                field_id=field.id,
                field_type=field.type.value,
                value=text or None,
                confidence=confidence,
                status=status,
                comment=note,
                raw_value=raw_text or None,
                source="ocr_fallback" if recovered else "ocr",
                inference_method=recovered_via or None,
                alternatives=alternatives,
            )
            if recovered and not note:
                result.comment = f"OCR respaldo ({recovered_via}): {fb_text!r}"
        page.add_field(result)

    # 7a) Unir las celdas de carácter (day_1..year_2) en day/month/year
    _join_char_fields(page)
    retry_month(
        page, image, template, engine, date_engine=date_engine,
        date_image=date_image, date_region=date_region,
        overrides=ocr_overrides, dpi=config.dpi,
    )

    # 7b) Combinar day/month/year en una sola fecha normalizada
    _combine_date_parts(page)

    # 8) Reglas de validación (required/regex/longitud/confianza)
    validate_page(page, template, config)
    if not config.read_day:
        # El día es obligatorio en la plantilla, así que las reglas lo dejan
        # en ERROR por vacío. No lo está: nadie lo miró. Se anota como lo que
        # es y se deja en WARNING, que es donde el corrector lo recoge para
        # completarlo con el último día que cabe en el libro.
        _mark_day_not_read(page)
    if page.alignment_quality != "ok":
        if page.status is Status.OK:
            page.status = Status.WARNING
        page.comment = " | ".join(filter(None, (page.comment, "Alineación no confiable; revisar campos críticos")))
    logger.info(f"[Página {page_number}] Estado: {page.status.value}")

    page.processing_ms = round((time.perf_counter() - t_start) * 1000, 1)
    return page


def _mark_day_not_read(page: PageResult) -> None:
    """Deja las casillas del día diciendo que no se leyeron a propósito."""
    for field in page.fields:
        if field.field_id not in DAY_FIELD_IDS:
            continue
        field.value = None
        field.confidence = 0.0
        field.status = Status.WARNING
        field.comment = DAY_NOT_READ_NOTE
        field.source = "csv_date_policy"
        field.inference_method = "month_end_policy"
    recompute_page_status(page)


def _accept_line_reading(
    field: "FieldTemplate", text: str, confidence: float
) -> bool:
    """Decide si la lectura sin detector de un campo es utilizable.

    Los campos ``ocr_mode='line'`` se leen sin detector porque su recorte ya
    contiene un único valor. Cuando esa lectura rápida no produce un valor
    canónico (o no cumple la regla del campo) se relee con el pipeline
    completo, así que la optimización solo puede ahorrar tiempo y nunca
    perder una lectura que el detector sí habría resuelto.
    """
    if not text.strip():
        return False
    value = text.strip()
    if field.postprocess:
        value, note = apply_postprocess(field.id, field.postprocess, text)
        if not value:
            return False
        if note and not any(soft in note for soft in _SOFT_NOTES):
            return False
    if field.regex and not re.fullmatch(field.regex, value):
        return False
    return True


_CRITICAL_POSTPROCESS = frozenset(
    {"day", "month", "year", "matricula", "digits"}
)
_TIGHT_CROP_POSTPROCESS = _CRITICAL_POSTPROCESS

# Campos de fecha: se leen sin crop_to_ink (ver _critical_region).
_DATE_FIELDS = frozenset({"day", "month", "year"})

# Celdas de carácter de la banda de fecha (una por posición de casilla).
_CHAR_COMPONENTS = (("day", 2), ("month", 3), ("year", 2))

# Las tres casillas que se saltan cuando la ejecución va a fin de mes. La
# casilla grande y sus dos celdas de carácter: son las únicas cuyo valor no
# llega a ninguna salida cuando el día se representa como el último del mes.
DAY_FIELD_IDS = frozenset({"day", "day_1", "day_2"})

# Lo que se anota en su lugar. Dice que no se leyó por decisión y no que no
# se pudo leer: son cosas distintas para quien revisa el CSV, y la segunda
# haría buscar un problema donde no lo hay.
DAY_NOT_READ_NOTE = (
    "Día no leído: la ejecución se configuró a fin de mes"
)


def _date_band_rect(
    template: Template,
    overrides: dict[str, "FieldTemplate"],
    margin_x: float = 0.012,
    margin_y: float = 0.012,
) -> Optional[Tuple[float, float, float, float]]:
    """Unión compacta de campos de fecha, con margen para interpolación."""
    fields = []
    for field in template.fields:
        if field.postprocess not in _DATE_FIELDS and field.postprocess != "char":
            continue
        fields.append(overrides.get(field.id, field))
    if not fields:
        return None
    left = max(0.0, min(field.x for field in fields) - margin_x)
    top = max(0.0, min(field.y for field in fields) - margin_y)
    right = min(1.0, max(field.x + field.w for field in fields) + margin_x)
    bottom = min(1.0, max(field.y + field.h for field in fields) + margin_y)
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def _date_geometries_from_region(
    geometries: dict[str, DateFieldGeometry],
    region: RenderedRegion,
    page_shape: Tuple[int, ...],
) -> dict[str, DateFieldGeometry]:
    """Proyecta una retícula regional al lienzo base alineado."""
    page_height, page_width = page_shape[:2]
    region_height, region_width = region.image.shape[:2]
    region_x, region_y, width_ratio, height_ratio = region.rect

    def global_x(local_x: int) -> int:
        normalized = region_x + local_x / max(region_width, 1) * width_ratio
        return int(round(normalized * page_width))

    def global_y(local_y: int) -> int:
        normalized = region_y + local_y / max(region_height, 1) * height_ratio
        return int(round(normalized * page_height))

    projected: dict[str, DateFieldGeometry] = {}
    for field_id, geometry in geometries.items():
        left, top, right, bottom = geometry.rect
        projected[field_id] = DateFieldGeometry(
            field_id=field_id,
            rect=(
                global_x(left), global_y(top),
                global_x(right), global_y(bottom),
            ),
            separators=[global_x(value) for value in geometry.separators],
            score=geometry.score,
        )
    return projected


def _slot_map_for_region(
    spec: dict,
    base_width: int,
    region: RenderedRegion,
) -> dict:
    """Convierte límites X del lienzo base al recorte regional de alta DPI."""
    return {
        "boundaries": [
            region.local_x(float(x) / max(base_width, 1))
            for x in spec["boundaries"]
        ],
        "slots": spec["slots"],
    }


def _date_cell_overrides(
    template: Template,
    geometries: dict[str, DateFieldGeometry],
    image_shape: Tuple[int, ...],
) -> dict[str, "FieldTemplate"]:
    """Deriva los siete recortes de carácter desde la retícula detectada."""
    height, width = image_shape[:2]
    result: dict[str, "FieldTemplate"] = {}
    for component, count in _CHAR_COMPONENTS:
        geometry = geometries.get(component)
        if geometry is None:
            continue
        boundaries = geometry.slot_spec()["boundaries"]
        if len(boundaries) != count + 1:
            continue
        _left, top, _right, bottom = geometry.rect
        for index, (left, right) in enumerate(
            zip(boundaries, boundaries[1:]), start=1
        ):
            cell = template.field(f"{component}_{index}")
            if cell is None or right <= left:
                continue
            result[cell.id] = cell.model_copy(update={
                "x": left / width,
                "y": top / height,
                "w": (right - left) / width,
                "h": (bottom - top) / height,
            })
    return result
def _accept_slot_candidate(
    rule: Optional[str],
    current_text: str,
    current_confidence: float,
    candidate_raw: str,
    candidate_confidence: float,
) -> bool:
    """Arbitra a favor de una lectura posicional suficientemente legible.

    La fecha siempre es manuscrita y cada carácter tiene una ranura física.
    Por eso la confianza del OCR global no debe bloquear un valor completo
    leído en las posiciones correctas: las confianzas de ambos recortes no
    son comparables. Los umbrales solo impiden que una ranura casi vacía
    reemplace una lectura válida.
    """
    if not candidate_raw:
        return False
    minimum = 0.35 if current_text and candidate_raw != current_text else 0.25
    if candidate_confidence < minimum:
        return False
    if not current_text:
        return True
    if candidate_raw == current_text:
        return True
    if rule in _DATE_FIELDS:
        return True
    return candidate_confidence >= current_confidence


def _critical_region(
    field: "FieldTemplate",
    image: np.ndarray,
    crop_padding: float,
    preprocess: bool = True,
    dpi: Optional[int] = None,
) -> Optional[np.ndarray]:
    """Recorte de un campo crítico, localizado por tinta y escalado."""
    try:
        field_padding = (
            0.0 if field.postprocess in _TIGHT_CROP_POSTPROCESS
            else crop_padding
        )
        region = crop_region(image, field, pad=field_padding)
    except ValueError:
        return None
    if preprocess:
        if field.postprocess in _DATE_FIELDS:
            region = strip_date_label(region)
        elif field.localize == "ink":
            localized = crop_to_ink(region, dpi=dpi)
            if localized is not None:
                region = localized
        region = upscale_for_ocr(region, min_side=600)
    return region


def _slot_ocr_fallback(
    field: "FieldTemplate",
    image: np.ndarray,
    crop_padding: float,
    slot_spec: dict,
    preprocess: bool = True,
    dpi: Optional[int] = None,
    engine: Optional[OcrEngine] = None,
) -> Optional[Tuple[str, float]]:
    """Segunda pasada: OCR por carácter sobre las ranuras de la casilla.

    La casilla lleva separadores verticales impresos entre caracteres;
    con el mapa de ranuras (posiciones fijas derivadas del fondo impreso)
    se recorta cada celda y se lee por separado, decodificando con
    restricciones (dígitos o abreviatura de mes). ``preprocess=False`` lee
    las ranuras crudas. ``engine`` es el motor OCR de la ejecución; las
    restricciones por regla las aplica ``read_date_slots``.
    """
    slots = crop_slots(image, field, 0.0, slot_spec)
    if not slots:
        return None
    if preprocess:
        slots = [localize_slot(slot, dpi=dpi) for slot in slots]
    return read_date_slots(field.id, field.postprocess, slots,
                           preprocess=preprocess, engine=engine)


def _join_char_fields(page: PageResult) -> None:
    """Reconstruye day/month/year a partir de las celdas de carácter.

    Cada casilla de la banda de fecha (day_1..year_2) está separada de
    sus vecinas por una barra vertical impresa; el OCR del campo
    completo puede perder caracteres o fusionar la barra con el trazo.
    Las celdas de carácter de la plantilla no sufren por la barra y
    localizan la tinta dentro de su propia casilla (localize='ink').

    Si las celdas unidas forman un valor canónico (aplicando el
    postprocesador del componente), la evidencia posicional reemplaza el
    OCR global incluso cuando este último reportó confianza alta.
    """
    by_id = {result.field_id: result for result in page.fields}
    for component, count in _CHAR_COMPONENTS:
        target = by_id.get(component)
        if target is None:
            continue
        cells: List[FieldResult] = []
        for index in range(1, count + 1):
            cell = by_id.get(f"{component}_{index}")
            if cell is None:
                cells.clear()
                break
            cells.append(cell)
        if not cells:
            continue
        if component == "month":
            numeric_month = _numeric_month_cells(cells)
            if numeric_month and not re.search(r"[A-Za-z]", target.raw_value or ""):
                _join_numeric_month(target, cells, numeric_month)
            else:
                resolve_month_cells(target, cells)
            continue
        numeric_ranked: List[Tuple[float, str]] = []
        per_cell = [
            _numeric_cell_candidates(cell, component, index)
            for index, cell in enumerate(cells)
        ]
        if all(per_cell):
            numeric_ranked = sorted(
                (
                    (round((left_score + right_score) / 2.0, 3), left + right)
                    for left, left_score in per_cell[0].items()
                    for right, right_score in per_cell[1].items()
                    if component == "year" or 1 <= int(left + right) <= 31
                ),
                reverse=True,
            )
        aligned = [
            max(options, key=options.get) if options else ""
            for options in per_cell
        ]

        # Evidencia estructurada completa. Para día también se acepta un solo
        # dígito únicamente si está en la segunda ranura (alineado a la
        # derecha); un dígito solo en la primera indica normalmente que el
        # segundo se perdió durante el OCR.
        if component == "day":
            if all(aligned):
                raw = "".join(aligned)
            elif (
                not (cells[0].raw_value or cells[0].value)
                and not aligned[0]
                and aligned[1]
            ):
                raw = aligned[1]
            else:
                raw = ""
        elif not all(aligned):
            raw = ""
        else:
            raw = "".join(aligned)
        if numeric_ranked:
            structured_confidence, raw = numeric_ranked[0]
            for _score, candidate_value in numeric_ranked[1:]:
                if (
                    candidate_value != raw
                    and candidate_value not in target.alternatives
                ):
                    target.alternatives.append(candidate_value)
        if not raw:
            if not target.value and any(cell.raw_value or cell.value for cell in cells):
                target.value = None
                target.status = Status.ERROR
                target.comment = "Incomplete or invalid handwritten date cells"
            continue
        candidate, note = apply_postprocess(component, component, raw)
        if not candidate or note:
            continue
        if not numeric_ranked:
            confidences = [cell.confidence for cell in cells
                           if _date_cell_observation(cell)]
            structured_confidence = (
                round(sum(confidences) / len(confidences), 3)
                if confidences else 0.0
            )
        if structured_confidence < 0.35:
            continue
        # Una lectura completa por posiciones vence una lectura global
        # discrepante. La confianza de los motores no es comparable entre
        # recortes completos y celdas, por lo que no se usa como único árbitro.
        if (
            target.value == candidate
            and target.status is Status.OK
        ):
            continue
        previous = target.value
        conflict_threshold = 0.65
        if (
            previous
            and previous != candidate
            and target.status is not Status.ERROR
            and structured_confidence < conflict_threshold
        ):
            if candidate not in target.alternatives:
                target.alternatives.append(candidate)
            if target.confidence < 0.5:
                if previous not in target.alternatives:
                    target.alternatives.append(previous)
                target.value = None
                target.status = Status.ERROR
                target.comment = "Conflicting weak handwritten date readings"
            continue
        if previous and previous != candidate and previous not in target.alternatives:
            target.alternatives.append(previous)
        target.value = candidate
        target.raw_value = raw
        target.confidence = structured_confidence
        target.status = Status.OK
        target.comment = f"unido de celdas: {raw!r}"
        target.source = "date_cells"
        target.inference_method = "date_cells"
        logger.info(
            f"[Página {page.page_number}] {component}: "
            f"reconstruido por celdas de carácter ({raw!r})"
        )

    # Un día de un dígito leído en el campo completo es ambiguo: puede ser un
    # día real o el primer carácter de un día de dos cifras. Solo se conserva
    # si proviene de ranuras (posición física conocida) o de celdas validadas.
    day = by_id.get("day")
    if (
        day is not None
        and re.fullmatch(r"\d", day.value or "")
        and day.inference_method not in {"ranuras", "date_cells"}
    ):
        ambiguous = day.value
        if ambiguous not in day.alternatives:
            day.alternatives.append(ambiguous)
        day.value = None
        day.status = Status.ERROR
        day.comment = "Ambiguous single-digit day without positional evidence"


def _numeric_cell(value: Optional[str]) -> str:
    """Normaliza un único carácter manuscrito de una ranura numérica."""
    if not value or len(value) != 1:
        return ""
    if value.isdigit():
        return value
    return _OCR_LETTER_TO_DIGIT_DICT.get(value, "")


def _date_cell_observation(cell: FieldResult) -> str:
    """Conserva la evidencia cruda de una celda aunque no sea ASCII."""
    return (cell.raw_value or cell.value or "").strip()


def _numeric_cell_candidates(
    cell: FieldResult, component: str, position: int,
) -> dict[str, float]:
    """Candidatos de un dígito manuscrito con peso visual.

    PP-OCRv5 es multilingüe y algunos ``2`` manuscritos se decodifican como
    ideogramas simples; ``C/B`` aparecen a menudo para un ``6`` abierto. Se
    conservan ambas interpretaciones cuando son realmente ambiguas, en vez
    de reemplazar globalmente un carácter.
    """
    observed = _date_cell_observation(cell)
    if not observed:
        return {}
    weights: dict[str, float] = {}
    base_confidence = max(0.05, float(cell.confidence))
    ambiguous = {
        "O": (("0", .90),), "Q": (("0", .80),),
        "I": (("1", .85),), "L": (("1", .70),),
        "Z": (("2", .90),), "乙": (("2", .85),), "二": (("2", .80),),
        "S": (("5", .85),),
        "G": (("6", .85),), "C": (("6", .72), ("0", .40)),
        "B": (("8", .82), ("6", .68)),
    }
    for character in observed.upper():
        if character.isascii() and character.isdigit():
            possibilities = ((character, 1.0),)
            # Un cero muy abierto puede ser leído como 4 en el año.
            if component == "year" and position == 1 and character == "4":
                possibilities += (("0", .35),)
            # En estas bitácoras el 9 manuscrito suele quedar abierto y el
            # reconocedor lo confunde con 7. Se conserva como alternativa;
            # nunca vence localmente, solo puede resolverla la secuencia.
            if component == "day" and position == 1 and character == "7":
                possibilities += (("9", .35),)
        else:
            possibilities = ambiguous.get(character, ())
        for digit, visual_weight in possibilities:
            score = base_confidence * visual_weight
            weights[digit] = max(weights.get(digit, 0.0), score)
    return weights


def _join_numeric_month(target: FieldResult, cells: List[FieldResult], raw: str) -> None:
    """Conserva el caso excepcional de un mes escrito solo con dígitos."""
    candidate = str(int(raw))
    global_numeric = re.sub(r"\D", "", target.value or "")
    if global_numeric and int(global_numeric) != int(candidate):
        return
    confidences = [cell.confidence for cell in cells if _date_cell_observation(cell)]
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    if confidence < 0.35:
        return
    previous = target.value
    if previous and previous != candidate and target.status is not Status.ERROR and confidence < 0.55:
        if candidate not in target.alternatives:
            target.alternatives.append(candidate)
        if target.confidence < 0.5:
            if previous not in target.alternatives:
                target.alternatives.append(previous)
            target.value = None
            target.status = Status.ERROR
            target.comment = "Mes numérico en conflicto con la lectura completa"
        return
    if previous and previous != candidate and previous not in target.alternatives:
        target.alternatives.append(previous)
    target.value = candidate
    target.confidence = round(confidence, 3)
    target.status = Status.WARNING
    target.source = "date_cells"
    target.inference_method = "date_cells"
    target.comment = f"{NUMERIC_MONTH_NOTE} confirmado por celdas: {raw!r}"


def _numeric_month_cells(cells: List[FieldResult]) -> str:
    """Mes 1-12 solo si toda tinta observada en las ranuras es numérica."""
    digits: List[str] = []
    for cell in cells:
        observed = cell.raw_value or cell.value or ""
        if not observed:
            continue
        digit = _numeric_cell(cell.value)
        if not digit:
            return ""
        digits.append(digit)
    raw = "".join(digits)
    if not raw or len(raw) > 2:
        return ""
    return raw if 1 <= int(raw) <= 12 else ""


def _combine_date_parts(page: PageResult) -> None:
    """Combina los campos day/month/year de la página en YYYY/MM/dd.

    La fecha normalizada se guarda en ``page.date``; el campo ``year``
    conserva el año corregido de 2-4 dígitos y day/month sus dígitos.
    Si algún parte falta o es inválida, la nota se deja en el campo
    afectado y la validación de ``required`` lo detectará.
    """
    by_id = {result.field_id: result for result in page.fields}
    day = by_id.get("day")
    month = by_id.get("month")
    year = by_id.get("year")
    if not (day and month and year):
        return
    if any(part.status is Status.ERROR for part in (day, month, year)):
        page.date = None
        logger.debug(
            f"[Página {page.page_number}] Fecha no combinada: "
            "hay un componente inválido"
        )
        return
    combined, note = combine_date(day.value, month.value, year.value)
    if note:
        year.comment = note
        page.date = None
        logger.debug(f"[Página {page.page_number}] Fecha: {note}")
    else:
        page.date = combined
        year.comment = ""
        logger.debug(
            f"[Página {page.page_number}] Fecha combinada: {combined}"
        )


class Pipeline:
    """Procesa un PDF completo contra una plantilla."""

    def __init__(
        self,
        config: AppConfig,
        engine: OcrEngine,
        template: Template,
        reference_image: Optional[np.ndarray] = None,
        on_progress: Optional[ProgressCallback] = None,
        workers: int = 1,
        cpu_threads: Optional[int] = None,
        date_engine: Optional[OcrEngine] = None,
        process_pool: Optional[OcrProcessPool] = None,
        reference_page: int = 1,
    ) -> None:
        self.config = config
        self.engine = engine
        self.date_engine = date_engine
        self.template = template
        self.reference_image = reference_image
        self.on_progress = on_progress
        self.workers = max(1, workers)
        self.cpu_threads = cpu_threads
        self.process_pool = process_pool
        self.reference_page = max(1, reference_page)
        self._printed_mask: Optional[np.ndarray] = None
        self._date_slot_map: dict = {}
        # Ángulo de deskew de cada página, medido durante la calibración e
        # indexado desde la primera página del tramo. Vacío mientras no haya
        # calibración: entonces cada página lo vuelve a detectar por su cuenta.
        self._skew_angles: List[float] = []
        self._should_cancel: Optional[Callable[[], bool]] = None
        self.calibration_ms: float = 0.0

    def _is_cancelled(self) -> bool:
        return self._should_cancel is not None and self._should_cancel()

    def process(self, pdf_path: Path,
                page_range: Optional[PageRange] = None,
                should_cancel: Optional[Callable[[], bool]] = None
                ) -> ValidationReport:
        """Procesa el PDF y devuelve el reporte de validación.

        Args:
            pdf_path: Ruta del PDF escaneado.
            page_range: Tramo de páginas *de este PDF* (1-based, inclusivo).
                None procesa el archivo entero. El reparto de un rango
                global del batch entre archivos lo hace
                :func:`app.core.page_range.slice_batch`.
            should_cancel: Callable sin argumentos que devuelve True cuando
                se pidió la cancelación; el pipeline verifica entre páginas
                y devuelve un reporte parcial con ``cancelled=True``.

        Returns:
            ValidationReport con el resumen y los resultados por página.
        """
        pdf_path = Path(pdf_path)
        with PdfPageRenderer(pdf_path) as renderer:
            return self._process_opened(
                pdf_path, renderer, page_range, should_cancel
            )

    def _process_opened(
        self,
        pdf_path: Path,
        renderer: PdfPageRenderer,
        page_range: Optional[PageRange],
        should_cancel: Optional[Callable[[], bool]],
    ) -> ValidationReport:
        """Implementación con un único handle abierto para todo el PDF."""
        self._should_cancel = should_cancel
        t_start = time.perf_counter()
        started_at = time.time()
        logger.info(f"[Pipeline] Inicio: {pdf_path.name} "
                    f"(plantilla {self.template.name}, "
                    f"workers={self.workers}, dpi={self.config.dpi}, "
                    f"date_dpi={self.config.date_dpi})")

        count = renderer.page_count()
        if count == 0:
            raise ValueError(f"El PDF no contiene páginas: {pdf_path}")
        selection = page_range or PageRange()
        first, last = selection.clamped(count)
        if last < first:
            raise ValueError(
                f"El rango {selection.label()} no cubre ninguna página de "
                f"{pdf_path.name} ({count} páginas)"
            )
        total = last - first + 1
        if not selection.is_full:
            logger.info(
                f"[Pipeline] Tramo solicitado: {selection.label()} "
                f"→ {first}-{last} de {count}"
            )

        reference = self.reference_image
        canonical_reference = False
        reference_number = min(first + self.reference_page - 1, last)
        if reference is None:
            reference = load_template_reference(self.template, self.config.dpi)
            canonical_reference = reference is not None
        if reference is None:
            reference = renderer.render_page(reference_number, self.config.dpi)
        logger.info(
            "[Pipeline] Referencia de alineación: "
            + (
                "imagen externa"
                if self.reference_image is not None
                else "formulario canonico impreso"
                if canonical_reference
                else f"página {reference_number}"
            )
        )

        own_transforms, anchors = self._calibrate(
            pdf_path, first, total, reference, renderer=renderer
        )

        t_pages = time.perf_counter()
        if self._is_cancelled():
            pages: List[PageResult] = []
        elif self.workers > 1:
            pages = self._process_parallel(pdf_path, first, total, reference,
                                           anchors, own_transforms)
        else:
            pages = self._process_sequential(pdf_path, first, total, reference,
                                             anchors, own_transforms,
                                             renderer=renderer)
        pages_ms = (time.perf_counter() - t_pages) * 1000

        # Las firmas que quedaron inciertas se contrastan con el resto de
        # la bitácora, que es evidencia que ya está leída. Tras una
        # cancelación no se entra: la revisión renderiza decenas de páginas
        # más (medido: 93 ms cada una a 200 DPI, 3 s solo la muestra) y
        # cancelar tiene que notarse en el acto, no varios segundos después.
        if not self._is_cancelled():
            pages = self._review_signatures(
                pdf_path, pages, reference, anchors, own_transforms,
                renderer=renderer, first_page=first,
            )

        review_pages = 0
        review_ms = 0.0
        if not self._is_cancelled():
            from app.vision.void_mark import revisar_voids

            t_review = time.perf_counter()
            # Con el reparto por páginas, el pool ya terminó con este
            # documento y está libre: la comprobación reparte en él sus hojas.
            review_pages = revisar_voids(
                pdf_path, pages, self.template, renderer,
                self._notify, self._is_cancelled, pool=self.process_pool,
            )
            review_ms = (time.perf_counter() - t_review) * 1000

        self._notify(total, total, "Generando reporte")
        report = ValidationReport(
            pdf_path=str(pdf_path),
            template_name=self.template.name,
            pages=pages,
            cancelled=self._is_cancelled(),
            calibration_ms=self.calibration_ms,
            processing_ms=round((time.perf_counter() - t_start) * 1000, 1),
            started_at=started_at,
            pages_ms=round(pages_ms, 1),
            review_pages=review_pages,
            review_ms=round(review_ms, 1),
        )
        report.summary = self._compute_summary(pages)

        elapsed = time.perf_counter() - t_start
        logger.info(
            f"[Pipeline] Fin en {elapsed:.1f}s "
            f"(calibración {self.calibration_ms / 1000:.1f}s + "
            f"procesado {(elapsed * 1000 - self.calibration_ms) / 1000:.1f}s) | "
            f"{report.summary}"
            + (" | CANCELADO" if report.cancelled else "")
        )
        return report

    # ── Ejecución (secuencial / paralela) ────────────────────────────────

    def _process_sequential(
            self, pdf_path: Path, first: int, total: int,
            reference: np.ndarray,
            anchors: Optional[List[TransformResult]] = None,
            own: Optional[List[TransformResult]] = None,
            renderer: Optional[PdfPageRenderer] = None,
        ) -> List[PageResult]:
        pages: List[PageResult] = []
        for index in range(total):
            if self._is_cancelled():
                break
            page_number = first + index
            # ``index`` son las páginas ya terminadas de este tramo; el
            # contador que ve el usuario lo compone la pantalla con las
            # cifras del batch (ver ``app.core.progress``).
            self._notify(index, total, PAGES_STAGE)
            image = (
                renderer.render_page(page_number, self.config.dpi)
                if renderer is not None
                else render_page(pdf_path, page_number, self.config.dpi)
            )
            own_transform = own[index] if own else None
            fallback = anchors[index] if anchors else None
            selected = (
                own_transform
                if own_transform is not None and own_transform.reliable
                else fallback
            )
            pages.append(process_page_image(
                image, page_number, self.config, self.engine,
                self.template, reference,
                transform=selected,
                transform_reliable=(
                    own_transform.reliable if own_transform is not None else None
                ),
                printed_mask=self._printed_mask,
                slot_map=self._date_slot_map,
                date_engine=self.date_engine,
                date_renderer=renderer,
                skew_angle=self._skew_angle_at(index),
            ))
        return pages

    def _skew_angle_at(self, index: int) -> Optional[float]:
        """Ángulo medido en la calibración para la página ``index`` del tramo."""
        if 0 <= index < len(self._skew_angles):
            return self._skew_angles[index]
        return None

    def _process_parallel(
            self, pdf_path: Path, first: int, total: int,
            reference: np.ndarray,
            anchors: Optional[List[TransformResult]] = None,
            own: Optional[List[TransformResult]] = None,
        ) -> List[PageResult]:
        pages: List[PageResult] = []
        effective = [
            measured
            if measured.reliable
            else anchors[index]
            if anchors and index < len(anchors)
            else measured
            for index, measured in enumerate(own or [])
        ]
        last = first + total - 1
        state_path: Optional[Path] = None
        owns_pool = self.process_pool is None
        if self.process_pool is not None:
            state_path = self.process_pool.prepare({
                "config": self.config,
                "template": self.template,
                "reference": reference,
                "pdf_path": Path(pdf_path),
                "transforms": effective,
                "own_reliability": [t.reliable for t in own or []],
                "printed_mask": self._printed_mask,
                "slot_map": dict(self._date_slot_map),
                "skew_angles": list(self._skew_angles),
                "first_page": first,
            })
            pool = self.process_pool.executor
        else:
            pool = ProcessPoolExecutor(
                max_workers=self.workers,
                initializer=_init_worker,
                initargs=(
                    self.config, self.template, self.engine.name,
                    self.config.ocr_lang, self.cpu_threads, reference, pdf_path,
                    effective or None,
                    [t.reliable for t in own] if own else None,
                    self._printed_mask,
                    self._date_slot_map,
                    self.date_engine.name if self.date_engine else None,
                    first,
                    list(self._skew_angles),
                ),
            )
        futures: dict = {}
        cancelled_pending: dict = {}
        try:
            next_page = first
            max_in_flight = max(self.workers, self.workers * 3)

            def submit_available() -> None:
                nonlocal next_page
                while next_page <= last and len(futures) < max_in_flight:
                    future = pool.submit(
                        _process_page_worker,
                        next_page,
                        str(state_path) if state_path is not None else None,
                    )
                    futures[future] = next_page
                    next_page += 1

            submit_available()
            while futures:
                done, _pending = wait(
                    tuple(futures), timeout=0.20,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    page_number = futures.pop(future)
                    page = future.result()
                    pages.append((page_number, page))
                    # Aquí no hay "página en curso" que anunciar: hay tantas
                    # en vuelo como workers y terminan desordenadas, así que
                    # nombrar la última en llegar hacía que el número se
                    # devolviera (52, 48, 53…). Lo que avanza es el número de
                    # páginas terminadas.
                    self._notify(len(pages), total, PAGES_STAGE)
                if self._is_cancelled():
                    cancelled_pending = dict(futures)
                    for future in cancelled_pending:
                        future.cancel()
                    futures.clear()
                    break
                submit_available()
        finally:
            if owns_pool:
                pool.shutdown(
                    wait=True, cancel_futures=self._is_cancelled()
                )
            elif state_path is not None:
                # Los trabajos que ya estaban en vuelo deben soltar el pickle
                # antes de eliminarlo, incluso durante una cancelación.
                for future in (*futures, *cancelled_pending):
                    if not future.cancelled():
                        try:
                            future.result()
                        except Exception:  # noqa: BLE001 - se propaga arriba
                            pass
                self.process_pool.release(state_path)
        if self._is_cancelled():
            # Conserva también las páginas que ya estaban ejecutándose cuando
            # se pulsó cancelar; no se pierde trabajo terminado.
            for future, page_number in cancelled_pending.items():
                if future.cancelled() or not future.done():
                    continue
                try:
                    pages.append((page_number, future.result()))
                except Exception:  # noqa: BLE001 - reporte parcial
                    continue
        pages.sort(key=lambda item: item[0])
        if self._is_cancelled():
            logger.warning(
                f"[Pipeline] Cancelado con {len(pages)}/{total} páginas"
            )
        return [page for _, page in pages]

    # ── Alineación por batch ─────────────────────────────────────────────

    def _calibrate(
        self, pdf_path: Path, first: int, total: int, reference: np.ndarray,
        renderer: Optional[PdfPageRenderer] = None,
    ) -> Tuple[Optional[List[TransformResult]], Optional[List[TransformResult]]]:
        """Paso 1: estima la transformación de cada página a baja resolución.

        Solo renderiza + deskew + matching ORB (sin deformar ni OCR), y
        devuelve (transformaciones propias, anclas estabilizadas), ambas
        indexadas desde ``first``. Con la alineación deshabilitada devuelve
        (None, None).

        Coste: bajo (~75), ≈ 0.2-0.4 s por página, sin OCR ni warp.
        """
        t_calib = time.perf_counter()
        try:
            return self._calibrate_impl(
                pdf_path, first, total, reference, renderer=renderer
            )
        finally:
            self.calibration_ms = round(
                (time.perf_counter() - t_calib) * 1000, 1
            )

    def _calibrate_impl(
        self, pdf_path: Path, first: int, total: int, reference: np.ndarray,
        renderer: Optional[PdfPageRenderer] = None,
    ) -> Tuple[Optional[List[TransformResult]], Optional[List[TransformResult]]]:
        self._printed_mask = None
        self._date_slot_map = {}
        self._skew_angles = []
        if not self.config.align or reference is None:
            return None, None
        last = first + total - 1

        calib_dpi = max(75, self.config.dpi // 2)
        zoom = calib_dpi / self.config.dpi
        calib_ref = cv2.resize(reference, None, fx=zoom, fy=zoom,
                               interpolation=cv2.INTER_AREA)
        factor = self.config.dpi / calib_dpi

        # La máscara de fondo impreso solo se construye si alguien va a
        # leerla: la aplican las casillas (``process_page_image``, y solo
        # cuando la plantilla declara alguna) y el mapa de ranuras de fecha
        # (``build_slot_maps``, solo con ``date_slot_ocr``). Construirla sin
        # consumidor costaba una imagen en grises por página retenida durante
        # toda la calibración (0,89 MB cada una, 0,34 GB en un libro de 393
        # páginas, fuera del presupuesto de memoria por proceso que calcula
        # ``app.core.parallelism``) más un warp y una acumulación por página.
        collect_background = (
            self.config.remove_printed
            and total >= 3
            and self._printed_mask_has_consumer()
        )
        own: List[TransformResult] = []
        # Las páginas en grises esperando a que su ancla quede fija. El ancla
        # de la página i es la mediana de la ventana [i-7, i+7], así que queda
        # decidida en cuanto se ha calibrado la página i+7 y no hace falta
        # retener el libro entero: como mucho hay ``half_window + 1`` páginas
        # aquí dentro (~7 MB), sea el libro de 50 páginas o de 393.
        half_window = 7
        pending_gray: dict[int, np.ndarray] = {}
        accum: Optional[np.ndarray] = None
        aligned_count = 0

        def fold_background(index: int) -> None:
            """Acumula una página ya alineada en el consenso del fondo."""
            nonlocal accum, aligned_count
            gray = pending_gray.pop(index, None)
            if gray is None:
                return
            measured = own[index]
            transform = (
                measured
                if measured.reliable
                else self._anchor_at(own, index, half_window)
            )
            if not transform.reliable:
                return
            calib_tr = TransformResult(
                rot=transform.rot, scale=transform.scale,
                tx=transform.tx / factor, ty=transform.ty / factor,
            )
            warped = warp_with_transform(
                gray, calib_tr, (calib_ref.shape[1], calib_ref.shape[0])
            )
            dark = warped < self.config.printed_ink_threshold
            if accum is None:
                accum = np.zeros_like(dark, dtype=np.float32)
            accum += dark.astype(np.float32)
            aligned_count += 1

        # Con el pool ya creado la calibración se reparte entre sus procesos,
        # que si no estarían parados esperando a que termine. No se reparte
        # cuando hay que construir el fondo impreso: ese consenso necesita la
        # página en grises, y devolverla desde cada worker cambiaría un buffer
        # acotado por un trasiego de imágenes entre procesos.
        pooled = (
            self.process_pool is not None
            and self.workers > 1
            and not collect_background
        )
        measured: Optional[List[Tuple[TransformResult, Optional[float]]]] = None
        if pooled:
            try:
                measured = self._pooled_calibration(
                    pdf_path, first, last, total, calib_dpi, calib_ref, factor
                )
            except Exception as exc:  # noqa: BLE001 - se cae a la ruta serie
                logger.warning(
                    f"[Pipeline] La calibración en paralelo falló ({exc}); "
                    f"se calibra en serie"
                )
                measured = None
                self._skew_angles = []

        if measured is not None:
            for tr, angle in measured:
                if angle is not None:
                    self._skew_angles.append(angle)
                own.append(tr)
        else:
            for page_number in range(first, last + 1):
                if self._is_cancelled():
                    break
                # Progreso como etapa, sin contar páginas: las páginas reales
                # se cuentan solo en el procesamiento (done=0 no avanza).
                self._notify(
                    0, total,
                    f"Calibrando alineación (página {page_number}/{last})",
                )
                image = (
                    renderer.render_page(page_number, calib_dpi)
                    if renderer is not None
                    else render_page(pdf_path, page_number, calib_dpi)
                )
                if self.config.deskew:
                    # El ángulo se guarda: la fase de OCR lo aplica en vez de
                    # volver a buscarlo sobre la página a DPI completo, que es
                    # la parte cara (Canny + Hough, 103-139 ms frente a los
                    # 62 ms de aquí). El ángulo no depende de la escala.
                    image, angle = deskew(image)
                    self._skew_angles.append(angle)
                tr = compute_similarity_transform(image, calib_ref, self.config)
                tr.tx *= factor
                tr.ty *= factor
                own.append(tr)

                # La página en grises espera aquí a que su ancla quede fija; la
                # máscara usa el mismo marco estabilizado que la fase de OCR.
                # Un canal conserva toda la evidencia que la máscara necesita.
                if collect_background:
                    pending_gray[len(own) - 1] = cv2.cvtColor(
                        image, cv2.COLOR_BGR2GRAY
                    )
                    settled = len(own) - half_window - 1
                    if settled >= 0:
                        fold_background(settled)

        # Las últimas páginas de la ventana, ya con la lista completa.
        for index in sorted(pending_gray):
            fold_background(index)

        anchors = self._stabilize_anchors(own)
        reliable = sum(1 for t in own if t.reliable)
        logger.info(f"[Pipeline] Calibración: {reliable}/{total} páginas "
                    f"fiables; anclas estabilizadas por ventana")

        if accum is not None and aligned_count:
            printed = (accum / aligned_count) >= 0.60
            printed = cv2.dilate(
                printed.astype(np.uint8), np.ones((3, 3), np.uint8),
            )
            self._printed_mask = cv2.resize(
                printed, (reference.shape[1], reference.shape[0]),
                interpolation=cv2.INTER_NEAREST,
            ).astype(bool)
            logger.info(
                f"[Pipeline] Fondo impreso: {int(self._printed_mask.sum())}"
                " px eliminados del OCR"
            )
        if self.config.date_slot_ocr:
            slot_mask = self._printed_mask
            if slot_mask is None:
                # Sin consenso de fondo impreso se usa la division uniforme
                # de la plantilla para no desactivar silenciosamente el
                # fallback por ranuras.
                slot_mask = np.zeros(reference.shape[:2], dtype=bool)
            self._date_slot_map = build_slot_maps(slot_mask, self.template)
            logger.info(
                f"[Pipeline] Ranuras de casillas de fecha: "
                f"{sorted(self._date_slot_map)}"
            )
        return own, anchors

    def _pooled_calibration(
        self,
        pdf_path: Path,
        first: int,
        last: int,
        total: int,
        calib_dpi: int,
        calib_ref: np.ndarray,
        factor: float,
    ) -> List[Tuple[TransformResult, Optional[float]]]:
        """Calibra el tramo repartiendo las páginas entre el pool OCR.

        La calibración corría en el proceso principal, página a página,
        mientras los procesos del pool (ya arrancados, con los modelos
        cargados) esperaban sin hacer nada. Medido: 202 ms por página
        (render 70 + deskew 41 + ORB 90), que en un libro de 50 páginas son
        10,1 s de un reloj de unos 65.

        Los resultados se consumen en orden de página, así que las anclas y
        los ángulos salen exactamente igual que en la ruta en serie. Si se
        cancela, devuelve lo calibrado hasta ese punto.
        """
        pool = self.process_pool
        assert pool is not None  # lo garantiza el llamador
        state_path = pool.prepare({
            "pdf_path": Path(pdf_path),
            "config": self.config,
            "calib_dpi": calib_dpi,
            "calib_ref": calib_ref,
            "factor": factor,
        })
        measured: List[Tuple[TransformResult, Optional[float]]] = []
        futures = []
        try:
            for page_number in range(first, last + 1):
                futures.append(pool.executor.submit(
                    _calibrate_page_worker, page_number, str(state_path)
                ))
            for offset, future in enumerate(futures):
                if self._is_cancelled():
                    for remaining in futures[offset:]:
                        remaining.cancel()
                    break
                measured.append(future.result())
                self._notify(
                    0, total,
                    f"Calibrando alineación "
                    f"(página {first + offset}/{last})",
                )
        finally:
            # Ningún worker puede seguir leyendo el pickle cuando se borra.
            for future in futures:
                if not future.cancelled():
                    try:
                        future.result()
                    except Exception:  # noqa: BLE001 - ya se propagó arriba
                        pass
            pool.release(state_path)
        return measured

    @staticmethod
    def _stabilize_anchors(
        transforms: List[TransformResult], half_window: int = 7
    ) -> List[TransformResult]:
        """Ancla por página = mediana de la ventana [i-half, i+half].

        La mediana es robusta a páginas catastróficas (ORB degradado) y
        deja la posición de los campos consistente entre bitácoras de un
        mismo batch. Si en la ventana no hay páginas fiables, toma la
        ventana completa como respaldo.
        """
        return [
            Pipeline._anchor_at(transforms, index, half_window)
            for index in range(len(transforms))
        ]

    @staticmethod
    def _anchor_at(
        transforms: List[TransformResult], index: int, half_window: int = 7
    ) -> TransformResult:
        """Ancla de una sola página, con la misma regla que el batch entero.

        Separarla permite decidir el ancla de una página en cuanto se ha
        calibrado la última de su ventana, sin esperar al libro completo: es
        lo que deja acotado el buffer del consenso de fondo impreso.
        """
        lo = max(0, index - half_window)
        hi = min(len(transforms), index + half_window + 1)
        window = transforms[lo:hi]
        reliable_pool = [t for t in window if t.reliable]
        pool = reliable_pool or window
        return TransformResult(
            rot=float(np.median([t.rot for t in pool])),
            tx=float(np.median([t.tx for t in pool])),
            ty=float(np.median([t.ty for t in pool])),
            scale=float(np.median([t.scale for t in pool])),
            inliers=max((t.inliers for t in pool), default=0),
            reliable=bool(reliable_pool),
        )

    def _printed_mask_has_consumer(self) -> bool:
        """¿Alguien va a leer la máscara de fondo impreso en esta ejecución?

        Sus dos consumidores son las casillas (que solo la aplican si la
        plantilla declara alguna) y el mapa de ranuras de fecha, que depende
        de ``date_slot_ocr``. Sin ninguno de los dos, construirla es trabajo
        y memoria que nadie mira.
        """
        if self.config.date_slot_ocr:
            return True
        return any(
            field.type is FieldType.CHECKBOX for field in self.template.fields
        )

    # ── Pasos internos ──────────────────────────────────────────────────

    def _notify(self, done: int, total: int, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(done, total, message)

    @staticmethod
    def _compute_summary(pages: List[PageResult]) -> dict:
        summary = {
            "total_pages": len(pages),
            "ok_pages": 0,
            "warning_pages": 0,
            "error_pages": 0,
            "blank_pages": sum(1 for p in pages if p.blank),
        }
        for page in pages:
            if page.status is Status.OK:
                summary["ok_pages"] += 1
            elif page.status is Status.WARNING:
                summary["warning_pages"] += 1
            else:
                summary["error_pages"] += 1
        return summary

    def _review_signatures(
        self,
        pdf_path: Path,
        pages: List[PageResult],
        reference: Optional[np.ndarray],
        anchors: Optional[List[TransformResult]],
        own: Optional[List[TransformResult]],
        renderer: Optional[PdfPageRenderer] = None,
        first_page: int = 1,
    ) -> List[PageResult]:
        """Resuelve firmas inciertas comparándolas con el resto del libro.

        Una bitácora son decenas de páginas del mismo formulario ya alineadas,
        así que la mediana de un campo a lo largo del libro es ese campo
        *vacío*, con su recuadro, su rótulo y el gris de la fotocopia. Restarla
        deja solo lo escrito en cada página, y las páginas que el detector sí
        resolvió dicen cuánta tinta significa "firmado" en este libro concreto
        (ver ``app/vision/book_background.py``).

        Solo se tocan los campos que quedaron en ``unclear``: los veredictos
        firmes no se revisan. Y solo se renderizan las páginas necesarias (una
        muestra para el fondo, más las dudosas), no el libro entero.

        Quedan fuera los campos con ``book_background`` en false. La segunda
        opinión compara densidades de tinta, y hay casillas que no se deciden
        por densidad: el bloque de corrección es mucho más ancho que alto y lo
        que lo distingue del papel rayado es la tinta repartida a lo largo de
        la línea. Un sello desbordado desde la fila de arriba concentra tanta
        tinta como una corrección escrita, así que contrastarlo por densidad
        lo daba por escrito y abría una discrepancia que no existe.

        Cualquier fallo aquí deja los resultados como estaban: es una segunda
        opinión, no un eslabón del que dependa la ejecución.
        """
        if not self.config.signature_book_background:
            return pages
        fields = [field for field in self.template.fields
                  if field.type is FieldType.SIGNATURE and field.book_background]
        if not fields:
            return pages
        revisables = {field.id for field in fields}
        pending = [
            (page, result)
            for page in pages
            for result in page.fields
            if result.value == UNCLEAR
            and result.field_type == FieldType.SIGNATURE.value
            and result.field_id in revisables
            and page.alignment_quality == "ok"
        ]
        if not pending:
            return pages
        try:
            return self._review_signatures_impl(
                pdf_path, pages, pending, fields, reference, anchors, own,
                renderer, first_page,
            )
        except Exception as exc:  # noqa: BLE001 - nunca debe tumbar la ejecución
            logger.warning(
                f"[Pipeline] No se pudo contrastar las firmas inciertas con "
                f"el libro: {exc}"
            )
            return pages

    def _review_signatures_impl(
        self,
        pdf_path: Path,
        pages: List[PageResult],
        pending: List[Tuple[PageResult, FieldResult]],
        fields: List[FieldTemplate],
        reference: Optional[np.ndarray],
        anchors: Optional[List[TransformResult]],
        own: Optional[List[TransformResult]],
        renderer: Optional[PdfPageRenderer],
        first_page: int,
    ) -> List[PageResult]:
        # Las páginas mal alineadas no sirven ni de fondo ni de referencia: sus
        # recortes no caen sobre la misma casilla que los demás.
        usable = [page for page in pages
                  if not page.blank and page.alignment_quality == "ok"]
        if len(usable) < MIN_BACKGROUND_PAGES:
            logger.info(
                f"[Pipeline] Firmas inciertas: {len(usable)} páginas alineadas, "
                f"hacen falta {MIN_BACKGROUND_PAGES} para el fondo del libro"
            )
            return pages

        # Muestra repartida por todo el libro para el fondo y para aprender
        # qué densidad significa "firmado" aquí, más las páginas a resolver.
        # El reparto va de la primera página a la última: tomar un paso fijo y
        # cortar al llegar al cupo dejaría fuera el final del libro, que es
        # justo donde el escaneo suele degradarse.
        wanted_count = min(len(usable), self.config.signature_background_pages)
        sample = [
            usable[round(index * (len(usable) - 1) / max(1, wanted_count - 1))]
            for index in range(wanted_count)
        ]
        wanted = {page.page_number for page in sample}
        wanted.update(page.page_number for page, _ in pending)

        if self.config.dpi < BACKGROUND_REFERENCE_DPI:
            # El recorte se reescala a la escala de calibración, pero
            # interpolar no devuelve el detalle que no se renderizó: los
            # trazos flojos pierden profundidad y se resuelven menos dudas.
            # No es un error (sigue sin equivocarse), pero conviene saberlo.
            logger.info(
                f"[Pipeline] Firmas inciertas: la ejecución va a "
                f"{self.config.dpi} DPI y la revisión está calibrada a "
                f"{BACKGROUND_REFERENCE_DPI}; se resolverán menos dudas"
            )
        self._notify(len(pages), len(pages),
                     "Contrastando firmas inciertas con el libro")
        # El ángulo que el procesado ya midió para cada página: el recorte que
        # se compara con el fondo del libro debe salir del mismo enderezado que
        # vio el detector, no de una segunda medición.
        by_number = {page.page_number: page for page in pages}
        crops: Dict[int, Dict[str, np.ndarray]] = {}
        for page_number in sorted(wanted):
            if self._is_cancelled():
                # La revisión es una segunda opinión: si se cancela a mitad,
                # las páginas se devuelven tal como las dejó el detector.
                logger.info(
                    "[Pipeline] Revisión de firmas interrumpida al cancelar"
                )
                return pages
            image = (
                renderer.render_page(page_number, self.config.dpi)
                if renderer is not None
                else render_page(pdf_path, page_number, self.config.dpi)
            )
            reviewed = by_number.get(page_number)
            image = self._aligned_image(
                image, page_number - first_page, reference,
                anchors=anchors, own=own,
                skew_angle=reviewed.skew_angle if reviewed else None,
            )
            page_crops: Dict[str, np.ndarray] = {}
            for field in fields:
                try:
                    page_crops[field.id] = crop_region(
                        image, field, pad_x=0.0, pad_y=0.0
                    )
                except ValueError:
                    continue
            crops[page_number] = page_crops

        verdicts = {
            (page.page_number, result.field_id): result.value
            for page in pages for result in page.fields
        }
        resolved = 0
        for field in fields:
            sample_crops = [
                crops[page.page_number][field.id] for page in sample
                if field.id in crops.get(page.page_number, {})
            ]
            background = build_background(sample_crops, self.config.dpi)
            if background is None:
                continue
            sample_peaks, sample_verdicts = [], []
            for page in sample:
                crop = crops.get(page.page_number, {}).get(field.id)
                if crop is None:
                    continue
                sample_peaks.append(background_peak(crop, background))
                sample_verdicts.append(
                    verdicts.get((page.page_number, field.id), UNCLEAR)
                )
            band = confident_band(sample_peaks, sample_verdicts)
            if band is None:
                logger.info(
                    f"[Pipeline] Firmas de '{field.id}': la bitácora no separa "
                    f"firmadas de vacías con claridad; se dejan inciertas"
                )
                continue
            for page, result in pending:
                if result.field_id != field.id:
                    continue
                crop = crops.get(page.page_number, {}).get(field.id)
                if crop is None:
                    continue
                peak = background_peak(crop, background)
                opinion = review_with_background(peak, band)
                if opinion is None:
                    logger.info(
                        f"[Pipeline] Firma incierta en página "
                        f"{page.page_number}/{field.id}: densidad {peak:.4f} "
                        f"cae dentro de la franja de duda del libro "
                        f"[{band[0]:.4f}, {band[1]:.4f}]; sigue incierta"
                    )
                    continue
                value, confidence, comment = opinion
                result.value = value
                result.confidence = round(confidence, 3)
                result.status = (
                    Status.OK if value == "true"
                    else Status.ERROR if field.required
                    else Status.WARNING
                )
                result.source = "vision"
                result.inference_method = "book_background"
                result.comment = comment
                resolved += 1
        logger.info(
            f"[Pipeline] Firmas inciertas contrastadas con el libro: "
            f"{resolved}/{len(pending)} resueltas"
        )
        return pages

    def _aligned_image(
        self,
        image: np.ndarray,
        index: int,
        reference: Optional[np.ndarray],
        anchors: Optional[List[TransformResult]] = None,
        own: Optional[List[TransformResult]] = None,
        skew_angle: Optional[float] = None,
    ) -> np.ndarray:
        """Reproduce el deskew y la alineación del procesado.

        Sin la máscara de fondo impreso: ni el OCR ni las firmas la usan, y
        aplicarla aquí dejaría un recorte con huecos blancos dentro de la
        escritura que el detector nunca vio.

        ``skew_angle`` es el ángulo que el procesado ya dejó en
        ``PageResult.skew_angle``. Reutilizarlo no solo ahorra la detección
        (103-139 ms por página, sobre decenas de páginas y en serie): también
        garantiza que el recorte que ve el fondo del libro sea el mismo que
        vio el detector, en vez de uno enderezado por una segunda medición
        que puede diferir de la primera.
        """
        if self.config.deskew:
            if skew_angle is None:
                image, _skew = deskew(image)
            elif abs(float(skew_angle)) > 0.0:
                image = rotate(image, float(skew_angle))
        if self.config.align and reference is not None:
            transform = None
            if own and 0 <= index < len(own) and own[index].reliable:
                transform = own[index]
            elif anchors and 0 <= index < len(anchors) and anchors[index].reliable:
                transform = anchors[index]
            else:
                measured = compute_similarity_transform(
                    image, reference, self.config
                )
                if measured.reliable:
                    transform = measured
            if transform is not None:
                image = warp_with_transform(
                    image, transform, (reference.shape[1], reference.shape[0])
                )
        return image



def _process_pdf_worker(
    pdf_path: Path,
    base_config: AppConfig,
    template: Template,
    page_range: Optional[PageRange],
    reference_page: int,
    cancel_path: str,
    progress_path: Optional[str] = None,
) -> ValidationReport:
    """Procesa un PDF completo dentro de un worker OCR persistente."""
    # Un estado de página del perfil B puede haber dejado un documento abierto
    # en este proceso. El Pipeline del archivo mantiene su propio handle único.
    previous_renderer = _WORKER_STATE.pop("renderer", None)
    if previous_renderer is not None:
        previous_renderer.close()
    file_config = config_for_pdf(base_config, Path(pdf_path))
    pipeline = Pipeline(
        file_config,
        _WORKER_STATE["engine"],
        template,
        workers=1,
        cpu_threads=None,
        date_engine=_WORKER_STATE.get("date_engine"),
        reference_page=reference_page,
    )
    if progress_path is not None:
        pipeline.on_progress = _page_counter_writer(Path(progress_path))
    cancellation_flag = Path(cancel_path)
    report = pipeline.process(
        Path(pdf_path),
        page_range=page_range,
        should_cancel=cancellation_flag.exists,
    )
    return report


def _page_counter_writer(path: Path) -> ProgressCallback:
    """Publica el avance del worker en un archivo que el padre puede leer.

    Un proceso del pool no puede emitir señales a la GUI, así que deja su
    contador de páginas en el directorio temporal del pool (el mismo sitio
    donde ya vive la bandera de cancelación) y el planificador lo consulta
    mientras espera. Es una línea de texto por página: más barato que una
    cola compartida y sin proceso extra de ``Manager``.

    La comprobación de VOID va detrás de un ``;`` (``50/50;3/40``): son hojas
    de esa etapa, y escribirlas en el lugar de las páginas hacía que el
    archivo pareciera volver a empezar con menos páginas.
    """
    pages = [0, 0]
    review: List[int] = []

    def write(done: int, total: int, message: str) -> None:
        if message == VOID_STAGE:
            review[:] = [done, total]
        else:
            pages[:] = [done, total]
        text = f"{pages[0]}/{pages[1]}"
        if review:
            text += f";{review[0]}/{review[1]}"
        try:
            path.write_text(text, encoding="ascii")
        except OSError:
            # Un contador que no se puede escribir no puede tumbar el OCR.
            pass

    return write


def _read_page_counter(path: Optional[Path]) -> Optional[Tuple[int, int]]:
    """Lee el contador de un worker; ``None`` si aún no hay dato utilizable."""
    if path is None:
        return None
    try:
        pages, _, _review = path.read_text(encoding="ascii").partition(";")
        done, _, total = pages.partition("/")
        return int(done), int(total)
    except (OSError, ValueError):
        # El padre puede leer justo mientras el worker reescribe el archivo.
        return None


def _read_review_counter(path: Optional[Path]) -> Optional[Tuple[int, int]]:
    """Hojas de la comprobación de VOID de un worker; ``None`` si no empezó."""
    if path is None:
        return None
    try:
        _pages, _, review = path.read_text(encoding="ascii").partition(";")
        if not review:
            return None
        done, _, total = review.partition("/")
        return int(done), int(total)
    except (OSError, ValueError):
        return None


def process_pdf_batch(
    pdf_paths: List[Path],
    base_config: AppConfig,
    template: Template,
    process_pool: OcrProcessPool,
    fallback_engine: OcrEngine,
    date_engine: Optional[OcrEngine] = None,
    page_range: Optional[PageRange] = None,
    reference_page: int = 1,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_file_started: Optional[Callable[[int, int, str], None]] = None,
    on_file_finished: Optional[Callable[[int, ValidationReport], None]] = None,
    on_progress: Optional[ProgressCallback] = None,
    on_file_progress: Optional[Callable[[int, int, int], None]] = None,
    on_review_progress: Optional[Callable[[int, int, int], None]] = None,
) -> List[ValidationReport]:
    """Procesa un batch con el perfil C siempre activo y cola acotada.

    ``on_review_progress`` recibe ``(archivo 1-based, hojas revisadas, hojas
    por revisar)`` de la comprobación de VOID, que no se cuenta como páginas.

    El planificador elige la granularidad sin cambiar el algoritmo OCR: reparte
    PDFs completos cuando hay suficientes archivos para ocupar el pool y, en
    batches menores, reparte las páginas para no dejar procesos ociosos. En ambos
    casos el pool OCR persiste y las colas permanecen acotadas.

    ``on_file_progress`` recibe ``(archivo 1-based, páginas hechas, páginas
    del archivo)`` en las dos estrategias, para que el panel de avance se lea
    igual reparta el planificador páginas o archivos completos.

    ``page_range`` es un rango del batch numerado de corrido: se reparte entre
    los archivos que lo contienen y los que quedan fuera ni se abren, así que
    los reportes devueltos cubren solo esos archivos.
    """
    paths = [Path(path) for path in pdf_paths]
    if not paths:
        return []

    document_pages: List[int] = []
    for path in paths:
        with PdfPageRenderer(path) as renderer:
            document_pages.append(renderer.page_count())
    slices = slice_batch(paths, document_pages, page_range)
    if not slices:
        logger.warning(
            f"[Perfil C] El rango {(page_range or PageRange()).label()} no "
            f"cubre ninguna página de los {len(paths)} archivo(s) del batch"
        )
        return []
    paths = [item.path for item in slices]
    counts = [item.count or 0 for item in slices]
    ranges = [item.pages for item in slices]
    total_pages = sum(counts)
    done_pages = 0
    total_files = len(paths)

    # Perfil C no es un modo reservado a batches grandes. Para pocos archivos,
    # paralelizar páginas utiliza mejor el mismo pool; para muchos, cada worker
    # recibe archivos completos y elimina las barreras entre PDFs.
    parallel_by_file = total_files >= process_pool.max_workers
    strategy = "archivos" if parallel_by_file else "páginas"
    logger.info(
        f"[Perfil C] activo | estrategia={strategy} | "
        f"workers={process_pool.max_workers} | archivos={total_files}"
    )
    # El equipo que ejecuta manda sobre el reparto, y no se parece al de las
    # mediciones: dejar constancia de lo que se detectó permite entender un
    # rendimiento distinto sin tener que reproducirlo.
    logger.info(
        f"[Equipo] {core_topology().describe()} | "
        f"memoria libre {available_memory_mb()} MB de "
        f"{total_memory_mb()} MB | reservados {reserved_memory_mb()} MB"
    )
    # El nombre del perfil es interno (ver README): la barra de estado sigue
    # hablando de páginas y archivos, que es lo que el usuario mira.
    if on_progress is not None:
        on_progress(0, total_pages, f"Procesando {total_files} archivo(s)…")

    if not parallel_by_file:
        sequential_reports: List[ValidationReport] = []
        for index, (path, count) in enumerate(zip(paths, counts)):
            if should_cancel is not None and should_cancel():
                break
            if on_file_started is not None:
                on_file_started(index + 1, total_files, path.name)
            if on_file_progress is not None:
                on_file_progress(index + 1, 0, count)
            file_config = config_for_pdf(base_config, path)
            pipeline = Pipeline(
                file_config,
                fallback_engine,
                template,
                workers=process_pool.max_workers,
                date_engine=date_engine,
                process_pool=process_pool,
                reference_page=reference_page,
            )
            offset = done_pages

            def page_progress(
                done: int,
                total_in_file: int,
                message: str,
                *,
                _offset: int = offset,
                _index: int = index,
                _name: str = path.name,
                _count: int = count,
            ) -> None:
                if message == VOID_STAGE:
                    # Hojas de la segunda vuelta: el archivo ya leyó sus
                    # páginas y su avance no puede volver a empezar.
                    if on_review_progress is not None:
                        on_review_progress(_index + 1, done, total_in_file)
                    if on_progress is not None:
                        on_progress(
                            _offset + _count,
                            total_pages,
                            f"Archivo {_index + 1}/{total_files}: {_name} - "
                            f"{message} {done}/{total_in_file}",
                        )
                    return
                if on_file_progress is not None:
                    on_file_progress(_index + 1, done, total_in_file)
                if on_progress is not None:
                    # El nombre del archivo delante, igual que en la ruta
                    # secuencial: sin él la etapa de un solo PDF y el
                    # contador del batch se leían como si hablaran de lo mismo.
                    on_progress(
                        _offset + done,
                        total_pages,
                        f"Archivo {_index + 1}/{total_files}: {_name} - "
                        f"{message}",
                    )

            pipeline.on_progress = page_progress
            report = pipeline.process(
                path,
                page_range=ranges[index],
                should_cancel=should_cancel,
            )
            sequential_reports.append(report)
            done_pages += len(report.pages)
            if on_file_progress is not None:
                on_file_progress(index + 1, len(report.pages), count)
            if on_file_finished is not None:
                on_file_finished(index + 1, report)
            if report.cancelled:
                break
            # En un PDF sin páginas procesables el callback del Pipeline no
            # puede avanzar por sí mismo; conserva el total global coherente.
            if on_progress is not None and count == 0:
                on_progress(done_pages, total_pages, f"Procesado {path.name}")
        logger.info(
            f"[Perfil C] {len(sequential_reports)}/{total_files} archivos | "
            f"estrategia=páginas | páginas={done_pages}"
        )
        return sequential_reports

    reports: List[Optional[ValidationReport]] = [None] * total_files
    retry: List[Tuple[int, Exception]] = []
    pending: dict = {}
    counters: dict = {}
    submitted: dict = {}
    next_index = 0
    published = 0
    cancelled = False
    cancel_flag = process_pool.temporary_path("cancel_mass_batch.flag")
    cancel_flag.unlink(missing_ok=True)

    def cancellation_requested() -> bool:
        return should_cancel is not None and should_cancel()

    def submit_available() -> None:
        nonlocal next_index
        while (
            not cancelled
            and next_index < total_files
            and len(pending) < process_pool.max_workers
        ):
            index = next_index
            path = paths[index]
            if on_file_started is not None:
                on_file_started(index + 1, total_files, path.name)
            if on_file_progress is not None:
                on_file_progress(index + 1, 0, counts[index])
            counter = process_pool.temporary_path(f"pages_{index}.count")
            counter.unlink(missing_ok=True)
            counters[index] = counter
            # El archivo empieza a costar tiempo aquí, no cuando el worker
            # llega a su primera página: entregarlo al pool es lo que dispara
            # el arranque del proceso y la carga de los modelos OCR.
            submitted[index] = time.time()
            future = process_pool.executor.submit(
                _process_pdf_worker,
                path,
                base_config,
                template,
                ranges[index],
                reference_page,
                str(cancel_flag),
                str(counter),
            )
            pending[future] = index
            next_index += 1

    def publish_live_progress() -> None:
        """Reparte el avance de los archivos en vuelo, uno por proceso.

        Sin esto la única señal era el fin de cada archivo: con ocho PDFs a la
        vez las barras se quedaban quietas varios minutos y luego saltaban de
        golpe, mientras la estrategia por páginas avanzaba página a página.
        """
        nonlocal published
        if on_file_progress is None and on_progress is None:
            return
        live = 0
        for index in pending.values():
            measured = _read_page_counter(counters.get(index))
            if measured is None:
                continue
            done, total_in_file = measured
            total_in_file = total_in_file or counts[index]
            done = min(done, total_in_file) if total_in_file else done
            live += done
            if on_file_progress is not None:
                on_file_progress(index + 1, done, total_in_file)
            if on_review_progress is not None:
                review = _read_review_counter(counters.get(index))
                if review is not None:
                    on_review_progress(index + 1, *review)
        if on_progress is not None:
            ready = sum(r is not None for r in reports)
            # El contador global no puede retroceder: un archivo que sale de
            # ``pending`` para reintentarse con el perfil B se lleva su avance
            # en vivo sin haber sumado a ``done_pages``, y sin este tope el
            # texto daría un paso atrás justo después de un fallo.
            published = max(published, min(done_pages + live, total_pages))
            message = (
                f"{len(pending)} archivo(s) en paralelo, "
                f"{ready}/{total_files} listos - {PAGES_STAGE}"
                if pending
                else f"Procesados {ready}/{total_files} archivos"
            )
            on_progress(published, total_pages, message)

    submit_available()
    while pending:
        finished, _waiting = wait(
            tuple(pending), timeout=0.20, return_when=FIRST_COMPLETED
        )
        if cancellation_requested() and not cancelled:
            cancelled = True
            cancel_flag.touch()
            for future in pending:
                future.cancel()
        for future in finished:
            index = pending.pop(future)
            counter = counters.pop(index, None)
            if counter is not None:
                counter.unlink(missing_ok=True)
            if future.cancelled():
                continue
            try:
                report = future.result()
            except Exception as exc:  # noqa: BLE001 - fallback perfil B
                retry.append((index, exc))
                continue
            # El reloj del worker arranca ya dentro del proceso, con los
            # modelos cargados; el del padre cubre la espera completa, que es
            # la que el usuario vio pasar. Sin esto la columna time_ms del
            # CSV se queda corta justo por el arranque del pool.
            started_at = submitted.pop(index, 0.0)
            if started_at > 0:
                report.processing_ms = round(
                    max(
                        report.processing_ms,
                        (time.time() - started_at) * 1000,
                    ),
                    1,
                )
                report.started_at = started_at
            reports[index] = report
            done_pages += len(report.pages)
            if on_file_progress is not None:
                on_file_progress(index + 1, len(report.pages), counts[index])
            if on_file_finished is not None:
                on_file_finished(index + 1, report)
        submit_available()
        publish_live_progress()

    cancel_flag.unlink(missing_ok=True)
    for counter in counters.values():
        counter.unlink(missing_ok=True)
    if retry and not cancelled:
        logger.warning(
            f"[Perfil C] {len(retry)} archivo(s) reintentarán con perfil B"
        )
        for index, first_error in retry:
            path = paths[index]
            try:
                file_config = config_for_pdf(base_config, path)
                pipeline = Pipeline(
                    file_config,
                    fallback_engine,
                    template,
                    workers=process_pool.max_workers,
                    date_engine=date_engine,
                    process_pool=process_pool,
                    reference_page=reference_page,
                )
                report = pipeline.process(path, page_range=ranges[index])
            except Exception as retry_error:
                raise RuntimeError(
                    f"Falló {path.name} en perfiles C y B: "
                    f"C={first_error}; B={retry_error}"
                ) from retry_error
            reports[index] = report
            done_pages += len(report.pages)
            if on_file_progress is not None:
                on_file_progress(index + 1, len(report.pages), counts[index])
            if on_file_finished is not None:
                on_file_finished(index + 1, report)
            if on_progress is not None:
                on_progress(
                    done_pages, total_pages,
                    f"Reintento B completado: {path.name}",
                )

    ordered_reports = [report for report in reports if report is not None]
    logger.info(
        f"[Perfil C] {len(ordered_reports)}/{total_files} archivos | "
        f"cola máxima={process_pool.max_workers} | páginas={done_pages}"
    )
    return ordered_reports
