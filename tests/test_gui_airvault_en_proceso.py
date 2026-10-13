"""El boton del proceso, el registro legible y el arco del batch en proceso."""

from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from app.gui.airvault_window import AirVaultWindow, color_indexando
from app.gui.batches_sidebar import ROL_EN_PROCESO
from app.gui.tokens import SPACE_S, paleta
from app.gui.widgets import app_chrome_qss
from tests.test_gui_batches_filtros import cargada


def _regla(hoja: str, selector: str) -> str:
    return hoja.split(selector + " {", 1)[1].split("}", 1)[0]


def _cerca(color: QColor, otro: str, margen: int = 12) -> bool:
    otro = QColor(otro)
    return all(abs(a - b) <= margen for a, b in (
        (color.red(), otro.red()), (color.green(), otro.green()), (color.blue(), otro.blue()),
    ))


def test_el_boton_dice_empezar_o_continuar_y_mide_lo_que_su_rotulo(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    boton = ventana.boton_proceso
    assert boton.text() == "Empezar"
    ancho = boton.width()
    medida = boton.fontMetrics()
    # El ancho sale del rótulo más largo, no de los 160 px del texto de antes.
    assert ancho == medida.horizontalAdvance("Continuar") + 32
    ventana._pausado = True
    ventana._actualizar_botones_proceso()
    assert boton.text() == "Continuar"
    assert boton.width() == ancho
    ventana.close()


def test_el_boton_de_acento_desactivado_se_apaga_como_los_demas():
    """Sin regla propia seguía con el acento y su texto aunque no se pudiera pulsar."""
    QApplication.instance() or QApplication([])
    c = paleta()
    hoja = app_chrome_qss()
    regla = _regla(hoja, "#primaryButton:disabled")
    assert f"color: {c.TEXT_DISABLED};" in regla
    assert f"background-color: {c.CONTROL_DISABLED};" in regla
    # Va después de la regla de reposo: con el mismo peso gana la última.
    assert hoja.index("#primaryButton:disabled {") > hoja.index("#primaryButton {")


def test_la_fila_elegida_del_registro_se_lee_sobre_su_fondo(app, tmp_path):
    """El texto de la fila elegida salía negro sobre el gris de la selección."""
    ventana = AirVaultWindow(tmp_path)
    ventana.resize(900, 700)
    ventana.show()
    ventana._anotar("Subida terminada")
    ventana._anotar("Revisado:", ["Batch uno: Listo para indexar"])
    app.processEvents()
    c = paleta()
    hoja = ventana.styleSheet()
    assert c.PANE_CONTROL_HOVER in _regla(hoja, "QListWidget#airvaultBitacora::item:hover:!selected")
    elegida = _regla(hoja, "QListWidget#airvaultBitacora::item:selected")
    assert c.STROKE_STRONG in elegida and c.PANE_TEXT in elegida

    fila = ventana.bitacora.item(0)
    fila.setSelected(True)
    app.processEvents()
    caja = ventana.bitacora.visualItemRect(fila)
    imagen = ventana.bitacora.viewport().grab(caja).toImage()
    colores = [
        imagen.pixelColor(x, y)
        for x in range(imagen.width()) for y in range(imagen.height())
    ]
    # El fondo es el gris del tema y encima hay texto del color del panel.
    assert sum(_cerca(color, c.STROKE_STRONG) for color in colores) > len(colores) // 2
    assert any(_cerca(color, c.PANE_TEXT) for color in colores)
    ventana.close()


def test_el_batch_en_proceso_lleva_el_arco_en_su_tarjeta(app, tmp_path, monkeypatch):
    ventana = cargada(app, tmp_path)
    claves = [ventana.lotes.item(fila).data(Qt.ItemDataRole.UserRole) for fila in range(3)]
    quieta = ventana.lotes.viewport().grab().toImage()

    def giros():
        return [ventana.lotes.item(fila).data(ROL_EN_PROCESO) for fila in range(3)]

    assert giros() == [None, None, None]
    # Sin hilo nada gira, aunque quede anotado el paso de antes.
    ventana._clave_del_paso = claves[0]
    ventana._marcar_en_proceso()
    assert giros() == [None, None, None]

    monkeypatch.setattr(ventana, "hilo", lambda: SimpleNamespace(modo="subir"))
    ventana._indexando.add(claves[1])
    ventana._marcar_en_proceso()
    assert giros() == [color_indexando(), color_indexando(), None]
    # Rehacer la tabla no apaga el arco de lo que sigue en proceso.
    ventana._pintar_lotes()
    assert giros() == [color_indexando(), color_indexando(), None]
    app.processEvents()

    # El arco se pinta bajo el número, en la columna que ya estaba reservada.
    girando = ventana.lotes.viewport().grab().toImage()
    caja = ventana.lotes.visualItemRect(ventana.lotes.item(0))
    medida = ventana.lotes.fontMetrics()
    canal = medida.horizontalAdvance("000") + SPACE_S
    cambiados = [
        (x, y)
        for x in range(caja.left(), caja.right() + 1)
        for y in range(caja.top(), caja.bottom() + 1)
        if girando.pixelColor(x, y) != quieta.pixelColor(x, y)
    ]
    assert cambiados
    assert all(x <= caja.left() + SPACE_S + 1 + canal for x, _y in cambiados)
    assert all(y >= caja.top() + SPACE_S + medida.lineSpacing() for _x, y in cambiados)

    monkeypatch.setattr(ventana, "hilo", lambda: None)
    ventana._marcar_en_proceso()
    assert giros() == [None, None, None]
    ventana.close()


def test_los_batches_que_se_leen_en_web_search_tambien_giran(app, tmp_path, monkeypatch):
    ventana = cargada(app, tmp_path)
    clave = ventana.lotes.item(2).data(Qt.ItemDataRole.UserRole)
    ventana._filas_websearch_activas.add(clave)
    ventana._marcar_en_proceso()
    assert ventana.lotes.item(2).data(ROL_EN_PROCESO) is None
    monkeypatch.setattr(ventana, "lectura_websearch", lambda: SimpleNamespace(estado={}))
    ventana._marcar_en_proceso()
    assert ventana.lotes.item(2).data(ROL_EN_PROCESO) == color_indexando()
    ventana._filas_websearch_activas.clear()
    ventana.close()


# ── lo que entra al pulsar «Empezar» ────────────────────────────────

def test_empezar_se_enciende_con_lo_que_la_cola_tiene_sin_terminar(app, tmp_path):
    """Sin elegir ejecucion, la cola ya dice que batches faltan."""
    ventana = cargada(app, tmp_path)
    assert not ventana._corrida and not ventana._listo_para_subir
    assert [parte.nombre for parte in ventana._pendientes_de_la_cola()] == ["pendiente"]
    assert ventana.boton_proceso.isEnabled()

    # Con «Completar batch», el indexado tambien tiene un paso por dar.
    ventana.completar_check.setChecked(True)
    assert [p.nombre for p in ventana._pendientes_de_la_cola()] == ["pendiente", "indexado"]

    # Lo omitido no entra en ningun proceso y tampoco enciende el boton.
    ventana.completar_check.setChecked(False)
    pendiente = ventana._estados[0]
    pendiente.trabajo.manifiesto.cancelado = True
    from app.airvault.flujo import CANCELADO, EstadoParte
    ventana._estados[0] = EstadoParte(pendiente.trabajo, CANCELADO)
    ventana._pintar_lotes()
    assert ventana._pendientes_de_la_cola() == []
    assert not ventana.boton_proceso.isEnabled()
    ventana.close()


def test_sin_ejecucion_elegida_empezar_retoma_la_cola(app, tmp_path, monkeypatch):
    """No hay entrega que preparar: se parte de los manifiestos."""
    ventana = cargada(app, tmp_path)
    pasos = []
    monkeypatch.setattr(ventana, "_continuar_pendiente", lambda: pasos.append("cola"))
    monkeypatch.setattr(ventana, "_subir", lambda *a, **k: pasos.append("subir"))

    ventana.boton_proceso.click()

    assert pasos == ["cola"]
    ventana.close()


def test_revisar_la_cola_no_exige_una_ejecucion(app, tmp_path, monkeypatch):
    ventana = cargada(app, tmp_path)
    lanzados = []
    monkeypatch.setattr(ventana, "_lanzar", lambda modo, estado: lanzados.append((modo, dict(estado))))

    ventana._comprobar()

    (modo, estado), = lanzados
    assert modo == "comprobar"
    assert estado["trabajos"] == ventana._trabajos
    assert "csv" not in estado and estado["raiz"] == tmp_path

    # Preparar una entrega nueva si la necesita, y lo dice.
    lanzados.clear()
    ventana._subir()
    assert lanzados == []
    assert "Falta elegir la ejecución" in ventana.resumen.text()
    ventana.close()
