"""Identidad visual compartida por las ventanas PySide6."""

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QAbstractButton, QComboBox, QLabel, QMainWindow, QMenu,
    QToolButton, QVBoxLayout, QWidget,
)

from app.branding import APPLICATION_DISPLAY_NAME
from app.gui.theme import (
    alternar_tema,
    aplicar_tema,
    install_application_theme,
)
from app.gui.tokens import (
    CLARA,
    FONT_BODY_PT,
    OSCURA,
    RADIUS_CARD,
    RADIUS_CONTROL,
    TEMA_CLARO,
    TEMA_OSCURO,
    accent_color,
    on_accent_text,
    paleta,
    set_tema,
    tema,
)
from app.gui.widgets import (
    accent_button_qss,
    app_chrome_qss,
    pintar_del_tema,
    window_stylesheet,
)


@pytest.fixture
def app_con_tema(tmp_path, monkeypatch):
    """La aplicación con el tema instalado y sin escribir en el disco real.

    El tema elegido se guarda junto al programa. Sin redirigir esa escritura,
    una prueba que cambia de tema le deja la preferencia cambiada a quien
    ejecute el programa después.
    """
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("app.gui.theme.leer_tema", lambda: None)
    monkeypatch.setattr("app.gui.theme.guardar_tema", lambda nombre: True)
    install_application_theme(app)
    try:
        yield app
    finally:
        set_tema(TEMA_OSCURO)
        install_application_theme(app)


def test_application_theme_uses_the_fluent_dark_palette(app_con_tema):
    app = app_con_tema
    c = paleta()

    palette = app.palette()
    assert palette.color(QPalette.ColorRole.Window) == QColor(c.PANE_SURFACE_BG)
    assert palette.color(QPalette.ColorRole.WindowText) == QColor(c.PANE_TEXT)
    assert palette.color(QPalette.ColorRole.Button) == QColor(c.PANE_CONTROL_BG)
    # El acento sale de Windows, no de un azul escrito en el codigo: se
    # compara con lo que el sistema diga, no con un literal.
    assert palette.color(QPalette.ColorRole.Highlight) == QColor(accent_color())
    assert palette.color(QPalette.ColorRole.Accent) == QColor(accent_color())
    # Encima del acento va lo que se lea sobre el acento, no el texto del
    # tema: con un acento claro el blanco de siempre desaparece.
    assert palette.color(QPalette.ColorRole.HighlightedText) == QColor(
        on_accent_text()
    )
    # La hoja global lleva pegado el fragmento del boton de acento, que no
    # cabe en la base: sale del acento del sistema y ese no se conoce hasta
    # que hay QApplication con su paleta puesta.
    assert app.styleSheet() == app_chrome_qss() + accent_button_qss()
    assert app.font().pointSize() == FONT_BODY_PT
    assert app._bits_native_window_theme is not None
    assert window_stylesheet("QWidget { padding: 1px; }") == (
        "QWidget { padding: 1px; }"
    )


def test_el_tema_arranca_en_el_que_quedo_guardado(monkeypatch):
    """Lo que se eligió la vez anterior es con lo que se abre esta."""
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("app.gui.theme.leer_tema", lambda: TEMA_CLARO)
    monkeypatch.setattr("app.gui.theme.guardar_tema", lambda nombre: True)
    try:
        install_application_theme(app)
        assert tema() == TEMA_CLARO
    finally:
        set_tema(TEMA_OSCURO)
        install_application_theme(app)


def test_cambiar_de_tema_repinta_la_aplicacion_entera(app_con_tema):
    """La paleta, la hoja global y las etiquetas con hoja propia, de una vez.

    Las tres viajan por caminos distintos (la paleta de Qt, el ``styleSheet``
    de la aplicación y la hoja de una línea del propio rótulo), y es justo la
    tercera la que antes se quedaba con el gris del tema anterior.
    """
    app = app_con_tema
    rotulo = QLabel()
    pintar_del_tema(rotulo, lambda: f"color: {paleta().TEXT_SECONDARY};")
    assert OSCURA.TEXT_SECONDARY in rotulo.styleSheet()

    assert aplicar_tema(TEMA_CLARO) is True

    assert tema() == TEMA_CLARO
    assert app.palette().color(QPalette.ColorRole.Window) == QColor(
        CLARA.PANE_SURFACE_BG
    )
    assert CLARA.PANE_SURFACE_BG in app.styleSheet()
    assert CLARA.TEXT_SECONDARY in rotulo.styleSheet()
    rotulo.deleteLater()


def test_las_vistas_previas_conservan_seleccion_y_colores_al_cambiar_tema(app_con_tema):
    from pathlib import Path
    from app.airvault.flujo import BatchPrevisto
    from app.airvault.model import EstadoRegistro, Registro
    from app.gui.airvault_previa import BitacorasDelBatch, VistaPreviaBatches

    paginas = BitacorasDelBatch("Prueba", [
        Registro(seq=1, separador="HP-1848CMP"),
        Registro(seq=2, estado=EstadoRegistro.ESCRITA),
    ])
    batches = VistaPreviaBatches([
        BatchPrevisto("Pendiente", 1, 2, False, Path("uno.pdf")),
        BatchPrevisto("Subido", 2, 2, False, Path("dos.pdf"), subido=True, existe=True),
    ])
    tablas = [paginas.tabla, batches.tabla]
    for tabla in tablas:
        tabla.selectRow(1)
    celdas = [[tabla.item(fila, 0) for fila in range(2)] for tabla in tablas]
    for nombre in (TEMA_CLARO, TEMA_OSCURO):
        aplicar_tema(nombre)
        for tabla, originales in zip(tablas, celdas):
            assert [tabla.item(fila, 0) for fila in range(2)] == originales
            assert tabla.currentRow() == 1
            assert originales[0].foreground().color() == QColor(paleta().TEXT_TERTIARY)
            assert originales[1].foreground().color() == QColor(paleta().STATUS_OK)


def test_pedir_el_tema_que_ya_esta_puesto_no_hace_nada(app_con_tema):
    assert aplicar_tema(TEMA_OSCURO) is False
    assert aplicar_tema("verde") is False


def test_alternar_va_y_vuelve_entre_los_dos_temas(app_con_tema):
    assert alternar_tema() == TEMA_CLARO
    assert alternar_tema() == TEMA_OSCURO


def test_el_toggle_repinta_la_ventana_y_las_que_abrio(app_con_tema):
    """El recorrido entero, desde el toggle hasta la otra ventana.

    Las piezas se prueban por separado más arriba; lo que esta comprueba es
    que están conectadas, y sobre todo que el aviso alcanza a una ventana que
    se abrió después y compone su hoja por su cuenta. Ahí es donde el cambio
    de tema se quedaba a medias: la principal cambiaba y las demás no.
    """
    from app.gui.main_window import MainWindow

    ventana = MainWindow()
    try:
        assert ventana.tema_toggle.isChecked() is False
        assert "Tema oscuro" in ventana.tema_toggle.toolTip()
        acciones = {
            accion.text()
            for accion in ventana.more_actions_button.menu().actions()
        }
        assert "Tema claro" not in acciones
        icono_oscuro = ventana.tema_toggle.icon().cacheKey()

        ventana.tema_toggle.click()

        assert tema() == TEMA_CLARO
        assert ventana.tema_toggle.isChecked() is True
        assert "Tema claro" in ventana.tema_toggle.toolTip()
        assert ventana.tema_toggle.icon().cacheKey() != icono_oscuro
        assert ventana.updatesEnabled() is True
        assert CLARA.TABLE_BASE_BG in ventana.styleSheet()
        assert CLARA.TEXT_SECONDARY in ventana.duplicates_label.styleSheet()

        ventana._open_web_reports()
        otra = ventana._web_reports_window
        ventana.tema_toggle.click()

        assert tema() == TEMA_OSCURO
        assert ventana.tema_toggle.isChecked() is False
        assert ventana.updatesEnabled() is True
        assert otra.updatesEnabled() is True
        assert OSCURA.TABLE_BASE_BG in ventana.styleSheet()
        assert OSCURA.TABLE_BASE_BG in otra.styleSheet()
    finally:
        ventana.close()


def test_los_dos_temas_traen_los_mismos_papeles():
    """Ningún tono se queda sin resolver en uno de los dos.

    Es lo que impide que una regla sirva en oscuro y deje un hueco en claro:
    las dos paletas son la misma clase, así que o están los dos colores o no
    se puede construir la paleta.
    """
    assert set(vars(OSCURA)) == set(vars(CLARA))
    # Y ninguno de los dos repite los tonos del otro: son paletas distintas,
    # no la misma con otro nombre.
    assert OSCURA.WINDOW_BG != CLARA.WINDOW_BG
    assert OSCURA.TEXT != CLARA.TEXT


@pytest.mark.parametrize("ancho,alto", [(1366, 728), (1920, 1040)])
def test_alternar_tema_no_mueve_botones_ni_difiere_su_ajuste(
    app_con_tema, monkeypatch, ancho, alto,
):
    """También vigila los saltos transitorios que una captura final no ve."""
    from app.gui.main_window import MainWindow
    from app.gui import responsive

    monkeypatch.setattr(
        responsive, "available_area", lambda *_: QRect(0, 0, ancho, alto)
    )
    ventana = MainWindow()
    ventana.show()
    QTest.qWait(150)
    botones = [b for b in ventana.findChildren(QAbstractButton) if b.isVisible()]

    def posiciones():
        return [(b.mapTo(ventana, QPoint()), b.size()) for b in botones]

    antes = posiciones()
    movimientos = []

    class Vigilar(QObject):
        def eventFilter(self, objeto, evento):
            if objeto in botones and evento.type() in (
                QEvent.Type.Move, QEvent.Type.Resize,
            ):
                movimientos.append((objeto.text(), evento.type().name))
            return False

    filtro = Vigilar()
    for boton in botones:
        boton.installEventFilter(filtro)
    try:
        for _ in range(4):
            ventana.tema_toggle.click()
            assert posiciones() == antes
            # Deja pasar también los LayoutRequest y el ajuste diferido de
            # densidad; antes había botones que volvían a su sitio aquí.
            QTest.qWait(150)
            assert posiciones() == antes
            assert movimientos == []
    finally:
        ventana.close()


def test_cambiar_tema_respeta_layouts_y_pintura_ya_deshabilitados(app_con_tema):
    ventana = QWidget()
    layout = QVBoxLayout(ventana)
    layout.addWidget(QLabel("Pausado"))
    layout.setEnabled(False)
    ventana.setUpdatesEnabled(False)
    try:
        aplicar_tema(TEMA_CLARO)
        assert not layout.isEnabled()
        assert not ventana.updatesEnabled()
    finally:
        ventana.close()


def test_titulo_cambia_al_final_y_no_crea_marcos_para_menus_ocultos(
    app_con_tema, monkeypatch,
):
    import app.gui.theme as modulo_tema

    ventana = QMainWindow()
    ventana.setCentralWidget(QLabel("Ventana visible"))
    oculta = QMainWindow()
    menu = QMenu(ventana)
    menu.addAction("Accion")
    ventana.show()
    QTest.qWait(50)
    marcos = []
    pintados = []

    def marco(widget, oscuro):
        # El titulo debe esperar a que la hoja y la paleta esten listas.
        assert CLARA.PANE_SURFACE_BG in app_con_tema.styleSheet()
        assert widget.palette().color(QPalette.ColorRole.Window) == QColor(
            CLARA.PANE_SURFACE_BG
        )
        marcos.append(widget)

    class VigilarPintura(QObject):
        def eventFilter(self, objeto, evento):
            if objeto is ventana and evento.type() == QEvent.Type.Paint:
                pintados.append(True)
            return False

    filtro = VigilarPintura()
    ventana.installEventFilter(filtro)
    monkeypatch.setattr(modulo_tema, "set_windows_native_window_style", marco)
    try:
        aplicar_tema(TEMA_CLARO)
        assert marcos == [ventana]
        assert pintados  # El contenido se pinto antes de retornar al llamante.
        assert not oculta.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
        assert not menu.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
    finally:
        ventana.close()
        oculta.close()


@pytest.mark.parametrize("nombre", [TEMA_CLARO, TEMA_OSCURO])
def test_paleta_distingue_textos_deshabilitados_y_recolorea_iconos(
    app_con_tema, nombre,
):
    from app.gui.csv_viewer import CsvColumnModeButton
    from app.gui.widgets import load_icon

    boton = CsvColumnModeButton()
    try:
        aplicar_tema(nombre)
        c = paleta()
        for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
            assert app_con_tema.palette().color(
                QPalette.ColorGroup.Disabled, role
            ) == QColor(c.TEXT_DISABLED)
        assert app_con_tema.palette().color(
            QPalette.ColorRole.PlaceholderText
        ) == QColor(c.TEXT_TERTIARY)
        for importante in (True, False):
            boton.setChecked(importante)
            icono = "columns_important" if importante else "columns_all"
            esperado = load_icon(icono, c.PANE_TEXT).pixmap(14, 14).toImage()
            assert boton.icon().pixmap(14, 14).toImage() == esperado
    finally:
        boton.close()


def test_el_tema_claro_escribe_oscuro_sobre_fondo_claro():
    """La escalera de superficies va al revés, y el texto con ella."""
    assert QColor(CLARA.WINDOW_BG).lightness() > QColor(CLARA.TEXT).lightness()
    assert QColor(OSCURA.WINDOW_BG).lightness() < QColor(OSCURA.TEXT).lightness()
    # En claro la tarjeta se apoya en la ventana subiendo hacia el blanco; en
    # oscuro, subiendo hacia el gris claro. En los dos, la tarjeta se separa.
    assert CLARA.CARD_BG != CLARA.WINDOW_BG
    assert OSCURA.CARD_BG != OSCURA.WINDOW_BG


@pytest.mark.parametrize("nombre", [TEMA_OSCURO, TEMA_CLARO])
def test_desplegable_pinta_el_fondo_de_la_fila_seleccionada(app_con_tema, nombre):
    """El texto de selección necesita su fondo incluso con el delegado nativo."""
    combo = QComboBox()
    combo.addItems(["aircraft_log", "aircraft_log.prueba"])
    combo.resize(280, 32)
    combo.show()
    try:
        aplicar_tema(nombre)
        combo.showPopup()
        QTest.qWait(50)
        vista = combo.view()
        indice = vista.model().index(0, 0)
        assert vista.selectionModel().isSelected(indice)
        captura = vista.viewport().grab().toImage()
        assert not captura.isNull()
        rect = vista.visualRect(indice).intersected(vista.viewport().rect())
        assert rect.width() > 24
        escala = captura.devicePixelRatio()
        color = captura.pixelColor(
            int((rect.right() - 12) * escala),
            int(rect.center().y() * escala),
        )
        assert color == QColor(accent_color())
        assert vista.palette().color(QPalette.ColorRole.HighlightedText) == QColor(
            on_accent_text()
        )
    finally:
        combo.hidePopup()
        combo.close()


def test_flechas_del_editor_conservan_navegacion_y_posicion(app_con_tema, monkeypatch, tmp_path):
    from app.gui.editor_window import EditorWindow

    editor = EditorWindow()
    editor.show()
    anterior, siguiente = (
        next(b for b in editor.findChildren(QToolButton) if b.defaultAction() == accion)
        for accion in (editor.btn_prev, editor.btn_next)
    )
    try:
        assert anterior.arrowType() == Qt.ArrowType.LeftArrow
        assert siguiente.arrowType() == Qt.ArrowType.RightArrow
        for boton in (anterior, siguiente):
            assert boton.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
            assert boton.accessibleName() == boton.defaultAction().text()
        editor._pdf_path = tmp_path / "documento.pdf"
        editor._total_pages = 2
        editor._current_page = 1
        monkeypatch.setattr(editor, "_render_current_page", editor._update_nav_state)
        editor._update_nav_state()
        assert not anterior.isEnabled()
        QTest.mouseClick(siguiente, Qt.MouseButton.LeftButton)
        assert editor._current_page == 2
        assert not siguiente.isEnabled()
        QTest.mouseClick(anterior, Qt.MouseButton.LeftButton)
        assert editor._current_page == 1
        app_con_tema.processEvents()
        posiciones = [b.geometry() for b in (anterior, siguiente)]
        for nombre in (TEMA_CLARO, TEMA_OSCURO):
            aplicar_tema(nombre)
            app_con_tema.processEvents()
            assert [b.geometry() for b in (anterior, siguiente)] == posiciones
    finally:
        editor.close()


def test_application_name_is_just_bits():
    """Es como se nombra el programa en la barra de tareas y en el título."""
    assert APPLICATION_DISPLAY_NAME == "BITS"


def test_group_titles_are_inside_the_frame_without_a_background_patch():
    c = paleta()
    title_rule = app_chrome_qss().split("QGroupBox::title", 1)[1].split("}", 1)[0]
    assert "subcontrol-origin: border;" in title_rule
    assert "background: transparent;" in title_rule
    assert f"background-color: {c.TABLE_BASE_BG};" not in title_rule
    assert f"background-color: {c.PANE_CONTROL_BG};" not in title_rule


def test_controls_and_surfaces_share_the_six_pixel_radius():
    assert RADIUS_CONTROL == 6
    assert RADIUS_CARD == 6
    group_rule = app_chrome_qss().split("QGroupBox {", 1)[1].split("}", 1)[0]
    assert "border-radius: 6px;" in group_rule


def test_el_boton_de_acento_solo_cambia_de_tono_al_pasar_y_al_pulsar():
    """Ni anillo blanco al pasar el cursor, ni marco gris al pulsar.

    Los dos venian de distinguir el estado con el marco, que es lo unico que
    se podia mover sin conocer el acento: de ``palette(highlight)`` no sale un
    tono mas claro. Con el acento delante el estado lo dice el propio color,
    que es lo que hace Windows con su boton de acento.
    """
    QApplication.instance() or QApplication([])
    acento = QColor(accent_color())
    c = paleta()
    fragmento = accent_button_qss()

    hover = _regla(fragmento, "#primaryButton:hover")
    pulsado = _regla(fragmento, "#primaryButton:pressed")
    assert c.PANE_TEXT not in hover
    assert c.CONTROL_HOVER not in hover
    assert c.TEXT_DISABLED not in pulsado
    # El fondo y el marco son el mismo color, asi que no hay anillo de nada.
    assert hover.count(_color(hover)) == 2
    assert pulsado.count(_color(pulsado)) == 2
    # Uno mas claro que el acento y otro mas oscuro, los dos reconocibles.
    assert QColor(_color(hover)).lightness() > acento.lightness()
    assert QColor(_color(pulsado)).lightness() < acento.lightness()


def test_la_celda_de_la_flecha_acompana_al_resto_del_boton_de_acento():
    """La flecha no se pinta con el gris de los botones divididos normales.

    Qt le pasa el ``:hover`` del widget entero a esa celda, este el cursor
    sobre el texto o sobre la flecha, asi que un color propio partia el boton
    en dos mitades distintas cada vez que el raton pasaba por encima.
    """
    QApplication.instance() or QApplication([])
    fragmento = accent_button_qss()
    celda = 'QToolButton#primaryButton[menuRole="split"]::menu-button'

    assert _color(_regla(fragmento, f"{celda}:hover")) == _color(
        _regla(fragmento, "#primaryButton:hover")
    )
    assert _color(_regla(fragmento, f"{celda}:pressed")) == _color(
        _regla(fragmento, "#primaryButton:pressed")
    )
    # La regla de la celda pulsada va despues de la del cursor: al pulsar el
    # cursor sigue encima y las dos valen, asi que decide la ultima.
    assert fragmento.index(f"{celda}:pressed") > fragmento.index(f"{celda}:hover")


def _regla(qss: str, selector: str) -> str:
    """Cuerpo de la regla de ese selector exacto."""
    marca = f"\n{selector} {{"
    assert marca in qss, selector
    return qss.split(marca, 1)[1].split("}", 1)[0]


def _color(regla: str) -> str:
    """El hex que declara la regla, para comparar reglas entre si."""
    return regla.split("background-color:", 1)[1].split(";", 1)[0].strip()
