"""Consultas locales: historial completo, copias y cambios de los reportes."""

from pathlib import Path

import pytest

from app.gui.csv_search import LocalLogbookIndex, LogbookQuery, SearchCancelled, logbook_query


def _csv(path: Path, number: str = "0012345", page: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"file,page,log_number\nscan.pdf,{page},{number}\n", encoding="utf-8-sig"
    )
    return path


def test_recorre_todo_el_historial_las_entradas_y_los_csv_historicos(tmp_path):
    old = _csv(tmp_path / "output" / "antiguo" / "antiguo.CSV")
    for n in range(30):
        _csv(tmp_path / "output" / f"nuevo{n}" / "datos" / f"nuevo{n}.CSV", "7654321")
    archived = _csv(tmp_path / "input" / "processed" / "escaneado.csv")
    loose = _csv(tmp_path / "input" / "pendiente.csv")
    # Los PDF sin reporte no obligan a iniciar OCR en una consulta.
    (tmp_path / "input" / "nuevo.pdf").touch()
    result = LocalLogbookIndex().search([tmp_path], LogbookQuery("0012345"))
    assert {match.csv_path for match in result.matches} == {old, archived, loose}
    assert result.reports == 33


def test_minimo_y_completo_cuentan_una_vez_sin_perder_copias_en_otro_batch(tmp_path):
    minimum = _csv(tmp_path / "uno" / "datos" / "uno.CSV", "0000000")
    full = _csv(minimum.with_name("uno_completo.csv"))
    other = _csv(tmp_path / "dos" / "datos" / "dos.CSV", page=7)
    result = LocalLogbookIndex().search([tmp_path, minimum.parent], LogbookQuery("0012345"), full)
    assert [(m.csv_path, m.page) for m in result.matches] == [(full, "1"), (other, "7")]
    assert result.reports == 2


def test_solo_busca_el_numero_y_conserva_ceros_iniciales(tmp_path):
    source = _csv(tmp_path / "run.csv")
    with source.open("a", encoding="utf-8") as handle:
        handle.write("0012345.pdf,2,7654321\nscan.pdf,3,00123456\n")
    result = LocalLogbookIndex().search([tmp_path], LogbookQuery("0012345"))
    assert [match.source_row for match in result.matches] == [0]
    partial = LocalLogbookIndex().search([tmp_path], logbook_query("bit:1234"))
    assert [match.source_row for match in partial.matches] == [0, 2]


@pytest.mark.parametrize("text,number", [
    (" 0012345 ", "0012345"), ("bit:1234", "1234"),
    ("BITÁCORA: 0123456", "0123456"), ("bitacora:1", "1"),
    ("HP-1234CMP", None), ("1234", None), ("12345678", None),
])
def test_el_texto_ordinario_sigue_siendo_una_busqueda_del_csv(text, number):
    parsed = logbook_query(text)
    assert (parsed.number if parsed else None) == number


def test_el_indice_reutiliza_y_actualiza_archivos_sin_perder_nuevas_ejecuciones(tmp_path, monkeypatch):
    path = _csv(tmp_path / "run.csv")
    index = LocalLogbookIndex()
    reads = []
    original = index._read

    def read(*args):
        reads.append(args[0])
        return original(*args)

    monkeypatch.setattr(index, "_read", read)
    assert len(index.search([tmp_path], LogbookQuery("0012345")).matches) == 1
    assert len(index.search([tmp_path], LogbookQuery("1234")).matches) == 1
    assert reads == [path]
    _csv(path, "9876543")
    new = _csv(tmp_path / "nuevo" / "datos" / "nuevo.CSV")
    assert [m.csv_path for m in index.search([tmp_path], LogbookQuery("0012345")).matches] == [new]
    assert len(reads) == 3
    new.unlink()
    assert not index.search([tmp_path], LogbookQuery("0012345")).matches
    assert len(reads) == 3


def test_un_archivo_danado_no_oculta_las_coincidencias_validas(tmp_path):
    good = _csv(tmp_path / "correcto.csv")
    broken = tmp_path / "danado.csv"
    broken.write_bytes(b"file,page,log_number\n\xff")
    (tmp_path / "estadisticas.csv").write_text("total,tiempo\n1,20\n", encoding="utf-8")
    result = LocalLogbookIndex().search([tmp_path, tmp_path / "ausente"], LogbookQuery("0012345"))
    assert [m.csv_path for m in result.matches] == [good]
    assert result.unavailable == (broken,)
    assert result.reports == 1


def test_no_publica_un_csv_que_se_reescribe_durante_la_consulta(tmp_path, monkeypatch):
    path = _csv(tmp_path / "run.csv")
    index = LocalLogbookIndex()
    original = index._read

    def changing(*args):
        rows = original(*args)
        _csv(path, "9876543", page=123)
        return rows

    monkeypatch.setattr(index, "_read", changing)
    result = index.search([tmp_path], LogbookQuery("0012345"))
    assert result.matches == ()
    assert result.unavailable == (path,)
    monkeypatch.setattr(index, "_read", original)
    assert len(index.search([tmp_path], LogbookQuery("9876543")).matches) == 1


def test_la_cancelacion_sale_sin_publicar_resultados(tmp_path):
    _csv(tmp_path / "run.csv")
    with pytest.raises(SearchCancelled):
        LocalLogbookIndex().search([tmp_path], LogbookQuery("0012345"), cancelled=lambda: True)
