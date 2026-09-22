"""El arranque de Edge: quien vive, quien muere y quien solo avisa.

Va en un archivo aparte de :mod:`tests.test_airvault_navegador` porque
``test_gui_shutdown`` arrastra una carrera propia de Qt que se despierta
cuando cambia el reparto de pruebas anteriores. Aqui no se lanza Edge: se
sustituye lo que el modulo usa para hablar con el sistema.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from app.airvault import navegador
from app.airvault.navegador import ErrorDeNavegador


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


class _LanzadorQueSeVa:
    """``msedge.exe`` tal como se comporta: entrega el encargo y se va."""

    def __init__(self, codigo: int):
        self.returncode = codigo

    def __call__(self, *_a, **_k):
        return self

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass


def _postizos(monkeypatch, lanzador, respuestas, reloj=None):
    """Sustituye ``subprocess``, ``urllib`` y ``time`` **dentro** del módulo.

    Se cambia el nombre en el espacio de ``navegador`` y no el atributo del
    módulo real: parchear ``time.sleep`` de verdad se lo cambia también a
    los hilos de Qt que dejaron vivos otras pruebas.
    """
    monkeypatch.setattr(navegador, "subprocess", types.SimpleNamespace(
        Popen=lanzador, DEVNULL=-3, TimeoutExpired=TimeoutError,
    ))

    def urlopen(_url, timeout=0):
        # Agotado el guion, el puerto deja de contestar: es lo que ve
        # ``cerrar`` cuando el navegador ya se fue.
        siguiente = next(respuestas, OSError("puerto cerrado"))
        if isinstance(siguiente, Exception):
            raise siguiente
        return _Respuesta(json.dumps(siguiente))

    monkeypatch.setattr(navegador, "urllib", types.SimpleNamespace(
        request=types.SimpleNamespace(urlopen=urlopen),
        error=types.SimpleNamespace(URLError=OSError),
    ))
    monkeypatch.setattr(navegador, "time", types.SimpleNamespace(
        monotonic=reloj or (lambda: 0.0), sleep=lambda _s: None,
    ))

    # El cierre pide ``Browser.close`` por el protocolo. Aqui no hay
    # navegador al otro lado, y dejarlo intentar la conexion de verdad se
    # come dos segundos esperando a un puerto que no existe.
    class _WSPostizo:
        pedidos: list = []

        def __init__(self, _url, timeout=15.0):
            pass

        def pedir(self, metodo, **_p):
            _WSPostizo.pedidos.append(metodo)
            return {}

        def cerrar(self):
            pass

    monkeypatch.setattr(navegador, "_WebSocket", _WSPostizo)
    return _WSPostizo


def test_que_el_lanzador_termine_no_es_que_edge_se_haya_caido(monkeypatch,
                                                              tmp_path):
    """``msedge.exe`` entrega el encargo a otro proceso y se va enseguida.

    Su código de salida (0, o 21 cuando avisó a una instancia que ya corría)
    no dice nada del navegador. Darlo por muerto convertía el arranque en
    una carrera: si el puerto tardaba más que el lanzador, el programa
    declaraba el fallo sobre un Edge que estaba abriendo.
    """
    reloj = iter([i * 0.5 for i in range(200)])
    ws = _postizos(
        monkeypatch, _LanzadorQueSeVa(21),
        iter([OSError("todavía no"), OSError("todavía no"),
              {"Browser": "Edg/151", "webSocketDebuggerUrl": "ws://x/y"}]),
        reloj=lambda: next(reloj),
    )
    sesion = navegador.SesionDeNavegador(tmp_path, edge=Path("msedge.exe"))
    try:
        assert sesion.abrir("about:blank")["Browser"] == "Edg/151"
    finally:
        sesion.cerrar()
    # Y al cerrar se le pide por las buenas, no se le mata.
    assert "Browser.close" in ws.pedidos


def test_al_abrir_deja_una_sola_pestana_y_no_toca_otros_objetivos(
        monkeypatch, tmp_path):
    """La restauracion conserva cookies, pero no debe acumular pestanas."""
    pedidos = []

    class _WSPostizo:
        def __init__(self, _url, timeout=15.0):
            pass

        def pedir(self, metodo, **params):
            pedidos.append((metodo, params))
            if metodo == "Target.createTarget":
                return {"targetId": "actual"}
            if metodo == "Target.getTargets":
                return {"targetInfos": [
                    {"type": "page", "targetId": "anterior-1"},
                    {"type": "service_worker", "targetId": "servicio"},
                    {"type": "page", "targetId": "actual"},
                    {"type": "page", "targetId": "anterior-2"},
                ]}
            return {}

        def cerrar(self):
            pass

    monkeypatch.setattr(navegador, "_WebSocket", _WSPostizo)
    sesion = navegador.SesionDeNavegador(
        tmp_path, edge=Path("msedge.exe"), visible=False
    )

    assert sesion._abrir_pagina(
        {"webSocketDebuggerUrl": "ws://127.0.0.1:4321/x"},
        "https://airvault/sso",
    )
    assert pedidos[:2] == [
        ("Target.createTarget", {"url": "https://airvault/sso"}),
        ("Target.getTargets", {}),
    ]
    assert [
        params["targetId"]
        for metodo, params in pedidos
        if metodo == "Target.closeTarget"
    ] == ["anterior-1", "anterior-2"]


def test_antes_de_cerrar_se_quitan_las_pestanas_que_se_restauraran(
        monkeypatch, tmp_path):
    """Lo que queda abierto al cerrar es lo que Edge vuelve a abrir.

    Es el momento en que estan todas: al abrir, la restauracion puede no
    haber terminado todavia. Se deja una, no ninguna, porque un navegador
    sin paginas se va por su cuenta y no llegaria a guardar el perfil.
    """
    pedidos = []

    class _WSPostizo:
        def __init__(self, _url, timeout=15.0):
            pass

        def pedir(self, metodo, **params):
            pedidos.append((metodo, params))
            if metodo == "Target.getTargets":
                return {"targetInfos": [
                    {"type": "page", "targetId": "restaurada-1"},
                    {"type": "service_worker", "targetId": "servicio"},
                    {"type": "page", "targetId": "restaurada-2"},
                    {"type": "page", "targetId": "de-trabajo"},
                ]}
            return {}

        def cerrar(self):
            pass

    monkeypatch.setattr(navegador, "_WebSocket", _WSPostizo)
    sesion = navegador.SesionDeNavegador(
        tmp_path, edge=Path("msedge.exe"), visible=False
    )

    sesion._pedir_que_se_cierre(
        {"webSocketDebuggerUrl": "ws://127.0.0.1:4321/x"}
    )

    assert [metodo for metodo, _ in pedidos] == [
        "Target.getTargets",
        "Target.closeTarget",
        "Target.closeTarget",
        "Browser.close",
    ]
    assert [
        params["targetId"]
        for metodo, params in pedidos
        if metodo == "Target.closeTarget"
    ] == ["restaurada-1", "restaurada-2"]


def test_si_nadie_contesta_tras_irse_el_lanzador_si_es_un_fallo(monkeypatch,
                                                               tmp_path):
    """Lo que sí vale: el lanzador se fue y el puerto sigue mudo un rato."""
    reloj = iter([i * 1.0 for i in range(200)])
    _postizos(
        monkeypatch, _LanzadorQueSeVa(0),
        iter(OSError("puerto mudo") for _ in range(200)),
        reloj=lambda: next(reloj),
    )
    sesion = navegador.SesionDeNavegador(tmp_path, edge=Path("msedge.exe"))
    try:
        with pytest.raises(ErrorDeNavegador) as fallo:
            sesion.abrir("about:blank", espera_s=120.0)
        # Y no se gasta la espera entera: se rinde al pasar la gracia. Se
        # mira aqui, antes de cerrar, porque el cierre tambien lee el reloj.
        assert next(reloj) < 120.0
    finally:
        sesion.cerrar()
    assert "no llego a arrancar" in str(fallo.value)


# ── el perfil que ya tiene un navegador ────────────────────────────
#
# Chromium admite un solo navegador por perfil: el segundo que se lanza le
# entrega la orden al primero y se va sin abrir su puerto. Un Edge que quedo
# vivo de una ejecucion anterior dejaba asi el acceso muerto para siempre,
# porque el puerto es otro cada vez y nadie sabia por donde escuchaba aquel.


def _perfil_con(monkeypatch, vivos, tmp_path, colgados=()):
    """Sustituye el sistema dejando vivos los puertos de ``vivos``.

    ``vivos`` es ``{puerto: version}`` y se modifica al pedir el cierre o al
    matar el proceso: es lo que hace que ``_esperar_a_que_se_vaya`` vea irse
    al navegador, como pasa de verdad. Los puertos de ``colgados`` contestan
    el ``/json/version`` pero no su protocolo, que es como termina un
    navegador olvidado durante horas.
    """
    lanzados = []
    matados = []

    class _Lanzador:
        def __init__(self, orden, **_k):
            lanzados.append(orden)
            # Con el perfil tomado, Chromium no abre nada: le entrega la
            # orden al que ya esta y se va con codigo 21.
            self.tomado = bool(vivos)
            self.returncode = 21 if self.tomado else 0
            if self.tomado:
                return
            # El navegador recien lanzado empieza a contestar por su puerto.
            for parte in orden:
                if parte.startswith("--remote-debugging-port="):
                    puerto = int(parte.split("=", 1)[1])
                    vivos[puerto] = {
                        "Browser": "Edg/151",
                        "webSocketDebuggerUrl": f"ws://127.0.0.1:{puerto}/x",
                    }

        def poll(self):
            return self.returncode if self.tomado else None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    def _correr(orden, **_k):
        """``powershell``, ``netstat`` y ``taskkill``: lo unico que se pide."""
        if orden[0] == "powershell":
            filas = [
                f"{7000 + puerto} \"msedge.exe\" "
                f"--remote-debugging-port={puerto} "
                f"--user-data-dir=\"{tmp_path}\" --no-first-run"
                for puerto in vivos
            ]
            return types.SimpleNamespace(stdout="\n".join(filas))
        if orden[0] == "netstat":
            filas = [
                f"  TCP    127.0.0.1:{puerto}   0.0.0.0:0   LISTENING   "
                f"{7000 + puerto}"
                for puerto in vivos
            ]
            return types.SimpleNamespace(stdout="\n".join(filas))
        pid = int(orden[orden.index("/PID") + 1])
        matados.append(pid)
        vivos.pop(pid - 7000, None)
        return types.SimpleNamespace(stdout="")

    monkeypatch.setattr(navegador, "subprocess", types.SimpleNamespace(
        Popen=_Lanzador, DEVNULL=-3, TimeoutExpired=TimeoutError, run=_correr,
    ))

    def urlopen(url, timeout=0):
        puerto = int(str(url).rsplit(":", 1)[1].split("/", 1)[0])
        if puerto not in vivos:
            raise OSError("puerto mudo")
        return _Respuesta(json.dumps(vivos[puerto]))

    monkeypatch.setattr(navegador, "urllib", types.SimpleNamespace(
        request=types.SimpleNamespace(urlopen=urlopen),
        error=types.SimpleNamespace(URLError=OSError),
    ))
    # El reloj avanza: hay esperas que solo terminan porque pasa el tiempo
    # (la del navegador que no se va), y con un reloj parado no volverian.
    tictac = iter(i * 0.5 for i in range(100000))
    monkeypatch.setattr(navegador, "time", types.SimpleNamespace(
        monotonic=lambda: next(tictac), sleep=lambda _s: None,
    ))

    class _WSPostizo:
        pedidos: list = []

        def __init__(self, url, timeout=15.0):
            self.puerto = int(str(url).rsplit(":", 1)[1].split("/", 1)[0])
            if self.puerto in colgados:
                raise OSError("timed out")

        def pedir(self, metodo, **_p):
            _WSPostizo.pedidos.append(metodo)
            if metodo == "Browser.close":
                vivos.pop(self.puerto, None)
            if metodo == "Target.createTarget":
                # Edge siempre contesta con el identificador de la pestana,
                # que es por donde se la conduce y se cierra despues.
                return {"targetId": "pestana-de-trabajo"}
            return {}

        def cerrar(self):
            pass

    _WSPostizo.pedidos = []
    monkeypatch.setattr(navegador, "_WebSocket", _WSPostizo)
    return lanzados, _WSPostizo, matados


def _anotacion(perfil):
    return Path(perfil) / navegador._ANOTACION_DEL_PUERTO


def test_la_gui_conserva_edge_y_cierra_todo_junto_al_salir(
        monkeypatch, tmp_path):
    """Ningun trabajo retira las paginas que otro hilo puede estar usando."""
    navegador.cerrar_navegadores_al_salir()
    vivos = {}
    lanzados, ws, _matados = _perfil_con(monkeypatch, vivos, tmp_path)
    navegador.mantener_navegadores_hasta_el_cierre()
    sesion = navegador.SesionDeNavegador(
        tmp_path, edge=Path("msedge.exe"), visible=False
    )

    try:
        sesion.abrir("https://airvault/sso")
        sesion.abrir_pestana("https://airvault/reporte")
        puerto = sesion.puerto
        sesion.cerrar()

        assert puerto in vivos
        assert "Browser.close" not in ws.pedidos
        assert "Target.closeTarget" not in ws.pedidos
        assert "--start-minimized" in lanzados[0]
        assert "--headless=new" not in lanzados[0]

        navegador.cerrar_navegadores_al_salir()

        assert puerto not in vivos
        assert ws.pedidos.count("Browser.close") == 1
        assert "Target.closeTarget" not in ws.pedidos
        assert not _anotacion(tmp_path).exists()
    finally:
        navegador.cerrar_navegadores_al_salir()


def test_se_habla_con_el_edge_que_ya_tenia_tomado_el_perfil(monkeypatch,
                                                            tmp_path):
    """Lanzar otro no serviria: le pasaria la orden y se iria sin abrir nada.

    Al navegador que quedo vivo se le piden sus cookies igual de bien —son
    las del perfil— y se le manda a la pagina de entrada, que es lo que
    rehace el acceso federado.
    """
    vivos = {4321: {"Browser": "Edg/151",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:4321/x"}}
    lanzados, ws, _matados = _perfil_con(monkeypatch, vivos, tmp_path)
    _anotacion(tmp_path).write_text("4321", encoding="ascii")

    sesion = navegador.SesionDeNavegador(
        tmp_path, edge=Path("msedge.exe"), visible=False
    )
    try:
        assert sesion.abrir("https://airvault/sso")["Browser"] == "Edg/151"
        assert lanzados == []
        assert sesion.puerto == 4321
        assert "Target.createTarget" in ws.pedidos
    finally:
        sesion.cerrar()
    # Y al cerrarlo se le pide por las buenas, aunque no sea proceso propio.
    assert "Browser.close" in ws.pedidos
    assert not _anotacion(tmp_path).exists()


def test_para_entrar_hace_falta_ventana_asi_que_al_otro_se_le_pide_paso(
        monkeypatch, tmp_path):
    """El que quedo vivo puede ser uno sin ventana, y ahi nadie teclea nada.

    Cuando lo que hace falta es que la persona entre, no se reusa: se le
    pide el cierre y se abre la ventana propia sobre el perfil libre.
    """
    vivos = {4321: {"Browser": "Edg/151",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:4321/x"}}
    lanzados, ws, _matados = _perfil_con(monkeypatch, vivos, tmp_path)
    _anotacion(tmp_path).write_text("4321", encoding="ascii")

    sesion = navegador.SesionDeNavegador(
        tmp_path, edge=Path("msedge.exe"), visible=True
    )
    try:
        assert sesion.abrir("https://airvault/sso")["Browser"] == "Edg/151"
        assert "Browser.close" in ws.pedidos
        assert len(lanzados) == 1
        assert sesion.puerto != 4321
    finally:
        sesion.cerrar()


def test_una_anotacion_vieja_no_manda_a_un_puerto_apagado(monkeypatch,
                                                          tmp_path):
    """El navegador se pudo ir sin borrarla (un cierre a la fuerza).

    Que el puerto anotado no conteste no es un fallo: se tira la anotacion y
    se abre un navegador nuevo, que deja la suya.
    """
    lanzados, _ws, _matados = _perfil_con(monkeypatch, {}, tmp_path)
    _anotacion(tmp_path).write_text("4321", encoding="ascii")

    sesion = navegador.SesionDeNavegador(
        tmp_path, edge=Path("msedge.exe"), visible=False
    )
    try:
        assert sesion.abrir("https://airvault/sso")["Browser"] == "Edg/151"
        assert len(lanzados) == 1
        assert _anotacion(tmp_path).read_text(encoding="ascii") == str(
            sesion.puerto
        )
    finally:
        sesion.cerrar()
    assert not _anotacion(tmp_path).exists()


def test_el_arranque_oculto_tambien_limpia_la_sesion_anterior(
        monkeypatch, tmp_path):
    """Un arranque nuevo limpia lo restaurado y conserva el modo oculto."""
    lanzados, ws, _matados = _perfil_con(monkeypatch, {}, tmp_path)
    sesion = navegador.SesionDeNavegador(
        tmp_path, edge=Path("msedge.exe"), visible=False
    )
    try:
        sesion.abrir("https://airvault/sso")
        assert "Target.createTarget" in ws.pedidos
        assert "--headless=new" in lanzados[0]
    finally:
        sesion.cerrar()


def test_el_puerto_se_anota_aunque_el_navegador_no_llegue_a_contestar(
        monkeypatch, tmp_path):
    """Es el caso que dejaba el perfil tomado para siempre.

    Si el puerto no llega a abrirse pero el navegador queda vivo, la
    anotacion es lo unico que le deja a la ejecucion siguiente para dar con
    el en vez de tropezar otra vez con el perfil tomado.
    """
    reloj = iter([i * 1.0 for i in range(200)])
    _postizos(
        monkeypatch, _LanzadorQueSeVa(21),
        iter(OSError("puerto mudo") for _ in range(200)),
        reloj=lambda: next(reloj),
    )
    sesion = navegador.SesionDeNavegador(tmp_path, edge=Path("msedge.exe"))
    try:
        with pytest.raises(ErrorDeNavegador) as fallo:
            sesion.abrir("about:blank", espera_s=120.0)
    finally:
        anotado = _anotacion(tmp_path)
        assert anotado.exists() and anotado.read_text() == str(sesion.puerto)
        sesion.cerrar()
    # Y el mensaje nombra la causa de verdad, no solo que nadie contesto.
    assert "tiene abierto el perfil" in str(fallo.value)


def test_al_que_se_colgo_se_le_cierra_a_la_fuerza(monkeypatch, tmp_path):
    """Un navegador olvidado durante horas contesta a medias.

    Su ``/json/version`` sigue respondiendo, asi que parece vivo, pero el
    protocolo por el que se le piden las cookies —y por el que se le pide el
    cierre— se queda esperando. Mientras ese proceso viva, ningun Edge nuevo
    abre el perfil: es lo que dejaba el acceso a AirVault muerto ejecucion
    tras ejecucion. No queda mas remedio que cerrarlo a la fuerza.
    """
    vivos = {4321: {"Browser": "Edg/151",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:4321/x"}}
    lanzados, _ws, matados = _perfil_con(
        monkeypatch, vivos, tmp_path, colgados={4321}
    )
    _anotacion(tmp_path).write_text("4321", encoding="ascii")

    sesion = navegador.SesionDeNavegador(
        tmp_path, edge=Path("msedge.exe"), visible=False
    )
    try:
        assert sesion.abrir("https://airvault/sso")["Browser"] == "Edg/151"
        # Se le cerro por el puerto, que es lo unico que lo distingue del
        # navegador de la persona, y despues se abrio uno propio.
        assert matados == [7000 + 4321]
        assert len(lanzados) == 1
        assert sesion.puerto != 4321
    finally:
        sesion.cerrar()


def test_se_encuentra_al_edge_que_no_dejo_anotado_su_puerto(monkeypatch,
                                                            tmp_path):
    """Uno de una version anterior, o uno que arranco sin llegar a anotarse.

    Sin anotacion que leer no hay por donde empezar, y el lanzamiento choca
    con el perfil tomado. Ahi se le pregunta a Windows que Edge hay abiertos
    sobre este perfil —lo unico que lo distingue del navegador de la
    persona— y se trabaja con el que aparezca. Antes no habia salida: el
    acceso moria igual ejecucion tras ejecucion.
    """
    vivos = {4321: {"Browser": "Edg/151",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:4321/x"}}
    lanzados, ws, matados = _perfil_con(monkeypatch, vivos, tmp_path)
    assert not _anotacion(tmp_path).exists()

    sesion = navegador.SesionDeNavegador(
        tmp_path, edge=Path("msedge.exe"), visible=False
    )
    try:
        assert sesion.abrir("https://airvault/sso")["Browser"] == "Edg/151"
        # Se intento lanzar, se choco con el perfil tomado y se acabo
        # hablando con el que ya estaba, sin cerrar nada.
        assert len(lanzados) == 1
        assert sesion.puerto == 4321
        assert matados == []
    finally:
        sesion.cerrar()


def test_al_cerrar_no_se_da_por_muerto_al_que_sigue_en_pie(monkeypatch,
                                                           tmp_path):
    """Pedirle el cierre no siempre basta, y ahi la anotacion mentia.

    Borrarla con el navegador todavia vivo dejaba a la ejecucion siguiente
    sin saber por donde buscarlo: se encontraba el perfil tomado y el acceso
    muerto otra vez. Ahora, si no se va cuando se le pide, se cierra a la
    fuerza; y solo cuando ya no esta se borra la anotacion.
    """
    vivos = {}
    lanzados, ws, matados = _perfil_con(monkeypatch, vivos, tmp_path)

    sesion = navegador.SesionDeNavegador(
        tmp_path, edge=Path("msedge.exe"), visible=False
    )
    sesion.abrir("https://airvault/sso")
    puerto = sesion.puerto
    assert _anotacion(tmp_path).read_text(encoding="ascii") == str(puerto)

    # Este no se va por las buenas: se queda contestando.
    monkeypatch.setattr(ws, "pedir", lambda *_a, **_k: {})
    sesion.cerrar()

    assert matados == [7000 + puerto]
    assert not _anotacion(tmp_path).exists()
