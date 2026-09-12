#!/usr/bin/env python3
"""Calibra la retícula de una plantilla: el patrón de rayas y las anclas.

Se ejecuta una vez por formulario, no en cada corrida. Hace dos cosas:

1. **El patrón.** Mide las rayas impresas en una muestra de páginas de varios
   libros y se queda con las que aparecen en casi todas. Su posición es la
   mediana de las medidas, y esa mediana solo sirve para *nombrar* las rayas:
   es la lista cuyos índices usan las anclas. En ejecución, la posición con la
   que se recorta sale siempre de la página que se está leyendo.

2. **Las anclas.** Para cada campo busca las dos rayas del patrón que lo
   delimitan y guarda sus bordes como fracciones de ese hueco. Las saca de las
   coordenadas que ya tiene la plantilla, así que el campo no se mueve de
   donde estaba: lo que cambia es que a partir de ahora sigue a la raya
   impresa en vez de al borde del lienzo.

Uso típico (intérprete portable del proyecto)::

    portable/python312/tools/python.exe tools/reticula_calibrar.py \\
        --input input/processed --paginas-por-pdf 6

    portable/python312/tools/python.exe tools/reticula_calibrar.py --aplicar
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.templates.manager import TemplateManager  # noqa: E402
from app.templates.schema import (  # noqa: E402
    AnclaCampo,
    FieldTemplate,
    PatronReticula,
    Template,
)
from app.vision.pdf_loader import render_page  # noqa: E402
from app.vision.reticula import (  # noqa: E402
    UMBRAL_PERFIL,
    centros_de_rayas,
    perfiles_de_reticula,
    registrar,
)

DEFAULT_TEMPLATE = ROOT / "template" / "aircraft_log.json"

# Fracción del lienzo por debajo de la cual dos medidas de páginas distintas
# se consideran la misma raya, una vez registradas contra la página semilla.
# Registrar primero es imprescindible: entre libros la retícula se desplaza
# hasta 0,005 del lienzo, más que la separación a la que habría que agrupar,
# así que agrupando posiciones crudas la misma raya cae en grupos distintos
# y el patrón sale con agujeros (medido: 32 rayas en vez de 40).
AGRUPA = 0.0015

# Presencia mínima para que una raya entre en el patrón. Con menos, el patrón
# se llena de rayas que solo salen en algunos libros y el casado de las demás
# páginas las deja sin pareja sin que eso signifique nada.
PRESENCIA = 0.80


def _pdf_list(entries: Sequence[str]) -> List[Path]:
    """PDFs a medir: archivos sueltos o el contenido de una carpeta."""
    paths: List[Path] = []
    for entry in entries:
        path = Path(entry)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.pdf")))
        elif path.is_file():
            paths.append(path)
    return paths


def _paginas(total: int, cuantas: int) -> List[int]:
    """Páginas repartidas por todo el libro, no las primeras."""
    if total <= cuantas:
        return list(range(1, total + 1))
    paso = total / float(cuantas + 1)
    return [max(1, min(total, round(paso * (i + 1)))) for i in range(cuantas)]


def _medir(
    pdfs: Sequence[Path], por_pdf: int, dpi: int
) -> Tuple[List[np.ndarray], List[np.ndarray], int]:
    """Rayas detectadas en cada página de la muestra."""
    import pypdfium2

    medidas_x: List[np.ndarray] = []
    medidas_y: List[np.ndarray] = []
    for pdf in pdfs:
        try:
            total = len(pypdfium2.PdfDocument(str(pdf)))
        except Exception as exc:  # noqa: BLE001 - PDF ilegible
            print(f"  {pdf.name}: no se pudo abrir ({exc})")
            continue
        leidas = 0
        for pagina in _paginas(total, por_pdf):
            try:
                imagen = render_page(pdf, pagina, dpi)
            except Exception as exc:  # noqa: BLE001 - página ilegible
                print(f"  {pdf.name} p{pagina}: no renderizable ({exc})")
                continue
            perfil_x, perfil_y = perfiles_de_reticula(imagen)
            medidas_x.append(centros_de_rayas(perfil_x, UMBRAL_PERFIL))
            medidas_y.append(centros_de_rayas(perfil_y, UMBRAL_PERFIL))
            leidas += 1
        print(f"  {pdf.name}: {leidas} página(s) de {total}")
    return medidas_x, medidas_y, len(medidas_y)


def patron_de_medidas(
    medidas: Sequence[np.ndarray], paginas: int
) -> List[float]:
    """Rayas que salen en casi todas las páginas, con su posición mediana.

    Primero registra cada página contra la que más rayas trajo, porque entre
    libros la retícula entera se desplaza y se escala un poco; agrupar sin
    registrar mezcla rayas vecinas y parte las propias. Luego agrupa por
    proximidad (cada grupo es una raya del formulario) y se queda con los
    grupos presentes en al menos ``PRESENCIA`` de las páginas, para que el
    patrón no incluya rayas que solo tiene un libro.

    La mediana de cada grupo es la posición que se guarda, y sirve solo para
    nombrar la raya: en ejecución, la posición con la que se recorta sale de
    la página que se está leyendo.
    """
    llenas = [m for m in medidas if m.size]
    if not llenas:
        return []
    semilla = max(llenas, key=len)
    registradas: List[np.ndarray] = []
    for medida in llenas:
        escala, desfase, _pareja = registrar(medida, semilla)
        registradas.append(medida * escala + desfase)
    todas = np.sort(np.concatenate(registradas))
    grupos: List[List[float]] = [[float(todas[0])]]
    for valor in todas[1:]:
        if valor - grupos[-1][-1] <= AGRUPA:
            grupos[-1].append(float(valor))
        else:
            grupos.append([float(valor)])
    minimo = max(2, int(round(PRESENCIA * paginas)))
    return [
        float(np.median(grupo)) for grupo in grupos if len(grupo) >= minimo
    ]


def _raya_de_borde(
    patron: Sequence[float], borde: float
) -> Optional[Tuple[int, float]]:
    """Raya más cercana a un borde y lo que sobra, en fracciones del hueco.

    Cada borde se ata a *su* raya, no a un par compartido: así el error de
    detección se traslada al borde tal cual en vez de amplificarse cuando las
    dos rayas quedan lejos (medido: 9,6 px de recorrido con el par compartido
    frente a 3 px atando cada borde por separado).
    """
    if len(patron) < 2:
        return None
    array = np.asarray(patron, dtype=np.float64)
    indice = int(np.abs(array - borde).argmin())
    # El hueco se mide hacia abajo; en la última raya no hay siguiente, así
    # que se toma el hueco anterior y el índice se queda donde está.
    siguiente = indice + 1 if indice + 1 < len(patron) else indice - 1
    hueco = abs(array[siguiente] - array[indice])
    if hueco <= 0:
        return None
    return indice, (borde - array[indice]) / hueco


def ancla_de_campo(
    campo: FieldTemplate, patron_y: Sequence[float], patron_x: Sequence[float]
) -> Optional[AnclaCampo]:
    """Ancla que reproduce el rectángulo actual del campo sobre las rayas."""
    superior = _raya_de_borde(patron_y, campo.y)
    inferior = _raya_de_borde(patron_y, campo.y + campo.h)
    if superior is None or inferior is None:
        return None
    if inferior[0] < superior[0]:
        return None
    raya_izquierda = raya_derecha = None
    izquierda = derecha = 0.0
    izq = _raya_de_borde(patron_x, campo.x)
    der = _raya_de_borde(patron_x, campo.x + campo.w)
    if izq is not None and der is not None and der[0] >= izq[0]:
        raya_izquierda, izquierda = izq
        raya_derecha, derecha = der
    return AnclaCampo(
        raya_arriba=superior[0],
        arriba=round(superior[1], 6),
        raya_abajo=inferior[0],
        abajo=round(inferior[1], 6),
        raya_izquierda=raya_izquierda,
        izquierda=round(izquierda, 6),
        raya_derecha=raya_derecha,
        derecha=round(derecha, 6),
    )


def _informe(
    plantilla: Template, patron_y: Sequence[float], patron_x: Sequence[float]
) -> Dict[str, AnclaCampo]:
    """Ancla de cada campo, con el desvío que corrige, e imprime el resumen."""
    anclas: Dict[str, AnclaCampo] = {}
    alto_px = plantilla.page_size[1] if len(plantilla.page_size) > 1 else 1272
    print(f"\n{'campo':<24}{'rayas y':>10}{'arriba':>9}{'abajo':>9}"
          f"{'rayas x':>10}{'desvio px':>11}")
    for campo in plantilla.fields:
        ancla = ancla_de_campo(campo, patron_y, patron_x)
        if ancla is None:
            print(f"{campo.id:<24}   sin rayas suficientes")
            continue
        anclas[campo.id] = ancla
        desvio = abs(campo.y - patron_y[ancla.raya_arriba]) * alto_px
        rayas_x = (
            f"{ancla.raya_izquierda}-{ancla.raya_derecha}"
            if ancla.raya_izquierda is not None else "(ninguna)"
        )
        print(f"{campo.id:<24}"
              f"{str(ancla.raya_arriba) + '-' + str(ancla.raya_abajo):>10}"
              f"{ancla.arriba:>+9.3f}{ancla.abajo:>+9.3f}{rayas_x:>10}"
              f"{desvio:>11.1f}")
    return anclas


def _aplicar(
    ruta: Path, patron_y: Sequence[float], patron_x: Sequence[float],
    anclas: Dict[str, AnclaCampo],
) -> None:
    """Escribe patrón y anclas en el JSON sin tocar nada más."""
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    datos["reticula"] = {
        "x": [round(v, 6) for v in patron_x],
        "y": [round(v, 6) for v in patron_y],
    }
    for campo in datos.get("fields", []):
        ancla = anclas.get(campo.get("id"))
        if ancla is None:
            campo.pop("ancla", None)
            continue
        campo["ancla"] = ancla.model_dump(exclude_none=True)
    ruta.write_text(
        json.dumps(datos, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nEscrito en {ruta}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calibra el patrón de retícula y las anclas de los campos",
    )
    parser.add_argument(
        "--input", nargs="+", default=["input/processed"],
        help="PDFs o carpetas con PDFs de los que medir la retícula",
    )
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument(
        "--paginas-por-pdf", type=int, default=6, dest="por_pdf",
        help="páginas a medir de cada libro, repartidas por todo él",
    )
    parser.add_argument(
        "--dpi", type=int, default=200,
        help="resolución de medida (la de trabajo, por defecto)",
    )
    parser.add_argument(
        "--aplicar", action="store_true",
        help="escribir el patrón y las anclas en la plantilla",
    )
    args = parser.parse_args()

    pdfs = _pdf_list(args.input)
    if not pdfs:
        print("No se encontró ningún PDF.", file=sys.stderr)
        return 1
    plantilla = TemplateManager().load(args.template)

    print(f"Midiendo la retícula en {len(pdfs)} libro(s) a {args.dpi} dpi:")
    medidas_x, medidas_y, paginas = _medir(pdfs, args.por_pdf, args.dpi)
    if paginas < 2:
        print("Hacen falta al menos dos páginas medibles.", file=sys.stderr)
        return 1
    patron_x = patron_de_medidas(medidas_x, paginas)
    patron_y = patron_de_medidas(medidas_y, paginas)
    print(f"\nPatrón: {len(patron_x)} rayas verticales y {len(patron_y)} "
          f"horizontales, presentes en >= {PRESENCIA:.0%} de {paginas} páginas")

    anclas = _informe(plantilla, patron_y, patron_x)
    if not anclas:
        print("No se pudo anclar ningún campo.", file=sys.stderr)
        return 1
    if args.aplicar:
        _aplicar(args.template, patron_y, patron_x, anclas)
    else:
        print("\nNada escrito. Repite con --aplicar para guardarlo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
