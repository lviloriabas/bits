"""El botón que avisa de una versión nueva y la trae con ``git pull``.

Comprueba que la consulta al repositorio diga cuántos commits faltan y calle
cuando no puede saberlo, que el botón solo aparezca con commits nuevos, y que
al pulsarlo se traiga la versión y se reinicie, o se explique por qué no.

Nada de esto llama a Git de verdad: se sustituye la ejecución por respuestas
fijas.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.gui import actualizacion
from app.gui import main_window as ventana_principal


def _respuesta(codigo: int = 0, salida: str = "", error: str = ""):
    return subprocess.CompletedProcess([], codigo, salida, error)


@pytest.fixture
def repositorio(tmp_path, monkeypatch) -> Path:
    """Un clon con Git instalado y rama remota, a falta de las respuestas."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(actualizacion.shutil, "which", lambda _nombre: "git")
    return tmp_path


def _git_que_responde(monkeypatch, respuestas: dict[str, subprocess.CompletedProcess]):
    llamadas: list[tuple[str, ...]] = []

    def git(_raiz, *argumentos, espera):
        llamadas.append(argumentos)
        return respuestas[argumentos[0]]

    monkeypatch.setattr(actualizacion, "_git", git)
    return llamadas


def test_cuenta_los_commits_que_faltan(repositorio, monkeypatch):
    llamadas = _git_que_responde(monkeypatch, {
        "rev-parse": _respuesta(salida="origin/main\n"),
        "fetch": _respuesta(),
        "rev-list": _respuesta(salida="3\n"),
    })

    assert actualizacion.commits_pendientes(repositorio) == 3
    assert ("fetch", "--quiet") in llamadas


def test_al_dia_no_hay_commits_pendientes(repositorio, monkeypatch):
    _git_que_responde(monkeypatch, {
        "rev-parse": _respuesta(salida="origin/main\n"),
        "fetch": _respuesta(),
        "rev-list": _respuesta(salida="0\n"),
    })

    assert actualizacion.commits_pendientes(repositorio) == 0


def test_sin_git_no_se_sabe(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(actualizacion.shutil, "which", lambda _nombre: None)

    assert actualizacion.commits_pendientes(tmp_path) is None


def test_sin_red_no_se_sabe(repositorio, monkeypatch):
    _git_que_responde(monkeypatch, {
        "rev-parse": _respuesta(salida="origin/main\n"),
        "fetch": _respuesta(codigo=128, error="Could not resolve host"),
    })

    assert actualizacion.commits_pendientes(repositorio) is None


def test_sin_rama_remota_no_se_sabe(repositorio, monkeypatch):
    _git_que_responde(monkeypatch, {
        "rev-parse": _respuesta(codigo=128, error="no upstream"),
    })

    assert actualizacion.commits_pendientes(repositorio) is None


def test_el_pull_solo_avanza(repositorio, monkeypatch):
    llamadas = _git_que_responde(monkeypatch, {
        "pull": _respuesta(salida="Fast-forward\n"),
    })

    assert actualizacion.traer_version_nueva(repositorio) == (
        True, "Fast-forward"
    )
    assert llamadas == [("pull", "--ff-only")]


def test_el_pull_que_falla_devuelve_lo_que_dijo_git(repositorio, monkeypatch):
    _git_que_responde(monkeypatch, {
        "pull": _respuesta(codigo=1, error="Not possible to fast-forward"),
    })

    assert actualizacion.traer_version_nueva(repositorio) == (
        False, "Not possible to fast-forward"
    )


def test_el_boton_empieza_oculto(window):
    assert window.btn_actualizar.isHidden()
    assert window.btn_actualizar.text() == "Hacer clic aquí para actualizar"


def test_el_boton_aparece_solo_con_commits_nuevos(window):
    window._on_actualizacion_encontrada(2)
    assert not window.btn_actualizar.isHidden()

    window._on_actualizacion_encontrada(0)
    assert window.btn_actualizar.isHidden()


def test_al_hacer_clic_actualiza_y_reinicia(window, app, monkeypatch):
    monkeypatch.setattr(
        actualizacion, "traer_version_nueva", lambda _raiz: (True, "")
    )
    reinicios: list[bool] = []
    monkeypatch.setattr(
        window, "_reiniciar_aplicacion", lambda: reinicios.append(True)
    )
    window._on_actualizacion_encontrada(1)

    window.btn_actualizar.click()
    window._actualizar_worker.wait()
    app.processEvents()

    assert reinicios == [True]
    assert window.btn_actualizar.text() == "Reiniciando…"


def test_si_el_pull_falla_lo_explica_y_deja_el_boton(window, app, monkeypatch):
    monkeypatch.setattr(
        actualizacion,
        "traer_version_nueva",
        lambda _raiz: (False, "Not possible to fast-forward"),
    )
    avisos: list[str] = []
    monkeypatch.setattr(
        ventana_principal.QMessageBox,
        "warning",
        lambda _padre, _titulo, texto: avisos.append(texto),
    )
    window._on_actualizacion_encontrada(1)

    window.btn_actualizar.click()
    window._actualizar_worker.wait()
    app.processEvents()

    assert len(avisos) == 1 and "Not possible to fast-forward" in avisos[0]
    assert window.btn_actualizar.isEnabled()
    assert window.btn_actualizar.text() == "Hacer clic aquí para actualizar"


def test_no_actualiza_con_trabajo_en_curso(window, monkeypatch):
    monkeypatch.setattr(window, "_running_workers", lambda: [object()])
    avisos: list[str] = []
    monkeypatch.setattr(
        ventana_principal.QMessageBox,
        "information",
        lambda _padre, _titulo, texto: avisos.append(texto),
    )
    window._on_actualizacion_encontrada(1)

    window.btn_actualizar.click()
    # El cierre de la ventana también pregunta por los hilos en marcha.
    monkeypatch.undo()

    assert window._actualizar_worker is None
    assert len(avisos) == 1
