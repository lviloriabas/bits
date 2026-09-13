"""Generación de PDFs de bitácoras ordenados y separados.

- **PDFs ordenados**: reordenan los escaneos tal cual (sin encabezados ni
  anotaciones, listos para subir a la plataforma de indexado), agrupados
  por avión (matrícula corregida) y/o mes inferido (YYYY-MM). En un
  PDF único, las secciones se ordenan por matrícula y fecha; dentro de cada
  sección las páginas siguen ``log_number`` y logpage.
- **Revisar**: las bitácoras que no son seguras para el indexado automático:
  matrícula sin confirmar, lectura que contradice al consenso del libro,
  confianza insuficiente en la matrícula o una discrepancia de firmas. Se
  escriben siempre, con cualquier combinación de opciones: en el PDF único
  cierran el archivo y en la salida de varios archivos forman
  ``revisar.pdf``. No abren grupo de avión propio, porque una matrícula dudosa
  puesta como título se lee como un avión confirmado.
- **Posibles discrepancias**: las marcadas como discrepancia abren «Revisar»
  bajo su propio separador ``POSIBLES DISCREPANCIAS``, antes del separador
  ``REVISAR`` que encabeza al resto. Van en el mismo archivo y se ordenan
  únicamente por logpage, sin subdividirse por avión ni mes.
- **Recortes de firmas**: volcado de las regiones de firma para auditar
  visualmente los bounding boxes.

Convenciones de nombres (rutas relativas a la carpeta de la ejecución):

- ``<carpeta>.pdf``                  un único PDF: usa el mismo nombre que la
                                     carpeta de la ejecución, sin separadores si
                                     no se eligen condiciones, o con páginas
                                     en blanco de matrícula/mes en grande como
                                      separadores independientes entre grupos
- ``HP-XXXXCMP.pdf``                 solo por avión (PDFs sueltos, sin carpetas)
- ``2026-JUL.pdf`` / ``sf.pdf``      solo por mes (``sf`` = sin fecha)
- ``HP-XXXXCMP_2026-JUL.pdf``        por avión y mes: ambos valores forman el
                                     nombre (``HP-XXXXCMP_sf.pdf`` sin fecha)
- ``discrepancias.pdf``              páginas con discrepancias
- ``revisar.pdf``                    páginas que requieren revisión manual

Las fechas pueden inferirse con el libro y, si el día no se resuelve, con el
último día del mes. «Revisar» se reserva para las páginas cuya matrícula no
permite asignarlas con seguridad a un separador, de modo que ninguna bitácora
queda por fuera de los PDFs generados ni se archiva bajo otro avión.

Exportar de nuevo nunca borra ni sobreescribe los PDFs que ya están en la
carpeta: cuando el nombre está ocupado, la copia nueva lo repite con un
sufijo numérico (``HP-XXXXCMP-2.pdf``, ``-3``…).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pymupdf as fitz  # PyMuPDF
import numpy as np
from loguru import logger
from PIL import Image

from app.models.schemas import (
    FieldResult,
    PageResult,
    Status,
    ValidationReport,
)
from app.templates.schema import FieldType, Template
from app.utils.io import sanitize_filename, unique_path
from app.validation.discrepancias import (
    Discrepancia,
)
from app.validation.grouping import log_number
from app.validation.page_status import needs_review
from app.vision.pdf_loader import PdfDocumentCache, copy_pdf_pages, render_page
from app.vision.reticula import leer_reticula, plantilla_ajustada
from app.vision.signature import SIGNATURE_PAD_X, SIGNATURE_PAD_Y

SIN_MATRICULA = "sin_matricula"
# La sección «Revisar» se escribe siempre: son las bitácoras que nadie pudo
# asignar a un avión, y quedarse fuera de la entrega es peor que cualquier
# opción de separación que se haya marcado.
ETIQUETA_REVISAR = "REVISAR"
# Dentro de «Revisar», las bitácoras marcadas como discrepancia abren su
# propia sección: se completan a mano igual que el resto, pero lo que hay
# que mirar en ellas es una firma, no un dato de índice que falte.
ETIQUETA_DISCREPANCIAS = "POSIBLES DISCREPANCIAS"
NOMBRE_PDF_REVISAR = "revisar.pdf"

_DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
_MATRICULA_RE = re.compile(r"^HP-\d{4}(CMP|WWP)$")
_MONTHS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)
_MONTH_NUMBER = {
    "JAN": 1, "ENE": 1, "FEB": 2, "MAR": 3,
    "APR": 4, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "AGO": 8, "SEP": 9,
    "OCT": 10, "NOV": 11, "DEC": 12, "DIC": 12,
}

@dataclass(frozen=True)
class PaginaRef:
    """Referencia a una página de un reporte, con su posición original.

    ``pdf_path`` es de dónde se lee la página ahora mismo; ``source_name``
    es con qué nombre se la conoce en el CSV. Casi siempre son el mismo
    archivo, pero al apartar la entrada a ``input/processed`` un nombre ya
    usado se numera (``bitacora-2.pdf``) y el reporte se queda apuntando
    ahí, mientras el CSV conserva el nombre original. Quien empareje el
    CSV con la entrega tiene que usar el segundo.
    """

    pdf_path: str
    page: PageResult
    orden: int
    source_name: str = ""

    @property
    def nombre_en_el_csv(self) -> str:
        """El ``file`` con el que esta página sale en el CSV."""
        return self.source_name or Path(self.pdf_path).name


def iterar_paginas(reports: Sequence[ValidationReport]) -> Iterable[PaginaRef]:
    """Itera las páginas de todos los reportes en el orden del batch."""
    orden = 0
    for report in reports:
        for page in report.pages:
            yield PaginaRef(
                str(report.pdf_path), page, orden, report.source_filename
            )
            orden += 1


def clave_avion(page: PageResult) -> str:
    """Matrícula corregida de la página, o 'sin matricula'."""
    for field in page.fields:
        if field.field_id == "matricula" and field.value:
            value = field.value.strip()
            if _MATRICULA_RE.fullmatch(value):
                return value
    return SIN_MATRICULA


def por_revisar(page: PageResult) -> bool:
    """La página necesita el batch manual por falta de datos o discrepancia.

    El criterio vive en ``app.validation.page_status``: es el mismo que
    escribe la columna ``review`` del CSV, y el reparto de la entrega no
    puede decidirlo por su cuenta sin que el archivo y los PDFs dejen de
    contar lo mismo.
    """
    return needs_review(page)


def paginas_para_revisar(
    reports: Sequence[ValidationReport],
) -> List[PaginaRef]:
    """Páginas que requieren revisión, ordenadas por libro y logpage."""
    refs = [
        ref for ref in iterar_paginas(reports)
        if por_revisar(ref.page)
    ]
    refs.sort(key=lambda ref: clave_orden(ref.page, ref.orden))
    return refs


def clave_mes(page: PageResult) -> str:
    """Mes de la página, incluso cuando el día no pudo determinarse."""
    if page.date and _DATE_RE.match(page.date):
        return page.date[:7].replace("/", "-")
    by_id = {field.field_id: field for field in page.fields}
    month_field = by_id.get("month")
    year_field = by_id.get("year")
    if (
        month_field is None
        or year_field is None
        or month_field.status is Status.ERROR
        or year_field.status is Status.ERROR
    ):
        return "sin_fecha"
    month_value = month_field.value or ""
    year_value = year_field.value or ""
    month = _MONTH_NUMBER.get(str(month_value).upper())
    if month is None and str(month_value).isdigit():
        numeric = int(str(month_value))
        month = numeric if 1 <= numeric <= 12 else None
    if str(year_value).isdigit() and len(str(year_value)) in (2, 4):
        year = int(str(year_value))
        if len(str(year_value)) == 2:
            year += 2000
        if month is not None and 2000 <= year <= 2100:
            return f"{year:04d}-{month:02d}"
    return "sin_fecha"


def clave_orden(page: PageResult, orden: int) -> Tuple[int, int]:
    """Clave de orden: log_number (libro + logpage) y posición original."""
    numero = log_number(page)
    return (numero if numero is not None else 1 << 30, orden)


def agrupar_paginas(
    reports: Sequence[ValidationReport],
    separar_por: Sequence[str],
    excluidas: Optional[set[Tuple[str, int]]] = None,
) -> Dict[Tuple[str, ...], List[PaginaRef]]:
    """Agrupa las páginas por las condiciones de separación elegidas.

    Args:
        reports: Reportes de validación (uno por PDF procesado).
        separar_por: Condiciones repetibles ``avion`` y/o ``mes``; vacío
            agrupa todo en una sola clave ().
        excluidas: Conjunto de (nombre del archivo, número de página) que
            NO se incluyen (páginas con discrepancia cuando se genera el
            PDF de discrepancias). Las páginas en blanco y las que no
            son seguras para autoindexar siempre se excluyen: las segundas van
            a la sección «Revisar» (``paginas_para_revisar``).

    Returns:
        Diccionario clave -> lista de páginas ya ordenadas (libro, logpage).
    """
    condiciones = list(separar_por or [])
    for condicion in condiciones:
        if condicion not in ("avion", "mes"):
            raise ValueError(
                f"Condición de separación desconocida: {condicion}"
            )

    grupos: Dict[Tuple[str, ...], List[PaginaRef]] = {}
    excluidas = excluidas or set()
    for ref in iterar_paginas(reports):
        if ref.page.blank:
            continue
        if (Path(ref.pdf_path).name, ref.page.page_number) in excluidas:
            continue
        if por_revisar(ref.page):
            # Va a la sección «Revisar», no a un grupo de avión.
            continue
        if condiciones:
            clave = tuple(
                clave_avion(ref.page) if condicion == "avion"
                else clave_mes(ref.page)
                for condicion in condiciones
            )
        else:
            clave = ()
        grupos.setdefault(clave, []).append(ref)

    for refs in grupos.values():
        refs.sort(key=lambda r: clave_orden(r.page, r.orden))
    return grupos


def nombre_mes(mes: str) -> str:
    """Nombre visible del mes: ``YYYY-MMM`` o ``sf`` sin fecha legible."""
    if mes == "sin_fecha":
        return "sf"
    match = re.fullmatch(r"(\d{4})-(\d{2})", mes)
    if not match:
        return mes
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return mes
    return f"{match.group(1)}-{_MONTHS[month - 1]}"


def ruta_pdf(
    clave: Tuple[str, ...],
    separar_por: Sequence[str],
    run_dir: Optional[Path] = None,
) -> Path:
    """Ruta relativa del PDF de un grupo dentro de la carpeta de la ejecución.

    Cada PDF queda suelto en la carpeta de la ejecución y su nombre incluye
    exactamente los criterios que lo identifican.
    """
    if not separar_por:
        if run_dir is not None:
            nombre = Path(run_dir).name or "bitacoras"
            return Path(f"{sanitize_filename(nombre)}.pdf")
        return Path("bitacoras.pdf")
    if "avion" in separar_por and "mes" in separar_por:
        valores = dict(zip(separar_por, clave))
        avion, mes = valores["avion"], valores["mes"]
        nombre = "_".join((
            sanitize_filename(avion),
            sanitize_filename(nombre_mes(mes)),
        ))
        return Path(f"{nombre}.pdf")
    if "avion" in separar_por:
        return Path(f"{sanitize_filename(clave[0])}.pdf")
    return Path(f"{sanitize_filename(nombre_mes(clave[0]))}.pdf")


def escribir_pdf_paginas(
    refs: Sequence[PaginaRef],
    output_path: Path,
    dpi: int,
    sources: Optional[PdfDocumentCache] = None,
) -> Path:
    """Escribe un PDF copiando los escaneos originales, sin anotaciones.

    ``dpi`` se conserva en la firma por compatibilidad con el CLI y la GUI,
    pero ya no se usa: exportar debe conservar la página fuente sin
    rasterizarla ni recomprimirla.
    """
    del dpi
    copy_pdf_pages(
        ((ref.pdf_path, ref.page.page_number) for ref in refs),
        output_path,
        sources=sources,
    )
    logger.info(f"[Organize] PDF generado: {output_path} "
                f"({len(refs)} páginas)")
    return output_path


def generar_pdfs(
    reports: Sequence[ValidationReport],
    run_dir: Path,
    separar_por: Sequence[str],
    excluidas: Optional[set[Tuple[str, int]]] = None,
    dpi: int = 150,
) -> List[Path]:
    """Genera los PDFs ordenados (uno por grupo) y devuelve sus rutas.

    Los PDFs de una exportación anterior no se pisan: si el nombre del
    grupo ya existe en la carpeta, la copia nueva lleva sufijo numérico.
    Las rutas se devuelven en el orden de ``sorted(grupos)`` y, al final,
    ``revisar.pdf`` con las bitácoras que requieren revisión (siempre que
    haya alguna).

    ``excluidas`` son las páginas que van a ``discrepancias.pdf``, así que
    tampoco entran en ``revisar.pdf``: estarían dos veces en la misma
    carpeta y nadie sabría cuál de las dos copias mirar.
    """
    grupos = agrupar_paginas(reports, separar_por, excluidas)
    fuera = excluidas or set()
    revisar = [
        ref for ref in paginas_para_revisar(reports)
        if (Path(ref.pdf_path).name, ref.page.page_number) not in fuera
    ]
    rutas: List[Path] = []
    refs = [ref for grupo in grupos.values() for ref in grupo] + revisar
    with PdfDocumentCache(ref.pdf_path for ref in refs) as sources:
        for clave in sorted(grupos):
            ruta = escribir_pdf_paginas(
                grupos[clave],
                unique_path(
                    Path(run_dir) / ruta_pdf(clave, separar_por, run_dir)
                ),
                dpi,
                sources=sources,
            )
            rutas.append(ruta)
        if revisar:
            rutas.append(escribir_pdf_paginas(
                revisar,
                unique_path(Path(run_dir) / NOMBRE_PDF_REVISAR),
                dpi,
                sources=sources,
            ))
    return rutas


# ── PDF único (sin separar en varios archivos) ──────────────────────────

def _preparar_paginas(
    reports: Sequence[ValidationReport],
    excluidas: Optional[set[Tuple[str, int]]],
) -> List[PaginaRef]:
    """Devuelve páginas no en blanco ni excluidas, ordenadas por logpage."""
    excluidas = excluidas or set()
    refs: List[PaginaRef] = []
    for ref in iterar_paginas(reports):
        if ref.page.blank:
            continue
        if (Path(ref.pdf_path).name, ref.page.page_number) in excluidas:
            continue
        if por_revisar(ref.page):
            continue
        refs.append(ref)
    refs.sort(key=lambda ref: clave_orden(ref.page, ref.orden))
    return refs


def _preparar_paginas_incluidas(
    reports: Sequence[ValidationReport],
    incluidas: Optional[set[Tuple[str, int]]],
) -> List[PaginaRef]:
    """Devuelve solo las páginas indicadas, ordenadas globalmente por logpage."""
    incluidas = incluidas or set()
    if not incluidas:
        return []
    refs = [
        ref
        for ref in iterar_paginas(reports)
        if not ref.page.blank
        and not por_revisar(ref.page)
        and (Path(ref.pdf_path).name, ref.page.page_number) in incluidas
    ]
    refs.sort(key=lambda ref: clave_orden(ref.page, ref.orden))
    return refs


def _pagina_divisoria(
    doc: fitz.Document,
    texto: str,
    size: Tuple[float, float] = (842.0, 595.0),
) -> None:
    """Añade una página horizontal blanca con texto grande centrado."""
    ancho, alto = max(size), min(size)
    page = doc.new_page(width=ancho, height=alto)
    page.draw_rect(fitz.Rect(0, 0, ancho, alto), fill=(1, 1, 1), color=None)

    lineas = [linea.strip().upper() for linea in texto.splitlines() if linea.strip()]
    if not lineas:
        return
    fontname = "hebo"  # Helvetica-Bold
    fontsize = 72
    while fontsize >= 12 and max(
        fitz.get_text_length(linea, fontname=fontname, fontsize=fontsize)
        for linea in lineas
    ) > ancho - 100:
        fontsize -= 8
    interlineado = fontsize * 1.25
    primera_base = (
        alto / 2 - (len(lineas) - 1) * interlineado / 2 + fontsize * 0.35
    )
    for indice, linea in enumerate(lineas):
        ancho_texto = fitz.get_text_length(
            linea, fontname=fontname, fontsize=fontsize
        )
        page.insert_text(
            ((ancho - ancho_texto) / 2, primera_base + indice * interlineado),
            linea,
            fontname=fontname,
            fontsize=fontsize,
            color=(0.05, 0.05, 0.05),
        )


def _etiqueta_separador(condicion: str, valor: str) -> str:
    """Texto de la página separadora para un criterio y su valor."""
    if condicion == "mes":
        if valor == "sin_fecha":
            return "SIN FECHA"
        etiqueta = nombre_mes(valor)
        if etiqueta == valor:
            return etiqueta
        anio, _, mes = etiqueta.partition("-")
        return f"{mes} {anio}"
    if condicion == "avion" and valor == "sin_matricula":
        return "SIN MATRICULA"
    return valor


def _etiqueta_grupo(
    clave: Tuple[str, ...],
    criterios: Sequence[str],
) -> str:
    """Une matrícula y mes en el mismo separador cuando se piden ambos."""
    valores = dict(zip(criterios, clave))
    orden = [
        condicion for condicion in ("avion", "mes")
        if condicion in valores
    ]
    return "\n".join(
        _etiqueta_separador(condicion, valores[condicion])
        for condicion in orden
    )


def _claves_en_orden_seccion(
    grupos: Dict[Tuple[str, ...], List[PaginaRef]],
    criterios: Sequence[str],
) -> List[Tuple[str, ...]]:
    """Ordena por matrícula y fecha; valores sin determinar van al final."""

    def section_key(clave: Tuple[str, ...]) -> tuple:
        valores = dict(zip(criterios, clave))
        key: List[tuple] = []
        if "avion" in valores:
            avion = valores["avion"]
            if avion == "sin_matricula":
                key.append((1, ""))
            else:
                match = re.search(r"HP-(\d{4})(CMP|WWP)", avion)
                key.append((
                    0,
                    int(match.group(1)) if match else 1 << 30,
                    match.group(2) if match else avion,
                ))
        if "mes" in valores:
            mes = valores["mes"]
            if mes == "sin_fecha":
                key.append((1, 1 << 30, 13))
            else:
                match = re.fullmatch(r"(\d{4})-(\d{2})", mes)
                key.append((
                    0,
                    int(match.group(1)) if match else 1 << 30,
                    int(match.group(2)) if match else 13,
                ))
        key.append(clave_orden(grupos[clave][0].page, grupos[clave][0].orden))
        return tuple(key)

    return sorted(grupos, key=section_key)


def _tamano_horizontal_fuente(
    sources: PdfDocumentCache,
    ref: Optional[PaginaRef],
) -> Tuple[float, float]:
    """Tamaño horizontal de la bitácora que sigue al separador."""
    if ref is None:
        return (842.0, 595.0)
    page = sources.get(ref.pdf_path).load_page(ref.page.page_number - 1)
    return max(page.rect.width, page.rect.height), min(
        page.rect.width, page.rect.height
    )


def _insertar_pagina_fuente(
    doc: fitz.Document,
    sources: PdfDocumentCache,
    ref: PaginaRef,
) -> None:
    """Copia una página fuente al documento sin rasterizarla ni anotarla."""
    doc.insert_pdf(
        sources.get(ref.pdf_path),
        from_page=ref.page.page_number - 1,
        to_page=ref.page.page_number - 1,
    )


@dataclass(frozen=True)
class EntradaPdf:
    """Una página del PDF de entrega: una bitácora o un separador.

    El PDF no es solo las bitácoras: entre ellas van páginas divisorias que
    el CSV no tiene. Quien después empareje ese PDF con el CSV (el indexado
    en AirVault) necesita saber en qué posiciones están, porque si las
    cuenta como bitácoras escribe cada dato una página más allá de donde va.
    """

    separador: str = ""
    ref: Optional[PaginaRef] = None

    @property
    def es_separador(self) -> bool:
        return self.ref is None


@dataclass(frozen=True)
class ArchivoDeEntrega:
    """Un archivo de la entrega con lo que lleva dentro.

    ``revisar`` marca el que recoge las bitácoras que requieren revisión. Va
    aparte porque en AirVault cada archivo es un batch, y ese no se indexa:
    se sube para que alguien lo resuelva a mano.
    """

    ruta: Path
    paginas: List[EntradaPdf]
    revisar: bool = False


def secuencia_de_revisar(
    reports: Sequence[ValidationReport],
) -> List[EntradaPdf]:
    """Bitácoras que requieren revisión, en dos secciones.

    Primero las marcadas como discrepancia, bajo «POSIBLES DISCREPANCIAS»,
    y después las demás bajo «REVISAR». Las dos van en el mismo archivo:
    son un solo batch que se completa a mano, y el separador solo dice qué
    hay que mirar en cada tramo.
    """
    refs = paginas_para_revisar(reports)
    if not refs:
        return []
    discrepantes = [ref for ref in refs if ref.page.discrepancy]
    resto = [ref for ref in refs if not ref.page.discrepancy]
    secuencia: List[EntradaPdf] = []
    for etiqueta, tramo in (
        (ETIQUETA_DISCREPANCIAS, discrepantes), (ETIQUETA_REVISAR, resto),
    ):
        if not tramo:
            continue
        secuencia.append(EntradaPdf(separador=etiqueta))
        secuencia.extend(EntradaPdf(ref=ref) for ref in tramo)
    return secuencia


def secuencia_pdf_unico(
    reports: Sequence[ValidationReport],
    separar_por: Sequence[str] = (),
    excluidas: Optional[set[Tuple[str, int]]] = None,
    discrepancias_al_final: bool = False,
    incluir_revisar: bool = True,
) -> List[EntradaPdf]:
    """Páginas del PDF único en el orden exacto en que se escriben.

    Es la única descripción del orden de entrega: la escritura del PDF
    recorre esta lista, así que no hay forma de que el archivo y lo que
    aquí se declara se separen.
    """
    criterios = list(separar_por or [])
    grupos: Dict[Tuple[str, ...], List[PaginaRef]] = {}
    secuencia: List[EntradaPdf] = []

    if not criterios:
        secuencia.extend(
            EntradaPdf(ref=ref)
            for ref in _preparar_paginas(reports, excluidas)
        )
    else:
        grupos = agrupar_paginas(reports, criterios, excluidas)
        for clave in _claves_en_orden_seccion(grupos, criterios):
            secuencia.append(
                EntradaPdf(separador=_etiqueta_grupo(clave, criterios))
            )
            secuencia.extend(EntradaPdf(ref=ref) for ref in grupos[clave])

    if discrepancias_al_final:
        discrepantes = _preparar_paginas_incluidas(reports, excluidas)
        if discrepantes:
            secuencia.append(EntradaPdf(separador=ETIQUETA_DISCREPANCIAS))
            secuencia.extend(EntradaPdf(ref=ref) for ref in discrepantes)

    if incluir_revisar:
        secuencia.extend(secuencia_de_revisar(reports))

    # Un separador sin ninguna bitácora detrás no llega a escribirse: la
    # divisoria toma su tamaño de la página que la sigue.
    if secuencia and all(entrada.es_separador for entrada in secuencia):
        return []
    return secuencia


NOMBRE_INDICE_PAGINAS = "_paginas.json"


def _secciones(secuencia: Sequence[EntradaPdf]) -> List[List[EntradaPdf]]:
    """Parte la secuencia en tramos que abren con su separador."""
    secciones: List[List[EntradaPdf]] = []
    actual: List[EntradaPdf] = []
    for entrada in secuencia:
        if entrada.es_separador and actual:
            secciones.append(actual)
            actual = []
        actual.append(entrada)
    if actual:
        secciones.append(actual)
    return secciones


def _cortar_seccion(
    seccion: Sequence[EntradaPdf], maximo: int
) -> List[List[EntradaPdf]]:
    """Trocea una seccion que no cabe entera en una parte.

    La continuacion repite el separador de la seccion: sin el, la parte
    siguiente abriria con bitacoras sueltas y nadie sabria de que avion son.
    """
    cabecera = seccion[0] if seccion and seccion[0].es_separador else None
    cuerpo = list(seccion[1:]) if cabecera is not None else list(seccion)
    repetir = cabecera is not None and maximo > 1
    paso = maximo - 1 if repetir else maximo
    trozos: List[List[EntradaPdf]] = []
    for inicio in range(0, len(cuerpo), paso):
        tramo = cuerpo[inicio:inicio + paso]
        trozos.append([cabecera] + tramo if repetir else tramo)
    return trozos or [list(seccion)]


def partir_secuencia(
    secuencia: Sequence[EntradaPdf], paginas_por_parte: int = 0
) -> List[List[EntradaPdf]]:
    """Reparte la entrega en partes de a lo sumo ``paginas_por_parte``.

    Se corta entre secciones siempre que se pueda, para que las bitacoras de
    un mismo avion no queden repartidas entre dos archivos sin necesidad.
    Una seccion que por si sola no cabe se trocea, y cada trozo vuelve a
    abrir con su separador.

    Con cero o con una entrega que ya cabe, devuelve una sola parte: es el
    mismo camino de siempre, no un caso aparte.
    """
    entradas = list(secuencia)
    if not entradas:
        return []
    if paginas_por_parte <= 0 or len(entradas) <= paginas_por_parte:
        return [entradas]

    partes: List[List[EntradaPdf]] = []
    actual: List[EntradaPdf] = []
    for seccion in _secciones(entradas):
        if len(seccion) > paginas_por_parte:
            if actual:
                partes.append(actual)
                actual = []
            trozos = _cortar_seccion(seccion, paginas_por_parte)
            partes.extend(trozos[:-1])
            actual = list(trozos[-1])
            continue
        if actual and len(actual) + len(seccion) > paginas_por_parte:
            partes.append(actual)
            actual = []
        actual.extend(seccion)
    if actual:
        partes.append(actual)
    return partes


def nombre_de_parte(base: str, indice: int, total: int) -> str:
    """Nombre del archivo de una parte: ``<base> -2``, o ``<base>`` si va sola.

    El sufijo no es decoración. Cada archivo es un batch distinto en AirVault
    y los batches se localizan por nombre; dos con el mismo no habría forma de
    distinguirlos. Tiene que coincidir con el sufijo que
    ``app.airvault.naming`` le pone al nombre del batch, y hay una prueba que
    lo comprueba.
    """
    if total <= 1:
        return base
    return f"{base} -{indice}"


def escribir_indice_paginas(
    partes: Sequence[ArchivoDeEntrega], destino: Path
) -> Path:
    """Deja escrito qué hay en cada página de cada PDF de entrega.

    El CSV describe las bitácoras; este archivo describe los PDF, que además
    llevan separadores y pueden venir repartidos en partes. Sin él,
    emparejar un PDF con el CSV por posición se desalinea en el primer
    separador, y una bitácora terminaría indexada con los datos de otra.
    """
    ruta = Path(destino)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        json.dumps(
            {"version": 2, "partes": [
                {"pdf": archivo.ruta.name, "revisar": archivo.revisar,
                 "paginas": _paginas_json(archivo.paginas)}
                for archivo in partes
            ]},
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return ruta


def _paginas_json(secuencia: Sequence[EntradaPdf]) -> List[dict]:
    return [
        {"separador": entrada.separador} if entrada.ref is None
        else {
            # El nombre del CSV, no el del archivo apartado: es la clave
            # con la que el indexado empareja cada pagina con su fila.
            "archivo": entrada.ref.nombre_en_el_csv,
            "pagina": entrada.ref.page.page_number,
            **({"discrepancy_fields": entrada.ref.page.discrepancy_fields}
               if entrada.ref.page.discrepancy_fields else {}),
            **(
                {"revision_pendiente": bool(
                    entrada.ref.page.discrepancy
                    or entrada.ref.page.airvault_discrepancy
                    or entrada.ref.page.date_review
                    or entrada.ref.page.blank
                )}
                if por_revisar(entrada.ref.page) else {}
            ),
            **(
                {"fecha_dudosa": True}
                if entrada.ref.page.date_review else {}
            ),
        }
        for entrada in secuencia
    ]


def escribir_pdf_unico(
    reports: Sequence[ValidationReport],
    run_dir: Path,
    separar_por: Sequence[str] = (),
    excluidas: Optional[set[Tuple[str, int]]] = None,
    dpi: int = 150,
    discrepancias_al_final: bool = False,
) -> Path:
    """Genera un único PDF con el mismo nombre que ``run_dir``.

    Si ese PDF ya existe (re-export), se conserva y el nuevo se escribe con
    sufijo numérico. Sin páginas que exportar no se escribe nada ni se
    toca el PDF anterior.

    - ``separar_por`` vacío: páginas en orden de logpage, sin separadores.
    - Con ``avion`` y/o ``mes``: las páginas se agrupan por esos criterios
      (ordenadas por logpage dentro del grupo) y cada grupo comienza con una
      página blanca horizontal. Las secciones van por matrícula ascendente y
      fecha cronológica, con los valores sin determinar al final. Si se eligen
      ambos criterios, el mismo separador muestra matrícula y mes.
    - Si ``discrepancias_al_final`` es verdadero, las páginas excluidas de los
      grupos se agregan al final bajo un único separador ``POSIBLES
      DISCREPANCIAS``, ordenadas globalmente por logpage.
    - Las páginas que no son seguras para autoindexar cierran bajo el separador
      ``REVISAR``, se hayan pedido o no las discrepancias y se haya elegido
      o no separar por avión: no se deben archivar automáticamente bajo una
      matrícula dudosa, así que siempre quedan juntas y señaladas.
    """
    escritas = escribir_entrega(
        reports, run_dir, separar_por, excluidas, dpi,
        discrepancias_al_final, revisar_aparte=False,
    )
    if not escritas:
        return Path(run_dir) / f"{Path(run_dir).name}.pdf"
    return escritas[0].ruta


def escribir_entrega(
    reports: Sequence[ValidationReport],
    run_dir: Path,
    separar_por: Sequence[str] = (),
    excluidas: Optional[set[Tuple[str, int]]] = None,
    dpi: int = 150,
    discrepancias_al_final: bool = False,
    paginas_por_parte: int = 0,
    revisar_aparte: bool = True,
) -> List[ArchivoDeEntrega]:
    """Escribe la entrega y devuelve cada archivo con lo que lleva dentro.

    Con ``paginas_por_parte`` en cero sale un solo PDF, como siempre. Con un
    tope, la entrega se reparte en partes de a lo sumo esas páginas, cada
    una en su archivo: una ejecución entera son casi dos gigas y ochocientas
    páginas, que en AirVault forman un batch incómodo de subir y de revisar.

    Con ``revisar_aparte`` las bitácoras que requieren revisión cierran en su
    propio archivo en vez de al final del último. En AirVault cada archivo
    es un batch, y esas páginas no se pueden indexar: sueltas dentro de un
    batch de cuatrocientas quedan bloqueadas donde nadie las encuentra.

    Devuelve el nombre definitivo de cada archivo porque solo se conoce
    después de escribir (uno que ya existía obliga a añadir sufijo) y el
    índice de páginas tiene que nombrar el archivo real.
    """
    del dpi  # las páginas se copian sin rasterizar
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    principal = secuencia_pdf_unico(
        reports, separar_por, excluidas, discrepancias_al_final,
        incluir_revisar=not revisar_aparte,
    )
    revisar = secuencia_de_revisar(reports) if revisar_aparte else []
    if not principal and not revisar:
        logger.info(
            f"[Organize] No hay páginas para exportar: {run_dir.name}"
        )
        return []

    partes = partir_secuencia(principal, paginas_por_parte)
    fuentes = [
        e.ref.pdf_path
        for e in list(principal) + list(revisar) if e.ref is not None
    ]
    escritas: List[ArchivoDeEntrega] = []
    with PdfDocumentCache(fuentes) as sources:
        for indice, tramo in enumerate(partes, start=1):
            nombre = nombre_de_parte(run_dir.name, indice, len(partes))
            destino = unique_path(run_dir / f"{nombre}.pdf")
            _escribir_documento(destino, tramo, sources)
            escritas.append(ArchivoDeEntrega(destino, tramo))
        if revisar:
            destino = unique_path(
                run_dir / f"{run_dir.name} {ETIQUETA_REVISAR}.pdf"
            )
            _escribir_documento(destino, revisar, sources)
            escritas.append(ArchivoDeEntrega(destino, revisar, revisar=True))
    if len(partes) > 1:
        logger.info(
            f"[Organize] Entrega repartida en {len(partes)} partes de "
            f"hasta {paginas_por_parte} páginas"
        )
    elif partes:
        logger.info(f"[Organize] PDF único generado: {escritas[0].ruta}")
    if revisar:
        cuantas = sum(1 for entrada in revisar if not entrada.es_separador)
        logger.info(
            f"[Organize] {cuantas} bitácoras para revisar "
            f"en {escritas[-1].ruta.name}"
        )
    return escritas


def _escribir_documento(
    destino: Path, secuencia: Sequence[EntradaPdf], sources: PdfDocumentCache
) -> None:
    """Vuelca una secuencia de páginas en un PDF."""
    doc = fitz.open()
    try:
        for indice, entrada in enumerate(secuencia):
            if entrada.ref is not None:
                _insertar_pagina_fuente(doc, sources, entrada.ref)
                continue
            # La divisoria copia el tamaño de la bitácora que abre, para que
            # la sección no cambie de formato al pasar la página.
            siguiente = next(
                (e.ref for e in secuencia[indice + 1:] if e.ref is not None),
                None,
            )
            _pagina_divisoria(
                doc,
                entrada.separador,
                _tamano_horizontal_fuente(sources, siguiente),
            )
        doc.save(str(destino), deflate=True)
    finally:
        doc.close()


def escribir_pdf_discrepancias(
    entradas: List[Discrepancia],
    template: Template,
    run_dir: Path,
    dpi: int = 150,
) -> Path:
    """Genera discrepancias.pdf con título y páginas ordenadas por logpage."""
    del template, dpi
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = unique_path(run_dir / "discrepancias.pdf")
    ordenadas = sorted(
        enumerate(entradas),
        key=lambda par: (
            par[1].log_number
            if par[1].log_number is not None
            else 1 << 30,
            par[0],
        ),
    )
    doc = fitz.open()
    try:
        with PdfDocumentCache(entrada.pdf_path for _, entrada in ordenadas) as sources:
            primera = ordenadas[0][1]
            source_page = sources.get(primera.pdf_path).load_page(
                primera.page_number - 1
            )
            _pagina_divisoria(
                doc,
                ETIQUETA_DISCREPANCIAS,
                (source_page.rect.width, source_page.rect.height),
            )
            for _, entrada in ordenadas:
                doc.insert_pdf(
                    sources.get(entrada.pdf_path),
                    from_page=entrada.page_number - 1,
                    to_page=entrada.page_number - 1,
                )
        doc.save(str(output_path), deflate=True)
    finally:
        doc.close()
    logger.info(f"[Discrepancias] PDF generado: {output_path} "
                f"({len(entradas)} páginas)")
    return output_path


# ── PDF de errores OCR (indexación manual) ─────────────────────────────

_OCR_MANUAL_FIELDS = ("matricula", "day", "month", "year", "log_number")


def _campos_sin_resolver(page: PageResult) -> List[FieldResult]:
    """Campos OCR que quedaron sin resolver tras los correctores.

    Un campo se considera pendiente de indexación manual cuando no tiene
    valor legible (None) o su estado tras la corrección sigue siendo ERROR
    (p. ej. una fecha incompleta o una matrícula ilegible que el corrector
    de libros no pudo inferir).
    """
    pendientes: List[FieldResult] = []
    for field in page.fields:
        if field.field_id not in _OCR_MANUAL_FIELDS:
            continue
        if not field.value or field.status is Status.ERROR:
            pendientes.append(field)
    return pendientes


def escribir_pdf_errores(
    reports: Sequence[ValidationReport],
    template: Template,
    run_dir: Path,
    dpi: int = 150,
) -> Path:
    """Genera errores.pdf con las páginas fuente pendientes de indexación.

    Incluye solo las páginas que, después de los correctores por libro,
    tienen campos OCR (matrícula, día/mes/año, log_number) sin valor o en
    estado ERROR. Las páginas se conservan sin anotaciones.

    Returns:
        Ruta del PDF generado (run_dir/errores.pdf).
    """
    del template, dpi
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = unique_path(run_dir / "errores.pdf")

    pendientes: List[Tuple[PaginaRef, List[FieldResult]]] = []
    for ref in iterar_paginas(reports):
        if ref.page.blank:
            continue
        campos = _campos_sin_resolver(ref.page)
        if campos:
            pendientes.append((ref, campos))

    copy_pdf_pages(
        ((ref.pdf_path, ref.page.page_number) for ref, _ in pendientes),
        output_path,
    )
    logger.info(f"[Errores] PDF generado: {output_path} "
                f"({len(pendientes)} página(s))")
    return output_path


# ── Recortes de firmas (auditoría de bounding boxes) ───────────────────

def escribir_recortes_firmas(
    reports: Sequence[ValidationReport],
    template: Template,
    run_dir: Path,
    dpi: int = 150,
) -> Path:
    """Vuelca los recortes de las regiones de firma como PNG por campo.

    Sirve para auditar a ojo por qué el detector decidió lo que decidió: el
    recorte incluye el mismo margen que usa ``detect_signature`` y el nombre
    del archivo lleva el veredicto (``true``/``false``/``unclear``), de modo
    que basta ordenar la carpeta por nombre para revisar juntos todos los
    casos inciertos.

    La página se renderiza sin alineación, así que el recorte se sitúa por las
    rayas impresas de esa misma página, igual que hizo el lector: con las
    coordenadas de plantilla sobre una página sin alinear el recuadro se iba
    hasta 11 píxeles, y lo que se auditaba no era lo que se había leído.
    """
    run_dir = Path(run_dir)
    out_root = run_dir / "recortes_firmas"
    campos = [f for f in template.fields if f.type is FieldType.SIGNATURE]
    if not campos:
        logger.warning("[Recortes] La plantilla no define campos de firma")
        return out_root
    for campo in campos:
        (out_root / campo.id).mkdir(parents=True, exist_ok=True)

    total = 0
    for ref in iterar_paginas(reports):
        if ref.page.blank:
            continue
        try:
            image_bgr = render_page(Path(ref.pdf_path), ref.page.page_number, dpi)
            imagen = Image.fromarray(
                np.ascontiguousarray(image_bgr[:, :, ::-1])
            ).convert("RGB")
        except Exception as exc:  # noqa: BLE001 - página no renderizable
            logger.warning(f"[Recortes] Página {ref.page.page_number} "
                           f"no renderizable: {exc}")
            continue
        base = f"{Path(ref.pdf_path).stem}_p{ref.page.page_number:03d}.png"
        leidos = {f.field_id: f.value for f in ref.page.fields}
        pagina_template = template
        if template.reticula is not None:
            reticula = leer_reticula(template, image_bgr)
            if reticula.fiable:
                pagina_template = plantilla_ajustada(template, reticula)
        for campo_plantilla in campos:
            campo = pagina_template.field(campo_plantilla.id) or campo_plantilla
            left, top, right, bottom = campo.rect_pixels(
                imagen.width, imagen.height
            )
            margen_x = max(1, round(SIGNATURE_PAD_X * (right - left)))
            margen_y = max(1, round(SIGNATURE_PAD_Y * (bottom - top)))
            recorte = np.array(imagen)[
                max(0, top - margen_y):min(imagen.height, bottom + margen_y),
                max(0, left - margen_x):min(imagen.width, right + margen_x),
            ]
            veredicto = leidos.get(campo.id) or "sin_lectura"
            Image.fromarray(recorte).save(
                out_root / campo.id / f"{veredicto}_{base}"
            )
            total += 1
    logger.info(f"[Recortes] {total} recorte(s) guardados en {out_root}")
    return out_root
