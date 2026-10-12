"""El chequeo de Web Search se pide a mano; el proceso continua lo pendiente.

Empezar el proceso lanzaba ademas la revision de todos los batches ya
subidos y no se daba por terminado hasta que esa lectura acabara. Por ahora
ese chequeo solo corre cuando alguien lo pide, y empezar o continuar el
proceso se ocupa de lo suyo: subir, indexar y completar lo que no este
completado.
"""

from types import SimpleNamespace

import pytest

from app.airvault.flujo import COMPLETADO, INDEXADO, EstadoParte, estado_local
from app.airvault.model import EstadoEtapa
from app.gui import airvault_window as modulo
from tests.test_airvault_confirmacion import caso
from tests.test_airvault_revision_inicio import WorkerFalso, crear_trabajo, preparar


@pytest.fixture
def ventana(app, tmp_path, monkeypatch):
    monkeypatch.setattr(modulo, "TrabajoAirVaultWorker", WorkerFalso)
    return modulo.AirVaultWindow(tmp_path)


def test_el_chequeo_automatico_esta_apagado():
    assert modulo.CHEQUEO_WEBSEARCH_AUTOMATICO is False


@pytest.mark.parametrize("modo", ["subir", "comprobar", "subir_pendientes"])
def test_empezar_o_continuar_el_proceso_no_consulta_web_search(ventana, tmp_path, modo):
    subido = crear_trabajo(tmp_path, "subido", EstadoEtapa.HECHA)
    nuevo = crear_trabajo(tmp_path, "nuevo")
    terminado = crear_trabajo(tmp_path, "terminado", EstadoEtapa.HECHA, completado=True)
    preparar(ventana, [subido, nuevo, terminado])
    ventana._lanzar(modo, ventana._estado)
    ventana._confirmar_solo()
    ventana._ajustar_confirmacion()
    assert ventana._worker.isRunning()
    assert ventana._worker_websearch is None
    # Nada queda apuntado «por confirmar»: es lo que retenia el final.
    assert not ventana._websearch_inicio_por_revisar
    assert ventana._confirmador is None or not ventana._confirmador.isActive()


def test_la_revision_periodica_tampoco_lo_lanza(ventana, tmp_path, monkeypatch):
    subido = crear_trabajo(tmp_path, "subido", EstadoEtapa.HECHA)
    preparar(ventana, [subido])
    ventana.auto_check.setChecked(True)
    monkeypatch.setattr(ventana, "_iniciar_confirmacion",
                        lambda *_a, **_k: pytest.fail("El chequeo solo se pide a mano"))
    ventana._ajustar_confirmacion()
    ventana._confirmar_solo()
    ventana._ajustar_vigilancia()
    assert ventana._confirmador is None or not ventana._confirmador.isActive()


def test_el_proceso_termina_sin_esperar_a_web_search(ventana, tmp_path):
    """Con todo completado se anuncia el final, sin lectura pendiente."""
    terminado = crear_trabajo(tmp_path, "terminado", EstadoEtapa.HECHA, batch_id="B1", completado=True)
    preparar(ventana, [terminado])
    ventana._lanzar("comprobar", ventana._estado)
    ventana._worker.corriendo = False
    ventana._al_terminar()
    assert ventana._worker_websearch is None
    assert ventana.estado_label.text() == "Proceso terminado"
    assert "Todo lo solicitado se completó" in ventana.resumen.text()


def test_sin_completar_batch_los_indexados_se_quedan_indexados(ventana, tmp_path):
    """El proceso los da por terminados y no los cierra."""
    indexado = crear_trabajo(tmp_path, "indexado", EstadoEtapa.HECHA, batch_id="B1")
    ventana._trabajos = [indexado]
    ventana._estados = [EstadoParte(indexado, INDEXADO)]
    ventana._estado["trabajos"] = [indexado]
    ventana.completar_check.setChecked(False)
    assert ventana._firma_de_fin() is not None
    assert not ventana._falta_esperar()
    ventana.completar_check.setChecked(True)
    # Marcada la casilla, si queda trabajo: completarlos.
    assert ventana._firma_de_fin() is None


def test_revisar_en_airvault_mira_la_cola_y_no_web_search(ventana, tmp_path, monkeypatch):
    subido = crear_trabajo(tmp_path, "subido", EstadoEtapa.HECHA)
    preparar(ventana, [subido])
    pasos = []
    monkeypatch.setattr(ventana, "_comprobar", lambda: pasos.append("cola"))
    monkeypatch.setattr(ventana, "_iniciar_confirmacion",
                        lambda *_a, **_k: pytest.fail("El chequeo solo se pide a mano"))
    ventana._revisar_a_mano()
    assert pasos == ["cola"]
    assert ventana._cadena_manual
    assert not ventana._revision_airvault_pendiente


def test_el_chequeo_pedido_a_mano_sigue_funcionando(ventana, tmp_path):
    subido = crear_trabajo(tmp_path, "subido", EstadoEtapa.HECHA)
    terminado = crear_trabajo(tmp_path, "terminado", EstadoEtapa.HECHA, completado=True)
    preparar(ventana, [subido, terminado])
    ventana._buscar_websearch()
    estado = ventana._worker_websearch.estado
    assert estado["buscar_trabajos"] == [subido, terminado]
    assert not estado["confirmacion_automatica"]


def _con_ausente(ventana, tmp_path):
    ausente = caso(tmp_path / "ausente", 50)[0]
    ausente.manifiesto.websearch_resultado = "ausente"
    cancelado = caso(tmp_path / "cancelado", 50)[0]
    cancelado.manifiesto.websearch_resultado = "ausente"
    cancelado.manifiesto.cancelado = True
    confirmado = caso(tmp_path / "confirmado", 50)[0]
    confirmado.manifiesto.websearch_resultado = "encontrado"
    ventana._trabajos = [ausente, cancelado, confirmado]
    ventana._estados = [estado_local(t) for t in ventana._trabajos]
    return ausente


@pytest.mark.parametrize("continuar", [False, True], ids=["empezar", "continuar"])
def test_el_proceso_retoma_los_batches_que_no_estan_en_web_search(ventana, tmp_path, monkeypatch, continuar):
    """Son los que mas falta hace terminar: marcados como subidos y sin publicar."""
    ausente = _con_ausente(ventana, tmp_path)
    pasos, encoladas = [], []
    monkeypatch.setattr(ventana, "_subir_a_mano", lambda: pasos.append("subir"))
    monkeypatch.setattr(ventana, "_continuar_pendiente", lambda: pasos.append("continuar"))
    monkeypatch.setattr(ventana, "_encolar", lambda modo, trabajos, texto: encoladas.append((modo, trabajos, texto)))
    ventana._pausado = continuar
    ventana._empezar_o_continuar()
    assert pasos == ["continuar" if continuar else "subir"]
    assert [(modo, trabajos) for modo, trabajos, _ in encoladas] == [("recuperar_ausentes", [ausente])]
    assert "Se retoma 1 batch que no está en Web Search" in encoladas[0][2]


def test_sin_batches_ausentes_el_proceso_no_encola_nada(ventana, tmp_path, monkeypatch):
    subido = crear_trabajo(tmp_path, "subido", EstadoEtapa.HECHA)
    preparar(ventana, [subido])
    monkeypatch.setattr(ventana, "_subir_a_mano", lambda: None)
    monkeypatch.setattr(ventana, "_encolar", lambda *_: pytest.fail("No hay nada que recuperar"))
    ventana._empezar_o_continuar()


def test_pausar_y_continuar_retoma_el_proceso_y_el_chequeo_pedido(ventana, tmp_path, monkeypatch):
    """La lectura cortada no depende ya de un chequeo que arranque solo."""
    cancelados = []
    trabajo = SimpleNamespace(carpeta=tmp_path / "batch", manifiesto=SimpleNamespace(
        websearch_resultado="", cancelado=False))
    principal = SimpleNamespace(cancelar=lambda: cancelados.append("proceso"))
    lector = SimpleNamespace(cancelar=lambda: cancelados.append("lectura"),
                             estado={"buscar_trabajos": [trabajo]})
    activo = [True]
    monkeypatch.setattr(ventana, "hilo", lambda: principal if activo[0] else None)
    monkeypatch.setattr(ventana, "lectura_websearch", lambda: lector if activo[0] else None)
    ventana._pausar_proceso()
    assert cancelados == ["proceso", "lectura"]
    assert ventana._continuacion_lector == [trabajo]
    assert not ventana._websearch_inicio_por_revisar
    activo[0] = False
    pasos = []
    monkeypatch.setattr(ventana, "_continuar_pendiente", lambda: pasos.append("proceso"))
    monkeypatch.setattr(ventana, "_iniciar_confirmacion",
                        lambda trabajos, automatico: pasos.append(("chequeo", trabajos, automatico)))
    ventana._empezar_o_continuar()
    assert pasos == ["proceso", ("chequeo", [trabajo], False)]
    assert ventana._continuacion_lector is None


def test_pausar_solo_el_chequeo_no_arranca_el_proceso_al_continuar(ventana, tmp_path, monkeypatch):
    trabajo = SimpleNamespace(carpeta=tmp_path / "batch")
    lector = SimpleNamespace(cancelar=lambda: None, estado={"buscar_trabajos": [trabajo]})
    activo = [True]
    monkeypatch.setattr(ventana, "lectura_websearch", lambda: lector if activo[0] else None)
    ventana._pausar_proceso()
    activo[0] = False
    pasos = []
    monkeypatch.setattr(ventana, "_continuar_pendiente", lambda: pytest.fail("Solo se pidio el chequeo"))
    monkeypatch.setattr(ventana, "_retomar_ausentes", lambda: pytest.fail("Solo se pidio el chequeo"))
    monkeypatch.setattr(ventana, "_iniciar_confirmacion",
                        lambda trabajos, automatico: pasos.append((trabajos, automatico)))
    ventana._empezar_o_continuar()
    assert pasos == [([trabajo], False)]


def test_un_batch_completado_sin_prueba_no_se_toca_al_empezar(ventana, tmp_path, monkeypatch):
    """Sin chequeo no hay ausencia comprobada: el proceso no lo reenvia."""
    completado = crear_trabajo(tmp_path, "completado", EstadoEtapa.HECHA, batch_id="B1", completado=True)
    preparar(ventana, [completado])
    assert estado_local(completado).estado == COMPLETADO
    monkeypatch.setattr(ventana, "_subir_a_mano", lambda: None)
    monkeypatch.setattr(ventana, "_encolar", lambda *_: pytest.fail("No hay ausencia comprobada"))
    ventana._empezar_o_continuar()
