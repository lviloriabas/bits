"""Audita cierres locales y recupera solo ausencias leidas sin errores."""

import re
from types import SimpleNamespace

import pytest

from app.airvault.confirmacion import verificar_batch
from app.airvault.flujo import COMPLETADO, NO_ENCONTRADO, Trabajo, estado_local
from app.airvault.manifest import cargar, guardar
from app.airvault.model import EstadoEtapa
from app.airvault.session import ErrorDeSesion, SesionAirVault
from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker
from tests.test_airvault_confirmacion import caso
from tests.test_airvault_revision_inicio import WorkerFalso


def comprobar(t, buscador, app, **estado):
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "buscar_trabajos": [t], "buscador": buscador, "revisar_todos": True, **estado,
    })
    avisos = []
    worker.buscado.connect(avisos.append)
    worker._buscar_websearch()
    return worker, avisos


def test_reconsulta_confirmados_y_revoca_un_cierre_falso(app, tmp_path):
    t, buscador, filas, consultas = caso(tmp_path, 50)
    comprobar(t, buscador, app)
    assert t.manifiesto.etapa_hecha("completar")
    antiguo = cargar(t.carpeta)
    filas.clear()
    consultas.clear()
    _, avisos = comprobar(t, buscador, app)
    assert len(consultas) == 7
    assert t.manifiesto.websearch_resultado == "ausente"
    assert not t.manifiesto.etapa_hecha("completar")
    assert estado_local(t).estado == NO_ENCONTRADO
    assert avisos[-1]["ausentes"][0].carpeta == t.carpeta
    assert all("ausentes" not in aviso for aviso in avisos[:-1])
    guardar(antiguo, t.carpeta)
    final = cargar(t.carpeta)
    assert not final.etapa_hecha("completar")
    assert final.websearch_resultado == "ausente"


@pytest.mark.parametrize("respuesta", [{"error": "No autorizado"}, {"records": 0}, None, "Login"])
def test_respuesta_ilegible_no_demuestra_una_ausencia(app, tmp_path, respuesta):
    t, buscador, _, _ = caso(tmp_path, 50)
    buscador.sesion.get = lambda *_: respuesta
    _, avisos = comprobar(t, buscador, app)
    assert t.manifiesto.websearch_resultado == "error"
    assert not avisos[-1]["ausentes"]
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [t]
    ventana._estados = [estado_local(t)]
    ventana._pintar_lotes()
    assert "chequeo pendiente" in ventana.lotes.item(0, 3).text()
    assert "revisado en AirVault" not in ventana.lotes.item(0, 3).text()


@pytest.mark.parametrize("error", [ErrorDeSesion("Caducada"), TimeoutError("Sin respuesta")])
def test_error_de_login_o_red_no_reenvia_y_no_marca_revisado(app, tmp_path, error):
    t, buscador, _, _ = caso(tmp_path, 50)
    comprobar(t, buscador, app)

    def fallar(*_):
        raise error

    buscador.sesion.get = fallar
    _, avisos = comprobar(t, buscador, app)
    assert t.manifiesto.websearch_resultado == "error"
    assert not avisos[-1]["ausentes"]
    assert not t.manifiesto.websearch_confirmado


def test_busca_toda_la_muestra_aunque_falte_la_primera(tmp_path):
    t, buscador, filas, consultas = caso(tmp_path, 500)
    filas.pop(0)
    resultado = verificar_batch(buscador, t)
    assert resultado.consulta_completa
    assert not resultado.confirmado
    assert resultado.encontradas == 24
    assert len(consultas) == 25


def test_un_batch_sin_numeros_validos_queda_sin_reenvio(app, tmp_path):
    t, buscador, _, consultas = caso(tmp_path, 1)
    t.manifiesto.registros[0].log_number = ""
    t.guardar()
    _, avisos = comprobar(t, buscador, app)
    assert not consultas
    assert t.manifiesto.websearch_resultado == "error"
    assert not avisos[-1]["ausentes"]


def test_no_revoca_un_batch_que_se_completa_durante_el_chequeo(app, tmp_path):
    t, buscador, _, _ = caso(tmp_path, 50)
    original = buscador.sesion.get
    cerrado = False

    def get(*args):
        nonlocal cerrado
        if not cerrado:
            actual = cargar(t.carpeta)
            actual.etapa("completar").marcar(EstadoEtapa.HECHA, "Cerrado durante la lectura")
            guardar(actual, t.carpeta)
            cerrado = True
        original(*args)
        return {"rows": []}

    buscador.sesion.get = get
    _, avisos = comprobar(t, buscador, app)
    assert not avisos[-1]["ausentes"]
    assert cargar(t.carpeta).etapa_hecha("completar")


def test_un_batch_confirmado_no_se_vuelve_a_consultar(app, tmp_path, monkeypatch):
    t, buscador, _, _ = caso(tmp_path, 50)
    comprobar(t, buscador, app)
    monkeypatch.setattr("app.gui.airvault_window.TrabajoAirVaultWorker", WorkerFalso)
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [t]
    ventana._estados = [estado_local(t)]
    ventana._pintar_lotes()
    # La tarjeta dice la confirmacion sola, y su ayuda una sola vez.
    assert ventana.lotes.item(0, 3).text() == "Confirmado en Web Search"
    ayuda = ventana.lotes.item(0).toolTip()
    assert ayuda.count("Web Search") == 1
    assert re.search(r"Confirmado el \d\d/\d\d/\d{4} a las \d\d:\d\d\.$", ayuda)
    ventana._buscar_websearch()
    assert ventana._worker_websearch is None
    assert "ya están confirmados" in ventana.resumen.text()
    ventana._iniciar_confirmacion([t], automatico=False)
    assert ventana._worker_websearch is None
    assert not ventana._subidos_por_confirmar_websearch()


def test_solo_volver_a_subir_borra_la_confirmacion(app, tmp_path):
    from app.airvault.flujo import reiniciar_subida, websearch_confirmacion_valida
    t, buscador, _, _ = caso(tmp_path, 50)
    comprobar(t, buscador, app)
    assert websearch_confirmacion_valida(cargar(t.carpeta))
    reiniciar_subida(t)
    final = cargar(t.carpeta)
    assert not final.websearch_confirmado
    assert not websearch_confirmacion_valida(final)
    assert not final.etapa_hecha("completar")


def test_la_resubida_espera_a_que_termine_el_worker(app, tmp_path, monkeypatch):
    t, buscador, filas, _ = caso(tmp_path, 50)
    filas.clear()
    _, avisos = comprobar(t, buscador, app)
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [t]
    ventana._estados = [estado_local(t)]
    worker = WorkerFalso("buscar_websearch", {"buscar_trabajos": [t]}, ventana)
    worker.start()
    ventana._worker_websearch = worker
    encoladas = []
    monkeypatch.setattr(ventana, "_encolar", lambda modo, trabajos, _texto: encoladas.append((modo, trabajos)))
    ventana._al_buscar_websearch(avisos[0])
    assert not encoladas
    assert ventana.lotes.item(0, 3).text() == "No encontrado en AirVault"
    ventana._al_buscar_websearch(avisos[-1])
    assert not encoladas
    worker.corriendo = False
    ventana._al_terminar_websearch()
    assert len(encoladas) == 1
    assert encoladas[0][0] == "recuperar_ausentes"
    assert encoladas[0][1][0].carpeta == t.carpeta


def test_cancelar_antes_del_reenvio_vacia_las_ausencias(app, tmp_path, monkeypatch):
    t, buscador, filas, _ = caso(tmp_path, 50)
    filas.clear()
    _, avisos = comprobar(t, buscador, app)
    ventana = AirVaultWindow(tmp_path)
    ventana._al_buscar_websearch(avisos[-1])
    ventana._cancelacion_solicitada = True
    monkeypatch.setattr(ventana, "_encolar", lambda *_: pytest.fail("No se reenvia al cancelar"))
    ventana._al_terminar_websearch()
    assert not ventana._websearch_resubidas


@pytest.mark.parametrize("cambio", ["contenido", "cancelado", "subida", "completado", "confirmado"])
def test_recuperacion_descarta_pruebas_que_ya_no_son_actuales(app, tmp_path, monkeypatch, cambio):
    t, buscador, filas, _ = caso(tmp_path, 50)
    filas.clear()
    _, avisos = comprobar(t, buscador, app)
    prueba = avisos[-1]["ausentes"][0]
    actual = cargar(t.carpeta)
    if cambio == "contenido":
        actual.registros[0].log_number = "2999999"
    elif cambio == "cancelado":
        actual.cancelado = True
    elif cambio in ("subida", "completado"):
        actual.etapa("subir" if cambio == "subida" else "completar").marcar(EstadoEtapa.HECHA)
    else:
        actual.websearch_resultado = "encontrado"
        actual.websearch_revision = "9999-01-01"
    guardar(actual, t.carpeta)
    worker = TrabajoAirVaultWorker("recuperar_ausentes", {"pendientes_subida": [prueba], "buscador": buscador})
    monkeypatch.setattr(worker, "_conectar", lambda: pytest.fail("La prueba ya no autoriza subir"))
    monkeypatch.setattr(worker, "_enviar", lambda *_: pytest.fail("No debe reenviar"))
    worker._recuperar_ausentes()


def test_recuperacion_usa_el_camino_de_subida_con_preflight(app, tmp_path, monkeypatch):
    from tests.airvault_fake import ClienteFalso
    t, buscador, filas, _ = caso(tmp_path, 50)
    filas.clear()
    _, avisos = comprobar(t, buscador, app)
    prueba = avisos[-1]["ausentes"][0]
    worker = TrabajoAirVaultWorker("recuperar_ausentes", {"pendientes_subida": [prueba], "buscador": buscador})
    cliente, envios = ClienteFalso(), []
    monkeypatch.setattr(worker, "_conectar", lambda: cliente)
    monkeypatch.setattr(worker, "_enviar", lambda trabajos, remoto: envios.append((trabajos, remoto, trabajos[0]._duplicado_permitido)))
    worker._recuperar_ausentes()
    assert envios[0][1] is cliente
    assert [str(t.carpeta) for t in envios[0][0]] == worker.estado["forzados"]
    assert envios[0][2]
    assert not envios[0][0][0]._duplicado_permitido


def test_url_sso_de_airvault_no_se_confunde_con_la_pagina_de_microsoft():
    from app.airvault.config import AirVaultConfig
    config = AirVaultConfig()
    assert not SesionAirVault._pide_login("Web Search", config.url_sso)
    assert SesionAirVault._pide_login("", "https://login.microsoftonline.com/tenant/wsfed")
    assert SesionAirVault._pide_login("", config.url("/signin2/"))


def test_fin_no_cancela_el_chequeo_completo_automatico(app, tmp_path):
    t, buscador, _, _ = caso(tmp_path, 50)
    comprobar(t, buscador, app)
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [t]
    ventana._estados = [estado_local(t)]
    assert ventana._estados[0].estado == COMPLETADO
    worker = WorkerFalso("buscar_websearch", {"confirmacion_automatica": True, "revisar_todos": True}, ventana)
    worker.start()
    ventana._worker_websearch = worker
    ventana._fin_pendiente = True
    assert not ventana._anunciar_fin()
    assert worker.isRunning()


def test_preflight_completo_detecta_publicacion_fuera_de_la_muestra(app, tmp_path, monkeypatch):
    from tests.airvault_fake import ClienteFalso
    t, buscador, filas, consultas = caso(tmp_path, 500)
    filas[:] = [filas[271]]
    _, avisos = comprobar(t, buscador, app)
    assert t.manifiesto.websearch_resultado == "ausente"
    assert len(consultas) == 25
    worker = TrabajoAirVaultWorker("recuperar_ausentes", {
        "pendientes_subida": avisos[-1]["ausentes"], "buscador": buscador,
    })
    monkeypatch.setattr(worker, "_conectar", lambda: ClienteFalso())
    monkeypatch.setattr(worker, "_enviar", lambda *_: pytest.fail("Ya hay una bitacora publicada"))
    fallos = []
    worker.subido.connect(lambda datos: fallos.extend(datos.get("fallos", [])))
    worker._recuperar_ausentes()
    assert len(consultas) == 525
    assert len(fallos) == 1
    assert "1 de 500" in fallos[0][1]


def test_recupera_pdf_previamente_reservado_y_autorizacion_no_sobrevive(app, tmp_path, monkeypatch):
    from app.airvault import envios_exactos
    from app.airvault.flujo import carpeta_del_libro
    from app.airvault.uploader import SubidorQuickUpload
    from tests.airvault_fake import ClienteFalso
    from tests.test_airvault_envios_exactos import subidor
    from tests.test_airvault_sin_duplicar import _trabajo
    import pymupdf

    t = _trabajo(tmp_path)
    archivo = t.manifiesto.pdf_origen
    with pymupdf.open() as documento:
        for _ in t.manifiesto.registros:
            documento.new_page()
        documento.save(archivo)
    raiz = carpeta_del_libro(t)
    envios_exactos.enviar(raiz, t, archivo, subidor([]), {})
    assert envios_exactos.motivo(raiz, archivo, t.config)
    t.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    t.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
    t.guardar()
    buscador = SimpleNamespace(sesion=SimpleNamespace(), config=t.config,
                              consultar_numero=lambda *_: {"rows": []})
    _, avisos = comprobar(t, buscador, app)
    worker = TrabajoAirVaultWorker("recuperar_ausentes", {
        "pendientes_subida": avisos[-1]["ausentes"], "buscador": buscador,
        "raiz": tmp_path, "sesion": SimpleNamespace(cancelada=False),
    })
    llamadas = []
    def subir(_self, archivo, valores, avisar=None):
        return subidor(llamadas).subir(archivo, valores, avisar)

    monkeypatch.setattr(SubidorQuickUpload, "subir", subir)
    monkeypatch.setattr(worker, "_conectar", lambda: ClienteFalso())

    def enviar(trabajos, cliente):
        from app.airvault.flujo import reiniciar_subida
        for actual in trabajos:
            reiniciar_subida(actual)
            actual.subir(worker.estado["sesion"])

    monkeypatch.setattr(worker, "_enviar", enviar)
    worker._recuperar_ausentes()
    assert len(llamadas) == 1
    final = Trabajo.cargar(t.config, t.carpeta)
    assert final.manifiesto.etapa_hecha("subir")
    assert not final._duplicado_permitido
    with pytest.raises(envios_exactos.CargaRepetida):
        envios_exactos.enviar(raiz, final, archivo, subidor(llamadas), {})


def test_reenvio_no_conserva_autorizacion_si_falla_antes_de_cargar(app, tmp_path, monkeypatch):
    from tests.airvault_fake import ClienteFalso
    t, buscador, filas, _ = caso(tmp_path, 50)
    filas.clear()
    _, avisos = comprobar(t, buscador, app)
    worker = TrabajoAirVaultWorker("recuperar_ausentes", {
        "pendientes_subida": avisos[-1]["ausentes"], "buscador": buscador,
    })
    monkeypatch.setattr(worker, "_conectar", lambda: ClienteFalso())
    usados = []

    def fallar(trabajos, _cliente):
        usados.extend(trabajos)
        assert trabajos[0]._duplicado_permitido
        raise RuntimeError("No esta el PDF")

    monkeypatch.setattr(worker, "_enviar", fallar)
    with pytest.raises(RuntimeError, match="No esta el PDF"):
        worker._recuperar_ausentes()
    assert not usados[0]._duplicado_permitido


def test_el_indexado_reutiliza_el_login_que_renovo_el_chequeo(app, tmp_path, monkeypatch):
    from app.airvault.config import AirVaultConfig
    config = AirVaultConfig()
    sesion = SesionAirVault(config)
    sesion.usar_cookie("FedAuth=prueba")
    ventana = AirVaultWindow(tmp_path)
    ventana._estado_websearch["sesion"] = sesion
    lanzados = []
    monkeypatch.setattr(ventana, "_lanzar", lambda modo, estado: lanzados.append((modo, dict(estado))))
    t, _, _, _ = caso(tmp_path)
    ventana._ejecutar_accion("recuperar_ausentes", [t])
    nueva = lanzados[0][1]["sesion"]
    assert nueva is not sesion
    assert nueva.http.cookies.get("FedAuth") == "prueba"
    assert "cliente" not in lanzados[0][1]
    worker = TrabajoAirVaultWorker("recuperar_ausentes", lanzados[0][1])
    monkeypatch.setattr("app.airvault.session.abrir_sesion", lambda *_a, **_k: pytest.fail("La sesion ya se abrio"))
    monkeypatch.setattr("app.airvault.session.comprobar_o_renovar", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "_detectar_pendientes", lambda: None)
    monkeypatch.setattr(worker, "_preparar_buscador", lambda: None)
    assert worker._conectar().sesion is nueva
    nueva.http.close()
    sesion.http.close()


def test_chequeo_con_errores_no_anuncia_todo_completado(app, tmp_path):
    t, buscador, _, _ = caso(tmp_path, 50)
    comprobar(t, buscador, app)
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [t]
    ventana._estados = [estado_local(t)]
    ventana._al_buscar_websearch({"resultados": [], "terminado": True, "errores": 1})
    ventana._resultado_fallido = False
    assert not ventana._anunciar_fin()
    assert ventana._chequeo_incompleto
