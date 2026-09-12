"""Estimación de tiempo basada en throughput real de pared."""

from __future__ import annotations


def wall_ms_per_page(
    elapsed_seconds: float, completed_pages: float
) -> float | None:
    """Costo efectivo por página, incluyendo la concurrencia del batch."""
    if elapsed_seconds <= 0 or completed_pages <= 0:
        return None
    return elapsed_seconds * 1000.0 / completed_pages


def estimate_remaining_seconds(
    *,
    total_pages: float,
    completed_pages: float,
    elapsed_seconds: float,
    cached_ms_per_page: float,
    warmup_units: float = 20.0,
) -> float | None:
    """Estima lo pendiente mezclando historial y throughput observado.

    El ritmo observado usa tiempo de pared del batch, no la suma de tiempos de
    cada worker. Durante las primeras páginas conserva más peso del historial
    para evitar saltos por la inicialización del modelo; a partir de veinte
    páginas el batch actual domina en 90%.

    ``warmup_units`` es cuántas unidades hacen falta para llegar a ese 90%.
    Veinte es lo que pide una página, que es una medida menuda y ruidosa. Un
    trabajo cuya unidad es una bitácora entera llega antes: cada una ya es
    una muestra larga, y esperar veinte sería no aprender nunca, porque casi
    ninguna corrida tiene tantas.

    Las cantidades pueden ser fraccionarias: la ventana cuenta en unidades de
    página, donde cada hoja de la comprobación de VOID vale lo que pesa
    frente a una página leída.
    """
    if total_pages <= 0:
        return None
    pending = max(0, total_pages - max(0, completed_pages))
    if pending == 0:
        return 0.0
    cached = max(1.0, float(cached_ms_per_page))
    observed = wall_ms_per_page(elapsed_seconds, completed_pages)
    if observed is None:
        rate = cached
    else:
        observed_weight = min(0.90, completed_pages / max(1.0, warmup_units))
        rate = cached * (1.0 - observed_weight) + observed * observed_weight
    return pending * rate / 1000.0

