"""Pruebas del re-export: las salidas se escriben sobre la carpeta de la
ejecución (mismo CSV, PDFs regenerados, sin artefactos de la separación
anterior)."""

from __future__ import annotations

import json
import csv
from datetime import date
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.models.schemas import FieldResult, PageResult, ValidationReport
from app.reports.outputs import OutputOptions, complete_csv_path, write_outputs
from app.templates.manager import TemplateManager

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "template" / "aircraft_log.json"
)


def _firma(page: PageResult, field_id: str, value: str, confidence: float) -> None:
    page.add_field(FieldResult(
        page_number=page.page_number,
        field_id=field_id,
        field_type="signature",
        value=value,
        confidence=confidence,
        status="OK",
    ))


def _page(pn: int, log, mat, firmas: bool = True) -> PageResult:
    # Estas pruebas ejercitan el re-export, no la ruta de datos incompletos:
    # la fila debe tener todos los obligatorios de AirVault.
    page = PageResult(page_number=pn, date="2026/08/23")
    if log is not None:
        page.add_field(FieldResult(page_number=pn, field_id="log_number",
                                   field_type="ocr", value=log,
                                   confidence=1.0, status="OK"))
    if mat is not None:
        page.add_field(FieldResult(page_number=pn, field_id="matricula",
                                   field_type="ocr", value=mat,
                                   confidence=1.0, status="OK"))
    if firmas:
        # Una entrada de vuelo con las tres firmas exigidas leidas. Sin
        # ellas la pagina seria una discrepancia y se iria al batch de
        # revisar, que no es lo que estas pruebas miden. Las dos casillas
        # del tecnico van vacias: cualquiera de las dos escrita haria de
        # esta una entrada de mantenimiento.
        _firma(page, "technician_signature", "false", 0.99)
        _firma(page, "technician_license", "false", 0.99)
        _firma(page, "pilot_signature", "true", 0.99)
        _firma(page, "captain_signature", "true", 0.99)
        _firma(page, "captain_license", "true", 0.99)
    return page


def _reporte(name: str, *pages: PageResult) -> ValidationReport:
    return ValidationReport(pdf_path=str(FIXTURES / name), template_name="fixture",
                            pages=list(pages))


class TestReexport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TemplateManager().load(TEMPLATE)
        cls.reports = [
            _reporte("test.pdf",
                     _page(1, "2147337", "HP-1534CMP"),
                     _page(2, "2147338", "HP-1534CMP"),
                     _page(3, "2271665", "HP-1538CMP")),
        ]

    def _options(
        self, root: Path, separar_por=(), run_dir=None, skip_pdfs=False,
        discrepancias=False,
    ) -> OutputOptions:
        return OutputOptions(
            template=self.template,
            output_root=root,
            dpi=100,
            crop_padding=0.01,
            separar_por=tuple(separar_por),
            run_dir=run_dir,
            skip_pdfs=skip_pdfs,
            discrepancias=discrepancias,
        )

    def test_reexport_misma_carpeta(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primer = write_outputs(self.reports, self._options(root))
            nombre = primer.name
            csv_primero = primer / "datos" / f"{nombre}.CSV"
            self.assertTrue(csv_primero.exists())
            # Re-export con otra separación apuntando a la misma carpeta:
            # no se crea una ejecución nueva y los PDFs se regeneran ahí.
            segundo = write_outputs(
                self.reports, self._options(root, ("avion",), primer)
            )
            self.assertEqual(primer, segundo)
            self.assertEqual(len(list(root.glob("BITS *"))), 1)
            self.assertTrue((primer / "datos" / f"{nombre}.CSV").exists())
            self.assertTrue((primer / "stats.json").exists())
            self.assertTrue((primer / "HP-1534CMP.pdf").exists())
            self.assertTrue((primer / "HP-1538CMP.pdf").exists())

    @patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
    def test_reexport_descarta_anos_futuros_aunque_dos_paginas_los_respalden(self, _clock):
        pages = [_page(1, "2147337", "HP-1534CMP"),
                 _page(2, "2147338", "HP-1534CMP")]
        for page in pages:
            page.date = "2028/08/23"
            for field_id, value in (("day", "23"), ("month", "AGO"), ("year", "28")):
                page.add_field(FieldResult(
                    page_number=page.page_number, field_id=field_id,
                    field_type="ocr", value=value, confidence=.9,
                ))
        reports = [_reporte("test.pdf", *pages)]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "BITS guardado"
            (run / "datos").mkdir(parents=True)
            write_outputs(reports, self._options(root, run_dir=run, skip_pdfs=True))

            minimal = run / "datos" / f"{run.name}.CSV"
            for path in (minimal, complete_csv_path(minimal)):
                with path.open(encoding="utf-8-sig", newline="") as fh:
                    rows = list(csv.DictReader(fh))
                self.assertEqual(len(rows), 2)
                for row in rows:
                    self.assertEqual(row["date"], "")
                    self.assertEqual(row["review"], "true")
                    if "year" in row:
                        self.assertEqual(row["year"], "")
            self.assertTrue(all(page.date_review for page in pages))
            self.assertTrue(all("Año futuro" in page.comment for page in pages))

    def test_conserva_los_pdfs_ya_exportados(self):
        """Un re-export nunca destruye la entrega anterior."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "BITS 07 AUG 2026 22 01"
            (run / "datos").mkdir(parents=True)
            (run / "logs").mkdir()
            (run / "HP-1234CMP.pdf").write_bytes(b"x")
            (run / "discrepancias.pdf").write_bytes(b"x")
            (run / "recortes_firmas").mkdir()
            (run / "datos" / "BITS 07 AUG 2026 22 01.CSV").write_text(
                "old", encoding="utf-8")
            (run / "logs" / "app.log").write_text("log", encoding="utf-8")
            write_outputs(self.reports, self._options(root, (), run))
            # Los PDFs previos siguen ahí, intactos.
            self.assertEqual((run / "HP-1234CMP.pdf").read_bytes(), b"x")
            self.assertEqual((run / "discrepancias.pdf").read_bytes(), b"x")
            # Lo regenerable que ya no corresponde sí se limpia.
            self.assertFalse((run / "recortes_firmas").exists())
            self.assertTrue(
                (run / "datos" / "BITS 07 AUG 2026 22 01.CSV").exists()
            )
            self.assertTrue((run / "logs" / "app.log").exists())

    def test_reexport_numera_los_pdfs_repetidos(self):
        """Mismo nombre en la misma carpeta: la copia lleva sufijo."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = write_outputs(
                self.reports, self._options(root, ("avion",))
            )
            self.assertTrue((run / "HP-1534CMP.pdf").exists())
            primer_tamano = (run / "HP-1534CMP.pdf").stat().st_size

            write_outputs(
                self.reports, self._options(root, ("avion",), run)
            )
            self.assertTrue((run / "HP-1534CMP-2.pdf").exists())
            self.assertEqual(
                (run / "HP-1534CMP.pdf").stat().st_size, primer_tamano
            )

            write_outputs(
                self.reports, self._options(root, ("avion",), run)
            )
            self.assertTrue((run / "HP-1534CMP-3.pdf").exists())
            self.assertEqual(
                sorted(p.name for p in run.glob("HP-1534CMP*.pdf")),
                ["HP-1534CMP-2.pdf", "HP-1534CMP-3.pdf", "HP-1534CMP.pdf"],
            )

    def test_stats_nombra_los_pdfs_reales(self):
        """stats.json lista los archivos que existen, no los teóricos."""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = write_outputs(
                self.reports, self._options(root, ("avion",))
            )
            write_outputs(
                self.reports, self._options(root, ("avion",), run)
            )
            stats = json.loads(
                (run / "stats.json").read_text(encoding="utf-8")
            )
            archivos = [p["archivo"] for p in stats["separacion"]["pdfs"]]
            self.assertEqual(
                archivos, ["HP-1534CMP-2.pdf", "HP-1538CMP-2.pdf"]
            )
            for nombre in archivos:
                self.assertTrue((run / nombre).exists())

    def test_csv_conserva_nombre_y_contenido(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primer = write_outputs(self.reports, self._options(root))
            nombre = primer.name
            csv_primero = (primer / "datos" / f"{nombre}.CSV").read_bytes()
            segundo = write_outputs(
                self.reports, self._options(root, ("mes",), primer)
            )
            csv_segundo = (segundo / "datos" / f"{nombre}.CSV").read_bytes()
            self.assertEqual(segundo, primer)
            self.assertEqual(csv_primero, csv_segundo)

    def test_la_division_manda_a_revisar_lo_que_quedaria_amarillo(self):
        with tempfile.TemporaryDirectory() as tmp:
            incompleta = _page(1, "2147337", "HP-1534CMP")
            incompleta.date = None
            run = write_outputs(
                [_reporte("test.pdf", incompleta)],
                self._options(Path(tmp)),
            )

            indice = json.loads(
                (
                    run / "datos" / f"{run.name}_paginas.json"
                ).read_text(encoding="utf-8")
            )
            assert len(indice["partes"]) == 1
            assert indice["partes"][0]["revisar"] is True

    def test_una_fecha_inferible_no_sale_del_batch_automatico(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages = [
                _page(1, "2287310", "HP-1534CMP"),
                _page(2, "2287311", "HP-1534CMP"),
                _page(3, "2287312", "HP-1534CMP"),
            ]
            pages[0].date = "2026/08/03"
            pages[1].date = None
            pages[2].date = "2026/08/28"
            run = write_outputs(
                [_reporte("test.pdf", *pages)],
                self._options(Path(tmp)),
            )

            indice = json.loads(
                (
                    run / "datos" / f"{run.name}_paginas.json"
                ).read_text(encoding="utf-8")
            )
            assert [parte["revisar"] for parte in indice["partes"]] == [False]

    def test_una_advertencia_con_indices_completos_va_al_batch_normal(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = _page(1, "2147337", "HP-1534CMP")
            matricula = next(
                field for field in page.fields
                if field.field_id == "matricula"
            )
            from app.models.schemas import Status

            matricula.status = Status.WARNING
            matricula.confidence = 0.49

            write_outputs(
                [_reporte("test.pdf", page)],
                self._options(Path(tmp), skip_pdfs=True),
            )

            assert page.airvault_review is False

    def test_las_discrepancias_no_se_repiten_en_revisar(self):
        """En varios PDF van a discrepancias.pdf, y solo ahi."""
        with tempfile.TemporaryDirectory() as tmp:
            discrepante = _page(1, "2147337", "HP-1534CMP", firmas=False)
            _firma(discrepante, "technician_signature", "false", 0.99)
            _firma(discrepante, "technician_license", "false", 0.99)
            _firma(discrepante, "pilot_signature", "false", 0.99)
            _firma(discrepante, "captain_signature", "true", 0.99)
            _firma(discrepante, "captain_license", "true", 0.99)
            limpia = _page(2, "2147338", "HP-1534CMP")

            run = write_outputs(
                [_reporte("test.pdf", discrepante, limpia)],
                self._options(
                    Path(tmp), ("avion",), discrepancias=True
                ),
            )

            assert (run / "discrepancias.pdf").is_file()
            assert not (run / "revisar.pdf").exists()
            assert (run / "HP-1534CMP.pdf").is_file()

    def test_una_discrepancia_confirmada_va_al_batch_revisar(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = _page(1, "2147337", "HP-1534CMP", firmas=False)
            _firma(page, "technician_signature", "false", 0.99)
            _firma(page, "technician_license", "false", 0.99)
            _firma(page, "pilot_signature", "true", 0.99)
            _firma(page, "captain_signature", "false", 0.99)
            _firma(page, "captain_license", "true", 0.99)

            write_outputs(
                [_reporte("test.pdf", page)],
                self._options(Path(tmp), skip_pdfs=True),
            )

            assert page.airvault_review is True


if __name__ == "__main__":
    unittest.main()
