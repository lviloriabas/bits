#!/usr/bin/env python3
"""Mide la revisión por fondo del libro contra las etiquetas humanas.

Compara dos cosas sobre los mismos recortes:

* el detector clásico solo (``app/vision/signature.py``);
* el detector clásico más la segunda opinión que le da el resto de la
  bitácora (``app/vision/book_background.py``), que es lo que corre en el
  pipeline desde que existe ``Pipeline._review_signatures``.

Reproduce el procedimiento del pipeline paso por paso (muestra de páginas
repartida por el libro, fondo mediano, franja aprendida de los veredictos
firmes) y usa las funciones de producción, no copias. Cada bitácora se mide
por separado, porque el fondo es del libro: mezclar dos escaneos en la misma
mediana daría un formulario que no es el de ninguno de los dos.

Uso::

    portable/python312/tools/python.exe \
        tools/signature_labeling/evaluate_background.py

Lo que hay que mirar es la columna de errores **graves**: dar por firmada una
página sin firmar, o reclamar una firma que sí está. Un incierto de más solo
cuesta una revisión; un grave se cuela en el reporte.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.utils.portable import ensure_portable_env  # noqa: E402

ensure_portable_env()

from app.core.config import AppConfig  # noqa: E402
from app.vision.book_background import (  # noqa: E402
    MIN_BACKGROUND_PAGES,
    build_background,
    confident_band,
)
from app.vision.signature import (  # noqa: E402
    UNCLEAR,
    background_peak,
    review_with_background,
)
from tools.signature_labeling.dataset import (  # noqa: E402
    EXPECTED_VALUE,
    LABEL_ABSENT,
    LABEL_PRESENT,
    Dataset,
    Sample,
)
from tools.signature_labeling.evaluate import verdict_for_sample  # noqa: E402

DEFAULT_DIR = ROOT / "output" / "firmas_dataset"

# El recorte se toma sin margen, igual que en el paso del pipeline: con el
# fondo restado, el margen solo añade tinta de la casilla vecina.
PAD = 0.0


class Tally:
    """Aciertos, inciertos y errores graves de una configuración."""

    def __init__(self) -> None:
        self.correct = 0
        self.unclear = 0
        self.serious = 0

    def add(self, verdict: str, label: str) -> None:
        if verdict == EXPECTED_VALUE[label]:
            self.correct += 1
        elif verdict == UNCLEAR:
            self.unclear += 1
        else:
            self.serious += 1

    @property
    def total(self) -> int:
        return self.correct + self.unclear + self.serious

    def line(self) -> str:
        return (f"{self.correct:>3} aciertos, {self.unclear:>2} inciertos, "
                f"{self.serious:>2} graves")


def _sample_pages(samples: Sequence[Sample], size: int) -> List[Sample]:
    """Muestra repartida de la primera a la última página, como el pipeline."""
    count = min(len(samples), size)
    if count <= 1:
        return list(samples[:count])
    return [
        samples[round(index * (len(samples) - 1) / (count - 1))]
        for index in range(count)
    ]


def evaluate_book(
    dataset: Dataset,
    book: str,
    samples: Sequence[Sample],
    size: int,
    verbose: bool,
) -> tuple:
    """Mide un libro; devuelve (clásico, clásico+revisión)."""
    classic_tally, reviewed_tally = Tally(), Tally()
    by_field: Dict[str, List[Sample]] = defaultdict(list)
    for sample in samples:
        by_field[sample.field_id].append(sample)

    for field_id, field_samples in sorted(by_field.items()):
        from tools.signature_labeling.evaluate import _template_fields
        field = _template_fields()[field_id]
        field_samples = sorted(field_samples, key=lambda item: item.page)
        classic = {
            sample.id: verdict_for_sample(dataset, sample)
            for sample in field_samples
        }
        crops = {
            sample.id: dataset.load_crop_padded(sample, PAD, PAD)
            for sample in field_samples
        }
        chosen = _sample_pages(field_samples, size)
        background = (
            build_background([crops[item.id] for item in chosen],
                             field_samples[0].dpi)
            if len(field_samples) >= MIN_BACKGROUND_PAGES else None
        )
        band = None
        peaks: Dict[str, float] = {}
        if background is not None:
            peaks = {
                sample.id: background_peak(crops[sample.id], background)
                for sample in field_samples
            }
            band = confident_band(
                [peaks[item.id] for item in chosen],
                [classic[item.id] for item in chosen],
            )
        if verbose:
            print(f"    {field_id:<22} "
                  + (f"franja [{band[0]:.4f}, {band[1]:.4f}]" if band
                     else "sin franja: el libro no separa con claridad"))

        for sample in field_samples:
            label = dataset.labels.get(sample.id)
            if label not in (LABEL_PRESENT, LABEL_ABSENT):
                continue
            verdict = classic[sample.id]
            classic_tally.add(verdict, label)
            reviewed = verdict
            if verdict == UNCLEAR and band is not None and field.book_background:
                opinion = review_with_background(
                    peaks[sample.id], band,
                    allow_absent=field_id not in ("captain_license", "technician_license"),
                )
                if opinion is not None:
                    reviewed = opinion[0]
                    mark = ("resuelta" if reviewed == EXPECTED_VALUE[label]
                            else "MAL RESUELTA")
                    print(f"      {mark:<13} {sample.id:<38} "
                          f"etiqueta={label:<8} -> {reviewed}")
            reviewed_tally.add(reviewed, label)
    return classic_tally, reviewed_tally


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mide la revisión de firmas por fondo del libro",
    )
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument(
        "--paginas", type=int, default=AppConfig().signature_background_pages,
        help="páginas muestreadas por libro (las mismas que usa el pipeline)",
    )
    parser.add_argument("--bitacora", help="medir solo este PDF")
    parser.add_argument("--detalle", action="store_true",
                        help="mostrar la franja aprendida en cada campo")
    args = parser.parse_args()

    try:
        dataset = Dataset.load(args.dir)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    by_book: Dict[str, List[Sample]] = defaultdict(list)
    for sample in dataset.samples:
        if dataset.labels.get(sample.id) in (LABEL_PRESENT, LABEL_ABSENT):
            by_book[sample.pdf].append(sample)
    if args.bitacora:
        by_book = {k: v for k, v in by_book.items() if k == args.bitacora}
    if not by_book:
        print("No hay recortes etiquetados que medir.", file=sys.stderr)
        return 1

    total_classic, total_reviewed = Tally(), Tally()
    for book, samples in sorted(by_book.items()):
        pages = len({sample.page for sample in samples})
        print(f"\n{book}  ({len(samples)} recortes etiquetados de "
              f"{pages} páginas)")
        classic, reviewed = evaluate_book(
            dataset, book, samples, args.paginas, args.detalle
        )
        print(f"  {'detector clásico':<24} {classic.line()}")
        print(f"  {'+ revisión del libro':<24} {reviewed.line()}")
        for source, target in ((classic, total_classic),
                               (reviewed, total_reviewed)):
            target.correct += source.correct
            target.unclear += source.unclear
            target.serious += source.serious

    if len(by_book) > 1:
        print(f"\nTodas las bitácoras ({total_classic.total} recortes)")
        print(f"  {'detector clásico':<24} {total_classic.line()}")
        print(f"  {'+ revisión del libro':<24} {total_reviewed.line()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
