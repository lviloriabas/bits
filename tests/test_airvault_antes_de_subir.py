"""Antes de cada carga se pregunta a Web Search si sus bitacoras ya estan publicadas.

Otro equipo volvio a subir ejecuciones que este ya habia completado: la
pregunta a Web Search, la unica que ve un batch completado, iba por una ruta
que AirVault dejo de contestar y, con la casilla de duplicados apagada, ni
se hacia.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.airvault import antes_de_subir
from app.airvault.antes_de_subir import (ESPERAR, NO_SUBIR, SUBIR, ConsultaPorPaginas,
                                         EnWebSearch, decidir)
from app.airvault.flujo import Trabajo, duplicado_bloquea, subir_partes
from app.airvault.model import Etapa
from tests.airvault_fake import ClienteFalso
from tests.test_airvault_copias_en_index import trabajo


def pendiente(raiz, **config):
    """Un batch de la barra que nunca se subio, con diez bitacoras."""
    t = trabajo(raiz, batch_id=None, verificado=False)
    t.manifiesto.etapas["subir"] = Etapa()
    if config:
        t.config = replace(t.config, **config)
    t.guardar()
    return t


def respuestas(publicadas=(), sin_respuesta=()):
    """Web Search falso: dice que si a las publicadas y no sabe las otras."""
    pedidas = []

    def consultar(numeros):
        pedidas.append(list(numeros))
        return {
            numero: None if numero in sin_respuesta else numero in publicadas
            for numero in numeros
        }

    consultar.pedidas = pedidas
    return consultar


def subir(t, monkeypatch, consultar, forzados=()):
    """Corre la subida; la carga real se reemplaza por un fallo reconocible."""
    intentos = []

    def subida(self, *_a, **_k):
        intentos.append(self.manifiesto.nombre_batch)
        raise RuntimeError("subida simulada")

    monkeypatch.setattr(Trabajo, "subir", subida)
    avisos = []
    fallos = subir_partes(
        [t], SimpleNamespace(cancelada=False), cliente=ClienteFalso(),
        avisar=lambda texto, *_a: avisos.append(texto), dormir=lambda _s: None,
        forzados=forzados, consultar_websearch=consultar,
    )
    return intentos, fallos, avisos


def numeros(t):
    return [r.log_number for r in t.manifiesto.registros]


# ── la decision ──────────────────────────────────────────────────────


def revision(publicadas, ausentes, sin_respuesta=0):
    return EnWebSearch(
        publicadas=tuple(f"p{i}" for i in range(publicadas)),
        ausentes=tuple(f"a{i}" for i in range(ausentes)),
        sin_respuesta=tuple(f"s{i}" for i in range(sin_respuesta)),
    )


@pytest.mark.parametrize("publicadas, ausentes, sin_respuesta, accion, entera", [
    (30, 0, 0, NO_SUBIR, True),
    (27, 3, 0, NO_SUBIR, True),       # 90 %: quien completa corrige numeros
    (10, 0, 20, NO_SUBIR, True),      # diez publicadas bastan para pararse
    (26, 4, 0, SUBIR, False),         # bitacoras sueltas: decide la casilla
    (0, 30, 0, SUBIR, False),
    (0, 4, 26, SUBIR, False),         # un batch nuevo se sabe en la primera tanda
    (9, 0, 21, ESPERAR, False),       # todavia no alcanza para decir nada
    (2, 3, 25, ESPERAR, False),
    (0, 0, 30, ESPERAR, False),       # Web Search no contesto
    (2, 0, 0, NO_SUBIR, True),        # un batch chico se decide entero
    (1, 1, 0, SUBIR, False),
    (0, 0, 0, SUBIR, False),          # sin numeros no hay pregunta
])
def test_la_regla_de_la_carga_entera(publicadas, ausentes, sin_respuesta, accion, entera):
    decision = decidir(revision(publicadas, ausentes, sin_respuesta), 100)
    assert decision.accion == accion
    assert decision.carga_publicada is entera


def test_las_sueltas_las_decide_el_porcentaje_de_la_casilla():
    assert decidir(revision(3, 27), 5).motivo
    assert not decidir(revision(1, 29), 5).motivo


def test_la_orden_a_mano_no_espera_a_web_search():
    assert decidir(revision(0, 0, 30), 5, orden_a_mano=True).accion == SUBIR
    assert decidir(revision(30, 0), 5, orden_a_mano=True).accion == NO_SUBIR


# ── la subida ────────────────────────────────────────────────────────


def test_un_batch_ya_publicado_no_se_sube_aunque_la_casilla_este_apagada(tmp_path, monkeypatch):
    t = pendiente(tmp_path, detener_por_duplicados=False)
    consultar = respuestas(publicadas=numeros(t))

    intentos, fallos, avisos = subir(t, monkeypatch, consultar)

    assert intentos == []
    (fallido, motivo), = fallos
    assert fallido is t and "la carga entera ya está en AirVault" in motivo
    assert "Web Search ya tiene publicadas 10 de las 10" in t.manifiesto.posible_duplicado
    assert t.manifiesto.duplicado_exacto and duplicado_bloquea(t)
    assert any("no se sube" in aviso for aviso in avisos)


def test_lo_que_web_search_no_tiene_se_sube_y_el_registro_lo_dice(tmp_path, monkeypatch):
    t = pendiente(tmp_path)
    consultar = respuestas()

    intentos, _fallos, avisos = subir(t, monkeypatch, consultar)

    assert intentos == [t.manifiesto.nombre_batch]
    # Un batch nuevo se sabe en la primera tanda.
    assert len(consultar.pedidas) == 1 and len(consultar.pedidas[0]) == antes_de_subir.TANDA
    assert any("Web Search no lo tiene publicado (0 de 8" in aviso for aviso in avisos)
    assert not t.manifiesto.posible_duplicado


def test_sin_respuesta_de_web_search_la_carga_espera_a_la_revision_siguiente(tmp_path, monkeypatch):
    t = pendiente(tmp_path)

    intentos, fallos, _avisos = subir(t, monkeypatch, respuestas(sin_respuesta=numeros(t)))

    assert intentos == []
    (_fallido, motivo), = fallos
    assert motivo.startswith("No se subió todavía: Web Search no contestó")
    # No hay prueba de duplicado: no se marca.
    assert not t.manifiesto.posible_duplicado


def test_la_orden_a_mano_sigue_si_web_search_no_contesta(tmp_path, monkeypatch):
    t = pendiente(tmp_path)

    intentos, _fallos, _avisos = subir(
        t, monkeypatch, respuestas(sin_respuesta=numeros(t)), forzados=[str(t.carpeta)],
    )

    assert intentos == [t.manifiesto.nombre_batch]


def test_la_orden_a_mano_tampoco_sube_un_batch_ya_publicado(tmp_path, monkeypatch):
    t = pendiente(tmp_path)

    intentos, fallos, _avisos = subir(
        t, monkeypatch, respuestas(publicadas=numeros(t)), forzados=[str(t.carpeta)],
    )

    assert intentos == [] and fallos


@pytest.mark.parametrize("detener, se_sube", [(False, True), (True, False)])
def test_unas_pocas_repetidas_las_sigue_decidiendo_la_casilla(tmp_path, monkeypatch, detener, se_sube):
    t = pendiente(tmp_path, detener_por_duplicados=detener, porcentaje_duplicados=5)

    intentos, _fallos, avisos = subir(t, monkeypatch, respuestas(publicadas=numeros(t)[:2]))

    assert bool(intentos) is se_sube
    assert "Web Search ya tiene publicadas 2 de las 8" in t.manifiesto.posible_duplicado
    assert not t.manifiesto.duplicado_exacto
    assert any("Posible duplicado" in aviso for aviso in avisos)


def test_lo_que_una_persona_autorizo_no_se_vuelve_a_preguntar(tmp_path, monkeypatch):
    t = pendiente(tmp_path)
    t._duplicado_permitido = True
    consultar = respuestas(publicadas=numeros(t))

    intentos, _fallos, _avisos = subir(t, monkeypatch, consultar, forzados=[str(t.carpeta)])

    assert intentos == [t.manifiesto.nombre_batch]
    assert consultar.pedidas == []


def test_se_pregunta_por_tandas_y_se_para_cuando_esta_claro(tmp_path):
    t = trabajo(tmp_path, cantidad=200)
    todas = antes_de_subir.muestra(t)
    consultar = respuestas(publicadas=todas)

    revision = antes_de_subir.en_web_search(t, consultar)

    # Con dos tandas ya hay diez publicadas sin ninguna ausente.
    assert len(consultar.pedidas) == 2 and revision.carga_publicada
    # Cada tanda va de principio a fin del batch, no son las primeras.
    primera = consultar.pedidas[0]
    assert todas.index(primera[-1]) > len(todas) // 2


def test_no_se_espera_a_web_search_mas_del_presupuesto(tmp_path):
    t = trabajo(tmp_path, cantidad=200)
    reloj = [0.0]

    def lenta(numeros):
        reloj[0] += antes_de_subir.PRESUPUESTO_S
        return {numero: None for numero in numeros}

    revision = antes_de_subir.en_web_search(t, lenta, reloj=lambda: reloj[0])

    assert reloj[0] == antes_de_subir.PRESUPUESTO_S
    assert decidir(revision, 5).accion == ESPERAR


def test_la_muestra_se_reparte_por_todo_el_batch(tmp_path):
    t = trabajo(tmp_path, cantidad=200)

    muestra = antes_de_subir.muestra(t)

    assert len(muestra) == antes_de_subir.MUESTRA
    assert muestra[0] == "2300000" and muestra[-1] == "2300199"


# ── la consulta por la pagina de Web Search ─────────────────────────


class LectorFalso:
    def __init__(self, publicadas, rotas, cerrados):
        self.publicadas, self.rotas, self.cerrados = publicadas, rotas, cerrados

    def consultar_numero(self, numero, _presupuesto):
        if numero in self.rotas:
            raise RuntimeError("la pagina no cargo")
        return {"rows": [{"LogNo": numero}] if numero in self.publicadas else []}

    def cerrar(self):
        self.cerrados.append(self)


def test_la_consulta_lee_varias_pestanas_y_deja_sin_saber_lo_que_no_cargo(monkeypatch):
    cerrados = []
    acceso = SimpleNamespace(
        crear_lector=lambda: LectorFalso({"1000001", "1000003"}, {"1000004"}, cerrados),
    )
    consulta = ConsultaPorPaginas(config=None, sesion=None)
    monkeypatch.setattr(consulta, "_abrir", lambda: acceso)

    contestado = consulta(["1000001", "1000002", "1000003", "1000004", "1000005"])

    assert contestado == {
        "1000001": True, "1000002": False, "1000003": True,
        "1000004": None, "1000005": False,
    }
    assert len(cerrados) == antes_de_subir.PESTANAS


def test_si_web_search_no_abre_todo_queda_sin_saber(monkeypatch):
    consulta = ConsultaPorPaginas(config=None, sesion=None)
    monkeypatch.setattr(consulta, "_abrir", lambda: None)

    assert consulta(["1000001", "1000002"]) == {"1000001": None, "1000002": None}


def test_la_ventana_solo_pregunta_con_una_sesion_de_airvault():
    from app.gui.airvault_window import TrabajoAirVaultWorker

    worker = TrabajoAirVaultWorker("subir", {"trabajos": []})

    assert worker._consulta_antes_de_subir(SimpleNamespace(cancelada=False)) is None
    assert worker._consulta_antes_de_subir(None) is None
