"""Pruebas de la suite de etiquetado y calibración de firmas.

Lo importante que se comprueba aquí es que la calibración mide *lo mismo* que
el detector:

- el recorte simulado desde el PNG guardado coincide píxel a píxel con el que
  ``crop_region`` saca de la página;
- la decisión vectorizada de la búsqueda de umbrales da el mismo veredicto que
  ``_classify``, fila a fila.

Si alguna de las dos se rompe, la calibración optimizaría una cosa distinta de
la que corre en producción y nadie se enteraría.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np

from app.templates.schema import FieldTemplate
from app.vision.preprocessing import crop_region
from app.vision.signature import _classify, _measure
from tools.signature_labeling.dataset import (
    EXTRACT_PAD_X,
    EXTRACT_PAD_Y,
    LABEL_ABSENT,
    LABEL_PRESENT,
    LABEL_UNSURE,
    Dataset,
    Sample,
    crop_with_pad,
    pad_pixels,
    sample_id,
)
from tools.signature_labeling.evaluate import (
    Thresholds,
    classify_vector,
    score_verdicts,
)

PAGE_W, PAGE_H = 1200, 1600


def _campo(**kw) -> FieldTemplate:
    valores = dict(id="firma", type="signature", required=True,
                   x=0.30, y=0.40, w=0.25, h=0.05)
    valores.update(kw)
    return FieldTemplate(**valores)


def _pagina() -> np.ndarray:
    """Página con ruido: dos recortes distintos no pueden salir iguales."""
    rng = np.random.default_rng(11)
    noise = rng.integers(0, 60, size=(PAGE_H, PAGE_W), dtype=np.uint8)
    page = np.full((PAGE_H, PAGE_W), 245, dtype=np.uint8) - noise
    return cv2.cvtColor(page, cv2.COLOR_GRAY2BGR)


class TestRecorteSimulado(unittest.TestCase):
    """El PNG guardado tiene que poder estrecharse al margen del detector."""

    def test_reproduce_el_recorte_de_produccion(self):
        page = _pagina()
        campo = _campo()
        guardado = crop_region(page, campo,
                               pad_x=EXTRACT_PAD_X, pad_y=EXTRACT_PAD_Y)
        left, top, right, bottom = campo.rect_pixels(PAGE_W, PAGE_H)
        rect = [
            pad_pixels(EXTRACT_PAD_X, right - left),
            pad_pixels(EXTRACT_PAD_Y, bottom - top),
            pad_pixels(EXTRACT_PAD_X, right - left) + (right - left),
            pad_pixels(EXTRACT_PAD_Y, bottom - top) + (bottom - top),
        ]
        for pad_x, pad_y in ((0.0, 0.10), (0.05, 0.0), (0.10, 0.20)):
            with self.subTest(pad_x=pad_x, pad_y=pad_y):
                esperado = crop_region(page, campo, pad_x=pad_x, pad_y=pad_y)
                obtenido = crop_with_pad(guardado, rect, pad_x, pad_y)
                self.assertEqual(obtenido.shape, esperado.shape)
                self.assertTrue(np.array_equal(obtenido, esperado))


class TestDecisionVectorizada(unittest.TestCase):
    """La búsqueda de umbrales decide igual que el detector."""

    def test_coincide_con_classify(self):
        rng = np.random.default_rng(3)
        # Rasgos en el rango real: densidades y coberturas entre 0 y 1.
        features = np.column_stack([
            rng.random(400) * 0.5,   # peak
            rng.random(400) * 0.5,   # weak_peak
            rng.random(400) * 0.1,   # coverage
            rng.random(400),         # span
            rng.random(400) * 0.8,   # dark_ratio
        ])
        for umbrales in (
            Thresholds(),
            Thresholds(min_ink_peak=0.20, max_empty_peak=0.02,
                       min_ink_span=0.55),
            Thresholds(min_ink_peak=0.06, max_empty_peak=0.06,
                       min_ink_span=1.0, max_ink_ratio=0.30),
            Thresholds(min_ink_peak=0.13, max_empty_peak=0.02,
                       min_ink_span=0.50, min_ink_coverage=0.024),
        ):
            campo = _campo(
                min_ink_peak=umbrales.min_ink_peak,
                max_empty_peak=umbrales.max_empty_peak,
                min_ink_span=umbrales.min_ink_span,
                min_ink_coverage=umbrales.min_ink_coverage,
                max_ink_ratio=umbrales.max_ink_ratio,
            )
            vectorizado = classify_vector(features, umbrales)
            for index, row in enumerate(features):
                esperado, _conf, _texto = _classify(
                    dict(zip(("peak", "weak_peak", "coverage", "span",
                              "dark_ratio"), row)),
                    campo,
                )
                self.assertEqual(vectorizado[index], esperado,
                                 f"fila {index} con {umbrales.describe()}")

    def test_mide_con_la_funcion_del_detector(self):
        """Los rasgos de la calibración son los del detector, no una copia."""
        page = _pagina()
        campo = _campo()
        region = crop_region(page, campo, pad_x=0.0, pad_y=0.10)
        metrics = _measure(region, campo, 200)
        self.assertEqual(
            set(metrics), {"peak", "weak_peak", "coverage", "span", "dark_ratio"}
        )


class TestConteoDeErrores(unittest.TestCase):
    def test_pesa_cada_error_donde_duele(self):
        etiquetas = [LABEL_ABSENT, LABEL_PRESENT, LABEL_PRESENT, LABEL_ABSENT]
        veredictos = ["true", "false", "true", "unclear"]
        score = score_verdicts(veredictos, etiquetas)
        self.assertEqual(score.total, 4)
        self.assertEqual(score.correct, 1)
        self.assertEqual(score.false_present, 1)
        self.assertEqual(score.false_absent, 1)
        self.assertEqual(score.unclear, 1)
        # 6 + 4 + 1 repartidos entre 4 muestras.
        self.assertAlmostEqual(score.cost(), 11 / 4)

    def test_las_dudosas_no_cuentan(self):
        score = score_verdicts(["true", "false"], [LABEL_UNSURE, LABEL_UNSURE])
        self.assertEqual(score.total, 0)


class TestConjunto(unittest.TestCase):
    """El etiquetado no puede perderse al volver a extraer ni al guardar."""

    def _dataset(self, root: Path) -> Dataset:
        (root / "recortes").mkdir(parents=True)
        dataset = Dataset(root=root)
        for page in (1, 2):
            identifier = sample_id("libro.pdf", page, "captain_signature")
            relative = f"recortes/{identifier}.png"
            cv2.imwrite(str(root / relative),
                        np.full((60, 200, 3), 240, dtype=np.uint8))
            dataset.samples.append(Sample(
                id=identifier, pdf="libro.pdf", page=page,
                field_id="captain_signature", file=relative, dpi=200,
                alignment="ok", rect=[10, 8, 190, 52],
            ))
        return dataset

    def test_guarda_y_recupera_etiquetas(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = self._dataset(root)
            dataset.save_manifest()
            dataset.labels[dataset.samples[0].id] = LABEL_PRESENT
            dataset.save_labels()

            recargado = Dataset.load(root)
            self.assertEqual(len(recargado.samples), 2)
            self.assertEqual(
                recargado.labels[dataset.samples[0].id], LABEL_PRESENT
            )
            self.assertEqual(recargado.counts()["etiquetadas"], 1)
            self.assertEqual(len(recargado.labeled()), 1)

    def test_el_archivo_de_etiquetas_no_queda_a_medias(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = self._dataset(root)
            dataset.save_manifest()
            dataset.labels[dataset.samples[0].id] = LABEL_ABSENT
            dataset.save_labels()
            payload = json.loads(
                (root / "labels.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["counts"][LABEL_ABSENT], 1)
            self.assertFalse(list(root.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
