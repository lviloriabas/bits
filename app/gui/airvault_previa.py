"""Vista previa de los batches de una entrega y de lo que lleva cada uno.

La ventana de AirVault enseña los batches que ya existen, pero hasta que se
pulsa «Subir a AirVault» no hay ninguno: el reparto se decide al preparar
los archivos, y hasta entonces no había forma de saber en cuántos batches
iba a quedar la ejecución ni qué bitácoras caían en cada uno. Aquí se
calcula ese mismo reparto sin escribir nada y se enseña antes de subir,
junto con los batches que ya están esperando en la cola.

De cada batch se puede abrir la lista de sus páginas. Esa lista se mira
como el visor de CSV, en compacto: la página de la bitácora a la izquierda,
la tabla a la derecha, un buscador encima y las columnas ordenables con un
clic en su cabecera. Comprobar que una bitácora concreta va donde se espera
no obliga entonces a abrir el PDF por fuera: se elige su fila y la hoja
escaneada aparece al lado.

La lista enseña el batch entero: las separadoras ocupan su fila, porque son
las que corren la numeración del batch y sin ellas la página que dice la
tabla no es la que enseña Web Index. Las columnas son los campos que el
programa da por importantes, los mismos de la vista resumida del visor de
CSV, y no todo lo que se le escribe a la página: repasar el índice entero
es lo que hace el reporte de revisión.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.airvault.mapping import fecha_airvault
from app.airvault.model import EstadoRegistro, Registro
from app.gui.responsive import Density, fit_to_screen
from app.gui.table_sort import ColumnSortController
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_S, paleta
from app.gui.widgets import (
    ElidedLabel,
    align_vertical_scrollbar_to_header,
    data_table_qss,
    pane_status_colors,
    pintar_celda_del_tema,
    pintar_del_tema,
    size_columns_once,
    style_data_table,
    window_stylesheet,
)
from app.utils.important_fields import default_important_columns

# Los mismos tonos con los que las dos ventanas escriben sus líneas de ayuda
# y marcan lo que ya está hecho. Son funciones y no constantes porque cambian
# con el tema.
def color_ayuda() -> str:
    """El gris de las líneas de ayuda."""
    return paleta().TEXT_SECONDARY


def color_hecho() -> str:
    """El verde de lo que ya está subido."""
    return pane_status_colors()["OK"]

_AYUDA_BUSQUEDA = "La búsqueda selecciona cada coincidencia."

# Reparto del ancho entre la página y la tabla, el mismo del visor de CSV.
_PANEL_PDF = 2
_TABLA = 3

NOMBRE_ESTADO_REGISTRO = {
    EstadoRegistro.PENDIENTE: "Por indexar",
    EstadoRegistro.ESCRITA: "Indexada",
    EstadoRegistro.OMITIDA: "Omitida",
    EstadoRegistro.ERROR: "Error",
}
# Lo que se escribió y además se dio por cerrado en AirVault. El batch
# completado ya salió de la cola y se fue a Web Search, así que decir de sus
# bitácoras que están «indexadas» se queda corto: no queda nada por hacerles.
NOMBRE_COMPLETADA = "Completada"
# La página divisoria del PDF de entrega. Ocupa su sitio en el batch para
# que la correspondencia por posición siga en pie, pero no es un documento:
# no se le escribe nada y se borra al cerrar el batch.
NOMBRE_SEPARADOR = "Separador"


# Columna de datos de la lista y campo de la ejecución del que sale. Cuáles
# se enseñan no se decide aquí: se quedan las que el programa da por
# importantes (``app/utils/important_fields.py``), que son las mismas que el
# visor de CSV deja en su vista resumida. Lo demás que se le escribe a la
# página (Doc Type, Fleet, Lessor, Audit Status y las marcas de lo deducido)
# no cabe en una lista que se mira de un vistazo: está en el reporte de
# revisión, que es donde se repasa el índice entero.
_CAMPO_DE_COLUMNA = {
    "Origen": "file",
    "Matrícula": "matricula",
    "Log Page": "log_number",
    "Vuelo": "flight_number",
    "End Date": "date",
}


def _columnas_de_datos() -> tuple[str, ...]:
    """Las columnas de campo que el programa marca como importantes."""
    importantes = default_important_columns(_CAMPO_DE_COLUMNA.values())
    return tuple(
        nombre
        for nombre, campo in _CAMPO_DE_COLUMNA.items()
        if campo in importantes
    )


def _plural(cantidad: int, singular: str, plural: str) -> str:
    return f"{cantidad} {singular if cantidad == 1 else plural}"


def _origen(registro: Registro) -> str:
    """De qué archivo y página de la ejecución salió la bitácora."""
    if not registro.archivo_origen:
        return ""
    return f"{registro.archivo_origen}, p. {registro.pagina_origen}"


def _estado_de(registro: Registro, completado: bool = False) -> str:
    """Cómo quedó la página: en una vista previa, «por indexar».

    En un batch ya trabajado dice cómo terminó cada bitácora, y en uno
    completado, que ya no queda nada por hacerle. Lo que le impide
    escribirse va aparte, en los avisos: son dos preguntas distintas y
    responder una tapando la otra dejaba sin estado a la página bloqueada.
    """
    if registro.es_separador:
        return NOMBRE_SEPARADOR
    if completado and registro.estado is EstadoRegistro.ESCRITA:
        return NOMBRE_COMPLETADA
    return NOMBRE_ESTADO_REGISTRO.get(registro.estado, str(registro.estado))


def _avisos_de(registro: Registro) -> str:
    """Lo que deja la página bloqueada, que es lo que hay que resolver."""
    partes = list(registro.avisos)
    if registro.duplicado:
        partes.append("duplicada")
    if registro.discrepancia:
        partes.append("discrepancia")
    return "; ".join(partes)


def _tabla(columnas: Sequence[str], ayuda: str) -> QTableWidget:
    """Una tabla de datos con el aspecto de las de las dos ventanas."""
    tabla = QTableWidget(0, len(columnas))
    tabla.setHorizontalHeaderLabels(list(columnas))
    tabla.setToolTip(ayuda)
    tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    tabla.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    tabla.setAlternatingRowColors(True)
    tabla.verticalHeader().setVisible(False)
    style_data_table(tabla)
    align_vertical_scrollbar_to_header(tabla)
    return tabla


class _ListaBuscable(QDialog):
    """Cuadro con una tabla que se busca y se ordena, como el visor de CSV.

    Las dos ventanas de la vista previa enseñan listas largas (los batches
    de una entrega, las bitácoras de un batch), así que las dos necesitan lo
    mismo: escribir un texto y que la fila aparezca, y ordenar por la
    columna que se quiera. Se resuelve aquí una vez para que las dos se
    manejen igual y con los mismos atajos.
    """

    #: Columnas donde busca el texto. Vacío significa todas.
    COLUMNAS_BUSCABLES: tuple[int, ...] = ()

    def __init__(self, dueño: QWidget | None = None) -> None:
        """Ventana nativa aparte, como el visor de CSV y la de AirVault.

        Sin dueño a nivel de Qt: en Windows una ventana con dueño no recibe
        entrada propia en la barra de tareas y se queda pegada a la de
        arriba. Quien la abre conserva la referencia, que es lo que la
        mantiene viva. ``dueño`` solo sirve para heredar el icono.
        """
        super().__init__(None, Qt.WindowType.Window)
        # Mirar una lista larga es compatible con seguir trabajando en la
        # ventana de al lado, así que se puede minimizar.
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        # Al cerrarla se destruye: son ventanas de consulta y no tiene
        # sentido acumularlas en memoria una por batch mirado.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        if dueño is not None:
            self.setWindowIcon(dueño.windowIcon())

    def _medir_la_pantalla(self, ancho: int, alto: int) -> Density:
        """Tamaño de apertura y hoja de estilo, como las demás ventanas.

        Sin dueño no se hereda la hoja de la ventana que la abrió, así que
        se aplica aquí la misma: tipografías, botones y el radio de 6 px de
        los cuadros salen de ahí y no del estilo nativo.
        """
        densidad = fit_to_screen(self, ancho, alto)
        self._densidad = densidad
        self._aplicar_hoja()
        # La hoja lleva el fragmento de la densidad, así que la vuelve a pedir
        # la propia ventana cuando cambia el tema.
        gestor_tema().cambiado.connect(self._al_cambiar_tema)
        return densidad

    def _aplicar_hoja(self) -> None:
        """La hoja de la ventana, con los tonos y las medidas de ahora."""
        self.setStyleSheet(
            window_stylesheet(data_table_qss() + self._densidad.qss)
        )

    def _al_cambiar_tema(self, _nombre: str) -> None:
        """Rehace la hoja de la ventana con los tonos del tema nuevo."""
        self._aplicar_hoja()

    def mostrar(self) -> None:
        """La trae al frente, esté recién abierta o ya abierta detrás."""
        self.show()
        self.raise_()
        self.activateWindow()

    def _fila_de_busqueda(self, pista: str, ayuda: str) -> QHBoxLayout:
        self._coincidencias: list[int] = []
        self._posicion = -1
        self._texto_buscado = ""
        self._pista_busqueda = _AYUDA_BUSQUEDA

        fila = QHBoxLayout()
        fila.setSpacing(SPACE_S)
        self.buscar_edit = QLineEdit()
        self.buscar_edit.setPlaceholderText(pista)
        self.buscar_edit.setToolTip(ayuda)
        self.buscar_edit.setAccessibleName("Texto que se busca en la lista")
        self.buscar_edit.returnPressed.connect(self._buscar)
        fila.addWidget(self.buscar_edit, 1)
        boton = QPushButton("Buscar")
        boton.setToolTip(
            "Buscar el texto; repetido, pasa a la coincidencia siguiente"
        )
        boton.clicked.connect(self._buscar)
        fila.addWidget(boton)
        self.buscar_anterior = QPushButton("‹")
        self.buscar_anterior.setToolTip("Coincidencia anterior")
        self.buscar_anterior.setEnabled(False)
        self.buscar_anterior.clicked.connect(lambda: self._mover_busqueda(-1))
        fila.addWidget(self.buscar_anterior)
        self.buscar_siguiente = QPushButton("›")
        self.buscar_siguiente.setToolTip("Coincidencia siguiente")
        self.buscar_siguiente.setEnabled(False)
        self.buscar_siguiente.clicked.connect(lambda: self._mover_busqueda(1))
        fila.addWidget(self.buscar_siguiente)
        # Una frase larga pide de ancho mínimo la frase entera: se recorta
        # antes que empujar el separador y estrechar la página. Recortar es
        # de ``ElidedLabel``, que la termina en puntos suspensivos y deja la
        # entera en el tooltip; un QLabel a secas la cortaba a media palabra.
        self.busqueda_ayuda = ElidedLabel(_AYUDA_BUSQUEDA)
        pintar_del_tema(self.busqueda_ayuda, lambda: f"color: {color_ayuda()};")
        self.busqueda_ayuda.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.busqueda_ayuda.setMinimumWidth(0)
        fila.addWidget(self.busqueda_ayuda, 1)
        # Ctrl+F desde cualquier punto del cuadro, como en el resto.
        QShortcut(
            QKeySequence.StandardKey.Find, self,
            activated=self.buscar_edit.setFocus,
        )
        return fila

    def _columnas_buscables(self) -> range | tuple[int, ...]:
        return self.COLUMNAS_BUSCABLES or range(self.tabla.columnCount())

    def _buscar(self) -> None:
        """Busca el texto escrito y lleva la tabla a la primera coincidencia.

        Repetirlo con el mismo texto avanza a la siguiente, igual que ›: es
        lo que se espera al volver a pulsar Intro sobre lo que ya se buscó.
        """
        texto = self.buscar_edit.text().strip()
        if (
            texto
            and texto.casefold() == self._texto_buscado
            and self._coincidencias
        ):
            self._mover_busqueda(1)
            return
        self._texto_buscado = texto.casefold()
        self._coincidencias = []
        self._posicion = -1
        if not texto:
            self.busqueda_ayuda.setText(self._pista_busqueda)
            self._sincronizar_busqueda()
            return
        self._coincidencias = self._filas_con(self._texto_buscado)
        if not self._coincidencias:
            self.busqueda_ayuda.setText(f"«{texto}»: sin coincidencias.")
            self._sincronizar_busqueda()
            return
        self._posicion = 0
        self._mostrar_coincidencia()

    def _filas_con(self, texto: str) -> list[int]:
        """Filas que contienen el texto, las exactas primero."""
        exactas: list[int] = []
        parciales: list[int] = []
        for fila in range(self.tabla.rowCount()):
            for columna in self._columnas_buscables():
                item = self.tabla.item(fila, columna)
                valor = item.text().strip().casefold() if item else ""
                if valor == texto:
                    exactas.append(fila)
                    break
                if texto in valor:
                    parciales.append(fila)
                    break
        return exactas + parciales

    def _mover_busqueda(self, salto: int) -> None:
        if not self._coincidencias:
            return
        self._posicion = (self._posicion + salto) % len(self._coincidencias)
        self._mostrar_coincidencia()

    def _mostrar_coincidencia(self) -> None:
        fila = self._coincidencias[self._posicion]
        self.tabla.selectRow(fila)
        self.tabla.scrollToItem(self.tabla.item(fila, 0))
        self.busqueda_ayuda.setText(
            f"Coincidencia {self._posicion + 1} de "
            f"{len(self._coincidencias)}."
        )
        self._sincronizar_busqueda()

    def _sincronizar_busqueda(self) -> None:
        varias = len(self._coincidencias) > 1
        self.buscar_anterior.setEnabled(varias)
        self.buscar_siguiente.setEnabled(varias)

    def _olvidar_busqueda(self) -> None:
        """Las coincidencias son posiciones, y ordenar las mueve de sitio."""
        self._coincidencias = []
        self._posicion = -1
        self._texto_buscado = ""
        self.busqueda_ayuda.setText(self._pista_busqueda)
        self._sincronizar_busqueda()


class BitacorasDelBatch(_ListaBuscable):
    """Las páginas que van dentro de un batch, con su hoja al lado.

    De cada una se enseñan las columnas importantes, y la fecha ya convertida
    al formato de AirVault: lo que se busca aquí es comprobar de un vistazo
    que la bitácora va donde debe, no repasar su índice entero.
    """

    #: La posición en el batch y cómo quedó la página no salen de ningún
    #: campo de la ejecución: son del batch, y por eso van siempre.
    COLUMNAS = ("Página", *_columnas_de_datos(), "Estado", "Avisos")
    # El estado y los avisos son frases, no datos que se busquen; el resto
    # sí, y se buscan por el mismo valor que la fila enseña.
    COLUMNAS_BUSCABLES = tuple(range(len(COLUMNAS) - 2))

    _PISTA = "Log Page, matrícula, fecha, archivo de origen…"

    def __init__(
        self,
        nombre: str,
        registros: Sequence[Registro],
        csv: Path | str = "",
        completado: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._registros = list(registros)
        self._csv = Path(csv) if csv else None
        self._completado = bool(completado)
        # Página del batch que muestra cada fila de la tabla, en el orden en
        # que se llenó. Es lo que le pide el visor a la hoja que enseña.
        self._paginas: list[Registro] = []
        self.setWindowTitle(f"Bitácoras de {nombre or 'el batch'}")
        # Como el resto de las ventanas: el tamaño lo pone la pantalla, que
        # en un portátil bajo dejaría los botones fuera del borde.
        self._build_ui(nombre, self._medir_la_pantalla(1180, 700))

    def _build_ui(self, nombre: str, densidad) -> None:
        from app.gui.csv_viewer import EmbeddedPdfViewer

        cuerpo = QVBoxLayout(self)
        margen = max(SPACE_S, densidad.window_margin)
        cuerpo.setContentsMargins(margen, margen, margen, margen)
        cuerpo.setSpacing(densidad.root_spacing)

        bitacoras = [r for r in self._registros if not r.es_separador]
        separadores = len(self._registros) - len(bitacoras)
        resumen = (
            f"«{nombre}»: {_plural(len(bitacoras), 'bitácora', 'bitácoras')} "
            f"en {_plural(len(self._registros), 'página', 'páginas')} del "
            "batch."
        )
        if separadores:
            resumen += (
                f" Las otras {separadores} son páginas separadoras: van en "
                "la lista porque ocupan página en el batch, pero no se "
                "indexan y se borran al terminar."
            )
        intro = QLabel(resumen)
        intro.setWordWrap(True)
        cuerpo.addWidget(intro)

        self.tabla = _tabla(
            self.COLUMNAS,
            "Cada página del batch con el número que ocupa dentro de él, el "
            "mismo que muestra Web Index. Al elegir una fila se abre su hoja "
            "al lado; el doble clic la lleva al visor.",
        )
        self._llenar(self._registros)
        # Después de llenarla, no antes: medir por contenido mientras entran
        # las filas obliga a Qt a repasar la columna entera en cada celda.
        size_columns_once(self.tabla, stretch_last=True)

        divisor = QSplitter(Qt.Orientation.Horizontal)
        divisor.setChildrenCollapsible(False)
        divisor.setHandleWidth(6)
        self.visor = EmbeddedPdfViewer(density=densidad)
        divisor.addWidget(self.visor)

        panel = QWidget()
        columna = QVBoxLayout(panel)
        columna.setContentsMargins(0, 0, 0, 0)
        columna.setSpacing(SPACE_S)
        columna.addLayout(
            self._fila_de_busqueda(
                self._PISTA,
                "Busca en la lista de bitácoras. Cada coincidencia abre su "
                "hoja en el visor; se recorren con ‹ y ›.",
            )
        )
        columna.addWidget(self.tabla, 1)
        divisor.addWidget(panel)
        divisor.setStretchFactor(0, _PANEL_PDF)
        divisor.setStretchFactor(1, _TABLA)
        cuerpo.addWidget(divisor, 1)
        self._divisor = divisor

        self.orden = ColumnSortController(self.tabla)
        self.orden.sortChanged.connect(self._olvidar_busqueda)
        self.tabla.itemSelectionChanged.connect(self._mostrar_la_elegida)
        self.tabla.itemDoubleClicked.connect(self._abrir_la_elegida)

        self._cargar_paginas(self._registros)

        fila = QHBoxLayout()
        fila.setSpacing(SPACE_S)
        fila.addStretch()
        self.boton_cerrar = QPushButton("Cerrar")
        self.boton_cerrar.clicked.connect(self.accept)
        fila.addWidget(self.boton_cerrar)
        cuerpo.addLayout(fila)

        if self.tabla.rowCount():
            self.tabla.selectRow(0)

    def showEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().showEvent(event)
        self._repartir_ancho()

    def _repartir_ancho(self) -> None:
        """El mismo reparto del visor de CSV: dos quintos para la página.

        Los factores de estiramiento solo gobiernan el espacio sobrante, y
        la tabla pide de ancho lo que suman sus columnas: sin repartirlo a
        mano la página se quedaba en su mínimo.
        """
        libre = max(0, self._divisor.width() - self._divisor.handleWidth())
        panel = libre * _PANEL_PDF // (_PANEL_PDF + _TABLA)
        self._divisor.setSizes([panel, libre - panel])

    def closeEvent(self, event) -> None:  # noqa: N802 - API Qt
        """Para el hilo de render antes de destruir el cuadro."""
        self.visor.shutdown()
        super().closeEvent(event)

    def done(self, result: int) -> None:  # noqa: D102 - API Qt
        # «Cerrar» y Escape terminan el diálogo sin evento de cierre.
        self.visor.shutdown()
        super().done(result)

    def _valores(self, registro: Registro) -> dict[str, str]:
        """Lo que la página tiene que decir, por columna.

        Se arma entero y la tabla se queda con lo que enseña: qué columnas
        son eso lo decide la lista de columnas importantes, y esto no tiene
        por qué saberlo. La fecha se convierte con la misma función que la
        manda a AirVault, así que la lista no puede prometer una distinta de
        la que se va a escribir.
        """
        pagina = str(registro.pagina_batch or registro.seq)
        if registro.es_separador:
            # No se le escribe nada, así que no tiene datos de índice: lo
            # único suyo es la etiqueta con la que se imprimió la divisoria.
            return {
                "Página": pagina,
                "Matrícula": registro.separador,
                "Estado": NOMBRE_SEPARADOR,
            }
        return {
            "Página": pagina,
            "Origen": _origen(registro),
            "Matrícula": registro.matricula,
            "Log Page": registro.log_number,
            "Vuelo": registro.flight_number,
            "End Date": fecha_airvault(registro.fecha),
            "Estado": _estado_de(registro, self._completado),
            "Avisos": _avisos_de(registro),
        }

    def _celdas(self, registro: Registro) -> tuple[str, ...]:
        """La fila de una página del batch, en el orden de las columnas."""
        valores = self._valores(registro)
        return tuple(valores.get(nombre, "") for nombre in self.COLUMNAS)

    def _llenar(self, paginas: Sequence[Registro]) -> None:
        self.tabla.setRowCount(0)
        self._paginas = list(paginas)
        for indice, registro in enumerate(self._paginas):
            fila = self.tabla.rowCount()
            self.tabla.insertRow(fila)
            for columna, texto in enumerate(self._celdas(registro)):
                item = QTableWidgetItem(texto)
                # La fila lleva encima a qué página del batch corresponde:
                # ordenar la mueve de sitio y el visor tiene que seguir
                # acertando.
                item.setData(Qt.ItemDataRole.UserRole, indice)
                if columna == 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter
                    )
                if registro.es_separador:
                    # El mismo gris con el que la lista de batches marca lo
                    # que no llega a existir en AirVault: la divisoria se
                    # borra al cerrar el batch.
                    pintar_celda_del_tema(item, "TEXT_TERTIARY")
                elif registro.estado is EstadoRegistro.ESCRITA:
                    # El mismo verde de la tabla de batches, y significa lo
                    # mismo: eso ya está escrito en AirVault.
                    pintar_celda_del_tema(item, "STATUS_OK")
                self.tabla.setItem(fila, columna, item)

    def _cargar_paginas(self, paginas: Sequence[Registro]) -> None:
        """Ubica en disco las hojas escaneadas de las que salió cada fila.

        La secuencia que recibe el visor tiene una entrada por página del
        batch, separadoras incluidas, para que su numeración sea la del
        batch. La separadora no sale de ninguna hoja escaneada: entra sin
        documento y el panel lo dice en vez de dejar a la vista la hoja de
        la fila anterior. Al buscador de documentos se le siguen dando solo
        las bitácoras, que son las únicas que el CSV registra.
        """
        from app.gui.csv_viewer import resolve_source_documents

        if self._csv is None:
            self.visor.load_paths([], [])
            return
        bitacoras = [r for r in paginas if not r.es_separador]
        filas = [
            {
                "file": registro.archivo_origen,
                "page": str(registro.pagina_origen),
            }
            for registro in bitacoras
        ]
        rutas, documentos, faltan = resolve_source_documents(self._csv, filas)
        encontradas = iter(rutas)
        secuencia = [
            (None, 0) if registro.es_separador
            else (next(encontradas), registro.pagina_origen)
            for registro in paginas
        ]
        self.visor.load_paths(documentos, faltan, secuencia)

    def _indice_elegido(self) -> int:
        """Página del batch de la fila resaltada, esté ordenada o no."""
        fila = self.tabla.currentRow()
        item = self.tabla.item(fila, 0) if fila >= 0 else None
        indice = item.data(Qt.ItemDataRole.UserRole) if item else None
        return indice if isinstance(indice, int) else -1

    def _mostrar_la_elegida(self) -> None:
        indice = self._indice_elegido()
        if 0 <= indice < len(self._paginas):
            self.visor.show_page(indice + 1)

    def _abrir_la_elegida(self, *_args) -> None:
        """El doble clic vuelve a traer la hoja, aunque ya fuera la actual."""
        self._mostrar_la_elegida()


class VistaPreviaBatches(_ListaBuscable):
    """En cuántos batches queda la entrega y qué lleva cada uno.

    Los que ya están en AirVault salen con su estado; los demás son los que
    se crearían al subir. Abrir esto no los prepara: la lista se calcula
    leyendo el índice de páginas y el CSV, y cerrarla no deja nada hecho.
    """

    COLUMNAS = ("Batch", "Páginas", "Bitácoras", "Estado")

    _PISTA = "Nombre del batch, número de páginas, estado…"

    def __init__(
        self,
        previstos: Sequence,
        csv: Path | str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._previstos = list(previstos)
        self._csv = Path(csv) if csv else None
        # Las ventanas de bitácoras que se hayan abierto desde aquí. Sin
        # esta referencia, Qt las destruiría al volver de este método.
        self._abiertas: list[BitacorasDelBatch] = []
        self.setWindowTitle("Vista previa de los batches")
        self._medir_la_pantalla(780, 520)
        self._build_ui()

    def _build_ui(self) -> None:
        cuerpo = QVBoxLayout(self)
        margen = max(SPACE_S, self._densidad.window_margin)
        cuerpo.setContentsMargins(margen, margen, margen, margen)
        cuerpo.setSpacing(self._densidad.root_spacing)

        por_subir = [p for p in self._previstos if not p.subido]
        bitacoras = sum(len(p.bitacoras) for p in self._previstos)
        intro = QLabel(
            f"{_plural(len(self._previstos), 'batch', 'batches')} con "
            f"{_plural(bitacoras, 'bitácora', 'bitácoras')} en total, "
            f"{len(por_subir)} sin subir todavía."
        )
        intro.setWordWrap(True)
        cuerpo.addWidget(intro)

        self.tabla = _tabla(
            self.COLUMNAS,
            "Batches de la entrega. Los que ya están en AirVault salen con "
            "su estado; los demás se crearían al subir.",
        )
        self.tabla.itemDoubleClicked.connect(self._abrir_bitacoras)
        self.tabla.itemSelectionChanged.connect(self._ajustar_boton)
        self._llenar()
        size_columns_once(self.tabla, stretch_last=True)
        cuerpo.addLayout(
            self._fila_de_busqueda(
                self._PISTA,
                "Busca en la lista de batches. Cada coincidencia selecciona "
                "su fila; se recorren con ‹ y ›.",
            )
        )
        cuerpo.addWidget(self.tabla, 1)
        self.orden = ColumnSortController(self.tabla)
        self.orden.sortChanged.connect(self._olvidar_busqueda)

        self.ayuda = QLabel(
            "Elija un batch para ver las bitácoras que lleva dentro."
        )
        pintar_del_tema(self.ayuda, lambda: f"color: {color_ayuda()};")
        self.ayuda.setWordWrap(True)
        cuerpo.addWidget(self.ayuda)

        fila = QHBoxLayout()
        fila.setSpacing(SPACE_S)
        self.boton_bitacoras = QPushButton("Ver las bitácoras…")
        self.boton_bitacoras.setEnabled(False)
        self.boton_bitacoras.setToolTip(
            "Abre las bitácoras del batch elegido, con la página que ocupa "
            "cada una y su hoja escaneada al lado."
        )
        self.boton_bitacoras.clicked.connect(self._abrir_bitacoras)
        fila.addWidget(self.boton_bitacoras)
        fila.addStretch()
        self.boton_cerrar = QPushButton("Cerrar")
        self.boton_cerrar.clicked.connect(self.accept)
        fila.addWidget(self.boton_cerrar)
        cuerpo.addLayout(fila)

        if self._previstos:
            self.tabla.selectRow(0)

    def _llenar(self) -> None:
        self.tabla.setRowCount(0)
        for indice, previsto in enumerate(self._previstos):
            fila = self.tabla.rowCount()
            self.tabla.insertRow(fila)
            celdas = (
                previsto.nombre or "(sin nombre)",
                str(previsto.paginas),
                str(len(previsto.bitacoras)),
                previsto.estado or "Por subir",
            )
            for columna, texto in enumerate(celdas):
                item = QTableWidgetItem(texto)
                item.setData(Qt.ItemDataRole.UserRole, indice)
                if columna == 0:
                    item.setToolTip(previsto.nombre)
                if columna in (1, 2):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter
                    )
                if previsto.subido:
                    pintar_celda_del_tema(item, "STATUS_OK")
                elif not previsto.existe:
                    # Gris es lo que todavía no existe en ninguna parte: el
                    # batch que solo está previsto.
                    pintar_celda_del_tema(item, "TEXT_TERTIARY")
                self.tabla.setItem(fila, columna, item)

    def _elegido(self):
        fila = self.tabla.currentRow()
        item = self.tabla.item(fila, 0) if fila >= 0 else None
        indice = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(indice, int) or indice >= len(self._previstos):
            return None
        return self._previstos[indice]

    def _ajustar_boton(self) -> None:
        self.boton_bitacoras.setEnabled(self._elegido() is not None)

    def _abrir_bitacoras(self, *_args) -> None:
        previsto = self._elegido()
        if previsto is None:
            return
        ventana = BitacorasDelBatch(
            previsto.nombre,
            previsto.registros,
            csv=self._csv or "",
            completado=bool(previsto.completado),
            parent=self,
        )
        self._abiertas.append(ventana)
        ventana.destroyed.connect(
            lambda *_a, v=ventana: self._olvidar(v)
        )
        ventana.mostrar()

    def _olvidar(self, ventana) -> None:
        """Suelta la ventana que se cerró, para no acumularlas."""
        if ventana in self._abiertas:
            self._abiertas.remove(ventana)

    def closeEvent(self, event) -> None:  # noqa: N802 - API Qt
        """Se lleva consigo las listas que se abrieron desde ella.

        Son ventanas de consulta sobre esta misma entrega: dejarlas sueltas
        cuando ya no está la lista de la que salieron mantendría el
        programa abierto por algo que nadie está mirando.
        """
        for ventana in list(self._abiertas):
            ventana.close()
        super().closeEvent(event)
