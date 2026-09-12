"""Modelos de dominio compartidos por toda la aplicación."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class Status(str, Enum):
    """Estado de validación de una página o campo."""

    OK = "OK"
    WARNING = "WARNING"
    ERROR = "ERROR"


class OcrResult(BaseModel):
    """Línea de texto reconocida por el motor OCR."""

    text: str
    confidence: float = 0.0
    box: Optional[List[List[float]]] = None


class InkAnalysis(BaseModel):
    """Análisis de tinta de una región (para firmas/checkbox)."""

    ink_ratio: float = 0.0
    component_count: int = 0
    max_component_area: float = 0.0


class FieldResult(BaseModel):
    """Resultado de la extracción/validación de un campo en una página."""

    page_number: int
    field_id: str
    field_type: str
    value: Optional[str] = None
    raw_value: Optional[str] = None
    confidence: float = 0.0
    status: Status = Status.OK
    comment: str = ""
    source: str = "direct"
    inference_method: Optional[str] = None
    alternatives: List[str] = Field(default_factory=list)
    # Cuantas lecturas independientes respaldan este valor exacto. Lo llenan
    # los correctores cuando imponen un valor que la pagina no leyo o cuando
    # el consenso confirma una lectura propia que estaba marcada. None
    # significa que el valor se sostiene solo en la lectura directa. Con 0 o
    # 1 una inferencia no alcanza para indexarla sin mirar.
    votes: Optional[int] = None


class PageResult(BaseModel):
    """Resultado del procesamiento de una página completa."""

    page_number: int
    status: Status = Status.OK
    blank: bool = False
    skew_angle: float = 0.0
    alignment_quality: str = "ok"
    processing_ms: float = 0.0
    discrepancy: bool = False
    # Qué discrepancia se detectó, en una frase corta y en castellano
    # («Faltan firma de técnico y licencia de técnico»). La escribe
    # ``clasificar_lote`` junto con ``discrepancy``, y solo cuando esa
    # bandera queda en True: es el texto de la columna ``disc_reason`` del
    # CSV, que está vacía siempre que no haya nada que reclamar.
    discrepancy_note: str = ""
    discrepancy_fields: List[str] = Field(default_factory=list)
    # Evidencia de la palabra grande, con caja normalizada a la pagina.
    void_mark: Optional[OcrResult] = None
    date: Optional[str] = None
    # La fecha leida contradice una evidencia amplia de la ejecucion y no
    # pudo corregirse con seguridad. Se conserva para diagnostico, pero el
    # batch REVISAR no debe enviarla a AirVault.
    date_review: bool = False
    fields: List[FieldResult] = Field(default_factory=list)
    comment: str = ""
    # Metadatos efímeros del visor. Viajan desde los workers hasta la GUI,
    # pero no forman parte del JSON ni de ningún reporte persistente.
    preview_alignment: Optional[Dict[str, float]] = Field(
        default=None, exclude=True, repr=False
    )
    preview_boxes: Dict[str, List[float]] = Field(
        default_factory=dict, exclude=True, repr=False
    )
    # Decisión efímera tomada sobre la fila final del CSV. Permite que la
    # división de los PDF use exactamente la misma información local que
    # recibirá AirVault, sin convertirla en una columna ni persistirla.
    airvault_review: bool = Field(default=False, exclude=True, repr=False)
    # Discrepancia confirmada antes de clasificar las firmas. Se mantiene
    # separada de ``discrepancy`` porque la fija el corrector del libro y no
    # la clasificación de firmas, que reescribe esa otra en cada exportación.
    airvault_discrepancy: bool = Field(
        default=False, exclude=True, repr=False
    )

    def add_field(self, field: FieldResult) -> None:
        """Agrega un campo y recalcula el estado de la página."""
        self.fields.append(field)

    def worst_status(self) -> Status:
        """Devuelve el peor estado entre los campos de la página."""
        if self.status is not Status.OK:
            return self.status
        order = {Status.OK: 0, Status.WARNING: 1, Status.ERROR: 2}
        worst = max((f.status for f in self.fields), key=order.get,
                    default=Status.OK)
        return worst


class ValidationReport(BaseModel):
    """Reporte completo de validación de un documento PDF."""

    pdf_path: str
    # Nombre lógico con el que la fuente entró a la ejecución. ``pdf_path``
    # puede terminar en ``processed/archivo-2.pdf`` para abrir la copia exacta,
    # pero el CSV debe conservar ``archivo.pdf`` al volver a exportarse.
    source_name: Optional[str] = None
    template_name: str
    generated_at: str = Field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    processing_ms: float = 0.0
    calibration_ms: float = 0.0
    # Instante de arranque (epoch, ``time.time()``) para poder medir el reloj
    # real de un batch: con un proceso por archivo las bitácoras se solapan y
    # sumar ``processing_ms`` cuenta el mismo minuto una vez por archivo. No
    # viaja al JSON: es metadato de la ejecución, no del reporte.
    started_at: float = Field(default=0.0, exclude=True, repr=False)
    # Lo que tardó cada vuelta del documento: la lectura de páginas y la
    # comprobación de VOID, con cuántas hojas entraron en ella. La ventana
    # aprende de aquí cuánto pesa una hoja de VOID frente a una página para
    # repartir la barra y el tiempo restante. Tampoco viajan al JSON.
    pages_ms: float = Field(default=0.0, exclude=True, repr=False)
    review_pages: int = Field(default=0, exclude=True, repr=False)
    review_ms: float = Field(default=0.0, exclude=True, repr=False)
    cancelled: bool = False
    summary: Dict[str, int] = Field(default_factory=dict)
    pages: List[PageResult] = Field(default_factory=list)

    @property
    def source_filename(self) -> str:
        return self.source_name or Path(self.pdf_path).name
