"""El chequeo de publicacion usa Web Search y recupera el acceso caducado."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app.airvault import websearch_pagina as modulo
from app.airvault.config import AirVaultConfig
from app.airvault.confirmacion import verificar_batch
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
        if expresion == modulo._LEER_REJILLA:
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
    assert "__bits_busqueda_anterior" in pagina.expresiones[-3]
    assert "typeof window.__bits_busqueda_anterior === 'undefined'" in pagina.esperas[-1][0]
    lector.cerrar()
    assert pagina.cerrada


def test_websearch_vacio_no_confirma_la_publicacion(lector, tmp_path):
    lector, pagina = lector
    pagina.filas = []
    resultado = verificar_batch(lector, trabajo(tmp_path, "pendiente"))
    assert not resultado.confirmado
    assert resultado.encontradas == 0
    assert len(pagina.esperas) == 1


def test_login_se_propaga_para_renovarlo_en_vez_de_declarar_websearch_no_disponible(lector):
    lector, pagina = lector
    pagina.login = True
    with pytest.raises(ErrorDeSesion, match="iniciar sesión"):
        lector.consultar_numero("2000001", 12)
    assert modulo._LEER_REJILLA not in pagina.expresiones


def test_si_la_pagina_no_carga_no_lee_resultados_anteriores(lector):
    lector, pagina = lector
    pagina.lista = False
    with pytest.raises(TimeoutError):
        lector.consultar_numero("2000001", 12)
    assert modulo._LEER_REJILLA not in pagina.expresiones


def test_fallo_al_conectar_cierra_solo_la_pestana_propia(monkeypatch, lector):
    lector, _ = lector

    def fallar(*_a, **_k):
        raise RuntimeError("No abre")

    monkeypatch.setattr(modulo, "_Pagina", fallar)
    with pytest.raises(RuntimeError):
        lector.consultar_numero("2000001", 12)
    assert lector.acceso.navegador.cerradas == ["propia"]


def test_al_transferir_el_acceso_no_copia_cookies_de_otros_sitios(monkeypatch):
    sesion = SesionAirVault(AirVaultConfig())
    host = urlparse(sesion.config.base_url).hostname
    sesion.http.cookies.set("FedAuth", "prueba", domain=host, path="/")
    sesion.http.cookies.set("ajena", "prueba", domain="otro.test", path="/")
    pedidos = []
    monkeypatch.setattr(modulo, "_WebSocket", lambda *_: SimpleNamespace(
        pedir=lambda accion, **datos: pedidos.append((accion, datos)), cerrar=lambda: None))
    acceso = modulo.AccesoWebSearch(sesion.config, sesion)
    acceso.version = {"webSocketDebuggerUrl": "ws://local"}
    acceso._usar_cookies()
    assert pedidos[0][0] == "Storage.setCookies"
    assert [c["name"] for c in pedidos[0][1]["cookies"]] == ["FedAuth"]
    sesion.http.close()


def test_batched_parallel_access_expiry_logs_in_once_and_retries(app, tmp_path, monkeypatch):
    """Dos batches caducados se renuevan juntos y conservan la concurrencia."""
    trabajos = [trabajo(tmp_path, nombre) for nombre in ("uno", "dos")]
    sesion = SesionAirVault(trabajos[0].config)
    sesion.usar_cookie("FedAuth=prueba")
    consultas, entradas, cerrados = [], [], []
    barrera = Barrier(2)
    monkeypatch.setattr(modulo, "comprobar_o_renovar", lambda *_a, **_k: entradas.append("comprobar"))
    monkeypatch.setattr(SesionAirVault, "renovar_en_navegador", lambda *_a: entradas.append("renovar"))
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
