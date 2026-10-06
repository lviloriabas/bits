"""Fecha deducida para las bitacoras que llegan sin ella.

End Date es obligatorio: una bitacora sin fecha bloquea su pagina y deja el
batch sin poder cerrarse. Estas pruebas fijan de donde sale la fecha que se
le pone y, sobre todo, hasta donde llega la deduccion.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from app.airvault.fechas import (
    METODO_ENTRE_ANCLAS,
    METODO_FIN_MES_AVION,
    METODO_FIN_MES_EJECUCION,
    METODO_FIN_MES_LIBRO,
    METODO_MISMA_BITACORA,
    fechas_inferidas,
)


def _fila(page, log_number, date="", matricula="HP-1848CMP", file="A.pdf"):
    return {"file": file, "page": str(page), "log_number": log_number,
            "matricula": matricula, "date": date}


@patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
def test_todos_los_rellenos_del_mes_actual_admiten_fin_de_mes(_clock):
    filas = [
        _fila(1, "2287310", "2026/10/02"),
        _fila(2, "2287311"),
        _fila(3, "3344500"),
        _fila(4, "4455600", matricula="HP-1534CMP"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 2)] == ("2026/10/31", METODO_FIN_MES_LIBRO)
    assert inferidas[("A.pdf", 3)] == ("2026/10/31", METODO_FIN_MES_AVION)
    assert inferidas[("A.pdf", 4)] == ("2026/10/31", METODO_FIN_MES_EJECUCION)


@patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
def test_fechas_futuras_no_anclan_el_libro_ni_la_ejecucion(_clock):
    filas = [
        _fila(1, "2287310", "2026/10/07"),
        _fila(2, "2287311", "2026/11/01"),
        _fila(3, "2287312", "2028/02/29"),
        _fila(4, "2287313"),
    ]
    assert fechas_inferidas(filas) == {}


def test_sin_fecha_entre_dos_fechadas_del_libro():
    """La fecha no retrocede en el libro: la de al lado cae en el intervalo."""
    filas = [
        _fila(1, "2287310", "2026/08/03"),
        _fila(2, "2287311"),
        _fila(3, "2287315", "2026/08/28"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 2)] == ("2026/08/03", METODO_ENTRE_ANCLAS)


def test_en_un_empate_manda_la_bitacora_posterior():
    """Es la ultima fecha que cabe en el hueco, como el dia del CSV."""
    filas = [
        _fila(1, "2287310", "2026/08/03"),
        _fila(2, "2287311"),
        _fila(3, "2287312", "2026/08/28"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 2)] == ("2026/08/28", METODO_ENTRE_ANCLAS)


def test_despues_de_la_ultima_fechada_va_el_fin_de_mes():
    filas = [
        _fila(1, "2287310", "2026/08/03"),
        _fila(2, "2287311", "2026/08/04"),
        _fila(3, "2287320"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 3)] == ("2026/08/31", METODO_FIN_MES_LIBRO)


def test_antes_de_la_primera_fechada_manda_esa_fecha():
    """Hacia atras no hay fin de mes que valga: seria una fecha posterior."""
    filas = [
        _fila(1, "2287305"),
        _fila(2, "2287310", "2026/08/03"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 1)] == ("2026/08/03", METODO_ENTRE_ANCLAS)


def test_la_misma_bitacora_repetida_presta_su_fecha():
    """Una duplicada que si se leyo no es una deduccion, es el mismo dato."""
    filas = [
        _fila(1, "2287310"),
        _fila(2, "2287310", "2026/08/03"),
        _fila(3, "2287340", "2026/08/20"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 1)] == ("2026/08/03", METODO_MISMA_BITACORA)


def test_la_regla_del_libro_no_cruza_libros():
    """Otro libro del mismo avion no ordena a este: solo aporta su mes."""
    filas = [
        _fila(1, "2287310", "2026/07/04"),
        _fila(2, "2287311", "2026/07/09"),
        # Otra serie: es otro libro, aunque sea el mismo avion.
        _fila(3, "2299920"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 3)] == ("2026/07/31", METODO_FIN_MES_AVION)


def test_las_dos_mitades_de_una_serie_son_libros_distintos():
    filas = [
        _fila(1, "2287310", "2026/07/04"),
        _fila(2, "2287315", "2026/07/09"),
        _fila(3, "2287360"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 3)][1] == METODO_FIN_MES_AVION


def test_un_avion_sin_ninguna_fecha_cae_al_mes_de_la_ejecucion():
    filas = [
        _fila(1, "2287310", "2026/08/04"),
        _fila(2, "2287315", "2026/08/09"),
        _fila(3, "2299920", matricula="HP-1849CMP"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 3)] == ("2026/08/31", METODO_FIN_MES_EJECUCION)


def test_el_mes_dominante_gana_al_que_tiene_menos_bitacoras():
    filas = [
        _fila(1, "2287310", "2026/07/28", matricula="HP-1849CMP"),
        _fila(2, "2287315", "2026/08/04", matricula="HP-1849CMP"),
        _fila(3, "2287316", "2026/08/09", matricula="HP-1849CMP"),
        _fila(4, "2299920", matricula="HP-1850CMP"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 4)] == ("2026/08/31", METODO_FIN_MES_EJECUCION)


def test_sin_log_number_no_se_deduce_nada():
    """Sin numero no hay libro ni posicion, y la pagina queda bloqueada igual."""
    filas = [
        _fila(1, "2287310", "2026/08/03"),
        _fila(2, ""),
        _fila(3, "228731"),
    ]
    inferidas = fechas_inferidas(filas)
    assert ("A.pdf", 2) not in inferidas
    assert ("A.pdf", 3) not in inferidas


def test_una_fecha_leida_no_se_toca():
    filas = [_fila(1, "2287310", "2026/08/03"), _fila(2, "2287311", "2026/08/09")]
    assert fechas_inferidas(filas) == {}


def test_sin_una_sola_fecha_en_la_ejecucion_no_se_inventa():
    """Una fecha sacada de la nada seria peor que la pagina bloqueada."""
    filas = [_fila(1, "2287310"), _fila(2, "2287311")]
    assert fechas_inferidas(filas) == {}


def test_febrero_de_un_ano_bisiesto_termina_el_29():
    filas = [
        _fila(1, "2287310", "2024/02/03"),
        _fila(2, "2287311", "2024/02/04"),
        _fila(3, "2287320"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("A.pdf", 3)] == ("2024/02/29", METODO_FIN_MES_LIBRO)


def test_las_anclas_se_buscan_en_toda_la_ejecucion():
    """Un libro repartido entre dos escaneos sigue siendo un solo libro."""
    filas = [
        _fila(1, "2287310", "2026/08/03", file="A.pdf"),
        _fila(1, "2287311", file="B.pdf"),
        _fila(2, "2287312", "2026/08/03", file="A.pdf"),
    ]
    inferidas = fechas_inferidas(filas)
    assert inferidas[("B.pdf", 1)] == ("2026/08/03", METODO_ENTRE_ANCLAS)
