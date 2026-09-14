"""Widgets Qt compartidos por las ventanas de la aplicación."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPalette,
    QRegion,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.gui.tokens import (
    ACCENT_FALLBACK,
    CONTROL_BOX_H,
    CONTROL_HEIGHT,
    CONTROL_PAD_H,
    FONT_BODY_PT,
    FONT_CAPTION_PT,
    FONT_FAMILY,
    RADIUS_CARD,
    RADIUS_CONTROL,
    SPACE_S,
    WEIGHT_STRONG,
    TEMA_CLARO,
    TEMA_OSCURO,
    accent_color,
    blend,
    checked_row_color,
    hover_row_color,
    link_text_color,
    on_accent_text,
    paleta,
    tema,
)

_ASSETS = Path(__file__).resolve().parents[2] / "assets"
# La flecha de los desplegables, una por tema. Va por archivo y no por color
# porque QSS la pide con ``image: url(...)`` y a un archivo no se le puede
# cambiar la tinta desde la hoja; los iconos de los botones si se tinan al
# vuelo (ver ``load_icon``), pero esos los pone Python y no la hoja.
_DROPDOWN_ARROWS = {
    TEMA_OSCURO: (_ASSETS / "dropdown_arrow.svg").as_posix(),
    TEMA_CLARO: (_ASSETS / "dropdown_arrow_claro.svg").as_posix(),
}


def _dropdown_arrow() -> str:
    """La flecha que se lee sobre la superficie del tema puesto ahora."""
    return _DROPDOWN_ARROWS[tema()]

# Los tonos ya no se copian aqui. Los nombres siguen siendo los mismos
# («la base de la tabla», «el borde del panel»), pero viven en
# ``tokens.Paleta`` y se piden con ``paleta()`` en el momento de armar la
# hoja: una constante de modulo se quedaria con los grises del arranque y el
# cambio de tema no llegaria a ninguna ventana ya abierta.

# El azul de la seleccion no es un valor escrito aqui: es el acento que el
# usuario eligio en Windows. Este queda como reserva, para el codigo que
# necesita un literal antes de que exista la aplicacion a la que preguntarle.
TABLE_SELECTION_BG = ACCENT_FALLBACK

# La fila bajo el cursor y la fila marcada con su casilla viven en
# ``tokens.hover_row_color`` y ``tokens.checked_row_color``: se calculan del
# acento en lugar de ser dos azules escritos a mano, que era lo que dejaba una
# banda azul en una aplicación con el acento en rojo.


def pane_status_colors() -> dict[str, str]:
    """Los tres estados, en los tonos del tema puesto ahora.

    Se leen como texto sobre la superficie del panel y no como relleno de
    celda, asi que cambian con el tema: el verde claro que se lee de noche
    desaparece sobre el blanco, y al reves.
    """
    tonos = paleta()
    return {
        "OK": tonos.STATUS_OK,
        "WARNING": tonos.STATUS_WARNING,
        "ERROR": tonos.STATUS_ERROR,
    }


# Radio de esquina. El de los controles; las superficies que los contienen
# llevan el suyo, mayor, para que un botón dentro de un cuadro no tenga la
# misma curva que el cuadro.
TABLE_RADIUS = RADIUS_CONTROL

# Botón dividido: la flecha vive en su propia celda pegada al borde derecho,
# fuera del área que se pulsa. Qt centra la etiqueta en el botón entero y no
# sabe de esa celda, así que el texto se va hacia la derecha hasta chocar con
# el separador. Se compensa con el relleno: si el de la derecha vale el de la
# izquierda más la celda, el centro del hueco de texto vuelve a caer en el
# centro de la parte pulsable.
#
# Van como constantes y no como números sueltos dentro de la hoja porque la
# relación entre los tres es lo que hace que el texto quede centrado; escritos
# a mano en sitios distintos se desincronizan sin que nada avise.
SPLIT_MENU_WIDTH = 24
SPLIT_PAD_LEFT = 10
SPLIT_PAD_RIGHT = SPLIT_PAD_LEFT + SPLIT_MENU_WIDTH


def data_table_qss() -> str:
    """La tabla, en los tonos del tema puesto ahora.

    La comparten todas las ventanas con tabla para que se vea igual en
    todas. Se arma al pedirla y no al importar el modulo: es lo que hace
    que al cambiar de tema la ventana ya abierta pueda volver a pedirla.
    """
    c = paleta()
    return (
        "QTableView, QTableWidget {"
        f" background-color: {c.TABLE_BASE_BG};"
        f" alternate-background-color: {c.TABLE_ALTERNATE_BG};"
        f" color: {c.TABLE_TEXT};"
        f" gridline-color: {c.TABLE_GRID};"
        f" selection-background-color: palette(highlight);"
        # El texto de la fila seleccionada va sobre el acento, no sobre la
        # tabla: lo decide la luminancia del acento y no el tema, porque en
        # los dos hay acentos claros sobre los que el blanco no se lee.
        f" selection-color: {on_accent_text()};"
        f" border: 1px solid {c.PANE_BORDER};"
        f" border-radius: {TABLE_RADIUS}px; }}"
        "QHeaderView { background-color: transparent; }"
        "QHeaderView::section {"
        f" background-color: {c.TABLE_HEADER_BG};"
        f" color: {c.TABLE_TEXT}; padding: 6px 8px; font-weight: 600;"
        f" border: 0; border-right: 1px solid {c.TABLE_GRID};"
        f" border-bottom: 1px solid {c.TABLE_GRID}; }}"
        "QTableCornerButton::section {"
        f" background-color: {c.TABLE_HEADER_BG};"
        f" border: 0; border-right: 1px solid {c.TABLE_GRID};"
        f" border-bottom: 1px solid {c.TABLE_GRID}; }}"
        # El hueco entre las dos barras de desplazamiento lo pinta la propia área
        # de scroll y es lo único que no respeta el radio: sin dejarlo
        # transparente, la esquina inferior derecha se queda en pico.
        "QTableView::corner, QTableWidget::corner { background: transparent; }"
        # Las barras de la propia tabla van en la hoja: aplicadas por
        # descendencia sirven igual aqui y en el visor de PDF.
    ) + scrollbars_qss("QTableView") + scrollbars_qss("QTableWidget")


def scrollbars_qss(scope: str) -> str:
    """Barras de desplazamiento del tema para los widgets de ``scope``.

    Son parte de la superficie: dejarlas en el blanco nativo ponia una franja
    luminosa al borde de cada widget oscuro. Se aplican por descendencia para
    que sirvan igual a la tabla y al visor de PDF.
    """
    c = paleta()
    return (
        f"{scope} QScrollBar:vertical, {scope} QScrollBar:horizontal {{"
        f" background: {c.TABLE_HEADER_BG}; border: 0; margin: 0; }}"
        f"{scope} QScrollBar:vertical {{ width: 12px; }}"
        f"{scope} QScrollBar:horizontal {{ height: 12px; }}"
        f"{scope} QScrollBar::handle:vertical,"
        f"{scope} QScrollBar::handle:horizontal {{"
        f" background: {c.TABLE_GRID}; border-radius: 6px; margin: 2px; }}"
        f"{scope} QScrollBar::handle:vertical:hover,"
        f"{scope} QScrollBar::handle:horizontal:hover {{"
        f" background: {c.SCROLL_HANDLE_HOVER}; }}"
        f"{scope} QScrollBar::handle:vertical {{ min-height: 24px; }}"
        f"{scope} QScrollBar::handle:horizontal {{ min-width: 24px; }}"
        # Sin esto Qt reserva el hueco de los botones de flecha y deja dos
        # cuadros vacíos en los extremos.
        f"{scope} QScrollBar::add-line, {scope} QScrollBar::sub-line {{"
        " width: 0; height: 0; border: 0; background: none; }"
        f"{scope} QScrollBar::add-page, {scope} QScrollBar::sub-page {{"
        " background: none; }"
    )


def zoom_overlay_qss() -> str:
    """El recuadro flotante de zoom, en los tonos del tema puesto ahora.

    El mismo bloque en la vista previa de la ventana principal y en el visor
    de PDF del visor de CSV. Vive aqui para que los dos no puedan separarse;
    cada ventana lo anade al final de su hoja, despues de sus reglas de panel,
    para ganar a las que tienen la misma especificidad.
    """
    c = paleta()
    return f"""
#zoomOverlay {{
    background-color: {c.CARD_BG};
    border: 1px solid {c.STROKE};
    border-radius: {RADIUS_CARD}px;
}}
#zoomOverlay QLabel {{
    border: 0;
    background: transparent;
    color: {c.TEXT};
    font-size: {FONT_CAPTION_PT}pt;
    font-weight: {WEIGHT_STRONG};
}}
#zoomOverlay QToolButton#zoomControl {{
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    max-height: 28px;
    padding: 0;
    border: 1px solid transparent;
    border-radius: {RADIUS_CONTROL}px;
    background-color: {c.CARD_BG};
}}
#zoomOverlay QToolButton#zoomControl:hover {{
    background-color: {c.CONTROL_HOVER};
    border-color: {c.STROKE_STRONG};
}}
#zoomOverlay QToolButton#zoomControl:pressed {{
    background-color: {c.CONTROL_PRESSED};
    border-color: {c.STROKE_STRONG};
}}
#zoomOverlay QToolButton#zoomControl:disabled {{
    background-color: {c.CARD_BG};
}}
#zoomOverlay QLabel#zoomValue {{
    min-width: 40px;
    padding: 0 2px;
    color: {c.TEXT};
    font-size: {FONT_CAPTION_PT}pt;
    font-weight: {WEIGHT_STRONG};
}}
"""


def app_chrome_qss() -> str:
    """Tipografia, controles y superficies de toda la aplicacion.

    Los colores salen de las mismas superficies que las tablas y los
    visores para que todas las ventanas se lean como una sola aplicacion.
    El marco del sistema lo completa ``theme.py``.

    Antes se armaba al importar el modulo, cuando todavia no hay
    ``QApplication`` a la que preguntarle el acento: por eso tantas reglas
    piden ``palette(highlight)``, que es el unico acento que QSS sabe leer
    solo. Ahora se arma al instalarla, asi que el texto que va encima del
    acento (``ON_ACCENT``) si puede decidirse por luminancia en vez de dar
    por hecho que el acento es un azul oscuro y escribir blanco encima.
    """
    c = paleta()
    on_accent = on_accent_text()
    flecha = _dropdown_arrow()
    flecha_acento = _DROPDOWN_ARROWS[
        TEMA_CLARO if QColor(on_accent).lightness() < 128 else TEMA_OSCURO
    ]
    separador_acento = blend(on_accent, accent_color(), 0.45)
    return f"""
QMainWindow, QDialog {{
    background-color: {c.PANE_SURFACE_BG};
}}
QWidget {{
    color: {c.PANE_TEXT};
    font-family: {FONT_FAMILY};
    font-size: {FONT_BODY_PT}pt;
}}
QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled {{
    color: {c.TEXT_DISABLED};
}}
QPushButton {{
    min-height: {CONTROL_BOX_H}px;
    max-height: {CONTROL_BOX_H}px;
    padding: 0 {CONTROL_PAD_H}px;
    color: {c.PANE_TEXT};
    background-color: {c.PANE_CONTROL_BG};
    border: 1px solid {c.PANE_BORDER};
    border-radius: {TABLE_RADIUS}px;
}}
QToolButton {{
    min-height: {CONTROL_BOX_H}px;
    max-height: {CONTROL_BOX_H}px;
    padding: 0 {CONTROL_PAD_H}px;
    color: {c.PANE_TEXT};
    background-color: {c.PANE_CONTROL_BG};
    border: 1px solid {c.PANE_BORDER};
    border-radius: {TABLE_RADIUS}px;
}}
QToolButton#primaryButton {{
    min-height: {CONTROL_BOX_H}px;
    max-height: {CONTROL_BOX_H}px;
    padding: 0 {CONTROL_PAD_H}px;
}}
QToolButton[menuRole="dropdown"] {{
    min-height: {CONTROL_BOX_H}px;
    max-height: {CONTROL_BOX_H}px;
    padding: 0 22px 0 {CONTROL_PAD_H}px;
}}
QToolButton[menuRole="dropdown"]::menu-indicator {{
    subcontrol-origin: border;
    subcontrol-position: right center;
    position: relative;
    right: 7px;
    width: 10px;
    height: 6px;
    image: url("{flecha}");
}}
QToolButton[menuRole="split"] {{
    min-height: {CONTROL_BOX_H}px;
    max-height: {CONTROL_BOX_H}px;
    padding: 0 {SPLIT_PAD_RIGHT}px 0 {SPLIT_PAD_LEFT}px;
}}
QToolButton[menuRole="split"]::menu-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: {SPLIT_MENU_WIDTH}px;
    border: 0;
    border-left: 1px solid {c.PANE_BORDER};
    border-top-right-radius: {TABLE_RADIUS}px;
    border-bottom-right-radius: {TABLE_RADIUS}px;
}}
QToolButton[menuRole="split"]::menu-button:hover {{
    background-color: {c.PANE_CONTROL_HOVER};
}}
QToolButton[menuRole="split"]::menu-arrow {{
    width: 10px;
    height: 6px;
    image: url("{flecha}");
}}
/* El relleno se repite aquí a propósito. En QSS un selector de ID pesa más
   que uno de atributo, así que «QToolButton#primaryButton» le gana a
   «QToolButton[menuRole="split"]» y le colaba su relleno simétrico al único
   botón dividido de la ventana: el texto se centraba en el botón entero y
   acababa pegado al separador de la flecha. */
QToolButton#primaryButton[menuRole="split"] {{
    min-height: {CONTROL_BOX_H}px;
    max-height: {CONTROL_BOX_H}px;
    padding: 0 {SPLIT_PAD_RIGHT}px 0 {SPLIT_PAD_LEFT}px;
}}
QPushButton:hover, QToolButton:hover {{
    background-color: {c.PANE_CONTROL_HOVER};
}}
QPushButton:pressed, QToolButton:pressed,
QPushButton:checked, QToolButton:checked {{
    background-color: {c.CONTROL_PRESSED};
}}
QPushButton:focus, QToolButton:focus {{
    border-color: palette(highlight);
}}
QPushButton:default {{
    border-color: palette(highlight);
}}
QPushButton:disabled, QToolButton:disabled {{
    color: {c.TEXT_DISABLED};
    background-color: {c.CONTROL_DISABLED};
    border-color: {c.PANE_BG};
}}
#primaryButton {{
    background-color: palette(highlight);
    color: {on_accent};
    border-color: palette(highlight);
}}
/* Flecha y separador acompañan al texto sobre el acento, que puede ser
   distinto del texto sobre las superficies del tema. */
QToolButton#primaryButton[menuRole="split"]::menu-button {{
    border-left: 1px solid {separador_acento};
}}
QToolButton#primaryButton[menuRole="split"]::menu-arrow {{
    image: url("{flecha_acento}");
}}
QToolButton#spinStepButton {{
    min-width: 18px; max-width: 18px; min-height: 0;
    padding: 0;
    border: 1px solid {c.PANE_BORDER};
    border-radius: {TABLE_RADIUS}px;
    background-color: {c.PANE_CONTROL_BG};
}}
QToolButton#spinStepButton:hover {{
    background-color: {c.PANE_CONTROL_HOVER};
    border-color: {c.TEXT_DISABLED};
}}
QToolButton#spinStepButton:pressed {{
    background-color: {c.CONTROL_PRESSED};
    border-color: {c.TEXT_DISABLED};
}}
QToolButton#spinStepButton:disabled {{
    background-color: {c.CONTROL_DISABLED};
    color: {c.TEXT_DISABLED};
}}
/* El campo numérico va en la lista. Fuera de ella se quedaba con el marco
   nativo, que mide 3 px por lado en vez de 1, y acababa 4 px más alto que sus
   vecinos de fila sin que ninguna regla lo dijera. */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QDateEdit, QTimeEdit, QDateTimeEdit {{
    min-height: {CONTROL_BOX_H}px;
    max-height: {CONTROL_BOX_H}px;
    padding: 0 {CONTROL_PAD_H}px;
    color: {c.PANE_TEXT};
    background-color: {c.PANE_CONTROL_BG};
    border: 1px solid {c.PANE_BORDER};
    /* Fluent marca el foco con una linea fina abajo, y solo al enfocar. Los
       2 px permanentes que habia aqui son de Material, y ponian un subrayado
       claro bajo cada campo de la ventana. */
    border-bottom: 1px solid {c.STROKE_STRONG};
    border-radius: {TABLE_RADIUS}px;
    selection-background-color: palette(highlight);
    selection-color: {on_accent};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover,
QDateEdit:hover, QTimeEdit:hover, QDateTimeEdit:hover {{
    background-color: {c.PANE_CONTROL_HOVER};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QDateEdit:focus, QTimeEdit:focus, QDateTimeEdit:focus {{
    border-bottom: 2px solid palette(highlight);
}}
QLineEdit:read-only {{
    background-color: {c.PANE_CONTROL_BG};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled, QDateEdit:disabled, QTimeEdit:disabled,
QDateTimeEdit:disabled {{
    color: {c.TEXT_DISABLED};
    background-color: {c.CONTROL_DISABLED};
    border-color: {c.PANE_BG};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border: 0;
    border-left: 1px solid transparent;
    background: transparent;
}}
QSpinBox, QDoubleSpinBox {{
    min-height: {CONTROL_BOX_H}px;
    max-height: {CONTROL_BOX_H}px;
    padding-left: {CONTROL_PAD_H}px;
    padding-right: {CONTROL_PAD_H}px;
    padding-top: 0;
    padding-bottom: 0;
}}
QComboBox {{
    padding: 0 30px 0 {CONTROL_PAD_H}px;
}}
QComboBox::down-arrow {{
    width: 10px;
    height: 6px;
    image: url("{flecha}");
}}
QComboBox:hover::drop-down, QComboBox:on::drop-down {{
    background-color: {c.PANE_CONTROL_HOVER};
    border-left-color: {c.PANE_BORDER};
}}
QComboBox QAbstractItemView {{
    color: {c.PANE_TEXT};
    background-color: {c.PANE_BG};
    border: 1px solid {c.PANE_BORDER};
    border-radius: {TABLE_RADIUS}px;
    padding: 4px;
    selection-background-color: palette(highlight);
    selection-color: {on_accent};
    outline: 0;
}}
QComboBox QAbstractItemView::item {{
    min-height: {CONTROL_BOX_H}px;
    padding: 3px 8px;
    border-radius: 4px;
}}
QComboBox QAbstractItemView::item:selected {{
    color: {on_accent};
    background-color: palette(highlight);
}}
/* El campo de fecha es el unico desplegable de la aplicacion que no es un
   QComboBox, y era el unico sin reglas: se quedaba con lo que dibuja
   Windows, un boton gris con el triangulo del sistema donde el resto de la
   ventana lleva la flecha fina de la hoja. El hueco de la derecha es el
   mismo que reserva el combo, para que el texto no se meta debajo. */
QDateEdit, QTimeEdit, QDateTimeEdit {{
    padding: 0 30px 0 {CONTROL_PAD_H}px;
}}
QDateEdit::drop-down, QTimeEdit::drop-down, QDateTimeEdit::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border: 0;
    background: transparent;
}}
QDateEdit::down-arrow, QTimeEdit::down-arrow, QDateTimeEdit::down-arrow {{
    width: 10px;
    height: 6px;
    image: url("{flecha}");
}}
QDateEdit:hover::drop-down, QDateEdit:on::drop-down,
QTimeEdit:hover::drop-down, QDateTimeEdit:hover::drop-down {{
    background-color: {c.PANE_CONTROL_HOVER};
}}
/* Y el calendario que sale al pulsarla. Es una ventana con sus propios
   hijos, asi que hay que nombrarlos uno a uno: la barra del mes, sus
   flechas y la rejilla de dias. La rejilla ademas hace falta nombrarla
   porque es un QTableView, y sin esto le caia entera la hoja de las tablas
   de datos: lineas de cuadricula, filas alternas y cabecera en negrita
   sobre lo que tiene que ser un calendario. */
QCalendarWidget QWidget#qt_calendar_navigationbar {{
    background-color: {c.TABLE_HEADER_BG};
    border-top-left-radius: {TABLE_RADIUS}px;
    border-top-right-radius: {TABLE_RADIUS}px;
}}
QCalendarWidget QToolButton {{
    color: {c.PANE_TEXT};
    background-color: transparent;
    border: 0;
    border-radius: {TABLE_RADIUS}px;
    margin: 2px;
    padding: 0 {SPACE_S}px;
}}
QCalendarWidget QToolButton:hover {{
    background-color: {c.PANE_CONTROL_HOVER};
}}
QCalendarWidget QToolButton::menu-indicator {{
    image: none;
}}
QCalendarWidget QAbstractItemView {{
    color: {c.PANE_TEXT};
    background-color: {c.PANE_BG};
    alternate-background-color: {c.PANE_BG};
    gridline-color: transparent;
    selection-background-color: palette(highlight);
    selection-color: {on_accent};
    border: 0;
    outline: 0;
}}
QCalendarWidget QAbstractItemView:disabled {{
    color: {c.TEXT_DISABLED};
}}
QGroupBox {{
    color: {c.PANE_TEXT};
    background-color: {c.TABLE_BASE_BG};
    font-weight: 600;
    border: 1px solid {c.PANE_BORDER};
    border-radius: {RADIUS_CARD}px;
    margin-top: 0;
    padding: 24px 12px 12px 12px;
}}
QGroupBox::title {{
    subcontrol-origin: border;
    subcontrol-position: top left;
    left: 8px;
    top: 5px;
    padding: 0;
    color: {c.PANE_TEXT};
    background: transparent;
}}
/* La casilla comparte fila con campos y botones, asi que comparte alto: sin
   esto medía 14 px contra los 32 de sus vecinos, se leia descolgada de su
   propia fila y el sitio donde se puede pulsar era menos de la mitad. El
   recuadro lo sigue dibujando Windows; aqui solo se le da la caja. */
QCheckBox, QRadioButton {{
    spacing: {SPACE_S}px;
    min-height: {CONTROL_HEIGHT}px;
    max-height: {CONTROL_HEIGHT}px;
}}
QProgressBar {{
    min-height: {CONTROL_BOX_H}px;
    max-height: {CONTROL_BOX_H}px;
    color: {c.PANE_TEXT};
    background-color: {c.PANE_CONTROL_BG};
    border: 1px solid {c.PANE_BORDER};
    border-radius: {TABLE_RADIUS}px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: palette(highlight);
    border-radius: 5px;
}}
QPlainTextEdit, QTextEdit, QListView, QListWidget, QTreeView, QTreeWidget {{
    color: {c.PANE_TEXT};
    background-color: {c.PANE_BG};
    alternate-background-color: {c.TABLE_ALTERNATE_BG};
    border: 1px solid {c.PANE_BORDER};
    border-radius: {TABLE_RADIUS}px;
    selection-background-color: palette(highlight);
    selection-color: {on_accent};
    outline: 0;
}}
QToolBar {{
    color: {c.PANE_TEXT};
    background-color: {c.PANE_SURFACE_BG};
    border: 0;
    border-bottom: 1px solid {c.PANE_BORDER};
    spacing: 4px;
    padding: 4px;
}}
QToolBar::separator {{
    width: 1px;
    margin: 5px 4px;
    background-color: {c.PANE_BORDER};
}}
QMenu {{
    color: {c.PANE_TEXT};
    background-color: {c.PANE_BG};
    border: 1px solid {c.PANE_BORDER};
    border-radius: {TABLE_RADIUS}px;
    padding: 4px;
}}
/* El relleno de la izquierda no es la sangría del texto: Qt le suma la
   columna del icono o de la marca, así que los 30 px de antes dejaban al
   rótulo a 46 px del borde y abrían un hueco enorme entre la marca y su
   texto. Con 12 px la separación queda en los ~12 que usa el menú de
   Windows, y los menús sin marcas ni iconos siguen con su sangría. */
QMenu::item {{
    border-radius: {TABLE_RADIUS}px;
    min-height: 22px;
    padding: 5px 28px 5px 12px;
}}
QMenu::item:selected {{ background-color: {c.PANE_CONTROL_HOVER}; }}
QMenu::item:disabled {{ color: {c.TEXT_DISABLED}; }}
QMenu::indicator {{
    width: 16px;
    height: 16px;
}}
QMenu::separator {{
    height: 1px;
    margin: 4px 8px;
    background-color: {c.PANE_BORDER};
}}
QTabWidget::pane {{
    border: 1px solid {c.PANE_BORDER};
    border-radius: {TABLE_RADIUS}px;
    background-color: {c.TABLE_BASE_BG};
}}
QTabBar::tab {{
    color: {c.PANE_TEXT};
    background-color: transparent;
    border: 0;
    border-radius: {TABLE_RADIUS}px;
    padding: 6px 12px;
}}
QTabBar::tab:hover {{ background-color: {c.PANE_CONTROL_HOVER}; }}
QTabBar::tab:selected {{
    background-color: {c.PANE_CONTROL_BG};
    border-bottom: 2px solid palette(highlight);
}}
QSplitter::handle {{ background-color: {c.PANE_SURFACE_BG}; }}
QSplitter::handle:hover {{ background-color: {c.PANE_BORDER}; }}
QScrollBar:vertical, QScrollBar:horizontal {{
    background: {c.TABLE_HEADER_BG};
    border: 0;
    margin: 0;
}}
QScrollBar:vertical {{ width: 12px; }}
QScrollBar:horizontal {{ height: 12px; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {c.PANE_BORDER};
    border-radius: 6px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover,
QScrollBar::handle:horizontal:hover {{ background: {c.SCROLL_HANDLE_HOVER}; }}
QScrollBar::handle:vertical {{ min-height: 24px; }}
QScrollBar::handle:horizontal {{ min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
    border: 0;
    background: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QToolTip {{
    color: {c.PANE_TEXT};
    background-color: {c.PANE_CONTROL_BG};
    border: 1px solid {c.PANE_BORDER};
    border-radius: {TABLE_RADIUS}px;
    padding: 4px 7px;
}}
QStatusBar {{
    color: {c.PANE_TEXT};
    background-color: {c.PANE_SURFACE_BG};
}}
""" + zoom_overlay_qss()



_APPLICATION_THEME_PROPERTY = "bitsApplicationThemeInstalled"

# Cuánto se aclara el botón de acento al pasar el cursor y cuánto se oscurece
# al pulsarlo. Es el gesto de Fluent para el botón de acento: el color se
# mueve un paso, el mismo en todo el botón, sin marco de otro color.
_ACCENT_HOVER = 0.10
_ACCENT_PRESSED = 0.12


def accent_button_qss() -> str:
    """Estados del botón de acento, ya con el acento del sistema delante.

    La hoja base se arma al importar el módulo, cuando todavía no hay
    ``QApplication`` a la que preguntarle el color: por eso el reposo pide
    ``palette(highlight)``, que es el único acento que QSS sabe leer solo.
    Pero de ``palette(highlight)`` no se puede sacar un tono más claro ni uno
    más oscuro, y esa era la raíz de lo que se veía: el hover se distinguía
    cambiando el marco a blanco y el pulsado a gris, así que el botón se
    rodeaba de un anillo que no es de ningún estado de Windows.

    Aquí ya hay acento, así que hover y pulsado son el mismo color un paso
    más claro y un paso más oscuro, con el marco a juego. La celda de la
    flecha del botón dividido se pinta con ese mismo color y no con el gris
    de la regla general de los botones divididos: Qt le pasa el ``:hover`` de
    todo el widget, esté el cursor sobre el texto o sobre la flecha, y
    cualquier color propio partía el botón en dos mitades distintas.
    """
    accent = accent_color()
    hover = blend("#ffffff", accent, _ACCENT_HOVER)
    pressed = blend("#000000", accent, _ACCENT_PRESSED)
    return f"""
#primaryButton:hover {{
    background-color: {hover};
    border-color: {hover};
}}
#primaryButton:pressed {{
    background-color: {pressed};
    border-color: {pressed};
}}
QToolButton#primaryButton[menuRole="split"]::menu-button:hover {{
    background-color: {hover};
}}
/* Después del de hover: al pulsar el cursor sigue encima, así que las dos
   reglas valen a la vez y decide la última. Sin esta, la celda se quedaba
   con el tono del cursor mientras el resto del botón ya estaba pulsado. */
QToolButton#primaryButton[menuRole="split"]::menu-button:pressed {{
    background-color: {pressed};
}}
"""


# Donde se guarda, en el propio widget, como volver a vestirlo. Es un atributo
# de Python y no una propiedad de Qt porque lo que se guarda es una funcion.
#
# Hace falta un registro asi porque hay dos clases de color que la hoja global
# no alcanza: la etiqueta que lleva el suyo en una hoja de una linea, y el
# widget al que se le fijo la paleta a mano (la tabla, el panel del visor)
# porque el estilo nativo de Windows pinta el fondo desde la paleta y no desde
# la hoja. Ninguno de los dos se entera de que la aplicacion cambio de tema.
_REPINTAR_TEMA = "_bits_repintar_tema"


def al_cambiar_tema(widget: QWidget, repintar) -> None:
    """Viste el widget ahora y deja anotado como volver a hacerlo."""
    setattr(widget, _REPINTAR_TEMA, repintar)
    repintar()


def pintar_del_tema(widget: QWidget, hoja) -> None:
    """Le pone al widget una hoja propia que sabe rehacerse al cambiar de tema.

    Las etiquetas de ayuda, los resumenes y los rotulos de estado llevan su
    color en una hoja de una linea, puesta al construirlas. Escrito asi el
    color se congelaba: al cambiar de tema la ventana se repintaba entera y
    esos rotulos se quedaban con el gris del tema anterior, que sobre la
    superficie nueva o desaparece o grita.

    ``hoja`` es la funcion que devuelve esa linea, no la linea ya armada.
    """
    al_cambiar_tema(widget, lambda: widget.setStyleSheet(hoja()))


def repintar_del_tema(app: QApplication) -> None:
    """Vuelve a vestir a todos los widgets anotados con ``al_cambiar_tema``."""
    for widget in app.allWidgets():
        repintar = getattr(widget, _REPINTAR_TEMA, None)
        if repintar is not None:
            repintar()
        if isinstance(widget, QTableWidget):
            for row in range(widget.rowCount()):
                for column in range(widget.columnCount()):
                    item = widget.item(row, column)
                    papel = item.data(_ROL_COLOR_TEMA) if item is not None else None
                    if papel:
                        pintar_celda_del_tema(item, papel)


_ROL_COLOR_TEMA = Qt.ItemDataRole.UserRole + 128


def pintar_celda_del_tema(item: QTableWidgetItem, papel: str) -> None:
    """Conserva el papel del color para repintar sin rehacer ni ordenar filas."""
    item.setData(_ROL_COLOR_TEMA, papel)
    color = link_text_color() if papel == "acento" else getattr(paleta(), papel)
    item.setForeground(QColor(color))


def window_stylesheet(local_qss: str) -> str:
    """Compone una hoja local sin duplicar el tema global instalado."""
    app = QApplication.instance()
    if app is not None and app.property(_APPLICATION_THEME_PROPERTY):
        return local_qss
    return app_chrome_qss() + accent_button_qss() + local_qss


class MultiSelectMenu(QMenu):
    """Menu de casillas que no se cierra entre selecciones."""

    def _trigger_active_check(self) -> bool:
        action = self.activeAction()
        if action is None or not action.isEnabled() or not action.isCheckable():
            return False
        action.trigger()
        return True

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - API Qt
        if self._trigger_active_check():
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - API Qt
        if event.key() in (
            Qt.Key.Key_Space,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ) and self._trigger_active_check():
            return
        super().keyPressEvent(event)


class SpinBoxWithButtons(QWidget):
    """Campo numérico con las flechas fuera del área de texto.

    El ``QSpinBox`` sigue siendo el dato público para no duplicar su API. Este
    contenedor solo separa sus dos pasos en una columna a la derecha y refleja
    tanto los límites como el estado habilitado del campo.
    """

    def __init__(self, spin: QSpinBox, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spin = spin
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        row.addWidget(spin)

        self.up_button = self._button(Qt.ArrowType.UpArrow, "Aumentar valor")
        self.down_button = self._button(
            Qt.ArrowType.DownArrow, "Disminuir valor"
        )
        # Comparten la fila para conservar exactamente el alto del campo.
        # Apilarlas duplicaría la altura de los controles compactos.
        row.addWidget(self.up_button)
        row.addWidget(self.down_button)

        self.up_button.clicked.connect(spin.stepUp)
        self.down_button.clicked.connect(spin.stepDown)
        spin.valueChanged.connect(self.sync_buttons)
        spin.installEventFilter(self)
        self.sync_buttons()

    def _button(self, arrow: Qt.ArrowType, accessible_name: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("spinStepButton")
        button.setArrowType(arrow)
        button.setAccessibleName(accessible_name)
        button.setToolTip(accessible_name)
        button.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Ignored
        )
        button.setAutoRepeat(True)
        button.setAutoRepeatDelay(300)
        button.setAutoRepeatInterval(80)
        return button

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - API Qt
        if watched is self.spin and event.type() == QEvent.Type.EnabledChange:
            self.sync_buttons()
        return super().eventFilter(watched, event)

    def sync_buttons(self, *_args) -> None:
        """Habilita cada flecha solo cuando ese paso se puede ejecutar."""
        enabled = self.spin.isEnabled() and not self.spin.isReadOnly()
        steps = self.spin.stepEnabled()
        self.up_button.setEnabled(
            enabled
            and bool(steps & QAbstractSpinBox.StepEnabledFlag.StepUpEnabled)
        )
        self.down_button.setEnabled(
            enabled
            and bool(steps & QAbstractSpinBox.StepEnabledFlag.StepDownEnabled)
        )


def _mezclado(fondo: QBrush, color: str, peso: float = 0.55) -> QColor:
    """Tiñe el color que ya tenía la celda en lugar de taparlo.

    El realce del cursor es pasajero y recorre la tabla entera: si borrara
    los colores de estado, media tabla cambiaría de significado mientras se
    mueve el ratón. Mezclándolo, la fila se lee como una sola banda y cada
    celda conserva de qué color era.
    """
    encima = QColor(color)
    if fondo.style() == Qt.BrushStyle.NoBrush:
        return encima
    debajo = fondo.color()
    resto = 1.0 - peso
    return QColor(
        round(debajo.red() * resto + encima.red() * peso),
        round(debajo.green() * resto + encima.green() * peso),
        round(debajo.blue() * resto + encima.blue() * peso),
    )


class _HoverRowTracker(QObject):
    """Recuerda sobre qué fila está el cursor y repinta las dos afectadas.

    Qt solo avisa del hover celda a celda, y ninguna celda sabe que su
    vecina de la misma fila también tiene que resaltarse. Aquí se guarda la
    fila en la propia vista (``hoverRow``), donde el delegado la consulta al
    pintar cada celda, y se repinta la fila que se deja y la que se toma.
    """

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self._view = view
        view.setProperty("hoverRow", -1)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - API Qt
        tipo = event.type()
        if tipo == QEvent.Type.MouseMove:
            self._marcar(self._view.indexAt(event.position().toPoint()).row())
        elif tipo in (QEvent.Type.Leave, QEvent.Type.Wheel):
            # Al salir no llega ningún movimiento más, y al rodar la rueda la
            # fila bajo el cursor cambia sin que el ratón se mueva.
            self._marcar(-1)
        return False

    def _marcar(self, fila: int) -> None:
        anterior = self._view.property("hoverRow")
        anterior = -1 if anterior is None else int(anterior)
        if fila == anterior:
            return
        self._view.setProperty("hoverRow", fila)
        for numero in (anterior, fila):
            if numero >= 0:
                self._repintar(numero)

    def _repintar(self, fila: int) -> None:
        modelo = self._view.model()
        if modelo is None or fila >= modelo.rowCount():
            return
        viewport = self._view.viewport()
        primera = self._view.visualRect(modelo.index(fila, 0))
        ultima = self._view.visualRect(
            modelo.index(fila, max(0, modelo.columnCount() - 1))
        )
        viewport.update(
            primera.united(ultima).adjusted(0, 0, viewport.width(), 0)
        )


class FlatSelectionDelegate(QStyledItemDelegate):
    """Pinta la fila seleccionada como una sola banda azul.

    El estilo nativo de Windows 11 dibuja la selección como un rectángulo
    redondeado *por celda*, con margen a los lados: una fila seleccionada se
    veía como una hilera de cuadros azules sueltos en vez de una selección
    continua. Aquí se le quita a la celda el estado de selección y el azul se
    pasa como fondo del ítem, que el estilo rellena a ras del rectángulo, tal
    como ya rellena los colores de estado de la tabla. La fila queda entonces
    como una banda de un solo color, sin esquinas redondas ni huecos.

    Quien decide si hay que pintar es la selección de la vista, no el estado
    que llegue en la celda. En una tabla que selecciona por filas las dos
    cosas deberían coincidir, pero basta que una celda no reciba ese estado
    (porque no tiene ítem, porque el estilo la trata aparte) para que la
    banda salga cortada justo ahí.
    """

    def _fila_seleccionada(self, index) -> bool:
        vista = self.parent()
        seleccion = getattr(vista, "selectionModel", None)
        if not callable(seleccion):
            return False
        modelo = seleccion()
        if modelo is None or vista.model() is not index.model():
            return False
        if (
            vista.selectionBehavior()
            is QAbstractItemView.SelectionBehavior.SelectItems
        ):
            return modelo.isSelected(index)
        return modelo.isRowSelected(index.row(), index.parent())

    def _fila_marcada(self, index) -> bool:
        """Si la fila lleva marcada su casilla, mire donde mire el cursor.

        La casilla vive en una sola columna, así que la vista dice en cuál
        con la propiedad ``checkColumn``; sin ella (una tabla sin casillas)
        no hay nada que pintar.
        """
        vista = self.parent()
        columna = vista.property("checkColumn") if vista is not None else None
        if columna is None or int(columna) < 0:
            return False
        modelo = index.model()
        if modelo is None or int(columna) >= modelo.columnCount():
            return False
        estado = modelo.index(index.row(), int(columna), index.parent()).data(
            Qt.ItemDataRole.CheckStateRole
        )
        return Qt.CheckState(estado) is Qt.CheckState.Checked if estado is not None else False

    def _fila_bajo_el_cursor(self, index) -> bool:
        """Si el cursor está sobre esta fila, aunque no sobre esta celda."""
        vista = self.parent()
        if vista is None:
            return False
        fila = vista.property("hoverRow")
        return fila is not None and int(fila) == index.row()

    def initStyleOption(self, option, index) -> None:  # noqa: N802 - API Qt
        super().initStyleOption(option, index)
        seleccionada = (
            option.state & QStyle.StateFlag.State_Selected
            or self._fila_seleccionada(index)
        )
        if not seleccionada:
            # El estilo nativo pinta el hover celda a celda, que es lo que
            # dejaba un solo cuadro azul bajo el cursor en vez de la fila
            # entera. Se le quita el estado y lo pinta esta clase, que sí
            # sabe de filas.
            option.state &= ~QStyle.StateFlag.State_MouseOver
            if self._fila_marcada(index):
                # Marcada es una decisión, como la selección: el color de
                # estado de la celda cede y la fila queda de un solo tono.
                option.backgroundBrush = QBrush(QColor(checked_row_color()))
                self._texto_claro(option)
                return
            if self._fila_bajo_el_cursor(index):
                option.backgroundBrush = QBrush(
                    _mezclado(option.backgroundBrush, hover_row_color())
                )
            return
        option.state &= ~QStyle.StateFlag.State_Selected
        option.backgroundBrush = QBrush(QColor(accent_color()))
        self._texto_claro(option)

    @staticmethod
    def _texto_claro(option) -> None:
        """Escribe el texto en blanco sobre las bandas de color.

        Sin el estado de selección, el estilo usaría el color normal de la
        tabla, y una fila que ya trae su propio color de letra (el verde de
        lo hecho, el gris de lo que no existe) se perdería sobre el azul.
        """
        palette = option.palette
        for role in (
            QPalette.ColorRole.Text,
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.HighlightedText,
        ):
            palette.setColor(role, QColor(paleta().TABLE_TEXT))
        option.palette = palette


class _RoundedCornerClip(QObject):
    """Recorta un widget a esquinas redondas por fuera, con una máscara.

    ``border-radius`` en la hoja de estilo solo redondea el fondo/borde que
    pinta el propio widget: una tabla dibuja las filas directamente sobre su
    viewport (no como hijos), así que ese contenido llega en escuadra hasta
    el borde y tapa el radio de las esquinas de abajo aunque las de arriba
    se vean bien (las pinta la cabecera, que sí respeta el radio). La única
    forma de que las cuatro esquinas queden iguales pase lo que pinte
    adentro es recortar el widget entero desde fuera, con una máscara que se
    recalcula cada vez que cambia de tamaño.
    """

    def __init__(self, radius: int, parent: QWidget) -> None:
        super().__init__(parent)
        self._radius = radius

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - API Qt
        if event.type() == QEvent.Type.Resize:
            self._apply(watched)
        return False

    def _apply(self, widget: QWidget) -> None:
        if widget.width() <= 0 or widget.height() <= 0:
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(widget.rect()), self._radius, self._radius)
        widget.setMask(QRegion(path.toFillPolygon().toPolygon()))


def round_corners(widget: QWidget, radius: int = TABLE_RADIUS) -> None:
    """Mantiene ``widget`` recortado a esquinas redondas mientras cambia de tamaño."""
    widget.installEventFilter(_RoundedCornerClip(radius, widget))
    if widget.width() > 0 and widget.height() > 0:
        path = QPainterPath()
        path.addRoundedRect(QRectF(widget.rect()), radius, radius)
        widget.setMask(QRegion(path.toFillPolygon().toPolygon()))


def _paleta_data_table(table: QAbstractItemView) -> None:
    """Los roles de color de la tabla, con el tema puesto ahora."""
    palette = table.palette()
    c = paleta()
    palette.setColor(QPalette.ColorRole.Base, QColor(c.TABLE_BASE_BG))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(c.TABLE_ALTERNATE_BG))
    # Sin el rol de texto, Qt seguiría escribiendo en negro sobre el gris.
    palette.setColor(QPalette.ColorRole.Text, QColor(c.TABLE_TEXT))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c.TABLE_TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(accent_color()))
    palette.setColor(
        QPalette.ColorRole.HighlightedText, QColor(on_accent_text())
    )
    table.setPalette(palette)
    # El viewport usa el rol ``Base``; sin autorrelleno conserva el blanco que
    # el estilo nativo pinta bajo las filas y en el área sobrante.
    viewport = table.viewport()
    viewport.setPalette(palette)
    viewport.setAutoFillBackground(True)


def style_data_table(table: QAbstractItemView) -> None:
    """Deja la tabla en las superficies de la aplicación.

    La hoja de estilo por sí sola no basta: el estilo nativo de Windows pinta
    el viewport y las filas desde la paleta, así que se fijan también los
    roles de color. Se aplican a *todos* los grupos (``setColor`` sin grupo
    cubre Active, Inactive y Disabled) para que la tabla no cambie de fondo
    al perder el foco ni mientras está deshabilitada durante el procesamiento.

    Solo la paleta se vuelve a poner al cambiar de tema. El delegado, el
    resaltado de fila y el recorte de esquinas se instalan una vez: no
    dependen del tema y volver a montarlos apilaría filtros de evento.
    """
    table.setAlternatingRowColors(True)
    al_cambiar_tema(table, lambda: _paleta_data_table(table))
    table.setItemDelegate(FlatSelectionDelegate(table))
    enable_row_hover(table)
    round_corners(table)


def enable_row_hover(table: QAbstractItemView) -> None:
    """Resalta la fila entera bajo el cursor, no solo la celda."""
    table.setMouseTracking(True)
    viewport = table.viewport()
    viewport.setMouseTracking(True)
    # Solo el viewport: es quien recibe el movimiento del ratón sobre las
    # filas, y sus coordenadas son las que entiende ``indexAt``. Las del
    # widget entero llevan encima la cabecera y señalarían otra fila.
    viewport.installEventFilter(_HoverRowTracker(table))


class _HeaderScrollbarAligner(QObject):
    """Mantiene la barra vertical debajo de la cabecera de una tabla."""

    def __init__(self, table: QAbstractItemView) -> None:
        super().__init__(table)
        self._table = table

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - API Qt
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._aplicar(watched.height())
        return False

    def _aplicar(self, alto: int) -> None:
        self._table.verticalScrollBar().setStyleSheet(
            f"QScrollBar:vertical {{ margin-top: {max(0, alto)}px; }}"
        )


# Filas que se miran al medir el ancho de una columna. Sin tope Qt las
# recorre todas, y con cuatrocientas filas eso ya es una décima de segundo
# de espera para averiguar un ancho que deciden las primeras pantallas.
RESIZE_PRECISION = 64


def size_columns_once(table, stretch_last: bool = False) -> None:
    """Ajusta las columnas al contenido una vez y las deja fijas.

    ``ResizeToContents`` no mide una vez: vuelve a medir la columna entera
    cada vez que cambia una celda. Llenar una tabla de cuatrocientas filas
    por siete columnas, u ordenarla (que reubica esas dos mil ochocientas
    celdas), pasa entonces a costar el cuadrado de las filas: ordenar la
    lista de bitácoras de un batch tardaba tres minutos y medio.

    Medir una vez y pasar a ``Interactive`` da el mismo ancho de partida y
    además deja arrastrar el borde de la columna, que con el modo por
    contenido no se podía. La última columna puede quedarse en ``Stretch``
    para que no sobre espacio a la derecha: ese modo no mide contenido, así
    que no cuesta nada.
    """
    header = table.horizontalHeader()
    ultima = table.columnCount() - 1
    for column in range(table.columnCount()):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
    header.setResizeContentsPrecision(RESIZE_PRECISION)
    table.resizeColumnsToContents()
    # Qt mide el texto, pero no siempre suma el relleno horizontal de la
    # sección definido en DATA_TABLE_QSS. En columnas cortas como «Páginas»
    # eso escondía la primera letra aunque hubiera ancho disponible.
    model = table.model()
    for column in range(table.columnCount()):
        label = model.headerData(
            column,
            Qt.Orientation.Horizontal,
            Qt.ItemDataRole.DisplayRole,
        )
        if label:
            minimum = (
                header.fontMetrics().horizontalAdvance(str(label))
                + 2 * SPACE_S
                + 2
            )
            table.setColumnWidth(
                column, max(table.columnWidth(column), minimum)
            )
    if stretch_last and ultima >= 0:
        header.setSectionResizeMode(ultima, QHeaderView.ResizeMode.Stretch)


def align_vertical_scrollbar_to_header(table: QAbstractItemView) -> None:
    """Hace que la barra vertical empiece donde termina la cabecera."""
    cabecera = table.horizontalHeader()
    filtro = _HeaderScrollbarAligner(table)
    cabecera.installEventFilter(filtro)
    filtro._aplicar(cabecera.height())


def configure_combo_box(combo: QComboBox, minimum_contents: int = 16) -> None:
    """Aplica el comportamiento compacto de un desplegable de Windows."""
    combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    combo.setMinimumContentsLength(minimum_contents)
    combo.setMaxVisibleItems(12)
    view = combo.view()
    view.setTextElideMode(Qt.TextElideMode.ElideRight)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setUniformItemSizes(True)


def configure_menu_button(
    button: QToolButton,
    menu,
    *,
    split: bool = False,
) -> None:
    """Configura un botón de menú o un botón dividido al estilo de Windows."""
    button.setMenu(menu)
    button.setPopupMode(
        QToolButton.ToolButtonPopupMode.MenuButtonPopup
        if split
        else QToolButton.ToolButtonPopupMode.InstantPopup
    )
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    button.setProperty("menuRole", "split" if split else "dropdown")


def _paleta_pane(pane: QWidget) -> None:
    """Los roles de color del panel, con el tema puesto ahora."""
    c = paleta()
    palette = pane.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(c.PANE_BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c.PANE_TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(c.PANE_CONTROL_BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(c.PANE_TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(c.PANE_CONTROL_BG))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(c.PANE_TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(accent_color()))
    palette.setColor(
        QPalette.ColorRole.HighlightedText, QColor(on_accent_text())
    )
    pane.setPalette(palette)
    pane.setAutoFillBackground(True)


def style_dark_pane(pane: QWidget) -> None:
    """Deja un panel completo en la superficie del tema, con sus controles.

    Los hijos heredan la paleta del padre, así que fijarla aquí cubre de una
    vez las etiquetas, las flechas de los ``QToolButton`` y el texto de los
    campos: todos ellos se pintan desde roles de paleta, no desde la hoja de
    estilo, y sin esto quedarían en el color que decida el estilo nativo.
    """
    al_cambiar_tema(pane, lambda: _paleta_pane(pane))


def _paleta_pdf_surface(scroll: QScrollArea) -> None:
    """Los roles de color de la superficie del PDF, con el tema de ahora."""
    c = paleta()
    palette = scroll.palette()
    palette.setColor(QPalette.ColorRole.Base, QColor(c.PANE_SURFACE_BG))
    palette.setColor(QPalette.ColorRole.Window, QColor(c.PANE_SURFACE_BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(c.PANE_TEXT))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c.PANE_TEXT))
    scroll.setPalette(palette)
    viewport = scroll.viewport()
    viewport.setPalette(palette)
    viewport.setAutoFillBackground(True)


def style_pdf_surface(scroll: QScrollArea) -> None:
    """Pinta el área que rodea a la página del PDF con la superficie del tema.

    El fondo que se ve detrás de la página no es del panel sino del *viewport*
    del área de desplazamiento, que Qt pinta desde el rol ``Base`` y que
    ninguna regla sobre el ``QScrollArea`` alcanza. Es el mismo motivo por el
    que la tabla se quedaba en blanco (ver ``style_data_table``).
    """
    al_cambiar_tema(scroll, lambda: _paleta_pdf_surface(scroll))


def load_zoom_icon(name: str) -> QIcon:
    """Un icono de zoom local, pintado del color del texto del tema.

    Local para que los visores se vean igual en Windows, que no trae tema de
    iconos. Tenido porque el dibujo viene en blanco: sobre la superficie clara
    del tema claro no se veia nada, solo el hueco del boton.
    """
    path = _ASSETS / f"zoom_{name}.svg"
    if not path.is_file():
        return QIcon.fromTheme(f"zoom-{name}")
    return load_icon(f"zoom_{name}", paleta().TEXT)


# Los iconos de los botones acompañan al texto: van al alto de una letra, la
# misma medida que ya usan los controles de zoom, para que la fila de botones
# no crezca ni el dibujo pese más que la palabra.
ICON_SIZE = QSize(14, 14)
# Tamaños que se guardan del dibujo: el del botón y el doble, para que se vea
# igual de limpio en una pantalla al 200 %.
_ICON_RENDER_SIZES = (ICON_SIZE.width(), ICON_SIZE.width() * 2)


def load_icon(name: str, color: QColor | str | None = None) -> QIcon:
    """Carga un icono de ``assets/`` por su nombre, sin extensión.

    Los iconos son locales por la misma razón que los del zoom: el tema de
    iconos del sistema no existe en Windows y dejar el botón sin dibujo según
    la máquina es peor que no ponerlo.

    Con ``color`` el dibujo se pinta de ese color entero. Los botones normales
    los pinta el estilo de Windows, que en tema claro los da con texto negro
    y en tema oscuro con texto blanco: un icono de color fijo se pierde en uno
    de los dos. Pintado del color del texto del botón, se lee en ambos y
    pertenece al botón en vez de estar pegado encima.
    """
    path = _ASSETS / f"{name}.svg"
    if not path.is_file():
        return QIcon()
    icon = QIcon(str(path))
    if color is None:
        return icon
    tinted = QIcon()
    for size in _ICON_RENDER_SIZES:
        pixmap = icon.pixmap(QSize(size, size))
        painter = QPainter(pixmap)
        # SourceIn conserva la transparencia del dibujo y sustituye el color:
        # el trazo queda del color pedido y los bordes suavizados se
        # mantienen, sin el recuadro que dejaría rellenar sin más.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(color))
        painter.end()
        tinted.addPixmap(pixmap)
    return tinted


class ZoomOverlay(QFrame):
    """Píldora de zoom que flota sobre la página.

    La comparten la vista previa de la ventana principal, el visor de PDF del
    visor de CSV y el lienzo del editor de plantillas: es el mismo control,
    con los mismos tamaños y el mismo orden. Cada ventana aporta únicamente
    los textos de sus acciones, que hablan de la vista previa, de la página o
    del lienzo según dónde esté.

    Va tumbada y no de pie. De pie ocupaba una columna a media altura, que es
    justo por donde se lee un escaneo vertical, y su alto marcaba el mínimo
    del panel; tumbada se apoya en el borde de abajo, donde no tapa texto, y
    pide ancho, que es lo que sobra. El «Zoom» que la encabezaba se cae con
    el cambio: el porcentaje ya dice de qué va, y en horizontal era una
    palabra más entre el borde y el primer botón.
    """

    def __init__(self, zoom_in, fit, zoom_out, parent: QWidget | None = None) -> None:
        """Cada acción es ``(tooltip, nombre accesible, función)``."""
        super().__init__(parent)
        self.setObjectName("zoomOverlay")
        panel = QHBoxLayout(self)
        panel.setContentsMargins(6, 5, 6, 5)
        panel.setSpacing(2)

        # De menor a mayor, como cualquier control tumbado: alejar a la
        # izquierda, acercar a la derecha y el ajuste en medio. Los
        # argumentos siguen llegando en el orden de siempre para no cambiar
        # la llamada de las tres ventanas.
        self.btn_out = self._button("out", zoom_out, panel)
        self.btn_fit = self._button("fit", fit, panel)
        self.btn_in = self._button("in", zoom_in, panel)
        # El dibujo lleva el color dentro, no en la hoja, asi que hay que
        # volver a pedirlo al cambiar de tema.
        al_cambiar_tema(self, self._tenir_iconos)

        self.value_label = QLabel("100%")
        self.value_label.setObjectName("zoomValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel.addWidget(self.value_label, 0, Qt.AlignmentFlag.AlignVCenter)

    def _tenir_iconos(self) -> None:
        """Vuelve a pintar los tres dibujos con el color del tema de ahora."""
        for nombre, boton in (
            ("out", self.btn_out),
            ("fit", self.btn_fit),
            ("in", self.btn_in),
        ):
            boton.setIcon(load_zoom_icon(nombre))

    def _button(self, icon: str, action, panel: QHBoxLayout) -> QToolButton:
        tooltip, accessible, slot = action
        button = QToolButton()
        button.setObjectName("zoomControl")
        button.setIcon(load_zoom_icon(icon))
        button.setIconSize(QSize(14, 14))
        button.setFixedSize(28, 28)
        button.setToolTip(tooltip)
        button.setAccessibleName(accessible)
        button.clicked.connect(slot)
        panel.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        return button


class _OverlayFitWatcher(QObject):
    """Esconde un control flotante en cuanto su hueco deja de darle."""

    def __init__(self, holder: QWidget) -> None:
        super().__init__(holder)
        self._holder = holder

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - API Qt
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.aplicar(watched.size())
        return False

    def aplicar(self, hueco: QSize) -> None:
        # Las dos medidas, no solo el alto: tumbado, lo que se le queda
        # corto a un flotante es el ancho.
        pedido = self._holder.sizeHint()
        cabe = (
            hueco.width() >= pedido.width()
            and hueco.height() >= pedido.height()
        )
        if self._holder.isVisible() != cabe:
            self._holder.setVisible(cabe)


def hide_overlay_when_tight(holder: QWidget) -> None:
    """El recuadro flotante se esconde si su marco no da para dibujarlo.

    Un flotante no manda sobre el mínimo del panel que lo lleva debajo (si
    lo hiciera, un control de zoom decidiría cuánto mide de mínimo la
    ventana entera), así que puede tocarle un hueco más pequeño que él.
    Metido a la fuerza, sus botones de tamaño fijo se montan unos sobre
    otros. O cabe entero o no se enseña.
    """
    marco = holder.parentWidget()
    if marco is None:
        return
    vigilante = _OverlayFitWatcher(holder)
    marco.installEventFilter(vigilante)
    vigilante.aplicar(marco.size())


class _OverlayScrollbarWatcher(QObject):
    """Aparta un control flotante de las barras del panel que lo lleva."""

    def __init__(self, holder: QWidget, panel: QWidget, base) -> None:
        super().__init__(holder)
        self._holder = holder
        self._panel = panel
        self._base = base

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - API Qt
        if event.type() in (
            QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize
        ):
            self.aplicar()
        return False

    def aplicar(self) -> None:
        layout = self._holder.layout()
        if layout is None:
            return
        izquierda, arriba, derecha, abajo = self._base
        vertical = self._panel.verticalScrollBar()
        horizontal = self._panel.horizontalScrollBar()
        if vertical is not None and vertical.isVisible():
            # El flotante va centrado, así que el hueco de la barra se
            # descuenta por su lado: el centro se corre a la izquierda
            # justo la mitad, que es donde queda el centro de la página.
            derecha += vertical.width()
        if horizontal is not None and horizontal.isVisible():
            abajo += horizontal.height()
        if layout.contentsMargins().right() == derecha and (
            layout.contentsMargins().bottom() == abajo
        ):
            return
        layout.setContentsMargins(izquierda, arriba, derecha, abajo)


def keep_overlay_clear_of_scrollbars(holder: QWidget, panel: QWidget) -> None:
    """El recuadro flotante se aparta de las barras de desplazamiento.

    Va abajo y centrado sobre el panel entero, y el panel entero incluye
    sus barras: en cuanto la página se acerca lo suficiente para que
    aparezca la de abajo, el recuadro se sienta encima de ella y no se
    puede arrastrar. Es el único control de la ventana que tapa a otro,
    porque es el único que flota.

    En vez de moverlo a una esquina se le suma al margen lo que mide cada
    barra visible. Así conserva el sitio que tiene (abajo y centrado sobre
    la página, no sobre el marco) y se corre solo lo justo, que además es
    lo que hace que siga centrado sobre lo que se está mirando.
    """
    margenes = holder.layout().contentsMargins() if holder.layout() else None
    if margenes is None:
        return
    vigilante = _OverlayScrollbarWatcher(
        holder,
        panel,
        (
            margenes.left(), margenes.top(),
            margenes.right(), margenes.bottom(),
        ),
    )
    for barra in (panel.verticalScrollBar(), panel.horizontalScrollBar()):
        if barra is not None:
            barra.installEventFilter(vigilante)
    vigilante.aplicar()


class ZoomableScrollArea(QScrollArea):
    """QScrollArea con zoom por Ctrl + rueda del ratón."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._zoom_callback = None

    def set_zoom_callback(self, callback) -> None:
        self._zoom_callback = callback

    def wheelEvent(self, event) -> None:
        if (event.modifiers() & Qt.KeyboardModifier.ControlModifier
                and self._zoom_callback is not None):
            delta = event.angleDelta().y()
            self._zoom_callback(1.25 if delta > 0 else 0.8)
            event.accept()
            return
        super().wheelEvent(event)


class ElidedLabel(QLabel):
    """Etiqueta informativa que se recorta con «…» en vez de ensanchar.

    Una ``QLabel`` normal pide de ancho mínimo todo su texto, así que cada
    frase larga (la estimación de tiempo, el reparto de hilos, el estado del
    procesamiento) se convertía en ancho mínimo de la ventana, y encima uno
    que crecía en marcha en cuanto se escribía un mensaje más largo que el
    anterior. Aquí el texto se pinta recortado a lo que haya de sitio y queda
    entero en el tooltip, igual que ya hacen los nombres de archivo del panel
    de avance.

    ``text()`` sigue devolviendo el texto completo, no el recortado: el
    recorte es cosa de cómo se ve la etiqueta, no de lo que dice.
    """

    # Con menos que esto el recorte no deja ni una palabra y solo se ve «…».
    MIN_ELIDED_WIDTH = 60
    # Holgura del ancho natural, para que la última letra no roce el borde.
    _TEXT_PADDING = 4

    def __init__(
        self,
        text: str = "",
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._elide_mode = mode
        # El tooltip automático es el texto completo, pero solo mientras nadie
        # ponga uno propio: varias de estas etiquetas llevan una explicación
        # que no se puede perder al escribirles el valor.
        self._custom_tooltip = False
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - API Qt
        self._full_text = text or ""
        if not self._custom_tooltip:
            QLabel.setToolTip(self, self._full_text)
        self._apply_elide()

    def text(self) -> str:
        return self._full_text

    def fullTextForCopy(self) -> str:  # noqa: N802 - API Qt
        """Texto sin el recorte visual, para copiar mensajes completos."""
        return self._full_text

    def setToolTip(self, text: str) -> None:  # noqa: N802 - API Qt
        self._custom_tooltip = bool(text)
        QLabel.setToolTip(self, text)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - API Qt
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), self.MIN_ELIDED_WIDTH), hint.height())

    def sizeHint(self) -> QSize:  # noqa: N802 - API Qt
        # Sobre el texto ya recortado el alto natural encogería y la etiqueta
        # no volvería a estirarse al ensanchar la ventana.
        hint = super().sizeHint()
        natural = self.fontMetrics().horizontalAdvance(self._full_text)
        return QSize(max(hint.width(), natural + self._TEXT_PADDING), hint.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = self.width()
        if width <= 0:
            # Antes del primer reparto no hay ancho contra el que recortar; se
            # deja entero y el ``resizeEvent`` que llega después lo ajusta.
            QLabel.setText(self, self._full_text)
            return
        QLabel.setText(
            self,
            self.fontMetrics().elidedText(self._full_text, self._elide_mode, width),
        )
