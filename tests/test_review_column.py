"""La columna ``review`` del CSV y el recuadro que la cuenta.

Una ejecución no se indexa entera sola: lo que quedó dudoso viaja en el
batch REVISAR y hay que terminarlo a mano en el Web Index. Eso se sabía
solo después de subir, abriendo el batch y contando páginas, y es
justamente lo que decide si la ejecución se cierra en cinco minutos o en
una tarde.

``review`` pone esa decisión en el archivo, una fila por bitácora, y el
recuadro de la ventana de AirVault la cuenta antes de subir nada. Las dos
salen del mismo criterio con el que se reparte la entrega, así que el CSV,
los PDFs y el recuadro no pueden contar cosas distintas.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.gui.automatizacion import OpcionesAutomatizacion
from app.gui.airvault_window import (
    AirVaultWindow,
    TEXTO_SIN_COLUMNA,
    TEXTO_SIN_EJECUCION,
    reparto_de_revision,
)
from app.models.schemas import FieldResult, PageResult, ValidationReport
from app.reports.csv_reporter import CsvReporter
from app.reports.dual_csv import minimal_columns
from app.reports.organize import por_revisar
from app.reports.outputs import marcar_revision
from app.templates.schema import FieldTemplate, Template
from app.utils.important_fields import default_important_columns


def _campo(field_id: str, valor: str) -> FieldResult:
    return FieldResult(
        page_number=1, field_id=field_id, field_type="ocr",
        value=valor, confidence=0.9,
    )


def _bitacora(
    pagina: int, matricula: str = "HP-1848CMP", log: str = "2287310",
    fecha: str | None = "2026/08/31",
) -> PageResult:
    """Una página que se indexa sola mientras no se le quite nada."""
    page = PageResult(page_number=pagina, date=fecha)
    page.add_field(_campo("matricula", matricula))
    page.add_field(_campo("log_number", log))
    return page


def _plantilla() -> Template:
    return Template(
        name="fixture",
        fields=[
            FieldTemplate(id="matricula", x=0.1, y=0.1, w=0.2, h=0.1),
            FieldTemplate(id="log_number", x=0.4, y=0.1, w=0.2, h=0.1),
        ],
    )


def _filas(tmp_path: Path, *pages: PageResult) -> list[dict]:
    reporte = ValidationReport(
        pdf_path="fixture.pdf", template_name="fixture", pages=list(pages)
    )
    destino = tmp_path / "salida.csv"
    CsvReporter().write([reporte], destino, _plantilla())
    with destino.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# ── la columna del CSV ──────────────────────────────────────────────

def test_review_marca_lo_que_va_al_batch_de_revisar(tmp_path):
    """Lo que no se puede indexar solo queda dicho fila por fila."""
    limpia = _bitacora(1)
    sin_matricula = _bitacora(2, matricula="")
    sin_log = _bitacora(3, log="")
    en_blanco = _bitacora(4)
    en_blanco.blank = True

    filas = _filas(tmp_path, limpia, sin_matricula, sin_log, en_blanco)

    assert [fila["review"] for fila in filas] == [
        "false", "true", "true", "true",
    ]


def test_las_discrepancias_tambien_salen_en_review(tmp_path):
    """Van al mismo batch, así que la columna que lo cuenta las incluye.

    ``disc`` sigue diciendo qué las apartó (una firma exigida que falta),
    pero contar cuánto hay que revisar no puede obligar a sumar dos
    columnas y acordarse de que una está dentro de la otra.
    """
    con_discrepancia = _bitacora(1)
    con_discrepancia.discrepancy = True

    fila = _filas(tmp_path, con_discrepancia)[0]

    assert fila["disc"] == "true"
    assert fila["review"] == "true"


def test_la_columna_dice_lo_mismo_que_el_reparto_de_la_entrega(tmp_path):
    """El archivo y los PDFs no pueden discrepar sobre la misma página."""
    paginas = [
        _bitacora(1),
        _bitacora(2, matricula="ilegible"),
        _bitacora(3, fecha=None),
    ]
    paginas[2].date_review = True

    filas = _filas(tmp_path, *paginas)

    for fila, page in zip(filas, paginas):
        assert (fila["review"] == "true") is por_revisar(page)


def test_un_obligatorio_que_el_csv_no_llega_a_traer_manda_a_revisar():
    """La fila manda, no la lectura: sin ``End Date`` la página va amarilla.

    La ejecución leyó matrícula y número, así que nada en el reporte la
    aparta; lo que la aparta es que el CSV no lleve fecha, y eso solo se ve
    mirando la fila que se va a escribir.
    """
    sin_fecha = _bitacora(1, fecha=None)
    reporte = ValidationReport(
        pdf_path="fixture.pdf", template_name="fixture", pages=[sin_fecha]
    )

    assert not por_revisar(sin_fecha)

    faltantes = marcar_revision([reporte], _plantilla())

    assert faltantes[("fixture.pdf", 1)] == ("End Date",)
    assert por_revisar(sin_fecha)


def test_una_ejecucion_completa_no_manda_nada_a_revisar():
    completa = _bitacora(1)
    reporte = ValidationReport(
        pdf_path="fixture.pdf", template_name="fixture", pages=[completa]
    )

    assert marcar_revision([reporte], _plantilla()) == {}
    assert not por_revisar(completa)


def test_review_viaja_al_csv_minimo():
    """El CSV corto es el que se mira y el que se sube: la columna va en él."""
    columnas = CsvReporter.columns_for_fields(["matricula", "log_number"])

    assert "review" in default_important_columns(columnas)
    assert "review" in minimal_columns(columnas)


# ── el recuadro de la ventana de AirVault ───────────────────────────

def _corrida(raiz: Path, filas: list[str], columnas: str) -> Path:
    """Una ejecución exportada con el CSV que se le pida."""
    nombre = "BITS 18 AUG 2026 05 42"
    carpeta = raiz / "output" / nombre
    (carpeta / "datos").mkdir(parents=True)
    csv_path = carpeta / "datos" / f"{nombre}.CSV"
    csv_path.write_text(
        "\n".join([columnas, *filas]) + "\n", encoding="utf-8-sig"
    )
    (carpeta / "stats.json").write_text(
        json.dumps({"corrida": nombre, "total_paginas": len(filas)}),
        encoding="utf-8",
    )
    (carpeta / f"{nombre}.pdf").write_bytes(b"%PDF-1.4\n")
    csv_path.with_name(f"{csv_path.stem}_paginas.json").write_text(
        json.dumps({"version": 2, "partes": [
            {"pdf": f"{nombre}.pdf", "revisar": False,
             "paginas": [{"archivo": "a.pdf", "pagina": 1}]}
        ]}),
        encoding="utf-8",
    )
    return csv_path


COLUMNAS = "file,page,log_number,matricula,date,review"


def _diez_bitacoras(revisar: int) -> list[str]:
    """Diez filas de las que las primeras ``revisar`` van a mano."""
    return [
        "a.pdf,{0},22873{0:02d},HP-1848CMP,2026/08/31,{1}".format(
            numero, "true" if numero <= revisar else "false"
        )
        for numero in range(1, 11)
    ]


def test_el_reparto_sale_de_la_columna_del_csv(tmp_path):
    csv_path = _corrida(tmp_path, _diez_bitacoras(3), COLUMNAS)

    assert reparto_de_revision(csv_path) == (7, 3)


def test_sin_la_columna_no_se_inventa_un_reparto(tmp_path):
    csv_path = _corrida(
        tmp_path,
        ["a.pdf,1,2287310,HP-1848CMP,2026/08/31"],
        "file,page,log_number,matricula,date",
    )

    assert reparto_de_revision(csv_path) is None


def test_el_recuadro_cuenta_las_bitacoras_y_sus_porcentajes(app, tmp_path):
    """Los dos porcentajes suman cien: son el reparto de una misma tanda."""
    ventana = AirVaultWindow(tmp_path, OpcionesAutomatizacion(tmp_path))
    csv_path = _corrida(tmp_path, _diez_bitacoras(3), COLUMNAS)

    ventana.fijar_corrida(csv_path)

    assert ventana.reparto_total.text() == "10 bitácoras"
    assert ventana.reparto_automaticas.text() == "7 automáticas (70 %)"
    assert ventana.reparto_revisar.text() == "3 a revisar (30 %)"


def test_el_recuadro_arranca_diciendo_que_falta_elegir(app, tmp_path):
    ventana = AirVaultWindow(tmp_path, OpcionesAutomatizacion(tmp_path))

    assert ventana.reparto_total.text() == TEXTO_SIN_EJECUCION
    assert ventana.reparto_revisar.text() == ""


def test_una_ejecucion_vieja_explica_por_que_no_hay_numeros(app, tmp_path):
    """Tres ceros se leerían como una ejecución sin bitácoras."""
    ventana = AirVaultWindow(tmp_path, OpcionesAutomatizacion(tmp_path))
    csv_path = _corrida(
        tmp_path,
        ["a.pdf,1,2287310,HP-1848CMP,2026/08/31"],
        "file,page,log_number,matricula,date",
    )

    ventana.fijar_corrida(csv_path)

    assert ventana.reparto_total.text() == TEXTO_SIN_COLUMNA
    assert ventana.reparto_automaticas.text() == ""
