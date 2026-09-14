"""Abrir un CSV grande no puede costar varios segundos de ventana muerta.

Una ejecución de dos mil páginas por ochenta y seis columnas se leía cinco
veces del JSON compañero y armaba a mano el mapa de estados de la ejecución
entera, aunque el CSV completo ya traiga sus columnas ``_status``. Las dos
cosas son trabajo que no hacía falta y aquí se fijan para que no vuelva.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.gui import csv_viewer
from app.gui.csv_viewer import CsvViewerWindow, _companion_payload


def _ejecucion(tmp_path: Path, completo: bool) -> Path:
    """Una ejecución con su CSV (mínimo o completo) y su JSON compañero."""
    run = tmp_path / "BITS 11 AUG 2026 09 00"
    datos = run / "datos"
    datos.mkdir(parents=True)
    origen = run / "paginas.pdf"
    origen.write_text("%PDF-1.4\n", encoding="utf-8")
    columnas = "file,page,log_number"
    fila = "paginas.pdf,1,2271620"
    if completo:
        columnas += ",log_number_status"
        fila += ",ERROR"
    csv = datos / "BITS 11 AUG 2026 09 00.csv"
    csv.write_text(f"{columnas}\n{fila}\n", encoding="utf-8")
    csv.with_suffix(".json").write_text(
        json.dumps(
            {
                "reportes": [
                    {
                        "pdf_path": str(origen),
                        "source_name": "paginas.pdf",
                        "pages": [
                            {
                                "page_number": 1,
                                "fields": [
                                    {
                                        "field_id": "log_number",
                                        "status": "ERROR",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return csv


def test_el_json_de_la_ejecucion_se_interpreta_una_sola_vez(
    app, tmp_path: Path, monkeypatch
):
    """Lo piden cinco sitios distintos al abrir; el archivo es el mismo."""
    csv = _ejecucion(tmp_path, completo=True)
    lecturas: list[str] = []
    original = csv_viewer._parsed_companion.__wrapped__

    def contar(path, marca):
        lecturas.append(path)
        return original(path, marca)

    csv_viewer._parsed_companion.cache_clear()
    monkeypatch.setattr(
        csv_viewer,
        "_parsed_companion",
        csv_viewer.lru_cache(maxsize=4)(contar),
    )

    visor = CsvViewerWindow(tmp_path)
    try:
        assert visor.load_csv_file(csv)
        assert len(lecturas) == 1
    finally:
        visor.pdf_viewer.shutdown()
        visor.close()
        app.processEvents()


def test_reescribir_el_json_invalida_lo_recordado(app, tmp_path: Path):
    """Eliminar páginas reescribe el archivo: lo guardado ya no vale."""
    csv = _ejecucion(tmp_path, completo=True)
    companion = csv.with_suffix(".json")

    assert len(_companion_payload(csv)["reportes"]) == 1

    payload = json.loads(companion.read_text(encoding="utf-8"))
    payload["reportes"] = []
    companion.write_text(json.dumps(payload), encoding="utf-8")

    assert _companion_payload(csv)["reportes"] == []


def _ejecucion_real(tmp_path: Path) -> tuple[Path, Path]:
    """CSV mínimo y completo escritos por la propia exportación.

    El JSON compañero tiene que ser el que escribe el programa: el respaldo
    lo reconstruye con los modelos de la ejecución, no con un JSON a mano.
    """
    from app.models.schemas import FieldResult, PageResult, ValidationReport
    from app.reports.csv_reporter import CsvReporter
    from app.reports.dual_csv import write_minimal_csv
    from app.templates.manager import TemplateManager

    plantilla = TemplateManager().load(
        Path(__file__).resolve().parents[1] / "template" / "aircraft_log.json"
    )
    pagina = PageResult(page_number=1)
    pagina.add_field(
        FieldResult(
            page_number=1,
            field_id="log_number",
            field_type="ocr",
            value="2147337",
            confidence=0.4,
            status="ERROR",
        )
    )
    reportes = [
        ValidationReport(
            pdf_path=str(tmp_path / "paginas.pdf"),
            template_name=plantilla.name,
            pages=[pagina],
        )
    ]
    datos = tmp_path / "run" / "datos"
    datos.mkdir(parents=True)
    completo = datos / "run_completo.CSV"
    minimo = datos / "run.CSV"
    CsvReporter().write(reportes, completo, plantilla)
    write_minimal_csv(completo, minimo)
    (datos / "run.json").write_text(
        json.dumps(
            {"reportes": [r.model_dump(mode="json") for r in reportes]}
        ),
        encoding="utf-8",
    )
    return minimo, completo


def test_el_csv_completo_no_arma_el_mapa_de_estados_del_json(
    app, tmp_path: Path
):
    """Sus columnas ``_status`` ya dicen el color de cada celda."""
    _minimo, completo = _ejecucion_real(tmp_path)
    visor = CsvViewerWindow(tmp_path)
    try:
        assert visor.load_csv_file(completo)
        assert "log_number_status" in visor._columns

        assert visor._status_for(visor._rows[0], "log_number") == "ERROR"
        # El respaldo ni se tocó: recorrer la ejecución entera para armarlo
        # es lo que hacía lenta la apertura.
        assert visor._field_statuses is None
    finally:
        visor.pdf_viewer.shutdown()
        visor.close()
        app.processEvents()


def test_el_csv_minimo_sigue_pintandose_con_el_json(app, tmp_path: Path):
    """Sin columnas de estado, el color solo puede salir del compañero."""
    minimo, completo = _ejecucion_real(tmp_path)
    # El completo tiene prioridad si existe. Esta prueba cubre una entrega
    # que solo conserva el minimo y obtiene sus estados desde el JSON.
    completo.unlink()
    visor = CsvViewerWindow(tmp_path)
    try:
        assert visor.load_csv_file(minimo)
        assert "log_number_status" not in visor._columns

        assert visor._status_for(visor._rows[0], "log_number") == "ERROR"
        # Y se armó al pedirlo, no al abrir.
        assert visor._field_statuses is not None
    finally:
        visor.pdf_viewer.shutdown()
        visor.close()
        app.processEvents()
