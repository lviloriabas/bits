"""Configuración global de la aplicación (pydantic)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    """Parámetros de configuración del pipeline."""

    dpi: int = Field(default=200, ge=72, le=600,
                     description="DPI del renderizado PDF->imagen")
    blank_threshold: float = Field(
        default=15.0, ge=0.0,
        description="Varianza de grises bajo la cual la página se considera vacía",
    )
    deskew: bool = True
    align: bool = True
    remove_printed: bool = Field(
        default=True,
        description="Construir un mapa del fondo impreso idéntico en todas "
                    "las páginas para análisis de firmas y ranuras de fecha. "
                    "El OCR conserva la imagen original porque la escritura "
                    "repetida también puede aparecer en el consenso. Se "
                    "activa solo con >= 3 páginas por archivo.",
    )
    printed_ink_threshold: int = Field(
        default=185, ge=0, le=255,
        description="Umbral de gris para el mapa del fondo impreso: los "
                    "píxeles con gris < este valor presentes en >=60% de las "
                    "páginas alineadas se marcan como impresos. El mapa no "
                    "se aplica directamente al OCR, porque la escritura "
                    "repetida también puede aparecer en el consenso.",
    )
    signature_book_background: bool = Field(
        default=True,
        description="Contrastar las firmas que quedan inciertas con el resto "
                    "de la bitácora: la mediana de cada campo a lo largo del "
                    "libro es ese campo vacío, y las páginas ya resueltas "
                    "dicen cuánta tinta significa 'firmado' en este escaneo. "
                    "Solo revisa lo que quedó en duda; los veredictos firmes "
                    "no se tocan.",
    )
    signature_background_pages: int = Field(
        default=32, ge=4, le=60,
        description="Páginas del libro que se muestrean para construir ese "
                    "fondo. Cada una cuesta un renderizado. Medido sobre una "
                    "bitácora etiquetada: con 12 páginas ya se resuelve parte "
                    "de las dudas y con 32 se llega al máximo (no mejora con "
                    "40); por debajo de eso el resultado depende de si la "
                    "muestra pilla o no páginas sin firmar.",
    )
    crop_preprocess: bool = Field(
        default=True,
        description="Preprocesado de los recortes antes del OCR: localización "
                    "de la tinta (crop_to_ink) y reescalado (upscale_for_ocr). "
                    "Desactivarlo alimenta el motor con el recorte crudo, para "
                    "comparar motores/resultados (escritura a mano).",
    )
    date_slot_ocr: bool = Field(
        default=False,
        description="Lectura estructurada para day/month/year: segmentación de la "
                    "casilla en ranuras (según los separadores verticales "
                    "impresos) y OCR por carácter con restricciones "
                    "(dígitos, abreviatura de mes). Verifica siempre la "
                    "lectura global cuando la retícula está disponible.",
    )
    read_day: bool = Field(
        default=True,
        description="Leer el día de la fecha. Se apaga cuando la ejecución "
                    "se configura a fin de mes: ahí el día no llega a "
                    "ninguna salida, así que leerlo son tres recortes de "
                    "OCR por página que nadie mira (medido: 10% del tiempo "
                    "de proceso). La casilla sigue en la plantilla, porque "
                    "la retícula de la banda de fecha se detecta con ella; "
                    "lo que se salta es su lectura. El día lo completa "
                    "después el corrector con el último que cabe en la "
                    "secuencia del libro, como ya hacía con un día "
                    "ilegible. A cambio, la ejecución deja de poder "
                    "representarse con el día exacto y los retrocesos de "
                    "fecha solo se detectan a resolución de mes.",
    )
    date_dynamic_geometry: bool = Field(
        default=True,
        description="Ajuste dinámico por página de las casillas de fecha: "
                    "detecta la retícula impresa (bordes y separadores) en "
                    "la ventana de la plantilla y alinea el peine esperado "
                    "con una traslación/escala pequeñas. Si la retícula no "
                    "encaja, se conserva la geometría de plantilla.",
    )
    min_match_count: int = Field(
        default=10, ge=1,
        description="Mínimo de coincidencias ORB para aceptar la alineación",
    )
    crop_padding: float = Field(
        default=0.10, ge=0.0, le=0.5,
        description="Margen al recortar cada región, relativo al tamaño "
                    "del campo (no de la página)",
    )
    confidence_warning: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="Confianza OCR bajo la cual se emite WARNING",
    )
    ocr_engine: str = "paddle"
    ocr_lang: str = "en"
    date_engine_name: str = ""
    date_dpi: int = Field(
        default=300, ge=72, le=600,
        description="DPI del renderizado de la banda manuscrita de fecha. "
                    "Más resolución no es mejor: el reconocedor reescala el "
                    "recorte de todas formas, y a 600 DPI también se amplifica "
                    "el grano del escaneo. Medido sobre 40 páginas de un libro "
                    "real, comparando con la regla de que las fechas no "
                    "retroceden dentro del libro: a 300 DPI se resuelven 30 "
                    "fechas de 40 con 5 retrocesos de 29 pares, y a 600 se "
                    "resuelven 28 con 6 de 27; sobre las 10 páginas etiquetadas "
                    "300 acierta el año 8 veces y 600 lo acierta 7. Bajar hasta "
                    "200 sí perjudica: aparecen años inventados (2006, 2024) y "
                    "los retrocesos suben a 9 de 29. Además 300 cuesta un 20% "
                    "menos de tiempo y un cuarto de los píxeles que 600.",
    )
    ocr_rec_model: Optional[str] = Field(
        default="PP-OCRv5_mobile_rec",
        description="Modelo fijo de reconocimiento manuscrito de PaddleOCR.",
    )
    ocr_det_model: Optional[str] = Field(
        default="PP-OCRv6_medium_det",
        description="Modelo fijo de detección de manuscrito de PaddleOCR.",
    )
    template_dir: Path = Field(
        default=Path("templates"),
        description="Directorio donde se buscan plantillas",
    )
    output_dir: Path = Field(default=Path("output"))
    log_dir: Path = Field(default=Path("output/logs"))
    verify_fleet: bool = Field(
        default=False,
        description=(
            "Reclasificar las matrículas leídas contra la lista de aviones."
        ),
    )
    fleet_file: Path = Field(
        default=Path("fleet.json"),
        description="Archivo portable con la lista de aviones de la flota.",
    )
    book_matriculas_file: Path = Field(
        default=Path("book_matriculas.json"),
        description=(
            "Archivo JSON compacto y portable que recuerda la matrícula "
            "confirmada de cada libro."
        ),
    )
    book_fechas_file: Path = Field(
        default=Path("book_fechas.json"),
        description=(
            "Archivo JSON compacto y portable que recuerda la primera y la "
            "última fecha confirmadas de cada libro."
        ),
    )


def config_for_pdf(config: AppConfig, pdf_path: Path) -> AppConfig:
    """Ajusta resolución de trabajo y detalle al escaneo de cada PDF.

    La página completa nunca se interpola por encima de ``config.dpi``. La
    banda manuscrita sí aprovecha hasta ``date_dpi`` píxeles reales del PDF,
    pero tampoco se sobreamplía si el documento fuente tiene menos detalle.
    """
    from app.vision.pdf_loader import detect_dpi

    source_dpi = detect_dpi(Path(pdf_path), default=config.date_dpi)
    base_dpi = max(72, min(config.dpi, source_dpi))
    date_dpi = max(base_dpi, min(config.date_dpi, source_dpi))
    return config.model_copy(update={"dpi": base_dpi, "date_dpi": date_dpi})
