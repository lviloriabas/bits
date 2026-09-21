"""La seleccion del menu de discrepancias sobrevive a cerrar el programa.

Vive en el archivo local de la interfaz y no en el JSON de la plantilla, que
si se versiona: marcar una casilla dejaba el repositorio con una
modificacion pendiente y el pull de la otra maquina chocaba contra ella. De
ahi que aqui se compruebe tanto que la seleccion vuelve como que el archivo
de la plantilla no se toca.

Tres formas de perderla, las tres cubiertas: que abrir el menu reescriba lo
guardado con los valores por defecto, que guardar desde el editor de
plantillas se lleve el ajuste por delante al rehacer la plantilla desde
cero, y que lo elegido no llegue a lo que se procesa.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.templates.manager import TemplateManager
from app.validation.discrepancias import (
    CAMPOS_POR_TIPO,
    NOMBRE_CAMPO,
    NOMBRE_TIPO,
    TIPO_CORRECCION,
    TIPO_MANTENIMIENTO,
    TIPO_VUELO,
    con_discrepancias_elegidas,
    discrepancias_elegidas,
    guardar_discrepancias_elegidas,
)

ROOT = Path(__file__).resolve().parents[1]
ORIGEN = ROOT / "template" / "aircraft_log.json"

SELECCION = {
    TIPO_VUELO: ["captain_license"],
    TIPO_MANTENIMIENTO: [],
    TIPO_CORRECCION: ["technician_license"],
}


@pytest.fixture
def plantilla(tmp_path: Path) -> Path:
    """Copia de la plantilla de produccion, para no tocar el repositorio."""
    destino = tmp_path / "aircraft_log.json"
    shutil.copy(ORIGEN, destino)
    return destino


def _guardar(ruta: Path, seleccion: dict) -> None:
    guardar_discrepancias_elegidas(TemplateManager().load(ruta), seleccion)


def _elegidas(ruta: Path) -> dict:
    return discrepancias_elegidas(TemplateManager().load(ruta))


def _ventana(app, ruta: Path):
    from app.gui.main_window import MainWindow

    ventana = MainWindow()
    ventana.template_combo.addItem(ruta.stem, str(ruta))
    ventana.template_combo.setCurrentIndex(ventana.template_combo.count() - 1)
    return ventana


def _marcas(menu) -> dict:
    """Lo que el menu muestra: por tipo, si esta encendido y sus casillas.

    La fila del tipo es a la vez la casilla y la que abre el submenu, asi
    que cada accion del menu principal lleva las dos cosas.
    """
    marcas = {}
    for accion in menu.actions():
        if accion.isSeparator() or accion.menu() is None:
            continue
        marcas[accion.text()] = (
            accion.isChecked(),
            [sub.text() for sub in accion.menu().actions() if sub.isChecked()],
        )
    return marcas


def test_la_seleccion_se_relee_al_reabrir(app, plantilla):
    _guardar(plantilla, SELECCION)
    ventana = _ventana(app, plantilla)
    ventana._llenar_menu_discrepancias()
    esperado = _marcas(ventana.discrepancias_menu)

    # Cerrar y volver a abrir: ventana nueva, lectura desde el disco.
    otra = _ventana(app, plantilla)
    otra._llenar_menu_discrepancias()
    assert _marcas(otra.discrepancias_menu) == esperado
    assert _elegidas(plantilla) == SELECCION


def test_elegir_no_toca_el_archivo_de_la_plantilla(app, plantilla):
    """Lo que se versiona se queda como estaba; si no, el pull choca."""
    antes = plantilla.read_bytes()
    ventana = _ventana(app, plantilla)
    ventana._cambiar_tipo_discrepancia(TIPO_MANTENIMIENTO, False)
    ventana._cambiar_discrepancia(TIPO_VUELO, "pilot_signature", False)
    assert plantilla.read_bytes() == antes
    assert _elegidas(plantilla)[TIPO_MANTENIMIENTO] == []


def test_abrir_el_menu_no_reescribe_lo_guardado(app, plantilla):
    """Dibujar las casillas no puede disparar un guardado con los defectos."""
    _guardar(plantilla, SELECCION)
    ventana = _ventana(app, plantilla)
    for _ in range(3):
        ventana._llenar_menu_discrepancias()
    assert _elegidas(plantilla) == SELECCION


def test_encender_un_tipo_devuelve_todas_sus_casillas(app, plantilla):
    _guardar(plantilla, SELECCION)
    ventana = _ventana(app, plantilla)
    ventana._cambiar_tipo_discrepancia(TIPO_MANTENIMIENTO, True)
    guardado = _elegidas(plantilla)
    assert guardado[TIPO_MANTENIMIENTO] == list(CAMPOS_POR_TIPO[TIPO_MANTENIMIENTO])
    # Y sin tocar a los otros dos.
    assert guardado[TIPO_VUELO] == SELECCION[TIPO_VUELO]
    assert guardado[TIPO_CORRECCION] == SELECCION[TIPO_CORRECCION]


def test_una_casilla_suelta_se_guarda_sin_tocar_los_otros_tipos(app, plantilla):
    _guardar(plantilla, SELECCION)
    ventana = _ventana(app, plantilla)
    ventana._cambiar_discrepancia(TIPO_VUELO, "pilot_signature", True)
    guardado = _elegidas(plantilla)
    assert set(guardado[TIPO_VUELO]) == {"pilot_signature", "captain_license"}
    assert guardado[TIPO_MANTENIMIENTO] == []
    assert guardado[TIPO_CORRECCION] == SELECCION[TIPO_CORRECCION]


def test_lo_elegido_llega_a_la_plantilla_que_se_procesa(plantilla):
    """Los hilos y la linea de comandos leen esto, no el archivo a secas."""
    _guardar(plantilla, SELECCION)
    puesta = con_discrepancias_elegidas(TemplateManager().load(plantilla))
    assert puesta.discrepancy_types == SELECCION


def test_sin_nada_elegido_manda_lo_que_trae_la_plantilla(plantilla):
    """El archivo versionado sigue siendo el valor de partida."""
    de_fabrica = TemplateManager().load(plantilla).discrepancy_types
    assert _elegidas(plantilla) == {
        tipo: list(de_fabrica[tipo]) for tipo in CAMPOS_POR_TIPO
    }


def test_el_editor_no_borra_la_seleccion_al_guardar(app, plantilla):
    """El editor solo edita geometria: el ajuste tiene que sobrevivir."""
    from app.gui.editor_window import EditorWindow

    editor = EditorWindow()
    editor._image_size = (1000, 700)
    base = TemplateManager().load(plantilla)
    editor._apply_template_to_scene(base)
    rehecha = editor._collect_template()
    assert rehecha.discrepancy_types == base.discrepancy_types


def test_la_fila_del_tipo_es_la_casilla_y_abre_el_submenu(app, plantilla):
    """Una sola fila por tipo: marca a la izquierda y flecha a la derecha."""
    ventana = _ventana(app, plantilla)
    ventana._llenar_menu_discrepancias()
    filas = [a for a in ventana.discrepancias_menu.actions() if not a.isSeparator()]
    assert len(filas) == len(CAMPOS_POR_TIPO)
    for accion in filas:
        assert accion.isCheckable(), accion.text()
        assert accion.menu() is not None, accion.text()
        # El submenu lleva solo las casillas del tipo, sin filas de relleno.
        assert [s.text() for s in accion.menu().actions()] == [
            NOMBRE_CAMPO[c] for c in CAMPOS_POR_TIPO[
                next(t for t, n in NOMBRE_TIPO.items() if n == accion.text())
            ]
        ]


def test_marcar_una_casilla_actualiza_la_fila_del_tipo(app, plantilla):
    """Con el menu abierto, las dos filas que dicen lo mismo van de acuerdo."""
    _guardar(plantilla, {TIPO_VUELO: [], TIPO_MANTENIMIENTO: [],
                         TIPO_CORRECCION: []})
    ventana = _ventana(app, plantilla)
    ventana._llenar_menu_discrepancias()
    fila = ventana._acciones_tipo[TIPO_VUELO]
    assert not fila.isChecked()
    ventana._acciones_campo[(TIPO_VUELO, "pilot_signature")].setChecked(True)
    assert fila.isChecked()
    ventana._acciones_campo[(TIPO_VUELO, "pilot_signature")].setChecked(False)
    assert not fila.isChecked()
