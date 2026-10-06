"""Traduccion del CSV de la ejecucion a los valores de AirVault."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from app.airvault.config import (
    CAMPO_AUDIT_STATUS,
    CAMPO_DESCRIPCION,
    CAMPO_DOC_TYPE,
    CAMPO_END_DATE,
    CAMPO_FLEET,
    CAMPO_LOG_NUMBER,
    CAMPO_MATRICULA,
)
from app.airvault.mapping import (
    ResolutorFlota,
    fecha_a_fin_de_mes,
    fecha_airvault,
    normalizar_log_number,
    normalizar_matricula,
    obligatorios_vacios_por_pagina,
    registros_desde_csv,
    registros_desde_entrega,
    valores_de_indice,
)


def test_fecha_del_csv_a_airvault():
    assert fecha_airvault("2026/08/31") == "08/31/2026"


def test_no_envia_fechas_imposibles():
    for valor in ("2026/02/29", "2026/04/31", "2026/13/01", "2026/00/12"):
        assert fecha_airvault(valor) == ""
    assert fecha_airvault("2024/02/29") == "02/29/2024"


@patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
def test_no_envia_lecturas_futuras_pero_admite_fin_de_mes_actual(_clock):
    for value in ("2026/10/07", "2026/11/01", "2028/02/29"):
        assert fecha_airvault(value) == ""
    assert fecha_airvault("2026/10/06") == "10/06/2026"
    assert fecha_a_fin_de_mes("2026/10/01") == "2026/10/31"
    assert fecha_airvault("2026/10/31") == "10/31/2026"
    assert fecha_a_fin_de_mes("2026/11/01") == ""


@patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
def test_indexado_admite_fin_del_mes_actual_como_respaldo(_clock):
    registro = registros_desde_csv([_fila(date="2026/10/02")], fin_de_mes=True)[0]
    assert registro.fecha == "2026/10/31"
    assert valores_de_indice(registro, "Log Page", "PUBLISHED")[CAMPO_END_DATE] == "10/31/2026"


@patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
def test_fecha_futura_solo_se_recupera_con_evidencia_posible(_clock):
    filas = [
        _fila(page="1", log_number="2287310", date="2026/10/02"),
        _fila(page="2", log_number="2287311", date="2026/10/07"),
        _fila(page="3", log_number="2287312", date="2026/10/05"),
    ]
    registro = registros_desde_csv(filas)[1]
    assert registro.fecha == "2026/10/05"
    assert registro.fecha_inferida == "entre bitacoras del libro"
    assert valores_de_indice(registro, "Log Page", "PUBLISHED")[CAMPO_END_DATE] == "10/05/2026"

    registro_solo = registros_desde_csv([filas[1]])[0]
    assert registro_solo.fecha == ""
    assert valores_de_indice(registro_solo, "Log Page", "PUBLISHED")[CAMPO_END_DATE] == ""


def test_fecha_invalida_queda_vacia():
    # Mejor un obligatorio vacio que la guarda acuse, a inventar una fecha.
    for valor in ("", "31/08/2026", "2026-08-31", "basura", None):
        assert fecha_airvault(valor) == ""


def test_matricula_se_normaliza():
    assert normalizar_matricula(" hp-1848cmp ") == "HP-1848CMP"
    assert normalizar_matricula("HK-4453") == "HK-4453"


def test_matricula_1522_sube_siempre_como_cmp():
    # AirVault tiene ese avion como HP-1522CMP; la bitacora lo trae escrito
    # de las dos maneras y la flota local lo guarda con WWP.
    assert normalizar_matricula("HP-1522WWP") == "HP-1522CMP"
    assert normalizar_matricula("HP-1522CMP") == "HP-1522CMP"
    assert normalizar_matricula(" hp-1522wwp ") == "HP-1522CMP"


def test_alias_de_1522_llega_a_los_valores_de_indice():
    registros = registros_desde_csv([
        {"file": "b.pdf", "page": "1", "matricula": "HP-1522WWP",
         "log_number": "2287325", "date": "2026/08/31"},
    ])
    valores = valores_de_indice(registros[0], "Log Page", "PUBLISHED")
    assert valores[CAMPO_MATRICULA] == "HP-1522CMP"


def test_matricula_invalida_queda_vacia():
    for valor in ("", "1848", "HP-184CMP", "XX-1848CMP"):
        assert normalizar_matricula(valor) == ""


def test_log_number_de_siete_digitos():
    assert normalizar_log_number("2287325") == "2287325"
    assert normalizar_log_number(" 2287325 ") == "2287325"
    assert normalizar_log_number("228732") == ""
    assert normalizar_log_number("22873250") == ""


def test_flota_conocida_no_se_infiere():
    resolutor = ResolutorFlota({"HP-1848CMP": {"fleet": "NG",
                                               "lessor": "SMBC A.C"}})
    fleet, lessor, inferido = resolutor.resolver("HP-1848CMP")
    assert (fleet, lessor, inferido) == ("NG", "SMBC A.C", False)


def test_flota_desconocida_se_infiere_y_se_avisa():
    fleet, _lessor, inferido = ResolutorFlota().resolver("HP-9924CMP")
    assert fleet == "MAX"
    assert inferido is True


def test_aprender_deja_de_inferir():
    resolutor = ResolutorFlota()
    resolutor.aprender("HP-9812CMP", "MAX", "COPA")
    fleet, lessor, inferido = resolutor.resolver("HP-9812CMP")
    assert (fleet, lessor, inferido) == ("MAX", "COPA", False)


def test_matricula_vacia_no_resuelve_flota():
    assert ResolutorFlota().resolver("") == ("", "", False)


def _fila(**kwargs):
    base = {"file": "Image_001.pdf", "page": "1", "log_number": "2287325",
            "matricula": "HP-1848CMP", "date": "2026/08/31", "dup": "false",
            "disc": "false"}
    base.update(kwargs)
    return base


def test_registros_conservan_el_orden_del_csv():
    filas = [_fila(page="1"), _fila(page="2", log_number="2287326")]
    registros = registros_desde_csv(filas)
    assert [r.seq for r in registros] == [1, 2]
    assert [r.log_number for r in registros] == ["2287325", "2287326"]


def test_paginas_en_blanco_no_entran():
    # Si entraran, la correspondencia con las paginas del batch se correria.
    filas = [_fila(page="1"),
             _fila(page="2", log_number="", matricula="", date=""),
             _fila(page="3", log_number="2287327")]
    registros = registros_desde_csv(filas)
    assert [r.seq for r in registros] == [1, 2]
    assert [r.pagina_origen for r in registros] == [1, 3]


def test_orden_explicito_manda_sobre_el_csv():
    filas = [_fila(page="1", log_number="2287325"),
             _fila(page="2", log_number="2287326")]
    registros = registros_desde_csv(
        filas, orden=[("Image_001.pdf", 2), ("Image_001.pdf", 1)]
    )
    assert [r.log_number for r in registros] == ["2287326", "2287325"]


def test_banderas_del_csv_viajan():
    registros = registros_desde_csv([_fila(dup="true", disc="true")])
    assert registros[0].duplicado is True
    assert registros[0].discrepancia is True


def test_una_bitacora_sin_fecha_la_hereda_de_su_libro():
    """End Date es obligatorio: sin fecha la pagina no se puede escribir."""
    filas = [_fila(page="1", log_number="2287310", date="2026/08/03"),
             _fila(page="2", log_number="2287311", date=""),
             _fila(page="3", log_number="2287312", date="2026/08/28")]
    registros = registros_desde_csv(filas)
    valores = valores_de_indice(registros[1], "Log Page", "PUBLISHED")
    assert valores[CAMPO_END_DATE] == "08/28/2026"
    assert registros[1].fecha_inferida == "entre bitacoras del libro"


def test_la_fecha_leida_no_queda_marcada_como_deducida():
    registros = registros_desde_csv([_fila(date="2026/08/31")])
    assert registros[0].fecha == "2026/08/31"
    assert registros[0].fecha_inferida == ""


def test_sin_log_number_la_fecha_sigue_vacia():
    """No hay libro con el que ubicarla, y el propio log_number la bloquea."""
    filas = [_fila(page="1", log_number="2287310", date="2026/08/03"),
             _fila(page="2", log_number="", date="")]
    registros = registros_desde_csv(filas)
    assert registros[1].fecha == ""
    assert registros[1].fecha_inferida == ""


def test_detecta_solo_los_obligatorios_que_quedarian_vacios():
    filas = [
        _fila(page="1", log_number="2287310", date="2026/08/03"),
        _fila(page="2", log_number="2287311", date=""),
        _fila(page="3", log_number="2287312", date="2026/08/28"),
        _fila(page="4", log_number="", date=""),
    ]

    faltantes = obligatorios_vacios_por_pagina(filas)

    assert ("Image_001.pdf", 2) not in faltantes
    assert faltantes[("Image_001.pdf", 4)] == (
        "Log Page Number", "End Date"
    )


def test_valores_de_indice_llevan_los_seis_obligatorios():
    registro = registros_desde_csv([_fila()])[0]
    valores = valores_de_indice(registro, "Log Page", "PUBLISHED")
    assert valores[CAMPO_DOC_TYPE] == "Log Page"
    assert valores[CAMPO_MATRICULA] == "HP-1848CMP"
    assert valores[CAMPO_FLEET] == "NG"
    assert valores[CAMPO_LOG_NUMBER] == "2287325"
    assert valores[CAMPO_AUDIT_STATUS] == "PUBLISHED"
    assert valores[CAMPO_END_DATE] == "08/31/2026"


def test_una_discrepancia_lleva_su_propio_audit_status():
    """Es lo unico que la distingue en AirVault del resto del batch."""
    registro = registros_desde_csv([_fila(disc="true")])[0]
    valores = valores_de_indice(
        registro, "Log Page", "PUBLISHED", "DP | PRUEBA", "AUDIT IN PROGRESS"
    )
    assert valores[CAMPO_AUDIT_STATUS] == "AUDIT IN PROGRESS"


def test_sin_discrepancia_el_audit_status_es_el_del_trabajo():
    registro = registros_desde_csv([_fila()])[0]
    valores = valores_de_indice(
        registro, "Log Page", "PUBLISHED", "DP | PRUEBA", "AUDIT IN PROGRESS"
    )
    assert valores[CAMPO_AUDIT_STATUS] == "PUBLISHED"


def test_sin_audit_status_de_discrepancia_no_se_cambia_nada():
    """Vacio en la configuracion deja a todas con el Audit Status normal."""
    registro = registros_desde_csv([_fila(disc="true")])[0]
    valores = valores_de_indice(registro, "Log Page", "PUBLISHED")
    assert valores[CAMPO_AUDIT_STATUS] == "PUBLISHED"


def test_no_se_mandan_campos_que_el_sistema_no_controla():
    # Lo que no se manda, AirVault lo conserva: asi un indexado no pisa lo
    # que alguien puso a mano.
    registro = registros_desde_csv([_fila()])[0]
    valores = valores_de_indice(registro, "Log Page", "PUBLISHED")
    assert 9625 not in valores  # WO #
    assert 9594 not in valores  # Start Date


def test_el_vuelo_de_la_bitacora_va_en_description():
    """Es la informacion de vuelo que la bitacora trae escrita."""
    registro = registros_desde_csv([_fila(flight_number="CM137")])[0]
    valores = valores_de_indice(registro, "Log Page", "PUBLISHED")
    assert valores[CAMPO_DESCRIPCION] == "CM137"


def test_un_vuelo_de_mantenimiento_viaja_igual():
    """No todos son numeros: ``TCK`` y compania son vuelos tambien."""
    registro = registros_desde_csv([_fila(flight_number="tck")])[0]
    valores = valores_de_indice(registro, "Log Page", "PUBLISHED")
    assert valores[CAMPO_DESCRIPCION] == "TCK"


def test_sin_vuelo_leido_no_se_toca_description():
    """Mandarlo vacio borraria lo que alguien haya escrito a mano."""
    registro = registros_desde_csv([_fila(flight_number="")])[0]
    valores = valores_de_indice(registro, "Log Page", "PUBLISHED")
    assert CAMPO_DESCRIPCION not in valores


# ── el PDF apartado con el nombre numerado ─────────────────────────
#
# Al terminar una ejecucion sus PDF se guardan en «input/processed», y si
# alli ya habia uno con ese nombre el nuevo se numera. El reporte pasa a
# apuntar al numerado; el CSV conserva el nombre con el que se leyo. Un
# indice escrito con el numerado no encontraba ni una sola fila, y el batch
# entero salia sin matricula, sin log y sin fecha: en AirVault eso son
# cuatrocientas paginas amarillas que el indexado se niega a escribir.

def test_el_indice_numerado_encuentra_igual_su_fila():
    filas = [_fila(file="bitacora.pdf", page="1")]
    indice = [{"archivo": "bitacora-2.pdf", "pagina": 1}]

    registro = registros_desde_entrega(filas, indice)[0]

    assert registro.matricula == "HP-1848CMP"
    assert registro.log_number == "2287325"
    assert registro.fecha == "2026/08/31"


def test_con_un_solo_pdf_la_pagina_manda_aunque_el_nombre_no_cuadre():
    """Un solo archivo no deja lugar a dudas: la pagina 1 es la pagina 1.

    Vale para cualquier renombrado, no solo para el sufijo numerado, que es
    lo que hace falta cuando no se sabe con que regla cambio el nombre.
    """
    filas = [_fila(file="bitacora.pdf", page="1")]
    indice = [{"archivo": "otro-nombre-cualquiera.pdf", "pagina": 1}]

    registro = registros_desde_entrega(filas, indice)[0]

    assert registro.matricula == "HP-1848CMP"


def test_con_varios_pdf_no_se_adivina_y_la_pagina_queda_anotada():
    """Con dos archivos la pagina 1 es ambigua: adivinar seria escribir mal."""
    filas = [_fila(file="uno.pdf", page="1"),
             _fila(file="dos.pdf", page="1", log_number="2287326")]
    indice = [{"archivo": "otra.pdf", "pagina": 1}]

    registro = registros_desde_entrega(filas, indice)[0]

    assert registro.matricula == ""
    assert any("sin_fila" in aviso for aviso in registro.avisos)


def test_una_pagina_que_no_esta_en_el_csv_se_anota():
    """El rescate no puede tapar una entrega descuadrada de verdad."""
    filas = [_fila(file="bitacora.pdf", page="1")]
    indice = [{"archivo": "bitacora.pdf", "pagina": 7}]

    registro = registros_desde_entrega(filas, indice)[0]

    assert registro.matricula == ""
    assert any("sin_fila" in aviso for aviso in registro.avisos)


def test_fecha_dudosa_del_indice_no_se_infiere_ni_se_envia():
    filas = [_fila(date="2024/08/20")]
    indice = [{
        "archivo": "Image_001.pdf",
        "pagina": 1,
        "fecha_dudosa": True,
    }]

    registro = registros_desde_entrega(filas, indice)[0]
    valores = valores_de_indice(registro, "Log Page", "PUBLISHED")

    assert registro.fecha == ""
    assert registro.fecha_dudosa is True
    assert CAMPO_END_DATE not in valores


# ── fecha de fin de mes al indexar ─────────────────────────────────
#
# Una ejecucion exportada con el dia exacto todavia puede indexarse a fin de
# mes: la conversion va sobre lo que trae el CSV, asi que no hace falta
# volver a procesarla. Al reves no, y de eso se ocupa la ventana.


def test_la_fecha_pasa_al_ultimo_dia_de_su_mes():
    assert fecha_a_fin_de_mes("2026/07/14") == "2026/07/31"
    assert fecha_a_fin_de_mes("2026/02/03") == "2026/02/28"


def test_un_ano_bisiesto_cierra_en_29():
    assert fecha_a_fin_de_mes("2024/02/10") == "2024/02/29"


def test_una_fecha_que_ya_es_fin_de_mes_no_cambia():
    assert fecha_a_fin_de_mes("2026/07/31") == "2026/07/31"


def test_lo_que_no_es_una_fecha_se_devuelve_igual():
    # Aqui no se inventa una fecha que no estaba.
    assert fecha_a_fin_de_mes("") == ""
    assert fecha_a_fin_de_mes("no es una fecha") == "no es una fecha"


def test_los_registros_se_pueden_construir_a_fin_de_mes():
    filas = [{
        "file": "a.pdf", "page": "1", "log_number": "2287325",
        "matricula": "HP-1848CMP", "date": "2026/07/14",
    }]

    normal = registros_desde_csv(filas)
    fin = registros_desde_csv(filas, fin_de_mes=True)

    assert normal[0].fecha == "2026/07/14"
    assert fin[0].fecha == "2026/07/31"
