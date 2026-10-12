"""El chequeo de Web Search no se queda a medias por un tropiezo de AirVault.

Lo que paso: el servidor contesto con error justo al abrir Web Search y el
chequeo salio sin mirar un solo batch. La ventana se quedo contando hacia la
revision periodica, que mira la cola de Web Index y no esto, y los batches
que no estaban publicados siguieron sin subirse.
"""

from types import SimpleNamespace

import pytest

from app.airvault import websearch_pagina
from app.airvault.errores_websearch import MOTIVOS, ErrorDeWebSearch
from app.airvault.flujo import NO_ENCONTRADO, estado_local
from app.airvault.session import ErrorDeConexion, ErrorDeSesion, SesionAirVault
from app.gui import airvault_window as modulo
from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker
from tests.test_airvault_confirmacion import caso
from tests.test_airvault_revision_inicio import WorkerFalso
from tests.test_airvault_rework import trabajo


def chequear(trabajos, buscador, **estado):
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "buscar_trabajos": list(trabajos), "buscador": buscador,
        "revisar_todos": True, **estado,
    })
    avisos = []
    worker.buscado.connect(avisos.append)
    worker._buscar_websearch()
    return worker, avisos


# ── las vueltas del chequeo ────────────────────────────────────────

def test_un_batch_que_no_se_pudo_leer_se_consulta_de_nuevo_al_final(app, tmp_path):
    t, buscador, _, consultas = caso(tmp_path, 50)
    original = buscador.sesion.get
    cortes = [TimeoutError("Sin respuesta")]

    def get(*args):
        if cortes:
            raise cortes.pop()
        return original(*args)

    buscador.sesion.get = get
    _, avisos = chequear([t], buscador)
    assert t.manifiesto.websearch_resultado == "encontrado"
    assert len(consultas) == 7
    # La primera pasada no cuenta el batch: todavia no se sabe nada de el.
    assert avisos[0]["por_repetir"] and avisos[0]["revisados"] == 0
    assert "vuelve a consultar" in avisos[1]["aviso"]
    assert avisos[-2]["resultados"][0][1] is True
    assert avisos[-1]["terminado"] and avisos[-1]["errores"] == 0
    assert (avisos[-1]["revisados"], avisos[-1]["confirmados"]) == (1, 1)


def test_solo_queda_sin_verificar_el_que_falla_en_todas_las_vueltas(app, tmp_path):
    t, buscador, _, _ = caso(tmp_path, 50)
    intentos = []

    def fallar(*_):
        intentos.append(1)
        raise TimeoutError("Sin respuesta")

    buscador.sesion.get = fallar
    _, avisos = chequear([t], buscador)
    # Una consulta por vuelta: la primera y las dos de repeticion.
    assert len(intentos) == 3
    assert t.manifiesto.websearch_resultado == "error"
    assert [bool(a.get("por_repetir")) for a in avisos if a["resultados"]] == [True, True, False]
    assert avisos[-1]["errores"] == 1 and not avisos[-1]["ausentes"]
    assert avisos[-1]["revisados"] == 1


def test_una_ausencia_comprobada_no_se_vuelve_a_consultar(app, tmp_path):
    """Web Search contesto: repetir la pregunta no cambia la respuesta."""
    t, buscador, filas, consultas = caso(tmp_path, 50)
    filas.clear()
    _, avisos = chequear([t], buscador)
    assert len(consultas) == 7
    assert estado_local(t).estado == NO_ENCONTRADO
    assert [a for a in avisos if a.get("por_repetir") or a.get("aviso")] == []
    assert avisos[-1]["ausentes"][0].carpeta == t.carpeta


def test_sin_numeros_que_buscar_no_hay_vuelta_que_lo_arregle(app, tmp_path):
    t, buscador, _, consultas = caso(tmp_path, 1)
    t.manifiesto.registros[0].log_number = ""
    t.guardar()
    _, avisos = chequear([t], buscador)
    assert not consultas
    assert [a for a in avisos if a.get("por_repetir") or a.get("aviso")] == []
    assert avisos[-1]["errores"] == 1


def test_las_vueltas_solo_repiten_lo_que_fallo(app, tmp_path):
    import base64

    bueno, buscador, _, _ = caso(tmp_path / "a", 50)
    malo, _, _, _ = caso(tmp_path / "b", 50)
    for registro in malo.manifiesto.registros:
        registro.log_number = str(int(registro.log_number) + 500000)
    malo.guardar()
    original = buscador.sesion.get
    vistos = []

    # Solo falla lo del segundo batch: se reconoce por su numeracion.
    def get(ruta, params):
        numero = base64.b64decode(params["encodedValues"]).decode().split("=", 1)[1]
        vistos.append(numero)
        if numero.startswith("25"):
            raise TimeoutError("Sin respuesta")
        return original(ruta, params)

    buscador.sesion.get = get
    _, avisos = chequear([bueno, malo], buscador)
    del_bueno = [n for n in vistos if n.startswith("20")]
    del_malo = [n for n in vistos if n.startswith("25")]
    assert len(del_bueno) == 7, "El confirmado no se vuelve a consultar"
    assert len(del_malo) == 3
    assert bueno.manifiesto.websearch_resultado == "encontrado"
    assert malo.manifiesto.websearch_resultado == "error"
    assert avisos[-1]["revisados"] == 2 and avisos[-1]["confirmados"] == 1


# ── el acceso a Web Search ─────────────────────────────────────────

@pytest.fixture
def acceso_falso(monkeypatch):
    """Web Search sin Edge: cada lector contesta lo que diga ``filas``."""
    entradas, filas = [], {}
    fallos = []

    def entrar(acceso):
        entradas.append(acceso.permitir_ventana)
        if fallos:
            raise fallos.pop(0)
        return acceso

    class Lector:
        def __init__(self, acceso):
            self.acceso, self.generacion = acceso, acceso.generacion

        def consultar_numero(self, numero, _presupuesto):
            return {"rows": [{"LogNo": numero}] if filas.get("todas", True) else []}

        def cerrar(self):
            return None

    monkeypatch.setattr(websearch_pagina.AccesoWebSearch, "__enter__", entrar)
    monkeypatch.setattr(websearch_pagina.AccesoWebSearch, "__exit__", lambda *_: None)
    monkeypatch.setattr(websearch_pagina.AccesoWebSearch, "crear_lector", lambda acceso: Lector(acceso))
    return SimpleNamespace(entradas=entradas, fallos=fallos, filas=filas)


def _con_sesion(tmp_path, **estado):
    t = trabajo(tmp_path, "uno")
    sesion = SesionAirVault(t.config)
    sesion.usar_cookie("FedAuth=prueba")
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "config": t.config, "raiz": tmp_path, "sesion_base": sesion,
        "buscar_trabajos": [t], "revisar_todos": True, **estado,
    })
    avisos = []
    worker.buscado.connect(avisos.append)
    return t, sesion, worker, avisos


def test_un_error_del_servidor_al_abrir_no_aborta_el_chequeo(app, tmp_path, acceso_falso):
    """Es lo que paso: 500 en la portada de Web Search y ningun batch mirado."""
    t, sesion, worker, avisos = _con_sesion(tmp_path)
    acceso_falso.fallos[:] = [ErrorDeConexion("No se pudo completar /zfp/ tras 3 intentos"),
                              ErrorDeSesion("AirVault volvio a pedir acceso")]
    worker._buscar_websearch()
    assert len(acceso_falso.entradas) == 3
    assert t.manifiesto.websearch_resultado == "encontrado"
    esperas = [a["aviso"] for a in avisos if a.get("aviso")]
    assert len(esperas) == 2
    assert "AirVault no responde" in esperas[0] and "2 de 3" in esperas[0]
    assert MOTIVOS["acceso"] in esperas[1] and "3 de 3" in esperas[1]
    sesion.http.close()


def test_si_web_search_no_abre_en_ningun_intento_se_dice_por_que(app, tmp_path, acceso_falso):
    t, sesion, worker, _ = _con_sesion(tmp_path)
    acceso_falso.fallos[:] = [ErrorDeConexion(f"corte {n}") for n in range(5)]
    with pytest.raises(ErrorDeConexion, match="corte 2"):
        worker._buscar_websearch()
    assert len(acceso_falso.entradas) == 3
    assert t.manifiesto.websearch_resultado == ""
    assert worker._lecturas_websearch == []
    sesion.http.close()


def test_un_fallo_que_no_es_de_acceso_no_se_reintenta(app, tmp_path, acceso_falso):
    """Insistir cuatro minutos contra un error del programa solo lo esconde."""
    _, sesion, worker, _ = _con_sesion(tmp_path)
    acceso_falso.fallos[:] = [ValueError("Configuracion incompleta")]
    with pytest.raises(ValueError):
        worker._buscar_websearch()
    assert len(acceso_falso.entradas) == 1
    sesion.http.close()


@pytest.mark.parametrize("estado,ventana", [
    ({}, True),
    ({"confirmacion_automatica": True}, False),
    ({"sin_ventana": True}, False),
])
def test_solo_el_chequeo_pedido_por_alguien_puede_abrir_la_ventana(
        app, tmp_path, acceso_falso, estado, ventana):
    """Lo que arranca un reloj corre con Edge en segundo plano."""
    _, sesion, worker, _ = _con_sesion(tmp_path, **estado)
    worker._buscar_websearch()
    assert acceso_falso.entradas == [ventana]
    sesion.http.close()


def test_un_batch_que_no_carga_se_repite_y_el_acceso_se_puede_renovar(
        app, tmp_path, acceso_falso, monkeypatch):
    t, sesion, worker, avisos = _con_sesion(tmp_path)
    pendientes = [ErrorDeWebSearch("carga")]
    reinicios = []

    class Lector:
        def __init__(self, acceso):
            self.acceso, self.generacion = acceso, acceso.generacion

        def consultar_numero(self, numero, _presupuesto):
            if pendientes:
                raise pendientes.pop()
            return {"rows": [{"LogNo": numero}]}

        def cerrar(self):
            return None

    monkeypatch.setattr(websearch_pagina.AccesoWebSearch, "crear_lector", lambda acceso: Lector(acceso))
    monkeypatch.setattr(websearch_pagina.AccesoWebSearch, "reintentar_acceso",
                        lambda _acceso: reinicios.append(1))
    worker._buscar_websearch()
    assert t.manifiesto.websearch_resultado == "encontrado"
    assert reinicios == [1]
    assert avisos[-1]["errores"] == 0
    sesion.http.close()


# ── la ventana ─────────────────────────────────────────────────────

@pytest.fixture
def ventana(app, tmp_path, monkeypatch):
    monkeypatch.setattr(modulo, "TrabajoAirVaultWorker", WorkerFalso)
    return AirVaultWindow(tmp_path)


def _en_la_ventana(ventana, tmp_path, cuantos=2):
    trabajos = [caso(tmp_path / str(n), 50)[0] for n in range(cuantos)]
    ventana._trabajos = trabajos
    ventana._estados = [estado_local(t) for t in trabajos]
    ventana._pintar_lotes()
    return trabajos


def _terminar(ventana):
    ventana._worker_websearch.corriendo = False
    ventana._al_terminar_websearch()


def _bitacora(ventana):
    return "\n".join(ventana.bitacora.item(i).text() for i in range(ventana.bitacora.count()))


def test_un_chequeo_que_no_pudo_empezar_se_repite_solo(ventana, tmp_path, monkeypatch):
    trabajos = _en_la_ventana(ventana, tmp_path)
    monkeypatch.setattr(ventana, "_encolar", lambda *_: pytest.fail("No hay nada revisado todavia"))
    ventana._revisar_a_mano()
    primero = ventana._worker_websearch
    assert not primero.estado["sin_ventana"]
    ventana._al_fallar_websearch("AirVault no responde. Revise la conexión y vuelva a intentarlo.")
    _terminar(ventana)

    assert ventana._reintentando_chequeo()
    assert ventana._reintento_chequeo.interval() == modulo.MINUTOS_ENTRE_CHEQUEOS[0] * 60_000
    # Lo que venia detras del chequeo sigue en pie para el nuevo intento.
    assert ventana._revision_airvault_pendiente
    assert "el chequeo se repite solo en 2 min (intento 2 de 5)" in _bitacora(ventana)
    viva = ventana.bitacora.item(ventana.bitacora.count() - 1)
    assert viva.text().startswith("Web Search no se pudo consultar: el chequeo se repite en")
    assert ventana.boton_cancelar.isEnabled()

    ventana._reintento_chequeo.stop()
    ventana._repetir_chequeo()
    segundo = ventana._worker_websearch
    assert segundo is not primero
    assert segundo.estado["buscar_trabajos"] == trabajos
    # Es la misma orden, pero ya no hay nadie delante.
    assert segundo.estado["sin_ventana"] and not segundo.estado["confirmacion_automatica"]


def test_la_cuenta_del_chequeo_va_por_delante_de_la_revision_periodica(ventana, tmp_path):
    """Era la linea que confundia: «proxima revision» con el chequeo caido."""
    from PySide6.QtCore import QTimer

    _en_la_ventana(ventana, tmp_path)
    ventana._vigilante = QTimer(ventana)
    ventana._vigilante.start(300_000)
    ventana._buscar_websearch()
    ventana._al_fallar_websearch("AirVault no responde")
    _terminar(ventana)
    viva = ventana.bitacora.item(ventana.bitacora.count() - 1).text()
    assert viva.startswith("Web Search no se pudo consultar")
    assert "próxima revisión" not in viva
    ventana._parar_reintento_del_chequeo()
    ventana._pintar_linea_viva()
    viva = ventana.bitacora.item(ventana.bitacora.count() - 1).text()
    assert viva.startswith("Depende de AirVault: próxima revisión en")


def test_gastados_los_intentos_el_chequeo_queda_pendiente_y_se_dice(ventana, tmp_path):
    _en_la_ventana(ventana, tmp_path)
    ventana._revisar_a_mano()
    for _ in modulo.MINUTOS_ENTRE_CHEQUEOS:
        ventana._al_fallar_websearch("AirVault no responde")
        _terminar(ventana)
        assert ventana._reintentando_chequeo()
        ventana._reintento_chequeo.stop()
        ventana._repetir_chequeo()
    ventana._al_fallar_websearch("AirVault no responde")
    _terminar(ventana)
    assert not ventana._reintentando_chequeo()
    assert not ventana._revision_airvault_pendiente
    assert "el chequeo quedó pendiente tras varios intentos" in _bitacora(ventana)
    assert ventana._linea_viva is None


def test_una_orden_nueva_devuelve_todos_los_intentos(ventana, tmp_path):
    _en_la_ventana(ventana, tmp_path)
    ventana._buscar_websearch()
    ventana._al_fallar_websearch("AirVault no responde")
    _terminar(ventana)
    assert ventana._intentos_de_chequeo == 1
    ventana._buscar_websearch()
    assert ventana._intentos_de_chequeo == 0
    assert not ventana._reintentando_chequeo()
    assert len(ventana._worker_websearch.estado["buscar_trabajos"]) == 2


def test_lo_ausente_se_recupera_ya_y_lo_que_no_se_leyo_se_repite(ventana, tmp_path, monkeypatch):
    ausente, sin_leer = _en_la_ventana(ventana, tmp_path)
    encoladas = []
    monkeypatch.setattr(ventana, "_encolar", lambda modo, lista, texto: encoladas.append((modo, lista, texto)))
    ventana._buscar_websearch()
    ausente.manifiesto.websearch_resultado = "ausente"
    sin_leer.manifiesto.websearch_resultado = "error"
    sin_leer.manifiesto.websearch_detalle = MOTIVOS["carga"]
    ventana._al_buscar_websearch({"resultados": [(sin_leer, False)], "por_repetir": True,
                                  "revisados": 0, "total": 2})
    ventana._al_buscar_websearch({"resultados": [(ausente, False)], "revisados": 1, "total": 2})
    ventana._al_buscar_websearch({"resultados": [], "terminado": True, "ausentes": [ausente],
                                  "errores": 1, "revisados": 2, "confirmados": 0, "total": 2})
    _terminar(ventana)

    assert [(modo, lista) for modo, lista, _ in encoladas] == [("recuperar_ausentes", [ausente])]
    assert "1 batch no está en Web Search; se vuelve a subir" in encoladas[0][2]
    assert list(ventana._chequeo_por_repetir.values()) == [sin_leer]
    assert ventana._reintentando_chequeo()
    texto = _bitacora(ventana)
    assert "Web Search - Batch «DP | ENTREGA»: no se pudo leer; se consulta de nuevo al final" in texto
    assert "Web Search 1 de 2 - Batch «DP | ENTREGA»: no está publicado; queda pendiente de subir" in texto
    assert "Web Search: chequeo terminado, 2 de 2 batches revisados" in texto
    assert "0 confirmados, 1 sin publicar y 1 sin verificar" in texto


def test_un_chequeo_limpio_no_deja_nada_esperando(ventana, tmp_path, monkeypatch):
    confirmado, _ = _en_la_ventana(ventana, tmp_path)
    monkeypatch.setattr(ventana, "_encolar", lambda *_: None)
    ventana._intentos_de_chequeo = 3
    ventana._buscar_websearch()
    for t in ventana._trabajos:
        t.manifiesto.websearch_resultado = "encontrado"
    ventana._al_buscar_websearch({"resultados": [(confirmado, True)], "revisados": 1, "total": 2})
    ventana._al_buscar_websearch({"resultados": [], "terminado": True, "ausentes": [], "errores": 0,
                                  "revisados": 2, "confirmados": 2, "total": 2})
    _terminar(ventana)
    assert not ventana._reintentando_chequeo()
    assert ventana._intentos_de_chequeo == 0
    assert "Web Search 1 de 2 - Batch «DP | ENTREGA»: confirmado" in _bitacora(ventana)


@pytest.mark.parametrize("accion", ["pausar", "cancelar", "cerrar"])
def test_pausar_cancelar_o_cerrar_paran_el_nuevo_intento(ventana, tmp_path, accion):
    trabajos = _en_la_ventana(ventana, tmp_path)
    ventana._buscar_websearch()
    ventana._al_fallar_websearch("AirVault no responde")
    _terminar(ventana)
    assert ventana._reintentando_chequeo()
    {"pausar": ventana._pausar_proceso, "cancelar": ventana._cancelar, "cerrar": ventana.detener}[accion]()
    assert not ventana._reintentando_chequeo()
    assert not ventana._chequeo_por_repetir
    if accion == "pausar":
        # Al continuar se retoma el chequeo que estaba esperando.
        assert ventana._continuacion_lector == trabajos


@pytest.mark.parametrize("pedido_ahora", [False, True])
def test_el_reintento_que_espera_turno_tampoco_abre_ventana(ventana, tmp_path, pedido_ahora):
    """Solo lo que alguien acaba de pedir puede necesitar a esa persona."""
    en_curso, en_espera = _en_la_ventana(ventana, tmp_path)
    ventana._iniciar_confirmacion([en_curso], automatico=True)
    if pedido_ahora:
        ventana._iniciar_confirmacion([en_espera], automatico=False)
    else:
        ventana._chequeo_por_repetir = {str(en_espera.carpeta): en_espera}
        ventana._chequeo_por_repetir_manual = True
        ventana._repetir_chequeo()
    _terminar(ventana)
    estado = ventana._worker_websearch.estado
    assert estado["buscar_trabajos"] == [en_espera]
    assert not estado["confirmacion_automatica"]
    assert estado["sin_ventana"] is (not pedido_ahora)


def test_la_confirmacion_de_una_subida_no_repite_que_quedo_pendiente(ventana, tmp_path):
    """Lo dice el chequeo que alguien pidio, no cada vuelta de la subida."""
    sin_leer, _ = _en_la_ventana(ventana, tmp_path)
    ventana._intentos_de_chequeo = len(modulo.MINUTOS_ENTRE_CHEQUEOS)
    sin_leer.manifiesto.websearch_resultado = "error"
    sin_leer.manifiesto.websearch_detalle = MOTIVOS["carga"]
    ventana._iniciar_confirmacion([sin_leer], automatico=True)
    _terminar(ventana)
    assert "quedó pendiente tras varios intentos" not in _bitacora(ventana)
    ventana._iniciar_confirmacion([sin_leer], automatico=False)
    _terminar(ventana)
    assert "quedó pendiente tras varios intentos" in _bitacora(ventana)


def test_los_avisos_del_chequeo_llegan_a_la_bitacora(ventana, tmp_path):
    _en_la_ventana(ventana, tmp_path)
    ventana._buscar_websearch()
    antes = ventana.bitacora.count()
    ventana._al_buscar_websearch({"resultados": [], "terminado": False,
                                  "aviso": "AirVault no responde. Nuevo intento en 0:15 (2 de 5)."})
    assert ventana.bitacora.count() == antes + 1
    assert "Web Search: AirVault no responde. Nuevo intento en 0:15 (2 de 5)." in _bitacora(ventana)


@pytest.mark.parametrize("indexar_solo", [True, False])
def test_lo_que_se_vuelve_a_subir_se_indexa_siempre(ventana, tmp_path, monkeypatch, indexar_solo):
    """Completar depende de la casilla; indexar, no: asi volvio a faltar."""
    from app.gui.automatizacion import INDEXAR

    trabajos = _en_la_ventana(ventana, tmp_path, 1)
    ventana._opciones.fijar(INDEXAR, indexar_solo)
    ventana.completar_check.setChecked(False)
    lanzados = []
    monkeypatch.setattr(ventana, "_lanzar", lambda modo, estado: lanzados.append((modo, dict(estado))))
    ventana._ejecutar_accion("recuperar_ausentes", trabajos)
    ventana._ejecutar_accion("revisar_pendientes", trabajos)
    assert lanzados[0][0] == "recuperar_ausentes"
    assert lanzados[0][1]["indexar_al_encontrar"] is True
    assert lanzados[0][1]["completar"] is False
    # La revision normal sigue obedeciendo a la opcion de indexar.
    assert lanzados[1][1]["indexar_al_encontrar"] is indexar_solo
