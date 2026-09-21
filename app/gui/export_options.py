"""Opciones compactas de salida compartidas por las ventanas."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.airvault.config import (
    AIRVAULT_FILENAME,
    AirVaultConfig,
    guardar_csv_date_mode,
    guardar_paginas_por_batch,
)
from app.gui.memoria import SALIDA, recordar
from app.gui.widgets import (
    MultiSelectMenu,
    SpinBoxWithButtons,
    configure_combo_box,
    configure_menu_button,
)
from app.reports.csv_reporter import CSV_DATE_MONTH_END, CSV_DATE_SPECIFIC


class ExportOptionsGroup(QGroupBox):
    """Salida, separacion y fecha en dos filas compactas."""

    def __init__(
        self,
        parent: QWidget | None = None,
        raiz: Path | str | None = None,
    ) -> None:
        super().__init__("Salida", parent)
        self._ruta_preferencias = (
            Path(raiz) if raiz is not None else Path.cwd()
        ) / AIRVAULT_FILENAME
        # Una sola lectura del archivo portable para todas las
        # preferencias del cuadro: se abre una vez al construirlo.
        self._preferencias = AirVaultConfig.load(self._ruta_preferencias)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(4)

        main_row = QHBoxLayout()
        main_row.setSpacing(8)
        # La etiqueta describe la propiedad y el desplegable contiene el
        # valor. «PDF: Un solo PDF» repetía el sustantivo sin aclarar qué se
        # estaba eligiendo.
        pdf_label = QLabel("Formato:")
        main_row.addWidget(pdf_label)
        self.controls_indent = pdf_label.sizeHint().width() + main_row.spacing()
        self.output_mode_combo = QComboBox()
        self.output_mode_combo.addItem("Un solo PDF", True)
        self.output_mode_combo.addItem("Varios PDF", False)
        self.output_mode_combo.setToolTip(
            "El PDF único conserva todas las secciones; varios PDF crea un "
            "archivo por cada separación marcada."
        )
        configure_combo_box(self.output_mode_combo, 12)
        # Con qué formato se entrega es una decisión de quien entrega y no
        # del programa, igual que la política de fecha de al lado: se abre
        # en el último elegido. Se repone antes de conectar «_sync_parts»
        # porque reponer no es elegir y no tiene que mover la fila de abajo
        # dos veces; al final del constructor se ajusta una sola vez.
        recordar(SALIDA, "formato", self.output_mode_combo)
        main_row.addWidget(self.output_mode_combo, 1)

        main_row.addSpacing(8)
        main_row.addWidget(QLabel("Fecha:"))
        self.csv_date_mode_combo = QComboBox()
        # «Fin de mes» va primero porque es lo que se elige casi siempre: es
        # la fecha con la que se indexa, y el día exacto solo hace falta
        # cuando alguien quiere ver lo que leyó el OCR. Con qué opción abre
        # el desplegable no lo decide este orden, sino la última elegida.
        self.csv_date_mode_combo.addItem("Fin de mes", CSV_DATE_MONTH_END)
        self.csv_date_mode_combo.addItem("Día exacto", CSV_DATE_SPECIFIC)
        self.csv_date_mode_combo.setItemData(
            0,
            "Usa siempre el último día del mes reconocido.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.csv_date_mode_combo.setItemData(
            1,
            "Usa el día reconocido; si falta, usa el último día del mes.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.csv_date_mode_combo.setToolTip(
            "Cambia la fecha representada en el CSV sin volver a ejecutar OCR. "
            "El resultado OCR original se conserva."
        )
        configure_combo_box(self.csv_date_mode_combo, 14)
        # Cambiar de política es una decisión de quien entrega y vale para
        # todo lo que venga después, así que el programa abre en la última
        # elegida en vez de imponer una. Se restaura antes de conectar el
        # guardado para no reescribir el archivo al abrir la ventana, y
        # antes de que las ventanas conecten sus propias reacciones al
        # cambio: restaurar no es elegir, y no debe reescribir ningún CSV.
        guardada = self.csv_date_mode_combo.findData(
            self._preferencias.csv_date_mode or ""
        )
        if guardada >= 0:
            self.csv_date_mode_combo.setCurrentIndex(guardada)
        self.csv_date_mode_combo.currentIndexChanged.connect(
            self._guardar_csv_date_mode
        )
        main_row.addWidget(self.csv_date_mode_combo, 1)
        layout.addLayout(main_row)

        detail_row = QHBoxLayout()
        detail_row.setSpacing(8)
        detail_row.addSpacing(self.controls_indent)
        self._detail_row = detail_row
        self.separation_menu = MultiSelectMenu(self)
        self.matricula_check = self._checkable_action(
            "separar_matricula",
            "Matrícula",
            "Separa la entrega por matrícula.",
            checked=True,
        )
        self.mes_check = self._checkable_action(
            "separar_mes", "Mes", "Separa la entrega por mes."
        )
        self.discrepancias_check = self._checkable_action(
            "discrepancias",
            "Posibles discrepancias",
            "Agrega una sección con posibles discrepancias de firma.",
            checked=True,
        )
        self.errores_check = self._checkable_action(
            "errores",
            "Errores",
            "Genera errores.pdf con las páginas que requieren revisión manual.",
        )
        self.separation_button = QToolButton()
        self.separation_button.setText("Separación")
        self.separation_button.setToolTip(
            "Elegir cómo se separan los PDF y qué apartados adicionales salen."
        )
        configure_menu_button(self.separation_button, self.separation_menu)
        detail_row.addWidget(self.separation_button)

        # Las opciones de division forman un bloque contra el margen derecho,
        # alineado con los botones de accion de la fila inferior.
        detail_row.addStretch(1)
        self.partes_check = QCheckBox("Dividir cada")
        self.partes_check.setToolTip(
            "Reparte el PDF único en varias partes sin cortar secciones."
        )
        # La cantidad de páginas ya se conservaba y la casilla que la
        # enciende no, así que una entrega repartida volvía entera en el
        # siguiente arranque con el número de partes todavía escrito.
        recordar(SALIDA, "dividir", self.partes_check)
        detail_row.addWidget(self.partes_check)
        self.partes_spin = QSpinBox()
        self.partes_spin.setRange(10, 5000)
        self.partes_spin.setSingleStep(50)
        guardadas = self._preferencias.paginas_por_batch
        if guardadas is not None:
            self.partes_spin.setValue(guardadas)
        self.partes_spin.setSuffix(" pág.")
        self.partes_spin.setToolTip(
            "Páginas como máximo en cada parte, contando las separadoras"
        )
        self.partes_spin.valueChanged.connect(
            lambda cantidad: guardar_paginas_por_batch(
                self._ruta_preferencias, cantidad
            )
        )
        self.partes_control = SpinBoxWithButtons(self.partes_spin)
        detail_row.addWidget(self.partes_control)
        layout.addLayout(detail_row)

        self.output_mode_combo.currentIndexChanged.connect(self._sync_parts)
        self.partes_check.toggled.connect(self._sync_parts)
        self._sync_parts()

    def agregar_menu(self, boton: QToolButton) -> None:
        """Suma otro desplegable a la fila en la que ya va «Separación».

        Los botones con menú son la misma clase de control y llevan el mismo
        ancho, así que se leen como un juego. Repartidos en dos filas, y
        encima uno justo debajo del otro, parecían una columna partida por
        la mitad en vez de dos controles hermanos.
        """
        fila = self._detail_row
        fila.insertWidget(fila.indexOf(self.separation_button) + 1, boton)

    def _checkable_action(
        self, opcion: str, text: str, tooltip: str, checked: bool = False
    ):
        action = self.separation_menu.addAction(text)
        action.setCheckable(True)
        action.setChecked(checked)
        action.setToolTip(tooltip)
        # El valor de arriba es con el que nace la instalación; a partir de
        # la primera vez que alguien lo mueve manda lo que eligió.
        recordar(SALIDA, opcion, action)
        return action

    def _guardar_csv_date_mode(self, _index: int) -> None:
        """Conserva la política de fecha recién elegida."""
        guardar_csv_date_mode(
            self._ruta_preferencias, self.csv_date_mode()
        )

    def _sync_parts(self, *_args) -> None:
        single = self.un_solo_pdf()
        self.partes_check.setEnabled(single)
        self.partes_spin.setEnabled(single and self.partes_check.isChecked())

    def un_solo_pdf(self) -> bool:
        return bool(self.output_mode_combo.currentData())

    def set_un_solo_pdf(self, single: bool) -> None:
        index = self.output_mode_combo.findData(bool(single))
        if index >= 0:
            self.output_mode_combo.setCurrentIndex(index)

    def separar_por(self) -> list[str] | None:
        separator = []
        if self.matricula_check.isChecked():
            separator.append("avion")
        if self.mes_check.isChecked():
            separator.append("mes")
        return separator or None

    def csv_date_mode(self) -> str:
        return self.csv_date_mode_combo.currentData() or CSV_DATE_MONTH_END
