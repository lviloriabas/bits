"""Postprocesadores de valores OCR.

El pipeline aplica el postprocesador cuyo nombre indique la plantilla
(campo ``postprocess``), manteniendo las reglas fuera del código.

Registrados: matricula, date, digits, day, month, year, char.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger

from app.utils.date_window import year_is_possible

MESES = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}
# Meses en español e inglés (para formularios con etiquetas en inglés).
_MESES_LETRAS = {
    "ENE": 1, "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "ABR": 4, "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8, "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DIC": 12, "DEC": 12,
}
_CANONICA_MES = {num: nombre
                 for nombre, num in _MESES_LETRAS.items()}
# Abreviaturas de mes (español e inglés) para decodificación por ranuras.
MONTH_WORDS: list = [(nombre, numero)
                     for nombre, numero in _MESES_LETRAS.items()]
# Confusiones típicas de OCR entre dígitos y letras.
_OCR_CHAR_MAP = str.maketrans({"0": "o", "1": "i", "5": "s", "8": "b"})

# Mapa de confusión OCR específico de meses: en las casillas de fecha el
# '1'/'I' leídos tras la 'JU' son la 'L' de JUL pegada al separador, así
# que aquí '1' e 'I' se mapean a 'L' (ningún mes contiene 'i'). Los
# objetivos van en mayúscula porque se aplica tras raw.upper().
_MESES_CHAR_MAP = str.maketrans({
    "0": "O", "1": "L", "5": "S", "8": "B", "I": "L",
})
# OCR manuscrito: confunde letras y dígitos. Mapeo letra->dígito para
# usar solo en lecturas donde la pieza esperada es numérica.
_OCR_LETTER_TO_DIGIT_DICT = {
    "O": "0", "o": "0", "Q": "0",
    "I": "1", "i": "1", "l": "1", "!": "1",
    "Z": "2", "z": "2",
    "S": "5", "s": "5",
    "B": "8", "b": "8",
    "G": "6", "g": "6",
}
_OCR_LETTER_TO_DIGIT = str.maketrans(_OCR_LETTER_TO_DIGIT_DICT)
# Caracteres que el reconocedor devuelve en lugar de un dígito manuscrito
# dentro de la matrícula. Es el mismo mapa anterior en mayúscula (la
# matrícula se normaliza así) más la 'F': el 7 de estas bitácoras se escribe
# con travesaño y el modelo lo devuelve como F. Solo se aplica dentro del
# token de cuatro caracteres de la matrícula; ningún otro campo lo usa.
_MATRICULA_AMBIGUOUS = {
    **{char.upper(): digit
       for char, digit in _OCR_LETTER_TO_DIGIT_DICT.items()},
    "F": "7",
}
# Primer carácter del sufijo "CMP"/"WWP" tal como lo devuelve el OCR.
_MATRICULA_SUFFIX_HEAD = frozenset("CW")
# Aviones cuyo sufijo no es "CMP" aunque la página no lo diga.
WWP_ONLY = {"1990"}
# Aviones que siempre salen como "CMP", se lea lo que se lea. El 1522 está
# escrito de las dos maneras en las bitácoras, pero el picklist de AirVault
# solo tiene HP-1522CMP: dejar pasar el WWP obligaba a corregirlo a mano en
# el CSV y hacía que la carga no encontrara su avión.
CMP_ONLY = {"1522"}
WEAK_MATRICULA_NOTE = (
    "registration: digits inferred from scattered OCR (low confidence)"
)
AMBIGUOUS_MATRICULA_NOTE = (
    "registration: handwritten characters read as digits"
)
NUMERIC_MONTH_NOTE = "numeric handwritten month"


def _levenshtein(a: str, b: str) -> int:
    """Distancia de Levenshtein entre dos cadenas cortas."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(
                cur[-1] + 1,
                prev[j] + 1,
                prev[j - 1] + (ca != cb),
            ))
        prev = cur
    return prev[-1]


def _lcs_length(a: str, b: str) -> int:
    """Longitud de la subsecuencia común más larga para cadenas cortas."""
    row = [0] * (len(b) + 1)
    for ca in a:
        previous = 0
        for index, cb in enumerate(b, start=1):
            saved = row[index]
            if ca == cb:
                row[index] = previous + 1
            else:
                row[index] = max(row[index], row[index - 1])
            previous = saved
    return row[-1]


def month_candidates(value: str) -> List[int]:
    """Meses compatibles con la lectura, sin desempatar por calendario.

    Acepta: "7", "07", "JUL", "JUIL", "GUL" (confusión OCR), "JAN",
    "JUL Month" (si las letras dominan). Agrupa las variantes de idioma.
    """
    raw = value.strip()
    if not raw:
        return []

    if not re.search(r"[A-Za-z]", raw):
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            return []
        mes = int(digits[:3])
        return [mes] if 1 <= mes <= 12 else []

    # Una abreviatura real tiene prioridad sobre las equivalencias: la I
    # de DIC no es la L que el OCR suele confundir en JUL.
    original = re.sub(r"[^A-Z]", "", raw.upper())
    exact = sorted({n for word, n in MONTH_WORDS if word in original})
    if exact:
        return exact

    # El mapa OCR se aplica ANTES de filtrar letras: los dígitos mal leídos
    # ('JU1' -> 'JUI', 'JUL' bien leído) se convierten a su letra probable.
    letters = re.sub(r"[^A-Za-z]", "", raw.upper().translate(_MESES_CHAR_MAP))
    if len(letters) < 2:
        return []
    original_letters = re.sub(r"[^A-Za-z]", "", raw)
    # Si el OCR mezcló varios dígitos con una sola letra no existe evidencia
    # suficiente de un mes manuscrito (``50c`` no debe convertirse en OCT).
    if re.search(r"\d", raw) and len(original_letters) < 2:
        return []
    # Subcadena exacta de un mes dentro de texto impreso ('JULMONTH').
    exact = sorted({n for word, n in MONTH_WORDS if word in letters})
    if exact:
        return exact
    # Fuzzy match conservador. Para un campo manuscrito de tres casillas, una
    # sustitución solo es aceptable si al menos dos letras siguen iguales en
    # la misma posición. Esto evita convertir ruido como ``50c`` en OCT/DIC.
    candidatos = [
        (numero, nombre)
        for nombre, numero in _MESES_LETRAS.items()
        if _levenshtein(letters, nombre) < 2
        and sum(a == b for a, b in zip(letters, nombre)) >= 2
    ]
    # Si no hay candidatos y la cadena parece contener un mes
    # (3+ letras, probablemente distorsionado por OCR), se reintenta
    # con un umbral más permisivo: artefactos como "JTUIC", "J011",
    # "UUC" donde los separadores de casilla o el ruido añaden
    # caracteres.
    if not candidatos and 3 <= len(letters) <= 5:
        candidatos = [
            (numero, nombre)
            for nombre, numero in _MESES_LETRAS.items()
            if _levenshtein(letters, nombre) < 3
            and _lcs_length(letters, nombre) >= 2
        ]
    if not candidatos:
        return []
    best_dist = min(_levenshtein(letters, nombre) for _, nombre in candidatos)
    empatados = [
        numero for numero, nombre in candidatos
        if _levenshtein(letters, nombre) == best_dist
    ]
    return sorted(set(empatados))


def _parse_month(value: str) -> Optional[int]:
    """Devuelve un mes solo cuando la lectura tiene una solución única."""
    candidates = month_candidates(value)
    return candidates[0] if len(candidates) == 1 else None


def _normalize_year(anio: int) -> Optional[int]:
    """Año 4 dígitos dentro de la ventana en que existen bitácoras.

    Acepta 2 dígitos (26 -> 2026) y rechaza lecturas absurdas
    ("216" -> 2216, "1751" -> fuera de rango) y las adelantadas: una página
    no se firma después del día en que se escanea, así que un '96' o un
    '28' leídos en 2026 son un año mal leído (ver
    ``app.utils.date_window``).
    """
    if anio < 100:
        anio += 2000
    return anio if year_is_possible(anio) else None


def _day(value: str) -> Tuple[str, str]:
    """Día: 1-2 dígitos en el rango 1-31.

    Si la lectura mezcla dígitos (p. ej. '21 0'), se prefiere el run de
    2 dígitos; un run único de 3+ dígitos es una lectura inválida. Dos
    dígitos separados por el separador de casilla impreso (p. ej. '2 0')
    se unen cuando forman un día válido.
    """
    # OCR manuscrito: "Z0" -> "20". Pre-normalizamos letras ambiguas
    # SOLO cuando la cadena es corta (parece manuscrito puro, sin
    # etiquetas como "DATE", "DAY", "OF CAPTAIN").
    if len(value) <= 8 and not re.search(r"[A-Z]{3,}", value):
        normalized = []
        for i, ch in enumerate(value):
            if (ch in _OCR_LETTER_TO_DIGIT_DICT
                    and i + 1 < len(value) and value[i + 1].isdigit()):
                normalized.append(_OCR_LETTER_TO_DIGIT_DICT[ch])
            else:
                normalized.append(ch)
        value = "".join(normalized)
    runs = re.findall(r"\d+", value)
    if not runs:
        return "", f"invalid day: {value}"
    if len(runs) == 2 and all(len(r) == 1 for r in runs):
        runs = ["".join(runs)]
    two = next((r for r in runs if len(r) == 2), None)
    if two is not None:
        digits = two
    else:
        one = [r for r in runs if len(r) == 1]
        digits = one[0] if len(one) == 1 else ""
    if not digits or len(digits) > 2:
        return "", f"invalid day: {value}"
    dia = int(digits)
    if not 1 <= dia <= 31:
        return "", f"invalid day: {value}"
    return digits, ""


def _canonical_month(mes: int) -> str:
    """Nombre canónico del mes (español preferido, inglés de respaldo)."""
    for nombre, numero in MESES.items():
        if numero == mes:
            return nombre
    return _CANONICA_MES.get(mes, str(mes))


def _month(value: str) -> Tuple[str, str]:
    """Mes manuscrito: normalmente ``MMM``, excepcionalmente numérico.

    La bitácora usa físicamente ``MMM``, pero algunos registros escasos usan
    ``1``-``12``. El número se acepta solo cuando todo el recorte es ese
    token; se anota para que no actúe como ancla de inferencia del libro.
    """
    raw = value.strip().upper()
    if not re.search(r"[A-Za-z]", raw):
        if not re.fullmatch(r"\d{1,2}", raw):
            return "", f"invalid month: {value}"
        numeric = int(raw)
        if not 1 <= numeric <= 12:
            return "", f"invalid month: {value}"
        return str(numeric), f"{NUMERIC_MONTH_NOTE}: {raw}"
    candidates = month_candidates(raw)
    if len(candidates) > 1:
        return "", f"Mes ambiguo: {value}"
    if not candidates:
        return "", f"invalid month: {value}"
    mes = candidates[0]
    letters = re.sub(r"[^A-Za-z]", "", raw.upper().translate(_OCR_CHAR_MAP))
    exact = any(nombre in letters for nombre in _MESES_LETRAS)
    normalized = re.sub(r"[^A-Z]", "", raw.translate(_MESES_CHAR_MAP))
    exact = exact or (len(normalized) == 3 and normalized in _MESES_LETRAS)
    # Incluso tres letras pueden ser una aproximación (JOC -> OCT).
    # La casilla completa no debe convertirla en ancla de otras páginas.
    note = "" if exact else f"month fuzzy: {value}"
    return _canonical_month(mes), note


def _year(value: str) -> Tuple[str, str]:
    """Año: 2 o 4 dígitos extraídos de una lectura que puede incluir
    etiquetas impresas ('Year YR 26' -> '26').

    Prefiere un run de 4 dígitos en la ventana plausible (de 2000 al año
    de la ejecución, ver ``app.utils.date_window``), luego
    el último run de 2; un run de 3 dígitos ('216') se conserva como ERROR
    para que el corrector por libro lo normalice contra el ganador del
    libro. Los runs de 4 dígitos absurdos (p. ej. '8313', '5102', restos
    del log_number) se rechazan devolviendo un valor vacío.

    Los dígitos separados por el separador de casilla impreso se unen:
    dos runs de 1 dígito forman el año de 2 ('2 6' -> '26') y cuatro
    runs de 1 dígito forman el de 4 ('2 0 2 6' -> '2026').
    """
    # OCR manuscrito suele leer "Z" en lugar de "2" en el primer dígito
    # del año ("Z6" -> "26"). Pre-normalizamos letras ambiguas a dígitos
    # cuando están pegadas a un run numérico (no en medio de palabras de
    # etiqueta como "YEAR" o "EMPLOYEE NUMBER OF CAPTAIN").
    normalized = []
    for i, ch in enumerate(value):
        if (ch in _OCR_LETTER_TO_DIGIT_DICT
                and i + 1 < len(value) and value[i + 1].isdigit()):
            normalized.append(_OCR_LETTER_TO_DIGIT_DICT[ch])
        else:
            normalized.append(ch)
    normalized = "".join(normalized)
    runs = re.findall(r"\d+", normalized)
    if not runs:
        return "", f"invalid year: {value}"
    if len(runs) == 2 and all(len(r) == 1 for r in runs):
        runs = ["".join(runs)]
    elif len(runs) == 4 and all(len(r) == 1 for r in runs):
        runs = ["".join(runs)]
    four = next((r for r in runs if len(r) == 4), None)
    if four:
        anio = int(four)
        if year_is_possible(anio):
            return four, ""
        return "", f"invalid year: {value}"
    twos = [r for r in runs if len(r) == 2]
    if twos:
        return twos[-1], ""
    threes = [r for r in runs if len(r) == 3]
    if threes:
        return threes[-1], f"invalid year: {threes[-1]}"
    return "", f"invalid year: {value}"


def _matricula_digits(token: str) -> Optional[str]:
    """Cuatro caracteres traducidos a dígitos, o None si alguno no lo es."""
    digits = []
    for char in token:
        if char.isdigit():
            digits.append(char)
        elif char in _MATRICULA_AMBIGUOUS:
            digits.append(_MATRICULA_AMBIGUOUS[char])
        else:
            return None
    return "".join(digits)


def _matricula_token(raw: str) -> Optional[Tuple[str, int]]:
    """Número de la matrícula entre el prefijo "HP" y el sufijo "CMP".

    El "1" de estas bitácoras es un palo sin base y el "7" lleva travesaño,
    así que el reconocedor devuelve ``HP-I7I7CMP`` o ``HP-1F17CMP`` para
    matrículas perfectamente legibles. Exigir cuatro dígitos seguidos
    descarta esas lecturas enteras, así que aquí se busca la ventana de
    cuatro caracteres traducibles a dígitos que mejor encaje en la
    estructura del campo: prefijo de uno a tres caracteres y, detrás, el
    sufijo ``CMP``/``WWP`` o el final del texto.

    Returns:
        (cuatro dígitos, cuántos eran ya dígitos) o None si ninguna ventana
        encaja con suficiente evidencia.
    """
    best: Optional[Tuple[int, str, int]] = None
    for start in range(len(raw) - 3):
        digits = _matricula_digits(raw[start:start + 4])
        if digits is None:
            continue
        real = sum(char.isdigit() for char in raw[start:start + 4])
        if real < 2:
            # Con un solo dígito real la ventana es indistinguible del
            # ruido: "GLOB" no es una matrícula.
            continue
        suffix = raw[start + 4:]
        if suffix[:1] in _MATRICULA_SUFFIX_HEAD:
            suffix_score = 3
        elif not suffix:
            suffix_score = 1
        elif suffix[0].isdigit():
            # Un dígito suelto detrás delata una ventana ejecución.
            suffix_score = -3
        else:
            suffix_score = -1
        prefix_score = (0, 1, 2, 1)[start] if start <= 3 else -2
        score = suffix_score + prefix_score + real
        if best is None or score > best[0]:
            best = (score, digits, real)
    if best is None:
        return None
    score, digits, real = best
    if score >= 4 or (real == 4 and score >= 3):
        return digits, real
    return None


def _matricula(value: str) -> Tuple[str, str]:
    """Normaliza la matrícula a HP-XXXXCMP (o HP-XXXXWWP).

    Acepta: 1717, hp1717, 1717cmp, HP-1717, hp-1717-cmp, 1717 CMP...
    Excepciones conocidas: HP-1990WWP no lleva CMP; el HP-1522, que se
    escribe de las dos maneras, siempre sale como HP-1522CMP.

    Si no hay cuatro dígitos seguidos se reconstruye el número a partir de
    los caracteres que el OCR confunde con dígitos manuscritos
    (``HP-I7I7CMP`` -> 1717), anotando la lectura para que quede auditable.
    La lectura es "débil" cuando el número solo puede reconstruirse con
    dígitos dispersos (p. ej. "wAT 1Hp i712cmp" -> 1712); en ese caso el
    pipeline reduce la confianza y el corrector por libro decide.

    Cuando no hay un número de 4 dígitos recuperable, devuelve un valor
    vacío (no el texto crudo del OCR): el corrector por libro lo inferirá
    o la página queda sin detectar, pero la basura nunca llega al CSV.
    """
    raw = re.sub(r"[^A-Z0-9]", "", value.upper())
    match = re.search(r"(?<!\d)\d{4}(?!\d)", raw)
    if match:
        numero = match.group(0)
        note = ""
    else:
        token = _matricula_token(raw)
        if token is not None:
            numero, real = token
            note = "" if real == 4 else AMBIGUOUS_MATRICULA_NOTE
        else:
            digits = re.findall(r"\d", raw)
            if len(digits) == 4:
                numero = "".join(digits)
                note = WEAK_MATRICULA_NOTE
            else:
                return "", "registration without 4-digit number"
    lleva_wwp = ("WWP" in raw or numero in WWP_ONLY)
    sufijo = "WWP" if lleva_wwp and numero not in CMP_ONLY else "CMP"
    return f"HP-{numero}{sufijo}", note


# ── Número de vuelo ────────────────────────────────────────────────────
# El casillero de vuelo no tiene un formato fijo. Escrito a mano trae un
# vuelo numerado (``CM`` y tres cifras, o solo las cifras) o una de las
# palabras cortas que usa el mantenimiento. Comparado contra los escaneos, el
# reconocedor casi nunca se equivoca de forma: se equivoca en una letra
# (``Tek``, ``TLK``, ``TCB`` son TCK) o confunde una cifra manuscrita con una
# letra (``7S8`` es 758, ``CMIO3`` es CM103). Por eso la lectura se ajusta a
# la palabra más parecida o se reconstruye alrededor de las cifras, y lo que
# no encaja en ninguna de las dos formas se deja vacío: un valor inventado en
# el CSV cuesta más que una celda por indexar a mano.
FLIGHT_CODES = ("TCK", "CCK", "SPV", "SVC", "SUP", "MTC", "SV")
# Clases de trazo que el reconocedor confunde en estas bitácoras: la S con el
# 5, la P con el 9 y la R, la T con el 7 y la J, la U con la V. Comparar los
# códigos por clase y no letra a letra es lo que permite reconocer "S9V",
# "SRV" o "52V" como el SPV que está escrito en la página, y "JCK" como TCK
# sin tener que aflojar la distancia y confundir "ZCC" (que es un 700) con
# CCK.
_FLIGHT_STROKE_GROUPS = (
    "0OQD", "1IL", "2Z", "3E", "4YA", "5S", "6G", "7TFJ", "8B", "9PR", "UV",
)
_FLIGHT_STROKE_CLASS = {
    char: group[0]
    for group in _FLIGHT_STROKE_GROUPS
    for char in group
}
FLIGHT_PREFIX = "CM"
FLIGHT_FUZZY_NOTE = "flight number matched against the logbook vocabulary"
_FLIGHT_LABEL_RE = re.compile(r"\b(?:FLT|FLIGHT|NO|CHECK)\b")
# Letras que el reconocedor devuelve en lugar de una cifra manuscrita.
_FLIGHT_LETTER_TO_DIGIT = {
    **{char.upper(): digit
       for char, digit in _OCR_LETTER_TO_DIGIT_DICT.items()},
    # El 1 sin base se lee como L y el 4 con la cabeza abierta como Y:
    # "cmloy" es CM104 y "73y" es 734.
    "L": "1",
    "Y": "4",
    # El 7 va con travesaño y vuelve como T o como F, igual que en la
    # matrícula ("cMTSO" es CM750).
    "T": "7",
    "F": "7",
}
# Detrás de un ``CM`` leído entero no puede haber letras: lo que sigue es el
# número. Ahí valen además las confusiones que en cualquier otro sitio serían
# ambiguas (la A de "CMPBA3" (CM843) es un 4, pero la de "A123" es el
# prefijo del vuelo).
_FLIGHT_TAIL_TO_DIGIT = {
    **_FLIGHT_LETTER_TO_DIGIT,
    "A": "4",
    "D": "0",
}
# Estas solo valen como cifra cuando el trazo queda encerrado entre dos
# números: la C de ``4C2`` es un cero, la de ``CM103`` no.
_FLIGHT_INNER_LETTER_TO_DIGIT = {"C": "0", "D": "0"}
# Un vuelo numerado son tres cifras, a veces cuatro. Un tramo más largo ya no
# es un vuelo: es el número de bitácora que se coló en el recorte.
_FLIGHT_MIN_DIGITS = 3
_FLIGHT_MAX_DIGITS = 4
# ``CM`` leído entero: lo que va detrás es el número del vuelo, sean las
# tres cifras de siempre o no.
_FLIGHT_PREFIXED_RE = re.compile(r"CMP?(.{1,4})$")


def _flight_code_in(compact: str) -> Optional[str]:
    """Palabra del vocabulario contenida tal cual en la lectura.

    Se buscan las más largas primero para que ``SVCVISIT`` no se quede en
    ``SV``. Un dígito pegado detrás forma parte del código (``SV3``); las
    letras de alrededor son ruido del recorte.
    """
    for code in sorted(FLIGHT_CODES, key=len, reverse=True):
        index = compact.find(code)
        if index < 0:
            continue
        tail = compact[index + len(code):]
        return f"{code}{tail[0]}" if tail[:1].isdigit() else code
    return None


def _flight_digit_run(compact: str) -> Optional[Tuple[int, str]]:
    """(posición, cifras) del tramo que puede leerse como número de vuelo.

    Gana el tramo con más cifras de verdad: entre ``BSO`` (ninguna) y
    ``7S8`` (dos) solo el segundo tiene evidencia de ser un número.
    """
    mapped: List[Optional[str]] = []
    for index, char in enumerate(compact):
        if char.isdigit():
            mapped.append(char)
            continue
        digit = _FLIGHT_LETTER_TO_DIGIT.get(char)
        if (
            digit is None
            and 0 < index < len(compact) - 1
            and compact[index - 1].isdigit()
            and compact[index + 1].isdigit()
        ):
            digit = _FLIGHT_INNER_LETTER_TO_DIGIT.get(char)
        mapped.append(digit)

    best: Optional[Tuple[Tuple[int, int, int], Tuple[int, str]]] = None
    start = 0
    while start < len(mapped):
        if mapped[start] is None:
            start += 1
            continue
        end = start
        while end < len(mapped) and mapped[end] is not None:
            end += 1
        length = end - start
        real = sum(char.isdigit() for char in compact[start:end])
        if _FLIGHT_MIN_DIGITS <= length <= _FLIGHT_MAX_DIGITS and real:
            score = (real, length, start)
            if best is None or score > best[0]:
                best = (score, (start, "".join(mapped[start:end])))
        start = end
    return best[1] if best else None


def _flight_as_digits(text: str) -> Optional[str]:
    """El tramo entero leído como cifras, o ``None`` si alguna no lo es."""
    digits = []
    for char in text:
        if char.isdigit():
            digits.append(char)
            continue
        digit = _FLIGHT_TAIL_TO_DIGIT.get(char)
        if digit is None:
            return None
        digits.append(digit)
    return "".join(digits)


def _same_stroke(left: str, right: str) -> bool:
    """Los dos caracteres salen del mismo trazo manuscrito."""
    return (
        _FLIGHT_STROKE_CLASS.get(left, left)
        == _FLIGHT_STROKE_CLASS.get(right, right)
    )


def _flight_nearest_code(token: str) -> Optional[str]:
    """Palabra del vocabulario a un solo trazo de distancia, sin empates.

    Con la misma cantidad de caracteres se comparan posición a posición por
    clase de trazo, que es como falla el reconocedor en este campo; con
    longitudes distintas queda la distancia de edición. Un empate no se
    resuelve: entre dos códigos igual de parecidos, acertar sería suerte y
    la celda se deja para revisión.
    """
    if len(token) < 2:
        return None
    ranked = sorted(
        (
            sum(
                0 if _same_stroke(left, right) else 1
                for left, right in zip(token, code)
            )
            if len(code) == len(token)
            else _levenshtein(token, code),
            # A igualdad de trazos gana el código que tiene los mismos
            # caracteres que la lectura: sobra menos explicación que en uno
            # al que hay que quitarle o ponerle una letra.
            abs(len(code) - len(token)),
            code,
        )
        for code in FLIGHT_CODES
    )
    best = ranked[0][:2]
    if best[0] > 1:
        return None
    tied = [code for *distance, code in ranked if tuple(distance) == best]
    return tied[0] if len(tied) == 1 else None


def _flight_number(value: str) -> Tuple[str, str]:
    """Normaliza el número de vuelo manuscrito junto a la matrícula.

    Se resuelve en este orden:

    1. La lectura contiene una palabra del vocabulario (``TCK``, ``SPV``…):
       esa palabra es el valor, aunque el recorte traiga números al lado.
    2. La lectura es solo cifras (hasta cuatro): es un vuelo numerado.
    3. Hay un tramo de tres o cuatro cifras recuperables: es el número del
       vuelo. Cualquier letra delante se normaliza a ``CM``, salvo la ``A``,
       que sí se escribe en el casillero.
    4. Sin cifras, las letras se ajustan a la palabra más parecida del
       vocabulario si está a un solo trazo de distancia.

    Lo que no cae en ninguno de los cuatro casos se devuelve vacío.
    """
    raw = _FLIGHT_LABEL_RE.sub(" ", value.upper())
    compact = re.sub(r"[^A-Z0-9]", "", raw)
    if not compact:
        return "", "empty flight number"

    code = _flight_code_in(compact)
    if code is not None:
        return code, "" if code == compact else FLIGHT_FUZZY_NOTE

    if compact.isdigit():
        # Un vuelo no es cero: ese trazo es una marca del casillero.
        if len(compact) <= _FLIGHT_MAX_DIGITS and set(compact) != {"0"}:
            return compact, ""
        return "", f"invalid flight number: {value}"

    run = _flight_digit_run(compact)
    if run is not None:
        start, digits = run
        letters = re.sub(r"[^A-Z]", "", compact[:start])
        if not letters:
            number = digits
        elif letters == "A":
            number = f"A{digits}"
        else:
            number = f"{FLIGHT_PREFIX}{digits}"
        return number, "" if number == compact else FLIGHT_FUZZY_NOTE

    # Un "CM" leído limpio es evidencia por sí solo: lo que va detrás es el
    # número del vuelo. Lo habitual son tres cifras, pero no se exige la
    # cifra exacta ni que el reconocedor las devolviera como dígitos: con el
    # prefijo delante, "CMPlOS" es CM105 y "CM 40" es CM40. Con una o dos
    # cifras sí se pide al menos un dígito de verdad, porque ahí el tramo es
    # demasiado corto para distinguirlo de un resto de texto.
    prefixed = _FLIGHT_PREFIXED_RE.fullmatch(compact)
    if prefixed is not None:
        tail = prefixed.group(1)
        digits = _flight_as_digits(tail)
        if digits is not None and (
            len(tail) >= _FLIGHT_MIN_DIGITS
            or any(char.isdigit() for char in tail)
        ):
            return f"{FLIGHT_PREFIX}{digits}", FLIGHT_FUZZY_NOTE

    # El código se compara entero, con los dígitos que el reconocedor haya
    # metido dentro: "S9V" y "52V" son el SPV de la página, no una palabra
    # más un número. La cifra que va detrás sí se aparta antes de comparar,
    # porque forma parte del código ("SV2", "SVC2").
    token, suffix = compact, ""
    if len(token) > 2 and token[-1].isdigit():
        token, suffix = token[:-1], token[-1]
    nearest = _flight_nearest_code(token)
    if nearest is not None:
        return f"{nearest}{suffix}", FLIGHT_FUZZY_NOTE
    return "", f"invalid flight number: {value}"


def _date(value: str) -> Tuple[str, str]:
    """Parsea fechas en múltiples formatos a YYYY/MM/dd.

    Soporta: 15 jul 26 | 15 7 26 | 15-07-26 | 15/07/26 | 15JUL26 |
    07-15-26 (mes/día/año).
    """
    raw = value.strip().upper()
    meses = "|".join(MESES)

    # DD MMM AA / DDMMMAA
    m = re.search(r"(\d{1,2})\s*(" + meses + r")\w*\s*(\d{2,4})", raw)
    if not m:
        m = re.search(r"(\d{1,2})(" + meses + r")(\d{2,4})", raw)
    if m:
        dia, mes, anio = int(m.group(1)), MESES[m.group(2)], int(m.group(3))
        return _build_iso(dia, mes, anio, value)

    # DD/MM/AA | DD-MM-AA | DD MM AA (se prueba día/mes y mes/día)
    m = re.search(
        r"(\d{1,2})\s*[\/\-\s]\s*(\d{1,2})\s*[\/\-\s]\s*(\d{2,4})", raw
    )
    if m:
        a, b, anio = int(m.group(1)), int(m.group(2)), int(m.group(3))
        for dia, mes in ((a, b), (b, a)):
            if 1 <= mes <= 12 and 1 <= dia <= 31:
                iso = _build_iso(dia, mes, anio, value)
                if iso[0] != value:
                    return iso
        return "", f"invalid date: {value}"

    return "", f"unrecognized date: {value}"


def _build_iso(dia: int, mes: int, anio: int,
               original: str) -> Tuple[str, str]:
    anio = _normalize_year(anio)
    if anio is None:
        return original, f"invalid year: {original}"
    try:
        return datetime(anio, mes, dia).strftime("%Y/%m/%d"), ""
    except ValueError:
        return original, f"invalid date: {original}"


def _digits(value: str) -> Tuple[str, str]:
    """Conserva solo dígitos (p. ej. horas o cantidades)."""
    digits = re.sub(r"[^\d]", "", value)
    return (digits, "") if digits else (value, "no digits")


def _char(value: str) -> Tuple[str, str]:
    """Un solo carácter (dígito o letra) de una celda de casilla.

    Cada celda de la banda de fecha (day_1..year_2) contiene un único
    carácter manuscrito; el OCR global puede devolver ruido alrededor
    (separadores, rótulos), así que se conserva solo el primer carácter
    alfanumérico y las letras se pasan a mayúscula para unificar las
    celdas de mes. La unión de las celdas en día/mes/año la hace el
    pipeline (``_join_char_fields``).
    """
    text = value.strip()
    if not text:
        return "", "empty char cell"
    ch = text[0].upper()
    if not (ch.isascii() and (ch.isalpha() or ch.isdigit())):
        return "", f"invalid char: {value}"
    return ch, ""


def combine_date(day_value: Optional[str], month_value: Optional[str],
                 year_value: Optional[str]) -> Tuple[str, str]:
    """Combina día/mes/año OCR separados en una fecha YYYY/MM/dd.

    Args:
        day_value: Valor OCR del día (p. ej. "15").
        month_value: Valor OCR del mes (p. ej. "7", "07" o "JUL").
        year_value: Valor OCR del año (p. ej. "26" o "2026").

    Returns:
        (fecha normalizada, nota). La nota vacía indica éxito; si hay
        error, el primer elemento no es una fecha válida.
    """
    if not (day_value and month_value and year_value):
        return year_value or "", "incomplete date"

    d = re.sub(r"[^\d]", "", day_value)
    y = re.sub(r"[^\d]", "", year_value)
    raw = f"{d}/{month_value}/{y}"
    if not (d and y):
        return raw, f"date without digits: {day_value}/{month_value}/{year_value}"

    dia, mes = int(d), _parse_month(month_value)
    if mes is None:
        return raw, f"invalid month: {month_value}"
    anio = _normalize_year(int(y))
    if anio is None:
        return raw, f"invalid year: {year_value}"
    if not 1 <= dia <= 31:
        return raw, f"invalid day: {day_value}"
    try:
        return datetime(anio, mes, dia).strftime("%Y/%m/%d"), ""
    except ValueError:
        return raw, f"invalid date: {day_value}/{month_value}/{year_value}"


POSTPROCESSORS: Dict[str, Callable[[str], Tuple[str, str]]] = {
    "matricula": _matricula,
    "flight_number": _flight_number,
    "date": _date,
    "digits": _digits,
    "day": _day,
    "month": _month,
    "year": _year,
    "char": _char,
}


def apply_postprocess(
    field_id: str, name: str, value: str
) -> Tuple[str, str]:
    """Aplica un postprocesador registrado.

    Args:
        field_id: Id del campo (para el log).
        name: Nombre del postprocesador.
        value: Valor OCR crudo.

    Returns:
        (valor procesado, nota). La nota vacía indica éxito.
    """
    processor = POSTPROCESSORS.get(name)
    if processor is None:
        logger.warning(f"Postprocesador '{name}' no registrado "
                       f"(campo {field_id})")
        return value, f"unknown postprocessor: {name}"
    processed, note = processor(value)
    if note:
        logger.debug(f"Postproceso {name} ({field_id}): {note}")
    return processed, note
