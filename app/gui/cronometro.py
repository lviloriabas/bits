"""El panel de tiempos de una ejecución: transcurrido, restante y estimado.

Nació en la ventana principal, pegado a la barra de progreso, y de ahí sale a
este módulo porque la ventana de Web Reports enseña lo mismo: las tres cifras,
los mismos rótulos y el mismo recuadro. Copiarlo allá habría dejado dos
paneles que se parecen hasta que alguien toque uno de los dos.

Aquí vive solo el panel: qué cifras enseña y cómo se ven. De dónde salen los
segundos es asunto de quien lo usa, que es quien sabe qué está corriendo.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from app.gui.tokens import (
    FONT_CAPTION_PT,
    RADIUS_CARD,
    SPACE_M,
    SPACE_S,
    SPACE_XS,
    WEIGHT_STRONG,
    paleta,
)


def cronometro_qss() -> str:
    """El recuadro de las tres cifras, en los tonos del tema puesto ahora."""
    c = paleta()
    return f"""
#timeSummary {{
    background-color: {c.CONTROL_BG};
    border: 1px solid {c.STROKE};
    border-radius: {RADIUS_CARD}px;
}}
#timeSummary QLabel[role="caption"] {{
    color: {c.TEXT_SECONDARY};
    font-size: {FONT_CAPTION_PT}pt;
}}
#timeSummary QLabel[role="value"] {{
    color: {c.TEXT};
    font-size: {FONT_CAPTION_PT}pt;
    font-weight: {WEIGHT_STRONG};
}}
#timeSummary QFrame[role="metricDivider"] {{
    background-color: {c.STROKE_STRONG};
    border: 0;
}}
"""


# Las tres cifras, en el orden en que se leen: lo que va, lo que falta y lo
# que va a sumar todo. Las claves son las que usa ``actualizar``.
METRICAS = (
    ("elapsed", "Transcurrido"),
    ("remaining", "Restante"),
    ("total", "Estimado"),
)

# Lo que se enseña cuando esa cifra no se sabe. No es cero: es que todavía no
# hay nada que decir, y el hueco vacío haría saltar el ancho de la fila.
EN_CERO = "00:00:00"

# Ancho mínimo del panel. Los tres rótulos con sus relojes caben de sobra, y
# fijarlo evita que la barra de progreso de al lado se lo vaya comiendo.
ANCHO_MINIMO = 400


def formato_reloj(segundos: float) -> str:
    """Muestra una duración con precisión de segundos, como un cronómetro."""
    total_segundos = max(0, int(round(segundos)))
    horas, resto = divmod(total_segundos, 3600)
    minutos, segs = divmod(resto, 60)
    return f"{horas:02d}:{minutos:02d}:{segs:02d}"


class Cronometro(QFrame):
    """Las tres cifras en una fila, separadas por dos rayas finas.

    El alto se pide al construirlo porque este panel comparte fila con
    botones y barras: si no midiera lo mismo que ellos, la fila quedaría
    escalonada. Con la densidad compacta cambia, y para eso está
    ``fijar_alto``.
    """

    def __init__(self, alto: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("timeSummary")
        self.setMinimumWidth(ANCHO_MINIMO)
        self.etiquetas: dict[str, QLabel] = {}
        self.separadores: list[QFrame] = []

        fila = QHBoxLayout(self)
        fila.setContentsMargins(SPACE_M, 0, SPACE_M, 0)
        fila.setSpacing(SPACE_S)
        for indice, (clave, titulo) in enumerate(METRICAS):
            if indice:
                separador = QFrame(self)
                separador.setProperty("role", "metricDivider")
                fila.addWidget(
                    separador, alignment=Qt.AlignmentFlag.AlignVCenter
                )
                self.separadores.append(separador)
            metrica = QHBoxLayout()
            metrica.setSpacing(SPACE_XS)
            rotulo = QLabel(titulo)
            rotulo.setProperty("role", "caption")
            valor = QLabel(EN_CERO)
            valor.setProperty("role", "value")
            valor.setAlignment(Qt.AlignmentFlag.AlignCenter)
            metrica.addWidget(rotulo)
            metrica.addWidget(valor)
            fila.addLayout(metrica, 1)
            self.etiquetas[clave] = valor
        self.fijar_alto(alto)

    def fijar_alto(self, alto: int) -> None:
        """El alto de los controles con los que comparte fila.

        Los separadores van un píxel por dentro de cada borde, que es lo que
        los deja como una raya entre cifras y no como un corte del recuadro.
        """
        self.setFixedHeight(alto)
        for separador in self.separadores:
            separador.setFixedSize(1, alto - 2)

    def actualizar(
        self,
        transcurrido: float | None = None,
        restante: float | None = None,
        total: float | None = None,
    ) -> None:
        """Las tres cifras a la vez, sin mezclar estados o estimaciones."""
        valores = {
            "elapsed": transcurrido,
            "remaining": restante,
            "total": total,
        }
        for clave, valor in valores.items():
            self.etiquetas[clave].setText(
                formato_reloj(valor) if valor is not None else EN_CERO
            )
