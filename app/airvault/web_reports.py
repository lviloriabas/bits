"""Consulta de Web Reports en el Edge de trabajo de AirVault.

El reporte Log Page Audit no ofrece una API documentada. Esta pieza conduce
el visor de SSRS por su DOM, siempre en el perfil portable que ya usa el
indexado, y devuelve datos legibles para la interfaz. Solo consulta: no abre
la edicion, no reindexa y no elimina documentos.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import quote

from app.airvault import cookies as galletas
from app.airvault.config import AirVaultConfig
from app.airvault.navegador import (
    PERFIL_POR_DEFECTO,
    SesionDeNavegador,
    _del_dominio,
    _WebSocket,
)

LOG_PAGE_AUDIT_URL = (
    "https://sql2014dw.criticaltech.com/ReportServer_2014/Pages/"
    "ReportViewer.aspx?/Reports2014/AVLogPageAudit"
    "&rc:Stylesheet=AVTheme-reports"
)

TIPO_MAL_INDEXADA = "Mal indexada"
TIPO_DUPLICADA = "Duplicada"
FILTRO_MAL_INDEXADAS = "8"
FILTRO_DUPLICADAS = "10"

_CONTROL = "ReportViewerControl_ctl04_"
_AREA_REPORTE = "ReportViewerControl_ctl09"
_ESPERA_REPORTE = "ReportViewerControl_AsyncWait"
_FORMULARIO_LISTO = (
    f"document.getElementById('{_CONTROL}ctl03_ddValue')"
    f" && !document.getElementById('{_CONTROL}ctl03_ddValue').disabled"
)
_FECHA_LISTA = (
    f"document.getElementById('{_CONTROL}ctl07_txtValue')"
    f" && !document.getElementById('{_CONTROL}ctl07_txtValue').disabled"
)

_DUPLICADA = re.compile(
    r"\bDUPLICATED\s+(?P<log>\d{7})\s*\((?P<copias>\d+)x\)",
    re.IGNORECASE,
)
_MAL_INDEXADA = re.compile(
    r"\b(?P<log>\d{7})\s+MIS-INDEX(?:ED)?\s+to\s+ACN\s*"
    r"\[(?P<destino>[^\]]+)\]",
    re.IGNORECASE,
)

# El rango del libro, tal y como lo escribe el reporte: «2008150 - 2008199».
# Sus dos extremos son numeros de bitacora, de siete digitos, asi que valen
# tal cual como limites de una busqueda por Log Page Number.
_LIMITES_DEL_RANGO = re.compile(r"(\d{7})\D+(\d{7})")


class ConsultaCancelada(RuntimeError):
    """La consulta fue detenida desde la interfaz."""


@dataclass(frozen=True)
class ExcepcionLogPageAudit:
    """Una pagina señalada por Log Page Audit."""

    tipo: str
    matricula_libro: str
    tipo_libro: str
    rango_libro: str
    rango_fechas: str
    faltantes: str
    duplicadas: str
    fecha_sospechosa: str
    detalle: str
    log_number: str
    destino: str = ""
    copias: int | None = None
    url_busqueda: str = ""
    url_busqueda_libro: str = ""


def url_busqueda_rango(
    config: AirVaultConfig, desde: str, hasta: str
) -> str:
    """Enlace estable de Web Search para un tramo de numeros de bitacora.

    Los parametros 3 y 4 de la busqueda guardada son el primer y el ultimo
    Log Page Number del tramo: con el mismo numero en los dos sale una sola
    pagina, y con los dos extremos de un libro salen sus cincuenta.
    """
    parametros = (
        f"1=LOG PAGE\t3={str(desde).strip()}\t4={str(hasta).strip()}"
    )
    return (
        f"{config.base_url.rstrip('/')}/zfp/client/unencryptedparamssearch/"
        f"?repoId={config.repo_id}&searchId=15504"
        f"&searchParams={quote(parametros, safe='=')}"
        "&maxHits=200&behavior=lockdown=false"
    )


def url_busqueda_log(config: AirVaultConfig, log_number: str) -> str:
    """Enlace estable de Web Search para las apariciones de una pagina."""
    numero = str(log_number).strip()
    return url_busqueda_rango(config, numero, numero)


def url_busqueda_del_libro(config: AirVaultConfig, rango: str) -> str:
    """Enlace de Web Search para el libro entero, o vacio si no se lee.

    Si la celda del rango viene vacia, o con algo que no son sus dos
    extremos, no hay libro que abrir: se devuelve vacio y quien pregunta se
    queda sin enlace, en vez de con uno inventado que buscaria otra cosa.
    """
    hallado = _LIMITES_DEL_RANGO.search(str(rango or ""))
    if hallado is None:
        return ""
    return url_busqueda_rango(config, hallado.group(1), hallado.group(2))


def _perfil_de(config: AirVaultConfig) -> Path:
    """La carpeta de Edge donde vive la sesion de trabajo."""
    return (
        Path(config.perfil_navegador)
        if config.perfil_navegador
        else PERFIL_POR_DEFECTO
    )


def entrada_federada(config: AirVaultConfig) -> str:
    """Por donde se entra a AirVault para que la sesion se rehaga sola.

    El acceso de la empresa esta federado con Entra ID, y quien dispara esa
    redireccion es el enlace federado, no la raiz del sitio: entrando por la
    raiz sale el formulario local de usuario y contrasena, que nadie usa.
    Es lo que hace el resto del programa antes de pedir nada.
    """
    return config.url_sso or config.base_url


def esperar_acceso(
    cookies: Callable[[], dict],
    config: AirVaultConfig,
    segundos: float = 25.0,
    dormir: Callable[[float], None] = time.sleep,
    reloj: Callable[[], float] = time.monotonic,
) -> bool:
    """Espera a que el perfil tenga la cookie que abre la sesion.

    La visita al enlace federado no autentica en el acto: el navegador va y
    vuelve de Microsoft, y mientras tanto lo que hay en el perfil son las
    cookies de la vez anterior. Esperar a que aparezca la buena es ademas lo
    que renueva la sesion sin que nadie teclee nada.

    Devuelve si llego a haberla. Un ``False`` no es motivo para no seguir:
    la pantalla dira mejor que nadie si hace falta entrar a mano.
    """
    host = galletas.dominio(config.base_url)
    limite = reloj() + max(0.0, segundos)
    while True:
        try:
            if galletas.sostienen_sesion(_del_dominio(cookies(), host)):
                return True
        except (OSError, RuntimeError, ValueError):
            pass
        if reloj() >= limite:
            return False
        dormir(1.0)


def abrir_en_web_search(
    config: AirVaultConfig,
    url: str,
    avisar: Callable[[str], None] | None = None,
) -> None:
    """Deja una busqueda de Web Search a la vista, para leerla.

    Va por el Edge del programa y no por el navegador de la persona: la
    sesion de AirVault vive en ese perfil, asi que la misma direccion en
    otro navegador acaba en la pantalla de acceso.

    Cada busqueda se suma como una pestana mas y las anteriores se quedan
    donde estaban. Estas paginas se abren para leerlas, y quien mira una
    bitacora casi siempre quiere ver ademas el libro al que pertenece:
    dejarle una sola pestana obligaba a elegir. Se cierran a mano, o cuando
    la consulta o la correccion siguientes reaprovechen este navegador.
    """
    if not url:
        raise ValueError("Esa fila no trae ninguna búsqueda que abrir")
    notificar = avisar or (lambda _texto: None)
    notificar("Abriendo Web Search en Edge")
    navegador = SesionDeNavegador(_perfil_de(config), visible=True)
    navegador.abrir_a_la_vista(url, espera_s=config.espera_login_s)


def _texto(celda: object) -> str:
    if isinstance(celda, dict):
        valor = celda.get("t", "")
    else:
        valor = celda
    return " ".join(str(valor or "").split())


def parsear_filas(
    filas: Iterable[Sequence[object]], config: AirVaultConfig
) -> list[ExcepcionLogPageAudit]:
    """Convierte las filas planas de SSRS en excepciones sin duplicarlas."""
    salida: list[ExcepcionLogPageAudit] = []
    vistos: set[tuple[str, str, str, str, int | None]] = set()
    for fila in filas:
        celdas = [_texto(celda) for celda in fila]
        # El diseño del reporte lleva dos celdas separadoras. Las filas de
        # datos tienen diez columnas; cabeceras y pies se descartan por no
        # contener ninguna excepcion reconocible.
        if len(celdas) < 10:
            continue
        detalle = celdas[9]
        coincidencias: list[tuple[str, re.Match[str]]] = []
        coincidencias.extend(
            (TIPO_DUPLICADA, hallada)
            for hallada in _DUPLICADA.finditer(detalle)
        )
        coincidencias.extend(
            (TIPO_MAL_INDEXADA, hallada)
            for hallada in _MAL_INDEXADA.finditer(detalle)
        )
        for tipo, hallada in coincidencias:
            numero = hallada.group("log")
            destino = (
                hallada.group("destino").strip()
                if tipo == TIPO_MAL_INDEXADA
                else ""
            )
            copias = (
                int(hallada.group("copias"))
                if tipo == TIPO_DUPLICADA
                else None
            )
            clave = (tipo, numero, celdas[0], destino, copias)
            if clave in vistos:
                continue
            vistos.add(clave)
            salida.append(
                ExcepcionLogPageAudit(
                    tipo=tipo,
                    matricula_libro=celdas[0],
                    tipo_libro=celdas[1],
                    rango_libro=celdas[3],
                    rango_fechas=celdas[4],
                    faltantes=celdas[5],
                    duplicadas=celdas[6],
                    fecha_sospechosa=celdas[7],
                    detalle=detalle,
                    log_number=numero,
                    destino=destino,
                    copias=copias,
                    url_busqueda=url_busqueda_log(config, numero),
                    url_busqueda_libro=url_busqueda_del_libro(
                        config, celdas[3]
                    ),
                )
            )
    return salida


def _puerto_de(version: dict) -> int:
    hallado = re.search(
        r"://[^:]+:(\d+)/", str(version.get("webSocketDebuggerUrl", ""))
    )
    if hallado is None:
        raise RuntimeError("Edge no informó su puerto de control")
    return int(hallado.group(1))


def _objetivos(puerto: int) -> list[dict]:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{puerto}/json/list", timeout=10
    ) as respuesta:
        datos = json.load(respuesta)
    return datos if isinstance(datos, list) else []


class _Pagina:
    """Pestaña del visor controlada por el protocolo local de Edge."""

    def __init__(
        self,
        version: dict,
        target_id: str,
        cancelar: Callable[[], bool],
        timeout: float = 120.0,
    ) -> None:
        self._cancelar = cancelar
        self._version = version
        self._target_id = target_id
        self.ws: _WebSocket | None = None
        limite = time.monotonic() + 45.0
        while time.monotonic() < limite:
            self._comprobar_cancelacion()
            iguales = [
                objetivo
                for objetivo in _objetivos(_puerto_de(version))
                if objetivo.get("type") == "page"
                and objetivo.get("id") == target_id
            ]
            if iguales:
                self.ws = _WebSocket(
                    iguales[-1]["webSocketDebuggerUrl"], timeout=timeout
                )
                return
            self._dormir(0.5)
        raise RuntimeError("No apareció la pestaña de Log Page Audit en Edge")

    def _comprobar_cancelacion(self) -> None:
        if self._cancelar():
            raise ConsultaCancelada()

    def _dormir(self, segundos: float) -> None:
        limite = time.monotonic() + segundos
        while time.monotonic() < limite:
            self._comprobar_cancelacion()
            time.sleep(min(0.2, max(0.0, limite - time.monotonic())))

    def evaluar(self, expresion: str):
        self._comprobar_cancelacion()
        if self.ws is None:
            raise RuntimeError("La pestaña de Log Page Audit ya no está abierta")
        respuesta = self.ws.pedir(
            "Runtime.evaluate",
            expression=expresion,
            returnByValue=True,
            awaitPromise=True,
        )
        if respuesta.get("exceptionDetails"):
            raise RuntimeError("El visor de Log Page Audit rechazó la consulta")
        return respuesta.get("result", {}).get("value")

    def esperar(
        self, condicion: str, segundos: float, cada: float = 0.5
    ) -> bool:
        limite = time.monotonic() + segundos
        while time.monotonic() < limite:
            self._comprobar_cancelacion()
            try:
                if self.evaluar(f"!!({condicion})"):
                    return True
            except (OSError, RuntimeError, ValueError):
                self._comprobar_cancelacion()
            self._dormir(cada)
        return False

    def cerrar(self) -> None:
        if self.ws is not None:
            self.ws.cerrar()
            self.ws = None
        try:
            navegador = _WebSocket(
                self._version["webSocketDebuggerUrl"], timeout=5.0
            )
            try:
                navegador.pedir("Target.closeTarget", targetId=self._target_id)
            finally:
                navegador.cerrar()
        except (KeyError, OSError, RuntimeError, ValueError):
            pass


class ClienteLogPageAudit:
    """Ejecuta uno o varios filtros de Log Page Audit en una sesión."""

    def __init__(self, config: AirVaultConfig) -> None:
        self.config = config

    def consultar(
        self,
        desde: date,
        hasta: date,
        filtros: Sequence[str],
        avisar: Callable[[str], None] | None = None,
        cancelar: Callable[[], bool] | None = None,
        progreso: Callable[[int, int], None] | None = None,
    ) -> list[ExcepcionLogPageAudit]:
        """Trae las excepciones del reporte en el rango y los filtros dados.

        ``avisar`` es la frase de estado y ``progreso`` la cuenta: cuantos
        reportes van de cuantos. Van por separado porque la frase la lee una
        persona y la cuenta la usa el cronometro de la ventana, que necesita
        numeros y no texto. El primer aviso llega con cero hechos, en cuanto
        el formulario responde: es la senal de que la apertura termino.
        """
        if desde > hasta:
            raise ValueError("La fecha inicial no puede ser posterior a la final")
        if not filtros:
            return []
        notificar = avisar or (lambda _texto: None)
        esta_cancelado = cancelar or (lambda: False)
        avanzar = progreso or (lambda _hechos, _total: None)
        perfil = _perfil_de(self.config)
        notificar("Abriendo Log Page Audit en Edge")
        pagina: _Pagina | None = None
        with SesionDeNavegador(perfil, visible=False) as navegador:
            # Se entra por el enlace federado y no por el reporte: el
            # reporte vive en el servidor de informes, y su enlace lleva a
            # la pantalla de acceso local de AirVault, que pide un usuario y
            # una contrasena que aqui no existen. Por el enlace federado el
            # navegador rehace la sesion solo.
            version = navegador.abrir(
                entrada_federada(self.config),
                espera_s=self.config.espera_login_s,
            )
            esperar_acceso(lambda: navegador.cookies(version), self.config)
            try:
                # La pestana la abre la sesion, que ademas cierra las que
                # hubieran quedado de una consulta anterior: el visor pesa, y
                # dejar copias abiertas cargaba el perfil ejecucion tras
                # ejecucion. Se conduce por su identificador, no por su
                # direccion, para no pilotar una restaurada.
                target_id = navegador.abrir_pestana(LOG_PAGE_AUDIT_URL)
                pagina = _Pagina(version, target_id, esta_cancelado)
                if not pagina.esperar(
                    _FORMULARIO_LISTO, self.config.espera_login_s
                ):
                    raise RuntimeError(
                        "Log Page Audit no quedó listo. Complete el acceso "
                        "en Edge con la cuenta de trabajo y vuelva a intentar."
                    )
                self._elegir_repositorio(pagina)
                if not pagina.esperar(_FECHA_LISTA, 120.0):
                    raise RuntimeError(
                        "El repositorio de producción no habilitó las fechas"
                    )

                resultado: list[ExcepcionLogPageAudit] = []
                avanzar(0, len(filtros))
                for numero, filtro in enumerate(filtros, start=1):
                    nombre = (
                        "mal indexadas"
                        if filtro == FILTRO_MAL_INDEXADAS
                        else "duplicadas"
                    )
                    notificar(f"Consultando páginas {nombre}")
                    filas = self._correr_reporte(pagina, desde, hasta, filtro)
                    resultado.extend(parsear_filas(filas, self.config))
                    avanzar(numero, len(filtros))
                return self._sin_repetidos(resultado)
            finally:
                if pagina is not None:
                    pagina.cerrar()

    @staticmethod
    def _elegir_repositorio(pagina: _Pagina) -> None:
        elegido = pagina.evaluar(
            f"""(function(){{
              var control = document.getElementById('{_CONTROL}ctl03_ddValue');
              if (!control) return false;
              if (control.value !== '1') {{
                control.value = '1';
                if (control.onchange) control.onchange();
                else control.dispatchEvent(new Event('change'));
              }}
              return true;
            }})()"""
        )
        if elegido is not True:
            raise RuntimeError(
                "El formulario de Log Page Audit no mostró el repositorio"
            )

    def _correr_reporte(
        self, pagina: _Pagina, desde: date, hasta: date, filtro: str
    ) -> list[list[object]]:
        valores = {
            f"{_CONTROL}ctl07_txtValue": self._fecha_ssrs(desde),
            f"{_CONTROL}ctl09_txtValue": self._fecha_ssrs(hasta),
            f"{_CONTROL}ctl17_ddValue": filtro,
            f"{_CONTROL}ctl21_ddValue": "2",
            f"{_CONTROL}ctl23_ddValue": "1",
        }
        lanzado = pagina.evaluar(
            """(function(valores, areaId, botonId){
              for (var id in valores) {
                var control = document.getElementById(id);
                if (!control) return 'Falta ' + id;
                control.value = valores[id];
              }
              var area = document.getElementById(areaId);
              if (area) area.innerHTML = '';
              var boton = document.getElementById(botonId);
              if (!boton) return 'Falta ' + botonId;
              boton.click();
              return 'OK';
            })(%s, %s, %s)"""
            % (
                json.dumps(valores),
                json.dumps(_AREA_REPORTE),
                json.dumps(f"{_CONTROL}ctl00"),
            )
        )
        if lanzado != "OK":
            raise RuntimeError(
                f"El formulario de Log Page Audit cambió: {lanzado}"
            )
        self._esperar_reporte(pagina)
        crudas = pagina.evaluar(
            r"""(function(){
              var area = document.getElementById(%s);
              if (!area) return [];
              var salida = [];
              area.querySelectorAll('tr').forEach(function(fila){
                var celdas = [];
                Array.from(fila.children).forEach(function(celda){
                  if (celda.tagName !== 'TD') return;
                  celdas.push({
                    t: (celda.innerText || '').replace(/\s+/g, ' ').trim()
                  });
                });
                if (celdas.length) salida.push(celdas);
              });
              return salida;
            })()"""
            % json.dumps(_AREA_REPORTE)
        )
        return crudas if isinstance(crudas, list) else []

    @staticmethod
    def _esperar_reporte(pagina: _Pagina) -> None:
        inicio = time.monotonic()
        limite = inicio + 1800.0
        while time.monotonic() < limite:
            estado = pagina.evaluar(
                """(function(){
                  var area = document.getElementById(%s);
                  var espera = document.getElementById(%s);
                  var repo = document.getElementById(%s);
                  var desde = document.getElementById(%s);
                  var ocupada = espera &&
                    getComputedStyle(espera).display !== 'none' &&
                    getComputedStyle(espera).visibility !== 'hidden';
                  return {
                    ocupada: !!ocupada,
                    texto: area ? area.innerText.trim().length : 0,
                    repo: repo ? repo.value : '',
                    desde: desde ? desde.value : ''
                  };
                })()"""
                % (
                    json.dumps(_AREA_REPORTE),
                    json.dumps(_ESPERA_REPORTE),
                    json.dumps(f"{_CONTROL}ctl03_ddValue"),
                    json.dumps(f"{_CONTROL}ctl07_txtValue"),
                )
            )
            if (
                isinstance(estado, dict)
                and not estado.get("ocupada")
                and int(estado.get("texto", 0)) > 20
            ):
                return
            if (
                time.monotonic() - inicio > 15.0
                and isinstance(estado, dict)
                and not estado.get("ocupada")
                and (
                    str(estado.get("repo", "")) != "1"
                    or not str(estado.get("desde", ""))
                )
            ):
                raise RuntimeError(
                    "SSRS reinició los parámetros sin generar el reporte"
                )
            pagina._dormir(1.0)
        raise RuntimeError("Log Page Audit no terminó dentro de 30 minutos")

    @staticmethod
    def _fecha_ssrs(valor: date) -> str:
        return f"{valor.month}/{valor.day}/{valor.year}"

    @staticmethod
    def _sin_repetidos(
        excepciones: Iterable[ExcepcionLogPageAudit],
    ) -> list[ExcepcionLogPageAudit]:
        salida: list[ExcepcionLogPageAudit] = []
        vistos: set[tuple[str, str, str, str, int | None]] = set()
        for excepcion in excepciones:
            clave = (
                excepcion.tipo,
                excepcion.log_number,
                excepcion.matricula_libro,
                excepcion.destino,
                excepcion.copias,
            )
            if clave not in vistos:
                vistos.add(clave)
                salida.append(excepcion)
        return salida
