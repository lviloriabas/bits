"""Lo que el cronometro de Web Reports cuenta y lo que aprende al terminar."""

from __future__ import annotations

import json

import pytest

from app.gui import web_reports_tiempos as tiempos
from app.gui.web_reports_tiempos import (
    TAREA_CONSULTA,
    TAREA_CORRECCION,
    Estimacion,
    Tiempos,
)


@pytest.fixture
def medidas(monkeypatch, tmp_path):
    """Las medidas de este equipo, en un archivo que solo ve la prueba."""
    archivo = tmp_path / ".web_reports.json"
    monkeypatch.setattr(tiempos, "ARCHIVO", archivo)
    return archivo


def test_sin_medidas_se_estima_con_las_de_fabrica(medidas) -> None:
    assert tiempos.cargar(TAREA_CONSULTA) == tiempos.POR_OMISION[
        TAREA_CONSULTA
    ]


def test_lo_medido_vuelve_en_la_corrida_siguiente(medidas) -> None:
    tiempos.guardar(TAREA_CORRECCION, Tiempos(apertura_s=12.0, unidad_s=7.5))

    assert tiempos.cargar(TAREA_CORRECCION) == Tiempos(12.0, 7.5)


def test_una_medida_imposible_no_se_guarda_como_esta(medidas) -> None:
    """Una espera de horas es el servidor caido, no el ritmo del equipo."""
    tiempos.guardar(TAREA_CONSULTA, Tiempos(apertura_s=99999.0, unidad_s=0.0))

    assert tiempos.cargar(TAREA_CONSULTA).apertura_s == tiempos.MAXIMO_S


def test_un_archivo_roto_no_rompe_la_ventana(medidas) -> None:
    medidas.write_text("{esto no es json", encoding="utf-8")

    assert tiempos.cargar(TAREA_CONSULTA) == tiempos.POR_OMISION[
        TAREA_CONSULTA
    ]


def test_mientras_se_abre_la_sesion_se_cuenta_con_lo_guardado(
    medidas,
) -> None:
    estimacion = Estimacion(
        TAREA_CORRECCION,
        unidades=4,
        tiempos=Tiempos(apertura_s=30.0, unidad_s=10.0),
        inicio=0.0,
    )

    # A los diez segundos faltan veinte de apertura y las cuatro bitacoras.
    assert estimacion.restante(ahora=10.0) == 60.0


def test_abierta_la_sesion_manda_el_ritmo_de_esta_corrida(medidas) -> None:
    """Con lo guardado serian cinco minutos; esta corrida va al doble."""
    estimacion = Estimacion(
        TAREA_CORRECCION,
        unidades=10,
        tiempos=Tiempos(apertura_s=5.0, unidad_s=30.0),
        inicio=0.0,
    )
    estimacion.avanzo(0, 10, ahora=5.0)
    estimacion.avanzo(5, 10, ahora=80.0)

    # Cinco bitacoras en setenta y cinco segundos son quince por bitacora, y
    # quedan cinco: el historico ya casi no pesa.
    assert 75.0 <= estimacion.restante(ahora=80.0) < 100.0


def test_el_total_lo_dice_quien_hace_el_trabajo(medidas) -> None:
    """El plan trae filas de revision manual que no se abren en Edge."""
    estimacion = Estimacion(TAREA_CORRECCION, unidades=9, inicio=0.0)

    estimacion.avanzo(0, 3, ahora=1.0)

    assert estimacion.unidades == 3


def test_la_cuenta_de_hechas_no_retrocede(medidas) -> None:
    estimacion = Estimacion(TAREA_CORRECCION, unidades=5, inicio=0.0)
    estimacion.avanzo(3, 5, ahora=10.0)

    estimacion.avanzo(1, 5, ahora=11.0)

    assert estimacion.hechas == 3


def test_al_terminar_guarda_lo_que_costo_de_verdad(medidas) -> None:
    estimacion = Estimacion(TAREA_CORRECCION, unidades=2, inicio=0.0)
    estimacion.avanzo(0, 2, ahora=20.0)
    estimacion.avanzo(2, 2, ahora=60.0)

    estimacion.aprender(ahora=60.0)

    assert tiempos.cargar(TAREA_CORRECCION) == Tiempos(20.0, 20.0)


def test_un_trabajo_que_no_llego_a_abrir_no_ensena_nada(medidas) -> None:
    estimacion = Estimacion(TAREA_CONSULTA, unidades=2, inicio=0.0)

    assert estimacion.aprender(ahora=9.0) is None
    assert not medidas.exists()


def test_sin_unidades_terminadas_se_conserva_el_costo_por_unidad(
    medidas,
) -> None:
    """Escribir un cero daria por instantaneo el trabajo de la vez siguiente."""
    estimacion = Estimacion(
        tiempos.TAREA_BUSQUEDA,
        unidades=0,
        tiempos=Tiempos(apertura_s=15.0, unidad_s=40.0),
        inicio=0.0,
    )
    estimacion.abrio(ahora=8.0)

    estimacion.aprender(ahora=8.0)

    assert tiempos.cargar(tiempos.TAREA_BUSQUEDA) == Tiempos(8.0, 40.0)


def test_cada_trabajo_guarda_lo_suyo_sin_pisar_al_otro(medidas) -> None:
    tiempos.guardar(TAREA_CONSULTA, Tiempos(10.0, 100.0))
    tiempos.guardar(TAREA_CORRECCION, Tiempos(11.0, 20.0))

    datos = json.loads(medidas.read_text(encoding="utf-8"))

    assert sorted(datos) == [TAREA_CONSULTA, TAREA_CORRECCION]
