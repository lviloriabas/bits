import hashlib
import json

from app.reports.outputs import OutputOptions, registrar_exportacion
from app.templates.schema import Template


def test_guarda_plantilla_exacta_y_conserva_exportaciones_anteriores(tmp_path):
    template = Template(name="Prueba", discrepancy_fields=["captain_license"])
    opciones = OutputOptions(template=template, output_root=tmp_path, dpi=200, crop_padding=.01)
    registrar_exportacion(tmp_path, opciones)
    registrar_exportacion(tmp_path, opciones, "exportacion_completada")
    registros = [json.loads(linea) for linea in (tmp_path / "logs/exportaciones.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["evento"] for r in registros] == ["exportacion_iniciada", "exportacion_completada"]
    contenido = (tmp_path / "logs" / registros[0]["plantilla"]).read_bytes()
    assert hashlib.sha256(contenido).hexdigest() == registros[0]["plantilla_sha256"]
    assert json.loads(contenido)["discrepancy_fields"] == ["captain_license"]
