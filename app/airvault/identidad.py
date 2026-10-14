"""Quien es en AirVault la persona que usa este programa.

AirVault admite un solo dueno por batch y la cola dice quien lo tiene
abierto. Un batch abierto por otro usuario no se toca. Uno que figura
abierto por el usuario de este programa es casi siempre un bloqueo que el
propio programa dejo atras (se cerro a media escritura, AirVault tomo el
batch y contesto con un error): ese se suelta y se sigue trabajando.

Para distinguirlos hay que saber quien es uno, y AirVault no lo dice en
ninguna respuesta que el programa ya pida. Se averigua tomando un instante
un batch libre de la propia cola y leyendo en ella quien figura como dueno.
Queda guardado por usuario de Windows para la proxima vez que no haya
ningun batch libre con que averiguarlo.
"""

from __future__ import annotations

import getpass
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from loguru import logger

from app.airvault.session import (ErrorDeConexion, ErrorDeSesion,
                                  SesionCancelada)

ARCHIVO = "airvault_usuario.json"

# Batches libres que se prueban como mucho para averiguar la identidad.
CANDIDATOS_MAXIMOS = 3

_FALLOS_DE_CAMINO = (ErrorDeSesion, ErrorDeConexion, SesionCancelada)

# Lo averiguado con la sesion en curso, por carpeta de la instalacion. Lo
# guardado en disco es de una sesion anterior y solo se usa a falta de esto.
_DE_ESTA_SESION: Dict[str, "Identidad"] = {}


def _id_util(valor: object) -> str:
    """El identificador de usuario, o vacio si la cola no lo trae."""
    texto = str(valor or "").strip()
    return "" if texto.casefold() in ("", "0", "none", "null") else texto


@dataclass(frozen=True)
class Identidad:
    """El usuario de AirVault con el que trabaja este programa."""

    id: str
    nombre: str

    def tiene(self, lote) -> bool:
        """Si ese batch esta abierto a nombre de este usuario."""
        quien = str(getattr(lote, "bloqueado_por", "") or "").strip()
        if not quien:
            return False
        su_id = _id_util(getattr(lote, "bloqueado_por_id", ""))
        if su_id and self.id:
            return su_id == self.id
        return bool(self.nombre) and quien.casefold() == self.nombre.casefold()


def _usuario_de_windows() -> str:
    try:
        return getpass.getuser().strip().casefold()
    except Exception:  # noqa: BLE001 - sin usuario no hay con que guardar
        return ""


def cargar(carpeta: Path | str) -> Optional[Identidad]:
    """La identidad guardada para quien usa este equipo, o ``None``."""
    usuario = _usuario_de_windows()
    try:
        datos = json.loads((Path(carpeta) / ARCHIVO).read_text(encoding="utf-8"))
        propio = datos[usuario]
        nombre = str(propio.get("nombre", "")).strip()
        ident = _id_util(propio.get("id", ""))
    except Exception:  # noqa: BLE001 - sin archivo o ilegible: no se sabe
        return None
    return Identidad(ident, nombre) if (ident or nombre) else None


def guardar(carpeta: Path | str, identidad: Identidad) -> None:
    usuario = _usuario_de_windows()
    if not usuario:
        return
    ruta = Path(carpeta) / ARCHIVO
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        if not isinstance(datos, dict):
            datos = {}
    except Exception:  # noqa: BLE001 - se empieza de nuevo
        datos = {}
    datos[usuario] = {"id": identidad.id, "nombre": identidad.nombre}
    try:
        ruta.write_text(
            json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("No se pudo guardar el usuario de AirVault: {}", exc)


def averiguada(carpeta: Path | str) -> bool:
    """Si esta sesion ya averiguo el usuario preguntando a AirVault."""
    return str(carpeta) in _DE_ESTA_SESION


def conocida(carpeta: Path | str) -> Optional[Identidad]:
    """Lo que se sabe sin preguntar nada a AirVault."""
    return _DE_ESTA_SESION.get(str(carpeta)) or cargar(carpeta)


def _fila(cliente, batch_id: str, nombre: str):
    """La fila de ese batch en la cola, o ``None`` si no aparece."""
    try:
        lotes = list(cliente.listar_lotes(nombre))
    except TypeError:
        lotes = list(cliente.listar_lotes())
    buscado = str(batch_id).strip().upper()
    for lote in lotes:
        if str(lote.batch_id).strip().upper() == buscado:
            return lote
    return None


def averiguar(
    cliente, candidatos: Sequence[Tuple[str, str]],
) -> Optional[Identidad]:
    """Toma un batch libre, lee quien figura como dueno y lo suelta.

    ``candidatos`` son pares ``(batch_id, nombre)`` de los batches propios.
    Solo se toma uno que la cola muestre libre: abrir uno ocupado deja la
    peticion colgada. ``None`` si no hay ninguno libre o no se pudo leer.

    Se lee la cola entera una vez para saber cuales siguen en ella y estan
    libres. Probar los primeros de la lista sin mirar eso no servia: los
    mas antiguos ya habian salido de la cola y se agotaban los intentos
    antes de llegar a uno que si estaba.
    """
    propios = {
        str(batch_id).strip().upper(): nombre for batch_id, nombre in candidatos
    }
    if not propios:
        logger.info(
            "No se pudo averiguar el usuario de AirVault: no hay otro batch "
            "propio con que comprobarlo"
        )
        return None
    libres = [
        fila for fila in cliente.listar_lotes()
        if str(fila.batch_id).strip().upper() in propios
        and not str(fila.bloqueado_por or "").strip()
    ]
    if not libres:
        logger.info(
            "No se pudo averiguar el usuario de AirVault: ninguno de los {} "
            "batches propios esta libre en la cola", len(propios),
        )
        return None
    for libre in libres[:CANDIDATOS_MAXIMOS]:
        batch_id = libre.batch_id
        nombre = propios[str(batch_id).strip().upper()]
        dueno = None
        try:
            cliente.abrir_lote(batch_id)
            dueno = _fila(cliente, batch_id, nombre)
        except _FALLOS_DE_CAMINO:
            raise
        except Exception as exc:  # noqa: BLE001 - se prueba con otro
            logger.info("No se pudo tomar el batch {} para saber el usuario "
                        "de AirVault: {}", batch_id, exc)
        finally:
            # Se suelta siempre: la apertura pudo tomar el batch aunque
            # despues fallara.
            try:
                cliente.cerrar_lote(batch_id)
            except Exception as exc:  # noqa: BLE001 - es limpieza
                logger.warning(
                    "No se pudo soltar el batch {} despues de averiguar el "
                    "usuario de AirVault: {}", batch_id, exc,
                )
        quien = str(getattr(dueno, "bloqueado_por", "") or "").strip()
        if quien:
            return Identidad(_id_util(getattr(dueno, "bloqueado_por_id", "")), quien)
        logger.info(
            "El batch {} se tomo para saber el usuario de AirVault, pero la "
            "cola no dijo quien lo tenia", batch_id,
        )
    return None


def del_usuario(
    cliente, carpeta: Path | str, candidatos: Sequence[Tuple[str, str]],
) -> Optional[Identidad]:
    """La identidad del usuario, averiguandola si esta sesion aun no la sabe.

    Lo averiguado ahora manda sobre lo guardado: el archivo pudo dejarlo
    otra cuenta de AirVault usada desde el mismo equipo. Si no hay batch
    libre con que averiguarlo, vale lo guardado.
    """
    clave = str(carpeta)
    if clave in _DE_ESTA_SESION:
        return _DE_ESTA_SESION[clave]
    hallada = averiguar(cliente, candidatos)
    if hallada is None:
        return cargar(carpeta)
    _DE_ESTA_SESION[clave] = hallada
    guardar(carpeta, hallada)
    logger.info("El usuario de AirVault de este programa es {}", hallada.nombre)
    return hallada
