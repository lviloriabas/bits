"""Reparto de correcciones sin acceder ni escribir en AirVault."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Barrier, Event, Lock

import pytest
from PySide6.QtCore import Qt

from app.airvault import correcciones, web_reports
from app.airvault.config import AirVaultConfig
from app.airvault.correcciones import (
    ACCION_BORRAR, Correccion, CorrectorLogPageAudit, Resultado,
)
from app.airvault.mapping import ResolutorFlota
from app.airvault.web_reports import (
    ConsultaCancelada, ExcepcionLogPageAudit, TIPO_DUPLICADA, _Pagina,
)
from app.gui.web_reports_window import CorreccionWorker


def _caso(numero: int) -> Correccion:
    return Correccion(ACCION_BORRAR, ExcepcionLogPageAudit(
        tipo=TIPO_DUPLICADA, matricula_libro="HP-9913CMP",
        tipo_libro="Copa-7 (50)", rango_libro="", rango_fechas="",
        faltantes="", duplicadas="", fecha_sospechosa="", detalle="",
        log_number=str(2008150 + numero), copias=2,
        url_busqueda=f"https://airvault/busqueda/{numero}",
    ), sobran=1)


def _aplicar(corrector, plan, progreso=lambda *_: None, cancelar=lambda: False):
    return corrector._aplicar_pendientes(
        None, {}, plan, lambda _: None, cancelar, True, progreso,
    )


@pytest.mark.parametrize("total,libre,hilos,activos,esperados", [
    (4096, 1800, 4, 0, 1),
    (8192, 3500, 8, 0, 2),
    (16384, 5000, 12, 0, 3),
    (32768, 10000, 64, 0, 9),
    (32768, 10000, 1, 0, 9),
    (65536, 60000, 64, 0, 74),
    (32768, 0, 12, 0, 1),
    (32768, 3000, 12, 0, 1),
    (8192, 2304, 8, 2, 3),
    (8192, 1536, 8, 2, 2),
])
def test_reutiliza_la_ram_libre_y_la_reserva_del_equipo(
    monkeypatch, total, libre, hilos, activos, esperados,
):
    monkeypatch.setattr(correcciones.parallelism, "total_memory_mb", lambda: total)
    monkeypatch.setattr(correcciones.parallelism, "available_memory_mb", lambda: libre)
    monkeypatch.setattr(correcciones.parallelism, "available_cpu_threads", lambda: hilos)
    assert correcciones._paralelismo_correcciones(activos) == esperados


@pytest.mark.parametrize("cantidad", [6, 12])
def test_abre_todos_los_casos_que_caben_en_ram_aunque_superen_los_hilos(
    monkeypatch, cantidad,
):
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    barrera = Barrier(cantidad, timeout=5)
    ocupadas = set()
    candado = Lock()
    picos = []

    def atender(_navegador, _version, caso, _cancelar, _ensayo):
        with candado:
            ocupadas.add(caso.log_number)
            picos.append(len(ocupadas))
        barrera.wait()
        with candado:
            ocupadas.remove(caso.log_number)
        return Resultado(caso, hecho=True)

    monkeypatch.setattr(correcciones.parallelism, "total_memory_mb", lambda: 32768)
    monkeypatch.setattr(correcciones.parallelism, "available_cpu_threads", lambda: 2)
    monkeypatch.setattr(correcciones.parallelism, "available_memory_mb",
                        lambda: 3072 + cantidad * 768)
    monkeypatch.setattr(corrector, "_un_caso", atender)
    resultados = _aplicar(corrector, [_caso(n) for n in range(cantidad)])
    assert len(resultados) == cantidad
    assert all(r.hecho for r in resultados)
    assert max(picos) == cantidad
    assert not ocupadas


def test_solapa_casos_y_devuelve_resultados_en_el_orden_del_plan(monkeypatch):
    plan = [_caso(n) for n in range(4)]
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    barrera = Barrier(2, timeout=3)
    segundo_listo = Event()
    candado = Lock()
    activos = 0
    pico = 0
    acabadas = []
    pasos = []

    def atender(_navegador, _version, caso, _cancelar, _ensayo):
        nonlocal activos, pico
        with candado:
            activos += 1
            pico = max(pico, activos)
        barrera.wait()
        if caso is plan[0]:
            assert segundo_listo.wait(3)
        with candado:
            acabadas.append(caso)
            activos -= 1
        if caso is plan[1]:
            segundo_listo.set()
        return Resultado(caso, hecho=True)

    monkeypatch.setattr(correcciones, "_paralelismo_correcciones", lambda _n: 2)
    monkeypatch.setattr(corrector, "_un_caso", atender)
    resultados = _aplicar(corrector, plan, lambda *p: pasos.append(p))

    assert pico == 2
    assert acabadas.index(plan[1]) < acabadas.index(plan[0])
    assert [r.correccion for r in resultados] == plan
    assert pasos == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_nunca_abre_dos_tareas_para_la_misma_bitacora(monkeypatch):
    plan = [_caso(0), _caso(0), _caso(1)]
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    barrera = Barrier(2, timeout=3)
    ocupadas = set()
    candado = Lock()

    def atender(_navegador, _version, caso, _cancelar, _ensayo):
        with candado:
            assert caso.log_number not in ocupadas
            ocupadas.add(caso.log_number)
        if caso is not plan[1]:
            barrera.wait()
        with candado:
            ocupadas.remove(caso.log_number)
        return Resultado(caso)

    monkeypatch.setattr(correcciones, "_paralelismo_correcciones", lambda _n: 2)
    monkeypatch.setattr(corrector, "_un_caso", atender)
    assert [r.correccion for r in _aplicar(corrector, plan)] == plan
    assert not ocupadas


def test_si_baja_la_ram_las_siguientes_bitacoras_van_de_una_en_una(monkeypatch):
    plan = [_caso(n) for n in range(5)]
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    barrera = Barrier(2, timeout=3)
    poca_memoria = Event()
    ocupadas = set()
    candado = Lock()

    def atender(_navegador, _version, caso, _cancelar, _ensayo):
        with candado:
            if caso not in plan[:2]:
                assert not ocupadas
            ocupadas.add(caso.log_number)
        if caso in plan[:2]:
            barrera.wait()
            poca_memoria.set()
        with candado:
            ocupadas.remove(caso.log_number)
        return Resultado(caso)

    monkeypatch.setattr(correcciones.parallelism, "total_memory_mb", lambda: 8192)
    monkeypatch.setattr(correcciones.parallelism, "available_cpu_threads", lambda: 8)
    monkeypatch.setattr(correcciones.parallelism, "available_memory_mb",
                        lambda: 1536 if poca_memoria.is_set() else 3072)
    monkeypatch.setattr(corrector, "_un_caso", atender)
    assert len(_aplicar(corrector, plan)) == 5


def test_no_reparte_dos_veces_la_ram_mientras_las_pestanas_cargan(monkeypatch):
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    cargar = Event()
    candado = Lock()
    lecturas = 0
    activos = 0
    pico = 0

    def memoria():
        nonlocal lecturas
        lecturas += 1
        if lecturas >= 4:
            cargar.set()
        # Los procesos todavía no ocupan la RAM que se les presupuestó.
        return 3072

    def atender(_navegador, _version, caso, _cancelar, _ensayo):
        nonlocal activos, pico
        with candado:
            activos += 1
            pico = max(pico, activos)
        assert cargar.wait(3)
        with candado:
            activos -= 1
        return Resultado(caso)

    monkeypatch.setattr(correcciones.parallelism, "total_memory_mb", lambda: 8192)
    monkeypatch.setattr(correcciones.parallelism, "available_cpu_threads", lambda: 8)
    monkeypatch.setattr(correcciones.parallelism, "available_memory_mb", memoria)
    monkeypatch.setattr(corrector, "_un_caso", atender)
    assert len(_aplicar(corrector, [_caso(n) for n in range(4)])) == 4
    assert pico == 2


def test_cancelar_no_inicia_mas_casos_y_espera_antes_de_soltar_edge(monkeypatch):
    plan = [_caso(n) for n in range(5)]
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    parar = Event()
    candado = Lock()
    abiertas = []
    cerradas = []
    salio = []

    class Navegador:
        def __init__(self, _perfil):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            assert len(cerradas) == 2
            salio.append(True)

        def abrir(self, *_args, **_kwargs):
            return {}

        def cookies(self, _version):
            return {}

    def atender(_navegador, _version, caso, cancelar, _ensayo):
        with candado:
            abiertas.append(caso)
            if len(abiertas) == 2:
                parar.set()
        assert parar.wait(3)
        try:
            assert cancelar()
            raise ConsultaCancelada()
        finally:
            with candado:
                cerradas.append(caso)

    monkeypatch.setattr(correcciones, "_NavegadorDeCorrecciones", Navegador)
    monkeypatch.setattr(correcciones, "esperar_acceso", lambda *_: None)
    monkeypatch.setattr(correcciones, "_paralelismo_correcciones", lambda _n: 2)
    monkeypatch.setattr(corrector, "_un_caso", atender)
    with pytest.raises(ConsultaCancelada):
        corrector.aplicar(plan, cancelar=parar.is_set)
    assert len(abiertas) == 2
    assert salio == [True]


def test_retira_la_pestana_si_falla_el_controlador(monkeypatch):
    cerradas = []

    class Navegador:
        def abrir_pestana(self, *_args, **_kwargs):
            return "temporal"

        def cerrar_pestana(self, _version, target_id):
            cerradas.append(target_id)

    def falla(*_args):
        raise ConsultaCancelada()

    monkeypatch.setattr(correcciones, "_Pagina", falla)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    with pytest.raises(ConsultaCancelada):
        corrector._un_caso(Navegador(), {}, _caso(0), lambda: False, True)
    assert cerradas == ["temporal"]


def test_cancelar_suelta_el_documento_antes_de_cerrar_su_pestana():
    pedidos = []

    class Socket:
        def pedir(self, metodo, **parametros):
            pedidos.append((metodo, parametros))
            return {"result": {"value": "OK"}}

    pagina = object.__new__(_Pagina)
    pagina.ws = Socket()
    pagina._cancelar = lambda: True
    with pytest.raises(ConsultaCancelada):
        with CorrectorLogPageAudit._soltando(pagina, "reindexDialog"):
            pagina.evaluar("true")
    assert len(pedidos) == 1
    assert pedidos[0][0] == "Runtime.evaluate"
    assert "reindexDialog" in pedidos[0][1]["expression"]
    assert "boton.click()" in pedidos[0][1]["expression"]


def test_cierra_solo_la_pestana_temporal_aunque_edge_se_conserve(monkeypatch):
    pedidos = []

    class Socket:
        def __init__(self, *_args, **_kwargs):
            pass

        def pedir(self, metodo, **parametros):
            pedidos.append((metodo, parametros))
            return {}

        def cerrar(self):
            pass

    pagina = object.__new__(_Pagina)
    pagina.ws = Socket()
    pagina._version = {"webSocketDebuggerUrl": "ws://local"}
    pagina._target_id = "temporal"
    monkeypatch.setattr(web_reports, "cierre_diferido_activo", lambda: True)
    monkeypatch.setattr(web_reports, "_WebSocket", Socket)
    pagina.cerrar(forzar=True)
    assert pedidos == [("Target.closeTarget", {"targetId": "temporal"})]
    assert pagina.ws is None


def test_las_revisiones_paralelas_conservan_su_respuesta(app):
    worker = CorreccionWorker(AirVaultConfig(), [])
    worker.mostrar_previas = True
    primera_abierta = Event()
    segunda_llama = Event()
    contestar_primera = Event()
    expuestas = []

    def mostrar(caso, _vistas, _elegidas):
        expuestas.append(caso)
        if caso == "primera":
            primera_abierta.set()
            assert contestar_primera.wait(3)
        worker.responder_previa({caso})

    worker.previa.connect(mostrar, Qt.ConnectionType.DirectConnection)

    def pedir_segunda():
        segunda_llama.set()
        return worker._revisar("segunda", [], set())

    with ThreadPoolExecutor(max_workers=2) as pool:
        primera = pool.submit(worker._revisar, "primera", [], set())
        assert primera_abierta.wait(3)
        segunda = pool.submit(pedir_segunda)
        try:
            assert segunda_llama.wait(3)
            with pytest.raises(TimeoutError):
                segunda.result(timeout=.1)
            assert expuestas == ["primera"]
        finally:
            contestar_primera.set()
        assert primera.result(timeout=3) == {"primera"}
        assert segunda.result(timeout=3) == {"segunda"}
    assert expuestas == ["primera", "segunda"]


def test_cancelar_despierta_la_revision_que_espera_turno(app):
    worker = CorreccionWorker(AirVaultConfig(), [])
    worker._candado_revision.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            revision = pool.submit(worker._revisar, "esperando", [], set())
            worker.cancelar()
            with pytest.raises(ConsultaCancelada):
                revision.result(timeout=3)
    finally:
        worker._candado_revision.release()


def test_apagar_el_visor_libera_los_casos_que_esperan_con_su_opcion_default(app):
    worker = CorreccionWorker(AirVaultConfig(), [])
    worker.mostrar_previas = True
    primera_abierta = Event()
    cerrar_primera = Event()
    expuestas = []

    def mostrar(caso, _vistas, elegidas):
        expuestas.append(caso)
        primera_abierta.set()
        assert cerrar_primera.wait(3)
        worker.mostrar_previas = False
        worker.responder_previa(elegidas)

    worker.previa.connect(mostrar, Qt.ConnectionType.DirectConnection)
    with ThreadPoolExecutor(max_workers=2) as pool:
        primera = pool.submit(worker._revisar, "primera", [], {"a"})
        assert primera_abierta.wait(3)
        segunda = pool.submit(worker._revisar, "segunda", [], {"b", "c"})
        cerrar_primera.set()
        assert primera.result(timeout=3) == {"a"}
        assert segunda.result(timeout=3) == {"b", "c"}
    assert expuestas == ["primera"]
