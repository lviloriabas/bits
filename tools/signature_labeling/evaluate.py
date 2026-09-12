"""Medición y contabilidad de aciertos del detector de firmas.

Este módulo es el puente entre las etiquetas humanas y el detector: mide los
recortes con la función real del detector (``_measure``), decide con las
mismas reglas (``_classify`` para un caso, ``classify_vector`` para miles a la
vez) y cuenta los errores con los pesos que tienen en la práctica.

Los dos errores no cuestan lo mismo:

- **Falso presente** (había que reclamar una firma y el sistema la da por
  puesta): el peor. Es una falta que desaparece del reporte, y nadie la va a
  buscar porque el sistema dijo que estaba.
- **Falso ausente** (hay firma y el sistema la reclama): acusa de una falta
  inexistente. Cuesta credibilidad y una revisión.
- **Incierto**: solo cuesta una revisión manual; el reporte lo marca REVISAR.

De ahí que el coste por defecto sea 6 / 4 / 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.templates.schema import FieldTemplate
from app.vision.signature import (
    _EMPTY_COVERAGE,
    _SPREAD_PEAK,
    _SPREAD_SPAN,
    _WEAK_PEAK_GUARD,
    SIGNATURE_PAD_X,
    SIGNATURE_PAD_Y,
    UNCLEAR,
    _classify,
    _measure,
    detect_signature,
)
from tools.signature_labeling.dataset import (
    EXPECTED_VALUE,
    LABEL_ABSENT,
    LABEL_PRESENT,
    Dataset,
    Sample,
)

# Orden de los rasgos dentro de la matriz de características.
FEATURE_NAMES = ("peak", "weak_peak", "coverage", "span", "dark_ratio")

DEFAULT_TEMPLATE = Path(__file__).resolve().parents[2] / "template" / "aircraft_log.json"

COST_FALSE_PRESENT = 6.0
COST_FALSE_ABSENT = 4.0
COST_UNCLEAR = 1.0


@dataclass(frozen=True)
class Thresholds:
    """Los seis números que deciden una firma.

    ``pad_x``/``pad_y`` son constantes del módulo del detector y el resto son
    campos de la plantilla; se manejan juntos porque el margen del recorte
    cambia lo que miden los demás y no tiene sentido calibrarlos por separado.
    """

    pad_x: float = SIGNATURE_PAD_X
    pad_y: float = SIGNATURE_PAD_Y
    ink_delta: float = 80.0
    min_ink_peak: float = 0.12
    max_empty_peak: float = 0.05
    min_ink_span: float = 0.30
    max_ink_ratio: float = 0.60

    @classmethod
    def from_field(cls, field: FieldTemplate) -> "Thresholds":
        return cls(
            pad_x=SIGNATURE_PAD_X,
            pad_y=SIGNATURE_PAD_Y,
            ink_delta=field.ink_delta,
            min_ink_peak=field.min_ink_peak,
            max_empty_peak=field.max_empty_peak,
            min_ink_span=field.min_ink_span,
            max_ink_ratio=field.max_ink_ratio,
        )

    def describe(self) -> str:
        return (
            f"pad {self.pad_x:.2f}/{self.pad_y:.2f} · "
            f"ink_delta {self.ink_delta:.0f} · "
            f"min_ink_peak {self.min_ink_peak:.3f} · "
            f"max_empty_peak {self.max_empty_peak:.3f} · "
            f"min_ink_span {self.min_ink_span:.2f}"
        )


def signature_fields(template) -> Dict[str, FieldTemplate]:
    """Campos de firma de la plantilla, por id."""
    from app.templates.schema import FieldType

    return {
        field.id: field for field in template.fields
        if field.type is FieldType.SIGNATURE
    }


def load_template(path: Path):
    from app.templates.manager import TemplateManager

    return TemplateManager().load(path)


_TEMPLATE_CACHE: Dict[str, FieldTemplate] = {}


def _template_fields() -> Dict[str, FieldTemplate]:
    """Campos de firma de la plantilla por defecto (se leen una sola vez)."""
    if not _TEMPLATE_CACHE:
        _TEMPLATE_CACHE.update(signature_fields(load_template(DEFAULT_TEMPLATE)))
    return _TEMPLATE_CACHE


def verdict_for_sample(
    dataset: Dataset, sample: Sample, field: Optional[FieldTemplate] = None
) -> str:
    """Veredicto del detector tal cual corre en el pipeline.

    Sin atajos: recorta con el margen de producción y llama a
    ``detect_signature``. Es la referencia con la que se compara cualquier
    ajuste propuesto.
    """
    if field is None:
        field = _template_fields().get(sample.field_id)
    if field is None:
        return UNCLEAR
    region = dataset.load_crop_padded(sample, SIGNATURE_PAD_X, SIGNATURE_PAD_Y)
    if region is None:
        return UNCLEAR
    return detect_signature(region, field, sample.page, dpi=sample.dpi).value


def measure_samples(
    dataset: Dataset,
    samples: Sequence[Sample],
    fields: Dict[str, FieldTemplate],
    pad_x: float,
    pad_y: float,
    ink_delta: float,
) -> np.ndarray:
    """Matriz ``(n, 5)`` de rasgos, uno por muestra, con ese margen y umbral.

    Las filas cuyo recorte no se pudo leer quedan en NaN; el contador las
    descarta en lugar de inventarles un valor.
    """
    rows = np.full((len(samples), len(FEATURE_NAMES)), np.nan, dtype=np.float64)
    for index, sample in enumerate(samples):
        field = fields.get(sample.field_id)
        if field is None:
            continue
        region = dataset.load_crop_padded(sample, pad_x, pad_y)
        if region is None or region.size == 0:
            continue
        metrics = _measure(
            region, field.model_copy(update={"ink_delta": ink_delta}),
            sample.dpi,
        )
        rows[index] = [metrics[name] for name in FEATURE_NAMES]
    return rows


def decision_masks(
    features: np.ndarray, thresholds: Thresholds
) -> Tuple[np.ndarray, np.ndarray]:
    """Máscaras ``(es_firma, es_ausencia)``; el resto de filas son inciertas.

    Es la cadena de decisiones de ``_classify`` escrita con máscaras de numpy,
    para poder recorrer miles de combinaciones de umbrales sin repetir la
    medición. Existe una sola vez: tanto ``classify_vector`` como la búsqueda
    de ``tune.py`` pasan por aquí, y ``tests/test_signature_labeling.py``
    comprueba fila a fila que coincide con ``_classify``. Eso es lo que impide
    calibrar una regla distinta de la que corre en producción.
    """
    peak = features[:, 0]
    weak_peak = features[:, 1]
    coverage = features[:, 2]
    span = features[:, 3]
    dark_ratio = features[:, 4]

    usable = ~np.isnan(peak) & (dark_ratio <= thresholds.max_ink_ratio)
    present = (peak >= thresholds.min_ink_peak) | (
        (peak >= thresholds.max_empty_peak) & (span >= thresholds.min_ink_span)
    )
    spread_ink = (peak >= _SPREAD_PEAK) & (span >= _SPREAD_SPAN)
    empty = (
        (peak < thresholds.max_empty_peak)
        & (coverage < _EMPTY_COVERAGE)
        & (weak_peak < _WEAK_PEAK_GUARD)
        & ~spread_ink
    )
    return usable & present, usable & ~present & empty


def classify_vector(features: np.ndarray, thresholds: Thresholds) -> np.ndarray:
    """Veredictos ("true"/"false"/"unclear") de una matriz de rasgos."""
    is_true, is_false = decision_masks(features, thresholds)
    verdicts = np.full(features.shape[0], UNCLEAR, dtype=object)
    verdicts[is_true] = "true"
    verdicts[is_false] = "false"
    return verdicts


@dataclass
class Score:
    """Resultado de contrastar unos veredictos con las etiquetas humanas."""

    total: int = 0
    correct: int = 0
    false_present: int = 0  # etiqueta "ausente", veredicto "true"
    false_absent: int = 0   # etiqueta "firma", veredicto "false"
    unclear_present: int = 0  # etiqueta "firma", veredicto incierto
    unclear_absent: int = 0   # etiqueta "ausente", veredicto incierto

    @property
    def unclear(self) -> int:
        return self.unclear_present + self.unclear_absent

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def cost(
        self,
        false_present: float = COST_FALSE_PRESENT,
        false_absent: float = COST_FALSE_ABSENT,
        unclear: float = COST_UNCLEAR,
    ) -> float:
        """Coste medio por muestra (comparable entre conjuntos distintos)."""
        if not self.total:
            return float("inf")
        return (
            self.false_present * false_present
            + self.false_absent * false_absent
            + self.unclear * unclear
        ) / self.total

    def line(self) -> str:
        return (
            f"aciertos {self.correct}/{self.total} ({self.accuracy:6.1%})  "
            f"falsos presentes {self.false_present:>3}  "
            f"falsos ausentes {self.false_absent:>3}  "
            f"inciertos {self.unclear:>3}"
        )


def score_verdicts(verdicts: Sequence[str], labels: Sequence[str]) -> Score:
    """Cuenta aciertos y errores; las filas sin medir (None) se descartan."""
    score = Score()
    for verdict, label in zip(verdicts, labels):
        if verdict is None:
            continue
        expected = EXPECTED_VALUE.get(label)
        if expected is None:  # "dudosa": fuera de las métricas
            continue
        score.total += 1
        if verdict == expected:
            score.correct += 1
        elif verdict == UNCLEAR:
            if label == LABEL_PRESENT:
                score.unclear_present += 1
            else:
                score.unclear_absent += 1
        elif label == LABEL_ABSENT:
            score.false_present += 1
        else:
            score.false_absent += 1
    return score


def scalar_verdict(metrics: Dict[str, float], field: FieldTemplate) -> str:
    """Veredicto de una muestra usando la función del detector."""
    return _classify(metrics, field)[0]


def features_to_metrics(row: Sequence[float]) -> Dict[str, float]:
    return dict(zip(FEATURE_NAMES, (float(value) for value in row)))


def apply_thresholds(field: FieldTemplate, thresholds: Thresholds) -> FieldTemplate:
    """Copia del campo con los umbrales propuestos."""
    return field.model_copy(update={
        "ink_delta": thresholds.ink_delta,
        "min_ink_peak": thresholds.min_ink_peak,
        "max_empty_peak": thresholds.max_empty_peak,
        "min_ink_span": thresholds.min_ink_span,
        "max_ink_ratio": thresholds.max_ink_ratio,
    })


def confusion(
    verdicts: Sequence[str], labels: Sequence[str]
) -> List[Tuple[str, str, int]]:
    """Tabla (etiqueta, veredicto, cuántos) para el informe."""
    counter: Dict[Tuple[str, str], int] = {}
    for verdict, label in zip(verdicts, labels):
        key = (label, str(verdict))
        counter[key] = counter.get(key, 0) + 1
    return sorted(
        ((label, verdict, count) for (label, verdict), count in counter.items()),
        key=lambda item: (item[0], item[1]),
    )
