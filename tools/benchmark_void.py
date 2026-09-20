"""Compara la ruta anterior de VOID con oneDNN sin cambiar los documentos."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ocr.engine import PaddleOcrEngine
from app.vision.pdf_loader import render_page
from app.vision.void_mark import DPI_VOID, MODELO_VOID, detectar_void


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("muestra", type=Path, help="JSON con archivo y pagina por caso")
    parser.add_argument("--pdf-dir", type=Path, default=ROOT / "input/processed")
    parser.add_argument("--salida", type=Path, required=True)
    parser.add_argument("--limite", type=int, default=0)
    args = parser.parse_args()
    casos = json.loads(args.muestra.read_text(encoding="utf-8"))
    if args.limite:
        casos = casos[:args.limite]
    motores = {
        "anterior": PaddleOcrEngine(cpu_threads=4, rec_model=MODELO_VOID),
        "optimizado": PaddleOcrEngine(cpu_threads=1, rec_model=MODELO_VOID, rec_mkldnn=True),
    }
    cargas = {}
    for nombre, motor in motores.items():
        inicio = perf_counter()
        motor._ensure_recognizer()
        cargas[nombre] = perf_counter() - inicio
    salida = {"python": platform.python_version(), "modelo": MODELO_VOID,
              "dpi": DPI_VOID, "carga_s": cargas, "casos": []}
    for caso in casos:
        imagen = render_page(args.pdf_dir / caso["archivo"], caso["pagina"], DPI_VOID)
        fila = {k: caso[k] for k in ("archivo", "pagina", "void_visual") if k in caso}
        for nombre, motor in motores.items():
            inicio = perf_counter()
            marca = detectar_void(imagen, motor)
            fila[nombre] = {"segundos": perf_counter() - inicio,
                            "marca": marca.model_dump() if marca else None}
        fila["misma_decision"] = bool(fila["anterior"]["marca"]) == bool(fila["optimizado"]["marca"])
        salida["casos"].append(fila)
        args.salida.parent.mkdir(parents=True, exist_ok=True)
        args.salida.write_text(json.dumps(salida, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(fila, ensure_ascii=True), flush=True)
    return 0 if all(f["misma_decision"] for f in salida["casos"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
