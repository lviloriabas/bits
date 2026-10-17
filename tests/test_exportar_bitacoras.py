"""Copia de bitácoras sueltas a una carpeta elegida."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from app.reports.exportar_bitacoras import (
    Bitacora,
    describir_numeros,
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


def _bitacoras(*numeros: str) -> list[Bitacora]:
    return [Bitacora(numero, Path("a.pdf"), 1) for numero in numeros]


def test_seguidas_el_nombre_es_su_rango():
    assert nombre_conjunto(_bitacoras("2281909", "2281907", "2281908")) == (
        "Bitacoras 2281907-2281909.pdf"
    )
    assert nombre_conjunto(_bitacoras("2281907", "2281908")) == (
        "Bitacoras 2281907-2281908.pdf"
    )


def test_sueltas_el_nombre_las_lista_todas():
    assert nombre_conjunto(_bitacoras("2281915", "2281907", "2281911")) == (
        "Bitacoras 2281907, 2281911, 2281915.pdf"
    )


def test_tramos_seguidos_y_sueltas_se_combinan():
    assert nombre_conjunto(
        _bitacoras("2281907", "2281908", "2281909", "2281915", "2281920", "2281921")
    ) == "Bitacoras 2281907-2281909, 2281915, 2281920-2281921.pdf"


def test_nombre_con_una_sola_repetidas_o_ilegibles():
    assert nombre_conjunto(_bitacoras("2281907")) == "2281907.pdf"
    assert nombre_conjunto(_bitacoras("2281907", "2281907")) == "Bitacoras 2281907.pdf"
    assert nombre_conjunto(_bitacoras("", "")) == "Bitacoras.pdf"
    # Una ilegible no rompe el rango de las legibles.
    assert nombre_conjunto(_bitacoras("2281907", "22819?", "2281908")) == (
        "Bitacoras 2281907-2281908.pdf"
    )


def test_muchas_sueltas_no_desbordan_el_nombre():
    """Windows no admite rutas de más de 260 caracteres."""
    numeros = [f"{2281900 + 2 * n}" for n in range(40)]  # ninguna seguida

    nombre = nombre_conjunto(_bitacoras(*numeros))

    assert nombre.startswith("Bitacoras 2281900, 2281902, ")
    assert len(nombre) < 130
    mostradas = nombre.count(", ") + 1
    assert nombre.endswith(f" y {40 - mostradas} mas.pdf")


def test_describir_numeros_para_la_interfaz():
    assert describir_numeros(["2281909", "2281907", "2281908"]) == "2281907-2281909"
    assert describir_numeros(["2281907", "2281909"]) == "2281907, 2281909"
    assert describir_numeros([f"{2281900 + 2 * n}" for n in range(5)], limite=20) == (
        "2281900, 2281902 y 3 más"
    )
    assert describir_numeros(["", "abc"]) == ""


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
