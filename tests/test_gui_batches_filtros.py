"""Seleccion, acciones y filtros compartidos de los batches."""

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QTimer, Qt
from PySide6.QtTest import QTest

from app.airvault.flujo import (COMPLETADO, INDEXADO, SIN_SUBIR, BatchPrevisto,
                               EstadoParte)
from app.gui.airvault_previa import VistaPreviaBatches
from app.gui.airvault_window import AirVaultWindow
from test_airvault_rework import trabajo


def cargada(app, tmp_path):
    trabajos = [trabajo(tmp_path, nombre) for nombre in ("pendiente", "indexado", "completado")]
    ventana = AirVaultWindow(tmp_path)
    ventana._estados = [EstadoParte(t, estado) for t, estado in
                        zip(trabajos, (SIN_SUBIR, INDEXADO, COMPLETADO))]
    ventana._trabajos = trabajos
    ventana._pintar_lotes()
    ventana.show()
    app.processEvents()
    return ventana


def visibles(previ):
    return [previ.tabla.item(fila, 0).text() for fila in range(previ.tabla.rowCount())
            if not previ.tabla.isRowHidden(fila)]


def acciones(menu):
    return {accion.text(): accion for accion in menu.actions() if not accion.isSeparator()}


def test_click_derecho_abre_acciones_y_conserva_seleccion_multiple(app, tmp_path, monkeypatch):
    ventana = cargada(app, tmp_path)
    lista = ventana.lotes
    lista.selectRow(0)
    lista.selectionModel().select(lista.model().index(1, 0), QItemSelectionModel.SelectionFlag.Select)
    menus = []
    construir = ventana._acciones_de_la_cola

    def construir_y_cerrar(partes):
        menu = construir(partes)
        menus.append(menu)
        QTimer.singleShot(0, menu.close)
        return menu

    monkeypatch.setattr(ventana, "_acciones_de_la_cola", construir_y_cerrar)
    punto = lista.visualItemRect(lista.item(1)).center()
    QTest.mouseClick(lista.viewport(), Qt.MouseButton.RightButton, pos=punto)
    ventana._menu_de_la_cola(punto)
    assert len(menus) == 1
    assert len(ventana._seleccionadas()) == 2
    assert "Revisar en AirVault (2)" in acciones(menus[0])
    assert "Eliminar el batch… (2)" in acciones(menus[0])
    assert lista.rowAt(punto.y()) == 1
    punto = lista.visualItemRect(lista.item(2)).center()
    ventana._menu_de_la_cola(punto)
    assert [p.nombre for p in ventana._seleccionadas()] == ["completado"]
    assert "Ver las bitácoras del batch" in acciones(menus[1])


def test_previa_abre_todos_sin_elegir_corrida_y_las_marcas_se_sincronizan(app, tmp_path):
    ventana = cargada(app, tmp_path)
    ventana.ocultar_indexados_check.setChecked(True)
    ventana.ocultar_completados_check.setChecked(True)
    assert ventana.boton_previa.isEnabled()
    ventana._vista_previa()
    previ = ventana._ventanas_de_consulta[-1]
    assert visibles(previ) == ["pendiente", "indexado", "completado"]
    assert not previ.segun_marcas_accion.isChecked()
    assert previ.acciones_filtro[1].isChecked()
    assert previ.acciones_filtro[2].isChecked()
    previ.segun_marcas_accion.setChecked(True)
    assert visibles(previ) == ["pendiente"]
    previ.orden.cycle_column(0)
    assert visibles(previ) == ["pendiente"]
    assert previ.intro.text().startswith("1 batch con 10 bitácoras")
    previ.acciones_filtro[1].setChecked(False)
    assert not ventana.ocultar_indexados_check.isChecked()
    assert visibles(previ) == ["pendiente", "indexado"]
    exterior = acciones(ventana.batch_actions_button.menu())
    assert not exterior["Ocultar batches indexados"].isChecked()
    exterior["Ocultar batches completados"].setChecked(False)
    assert not ventana.ocultar_completados_check.isChecked()
    assert not previ.acciones_filtro[2].isChecked()
    assert visibles(previ) == ["pendiente", "indexado", "completado"]
    ventana.ocultar_completados_check.setChecked(True)
    assert previ.acciones_filtro[2].isChecked()
    assert visibles(previ) == ["pendiente", "indexado"]
    previ.segun_marcas_accion.setChecked(False)
    assert visibles(previ) == ["pendiente", "indexado", "completado"]


def test_previa_filtra_corridas_y_busca_solo_en_las_filas_visibles(app, tmp_path):
    ventana = cargada(app, tmp_path)
    ventana._corrida = ventana._trabajos[0].manifiesto.csv_origen
    ventana.solo_ejecucion_check.setChecked(True)
    ventana._vista_previa()
    previ = ventana._ventanas_de_consulta[-1]
    assert len(visibles(previ)) == 3
    previ.tabla.sortItems(0, Qt.SortOrder.DescendingOrder)
    previ.segun_marcas_accion.setChecked(True)
    assert visibles(previ) == ["pendiente"]
    previ.buscar_edit.setText("completado")
    previ._buscar()
    assert not previ._coincidencias
    ventana._corrida = ventana._trabajos[1].manifiesto.csv_origen
    ventana._pintar_lotes()
    assert visibles(previ) == ["indexado"]
    assert previ._elegido().nombre == "indexado"
    ventana.ocultar_indexados_check.setChecked(True)
    assert visibles(previ) == []
    assert previ._elegido() is None
    assert not previ.boton_bitacoras.isEnabled()


def test_dos_previas_comparten_marcas_y_cada_una_elige_su_vista(app, tmp_path):
    ventana = cargada(app, tmp_path)
    ventana._vista_previa()
    primera = ventana._ventanas_de_consulta[-1]
    ventana._vista_previa()
    segunda = ventana._ventanas_de_consulta[-1]
    primera.segun_marcas_accion.setChecked(True)
    segunda.acciones_filtro[2].setChecked(True)
    assert primera.acciones_filtro[2].isChecked()
    assert len(visibles(primera)) == 2
    assert len(visibles(segunda)) == 3
    segunda.close()
    app.processEvents()
    ventana.ocultar_indexados_check.setChecked(True)
    assert visibles(primera) == ["pendiente"]


def test_previa_no_duplica_locales_y_conserva_csv_de_cada_batch(app, tmp_path):
    ventana = cargada(app, tmp_path)
    locales = ventana._previstos_de_la_cola()
    repetido = BatchPrevisto("pendiente", 1, 1, False, Path("entrega.pdf"),
                            csv_origen=locales[0].csv_origen)
    nuevo = BatchPrevisto("pendiente", 1, 1, False, Path("otra.pdf"),
                         csv_origen=str(tmp_path / "otra" / "reporte.csv"))
    previstos = ventana._previstos_de_la_cola([locales[1], repetido, nuevo])
    assert len(previstos) == 4
    assert previstos[-1] is nuevo
    previ = VistaPreviaBatches(previstos, csv="equivocado.csv")
    previ.tabla.selectRow(1)
    previ._abrir_bitacoras()
    assert previ._abiertas[-1]._csv == Path(locales[1].csv_origen)


def test_tarjeta_reserva_alto_para_el_nombre_y_estado_en_sidebar_estrecho(app, tmp_path):
    ventana = cargada(app, tmp_path)
    lista = ventana.lotes
    tarjeta = lista.item(0)
    tarjeta.setText("DP | BITS 10 OCT 2026 08 30, una entrega de nombre largo\n80 páginas, ID 23567\nListo para indexar")
    lista.doItemsLayout()
    alto_estrecho = lista.sizeHintForRow(0)
    assert alto_estrecho >= lista.fontMetrics().height() * 4
    ventana.resize(1280, 720)
    ventana.divisor_batches.setSizes([550, 800])
    app.processEvents()
    assert lista.sizeHintForRow(0) < alto_estrecho


def test_previa_refleja_altas_bajas_y_estados_sin_perder_el_orden(app, tmp_path):
    ventana = cargada(app, tmp_path)
    ventana._vista_previa()
    previ = ventana._ventanas_de_consulta[-1]
    previ.orden.cycle_column(0)
    previ.segun_marcas_accion.setChecked(True)
    ventana.ocultar_completados_check.setChecked(True)
    nuevo = trabajo(tmp_path, "nuevo")
    ventana._estados = ventana._estados[1:] + [EstadoParte(nuevo, SIN_SUBIR)]
    ventana._pintar_lotes()
    assert visibles(previ) == ["nuevo", "indexado"]
    assert previ.orden.sorted_column == 0 and previ.orden.descending
    assert previ._elegido() is not None
    ventana._estados[0] = EstadoParte(ventana._estados[0].trabajo, COMPLETADO)
    ventana._pintar_lotes()
    assert visibles(previ) == ["nuevo"]
    previ.segun_marcas_accion.setChecked(False)
    assert visibles(previ) == ["nuevo", "indexado", "completado"]
    assert previ.tabla.item(1, 3).text() == "Completado"
