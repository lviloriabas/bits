"""Una carga que AirVault acepto y no publico se retoma sola, con memoria y con tope.

Antes se quedaba en «No encontrado en AirVault» hasta que alguien la mandara
a mano. Ahora la revision de la cola la deja para retomar: se buscan sus
bitacoras en Web Search y, si tampoco estan publicadas, vuelve a Quick
Upload. Cada paso queda en el historial del manifiesto, que es lo que
permite decidir sin repetir lo que no hace falta.
"""

from datetime import datetime, timedelta

import pytest

from app.airvault.config import AirVaultConfig
from app.airvault.flujo import (NO_ENCONTRADO, REENVIOS_AUTOMATICOS_MAXIMOS,
                                EstadoParte, Trabajo,
                                _reiniciar_subida_ausente, anotar_reenvio,
                                ausencia_vigente, comprobar_partes,
                                detalle_de_no_encontrada, estado_local,
                                partes_por_recuperar, partes_por_subir,
                                reenvios_automaticos, se_reenvia_sola)
from app.airvault.model import MAXIMO_HISTORIAL, EstadoEtapa, Manifiesto
from app.gui import airvault_window as modulo
from tests.test_airvault_carga_vieja import envejecer
from tests.test_airvault_comprobar import corrida, trabajo_subido
from tests.test_airvault_trabajo import SubidorFalso
from tests.test_airvault_trabajo import corrida as corrida_con_pdf


def perdida(tmp_path):
    """Una carga vieja que la cola de AirVault no tiene con ningun nombre."""
    trabajo, cliente = trabajo_subido(tmp_path)
    envejecer(trabajo)
    cliente.lotes = []
    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == NO_ENCONTRADO
    return trabajo, cliente, parte


# ── el historial ────────────────────────────────────────────────────

def test_el_historial_guarda_los_hechos_en_orden_y_con_tope():
    manifiesto = Manifiesto(job_id="x")
    manifiesto.anotar("subida", "aceptada", "entrega.pdf")
    manifiesto.anotar("reenvio", "automatico")
    manifiesto.anotar("reenvio", "manual")

    assert [evento.paso for evento in manifiesto.historial] == ["subida", "reenvio", "reenvio"]
    assert all(evento.momento for evento in manifiesto.historial)
    assert len(manifiesto.eventos("reenvio")) == 2
    assert len(manifiesto.eventos("reenvio", "automatico")) == 1

    for numero in range(MAXIMO_HISTORIAL + 20):
        manifiesto.anotar("subida", "rechazada", str(numero))
    assert len(manifiesto.historial) == MAXIMO_HISTORIAL
    # Se van los mas antiguos.
    assert manifiesto.historial[-1].detalle == str(MAXIMO_HISTORIAL + 19)


def test_un_manifiesto_anterior_se_lee_sin_historial(tmp_path):
    trabajo, _cliente = trabajo_subido(tmp_path)
    datos = trabajo.manifiesto.model_dump(mode="json")
    datos.pop("historial")

    assert Manifiesto.model_validate(datos).historial == []


def test_cada_subida_deja_como_termino(tmp_path, monkeypatch):
    from app.airvault import uploader
    from app.airvault.uploader import ResultadoSubida

    csv = corrida_con_pdf(tmp_path)
    monkeypatch.setattr(uploader, "SubidorQuickUpload", SubidorFalso())
    aceptada = Trabajo.preparar(AirVaultConfig(), tmp_path / "uno", csv, "DP | UNO")
    aceptada.subir(object())
    assert [e.resultado for e in aceptada.manifiesto.eventos("subida")] == ["aceptada"]
    # Lo anotado esta en disco, no solo en memoria.
    releido = Trabajo.cargar(AirVaultConfig(), aceptada.carpeta)
    assert [e.resultado for e in releido.manifiesto.eventos("subida")] == ["aceptada"]

    class Rechaza(SubidorFalso):
        def subir(self, ruta, valores, avisar=None):
            return ResultadoSubida(str(ruta), False, "AirVault lo rechazo")

    monkeypatch.setattr(uploader, "SubidorQuickUpload", Rechaza())
    rechazada = Trabajo.preparar(AirVaultConfig(), tmp_path / "dos", csv, "DP | DOS")
    rechazada._duplicado_permitido = True  # el mismo PDF de arriba
    with pytest.raises(Exception):
        rechazada.subir(object())
    assert [e.resultado for e in rechazada.manifiesto.eventos("subida")] == ["rechazada"]
    assert rechazada.manifiesto.etapa("subir").estado is EstadoEtapa.ERROR

    class SeCorta(SubidorFalso):
        def subir(self, ruta, valores, avisar=None):
            raise ConnectionError("se fue la red")

    monkeypatch.setattr(uploader, "SubidorQuickUpload", SeCorta())
    cortada = Trabajo.preparar(AirVaultConfig(), tmp_path / "tres", csv, "DP | TRES")
    cortada._duplicado_permitido = True
    with pytest.raises(ConnectionError):
        cortada.subir(object())
    assert [e.resultado for e in cortada.manifiesto.eventos("subida")] == ["interrumpida"]
    # Los trozos pudieron llegar: sigue «en curso» y se busca en la cola.
    assert cortada.manifiesto.etapa("subir").estado is EstadoEtapa.EN_CURSO


def test_encontrar_el_batch_se_anota_una_vez(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)

    comprobar_partes([trabajo], cliente)
    comprobar_partes([trabajo], cliente)

    assert trabajo.manifiesto.batch_id == "003SRO"
    hallazgos = trabajo.manifiesto.eventos("deteccion", "encontrado")
    assert len(hallazgos) == 1
    assert "003SRO" in hallazgos[0].detalle


def test_darla_por_no_encontrada_se_anota_una_vez_y_conserva_su_fecha(tmp_path):
    trabajo, cliente, _parte = perdida(tmp_path)
    desde = trabajo.manifiesto.no_encontrado_desde

    comprobar_partes([trabajo], cliente)

    assert trabajo.manifiesto.no_encontrado_desde == desde
    assert len(trabajo.manifiesto.eventos("deteccion", "no_encontrado")) == 1


# ── la decision ─────────────────────────────────────────────────────

def test_la_carga_que_nunca_aparecio_se_deja_para_retomar_sola(tmp_path):
    trabajo, _cliente, parte = perdida(tmp_path)

    assert se_reenvia_sola(parte)
    assert partes_por_recuperar([parte]) == [trabajo]
    # No es lo mismo que lo que nunca se subio: esa lista sigue aparte.
    assert partes_por_subir([parte]) == []
    assert "se vuelve a subir" in parte.detalle
    assert estado_local(trabajo).detalle == parte.detalle


def test_una_carga_recien_subida_espera_y_no_se_retoma(tmp_path):
    """Que AirVault aun no la publique es lo normal: se sigue esperando."""
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = []

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado != NO_ENCONTRADO
    assert not se_reenvia_sola(parte)
    assert partes_por_recuperar([parte]) == []
    assert not trabajo.manifiesto.eventos("deteccion", "no_encontrado")


@pytest.mark.parametrize("cambio", [
    lambda m: setattr(m, "cancelado", True),
    # Tuvo batch y dejo de estar en la cola: lo completo o lo borro alguien.
    lambda m: setattr(m, "batch_id", "003YA"),
    lambda m: setattr(m, "mezcla_pendiente", "003MEZCLA"),
    lambda m: setattr(m, "no_encontrado_desde", ""),
], ids=["cancelado", "tuvo_batch", "mezcla_pendiente", "sin_plazo_vencido"])
def test_lo_que_no_se_retoma_solo(tmp_path, cambio):
    trabajo, _cliente, _parte = perdida(tmp_path)
    cambio(trabajo.manifiesto)

    parte = EstadoParte(trabajo, NO_ENCONTRADO)

    assert not se_reenvia_sola(parte)
    assert partes_por_recuperar([parte]) == []


def test_tras_dos_reenvios_automaticos_la_decision_pasa_a_una_persona(tmp_path):
    trabajo, _cliente, _parte = perdida(tmp_path)
    assert REENVIOS_AUTOMATICOS_MAXIMOS == 2

    # Los que alguien ordeno no gastan el tope.
    anotar_reenvio(trabajo, automatico=False, motivo="orden expresa")
    anotar_reenvio(trabajo, automatico=True)
    assert reenvios_automaticos(trabajo) == 1
    assert se_reenvia_sola(EstadoParte(trabajo, NO_ENCONTRADO))

    anotar_reenvio(trabajo, automatico=True)
    assert reenvios_automaticos(trabajo) == 2
    assert not se_reenvia_sola(EstadoParte(trabajo, NO_ENCONTRADO))
    detalle = detalle_de_no_encontrada(trabajo)
    assert "Ya se volvió a subir 2 veces" in detalle
    assert "desde sus acciones" in detalle


def test_reiniciar_la_subida_no_borra_lo_que_ya_se_hizo(tmp_path):
    """Sin esa memoria cada carga perdida pareceria la primera."""
    trabajo, _cliente, _parte = perdida(tmp_path)
    anotar_reenvio(trabajo, automatico=True)

    _reiniciar_subida_ausente(trabajo)

    assert not trabajo.manifiesto.no_encontrado_desde
    assert not trabajo.manifiesto.etapa_hecha("subir")
    releido = Trabajo.cargar(AirVaultConfig(), trabajo.carpeta)
    assert reenvios_automaticos(releido) == 1
    assert releido.manifiesto.eventos("deteccion", "no_encontrado")


def test_la_ausencia_de_web_search_vale_mientras_es_reciente(tmp_path):
    trabajo, _cliente, _parte = perdida(tmp_path)
    manifiesto = trabajo.manifiesto
    assert not ausencia_vigente(trabajo)

    manifiesto.websearch_resultado = "ausente"
    manifiesto.websearch_revision = datetime.now().isoformat(timespec="microseconds")
    assert ausencia_vigente(trabajo)

    manifiesto.websearch_revision = (
        datetime.now() - timedelta(hours=2)
    ).isoformat(timespec="microseconds")
    assert not ausencia_vigente(trabajo)

    manifiesto.websearch_revision = datetime.now().isoformat(timespec="microseconds")
    manifiesto.websearch_resultado = "error"
    assert not ausencia_vigente(trabajo)


# ── la ventana ──────────────────────────────────────────────────────

@pytest.fixture
def ventana(app, tmp_path):
    return modulo.AirVaultWindow(tmp_path)


def revisada(ventana, tmp_path):
    """La ventana tras una revision que deja una carga como no publicada."""
    trabajo, _cliente, parte = perdida(tmp_path)
    ventana._trabajos = [trabajo]
    ventana._estados = [parte]
    ventana._estado["trabajos"] = [trabajo]
    ventana.auto_check.setChecked(True)
    ventana._al_comprobar({"estados": [parte], "planes": {}, "partes": []})
    return trabajo, parte


def lineas(ventana) -> str:
    return "\n".join(
        ventana.bitacora.item(fila).text() for fila in range(ventana.bitacora.count())
    )


def test_la_revision_deja_la_carga_perdida_para_retomar(ventana, tmp_path):
    trabajo, _parte = revisada(ventana, tmp_path)

    assert ventana._perdidos_por_recuperar == [trabajo]
    assert "Se comprueba en Web Search" in ventana.resumen.text()
    # Ya no se le pide nada a quien mira la ventana.
    assert "No se vuelve a mandar solo" not in ventana.resumen.text()
    assert not ventana._subir_al_terminar


def test_sin_revision_periodica_ni_orden_no_se_retoma_nada(ventana, tmp_path):
    trabajo, _cliente, parte = perdida(tmp_path)
    ventana._trabajos = [trabajo]
    ventana._estados = [parte]
    ventana.auto_check.setChecked(False)
    ventana._cadena_manual = False

    ventana._al_comprobar({"estados": [parte], "planes": {}, "partes": []})

    assert ventana._perdidos_por_recuperar == []
    assert "Subir a AirVault ahora" in ventana.resumen.text()


def test_revisar_filas_concretas_no_retoma_cargas_perdidas(ventana, tmp_path):
    trabajo, _cliente, parte = perdida(tmp_path)
    ventana._trabajos = [trabajo]
    ventana._estados = [parte]

    ventana._al_comprobar({
        "estados": [parte], "planes": {}, "partes": [], "acotado": True,
    })

    assert ventana._perdidos_por_recuperar == []


def test_antes_de_reenviar_se_buscan_sus_bitacoras_en_web_search(ventana, tmp_path, monkeypatch):
    trabajo, _parte = revisada(ventana, tmp_path)
    consultas, lanzados = [], []
    monkeypatch.setattr(
        ventana, "_iniciar_confirmacion",
        lambda trabajos, **opciones: consultas.append((list(trabajos), opciones)),
    )
    monkeypatch.setattr(ventana, "_lanzar", lambda modo, estado: lanzados.append(modo))

    assert ventana._recuperar_perdidos() is False

    assert consultas == [([trabajo], {"automatico": True})]
    assert lanzados == []
    assert str(trabajo.carpeta) in ventana._reenvios_automaticos
    assert "se comprueba en Web Search antes de volver a subirlo" in lineas(ventana)
    # La lista se consume: no queda nada que retomar hasta otra revision.
    assert ventana._perdidos_por_recuperar == []


def test_no_se_pregunta_otra_vez_por_la_misma_carga_en_cada_vuelta(ventana, tmp_path, monkeypatch):
    """La cola se revisa cada pocos minutos; Web Search no."""
    trabajo, parte = revisada(ventana, tmp_path)
    consultas = []
    monkeypatch.setattr(
        ventana, "_iniciar_confirmacion",
        lambda trabajos, **_opciones: consultas.append(list(trabajos)),
    )
    ventana._recuperar_perdidos()

    ventana._al_comprobar({"estados": [parte], "planes": {}, "partes": []})
    ventana._recuperar_perdidos()
    assert consultas == [[trabajo]]

    # Pasado el espacio entre consultas, si.
    clave = str(trabajo.carpeta)
    ventana._chequeo_de_perdidos[clave] -= modulo.MINUTOS_ENTRE_CHEQUEOS_DE_PERDIDOS * 60
    ventana._al_comprobar({"estados": [parte], "planes": {}, "partes": []})
    ventana._recuperar_perdidos()
    assert consultas == [[trabajo], [trabajo]]


def test_con_la_ausencia_reciente_se_reenvia_sin_volver_a_preguntar(ventana, tmp_path, monkeypatch):
    trabajo, _cliente, parte = perdida(tmp_path)
    trabajo.manifiesto.websearch_resultado = "ausente"
    trabajo.manifiesto.websearch_revision = datetime.now().isoformat(timespec="microseconds")
    ventana._trabajos = [trabajo]
    ventana._estados = [parte]
    ventana._estado["trabajos"] = [trabajo]
    ventana._al_comprobar({"estados": [parte], "planes": {}, "partes": []})
    lanzados = []
    monkeypatch.setattr(
        ventana, "_iniciar_confirmacion",
        lambda *_a, **_k: pytest.fail("La ausencia ya estaba comprobada"),
    )
    monkeypatch.setattr(
        ventana, "_lanzar", lambda modo, estado: lanzados.append((modo, dict(estado))),
    )

    assert ventana._recuperar_perdidos() is True

    (modo, estado), = lanzados
    assert modo == "recuperar_ausentes"
    assert estado["pendientes_subida"] == [trabajo]
    # Queda dicho que lo retoma el programa: es lo que cuenta para el tope.
    assert estado["reenvios_automaticos"] == {str(trabajo.carpeta)}
    assert "Se vuelve a subir 1 batch que AirVault no publicó" in lineas(ventana)


def test_la_cadena_retoma_lo_perdido_al_terminar_lo_demas(ventana, tmp_path, monkeypatch):
    _trabajo, _parte = revisada(ventana, tmp_path)
    pasos = []
    monkeypatch.setattr(ventana, "_recuperar_perdidos", lambda: pasos.append("perdidos") or True)
    monkeypatch.setattr(ventana, "_siguiente_de_la_cola", lambda: pasos.append("cola") or False)

    ventana._al_terminar()

    # Ocupo el hilo: lo que venia detras espera su turno.
    assert pasos == ["perdidos"]


def test_pausar_suelta_lo_que_quedaba_por_retomar(ventana, tmp_path):
    revisada(ventana, tmp_path)

    ventana._detener_cadena()

    assert ventana._perdidos_por_recuperar == []


def test_el_chequeo_de_una_carga_perdida_que_falla_no_deja_el_proceso_fallido(ventana, tmp_path):
    from types import SimpleNamespace

    trabajo, _parte = revisada(ventana, tmp_path)
    ventana._reenvios_automaticos.add(str(trabajo.carpeta))
    ventana._worker_websearch = SimpleNamespace(estado={
        "buscar_trabajos": [trabajo], "confirmacion_automatica": True,
    })

    ventana._al_fallar_websearch("Web Search no respondió")

    assert not ventana._chequeo_incompleto
    assert not ventana._resultado_fallido
    # Sigue anotado para que el chequeo se repita solo.
    assert ventana._chequeo_fallido
    ventana._worker_websearch = None


def test_el_reenvio_automatico_queda_anotado_y_respeta_el_tope(app, tmp_path, monkeypatch):
    from tests.airvault_fake import ClienteFalso

    trabajo, _cliente, _parte = perdida(tmp_path)
    clave = str(trabajo.carpeta)
    worker = modulo.TrabajoAirVaultWorker("recuperar_ausentes", {
        "reenvios_automaticos": {clave},
    })

    autorizados, fallos = worker._revisar_reenvios([trabajo], ClienteFalso())

    assert autorizados == [trabajo] and not fallos
    assert reenvios_automaticos(trabajo) == 1
    assert trabajo._duplicado_permitido

    anotar_reenvio(trabajo, automatico=True)
    autorizados, fallos = worker._revisar_reenvios([trabajo], ClienteFalso())
    assert not autorizados
    assert "Ya se volvió a subir 2 veces" in fallos[0][1]
    assert reenvios_automaticos(trabajo) == 2

    # La orden de una persona no tiene tope y queda anotada como suya.
    manual = modulo.TrabajoAirVaultWorker("recuperar_ausentes", {})
    autorizados, fallos = manual._revisar_reenvios([trabajo], ClienteFalso())
    assert autorizados == [trabajo] and not fallos
    assert len(trabajo.manifiesto.eventos("reenvio", "manual")) == 1
    assert reenvios_automaticos(trabajo) == 2
