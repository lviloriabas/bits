"""La ruta remota: apagada de fabrica, persistente y con respaldo local."""

from __future__ import annotations

import json
import time

import pytest

from app.utils import ruta_remota


@pytest.fixture(autouse=True)
def _sin_conexiones_recordadas():
    ruta_remota.olvidar_conexion()
    yield
    ruta_remota.olvidar_conexion()


def test_nace_apagada_y_usa_las_carpetas_locales(tmp_path):
    ajustes = ruta_remota.leer_ajustes()

    assert ajustes == ruta_remota.Ajustes(activa=False, ruta="")
    assert ruta_remota.raiz_remota() is None
    assert not ruta_remota.en_uso()
    assert ruta_remota.carpeta_input(tmp_path) == tmp_path / "input"
    assert ruta_remota.carpeta_output(tmp_path) == tmp_path / "output"


def test_apagada_no_mira_la_red_aunque_tenga_ruta(tmp_path, monkeypatch):
    servidor = tmp_path / "servidor"
    servidor.mkdir()
    ruta_remota.guardar_ajustes(False, str(servidor))

    def no_se_consulta(*_a, **_k):
        raise AssertionError("apagada no debe preguntar por la carpeta")

    monkeypatch.setattr(ruta_remota, "_contesta", no_se_consulta)

    assert ruta_remota.raiz_remota() is None
    assert ruta_remota.carpeta_output(tmp_path) == tmp_path / "output"


def test_la_eleccion_se_guarda_en_las_preferencias_locales(
    tmp_path, opciones_de_interfaz
):
    servidor = tmp_path / "servidor"

    assert ruta_remota.guardar_ajustes(True, f"  {servidor}  ")

    guardado = json.loads(opciones_de_interfaz.read_text(encoding="utf-8"))
    assert guardado[ruta_remota.CLAVE_ACTIVA] is True
    assert guardado[ruta_remota.CLAVE_RUTA] == str(servidor)
    assert ruta_remota.leer_ajustes() == ruta_remota.Ajustes(True, str(servidor))


def test_desactivar_conserva_la_ruta(tmp_path):
    servidor = tmp_path / "servidor"
    ruta_remota.guardar_ajustes(True, str(servidor))

    ruta_remota.guardar_ajustes(False, str(servidor))

    assert ruta_remota.leer_ajustes() == ruta_remota.Ajustes(False, str(servidor))


def test_activa_y_conectada_manda_el_servidor(tmp_path):
    servidor = tmp_path / "servidor"
    servidor.mkdir()
    ruta_remota.guardar_ajustes(True, str(servidor))

    assert ruta_remota.raiz_remota() == servidor
    assert ruta_remota.en_uso()
    assert ruta_remota.carpeta_input(tmp_path) == servidor / "input"
    assert ruta_remota.carpeta_output(tmp_path) == servidor / "output"


def test_sin_conexion_se_trabaja_con_lo_local(tmp_path):
    ruta_remota.guardar_ajustes(True, str(tmp_path / "no-existe"))

    assert ruta_remota.raiz_remota() is None
    assert ruta_remota.carpeta_input(tmp_path) == tmp_path / "input"
    assert ruta_remota.carpeta_output(tmp_path) == tmp_path / "output"


def test_activa_sin_ruta_escrita_es_local(tmp_path):
    ruta_remota.guardar_ajustes(True, "   ")

    assert ruta_remota.raiz_remota() is None
    assert ruta_remota.carpeta_output(tmp_path) == tmp_path / "output"


def test_una_carpeta_que_no_contesta_no_detiene_al_programa(tmp_path, monkeypatch):
    """Una ruta de red caida tarda medio minuto en Windows; aqui no."""
    servidor = tmp_path / "servidor"
    servidor.mkdir()
    lento = type(servidor)

    def is_dir_colgado(self):
        time.sleep(5)
        return True

    monkeypatch.setattr(lento, "is_dir", is_dir_colgado)

    inicio = time.monotonic()
    assert not ruta_remota.conectada(servidor, espera_s=0.2)
    assert time.monotonic() - inicio < 2


def test_la_respuesta_se_recuerda_y_no_se_repite_la_consulta(tmp_path, monkeypatch):
    servidor = tmp_path / "servidor"
    servidor.mkdir()
    consultas = []

    def contar(carpeta, _espera):
        consultas.append(carpeta)
        return False

    monkeypatch.setattr(ruta_remota, "_contesta", contar)

    assert not ruta_remota.conectada(servidor)
    assert not ruta_remota.conectada(servidor)
    assert len(consultas) == 1

    assert not ruta_remota.conectada(servidor, refrescar=True)
    assert len(consultas) == 2


def test_guardar_los_ajustes_olvida_lo_recordado(tmp_path, monkeypatch):
    servidor = tmp_path / "servidor"
    ruta_remota.guardar_ajustes(True, str(servidor))
    assert ruta_remota.raiz_remota() is None

    servidor.mkdir()
    # Sin guardar de nuevo, la caida sigue recordada.
    assert ruta_remota.raiz_remota() is None
    ruta_remota.guardar_ajustes(True, str(servidor))

    assert ruta_remota.raiz_remota() == servidor


def test_preparar_crea_las_carpetas_sin_tocar_lo_que_hay(tmp_path):
    servidor = tmp_path / "servidor"
    (servidor / "input").mkdir(parents=True)
    existente = servidor / "input" / "bitacora.pdf"
    existente.write_bytes(b"PDF")

    ruta_remota.preparar(servidor)

    assert (servidor / "input" / "processed").is_dir()
    assert (servidor / "output").is_dir()
    assert existente.read_bytes() == b"PDF"
