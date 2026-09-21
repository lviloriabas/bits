"""Clasificación de páginas con discrepancias (faltas de firma).

Reglas:

La discrepancia es de cada bitácora por separado. El libro no interviene:
dos páginas seguidas del mismo avión pueden ser una de vuelo y otra de
mantenimiento, y cada una se juzga sola.

El tipo lo deciden solo las casillas **limpias**: la licencia de técnico y el
bloque del capitán. ``technician_signature`` queda fuera de esa decisión
porque cae justo debajo de los sellos «MXI Entry Performed By» y «DATE / STA»,
que la llenan de tinta ajena. De 30 páginas revisadas a mano en las que el
detector la daba por escrita, ninguna tenía firma. Un sello solo añade tinta y
nunca la quita, así que su lectura «ausente» sigue siendo de fiar: el campo se
conserva como requisito de mantenimiento, pero no puede decidir el tipo.

- Una página es de **mantenimiento** cuando ``technician_license`` está
  escrita de forma confiable, o cuando lo está el bloque de corrección
  (``correction_block``): el recuadro «CORRECTION OR DEFERRAL» donde el
  técnico describe el trabajo hecho o el diferimiento aplicado. Escribir ahí
  es haber intervenido la aeronave, y una intervención se cierra con firmas
  aunque la casilla de la licencia haya quedado en blanco; de hecho es
  justamente ese caso —trabajo descrito y nadie que lo firme— el que hay que
  reclamar.
- Es de **vuelo** cuando esa licencia está vacía, el bloque de corrección no
  tiene nada escrito y hay algo escrito en el bloque del capitán o en la
  firma del piloto.
- Una marca **VOID** grande y confirmada en la imagen anula los requisitos
  de firmas, aunque queden vuelos, firmas o trabajo escrito debajo. Conserva
  el indice de avion, logpage y fecha. Las casillas vacias no confirman VOID.
- Si ninguna casilla limpia lo dice con seguridad, el tipo es **incierto**
  (INCIERTO): se reportan solo las anomalías robustas (firma de piloto,
  exigida en las dos interpretaciones) y las casillas ilegibles que impiden
  decidir. El resto es ambiguo: acusarlo produciría discrepancias falsas.
- **Vuelo**: deben estar presentes la firma de piloto, la firma de capitán y
  la licencia del capitán.
- **Mantenimiento**: deben estar presentes la firma de piloto, la firma de
  técnico y la licencia del técnico. La firma y la licencia de capitán no se
  miran: el formulario F-MNT-001 es uno solo y lleva el bloque de
  mantenimiento («MAINTENANCE RETURN TO SERVICE» + «MECH. LICENSE No.») y el
  de aceptación de la aeronave («MAINTENANCE CHECK AIRWORTHINESS RELEASE»,
  que firma el capitán) en la misma hoja. Que estén los dos es lo normal, no
  una anomalía.

De las dos licencias solo se mira si la casilla está escrita o vacía. No se
lee lo que dice: el número no forma parte del índice ni de la regla.

El bloque de corrección se mide con el mismo detector de escritura que las
firmas, pero con umbrales propios: es una casilla mucho más ancha que alta y
el rasgo que distingue una corrección escrita del papel rayado vacío es la
tinta *repartida* a lo largo de la línea, no un trazo denso en un punto. Por
eso pide densidad ``max_empty_peak`` sostenida sobre ``min_ink_span`` del
ancho.

La extensión sola no basta. Lo que invade esta casilla desde la fila de
arriba son los sellos «MXI Entry Performed By» y «DATE / STA», y un sello no
es un borrón: es un recuadro con su rótulo, su número y su raya, que llega a
cruzar dos tercios del ancho. Sobre un libro entero de 122 páginas, las
cuatro que el detector daba por escritas sin serlo eran las cuatro un sello,
y las cuatro cruzaban entre el 56 % y el 70 % del ancho. Lo que sí las
separa es cuánta tinta hay en el recuadro *entero*: un sello ensucia entre
el 2.5 % y el 3.9 %, y la corrección más floja de ese mismo libro, el 5.7 %.
Por eso el campo exige además ``min_ink_coverage`` (0.05), a mitad de camino
entre los dos grupos. Lo que no lo alcanza queda en *incierto*, que es donde
tiene que quedar: no es una corrección y no debe reclamar firmas.

La "presencia" de una firma se decide con el resultado del detector
(``true`` / ``false`` / ``unclear``) combinado con la confianza y los
umbrales del campo (``sig_present_conf`` / ``sig_absent_conf``): una
lectura de baja confianza nunca se acusa como falta (evita discrepancias
falsas) y se marca como *incierta* (categoría UNCERTAIN, revisión manual).

El menú «Discrepancias a detectar» no toca nada de lo anterior. Solo filtra
lo ya clasificado, con tres interruptores que corresponden a los tres casos
en que esta clasificación reclama firmas: vuelo, mantenimiento y
mantenimiento nacido del bloque de corrección (``por_correccion``). Cada uno
lleva la lista de casillas que se reclaman en él (``Template
.discrepancy_types``), así que apagar la firma de piloto en vuelo no la apaga
en mantenimiento. Las páginas de tipo incierto no responden a ningún
interruptor (``tipo_de_menu`` devuelve None) y se reportan como siempre.

Las discrepancias se ordenan globalmente por número de bitácora
(``log_number``) ascendente, sin subdividirlas por matrícula o mes.

Cada discrepancia confirmada resume en una frase qué le falta a la página
(``Discrepancia.resumen``). Es lo que va a la columna ``disc_reason`` del
CSV, así que se escribe corto y en el idioma del que revisa: «Faltan firma
de técnico y licencia de técnico», no una lista de identificadores.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger
from pydantic import BaseModel, Field

from app.models.schemas import FieldResult, PageResult, ValidationReport
from app.templates.schema import Template
from app.utils.preferencias_ui import guardar_opcion, leer_opcion
from app.validation.grouping import log_number

FIELD_PILOT = "pilot_signature"
FIELD_CAPTAIN = "captain_signature"
FIELD_CAPTAIN_LICENSE = "captain_license"
FIELD_TECH = "technician_signature"
FIELD_TECH_LICENSE = "technician_license"
FIELD_CORRECTION = "correction_block"

_MATRICULA_RE = re.compile(r"^HP-\d{4}(CMP|WWP)$")

# Cómo se nombra cada casilla cuando es ella la que impide decidir el tipo.
_NOMBRE_ILEGIBLE = {
    FIELD_TECH_LICENSE: "Licencia de técnico",
    FIELD_CAPTAIN: "Firma de capitán",
    FIELD_CAPTAIN_LICENSE: "Licencia del capitán",
}

# Los tres tipos que el menú deja encender y apagar. No son una regla nueva:
# salen de lo que `_clasificar_sin_filtro` ya decidió, y solo sirven para
# saber a qué interruptor responde la página.
TIPO_VUELO = "vuelo"
TIPO_MANTENIMIENTO = "mantenimiento"
TIPO_CORRECCION = "correccion"

# Qué casillas reclama cada tipo. Es el mismo juego que arman las listas de
# `requisitos` dentro de `_clasificar_sin_filtro`: aquí solo se repite para
# poder dibujar el menú sin clasificar una página. Si allá cambian los
# requisitos, esta tabla tiene que seguirlos.
CAMPOS_POR_TIPO = {
    TIPO_VUELO: (FIELD_PILOT, FIELD_CAPTAIN, FIELD_CAPTAIN_LICENSE),
    TIPO_MANTENIMIENTO: (FIELD_PILOT, FIELD_TECH, FIELD_TECH_LICENSE),
    TIPO_CORRECCION: (FIELD_PILOT, FIELD_TECH, FIELD_TECH_LICENSE),
}

# Dónde se guarda lo que el menú deja elegido, y con qué nombre.
#
# Es una preferencia de quien revisa, no parte de la plantilla: dos personas
# con la misma plantilla pueden querer reclamar cosas distintas, y mañana la
# misma persona otra. Vivía dentro del JSON de la plantilla, que sí se
# versiona, así que marcar una casilla dejaba el repositorio con una
# modificación pendiente y el pull de la otra máquina chocaba contra ella.
# La plantilla conserva el valor de partida, el que se reparte con el
# programa; lo elegido se queda en el archivo local de la interfaz.
#
# La clave lleva el nombre del archivo de la plantilla y no su ruta: la
# carpeta entera se copia a otro Windows, donde la ruta ya no existe.
_SECCION_DISCREPANCIAS = "discrepancias"


def nombre_de_plantilla(template: Template) -> str:
    """Con qué nombre se guarda lo elegido para esa plantilla."""
    if template.source_path is not None:
        return Path(template.source_path).stem
    return template.name or "plantilla"


def discrepancias_elegidas(template: Template) -> Dict[str, List[str]]:
    """Qué casillas reclama hoy cada tipo, con la plantilla sin ajuste.

    Sin nada elegido y sin nada escrito en la plantilla se reclama todo,
    que es como el programa viene de fábrica.
    """
    guardado = leer_opcion(
        f"{_SECCION_DISCREPANCIAS}.{nombre_de_plantilla(template)}"
    )
    if not isinstance(guardado, dict):
        guardado = template.discrepancy_types
    return {
        tipo: list(campos if guardado is None else guardado.get(tipo, campos))
        for tipo, campos in CAMPOS_POR_TIPO.items()
    }


def guardar_discrepancias_elegidas(
    template: Template, activas: Dict[str, List[str]]
) -> bool:
    """Anota la selección de esa plantilla sin tocar su archivo."""
    return guardar_opcion(
        f"{_SECCION_DISCREPANCIAS}.{nombre_de_plantilla(template)}",
        {tipo: list(campos) for tipo, campos in activas.items()},
    )


def con_discrepancias_elegidas(template: Template) -> Template:
    """La plantilla con lo elegido puesto, que es lo que filtra al clasificar.

    Se aplica al cargarla para procesar (la interfaz y la línea de comandos)
    y no al cargarla para editarla: el editor rehace el archivo, y meterle
    la selección de esta máquina lo devolvería al repositorio.
    """
    return template.model_copy(
        update={"discrepancy_types": discrepancias_elegidas(template)}
    )


# Cómo se nombra cada tipo en el menú.
NOMBRE_TIPO = {
    TIPO_VUELO: "De vuelo",
    TIPO_MANTENIMIENTO: "De mantenimiento",
    TIPO_CORRECCION: "Del bloque de correcciones (mantenimiento)",
}

# Cómo se nombra cada casilla en el menú. Repite la redacción de las razones
# para que lo que se apaga aquí se reconozca en el reporte.
NOMBRE_CAMPO = {
    FIELD_PILOT: "Firma de piloto",
    FIELD_CAPTAIN: "Firma de capitán",
    FIELD_CAPTAIN_LICENSE: "Licencia de capitán",
    FIELD_TECH: "Firma de técnico",
    FIELD_TECH_LICENSE: "Licencia de técnico",
}

# Cómo se nombra cada casilla dentro del resumen de una línea. Van en
# minúscula porque se encadenan detrás de «Falta(n)».
_NOMBRE_CORTO = {
    FIELD_PILOT: "firma de piloto",
    FIELD_CAPTAIN: "firma de capitán",
    FIELD_CAPTAIN_LICENSE: "licencia de capitán",
    FIELD_TECH: "firma de técnico",
    FIELD_TECH_LICENSE: "licencia de técnico",
}


def _enumerar(nombres: List[str]) -> str:
    """Une los nombres en castellano: «a», «a y b», «a, b y c»."""
    if len(nombres) <= 1:
        return "".join(nombres)
    return f"{', '.join(nombres[:-1])} y {nombres[-1]}"


class TipoEntrada(str, Enum):
    """Tipo de entrada de la bitácora."""

    VUELO = "vuelo"
    MANTENIMIENTO = "mantenimiento"
    INCIERTO = "incierto"


class Categoria(str, Enum):
    """Categoría de la discrepancia."""

    MISSING = "missing"      # firma faltante confirmada
    UNCERTAIN = "uncertain"  # firma incierta (revisión manual)


class CampoAfectado(BaseModel):
    """Campo de firma afectado, con su categoría y razón legible."""

    field_id: str
    categoria: Categoria
    razon: str


class Discrepancia(BaseModel):
    """Página con discrepancia de firmas."""

    pdf_path: str
    page_number: int
    matricula: Optional[str] = None
    log_number: Optional[int] = None
    tipo: TipoEntrada
    categoria: Categoria
    campos: List[CampoAfectado] = Field(default_factory=list)
    # La exigencia de firmas nació del bloque de corrección escrito y no de
    # la licencia de técnico. Cambia cómo se explica la falta, no cuáles se
    # exigen: el resumen tiene que decir de dónde salió el reclamo para que
    # quien revise sepa dónde mirar en la hoja.
    por_correccion: bool = False

    def razones(self) -> List[str]:
        """Razones legibles de la discrepancia, en orden de importancia."""
        return [campo.razon for campo in self.campos]

    def resumen(self) -> str:
        """Una línea con lo que le falta a la página, para la columna del CSV.

        Solo nombra las ausencias confirmadas: una lectura incierta no es una
        falta y no se acusa (la misma regla que deja ``page.discrepancy`` en
        ``False``). Sin ninguna ausencia confirmada devuelve cadena vacía, que
        es lo que el CSV escribe cuando no hay nada que reportar.
        """
        faltan = [
            _NOMBRE_CORTO[campo.field_id]
            for campo in self.campos
            if campo.categoria is Categoria.MISSING
            and campo.field_id in _NOMBRE_CORTO
        ]
        if not faltan:
            return ""
        verbo = "Falta" if len(faltan) == 1 else "Faltan"
        frase = f"{verbo} {_enumerar(faltan)}"
        if self.por_correccion:
            return f"Corrección escrita: {frase[0].lower()}{frase[1:]}"
        return frase


def _campo(page: PageResult, field_id: str) -> Optional[FieldResult]:
    for field in page.fields:
        if field.field_id == field_id:
            return field
    return None


def _presencia(campo: Optional[FieldResult], field_template) -> Optional[bool]:
    """Presencia de una firma: True, False o None (incierta).

    El umbral se toma del campo de la plantilla:
    - ``true`` con confianza >= ``sig_present_conf``  -> presente.
    - ``false`` con confianza >= ``sig_absent_conf``  -> ausente.
    - Cualquier otro caso (``unclear``, confianza baja) -> incierta.
    """
    if campo is None or not campo.value:
        return None
    if campo.value == "true" and campo.confidence >= field_template.sig_present_conf:
        return True
    if campo.value == "false" and campo.confidence >= field_template.sig_absent_conf:
        return False
    return None


def _campo_presente(
    page: PageResult, template: Template, field_id: str
) -> Optional[bool]:
    """Presencia de un campo de firma, o ``False`` si no está en la plantilla.

    Un campo que el esquema no define no puede estar escrito, así que no
    aporta evidencia de que la entrada sea de mantenimiento.
    """
    plantilla = template.field(field_id)
    if plantilla is None:
        return False
    return _presencia(_campo(page, field_id), plantilla)


def _matricula(page: PageResult) -> Optional[str]:
    """Matrícula corregida de la página (canónica HP-XXXXCMP/WWP), o None."""
    campo = _campo(page, "matricula")
    if campo is None or not campo.value:
        return None
    value = campo.value.strip()
    return value if _MATRICULA_RE.fullmatch(value) else None


def _clasificar_sin_filtro(page: PageResult, template: Template
                       ) -> Optional[Tuple[TipoEntrada, Categoria,
                                           List[CampoAfectado], bool]]:
    """Clasifica una página y devuelve sus campos de firma afectados.

    Returns:
        (tipo, categoria, campos afectados, por corrección) si hay
        discrepancia, o None si la página cumple todas las firmas requeridas.
    """
    if (page.void_mark is not None and page.void_mark.text == "VOID"
            and page.void_mark.confidence >= .80):
        return None
    licencia_tecnico = _campo_presente(page, template, FIELD_TECH_LICENSE)
    firma_capitan = _campo_presente(page, template, FIELD_CAPTAIN)
    licencia_capitan = _campo_presente(page, template, FIELD_CAPTAIN_LICENSE)
    firma_piloto = _campo_presente(page, template, FIELD_PILOT)
    correccion = _campo_presente(page, template, FIELD_CORRECTION)

    # Sin evidencia de uso no se puede atribuir una falta de firma a vuelo
    # o mantenimiento. Esto NO confirma VOID: solo la palabra grande leida
    # en la imagen lo confirma, incluso si quedaron vuelos o firmas debajo.
    if (licencia_tecnico is False and firma_capitan is False
            and licencia_capitan is False and firma_piloto is False
            and correccion is not True):
        return None

    # El tipo lo deciden solo las casillas limpias. La de firma de técnico
    # queda fuera de esta decisión: cae justo debajo de los sellos «MXI Entry
    # Performed By» y «DATE / STA», que la llenan de tinta ajena, y sobre 30
    # páginas revisadas a mano ninguna tenía firma. Un sello solo añade tinta,
    # nunca la quita, así que su lectura «ausente» sigue siendo de fiar y por
    # eso el campo se conserva como requisito de mantenimiento; lo que no
    # soporta es decidir de qué tipo es la bitácora.
    #
    # El bloque de corrección escrito basta por sí solo para exigir el juego
    # de firmas de mantenimiento: describe un trabajo hecho sobre la
    # aeronave, y no lo escribe nadie más que el técnico que lo hizo.
    por_correccion = licencia_tecnico is not True and correccion is True
    if licencia_tecnico is True or correccion is True:
        tipo = TipoEntrada.MANTENIMIENTO
        contexto = ("corrección escrita" if por_correccion
                    else "entrada de mantenimiento")
        requisitos = [
            (f"Falta firma de piloto ({contexto})",
             f"Firma de piloto incierta ({contexto}); revisar",
             FIELD_PILOT),
            (f"Falta firma de técnico ({contexto})",
             f"Firma de técnico incierta ({contexto}); revisar",
             FIELD_TECH),
            (f"Falta licencia de técnico ({contexto})",
             f"Licencia de técnico incierta ({contexto}); revisar",
             FIELD_TECH_LICENSE),
        ]
    elif licencia_tecnico is False and True in (
        firma_capitan, licencia_capitan, firma_piloto
    ):
        tipo = TipoEntrada.VUELO
        requisitos = [
            ("Falta firma de piloto",
             "Firma de piloto incierta; revisar", FIELD_PILOT),
            ("Falta firma de capitán",
             "Firma de capitán incierta; revisar", FIELD_CAPTAIN),
            ("Falta licencia del capitán",
             "Firma de licencia del capitán incierta; revisar",
             FIELD_CAPTAIN_LICENSE),
        ]
    else:
        # Ninguna casilla limpia dice con seguridad de qué tipo es la
        # bitácora: no se puede decidir entre vuelo, mantenimiento y hoja
        # anulada. Solo se reportan anomalías robustas (firma de piloto,
        # exigida en las dos interpretaciones) y las casillas ilegibles que
        # impiden decidir. El resto es ambiguo: acusarlo produciría
        # discrepancias falsas.
        tipo = TipoEntrada.INCIERTO
        afectados: List[CampoAfectado] = [
            CampoAfectado(
                field_id=field_id,
                categoria=Categoria.UNCERTAIN,
                razon=(
                    f"{_NOMBRE_ILEGIBLE[field_id]} ilegible; no se pudo "
                    "determinar de qué tipo es la bitácora"
                ),
            )
            for field_id, presencia in (
                (FIELD_TECH_LICENSE, licencia_tecnico),
                (FIELD_CAPTAIN, firma_capitan),
                (FIELD_CAPTAIN_LICENSE, licencia_capitan),
            )
            if presencia is None
        ]
        tmpl_piloto = template.field(FIELD_PILOT)
        if tmpl_piloto is not None:
            presencia = _presencia(_campo(page, FIELD_PILOT), tmpl_piloto)
            if presencia is None:
                afectados.append(CampoAfectado(
                    field_id=FIELD_PILOT, categoria=Categoria.UNCERTAIN,
                    razon="Firma de piloto incierta (tipo de página incierto); "
                          "revisar",
                ))
            elif presencia is False:
                afectados.append(CampoAfectado(
                    field_id=FIELD_PILOT, categoria=Categoria.MISSING,
                    razon="Falta firma de piloto (tipo de página incierto)",
                ))
        categoria = (
            Categoria.MISSING
            if any(a.categoria is Categoria.MISSING for a in afectados)
            else Categoria.UNCERTAIN
        )
        return tipo, categoria, afectados, False

    afectados: List[CampoAfectado] = []
    for razon_missing, razon_uncertain, field_id in requisitos:
        tmpl = template.field(field_id)
        if tmpl is None:
            continue
        presencia = _presencia(_campo(page, field_id), tmpl)
        if presencia is None:
            afectados.append(CampoAfectado(
                field_id=field_id, categoria=Categoria.UNCERTAIN,
                razon=razon_uncertain,
            ))
        elif presencia is False:
            afectados.append(CampoAfectado(
                field_id=field_id, categoria=Categoria.MISSING,
                razon=razon_missing,
            ))

    if not afectados:
        return None
    categoria = (
        Categoria.MISSING
        if any(a.categoria is Categoria.MISSING for a in afectados)
        else Categoria.UNCERTAIN
    )
    return tipo, categoria, afectados, por_correccion


def tipo_de_menu(tipo: TipoEntrada, por_correccion: bool) -> Optional[str]:
    """A qué interruptor del menú responde una página ya clasificada.

    No decide nada: traduce lo que `_clasificar_sin_filtro` devolvió. Las
    páginas de tipo incierto no responden a ningún interruptor y por eso
    devuelven None: el menú no las gobierna y se reportan como siempre.
    """
    if tipo is TipoEntrada.VUELO:
        return TIPO_VUELO
    if tipo is TipoEntrada.MANTENIMIENTO:
        return TIPO_CORRECCION if por_correccion else TIPO_MANTENIMIENTO
    return None


def _clasificar_pagina(page: PageResult, template: Template):
    """Aplica la seleccion sin perder la evidencia para distinguir el tipo."""
    resultado = _clasificar_sin_filtro(page, template)
    if resultado is None or template.discrepancy_types is None:
        return resultado
    tipo, categoria, campos, por_correccion = resultado
    clave = tipo_de_menu(tipo, por_correccion)
    if clave is None:
        return resultado
    activos = template.discrepancy_types.get(clave, CAMPOS_POR_TIPO[clave])
    campos = [c for c in campos if c.field_id in activos]
    if not campos:
        return None
    categoria = (Categoria.MISSING if any(c.categoria is Categoria.MISSING
                                         for c in campos) else Categoria.UNCERTAIN)
    return tipo, categoria, campos, por_correccion


def clasificar_lote(reports: List[ValidationReport], template: Template
                    ) -> List[Discrepancia]:
    """Clasifica todas las páginas del batch y devuelve las discrepancias.

    Marca ``page.discrepancy`` solo en las páginas con una ausencia
    confirmada, y deja en ``page.discrepancy_note`` la frase que explica
    cuál es. Las lecturas inciertas se devuelven igual, y con ellas se
    escribe el reporte de discrepancias, pero no llevan marca ni frase:
    ninguna firma es un index field, así que una firma ilegible no puede
    estropear lo que se escribe en AirVault, y apartar esa página costaría
    teclear a mano seis campos que ya están resueltos.

    El resultado va ordenado globalmente por ``log_number`` ascendente
    (libro + logpage), y dentro del mismo número por archivo/página.
    """
    entradas: List[Discrepancia] = []
    for report in reports:
        for page in report.pages:
            page.discrepancy = False
            page.discrepancy_note = ""
            page.discrepancy_fields = []
            if page.blank:
                continue
            resultado = _clasificar_pagina(page, template)
            if resultado is None:
                continue
            tipo, categoria, campos, por_correccion = resultado
            entrada = Discrepancia(
                pdf_path=str(report.pdf_path),
                page_number=page.page_number,
                matricula=_matricula(page),
                log_number=log_number(page),
                tipo=tipo,
                categoria=categoria,
                campos=campos,
                por_correccion=por_correccion,
            )
            page.discrepancy = categoria is Categoria.MISSING
            if page.discrepancy:
                page.discrepancy_note = entrada.resumen()
                page.discrepancy_fields = [
                    campo.field_id for campo in campos
                    if campo.categoria is Categoria.MISSING
                ]
            entradas.append(entrada)

    entradas.sort(key=lambda d: (
        d.log_number if d.log_number is not None else 1 << 30,
        d.pdf_path,
        d.page_number,
    ))
    logger.info(f"[Discrepancias] {len(entradas)} página(s) con discrepancias")
    return entradas


def confirmadas_para_revision(
    entradas: List[Discrepancia],
) -> List[Discrepancia]:
    """Devuelve solo ausencias confirmadas para la revisión manual.

    Las lecturas ``UNCERTAIN`` conservan su detalle en el reporte de
    discrepancias, pero no salen del flujo automático ni llevan
    ``page.discrepancy``. Solo ``MISSING`` confirma que falta una firma
    exigida y justifica separar la página.
    """
    return [
        entrada
        for entrada in entradas
        if entrada.categoria is Categoria.MISSING
    ]
