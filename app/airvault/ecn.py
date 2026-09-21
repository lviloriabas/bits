"""Categorias consultadas en el catalogo MXDocs con Edge el 8 de septiembre de 2026."""

import re
import unicodedata

CAMPOS_ECN = (9692,)
_CAMPOS_SECUNDARIOS = (9781, 9782)
_PRIORIDAD = (
    "captain_signature", "captain_license", "pilot_signature",
    "technician_signature", "technician_license",
)
_PREFIJO = "DISCREPANCY NOTE: "
RAZON_CAPITAN = "MISSING SIGNATURE AND/OR LICENSE NUMBER OF THE CAPTAIN"
RAZON_PILOTO = "MISSING SIGNATURE AND/OR LICENSE NUMBER OF THE PILOT"
RAZON_FIRMA_TECNICO = "MISSING TECHNICIAN SIGNATURE"
RAZON_LICENCIA = "MISSING LICENSE NUMBER"
_RAZONES = {
    RAZON_CAPITAN, RAZON_PILOTO, RAZON_FIRMA_TECNICO, RAZON_LICENCIA,
}
RAZON_POR_CAMPO = {
    "captain_signature": _PREFIJO + RAZON_CAPITAN,
    "captain_license": _PREFIJO + RAZON_CAPITAN,
    "pilot_signature": _PREFIJO + RAZON_PILOTO,
    "technician_signature": _PREFIJO + RAZON_FIRMA_TECNICO,
    "technician_license": _PREFIJO + RAZON_LICENCIA,
}


def razon_de_discrepancia(tipo, campos):
    """Nombre del catálogo que debe aparecer en ``disc_reason``."""
    faltantes = set(campos)
    if tipo == "mantenimiento":
        if faltantes & {"pilot_signature", "technician_signature"}:
            return RAZON_FIRMA_TECNICO
        if "technician_license" in faltantes:
            return RAZON_LICENCIA
        return ""
    if faltantes & {"captain_signature", "captain_license"}:
        return RAZON_CAPITAN
    if "pilot_signature" in faltantes:
        return RAZON_PILOTO
    return ""


def normalizar_razon(resumen):
    """Devuelve la razón sin prefijo, incluida la de entregas anteriores."""
    original = str(resumen or "").strip()
    mayusculas = original.upper()
    if mayusculas.startswith(_PREFIJO):
        mayusculas = mayusculas.removeprefix(_PREFIJO).strip()
    if mayusculas in _RAZONES:
        return mayusculas

    texto = "".join(
        c for c in unicodedata.normalize("NFD", original)
        if not unicodedata.combining(c)
    ).lower().strip()
    mantenimiento = (
        texto.startswith("correccion escrita: ")
        or "(entrada de mantenimiento)" in texto
        or "(correccion escrita)" in texto
    )
    campos = campos_del_resumen(original)
    return razon_de_discrepancia(
        "mantenimiento" if mantenimiento else "vuelo", campos
    )


def razones_ecn(campos, resumen=""):
    """Una sola falta confirmada: capitan, piloto y tecnico, en ese orden."""
    razon = normalizar_razon(resumen)
    if razon:
        return [_PREFIJO + razon]
    faltantes = set(campos)
    for campo in _PRIORIDAD:
        if campo in faltantes:
            return [RAZON_POR_CAMPO[campo]]
    return []


def campos_del_resumen(resumen):
    """Compatibilidad con entregas anteriores que solo guardaban la frase generada."""
    original = str(resumen or "").strip()
    formal = original.upper().removeprefix(_PREFIJO).strip()
    por_razon = {
        RAZON_CAPITAN: ["captain_signature", "captain_license"],
        RAZON_PILOTO: ["pilot_signature"],
        RAZON_FIRMA_TECNICO: ["technician_signature"],
        RAZON_LICENCIA: ["technician_license"],
    }
    if formal in por_razon:
        return por_razon[formal]
    texto = "".join(c for c in unicodedata.normalize("NFD", original)
                    if not unicodedata.combining(c)).lower().strip()
    texto = texto.removeprefix("correccion escrita: ")
    texto = re.sub(
        r" \((entrada de mantenimiento|correccion escrita|"
        r"tipo de pagina incierto)\)", "", texto,
    )
    match = re.fullmatch(r"faltan? (.+)", texto)
    if not match:
        return []
    nombres = {
        "firma de piloto": "pilot_signature",
        "firma de capitan": "captain_signature",
        "licencia de capitan": "captain_license",
        "licencia del capitan": "captain_license",
        "firma de tecnico": "technician_signature",
        "licencia de tecnico": "technician_license",
    }
    partes = re.split(r", | y ", match[1])
    return [nombres[p] for p in partes] if all(p in nombres for p in partes) else []


def conservar_razones(valores, remotos):
    """Solo escribe ECN Reason y respeta su anotacion existente, si la hay."""
    resultado = {c: v for c, v in valores.items() if c not in _CAMPOS_SECUNDARIOS}
    campo = CAMPOS_ECN[0]
    existente = str(remotos.get(campo, "") or "").strip()
    if resultado.get(campo) and existente:
        resultado[campo] = existente
    return resultado
