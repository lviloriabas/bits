"""Lee la consulta guardada de Web Search con la sesion iniciada en Edge."""

from pathlib import Path
from threading import Lock
import json

from app.airvault.cookies import dominio
from app.airvault.correcciones import (
    _LEER_REJILLA,
    _NavegadorDeCorrecciones,
    _REJILLA_CARGADA,
)
from app.airvault.navegador import PERFIL_POR_DEFECTO, _WebSocket
from app.airvault.session import ErrorDeSesion, SesionCancelada, comprobar_o_renovar
from app.airvault.web_reports import ConsultaCancelada, _Pagina, url_busqueda_log


_ACCESO_PEDIDO = (
    "location.href.toLowerCase().includes('/signin2/') || "
    "location.hostname.toLowerCase() === 'login.microsoftonline.com'"
)
_LISTA_O_ACCESO = f"({_REJILLA_CARGADA}) || ({_ACCESO_PEDIDO})"


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
            self.avisar("Web Search pide acceso; iniciando sesión de nuevo")
            self.sesion.renovar_en_navegador(self.avisar)
            comprobar_o_renovar(self.sesion, self.avisar, websearch=True)
            self._usar_cookies()
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
            filas = self.pagina.evaluar(_LEER_REJILLA)
            if not isinstance(filas, list):
                raise RuntimeError("No se pudieron leer los resultados de Web Search.")
            return {"rows": [{"LogNo": str(fila.get("log", "")).strip()}
                             for fila in filas if isinstance(fila, dict)]}
        except ConsultaCancelada as exc:
            raise SesionCancelada("Se canceló la búsqueda en Web Search") from exc

    def cerrar(self):
        if self.pagina is not None:
            self.pagina.cerrar(forzar=True)
            self.pagina = None
