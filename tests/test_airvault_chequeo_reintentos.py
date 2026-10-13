"""El chequeo de Web Search no se queda a medias por un tropiezo de AirVault.

Lo que paso: el servidor contesto con error justo al abrir Web Search y el
chequeo salio sin mirar un solo batch. La ventana se quedo contando hacia la
revision periodica, que mira la cola de Web Index y no esto, y los batches
que no estaban publicados siguieron sin subirse.
"""

from types import SimpleNamespace

import pytest

from app.airvault import confirmacion, websearch_pagina
from app.airvault.confirmacion import plazo_de_batch, verificar_batch
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


def test_lo_ya_leido_no_se_vuelve_a_preguntar(tmp_path):
    t, buscador, _, consultas = caso(tmp_path, 50)
    leidas: dict = {}
    assert verificar_batch(buscador, t, consultas=leidas).confirmado
    assert len(consultas) == len(leidas) == 7
    assert verificar_batch(buscador, t, consultas=leidas).confirmado
    assert len(consultas) == 7


def test_la_vuelta_que_repite_un_batch_solo_pide_lo_que_faltaba(app, tmp_path):
    """Llevaba encontradas 22 de 25 y el batch volvia a empezar de cero."""
    t, buscador, _, _ = caso(tmp_path, 50)
    original = buscador.sesion.get
    llamadas = []

    def get(*args):
        llamadas.append(1)
        if len(llamadas) == 5:
            raise TimeoutError("Sin respuesta")
        return original(*args)

    buscador.sesion.get = get
    _, avisos = chequear([t], buscador)
    assert t.manifiesto.websearch_resultado == "encontrado"
    # Cuatro leidas y una que no contesto; despues, solo las tres que faltaban.
    assert len(llamadas) == 8
    assert avisos[-1]["confirmados"] == 1 and avisos[-1]["errores"] == 0


def test_el_plazo_de_un_batch_da_tiempo_a_cada_bitacora():
    """Con diez segundos por bitacora ningun batch terminaba con AirVault cargado."""
    assert plazo_de_batch(25) == 25 * confirmacion.SEGUNDOS_POR_BITACORA == 1500
    assert plazo_de_batch(1) == confirmacion.PLAZO_MINIMO_DE_BATCH_S == 120


def test_el_hilo_cuenta_el_avance_de_cada_batch_por_separado(app, tmp_path):
    uno, buscador, _, _ = caso(tmp_path / "a", 50)
    otro, _, _, _ = caso(tmp_path / "b", 50)
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "buscar_trabajos": [uno, otro], "buscador": buscador, "revisar_todos": True,
    })
    avances, pasos = [], []
    worker.avance_websearch.connect(avances.append)
    worker.paso.connect(lambda texto, _hechas, _total: pasos.append(texto))
    worker._buscar_websearch()
    # Cada lectura corre en su hilo y su avance llega a la ventana por la cola.
    app.processEvents()
    for t in (uno, otro):
        propios = [a for a in avances if a["carpeta"] == str(t.carpeta)]
        assert [a["hechas"] for a in propios][:7] == [0, 1, 2, 3, 4, 5, 6]
        assert propios[-1]["hechas"] == 7
        assert all(a["total"] == 7 and a["nombre"] == "DP | ENTREGA" for a in propios)
    # El aviso comun ya no salta de un batch a otro.
    assert pasos == ["Revisando batches en Web Search"]


def test_cuantos_batches_se_leen_a_la_vez_lo_decide_la_memoria_libre():
    """Cada batch en lectura es una pestana: se abren las que caben."""
    tope, minimo = confirmacion.MAX_BATCHES_PARALELOS, confirmacion.MINIMO_BATCHES_PARALELOS
    assert tope == 20
    # Un equipo de 32 GB con la mitad libre llega al tope.
    assert confirmacion.batches_paralelos(16_000, 32_000) == tope
    # En uno de 16 GB se aparta la cuarta parte: con 5,6 GB libres caben diez.
    assert confirmacion.batches_paralelos(5_600, 16_000) == 10
    # La reserva nunca baja de 3 GB, aunque el equipo sea pequeno.
    assert confirmacion.batches_paralelos(4_000, 8_000) == 6
    # Sin margen se sigue leyendo, pero de a pocos.
    assert confirmacion.batches_paralelos(2_500, 16_000) == minimo
    assert confirmacion.batches_paralelos(300, 16_000) == minimo
    # Sin poder medir se usa el valor de siempre.
    assert confirmacion.batches_paralelos(0, 0) == confirmacion.BATCHES_PARALELOS_SIN_MEDIDA
    for libre in range(0, 40_000, 517):
        assert minimo <= confirmacion.batches_paralelos(libre, 16_000) <= tope


def test_no_se_leen_mas_batches_a_la_vez_de_los_que_caben(app, tmp_path, monkeypatch):
    """El chequeo abre las pestanas que le dice la memoria, y ni una mas."""
    import threading
    import time as reloj

    monkeypatch.setattr(confirmacion, "batches_paralelos", lambda *_: 10)
    trabajos = [caso(tmp_path / str(n), 1)[0] for n in range(24)]
    candado, activos, maximo = threading.Lock(), [0], [0]

    def consultar_numero(numero, _presupuesto):
        with candado:
            activos[0] += 1
            maximo[0] = max(maximo[0], activos[0])
        reloj.sleep(0.03)
        with candado:
            activos[0] -= 1
        return {"rows": [{"LogNo": numero}]}

    buscador = SimpleNamespace(sesion=SimpleNamespace(), config=trabajos[0].config,
                              consultar_numero=consultar_numero)
    _, avisos = chequear(trabajos, buscador)
    assert avisos[-1]["confirmados"] == 24
    assert 1 < maximo[0] <= 10


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
    # «Revisar en AirVault» solo empieza por Web Search con el chequeo
    # automatico encendido; es la cadena que esta prueba sigue.
    monkeypatch.setattr("app.gui.airvault_window.CHEQUEO_WEBSEARCH_AUTOMATICO", True)
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
    assert viva.startswith("En espera de AirVault: próxima revisión automática en")


def test_gastados_los_intentos_el_chequeo_queda_pendiente_y_se_dice(ventana, tmp_path, monkeypatch):
    monkeypatch.setattr("app.gui.airvault_window.CHEQUEO_WEBSEARCH_AUTOMATICO", True)
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
    ausente.manifiesto.websearch_muestra = list("1234567")
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
    assert "Batch 2 «DP | ENTREGA»: sin terminar; se retoma al final" in texto
    assert ("Batch 1 «DP | ENTREGA»: no está publicado, 0 de 7 bitácoras encontradas; "
            "queda pendiente de subir") in texto
    assert "Web Search: chequeo terminado, 2 de 2 batches revisados" in texto
    assert "0 confirmados, 1 sin publicar y 1 sin verificar" in texto


def test_un_chequeo_limpio_no_deja_nada_esperando(ventana, tmp_path, monkeypatch):
    confirmado, _ = _en_la_ventana(ventana, tmp_path)
    monkeypatch.setattr(ventana, "_encolar", lambda *_: None)
    ventana._intentos_de_chequeo = 3
    ventana._buscar_websearch()
    for t in ventana._trabajos:
        t.manifiesto.websearch_resultado = "encontrado"
        t.manifiesto.websearch_muestra = list("1234567")
        t.manifiesto.websearch_cotejadas = 7
    ventana._al_buscar_websearch({"resultados": [(confirmado, True)], "revisados": 1, "total": 2})
    ventana._al_buscar_websearch({"resultados": [], "terminado": True, "ausentes": [], "errores": 0,
                                  "revisados": 2, "confirmados": 2, "total": 2})
    _terminar(ventana)
    assert not ventana._reintentando_chequeo()
    assert ventana._intentos_de_chequeo == 0
    assert "Batch 1 «DP | ENTREGA»: confirmado, 7 de 7 bitácoras encontradas" in _bitacora(ventana)


def _lista(ventana, tmp_path):
    uno, dos = _en_la_ventana(ventana, tmp_path)
    uno.manifiesto.nombre_batch, dos.manifiesto.nombre_batch = "Entrega -1", "Entrega -2"
    ventana._buscar_websearch()

    def avance(trabajo, hechas, total=7):
        ventana._al_avanzar_websearch(dict(carpeta=str(trabajo.carpeta), hechas=hechas, total=total,
                                           nombre=trabajo.manifiesto.nombre_batch))

    return uno, dos, avance


def _lineas(ventana):
    return [ventana.bitacora.item(i).text() for i in range(ventana.bitacora.count())]


def test_cada_batch_tiene_su_linea_y_avanza_en_su_sitio(ventana, tmp_path):
    """Una sola frase que saltaba entre treinta batches no dejaba seguir ninguno."""
    uno, dos, avance = _lista(ventana, tmp_path)
    antes = ventana.bitacora.count()
    avance(uno, 0)
    avance(dos, 0)
    assert ventana.bitacora.count() == antes + 2
    for hechas in (1, 2, 3):
        avance(uno, hechas)
    avance(dos, 5)
    # Ninguna linea nueva: cada batch mueve la suya.
    assert ventana.bitacora.count() == antes + 2
    lineas = _lineas(ventana)
    assert lineas[-3].endswith("Batch 1 «Entrega -1»: revisando, 3 de 7 bitácoras")
    assert lineas[-2].endswith("Batch 2 «Entrega -2»: revisando, 5 de 7 bitácoras")
    # Debajo queda el resumen, que no cambia de batch.
    assert "0 de 2 batches" in lineas[-1]
    assert "Entrega" not in lineas[-1]
    # Lo que se esta leyendo gira, como todo lo que esta en marcha.
    assert not ventana._filas_websearch[str(uno.carpeta)].icon().isNull()


def test_al_terminar_un_batch_su_linea_se_queda_con_el_resultado(ventana, tmp_path):
    uno, dos, avance = _lista(ventana, tmp_path)
    avance(uno, 6)
    avance(dos, 2)
    cuantas = ventana.bitacora.count()
    uno.manifiesto.websearch_resultado = "encontrado"
    uno.manifiesto.websearch_muestra = list("1234567")
    uno.manifiesto.websearch_cotejadas = 7
    ventana._al_buscar_websearch({"resultados": [(uno, True)], "revisados": 1, "total": 2})
    fila = ventana._filas_websearch[str(uno.carpeta)]
    assert ventana.bitacora.count() == cuantas
    assert fila.text().endswith("Batch 1 «Entrega -1»: confirmado, 7 de 7 bitácoras encontradas")
    assert fila.icon().isNull()
    assert str(uno.carpeta) not in ventana._filas_websearch_activas
    # El otro sigue leyendose en su linea.
    assert str(dos.carpeta) in ventana._filas_websearch_activas
    ventana._pintar_linea_viva()
    assert "1 de 2 batches" in _lineas(ventana)[-1]


def test_el_batch_que_se_queda_corto_dice_cuanto_llevaba_y_se_retoma_en_su_linea(ventana, tmp_path):
    uno, dos, avance = _lista(ventana, tmp_path)
    avance(dos, 22, 25)
    dos.manifiesto.websearch_resultado = "error"
    dos.manifiesto.websearch_detalle = MOTIVOS["carga"]
    ventana._al_buscar_websearch({"resultados": [(dos, False)], "por_repetir": True,
                                  "revisados": 0, "total": 2})
    fila = ventana._filas_websearch[str(dos.carpeta)]
    assert fila.text().endswith(
        "Batch 2 «Entrega -2»: sin terminar, 22 de 25 bitácoras leídas; se retoma al final")
    assert fila.icon().isNull()
    cuantas = ventana.bitacora.count()
    avance(dos, 23, 25)
    assert ventana.bitacora.count() == cuantas
    assert fila.text().endswith("Batch 2 «Entrega -2»: revisando, 23 de 25 bitácoras")
    dos.manifiesto.websearch_detalle = MOTIVOS["carga"]
    ventana._al_buscar_websearch({"resultados": [(dos, False)], "revisados": 1, "total": 2})
    assert fila.text().endswith("Batch 2 «Entrega -2»: sin verificar. " + MOTIVOS["carga"])


def test_un_batch_publicado_a_medias_no_se_anuncia_como_ausente(ventana, tmp_path):
    """Con 6 de 7 encontradas hay bitacoras suyas arriba: no es «no esta»."""
    uno, _, avance = _lista(ventana, tmp_path)
    avance(uno, 7)
    uno.manifiesto.websearch_resultado = "ausente"
    uno.manifiesto.websearch_muestra = list("1234567")
    uno.manifiesto.websearch_cotejadas = 6
    ventana._al_buscar_websearch({"resultados": [(uno, False)], "revisados": 1, "total": 2})
    fila = ventana._filas_websearch[str(uno.carpeta)]
    assert fila.text().endswith(
        "Batch 1 «Entrega -1»: incompleto en Web Search, 6 de 7 bitácoras encontradas; "
        "se revisa entero antes de volver a subir")


def test_al_cortarse_el_chequeo_ninguna_linea_se_queda_girando(ventana, tmp_path):
    uno, dos, avance = _lista(ventana, tmp_path)
    avance(uno, 3)
    ventana._cancelar()
    _terminar(ventana)
    fila = ventana._filas_websearch[str(uno.carpeta)]
    assert fila.text().endswith("Batch 1 «Entrega -1»: lectura sin terminar, 3 de 7 bitácoras leídas")
    assert fila.icon().isNull()
    assert not ventana._filas_websearch_activas


def test_cada_chequeo_empieza_su_lista_y_conserva_la_anterior(ventana, tmp_path):
    uno, dos, avance = _lista(ventana, tmp_path)
    avance(uno, 7)
    uno.manifiesto.websearch_resultado = "encontrado"
    uno.manifiesto.websearch_muestra = list("1234567")
    uno.manifiesto.websearch_cotejadas = 7
    ventana._al_buscar_websearch({"resultados": [(uno, True)], "revisados": 1, "total": 2})
    ventana._al_buscar_websearch({"resultados": [], "terminado": True, "ausentes": [], "errores": 0,
                                  "revisados": 2, "confirmados": 2, "total": 2})
    _terminar(ventana)
    anterior = ventana._filas_websearch[str(uno.carpeta)]
    ventana._buscar_websearch()
    assert ventana._filas_websearch == {}
    avance(uno, 1)
    nueva = ventana._filas_websearch[str(uno.carpeta)]
    assert nueva is not anterior
    assert anterior.text().endswith("confirmado, 7 de 7 bitácoras encontradas")
    assert nueva.text().endswith("Batch 1 «Entrega -1»: revisando, 1 de 7 bitácoras")


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


def test_la_hora_de_la_linea_no_avanza_mientras_el_batch_se_lee(ventana, tmp_path, monkeypatch):
    """Una docena de horas saltando a la vez parecian relojes, y no lo eran."""
    uno, dos, avance = _lista(ventana, tmp_path)
    cuenta = {"n": 0}

    def hora(_formato):
        cuenta["n"] += 1
        return f"19:59:{cuenta['n']:02d}"

    monkeypatch.setattr(modulo.time, "strftime", hora)
    avance(uno, 1)
    avance(dos, 1)
    avance(uno, 2)
    avance(uno, 3)
    de_uno = next(linea for linea in _lineas(ventana) if "Entrega -1" in linea)
    de_dos = next(linea for linea in _lineas(ventana) if "Entrega -2" in linea)
    # La hora es la de cuando cada batch empezo a leerse; la cuenta si avanza.
    assert de_uno == "19:59:01  Batch 1 «Entrega -1»: revisando, 3 de 7 bitácoras"
    assert de_dos.startswith("19:59:02  ")
    # Al quedar con su resultado lleva la hora en que termino.
    ventana._fila_websearch(str(uno.carpeta), "Batch «Entrega -1»: confirmado", False)
    de_uno = next(linea for linea in _lineas(ventana) if "Entrega -1" in linea)
    assert de_uno.startswith("19:59:0") and not de_uno.startswith("19:59:01")


def test_la_linea_viva_estima_lo_que_falta_del_chequeo(ventana, tmp_path, monkeypatch):
    """Un solo tiempo para todos los batches, por las bitacoras que quedan."""
    reloj = [1000.0]
    monkeypatch.setattr(modulo.time, "monotonic", lambda: reloj[0])
    uno, dos, avance = _lista(ventana, tmp_path)
    assert ventana._bitacoras_websearch == {str(uno.carpeta): 7, str(dos.carpeta): 7}
    # Abrir Web Search no cuenta: todavia no se ha leido nada.
    reloj[0] += 40
    assert ventana._restante_websearch() == ""
    avance(uno, 1)
    assert ventana._restante_websearch() == "calculando el tiempo restante"
    reloj[0] += 60
    avance(uno, 6)
    avance(dos, 1)
    # Siete de catorce en un minuto: queda otro tanto.
    assert ventana._restante_websearch() == "tiempo restante estimado: aprox. 1 min"
    ventana._pintar_linea_viva()
    assert "tiempo restante estimado: aprox. 1 min" in ventana._linea_viva.text()
    # El que ya contesto cuenta entero aunque su ultima cuenta fuera menor.
    ventana._websearch_terminados.add(str(uno.carpeta))
    reloj[0] += 60
    avance(dos, 6)
    assert ventana._restante_websearch() == "tiempo restante estimado: menos de 1 min"
    ventana._websearch_terminados.add(str(dos.carpeta))
    assert ventana._restante_websearch() == ""


def test_lo_que_falta_se_dice_sin_segundos():
    assert modulo._duracion_aproximada(20) == "menos de 1 min"
    assert modulo._duracion_aproximada(240) == "aprox. 4 min"
    assert modulo._duracion_aproximada(3600) == "aprox. 1 h"
    assert modulo._duracion_aproximada(5400) == "aprox. 1 h 30 min"
