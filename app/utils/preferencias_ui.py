"""Preferencias de la interfaz que sobreviven al cierre del programa.

Todo lo que alguien elige moviendo un control (el tema, las casillas de
salida, los filtros de cada ventana, la plantilla con la que se trabaja)
vive en un JSON junto al programa, como ``fleet.json`` o
``important_fields.json``, y no en QSettings: la carpeta completa tiene que
poder copiarse a otro Windows y llegar con lo mismo que tenia, sin depender
del registro ni del perfil del usuario.

El archivo no se versiona. Cada instalacion lo reescribe sola en cuanto
alguien toca un control, asi que tenerlo en el repositorio convertia cada
casilla marcada en una modificacion pendiente y bloqueaba el pull en la otra
maquina. Los valores de partida estan en el codigo, junto al control que los
usa, no aqui.

Nada de esto es critico: si el archivo falta, esta a medio escribir o lo dejo
otra version, se sigue con el valor por omision en vez de impedir que la
ventana se abra.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

from app.utils.portable import app_root

INTERFAZ_FILENAME = "interfaz.json"
_CLAVE_TEMA = "tema"

# La ventana principal, la de AirVault y el visor escriben aqui desde hilos
# distintos. Sin candado, dos guardados a la vez dejaban el archivo con la
# mitad de las opciones.
_CANDADO = threading.Lock()


def ruta_preferencias(raiz: Path | str | None = None) -> Path:
    """Donde vive el archivo.

    Se resuelve en cada llamada y no al importar: es el unico punto por
    el que pasan la lectura y la escritura, y por el que las pruebas lo
    mandan a otro sitio.
    """
    return Path(raiz or app_root()) / INTERFAZ_FILENAME


def _leer(raiz: Path | str | None = None) -> dict:
    ruta = ruta_preferencias(raiz)
    if not ruta.is_file():
        return {}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.debug(f"No se pudo leer {ruta.name}: {error}")
        return {}
    return datos if isinstance(datos, dict) else {}


def _escribir(valores: Mapping[str, Any], raiz: Path | str | None = None) -> bool:
    """Cambia unas claves sin perder las demas, y de una sola pieza.

    Se relee, se sustituye lo que cambia y se reemplaza el archivo entero:
    un corte a mitad de escritura deja el anterior, no uno truncado que en
    el siguiente arranque se leeria como «sin preferencias».
    """
    if not valores:
        return False
    ruta = ruta_preferencias(raiz)
    try:
        with _CANDADO:
            datos = _leer(raiz)
            datos.update(valores)
            ruta.parent.mkdir(parents=True, exist_ok=True)
            temporal = ruta.with_name(f"{ruta.name}.tmp")
            temporal.write_text(
                json.dumps(datos, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporal, ruta)
    except (OSError, TypeError, ValueError) as error:
        logger.debug(f"No se pudo escribir {ruta.name}: {error}")
        return False
    return True


def leer_opcion(
    clave: str, defecto: Any = None, raiz: Path | str | None = None
) -> Any:
    """Lo guardado para esa clave, o el defecto si nadie la ha tocado.

    Devolver el defecto y no ``None`` a secas importa en las casillas que
    nacen marcadas: «no hay preferencia» no es «desmarcada».
    """
    valor = _leer(raiz).get(clave)
    return defecto if valor is None else valor


def guardar_opcion(
    clave: str, valor: Any, raiz: Path | str | None = None
) -> bool:
    """Anota una opcion conservando el resto del archivo."""
    return _escribir({clave: valor}, raiz)


def leer_tema(raiz: Path | str | None = None) -> str | None:
    """El tema guardado, o ``None`` si nadie ha elegido todavia."""
    valor = leer_opcion(_CLAVE_TEMA, raiz=raiz)
    return valor if isinstance(valor, str) and valor else None


def guardar_tema(nombre: str, raiz: Path | str | None = None) -> bool:
    """Anota el tema elegido sin perder el resto del archivo."""
    return guardar_opcion(_CLAVE_TEMA, str(nombre), raiz)
