"""Detección de firmas: solo presencia/ausencia de escritura.

La pregunta que responde este módulo es "¿hay algo escrito a mano en este
campo?", no "¿de quién es la firma?".

El recorte de un campo de firma nunca contiene solo la firma: trae la línea
impresa donde se firma, a veces un rótulo impreso ("CAPTAIN LICENSE No."),
los bordes de las columnas vecinas, la calca de la página anterior y el
gris irregular de una fotocopia. La estrategia son tres pasos que van
descartando todo lo que no es escritura:

1. **Nivel de papel local.** Un cierre morfológico con un kernel más ancho
   que un trazo devuelve, píxel a píxel, el blanco del papel *debajo* de la
   escritura. El residuo (papel − gris) mide cuánto oscurece la tinta a su
   propio fondo. Así una fotocopia gris, un sombreado o un degradado no
   aportan nada, porque el fondo se mueve con ellos; solo la tinta deja
   residuo. Esto sustituye al umbral global (Otsu) que degeneraba en los
   recortes casi uniformes y marcaba media región como tinta.

2. **Estructura impresa.** Se borran las reglas largas del formulario:
   aperturas morfológicas con kernels muy alargados dejan solo lo casi
   horizontal o casi vertical y suficientemente largo, que se descarta. Los
   trazos manuscritos, cortos y curvos, sobreviven.

3. **Densidad local de tinta.** Los rótulos impresos, los sellos y la calca
   de la página vecina sí dejan residuo, pero son trazos finos y dispersos.
   La escritura a mano es *densa*: concentra mucha tinta dentro de una
   ventana del tamaño del campo. El estadístico principal es por tanto la
   densidad máxima de tinta en una ventana cuadrada del alto del recorte
   (``peak``), que no depende de la resolución, del ancho del campo ni del
   nivel de gris del escaneo, y separa la escritura del ruido impreso por
   un margen amplio.

La decisión tiene tres estados:

- ``true``: densidad local >= ``min_ink_peak``; o densidad moderada
  (>= ``max_empty_peak``) repartida a lo largo de al menos ``min_ink_span``
  del ancho, que es la forma de un número de licencia: dígitos separados
  que nunca concentran tanta tinta como una rúbrica. En una casilla mucho
  más ancha que alta la extensión sola no basta —un sello compacto llega a
  cruzar más de medio ancho sin ser escritura—, así que el campo puede
  exigir además una cobertura mínima (``min_ink_coverage``): cuánta tinta
  hay en el recorte entero, no en su punto más denso. Si la exige, ni un
  pico alto confirma escritura sin alcanzar tambien la extension exigida.
- ``false``: densidad por debajo de ``max_empty_peak``, poca tinta total y
  ninguna evidencia ni siquiera con el umbral de tinta relajado.
- ``unclear`` (WARNING): todo lo demás, escritura que se sale del campo,
  sellos, calcas. No se acusa una falta: la clasificación de discrepancias
  lo anota como lectura incierta en el reporte y deja la página en el
  indexado automático, porque ninguna firma es un index field.

Los umbrales están elegidos con margen hacia el lado seguro: un falso
positivo esconde una firma que falta de verdad. Un ``unclear`` es más barato,
pero ya no es gratis: desde que las inciertas no van al batch manual, una
firma que de verdad falta y se lee ``unclear`` tampoco se reclama, y queda
solo anotada en el reporte de discrepancias.

Lo que queda en ``unclear`` tiene una segunda oportunidad: el pipeline lo
contrasta con el resto de la bitácora (``review_with_background``, apoyado
en ``app/vision/book_background.py``).
Este módulo mira cada recorte aislado y estima el papel con morfología; aquel
resta la mediana del mismo campo a lo largo del libro, que es ese campo
realmente vacío.

Medido sobre 394 recortes etiquetados a mano de dos bitácoras distintas, la
combinación baja las revisiones manuales de 12 a 1 **sin producir ningún
veredicto equivocado en ninguna de las dos**. Los veredictos firmes de aquí no
se revisan: la segunda opinión solo se pronuncia sobre lo que este módulo dejó
en duda, y solo cuando la evidencia del libro la sitúa fuera de toda duda.
"""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from app.models.schemas import FieldResult, Status
from app.templates.schema import FieldTemplate
from app.vision.book_background import drop_crossing_strokes as book_drop_crossing
from app.vision.book_background import ink_mask as book_ink_mask
from app.vision.book_background import peak_density as book_peak_density

# Estado de evidencia parcial: presente en el JSON como "unclear".
UNCLEAR = "unclear"

# Margen de recorte propio de los campos de firma, relativo al tamaño del
# campo. El margen vertical recupera los rasgos que se salen por arriba o
# por abajo de la casilla; el horizontal es nulo porque a los lados están
# las columnas vecinas del formulario, cuyo contenido impreso solo añade
# ruido (medido sobre 265 recortes etiquetados).
SIGNATURE_PAD_X = 0.0
SIGNATURE_PAD_Y = 0.10

# Umbral de tinta relajado, solo para *no* afirmar una ausencia: rescata la
# escritura clara sobre fotocopia oscura, que apenas supera el umbral normal.
_WEAK_INK_DELTA = 45.0
_WEAK_PEAK_GUARD = 0.08

# Cobertura total de tinta por encima de la cual una región no se declara
# vacía aunque la densidad local sea baja (sellos, recuadros, calcas).
_EMPTY_COVERAGE = 0.03

# Tinta repartida: una rúbrica grande de trazo fino deja poca densidad en
# cualquier ventana, pero la reparte a lo ancho del campo. Un campo vacío no
# hace eso: su ruido de escaneo se queda en una banda o en una esquina.
# Medido sobre 144 recortes etiquetados como ausentes, solo uno llega a esta
# pareja de valores; una firma con florituras que cruza la casilla la pasa
# con holgura. No afirma que haya firma (la densidad no da para tanto): solo
# impide afirmar que el campo está vacío.
_SPREAD_PEAK = 0.04
_SPREAD_SPAN = 0.30

# Margen del veredicto de vacío en las casillas que se deciden por reparto.
# Con el mismo baremo que la presencia, todo lo que no fuera presencia sería
# ausencia y no quedaría sitio para la duda. Por debajo de este margen la
# casilla está claramente sin escribir; entre un baremo y el otro se queda
# incierta, que es lo que la manda a REVISAR en vez de afirmar nada.
_REPARTO_MARGEN = 0.8

# DPI con el que están calibrados los kernels morfológicos y los umbrales.
# El recorte se lleva a esta escala antes de medirlo, para que la misma
# página dé el mismo veredicto tanto si se renderiza a 150 DPI (opción por
# defecto del CLI) como a 200 o 400.
_REFERENCE_DPI = 200


def _normalize_scale(gray: np.ndarray, dpi: int) -> np.ndarray:
    """Lleva el recorte a ``_REFERENCE_DPI`` para medirlo siempre igual.

    Los umbrales son densidades de tinta, pero el grosor de un trazo en
    píxeles sí depende de la resolución, y con él cuánta tinta sobrevive a
    las aperturas morfológicas. Reescalar primero es lo que hace que el
    detector no haya que recalibrarlo por cada DPI.
    """
    factor = _REFERENCE_DPI / max(dpi, 1)
    if abs(factor - 1.0) < 0.05:
        return gray
    return cv2.resize(
        gray, None, fx=factor, fy=factor,
        interpolation=cv2.INTER_AREA if factor < 1 else cv2.INTER_CUBIC,
    )


def _ink_channel(region: np.ndarray) -> np.ndarray:
    """Canal donde la tinta es más oscura que el papel, sea del color que sea.

    La tinta de bolígrafo casi nunca es neutra: un azul claro conserva el
    canal B alto y el gris lo aplana hasta confundirlo con el papel. Tomar
    el mínimo de los tres canales deja oscuro cualquier trazo que lo sea en
    *alguno* de ellos, y sobre un escaneo en grises (B≈G≈R) coincide con la
    conversión habitual.
    """
    if region.ndim != 3:
        return region
    return np.min(region, axis=2)


def _paper_level(gray: np.ndarray) -> np.ndarray:
    """Nivel de blanco del papel bajo la escritura.

    Un cierre morfológico con un kernel varias veces más ancho que un trazo
    rellena la escritura y deja el fondo: el papel, su gris de fotocopia y
    su sombreado, pero no la tinta.
    """
    return cv2.morphologyEx(
        gray, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (13, 13)),
    )


def _ink_mask(residual: np.ndarray, delta: float) -> np.ndarray:
    """Tinta del recorte sin las reglas impresas ni el ruido de escaneo.

    ``residual`` es cuánto se oscurece cada píxel respecto a su propio papel.
    Se marcan los que superan ``delta``, se restan las líneas largas del
    formulario (horizontales y verticales) y se abre la máscara para
    eliminar los speckles sueltos del escáner.
    """
    mask = (residual >= delta).astype(np.uint8)
    height, width = mask.shape

    horizontal = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, width // 3), 1)),
    )
    vertical = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(
            cv2.MORPH_RECT, (1, max(9, height * 2 // 3))
        ),
    )
    # La impresión ocupa 2-3 px por el antialiasing del escaneo.
    printed = cv2.dilate(
        cv2.bitwise_or(horizontal, vertical),
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
    )

    clean = cv2.bitwise_and(mask, cv2.bitwise_not(printed))
    return cv2.morphologyEx(
        clean, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )


def _peak_density(mask: np.ndarray) -> float:
    """Máxima fracción de tinta dentro de una ventana del alto del recorte.

    Es el rasgo que distingue escritura de ruido impreso: una rúbrica o unos
    dígitos manuscritos concentran tinta, mientras que un rótulo, un sello o
    la calca de la página vecina la reparten en trazos finos y sueltos.
    """
    height, width = mask.shape
    window = max(9, min(height, width))
    density = cv2.boxFilter(
        mask.astype(np.float32), -1, (window, window),
        normalize=True, borderType=cv2.BORDER_CONSTANT,
    )
    return float(density.max())


def _measure(region: np.ndarray, field: FieldTemplate, dpi: int) -> dict:
    """Rasgos de tinta del recorte (ver el docstring del módulo)."""
    gray = _normalize_scale(_ink_channel(region), dpi)
    gray = cv2.medianBlur(gray, 3)
    residual = cv2.subtract(_paper_level(gray), gray)

    mask = _ink_mask(residual, field.ink_delta)
    weak = _ink_mask(residual, _WEAK_INK_DELTA)
    return {
        "peak": _peak_density(mask),
        "weak_peak": _peak_density(weak),
        "coverage": float(mask.mean()),
        "span": float((mask.sum(axis=0) > 0).mean()),
        "dark_ratio": float(np.mean(gray < 110)),
    }


def _result(
    field: FieldTemplate,
    page_number: int,
    value: str,
    confidence: float,
    comment: str,
) -> FieldResult:
    if value == "true":
        status = Status.OK
    elif value == UNCLEAR:
        status = Status.WARNING
    else:
        status = Status.ERROR if field.required else Status.WARNING
    return FieldResult(
        page_number=page_number,
        field_id=field.id,
        field_type=field.type.value,
        source="vision",
        value=value,
        confidence=round(confidence, 3),
        status=status,
        comment=comment,
    )


def _classify(metrics: dict, field: FieldTemplate) -> tuple[str, float, str]:
    """Veredicto ("true"/"false"/"unclear"), confianza y motivo.

    Está separado de la medición porque la calibración de umbrales
    (``tools/signature_labeling/``) recorre miles de combinaciones sobre
    recortes ya medidos: al decidir con esta misma función, lo que se calibra
    es exactamente lo que después ejecuta el detector.
    """
    peak = metrics["peak"]
    span = metrics["span"]
    evidence = (
        f"densidad={peak:.4f} (umbral {field.min_ink_peak:.3f}), "
        f"extensión={span:.3f}, cobertura={metrics['coverage']:.4f}"
    )

    if metrics["dark_ratio"] > field.max_ink_ratio:
        # Región quemada o deteriorada: bajo tanta tinta uniforme no se puede
        # afirmar ni que hay firma ni que falta.
        return (
            UNCLEAR, 0.15,
            f"Firma indeterminable: la región está oscura o dañada "
            f"(tinta={metrics['dark_ratio']:.3f} > {field.max_ink_ratio:.3f})",
        )

    concentrated = peak >= field.min_ink_peak
    spread = (
        peak >= field.max_empty_peak
        and span >= field.min_ink_span
        and metrics["coverage"] >= field.min_ink_coverage
    )
    if concentrated and field.min_ink_coverage > 0.0 and not spread:
        # Las casillas que exigen reparto no se confirman por un pico:
        # un sello invasor puede ser mas denso que una correccion escrita.
        # Tampoco se declara vacio: podria ser texto corto o una rubrica.
        return (
            UNCLEAR, 0.40,
            f"Escritura incierta: tinta concentrada sin el reparto "
            f"exigido para esta casilla; {evidence}",
        )
    if concentrated or spread:
        margin = min(1.0, peak / (3.0 * field.min_ink_peak))
        return (
            "true", 0.55 + 0.45 * margin,
            "Firma detectada: "
            + ("trazo denso" if concentrated else "escritura repartida")
            + f"; {evidence}",
        )

    spread_ink = peak >= _SPREAD_PEAK and span >= _SPREAD_SPAN
    if field.min_ink_coverage > 0.0:
        # Casilla que se decide por reparto, no por densidad: el bloque de
        # corrección son renglones impresos de lado a lado, y su densidad de
        # tinta es la misma escrito que vacío (medido sobre 120 recortes
        # etiquetados: mediana 0,089 con escritura y 0,058 sin ella, con los
        # dos rangos solapados). Lo que sí los separa es que la escritura
        # cruza la casilla y la ensucia: extensión 0,51 contra 0,50 y
        # cobertura 0,027 contra 0,021 en sus percentiles de frontera. Así
        # que aquí el campo está vacío cuando la tinta no se reparte, no
        # cuando hay poca.
        empty = (
            (span < _REPARTO_MARGEN * field.min_ink_span
             or metrics["coverage"] < _REPARTO_MARGEN * field.min_ink_coverage)
            and not spread_ink
        )
    else:
        empty = (
            peak < field.max_empty_peak
            and metrics["coverage"] < _EMPTY_COVERAGE
            and metrics["weak_peak"] < _WEAK_PEAK_GUARD
            and not spread_ink
        )
    if empty:
        margin = 1.0 - min(1.0, peak / max(field.max_empty_peak, 1e-6))
        return (
            "false", 0.60 + 0.35 * margin,
            f"Firma ausente: el campo está vacío, no hay trazos de "
            f"escritura; {evidence}",
        )

    return (
        UNCLEAR,
        min(0.5, 0.2 + 0.3 * peak / max(field.min_ink_peak, 1e-6)),
        f"Firma incierta: hay tinta que no tiene forma de escritura "
        f"(sello, calca o trazo fuera del campo); {evidence}; "
        f"revisar manualmente",
    )


def background_peak(region: np.ndarray, background: np.ndarray) -> float:
    """Densidad de tinta escrita en esta casilla, sobre el fondo del libro.

    Entre medir y decidir hay un paso de atribución: la tinta que solo cruza
    la casilla (la X de una página anulada) se descuenta, porque está en la
    página pero no es de este campo.
    """
    return book_peak_density(
        book_drop_crossing(book_ink_mask(region, background))
    )


def review_with_background(
    peak: float, band: tuple
) -> Optional[tuple[str, float, str]]:
    """Segunda opinión sobre una firma incierta, con la evidencia del libro.

    Solo se pronuncia cuando el valor cae *fuera* de la franja de duda que
    dejaron las páginas ya resueltas de esta bitácora. Dentro de la franja
    devuelve None ("sigo sin saberlo") en lugar de partir por el medio: la
    firma incierta ya está a salvo en REVISAR, y convertirla en un veredicto
    equivocado sería el único cambio que empeora las cosas de verdad.

    Returns:
        ``(valor, confianza, comentario)`` o None si tampoco puede decidirlo.
    """
    empty_ceiling, signed_floor = band
    if peak >= signed_floor:
        distance = (peak - signed_floor) / max(signed_floor, 1e-6)
        return ("true", min(0.90, 0.72 + 0.18 * min(1.0, distance)),
                f"Firma detectada al comparar con el resto de la bitácora: "
                f"densidad={peak:.4f}, tanta como en sus páginas firmadas "
                f"(desde {signed_floor:.4f})")
    if peak <= empty_ceiling:
        distance = (empty_ceiling - peak) / max(empty_ceiling, 1e-6)
        return ("false", min(0.90, 0.72 + 0.18 * min(1.0, distance)),
                f"Firma ausente al comparar con el resto de la bitácora: "
                f"densidad={peak:.4f}, la de sus páginas sin firmar "
                f"(hasta {empty_ceiling:.4f})")
    return None


def detect_signature(
    region: np.ndarray,
    field: FieldTemplate,
    page_number: int,
    dpi: int = _REFERENCE_DPI,
) -> FieldResult:
    """Determina si hay escritura a mano en la región de un campo de firma.

    Args:
        region: Recorte BGR (o gris) del campo, ya alineado. Debe venir de la
            página *sin* la máscara de fondo impreso: esa máscara se calcula
            por consenso a media resolución y borra trozos de los trazos.
        field: Campo de la plantilla, con sus umbrales de tinta.
        page_number: Página a la que pertenece el recorte.
        dpi: Resolución a la que se renderizó el recorte. El recorte se lleva
            a la escala canónica antes de medirlo, de forma que el veredicto
            no dependa del DPI elegido para la ejecución.

    Returns:
        FieldResult con valor "true", "false" o "unclear" y su confianza.
    """
    value, confidence, comment = _classify(_measure(region, field, dpi), field)
    return _result(field, page_number, value, confidence, comment)
