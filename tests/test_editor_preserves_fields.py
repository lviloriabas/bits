"""Guardar desde el editor no puede borrar reglas que no muestra.

El editor solo edita geometría, tipo y reglas básicas. Antes reconstruía
cada campo desde cero, así que un guardado de coordenadas borraba en
silencio ``min_length``/``max_length`` y el ``ocr_mode`` de la plantilla.

Lo mismo pasaba un nivel más arriba: la plantilla entera se rehacía con
``name``, ``page_size`` y ``fields``, de modo que guardar la geometría
borraba la retícula, las discrepancias activas, la versión y el DPI de
referencia.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.templates.manager import TemplateManager
from app.templates.schema import FieldTemplate, OcrMode, Template

TEMPLATE = Path(__file__).resolve().parents[1] / "template" / "aircraft_log.json"


class TestPlantillaDeProduccion(unittest.TestCase):
    """La plantilla del repositorio conserva sus reglas de longitud."""

    def setUp(self) -> None:
        self.template = TemplateManager().load(TEMPLATE)

    def test_las_celdas_de_caracter_limitan_a_un_caracter(self):
        for component, count in (("day", 2), ("month", 3), ("year", 2)):
            for index in range(1, count + 1):
                cell = self.template.field(f"{component}_{index}")
                self.assertIsNotNone(cell, f"falta {component}_{index}")
                self.assertEqual(cell.min_length, 1, cell.id)
                self.assertEqual(cell.max_length, 1, cell.id)

    def test_flight_number_conserva_sus_longitudes(self):
        field = self.template.field("flight_number")
        self.assertEqual((field.min_length, field.max_length), (1, 7))

    def test_log_number_lee_sin_detector(self):
        self.assertIs(
            self.template.field("log_number").ocr_mode, OcrMode.LINE
        )


class TestRecoleccionDelEditor(unittest.TestCase):
    """``_collect_template`` copia el campo base en vez de rehacerlo."""

    def test_copiar_el_campo_base_conserva_lo_no_editado(self):
        base = FieldTemplate(
            id="day_1", x=0.1, y=0.1, w=0.05, h=0.05,
            regex=r"^\d$", min_length=1, max_length=1,
            postprocess="char", localize="ink", ocr_mode=OcrMode.LINE,
            ink_delta=91.0,
        )
        # Lo que el editor sí edita al mover el rectángulo.
        edited = {
            "id": "day_1", "type": base.type, "required": False,
            "x": 0.2, "y": 0.3, "w": 0.06, "h": 0.04,
            "regex": r"^\d$", "postprocess": "char", "localize": "ink",
            "min_ink_ratio": 0.02, "max_ink_ratio": 0.90,
            "min_components": 2,
        }
        saved = base.model_copy(update=edited)

        self.assertEqual((saved.x, saved.y, saved.w, saved.h),
                         (0.2, 0.3, 0.06, 0.04))
        self.assertEqual(saved.min_length, 1)
        self.assertEqual(saved.max_length, 1)
        self.assertIs(saved.ocr_mode, OcrMode.LINE)
        self.assertEqual(saved.ink_delta, 91.0)

    def test_un_campo_nuevo_no_necesita_base(self):
        field = FieldTemplate(id="nuevo", x=0.1, y=0.1, w=0.1, h=0.1)
        self.assertIs(field.ocr_mode, OcrMode.DETECT)
        self.assertIsNone(field.min_length)


class TestNivelSuperiorDelEditor(unittest.TestCase):
    """``_collect_template`` copia la plantilla base, no solo sus campos."""

    def test_guardar_geometria_conserva_lo_que_el_editor_no_muestra(self):
        base = TemplateManager().load(TEMPLATE).model_copy(update={
            "discrepancy_types": {"vuelo": ["captain_license"]},
        })
        # Lo único que el editor rehace al guardar.
        guardada = base.model_copy(update={
            "name": "Aircraft Log",
            "page_size": [1000, 700],
            "fields": base.fields,
        })

        self.assertEqual(guardada.page_size, [1000, 700])
        self.assertEqual(guardada.discrepancy_types,
                         {"vuelo": ["captain_license"]})
        self.assertIsNotNone(guardada.reticula)
        self.assertEqual(guardada.version, base.version)
        self.assertEqual(guardada.reference_dpi, base.reference_dpi)


class TestSerializacion(unittest.TestCase):
    def test_la_plantilla_en_disco_es_json_valido_y_completo(self):
        data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        template = Template(**data)
        self.assertEqual(len(template.fields), len(data["fields"]))
        ids = [field.id for field in template.fields]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
