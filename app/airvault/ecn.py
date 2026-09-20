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
RAZON_POR_CAMPO = {
    "captain_signature": _PREFIJO + "MISSING SIGNATURE AND/OR LICENSE NUMBER OF THE PILOT",
    "captain_license": _PREFIJO + "MISSING SIGNATURE AND/OR LICENSE NUMBER OF THE PILOT",
    "pilot_signature": _PREFIJO + "MISSING TECHNICIAN SIGNATURE",
    "technician_signature": _PREFIJO + "MISSING TECHNICIAN SIGNATURE",
    "technician_license": _PREFIJO + "MISSING LICENSE NUMBER",
}


def razones_ecn(campos):
    """Una sola falta confirmada: capitan, piloto y tecnico, en ese orden."""
    faltantes = set(campos)
    for campo in _PRIORIDAD:
        if campo in faltantes:
            return [RAZON_POR_CAMPO[campo]]
    return []


def campos_del_resumen(resumen):
    """Compatibilidad con entregas anteriores que solo guardaban la frase generada."""
    texto = "".join(c for c in unicodedata.normalize("NFD", str(resumen or ""))
                    if not unicodedata.combining(c)).lower().strip()
    texto = texto.removeprefix("correccion escrita: ")
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
