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
    # AirVault no conservo la fecha de la pagina 2.
    cliente.paginas[2] = pagina(
        2, estado=ESTADO_NECESITA_CORRECCION,
        valores={**cliente.paginas[2].valores, CAMPO_END_DATE: ""},
    )
    assert verificar_revision(cliente, m)[:2] == (3, 4)
    cliente.lecturas.clear()

    assert verificar_revision(cliente, m)[:2] == (3, 4)
    assert cliente.lecturas == [2]

    cliente.lecturas.clear()
    plan = Indexador(cliente, m, PICKLIST).planificar(4)
    assert cliente.lecturas == [2]
    assert [p.seq for p in plan.paginas if not p.ya_indexada] == [2]


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


# ── la pagina suelta que AirVault tiene con otro numero ────────────


def con_un_log_distinto(cuantos: int = 12, distinta: int = 5,
                        revisar: bool = True, estado: int = 3):
    """Un batch escrito, con una pagina que AirVault trae con otro numero.

    En la primera pasada las demas paginas estan vacias en AirVault, asi
    que no hay con que saber si es una suelta o un PDF corrido: se bloquea.
    """
    m = manifiesto(cuantos, revisar=revisar)
    for registro in m.registros:
        registro.log_number = f"22873{registro.seq:02d}"
    cliente = ClienteConMapa(
        page_count=cuantos,
        paginas={distinta: pagina(
            distinta, estado=estado, valores={CAMPO_LOG_NUMBER: "2297399"},
        )},
    )
    indexador = Indexador(cliente, m, PICKLIST, dormir=lambda _s: None)
    plan = indexador.planificar(cuantos)
    indexador.aplicar(plan, detener_en_error=False)
    return m, cliente, plan


def revisar_con_un_log_distinto(cuantos: int = 12, distinta: int = 5):
    return con_un_log_distinto(cuantos, distinta)


def reintentar(m, cliente):
    """La segunda pasada, ya con el resto del batch escrito."""
    indexador = Indexador(cliente, m, PICKLIST, dormir=lambda _s: None)
    plan = indexador.planificar(len(m.registros))
    indexador.aplicar(plan, detener_en_error=False)
    return plan


def test_sin_el_resto_del_batch_la_pagina_con_otro_numero_no_se_escribe():
    _m, cliente, plan = revisar_con_un_log_distinto()

    assert [p.seq for p in plan.bloqueadas] == [5]
    assert 5 not in [escritura[0] for escritura in cliente.escrituras]
    assert cliente.paginas[5].valores[CAMPO_LOG_NUMBER] == "2297399"


def test_la_pagina_amarilla_suelta_se_escribe_cuando_el_resto_coincide():
    """Era la unica amarilla del batch y nunca se llegaba a escribir.

    AirVault leyo otro numero en esa pagina y nadie la dio por buena. Con
    el resto del batch coincidiendo no es un PDF corrido: se escribe lo del
    manifiesto y el batch termina.
    """
    for revisar in (True, False):
        m, cliente, _plan = con_un_log_distinto(revisar=revisar)
        cliente.escrituras.clear()

        plan = reintentar(m, cliente)

        assert [p.seq for p in plan.escribibles if not p.ya_indexada] == [5]
        assert [escritura[0] for escritura in cliente.escrituras] == [5]
        assert cliente.paginas[5].valores[CAMPO_LOG_NUMBER] == "2287305"
        verificar = verificar_revision if revisar else verificar_lote
        assert verificar(cliente, m) == (12, 12, [])


def test_la_pagina_que_alguien_valido_con_otro_numero_no_se_pisa():
    """En verde la dejo una persona: se respeta, y REVISAR termina igual."""
    m, cliente, _plan = con_un_log_distinto(estado=0)
    cliente.escrituras.clear()
    esperas = []

    plan = reintentar(m, cliente)

    assert cliente.escrituras == []
    assert [p.seq for p in plan.bloqueadas if not p.ya_indexada] == [5]
    assert cliente.paginas[5].valores[CAMPO_LOG_NUMBER] == "2297399"
    resultado = verificar_revision(
        cliente, m, esperas=(5, 15), dormir=esperas.append,
    )
    assert resultado == (12, 12, [])
    # Releerla no la iba a cambiar: no se espera por ella.
    assert esperas == []


def test_un_batch_revisar_corrido_no_se_da_por_terminado():
    """Si muchas paginas traen otro numero no es una suelta: es otro PDF."""
    m = manifiesto(12, revisar=True)
    for registro in m.registros:
        registro.log_number = f"22873{registro.seq:02d}"
    cliente = ClienteConMapa(page_count=12, paginas={
        n: pagina(n, estado=ESTADO_NECESITA_CORRECCION,
                  valores={CAMPO_LOG_NUMBER: f"22973{n:02d}"})
        for n in range(1, 7)
    })
    indexador = Indexador(cliente, m, PICKLIST, dormir=lambda _s: None)
    indexador.aplicar(indexador.planificar(12), detener_en_error=False)

    validas, total, problemas = verificar_revision(cliente, m)

    assert (validas, total) == (6, 12)
    assert len(problemas) == 6


def test_dos_paginas_con_los_numeros_cruzados_no_se_dejan_pasar():
    """Cada una con el log de la otra: es un PDF en otro orden."""
    m, cliente, _plan = revisar_con_un_log_distinto(cuantos=24)
    for numero, vecina in ((5, 6), (6, 5)):
        cliente.paginas[numero] = pagina(
            numero, estado=ESTADO_NECESITA_CORRECCION,
            valores={CAMPO_LOG_NUMBER: m.registros[vecina - 1].log_number},
        )

    assert verificar_revision(cliente, m)[:2] == (22, 24)


def test_un_numero_que_airvault_leyo_dos_veces_tambien_es_una_suelta():
    """El numero remoto es el de otra bitacora que si lo tiene en su pagina.

    No hay paginas cruzadas: AirVault leyo el mismo numero en dos paginas.
    Sigue siendo una pagina suelta, y dejarla sin escribir hacia repetir el
    indexado del batch entero en cada revision.
    """
    m, cliente, _plan = revisar_con_un_log_distinto()
    cliente.paginas[5] = pagina(
        5, estado=ESTADO_NECESITA_CORRECCION,
        valores={CAMPO_LOG_NUMBER: m.registros[5].log_number},
    )
    cliente.escrituras.clear()

    reintentar(m, cliente)

    assert [escritura[0] for escritura in cliente.escrituras] == [5]
    assert verificar_revision(cliente, m) == (12, 12, [])


def test_en_el_batch_automatico_la_validada_con_otro_numero_no_se_confirma():
    """Alguien la dejo en verde con otro numero: ni se pisa ni se da por buena."""
    m, cliente, _plan = con_un_log_distinto(revisar=False, estado=0)
    cliente.escrituras.clear()

    reintentar(m, cliente)

    assert cliente.escrituras == []
    assert verificar_lote(cliente, m)[:2] == (11, 12)


# ── cual de los dos numeros es el bueno ────────────────────────────


def test_si_el_numero_bueno_es_el_de_airvault_se_conserva_el_de_airvault():
    """El caso real: 2334732 en el manifiesto y 2324732 en AirVault.

    Las demas bitacoras de ese avion en el batch son del libro de AirVault,
    asi que el equivocado es el manifiesto. La pagina se escribe con el
    numero de AirVault y el registro lo adopta, guardando el que se leyo.
    """
    for revisar in (True, False):
        m, cliente, _plan = con_un_log_distinto(revisar=revisar)
        # La lectura del programa cae en un libro que no es de ese avion.
        m.registros[4].log_number = "2297305"
        cliente.paginas[5] = pagina(
            5, estado=ESTADO_NECESITA_CORRECCION,
            valores={CAMPO_LOG_NUMBER: "2287305"},
        )
        cliente.escrituras.clear()

        reintentar(m, cliente)

        assert m.registros[4].log_number == "2287305"
        assert m.registros[4].log_leido == "2297305"
        assert [escritura[0] for escritura in cliente.escrituras] == [5]
        assert cliente.paginas[5].valores[CAMPO_LOG_NUMBER] == "2287305"
        verificar = verificar_revision if revisar else verificar_lote
        assert verificar(cliente, m) == (12, 12, [])


def test_sin_avion_leido_no_se_decide_y_la_pagina_no_se_toca():
    """Sin avion no hay libro con que comprobar: no se escribe ninguno."""
    m, cliente, _plan = con_un_log_distinto()
    m.registros[4].matricula = ""
    cliente.escrituras.clear()

    plan = reintentar(m, cliente)

    assert cliente.escrituras == []
    assert [p.seq for p in plan.bloqueadas if not p.ya_indexada] == [5]
    assert m.registros[4].log_number == "2287305"
    assert cliente.paginas[5].valores[CAMPO_LOG_NUMBER] == "2297399"
    # En REVISAR queda para quien revisa y el batch termina su parte.
    assert verificar_revision(cliente, m) == (12, 12, [])


def test_en_el_batch_automatico_la_que_no_se_decide_queda_amarilla():
    m, cliente, _plan = con_un_log_distinto(revisar=False)
    # Ninguna otra bitacora del batch es de su libro ni del de AirVault.
    m.registros[4].log_number = "2291105"
    cliente.escrituras.clear()

    reintentar(m, cliente)

    assert cliente.escrituras == []
    assert verificar_lote(cliente, m)[:2] == (11, 12)


def test_no_gana_un_numero_cuyo_libro_es_de_otro_avion():
    from app.airvault.indexer import GANA_MANIFIESTO, arbitrar_numero, logs_sueltos

    m, cliente, _plan = con_un_log_distinto()
    # Dos bitacoras de otro avion ocupan el libro del numero de AirVault.
    for registro, numero in zip(m.registros[10:12], ("2297350", "2297351")):
        registro.log_number = numero
        registro.matricula = "HP-1852CMP"
    cliente.paginas[11] = pagina(11, estado=3, valores={CAMPO_LOG_NUMBER: "2297350"})
    cliente.paginas[12] = pagina(12, estado=3, valores={CAMPO_LOG_NUMBER: "2297351"})
    remotas = {
        registro.seq: cliente.paginas[registro.seq] for registro in m.registros
    }
    sueltas = logs_sueltos(m, remotas)

    ganador, motivo = arbitrar_numero(
        m, remotas, m.registros[4], "2297399", sueltas,
    )

    assert ganador == GANA_MANIFIESTO
    assert "22873A" in motivo
