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
3. **Indexar** puede trabajar un batch listo mientras se siguen buscando los
   demás, pero siempre después de terminar todas las subidas. Se puede
   desactivar en «Automatización…», el menú que dice hasta dónde llega la
   cadena y que es el mismo que el de la ventana principal.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Optional, Sequence

from PySide6.QtCore import (QItemSelection, QItemSelectionModel,
                            QSignalBlocker, Qt, QThread, QTimer, Signal)
from PySide6.QtGui import (QBrush, QColor, QGuiApplication, QKeySequence,
                           QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListView,
                               QMenu, QMessageBox, QProgressBar, QPushButton,
                               QSpinBox, QTableWidget, QTableWidgetItem,
                               QToolButton, QVBoxLayout, QWidget)

from app.airvault.config import (AIRVAULT_FILENAME, AirVaultConfig,
                                 guardar_paginas_por_batch)
from app.airvault.session import SesionCancelada
from app.gui.airvault_busqueda import buscar_en_la_cola, frase_de
from app.gui.automatizacion import (COMPLETAR, MenuAutomatizacion,
                                    OpcionesAutomatizacion)
from app.gui.csv_utils import (TEXTO_ELEGIR_EJECUCION, find_csv_files,
                               find_run_dirs, run_read_day)
from app.gui.responsive import available_area, fit_to_screen
from app.gui.text_copy import CopyableListWidget
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_L, SPACE_S, link_text_color, paleta
from app.gui.widgets import (ElidedLabel, SpinBoxWithButtons,
                             align_vertical_scrollbar_to_header,
                             configure_combo_box, configure_menu_button,
                             data_table_qss, pane_status_colors,
                             pintar_celda_del_tema, pintar_del_tema, style_data_table,
                             window_stylesheet)
from app.utils.io import send_to_trash


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
# sin confirmar. Cada una relee AirVault y, si hace falta, reescribe y
# completa. Acotadas para que una página que de verdad necesita corrección
# manual no deje el reloj preguntando toda la tarde.
RECONFIRMACIONES_TRAS_INDEXAR = 3

# Fallos seguidos de la comprobación automática antes de parar el reloj.
# Uno solo no significa nada: AirVault devuelve un 500 de vez en cuando y
# la sesión se renueva sola, así que parar en el primero dejaba la ejecución
# muerta hasta que alguien volviera a la ventana. Tres seguidos ya no son un
# tropiezo, y repetir el mismo error toda la tarde no arregla nada.
FALLOS_SEGUIDOS_ANTES_DE_PARAR = 3

# Líneas que conserva la bitácora. Con la comprobación automática corriendo
# toda una tarde, sin tope crecería sin fin.
LIMITE_BITACORA = 300

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

TEXTO_SIN_SUBIR = (
    "Sin subir. El proceso comienza con «Subir a AirVault»."
)

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
            "indexar": self._indexar,
            "completar": self._completar,
        }
        try:
            etapas[self.modo]()
        except TrabajoCancelado:
            self.cancelado.emit()
        except SesionCancelada:
            # La sesión se cortó porque alguien canceló: es lo mismo que
            # llegar al siguiente paso con la bandera puesta, solo que sin
            # esperar a que el servidor conteste.
            self.cancelado.emit()
        except Exception as exc:  # noqa: BLE001 - llega a la interfaz
            self.fallo.emit(str(exc))

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
        csv = Path(estado["csv"])
        raiz = Path(estado["raiz"])

        self._avisar(f"Leyendo la ejecución {csv.parent.parent.name}", 0, 0)
        entrega = comprobar_entrega(csv)
        cuantas = sum(len(p.paginas) for p in entrega)
        archivos = ("1 archivo" if len(entrega) == 1
                    else f"{len(entrega)} archivos")
        self._avisar(f"{cuantas} páginas en {archivos} de entrega", 0, 0)
        resolutor = ResolutorFlota.load(raiz / FLOTA_CACHE_FILENAME)
        trabajos = preparar_partes(
            estado["config"], Path(estado["carpeta_job"]), csv,
            estado["nombre_lote"], resolutor=resolutor,
            paginas_por_batch=estado["paginas_por_batch"],
            avisar=self._avisar,
            compresion=estado.get("compresion", False),
            fin_de_mes=estado.get("fin_de_mes", False),
        )
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
        """Manda a Quick Upload lo que falte y encadena lo que se encuentre.

        Es el mismo camino para la subida inicial y para la reanudación
        automática: la lista de archivos cambia, pero no lo que se hace con
        ellos ni el carril paralelo que indexa cada batch en cuanto AirVault
        le asigna un ID.
        """
        from app.airvault.flujo import subir_partes

        estado = self.estado
        raiz = Path(estado["raiz"])
        trabajos = list(estado.get("trabajos") or por_subir)
        self._notificar_subidas(trabajos)
        futuros: list[Future] = []
        en_cola: set[str] = set()
        ejecutor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="airvault-indexado"
        )
        cliente_indice = self._cliente_paralelo(cliente)

        def al_encontrar(trabajo, _todos) -> None:
            """Publica el ID y pone el batch listo en el carril de escritura.

            La tabla se repinta con la ejecucion entera, no con la lista
            reducida que se acaba de enviar: al reanudar solo lo pendiente,
            las filas ya subidas desaparecerian de la ventana.
            """
            from app.airvault.flujo import comprobar_partes

            remoto = comprobar_partes(
                [trabajo], cliente, avisar=self._avisar
            )[0]
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
            futuros.append(ejecutor.submit(
                self._indexar_batch_encontrado,
                trabajo, cliente_indice, raiz,
            ))

        fallos: list[tuple[object, str]] = []
        try:
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
            ) or []
            # La escritura puede correr mientras este hilo busca las partes
            # siguientes, pero subir_partes retiene los hallazgos hasta que
            # hayan terminado todos los intentos de subida.
            for futuro in futuros:
                futuro.result()
        finally:
            ejecutor.shutdown(wait=True, cancel_futures=True)
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
        })

    def _cliente_paralelo(self, cliente):
        """Cliente independiente para indexar sin compartir Session con Upload."""
        from app.airvault.client import ClienteHttp

        sesion = self.estado.get("sesion")
        clonar = getattr(sesion, "clonar", None)
        if not callable(clonar):
            # Clientes falsos y adaptadores antiguos ya son objetos aislados
            # en sus pruebas; conservarlos mantiene ese contrato.
            return cliente
        return ClienteHttp(clonar(), self.estado["config"])

    def _indexar_batch_encontrado(self, trabajo, cliente, raiz: Path) -> dict:
        """Planifica e indexa un batch mientras se buscan los siguientes."""
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
        nuevos = 0
        for parte in estados:
            clave = str(parte.trabajo.carpeta)
            if clave in planes or not parte.se_puede_indexar:
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
        estado = self.estado
        cliente = estado["cliente"]
        trabajos = list(estado["listos"])
        planes = [estado["planes"][str(t.carpeta)] for t in trabajos]
        datos = self._ejecutar_indexado(
            trabajos, planes, cliente,
            completar=bool(estado.get("completar")),
        )
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
        from app.airvault.indexer import Resultado

        def esperar_confirmacion(segundos: float) -> None:
            self._avisar(
                "AirVault aún no confirma todas las páginas; se vuelven a "
                f"leer en {segundos:.0f} s", 0, 0,
            )
            self._dormir(segundos)

        cierres: list = []
        resultado = Resultado()
        validas = total = 0
        try:
            for intento in range(1, INTENTOS_INDEXADO + 1):
                parcial = indexar_partes(
                    trabajos, planes, avisar=self._avisar,
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
                validas, total, _problemas = verificar_partes(
                    trabajos, cliente, avisar=self._avisar,
                    esperas=() if parcial.interrumpido
                    else ESPERAS_CONFIRMACION[intento - 1],
                    dormir=esperar_confirmacion,
                )
                if validas == total or parcial.interrumpido:
                    break
                if intento < INTENTOS_INDEXADO:
                    self._avisar(
                        f"Reintentando páginas sin confirmar "
                        f"({intento + 1}/{INTENTOS_INDEXADO})", 0, 0,
                    )
                    resolutor = planes[0][1].resolutor if planes else None
                    planes = planificar_partes(
                        trabajos, cliente, resolutor=resolutor,
                        avisar=self._avisar,
                    )
                    for trabajo, plan in zip(trabajos, planes):
                        self.estado.setdefault("planes", {})[
                            str(trabajo.carpeta)
                        ] = plan
            if validas != total:
                resultado.detalles.extend(_problemas)
                for problema in _problemas[:5]:
                    self._avisar(problema, 0, 0)
            if completar and validas == total:
                cierres = completar_partes(
                    trabajos, cliente, avisar=self._avisar, automatico=True
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
                        "AirVault aún no deja completar; se vuelve a "
                        f"intentar en {espera:.0f} s", 0, 0,
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
        }

    def _completar(self) -> None:
        """Cierra batches ya verificados sin reescribir sus paginas."""
        from app.airvault.flujo import cerrar_partes, completar_partes
        from app.airvault.indexer import Resultado

        estado = self.estado
        cliente = self._conectar()
        trabajos = list(estado["por_completar"])
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
            "acotado": bool(estado.pop("completar_acotado", False)),
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
        # Reloj del paso en curso y último texto anotado, para no repetir
        # una línea por cada trozo de una subida.
        self._reloj: Optional[QTimer] = None
        self._inicio_paso = time.monotonic()
        self._ultimo_paso = ""
        # El que pregunta solo por los batches cada tantos minutos.
        self._vigilante: Optional[QTimer] = None
        # Fallos seguidos sin ninguna comprobación buena por medio. Es lo
        # que separa un tropiezo de AirVault de un problema que no se va a
        # arreglar solo; ver `FALLOS_SEGUIDOS_ANTES_DE_PARAR`.
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
        # Acciones pedidas desde la tabla mientras habia algo en vuelo.
        # Se van lanzando en orden segun el hilo queda libre.
        self._cola_de_acciones: list[tuple[str, list]] = []
        self._indexado_incompleto = False
        # Comprobaciones que le quedan a cada batch (por carpeta) que terminó
        # el indexado sin confirmar. Mantienen vivo el reloj aunque no quede
        # nada más que esperar; ver `RECONFIRMACIONES_TRAS_INDEXAR`.
        self._reconfirmaciones: dict[str, int] = {}
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
        # La hoja lleva el fragmento de la densidad, así que no la puede
        # rehacer el módulo del tema: la vuelve a pedir la ventana.
        gestor_tema().cambiado.connect(self._al_cambiar_tema)

    def _aplicar_hoja(self) -> None:
        """La hoja de la ventana, con los tonos y las medidas de ahora."""
        self.setStyleSheet(
            window_stylesheet(data_table_qss() + self._densidad.qss)
        )

    def _al_cambiar_tema(self, _nombre: str) -> None:
        """Rehace la hoja de la ventana con los tonos del tema nuevo."""
        self._aplicar_hoja()

    # ── construcción ───────────────────────────────────────────────

    def _build_ui(self) -> None:
        cuerpo = QVBoxLayout(self)
        margen = max(8, self._densidad.window_margin)
        cuerpo.setContentsMargins(margen, margen, margen, margen)
        cuerpo.setSpacing(self._densidad.root_spacing)
        self._root_layout = cuerpo

        # Sin frase de bienvenida: la lista abre en «Seleccionar ejecución»
        # y eso ya dice lo que hay que hacer con ella, en el sitio donde se
        # hace. La línea de arriba solo repetía lo mismo y le quitaba alto a
        # la cola de batches, que es lo que se mira mientras trabaja.
        cuerpo.addWidget(self._historial())
        cuerpo.addLayout(self._campos())
        cuerpo.addWidget(self._recuadro_de_revision())
        self.solo_ejecucion_check = QCheckBox("Mostrar solo la ejecución seleccionada")
        self.solo_ejecucion_check.setToolTip(
            "Limita la cola y sus acciones a los batches de la ejecución seleccionada."
        )
        self.solo_ejecucion_check.toggled.connect(self._al_filtrar_ejecucion)
        cuerpo.addWidget(self.solo_ejecucion_check)
        cuerpo.addLayout(self._cabecera_de_lotes())
        cuerpo.addWidget(self._lotes(), 1)
        cuerpo.addWidget(self._respuesta_de_la_busqueda())
        cuerpo.addLayout(self._fila_vigilancia())
        cuerpo.addLayout(self._fila_avance())
        # La bitácora se queda con el alto que sobre: las dos tablas
        # tienen tope y ella es la que necesita sitio para los mensajes
        # largos.
        cuerpo.addWidget(self._bitacora(), 2)

        self.resumen = ElidedLabel(TEXTO_SIN_SUBIR)
        pintar_del_tema(
            self.resumen, lambda: f"color: {color_ayuda()};"
        )
        self.resumen.setMinimumHeight(self.resumen.fontMetrics().height() + SPACE_S)
        self.resumen.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        cuerpo.addWidget(self.resumen)

        botones = self._fila_botones()
        cuerpo.addWidget(self.detener_duplicados_check)
        cuerpo.addLayout(botones)

    @staticmethod
    def _titulo(texto: str) -> QLabel:
        etiqueta = QLabel(texto)
        etiqueta.setStyleSheet("font-weight: 600;")
        return etiqueta

    def _historial(self) -> QComboBox:
        """La lista de ejecuciones, la misma que la del visor de CSV.

        Era una tabla de tres columnas, y ese alto le hacía falta a la cola
        de batches, que es lo que se mira mientras la ventana trabaja. En
        una línea cabe lo que decía: el nombre de la ejecución, sus páginas
        y en qué quedó su entrega. Abre en «Seleccionar ejecución» para que
        la más reciente no parezca elegida antes de que nadie la elija, y es
        el único sitio desde el que se elige lo que se sube.
        """
        combo = QComboBox()
        configure_combo_box(combo, 22)
        combo.setToolTip(
            "Ejecuciones procesadas, de la más reciente a la más antigua. "
            "Solo se suben las exportadas, y solo las últimas "
            f"{LIMITE_HISTORIAL}."
        )
        combo.setAccessibleName("Ejecuciones procesadas recientes")
        # «activated» solo lo emite quien elige con el ratón o el teclado,
        # así que sincronizar la lista desde el código no se lee como que
        # alguien cambió de ejecución y tira lo hecho.
        combo.activated.connect(self._al_elegir_del_historial)
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
        registro.setToolTip(TOOLTIP_ELIMINAR_REGISTRO)
        registro.triggered.connect(lambda: self._eliminar_registro(csv))
        menu.addSeparator()
        ejecucion = menu.addAction("Eliminar la ejecución…")
        ejecucion.setToolTip(
            "Manda a la Papelera la carpeta de esta ejecución en output/. Lo "
            "que ya esté en AirVault no se toca."
        )
        ejecucion.triggered.connect(
            lambda: self._eliminar_ejecucion(csv, nombre)
        )
        return menu

    def _campos(self) -> QGridLayout:
        """Los datos de la carga, en rejilla para que se alineen.

        En filas sueltas cada etiqueta medía lo suyo y los controles
        empezaban en sitios distintos.
        """
        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACE_S)
        grid.setVerticalSpacing(self._densidad.group_spacing)
        grid.setColumnStretch(1, 1)
        etiquetas = ("Nombre del batch:", "Máximo por batch:", "Sesión:")
        for fila, etiqueta in enumerate(etiquetas):
            grid.addWidget(QLabel(etiqueta), fila, 0)

        self.lote_edit = QLineEdit()
        self.lote_edit.setPlaceholderText(
            "Se completa al elegir la ejecución"
        )
        self.lote_edit.setToolTip(
            "Nombre con el que el batch queda en AirVault. Lleva fecha y hora "
            "para no confundirlo con otro de la cola."
        )
        grid.addWidget(self.lote_edit, 0, 1, 1, 3)

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
        self.limite_batch_control.setMaximumWidth(180)
        grid.addWidget(self.limite_batch_control, 1, 1)

        self.compresion_check = QCheckBox("Compresión")
        self.compresion_check.setToolTip(
            "Envía los PDF a AirVault a 200 DPI. No cambia los PDF "
            "exportados."
        )
        grid.addWidget(self.compresion_check, 1, 2)

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
        # «Fin de mes» y «Día exacto» solos no dicen de qué son. La etiqueta
        # va pegada al desplegable y no en la columna de la izquierda, que
        # esa fila ya la ocupa «Máximo por batch:».
        fecha_fila = QHBoxLayout()
        fecha_fila.setContentsMargins(0, 0, 0, 0)
        fecha_fila.setSpacing(SPACE_S)
        fecha_label = QLabel("Fecha:")
        fecha_label.setToolTip(TOOLTIP_FECHA_INDEXADO)
        fecha_label.setBuddy(self.fecha_combo)
        fecha_fila.addWidget(fecha_label)
        fecha_fila.addWidget(self.fecha_combo, 1)
        grid.addLayout(fecha_fila, 1, 3)

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
        grid.addWidget(self.cookie_edit, 2, 1, 1, 3)
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
        recuadro.setToolTip(TOOLTIP_REPARTO)
        fila = QHBoxLayout(recuadro)
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(SPACE_L)
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
            fila.addWidget(etiqueta, 1)
        self._reparto_fila = fila
        self._mostrar_reparto(None)
        return recuadro

    def _mostrar_reparto(self, reparto: Optional[tuple[int, int]]) -> None:
        """Escribe en el recuadro el reparto de la ejecución elegida.

        Sin ejecución, o con una exportada antes de que el CSV lo dijera,
        se explica por qué no hay números en vez de enseñar tres ceros, que
        se leerían como una ejecución vacía.
        """
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
            f"{automaticas} se indexan solas ({100 - parte_revisar} %)"
        )
        self.reparto_revisar.setText(
            f"{revisar} van a REVISAR ({parte_revisar} %)"
        )

    def _cabecera_de_lotes(self) -> QHBoxLayout:
        """El título de la tabla de batches, el buscador y la vista previa.

        El buscador pregunta por una bitácora y contesta en qué batches de
        la cola está. Va aquí, encima de la tabla, porque lo que responde
        son filas de esa tabla: las de los batches que la llevan se quedan
        resaltadas y la línea de debajo dice cuáles son.
        """
        fila = QHBoxLayout()
        fila.setSpacing(SPACE_S)
        fila.addWidget(self._titulo("Cola de AirVault"))
        self.buscar_bitacora_edit = QLineEdit()
        self.buscar_bitacora_edit.setPlaceholderText(
            "Bitácora, matrícula, fecha o archivo"
        )
        self.buscar_bitacora_edit.setToolTip(AYUDA_BUSCAR_BITACORA)
        self.buscar_bitacora_edit.setAccessibleName(
            "Bitácora que se busca en la cola"
        )
        self.buscar_bitacora_edit.returnPressed.connect(self._buscar_bitacora)
        fila.addWidget(self.buscar_bitacora_edit, 1)
        self.boton_buscar_bitacora = QPushButton("Buscar")
        self.boton_buscar_bitacora.setToolTip(
            "Buscar la bitácora en la cola; repetido, pasa al batch siguiente"
        )
        self.boton_buscar_bitacora.clicked.connect(self._buscar_bitacora)
        fila.addWidget(self.boton_buscar_bitacora)
        self.buscar_bitacora_anterior = QPushButton("‹")
        self.buscar_bitacora_anterior.setToolTip(
            "Batch anterior de los que la llevan"
        )
        self.buscar_bitacora_anterior.setEnabled(False)
        self.buscar_bitacora_anterior.clicked.connect(
            lambda: self._mover_hallazgo(-1)
        )
        fila.addWidget(self.buscar_bitacora_anterior)
        self.buscar_bitacora_siguiente = QPushButton("›")
        self.buscar_bitacora_siguiente.setToolTip(
            "Batch siguiente de los que la llevan"
        )
        self.buscar_bitacora_siguiente.setEnabled(False)
        self.buscar_bitacora_siguiente.clicked.connect(
            lambda: self._mover_hallazgo(1)
        )
        fila.addWidget(self.buscar_bitacora_siguiente)
        # Ctrl+F desde cualquier punto de la ventana, como en el resto.
        QShortcut(
            QKeySequence.StandardKey.Find, self,
            activated=self.buscar_bitacora_edit.setFocus,
        )
        batch_menu = QMenu(self)
        batch_menu.setToolTipsVisible(True)
        self.boton_previa = batch_menu.addAction("Vista previa…")
        self.boton_previa.setEnabled(False)
        self.boton_previa.setToolTip(
            "Muestra cómo quedaría repartida la ejecución en batches y qué "
            "lleva cada uno. No prepara ni sube nada."
        )
        self.boton_previa.triggered.connect(self._vista_previa)
        batch_menu.addSeparator()
        self.boton_eliminar_batches = batch_menu.addAction(
            "Eliminar seleccionados…"
        )
        self.boton_eliminar_batches.setEnabled(False)
        self.boton_eliminar_batches.setToolTip(
            "Envía a la Papelera todos los batches seleccionados en la tabla. "
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
        self.batch_actions_button = QToolButton()
        self.batch_actions_button.setText("Acciones")
        configure_menu_button(self.batch_actions_button, batch_menu)
        fila.addWidget(self.batch_actions_button)
        return fila

    def _vista_previa(self) -> None:
        """Calcula el reparto sin tocar nada y lo enseña.

        Hasta que se sube no hay ningún batch que mirar, y el reparto solo
        se sabía después de haberlo hecho. Esto responde antes la misma
        pregunta: con qué nombre y con cuántas páginas saldría cada batch,
        y qué bitácoras van dentro.
        """
        from app.airvault.flujo import (ErrorDeCorrida, carpeta_de_corrida,
                                        carpeta_de_trabajo,
                                        previsualizar_reparto)
        from app.airvault.mapping import FLOTA_CACHE_FILENAME, ResolutorFlota
        from app.gui.airvault_previa import VistaPreviaBatches

        csv = self._corrida.strip()
        if not csv:
            return
        carpeta = self._raiz / carpeta_de_trabajo(
            carpeta_de_corrida(csv).name
        )
        try:
            previstos = previsualizar_reparto(
                self._config_actual(),
                carpeta,
                Path(csv),
                self.lote_edit.text().strip(),
                resolutor=ResolutorFlota.load(
                    self._raiz / FLOTA_CACHE_FILENAME
                ),
                paginas_por_batch=self.limite_batch_spin.value(),
                compresion=self.compresion_check.isChecked(),
                fin_de_mes=self.fin_de_mes(),
            )
        except (ErrorDeCorrida, OSError, ValueError) as error:
            QMessageBox.warning(self, "Vista previa", str(error))
            return
        if not previstos:
            QMessageBox.information(
                self,
                "Vista previa",
                "Esta ejecución no deja ningún batch: todas sus bitácoras "
                "viajaron ya a AirVault.",
            )
            return
        self._abrir_ventana(
            VistaPreviaBatches(previstos, csv=csv, parent=self)
        )

    def _lotes(self) -> QTableWidget:
        """En qué va cada batch de esta ejecución dentro de AirVault.

        Una entrega puede ser varios batches (las partes, y el de REVISAR), y
        no llegan a estar listos a la vez: AirVault los procesa en su cola.
        Aquí se ve cuál ya se puede indexar y cuál sigue esperando, en vez
        de una sola línea de estado que solo puede decir una cosa.
        """
        tabla = QTableWidget(0, 4)
        tabla.setHorizontalHeaderLabels(
            ["ID", "Batch", "Páginas", "Estado"]
        )
        tabla.setToolTip(
            "Batches de esta ejecución y pendientes de otras. Pasan a «Listo "
            "para indexar» cuando el servidor termina. Con Ctrl o Mayúsculas "
            "se eligen varios."
        )
        # Varios batches se mandan a la cola de una vez: con Ctrl o
        # Mayúsculas se eligen las filas y la acción vale para todas.
        tabla.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        tabla.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tabla.setAlternatingRowColors(True)
        tabla.verticalHeader().setVisible(False)
        cabecera = tabla.horizontalHeader()
        cabecera.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        cabecera.setSectionResizeMode(
            1, QHeaderView.ResizeMode.Interactive
        )
        cabecera.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        cabecera.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        tabla.setColumnWidth(1, ANCHO_MINIMO_NOMBRE_BATCH)
        # La tabla es la cola de trabajo: cada fila se puede reintentar,
        # indexar, cerrar o sacar de la cola sin tocar a las demas.
        tabla.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        tabla.customContextMenuRequested.connect(self._menu_de_la_cola)
        tabla.itemSelectionChanged.connect(
            self._actualizar_eliminar_seleccionados
        )
        self._ajustar_tabla(tabla)
        self.lotes = tabla
        return tabla

    def _respuesta_de_la_busqueda(self) -> QLabel:
        """La línea que dice en qué batches de la cola está la bitácora."""
        etiqueta = QLabel(AYUDA_BUSCAR_BITACORA)
        etiqueta.setWordWrap(True)
        pintar_del_tema(etiqueta, lambda: f"color: {color_ayuda()};")
        # Sitio para dos líneas: nombrar varios batches con sus páginas no
        # cabe en una, y sin reservarlo la ventana daba un salto al buscar.
        etiqueta.setMinimumHeight(etiqueta.fontMetrics().lineSpacing() * 2)
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
        self._resaltar_hallazgos()

    # ── la cola: cada fila por separado ────────────────────────────

    def _menu_de_la_cola(self, punto) -> None:
        """Abre el menú de los batches sobre los que se hizo clic derecho.

        Si la fila del clic ya estaba entre las elegidas, la acción vale
        para toda la selección; si no, la selección pasa a ser esa fila.
        Es lo que hace cualquier lista, y evita actuar sobre batches que no
        se están mirando.
        """
        fila = self.lotes.rowAt(punto.y())
        if fila < 0 or fila >= len(self._partes_en_cola()):
            return
        menu = self._acciones_de_la_cola(self._elegidas(fila))
        menu.exec(self.lotes.viewport().mapToGlobal(punto))

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
        trabajos = [
            trabajo for trabajo in trabajos
            if not getattr(trabajo.manifiesto, "cancelado", False)
        ]
        if not trabajos:
            return
        if self.hilo() is not None:
            self._cola_de_acciones.append((modo, trabajos))
            pendientes = len(self._cola_de_acciones)
            plural = "acciones" if pendientes != 1 else "acción"
            self._anotar(f"{texto}: en cola, {pendientes} {plural} esperando")
            return
        self._anotar(texto)
        self._ejecutar_accion(modo, trabajos)

    def _ejecutar_accion(self, modo: str, trabajos) -> bool:
        """Prepara el estado que pide cada modo y arranca el hilo."""
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
            planes = estado.get("planes") or {}
            listos = [
                trabajo for trabajo in trabajos
                if str(trabajo.carpeta) in planes
            ]
            if not listos:
                return False
            estado["listos"] = listos
            estado["indexar_acotado"] = True
        elif modo == "comprobar":
            # El menu contextual pone esta lista. El boton inferior no la
            # pone y por eso conserva el alcance global sobre toda la tabla.
            estado["comprobar_trabajos"] = list(trabajos)
        elif modo == "completar":
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
            f"Esto subiría {cuantas} páginas que quedarían amarillas en "
            "AirVault: les falta algún campo obligatorio y habría que "
            "completarlas a mano en Web Index."
        )
        dialogo.setInformativeText(
            f"{detalle}\n\nLo limpio es volver a exportar la ejecución para "
            "que esas bitácoras vayan al batch REVISAR. Si el archivo ya "
            "está hecho y prefiere subirlo así, se sube y esas páginas se "
            "terminan a mano."
        )
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
            f"Se cierra {nombres}",
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
                f"- {ruta.name}: {error}" for ruta, error in fallidos
            )
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

    def _ajustar_tabla(self, tabla: QTableWidget) -> None:
        """Deja la cola con su alto y la barra debajo de la cabecera."""
        style_data_table(tabla)
        tabla.setMinimumHeight(self._densidad.airvault_table_min_height)
        tabla.setMaximumHeight(360)
        align_vertical_scrollbar_to_header(tabla)

    def _fila_vigilancia(self) -> QHBoxLayout:
        """Cada cuánto se pregunta automáticamente a AirVault."""
        fila = QHBoxLayout()
        fila.setSpacing(SPACE_S)
        self.auto_check = QCheckBox("Revisar cada")
        # Esperar a que AirVault los deje listos va dentro de «Subir a
        # AirVault» y no se elige aparte; esta casilla no decide si se
        # espera, sino cada cuánto se pregunta, y vale mientras la ventana
        # esté abierta: apagarla deja la cadena parada aquí, y la línea de
        # pasos de la ventana principal lo enseña en rojo.
        self.auto_check.setChecked(True)
        self.auto_check.setToolTip(
            "Pregunta a AirVault cada tantos minutos si ya terminó lo subido. "
            "Apagado, hay que pulsar «Revisar en AirVault»."
        )
        self.auto_check.toggled.connect(self._ajustar_vigilancia)
        fila.addWidget(self.auto_check)

        self.minutos_spin = QSpinBox()
        self.minutos_spin.setRange(1, 60)
        self.minutos_spin.setValue(MINUTOS_POR_DEFECTO)
        self.minutos_spin.setSuffix(" min")
        self.minutos_spin.setToolTip(
            "Cada cuánto se le pregunta a AirVault. Preguntar más seguido no "
            "apura la cola."
        )
        self.minutos_spin.valueChanged.connect(self._ajustar_vigilancia)
        self.minutos_control = SpinBoxWithButtons(self.minutos_spin)
        fila.addWidget(self.minutos_control)

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
        fila.addWidget(self.boton_automatizacion)

        # Continuar y reiniciar vivían escondidos detrás de «Automatización…»,
        # junto a unas casillas que ahora son un menú. Son acciones de esta
        # ventana, no ajustes, así que se quedan a la vista: son lo que se
        # pulsa cuando un batch quedó a medias.
        self.boton_continuar = QPushButton("Continuar pendiente")
        self.boton_continuar.setToolTip(
            "Continúa desde el primer paso sin terminar; no repite las "
            "páginas en verde."
        )
        self.boton_continuar.clicked.connect(self._continuar_pendiente)
        fila.addWidget(self.boton_continuar)

        self.boton_reiniciar = QPushButton("Reiniciar paso incompleto")
        self.boton_reiniciar.setToolTip(
            "Reinicia el estado local del batch elegido, o de todos los "
            "incompletos si no hay ninguno. No borra nada en AirVault."
        )
        self.boton_reiniciar.clicked.connect(self._reiniciar_incompleto)
        fila.addWidget(self.boton_reiniciar)
        fila.addStretch()
        return fila

    def _al_cambiar_automatizacion(self, paso: str, marcado: bool) -> None:
        """Refleja lo que se eligió en la ventana principal.

        La casilla que esta ventana sigue enseñando («Completar batch») es
        el mismo ajuste que el de allá, así que se mueve sola cuando se
        toca el otro lado.
        """
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
        """Barra y reloj propios; el detalle vive en la bitácora inferior."""
        fila = QHBoxLayout()
        self.estado_label = ElidedLabel("", parent=self)
        self.estado_label.hide()
        self.progreso = QProgressBar()
        self.progreso.setRange(0, 100)
        self.progreso.setValue(0)
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
        lista.setToolTip("Lo que el indexado va haciendo, con la hora de cada paso")
        # Sin tope y con suelo: un mensaje largo se envuelve en varias
        # líneas y con 110 px fijos solo se veía el principio.
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

    def _fila_botones(self) -> QHBoxLayout:
        fila = QHBoxLayout()
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(SPACE_S)

        self.completar_check = QCheckBox("Completar batch")
        self.completar_check.setChecked(self._opciones.completar)
        self.completar_check.setToolTip(
            "Al terminar de escribir, da el batch por terminado y lo manda a "
            "Web Search. Solo se acepta con todas las páginas en verde."
        )
        self.completar_check.toggled.connect(
            lambda marcado: self._opciones.fijar(COMPLETAR, marcado)
        )
        fila.addWidget(self.completar_check)
        self.detener_duplicados_check = QCheckBox("Detener subida si se detectan duplicados")
        self.detener_duplicados_check.setChecked(self._config.detener_por_duplicados)
        self.detener_duplicados_check.setToolTip("Desmarcada: avisa de posibles duplicados y continúa. La selección se recuerda al cerrar.")
        self.detener_duplicados_check.toggled.connect(self._guardar_politica_duplicados)
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
            "Comprueba en AirVault el nombre y las páginas de cada batch. "
            "Solo los confirmados se pueden indexar."
        )
        self.boton_revisar.clicked.connect(self._comprobar)
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

    def _acotar_a_la_pantalla(self) -> None:
        """Impide que el contenido exija más ancho del que hay.

        El layout pide de mínimo lo que suman sus controles puestos en fila,
        y la fila de botones de abajo sola pide más de 1200 px. Qt aplica ese
        mínimo por encima del tamaño con el que la ventana se abrió, así que
        la ventana crecía sola: en una pantalla de 1366 quedaba pegada a los
        bordes y por debajo se salía, con los botones fuera del alcance.

        Aquí se acota ese mínimo a lo que da el escritorio. Lo que no quepa
        se recorta, que es preferible a mandar media ventana a donde no se
        puede llegar con el ratón. Se recalcula al mostrarla porque la
        pantalla puede no ser la misma que la última vez.
        """
        disponible = available_area(self)
        pedido = self.minimumSizeHint()
        self.setMinimumSize(
            max(ANCHO_MINIMO_VENTANA, min(pedido.width(), disponible.width())),
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
                "No hay ejecuciones procesadas. Procese y exporte una "
                "ejecución para subirla a AirVault."
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
        if csv == self._corrida:
            return
        if self.hilo() is not None:
            self.abrir_corrida_paralela.emit(str(csv))
            self._marcar_en_historial(self._corrida)
            return
        self.fijar_corrida(csv)

    def _marcar_en_historial(self, csv: Path | str) -> None:
        """Deja elegida en la lista la ejecución abierta, si está en ella."""
        combo = self.historial
        texto = str(csv).strip()
        clave = str(Path(texto)).casefold() if texto else ""
        with QSignalBlocker(combo):
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

    def fijar_corrida(self, csv: Path | str) -> None:
        """Apunta la ventana a una ejecución y propone el nombre del batch."""
        from app.airvault.flujo import carpeta_de_corrida, carpeta_de_trabajo
        from app.airvault.naming import nombre_desde_corrida

        # Cambiar de ejecución tira lo hecho, y con ello los batches que
        # hubieran quedado tomados en AirVault: sin soltarlos quedan
        # colgados para quien los abra después.
        self._soltar_lotes()
        self._parar_vigilancia()
        ruta = Path(csv)
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
                                        cargar_partes,
                                        cargar_trabajos_pendientes,
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
        conocidos = {
            str(trabajo.carpeta.resolve()).casefold()
            for trabajo in self._trabajos
        }
        salida_local = (self._raiz / "output").resolve()
        pendientes_globales = (
            cargar_trabajos_pendientes(
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
        compresiones = {t.manifiesto.compresion for t in trabajos_de_corrida}
        if len(compresiones) == 1:
            self.compresion_check.setChecked(compresiones.pop())
        fechas = {t.manifiesto.fin_de_mes for t in trabajos_de_corrida}
        if len(fechas) == 1:
            indice = self.fecha_combo.findData(fechas.pop())
            if indice >= 0:
                with QSignalBlocker(self.fecha_combo):
                    self.fecha_combo.setCurrentIndex(indice)
        # Se pueden cambiar aunque la ejecucion ya tenga batches: lo que ya
        # esta en AirVault se conserva y solo se reparte lo que falta.
        self.limite_batch_spin.setEnabled(self.hilo() is None)
        self.compresion_check.setEnabled(self.hilo() is None)
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
                "Se eliminaron los batches locales y no se reconstruirán al "
                "reiniciar. Los batches remotos no se modificaron."
            )
        for carpeta_anotada, grupo in registros.items():
            if movidos_set.intersection(grupo):
                self._anotar(
                    "Registro local de AirVault eliminado: "
                    f"{carpeta_anotada.name}"
                )

        if fallidos:
            detalle = "\n".join(
                f"- {ruta.name}: {error}" for ruta, error in fallidos
            )
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
        if self._es_la_ejecucion_abierta(csv) and self.hilo() is not None:
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
                f"- {ruta.name}: {error}" for ruta, error in fallidos
            )
            QMessageBox.warning(
                self,
                "No se pudo eliminar la ejecución",
                "No se pudieron enviar a la Papelera:\n" + detalle,
            )
        if carpeta not in movidos:
            return

        self._anotar(f"Ejecución eliminada: {nombre}")
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
                "La ejecución no tiene ningún PDF de entrega. Expórtela antes "
                "de subirla a AirVault."
            )
        else:
            self.resumen.setText(
                "La ejecución se exportó antes de que existiera el índice de "
                "páginas. Vuelva a exportarla para poder indexarla."
            )

    # ── la lista de batches ──────────────────────────────────────────

    def _filtrar_trabajos(self, trabajos) -> list:
        if not self.solo_ejecucion_check.isChecked():
            return list(trabajos)
        return [
            trabajo for trabajo in trabajos
            if self._es_la_ejecucion_abierta(trabajo.manifiesto.csv_origen)
        ]

    def _partes_en_cola(self) -> list:
        visibles = {id(t) for t in self._filtrar_trabajos(
            parte.trabajo for parte in self._estados
        )}
        return [parte for parte in self._estados if id(parte.trabajo) in visibles]

    def _al_filtrar_ejecucion(self, _marcado: bool) -> None:
        self._pintar_lotes()
        self._ajustar_vigilancia()
        self._habilitar(self.hilo() is None)

    def _recibir_trabajos(self, trabajos) -> None:
        """Conserva las ejecuciones ocultas cuando termina una accion filtrada."""
        nuevos = list(trabajos)
        if self.solo_ejecucion_check.isChecked():
            claves = {str(t.carpeta) for t in nuevos}
            nuevos.extend(
                t for t in self._trabajos
                if not self._es_la_ejecucion_abierta(t.manifiesto.csv_origen)
                and str(t.carpeta) not in claves
            )
        self._trabajos = nuevos

    def _pintar_lotes(self) -> None:
        """Vuelca en la tabla en qué va cada batch."""
        from app.airvault.flujo import (AUTOCOMPLETADO, CANCELADO, COMPLETADO,
                                        INDEXADO, INCOMPLETO,
                                        POSIBLE_DUPLICADO, SIN_SUBIR)

        tabla = self.lotes
        tabla.setRowCount(0)
        for parte in self._partes_en_cola():
            fila = tabla.rowCount()
            tabla.insertRow(fila)
            nombre = parte.nombre or "(sin nombre)"
            esperadas = len(parte.trabajo.manifiesto.registros)
            celdas = (
                parte.batch_id, nombre, str(esperadas), str(parte),
            )
            completado = parte.estado in (COMPLETADO, AUTOCOMPLETADO)
            for columna, texto in enumerate(celdas):
                item = QTableWidgetItem(texto)
                if columna == 1:
                    item.setToolTip(nombre)
                if columna == 2:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter
                    )
                if completado:
                    pintar_celda_del_tema(item, "STATUS_OK")
                elif (str(parte.trabajo.carpeta) in self._indexando
                      or parte.estado in (INDEXADO, INCOMPLETO)):
                    pintar_celda_del_tema(item, "acento")
                elif parte.estado in (
                    SIN_SUBIR, CANCELADO, POSIBLE_DUPLICADO
                ):
                    # Gris es lo que la cola no va a mover por su cuenta:
                    # el archivo que Quick Upload aun no acepto, el batch
                    # que alguien sacó de la cola y el que se paró por
                    # parecerse a algo ya subido. Desde que se sube queda
                    # en blanco, incluso mientras AirVault lo procesa.
                    pintar_celda_del_tema(item, "TEXT_TERTIARY")
                tabla.setItem(fila, columna, item)
        ancho_nombre = min(
            max(
                tabla.sizeHintForColumn(1) + 16,
                ANCHO_MINIMO_NOMBRE_BATCH,
            ),
            ANCHO_MAXIMO_NOMBRE_BATCH,
        )
        tabla.setColumnWidth(1, ancho_nombre)
        # La tabla se acaba de rehacer entera: la bitácora que se estaba
        # buscando perdió su resaltado y hay que devolvérselo.
        self._rehacer_la_busqueda()

    def _listos(self) -> list:
        """Partes que ya se pueden escribir y tienen su plan calculado."""
        planes = self._estado.get("planes") or {}
        return [
            parte.trabajo for parte in self._partes_en_cola()
            if parte.se_puede_indexar and str(parte.trabajo.carpeta) in planes
        ]

    def _por_completar(self) -> list:
        """Batches verificados que pueden cerrarse sin volver a escribir."""
        from app.airvault.flujo import INDEXADO

        return [
            parte.trabajo for parte in self._partes_en_cola()
            if parte.estado == INDEXADO and not parte.trabajo.manifiesto.solo_subir
        ]

    def _ejecucion(self) -> list:
        """Todas las partes de la ejecución, tal como están en la tabla.

        Dar una carga por perdida depende de las demás: son las partes
        siguientes, ya indexadas, las que demuestran que AirVault pasó de
        largo. Ninguna regla puede decidirlo mirando una sola fila.
        """
        return [parte.trabajo for parte in self._partes_en_cola()]

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
        """
        from app.airvault.flujo import INDEXADO, INCOMPLETO, POSIBLE_DUPLICADO

        return any(
            (not parte.se_acabo
             or (parte.estado == INDEXADO and self.completar_check.isChecked()
                 and not parte.trabajo.manifiesto.solo_subir))
            and parte.estado != POSIBLE_DUPLICADO
            and (
                not parte.se_puede_indexar
                or parte.estado == INCOMPLETO
                or self._reconfirmaciones.get(str(parte.trabajo.carpeta), 0) > 0
            )
            for parte in self._partes_en_cola()
        )

    def _subidas_perdidas(self) -> list:
        """Cargas que AirVault aceptó y ya se pueden dar por no publicadas.

        O bien las partes siguientes ya se indexaron, o bien el archivo
        lleva subido más tiempo del que AirVault tarda en publicar.
        """
        from app.airvault.flujo import subida_perdida

        ejecucion = self._ejecucion()
        return [
            parte for parte in self._partes_en_cola()
            if subida_perdida(parte, ejecucion)
        ]

    def _sin_subir_todavia(self) -> list:
        """Batches que la comprobación periódica va a mandar sola.

        Solo los que nunca llegaron a Quick Upload. Una carga que AirVault
        aceptó y no publicó no vuelve a salir sola por mucho que tarde: se
        avisa y la manda quien mire Web Index.
        """
        from app.airvault.flujo import partes_por_subir

        if not self.auto_check.isChecked():
            return []
        return [
            trabajo for trabajo in partes_por_subir(self._partes_en_cola())
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
        return (
            f" Ojo: {cuantos} ({nombres}); el motivo de cada uno está en la "
            "bitácora de aquí abajo."
        )

    def _aviso_para_subir_a_mano(self) -> str:
        """Recuerda que la espera se puede saltar cuando no hay batch.

        La espera es una suposición del programa; quien tiene Web Index
        delante sabe más que él. Si ya miró y el batch no está, no tiene por
        qué esperar a que venza ningún reloj.
        """
        if not any(parte.se_puede_subir for parte in self._partes_en_cola()):
            return ""
        return (
            " Si ya miró la cola de AirVault y el batch no está, no espere: "
            "clic derecho sobre su fila y «Subir a AirVault ahora». Se envía "
            "en el momento, sin volver a revisar nada."
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
            razon = (
                "Se recorrió la cola entera y no están con ningún nombre, "
                "ni por páginas ni por Log Page Number."
            )
        elif len(rebasadas) == len(perdidas):
            razon = (
                "Las partes que se enviaron después ya están indexadas, "
                "así que la cola pasó de largo."
            )
        elif sin_hallar or rebasadas:
            razon = (
                "Unos no están en la cola con ningún nombre, de otros ya se "
                "indexaron las partes siguientes y de los demás pasó el "
                f"tiempo de espera de {minutos} minutos."
            )
        else:
            razon = (
                f"Ya pasó el tiempo de espera de {minutos} minutos y es "
                "probable que la carga no vaya a aparecer."
            )
        cabeza = f" AirVault no publicó en Web Index: {nombres}. {razon}"
        varios = len(perdidas) > 1
        sigue = (
            (
                " Se sigue mirando la cola por si AirVault acaba "
                "publicándolos; si aparecen, se indexan solos."
                if varios else
                " Se sigue mirando la cola por si AirVault acaba "
                "publicándolo; si aparece, se indexa solo."
            )
            if self.auto_check.isChecked() else ""
        )
        no_se_mandan = (
            "No se vuelven a mandar solos" if varios
            else "No se vuelve a mandar solo"
        )
        mirelos = (
            "Mírelos en Web Index y, si de verdad no están" if varios
            else "Mírelo en Web Index y, si de verdad no está"
        )
        return (
            f"{cabeza} {no_se_mandan}: insistir con el mismo archivo es como "
            f"acaban varias copias del mismo batch en la cola.{sigue} "
            f"{mirelos}, clic derecho sobre su fila y «Subir a AirVault "
            "ahora»."
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
        if not self.auto_check.isChecked() or not self._falta_esperar():
            self._parar_vigilancia()
            return
        if self._vigilante is None:
            self._vigilante = QTimer(self)
            self._vigilante.timeout.connect(self._comprobar_solo)
        self._vigilante.setInterval(self.minutos_spin.value() * 60_000)
        self._vigilante.start()

    def _parar_vigilancia(self) -> None:
        if self._vigilante is not None:
            self._vigilante.stop()

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
            if clave not in self._reconfirmaciones:
                continue
            if parte.estado == INCOMPLETO:
                self._reconfirmaciones[clave] = max(
                    0, self._reconfirmaciones[clave] - 1
                )
            else:
                del self._reconfirmaciones[clave]

    def _comprobar_solo(self) -> None:
        """Lo que dispara el reloj. Se salta el turno si hay algo en vuelo."""
        if self.hilo() is not None:
            return
        # Cada vuelta del reloj vuelve a dar permiso de subida: lo que no
        # se pudo subir hace cinco minutos se intenta otra vez ahora.
        self._subidas_del_ciclo.clear()
        self._comprobar()

    # ── acciones ───────────────────────────────────────────────────

    def _config_actual(self):
        return self._config

    def _guardar_politica_duplicados(self, marcada: bool) -> None:
        from app.airvault.config import guardar_preferencias
        from app.airvault.flujo import POSIBLE_DUPLICADO, estado_local
        if not guardar_preferencias(self._raiz / AIRVAULT_FILENAME, detener_por_duplicados=marcada):
            with QSignalBlocker(self.detener_duplicados_check):
                self.detener_duplicados_check.setChecked(self._config.detener_por_duplicados)
            self.resumen.setText("No se pudo guardar la opción de duplicados.")
            return
        self._config = self._config.with_overrides(detener_por_duplicados=marcada)
        self._estado["config"] = self._config
        trabajos = list(self._trabajos) + [p.trabajo for p in self._estados]
        for trabajo in trabajos:
            trabajo.config = trabajo.config.with_overrides(detener_por_duplicados=marcada)
        self._estados = [estado_local(p.trabajo) if p.estado == POSIBLE_DUPLICADO else p for p in self._estados]
        self._pintar_lotes()
        self._ajustar_vigilancia()

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
            self.resumen.setText(
                "Falta elegir la ejecución que se va a indexar."
            )
            return None
        if not self.lote_edit.text().strip():
            self.resumen.setText("Falta el nombre del batch en AirVault.")
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
            "compresion": self.compresion_check.isChecked(),
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
        sospechosos = [
            parte for parte in self._partes_en_cola()
            if parte.trabajo.manifiesto.posible_duplicado
        ]
        if sospechosos:
            self._quitar_sospecha(sospechosos)
            return
        self._recuperar_pendientes = True
        self._subir()

    def _subir(self) -> None:
        estado = self._base_del_estado()
        if estado is None:
            return
        self._subidas_del_ciclo.clear()
        self._lanzar("subir", estado)

    def _comprobar(self) -> None:
        estado = self._base_del_estado()
        if estado is None:
            return
        estado.pop("comprobar_trabajos", None)
        self._lanzar("comprobar", estado)

    def _indexar(self) -> None:
        self._estado.pop("indexar_acotado", None)
        self._estado.pop("completar_acotado", None)
        # Una ejecucion parcial no puede adelantarse: aunque ya haya un batch
        # listo, primero se terminan todas las cargas. Este es tambien el
        # camino de recuperacion para trabajos que quedaron a medias.
        if any(
            not trabajo.manifiesto.etapa_hecha("subir")
            for trabajo in self._filtrar_trabajos(self._trabajos)
        ):
            self._continuar_pendiente()
            return
        listos = self._listos()
        if not listos:
            por_completar = self._por_completar()
            if self.completar_check.isChecked() and por_completar:
                self._estado["por_completar"] = por_completar
                self._lanzar("completar", self._estado)
                return
            if self._corrida.strip():
                # Conecta y detecta tambien batches que esta aplicacion subio
                # en ejecuciones anteriores. Despues decide si hay que
                # reindexar, continuar o solamente comprobar su estado.
                self._indexar_al_terminar = True
                self._comprobar()
            return
        self._estado["listos"] = listos
        self._estado["completar"] = self.completar_check.isChecked()
        self._lanzar("indexar", self._estado)

    def _continuar_pendiente(self) -> None:
        """Retoma el primer paso necesario sin duplicar trabajo terminado."""
        from app.airvault.model import EstadoEtapa

        self._subidas_del_ciclo.clear()
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
        if self._worker is not None and self._worker.isRunning():
            return
        if self.solo_ejecucion_check.isChecked():
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
        worker.indexado.connect(self._al_indexar)
        worker.fallo.connect(self._al_fallar)
        worker.cancelado.connect(self._al_cancelar)
        worker.finished.connect(self._al_terminar)
        self._worker = worker
        self._arrancar_reloj()
        worker.start()
        # Con el hilo ya en marcha, para que la línea de pasos de la ventana
        # principal pase a «en curso» al empezar y no al terminar.
        self._publicar_avance()

    def _habilitar(self, activo: bool) -> None:
        self.detener_duplicados_check.setEnabled(activo)
        self.completar_check.setEnabled(activo)
        self.solo_ejecucion_check.setEnabled(activo)
        # La ejecución de esta ventana no cambia mientras trabaja, pero el
        # historial sigue disponible: elegir otra emite una solicitud para
        # abrirla en su propia ventana y su propio hilo.
        self.historial.setEnabled(True)
        self.boton_subir.setEnabled(activo and self._listo_para_subir)
        self.boton_eliminar_registro.setEnabled(
            activo and bool(self._registros_presentes())
        )
        self.lote_edit.setEnabled(activo)
        self.cookie_edit.setEnabled(activo)
        self.limite_batch_spin.setEnabled(activo)
        self.compresion_check.setEnabled(activo)
        self.fecha_combo.setEnabled(activo)
        self.boton_comprobar.setEnabled(activo and bool(self._trabajos))
        # La vista previa solo lee el disco, pero mientras el hilo reparte
        # los manifiestos están a medio escribir y enseñarlos engaña.
        self.boton_previa.setEnabled(
            activo and bool(self._corrida.strip())
        )
        self.boton_automatizacion.setEnabled(activo)
        self.boton_continuar.setEnabled(activo)
        self.boton_reiniciar.setEnabled(activo and bool(self._trabajos))
        # Cerrar y Cancelar nunca se apagan a la vez: mientras hay trabajo
        # en vuelo tiene que haber siempre algo que pulsar, o la ventana se
        # queda muda durante una espera de minutos.
        self.boton_cancelar.setEnabled(not activo)
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
        segundos = int(time.monotonic() - self._inicio_paso)
        self.reloj_label.setText(f"{segundos // 60:d}:{segundos % 60:02d}")

    # ── respuestas del hilo ────────────────────────────────────────

    def _mostrar_paso(self, texto: str, hechas: int, total: int) -> None:
        self.estado_label.setText(texto)
        self.estado_label.setToolTip(texto)
        if total > 0:
            self.progreso.setRange(0, total)
            self.progreso.setValue(min(hechas, total))
        else:
            # Sin cuenta, la barra va en marcha continua: parada en cero se
            # lee como que no está pasando nada.
            self.progreso.setRange(0, 0)
        # Los avisos de una subida llegan por trozo (mil en una entrega
        # grande), así que solo se anota cuando cambia el texto: la
        # bitácora cuenta por qué etapa va, no cuántas veces avisó.
        if texto != self._ultimo_paso:
            self._ultimo_paso = texto
            self._inicio_paso = time.monotonic()
            self._anotar(texto)

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
        self.bitacora.addItem("\n".join(lineas))
        while self.bitacora.count() > LIMITE_BITACORA:
            self.bitacora.takeItem(0)
        self.bitacora.scrollToBottom()

    def _al_subir(self, datos: dict) -> None:
        self._al_actualizar_subidas(datos)
        cuantos = len(self._trabajos)
        lotes = "el batch" if cuantos == 1 else f"los {cuantos} batches"
        fallos = datos.get("fallos") or []
        self.resumen.setText(
            f"Subida terminada. AirVault tiene que procesar {lotes} antes "
            f"de poder indexarlos, y eso tarda: se va preguntando solo."
            + self._aviso_de_cargas_fallidas(fallos)
        )
        self.estado_label.setText("Subida terminada")
        self._anotar(
            "Subida terminada; se confirma cada batch en AirVault"
        )
        # El motivo de cada carga que no salió. Antes solo quedaba en el
        # archivo de registro, así que la ventana decía «subida terminada»
        # de archivos que nunca se enviaron y nadie sabía por qué.
        for nombre, detalle in fallos:
            self._anotar(f"No se subió «{nombre}»: {detalle}")
        # «Subir» no confia en la marca local: en cada clic consulta la cola
        # remota, recupera el ID que falte y solo entonces deja indexar. La
        # opcion de espera automatica decide si se seguira preguntando cuando
        # AirVault aun lo procese, pero nunca elimina esta primera comprobacion.
        self._comprobar_al_terminar = True

    def _al_actualizar_subidas(self, datos: dict) -> None:
        """Actualiza el estado interno antes de buscar los IDs."""
        from app.airvault.flujo import estado_local

        self._recibir_trabajos(datos["trabajos"])
        self._estado["trabajos"] = self._filtrar_trabajos(self._trabajos)
        self._estados = [estado_local(t) for t in self._trabajos]
        self._pintar_lotes()

    def _al_batch_encontrado(self, datos: dict) -> None:
        """Muestra el ID apenas se resuelve, sin esperar las otras búsquedas."""
        from app.airvault.flujo import estado_local

        self._recibir_trabajos(datos["trabajos"])
        self._estado["trabajos"] = self._filtrar_trabajos(self._trabajos)
        remoto = datos["estado"]
        self._estados = [estado_local(t) for t in self._trabajos]
        clave = str(remoto.trabajo.carpeta)
        for indice, parte in enumerate(self._estados):
            if str(parte.trabajo.carpeta) == clave:
                self._estados[indice] = remoto
                break
        self._pintar_lotes()
        self._anotar(
            f"Batch {remoto.batch_id} asignado a «{remoto.nombre}»"
        )

    def _al_batch_indexando(self, trabajo, activo: bool) -> None:
        clave = str(trabajo.carpeta)
        if activo:
            self._indexando.add(clave)
        else:
            self._indexando.discard(clave)
        self._pintar_lotes()

    def _al_batch_indexado(self, datos: dict) -> None:
        """Pinta en verde un batch terminado por el carril paralelo."""
        from app.airvault.flujo import estado_local

        trabajo = datos["trabajo"]
        self._estados = [estado_local(t) for t in self._trabajos]
        if datos.get("incompleto"):
            self._programar_reconfirmaciones(datos.get("carpetas"))
            self._ajustar_vigilancia()
        self._pintar_lotes()
        resultado = datos["resultado"]
        self._anotar(
            f"Batch {trabajo.manifiesto.batch_id} indexado: "
            f"{resultado.escritas} escritas, {resultado.fallidas} fallidas"
        )
        self.resumen.setText(
            f"El batch {trabajo.manifiesto.nombre_batch} ya se indexó "
            f"({datos['validas']} de {datos['total']} páginas revisadas). "
            "La cola continúa con los demás batches."
        )

    def _al_comprobar(self, datos: dict) -> None:
        # Una comprobación buena borra la racha: lo que llevara fallando
        # dejó de fallar.
        self._fallos_seguidos = 0
        self._estado["planes"] = datos["planes"]
        self._recibir_trabajos(self._estado.get("trabajos") or self._trabajos)
        acotado = bool(datos.get("acotado"))
        revisados = list(datos["estados"])
        if acotado or self.solo_ejecucion_check.isChecked():
            por_carpeta = {
                str(parte.trabajo.carpeta): parte for parte in revisados
            }
            self._estados = [
                por_carpeta.get(str(parte.trabajo.carpeta), parte)
                for parte in self._estados
            ]
        else:
            self._estados = revisados
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
                "Faltan por subir; se envían ahora:",
                [t.manifiesto.nombre_batch for t in sin_subir],
            )
        if listos:
            self.resumen.setText(
                self._resumen_de_listos(datos["partes"])
                + ("" if acotado else self._aviso_para_volver_a_subir())
            )
        elif acotado:
            detalle = "; ".join(f"{p.nombre}: {p}" for p in revisados)
            self.resumen.setText(
                f"Revisión terminada para la selección. {detalle}. "
                "No se seguirá revisando automáticamente."
            )
        elif self._subidas_perdidas():
            self.resumen.setText(
                self._aviso_para_volver_a_subir().lstrip()
            )
        elif self._falta_esperar():
            pendientes = ", ".join(
                f"{p.nombre}: {p}" for p in self._estados if not p.se_acabo
            )
            self.resumen.setText(
                f"AirVault todavía no ha terminado. {pendientes}. Se vuelve "
                f"a preguntar solo cada {self.minutos_spin.value()} min."
                + self._aviso_para_subir_a_mano()
            )
        else:
            incluye_revision = any(
                p.trabajo.manifiesto.solo_subir for p in self._estados
            )
            self.resumen.setText(
                "El proceso automático terminó. Los datos disponibles de REVISAR "
                "están guardados; sus incidencias se resuelven manualmente."
                if incluye_revision else
                "No queda nada pendiente en AirVault para esta ejecución."
            )
            self.estado_label.setText("Proceso automático terminado")
        self._anotar(
            "Revisado:",
            [f"{p.nombre}: {p}" for p in revisados],
        )
        self._limpiar_progreso()
        if not acotado and self._opciones.indexar and (
            self._listos()
            or (
                self.completar_check.isChecked()
                and self._por_completar()
            )
        ):
            self._indexar_al_terminar = True

    def _resumen_de_listos(self, partes) -> str:
        """Cuántas páginas se escribirían y cuántas quedan bloqueadas."""
        from app.airvault.report import _resumen_sumado

        if not partes:
            return "Hay batches listos; falta calcular qué se les escribiría."
        resumen = _resumen_sumado(partes)
        donde = (
            f"{len(partes)} batches" if len(partes) > 1
            else f"Batch {partes[0][0] or partes[0][1].batch_id}"
        )
        return (
            f"{donde}: {resumen['total']} páginas, "
            f"{resumen['escribibles']} se escribirían y "
            f"{resumen['bloqueadas']} quedan bloqueadas. "
            f"Nada se ha escrito todavía."
        )

    def _al_indexar(self, datos: dict) -> None:
        resultado = datos["resultado"]
        acotado = bool(datos.get("acotado"))
        if acotado:
            self._parar_vigilancia()
        lotes = datos.get("lotes", 1)
        self.boton_indexar.setEnabled(False)
        donde = f" en {lotes} batches" if lotes > 1 else ""
        cuenta = (
            f"Escritas {resultado.escritas}, omitidas {resultado.omitidas} "
            f"y fallidas {resultado.fallidas}. En AirVault quedaron "
            f"{datos['validas']} de {datos['total']} páginas revisadas{donde}."
        )
        separadores_borrados = getattr(resultado, "separadores_borrados", 0)
        separadores_pendientes = getattr(
            resultado, "separadores_pendientes", 0
        )
        if separadores_borrados:
            cuenta += (
                f" Se borraron {separadores_borrados} páginas "
                f"separadoras del indexado automático."
            )
        if separadores_pendientes:
            cuenta += (
                f" No se pudieron borrar "
                f"{separadores_pendientes} páginas separadoras "
                f"en AirVault."
            )
        if resultado.interrumpido:
            self.resumen.setText(
                f"{cuenta} El indexado se cortó: {resultado.interrumpido} "
                f"Lo que falta queda pendiente; al volver a revisar se "
                f"retoma sin repetir lo escrito."
            )
            self.estado_label.setText("El indexado se cortó a medio camino")
            self._anotar("El indexado se cortó a medio camino")
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
                cuenta
                + " Aún hay páginas amarillas. Se reintentaron en esta "
                "ejecución y el proceso queda disponible para continuar "
                "sin repetir las páginas verdes."
                + (
                    f" Se vuelve a revisar solo cada "
                    f"{self.minutos_spin.value()} min."
                    if sigue else ""
                )
                + (f" Motivo: {motivo}" if motivo else "")
            )
            self.estado_label.setText("Indexado incompleto")
            self._anotar("Indexado incompleto; quedan páginas pendientes")
            self._limpiar_progreso()
            return
        self._indexado_incompleto = False
        if datos.get("incluye_revision"):
            cuenta += (
                " El guardado de REVISAR terminó. Las incidencias indicadas "
                "quedan para revisión humana."
            )
        self.resumen.setText(cuenta + self._cuenta_de_cierres(datos))
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
                f" Se completaron {len(cerrados)} batches en AirVault y "
                "se mandaron a Web Search."
                if len(cerrados) > 1 else
                " El batch se completó en AirVault y se mandó a Web Search."
            )
            quitadas = sum(len(r.quitadas) for _t, r in cierres if r.completado)
            if quitadas:
                # Se dice porque es un cambio en el batch: esas páginas ya no
                # están, y quien lo abra en AirVault no las va a encontrar.
                texto += (
                    f" Se quitaron {quitadas} páginas separadoras, que no son "
                    f"bitácoras y no dejan completar el batch."
                )
            partes.append(texto)
        for trabajo, resultado in colgados:
            partes.append(
                f" El batch {trabajo.manifiesto.nombre_batch} no se pudo "
                f"completar: {resultado.detalle}"
            )
        return "".join(partes)

    def _al_fallar(self, mensaje: str) -> None:
        preparados = self._estado.get("trabajos") or []
        if preparados and not self._trabajos:
            # Preparar los PDF ocurre antes de conectar. Si la sesion o la
            # subida falla despues, se conserva ese reparto para reanudarlo
            # con el mismo limite y no mezclar manifiestos de dos repartos.
            from app.airvault.flujo import estado_local

            self._trabajos = list(preparados)
            self._estados = [estado_local(t) for t in self._trabajos]
            self._pintar_lotes()
        # Un fallo suelto no para nada: AirVault devuelve un error de vez en
        # cuando y la sesión se renueva sola, así que el siguiente intervalo
        # tiene todas las papeletas de salir bien, y nadie está delante para
        # volver a pulsar. Lo que no tiene sentido es repetir el mismo error
        # toda la tarde, y para eso está el tope.
        self._fallos_seguidos += 1
        sigue = (
            self.auto_check.isChecked()
            and self._vigilante is not None
            and self._vigilante.isActive()
            and self._fallos_seguidos < FALLOS_SEGUIDOS_ANTES_DE_PARAR
        )
        if sigue:
            mensaje += (
                f" Se vuelve a intentar solo dentro de "
                f"{self.minutos_spin.value()} min."
            )
        else:
            self._parar_vigilancia()
        self.resumen.setText(mensaje)
        self.estado_label.setText("El indexado no pudo continuar")
        # El mensaje entero queda en el resumen, que se lee de una vez.
        self._anotar(f"Se detuvo: {primera_frase(mensaje)}")
        self._limpiar_progreso()

    def _al_cancelar(self) -> None:
        """Lo paró quien lo lanzó: se dice y se sueltan los batches."""
        self.estado_label.setText("Cancelado")
        self._anotar(
            "Cancelado; se desbloquean los batches abiertos en AirVault"
        )
        self._parar_vigilancia()
        self._soltar_lotes()
        self.resumen.setText(
            "El trabajo se canceló. Los batches quedaron desbloqueados en "
            "AirVault y lo que ya se hubiera escrito se conserva: al volver a "
            "revisar se retoma sin repetirlo."
        )
        self._limpiar_progreso()

    def _al_terminar(self) -> None:
        """Cierre común del hilo, salga como salga."""
        self._indexando.clear()
        self._pintar_lotes()
        self._habilitar(True)
        self._parar_reloj()
        if self._cerrar_al_terminar:
            self._cerrar_al_terminar = False
            self.close()
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
            if self._listos() or (
                self.completar_check.isChecked() and self._por_completar()
            ):
                self._indexar()
                return
        self._publicar_avance()
        # Lo que se pidió desde la tabla mientras esto trabajaba entra ahora,
        # que es lo que hace de la tabla una cola y no una lista de avisos.
        self._siguiente_de_la_cola()

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
                                        SIN_SUBIR, POSIBLE_DUPLICADO)
        from app.gui.automatizacion import (COMPLETAR, CORTADO, EN_CURSO,
                                            ESPERAR, HECHO, INDEXAR, PENDIENTE,
                                            SUBIR)

        if not self._estados:
            return
        trabajando = self.hilo() is not None
        propios = [
            parte for parte in self._estados
            if not parte.trabajo.manifiesto.solo_subir
        ]
        terminados = (INDEXADO, COMPLETADO, AUTOCOMPLETADO)
        cerrados = (COMPLETADO, AUTOCOMPLETADO)

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
            parte.estado in (SIN_SUBIR, POSIBLE_DUPLICADO) for parte in self._estados
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
        """Deja la barra quieta en cero: lo que pasó lo cuenta el resumen."""
        self.progreso.setRange(0, 100)
        self.progreso.setValue(0)
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
                "Cancelando el trabajo en marcha. La ventana se cierra en "
                "cuanto AirVault quite el bloqueo de edición de los batches "
                "que había abierto."
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
            return
        worker.cancelar()
        self.boton_cancelar.setEnabled(False)
        self.estado_label.setText("Cancelando…")
        self._anotar("Cancelando: se espera a que suelte lo que tiene tomado")

    def detener(self) -> None:
        """Pide al hilo que pare; la llama la ventana principal al cerrarse."""
        self._parar_vigilancia()
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancelar()
            self._worker.wait(5000)
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
