"""Pruebas de las puertas finales de formato del reporter CSV."""

from __future__ import annotations

import csv
from datetime import date
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.models.schemas import FieldResult, PageResult, ValidationReport
from app.reports.csv_reporter import (
    CSV_DATE_MONTH_END,
    CSV_DATE_SPECIFIC,
    CsvReporter,
)
from app.templates.schema import FieldTemplate, Template


def _field(fid: str, value, conf=0.9) -> FieldResult:
    return FieldResult(page_number=1, field_id=fid, field_type="ocr",
                       value=value, confidence=conf)


def _report(*pages: PageResult) -> ValidationReport:
    return ValidationReport(pdf_path="fixture.pdf", template_name="fixture",
                            pages=list(pages))


class TestCsvGates(unittest.TestCase):
    def _rows(
        self, page: PageResult, date_mode: str = CSV_DATE_SPECIFIC
    ) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            CsvReporter().write(_report(page), path, date_mode=date_mode)
            with open(path, encoding="utf-8-sig", newline="") as fh:
                return list(csv.DictReader(fh))[0]

    def test_matricula_gate(self):
        page = PageResult(page_number=1)
        page.add_field(_field("matricula", "All H89916cmp FL /C"))
        row = self._rows(page)
        self.assertEqual(row["matricula"], "")

        page = PageResult(page_number=1)
        page.add_field(_field("matricula", "HP-1534CMP"))
        row = self._rows(page)
        self.assertEqual(row["matricula"], "HP-1534CMP")

    def test_archiving_with_a_suffix_does_not_rename_the_csv_source(self):
        page = PageResult(page_number=1)
        report = ValidationReport(
            pdf_path="input/processed/bitacora-2.pdf",
            source_name="bitacora.pdf",
            template_name="fixture",
            pages=[page],
        )

        row = CsvReporter.row_for_page(report, page, [])

        self.assertEqual(row["file"], "bitacora.pdf")

    def test_date_part_gates(self):
        page = PageResult(page_number=1)
        page.add_field(_field("day", "Schedule Fit 211 DATE"))
        page.add_field(_field("month", "51012"))
        page.add_field(_field("year", "8313"))
        row = self._rows(page)
        self.assertEqual(row["day"], "")
        self.assertEqual(row["month"], "")
        self.assertEqual(row["year"], "")

    def test_three_digit_year_gated_out(self):
        for bad_year in ("723", "216"):
            page = PageResult(page_number=1)
            page.add_field(_field("year", bad_year))
            row = self._rows(page)
            self.assertEqual(row["year"], "")

    @patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
    def test_future_year_never_reappears_from_fields_or_saved_date(self, _clock):
        for year in ("27", "28", "96", "2027", "2096"):
            for mode in (CSV_DATE_SPECIFIC, CSV_DATE_MONTH_END):
                with self.subTest(year=year, mode=mode):
                    full_year = 2000 + int(year) if len(year) == 2 else int(year)
                    page = PageResult(page_number=1, date=f"{full_year}/07/16")
                    page.add_field(_field("day", "16"))
                    page.add_field(_field("month", "JUL"))
                    page.add_field(_field("year", year))
                    row = self._rows(page, mode)
                    self.assertEqual(row["year"], "")
                    self.assertEqual(row["date"], "")
                    self.assertEqual(page.date, f"{full_year}/07/16")

        saved = PageResult(page_number=1, date="2028/07/16")
        self.assertEqual(self._rows(saved)["date"], "")

    @patch("app.utils.date_window.reference_date", return_value=date(2026, 1, 2))
    def test_january_keeps_previous_december_in_both_csv_modes(self, _clock):
        page = PageResult(page_number=1, date="2025/12/30")
        page.add_field(_field("day", "30"))
        page.add_field(_field("month", "DIC"))
        page.add_field(_field("year", "25"))
        self.assertEqual(self._rows(page, CSV_DATE_SPECIFIC)["date"], "2025/12/30")
        self.assertEqual(self._rows(page, CSV_DATE_MONTH_END)["date"], "2025/12/31")

    @patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
    def test_current_month_end_remains_an_accepted_fallback(self, _clock):
        for day in ("01", None, "31"):
            page = PageResult(page_number=1)
            page.add_field(_field("day", day))
            page.add_field(_field("month", "OCT"))
            page.add_field(_field("year", "26"))
            row = self._rows(page, CSV_DATE_MONTH_END)
            self.assertEqual(row["date"], "2026/10/31")
            self.assertEqual(row["day"], "31")

    @patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
    def test_future_day_or_month_cannot_reappear_from_saved_date(self, _clock):
        for month, day in (("OCT", "07"), ("OCT", "31"), ("NOV", "01")):
            number = 10 if month == "OCT" else 11
            page = PageResult(page_number=1, date=f"2026/{number:02d}/{day}")
            page.add_field(_field("day", day))
            page.add_field(_field("month", month))
            page.add_field(_field("year", "26"))
            self.assertEqual(self._rows(page, CSV_DATE_SPECIFIC)["date"], "")
            saved = PageResult(page_number=1, date=page.date)
            self.assertEqual(self._rows(saved)["date"], "")
            if month == "NOV":
                self.assertEqual(self._rows(page, CSV_DATE_MONTH_END)["date"], "")

    @patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
    def test_specific_mode_accepts_month_end_for_unread_day(self, _clock):
        page = PageResult(page_number=1)
        page.add_field(_field("day", None))
        page.add_field(_field("month", "OCT"))
        page.add_field(_field("year", "26"))
        self.assertEqual(self._rows(page, CSV_DATE_SPECIFIC)["date"], "2026/10/31")

    def test_date_column_from_page_date(self):
        page = PageResult(page_number=1, date="2026/07/16")
        page.add_field(_field("day", "16"))
        page.add_field(_field("month", "JUL"))
        page.add_field(_field("year", "26"))
        row = self._rows(page)
        self.assertEqual(row["date"], "2026/07/16")
        self.assertEqual(row["year"], "26")

    def test_specific_day_keeps_a_correct_14(self):
        page = PageResult(page_number=1, date="2026/07/14")
        page.add_field(_field("day", "14"))
        page.add_field(_field("month", "JUL"))
        page.add_field(_field("year", "26"))

        row = self._rows(page, CSV_DATE_SPECIFIC)

        self.assertEqual(row["day"], "14")
        self.assertEqual(row["date"], "2026/07/14")
        self.assertEqual(row["day_source"], "direct")

    def test_specific_day_uses_month_end_only_when_day_is_missing(self):
        page = PageResult(page_number=1)
        page.add_field(_field("day", None, conf=0.0))
        page.add_field(_field("month", "JUL", conf=0.8))
        page.add_field(_field("year", "26", conf=0.7))

        row = self._rows(page, CSV_DATE_SPECIFIC)

        self.assertEqual(row["day"], "31")
        self.assertEqual(row["date"], "2026/07/31")
        self.assertEqual(row["day_status"], "WARNING")
        self.assertEqual(row["day_source"], "csv_date_policy")
        self.assertIn("sin resolver", row["day_comment"])

    def test_month_end_mode_changes_detected_day_without_mutating_page(self):
        page = PageResult(page_number=1, date="2026/07/14")
        page.add_field(_field("day", "14"))
        page.add_field(_field("month", "JUL"))
        page.add_field(_field("year", "26"))

        row = self._rows(page, CSV_DATE_MONTH_END)

        self.assertEqual(row["day"], "31")
        self.assertEqual(row["date"], "2026/07/31")
        self.assertEqual(row["day_source"], "csv_date_policy")
        self.assertEqual(next(f for f in page.fields if f.field_id == "day").value,
                         "14")
        self.assertEqual(page.date, "2026/07/14")

    def test_month_end_mode_respects_leap_year(self):
        page = PageResult(page_number=1)
        page.add_field(_field("day", None))
        page.add_field(_field("month", "FEB"))
        page.add_field(_field("year", "24"))

        row = self._rows(page, CSV_DATE_MONTH_END)

        self.assertEqual(row["day"], "29")
        self.assertEqual(row["date"], "2024/02/29")

    def test_canonical_values_pass(self):
        page = PageResult(page_number=1)
        page.add_field(_field("day", "23"))
        page.add_field(_field("month", "JUL"))
        page.add_field(_field("year", "2026"))
        row = self._rows(page)
        self.assertEqual(row["day"], "23")
        self.assertEqual(row["month"], "JUL")
        self.assertEqual(row["year"], "2026")

    def test_english_and_spanish_canonical_months_pass(self):
        for month in ("JAN", "ENE", "APR", "ABR", "AUG", "AGO", "DEC", "DIC"):
            page = PageResult(page_number=1)
            page.add_field(_field("month", month))
            row = self._rows(page)
            self.assertEqual(row["month"], month)

    def test_signature_columns_omit_status_and_comment(self):
        template = Template(
            name="fixture",
            fields=[
                FieldTemplate(id="pilot_signature", type="signature",
                              x=0.1, y=0.1, w=0.2, h=0.1),
                FieldTemplate(id="matricula", x=0.1, y=0.2, w=0.2, h=0.1),
            ],
        )
        page = PageResult(page_number=1)
        page.add_field(_field("pilot_signature", "presente", 0.8))
        page.add_field(_field("matricula", "HP-1534CMP"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            CsvReporter().write(_report(page), path, template)
            with open(path, encoding="utf-8-sig", newline="") as fh:
                row = list(csv.DictReader(fh))[0]
        self.assertNotIn("pilot_signature_status", row)
        self.assertNotIn("pilot_signature_comment", row)
        self.assertEqual(row["pilot_signature"], "presente")
        self.assertEqual(row["pilot_signature_conf"], "0.8")
        self.assertEqual(row["pilot_signature_source"], "direct")
        self.assertIn("matricula_status", row)
        self.assertIn("matricula_comment", row)

    def test_dup_is_persisted_after_page_for_the_complete_batch(self):
        first = PageResult(page_number=2)
        first.add_field(_field("log_number", "2147300"))
        repeated = PageResult(page_number=9)
        repeated.add_field(FieldResult(
            page_number=9,
            field_id="log_number",
            field_type="ocr",
            value="2147300",
            confidence=0.9,
        ))
        reports = [
            ValidationReport(
                pdf_path="first.pdf",
                template_name="fixture",
                pages=[first],
            ),
            ValidationReport(
                pdf_path="second.pdf",
                template_name="fixture",
                pages=[repeated],
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            CsvReporter().write(reports, path)
            with open(path, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)
                columns = reader.fieldnames

        self.assertEqual(columns[:6], [
            "file",
            "page",
            "log_number",
            "review",
            "dup",
            "disc",
        ])
        # Las dos apariciones de 2147300 se marcan: quien lee el CSV tiene
        # que ver las dos filas del choque, no solo la posterior.
        self.assertEqual([row["dup"] for row in rows], ["true", "true"])

    def test_disc_marks_the_pages_flagged_as_discrepancy(self):
        limpia = PageResult(page_number=1)
        limpia.add_field(_field("log_number", "2147300"))
        marcada = PageResult(page_number=2)
        marcada.add_field(_field("log_number", "2147301"))
        marcada.discrepancy = True

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            CsvReporter().write(_report(limpia, marcada), path)
            with open(path, encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual([row["disc"] for row in rows], ["false", "true"])

    def test_disc_is_written_without_a_log_number_column(self):
        page = PageResult(page_number=1)
        page.add_field(_field("matricula", "HP-1534CMP"))
        row = self._rows(page)
        self.assertEqual(row["disc"], "false")


class TestPageTime(unittest.TestCase):
    """``time_ms`` debe sumar el tiempo real de la bitácora, no varias veces."""

    def _report(self, page_times, total_ms) -> ValidationReport:
        pages = []
        for number, milliseconds in enumerate(page_times, start=1):
            page = PageResult(page_number=number)
            page.processing_ms = milliseconds
            pages.append(page)
        return ValidationReport(
            pdf_path="fixture.pdf",
            template_name="fixture",
            pages=pages,
            processing_ms=total_ms,
        )

    def _times(self, report: ValidationReport) -> list:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            CsvReporter().write(report, path)
            with open(path, encoding="utf-8-sig", newline="") as fh:
                return [float(row["time_ms"]) for row in csv.DictReader(fh)]

    def test_parallel_page_times_are_scaled_to_the_run_clock(self):
        # Cuatro páginas de 10 s medidas dentro de sus procesos mientras el
        # reloj de la bitácora marcaba 10 s: sumadas daban 40 s.
        report = self._report([10_000.0] * 4, 10_000.0)
        times = self._times(report)
        self.assertAlmostEqual(sum(times), 10_000.0, places=0)
        self.assertEqual(times, [2500.0] * 4)

    def test_relative_weight_between_pages_is_preserved(self):
        report = self._report([2_000.0, 6_000.0], 4_000.0)
        times = self._times(report)
        self.assertAlmostEqual(sum(times), 4_000.0, places=0)
        self.assertAlmostEqual(times[1] / times[0], 3.0, places=3)

    def test_sequential_run_keeps_its_measured_times(self):
        # Sin paralelismo la suma ya coincide con el reloj: el factor es 1.
        report = self._report([1_500.0, 2_500.0], 4_000.0)
        self.assertEqual(self._times(report), [1_500.0, 2_500.0])

    def test_missing_run_clock_falls_back_to_the_measured_time(self):
        report = self._report([1_200.0], 0.0)
        self.assertEqual(self._times(report), [1_200.0])


class TestRunTime(unittest.TestCase):
    """El batch se normaliza contra el reloj de la ejecución, no por archivo."""

    def _report(
        self, page_times, total_ms, started_at=0.0
    ) -> ValidationReport:
        pages = []
        for number, milliseconds in enumerate(page_times, start=1):
            page = PageResult(page_number=number)
            page.processing_ms = milliseconds
            pages.append(page)
        return ValidationReport(
            pdf_path="fixture.pdf",
            template_name="fixture",
            pages=pages,
            processing_ms=total_ms,
            started_at=started_at,
        )

    def _times(self, reports) -> list:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            CsvReporter().write(reports, path)
            with open(path, encoding="utf-8-sig", newline="") as fh:
                return [float(row["time_ms"]) for row in csv.DictReader(fh)]

    def test_overlapping_files_are_not_counted_once_per_file(self):
        # Cuatro archivos arrancados a la vez, 100 s cada uno: la ejecución duró
        # 100 s, no 400. Sumar los relojes multiplicaba la columna por cuatro.
        reports = [
            self._report([50_000.0, 50_000.0], 100_000.0, started_at=1_000.0)
            for _ in range(4)
        ]
        self.assertEqual(CsvReporter.run_wall_ms(reports), 100_000.0)
        times = self._times(reports)
        self.assertAlmostEqual(sum(times), 100_000.0, places=0)
        self.assertEqual(times, [12_500.0] * 8)

    def test_staggered_files_span_from_the_first_start_to_the_last_end(self):
        reports = [
            self._report([10_000.0], 10_000.0, started_at=1_000.0),
            self._report([10_000.0], 10_000.0, started_at=1_005.0),
        ]
        # De 1000 a 1015: 15 s de reloj para 20 s de página medidos.
        self.assertEqual(CsvReporter.run_wall_ms(reports), 15_000.0)
        self.assertAlmostEqual(sum(self._times(reports)), 15_000.0, places=0)

    def test_reports_without_a_start_stamp_keep_the_per_file_clock(self):
        reports = [
            self._report([2_000.0, 2_000.0], 2_000.0),
            self._report([1_000.0], 1_000.0),
        ]
        self.assertEqual(CsvReporter.run_wall_ms(reports), 3_000.0)
        self.assertAlmostEqual(sum(self._times(reports)), 3_000.0, places=0)


if __name__ == "__main__":
    unittest.main()
