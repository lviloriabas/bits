"""La interfaz cabe en la pantalla del equipo, sea cual sea.

La carpeta se copia a portátiles de 1366x768 y a monitores 1080p con el
escalado de Windows al 125% o al 150%, que dejan un escritorio lógico de
1536x864 o de 1280x720. La ventana principal pedía 1280x800 y su mínimo real
era 1447x886: en todas esas pantallas menos en un 1080p sin escalar se abría
más grande que el escritorio y la franja de abajo (la consola y el panel de
avance) quedaba fuera, sin forma de alcanzarla.

Estas pruebas fijan las dos condiciones que lo arreglan: que ninguna ventana
se abra más grande que el área de trabajo, y que con ese tamaño no le sobre
contenido, es decir que su tamaño mínimo quepa dentro. Se comprueban con la
tipografía real del sistema, porque con la que pone la plataforma «offscreen»
las medidas de texto no se parecen a las de Windows y la prueba no diría nada.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

import app.gui.responsive as responsive
from app.gui.automatizacion import PREPROCESAR
from app.gui.csv_viewer import CsvViewerWindow
from app.gui.editor_window import EditorWindow
from app.gui.field_selector import ImportantFieldsDialog
from app.gui.fleet_editor import FleetEditorDialog, FleetStore
from app.gui.main_window import _PREFERRED_HEIGHT, _PREFERRED_WIDTH, MainWindow
from app.gui.responsive import (
    COMPACT,
    FRAME_ALLOWANCE,
    ROOMY,
    density_for,
    fitted_geometry,
)
from app.gui.tokens import CONTROL_HEIGHT, CONTROL_HEIGHT_COMPACT
from app.gui.widgets import (
    SPLIT_MENU_WIDTH,
    SPLIT_PAD_LEFT,
    SPLIT_PAD_RIGHT,
    ElidedLabel,
    ZoomOverlay,
    ZoomableScrollArea,
    hide_overlay_when_tight,
    keep_overlay_clear_of_scrollbars,
    size_columns_once,
)

# Pantallas reales, con el escalado ya aplicado: lo que Qt ve como escritorio
# es el tamaño físico dividido por el factor de Windows.
PANTALLAS = [
    ("1024x768", 1024, 768),
    ("1280x720 (1080p al 150%)", 1280, 720),
    ("1366x768", 1366, 768),
    ("1440x900", 1440, 900),
    ("1536x864 (1080p al 125%)", 1536, 864),
    ("1600x900", 1600, 900),
    ("1920x1080", 1920, 1080),
    ("2560x1440", 2560, 1440),
    ("3840x2160", 3840, 2160),
]

# Alto que se lleva la barra de tareas de Windows en su tamaño de siempre.
BARRA_DE_TAREAS = 40


def _app() -> QApplication:
    """Aplicación con la tipografía de Windows, no con la de «offscreen».

    Los anchos mínimos de la ventana salen de medir texto. La plataforma sin
    pantalla no resuelve ninguna familia real y devuelve medidas que no son
    las del equipo del usuario, así que se carga Segoe UI (la que declara la
    hoja de estilo) desde la carpeta de fuentes del sistema.
    """
    app = QApplication.instance() or QApplication([])
    if getattr(app, "_fuente_real", False):
        return app
    fuentes = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    familias: list[str] = []
    for archivo in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        identificador = QFontDatabase.addApplicationFont(str(fuentes / archivo))
        familias += QFontDatabase.applicationFontFamilies(identificador)
    if "Segoe UI" not in familias:
        pytest.skip("sin la tipografía del sistema las medidas no son las reales")
    app.setFont(QFont("Segoe UI", 10))
    app._fuente_real = True
    return app


def _area_de_trabajo(ancho: int, alto: int) -> QRect:
    """Escritorio útil de una pantalla, ya sin la barra de tareas."""
    return QRect(0, 0, ancho, alto - BARRA_DE_TAREAS)


def _cabe(ventana, area: QRect) -> bool:
    """Si la ventana entera, con su marco, está dentro del área de trabajo."""
    tamano = ventana.size()
    return (
        tamano.width() + FRAME_ALLOWANCE.width() <= area.width()
        and tamano.height() + FRAME_ALLOWANCE.height() <= area.height()
    )


def _sin_recortes(ventana) -> bool:
    """Si el contenido cabe en la ventana: nada empujado fuera del borde."""
    minimo = ventana.minimumSizeHint()
    return (
        minimo.width() <= ventana.width()
        and minimo.height() <= ventana.height()
    )


# ── El cálculo de tamaño, sin ventanas de por medio ──────────────────────

@pytest.mark.parametrize("nombre,ancho,alto", PANTALLAS)
def test_la_ventana_calculada_entra_en_el_escritorio(nombre, ancho, alto):
    area = _area_de_trabajo(ancho, alto)
    geometria = fitted_geometry(QSize(_PREFERRED_WIDTH, _PREFERRED_HEIGHT), area)
    assert geometria.width() + FRAME_ALLOWANCE.width() <= area.width(), nombre
    assert geometria.height() + FRAME_ALLOWANCE.height() <= area.height(), nombre
    assert area.contains(geometria), nombre


def test_no_agranda_a_quien_pide_poco():
    """Un diálogo pequeño se queda con su tamaño; el recorte es solo techo."""
    geometria = fitted_geometry(QSize(420, 520), _area_de_trabajo(1920, 1080))
    assert geometria.size() == QSize(420, 520)


def test_la_ventana_se_centra_en_el_area_disponible():
    area = QRect(0, 40, 1920, 1000)
    geometria = fitted_geometry(QSize(1280, 900), area)
    izquierda = geometria.x() - area.x()
    derecha = area.right() - geometria.right()
    assert abs(izquierda - derecha) <= FRAME_ALLOWANCE.width()
    assert geometria.y() >= area.y()


def test_un_escritorio_diminuto_no_deja_la_ventana_sin_contenido():
    """Con menos sitio del razonable se prefiere que asome a que se anule."""
    geometria = fitted_geometry(QSize(1280, 900), QRect(0, 0, 320, 240))
    assert geometria.width() >= responsive.MIN_WINDOW.width()
    assert geometria.height() >= responsive.MIN_WINDOW.height()


# ── Elección de medidas ──────────────────────────────────────────────────

def test_las_pantallas_bajas_usan_medidas_compactas():
    assert density_for(680) is COMPACT
    assert density_for(1001) is ROOMY


def test_la_densidad_no_parpadea_al_arrastrar_el_borde():
    """Volver a lo holgado exige recuperar más alto del que se perdió."""
    justo_encima = responsive.COMPACT_HEIGHT + 1
    assert density_for(justo_encima, COMPACT) is COMPACT
    assert density_for(justo_encima + responsive.DENSITY_HYSTERESIS, COMPACT) is ROOMY


def test_las_medidas_holgadas_no_tocan_nada():
    """Son las de la hoja base: la ventana grande se dibuja como siempre."""
    assert ROOMY.qss == ""
    assert COMPACT.qss != ""


# ── Las ventanas, abiertas en cada pantalla ──────────────────────────────

@pytest.mark.parametrize("nombre,ancho,alto", PANTALLAS)
def test_la_ventana_principal_cabe_entera(nombre, ancho, alto):
    _app()
    area = _area_de_trabajo(ancho, alto)
    with patch.object(responsive, "available_area", return_value=area):
        ventana = MainWindow()
        try:
            ventana.show()
            assert _cabe(ventana, area), f"{nombre}: se abre fuera del escritorio"
            assert _sin_recortes(ventana), f"{nombre}: le sobra contenido"
        finally:
            ventana.close()


@pytest.mark.parametrize("nombre,ancho,alto", PANTALLAS)
def test_las_demas_ventanas_caben_enteras(nombre, ancho, alto):
    _app()
    area = _area_de_trabajo(ancho, alto)
    carpeta = Path(tempfile.mkdtemp())
    with patch.object(responsive, "available_area", return_value=area):
        ventanas = [
            CsvViewerWindow(carpeta),
            EditorWindow(),
            ImportantFieldsDialog(["log_number", "fecha"], ["fecha"]),
            FleetEditorDialog(FleetStore(carpeta / "fleet.json")),
        ]
        try:
            for ventana in ventanas:
                ventana.show()
                etiqueta = f"{nombre}: {type(ventana).__name__}"
                assert _cabe(ventana, area), f"{etiqueta} se abre fuera"
                assert _sin_recortes(ventana), f"{etiqueta} le sobra contenido"
        finally:
            for ventana in ventanas:
                ventana.close()


def test_el_selector_de_campos_muestra_sus_acciones_en_espanol():
    _app()
    dialogo = ImportantFieldsDialog(["log_number", "fecha"], ["fecha"])
    try:
        assert dialogo.buttons.button(
            QDialogButtonBox.StandardButton.Apply
        ).text() == "Aplicar"
        assert dialogo.buttons.button(
            QDialogButtonBox.StandardButton.Close
        ).text() == "Cerrar"
    finally:
        dialogo.close()


def test_las_cabeceras_cortas_conservan_el_texto_completo():
    _app()
    tabla = QTableWidget(0, 3)
    tabla.setHorizontalHeaderLabels(["Batch", "Páginas", "Bitácoras"])
    size_columns_once(tabla)
    try:
        for columna, texto in enumerate(("Batch", "Páginas", "Bitácoras")):
            ancho_texto = tabla.horizontalHeader().fontMetrics().horizontalAdvance(
                texto
            )
            assert tabla.columnWidth(columna) >= ancho_texto + 18
    finally:
        tabla.close()


def test_en_pantalla_baja_los_cuadros_se_reparten_en_dos_columnas():
    """El alto que se ahorra es el que necesita la vista previa."""
    _app()
    area = _area_de_trabajo(1366, 768)
    with patch.object(responsive, "available_area", return_value=area):
        ventana = MainWindow()
        try:
            ventana.show()
            assert ventana._density is COMPACT
            assert ventana._controls_columns == 2
            apilados = sum(
                cuadro.minimumSizeHint().height()
                for cuadro in (
                    ventana._input_group,
                    ventana._options_group,
                )
            )
            reparto = ventana._controls_grid.parentWidget().minimumSizeHint()
            assert reparto.height() < apilados
        finally:
            ventana.close()


def test_en_pantalla_grande_aprovecha_el_ancho_en_dos_columnas():
    """Entrada y Salidas no gastan dos filas completas en un monitor ancho."""
    _app()
    area = _area_de_trabajo(1920, 1080)
    with patch.object(responsive, "available_area", return_value=area):
        ventana = MainWindow()
        try:
            ventana.show()
            assert ventana._density is ROOMY
            assert ventana._controls_columns == 2
        finally:
            ventana.close()


def test_la_ventana_se_readapta_al_cambiar_de_tamano():
    """Maximizar, restaurar o mover a otro monitor recalcula las medidas."""
    _app()
    grande = _area_de_trabajo(1920, 1080)
    with patch.object(responsive, "available_area", return_value=grande):
        ventana = MainWindow()
        try:
            ventana.show()
            assert ventana._density is ROOMY
            ventana.resize(1366, 680)
            assert ventana._density is COMPACT
            assert ventana._controls_columns == 2
            # Se vuelve a lo holgado cuando el alto llega a lo que ese
            # reparto pide de verdad, medido con la tipografía del equipo.
            holgado = ventana._roomy_minimum.height()
            ventana.resize(1400, holgado + responsive.DENSITY_HYSTERESIS)
            assert ventana._density is ROOMY
            assert ventana._controls_columns == 2
        finally:
            ventana.close()


def test_ningun_alto_deja_el_contenido_apretado_por_debajo_de_su_minimo():
    """El umbral de 820 px escrito a mano dejaba un hueco de cien píxeles.

    Entre ese número y lo que el reparto holgado pide de verdad, la ventana
    se quedaba con las medidas holgadas sin sitio para ellas: el layout
    encogía los cuadros por debajo de su mínimo y «Salidas» aparecía con las
    casillas montadas unas sobre otras y el botón de matrículas fuera de su
    marco. Con el umbral medido, a cualquier alto cabe lo que hay dentro.
    """
    app = _app()
    with patch.object(
        responsive, "available_area", return_value=_area_de_trabajo(2560, 1440)
    ):
        ventana = MainWindow()
        try:
            ventana.show()
            for alto in range(700, 1101, 20):
                ventana.resize(1366, alto)
                app.processEvents()
                layout = ventana.centralWidget().layout()
                layout.invalidate()
                layout.activate()
                assert layout.minimumSize().height() <= ventana.height(), (
                    f"a {alto} px de alto el contenido no cabe "
                    f"({ventana._density.name})"
                )
        finally:
            ventana.close()


def test_el_panel_de_avance_no_recorta_sus_rotulos_en_la_ventana_mas_pequena():
    """Los rótulos no se desplazan ni se recortan: o caben o no caben.

    El panel cedió veinte píxeles de su mínimo para hacerle sitio a la línea
    de pasos, y el reparto se los quitó a los rótulos: «Avance por archivo»
    salía partido por la mitad. Lo que cede ahora es la lista, que se
    desplaza.
    """
    app = _app()
    with patch.object(
        responsive, "available_area", return_value=_area_de_trabajo(1366, 768)
    ):
        ventana = MainWindow()
        try:
            ventana.show()
            ventana.resize(ventana.minimumWidth(), ventana.minimumHeight())
            app.processEvents()
            rotulos = [
                etiqueta
                for etiqueta in ventana.times_pane.findChildren(QLabel)
                if etiqueta.text() and etiqueta.isVisibleTo(ventana)
            ]
            assert any(
                etiqueta.text() == "Progreso por archivo" for etiqueta in rotulos
            )
            for etiqueta in rotulos:
                assert etiqueta.height() >= etiqueta.fontMetrics().height(), (
                    f"«{etiqueta.text()}» se recorta"
                )
        finally:
            ventana.close()


def test_los_controles_vecinos_comparten_alto_y_ancho():
    """La métrica visual es una sola, incluso entre tipos de control."""
    app = _app()
    area = _area_de_trabajo(1920, 1080)
    with patch.object(responsive, "available_area", return_value=area):
        ventana = MainWindow()
        try:
            ventana.resize(1920, 1000)
            ventana.show()
            app.processEvents()
            salidas = ventana.export_options
            controles = (
                ventana.input_edit,
                ventana.template_combo,
                ventana.input_actions_button,
                ventana.template_actions_button,
                salidas.output_mode_combo,
                salidas.csv_date_mode_combo,
                salidas.separation_button,
                ventana.view_button,
                salidas.partes_control,
                ventana.fleet_check,
                salidas.partes_check,
                ventana.progress,
                ventana.btn_process,
                ventana.btn_cancel,
                ventana.tema_toggle,
                ventana.more_actions_button,
                ventana.btn_automatico,
                ventana.search_edit,
                ventana.search_button,
                ventana.search_prev,
                ventana.search_next,
            )
            # Un solo alto para todo lo que se pulsa o se escribe, que es la
            # medida de WinUI. Antes la hoja declaraba siete distintos y en
            # pantalla salian cuatro, asi que las casillas no cuadraban con
            # sus vecinos de fila.
            assert {control.height() for control in controles} == {
                CONTROL_HEIGHT
            }
            assert ventana.tema_toggle.width() == CONTROL_HEIGHT
            assert ventana.time_summary.height() == CONTROL_HEIGHT
            divisores = [
                child
                for child in ventana.time_summary.children()
                if child.property("role") == "metricDivider"
            ]
            assert len(divisores) == 2
            assert all(divider.width() == 1 for divider in divisores)
            assert all(
                divider.height() == ventana.time_summary.height() - 2
                for divider in divisores
            )
            assert (
                ventana.input_actions_button.width()
                == ventana.template_actions_button.width()
            )
            assert (
                salidas.separation_button.width()
                == ventana.view_button.width()
            )
            assert ventana._input_group.height() == ventana._options_group.height()
            assert abs(
                ventana._input_group.width() - ventana._options_group.width()
            ) <= 1
            automatico_texto = (
                ventana.btn_automatico.fontMetrics().horizontalAdvance(
                    ventana.btn_automatico.text()
                )
            )
            # La flecha del botón dividido va en su propia celda pegada al
            # borde, fuera de lo que se pulsa, y Qt no la tiene en cuenta al
            # centrar el rótulo. El relleno de la derecha la compensa: si vale
            # el de la izquierda más la celda, el texto vuelve al centro de la
            # parte pulsable en lugar de quedarse contra el separador.
            assert SPLIT_PAD_RIGHT == SPLIT_PAD_LEFT + SPLIT_MENU_WIDTH
            assert ventana.btn_automatico.width() >= (
                automatico_texto + SPLIT_PAD_LEFT + SPLIT_PAD_RIGHT
            )
            progreso = ventana.progress.mapTo(ventana, QPoint())
            automatico = ventana.btn_automatico.mapTo(ventana, QPoint())
            tema = ventana.tema_toggle.mapTo(ventana, QPoint())
            assert progreso.x() == ventana._density.window_margin
            assert (
                automatico.x() + ventana.btn_automatico.width()
                == ventana.width() - ventana._density.window_margin
            )
            assert progreso.x() + ventana.progress.width() < automatico.x()
            assert (
                tema.x() + ventana.tema_toggle.width()
                == ventana.width() - ventana._density.window_margin
            )
            assert all(
                etiqueta.text() == "00:00:00"
                for etiqueta in ventana.time_labels.values()
            )
            ventana.resize(1366, 680)
            app.processEvents()
            assert {control.height() for control in controles} == {
                CONTROL_HEIGHT_COMPACT
            }
            assert ventana.tema_toggle.width() == CONTROL_HEIGHT_COMPACT
            assert ventana.time_summary.height() == CONTROL_HEIGHT_COMPACT
            # La compacta aprieta el relleno, pero la celda de la flecha mide
            # lo mismo: el botón sigue teniendo que reservarla.
            assert ventana.btn_automatico.width() >= (
                automatico_texto + SPLIT_MENU_WIDTH
            )
            assert abs(
                ventana._input_group.width() - ventana._options_group.width()
            ) <= 1
        finally:
            ventana.close()


def test_el_buscador_no_desalinea_el_visor_y_la_tabla():
    """La busqueda es una barra comun y los dos cuadros comparten limites."""
    app = _app()
    area = _area_de_trabajo(1920, 1080)
    with patch.object(responsive, "available_area", return_value=area):
        ventana = MainWindow()
        try:
            ventana.show()
            for _ in range(4):
                app.processEvents()

            visor = ventana.preview_scroll.mapTo(ventana, QPoint())
            tabla = ventana.table.mapTo(ventana, QPoint())
            buscador = ventana.search_edit.mapTo(ventana, QPoint())
            margenes = {
                ventana.cadena._etiquetas[PREPROCESAR].mapTo(
                    ventana, QPoint()
                ).x(),
                buscador.x(),
                ventana.preview_file_caption.mapTo(ventana, QPoint()).x(),
            }
            assert margenes == {ventana._density.window_margin}
            assert 280 <= ventana.search_edit.width() <= 420
            assert ventana.search_edit.placeholderText().startswith("Buscar ")
            assert ventana.search_next.x() + ventana.search_next.width() < (
                ventana.width() // 2
            )
            assert buscador.y() < visor.y()
            assert abs(visor.y() - tabla.y()) <= 1
            assert abs(
                visor.y() + ventana.preview_scroll.height()
                - tabla.y()
                - ventana.table.height()
            ) <= 1
        finally:
            ventana.close()
            app.processEvents()


def test_el_limite_de_paginas_muestra_valor_y_sufijo_completos():
    app = _app()
    area = _area_de_trabajo(1366, 768)
    with patch.object(responsive, "available_area", return_value=area):
        ventana = MainWindow()
        try:
            ventana.show()
            app.processEvents()
            campo = ventana.export_options.partes_spin
            assert campo.height() >= campo.fontMetrics().lineSpacing() + 8
        finally:
            ventana.close()


def test_el_recuadro_de_zoom_cabe_tumbado_hasta_en_la_ventana_mas_pequena():
    """Tumbado entra donde de pie no entraba, que es para lo que se tumbó.

    De pie medía más que el alto que la vista previa tiene garantizado en la
    ventana mínima, así que en ese tamaño desaparecía justo cuando hace más
    falta. Tumbado pide ancho, que es lo que sobra en un panel que enseña una
    hoja vertical, y se queda.
    """
    app = _app()
    with patch.object(
        responsive, "available_area", return_value=_area_de_trabajo(1920, 1080)
    ):
        ventana = MainWindow()
        try:
            ventana.show()
            app.processEvents()
            assert ventana._zoom_holder.isVisible()

            ventana.resize(ventana.minimumWidth(), ventana.minimumHeight())
            for _ in range(4):
                app.processEvents()

            assert ventana._zoom_holder.isVisible()
            pedido = ventana._zoom_holder.sizeHint()
            marco = ventana._zoom_holder.parentWidget()
            assert marco.width() >= pedido.width()
            assert marco.height() >= pedido.height()
        finally:
            ventana.close()


def test_el_recuadro_de_zoom_se_esconde_antes_que_dibujarse_a_medias():
    """Flota sobre la página: no manda sobre el mínimo, y o cabe o no está.

    Se comprueba sobre el marco a pelo porque en la ventana real ya no hay
    ningún tamaño legal en el que la píldora no quepa; la regla sigue viva
    para los paneles que puedan quedarse más estrechos que ella.
    """
    app = _app()
    marco = QWidget()
    marco.resize(600, 400)
    disposicion = QGridLayout(marco)
    disposicion.setContentsMargins(0, 0, 0, 0)

    soporte = QWidget(marco)
    soporte_layout = QVBoxLayout(soporte)
    soporte_layout.setContentsMargins(8, 8, 8, 8)
    soporte_layout.addWidget(
        ZoomOverlay(
            ("Acercar", "Acercar", lambda: None),
            ("Ajustar", "Ajustar", lambda: None),
            ("Alejar", "Alejar", lambda: None),
            marco,
        )
    )
    soporte.setSizePolicy(
        QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
    )
    disposicion.addWidget(soporte, 0, 0)
    hide_overlay_when_tight(soporte)
    marco.show()
    app.processEvents()
    try:
        assert soporte.isVisible()

        pedido = soporte.sizeHint()
        # Estrecho: es la medida que le queda corta a un control tumbado.
        marco.resize(pedido.width() - 20, 400)
        app.processEvents()
        assert not soporte.isVisible()

        # Bajo: y la otra tampoco puede recortarlo.
        marco.resize(600, pedido.height() - 5)
        app.processEvents()
        assert not soporte.isVisible()

        marco.resize(600, 400)
        app.processEvents()
        assert soporte.isVisible()
    finally:
        marco.close()


def test_el_recuadro_de_zoom_no_se_sienta_sobre_la_barra():
    """Va abajo y centrado, y abajo es donde aparece la barra horizontal.

    En cuanto la pagina se acerca lo suficiente para que haya que
    desplazarla, la barra sale justo debajo del recuadro y queda tapada:
    no se puede arrastrar. El recuadro se aparta lo que mide la barra.
    """
    app = _app()
    marco = QWidget()
    marco.resize(400, 300)
    disposicion = QGridLayout(marco)
    disposicion.setContentsMargins(0, 0, 0, 0)

    panel = ZoomableScrollArea()
    panel.setWidgetResizable(False)
    pagina = QLabel()
    pagina.setFixedSize(200, 200)
    panel.setWidget(pagina)
    disposicion.addWidget(panel, 0, 0)

    soporte = QWidget(marco)
    soporte_layout = QVBoxLayout(soporte)
    soporte_layout.setContentsMargins(8, 8, 8, 8)
    soporte_layout.addWidget(
        ZoomOverlay(
            ("Acercar", "Acercar", lambda: None),
            ("Ajustar", "Ajustar", lambda: None),
            ("Alejar", "Alejar", lambda: None),
            marco,
        )
    )
    soporte.setSizePolicy(
        QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
    )
    disposicion.addWidget(
        soporte, 0, 0,
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
    )
    keep_overlay_clear_of_scrollbars(soporte, panel)
    marco.show()
    app.processEvents()
    try:
        # Con la pagina entera a la vista no hay barras que esquivar.
        assert not panel.horizontalScrollBar().isVisible()
        assert soporte_layout.contentsMargins().bottom() == 8

        # La pagina ya no cabe: salen las dos barras.
        pagina.setFixedSize(2000, 2000)
        for _ in range(4):
            app.processEvents()

        horizontal = panel.horizontalScrollBar()
        vertical = panel.verticalScrollBar()
        assert horizontal.isVisible() and vertical.isVisible()
        assert soporte_layout.contentsMargins().bottom() == (
            8 + horizontal.height()
        )
        assert soporte_layout.contentsMargins().right() == (
            8 + vertical.width()
        )
        # Y el recuadro queda por encima de la barra, no sobre ella.
        recuadro = soporte_layout.itemAt(0).widget()
        abajo = recuadro.mapTo(marco, QPoint(0, recuadro.height()))
        barra = horizontal.mapTo(marco, QPoint())
        assert abajo.y() <= barra.y()
    finally:
        marco.close()


# ── Etiquetas que no pueden mandar sobre el ancho de la ventana ──────────

def test_el_estado_textual_no_ocupa_la_fila_de_progreso():
    """Los mensajes quedan fuera de la fila y la barra usa ese espacio."""
    _app()
    area = _area_de_trabajo(1366, 768)
    with patch.object(responsive, "available_area", return_value=area):
        ventana = MainWindow()
        try:
            ventana.show()
            antes = ventana.minimumSizeHint().width()
            ventana.status_label.setText(
                "Archivo 12/40: bitacora-YV1234-enero-2026-revisada.pdf - "
                "reconociendo páginas 1234/5678 con la plantilla activa"
            )
            assert not ventana.status_label.isVisibleTo(ventana)
            assert ventana.minimumSizeHint().width() <= antes
            assert ventana.progress.x() == ventana._density.window_margin
        finally:
            ventana.close()


def test_la_etiqueta_recortada_conserva_su_texto():
    """Se recorta lo que se pinta, no lo que la etiqueta dice."""
    _app()
    etiqueta = ElidedLabel("Distribución automática: 4 procesos x 2 hilos")
    etiqueta.show()
    etiqueta.resize(80, 20)
    assert etiqueta.text() == "Distribución automática: 4 procesos x 2 hilos"
    assert etiqueta.toolTip() == etiqueta.text()
    assert etiqueta.minimumSizeHint().width() <= ElidedLabel.MIN_ELIDED_WIDTH
    etiqueta.close()


def test_una_explicacion_propia_gana_al_tooltip_automatico():
    _app()
    etiqueta = ElidedLabel("")
    etiqueta.setToolTip("Rango de páginas contando el batch entero")
    etiqueta.setText("de 35 pág.")
    assert etiqueta.toolTip() == "Rango de páginas contando el batch entero"


# ── La bitácora tiene que caber en la vista previa ───────────────────────

def test_la_vista_previa_se_queda_con_la_mitad_del_ancho():
    """Una hoja de bitácora es vertical: en una franja no se lee.

    Los factores de estiramiento solo reparten el espacio *sobrante*, y el
    panel de la tabla pedía de ancho lo que suman sus controles: se llevaba
    tres cuartas partes de la ventana y la página quedaba en su mínimo. El
    reparto se aplica a mano hasta que alguien mueva el separador.
    """
    app = _app()
    area = _area_de_trabajo(1920, 1080)
    with patch.object(responsive, "available_area", return_value=area):
        ventana = MainWindow()
        try:
            ventana.show()
            for _ in range(4):
                app.processEvents()

            pagina, tabla = ventana.content_splitter.sizes()
            assert abs(pagina - tabla) <= ventana.content_splitter.handleWidth()
        finally:
            ventana.close()
            app.processEvents()


def test_mover_el_separador_manda_sobre_el_reparto_automatico():
    """Quien lo ajusta a mano no quiere que se lo devuelvan al redibujar."""
    app = _app()
    area = _area_de_trabajo(1920, 1080)
    with patch.object(responsive, "available_area", return_value=area):
        ventana = MainWindow()
        try:
            ventana.show()
            app.processEvents()
            ventana.content_splitter.splitterMoved.emit(400, 1)
            ventana.content_splitter.setSizes([1200, 400])

            a_mano = ventana.content_splitter.sizes()

            ventana._balance_content_splitter()

            # Ni se reparte a medias ni se toca lo que el usuario dejó.
            assert ventana.content_splitter.sizes() == a_mano
            assert a_mano[0] > a_mano[1]
        finally:
            ventana.close()
            app.processEvents()
