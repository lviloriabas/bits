"""Aviso de version nueva y actualizacion con ``git pull``.

La carpeta de BITS es un clon del repositorio. Cuando ``origin`` tiene
commits que la copia local no tiene, la ventana principal muestra un boton;
al pulsarlo se trae la version nueva con ``git pull --ff-only`` y la
aplicacion se reinicia sola para cargarla.

Git no forma parte de la carpeta portable: en un PC sin Git, sin red o sin
rama remota la consulta no encuentra nada y el boton no aparece. Nada de
esto es necesario para que BITS funcione.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QThread, Signal

from app.utils.no_console import CREATE_NO_WINDOW
from app.utils.preferencias_ui import guardar_opcion, leer_opcion

# ``git fetch`` va a la red; si no contesta en este tiempo se da por no
# disponible y se vuelve a intentar en la siguiente consulta.
_ESPERA_FETCH_S = 30
_ESPERA_PULL_S = 120
_ESPERA_LOCAL_S = 10
_NOVEDADES_INSTALADAS = "actualizacion.commits_instalados"


def _git(raiz: Path, *argumentos: str, espera: float) -> subprocess.CompletedProcess:
    """Ejecuta Git en ``raiz`` sin consola y sin pedir credenciales.

    ``GIT_TERMINAL_PROMPT=0`` hace que un remoto que pide usuario falle en
    el acto en vez de quedarse esperando una respuesta que nadie va a dar.
    """
    entorno = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(
        ["git", *argumentos],
        cwd=str(raiz),
        env=entorno,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=espera,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def git_disponible(raiz: Path) -> bool:
    """Si hay Git y ``raiz`` es un clon con rama remota a la que seguir."""
    if shutil.which("git") is None or not (raiz / ".git").exists():
        return False
    try:
        rama = _git(
            raiz, "rev-parse", "--abbrev-ref", "@{u}", espera=_ESPERA_LOCAL_S
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return rama.returncode == 0 and bool(rama.stdout.strip())


def commits_pendientes(raiz: Path) -> int | None:
    """Commits de la rama remota que la copia local todavia no tiene.

    Devuelve None si no se pudo saber (sin Git, sin red, sin rama remota).
    """
    if not git_disponible(raiz):
        return None
    try:
        traida = _git(raiz, "fetch", "--quiet", espera=_ESPERA_FETCH_S)
        if traida.returncode != 0:
            logger.info(
                f"No se pudo consultar si hay version nueva: "
                f"{traida.stderr.strip()}"
            )
            return None
        cuenta = _git(
            raiz, "rev-list", "--count", "HEAD..@{u}", espera=_ESPERA_LOCAL_S
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info(f"No se pudo consultar si hay version nueva: {exc}")
        return None
    if cuenta.returncode != 0:
        return None
    try:
        return int(cuenta.stdout.strip())
    except ValueError:
        return None


def traer_version_nueva(raiz: Path) -> tuple[bool, str]:
    """Hace ``git pull --ff-only`` y devuelve si salio bien y lo que dijo Git.

    ``--ff-only`` solo avanza la copia local: si hay cambios propios que no
    encajan, Git se niega y la carpeta queda como estaba.
    """
    anterior = ""
    try:
        revision = _git(raiz, "rev-parse", "HEAD", espera=_ESPERA_LOCAL_S)
        if revision.returncode == 0:
            anterior = revision.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info(f"No se pudo identificar la version anterior: {exc}")
    try:
        resultado = _git(raiz, "pull", "--ff-only", espera=_ESPERA_PULL_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    salida = "\n".join(
        parte.strip()
        for parte in (resultado.stdout, resultado.stderr)
        if parte.strip()
    )
    ok = resultado.returncode == 0
    if ok and anterior:
        _guardar_novedades(raiz, anterior)
    return ok, salida


def _guardar_novedades(raiz: Path, anterior: str) -> None:
    """Anota solo los commits incorporados por el pull que acaba de terminar."""
    try:
        historia = _git(
            raiz, "log", "--reverse", "--format=%h %s", f"{anterior}..HEAD",
            espera=_ESPERA_LOCAL_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info(f"No se pudieron leer los cambios instalados: {exc}")
        return
    if historia.returncode != 0:
        return
    nuevos = [linea.strip() for linea in historia.stdout.splitlines() if linea.strip()]
    if nuevos:
        # Si el reinicio anterior fallo, conserva tambien su aviso pendiente.
        pendientes = leer_novedades_instaladas(raiz)
        pendientes.extend(linea for linea in nuevos if linea not in pendientes)
        if not guardar_opcion(_NOVEDADES_INSTALADAS, pendientes, raiz):
            logger.warning("No se pudo guardar el aviso de actualizacion")


def leer_novedades_instaladas(raiz: Path) -> list[str]:
    """El aviso pendiente vive junto al programa y se lee sin red ni Git."""
    datos = leer_opcion(_NOVEDADES_INSTALADAS, [], raiz)
    if not isinstance(datos, list):
        return []
    return [dato for dato in datos if isinstance(dato, str) and dato.strip()]


def confirmar_novedades_instaladas(raiz: Path) -> None:
    """Tras cerrar el aviso, el siguiente arranque ya no lo repite."""
    if not guardar_opcion(_NOVEDADES_INSTALADAS, [], raiz):
        logger.warning("No se pudo confirmar el aviso de actualizacion")


def recuperar_aviso_del_pull(raiz: Path) -> None:
    """Recupera el primer aviso si el pull lo hizo una version sin esta funcion."""
    if leer_opcion(_NOVEDADES_INSTALADAS, raiz=raiz) is not None:
        return
    if shutil.which("git") is None or not (raiz / ".git").exists():
        return
    try:
        ultimo = _git(
            raiz, "reflog", "-1", "--format=%gs", "HEAD", espera=_ESPERA_LOCAL_S,
        )
        if ultimo.returncode == 0 and ultimo.stdout.strip().startswith("pull "):
            previo = _git(raiz, "rev-parse", "HEAD@{1}", espera=_ESPERA_LOCAL_S)
            if previo.returncode == 0 and previo.stdout.strip():
                _guardar_novedades(raiz, previo.stdout.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info(f"No se pudo recuperar el aviso del ultimo pull: {exc}")


class BuscarActualizacionWorker(QThread):
    """Consulta en segundo plano cuantos commits nuevos hay en el remoto."""

    encontrado = Signal(int)

    def __init__(self, raiz: Path, parent=None) -> None:
        super().__init__(parent)
        self._raiz = raiz

    def run(self) -> None:  # noqa: D102 - lo describe la clase
        pendientes = commits_pendientes(self._raiz)
        if pendientes is not None:
            self.encontrado.emit(pendientes)


class ActualizarWorker(QThread):
    """Trae la version nueva sin congelar la ventana."""

    terminado = Signal(bool, str)

    def __init__(self, raiz: Path, parent=None) -> None:
        super().__init__(parent)
        self._raiz = raiz

    def run(self) -> None:  # noqa: D102 - lo describe la clase
        ok, salida = traer_version_nueva(self._raiz)
        self.terminado.emit(ok, salida)
