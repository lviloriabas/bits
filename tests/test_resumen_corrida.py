"""Cifras de la ejecución que el visor de CSV deja bajo la página."""

from __future__ import annotations

import json
from pathlib import Path

from app.gui.resumen_corrida import (
    detalle_entrega,
    resumir_corrida,
    texto_resumen,
)

_COLUMNAS = "file,page,log_number,review,dup,disc,matricula,date\n"


def _corrida(tmp_path: Path, filas: list[str], partes: list[dict] | None):
    """Carpeta de ejecución con su CSV y, si se da, el índice de entrega."""
    run = tmp_path / "run"
    datos = run / "datos"
    datos.mkdir(parents=True)
    csv_path = datos / "run.CSV"
    csv_path.write_text(_COLUMNAS + "".join(filas), encoding="utf-8")
    if partes is not None:
        (datos / "run_paginas.json").write_text(
            json.dumps({"version": 2, "partes": partes}), encoding="utf-8"
        )
    return run, csv_path


def _filas(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    from app.gui.csv_utils import read_csv_file

    return read_csv_file(csv_path)


_FILAS = [
    "a.pdf,1,2281909,false,false,false,HP-9821CMP,2026/08/27\n",
    "a.pdf,2,2281910,false,true,false,HP-9821CMP,2026/08/28\n",
    "a.pdf,3,2318039,true,false,true,HP-9810CMP,\n",
]

_PARTES = [
    {"pdf": "run.pdf", "revisar": False, "paginas": [
        {"separador": "HP-9821CMP"},
        {"archivo": "a.pdf", "pagina": 1},
        {"archivo": "a.pdf", "pagina": 2},
    ]},
    {"pdf": "run REVISAR.pdf", "revisar": True, "paginas": [
        {"separador": "POSIBLES DISCREPANCIAS"},
        {"archivo": "a.pdf", "pagina": 3},
    ]},
]


def test_cuenta_bitacoras_separadores_y_paginas_de_la_entrega(tmp_path: Path):
    run, csv_path = _corrida(tmp_path, _FILAS, _PARTES)
    columnas, filas = _filas(csv_path)

    resumen = resumir_corrida(filas, csv_path, run, columnas)

    assert resumen.bitacoras == 3
    assert resumen.separadores == 2
    assert resumen.paginas_de_entrega == 5
    assert resumen.entrega_al_dia
    texto = texto_resumen(resumen)
    assert texto.splitlines() == [
        "3 bitácoras y 2 separadores: 5 páginas en 2 PDF de entrega.",
        "2 aviones, 1 a revisar, 1 duplicada, 1 con discrepancia, 1 sin fecha.",
    ]
    assert detalle_entrega(resumen).splitlines() == [
        "run.pdf: 3 páginas (2 bitácoras, 1 separador)",
        "run REVISAR.pdf: 2 páginas (1 bitácora, 1 separador)",
    ]


def test_el_csv_completo_usa_el_indice_de_la_ejecucion(tmp_path: Path):
    run, csv_path = _corrida(tmp_path, _FILAS, _PARTES)
    completo = csv_path.with_name("run_completo.CSV")
    completo.write_text(csv_path.read_text(encoding="utf-8"), encoding="utf-8")
    columnas, filas = _filas(completo)

    resumen = resumir_corrida(filas, completo, run, columnas)

    assert resumen.separadores == 2


def test_avisa_cuando_la_entrega_es_de_antes_de_borrar_paginas(tmp_path: Path):
    """Borrar páginas reescribe el CSV, no los PDF ni su índice."""
    run, csv_path = _corrida(tmp_path, _FILAS[:2], _PARTES)
    columnas, filas = _filas(csv_path)

    resumen = resumir_corrida(filas, csv_path, run, columnas)

    assert not resumen.entrega_al_dia
    assert texto_resumen(resumen).splitlines()[0] == (
        "2 bitácoras; los 2 PDF de entrega (5 páginas, 3 bitácoras y "
        "2 separadores) son anteriores: use «Generar salidas»."
    )


def test_sin_entrega_lo_dice(tmp_path: Path):
    run, csv_path = _corrida(tmp_path, _FILAS, None)
    columnas, filas = _filas(csv_path)

    texto = texto_resumen(resumir_corrida(filas, csv_path, run, columnas))

    assert texto.splitlines()[0] == (
        "3 bitácoras. Aún no hay PDF de entrega: use «Generar salidas»."
    )


def test_varios_pdf_sin_indice_no_inventan_separadores(tmp_path: Path):
    """La entrega en varios PDF no deja índice: no se sabe qué lleva."""
    run, csv_path = _corrida(tmp_path, _FILAS, None)
    (run / "HP-9821CMP.pdf").write_bytes(b"%PDF-1.4\n")
    columnas, filas = _filas(csv_path)

    resumen = resumir_corrida(filas, csv_path, run, columnas)

    assert resumen.entrega is None
    assert texto_resumen(resumen).splitlines()[0] == "3 bitácoras."
    assert detalle_entrega(resumen) == ""


def test_los_pdf_auxiliares_no_son_entrega(tmp_path: Path):
    run, csv_path = _corrida(tmp_path, _FILAS, None)
    (run / "errores.pdf").write_bytes(b"%PDF-1.4\n")
    (run / "debug.pdf").write_bytes(b"%PDF-1.4\n")
    columnas, filas = _filas(csv_path)

    resumen = resumir_corrida(filas, csv_path, run, columnas)

    assert resumen.entrega == ()


def test_un_csv_historico_no_afirma_lo_que_no_trae(tmp_path: Path):
    csv_path = tmp_path / "viejo.csv"
    csv_path.write_text(
        "file,page,matricula\na.pdf,1,HP-1234CMP\n", encoding="utf-8"
    )
    columnas, filas = _filas(csv_path)

    resumen = resumir_corrida(filas, csv_path, None, columnas)

    assert texto_resumen(resumen).splitlines() == [
        "1 bitácora. Aún no hay PDF de entrega: use «Generar salidas».",
        "1 avión.",
    ]


def test_el_visor_muestra_las_cifras_bajo_la_pagina(app, tmp_path: Path):
    from app.gui.csv_viewer import CsvViewerWindow

    run, _csv_path = _corrida(tmp_path, _FILAS, _PARTES)
    viewer = CsvViewerWindow(tmp_path)
    try:
        assert viewer.pdf_viewer.run_stats.isHidden()
        assert viewer.load_folder(run) is True
        app.processEvents()

        etiqueta = viewer.pdf_viewer.run_stats
        assert not etiqueta.isHidden()
        assert etiqueta.text().startswith("3 bitácoras y 2 separadores")
        assert "run REVISAR.pdf: 2 páginas" in etiqueta.toolTip()
    finally:
        viewer.pdf_viewer.shutdown()
        viewer.close()
        app.processEvents()
