"""La seleccion del menu de discrepancias sobrevive a cerrar el programa.

Vive en el JSON de la plantilla, asi que reabrir la ventana tiene que
volver a dibujar las mismas casillas. Dos formas de perderla, las dos
cubiertas aqui: que abrir el menu reescriba el archivo con los valores por
defecto, y que guardar desde el editor de plantillas se lleve el ajuste por
delante al rehacer la plantilla desde cero.
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
    manager = TemplateManager()
    manager.save(
        manager.load(ruta).model_copy(update={"discrepancy_types": seleccion}),
        ruta,
    )


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
    assert TemplateManager().load(plantilla).discrepancy_types == SELECCION


def test_abrir_el_menu_no_reescribe_la_plantilla(app, plantilla):
    """Dibujar las casillas no puede disparar un guardado con los defectos."""
    _guardar(plantilla, SELECCION)
    ventana = _ventana(app, plantilla)
    antes = plantilla.stat().st_mtime_ns
    for _ in range(3):
        ventana._llenar_menu_discrepancias()
    assert plantilla.stat().st_mtime_ns == antes
    assert TemplateManager().load(plantilla).discrepancy_types == SELECCION


def test_encender_un_tipo_devuelve_todas_sus_casillas(app, plantilla):
    _guardar(plantilla, SELECCION)
    ventana = _ventana(app, plantilla)
    ventana._cambiar_tipo_discrepancia(TIPO_MANTENIMIENTO, True)
    guardado = TemplateManager().load(plantilla).discrepancy_types
    assert guardado[TIPO_MANTENIMIENTO] == list(CAMPOS_POR_TIPO[TIPO_MANTENIMIENTO])
    # Y sin tocar a los otros dos.
    assert guardado[TIPO_VUELO] == SELECCION[TIPO_VUELO]
    assert guardado[TIPO_CORRECCION] == SELECCION[TIPO_CORRECCION]


def test_una_casilla_suelta_se_guarda_sin_tocar_los_otros_tipos(app, plantilla):
    _guardar(plantilla, SELECCION)
    ventana = _ventana(app, plantilla)
    ventana._cambiar_discrepancia(TIPO_VUELO, "pilot_signature", True)
    guardado = TemplateManager().load(plantilla).discrepancy_types
    assert set(guardado[TIPO_VUELO]) == {"pilot_signature", "captain_license"}
    assert guardado[TIPO_MANTENIMIENTO] == []
    assert guardado[TIPO_CORRECCION] == SELECCION[TIPO_CORRECCION]


def test_el_editor_no_borra_la_seleccion_al_guardar(app, plantilla):
    """El editor solo edita geometria: el ajuste tiene que sobrevivir."""
    from app.gui.editor_window import EditorWindow

    _guardar(plantilla, SELECCION)
    editor = EditorWindow()
    editor._image_size = (1000, 700)
    editor._apply_template_to_scene(TemplateManager().load(plantilla))
    rehecha = editor._collect_template()
    assert rehecha.discrepancy_types == SELECCION


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
