"""Localiza letras grandes de VOID sin depender de su posicion en el formulario.

La geometria solo propone regiones. Para anular requisitos de firmas se
exigen dos lecturas de cuatro letras compatibles en la misma zona.

Es la etapa mas cara de la ejecucion: cada hoja con una posible discrepancia
manda decenas de recortes a un reconocedor mediano. Medido en este equipo
(12 nucleos): 0,28 s por recorte, seis recortes por zona y hasta seis zonas,
o sea unos 10 s por hoja, mas unos 7 s de carga del modelo la primera
vez en cada proceso. Por eso el modelo se carga una sola vez por proceso y,
cuando el pool de OCR esta libre, las hojas se reparten entre sus procesos:
medido, 4,6 s por hoja repartiendo frente a 10,6 s haciendolas de una en una.

Pedirle menos hilos al reconocedor no sirve de nada: mide igual con uno que
con doce (usa unos seis nucleos pase lo que pase), asi que el reparto solo
puede ser por hojas. Nada de esto cambia que marca se confirma.
"""

from concurrent.futures import FIRST_COMPLETED, wait
from pathlib import Path
import re
import threading

import cv2
import numpy as np

from app.core.progress import VOID_STAGE
from app.models.schemas import OcrResult

MODELO_VOID = "PP-OCRv6_medium_rec"
_ARCHIVOS_MODELO = ("inference.json", "inference.pdiparams", "inference.yml")
# Letras de varios centimetros: no hace falta mas resolucion para leerlas.
DPI_VOID = 120
# Tres giros por dos umbrales de tinta, en una sola llamada por zona: los
# recortes de una zona miden lo mismo y el reconocedor los agrupa sin relleno.
_RECORTES_POR_ZONA = 6
# Zonas que se llegan a leer, de mayor a menor. Eran doce y costaban el doble:
# en la muestra etiquetada las marcas que el reconocedor alcanza a leer salen
# siempre entre las cinco primeras, asi que las de mas solo gastaban tiempo.
_ZONAS_POR_HOJA = 6
# Hilos que se le piden al reconocedor. Los ignora, pero es lo declarado.
_HILOS_RECONOCEDOR = 4
# Lo que suma cada proceso del pool al cargar el modelo de VOID. Repartir la
# etapa solo se hace si cabe en la memoria libre para todos a la vez.
MEMORIA_MODELO_MB = 450

_MOTOR = None
_MOTOR_CERROJO = threading.Lock()


def candidatos(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 35, 15)
    lines = cv2.HoughLinesP(binary, 1, np.pi / 180, threshold=80,
                            minLineLength=w * .3, maxLineGap=8)
    mask = np.zeros_like(gray)
    if lines is not None:
        for x1, y1, x2, y2 in lines[:, 0]:
            if abs(y2-y1) < abs(x2-x1)*.07 or (abs(x2-x1) < abs(y2-y1)*.035 and abs(y2-y1) > h*.6):
                cv2.line(mask, (x1, y1), (x2, y2), 255, 2)
    clean = cv2.inpaint(gray, mask, 3, cv2.INPAINT_NS)
    results = []
    seen = []
    for threshold in [110, 0]:
        b = (cv2.threshold(clean, threshold, 255, cv2.THRESH_BINARY_INV)[1] if threshold else cv2.adaptiveThreshold(clean,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY_INV,55,12))
        b = cv2.morphologyEx(b, cv2.MORPH_CLOSE, np.ones((7,7),np.uint8))
        _, _, stats, _ = cv2.connectedComponentsWithStats(b)
        boxes = np.array([s[:4] for s in stats[1:] if h*.045 < s[3] < h*.45
                          and w*.006 < s[2] < w*.3 and s[4] > 40])
        if len(boxes) < 3:
            continue
        boxes = boxes[np.sort(np.argsort(-boxes[:, 3])[:60])]
        centers = boxes[:,:2] + boxes[:,2:]/2
        for i in range(len(boxes)):
            for j in range(i+1, len(boxes)):
                delta = centers[j] - centers[i]
                norm = np.linalg.norm(delta)
                if not w*.05 < norm < w*.5:
                    continue
                unit = delta / norm
                if unit[0] < 0:
                    unit = -unit
                normal = np.array([-unit[1], unit[0]])
                height = float(np.median([boxes[i,3], boxes[j,3]]))
                chosen = np.flatnonzero((abs((centers-centers[i]) @ normal) < height*.4)
                                       & (abs((centers-centers[i]) @ unit) < height*4))
                if len(chosen) < 3 or len(chosen) > 8:
                    continue
                pts = centers[chosen]
                u = pts @ unit
                v = pts @ normal
                cw = float(np.ptp(u) + height*1.8)
                ch = float(height*1.7)
                if cw < w*.15 or not 1.15 < cw/ch < 6:
                    continue
                center = unit*((u.min()+u.max())/2) + normal*((v.min()+v.max())/2)
                if any(np.linalg.norm(center-c) < height*.45 and abs(cw-oldw)<height
                       for c,oldw in seen):
                    continue
                seen.append((center,cw))
                angle = np.degrees(np.arctan2(unit[1],unit[0]))
                results.append((len(chosen)*height,center,cw,ch,angle))
    return sorted(results,key=lambda x:-x[0])[:_ZONAS_POR_HOJA]


def es_lectura_void(texto, confianza):
    """Cuatro letras completas; no acepta VOD, VOID dentro de una frase ni GOLD."""
    texto = re.sub(r"\s+", "", texto).upper()
    return confianza >= .80 and re.fullmatch(r"V[O0][I1L|/]D", texto) is not None


def modelo_disponible():
    carpeta = Path(__file__).resolve().parents[2] / "portable/paddlex/official_models" / MODELO_VOID
    return all((carpeta / archivo).is_file() for archivo in _ARCHIVOS_MODELO)


def motor_void():
    """El modelo debe estar precargado; nunca se descarga al procesar un PDF.

    Se crea una sola vez por proceso: cargarlo cuesta unos 7 s y antes se
    pagaba de nuevo en cada PDF del batch.
    """
    from app.ocr.engine import PaddleOcrEngine

    global _MOTOR
    if not modelo_disponible():
        raise RuntimeError("Falta precargar el modelo portable para leer VOID")
    with _MOTOR_CERROJO:
        if _MOTOR is None:
            _MOTOR = PaddleOcrEngine(
                cpu_threads=_HILOS_RECONOCEDOR, rec_model=MODELO_VOID
            )
    return _MOTOR


def _recortes(image, zona):
    _, center, cw, ch, angle = zona
    crops = []
    for delta in (0, -15, 15):
        matrix = cv2.getRotationMatrix2D(tuple(center), angle + delta, 1)
        matrix[0, 2] += cw / 2 - center[0]
        matrix[1, 2] += ch / 2 - center[1]
        crop = cv2.warpAffine(image, matrix, (round(cw), round(ch)), borderValue=(255, 255, 255))
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        for threshold in (110, 145):
            ink = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY_INV)[1]
            crops.append(255 - cv2.dilate(ink, np.ones((2, 2), np.uint8)))
    return crops


def detectar_void(image, engine, cancelado=lambda: False):
    """Busqueda finita en toda la hoja, zona por zona y con cancelacion.

    Las zonas se leen en una llamada cada una: la primera que se confirma
    detiene la busqueda, y los seis recortes de una zona miden lo mismo, que
    es como el reconocedor los agrupa sin desperdiciar relleno. Juntar varias
    zonas en una llamada se midio y salia peor (10,4 s por hoja frente a 9,6).
    """
    escala = min(1., 1800 / max(image.shape[:2]))
    if escala < 1:
        image = cv2.resize(image, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    h, w = image.shape[:2]
    lecturas = []
    for zona in candidatos(image):
        if cancelado():
            return None
        _, center, cw, ch, angle = zona
        for resultados in engine.recognize_lines(_recortes(image, zona)):
            for result in resultados:
                if not es_lectura_void(result.text, result.confidence):
                    continue
                cercanas = [(c, score) for c, score in lecturas
                            if np.linalg.norm(center - c) < ch * .6]
                if cercanas:
                    confianza = min(result.confidence, max(score for _, score in cercanas))
                    box = cv2.boxPoints((tuple(center), (cw, ch), angle))
                    box = (box / np.array([w, h])).tolist()
                    return OcrResult(text="VOID", confidence=confianza, box=box)
                lecturas.append((center, result.confidence))
    return None


def paginas_por_revisar(pages, template):
    """Hojas cuya discrepancia de firmas podria anular una marca VOID."""
    from app.validation.discrepancias import _clasificar_pagina, Categoria

    pendientes = []
    for page in pages:
        if page.blank or page.void_mark:
            continue
        resultado = _clasificar_pagina(page, template)
        if resultado and resultado[1] is Categoria.MISSING:
            pendientes.append(page)
    return pendientes


def comprobar_hoja_en_worker(pdf_path, page_number):
    """Tarea del pool: renderiza una hoja y busca su marca con el modelo del proceso.

    Abre y cierra el PDF en cada hoja: un documento que quedara abierto en un
    proceso del pool impediria moverlo a ``input/processed`` al terminar.
    """
    from app.vision.pdf_loader import render_page

    image = render_page(Path(pdf_path), page_number, DPI_VOID)
    return detectar_void(image, motor_void())


def _cabe_en_el_pool(pool):
    from app.core.parallelism import available_memory_mb, reserved_memory_mb

    libre = available_memory_mb()
    if libre <= 0:
        return True
    return libre - reserved_memory_mb() >= pool.max_workers * MEMORIA_MODELO_MB


def _anotar(page, logger):
    if page.void_mark:
        logger.info("[VOID] Pagina {}: marca confirmada; conserva su indice", page.page_number)


def _revisar_aqui(pdf_path, pendientes, renderer, avisar, cancelado, logger):
    from app.vision.pdf_loader import render_page

    try:
        engine = motor_void()
    except RuntimeError as exc:
        logger.warning("[VOID] {}; se mantienen las discrepancias", exc)
        return
    for hechas, page in enumerate(pendientes, start=1):
        if cancelado():
            return
        try:
            image = (renderer.render_page(page.page_number, DPI_VOID) if renderer
                     else render_page(pdf_path, page.page_number, DPI_VOID))
            page.void_mark = detectar_void(image, engine, cancelado)
            _anotar(page, logger)
        except Exception as exc:
            logger.warning("[VOID] Pagina {} sin confirmacion: {}", page.page_number, exc)
        avisar(hechas, len(pendientes), VOID_STAGE)


def _revisar_en_pool(pdf_path, pendientes, pool, avisar, cancelado, logger):
    por_numero = {page.page_number: page for page in pendientes}
    cola = list(por_numero)
    en_vuelo = {}
    hechas = 0
    # Una hoja por proceso y ninguna de mas: cada una cuesta decenas de
    # segundos, asi que encolar de sobra solo retrasaria la cancelacion.
    tope = max(1, pool.max_workers)
    while cola or en_vuelo:
        while cola and len(en_vuelo) < tope and not cancelado():
            numero = cola.pop(0)
            futuro = pool.executor.submit(
                comprobar_hoja_en_worker, str(pdf_path), numero
            )
            en_vuelo[futuro] = numero
        if cancelado():
            for futuro in en_vuelo:
                futuro.cancel()
            return
        listos, _ = wait(tuple(en_vuelo), timeout=0.20, return_when=FIRST_COMPLETED)
        for futuro in listos:
            page = por_numero[en_vuelo.pop(futuro)]
            try:
                page.void_mark = futuro.result()
                _anotar(page, logger)
            except Exception as exc:
                logger.warning("[VOID] Pagina {} sin confirmacion: {}", page.page_number, exc)
            hechas += 1
            avisar(hechas, len(pendientes), VOID_STAGE)


def revisar_voids(pdf_path, pages, template, renderer, avisar, cancelado, pool=None):
    """Relee posibles discrepancias antes de exigir una intervencion humana.

    ``avisar`` recibe ``(hojas revisadas, hojas por revisar, VOID_STAGE)``:
    son hojas de esta etapa, no paginas del documento. Con ``pool`` (el pool
    de OCR, libre cuando el documento se reparte por paginas) las hojas se
    leen en sus procesos si el modelo cabe en la memoria de todos.

    Devuelve cuantas hojas habia que revisar.
    """
    from loguru import logger

    pendientes = paginas_por_revisar(pages, template)
    if not pendientes or cancelado():
        return 0
    if not modelo_disponible():
        logger.warning("[VOID] Falta precargar el modelo portable para leer VOID; "
                       "se mantienen las discrepancias")
        return 0
    avisar(0, len(pendientes), VOID_STAGE)
    if pool is not None and len(pendientes) > 1 and _cabe_en_el_pool(pool):
        _revisar_en_pool(pdf_path, pendientes, pool, avisar, cancelado, logger)
    else:
        _revisar_aqui(pdf_path, pendientes, renderer, avisar, cancelado, logger)
    return len(pendientes)
