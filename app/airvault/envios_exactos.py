"""Proteccion portable del mismo PDF, incluso tras cerrar o borrar un trabajo."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

ARCHIVO = "batches-enviados.sqlite3"
_HILO = threading.Lock()


class CargaRepetida(RuntimeError):
    pass


def huella(archivo, config):
    digest = hashlib.sha256()
    digest.update(f"{config.base_url.rstrip('/')}|{config.repo_id}|".encode())
    with Path(archivo).open("rb") as pdf:
        for bloque in iter(lambda: pdf.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


@contextmanager
def _conexion(raiz):
    raiz = Path(raiz)
    raiz.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(raiz / ARCHIVO, timeout=30)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS cargas "
                   "(huella TEXT PRIMARY KEY, nombre TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS migracion (version INTEGER PRIMARY KEY)")
        if not db.execute("SELECT 1 FROM migracion WHERE version=1").fetchone():
            from app.airvault import manifest
            from app.airvault.config import AirVaultConfig
            from app.airvault.model import EstadoEtapa
            for ruta in raiz.rglob(manifest.MANIFIESTO_FILENAME):
                anterior = manifest.cargar(ruta.parent)
                if anterior.etapa("subir").estado not in (
                    EstadoEtapa.HECHA, EstadoEtapa.OMITIDA, EstadoEtapa.EN_CURSO
                ):
                    continue
                archivo = Path(anterior.pdf_origen)
                if archivo.is_file():
                    config = AirVaultConfig(repo_id=anterior.repo_id)
                    db.execute("INSERT OR IGNORE INTO cargas VALUES (?, ?)",
                               (huella(archivo, config), anterior.nombre_batch))
            db.execute("INSERT OR IGNORE INTO migracion VALUES (1)")
        db.commit()
        yield db
    finally:
        db.close()


def iniciar(raiz):
    """Incorpora los trabajos anteriores antes de que se reinicie su estado."""
    with _conexion(raiz):
        pass


def motivo(raiz, archivo, config):
    if not archivo or not Path(archivo).is_file():
        return ""
    clave = huella(archivo, config)
    with _conexion(raiz) as db:
        envio = db.execute("SELECT nombre FROM cargas WHERE huella=?", (clave,)).fetchone()
    return (f"Este mismo PDF ya se envio o tiene una carga pendiente de confirmar "
            f"en AirVault ({envio[0]}). Volver a subirlo puede dejar el mismo "
            "documento dos veces en AirVault") if envio else ""


@contextmanager
def _exclusiva(raiz):
    # El candado del sistema se libera incluso si Windows termina el proceso.
    if not _HILO.acquire(blocking=False):
        raise CargaRepetida("Hay una carga en curso; espere antes de reenviar")
    archivo = None
    tomado = False
    try:
        Path(raiz).mkdir(parents=True, exist_ok=True)
        archivo = (Path(raiz) / "cargas.lock").open("a+b")
        if archivo.tell() == 0:
            archivo.write(b"0")
            archivo.flush()
        archivo.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(archivo.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(archivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            tomado = True
        except OSError as exc:
            raise CargaRepetida("Otra ventana esta subiendo; espere antes de reenviar") from exc
        yield
    finally:
        if archivo is not None:
            if tomado:
                archivo.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(archivo.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(archivo.fileno(), fcntl.LOCK_UN)
            archivo.close()
        _HILO.release()


def enviar(raiz, trabajo, archivo, subidor, valores, avisar=None):
    with _exclusiva(raiz):
        clave = huella(archivo, trabajo.config)
        with _conexion(raiz) as db:
            anterior = db.execute("SELECT nombre FROM cargas WHERE huella=?", (clave,)).fetchone()
            if anterior and not trabajo._duplicado_permitido:
                raise CargaRepetida(motivo(raiz, archivo, trabajo.config))
            # Se confirma la reserva en disco ANTES de cualquier peticion.
            db.execute("INSERT OR REPLACE INTO cargas VALUES (?, ?)",
                       (clave, trabajo.manifiesto.nombre_batch))
            db.commit()
        trabajo._duplicado_permitido = False
        resultado = subidor.subir(archivo, valores, avisar=avisar)
        if not resultado.ok and not anterior:
            # Un rechazo confirmado permite reintentar. Una excepcion o una
            # respuesta incierta conserva la reserva y requiere advertencia.
            with _conexion(raiz) as db:
                db.execute("DELETE FROM cargas WHERE huella=?", (clave,))
                db.commit()
        return resultado
