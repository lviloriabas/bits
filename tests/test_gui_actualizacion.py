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
from PySide6.QtCore import Qt

from app.gui import actualizacion
from app.gui import main_window as ventana_principal
from app.gui.tokens import TEMA_CLARO, TEMA_OSCURO, paleta, tema
from app.gui.theme import aplicar_tema, install_application_theme
from app.utils.preferencias_ui import guardar_opcion


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
        "rev-parse": _respuesta(salida="abc123\n"),
        "pull": _respuesta(salida="Fast-forward\n"),
        "log": _respuesta(salida="def456 Mejora el visor\nfed321 Iguala el boton\n"),
    })

    assert actualizacion.traer_version_nueva(repositorio) == (
        True, "Fast-forward"
    )
    assert llamadas == [
        ("rev-parse", "HEAD"),
        ("pull", "--ff-only"),
        ("log", "--reverse", "--format=%h %s", "abc123..HEAD"),
    ]
    assert actualizacion.leer_novedades_instaladas(repositorio) == [
        "def456 Mejora el visor", "fed321 Iguala el boton",
    ]


def test_el_pull_que_falla_devuelve_lo_que_dijo_git(repositorio, monkeypatch):
    llamadas = _git_que_responde(monkeypatch, {
        "rev-parse": _respuesta(salida="abc123\n"),
        "pull": _respuesta(codigo=1, error="Not possible to fast-forward"),
    })

    assert actualizacion.traer_version_nueva(repositorio) == (
        False, "Not possible to fast-forward"
    )
    assert actualizacion.leer_novedades_instaladas(repositorio) == []
    assert not any(llamada[0] == "log" for llamada in llamadas)


def test_el_boton_empieza_oculto(window):
    assert window.btn_actualizar.isHidden()
    assert window.btn_actualizar.text() == "Hacer clic aquí para actualizar"
    assert window.btn_actualizar.objectName() == "actualizarButton"
    assert paleta().UPDATE_BG in window.btn_actualizar.styleSheet()


def test_actualizar_es_amarillo_y_conserva_la_altura_al_cambiar_tema(window, app, monkeypatch):
    previo = tema()
    monkeypatch.setattr("app.gui.theme.guardar_tema", lambda _nombre: True)
    install_application_theme(app)
    window.show()
    try:
        for nombre in (TEMA_CLARO, TEMA_OSCURO):
            aplicar_tema(nombre)
            window._on_actualizacion_encontrada(1)
            window.ensurePolished()
            app.processEvents()
            assert f"background-color: {paleta().UPDATE_BG}" in window.btn_actualizar.styleSheet()
            assert window.btn_actualizar.height() == window.btn_automatico.height()
    finally:
        aplicar_tema(previo)


def test_el_aviso_se_muestra_una_vez_al_volver_a_abrir(window, tmp_path, monkeypatch):
    guardar_opcion(
        actualizacion._NOVEDADES_INSTALADAS,
        ["def456 Mejora el visor", "fed321 Conserva <datos>"], tmp_path,
    )
    monkeypatch.setattr(ventana_principal, "SCRIPT_DIR", tmp_path)
    vistos = []

    def mostrar(aviso):
        vistos.append(aviso.text())
        assert aviso.textFormat() == Qt.TextFormat.PlainText

    monkeypatch.setattr(ventana_principal.QMessageBox, "exec", mostrar)
    window.mostrar_novedades_instaladas()
    window.mostrar_novedades_instaladas()

    assert vistos == [
        "Se instalaron estos cambios:\n\ndef456 Mejora el visor\nfed321 Conserva <datos>"
    ]
    assert actualizacion.leer_novedades_instaladas(tmp_path) == []


def test_un_pull_sin_cambios_no_inventa_novedades(repositorio, monkeypatch):
    _git_que_responde(monkeypatch, {
        "rev-parse": _respuesta(salida="abc123\n"),
        "pull": _respuesta(salida="Already up to date.\n"),
        "log": _respuesta(),
    })
    assert actualizacion.traer_version_nueva(repositorio)[0]
    assert actualizacion.leer_novedades_instaladas(repositorio) == []


def test_conserva_el_aviso_si_no_se_pudo_reiniciar(repositorio, monkeypatch):
    guardar_opcion(actualizacion._NOVEDADES_INSTALADAS, ["abc123 Mejora anterior"], repositorio)
    _git_que_responde(monkeypatch, {
        "rev-parse": _respuesta(salida="abc123\n"),
        "pull": _respuesta(),
        "log": _respuesta(salida="def456 Mejora nueva\n"),
    })
    assert actualizacion.traer_version_nueva(repositorio)[0]
    assert actualizacion.leer_novedades_instaladas(repositorio) == [
        "abc123 Mejora anterior", "def456 Mejora nueva",
    ]


def test_fallar_al_leer_el_historial_no_impide_actualizar(repositorio, monkeypatch):
    _git_que_responde(monkeypatch, {
        "rev-parse": _respuesta(salida="abc123\n"),
        "pull": _respuesta(salida="Fast-forward\n"),
        "log": _respuesta(codigo=128),
    })
    assert actualizacion.traer_version_nueva(repositorio) == (True, "Fast-forward")
    assert actualizacion.leer_novedades_instaladas(repositorio) == []


def test_primer_arranque_tras_actualizar_desde_una_version_anterior(repositorio, monkeypatch):
    llamadas = _git_que_responde(monkeypatch, {
        "reflog": _respuesta(salida="pull --ff-only: Fast-forward\n"),
        "rev-parse": _respuesta(salida="abc123\n"),
        "log": _respuesta(salida="def456 Mejora el visor\n"),
    })
    actualizacion.recuperar_aviso_del_pull(repositorio)
    assert actualizacion.leer_novedades_instaladas(repositorio) == ["def456 Mejora el visor"]
    assert ("rev-parse", "HEAD@{1}") in llamadas
    actualizacion.confirmar_novedades_instaladas(repositorio)
    cantidad = len(llamadas)
    actualizacion.recuperar_aviso_del_pull(repositorio)
    assert len(llamadas) == cantidad
    assert actualizacion.leer_novedades_instaladas(repositorio) == []


def test_abrir_despues_de_un_commit_local_no_muestra_actualizacion(repositorio, monkeypatch):
    llamadas = _git_que_responde(monkeypatch, {
        "reflog": _respuesta(salida="commit: Mejora local\n"),
    })
    actualizacion.recuperar_aviso_del_pull(repositorio)
    assert actualizacion.leer_novedades_instaladas(repositorio) == []
    assert len(llamadas) == 1


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
