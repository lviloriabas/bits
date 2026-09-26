"""Lectura y escritura del manifiesto de un trabajo de indexado.

La escritura es atomica (archivo temporal y ``os.replace``) porque el
indexado guarda el manifiesto despues de cada pagina: si el proceso muere a
mitad de la escritura, un JSON truncado dejaria el trabajo irrecuperable y
habria que volver a indexar todo el batch.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from app.airvault.model import Manifiesto

MANIFIESTO_FILENAME = "manifiesto.json"

# Windows no deja reemplazar un archivo mientras otro lo tiene abierto: el
# antivirus que lo revisa, el indexador de busqueda u otra ventana que lee
# los manifiestos. Pasa en un instante y se resuelve solo, pero el indexado
# guarda tras cada pagina y un solo ``PermissionError`` cortaba el batch a
# la mitad sin que el registro dijera por que. Se reintenta durante unos
# dos segundos y medio antes de darlo por fallo.
PAUSAS_REEMPLAZO = (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6)


def ruta_manifiesto(carpeta_job: Path | str) -> Path:
    return Path(carpeta_job) / MANIFIESTO_FILENAME


def cargar(carpeta_job: Path | str) -> Manifiesto:
    """Carga el manifiesto de un trabajo.

    Raises:
        FileNotFoundError: si el trabajo no existe todavía.
        ValueError: si el archivo esta corrupto o es de otra version.
    """
    ruta = ruta_manifiesto(carpeta_job)
    if not ruta.is_file():
        raise FileNotFoundError(f"No hay manifiesto en {ruta}")
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Manifiesto ilegible en {ruta}: {exc}") from exc
    manifiesto = Manifiesto.model_validate(datos)
    if manifiesto.version != 1:
        raise ValueError(f"Manifiesto version {manifiesto.version}, esperada 1")
    return manifiesto


def guardar(manifiesto: Manifiesto, carpeta_job: Path | str) -> Path:
    """Guarda el manifiesto de forma atomica y devuelve su ruta."""
    carpeta = Path(carpeta_job)
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = ruta_manifiesto(carpeta)
    contenido = manifiesto.model_dump_json(indent=2)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(carpeta), prefix=".manifiesto-", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(contenido)
            handle.flush()
            os.fsync(handle.fileno())
        for pausa in (*PAUSAS_REEMPLAZO, None):
            try:
                os.replace(tmp_name, destino)
                break
            except PermissionError:
                if pausa is None:
                    raise
                time.sleep(pausa)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return destino


def existe(carpeta_job: Path | str) -> bool:
    return ruta_manifiesto(carpeta_job).is_file()
