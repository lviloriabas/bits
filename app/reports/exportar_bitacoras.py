"""Copia de bitácoras sueltas a una carpeta elegida.

Es lo que se hace cuando alguien pide unas bitácoras concretas: se buscan por
número en el visor y se sacan sus páginas tal como se escanearon, sin
anotaciones ni separadores, a la carpeta que se elija. No toca la ejecución:
ni su CSV, ni su JSON, ni los PDF de su entrega.

Como el resto de las salidas, nunca pisa un archivo que ya esté en la
carpeta: un nombre ocupado se repite con sufijo (``2281909-2.pdf``).

Este módulo no depende de Qt; el visor lo corre en un hilo de fondo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from loguru import logger

from app.utils.io import sanitize_filename, unique_path
from app.vision.pdf_loader import PdfDocumentCache, copy_pdf_pages

_NUMERO_RE = re.compile(r"^\d{7}$")
# Caracteres de números en el nombre del PDF conjunto. Windows no admite
# rutas de más de 260 caracteres, y la carpeta elegida también cuenta.
_LARGO_NOMBRE = 100


@dataclass(frozen=True)
class Bitacora:
    """Una fila del CSV con el PDF del que sale su página.

    ``numero`` es el ``log_number`` tal como lo trae el CSV, legible o no;
    ``archivo`` es el nombre con el que el CSV conoce al PDF, que nombra la
    bitácora cuando su número no se pudo leer.
    """

    numero: str
    pdf_path: Path
    pagina: int
    archivo: str = ""


def nombre_de_bitacora(bitacora: Bitacora) -> str:
    """Nombre del PDF de una sola bitácora: su número.

    Sin número legible se nombra por el PDF y la página de donde sale, que es
    como se la ubica en la tabla.
    """
    numero = sanitize_filename(bitacora.numero.strip())
    if numero:
        return f"{numero}.pdf"
    base = Path(bitacora.archivo or bitacora.pdf_path.name).stem
    return f"{sanitize_filename(base)} p{bitacora.pagina}.pdf"


def tramos(numeros: Iterable[str]) -> list[tuple[str, int]]:
    """Los números legibles agrupados en tramos seguidos, de menor a mayor.

    Cada tramo es su texto y cuántas bitácoras abarca: ``2281907-2281909``
    son tres, ``2281915`` es una. Los repetidos cuentan una vez y los que no
    son de siete dígitos no entran, porque no se pueden ordenar ni encadenar.
    """
    valores = sorted({
        int(numero.strip()) for numero in numeros if _NUMERO_RE.match(numero.strip())
    })
    resultado: list[tuple[str, int]] = []
    inicio = anterior = None
    for valor in valores + [None]:
        if valor is not None and anterior is not None and valor == anterior + 1:
            anterior = valor
            continue
        if inicio is not None:
            texto = (
                f"{inicio:07d}" if inicio == anterior
                else f"{inicio:07d}-{anterior:07d}"
            )
            resultado.append((texto, anterior - inicio + 1))
        inicio = anterior = valor
    return resultado


def describir_numeros(
    numeros: Iterable[str], limite: int = 0, mas: str = "más"
) -> str:
    """Los números como se leen: un rango si son seguidos, si no uno a uno.

    ``2281907-2281909`` cuando van seguidos, ``2281907, 2281915`` cuando no,
    y las dos cosas mezcladas cuando hay tramos y sueltos. Con ``limite`` el
    texto se corta antes de pasarse de esos caracteres y dice cuántas quedan
    fuera; ``mas`` es la palabra de ese final, sin tilde en un nombre de
    archivo.
    """
    partes = tramos(numeros)
    total = sum(cuantas for _, cuantas in partes)
    texto = ""
    incluidas = 0
    for parte, cuantas in partes:
        candidato = f"{texto}, {parte}" if texto else parte
        if limite and texto and len(candidato) > limite:
            return f"{texto} y {total - incluidas} {mas}"
        texto = candidato
        incluidas += cuantas
    return texto


def nombre_conjunto(bitacoras: Sequence[Bitacora]) -> str:
    """Nombre del PDF que junta varias bitácoras: sus números.

    Un rango si son seguidos y la lista si no lo son. Muchas sueltas no caben
    en un nombre de archivo de Windows, así que a partir de cierto largo se
    dice cuántas faltan.
    """
    if len(bitacoras) == 1:
        return nombre_de_bitacora(bitacoras[0])
    numeros = describir_numeros(
        (b.numero for b in bitacoras), limite=_LARGO_NOMBRE, mas="mas"
    )
    return f"Bitacoras {numeros}.pdf" if numeros else "Bitacoras.pdf"


def ordenar(bitacoras: Iterable[Bitacora]) -> list[Bitacora]:
    """Por número de bitácora; las ilegibles al final, en el orden recibido.

    Es el orden del libro, el mismo de la entrega: quien pide un tramo lo
    lee de corrido aunque la tabla estuviera ordenada por otra columna.
    """
    lista = list(bitacoras)

    def clave(par: tuple[int, Bitacora]) -> tuple[int, int]:
        posicion, bitacora = par
        numero = bitacora.numero.strip()
        return (int(numero) if _NUMERO_RE.match(numero) else 1 << 30, posicion)

    return [bitacora for _, bitacora in sorted(enumerate(lista), key=clave)]


def exportar_bitacoras(
    bitacoras: Sequence[Bitacora],
    carpeta: Path,
    un_solo_pdf: bool = False,
    al_avanzar: Optional[Callable[[int, int], None]] = None,
    cancelado: Optional[Callable[[], bool]] = None,
) -> list[Path]:
    """Escribe las bitácoras en ``carpeta`` y devuelve los archivos creados.

    Con ``un_solo_pdf`` van todas en un archivo, ordenadas por número; si
    no, una por archivo. ``al_avanzar`` recibe (hechas, total) tras cada
    archivo, y ``cancelado`` se consulta entre uno y otro: lo ya escrito se
    queda, y se devuelve, porque cada archivo está completo por sí solo.
    """
    lista = ordenar(bitacoras)
    if not lista:
        return []
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    escritos: list[Path] = []
    with PdfDocumentCache(b.pdf_path for b in lista) as fuentes:
        if un_solo_pdf:
            destino = unique_path(carpeta / nombre_conjunto(lista))
            copy_pdf_pages(
                ((b.pdf_path, b.pagina) for b in lista), destino, sources=fuentes
            )
            escritos.append(destino)
            if al_avanzar is not None:
                al_avanzar(len(lista), len(lista))
        else:
            for hechas, bitacora in enumerate(lista, start=1):
                if cancelado is not None and cancelado():
                    break
                destino = unique_path(carpeta / nombre_de_bitacora(bitacora))
                copy_pdf_pages(
                    [(bitacora.pdf_path, bitacora.pagina)], destino, sources=fuentes
                )
                escritos.append(destino)
                if al_avanzar is not None:
                    al_avanzar(hechas, len(lista))
    logger.info(
        f"[Bitacoras] {len(lista)} bitácora(s) pedida(s), "
        f"{len(escritos)} archivo(s) escrito(s) en {carpeta}"
    )
    return escritos
