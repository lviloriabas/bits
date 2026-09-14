"""El cronometro de la ventana de Web Reports.

Es el mismo panel de la ventana principal, con las mismas tres cifras, pero
contando bitacoras en vez de paginas. Lo que se comprueba aqui es que arranca
con el trabajo, que sigue la cuenta que manda el hilo, que aprende de una
corrida terminada y que no aprende de una que se corto.
"""

from __future__ import annotations

import pytest

from app.gui import web_reports_tiempos as tiempos
from app.gui.cronometro import EN_CERO
from app.gui.web_reports_tiempos import (
    TAREA_CONSULTA,
    TAREA_CORRECCION,
    Tiempos,
)
from app.gui.web_reports_window import WebReportsWindow


@pytest.fixture
def medidas(monkeypatch, tmp_path):
    """Las medidas de este equipo, en un archivo que solo ve la prueba."""
    archivo = tmp_path / ".web_reports.json"
    monkeypatch.setattr(tiempos, "ARCHIVO", archivo)
    return archivo


def _cifras(ventana) -> dict[str, str]:
    return {
        clave: etiqueta.text()
        for clave, etiqueta in ventana.cronometro.etiquetas.items()
    }


def test_el_reloj_empieza_en_cero_y_quieto(app, tmp_path, medidas) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        assert _cifras(ventana) == {
            "elapsed": EN_CERO,
            "remaining": EN_CERO,
            "total": EN_CERO,
        }
        assert not ventana._timer.isActive()
    finally:
        ventana.close()


def test_el_reloj_lleva_los_rotulos_de_la_ventana_principal(
    app, tmp_path, medidas
) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        rotulos = [
            hijo.text()
            for hijo in ventana.cronometro.findChildren(type(ventana.resumen))
        ]
        assert "Estimado" in rotulos
        assert "Restante" in rotulos
        assert "Transcurrido" in rotulos
    finally:
        ventana.close()


def test_al_lanzar_el_trabajo_el_reloj_ya_dice_cuanto_va_a_costar(
    app, tmp_path, medidas
) -> None:
    """Sin haber hecho nada todavia, con lo que costo la vez pasada."""
    tiempos.guardar(TAREA_CORRECCION, Tiempos(apertura_s=10.0, unidad_s=60.0))
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._arrancar_cronometro(TAREA_CORRECCION, 3)

        # Diez segundos de apertura y tres bitacoras de un minuto.
        assert _cifras(ventana)["total"] == "00:03:10"
        assert ventana._timer.isActive()
    finally:
        ventana.close()


def test_la_cuenta_del_hilo_acorta_lo_que_falta(app, tmp_path, medidas) -> None:
    tiempos.guardar(TAREA_CORRECCION, Tiempos(apertura_s=1.0, unidad_s=60.0))
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._arrancar_cronometro(TAREA_CORRECCION, 4)
        antes = ventana._estimacion.restante()

        ventana._al_pasar(2, 4)

        assert ventana._estimacion.hechas == 2
        assert ventana._estimacion.restante() < antes
    finally:
        ventana.close()


def test_el_total_que_manda_el_hilo_es_el_que_vale(
    app, tmp_path, medidas
) -> None:
    """El plan trae filas de revision manual que no se abren en Edge."""
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._arrancar_cronometro(TAREA_CORRECCION, 9)

        ventana._al_pasar(0, 3)

        assert ventana._estimacion.unidades == 3
    finally:
        ventana.close()


def test_una_corrida_terminada_deja_sus_tiempos_para_la_siguiente(
    app, tmp_path, medidas
) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._arrancar_cronometro(TAREA_CONSULTA, 2)
        ventana._al_pasar(0, 2)
        ventana._al_pasar(2, 2)

        ventana._al_recibir([])

        assert medidas.exists()
        assert ventana._estimacion is None
        assert not ventana._timer.isActive()
        # Terminado no queda nada por delante, y el estimado es lo que costo.
        cifras = _cifras(ventana)
        assert cifras["remaining"] == EN_CERO
        assert cifras["total"] == cifras["elapsed"]
    finally:
        ventana.close()


def test_una_corrida_cortada_no_ensena_un_trabajo_mas_corto_del_que_es(
    app, tmp_path, medidas
) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._arrancar_cronometro(TAREA_CONSULTA, 10)
        ventana._al_pasar(1, 10)

        ventana._al_cancelar()

        assert not medidas.exists()
        assert not ventana._timer.isActive()
        # Queda el tiempo gastado y ningun pronostico que ya no se debe.
        assert _cifras(ventana)["remaining"] == EN_CERO
    finally:
        ventana.close()


def test_un_fallo_tampoco_ensena_nada(app, tmp_path, medidas) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._arrancar_cronometro(TAREA_CORRECCION, 5)
        ventana._al_pasar(0, 5)

        ventana._al_fallar_correccion("Edge se cerro")

        assert not medidas.exists()
        assert ventana._estimacion is None
    finally:
        ventana.close()


def test_sin_trabajo_en_marcha_los_avisos_no_rompen_nada(
    app, tmp_path, medidas
) -> None:
    """Los resultados de un trabajo ya cerrado siguen llegando a la tabla."""
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_pasar(1, 2)
        ventana._al_latir()
        ventana._al_recibir([])

        assert ventana._estimacion is None
        assert _cifras(ventana)["elapsed"] == EN_CERO
    finally:
        ventana.close()


def test_abrir_una_busqueda_se_mide_como_apertura_y_nada_mas(
    app, tmp_path, medidas
) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._arrancar_cronometro(tiempos.TAREA_BUSQUEDA, 0)

        ventana._al_abrir("HP-9913CMP")

        assert medidas.exists()
        medido = tiempos.cargar(tiempos.TAREA_BUSQUEDA)
        assert medido.unidad_s == 0.0
        assert ventana._estimacion is None
    finally:
        ventana.close()
