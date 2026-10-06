"""Dos periodos de las bitácoras, con usos distintos.

El **habitual** (mes actual y anterior, hasta hoy) ordena candidatos: de dos
lecturas posibles de una misma fecha, la que cae en él es la creíble. Es
estrecho a propósito, porque solo sirve para comparar.

El de **revisión** decide cuándo una fecha es tan antigua que tiene que
mirarla una persona. Fuera de enero solo admite el año de la ejecución. En
enero también admite el anterior, porque una entrega puede conservar la cola
del cierre de diciembre.

Ninguna fecha leída puede ser futura. El último día del mes actual se
admite como representación de respaldo cuando el día no se leyó; ese
respaldo nunca puede usar un mes ni un año futuro.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Optional

# Debajo de este año no hay bitácoras que indexar.
MIN_YEAR = 2000


def reference_date(today: Optional[date] = None) -> date:
    """Día contra el que se mide la ventana (hoy, salvo que se indique)."""
    return today if today is not None else date.today()


def year_is_possible(year: int, today: Optional[date] = None) -> bool:
    """Año de cuatro dígitos que una bitácora puede llevar escrito."""
    return MIN_YEAR <= year <= reference_date(today).year


def month_is_possible(
    year: int, month: int, today: Optional[date] = None
) -> bool:
    """Mes completo que una bitácora puede llevar escrito.

    Un mes es posible si ya empezó, incluso cuando su último día se usa
    como representación de respaldo y todavía no llegó.
    """
    reference = reference_date(today)
    return 1 <= month <= 12 and year_is_possible(year, today) and (
        (year, month) <= (reference.year, reference.month)
    )


def date_is_possible(
    value: date, today: Optional[date] = None, *, allow_month_end: bool = False
) -> bool:
    """Fecha leída posible, o fin de mes si se autoriza ese respaldo."""
    reference = reference_date(today)
    if not year_is_possible(value.year, reference):
        return False
    return value <= reference or (
        allow_month_end
        and value == month_end_date(value.year, value.month, reference)
    )


def month_end_date(
    year: int, month: int, today: Optional[date] = None
) -> Optional[date]:
    """Fin de mes de respaldo, sin aceptar meses ni años futuros."""
    reference = reference_date(today)
    if not month_is_possible(year, month, reference):
        return None
    return date(year, month, monthrange(year, month)[1])


def usual_start(today: Optional[date] = None) -> date:
    """Primer día del mes anterior, incluyendo diciembre al pasar a enero."""
    first = reference_date(today).replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


def review_start(today: Optional[date] = None) -> date:
    """Primer día del periodo que no necesita revisión por antigüedad.

    El año anterior solo entra durante enero, cuando todavía es normal recibir
    páginas del cierre de diciembre. El resto del año, una fecha anterior al
    año de la ejecución necesita revisión.
    """
    reference = reference_date(today)
    first_year = reference.year - (1 if reference.month == 1 else 0)
    return date(first_year, 1, 1)


def needs_review_for_age(value: date, today: Optional[date] = None) -> bool:
    """Indica si la fecha es tan antigua que la página debe revisarse."""
    return value < review_start(today)


def days_outside_usual(value: date, today: Optional[date] = None) -> int:
    """Días que separan la fecha de la ventana habitual (0 si cae dentro).

    Una fecha adelantada devuelve los días que le sobran; una atrasada,
    los que le faltan para entrar en la ventana. Sirve para ordenar
    candidatos, no para descartarlos: un libro antiguo es raro, no
    imposible.
    """
    reference = reference_date(today)
    if value > reference:
        return (value - reference).days
    return max(0, (usual_start(reference) - value).days)


def years_outside_usual(value: date, today: Optional[date] = None) -> int:
    """Anos enteros que separan la fecha de la ventana habitual.

    Es la misma distancia medida en grueso. Sirve para comparar
    candidatos sin que el borde de la ventana decida nada: un libro viejo
    entero puntua igual en todas sus paginas, y solo destaca la pagina
    que un ano mal leido manda decadas fuera.
    """
    return days_outside_usual(value, today) // 365


def is_usual(value: date, today: Optional[date] = None) -> bool:
    """Indica si la fecha cae en la ventana habitual de una ejecución."""
    return days_outside_usual(value, today) == 0
