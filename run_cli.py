#!/usr/bin/env python3
"""Interfaz de línea de comandos de BITS - Clasificación de Bitácoras.

Por defecto procesa TODOS los PDFs de la carpeta ``input/`` y genera un
único CSV consolidado en ``output/`` con nombre en mayúsculas en el formato
``BITS <DD MON YYYY> <HH MM>.CSV`` (fecha/hora de la ejecución).

Todos los outputs de la ejecución se generan dentro de una carpeta con el
nombre del CSV (sin extensión): ``output/BITS <DD MON YYYY> <HH MM>/``.
El CSV y el JSON consolidado (mismo nombre que el CSV) van en su
subcarpeta ``datos/``; junto a ellos quedan ``stats.json``, los logs,
los PDFs ordenados (PDFs por matrícula, o carpetas por matrícula con un
PDF por mes dentro) y los informes opcionales.

Opciones útiles:
    --pages 1-40      Procesar solo ese rango de páginas del batch completo,
                      numerado de corrido sobre los PDFs de la entrada.
    --debug           Generar debug.pdf con los bounding boxes de los
                      campos sobre los archivos procesados.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from app.utils.portable import ensure_portable_env

ensure_portable_env()
os.chdir(_ROOT)

from app.core.config import AppConfig, config_for_pdf
from app.core.page_range import PageRange, slice_paths
from app.core.pipeline import OcrProcessPool, Pipeline, process_pdf_batch
from app.core.parallelism import available_cpu_threads, recommended_parallelism
from app.core.progress import with_page_counter
from app.models.schemas import Status
from app.ocr.engine import create_engine
from app.reports.csv_reporter import (
    CSV_DATE_MONTH_END,
    CSV_DATE_SPECIFIC,
    CsvReporter,
)
from app.reports.outputs import (
    OutputOptions,
    complete_csv_path,
    new_run_dir,
    write_outputs,
)
from app.templates.manager import TemplateManager
from app.templates.schema import Template
from app.utils.fleet import FLEET_FILENAME
from app.utils.important_fields import (
    IMPORTANT_FIELDS_FILENAME,
    ImportantFieldsStore,
    default_important_columns,
)
from app.utils.logging import setup_logging
from app.validation.date_corrector import BOOK_DATES_FILENAME
from app.validation.discrepancias import con_discrepancias_elegidas


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="logbook-classification",
        description=(
            "Validates scanned aircraft logbooks against a template. "
            "Processes all PDFs in the input folder by default."
        ),
    )
    parser.add_argument(
        "--pdf", default=None,
        help="PDF específico a procesar (default: todos los PDFs de input/)",
    )
    parser.add_argument(
        "--input-dir", default="input",
        help="Carpeta con los PDFs a analizar (default: input)",
    )
    parser.add_argument(
        "--template",
        default=str(
            Path(__file__).resolve().parent
            / "template/aircraft_log.json"
        ),
        help="Plantilla JSON (default: aircraft_log.json)",
    )
    parser.add_argument(
        "--output-dir", default="output", help="Carpeta de resultados"
    )
    parser.add_argument("--dpi", type=int, default=150, help="DPI de renderizado")
    parser.add_argument(
        "--pages", default=None, metavar="RANGO",
        help="Procesar solo un rango de páginas del batch completo, numerado "
             "de corrido sobre los PDFs ordenados de la entrada: '1-40' "
             "(las 40 primeras del batch, caigan donde caigan), '200-' (desde "
             "la 200 hasta el final) o '15' (solo esa página). Sin esta "
             "opción se procesa todo.",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Generar debug.pdf con las páginas originales, sin anotaciones",
    )
    parser.add_argument(
        "--reference-page", type=int, default=1,
        help="Página usada como referencia de alineación (default: 1)",
    )
    parser.add_argument(
        "--threads", "--cpu-threads", dest="cpu_threads", type=int,
        default=None,
        help="Hilos totales del procesador (default: todos los disponibles)",
    )
    parser.add_argument("--no-deskew", action="store_true",
                        help="Desactivar corrección de inclinación")
    parser.add_argument("--no-align", action="store_true",
                        help="Desactivar alineación con plantilla")
    parser.add_argument(
        "--no-remove-printed", action="store_true",
        help="No construir el mapa del fondo impreso idéntico en todas las "
             "páginas. Recomendado dejarlo activado para firmas y ranuras.",
    )
    parser.add_argument(
        "--no-crop-preprocess", action="store_true",
        help="Sin preprocesado de los recortes (escala + localización de "
             "tinta): se envía al OCR el recorte crudo. Útil para comparar "
             "motores con escritura a mano.",
    )
    parser.add_argument(
        "--separar-por", action="append", choices=["avion", "mes"],
        default=None,
        help="Separar los archivos en PDFs independientes. Repetible: "
             "'avion' (un PDF por aeronave), 'mes' (un PDF por mes) o ambos "
             "para separar simultáneamente. Sin esta opción se genera un "
             "único PDF con el mismo nombre que la carpeta de salida.",
    )
    parser.add_argument(
        "--paginas-por-parte", type=int, default=0, metavar="N",
        help="Reparte el PDF unico en partes de a lo sumo N paginas. Cada "
             "parte es un batch aparte en AirVault. Cero deja un solo "
             "archivo.",
    )
    parser.add_argument(
        "--un-solo-pdf", action="store_true",
        help="Generar un único PDF con el mismo nombre que la carpeta de "
             "salida: con --separar-por se "
             "insertan páginas separadoras de matrícula/mes entre los "
             "grupos; sin separadores, un PDF plano sin dividir.",
    )
    parser.add_argument(
        "--discrepancias", action="store_true",
        help="Separar las páginas con firmas faltantes o inciertas. En un "
             "PDF único se agregan al final bajo 'Posibles discrepancias'; "
             "en modo de varios archivos se genera discrepancias.pdf.",
    )
    parser.add_argument(
        "--recortes-firmas", action="store_true",
        help="Volcar los recortes de las regiones de firma a "
             "recortes_firmas/ para auditar visualmente los bounding boxes "
             "(usar con --pages para batches pequeños).",
    )
    parser.add_argument(
        "--errores", action="store_true",
        help="Generar errores.pdf con las páginas cuyos campos OCR "
             "(matrícula, fechas, log_number) quedaron sin resolver tras "
             "los correctores, sin anotaciones.",
    )
    parser.add_argument(
        "--verificar-flota", action="store_true",
        help="Clasificar cada matrícula leída contra la lista de aviones: "
             "la que no esté en la lista se reclasifica como la más "
             "parecida de la flota. La lista debe tener todos los aviones.",
    )
    parser.add_argument(
        "--lista-flota", default=None, metavar="ARCHIVO",
        help=f"Lista de aviones de la flota (default: {FLEET_FILENAME} en "
             "la carpeta del programa).",
    )
    parser.add_argument(
        "--fecha-csv", choices=["fin-de-mes", "especifica"],
        default="fin-de-mes",
        help="Fecha representada en el CSV: 'fin-de-mes' usa siempre el "
             "último día del mes; 'especifica' usa el día leído y cae al "
             "fin de mes cuando falta (default: fin-de-mes).",
    )
    parser.add_argument(
        # ``--campos-importantes`` es como se llamó hasta que la interfaz
        # pasó a decir «columnas importantes». Se conserva como alias para
        # no romper los .cmd que ya la escriben.
        "--columnas-importantes", "--campos-importantes",
        dest="columnas_importantes", default=None, metavar="COLUMNAS",
        help="Columnas del CSV mínimo, separadas por coma. Sin esta opción "
             "se usa la selección guardada en la carpeta del programa, la "
             "misma que aplica la interfaz.",
    )
    parser.add_argument("--verbose", action="store_true", help="Logs detallados")
    return parser.parse_args()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 - CLI amigable
        print(f"\nERROR: {exc}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


def _resolve_pdfs(args: argparse.Namespace) -> list[Path]:
    if args.pdf:
        pdf = Path(args.pdf)
        if not pdf.exists():
            raise FileNotFoundError(f"PDF no encontrado: {pdf}")
        return [pdf]
    folder = Path(args.input_dir)
    if not folder.is_dir():
        raise FileNotFoundError(
            f"Carpeta de entrada no encontrada: {folder}"
        )
    pdfs = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
    )
    if not pdfs:
        raise ValueError(f"No hay archivos PDF en {folder}")
    return pdfs


def _important_columns(
    template: Template, seleccion: str | None
) -> tuple[str, ...]:
    """Columnas del CSV mínimo, las mismas que aplicaría la interfaz.

    Sin ``--columnas-importantes`` se lee la selección que el selector de la
    ventana dejó guardada en la carpeta del programa, y si esa plantilla
    nunca se editó se usa el mismo conjunto por defecto. Así una ejecución de
    línea de comandos y una de la interfaz escriben el mismo CSV mínimo.
    """
    columns = CsvReporter.columns_for_fields(
        [field.id for field in template.fields],
        skip_ids=frozenset(
            field.id
            for field in template.fields
            if field.type.value == "signature"
        ),
    )
    if seleccion is not None:
        elegidas = {
            name.strip() for name in seleccion.split(",") if name.strip()
        }
    else:
        store = ImportantFieldsStore(_ROOT / IMPORTANT_FIELDS_FILENAME)
        guardadas = store.load(template.name)
        elegidas = (
            set(guardadas) if guardadas is not None
            else default_important_columns(columns)
        )
    return tuple(column for column in columns if column in elegidas)


def _print_pdf_result(pdf_path: Path, report, pages: PageRange) -> None:
    tramo = "" if pages.is_full else f" ({pages.label()} del archivo)"
    print(f"\n>>> {pdf_path.name}{tramo}")
    summary = report.summary
    print(f"  Resumen {pdf_path.name}: {summary.get('total_pages', 0)} "
          f"páginas | OK: {summary.get('ok_pages', 0)} "
          f"| WARNING: {summary.get('warning_pages', 0)} "
          f"| ERROR: {summary.get('error_pages', 0)} "
          f"| {report.processing_ms / 1000:.2f} s")
    for page in report.pages:
        if page.status is Status.OK:
            continue
        for field in page.fields:
            if field.status is not Status.OK:
                print(f"    P{page.page_number:02d} "
                      f"{field.field_id}: {field.status.value} "
                      f"({field.value!r})")
                break


def _run(args: argparse.Namespace) -> int:
    from loguru import logger

    # Con la misma selección de discrepancias que la interfaz: es la misma
    # instalación y el mismo archivo local, y las dos superficies tienen que
    # entregar el mismo CSV sobre la misma plantilla.
    template = con_discrepancias_elegidas(
        TemplateManager().load(Path(args.template))
    )
    config = AppConfig(
        dpi=args.dpi,
        deskew=not args.no_deskew,
        align=not args.no_align,
        ocr_engine="paddle",
        ocr_lang="en",
        remove_printed=not args.no_remove_printed,
        crop_preprocess=not args.no_crop_preprocess,
        ocr_rec_model="PP-OCRv5_mobile_rec",
        ocr_det_model="PP-OCRv6_medium_det",
        date_engine_name="",
        date_slot_ocr=False,
        # A fin de mes el día no llega a ninguna salida: no se lee.
        read_day=args.fecha_csv != "fin-de-mes",
        verify_fleet=args.verificar_flota,
        fleet_file=(
            Path(args.lista_flota) if args.lista_flota
            else _ROOT / FLEET_FILENAME
        ),
        book_fechas_file=_ROOT / BOOK_DATES_FILENAME,
    )

    page_range = PageRange()
    if args.pages is not None:
        try:
            page_range = PageRange.parse(args.pages)
        except ValueError as exc:
            print(f"ERROR: --pages inválido: {exc}", file=sys.stderr)
            return 1
    if args.cpu_threads is not None and args.cpu_threads < 1:
        print("ERROR: --threads debe ser >= 1", file=sys.stderr)
        return 1

    # ── Carpeta de la ejecución: nombre del CSV (sin extensión) ──────────
    run_dir = new_run_dir(Path(args.output_dir))
    setup_logging(run_dir / "logs",
                  level="DEBUG" if args.verbose else "INFO")

    entrada = _resolve_pdfs(args)
    # El rango numera el batch de corrido: se reparte entre los archivos que
    # lo contienen y los demás ni se abren.
    slices = slice_paths(entrada, page_range)
    if not slices:
        print(
            f"ERROR: el rango ({page_range.label()}) no incluye ninguna "
            f"página de los {len(entrada)} archivo(s) de la entrada",
            file=sys.stderr,
        )
        return 1
    pdfs = [item.path for item in slices]

    def on_progress(done: int, total: int, message: str) -> None:
        if total:
            pct = int(done * 100 / total)
            # El contador de páginas lo pone quien conoce el par que se está
            # mostrando; el pipeline solo nombra la etapa (ver
            # ``app.core.progress``).
            text = with_page_counter(done, total, message)
            print(f"\r[{pct:3d}%] {text:<60}", end="", flush=True)

    print(f"Analizando {len(pdfs)} archivo(s) de: {pdfs[0].parent}")
    if not page_range.is_full:
        print(f"Rango del batch: {page_range.label()}")
    print(f"Plantilla : {template.name} ({len(template.fields)} campos)")
    print(f"Salida    : {run_dir}")

    selected_threads = (
        args.cpu_threads
        if args.cpu_threads is not None
        else available_cpu_threads()
    )
    workers, cpu_threads = recommended_parallelism(selected_threads)
    engine_kwargs = {"cpu_threads": cpu_threads} if cpu_threads else {}
    if config.ocr_rec_model:
        engine_kwargs["rec_model"] = config.ocr_rec_model
    if config.ocr_det_model:
        engine_kwargs["det_model"] = config.ocr_det_model
    engine = create_engine(
        config.ocr_engine, lang=config.ocr_lang, **engine_kwargs
    )
    date_engine = None
    print(
        "Motor OCR fijo: PaddleOCR "
        "PP-OCRv6_medium_det + PP-OCRv5_mobile_rec"
    )
    print(
        f"Hilos seleccionados: {selected_threads} | "
        f"distribución automática: {workers} worker(s) x "
        f"{cpu_threads} hilos c/u"
    )

    reports = []
    process_pool = (
        OcrProcessPool(
            workers,
            config,
            config.ocr_engine,
            config.ocr_lang,
            cpu_threads,
            None,
        )
        if workers > 1 else None
    )
    if process_pool is not None:
        print(
            f"Perfil C activo: {workers} workers persistentes, "
            f"planificación adaptativa y colas acotadas"
        )
        reports = process_pdf_batch(
            entrada,
            config,
            template,
            process_pool,
            engine,
            date_engine=date_engine,
            page_range=page_range,
            reference_page=args.reference_page,
            on_progress=on_progress,
        )
        for item, report in zip(slices, reports):
            _print_pdf_result(item.path, report, item.pages)
    else:
        print("Perfil C activo: ejecución secuencial con un worker")
        for item in slices:
            pdf_path = item.path
            file_config = config_for_pdf(config, pdf_path)
            logger.info(f"[CLI] Procesando: {pdf_path.name}")
            print(
                f"\nProcesando {pdf_path.name}: "
                f"{file_config.dpi} DPI base / "
                f"{file_config.date_dpi} DPI fecha"
            )
            pipeline = Pipeline(
                file_config,
                engine,
                template,
                on_progress=on_progress,
                workers=workers,
                cpu_threads=cpu_threads,
                date_engine=date_engine,
                process_pool=process_pool,
                reference_page=args.reference_page,
            )
            report = pipeline.process(pdf_path, page_range=item.pages)
            reports.append(report)
            _print_pdf_result(pdf_path, report, item.pages)

    if process_pool is not None:
        process_pool.close()

    # ── Corrector de matrículas por libro (un avión por libro) ──────────
    from app.validation.book_corrector import correct_matricula_by_book
    from app.validation.date_corrector import correct_dates_by_book
    from app.validation.log_sequence import infer_log_numbers_from_pdf_order

    from app.utils.fleet import load_fleet

    fleet = load_fleet(config.fleet_file) if config.verify_fleet else []
    inferred = infer_log_numbers_from_pdf_order(reports)
    print(f"\nNúmeros de bitácora deducidos por sus páginas vecinas: {inferred}")

    stats = correct_matricula_by_book(
        reports, config.book_matriculas_file, fleet=fleet
    )
    print(f"\nCorrector de matrículas por libro: {stats['books']} libro(s), "
          f"{stats['corrected']} matrícula(s) corregidas, "
          f"{stats['flagged']} discrepante(s) marcada(s)")

    stats = correct_dates_by_book(reports, config.book_fechas_file)
    print(f"Corrector de fechas por libro: {stats['books']} libro(s), "
          f"{stats['corrected']} fecha(s) corregidas, "
          f"{stats['flagged']} discrepante(s) marcada(s), "
          f"{stats['regressions']} regresión(es) de fecha")

    # ── Lista de flota: lo que no sea un avión conocido se reclasifica ──
    if config.verify_fleet:
        from app.validation.fleet import verify_reports_against_fleet

        verify_reports_against_fleet(reports, config.fleet_file)
        print(f"Lista de aviones: {config.fleet_file}")

    from app.validation.book_corrector import learn_book_matriculas
    from app.validation.date_corrector import learn_book_dates

    learn_book_matriculas(reports, config.book_matriculas_file, fleet=fleet)
    learn_book_dates(reports, config.book_fechas_file)

    # ── Salidas de la ejecución ───────────────────────────────────────────
    # Se escriben con la misma función que usa la interfaz. Tenerlas
    # duplicadas era lo que hacía que las dos superficies entregaran
    # carpetas distintas sobre la misma ejecución.
    def on_stage(message: str, percent: int) -> None:
        print(f"[{percent:3d}%] {message}")

    print()
    run_dir = write_outputs(
        reports,
        OutputOptions(
            template=template,
            output_root=Path(args.output_dir),
            dpi=args.dpi,
            crop_padding=config.crop_padding,
            separar_por=tuple(args.separar_por or ()),
            un_solo_pdf=args.un_solo_pdf,
            paginas_por_parte=args.paginas_por_parte,
            discrepancias=args.discrepancias,
            errores=args.errores,
            recortes_firmas=args.recortes_firmas,
            debug=args.debug,
            run_dir=run_dir,
            csv_date_mode=(
                CSV_DATE_MONTH_END if args.fecha_csv == "fin-de-mes"
                else CSV_DATE_SPECIFIC
            ),
            read_day=args.fecha_csv != "fin-de-mes",
            important_csv_columns=_important_columns(
                template, args.columnas_importantes
            ),
        ),
        on_stage=on_stage,
    )

    corrida = run_dir.name
    datos_dir = run_dir / "datos"
    csv_path = datos_dir / f"{corrida}.CSV"
    json_path = datos_dir / f"{corrida}.json"
    total_ms = sum(r.processing_ms for r in reports)
    print(f"\nSalidas en: {run_dir}")
    print(f"  CSV mínimo  : {csv_path}")
    print(f"  CSV completo: {complete_csv_path(csv_path)}")
    print(f"  JSON        : {json_path} ({len(reports)} archivo(s))")
    print(f"  Stats       : {run_dir / 'stats.json'}")
    for pdf_path in sorted(run_dir.rglob("*.pdf")):
        print(f"  PDF         : {pdf_path.relative_to(run_dir)}")
    print(f"Tiempo total: {total_ms / 1000:.2f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
