from app.gui.eta import estimate_remaining_seconds, wall_ms_per_page


def test_wall_rate_uses_elapsed_batch_time_not_worker_sum():
    # Cuatro workers terminan 20 páginas en 40 s. El costo efectivo es 2 s/p,
    # aunque sumar los tiempos individuales podría producir 8 s/p.
    assert wall_ms_per_page(40.0, 20) == 2000.0


def test_initial_estimate_uses_cached_rate():
    assert estimate_remaining_seconds(
        total_pages=100,
        completed_pages=0,
        elapsed_seconds=0.0,
        cached_ms_per_page=2500.0,
    ) == 250.0


def test_live_estimate_converges_to_wall_throughput():
    remaining = estimate_remaining_seconds(
        total_pages=100,
        completed_pages=20,
        elapsed_seconds=40.0,
        cached_ms_per_page=8000.0,
    )
    # Con veinte páginas, 90% del peso corresponde a los 2 s/p observados.
    assert remaining == 208.0


def test_completed_batch_has_zero_remaining():
    assert estimate_remaining_seconds(
        total_pages=12,
        completed_pages=12,
        elapsed_seconds=9.0,
        cached_ms_per_page=2500.0,
    ) == 0.0


def test_pocas_unidades_grandes_pesan_mas_que_pocas_paginas():
    """Web Reports mide bitacoras enteras, no paginas: con tres ya se sabe.

    Con el calentamiento de paginas, tres de veinte apenas moverian la
    estimacion del historico, y casi ninguna corrida de Web Reports llega a
    las veinte unidades: seria no mirar nunca lo que esta pasando.
    """
    comun = dict(
        total_pages=10,
        completed_pages=3,
        elapsed_seconds=30.0,
        cached_ms_per_page=30000.0,
    )
    # Observado: 10 s/unidad, que son 70 s de las siete que quedan. Guardado:
    # 30 s/unidad, que son 210.
    assert estimate_remaining_seconds(**comun, warmup_units=3.0) == 84.0
    assert estimate_remaining_seconds(**comun) == 189.0
