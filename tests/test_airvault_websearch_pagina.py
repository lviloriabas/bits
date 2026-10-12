"""El chequeo de publicacion usa Web Search y recupera el acceso caducado."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app.airvault import websearch_pagina as modulo
from app.airvault.config import AirVaultConfig
from app.airvault.confirmacion import verificar_batch
from app.airvault.errores_websearch import ErrorDeWebSearch
from app.airvault.session import ErrorDeSesion, SesionAirVault, SesionCancelada, abrir_sesion, comprobar_o_renovar
from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker
from tests.test_airvault_rework import trabajo


def test_abre_el_login_comprobando_websearch_desde_el_principio(monkeypatch):
    consultas = []

    def entrar(sesion, *_args):
        consultas.append(sesion._comprobar_en_websearch)
        return sesion

    monkeypatch.delenv("AIRVAULT_COOKIE", raising=False)
    monkeypatch.setattr(SesionAirVault, "usar_navegador", entrar)
    sesion = abrir_sesion(AirVaultConfig(), websearch=True)
    assert consultas == [True]
    assert sesion._comprobar_en_websearch
    sesion.http.close()


def test_cookie_caducada_renueva_login_y_solo_comprueba_websearch(monkeypatch):
    from tests.test_airvault_errores import RespuestaFalsa, sesion as crear_sesion
    sesion = crear_sesion([RespuestaFalsa(status_code=401), RespuestaFalsa(text="Web Search")])
    entradas = []
    monkeypatch.setattr(sesion, "renovar_en_navegador", lambda _avisar: entradas.append("login"))
    assert comprobar_o_renovar(sesion, websearch=True) == 0
    assert entradas == ["login"]
    assert [urlparse(url).path for _, url in sesion.http.pedidas] == ["/zfp/", "/zfp/"]


def test_cancelar_el_login_no_abre_otra_ventana(monkeypatch):
    sesion = SesionAirVault(AirVaultConfig())
    sesion.cancelar()
    monkeypatch.setattr(sesion, "renovar_en_navegador", lambda *_: pytest.fail("No se renueva al cancelar"))
    with pytest.raises(SesionCancelada):
        comprobar_o_renovar(sesion, websearch=True)
    sesion.http.close()


class NavegadorFalso:
    def __init__(self):
        self.urls = []
        self.cerradas = []

    def abrir_pestana(self, url, version):
        self.urls.append(url)
        return "propia"

    def cerrar_pestana(self, version, target):
        self.cerradas.append(target)


class PaginaFalsa:
    def __init__(self, *_args, **_kwargs):
        self.expresiones = []
        self.esperas = []
        self.filas = [{"log": "2000001"}]
        self.login = False
        self.lista = True
        self.cerrada = False

    def evaluar(self, expresion):
        self.expresiones.append(expresion)
        if expresion == modulo._ACCESO_PEDIDO:
            return self.login
        if expresion == modulo._LEER_MUESTRA:
            return self.filas

    def esperar(self, condicion, presupuesto, cada):
        self.esperas.append((condicion, presupuesto))
        return self.lista

    def cerrar(self, forzar):
        self.cerrada = True


@pytest.fixture
def lector(monkeypatch):
    pagina = PaginaFalsa()
    monkeypatch.setattr(modulo, "_Pagina", lambda *_a, **_k: pagina)
    acceso = SimpleNamespace(config=AirVaultConfig(), sesion=object(), generacion=0,
                             navegador=NavegadorFalso(), version={}, cancelado=lambda: False)
    return modulo.LectorWebSearch(acceso), pagina


def test_busca_numero_exacto_en_consulta_real_y_no_reutiliza_la_rejilla_vieja(lector):
    lector, pagina = lector
    assert lector.consultar_numero("2000001", 12) == {"rows": [{"LogNo": "2000001"}]}
    url = lector.acceso.navegador.urls[0]
    assert urlparse(url).path == "/zfp/client/unencryptedparamssearch/"
    parametros = parse_qs(urlparse(url).query)
    assert parametros["searchParams"] == ["1=LOG PAGE\t3=2000001\t4=2000001"]
    assert parametros["searchId"] == ["15504"]
    pagina.filas = [{"log": "2000002"}]
    assert lector.consultar_numero("2000002", 9) == {"rows": [{"LogNo": "2000002"}]}
    assert len(lector.acceso.navegador.urls) == 1
    assert any("__bits_busqueda_anterior" in expresion for expresion in pagina.expresiones)
    assert "typeof window.__bits_busqueda_anterior === 'undefined'" in pagina.esperas[-1][0]
    lector.cerrar()
    assert pagina.cerrada


def test_websearch_vacio_no_confirma_la_publicacion(lector, tmp_path):
    lector, pagina = lector
    pagina.filas = []
    resultado = verificar_batch(lector, trabajo(tmp_path, "pendiente"))
    assert not resultado.confirmado
    assert resultado.encontradas == 0
    assert len(pagina.esperas) == 7


def test_login_se_propaga_para_renovarlo_en_vez_de_declarar_websearch_no_disponible(lector):
    lector, pagina = lector
    pagina.login = True
    with pytest.raises(ErrorDeSesion, match="iniciar sesión"):
        lector.consultar_numero("2000001", 12)
    assert modulo._LEER_MUESTRA not in pagina.expresiones


def test_si_la_pagina_no_carga_no_lee_resultados_anteriores(lector):
    lector, pagina = lector
    pagina.lista = False
    with pytest.raises(ErrorDeWebSearch, match="no terminó de cargar"):
        lector.consultar_numero("2000001", 12)
    assert modulo._LEER_MUESTRA not in pagina.expresiones


def test_fallo_al_conectar_cierra_solo_la_pestana_propia(monkeypatch, lector):
    lector, _ = lector

    def fallar(*_a, **_k):
        raise RuntimeError("No abre")

    monkeypatch.setattr(modulo, "_Pagina", fallar)
    with pytest.raises(RuntimeError):
        lector.consultar_numero("2000001", 12)
    assert lector.acceso.navegador.cerradas == ["propia"]


class _EdgeFalso:
    """Lo justo del protocolo de Edge para ver que cookies se le escriben."""

    def __init__(self, presentes=()):
        self.presentes = list(presentes)
        self.pedidos = []

    def __call__(self, *_a, **_k):
        return self

    def pedir(self, accion, **datos):
        self.pedidos.append((accion, datos))
        return {"cookies": self.presentes} if accion == "Storage.getCookies" else {}

    def cerrar(self):
        return None

    @property
    def escritas(self):
        return [c for accion, datos in self.pedidos if accion == "Storage.setCookies"
                for c in datos["cookies"]]


def _acceso_con(monkeypatch, sesion, presentes=()):
    edge = _EdgeFalso(presentes)
    monkeypatch.setattr(modulo, "_WebSocket", edge)
    acceso = modulo.AccesoWebSearch(sesion.config, sesion)
    acceso.version = {"webSocketDebuggerUrl": "ws://local"}
    return acceso, edge


def test_al_transferir_el_acceso_no_copia_cookies_de_otros_sitios(monkeypatch):
    sesion = SesionAirVault(AirVaultConfig())
    host = urlparse(sesion.config.base_url).hostname
    sesion.http.cookies.set("FedAuth", "prueba", domain=host, path="/")
    sesion.http.cookies.set("ajena", "prueba", domain="otro.test", path="/")
    acceso, edge = _acceso_con(monkeypatch, sesion)
    acceso._usar_cookies()
    assert [c["name"] for c in edge.escritas] == ["FedAuth"]
    # La que se crea es del sitio y segura, no una copia a medias.
    assert edge.escritas[0]["secure"] and edge.escritas[0]["httpOnly"]
    sesion.http.close()


def test_la_sesion_tomada_de_edge_no_se_le_vuelve_a_escribir_a_edge(monkeypatch):
    """Era lo que rompia el acceso: copias viejas tapando a las del sitio.

    De la conexion solo se sabe el nombre y el valor de cada cookie.
    Escritas asi en el navegador quedaban sin dominio ni marca de segura
    junto a las que puso AirVault; al renovarse estas, viajaban las dos y
    el servidor pedia acceso sin parar.
    """
    sesion = SesionAirVault(AirVaultConfig())
    sesion.usar_cookies({"Critical": "sesion", "ProdSSO": "sso"}, "navegador")
    acceso, edge = _acceso_con(monkeypatch, sesion)
    acceso._usar_cookies()
    assert edge.pedidos == []
    sesion.http.close()


def test_una_cookie_pegada_va_sobre_la_del_sitio_sin_duplicarla(monkeypatch):
    sesion = SesionAirVault(AirVaultConfig())
    sesion.usar_cookie("ProdSSO=pegada; Critical=pegada")
    del_sitio = {"name": "ProdSSO", "value": "vieja", "domain": ".criticaltech.com",
                 "path": "/", "secure": True, "httpOnly": True, "session": True}
    ajena = {"name": "ProdSSO", "value": "otra", "domain": ".otro.test", "path": "/"}
    acceso, edge = _acceso_con(monkeypatch, sesion, [del_sitio, ajena])
    acceso._usar_cookies()
    escritas = {c["name"]: c for c in edge.escritas}
    assert len(edge.escritas) == 2
    # Conserva el dominio y las marcas de la que ya estaba.
    assert escritas["ProdSSO"] == {"name": "ProdSSO", "value": "pegada", "domain": ".criticaltech.com",
                                   "path": "/", "secure": True, "httpOnly": True}
    assert escritas["Critical"]["domain"] == urlparse(sesion.config.base_url).hostname
    sesion.http.close()


def test_batched_parallel_access_expiry_logs_in_once_and_retries(app, tmp_path, monkeypatch):
    """Dos batches caducados se renuevan juntos y conservan la concurrencia."""
    trabajos = [trabajo(tmp_path, nombre) for nombre in ("uno", "dos")]
    sesion = SesionAirVault(trabajos[0].config)
    sesion.usar_cookie("FedAuth=prueba")
    consultas, entradas, cerrados = [], [], []
    barrera = Barrier(2)
    monkeypatch.setattr(modulo, "comprobar_o_renovar", lambda *_a, **_k: entradas.append("comprobar"))
    monkeypatch.setattr(SesionAirVault, "renovar_sin_ventana", lambda *_a, **_k: entradas.append("renovar"))
    monkeypatch.setattr(modulo.AccesoWebSearch, "_usar_cookies", lambda _: None)
    monkeypatch.setattr(modulo.AccesoWebSearch, "__enter__", lambda acceso: acceso)
    monkeypatch.setattr(modulo.AccesoWebSearch, "__exit__", lambda *_: None)

    class Lector:
        def __init__(self, acceso):
            self.acceso, self.generacion = acceso, acceso.generacion

        def consultar_numero(self, numero, _presupuesto):
            if self.generacion == 0:
                barrera.wait(timeout=5)
                raise ErrorDeSesion("Caducada")
            consultas.append(numero)
            return {"rows": [{"LogNo": numero}]}

        def cerrar(self):
            cerrados.append(self)

    monkeypatch.setattr(modulo.AccesoWebSearch, "crear_lector", lambda acceso: Lector(acceso))
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "config": trabajos[0].config, "raiz": tmp_path, "sesion_base": sesion,
        "buscar_trabajos": trabajos,
    })
    resultados = []
    worker.buscado.connect(lambda datos: resultados.extend(datos["resultados"]))
    worker._buscar_websearch()
    assert all(ok for _t, ok in resultados), [t.manifiesto.websearch_detalle for t, _ok in resultados]
    assert entradas == ["renovar", "comprobar"]
    assert len(consultas) == 14
    assert all(t.manifiesto.etapa_hecha("completar") for t in trabajos)
    assert len(set(cerrados)) == 4
    assert worker.estado["sesion"] is not sesion
    assert not sesion.cancelada
    sesion.http.close()


def test_muestra_estado_de_login_mientras_websearch_abre(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    ventana._mostrar_paso_websearch("Iniciando sesión en Web Search", 0, 0)
    assert ventana.estado_label.text() == "Iniciando sesión en Web Search"


@pytest.mark.parametrize("filas", [None, [{"error": "Sin permisos"}], [{"log": ""}], [{"log": "123"}]])
def test_resultados_ilegibles_no_se_interpretan_como_busqueda_vacia(lector, filas):
    lector, pagina = lector
    pagina.filas = filas
    with pytest.raises(ErrorDeWebSearch, match="leer sus bitácoras"):
        lector.consultar_numero("2000001", 12)


def test_renovacion_actualiza_el_endpoint_del_navegador(monkeypatch):
    sesion = SesionAirVault(AirVaultConfig())
    entradas = []
    monkeypatch.setattr(sesion, "renovar_sin_ventana", lambda *_a, **_k: entradas.append("entrar"))
    monkeypatch.setattr(modulo, "comprobar_o_renovar", lambda *_a, **_k: None)
    acceso = modulo.AccesoWebSearch(sesion.config, sesion)
    nueva = {"webSocketDebuggerUrl": "ws://nuevo"}
    acceso.version = {"webSocketDebuggerUrl": "ws://anterior"}
    acceso.navegador = SimpleNamespace(abrir=lambda *_a, **_k: nueva)
    transferidas = []
    monkeypatch.setattr(acceso, "_usar_cookies", lambda: transferidas.append(acceso.version))
    acceso.renovar(0)
    acceso.renovar(0)
    assert entradas == ["entrar"]
    assert transferidas == [nueva]
    assert acceso.generacion == 1
    sesion.http.close()


def test_login_fallido_no_se_repite_por_cada_batch(monkeypatch):
    sesion = SesionAirVault(AirVaultConfig())
    entradas = []

    def fallar(*_a, **_k):
        entradas.append("entrar")
        raise ErrorDeSesion("No se pudo entrar")

    monkeypatch.setattr(sesion, "renovar_sin_ventana", fallar)
    acceso = modulo.AccesoWebSearch(sesion.config, sesion)
    for _ in range(3):
        with pytest.raises(ErrorDeSesion, match="No se pudo entrar"):
            acceso.renovar(0)
    assert entradas == ["entrar"]
    # La vuelta siguiente del chequeo si puede intentarlo otra vez.
    acceso.reintentar_acceso()
    with pytest.raises(ErrorDeSesion, match="No se pudo entrar"):
        acceso.renovar(0)
    assert entradas == ["entrar", "entrar"]
    sesion.http.close()


def test_renovar_con_pestanas_leyendo_no_pide_ninguna_ventana(monkeypatch):
    """Edge esta ocupado: cambiarle la visibilidad era justo lo que fallaba."""
    sesion = SesionAirVault(AirVaultConfig())
    pedidos = []
    monkeypatch.setattr(sesion, "renovar_sin_ventana",
                        lambda _avisar, forzar=True: pedidos.append(("renovar", forzar)))
    monkeypatch.setattr(sesion, "renovar_en_navegador",
                        lambda *_a, **_k: pytest.fail("Abriria la ventana de acceso"))
    monkeypatch.setattr(modulo, "comprobar_o_renovar",
                        lambda *_a, **k: pedidos.append(("comprobar", k.get("permitir_ventana"))))
    acceso = modulo.AccesoWebSearch(sesion.config, sesion)
    acceso.renovar(0)
    # Primero se deja que el perfil la renueve solo; nunca con ventana.
    assert pedidos == [("renovar", False), ("comprobar", False)]
    sesion.http.close()
