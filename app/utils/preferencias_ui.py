"""Preferencias de la interfaz que sobreviven al cierre del programa.

De momento solo el tema. Vive en un JSON junto al programa, como
``fleet.json`` o ``important_fields.json``, y no en QSettings: la carpeta
completa tiene que poder copiarse a otro Windows y llegar con lo mismo que
tenia, sin depender del registro ni del perfil del usuario.

Nada de esto es critico: si el archivo falta, esta a medio escribir o lo dejo
otra version, se sigue con el valor por omision en vez de impedir que la
ventana se abra.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from app.utils.portable import app_root

INTERFAZ_FILENAME = "interfaz.json"
_CLAVE_TEMA = "tema"


def _ruta(raiz: Path | str | None = None) -> Path:
    return Path(raiz or app_root()) / INTERFAZ_FILENAME


def _leer(raiz: Path | str | None = None) -> dict:
    ruta = _ruta(raiz)
    if not ruta.is_file():
        return {}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.debug(f"No se pudo leer {ruta.name}: {error}")
        return {}
    return datos if isinstance(datos, dict) else {}


def leer_tema(raiz: Path | str | None = None) -> str | None:
    """El tema guardado, o ``None`` si nadie ha elegido todavia."""
    valor = _leer(raiz).get(_CLAVE_TEMA)
    return valor if isinstance(valor, str) and valor else None


def guardar_tema(nombre: str, raiz: Path | str | None = None) -> bool:
    """Anota el tema elegido sin perder el resto del archivo."""
    datos = _leer(raiz)
    datos[_CLAVE_TEMA] = str(nombre)
    ruta = _ruta(raiz)
    try:
        ruta.write_text(
            json.dumps(datos, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        logger.debug(f"No se pudo escribir {ruta.name}: {error}")
        return False
    return True
