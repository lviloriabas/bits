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
    cierre_diferido_activo,
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

# Valores comprobados en el formulario de produccion de Log Page Audit.
# SSRS usa posiciones del desplegable, no los numeros que muestra.
OPCIONES_REPOSITORIO = (("REPO: Copa:MX:MXDocs", "1"), ("REPO: Copa:MX:MXDocs Test", "2"))
OPCIONES_LIBRO = (("(ALL)", "1"), ("Copa-6", "2"), ("Copa-7", "3"))
OPCIONES_MINIMO = tuple((str(n), str(i)) for i, n in enumerate((5, 10, 25, 40, 50), 1))
OPCIONES_MOSTRAR = (
    ("Ambas", (FILTRO_MAL_INDEXADAS, FILTRO_DUPLICADAS)),
    ("Solo mal indexadas", (FILTRO_MAL_INDEXADAS,)),
    ("Solo duplicadas", (FILTRO_DUPLICADAS,)),
)
OPCIONES_ORDEN = (
    ("AC#, Start Page Number", "1"),
    ("AC#, Book Start Date", "2"),
    ("AC#, Book End Date", "3"),
)
OPCIONES_ACTUALIZAR = (("YES", "1"), ("NO", "2"))


@dataclass(frozen=True)
class ParametrosLogPageAudit:
    repositorio: str = "1"
    tipo_libro: str = "1"
    aeronaves: str = ""
    bitacoras: str = ""
    minimo_paginas: str = "1"
    orden: str = "1"
    actualizar: str = "1"

    def validar(self) -> None:
        for valor, opciones in (
            (self.repositorio, OPCIONES_REPOSITORIO),
            (self.tipo_libro, OPCIONES_LIBRO),
            (self.minimo_paginas, OPCIONES_MINIMO),
            (self.orden, OPCIONES_ORDEN),
            (self.actualizar, OPCIONES_ACTUALIZAR),
        ):
            if valor not in {v for _nombre, v in opciones}:
                raise ValueError("Una opción de Log Page Audit no es válida")

_CONTROL = "ReportViewerControl_ctl04_"
_AREA_REPORTE = "ReportViewerControl_ctl09"
_ESPERA_REPORTE = "ReportViewerControl_AsyncWait"
_PAGINADOR = "ReportViewerControl_ctl05_ctl00_"
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
    """Lee duplicadas y mal indexadas sin perder las filas de continuacion.

    SSRS deja vacios los datos del libro en las filas siguientes del mismo
    grupo. Se heredan incluso al cambiar de pagina, pero nunca de una
    cabecera ni de otro libro.
    """
    salida: list[ExcepcionLogPageAudit] = []
    libro: list[str] | None = None
    config_libro = config
    for fila in filas:
        celdas = [_texto(celda) for celda in fila]
        # El diseño del reporte lleva dos celdas separadoras. Las filas de
        # datos tienen diez columnas; cabeceras y pies se descartan por no
        # contener ninguna excepcion reconocible.
        if len(celdas) == 8:
            # Version sin las dos columnas separadoras del visor.
            celdas = celdas[:2] + [""] + celdas[2:7] + [""] + celdas[7:]
        if len(celdas) != 10:
            continue
        if celdas[0] and celdas[1] and celdas[0] != "AC#":
            libro = celdas[:9]
            repositorios = [
                celda.get("repo_id") for celda in fila
                if isinstance(celda, dict) and celda.get("repo_id")
            ]
            config_libro = config.with_overrides(repo_id=int(repositorios[0])) if repositorios else config
        elif not any(celdas[:9]) and celdas[9] and libro is not None:
            celdas[:9] = libro
        else:
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
                    url_busqueda=url_busqueda_log(config_libro, numero),
                    url_busqueda_libro=url_busqueda_del_libro(
                        config_libro, celdas[3]
                    ),
                )
            )
    return ClienteLogPageAudit._sin_repetidos(salida)


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
        return self._evaluar(expresion)

    def evaluar_al_cerrar(self, expresion: str):
        """Permite soltar cuadros abiertos aunque el trabajo se cancelara."""
        return self._evaluar(expresion)

    def _evaluar(self, expresion: str):
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

    def cerrar(self, forzar: bool = False) -> None:
        if self.ws is not None:
            self.ws.cerrar()
            self.ws = None
        if cierre_diferido_activo() and not forzar:
            return
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
        desde: date | None,
        hasta: date | None,
        filtros: Sequence[str],
        avisar: Callable[[str], None] | None = None,
        cancelar: Callable[[], bool] | None = None,
        progreso: Callable[[int, int], None] | None = None,
        parametros: ParametrosLogPageAudit | None = None,
    ) -> list[ExcepcionLogPageAudit]:
        """Trae las excepciones del reporte en el rango y los filtros dados.

        ``avisar`` es la frase de estado y ``progreso`` la cuenta: cuantos
        reportes van de cuantos. Van por separado porque la frase la lee una
        persona y la cuenta la usa el cronometro de la ventana, que necesita
        numeros y no texto. El primer aviso llega con cero hechos, en cuanto
        el formulario responde: es la senal de que la apertura termino.
        """
        hoy = date.today()
        hasta = hasta or hoy
        if hasta > hoy or (desde is not None and desde > hoy):
            raise ValueError("Las fechas no pueden ser posteriores al día actual")
        if desde is not None and desde > hasta:
            raise ValueError("La fecha inicial no puede ser posterior a la final")
        opciones = parametros or ParametrosLogPageAudit()
        opciones.validar()
        if any(filtro not in (FILTRO_MAL_INDEXADAS, FILTRO_DUPLICADAS) for filtro in filtros):
            raise ValueError("Elija duplicadas, mal indexadas o ambas")
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
                self._elegir_repositorio(pagina, opciones.repositorio)
                if not pagina.esperar(_FECHA_LISTA, 120.0):
                    raise RuntimeError(
                        "El repositorio no habilitó los parámetros del reporte"
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
                    filas = self._correr_reporte(
                        pagina, desde, hasta, filtro, opciones, notificar
                    )
                    tipo = TIPO_MAL_INDEXADA if filtro == FILTRO_MAL_INDEXADAS else TIPO_DUPLICADA
                    resultado.extend(
                        item for item in parsear_filas(filas, self.config)
                        if item.tipo == tipo
                    )
                    avanzar(numero, len(filtros))
                return self._sin_repetidos(resultado)
            finally:
                if pagina is not None:
                    pagina.cerrar()

    @staticmethod
    def _elegir_repositorio(pagina: _Pagina, repositorio: str = "1") -> None:
        elegido = pagina.evaluar(
            """(function(valor){
              var c = document.getElementById('%sctl03_ddValue');
              if (!c || !Array.from(c.options).some(o => o.value === valor)) return false;
              if (c.value !== valor) {
                c.value = valor;
                c.dispatchEvent(new Event('change', {bubbles: true}));
              }
              return true;
            })(%s)""" % (_CONTROL, json.dumps(repositorio))
        )
        if elegido is not True:
            raise RuntimeError("El formulario no mostró el repositorio elegido")

    @classmethod
    def _valores(cls, desde, hasta, filtro, parametros):
        return {
            f"{_CONTROL}ctl05_ddValue": parametros.tipo_libro,
            f"{_CONTROL}ctl07_txtValue": cls._fecha_ssrs(desde) if desde else "",
            f"{_CONTROL}ctl09_txtValue": cls._fecha_ssrs(hasta) if hasta else "",
            f"{_CONTROL}ctl11_txtValue": parametros.aeronaves.strip(),
            f"{_CONTROL}ctl13_txtValue": parametros.bitacoras.strip(),
            f"{_CONTROL}ctl15_ddValue": parametros.minimo_paginas,
            f"{_CONTROL}ctl17_ddValue": filtro,
            f"{_CONTROL}ctl19_ddValue": parametros.orden,
            f"{_CONTROL}ctl21_ddValue": "1",  # For Export siempre NO.
            f"{_CONTROL}ctl23_ddValue": parametros.actualizar,
        }

    def _correr_reporte(
        self, pagina: _Pagina, desde: date | None, hasta: date | None,
        filtro: str, parametros: ParametrosLogPageAudit | None = None,
        avisar: Callable[[str], None] | None = None,
    ) -> list[list[object]]:
        opciones = parametros or ParametrosLogPageAudit()
        valores = self._valores(desde, hasta, filtro, opciones)
        nulos = {
            f"{_CONTROL}ctl07_cbNull": desde is None,
            f"{_CONTROL}ctl09_cbNull": hasta is None,
        }
        anterior = self._estado_reporte(pagina).get("huella", "")
        lanzado = pagina.evaluar(
            """(function(valores, nulos, botonId){
              // Primero validar todo: una opcion inexistente no queda en blanco.
              for (var id in valores) {
                var c = document.getElementById(id);
                if (!c) return 'Falta ' + id;
                if (c.options && !Array.from(c.options).some(o => o.value === valores[id]))
                  return 'Opcion no disponible: ' + id;
              }
              for (var id in nulos) {
                var c = document.getElementById(id);
                if (!c) return 'Falta ' + id;
                if (c.checked !== nulos[id]) c.click();
              }
              for (var id in valores) document.getElementById(id).value = valores[id];
              var boton = document.getElementById(botonId);
              if (!boton || boton.disabled) return 'El reporte no esta listo';
              boton.click();
              return 'OK';
            })(%s, %s, %s)"""
            % (json.dumps(valores), json.dumps(nulos), json.dumps(f"{_CONTROL}ctl00"))
        )
        if lanzado != "OK":
            raise RuntimeError(f"El formulario de Log Page Audit cambió: {lanzado}")
        esperados = dict(valores, **{f"{_CONTROL}ctl03_ddValue": opciones.repositorio})
        self._esperar_reporte(pagina, anterior, 1, esperados, nulos)
        return self._leer_todas_paginas(pagina, esperados, nulos, avisar)

    @staticmethod
    def _estado_reporte(pagina: _Pagina) -> dict:
        estado = pagina.evaluar(
            """(function(areaId, esperaId, prefijo, paginador){
              function visible(c) { return !!c && c.getClientRects().length > 0 &&
                getComputedStyle(c).visibility !== 'hidden'; }
              var area = document.getElementById(areaId);
              var actual = document.getElementById(paginador + 'CurrentPage');
              var total = document.getElementById(paginador + 'TotalPages');
              var boton = document.getElementById(prefijo + 'ctl00');
              var siguiente = document.getElementById(paginador + 'Next_ctl00_ctl00');
              var valores = {}, nulos = {};
              document.querySelectorAll('[id^="' + prefijo + '"]').forEach(c => {
                if (c.tagName === 'INPUT' || c.tagName === 'SELECT') {
                  if (c.type === 'checkbox') nulos[c.id] = c.checked;
                  else valores[c.id] = c.value;
                }
              });
              var errores = Array.from(document.querySelectorAll(
                '[id*="ErrorMessage"], [id*="ValidationSummary"]'))
                .filter(visible).map(c => c.innerText.trim()).filter(Boolean).join(' ');
              return {
                ocupada: visible(document.getElementById(esperaId)) || !!(boton && boton.disabled),
                huella: area ? area.innerHTML : '',
                texto: area ? area.innerText.trim() : '',
                actual: actual ? Number(actual.value) : 0,
                total: total ? total.innerText.trim() : '',
                siguiente: visible(siguiente) && !siguiente.disabled,
                valores: valores, nulos: nulos, errores: errores
              };
            })(%s, %s, %s, %s)"""
            % tuple(json.dumps(v) for v in (_AREA_REPORTE, _ESPERA_REPORTE, _CONTROL, _PAGINADOR))
        )
        if not isinstance(estado, dict):
            raise RuntimeError("No se pudo leer el estado de Log Page Audit")
        return estado

    @staticmethod
    def _comprobar_parametros(estado, valores, nulos) -> None:
        for id, esperado in (valores or {}).items():
            recibido = estado.get("valores", {}).get(id)
            if id.endswith(('ctl07_txtValue', 'ctl09_txtValue')):
                # El visor puede devolver mes y dia con ceros iniciales.
                if esperado:
                    try:
                        recibido = '/'.join(str(int(n)) for n in recibido.split('/'))
                    except (AttributeError, ValueError):
                        recibido = None
            if recibido != esperado:
                raise RuntimeError("AirVault cambió los filtros o las fechas; vuelva a consultar")
        for id, esperado in (nulos or {}).items():
            if estado.get("nulos", {}).get(id) != esperado:
                raise RuntimeError("AirVault cambió el límite de fechas; vuelva a consultar")

    @classmethod
    def _esperar_reporte(
        cls, pagina: _Pagina, anterior: str = "", pagina_esperada: int = 1,
        valores: dict | None = None, nulos: dict | None = None,
    ) -> None:
        limite = time.monotonic() + 1800.0
        while time.monotonic() < limite:
            estado = cls._estado_reporte(pagina)
            if not estado.get("ocupada"):
                if estado.get("errores"):
                    raise RuntimeError(str(estado["errores"]))
                if (estado.get("huella") and estado["huella"] != anterior
                        and estado.get("texto") and estado.get("actual") == pagina_esperada):
                    cls._comprobar_parametros(estado, valores, nulos)
                    return
            pagina._dormir(0.5)
        raise RuntimeError(f"Log Page Audit no terminó de cargar la página {pagina_esperada}")

    @staticmethod
    def _leer_filas(pagina: _Pagina) -> list[list[object]]:
        crudas = pagina.evaluar(
            r"""(function(){
              var area = document.getElementById(%s);
              if (!area) return null;
              var salida = [];
              area.querySelectorAll('tr').forEach(function(fila){
                var celdas = Array.from(fila.children).filter(c => c.tagName === 'TD');
                // Las tablas exteriores contienen el reporte entero; no son datos.
                if (![8, 10].includes(celdas.length)) return;
                salida.push(celdas.map(c => ({
                  t: (c.innerText || '').replace(/\s+/g, ' ').trim(),
                  repo_id: (function(){
                    var a = c.querySelector('a[href]');
                    var m = a && a.getAttribute('href').match(/repoId=(\d+)/);
                    return m ? Number(m[1]) : null;
                  })()
                })));
              });
              return salida;
            })()""" % json.dumps(_AREA_REPORTE)
        )
        if not isinstance(crudas, list):
            raise RuntimeError("No se pudieron leer las filas del reporte")
        return crudas

    @classmethod
    def _leer_todas_paginas(cls, pagina, valores=None, nulos=None, avisar=None):
        filas = []
        numero = 1
        notificar = avisar or (lambda _texto: None)
        while True:
            estado = cls._estado_reporte(pagina)
            cls._comprobar_parametros(estado, valores, nulos)
            if estado.get("ocupada") or estado.get("actual") != numero:
                raise RuntimeError("El visor cambió de página durante la lectura")
            notificar(f"Leyendo página {numero} de {estado.get('total') or '?'}")
            filas.extend(cls._leer_filas(pagina))
            # SSRS puede anunciar un total provisional ("2?"). Se avanza hasta
            # que deshabilite Siguiente y confirme el total definitivo.
            total = str(estado.get("total", "")).strip()
            if not estado.get("siguiente"):
                if not total.isdigit() or int(total) != numero:
                    raise RuntimeError("El visor no confirmó la última página; el reporte está incompleto")
                return filas
            anterior = estado["huella"]
            pulsado = pagina.evaluar(
                """(function(){
                  var c = document.getElementById(%s);
                  if (!c || c.disabled || !c.getClientRects().length) return false;
                  c.click(); return true;
                })()""" % json.dumps(f"{_PAGINADOR}Next_ctl00_ctl00")
            )
            if pulsado is not True:
                raise RuntimeError("No se pudo avanzar; el reporte está incompleto")
            numero += 1
            cls._esperar_reporte(pagina, anterior, numero, valores, nulos)

    @staticmethod
    def _fecha_ssrs(valor: date) -> str:
        return f"{valor.month}/{valor.day}/{valor.year}"

    @staticmethod
    def _sin_repetidos(excepciones: Iterable[ExcepcionLogPageAudit]) -> list[ExcepcionLogPageAudit]:
        return list(dict.fromkeys(excepciones))
