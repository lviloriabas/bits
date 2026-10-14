"""La memoria de libros se junta con la del servidor sin pisarla."""

from __future__ import annotations

import json
import os
import time

import pytest

from app.utils import ruta_remota
from app.validation import memoria_remota
from app.validation.memoria_remota import (
    BASE_FILENAME,
    CAMBIOS_FILENAME,
    CANDADO_FILENAME,
    CandadoOcupado,
    sincronizar,
    sincronizar_en,
)

MATRICULAS = "book_matriculas.json"
FECHAS = "book_fechas.json"


@pytest.fixture(autouse=True)
def _sin_conexiones_recordadas():
    ruta_remota.olvidar_conexion()
    yield
    ruta_remota.olvidar_conexion()


@pytest.fixture
def lados(tmp_path):
    local = tmp_path / "local"
    servidor = tmp_path / "servidor"
    local.mkdir()
    servidor.mkdir()
    return local, servidor


def escribir(carpeta, matriculas=None, fechas=None):
    if matriculas is not None:
        (carpeta / MATRICULAS).write_text(json.dumps(matriculas), encoding="utf-8")
    if fechas is not None:
        (carpeta / FECHAS).write_text(json.dumps(fechas), encoding="utf-8")


def leer(carpeta, nombre):
    ruta = carpeta / nombre
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.is_file() else {}


def juntar(local, servidor):
    return sincronizar_en(servidor, local / MATRICULAS, local / FECHAS)


def test_la_primera_vez_sube_la_memoria_de_quien_ya_la_tenia(lados):
    local, servidor = lados
    escribir(
        local,
        {"23001B": "HP-1728CMP", "23003A": "HP-1719CMP"},
        {"23001B": {"76": "2026-08-13", "93": "2026-08-16"}},
    )

    resumen = juntar(local, servidor)

    assert leer(servidor, MATRICULAS) == leer(local, MATRICULAS)
    assert leer(servidor, FECHAS) == leer(local, FECHAS)
    assert resumen.subidos == 3
    assert resumen.traidos == 0
    assert not resumen.reemplazos


def test_cada_lado_recibe_lo_que_le_faltaba(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"}, {"23001B": {"76": "2026-08-13"}})
    escribir(servidor, {"23003A": "HP-1719CMP"}, {"23003A": {"16": "2026-08-12"}})

    resumen = juntar(local, servidor)

    juntas = {"23001B": "HP-1728CMP", "23003A": "HP-1719CMP"}
    assert leer(local, MATRICULAS) == juntas
    assert leer(servidor, MATRICULAS) == juntas
    assert leer(local, FECHAS) == leer(servidor, FECHAS)
    assert set(leer(local, FECHAS)) == {"23001B", "23003A"}
    assert (resumen.subidos, resumen.traidos) == (2, 2)


def test_si_no_coinciden_manda_el_servidor(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    escribir(servidor, {"23001B": "HP-1730CMP"})

    resumen = juntar(local, servidor)

    assert leer(local, MATRICULAS) == {"23001B": "HP-1730CMP"}
    assert leer(servidor, MATRICULAS) == {"23001B": "HP-1730CMP"}
    assert resumen.traidos == 1
    assert not resumen.reemplazos
    assert not (servidor / CAMBIOS_FILENAME).exists()


def test_una_correccion_local_posterior_sube_y_queda_anotada(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    juntar(local, servidor)
    # La comprobacion contra AirVault corrige la entrada en este equipo.
    escribir(local, {"23001B": "HP-1730CMP"})

    resumen = juntar(local, servidor)

    assert leer(servidor, MATRICULAS) == {"23001B": "HP-1730CMP"}
    assert leer(local, MATRICULAS) == {"23001B": "HP-1730CMP"}
    assert len(resumen.reemplazos) == 1
    linea = json.loads(
        (servidor / CAMBIOS_FILENAME).read_text(encoding="utf-8").strip()
    )
    assert linea["libro"] == "23001B"
    assert linea["antes"] == "HP-1728CMP"
    assert linea["despues"] == "HP-1730CMP"


def test_si_el_servidor_tambien_cambio_la_correccion_local_no_lo_pisa(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    juntar(local, servidor)
    escribir(local, {"23001B": "HP-1730CMP"})
    escribir(servidor, {"23001B": "HP-1723CMP"})

    juntar(local, servidor)

    assert leer(servidor, MATRICULAS) == {"23001B": "HP-1723CMP"}
    assert leer(local, MATRICULAS) == {"23001B": "HP-1723CMP"}


def test_lo_que_cambia_en_el_servidor_llega_al_equipo(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    juntar(local, servidor)
    escribir(servidor, {"23001B": "HP-1730CMP"})

    resumen = juntar(local, servidor)

    assert leer(local, MATRICULAS) == {"23001B": "HP-1730CMP"}
    assert resumen.traidos == 1
    assert not resumen.reemplazos


def test_descartar_un_libro_aqui_no_lo_borra_del_servidor_ni_lo_trae_de_vuelta(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP", "23003A": "HP-1719CMP"})
    juntar(local, servidor)
    # La flota de este equipo no trae ese avion y la comprobacion lo quita.
    escribir(local, {"23003A": "HP-1719CMP"})

    resumen = juntar(local, servidor)

    assert leer(servidor, MATRICULAS) == {
        "23001B": "HP-1728CMP", "23003A": "HP-1719CMP",
    }
    assert leer(local, MATRICULAS) == {"23003A": "HP-1719CMP"}
    assert not resumen.hay_cambios


def test_el_libro_descartado_vuelve_si_el_servidor_dice_algo_nuevo(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP", "23003A": "HP-1719CMP"})
    juntar(local, servidor)
    escribir(local, {"23003A": "HP-1719CMP"})
    juntar(local, servidor)
    escribir(servidor, {"23001B": "HP-1730CMP", "23003A": "HP-1719CMP"})

    juntar(local, servidor)

    assert leer(local, MATRICULAS)["23001B"] == "HP-1730CMP"


def test_un_servidor_vaciado_no_vacia_la_memoria_de_los_equipos(lados):
    local, servidor = lados
    memoria = {"23001B": "HP-1728CMP", "23003A": "HP-1719CMP"}
    escribir(local, memoria)
    juntar(local, servidor)
    (servidor / MATRICULAS).unlink()

    juntar(local, servidor)

    assert leer(local, MATRICULAS) == memoria
    assert leer(servidor, MATRICULAS) == memoria


def test_un_archivo_danado_del_servidor_no_se_sobrescribe(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    (servidor / MATRICULAS).write_text('{"23003A":"HP-17', encoding="utf-8")

    with pytest.raises(OSError):
        juntar(local, servidor)

    assert (servidor / MATRICULAS).read_text(encoding="utf-8") == '{"23003A":"HP-17'
    assert leer(local, MATRICULAS) == {"23001B": "HP-1728CMP"}
    assert not (servidor / CANDADO_FILENAME).exists()


def test_lo_que_esta_version_no_entiende_del_servidor_se_conserva(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP", "23003A": "HP-1719CMP"})
    # Otra version del programa escribio dos entradas con otra forma.
    escribir(
        servidor,
        {"23003A": {"matricula": "HP-1719CMP", "votos": 4}, "9": "otra cosa"},
    )

    juntar(local, servidor)

    assert leer(servidor, MATRICULAS) == {
        "23001B": "HP-1728CMP",
        "23003A": {"matricula": "HP-1719CMP", "votos": 4},
        "9": "otra cosa",
    }
    assert leer(local, MATRICULAS) == {
        "23001B": "HP-1728CMP", "23003A": "HP-1719CMP",
    }
    assert not (servidor / CAMBIOS_FILENAME).exists()


def test_una_instalacion_que_perdio_su_memoria_la_recupera(lados):
    local, servidor = lados
    memoria = {"23001B": "HP-1728CMP"}
    escribir(local, memoria, {"23001B": {"76": "2026-08-13"}})
    juntar(local, servidor)
    (local / MATRICULAS).unlink()
    (local / FECHAS).unlink()

    juntar(local, servidor)

    assert leer(local, MATRICULAS) == memoria
    assert leer(local, FECHAS) == {"23001B": {"76": "2026-08-13"}}


def test_las_fechas_que_no_se_contradicen_se_unen_en_los_extremos(lados):
    local, servidor = lados
    escribir(local, fechas={"23001B": {"52": "2026-08-10", "60": "2026-08-12"}})
    escribir(servidor, fechas={"23001B": {"76": "2026-08-13", "93": "2026-08-16"}})

    juntar(local, servidor)

    unidas = {"23001B": {"52": "2026-08-10", "93": "2026-08-16"}}
    assert leer(local, FECHAS) == unidas
    assert leer(servidor, FECHAS) == unidas


def test_las_fechas_que_se_contradicen_quedan_como_las_tiene_el_servidor(lados):
    local, servidor = lados
    # Juntas obligarian al libro a retroceder.
    escribir(local, fechas={"23001B": {"80": "2026-08-01"}})
    escribir(servidor, fechas={"23001B": {"76": "2026-08-13", "93": "2026-08-16"}})

    juntar(local, servidor)

    del_servidor = {"23001B": {"76": "2026-08-13", "93": "2026-08-16"}}
    assert leer(servidor, FECHAS) == del_servidor
    assert leer(local, FECHAS) == del_servidor


def test_la_misma_pagina_con_dos_fechas_se_resuelve_con_el_servidor(lados):
    local, servidor = lados
    escribir(local, fechas={"23001B": {"76": "2026-08-14"}})
    escribir(servidor, fechas={"23001B": {"76": "2026-08-13"}})

    juntar(local, servidor)

    assert leer(local, FECHAS) == {"23001B": {"76": "2026-08-13"}}
    assert leer(servidor, FECHAS) == {"23001B": {"76": "2026-08-13"}}


def test_una_fecha_ampliada_aqui_despues_de_sincronizar_sube(lados):
    local, servidor = lados
    escribir(local, fechas={"23001B": {"76": "2026-08-13"}})
    juntar(local, servidor)
    escribir(local, fechas={"23001B": {"76": "2026-08-13", "93": "2026-08-16"}})

    juntar(local, servidor)

    assert leer(servidor, FECHAS) == {
        "23001B": {"76": "2026-08-13", "93": "2026-08-16"},
    }


def test_sincronizar_dos_veces_no_cambia_nada(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"}, {"23001B": {"76": "2026-08-13"}})
    escribir(servidor, {"23003A": "HP-1719CMP"})
    juntar(local, servidor)
    antes = {
        ruta.name: ruta.stat().st_mtime_ns
        for carpeta in (local, servidor)
        for ruta in carpeta.glob("book_*.json")
        if ruta.name != BASE_FILENAME
    }

    resumen = juntar(local, servidor)

    assert not resumen.hay_cambios
    assert antes == {
        ruta.name: ruta.stat().st_mtime_ns
        for carpeta in (local, servidor)
        for ruta in carpeta.glob("book_*.json")
        if ruta.name != BASE_FILENAME
    }


def test_dos_equipos_acaban_con_la_misma_memoria(tmp_path):
    servidor = tmp_path / "servidor"
    uno = tmp_path / "uno"
    dos = tmp_path / "dos"
    for carpeta in (servidor, uno, dos):
        carpeta.mkdir()
    escribir(uno, {"23001B": "HP-1728CMP"})
    escribir(dos, {"23003A": "HP-1719CMP"})

    juntar(uno, servidor)
    juntar(dos, servidor)
    juntar(uno, servidor)

    juntas = {"23001B": "HP-1728CMP", "23003A": "HP-1719CMP"}
    assert leer(uno, MATRICULAS) == juntas
    assert leer(dos, MATRICULAS) == juntas
    assert leer(servidor, MATRICULAS) == juntas


def test_con_el_turno_ocupado_no_se_escribe_nada(lados, monkeypatch):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    (servidor / CANDADO_FILENAME).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(memoria_remota, "ESPERA_CANDADO_S", 0.3)

    with pytest.raises(CandadoOcupado):
        with memoria_remota._turno(servidor, espera_s=0.3):
            pass

    assert not (servidor / MATRICULAS).exists()
    assert (servidor / CANDADO_FILENAME).exists()


def test_un_candado_abandonado_se_retira(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    candado = servidor / CANDADO_FILENAME
    candado.write_text("{}", encoding="utf-8")
    viejo = time.time() - memoria_remota.CANDADO_VIEJO_S - 60
    os.utime(candado, (viejo, viejo))

    juntar(local, servidor)

    assert leer(servidor, MATRICULAS) == {"23001B": "HP-1728CMP"}
    assert not candado.exists()


def test_la_ruta_remota_apuntando_a_la_propia_carpeta_no_hace_nada(lados):
    local, _servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})

    resumen = sincronizar_en(local, local / MATRICULAS, local / FECHAS)

    assert not resumen.hay_cambios
    assert not (local / BASE_FILENAME).exists()


def test_apagada_no_sincroniza(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    ruta_remota.guardar_ajustes(False, str(servidor))

    assert sincronizar(local / MATRICULAS, local / FECHAS) is None
    assert not (servidor / MATRICULAS).exists()


def test_activa_sincroniza_con_la_carpeta_elegida(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    ruta_remota.guardar_ajustes(True, str(servidor))

    resumen = sincronizar(local / MATRICULAS, local / FECHAS)

    assert resumen is not None and resumen.subidos == 1
    assert leer(servidor, MATRICULAS) == {"23001B": "HP-1728CMP"}


def test_sin_conexion_la_memoria_local_queda_como_estaba(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    ruta_remota.guardar_ajustes(True, str(servidor / "no-existe"))

    assert sincronizar(local / MATRICULAS, local / FECHAS) is None
    assert leer(local, MATRICULAS) == {"23001B": "HP-1728CMP"}


def test_un_fallo_del_servidor_no_tumba_la_ejecucion(lados):
    local, servidor = lados
    escribir(local, {"23001B": "HP-1728CMP"})
    (servidor / MATRICULAS).write_text("no es json", encoding="utf-8")
    ruta_remota.guardar_ajustes(True, str(servidor))

    assert sincronizar(local / MATRICULAS, local / FECHAS) is None
    assert leer(local, MATRICULAS) == {"23001B": "HP-1728CMP"}
