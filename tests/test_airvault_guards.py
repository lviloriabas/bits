"""Las guardas son lo unico que impide escribir en la bitacora equivocada."""

from __future__ import annotations

import pytest

from app.airvault.client import PaginaIndexada
from app.airvault.config import (
    CAMPO_LOG_NUMBER,
    CAMPO_MATRICULA,
    ESTADO_VALIDO,
)
from app.airvault.guards import (
    ErrorDeGuarda,
    matriculas_por_libro,
    verificar_alineacion,
    verificar_cantidad,
    verificar_duplicados,
    verificar_matricula_del_libro,
    verificar_matriculas,
    verificar_no_pisar,
    verificar_obligatorios,
)
from app.airvault.mapping import valores_de_indice
from app.airvault.model import Registro


def remota(log="2287325", matricula="HP-1848CMP", estado=ESTADO_VALIDO):
    """Una pagina tal como AirVault la devuelve al leer el batch."""
    return PaginaIndexada(
        pagina=1,
        estado=estado,
        valores={CAMPO_LOG_NUMBER: log, CAMPO_MATRICULA: matricula},
        columnas={},
    )


def registro(seq=1, matricula="HP-1848CMP", log="2287325",
             fecha="2026/08/31", fleet="NG"):
    return Registro(seq=seq, matricula=matricula, log_number=log,
                    fecha=fecha, fleet=fleet)


def test_cantidad_igual_pasa():
    verificar_cantidad([registro(1), registro(2)], 2)


def test_cantidad_distinta_corta():
    with pytest.raises(ErrorDeGuarda) as exc:
        verificar_cantidad([registro(1), registro(2)], 3)
    assert "3" in str(exc.value) and "2" in str(exc.value)


def test_matricula_fuera_del_picklist():
    avisos = verificar_matriculas([registro(matricula="HP-9999CMP")],
                                  ["HP-1848CMP"])
    assert [a.codigo for a in avisos] == ["matricula_desconocida"]


def test_matricula_vacia_se_acusa():
    avisos = verificar_matriculas([registro(matricula="")], ["HP-1848CMP"])
    assert [a.codigo for a in avisos] == ["matricula_vacia"]


def test_picklist_vacio_no_inventa_errores():
    # Si no se pudo leer el catalogo no se puede acusar a nadie.
    assert verificar_matriculas([registro()], []) == []


def test_obligatorio_vacio_se_acusa():
    reg = registro(fecha="")
    valores = valores_de_indice(reg, "Log Page", "PUBLISHED")
    avisos = verificar_obligatorios(reg, valores)
    assert [a.codigo for a in avisos] == ["obligatorio_vacio"]


def test_todos_los_obligatorios_llenos_no_avisa():
    reg = registro()
    valores = valores_de_indice(reg, "Log Page", "PUBLISHED")
    assert verificar_obligatorios(reg, valores) == []


def test_log_distinto_es_desalineacion():
    avisos = verificar_alineacion(registro(log="2287325"),
                                  {CAMPO_LOG_NUMBER: "2287999"})
    assert [a.codigo for a in avisos] == ["desalineado"]


def test_una_prueba_controlada_puede_permitir_log_distinto():
    assert verificar_alineacion(
        registro(log="7777777"),
        {CAMPO_LOG_NUMBER: "2287325"},
        permitir_log_distinto=True,
    ) == []


def test_log_igual_no_avisa():
    assert verificar_alineacion(registro(log="2287325"),
                                {CAMPO_LOG_NUMBER: "2287325"}) == []


def test_sin_dato_remoto_se_sigue_por_posicion():
    assert verificar_alineacion(registro(), {}) == []


@pytest.mark.parametrize("leido", ["00", "1", "228732", "22873255", "N/A"])
def test_una_lectura_fallida_de_airvault_no_es_desalineacion(leido):
    """AirVault lee la letra por su cuenta y a veces deja algo que no es un log.

    En una pagina sin indexar eso no nombra otra bitacora. Tomarlo por un
    PDF corrido dejaba la pagina sin escribir y el batch en amarillo.
    """
    for estado in (1, 2, 3):
        assert verificar_alineacion(
            registro(log="2287325"), {CAMPO_LOG_NUMBER: leido},
            estado_pagina=estado,
        ) == [], f"estado {estado}"


@pytest.mark.parametrize("estado", [None, ESTADO_VALIDO])
def test_un_log_raro_si_bloquea_en_verde_o_sin_saber_el_estado(estado):
    avisos = verificar_alineacion(
        registro(log="2287325"), {CAMPO_LOG_NUMBER: "00"},
        estado_pagina=estado,
    )

    assert [aviso.codigo for aviso in avisos] == ["desalineado"]


def test_otro_numero_de_bitacora_bloquea_aunque_la_pagina_no_este_verde():
    avisos = verificar_alineacion(
        registro(log="2287325"), {CAMPO_LOG_NUMBER: "2287999"},
        estado_pagina=3,
    )

    assert [aviso.codigo for aviso in avisos] == ["desalineado"]


def test_la_matricula_de_quick_upload_nunca_bloquea():
    """Es la del archivo entero, puesta por nosotros: no dice nada.

    Quick Upload clasifica el PDF completo con el Aircraft de su primera
    bitacora, asi que toda pagina cuyo avion sea otro llega con la matricula
    «distinta». Tomarla por evidencia dejaba el batch entero sin indexar y
    en amarillo: ni una sola pagina llegaba a escribirse.
    """
    for estado in (None, 1, 2, 3):
        avisos = verificar_alineacion(
            registro(matricula="HP-1848CMP"),
            {CAMPO_MATRICULA: "HP-1852CMP"},
            estado_pagina=estado,
        )

        assert avisos == [], f"estado {estado}"


def test_matricula_distinta_si_bloquea_una_pagina_verde():
    avisos = verificar_alineacion(
        registro(matricula="HP-1848CMP"),
        {CAMPO_MATRICULA: "HP-1852CMP"},
        estado_pagina=0,
    )

    assert [aviso.codigo for aviso in avisos] == ["matricula_distinta"]


def test_pagina_ya_valida_no_se_pisa():
    avisos = verificar_no_pisar(registro(), estado_pagina=0,
                               sobrescribir=False)
    assert [a.codigo for a in avisos] == ["ya_indexada"]


def test_pagina_ya_valida_se_pisa_si_se_pide():
    assert verificar_no_pisar(registro(), 0, sobrescribir=True) == []


def test_pagina_amarilla_se_puede_escribir():
    assert verificar_no_pisar(registro(), 3, sobrescribir=False) == []


def test_logs_repetidos_se_acusan():
    avisos = verificar_duplicados([registro(1, log="2287325"),
                                   registro(2, log="2287325")])
    assert [a.codigo for a in avisos] == ["log_duplicado"]
    assert avisos[0].seq == 2


def test_logs_vacios_no_cuentan_como_duplicados():
    assert verificar_duplicados([registro(1, log=""),
                                 registro(2, log="")]) == []


# ── el avion del libro ─────────────────────────────────────────────
#
# Un libro fisico tiene cincuenta paginas y una sola aeronave, asi que
# cualquier pagina suya que AirVault ya da por buena responde por las
# demas. Es la unica evidencia externa que hay al planificar, y sale de la
# misma lectura del batch: no cuesta ni una peticion de mas.


def test_una_pagina_verde_dice_el_avion_de_su_libro():
    por_libro = matriculas_por_libro([remota("2287325", "HP-1848CMP")])

    assert por_libro == {"22873A": "HP-1848CMP"}


def test_una_pagina_que_no_esta_verde_no_dice_nada():
    """En cualquier otro estado se ve la clasificacion de Quick Upload."""
    for estado in (1, 2, 3):
        assert matriculas_por_libro(
            [remota(estado=estado)]
        ) == {}, f"estado {estado}"


def test_un_libro_con_dos_aviones_en_airvault_no_manda():
    por_libro = matriculas_por_libro([
        remota("2287325", "HP-1848CMP"),
        remota("2287330", "HP-1852CMP"),
    ])

    assert por_libro == {}


def test_otro_avion_en_el_mismo_libro_bloquea_la_pagina():
    avisos = verificar_matricula_del_libro(
        [registro(seq=4, matricula="HP-1852CMP", log="2287340")],
        {"22873A": "HP-1848CMP"},
    )

    assert [aviso.codigo for aviso in avisos] == ["matricula_del_libro"]
    assert avisos[0].seq == 4
    assert "HP-1848CMP" in avisos[0].detalle


def test_el_mismo_avion_del_libro_pasa():
    assert verificar_matricula_del_libro(
        [registro(matricula="HP-1848CMP", log="2287340")],
        {"22873A": "HP-1848CMP"},
    ) == []


def test_un_libro_del_que_airvault_no_sabe_nada_no_bloquea():
    assert verificar_matricula_del_libro(
        [registro(matricula="HP-1852CMP", log="2299901")],
        {"22873A": "HP-1848CMP"},
    ) == []


def test_el_sufijo_del_1522_no_cuenta_como_otro_avion():
    """AirVault solo lo tiene como CMP; una carga vieja pudo decir WWP."""
    assert verificar_matricula_del_libro(
        [registro(matricula="HP-1522WWP", log="2287340")],
        {"22873A": "HP-1522CMP"},
    ) == []


def test_un_separador_no_lleva_avion_que_comparar():
    divisoria = Registro(seq=1, es_separador=True)

    assert verificar_matricula_del_libro(
        [divisoria], {"22873A": "HP-1848CMP"}
    ) == []
