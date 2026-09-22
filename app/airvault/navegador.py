"""La sesion de AirVault, tomada del navegador que el propio programa abre.

El acceso esta federado con Microsoft Entra ID y pide segundo factor. Eso
no se automatiza (ni se debe: el segundo factor existe justamente para que
lo haga una persona), pero lo que sigue despues si.

El programa abre Edge con **un perfil propio**, dentro de ``portable/``,
apuntando a AirVault. La persona entra una vez, con su usuario y su segundo
factor, y en cuanto la sesion queda abierta el programa se la pide al propio
navegador por su protocolo de depuracion. En la aplicacion ese Edge y sus
pestanas se conservan hasta que BITS termina; la linea de comandos lo cierra
al acabar su trabajo.

Por que por el protocolo y no leyendo el archivo de cookies: un Edge moderno
las cifra con la identidad del navegador (``v20``), que no se deshace desde
fuera, y ademas mantiene su base abierta en exclusiva mientras corre. El
navegador si sabe descifrar las suyas, y por el protocolo las entrega ya en
claro. Ver :mod:`app.airvault.cookies`.

Chromium admite **un solo navegador por perfil**. El segundo que se lanza
sobre el mismo ``--user-data-dir`` no abre nada: le entrega la orden al que
ya esta corriendo y se va. Por eso el programa apunta primero al navegador
que quedo vivo de una ejecucion anterior, en vez de lanzar otro que nunca
llegaria a abrir su puerto de depuracion.

Nada de esto instala ni descarga nada: Edge ya viene con Windows y el perfil
es una carpeta mas dentro de ``portable/``. Si no hay Edge, o si algo falla,
se dice y se sigue con la cookie pegada a mano.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger

from app.airvault import cookies as galletas
from app.utils.portable import app_root

# Donde vive el perfil que usa el programa. Va en portable/ para que la
# carpeta entera se pueda copiar a otra maquina con la sesion incluida.
#
# La ruta se arma desde la raiz del proyecto y **absoluta**: Chromium
# descarta un ``--user-data-dir`` relativo sin decir nada y se cierra al
# instante, asi que el programa veia «Edge se cerro antes de abrir la
# sesion» y mandaba a pegar la cookie a mano. Ademas la interfaz no siempre
# corre desde la carpeta del proyecto, y una ruta relativa dejaria el perfil
# (con la sesion dentro) donde cayera el directorio de trabajo.
PERFIL_POR_DEFECTO = app_root() / "portable" / "edge-airvault"

_UBICACIONES_EDGE = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# Argumentos que aislan la ventana del Edge de la persona: perfil propio, sin
# la primera ejecucion y sin extensiones.
#
# ``--restore-last-session`` no esta para reabrir pestanas: es lo unico que
# hace que la sesion se guarde. La cookie de federacion es **de sesion**, y
# Chromium solo escribe esas cookies en disco cuando el perfil arranca
# restaurando la sesion anterior. Sin esta bandera el perfil pierde el
# acceso cada vez que se cierra el navegador y hay que entrar de nuevo con
# el segundo factor en cada ejecución, que es justo lo que el perfil propio
# viene a evitar. Comprobado midiendo la cookie antes y despues de cerrar.
#
_ARGUMENTOS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--restore-last-session",
)


# Cuanto se le da al navegador para abrir su puerto despues de que el
# proceso lanzador ya termino. Un perfil recien creado tarda unos segundos.
_GRACIA_TRAS_LANZADOR_S = 15.0

# Cuantas veces se repasan las pestanas de un navegador recien lanzado, y
# cuanto se espera entre repasos. La restauracion sigue trayendo paginas
# despues de que el puerto conteste, asi que un solo repaso deja fuera a las
# ultimas. Se para en cuanto un repaso no cierra nada, que en un perfil
# limpio es el segundo: el tope es solo para no repasar sin fin.
_BARRIDOS_TRAS_RESTAURAR = 4
_ESPERA_ENTRE_BARRIDOS_S = 0.5

# La GUI conserva un solo Edge durante toda su vida. Web Reports, el acceso
# y las correcciones comparten perfil y puerto; cerrarlo al terminar cada
# operacion dejaba a otro hilo con un socket que Windows ya habia retirado.
# El modo se activa desde ``run_gui``. La linea de comandos conserva el ciclo
# corto de siempre y cierra su navegador al terminar.
_CIERRE_DIFERIDO = False
_NAVEGADORES_ABIERTOS: dict[str, "SesionDeNavegador"] = {}
_CANDADO_NAVEGADORES = threading.RLock()


def mantener_navegadores_hasta_el_cierre() -> None:
    """Hace que la GUI conserve Edge y sus pestanas hasta salir de BITS."""
    global _CIERRE_DIFERIDO
    with _CANDADO_NAVEGADORES:
        _CIERRE_DIFERIDO = True


def cierre_diferido_activo() -> bool:
    """Si las operaciones deben dejar vivo el Edge compartido."""
    with _CANDADO_NAVEGADORES:
        return _CIERRE_DIFERIDO


def _recordar_navegador(sesion: "SesionDeNavegador") -> None:
    if not _CIERRE_DIFERIDO or sesion._version is None:
        return
    clave = str(sesion.perfil.resolve()).casefold()
    _NAVEGADORES_ABIERTOS[clave] = sesion


def cerrar_navegadores_al_salir() -> None:
    """Cierra de una vez los Edge que la GUI mantuvo durante su sesion."""
    global _CIERRE_DIFERIDO
    with _CANDADO_NAVEGADORES:
        sesiones = list(_NAVEGADORES_ABIERTOS.values())
        _NAVEGADORES_ABIERTOS.clear()
        _CIERRE_DIFERIDO = False
    for sesion in sesiones:
        # No se recorren ni se cierran pestanas una a una. Browser.close las
        # entrega juntas a Edge; dos segundos bastan para guardar el perfil.
        # Si no responde, ``cerrar`` identifica y termina solo ese navegador.
        sesion.cerrar(
            definitivo=True,
            espera_s=2.0,
            limpiar_pestanas=False,
            timeout_cierre_s=1.0,
        )


class ErrorDeNavegador(RuntimeError):
    """No se pudo obtener la sesion a traves del navegador."""


# ── protocolo de depuracion ────────────────────────────────────────

class _WebSocket:
    """Cliente WebSocket con lo justo para hablar con Edge.

    Se escribe a mano porque el programa es portable y no puede traer una
    dependencia nueva: son un saludo HTTP y unas tramas, y el interlocutor
    es siempre el mismo navegador en la maquina local.
    """

    def __init__(self, url: str, timeout: float = 15.0):
        resto = url.split("://", 1)[1]
        host_puerto, _, ruta = resto.partition("/")
        host, _, puerto = host_puerto.partition(":")
        self.sock = socket.create_connection((host, int(puerto or 80)), timeout)
        self.sock.settimeout(timeout)
        clave = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET /{ruta} HTTP/1.1\r\nHost: {host_puerto}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {clave}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            .encode()
        )
        datos = b""
        while b"\r\n\r\n" not in datos:
            trozo = self.sock.recv(4096)
            if not trozo:
                raise ErrorDeNavegador("Edge cerro la conexion de depuracion")
            datos += trozo
        if b" 101 " not in datos.split(b"\r\n")[0] + b" ":
            raise ErrorDeNavegador("Edge no acepto la conexion de depuracion")
        self._resto = datos.split(b"\r\n\r\n", 1)[1]
        self._siguiente_id = 0

    def _leer(self, cuantos: int) -> bytes:
        while len(self._resto) < cuantos:
            trozo = self.sock.recv(65536)
            if not trozo:
                raise ErrorDeNavegador("Edge cerro la conexion de depuracion")
            self._resto += trozo
        salida, self._resto = self._resto[:cuantos], self._resto[cuantos:]
        return salida

    def _enviar(self, texto: str) -> None:
        carga = texto.encode("utf-8")
        largo = len(carga)
        cabecera = b"\x81"
        if largo < 126:
            cabecera += struct.pack("!B", largo | 0x80)
        elif largo < 65536:
            cabecera += struct.pack("!BH", 126 | 0x80, largo)
        else:
            cabecera += struct.pack("!BQ", 127 | 0x80, largo)
        mascara = os.urandom(4)
        self.sock.sendall(
            cabecera + mascara
            + bytes(b ^ mascara[i % 4] for i, b in enumerate(carga))
        )

    def _recibir(self) -> str:
        primero = self._leer(2)
        largo = primero[1] & 0x7F
        if largo == 126:
            largo = struct.unpack("!H", self._leer(2))[0]
        elif largo == 127:
            largo = struct.unpack("!Q", self._leer(8))[0]
        return self._leer(largo).decode("utf-8", "replace")

    def pedir(self, metodo: str, **params) -> dict:
        """Manda un comando y devuelve su respuesta, saltandose los avisos."""
        self._siguiente_id += 1
        propio = self._siguiente_id
        self._enviar(json.dumps(
            {"id": propio, "method": metodo, "params": params or {}}
        ))
        while True:
            mensaje = json.loads(self._recibir())
            if mensaje.get("id") == propio:
                if "error" in mensaje:
                    raise ErrorDeNavegador(
                        f"Edge rechazo {metodo}: "
                        f"{mensaje['error'].get('message')}"
                    )
                return mensaje.get("result", {})

    def cerrar(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ── el navegador ───────────────────────────────────────────────────

def ruta_de_edge(candidatas=_UBICACIONES_EDGE) -> Path:
    """Ubica el Edge que trae Windows."""
    for ruta in candidatas:
        if Path(ruta).is_file():
            return Path(ruta)
    raise ErrorDeNavegador(
        "No se encontro Microsoft Edge en esta maquina. Hay que pegar la "
        "cookie a mano."
    )


def _absoluta(perfil: Path | str) -> Path:
    """Ruta del perfil, siempre absoluta.

    Edge no admite un ``--user-data-dir`` relativo: no lo usa, no avisa y se
    cierra. Como la ruta puede venir de ``airvault.json``, se normaliza aqui
    y no solo en el valor por defecto.
    """
    ruta = Path(perfil).expanduser()
    return ruta if ruta.is_absolute() else (app_root() / ruta)


def _puerto_libre() -> int:
    """Un puerto que nadie este usando, para no chocar con otra ventana."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# Donde queda anotado el puerto de depuracion del navegador que el programa
# tiene abierto sobre ese perfil.
#
# Hace falta porque el puerto es otro cada vez y Chromium admite un solo
# navegador por perfil: si una ejecucion anterior dejo a Edge vivo (se cerro
# el programa a media faena, o el puerto no llego a contestar y nadie mato
# al navegador), el que se lanza despues le entrega la orden y se va sin
# abrir nada. Su puerto nuevo no contesta jamas, y sin saber por donde
# escucha el que quedo vivo el perfil se queda tomado **para siempre**: el
# acceso a AirVault moria en «Edge no llego a arrancar» ejecucion tras
# ejecucion, y con el se quedaba parado todo lo que va detras. El archivo va
# dentro del perfil porque es de ese perfil de quien habla, y viaja con el
# si la carpeta ``portable/`` se copia a otra maquina.
_ANOTACION_DEL_PUERTO = "bits-puerto-depuracion"


def _anotar_puerto(perfil: Path, puerto: int) -> None:
    """Deja escrito por donde escucha el navegador de este perfil."""
    try:
        (perfil / _ANOTACION_DEL_PUERTO).write_text(str(puerto), encoding="ascii")
    except OSError as exc:  # noqa: BLE001 - anotar no puede tumbar el acceso
        logger.debug("No se pudo anotar el puerto de depuracion: {}", exc)


def _olvidar_puerto(perfil: Path) -> None:
    """Borra la anotacion: ese navegador ya no esta."""
    try:
        (perfil / _ANOTACION_DEL_PUERTO).unlink()
    except OSError:
        pass


def _puerto_anotado(perfil: Path) -> Optional[int]:
    """El puerto que dejo anotado la ultima apertura, si hay alguno."""
    try:
        texto = (perfil / _ANOTACION_DEL_PUERTO).read_text(encoding="ascii")
        return int(texto.strip())
    except (OSError, ValueError):
        return None


def _version_en(puerto: int, timeout: float = 2.0) -> Optional[dict]:
    """Lo que contesta ``/json/version``, o ``None`` si ahi no hay navegador.

    Se comprueba que traiga ``webSocketDebuggerUrl`` y no solo que algo
    conteste: el puerto anotado puede haberlo tomado despues cualquier otro
    programa, y hablarle por el protocolo de Chromium a quien no lo es
    dejaria el fallo lejos de su causa.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{puerto}/json/version", timeout=timeout
        ) as respuesta:
            version = json.load(respuesta)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if isinstance(version, dict) and version.get("webSocketDebuggerUrl"):
        return version
    return None


def _sin_ventana(version: dict) -> bool:
    """Si ese navegador esta corriendo sin ventana que nadie ve.

    Lo dice el propio navegador en lo que contesta ``/json/version``: en modo
    sin ventana se anuncia como «HeadlessChrome». Importa porque sumarle una
    pestana a uno asi no ensena nada, y quien pidio mirar una busqueda se
    quedaria esperando una ventana que no llega.
    """
    return "headless" in str(version.get("User-Agent", "")).lower()


# Con que se le pregunta a Windows por los Edge abiertos. Se pide la linea
# de ordenes porque el perfil es lo unico que distingue al navegador del
# programa del de la persona: por el nombre son el mismo msedge.exe.
_CONSULTA_DE_EDGES = (
    "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
    "ForEach-Object { $_.ProcessId.ToString() + ' ' + $_.CommandLine }"
)

_PUERTO_EN_LA_ORDEN = re.compile(r"--remote-debugging-port=(\d+)")


def _edges_del_perfil(perfil: Path) -> List[Tuple[int, Optional[int]]]:
    """Los navegadores que tienen abierto ese perfil, con su puerto si lo hay.

    Es el ultimo recurso para saber quien tiene tomado el perfil cuando no
    hay anotacion que leer: uno que dejo una version anterior del programa,
    o uno que arranco y nunca llego a anotarse. Sin esto, ese navegador no
    se puede ni reusar ni cerrar, y el acceso se queda muerto hasta que
    alguien lo busca a mano en el administrador de tareas.

    Se saltan los procesos hijo (``--type=``): se van con su navegador, y
    cerrarlos por separado solo dejaria el perfil a medias.
    """
    try:
        salida = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-Command", _CONSULTA_DE_EDGES],
            capture_output=True, text=True, timeout=30,
        ).stdout or ""
    except Exception as exc:  # noqa: BLE001 - preguntar no puede fallar aqui
        logger.debug("No se pudo mirar que Edge hay abiertos: {}", exc)
        return []
    aguja = str(perfil).lower()
    encontrados: List[Tuple[int, Optional[int]]] = []
    for linea in salida.splitlines():
        baja = linea.lower()
        if aguja not in baja or "--type=" in baja:
            continue
        cabeza = linea.split(None, 1)
        if not cabeza or not cabeza[0].isdigit():
            continue
        hallado = _PUERTO_EN_LA_ORDEN.search(linea)
        encontrados.append((
            int(cabeza[0]), int(hallado.group(1)) if hallado else None
        ))
    if encontrados:
        logger.info(
            "El perfil {} lo tienen abierto {} navegador(es): {}",
            perfil, len(encontrados), encontrados,
        )
    return encontrados


def _matar_proceso(pid: int) -> bool:
    """Cierra ese proceso y los suyos. Windows no pregunta dos veces."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 - se sigue igual sin poder matarlo
        logger.debug("No se pudo cerrar el proceso {}: {}", pid, exc)
        return False
    logger.info("Se cerro a la fuerza el Edge que tenia tomado el perfil "
                "(proceso {})", pid)
    return True


def _pid_que_escucha(puerto: int) -> Optional[int]:
    """Quien tiene abierto ese puerto, preguntandoselo a Windows.

    Hace falta cuando el navegador que quedo vivo se colgo: contesta el
    ``/json/version`` pero no su protocolo, asi que no se le puede pedir el
    cierre por las buenas y no hay otra forma de saber a quien cerrar. Se
    pregunta por el puerto y no por el nombre del programa: matar «msedge»
    a secas se llevaria por delante el navegador de la persona.
    """
    try:
        salida = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception as exc:  # noqa: BLE001 - preguntar no puede fallar aqui
        logger.debug("No se pudo preguntar quien escucha en {}: {}", puerto, exc)
        return None
    for linea in salida.splitlines():
        partes = linea.split()
        if len(partes) < 5 or partes[3].upper() != "LISTENING":
            continue
        if partes[1].rsplit(":", 1)[-1] != str(puerto):
            continue
        try:
            return int(partes[4])
        except ValueError:
            return None
    return None


def _matar_al_del_puerto(puerto: int) -> bool:
    """Cierra a la fuerza el navegador colgado que tiene tomado el perfil.

    Es el ultimo recurso y solo se llega aqui cuando ya no contesta a
    ``Browser.close``: mientras ese proceso siga vivo, ningun Edge nuevo
    puede abrir el perfil, y el acceso a AirVault queda muerto ejecucion
    tras ejecucion. Lo que se pierde por no cerrarlo por las buenas es lo
    que Chromium guarda al salir; volver a entrar lo rehace, y quedarse sin
    poder entrar no lo rehace nada.
    """
    pid = _pid_que_escucha(puerto)
    return _matar_proceso(pid) if pid is not None else False


class SesionDeNavegador:
    """Una ventana de Edge con perfil propio, abierta por el programa."""

    def __init__(self, perfil: Path, edge: Optional[Path] = None,
                 visible: bool = True):
        self.perfil = _absoluta(perfil)
        self.edge = Path(edge) if edge else ruta_de_edge()
        self.visible = visible
        self.puerto = _puerto_libre()
        self._proceso: Optional[subprocess.Popen] = None
        self._quejas = None
        # Lo que contesto /json/version: hace falta para pedirle el cierre
        # por el mismo protocolo por el que se le piden las cookies.
        self._version: Optional[dict] = None
        # Si el navegador con el que se habla lo abrio otra ejecucion. No
        # hay proceso propio que esperar, pero si hay que cerrarlo al
        # terminar: es lo que suelta el perfil para la vez siguiente.
        self._adoptado = False

    def abrir(self, url: str, espera_s: float = 30.0) -> dict:
        """Deja un navegador abierto sobre este perfil y lo devuelve.

        Antes de lanzar nada mira si el perfil ya lo tiene tomado un Edge de
        una ejecucion anterior. Lanzar otro no serviria: Chromium admite un
        solo navegador por perfil, asi que el segundo le pasa la orden al
        primero y se va sin abrir su puerto, que es como el acceso se
        quedaba muerto hasta que alguien cerraba aquella ventana a mano.

        Lo mira por el puerto que dejo anotado la ultima apertura y, si el
        lanzamiento choca igual con el perfil tomado, preguntandole a
        Windows que Edge hay abiertos sobre este perfil.
        """
        # Dos hilos pueden pedir AirVault y Web Reports casi a la vez. Se
        # serializa solo la apertura; una vez encontrado el Edge comparten su
        # protocolo sin que ninguno cierre el navegador del otro.
        with _CANDADO_NAVEGADORES:
            self.perfil.mkdir(parents=True, exist_ok=True)
            version = self._reusar_el_que_tiene_el_perfil(url)
            if version is None:
                try:
                    version = self._lanzar(url, espera_s)
                except ErrorDeNavegador:
                    # El perfil lo tiene un Edge que no dejo anotado su puerto:
                    # uno de una version anterior del programa, o uno que
                    # arranco sin llegar a anotarse. Se busca por el perfil,
                    # se aprovecha o se cierra, y se vuelve a intentar una vez.
                    version, habia = self._soltar_el_perfil(url)
                    if version is None:
                        if not habia:
                            raise
                        version = self._lanzar(url, espera_s)
            self._version = version
            _recordar_navegador(self)
            return version

    def _soltar_el_perfil(self, url: str) -> Tuple[Optional[dict], bool]:
        """Aprovecha o quita de en medio a los Edge que tienen este perfil.

        Devuelve el navegador con el que se va a trabajar (o ``None``) y si
        habia alguno, que es lo que distingue «se solto, vuelve a intentar»
        de «el perfil estaba libre y el fallo era otro».
        """
        encontrados = _edges_del_perfil(self.perfil)
        for pid, puerto in encontrados:
            if puerto is not None:
                version = self._aprovechar(puerto, url)
                if version is not None:
                    return version, True
                if _version_en(puerto) is None:
                    continue
            # Sin puerto por donde hablarle, o sordo: no queda otra.
            _matar_proceso(pid)
        return None, bool(encontrados)

    def _reusar_el_que_tiene_el_perfil(self, url: str) -> Optional[dict]:
        """El navegador que ya tenia el perfil, listo para trabajar con el.

        Para pedirle las cookies sirve igual de bien que uno recien lanzado:
        son las del perfil, y visitar la pagina de entrada dispara el acceso
        federado lo mismo. No sirve en dos casos, y en los dos hay que
        quitarlo de en medio antes de abrir el propio: cuando hace falta una
        ventana que la persona vea (el que quedo vivo puede ser uno sin
        ventana) y cuando ya no contesta a su protocolo, que es como termina
        un navegador olvidado durante horas: el ``/json/version`` sigue
        respondiendo y todo lo demas se queda esperando.

        ``None`` cuando no hay ninguno, o cuando el que habia ya se fue.
        """
        puerto = _puerto_anotado(self.perfil)
        if puerto is None:
            return None
        version = self._aprovechar(puerto, url)
        if version is None:
            # O el navegador ya no esta, o se quito de en medio. En los dos
            # casos la anotacion sobra.
            _olvidar_puerto(self.perfil)
        return version

    def _aprovechar(self, puerto: int, url: str) -> Optional[dict]:
        """Trabaja con el navegador de ese puerto, o lo quita de en medio."""
        version = _version_en(puerto)
        if version is None:
            # La anotacion sobrevivio al navegador (un cierre a la fuerza,
            # un apagon). No es un fallo: se abre uno nuevo.
            return None
        reutilizado = False
        if (
            cierre_diferido_activo()
            and self.visible
            and not _sin_ventana(version)
        ):
            try:
                self.sumar_pestana(url, version=version)
                reutilizado = True
            except (ErrorDeNavegador, OSError, KeyError, ValueError):
                reutilizado = False
        elif not self.visible and self._abrir_pagina(version, url):
            reutilizado = True
        if reutilizado:
            logger.info(
                "Se reusa el Edge que ya tenia abierto el perfil {} "
                "(puerto {})", self.perfil, puerto,
            )
            self.puerto = puerto
            self._version = version
            self._adoptado = True
            return version
        logger.info(
            "Un Edge de otra ejecucion tenia tomado el perfil {}; se le pide "
            "paso para abrir el propio", self.perfil,
        )
        self._quitar_de_en_medio(puerto, version)
        return None

    def _quitar_de_en_medio(self, puerto: int, version: dict) -> None:
        """Suelta el perfil que tiene tomado otro navegador.

        Primero por las buenas, que es lo que le deja guardar la sesion. Si
        despues de pedirselo sigue contestando es que esta colgado, y ahi
        no queda mas remedio que cerrarlo a la fuerza: mientras el viva,
        ningun Edge nuevo abre ese perfil.
        """
        self._pedir_que_se_cierre(version)
        self._esperar_a_que_se_vaya(espera_s=10.0, puerto=puerto)
        if _version_en(puerto) is not None:
            _matar_al_del_puerto(puerto)
        _olvidar_puerto(self.perfil)

    def abrir_pestana(self, url: str, version: Optional[dict] = None,
                      timeout: float = 15.0, insistir: bool = False) -> str:
        """Abre una pagina de trabajo y devuelve su identificador.

        La linea de comandos conserva su limpieza anterior. En la GUI las
        pestanas permanecen abiertas mientras BITS siga abierto: cada trabajo
        conduce la suya por identificador y ninguna operacion invalida el
        socket o la pagina que otro hilo todavia esta usando.

        Con ``insistir`` se vuelve a barrer mientras se siga cerrando algo.
        Es para el navegador recien lanzado: cuando su puerto empieza a
        contestar, la restauracion todavia esta trayendo pestanas, y las que
        llegan tarde se libraban del primer barrido. En un perfil limpio el
        segundo barrido no encuentra nada y no cuesta nada.

        Devuelve el identificador de la pagina. Hace falta para conducirla
        despues sin confundirla con una restaurada que lleve la misma
        direccion, que es como se acababa pilotando una copia vieja.
        """
        version = version or self._version or {}
        ws = _WebSocket(version["webSocketDebuggerUrl"], timeout=timeout)
        try:
            creada = ws.pedir("Target.createTarget", url=url)
            target_id = str(creada.get("targetId", ""))
            if not target_id:
                raise ErrorDeNavegador(
                    f"Edge no abrio {url} y no dijo por que."
                )
            if not cierre_diferido_activo():
                self._cerrar_otras_pestanas(ws, target_id)
                for _ in range(_BARRIDOS_TRAS_RESTAURAR if insistir else 0):
                    time.sleep(_ESPERA_ENTRE_BARRIDOS_S)
                    if not self._cerrar_otras_pestanas(ws, target_id):
                        break
            return target_id
        finally:
            ws.cerrar()

    def sumar_pestana(self, url: str, version: Optional[dict] = None,
                      timeout: float = 15.0) -> str:
        """Abre otra pagina y deja donde estaban las que ya habia.

        Estas paginas las abre una persona para mirarlas. Se acumulan para
        poder comparar dos busquedas y BITS conduce cada una por su propio
        identificador, sin depender de cual quede delante.
        """
        version = version or self._version or {}
        ws = _WebSocket(version["webSocketDebuggerUrl"], timeout=timeout)
        try:
            creada = ws.pedir("Target.createTarget", url=url)
            target_id = str(creada.get("targetId", ""))
            if not target_id:
                raise ErrorDeNavegador(
                    f"Edge no abrio {url} y no dijo por que."
                )
            try:
                ventana = ws.pedir(
                    "Browser.getWindowForTarget", targetId=target_id
                )
                window_id = ventana.get("windowId")
                if window_id is not None:
                    ws.pedir(
                        "Browser.setWindowBounds",
                        windowId=window_id,
                        bounds={"windowState": "normal"},
                    )
                ws.pedir("Target.activateTarget", targetId=target_id)
            except (ErrorDeNavegador, OSError, ValueError) as exc:
                # Traerla al frente es comodidad, no el encargo: la pestana
                # ya esta abierta y el fallo no puede tumbar la apertura.
                logger.debug("No se pudo traer al frente la pestana: {}", exc)
            return target_id
        finally:
            ws.cerrar()

    def abrir_a_la_vista(self, url: str, espera_s: float = 30.0) -> dict:
        """Deja esa pagina a la vista, sumandola a lo que ya estuviera.

        Es para las paginas que se abren para mirarlas. Si el perfil ya lo
        tiene un Edge con ventana, la pagina se le suma como una pestana mas
        y las que hubiera se quedan: es lo unico que permite abrir dos
        busquedas y compararlas. Si no hay ninguno, o el que hay es uno sin
        ventana (que no ensenaria nada) o ya no contesta, se cae en
        :meth:`abrir`, que se encarga de dejar el perfil libre y lanzar el
        navegador propio.
        """
        self.perfil.mkdir(parents=True, exist_ok=True)
        puerto = _puerto_anotado(self.perfil)
        version = _version_en(puerto) if puerto is not None else None
        if version is not None and not _sin_ventana(version):
            try:
                self.sumar_pestana(url, version=version)
            except (ErrorDeNavegador, OSError, KeyError, ValueError) as exc:
                logger.debug("El Edge que estaba no sumo la pestana: {}", exc)
            else:
                logger.info(
                    "Se suma una pestana al Edge que ya tenia el perfil {} "
                    "(puerto {})", self.perfil, puerto,
                )
                self.puerto = int(puerto)
                self._version = version
                self._adoptado = True
                return version
        return self.abrir(url, espera_s=espera_s)

    def _abrir_pagina(self, version: dict, url: str,
                      timeout: float = 5.0, insistir: bool = False) -> bool:
        """Manda al navegador que ya estaba a la pagina de entrada.

        Es lo mismo que hace la orden de arranque cuando el navegador se
        lanza: esa visita es la que rehace el acceso federado y renueva la
        sesion sin que nadie teclee nada.

        Devuelve si el navegador contesto, que es ademas la prueba de que
        se puede trabajar con el: si no atiende su protocolo tampoco va a
        entregar las cookies, y lo que hay que hacer con el es cerrarlo, no
        esperarlo. Por eso se le da poco tiempo: uno colgado no mejora.
        """
        try:
            self.abrir_pestana(
                url, version=version, timeout=timeout, insistir=insistir
            )
            return True
        except (ErrorDeNavegador, OSError, KeyError, ValueError) as exc:
            logger.debug("El Edge que estaba no abrio la pagina: {}", exc)
            return False

    def _cerrar_otras_pestanas(self, ws: _WebSocket,
                               conservar: str = "") -> int:
        """Deja el navegador con una sola pagina abierta y dice cuantas cerro.

        ``conservar`` es la pagina recien creada, la que se va a usar. Sin
        ella se conserva la ultima de la lista: esto se llama tambien justo
        antes de cerrar el navegador, y quedarse sin ninguna pagina lo haria
        irse por su cuenta, sin ocasion de guardar el perfil.

        Solo se cierran paginas, nunca otros objetivos (procesos de servicio,
        extensiones), y siempre las de este navegador, que es el del perfil
        propio del programa: la ventana de la persona no se toca. Fallar aqui
        no puede tumbar el acceso, asi que se anota y se sigue.
        """
        try:
            objetivos = ws.pedir("Target.getTargets").get("targetInfos", [])
        except (ErrorDeNavegador, OSError, ValueError) as exc:
            logger.debug("No se pudieron listar las pestanas de Edge: {}", exc)
            return 0
        paginas = [
            str(objetivo.get("targetId", ""))
            for objetivo in objetivos
            if objetivo.get("type") == "page" and objetivo.get("targetId")
        ]
        if not conservar and paginas:
            conservar = paginas[-1]
        cerradas = 0
        for target_id in paginas:
            if target_id == conservar:
                continue
            try:
                ws.pedir("Target.closeTarget", targetId=target_id)
                cerradas += 1
            except (ErrorDeNavegador, OSError, ValueError) as exc:
                logger.debug(
                    "No se pudo cerrar la pestana {}: {}", target_id, exc,
                )
        if cerradas:
            logger.info(
                "Se cerraron {} pestana(s) sobrantes del perfil {}",
                cerradas, self.perfil,
            )
        return cerradas

    def _lanzar(self, url: str, espera_s: float = 30.0) -> dict:
        """Arranca el navegador y espera a que conteste el protocolo."""
        orden = [
            str(self.edge),
            f"--remote-debugging-port={self.puerto}",
            f"--user-data-dir={self.perfil}",
            *_ARGUMENTOS,
        ]
        if not self.visible:
            if cierre_diferido_activo():
                # Debe poder mostrarse mas tarde sin derribarlo y perder sus
                # pestanas. Arranca como Edge normal, pero fuera del camino.
                orden.append("--start-minimized")
            else:
                orden.append("--headless=new")
        orden.append(url)
        # La salida de error de Edge se guarda: es lo unico que dice por que
        # no arranco (perfil tomado, bandera rechazada, politica de la
        # empresa) y tirarla dejaba el fallo en «Edge se cerro», que no se
        # puede diagnosticar.
        if self._quejas is not None:
            # Puede haber un intento anterior: el que descubrio que el perfil
            # estaba tomado.
            self._quejas.close()
        self._quejas = tempfile.TemporaryFile()
        self._proceso = subprocess.Popen(
            orden, stdout=subprocess.DEVNULL, stderr=self._quejas,
        )
        # Se anota antes de saber si contesta, no despues: si el puerto no
        # llega a abrirse pero el navegador queda vivo, esta anotacion es lo
        # unico que le deja a la ejecucion siguiente para dar con el en vez
        # de tropezar otra vez con el perfil tomado.
        _anotar_puerto(self.perfil, self.puerto)
        limite = time.monotonic() + espera_s
        # ``msedge.exe`` no *es* el navegador: entrega el encargo a un proceso
        # suelto y se va enseguida, con codigo 0 o 21 («ya avise a otro»).
        # Que el lanzador termine no dice nada, asi que no se puede tomar por
        # un fallo: dar por muerto al navegador ahi era declarar «Edge se
        # cerro antes de abrir la sesion» sobre un Edge que estaba
        # arrancando, y el resultado dependia de cual de los dos ganaba la
        # carrera. Lo que si vale es que el lanzador se haya ido **y** el
        # puerto siga mudo un rato despues: eso es que no arranco nadie.
        gracia: Optional[float] = None
        while time.monotonic() < limite:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.puerto}/json/version", timeout=2
                ) as respuesta:
                    self._version = json.load(respuesta)
                    # El argumento de restauracion es necesario para
                    # guardar la cookie de sesion, pero tambien reabre las
                    # paginas de la ejecucion anterior. Se deja una sola de
                    # trabajo, insistiendo hasta que la restauracion acabe.
                    self._abrir_pagina(self._version, url, insistir=True)
                    return self._version
            except (urllib.error.URLError, OSError, ValueError):
                if self._proceso.poll() is not None:
                    ahora = time.monotonic()
                    if gracia is None:
                        gracia = min(ahora + _GRACIA_TRAS_LANZADOR_S, limite)
                    elif ahora >= gracia:
                        raise ErrorDeNavegador(
                            f"Edge no llego a arrancar: el proceso termino "
                            f"(codigo {self._proceso.returncode}) y nadie "
                            f"contesto por el puerto de depuracion. "
                            f"{self._perfil_tomado()}{self._por_que()}"
                        )
                time.sleep(0.5)
        raise ErrorDeNavegador(
            f"Edge arranco pero no contesto por su puerto de depuracion en "
            f"{espera_s:.0f}s, asi que no hay forma de pedirle la sesion. "
            f"Casi siempre es que el perfil {self.perfil} ya esta abierto en "
            f"otra ventana: hay que cerrarla y volver a intentar. "
            f"{self._por_que()}"
        )

    def _perfil_tomado(self) -> str:
        """Lo que casi siempre pasa cuando el lanzador se va sin abrir nada."""
        return (
            f"Casi siempre es que otro Edge tiene abierto el perfil "
            f"{self.perfil}: Chromium admite un solo navegador por perfil, y "
            f"el que se lanza despues le entrega la orden y se va. Hay que "
            f"cerrar esa ventana y volver a intentar. "
        )

    def _por_que(self) -> str:
        """Lo que Edge dejo escrito al fallar, recortado a lo legible."""
        quejas = getattr(self, "_quejas", None)
        if quejas is None:
            return ""
        try:
            quejas.seek(0)
            texto = quejas.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""
        lineas = [l.strip() for l in texto.splitlines() if l.strip()]
        # Las ultimas son las del fallo; las primeras suelen ser ruido de
        # arranque que Edge escribe siempre.
        utiles = [l for l in lineas if "ERROR" in l or "FATAL" in l] or lineas
        if not utiles:
            return ""
        return "Edge dijo: " + " / ".join(utiles[-3:])[:400]

    def cookies(self, version: dict) -> Dict[str, List[dict]]:
        """Pide al navegador sus cookies, ya descifradas."""
        return cookies_de(version)

    def cerrar(
        self,
        version: Optional[dict] = None,
        *,
        definitivo: bool = False,
        espera_s: float = 15.0,
        limpiar_pestanas: bool = True,
        timeout_cierre_s: float = 5.0,
    ) -> None:
        """Cierra el navegador, pidiendoselo antes de matarlo.

        Importa que sea por las buenas: Chromium escribe al salir lo que
        conserva del perfil (entre otras cosas la sesion) y suelta el
        candado de la carpeta. Matarlo deja las dos cosas a medias, y la
        siguiente apertura del mismo perfil se encuentra un candado que ya
        no tiene dueno.
        """
        if cierre_diferido_activo() and not definitivo:
            # El archivo temporal solo recoge el arranque. Edge ya heredo el
            # descriptor y conservarlo aqui no aporta nada durante la sesion.
            if self._quejas is not None:
                self._quejas.close()
                self._quejas = None
            return
        if self._proceso is None and not self._adoptado:
            return
        version = version or self._version
        if version:
            self._pedir_que_se_cierre(
                version,
                limpiar_pestanas=limpiar_pestanas,
                timeout=timeout_cierre_s,
            )
        # Esperar al proceso lanzador no dice nada: hace rato que termino
        # (ver ``abrir``). Al navegador se le mide por su puerto, que deja de
        # contestar justo cuando termina de guardar el perfil y suelta el
        # candado. Eso es lo que hay que ver antes de volver a abrirlo.
        self._esperar_a_que_se_vaya(espera_s=espera_s)
        if self._proceso is not None:
            try:
                self._proceso.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._proceso.terminate()
        if _version_en(self.puerto) is not None:
            # Pedirselo no basto. Mientras ese proceso viva, ningun Edge
            # nuevo abre el perfil: es justo lo que dejaba el acceso muerto,
            # y encima la ventana de entrar es la siguiente en necesitarlo.
            logger.info(
                "Edge no se fue cuando se le pidio; se cierra a la fuerza "
                "para dejar libre el perfil {}", self.perfil,
            )
            _matar_al_del_puerto(self.puerto)
        # La anotacion vale mientras ese navegador este vivo. Borrarla con el
        # todavia en pie dejaria a la ejecucion siguiente sin saber por donde
        # buscarlo; dejarla despues de cerrarlo la manda a un puerto mudo.
        if _version_en(self.puerto) is None:
            _olvidar_puerto(self.perfil)
        else:
            logger.warning(
                "No se pudo cerrar el Edge del perfil {}; queda anotado su "
                "puerto {} para poder aprovecharlo despues",
                self.perfil, self.puerto,
            )
        self._proceso = None
        self._version = None
        self._adoptado = False
        if self._quejas is not None:
            self._quejas.close()
            self._quejas = None

    def _esperar_a_que_se_vaya(self, espera_s: float = 15.0,
                               puerto: Optional[int] = None) -> None:
        """Espera a que el puerto de depuracion deje de contestar."""
        puerto = self.puerto if puerto is None else int(puerto)
        limite = time.monotonic() + espera_s
        while time.monotonic() < limite:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{puerto}/json/version", timeout=2
                ):
                    pass
            except (urllib.error.URLError, OSError, ValueError):
                return
            time.sleep(0.5)
        logger.debug(
            "Edge sigue contestando en {} despues de pedirle el cierre",
            puerto,
        )

    def _pedir_que_se_cierre(
        self,
        version: dict,
        *,
        limpiar_pestanas: bool = True,
        timeout: float = 5.0,
    ) -> None:
        """Manda ``Browser.close`` por el protocolo y no insiste si falla."""
        try:
            ws = _WebSocket(version["webSocketDebuggerUrl"], timeout=timeout)
        except (ErrorDeNavegador, OSError, KeyError) as exc:
            logger.debug("No se pudo pedir el cierre a Edge: {}", exc)
            return
        try:
            # Lo que este abierto ahora es justo lo que Chromium guarda y
            # vuelve a abrir en el arranque siguiente. Se limpia aqui, con el
            # navegador todavia en pie, porque es el unico momento en que
            # estan todas: al abrir, la restauracion puede no haber acabado.
            if limpiar_pestanas:
                self._cerrar_otras_pestanas(ws)
            ws.pedir("Browser.close")
        except (ErrorDeNavegador, OSError, ValueError) as exc:
            # El navegador se va mientras contesta: que no llegue la
            # respuesta es lo normal, no un fallo.
            logger.debug("Edge se fue sin contestar al cierre: {}", exc)
        finally:
            ws.cerrar()

    def __enter__(self) -> "SesionDeNavegador":
        return self

    def __exit__(self, *_excepcion) -> None:
        self.cerrar()


def _del_dominio(por_dominio: Dict[str, List[dict]], host: str
                 ) -> Dict[str, str]:
    """Cookies que viajan a ``host``, en el formato del resto del modulo."""
    mapa = {
        dominio: {str(c.get("name")): str(c.get("value", ""))
                  for c in lista}
        for dominio, lista in por_dominio.items()
    }
    return galletas.del_dominio(mapa, host)


def cookies_de(version: dict) -> Dict[str, List[dict]]:
    """Las cookies del navegador de ese ``version``, por dominio.

    Es de modulo y no de la sesion porque tambien la pide quien trabaja
    sobre un Edge que abrio otro (la correccion de Web Reports usa la
    ventana que ya tiene el perfil, sin duenarla).
    """
    ws = _WebSocket(version["webSocketDebuggerUrl"])
    try:
        crudas = ws.pedir("Storage.getCookies").get("cookies", [])
    finally:
        ws.cerrar()
    por_dominio: Dict[str, List[dict]] = {}
    for cookie in crudas:
        dominio = str(cookie.get("domain", ""))
        por_dominio.setdefault(dominio, []).append(cookie)
    return por_dominio


def obtener_cookies(
    base_url: str,
    perfil: Optional[Path] = None,
    url_sso: str = "",
    edge: Optional[Path] = None,
    espera_login_s: float = 300.0,
    avisar: Optional[Callable[[str], None]] = None,
    dormir: Callable[[float], None] = time.sleep,
    reloj: Callable[[], float] = time.monotonic,
    forzar_login: bool = False,
    confirmar: Optional[Callable[[Dict[str, str]], bool]] = None,
    espera_perfil_s: float = 25.0,
) -> Dict[str, str]:
    """Devuelve las cookies de AirVault, abriendo el navegador si hace falta.

    Primero lo intenta **sin ventana**: si el perfil conserva la sesion de la
    vez anterior, nadie tiene que hacer nada. Solo cuando no hay sesion se
    abre una ventana para que la persona entre; en cuanto AirVault suelta sus
    cookies, la ventana se cierra sola.

    Con ``forzar_login`` se salta el intento sin ventana. Lo usa quien ya
    probo las cookies del perfil contra el servidor y se encontro con que
    ya no valen: en ese caso volver a leerlas devolveria las mismas.
    """
    perfil = Path(perfil or PERFIL_POR_DEFECTO)
    host = galletas.dominio(base_url)
    # Se entra por el enlace federado, no por la raiz: es el que dispara la
    # redireccion a Microsoft y, con ella, la cookie que autentica.
    entrada = url_sso or base_url

    def sirven(cookies: Dict[str, str]) -> bool:
        """Si estas cookies dan una sesion con la que se pueda trabajar.

        Con ``confirmar`` se le pregunta al servidor, que es el unico que lo
        sabe de verdad: el perfil guarda cookies con la forma correcta
        mucho despues de que hayan caducado, y darlas por buenas dejaba el
        trabajo muriendo en la primera peticion.
        """
        return bool(cookies) and galletas.sostienen_sesion(cookies) and (
            confirmar is None or confirmar(cookies)
        )

    if not forzar_login:
        encontradas: Dict[str, str] = {}
        listas = False
        with SesionDeNavegador(perfil, edge, visible=False) as navegador:
            version = navegador.abrir(entrada)
            # Las cookies no se leen de golpe: recien abierto, el navegador
            # todavía esta yendo y volviendo de Microsoft, y lo que hay en
            # ese instante es lo de la vez anterior (caducado, si paso el
            # rato). Esperar a que la sesion sirva es ademas lo que la
            # renueva sola: el navegador rehace el acceso federado sin que
            # nadie teclee nada.
            limite = reloj() + espera_perfil_s
            while True:
                encontradas = _del_dominio(navegador.cookies(version), host)
                listas = sirven(encontradas)
                if listas or reloj() >= limite:
                    break
                dormir(1.0)
        if listas:
            logger.info("La sesion del perfil de Edge seguia abierta")
            return encontradas
        logger.info(
            "El perfil {} no tiene sesion de AirVault; se abre la ventana "
            "para entrar", perfil,
        )

    if avisar is not None:
        avisar(
            "Se abrió una ventana de Edge: entre a AirVault con su usuario "
            "de Microsoft. La ventana se cierra sola al terminar."
        )
    with SesionDeNavegador(perfil, edge, visible=True) as navegador:
        version = navegador.abrir(entrada)
        limite = reloj() + espera_login_s
        while reloj() < limite:
            encontradas = _del_dominio(navegador.cookies(version), host)
            if sirven(encontradas):
                logger.info(
                    "Sesion de AirVault abierta en el navegador: {}",
                    galletas.resumir(encontradas),
                )
                return encontradas
            dormir(2.0)
        acompanantes = galletas.resumir(encontradas) if encontradas else ""
    detalle = (
        f" Del sitio si llegaron {acompanantes}, que son las que pone "
        f"AirVault antes de saber quien entra: eso pasa cuando la ventana "
        f"se quedo en la pagina de Microsoft sin completar el acceso."
        if acompanantes else ""
    )
    raise ErrorDeNavegador(
        f"Pasaron {espera_login_s / 60:.0f} minutos y AirVault no llego a "
        f"dar una sesion en la ventana de Edge que abrio el programa.{detalle} "
        f"Se puede volver a intentar, o pegar la cookie a mano en el campo "
        f"Sesion."
    )


def disponible() -> bool:
    """Dice si en esta maquina se puede tomar la sesion del navegador."""
    try:
        ruta_de_edge()
        return True
    except ErrorDeNavegador:
        return False
