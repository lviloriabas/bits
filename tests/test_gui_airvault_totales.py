"""El avance conserva el total de bitácoras al cambiar o reintentar batches."""

from types import SimpleNamespace

import pytest

from app.airvault.config import AirVaultConfig
from app.airvault.flujo import estado_local
from app.airvault.model import EstadoEtapa, EstadoRegistro, Manifiesto, Registro
from app.gui.airvault_window import AirVaultWindow


def trabajo(carpeta, nombre, cantidad, subido=False):
    manifiesto = Manifiesto(
        job_id=carpeta, nombre_batch=nombre, csv_origen=f"{carpeta}/datos.csv",
        registros=[Registro(seq=1, separador="Aeronave")] + [
            Registro(seq=i + 2, log_number=f"20000{i:02d}") for i in range(cantidad)
        ],
    )
    if subido:
        manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
        manifiesto.batch_id = carpeta.upper()
    return SimpleNamespace(carpeta=carpeta, manifiesto=manifiesto, config=AirVaultConfig())


@pytest.fixture
def ventana(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    ventana.solo_ejecucion_check.setChecked(False)
    yield ventana
    ventana.close()


def test_el_total_acumula_batches_y_no_cuenta_separadores_o_reintentos(ventana):
    primero = trabajo("uno", "Batch uno", 3, subido=True)
    segundo = trabajo("dos", "Batch dos", 2)
    primero.manifiesto.bitacoras()[0].estado = EstadoRegistro.ESCRITA
    ventana._trabajos = [primero, segundo]
    ventana._estado["trabajos"] = [primero, segundo]
    ventana._estados = [estado_local(t) for t in ventana._trabajos]
    assert "3 de 5 bitácoras subidas, 1 de 5 indexadas" in ventana._totales_bitacoras()

    segundo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    ventana._mostrar_paso("Batch UNO: Escribiendo en AirVault", 1, 2)
    ventana._mostrar_paso("Batch UNO: Escribiendo en AirVault", 0, 1)
    assert "5 de 5 bitácoras subidas, 1 de 5 indexadas" in ventana._totales_bitacoras()
    assert "Batch «Batch uno»" in ventana.bitacora.item(0).text()
    assert "Total:" in ventana.bitacora.item(0).text()
    assert ventana.bitacora.count() == 1


def test_el_total_respeta_la_ejecucion_elegida_y_los_batches_cancelados(ventana):
    actual = trabajo("actual", "Actual", 4, subido=True)
    otro = trabajo("otro", "Otro", 7)
    cancelado = trabajo("cancelado", "Cancelado", 9)
    cancelado.manifiesto.cancelado = True
    cancelado.manifiesto.csv_origen = actual.manifiesto.csv_origen
    ventana._trabajos = [actual, otro, cancelado]
    ventana._corrida = actual.manifiesto.csv_origen
    ventana.solo_ejecucion_check.setChecked(True)
    assert "4 de 4 bitácoras subidas, 0 de 4 indexadas" in ventana._totales_bitacoras()
    ventana.solo_ejecucion_check.setChecked(False)
    assert "4 de 11 bitácoras subidas, 0 de 11 indexadas" in ventana._totales_bitacoras()


def test_antes_de_preparar_los_batches_usa_el_total_de_la_ejecucion(ventana):
    ventana._mostrar_reparto((20, 5))
    ventana._mostrar_paso("Preparando entrega", 0, 0)
    assert "0 de 25 bitácoras subidas, 0 de 25 indexadas" in ventana.bitacora.item(0).text()


def test_la_linea_viva_actualiza_el_total_entre_pasos_del_mismo_batch(ventana):
    actual = trabajo("actual", "Actual", 3, subido=True)
    ventana._trabajos = [actual]
    ventana._worker = SimpleNamespace(isRunning=lambda: True)
    try:
        ventana._actualizar_latido()
        viva = ventana._linea_viva
        assert "0 de 3 indexadas" in viva.text()
        actual.manifiesto.bitacoras()[0].estado = EstadoRegistro.ESCRITA
        ventana._pintar_linea_viva()
        assert "1 de 3 indexadas" in viva.text()
        actual.manifiesto.etapa("verificar").marcar(EstadoEtapa.HECHA)
        ventana._pintar_linea_viva()
        assert "3 de 3 indexadas" in viva.text()
    finally:
        ventana._worker = None
        ventana._actualizar_latido()
