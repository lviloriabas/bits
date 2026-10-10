"""Ventana «Indexar en AirVault».

Es la cara de :mod:`app.airvault`: no decide nada por su cuenta, solo pide
los datos que hacen falta, lanza el recorrido en un hilo aparte y cuenta
como fue. Todo lo que decide si una página se escribe o no vive en el
módulo, que se prueba sin interfaz.

Va en ventana aparte y no colgando de la principal. Empotrado, el indexado
le quitaba alto a la vista previa y descuadraba el reparto: al desplegarse
cambiaba el mínimo de la ventana, y en pantallas bajas eso la sacaba del
escritorio. Aparte tiene el sitio que necesita (el historial entero de
ejecuciones, su propio avance) y la ventana principal vuelve a medirse
sola.

El trabajo va en tres tiempos, separados porque duran cosas muy distintas:

1. **Subir a AirVault** manda los PDF de uno en uno: cada carga se
   identifica y se renombra antes de empezar la siguiente.
2. **Revisar** asigna el ID apenas aparece y confirma si ya está entero.
3. **Indexar** escribe cada batch en cuanto queda confirmado y antes de
   mandar el archivo siguiente: Subida > Indexado, batch por batch, en un
   solo hilo y con una sola sesión. Se puede desactivar en
   «Automatización…», el menú que dice hasta dónde llega la cadena y que es
   el mismo que el de la ventana principal.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Dict, Optional, Sequence

from loguru import logger
from PySide6.QtCore import (QEvent, QItemSelection, QItemSelectionModel, QRectF,
                            QSignalBlocker, Qt, QThread, QTimer, Signal)
from PySide6.QtGui import (QBrush, QColor, QGuiApplication, QIcon,
                           QKeySequence, QPainter, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLayout, QLineEdit, QListView,
                               QListWidgetItem, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy, QSpinBox,
                               QSplitter, QStyle, QStyleOptionComboBox, QStylePainter,
                               QTableWidgetItem, QToolButton,
                               QVBoxLayout, QWidget)

from app.airvault.config import (AIRVAULT_FILENAME, AirVaultConfig,
                                 guardar_paginas_por_batch)
from app.airvault.session import SesionCancelada
from app.gui.airvault_busqueda import buscar_en_la_cola, frase_de
from app.gui.batches_sidebar import BatchesSidebar, agregar_filtros_al_menu
from app.gui.automatizacion import (COMPLETAR, MenuAutomatizacion,
                                    OpcionesAutomatizacion)
from app.gui.csv_utils import (TEXTO_ELEGIR_EJECUCION, find_csv_files,
                               find_run_dirs, run_read_day)
from app.gui.memoria import AIRVAULT, recordar
from app.gui.responsive import available_area, fit_to_screen
from app.gui.text_copy import CopyableListWidget
from app.gui.theme import gestor_tema
from app.gui.tokens import (SPACE_L, SPACE_M, SPACE_S, SPACE_XL, SPACE_XS,
                            link_text_color, paleta)
from app.gui.widgets import (ElidedLabel, IconoAyuda, SpinBoxWithButtons,
                             configure_combo_box, configure_menu_button,
                             data_table_qss, pane_status_colors,
                             pintar_celda_del_tema, pintar_del_tema,
                             window_stylesheet)
from app.utils.io import send_to_trash
from app.utils.mensajes import mensaje_error


# Los mismos colores con los que la ventana principal escribe las líneas de
# ayuda y marca los estados. Son funciones y no constantes porque cambian con
# el tema: el verde que se lee sobre el gris de noche desaparece sobre el
# blanco, y al revés.
def color_ayuda() -> str:
    """Gris con el que se escriben las líneas de ayuda."""
    return paleta().TEXT_SECONDARY


def color_indexado() -> str:
    """Verde de lo que ya está en AirVault."""
    return pane_status_colors()["OK"]


def color_revisar() -> str:
    """Ámbar de lo que hay que confirmar a mano, como en la ventana principal."""
    return pane_status_colors()["WARNING"]


def color_indexando() -> str:
    """Acento legible sobre el fondo del tema activo."""
    return link_text_color()


# El rótulo del batch que se está escribiendo ahora mismo. No es un estado
# guardado en ningún manifiesto: dura lo que dura la escritura, y sin él la
# fila seguía diciendo «Listo para indexar» mientras se le escribía.
TEXTO_INDEXANDO = "Indexando"


def papel_de_estado(
    estado: str, indexando: bool = False, revisar: bool = False,
) -> Optional[str]:
    """El color con el que la cola escribe un estado, por su papel en el tema.

    ``None`` es el texto normal de la tabla. La tabla y la leyenda lo sacan
    de aquí, así que no pueden contar colores distintos.
    """
    from app.airvault.flujo import (AUTOCOMPLETADO, CANCELADO, COMPLETADO,
                                    INCOMPLETO, INDEXADO, POSIBLE_DUPLICADO,
                                    NO_ENCONTRADO, SIN_SUBIR)

    if estado in (COMPLETADO, AUTOCOMPLETADO):
        return "STATUS_OK"
    if estado == NO_ENCONTRADO:
        return "STATUS_ERROR"
    if revisar and not indexando and estado in (INDEXADO, INCOMPLETO):
        return "STATUS_WARNING"
    if indexando or estado in (INDEXADO, INCOMPLETO):
        return "acento"
    if estado in (SIN_SUBIR, CANCELADO, POSIBLE_DUPLICADO):
        # Gris es lo que la cola no va a mover por su cuenta: el archivo que
        # Quick Upload aún no aceptó, el batch que alguien sacó de la cola y
        # el que se paró por parecerse a algo ya subido. Desde que se sube
        # queda en blanco, incluso mientras AirVault lo procesa.
        return "TEXT_TERTIARY"
    return None


def _color_de_papel(papel: str) -> str:
    return link_text_color() if papel == "acento" else getattr(paleta(), papel)


def leyenda_de_estados() -> list[tuple[str, Optional[str], str]]:
    """Cada rótulo de la columna Estado, su color y qué quiere decir.

    Va en el orden en que un batch los recorre, y los que se salen del
    camino (descuadrado, tomado, duplicado, cancelado) al final.
    """
    from app.airvault.flujo import (BUSCANDO, CANCELADO, COMPLETADO,
                                    DESCUADRADO, INCOMPLETO, INDEXADO, LISTO,
                                    NOMBRE_ESTADO_PARTE, POSIBLE_DUPLICADO,
                                    PROCESANDO, PUBLICADO, NO_ENCONTRADO, SIN_SUBIR, TOMADO)

    explicaciones = (
        (SIN_SUBIR, "El PDF todavía no se envió a AirVault."),
        (BUSCANDO, "El PDF ya se envió. Se espera a que el programa lo "
                   "detecte en la cola de AirVault."),
        (PROCESANDO, "AirVault lo recibió y todavía está cargando sus "
                     "páginas."),
        (LISTO, "Está completo en AirVault; falta colocar la información "
                "en las páginas."),
        (None, "Se está colocando la información en las páginas."),
        (INDEXADO, "En los batches normales, todas las páginas están en "
                   "verde y falta cerrar con «Complete». REVISAR conserva "
                   "el amarillo: sus datos están guardados y las "
                   "incidencias quedan para revisión manual."),
        (INCOMPLETO, "Quedan páginas en amarillo: se reintentan solas o se "
                     "corrigen a mano."),
        (COMPLETADO, "Se cerró con «Complete» y pasó a Web Search."),
        (PUBLICADO, "Su publicación se confirmó en Web Search. "
                     "No se vuelve a enviar."),
        (NO_ENCONTRADO, "AirVault no publicó la carga al agotar la espera y la cola no la contiene. "
                        "Se conservan sus archivos y se puede reenviar a mano."),
        (DESCUADRADO, "AirVault tiene otra cantidad de páginas. No se toca "
                      "hasta revisarlo."),
        (TOMADO, "Alguien lo tiene abierto en AirVault; se espera a que lo "
                 "cierre."),
        (POSIBLE_DUPLICADO, "Sus bitácoras parecen estar ya en AirVault. No "
                            "se sube sin revisarlo."),
        (CANCELADO, "Se sacó de la cola y no avanza hasta reanudarlo."),
    )
    return [
        (
            TEXTO_INDEXANDO if estado is None else NOMBRE_ESTADO_PARTE[estado],
            papel_de_estado(estado or "", indexando=estado is None),
            explicacion,
        )
        for estado, explicacion in explicaciones
    ]


def leyenda_de_estados_html() -> str:
    """La leyenda como tabla, con cada rótulo en el color de la cola.

    Se arma cada vez que se enseña, no una al construir la ventana: los
    colores son los del tema de ese momento.
    """
    filas = []
    for rotulo, papel, explicacion in leyenda_de_estados():
        estilo = f"color: {_color_de_papel(papel)};" if papel else ""
        filas.append(
            f'<tr><td style="{estilo} white-space: nowrap; '
            f'padding-right: {SPACE_S}px;"><b>{rotulo}</b></td>'
            f"<td>{explicacion}</td></tr>"
        )
    return (
        '<table cellspacing="0" cellpadding="2">' + "".join(filas) + "</table>"
    )


def conteo_de_estados(partes) -> str:
    """Cuántos batches hay en cada estado: «2 completados, 1 subido».

    Es lo que dice el resumen en vez de una frase por batch. Una línea por
    batch con su detalle ocupaba media ventana y no contestaba lo que se
    mira ahí, que es cómo va la cola entera. Sigue el orden del recorrido.
    """
    from app.airvault.flujo import (AUTOCOMPLETADO, BUSCANDO, CANCELADO,
                                    COMPLETADO, DESCUADRADO, INCOMPLETO,
                                    INDEXADO, LISTO, POSIBLE_DUPLICADO,
                                    PROCESANDO, SIN_SUBIR, SOLO_REVISAR,
                                    TOMADO)

    nombres = (
        ((SIN_SUBIR,), "sin subir", "sin subir"),
        ((BUSCANDO,), "subido", "subidos"),
        ((PROCESANDO,), "procesando en AirVault", "procesando en AirVault"),
        ((LISTO, SOLO_REVISAR), "listo para indexar", "listos para indexar"),
        ((INDEXADO,), "indexado", "indexados"),
        ((INCOMPLETO,), "indexado incompleto", "indexados incompletos"),
        ((COMPLETADO, AUTOCOMPLETADO), "completado", "completados"),
        ((DESCUADRADO,), "con páginas que no coinciden",
         "con páginas que no coinciden"),
        ((TOMADO,), "abierto por otra persona", "abiertos por otra persona"),
        ((POSIBLE_DUPLICADO,), "posible duplicado", "posibles duplicados"),
        ((CANCELADO,), "cancelado", "cancelados"),
    )
    estados = [parte.estado for parte in partes]
    trozos = []
    for grupo, singular, plural in nombres:
        cuantos = sum(estado in grupo for estado in estados)
        if cuantos:
            trozos.append(
                f"{cuantos} {singular if cuantos == 1 else plural}"
            )
    return ", ".join(trozos)

# Lo que se lee debajo de la tabla de batches mientras no se ha buscado
# ninguna bitácora, y a lo que se vuelve al vaciar el campo.
AYUDA_BUSCAR_BITACORA = (
    "Busque una bitácora, matrícula, vuelo, fecha o archivo para localizarla "
    "en la cola."
)

# Ejecuciones que lista el historial, las mismas que el visor de CSV: es la
# ventana de trabajo de un turno. Se sube lo que está en la lista y nada
# más: la ruta de un CSV cualquiera no dice de qué ejecución viene, y
# dejarla elegir a mano abría la puerta a subir el CSV equivocado.
LIMITE_HISTORIAL = 25

# Si la ejecución ya se puede subir. El nombre no hace falta guardarlo: es el
# texto de la opción tal cual.
ROL_SE_PUEDE_SUBIR = Qt.ItemDataRole.UserRole + 1

# Cada cuántos minutos se le pregunta a AirVault sin que nadie pulse nada.
# Dos minutos mantiene la cola al dia sin convertir la espera en sondeo
# continuo; el valor sigue siendo configurable en la ventana.
MINUTOS_POR_DEFECTO = 2

# Una respuesta de guardado puede ser aceptada por HTTP y aun dejar la pagina
# en Need Correction durante un instante. Se relee y reenvia en el mismo
# proceso antes de devolver el control a la persona.
INTENTOS_INDEXADO = 3

# Relecturas de la verificación en cada intento de indexado, en segundos.
# Van creciendo: la primera vuelta solo cubre el desfase corto de AirVault
# antes de reescribir, y las siguientes le dan más margen. Una página cuyo
# problema es del manifiesto (una fecha dudosa) no se relee.
ESPERAS_CONFIRMACION = ((5, 15), (30,), (60,))

# Esperas antes de volver a pedir el cierre de un batch ya verificado que
# AirVault todavía no dejó completar por páginas fuera de verde.
ESPERAS_CIERRE = (15, 45)

# Comprobaciones periódicas que se le dan a un batch que acabó el indexado
# sin confirmar. Cada una relee AirVault y, si hace falta, reescribe las
# páginas amarillas y completa. Un intento que deja menos amarillas que el
# anterior las devuelve enteras: mientras reescribir sirva, se sigue.
RECONFIRMACIONES_TRAS_INDEXAR = 3

# Gastadas esas comprobaciones sin avanzar, el batch amarillo se sigue
# reintentando solo, pero espaciado: suele ser AirVault, que tarda en
# reflejar lo guardado o rechaza un rato. Con un tope, para que una página
# que de verdad necesita una mano no haga releer el batch toda la noche.
MINUTOS_ENTRE_REINTENTOS_AMARILLOS = 20
REINTENTOS_AMARILLOS_ESPACIADOS = 6

# Fallos seguidos de la comprobación automática antes de espaciarla. Uno
# solo no significa nada: AirVault devuelve un 500 de vez en cuando y la
# sesión se renueva sola. Tres seguidos ya no son un tropiezo, pero tampoco
# motivo para parar: una red caída o un AirVault en mantenimiento vuelven,
# y parar dejaba la ejecución sin indexar hasta que alguien volviera a la
# ventana. Se sigue intentando cada `MINUTOS_TRAS_FALLOS`.
FALLOS_SEGUIDOS_ANTES_DE_ESPACIAR = 3
MINUTOS_TRAS_FALLOS = 15

# Líneas que conserva la bitácora. Con la comprobación automática corriendo
# toda una tarde, sin tope crecería sin fin.
LIMITE_BITACORA = 300

# Cada cuánto se repinta la línea «En curso» de la bitácora, en ms. Es lo
# que gira; más lento se lee como tirones y no como algo que trabaja.
MS_LATIDO = 100

# Cuánto del camino de un batch deja hecho cada estado, para la barra de
# avance de toda la cola. Subir y escribir son lo que tarda, y por eso se
# llevan casi todo el recorrido; la espera a que AirVault lo publique y el
# cierre pesan poco. Sin «Completar batch» la meta es quedar indexado.
AVANCE_SUBIDO = 0.30
AVANCE_LISTO = 0.40
AVANCE_ESCRITO = 0.80
AVANCE_INDEXADO = 0.90


def avance_de_estado(estado: str) -> float:
    """La parte del recorrido de un batch que ya dejó hecha su estado."""
    from app.airvault.flujo import (AUTOCOMPLETADO, BUSCANDO, COMPLETADO,
                                    DESCUADRADO, INCOMPLETO, INDEXADO, LISTO,
                                    PROCESANDO, PUBLICADO, NO_ENCONTRADO, SOLO_REVISAR, TOMADO)

    if estado in (COMPLETADO, AUTOCOMPLETADO, PUBLICADO):
        return 1.0
    if estado == INDEXADO:
        return AVANCE_INDEXADO
    if estado == INCOMPLETO:
        return AVANCE_ESCRITO
    if estado in (LISTO, SOLO_REVISAR, TOMADO):
        return AVANCE_LISTO
    if estado in (BUSCANDO, PROCESANDO, DESCUADRADO, NO_ENCONTRADO):
        return AVANCE_SUBIDO
    return 0.0

# El alto mínimo de la bitácora, el de las dos tablas y el del resumen de
# abajo salen de la densidad (``airvault_log_min_height`` y compañía): con
# 110 px fijos la bitácora cabía en tres líneas y un mensaje largo había que
# leerlo a trozos, y con el mínimo holgado la ventana no entraba en un
# escritorio de 1366x768 sin montar unos controles sobre otros.

# El nombre distingue las divisiones y REVISAR, así que no puede quedar
# reducido a unas pocas letras. A partir de este ancho se conserva espacio
# para Páginas y Estado; el texto completo sigue disponible en la ayuda.
# Suelo por debajo del cual la ventana no sirve de nada, aunque la pantalla
# sea mas pequenya todavia. Por encima manda lo que quepa en el escritorio.
ANCHO_MINIMO_VENTANA = 640
ALTO_MINIMO_VENTANA = 480

ANCHO_MINIMO_NOMBRE_BATCH = 220
ANCHO_MAXIMO_NOMBRE_BATCH = 420
ANCHO_FORMULARIO_DOBLE = 640

# Lo que explica el desplegable de fecha del indexado y lo que se dice
# cuando la ejecución no deja elegir.
TOOLTIP_FECHA_INDEXADO = (
    "Fecha con la que se escribe cada bitácora en AirVault. No cambia el "
    "CSV de la ejecución."
)
TOOLTIP_FECHA_SIN_DIA = (
    "Esta ejecución se procesó a fin de mes y no leyó el día, así que no "
    "se puede indexar con el día exacto. Vuelva a procesarla con «Día "
    "exacto» si lo necesita."
)

TEXTO_SIN_SUBIR = "Sin subir. Pulse «Subir a AirVault» para empezar."

# Lo que dice el recuadro de reparto mientras no hay una ejecución elegida,
# y cuando la elegida se exportó antes de que el CSV llevara la columna.
TEXTO_SIN_EJECUCION = "Elija una ejecución para ver cuántas hay que revisar."
TEXTO_SIN_COLUMNA = (
    "Esta ejecución se exportó antes de que el CSV dijera cuáles van a "
    "revisar. Vuelva a exportarla para saberlo."
)
TOOLTIP_REPARTO = (
    "Cuántas bitácoras de la ejecución se indexan solas y cuántas viajan en "
    "el batch REVISAR para terminarlas a mano. Sale de la columna «review» "
    "del CSV, la misma con la que se reparte la entrega."
)

AIRVAULT_TOOLTIP = (
    "Escribe en AirVault los datos que la ejecución ya leyó, sin teclear "
    "página por página en el Web Index."
)


def _minutos(segundos: float) -> str:
    """«m:ss», para el reloj del paso y la cuenta atrás de la revisión."""
    entero = int(segundos)
    return f"{entero // 60:d}:{entero % 60:02d}"


def primera_frase(texto: str) -> str:
    """La primera frase de un mensaje largo.

    El resumen explica con detalle, y ahí está bien: se lee entero. En la
    bitácora ese mismo párrafo ocupa media pantalla y tapa las horas de
    alrededor, así que solo se apunta con qué empieza.
    """
    limpio = " ".join(str(texto or "").split())
    corte = limpio.find(". ")
    return limpio[:corte + 1] if corte > 0 else limpio


def csv_de_corrida(carpeta: Path | str) -> Optional[Path]:
    """CSV mínimo de una ejecución, que es el que va a AirVault.

    El indexado necesita el CSV corto (el de las columnas del Web Index),
    no el ``_completo``, que trae además el detalle de la lectura.
    """
    carpeta = Path(carpeta)
    preferido = carpeta / "datos" / f"{carpeta.name}.CSV"
    if preferido.is_file():
        return preferido
    candidatos = [
        ruta for ruta in find_csv_files(carpeta)
        if not ruta.stem.casefold().endswith("_completo")
    ]
    return candidatos[0] if candidatos else None


def paginas_de_corrida(carpeta: Path | str) -> Optional[int]:
    """Páginas que dejó la ejecución, según sus estadísticas."""
    try:
        datos = json.loads(
            (Path(carpeta) / "stats.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    total = datos.get("total_paginas") if isinstance(datos, dict) else None
    return int(total) if isinstance(total, (int, float)) else None


def _filas_con_review(ruta: Path) -> list[dict] | None:
    """Filas de un CSV que ya trae la columna ``review``, o ``None``."""
    # Local: en esta ventana ``csv`` es el nombre con el que viaja la ruta
    # de la ejecución, y traer el módulo al espacio del archivo dejaría dos
    # cosas distintas llamadas igual.
    import csv

    if not ruta.is_file():
        return None
    try:
        with ruta.open("r", encoding="utf-8-sig", newline="") as handle:
            lector = csv.DictReader(handle)
            if not lector.fieldnames or "review" not in lector.fieldnames:
                return None
            return list(lector)
    except (OSError, ValueError):
        return None


def reparto_de_revision(csv: Path | str) -> tuple[int, int] | None:
    """Cuántas bitácoras se indexan solas y cuántas van a REVISAR.

    Sale de la columna ``review`` del CSV, que es la misma decisión con la
    que la exportación reparte la entrega: contar aquí por otro camino
    daría un número que no es el de los batches que se van a subir.

    Se mira primero el CSV mínimo, que es el que se sube; si esa columna no
    está marcada como importante, la trae igual el ``_completo``. Devuelve
    ``None`` cuando la ejecución se exportó antes de que la columna
    existiera, que es lo único que no se puede contestar.
    """
    from app.reports.outputs import complete_csv_path

    ruta = Path(csv)
    filas = _filas_con_review(ruta)
    if filas is None:
        filas = _filas_con_review(complete_csv_path(ruta))
    if filas is None:
        return None
    revisar = sum(
        1 for fila in filas
        if str(fila.get("review", "")).strip().lower() == "true"
    )
    return len(filas) - revisar, revisar


def batches_de_entrega(csv: Path | str, limite: int) -> int | None:
    """En cuántos batches se partiría la ejecución con ese máximo por batch.

    Se calcula con el mismo reparto que después se sube, no dividiendo
    páginas entre el límite: un batch que empieza a mitad de una aeronave
    repite el separador de su sección, y esa página repetida ocupa sitio.
    Dividiendo a mano el número sale corto justo en las ejecuciones con
    muchas aeronaves, que son las que más batches producen.

    Devuelve ``None`` si la ejecución todavía no tiene con qué calcularlo.
    """
    from app.airvault.flujo import (ErrorDeCorrida,
                                    _partir_paginas_por_seccion,
                                    partes_de_corrida)

    try:
        partes = partes_de_corrida(csv)
    except (OSError, ValueError):
        return None
    if not partes:
        return None
    total = 0
    for parte in partes:
        try:
            tramos = _partir_paginas_por_seccion(parte.paginas, limite)
        except ErrorDeCorrida:
            # Un límite que no permite repetir el separador. El reparto real
            # dará el mismo error y lo explicará; aquí no hay nada que decir.
            return None
        total += len(tramos) or 1
    return total


def estado_de_entrega(
    csv: Path | str, limite: int | None = None
) -> tuple[str, bool]:
    """Qué tiene la ejecución para subir, y si con eso alcanza.

    Se mira aquí para que el historial diga de un vistazo cuáles se pueden
    subir. El motivo exacto lo vuelve a comprobar ``comprobar_entrega`` al
    arrancar, que es quien manda: esto solo evita empezar un trabajo que ya
    se sabe que no va a salir.

    Con ``limite`` se añade en cuántos batches queda repartida, que es lo
    que de verdad se sube: un archivo de entrega grande se parte en varios,
    y saberlo antes de empezar evita la sorpresa de ver diez filas en la
    cola donde se esperaban dos.
    """
    from app.airvault.flujo import pdfs_de_corrida, ruta_indice_paginas

    pdfs = pdfs_de_corrida(csv)
    if not pdfs:
        return "Sin exportar", False
    if not ruta_indice_paginas(csv).is_file():
        # Exportada antes de que existiera el índice de páginas: hay PDF,
        # pero nada que diga qué página del batch es cuál.
        return "Falta reexportar", False
    archivos = "1 archivo" if len(pdfs) == 1 else f"{len(pdfs)} archivos"
    batches = batches_de_entrega(csv, limite) if limite else None
    if batches is None:
        return archivos, True
    reparto = "1 batch" if batches == 1 else f"{batches} batches"
    return f"{archivos}, {reparto}", True


TOOLTIP_ELIMINAR_REGISTRO = (
    "Elimina los batches locales de esta ejecución y evita que vuelvan a "
    "crearse. No toca el CSV, los PDF de entrega ni AirVault."
)
TOOLTIP_ELIMINAR_REGISTROS = (
    "Elimina para siempre los batches locales que quedan en output/airvault, "
    "aunque su ejecución ya no esté en el historial. No toca los CSV, los "
    "PDF de entrega ni los batches remotos."
)


class SelectorEjecuciones(QComboBox):
    """El nombre elige una ejecucion; las casillas permiten reunir varias."""

    seleccion_cambiada = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.permitir_marcar = True
        self.view().viewport().installEventFilter(self)
        self.view().installEventFilter(self)

    def marcar(self, indice: int) -> None:
        if not self.permitir_marcar or not self.itemData(indice, ROL_SE_PUEDE_SUBIR):
            return
        marcado = self.itemData(indice, Qt.ItemDataRole.CheckStateRole)
        self.setItemData(
            indice,
            Qt.CheckState.Unchecked if marcado == Qt.CheckState.Checked
            else Qt.CheckState.Checked,
            Qt.ItemDataRole.CheckStateRole,
        )
        self.seleccion_cambiada.emit()
        self.update()

    def eventFilter(self, objeto, evento) -> bool:  # noqa: N802 - API Qt
        if evento.type() == QEvent.Type.MouseButtonRelease and objeto is self.view().viewport():
            indice = self.view().indexAt(evento.position().toPoint())
            ancho = self.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth) + SPACE_S * 2
            if (
                evento.button() == Qt.MouseButton.LeftButton
                and indice.isValid()
                and self.itemData(indice.row(), Qt.ItemDataRole.CheckStateRole) is not None
                and evento.position().x() < self.view().visualRect(indice).left() + ancho
            ):
                self.marcar(indice.row())
                return True
        if evento.type() == QEvent.Type.KeyPress and objeto is self.view():
            if evento.key() == Qt.Key.Key_Space:
                self.marcar(self.view().currentIndex().row())
                return True
        return False

    def paintEvent(self, evento) -> None:  # noqa: N802 - API Qt
        cantidad = sum(
            self.itemData(i, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
            for i in range(1, self.count())
        )
        if cantidad <= 1:
            super().paintEvent(evento)
            return
        pintor = QStylePainter(self)
        opcion = QStyleOptionComboBox()
        self.initStyleOption(opcion)
        opcion.currentText = f"{cantidad} ejecuciones seleccionadas"
        pintor.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opcion)
        pintor.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opcion)


class TrabajoCancelado(BaseException):
    """Alguien pulsó Cancelar; el trabajo se deshace y se sale.

    Hereda de ``BaseException`` a propósito: el recorrido del indexado
    atrapa ``Exception`` en varios sitios para anotar la página que falló y
    seguir, y una cancelación no puede quedarse ahí anotada como si fuera
    el error de una página. Así atraviesa todo hasta el hilo, pasando por
    los ``finally`` que sueltan los batches en AirVault.
    """


class TrabajoAirVaultWorker(QThread):
    """Corre las etapas del indexado fuera del hilo de la interfaz.

    Una ejecución completa sube casi dos gigas y escribe cientos de páginas
    por red; hecho en el hilo de la ventana, Windows la daría por colgada.

    Todo lo que tarda pasa por ``_avisar``, que además es por donde entra la
    cancelación: así se puede parar dentro de una subida de mil trozos o de
    una espera de quince minutos sin que el recorrido sepa que existe un
    botón de cancelar.
    """

    paso = Signal(str, int, int)
    subidas_actualizadas = Signal(object)
    batch_encontrado = Signal(object)
    batch_indexado = Signal(object)
    batch_indexando = Signal(object, bool)
    subido = Signal(object)
    comprobado = Signal(object)
    buscado = Signal(object)
    indexado = Signal(object)
    fallo = Signal(str)
    cancelado = Signal()

    def __init__(self, modo: str, panel_estado: dict, parent=None) -> None:
        super().__init__(parent)
        self.modo = modo
        self.estado = panel_estado
        # Bandera propia, ademas de la de Qt. ``requestInterruption`` no
        # hace nada sobre un hilo que todavía no arranco, y el cierre de la
        # ventana puede pedir la cancelacion en ese hueco.
        self._parar = False
        self._lecturas_websearch: list = []

    def cancelar(self) -> None:
        """Pide que pare, arrancado o no.

        La bandera sola no basta: el hilo la mira entre paso y paso, y entre
        dos pasos puede haber una petición esperando hasta un minuto y hasta
        tres intentos, o la ventana de acceso esperando cinco minutos. Por
        eso se cancela también la sesión, que es quien está esperando de
        verdad.
        """
        self._parar = True
        self.requestInterruption()
        if self.modo == "buscar_websearch":
            for sesion in tuple(self._lecturas_websearch):
                cancelar = getattr(sesion, "cancelar", None)
                if callable(cancelar):
                    cancelar()
        else:
            sesion = self.estado.get("sesion")
            if sesion is not None:
                sesion.cancelar()

    def hay_que_parar(self) -> bool:
        return self._parar or self.isInterruptionRequested()

    # ── ejecución ──────────────────────────────────────────────────

    def run(self) -> None:  # noqa: D102 - lo describe la clase
        etapas = {
            "subir": self._subir,
            "subir_pendientes": self._subir_pendientes,
            "resubir": self._subir_pendientes,
            "comprobar": self._comprobar,
            "buscar_websearch": self._buscar_websearch,
            "indexar": self._indexar,
            "completar": self._completar,
        }
        # Lo que corta el hilo queda también en el registro. Antes solo se
        # veía en la ventana, y un indexado cortado a mitad del batch dejaba
        # en el archivo de registro páginas sin escribir y ninguna causa.
        try:
            etapas[self.modo]()
        except TrabajoCancelado:
            logger.info("Se canceló el trabajo de AirVault ({})", self.modo)
            self.cancelado.emit()
        except SesionCancelada:
            # La sesión se cortó porque alguien canceló: es lo mismo que
            # llegar al siguiente paso con la bandera puesta, solo que sin
            # esperar a que el servidor conteste.
            logger.info("Se canceló el trabajo de AirVault ({})", self.modo)
            self.cancelado.emit()
        except Exception as exc:  # noqa: BLE001 - llega a la interfaz
            logger.opt(exception=exc).error(
                "El trabajo de AirVault ({}) se detuvo: {}", self.modo, exc
            )
            fallback = ("No se pudieron consultar las bitácoras en Web Search. Vuelva a intentarlo."
                        if self.modo == "buscar_websearch" else
                        "No se pudo continuar el indexado. Vuelva a revisar en AirVault.")
            self.fallo.emit(mensaje_error(exc, fallback))

    def _avisar(self, texto: str, hechas: int, total: int) -> None:
        """Cuenta en qué va y, de paso, mira si hay que parar."""
        if self.hay_que_parar():
            raise TrabajoCancelado()
        self.paso.emit(texto, int(hechas), int(total))

    def _dormir(self, segundos: float) -> None:
        """Espera troceada, para que cancelar no tarde lo que tarde la espera.

        AirVault puede tardar minutos en sacar el batch de su cola. Dormir
        eso de una vez dejaba el botón de cancelar sin efecto hasta el
        siguiente sondeo.
        """
        restante = float(segundos)
        while restante > 0:
            if self.hay_que_parar():
                raise TrabajoCancelado()
            self.msleep(int(min(0.5, restante) * 1000))
            restante -= 0.5
        if self.hay_que_parar():
            raise TrabajoCancelado()

    def _notificar_subidas(self, trabajos) -> None:
        """Publica el estado local antes de empezar a buscar los IDs."""
        actuales = self.estado.get("trabajos") or trabajos
        self.subidas_actualizadas.emit({"trabajos": list(actuales)})

    # ── la conexión ────────────────────────────────────────────────

    def _conectar(self):
        """Devuelve el cliente de AirVault, abriendo sesión si hace falta.

        La comprobación periódica reusa el mismo: la sesión se renueva sola
        cuando caduca, así que volver a abrirla cada dos minutos serían
        dos viajes al navegador para nada.
        """
        from app.airvault.client import ClienteHttp
        from app.airvault.session import abrir_sesion, comprobar_o_renovar

        cliente = self.estado.get("cliente")
        if cliente is not None:
            self._detectar_pendientes()
            self._preparar_buscador()
            return cliente

        def avisar_texto(texto: str) -> None:
            self._avisar(texto, 0, 0)

        self._avisar("Entrando a AirVault", 0, 0)
        sesion = abrir_sesion(
            self.estado["config"], cookie=self.estado.get("cookie") or None,
            avisar=avisar_texto,
        )
        # Comprobar antes de subir nada: si la sesión guardada ya no vale,
        # esto vuelve a abrir el navegador en lugar de morir en la primera
        # página con un mensaje que manda a copiar cookies a mano.
        self._avisar("Revisando la sesión de AirVault", 0, 0)
        comprobar_o_renovar(sesion, avisar=avisar_texto)
        self._avisar(f"Sesión tomada del {sesion.origen}", 0, 0)
        cliente = ClienteHttp(sesion, self.estado["config"])
        self.estado["cliente"] = cliente
        self.estado["sesion"] = sesion
        self._detectar_pendientes()
        self._preparar_buscador()
        return cliente

    def _preparar_buscador(self) -> None:
        """Deja lista la consulta a Web Search de esta ejecución.

        Es lo que ve un batch que ya se completó: completarlo lo saca de la
        cola de Web Index, así que a partir de ese momento ninguna consulta
        a la cola lo encuentra y nada impedía volver a subirlo. Se construye
        una vez y lo comparten todas las cargas: descubrir la ruta cuesta
        varias peticiones y no cambia entre batches.
        """
        from app.airvault.config import AIRVAULT_FILENAME
        from app.airvault.flujo import buscador_de

        if self.estado.get("buscador") is not None:
            return
        sesion = self.estado.get("sesion")
        if sesion is None:
            return
        self.estado["buscador"] = buscador_de(
            sesion,
            self.estado["config"],
            Path(self.estado["raiz"]) / AIRVAULT_FILENAME,
        )

    def _buscar_websearch(self) -> None:
        """Consulta los batches en paralelo, con conexiones de Web Search propias."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from copy import copy
        from dataclasses import is_dataclass, replace
        from datetime import datetime
        from threading import Lock
        from app.airvault.confirmacion import MAX_BATCHES_PARALELOS, METODO_MUESTRA, huella_de_numeros, verificar_batch
        from app.airvault.flujo import Trabajo, websearch_confirmacion_valida
        from app.airvault.manifest import copiar_confirmacion, guardar_confirmacion
        from app.airvault.session import ErrorDeSesion, SesionAirVault
        from app.airvault.websearch import Buscador

        buscador = self.estado.get("buscador")
        if buscador is None:
            sesion = self.estado.get("sesion_base") or self.estado.get("sesion")
            if sesion is None:
                from app.airvault.session import abrir_sesion
                sesion = abrir_sesion(
                    self.estado["config"], cookie=self.estado.get("cookie") or None,
                    avisar=lambda texto: self._avisar(texto, 0, 0),
                    websearch=True,
                )
            buscador = Buscador(sesion, self.estado["config"],
                               ruta_config=Path(self.estado["raiz"]) / AIRVAULT_FILENAME)

        def con_sesion(base, sesion):
            if is_dataclass(base):
                return replace(base, sesion=sesion, _memoria={})
            nuevo = copy(base)
            nuevo.sesion = sesion
            return nuevo

        if callable(getattr(buscador.sesion, "clonar", None)):
            lectura = buscador.sesion.clonar(renovable=True, cancelacion_independiente=True)
            lectura.config = lectura.config.with_overrides(timeout_s=5.0, reintentos=1)
            buscador = con_sesion(buscador, lectura)
        self.estado["sesion"] = buscador.sesion
        self.estado["buscador"] = buscador
        self._lecturas_websearch.append(buscador.sesion)
        acceso = None
        if isinstance(buscador.sesion, SesionAirVault):
            from app.airvault.websearch_pagina import AccesoWebSearch
            self._avisar("Abriendo Web Search e iniciando sesión", 0, 0)
            acceso = AccesoWebSearch(buscador.config, buscador.sesion,
                                     avisar=lambda texto: self._avisar(texto, 0, 0),
                                     cancelado=self.hay_que_parar)
            try:
                acceso.__enter__()
            except BaseException:
                buscador.sesion.http.close()
                self._lecturas_websearch.clear()
                raise
        trabajos = [t for t in self.estado["buscar_trabajos"]
                    if not t.manifiesto.cancelado and not websearch_confirmacion_valida(t.manifiesto)]
        candado = Lock()
        revisados = confirmados = 0

        def cerrar(sesion):
            http = getattr(sesion, "http", None)
            if http is not None:
                http.close()

        def consultar(trabajo):
            if self.hay_que_parar():
                raise SesionCancelada("Se canceló la búsqueda en Web Search")
            copia = Trabajo(trabajo.config, trabajo.carpeta, trabajo.manifiesto.model_copy(deep=True))
            with candado:
                if acceso is not None:
                    local = acceso.crear_lector()
                elif callable(getattr(buscador.sesion, "clonar", None)):
                    sesion = buscador.sesion.clonar(renovable=False)
                    local = con_sesion(buscador, sesion)
                    self._lecturas_websearch.append(sesion)
                else:
                    local = con_sesion(buscador, buscador.sesion)
            try:
                try:
                    resultado = verificar_batch(local, copia, self._avisar,
                                                presupuesto_s=60.0, cancelado=self.hay_que_parar)
                except ErrorDeSesion:
                    if acceso is None:
                        raise
                    local.cerrar()
                    acceso.renovar(local.generacion)
                    local = acceso.crear_lector()
                    resultado = verificar_batch(local, copia, self._avisar,
                                                presupuesto_s=60.0, cancelado=self.hay_que_parar)
                return copia, resultado, local
            finally:
                if acceso is not None:
                    local.cerrar()
                elif local.sesion is not buscador.sesion:
                    cerrar(local.sesion)
                    with candado:
                        self._lecturas_websearch.remove(local.sesion)

        try:
            with ThreadPoolExecutor(max_workers=min(MAX_BATCHES_PARALELOS, max(1, len(trabajos))),
                                    thread_name_prefix="websearch-batch") as pool:
                futuros = {pool.submit(consultar, t): t for t in trabajos}
                for futuro in as_completed(futuros):
                    trabajo = futuros[futuro]
                    try:
                        copia, resultado, local = futuro.result()
                    except SesionCancelada:
                        raise
                    except Exception as exc:
                        trabajo.manifiesto.websearch_detalle = mensaje_error(exc, "No se pudieron consultar sus bitácoras en Web Search.")
                        revisados += 1
                        self.buscado.emit(dict(resultados=[(trabajo, False)], revisados=revisados,
                                               confirmados=confirmados, total=len(trabajos), terminado=False,
                                               automatico=bool(self.estado.get("confirmacion_automatica"))))
                        continue
                    with candado:
                        for campo in ("_ruta", "_plantilla", "_candidatas"):
                            if getattr(local, campo, None):
                                setattr(buscador, campo, getattr(local, campo))
                    momento = datetime.now().isoformat(timespec="microseconds")
                    datos = dict(websearch_confirmado=momento if resultado.confirmado else "",
                                 websearch_muestra=list(resultado.muestra), websearch_detalle=resultado.detalle,
                                 websearch_metodo=METODO_MUESTRA, websearch_batch_id=resultado.batch_id,
                                 websearch_revision=momento, websearch_cotejadas=resultado.encontradas,
                                 websearch_huella=huella_de_numeros(copia.manifiesto), batch_id=resultado.batch_id)
                    confirmado = resultado.confirmado
                    try:
                        actual = guardar_confirmacion(datos, trabajo.carpeta)
                    except (OSError, ValueError) as exc:
                        trabajo.manifiesto.websearch_detalle = str(exc) + " Se verificará de nuevo."
                        confirmado = False
                    else:
                        copiar_confirmacion(actual, trabajo.manifiesto)
                    revisados += 1
                    confirmados += confirmado
                    self.buscado.emit(dict(resultados=[(trabajo, confirmado)], revisados=revisados,
                                           confirmados=confirmados, total=len(trabajos), terminado=False,
                                           automatico=bool(self.estado.get("confirmacion_automatica"))))
        finally:
            if acceso is not None:
                acceso.__exit__(None, None, None)
            cerrar(buscador.sesion)
            self._lecturas_websearch.clear()
        self.buscado.emit(dict(resultados=[], revisados=revisados, confirmados=confirmados,
                               total=len(trabajos), terminado=True,
                               automatico=bool(self.estado.get("confirmacion_automatica"))))

    def _detectar_pendientes(self) -> None:
        """Agrega trabajos de otras ejecuciones, con los no subidos primero.

        Solo cuando alguien lo pidió con «Subir a AirVault». Lo que arranca
        solo (la cadena automática y el reloj de comprobación) se queda en
        la ejecución elegida: nadie está delante, y mandar a AirVault los
        batches de otro día sin pedirlo es como acaban subidos dos veces.
        """
        if not self.estado.get("recuperar_pendientes"):
            return
        from app.airvault.flujo import (CARPETA_TRABAJOS, SIN_SUBIR,
                                        cargar_trabajos_pendientes,
                                        estado_local)

        actuales = list(self.estado.get("trabajos") or [])
        conocidos = {str(t.carpeta.resolve()).casefold() for t in actuales}
        encontrados = cargar_trabajos_pendientes(
            self.estado["config"], Path(self.estado["raiz"]) / CARPETA_TRABAJOS,
        )
        nuevos = [
            t for t in encontrados
            if str(t.carpeta.resolve()).casefold() not in conocidos
        ]
        if nuevos:
            actuales.extend(nuevos)
            self._avisar(
                f"Se recuperaron {len(nuevos)} batches pendientes de "
                "ejecuciones anteriores", 0, 0,
            )
        # Partición estable: conserva el orden de cada ejecución, pero ningún
        # batch ya subido se adelanta a una fila que todavía requiere carga.
        actuales.sort(key=lambda trabajo: estado_local(trabajo).estado != SIN_SUBIR)
        self.estado["trabajos"] = actuales

    # ── subir ──────────────────────────────────────────────────────

    def _subir(self) -> None:
        from app.airvault.flujo import comprobar_entrega, preparar_partes
        from app.airvault.mapping import FLOTA_CACHE_FILENAME, ResolutorFlota

        estado = self.estado
        raiz = Path(estado["raiz"])
        resolutor = ResolutorFlota.load(raiz / FLOTA_CACHE_FILENAME)
        ejecuciones = estado.pop("ejecuciones_subida", None) or [estado]
        trabajos = []
        # Se prepara toda la seleccion antes de enviar. Cada ejecucion
        # conserva su nombre, fecha y memoria de lo que ya se subio.
        for ejecucion in ejecuciones:
            csv = Path(ejecucion["csv"])
            self._avisar(f"Leyendo la ejecución {csv.parent.parent.name}", 0, 0)
            entrega = comprobar_entrega(csv)
            cuantas = sum(len(p.paginas) for p in entrega)
            archivos = ("1 archivo" if len(entrega) == 1
                        else f"{len(entrega)} archivos")
            self._avisar(f"{cuantas} páginas en {archivos} de entrega", 0, 0)
            trabajos.extend(preparar_partes(
                estado["config"], Path(ejecucion["carpeta_job"]), csv,
                ejecucion["nombre_lote"], resolutor=resolutor,
                paginas_por_batch=estado["paginas_por_batch"],
                avisar=self._avisar,
                fin_de_mes=ejecucion.get("fin_de_mes", False),
            ))
        estado["trabajos"] = trabajos
        for trabajo in trabajos:
            manifiesto = trabajo.manifiesto
            self._avisar(
                f"Batch «{manifiesto.nombre_batch}»: "
                f"{len(manifiesto.bitacoras())} bitácoras y "
                f"{len(manifiesto.separadores())} separadores", 0, 0,
            )

        cliente = self._conectar()
        # _conectar agrega también los manifiestos pendientes de otras
        # ejecuciones. La variable local todavía contenía solo la ejecución
        # seleccionada y por eso esas filas se mostraban pero no se subían.
        trabajos = list(estado.get("trabajos") or trabajos)
        estado["trabajos"] = trabajos
        self._enviar(trabajos, cliente)

    def _enviar(self, por_subir, cliente) -> None:
        """Manda a Quick Upload lo que falte e indexa cada batch confirmado.

        Es el mismo camino para la subida inicial y para la reanudación
        automática: la lista de archivos cambia, pero no lo que se hace con
        ellos. Todo va en este mismo hilo: se sube un archivo y, mientras
        AirVault lo arma, se escribe, verifica y completa un batch ya
        confirmado; cuando el archivo queda confirmado sale el siguiente.
        Las páginas de cada batch se leen y escriben repartidas entre varias
        conexiones (ver :mod:`app.airvault.carriles`).

        Antes el indexado corría en un segundo hilo con una copia de la
        sesión mientras el siguiente archivo subía. Las dos copias llevaban
        la misma cookie de sesión de AirVault, que atiende de una en una las
        peticiones de una misma sesión: una carga larga dejaba al indexado
        esperando hasta agotar sus intentos, y el batch quedaba sin escribir.
        Ahora nada se indexa mientras un archivo sube: solo en la espera.
        """
        from app.airvault.flujo import (BUSCANDO, SOLO_REVISAR, estado_local,
                                        subir_partes)

        estado = self.estado
        raiz = Path(estado["raiz"])
        trabajos = list(estado.get("trabajos") or por_subir)
        self._notificar_subidas(trabajos)
        en_cola: set[str] = set()
        fallos_indexado: list[tuple[str, str]] = []

        def indexar_sin_cortar(trabajo) -> None:
            """Un batch que no se deja indexar no frena a los siguientes."""
            if not estado.get("indexar_al_encontrar"):
                return
            try:
                self._indexar_batch_encontrado(trabajo, cliente, raiz)
            except (TrabajoCancelado, SesionCancelada):
                raise
            except Exception as exc:  # noqa: BLE001 - se informa y siguen
                logger.opt(exception=exc).error(
                    "No se pudo indexar el batch {}: {}",
                    trabajo.manifiesto.batch_id, exc,
                )
                fallos_indexado.append((trabajo.manifiesto.nombre_batch, str(exc)))

        def al_encontrar(trabajo, _todos) -> None:
            """Publica el ID e indexa el batch confirmado.

            La tabla se repinta con la ejecucion entera, no con la lista
            reducida que se acaba de enviar: al reanudar solo lo pendiente,
            las filas ya subidas desaparecerian de la ventana.
            """
            from app.airvault.flujo import comprobar_partes

            try:
                remoto = comprobar_partes(
                    [trabajo], cliente, avisar=self._avisar
                )[0]
            except (TrabajoCancelado, SesionCancelada):
                raise
            except Exception as exc:  # noqa: BLE001 - se informa y siguen
                logger.opt(exception=exc).error(
                    "No se pudo revisar el batch {} antes de indexarlo: {}",
                    trabajo.manifiesto.batch_id, exc,
                )
                fallos_indexado.append(
                    (trabajo.manifiesto.nombre_batch, str(exc))
                )
                return
            self.batch_encontrado.emit({
                "trabajos": list(trabajos), "estado": remoto,
            })
            clave = str(trabajo.carpeta)
            if (
                not estado.get("indexar_al_encontrar")
                or not remoto.se_puede_indexar
                or clave in en_cola
            ):
                return
            en_cola.add(clave)
            indexar_sin_cortar(trabajo)

        # Los batches de la ejecución que ya están en AirVault y todavía no se
        # indexaron, y no vienen en esta tanda (la reanudación solo manda lo
        # que falta subir), entran en la misma cola que los confirmados de
        # la tanda: se indexan mientras AirVault arma la primera carga, o
        # enseguida si no hay ninguna. Antes esperaban a otra vuelta del
        # reloj, que tampoco los indexaba mientras quedaran cargas. Un
        # incompleto no entra aquí: lo retoma la revisión periódica. La
        # orden a mano sobre filas concretas («resubir») no toca otros.
        ya_confirmados = []
        if estado.get("indexar_al_encontrar") and not estado.get("forzados"):
            enviados = {str(trabajo.carpeta) for trabajo in por_subir}
            ya_confirmados = [
                trabajo for trabajo in trabajos
                if str(trabajo.carpeta) not in enviados
                and trabajo.manifiesto.batch_id
                and estado_local(trabajo).estado in (BUSCANDO, SOLO_REVISAR)
            ]
        # ``or []``: la suite sustituye subir_partes por dobles que no
        # devuelven nada, y esto no es motivo para tumbar una subida.
        fallos = subir_partes(
            list(por_subir), estado["sesion"], avisar=self._avisar,
            cliente=cliente, dormir=self._dormir,
            al_finalizar_subidas=self._notificar_subidas,
            al_encontrar=al_encontrar,
            en_la_ejecucion=trabajos,
            forzados=estado.get("forzados") or (),
            buscador=estado.get("buscador"),
            ya_confirmados=ya_confirmados,
        ) or []
        self.subido.emit({
            "trabajos": estado["trabajos"], "cliente": cliente,
            "sesion": estado.get("sesion"),
            # Cada carga que no salio, con su motivo. Sin esto el fallo solo
            # quedaba en el archivo de registro y la ventana decia «subida
            # terminada» de un archivo que nunca se envio.
            "fallos": [
                (trabajo.manifiesto.nombre_batch, detalle)
                for trabajo, detalle in fallos
            ],
            "fallos_indexado": fallos_indexado,
        })

    def _indexar_batch_encontrado(self, trabajo, cliente, raiz: Path) -> dict:
        """Planifica, escribe, verifica y cierra un batch recién confirmado."""
        from app.airvault.flujo import comprobar_memoria_de_libros
        from app.airvault.mapping import FLOTA_CACHE_FILENAME, ResolutorFlota

        self._avisar(
            f"Batch {trabajo.manifiesto.batch_id}: Preparando indexado",
            0,
            0,
        )

        def avisar_batch(texto: str, hechas: int, total: int) -> None:
            self._avisar(
                f"Batch {trabajo.manifiesto.batch_id}: {texto}",
                hechas,
                total,
            )

        resolutor = ResolutorFlota.load(raiz / FLOTA_CACHE_FILENAME)
        plan = trabajo.planificar(
            cliente, resolutor, avisar=avisar_batch
        )
        self.estado.setdefault("planes", {})[str(trabajo.carpeta)] = plan
        resolutor.guardar(raiz / FLOTA_CACHE_FILENAME)
        comprobar_memoria_de_libros(plan[1], raiz)
        datos = self._ejecutar_indexado(
            [trabajo], [plan], cliente,
            completar=bool(self.estado.get("completar")),
        )
        datos["trabajo"] = trabajo
        self.batch_indexado.emit(datos)
        return datos

    def _subir_pendientes(self) -> None:
        """Reanuda cargas locales sin volver a preparar la ejecución actual.

        Es por donde entra la reanudación de la comprobación periódica: los
        archivos que nunca llegaron a subirse y los que se dieron por
        perdidos vuelven a Quick Upload sin que nadie pulse nada. Y es
        también por donde entra la orden dada a mano desde la tabla, que
        llega con esos batches marcados en ``forzados`` para que se salten
        la comprobación larga.
        """
        estado = self.estado
        cliente = self._conectar()
        self._enviar(estado["pendientes_subida"], cliente)

    # ── comprobar ──────────────────────────────────────────────────

    def _comprobar(self) -> None:
        """Pregunta a AirVault y planifica lo que ya esté listo.

        Planificar es solo leer: abre el batch, lee sus páginas, calcula qué
        se escribiría y lo suelta. Se hace aquí, en cuanto una parte queda
        lista, para que la lista pueda decir «14 se escribirían, 5
        bloqueadas» en vez de un «listo» a secas.
        """
        from app.airvault.flujo import (INCOMPLETO, LISTO,
                                        comprobar_memoria_de_libros,
                                        comprobar_partes, detectar_indexados)
        from app.airvault.mapping import FLOTA_CACHE_FILENAME, ResolutorFlota

        estado = self.estado
        raiz = Path(estado["raiz"])
        planes: Dict[str, tuple] = estado.setdefault("planes", {})

        cliente = self._conectar()
        seleccionados = estado.pop("comprobar_trabajos", None)
        trabajos = (
            list(seleccionados)
            if seleccionados is not None
            else estado["trabajos"]
        )
        estados = comprobar_partes(trabajos, cliente, avisar=self._avisar)
        from app.airvault.flujo import DESCUADRADO
        from app.airvault.mezclas import recuperar_mezclas

        if any(p.estado == DESCUADRADO or p.trabajo.manifiesto.mezcla_pendiente for p in estados):
            if recuperar_mezclas(trabajos, cliente, self._avisar):
                estados = comprobar_partes(trabajos, cliente, avisar=self._avisar)
        recuperados = [
            t for t in trabajos if t.manifiesto.resubir_por_mezcla
        ]

        # Cada revision vuelve a leer las paginas. Asi se reconocen batches
        # indexados a mano y un plan calculado antes de esa intervencion no
        # intenta volver a escribir las paginas que ahora estan en verde.
        por_releer = {
            str(parte.trabajo.carpeta)
            for parte in estados
            if parte.estado in (LISTO, INCOMPLETO)
            and not parte.trabajo.manifiesto.solo_subir
        }
        estados = detectar_indexados(
            estados, cliente, avisar=self._avisar
        )
        for clave in por_releer:
            planes.pop(clave, None)

        resolutor = ResolutorFlota.load(raiz / FLOTA_CACHE_FILENAME)
        # Los amarillos que el reloj no va a reescribir en esta vuelta no se
        # planifican: planificar es leer el batch página por página, y se
        # hacía en cada vuelta para nada. El mapa de detectar_indexados ya
        # dijo si siguen amarillos.
        en_espera = set(estado.pop("no_planificar", None) or ())
        nuevos = 0
        for parte in estados:
            clave = str(parte.trabajo.carpeta)
            if (
                clave in planes or not parte.se_puede_indexar
                or (parte.estado == INCOMPLETO and clave in en_espera)
            ):
                continue
            self._avisar(
                f"Batch {parte.batch_id}: Preparando revisión", 0, 0
            )

            def avisar_batch(
                texto: str,
                hechas: int,
                total: int,
                batch_id: str = parte.batch_id,
            ) -> None:
                self._avisar(
                    f"Batch {batch_id}: {texto}", hechas, total
                )

            planes[clave] = parte.trabajo.planificar(
                cliente, resolutor, avisar=avisar_batch
            )
            comprobar_memoria_de_libros(planes[clave][1], raiz)
            nuevos += 1
        if nuevos:
            resolutor.guardar(raiz / FLOTA_CACHE_FILENAME)

        # Lo planificado de toda la ejecución, que es de donde sale el
        # recuento de «se escribirían / bloqueadas» del resumen.
        partes = [
            (t.manifiesto.nombre_batch, planes[str(t.carpeta)][0])
            for t in trabajos if str(t.carpeta) in planes
        ]
        self.comprobado.emit({
            "estados": estados, "planes": planes, "partes": partes,
            "recuperados": recuperados,
            "cliente": cliente,
            "acotado": seleccionados is not None,
        })

    # ── indexar ────────────────────────────────────────────────────

    def _indexar(self) -> None:
        from app.airvault.flujo import cerrar_partes, completar_partes
        from app.airvault.indexer import Resultado

        estado = self.estado
        cliente = estado["cliente"]
        trabajos = list(estado["listos"])
        # Batches ya verificados que esperan su cierre. Antes solo se cerraban
        # cuando no quedaba ningún otro por escribir, así que uno amarillo en
        # la ejecución dejaba a los demás sin completar indefinidamente.
        tambien = [
            trabajo for trabajo in estado.pop("completar_tambien", None) or ()
            if trabajo not in trabajos
        ]
        planes = [estado["planes"][str(t.carpeta)] for t in trabajos]
        datos: dict = {
            "resultado": Resultado(), "validas": 0, "total": 0,
            "lotes": len(trabajos), "cierres": [], "incompleto": False,
            "incluye_revision": False, "carpetas": [], "amarillas": {},
        }
        if tambien and bool(estado.get("completar")):
            try:
                datos["cierres"] = completar_partes(
                    tambien, cliente, avisar=self._avisar, automatico=True
                )
            finally:
                cerrar_partes(tambien, cliente)
        # Batch por batch: cada uno se escribe, se verifica y, si procede, se
        # completa antes de empezar el siguiente. En tanda, el primero
        # esperaba a que todos los demás estuvieran escritos y verificados.
        for trabajo, plan in zip(trabajos, planes):
            if not estado.get("indexar_manual", True) and not estado.get("indexar_al_encontrar"):
                self._avisar("Indexado automático desactivado; el batch en curso quedó guardado", 0, 0)
                datos["pausado"] = True
                break
            parte = self._ejecutar_indexado(
                [trabajo], [plan], cliente,
                completar=bool(estado.get("completar")),
            )
            suma, suyo = datos["resultado"], parte["resultado"]
            for atributo in (
                "escritas", "omitidas", "fallidas",
                "separadores_borrados", "separadores_pendientes",
            ):
                setattr(
                    suma, atributo,
                    getattr(suma, atributo) + getattr(suyo, atributo),
                )
            suma.detalles.extend(suyo.detalles)
            suma.interrumpido = suyo.interrumpido
            datos["validas"] += parte["validas"]
            datos["total"] += parte["total"]
            datos["cierres"] = list(datos["cierres"]) + list(parte["cierres"])
            datos["incompleto"] = datos["incompleto"] or parte["incompleto"]
            datos["incluye_revision"] = (
                datos["incluye_revision"] or parte["incluye_revision"]
            )
            datos["carpetas"].extend(parte["carpetas"])
            datos["amarillas"].update(parte.get("amarillas") or {})
            if suyo.interrumpido:
                # Sin sesión o sin red los siguientes fallarían igual; lo
                # que falta se retoma en la siguiente revisión.
                break
        datos["acotado"] = bool(estado.pop("indexar_acotado", False))
        self.indexado.emit(datos)

    def _ejecutar_indexado(
        self, trabajos, planes, cliente, completar: bool = False,
    ) -> dict:
        """Escribe y verifica uno o varios batches con el mismo reintento."""
        from app.airvault.flujo import (cerrar_partes, completar_partes,
                                        comprobar_tanda_de_libros,
                                        indexar_partes, planificar_partes,
                                        verificar_partes)
        from app.airvault.indexer import FALLOS_DE_CAMINO, Resultado

        def esperar_confirmacion(segundos: float) -> None:
            self._avisar(
                f"Páginas aún sin confirmar; se releen en {segundos:.0f} s",
                0, 0,
            )
            self._dormir(segundos)

        cierres: list = []
        resultado = Resultado()
        validas = total = 0
        faltan_previas: Optional[int] = None
        # Cada batch lleva su propia cuenta. Un batch confirmado sale del
        # reintento: releerlo y reescribirlo porque otro batch de la misma
        # tanda sigue amarillo solo gastaba tiempo.
        por_batch: dict[str, tuple[int, int, list[str]]] = {}
        activos = list(trabajos)
        try:
            for intento in range(1, INTENTOS_INDEXADO + 1):
                # Una página que sigue fallando tras sus reintentos se anota
                # y se sigue: el fallo es de AirVault, no de los datos, y ni
                # el resto del batch ni los demás batches tienen que esperar.
                # La vuelta siguiente la vuelve a intentar.
                parcial = indexar_partes(
                    activos, planes, detener_en_error=False,
                    avisar=self._avisar,
                    al_indexar=self.batch_indexando.emit,
                )
                for atributo in (
                    "escritas", "omitidas", "fallidas",
                    "separadores_borrados", "separadores_pendientes",
                ):
                    setattr(
                        resultado, atributo,
                        getattr(resultado, atributo) + getattr(parcial, atributo),
                    )
                resultado.detalles.extend(parcial.detalles)
                resultado.interrumpido = parcial.interrumpido
                self._avisar("Verificando batches", 0, 0)
                # Una lectura sola no basta para dar una página por
                # incompleta: recién escrito el batch, AirVault puede
                # devolverla sin algún obligatorio que sí guardó.
                for trabajo in activos:
                    por_batch[str(trabajo.carpeta)] = verificar_partes(
                        [trabajo], cliente, avisar=self._avisar,
                        esperas=() if parcial.interrumpido
                        else ESPERAS_CONFIRMACION[intento - 1],
                        dormir=esperar_confirmacion,
                    )
                validas = sum(cuenta[0] for cuenta in por_batch.values())
                total = sum(cuenta[1] for cuenta in por_batch.values())
                _problemas = [
                    problema for cuenta in por_batch.values()
                    for problema in cuenta[2]
                ]
                if validas == total:
                    break
                # Una escritura que se corto a la mitad es justo la que hay
                # que retomar: lo escrito queda escrito y el plan siguiente
                # solo mira las que no quedaron en verde. Antes se
                # abandonaba aqui mismo y el batch se quedaba a medias
                # esperando a que alguien lo repitiera a mano. Ahora se deja
                # cuando una pasada entera no reduce las que faltan, que es
                # la senal de que reintentar ya no va a cambiar nada.
                faltan = total - validas
                if faltan_previas is not None and faltan >= faltan_previas:
                    break
                faltan_previas = faltan
                if intento < INTENTOS_INDEXADO:
                    self._avisar(
                        f"Reintentando páginas sin confirmar "
                        f"({intento + 1}/{INTENTOS_INDEXADO})", 0, 0,
                    )
                    resolutor = planes[0][1].resolutor if planes else None
                    activos = [
                        trabajo for trabajo in activos
                        if por_batch[str(trabajo.carpeta)][0]
                        != por_batch[str(trabajo.carpeta)][1]
                    ]
                    try:
                        planes = planificar_partes(
                            activos, cliente, resolutor=resolutor,
                            avisar=self._avisar,
                        )
                    except FALLOS_DE_CAMINO as exc:
                        # Sin sesion no hay plan nuevo que valga. Lo escrito
                        # sigue escrito y la ronda siguiente lo retoma desde
                        # donde quedo, asi que no se pierde el avance.
                        resultado.interrumpido = str(exc)
                        break
                    for trabajo, plan in zip(activos, planes):
                        self.estado.setdefault("planes", {})[
                            str(trabajo.carpeta)
                        ] = plan
            if validas != total:
                resultado.detalles.extend(_problemas)
                for problema in _problemas[:5]:
                    self._avisar(mensaje_error(problema, "Quedan páginas por revisar en AirVault."), 0, 0)
            # La casilla sigue activa mientras se escribe. Se consulta al
            # llegar al cierre para que un cambio hecho durante el indexado
            # se aplique a este mismo trabajo.
            completar_ahora = bool(self.estado.get("completar", completar))
            # Cada batch se cierra en cuanto el suyo quedó confirmado. Exigir
            # la tanda entera en verde dejaba sin completar batches enteros
            # por una sola página amarilla de otro.
            confirmados = [
                trabajo for trabajo in trabajos
                if trabajo.manifiesto.etapa_hecha("verificar")
            ]
            if completar_ahora and confirmados:
                cierres = completar_partes(
                    confirmados, cliente, avisar=self._avisar, automatico=True
                )
                # El cierre lee el mapa del batch por otra ruta, y recién
                # verificado puede ver aún en amarillo lo que ya está verde.
                # Se reintenta solo el que no se cerró por páginas; el que
                # no se cierra por otro motivo no mejora esperando.
                for espera in ESPERAS_CIERRE:
                    pendientes = [
                        trabajo for trabajo, cierre in cierres
                        if not cierre.completado and cierre.bloqueadas
                    ]
                    if not pendientes:
                        break
                    self._avisar(
                        "AirVault aún no deja completar; nuevo intento en "
                        f"{espera:.0f} s", 0, 0,
                    )
                    self._dormir(espera)
                    nuevos = {
                        str(trabajo.carpeta): (trabajo, cierre)
                        for trabajo, cierre in completar_partes(
                            pendientes, cliente, avisar=self._avisar,
                            automatico=True,
                        )
                    }
                    cierres = [
                        nuevos.get(str(trabajo.carpeta), (trabajo, cierre))
                        for trabajo, cierre in cierres
                    ]
        finally:
            # Escribir toma el batch y lo suelta al terminar; esto es la red
            # de seguridad para cuando algo se corta por el medio. Un batch
            # que queda tomado no da error: cuelga la próxima vez que
            # alguien lo abra.
            cerrar_partes(trabajos, cliente)

        # El plan ya comprobo la memoria, pero solo contra los libros de
        # este batch. A los viejos no los mira nadie mas que esto. Va al
        # final y no antes porque el batch ya esta escrito y soltado: lo
        # que tarde o falle aqui no le quita nada al trabajo de la corrida.
        #
        # Una sola vez por ejecucion aunque se indexen muchos batches. La
        # tanda existe para repartir las peticiones entre dias, y dispararla
        # en cada batch de una cadena larga seria la rafaga que evita. La
        # marca se pone antes de preguntar: si falla, no se reintenta en
        # cada batch que venga detras.
        def avisar_tanda(hechos: int, total: int) -> None:
            self._avisar("Revisando la memoria de libros", hechos, total)

        if not self.estado.get("tanda_hecha"):
            self.estado["tanda_hecha"] = True
            comprobar_tanda_de_libros(
                self.estado.get("buscador"), self.estado.get("raiz"),
                avisar=avisar_tanda,
            )
        return {
            "resultado": resultado, "validas": validas, "total": total,
            "lotes": len(trabajos), "cierres": cierres,
            "incluye_revision": any(t.manifiesto.solo_subir for t in trabajos),
            "incompleto": validas != total,
            "carpetas": [str(t.carpeta) for t in trabajos],
            # Lo que quedó sin confirmar en cada batch. Con esto la ventana
            # sabe si volver a intentarlo está sirviendo.
            "amarillas": {
                clave: cuenta[1] - cuenta[0]
                for clave, cuenta in por_batch.items()
            },
        }

    def _completar(self) -> None:
        """Cierra batches ya verificados sin reescribir sus paginas."""
        from app.airvault.flujo import cerrar_partes, completar_partes
        from app.airvault.indexer import Resultado

        estado = self.estado
        acotado = bool(estado.pop("completar_acotado", False))
        cliente = self._conectar()
        trabajos = list(estado["por_completar"])
        # Lo que arranca solo obedece a «Completar batch» en el momento de
        # cerrar, como el cierre que sigue al indexado. La orden dada desde
        # el menú sobre filas concretas es explícita y no depende de ella.
        if not acotado and not bool(estado.get("completar")):
            trabajos = []
        try:
            cierres = completar_partes(
                trabajos, cliente, avisar=self._avisar, automatico=True
            )
        finally:
            cerrar_partes(trabajos, cliente)
        total = sum(len(t.manifiesto.bitacoras()) for t in trabajos)
        self.indexado.emit({
            "resultado": Resultado(), "validas": total, "total": total,
            "lotes": len(trabajos), "cierres": cierres,
            "incompleto": False,
            "acotado": acotado,
        })


def _soltar(trabajos, cliente) -> None:
    """Suelta los batches y se calla si no puede: es limpieza, no trabajo."""
    from app.airvault.flujo import cerrar_partes

    try:
        cerrar_partes(trabajos, cliente)
    except Exception:  # noqa: BLE001 - soltando no se avisa de nada
        pass


class SoltarLotesWorker(QThread):
    """Suelta los batches fuera del hilo de la ventana.

    Es una petición por batch contra un servidor que puede tardar un minuto
    en contestar. En el hilo de la ventana, cambiar de ejecución o cancelar
    la dejaba congelada todo ese rato.
    """

    def __init__(self, trabajos, cliente, parent=None) -> None:
        super().__init__(parent)
        self._trabajos = trabajos
        self._cliente = cliente

    def run(self) -> None:  # noqa: D102 - lo describe la clase
        _soltar(self._trabajos, self._cliente)


class AirVaultWindow(QDialog):
    """Ventana aparte que sube al Web Index una ejecución del historial."""

    abrir_corrida_paralela = Signal(str)
    # Paso de la cadena y en qué quedó, para la línea de pasos de la
    # ventana principal. Los cuatro últimos pasos del proceso automático
    # ocurren aquí, así que sin esto aquella línea se quedaba en «Exportar»
    # y no había forma de saber desde allí si la entrega llegó a subirse.
    avance_automatico = Signal(str, str)
    proceso_terminado = Signal(str)

    def __init__(
        self, raiz: Path, opciones: OpcionesAutomatizacion | None = None
    ) -> None:
        # Aunque conserva QDialog por su comportamiento de cierre, se crea
        # como ventana nativa normal y sin dueño. En Windows, los diálogos
        # parentados no tienen entrada propia en la barra de tareas y su marco
        # puede quedar desincronizado del contenido al minimizar o restaurar.
        super().__init__(None, Qt.WindowType.Window)
        self._raiz = Path(raiz)
        # Los pasos del proceso automático se eligen en la ventana principal
        # y esta ventana los obedece. Compartir el objeto es lo que hace que
        # «Completar batch» y la espera valgan lo mismo en los dos sitios;
        # abierta por su cuenta (una prueba, un arranque suelto) se lee la
        # misma memoria portable, así que tampoco cambia nada.
        self._opciones = opciones or OpcionesAutomatizacion(self._raiz, self)
        self._opciones.cambiado.connect(self._al_cambiar_automatizacion)
        self._worker: Optional[TrabajoAirVaultWorker] = None
        # Conserva el alcance con el que arrancó el hilo. La casilla de
        # visualización se puede cambiar mientras trabaja, pero desmarcarla
        # no debe hacer desaparecer los batches que el hilo no recibió.
        self._worker_filtrado: Optional[bool] = None
        self._indexando: set[str] = set()
        # Todo lo que el hilo necesita y devuelve: la conexión abierta, los
        # trabajos de cada parte y los planes ya calculados. Vive aquí para
        # que la comprobación periódica reuse la sesión en vez de volver al
        # navegador cada cinco minutos.
        self._estado: dict = {}
        self._trabajos: list = []
        self._estados: list = []
        self._config = AirVaultConfig.load(self._raiz / AIRVAULT_FILENAME)
        self._listo_para_subir = False
        # El CSV de la ejecucion abierta. Lo pone la lista de arriba (o la
        # ventana principal al exportar), no se teclea y no se ensenya: el
        # nombre de la ejecucion elegida ya dice cual es, y la ruta debajo
        # solo repetia lo mismo ocupando una fila.
        self._corrida: str = ""
        self._corridas_marcadas: set[str] = set()
        # Reloj del paso en curso y último texto anotado, para no repetir
        # una línea por cada trozo de una subida.
        self._reloj: Optional[QTimer] = None
        self._inicio_paso = time.monotonic()
        self._ultimo_paso = ""
        # La cuenta del paso en curso («30 de 120»), para la línea viva.
        self._cuenta_paso = (0, 0)
        # Lo que lleva el paso en curso de cada batch que está subiendo o
        # escribiendo, por carpeta: (etapa, fracción). Es lo que deja a la
        # barra moverse dentro de un batch y no solo al cambiar de estado.
        self._en_vuelo: dict[str, tuple[str, float]] = {}
        self._avance_por_batch: dict[tuple[str, bool], float] = {}
        self._alcance_proceso: Optional[set[str]] = None
        # El final se anuncia despues de una comprobacion buena y del ultimo
        # hilo, una sola vez para la misma cola y meta.
        self._fin_pendiente = False
        self._resultado_fallido = False
        self._fin_confirmado: Optional[tuple] = None
        # La última línea de la bitácora mientras hay algo en marcha: gira
        # mientras trabaja y cuenta lo que falta mientras espera. Sin ella no
        # había forma de distinguir un trabajo largo de uno ya parado.
        self._linea_viva: Optional[QListWidgetItem] = None
        self._latido: Optional[QTimer] = None
        # El que pregunta solo por los batches cada tantos minutos.
        self._vigilante: Optional[QTimer] = None
        self._confirmador: Optional[QTimer] = None
        self._worker_websearch: Optional[TrabajoAirVaultWorker] = None
        self._estado_websearch: dict = {}
        self._websearch_revisados: dict = {}
        self._websearch_pendientes: dict = {}
        self._websearch_manual_pendiente = False
        self._cuenta_websearch = (0, 0)
        self._websearch_inicio: Optional[set[str]] = None
        self._websearch_inicio_por_revisar: set[str] = set()
        self._websearch_proceso_activo = False
        self._deteniendo = False
        # Fallos seguidos sin ninguna comprobación buena por medio. Es lo
        # que separa un tropiezo de AirVault de un problema que tarda en
        # arreglarse; ver `FALLOS_SEGUIDOS_ANTES_DE_ESPACIAR`.
        self._fallos_seguidos = 0
        # Encadena una comprobacion en cuanto termine lo que esta en vuelo:
        # subir e indexar dejan la lista desactualizada.
        self._comprobar_al_terminar = False
        self._subir_al_terminar = False
        self._indexar_al_terminar = False
        # Batches que la cadena automática ya mandó a Quick Upload en este
        # ciclo. Una subida que falla deja la fila otra vez en «sin subir», y
        # sin esta marca comprobar y subir se llamarían el uno al otro sin
        # parar. Se vacía en cada vuelta del reloj y en cada acción manual.
        self._subidas_del_ciclo: set[str] = set()
        # La cadena en curso la pidió alguien con un botón («Revisar en
        # AirVault», «Subir», «Continuar pendiente»). Entonces se sube lo que
        # falte aunque la revisión periódica esté apagada: la orden es
        # terminar lo pendiente. El reloj lo apaga en cada vuelta.
        self._cadena_manual = False
        # Acciones pedidas desde la tabla mientras habia algo en vuelo.
        # Se van lanzando en orden segun el hilo queda libre.
        self._cola_de_acciones: list[tuple[str, list]] = []
        self._indexado_incompleto = False
        # Comprobaciones que le quedan a cada batch (por carpeta) que terminó
        # el indexado sin confirmar. Mantienen vivo el reloj aunque no quede
        # nada más que esperar; ver `RECONFIRMACIONES_TRAS_INDEXAR`.
        self._reconfirmaciones: dict[str, int] = {}
        # Páginas que quedaron sin confirmar en el último intento de cada
        # batch, para saber si reintentar está sirviendo.
        self._amarillas: dict[str, int] = {}
        self._cierres_fallidos: dict[str, tuple[int, float]] = {}
        # Reintentos espaciados ya hechos de cada batch que gastó sus
        # comprobaciones, y cuándo fue el último (``time.monotonic``); ver
        # `MINUTOS_ENTRE_REINTENTOS_AMARILLOS`.
        self._reintentos_espaciados: dict[str, tuple[int, float]] = {}
        # Solo «Subir a AirVault» recupera batches de ejecuciones
        # anteriores, y solo para la acción que lanza. Ver `_subir_a_mano`.
        self._recuperar_pendientes = False
        # Cerrar con trabajo en vuelo no bloquea: se pide la cancelación y
        # la ventana se va en cuanto el hilo suelta lo que tenía tomado.
        self._cerrar_al_terminar = False
        # Hilos que están soltando batches en AirVault, para que Qt no los
        # destruya a media petición.
        self._soltando: list[QThread] = []
        # Ventanas de consulta abiertas desde aquí (la vista previa y las
        # listas de bitácoras). No tienen dueño en Qt, así que esto es lo
        # único que las mantiene vivas.
        self._ventanas_de_consulta: list = []
        # La bitácora que se buscó en la cola y los batches que la llevan.
        # Se guardan porque la tabla se repinta sola cada vez que cambia el
        # estado de un batch, y el resaltado hay que devolverlo.
        self._bitacora_buscada = ""
        self._hallazgos: list = []
        self._posicion_hallazgo = -1

        self.setWindowTitle("Indexar en AirVault")
        # Con botón de minimizar: escribir una ejecución entera tarda, y
        # mientras tanto se sigue trabajando en la ventana principal.
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        # Como el resto de las ventanas: el tamaño lo pone la pantalla, que
        # en un portátil bajo dejaría los botones fuera del borde.
        # El alto pedido deja sitio a la bitácora, que es lo que se lee
        # mientras trabaja; lo que no quepa lo recorta la pantalla.
        # La densidad viaja como atributo porque la piden las piezas que se
        # construyen luego: lo que ocupan las tablas, la bitácora y el
        # resumen es lo que decide si la ventana entra en un escritorio bajo.
        self._densidad = fit_to_screen(self, 780, 800)
        self._aplicar_hoja()
        self._build_ui()
        self._cargar_sidebar()
        # La hoja lleva el fragmento de la densidad, así que no la puede
        # rehacer el módulo del tema: la vuelve a pedir la ventana.
        gestor_tema().cambiado.connect(self._al_cambiar_tema)

    def _aplicar_hoja(self) -> None:
        """La hoja de la ventana, con los tonos y las medidas de ahora."""
        self.setStyleSheet(
            window_stylesheet(data_table_qss() + self._densidad.qss)
            + self._hoja_sidebar()
        )

    @staticmethod
    def _hoja_sidebar() -> str:
        c = paleta()
        return (
            f"QSplitter#airvaultBatchesSplitter::handle:horizontal {{ "
            f"background: {c.TABLE_GRID}; margin: 0 {(SPACE_L - 2) // 2}px; "
            f"border-radius: 6px; }}"
            f"QSplitter#airvaultBatchesSplitter::handle:horizontal:hover {{ "
            f"background: {c.PANE_TEXT}; }}"
            f"QListWidget#batchesSidebar {{ background: {c.TABLE_BASE_BG}; "
            f"border: 1px solid {c.PANE_BORDER}; border-radius: 6px; "
            f"padding: {SPACE_XS}px; }}"
            f"QListWidget#batchesSidebar::item {{ padding: {SPACE_S}px; border-radius: 6px; "
            f"border: 1px solid {c.DIVIDER}; }}"
            f"QListWidget#batchesSidebar::item:hover:!selected {{ background: {c.PANE_CONTROL_BG}; }}"
            f"QListWidget#batchesSidebar::item:selected {{ background: {c.STROKE_STRONG}; "
            f"border: 1px solid {c.TEXT_SECONDARY}; color: {c.PANE_TEXT}; }}"
            f"QListWidget#batchesSidebar::item:selected:!active {{ background: {c.STROKE_STRONG}; "
            f"border: 1px solid {c.TEXT_SECONDARY}; color: {c.PANE_TEXT}; }}"
            f"QGroupBox#airvaultResultado {{ padding: {SPACE_XL}px {SPACE_M}px {SPACE_M}px; }}"
            f"QGroupBox#airvaultResultado::title {{ left: {SPACE_M}px; }}"
            f"QListWidget#airvaultBitacora {{ padding: {SPACE_S}px; }}"
            f"QListWidget#airvaultBitacora::item {{ padding: {SPACE_XS}px {SPACE_S}px; }}"
            f"QToolButton#spinStepButton {{ min-width: {SPACE_L + 2}px; "
            f"max-width: {SPACE_L + 2}px; padding: 0; }}"
        )

    def _al_cambiar_tema(self, _nombre: str) -> None:
        """Rehace la hoja de la ventana con los tonos del tema nuevo."""
        self._aplicar_hoja()
        for fila, parte in enumerate(self._partes_en_cola()):
            papel = papel_de_estado(parte.estado, str(parte.trabajo.carpeta) in self._indexando,
                                    revisar=parte.trabajo.manifiesto.solo_subir)
            self.lotes.item(fila).setForeground(QBrush(QColor(_color_de_papel(papel))) if papel else QBrush())
            for columna in range(4):
                if papel:
                    pintar_celda_del_tema(self.lotes.item(fila, columna), papel)

    # ── construcción ───────────────────────────────────────────────

    def _build_ui(self) -> None:
        cuerpo = QVBoxLayout(self)
        cuerpo.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        margen = max(SPACE_L, self._densidad.window_margin)
        cuerpo.setContentsMargins(margen, margen, margen, margen)
        cuerpo.setSpacing(SPACE_S)
        self._root_layout = cuerpo

        # Sin frase de bienvenida: la lista abre en «Seleccionar ejecución»
        # y eso ya dice lo que hay que hacer con ella, en el sitio donde se
        # hace. La línea de arriba solo repetía lo mismo y le quitaba alto a
        # la cola de batches, que es lo que se mira mientras trabaja.
        self.divisor_batches = QSplitter(Qt.Orientation.Horizontal)
        self.divisor_batches.setObjectName("airvaultBatchesSplitter")
        self.divisor_batches.setHandleWidth(SPACE_L)
        self.divisor_batches.setChildrenCollapsible(False)
        lateral = QWidget()
        lateral.setMinimumWidth(220)
        self.panel_batches = lateral
        sidebar = QVBoxLayout(lateral)
        sidebar.setContentsMargins(0, 0, 0, 0)
        sidebar.setSpacing(SPACE_S)
        filtros = QVBoxLayout()
        filtros.setSpacing(SPACE_XS)
        sidebar.addLayout(filtros)
        principal = QWidget()
        self._panel_formulario = principal
        contenido = QVBoxLayout(principal)
        contenido.setContentsMargins(0, 0, 0, 0)
        contenido.setSpacing(SPACE_M)
        ajustes = self._campos()
        self._formulario = ajustes
        ajustes.addWidget(self._historial(), 0, 0, 1, 3)
        contenido.addLayout(ajustes)
        self._resultado = self._recuadro_de_revision()
        ajustes.addWidget(self._resultado, 5, 0, 1, 3)
        self.solo_ejecucion_check = QCheckBox("Solo la ejecución seleccionada")
        self.solo_ejecucion_check.setToolTip(
            "Limita la cola y sus acciones a los batches de la ejecución seleccionada."
        )
        self.solo_ejecucion_check.toggled.connect(self._al_filtrar_ejecucion)
        # Trabajar sobre una sola ejecución o sobre toda la cola es una
        # manera de trabajar, no algo de esta sesión. Se repone con la señal
        # bloqueada porque la cola todavía no existe; al llenarla se pinta
        # ya filtrada.
        recordar(AIRVAULT, "solo_ejecucion", self.solo_ejecucion_check)
        filtros.addWidget(self.solo_ejecucion_check)
        self.ocultar_indexados_check = QCheckBox("Ocultar batches indexados")
        self.ocultar_indexados_check.setToolTip("Oculta los batches con el indexado terminado y el batch aún abierto.")
        self.ocultar_completados_check = QCheckBox("Ocultar batches completados")
        self.ocultar_completados_check.setToolTip("Oculta los batches completados, también los confirmados en Web Search.")
        for clave, control in (("ocultar_indexados", self.ocultar_indexados_check),
                               ("ocultar_completados", self.ocultar_completados_check)):
            recordar(AIRVAULT, clave, control)
            control.toggled.connect(self._al_filtrar_vista)
            filtros.addWidget(control)
        sidebar.addLayout(self._cabecera_de_lotes())
        sidebar.addWidget(self._lotes(), 1)
        sidebar.addWidget(self._respuesta_de_la_busqueda())
        sidebar.addLayout(self._fila_avance())
        self._fila_vigilancia(ajustes, 6)
        self._fila_politica_duplicados(ajustes, 8)
        acciones = QHBoxLayout()
        acciones.setSpacing(SPACE_S)
        acciones.addStretch()
        for boton in (self.boton_automatizacion, self.boton_continuar, self.boton_reiniciar):
            acciones.addWidget(boton)
        contenido.addLayout(acciones)
        self._formulario_ancho = None
        self._distribuir_formulario(False)
        principal.installEventFilter(self)
        contenido.addWidget(self._bitacora(), 1)
        self.divisor_batches.addWidget(lateral)
        self.divisor_batches.addWidget(principal)
        self.divisor_batches.setStretchFactor(0, 0)
        self.divisor_batches.setStretchFactor(1, 1)
        self.divisor_batches.setSizes([235, 1200])
        cuerpo.addWidget(self.divisor_batches, 1)
        # La cola tiene prioridad: en pantallas bajas necesita mostrar
        # varios batches a la vez. El registro sigue siendo desplazable.

        self.resumen = ElidedLabel(TEXTO_SIN_SUBIR)
        pintar_del_tema(
            self.resumen, lambda: f"color: {color_ayuda()};"
        )
        self.resumen.setMinimumHeight(self.resumen.fontMetrics().height() + SPACE_S)
        self.resumen.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        cuerpo.addWidget(self.resumen)

        cuerpo.addLayout(self._fila_botones())

    @staticmethod
    def _titulo(texto: str) -> QLabel:
        etiqueta = QLabel(texto)
        etiqueta.setStyleSheet("font-weight: 600;")
        return etiqueta

    def eventFilter(self, objeto, evento) -> bool:  # noqa: N802 - API Qt
        if objeto is getattr(self, "_panel_formulario", None) and evento.type() == QEvent.Type.Resize:
            self._distribuir_formulario(objeto.width() >= ANCHO_FORMULARIO_DOBLE)
        return super().eventFilter(objeto, evento)

    def _distribuir_formulario(self, ancho: bool) -> None:
        """Alinea formulario y registro, con dos pares de campos si caben."""
        if ancho == self._formulario_ancho:
            return
        self._formulario_ancho = ancho
        grid = self._formulario
        while grid.count():
            grid.takeAt(0)
        for columna in range(4):
            grid.setColumnStretch(columna, 0)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1 if ancho else 0)
        self._resultado.setMinimumHeight(
            0 if ancho else SPACE_XL + SPACE_M + self.reparto_total.fontMetrics().lineSpacing() * 2
        )
        grid.addWidget(self.historial, 0, 0, 1, 4)
        campos = (self.lote_edit, self.limite_batch_control,
                  self.fecha_combo, self.cookie_edit)
        for indice, (etiqueta, campo) in enumerate(zip(self._etiquetas_campos, campos)):
            fila, columna = ((1 + indice // 2, 2 * (indice % 2)) if ancho else (1 + indice, 0))
            grid.addWidget(etiqueta, fila, columna)
            span = 1 if ancho else 3
            alineacion = (Qt.AlignmentFlag.AlignLeft if indice in (1, 2)
                          else Qt.AlignmentFlag(0))
            grid.addWidget(campo, fila, columna + 1, 1, span, alineacion)
        if ancho:
            grid.addWidget(self._resultado, 3, 0, 1, 4)
            grid.addWidget(self.auto_check, 4, 0)
            grid.addWidget(self.minutos_control, 4, 1, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(self.completar_check, 4, 2)
            grid.addWidget(self.detener_duplicados_check, 5, 0, 1, 2)
            grid.addWidget(self._etiqueta_duplicados, 5, 2)
            grid.addWidget(self.porcentaje_duplicados_control, 5, 3, Qt.AlignmentFlag.AlignLeft)
        else:
            grid.addWidget(self._resultado, 5, 0, 1, 4)
            grid.addWidget(self.auto_check, 6, 0)
            grid.addWidget(self.minutos_control, 6, 1, 1, 3, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(self.completar_check, 7, 0, 1, 4)
            grid.addWidget(self.detener_duplicados_check, 8, 0, 1, 4)
            grid.addWidget(self._etiqueta_duplicados, 9, 0)
            grid.addWidget(self.porcentaje_duplicados_control, 9, 1, 1, 3, Qt.AlignmentFlag.AlignLeft)
        if self.isVisible():
            self._acotar_a_la_pantalla()

    def _historial(self) -> QComboBox:
        """La lista de ejecuciones, la misma que la del visor de CSV.

        Era una tabla de tres columnas, y ese alto le hacía falta a la cola
        de batches, que es lo que se mira mientras la ventana trabaja. En
        una línea cabe lo que decía: el nombre de la ejecución, sus páginas
        y en qué quedó su entrega. Abre en «Seleccionar ejecución» para que
        la más reciente no parezca elegida antes de que nadie la elija, y es
        el único sitio desde el que se elige lo que se sube.
        """
        combo = SelectorEjecuciones()
        configure_combo_box(combo, 22)
        combo.setToolTip(
            "Ejecuciones procesadas, de la más reciente a la más antigua. "
            "Solo se suben las exportadas, y solo las últimas "
            f"{LIMITE_HISTORIAL}."
            " Pulse un nombre para elegir una ejecución o marque las "
            "casillas para subir varias juntas. Espacio marca la fila."
        )
        combo.setAccessibleName("Ejecuciones procesadas recientes")
        # «activated» solo lo emite quien elige con el ratón o el teclado,
        # así que sincronizar la lista desde el código no se lee como que
        # alguien cambió de ejecución y tira lo hecho.
        combo.activated.connect(self._al_elegir_del_historial)
        combo.seleccion_cambiada.connect(self._al_marcar_ejecuciones)
        # Cada ejecución se puede reiniciar o quitar de en medio sin tocar a
        # las demás; el clic derecho actúa sobre la que está elegida.
        combo.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        combo.customContextMenuRequested.connect(self._menu_del_historial)
        self.historial = combo
        return combo

    def _menu_del_historial(self, punto) -> None:
        """Lo que se puede hacer con la ejecución elegida en la lista.

        Son dos cosas distintas y por eso salen separadas: olvidar lo que la
        aplicación recuerda de esa ejecución en AirVault (para volver a
        empezar con ella) y deshacerse de la ejecución entera, que es lo que
        vacía la lista de lo que ya no hace falta. Ninguna de las dos toca
        los batches que ya estén en AirVault.

        Sobre «Seleccionar ejecución» no hay menú: no nombra ninguna, y
        actuar sobre la primera por descarte borraría lo que no se pidió.
        """
        csv = self.historial.currentData()
        if not csv:
            return
        menu = self._acciones_del_historial(
            Path(str(csv)), self.historial.currentText()
        )
        menu.exec(self.historial.mapToGlobal(punto))

    def _acciones_del_historial(self, csv: Path, nombre: str) -> QMenu:
        """Lo que el menú ofrece para una ejecución de la lista."""
        menu = QMenu(self)
        registro = menu.addAction("Eliminar el registro de AirVault")
        en_uso = self.hilo() is not None and self._es_ejecucion_seleccionada(csv)
        registro.setEnabled(not en_uso)
        registro.setToolTip(TOOLTIP_ELIMINAR_REGISTRO)
        registro.triggered.connect(lambda: self._eliminar_registro(csv))
        menu.addSeparator()
        ejecucion = menu.addAction("Eliminar la ejecución…")
        ejecucion.setEnabled(not en_uso)
        ejecucion.setToolTip(
            "Manda a la Papelera la carpeta de esta ejecución en output/. Lo "
            "que ya esté en AirVault no se toca."
        )
        ejecucion.triggered.connect(
            lambda: self._eliminar_ejecucion(csv, nombre)
        )
        return menu

    def _campos(self) -> QGridLayout:
        """Rejilla comun de datos, automatizacion y limite de duplicadas."""
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(SPACE_M)
        grid.setVerticalSpacing(SPACE_S)
        grid.setColumnStretch(1, 1)
        etiquetas = ("Nombre del batch:", "Máximo por batch:", "Fecha:", "Sesión:")
        self._etiquetas_campos = []
        for fila, etiqueta in enumerate(etiquetas, start=1):
            rotulo = QLabel(etiqueta)
            self._etiquetas_campos.append(rotulo)
            grid.addWidget(rotulo, fila, 0)

        self.lote_edit = QLineEdit()
        self.lote_edit.setPlaceholderText(
            "Se completa al elegir la ejecución"
        )
        self.lote_edit.setToolTip(
            "Nombre con el que el batch queda en AirVault. Lleva fecha y hora "
            "para no confundirlo con otro de la cola."
        )
        grid.addWidget(self.lote_edit, 1, 1, 1, 2)

        self.limite_batch_spin = QSpinBox()
        self.limite_batch_spin.setRange(10, 5000)
        self.limite_batch_spin.setSingleStep(50)
        if self._config.paginas_por_batch is not None:
            self.limite_batch_spin.setValue(self._config.paginas_por_batch)
        self.limite_batch_spin.setSuffix(" pág.")
        self.limite_batch_spin.setFixedHeight(
            self.lote_edit.sizeHint().height()
        )
        self.limite_batch_spin.setToolTip(
            "Páginas de cada batch de Quick Upload, separadoras incluidas; "
            "solo el último lleva menos. Los batches ya subidos se conservan."
        )
        self.limite_batch_spin.valueChanged.connect(
            self._guardar_limite_batch
        )
        self.limite_batch_control = SpinBoxWithButtons(self.limite_batch_spin)
        self.limite_batch_control.setMaximumWidth(160)
        self.limite_batch_control.layout().setSpacing(SPACE_XS)
        grid.addWidget(self.limite_batch_control, 2, 1, Qt.AlignmentFlag.AlignLeft)

        # La misma elección que en la ventana principal, vista desde aquí:
        # con qué fecha se escribe cada bitácora. Una ejecución exportada
        # con el día exacto todavía puede indexarse a fin de mes; al revés
        # no, porque esa ejecución no leyó el día, y por eso la opción se
        # apaga en vez de ofrecer algo que no se puede dar.
        self.fecha_combo = QComboBox()
        self.fecha_combo.addItem("Fin de mes", True)
        self.fecha_combo.addItem("Día exacto", False)
        self.fecha_combo.setItemData(
            0,
            "Escribe el último día del mes en todas las bitácoras.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.fecha_combo.setItemData(
            1,
            "Escribe el día que leyó la ejecución.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.fecha_combo.setToolTip(TOOLTIP_FECHA_INDEXADO)
        self.fecha_combo.setAccessibleName("Fecha con la que se indexa")
        configure_combo_box(self.fecha_combo, 12)
        self.fecha_combo.setMaximumWidth(self.limite_batch_control.maximumWidth())
        grid.addWidget(self.fecha_combo, 3, 1, Qt.AlignmentFlag.AlignLeft)

        # El campo de la sesión queda por si el navegador no puede: el
        # camino normal es que se resuelva sola.
        self.cookie_edit = QLineEdit()
        self.cookie_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie_edit.setPlaceholderText(
            "Se resuelve sola; solo si el navegador falla"
        )
        self.cookie_edit.setToolTip(
            "Casi nunca hace falta: el programa toma la sesión de su propio "
            "Edge. Si eso falla, pegue aquí la cookie de AirVault. No se "
            "guarda en el disco."
        )
        grid.addWidget(self.cookie_edit, 4, 1, 1, 2)
        return grid

    def _recuadro_de_revision(self) -> QGroupBox:
        """Cuánto de la ejecución se indexa solo y cuánto hay que mirar.

        La cola dice en qué va cada batch, pero no cuánto trabajo a mano
        deja la ejecución: eso solo se sabía subiéndola y abriendo el batch
        REVISAR en AirVault. Aquí se lee antes de subir nada, y con los dos
        porcentajes se sabe de un vistazo si la ejecución se termina sola o
        si detrás hay una tarde de Web Index.
        """
        recuadro = QGroupBox("Resultado")
        recuadro.setObjectName("airvaultResultado")
        recuadro.setToolTip(TOOLTIP_REPARTO)
        fila = QHBoxLayout(recuadro)
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(SPACE_M)
        self.reparto_total = QLabel()
        self.reparto_automaticas = QLabel()
        pintar_del_tema(
            self.reparto_automaticas, lambda: f"color: {color_indexado()};"
        )
        self.reparto_revisar = QLabel()
        pintar_del_tema(
            self.reparto_revisar, lambda: f"color: {color_revisar()};"
        )
        # Las tres cuentas se reparten el ancho a partes iguales y cada una
        # se centra en la suya. Apiladas a la izquierda, el recuadro cruzaba
        # la ventana entera para escribir tres cifras en su esquina y dejaba
        # el resto en blanco.
        for etiqueta in (self.reparto_total, self.reparto_automaticas,
                         self.reparto_revisar):
            etiqueta.setAlignment(Qt.AlignmentFlag.AlignCenter)
            etiqueta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            fila.addWidget(etiqueta, 1)
        self.reparto_automaticas.setWordWrap(True)
        self.reparto_revisar.setWordWrap(True)
        recuadro.setMinimumHeight(
            SPACE_XL + SPACE_M + self.reparto_total.fontMetrics().lineSpacing() * 2
        )
        self._reparto_fila = fila
        self._mostrar_reparto(None)
        return recuadro

    def _mostrar_reparto(self, reparto: Optional[tuple[int, int]]) -> None:
        """Escribe en el recuadro el reparto de la ejecución elegida.

        Sin ejecución, o con una exportada antes de que el CSV lo dijera,
        se explica por qué no hay números en vez de enseñar tres ceros, que
        se leerían como una ejecución vacía.
        """
        self._total_ejecucion = sum(reparto) if reparto is not None else 0
        aviso = ""
        if reparto is None:
            aviso = (
                TEXTO_SIN_EJECUCION if not self._corrida.strip()
                else TEXTO_SIN_COLUMNA
            )
        elif sum(reparto) == 0:
            aviso = "La ejecución no dejó ninguna bitácora."
        # El aviso es una frase y necesita partirse; la cuenta son dos
        # palabras y con el salto activo la caja se llevaba dos líneas de
        # alto para escribir «148» encima de «bitácoras».
        self.reparto_total.setWordWrap(bool(aviso))
        # Un aviso no es una de tres cuentas: se queda con el ancho entero,
        # centrado, en vez de partirse en el primer tercio y dejar los otros
        # dos vacíos. Las otras dos etiquetas van sin texto, así que sin
        # estirarlas no ocupan nada.
        self._reparto_fila.setStretch(1, 0 if aviso else 1)
        self._reparto_fila.setStretch(2, 0 if aviso else 1)
        if aviso:
            self.reparto_total.setText(aviso)
            self.reparto_automaticas.clear()
            self.reparto_revisar.clear()
            return
        automaticas, revisar = reparto
        total = automaticas + revisar
        # Solo se redondea el porcentaje de revisar y el otro se despeja de
        # él: redondeando los dos por separado la suma se iba a 99 o 101 y
        # parecía que faltaban bitácoras.
        parte_revisar = round(revisar * 100 / total)
        self.reparto_total.setText(
            f"{total} bitácoras" if total != 1 else "1 bitácora"
        )
        self.reparto_automaticas.setText(
            f"{automaticas} automáticas ({100 - parte_revisar} %)"
        )
        self.reparto_revisar.setText(
            f"{revisar} a revisar ({parte_revisar} %)"
        )

    def _cabecera_de_lotes(self) -> QGridLayout:
        """El título de la tabla de batches, el buscador y la vista previa.

        El buscador pregunta por una bitácora y contesta en qué batches de
        la cola está. Va aquí, encima de la tabla, porque lo que responde
        son filas de esa tabla: las de los batches que la llevan se quedan
        resaltadas y la línea de debajo dice cuáles son.
        """
        fila = QGridLayout()
        fila.setSpacing(SPACE_S)
        fila.addWidget(self._titulo("Batches de AirVault"), 0, 0, 1, 3)
        # Qué quiere decir cada estado de la columna, con su color. Pegado
        # al título de la cola porque es de ella de lo que habla.
        self.leyenda_estados = IconoAyuda(
            leyenda_de_estados_html, "Qué significa cada estado"
        )
        fila.addWidget(self.leyenda_estados, 0, 3)
        self.buscar_bitacora_edit = QLineEdit()
        self.buscar_bitacora_edit.setPlaceholderText(
            "Bitácora, matrícula, fecha o archivo"
        )
        self.buscar_bitacora_edit.setToolTip(AYUDA_BUSCAR_BITACORA)
        self.buscar_bitacora_edit.setAccessibleName(
            "Bitácora que se busca en la cola"
        )
        self.buscar_bitacora_edit.returnPressed.connect(self._buscar_bitacora)
        fila.addWidget(self.buscar_bitacora_edit, 1, 0, 1, 4)
        self.boton_buscar_bitacora = QPushButton("Buscar")
        self.boton_buscar_bitacora.setToolTip(
            "Buscar la bitácora en la cola; repetido, pasa al batch siguiente"
        )
        self.boton_buscar_bitacora.clicked.connect(self._buscar_bitacora)
        fila.addWidget(self.boton_buscar_bitacora, 2, 0)
        self.buscar_bitacora_anterior = QPushButton("‹")
        self.buscar_bitacora_anterior.setToolTip(
            "Batch anterior de los que la llevan"
        )
        self.buscar_bitacora_anterior.setEnabled(False)
        self.buscar_bitacora_anterior.setFixedWidth(28)
        self.buscar_bitacora_anterior.clicked.connect(
            lambda: self._mover_hallazgo(-1)
        )
        fila.addWidget(self.buscar_bitacora_anterior, 2, 1)
        self.buscar_bitacora_siguiente = QPushButton("›")
        self.buscar_bitacora_siguiente.setToolTip(
            "Batch siguiente de los que la llevan"
        )
        self.buscar_bitacora_siguiente.setEnabled(False)
        self.buscar_bitacora_siguiente.setFixedWidth(28)
        self.buscar_bitacora_siguiente.clicked.connect(
            lambda: self._mover_hallazgo(1)
        )
        fila.addWidget(self.buscar_bitacora_siguiente, 2, 2)
        # Ctrl+F desde cualquier punto de la ventana, como en el resto.
        QShortcut(
            QKeySequence.StandardKey.Find, self,
            activated=self.buscar_bitacora_edit.setFocus,
        )
        batch_menu = QMenu(self)
        batch_menu.setToolTipsVisible(True)
        self.boton_buscar_websearch = batch_menu.addAction("Verificar subidas en Web Search")
        self.boton_buscar_websearch.setToolTip(
            "Busca el 2 % de las bitácoras, con un mínimo de 7 y un máximo de 15, "
            "repartidas entre el inicio y el final de cada batch. "
            "Consulta números en Web Search de hasta 32 batches a la vez, incluso durante "
            "la subida o el indexado. Si la muestra ya está publicada, lo marca completado."
        )
        self.boton_buscar_websearch.triggered.connect(self._buscar_websearch)
        batch_menu.addSeparator()
        self.boton_previa = batch_menu.addAction("Vista previa…")
        self.boton_previa.setEnabled(False)
        self.boton_previa.setToolTip(
            "Muestra todos los batches locales y el reparto previsto de la ejecución. "
            "Permite aplicar las casillas del panel lateral. No prepara ni sube nada."
        )
        self.boton_previa.triggered.connect(self._vista_previa)
        batch_menu.addSeparator()
        self.boton_eliminar_batches = batch_menu.addAction(
            "Eliminar seleccionados…"
        )
        self.boton_eliminar_batches.setEnabled(False)
        self.boton_eliminar_batches.setToolTip(
            "Envía a la Papelera todos los batches seleccionados en el panel. "
            "No modifica los batches que ya estén en AirVault."
        )
        self.boton_eliminar_batches.triggered.connect(
            self._eliminar_seleccionados
        )
        batch_menu.addSeparator()
        self.boton_eliminar_registro = batch_menu.addAction(
            "Eliminar registros locales…"
        )
        self.boton_eliminar_registro.setEnabled(False)
        self.boton_eliminar_registro.setToolTip(TOOLTIP_ELIMINAR_REGISTROS)
        self.boton_eliminar_registro.triggered.connect(
            lambda: self._eliminar_registro()
        )
        batch_menu.addSeparator()
        agregar_filtros_al_menu(batch_menu, self._controles_filtro_batches())
        self.batch_actions_button = QToolButton()
        self.batch_actions_button.setText("Acciones")
        configure_menu_button(self.batch_actions_button, batch_menu)
        fila.addWidget(self.batch_actions_button, 2, 3)
        return fila

    def _vista_previa(self) -> None:
        """Enseña toda la cola y añade el reparto que todavía no existe."""
        from app.airvault.flujo import (ErrorDeCorrida, carpeta_de_corrida,
                                        carpeta_de_trabajo,
                                        previsualizar_reparto)
        from app.airvault.mapping import FLOTA_CACHE_FILENAME, ResolutorFlota
        from app.gui.airvault_previa import VistaPreviaBatches

        csv = self._corrida.strip()
        previstos = []
        try:
            if csv and (self._listo_para_subir or not self._estados):
                carpeta = self._raiz / carpeta_de_trabajo(carpeta_de_corrida(csv).name)
                previstos = previsualizar_reparto(
                    self._config_actual(), carpeta, Path(csv),
                    self.lote_edit.text().strip(),
                    resolutor=ResolutorFlota.load(self._raiz / FLOTA_CACHE_FILENAME),
                    paginas_por_batch=self.limite_batch_spin.value(),
                    fin_de_mes=self.fin_de_mes(),
                )
        except (ErrorDeCorrida, OSError, ValueError) as error:
            logger.opt(exception=error).warning("No se pudo preparar la vista previa: {}", error)
            QMessageBox.warning(self, "Vista previa", mensaje_error(error, "No se pudo preparar la vista previa. Vuelva a exportar la ejecución."))
            return
        previstos = self._previstos_de_la_cola(previstos)
        if not previstos:
            QMessageBox.information(
                self,
                "Vista previa",
                "No hay batches locales ni batches previstos para mostrar.",
            )
            return
        self._abrir_ventana(
            VistaPreviaBatches(
                previstos, csv=csv, parent=self,
                filtros=self._controles_filtro_batches(),
                filtrar=self._filtrar_previstos,
            )
        )

    def _controles_filtro_batches(self) -> tuple:
        return (self.solo_ejecucion_check, self.ocultar_indexados_check,
                self.ocultar_completados_check)

    def _previstos_de_la_cola(self, previstos=()) -> list:
        from app.airvault.flujo import (AUTOCOMPLETADO, COMPLETADO, PUBLICADO,
                                        _previsto_de_trabajo)

        locales = []
        for parte in self._estados:
            previsto = _previsto_de_trabajo(parte.trabajo)
            locales.append(replace(
                previsto,
                estado=(TEXTO_INDEXANDO if str(parte.trabajo.carpeta) in self._indexando
                        else parte.titulo),
                completado=parte.estado in (COMPLETADO, AUTOCOMPLETADO, PUBLICADO),
            ))
        carpetas = {p.carpeta for p in locales}
        nombres = {(str(Path(p.csv_origen)).casefold(), p.nombre) for p in locales}
        return locales + [p for p in previstos
                          if (p.carpeta is None or p.carpeta not in carpetas)
                          and (str(Path(p.csv_origen)).casefold(), p.nombre) not in nombres]

    def _filtrar_previstos(self, previstos) -> list:
        carpetas = {parte.trabajo.carpeta for parte in self._partes_en_cola()}
        return [p for p in previstos if p.carpeta in carpetas
                or (p.carpeta is None and (
                    not self.solo_ejecucion_check.isChecked()
                    or self._es_ejecucion_seleccionada(p.csv_origen)
                ))]

    def _lotes(self) -> BatchesSidebar:
        lista = BatchesSidebar()
        lista.setSpacing(SPACE_XS)
        lista.setToolTip(
            "Todos los batches locales. Clic derecho para sus acciones; "
            "Ctrl o Mayúsculas para seleccionar varios."
        )
        lista.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lista.customContextMenuRequested.connect(self._menu_de_la_cola)
        lista.itemSelectionChanged.connect(self._actualizar_eliminar_seleccionados)
        lista.setMinimumHeight(self._densidad.airvault_table_min_height)
        self.lotes = lista
        return lista

    def _cargar_sidebar(self) -> None:
        from app.airvault.flujo import CARPETA_TRABAJOS, cargar_todos_trabajos, estado_local
        conocidos = {str(t.carpeta) for t in self._trabajos}
        nuevos = [t for t in cargar_todos_trabajos(
            self._config_actual(), self._raiz / CARPETA_TRABAJOS
        ) if str(t.carpeta) not in conocidos]
        self._trabajos.extend(nuevos)
        self._estados.extend(estado_local(t) for t in nuevos)
        self._pintar_lotes()
        self.boton_buscar_websearch.setEnabled(bool(self._trabajos))
        self._fin_pendiente = self._firma_de_fin() is not None
        self._anunciar_fin()
        self._ajustar_confirmacion()

    def _buscar_websearch(self) -> None:
        trabajos = self._por_confirmar_websearch()
        if not trabajos:
            self.resumen.setText("No hay batches pendientes con números válidos para consultar en Web Search.")
            return
        self._iniciar_confirmacion(trabajos, automatico=False)

    def _iniciar_confirmacion(self, trabajos, automatico: bool = False) -> None:
        """La lectura tiene su hilo y su estado, independientes de subida e indexado."""
        trabajos = list(trabajos)
        if not trabajos or self._deteniendo:
            return
        lector = self.lectura_websearch()
        if self._worker_websearch is not None:
            en_curso = {str(t.carpeta) for t in lector.estado["buscar_trabajos"]} if lector else set()
            self._websearch_pendientes.update({str(t.carpeta): t for t in trabajos
                                              if str(t.carpeta) not in en_curso})
            self._websearch_manual_pendiente |= not automatico
            return
        estado = dict(config=self._config_actual(), raiz=self._raiz,
                      cookie=self.cookie_edit.text(), buscar_trabajos=trabajos,
                      confirmacion_automatica=automatico)
        sesion = self._estado.get("sesion") or self._estado_websearch.get("sesion")
        if sesion is not None:
            estado["sesion_base"] = sesion
        anterior = self._estado_websearch.get("buscador")
        if anterior is not None:
            from copy import copy
            estado["buscador"] = copy(anterior)
            if sesion is not None:
                estado["buscador"].sesion = sesion
        for trabajo in trabajos:
            self._websearch_revisados[str(trabajo.carpeta)] = self._firma_consulta_websearch(trabajo)
            self._websearch_inicio_por_revisar.discard(str(trabajo.carpeta))
        self._cuenta_websearch = (0, len(trabajos))
        worker = TrabajoAirVaultWorker("buscar_websearch", estado, self)
        worker.paso.connect(self._mostrar_paso_websearch)
        worker.buscado.connect(self._al_buscar_websearch)
        worker.fallo.connect(self._al_fallar_websearch)
        worker.cancelado.connect(lambda: self._anotar("Búsqueda en Web Search cancelada")
                                 if not automatico or self._fin_confirmado is None else None)
        worker.finished.connect(self._al_terminar_websearch)
        self._worker_websearch = worker
        self.boton_cancelar.setEnabled(True)
        self._anotar(f"Web Search: buscando números de bitácora de {len(trabajos)} batches en paralelo")
        worker.start()
        self._actualizar_latido()

    def _mostrar_paso_websearch(self, texto: str, hechas: int, total: int) -> None:
        if self._fin_confirmado is not None and self._lectura_websearch_pendiente() is None:
            return
        if self.hilo() is None:
            self.estado_label.setText(texto if total == 0 else "Buscando números de bitácora en Web Search")
            self.estado_label.setToolTip(texto)

    def _al_fallar_websearch(self, mensaje: str) -> None:
        if (self._fin_confirmado is not None and self._worker_websearch is not None
                and self._worker_websearch.estado.get("confirmacion_automatica")):
            return
        mensaje = mensaje_error(mensaje, "No se pudo consultar Web Search.")
        self._anotar("Web Search: " + primera_frase(mensaje))
        if self.hilo() is None:
            self.resumen.setText("Web Search: " + mensaje)

    def _al_terminar_websearch(self) -> None:
        worker = self._worker_websearch
        if worker is not None:
            self._estado_websearch = worker.estado
        self._worker_websearch = None
        if worker is not None:
            worker.deleteLater()
        self.boton_cancelar.setEnabled(self.hilo() is not None)
        self._actualizar_latido()
        self._publicar_avance()
        self._anunciar_fin()
        pendientes = list(self._websearch_pendientes.values())
        manual = self._websearch_manual_pendiente
        self._websearch_pendientes.clear()
        self._websearch_manual_pendiente = False
        if pendientes and not self._deteniendo:
            self._iniciar_confirmacion(pendientes, automatico=not manual)
        else:
            self._ajustar_confirmacion()

    def _al_buscar_websearch(self, datos: dict) -> None:
        from app.airvault.flujo import estado_local
        from app.airvault.manifest import copiar_confirmacion
        auxiliar_terminado = self._fin_confirmado is not None and datos.get("automatico")
        resultados = datos["resultados"]
        for trabajo, confirmado in resultados:
            for actual in self._trabajos + list(self._estado.get("trabajos") or []):
                if str(actual.carpeta) == str(trabajo.carpeta):
                    copiar_confirmacion(trabajo.manifiesto, actual.manifiesto)
            if not auxiliar_terminado:
                self._anotar(
                    f"Batch «{trabajo.manifiesto.nombre_batch}»: "
                    + ("confirmado en Web Search" if confirmado else "pendiente de confirmar en Web Search"),
                    [trabajo.manifiesto.websearch_detalle],
                )
        confirmados = datos.get("confirmados", sum(confirmado for _, confirmado in resultados))
        revisados = datos.get("revisados", len(resultados))
        total = datos.get("total", len(resultados))
        self._cuenta_websearch = (revisados, total)
        if self.hilo() is None and not auxiliar_terminado:
            self.resumen.setText(
                f"Web Search: {revisados} de {total} batches consultados, {confirmados} confirmados por muestra. "
                "Los no encontrados siguen pendientes de confirmar la subida."
            )
        terminado = self._fin_confirmado is not None
        locales = {str(t.carpeta): t for t in self._trabajos}
        por_carpeta = {str(t.carpeta): estado_local(locales.get(str(t.carpeta), t)) for t, _ in resultados}
        self._estados = [por_carpeta.get(str(p.trabajo.carpeta), p) for p in self._estados]
        if terminado:
            self._fin_confirmado = self._firma_de_fin()
        self._fin_pendiente = True
        self._pintar_lotes()
        self._ajustar_confirmacion()
        self._anunciar_fin()

    def _respuesta_de_la_busqueda(self) -> QLabel:
        """La línea que dice en qué batches de la cola está la bitácora."""
        etiqueta = QLabel(AYUDA_BUSCAR_BITACORA)
        etiqueta.setWordWrap(True)
        pintar_del_tema(etiqueta, lambda: f"color: {color_ayuda()};")
        # Sitio para dos líneas: nombrar varios batches con sus páginas no
        # cabe en una, y sin reservarlo la ventana daba un salto al buscar.
        etiqueta.setMinimumHeight(etiqueta.fontMetrics().lineSpacing() * 2)
        etiqueta.setMaximumHeight(etiqueta.fontMetrics().lineSpacing() * 3)
        etiqueta.hide()
        etiqueta.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.busqueda_bitacora = etiqueta
        return etiqueta

    # ── buscar una bitácora en la cola ─────────────────────────────

    def _batches_de_la_cola(self) -> list:
        """La cola tal como la enseña la tabla: mismo orden, mismas filas.

        Se buscan todos los batches que hay delante, en cualquier estado:
        que uno esté terminado es parte de la respuesta, porque explica por
        qué la bitácora ya no hace falta subirla otra vez.
        """
        return [
            (
                parte.nombre or "(sin nombre)",
                parte.trabajo.manifiesto.registros,
            )
            for parte in self._partes_en_cola()
        ]

    def _buscar_bitacora(self) -> None:
        """Busca la bitácora escrita y resalta los batches que la llevan.

        Repetir la búsqueda con el mismo texto pasa al batch siguiente,
        igual que ›: es lo que se espera al volver a pulsar Intro sobre lo
        que ya se buscó.
        """
        texto = self.buscar_bitacora_edit.text().strip()
        self.busqueda_bitacora.setVisible(bool(texto))
        if (
            texto
            and texto.casefold() == self._bitacora_buscada
            and self._hallazgos
        ):
            self._mover_hallazgo(1)
            return
        self._bitacora_buscada = texto.casefold()
        self._hallazgos = buscar_en_la_cola(self._batches_de_la_cola(), texto)
        self._posicion_hallazgo = 0 if self._hallazgos else -1
        self.busqueda_bitacora.setText(
            frase_de(texto, self._hallazgos) if texto
            else AYUDA_BUSCAR_BITACORA
        )
        self.busqueda_bitacora.setToolTip(self.busqueda_bitacora.text())
        self._resaltar_hallazgos()

    def _mover_hallazgo(self, salto: int) -> None:
        """Pasa al batch anterior o siguiente de los que la llevan."""
        if not self._hallazgos:
            return
        self._posicion_hallazgo = (
            self._posicion_hallazgo + salto
        ) % len(self._hallazgos)
        self._resaltar_hallazgos()

    def _resaltar_hallazgos(self) -> None:
        """Deja elegidos en la tabla los batches donde está la bitácora.

        Se resaltan todos a la vez, no uno: la respuesta es que va en
        varios, y verlos juntos es lo que se vino a ver. La fila actual es
        la del batch que se está mirando, y ‹ y › la mueven sin soltar el
        resaltado de los demás.
        """
        seleccion = self.lotes.selectionModel()
        modelo = self.lotes.model()
        if seleccion is None or modelo is None:
            return
        filas = [
            hallazgo.fila for hallazgo in self._hallazgos
            if hallazgo.fila < self.lotes.rowCount()
        ]
        marcadas = QItemSelection()
        ultima = self.lotes.columnCount() - 1
        for fila in filas:
            marcadas.select(modelo.index(fila, 0), modelo.index(fila, ultima))
        seleccion.select(
            marcadas,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        if filas:
            mirado = max(0, min(self._posicion_hallazgo, len(filas) - 1))
            actual = filas[mirado]
            seleccion.setCurrentIndex(
                modelo.index(actual, 0),
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
            self.lotes.scrollTo(modelo.index(actual, 0))
        varios = len(filas) > 1
        self.buscar_bitacora_anterior.setEnabled(varios)
        self.buscar_bitacora_siguiente.setEnabled(varios)

    def _rehacer_la_busqueda(self) -> None:
        """Vuelve a buscar sobre la tabla recién pintada.

        La cola se repinta entera cada vez que cambia el estado de un batch
        o se cambia de ejecución, y eso borra el resaltado. Repetir la
        búsqueda lo devuelve y, de paso, la respuesta deja de ser la de una
        cola que ya cambió: si el batch se canceló o llegó otro con la
        misma bitácora, se dice ahora y no en la siguiente búsqueda.
        """
        if not self._bitacora_buscada:
            return
        texto = self.buscar_bitacora_edit.text().strip()
        self._hallazgos = buscar_en_la_cola(self._batches_de_la_cola(), texto)
        if not self._hallazgos:
            self._posicion_hallazgo = -1
        else:
            self._posicion_hallazgo = max(
                0, min(self._posicion_hallazgo, len(self._hallazgos) - 1)
            )
        self.busqueda_bitacora.setText(frase_de(texto, self._hallazgos))
        self.busqueda_bitacora.setToolTip(self.busqueda_bitacora.text())
        self._resaltar_hallazgos()

    # ── la cola: cada fila por separado ────────────────────────────

    def _menu_de_la_cola(self, punto) -> None:
        """Abre el menú de los batches sobre los que se hizo clic derecho.

        Si la fila del clic ya estaba entre las elegidas, la acción vale
        para toda la selección; si no, la selección pasa a ser esa fila.
        Es lo que hace cualquier lista, y evita actuar sobre batches que no
        se están mirando.
        """
        fila = self.lotes.indexAt(punto).row()
        if fila < 0 or fila >= len(self._partes_en_cola()):
            return
        menu = self._acciones_de_la_cola(self._elegidas(fila))
        menu.exec(self.lotes.viewport().mapToGlobal(punto))
        menu.deleteLater()

    def _elegidas(self, fila: int) -> list:
        """Las filas sobre las que va a actuar el menú."""
        seleccion = self.lotes.selectionModel()
        filas = sorted(
            {indice.row() for indice in seleccion.selectedRows()}
        ) if seleccion is not None else []
        if fila not in filas:
            self.lotes.selectRow(fila)
            filas = [fila]
        return [
            self._partes_en_cola()[numero] for numero in filas
            if numero < len(self._partes_en_cola())
        ]

    def _seleccionadas(self) -> list:
        """Batches de todas las filas seleccionadas en la tabla."""
        seleccion = self.lotes.selectionModel()
        if seleccion is None:
            return []
        filas = sorted({indice.row() for indice in seleccion.selectedRows()})
        return [
            self._partes_en_cola()[fila] for fila in filas
            if fila < len(self._partes_en_cola())
        ]

    def _actualizar_eliminar_seleccionados(self) -> None:
        elegidos = self._seleccionadas()
        self.boton_eliminar_batches.setEnabled(bool(elegidos))
        self.boton_eliminar_batches.setText(
            "Eliminar seleccionado…" if len(elegidos) == 1
            else f"Eliminar seleccionados ({len(elegidos)})…"
            if elegidos else "Eliminar seleccionados…"
        )

    def _eliminar_seleccionados(self) -> None:
        """Elimina en una sola operación toda la selección visible."""
        self._eliminar_estas(self._seleccionadas())

    def _acciones_de_la_cola(self, partes) -> QMenu:
        """Lo que se puede hacer con los batches elegidos, y lo que no.

        Cada acción es la misma que ya hace la ventana entera, acotada a
        las filas elegidas: se habilita si vale para alguna de ellas y se
        aplica solo a esas, de modo que elegir cinco batches mezclados hace
        en cada uno lo que corresponde. Lo que no se puede hacer sale
        desactivado en vez de desaparecer, para que la fila diga siempre de
        qué es capaz.

        Trabajando también se puede elegir: la acción no se pierde, se pone
        en cola y arranca en cuanto termine lo que hay en vuelo.
        """
        from app.airvault.flujo import INDEXADO

        planes = self._estado.get("planes") or {}

        def activo(parte) -> bool:
            return not bool(
                getattr(parte.trabajo.manifiesto, "cancelado", False)
            )

        # Subir se ofrece siempre que AirVault no haya devuelto un batch,
        # sin importar en qué punto de la comprobación esté la fila. Antes
        # solo valía para dos estados, así que una carga que se estaba
        # revisando o que apareció descuadrada no se podía volver a mandar
        # aunque quien miraba la cola ya supiera que no está.
        subibles = [
            parte for parte in partes
            if activo(parte) and parte.se_puede_subir
        ]
        indexables = [
            parte for parte in partes
            if activo(parte) and parte.se_puede_indexar
            and str(parte.trabajo.carpeta) in planes
        ]
        cerrables = [
            parte for parte in partes
            if activo(parte) and parte.estado == INDEXADO
            and not parte.trabajo.manifiesto.solo_subir
        ]
        cancelables = [
            parte for parte in partes
            if activo(parte) and not parte.se_acabo
        ]
        reanudables = [parte for parte in partes if not activo(parte)]

        menu = QMenu(self)
        self._accion(
            menu, "Subir a AirVault ahora", subibles,
            lambda: self._subir_estas(subibles),
        )
        self._accion(
            menu, "Revisar en AirVault", partes,
            lambda: self._comprobar_estas(partes),
        )
        self._accion(
            menu, "Verificar subidas en Web Search", partes,
            lambda: self._iniciar_confirmacion([p.trabajo for p in partes], automatico=False),
        )
        self._accion(
            menu, "Indexar ahora", indexables,
            lambda: self._indexar_estas(indexables),
        )
        self._accion(
            menu, "Completar el batch", cerrables,
            lambda: self._completar_estas(cerrables),
        )

        menu.addSeparator()
        sospechosos = [
            parte for parte in partes
            if parte.trabajo.manifiesto.posible_duplicado
        ]
        self._accion(
            menu, "No es duplicado: volver a subir", sospechosos,
            lambda: self._quitar_sospecha(sospechosos),
        )

        menu.addSeparator()
        if reanudables and not cancelables:
            self._accion(
                menu, "Reanudar en la cola", reanudables,
                lambda: self._cancelar_estas(reanudables, False),
            )
        else:
            self._accion(
                menu, "Cancelar en la cola", cancelables,
                lambda: self._cancelar_estas(cancelables, True),
            )

        self._accion(
            menu, "Eliminar el batch…", partes,
            lambda: self._eliminar_estas(list(partes)),
        )

        menu.addSeparator()
        # Mirar lo que lleva dentro es de un batch a la vez: son listas
        # distintas y no hay una sola que enseñar por varios.
        con_bitacoras = (
            list(partes)
            if len(partes) == 1 and partes[0].trabajo.manifiesto.registros
            else []
        )
        self._accion(
            menu, "Ver las bitácoras del batch", con_bitacoras,
            lambda: self._ver_bitacoras(con_bitacoras[0]),
        )

        menu.addSeparator()
        con_nombre = [parte for parte in partes if parte.nombre]
        self._accion(
            menu, "Copiar el nombre del batch", con_nombre,
            lambda: self._copiar_al_portapapeles(
                "\n".join(parte.nombre for parte in con_nombre)
            ),
        )
        con_id = [parte for parte in partes if parte.batch_id]
        self._accion(
            menu, "Copiar el ID del batch", con_id,
            lambda: self._copiar_al_portapapeles(
                "\n".join(parte.batch_id for parte in con_id)
            ),
        )
        menu.addSeparator()
        menu.addAction(self.boton_previa)
        agregar_filtros_al_menu(menu, self._controles_filtro_batches())
        return menu

    @staticmethod
    def _accion(menu: QMenu, texto: str, sobre, hacer):
        """Añade una acción y dice a cuántos batches se aplicaría."""
        accion = menu.addAction(
            texto if len(sobre) <= 1 else f"{texto} ({len(sobre)})"
        )
        accion.setEnabled(bool(sobre))
        accion.triggered.connect(hacer)
        return accion

    def _preguntar_reenvio(self, partes) -> bool:
        nombres = ", ".join(parte.nombre for parte in partes)
        respuesta = QMessageBox.warning(
            self, "Advertencia: posible batch duplicado",
            f"Se volverá a subir: {nombres}.\n\n"
            "Estas bitácoras pueden estar ya en AirVault. Reenviar crea otra copia, "
            "incluso si el batch anterior está completado. Revise AirVault antes de continuar.\n\n"
            "¿Autoriza volver a subir estos batches?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return respuesta == QMessageBox.StandardButton.Yes

    def _quitar_sospecha(self, partes) -> None:
        """Autoriza y reenvia un batch marcado como posible duplicado.

        La sospecha se levanta con pruebas de que esas bitácoras están en
        AirVault, no de que las subiera este batch: pueden haber llegado
        por otro batch, por otra persona o por una carga anterior de la
        misma ejecución. Quién lo decide es quien mira AirVault, así que
        aquí se autoriza el reenvío que la persona acaba de pedir. La
        automatización sigue sin hacerlo sola mientras la duda esté puesta.
        """
        from app.airvault.flujo import (autorizar_posible_duplicado,
                                        estado_local)

        partes = list(partes)
        if not partes or not self._preguntar_reenvio(partes):
            return
        if not self._preguntar_por_amarillas([parte.trabajo for parte in partes]):
            return
        limpiadas = set()
        for parte in partes:
            autorizar_posible_duplicado(parte.trabajo)
            limpiadas.add(str(parte.trabajo.carpeta))
        # La fila la pinta el estado que se calculó antes de quitar la
        # marca, así que sin recalcularlo seguiría diciendo «Posible
        # duplicado» hasta la siguiente comprobación.
        self._estados = [
            estado_local(otra.trabajo)
            if str(otra.trabajo.carpeta) in limpiadas
            else otra
            for otra in self._estados
        ]
        self._pintar_lotes()
        self._subir_estas(partes, duplicados_autorizados=True)

    def _ver_bitacoras(self, parte) -> None:
        """Abre la lista de las bitácoras que lleva dentro un batch."""
        from app.airvault.flujo import AUTOCOMPLETADO, COMPLETADO
        from app.gui.airvault_previa import BitacorasDelBatch

        manifiesto = parte.trabajo.manifiesto
        self._abrir_ventana(
            BitacorasDelBatch(
                manifiesto.nombre_batch,
                manifiesto.registros,
                csv=manifiesto.csv_origen or self._corrida.strip(),
                completado=parte.estado in (COMPLETADO, AUTOCOMPLETADO),
                parent=self,
            )
        )

    def _abrir_ventana(self, ventana) -> None:
        """Muestra una ventana de consulta y la conserva viva.

        La vista previa y la lista de bitácoras son ventanas aparte, como el
        visor de CSV: sin dueño a nivel de Qt (para que Windows les dé su
        entrada en la barra de tareas) y sin bloquear esta, que puede estar
        subiendo mientras se las mira. Como nadie más las sostiene, la
        referencia vive aquí hasta que se cierran.
        """
        self._ventanas_de_consulta.append(ventana)
        ventana.destroyed.connect(
            lambda *_a, v=ventana: (
                self._ventanas_de_consulta.remove(v)
                if v in self._ventanas_de_consulta
                else None
            )
        )
        ventana.mostrar()

    def _copiar_al_portapapeles(self, texto: str) -> None:
        if not texto:
            return
        QGuiApplication.clipboard().setText(texto)
        self._anotar(f"Copiado: {texto}")

    # ── la cola de acciones ────────────────────────────────────────

    def _encolar(self, modo: str, trabajos, texto: str) -> None:
        """Lanza la acción, o la deja esperando si hay algo en vuelo.

        Antes cada acción exigía la ventana parada, así que elegir un batch
        mientras subía otro no hacía nada. Ahora se apunta y arranca sola
        en cuanto el hilo queda libre: la tabla es una cola y se comporta
        como tal.
        """
        if modo == "buscar_websearch":
            self._iniciar_confirmacion(trabajos, automatico=False)
            return
        trabajos = [
            trabajo for trabajo in trabajos
            if not getattr(trabajo.manifiesto, "cancelado", False)
        ]
        if not trabajos:
            return
        if self.hilo() is not None:
            claves = {str(t.carpeta) for t in trabajos}
            if any(modo == pendiente and claves == {str(t.carpeta) for t in anteriores}
                   for pendiente, anteriores in self._cola_de_acciones):
                self.resumen.setText("Esta acción ya está en cola y se ejecutará al terminar el trabajo actual.")
                return
            self._cola_de_acciones.append((modo, trabajos))
            pendientes = len(self._cola_de_acciones)
            plural = "acciones" if pendientes != 1 else "acción"
            self._anotar(f"{texto}: en cola, {pendientes} {plural} esperando")
            return
        self._anotar(texto)
        self._ejecutar_accion(modo, trabajos)

    def _ejecutar_accion(self, modo: str, trabajos) -> bool:
        """Prepara el estado que pide cada modo y arranca el hilo."""
        if modo == "buscar_websearch":
            self._iniciar_confirmacion(trabajos, automatico=False)
            return bool(trabajos)
        trabajos = self._filtrar_trabajos(trabajos)
        if not trabajos:
            return False
        estado = self._base_del_estado()
        if estado is None:
            return False
        if modo in ("subir_pendientes", "resubir"):
            estado["pendientes_subida"] = list(trabajos)
            estado["indexar_al_encontrar"] = self._opciones.indexar
            if modo == "resubir":
                # La orden dada a mano sobre filas concretas. El otro modo
                # es la reanudación automática, que sí comprueba antes.
                estado["forzados"] = [
                    str(trabajo.carpeta) for trabajo in trabajos
                ]
            self._subidas_del_ciclo.clear()
        elif modo == "indexar":
            estado["indexar_manual"] = True
            planes = estado.get("planes") or {}
            listos = [
                trabajo for trabajo in trabajos
                if str(trabajo.carpeta) in planes
            ]
            if not listos:
                return False
            estado["listos"] = listos
            estado["indexar_acotado"] = True
            # Una orden sobre filas concretas no cierra otros batches.
            estado.pop("completar_tambien", None)
        elif modo == "comprobar":
            # El menu contextual pone esta lista. El boton inferior no la
            # pone y por eso conserva el alcance global sobre toda la tabla.
            estado["comprobar_trabajos"] = list(trabajos)
        elif modo == "completar":
            for trabajo in trabajos:
                self._cierres_fallidos.pop(str(trabajo.carpeta), None)
            estado["por_completar"] = list(trabajos)
            estado["completar_acotado"] = True
        estado["completar"] = self.completar_check.isChecked()
        self._lanzar(modo, estado)
        return True

    def _siguiente_de_la_cola(self) -> bool:
        """Arranca la primera acción en espera que todavía tenga sentido."""
        while self._cola_de_acciones:
            modo, trabajos = self._cola_de_acciones.pop(0)
            vigentes = [
                trabajo for trabajo in trabajos
                if not getattr(trabajo.manifiesto, "cancelado", False)
            ]
            if vigentes and self._ejecutar_accion(modo, vigentes):
                return True
        return False

    def _subir_estas(self, partes, duplicados_autorizados: bool = False) -> None:
        """Manda estos batches a Quick Upload y no pregunta nada más.

        Es una orden expresa y se obedece como tal: el archivo sale hacia
        AirVault sin pasar por la comprobación larga. Solo se antepone una
        lectura de la cola, que es lo único que impide publicar dos veces la
        misma bitácora. Quien pulsa esto ya miró Web Index, y la acción solo
        se ofrece cuando AirVault no ha devuelto ningún batch para esa
        parte.

        No se reinicia aquí el manifiesto, aunque sea la orden de volver a
        subir: eso borraría ``lotes_previos``, la foto de la cola anterior a
        la carga, que es justo lo que permite reconocer un ``Empty-Batch``
        propio. Lo reinicia ``subir_partes`` cuando toca, después de
        comprobar y justo antes de enviar.
        """
        trabajos = [parte.trabajo for parte in partes]
        if not self._preguntar_por_amarillas(trabajos):
            return
        nombres = ", ".join(parte.nombre for parte in partes)
        self._encolar(
            "resubir",
            trabajos,
            (
                f"Reenvío autorizado tras revisar posible duplicado: {nombres}"
                if duplicados_autorizados else
                f"Se vuelve a subir {nombres}"
            ),
        )

    def _comprobar_estas(self, partes) -> None:
        """Revisa una seleccion de la tabla como una accion de una sola vez."""
        trabajos = [parte.trabajo for parte in partes]
        nombres = ", ".join(parte.nombre for parte in partes)
        self._encolar(
            "comprobar",
            trabajos,
            f"Se revisa en AirVault: {nombres}",
        )

    def _preguntar_por_amarillas(self, trabajos) -> bool:
        """Pregunta si se suben batches que dejarían páginas sin indexar.

        Amarilla es la página a la que le falta un campo obligatorio: entra
        en AirVault, pero hay que completarla a mano. Lo limpio es que esas
        bitácoras vayan al batch REVISAR, y para eso hay que volver a
        exportar la ejecución. Cuando el archivo ya está hecho eso cuesta
        más que terminarlas a mano, así que la decisión es de quien sube y
        se pregunta aquí, con la cuenta delante.

        Se calcula leyendo el manifiesto, sin tocar la red, así que se puede
        preguntar antes de arrancar el hilo. Devuelve ``False`` solo si
        alguien dijo que no; entonces no se sube nada.
        """
        from app.airvault.flujo import autorizar_amarillas, paginas_amarillas

        con_amarillas = [
            (trabajo, paginas_amarillas(trabajo))
            for trabajo in trabajos
            if not trabajo.manifiesto.amarillas_permitidas
        ]
        con_amarillas = [
            (trabajo, paginas) for trabajo, paginas in con_amarillas if paginas
        ]
        if not con_amarillas:
            return True

        cuantas = sum(len(paginas) for _trabajo, paginas in con_amarillas)
        if not self._confirmar_amarillas(con_amarillas, cuantas):
            self._anotar(
                f"No se sube: {cuantas} páginas amarillas sin autorizar"
            )
            return False
        for trabajo, _paginas in con_amarillas:
            autorizar_amarillas(trabajo)
        self._anotar(
            f"Autorizado subir con {cuantas} páginas amarillas:",
            [
                trabajo.manifiesto.nombre_batch
                for trabajo, _p in con_amarillas
            ],
        )
        return True

    def _confirmar_amarillas(self, con_amarillas, cuantas: int) -> bool:
        """El diálogo que lo pregunta, y nada más.

        Aparte de la decisión para que las pruebas puedan responder que sí o
        que no sin abrir una ventana.
        """
        from app.airvault.flujo import resumen_amarillas

        detalle = "\n\n".join(
            f"«{trabajo.manifiesto.nombre_batch}»: {len(paginas)} páginas\n"
            + resumen_amarillas(paginas, 3)
            for trabajo, paginas in con_amarillas
        )
        dialogo = QMessageBox(self)
        dialogo.setIcon(QMessageBox.Icon.Question)
        dialogo.setWindowTitle("Páginas sin un campo obligatorio")
        dialogo.setText(
            f"{cuantas} páginas tienen datos sin confirmar. Requerirán revisión en AirVault."
        )
        dialogo.setInformativeText(
            "Vuelva a exportar para enviarlas a REVISAR, o elija «Subir así» para corregirlas a mano."
        )
        logger.info("Páginas sin confirmar antes de subir: {}", detalle)
        # «Subir así» es el botón por omisión a propósito: la pregunta
        # existe para que se pueda decir que sí, no para desanimar.
        subir = dialogo.addButton(
            "Subir así", QMessageBox.ButtonRole.AcceptRole
        )
        dialogo.addButton("No subir", QMessageBox.ButtonRole.RejectRole)
        dialogo.setDefaultButton(subir)
        dialogo.exec()
        return dialogo.clickedButton() is subir

    def _indexar_estas(self, partes) -> None:
        """Escribe solo estos batches, con el plan que ya se calculó."""
        nombres = ", ".join(parte.nombre for parte in partes)
        self._encolar(
            "indexar",
            [parte.trabajo for parte in partes],
            f"Se indexa {nombres}",
        )

    def _completar_estas(self, partes) -> None:
        """Cierra solo estos batches, sin volver a escribir sus páginas."""
        nombres = ", ".join(parte.nombre for parte in partes)
        self._encolar(
            "completar",
            [parte.trabajo for parte in partes],
            f"Se completa {nombres}",
        )

    def _cancelar_estas(self, partes, cancelar: bool) -> None:
        """Saca estos batches de la cola, o los devuelve a ella."""
        for parte in partes:
            self._cancelar_una(parte, cancelar)

    def _cancelar_una(self, parte, cancelar: bool) -> None:
        """Saca este batch de la cola, o lo devuelve a ella.

        No deshace nada de lo hecho: un batch cancelado conserva su ID y lo
        que ya se le escribió. Solo deja de subirse, de buscarse y de
        indexarse hasta que alguien lo reanude.
        """
        from app.airvault.flujo import estado_local

        trabajo = parte.trabajo
        trabajo.manifiesto.cancelado = bool(cancelar)
        trabajo.guardar()
        if cancelar:
            # Lo que estuviera esperando turno para este batch deja de
            # tener sentido; lo que ya esté en vuelo termina su paso.
            self._cola_de_acciones = [
                (modo, [
                    otro for otro in trabajos if otro is not trabajo
                ])
                for modo, trabajos in self._cola_de_acciones
            ]
            self._cola_de_acciones = [
                (modo, trabajos)
                for modo, trabajos in self._cola_de_acciones if trabajos
            ]
        self._estados = [
            estado_local(otra.trabajo) if otra.trabajo is trabajo else otra
            for otra in self._estados
        ]
        self._pintar_lotes()
        self._ajustar_vigilancia()
        self._anotar(
            f"{parte.nombre}: {'cancelado en' if cancelar else 'reanudado en'} "
            "la cola"
        )

    def _rutas_del_batch(self, trabajo) -> Optional[list[Path]]:
        """Lo que se va a la Papelera al eliminar un batch, y nada más.

        Una entrega repartida deja cada batch en su propia carpeta
        («parte-02», «revisar»): ahí se va la carpeta entera, con el PDF que
        se preparó para subirlo. Sin repartir, el batch vive directamente en
        la carpeta de la ejecución, junto al registro que es de la entrega
        entera y a los manifiestos apartados de repartos anteriores; ahí solo
        se va su manifiesto, porque lo demás no es suyo.

        Devuelve ``None`` si la carpeta no cuelga de la de trabajos, y eso no
        es lo mismo que no tener nada que borrar: es un batch que no se sabe
        de dónde salió, y sacarlo de la cola sin tocar el disco lo devolvería
        a ella en cuanto se recargara la ejecución.
        """
        from app.airvault import registro as registro_de_entrega
        from app.airvault.flujo import CARPETA_TRABAJOS
        from app.airvault.manifest import ruta_manifiesto

        raiz_trabajos = (self._raiz / CARPETA_TRABAJOS).resolve()
        carpeta = Path(trabajo.carpeta)
        try:
            resuelta = carpeta.resolve()
        except OSError:
            return None
        if (
            resuelta == raiz_trabajos
            or not resuelta.is_relative_to(raiz_trabajos)
        ):
            return None
        if registro_de_entrega.raiz_de_registro(carpeta) != carpeta:
            return [carpeta] if carpeta.is_dir() else []
        manifiesto = ruta_manifiesto(carpeta)
        return [manifiesto] if manifiesto.is_file() else []

    def _eliminar_estas(self, partes) -> None:
        """Saca estos batches de la cola local para siempre.

        No es cancelar. Un batch cancelado sigue en la cola con su ID y con
        sus bitácoras apuntadas, y por eso ningún reparto posterior las
        vuelve a mandar. Eliminarlo retira sus archivos locales y conserva
        solo una marca de supresión: al reiniciar o preparar la entrega no
        reaparece. Lo que ya esté en AirVault no se toca, que no vive aquí.
        """
        from app.airvault import registro as registro_de_entrega
        from app.airvault.flujo import estado_local

        partes = [parte for parte in partes if parte is not None]
        if not partes:
            return
        if self.hilo() is not None:
            QMessageBox.information(
                self,
                "Eliminar el batch",
                "Esta ejecución se está subiendo o indexando ahora mismo. "
                "Cancele el trabajo antes de eliminar batches de la cola.",
            )
            return
        rutas_de = {
            id(parte.trabajo): self._rutas_del_batch(parte.trabajo)
            for parte in partes
        }
        ajenas = [
            parte for parte in partes if rutas_de[id(parte.trabajo)] is None
        ]
        if ajenas:
            # Sacarlas de la cola sin tocar el disco sería mentir: vuelven
            # en cuanto se recargue la ejecución.
            QMessageBox.information(
                self,
                "Eliminar el batch",
                "Estos batches no están en la carpeta de trabajos del "
                "programa, así que no se eliminan desde aquí:\n\n"
                + "\n".join(
                    f"- {parte.nombre or '(sin nombre)'}" for parte in ajenas
                ),
            )
            partes = [parte for parte in partes if parte not in ajenas]
            if not partes:
                return
        nombres = "\n".join(
            f"- {parte.nombre or '(sin nombre)'}" for parte in partes
        )
        cuantos = (
            "este batch" if len(partes) == 1
            else f"estos {len(partes)} batches"
        )
        subidos = sum(
            1 for parte in partes if parte.trabajo.manifiesto.batch_id
        )
        aviso = ""
        if subidos:
            cuales = (
                "Ya está en AirVault" if subidos == len(partes) == 1
                else f"{subidos} de ellos ya están en AirVault"
                if subidos > 1 else "Uno de ellos ya está en AirVault"
            )
            aviso = (
                f"\n\n{cuales}. El batch remoto se queda donde está, pero "
                "aquí se pierde el rastro de que fue este trabajo el que lo "
                "subió."
            )
        respuesta = QMessageBox.warning(
            self,
            "Eliminar el batch",
            f"Se enviará a la Papelera lo que el programa guarda de "
            f"{cuantos}:\n\n{nombres}\n\n"
            "Saldrán de la cola local y no volverán a crearse al reiniciar "
            "el programa ni al preparar otra vez esta ejecución."
            f"{aviso}\n\n¿Desea continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if respuesta != QMessageBox.StandardButton.Yes:
            return

        rutas = [
            ruta for parte in partes
            for ruta in rutas_de[id(parte.trabajo)] or []
        ]
        movidos, fallidos = send_to_trash(rutas) if rutas else ([], [])
        if fallidos:
            detalle = "\n".join(
                f"- {ruta.name}" for ruta, error in fallidos[:5]
            )
            for ruta, error in fallidos:
                logger.warning("No se pudo enviar a la Papelera {}: {}", ruta, error)
            QMessageBox.warning(
                self,
                "No se pudo eliminar el batch",
                "No se pudieron enviar a la Papelera:\n" + detalle,
            )
        atascadas = {str(ruta).casefold() for ruta, _ in fallidos}
        idas = [
            parte for parte in partes
            if not any(
                str(ruta).casefold() in atascadas
                for ruta in rutas_de[id(parte.trabajo)] or []
            )
        ]
        if not idas:
            return

        # Un batch tomado en AirVault y borrado aquí se quedaría bloqueado
        # para quien lo abriera después, y ya no queda en la cola nadie que
        # lo suelte al cerrar. Se suelta ahora, y solo los eliminados.
        cliente = self._estado.get("cliente")
        if cliente is not None:
            hilo = SoltarLotesWorker(
                [parte.trabajo for parte in idas], cliente, self
            )
            self._soltando.append(hilo)
            hilo.finished.connect(
                lambda: self._soltando.remove(hilo)
                if hilo in self._soltando else None
            )
            hilo.start()

        # El registro es de la entrega entera y una selección puede mezclar
        # batches de varias, así que se reescribe uno por entrega.
        from app.airvault import duplicados as libro_de_envios

        por_entrega: dict[Path, dict[str, list]] = {}
        for parte in idas:
            carpeta = Path(parte.trabajo.carpeta)
            grupo = por_entrega.setdefault(
                registro_de_entrega.raiz_de_registro(carpeta),
                {"carpetas": [], "paginas": []},
            )
            grupo["carpetas"].append(carpeta)
            grupo["paginas"].extend(
                (registro.archivo_origen, int(registro.pagina_origen))
                for registro in parte.trabajo.manifiesto.registros
                if not registro.es_separador and registro.archivo_origen
            )
        for entrega, grupo in por_entrega.items():
            try:
                registro_de_entrega.olvidar(
                    entrega,
                    grupo["carpetas"],
                    grupo["paginas"],
                )
                libro_de_envios.olvidar(
                    entrega.parent,
                    grupo["carpetas"],
                )
            except OSError:
                self._anotar(
                    f"No se pudo guardar la eliminación de {entrega.name}"
                )

        fuera = {id(parte.trabajo) for parte in idas}
        self._trabajos = [
            trabajo for trabajo in self._trabajos if id(trabajo) not in fuera
        ]
        # Lo que estuviera esperando turno para un batch que ya no existe no
        # tiene a qué volver.
        self._cola_de_acciones = [
            (modo, [
                trabajo for trabajo in trabajos if id(trabajo) not in fuera
            ])
            for modo, trabajos in self._cola_de_acciones
        ]
        self._cola_de_acciones = [
            (modo, trabajos)
            for modo, trabajos in self._cola_de_acciones if trabajos
        ]
        self._estados = [estado_local(trabajo) for trabajo in self._trabajos]
        self._pintar_lotes()
        self._ajustar_vigilancia()
        self.boton_revisar.setEnabled(bool(self._trabajos))
        self.boton_reiniciar.setEnabled(bool(self._trabajos))
        self._actualizar_boton_eliminar_registros()
        for parte in idas:
            self._anotar(
                f"{parte.nombre or '(sin nombre)'}: eliminado de la cola"
            )

    def _fila_vigilancia(self, fila: QGridLayout, inicio: int) -> None:
        """Cada cuánto se pregunta automáticamente a AirVault."""
        self.auto_check = QCheckBox("Revisar cada")
        # Esperar a que AirVault los deje listos va dentro de «Subir a
        # AirVault» y no se elige aparte; esta casilla no decide si se
        # espera, sino cada cuánto se pregunta, y vale mientras la ventana
        # esté abierta: apagarla deja la cadena parada aquí, y la línea de
        # pasos de la ventana principal lo enseña en rojo.
        self.auto_check.setChecked(True)
        self.auto_check.setToolTip(
            "Pregunta a AirVault cada tantos minutos si ya detectó lo subido. "
            "Apagado, hay que pulsar «Revisar en AirVault»."
        )
        self.auto_check.toggled.connect(self._ajustar_vigilancia)
        recordar(AIRVAULT, "revisar_cada", self.auto_check)
        fila.addWidget(self.auto_check, inicio, 0)

        self.minutos_spin = QSpinBox()
        self.minutos_spin.setRange(1, 60)
        self.minutos_spin.setValue(MINUTOS_POR_DEFECTO)
        self.minutos_spin.setSuffix(" min")
        self.minutos_spin.setToolTip(
            "Cada cuánto se le pregunta a AirVault. Preguntar más seguido no "
            "apura la cola."
        )
        self.minutos_spin.valueChanged.connect(self._ajustar_vigilancia)
        # Las dos se reponen con la señal bloqueada: arrancar el reloj aquí
        # no serviría de nada, porque todavía no hay batches que esperar.
        # «_ajustar_vigilancia» corre en cuanto la cola se llena y lee de
        # estos dos controles cada cuánto preguntar.
        recordar(AIRVAULT, "minutos", self.minutos_spin)
        self.minutos_control = SpinBoxWithButtons(self.minutos_spin)
        self.minutos_control.setMaximumWidth(160)
        self.minutos_control.layout().setSpacing(SPACE_XS)
        fila.addWidget(self.minutos_control, inicio, 1, Qt.AlignmentFlag.AlignLeft)

        # Los mismos pasos que en la ventana principal y el mismo menú: no
        # es una copia sino el mismo ajuste visto desde aquí, que es donde
        # se está mirando mientras AirVault trabaja. Empotrado ocupaba media
        # ventana; en un menú no le quita sitio a la bitácora.
        self.boton_automatizacion = QToolButton()
        self.boton_automatizacion.setText("Automatización")
        self.boton_automatizacion.setToolTip(
            "Hasta dónde sigue el trabajo solo: subir, indexar y completar. "
            "La misma elección que en la ventana principal."
        )
        self.menu_automatizacion = MenuAutomatizacion(self._opciones, self)
        configure_menu_button(
            self.boton_automatizacion, self.menu_automatizacion
        )
        self.boton_automatizacion.setMaximumWidth(self.minutos_control.maximumWidth())
        fila.addWidget(self.boton_automatizacion, inicio, 2)

        self.completar_check = QCheckBox("Completar batch")
        self.completar_check.setChecked(self._opciones.completar)
        self.completar_check.setToolTip(
            "Al terminar de escribir, cierra el batch con «Complete» y lo "
            "manda a Web Search. Solo se acepta con todas las páginas en "
            "verde."
        )
        self.completar_check.toggled.connect(self._al_cambiar_completar)
        fila.addWidget(self.completar_check, inicio + 1, 0)

        # Continuar y reiniciar vivían escondidos detrás de «Automatización…»,
        # junto a unas casillas que ahora son un menú. Son acciones de esta
        # ventana, no ajustes, así que se quedan a la vista: son lo que se
        # pulsa cuando un batch quedó a medias.
        self.boton_continuar = QPushButton("Continuar")
        self.boton_continuar.setToolTip(
            "Continúa desde el primer paso sin terminar; no repite las "
            "páginas en verde."
        )
        self.boton_continuar.clicked.connect(self._continuar_pendiente)
        self.boton_continuar.setMaximumWidth(160)
        fila.addWidget(self.boton_continuar, inicio + 1, 1)

        self.boton_reiniciar = QPushButton("Reiniciar")
        self.boton_reiniciar.setToolTip(
            "Reinicia el estado local del batch elegido, o de todos los "
            "incompletos si no hay ninguno. No borra nada en AirVault."
        )
        self.boton_reiniciar.clicked.connect(self._reiniciar_incompleto)
        self.boton_reiniciar.setMaximumWidth(self.minutos_control.maximumWidth())
        fila.addWidget(self.boton_reiniciar, inicio + 1, 2)

    def _al_cambiar_automatizacion(self, paso: str, marcado: bool) -> None:
        """Refleja lo que se eligió en la ventana principal.

        La casilla que esta ventana sigue enseñando («Completar batch») es
        el mismo ajuste que el de allá, así que se mueve sola cuando se
        toca el otro lado.
        """
        from app.gui.automatizacion import INDEXAR
        if paso == INDEXAR:
            self._estado["indexar_al_encontrar"] = bool(marcado)
            if hasattr(self, "bitacora"):
                self._anotar("Indexado automático activado" if marcado else
                             "Indexado automático desactivado: termina el batch en curso y conserva los pendientes")
                self._pintar_avance()
                self._ajustar_vigilancia()
            return
        if paso != COMPLETAR:
            return
        casilla = getattr(self, "completar_check", None)
        if casilla is None:
            return
        if casilla.isChecked() != marcado:
            casilla.setChecked(marcado)
        if hasattr(self, "auto_check"):
            self._ajustar_vigilancia()

    def _fila_avance(self) -> QHBoxLayout:
        """Barra de toda la cola y reloj del paso; el detalle, en la bitácora.

        La barra cuenta el proceso entero, de subir el primer batch a cumplir
        la meta elegida en el último. Antes se llenaba y vaciaba en cada paso (subir
        un archivo, leer un batch, escribirlo) y no decía cuánto faltaba.
        """
        fila = QHBoxLayout()
        self.estado_label = ElidedLabel("", parent=self)
        self.estado_label.hide()
        self.progreso = QProgressBar()
        self.progreso.setRange(0, 100)
        self.progreso.setValue(0)
        self.progreso.setToolTip(
            "Avance de toda la cola: subir y cumplir la meta elegida, indexar o completar."
        )
        # Cuánto lleva el paso actual. Sin esto, una espera de AirVault y un
        # programa colgado se ven exactamente igual. Vive dentro de la barra
        # para no reservarle una columna y dejar el progreso corto.
        self.reloj_label = QLabel("", self.progreso)
        pintar_del_tema(
            self.reloj_label, lambda: f"color: {color_ayuda()};"
        )
        self.reloj_label.setMinimumWidth(56)
        self.reloj_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        reloj = QHBoxLayout(self.progreso)
        reloj.setContentsMargins(SPACE_S, 0, SPACE_S, 0)
        reloj.addStretch()
        reloj.addWidget(self.reloj_label)
        fila.addWidget(self.progreso, 1)
        return fila

    def _bitacora(self) -> CopyableListWidget:
        """Lo que va haciendo, paso a paso y con la hora.

        Un batch tarda lo suyo y pasa por etapas muy distintas (subir,
        esperar a que AirVault lo procese, leer el batch, escribir). Con una
        sola línea de estado no había forma de saber en cuál estaba ni
        cuánto llevaba, y una espera larga no se distinguía de un cuelgue.
        """
        lista = CopyableListWidget()
        lista.setObjectName("airvaultBitacora")
        lista.setToolTip("Lo que el indexado va haciendo, con la hora de cada paso")
        lista.setMinimumHeight(self._densidad.airvault_log_min_height)
        lista.setWordWrap(True)
        lista.setTextElideMode(Qt.TextElideMode.ElideNone)
        lista.setResizeMode(QListView.ResizeMode.Adjust)
        lista.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        lista.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.bitacora = lista
        return lista

    def _fila_politica_duplicados(self, fila: QGridLayout, inicio: int) -> None:
        self.detener_duplicados_check = QCheckBox("Detener subida por duplicadas")
        self.detener_duplicados_check.setChecked(self._config.detener_por_duplicados)
        self.detener_duplicados_check.setToolTip(
            "Consulta las bitácoras en AirVault y detiene al alcanzar el porcentaje elegido. "
            "El mismo PDF siempre requiere advertencia y confirmación para reenviarse."
        )
        self.porcentaje_duplicados_spin = QSpinBox()
        self.porcentaje_duplicados_spin.setRange(1, 100)
        self.porcentaje_duplicados_spin.setSuffix(" %")
        self.porcentaje_duplicados_spin.setValue(self._config.porcentaje_duplicados)
        self.porcentaje_duplicados_spin.setToolTip(
            "Bitácoras ya presentes / total de bitácoras del batch. "
            "Se detiene cuando el porcentaje es igual o mayor. No cuenta separadores."
        )
        self.porcentaje_duplicados_spin.valueChanged.connect(
            lambda _: self._guardar_politica_duplicados(self.detener_duplicados_check.isChecked())
        )
        self.porcentaje_duplicados_control = SpinBoxWithButtons(self.porcentaje_duplicados_spin)
        self.porcentaje_duplicados_control.setMaximumWidth(160)
        self.porcentaje_duplicados_control.layout().setSpacing(SPACE_XS)
        self.detener_duplicados_check.toggled.connect(self._guardar_politica_duplicados)
        fila.addWidget(self.detener_duplicados_check, inicio, 0, 1, 3)
        self._etiqueta_duplicados = QLabel("Máximo de duplicadas:")
        fila.addWidget(self._etiqueta_duplicados, inicio + 1, 0)
        fila.addWidget(self.porcentaje_duplicados_control, inicio + 1, 1, Qt.AlignmentFlag.AlignLeft)

    def _fila_botones(self) -> QHBoxLayout:
        fila = QHBoxLayout()
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(SPACE_M)

        fila.addStretch()

        self.boton_subir = QPushButton("Subir a AirVault")
        self.boton_subir.setObjectName("primaryButton")
        self.boton_subir.setEnabled(False)
        self.boton_subir.setToolTip(
            "Busca en AirVault los batches de la entrega y sube solo los que "
            "falten. Si uno se queda atascado, al pulsarlo de nuevo se "
            "reenvía."
        )
        self.boton_subir.clicked.connect(self._subir_a_mano)

        self.boton_revisar = QPushButton("Revisar en AirVault")
        self.boton_revisar.setEnabled(False)
        self.boton_revisar.setToolTip(
            "Comprueba en AirVault cada batch y termina lo que falte: sube "
            "los que no se subieron, indexa los confirmados y, con «Completar "
            "batch», los completa."
        )
        self.boton_revisar.clicked.connect(self._revisar_a_mano)
        # Conserva el nombre interno que usa la comprobación automática y
        # el código que habilita los controles mientras trabaja el hilo.
        self.boton_comprobar = self.boton_revisar

        self.boton_indexar = QPushButton("Indexar")
        self.boton_indexar.setEnabled(False)
        self.boton_indexar.setToolTip(
            "Escribe en AirVault los datos de los batches listos y borra las "
            "páginas separadoras. Las bloqueadas se saltan."
        )
        self.boton_indexar.clicked.connect(self._indexar)

        # Siempre disponible mientras hay trabajo en vuelo. Es lo que
        # convierte una espera larga en algo de lo que se puede salir: sin
        # él, una sesión que no llega o un batch que AirVault no suelta
        # dejaban la ventana sin nada que pulsar durante minutos.
        self.boton_cancelar = QPushButton("Cancelar")
        self.boton_cancelar.setEnabled(False)
        self.boton_cancelar.setToolTip(
            "Detiene el trabajo y desbloquea los batches abiertos. Lo ya "
            "escrito se conserva."
        )
        self.boton_cancelar.clicked.connect(self._cancelar)

        self.boton_cerrar = QPushButton("Cerrar")
        self.boton_cerrar.clicked.connect(self.close)

        for boton in (
            self.boton_subir, self.boton_revisar,
            self.boton_indexar, self.boton_cancelar, self.boton_cerrar,
        ):
            fila.addWidget(boton)
        return fila

    # ── el historial ───────────────────────────────────────────────

    def showEvent(self, event) -> None:
        """Rehace el historial cada vez que la ventana se muestra.

        Vive escondida mientras se procesa, y una ejecución recién
        exportada tiene que estar en la lista sin cerrar nada.
        """
        super().showEvent(event)
        self._acotar_a_la_pantalla()
        self._refrescar_historial()
        if self.hilo() is None:
            self._cargar_sidebar()

    def _acotar_a_la_pantalla(self) -> None:
        """Permite apilar el formulario y conserva su alto dentro del escritorio.

        El minimo horizontal no depende de los dos pares de campos: de lo
        contrario impediria estrechar la ventana antes de que el formulario
        pueda apilarse. El alto se mide con la distribucion activa, tambien
        al cambiarla, para mantener accesibles los controles y el registro.
        """
        disponible = available_area(self)
        self.lotes.setMinimumHeight(80 if disponible.height() < 600 else self._densidad.airvault_table_min_height)
        self._root_layout.invalidate()
        pedido = self.minimumSizeHint()
        self.setMinimumSize(
            ANCHO_MINIMO_VENTANA,
            max(ALTO_MINIMO_VENTANA, min(pedido.height(), disponible.height())),
        )
        # Levantar el tope no encoge sola a la ventana que ya habia crecido:
        # hay que devolverla dentro de la pantalla, y volver a entrar si se
        # habia quedado con una esquina fuera.
        alto = min(self.height(), disponible.height())
        ancho = min(self.width(), disponible.width())
        if (ancho, alto) != (self.width(), self.height()):
            self.resize(ancho, alto)
        marco = self.frameGeometry()
        if not disponible.contains(marco):
            marco.moveLeft(
                max(disponible.left(), min(marco.left(), disponible.right() - marco.width()))
            )
            marco.moveTop(
                max(disponible.top(), min(marco.top(), disponible.bottom() - marco.height()))
            )
            self.move(marco.topLeft())

    def _refrescar_historial(self) -> None:
        corridas = [
            (carpeta, csv_de_corrida(carpeta))
            for carpeta in find_run_dirs(
                self._raiz / "output", LIMITE_HISTORIAL
            )
        ]
        combo = self.historial
        # Rehacer la lista mueve la opcion elegida; las senales se cortan
        # para que eso no se lea como que alguien eligio otra ejecucion y
        # tire lo hecho.
        with QSignalBlocker(combo):
            combo.clear()
            # La opcion con la que abre. Sin ella la lista ensena la
            # ejecucion mas reciente como si ya estuviera elegida, y dice
            # en su sitio lo que antes decia una frase encima.
            combo.addItem(TEXTO_ELEGIR_EJECUCION)
            for carpeta, csv in corridas:
                if csv is not None:
                    self._agregar_ejecucion(carpeta, csv)
        self._actualizar_boton_eliminar_registros()
        if combo.count() <= 1:
            self.resumen.setText(
                "No hay ejecuciones procesadas. Procese y exporte una para "
                "subirla."
            )
            return
        if not self._corrida.strip():
            self.fijar_corrida(self._primera_que_se_puede_subir())
        else:
            self._marcar_en_historial(self._corrida)

    def _primera_que_se_puede_subir(self) -> str:
        """CSV con el que abrir: la ejecución exportada más reciente.

        Sin nada elegido se propone una, y proponer la más reciente a secas
        deja la ventana señalando una ejecución que todavía no se puede
        subir cuando lo último que se hizo fue procesar sin exportar.
        """
        combo = self.historial
        for indice in range(1, combo.count()):
            if combo.itemData(indice, ROL_SE_PUEDE_SUBIR):
                return combo.itemData(indice)
        return combo.itemData(1)

    def _agregar_ejecucion(self, carpeta: Path, csv: Path) -> None:
        """Mete una ejecución en la lista, con su nombre y nada más.

        El nombre es lo que se busca al desplegarla, y es lo único que se
        lee de un vistazo: sus páginas y en qué quedó su entrega llevaban la
        línea al doble de largo para responder algo que no se estaba
        preguntando. Van al aviso que sale al posarse encima, que es donde
        se miran cuando hacen falta.
        """
        # El historial solo necesita saber si se puede subir. Calcular aquí
        # el reparto completo de cada CSV repetía hasta 25 recorridos grandes
        # al abrir la ventana; el número de batches se calcula en la vista
        # previa, donde sí se usa.
        entrega, listo = estado_de_entrega(csv)
        paginas = paginas_de_corrida(carpeta)
        cuenta = "sin contar" if paginas is None else f"{paginas} pág."
        indice = self.historial.count()
        self.historial.addItem(carpeta.name, str(csv))
        self.historial.setItemData(
            indice,
            f"{carpeta.name}\n{cuenta} - {entrega}",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.historial.setItemData(indice, listo, ROL_SE_PUEDE_SUBIR)
        if listo:
            self.historial.setItemData(
                indice,
                Qt.CheckState.Checked if str(csv) in self._corridas_marcadas
                else Qt.CheckState.Unchecked,
                Qt.ItemDataRole.CheckStateRole,
            )
        if not listo:
            # Sale en la lista igualmente: quien la busca tiene que verla, y
            # el gris es lo que dice que todavía le falta exportarla.
            self.historial.setItemData(
                indice,
                QBrush(Qt.GlobalColor.gray),
                Qt.ItemDataRole.ForegroundRole,
            )

    def _al_elegir_del_historial(self, indice: int) -> None:
        """Apunta la ventana a la ejecución que se acaba de elegir.

        Es la única forma de cambiar de ejecución aquí. Volver a
        «Seleccionar ejecución» no descarga nada: es la opción con la que la
        lista abre, no una orden de soltar lo que se está subiendo. Deja la
        lista como estaba y se queda donde está.
        """
        csv = self.historial.itemData(indice)
        if not csv:
            self._marcar_en_historial(self._corrida)
            return
        if self.hilo() is not None:
            if not self._es_ejecucion_seleccionada(csv):
                self.abrir_corrida_paralela.emit(str(csv))
            self._marcar_en_historial(self._corrida)
            return
        if csv == self._corrida and len(self._corridas_marcadas) <= 1:
            return
        self.fijar_corrida(csv)

    def _corridas_seleccionadas(self) -> list[str]:
        """La seleccion en el orden del historial, sin repetir ejecuciones."""
        ordenadas = [
            str(self.historial.itemData(i))
            for i in range(1, self.historial.count())
            if str(self.historial.itemData(i)) in self._corridas_marcadas
        ]
        ordenadas.extend(sorted(self._corridas_marcadas.difference(ordenadas)))
        return ordenadas

    def _fecha_de_ejecucion(self, csv: str) -> bool:
        """Respeta la fecha elegida antes, incluso en ejecuciones retomadas."""
        if csv == self._corrida:
            return self.fin_de_mes()
        fechas = {
            t.manifiesto.fin_de_mes for t in self._trabajos
            if str(Path(t.manifiesto.csv_origen)).casefold() == str(Path(csv)).casefold()
        }
        return fechas.pop() if len(fechas) == 1 else not run_read_day(Path(csv))

    def _al_marcar_ejecuciones(self) -> None:
        """Reune las ejecuciones marcadas en la misma cola de trabajo."""
        if self.hilo() is not None:
            return
        self._corridas_marcadas = {
            str(self.historial.itemData(i))
            for i in range(1, self.historial.count())
            if self.historial.itemData(i, Qt.ItemDataRole.CheckStateRole)
            == Qt.CheckState.Checked
        }
        elegidas = self._corridas_seleccionadas()
        if not elegidas:
            self._parar_vigilancia()
            self._corrida = ""
            self.historial.setCurrentIndex(0)
            self.lote_edit.clear()
            self._listo_para_subir = False
            self._mostrar_reparto(None)
            self.resumen.setText("Marque una o varias ejecuciones para subirlas.")
            self._pintar_lotes()
            self._habilitar(True)
            return
        csv = self._corrida if self._corrida in elegidas else elegidas[0]
        self.fijar_corrida(csv, conservar_seleccion=True)

    def _actualizar_seleccion(self) -> None:
        """Refleja el alcance y conserva las fechas propias al subir varias."""
        elegidas = self._corridas_seleccionadas()
        varias = len(elegidas) > 1
        self.solo_ejecucion_check.setText(
            "Solo ejecuciones seleccionadas" if varias
            else "Solo la ejecución seleccionada"
        )
        if varias:
            self.setWindowTitle(f"Indexar en AirVault - {len(elegidas)} ejecuciones")
            self._listo_para_subir = all(estado_de_entrega(Path(csv))[1] for csv in elegidas)
            repartos = [reparto_de_revision(Path(csv)) for csv in elegidas]
            self._mostrar_reparto(
                tuple(sum(r[i] for r in repartos) for i in range(2))
                if all(r is not None for r in repartos) else None
            )
            self.resumen.setText(
                f"{len(elegidas)} ejecuciones seleccionadas. Cada una conserva "
                "su nombre y fecha de indexado."
            )
        self._habilitar(self.hilo() is None)

    def _marcar_en_historial(self, csv: Path | str) -> None:
        """Deja elegida en la lista la ejecución abierta, si está en ella."""
        combo = self.historial
        texto = str(csv).strip()
        clave = str(Path(texto)).casefold() if texto else ""
        with QSignalBlocker(combo):
            for indice in range(1, combo.count()):
                if combo.itemData(indice, ROL_SE_PUEDE_SUBIR):
                    combo.setItemData(
                        indice,
                        Qt.CheckState.Checked if str(combo.itemData(indice)) in self._corridas_marcadas
                        else Qt.CheckState.Unchecked,
                        Qt.ItemDataRole.CheckStateRole,
                    )
            for indice in range(1, combo.count()):
                dato = combo.itemData(indice)
                if dato and str(Path(dato)).casefold() == clave:
                    combo.setCurrentIndex(indice)
                    return
            # Abierta desde la ventana principal al exportar, o de una
            # ejecución que ya salió de las últimas de la lista: dejar
            # marcada otra haría creer que se sube esa.
            if combo.count():
                combo.setCurrentIndex(0)

    # ── estado de la ejecución ─────────────────────────────────────

    def _sincronizar_fecha(self, csv: Path) -> None:
        """Deja el desplegable de fecha en lo que esa ejecución permite.

        Se abre en lo que la ejecución trae escrito, no en lo que se
        prefiere en general: lo que se ve en la lista es lo que se va a
        escribir, y pasar a fin de mes es una decisión que se toma aquí.
        """
        leyo_el_dia = run_read_day(csv)
        entrada = self.fecha_combo.model().item(1)
        if entrada is not None:
            entrada.setEnabled(leyo_el_dia)
        with QSignalBlocker(self.fecha_combo):
            self.fecha_combo.setCurrentIndex(0 if not leyo_el_dia else 1)
        self.fecha_combo.setToolTip(
            TOOLTIP_FECHA_INDEXADO if leyo_el_dia else TOOLTIP_FECHA_SIN_DIA
        )

    def fin_de_mes(self) -> bool:
        """Si las bitácoras se escriben con el último día del mes."""
        return bool(self.fecha_combo.currentData())

    def fijar_corrida(self, csv: Path | str, conservar_seleccion: bool = False) -> None:
        """Apunta la ventana a una ejecución y propone el nombre del batch."""
        from app.airvault.flujo import carpeta_de_corrida, carpeta_de_trabajo
        from app.airvault.naming import nombre_desde_corrida

        misma = conservar_seleccion and str(csv) == self._corrida
        if not misma:
            self._alcance_proceso = None
            self._avance_por_batch.clear()
        nombre_previo = self.lote_edit.text() if misma else None
        fecha_previa = self.fin_de_mes() if misma else None

        # Cambiar de ejecución tira lo hecho, y con ello los batches que
        # hubieran quedado tomados en AirVault: sin soltarlos quedan
        # colgados para quien los abra después.
        self._soltar_lotes()
        self._parar_vigilancia()
        ruta = Path(csv)
        if not conservar_seleccion:
            self._corridas_marcadas = {str(ruta)}
        self.setWindowTitle(f"Indexar en AirVault - {ruta.parent.parent.name}")
        self._corrida = str(ruta)
        self.lote_edit.setText(nombre_desde_corrida(ruta))
        self.boton_indexar.setEnabled(False)
        self._marcar_en_historial(ruta)
        self._sincronizar_entrega(ruta)
        self._sincronizar_fecha(ruta)
        # Una ejecución que ya se subió en otro momento se retoma sin
        # volver a subir nada: sus manifiestos dicen en qué quedó.
        carpeta = self._raiz / carpeta_de_trabajo(carpeta_de_corrida(csv).name)
        self._cargar_trabajos(carpeta, ruta)
        if misma:
            self.lote_edit.setText(nombre_previo)
            with QSignalBlocker(self.fecha_combo):
                self.fecha_combo.setCurrentIndex(self.fecha_combo.findData(fecha_previa))
        self._actualizar_seleccion()

    def corrida(self) -> Optional[Path]:
        """La ejecución a la que apunta la ventana, si ya hay una."""
        texto = self._corrida.strip()
        return Path(texto) if texto else None

    def subir_automaticamente(self) -> None:
        """Arranca la subida sin que nadie pulse «Subir a AirVault».

        Es el único punto por el que el proceso automático de la ventana
        principal entra aquí. De este paso en adelante manda la cadena de
        esta ventana, que ya sabe hasta dónde continuar y lo cuenta en su
        bitácora.
        """
        from app.gui.automatizacion import CORTADO, EN_CURSO, SUBIR

        self.show()
        self.raise_()
        self.activateWindow()
        if self.hilo() is not None:
            self._anotar(
                "Hay trabajo en curso: la subida automática no se lanza"
            )
            self.avance_automatico.emit(SUBIR, CORTADO)
            return
        if not self._listo_para_subir:
            # ``_sincronizar_entrega`` ya dejó escrito el motivo entero en
            # el resumen; en la bitácora basta con qué empieza, y con hora.
            self._anotar(
                f"No se puede subir todavía: "
                f"{primera_frase(self.resumen.text())}"
            )
            self.avance_automatico.emit(SUBIR, CORTADO)
            return
        self.avance_automatico.emit(SUBIR, EN_CURSO)
        self._anotar("Subida pedida por el proceso automático")
        self._subir()

    def _cargar_trabajos(self, carpeta: Path, csv: Path) -> None:
        """Retoma los trabajos que ya existan para esta ejecución."""
        from app.airvault.flujo import (CARPETA_TRABAJOS, SIN_SUBIR,
                                        carpeta_de_corrida, carpeta_de_trabajo,
                                        cargar_partes,
                                        cargar_todos_trabajos,
                                        estado_local)

        # La cantidad es una preferencia compartida con la exportacion.
        # Se recupera tambien si otra ventana la cambio estando esta abierta.
        guardadas = AirVaultConfig.load(
            self._raiz / AIRVAULT_FILENAME
        ).paginas_por_batch
        if guardadas is not None:
            self._config = self._config.with_overrides(paginas_por_batch=guardadas)
            with QSignalBlocker(self.limite_batch_spin):
                self.limite_batch_spin.setValue(guardadas)
        try:
            self._trabajos = cargar_partes(self._config_actual(), carpeta, csv)
        except Exception:  # noqa: BLE001 - sin trabajos se empieza de cero
            self._trabajos = []
        trabajos_de_corrida = list(self._trabajos)
        for otra in self._corridas_seleccionadas():
            if otra != str(csv):
                try:
                    self._trabajos.extend(cargar_partes(
                        self._config_actual(),
                        self._raiz / carpeta_de_trabajo(carpeta_de_corrida(otra).name),
                        Path(otra),
                    ))
                except Exception as exc:  # noqa: BLE001 - la subida lo validara
                    logger.warning("No se pudo retomar la ejecucion {}: {}", otra, exc)
        conocidos = {
            str(trabajo.carpeta.resolve()).casefold()
            for trabajo in self._trabajos
        }
        salida_local = (self._raiz / "output").resolve()
        pendientes_globales = (
            cargar_todos_trabajos(
                self._config_actual(),
                self._raiz / CARPETA_TRABAJOS,
            )
            if csv.resolve().is_relative_to(salida_local)
            else []
        )
        self._trabajos.extend(
            trabajo
            for trabajo in pendientes_globales
            if str(trabajo.carpeta.resolve()).casefold() not in conocidos
        )
        self._trabajos.sort(
            key=lambda trabajo: estado_local(trabajo).estado != SIN_SUBIR
        )
        fechas = {t.manifiesto.fin_de_mes for t in trabajos_de_corrida}
        if len(fechas) == 1:
            indice = self.fecha_combo.findData(fechas.pop())
            if indice >= 0:
                with QSignalBlocker(self.fecha_combo):
                    self.fecha_combo.setCurrentIndex(indice)
        # Se pueden cambiar aunque la ejecucion ya tenga batches: lo que ya
        # esta en AirVault se conserva y solo se reparte lo que falta.
        self.limite_batch_spin.setEnabled(self.hilo() is None)
        self.fecha_combo.setEnabled(self.hilo() is None)
        # La conexion sobrevive al cambio de ejecucion: es el mismo
        # servidor, y volver a abrirla es volver a arrancar el navegador.
        self._estado = {
            clave: self._estado[clave]
            for clave in ("cliente", "sesion") if clave in self._estado
        }
        self._estados = [estado_local(t) for t in self._trabajos]
        self._pintar_lotes()
        self.boton_indexar.setEnabled(bool(self._corrida.strip()))
        self.boton_previa.setEnabled(bool(self._corrida.strip()))
        self.boton_revisar.setEnabled(bool(self._trabajos))
        self._actualizar_boton_eliminar_registros()
        self.boton_reiniciar.setEnabled(bool(self._trabajos))
        self._ajustar_vigilancia()
        # Los manifiestos conservan la verificacion y el cierre. Al volver
        # a abrir una ejecucion terminada no falta otro clic para reconocer
        # ese final, ni debe quedarse la barra en 99 %.
        self._fin_pendiente = self._firma_de_fin() is not None
        self._anunciar_fin()

    def _carpeta_del_registro(
        self, corrida: Path | str = ""
    ) -> Optional[Path]:
        """Carpeta local exacta de una ejecución, si es segura.

        Sin ``corrida`` vale la que la ventana tiene abierta, que es lo que
        pide el botón; el menú del historial nombra la fila sobre la que se
        hizo clic, que puede no ser esa.
        """
        from app.airvault.flujo import (CARPETA_TRABAJOS, carpeta_de_corrida,
                                        carpeta_de_trabajo)

        csv = str(corrida or self._corrida).strip()
        if not csv:
            return None
        raiz_trabajos = (self._raiz / CARPETA_TRABAJOS).resolve()
        carpeta = (
            self._raiz
            / carpeta_de_trabajo(carpeta_de_corrida(csv).name)
        ).resolve()
        if (
            carpeta == raiz_trabajos
            or not carpeta.is_relative_to(raiz_trabajos)
        ):
            return None
        return carpeta

    def _rutas_del_registro_en(self, carpeta: Path) -> list[Path]:
        """Batches y memoria local de esa carpeta, nunca de otra.

        Las partes repartidas se eliminan con su carpeta completa para que
        tampoco sobrevivan sus PDF temporales. El batch sin repartir vive en
        la raíz de la entrega, junto a memoria compartida, y de él solo se
        retira el manifiesto.
        """
        from app.airvault.manifest import MANIFIESTO_FILENAME
        from app.airvault.registro import raiz_de_registro, rutas_del_registro

        rutas = {
            ruta for ruta in carpeta.rglob(MANIFIESTO_FILENAME)
            if ruta.is_file() and ruta.resolve().is_relative_to(carpeta)
        }
        rutas.update(
            ruta for ruta in rutas_del_registro(carpeta)
            if ruta.resolve().is_relative_to(carpeta)
        )
        objetivos = {
            ruta.parent
            if ruta.is_file()
            and raiz_de_registro(ruta.parent) != ruta.parent
            else ruta
            for ruta in rutas
        }
        return sorted(
            ruta for ruta in objetivos
            if not any(
                ruta != otra and ruta.resolve().is_relative_to(otra.resolve())
                for otra in objetivos
            )
        )

    @staticmethod
    def _paginas_de_los_batches_en(carpeta: Path) -> list[tuple[str, int]]:
        """Claves del registro y de manifiestos que se van a retirar."""
        from app.airvault.manifest import MANIFIESTO_FILENAME, cargar
        from app.airvault.registro import leer

        guardado = leer(carpeta)
        paginas: set[tuple[str, int]] = {
            clave for batch in guardado.batches for clave in batch.claves()
        }
        paginas.update(
            clave
            for reparto in guardado.historial
            for batch in reparto.batches
            for clave in batch.claves()
        )
        for ruta in carpeta.rglob(MANIFIESTO_FILENAME):
            try:
                manifiesto = cargar(ruta.parent)
            except (OSError, ValueError):
                continue
            paginas.update(
                (registro.archivo_origen, int(registro.pagina_origen))
                for registro in manifiesto.registros
                if not registro.es_separador and registro.archivo_origen
            )
        return sorted(paginas)

    def _rutas_del_registro(self, corrida: Path | str = "") -> list[Path]:
        """Memoria local de la ejecución indicada, nunca de otra."""
        carpeta = self._carpeta_del_registro(corrida)
        if carpeta is None:
            return []
        return self._rutas_del_registro_en(carpeta)

    def _carpetas_de_trabajo_presentes(self) -> list[Path]:
        """Las carpetas de trabajo de AirVault que hay en el disco.

        Esta lista salía antes del historial, y por eso dejaba fuera justo
        lo que más falta hace olvidar: cuando la ejecución ya no está en
        ``output/`` (se borró, o quedó fuera del límite del historial) su
        trabajo sigue en ``output/airvault/`` con sus manifiestos y su
        registro, y desde aquí no había manera de eliminarlos. Se mira la
        carpeta de trabajos, que es donde esa memoria vive de verdad.
        """
        from app.airvault.flujo import CARPETA_TRABAJOS

        raiz = (self._raiz / CARPETA_TRABAJOS).resolve()
        if not raiz.is_dir():
            return []
        return sorted(hijo for hijo in raiz.iterdir() if hijo.is_dir())

    def _registros_presentes(self) -> dict[Path, list[Path]]:
        """Registros locales de todos los trabajos que aún los conservan."""
        return {
            carpeta: rutas
            for carpeta in self._carpetas_de_trabajo_presentes()
            if (rutas := self._rutas_del_registro_en(carpeta))
        }

    def _actualizar_boton_eliminar_registros(self) -> None:
        boton = getattr(self, "boton_eliminar_registro", None)
        if boton is None:
            return
        boton.setEnabled(
            self.hilo() is None and bool(self._registros_presentes())
        )

    def _eliminar_registro(self, corrida: Path | str = "") -> None:
        """Elimina batches locales de una ejecución o de todos los trabajos.

        El menú del historial pasa una ``corrida`` y actúa solo sobre ella.
        El botón no la pasa y limpia todo lo que quede en la carpeta de
        trabajos, esté o no su ejecución en el historial.
        """
        texto = str(corrida).strip()
        if not texto and self.solo_ejecucion_check.isChecked():
            texto = self._corrida.strip()
            if not texto:
                return
        individual = bool(texto)
        if individual:
            carpeta = self._carpeta_del_registro(texto)
            rutas_de = (
                self._rutas_del_registro_en(carpeta)
                if carpeta is not None else []
            )
            registros = {carpeta: rutas_de} if rutas_de else {}
        else:
            registros = self._registros_presentes()
        if not registros:
            QMessageBox.information(
                self,
                "Eliminar registro",
                "No hay registros locales de AirVault que eliminar."
                if not individual else
                "Esa ejecución no tiene registro local de AirVault.",
            )
            return
        paginas = {
            carpeta: self._paginas_de_los_batches_en(carpeta)
            for carpeta in registros
        }
        rutas = sorted({ruta for grupo in registros.values() for ruta in grupo})
        cantidad = len(registros)
        if individual:
            alcance = f"la ejecución «{next(iter(registros)).name}»"
        else:
            alcance = (
                "todos los trabajos de AirVault "
                f"({cantidad} con registro)"
            )
        respuesta = QMessageBox.warning(
            self,
            "Eliminar registros de AirVault",
            f"Se enviarán a la Papelera los batches locales de {alcance} "
            f"({len(rutas)} elemento(s)).\n\n"
            "No volverán a crearse al reiniciar el programa. No se borrarán "
            "los CSV, los PDF de entrega ni los batches existentes en "
            "AirVault.\n\n¿Desea continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if respuesta != QMessageBox.StandardButton.Yes:
            return

        abierta = self.corrida()
        rutas_abiertas = (
            set(self._rutas_del_registro(abierta))
            if abierta is not None else set()
        )
        movidos, fallidos = send_to_trash(rutas)
        movidos_set = set(movidos)
        from app.airvault import duplicados as libro_de_envios
        from app.airvault import registro as registro_de_entrega

        for carpeta_anotada, grupo in registros.items():
            if not movidos_set.intersection(grupo):
                continue
            try:
                registro_de_entrega.eliminar_historial(
                    carpeta_anotada,
                    paginas[carpeta_anotada],
                )
                libro_de_envios.olvidar(
                    carpeta_anotada.parent,
                    [carpeta_anotada],
                    incluir_hijas=True,
                )
            except OSError as exc:
                self._anotar(
                    f"No se pudo guardar la eliminación de "
                    f"{carpeta_anotada.name}: {exc}"
                )
        if abierta is not None and movidos_set & rutas_abiertas:
            carpeta = self._carpeta_del_registro(abierta)
            self._parar_vigilancia()
            self._indexado_incompleto = False
            if carpeta is not None:
                self._cargar_trabajos(carpeta, abierta)
            self.estado_label.setText("Registro local eliminado")
            self.resumen.setText(
                "Registro local eliminado. Los batches remotos no se "
                "modificaron."
            )
        for carpeta_anotada, grupo in registros.items():
            if movidos_set.intersection(grupo):
                self._anotar(
                    "Registro local de AirVault eliminado: "
                    f"{carpeta_anotada.name}"
                )

        if fallidos:
            detalle = "\n".join(
                f"- {ruta.name}" for ruta, error in fallidos[:5]
            )
            for ruta, error in fallidos:
                logger.warning("No se pudo enviar a la Papelera {}: {}", ruta, error)
            QMessageBox.warning(
                self,
                "Registro eliminado parcialmente",
                f"Se eliminaron {len(movidos)} de {len(rutas)} registros.\n\n"
                "No se pudieron eliminar:\n" + detalle,
            )
        self._actualizar_boton_eliminar_registros()

    def _es_la_ejecucion_abierta(self, csv: Path | str) -> bool:
        """Si esa ejecución es la que la ventana tiene cargada ahora."""
        abierta = self._corrida.strip()
        if not abierta:
            return False
        return str(Path(csv)).casefold() == str(Path(abierta)).casefold()

    def _carpeta_de_la_ejecucion(self, csv: Path | str) -> Optional[Path]:
        """Carpeta de output de esa ejecución, si está donde debe estar.

        Se exige que cuelgue de ``output/`` y que no sea la propia carpeta:
        lo que se va a la Papelera es una ejecución concreta, y la ventana
        puede quedar apuntando a un CSV de cualquier sitio.
        """
        from app.airvault.flujo import carpeta_de_corrida

        raiz = (self._raiz / "output").resolve()
        try:
            carpeta = carpeta_de_corrida(Path(csv)).resolve()
        except OSError:
            return None
        if carpeta == raiz or not carpeta.is_relative_to(raiz):
            return None
        return carpeta

    def _eliminar_ejecucion(self, csv: Path | str, nombre: str) -> None:
        """Manda a la Papelera la ejecución entera, con su registro.

        Es lo que vacía el historial de lo que ya no hace falta. Va a la
        Papelera y no al vacío porque una ejecución son horas de proceso, y
        equivocarse de fila tiene que poder deshacerse. Lo que ya esté en
        AirVault no se toca: eso no vive aquí.
        """
        csv = Path(csv)
        carpeta = self._carpeta_de_la_ejecucion(csv)
        if carpeta is None:
            QMessageBox.information(
                self,
                "Eliminar la ejecución",
                "Esta ejecución no está en la carpeta output/ del programa, "
                "así que no se elimina desde aquí.",
            )
            return
        if self._es_ejecucion_seleccionada(csv) and self.hilo() is not None:
            QMessageBox.information(
                self,
                "Eliminar la ejecución",
                "Esta ejecución se está subiendo o indexando ahora mismo. "
                "Cancele el trabajo antes de eliminarla.",
            )
            return
        respuesta = QMessageBox.warning(
            self,
            "Eliminar la ejecución",
            f"Se enviará a la Papelera la ejecución «{nombre}» entera: su "
            "CSV, su JSON, sus estadísticas y los PDF de entrega, junto con "
            "el registro local de AirVault.\n\n"
            "Los batches que ya estén en AirVault no se modifican.\n\n"
            "¿Desea continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if respuesta != QMessageBox.StandardButton.Yes:
            return

        # Primero la memoria de AirVault y después la ejecución: al revés,
        # un fallo al mover la carpeta dejaría un registro que habla de una
        # ejecución que ya no está.
        registro = self._carpeta_del_registro(csv)
        objetivos = [carpeta]
        if registro is not None and registro.is_dir():
            objetivos.insert(0, registro)
        movidos, fallidos = send_to_trash(objetivos)
        if fallidos:
            detalle = "\n".join(
                f"- {ruta.name}" for ruta, error in fallidos[:5]
            )
            for ruta, error in fallidos:
                logger.warning("No se pudo enviar a la Papelera {}: {}", ruta, error)
            QMessageBox.warning(
                self,
                "No se pudo eliminar la ejecución",
                "No se pudieron enviar a la Papelera:\n" + detalle,
            )
        if carpeta not in movidos:
            return

        self._anotar(f"Ejecución eliminada: {nombre}")
        self._corridas_marcadas.discard(str(csv))
        if self._es_la_ejecucion_abierta(csv):
            # La ventana se queda apuntando a una carpeta que ya no existe:
            # se suelta lo que hubiera tomado y se parte de cero.
            self._soltar_lotes()
            self._parar_vigilancia()
            self._corrida = ""
            self.lote_edit.clear()
            self._trabajos = []
            self._estados = []
            self._pintar_lotes()
            self.boton_subir.setEnabled(False)
            self.boton_indexar.setEnabled(False)
            self.boton_previa.setEnabled(False)
        if self._corridas_marcadas:
            self.fijar_corrida(
                self._corrida or self._corridas_seleccionadas()[0],
                conservar_seleccion=True,
            )
        self._refrescar_historial()

    def _sincronizar_entrega(self, csv: Path) -> None:
        """Dice si la ejecución elegida se puede subir, antes de intentarlo."""
        entrega, listo = estado_de_entrega(csv)
        self._mostrar_reparto(reparto_de_revision(csv))
        self._listo_para_subir = listo
        self.boton_subir.setEnabled(listo)
        if listo:
            self.resumen.setText(TEXTO_SIN_SUBIR)
        elif entrega == "Sin exportar":
            self.resumen.setText(
                "No tiene PDF de entrega. Expórtela antes de subirla."
            )
        else:
            self.resumen.setText(
                "Faltan datos de la entrega. Vuelva a exportarla antes de indexar."
            )

    # ── la lista de batches ──────────────────────────────────────────

    def _filtrar_trabajos(
        self, trabajos, solo_ejecucion: Optional[bool] = None,
    ) -> list:
        if solo_ejecucion is None:
            solo_ejecucion = self.solo_ejecucion_check.isChecked()
        if not solo_ejecucion:
            return list(trabajos)
        return [
            trabajo for trabajo in trabajos
            if self._es_ejecucion_seleccionada(trabajo.manifiesto.csv_origen)
        ]

    def _es_ejecucion_seleccionada(self, csv: Path | str) -> bool:
        elegidas = self._corridas_marcadas or {self._corrida}
        return str(Path(csv)).casefold() in {
            str(Path(elegida)).casefold() for elegida in elegidas if elegida
        }

    def _partes_en_cola(self) -> list:
        from app.airvault.flujo import (AUTOCOMPLETADO, COMPLETADO, INDEXADO, PUBLICADO)
        partes = self._partes_del_alcance()
        if self.ocultar_indexados_check.isChecked():
            partes = [p for p in partes if p.estado != INDEXADO]
        if self.ocultar_completados_check.isChecked():
            partes = [p for p in partes if p.estado not in (COMPLETADO, AUTOCOMPLETADO, PUBLICADO)]
        return partes

    def _partes_del_alcance(self) -> list:
        visibles = {id(t) for t in self._filtrar_trabajos(
            parte.trabajo for parte in self._estados
        )}
        return [parte for parte in self._estados if id(parte.trabajo) in visibles]

    def _al_filtrar_vista(self, _marcado: bool) -> None:
        self.lotes.clearSelection()
        self._pintar_lotes()

    def _al_filtrar_ejecucion(self, _marcado: bool) -> None:
        self.lotes.clearSelection()
        self._pintar_lotes()
        self._ajustar_vigilancia()
        self._habilitar(self.hilo() is None)

    def _recibir_trabajos(self, trabajos) -> None:
        """Conserva las ejecuciones ocultas cuando termina una accion filtrada."""
        nuevos = list(trabajos)
        claves = {str(t.carpeta) for t in nuevos}
        nuevos.extend(t for t in self._trabajos if str(t.carpeta) not in claves)
        self._trabajos = nuevos

    def _pintar_lotes(self) -> None:
        """Vuelca en la tabla en qué va cada batch."""
        tabla = self.lotes
        seleccionadas = {item.data(Qt.ItemDataRole.UserRole) for item in tabla.selectedItems()}
        actual = tabla.currentItem().data(Qt.ItemDataRole.UserRole) if tabla.currentItem() else None
        scroll = tabla.verticalScrollBar().value()
        bloqueo = QSignalBlocker(tabla)
        tabla.setUpdatesEnabled(False)
        tabla.setRowCount(0)
        for parte in self._partes_en_cola():
            fila = tabla.rowCount()
            tabla.insertRow(fila)
            nombre = parte.nombre or "(sin nombre)"
            esperadas = len(parte.trabajo.manifiesto.registros)
            indexando = str(parte.trabajo.carpeta) in self._indexando
            # La celda dice el estado en dos o tres palabras; el detalle
            # (cuántas páginas, por qué se paró) queda al posar el puntero.
            estado = TEXTO_INDEXANDO if indexando else parte.titulo
            from app.airvault.flujo import websearch_confirmacion_valida
            confirmado = parte.trabajo.manifiesto.websearch_confirmado if websearch_confirmacion_valida(parte.trabajo.manifiesto) else ""
            if confirmado and parte.titulo != "Confirmado en Web Search":
                estado += ", confirmado en Web Search"
            celdas = (parte.batch_id, nombre, str(esperadas), estado)
            papel = papel_de_estado(
                parte.estado, indexando, revisar=parte.trabajo.manifiesto.solo_subir,
            )
            for columna, texto in enumerate(celdas):
                item = QTableWidgetItem(texto)
                if columna == 1:
                    item.setToolTip(nombre)
                if columna == 2:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter
                    )
                if columna == 3:
                    from app.airvault.flujo import DESCUADRADO, INCOMPLETO, POSIBLE_DUPLICADO, SIN_SUBIR, TOMADO
                    detalle = parte.detalle.strip()
                    if detalle.casefold() == parte.titulo.casefold():
                        detalle = ""
                    elif detalle.casefold().startswith(parte.titulo.casefold() + ";"):
                        detalle = detalle[len(parte.titulo) + 1:].strip()
                    item.setToolTip(
                        mensaje_error(detalle, parte.titulo)
                        if parte.estado in (DESCUADRADO, INCOMPLETO, POSIBLE_DUPLICADO, SIN_SUBIR, TOMADO)
                        else f"{parte.titulo}: {detalle}" if detalle else parte.titulo
                    )
                    if confirmado:
                        item.setToolTip(item.toolTip() + "\nConfirmado en Web Search: " + confirmado
                                        + "\n" + parte.trabajo.manifiesto.websearch_detalle)
                if papel:
                    pintar_celda_del_tema(item, papel)
                tabla.setItem(fila, columna, item)
            tarjeta = tabla.item(fila)
            clave = str(parte.trabajo.carpeta)
            tarjeta.setData(Qt.ItemDataRole.UserRole, clave)
            tarjeta.setSelected(clave in seleccionadas)
            if clave == actual:
                tabla.selectionModel().setCurrentIndex(tabla.model().index(fila, 0),
                                                        QItemSelectionModel.SelectionFlag.NoUpdate)
        ancho_nombre = min(
            max(
                tabla.sizeHintForColumn(1) + 16,
                ANCHO_MINIMO_NOMBRE_BATCH,
            ),
            ANCHO_MAXIMO_NOMBRE_BATCH,
        )
        tabla.setColumnWidth(1, ancho_nombre)
        tabla.verticalScrollBar().setValue(scroll)
        tabla.setUpdatesEnabled(True)
        del bloqueo
        self._actualizar_eliminar_seleccionados()
        # La tabla se acaba de rehacer entera: la bitácora que se estaba
        # buscando perdió su resaltado y hay que devolvérselo.
        self._rehacer_la_busqueda()
        # Cada repintado es un cambio de estado de algún batch, y la barra
        # de la cola sale de esos estados.
        self._pintar_avance()
        self.boton_previa.setEnabled(
            self.hilo() is None and bool(self._estados or self._corrida.strip())
        )
        from app.gui.airvault_previa import VistaPreviaBatches
        for ventana in self._ventanas_de_consulta:
            if isinstance(ventana, VistaPreviaBatches):
                ventana.actualizar(self._previstos_de_la_cola(
                    p for p in ventana._previstos if not p.existe
                ))

    def _listos(self) -> list:
        """Partes que ya se pueden escribir y tienen su plan calculado."""
        planes = self._estado.get("planes") or {}
        return [
            parte.trabajo for parte in self._partes_del_alcance()
            if parte.se_puede_indexar and str(parte.trabajo.carpeta) in planes
        ]

    def _listos_automaticos(self) -> list:
        """Los listos que la cadena automática vuelve a escribir sola.

        El batch incompleto que ya gastó sus comprobaciones
        (`RECONFIRMACIONES_TRAS_INDEXAR`) no se reescribe en cada vuelta del
        reloj: antes cada revisión lo replanificaba toda la tarde por una
        página que necesitaba una mano. Pero tampoco se abandona: vuelve a
        intentarse cada `MINUTOS_ENTRE_REINTENTOS_AMARILLOS`, hasta
        `REINTENTOS_AMARILLOS_ESPACIADOS` veces, porque lo que deja páginas
        amarillas suele ser AirVault tardando en reflejar lo guardado.
        «Indexar» y el menú de la fila lo escriben cuando alguien lo pide.
        """
        from app.airvault.flujo import INCOMPLETO

        agotados = {
            str(parte.trabajo.carpeta) for parte in self._partes_del_alcance()
            if parte.estado == INCOMPLETO
            and self._reconfirmaciones.get(str(parte.trabajo.carpeta)) == 0
            and not self._toca_reintento_espaciado(str(parte.trabajo.carpeta))
        }
        return [
            trabajo for trabajo in self._listos()
            if str(trabajo.carpeta) not in agotados
        ]

    def _toca_reintento_espaciado(self, clave: str) -> bool:
        """Si al batch amarillo ya le toca otro intento espaciado."""
        hechos, ultimo = self._reintentos_espaciados.get(
            clave, (0, time.monotonic())
        )
        return (
            hechos < REINTENTOS_AMARILLOS_ESPACIADOS
            and time.monotonic() - ultimo
            >= MINUTOS_ENTRE_REINTENTOS_AMARILLOS * 60
        )

    def _gastar_reintentos_espaciados(self, trabajos) -> None:
        """Cuenta el intento espaciado de los batches que se van a reescribir."""
        from app.airvault.flujo import INCOMPLETO

        incompletos = {
            str(parte.trabajo.carpeta) for parte in self._partes_del_alcance()
            if parte.estado == INCOMPLETO
        }
        for trabajo in trabajos:
            clave = str(trabajo.carpeta)
            if (
                clave not in incompletos
                or self._reconfirmaciones.get(clave) != 0
                or clave not in self._reintentos_espaciados
            ):
                continue
            hechos, _ultimo = self._reintentos_espaciados[clave]
            self._reintentos_espaciados[clave] = (hechos + 1, time.monotonic())

    def _anotar_intentos(self, amarillas_por_batch) -> None:
        """Lo que dejó cada intento de indexado, para decidir si reintentar.

        Un intento que deja menos páginas sin confirmar que el anterior
        devuelve las comprobaciones enteras y reinicia los reintentos
        espaciados: reescribir está sirviendo. El batch confirmado sale de
        todas las cuentas.
        """
        for clave, faltan in (amarillas_por_batch or {}).items():
            clave = str(clave)
            previas = self._amarillas.get(clave)
            if not faltan:
                for cuenta in (
                    self._amarillas, self._reconfirmaciones,
                    self._reintentos_espaciados,
                ):
                    cuenta.pop(clave, None)
                continue
            self._amarillas[clave] = faltan
            if previas is not None and faltan < previas:
                self._reconfirmaciones[clave] = RECONFIRMACIONES_TRAS_INDEXAR
                self._reintentos_espaciados.pop(clave, None)

    def _por_completar(self, automatico: bool = False) -> list:
        """Batches verificados que pueden cerrarse sin volver a escribir."""
        from app.airvault.flujo import INDEXADO

        partes = self._partes_del_proceso() if automatico else self._partes_del_alcance()
        ahora = time.monotonic()
        return [
            parte.trabajo for parte in partes
            if parte.estado == INDEXADO and not parte.trabajo.manifiesto.solo_subir
            and (not automatico or (
                self._cierres_fallidos.get(str(parte.trabajo.carpeta), (0, 0))[0] < 3
                and ahora - self._cierres_fallidos.get(str(parte.trabajo.carpeta), (0, 0))[1]
                >= self.minutos_spin.value() * 60
            ))
        ]

    def _anotar_cierres(self, datos: dict) -> None:
        for trabajo, resultado in datos.get("cierres") or []:
            clave = str(trabajo.carpeta)
            if resultado.completado:
                self._cierres_fallidos.pop(clave, None)
                continue
            intentos = self._cierres_fallidos.get(clave, (0, 0))[0] + 1
            self._cierres_fallidos[clave] = (intentos, time.monotonic())
            if intentos >= 3:
                self._anotar(f"Batch «{trabajo.manifiesto.nombre_batch}»: AirVault no confirmó el cierre tras tres intentos. "
                             "Use Completar desde sus acciones para volver a intentarlo.")

    def _ejecucion(self) -> list:
        """Todas las partes de la ejecución, tal como están en la tabla.

        Dar una carga por perdida depende de las demás: son las partes
        siguientes, ya indexadas, las que demuestran que AirVault pasó de
        largo. Ninguna regla puede decidirlo mirando una sola fila.
        """
        return [parte.trabajo for parte in self._partes_del_alcance()]

    def _falta_esperar(self) -> bool:
        """Si queda algún batch que AirVault todavía no ha terminado.

        Mientras una parte no esté terminada ni lista para escribir, se
        sigue preguntando. También cuando ya se dio la carga por perdida:
        no se vuelve a enviar sola, pero sí se sigue buscando. AirVault
        publica cargas horas después de aceptarlas, y pararse ahí dejaba el
        batch en la tabla esperando a que alguien volviera a pulsar;
        preguntar no escribe nada, y es lo que hace que la carga aparezca
        sola y se indexe sola cuando por fin sale de la cola.

        Una alerta de duplicados solo bloquea cuando está activada la
        preferencia de detener la subida.

        Un indexado incompleto sigue bajo vigilancia aunque agote las
        relecturas inmediatas. Un indexado confirmado también sigue si
        falta completar el batch y esa opción está activada.

        Un batch listo para escribir también, si el indexado va solo: lo
        escribe la cadena, y si esa escritura se corta (la sesión, la red,
        un fallo de AirVault) es el reloj quien la retoma. Antes el reloj se
        paraba al ver todo listo, y un indexado cortado dejaba la ejecución
        quieta hasta que alguien volviera a pulsar.
        """
        from app.airvault.flujo import INDEXADO, INCOMPLETO, POSIBLE_DUPLICADO

        if self._firma_de_fin() is not None:
            return False
        indexa_solo = bool(self._opciones.indexar)
        return any(
            (not parte.se_acabo
             or (parte.estado == INDEXADO and self.completar_check.isChecked()
                 and not parte.trabajo.manifiesto.solo_subir))
            and parte.estado != POSIBLE_DUPLICADO
            and not (parte.estado == INDEXADO
                     and self._cierres_fallidos.get(str(parte.trabajo.carpeta), (0, 0))[0] >= 3)
            and (
                not parte.se_puede_indexar
                or indexa_solo
                or parte.estado == INCOMPLETO
                or self._reconfirmaciones.get(str(parte.trabajo.carpeta), 0) > 0
            )
            for parte in self._partes_del_proceso()
        )

    def _subidas_perdidas(self) -> list:
        """Cargas que AirVault aceptó y ya se pueden dar por no publicadas.

        O bien las partes siguientes ya se indexaron, o bien el archivo
        lleva subido más tiempo del que AirVault tarda en publicar.
        """
        from app.airvault.flujo import subida_perdida

        ejecucion = self._ejecucion()
        return [
            parte for parte in self._partes_del_alcance()
            if subida_perdida(parte, ejecucion)
        ]

    def _sin_subir_todavia(self) -> list:
        """Batches que la comprobación va a mandar sola.

        Solo los que nunca llegaron a Quick Upload. Una carga que AirVault
        aceptó y no publicó no vuelve a salir sola por mucho que tarde: se
        avisa y la manda quien mire Web Index.

        Los manda la revisión periódica y también la cadena que alguien
        pidió con un botón, aunque la periódica esté apagada: «Revisar en
        AirVault» termina lo pendiente, subida incluida.
        """
        from app.airvault.flujo import partes_por_subir

        if not self.auto_check.isChecked() and not self._cadena_manual:
            return []
        return [
            trabajo for trabajo in partes_por_subir(self._partes_del_alcance())
            if str(trabajo.carpeta) not in self._subidas_del_ciclo
        ]

    @staticmethod
    def _aviso_de_cargas_fallidas(fallos) -> str:
        """Dice cuáles no salieron, para no cantar victoria por todas."""
        if not fallos:
            return ""
        nombres = ", ".join(nombre for nombre, _detalle in fallos)
        cuantos = (
            "1 batch no se subió" if len(fallos) == 1
            else f"{len(fallos)} batches no se subieron"
        )
        return f" {cuantos}: {nombres}. El motivo está en la bitácora."

    def _aviso_para_subir_a_mano(self) -> str:
        """Recuerda que la espera se puede saltar cuando no hay batch.

        La espera es una suposición del programa; quien tiene Web Index
        delante sabe más que él. Si ya miró y el batch no está, no tiene por
        qué esperar a que venza ningún reloj.
        """
        if not any(parte.se_puede_subir for parte in self._partes_del_alcance()):
            return ""
        return (
            " Si el batch no está en AirVault, clic derecho en su fila y "
            "«Subir a AirVault ahora»."
        )

    def _aviso_para_volver_a_subir(self) -> str:
        """Dice qué cargas AirVault no publicó y deja la decisión a quien mire.

        El programa no vuelve a mandar ninguna solo: no puede distinguir una
        carga perdida de una cola lenta, y por ese margen es por donde
        aparecen dos copias del mismo batch. Seguir buscándolas sí lo hace
        solo, que mirar la cola no escribe nada.
        """
        from app.airvault.flujo import (busqueda_amplia_sin_hallar,
                                        espera_para_darla_por_perdida,
                                        subida_rebasada)

        perdidas = self._subidas_perdidas()
        if not perdidas:
            return ""
        ejecucion = self._ejecucion()
        nombres = ", ".join(parte.nombre for parte in perdidas)
        minutos = max(
            1,
            round(
                max(
                    espera_para_darla_por_perdida(parte.trabajo)
                    for parte in perdidas
                ) / 60
            ),
        )
        sin_hallar = [
            parte for parte in perdidas
            if busqueda_amplia_sin_hallar(parte.trabajo)
        ]
        rebasadas = [
            parte for parte in perdidas
            if parte not in sin_hallar
            and subida_rebasada(parte.trabajo, ejecucion)
        ]
        if len(sin_hallar) == len(perdidas):
            razon = "No aparecen en la cola con ningún nombre."
        elif len(rebasadas) == len(perdidas):
            razon = "Las partes siguientes ya están indexadas."
        elif sin_hallar or rebasadas:
            razon = (
                "Unos no aparecen en la cola y en otros ya pasó la espera "
                f"de {minutos} minutos."
            )
        else:
            razon = (
                f"Pasó la espera de {minutos} minutos y es probable que la "
                "carga no vaya a aparecer."
            )
        varios = len(perdidas) > 1
        # Reenviar solo es como acaban dos copias del mismo batch en la
        # cola, así que eso lo decide quien mira Web Index; mirar la cola sí
        # se sigue haciendo solo, que no escribe nada.
        no_se_mandan = (
            "No se vuelven a mandar solos" if varios
            else "No se vuelve a mandar solo"
        )
        sigue = (
            "; se sigue mirando la cola por si aparece" + ("n" if varios else "")
            if self.auto_check.isChecked() else ""
        )
        return (
            f" AirVault no publicó: {nombres}. {razon} {no_se_mandan}"
            f"{sigue}. Si no está en Web Index, clic derecho en su fila y "
            "«Subir a AirVault ahora»."
        )

    # ── la comprobación periódica ──────────────────────────────────

    def _ajustar_vigilancia(self) -> None:
        """Arranca o para la comprobación automática según haga falta.

        Se pregunta mientras quede algo que esperar. Cuando todos los batches
        están listos (o ya indexados, o REVISAR está listo para escribir lo
        disponible) no hay nada que AirVault vaya a cambiar solo, así que
        se deja de preguntar en vez
        de golpear el servidor toda la tarde.
        """
        self._ajustar_confirmacion()
        if not self.auto_check.isChecked() or not self._falta_esperar():
            self._parar_vigilancia()
            return
        if self._vigilante is None:
            self._vigilante = QTimer(self)
            self._vigilante.timeout.connect(self._comprobar_solo)
        self._vigilante.setInterval(self._minutos_de_vigilancia() * 60_000)
        self._vigilante.start()
        self._actualizar_latido()

    def _por_confirmar_websearch(self) -> list:
        from app.airvault.flujo import websearch_confirmacion_valida
        from app.airvault.confirmacion import muestra_de_batch
        return sorted([
            t for t in self._trabajos if not t.manifiesto.cancelado
            and not websearch_confirmacion_valida(t.manifiesto)
            and muestra_de_batch(t.manifiesto)
        ], key=lambda t: (getattr(t.manifiesto, "websearch_revision", ""), str(t.carpeta)))

    def _firma_consulta_websearch(self, trabajo):
        from app.airvault.confirmacion import huella_de_numeros
        subida = trabajo.manifiesto.etapas.get("subir")
        cierre = trabajo.manifiesto.etapas.get("completar")
        return (huella_de_numeros(trabajo.manifiesto),
                (subida.estado.value, subida.actualizada) if subida else None,
                (cierre.estado.value, cierre.actualizada) if cierre else None)

    def _subidos_por_confirmar_websearch(self) -> list:
        from app.airvault.flujo import _subida_rastreable
        return [
            t for t in self._por_confirmar_websearch()
            if not t.manifiesto.etapa_hecha("completar")
            and _subida_rastreable(t, bool(t.manifiesto.batch_id))
        ]

    def _pendientes_websearch_automaticos(self) -> list:
        """Conserva el alcance inicial aunque se suban nuevos batches."""
        if self._firma_de_fin() is not None:
            return []
        pendientes = []
        for trabajo in self._subidos_por_confirmar_websearch():
            clave = str(trabajo.carpeta)
            if self._websearch_inicio is not None and clave not in self._websearch_inicio:
                continue
            if self._websearch_revisados.get(clave) == self._firma_consulta_websearch(trabajo):
                continue
            if self.auto_check.isChecked() or clave in self._websearch_inicio_por_revisar:
                pendientes.append(trabajo)
        return pendientes

    def _ajustar_confirmacion(self) -> None:
        if not hasattr(self, "auto_check") or self._deteniendo:
            return
        pendientes = self._pendientes_websearch_automaticos()
        sesion = self._estado.get("sesion") or self._estado_websearch.get("sesion")
        inicial = any(str(t.carpeta) in self._websearch_inicio_por_revisar for t in pendientes)
        if not pendientes or (sesion is None and not inicial):
            if self._confirmador is not None:
                self._confirmador.stop()
            return
        if self._confirmador is None:
            self._confirmador = QTimer(self)
            self._confirmador.setSingleShot(True)
            self._confirmador.timeout.connect(self._confirmar_solo)
        if not self._confirmador.isActive():
            self._confirmador.start(0)

    def _confirmar_solo(self) -> None:
        if self._deteniendo:
            return
        trabajos = self._pendientes_websearch_automaticos()
        if not trabajos:
            self._ajustar_confirmacion()
            return
        self._iniciar_confirmacion(trabajos, automatico=True)

    def _minutos_de_vigilancia(self) -> int:
        """Cada cuánto pregunta el reloj: más espaciado tras varios fallos."""
        minutos = self.minutos_spin.value()
        if self._fallos_seguidos >= FALLOS_SEGUIDOS_ANTES_DE_ESPACIAR:
            return max(minutos, MINUTOS_TRAS_FALLOS)
        return minutos

    def _parar_vigilancia(self) -> None:
        if self._vigilante is not None:
            self._vigilante.stop()
        self._actualizar_latido()

    def _programar_reconfirmaciones(self, carpetas) -> None:
        """Da comprobaciones extra a los batches que acabaron sin confirmar."""
        for clave in carpetas or ():
            # ``setdefault`` y no asignación: el batch que ya las gastó no
            # las recupera al volver a quedar incompleto. Es lo que corta el
            # ciclo comprobar, reindexar y comprobar otra vez.
            self._reconfirmaciones.setdefault(
                str(clave), RECONFIRMACIONES_TRAS_INDEXAR
            )

    def _descontar_reconfirmaciones(self, revisados) -> None:
        """Gasta una comprobación de cada batch que sigue sin confirmar.

        El que ya no está incompleto sale de la cuenta: quedó indexado,
        completado o alguien reinició su paso.
        """
        from app.airvault.flujo import INCOMPLETO

        for parte in revisados:
            clave = str(parte.trabajo.carpeta)
            if parte.estado != INCOMPLETO:
                self._reintentos_espaciados.pop(clave, None)
                self._amarillas.pop(clave, None)
            if clave not in self._reconfirmaciones:
                continue
            if parte.estado == INCOMPLETO:
                self._reconfirmaciones[clave] = max(
                    0, self._reconfirmaciones[clave] - 1
                )
                if self._reconfirmaciones[clave] == 0:
                    # Desde aquí corren los reintentos espaciados.
                    self._reintentos_espaciados.setdefault(
                        clave, (0, time.monotonic())
                    )
            else:
                del self._reconfirmaciones[clave]

    def _comprobar_solo(self) -> None:
        """Lo que dispara el reloj. Se salta el turno si hay algo en vuelo."""
        if self.hilo() is not None:
            return
        if not self._resultado_fallido and self._firma_de_fin() is not None:
            self._fin_pendiente = True
            self._anunciar_fin()
            return
        self._cadena_manual = False
        # Cada vuelta del reloj vuelve a dar permiso de subida: lo que no
        # se pudo subir hace cinco minutos se intenta otra vez ahora.
        self._subidas_del_ciclo.clear()
        self._estado["no_planificar"] = self._amarillos_en_espera()
        self._comprobar()

    def _amarillos_en_espera(self) -> set[str]:
        """Batches amarillos que esta vuelta del reloj no va a reescribir.

        Gastaron sus comprobaciones y aún no les toca el reintento
        espaciado: se vigilan con el mapa del batch, sin planificarlos.
        """
        from app.airvault.flujo import INCOMPLETO

        return {
            str(parte.trabajo.carpeta) for parte in self._partes_del_alcance()
            if parte.estado == INCOMPLETO
            and self._reconfirmaciones.get(str(parte.trabajo.carpeta)) == 0
            and not self._toca_reintento_espaciado(str(parte.trabajo.carpeta))
        }

    # ── acciones ───────────────────────────────────────────────────

    def _config_actual(self):
        return self._config

    def _guardar_politica_duplicados(self, marcada: bool) -> None:
        from app.airvault.config import guardar_politica_duplicados
        from app.airvault.flujo import POSIBLE_DUPLICADO, estado_local
        porcentaje = self.porcentaje_duplicados_spin.value()
        porcentaje_anterior = self._config.porcentaje_duplicados
        if not guardar_politica_duplicados(self._raiz / AIRVAULT_FILENAME, marcada, porcentaje):
            with QSignalBlocker(self.detener_duplicados_check):
                self.detener_duplicados_check.setChecked(self._config.detener_por_duplicados)
            with QSignalBlocker(self.porcentaje_duplicados_spin):
                self.porcentaje_duplicados_spin.setValue(self._config.porcentaje_duplicados)
            self.resumen.setText("No se pudo guardar la opción de duplicados.")
            return
        self._config = self._config.with_overrides(detener_por_duplicados=marcada, porcentaje_duplicados=porcentaje)
        self._estado["config"] = self._config
        trabajos = list(self._trabajos) + [p.trabajo for p in self._estados]
        for trabajo in trabajos:
            trabajo.config = trabajo.config.with_overrides(detener_por_duplicados=marcada, porcentaje_duplicados=porcentaje)
            if (porcentaje != porcentaje_anterior
                    and not getattr(trabajo.manifiesto, "duplicado_exacto", False)):
                from app.airvault.flujo import limpiar_posible_duplicado
                limpiar_posible_duplicado(trabajo)
        self._estados = [estado_local(p.trabajo) if p.estado == POSIBLE_DUPLICADO else p for p in self._estados]
        self._pintar_lotes()
        self._ajustar_vigilancia()

    def _al_cambiar_completar(self, marcado: bool) -> None:
        """Aplica la opción también al trabajo que ya está en curso."""
        self._estado["completar"] = bool(marcado)
        self._opciones.fijar(COMPLETAR, marcado)
        # Con o sin cierre cambia dónde acaba el recorrido de cada batch.
        self._pintar_avance()

    def _guardar_limite_batch(self, cantidad: int) -> None:
        """Recuerda el valor en la propia carpeta portable."""
        if guardar_paginas_por_batch(
            self._raiz / AIRVAULT_FILENAME, cantidad
        ):
            self._config = self._config.with_overrides(
                paginas_por_batch=int(cantidad)
            )
        # La columna «Entrega» cuenta los batches con este máximo, así que
        # cambiarlo la deja diciendo un reparto que ya no es el que se va a
        # subir. Se rehace la lista con el número nuevo.
        self._refrescar_historial()

    def _base_del_estado(self) -> Optional[dict]:
        """Los datos comunes del trabajo, o ``None`` si falta algo."""
        from app.airvault.flujo import carpeta_de_corrida, carpeta_de_trabajo

        csv = self._corrida.strip()
        if not csv:
            self.resumen.setText("Falta elegir la ejecución.")
            return None
        if not self.lote_edit.text().strip():
            self.resumen.setText("Falta el nombre del batch.")
            return None
        job = carpeta_de_corrida(csv).name
        self._estado.update({
            "config": self._config_actual(),
            "csv": csv,
            "raiz": self._raiz,
            "carpeta_job": self._raiz / carpeta_de_trabajo(job),
            "nombre_lote": self.lote_edit.text().strip(),
            "cookie": self.cookie_edit.text(),
            "paginas_por_batch": self.limite_batch_spin.value(),
            "fin_de_mes": self.fin_de_mes(),
            "indexar_al_encontrar": self._opciones.indexar,
            "completar": self.completar_check.isChecked(),
            # Solo lo pone «Subir a AirVault», y para una sola acción. Todo
            # lo que corre solo (la cadena automática, el reloj de
            # comprobación, la reanudación) se queda en la ejecución que
            # está elegida y no toca las de otros días.
            "recuperar_pendientes": self._recuperar_pendientes,
        })
        self._recuperar_pendientes = False
        self._estado["trabajos"] = self._filtrar_trabajos(self._trabajos)
        if self.solo_ejecucion_check.isChecked():
            self._estado["recuperar_pendientes"] = False
        return self._estado

    def _subir_a_mano(self) -> None:
        """Lo que hace el botón, que es más que lo que hace la cadena.

        Pulsarlo significa «ponte al día con lo que haya pendiente», así
        que además de esta ejecución retoma los batches que quedaron a
        medias en ejecuciones anteriores. La cadena automática no hace eso:
        lo que arranca solo se ciñe a la ejecución elegida, porque nadie
        está mirando y mandar a AirVault batches de otro día sin pedirlo es
        justo como se acaban subiendo dos veces.
        """
        if self.hilo() is not None:
            return
        elegidas = self._corridas_seleccionadas()
        if len(elegidas) > 1:
            from app.airvault.flujo import carpeta_de_corrida, comprobar_entrega

            # La entrega puede haber cambiado desde que se abrio la lista.
            # Se valida la seleccion entera antes de mandar ningun archivo.
            try:
                for csv in elegidas:
                    comprobar_entrega(Path(csv))
            except Exception as exc:  # noqa: BLE001 - se explica en la ventana
                self.resumen.setText(
                    f"{carpeta_de_corrida(csv).name}: "
                    + mensaje_error(exc, "Vuelva a exportar la ejecución antes de subirla.")
                )
                return
            self._cadena_manual = True
            self._subir(varias=True)
            return
        sospechosos = [
            parte for parte in self._partes_en_cola()
            if parte.trabajo.manifiesto.posible_duplicado
        ]
        if sospechosos:
            self._quitar_sospecha(sospechosos)
            return
        self._recuperar_pendientes = True
        self._cadena_manual = True
        self._subir()

    def _subir(self, varias: bool = False) -> None:
        estado = self._base_del_estado()
        if estado is None:
            return
        if varias:
            from app.airvault.flujo import carpeta_de_corrida, carpeta_de_trabajo
            from app.airvault.naming import nombre_desde_corrida

            estado["recuperar_pendientes"] = False
            estado["ejecuciones_subida"] = [
                {
                    "csv": csv,
                    "carpeta_job": self._raiz / carpeta_de_trabajo(carpeta_de_corrida(csv).name),
                    "nombre_lote": self.lote_edit.text().strip() if csv == self._corrida
                    else nombre_desde_corrida(csv),
                    "fin_de_mes": self._fecha_de_ejecucion(csv),
                }
                for csv in self._corridas_seleccionadas()
            ]
        self._subidas_del_ciclo.clear()
        self._lanzar("subir", estado)

    def _comprobar(self) -> None:
        estado = self._base_del_estado()
        if estado is None:
            return
        estado.pop("comprobar_trabajos", None)
        self._lanzar("comprobar", estado)

    def _revisar_a_mano(self) -> None:
        """«Revisar en AirVault»: pregunta y termina todo lo que falte.

        La revisión encadena lo que haga falta según lo que encuentre:
        sube los que nunca salieron, indexa los confirmados y, con
        «Completar batch», los completa. Antes solo subía si además estaba
        marcada la revisión periódica, así que con ella apagada el botón
        miraba la cola y no terminaba nada.
        """
        self._cadena_manual = True
        self._subidas_del_ciclo.clear()
        self._comprobar()

    def _indexar(self, automatico: bool = False) -> None:
        self._estado.pop("indexar_acotado", None)
        self._estado.pop("completar_acotado", None)
        self._estado["indexar_manual"] = not automatico
        if not automatico:
            for trabajo in self._por_completar():
                self._cierres_fallidos.pop(str(trabajo.carpeta), None)
        listos = self._listos_automaticos() if automatico else self._listos()
        if not listos:
            # Cerrar lo verificado va antes que reintentar subidas: si no,
            # una carga que no sale dejaba sin completar a los batches ya
            # indexados, reintentándola una y otra vez.
            por_completar = self._por_completar(automatico)
            if self.completar_check.isChecked() and por_completar:
                self._estado["por_completar"] = por_completar
                self._lanzar("completar", self._estado)
                return
            # Un batch confirmado se indexa ya, aunque otras partes sigan sin
            # subir: Subida > Indexado va batch por batch. Antes se exigían
            # todas las cargas terminadas, y una sola subida atascada dejaba
            # la ejecución entera sin indexar. Sin nada que escribir, lo que
            # toca es subir lo que falta.
            if any(
                not trabajo.manifiesto.etapa_hecha("subir")
                for trabajo in self._filtrar_trabajos(self._trabajos)
            ):
                self._continuar_pendiente()
                return
            if self._corrida.strip():
                # Conecta y detecta tambien batches que esta aplicacion subio
                # en ejecuciones anteriores. Despues decide si hay que
                # reindexar, continuar o solamente comprobar su estado.
                self._indexar_al_terminar = True
                self._comprobar()
            return
        if automatico:
            self._gastar_reintentos_espaciados(listos)
        self._estado["listos"] = listos
        self._estado["completar"] = self.completar_check.isChecked()
        self._estado["completar_tambien"] = (
            self._por_completar(automatico) if self.completar_check.isChecked() else []
        )
        self._lanzar("indexar", self._estado)

    def _continuar_pendiente(self) -> None:
        """Retoma el primer paso necesario sin duplicar trabajo terminado."""
        from app.airvault.model import EstadoEtapa

        self._subidas_del_ciclo.clear()
        self._cadena_manual = True
        estado = self._base_del_estado()
        if estado is None:
            return
        if not self._filtrar_trabajos(self._trabajos):
            self._indexar_al_terminar = self._opciones.indexar
            self._comprobar()
            return
        pendientes_subida = [
            trabajo for trabajo in self._filtrar_trabajos(self._trabajos)
            if not trabajo.manifiesto.etapa_hecha("subir")
        ]
        if pendientes_subida:
            # EN_CURSO puede significar que AirVault acepto el archivo y la
            # respuesta se perdio. Primero se consulta; solo «Reiniciar»
            # autoriza una nueva carga cuando esa duda existe.
            if any(
                trabajo.manifiesto.etapas.get("subir") is not None
                and trabajo.manifiesto.etapa("subir").estado
                is EstadoEtapa.EN_CURSO
                for trabajo in pendientes_subida
            ):
                self._indexar_al_terminar = True
                self._comprobar()
                return
            if not self._preguntar_por_amarillas(pendientes_subida):
                return
            estado["pendientes_subida"] = pendientes_subida
            self._lanzar("subir_pendientes", estado)
            return
        if self._listos():
            self._indexar()
            return
        self._indexar_al_terminar = True
        self._comprobar()

    def _reiniciar_incompleto(self) -> None:
        """Reabre el paso local incompleto del batch elegido o de todos."""
        from app.airvault.flujo import (estado_local,
                                        reiniciar_trabajos_incompletos)

        filas = self.lotes.selectionModel().selectedRows()
        objetivos = (
            [self._partes_en_cola()[filas[0].row()].trabajo]
            if filas and filas[0].row() < len(self._partes_en_cola())
            else self._filtrar_trabajos(self._trabajos)
        )
        reiniciados = reiniciar_trabajos_incompletos(objetivos)
        if not reiniciados:
            self.resumen.setText("No hay ningún paso incompleto que reiniciar.")
            return
        claves = {str(trabajo.carpeta) for trabajo, _paso in reiniciados}
        planes = self._estado.get("planes") or {}
        self._estado["planes"] = {
            clave: plan for clave, plan in planes.items() if clave not in claves
        }
        self._estados = [estado_local(t) for t in self._trabajos]
        self._pintar_lotes()
        pasos = ", ".join(sorted({paso for _trabajo, paso in reiniciados}))
        self.resumen.setText(
            f"Se reinició el paso incompleto ({pasos}) en "
            f"{len(reiniciados)} batch"
            + ("es." if len(reiniciados) != 1 else ".")
        )
        self.boton_indexar.setEnabled(True)

    def _lanzar(self, modo: str, estado: dict) -> None:
        if modo == "buscar_websearch":
            self._iniciar_confirmacion(estado.get("buscar_trabajos", []),
                                      automatico=bool(estado.get("confirmacion_automatica")))
            return
        if self._worker is not None and self._worker.isRunning():
            return
        if not self._websearch_proceso_activo:
            self._websearch_inicio = {
                str(t.carpeta) for t in self._subidos_por_confirmar_websearch()
            }
            self._websearch_inicio_por_revisar = set(self._websearch_inicio)
            self._websearch_revisados.clear()
            self._websearch_proceso_activo = True
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if modo != "buscar_websearch" and (self._alcance_proceso is None or self._fin_confirmado is not None):
            self._alcance_proceso = {
                str(t.carpeta) for t in estado.get("trabajos", self._trabajos)
                if not t.manifiesto.etapa_hecha("completar")
            } or None
        if not (modo == "buscar_websearch" and estado.get("confirmacion_automatica")):
            self._fin_pendiente = False
        self._resultado_fallido = False
        self._worker_filtrado = self.solo_ejecucion_check.isChecked()
        if self._worker_filtrado:
            estado["recuperar_pendientes"] = False
            for clave in (
                "trabajos", "listos", "por_completar", "pendientes_subida",
                "comprobar_trabajos",
            ):
                if clave in estado:
                    estado[clave] = self._filtrar_trabajos(estado[clave])
        if modo != "resubir":
            # El estado se reusa de una acción a la siguiente. Sin borrar
            # esto, una orden expresa dejaría a la reanudación automática
            # subiendo sin comprobar nada.
            estado["forzados"] = []
        if modo in ("subir", "subir_pendientes", "resubir"):
            estado["indexar_manual"] = False
        sesion = estado.get("sesion")
        if sesion is not None and sesion.cancelada:
            # La sesión quedó cortada por la cancelación anterior. Lo que se
            # lanza ahora es una orden nueva, así que vuelve a valer; sin
            # esto, la primera petición se negaría sola.
            sesion.reanudar()
        self._habilitar(False)
        worker = TrabajoAirVaultWorker(modo, estado, self)
        worker.paso.connect(self._mostrar_paso)
        worker.subidas_actualizadas.connect(self._al_actualizar_subidas)
        worker.batch_encontrado.connect(self._al_batch_encontrado)
        worker.batch_indexado.connect(self._al_batch_indexado)
        worker.batch_indexando.connect(self._al_batch_indexando)
        worker.subido.connect(self._al_subir)
        worker.comprobado.connect(self._al_comprobar)
        worker.buscado.connect(self._al_buscar_websearch)
        worker.indexado.connect(self._al_indexar)
        worker.fallo.connect(self._al_fallar)
        worker.cancelado.connect(self._al_cancelar)
        worker.finished.connect(self._al_terminar)
        self._worker = worker
        self._cuenta_paso = (0, 0)
        self._arrancar_reloj()
        worker.start()
        self._ajustar_confirmacion()
        self._pintar_avance()
        self._actualizar_latido()
        # Con el hilo ya en marcha, para que la línea de pasos de la ventana
        # principal pase a «en curso» al empezar y no al terminar.
        self._publicar_avance()

    def _habilitar(self, activo: bool) -> None:
        # Estas tres opciones se aplican al trabajo en curso y deben poder
        # cambiarse mientras los controles que lanzarían otra acción quedan
        # bloqueados.
        self.detener_duplicados_check.setEnabled(True)
        self.completar_check.setEnabled(True)
        self.solo_ejecucion_check.setEnabled(True)
        # La ejecución de esta ventana no cambia mientras trabaja, pero el
        # historial sigue disponible: elegir otra emite una solicitud para
        # abrirla en su propia ventana y su propio hilo.
        self.historial.setEnabled(True)
        self.historial.permitir_marcar = activo
        self.boton_subir.setEnabled(activo and self._listo_para_subir)
        self.boton_eliminar_registro.setEnabled(
            activo and bool(self._registros_presentes())
        )
        varias = len(self._corridas_marcadas) > 1
        self.lote_edit.setEnabled(activo and not varias)
        self.cookie_edit.setEnabled(activo)
        self.limite_batch_spin.setEnabled(activo)
        self.fecha_combo.setEnabled(activo and not varias)
        self.boton_comprobar.setEnabled(activo and bool(self._trabajos))
        # La vista previa solo lee el disco, pero mientras el hilo reparte
        # los manifiestos están a medio escribir y enseñarlos engaña.
        self.boton_previa.setEnabled(
            activo and bool(self._estados or self._corrida.strip())
        )
        self.boton_automatizacion.setEnabled(True)
        self.boton_continuar.setEnabled(activo)
        self.boton_reiniciar.setEnabled(activo and bool(self._trabajos))
        # Cerrar y Cancelar nunca se apagan a la vez: mientras hay trabajo
        # en vuelo tiene que haber siempre algo que pulsar, o la ventana se
        # queda muda durante una espera de minutos.
        self.boton_cancelar.setEnabled(not activo or self.lectura_websearch() is not None)
        self.boton_indexar.setEnabled(
            activo and (
                bool(self._listos()) or bool(self._trabajos)
                or (
                    self.completar_check.isChecked()
                    and bool(self._por_completar())
                )
                or bool(self._corrida.strip())
            )
        )

    # ── el reloj del paso ──────────────────────────────────────────

    def _arrancar_reloj(self) -> None:
        """Deja a la vista que el trabajo sigue vivo mientras espera.

        La barra sola no basta: en las etapas sin cuenta (entrar a
        AirVault, esperar a que el batch salga de la cola) no se mueve, y
        una espera de diez minutos se lee como un cuelgue.
        """
        self._inicio_paso = time.monotonic()
        if self._reloj is None:
            self._reloj = QTimer(self)
            self._reloj.setInterval(1000)
            self._reloj.timeout.connect(self._marcar_reloj)
        self._marcar_reloj()
        self._reloj.start()

    def _parar_reloj(self) -> None:
        if self._reloj is not None:
            self._reloj.stop()
        self.reloj_label.setText("")

    def _marcar_reloj(self) -> None:
        self.reloj_label.setText(_minutos(time.monotonic() - self._inicio_paso))

    # ── el avance de toda la cola ──────────────────────────────────

    def _seguir_en_vuelo(self, texto: str, hechas: int, total: int) -> None:
        """Anota cuánto lleva el batch que está subiendo o escribiendo.

        El aviso no dice de qué batch es más que por su prefijo («Batch
        003SRO: »), el mismo con el que el recorrido lo escribe. Solo cuentan
        la subida de un batch que aún no está en AirVault y la escritura del
        que se está indexando: el resto de los pasos con cuenta (leer el
        batch, revisar lo guardado) no acercan el final.
        """
        if total <= 0:
            return
        from app.airvault.flujo import POSIBLE_DUPLICADO, SIN_SUBIR, _prefijo

        for parte in self._partes_del_proceso():
            if not texto.startswith(_prefijo(parte.trabajo)):
                continue
            clave = str(parte.trabajo.carpeta)
            if clave in self._indexando:
                etapa = "indexar"
            elif parte.estado in (SIN_SUBIR, POSIBLE_DUPLICADO):
                etapa = "subir"
            else:
                continue
            fraccion = min(1.0, max(0.0, hechas / total))
            previa = self._en_vuelo.get(clave)
            if previa is not None and previa[0] == etapa:
                # Nunca hacia atrás: un reintento vuelve a contar desde cero
                # lo que la barra ya había dado por hecho.
                fraccion = max(fraccion, previa[1])
            self._en_vuelo[clave] = (etapa, fraccion)
            return

    def _avance_global(self) -> Optional[float]:
        """Cuánto lleva la cola entera, de 0 a 1; ``None`` sin batches.

        Cada batch cuenta lo mismo, y su parte sale de su estado más lo que
        lleve el paso en curso. La meta es completarlo si «Completar batch»
        está marcado y dejarlo indexado si no; REVISAR nunca se completa.
        """
        from app.airvault.flujo import CANCELADO

        partes = [
            parte for parte in self._partes_del_proceso()
            if parte.estado != CANCELADO
        ]
        if not partes:
            return None
        completar = self.completar_check.isChecked()
        indexar = completar or self._opciones.indexar or self._estado.get("indexar_manual", False)
        suma = 0.0
        peso_total = 0
        for parte in partes:
            avance = avance_de_estado(parte.estado)
            etapa, fraccion = self._en_vuelo.get(
                str(parte.trabajo.carpeta), ("", 0.0)
            )
            if etapa == "subir":
                avance = max(avance, AVANCE_SUBIDO * fraccion)
            elif etapa == "indexar":
                avance = max(
                    avance,
                    AVANCE_LISTO + (AVANCE_ESCRITO - AVANCE_LISTO) * fraccion,
                )
            meta = (
                1.0 if completar and not parte.trabajo.manifiesto.solo_subir
                else AVANCE_INDEXADO if indexar else AVANCE_LISTO
            )
            clave = (str(parte.trabajo.carpeta), completar)
            avance = max(avance, self._avance_por_batch.get(clave, 0.0))
            self._avance_por_batch[clave] = avance
            peso = max(1, sum(not getattr(r, "es_separador", False)
                              for r in parte.trabajo.manifiesto.registros))
            suma += min(1.0, avance / meta) * peso
            peso_total += peso
        return suma / peso_total

    def _partes_del_proceso(self) -> list:
        """El filtro visual no cambia la meta de un trabajo que ya comenzo."""
        if self._alcance_proceso is None:
            return self._partes_del_alcance()
        return [p for p in self._estados if str(p.trabajo.carpeta) in self._alcance_proceso]

    def _firma_de_fin(self) -> Optional[tuple]:
        """Identifica la cola solo si todos sus batches alcanzaron la meta."""
        from app.airvault.flujo import (AUTOCOMPLETADO, CANCELADO,
                                        COMPLETADO, INDEXADO, LISTO, PUBLICADO, SOLO_REVISAR)

        partes = self._partes_del_proceso()
        activos = [p for p in partes if p.estado != CANCELADO]
        if not activos:
            return None
        completar = self.completar_check.isChecked()
        indexar = completar or self._opciones.indexar or self._estado.get("indexar_manual", False)
        for parte in activos:
            if parte.estado in (COMPLETADO, AUTOCOMPLETADO, PUBLICADO):
                continue
            if not indexar and parte.estado in (LISTO, SOLO_REVISAR):
                continue
            if parte.estado == INDEXADO and (
                not completar or parte.trabajo.manifiesto.solo_subir
            ):
                continue
            return None
        return (completar, indexar, tuple(sorted(
            (str(p.trabajo.carpeta), p.batch_id, p.estado,
             len(p.trabajo.manifiesto.registros),
             p.trabajo.manifiesto.solo_subir)
            for p in partes
        )))

    def _pintar_avance(self) -> None:
        """Deja la barra en lo que lleva la cola entera."""
        if not hasattr(self, "completar_check"):
            # Todavía construyéndose: la meta depende de esa casilla.
            return
        avance = self._avance_global()
        firma = self._firma_de_fin()
        if firma is None:
            self._fin_confirmado = None
        worker = self.hilo()
        lector = bool(worker is not None and getattr(worker, "modo", "") == "buscar_websearch")
        terminado = bool(
            firma is not None and firma == self._fin_confirmado
            and (worker is None or lector) and not self._vigilando()
            and not self._cola_de_acciones
            and not self._comprobar_al_terminar
            and not self._subir_al_terminar
            and not self._indexar_al_terminar
        )
        self.progreso.setRange(0, 100)
        # El ultimo punto incluye la comprobacion y el cierre del hilo.
        # Redondear una cola casi lista tampoco puede anunciar el 100%.
        self.progreso.setValue(
            100 if terminado else
            0 if avance is None else min(99, round(avance * 100))
        )
        self.progreso.setFormat(
            "%p% - Proceso terminado" if terminado else "%p%"
        )
        self.reloj_label.setVisible(not terminado)

    # ── la línea viva de la bitácora ───────────────────────────────

    def _totales_bitacoras(self) -> str:
        """Cuenta bitácoras reales, sin separadores ni reintentos repetidos."""
        from app.airvault.model import EstadoRegistro

        conocidos = {str(t.carpeta): t for t in self._trabajos}
        conocidos.update({
            str(t.carpeta): t for t in self._estado.get("trabajos") or []
        })
        trabajos = self._filtrar_trabajos(conocidos.values())
        total = 0 if trabajos else getattr(self, "_total_ejecucion", 0)
        subidas = indexadas = 0
        for trabajo in trabajos:
            manifiesto = trabajo.manifiesto
            if manifiesto.cancelado:
                continue
            registros = manifiesto.bitacoras()
            cantidad = len(registros)
            total += cantidad
            completado = manifiesto.etapa_hecha("completar")
            if manifiesto.etapa_hecha("subir") or manifiesto.batch_id or completado:
                subidas += cantidad
            confirmado = completado or manifiesto.etapa_hecha("verificar")
            indexadas += cantidad if confirmado else sum(
                r.estado is EstadoRegistro.ESCRITA for r in registros
            )
        return f"Total: {subidas} de {total} bitácoras subidas, {indexadas} de {total} indexadas"

    def _nombre_del_paso(self, texto: str) -> str:
        """Identifica el batch por su nombre en los avisos de trabajo."""
        from app.airvault.flujo import _prefijo

        for trabajo in self._estado.get("trabajos") or self._trabajos:
            prefijo = _prefijo(trabajo)
            if prefijo and texto.startswith(prefijo):
                paso = texto[len(prefijo):]
                if paso.startswith("Posible duplicado:"):
                    paso = (
                        "Posible duplicado; se continúa la subida autorizada."
                        if "se continúa" in paso else "Posible duplicado; no se sube."
                    )
                return f"Batch «{trabajo.manifiesto.nombre_batch}»: {paso}"
        return texto

    def _actualizar_latido(self) -> None:
        """Pone, cambia o quita la línea viva según lo que esté en marcha.

        Trabajando, gira; esperando a la próxima revisión, cuenta lo que
        falta. Sin nada de las dos cosas no queda ninguna: esa ausencia es
        lo que dice que el proceso está parado.
        """
        if not hasattr(self, "bitacora"):
            return
        if self.hilo() is None and self._lectura_websearch_pendiente() is None and not self._vigilando():
            if self._latido is not None:
                self._latido.stop()
            if self._linea_viva is not None:
                self.bitacora.takeItem(self.bitacora.row(self._linea_viva))
                self._linea_viva = None
            return
        if self._linea_viva is None:
            self._linea_viva = QListWidgetItem()
            # Ni se elige ni se copia: no es algo que haya pasado.
            self._linea_viva.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.bitacora.addItem(self._linea_viva)
            self.bitacora.scrollToBottom()
        if self._latido is None:
            self._latido = QTimer(self)
            self._latido.setInterval(MS_LATIDO)
            self._latido.timeout.connect(self._latir)
        if not self._latido.isActive():
            self._latido.start()
        self._pintar_linea_viva()

    def _vigilando(self) -> bool:
        return bool(self._vigilante is not None and self._vigilante.isActive())

    def _latir(self) -> None:
        """Cada vuelta del temporizador de la línea viva.

        Con la ventana cerrada la revisión sigue, pero repintar diez veces
        por segundo lo que nadie ve no; al volver a abrirla se pone al día
        en la vuelta siguiente.
        """
        if self.isVisible() or (self.hilo() is None and self._lectura_websearch_pendiente() is None and not self._vigilando()):
            self._pintar_linea_viva()

    def _pintar_linea_viva(self) -> None:
        """Texto, color e icono de la línea viva, con el tema de ahora."""
        linea = self._linea_viva
        if linea is None:
            return
        lector_paralelo = self._lectura_websearch_pendiente()
        worker = self.hilo() or lector_paralelo
        if worker is None and not self._vigilando():
            # El hilo acaba de terminar y su aviso todavía no llegó: la
            # línea se va ya, sin esperar a ``_al_terminar``.
            self._actualizar_latido()
            return
        if worker is not None:
            lector = getattr(worker, "modo", "") == "buscar_websearch"
            hechas, total = self._cuenta_websearch if lector else self._cuenta_paso
            parando = getattr(worker, "hay_que_parar", lambda: False)()
            esperando = total <= 0 and any(p in self._ultimo_paso.casefold()
                                           for p in ("esperando", "airvault está", "airvault arma", "entrando"))
            texto = ("Cancelando" if parando else "Buscando números en Web Search" if lector
                     else "Esperando respuesta de AirVault" if esperando
                     else "BITS trabajando") + f" ({_minutos(time.monotonic() - self._inicio_paso)})"
            if total > 0:
                texto += f" - {min(hechas, total)} de {total}"
                if lector:
                    texto += " batches"
            if not lector and lector_paralelo is not None:
                revisados, batches = self._cuenta_websearch
                if batches > 0:
                    texto += f" - Web Search: {min(revisados, batches)} de {batches} batches"
            color = color_indexando()
        else:
            restante = max(0, self._vigilante.remainingTime()) / 1000
            texto = f"Depende de AirVault: próxima revisión en {_minutos(restante)}"
            color = color_ayuda()
        linea.setText(f"{texto} - {self._totales_bitacoras()}")
        linea.setForeground(QColor(color))
        linea.setIcon(self._icono_de_latido(color, girando=worker is not None))

    def _icono_de_latido(self, color: str, girando: bool) -> QIcon:
        """Un arco que da una vuelta por segundo, o un círculo quieto.

        Se dibuja y no se escribe con un carácter: la fuente de la interfaz
        no trae los de una terminal y cada Windows los sustituía por otro.
        """
        # Al alto de la letra de la bitácora: la lista no fija tamaño de
        # icono y el suyo por omisión es -1.
        lado = self.bitacora.fontMetrics().height()
        escala = self.bitacora.devicePixelRatioF()
        mapa = QPixmap(round(lado * escala), round(lado * escala))
        mapa.setDevicePixelRatio(escala)
        mapa.fill(Qt.GlobalColor.transparent)
        pintor = QPainter(mapa)
        pintor.setRenderHint(QPainter.RenderHint.Antialiasing)
        pluma = QPen(QColor(color))
        pluma.setWidthF(2.0)
        pluma.setCapStyle(Qt.PenCapStyle.RoundCap)
        pintor.setPen(pluma)
        caja = QRectF(2, 2, lado - 4, lado - 4)
        if girando:
            # Ángulos de Qt en dieciseisavos de grado y en sentido contrario
            # al reloj; el signo lo hace girar como uno.
            angulo = int(time.monotonic() * 360) % 360
            pintor.drawArc(caja, -angulo * 16, 270 * 16)
        else:
            pintor.drawEllipse(caja)
        pintor.end()
        return QIcon(mapa)

    # ── respuestas del hilo ────────────────────────────────────────

    def _mostrar_paso(self, texto: str, hechas: int, total: int) -> None:
        visible = self._nombre_del_paso(texto)
        self.estado_label.setText(visible)
        self.estado_label.setToolTip(visible)
        # La barra no se vuelve «en marcha continua» en los pasos sin
        # cuenta: es la de toda la cola y se queda donde va. Que el trabajo
        # sigue vivo lo dice la línea «En curso» de la bitácora.
        self._cuenta_paso = (hechas, total)
        self._seguir_en_vuelo(texto, hechas, total)
        self._pintar_avance()
        # Los avisos de una subida llegan por trozo (mil en una entrega
        # grande), así que solo se anota cuando cambia el texto: la
        # bitácora cuenta por qué etapa va, no cuántas veces avisó.
        if texto != self._ultimo_paso:
            self._ultimo_paso = texto
            self._inicio_paso = time.monotonic()
            self._anotar(visible)
            self._ajustar_confirmacion()

    def _anotar(self, texto: str, detalles: Sequence[str] = ()) -> None:
        """Apunta el paso en la bitácora, con su hora.

        ``detalles`` es la lista que acompaña al paso (los batches que se
        comprobaron, los que faltan por subir) y va uno por línea debajo
        del encabezado. Todos seguidos en la misma línea, separados por
        «;», había que leerla entera para encontrar un batch.
        """
        hora = time.strftime("%H:%M:%S")
        sangria = " " * (len(hora) + 2)
        lineas = [f"{hora}  {texto}"]
        lineas += [f"{sangria}{detalle}" for detalle in detalles]
        lineas.append(f"{sangria}{self._totales_bitacoras()}")
        # Por encima de la línea viva, que es siempre la última: como el
        # cursor de una terminal, lo que pasa se escribe antes que él.
        if self._linea_viva is not None:
            self.bitacora.insertItem(
                self.bitacora.row(self._linea_viva), "\n".join(lineas)
            )
        else:
            self.bitacora.addItem("\n".join(lineas))
        while self.bitacora.count() > LIMITE_BITACORA:
            self.bitacora.takeItem(0)
        self.bitacora.scrollToBottom()

    def _al_subir(self, datos: dict) -> None:
        self._al_actualizar_subidas(datos)
        fallos = datos.get("fallos") or []
        # Lo que quedó de verdad, no lo que se supone: con el indexado en
        # cadena, al acabar de subir varios batches ya están completados, y
        # decir que «AirVault tiene que procesarlos» era contar otra cosa.
        conteo = conteo_de_estados(self._partes_del_alcance())
        self.resumen.setText(
            "Subida terminada" + (f": {conteo}." if conteo else ".")
            + self._aviso_de_cargas_fallidas(fallos)
        )
        self.estado_label.setText("Subida terminada")
        self._anotar("Subida terminada")
        # El motivo de cada carga que no salió. Antes solo quedaba en el
        # archivo de registro, así que la ventana decía «subida terminada»
        # de archivos que nunca se enviaron y nadie sabía por qué.
        for nombre, detalle in fallos:
            self._anotar(f"«{nombre}»: {mensaje_error(detalle, 'No se pudo subir. Vuelva a intentarlo.')}")
        for nombre, detalle in datos.get("fallos_indexado") or []:
            self._anotar(f"«{nombre}»: {mensaje_error(detalle, 'No se pudo indexar. Vuelva a revisar en AirVault.')}")
        # «Subir» no confia en la marca local: en cada clic consulta la cola
        # remota, recupera el ID que falte y solo entonces deja indexar. La
        # opcion de espera automatica decide si se seguira preguntando cuando
        # AirVault aun lo procese, pero nunca elimina esta primera comprobacion.
        self._comprobar_al_terminar = True

    def _al_actualizar_subidas(self, datos: dict) -> None:
        """Actualiza el estado interno antes de buscar los IDs."""
        from app.airvault.flujo import estado_local

        self._recibir_trabajos(datos["trabajos"])
        if self._alcance_proceso is not None:
            self._alcance_proceso.update(str(t.carpeta) for t in datos["trabajos"]
                                        if not t.manifiesto.etapa_hecha("completar"))
        self._estado["trabajos"] = self._filtrar_trabajos(
            self._trabajos, self._worker_filtrado
        )
        self._estados = [estado_local(t) for t in self._trabajos]
        self._pintar_lotes()
        self._ajustar_confirmacion()

    def _al_batch_encontrado(self, datos: dict) -> None:
        """Muestra el ID apenas se resuelve, sin esperar las otras búsquedas."""
        from app.airvault.flujo import estado_local

        self._recibir_trabajos(datos["trabajos"])
        self._estado["trabajos"] = self._filtrar_trabajos(
            self._trabajos, self._worker_filtrado
        )
        remoto = datos["estado"]
        self._estados = [estado_local(t) for t in self._trabajos]
        clave = str(remoto.trabajo.carpeta)
        for indice, parte in enumerate(self._estados):
            if str(parte.trabajo.carpeta) == clave:
                self._estados[indice] = remoto
                break
        self._pintar_lotes()
        self._anotar(
            f"Detectado en AirVault: «{remoto.nombre}» es el batch "
            f"{remoto.batch_id}"
        )

    def _al_batch_indexando(self, trabajo, activo: bool) -> None:
        clave = str(trabajo.carpeta)
        if activo:
            self._indexando.add(clave)
        else:
            self._indexando.discard(clave)
        self._pintar_lotes()

    def _al_batch_indexado(self, datos: dict) -> None:
        """Pinta cómo quedó un batch que la cadena acaba de indexar."""
        from app.airvault.flujo import estado_local

        trabajo = datos["trabajo"]
        self._anotar_cierres(datos)
        self._estados = [estado_local(t) for t in self._trabajos]
        if datos.get("incompleto"):
            self._programar_reconfirmaciones(datos.get("carpetas"))
        self._anotar_intentos(datos.get("amarillas"))
        # También si se cortó: lo que quede sin escribir lo retoma el reloj.
        self._ajustar_vigilancia()
        self._pintar_lotes()
        # Se dice en qué quedó, que no es siempre «indexado»: con «Completar
        # batch» sale completado, y con páginas amarillas, incompleto.
        quedo = estado_local(trabajo).titulo.lower()
        if trabajo.manifiesto.solo_subir:
            cuenta = (
                f"{datos['validas']} de {datos['total']} páginas con los datos "
                "disponibles guardados; las incidencias siguen en amarillo "
                "para revisión manual"
            )
        else:
            cuenta = f"{datos['validas']} de {datos['total']} páginas en verde"
        self._anotar(
            f"Batch «{trabajo.manifiesto.nombre_batch}» {quedo}: {cuenta}"
        )
        self.resumen.setText(
            f"«{trabajo.manifiesto.nombre_batch}» {quedo}: {cuenta}."
        )

    def _al_comprobar(self, datos: dict) -> None:
        # Una comprobación buena borra la racha: lo que llevara fallando
        # dejó de fallar.
        self._fallos_seguidos = 0
        self._fin_pendiente = True
        self._estado["planes"] = datos["planes"]
        self._recibir_trabajos(self._estado.get("trabajos") or self._trabajos)
        acotado = bool(datos.get("acotado"))
        revisados = list(datos["estados"])
        por_carpeta = {str(p.trabajo.carpeta): p for p in revisados}
        self._estados = [por_carpeta.pop(str(p.trabajo.carpeta), p) for p in self._estados]
        self._estados.extend(por_carpeta.values())
        self._descontar_reconfirmaciones(revisados)
        self._indexado_incompleto = False
        self._pintar_lotes()
        if acotado:
            # Revisar desde el menu contextual es una orden de una sola vez.
            # No deja un reloj que despues vuelva a recorrer toda la tabla.
            self._parar_vigilancia()
        else:
            self._ajustar_vigilancia()
        listos = [p for p in revisados if p.se_puede_indexar]
        self.boton_indexar.setEnabled(
            bool(self._listos())
            or (
                self.completar_check.isChecked()
                and bool(self._por_completar())
            )
        )
        self.estado_label.setText("Revisado")
        # Lo que nunca llegó a AirVault sale a Quick Upload sin esperar a
        # que nadie pulse nada. Antes la comprobación periódica solo
        # preguntaba, así que un archivo sin subir se quedaba en la lista
        # para siempre mientras el reloj seguía consultando por él. Lo que
        # sí llegó y AirVault no publicó no entra aquí: eso se avisa y lo
        # manda quien mire Web Index.
        sin_subir = self._filtrar_trabajos(datos.get("recuperados") or [])
        if not sin_subir and not acotado:
            sin_subir = self._sin_subir_todavia()
        if sin_subir:
            self._estado["pendientes_subida"] = sin_subir
            self._estado["indexar_al_encontrar"] = self._opciones.indexar
            self._estado["completar"] = self.completar_check.isChecked()
            self._subidas_del_ciclo.update(
                str(trabajo.carpeta) for trabajo in sin_subir
            )
            self._subir_al_terminar = True
            self._anotar(
                "Se suben los que faltan:",
                [t.manifiesto.nombre_batch for t in sin_subir],
            )
        conteo = conteo_de_estados(self._partes_del_alcance())
        if listos:
            self.resumen.setText(
                self._resumen_de_listos(datos["partes"])
                + ("" if acotado else self._aviso_para_volver_a_subir())
            )
        elif acotado:
            self.resumen.setText(
                f"Selección revisada: {conteo_de_estados(revisados)}. No se "
                "sigue revisando sola."
            )
        elif self._subidas_perdidas():
            self.resumen.setText(
                self._aviso_para_volver_a_subir().lstrip()
            )
        elif self._falta_esperar():
            self.resumen.setText(
                f"Esperando a AirVault: {conteo}. Se revisa cada "
                f"{self.minutos_spin.value()} min."
                + self._aviso_para_subir_a_mano()
            )
        else:
            self.resumen.setText(
                f"Revisión terminada: {conteo}." if conteo else
                "Revisión terminada: no hay batches en la cola."
            )
            self.estado_label.setText("Revisión terminada")
        # Una línea por batch con su rótulo, como la columna Estado. El
        # detalle entero de cada uno sigue en la ayuda de su celda.
        self._anotar(
            "Revisado:",
            [f"{p.nombre}: {p.titulo}" for p in revisados],
        )
        self._limpiar_progreso()
        if not acotado and self._opciones.indexar and (
            self._listos_automaticos()
            or (
                self.completar_check.isChecked()
                and self._por_completar(automatico=True)
            )
        ):
            self._indexar_al_terminar = True

    def _resumen_de_listos(self, partes) -> str:
        """Cuántas páginas se escribirían y cuántas quedan bloqueadas."""
        from app.airvault.report import _resumen_sumado

        if not partes:
            return "Listos para indexar; falta calcular qué se escribiría."
        resumen = _resumen_sumado(partes)
        if len(partes) > 1:
            donde = f"Listos para indexar {len(partes)} batches"
        else:
            nombre = partes[0][0] or partes[0][1].batch_id
            donde = f"Listo para indexar «{nombre}»"
        return (
            f"{donde}: {resumen['total']} páginas, "
            f"{resumen['escribibles']} se escribirían y "
            f"{resumen['bloqueadas']} bloqueadas."
        )

    def _al_indexar(self, datos: dict) -> None:
        resultado = datos["resultado"]
        self._anotar_cierres(datos)
        acotado = bool(datos.get("acotado"))
        self._fin_pendiente = bool(
            not resultado.interrumpido and not datos.get("incompleto")
            and all(r.completado for _t, r in datos.get("cierres") or [])
        )
        # Una accion de la tabla no encadena una revision global. Su resultado
        # ya confirmado debe actualizar las filas que acaba de escribir o cerrar.
        from app.airvault.flujo import estado_local

        carpetas = set(datos.get("carpetas") or [])
        carpetas.update(str(t.carpeta) for t, _r in datos.get("cierres") or [])
        if carpetas:
            self._estados = [
                estado_local(p.trabajo) if str(p.trabajo.carpeta) in carpetas
                else p for p in self._estados
            ]
            self._pintar_lotes()
        if datos.get("pausado"):
            self._fin_pendiente = False
            self.estado_label.setText("Indexado automático pausado")
            self.resumen.setText("El batch en curso quedó guardado. Active el indexado automático o pulse Indexar para continuar.")
            self._anotar("Indexado automático pausado: quedan batches pendientes")
            self._ajustar_vigilancia()
            self._limpiar_progreso()
            return
        if acotado:
            self._parar_vigilancia()
        lotes = datos.get("lotes", 1)
        self.boton_indexar.setEnabled(False)
        donde = f" en {lotes} batches" if lotes > 1 else ""
        # Primero lo que importa, las páginas confirmadas; después cómo se
        # llegó ahí. Al revés, la cifra que se venía a mirar quedaba al final
        # de una frase que la línea recortaba.
        confirmacion = (
            "con los datos disponibles guardados"
            if datos.get("incluye_revision") else "en verde"
        )
        cuenta = (
            f"{datos['validas']} de {datos['total']} páginas {confirmacion}{donde} "
            f"({resultado.escritas} escritas, {resultado.omitidas} omitidas, "
            f"{resultado.fallidas} fallidas)."
        )
        separadores_borrados = getattr(resultado, "separadores_borrados", 0)
        separadores_pendientes = getattr(
            resultado, "separadores_pendientes", 0
        )
        if separadores_borrados:
            cuenta += f" Se borraron {separadores_borrados} separadoras."
        if separadores_pendientes:
            cuenta += (
                f" No se pudieron borrar {separadores_pendientes} "
                "separadoras."
            )
        self._anotar_intentos(datos.get("amarillas"))
        if resultado.interrumpido:
            if not acotado:
                # Sin sesión o sin red no se puede seguir ahora, pero la
                # ejecución no se queda quieta: la próxima vuelta del reloj
                # lo retoma desde donde quedó.
                self._ajustar_vigilancia()
            self.resumen.setText(
                f"{mensaje_error(resultado.interrumpido, 'El indexado se detuvo.')} {cuenta}"
            )
            self.estado_label.setText("Indexado cortado")
            self._anotar("Indexado cortado; se retoma al revisar")
            self._limpiar_progreso()
            return
        if datos.get("incompleto"):
            self._indexado_incompleto = True
            motivo = next(
                (d for d in resultado.detalles
                 if "fecha" in d.casefold() or "end date" in d.casefold()
                 or "obligatorios" in d.casefold()),
                resultado.detalles[-1] if resultado.detalles else "",
            )
            if not acotado:
                # Lo que no se confirmó puede ser una lectura que AirVault
                # aún no reflejaba: el reloj vuelve a mirarlo y, si ya está,
                # lo completa sin esperar a que alguien pulse nada.
                self._programar_reconfirmaciones(datos.get("carpetas"))
                self._ajustar_vigilancia()
            sigue = bool(
                self._vigilante is not None and self._vigilante.isActive()
            )
            self.resumen.setText(
                f"Indexado incompleto: {cuenta}"
                + self._cuenta_de_cierres(datos)
                + " Quedan páginas en amarillo"
                + (
                    f"; se vuelve a revisar solo cada "
                    f"{self.minutos_spin.value()} min."
                    if sigue else "."
                )
                + (f" {mensaje_error(motivo, 'Revise las páginas pendientes en AirVault.')}" if motivo else "")
            )
            self.estado_label.setText("Indexado incompleto")
            self._anotar("Indexado incompleto: quedan páginas en amarillo")
            self._limpiar_progreso()
            return
        self._indexado_incompleto = False
        if datos.get("incluye_revision"):
            cuenta += (
                " REVISAR guardado; sus incidencias siguen en amarillo "
                "para revisión manual."
            )
        self.resumen.setText(
            f"Indexado terminado: {cuenta}" + self._cuenta_de_cierres(datos)
        )
        self.estado_label.setText("Indexado terminado")
        self._anotar("Indexado terminado")
        self._limpiar_progreso()
        # Vuelve a preguntar para que la lista quede diciendo cómo acabó
        # cada batch, en vez de con lo que se sabía antes de escribir.
        if not acotado:
            self._comprobar_al_terminar = True

    def _cuenta_de_cierres(self, datos: dict) -> str:
        """Qué pasó con «Completar batch», si estaba marcado."""
        cierres = datos.get("cierres") or []
        if not cierres:
            return ""
        cerrados = [t for t, r in cierres if r.completado]
        colgados = [(t, r) for t, r in cierres if not r.completado]
        partes = []
        if cerrados:
            texto = (
                f" {len(cerrados)} batches completados; pasaron a Web Search."
                if len(cerrados) > 1 else
                " Batch completado; pasó a Web Search."
            )
            quitadas = sum(len(r.quitadas) for _t, r in cierres if r.completado)
            if quitadas:
                # Se dice porque es un cambio en el batch: esas páginas ya no
                # están, y quien lo abra en AirVault no las va a encontrar.
                texto += f" Se quitaron {quitadas} páginas separadoras."
            partes.append(texto)
        for trabajo, resultado in colgados:
            partes.append(
                f" No se pudo completar «{trabajo.manifiesto.nombre_batch}»: "
                f"{mensaje_error(resultado.detalle, 'Revise las páginas pendientes en AirVault.')}"
            )
        return "".join(partes)

    def _al_fallar(self, mensaje: str) -> None:
        worker = self.hilo()
        if worker is not None and getattr(worker, "modo", "") == "buscar_websearch":
            mensaje = mensaje_error(mensaje, "No se pudo verificar la publicación en Web Search.")
            self.resumen.setText("Web Search pendiente: " + mensaje)
            self._anotar("AirVault no permitió verificar la publicación: " + primera_frase(mensaje))
            self._ajustar_confirmacion()
            self._pintar_avance()
            return
        mensaje = mensaje_error(mensaje, "No se pudo continuar el indexado. Vuelva a revisar en AirVault.")
        self._fin_pendiente = False
        self._resultado_fallido = True
        self._fin_confirmado = None
        preparados = self._estado.get("trabajos") or []
        if preparados and not self._trabajos:
            # Preparar los PDF ocurre antes de conectar. Si la sesion o la
            # subida falla despues, se conserva ese reparto para reanudarlo
            # con el mismo limite y no mezclar manifiestos de dos repartos.
            from app.airvault.flujo import estado_local

            self._trabajos = list(preparados)
            self._estados = [estado_local(t) for t in self._trabajos]
            self._pintar_lotes()
        # Un fallo no para nada: AirVault devuelve un error de vez en cuando
        # y la sesión se renueva sola, así que el siguiente intervalo tiene
        # todas las papeletas de salir bien, y nadie está delante para volver
        # a pulsar. Tampoco una racha: la red o AirVault vuelven, y la
        # ejecución tiene que acabar indexada. Tras varios fallos seguidos
        # solo se pregunta más espaciado.
        self._fallos_seguidos += 1
        if not self._cerrar_al_terminar:
            self._ajustar_vigilancia()
        sigue = bool(
            self.auto_check.isChecked()
            and self._vigilante is not None
            and self._vigilante.isActive()
        )
        if sigue:
            mensaje += f" Se reintenta en {self._minutos_de_vigilancia()} min."
        self.resumen.setText(mensaje)
        self.estado_label.setText("El indexado no pudo continuar")
        # El mensaje entero queda en el resumen, que se lee de una vez.
        self._anotar(f"Se detuvo: {primera_frase(mensaje)}")
        self._limpiar_progreso()

    def _al_cancelar(self) -> None:
        """Lo paró quien lo lanzó: se dice y se sueltan los batches."""
        self._fin_pendiente = False
        self._resultado_fallido = True
        self._fin_confirmado = None
        self.estado_label.setText("Cancelado")
        self._anotar("Cancelado: se desbloquean los batches abiertos")
        self._parar_vigilancia()
        self._soltar_lotes()
        self.resumen.setText(
            "Se canceló el trabajo. Los batches quedaron desbloqueados y lo "
            "escrito se conserva."
        )
        self._limpiar_progreso()

    def _al_terminar(self) -> None:
        """Cierre común del hilo, salga como salga."""
        self._worker_filtrado = None
        self._indexando.clear()
        # Lo que quedaba a medias ya lo cuentan los estados que devolvió el
        # hilo; una subida que falló no puede seguir sumando lo que llevaba.
        self._en_vuelo.clear()
        self._pintar_lotes()
        self._habilitar(True)
        self._parar_reloj()
        # Si lo que sigue lanza otro hilo, la línea vuelve a girar en el acto.
        self._actualizar_latido()
        if self._cerrar_al_terminar:
            self._websearch_proceso_activo = False
            self._cerrar_al_terminar = False
            self.close()
            return
        # Las metas ya confirmadas cierran la cadena antes de otra consulta.
        # Las acciones pedidas desde la cola conservan su turno.
        if not self._resultado_fallido and self._firma_de_fin() is not None:
            self._fin_pendiente = True
            if self._anunciar_fin():
                return
        if getattr(self, "_comprobar_al_terminar", False):
            self._comprobar_al_terminar = False
            self._comprobar()
            return
        if getattr(self, "_subir_al_terminar", False):
            self._subir_al_terminar = False
            if self._estado.get("pendientes_subida"):
                self._lanzar("subir_pendientes", self._estado)
                return
        if getattr(self, "_indexar_al_terminar", False):
            self._indexar_al_terminar = False
            if self._listos_automaticos() or (
                self.completar_check.isChecked() and self._por_completar(automatico=True)
            ):
                self._indexar(automatico=True)
                return
        # La cadena que alguien pidió termina aquí; lo que venga después lo
        # arranca el reloj o un botón nuevo.
        self._websearch_proceso_activo = False
        self._cadena_manual = False
        self._publicar_avance()
        # Lo que se pidió desde la tabla mientras esto trabajaba entra ahora,
        # que es lo que hace de la tabla una cola y no una lista de avisos.
        if self._siguiente_de_la_cola():
            return
        if not self._resultado_fallido and self._firma_de_fin() is not None:
            self._fin_pendiente = True
            self._parar_vigilancia()
        self._anunciar_fin()

    def _anunciar_fin(self) -> bool:
        """Deja el final en la barra y la bitacora cuando ya no falta nada."""
        if not self._fin_pendiente or self._resultado_fallido:
            return False
        firma = self._firma_de_fin()
        lector = self.lectura_websearch()
        if (
            firma is None or self.hilo() is not None or self._cola_de_acciones
            or self._websearch_manual_pendiente
            or (lector is not None and not lector.estado.get("confirmacion_automatica"))
        ):
            return False
        self._fin_pendiente = False
        self._comprobar_al_terminar = False
        self._subir_al_terminar = False
        self._indexar_al_terminar = False
        self._cadena_manual = False
        self._websearch_proceso_activo = False
        self._websearch_inicio_por_revisar.clear()
        self._websearch_pendientes.clear()
        anterior = self._fin_confirmado
        self._fin_confirmado = firma
        if self._confirmador is not None:
            self._confirmador.stop()
        self._parar_vigilancia()
        self._parar_reloj()
        if lector is not None:
            # La lectura automatica es auxiliar. Se cancela sin bloquear la
            # interfaz y su QThread se conserva hasta su señal finished.
            lector.cancelar()
        self.boton_cancelar.setEnabled(False)
        partes = self._partes_del_proceso()
        texto = (
            f"Proceso terminado: {conteo_de_estados(partes)}. "
            "Todo lo solicitado se completó. No queda trabajo automático pendiente."
        )
        if any(p.trabajo.manifiesto.solo_subir for p in partes):
            texto += " Las incidencias de REVISAR quedan para revisión manual."
        self.resumen.setText(texto)
        self.estado_label.setText("Proceso terminado")
        self.estado_label.setToolTip(texto)
        self._actualizar_latido()
        if firma != anterior:
            self._anotar(texto)
            self.proceso_terminado.emit(texto)
        self._pintar_avance()
        self._publicar_avance()
        return True

    # ── lo que ve la ventana principal ─────────────────────────────

    def _publicar_avance(self) -> None:
        """Cuenta a la ventana principal por qué paso va esta ejecución.

        Se deduce de los batches, no de qué botón se pulsó: es lo único que
        vale igual venga la orden de la cadena automática, del reloj o de
        la tabla. Cada paso está hecho cuando ya no queda ningún batch al
        que le falte, en curso mientras el hilo trabaja o el reloj espera, y
        cortado cuando no queda quién lo haga avanzar.
        """
        from app.airvault.flujo import (AUTOCOMPLETADO, COMPLETADO, INDEXADO,
                                        SIN_SUBIR, POSIBLE_DUPLICADO, PUBLICADO)
        from app.gui.automatizacion import (COMPLETAR, CORTADO, EN_CURSO,
                                            ESPERAR, HECHO, INDEXAR, PENDIENTE,
                                            SUBIR)

        if not self._estados:
            return
        trabajando = self.hilo() is not None
        partes = self._partes_del_proceso()
        propios = [
            parte for parte in partes
            if not parte.trabajo.manifiesto.solo_subir
        ]
        terminados = (INDEXADO, COMPLETADO, AUTOCOMPLETADO, PUBLICADO)
        cerrados = (COMPLETADO, AUTOCOMPLETADO, PUBLICADO)

        def como(hecho: bool, avanza: bool) -> str:
            if hecho:
                return HECHO
            if trabajando or avanza:
                return EN_CURSO
            return CORTADO

        # Nadie va a mover esto solo si no hay hilo, ni reloj, ni nada
        # encolado: eso es que la cadena se paró aquí.
        avanza = bool(
            self._vigilante is not None and self._vigilante.isActive()
        ) or bool(self._cola_de_acciones)
        subido = not any(
            parte.estado in (SIN_SUBIR, POSIBLE_DUPLICADO) for parte in partes
        )
        self.avance_automatico.emit(SUBIR, como(subido, avanza))
        self.avance_automatico.emit(
            ESPERAR,
            HECHO if subido and not self._falta_esperar()
            else como(False, avanza) if subido
            else PENDIENTE,
        )
        indexado = bool(propios) and all(
            parte.estado in terminados for parte in propios
        )
        self.avance_automatico.emit(
            INDEXAR, como(indexado, avanza) if subido else PENDIENTE
        )
        completado = bool(propios) and all(
            parte.estado in cerrados for parte in propios
        )
        self.avance_automatico.emit(
            COMPLETAR, como(completado, avanza) if indexado else PENDIENTE
        )

    def _limpiar_progreso(self) -> None:
        """Para el reloj del paso y deja la barra en lo que lleva la cola."""
        self._pintar_avance()
        self._parar_reloj()

    # ── cierre ─────────────────────────────────────────────────────

    def hilo(self) -> Optional[QThread]:
        """Hilo del indexado si está en marcha, para que el cierre lo espere.

        Cerrar el programa destruyendo un ``QThread`` vivo lo mata, y este
        puede estar a medio escribir un batch.
        """
        worker = self._worker
        if worker is None:
            return None
        try:
            return worker if worker.isRunning() else None
        except RuntimeError:
            # El objeto C++ ya se destruyó tras ``deleteLater``.
            return None

    def lectura_websearch(self) -> Optional[QThread]:
        worker = self._worker_websearch
        if worker is None:
            return None
        try:
            return worker if worker.isRunning() else None
        except RuntimeError:
            return None

    def _lectura_websearch_pendiente(self) -> Optional[QThread]:
        """Una lectura auxiliar cancelada al terminar no mantiene el proceso vivo."""
        lector = self.lectura_websearch()
        if (lector is not None and self._fin_confirmado is not None
                and lector.estado.get("confirmacion_automatica")):
            return None
        return lector

    def closeEvent(self, event) -> None:
        """Cerrar siempre se puede; con trabajo en vuelo, lo cancela antes.

        Antes se negaba a cerrar mientras hubiera un hilo vivo, y como el
        hilo podía estar esperando cinco minutos a que alguien entrara a
        AirVault, la ventana se quedaba sin salida: ni cerraba, ni avanzaba,
        ni había nada que pulsar. Ahora se pide la cancelación y la ventana
        se va sola en cuanto el hilo suelta los batches que tuviera tomados,
        que es lo que no se puede dejar a medias.

        Lo comprobado sí sobrevive a un cierre sin trabajo en vuelo: los
        manifiestos guardan en qué quedó cada batch y al reabrir se retoma.

        Cerrarla **no** apaga la comprobación automática. Esperar a que
        AirVault procese un batch puede llevar horas, y lo normal es cerrar
        esta ventana y seguir procesando en la principal; al volver, la
        lista ya está al día. El que sí la apaga es el cierre del programa.
        """
        if self.hilo() is not None:
            self._cerrar_al_terminar = True
            self._cancelar()
            self.resumen.setText(
                "Cancelando. La ventana se cierra en cuanto se desbloqueen "
                "los batches abiertos."
            )
            event.ignore()
            return
        # Las ventanas de consulta hablan de esta ejecución: dejarlas
        # sueltas mantendría el programa abierto por algo que ya no tiene de
        # dónde colgarse.
        for ventana in list(self._ventanas_de_consulta):
            ventana.close()
        super().closeEvent(event)

    def _cancelar(self) -> None:
        """Le pide al hilo que pare. No espera: esperar congelaría esto."""
        worker = self.hilo()
        if worker is None:
            lector = self.lectura_websearch()
            if lector is not None:
                lector.cancelar()
                self.boton_cancelar.setEnabled(False)
                self.estado_label.setText("Cancelando búsqueda en Web Search")
            return
        if self._confirmador is not None:
            self._confirmador.stop()
        worker.cancelar()
        self.boton_cancelar.setEnabled(False)
        self.estado_label.setText("Cancelando…")
        self._anotar("Cancelando: se espera a desbloquear los batches abiertos")

    def detener(self) -> None:
        """Pide al hilo que pare; la llama la ventana principal al cerrarse."""
        self._deteniendo = True
        self._websearch_pendientes.clear()
        self._parar_vigilancia()
        if self._confirmador is not None:
            self._confirmador.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancelar()
            self._worker.wait(5000)
        if self._worker_websearch is not None and self._worker_websearch.isRunning():
            self._worker_websearch.cancelar()
            self._worker_websearch.wait(7000)
        # Aquí sí se espera: el programa se está cerrando y un batch que
        # queda tomado deja colgada la próxima apertura.
        self._soltar_lotes(esperar=True)
        # Y a los que ya estuvieran soltando en su propio hilo: destruirlos
        # a media petición deja el batch tomado, que es lo que se venía a
        # evitar.
        for hilo in list(self._soltando):
            hilo.wait(5000)

    def _soltar_lotes(self, esperar: bool = False) -> None:
        """Suelta en AirVault los batches que hubieran quedado tomados.

        Con el recorrido normal no queda ninguno: leer el batch lo suelta en
        cuanto termina y escribirlo también. Esto es para lo que se corta
        por el medio (un cierre, una cancelación, un fallo de red), porque
        un batch tomado no da error: deja colgada la próxima vez que alguien
        lo abra.

        Va en un hilo aparte salvo al cerrar el programa. Soltar es una
        petición por batch contra un servidor que puede tardar un minuto en
        contestar, y hacerlo en el hilo de la ventana la dejaba congelada
        justo al cambiar de ejecución o al cancelar.
        """
        trabajos = self._trabajos
        cliente = self._estado.get("cliente")
        if not trabajos or cliente is None:
            return
        sesion = self._estado.get("sesion")
        if sesion is not None and sesion.cancelada:
            # Soltar es trabajo que existe *porque* se canceló: con la
            # sesión cortada, cada petición se negaría y los batches
            # quedarían tomados, que es justo lo que esto viene a evitar.
            sesion.reanudar()
        if esperar:
            _soltar(trabajos, cliente)
            return
        hilo = SoltarLotesWorker(trabajos, cliente, self)
        # Se guarda la referencia: un QThread sin dueño vivo se destruye al
        # salir del método y Qt lo mata a media petición.
        self._soltando.append(hilo)
        hilo.finished.connect(lambda: self._soltando.remove(hilo)
                              if hilo in self._soltando else None)
        hilo.start()
