"""Lee la consulta guardada de Web Search con la sesion iniciada en Edge."""

from pathlib import Path
from threading import Lock
import json
import re
import time

from loguru import logger

from app.airvault.cookies import dominio
from app.airvault.errores_websearch import ErrorDeWebSearch, MOTIVOS, mensaje_error_websearch
from app.airvault.correcciones import (
    _MENSAJE,
    _NavegadorDeCorrecciones,
)
from app.airvault.navegador import PERFIL_POR_DEFECTO, _WebSocket
from app.airvault.session import ErrorDeSesion, SesionCancelada, comprobar_o_renovar
from app.airvault.web_reports import ConsultaCancelada, ErrorDePaginaAirVault, _Pagina, url_busqueda_log


_ACCESO_PEDIDO = (
    "location.pathname.toLowerCase().includes('/signin2/') || "
    "location.hostname.toLowerCase() === 'login.microsoftonline.com' || "
    "!!document.querySelector('form[action*=dosignin], input[name=wresult]')"
)
_LEER_MUESTRA = """(function(){
  var tabla = document.querySelector('.ui-jqgrid-btable');
  if (!tabla || !window.jQuery || !jQuery.getResultRowData) return null;
  if (tabla.grid && tabla.grid.hDiv && tabla.grid.hDiv.loading) return null;
  var pie = document.querySelector('[id$=Pager_right]');
  if (!pie || !pie.innerText.trim()) return null;
  var parametros = tabla.p || {};
  var total = parametros.records, visibles = parametros.reccount;
  try {
    if (typeof jQuery(tabla).jqGrid === 'function') {
      total = jQuery(tabla).jqGrid('getGridParam', 'records') ?? total;
      visibles = jQuery(tabla).jqGrid('getGridParam', 'reccount') ?? visibles;
    }
  } catch (e) { }
  var filas = tabla.querySelectorAll('tr.jqgrow');
  var cantidadConocida = /^\\d+$/.test(String(total));
  if (/^\\d+$/.test(String(visibles)) && filas.length !== Number(visibles)) return null;
  if (cantidadConocida && Number(total) < filas.length) return null;
  if (!filas.length && !(cantidadConocida && Number(total) === 0) &&
      !/no records|no hay registros|sin registros/i.test(pie.innerText)) return null;
  var salida = [];
  for (var i = 0; i < filas.length; i++) {
    var datos;
    try { datos = jQuery.getResultRowData(filas[i].id); } catch (e) { return null; }
    if (!datos || typeof datos.LogNo === 'undefined') return null;
    if (!/^\\d{7}$/.test(String(datos.LogNo).trim())) return null;
    salida.push({log: String(datos.LogNo).trim()});
  }
  return salida;
})()"""

# El pie puede aparecer antes de que las filas terminen de cargarse.
# Solo una lectura valida, incluso vacia, permite decidir si esta ausente.
_LISTA_O_ACCESO = f"({_ACCESO_PEDIDO}) || !!({_MENSAJE}) || (({_LEER_MUESTRA}) !== null)"


class AccesoWebSearch:
    """Comparte el navegador autenticado, con una pagina propia por batch."""

    def __init__(self, config, sesion, avisar=None, cancelado=lambda: False):
        self.config = config
        self.sesion = sesion
        self.avisar = avisar or (lambda _texto: None)
        self.cancelado = cancelado
        self.navegador = None
        self.version = None
        self.generacion = 0
        self._acceso = Lock()
        self._error_acceso = None

    def __enter__(self):
        self.avisar("Iniciando sesión en Web Search")
        comprobar_o_renovar(self.sesion, self.avisar, websearch=True)
        if self.cancelado():
            raise SesionCancelada("Se canceló el acceso a Web Search")
        perfil = Path(self.config.perfil_navegador) if self.config.perfil_navegador else PERFIL_POR_DEFECTO
        self.navegador = _NavegadorDeCorrecciones(perfil)
        self.navegador.__enter__()
        try:
            self.version = self.navegador.abrir(self.config.url_sso, espera_s=self.config.espera_login_s)
            self._usar_cookies()
        except BaseException:
            self.navegador.__exit__(None, None, None)
            self.navegador = None
            raise
        self.avisar("Sesión de Web Search lista; consultando batches")
        return self

    def __exit__(self, *error):
        if self.navegador is not None:
            self.navegador.__exit__(*error)
            self.navegador = None

    def _usar_cookies(self):
        """Mantiene el navegador y la conexion HTTP en la misma cuenta."""
        host = dominio(self.config.base_url)
        cookies = []
        for cookie in self.sesion.http.cookies:
            origen = cookie.domain.lstrip(".")
            if origen and host != origen and not host.endswith("." + origen):
                continue
            dato = dict(name=cookie.name, value=cookie.value,
                        domain=cookie.domain or host, path=cookie.path or "/",
                        secure=bool(cookie.secure))
            if cookie.expires is not None:
                dato["expires"] = float(cookie.expires)
            cookies.append(dato)
        if cookies:
            ws = _WebSocket(self.version["webSocketDebuggerUrl"])
            try:
                ws.pedir("Storage.setCookies", cookies=cookies)
            finally:
                ws.cerrar()

    def crear_lector(self):
        return LectorWebSearch(self)

    def renovar(self, generacion):
        """Un solo acceso nuevo aunque varios batches encuentren la caducidad."""
        with self._acceso:
            if generacion != self.generacion:
                return
            if self._error_acceso is not None:
                raise self._error_acceso
            self.avisar("Web Search pide acceso; iniciando sesión de nuevo")
            try:
                self.sesion.renovar_en_navegador(self.avisar)
                comprobar_o_renovar(self.sesion, self.avisar, websearch=True)
                if self.navegador is not None:
                    self.version = self.navegador.abrir(self.config.url_sso, espera_s=self.config.espera_login_s)
                self._usar_cookies()
            except Exception as exc:
                self._error_acceso = exc
                raise
            self.generacion += 1


class LectorWebSearch:
    """Reutiliza una pestaña para los numeros de la muestra de un batch."""

    def __init__(self, acceso):
        self.acceso = acceso
        self.sesion = acceso.sesion
        self.config = acceso.config
        self.generacion = acceso.generacion
        self.pagina = None

    def consultar_numero(self, numero, presupuesto_s):
        limite = time.monotonic() + max(0.0, presupuesto_s)
        for intento in range(2):
            if self.acceso.cancelado():
                raise SesionCancelada("Se canceló la consulta de bitácoras en Web Search")
            restante = limite - time.monotonic()
            if restante <= 0:
                raise ErrorDeWebSearch("carga")
            try:
                return self._consultar_numero(numero, min(restante, 30.0) if intento == 0 else restante)
            except (TimeoutError, ConnectionError, ErrorDePaginaAirVault, ErrorDeWebSearch) as exc:
                self.cerrar()
                if isinstance(exc, ErrorDeWebSearch) and exc.motivo in ("permisos", "rechazo"):
                    raise
                if intento == 1 or time.monotonic() >= limite:
                    if isinstance(exc, ErrorDeWebSearch):
                        raise
                    raise ErrorDeWebSearch("navegador") from exc
                logger.warning("Web Search reabre la lectura de la bitacora {}: {}", numero, exc)

    def _consultar_numero(self, numero, presupuesto_s):
        url = url_busqueda_log(self.config, numero)
        try:
            if self.pagina is None:
                target = self.acceso.navegador.abrir_pestana(url, version=self.acceso.version)
                try:
                    self.pagina = _Pagina(self.acceso.version, target, self.acceso.cancelado,
                                          timeout=min(15.0, max(1.0, presupuesto_s)))
                except BaseException:
                    self.acceso.navegador.cerrar_pestana(self.acceso.version, target)
                    raise
                condicion = _LISTA_O_ACCESO
            else:
                self.pagina.evaluar(
                    "window.__bits_busqueda_anterior = true; location.assign(" + json.dumps(url) + "); true"
                )
                condicion = "typeof window.__bits_busqueda_anterior === 'undefined' && (" + _LISTA_O_ACCESO + ")"
            if not self.pagina.esperar(condicion, presupuesto_s, cada=0.2):
                raise ErrorDeWebSearch("carga")
            if self.pagina.evaluar(_ACCESO_PEDIDO):
                raise ErrorDeSesion("Web Search pide iniciar sesión de nuevo.")
            aviso = self.pagina.evaluar(_MENSAJE)
            if aviso:
                logger.warning("Web Search rechazo la bitacora {}: {}", numero, aviso)
                motivo = mensaje_error_websearch(aviso)
                if motivo == MOTIVOS["acceso"]:
                    raise ErrorDeSesion(motivo)
                raise ErrorDeWebSearch("permisos" if motivo == MOTIVOS["permisos"] else "rechazo")
            filas = self.pagina.evaluar(_LEER_MUESTRA)
            if (not isinstance(filas, list) or any(not isinstance(fila, dict)
                    or not re.fullmatch(r"\d{7}", str(fila.get("log", "")).strip()) for fila in filas)):
                raise ErrorDeWebSearch("lectura")
            return {"rows": [{"LogNo": str(fila.get("log", "")).strip()}
                             for fila in filas if isinstance(fila, dict)]}
        except ConsultaCancelada as exc:
            raise SesionCancelada("Se canceló la búsqueda en Web Search") from exc

    def cerrar(self):
        if self.pagina is not None:
            self.pagina.cerrar(forzar=True)
            self.pagina = None
