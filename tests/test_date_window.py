"""Ventana temporal en la que puede caer la fecha de una bitácora."""

from __future__ import annotations

from datetime import date

from app.utils.date_window import (
    date_is_possible,
    days_outside_usual,
    is_usual,
    month_end_date,
    needs_review_for_age,
    review_start,
    year_is_possible,
    years_outside_usual,
)

HOY = date(2026, 9, 5)


def test_el_ano_de_la_ejecucion_y_los_anteriores_son_posibles():
    assert year_is_possible(2026, HOY)
    assert year_is_possible(2025, HOY)
    assert year_is_possible(2000, HOY)


def test_un_ano_posterior_a_la_ejecucion_no_existe():
    # Una página no se firma después del día en que se escanea: un '96' o
    # un '28' leídos en 2026 son el '26' mal leído.
    assert not year_is_possible(2096, HOY)
    assert not year_is_possible(2028, HOY)
    assert not year_is_possible(1999, HOY)


def test_la_fecha_no_puede_superar_el_dia_de_ejecucion():
    assert date_is_possible(HOY, HOY)
    assert not date_is_possible(date(2026, 9, 6), HOY)
    assert not date_is_possible(date(2026, 12, 31), HOY)
    assert not date_is_possible(date(2027, 1, 1), HOY)


def test_fin_de_mes_admite_el_mes_actual_pero_no_meses_futuros():
    assert month_end_date(2026, 8, HOY) == date(2026, 8, 31)
    assert month_end_date(2026, 9, HOY) == date(2026, 9, 30)
    assert month_end_date(2026, 10, HOY) is None
    assert month_end_date(2027, 1, HOY) is None
    assert month_end_date(2024, 2, HOY) == date(2024, 2, 29)
    assert month_end_date(2026, 0, HOY) is None


def test_el_mes_actual_y_el_anterior_son_lo_habitual():
    assert is_usual(HOY, HOY)
    assert is_usual(date(2026, 8, 20), HOY)
    assert is_usual(date(2026, 8, 1), HOY)
    assert not is_usual(date(2026, 7, 22), HOY)
    assert not is_usual(date(2026, 6, 1), HOY)


def test_solo_el_fin_de_mes_puede_ser_un_respaldo_adelantado():
    assert not date_is_possible(date(2026, 9, 30), HOY)
    assert date_is_possible(date(2026, 9, 30), HOY, allow_month_end=True)
    assert not date_is_possible(date(2026, 9, 6), HOY, allow_month_end=True)
    assert not date_is_possible(date(2026, 10, 31), HOY, allow_month_end=True)
    assert not date_is_possible(date(2027, 1, 31), HOY, allow_month_end=True)


def test_lo_antiguo_se_ordena_por_detras_pero_no_se_descarta():
    assert days_outside_usual(date(2026, 6, 1), HOY) > 0
    assert date_is_possible(date(2020, 7, 25), HOY)


def test_los_anos_fuera_no_se_mueven_por_el_borde_de_la_ventana():
    # Dentro del año en curso todo puntúa igual, dentro y fuera de la
    # ventana habitual, así que el borde no decide nada. Lo que destaca es
    # la página que un año mal leído manda años fuera.
    assert years_outside_usual(date(2026, 8, 20), HOY) == 0
    assert years_outside_usual(date(2026, 6, 1), HOY) == 0
    assert years_outside_usual(date(2024, 8, 20), HOY) == 1
    assert years_outside_usual(date(2020, 8, 20), HOY) == 5


def test_fuera_de_enero_solo_el_ano_actual_evade_revision():
    assert review_start(HOY) == date(2026, 1, 1)
    assert not needs_review_for_age(HOY, HOY)
    assert not needs_review_for_age(date(2026, 1, 3), HOY)
    assert needs_review_for_age(date(2025, 12, 31), HOY)
    assert needs_review_for_age(date(2024, 1, 1), HOY)
    assert needs_review_for_age(date(2023, 12, 31), HOY)
    assert needs_review_for_age(date(2020, 8, 20), HOY)
    assert needs_review_for_age(date(2001, 5, 4), HOY)


def test_enero_admite_la_cola_del_ano_anterior():
    enero = date(2026, 1, 31)
    assert review_start(enero) == date(2025, 1, 1)
    assert not needs_review_for_age(date(2025, 1, 1), enero)
    assert needs_review_for_age(date(2024, 12, 31), enero)


def test_desde_febrero_el_ano_anterior_vuelve_a_revision():
    febrero = date(2026, 2, 1)
    assert review_start(febrero) == date(2026, 1, 1)
    assert needs_review_for_age(date(2025, 12, 31), febrero)
