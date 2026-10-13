"""La carpeta portable se copia a otra PC y los batches siguen pudiendo subirse."""

import shutil
from pathlib import Path

from app.airvault import manifest as manifiestos
from app.airvault.config import AirVaultConfig
from app.airvault.flujo import (
    Trabajo,
    cargar_trabajos_pendientes,
    preparar_partes,
)
from tests.test_airvault_comprobar import corrida

NOMBRE = "BITS 18 AUG 2026 05 42"


def instalacion_movida(tmp_path):
    """Prepara una entrega en una carpeta y la muda entera a otra."""
    vieja, nueva = tmp_path / "pc-anterior", tmp_path / "pc-nueva"
    csv = corrida(vieja, NOMBRE)
    trabajos = preparar_partes(
        AirVaultConfig(), vieja / "output" / "airvault" / NOMBRE, csv, "DP | BIT"
    )
    assert all(Path(t.manifiesto.pdf_origen).is_file() for t in trabajos)
    carpetas = [t.carpeta.relative_to(vieja) for t in trabajos]
    shutil.move(str(vieja), str(nueva))
    return nueva, [nueva / carpeta for carpeta in carpetas]


def test_el_manifiesto_encuentra_su_pdf_y_su_csv_en_la_carpeta_nueva(tmp_path):
    nueva, carpetas = instalacion_movida(tmp_path)
    guardado = manifiestos.cargar(carpetas[0])
    assert not Path(guardado.pdf_origen).is_file()

    trabajo = Trabajo.cargar(AirVaultConfig(), carpetas[0])

    pdf, csv = Path(trabajo.manifiesto.pdf_origen), Path(trabajo.manifiesto.csv_origen)
    assert pdf.is_file() and pdf.is_relative_to(nueva.resolve())
    assert pdf.name == Path(guardado.pdf_origen).name
    assert csv.is_file() and csv.is_relative_to(nueva.resolve())
    # Cargar no escribe: la ruta nueva se guarda con el siguiente cambio.
    assert manifiestos.cargar(carpetas[0]).pdf_origen == guardado.pdf_origen
    trabajo.guardar()
    assert manifiestos.cargar(carpetas[0]).pdf_origen == str(pdf)


def test_un_batch_sin_subir_sigue_pendiente_tras_mudar_la_carpeta(tmp_path):
    """Sin su PDF «no habia nada que enviar» y el batch dejaba de ofrecerse."""
    nueva, carpetas = instalacion_movida(tmp_path)

    pendientes = cargar_trabajos_pendientes(
        AirVaultConfig(), nueva / "output" / "airvault"
    )

    assert [t.carpeta for t in pendientes] == carpetas
    assert all(Path(t.manifiesto.pdf_origen).is_file() for t in pendientes)


def test_no_se_cambia_una_ruta_que_sigue_siendo_valida(tmp_path):
    csv = corrida(tmp_path, NOMBRE)
    trabajos = preparar_partes(
        AirVaultConfig(), tmp_path / "output" / "airvault" / NOMBRE, csv, "DP | BIT"
    )
    antes = (trabajos[0].manifiesto.pdf_origen, trabajos[0].manifiesto.csv_origen)

    cargado = Trabajo.cargar(AirVaultConfig(), trabajos[0].carpeta)

    assert (cargado.manifiesto.pdf_origen, cargado.manifiesto.csv_origen) == antes


def test_sin_el_archivo_en_la_carpeta_nueva_la_ruta_se_conserva(tmp_path):
    """No se inventa un origen: quien suba dira que falta el PDF."""
    _nueva, carpetas = instalacion_movida(tmp_path)
    guardado = manifiestos.cargar(carpetas[0])
    for pdf in _nueva.rglob("*.pdf"):
        pdf.unlink()

    trabajo = Trabajo.cargar(AirVaultConfig(), carpetas[0])

    assert trabajo.manifiesto.pdf_origen == guardado.pdf_origen
