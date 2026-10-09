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

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from threading import RLock

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
_TURNO_GIT = RLock()


def mensaje_de_git(salida: str) -> str:
    """Explica los fallos habituales sin mostrar la salida interna de Git."""
    texto = salida.casefold()
    if any(p in texto for p in ("could not resolve", "unable to access", "connection", "conectar")):
        return "Revise la conexión y vuelva a intentarlo."
    if "index.lock" in texto or "git está ocupado" in texto:
        return "Hay otra actualización en curso. Espere a que termine y vuelva a intentarlo."
    if any(p in texto for p in ("permission denied", "unable to unlink", "access is denied")):
        return "Un archivo está abierto en otro programa. Ciérrelo y vuelva a intentarlo."
    if "respaldo" in texto or "no se pudo cambiar" in texto:
        return salida
    return "Cierre BITS y vuelva a intentarlo. Su trabajo local se conserva."


def _git(raiz: Path, *argumentos: str, espera: float) -> subprocess.CompletedProcess:
    """Ejecuta Git en ``raiz`` sin consola y sin pedir credenciales.

    ``GIT_TERMINAL_PROMPT=0`` hace que un remoto que pide usuario falle en
    el acto en vez de quedarse esperando una respuesta que nadie va a dar.
    """
    entorno = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(
        [_ruta_git(raiz) or "git", *argumentos],
        cwd=str(raiz),
        env=entorno,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=espera,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _ruta_git(raiz: Path) -> str | None:
    portable = raiz / "portable" / "git" / "cmd" / "git.exe"
    return str(portable) if portable.is_file() else shutil.which("git")


def git_disponible(raiz: Path) -> bool:
    """Si hay Git y ``raiz`` es un clon con rama remota a la que seguir."""
    if _ruta_git(raiz) is None or not (raiz / ".git").exists():
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
    if not ok and any(texto in salida.casefold() for texto in (
        "not possible to fast-forward", "divergent", "would be overwritten",
        "unmerged", "merging is not possible", "unfinished merge", "rebase in progress",
        "not concluded your merge", "there is no tracking information",
    )):
        return sincronizar_rama(raiz)
    if ok and anterior:
        _guardar_novedades(raiz, anterior)
    return ok, salida


def _exigir_git(raiz: Path, *args: str, espera: float = _ESPERA_LOCAL_S) -> str:
    resultado = _git(raiz, *args, espera=espera)
    if resultado.returncode:
        raise RuntimeError(resultado.stderr.strip() or resultado.stdout.strip())
    return resultado.stdout.strip()


def ramas_disponibles(raiz: Path) -> tuple[str, list[str]]:
    """Consulta las ramas de origin, conservando nombres con barras."""
    with _TURNO_GIT:
        _exigir_git(raiz, "fetch", "--prune", "origin", espera=_ESPERA_FETCH_S)
        actual = _exigir_git(raiz, "branch", "--show-current")
        remotas = _exigir_git(raiz, "for-each-ref", "--format=%(refname:strip=3)",
                             "refs/remotes/origin/")
        return actual, sorted(r for r in remotas.splitlines() if r and r != "HEAD")


def _respaldar_copia(raiz: Path, rama_destino: str = "") -> Path:
    """Conserva commits, archivos locales y eliminaciones antes de recuperar Git."""
    marca = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    referencia = f"refs/bits-respaldo/{marca}"
    _exigir_git(raiz, "update-ref", referencia, "HEAD")
    destino_previo = ""
    if rama_destino:
        previo = _git(raiz, "rev-parse", "--verify", f"refs/heads/{rama_destino}", espera=_ESPERA_LOCAL_S)
        if previo.returncode == 0:
            destino_previo = previo.stdout.strip()
            _exigir_git(raiz, "update-ref", referencia + "-destino", destino_previo)
    carpeta = raiz / "portable" / "actualizaciones" / marca
    carpeta.mkdir(parents=True)
    archivos = _exigir_git(raiz, "ls-files", "-m", "-d", "-o", "--exclude-standard", "-z")
    eliminados = []
    for nombre in set(archivos.split("\0")) - {""}:
        origen = (raiz / nombre).resolve()
        if not origen.is_relative_to(raiz.resolve()):
            raise RuntimeError("No se pudo respaldar un archivo fuera de la carpeta del programa.")
        if not origen.is_file():
            eliminados.append(nombre)
            continue
        destino = carpeta / "archivos" / nombre
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origen, destino)
    (carpeta / "respaldo.json").write_text(json.dumps({
        "referencia": referencia, "destino_previo": destino_previo, "eliminados": eliminados,
        "estado": _exigir_git(raiz, "status", "--porcelain"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return carpeta


def sincronizar_rama(raiz: Path, rama: str | None = None) -> tuple[bool, str]:
    """Instala exactamente la rama remota despues de guardar una copia local.

    Los datos ignorados, las entradas, las salidas y el portable se conservan.
    Un cambio local inesperado queda respaldado y en el stash de Git.
    """
    with _TURNO_GIT:
        stash_creado = False
        checkout_realizado = False
        try:
            _exigir_git(raiz, "fetch", "--prune", "origin", espera=_ESPERA_FETCH_S)
            rama = rama or _exigir_git(raiz, "branch", "--show-current")
            if not rama or rama.startswith("-"):
                return False, "Seleccione una rama disponible en origin."
            _exigir_git(raiz, "check-ref-format", "--branch", rama)
            remoto = f"refs/remotes/origin/{rama}"
            _exigir_git(raiz, "rev-parse", "--verify", remoto)
            anterior = _exigir_git(raiz, "rev-parse", "HEAD")
            rama_anterior = _exigir_git(raiz, "branch", "--show-current")
            _respaldar_copia(raiz, rama)
            # Una operacion interrumpida puede impedir guardar el stash.
            # La copia de archivos ya esta completa antes de abortarla.
            git_dir = Path(_exigir_git(raiz, "rev-parse", "--absolute-git-dir"))
            if (git_dir / "MERGE_HEAD").exists():
                _exigir_git(raiz, "merge", "--abort")
            if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
                _exigir_git(raiz, "rebase", "--abort")
            if (git_dir / "CHERRY_PICK_HEAD").exists():
                _exigir_git(raiz, "cherry-pick", "--abort")
            if _exigir_git(raiz, "status", "--porcelain"):
                _exigir_git(raiz, "stash", "push", "--include-untracked", "-m", "Respaldo BITS antes de actualizar")
                stash_creado = True
            _exigir_git(raiz, "checkout", "--no-overwrite-ignore", "-B", rama, remoto)
            checkout_realizado = True
            _exigir_git(raiz, "branch", "--set-upstream-to", f"origin/{rama}", rama)
            _guardar_novedades(raiz, anterior)
            return True, f"Rama {rama} instalada. La copia anterior queda respaldada."
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            logger.warning("No se pudo instalar la rama: {}", exc)
            if checkout_realizado:
                args = ("checkout", "--no-overwrite-ignore", "-B", rama_anterior, anterior) if rama_anterior else ("checkout", "--detach", anterior)
                try:
                    rollback = _git(raiz, *args, espera=_ESPERA_LOCAL_S)
                    checkout_realizado = rollback.returncode != 0
                except (OSError, subprocess.SubprocessError):
                    pass
            if stash_creado and not checkout_realizado:
                # Checkout es transaccional: si se nego, la rama anterior
                # sigue activa y se puede devolver su trabajo local.
                try:
                    restaurado = _git(raiz, "stash", "apply", espera=_ESPERA_LOCAL_S)
                    if restaurado.returncode:
                        logger.warning("El trabajo local queda en el respaldo: {}", restaurado.stderr.strip())
                except (OSError, subprocess.SubprocessError):
                    logger.warning("No se pudo reponer el trabajo local; queda en el respaldo")
            texto = str(exc).casefold()
            if "could not resolve" in texto or "unable to access" in texto:
                return False, "No se pudo conectar con origin. Revise la conexión y vuelva a intentarlo."
            if "index.lock" in texto:
                return False, "Git está ocupado. Cierre otras actualizaciones y vuelva a intentarlo."
            return False, "No se pudo cambiar la versión. Se conserva el respaldo; cierre BITS y vuelva a intentarlo."


class BuscarRamasWorker(QThread):
    terminado = Signal(str, object, str)

    def __init__(self, raiz: Path, parent=None):
        super().__init__(parent)
        self._raiz = raiz

    def run(self):
        try:
            actual, ramas = ramas_disponibles(self._raiz)
            self.terminado.emit(actual, ramas, "")
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            logger.warning("No se pudieron consultar ramas: {}", exc)
            self.terminado.emit("", [], "No se pudieron consultar las ramas de origin. Revise la conexión.")


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
        with _TURNO_GIT:
            pendientes = commits_pendientes(self._raiz)
        if pendientes is not None:
            self.encontrado.emit(pendientes)


class ActualizarWorker(QThread):
    """Trae la version nueva sin congelar la ventana."""

    terminado = Signal(bool, str)

    def __init__(self, raiz: Path, parent=None, rama: str | None = None) -> None:
        super().__init__(parent)
        self._raiz = raiz
        self._rama = rama

    def run(self) -> None:  # noqa: D102 - lo describe la clase
        with _TURNO_GIT:
            ok, salida = (sincronizar_rama(self._raiz, self._rama) if self._rama
                          else traer_version_nueva(self._raiz))
        self.terminado.emit(ok, salida)
