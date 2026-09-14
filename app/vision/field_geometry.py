"""Refina las anclas cerca de cada campo usando solo la pagina actual.

La reticula completa identifica las rayas. Los perfiles locales miden donde
pasan junto al campo, sin imponer a toda la hoja el mismo desplazamiento.
Una raya ausente solo se recupera si sus vecinas la sitúan y aparece una
unica linea en su ventana. No se busca desde coordenadas fijas sin evidencia.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.templates.schema import Template
from app.vision.alignment import printed_structure
from app.vision.reticula import Reticula, leer_reticula, rect_de_campo


@dataclass(frozen=True)
class FieldPlacement:
    template: Template
    unverified: tuple[str, ...] = ()
    lines: int = 0


def _positions(pattern: list[float], measured: dict[int, float]) -> dict[int, float]:
    """Interpola huecos interiores para buscar, nunca para dar por medidos."""
    if len(measured) < 2:
        return dict(measured)
    indices = sorted(measured)
    reference = [pattern[j] for j in indices]
    positions = [measured[j] for j in indices]
    return {
        i: float(np.interp(pattern[i], reference, positions))
        for i in range(indices[0], indices[-1] + 1)
    }


def _peaks(profile: np.ndarray, dimension: int) -> np.ndarray:
    """Solo rayas finas que cubren la mayor parte de la banda observada."""
    strong = profile >= 0.55
    edges = np.flatnonzero(np.diff(np.r_[False, strong, False]))
    starts, ends = edges[::2], edges[1::2]
    thin = ends - starts <= max(3, round(dimension * 0.003))
    return (starts[thin] + ends[thin] - 1) * 0.5 / dimension


def _refine_axis(measured, predicted, profile, needed):
    dimension = profile.size
    peaks = _peaks(profile, dimension)
    refined = dict(measured)
    ambiguous = set()
    for index in needed:
        if index not in predicted:
            continue
        position = predicted[index]
        gaps = [abs(predicted[j] - position) for j in (index - 1, index + 1)
                if j in predicted]
        if not gaps:
            continue
        radius = min(0.006, min(gaps) * 0.30)
        candidates = peaks[np.abs(peaks - position) <= radius]
        if candidates.size == 1:
            refined[index] = float(candidates[0])
        elif candidates.size > 1:
            ambiguous.add(index)
    # No aceptar una pareja que cambie mucho su separacion local.
    for index in sorted(needed):
        if index not in refined or index + 1 not in refined:
            continue
        if index not in predicted or index + 1 not in predicted:
            continue
        expected = predicted[index + 1] - predicted[index]
        gap = refined[index + 1] - refined[index]
        if expected <= 0 or not 0.6 <= gap / expected <= 1.4:
            ambiguous.update((index, index + 1))
    return refined, ambiguous


def _needed(first: int, first_offset: float, last: int, last_offset: float,
            size: int) -> set[int]:
    indices = {first, last}
    for index, offset in ((first, first_offset), (last, last_offset)):
        if offset != 0 and index + 1 < size:
            indices.add(index + 1)
    return indices


def place_fields(template: Template, image: np.ndarray) -> FieldPlacement:
    """Una estructura por pagina, perfiles pequenos por campo, sin OCR extra.

    Sin evidencia suficiente conserva el recorte de respaldo y devuelve el
    campo en ``unverified``. Nunca presenta una coordenada fija como verificada.
    """
    pattern = template.reticula
    if pattern is None:
        return FieldPlacement(template)
    structure = printed_structure(image)
    grid = leer_reticula(template, image, structure)
    predicted_x = _positions(pattern.x, grid.x)
    predicted_y = _positions(pattern.y, grid.y)
    height, width = structure.shape[:2]
    seed = Reticula(x=predicted_x, y=predicted_y,
                    escala_x=grid.escala_x, escala_y=grid.escala_y, fiable=True)
    # Las celdas manuscritas comparten la banda DD|MMM|AA. Un perfil del
    # ancho de un solo caracter puede confundir su trazo con una raya.
    date_rects = [rect_de_campo(f, seed, template) for f in template.fields
                  if f.id in ("day", "month", "year")]
    date_band = None
    if len(date_rects) == 3 and all(r is not None for r in date_rects):
        left = min(r[0] for r in date_rects)
        top = min(r[1] for r in date_rects)
        right = max(r[0] + r[2] for r in date_rects)
        bottom = max(r[1] + r[3] for r in date_rects)
        date_band = (left, top, right - left, bottom - top)
    fields, unverified = [], []
    for field in template.fields:
        anchor = field.ancla
        if anchor is None:
            fields.append(field)
            continue
        required_x = ({anchor.raya_izquierda, anchor.raya_derecha}
                      if anchor.raya_izquierda is not None else set())
        needed_y = _needed(anchor.raya_arriba, anchor.arriba,
                           anchor.raya_abajo, anchor.abajo, len(pattern.y))
        needed_x = (_needed(anchor.raya_izquierda, anchor.izquierda,
                             anchor.raya_derecha, anchor.derecha, len(pattern.x))
                    if required_x else set())
        search = rect_de_campo(field, seed, template)
        rect = None
        if search is not None:
            if (date_band is not None
                    and field.id.split("_", 1)[0] in ("day", "month", "year")):
                search = date_band
            x, y, w, h = search
            # Observar el interior del campo, sin los cruces de sus bordes.
            left = max(0, int((x + w * 0.1) * width))
            right = min(width, int((x + w * 0.9) * width))
            top = max(0, int((y + h * 0.1) * height))
            bottom = min(height, int((y + h * 0.9) * height))
            if right > left and bottom > top:
                profile_y = np.count_nonzero(structure[:, left:right], axis=1) / (right - left)
                profile_x = np.count_nonzero(structure[top:bottom, :], axis=0) / (bottom - top)
                local_y, ambiguous_y = _refine_axis(grid.y, predicted_y, profile_y, needed_y)
                local_x, ambiguous_x = _refine_axis(grid.x, predicted_x, profile_x, needed_x)
                local = Reticula(x=local_x, y=local_y,
                                 escala_x=grid.escala_x, escala_y=grid.escala_y, fiable=True)
                if not (ambiguous_x & needed_x or ambiguous_y & needed_y):
                    rect = rect_de_campo(field, local, template)
        if rect is None:
            unverified.append(field.id)
            # Conservar cualquier ajuste global comprobado, sin fingir
            # que la comprobacion local paso.
            rect = rect_de_campo(field, grid, template)
        if rect is None:
            fields.append(field)
        else:
            fields.append(field.model_copy(
                update=dict(zip(("x", "y", "w", "h"), rect)),
            ))
    return FieldPlacement(template.model_copy(update={"fields": fields}),
                          tuple(unverified), len(grid.x) + len(grid.y))
