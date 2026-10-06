"""El mismo buscador navega batches locales y conserva la busqueda de texto."""

import time
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtWidgets import QApplication

from app.gui.csv_search import SearchCancelled
from app.gui.csv_viewer import CsvViewerWindow


def _csv(root: Path, batch: str, number: str = "0012345", page: int = 1) -> Path:
    path = root / batch / "datos" / f"{batch}.CSV"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"file,page,log_number,matricula\nscan.pdf,{page},{number},HP-1234CMP\n",
        encoding="utf-8",
    )
    return path


def _wait(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        QApplication.instance().processEvents()
        time.sleep(0.005)
    assert predicate()


def _search(viewer, query):
    viewer.search_edit.setText(query)
    viewer._find_in_csv()
    _wait(lambda: viewer._search_worker is None and viewer._pending_search is None)


@pytest.fixture
def viewer(app, tmp_path, monkeypatch):
    monkeypatch.setattr("app.gui.csv_viewer._PROGRAM_DIR", tmp_path)
    window = CsvViewerWindow(tmp_path / "output")
    yield window
    window.close()
    app.processEvents()


def test_busca_sin_abrir_csv_y_las_flechas_cruzan_batches(viewer, tmp_path):
    old = _csv(tmp_path / "output", "viejo", page=7)
    for n in range(30):
        _csv(tmp_path / "output", f"nuevo{n}", "7654321")
    recent = _csv(tmp_path / "output", "reciente", page=9)
    _search(viewer, "0012345")
    assert viewer._loaded_csv_path == recent
    assert len(viewer._local_matches) == 2
    assert viewer.search_next.isEnabled()
    assert "reciente" in viewer.search_context.text()
    viewer.search_next.click()
    assert viewer._loaded_csv_path == old
    assert "viejo" in viewer.search_context.text()
    assert "página 7" in viewer.search_context.text()
    assert viewer.search_edit.text() == "0012345"
    assert viewer._search_position == 1
    viewer._find_in_csv()
    assert viewer._loaded_csv_path == recent
    viewer.search_prev.click()
    assert viewer._loaded_csv_path == old


def test_prefiere_el_csv_abierto_y_mantiene_la_fila_al_ordenar(viewer, tmp_path):
    first = _csv(tmp_path / "output", "uno")
    _csv(tmp_path / "output", "dos")
    with first.open("a", encoding="utf-8") as handle:
        handle.write("otro.pdf,3,9876543,HP-9999CMP\nscan.pdf,2,0012345,HP-1234CMP\n")
    viewer.load_csv_file(first)
    column = viewer.table_model.column_of("page")
    viewer.table_model.sort(column)
    _search(viewer, "0012345")
    assert viewer._loaded_csv_path == first
    viewer.search_next.click()
    assert viewer.table_model.source_row(viewer.table.currentIndex().row()) == 2
    assert "página 2" in viewer.search_context.text()
    _search(viewer, "HP-1234CMP")
    assert viewer._local_matches == ()
    assert [row for row, _ in viewer._search_matches] == [0, 2]


def test_incluye_input_processed_y_carpetas_abiertas(viewer, tmp_path):
    archived = _csv(tmp_path / "input" / "processed", "escaneado")
    external = _csv(tmp_path / "externo", "externo")
    assert viewer.load_folder(external.parent.parent)
    _search(viewer, "bit:1234")
    assert {m.csv_path for m in viewer._local_matches} == {archived, external}


def test_localiza_el_pdf_archivado_y_limpia_la_imagen_si_la_siguiente_falta(viewer, tmp_path):
    import fitz

    source = tmp_path / "input" / "processed" / "scan.pdf"
    source.parent.mkdir(parents=True)
    with fitz.open() as document:
        for n in range(3):
            document.new_page().insert_text((20, 30), f"Pagina {n + 1}")
        document.save(source)
    report = _csv(tmp_path / "output", "uno", page=3)
    with report.open("a", encoding="utf-8") as handle:
        handle.write("ausente.pdf,5,0012345,HP-1234CMP\n")
    _search(viewer, "0012345")
    assert viewer.pdf_viewer._path == source
    assert viewer.pdf_viewer._page == 3
    viewer.search_next.click()
    assert viewer.pdf_viewer._path is None
    assert viewer.pdf_viewer._pending_render is None
    assert "No se encontró el PDF" in viewer.pdf_viewer.image.text()
    assert "PDF no disponible" in viewer.search_context.text()


def test_cambio_o_borrado_del_reporte_no_lleva_a_una_pagina_equivocada(viewer, tmp_path):
    first = _csv(tmp_path / "output", "uno")
    _csv(tmp_path / "output", "dos")
    viewer.load_csv_file(first)
    _search(viewer, "0012345")
    target = viewer._local_matches[1].csv_path
    target.unlink()
    viewer.search_next.click()
    assert viewer._loaded_csv_path == first
    assert "ya no está disponible" in viewer.search_context.text()
    assert not viewer.search_next.isEnabled()
    _search(viewer, "0012345")
    assert len(viewer._local_matches) == 1
    _csv(tmp_path / "output", "uno", "9876543")
    _search(viewer, "9876543")
    assert viewer._rows[0]["log_number"] == "9876543"


def test_vaciar_o_cambiar_csv_limpia_la_navegacion(viewer, tmp_path):
    _csv(tmp_path / "output", "uno")
    other = _csv(tmp_path / "output", "dos")
    _search(viewer, "0012345")
    viewer.search_edit.clear()
    assert viewer._local_matches == ()
    assert not viewer.search_next.isEnabled()
    _search(viewer, "0012345")
    viewer.load_csv_file(other)
    assert viewer._local_matches == ()
    assert not viewer.search_prev.isEnabled()


def test_indica_consulta_incompleta_y_ausencia_de_resultados(viewer, tmp_path):
    _csv(tmp_path / "output", "uno", "7654321")
    broken = tmp_path / "output" / "danado.csv"
    broken.write_bytes(b"file,page,log_number\n\xff")
    _search(viewer, "0012345")
    assert "sin coincidencias" in viewer.search_context.text()
    assert "1 reporte(s)" in viewer.search_context.text()
    assert "1 archivo(s) o carpeta(s) sin consultar" in viewer.search_context.text()


def test_cambiar_la_consulta_cancela_el_hilo_y_descarta_su_respuesta(viewer, tmp_path, monkeypatch):
    _csv(tmp_path / "output", "uno")
    expected = _csv(tmp_path / "output", "dos", "9876543")
    started = Event()
    release = Event()
    original = viewer._local_index.search

    def delay(roots, query, preferred, cancelled):
        if query.number == "0012345":
            started.set()
            while not release.wait(0.01):
                if cancelled():
                    raise SearchCancelled
        return original(roots, query, preferred, cancelled)

    monkeypatch.setattr(viewer._local_index, "search", delay)
    try:
        viewer.search_edit.setText("0012345")
        viewer._find_in_csv()
        _wait(started.is_set)
        _search(viewer, "9876543")
        assert viewer._loaded_csv_path == expected
        assert [m.number for m in viewer._local_matches] == ["9876543"]
    finally:
        release.set()


def test_cerrar_cancela_una_consulta_activa(viewer, tmp_path, monkeypatch):
    _csv(tmp_path / "output", "uno")
    started = Event()

    def delay(roots, query, preferred, cancelled):
        started.set()
        while not cancelled():
            time.sleep(0.01)
        raise SearchCancelled

    monkeypatch.setattr(viewer._local_index, "search", delay)
    viewer.search_edit.setText("0012345")
    viewer._find_in_csv()
    _wait(started.is_set)
    worker = viewer._search_worker
    viewer.close()
    assert not worker.isRunning()
