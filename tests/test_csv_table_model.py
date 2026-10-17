"""Lo que muestra la tabla del visor de CSV y cómo lo guarda.

Cada columna de la tabla es una del CSV. Hubo una columna de casillas para
marcar páginas sueltas, pero no seguía a la selección y confundía: ahora se
elige solo con la selección.

De paso se comprueba lo que hace lenta a esta ventana: la tabla lee del CSV
que ya está en memoria en vez de guardar un ítem por celda, y ordenar es
reordenar índices.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt

from app.gui.csv_viewer import CsvViewerWindow


def _visor(tmp_path: Path) -> CsvViewerWindow:
    run = tmp_path / "run"
    datos = run / "datos"
    datos.mkdir(parents=True)
    (datos / "run.csv").write_text(
        "file,page,log_number,matricula\n"
        "a.pdf,1,2271620,HP-1848CMP\n"
        "b.pdf,2,2271621,HP-1849CMP\n"
        "c.pdf,3,2271622,HP-1850CMP\n",
        encoding="utf-8",
    )
    visor = CsvViewerWindow(tmp_path)
    assert visor.load_folder(run)
    return visor


def test_la_tabla_no_tiene_casillas(app, tmp_path: Path):
    """La primera columna es la primera del CSV y nada se puede marcar."""
    visor = _visor(tmp_path)
    try:
        modelo = visor.table_model
        assert modelo.columnCount() == 4
        assert modelo.index(0, 0).data() == "a.pdf"
        assert modelo.column_of("file") == 0
        assert modelo.name_of(0) == "file"
        for columna in range(modelo.columnCount()):
            indice = modelo.index(0, columna)
            assert indice.data(Qt.ItemDataRole.CheckStateRole) is None
            assert not modelo.flags(indice) & Qt.ItemFlag.ItemIsUserCheckable

        # Lo elegido es lo resaltado, y se cuenta en filas del CSV aunque
        # la tabla esté ordenada al revés.
        visor.table_sort.cycle_column(modelo.column_of("page"))
        visor.table.selectRow(0)
        assert visor._selected_source_rows() == [2]
    finally:
        visor.pdf_viewer.shutdown()
        visor.close()
        app.processEvents()


def test_la_tabla_delega_en_el_csv_en_vez_de_copiarlo(app, tmp_path: Path):
    """Ordenar es reordenar índices: no se mueve ninguna celda de sitio."""
    visor = _visor(tmp_path)
    try:
        modelo = visor.table_model
        # Las filas del modelo son las del CSV, no copias suyas: la tabla
        # no duplica en memoria lo que ya está leído.
        assert all(
            fila is original
            for fila, original in zip(modelo.rows, visor._rows)
        )

        columna = modelo.column_of("page")
        visor.table_sort.cycle_column(columna)
        assert [
            modelo.source_row(fila) for fila in range(modelo.rowCount())
        ] == [2, 1, 0]
        # El CSV sigue en su orden: lo que cambió es cómo se mira.
        assert [fila["page"] for fila in visor._rows] == ["1", "2", "3"]
    finally:
        visor.pdf_viewer.shutdown()
        visor.close()
        app.processEvents()
