"""Fecha deducida para las bitacoras que no la traen leida.

End Date es obligatorio en AirVault. Una bitacora sin fecha queda bloqueada
por la guarda de obligatorios, y basta con que quede una para que el batch no
se pueda cerrar: alguien tiene que abrirlo en el Web Index y teclearla a
mano, que es justo lo que este modulo existe para evitar.

Lo que si se lee casi siempre es el ``log_number``, y ese numero ordena el
libro. Sobre el se apoyan las mismas reglas que ya usa el corrector de
fechas del procesamiento (``app/validation/date_corrector.py``), aplicadas
aqui a los datos que llegan al indexado:

* dentro de un libro la fecha no retrocede al aumentar el ``log_number``,
  asi que una pagina sin fecha esta entre la de la anterior y la de la
  siguiente: se le pone la de la bitacora fechada mas cercana del libro, que
  cae dentro de ese intervalo por construccion;
* pasada la ultima bitacora fechada del libro ya no hay techo, y se usa el
  ultimo dia de ese mes, la misma convencion con la que el CSV completa un
  dia ilegible;
* sin ninguna fechada en el libro se baja al mes dominante del avion y, si
  el avion entero llego sin fechas, al de la ejecucion.

Nada de esto se aplica a una bitacora cuyo ``log_number`` no se leyo: sin
numero no hay libro ni posicion, la pagina esta bloqueada de todos modos por
ese campo obligatorio, y ponerle una fecha solo maquillaria el reporte.

La fecha deducida viaja marcada con el metodo que la produjo, para que el
reporte de revision la distinga de una leida y se pueda mirar antes de
aprobar.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.utils.date_window import date_is_possible, month_end_date

_FECHA_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")
_LOG_RE = re.compile(r"^\d{7}$")

# Como se nombra en el reporte cada regla, de la que mas evidencia tiene a
# la que menos.
METODO_MISMA_BITACORA = "misma bitacora"
METODO_ENTRE_ANCLAS = "entre bitacoras del libro"
METODO_FIN_MES_LIBRO = "fin del mes del libro"
METODO_FIN_MES_AVION = "fin del mes del avion"
METODO_FIN_MES_EJECUCION = "fin del mes de la ejecucion"

Clave = Tuple[str, int]
Ancla = Tuple[int, date]
Propuesta = Tuple[date, str]


def _clave(fila: Mapping[str, str]) -> Optional[Clave]:
    """Par ``(archivo, pagina)`` con el que se identifica una fila del CSV."""
    try:
        pagina = int(str(fila.get("page", "")).strip())
    except ValueError:
        return None
    return str(fila.get("file", "")).strip(), pagina


def _log_number(fila: Mapping[str, str]) -> Optional[int]:
    limpio = re.sub(r"\D", "", str(fila.get("log_number", "") or ""))
    return int(limpio) if _LOG_RE.fullmatch(limpio) else None


def _fecha(fila: Mapping[str, str]) -> Optional[date]:
    match = _FECHA_RE.match(str(fila.get("date", "") or "").strip())
    if not match:
        return None
    try:
        value = date(*(int(parte) for parte in match.groups()))
        # El CSV puede representar un día no leído con el fin del mes actual.
        return value if date_is_possible(value, allow_month_end=True) else None
    except ValueError:
        return None


def _matricula(fila: Mapping[str, str]) -> str:
    return str(fila.get("matricula", "") or "").strip().upper()


def _libro(log_number: int) -> Tuple[int, int]:
    """Serie y mitad del libro, la misma clave que usa el procesamiento.

    Un libro fisico son 50 paginas de un solo avion: comparten los cinco
    primeros digitos y la mitad en la que cae el logpage (00-49 o 50-99).
    """
    return log_number // 100, 0 if log_number % 100 < 50 else 1


def _fin_de_mes(dia: date) -> Optional[date]:
    return month_end_date(dia.year, dia.month)


def _del_libro(
    anclas: Sequence[Ancla], log_number: int
) -> Optional[Propuesta]:
    """Deduce la fecha por la posicion de la bitacora dentro de su libro."""
    if not anclas:
        return None
    iguales = [f for n, f in anclas if n == log_number]
    if iguales:
        # La misma bitacora entro dos veces en la ejecucion y una de las dos
        # si trajo fecha. No hay nada que deducir: es esa.
        return max(iguales), METODO_MISMA_BITACORA

    antes = [a for a in anclas if a[0] < log_number]
    despues = [a for a in anclas if a[0] > log_number]
    if antes and despues:
        izquierda, derecha = antes[-1], despues[0]
        # La fecha no retrocede dentro del libro, asi que la de esta pagina
        # esta entre las dos. Se toma la de la bitacora mas cercana, y en un
        # empate la posterior, que es la ultima que cabe en el hueco.
        cercana = (
            izquierda if log_number - izquierda[0] < derecha[0] - log_number
            else derecha
        )
        return cercana[1], METODO_ENTRE_ANCLAS
    if antes:
        # Detras de la ultima fechada no hay techo que respetar: el ultimo
        # dia de ese mes es lo mas tarde que la bitacora pudo llenarse sin
        # cambiar de mes.
        ultimo = _fin_de_mes(antes[-1][1])
        return (ultimo, METODO_FIN_MES_LIBRO) if ultimo is not None else None
    # Antes de la primera fechada: su fecha es el techo y tambien lo unico
    # que se sabe del libro.
    return despues[0][1], METODO_ENTRE_ANCLAS


def _del_mes_dominante(meses: Counter, metodo: str) -> Optional[Propuesta]:
    """Ultimo dia del mes que mas bitacoras reune, o ``None`` si no hay."""
    if not meses:
        return None
    (anio, mes), _cuantas = max(
        meses.items(), key=lambda par: (par[1], par[0])
    )
    ultimo = _fin_de_mes(date(anio, mes, 1))
    return (ultimo, metodo) if ultimo is not None else None


def fechas_inferidas(
    filas: Iterable[Mapping[str, str]],
) -> Dict[Clave, Tuple[str, str]]:
    """Propone una fecha para cada bitacora del CSV que llego sin ella.

    Args:
        filas: filas del CSV de la ejecucion completa. Cuantas mas, mejor:
            las que si traen fecha son las que fechan a las demas, y una
            ejecucion repartida en partes se deduce con el CSV entero aunque
            cada parte sea un batch distinto.

    Returns:
        ``{(archivo, pagina): (fecha, metodo)}`` en el formato del CSV
        (``YYYY/MM/dd``), solo para las filas con ``log_number`` legible y
        sin fecha propia. Las demas no salen en el diccionario.
    """
    anclas_por_libro: Dict[Tuple[int, int], List[Ancla]] = {}
    meses_por_avion: Dict[str, Counter] = {}
    meses_de_la_ejecucion: Counter = Counter()
    sin_fecha: List[Tuple[Clave, int, str]] = []

    for fila in filas:
        clave = _clave(fila)
        if clave is None:
            continue
        log_number = _log_number(fila)
        fecha = _fecha(fila)
        matricula = _matricula(fila)
        if fecha is None:
            if log_number is not None:
                sin_fecha.append((clave, log_number, matricula))
            continue
        if log_number is not None:
            anclas_por_libro.setdefault(_libro(log_number), []).append(
                (log_number, fecha)
            )
        mes = (fecha.year, fecha.month)
        if matricula:
            meses_por_avion.setdefault(matricula, Counter())[mes] += 1
        meses_de_la_ejecucion[mes] += 1

    if not sin_fecha:
        return {}
    for anclas in anclas_por_libro.values():
        anclas.sort()

    propuestas: Dict[Clave, Tuple[str, str]] = {}
    for clave, log_number, matricula in sin_fecha:
        propuesta = _del_libro(
            anclas_por_libro.get(_libro(log_number), ()), log_number
        )
        if propuesta is None and matricula:
            propuesta = _del_mes_dominante(
                meses_por_avion.get(matricula, Counter()),
                METODO_FIN_MES_AVION,
            )
        if propuesta is None:
            propuesta = _del_mes_dominante(
                meses_de_la_ejecucion, METODO_FIN_MES_EJECUCION
            )
        if propuesta is None:
            # Ni una sola bitacora fechada en toda la ejecucion. No hay de
            # donde sacar un mes, y una fecha inventada seria peor que la
            # pagina bloqueada que la guarda va a dejar.
            continue
        fecha, metodo = propuesta
        propuestas[clave] = (fecha.strftime("%Y/%m/%d"), metodo)
    return propuestas
