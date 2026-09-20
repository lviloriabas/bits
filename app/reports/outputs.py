"""Generación de todas las salidas de una ejecución.

Este módulo no depende de Qt. La función pública puede ejecutarse en un
hilo de fondo sin leer ni modificar widgets de la interfaz.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from loguru import logger

from app.models.schemas import ValidationReport
from app.reports.csv_reporter import CSV_DATE_MONTH_END, CsvReporter
from app.reports.dual_csv import minimal_columns, write_minimal_csv
from app.reports.debug_pdf import write_debug_pdf
from app.reports.json_reporter import JsonReporter
from app.templates.schema import Template
from app.validation.date_review import review_date_windows


_MONTHS = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
]


@dataclass(frozen=True)
class OutputOptions:
    """Opciones inmutables necesarias para escribir las salidas."""

    template: Template
    output_root: Path
    dpi: int
    crop_padding: float
    separar_por: tuple[str, ...] = ()
    un_solo_pdf: bool = False
    discrepancias: bool = False
    errores: bool = False
    recortes_firmas: bool = False
    debug: bool = False
    run_dir: Path | None = None
    skip_pdfs: bool = False
    csv_date_mode: str = CSV_DATE_MONTH_END
    # Si la ejecución leyó el día. Va en las salidas para que la ventana de
    # AirVault sepa que una ejecución a fin de mes ya no puede volver a
    # representarse con el día exacto.
    read_day: bool = True
    important_csv_columns: tuple[str, ...] = ()
    # Páginas por parte del PDF único. Cero deja la entrega en un solo
    # archivo; con un tope se reparte, para que ningún batch de AirVault
    # cargue con una ejecución entera.
    paginas_por_parte: int = 0


def complete_csv_path(csv_path: Path) -> Path:
    """Nombre estable del CSV referencial con todas las columnas."""
    csv_path = Path(csv_path)
    return csv_path.with_name(f"{csv_path.stem}_completo{csv_path.suffix}")


def registrar_exportacion(run_dir: Path, options: OutputOptions, evento: str = "exportacion_iniciada") -> None:
    """Conserva la plantilla exacta y opciones de cada exportación."""
    contenido = json.dumps(options.template.model_dump(mode="json"), ensure_ascii=False,
                           sort_keys=True, indent=2).encode("utf-8")
    huella = hashlib.sha256(contenido).hexdigest()
    carpeta = Path(run_dir) / "logs"
    carpeta.mkdir(parents=True, exist_ok=True)
    plantilla = carpeta / f"plantilla-{huella}.json"
    if not plantilla.exists():
        plantilla.write_bytes(contenido)
    registro = {
        "fecha": datetime.now().astimezone().isoformat(),
        "evento": evento, "plantilla_sha256": huella,
        "plantilla": plantilla.name, "discrepancias_a_detectar": options.template.discrepancy_fields,
        "columnas_importantes": list(options.important_csv_columns), "dpi": options.dpi,
        "modo_fecha": options.csv_date_mode, "leer_dia": options.read_day,
        "sin_pdf": options.skip_pdfs,
    }
    with (carpeta / "exportaciones.jsonl").open("a", encoding="utf-8") as salida:
        salida.write(json.dumps(registro, ensure_ascii=False) + "\n")


def marcar_revision(
    reports: Sequence[ValidationReport],
    template: Template,
    date_mode: str = CSV_DATE_MONTH_END,
    important_columns: Sequence[str] = (),
) -> dict[tuple[str, int], tuple[str, ...]]:
    """Decide qué bitácoras van al batch REVISAR y deja la marca en cada una.

    Se resuelve sobre la fila que el CSV está a punto de llevar, no sobre el
    reporte: lo que deja amarilla una página en AirVault es lo que le falte
    al archivo, y un campo que la ejecución leyó pero que no entra en el CSV
    mínimo (el que se sube) no llega a indexarse. Por eso las filas se miran
    ya recortadas a las columnas que ese archivo va a tener.

    Lo demás que aparta una página (discrepancia confirmada, fecha que
    contradice al libro) ya viene marcado en el reporte, y ``needs_review``
    lo vuelve a mirar al escribir la columna ``review``. Aquí solo se añade
    lo único que no se puede saber sin la fila.

    Devuelve, por página, qué campos obligatorios quedarían vacíos, que es
    lo que la ventana de AirVault enseña al preguntar por las amarillas.
    """
    from app.airvault.mapping import obligatorios_vacios_por_pagina

    reports = list(reports)
    filas = CsvReporter.rows_for(reports, template, date_mode=date_mode)
    columnas = minimal_columns(
        CsvReporter.columns_for(reports, template), important_columns
    )
    faltantes = obligatorios_vacios_por_pagina(
        {columna: str(fila.get(columna, "")) for columna in columnas}
        for fila in filas
    )
    discrepancias = 0
    for report in reports:
        archivo = report.source_filename
        for page in report.pages:
            if page.discrepancy:
                discrepancias += 1
            page.airvault_review = bool(
                (archivo, page.page_number) in faltantes
                or page.discrepancy
                or page.airvault_discrepancy
                or page.date_review
            )
    if faltantes or discrepancias:
        logger.info(
            "[Organize] {} páginas van a REVISAR por campos obligatorios "
            "vacíos y {} por discrepancias confirmadas",
            len(faltantes),
            discrepancias,
        )
    return faltantes


def run_csv_name() -> str:
    """Nombre de ejecución en el formato ``BITS <DD MON YYYY> <HH MM>.CSV``."""
    now = datetime.now()
    stamp = now.strftime(f"%d {_MONTHS[now.month - 1]} %Y %H %M").upper()
    return f"BITS {stamp}.CSV"


def new_run_dir(output_root: Path) -> Path:
    """Carpeta de una ejecución nueva, sin pisar ninguna anterior.

    Dos ejecuciones lanzadas dentro del mismo minuto comparten nombre, así que
    la segunda se desempata con un sufijo. La carpeta se crea aquí porque la
    línea de comandos necesita el sitio de los logs antes de procesar, y
    tiene que ser exactamente la misma carpeta donde luego se escriban las
    salidas.
    """
    output_root = Path(output_root)
    base = Path(run_csv_name()).stem
    corrida = base
    n = 2
    while (output_root / corrida).exists():
        corrida = f"{base}-{n}"
        n += 1
    run_dir = output_root / corrida
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _clean_stale_artifacts(run_dir: Path) -> None:
    """Borra los artefactos regenerables de una exportación anterior.

    Conserva ``datos/`` (se sobreescribe), ``logs/`` y **todos los PDFs ya
    exportados**: un re-export nunca destruye una entrega anterior, sino que
    escribe copias con sufijo numérico junto a ellas. Solo se limpia lo que
    la ejecución vuelve a escribir entero (stats, recortes de auditoría).
    """
    keep = {"datos", "logs"}
    for child in run_dir.iterdir():
        if child.name in keep:
            continue
        if child.is_file() and child.suffix.lower() == ".pdf":
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001 - archivo en uso
            logger.warning(f"No se pudo limpiar {child}: {exc}")


def write_outputs(
    reports: Sequence[ValidationReport],
    options: OutputOptions,
    on_stage: Optional[Callable[[str, int], None]] = None,
) -> Path:
    """Escribe los reportes y PDFs de una ejecución completa.

    Todas las operaciones son de disco/renderizado y no deben ejecutarse en
    el hilo de la interfaz. ``on_stage`` recibe (mensaje, porcentaje 0-100)
    al avanzar de cada fase.

    Si ``options.run_dir`` viene, la ejecución se escribe SOBRE esa carpeta
    (mismo nombre de CSV y mismo carpeta de ejecución): es el modo re-export,
    usado por la GUI para regenerar las salidas sin crear una ejecución nueva.
    Si no, se crea una carpeta de ejecución nueva con timestamp.

    Un re-export NO borra los PDFs ya exportados: los conserva y escribe
    los nuevos junto a ellos con sufijo numérico cuando el nombre coincide
    (``HP-1534CMP.pdf`` -> ``HP-1534CMP-2.pdf``).

    Con ``options.skip_pdfs`` (ejecución cancelada a mitad de camino) se
    guardan solo los datos (CSV, JSON, stats) y NO se generan PDFs, para
    que la ejecución quede guardada exactamente hasta donde se canceló.
    """

    def stage(message: str, percent: int) -> None:
        if on_stage is not None:
            on_stage(message, percent)

    reports = list(reports)
    review_date_windows(reports)
    template = options.template
    output_root = Path(options.output_root)
    skip_pdfs = options.skip_pdfs

    if options.run_dir is not None:
        run_dir = Path(options.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        corrida = run_dir.name
        csv_name = f"{corrida}.CSV"
        # La carpeta puede venir recién creada (la línea de comandos la abre
        # antes de procesar, para dejar ahí los logs) o traer una entrega
        # anterior. Solo lo segundo es un re-export.
        reexport = (run_dir / "datos").is_dir()
        _clean_stale_artifacts(run_dir)
        if reexport:
            logger.info(f"Re-export sobre la ejecución existente: {run_dir}")
    else:
        run_dir = new_run_dir(output_root)
        corrida = run_dir.name
        csv_name = f"{corrida}.CSV"

    datos_dir = run_dir / "datos"
    datos_dir.mkdir(parents=True, exist_ok=True)
    registrar_exportacion(run_dir, options)

    from app.validation.discrepancias import clasificar_lote

    entradas = clasificar_lote(reports, template)

    stage("Escribiendo CSV mínimo y completo…", 10)
    csv_path = datos_dir / csv_name
    full_csv_path = complete_csv_path(csv_path)
    # El reparto se decide antes de escribir: la columna ``review`` del CSV
    # es esa misma decisión, así que tomarla después obligaría a rehacer el
    # archivo para poder contarlo.
    marcar_revision(
        reports,
        template,
        date_mode=options.csv_date_mode,
        important_columns=options.important_csv_columns,
    )
    CsvReporter().write(
        reports,
        full_csv_path,
        template,
        date_mode=options.csv_date_mode,
    )
    write_minimal_csv(
        full_csv_path, csv_path, options.important_csv_columns
    )

    # Las que van a «Posibles discrepancias»: las confirmadas y las
    # inciertas por igual. Las confirmadas ya estan en REVISAR; a las
    # inciertas esto solo las agrupa al final de la entrega, sin apartarlas
    # del indexado, y solo cuando se pide la opcion.
    excluidas: set[tuple[str, int]] = set()
    if options.discrepancias:
        excluidas = {
            (Path(entrada.pdf_path).name, entrada.page_number)
            for entrada in entradas
        }
    stage("Escribiendo JSON…", 20)
    json_path = datos_dir / f"{corrida}.json"
    JsonReporter().write_consolidated(
        reports, json_path, corrida=corrida,
        dia_leido=options.read_day,
    )

    if options.debug and not skip_pdfs:
        stage("Generando debug.pdf…", 35)
        write_debug_pdf(
            reports,
            template,
            run_dir / "debug.pdf",
            dpi=options.dpi,
            crop_padding=options.crop_padding,
        )

    if options.errores and not skip_pdfs:
        stage("Generando errores.pdf…", 38)
        from app.reports.organize import escribir_pdf_errores

        errores_path = escribir_pdf_errores(
            reports, template, run_dir, dpi=options.dpi
        )
        logger.info(f"Errores (indexación manual): {errores_path}")

    if options.recortes_firmas and not skip_pdfs:
        stage("Volcando recortes de firmas…", 41)
        from app.reports.organize import escribir_recortes_firmas

        recortes_dir = escribir_recortes_firmas(
            reports, template, run_dir, dpi=options.dpi
        )
        logger.info(f"Recortes de firmas: {recortes_dir}")

    separar = list(options.separar_por) or None
    pdf_unico = options.un_solo_pdf or not separar
    if (options.discrepancias and entradas and not skip_pdfs
            and not pdf_unico):
        stage("Generando discrepancias.pdf…", 45)
        from app.reports.organize import escribir_pdf_discrepancias

        escribir_pdf_discrepancias(
            entradas, template, run_dir, dpi=options.dpi
        )
    elif (options.discrepancias and not entradas and not skip_pdfs
          and not pdf_unico):
        logger.info(
            "Ninguna pagina quedo marcada como discrepancia; no se genera "
            "discrepancias.pdf"
        )

    if skip_pdfs:
        pdf_paths: list[Path] = []
        stage("Guardando datos (sin PDFs)…", 60)
    else:
        from app.reports.organize import (
            NOMBRE_INDICE_PAGINAS,
            escribir_entrega,
            escribir_indice_paginas,
            generar_pdfs,
        )

        stage("Organizando PDFs…", 60)
        if pdf_unico:
            partes = escribir_entrega(
                reports,
                run_dir,
                separar or [],
                excluidas,
                dpi=options.dpi,
                discrepancias_al_final=options.discrepancias,
                paginas_por_parte=options.paginas_por_parte,
            )
            pdf_paths = [archivo.ruta for archivo in partes]
            # El indexado en AirVault empareja cada PDF con el CSV por
            # posición, y los PDF llevan separadores que el CSV no tiene: se
            # deja escrito qué hay en cada página de cada archivo.
            escribir_indice_paginas(
                partes, datos_dir / f"{corrida}{NOMBRE_INDICE_PAGINAS}"
            )
            if len(pdf_paths) == 1:
                logger.info(f"PDF único: {pdf_paths[0].relative_to(run_dir)}")
            elif pdf_paths:
                logger.info(
                    f"Entrega en {len(pdf_paths)} partes de hasta "
                    f"{options.paginas_por_parte} páginas"
                )
        elif separar:
            pdf_paths = generar_pdfs(
                reports,
                run_dir,
                separar,
                excluidas,
                dpi=options.dpi,
            )
            logger.info(f"PDFs separados: {len(pdf_paths)}")
            for pdf_path in pdf_paths:
                logger.info(f"  - {pdf_path.relative_to(run_dir)}")

    from app.reports.stats import escribir_stats

    stage("Calculando estadísticas…", 85)
    stats_path = escribir_stats(
        reports,
        run_dir,
        corrida=corrida,
        separar_por=None if skip_pdfs else separar,
        entradas=entradas,
        excluidas=excluidas or None,
        pdf_paths=pdf_paths,
    )
    logger.info(f"Stats de la ejecución: {stats_path}")
    registrar_exportacion(run_dir, options, "exportacion_completada")
    stage("Finalizando…", 100)
    logger.info(f"Outputs generados en: {run_dir}")

    return run_dir
