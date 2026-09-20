"""Esquemas pydantic del sistema de plantillas."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator


class FieldType(str, Enum):
    """Tipos de campo soportados por la plantilla."""

    OCR = "ocr"
    TEXT = "text"
    DATE = "date"
    SIGNATURE = "signature"
    CHECKBOX = "checkbox"


class OcrMode(str, Enum):
    """Cómo se lee el recorte de un campo con PaddleOCR (solo CPU).

    ``detect`` ejecuta el detector de texto y luego el reconocedor. El
    detector localiza la escritura dentro de la casilla, así que tolera
    bordes impresos, rótulos y márgenes sobrantes.

    ``line`` salta el detector y envía el recorte completo al reconocedor,
    que lo trata como una sola línea de texto. Medido sobre los recortes
    reales del pipeline: 646 ms por recorte con detector frente a 175 ms
    sin él (3.7x). Solo es equivalente cuando el recorte ya contiene
    exactamente un valor y nada más; en casillas con rótulo impreso o
    varias palabras el detector sigue siendo necesario.
    """

    DETECT = "detect"
    LINE = "line"


class AnclaCampo(BaseModel):
    """Campo clavado a las rayas impresas que lo delimitan.

    Las coordenadas relativas de un campo miden desde el borde del lienzo, y
    el borde del lienzo no es nada: depende de cómo cayó la hoja en el
    escáner. El formulario impreso, en cambio, es siempre el mismo. Medido
    sobre doce páginas de ocho libros, el borde fijo de un campo se separa de
    su raya impresa real entre 8 y 11 píxeles según el libro, mientras que la
    distancia entre dos rayas de la retícula varía un solo píxel.

    Así que el ancla ata **cada borde a su propia raya**, la más cercana, y
    guarda lo que sobra como una fracción del hueco hasta la raya siguiente.
    Atar los dos bordes a un mismo par de rayas parece más simple pero sale
    peor: cuando el par queda separado (``captain_signature`` abarca cuatro
    renglones) el error de detección de las dos rayas se amplifica sobre el
    borde, y el recorrido entre libros se queda en 9,6 px en vez de bajar a 3.

    En cada página se usan las rayas *de esa página*. El patrón de la retícula
    sirve para saber qué raya es cuál; no aporta ni un píxel a la posición.
    """

    raya_arriba: int = Field(
        ge=0, description="raya horizontal de referencia del borde superior",
    )
    arriba: float = Field(
        description="borde superior, en fracciones del hueco entre esa raya "
                    "y la siguiente (0 = justo sobre la raya)",
    )
    raya_abajo: int = Field(
        ge=0, description="raya horizontal de referencia del borde inferior",
    )
    abajo: float = Field(
        description="borde inferior, en fracciones del hueco entre esa raya "
                    "y la siguiente",
    )
    raya_izquierda: Optional[int] = Field(
        default=None, ge=0,
        description="raya vertical de referencia del borde izquierdo; sin "
                    "ella el campo conserva su x y su ancho relativos",
    )
    izquierda: float = Field(default=0.0)
    raya_derecha: Optional[int] = Field(default=None, ge=0)
    derecha: float = Field(default=0.0)

    @model_validator(mode="after")
    def _validate_rayas(self) -> "AnclaCampo":
        """Los bordes tienen que ir en orden y las rayas en pareja."""
        if self.raya_abajo < self.raya_arriba:
            raise ValueError(
                f"ancla con rayas horizontales invertidas: "
                f"{self.raya_arriba} -> {self.raya_abajo}"
            )
        if (self.raya_abajo == self.raya_arriba
                and self.abajo <= self.arriba):
            raise ValueError(
                f"ancla con bordes horizontales invertidos sobre la misma "
                f"raya: {self.arriba} -> {self.abajo}"
            )
        tiene_x = (self.raya_izquierda is not None,
                   self.raya_derecha is not None)
        if any(tiene_x) and not all(tiene_x):
            raise ValueError(
                "el ancla horizontal necesita las dos rayas verticales o "
                "ninguna"
            )
        if all(tiene_x):
            if self.raya_derecha < self.raya_izquierda:
                raise ValueError(
                    f"ancla con rayas verticales invertidas: "
                    f"{self.raya_izquierda} -> {self.raya_derecha}"
                )
            if (self.raya_derecha == self.raya_izquierda
                    and self.derecha <= self.izquierda):
                raise ValueError(
                    f"ancla con bordes verticales invertidos sobre la misma "
                    f"raya: {self.izquierda} -> {self.derecha}"
                )
        return self


class PatronReticula(BaseModel):
    """Posiciones de las rayas impresas del formulario, para nombrarlas.

    Es el patrón contra el que se casan las rayas detectadas en cada página,
    y su único cometido es decir qué raya es cuál: los índices que usan las
    anclas. La posición con la que se recorta sale siempre de la página que
    se está leyendo, nunca de aquí.
    """

    x: List[float] = Field(
        default_factory=list,
        description="rayas verticales, en fracciones del ancho",
    )
    y: List[float] = Field(
        default_factory=list,
        description="rayas horizontales, en fracciones del alto",
    )

    @field_validator("x", "y")
    @classmethod
    def _validate_orden(cls, value: List[float]) -> List[float]:
        """Los índices de las anclas son posiciones en esta lista."""
        if any(b <= a for a, b in zip(value, value[1:])):
            raise ValueError("las rayas del patrón deben ir en orden creciente")
        return value


class FieldTemplate(BaseModel):
    """Definición de un campo de la plantilla (coordenadas relativas 0-1)."""

    id: str
    type: FieldType = FieldType.OCR
    required: bool = False
    x: float = Field(ge=0.0, le=1.0, description="left relativa")
    y: float = Field(ge=0.0, le=1.0, description="top relativa")
    w: float = Field(gt=0.0, le=1.0, description="ancho relativo")
    h: float = Field(gt=0.0, le=1.0, description="alto relativo")
    regex: Optional[str] = None
    min_length: Optional[int] = Field(default=None, ge=0)
    max_length: Optional[int] = Field(default=None, ge=0)
    postprocess: Optional[str] = Field(
        default=None,
        description="Nombre de un postprocesador (matricula, date, digits)",
    )
    localize: Optional[str] = Field(
        default=None,
        description="Modo de localización antes del OCR: 'ink' sub-recorta "
                    "la región al extento de la tinta manuscrita",
    )
    ocr_mode: OcrMode = Field(
        default=OcrMode.DETECT,
        description="'detect' ejecuta detector + reconocedor (por defecto); "
                    "'line' salta el detector y lee el recorte como una "
                    "sola línea (3.7x más rápido, solo válido cuando el "
                    "recorte contiene un único valor)",
    )
    min_ink_ratio: float = Field(default=0.02, ge=0.0, le=1.0)
    max_ink_ratio: float = Field(default=0.90, ge=0.0, le=1.0)
    min_components: int = Field(default=2, ge=0)
    ink_delta: float = Field(
        default=80.0, ge=1.0, le=255.0,
        description="cuánto más oscuro que su propio papel debe ser un píxel "
                    "para contar como tinta de firma; al medirse contra el "
                    "fondo local no depende del gris de la fotocopia",
    )
    min_ink_peak: float = Field(
        default=0.12, ge=0.0, le=1.0,
        description="densidad local de tinta (fracción de una ventana del "
                    "alto del campo) a partir de la cual se declara la firma "
                    "presente",
    )
    max_empty_peak: float = Field(
        default=0.05, ge=0.0, le=1.0,
        description="densidad local de tinta por debajo de la cual el campo "
                    "se considera vacío; entre este umbral y min_ink_peak la "
                    "firma queda incierta",
    )
    min_ink_span: float = Field(
        default=0.30, ge=0.0, le=1.0,
        description="fracción del ancho con tinta que basta para dar por "
                    "presente una escritura poco densa pero repartida "
                    "(números de licencia manuscritos)",
    )
    min_ink_coverage: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="fracción del recorte entero que debe ser tinta para dar "
                    "por escrita una casilla, junto con min_ink_span. Si es "
                    "mayor que cero, un pico denso sin ese reparto queda "
                    "incierto. En una casilla "
                    "mucho más ancha que alta la extensión sola no distingue "
                    "una línea de texto de un sello compacto: el sello cruza "
                    "medio ancho pero apenas ensucia el recuadro. En 0 (por "
                    "defecto) no pide nada y la regla es la de siempre",
    )
    sig_present_conf: float = Field(
        default=0.45, ge=0.0, le=1.0,
        description="confianza mínima para confiar en que una firma está presente",
    )
    sig_absent_conf: float = Field(
        default=0.55, ge=0.0, le=1.0,
        description="confianza mínima para confiar en que una firma está ausente "
                     "(región limpia); por debajo se trata como incierta",
    )
    book_background: bool = Field(
        default=True,
        description="si una lectura incierta de este campo puede resolverse "
                    "contrastándola con el resto de la bitácora. Esa segunda "
                    "opinión compara densidades de tinta, así que solo vale "
                    "para los campos que se deciden por densidad; una casilla "
                    "que se decide por cuánto se reparte la tinta a lo largo "
                    "de la línea tiene que declararlo en false o la segunda "
                    "opinión le dará por escrito cualquier borrón denso",
    )

    ancla: Optional[AnclaCampo] = Field(
        default=None,
        description="rayas impresas que delimitan el campo. Con ancla, la "
                    "posición se recalcula en cada página a partir de sus "
                    "propias rayas y x/y/w/h solo quedan como respaldo para "
                    "las páginas donde la retícula no se identifica",
    )

    @model_validator(mode="after")
    def _validate_geometry(self) -> "FieldTemplate":
        """Evita que un campo salga del lienzo de la plantilla."""
        if self.x + self.w > 1.0:
            raise ValueError(
                f"campo '{self.id}' excede el ancho de la página "
                f"(x + w = {self.x + self.w:.6f})"
            )
        if self.y + self.h > 1.0:
            raise ValueError(
                f"campo '{self.id}' excede el alto de la página "
                f"(y + h = {self.y + self.h:.6f})"
            )
        if (
            self.type is FieldType.SIGNATURE
            and self.max_empty_peak > self.min_ink_peak
        ):
            raise ValueError(
                f"campo '{self.id}' tiene umbrales de firma invertidos "
                f"(max_empty_peak {self.max_empty_peak} > "
                f"min_ink_peak {self.min_ink_peak})"
            )
        return self

    @field_validator("regex")
    @classmethod
    def _validate_regex(cls, value: Optional[str]) -> Optional[str]:
        if value:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"regex inválida: {exc}") from exc
        return value

    def rect_pixels(self, width: int, height: int) -> Tuple[int, int, int, int]:
        """Convierte el rectángulo relativo a píxeles (left, top, right, bottom)."""
        left = int(self.x * width)
        top = int(self.y * height)
        right = int((self.x + self.w) * width)
        bottom = int((self.y + self.h) * height)
        return left, top, right, bottom


class Template(BaseModel):
    """Plantilla completa de un formulario."""

    name: str
    version: str = "1.0"
    page_size: List[int] = Field(default_factory=lambda: [2480, 3508])
    reference_image: Optional[str] = Field(
        default=None,
        description="Imagen canonica sobre la que se definieron los campos",
    )
    reference_dpi: int = Field(
        default=150, ge=72, le=600,
        description="Resolucion de la imagen canonica",
    )
    reticula: Optional[PatronReticula] = Field(
        default=None,
        description="rayas impresas del formulario, para identificar las de "
                    "cada página y colocar los campos anclados",
    )
    source_path: Optional[Path] = Field(default=None, exclude=True, repr=False)
    fields: List[FieldTemplate] = Field(default_factory=list)
    discrepancy_types: Optional[Dict[str, List[str]]] = Field(
        default=None,
        description="Discrepancias activas por tipo de entrada: null reclama "
                    "todo. Cada clave (vuelo, mantenimiento, correccion) lleva "
                    "los campos que se reclaman en ese tipo; lista vacia apaga "
                    "el tipo entero",
    )

    @field_validator("fields")
    @classmethod
    def _validate_unique_field_ids(
        cls, fields: List[FieldTemplate]
    ) -> List[FieldTemplate]:
        """Los IDs son claves de resultado y no pueden sobrescribirse."""
        ids = [field.id for field in fields]
        duplicates = sorted({field_id for field_id in ids if ids.count(field_id) > 1})
        if duplicates:
            raise ValueError(
                "IDs de campo duplicados: " + ", ".join(duplicates)
            )
        return fields

    def field(self, field_id: str) -> Optional[FieldTemplate]:
        """Devuelve un campo por su id, o None si no existe."""
        for field in self.fields:
            if field.id == field_id:
                return field
        return None

    def resolved_reference_image(self) -> Optional[Path]:
        """Resuelve la referencia sin convertir la plantilla en no portable."""
        if not self.reference_image:
            return None
        path = Path(self.reference_image)
        if path.is_absolute():
            return path
        if self.source_path is None:
            return path
        return (self.source_path.parent / path).resolve()
