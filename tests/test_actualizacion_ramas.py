"""Cambio de rama y recuperacion con repositorios reales, sin red externa."""

import json
import subprocess

import pytest

from app.gui.actualizacion import ramas_disponibles, sincronizar_rama, traer_version_nueva


def git(raiz, *args):
    resultado = subprocess.run(["git", *args], cwd=raiz, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    assert resultado.returncode == 0, resultado.stderr
    return resultado.stdout.strip()


@pytest.fixture
def copias(tmp_path):
    remoto = tmp_path / "remoto.git"
    git(tmp_path, "init", "--bare", str(remoto))
    fuente = tmp_path / "fuente"
    git(tmp_path, "clone", str(remoto), str(fuente))
    git(fuente, "config", "user.name", "Prueba")
    git(fuente, "config", "user.email", "prueba@example.com")
    git(fuente, "switch", "-c", "main")
    (fuente / ".gitignore").write_text("portable/\noutput/\ninterfaz.json\n", encoding="utf-8")
    (fuente / "app.py").write_text("version = 1\n", encoding="utf-8")
    git(fuente, "add", ".")
    git(fuente, "commit", "-m", "Inicial")
    git(fuente, "push", "-u", "origin", "main")
    git(remoto, "symbolic-ref", "HEAD", "refs/heads/main")
    git(fuente, "switch", "-c", "pruebas/sidebar")
    (fuente / "app.py").write_text("version = 2\n", encoding="utf-8")
    git(fuente, "commit", "-am", "Version alternativa")
    git(fuente, "push", "-u", "origin", "pruebas/sidebar")
    git(fuente, "switch", "main")
    copia = tmp_path / "copia"
    git(tmp_path, "clone", str(remoto), str(copia))
    git(copia, "config", "user.name", "Prueba")
    git(copia, "config", "user.email", "prueba@example.com")
    return fuente, copia


def test_lista_y_cambia_ramas_con_barras(copias):
    _, copia = copias
    actual, ramas = ramas_disponibles(copia)
    assert actual == "main"
    assert ramas == ["main", "pruebas/sidebar"]
    ok, mensaje = sincronizar_rama(copia, "pruebas/sidebar")
    assert ok, mensaje
    assert git(copia, "branch", "--show-current") == "pruebas/sidebar"
    assert git(copia, "rev-parse", "--abbrev-ref", "@{u}") == "origin/pruebas/sidebar"
    assert (copia / "app.py").read_text() == "version = 2\n"
    assert not git(copia, "status", "--porcelain")
    assert sincronizar_rama(copia, "main")[0]
    assert (copia / "app.py").read_text() == "version = 1\n"


def test_respalda_cambios_y_conserva_datos_ignorados(copias):
    _, copia = copias
    (copia / "app.py").write_text("cambio local\n", encoding="utf-8")
    (copia / "nota.txt").write_text("nota local", encoding="utf-8")
    (copia / "output").mkdir()
    (copia / "output" / "batch.pdf").write_bytes(b"PDF local")
    (copia / "interfaz.json").write_text('{"tema":"oscuro"}', encoding="utf-8")
    ok, mensaje = sincronizar_rama(copia, "pruebas/sidebar")
    assert ok, mensaje
    respaldos = list((copia / "portable" / "actualizaciones").iterdir())
    assert (respaldos[0] / "archivos" / "app.py").read_text() == "cambio local\n"
    assert (respaldos[0] / "archivos" / "nota.txt").read_text() == "nota local"
    assert (copia / "output" / "batch.pdf").read_bytes() == b"PDF local"
    assert json.loads((copia / "interfaz.json").read_text())["tema"] == "oscuro"
    assert git(copia, "stash", "list")
    assert not git(copia, "status", "--porcelain")


def test_recupera_divergencia_al_actualizar(copias):
    fuente, copia = copias
    (copia / "app.py").write_text("version local\n", encoding="utf-8")
    git(copia, "commit", "-am", "Cambio local")
    anterior = git(copia, "rev-parse", "HEAD")
    (fuente / "app.py").write_text("version remota\n", encoding="utf-8")
    git(fuente, "commit", "-am", "Cambio remoto")
    git(fuente, "push")
    ok, mensaje = traer_version_nueva(copia)
    assert ok, mensaje
    assert git(copia, "rev-parse", "HEAD") == git(copia, "rev-parse", "origin/main")
    referencias = git(copia, "for-each-ref", "--format=%(objectname)", "refs/bits-respaldo/")
    assert anterior in referencias


def test_conserva_commits_propios_de_la_rama_destino(copias):
    _, copia = copias
    git(copia, "switch", "pruebas/sidebar")
    (copia / "app.py").write_text("alternativa local\n", encoding="utf-8")
    git(copia, "commit", "-am", "Alternativa local")
    anterior = git(copia, "rev-parse", "HEAD")
    git(copia, "switch", "main")
    assert sincronizar_rama(copia, "pruebas/sidebar")[0]
    referencias = git(copia, "for-each-ref", "--format=%(objectname)", "refs/bits-respaldo/")
    assert anterior in referencias


def test_no_sobrescribe_datos_ignorados_de_una_rama_antigua(copias):
    fuente, copia = copias
    git(fuente, "switch", "pruebas/sidebar")
    (fuente / "interfaz.json").write_text('{"tema":"claro"}', encoding="utf-8")
    git(fuente, "add", "-f", "interfaz.json")
    git(fuente, "commit", "-m", "Preferencias antiguas")
    git(fuente, "push")
    (copia / "interfaz.json").write_text('{"tema":"oscuro"}', encoding="utf-8")
    (copia / "app.py").write_text("cambio local\n", encoding="utf-8")
    ok, _ = sincronizar_rama(copia, "pruebas/sidebar")
    assert not ok
    assert git(copia, "branch", "--show-current") == "main"
    assert json.loads((copia / "interfaz.json").read_text())["tema"] == "oscuro"
    assert (copia / "app.py").read_text() == "cambio local\n"


def test_aborta_conflicto_guardando_archivos(copias):
    fuente, copia = copias
    (copia / "app.py").write_text("local\n", encoding="utf-8")
    git(copia, "commit", "-am", "Local")
    (fuente / "app.py").write_text("remoto\n", encoding="utf-8")
    git(fuente, "commit", "-am", "Remoto")
    git(fuente, "push")
    git(copia, "fetch")
    conflicto = subprocess.run(["git", "merge", "origin/main"], cwd=copia, capture_output=True)
    assert conflicto.returncode
    contenido = (copia / "app.py").read_text()
    assert "<<<<<<<" in contenido
    ok, mensaje = sincronizar_rama(copia, "pruebas/sidebar")
    assert ok, mensaje
    respaldo = next((copia / "portable" / "actualizaciones").iterdir())
    assert (respaldo / "archivos" / "app.py").read_text() == contenido
    assert not (copia / ".git" / "MERGE_HEAD").exists()
    assert not git(copia, "status", "--porcelain")


@pytest.mark.parametrize("rama", ["--detach", "no-existe", "main; rm"])
def test_rama_invalida_no_toca_la_copia(copias, rama):
    _, copia = copias
    anterior = git(copia, "rev-parse", "HEAD")
    ok, _ = sincronizar_rama(copia, rama)
    assert not ok
    assert git(copia, "rev-parse", "HEAD") == anterior
    assert not (copia / "portable").exists()
