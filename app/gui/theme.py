"""El tema de la aplicacion: paleta, controles y marco de Windows.

Hay dos, claro y oscuro, y los dos salen de ``tokens.Paleta``. Este modulo es
quien los instala y, sobre todo, quien sabe cambiarlos con la aplicacion ya
abierta, que es la parte que no es evidente: una hoja de estilo de Qt se
resuelve al aplicarla, asi que cambiar la paleta no repinta nada por si solo.
Hacen falta tres cosas, y en este orden:

1. La paleta y la hoja de la aplicacion, que cubren a todo widget que no
   tenga hoja propia.
2. Las hojas propias de cada ventana. No se pueden rehacer desde aqui porque
   cada una compone la suya con su fragmento de densidad, asi que las pide
   con la senal ``cambiado``, a la que cada ventana se suscribe con el metodo
   que ya usaba para armarla.
3. El marco de la ventana, que no lo dibuja Qt sino DWM, y las etiquetas que
   llevan su color en una hoja de una linea.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QLayout, QMainWindow

from app.gui.tokens import (
    FONT_BODY_PT,
    TEMA_CLARO,
    TEMA_OSCURO,
    TEMA_POR_OMISION,
    TEMAS,
    accent_color,
    on_accent_text,
    otro_tema,
    paleta,
    set_tema,
    tema,
)
from app.gui.widgets import (
    _APPLICATION_THEME_PROPERTY,
    accent_button_qss,
    app_chrome_qss,
    repintar_del_tema,
)
from app.utils.app_identity import set_windows_native_window_style
from app.utils.preferencias_ui import guardar_tema, leer_tema

# Los nombres que se leen en la interfaz. El menu ofrece el que no esta
# puesto, asi que la frase se arma con el nombre del otro tema.
NOMBRE_TEMA = {TEMA_OSCURO: "oscuro", TEMA_CLARO: "claro"}


class _Tema(QObject):
    """Avisa a las ventanas abiertas de que el tema cambio.

    Va por senal y no por una lista de ventanas porque asi Qt se encarga de
    la parte tediosa: una ventana que se cierra deja de recibir el aviso sin
    que nadie tenga que acordarse de darla de baja.
    """

    cambiado = Signal(str)


_GESTOR: _Tema | None = None


def gestor_tema() -> _Tema:
    """El emisor unico al que se suscriben las ventanas."""
    global _GESTOR
    if _GESTOR is None:
        _GESTOR = _Tema()
    return _GESTOR


class _NativeWindowTheme(QObject):
    """Aplica DWM tambien a dialogos y ventanas creados mas adelante."""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - API Qt
        if (
            isinstance(watched, (QMainWindow, QDialog))
            and event.type() == QEvent.Type.Show
            and watched.isWindow()
        ):
            set_windows_native_window_style(watched, tema() == TEMA_OSCURO)
        return False


def _application_font() -> QFont:
    families = set(QFontDatabase.families())
    family = (
        "Segoe UI"
        if "Segoe UI" in families
        else "Segoe UI Variable Text"
    )
    return QFont(family, FONT_BODY_PT)


def _palette() -> QPalette:
    """La paleta de Qt con los tonos del tema puesto ahora.

    Es lo que leen los widgets que no pasan por la hoja de estilo, y tambien
    lo que leen las propias hojas cuando piden ``palette(highlight)``: por ahi
    es por donde el acento del sistema llega a los controles sin tener que
    reconstruir la hoja cada vez que cambia.
    """
    palette = QPalette()
    # El acento sale de Windows, no de un azul escrito aqui, y no depende del
    # tema: el mismo en claro y en oscuro, porque es del escritorio.
    acento = accent_color()
    c = paleta()
    colors = {
        QPalette.ColorRole.Window: c.PANE_SURFACE_BG,
        QPalette.ColorRole.WindowText: c.PANE_TEXT,
        QPalette.ColorRole.Base: c.PANE_BG,
        QPalette.ColorRole.AlternateBase: c.TABLE_ALTERNATE_BG,
        QPalette.ColorRole.ToolTipBase: c.PANE_CONTROL_BG,
        QPalette.ColorRole.ToolTipText: c.PANE_TEXT,
        QPalette.ColorRole.Text: c.PANE_TEXT,
        QPalette.ColorRole.Button: c.PANE_CONTROL_BG,
        QPalette.ColorRole.ButtonText: c.PANE_TEXT,
        QPalette.ColorRole.BrightText: c.PANE_TEXT,
        QPalette.ColorRole.Highlight: acento,
        QPalette.ColorRole.Accent: acento,
        # Encima del acento no va el texto del tema sino el que se lea sobre
        # el acento: con uno claro, el blanco de siempre desaparecia.
        QPalette.ColorRole.HighlightedText: on_accent_text(acento),
        QPalette.ColorRole.Link: acento,
        QPalette.ColorRole.PlaceholderText: c.TEXT_TERTIARY,
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.PlaceholderText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(c.TEXT_DISABLED))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button,
        QColor(c.CONTROL_DISABLED),
    )
    return palette


def _esquema_nativo(app: QApplication) -> None:
    """Actualiza el esquema que Windows usa para los controles nativos."""
    oscuro = tema() == TEMA_OSCURO
    try:
        app.styleHints().setColorScheme(
            Qt.ColorScheme.Dark if oscuro else Qt.ColorScheme.Light
        )
    except AttributeError:
        pass


def _instalar(app: QApplication) -> None:
    """Paleta y hoja de la aplicacion, con el tema de ahora."""
    app.setPalette(_palette())
    # El fragmento del boton de acento va despues de la paleta, no antes: lee
    # el acento ya instalado para sacar de el el tono del cursor y el del
    # pulsado, que es lo unico que ``palette(highlight)`` no sabe dar.
    hoja = app_chrome_qss() + accent_button_qss()
    if not app.styleSheet():
        app.setStyleSheet(hoja)
        return
    # El repulido global de QSS recorre los widgets cacheados y vuelve a
    # recorrer sus descendientes: los controles profundos se repiten varias
    # veces. Invalidar primero el estilo de la aplicacion vacia esa cache;
    # despues se repule cada widget una sola vez, incluidas sus hojas locales.
    app.style().unpolish(app)
    app.setStyleSheet(hoja)
    app.style().polish(app)
    for widget in app.allWidgets():
        widget.style().polish(widget)
        QApplication.sendEvent(widget, QEvent(QEvent.Type.StyleChange))


def install_application_theme(app: QApplication) -> None:
    """Instala tipografia, paleta, controles y marcos para toda la GUI.

    Arranca con el tema que el usuario dejo elegido la ultima vez. Si no hay
    ninguno guardado vale el de siempre, el oscuro.
    """
    set_tema(leer_tema() or TEMA_POR_OMISION)
    app.setFont(_application_font())
    _esquema_nativo(app)
    _instalar(app)
    app.setProperty(_APPLICATION_THEME_PROPERTY, True)
    native_theme = _NativeWindowTheme(app)
    app.installEventFilter(native_theme)
    app._bits_native_window_theme = native_theme


def aplicar_tema(nombre: str, app: QApplication | None = None) -> bool:
    """Cambia el tema con la aplicacion abierta y repinta lo que ya se ve.

    Devuelve si hubo cambio. Pedir el tema que ya esta puesto no repinta nada.
    """
    if nombre not in TEMAS:
        return False
    app = app or QApplication.instance()
    if app is None or not set_tema(nombre):
        return False

    ventanas = [
        ventana for ventana in app.topLevelWidgets() if ventana.isWindow()
    ]
    estados_actualizacion = [
        (ventana, ventana.updatesEnabled()) for ventana in ventanas
    ]
    # QSS retira y repone fuentes y margenes mientras se sustituye. Bloquear
    # solo la pintura deja que el layout mida esos estados transitorios y
    # publique el ajuste final despues, cuando ya se esta pintando otra vez.
    layouts = [
        layout
        for ventana in ventanas
        for layout in ventana.findChildren(QLayout)
        if layout.isEnabled()
    ]
    for ventana, habilitada in estados_actualizacion:
        if habilitada:
            ventana.setUpdatesEnabled(False)
    for layout in layouts:
        layout.setEnabled(False)
    try:
        _instalar(app)
        # Las hojas propias de cada ventana, las etiquetas con hoja de una
        # linea y la barra de titulo, que es de DWM y no de Qt.
        gestor_tema().cambiado.emit(nombre)
        repintar_del_tema(app)
    finally:
        # Primero se habilita el arbol completo y despues se resuelven sus
        # medidas definitivas, todavia sin pintar ni despachar entrada o timers.
        for layout in layouts:
            layout.setEnabled(True)
            layout.invalidate()
        for layout in layouts:
            layout.activate()
        # Windows dibuja el titulo por separado. Se cambia cuando el cliente
        # ya esta preparado, no al comenzar el trabajo costoso de QSS.
        _esquema_nativo(app)
        oscuro = nombre == TEMA_OSCURO
        for ventana in ventanas:
            if ventana.isVisible() and isinstance(ventana, (QMainWindow, QDialog)):
                set_windows_native_window_style(ventana, oscuro)
        # La paleta, las hojas y los iconos se preparan sin pintar cada paso.
        # Al reactivar las ventanas aparece directamente el tema completo y
        # no se ve el layout pasar por posiciones intermedias.
        for ventana, habilitada in estados_actualizacion:
            ventana.setUpdatesEnabled(habilitada)
            if habilitada and ventana.isVisible():
                ventana.repaint()

    guardar_tema(nombre)
    return True


def alternar_tema(app: QApplication | None = None) -> str:
    """Pasa del tema puesto al otro y devuelve el que quedo."""
    destino = otro_tema()
    aplicar_tema(destino, app)
    return tema()
