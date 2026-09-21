"""El PDF de entrega declara qué hay en cada una de sus páginas.

El CSV describe las bitácoras; el PDF además lleva separadores. Sin saber
en qué posiciones están, emparejar los dos por posición se desalinea en el
primer separador y el indexado escribiría cada dato una página más allá de
donde va. Estas pruebas fijan que la secuencia declarada sea exactamente la
que se escribe.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.models.schemas import FieldResult, PageResult, ValidationReport
from app.models.schemas import Status
from app.reports.organize import (
    ArchivoDeEntrega,
    ETIQUETA_REVISAR,
    escribir_indice_paginas,
    secuencia_de_revisar,
    secuencia_pdf_unico,
)


def _page(pn: int, log, mat, date=None) -> PageResult:
    page = PageResult(page_number=pn, date=date)
    if log is not None:
        page.add_field(FieldResult(page_number=pn, field_id="log_number",
                                   field_type="ocr", value=log,
                                   confidence=1.0, status="OK"))
    if mat is not None:
        page.add_field(FieldResult(page_number=pn, field_id="matricula",
                                   field_type="ocr", value=mat,
                                   confidence=1.0, status="OK"))
    return page


def _reporte(*pages: PageResult) -> ValidationReport:
    return ValidationReport(pdf_path="fixture.pdf", template_name="fixture",
                            pages=list(pages))


def etiquetas(secuencia) -> list:
    """Secuencia en corto: la etiqueta del separador o el número de página."""
    return [
        e.separador if e.ref is None else e.ref.page.page_number
        for e in secuencia
    ]


# ── secuencia ──────────────────────────────────────────────────────

def test_sin_separar_no_hay_ninguna_divisoria():
    reporte = _reporte(
        _page(1, "2147337", "HP-1534CMP"),
        _page(2, "2147338", "HP-1534CMP"),
    )
    assert etiquetas(secuencia_pdf_unico([reporte])) == [1, 2]


def test_cada_grupo_abre_con_su_separador():
    reporte = _reporte(
        _page(1, "2147337", "HP-1534CMP"),
        _page(2, "2271665", "HP-1538CMP"),
    )
    secuencia = secuencia_pdf_unico([reporte], ["avion"])
    assert etiquetas(secuencia) == ["HP-1534CMP", 1, "HP-1538CMP", 2]


def test_las_paginas_sin_matricula_cierran_bajo_revisar():
    reporte = _reporte(
        _page(1, "2147337", "HP-1534CMP"),
        _page(2, "2147338", None),
    )
    assert etiquetas(secuencia_pdf_unico([reporte])) == [1, ETIQUETA_REVISAR, 2]


def test_una_fila_que_quedaria_amarilla_cierra_bajo_revisar():
    completa = _page(1, "2147337", "HP-1534CMP")
    incompleta = _page(2, "2147338", "HP-1534CMP")
    incompleta.airvault_review = True

    assert etiquetas(secuencia_pdf_unico([_reporte(completa, incompleta)])) == [
        1, ETIQUETA_REVISAR, 2,
    ]


def test_una_matricula_completa_con_alternativas_sigue_en_su_batch():
    segura = _page(1, "2147337", "HP-1534CMP")
    dudosa = _page(2, "2147338", "HP-1534CMP")
    dudosa.status = Status.WARNING
    matricula = next(
        field for field in dudosa.fields if field.field_id == "matricula"
    )
    matricula.status = Status.WARNING
    matricula.source = "book_correction"
    matricula.votes = 4
    matricula.alternatives = ["HP-1734CMP"]

    secuencia = secuencia_pdf_unico(
        [_reporte(segura, dudosa)], ["avion"]
    )

    assert etiquetas(secuencia) == ["HP-1534CMP", 1, 2]


def test_revisar_puede_quedar_fuera_de_la_secuencia_principal():
    """Va a su propio archivo, que en AirVault sera su propio batch."""
    reporte = _reporte(
        _page(1, "2147337", "HP-1534CMP"),
        _page(2, "2147338", None),
    )
    principal = secuencia_pdf_unico([reporte], incluir_revisar=False)
    assert etiquetas(principal) == [1]
    assert etiquetas(secuencia_de_revisar([reporte])) == [ETIQUETA_REVISAR, 2]


def test_sin_paginas_sin_matricula_no_hay_archivo_de_revisar():
    reporte = _reporte(_page(1, "2147337", "HP-1534CMP"))
    assert secuencia_de_revisar([reporte]) == []


def test_las_discrepancias_van_al_final_con_su_separador():
    reporte = _reporte(
        _page(1, "2147337", "HP-1534CMP"),
        _page(2, "2147338", "HP-1534CMP"),
    )
    secuencia = secuencia_pdf_unico(
        [reporte], excluidas={("fixture.pdf", 2)},
        discrepancias_al_final=True,
    )
    assert etiquetas(secuencia) == [1, "POSIBLES DISCREPANCIAS", 2]


def test_la_pagina_en_blanco_va_a_revisar():
    """La ausencia total de campos debe quedar visible para una persona."""
    en_blanco = PageResult(page_number=2, blank=True)
    reporte = _reporte(_page(1, "2147337", "HP-1534CMP"), en_blanco)
    assert etiquetas(secuencia_pdf_unico([reporte])) == [
        1, ETIQUETA_REVISAR, 2,
    ]


def test_sin_bitacoras_no_hay_secuencia():
    assert secuencia_pdf_unico([_reporte()]) == []


def test_la_secuencia_cuenta_lo_mismo_que_el_pdf():
    """Es la condicion que sostiene toda la correspondencia por posicion."""
    reporte = _reporte(
        _page(1, "2147337", "HP-1534CMP"),
        _page(2, "2271665", "HP-1538CMP"),
        _page(3, "2147338", None),
    )
    secuencia = secuencia_pdf_unico([reporte], ["avion"])
    # dos grupos con su separador, mas el separador de revisar y su pagina
    assert len(secuencia) == 6
    assert sum(1 for e in secuencia if e.es_separador) == 3


# ── archivo escrito ────────────────────────────────────────────────

def test_el_indice_describe_separadores_y_bitacoras(tmp_path):
    reporte = _reporte(
        _page(1, "2147337", "HP-1534CMP"),
        _page(2, "2147338", None),
    )
    destino = escribir_indice_paginas(
        [ArchivoDeEntrega(Path("ejecución.pdf"), secuencia_pdf_unico([reporte]))],
        tmp_path / "corrida_paginas.json",
    )
    datos = json.loads(Path(destino).read_text(encoding="utf-8"))

    assert datos["version"] == 2
    assert len(datos["partes"]) == 1
    assert datos["partes"][0]["pdf"] == "ejecución.pdf"
    assert datos["partes"][0]["paginas"] == [
        {"archivo": "fixture.pdf", "pagina": 1},
        {"separador": ETIQUETA_REVISAR},
        {"archivo": "fixture.pdf", "pagina": 2, "revision_pendiente": False},
    ]


def test_el_indice_nombra_cada_parte_con_su_archivo(tmp_path):
    """Cada archivo es un batch distinto: hay que saber que lleva cada uno."""
    reporte = _reporte(
        _page(1, "2147337", "HP-1534CMP"),
        _page(2, "2147338", "HP-1534CMP"),
    )
    secuencia = secuencia_pdf_unico([reporte])
    destino = escribir_indice_paginas(
        [ArchivoDeEntrega(Path("ejecución -1.pdf"), secuencia[:1]),
         ArchivoDeEntrega(Path("ejecución -2.pdf"), secuencia[1:])],
        tmp_path / "corrida_paginas.json",
    )
    datos = json.loads(Path(destino).read_text(encoding="utf-8"))

    assert [p["pdf"] for p in datos["partes"]] == [
        "ejecución -1.pdf", "ejecución -2.pdf"
    ]
    assert [len(p["paginas"]) for p in datos["partes"]] == [1, 1]


def test_el_indice_marca_cual_es_el_lote_de_revisar(tmp_path):
    """Sin la marca, el indexado escribiria en las que nadie pudo asignar."""
    reporte = _reporte(_page(1, "2147337", "HP-1534CMP"), _page(2, "x", None))
    principal = secuencia_pdf_unico([reporte], incluir_revisar=False)
    revisar = secuencia_de_revisar([reporte])
    destino = escribir_indice_paginas(
        [ArchivoDeEntrega(Path("ejecución.pdf"), principal),
         ArchivoDeEntrega(Path("ejecución REVISAR.pdf"), revisar, revisar=True)],
        tmp_path / "corrida_paginas.json",
    )
    datos = json.loads(Path(destino).read_text(encoding="utf-8"))
    assert [p["revisar"] for p in datos["partes"]] == [False, True]


def test_el_indice_marca_solo_la_pagina_con_fecha_dudosa(tmp_path):
    normal = _page(1, "2147337", "HP-1534CMP", "2026/08/20")
    dudosa = _page(2, "2147338", "HP-1534CMP", "2024/08/21")
    dudosa.date_review = True
    reporte = _reporte(normal, dudosa)

    destino = escribir_indice_paginas(
        [ArchivoDeEntrega(
            Path("ejecucion REVISAR.pdf"),
            secuencia_de_revisar([reporte]),
            revisar=True,
        )],
        tmp_path / "corrida_paginas.json",
    )
    paginas = json.loads(
        Path(destino).read_text(encoding="utf-8")
    )["partes"][0]["paginas"]

    assert paginas == [
        {"separador": ETIQUETA_REVISAR},
        {
            "archivo": "fixture.pdf",
            "pagina": 2,
            "fecha_dudosa": True,
            "revision_pendiente": True,
        },
    ]


def test_el_indice_conserva_la_razon_exacta_de_la_discrepancia(tmp_path):
    pagina = _page(1, "2147337", "HP-1534CMP")
    pagina.discrepancy = True
    pagina.discrepancy_fields = ["pilot_signature"]
    pagina.discrepancy_note = "MISSING TECHNICIAN SIGNATURE"
    reporte = _reporte(pagina)

    destino = escribir_indice_paginas(
        [ArchivoDeEntrega(
            Path("ejecucion REVISAR.pdf"),
            secuencia_de_revisar([reporte]),
            revisar=True,
        )],
        tmp_path / "corrida_paginas.json",
    )
    paginas = json.loads(
        Path(destino).read_text(encoding="utf-8")
    )["partes"][0]["paginas"]

    assert paginas[1]["discrepancy_fields"] == ["pilot_signature"]
    assert paginas[1]["disc_reason"] == "MISSING TECHNICIAN SIGNATURE"


def test_el_indice_usa_el_nombre_con_el_que_sale_en_el_csv(tmp_path):
    """El PDF apartado se numera; el CSV conserva el nombre original.

    Al terminar la ejecucion su PDF se guarda en «input/processed», y si
    ahi ya habia uno igual el nuevo se numera. El reporte pasa a apuntar al
    numerado, pero el CSV sigue diciendo «bitacora.pdf». Si el indice
    escribiera el numerado, el indexado no encontraria ni una fila y el
    batch entero saldria sin matricula, sin log y sin fecha.
    """
    reporte = _reporte(_page(1, "2147337", "HP-1534CMP"))
    reporte.pdf_path = "input/processed/bitacora-2.pdf"
    reporte.source_name = "bitacora.pdf"

    destino = escribir_indice_paginas(
        [ArchivoDeEntrega(Path("ejecución.pdf"), secuencia_pdf_unico([reporte]))],
        tmp_path / "corrida_paginas.json",
    )
    datos = json.loads(Path(destino).read_text(encoding="utf-8"))

    assert datos["partes"][0]["paginas"][0]["archivo"] == "bitacora.pdf"


def test_el_indice_se_escribe_aunque_no_exista_la_carpeta(tmp_path):
    destino = escribir_indice_paginas([], tmp_path / "datos" / "x.json")
    assert Path(destino).is_file()
    assert json.loads(Path(destino).read_text(encoding="utf-8"))["partes"] == []
