"""Recorrido completo del indexado contra un cliente falso."""

from __future__ import annotations

import pytest

from app.airvault.config import (
    CAMPOS_OBLIGATORIOS,
    CAMPO_AUDIT_STATUS,
    CAMPO_DESCRIPCION,
    CAMPO_END_DATE,
    CAMPO_LOG_NUMBER,
    CAMPO_MATRICULA,
    CAMPO_WORK_LOCATION,
    ESTADO_NECESITA_CORRECCION,
    ESTADO_VALIDO,
)
from app.airvault.guards import ErrorDeGuarda
from app.airvault.indexer import Indexador, verificar_lote
from app.airvault.model import EstadoRegistro, Manifiesto, Registro
from tests.airvault_fake import ClienteFalso, pagina

PICKLIST = ["HP-1848CMP", "HP-1852CMP"]


def manifiesto(cuantos: int = 2, batch_id: str = "003TEST") -> Manifiesto:
    registros = [
        Registro(seq=i, matricula="HP-1848CMP",
                 log_number=f"228732{i}", fecha="2026/08/31", fleet="NG",
                 archivo_origen="Image_001.pdf", pagina_origen=i)
        for i in range(1, cuantos + 1)
    ]
    return Manifiesto(job_id="t", nombre_batch="DP | PRUEBA",
                      batch_id=batch_id, registros=registros)


def test_plan_marca_todo_escribible_en_lote_limpio():
    cliente = ClienteFalso(page_count=2)
    plan = Indexador(cliente, manifiesto(), PICKLIST).planificar(2)
    assert plan.resumen() == {"total": 2, "escribibles": 2, "bloqueadas": 0,
                              "separadores": 0, "avisos_globales": 0,
                              "fechas_inferidas": 0}


def test_plan_no_escribe_nada():
    cliente = ClienteFalso(page_count=2)
    Indexador(cliente, manifiesto(), PICKLIST).planificar(2)
    assert cliente.escrituras == []


@pytest.mark.parametrize("fecha", ["", "08/30/2026", "02/31/2026"])
def test_verificar_no_da_por_buena_una_fecha_ausente_o_distinta(fecha):
    cli = ClienteFalso(paginas={1: pagina(1, estado=0, valores={
        CAMPO_LOG_NUMBER: "2287321", CAMPO_MATRICULA: "HP-1848CMP",
        CAMPO_END_DATE: fecha,
    })})
    validas, total, problemas = verificar_lote(cli, manifiesto(1))
    assert (validas, total) == (0, 1)
    assert any("fecha guardada" in p for p in problemas)


@pytest.mark.parametrize("fecha", ["08/31/2026", "8/31/2026 00:00:00", "2026-08-31T00:00:00"])
def test_verificar_acepta_la_misma_fecha_en_las_respuestas_de_airvault(fecha):
    from app.airvault.config import CAMPO_DOC_TYPE, CAMPO_FLEET, CAMPO_AUDIT_STATUS
    cli = ClienteFalso(paginas={1: pagina(1, estado=0, valores={
                CAMPO_DOC_TYPE: "Log Page", CAMPO_FLEET: "NG", CAMPO_AUDIT_STATUS: "PUBLISHED",
        CAMPO_LOG_NUMBER: "2287321", CAMPO_MATRICULA: "HP-1848CMP",
        CAMPO_END_DATE: fecha,
    })})
    assert verificar_lote(cli, manifiesto(1)) == (1, 1, [])


def test_repara_fecha_vacia_sin_cambiar_los_campos_remotos():
    m = manifiesto(1)
    cli = ClienteFalso(paginas={1: pagina(1, estado=0, valores={
        CAMPO_LOG_NUMBER: "2287321", CAMPO_MATRICULA: "HP-1848CMP",
        CAMPO_END_DATE: "", CAMPO_AUDIT_STATUS: "Conservado",
    })}, page_count=1)
    indexador = Indexador(cli, m, PICKLIST)
    plan = indexador.planificar(1)
    assert len(plan.escribibles) == 1
    indexador.aplicar(plan)
    assert cli.escrituras[0][1][CAMPO_END_DATE] == "08/31/2026"
    assert cli.escrituras[0][1][CAMPO_AUDIT_STATUS] == "Conservado"
    assert verificar_lote(cli, m) == (1, 1, [])


def test_no_repara_fecha_si_el_log_remoto_pertenece_a_otra_bitacora():
    cli = ClienteFalso(paginas={1: pagina(1, estado=0, valores={
        CAMPO_LOG_NUMBER: "9999999", CAMPO_MATRICULA: "HP-1848CMP",
        CAMPO_END_DATE: "",
    })}, page_count=1)
    indexador = Indexador(cli, manifiesto(1), PICKLIST)
    assert not indexador.planificar(1).escribibles


@pytest.mark.parametrize("campo", CAMPOS_OBLIGATORIOS)
def test_una_pagina_sin_obligatorio_no_cuenta_como_indexada(campo):
    from app.airvault.mapping import valores_de_indice

    m = manifiesto(1)
    valores = valores_de_indice(m.registros[0], m.doc_type, m.audit_status)
    valores.pop(campo)
    cli = ClienteFalso(paginas={1: pagina(1, estado=0, valores=valores)})
    validas, total, problemas = verificar_lote(cli, m)
    assert (validas, total) == (0, 1)
    assert any("faltan datos obligatorios" in p for p in problemas)


def test_lote_con_otra_cantidad_de_paginas_corta():
    cliente = ClienteFalso(page_count=5)
    with pytest.raises(ErrorDeGuarda):
        Indexador(cliente, manifiesto(), PICKLIST).planificar(5)


def test_log_distinto_bloquea_esa_pagina():
    cliente = ClienteFalso(
        paginas={2: pagina(2, estado=3, valores={CAMPO_LOG_NUMBER: "9999999"})},
        page_count=2,
    )
    plan = Indexador(cliente, manifiesto(), PICKLIST).planificar(2)
    assert len(plan.escribibles) == 1
    assert plan.bloqueadas[0].seq == 2
    assert plan.bloqueadas[0].avisos[0].codigo == "desalineado"


def test_otro_avion_en_el_libro_bloquea_las_paginas_que_lo_contradicen():
    """Un libro es un avion, y AirVault ya dijo cual en una pagina verde.

    La pagina 1 esta publicada como HP-1852CMP; la 2 es del mismo libro y
    el manifiesto la trae como HP-1848CMP. Una de las dos lecturas esta
    mal y no se sabe cual, asi que no se escribe ninguna: publicar la
    bitacora a nombre de otro avion no se deshace con comodidad.
    """
    cliente = ClienteFalso(
        paginas={1: pagina(1, estado=ESTADO_VALIDO, valores={
            CAMPO_LOG_NUMBER: "2287321",
            CAMPO_MATRICULA: "HP-1852CMP",
        })},
        page_count=2,
    )

    plan = Indexador(cliente, manifiesto(), PICKLIST).planificar(2)

    assert plan.escribibles == []
    bloqueada = [p for p in plan.bloqueadas if p.seq == 2][0]
    assert [a.codigo for a in bloqueada.avisos] == ["matricula_del_libro"]


def test_el_avion_que_airvault_confirma_no_bloquea_nada():
    cliente = ClienteFalso(
        paginas={1: pagina(1, estado=ESTADO_VALIDO, valores={
            CAMPO_END_DATE: "08/31/2026", CAMPO_LOG_NUMBER: "2287321",
            CAMPO_MATRICULA: "HP-1848CMP",
        })},
        page_count=2,
    )

    plan = Indexador(cliente, manifiesto(), PICKLIST).planificar(2)

    # La 1 se salta por estar ya en verde, no por su avion.
    assert [p.seq for p in plan.escribibles] == [2]


def test_pagina_bloqueada_no_se_escribe():
    cliente = ClienteFalso(
        paginas={2: pagina(2, estado=3, valores={CAMPO_LOG_NUMBER: "9999999"})},
        page_count=2,
    )
    indexador = Indexador(cliente, manifiesto(), PICKLIST)
    plan = indexador.planificar(2)
    resultado = indexador.aplicar(plan)
    assert [p for p, _v, _e in cliente.escrituras] == [1]
    assert resultado.escritas == 1 and resultado.omitidas == 1


def test_escritura_manda_los_valores_correctos():
    cliente = ClienteFalso(page_count=1)
    m = manifiesto(1)
    indexador = Indexador(cliente, m, PICKLIST)
    indexador.aplicar(indexador.planificar(1))
    _pagina, valores, estado = cliente.escrituras[0]
    assert valores[CAMPO_MATRICULA] == "HP-1848CMP"
    assert valores[CAMPO_LOG_NUMBER] == "2287321"
    assert valores[CAMPO_WORK_LOCATION] == ""
    assert estado == ESTADO_VALIDO


def test_una_pagina_localmente_escrita_se_reenvia_si_sigue_amarilla():
    cliente = ClienteFalso(
        paginas={1: pagina(1, estado=3)}, page_count=1
    )
    m = manifiesto(1)
    m.registros[0].estado = EstadoRegistro.ESCRITA

    indexador = Indexador(cliente, m, PICKLIST)
    indexador.aplicar(indexador.planificar(1))

    assert [numero for numero, _valores, _estado in cliente.escrituras] == [1]


def test_el_vuelo_se_marca_solo_en_el_payload_automatico():
    cliente = ClienteFalso(page_count=1)
    m = manifiesto(1)
    m.registros[0].flight_number = "CM137"
    indexador = Indexador(cliente, m, PICKLIST)
    plan = indexador.planificar(1)

    # El plan alimenta el reporte local y conserva el vuelo original.
    assert plan.paginas[0].valores[CAMPO_DESCRIPCION] == "CM137"
    indexador.aplicar(plan)

    _pagina, valores_remotos, _estado = cliente.escrituras[0]
    assert valores_remotos[CAMPO_DESCRIPCION] == "CM137 AUTO INDEX"


def test_sin_vuelo_se_manda_solo_la_marca_automatica():
    cliente = ClienteFalso(page_count=1)
    m = manifiesto(1)
    indexador = Indexador(cliente, m, PICKLIST)
    indexador.aplicar(indexador.planificar(1))

    _pagina, valores_remotos, _estado = cliente.escrituras[0]
    assert valores_remotos[CAMPO_DESCRIPCION] == "AUTO INDEX"


def test_una_discrepancia_se_escribe_con_su_audit_status():
    """La marca del batch REVISAR que dice cual hay que auditar."""
    cliente = ClienteFalso(page_count=2)
    m = manifiesto()
    m.solo_subir = True
    m.registros[0].discrepancia = True
    indexador = Indexador(cliente, m, PICKLIST)
    indexador.aplicar(indexador.planificar(2))

    escritos = {
        pagina: valores[CAMPO_AUDIT_STATUS]
        for pagina, valores, _estado in cliente.escrituras
    }
    assert escritos == {1: "AUDIT IN PROGRESS", 2: "PUBLISHED"}


def test_revisar_no_lleva_la_marca_automatica():
    """El batch de REVISAR se termina a mano: su Description va sin marca."""
    cliente = ClienteFalso(page_count=1)
    m = manifiesto(1)
    m.solo_subir = True
    m.registros[0].flight_number = "CM137"
    indexador = Indexador(cliente, m, PICKLIST)
    indexador.aplicar(indexador.planificar(1))

    _pagina, valores_remotos, estado = cliente.escrituras[0]
    assert valores_remotos[CAMPO_DESCRIPCION] == "CM137"
    assert estado == ESTADO_NECESITA_CORRECCION


def test_revisar_sin_vuelo_no_manda_description():
    """Sin vuelo no hay nada que poner: el campo no viaja y no borra nada."""
    cliente = ClienteFalso(page_count=1)
    m = manifiesto(1)
    m.solo_subir = True
    indexador = Indexador(cliente, m, PICKLIST)
    indexador.aplicar(indexador.planificar(1))

    _pagina, valores_remotos, _estado = cliente.escrituras[0]
    assert CAMPO_DESCRIPCION not in valores_remotos


def test_revisar_omite_end_date_solo_en_la_fecha_dudosa():
    cliente = ClienteFalso(page_count=2)
    m = manifiesto(2)
    m.solo_subir = True
    m.registros[0].fecha = ""
    m.registros[0].fecha_dudosa = True
    indexador = Indexador(cliente, m, PICKLIST)

    plan = indexador.planificar(2)
    assert [pagina.escribible for pagina in plan.paginas] == [True, True]
    assert CAMPO_END_DATE not in plan.paginas[0].valores
    assert plan.paginas[1].valores[CAMPO_END_DATE] == "08/31/2026"

    indexador.aplicar(plan)
    escritos = {pagina: valores for pagina, valores, _ in cliente.escrituras}
    assert CAMPO_END_DATE not in escritos[1]
    assert escritos[2][CAMPO_END_DATE] == "08/31/2026"


def test_fecha_dudosa_no_se_omite_fuera_de_revisar():
    cliente = ClienteFalso(page_count=1)
    m = manifiesto(1)
    m.registros[0].fecha = ""
    m.registros[0].fecha_dudosa = True

    plan = Indexador(cliente, m, PICKLIST).planificar(1)

    assert plan.escribibles == []
    assert [a.codigo for a in plan.bloqueadas[0].avisos] == [
        "obligatorio_vacio"
    ]


def test_pagina_ya_valida_se_respeta():
    cliente = ClienteFalso(paginas={1: pagina(1, estado=ESTADO_VALIDO)},
                           page_count=1)
    indexador = Indexador(cliente, manifiesto(1), PICKLIST)
    plan = indexador.planificar(1)
    assert plan.bloqueadas[0].avisos[0].codigo == "ya_indexada"
    indexador.aplicar(plan)
    assert cliente.escrituras == []


def test_pagina_valida_se_reescribe_si_work_location_no_esta_vacio():
    cliente = ClienteFalso(
        paginas={
            1: pagina(
                1, estado=ESTADO_VALIDO,
                valores={CAMPO_WORK_LOCATION: "PTY"},
            )
        },
        page_count=1,
    )
    indexador = Indexador(cliente, manifiesto(1), PICKLIST)

    plan = indexador.planificar(1)
    indexador.aplicar(plan)

    assert len(cliente.escrituras) == 1
    assert cliente.escrituras[0][1][CAMPO_WORK_LOCATION] == ""


def test_sobrescribir_permite_pisar():
    cliente = ClienteFalso(paginas={1: pagina(1, estado=ESTADO_VALIDO)},
                           page_count=1)
    indexador = Indexador(cliente, manifiesto(1), PICKLIST,
                          sobrescribir=True)
    indexador.aplicar(indexador.planificar(1))
    assert len(cliente.escrituras) == 1


def test_corte_a_media_escritura_deja_constancia():
    cliente = ClienteFalso(page_count=3, fallar_en={2})
    m = manifiesto(3)
    indexador = Indexador(cliente, m, PICKLIST)
    resultado = indexador.aplicar(indexador.planificar(3))
    assert resultado.escritas == 1 and resultado.fallidas == 1
    assert m.registros[0].estado is EstadoRegistro.ESCRITA
    assert m.registros[1].estado is EstadoRegistro.ERROR
    assert m.registros[2].estado is EstadoRegistro.PENDIENTE


def test_reanudar_no_reescribe_lo_ya_hecho():
    cliente = ClienteFalso(page_count=3, fallar_en={2})
    m = manifiesto(3)
    indexador = Indexador(cliente, m, PICKLIST)
    indexador.aplicar(indexador.planificar(3))

    # Segunda ejecución: la pagina 1 ya esta escrita y no se vuelve a tocar.
    cliente.fallar_en = set()
    m.registros[1].estado = EstadoRegistro.PENDIENTE
    m.registros[1].avisos = []
    indexador2 = Indexador(cliente, m, PICKLIST, sobrescribir=True)
    indexador2.aplicar(indexador2.planificar(3))
    escritas = [p for p, _v, _e in cliente.escrituras]
    assert escritas.count(1) == 1
    assert sorted(set(escritas)) == [1, 2, 3]


def test_manifiesto_se_guarda_tras_cada_pagina():
    guardados = []
    cliente = ClienteFalso(page_count=2)
    indexador = Indexador(cliente, manifiesto(2), PICKLIST,
                          al_guardar=lambda m: guardados.append(
                              len(m.escritos())))
    indexador.aplicar(indexador.planificar(2))
    assert guardados == [1, 2]


def test_sin_lote_no_se_planifica():
    cliente = ClienteFalso(page_count=1)
    m = manifiesto(1)
    m.batch_id = None
    with pytest.raises(ErrorDeGuarda):
        Indexador(cliente, m, PICKLIST).planificar(1)


def test_verificar_cuenta_las_validas():
    from app.airvault.config import CAMPO_DOC_TYPE, CAMPO_FLEET, CAMPO_AUDIT_STATUS
    cliente = ClienteFalso(
        paginas={
            1: pagina(1, estado=ESTADO_VALIDO, valores={
                CAMPO_DOC_TYPE: "Log Page", CAMPO_FLEET: "NG", CAMPO_AUDIT_STATUS: "PUBLISHED",
                CAMPO_END_DATE: "08/31/2026", CAMPO_LOG_NUMBER: "2287321", CAMPO_MATRICULA: "HP-1848CMP",
            }),
            2: pagina(2, estado=3, valores={
                CAMPO_DOC_TYPE: "Log Page", CAMPO_FLEET: "NG", CAMPO_AUDIT_STATUS: "PUBLISHED",
                CAMPO_END_DATE: "08/31/2026", CAMPO_LOG_NUMBER: "2287322", CAMPO_MATRICULA: "HP-1848CMP",
            }),
        },
        page_count=2,
    )
    validas, total, problemas = verificar_lote(cliente, manifiesto(2))
    assert (validas, total) == (1, 2)
    assert len(problemas) == 1


def test_verificar_exige_work_location_vacio():
    cliente = ClienteFalso(
        paginas={
            1: pagina(
                1, estado=ESTADO_VALIDO,
                valores={
                    CAMPO_WORK_LOCATION: "BOG",
                    CAMPO_LOG_NUMBER: "2287321",
                    CAMPO_MATRICULA: "HP-1848CMP",
                },
            )
        },
        page_count=1,
    )

    validas, total, problemas = verificar_lote(cliente, manifiesto(1))

    assert (validas, total) == (0, 1)
    assert any("Work Location" in problema for problema in problemas)


def test_verificar_no_cuenta_una_verde_con_identidad_equivocada():
    cliente = ClienteFalso(paginas={
        1: pagina(1, estado=ESTADO_VALIDO, valores={
            CAMPO_LOG_NUMBER: "2287999", CAMPO_MATRICULA: "HP-1852CMP",
        }),
    })

    validas, total, problemas = verificar_lote(cliente, manifiesto(1))

    assert (validas, total) == (0, 1)
    assert any("log 2287999" in problema for problema in problemas)
    assert any("HP-1852CMP" in problema for problema in problemas)


# ── lo recien escrito que AirVault tarda en devolver ───────────────

class ClienteQueTardaEnReflejar(ClienteFalso):
    """Devuelve una version vieja de ciertas paginas las primeras lecturas.

    Es lo que se vio tras indexar: la verificacion leyo una pagina sin un
    obligatorio que si estaba guardado, y minutos despues la misma lectura
    la traia entera.
    """

    def __init__(self, viejas, lecturas_viejas=1, **extra):
        super().__init__(**extra)
        self.viejas = dict(viejas)
        self.quedan = {numero: lecturas_viejas for numero in self.viejas}

    def leer_pagina(self, batch_id, numero):
        if self.quedan.get(numero, 0) > 0:
            self.quedan[numero] -= 1
            self.lecturas.append(numero)
            return self.viejas[numero]
        return super().leer_pagina(batch_id, numero)


def _completa(m, registro, estado=ESTADO_VALIDO):
    from app.airvault.mapping import valores_de_indice

    return pagina(registro.seq, estado=estado, valores=valores_de_indice(
        registro, m.doc_type, m.audit_status,
    ))


def _sin_fleet(m, registro):
    from app.airvault.config import CAMPO_FLEET

    completa = _completa(m, registro)
    valores = dict(completa.valores)
    valores.pop(CAMPO_FLEET)
    return pagina(registro.seq, estado=ESTADO_VALIDO, valores=valores)


def test_verificar_relee_la_pagina_que_airvault_aun_no_refleja():
    m = manifiesto(2)
    cliente = ClienteQueTardaEnReflejar(
        {2: _sin_fleet(m, m.registros[1])},
        paginas={r.seq: _completa(m, r) for r in m.registros},
    )
    dormidas: list = []

    resultado = verificar_lote(
        cliente, m, esperas=(5, 15), dormir=dormidas.append,
    )

    assert resultado == (2, 2, [])
    # Solo se espera lo necesario y solo se relee la pagina dudosa.
    assert dormidas == [5]
    assert cliente.lecturas == [1, 2, 2]


def test_sin_esperas_la_lectura_vieja_cuenta_como_problema():
    m = manifiesto(2)
    cliente = ClienteQueTardaEnReflejar(
        {2: _sin_fleet(m, m.registros[1])},
        paginas={r.seq: _completa(m, r) for r in m.registros},
    )

    validas, total, problemas = verificar_lote(cliente, m)

    assert (validas, total) == (1, 2)
    assert any("faltan datos obligatorios" in p for p in problemas)


def test_agotadas_las_relecturas_el_problema_se_informa():
    m = manifiesto(1)
    cliente = ClienteFalso(paginas={1: _sin_fleet(m, m.registros[0])})
    dormidas: list = []

    validas, total, problemas = verificar_lote(
        cliente, m, esperas=(5, 15), dormir=dormidas.append,
    )

    assert (validas, total) == (0, 1)
    assert dormidas == [5, 15]
    assert problemas == [
        "pagina 1: faltan datos obligatorios en AirVault: Fleet"
    ]


def test_una_fecha_dudosa_no_se_relee():
    """Lo que falta es del manifiesto: esperar a AirVault no lo arregla."""
    m = manifiesto(1)
    m.registros[0].fecha_dudosa = True
    cliente = ClienteFalso(paginas={1: _completa(m, m.registros[0])})
    dormidas: list = []

    validas, _total, _problemas = verificar_lote(
        cliente, m, esperas=(5, 15), dormir=dormidas.append,
    )

    assert validas == 0
    assert dormidas == []
    assert cliente.lecturas == [1]


def test_matricula_fuera_de_picklist_se_escribe_completa_y_valida():
    m = manifiesto(1)
    m.registros[0].matricula = "HP-0000CMP"
    cliente = ClienteFalso(page_count=1)
    indexador = Indexador(cliente, m, PICKLIST)
    plan = indexador.planificar(1)
    codigos = {a.codigo for a in plan.escribibles[0].avisos}
    assert "matricula_desconocida" in codigos
    assert plan.escribibles[0].requiere_revision

    indexador.aplicar(plan)

    assert cliente.escrituras[0][2] == ESTADO_VALIDO


def test_campo_obligatorio_vacio_bloquea_un_batch_automatico():
    m = manifiesto(1)
    m.registros[0].log_number = ""
    cliente = ClienteFalso(page_count=1)
    indexador = Indexador(cliente, m, PICKLIST)

    plan = indexador.planificar(1)
    indexador.aplicar(plan)

    assert len(plan.escribibles) == 0
    assert cliente.escrituras == []


def test_revisar_omite_el_vacio_y_guarda_los_demas_datos():
    """El campo sin lectura no viaja como vacio ni bloquea los demas."""
    m = manifiesto(1)
    m.solo_subir = True
    m.registros[0].log_number = ""
    cliente = ClienteFalso(page_count=1)
    indexador = Indexador(cliente, m, PICKLIST)

    plan = indexador.planificar(1)
    indexador.aplicar(plan)

    assert len(plan.escribibles) == 1
    assert CAMPO_LOG_NUMBER not in cliente.escrituras[0][1]
    assert cliente.escrituras[0][1][CAMPO_MATRICULA] == "HP-1848CMP"
    assert cliente.escrituras[0][2] == ESTADO_NECESITA_CORRECCION
    assert any("Log Page Number" in aviso for aviso in m.registros[0].avisos)


def test_una_discrepancia_completa_se_conserva_amarilla_en_revisar():
    m = manifiesto(1)
    m.solo_subir = True
    cliente = ClienteFalso(page_count=1)
    indexador = Indexador(cliente, m, PICKLIST)

    indexador.aplicar(indexador.planificar(1))

    assert cliente.escrituras[0][2] == ESTADO_NECESITA_CORRECCION


def test_aprende_la_flota_que_airvault_ya_tiene():
    from app.airvault.config import CAMPO_FLEET, CAMPO_LESSOR
    from app.airvault.mapping import ResolutorFlota

    m = manifiesto(2)
    # La flota venia adivinada por la regla de prefijos.
    for registro in m.registros:
        registro.fleet = "NG"
        registro.fleet_inferido = True
    cliente = ClienteFalso(
        paginas={
            1: pagina(1, valores={CAMPO_MATRICULA: "HP-1848CMP",
                                  CAMPO_FLEET: "MAX",
                                  CAMPO_LESSOR: "COPA"}),
        },
        page_count=2,
    )
    resolutor = ResolutorFlota()
    indexador = Indexador(cliente, m, PICKLIST, resolutor=resolutor)
    indexador.planificar(2)
    # AirVault manda sobre la regla, y deja de estar marcada como inferida.
    assert m.registros[0].fleet == "MAX"
    assert m.registros[0].fleet_inferido is False
    assert resolutor.resolver("HP-1848CMP")[:2] == ("MAX", "COPA")


def test_cada_pagina_se_lee_una_sola_vez():
    cliente = ClienteFalso(page_count=3)
    Indexador(cliente, manifiesto(3), PICKLIST).planificar(3)
    assert sorted(cliente.lecturas) == [1, 2, 3]
