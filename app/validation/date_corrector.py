"""Inferencia conservadora de fechas guiada por ``log_number`` y evidencia OCR.

Las paginas de un mismo libro pueden llegar repartidas entre varios PDFs o
en un orden distinto al de la bitacora. Por eso este modulo usa el numero de
bitacora legible solo para establecer la secuencia. El valor que se propaga
debe venir de lecturas directas confiables, no del numero ni de otra
inferencia previa.

La politica de inferencia es deliberadamente asimetrica:

* mes y ano pueden inferirse entre dos anclas directas compatibles;
* en los extremos se permite una extrapolacion corta con dos anclas locales;
* una lectura mensual posicional clara puede actuar como ancla aun si su
  confianza aislada es baja;
* una lectura que contradice a las dos que la rodean no es una fecha
  discutible sino una lectura equivocada, porque la fecha no retrocede
  dentro del libro: el mes y el ano se corrigen con las anclas compatibles,
  y los dias se rehacen todos a la vez con la asignacion que no retrocede y
  que menos evidencia contradice;
* un ano posterior al de la ejecucion no existe (la pagina no se firma
  despues de escanearse) y se trata como lectura invalida; el resto de la
  ventana de ``app.utils.date_window`` solo ordena candidatos, nunca
  descarta un libro antiguo ni acerca ninguna fecha a hoy;
* el día leído solo se sustituye cuando el propio libro lo desmiente (o
  retrocede en la secuencia, o se separa más de una semana de sus vecinas
  con la casilla de las decenas sin leer), y entonces por el día más
  parecido al leído que la secuencia admite: no se cambia una fecha por
  parecer rara ni por caer lejos de hoy, únicamente por contradecir a las
  páginas de su mismo libro. El día que no se leyó se
  completa con el último que cabe en la secuencia (como mucho, el último
  del mes), porque una página sin día es una bitácora entera por indexar a
  mano aunque todo lo demás se haya leído. La política del CSV (día
  específico o fin de mes) sigue decidiendo cómo se representa la fecha.

Una inferencia conserva su procedencia en ``FieldResult`` y queda en WARNING,
nunca se presenta como una lectura OCR directa en estado OK.
"""

from __future__ import annotations

import json
import re
from calendar import monthrange
from collections import Counter
from datetime import date
from itertools import product
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from app.models.schemas import FieldResult, PageResult, Status, ValidationReport
from app.utils.date_window import (
    date_is_possible,
    reference_date,
    month_is_possible,
    month_end_date,
    review_start,
    year_is_possible,
    years_outside_usual,
)
from app.utils.postprocess import MESES, _parse_month, combine_date
from app.validation.book_corrector import (
    _BOOK_STORAGE_KEY_RE,
    _recompute_page_status,
    _recompute_summary,
    _storage_key,
)
from app.validation.grouping import group_books, log_number
from app.validation.date_review import review_date_windows
from app.validation.page_status import AUTO_INDEX_MIN_VOTES

DATE_FIELD_IDS = ("day", "month", "year")
YEAR_FIELD_ID = "year"
DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")

# Las anclas directas deben tener al menos la confianza que dispara el
# WARNING normal del OCR. Valores por debajo de esto no deben propagar errores.
MIN_DIRECT_CONFIDENCE = 0.5
# Una lectura exacta por ranuras conserva evidencia física suficiente para el
# mes aunque la confianza del carácter manuscrito quede en WARNING.
MIN_POSITIONAL_MONTH_CONFIDENCE = 0.35
# Para inferir un tramo se necesitan dos anclas del mismo componente.
MIN_ANCHORS = 2
# La extrapolacion a un extremo es mas arriesgada que un intervalo cerrado.
MAX_EDGE_LOG_SPAN = 10
# El consenso solo se activa con evidencia suficiente y una mayoría clara.
MIN_YEAR_CONSENSUS_READINGS = 3
MIN_YEAR_CONSENSUS_COUNT = 2
MIN_YEAR_CONSENSUS_RATIO = 0.60
# Un consenso de toda la ejecucion solo desempata una alternativa que el
# propio OCR ya propuso. Exigir varios libros evita que un unico libro
# antiguo haga parecer universal su ano.
MIN_RUN_YEAR_CONSENSUS_READINGS = 12
MIN_RUN_YEAR_CONSENSUS_BOOKS = 3
MIN_RUN_YEAR_CONSENSUS_RATIO = 0.90
MIN_RUN_YEAR_CORRECTION_DISTANCE = 2
# Un libro se llena en dias seguidos. Mas de una semana de separacion
# con las paginas vecinas delata una decena que no se leyo, no un salto
# real de la bitacora.
MAX_DAY_GAP = 7
# Lo que cuesta escribir en el dia un valor que no propuso ninguna lectura,
# y lo que cuesta cada dia que la fecha se aleje de lo leido. Los dos
# mantienen el resultado pegado a lo que dice la bitacora.
UNSEEN_DAY_COST = 0.5
DAY_DISTANCE_COST = 0.01

def _field(page: PageResult, field_id: str) -> Optional[FieldResult]:
    for field in page.fields:
        if field.field_id == field_id:
            return field
    return None


def _month_number(value: Optional[str]) -> Optional[int]:
    """Devuelve el mes numerico desde texto o digitos, o ``None``."""
    if not value:
        return None
    raw = re.sub(r"[^\dA-Za-z]", "", value).upper()
    if not raw:
        return None
    if not re.search(r"[A-Za-z]", raw):
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            return None
        try:
            month = int(digits)
        except ValueError:
            return None
        return month if 1 <= month <= 12 else None
    return _parse_month(raw)


def _year_reading(value: Optional[str]) -> Optional[int]:
    """Ano de cuatro digitos tal como se leyo, quepa o no en la ventana."""
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    if len(digits) == 4:
        return int(digits)
    if len(digits) == 2:
        return 2000 + int(digits)
    return None


def _year_normalize(value: Optional[str]) -> Optional[str]:
    """Normaliza un ano valido a dos digitos, sin aceptar anos de 3 digitos.

    Un ano posterior al de la ejecucion tampoco es valido: la pagina no se
    firma despues del dia en que se escanea (``app.utils.date_window``).
    """
    year = _year_reading(value)
    if year is None or not year_is_possible(year):
        return None
    return f"{year % 100:02d}"


def _day_normalize(value: Optional[str]) -> Optional[str]:
    """Normaliza un dia valido; no se usa para inferir valores."""
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    if not digits:
        return None
    try:
        day = int(digits)
    except ValueError:
        return None
    return f"{day:02d}" if 1 <= day <= 31 else None


def _format_month(month: int) -> str:
    for name, number in MESES.items():
        if number == month:
            return name
    return str(month)


def _format_component(field_id: str, value: str) -> str:
    if field_id == "month":
        return _format_month(int(value))
    if field_id == YEAR_FIELD_ID:
        return f"{int(value):02d}"
    return value


def _component_candidates(
    field: Optional[FieldResult], normalize: Normalizer,
) -> List[str]:
    """Valores canónicos: lectura elegida primero y luego alternativas."""
    if field is None:
        return []
    candidates: List[str] = []
    raws = (
        [field.value]
        if field.source in {"inferred", "book_correction"}
        else [field.value, *field.alternatives]
    )
    for raw in raws:
        value = normalize(raw)
        if value is not None and value not in candidates:
            candidates.append(value)
    return candidates


def _date_candidates(page: PageResult) -> List[Tuple[date, Tuple[str, str, str]]]:
    """Fechas calendario posibles a partir de la evidencia OCR de la página."""
    days = _component_candidates(_field(page, "day"), _day_normalize)
    month_field = _field(page, "month")
    months = _component_candidates(month_field, _month_number)
    # La búsqueda general minimiza saltos, pero eso no demuestra qué mes
    # decía una casilla ambigua. Solo un intervalo directo puede decidirla.
    if month_field is not None and not month_field.value and len(months) > 1:
        return []
    years = _component_candidates(_field(page, YEAR_FIELD_ID), _year_normalize)
    candidates: List[Tuple[date, Tuple[str, str, str]]] = []
    for day_value, month_value, year_value in product(days, months, years):
        try:
            parsed = date(
                2000 + int(year_value), int(month_value), int(day_value)
            )
        except ValueError:
            continue
        day_field = _field(page, "day")
        month_only = day_field is not None and day_field.inference_method == "month_end_policy"
        if not date_is_possible(parsed.replace(day=1) if month_only else parsed):
            continue
        candidates.append((parsed, (day_value, month_value, year_value)))
    return candidates


def _resolve_ambiguous_months(book: Sequence[PageResult]) -> int:
    """Resuelve candidatos solo si dos fechas directas dejan uno posible."""
    ordered = _ordered_pages(book)
    anchors = []
    for page in ordered:
        components = [(fid, _field(page, fid), normalize)
                      for fid, normalize in (("day", _day_normalize),
                                             ("month", _month_number),
                                             ("year", _year_normalize))]
        if not all(_is_book_anchor(page, field,
                                  normalize(field.value) if field else None, fid)
                   for fid, field, normalize in components):
            continue
        resolved = _resolved_date(page)
        if resolved is not None:
            try:
                anchors.append((log_number(page), date(*resolved)))
            except ValueError:
                continue
    changed = 0
    for page in ordered:
        month = _field(page, "month")
        if month is None or month.value or len(month.alternatives) < 2:
            continue
        number = log_number(page)
        before = [anchor for anchor in anchors if anchor[0] < number]
        after = [anchor for anchor in anchors if anchor[0] > number]
        if not before or not after:
            continue
        left, right = before[-1], after[0]
        if left[1] > right[1]:
            continue
        day, year = _field(page, "day"), _field(page, "year")
        if not all(_is_book_anchor(page, field,
                                  normalize(field.value) if field else None, fid)
                   for fid, field, normalize in (("day", day, _day_normalize),
                                                  ("year", year, _year_normalize))):
            continue
        possible = set()
        for value in month.alternatives:
            candidate = _month_number(value)
            if candidate is None:
                continue
            try:
                parsed = date(2000 + int(_year_normalize(year.value)),
                              candidate, int(_day_normalize(day.value)))
            except ValueError:
                continue
            if left[1] <= parsed <= right[1]:
                possible.add(candidate)
        if len(possible) == 1:
            changed += int(_set_inferred_component(
                page, "month", str(possible.pop()), "log_number_month_candidates",
                [left[0], right[0]],
            ))
    return changed


def _resolve_sequence_alternatives(book: Sequence[PageResult]) -> int:
    """Elige alternativas OCR solo si reducen regresiones del mismo libro.

    La búsqueda es dinámica para no explotar combinatoriamente. Cada estado
    conserva el menor costo hasta una fecha candidata. La lectura actual vale
    cero cambios; usar una alternativa cuesta una unidad por componente. La
    cercania a la ejecucion ordena soluciones, pero nunca basta para cambiar
    un libro que ya tiene una secuencia valida.
    """
    pages = sorted(
        (page for page in book if log_number(page) is not None),
        key=lambda page: (log_number(page), page.page_number),  # type: ignore[arg-type]
    )
    rows = [(page, _date_candidates(page)) for page in pages]
    rows = [(page, candidates) for page, candidates in rows if candidates]
    if len(rows) < 2:
        return 0

    def local_changes(page: PageResult, values: Tuple[str, str, str]) -> int:
        day = _field(page, "day")
        month = _field(page, "month")
        year = _field(page, YEAR_FIELD_ID)
        current = (
            _day_normalize(day.value if day else None),
            _month_number(month.value if month else None),
            _year_normalize(year.value if year else None),
        )
        normalized = (values[0], int(values[1]), values[2])
        return sum(left != right for left, right in zip(current, normalized))

    # estado: candidato actual -> (anos fuera, regresiones, cambios,
    # saltos, camino). Los anos fuera de la ventana van primero: una
    # regresion la puede causar cualquiera de los tres componentes, y
    # taparla mandando una pagina dos anos atras deja una fecha peor que
    # la que se queria arreglar (ver ``app.utils.date_window``).
    first_page, first_candidates = rows[0]
    states = {
        candidate[0]: (
            years_outside_usual(candidate[0]),
            0, local_changes(first_page, candidate[1]), 0, [candidate]
        )
        for candidate in first_candidates
    }
    for page, candidates in rows[1:]:
        next_states = {}
        for candidate in candidates:
            parsed, values = candidate
            best = None
            for previous_date, state in states.items():
                outside, regressions, changes, jumps, path = state
                score = (
                    outside + years_outside_usual(parsed),
                    regressions + int(parsed < previous_date),
                    changes + local_changes(page, values),
                    jumps + abs((parsed - previous_date).days),
                    [*path, candidate],
                )
                if best is None or score[:4] < best[:4]:
                    best = score
            existing = next_states.get(parsed)
            if best is not None and (
                existing is None or best[:4] < existing[:4]
            ):
                next_states[parsed] = best
        states = next_states
    if not states:
        return 0
    best = min(states.values(), key=lambda item: item[:4])

    current_dates = [candidates[0] for _page, candidates in rows]
    current_regressions = sum(
        right[0] < left[0]
        for left, right in zip(current_dates, current_dates[1:])
    )
    current_outside = sum(
        years_outside_usual(parsed) for parsed, _values in current_dates
    )
    if (
        current_regressions == 0
        or best[1] >= current_regressions
        or (best[0], best[1]) >= (current_outside, current_regressions)
    ):
        return 0

    corrected = 0
    for (page, _candidates), (_parsed, values) in zip(rows, best[4]):
        for field_id, normalized, formatter in (
            ("day", values[0], lambda value: str(int(value))),
            ("month", values[1], _format_component),
            (YEAR_FIELD_ID, values[2], lambda value: f"{int(value):02d}"),
        ):
            field = _field(page, field_id)
            if field is None:
                continue
            formatted = (
                _format_component("month", normalized)
                if field_id == "month" else formatter(normalized)
            )
            current = (
                _day_normalize(field.value) if field_id == "day" else
                _month_number(field.value) if field_id == "month" else
                _year_normalize(field.value)
            )
            expected = (
                str(int(normalized)) if field_id == "month" else normalized
            )
            if str(current) == expected:
                continue
            previous = field.value
            if previous and previous not in field.alternatives:
                field.alternatives.append(previous)
            field.value = formatted
            field.status = Status.WARNING
            field.source = "book_correction"
            field.inference_method = "log_number_sequence_candidate"
            field.comment = (
                f"OCR alternative selected by nondecreasing book sequence: "
                f"{previous!r} -> {formatted!r}"
            )
            corrected += 1
        _recombine(page)
    return corrected


def _correct_year_by_book_consensus(book: Sequence[PageResult]) -> int:
    """Corrige años OCR aislados usando mayoría y posición en el libro.

    Un año no adyacente al mayoritario (p. ej. 21/24 frente a 26) no puede
    ser una transición anual real y se corrige. Un año adyacente se conserva
    únicamente cuando forma el prefijo o sufijo cronológico esperado, para
    permitir libros que cruzan de diciembre a enero.
    """
    pages = sorted(
        (page for page in book if log_number(page) is not None and not page.blank),
        key=lambda page: (log_number(page), page.page_number),  # type: ignore[arg-type]
    )
    observed: List[Tuple[int, PageResult, FieldResult, str]] = []
    for index, page in enumerate(pages):
        field = _field(page, YEAR_FIELD_ID)
        value = _year_normalize(field.value if field else None)
        if field is not None and value is not None:
            observed.append((index, page, field, value))
    if len(observed) < MIN_YEAR_CONSENSUS_READINGS:
        return 0

    counts = Counter(value for _index, _page, _field_result, value in observed)
    ranked = counts.most_common()
    majority_year, majority_count = ranked[0]
    runner_count = ranked[1][1] if len(ranked) > 1 else 0
    ratio = majority_count / len(observed)
    if (
        majority_count < MIN_YEAR_CONSENSUS_COUNT
        or majority_count <= runner_count
        or ratio < MIN_YEAR_CONSENSUS_RATIO
    ):
        return 0

    majority_positions = [
        index for index, _page, _field_result, value in observed
        if value == majority_year
    ]
    first_majority = min(majority_positions)
    last_majority = max(majority_positions)
    majority_number = int(majority_year)
    corrected = 0
    for index, page, field, value in observed:
        if value == majority_year:
            continue
        delta = int(value) - majority_number
        plausible_previous_prefix = delta == -1 and index < first_majority
        plausible_next_suffix = delta == 1 and index > last_majority
        if plausible_previous_prefix or plausible_next_suffix:
            continue

        previous = field.value
        if previous and previous not in field.alternatives:
            field.alternatives.append(previous)
        field.value = majority_year
        field.confidence = round(min(0.95, 0.55 + ratio * 0.40), 3)
        field.status = Status.WARNING
        field.source = "book_correction"
        field.inference_method = "log_number_year_consensus"
        field.comment = (
            f"Year corrected by book majority ({majority_count}/"
            f"{len(observed)}): {previous!r} -> {majority_year!r}"
        )
        _recombine(page)
        corrected += 1
    return corrected


def _correct_year_outliers(book: Sequence[PageResult]) -> int:
    """Devuelve al año del libro la página que quedó fuera de él.

    Un libro son 50 páginas llenadas en pocas semanas: el año solo puede
    cambiar una vez dentro de él, de diciembre a enero. Con una mayoría clara
    de lecturas directas, un año a dos o más de ella es imposible, y uno a
    uno solo cabe en el borde que corresponde (el anterior antes del bloque
    mayoritario, el siguiente después). Se revisa también lo que pusieron
    los correctores, que no vota pero sí puede equivocarse.

    Un año que el libro leyó en dos bitácoras propias no se toca: ya no es
    una página suelta.
    """
    pages = _ordered_pages(book)
    readings: Dict[str, set] = {}
    for page in pages:
        field = _field(page, YEAR_FIELD_ID)
        value = _year_normalize(field.value if field else None)
        if (
            field is None
            or value is None
            or field.source in {"inferred", "book_correction"}
        ):
            continue
        readings.setdefault(value, set()).add(log_number(page))
    if not readings:
        return 0
    ranked = sorted(
        ((len(numbers), year) for year, numbers in readings.items()),
        reverse=True,
    )
    count, majority = ranked[0]
    runner = ranked[1][0] if len(ranked) > 1 else 0
    total = sum(len(numbers) for numbers in readings.values())
    if (
        count < MIN_YEAR_CONSENSUS_COUNT
        or count <= runner
        or count / total < MIN_YEAR_CONSENSUS_RATIO
    ):
        return 0
    positions = [
        index for index, page in enumerate(pages)
        if _page_year(page) == majority
    ]
    first, last = min(positions), max(positions)
    corrected = 0
    for index, page in enumerate(pages):
        field = _field(page, YEAR_FIELD_ID)
        value = _year_normalize(field.value if field else None)
        if field is None or value is None or value == majority:
            continue
        if len(readings.get(value, ())) >= MIN_YEAR_CONSENSUS_COUNT:
            continue
        delta = int(value) - int(majority)
        if (delta == -1 and index < first) or (delta == 1 and index > last):
            continue
        previous = field.value
        if previous and previous not in field.alternatives:
            field.alternatives.append(previous)
        field.value = majority
        field.confidence = round(min(0.95, 0.55 + count / total * 0.40), 3)
        field.status = Status.WARNING
        field.source = "book_correction"
        field.inference_method = "book_year_outlier"
        field.comment = (
            f"Year {previous!r} outside its book ({count}/{total} direct "
            f"readings of {majority!r})"
        )
        _recombine(page)
        corrected += 1
    return corrected


def _correct_year_by_run_consensus(
    books: Sequence[Sequence[PageResult]],
) -> Tuple[int, int]:
    """Desempata anos dudosos con el consenso amplio de la ejecucion.

    No acerca una lectura antigua a la fecha actual por iniciativa propia.
    Solo actua cuando muchos libros aportan el mismo ano directamente y el
    OCR de la pagina dudosa incluyo ese ano entre sus alternativas. Si no
    puede corregir una lectura aislada que esta dos anos o mas lejos del
    consenso reciente, la marca para revision y conserva el valor como
    evidencia. Dos paginas del propio libro con el mismo ano bastan para
    conservar un libro antiguo aunque sea minoritario en la ejecucion.
    """
    votes: Counter[str] = Counter()
    books_by_year: Dict[str, set[int]] = {}
    readings_by_book: Dict[int, Counter[str]] = {}
    for book_index, book in enumerate(books):
        anchors = _anchors(book, YEAR_FIELD_ID, _year_normalize)
        readings: Counter[str] = Counter()
        for page in book:
            field = _field(page, YEAR_FIELD_ID)
            value = _year_normalize(field.value if field else None)
            if (
                value is not None
                and field is not None
                and field.source not in {"inferred", "book_correction"}
            ):
                readings[value] += 1
        readings_by_book[book_index] = readings
        for _number, value, _page in anchors:
            votes[value] += 1
            books_by_year.setdefault(value, set()).add(book_index)
    if not votes:
        return 0, 0
    ranked = votes.most_common()
    majority_year, majority_count = ranked[0]
    runner_count = ranked[1][1] if len(ranked) > 1 else 0
    total = sum(votes.values())
    ratio = majority_count / total
    if (
        majority_count < MIN_RUN_YEAR_CONSENSUS_READINGS
        or len(books_by_year.get(majority_year, ()))
        < MIN_RUN_YEAR_CONSENSUS_BOOKS
        or majority_count <= runner_count
        or ratio < MIN_RUN_YEAR_CONSENSUS_RATIO
    ):
        return 0, 0

    corrected = 0
    reviewed = 0
    majority_is_recent = 2000 + int(majority_year) in {
        date.today().year,
        date.today().year - 1,
    }
    # Meses que la ejecución leyó con el año mayoritario. Una página suelta
    # con otro año en uno de esos meses es ese mismo mes con el año mal
    # leído: una entrega no mezcla agosto de hace dos años con agosto de este
    # si el libro de la página no lo respalda con otra lectura.
    run_months = set()
    for book in books:
        for page in book:
            if _page_year(page) != majority_year:
                continue
            month_field = _field(page, "month")
            month = _month_number(month_field.value if month_field else None)
            if month is not None:
                run_months.add(month)

    def adopt(field: FieldResult, page: PageResult, method: str, why: str) -> None:
        previous = field.value
        if previous and previous not in field.alternatives:
            field.alternatives.append(previous)
        field.value = majority_year
        field.confidence = round(min(0.92, 0.55 + ratio * 0.35), 3)
        field.status = Status.WARNING
        field.source = "book_correction"
        field.inference_method = method
        field.comment = (
            f"Year corrected by execution consensus "
            f"({majority_count}/{total} direct readings across "
            f"{len(books_by_year[majority_year])} books) and {why}: "
            f"{previous!r} -> {majority_year!r}"
        )
        page.date_review = False
        _recombine(page)

    for book_index, book in enumerate(books):
        for page in book:
            if page.blank:
                continue
            field = _field(page, YEAR_FIELD_ID)
            current = _year_normalize(field.value if field else None)
            if field is None or current is None or current == majority_year:
                continue
            if readings_by_book.get(book_index, Counter())[current] >= 2:
                continue
            distance = abs(int(current) - int(majority_year))
            aligned = page.alignment_quality == "ok"
            alternatives = {
                value for raw in field.alternatives
                if (value := _year_normalize(raw)) is not None
            }
            if (
                aligned
                and distance >= MIN_RUN_YEAR_CORRECTION_DISTANCE
                and majority_year in alternatives
            ):
                adopt(field, page, "run_year_consensus", "OCR alternative")
                corrected += 1
                continue
            month_field = _field(page, "month")
            month = _month_number(month_field.value if month_field else None)
            if (
                majority_is_recent
                and month in run_months
                and (
                    distance >= MIN_RUN_YEAR_CORRECTION_DISTANCE
                    or date(2000 + int(current), month, 1) < review_start()
                )
            ):
                adopt(field, page, "run_year_month_fit", "a month of the run")
                corrected += 1
                continue
            if (
                not aligned
                or distance < MIN_RUN_YEAR_CORRECTION_DISTANCE
                or not majority_is_recent
            ):
                continue
            field.status = Status.ERROR
            field.inference_method = "run_year_review"
            _append_comment(
                field,
                f"Year {field.value!r} requires review: execution consensus "
                f"is {majority_year!r} ({majority_count}/{total} direct "
                f"readings across {len(books_by_year[majority_year])} books) "
                "and OCR did not provide a safe correction",
            )
            page.date_review = True
            reviewed += 1
    return corrected, reviewed


AFTER_THE_RUN_METHODS = frozenset(
    {"year_out_of_window", "month_out_of_window", "day_out_of_window"}
)


def _is_after_the_run(field: Optional[FieldResult]) -> bool:
    """Indica si el campo se descarto por caer despues de la ejecucion."""
    return (
        field is not None
        and field.inference_method in AFTER_THE_RUN_METHODS
    )


def _discard_reading(field: FieldResult, method: str, note: str) -> None:
    """Aparta una lectura imposible sin perderla.

    El valor pasa a las alternativas y el campo queda vacio y en ERROR,
    de modo que la inferencia del libro lo trate como lo que es: una
    casilla sin leer. El motivo queda escrito para quien revise el CSV.
    """
    previous = field.value
    if previous and previous not in field.alternatives:
        field.alternatives.append(previous)
    field.value = None
    field.status = Status.ERROR
    field.inference_method = method
    _append_comment(field, note)


def _flag_readings_after_the_run(book: Sequence[PageResult]) -> int:
    """Aparta el ano y el mes leidos que caen despues de la ejecucion.

    Una pagina no se firma despues del dia en que se escanea, asi que un
    ano posterior al de la ejecucion (un '26' leido '96' o '28') y un mes
    posterior al que corre (un AGO leido OCT o DIC) son errores de lectura
    y no bitacoras adelantadas. Se apartan para que nunca anclen una
    inferencia y para que el libro los complete con lo que si leyo. Si el
    libro no puede completarlos, la pagina se queda sin fecha: el CSV sigue
    escribiendo el ultimo dia del mes cuando el dia no se resolvio, asi que
    la bitacora se indexa igual y solo va a revision si el indice no se
    puede completar. Lo que no pasa nunca es escribir la fecha imposible.

    El mes se mira con el ano ya validado. El ultimo dia del mes actual
    se admite cuando lo genero la politica de fin de mes o el respaldo.
    """
    invalid = 0
    for page in book:
        if page.blank:
            continue
        year_field = _field(page, YEAR_FIELD_ID)
        if year_field is not None:
            year_read = _year_reading(year_field.value)
            if year_read is not None and not year_is_possible(year_read):
                _discard_reading(
                    year_field, "year_out_of_window",
                    f"invalid year: {year_field.value} is later than the run",
                )
                invalid += 1
        month_field = _field(page, "month")
        year = _year_normalize(
            year_field.value if year_field is not None else None
        )
        month = _month_number(
            month_field.value if month_field is not None else None
        )
        if (
            month_field is None
            or month_field.status is Status.ERROR
            or year is None
            or month is None
            or month_is_possible(2000 + int(year), month)
        ):
            continue
        _discard_reading(
            month_field, "month_out_of_window",
            f"invalid month: {month_field.value} is later than the run",
        )
        invalid += 1
    for page in book:
        day_field = _field(page, "day")
        resolved = _resolved_date(page)
        if page.blank or day_field is None or resolved is None:
            continue
        try:
            parsed = date(*resolved)
        except ValueError:
            continue
        if not date_is_possible(
            parsed, allow_month_end=day_field.inference_method in {
                "month_end_policy", "month_end_fallback",
            },
        ):
            _discard_reading(day_field, "day_out_of_window",
                             f"Fecha futura: {parsed:%Y/%m/%d}")
            invalid += 1
    return invalid


def _is_direct_anchor(field: Optional[FieldResult], value: Optional[str]) -> bool:
    """Indica si una lectura directa confiable puede ser una ancla.

    ``log_number`` no aporta el valor de la fecha. Solo las lecturas en OK
    pueden aportar evidencia; warnings, errores y valores ya inferidos se
    conservan para auditoria, pero no propagan informacion.
    """
    comment = (field.comment or "").lower() if field is not None else ""
    alternatives = field.alternatives if field is not None else []
    unreliable_note = any(token in comment for token in (
        "fuzzy", "numeric handwritten month", "low confidence",
        "conflicts with", "regression",
    ))
    return bool(
        field is not None
        and value is not None
        and field.source not in {"inferred", "book_correction"}
        and field.status is Status.OK
        and field.confidence >= MIN_DIRECT_CONFIDENCE
        and not alternatives
        and not unreliable_note
    )


def _is_positional_month_anchor(
    field: Optional[FieldResult], value: Optional[str]
) -> bool:
    """Acepta un mes canónico reconstruido directamente desde sus casillas."""
    comment = (field.comment or "").lower() if field is not None else ""
    return bool(
        field is not None
        and value is not None
        and field.status is Status.WARNING
        and field.confidence >= MIN_POSITIONAL_MONTH_CONFIDENCE
        and field.inference_method in {"ranuras", "date_cells"}
        and field.source not in {"inferred", "book_correction"}
        and not field.alternatives
        and "fuzzy" not in comment
        and "numeric handwritten month" not in comment
        and "conflict" not in comment
    )


Anchor = Tuple[int, str, PageResult]
Normalizer = Callable[[Optional[str]], Optional[str]]


def _is_book_anchor(
    page: PageResult,
    field: Optional[FieldResult],
    value: Optional[str],
    field_id: str,
) -> bool:
    """Indica si la lectura de esta pagina puede anclar al libro."""
    return bool(
        page.alignment_quality == "ok"
        and (
            _is_direct_anchor(field, value)
            or (
                field_id == "month"
                and _is_positional_month_anchor(field, value)
            )
        )
    )


def _anchors(
    book: Sequence[PageResult], field_id: str, normalize: Normalizer
) -> List[Anchor]:
    """Obtiene lecturas confiables y las ordena por ``log_number``.

    El numero solo ordena las evidencias. El valor de cada ancla siempre sale
    del campo de fecha que fue leido y validado directamente.
    """
    anchors: List[Anchor] = []
    for page in book:
        number = log_number(page)
        field = _field(page, field_id)
        value = normalize(field.value if field else None)
        if number is None or not _is_book_anchor(page, field, value, field_id):
            continue
        anchors.append((number, value, page))  # type: ignore[arg-type]
    return sorted(anchors, key=lambda item: (item[0], item[2].page_number))


def _inferred_confidence(anchor_count: int, span: int = 0) -> float:
    """Confianza acotada para una inferencia, siempre menor que una lectura."""
    confidence = 0.64 + min(anchor_count, 5) * 0.05
    if span > 0:
        confidence -= min(0.10, span * 0.005)
    return round(min(0.90, max(0.60, confidence)), 3)


def _append_comment(field: FieldResult, comment: str) -> None:
    field.comment = f"{field.comment} | {comment}" if field.comment else comment


def _set_inferred_component(
    page: PageResult,
    field_id: str,
    value: str,
    method: str,
    anchor_numbers: Sequence[int],
) -> bool:
    """Escribe mes o ano inferido y deja su procedencia estructurada."""
    field = _field(page, field_id)
    if field is None:
        return False
    formatted = _format_component(field_id, value)
    previous = field.value
    if previous and previous != formatted and previous not in field.alternatives:
        field.alternatives.append(previous)
    field.value = formatted
    field.status = Status.WARNING
    field.confidence = _inferred_confidence(
        len(anchor_numbers),
        max(anchor_numbers) - min(anchor_numbers)
        if len(anchor_numbers) > 1 else 0,
    )
    field.source = "inferred"
    field.inference_method = method
    field.comment = (
        f"Inferred {field_id} from log_number anchors "
        f"{', '.join(str(number) for number in anchor_numbers)}: {formatted}"
    )
    return True


def _ordered_pages(book: Sequence[PageResult]) -> List[PageResult]:
    """Paginas escritas del libro en el orden en que se llenaron."""
    return sorted(
        (page for page in book
         if log_number(page) is not None and not page.blank),
        key=lambda page: (log_number(page), page.page_number),  # type: ignore[arg-type]
    )


def _page_year(page: PageResult) -> Optional[str]:
    """Ano resuelto de la pagina, si lo tiene."""
    field = _field(page, YEAR_FIELD_ID)
    return _year_normalize(field.value if field else None)


def _correct_bracketed_component(
    book: Sequence[PageResult], field_id: str, normalize: Normalizer
) -> int:
    """Corrige el mes o el ano que contradice a sus dos vecinos leidos.

    Dentro del libro la fecha no retrocede, asi que una pagina cuyas dos
    vecinas resueltas coinciden en el mismo valor no puede llevar otro: el
    suyo quedaria por debajo del anterior o por encima del siguiente. Ahi
    no hay dos lecturas discutibles, hay una lectura imposible.

    Se corrige incluso cuando la lectura intermedia trae buena confianza:
    si las dos anclas que la rodean coinciden, un valor distinto en medio
    haria retroceder la fecha por uno de los dos lados. La confianza del OCR
    no puede convertir esa secuencia imposible en una fecha valida.

    Sin esta correccion una sola lectura equivocada hace dos danos: sale
    en el CSV con una fecha a un mes de la real y, por estar en medio,
    rompe el intervalo que habria completado a las vecinas sin leer.

    El ano no pasa por aqui aunque la regla valga igual para el: dos
    vecinas que coinciden en el mismo ano mal leido son mucho mas
    frecuentes que dos meses mal leidos seguidos, y el ano ya tiene dos
    jueces mejores, la mayoria del libro y la ventana de la ejecucion.
    """
    ordered = _ordered_pages(book)
    readings = [
        (page, field, normalize(field.value) if field is not None else None)
        for page, field in (
            (page, _field(page, field_id)) for page in ordered
        )
    ]
    resolved = [
        index for index, (_page, field, value) in enumerate(readings)
        if field is not None and value is not None
    ]
    corrected = 0
    for position in range(1, len(resolved) - 1):
        page, field, value = readings[resolved[position]]
        left_page, left_field, left_value = readings[resolved[position - 1]]
        right_page, right_field, right_value = readings[resolved[position + 1]]
        if left_value != right_value or left_value == value:
            continue
        if not (
            _is_book_anchor(left_page, left_field, left_value, field_id)
            and _is_book_anchor(right_page, right_field, right_value, field_id)
        ):
            continue
        if field_id == "month" and len({
            year for year in (
                _page_year(left_page), _page_year(page), _page_year(right_page)
            ) if year is not None
        }) > 1:
            # Con anos distintos a los lados, dos meses iguales no fijan el
            # de en medio: entre JUL de un ano y JUL del siguiente cabe
            # cualquier mes.
            continue
        numbers = [log_number(left_page), log_number(right_page)]
        previous = field.value
        formatted = _format_component(field_id, left_value)  # type: ignore[arg-type]
        if previous and previous not in field.alternatives:
            field.alternatives.append(previous)
        field.value = formatted
        field.status = Status.WARNING
        field.confidence = _inferred_confidence(len(numbers))
        field.source = "book_correction"
        field.inference_method = "log_number_bracket"
        field.comment = (
            f"{field_id} corrected by the readings that surround it "
            f"({', '.join(str(number) for number in numbers)}): "
            f"{previous!r} -> {formatted!r}"
        )
        _recombine(page)
        corrected += 1
    return corrected


def _infer_between_anchors(
    book: Sequence[PageResult],
    field_id: str,
    normalize: Normalizer,
) -> Tuple[int, int]:
    """Completa o corrige dentro de un intervalo con dos anclas iguales."""
    anchors = _anchors(book, field_id, normalize)
    if len(anchors) < MIN_ANCHORS:
        return 0, 0

    filled = 0
    flagged = 0
    known_pages = sorted(
        (page for page in book if log_number(page) is not None),
        key=lambda page: (log_number(page), page.page_number),  # type: ignore[arg-type]
    )
    for page in known_pages:
        number = log_number(page)
        field = _field(page, field_id)
        if number is None or field is None or page.alignment_quality != "ok":
            continue
        before = [anchor for anchor in anchors if anchor[0] < number]
        after = [anchor for anchor in anchors if anchor[0] > number]
        if not before or not after:
            continue
        left = before[-1]
        right = after[0]
        if left[1] != right[1]:
            continue
        if (
            field_id == "month"
            and _page_year(left[2]) != _page_year(right[2])
        ):
            # El mismo mes a ambos lados no fija el intervalo si las
            # anclas pertenecen a anos distintos.
            continue

        interior = [
            anchor for anchor in anchors
            if left[0] < anchor[0] < right[0]
            and not (anchor[0] == number and anchor[2] is page)
        ]
        if any(anchor[1] != left[1] for anchor in interior):
            continue

        anchor_numbers = [left[0], *[anchor[0] for anchor in interior], right[0]]
        current = normalize(field.value)
        if current == left[1] and field.status is not Status.ERROR:
            continue
        if current is not None and field.status is not Status.ERROR:
            filled += int(_set_inferred_component(
                page, field_id, left[1],
                "log_number_interval_correction", anchor_numbers,
            ))
            continue
        filled += int(_set_inferred_component(
            page, field_id, left[1], "log_number_interval", anchor_numbers
        ))
    return filled, flagged


def _edge_component_is_impossible(
    page: PageResult,
    field_id: str,
    current: str,
    anchor: Anchor,
    target_is_before: bool,
) -> bool:
    """Indica si un valor de borde contradice la direccion del libro."""
    anchor_value = anchor[1]
    if field_id == YEAR_FIELD_ID:
        current_number = int(current)
        anchor_number = int(anchor_value)
        if abs(current_number - anchor_number) > 1:
            return True
        return (
            current_number > anchor_number
            if target_is_before else current_number < anchor_number
        )
    if field_id != "month":
        return False
    page_year = _page_year(page)
    anchor_year = _page_year(anchor[2])
    if page_year is None or anchor_year is None:
        return False
    current_date = (int(page_year), int(current))
    anchor_date = (int(anchor_year), int(anchor_value))
    return (
        current_date > anchor_date
        if target_is_before else current_date < anchor_date
    )


def _is_positional_year_support(
    field: Optional[FieldResult], value: Optional[str]
) -> bool:
    """Acepta como segunda evidencia un ano claro leido por sus casillas."""
    return bool(
        field is not None
        and value is not None
        and field.status is Status.OK
        and field.confidence >= MIN_DIRECT_CONFIDENCE
        and field.inference_method in {"ranuras", "date_cells"}
        and field.source not in {"inferred", "book_correction"}
    )


def _infer_edges(
    book: Sequence[PageResult],
    field_id: str,
    normalize: Normalizer,
) -> int:
    """Infiere un tramo corto al inicio o final con dos anclas iguales."""
    anchors = _anchors(book, field_id, normalize)
    if field_id == YEAR_FIELD_ID:
        supported: List[Anchor] = []
        for page in book:
            number = log_number(page)
            field = _field(page, field_id)
            value = normalize(field.value if field else None)
            if number is None or value is None:
                continue
            if (
                _is_book_anchor(page, field, value, field_id)
                or (
                    page.alignment_quality == "ok"
                    and _is_positional_year_support(field, value)
                )
            ):
                supported.append((number, value, page))
        anchors = sorted(
            supported, key=lambda item: (item[0], item[2].page_number)
        )
    if len(anchors) < MIN_ANCHORS:
        return 0
    direct_anchors = [
        anchor for anchor in anchors
        if _is_direct_anchor(_field(anchor[2], field_id), anchor[1])
    ]
    if len(direct_anchors) >= MIN_ANCHORS:
        # Una lectura posicional en WARNING ayuda dentro de un intervalo,
        # pero no debe ocultar dos lecturas directas iguales solo por estar
        # en el borde que precisamente se esta comprobando.
        anchors = direct_anchors
    pages = sorted(
        (page for page in book if log_number(page) is not None),
        key=lambda page: (log_number(page), page.page_number),  # type: ignore[arg-type]
    )
    filled = 0

    edge_specs: List[Tuple[List[Anchor], List[PageResult]]] = []
    first_two = anchors[:2]
    last_two = anchors[-2:]
    if first_two[0][1] == first_two[1][1]:
        edge_specs.append((first_two, [
            page for page in pages
            if log_number(page) < first_two[0][0]  # type: ignore[operator]
        ]))
    if last_two[0][1] == last_two[1][1]:
        edge_specs.append((last_two, [
            page for page in pages
            if log_number(page) > last_two[-1][0]  # type: ignore[operator]
        ]))

    for edge_anchors, targets in edge_specs:
        if not edge_anchors:
            continue
        value = edge_anchors[0][1]
        for page in targets:
            number = log_number(page)
            field = _field(page, field_id)
            if number is None or field is None \
                    or page.alignment_quality != "ok":
                continue
            distance = (
                edge_anchors[0][0] - number
                if number < edge_anchors[0][0]
                else number - edge_anchors[-1][0]
            )
            if distance > MAX_EDGE_LOG_SPAN:
                continue
            current = normalize(field.value)
            if current is not None and field.status is not Status.ERROR:
                target_is_before = number < edge_anchors[0][0]
                anchor = (
                    edge_anchors[0] if target_is_before else edge_anchors[-1]
                )
                if not _edge_component_is_impossible(
                    page, field_id, str(current), anchor, target_is_before
                ):
                    continue
            filled += int(_set_inferred_component(
                page, field_id, value, "log_number_edge_correction",
                [anchor[0] for anchor in edge_anchors],
            ))
    return filled


def _fill_from_book_consensus(
    book: Sequence[PageResult],
    field_id: str,
    normalize: Normalizer,
    anchors: Sequence[Anchor],
) -> int:
    """Completa un componente cuando todo el libro coincide en su valor.

    Un libro es un solo avión llenado de corrido. Si todas las lecturas
    confiables del libro dicen el mismo mes (o el mismo año), no queda otra
    opción posible para las páginas que no se dejaron leer: no hay tramo que
    interpolar ni extremo que extrapolar, hay un único valor. Antes esas
    páginas se quedaban sin fecha porque la inferencia pedía una ancla a cada
    lado, y una página sin fecha es una bitácora que hay que indexar a mano.

    ``anchors`` se calcula **antes** de interpolar: la interpolación marca
    como "en conflicto" las lecturas que contradicen un intervalo, y esas
    lecturas dejan de ser anclas. Recalcularlas aquí haría unánime un libro
    que no lo era y llenaría el hueco con el mes equivocado.

    La página queda en WARNING y con la procedencia escrita, igual que
    cualquier otra inferencia: nunca se presenta como lectura directa.
    """
    if len(anchors) < MIN_ANCHORS:
        return 0
    values = {anchor[1] for anchor in anchors}
    if len(values) != 1:
        return 0
    value = values.pop()
    numbers = [anchor[0] for anchor in anchors]
    filled = 0
    for page in book:
        if page.blank:
            continue
        field = _field(page, field_id)
        if field is None or normalize(field.value) is not None:
            continue
        filled += int(_set_inferred_component(
            page, field_id, value, "book_consensus", numbers
        ))
    return filled


def _resolved_date(page: PageResult) -> Optional[Tuple[int, int, int]]:
    """(año, mes, día) de la página cuando los tres están resueltos."""
    day = _day_normalize(_field(page, "day").value if _field(page, "day") else None)
    month = _month_number(
        _field(page, "month").value if _field(page, "month") else None
    )
    year = _year_normalize(
        _field(page, YEAR_FIELD_ID).value
        if _field(page, YEAR_FIELD_ID) else None
    )
    if day is None or month is None or year is None:
        return None
    return (2000 + int(year), month, int(day))


def _neighbour_day(
    dates: Sequence[Optional[Tuple[int, int, int]]],
    index: int,
    step: int,
    month: Tuple[int, int],
) -> Optional[int]:
    """Día de la página resuelta más cercana que cae en el mismo mes."""
    position = index + step
    while 0 <= position < len(dates):
        neighbour = dates[position]
        if neighbour is not None:
            return neighbour[2] if neighbour[:2] == month else None
        position += step
    return None


def _day_readings(
    book: Sequence[PageResult],
) -> List[Tuple[PageResult, FieldResult, Tuple[int, int, int]]]:
    """Paginas del libro con la fecha resuelta y un dia escrito."""
    readings = []
    for page in _ordered_pages(book):
        field = _field(page, "day")
        resolved = _resolved_date(page)
        if field is not None and resolved is not None:
            readings.append((page, field, resolved))
    return readings


def _day_candidates(field: FieldResult, current: int) -> List[int]:
    """Dias que la propia lectura permite ademas del elegido.

    Son las alternativas que dejo el OCR y, cuando el dia salio de una
    sola cifra, ese mismo digito con su decena: la casilla de las decenas
    se queda vacia a menudo y un 18 llega como 8.
    """
    candidates: List[int] = []
    for raw in field.alternatives:
        value = _day_normalize(raw)
        if value is None or int(value) in candidates:
            continue
        if int(value) < 10 <= current:
            # Un dia de una cifra frente a uno de dos no es otra lectura,
            # es la misma sin su decena: nunca la sustituye.
            continue
        candidates.append(int(value))
    if current < 10:
        candidates.extend(current + tens for tens in (10, 20, 30))
    return [value for value in candidates if value != current]


def _set_repaired_day(
    page: PageResult, field: FieldResult, day: int, method: str,
    anchor_count: int, comment: str,
) -> None:
    """Escribe un dia rehecho por el libro y guarda el que se leyo."""
    previous = field.value
    if previous and previous not in field.alternatives:
        field.alternatives.append(previous)
    field.value = f"{day:02d}"
    field.status = Status.WARNING
    field.confidence = _inferred_confidence(anchor_count)
    field.source = "book_correction"
    field.inference_method = method
    field.comment = comment
    _recombine(page)


def _day_strength(field: FieldResult, day: int) -> float:
    """Fuerza de la lectura de un dia, que es lo que cuesta cambiarla.

    Es la confianza del reconocedor, y un punto entero mas cuando ademas
    es una lectura directa de dos cifras. El dia de una sola cifra no se
    lleva ese punto por seguro que este: su casilla de las decenas no se
    leyo, y es justo lo que se esta poniendo en duda.
    """
    directa = day >= 10 and _is_direct_anchor(
        field, _day_normalize(field.value)
    )
    return field.confidence + (1.0 if directa else 0.0)


def _repair_days_by_sequence(book: Sequence[PageResult]) -> int:
    """Rehace los dias con los que el libro se contradice a si mismo.

    El libro se llena de corrido: la fecha no retrocede al aumentar el
    numero de bitacora. Un dia que rompe esa regla no es una fecha
    discutible, es una lectura equivocada, y es ademas el error mas comun
    de la banda manuscrita (la casilla de las decenas se lee vacia y el 18
    sale como 8, o el 17 como 7).

    No se decide pagina por pagina, porque un dia mal leido tambien hace
    que parezcan mal los dos que lo rodean. Se busca de una vez la
    asignacion de dias que no retrocede y que cuesta menos evidencia:
    conservar el dia leido vale cero, y cambiarlo cuesta la fuerza de esa
    lectura, algo mas si el dia nuevo no lo propuso nadie, y un poco mas
    cuanto mas se aleje de lo leido. Asi gana la explicacion que
    contradice a menos paginas y que menos mueve la fecha: con dos
    lecturas firmes a los lados cae la de en medio, y con una lectura
    firme contra dos dudosas caen las dudosas.

    Todo lo que cambia queda en WARNING, con la lectura anterior guardada
    como alternativa y el motivo escrito.
    """
    readings = _day_readings(book)
    if len(readings) < 2:
        return 0

    rows = []
    for page, field, (year, month, current) in readings:
        strength = _day_strength(field, current)
        evidence = set(_day_candidates(field, current))
        options = []
        for day in range(1, monthrange(year, month)[1] + 1):
            if day == current:
                cost = 0.0
            else:
                cost = strength + DAY_DISTANCE_COST * abs(day - current)
                if day not in evidence:
                    cost += UNSEEN_DAY_COST
            options.append(((year, month, day), cost))
        rows.append((page, field, current, options))

    # Programacion dinamica sobre las fechas: cada estado guarda el costo
    # minimo con el que se llega a esa fecha y el estado del que viene.
    table: List[List[Tuple[Tuple[int, int, int], float, Optional[int]]]] = []
    for _page, _field_result, _current, options in rows:
        row: List[Tuple[Tuple[int, int, int], float, Optional[int]]] = []
        if table:
            for value, cost in options:
                best: Optional[Tuple[float, int]] = None
                for position, (previous, total, _back) in enumerate(table[-1]):
                    if previous <= value and (best is None or total < best[0]):
                        best = (total, position)
                if best is not None:
                    row.append((value, cost + best[0], best[1]))
        if not row:
            # Ningun dia de esta pagina cabe detras de la anterior: lo que
            # no cuadra es el mes. El libro se parte y se sigue desde aqui.
            row = [(value, cost, None) for value, cost in options]
        table.append(row)

    chosen = [0] * len(rows)
    position = min(range(len(table[-1])), key=lambda i: table[-1][i][1])
    for index in range(len(rows) - 1, -1, -1):
        value, _total, back = table[index][position]
        chosen[index] = value[2]
        if index == 0:
            break
        position = back if back is not None else min(
            range(len(table[index - 1])), key=lambda i: table[index - 1][i][1]
        )

    repaired = 0
    for (page, field, current, _options), day in zip(rows, chosen):
        if day == current:
            continue
        _set_repaired_day(
            page, field, day, "log_number_day_sequence", 2,
            f"Day {field.value!r} does not fit the book sequence; "
            f"day that costs the least evidence: {day:02d}",
        )
        repaired += 1
    return repaired


def _complete_lost_tens_digit(book: Sequence[PageResult]) -> int:
    """Devuelve su decena al dia de una cifra que quedo lejos del libro.

    La casilla de las decenas es la que mas se pierde, y un 18 leido 8 no
    retrocede en la secuencia cuando abre el tramo: cabe antes que todo lo
    demas y ninguna regla lo delata. Lo que lo delata es la distancia: un
    libro se llena en dias seguidos, asi que una pagina a mas de una
    semana de sus vecinas, con la decena puesta, vuelve justo al lado de
    ellas.

    Solo se completa cuando la decena acerca la fecha y cabe en el hueco
    que dejan las vecinas. Un libro escrito de verdad a principios de mes
    tiene vecinas de una cifra, asi que no entra aqui.
    """
    readings = _day_readings(book)
    completed = 0
    for index, (page, field, resolved) in enumerate(readings):
        year, month, current = resolved
        if current >= 10 or field.source in {"inferred", "book_correction"}:
            continue
        before = readings[index - 1][2] if index else None
        after = (
            readings[index + 1][2] if index + 1 < len(readings) else None
        )
        same_month = [
            neighbour for neighbour in (before, after)
            if neighbour is not None and neighbour[:2] == (year, month)
        ]
        if not same_month:
            continue
        distance = min(abs(current - day) for _y, _m, day in same_month)
        if distance <= MAX_DAY_GAP:
            continue
        low = before[2] if before is not None and before in same_month else 1
        high = (
            after[2] if after is not None and after in same_month
            else monthrange(year, month)[1]
        )
        fitting = [
            value for value in (current + tens for tens in (10, 20, 30))
            if low <= value <= high
            and min(abs(value - day) for _y, _m, day in same_month) < distance
        ]
        if not fitting:
            continue
        chosen = min(fitting, key=lambda value: min(
            abs(value - day) for _y, _m, day in same_month
        ))
        _set_repaired_day(
            page, field, chosen, "lost_tens_digit", len(same_month),
            f"Day {field.value!r} sits {distance} days away from the book; "
            f"tens digit restored: {chosen:02d}",
        )
        completed += 1
    return completed


def _fill_days_to_month_end(book: Sequence[PageResult]) -> int:
    """Completa el día ilegible con el último que cabe en la secuencia.

    Una página con mes y año resueltos pero sin día se quedaba sin fecha, y
    con ella la bitácora entera había que indexarla a mano aunque todo lo
    demás se hubiera leído. Como el libro se llena de corrido, ese día no
    puede ser anterior al de la página previa ni posterior al de la
    siguiente: se escribe el último día que cabe en ese hueco y, si no hay
    página posterior del mismo mes, el último del mes.

    También recoge el día que se apartó por caer después de la ejecución.
    Una página no se firma en el futuro, así que ese número está mal leído,
    pero el resto de la fecha suele estar bien: el libro lo sustituye por el
    día que sí cabe y la bitácora se indexa sola. La lectura apartada queda
    en las alternativas y el motivo en el comentario, así que nada se pierde.

    El día queda en WARNING, con su procedencia y su comentario, así que en
    el CSV se distingue de un día leído de la casilla.
    """
    ordered = sorted(
        (page for page in book if not page.blank),
        key=lambda page: (
            log_number(page) if log_number(page) is not None else 1 << 30,
            page.page_number,
        ),
    )
    dates = [_resolved_date(page) for page in ordered]
    filled = 0
    for index, page in enumerate(ordered):
        field = _field(page, "day")
        if field is None or _day_normalize(field.value) is not None:
            continue
        month = _month_number(
            _field(page, "month").value if _field(page, "month") else None
        )
        year = _year_normalize(
            _field(page, YEAR_FIELD_ID).value
            if _field(page, YEAR_FIELD_ID) else None
        )
        if month is None or year is None:
            continue
        full_year = 2000 + int(year)
        last_date = month_end_date(full_year, month)
        if last_date is None:
            continue
        last_day = last_date.day
        same_month = (full_year, month)
        after = _neighbour_day(dates, index, 1, same_month)
        before = _neighbour_day(dates, index, -1, same_month)
        day = min(last_day, after if after is not None else last_day)
        if before is not None:
            day = max(day, before)
        # La página previa puede empujar el día por encima del tope: un día
        # posterior a la ejecución volvería a apartarse y la bitácora daría
        # la vuelta entera para nada.
        day = min(day, last_day)
        previous = field.value
        if previous and previous not in field.alternatives:
            field.alternatives.append(previous)
        # Un día que nadie leyó porque la ejecución va a fin de mes no es un
        # día que no se pudo leer, y el CSV no debe decir lo mismo de los
        # dos: quien revisa buscaría un problema donde no lo hay.
        por_politica = field.inference_method == "month_end_policy"
        apartado = field.inference_method in AFTER_THE_RUN_METHODS
        field.value = f"{day:02d}"
        field.status = Status.WARNING
        field.confidence = _inferred_confidence(1)
        field.source = "csv_date_policy" if por_politica else "inferred"
        field.inference_method = (
            "month_end_policy" if por_politica else "month_end_fallback"
        )
        if apartado:
            # El comentario del descarte ya dice que la lectura caía después
            # de la ejecución: se le añade con qué se sustituyó, y así el CSV
            # cuenta la historia completa en una sola celda.
            _append_comment(
                field,
                f"day replaced by the book sequence: {day:02d}",
            )
        else:
            field.comment = (
                f"Día no leído (ejecución a fin de mes); "
                f"último día del libro que cabe: {day:02d}"
                if por_politica else
                f"Day not read; last day that fits the book sequence: {day:02d}"
            )
        dates[index] = (full_year, month, day)
        filled += 1
    return filled


def _recombine(page: PageResult) -> None:
    """Combina la fecha solo si dia, mes y ano son validos."""
    day = _field(page, "day")
    month = _field(page, "month")
    year = _field(page, YEAR_FIELD_ID)
    if not (day and month and year):
        page.date = None
        return
    if any(part.status is Status.ERROR for part in (day, month, year)):
        page.date = None
        return

    normalized_day = _day_normalize(day.value)
    normalized_month = _month_number(month.value)
    normalized_year = _year_normalize(year.value)
    if normalized_day is None or normalized_month is None \
            or normalized_year is None:
        page.date = None
        return

    combined, _note = combine_date(
        normalized_day,
        _format_month(normalized_month),
        normalized_year,
    )
    page.date = combined if DATE_RE.fullmatch(combined or "") else None


def _check_regressions(book: Sequence[PageResult]) -> int:
    """Marca regresiones usando exclusivamente el orden de ``log_number``."""
    previous: Optional[Tuple[int, int, int]] = None
    regressions = 0
    pages = sorted(
        (page for page in book if log_number(page) is not None),
        key=lambda page: (log_number(page), page.page_number),  # type: ignore[arg-type]
    )
    for page in pages:
        if not page.date or not DATE_RE.fullmatch(page.date):
            continue
        current = tuple(int(part) for part in page.date.split("/"))
        if previous is not None and current < previous:
            year = _field(page, YEAR_FIELD_ID)
            if year is not None and year.status is not Status.ERROR:
                year.status = Status.WARNING
                year.inference_method = "log_number_regression"
                _append_comment(year, "Date regression by log_number")
                regressions += 1
        previous = current
    return regressions


def _flag_unresolved(book: Sequence[PageResult]) -> int:
    """Marca los componentes que siguen sin resolverse tras la inferencia."""
    unresolved = 0
    for page in book:
        if page.blank:
            continue
        _recombine(page)
        if page.date is not None:
            continue
        unresolved += 1

        day = _field(page, "day")
        month = _field(page, "month")
        year = _field(page, YEAR_FIELD_ID)
        if day is not None and _day_normalize(day.value) is None:
            day.status = Status.WARNING
            if not _is_after_the_run(day):
                day.inference_method = "date_incomplete"
                day.comment = "Day unresolved; date left incomplete"
        # Lo que se aparto por caer despues de la ejecucion ya explico por
        # que no vale; quien revisa el CSV necesita ese motivo y no un "no
        # se pudo resolver" que parece una casilla ilegible.
        if month is not None and _month_number(month.value) is None:
            month.status = Status.WARNING
            if not _is_after_the_run(month):
                month.inference_method = "date_unresolved"
                month.comment = "Month unresolved after log_number inference"
        if year is not None and _year_normalize(year.value) is None:
            year.status = Status.WARNING
            if not _is_after_the_run(year):
                year.inference_method = "date_unresolved"
                year.comment = "Year unresolved after log_number inference"
        if (
            day is not None
            and month is not None
            and year is not None
            and _day_normalize(day.value) is not None
            and _month_number(month.value) is not None
            and _year_normalize(year.value) is not None
        ):
            year.status = Status.WARNING
            year.inference_method = "invalid_calendar_date"
            year.comment = "Date is not a valid calendar date"
    return unresolved


# ── Registro de fechas por libro, persistente entre ejecuciones ─────────
#
# Un libro puede llegar repartido en varias ejecuciones: las primeras
# páginas hoy y el resto la semana que viene. La inferencia de arriba solo
# ve las páginas de la ejecución actual, así que un libro cuyas fechas
# legibles se quedaron en la entrega anterior vuelve a empezar sin anclas.
# El registro guarda de cada libro la primera y la última fecha confirmadas
# por lectura directa, y nada más: dos pares "logpage: fecha" por libro
# ocupan unas decenas de bytes, de modo que el archivo sigue siendo del
# tamaño de una lista y no de un historial.
#
# Con las dos anclas basta para resolver el tramo que hay entre ellas: la
# fecha no retrocede dentro del libro, así que toda página intermedia cae
# entre las dos. Si ambas comparten mes (o al menos año), no queda otro
# valor posible para las páginas intermedias que no se dejaron leer. Fuera
# de ese tramo no se infiere nada: una página posterior a la última ancla
# solo tiene garantizado que su fecha no es anterior, y eso no fija el mes.
#
# El registro se pone al día en los dos sentidos. Crece cuando aparecen
# páginas nuevas del mismo libro, y se rehace cuando lo que se lee lo
# contradice con respaldo suficiente: una entrada equivocada que nadie
# corrige envenena la inferencia de todas las ejecuciones siguientes.
BOOK_DATES_FILENAME = "book_fechas.json"

# Una entrada del registro es "logpage": "AAAA-MM-DD".
_REGISTRY_LOGPAGE_RE = re.compile(r"^\d{2}$")
_REGISTRY_DATE_RE = re.compile(r"^(20\d{2})-(\d{2})-(\d{2})$")
# Un libro nunca aporta más de dos anclas al registro: sus extremos.
_MAX_REGISTRY_ANCHORS = 2
# Lecturas directas que debe traer una ejecución para corregir una entrada
# que la contradice. Es el respaldo de dos páginas independientes que ya
# exige el registro de matrículas para dar por buena una lectura.
MIN_REGISTRY_OVERRIDE_READINGS = AUTO_INDEX_MIN_VOTES

RegistryAnchor = Tuple[int, date]


def _registry_anchor(
    key: str, logpage_text: str, iso: object
) -> Optional[RegistryAnchor]:
    """Convierte una entrada del archivo en ancla, o la descarta."""
    if not _REGISTRY_LOGPAGE_RE.fullmatch(logpage_text):
        return None
    logpage = int(logpage_text)
    if ("A" if logpage < 50 else "B") != key[5]:
        return None
    matched = _REGISTRY_DATE_RE.fullmatch(iso) if isinstance(iso, str) else None
    if matched is None:
        return None
    try:
        parsed = date(
            int(matched.group(1)), int(matched.group(2)),
            int(matched.group(3)),
        )
    except ValueError:
        return None
    if not date_is_possible(parsed):
        # Una fecha adelantada guardada en otra ejecucion envenenaria la
        # inferencia de todas las siguientes.
        return None
    return (logpage, parsed)


def _load_book_dates(path: Path) -> Dict[str, List[RegistryAnchor]]:
    """Lee el registro; un archivo dañado no detiene el procesamiento."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"No se pudo leer el registro de fechas {path}: {exc}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"Registro de fechas inválido: {path}")
        return {}
    stored: Dict[str, List[RegistryAnchor]] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not _BOOK_STORAGE_KEY_RE.fullmatch(key):
            continue
        if (
            not isinstance(value, dict)
            or not 1 <= len(value) <= _MAX_REGISTRY_ANCHORS
        ):
            continue
        anchors: List[RegistryAnchor] = []
        for logpage_text, iso in value.items():
            anchor = (
                _registry_anchor(key, logpage_text, iso)
                if isinstance(logpage_text, str) else None
            )
            if anchor is None:
                anchors = []
                break
            anchors.append(anchor)
        if not anchors:
            continue
        anchors.sort()
        if len(anchors) == _MAX_REGISTRY_ANCHORS and anchors[0][1] > anchors[1][1]:
            # La fecha no retrocede dentro de un libro: el par no es de fiar.
            logger.warning(f"Registro de fechas: se ignora {key}, retrocede")
            continue
        stored[key] = anchors
    return stored


def _save_book_dates(
    path: Path, stored: Dict[str, List[RegistryAnchor]]
) -> None:
    """Escribe solo clave y extremos en JSON compacto y atómico."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            key: {
                f"{logpage:02d}": value.isoformat()
                for logpage, value in sorted(anchors)
            }
            for key, anchors in sorted(stored.items())
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(f"{payload}\n", encoding="utf-8")
    temporary.replace(path)


def _confirmed_date(page: PageResult) -> Optional[date]:
    """Fecha de la página cuando sus tres componentes son lectura directa.

    Al registro solo entra lo que se leyó y se validó en la página. Una
    fecha inferida no puede guardarse como ancla: sería la propia
    inferencia dándose la razón en la ejecución siguiente.
    """
    if page.blank or page.alignment_quality != "ok":
        return None
    day_field = _field(page, "day")
    month_field = _field(page, "month")
    year_field = _field(page, YEAR_FIELD_ID)
    day = _day_normalize(day_field.value if day_field else None)
    month = _month_number(month_field.value if month_field else None)
    year = _year_normalize(year_field.value if year_field else None)
    if not (
        _is_direct_anchor(day_field, day)
        and _is_direct_anchor(
            month_field, f"{month:02d}" if month is not None else None
        )
        and _is_direct_anchor(year_field, year)
    ):
        return None
    try:
        parsed = date(2000 + int(year), int(month), int(day))  # type: ignore[arg-type]
        return parsed if date_is_possible(parsed) else None
    except ValueError:
        return None


def _registry_numbers(key: str, anchors: Sequence[RegistryAnchor]) -> List[int]:
    """log_number completo de cada ancla guardada."""
    return [int(f"{key[:5]}{logpage:02d}") for logpage, _value in anchors]


def _fill_component_from_registry(
    page: PageResult,
    field_id: str,
    value: str,
    key: str,
    anchors: Sequence[RegistryAnchor],
) -> bool:
    """Escribe el componente que el registro deja sin alternativa."""
    field = _field(page, field_id)
    if field is None:
        return False
    if not _set_inferred_component(
        page, field_id, value, "book_dates_registry",
        _registry_numbers(key, anchors),
    ):
        return False
    span = " a ".join(anchor[1].isoformat() for anchor in anchors)
    _append_comment(field, f"Registro del libro {key}: {span}")
    return True


def _fill_from_registry(
    book: Sequence[PageResult], key: str, anchors: Sequence[RegistryAnchor]
) -> int:
    """Completa mes y año del tramo que las anclas guardadas encierran."""
    if len(anchors) < _MAX_REGISTRY_ANCHORS:
        return 0
    (first_page, first_date), (last_page, last_date) = anchors[0], anchors[-1]
    in_span = [
        page for page in book
        if (number := log_number(page)) is not None
        and first_page <= number % 100 <= last_page
    ]
    for page in in_span:
        confirmed = _confirmed_date(page)
        if confirmed is not None and not first_date <= confirmed <= last_date:
            # La ejecución actual contradice lo guardado. Ni se corrige el
            # registro por iniciativa propia ni se usa para inferir: el
            # conflicto lo resuelve quien revise.
            logger.warning(
                f"Registro del libro {key} ({first_date.isoformat()} a "
                f"{last_date.isoformat()}) contra la lectura "
                f"{confirmed.isoformat()}: no se usa en esta ejecución"
            )
            return 0
    same_year = first_date.year == last_date.year
    same_month = same_year and first_date.month == last_date.month
    if not same_year:
        return 0
    filled = 0
    for page in in_span:
        if page.blank:
            continue
        year_field = _field(page, YEAR_FIELD_ID)
        if year_field is not None and _year_normalize(year_field.value) is None:
            filled += int(_fill_component_from_registry(
                page, YEAR_FIELD_ID, f"{first_date.year % 100:02d}",
                key, anchors,
            ))
        month_field = _field(page, "month")
        if (
            same_month
            and month_field is not None
            and _month_number(month_field.value) is None
        ):
            filled += int(_fill_component_from_registry(
                page, "month", str(first_date.month), key, anchors,
            ))
    return filled


def _extremes(anchors: Sequence[RegistryAnchor]) -> List[RegistryAnchor]:
    """Primera y última ancla; una sola se guarda tal cual."""
    ordered = sorted(anchors)
    return [ordered[0], ordered[-1]] if len(ordered) > 1 else [ordered[0]]


def _describe_anchors(anchors: Sequence[RegistryAnchor]) -> str:
    """Anclas en una línea, para el log de una corrección."""
    return ", ".join(
        f"{logpage:02d}={value.isoformat()}"
        for logpage, value in sorted(anchors)
    ) or "nada"


def _merge_registry_anchors(
    key: str,
    previous: Sequence[RegistryAnchor],
    observed: Sequence[RegistryAnchor],
    origen: str = "lecturas directas de esta ejecución",
) -> Tuple[Optional[List[RegistryAnchor]], str]:
    """Concilia lo guardado con lo que se acaba de leer del libro.

    Devuelve las anclas que quedan guardadas y qué se hizo con ellas:
    ``"nuevo"``, ``"ampliado"``, ``"corregido"``, o ``""`` cuando nada
    cambia.

    Mientras lo guardado y lo leído se sostengan a la vez, la entrada solo
    crece hacia los extremos. Si se contradicen (la misma página con otra
    fecha, o una fecha que obligaría al libro a retroceder) uno de los dos
    describe un libro que no existe, y dejarlo estar significa seguir
    infiriendo desde un dato falso en todas las ejecuciones que vengan. Gana
    entonces la ejecución actual, siempre que traiga por lo menos
    ``MIN_REGISTRY_OVERRIDE_READINGS`` lecturas directas del libro
    coherentes entre sí: son páginas que se acaban de leer y de validar,
    mientras que lo guardado pudo salir de una lectura equivocada que nadie
    volvió a mirar. La entrada se rehace con esas lecturas y el cambio queda
    en el log.

    Con una sola lectura en contra no se toca nada: un OCR suelto no basta
    para borrar lo que ya estaba, y el conflicto queda para revisión.

    ``origen`` solo nombra en el log de dónde salieron las anclas nuevas.
    La misma conciliación la usa :mod:`app.validation.book_memory` con las
    fechas que AirVault ya tiene indexadas, y un aviso que las llamara
    lecturas de esta ejecución mandaría a buscar el error donde no está.
    """
    merged: Dict[int, date] = dict(previous)
    conflicts: List[str] = []
    for logpage, value in observed:
        known = merged.get(logpage)
        if known is not None and known != value:
            conflicts.append(
                f"la página {logpage:02d} pasa de {known.isoformat()} a "
                f"{value.isoformat()}"
            )
        merged[logpage] = value
    ordered = sorted(merged.items())
    if any(
        later < earlier
        for (_a, earlier), (_b, later) in zip(ordered, ordered[1:])
    ):
        conflicts.append("la fecha del libro retrocedería")
    if not conflicts:
        anchors = _extremes(ordered)
        if list(previous) == anchors:
            return anchors, ""
        return anchors, "ampliado" if previous else "nuevo"
    if len(observed) < MIN_REGISTRY_OVERRIDE_READINGS:
        logger.warning(
            f"Registro del libro {key}: {'; '.join(conflicts)}. Una lectura "
            f"suelta no reemplaza lo guardado; queda para revisión"
        )
        return None, ""
    corrected = _extremes(observed)
    logger.warning(
        f"Registro del libro {key}: {'; '.join(conflicts)}. Mandan las "
        f"{len(observed)} {origen}: "
        f"{_describe_anchors(previous)} pasa a "
        f"{_describe_anchors(corrected)}"
    )
    return corrected, "corregido"


def learn_book_dates(reports: List[ValidationReport], path: Path) -> int:
    """Pone al día los extremos de fecha confirmados de cada libro.

    Solo aprende de lecturas directas en OK, con la página bien alineada y
    el ``log_number`` legible. El archivo guarda por libro un par de
    entradas como ``"23159B":{"52":"2025-05-14","97":"2025-06-02"}``: ni
    páginas, ni imágenes, ni historial de ejecuciones.

    Una entrada no solo crece: si lo que se acaba de leer la contradice y
    hay respaldo suficiente, se rehace con las lecturas nuevas. Las entradas
    imposibles o dañadas del archivo se descartan al leerlo y no vuelven a
    escribirse.

    Returns:
        Cuántos libros dejaron una entrada nueva, ampliada o corregida.
    """
    path = Path(path)
    stored = _load_book_dates(path)
    counters = {"nuevo": 0, "ampliado": 0, "corregido": 0}
    for book in group_books(reports):
        key = _storage_key(book)
        if key is None:
            continue
        observed: List[RegistryAnchor] = []
        for page in book:
            number = log_number(page)
            confirmed = _confirmed_date(page) if number is not None else None
            if number is None or confirmed is None:
                continue
            observed.append((number % 100, confirmed))
        if not observed:
            continue
        observed.sort()
        if any(
            later < earlier
            for (_a, earlier), (_b, later) in zip(observed, observed[1:])
        ):
            logger.warning(
                f"Libro {key}: las fechas leídas retroceden, no se aprenden"
            )
            continue
        previous = stored.get(key, [])
        merged, action = _merge_registry_anchors(key, previous, observed)
        if merged is None or not action:
            continue
        stored[key] = merged
        counters[action] += 1
    learned = sum(counters.values())
    if learned:
        try:
            _save_book_dates(path, stored)
        except OSError as exc:
            logger.warning(
                f"No se pudo guardar el registro de fechas {path}: {exc}"
            )
            return 0
        logger.info(
            f"Registro de fechas: {counters['nuevo']} libro(s) nuevo(s), "
            f"{counters['ampliado']} ampliado(s), "
            f"{counters['corregido']} corregido(s), "
            f"{len(stored)} total, {path.stat().st_size} bytes"
        )
    return learned


def correct_dates_by_book(
    reports: List[ValidationReport],
    book_dates_path: Optional[Path] = None,
) -> Dict[str, int]:
    """Completa mes y año por ``log_number`` sin alterar el día OCR.

    Args:
        reports: Reportes ya validados (uno por PDF procesado).
        book_dates_path: Registro de extremos de fecha aprendidos en otras
            ejecuciones. Si se omite, el corrector solo ve la ejecución
            actual, que es el comportamiento aislado de siempre.

    El resultado ``corrected`` cuenta componentes inferidos o corregidos,
    no paginas.
    ``days_filled`` cuenta los días completados con el último día que cabe en
    la secuencia del libro y ``days_repaired`` los que se leyeron pero
    contradecían esa secuencia.
    """
    books = group_books(reports)
    stored = (
        _load_book_dates(Path(book_dates_path))
        if book_dates_path is not None
        else {}
    )
    stats: Dict[str, int] = {
        "books": len(books),
        "corrected": 0,
        "flagged": 0,
        "regressions": 0,
        "sequence_candidates": 0,
        "years_consensus": 0,
        "run_year_consensus": 0,
        "run_year_review": 0,
        "year_outliers": 0,
        "after_the_run": 0,
        "bracket_corrected": 0,
        "days_repaired": 0,
        "months_filled": 0,
        "years_filled": 0,
        "days_filled": 0,
        "registry_filled": 0,
        "unresolved": 0,
    }

    for book in books:
        for page in book:
            _recombine(page)

        # Lo que cae despues de la ejecucion se aparta lo primero: no
        # debe votar en el consenso ni anclar una inferencia.
        stats["after_the_run"] += _flag_readings_after_the_run(book)

        year_consensus = _correct_year_by_book_consensus(book)
        stats["years_consensus"] += year_consensus

        # El mes que contradice a sus dos vecinas se arregla antes de
        # buscar alternativas y antes de tomar las anclas. Hace tres danos
        # si se queda: sale en el CSV con una fecha a un mes de la real,
        # rompe el intervalo que habria completado a las paginas que no se
        # dejaron leer, y deja una regresion que la busqueda por
        # alternativas intenta tapar moviendo el ano de otra pagina.
        resolved_months = _resolve_ambiguous_months(book)
        bracket = _correct_bracketed_component(book, "month", _month_number)
        stats["bracket_corrected"] += bracket

        # Las anclas del libro se fotografían antes de interpolar: después,
        # una lectura en conflicto con un intervalo ya no cuenta como ancla
        # y el libro parecería unánime sin serlo.
        year_anchors = _anchors(book, YEAR_FIELD_ID, _year_normalize)
        month_anchors = _anchors(book, "month", _month_number)

        years, year_flags = _infer_between_anchors(
            book, YEAR_FIELD_ID, _year_normalize
        )
        years += _infer_edges(book, YEAR_FIELD_ID, _year_normalize)
        years += _fill_from_book_consensus(
            book, YEAR_FIELD_ID, _year_normalize, year_anchors
        )
        months, month_flags = _infer_between_anchors(
            book, "month", _month_number
        )
        months += resolved_months
        months += _infer_edges(book, "month", _month_number)
        months += _fill_from_book_consensus(
            book, "month", _month_number, month_anchors
        )

        # Lo último que se consulta es el registro de otras ejecuciones: la
        # evidencia de las páginas que están aquí siempre manda sobre lo
        # guardado.
        registry_key = _storage_key(book)
        registry_filled = (
            _fill_from_registry(book, registry_key, stored[registry_key])
            if registry_key is not None and registry_key in stored
            else 0
        )
        stats["registry_filled"] += registry_filled

        # Las alternativas se deciden despues de aprovechar la evidencia
        # directa del libro. Asi una lectura como 01 no se convierte primero
        # en 06 cuando dos paginas cercanas ya demostraban que era 26.
        sequence_candidates = _resolve_sequence_alternatives(book)
        stats["sequence_candidates"] += sequence_candidates

        for page in book:
            _recombine(page)
        days_repaired = _repair_days_by_sequence(book)
        days_repaired += _complete_lost_tens_digit(book)
        stats["days_repaired"] += days_repaired
        days = _fill_days_to_month_end(book)
        for page in book:
            _recombine(page)

        # La última palabra sobre el año la tiene el libro: algún paso
        # anterior (las alternativas de secuencia, el registro) puede haber
        # dejado un año que el resto del libro desmiente.
        year_outliers = _correct_year_outliers(book)
        stats["year_outliers"] += year_outliers

        # Segunda vuelta: las alternativas de secuencia, la decena repuesta
        # y el relleno del dia trabajan despues del primer descarte, asi que
        # pueden devolver a la pagina una fecha posterior a la ejecucion. Se
        # aparta otra vez y el libro vuelve a completar el dia, que es lo
        # unico que puede sobrar cuando el mes y el ano ya son posibles.
        late = _flag_readings_after_the_run(book)
        if late:
            days += _fill_days_to_month_end(book)
            for page in book:
                _recombine(page)
        stats["after_the_run"] += late

        regressions = _check_regressions(book)
        unresolved = _flag_unresolved(book)

        stats["years_filled"] += years
        stats["months_filled"] += months
        stats["days_filled"] += days
        stats["corrected"] += (
            years + months + days + year_consensus + sequence_candidates
            + registry_filled + bracket + days_repaired + year_outliers
        )
        stats["flagged"] += year_flags + month_flags
        stats["regressions"] += regressions
        stats["unresolved"] += unresolved
        for page in book:
            _recompute_page_status(page)

    run_year_consensus, run_year_review = _correct_year_by_run_consensus(
        books
    )
    stats["run_year_consensus"] = run_year_consensus
    stats["run_year_review"] = run_year_review
    stats["corrected"] += run_year_consensus
    stats["flagged"] += run_year_review

    for report in reports:
        for page in report.pages:
            _recombine(page)
    # La antigüedad se juzga con el libro a la vista: un año que leyeron dos
    # bitácoras del libro es un libro atrasado, no una lectura dudosa.
    review_date_windows(reports)
    for report in reports:
        for page in report.pages:
            _recompute_page_status(page)
        _recompute_summary(report)
    logger.info(f"Corrector de fechas por log_number: {stats}")
    return stats
