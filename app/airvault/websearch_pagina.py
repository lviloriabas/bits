"""Lee la consulta guardada de Web Search con la sesion iniciada en Edge."""

from pathlib import Path
from threading import Lock
import json
import re

from app.airvault.cookies import dominio
from app.airvault.correcciones import (
    _NavegadorDeCorrecciones,
    _REJILLA_CARGADA,
)
from app.airvault.navegador import PERFIL_POR_DEFECTO, _WebSocket
from app.airvault.session import ErrorDeSesion, SesionCancelada, comprobar_o_renovar
from app.airvault.web_reports import ConsultaCancelada, _Pagina, url_busqueda_log


_ACCESO_PEDIDO = (
    "location.pathname.toLowerCase().includes('/signin2/') || "
    "location.hostname.toLowerCase() === 'login.microsoftonline.com' || "
    "!!document.querySelector('form[action*=dosignin], input[name=wresult]')"
)
_LISTA_O_ACCESO = f"({_REJILLA_CARGADA}) || ({_ACCESO_PEDIDO})"

_LEER_MUESTRA = """(function(){
  var tabla = document.querySelector('.ui-jqgrid-btable');
  if (!tabla || !window.jQuery || !jQuery.getResultRowData) return null;
  if (tabla.grid && tabla.grid.hDiv && tabla.grid.hDiv.loading) return null;
  var total = jQuery(tabla).jqGrid('getGridParam', 'records');
  var visibles = jQuery(tabla).jqGrid('getGridParam', 'reccount');
  if (!/^\\d+$/.test(String(total)) || !/^\\d+$/.test(String(visibles))) return null;
  var filas = tabla.querySelectorAll('tr.jqgrow');
  if (filas.length !== Number(visibles) || Number(total) < filas.length) return null;
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
        url = url_busqueda_log(self.config, numero)
        try:
            if self.pagina is None:
                target = self.acceso.navegador.abrir_pestana(url, version=self.acceso.version)
                try:
                    self.pagina = _Pagina(self.acceso.version, target, self.acceso.cancelado,
                                          timeout=min(5.0, max(1.0, presupuesto_s)))
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
                raise TimeoutError("Web Search no terminó de responder la búsqueda.")
            if self.pagina.evaluar(_ACCESO_PEDIDO):
                raise ErrorDeSesion("Web Search pide iniciar sesión de nuevo.")
            filas = self.pagina.evaluar(_LEER_MUESTRA)
            if (not isinstance(filas, list) or any(not isinstance(fila, dict)
                    or not re.fullmatch(r"\d{7}", str(fila.get("log", "")).strip()) for fila in filas)):
                raise RuntimeError("No se pudieron leer los resultados de Web Search.")
            return {"rows": [{"LogNo": str(fila.get("log", "")).strip()}
                             for fila in filas if isinstance(fila, dict)]}
        except ConsultaCancelada as exc:
            raise SesionCancelada("Se canceló la búsqueda en Web Search") from exc

    def cerrar(self):
        if self.pagina is not None:
            self.pagina.cerrar(forzar=True)
            self.pagina = None
