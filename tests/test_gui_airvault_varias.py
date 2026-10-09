"""Seleccion de varias ejecuciones y su subida conjunta, sin red."""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest

from app.airvault.flujo import Trabajo, cargar_partes, carpeta_de_corrida, carpeta_de_trabajo
from app.gui import airvault_window, responsive
from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker
from tests.airvault_fake import AirVaultSimulado, subida_simulada
from tests.test_gui_airvault_cadena import SesionFalsa
from tests.test_gui_airvault_window import corrida, registrar_en_airvault


def seleccionar(ventana, *csvs):
    for csv in csvs:
        indice = ventana.historial.findData(str(csv))
        assert indice > 0
        if ventana.historial.itemData(indice, Qt.ItemDataRole.CheckStateRole) != Qt.CheckState.Checked:
            ventana.historial.marcar(indice)


def test_casillas_reunen_ejecuciones_y_se_conservan_al_refrescar(app, tmp_path):
    primera = corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    segunda = corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    seleccionar(ventana, primera, segunda)
    ventana._refrescar_historial()

    assert set(ventana._corridas_seleccionadas()) == {str(primera), str(segunda)}
    assert ventana.boton_subir.isEnabled()
    assert not ventana.lote_edit.isEnabled()
    assert not ventana.fecha_combo.isEnabled()
    assert "2 ejecuciones" in ventana.windowTitle()

    ventana._al_elegir_del_historial(ventana.historial.findData(str(primera)))
    assert ventana._corridas_seleccionadas() == [str(primera)]
    assert ventana.lote_edit.isEnabled() and ventana.fecha_combo.isEnabled()


def test_el_raton_marca_sin_cerrar_el_selector(app, tmp_path):
    primera = corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana.show()
    ventana.historial.showPopup()
    app.processEvents()
    vista = ventana.historial.view()
    indice = vista.model().index(ventana.historial.findData(str(primera)), 0)
    fila = vista.visualRect(indice)
    QTest.mouseClick(vista.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(fila.left() + 8, fila.center().y()))
    app.processEvents()

    assert len(ventana._corridas_seleccionadas()) == 2
    assert vista.isVisible()
    QTest.mouseClick(
        vista.viewport(), Qt.MouseButton.LeftButton,
        pos=QPoint(fila.left() + 80, fila.center().y()),
    )
    app.processEvents()
    assert ventana._corridas_seleccionadas() == [str(primera)]
    ventana.historial.hidePopup()


def test_espacio_marca_y_desmarca_en_el_selector(app, tmp_path):
    primera = corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    segunda = corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana.show()
    ventana.historial.showPopup()
    vista = ventana.historial.view()
    vista.setCurrentIndex(vista.model().index(ventana.historial.findData(str(primera)), 0))
    QTest.keyClick(vista, Qt.Key.Key_Space)
    assert set(ventana._corridas_seleccionadas()) == {str(primera), str(segunda)}
    QTest.keyClick(vista, Qt.Key.Key_Space)
    assert ventana._corridas_seleccionadas() == [str(segunda)]
    ventana.historial.hidePopup()


def test_sin_marcas_no_se_puede_subir(app, tmp_path):
    csv = corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    ventana.historial.marcar(ventana.historial.findData(str(csv)))
    assert not ventana.boton_subir.isEnabled()
    assert ventana.corrida() is None


def test_una_ejecucion_sin_exportar_no_se_puede_marcar(app, tmp_path):
    corrida(tmp_path)
    sin_exportar = corrida(tmp_path, "BITS 19 AUG 2026 05 50", exportada=False)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    ventana.historial.marcar(ventana.historial.findData(str(sin_exportar)))
    assert str(sin_exportar) not in ventana._corridas_seleccionadas()


def test_filtrar_con_varias_incluye_solo_las_marcadas(app, tmp_path):
    primera = corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    segunda = corrida(tmp_path)
    tercera = corrida(tmp_path, "BITS 16 AUG 2026 05 50")
    for csv in (primera, segunda, tercera):
        registrar_en_airvault(tmp_path, csv)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    ventana.fijar_corrida(segunda)
    seleccionar(ventana, primera, segunda)
    ventana.solo_ejecucion_check.setChecked(True)

    assert {p.trabajo.manifiesto.csv_origen for p in ventana._partes_en_cola()} == {str(primera), str(segunda)}
    assert ventana.lotes.rowCount() == 2


def test_la_seleccion_no_cambia_mientras_trabaja(app, tmp_path):
    primera = corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    segunda = corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    ventana._habilitar(False)
    ventana.historial.marcar(ventana.historial.findData(str(primera)))
    assert ventana._corridas_seleccionadas() == [str(segunda)]
    assert ventana.historial.isEnabled()


def test_el_historial_protege_todas_las_ejecuciones_en_curso(app, tmp_path, monkeypatch):
    primera = corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    segunda = corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    seleccionar(ventana, primera, segunda)
    with monkeypatch.context() as parche:
        parche.setattr(ventana, "hilo", lambda: object())
        for csv in (primera, segunda):
            menu = ventana._acciones_del_historial(csv, csv.parent.parent.name)
            assert not menu.actions()[0].isEnabled()
            assert not menu.actions()[-1].isEnabled()


@pytest.mark.parametrize("cambiar_fecha", [False, True])
def test_retomar_varias_respeta_la_fecha_guardada_en_sus_batches(app, tmp_path, monkeypatch, cambiar_fecha):
    from app.airvault.config import AirVaultConfig
    from app.airvault.flujo import preparar_partes
    from tests import test_gui_airvault_cadena as cadena

    monkeypatch.setattr(cadena, "NOMBRE", "BITS 17 AUG 2026 05 50")
    primera = cadena.corrida(tmp_path)
    monkeypatch.setattr(cadena, "NOMBRE", "BITS 18 AUG 2026 05 42")
    segunda = cadena.corrida(tmp_path)
    preparar_partes(
        AirVaultConfig(), tmp_path / carpeta_de_trabajo(carpeta_de_corrida(primera).name),
        primera, fin_de_mes=True,
    )
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    ventana.fijar_corrida(segunda)
    ventana.lote_edit.setText("DP | ENTREGA PERSONALIZADA")
    if cambiar_fecha:
        ventana.fecha_combo.setCurrentIndex(0)
    seleccionar(ventana, primera, segunda)
    lanzadas = []
    monkeypatch.setattr(ventana, "_lanzar", lambda modo, estado: lanzadas.extend(estado["ejecuciones_subida"]))
    ventana._subir_a_mano()
    assert {e["csv"]: e["fin_de_mes"] for e in lanzadas} == {str(primera): True, str(segunda): cambiar_fecha}
    assert next(e for e in lanzadas if e["csv"] == str(segunda))["nombre_lote"] == "DP | ENTREGA PERSONALIZADA"


def test_la_subida_valida_todas_las_entregas_antes_de_arrancar(app, tmp_path, monkeypatch):
    primera = corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    segunda = corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    seleccionar(ventana, primera, segunda)
    primera.with_name(f"{primera.stem}_paginas.json").unlink()
    lanzadas = []
    monkeypatch.setattr(ventana, "_lanzar", lambda *args: lanzadas.append(args))
    ventana._subir_a_mano()
    assert lanzadas == []
    assert ventana.resumen.text()


@pytest.mark.parametrize("indexar", [False, True], ids=["solo-subida", "hasta-completar"])
def test_dos_ejecuciones_se_suben_con_sus_fechas_y_no_se_reenvian(app, tmp_path, monkeypatch, indexar):
    from tests import test_gui_airvault_cadena as cadena

    csvs = []
    for numero, nombre in enumerate(("BITS 17 AUG 2026 05 50", "BITS 18 AUG 2026 05 42")):
        monkeypatch.setattr(cadena, "NOMBRE", nombre)
        csv = cadena.corrida(tmp_path)
        if numero == 0:
            csv.with_suffix(".json").write_text(json.dumps({"dia_leido": False}), encoding="utf-8")
            csv.write_text(
                csv.read_text(encoding="utf-8").replace("231223", "231233").replace("2312240", "2312340"),
                encoding="utf-8",
            )
        csvs.append(csv)
    # Hay otra entrega atrasada que no fue seleccionada.
    otra = corrida(tmp_path, "BITS 16 AUG 2026 05 50")
    registrar_en_airvault(tmp_path, otra)
    cliente = AirVaultSimulado()
    monkeypatch.setattr(Trabajo, "subir", subida_simulada(cliente, vueltas=0))
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    ventana.fijar_corrida(csvs[-1])
    seleccionar(ventana, *csvs)
    recibidas = []

    def lanzar(modo, estado):
        assert modo == "subir"
        assert estado["recuperar_pendientes"] is False
        worker = TrabajoAirVaultWorker(modo, estado)
        worker._conectar = lambda: cliente
        worker._dormir = cliente.avanzar
        estado.update(sesion=SesionFalsa(), cliente=cliente, indexar_al_encontrar=indexar, completar=indexar)
        worker.subido.connect(recibidas.append)
        worker._subir()

    monkeypatch.setattr(ventana, "_lanzar", lanzar)
    ventana._subir_a_mano()
    trabajos = recibidas[-1]["trabajos"]
    assert len(trabajos) == 6
    assert {t.manifiesto.csv_origen for t in trabajos} == {str(csv) for csv in csvs}
    for csv in csvs:
        propios = cargar_partes(ventana._config_actual(), tmp_path / carpeta_de_trabajo(carpeta_de_corrida(csv).name), csv)
        assert len(propios) == 3
        assert all(t.manifiesto.fin_de_mes is (csv == csvs[0]) for t in propios)
        assert all(t.manifiesto.nombre_batch.startswith(f"DP | {csv.parent.parent.name}") for t in propios)
        assert all(t.manifiesto.etapa_hecha("subir") for t in propios)
        if indexar:
            assert all(t.manifiesto.etapa_hecha("verificar") for t in propios)
            assert all(t.manifiesto.etapa_hecha("completar") for t in propios)
    assert len([e for e in cliente.eventos if e[0] == "subir"]) == 6
    ventana._subir_a_mano()
    assert len([e for e in cliente.eventos if e[0] == "subir"]) == 6


@pytest.mark.parametrize("ancho,alto", [(720, 768), (1280, 720)])
def test_la_cola_muestra_al_menos_cuatro_batches_completos(app, tmp_path, monkeypatch, ancho, alto):
    from tests.test_gui_airvault_window import parte
    from app.airvault.flujo import SIN_SUBIR

    disponible = lambda _w=None: QRect(0, 0, ancho, alto)
    monkeypatch.setattr(airvault_window, "available_area", disponible)
    monkeypatch.setattr(responsive, "available_area", disponible)
    ventana = AirVaultWindow(tmp_path)
    ventana._estados = [parte(SIN_SUBIR, f"DP | BITS -{i}") for i in range(10)]
    ventana._pintar_lotes()
    ventana.show()
    app.processEvents()
    assert ventana.lotes.viewport().height() >= ventana.lotes.rowHeight(0) * 4
    assert ventana.width() <= ancho and ventana.height() <= alto
    cerrar = ventana.boton_cerrar
    posicion = cerrar.mapTo(ventana, QPoint())
    assert posicion.y() + cerrar.height() <= ventana.height()
