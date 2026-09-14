"""El clic derecho sobre el historial de «Indexar en AirVault».

Son dos cosas distintas y por eso son dos acciones: eliminar para siempre
los batches locales de una ejecución y deshacerse de la ejecución entera,
que es lo que vacía la lista de lo que ya no hace falta. Ninguna de las dos
toca los batches que ya estén en AirVault.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMessageBox

from app.airvault.config import AirVaultConfig
from app.airvault.flujo import preparar_partes
from app.gui.airvault_window import AirVaultWindow

from test_airvault_entrega import corrida as corrida_de_entrega
from test_gui_airvault_window import corrida, registrar_en_airvault


@pytest.fixture
def papelera(monkeypatch):
    """Recoge lo que se mandaría a la Papelera y lo borra de verdad."""
    import shutil

    enviados: list[Path] = []

    def enviar(rutas):
        enviados.extend(rutas)
        for ruta in rutas:
            if ruta.is_dir():
                shutil.rmtree(ruta)
            else:
                ruta.unlink()
        return list(rutas), []

    monkeypatch.setattr("app.gui.airvault_window.send_to_trash", enviar)
    monkeypatch.setattr(
        "app.gui.airvault_window.QMessageBox.warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    return enviados


def _acciones(ventana, indice: int = 1) -> list[str]:
    """Textos del menú que abriría un clic derecho con esa opción elegida."""
    ventana.historial.setCurrentIndex(indice)
    menu = ventana._acciones_del_historial(
        Path(str(ventana.historial.currentData())),
        ventana.historial.currentText(),
    )
    return [accion.text() for accion in menu.actions()]


def test_el_menu_ofrece_las_dos_eliminaciones(app, tmp_path):
    corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana._refrescar_historial()
        assert ventana.historial.count() == 2

        assert _acciones(ventana) == [
            "Eliminar el registro de AirVault",
            "",  # el separador entre las dos
            "Eliminar la ejecución…",
        ]
    finally:
        ventana.close()
        app.processEvents()


def test_sobre_la_opcion_de_abrir_no_hay_menu(app, tmp_path):
    """«Seleccionar ejecución» no nombra ninguna, así que no ofrece nada."""
    corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana._refrescar_historial()
        ventana.historial.setCurrentIndex(0)

        # No abre nada y, sobre todo, no actúa sobre la primera ejecución
        # por descarte: sin ejecución elegida no hay nada que nombrar.
        assert ventana.historial.currentData() is None
        assert ventana._menu_del_historial(QPoint(5, 5)) is None
    finally:
        ventana.close()
        app.processEvents()


def test_eliminar_el_registro_de_una_fila_que_no_es_la_abierta(
    app, tmp_path, papelera
):
    """Cada fila lleva su propia ejecución; el menú no actúa sobre otra."""
    primera = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    segunda = corrida(tmp_path, "BITS 19 AUG 2026 06 10")
    manifiesto = registrar_en_airvault(tmp_path, primera)
    registrar_en_airvault(tmp_path, segunda)
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana.fijar_corrida(segunda)

        ventana._eliminar_registro(primera)

        # Se fue el de la fila elegida, no el de la ejecución abierta.
        assert papelera == [manifiesto]
        assert not manifiesto.exists()
        # Y la ventana sigue donde estaba, con su ejecución cargada.
        assert ventana.corrida() == segunda
        assert primera.exists()
    finally:
        ventana.close()
        app.processEvents()


def test_el_boton_elimina_el_registro_de_una_ejecucion_que_ya_no_esta(
    app, tmp_path, papelera
):
    """El caso que el botón no alcanzaba: trabajo sin ejecución en la lista.

    Es el que más falta hace olvidar. La ejecución se borró de ``output/``
    (o se salió del historial) y su trabajo se quedó en ``output/airvault``
    con los manifiestos dentro: mientras el botón mirara el historial, esos
    registros no había forma de eliminarlos desde la ventana.
    """
    import shutil

    csv = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    manifiesto = registrar_en_airvault(tmp_path, csv)
    shutil.rmtree(csv.parent.parent)
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana._refrescar_historial()

        assert ventana.historial.count() == 1  # solo «elegir ejecución»
        assert ventana.boton_eliminar_registro.isEnabled()
        ventana.boton_eliminar_registro.trigger()

        assert papelera == [manifiesto]
        assert not manifiesto.exists()
        assert not ventana.boton_eliminar_registro.isEnabled()
    finally:
        ventana.close()
        app.processEvents()


def test_el_boton_elimina_registros_de_todas_las_ejecuciones_presentes(
    app, tmp_path, papelera
):
    primera = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    segunda = corrida(tmp_path, "BITS 19 AUG 2026 06 10")
    manifiestos = {
        registrar_en_airvault(tmp_path, primera),
        registrar_en_airvault(tmp_path, segunda),
    }
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana._refrescar_historial()

        assert ventana.boton_eliminar_registro.isEnabled()
        ventana.boton_eliminar_registro.trigger()

        assert set(papelera) == manifiestos
        assert all(not ruta.exists() for ruta in manifiestos)
        assert primera.exists() and segunda.exists()
        assert not ventana.boton_eliminar_registro.isEnabled()
    finally:
        ventana.close()
        app.processEvents()


def test_eliminar_el_registro_no_reconstruye_los_batches_al_reiniciar(
    app, tmp_path, papelera,
):
    csv, _partes = corrida_de_entrega(tmp_path)
    carpeta = tmp_path / "output" / "airvault" / "BITS 19 AUG 2026 10 00"
    trabajos = preparar_partes(
        AirVaultConfig(), carpeta, csv, paginas_por_batch=5,
    )
    paginas = {
        (r.archivo_origen.casefold(), int(r.pagina_origen))
        for trabajo in trabajos for r in trabajo.manifiesto.bitacoras()
    }
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana._eliminar_registro(csv)

        assert all(not Path(trabajo.carpeta).exists() for trabajo in trabajos)
        reiniciados = preparar_partes(
            AirVaultConfig(), carpeta, csv, paginas_por_batch=5,
        )
        reconstruidas = {
            (r.archivo_origen.casefold(), int(r.pagina_origen))
            for trabajo in reiniciados for r in trabajo.manifiesto.bitacoras()
        }
        assert not (paginas & reconstruidas)
        assert reiniciados == []
    finally:
        ventana.close()
        app.processEvents()


def test_eliminar_la_ejecucion_la_manda_entera_a_la_papelera(
    app, tmp_path, papelera
):
    csv = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    carpeta = csv.parent.parent
    manifiesto = registrar_en_airvault(tmp_path, csv)
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana._refrescar_historial()

        ventana._eliminar_ejecucion(csv, carpeta.name)

        # Primero su memoria de AirVault y después la ejecución: al revés,
        # un fallo dejaría un registro hablando de lo que ya no está.
        assert papelera == [manifiesto.parent, carpeta]
        assert not carpeta.exists()
        assert ventana.historial.count() == 1
    finally:
        ventana.close()
        app.processEvents()


def test_no_se_elimina_la_ejecucion_que_se_esta_subiendo(
    app, tmp_path, papelera, monkeypatch
):
    csv = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana.fijar_corrida(csv)
        monkeypatch.setattr(type(ventana), "hilo", lambda self: object())
        avisos = []
        monkeypatch.setattr(
            "app.gui.airvault_window.QMessageBox.information",
            lambda *args, **kwargs: avisos.append(args[2]),
        )

        ventana._eliminar_ejecucion(csv, csv.parent.parent.name)

        assert papelera == []
        assert csv.exists()
        assert avisos and "Cancele el trabajo" in avisos[0]
    finally:
        # El hilo falso solo vale mientras dura la comprobación: cerrar la
        # ventana le pediría cancelar y ese objeto no sabe hacerlo.
        monkeypatch.undo()
        ventana.close()
        app.processEvents()


def test_una_ejecucion_de_fuera_de_output_no_se_elimina_desde_aqui(
    app, tmp_path, papelera, monkeypatch
):
    """La ventana puede apuntar a un CSV de fuera; eso no se borra aquí."""
    suelto = tmp_path / "por_ahi" / "datos" / "suelta.csv"
    suelto.parent.mkdir(parents=True)
    suelto.write_text("file,page\n", encoding="utf-8")
    ventana = AirVaultWindow(tmp_path)
    try:
        avisos = []
        monkeypatch.setattr(
            "app.gui.airvault_window.QMessageBox.information",
            lambda *args, **kwargs: avisos.append(args[2]),
        )

        ventana._eliminar_ejecucion(suelto, "suelta")

        assert papelera == []
        assert suelto.exists()
        assert avisos and "output/" in avisos[0]
    finally:
        ventana.close()
        app.processEvents()
