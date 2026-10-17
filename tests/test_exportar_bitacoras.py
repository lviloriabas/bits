"""Copia de bitácoras sueltas a una carpeta elegida."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from app.reports.exportar_bitacoras import (
    Bitacora,
    exportar_bitacoras,
    nombre_conjunto,
    nombre_de_bitacora,
)


def _pdf(path: Path, paginas: int) -> Path:
    """PDF de prueba cuya página N dice «pagina N», para reconocerla."""
    documento = fitz.open()
    for numero in range(1, paginas + 1):
        documento.new_page().insert_text((72, 72), f"pagina {numero}")
    path.parent.mkdir(parents=True, exist_ok=True)
    documento.save(str(path))
    documento.close()
    return path


def _textos(path: Path) -> list[str]:
    with fitz.open(str(path)) as documento:
        return [pagina.get_text().strip() for pagina in documento]


def test_cada_bitacora_sale_en_un_archivo_con_su_numero(tmp_path: Path):
    origen = _pdf(tmp_path / "batch.pdf", 3)
    destino = tmp_path / "pedidas"

    escritos = exportar_bitacoras(
        [Bitacora("2281909", origen, 3), Bitacora("2281907", origen, 1)],
        destino,
    )

    # Por número de bitácora, no en el orden en que se eligieron.
    assert [p.name for p in escritos] == ["2281907.pdf", "2281909.pdf"]
    assert _textos(destino / "2281907.pdf") == ["pagina 1"]
    assert _textos(destino / "2281909.pdf") == ["pagina 3"]


def test_un_solo_pdf_las_junta_en_orden_de_libro(tmp_path: Path):
    origen = _pdf(tmp_path / "batch.pdf", 3)

    escritos = exportar_bitacoras(
        [
            Bitacora("2281909", origen, 3),
            Bitacora("2281907", origen, 1),
            Bitacora("2281908", origen, 2),
        ],
        tmp_path / "pedidas",
        un_solo_pdf=True,
    )

    assert [p.name for p in escritos] == ["Bitacoras 2281907-2281909.pdf"]
    assert _textos(escritos[0]) == ["pagina 1", "pagina 2", "pagina 3"]


def test_las_bitacoras_de_varios_pdf_salen_de_cada_uno(tmp_path: Path):
    uno = _pdf(tmp_path / "uno.pdf", 2)
    otro = _pdf(tmp_path / "otro" / "uno.pdf", 4)

    escritos = exportar_bitacoras(
        [Bitacora("1000001", uno, 2), Bitacora("1000002", otro, 4)],
        tmp_path / "pedidas",
        un_solo_pdf=True,
    )

    assert _textos(escritos[0]) == ["pagina 2", "pagina 4"]


def test_un_nombre_ocupado_no_se_pisa(tmp_path: Path):
    origen = _pdf(tmp_path / "batch.pdf", 2)
    destino = tmp_path / "pedidas"
    destino.mkdir()
    (destino / "2281907.pdf").write_bytes(b"de antes")

    escritos = exportar_bitacoras(
        # La misma bitácora escaneada dos veces tampoco se pisa a sí misma.
        [Bitacora("2281907", origen, 1), Bitacora("2281907", origen, 2)],
        destino,
    )

    assert (destino / "2281907.pdf").read_bytes() == b"de antes"
    assert [p.name for p in escritos] == ["2281907-2.pdf", "2281907-3.pdf"]


def test_sin_numero_legible_se_nombra_por_su_pdf_y_pagina():
    bitacora = Bitacora("", Path("x/BITS_0828.pdf"), 4, "BITS_0828.pdf")

    assert nombre_de_bitacora(bitacora) == "BITS_0828 p4.pdf"
    # Lo que no puede ir en un nombre de archivo de Windows se reemplaza.
    assert nombre_de_bitacora(Bitacora("22/19?09", Path("a.pdf"), 1)) == "22_19_09.pdf"


def test_nombre_del_pdf_conjunto():
    a = Bitacora("2281907", Path("a.pdf"), 1)
    b = Bitacora("2281909", Path("a.pdf"), 2)
    ilegible = Bitacora("", Path("a.pdf"), 3)

    assert nombre_conjunto([a]) == "2281907.pdf"
    assert nombre_conjunto([b, a]) == "Bitacoras 2281907-2281909.pdf"
    assert nombre_conjunto([a, a]) == "Bitacoras 2281907.pdf"
    assert nombre_conjunto([ilegible, ilegible]) == "Bitacoras.pdf"


def test_cancelar_deja_completo_lo_ya_escrito(tmp_path: Path):
    origen = _pdf(tmp_path / "batch.pdf", 3)
    avances: list[tuple[int, int]] = []

    escritos = exportar_bitacoras(
        [Bitacora(f"100000{n}", origen, n) for n in (1, 2, 3)],
        tmp_path / "pedidas",
        al_avanzar=lambda hechas, total: avances.append((hechas, total)),
        cancelado=lambda: len(avances) >= 2,
    )

    assert [p.name for p in escritos] == ["1000001.pdf", "1000002.pdf"]
    assert avances == [(1, 3), (2, 3)]
    assert _textos(escritos[1]) == ["pagina 2"]
