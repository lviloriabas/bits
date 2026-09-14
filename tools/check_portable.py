"""Comprueba requisitos reales e imports; repara paquetes incompletos con pip.

Solo lo ejecuta setup. No descarga nada con --check y no se usa al abrir BITS.
"""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import subprocess
import sys

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name


IMPORTS = {
    "opencv-python": "cv2", "numpy": "numpy", "pymupdf": "pymupdf",
    "pillow": "PIL.Image", "paddlepaddle": "paddle", "paddleocr": "paddleocr",
    "paddlex": "paddlex", "pyside6": "PySide6.QtWidgets",
    "send2trash": "send2trash", "pydantic": "pydantic", "loguru": "loguru",
    "requests": "requests", "truststore": "truststore",
}


def requirements(path: Path) -> list[Requirement]:
    result = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            req = Requirement(line)
            if req.marker is None or req.marker.evaluate():
                result.append(req)
    return result


def missing_versions(reqs: list[Requirement]) -> list[str]:
    missing = []
    for req in reqs:
        try:
            version = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            missing.append(str(req))
            continue
        if not req.specifier.contains(version, prereleases=True):
            missing.append(str(req))
    return missing


def broken_imports(reqs: list[Requirement]) -> list[str]:
    modules = [IMPORTS[canonicalize_name(req.name)] for req in reqs
               if canonicalize_name(req.name) in IMPORTS]
    # La instalacion sana carga el conjunto una vez. Aislar cada import se
    # reserva a reparar: PaddleOCR y PaddleX comparten modulos pesados.
    try:
        batch = subprocess.run(
            [sys.executable, "-c", "; ".join(f"import {m}" for m in modules)],
            capture_output=True, timeout=60,
        )
        if batch.returncode == 0:
            return []
    except subprocess.TimeoutExpired:
        pass
    broken = []
    for req in reqs:
        module = IMPORTS.get(canonicalize_name(req.name))
        if module is None:
            continue
        try:
            completed = subprocess.run(
                [sys.executable, "-c", f"import {module}"],
                capture_output=True, timeout=60,
            )
            if completed.returncode:
                broken.append(req.name)
        except subprocess.TimeoutExpired:
            broken.append(req.name)
    return broken


def pip_run(*args: str) -> int:
    return subprocess.call([sys.executable, "-m", "pip", *args])


def repair(path: Path, force: bool = False) -> int:
    args = ["install", "--disable-pip-version-check", "--no-warn-script-location"]
    if force:
        args.append("--force-reinstall")
    if pip_run(*args, "-r", str(path)):
        return 1
    reqs = requirements(path)
    broken = broken_imports(reqs)
    if broken:
        # pip puede dar por instalado un paquete al que le borraron archivos.
        # Reponer solo esos paquetes respetando la version ya resuelta.
        pinned = [f"{name}=={metadata.version(name)}" for name in broken]
        if pip_run(*args, "--force-reinstall", "--no-deps", *pinned):
            return 1
    return check(path)


def check(path: Path) -> int:
    reqs = requirements(path)
    missing = missing_versions(reqs)
    if missing:
        print("Faltan requisitos o versiones: " + ", ".join(missing))
        return 1
    broken = broken_imports(reqs)
    if broken:
        print("No se pueden importar: " + ", ".join(broken))
        return 1
    return pip_run("check")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    return check(args.requirements) if args.check else repair(args.requirements, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
