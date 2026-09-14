"""Corrector de matrículas por libro (un avión por libro).

Un "libro" es un bloque de 50 páginas (misma serie del ``log_number`` y
misma mitad del logpage 00-49/50-99). Como cada libro pertenece a una
sola aeronave, la matrícula debe ser idéntica en todas sus páginas.

Después del procesamiento OCR y de la normalización de formato, este
corrector:

1. Agrupa las páginas en libros (regla de la serie + mitad).
2. En cada libro, decide la matrícula **dígito a dígito**: cada posición
   se vota por separado con todas las lecturas del libro, pesadas por su
   confianza y por lo limpia que fue la lectura.
3. Corrige de forma agresiva a la ganadora TODAS las páginas que no
   coincidan (ilegibles, de confianza baja o de formato válido pero
   distinto), dejando el valor original en el comentario para
   auditoría: un libro = un avión es una regla dura.
4. Si el libro no tiene ninguna lectura válida, no hay ganador y las
   páginas quedan sin matrícula (no se detectó).

El voto por posición sustituye a la mayoría sobre la matrícula completa.
Con la mayoría simple, una sola página confiada pero equivocada (el 7
manuscrito leído como 3) se imponía a todo un libro, porque las demás
páginas del libro leían el número de formas distintas entre sí y ninguna
alcanzaba dos votos. Separando las posiciones, esas lecturas dispersas sí
coinciden allí donde importa y el dígito correcto gana.
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
from typing import Collection, Dict, List, Optional, Tuple

from loguru import logger

from app.models.schemas import FieldResult, PageResult, Status, ValidationReport
from app.utils.postprocess import (
    AMBIGUOUS_MATRICULA_NOTE,
    WEAK_MATRICULA_NOTE,
    apply_postprocess,
)
from app.validation.fleet_match import fleet_match
from app.validation.grouping import book_key, group_books, log_number
from app.validation.page_status import (
    AUTO_INDEX_MIN_VOTES,
    recompute_page_status,
)

MATRICULA_FIELD_ID = "matricula"
BOOK_MATRICULAS_FILENAME = "book_matriculas.json"

# Peso de la evidencia según cómo se obtuvo el número: una tirada limpia de
# cuatro dígitos vale más que una reconstruida con caracteres confundibles,
# y esta mucho más que un número recompuesto con dígitos dispersos. La
# lectura reconstruida se descuenta poco: viene de una ventana anclada entre
# el prefijo y el sufijo del campo, así que es una matrícula completa, no un
# resto de texto.
_EVIDENCE_QUALITY = {
    "": 1.0,
    AMBIGUOUS_MATRICULA_NOTE: 0.8,
    WEAK_MATRICULA_NOTE: 0.25,
}
# Piso de peso: una lectura sin confianza sigue siendo evidencia, pero mínima.
_MIN_WEIGHT = 0.05
# Valores que ya son resultado de una inferencia: no pueden votarse a sí mismos.
_DERIVED_SOURCES = frozenset({
    "book_correction",
    "book_registry",
    "inferred",
})
# Valores resueltos por una comprobación posterior (el respaldo de fecha o
# la lista de la flota): valen más que el texto crudo de la pasada
# principal, que ya falló.
_VERIFIED_SOURCES = frozenset({"ocr_fallback", "fleet_validation"})

_ORDER = {Status.OK: 0, Status.WARNING: 1, Status.ERROR: 2}
_CANONICAL_MATRICULA_RE = re.compile(r"^HP-(\d{4})(CMP|WWP)$")
# El corrector no recibe ``AppConfig``. Este es el mismo umbral general que
# usa la configuración por defecto; el número de votos y los conflictos son
# las guardas adicionales que deciden si la inferencia puede ir automática.
_MIN_BOOK_AUTO_CONFIDENCE = 0.50
_BOOK_STORAGE_KEY_RE = re.compile(r"^\d{5}[AB]$")
# Cifras en que una lectura canónica puede diferir de la matrícula del libro
# y seguir contándose como mal leída. Una sola es el error habitual del trazo
# manuscrito; con dos ya puede ser otro avión con el log_number mal leído.
_MISREAD_MAX_DIGITS = 1


def _misread_of(
    reading: str,
    winner: str,
    foreign: Collection[str] = frozenset(),
    fleet: Collection[str] = frozenset(),
) -> bool:
    """La lectura se explica como la matrícula del libro mal leída.

    ``foreign`` son las matrículas que otros libros de la ejecución dan por
    suyas: si la página leyó una de ellas, puede ser una bitácora de ese otro
    avión y no se da por mal leída. Con la flota a la vista, una lectura que
    no es ningún avión tampoco puede ser otro avión: es un error de lectura,
    esté a las cifras que esté.
    """
    if reading in foreign:
        return False
    if fleet and reading not in fleet:
        return True
    observed = _CANONICAL_MATRICULA_RE.fullmatch(reading)
    expected = _CANONICAL_MATRICULA_RE.fullmatch(winner)
    if observed is None or expected is None:
        return False
    different = sum(
        left != right
        for left, right in zip(observed.group(1), expected.group(1))
    )
    return different <= _MISREAD_MAX_DIGITS


def _storage_key(book: List[PageResult]) -> Optional[str]:
    """Clave compacta de seis caracteres para un libro conocido."""
    keys = {key for page in book if (key := book_key(page)) is not None}
    if len(keys) != 1:
        return None
    series, half = keys.pop()
    return f"{series}{half}"


def _load_book_matriculas(path: Path) -> Dict[str, str]:
    """Lee el mapa compacto; un archivo dañado no detiene el procesamiento."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"No se pudo leer el mapa de libros {path}: {exc}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"Mapa de libros inválido: {path}")
        return {}
    return {
        key: value
        for key, value in data.items()
        if isinstance(key, str)
        and _BOOK_STORAGE_KEY_RE.fullmatch(key)
        and isinstance(value, str)
        and _CANONICAL_MATRICULA_RE.fullmatch(value)
    }


def _save_book_matriculas(path: Path, values: Dict[str, str]) -> None:
    """Escribe solo ``clave: matrícula`` en JSON compacto y atómico."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    payload = json.dumps(
        dict(sorted(values.items())),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    temporary.write_text(f"{payload}\n", encoding="utf-8")
    temporary.replace(path)


def _matricula_field(page: PageResult):
    for field in page.fields:
        if field.field_id == MATRICULA_FIELD_ID:
            return field
    return None


def _page_evidence(
    field: FieldResult,
    fleet: Collection[str] = frozenset(),
) -> Optional[Tuple[str, str, float]]:
    """(número de 4 dígitos, sufijo, peso) con que una página vota.

    Se parte del texto crudo del OCR: una página descartada por el formato
    sigue conteniendo dígitos legibles, y esa evidencia vale igual que la de
    una lectura que sí pasó. El valor ya normalizado se usa cuando no hay
    texto crudo o cuando lo resolvió una segunda pasada (que vio el recorte
    de nuevo), pero nunca si viene de una corrección previa: contaría la
    inferencia anterior como si fuera una lectura nueva.
    """
    confidence = max(float(field.confidence), _MIN_WEIGHT)
    if fleet and field.raw_value and field.source not in _VERIFIED_SOURCES:
        # Con la flota, el texto crudo se lee contra ella y no con la ventana
        # de cifras que el postproceso elige a ciegas.
        match = fleet_match(field.raw_value, fleet)
        if match is not None:
            found = _CANONICAL_MATRICULA_RE.fullmatch(match[0])
            if found is not None:
                return found.group(1), found.group(2), confidence
    if field.raw_value and field.source not in _VERIFIED_SOURCES:
        value, note = apply_postprocess(
            field.field_id, MATRICULA_FIELD_ID, field.raw_value
        )
        match = _CANONICAL_MATRICULA_RE.fullmatch(value)
        if match is not None:
            quality = _EVIDENCE_QUALITY.get(
                note, _EVIDENCE_QUALITY[WEAK_MATRICULA_NOTE]
            )
            return match.group(1), match.group(2), confidence * quality
    if field.value and field.source not in _DERIVED_SOURCES:
        match = _CANONICAL_MATRICULA_RE.fullmatch(field.value)
        if match is not None:
            return match.group(1), match.group(2), confidence
    if field.raw_value:
        # Segunda pasada sin valor utilizable: queda el texto crudo.
        value, note = apply_postprocess(
            field.field_id, MATRICULA_FIELD_ID, field.raw_value
        )
        match = _CANONICAL_MATRICULA_RE.fullmatch(value)
        if match is not None:
            quality = _EVIDENCE_QUALITY.get(
                note, _EVIDENCE_QUALITY[WEAK_MATRICULA_NOTE]
            )
            return match.group(1), match.group(2), confidence * quality
    return None


def _unique_evidence(
    entries: List[Tuple[PageResult, FieldResult]],
    fleet: Collection[str] = frozenset(),
) -> List[Tuple[str, str, float]]:
    """Evidencia del libro con una sola aportación por página física.

    La misma página escaneada en dos PDF distintos llega dos veces con la
    misma lectura. Contarla dos veces duplicaría un error de OCR y le daría
    ventaja sobre las páginas que solo aparecen una vez, así que de cada
    ``log_number`` se conserva la aportación de mayor peso.
    """
    best: Dict[object, Tuple[str, str, float]] = {}
    for index, (page, field) in enumerate(entries):
        evidence = _page_evidence(field, fleet)
        if evidence is None:
            continue
        # Sin log_number legible no se puede saber si dos páginas son la
        # misma, así que cada una cuenta por separado.
        key = log_number(page) or f"page-{index}"
        previous = best.get(key)
        if previous is None or evidence[2] > previous[2]:
            best[key] = evidence
    return list(best.values())


def _snap_to_fleet(
    reports: List[ValidationReport], fleet: Collection[str]
) -> int:
    """Lleva al avión de la flota cada lectura que solo se parece a uno.

    Va antes del voto del libro: una página cuyo texto crudo señala sin
    competencia un avión (ver :func:`app.validation.fleet_match.fleet_match`)
    vota por ese avión y no por la ventana de cifras que el postproceso eligió
    a ciegas, o por nada si la descartó. La lectura anterior queda en las
    alternativas.
    """
    snapped = 0
    for report in reports:
        for page in report.pages:
            field = _matricula_field(page)
            if page.blank or field is None or not field.raw_value:
                continue
            current = (field.value or "").strip().upper()
            if current in fleet:
                continue
            match = fleet_match(field.raw_value, fleet)
            if match is None:
                continue
            aircraft, cost = match
            if current and current not in field.alternatives:
                field.alternatives.append(current)
            field.value = aircraft
            field.source = "fleet_validation"
            field.inference_method = "fleet_reading_match"
            if cost or field.status is Status.ERROR:
                field.status = Status.WARNING
            note = (
                f"Reading {field.raw_value!r} matches fleet aircraft "
                f"{aircraft} (cost {cost})"
            )
            field.comment = f"{field.comment} | {note}".strip(" |")
            _recompute_page_status(page)
            snapped += 1
    return snapped


def _book_winner(
    entries: List[Tuple[PageResult, FieldResult]],
    fleet: Collection[str] = frozenset(),
) -> Optional[Tuple[str, int, float]]:
    """Matrícula del libro por consenso dígito a dígito.

    Cada posición se decide por separado sumando el peso de todas las
    lecturas, de modo que el dígito correcto gana aunque ninguna lectura
    completa se repita. El número resultante tiene que coincidir además con
    alguna lectura completa del libro: el consenso elige entre lo que se
    leyó, nunca inventa una matrícula que no vio ninguna página. Si mezclar
    posiciones produce un número que nadie leyó, decide la lectura completa
    de más peso.

    Returns:
        (matrícula canónica, páginas que la leyeron entera, confianza) o
        None si el libro no aporta ninguna lectura utilizable.
    """
    evidence = _unique_evidence(entries, fleet)
    if fleet:
        # Con la flota a la vista, lo que no es ningún avión no vota mientras
        # alguna página del libro sí haya leído uno.
        in_fleet = [
            item for item in evidence if f"HP-{item[0]}{item[1]}" in fleet
        ]
        evidence = in_fleet or evidence
    if not evidence:
        return None
    votes: List[Dict[str, float]] = [defaultdict(float) for _ in range(4)]
    observed: Dict[str, float] = defaultdict(float)
    suffixes: Dict[str, float] = defaultdict(float)
    for number, suffix, weight in evidence:
        observed[number] += weight
        suffixes[suffix] += weight
        for position, digit in enumerate(number):
            votes[position][digit] += weight
    # El desempate se ordena por el propio dígito para que dos ejecuciones del
    # mismo batch den siempre el mismo resultado: con pesos idénticos no hay
    # evidencia que distinga, pero la salida no puede depender del orden en
    # que llegaron las páginas.
    number = "".join(
        max(sorted(slot.items()), key=lambda item: item[1])[0]
        for slot in votes
    )
    if number not in observed:
        number = max(sorted(observed.items()), key=lambda item: item[1])[0]
    suffix = max(sorted(suffixes.items()), key=lambda item: item[1])[0]
    winner = f"HP-{number}{suffix}"
    # ``evidence`` ya eliminó escaneos repetidos del mismo log_number. El
    # número que habilita una inferencia automática tiene que contar páginas
    # físicas independientes, no filas: dos copias del mismo escaneo siguen
    # siendo un solo respaldo.
    matches = [
        weight for observed_number, observed_suffix, weight in evidence
        if observed_number == number and observed_suffix == suffix
    ]
    confidence = (
        round(sum(matches) / len(matches), 3) if matches
        else round(min(observed[number], 1.0), 3)
    )
    return winner, len(matches), confidence


def _correct_book(
    book: List[PageResult],
    foreign: Collection[str] = frozenset(),
    fleet: Collection[str] = frozenset(),
) -> Tuple[int, int]:
    """Corrige las matrículas de un libro. Devuelve (corregidas, marcadas).

    Corrección agresiva: toda página cuya matrícula difiera del ganador
    (vacía, ilegible, de formato válido pero distinta) se sobrescribe con
    la matrícula del libro; el valor original queda en el comentario.
    ``foreign`` son las matrículas de los demás libros de la ejecución.
    """
    entries = [(page, _matricula_field(page)) for page in book]
    entries = [(p, f) for p, f in entries if f is not None]
    if not entries:
        return 0, 0

    winner_info = _book_winner(entries, fleet)
    if winner_info is None:
        return 0, 0
    winner, count, winner_confidence = winner_info

    corrected = 0
    flagged = 0
    for page, field in entries:
        if field.value == winner:
            # Una lectura propia débil no tiene por qué ir a Revisar cuando
            # varias páginas independientes leyeron exactamente lo mismo. El
            # consenso no cambia su valor: únicamente aporta el respaldo que
            # le faltaba y conserva esa procedencia para auditoría.
            if (
                field.status is not Status.OK
                and count >= AUTO_INDEX_MIN_VOTES
                and winner_confidence >= _MIN_BOOK_AUTO_CONFIDENCE
            ):
                field.confidence = max(field.confidence, winner_confidence)
                field.status = Status.OK
                field.source = "book_correction"
                field.inference_method = "book_consensus_confirmation"
                field.votes = count
                field.comment = (
                    f"{field.comment} | Confirmed by book consensus "
                    f"({count} vote(s))"
                ).strip(" |")
                _recompute_page_status(page)
            continue
        original = field.value
        if original and original not in field.alternatives:
            field.alternatives.append(original)
        field.value = winner
        field.confidence = winner_confidence
        # Se conserva la inferencia, pero no se borra la duda que la produjo.
        # Una lectura canónica distinta puede significar que el log_number
        # también se leyó mal y que esta página ni siquiera pertenece al libro
        # cuya matrícula ganó. Ponerla en OK la enviaba bajo el separador de
        # otro avión. Las lecturas vacías o inválidas sí pueden repararse de
        # forma automática cuando el consenso tiene respaldo y confianza.
        canonical_original = bool(
            original and _CANONICAL_MATRICULA_RE.fullmatch(original)
        )
        sufficiently_supported = (
            count >= AUTO_INDEX_MIN_VOTES
            and winner_confidence >= _MIN_BOOK_AUTO_CONFIDENCE
        )
        # Frente a un consenso con respaldo, una cifra distinta es el error
        # habitual del reconocedor y no otro avión: se corrige sin apartar la
        # página. Más cifras, o la matrícula de otro libro, sí se revisan.
        conflicting = canonical_original and not (
            sufficiently_supported
            and _misread_of(original, winner, foreign, fleet)
        )
        field.status = (
            Status.OK
            if not canonical_original and sufficiently_supported
            else Status.WARNING
        )
        field.source = "book_correction"
        field.inference_method = "book_digit_consensus"
        # Cuantas paginas del libro leyeron entera esta matricula. La pagina
        # no la leyo (por eso se corrige), asi que es todo el respaldo que
        # tiene, y con uno solo no alcanza para indexarla sin mirar.
        field.votes = count
        if original:
            field.comment = (
                f"Corrected from {original!r} by book consensus "
                f"({count} vote(s))"
            )
            if conflicting:
                field.comment += "; conflicting registration requires review"
                page.airvault_discrepancy = True
            elif canonical_original:
                field.comment += "; one digit apart, taken as a misreading"
            flagged += 1
        else:
            field.comment = (
                f"Inferred from book readings: {winner} ({count} vote(s))"
            )
            if not sufficiently_supported:
                field.comment += "; insufficient support for auto index"
            corrected += 1
        _recompute_page_status(page)

    logger.info(
        f"[Libro] Matrícula dominante {winner} ({count} votos, "
        f"conf={winner_confidence}) | corregidas: {corrected} | "
        f"discrepantes sobrescritas: {flagged}"
    )
    return corrected, flagged


def _apply_stored_matricula(
    book: List[PageResult],
    matricula: str,
    foreign: Collection[str] = frozenset(),
    fleet: Collection[str] = frozenset(),
) -> Tuple[int, int]:
    """Aplica una asociación confirmada en otra ejecución.

    Una lectura canónica distinta no se oculta: se conserva como alternativa
    y la página queda en WARNING. Solo va a revisión cuando no se explica
    como una cifra mal leída (ver :func:`_misread_of`). Los valores vacíos,
    inválidos o coincidentes sí quedan confirmados porque el mapa solo
    aprende consensos fuertes.
    """
    corrected = 0
    flagged = 0
    for page in book:
        field = _matricula_field(page)
        if field is None:
            continue
        original = (field.value or "").strip()
        different_reading = bool(
            original
            and original != matricula
            and _CANONICAL_MATRICULA_RE.fullmatch(original)
        )
        canonical_conflict = different_reading and not _misread_of(
            original, matricula, foreign, fleet
        )
        if original and original != matricula \
                and original not in field.alternatives:
            field.alternatives.append(original)
        if original != matricula:
            field.value = matricula
            corrected += 1
        field.confidence = max(field.confidence, _MIN_BOOK_AUTO_CONFIDENCE)
        field.status = Status.WARNING if different_reading else Status.OK
        field.source = "book_registry"
        field.inference_method = "stored_book_matricula"
        field.votes = AUTO_INDEX_MIN_VOTES
        if canonical_conflict:
            field.comment = (
                f"Stored book registration {matricula} conflicts with "
                f"direct reading {original}; requires review"
            )
            page.airvault_discrepancy = True
            flagged += 1
        elif different_reading:
            field.comment = (
                f"Stored book registration {matricula} replaces direct "
                f"reading {original}: one digit apart"
            )
        else:
            field.comment = f"Confirmed from stored book registration: {matricula}"
        _recompute_page_status(page)
    return corrected, flagged


def _mark_stored_conflict(
    book: List[PageResult], stored: str, observed: str
) -> int:
    """Bloquea un libro cuando dos consensos fuertes se contradicen."""
    marked = 0
    for page in book:
        field = _matricula_field(page)
        if field is None:
            continue
        field.status = Status.WARNING
        field.comment = (
            f"{field.comment} | Stored book registration {stored} conflicts "
            f"with current consensus {observed}; requires review"
        ).strip(" |")
        page.airvault_discrepancy = True
        _recompute_page_status(page)
        marked += 1
    return marked


def _recompute_page_status(page: PageResult) -> None:
    """Recalcula el estado de una página con la política de indexación.

    La política vive en ``app.validation.page_status`` porque la comparten
    los tres correctores y la validación de la plantilla: si cada uno
    contara los campos a su manera, activar la verificación de matrículas
    cambiaba el estado de páginas que nadie había tocado.
    """
    if page.blank:
        return
    recompute_page_status(page)


def _recompute_summary(report: ValidationReport) -> None:
    pages = report.pages
    summary = {
        "total_pages": len(pages),
        "ok_pages": 0,
        "warning_pages": 0,
        "error_pages": 0,
        "blank_pages": sum(1 for p in pages if p.blank),
    }
    for page in pages:
        if page.blank:
            continue
        if page.status is Status.OK:
            summary["ok_pages"] += 1
        elif page.status is Status.WARNING:
            summary["warning_pages"] += 1
        else:
            summary["error_pages"] += 1
    report.summary = summary


def correct_matricula_by_book(
    reports: List[ValidationReport],
    book_matriculas_path: Optional[Path] = None,
    fleet: Collection[str] = (),
) -> Dict[str, int]:
    """Corrector global de matrículas (un avión por libro).

    Args:
        reports: Reportes ya validados (uno por PDF procesado).
        book_matriculas_path: Mapa compacto aprendido en otras ejecuciones.
            Si se omite, el corrector conserva el comportamiento aislado.
        fleet: Lista de aviones cuando la verificación está activa. Con ella
            las lecturas ruidosas se leen contra la flota antes de votar, y
            una lectura que no es ningún avión no aparta la página.

    Returns:
        Estadísticas: libros, corregidas, marcadas.
    """
    fleet = frozenset(fleet or ())
    fleet_matched = _snap_to_fleet(reports, fleet) if fleet else 0
    books = group_books(reports)
    stored = (
        _load_book_matriculas(Path(book_matriculas_path))
        if book_matriculas_path is not None
        else {}
    )
    stats = {
        "books": len(books),
        "corrected": 0,
        "flagged": 0,
        "reused": 0,
        "registry_conflicts": 0,
        "fleet_matched": fleet_matched,
    }
    plans = []
    for book in books:
        key = _storage_key(book)
        remembered = stored.get(key, "") if key is not None else ""
        entries = [(page, _matricula_field(page)) for page in book]
        entries = [(page, field) for page, field in entries if field is not None]
        winner_info = _book_winner(entries, fleet) if entries else None
        strong_current = bool(
            winner_info
            and winner_info[1] >= AUTO_INDEX_MIN_VOTES
            and winner_info[2] >= _MIN_BOOK_AUTO_CONFIDENCE
        )
        plans.append((book, remembered, winner_info, strong_current))
    # Lo que cada libro da por suyo. Una página que lee la matrícula de otro
    # libro de la ejecución puede ser de ese avión, y no se da por mal leída.
    claimed = [
        {remembered, winner_info[0] if strong_current else ""} - {""}
        for _book, remembered, winner_info, strong_current in plans
    ]
    for index, (book, remembered, winner_info, strong_current) in enumerate(plans):
        foreign = frozenset().union(
            *(values for other, values in enumerate(claimed) if other != index)
        )
        if remembered and winner_info and winner_info[0] != remembered \
                and strong_current:
            corrected, flagged = _correct_book(book, foreign, fleet)
            conflict_count = _mark_stored_conflict(
                book, remembered, winner_info[0]
            )
            flagged += conflict_count
            stats["registry_conflicts"] += 1
        elif remembered:
            corrected, flagged = _apply_stored_matricula(
                book, remembered, foreign, fleet
            )
            stats["reused"] += 1
        else:
            corrected, flagged = _correct_book(book, foreign, fleet)
        stats["corrected"] += corrected
        stats["flagged"] += flagged
    for report in reports:
        _recompute_summary(report)
    logger.info(f"Corrector de matrículas: {stats}")
    return stats


def learn_book_matriculas(
    reports: List[ValidationReport],
    path: Path,
    fleet: Collection[str] = (),
) -> int:
    """Guarda asociaciones fuertes libro->matrícula para otras ejecuciones.

    Solo aprende cuando dos páginas físicas independientes respaldan la
    lectura con la confianza normal del corrector y el resultado final no
    quedó en WARNING. Nunca reemplaza una asociación previa contradictoria:
    ese caso requiere revisión humana.

    El archivo contiene exclusivamente pares como ``"21473A":"HP-1534CMP"``.
    Incluso diez mil libros ocupan solo unos cientos de kilobytes.
    """
    path = Path(path)
    stored = _load_book_matriculas(path)
    learned = 0
    for book in group_books(reports):
        key = _storage_key(book)
        if key is None:
            continue
        entries = [(page, _matricula_field(page)) for page in book]
        entries = [(page, field) for page, field in entries if field is not None]
        winner_info = (
            _book_winner(entries, frozenset(fleet or ())) if entries else None
        )
        if winner_info is None:
            continue
        winner, count, confidence = winner_info
        if (
            count < AUTO_INDEX_MIN_VOTES
            or confidence < _MIN_BOOK_AUTO_CONFIDENCE
            or not any(
                field.value == winner and field.status is Status.OK
                for _page, field in entries
            )
        ):
            continue
        previous = stored.get(key)
        if previous is not None and previous != winner:
            logger.warning(
                f"No se reemplaza {key}={previous}: la ejecución actual "
                f"propone {winner}"
            )
            continue
        if previous is None:
            stored[key] = winner
            learned += 1
    if learned:
        try:
            _save_book_matriculas(path, stored)
        except OSError as exc:
            logger.warning(f"No se pudo guardar el mapa de libros {path}: {exc}")
            return 0
        logger.info(
            f"Mapa de libros: {learned} asociación(es) nueva(s), "
            f"{len(stored)} total, {path.stat().st_size} bytes"
        )
    return learned
