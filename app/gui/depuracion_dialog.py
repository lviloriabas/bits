"""Cuadro «Depurar páginas», compartido por la ventana principal y el visor.

Las dos vistas que muestran a la vez el CSV y su PDF ofrecen lo mismo, así
que el cuadro se construye una sola vez: los mismos textos, las mismas
casillas y el mismo conteo antes de borrar nada.

Cada criterio trae además la lista de lo que se llevaría. En los duplicados
se enseña el grupo entero de cada bitácora repetida, no solo las apariciones
sobrantes: para decidir cuál se va hay que ver también la que se queda, y a
veces la buena es la segunda.

Y de cada aparición se enseña lo que trae escrito: matrícula, fecha y vuelo.
Sin eso las dos apariciones de una bitácora repetida se distinguen solo por
el archivo y el número de página, que no dicen cuál es la buena. Este cuadro
es el único sitio donde se borran páginas de una ejecución, así que es aquí
donde tiene que estar todo lo que hace falta para elegir.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QHBoxLayout,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QScrollArea,
    QWidget,
)

from app.gui.responsive import fit_to_screen
from app.gui.exclusion_previa import PreviaExclusion
from app.reports.exclusiones import motivo_restauracion
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_S, on_accent_text, paleta
from app.gui.widgets import (
    TABLE_RADIUS,
    pintar_del_tema,
    window_stylesheet,
    zoom_overlay_qss,
    scrollbars_qss,
)
from app.validation.depuracion import (
    PaginaDepurable,
    ResumenDepuracion,
    contar_depuracion,
    grupos_duplicados,
    paginas_en_blanco,
    claves_depurables,
    paginas_depurables,
)

REVISAR_PAGINAS_TEXTO = "Duplicadas y en blanco"

DEPURAR_TOOLTIP = (
    "Compare las páginas repetidas o en blanco, elija cuáles excluir y "
    "restaure la última exclusión si necesita corregirla. Los originales se conservan."
)

# Lo que se contesta a quien intenta marcar la última aparición libre de una
# bitácora repetida. No es un error de uso: es la regla del descarte dicha
# donde se intenta romper.
_AVISO_GRUPO = (
    "De cada bitácora repetida tiene que quedar una página. Desmarque otra "
    "aparición y después marque esta."
)

# La clave de cada página viaja en el propio elemento del árbol: es lo que
# después se le pasa a ``depurar_claves`` y no depende de en qué fila quedó.
_CLAVE = Qt.ItemDataRole.UserRole

def _arbol_qss() -> str:
    """Las listas del cuadro, con los mismos tonos que las tablas.

    Son tablas de datos como las de las dos ventanas y tienen que verse igual:
    las mismas superficies y el mismo radio de 6 px. La hoja compartida solo
    nombra QTableView y QTableWidget, así que el árbol se queda con el estilo
    nativo (esquinas en pico y colores del sistema) si no se le repiten aquí
    los mismos valores.
    """
    c = paleta()
    return (
        "QTreeWidget {"
        f" background-color: {c.TABLE_BASE_BG};"
        f" color: {c.TABLE_TEXT};"
        f" selection-background-color: palette(highlight);"
        f" selection-color: {on_accent_text()};"
        f" border: 1px solid {c.PANE_BORDER};"
        f" border-radius: {TABLE_RADIUS}px; }}"
        "QTreeWidget::item { padding: 3px 2px; }"
        f"QTreeWidget::item:selected {{ background-color: palette(highlight); }}"
        f"QTreeWidget::branch {{ background-color: {c.TABLE_BASE_BG}; }}"
        f"QTreeWidget QHeaderView::section {{ background-color: {c.TABLE_HEADER_BG};"
        f" color: {c.TABLE_TEXT}; border: 0;"
        f" border-right: 1px solid {c.TABLE_GRID}; }}"
    )


def _texto_conteo(cantidad: int) -> str:
    if cantidad == 1:
        return "1 página"
    return f"{cantidad} páginas"


def _etiqueta(pagina: PaginaDepurable, sufijo: str = "") -> str:
    """Identifica cada pagina y nombra los datos que faltan."""
    return (
        f"Página {pagina.pagina}{sufijo}, archivo: {pagina.archivo}\n"
        f"Matrícula: {pagina.matricula or 'No disponible'}, "
        f"fecha: {pagina.fecha or 'No disponible'}, "
        f"vuelo: {pagina.vuelo or 'No disponible'}"
    )


class DepurarPaginasDialog(QDialog):
    """Elige qué páginas se quitan y enseña cuáles son antes de hacerlo."""

    def __init__(self, reports, parent: QWidget | None = None, *, run_dir=None) -> None:
        super().__init__(parent)
        self._reports = list(reports)
        self.restaurar = False
        self._run_dir = run_dir
        self._pagina_actual = None
        self._paginas = {p.clave: p for p in paginas_depurables(self._reports)}
        # El conteo por criterio no cambia mientras el cuadro está abierto:
        # se mide una vez sobre los reportes y las casillas solo eligen
        # cuáles de esas páginas entran en el total.
        self._disponibles = contar_depuracion(self._reports, True, True)
        self._grupos = grupos_duplicados(self._reports)
        self._blancas = paginas_en_blanco(self._reports)
        self.setWindowTitle("Revisar páginas repetidas y en blanco")
        # Como el resto de los cuadros: la pantalla decide el tamaño, que en
        # un portátil bajo el alto pedido deja los botones fuera del borde.
        self._density = fit_to_screen(self, 1040, 780)
        self._aplicar_hoja()
        self._build_ui()
        # La hoja lleva el fragmento de la densidad, así que la rehace el
        # propio cuadro cuando cambia el tema.
        gestor_tema().cambiado.connect(self._al_cambiar_tema)

    def _aplicar_hoja(self) -> None:
        """La hoja del cuadro, con los tonos y las medidas de ahora."""
        self.setStyleSheet(window_stylesheet(self._density.qss + zoom_overlay_qss()
                                            + scrollbars_qss("QScrollArea")))

    def _al_cambiar_tema(self, _nombre: str) -> None:
        """Rehace la hoja del cuadro con los tonos del tema nuevo."""
        self._aplicar_hoja()
        for arbol in (self.arbol_duplicados, self.arbol_blancas):
            arbol.setStyleSheet(_arbol_qss())

    def _build_ui(self) -> None:
        raiz = QVBoxLayout(self)
        margin = max(SPACE_S, self._density.window_margin)
        raiz.setContentsMargins(margin, margin, margin, margin)
        raiz.setSpacing(SPACE_S)
        scroll = QScrollArea()
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        contenido = QWidget()
        layout = QVBoxLayout(contenido)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_S)
        scroll.setWidget(contenido)
        raiz.addWidget(scroll, 1)
        intro = QLabel(
            "Compare las imágenes y marque las páginas que desea excluir de la entrega. "
            "Seleccionar una fila muestra su imagen; marcar su casilla la excluye. "
            "Puede conservar todas si necesita revisarlas después."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.check_duplicados = QCheckBox(
            f"Marcar repeticiones: {_texto_conteo(self._disponibles.duplicadas)}"
        )
        self.check_duplicados.setToolTip(
            "Marca las apariciones posteriores para excluirlas. La primera aparición "
            "solo indica el orden: compare las imágenes y cambie las marcas si lo necesita."
        )
        self.check_duplicados.setEnabled(bool(self._disponibles.duplicadas))
        self.check_duplicados.toggled.connect(self._marcar_duplicados)
        layout.addWidget(self.check_duplicados)

        self.arbol_duplicados = self._nuevo_arbol(
            "Cada bitácora repetida con sus apariciones y lo que trae escrita "
            "cada una (matrícula, fecha y vuelo). Marque la que sobra; una de "
            "cada grupo se queda siempre."
        )
        self._llenar_duplicados()
        self.arbol_duplicados.setMaximumHeight(150)
        layout.addWidget(self.arbol_duplicados, 1)

        self.check_blancas = QCheckBox(
            f"Páginas en blanco: {_texto_conteo(self._disponibles.en_blanco)}"
        )
        self.check_blancas.setToolTip(
            "Páginas que el procesamiento marcó como vacías, sin nada que "
            "leer en la región de la plantilla."
        )
        self.check_blancas.setEnabled(bool(self._disponibles.en_blanco))
        self.check_blancas.toggled.connect(self._marcar_blancas)
        layout.addWidget(self.check_blancas)

        self.arbol_blancas = self._nuevo_arbol(
            "Las páginas que quedaron vacías. Desmarque la que prefiera "
            "conservar."
        )
        self._llenar_blancas()
        self.arbol_blancas.setMaximumHeight(85)
        self.arbol_blancas.setVisible(bool(self._blancas))
        layout.addWidget(self.arbol_blancas, 1)

        self.contexto = QLabel("Seleccione una página para comparar sus imágenes.")
        self.contexto.setWordWrap(True)
        layout.addWidget(self.contexto)
        comparacion = QHBoxLayout()
        comparacion.addWidget(QLabel("Comparar con:"))
        self.comparar_combo = QComboBox()
        self.comparar_combo.setAccessibleName("Página con la que se compara la bitácora")
        self.comparar_combo.setMinimumContentsLength(15)
        self.comparar_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.comparar_combo.setEnabled(False)
        self.comparar_combo.currentIndexChanged.connect(lambda _: self._refrescar_vistas())
        comparacion.addWidget(self.comparar_combo, 1)
        layout.addLayout(comparacion)
        previews = QHBoxLayout()
        self.vista_elegida = PreviaExclusion("Página seleccionada")
        self.vista_comparada = PreviaExclusion("Página comparada")
        previews.addWidget(self.vista_elegida, 1)
        previews.addWidget(self.vista_comparada, 1)
        self.previa_elegida = self.vista_elegida.imagen
        self.previa_comparada = self.vista_comparada.imagen
        layout.addLayout(previews, 4)
        for arbol in (self.arbol_duplicados, self.arbol_blancas):
            arbol.currentItemChanged.connect(self._mostrar_previa)
            for tecla in ("Return", "Enter"):
                atajo = QShortcut(QKeySequence(tecla), arbol)
                atajo.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                atajo.activated.connect(lambda a=arbol: self._alternar_elegida(a))

        self.total_label = QLabel()
        pintar_del_tema(
            self.total_label, lambda: f"color: {paleta().TEXT_SECONDARY};"
        )
        self.total_label.setWordWrap(True)
        raiz.addWidget(self.total_label)
        efectos = QLabel(
            "Al aplicar se actualizarán los datos procesados. Los PDF de la entrega "
            "cambiarán cuando vuelva a generarlos. Los archivos originales se conservan."
        )
        efectos.setWordWrap(True)
        raiz.addWidget(efectos)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.boton_eliminar = self.buttons.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self.boton_eliminar.setText("Excluir páginas")
        self.boton_eliminar.setAutoDefault(False)
        self.buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Cancelar")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.boton_restaurar = self.buttons.addButton(
            "Restaurar última exclusión", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.boton_restaurar.setAutoDefault(False)
        motivo = motivo_restauracion(self._run_dir)
        self.boton_restaurar.setEnabled(not motivo)
        self.boton_restaurar.setToolTip(motivo or "Recupera los datos anteriores sin volver a leer las páginas.")
        self.boton_restaurar.clicked.connect(self._pedir_restauracion)
        raiz.addWidget(self.buttons)

        # Ninguna marcada de entrada: el cuadro se abre sin nada que borrar y
        # es quien lo abre el que elige, no el que descubre lo ya marcado.
        self._refrescar_total()

    def _pedir_restauracion(self):
        self.restaurar = True
        self.accept()

    def _alternar_elegida(self, arbol):
        item = arbol.currentItem()
        if item is not None and item.data(0, _CLAVE) is not None:
            item.setCheckState(0, Qt.CheckState.Unchecked
                              if item.checkState(0) == Qt.CheckState.Checked
                              else Qt.CheckState.Checked)

    def _mostrar_previa(self, item, _anterior=None):
        clave = item.data(0, _CLAVE) if item is not None else None
        self._pagina_actual = clave
        anterior = self.comparar_combo.currentData()
        self.comparar_combo.blockSignals(True)
        self.comparar_combo.clear()
        paginas = next((paginas for _, paginas in self._grupos
                        if any(p.clave == clave for p in paginas)), [])
        for pagina in paginas:
            if pagina.clave != clave:
                self.comparar_combo.addItem(f"Página {pagina.pagina}, archivo: {pagina.archivo}", pagina.clave)
        indice = self.comparar_combo.findData(anterior)
        self.comparar_combo.setCurrentIndex(max(0, indice) if self.comparar_combo.count() else -1)
        self.comparar_combo.setEnabled(self.comparar_combo.count() > 1)
        self.comparar_combo.blockSignals(False)
        if paginas:
            self.contexto.setText(f"Número de bitácora: {paginas[0].log_number:07d}. "
                                  f"Se encontraron {len(paginas)} páginas con este número de bitácora.")
        else:
            self.contexto.setText("Página detectada como en blanco. Revise la imagen antes de excluirla."
                                  if clave else "Seleccione una página para comparar sus imágenes.")
        self._refrescar_vistas()

    def _refrescar_vistas(self):
        excluidas = self.claves()
        for vista, clave in ((self.vista_elegida, self._pagina_actual),
                             (self.vista_comparada, self.comparar_combo.currentData())):
            pagina = self._paginas.get(clave)
            ruta = Path(self._reports[clave[0]].pdf_path) if pagina is not None else None
            vista.mostrar(pagina, ruta, excluida=clave in excluidas)

    def _nuevo_arbol(self, ayuda: str) -> QTreeWidget:
        arbol = QTreeWidget()
        arbol.setHeaderHidden(True)
        arbol.setToolTip(ayuda)
        arbol.setUniformRowHeights(False)
        arbol.setWordWrap(True)
        arbol.setMinimumHeight(75)
        arbol.setRootIsDecorated(True)
        arbol.setAlternatingRowColors(False)
        arbol.setStyleSheet(_arbol_qss())
        arbol.itemChanged.connect(self._al_cambiar_marca)
        return arbol

    def _nueva_hoja(
        self, pagina: PaginaDepurable, texto: str, padre
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem(padre, [texto])
        item.setData(0, _CLAVE, pagina.clave)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        return item

    def _llenar_duplicados(self) -> None:
        # Cada hoja nace con su casilla, y ponerla avisa de un cambio de
        # marca: durante la construcción eso llamaba al refresco del pie
        # antes de que el pie existiera. Se llena en silencio y el total se
        # escribe una vez, al final de ``_build_ui``.
        self.arbol_duplicados.blockSignals(True)
        try:
            self._llenar_grupos()
        finally:
            self.arbol_duplicados.blockSignals(False)

    def _llenar_grupos(self) -> None:
        for numero, paginas in self._grupos:
            cabeza = QTreeWidgetItem(
                self.arbol_duplicados,
                [f"Número de bitácora: {numero:07d}, encontrada en {len(paginas)} páginas"],
            )
            # La cabecera agrupa, no se elige: marcarla no querría decir
            # nada, porque borrar el grupo entero pierde la bitácora.
            cabeza.setFlags(Qt.ItemFlag.ItemIsEnabled)
            cabeza.setExpanded(True)
            for orden, pagina in enumerate(paginas):
                sufijo = " (primera aparición)" if orden == 0 else ""
                self._nueva_hoja(pagina, _etiqueta(pagina, sufijo), cabeza)

    def _llenar_blancas(self) -> None:
        self.arbol_blancas.blockSignals(True)
        try:
            for pagina in self._blancas:
                self._nueva_hoja(pagina, _etiqueta(pagina), self.arbol_blancas)
        finally:
            self.arbol_blancas.blockSignals(False)

    def _hojas(self, arbol: QTreeWidget):
        """Recorre las páginas de un árbol, estén donde estén."""
        for indice in range(arbol.topLevelItemCount()):
            item = arbol.topLevelItem(indice)
            if item.data(0, _CLAVE) is not None:
                yield item
            for hijo in range(item.childCount()):
                yield item.child(hijo)

    def _marcar(self, arbol: QTreeWidget, marcadas) -> None:
        """Deja marcadas exactamente ``marcadas`` sin disparar un refresco por hoja."""
        arbol.blockSignals(True)
        try:
            for item in self._hojas(arbol):
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if item.data(0, _CLAVE) in marcadas
                    else Qt.CheckState.Unchecked,
                )
        finally:
            arbol.blockSignals(False)
        self._refrescar_total()

    def _marcar_duplicados(self, activado: bool) -> None:
        """Al encender el criterio se marcan las apariciones sobrantes."""
        sobrantes = {
            pagina.clave
            for _numero, paginas in self._grupos
            for pagina in paginas
            if pagina.duplicada
        }
        self._marcar(self.arbol_duplicados, sobrantes if activado else set())

    def _marcar_blancas(self, activado: bool) -> None:
        self._marcar(
            self.arbol_blancas,
            {pagina.clave for pagina in self._blancas} if activado else set(),
        )

    def _al_cambiar_marca(self, item, _columna: int) -> None:
        if self._deshacer_si_vacia_el_grupo(item):
            return
        clave = item.data(0, _CLAVE)
        for arbol in (self.arbol_duplicados, self.arbol_blancas):
            arbol.blockSignals(True)
            for hoja in self._hojas(arbol):
                if hoja.data(0, _CLAVE) == clave:
                    hoja.setCheckState(0, item.checkState(0))
            arbol.blockSignals(False)
        self._refrescar_total()

    def _deshacer_si_vacia_el_grupo(self, item) -> bool:
        """Devuelve la marca que dejaría una bitácora repetida sin páginas.

        De cada grupo se va una sola aparición, la más nueva, y marcarlas
        todas borraría la bitácora entera de la ejecución. En vez de aceptar
        esa elección y corregirla por detrás al borrar, la marca vuelve
        atrás en el sitio y el pie dice por qué: quien elige ve lo que va a
        pasar. Las páginas en blanco cuelgan del árbol sin cabecera, así que
        no entran por aquí.
        """
        if item.checkState(0) != Qt.CheckState.Checked:
            return False
        grupo = item.parent()
        if grupo is None:
            return False
        hermanos = (grupo.child(indice) for indice in range(grupo.childCount()))
        if any(
            hermano.checkState(0) != Qt.CheckState.Checked
            for hermano in hermanos
        ):
            return False
        arbol = item.treeWidget()
        arbol.blockSignals(True)
        try:
            item.setCheckState(0, Qt.CheckState.Unchecked)
        finally:
            arbol.blockSignals(False)
        self._refrescar_total(_AVISO_GRUPO)
        return True

    def _refrescar_total(self, aviso: str = "") -> None:
        elegidas = claves_depurables(self._reports, self.claves())
        for arbol in (self.arbol_duplicados, self.arbol_blancas):
            arbol.blockSignals(True)
            for item in self._hojas(arbol):
                clave = item.data(0, _CLAVE)
                item.setCheckState(0, Qt.CheckState.Checked if clave in elegidas else Qt.CheckState.Unchecked)
                pagina = self._paginas[clave]
                estado = "Se excluirá" if clave in elegidas else "Se conservará"
                primera = item.parent() is not None and item.parent().child(0) is item
                item.setText(0, f"{_etiqueta(pagina, ' (primera aparición)' if primera else '')}\n{estado}")
                item.setToolTip(0, f"{_etiqueta(pagina)}\n{estado}")
            arbol.blockSignals(False)
        resumen = self.resumen()
        if resumen.total:
            texto = (
                f"Se excluirán {_texto_conteo(resumen.total)} de la entrega. "
                "Las páginas sin marcar se conservarán."
            )
        elif not self._disponibles.total:
            texto = "La ejecución no tiene páginas repetidas ni en blanco."
        else:
            texto = "Todas las páginas se conservarán. Marque las que desea excluir."
        self.total_label.setText(f"{aviso} {texto}" if aviso else texto)
        self.boton_eliminar.setEnabled(bool(resumen.total))
        self.boton_eliminar.setText(f"Excluir {_texto_conteo(resumen.total)}" if resumen.total else "Excluir páginas")
        self._refrescar_vistas()

    def _claves_de(self, arbol: QTreeWidget) -> set:
        return {
            item.data(0, _CLAVE)
            for item in self._hojas(arbol)
            if item.checkState(0) == Qt.CheckState.Checked
        }

    def hay_depurables(self) -> bool:
        """Si la ejecución trae algo que se pudiera quitar.

        Distingue las dos maneras de salir sin borrar nada: que no hubiera
        páginas repetidas ni vacías, o que las hubiera y no se marcara
        ninguna. Quien abre el cuadro merece leer cuál de las dos fue.
        """
        return bool(self._disponibles.total)

    def duplicados(self) -> bool:
        return self.check_duplicados.isChecked()

    def en_blanco(self) -> bool:
        return self.check_blancas.isChecked()

    def claves(self) -> set:
        """Las páginas elegidas, sin repetir la que cae por los dos criterios."""
        return self._claves_de(self.arbol_duplicados) | self._claves_de(
            self.arbol_blancas
        )

    def resumen(self) -> ResumenDepuracion:
        """Lo que se quitaría con lo marcado en este momento."""
        duplicadas = self._claves_de(self.arbol_duplicados)
        blancas = self._claves_de(self.arbol_blancas)
        return ResumenDepuracion(
            duplicadas=len(duplicadas),
            en_blanco=len(blancas),
            total=len(duplicadas | blancas),
        )
