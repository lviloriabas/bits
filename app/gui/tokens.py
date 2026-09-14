"""Medidas, tipografía y colores de la aplicación, en un solo sitio.

Antes cada hoja de estilo traía sus propios números y sus propios grises: la
ventana llegó a tener siete alturas distintas para controles que van en la
misma fila y veinticinco colores escritos a mano, de dos familias que no
pegaban entre sí (los de GitHub y los de Windows). Nada de eso se ve leyendo
un archivo suelto, porque el desajuste solo aparece cuando dos piezas caen
juntas en pantalla.

Aquí viven los valores y, sobre todo, las **relaciones** entre ellos: el alto
de caja sale del alto de control menos el borde, el relleno derecho del botón
dividido sale del izquierdo más la celda de la flecha. Escritas como cuentas y
no como números sueltos, no se pueden desincronizar sin que se note.

Los colores viven en ``Paleta``, que existe en dos versiones (``OSCURA`` y
``CLARA``) y se consulta con ``paleta()`` en vez de importarse suelta: es lo
que permite cambiar de tema sin reiniciar. Las medidas no cambian con el
tema y siguen siendo constantes de módulo.

El acento no está aquí: lo pone Windows. Ver ``accent_color``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from PySide6.QtGui import QColor, QGuiApplication, QPalette

# --------------------------------------------------------------------------
# Espaciado. La escala de Fluent, en múltiplos de 4. Cualquier hueco de la
# ventana tiene que salir de aquí; los números intermedios (5, 7, 9, 13…) son
# los que hacen que dos bloques parezcan mal alineados sin saber por qué.
# --------------------------------------------------------------------------
SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24

# --------------------------------------------------------------------------
# Alto de los controles. 32 px es la medida de WinUI y la que usa el panel de
# ajustes de Windows: campos, botones, desplegables y casillas comparten fila
# y tienen que compartir alto. La compacta baja a 28 para las pantallas de
# 1366x768, que es donde la ventana no cabe entera.
#
# El alto que se escribe en la hoja es el de la caja de contenido, así que hay
# que descontar el borde: Qt suma después el marco por fuera. Con el relleno
# vertical a cero la cuenta es esta y no hay que repetirla en cada regla.
# --------------------------------------------------------------------------
BORDER = 1
CONTROL_HEIGHT = 32
CONTROL_HEIGHT_COMPACT = 28
CONTROL_BOX_H = CONTROL_HEIGHT - 2 * BORDER
CONTROL_BOX_H_COMPACT = CONTROL_HEIGHT_COMPACT - 2 * BORDER

# Relleno horizontal de los controles con texto.
CONTROL_PAD_H = 12
CONTROL_PAD_H_COMPACT = 8

# Radio de esquina. Una sola medida mantiene coherentes controles, tablas y
# superficies sin introducir curvas distintas dentro de la misma ventana.
RADIUS_CONTROL = 6
RADIUS_CARD = 6

# --------------------------------------------------------------------------
# Tipografía. Cuatro papeles y ni uno más, todos en puntos.
#
# La unidad importa más que el tamaño: ``pt`` sigue el escalado de Windows y
# el cuerpo de letra que el usuario haya elegido, ``px`` no. La ventana tenía
# los dos mezclados, así que en un monitor al 150 % el panel de tiempos se
# encogía respecto a todo lo demás en lugar de crecer con ello.
# --------------------------------------------------------------------------
FONT_FAMILY = '"Segoe UI Variable Text", "Segoe UI", sans-serif'
FONT_BODY_PT = 10
FONT_CAPTION_PT = 9
FONT_SUBTITLE_PT = 14
WEIGHT_REGULAR = 400
WEIGHT_STRONG = 600

# --------------------------------------------------------------------------
# La paleta, en sus dos versiones. Los papeles son los mismos en las dos (el
# fondo de la ventana, la tarjeta que se apoya en el, el control que se apoya
# en la tarjeta) y lo que cambia es el tono con que se resuelve cada uno. En
# el tema oscuro la escalera sube: cada superficie mas cercana es un gris mas
# claro. En el claro baja: la mas honda es la mas gris y la mas cercana es el
# blanco. Escrito asi, una regla que diga «el control va sobre la tarjeta»
# sigue diciendo lo mismo en los dos temas sin tener que reescribirla.
#
# Va como un objeto y no como constantes sueltas por una razon concreta: los
# nombres tienen que resolverse al pintar, no al importar el modulo. Una
# constante de modulo se copia en quien la importa y se queda con el tema que
# hubiera al arrancar; ``paleta()`` devuelve siempre el que esta puesto ahora.
#
# El acento no esta aqui, ni en un tema ni en el otro: lo pone Windows. Ver
# ``accent_color``.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Paleta:
    """Los tonos de un tema, cada uno con el nombre del papel que cumple.

    Los campos van en mayusculas, contra la costumbre de los atributos, porque
    son los mismos nombres que llevan los marcadores de las hojas de estilo:
    ``qss_vars`` es literalmente esta paleta convertida en diccionario, asi que
    una sola lista de nombres impide que una hoja pida un color que la paleta
    no tenga.
    """

    # Superficies, de la mas honda a la mas cercana.
    WINDOW_BG: str
    CARD_BG: str
    CONTROL_BG: str
    CONTROL_HOVER: str
    CONTROL_PRESSED: str
    CONTROL_DISABLED: str

    STROKE: str
    STROKE_STRONG: str
    DIVIDER: str

    TEXT: str
    TEXT_SECONDARY: str
    TEXT_TERTIARY: str
    TEXT_DISABLED: str

    # Estados. Se leen como texto sobre la superficie del tema, no como
    # relleno de celda, asi que el oscuro los quiere claros y el claro los
    # quiere oscuros: el mismo verde no sirve en los dos.
    STATUS_OK: str
    STATUS_WARNING: str
    STATUS_ERROR: str

    # La tabla. La fila alterna y la cabecera son tonos propios porque tienen
    # que separarse del fondo de la tabla sin llegar al de un control.
    TABLE_ALTERNATE_BG: str
    TABLE_HEADER_BG: str
    # El pulgar de la barra de desplazamiento cuando el cursor esta encima:
    # el unico gris que la barra no comparte con nada mas.
    SCROLL_HANDLE_HOVER: str

    # Los nombres de siempre, que dicen donde va cada color («la base de la
    # tabla», «el borde del panel»). No traen tono propio: los rellena
    # ``_con_alias`` a partir de los de arriba, en un solo sitio, para que no
    # puedan separarse de aquello a lo que acompanan.
    TABLE_BASE_BG: str
    TABLE_GRID: str
    TABLE_TEXT: str
    PANE_BG: str
    PANE_SURFACE_BG: str
    PANE_CONTROL_BG: str
    PANE_CONTROL_HOVER: str
    PANE_BORDER: str
    PANE_TEXT: str


def _con_alias(**tonos: str) -> Paleta:
    """Completa los nombres del panel y la tabla desde los tonos base."""
    return Paleta(
        **tonos,
        TABLE_BASE_BG=tonos["CARD_BG"],
        TABLE_GRID=tonos["STROKE_STRONG"],
        TABLE_TEXT=tonos["TEXT"],
        # El visor de PDF acompana a la tabla dentro de la misma ventana, asi
        # que va en su mismo tono. La superficie que rodea a la pagina baja al
        # escalon mas hondo: el papel del escaneo es blanco y necesita flotar
        # sobre algo, como en cualquier lector de PDF.
        PANE_BG=tonos["CARD_BG"],
        PANE_SURFACE_BG=tonos["WINDOW_BG"],
        PANE_CONTROL_BG=tonos["CONTROL_BG"],
        PANE_CONTROL_HOVER=tonos["CONTROL_HOVER"],
        PANE_BORDER=tonos["STROKE"],
        PANE_TEXT=tonos["TEXT"],
    )


# Los grises oscuros de Windows 11. Los estados son los tres de la plataforma
# para texto sobre fondo oscuro; los que habia antes venian de la paleta de
# GitHub y no eran los de aqui.
OSCURA = _con_alias(
    WINDOW_BG="#202020",
    CARD_BG="#2b2b2b",
    CONTROL_BG="#333333",
    CONTROL_HOVER="#3d3d3d",
    CONTROL_PRESSED="#292929",
    CONTROL_DISABLED="#282828",
    STROKE="#3d3d3d",
    STROKE_STRONG="#4a4a4a",
    DIVIDER="#2a2a2a",
    TEXT="#ffffff",
    TEXT_SECONDARY="#c5c5c5",
    TEXT_TERTIARY="#9a9a9a",
    TEXT_DISABLED="#7a7a7a",
    STATUS_OK="#6ccb5f",
    STATUS_WARNING="#fce100",
    STATUS_ERROR="#ff99a4",
    TABLE_ALTERNATE_BG="#313131",
    TABLE_HEADER_BG="#252525",
    SCROLL_HANDLE_HOVER="#5f5f5f",
)

# Los claros, de la misma plataforma. Dos cosas se invierten y no se traducen
# tono a tono: el control en reposo es mas claro que la tarjeta pero al pasar
# el cursor se oscurece en vez de aclararse, que es como responde Fluent en
# claro; y los estados bajan a versiones saturadas y oscuras, porque el mismo
# verde que se lee sobre el gris de noche desaparece sobre el blanco.
CLARA = _con_alias(
    WINDOW_BG="#f3f3f3",
    CARD_BG="#ffffff",
    CONTROL_BG="#fbfbfb",
    CONTROL_HOVER="#f2f2f2",
    CONTROL_PRESSED="#e9e9e9",
    CONTROL_DISABLED="#f4f4f4",
    STROKE="#e2e2e2",
    STROKE_STRONG="#c9c9c9",
    DIVIDER="#ebebeb",
    TEXT="#1b1b1b",
    TEXT_SECONDARY="#5d5d5d",
    TEXT_TERTIARY="#767676",
    TEXT_DISABLED="#a0a0a0",
    STATUS_OK="#0f7b0f",
    STATUS_WARNING="#9d5d00",
    STATUS_ERROR="#c42b1c",
    TABLE_ALTERNATE_BG="#f7f7f7",
    TABLE_HEADER_BG="#f3f3f3",
    SCROLL_HANDLE_HOVER="#8a8a8a",
)

TEMA_OSCURO = "oscuro"
TEMA_CLARO = "claro"
TEMAS = {TEMA_OSCURO: OSCURA, TEMA_CLARO: CLARA}

# Con cual se abre si nadie ha elegido: el oscuro, que es con el que nacio la
# ventana y el que siguen dando por hecho las capturas del manual.
TEMA_POR_OMISION = TEMA_OSCURO

_tema_activo = TEMA_POR_OMISION


def tema() -> str:
    """El nombre del tema puesto ahora mismo."""
    return _tema_activo


def paleta() -> Paleta:
    """Los tonos del tema puesto ahora mismo.

    Se llama en el momento de pintar y no se guarda en una constante: eso es
    lo que permite que la misma regla dé un color u otro segun el tema.
    """
    return TEMAS[_tema_activo]


def set_tema(nombre: str) -> bool:
    """Cambia el tema activo y dice si de verdad cambio algo.

    Solo mueve la paleta. Repintar lo que ya esta en pantalla es asunto de
    ``app.gui.theme.aplicar_tema``, que es quien conoce la aplicacion.
    """
    global _tema_activo
    if nombre not in TEMAS or nombre == _tema_activo:
        return False
    _tema_activo = nombre
    return True


def otro_tema() -> str:
    """El que no esta puesto. Es lo que pide el boton que alterna los dos."""
    return TEMA_CLARO if _tema_activo == TEMA_OSCURO else TEMA_OSCURO


# Azul de reserva: el de Windows, para cuando no hay aplicacion de la que leer
# el acento del sistema todavia.
ACCENT_FALLBACK = "#0078d4"


def accent_color() -> str:
    """El color de acento que el usuario eligió en Windows.

    Se lee del sistema en lugar de fijarlo, que es lo que hace el panel de
    ajustes y lo que hace PowerToys: así la aplicación pertenece al escritorio
    en el que se abre en vez de traer su propio azul. Si todavía no hay
    ``QGuiApplication`` (al importar el módulo, por ejemplo) no hay a quién
    preguntarle, y entonces vale el de Windows por omisión.
    """
    app = QGuiApplication.instance()
    if app is None:
        return ACCENT_FALLBACK
    color = app.palette().color(QPalette.ColorRole.Accent)
    return color.name() if color.isValid() else ACCENT_FALLBACK


def blend(encima: str, debajo: str, peso: float) -> str:
    """Mezcla ``encima`` sobre ``debajo`` con el peso dado, y devuelve el hex."""
    a, b = QColor(encima), QColor(debajo)
    resto = 1.0 - peso
    return QColor(
        round(a.red() * peso + b.red() * resto),
        round(a.green() * peso + b.green() * resto),
        round(a.blue() * peso + b.blue() * resto),
    ).name()


def hover_row_color() -> str:
    """Banda de la fila que tiene el cursor encima.

    Sale del acento y no de un azul propio: si el usuario tiene el acento en
    rojo, una fila resaltada en azul no pertenece a nada. Queda muy rebajada
    sobre la tarjeta porque el cursor solo pasa por encima y no puede competir
    con la banda de la selección.
    """
    return blend(accent_color(), paleta().CARD_BG, 0.22)


def checked_row_color() -> str:
    """Banda de la fila marcada con su casilla.

    Más presente que el cursor, porque es una decisión y no un roce, pero por
    debajo de la selección, que es lo que se está mirando ahora mismo.
    """
    return blend(accent_color(), paleta().CARD_BG, 0.45)


def link_text_color() -> str:
    """Texto de una celda que se puede pulsar para ir a otro sitio.

    Sale del acento, por lo mismo que las bandas de fila, pero llevado hacia
    el color del texto: el acento tal cual es un tono pensado para pintar
    fondos y sobre el fondo de la tabla se lee mal, que es justo lo contrario
    de lo que esta celda tiene que conseguir. Hacia dónde lo lleva depende del
    tema (al blanco en el oscuro, al casi negro en el claro), y por eso mezcla
    con el texto del tema y no con un blanco escrito aquí.
    """
    return blend(paleta().TEXT, accent_color(), 0.45)


def on_accent_text(accent: str | None = None) -> str:
    """Blanco o negro sobre el acento, el que se lea.

    El acento lo elige el usuario y puede ser un amarillo o un lima, sobre los
    que el texto blanco desaparece. Se decide por luminancia en vez de dar por
    hecho que siempre será un azul oscuro.
    """
    color = QColor(accent or accent_color())
    r, g, b = color.redF(), color.greenF(), color.blueF()

    def lineal(canal: float) -> float:
        return canal / 12.92 if canal <= 0.04045 else ((canal + 0.055) / 1.055) ** 2.4

    luminancia = 0.2126 * lineal(r) + 0.7152 * lineal(g) + 0.0722 * lineal(b)
    return "#000000" if luminancia > 0.45 else "#ffffff"


def qss_vars() -> dict[str, object]:
    """Todo lo que una hoja de estilo puede nombrar entre llaves.

    Las hojas son plantillas con marcadores (``{PANE_TEXT}``, ``{CONTROL_BOX_H}``)
    y se rellenan con esto justo antes de instalarlas, no al importar el módulo:
    esa es la diferencia entre una hoja que se puede repintar al cambiar de tema
    y una que se quedó con los grises del arranque.

    Van juntos los colores y las medidas porque una misma regla suele pedir de
    los dos, y un solo diccionario evita tener que acordarse de cuál es cuál.
    """
    variables: dict[str, object] = {
        "BORDER": BORDER,
        "CONTROL_HEIGHT": CONTROL_HEIGHT,
        "CONTROL_HEIGHT_COMPACT": CONTROL_HEIGHT_COMPACT,
        "CONTROL_BOX_H": CONTROL_BOX_H,
        "CONTROL_BOX_H_COMPACT": CONTROL_BOX_H_COMPACT,
        "CONTROL_PAD_H": CONTROL_PAD_H,
        "CONTROL_PAD_H_COMPACT": CONTROL_PAD_H_COMPACT,
        "FONT_FAMILY": FONT_FAMILY,
        "FONT_BODY_PT": FONT_BODY_PT,
        "FONT_CAPTION_PT": FONT_CAPTION_PT,
        "FONT_SUBTITLE_PT": FONT_SUBTITLE_PT,
        "RADIUS_CARD": RADIUS_CARD,
        "RADIUS_CONTROL": RADIUS_CONTROL,
        "SPACE_XS": SPACE_XS,
        "SPACE_S": SPACE_S,
        "SPACE_M": SPACE_M,
        "SPACE_L": SPACE_L,
        "SPACE_XL": SPACE_XL,
        "WEIGHT_REGULAR": WEIGHT_REGULAR,
        "WEIGHT_STRONG": WEIGHT_STRONG,
        # El texto que se lee encima del acento. No es de la paleta porque no
        # lo decide el tema sino el acento del sistema: sobre un amarillo hay
        # que escribir en negro tanto de día como de noche.
        "ON_ACCENT": on_accent_text(),
    }
    variables.update(asdict(paleta()))
    return variables
