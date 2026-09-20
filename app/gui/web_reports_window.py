"""Ventana de consulta para los reportes de limpieza de AirVault."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from threading import Event
from typing import Optional

from PySide6.QtCore import QDate, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCheckBox,
    QDateEdit,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.airvault.config import AIRVAULT_FILENAME, AirVaultConfig
from app.airvault.correcciones import (
    ACCION_BORRAR,
    ACCION_REVISAR,
    Correccion,
    CorrectorLogPageAudit,
    planificar,
    resumen_del_plan,
)
from app.airvault.web_reports import (
    FILTRO_DUPLICADAS,
    FILTRO_MAL_INDEXADAS,
    TIPO_DUPLICADA,
    TIPO_MAL_INDEXADA,
    ClienteLogPageAudit,
    ConsultaCancelada,
    ExcepcionLogPageAudit,
    abrir_en_web_search,
)
from app.gui.cronometro import Cronometro, cronometro_qss
from app.gui.responsive import fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import (
    CONTROL_HEIGHT,
    CONTROL_HEIGHT_COMPACT,
    SPACE_L,
    SPACE_S,
    link_text_color,
    paleta,
)
from app.gui.web_reports_tiempos import (
    TAREA_BUSQUEDA,
    TAREA_CONSULTA,
    TAREA_CORRECCION,
    Estimacion,
)
from app.gui.widgets import (
    align_vertical_scrollbar_to_header,
    configure_combo_box,
    data_table_qss,
    pintar_del_tema,
    size_columns_once,
    style_data_table,
    window_stylesheet,
)

# El mismo nombre que le dan la ventana de AirVault y la vista previa al gris
# de las frases de ayuda. Es una función y no una constante porque el gris
# cambia con el tema.
def color_ayuda() -> str:
    """El gris de las frases de ayuda."""
    return paleta().TEXT_SECONDARY

# Con el que se enseñan las dos fechas y con el que se mide cuánto ocupan.
FORMATO_FECHA = "d/M/yyyy"

# La fecha más ancha que pueden llegar a enseñar: día y mes de dos cifras.
FECHA_MAS_LARGA = QDate(2000, 12, 30)

WEB_REPORTS_TOOLTIP = (
    "Consulta páginas mal indexadas y duplicadas en Log Page Audit."
)

CORREGIR_TOOLTIP = (
    "Corrige todas las filas que Log Page Audit permite resolver."
)

CORREGIR_SELECCION_TOOLTIP = (
    "Corrige las filas seleccionadas. Use Ctrl o Mayús para elegir varias."
)

ADVERTENCIA_CORRECCION = (
    "Las copias borradas no se pueden recuperar desde BITS."
)

# Las dos columnas que llevan a Web Search, y lo que abre cada una: la
# página, sus apariciones; el rango, el libro entero al que pertenece.
COLUMNA_BITACORA = 3
COLUMNA_RANGO_LIBRO = 5

# La matricula bajo la que quedo indexada la bitacora, al lado de la del
# libro. Vacia en las duplicadas: el reporte solo la dice de las mal
# indexadas.
COLUMNA_MATRICULA_INDEXADA = 2

# Donde cada una de esas celdas guarda su dirección. No se recalcula al
# pulsar: la tabla se ordena, y la fila que se pulsa ya no es la que trajo
# la consulta.
ROL_ENLACE = int(Qt.ItemDataRole.UserRole) + 1


def _ensanchar_hasta_la_fecha_mas_larga(campo: QDateEdit) -> None:
    """Deja sitio para 30/12/2025, no solo para las fechas que lo limitan.

    Qt pide el ancho del campo midiendo sus dos límites, y aquí los dos
    (1/1/2000 y el día de hoy) llevan día y mes de una cifra. Con ese ancho
    una fecha de dos y dos no entra: del día 10 en adelante el año se cortaba
    por el final.

    Se llama con el campo ya colgado de la ventana. Mientras el cuadro que lo
    contiene no cuelgue de ella, el campo no lleva puesta la hoja de estilo y
    el ancho que pide sale sin contar ni el relleno de los lados ni el pozo de
    la flecha del calendario.
    """
    fuente = campo.fontMetrics()
    limites = max(
        fuente.horizontalAdvance(fecha.toString(FORMATO_FECHA))
        for fecha in (campo.minimumDate(), campo.maximumDate())
    )
    falta = (
        fuente.horizontalAdvance(FECHA_MAS_LARGA.toString(FORMATO_FECHA))
        - limites
    )
    campo.setMinimumWidth(campo.sizeHint().width() + max(0, falta))


class _TrabajoEnEdge(QThread):
    """Lo que comparten los dos hilos que conducen el navegador.

    Consultar el reporte y corregir lo que dice son trabajos distintos, pero
    se cancelan igual, informan igual y acaban igual: quien los lanza no
    tiene por qué saber cuál de los dos está corriendo.
    """

    avance = Signal(str)
    # Cuántas unidades van de cuántas: reportes en la consulta y bitácoras
    # en la corrección. Va aparte de «avance» porque el cronómetro necesita
    # números, y la frase de estado está escrita para leerla una persona.
    paso = Signal(int, int)
    resultado = Signal(object)
    fallo = Signal(str)
    cancelado = Signal()

    def __init__(self, config: AirVaultConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._parar = False

    def cancelar(self) -> None:
        self._parar = True
        self.requestInterruption()

    def _cancelado(self) -> bool:
        return self._parar or self.isInterruptionRequested()

    def _trabajar(self):
        """Lo propio de cada hilo. Devuelve lo que se emite al terminar."""
        raise NotImplementedError

    def run(self) -> None:  # noqa: D102 - lo describe la clase
        try:
            hecho = self._trabajar()
            if self._cancelado():
                self.cancelado.emit()
            else:
                self.resultado.emit(hecho)
        except ConsultaCancelada:
            self.cancelado.emit()
        except Exception as exc:  # noqa: BLE001 - llega a la interfaz
            self.fallo.emit(str(exc))


class WebReportsWorker(_TrabajoEnEdge):
    """Consulta SSRS fuera del hilo de la interfaz."""

    def __init__(
        self,
        config: AirVaultConfig,
        desde: date,
        hasta: date,
        filtros: tuple[str, ...],
        parent=None,
    ) -> None:
        super().__init__(config, parent)
        self._desde = desde
        self._hasta = hasta
        self._filtros = filtros

    def _trabajar(self):
        return ClienteLogPageAudit(self._config).consultar(
            self._desde,
            self._hasta,
            self._filtros,
            avisar=self.avance.emit,
            cancelar=self._cancelado,
            progreso=self.paso.emit,
        )


class CorreccionWorker(_TrabajoEnEdge):
    """Aplica el plan en AirVault fuera del hilo de la interfaz."""

    previa = Signal(object, object, object)

    def __init__(
        self,
        config: AirVaultConfig,
        plan: list[Correccion],
        parent=None,
    ) -> None:
        super().__init__(config, parent)
        self._plan = list(plan)
        self.mostrar_previas = False
        self.cache_previa = {}
        self._respuesta_previa = Event()
        self._seleccion_previa = None

    def responder_previa(self, seleccion):
        self._seleccion_previa = seleccion
        self._respuesta_previa.set()

    def _revisar(self, correccion, vistas, elegidas):
        self._respuesta_previa.clear()
        self._seleccion_previa = None
        self.previa.emit(correccion, vistas, elegidas)
        while not self._respuesta_previa.wait(.2):
            if self._cancelado():
                raise ConsultaCancelada()
        if self._cancelado():
            raise ConsultaCancelada()
        return self._seleccion_previa

    def _trabajar(self):
        # Sin ensayo porque a este hilo solo se llega después de que alguien
        # haya leído el plan y lo haya autorizado. Cada caso se sigue
        # comprobando contra la pantalla antes de escribir nada.
        corrector = CorrectorLogPageAudit(self._config)
        if self.mostrar_previas:
            corrector.revisar = self._revisar
            corrector.cache_previa = self.cache_previa
        return corrector.aplicar(
            self._plan,
            avisar=self.avance.emit,
            cancelar=self._cancelado,
            ensayo=False,
            progreso=self.paso.emit,
        )


class WebSearchWorker(_TrabajoEnEdge):
    """Deja una búsqueda de Web Search abierta y a la vista.

    Es el más corto de los tres, pero va por aquí igual: levantar Edge tarda
    lo suyo, y hacerlo en el hilo de la ventana la dejaba congelada sin
    decir por qué.
    """

    def __init__(
        self,
        config: AirVaultConfig,
        url: str,
        etiqueta: str,
        parent=None,
    ) -> None:
        super().__init__(config, parent)
        self._url = url
        self._etiqueta = etiqueta

    def _trabajar(self):
        abrir_en_web_search(
            self._config, self._url, avisar=self.avance.emit
        )
        return self._etiqueta


class WebReportsWindow(QDialog):
    """Suite de consulta para limpiar excepciones de Web Reports."""

    COLUMNAS = (
        "Tipo",
        "Matrícula del libro",
        # Las dos matrículas van juntas a propósito: la del libro es la que
        # le toca a la bitácora y esta es bajo la que quedó indexada, así
        # que una mal indexada enseña la contradicción de un vistazo. En las
        # duplicadas el reporte no la trae y la celda queda vacía, que es lo
        # que hacen las demás columnas cuando el reporte no dice el dato.
        "Matrícula indexada",
        "Página",
        "Tipo de libro",
        "Rango del libro",
        "Rango de fechas",
        "Detalle",
    )

    def __init__(self, raiz: Path) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self._raiz = Path(raiz)
        self._config = AirVaultConfig.load(self._raiz / AIRVAULT_FILENAME)
        self._worker: Optional[_TrabajoEnEdge] = None
        self._cerrar_al_terminar = False
        self._resultados: list[ExcepcionLogPageAudit] = []
        # Si ahora mismo no hay ningún trabajo en Edge. Lo consulta el
        # cambio de selección, que llega por su cuenta y no puede encender
        # un botón en mitad de una consulta.
        self._libre = True
        # El trabajo que corre ahora, con lo que lleva y lo que le falta.
        # Sin nada en Edge no hay nada que contar y el reloj se queda quieto.
        self._estimacion: Optional[Estimacion] = None
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._al_latir)

        self.setWindowTitle("Web Reports")
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self._densidad = fit_to_screen(self, 1080, 680)
        self._aplicar_hoja()
        self._build_ui()
        # La hoja lleva el fragmento de la densidad, así que la vuelve a pedir
        # la propia ventana cuando cambia el tema.
        gestor_tema().cambiado.connect(self._al_cambiar_tema)

    def _aplicar_hoja(self) -> None:
        """La hoja de la ventana, con los tonos y las medidas de ahora."""
        self.setStyleSheet(
            window_stylesheet(
                cronometro_qss() + data_table_qss() + self._densidad.qss
            )
        )

    def _al_cambiar_tema(self, _nombre: str) -> None:
        """Rehace la hoja y repinta los enlaces de la tabla.

        Los enlaces llevan su color en el propio elemento, puesto al llenar la
        tabla, así que la hoja no los alcanza: se vuelven a recorrer.
        """
        self._aplicar_hoja()
        self._repintar_enlaces()

    def _repintar_enlaces(self) -> None:
        """Devuelve a las celdas con enlace el color del tema puesto ahora."""
        color = QColor(link_text_color())
        for fila in range(self.tabla.rowCount()):
            for columna in range(self.tabla.columnCount()):
                item = self.tabla.item(fila, columna)
                if item is not None and item.data(ROL_ENLACE):
                    item.setForeground(color)

    def _build_ui(self) -> None:
        cuerpo = QVBoxLayout(self)
        margen = max(8, self._densidad.window_margin)
        cuerpo.setContentsMargins(margen, margen, margen, margen)
        cuerpo.setSpacing(self._densidad.root_spacing)

        consulta = QGroupBox("Log Page Audit")
        grid = QGridLayout(consulta)
        grid.setHorizontalSpacing(SPACE_S)
        grid.setVerticalSpacing(self._densidad.group_spacing)

        hoy = QDate.currentDate()
        inicio = QDate(hoy.year(), hoy.month(), 1)
        grid.addWidget(QLabel("Desde:"), 0, 0)
        self.desde_edit = self._fecha(inicio)
        self.desde_edit.setToolTip("Primer día del reporte, incluido.")
        grid.addWidget(self.desde_edit, 0, 1)
        grid.addWidget(QLabel("Hasta:"), 0, 2)
        self.hasta_edit = self._fecha(hoy)
        self.hasta_edit.setToolTip("Último día del reporte, incluido.")
        grid.addWidget(self.hasta_edit, 0, 3)
        # Columna vacía entre el rango y el filtro: son dos preguntas
        # distintas y sin ese aire la fila se leía como seis controles
        # seguidos, con «Mostrar:» pegado al campo de la fecha final.
        grid.setColumnMinimumWidth(4, SPACE_L)
        grid.addWidget(QLabel("Mostrar:"), 0, 5)
        self.filtro_combo = QComboBox()
        self.filtro_combo.addItem(
            "Mal indexadas y duplicadas",
            (FILTRO_MAL_INDEXADAS, FILTRO_DUPLICADAS),
        )
        self.filtro_combo.addItem(
            "Solo mal indexadas", (FILTRO_MAL_INDEXADAS,)
        )
        self.filtro_combo.addItem("Solo duplicadas", (FILTRO_DUPLICADAS,))
        # Sin suelo de caracteres y ajustado al contenido: pide lo que mide
        # su frase más larga y ni un píxel más. Con el suelo de 26 pedía el
        # ancho de 26 mayúsculas, casi el doble de lo que ocupan sus tres
        # opciones, y ese exceso salía de los campos de fecha, que se
        # quedaban en su mínimo.
        configure_combo_box(self.filtro_combo, 0)
        self.filtro_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.filtro_combo.setToolTip(
            "Qué excepciones del reporte se traen a la tabla."
        )
        grid.addWidget(self.filtro_combo, 0, 6)
        # El sitio que sobra se queda al final de la fila. Con el estiramiento
        # en la columna del desplegable, este crecía hasta el borde de la
        # ventana: cuatrocientos veintisiete píxeles para tres frases que
        # miden la mitad.
        grid.setColumnStretch(7, 1)

        ayuda = QLabel(
            "Las celdas subrayadas abren la página o el libro en Web Search."
        )
        ayuda.setWordWrap(True)
        pintar_del_tema(ayuda, lambda: f"color: {color_ayuda()};")
        grid.addWidget(ayuda, 1, 0, 1, 8)
        cuerpo.addWidget(consulta)
        # Con el cuadro ya colgado de la ventana, que es cuando los campos
        # heredan la hoja de estilo y saben cuánto miden de verdad.
        for campo in (self.desde_edit, self.hasta_edit):
            _ensanchar_hasta_la_fecha_mas_larga(campo)

        self.tabla = QTableWidget(0, len(self.COLUMNAS))
        self.tabla.setHorizontalHeaderLabels(list(self.COLUMNAS))
        self.tabla.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        # Varias filas a la vez porque la selección es lo que elige qué
        # corregir. Con una sola, «Corregir seleccionadas…» habría sido un
        # botón para una bitácora, y lo que se pidió fue elegir un grupo.
        self.tabla.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.tabla.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setSortingEnabled(True)
        self.tabla.setToolTip(
            "Consultar no modifica AirVault. Las celdas subrayadas abren "
            "Web Search."
        )
        self.tabla.setAccessibleName("Excepciones de Log Page Audit")
        # Sin seguimiento del ratón no llega «cellEntered», y sin él el
        # cursor no puede cambiar de flecha a mano al pasar por las dos
        # columnas que abren algo.
        self.tabla.setMouseTracking(True)
        self.tabla.cellEntered.connect(self._al_pasar_por_la_celda)
        self.tabla.cellClicked.connect(self._al_pulsar_la_celda)
        style_data_table(self.tabla)
        align_vertical_scrollbar_to_header(self.tabla)
        size_columns_once(self.tabla, stretch_last=True)
        cuerpo.addWidget(self.tabla, 1)

        # El mismo orden que la ventana de AirVault: la barra en su fila,
        # debajo la frase de estado y al final los botones. Compartir fila
        # con los botones dejaba la barra corta y ponía las acciones a media
        # altura de la ventana, donde no las busca nadie. El reloj sí va con
        # la barra, y con el mismo reparto que en la ventana principal: la
        # barra se estira y las tres cifras conservan su ancho.
        fila_progreso = QHBoxLayout()
        fila_progreso.setSpacing(SPACE_S)
        self.progreso = QProgressBar()
        self.progreso.setRange(0, 100)
        self.progreso.setValue(0)
        self.progreso.setTextVisible(False)
        fila_progreso.addWidget(self.progreso, 1)

        alto = (
            CONTROL_HEIGHT_COMPACT
            if self._densidad.compact
            else CONTROL_HEIGHT
        )
        self.cronometro = Cronometro(alto)
        self.cronometro.setToolTip(
            "El tiempo restante se recalcula con las bitácoras terminadas "
            "y el ritmo observado."
        )
        fila_progreso.addWidget(self.cronometro)
        cuerpo.addLayout(fila_progreso)

        self.resumen = QLabel("Listo para consultar.")
        self.resumen.setWordWrap(True)
        pintar_del_tema(
            self.resumen, lambda: f"color: {color_ayuda()};"
        )
        # Con el sitio reservado, como allá: los motivos de fallo de esta
        # consulta son igual de largos («Complete el acceso en Edge con la
        # cuenta de trabajo…») y sin el hueco la ventana pegaba un salto
        # cada vez que aparecía uno.
        self.resumen.setMinimumHeight(
            self._densidad.airvault_summary_min_height
        )
        self.resumen.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        cuerpo.addWidget(self.resumen)

        self.mostrar_previas = QCheckBox("Revisar imágenes antes de eliminar copias")
        self.mostrar_previas.setChecked(True)
        self.mostrar_previas.setToolTip("Carga las imágenes para comparar y ajustar qué copias se eliminan en cada bitácora.")
        self._cache_previa = {}
        cuerpo.addWidget(self.mostrar_previas)
        cuerpo.addLayout(self._fila_botones())
        # Después de los botones: lo que hace al cambiar la selección es
        # encenderlos o apagarlos, y hasta aquí no existen.
        self.tabla.itemSelectionChanged.connect(self._al_elegir_filas)

    def _fila_botones(self) -> QHBoxLayout:
        """Las acciones contra el margen derecho, como en AirVault.

        En azul va «Corregir todas…», que es la que cierra el trabajo de la
        ventana. Las otras dos que arrancan algo quedan en el gris de
        siempre: consultar es el paso previo, y corregir lo elegido es la
        misma acción sobre menos filas.
        """
        fila = QHBoxLayout()
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(SPACE_S)
        fila.addStretch()

        self.boton_consultar = QPushButton("Consultar")
        self.boton_consultar.setToolTip(WEB_REPORTS_TOOLTIP)
        self.boton_consultar.clicked.connect(self._consultar)

        self.boton_corregir = QPushButton("Corregir seleccionadas…")
        self.boton_corregir.setEnabled(False)
        self.boton_corregir.setToolTip(CORREGIR_SELECCION_TOOLTIP)
        self.boton_corregir.clicked.connect(self._corregir_seleccion)

        self.boton_corregir_todas = QPushButton("Corregir todas…")
        self.boton_corregir_todas.setObjectName("primaryButton")
        self.boton_corregir_todas.setEnabled(False)
        self.boton_corregir_todas.setToolTip(CORREGIR_TOOLTIP)
        self.boton_corregir_todas.clicked.connect(self._corregir_todas)

        self.boton_cancelar = QPushButton("Cancelar")
        self.boton_cancelar.setEnabled(False)
        self.boton_cancelar.setToolTip("Detiene el trabajo en curso.")
        self.boton_cancelar.clicked.connect(self._cancelar)

        self.boton_cerrar = QPushButton("Cerrar")
        self.boton_cerrar.clicked.connect(self.close)

        for boton in (
            self.boton_consultar,
            self.boton_corregir,
            self.boton_corregir_todas,
            self.boton_cancelar,
            self.boton_cerrar,
        ):
            fila.addWidget(boton)
        return fila

    # ── corregir ────────────────────────────────────────────────────

    def plan(self) -> list[Correccion]:
        """Qué haría la corrección con lo que hay ahora en la tabla."""
        return planificar(self._resultados)

    def seleccionadas(self) -> list[ExcepcionLogPageAudit]:
        """Las excepciones de las filas elegidas, de arriba abajo.

        Salen de la propia celda y no de la posición de la fila: la tabla se
        ordena por cualquier columna, así que la fila tercera de la pantalla
        no tiene por qué ser la tercera que trajo la consulta.
        """
        filas = sorted(
            {indice.row() for indice in self.tabla.selectedIndexes()}
        )
        elegidas: list[ExcepcionLogPageAudit] = []
        for fila in filas:
            item = self.tabla.item(fila, 0)
            if item is None:
                continue
            excepcion = item.data(Qt.ItemDataRole.UserRole)
            if excepcion is not None:
                elegidas.append(excepcion)
        return elegidas

    def plan_seleccionado(self) -> list[Correccion]:
        """El plan de siempre, recortado a las filas elegidas.

        Se planifica con la tabla entera y después se recorta, y no al
        revés: el plan cruza unas excepciones con otras (una mal indexada
        que además está repetida se deja para después de quitar las
        copias). Planificando solo lo elegido, elegir la mal indexada sin su
        duplicada la habría dado por reindexable, que es justo lo que esa
        regla evita.
        """
        elegidas = {id(excepcion) for excepcion in self.seleccionadas()}
        return [
            correccion
            for correccion in self.plan()
            if id(correccion.excepcion) in elegidas
        ]

    @staticmethod
    def _aplicables(plan: list[Correccion]) -> list[Correccion]:
        return [
            correccion
            for correccion in plan
            if correccion.accion != ACCION_REVISAR
        ]

    def _corregir_todas(self) -> None:
        """Todo lo que el reporte deja decidido, sin elegir nada."""
        self._corregir(self.plan())

    def _corregir_seleccion(self) -> None:
        """Solo las filas elegidas en la tabla."""
        plan = self.plan_seleccionado()
        if not plan:
            self.resumen.setText("Seleccione al menos una fila.")
            return
        self._corregir(plan)

    def _corregir(self, plan: list[Correccion]) -> None:
        """Aplica en AirVault lo que el reporte deja decidido."""
        if self.hilo() is not None:
            return
        if not self._aplicables(plan):
            self.resumen.setText(resumen_del_plan(plan))
            return
        if not self._autorizado(plan):
            return
        self._config = AirVaultConfig.load(self._raiz / AIRVAULT_FILENAME)
        worker = CorreccionWorker(self._config, plan, self)
        worker.mostrar_previas = self.mostrar_previas.isChecked()
        worker.cache_previa = self._cache_previa
        worker.previa.connect(self._revisar_copias)
        worker.avance.connect(self._al_avanzar)
        worker.resultado.connect(self._al_corregir)
        worker.fallo.connect(self._al_fallar_correccion)
        worker.cancelado.connect(self._al_cancelar)
        worker.paso.connect(self._al_pasar)
        worker.finished.connect(self._al_terminar)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        self._habilitar(False)
        self.progreso.setRange(0, 0)
        self.resumen.setText("Corrigiendo en AirVault…")
        # Por bitácora que de verdad se va a abrir en Edge: las de revisión
        # manual salen del plan resueltas y no cuestan nada de navegador.
        self._arrancar_cronometro(
            TAREA_CORRECCION, len(self._aplicables(plan))
        )
        worker.start()

    def _revisar_copias(self, correccion, vistas, elegidas):
        from app.gui.revision_copias_dialog import RevisionCopiasDialog
        worker = self.sender()
        if worker is not self._worker or worker._cancelado():
            worker.responder_previa(None)
            return
        dialogo = RevisionCopiasDialog(correccion, vistas, elegidas, self)
        aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
        worker.responder_previa(dialogo.seleccionadas() if aceptado else None)

    def _autorizado(self, plan: list[Correccion]) -> bool:
        """Lo que va a pasar, por escrito, antes de tocar nada.

        Con la lista entera detrás del botón de detalles: el resumen dice
        cuánto, y quien autoriza tiene derecho a ver qué bitácoras son sin
        salir del cuadro.
        """
        dialogo = QMessageBox(self)
        dialogo.setIcon(QMessageBox.Icon.Question)
        dialogo.setWindowTitle("Corregir en AirVault")
        dialogo.setText(resumen_del_plan(plan))
        if any(correccion.accion == ACCION_BORRAR for correccion in plan):
            dialogo.setInformativeText(ADVERTENCIA_CORRECCION)
        aceptar = dialogo.addButton(
            "Corregir", QMessageBox.ButtonRole.AcceptRole
        )
        dialogo.addButton("No corregir", QMessageBox.ButtonRole.RejectRole)
        dialogo.exec()
        return dialogo.clickedButton() is aceptar

    def _al_corregir(self, resultados: object) -> None:
        """Cuenta lo que se hizo y lo que no, sin esconder lo segundo."""
        self._cerrar_cronometro(aprender=True)
        resultados = list(resultados)
        hechos = [resultado for resultado in resultados if resultado.hecho]
        intentados = [
            resultado
            for resultado in resultados
            if resultado.correccion.accion != ACCION_REVISAR
        ]
        fallidos = [resultado for resultado in intentados if not resultado.hecho]
        total = len(intentados)
        unidad = "bitácora" if total == 1 else "bitácoras"
        if fallidos:
            sin_cambiar = (
                "1 no se modificó"
                if len(fallidos) == 1
                else f"{len(fallidos)} no se modificaron"
            )
            texto = (
                f"Corregidas {len(hechos)} de {total} {unidad}. "
                f"{sin_cambiar}."
            )
        else:
            texto = f"Corregidas {len(hechos)} {unidad}."
        self.resumen.setText(texto)

        if not fallidos:
            return
        aviso = QMessageBox(self)
        aviso.setIcon(QMessageBox.Icon.Warning)
        aviso.setWindowTitle("Corrección incompleta")
        aviso.setText(texto)
        primero = fallidos[0]
        aviso.setInformativeText(
            f"Bitácora {primero.log_number}: {primero.detalle}"
        )
        aviso.setDetailedText(
            "\n".join(
                f"{resultado.log_number}: {resultado.detalle}"
                for resultado in fallidos
            )
        )
        for boton in aviso.findChildren(QPushButton):
            if "details" in boton.text().casefold():
                boton.setText("Ver detalles…")
        aviso.exec()

    def _al_fallar_correccion(self, mensaje: str) -> None:
        """Un fallo del corrector no se presenta como fallo de consulta."""
        self._cerrar_cronometro(aprender=False)
        self.resumen.setText(f"Error al corregir: {mensaje}")

    @staticmethod
    def _fecha(valor: QDate) -> QDateEdit:
        control = QDateEdit(valor)
        control.setCalendarPopup(True)
        control.setDisplayFormat(FORMATO_FECHA)
        control.setMinimumDate(QDate(2000, 1, 1))
        control.setMaximumDate(QDate.currentDate())
        return control

    def _consultar(self) -> None:
        if self.hilo() is not None:
            return
        desde = self.desde_edit.date().toPython()
        hasta = self.hasta_edit.date().toPython()
        if desde > hasta:
            self.resumen.setText(
                "La fecha inicial no puede ser posterior a la fecha final."
            )
            return
        filtros = tuple(self.filtro_combo.currentData())
        self._config = AirVaultConfig.load(self._raiz / AIRVAULT_FILENAME)
        worker = WebReportsWorker(
            self._config, desde, hasta, filtros, self
        )
        worker.avance.connect(self._al_avanzar)
        worker.resultado.connect(self._al_recibir)
        worker.fallo.connect(self._al_fallar)
        worker.cancelado.connect(self._al_cancelar)
        worker.paso.connect(self._al_pasar)
        worker.finished.connect(self._al_terminar)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        self._habilitar(False)
        self.progreso.setRange(0, 0)
        self.resumen.setText("Consultando Web Reports…")
        self._arrancar_cronometro(TAREA_CONSULTA, len(filtros))
        worker.start()

    def _al_avanzar(self, texto: str) -> None:
        self.resumen.setText(texto)

    def _arrancar_cronometro(self, tarea: str, unidades: int) -> None:
        """Pone el reloj en marcha con lo que costó ese trabajo la vez pasada."""
        self._estimacion = Estimacion(tarea, unidades)
        self._al_latir()
        self._timer.start()

    def _al_pasar(self, hechas: int, total: int) -> None:
        """Unidades terminadas, contadas por el hilo que conduce Edge.

        El total llega con cada aviso porque es el hilo el que sabe cuántas
        piezas tenía el trabajo de verdad.
        """
        if self._estimacion is not None:
            self._estimacion.avanzo(hechas, total)

    def _al_latir(self) -> None:
        """Repinta las tres cifras, cuatro veces por segundo."""
        estimacion = self._estimacion
        if estimacion is None:
            return
        transcurrido = estimacion.transcurrido()
        restante = estimacion.restante()
        self.cronometro.actualizar(
            transcurrido, restante, transcurrido + restante
        )

    def _cerrar_cronometro(self, aprender: bool) -> None:
        """Para el reloj donde acabó el trabajo.

        Solo se guardan las medidas de una corrida que llegó al final. Una
        cancelada o una que falló dejó fuera lo que faltaba, y guardarla
        enseñaría la próxima vez un trabajo más corto de lo que es; en esas
        queda el tiempo que se gastó y ningún pronóstico que ya no se debe.
        """
        estimacion = self._estimacion
        self._timer.stop()
        self._estimacion = None
        if estimacion is None:
            return
        transcurrido = estimacion.transcurrido()
        if not aprender:
            self.cronometro.actualizar(transcurrido, None, None)
            return
        estimacion.aprender()
        self.cronometro.actualizar(transcurrido, 0.0, transcurrido)

    def _al_recibir(self, excepciones: object) -> None:
        self._cerrar_cronometro(aprender=True)
        self._resultados = list(excepciones)
        self._llenar_tabla(self._resultados)
        mal_indexadas = sum(
            resultado.tipo == TIPO_MAL_INDEXADA
            for resultado in self._resultados
        )
        duplicadas = sum(
            resultado.tipo == TIPO_DUPLICADA
            for resultado in self._resultados
        )
        if not self._resultados:
            self.resumen.setText("Sin resultados en el rango seleccionado.")
        else:
            tipo_mal = (
                "mal indexada" if mal_indexadas == 1 else "mal indexadas"
            )
            tipo_duplicada = (
                "duplicada" if duplicadas == 1 else "duplicadas"
            )
            texto = (
                f"{mal_indexadas} {tipo_mal}, "
                f"{duplicadas} {tipo_duplicada}."
            )
            aplicables = len(self._aplicables(self.plan()))
            if aplicables:
                texto += (
                    " 1 se puede corregir."
                    if aplicables == 1
                    else f" {aplicables} se pueden corregir."
                )
            else:
                texto += " Requieren revisión manual."
            self.resumen.setText(texto)

    def _llenar_tabla(
        self, excepciones: list[ExcepcionLogPageAudit]
    ) -> None:
        self.tabla.setSortingEnabled(False)
        self.tabla.setRowCount(len(excepciones))
        for fila, excepcion in enumerate(excepciones):
            detalle = excepcion.detalle
            if excepcion.tipo == TIPO_DUPLICADA and excepcion.copias:
                detalle = f"{excepcion.copias} apariciones: {detalle}"
            elif excepcion.destino:
                detalle = (
                    f"Indexada como {excepcion.destino}: {detalle}"
                )
            valores = (
                excepcion.tipo,
                excepcion.matricula_libro,
                excepcion.destino,
                excepcion.log_number,
                excepcion.tipo_libro,
                excepcion.rango_libro,
                excepcion.rango_fechas,
                detalle,
            )
            enlaces = {
                COLUMNA_BITACORA: excepcion.url_busqueda,
                COLUMNA_RANGO_LIBRO: excepcion.url_busqueda_libro,
            }
            for columna, valor in enumerate(valores):
                item = QTableWidgetItem(str(valor))
                item.setToolTip(str(valor))
                if columna == 0:
                    item.setData(Qt.ItemDataRole.UserRole, excepcion)
                if enlaces.get(columna):
                    self._marcar_enlace(item, columna, enlaces[columna])
                self.tabla.setItem(fila, columna, item)
        self.tabla.setSortingEnabled(True)
        size_columns_once(self.tabla, stretch_last=True)

    def _marcar_enlace(
        self, item: QTableWidgetItem, columna: int, url: str
    ) -> None:
        """Deja la celda con pinta de enlace y con su dirección dentro.

        El subrayado es el indicador que se ve siempre, incluso en la fila
        seleccionada: ahí Qt pinta el texto con el color de la selección y
        el azul se pierde, pero la raya de debajo se queda. El color y el
        cursor lo acompañan mientras la fila no esté elegida.
        """
        item.setData(ROL_ENLACE, url)
        item.setForeground(QColor(link_text_color()))
        fuente = self.tabla.font()
        fuente.setUnderline(True)
        item.setFont(fuente)
        item.setToolTip(
            f"Clic para abrir {self._lo_que_abre(columna, item.text())} en "
            "Web Search."
        )

    @staticmethod
    def _lo_que_abre(columna: int, valor: str) -> str:
        """Cómo se nombra lo que hay al otro lado de cada enlace."""
        if columna == COLUMNA_BITACORA:
            return f"la bitácora {valor}"
        return f"el libro {valor}"

    def _al_pasar_por_la_celda(self, fila: int, columna: int) -> None:
        """Mano sobre lo que abre algo, flecha sobre lo demás."""
        item = self.tabla.item(fila, columna)
        enlace = item.data(ROL_ENLACE) if item is not None else None
        self.tabla.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor
            if enlace
            else Qt.CursorShape.ArrowCursor
        )

    def _al_pulsar_la_celda(self, fila: int, columna: int) -> None:
        """Abre en Web Search lo que la celda pulsada tenga guardado."""
        item = self.tabla.item(fila, columna)
        url = str(item.data(ROL_ENLACE) or "") if item is not None else ""
        if not url:
            return
        self._abrir_en_web_search(
            url, self._lo_que_abre(columna, item.text())
        )

    def _abrir_en_web_search(self, url: str, etiqueta: str) -> None:
        """Lleva el Edge del programa a esa búsqueda, sin colgar la ventana.

        Con el mismo candado que el resto: Edge admite un navegador por
        perfil, así que abrir esto mientras se consulta o se corrige le
        quitaría la pestaña al trabajo que ya estaba corriendo. Las
        búsquedas de una en una, entonces; abierta la primera, cada una
        siguiente se suma a la misma ventana y ninguna cierra a la anterior.
        """
        if self.hilo() is not None:
            self.resumen.setText("Espere a que termine el trabajo actual.")
            return
        self._config = AirVaultConfig.load(self._raiz / AIRVAULT_FILENAME)
        worker = WebSearchWorker(self._config, url, etiqueta, self)
        worker.avance.connect(self._al_avanzar)
        worker.resultado.connect(self._al_abrir)
        worker.fallo.connect(self._al_fallar_al_abrir)
        worker.cancelado.connect(self._al_cancelar_la_apertura)
        worker.finished.connect(self._al_terminar)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        self._habilitar(False)
        self.progreso.setRange(0, 0)
        self.resumen.setText(f"Abriendo {etiqueta} en Web Search.")
        # Sin unidades: abrir una búsqueda es la apertura y nada más.
        self._arrancar_cronometro(TAREA_BUSQUEDA, 0)
        worker.start()

    def _al_abrir(self, etiqueta: object) -> None:
        # La búsqueda no tiene unidades, así que la apertura no termina
        # hasta aquí: lo que costó abrirla es el trabajo entero.
        if self._estimacion is not None:
            self._estimacion.abrio()
        self._cerrar_cronometro(aprender=True)
        self.resumen.setText(f"Abierto en Web Search: {etiqueta}.")

    def _al_fallar_al_abrir(self, mensaje: str) -> None:
        self._cerrar_cronometro(aprender=False)
        self.resumen.setText(f"No se pudo abrir Web Search: {mensaje}")

    def _al_cancelar_la_apertura(self) -> None:
        """Abrir no se puede detener a medias, y no se finge que sí.

        Cuando llega la cancelación, Edge ya está arriba con la búsqueda
        dentro: lo único que queda por decir es dónde quedó la ventana.
        """
        self._cerrar_cronometro(aprender=False)
        self.resumen.setText(
            "Web Search quedó abierto."
        )

    def _al_fallar(self, mensaje: str) -> None:
        self._cerrar_cronometro(aprender=False)
        self.resumen.setText(f"Error al consultar: {mensaje}")

    def _al_cancelar(self) -> None:
        self._cerrar_cronometro(aprender=False)
        self.resumen.setText("Consulta cancelada.")

    def _al_terminar(self) -> None:
        # Por si el hilo acabó sin decir cómo: el reloj no se queda corriendo
        # detrás de un trabajo que ya no existe.
        self._cerrar_cronometro(aprender=False)
        self._worker = None
        self._habilitar(True)
        self.progreso.setRange(0, 100)
        self.progreso.setValue(0)
        if self._cerrar_al_terminar:
            self._cerrar_al_terminar = False
            self.close()

    def _habilitar(self, habilitado: bool) -> None:
        self._libre = habilitado
        for control in (
            self.desde_edit,
            self.hasta_edit,
            self.filtro_combo,
            self.boton_consultar,
        ):
            control.setEnabled(habilitado)
        # Corregir solo se ofrece cuando hay algo que el reporte deje
        # decidido. Con la tabla vacía, o con todo pendiente de revisar a
        # mano, el botón no tendría nada que hacer; y el de la selección
        # tampoco mientras no haya filas elegidas que corregir.
        self.boton_corregir_todas.setEnabled(
            habilitado and bool(self._aplicables(self.plan()))
        )
        self.boton_corregir.setEnabled(
            habilitado and bool(self._aplicables(self.plan_seleccionado()))
        )
        self.boton_cancelar.setEnabled(not habilitado)

    def _al_elegir_filas(self) -> None:
        """Elegir filas enciende o apaga el botón que actúa sobre ellas."""
        self._habilitar(self._libre)

    def _cancelar(self) -> None:
        worker = self.hilo()
        if worker is None:
            return
        worker.cancelar()
        self.boton_cancelar.setEnabled(False)
        self.resumen.setText("Cancelando…")

    def hilo(self) -> Optional[QThread]:
        worker = self._worker
        if worker is None:
            return None
        try:
            return worker if worker.isRunning() else None
        except RuntimeError:
            return None

    def detener(self) -> None:
        worker = self.hilo()
        if worker is not None:
            worker.cancelar()

    def closeEvent(self, event) -> None:
        if self.hilo() is not None:
            self._cerrar_al_terminar = True
            self._cancelar()
            event.ignore()
            return
        super().closeEvent(event)
