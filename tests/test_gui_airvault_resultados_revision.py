"""Guardar datos de REVISAR no equivale a validar sus paginas en verde."""

import pytest
from PySide6.QtGui import QColor

from app.airvault.config import AirVaultConfig
from app.airvault.flujo import Trabajo
from app.airvault.indexer import Resultado
from app.airvault.model import EstadoEtapa, Manifiesto, Registro
from app.gui.airvault_window import AirVaultWindow
from app.gui.tokens import paleta


@pytest.mark.parametrize("revision", [False, True])
def test_el_aviso_por_batch_distingue_guardado_de_validacion(app, tmp_path, revision):
    ventana = AirVaultWindow(tmp_path)
    manifiesto = Manifiesto(
        job_id="prueba", nombre_batch="DP | PRUEBA REVISAR" if revision else "DP | PRUEBA",
        solo_subir=revision, batch_id="003TH7", registros=[Registro(seq=1)],
    )
    for etapa in ("subir", "indexar", "verificar"):
        manifiesto.etapa(etapa).marcar(EstadoEtapa.HECHA)
    trabajo = Trabajo(AirVaultConfig(), tmp_path / "job", manifiesto)
    ventana._trabajos = [trabajo]
    ventana._al_batch_indexado({"trabajo": trabajo, "validas": 1, "total": 1})
    resumen = ventana.resumen.text()
    bitacora = ventana.bitacora.item(ventana.bitacora.count() - 1).text()
    for texto in (resumen, bitacora):
        if revision:
            assert "datos disponibles guardados" in texto
            assert "amarillo" in texto
            assert "en verde" not in texto
        else:
            assert "1 de 1 páginas en verde" in texto
    if revision:
        for columna in range(ventana.lotes.columnCount()):
            assert ventana.lotes.item(0, columna).foreground().color() == QColor(
                paleta().STATUS_WARNING
            )
    ventana.close()


def test_la_tanda_con_revision_no_cuenta_datos_guardados_como_verdes(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    ventana._al_indexar({
        "resultado": Resultado(escritas=8), "validas": 8, "total": 8,
        "incluye_revision": True, "lotes": 2,
    })
    texto = ventana.resumen.text()
    assert "8 de 8 páginas con los datos disponibles guardados" in texto
    assert "REVISAR guardado" in texto
    assert "amarillo" in texto
    assert "en verde" not in texto
    ventana.close()


@pytest.mark.parametrize("completar", [False, True])
def test_reabrir_una_ejecucion_terminada_recupera_el_cien_por_ciento(
    app, tmp_path, monkeypatch, completar,
):
    ventana = AirVaultWindow(tmp_path)
    ventana.completar_check.setChecked(completar)
    trabajos = []
    for revision in (False, True):
        manifiesto = Manifiesto(
            job_id=f"parte-{revision}", nombre_batch="DP | PRUEBA",
            solo_subir=revision, batch_id=f"003TH{7 if revision else 6}",
            registros=[Registro(seq=1)],
        )
        for etapa in ("subir", "indexar", "verificar"):
            manifiesto.etapa(etapa).marcar(EstadoEtapa.HECHA)
        if completar and not revision:
            manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
            manifiesto.completado_automatico = True
        trabajos.append(Trabajo(AirVaultConfig(), tmp_path / manifiesto.job_id, manifiesto))
    monkeypatch.setattr("app.airvault.flujo.cargar_partes", lambda *a: trabajos)
    monkeypatch.setattr("app.airvault.flujo.cargar_trabajos_pendientes", lambda *a: [])
    ventana._corrida = str(tmp_path / "output/prueba/datos/prueba.CSV")
    ventana._cargar_trabajos(tmp_path / "job", tmp_path / "prueba.CSV")
    assert ventana.progreso.text() == "100% - Proceso terminado"
    assert ventana.estado_label.text() == "Proceso terminado"
    assert "REVISAR quedan para revisión manual" in ventana.resumen.text()
    assert "Sin subir" not in ventana.resumen.text()
    assert not ventana._vigilando()
    ventana.close()


def test_el_aviso_de_fin_no_se_pierde_si_el_hilo_aun_esta_terminando(
    app, tmp_path, monkeypatch,
):
    from tests.test_gui_airvault_window import parte

    ventana = AirVaultWindow(tmp_path)
    ventana._estados = [parte("completado")]
    ventana._fin_pendiente = True
    monkeypatch.setattr(ventana, "hilo", lambda: object())
    ventana._anunciar_fin()
    assert ventana._fin_pendiente
    monkeypatch.setattr(ventana, "hilo", lambda: None)
    ventana._anunciar_fin()
    assert ventana.progreso.text() == "100% - Proceso terminado"
    assert not ventana._fin_pendiente
    ventana.close()
