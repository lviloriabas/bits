"""Worker en QThread para ejecutar el pipeline sin congelar la GUI."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import TYPE_CHECKING, List, Union

from loguru import logger
from PySide6.QtCore import QThread, Signal

from app.core.config import AppConfig
from app.core.page_range import PageRange, slice_batch, slice_paths
from app.core.progress import VOID_STAGE
from app.models.schemas import ValidationReport
from app.templates.manager import TemplateManager
from app.utils.fleet import load_fleet
from app.validation.book_corrector import correct_matricula_by_book
from app.validation.date_corrector import correct_dates_by_book
from app.validation.log_sequence import infer_log_numbers_from_pdf_order

if TYPE_CHECKING:  # el módulo de salidas se importa dentro del hilo
    from app.reports.outputs import OutputOptions


class PipelineWorker(QThread):
    """Ejecuta el pipeline de validación en un hilo separado.

    Puede procesar una sola bitácora o un batch. Con un batch, los
    correctores por libro (matrícula/fecha) se aplican sobre todos los
    reportes antes de emitir ``succeeded``. Al cancelar (requestInterruption)
    se emite ``succeeded`` con los reportes ya procesados (parciales), para
    que la GUI guarde el trabajo en curso.
    """

    progress = Signal(int, int, str)
    file_started = Signal(int, int, str)
    # (archivo 1-based, páginas hechas, páginas del archivo)
    file_progress = Signal(int, int, int)
    # (archivo 1-based, hojas revisadas, hojas por revisar) de la comprobación
    # de VOID: una segunda vuelta que no son páginas del documento.
    review_progress = Signal(int, int, int)
    file_finished = Signal(int, object)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        pdf_paths: Union[Path, List[Path]],
        template_path: Path,
        config: AppConfig,
        page_range: PageRange | None = None,
        reference_page: int = 1,
        workers: int = 1,
        cpu_threads: int | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if isinstance(pdf_paths, Path):
            pdf_paths = [pdf_paths]
        self.pdf_paths = [Path(p) for p in pdf_paths]
        self.template_path = Path(template_path)
        self.config = config
        self.page_range = page_range
        self.reference_page = max(1, reference_page)
        self.workers = max(1, workers)
        self.cpu_threads = cpu_threads
        self._progress_offset = 0
        self._prev_total = 0
        self._progress_file = 0
        self._current_file_index = 0
        # Archivos que el rango deja dentro; se fija al arrancar la ejecución.
        self._active_paths: List[Path] = list(self.pdf_paths)
        self.reports: List[ValidationReport] = []

    def run(self) -> None:
        """Punto de entrada del hilo."""
        try:
            from contextlib import nullcontext

            from app.core.pipeline import (
                OcrProcessPool,
                Pipeline,
                process_pdf_batch,
            )
            from app.core.config import config_for_pdf
            from app.ocr.engine import create_engine

            template = TemplateManager().load(self.template_path)
            engine_kwargs = {}
            if self.cpu_threads is not None:
                engine_kwargs["cpu_threads"] = self.cpu_threads
            if self.config.ocr_rec_model:
                engine_kwargs["rec_model"] = self.config.ocr_rec_model
            if self.config.ocr_det_model:
                engine_kwargs["det_model"] = self.config.ocr_det_model
            engine = create_engine(
                self.config.ocr_engine,
                lang=self.config.ocr_lang,
                **engine_kwargs,
            )
            date_engine = None
            if self.config.date_engine_name:
                date_engine = create_engine(
                    self.config.date_engine_name,
                    lang=self.config.ocr_lang,
                    **engine_kwargs,
                )
            reports: List[ValidationReport] = []
            pool_context = (
                OcrProcessPool(
                    self.workers,
                    self.config,
                    self.config.ocr_engine,
                    self.config.ocr_lang,
                    self.cpu_threads,
                    self.config.date_engine_name or None,
                )
                if self.workers > 1
                else nullcontext(None)
            )
            with pool_context as process_pool:
                if process_pool is not None:
                    reports = process_pdf_batch(
                        self.pdf_paths,
                        self.config,
                        template,
                        process_pool,
                        engine,
                        date_engine=date_engine,
                        page_range=self.page_range,
                        reference_page=self.reference_page,
                        should_cancel=self.isInterruptionRequested,
                        on_file_started=lambda index, total, name: (
                            self.file_started.emit(index, total, name)
                        ),
                        on_file_finished=lambda index, report: (
                            self.file_finished.emit(index, report)
                        ),
                        on_progress=lambda done, total, message: (
                            self.progress.emit(done, total, message)
                        ),
                        on_file_progress=lambda index, done, total: (
                            self.file_progress.emit(index, done, total)
                        ),
                        on_review_progress=lambda index, done, total: (
                            self.review_progress.emit(index, done, total)
                        ),
                    )
                    self.reports = list(reports)
                else:
                    logger.info(
                        "[Perfil C] activo | estrategia=secuencial | workers=1"
                    )
                    # El rango es del batch completo: recorta qué archivos
                    # entran y con qué páginas antes de abrir ninguno.
                    slices = slice_paths(self.pdf_paths, self.page_range)
                    self._active_paths = [item.path for item in slices]
                    for index, item in enumerate(slices):
                        if self.isInterruptionRequested():
                            break
                        pdf_path = item.path
                        self._current_file_index = index + 1
                        self.file_started.emit(
                            index + 1, len(slices), pdf_path.name
                        )
                        try:
                            file_config = config_for_pdf(self.config, pdf_path)
                        except (FileNotFoundError, ValueError):
                            # El Pipeline conserva el error autoritativo. Este
                            # fallback permite inyectar pipelines de prueba.
                            file_config = self.config
                        pipeline = Pipeline(
                            file_config,
                            engine,
                            template,
                            on_progress=self._on_progress,
                            workers=self.workers,
                            cpu_threads=self.cpu_threads,
                            date_engine=date_engine,
                            process_pool=process_pool,
                            reference_page=self.reference_page,
                        )
                        report: ValidationReport = pipeline.process(
                            pdf_path,
                            page_range=item.pages,
                            should_cancel=self.isInterruptionRequested,
                        )
                        reports.append(report)
                        self.reports = list(reports)
                        self.file_progress.emit(
                            index + 1, len(report.pages), len(report.pages)
                        )
                        self.file_finished.emit(index + 1, report)
                        if report.cancelled:
                            break
            # Con la verificación activa, la flota también ayuda a leer: una
            # lectura ruidosa que solo se parece a un avión se toma por él.
            fleet = (
                load_fleet(self.config.fleet_file)
                if self.config.verify_fleet else []
            )
            infer_log_numbers_from_pdf_order(reports)
            correct_matricula_by_book(
                reports, self.config.book_matriculas_file, fleet=fleet
            )
            correct_dates_by_book(reports, self.config.book_fechas_file)
            if self.config.verify_fleet:
                from app.validation.fleet import verify_reports_against_fleet

                verify_reports_against_fleet(reports, self.config.fleet_file)
            from app.validation.book_corrector import learn_book_matriculas
            from app.validation.date_corrector import learn_book_dates

            learn_book_matriculas(
                reports, self.config.book_matriculas_file, fleet=fleet
            )
            learn_book_dates(reports, self.config.book_fechas_file)
            self.reports = reports
            self.succeeded.emit(reports)
        except Exception as exc:  # noqa: BLE001 - la GUI muestra el error
            self.failed.emit(f"{exc}\n{traceback.format_exc()}")

    def _on_progress(self, done: int, total: int, message: str) -> None:
        """Acumula el progreso entre bitácoras para un conteo global.

        Cada Pipeline emite ``(done, total)`` relativo a su propio PDF; al
        cambiar de archivo el contador vuelve a empezar, así que se acumula
        un desplazamiento con el total de la bitácora anterior. El mensaje se
        antepone con el archivo actual para mostrar el avance por bitácora.

        El total emitido solo cubre los archivos ya vistos: con el rango
        completo los tramos llegan sin recuento y esta ruta no abre los PDFs
        para contarlos. La ventana usa el total del batch, que ya calculó antes
        de arrancar, y este par sirve para el avance por archivo.
        """
        if self._current_file_index != self._progress_file:
            self._progress_offset += self._prev_total
            self._progress_file = self._current_file_index
            self._prev_total = total
        if message == VOID_STAGE:
            # Hojas de la comprobación de VOID, no páginas: el archivo ya leyó
            # las suyas, así que su avance se queda donde estaba.
            if self._current_file_index:
                self.review_progress.emit(
                    self._current_file_index, done, total
                )
            pages_done = self._progress_offset + self._prev_total
            self.progress.emit(
                pages_done, pages_done,
                self._message_prefix() + f"{message} {done}/{total}",
            )
            return
        # Hay etapas que informan con las páginas leídas en vez del total del
        # tramo (la revisión de firmas, o una cancelación a media bitácora).
        # El desplazamiento se queda con el mayor total visto del archivo:
        # sumarlo otra vez, como hacía la comparación anterior, adelantaba el
        # contador global de golpe en cuanto llegaba un total más pequeño.
        self._prev_total = max(self._prev_total, total)
        if self._current_file_index:
            self.file_progress.emit(self._current_file_index, done, total)
        self.progress.emit(self._progress_offset + done,
                           self._progress_offset + self._prev_total,
                           self._message_prefix() + message)

    def _message_prefix(self) -> str:
        """``Archivo i/n: nombre - `` del archivo en curso, o nada."""
        if not (self._current_file_index and self._active_paths):
            return ""
        name = self._active_paths[self._current_file_index - 1].name
        return (f"Archivo {self._current_file_index}/"
                f"{len(self._active_paths)}: {name} - ")


class PreprocessWorker(QThread):
    """Calcula la geometría de preprocesado de cada página, sin OCR.

    Emite la *geometría* (ángulo de deskew + transformación de alineación
    normalizada), no la imagen. La vista previa reconstruye la página con
    esos números cuando hace falta, que es exactamente lo que ya hace con las
    páginas ya procesadas. Emitir la imagen obligaba a la ventana a retener un
    ``QImage`` de página completa por página: 11.2 MB a 200 DPI, sin tope, es
    decir 0.55 GB en un libro de 50 páginas y 4.3 GB en uno de 393.
    """

    progress = Signal(int, int, str)
    # (pdf, número de página, geometría); ver ``_geometry``.
    page_ready = Signal(str, int, object)
    succeeded = Signal(bool)
    failed = Signal(str)

    def __init__(
        self,
        pdf_paths: List[Path],
        template_path: Path,
        config: AppConfig,
        page_range: PageRange | None = None,
        reference_page: int = 1,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.pdf_paths = [Path(path) for path in pdf_paths]
        self.template_path = Path(template_path)
        self.config = config
        self.page_range = page_range
        self.reference_page = max(1, reference_page)

    def run(self) -> None:
        """Renderiza, endereza y alinea las páginas para su revisión visual."""
        try:
            from app.vision.alignment import (
                compute_similarity_transform,
                load_template_reference,
                warp_with_transform,
            )
            from app.vision.field_geometry import place_fields
            from app.core.config import config_for_pdf
            from app.vision.pdf_loader import PdfPageRenderer, page_count
            from app.vision.preprocessing import deskew

            template = TemplateManager().load(self.template_path)

            slices = slice_batch(
                self.pdf_paths,
                [page_count(pdf_path) for pdf_path in self.pdf_paths],
                self.page_range,
            )
            total = sum(item.count or 0 for item in slices)

            done = 0
            for item in slices:
                if self.isInterruptionRequested():
                    break

                pdf_path = item.path
                first, last = item.pages.first, item.pages.last or 0
                count = item.count or 0
                file_config = config_for_pdf(self.config, pdf_path)
                with PdfPageRenderer(pdf_path) as renderer:
                    reference = None
                    if file_config.align and count:
                        reference = load_template_reference(
                            template, file_config.dpi
                        )
                        if reference is None:
                            reference = renderer.render_page(
                                min(first + self.reference_page - 1, last),
                                file_config.dpi,
                            )

                    for page_number in range(first, last + 1):
                        if self.isInterruptionRequested():
                            break
                        self.progress.emit(
                            done,
                            total,
                            f"Preprocesando {pdf_path.name}: "
                            f"página {page_number}/{last}",
                        )
                        image = renderer.render_page(
                            page_number, file_config.dpi
                        )
                        angle = 0.0
                        if file_config.deskew:
                            image, angle = deskew(image)
                        alignment = None
                        if file_config.align and reference is not None:
                            transform = compute_similarity_transform(
                                image, reference, file_config
                            )
                            if transform.reliable:
                                # Ratios, no píxeles: la vista previa
                                # rasteriza a otra resolución que este paso.
                                height, width = image.shape[:2]
                                alignment = {
                                    "rot": float(transform.rot),
                                    "tx_ratio": float(transform.tx) / max(width, 1),
                                    "ty_ratio": float(transform.ty) / max(height, 1),
                                    "scale": float(transform.scale),
                                    "target_width_ratio": (
                                        float(reference.shape[1]) / max(width, 1)
                                    ),
                                    "target_height_ratio": (
                                        float(reference.shape[0]) / max(height, 1)
                                    ),
                                }
                                image = warp_with_transform(
                                    image, transform,
                                    (reference.shape[1], reference.shape[0]),
                                )
                        placement = place_fields(template, image)
                        boxes = {
                            field.id: [field.x, field.y, field.w, field.h]
                            for field in placement.template.fields
                        }
                        done += 1
                        self.page_ready.emit(
                            str(pdf_path),
                            page_number,
                            {"skew_angle": float(angle), "alignment": alignment,
                             "boxes": boxes},
                        )
                        self.progress.emit(
                            done,
                            total,
                            f"Preprocesando {pdf_path.name}: "
                            f"página {page_number}/{last}",
                        )
            self.succeeded.emit(self.isInterruptionRequested())
        except Exception as exc:  # noqa: BLE001 - la GUI muestra el error
            self.failed.emit(f"{exc}\n{traceback.format_exc()}")


class OutputsWorker(QThread):
    """Genera las salidas de una ejecución sin bloquear la interfaz."""

    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str, int)

    def __init__(
        self,
        reports: List[ValidationReport],
        options: "OutputOptions",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.reports = reports
        self.options = options

    def run(self) -> None:
        """Punto de entrada del hilo de generación de salidas."""
        try:
            from app.reports.outputs import write_outputs

            output_dir = write_outputs(
                self.reports,
                self.options,
                on_stage=self._on_stage,
            )
        except Exception as exc:  # noqa: BLE001 - la GUI muestra el error
            self.failed.emit(f"{exc}\n{traceback.format_exc()}")
        else:
            self.succeeded.emit(output_dir)

    def _on_stage(self, message: str, percent: int) -> None:
        self.progress.emit(message, percent)


class InputScanWorker(QThread):
    """Lee DPI y numero de paginas de la entrada, fuera del hilo de la GUI.

    Abrir los PDFs de ``input/`` es lo primero que hace la ventana al
    arrancar, y es lo que la dejaba en «no responde» nada mas aparecer: los
    escaneos son de cientos de megas y la primera apertura arrastra ademas
    PyMuPDF, NumPy y OpenCV, que no estan cargados todavía. Medido con ocho
    archivos de la carpeta de trabajo, un cuarto de segundo largo con la
    ventana ya dibujada y sin atender un solo clic.

    Las dos lecturas se hacen con el mismo handle porque el rango de paginas
    necesita el recuento y el DPI ya obligaba a abrir cada archivo.

    ``generacion`` viaja de ida y vuelta en las señales: elegir otros
    archivos mientras un recorrido esta en marcha deja dos hilos vivos, y la
    ventana tiene que poder descartar el que ya no corresponde.
    """

    # (generacion, [(ruta, dpi, paginas)…])
    scanned = Signal(int, object)

    def __init__(
        self, pdf_paths: List[Path], generacion: int = 0, parent=None
    ) -> None:
        super().__init__(parent)
        self.pdf_paths = [Path(path) for path in pdf_paths]
        self.generacion = int(generacion)

    def run(self) -> None:
        """Recorre la entrada y entrega lo leido de cada archivo."""
        from app.vision.pdf_loader import PdfPageRenderer

        leido: List[tuple] = []
        for path in self.pdf_paths:
            if self.isInterruptionRequested():
                return
            try:
                with PdfPageRenderer(path) as renderer:
                    leido.append(
                        (path, renderer.detect_dpi(default=600),
                         renderer.page_count())
                    )
            except Exception:  # noqa: BLE001 - PDF invalido, se sigue
                logger.warning(f"No se pudo leer la entrada: {path}")
                leido.append((path, 0, 0))
        if not self.isInterruptionRequested():
            self.scanned.emit(self.generacion, leido)
