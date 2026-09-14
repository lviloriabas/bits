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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.gui.responsive import fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import SPACE_S, on_accent_text, paleta
from app.gui.widgets import (
    TABLE_RADIUS,
    TABLE_SELECTION_BG,
    pintar_del_tema,
    window_stylesheet,
)
from app.validation.depuracion import (
    PaginaDepurable,
    ResumenDepuracion,
    contar_depuracion,
    grupos_duplicados,
    paginas_en_blanco,
)

DEPURAR_TOOLTIP = (
    "Quita de la ejecución las páginas repetidas o en blanco. El CSV y el "
    "JSON se reescriben sin ellas."
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
    """Cómo se nombra una página en las listas del cuadro.

    Detrás del archivo y la página va lo que la bitácora dice de sí misma.
    Lo que no se leyó no se escribe: un hueco vacío entre dos guiones se
    lee como un dato y no lo es. Una página sin nada legible se queda con
    su nombre, que ya es toda la respuesta que hay.
    """
    escritos = [
        dato for dato in (pagina.matricula, pagina.fecha, pagina.vuelo)
        if dato
    ]
    cabeza = f"{pagina.archivo}, página {pagina.pagina}{sufijo}"
    return f"{cabeza} - {', '.join(escritos)}" if escritos else cabeza


class DepurarPaginasDialog(QDialog):
    """Elige qué páginas se quitan y enseña cuáles son antes de hacerlo."""

    def __init__(self, reports, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._reports = list(reports)
        # El conteo por criterio no cambia mientras el cuadro está abierto:
        # se mide una vez sobre los reportes y las casillas solo eligen
        # cuáles de esas páginas entran en el total.
        self._disponibles = contar_depuracion(self._reports, True, True)
        self._grupos = grupos_duplicados(self._reports)
        self._blancas = paginas_en_blanco(self._reports)
        self.setWindowTitle("Depurar páginas")
        # Como el resto de los cuadros: la pantalla decide el tamaño, que en
        # un portátil bajo el alto pedido deja los botones fuera del borde.
        self._density = fit_to_screen(self, 560, 560)
        self._aplicar_hoja()
        self._build_ui()
        # La hoja lleva el fragmento de la densidad, así que la rehace el
        # propio cuadro cuando cambia el tema.
        gestor_tema().cambiado.connect(self._al_cambiar_tema)

    def _aplicar_hoja(self) -> None:
        """La hoja del cuadro, con los tonos y las medidas de ahora."""
        self.setStyleSheet(window_stylesheet(self._density.qss))

    def _al_cambiar_tema(self, _nombre: str) -> None:
        """Rehace la hoja del cuadro con los tonos del tema nuevo."""
        self._aplicar_hoja()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        margin = max(SPACE_S, self._density.window_margin)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(SPACE_S)
        intro = QLabel(
            "Marque las páginas que quiere quitar. Se actualizarán el CSV, "
            "el JSON y las estadísticas; los PDF cambiarán cuando vuelva a "
            "exportar."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.check_duplicados = QCheckBox(
            f"Duplicados: {_texto_conteo(self._disponibles.duplicadas)}"
        )
        self.check_duplicados.setToolTip(
            "Bitácoras que aparecen más de una vez. Se quitan las "
            "repeticiones y se queda una; abajo puede elegir cuál."
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
        layout.addWidget(self.arbol_blancas, 1)

        self.total_label = QLabel()
        pintar_del_tema(
            self.total_label, lambda: f"color: {paleta().TEXT_SECONDARY};"
        )
        self.total_label.setWordWrap(True)
        layout.addWidget(self.total_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.boton_eliminar = self.buttons.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self.boton_eliminar.setText("Eliminar")
        self.buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Cancelar")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        # Ninguna marcada de entrada: el cuadro se abre sin nada que borrar y
        # es quien lo abre el que elige, no el que descubre lo ya marcado.
        self._refrescar_total()

    def _nuevo_arbol(self, ayuda: str) -> QTreeWidget:
        arbol = QTreeWidget()
        arbol.setHeaderHidden(True)
        arbol.setToolTip(ayuda)
        arbol.setUniformRowHeights(True)
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
                [f"{numero:07d}: {len(paginas)} apariciones"],
            )
            # La cabecera agrupa, no se elige: marcarla no querría decir
            # nada, porque borrar el grupo entero pierde la bitácora.
            cabeza.setFlags(Qt.ItemFlag.ItemIsEnabled)
            cabeza.setExpanded(True)
            for orden, pagina in enumerate(paginas):
                sufijo = " (primera)" if orden == 0 else ""
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
        resumen = self.resumen()
        if resumen.total:
            texto = (
                f"Se eliminarán {_texto_conteo(resumen.total)} de la ejecución."
            )
        elif not self._disponibles.total:
            texto = "La ejecución no tiene páginas repetidas ni en blanco."
        else:
            texto = "Marque al menos un criterio."
        self.total_label.setText(f"{aviso} {texto}" if aviso else texto)
        self.boton_eliminar.setEnabled(bool(resumen.total))

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
