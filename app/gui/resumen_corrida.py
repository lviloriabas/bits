"""Las cifras de una ejecución que el visor de CSV deja siempre a la vista.

La paginación del visor recorre las filas del CSV, que son las bitácoras, y
eso no dice cuántas páginas tiene lo que se entrega: el PDF de entrega lleva
además una página separadora delante de cada grupo, y así es como se sube a
AirVault. Las cifras de la entrega salen del índice que la ejecución deja
junto al CSV (``_paginas.json``), que describe cada página de cada PDF; las
de las bitácoras salen del CSV abierto.

Borrar páginas en el visor reescribe el CSV pero no los PDF, que se rehacen
al generar las salidas. Mientras tanto la entrega tiene otras bitácoras que
el CSV, y el resumen lo dice en lugar de sumar cifras de dos momentos.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# PDF que la ejecución deja en su carpeta y que no son la entrega.
_PDF_AUXILIARES = ("errores", "debug")


@dataclass(frozen=True)
class PdfDeEntrega:
    """Un PDF de la entrega y cuántas de sus páginas son separadores."""

    nombre: str
    paginas: int
    separadores: int

    @property
    def bitacoras(self) -> int:
        return self.paginas - self.separadores


@dataclass(frozen=True)
class ResumenCorrida:
    """Lo que cuenta el CSV abierto y lo que lleva su entrega.

    Las cifras del CSV son ``None`` cuando el archivo no trae la columna de
    la que salen: un CSV histórico no tiene ``review`` y decir «0 a revisar»
    sería afirmar algo que no se sabe. ``entrega`` es ``None`` cuando hay
    PDF en la carpeta pero ningún índice que diga qué lleva cada página
    (la entrega en varios PDF no lo escribe).
    """

    bitacoras: int
    aviones: int | None = None
    a_revisar: int | None = None
    duplicadas: int | None = None
    con_discrepancia: int | None = None
    sin_fecha: int | None = None
    sin_numero: int | None = None
    entrega: tuple[PdfDeEntrega, ...] | None = ()

    @property
    def paginas_de_entrega(self) -> int:
        return sum(pdf.paginas for pdf in self.entrega or ())

    @property
    def separadores(self) -> int:
        return sum(pdf.separadores for pdf in self.entrega or ())

    @property
    def bitacoras_de_entrega(self) -> int:
        return sum(pdf.bitacoras for pdf in self.entrega or ())

    @property
    def entrega_al_dia(self) -> bool:
        """La entrega lleva tantas bitácoras como el CSV.

        Basta con contarlas: lo que las separa es borrar o restaurar páginas
        sin volver a generar las salidas, y eso siempre cambia el número.
        """
        return not self.entrega or self.bitacoras_de_entrega == self.bitacoras


def _plural(cantidad: int, singular: str, plural: str) -> str:
    return f"{cantidad} {singular if cantidad == 1 else plural}"


def _marcadas(
    filas: Sequence[Mapping[str, str]], columnas: set[str], columna: str
) -> int | None:
    if columna not in columnas:
        return None
    return sum(
        1 for fila in filas if (fila.get(columna) or "").strip().lower() == "true"
    )


def _vacias(
    filas: Sequence[Mapping[str, str]], columnas: set[str], columna: str
) -> int | None:
    if columna not in columnas:
        return None
    return sum(1 for fila in filas if not (fila.get(columna) or "").strip())


def ruta_indice(csv_path: Path) -> Path:
    """Índice de la entrega de la ejecución a la que pertenece el CSV.

    Lleva el nombre de la ejecución, así que el CSV completo apunta al
    mismo índice que el mínimo.
    """
    from app.reports.organize import NOMBRE_INDICE_PAGINAS

    csv_path = Path(csv_path)
    stem = csv_path.stem
    if stem.casefold().endswith("_completo"):
        stem = stem[: -len("_completo")]
    return csv_path.with_name(f"{stem}{NOMBRE_INDICE_PAGINAS}")


def _entrega_del_indice(csv_path: Path) -> tuple[PdfDeEntrega, ...]:
    from app.airvault.mapping import leer_indice_paginas

    return tuple(
        PdfDeEntrega(
            nombre=str(parte.get("pdf") or ""),
            paginas=len(parte["paginas"]),
            separadores=sum(
                1 for pagina in parte["paginas"] if pagina.get("separador")
            ),
        )
        for parte in leer_indice_paginas(ruta_indice(csv_path))
    )


def _hay_pdf_de_entrega(run_dir: Path) -> bool:
    """Si la carpeta de la ejecución tiene algún PDF que no sea auxiliar."""
    try:
        return any(
            not ruta.stem.casefold().startswith(_PDF_AUXILIARES)
            for ruta in Path(run_dir).iterdir()
            if ruta.is_file() and ruta.suffix.lower() == ".pdf"
        )
    except OSError:
        return False


def resumir_corrida(
    filas: Sequence[Mapping[str, str]],
    csv_path: Path,
    run_dir: Path | None = None,
    columnas: Iterable[str] | None = None,
) -> ResumenCorrida:
    """Cifras del CSV abierto y de la entrega que salió de él.

    ``run_dir`` es la carpeta de la ejecución, si el CSV está en ella: solo
    ahí un PDF suelto es parte de la entrega y no un archivo cualquiera.
    """
    filas = list(filas)
    columnas_csv = set(columnas) if columnas is not None else set(
        filas[0] if filas else ()
    )
    entrega: tuple[PdfDeEntrega, ...] | None = _entrega_del_indice(csv_path)
    if not entrega and run_dir is not None and _hay_pdf_de_entrega(run_dir):
        entrega = None
    return ResumenCorrida(
        bitacoras=len(filas),
        aviones=(
            len({
                (fila.get("matricula") or "").strip().upper()
                for fila in filas
                if (fila.get("matricula") or "").strip()
            })
            if "matricula" in columnas_csv
            else None
        ),
        a_revisar=_marcadas(filas, columnas_csv, "review"),
        duplicadas=_marcadas(filas, columnas_csv, "dup"),
        con_discrepancia=_marcadas(filas, columnas_csv, "disc"),
        sin_fecha=_vacias(filas, columnas_csv, "date"),
        sin_numero=_vacias(filas, columnas_csv, "log_number"),
        entrega=entrega,
    )


def _linea_de_entrega(resumen: ResumenCorrida) -> str:
    bitacoras = _plural(resumen.bitacoras, "bitácora", "bitácoras")
    if resumen.entrega is None:
        return f"{bitacoras}."
    if not resumen.entrega:
        return f"{bitacoras}. Aún no hay PDF de entrega: use «Generar salidas»."
    paginas = _plural(resumen.paginas_de_entrega, "página", "páginas")
    pdfs = f"{len(resumen.entrega)} PDF de entrega"
    separadores = _plural(resumen.separadores, "separador", "separadores")
    if not resumen.entrega_al_dia:
        en_entrega = _plural(
            resumen.bitacoras_de_entrega, "bitácora", "bitácoras"
        )
        return (
            f"{bitacoras}; los {pdfs} ({paginas}, {en_entrega} y "
            f"{separadores}) son anteriores: use «Generar salidas»."
        )
    con = (
        f"{bitacoras} y {separadores}"
        if resumen.separadores
        else f"{bitacoras}, sin separadores"
    )
    return f"{con}: {paginas} en {pdfs}."


def _linea_de_bitacoras(resumen: ResumenCorrida) -> str:
    partes = []
    if resumen.aviones is not None:
        partes.append(_plural(resumen.aviones, "avión", "aviones"))
    if resumen.a_revisar is not None:
        partes.append(f"{resumen.a_revisar} a revisar")
    if resumen.duplicadas is not None:
        partes.append(_plural(resumen.duplicadas, "duplicada", "duplicadas"))
    if resumen.con_discrepancia is not None:
        partes.append(f"{resumen.con_discrepancia} con discrepancia")
    # Estas dos solo cuando las hay: son faltas, y un «0 sin fecha» en cada
    # ejecución limpia alarga la línea sin decir nada.
    if resumen.sin_fecha:
        partes.append(f"{resumen.sin_fecha} sin fecha")
    if resumen.sin_numero:
        partes.append(f"{resumen.sin_numero} sin número")
    return f"{', '.join(partes)}." if partes else ""


def texto_resumen(resumen: ResumenCorrida) -> str:
    """Las líneas que se ven debajo de la página."""
    lineas = [_linea_de_entrega(resumen), _linea_de_bitacoras(resumen)]
    return "\n".join(linea for linea in lineas if linea)


def detalle_entrega(resumen: ResumenCorrida) -> str:
    """Cada PDF de la entrega con lo que lleva, para la ayuda emergente."""
    return "\n".join(
        f"{pdf.nombre}: {_plural(pdf.paginas, 'página', 'páginas')} "
        f"({_plural(pdf.bitacoras, 'bitácora', 'bitácoras')}, "
        f"{_plural(pdf.separadores, 'separador', 'separadores')})"
        for pdf in resumen.entrega or ()
    )
