"""Ruta remota: una carpeta compartida donde varias personas trabajan a la vez.

El programa nació guardando todo junto a sí mismo: ``input/``, lo ya
procesado en ``input/processed`` y las ejecuciones en ``output/``. Con una
ruta remota esas tres carpetas pasan a ser las de un servidor que cada quien
tiene montado con su propio usuario de Windows, de modo que lo que una
persona deja en la entrada lo puede procesar otra y las ejecuciones quedan a
la vista de todas.

Se escribe una sola ruta y dentro va la misma forma que tiene la carpeta del
programa (``input``, ``input/processed``, ``output`` y los dos archivos de la
memoria de libros). Así sirve apuntar a una carpeta vacía, que se prepara
sola, o a la copia de quien ya lleva tiempo trabajando.

Tres cosas mandan en el diseño:

* **Nace apagada.** Sin activarla nada cambia y no se toca la red: cada
  función de aquí devuelve la carpeta local sin mirar nada más.
* **La elección es de cada equipo.** La ruta y si está activa se guardan en
  ``interfaz.json``, junto a las demás preferencias de la instalación, y
  sobreviven al cierre. Quién usa el programa, sus preferencias y la cola de
  AirVault siguen siendo locales.
* **Sin servidor se sigue trabajando.** Si la ruta no responde se usan las
  carpetas locales y se avisa una vez por el log. Preguntar por una carpeta
  de red caída puede tardar medio minuto en Windows, así que la consulta se
  hace con límite de tiempo y su respuesta se recuerda unos segundos: quien
  pinta la ventana no puede quedarse esperando a un servidor apagado.

La memoria de libros no se muda al servidor: se sincroniza con él (ver
:mod:`app.validation.memoria_remota`), para que la inferencia siga
funcionando sin red.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from loguru import logger

from app.utils import preferencias_ui
from app.utils.io import PROCESSED_DIRNAME
from app.utils.portable import app_root

CLAVE_ACTIVA = "remota.activa"
CLAVE_RUTA = "remota.ruta"

CARPETA_INPUT = "input"
CARPETA_OUTPUT = "output"

# Cuánto se espera a que la carpeta de red conteste antes de darla por caída.
ESPERA_CONEXION_S = 3.0
# Cuánto vale una respuesta. La de «no contesta» dura más: repetir la
# consulta es volver a esperar el límite entero, y una red caída no vuelve en
# dos segundos.
VIGENCIA_CONECTADA_S = 15.0
VIGENCIA_CAIDA_S = 30.0

_CANDADO = threading.Lock()
# Por ruta: cuándo se preguntó y qué contestó.
_RESPUESTAS: Dict[str, Tuple[float, bool]] = {}
# Lo último que se dijo por el log, para avisar del cambio y no de cada vez.
_ULTIMO_AVISO: Dict[str, bool] = {}
# Los ajustes ya leídos, con la firma del archivo del que salieron.
_AJUSTES_LEIDOS: Optional[Tuple[tuple, "Ajustes"]] = None


@dataclass(frozen=True)
class Ajustes:
    """Lo que esta instalación tiene elegido."""

    activa: bool = False
    ruta: str = ""

    @property
    def carpeta(self) -> Optional[Path]:
        """La ruta escrita, o nada si está vacía."""
        texto = self.ruta.strip()
        return Path(texto) if texto else None


def _firma_de_preferencias() -> tuple:
    """Qué archivo de preferencias hay y cuándo cambió por última vez."""
    ruta = preferencias_ui.ruta_preferencias()
    try:
        estado = ruta.stat()
    except OSError:
        return (str(ruta), None, None)
    return (str(ruta), estado.st_mtime_ns, estado.st_size)


def leer_ajustes() -> Ajustes:
    """La ruta guardada y si está activa. De fábrica, apagada y sin ruta.

    Las carpetas se preguntan muchas veces seguidas (el visor busca el PDF
    de origen de cada ejecución que abre), así que el archivo solo se vuelve
    a leer cuando cambia.
    """
    global _AJUSTES_LEIDOS
    firma = _firma_de_preferencias()
    leidos = _AJUSTES_LEIDOS
    if leidos is not None and leidos[0] == firma:
        return leidos[1]
    activa = preferencias_ui.leer_opcion(CLAVE_ACTIVA, False)
    ruta = preferencias_ui.leer_opcion(CLAVE_RUTA, "")
    ajustes = Ajustes(
        activa=activa is True,
        ruta=ruta.strip() if isinstance(ruta, str) else "",
    )
    _AJUSTES_LEIDOS = (firma, ajustes)
    return ajustes


def guardar_ajustes(activa: bool, ruta: str) -> bool:
    """Anota la elección en las preferencias de esta instalación.

    La ruta se conserva aunque se desactive: apagar y volver a encender no
    obliga a escribirla otra vez.
    """
    global _AJUSTES_LEIDOS
    ruta = str(ruta or "").strip()
    olvidar_conexion()
    guardado = preferencias_ui.guardar_opciones(
        {CLAVE_ACTIVA: bool(activa), CLAVE_RUTA: ruta}
    )
    _AJUSTES_LEIDOS = None
    return guardado


def olvidar_conexion() -> None:
    """Descarta lo recordado, para que la próxima consulta mire de verdad."""
    with _CANDADO:
        _RESPUESTAS.clear()


def _contesta(carpeta: Path, espera_s: float) -> bool:
    """Si la carpeta existe, sin esperar más de ``espera_s``.

    La consulta va en un hilo aparte porque Windows no deja acortar la
    espera de una ruta de red: si el hilo no vuelve a tiempo se da por
    caída y él termina solo cuando el sistema se rinde.
    """
    respuesta: list[bool] = []

    def mirar() -> None:
        try:
            respuesta.append(carpeta.is_dir())
        except OSError:
            respuesta.append(False)

    hilo = threading.Thread(target=mirar, name="ruta-remota", daemon=True)
    hilo.start()
    hilo.join(espera_s)
    return bool(respuesta and respuesta[0])


def conectada(
    carpeta: Path | str,
    *,
    refrescar: bool = False,
    espera_s: float = ESPERA_CONEXION_S,
) -> bool:
    """Si esa carpeta responde ahora mismo, recordándolo unos segundos."""
    carpeta = Path(carpeta)
    clave = str(carpeta).casefold()
    ahora = time.monotonic()
    with _CANDADO:
        previa = _RESPUESTAS.get(clave)
    if previa is not None and not refrescar:
        cuando, respuesta = previa
        vigencia = VIGENCIA_CONECTADA_S if respuesta else VIGENCIA_CAIDA_S
        if ahora - cuando < vigencia:
            return respuesta
    respuesta = _contesta(carpeta, espera_s)
    with _CANDADO:
        _RESPUESTAS[clave] = (time.monotonic(), respuesta)
        cambio = _ULTIMO_AVISO.get(clave) != respuesta
        _ULTIMO_AVISO[clave] = respuesta
    if cambio:
        if respuesta:
            logger.info(f"Ruta remota conectada: {carpeta}")
        else:
            logger.warning(
                f"La ruta remota no responde ({carpeta}): se trabaja con las "
                "carpetas de este equipo"
            )
    return respuesta


def raiz_remota(*, refrescar: bool = False) -> Optional[Path]:
    """La carpeta compartida, si está activa y responde; si no, nada."""
    ajustes = leer_ajustes()
    carpeta = ajustes.carpeta
    if not ajustes.activa or carpeta is None:
        return None
    return carpeta if conectada(carpeta, refrescar=refrescar) else None


def en_uso() -> bool:
    """Si ahora mismo se está trabajando sobre la carpeta compartida."""
    return raiz_remota() is not None


def raiz_de_datos(local: Path | str | None = None) -> Path:
    """De dónde cuelgan ``input`` y ``output`` en este momento.

    ``local`` es la carpeta del programa de quien pregunta; sin ella vale la
    de la instalación.
    """
    remota = raiz_remota()
    if remota is not None:
        return remota
    return Path(local) if local is not None else app_root()


def carpeta_input(local: Path | str | None = None) -> Path:
    """La entrada: la del servidor si hay ruta remota, la local si no."""
    return raiz_de_datos(local) / CARPETA_INPUT


def carpeta_output(local: Path | str | None = None) -> Path:
    """Donde se guardan las ejecuciones, con el mismo criterio."""
    return raiz_de_datos(local) / CARPETA_OUTPUT


def preparar(carpeta: Path | str) -> None:
    """Deja en la carpeta compartida las subcarpetas que el programa usa.

    Lo que ya exista no se toca. Lanza ``OSError`` si no se puede escribir
    allí, que es justo lo que quien activa la ruta necesita saber.
    """
    carpeta = Path(carpeta)
    for sub in (
        Path(CARPETA_INPUT),
        Path(CARPETA_INPUT) / PROCESSED_DIRNAME,
        Path(CARPETA_OUTPUT),
    ):
        (carpeta / sub).mkdir(parents=True, exist_ok=True)
