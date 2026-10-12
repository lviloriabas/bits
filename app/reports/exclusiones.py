"""Respaldo y restauracion exacta de los datos de la ultima exclusion."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import threading
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

from app.models.schemas import ValidationReport

_CANDADO = threading.Lock()
_TURNOS: dict[str, threading.RLock] = {}


@contextmanager
def turno_datos(run_dir: Path):
    """Una escritura por ejecucion entre las ventanas de esta aplicacion."""
    clave = str(Path(run_dir).resolve()).casefold()
    with _CANDADO:
        turno = _TURNOS.setdefault(clave, threading.RLock())
    if not turno.acquire(blocking=False):
        raise ValueError("Otra ventana está guardando esta ejecución. Espere a que termine.")
    try:
        yield
    finally:
        turno.release()


def ruta_respaldo(run_dir: Path) -> Path:
    return Path(run_dir) / "logs" / "ultima_exclusion.zip"


def _rutas(run_dir: Path) -> dict[str, Path]:
    run_dir = Path(run_dir)
    nombres = (f"{run_dir.name}.CSV", f"{run_dir.name}_completo.CSV", f"{run_dir.name}.json")
    rutas = {"stats.json": run_dir / "stats.json"}
    for carpeta in (run_dir, run_dir / "datos"):
        for nombre in nombres:
            ruta = carpeta / nombre
            # Las ejecuciones antiguas pueden usar otra caja en la extension.
            if carpeta.is_dir():
                ruta = next((p for p in carpeta.iterdir()
                             if p.name.casefold() == nombre.casefold()), ruta)
            rutas[ruta.relative_to(run_dir).as_posix()] = ruta
    return rutas


def _capturar(run_dir: Path) -> dict[str, bytes]:
    return {nombre: ruta.read_bytes() for nombre, ruta in _rutas(run_dir).items()
            if ruta.is_file()}


def _huellas(datos: dict[str, bytes]) -> dict[str, str]:
    return {nombre: sha256(valor).hexdigest() for nombre, valor in datos.items()}


def _atomico(ruta: Path, datos: bytes) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor, nombre = tempfile.mkstemp(dir=ruta.parent, prefix=".exclusion-", suffix=".tmp")
    temporal = Path(nombre)
    try:
        with os.fdopen(descriptor, "wb") as archivo:
            archivo.write(datos)
        os.replace(temporal, ruta)
    finally:
        temporal.unlink(missing_ok=True)


def _reponer(run_dir: Path, datos: dict[str, bytes]) -> None:
    permitidas = _rutas(run_dir)
    if not set(datos).issubset(permitidas):
        raise ValueError("El respaldo contiene archivos ajenos a los datos de esta ejecución.")
    for nombre, contenido in datos.items():
        _atomico(permitidas[nombre], contenido)
    for nombre, ruta in permitidas.items():
        if nombre not in datos:
            ruta.unlink(missing_ok=True)


def _reportes(run_dir: Path, datos: dict[str, bytes]) -> list[ValidationReport]:
    nombre = f"{Path(run_dir).name}.json"
    por_nombre = {ruta.casefold(): contenido for ruta, contenido in datos.items()}
    contenido = por_nombre.get(f"datos/{nombre}".casefold(), por_nombre.get(nombre.casefold()))
    if contenido is None:
        raise ValueError("Faltan los datos guardados de esta ejecución.")
    payload = json.loads(contenido)
    if not isinstance(payload, dict) or not isinstance(payload.get("reportes"), list):
        raise ValueError("El respaldo no contiene datos de una ejecución.")
    reportes = [ValidationReport.model_validate(r) for r in payload.get("reportes", [])]
    if not reportes or not any(r.pages for r in reportes):
        raise ValueError("El respaldo no contiene páginas para restaurar.")
    return reportes


def _leer(run_dir: Path):
    with ZipFile(ruta_respaldo(run_dir)) as archivo:
        meta = json.loads(archivo.read("manifest.json"))
        if not isinstance(meta, dict) or meta.get("version") != 1 or meta.get("ejecucion") != Path(run_dir).name:
            raise ValueError("El respaldo no pertenece a esta ejecución.")
        nombres = meta["archivos"]
        if not isinstance(nombres, list) or not all(isinstance(nombre, str) for nombre in nombres):
            raise ValueError("El respaldo no contiene una lista de archivos válida.")
        if not set(nombres).issubset(_rutas(run_dir)):
            raise ValueError("El respaldo contiene archivos inesperados.")
        datos = {nombre: archivo.read(f"antes/{nombre}") for nombre in nombres}
    _reportes(run_dir, datos)
    return meta, datos


def motivo_restauracion(run_dir: Path | None) -> str:
    """Vacio cuando restaurar es posible, o el motivo de su bloqueo."""
    if run_dir is None or not ruta_respaldo(run_dir).is_file():
        return "Esta ejecución no tiene una exclusión guardada para restaurar."
    try:
        meta, _datos = _leer(run_dir)
        if meta["despues"] is not None and meta["despues"] != _huellas(_capturar(run_dir)):
            return "Los datos cambiaron después de la exclusión. No se sobrescribirán esos cambios."
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, BadZipFile):
        return "No se pudo verificar el respaldo de esta ejecución. No se aplicará la restauración."
    return ""


def _guardar_respaldo(run_dir: Path, antes: dict[str, bytes], despues) -> None:
    respaldo = ruta_respaldo(run_dir)
    respaldo.parent.mkdir(parents=True, exist_ok=True)
    descriptor, nombre = tempfile.mkstemp(dir=respaldo.parent, suffix=".zip")
    os.close(descriptor)
    temporal = Path(nombre)
    try:
        with ZipFile(temporal, "w", ZIP_DEFLATED) as archivo:
            archivo.writestr("manifest.json", json.dumps({
                "version": 1, "ejecucion": run_dir.name,
                "archivos": list(antes), "despues": despues,
            }, ensure_ascii=False))
            for ruta, contenido in antes.items():
                archivo.writestr(f"antes/{ruta}", contenido)
        os.replace(temporal, respaldo)
    finally:
        temporal.unlink(missing_ok=True)


def guardar_con_respaldo(run_dir: Path, escribir):
    """Guarda la exclusion; si falla, repone los datos y el respaldo anterior."""
    run_dir = Path(run_dir)
    with turno_datos(run_dir):
        antes = _capturar(run_dir)
        _reportes(run_dir, antes)
        respaldo = ruta_respaldo(run_dir)
        previo = respaldo.read_bytes() if respaldo.is_file() else None
        try:
            # El respaldo existe antes de la primera escritura, tambien si
            # el proceso termina inesperadamente mientras guarda los datos.
            _guardar_respaldo(run_dir, antes, None)
            resultado = escribir()
            _guardar_respaldo(run_dir, antes, _huellas(_capturar(run_dir)))
        except Exception:
            try:
                _reponer(run_dir, antes)
            except OSError as rollback_error:
                raise OSError("No se pudieron reponer todos los datos. Cierre los archivos y use Restaurar última exclusión.") from rollback_error
            if previo is not None:
                _atomico(respaldo, previo)
            else:
                respaldo.unlink(missing_ok=True)
            raise
        return resultado


def restaurar_exclusion(run_dir: Path) -> list[ValidationReport]:
    """Recupera los archivos exactos sin OCR, sin cambiar los PDF originales."""
    run_dir = Path(run_dir)
    with turno_datos(run_dir):
        motivo = motivo_restauracion(run_dir)
        if motivo:
            raise ValueError(motivo)
        _meta, antes = _leer(run_dir)
        actuales = _capturar(run_dir)
        reportes = _reportes(run_dir, antes)
        try:
            _reponer(run_dir, antes)
            ruta_respaldo(run_dir).unlink()
        except Exception:
            _reponer(run_dir, actuales)
            raise
        return reportes
