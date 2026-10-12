"""La sesión tomada del navegador que abre el propio programa.

No se lanza Edge en ninguna prueba: se ejerce el cliente del protocolo
contra un servidor de mentira en la misma máquina, y el recorrido completo
contra un navegador falso. Lo que no se puede probar aquí (que Microsoft
devuelva la sesión) es justamente lo que hace una persona.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from pathlib import Path

import pytest

from app.airvault import navegador
from app.airvault.navegador import (
    ErrorDeNavegador,
    ServidorSinRespuesta,
    SondaIndeterminada,
    _del_dominio,
    _WebSocket,
    cookies_intrusas,
    obtener_cookies,
    ruta_de_edge,
)

class _Respuesta:
    """Lo justo de una respuesta HTTP para que ``json.load`` la lea."""

    def __init__(self, cuerpo: str):
        self._cuerpo = cuerpo.encode()

    def read(self, *_a):
        return self._cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *_e):
        return False


HOST = "airvault.criticaltech.com"
FEDAUTH = "77u/PD94bWwgdmVyc2lvbj0iMS4wIiA/Pg=="


# ── servidor de mentira que habla el protocolo ─────────────────────

class ServidorWS:
    """Acepta una conexión, hace el saludo y contesta lo que se le diga."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.recibidos: list = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.puerto = self.sock.getsockname()[1]
        self.hilo = threading.Thread(target=self._atender, daemon=True)
        self.hilo.start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.puerto}/devtools/browser/abc"

    def cerrar(self) -> None:
        """Suelta la escucha.

        Sin esto el descriptor sobrevive a la prueba y el aviso de socket sin
        cerrar sale mucho despues, cargado a la prueba que lo recoja.
        """
        self.sock.close()
        self.hilo.join(timeout=1)

    def _atender(self) -> None:
        try:
            cliente, _ = self.sock.accept()
        except OSError:
            # La prueba termino antes de que nadie se conectara.
            return
        try:
            peticion = b""
            while b"\r\n\r\n" not in peticion:
                peticion += cliente.recv(4096)
            clave = ""
            for linea in peticion.decode().split("\r\n"):
                if linea.lower().startswith("sec-websocket-key:"):
                    clave = linea.split(":", 1)[1].strip()
            acepta = base64.b64encode(hashlib.sha1(
                (clave + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            cliente.sendall(
                f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {acepta}\r\n\r\n"
                .encode()
            )
            for respuesta in self.respuestas:
                self.recibidos.append(json.loads(self._leer(cliente)))
                self._escribir(cliente, json.dumps(respuesta))
        except OSError:
            pass
        finally:
            cliente.close()

    @staticmethod
    def _leer(cliente) -> str:
        cabecera = cliente.recv(2)
        largo = cabecera[1] & 0x7F
        if largo == 126:
            largo = struct.unpack("!H", cliente.recv(2))[0]
        elif largo == 127:
            largo = struct.unpack("!Q", cliente.recv(8))[0]
        mascara = cliente.recv(4)
        datos = b""
        while len(datos) < largo:
            datos += cliente.recv(largo - len(datos))
        return bytes(
            b ^ mascara[i % 4] for i, b in enumerate(datos)
        ).decode()

    @staticmethod
    def _escribir(cliente, texto: str) -> None:
        carga = texto.encode()
        if len(carga) < 126:
            cabecera = struct.pack("!BB", 0x81, len(carga))
        else:
            cabecera = struct.pack("!BBH", 0x81, 126, len(carga))
        cliente.sendall(cabecera + carga)


def test_el_cliente_del_protocolo_va_y_vuelve():
    servidor = ServidorWS([{"id": 1, "result": {"cookies": []}}])
    ws = _WebSocket(servidor.url)
    try:
        assert ws.pedir("Storage.getCookies") == {"cookies": []}
    finally:
        ws.cerrar()
        servidor.cerrar()
    assert servidor.recibidos[0]["method"] == "Storage.getCookies"


def test_una_respuesta_larga_no_se_parte():
    """Las cookies de federacion pasan de 126 bytes y cambian el encuadre."""
    galletas = [{"name": f"FedAuth{n}", "value": "x" * 300, "domain": HOST}
                for n in range(4)]
    servidor = ServidorWS([{"id": 1, "result": {"cookies": galletas}}])
    ws = _WebSocket(servidor.url)
    try:
        recibidas = ws.pedir("Storage.getCookies")["cookies"]
    finally:
        ws.cerrar()
        servidor.cerrar()
    assert len(recibidas) == 4
    assert all(len(c["value"]) == 300 for c in recibidas)


def test_un_aviso_del_navegador_no_confunde_la_respuesta():
    """Edge intercala eventos; hay que quedarse con la respuesta propia."""
    servidor = ServidorWS([{"id": 1, "result": {"cookies": [{"name": "a"}]}}])
    ws = _WebSocket(servidor.url)
    try:
        assert ws.pedir("Storage.getCookies")["cookies"][0]["name"] == "a"
    finally:
        ws.cerrar()
        servidor.cerrar()


def test_el_error_del_navegador_se_cuenta():
    servidor = ServidorWS([
        {"id": 1, "error": {"message": "'X' wasn't found"}},
    ])
    ws = _WebSocket(servidor.url)
    try:
        with pytest.raises(ErrorDeNavegador) as fallo:
            ws.pedir("X")
    finally:
        ws.cerrar()
        servidor.cerrar()
    assert "wasn't found" in str(fallo.value)


# ── ubicación de Edge ──────────────────────────────────────────────

def test_sin_edge_se_dice_y_se_sigue_a_mano(tmp_path):
    with pytest.raises(ErrorDeNavegador) as fallo:
        ruta_de_edge((str(tmp_path / "no-esta.exe"),))
    assert "pegar la cookie" in str(fallo.value)


def test_se_toma_el_primer_edge_que_exista(tmp_path):
    segundo = tmp_path / "msedge.exe"
    segundo.write_bytes(b"")
    assert ruta_de_edge(
        (str(tmp_path / "no-esta.exe"), str(segundo))
    ) == segundo


# ── filtrado por dominio ───────────────────────────────────────────

def test_solo_viajan_las_cookies_del_dominio():
    por_dominio = {
        HOST: [{"name": "FedAuth", "value": FEDAUTH}],
        ".criticaltech.com": [{"name": "consent", "value": "1"}],
        ".login.microsoftonline.com": [{"name": "ESTSAUTH", "value": "z"}],
    }
    elegidas = _del_dominio(por_dominio, HOST)
    assert set(elegidas) == {"FedAuth", "consent"}


def _del_sitio(nombre, valor):
    """La cookie como la pone AirVault: de dominio y segura."""
    return {"name": nombre, "value": valor, "domain": ".criticaltech.com",
            "path": "/", "secure": True, "httpOnly": True,
            "sourceScheme": "Secure"}


def _copia(nombre, valor):
    """La que escribia el programa al pasar la sesion al navegador."""
    return {"name": nombre, "value": valor, "domain": HOST, "path": "/",
            "secure": False, "httpOnly": False, "sourceScheme": "NonSecure"}


def _agrupar(cookies):
    por_dominio: dict = {}
    for cookie in cookies:
        por_dominio.setdefault(cookie["domain"], []).append(cookie)
    return por_dominio


@pytest.mark.parametrize("orden", [0, 1])
def test_entre_dos_cookies_del_mismo_nombre_gana_la_del_sitio(orden):
    """Tomar la copia dejaba la conexion probando una sesion vieja."""
    cookies = [_copia("ProdSSO", "vieja"), _del_sitio("ProdSSO", "nueva")]
    por_dominio = _agrupar(cookies[::-1] if orden else cookies)
    assert _del_dominio(por_dominio, HOST) == {"ProdSSO": "nueva"}


def test_son_intrusas_las_copias_sin_marca_de_seguras_venidas_de_http():
    """Un sitio que solo se visita por https no pudo poner esas cookies."""
    por_dominio = _agrupar([
        _del_sitio("ProdSSO", "nueva"), _copia("ProdSSO", "vieja"),
        _copia("Critical", "vieja"),
        {"name": "ESTSAUTH", "value": "z", "secure": False,
         "domain": ".login.microsoftonline.com", "sourceScheme": "NonSecure"},
    ])
    intrusas = cookies_intrusas(por_dominio, f"https://{HOST}")
    assert sorted((c["name"], c["domain"]) for c in intrusas) == [
        ("Critical", HOST), ("ProdSSO", HOST),
    ]
    # Sin https no hay como distinguirlas, y sin el origen tampoco.
    assert cookies_intrusas(por_dominio, f"http://{HOST}") == []
    assert cookies_intrusas(
        {HOST: [{"name": "FedAuth", "value": "x"}]}, f"https://{HOST}"
    ) == []


# ── recorrido completo, con un navegador falso ─────────────────────

class NavegadorFalso:
    """Sustituye a Edge: entrega las cookies que se le digan."""

    abiertos: list = []

    def __init__(self, guion):
        self.guion = list(guion)
        self.visible = None
        self.urls: list = []
        self.borradas: list = []
        self.visitas: list = []

    def __call__(self, perfil, edge=None, visible=True):
        self.visible = visible
        NavegadorFalso.abiertos.append(visible)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_e):
        return None

    def abrir(self, url, espera_s=30.0):
        self.urls.append(url)
        return {"webSocketDebuggerUrl": "ws://x/y"}

    def cookies(self, _version):
        actual = self.guion[0] if len(self.guion) == 1 else self.guion.pop(0)
        if isinstance(actual, list):
            # Cookies enteras, con su dominio y sus marcas.
            return _agrupar(actual)
        return {HOST: [{"name": n, "value": v} for n, v in actual.items()]}

    def borrar_cookies(self, _version, cookies):
        self.borradas.append(sorted((c["name"], c["domain"]) for c in cookies))
        return len(cookies)

    def visitar(self, url, _version=None):
        self.visitas.append(url)
        return True


def preparar(monkeypatch, guion):
    NavegadorFalso.abiertos = []
    falso = NavegadorFalso(guion)
    monkeypatch.setattr(navegador, "SesionDeNavegador", falso)
    return falso


def test_con_la_sesion_viva_nadie_ve_ninguna_ventana(monkeypatch, tmp_path):
    """Es el caso normal a partir de la segunda vez."""
    falso = preparar(monkeypatch, [{"FedAuth": FEDAUTH}])
    cookies = obtener_cookies(
        f"https://{HOST}", tmp_path, dormir=lambda _s: None
    )
    assert cookies == {"FedAuth": FEDAUTH}
    assert NavegadorFalso.abiertos == [False]
    assert falso.visible is False


def test_sin_sesion_se_abre_la_ventana_para_entrar(monkeypatch, tmp_path):
    guion = [{}, {}, {"FedAuth": FEDAUTH}]
    preparar(monkeypatch, guion)
    avisos: list = []
    cookies = obtener_cookies(
        f"https://{HOST}", tmp_path, avisar=avisos.append,
        dormir=lambda _s: None, espera_perfil_s=0,
    )
    assert cookies == {"FedAuth": FEDAUTH}
    # Primero sin ventana; al no haber sesion, con ventana.
    assert NavegadorFalso.abiertos == [False, True]
    assert avisos and "entre a AirVault" in avisos[0]


def test_el_numero_de_sesion_solo_no_se_da_por_bueno(monkeypatch, tmp_path):
    """Sin FedAuth la sesion no sirve, aunque AirVault ya haya contestado."""
    guion = [{"ASP.NET_SessionId": "abc"}, {"ASP.NET_SessionId": "abc",
                                            "FedAuth": FEDAUTH}]
    preparar(monkeypatch, guion)
    cookies = obtener_cookies(
        f"https://{HOST}", tmp_path, dormir=lambda _s: None,
        espera_perfil_s=0,
    )
    assert "FedAuth" in cookies
    assert NavegadorFalso.abiertos == [False, True]


def test_si_nadie_entra_se_dice_y_no_se_espera_para_siempre(monkeypatch,
                                                            tmp_path):
    preparar(monkeypatch, [{}])
    # Los dos primeros son el intento sin ventana, que no encuentra nada.
    reloj = iter([0.0, 0.0, 0.0, 10.0, 400.0, 500.0, 600.0])
    with pytest.raises(ErrorDeNavegador) as fallo:
        obtener_cookies(
            f"https://{HOST}", tmp_path, espera_login_s=300.0,
            dormir=lambda _s: None, reloj=lambda: next(reloj),
            espera_perfil_s=0,
        )
    assert "pegar la cookie" in str(fallo.value)


def test_se_entra_por_el_enlace_federado(monkeypatch, tmp_path):
    """Por la raiz no se dispara la redireccion y no llega la cookie."""
    falso = preparar(monkeypatch, [{"FedAuth": FEDAUTH}])
    sso = f"https://{HOST}/zfp/?whr=https://login.microsoftonline.com/x/wsfed"
    obtener_cookies(f"https://{HOST}", tmp_path, sso, dormir=lambda _s: None)
    assert falso.urls == [sso]


def test_forzar_login_no_se_fia_de_lo_que_guarda_el_perfil(monkeypatch,
                                                           tmp_path):
    """Cuando el servidor ya rechazo lo del perfil, releerlo da lo mismo.

    Es la salida del callejon: la cookie guardada tiene la forma correcta,
    asi que releerla la daria por buena una y otra vez. Se borra del perfil
    y se entra de nuevo, sin ventana: el acceso de Microsoft sigue ahi.
    """
    falso = preparar(monkeypatch, [{"FedAuth": "rechazada"},
                                   {"FedAuth": FEDAUTH}])
    sso = f"https://{HOST}/zfp/?whr=https://login.microsoftonline.com/x/wsfed"
    cookies = obtener_cookies(
        f"https://{HOST}", tmp_path, sso, dormir=lambda _s: None,
        forzar_login=True,
    )
    assert cookies == {"FedAuth": FEDAUTH}
    assert falso.borradas == [[("FedAuth", HOST)]]
    assert falso.visitas == [sso]
    assert NavegadorFalso.abiertos == [False]


def test_la_espera_agotada_dice_que_llego_y_que_falto(monkeypatch, tmp_path):
    """Sin ventana no se ve nada; el motivo tiene que decirlo todo."""
    preparar(monkeypatch, [{"ASP.NET_SessionId": "abc"}])
    # Los dos primeros son del intento sin ventana.
    relojes = iter([0.0, 0.0, 0.0, 0.0, 1.0, 999.0])
    with pytest.raises(ErrorDeNavegador) as fallo:
        obtener_cookies(
            f"https://{HOST}", tmp_path, espera_login_s=120.0,
            dormir=lambda _s: None, reloj=lambda: next(relojes),
            espera_perfil_s=0,
        )
    motivo = str(fallo.value)
    assert "ASP.NET_SessionId" in motivo
    assert "pagina de Microsoft" in motivo


def test_no_se_toma_la_primera_cookie_que_aparece_sino_la_que_sirve(
        monkeypatch, tmp_path):
    """Recien abierto, el navegador todavía va y viene de Microsoft.

    Lo que hay en el perfil en ese instante es la cookie de la vez
    anterior, caducada si paso el rato, y tomarla dejaba el trabajo
    muriendo en la primera peticion con la sesion buena a un segundo de
    distancia. Se espera a una que el servidor acepte, que es ademas lo
    que la renueva sin que nadie teclee nada.
    """
    preparar(monkeypatch, [{"FedAuth": "vieja"}, {"FedAuth": "vieja"},
                           {"FedAuth": FEDAUTH}])
    cookies = obtener_cookies(
        f"https://{HOST}", tmp_path, dormir=lambda _s: None,
        confirmar=lambda c: c.get("FedAuth") == FEDAUTH,
    )
    assert cookies == {"FedAuth": FEDAUTH}
    # Sin ventana: la sesion estaba, solo habia que dejarla llegar.
    assert NavegadorFalso.abiertos == [False]


def test_si_la_del_perfil_no_sirve_se_entra_de_nuevo_sin_ventana(
        monkeypatch, tmp_path):
    """Una cookie con la forma correcta pero caducada no es una sesion.

    Antes de molestar a nadie se quita la del perfil y se deja que el
    navegador rehaga el acceso: Edge se queda en segundo plano.
    """
    falso = preparar(monkeypatch, [{"FedAuth": "caducada"},
                                   {"FedAuth": FEDAUTH}])
    cookies = obtener_cookies(
        f"https://{HOST}", tmp_path, dormir=lambda _s: None,
        espera_perfil_s=0,
        confirmar=lambda c: c.get("FedAuth") == FEDAUTH,
    )
    assert cookies == {"FedAuth": FEDAUTH}
    assert falso.borradas == [[("FedAuth", HOST)]]
    assert NavegadorFalso.abiertos == [False]


def test_la_ventana_queda_para_cuando_ni_entrando_de_nuevo(monkeypatch,
                                                           tmp_path):
    """Si Microsoft pide a una persona, ahi si hace falta la ventana."""
    preparar(monkeypatch, [{"FedAuth": "caducada"}, {"FedAuth": "caducada"},
                           {"FedAuth": FEDAUTH}])
    cookies = obtener_cookies(
        f"https://{HOST}", tmp_path, dormir=lambda _s: None,
        espera_perfil_s=0,
        confirmar=lambda c: c.get("FedAuth") == FEDAUTH,
    )
    assert cookies == {"FedAuth": FEDAUTH}
    assert NavegadorFalso.abiertos == [False, True]


def test_renovar_a_mitad_del_trabajo_nunca_abre_la_ventana(monkeypatch,
                                                           tmp_path):
    """Edge esta ocupado y no hay nadie delante: se dice y se reintenta."""
    preparar(monkeypatch, [{"FedAuth": "caducada"}])
    with pytest.raises(ErrorDeNavegador, match="segundo plano"):
        obtener_cookies(
            f"https://{HOST}", tmp_path, dormir=lambda _s: None,
            espera_perfil_s=0, confirmar=lambda _c: False,
            permitir_ventana=False,
        )
    assert NavegadorFalso.abiertos == [False]


def test_sin_ventana_se_prueba_a_entrar_en_limpio_antes_de_rendirse(
        monkeypatch, tmp_path):
    """Aunque no quede cookie que autentique: no hay ventana que retrasar."""
    falso = preparar(monkeypatch, [{"ASP.NET_SessionId": "vieja"},
                                   {"ASP.NET_SessionId": "n", "FedAuth": FEDAUTH}])
    cookies = obtener_cookies(
        f"https://{HOST}", tmp_path, dormir=lambda _s: None,
        espera_perfil_s=0, permitir_ventana=False,
    )
    assert cookies["FedAuth"] == FEDAUTH
    assert falso.borradas == [[("ASP.NET_SessionId", HOST)]]
    assert NavegadorFalso.abiertos == [False]


def test_un_error_del_servidor_no_abre_la_ventana_de_acceso(monkeypatch,
                                                            tmp_path):
    """Un 500 no es una sesion caducada, y entrar otra vez no lo arregla.

    Tomarlo por «no valen» dejaba la ventana de acceso cinco minutos
    delante de un AirVault ya abierto.
    """
    falso = preparar(monkeypatch, [{"FedAuth": FEDAUTH}])

    def confirmar(_cookies):
        raise SondaIndeterminada("el servidor respondio 500")

    with pytest.raises(ServidorSinRespuesta):
        obtener_cookies(
            f"https://{HOST}", tmp_path, dormir=lambda _s: None,
            espera_perfil_s=0, confirmar=confirmar,
        )
    assert NavegadorFalso.abiertos == [False]
    # Por si era la sesion, antes de rendirse se entra de nuevo.
    assert falso.borradas == [[("FedAuth", HOST)]]


def test_las_copias_que_no_puso_el_sitio_se_quitan_antes_de_leer(monkeypatch,
                                                                 tmp_path):
    """La copia vieja tapa a la renovada y AirVault pide acceso sin parar.

    Paso de verdad: el chequeo de Web Search escribia en Edge la sesion de
    la conexion sin dominio ni marca de segura, Chromium la guardaba junto a
    la del sitio y, al renovarse esta, viajaban las dos.
    """
    entera = [_del_sitio("FedAuth", FEDAUTH), _del_sitio("ProdSSO", "nueva")]
    falso = preparar(monkeypatch, [
        entera + [_copia("ProdSSO", "vieja"), _copia("FedAuth", "vieja")],
        entera,
    ])
    probadas: list = []

    def confirmar(cookies):
        probadas.append(dict(cookies))
        return True

    cookies = obtener_cookies(
        f"https://{HOST}", tmp_path, dormir=lambda _s: None,
        confirmar=confirmar,
    )
    assert cookies == {"FedAuth": FEDAUTH, "ProdSSO": "nueva"}
    # Solo las copias: las del sitio y las de Microsoft no se tocan.
    assert falso.borradas == [[("FedAuth", HOST), ("ProdSSO", HOST)]]
    # Y se vuelve a entrar, porque el acceso en marcha viajo con ellas.
    assert falso.visitas == [f"https://{HOST}"]
    assert probadas == [{"FedAuth": FEDAUTH, "ProdSSO": "nueva"}]
    assert NavegadorFalso.abiertos == [False]


def test_un_aviso_que_falla_no_tumba_el_acceso(monkeypatch, tmp_path):
    """La sesion guarda el aviso de un trabajo que pudo haber terminado."""
    preparar(monkeypatch, [{}, {}, {"FedAuth": FEDAUTH}])

    def avisar(_texto):
        raise RuntimeError("Internal C++ object already deleted.")

    cookies = obtener_cookies(
        f"https://{HOST}", tmp_path, avisar=avisar,
        dormir=lambda _s: None, espera_perfil_s=0,
    )
    assert cookies == {"FedAuth": FEDAUTH}


def test_la_sesion_se_guarda_al_cerrar_el_navegador():
    """La cookie de federacion es de sesion y se pierde sin esta bandera.

    Sin ella hay que entrar con segundo factor en cada ejecución, que es lo
    que el perfil propio viene a evitar. Se fija aqui porque parece una
    preferencia de pestanas y no lo es.
    """
    assert "--restore-last-session" in navegador._ARGUMENTOS


def test_el_perfil_va_en_ruta_absoluta():
    """Edge descarta un ``--user-data-dir`` relativo y no arranca.

    No avisa ni escribe nada: el proceso termina y el programa lo leía como
    «Edge se cerró antes de abrir la sesión», así que mandaba a pegar la
    cookie a mano en una máquina donde el navegador funcionaba de sobra.
    """
    assert navegador.PERFIL_POR_DEFECTO.is_absolute()
    assert navegador.PERFIL_POR_DEFECTO.parts[-2:] == (
        "portable", "edge-airvault"
    )


def test_un_perfil_relativo_de_la_configuracion_se_vuelve_absoluto(monkeypatch):
    """La ruta también puede venir de ``airvault.json``, y valdría lo mismo."""
    monkeypatch.setattr(navegador, "ruta_de_edge", lambda: Path("msedge.exe"))
    sesion = navegador.SesionDeNavegador(Path("perfiles") / "propio")
    assert sesion.perfil.is_absolute()
    assert sesion.perfil.parts[-2:] == ("perfiles", "propio")
