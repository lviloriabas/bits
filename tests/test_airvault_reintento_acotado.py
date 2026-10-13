"""El reintento lee solo las paginas que siguen pendientes.

Un batch al que le quedaban dos paginas amarillas se releia entero en cada
reintento, al planificar y al verificar. Lo que una verificacion ya confirmo
y el mapa del batch sigue mostrando igual no se vuelve a pedir.
"""

from __future__ import annotations

from typing import List

from app.airvault.client import PaginaDelLote
from app.airvault.config import (
    CAMPO_END_DATE,
    CAMPO_LOG_NUMBER,
    ESTADO_NECESITA_CORRECCION,
)
from app.airvault.indexer import (
    Indexador,
    verificar_lote,
    verificar_lotes,
    verificar_revision,
)
from app.airvault.model import Manifiesto, Registro
from app.airvault.session import ErrorDeConexion
from tests.airvault_fake import ClienteFalso, pagina

PICKLIST = ["HP-1848CMP"]


class ClienteConMapa(ClienteFalso):
    """El mapa dice el estado que cada pagina tiene de verdad."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mapas_pedidos = 0

    def paginas_del_lote(self, batch_id: str) -> List[PaginaDelLote]:
        self.mapas_pedidos += 1
        return [
            PaginaDelLote(
                pagina=n,
                estado=self.paginas[n].estado if n in self.paginas else 3,
                inicio_documento=n,
            )
            for n in range(1, self.page_count + 1)
        ]


def manifiesto(cuantos: int = 4, revisar: bool = False) -> Manifiesto:
    registros = [
        Registro(seq=i, matricula="HP-1848CMP",
                 log_number=f"228732{i}", fecha="2026/08/31", fleet="NG",
                 archivo_origen="Image_001.pdf", pagina_origen=i)
        for i in range(1, cuantos + 1)
    ]
    return Manifiesto(job_id="t", nombre_batch="DP | PRUEBA",
                      batch_id="003TEST", registros=registros,
                      solo_subir=revisar)


def indexado_con_una_amarilla(amarilla: int = 3):
    """Un batch escrito y verificado al que le quedo una pagina sin guardar."""
    m = manifiesto()
    cliente = ClienteConMapa(page_count=4, rechazar_en={amarilla})
    indexador = Indexador(cliente, m, PICKLIST, dormir=lambda _s: None)
    indexador.aplicar(indexador.planificar(4), detener_en_error=False)
    assert verificar_lote(cliente, m)[:2] == (3, 4)
    cliente.lecturas.clear()
    cliente.escrituras.clear()
    return m, cliente


def test_la_primera_verificacion_lee_todo_el_batch():
    m = manifiesto()
    cliente = ClienteConMapa(page_count=4)
    indexador = Indexador(cliente, m, PICKLIST)
    indexador.aplicar(indexador.planificar(4))
    cliente.lecturas.clear()

    assert verificar_lote(cliente, m) == (4, 4, [])
    assert sorted(cliente.lecturas) == [1, 2, 3, 4]
    # Sin nada confirmado todavia no se pide ni el mapa.
    assert cliente.mapas_pedidos == 0


def test_el_reintento_planifica_leyendo_solo_la_pagina_amarilla():
    m, cliente = indexado_con_una_amarilla()
    cliente.rechazar_en.clear()

    indexador = Indexador(cliente, m, PICKLIST)
    plan = indexador.planificar(4)

    assert cliente.lecturas == [3]
    # El plan es el mismo que con el batch entero leido: las verdes no se
    # tocan y la amarilla se escribe.
    assert [p.seq for p in plan.escribibles] == [3]
    assert [p.seq for p in plan.paginas if p.ya_indexada] == [1, 2, 4]
    assert sorted(indexador.remotas) == [1, 2, 3, 4]

    indexador.aplicar(plan)
    assert [escritura[0] for escritura in cliente.escrituras] == [3]


def test_el_reintento_verifica_leyendo_solo_la_pagina_amarilla():
    m, cliente = indexado_con_una_amarilla()

    validas, total, problemas = verificar_lote(cliente, m)

    assert (validas, total) == (3, 4)
    assert cliente.lecturas == [3]
    assert len(problemas) >= 1 and all("pagina 3" in p for p in problemas)


def test_el_batch_queda_confirmado_al_arreglarse_la_amarilla():
    m, cliente = indexado_con_una_amarilla()
    cliente.rechazar_en.clear()
    indexador = Indexador(cliente, m, PICKLIST)
    indexador.aplicar(indexador.planificar(4))
    cliente.lecturas.clear()

    assert verificar_lote(cliente, m) == (4, 4, [])
    assert cliente.lecturas == [3]
    # Y la siguiente ya no lee ninguna.
    cliente.lecturas.clear()
    assert verificar_lote(cliente, m) == (4, 4, [])
    assert cliente.lecturas == []


def test_una_confirmada_que_cambio_de_estado_se_vuelve_a_leer():
    m, cliente = indexado_con_una_amarilla()
    # La validacion final de AirVault, o una persona, la devolvio a amarillo.
    cliente.paginas[2] = pagina(
        2, estado=ESTADO_NECESITA_CORRECCION,
        valores={**cliente.paginas[2].valores, CAMPO_END_DATE: ""},
    )

    validas, total, _problemas = verificar_lote(cliente, m)

    assert (validas, total) == (2, 4)
    assert sorted(cliente.lecturas) == [2, 3]


def test_una_confirmada_borrada_del_batch_se_vuelve_a_leer():
    m, cliente = indexado_con_una_amarilla()
    mapa = cliente.paginas_del_lote("003TEST")
    cliente.paginas_del_lote = lambda _batch: [
        PaginaDelLote(p.pagina, p.estado, p.inicio_documento, p.pagina == 1)
        for p in mapa
    ]

    verificar_lote(cliente, m)

    assert sorted(cliente.lecturas) == [1, 3]


def test_si_el_registro_cambia_la_pagina_se_vuelve_a_leer():
    m, cliente = indexado_con_una_amarilla()
    m.registros[0].fecha = "2026/09/01"

    validas, total, problemas = verificar_lote(cliente, m)

    assert sorted(cliente.lecturas) == [1, 3]
    assert (validas, total) == (2, 4)
    assert any("pagina 1: fecha guardada" in p for p in problemas)


def test_lo_que_se_reescribe_deja_de_estar_confirmado():
    m, cliente = indexado_con_una_amarilla()
    indexador = Indexador(cliente, m, PICKLIST, sobrescribir=True)
    plan = indexador.planificar(4)
    # Con sobrescribir se lee el batch entero: se quiere el estado de ahora.
    assert sorted(cliente.lecturas) == [1, 2, 3, 4]
    indexador.aplicar(plan, detener_en_error=False)
    cliente.lecturas.clear()

    verificar_lote(cliente, m)

    assert sorted(cliente.lecturas) == [1, 2, 3, 4]


def test_sin_mapa_se_lee_el_batch_entero():
    m, cliente = indexado_con_una_amarilla()

    def sin_mapa(_batch):
        raise RuntimeError("AirVault no devolvio el mapa")

    cliente.paginas_del_lote = sin_mapa

    assert verificar_lote(cliente, m)[:2] == (3, 4)
    assert sorted(cliente.lecturas) == [1, 2, 3, 4]
    cliente.lecturas.clear()
    Indexador(cliente, m, PICKLIST).planificar(4)
    assert sorted(cliente.lecturas) == [1, 2, 3, 4]


def test_una_caida_al_leer_el_mapa_no_se_confunde_con_falta_de_mapa():
    m, cliente = indexado_con_una_amarilla()

    def sin_red(_batch):
        raise ErrorDeConexion("sin red")

    cliente.paginas_del_lote = sin_red

    try:
        verificar_lote(cliente, m)
    except ErrorDeConexion:
        pass
    else:
        raise AssertionError("la caida de la red tenia que salir")
    assert cliente.lecturas == []


def test_un_manifiesto_recien_cargado_lee_todo_una_vez():
    m, cliente = indexado_con_una_amarilla()
    recargado = Manifiesto.model_validate_json(m.model_dump_json())

    assert verificar_lote(cliente, recargado)[:2] == (3, 4)
    assert sorted(cliente.lecturas) == [1, 2, 3, 4]
    cliente.lecturas.clear()
    assert verificar_lote(cliente, recargado)[:2] == (3, 4)
    assert cliente.lecturas == [3]


def test_revisar_no_relee_las_paginas_que_ya_guardo():
    m = manifiesto(revisar=True)
    cliente = ClienteConMapa(page_count=4)
    indexador = Indexador(cliente, m, PICKLIST, dormir=lambda _s: None)
    indexador.aplicar(indexador.planificar(4))
    # Alguien corrigio a mano el log de la pagina 2 antes de confirmarla.
    cliente.paginas[2] = pagina(
        2, estado=ESTADO_NECESITA_CORRECCION,
        valores={**cliente.paginas[2].valores, CAMPO_LOG_NUMBER: "2287399"},
    )
    assert verificar_revision(cliente, m)[:2] == (3, 4)
    cliente.lecturas.clear()

    assert verificar_revision(cliente, m)[:2] == (3, 4)
    assert cliente.lecturas == [2]

    cliente.lecturas.clear()
    plan = Indexador(cliente, m, PICKLIST).planificar(4)
    assert cliente.lecturas == [2]
    assert [p.seq for p in plan.bloqueadas if not p.ya_indexada] == [2]


def test_la_verificacion_conjunta_lee_solo_lo_pendiente_de_cada_batch():
    m, cliente = indexado_con_una_amarilla()
    avances = []

    resultados = verificar_lotes(
        cliente, [m],
        al_avanzar=lambda _m, hechas, total: avances.append((hechas, total)),
    )

    assert resultados[0][:2] == (3, 4)
    assert cliente.lecturas == [3]
    # La barra cuenta las ya confirmadas: no vuelve a empezar de cero al
    # leer la unica que falta.
    assert avances[-1] == (4, 4)
