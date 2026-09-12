"""Coloca los campos sobre las rayas impresas de cada página.

Las coordenadas relativas de una plantilla miden desde el borde del lienzo,
y el borde del lienzo no significa nada: depende de cómo cayó la hoja en el
escáner y de qué página se tomó como referencia. El formulario impreso, en
cambio, es siempre el mismo. Medido a 200 dpi sobre doce páginas de ocho
libros distintos:

* el borde fijo de un campo se separa de su raya impresa real entre 8 y 11
  píxeles según el libro (desviación típica 2,5 a 3,9), que en un campo de
  firma de 40 px de alto es entre el 20% y el 28% de su altura;
* la distancia entre las dos rayas que delimitan ``pilot_signature`` es de
  40,6 px y varía **un** píxel entre esos ocho libros.

O sea que la retícula impresa es un dato mucho más firme que el lienzo. Este
módulo la mide en cada página y recoloca los campos anclados sobre ella.

Lo que hace y lo que no
-----------------------
El patrón de ``Template.reticula`` sirve **solo para nombrar** las rayas: dice
que la raya que está por ahí arriba es la número 12, y así las anclas pueden
referirse a ella. La posición con la que se recorta sale siempre de la raya
detectada en la página que se está leyendo. Ninguna otra página entra en el
cálculo, ni como media ni como mediana: si la retícula de una página no se
identifica, esa página se queda con las coordenadas de respaldo y lo dice,
en vez de heredar la posición de sus vecinas.

Coste
-----
``printed_structure`` ya se calcula hoy para la alineación, así que lo único
nuevo son los dos perfiles, la extracción de centros y el casado: 8,2 ms por
página a 200 dpi, el 0,165% de los 4,98 s que cuesta una página.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from loguru import logger

from app.templates.schema import FieldTemplate, Template
from app.vision.alignment import printed_structure

# Fracción del ancho (o del alto) que tiene que estar entintada para que una
# columna cuente como raya. Medido sobre nueve páginas de ocho libros: con
# 0,20 salen 40 rayas horizontales en todas y un núcleo de ~24 verticales
# presente en todas; subiendo a 0,30 se pierden dos tercios de las
# verticales y bajando a 0,15 entran renglones de escritura a mano.
UMBRAL_PERFIL = 0.20

# Distancia máxima, en fracciones del lienzo, para dar por casadas una raya
# detectada y una del patrón. A 200 dpi son unos 10 px: más que el desajuste
# que queda tras el ajuste (0,5 a 0,8 px medidos) y menos que la separación
# entre dos renglones del formulario.
TOLERANCIA = 0.006

# Rayas que tienen que casar para fiarse de la identificación. El formulario
# deja 40 horizontales y ~24 verticales, así que estos mínimos dan un margen
# holgado y a la vez descartan una página con la retícula rota.
MIN_RAYAS_Y = 24
MIN_RAYAS_X = 10

# Pasadas del ajuste por mínimos cuadrados. Converge en dos o tres desde la
# identidad, porque el desplazamiento de partida nunca pasa de 0,005 del
# lienzo y la escala se queda dentro del 1%.
_PASADAS = 4

# Arranques del ajuste. Hay que sembrar también la escala, no solo el
# desplazamiento: partiendo siempre de escala 1 el ajuste se quedaba en un
# mínimo local con la asignación corrida una raya, y dos de doce páginas
# casaban 26 y 30 rayas de 40 en vez de 40. Con estas semillas casan las
# cuarenta, y además sale más barato porque se corta en cuanto una acierta.
_SEMILLAS_ESCALA = (1.0, 0.995, 1.005, 0.990, 1.010)
_SEMILLAS_DESFASE = (0.0, -0.01, 0.01, -0.02, 0.02)


@dataclass(frozen=True)
class Reticula:
    """Rayas impresas de una página, ya identificadas contra el patrón.

    Attributes:
        y: Raya horizontal por índice del patrón, en fracciones del alto de
            *esta* página. Las que no casaron no están.
        x: Lo mismo para las verticales, en fracciones del ancho.
        fiable: Si casaron suficientes rayas en los dos ejes.
    """

    y: Dict[int, float] = dc_field(default_factory=dict)
    x: Dict[int, float] = dc_field(default_factory=dict)
    escala_y: float = 1.0
    escala_x: float = 1.0
    fiable: bool = False

    def borde(
        self, rayas: Dict[int, float], patron: Sequence[float],
        indice: int, fraccion: float, escala: float,
    ) -> Optional[float]:
        """Posición de un borde: su raya más lo que sobra, medido aquí.

        Lo que sobra se guarda como fracción del hueco hasta la raya
        siguiente, así que el hueco se toma de esta misma página. Si la raya
        siguiente no casó, se usa el hueco del patrón llevado a la escala que
        esta página mostró: sigue sin entrar ninguna otra página, solo la
        escala medida aquí.
        """
        if indice not in rayas:
            return None
        if indice + 1 in rayas:
            hueco = rayas[indice + 1] - rayas[indice]
        elif indice + 1 < len(patron):
            hueco = (patron[indice + 1] - patron[indice]) * escala
        elif indice - 1 in rayas:
            hueco = rayas[indice] - rayas[indice - 1]
        else:
            return None
        return rayas[indice] + fraccion * hueco


def centros_de_rayas(
    perfil: np.ndarray, umbral: float = UMBRAL_PERFIL
) -> np.ndarray:
    """Centro de cada tramo del perfil que supera el umbral, normalizado.

    Una raya impresa ocupa varios píxeles de ancho, y su centro es más
    estable que su primer píxel: el grosor cambia con el contraste del
    escaneo, el centro no.
    """
    if perfil.size == 0:
        return np.empty(0, dtype=np.float64)
    fuerte = perfil > umbral
    bordes = np.flatnonzero(np.diff(np.concatenate(([False], fuerte, [False]))))
    if bordes.size == 0:
        return np.empty(0, dtype=np.float64)
    inicios, finales = bordes[0::2], bordes[1::2]
    return (inicios + finales - 1) / 2.0 / float(perfil.size)


def perfiles_de_reticula(
    imagen: np.ndarray, estructura: Optional[np.ndarray] = None
) -> Tuple[np.ndarray, np.ndarray]:
    """Perfiles de tinta de retícula por eje, normalizados a 0-1.

    Args:
        imagen: Página en BGR o gris, ya endereza da.
        estructura: Salida de ``printed_structure`` si ya se calculó. La
            alineación la calcula igual para cada página, así que pasarla
            evita repetir los 25 ms que cuesta.

    Returns:
        ``(perfil_vertical, perfil_horizontal)``: el primero tiene un pico
        por cada raya vertical, el segundo por cada horizontal.
    """
    if estructura is None:
        estructura = printed_structure(imagen)
    alto, ancho = estructura.shape[:2]
    if alto == 0 or ancho == 0:
        return np.empty(0), np.empty(0)
    perfil_x = estructura.sum(axis=0) / (255.0 * alto)
    perfil_y = estructura.sum(axis=1) / (255.0 * ancho)
    return perfil_x, perfil_y


def registrar(
    detectadas: np.ndarray,
    patron: Sequence[float],
    escala: float = 1.0,
    desfase: float = 0.0,
) -> Tuple[float, float, np.ndarray]:
    """Lleva las rayas detectadas al sistema del patrón.

    Devuelve la escala, el desplazamiento y, para cada raya del patrón, el
    índice de la detectada que le corresponde, o -1 si ninguna cae dentro de
    la tolerancia.
    """
    return _ajusta(detectadas, np.asarray(patron, dtype=np.float64),
                   escala, desfase)


def _tolerancias(patron: np.ndarray) -> np.ndarray:
    """Tolerancia de casado de cada raya, según lo que la separa de su vecina.

    Una tolerancia fija no vale: abajo de la hoja los renglones se juntan
    hasta 0,0095 del lienzo, y con una tolerancia de 0,006 (más de media
    separación) la identificación se corre una raya. Medido: en dos de ocho
    libros la raya 34 acababa llamándose 35, y con ella se movía el campo
    anclado a esa raya.
    """
    if patron.size < 2:
        return np.full(patron.size, TOLERANCIA)
    huecos = np.diff(patron)
    vecino = np.minimum(
        np.concatenate([huecos[:1], huecos]),
        np.concatenate([huecos, huecos[-1:]]),
    )
    return np.minimum(TOLERANCIA, 0.35 * vecino)


def _alinear(
    movidas: np.ndarray, patron: np.ndarray, tolerancias: np.ndarray
) -> np.ndarray:
    """Alinea dos series ordenadas de rayas respetando el orden.

    El vecino más cercano decide cada raya por su cuenta, y eso permite que
    una raya no detectada renombre a todas las de abajo. Esta alineación es
    la de dos secuencias: puede saltarse una raya del patrón (no se detectó)
    o una detectada (una raya ajena, de un sello o una etiqueta) pagando una
    penalización, pero no puede cruzarlas. Así una raya que falta deja un
    hueco en vez de desplazar el resto.
    """
    n, m = patron.size, movidas.size
    if n == 0 or m == 0:
        return np.full(n, -1, dtype=np.int64)
    # El salto cuesta lo que costaría una pareja en el límite de tolerancia,
    # así que casar siempre gana mientras la raya esté dentro de tolerancia.
    salto = 1.0
    coste = np.full((n + 1, m + 1), np.inf)
    desde = np.zeros((n + 1, m + 1), dtype=np.int8)
    coste[0, 0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            actual = coste[i, j]
            if not np.isfinite(actual):
                continue
            if i < n and j < m:
                distancia = abs(patron[i] - movidas[j])
                if distancia < tolerancias[i]:
                    valor = actual + distancia / max(tolerancias[i], 1e-9)
                    if valor < coste[i + 1, j + 1]:
                        coste[i + 1, j + 1] = valor
                        desde[i + 1, j + 1] = 1
            if i < n and actual + salto < coste[i + 1, j]:
                coste[i + 1, j] = actual + salto
                desde[i + 1, j] = 2
            if j < m and actual + salto < coste[i, j + 1]:
                coste[i, j + 1] = actual + salto
                desde[i, j + 1] = 3
    pareja = np.full(n, -1, dtype=np.int64)
    i, j = n, m
    while i > 0 or j > 0:
        paso = desde[i, j]
        if paso == 1:
            pareja[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif paso == 2:
            i -= 1
        elif paso == 3:
            j -= 1
        else:
            break
    return pareja


def _ajusta(
    detectadas: np.ndarray, patron: np.ndarray, escala: float, desfase: float
) -> Tuple[float, float, np.ndarray]:
    """Refina escala y desplazamiento sobre una alineación que respeta el orden."""
    tolerancias = _tolerancias(patron)
    pareja = np.full(patron.size, -1, dtype=np.int64)
    for _pasada in range(_PASADAS):
        pareja = _alinear(detectadas * escala + desfase, patron, tolerancias)
        casan = pareja >= 0
        if int(casan.sum()) < 3:
            break
        # Mínimos cuadrados sobre las parejas alineadas, recortando las que se
        # salen. Dos parejas malas al final de la hoja bastaban para torcer el
        # ajuste de toda la página y dejar fuera de tolerancia a las buenas:
        # se pasaba de casar 40 rayas a casar 26.
        origen = detectadas[pareja[casan]]
        destino = patron[casan]
        peso = np.ones(origen.size, dtype=bool)
        nueva_escala, nuevo_desfase = escala, desfase
        for _recorte in range(2):
            base = np.column_stack(
                [origen[peso], np.ones(int(peso.sum()))]
            )
            solucion, *_ = np.linalg.lstsq(base, destino[peso], rcond=None)
            nueva_escala = float(solucion[0])
            nuevo_desfase = float(solucion[1])
            residuo = np.abs(origen * nueva_escala + nuevo_desfase - destino)
            corte = 2.5 * max(float(np.median(residuo)), 1e-9)
            siguiente = residuo <= corte
            if int(siguiente.sum()) < 3 or np.array_equal(siguiente, peso):
                break
            peso = siguiente
        if (abs(nueva_escala - escala) < 1e-9
                and abs(nuevo_desfase - desfase) < 1e-9):
            escala, desfase = nueva_escala, nuevo_desfase
            break
        escala, desfase = nueva_escala, nuevo_desfase
    return escala, desfase, pareja


def casar_rayas(
    detectadas: np.ndarray, patron: Sequence[float], minimo: int
) -> Tuple[Dict[int, float], float]:
    """Identifica qué raya detectada es cada raya del patrón.

    El casado se hace en el sistema del patrón (para poder comparar), pero lo
    que se devuelve es la posición **medida en la página**: el patrón nombra
    las rayas y se retira.

    Returns:
        ``({índice del patrón: posición en la página}, escala)`` para las
        rayas que casan, o ``({}, 1.0)`` si no llegan al mínimo. La escala es
        la que muestra esta página frente al patrón, y sirve para estimar un
        hueco cuando falta la raya siguiente.
    """
    patron_array = np.asarray(patron, dtype=np.float64)
    if detectadas.size == 0 or patron_array.size == 0:
        return {}, 1.0
    mejor: Tuple[int, np.ndarray, float] = (
        0, np.full(patron_array.size, -1, np.int64), 1.0,
    )
    # Basta con que una semilla case casi todas: se busca por orden de
    # probabilidad y se corta, así la página normal paga un solo ajuste.
    objetivo = max(minimo, patron_array.size - 2)
    for semilla_escala in _SEMILLAS_ESCALA:
        for semilla_desfase in _SEMILLAS_DESFASE:
            escala, _desfase, pareja = _ajusta(
                detectadas, patron_array, semilla_escala, semilla_desfase
            )
            casadas = int((pareja >= 0).sum())
            if casadas > mejor[0]:
                mejor = (casadas, pareja, escala)
            if mejor[0] >= objetivo:
                break
        if mejor[0] >= objetivo:
            break
    if mejor[0] < minimo:
        return {}, 1.0
    # La escala del ajuste lleva la página al patrón; el hueco se necesita en
    # el sentido contrario, del patrón a la página.
    inversa = 1.0 / mejor[2] if abs(mejor[2]) > 1e-9 else 1.0
    return {
        indice: float(detectadas[origen])
        for indice, origen in enumerate(mejor[1])
        if origen >= 0
    }, inversa


def leer_reticula(
    plantilla: Template,
    imagen: np.ndarray,
    estructura: Optional[np.ndarray] = None,
) -> Reticula:
    """Mide e identifica la retícula impresa de una página.

    Args:
        plantilla: Aporta el patrón con el que se nombran las rayas.
        imagen: La misma página de la que después se recortan los campos. Si
            la página se deformó para alinearla, hay que medir *después* de
            la deformación: así el resultado no depende de lo bien que saliera
            esa alineación.
        estructura: ``printed_structure`` ya calculada, si se tiene.

    Returns:
        La retícula de esta página. Con ``fiable`` en false, los campos
        anclados se quedan con sus coordenadas de respaldo.
    """
    if plantilla.reticula is None:
        return Reticula()
    perfil_x, perfil_y = perfiles_de_reticula(imagen, estructura)
    rayas_x, escala_x = casar_rayas(
        centros_de_rayas(perfil_x), plantilla.reticula.x, MIN_RAYAS_X
    )
    rayas_y, escala_y = casar_rayas(
        centros_de_rayas(perfil_y), plantilla.reticula.y, MIN_RAYAS_Y
    )
    fiable = bool(rayas_x) and bool(rayas_y)
    return Reticula(y=rayas_y, x=rayas_x, escala_y=escala_y,
                    escala_x=escala_x, fiable=fiable)


def rect_de_campo(
    campo: FieldTemplate, reticula: Reticula, plantilla: Template
) -> Optional[Tuple[float, float, float, float]]:
    """Rectángulo relativo del campo sobre las rayas de esta página.

    Returns:
        ``(x, y, w, h)`` en fracciones del lienzo, o None si el campo no está
        anclado, si la plantilla no trae patrón o si le falta alguna de sus
        rayas.
    """
    ancla = campo.ancla
    if ancla is None or plantilla.reticula is None:
        return None
    arriba = reticula.borde(
        reticula.y, plantilla.reticula.y, ancla.raya_arriba, ancla.arriba,
        reticula.escala_y,
    )
    abajo = reticula.borde(
        reticula.y, plantilla.reticula.y, ancla.raya_abajo, ancla.abajo,
        reticula.escala_y,
    )
    if arriba is None or abajo is None or abajo <= arriba:
        return None
    y, h = arriba, abajo - arriba

    x, w = campo.x, campo.w
    if ancla.raya_izquierda is not None and ancla.raya_derecha is not None:
        izquierda = reticula.borde(
            reticula.x, plantilla.reticula.x, ancla.raya_izquierda,
            ancla.izquierda, reticula.escala_x,
        )
        derecha = reticula.borde(
            reticula.x, plantilla.reticula.x, ancla.raya_derecha,
            ancla.derecha, reticula.escala_x,
        )
        if izquierda is not None and derecha is not None and derecha > izquierda:
            x, w = izquierda, derecha - izquierda

    # Un campo que se sale del lienzo rompe el recorte y la plantilla lo
    # rechaza en su validador, así que se recorta aquí: vale más un campo
    # pegado al borde que una página entera sin leer.
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    w = min(max(w, 1e-6), 1.0 - x)
    h = min(max(h, 1e-6), 1.0 - y)
    return x, y, w, h


def plantilla_ajustada(plantilla: Template, reticula: Reticula) -> Template:
    """Copia de la plantilla con los campos anclados puestos en su sitio.

    Devolver una plantilla en vez de rectángulos sueltos es lo que deja el
    resto del programa intacto: todo lo que lee un campo (el recorte, las
    ranuras de fecha, el OCR regional, el visor) pasa por ``rect_pixels``, y
    con la copia ajustada sigue pasando por ahí sin enterarse.

    Con una retícula no fiable devuelve la plantilla original: los campos se
    quedan donde dice la plantilla, que es el comportamiento de siempre.
    """
    if not reticula.fiable:
        return plantilla
    campos: List[FieldTemplate] = []
    movidos = 0
    for campo in plantilla.fields:
        rect = rect_de_campo(campo, reticula, plantilla)
        if rect is None:
            campos.append(campo)
            continue
        x, y, w, h = rect
        campos.append(campo.model_copy(update={"x": x, "y": y, "w": w, "h": h}))
        movidos += 1
    if not movidos:
        return plantilla
    return plantilla.model_copy(update={"fields": campos})
