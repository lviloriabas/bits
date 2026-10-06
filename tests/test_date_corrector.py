"""Pruebas de inferencia de mes y ano guiada por ``log_number``."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from datetime import date

from app.models.schemas import FieldResult, PageResult, Status, ValidationReport
from app.validation.date_corrector import _format_month, correct_dates_by_book


def _field(
    pn: int,
    fid: str,
    value,
    status=Status.OK,
    conf=0.9,
    source="direct",
) -> FieldResult:
    return FieldResult(
        page_number=pn,
        field_id=fid,
        field_type="ocr",
        value=value,
        status=status,
        confidence=conf,
        source=source,
    )


def _page(
    pn: int,
    log: str | None,
    day=None,
    month=None,
    year=None,
    month_status=Status.OK,
    year_status=Status.OK,
) -> PageResult:
    page = PageResult(page_number=pn)
    page.add_field(_field(pn, "log_number", log))
    page.add_field(_field(pn, "day", day))
    page.add_field(_field(pn, "month", month, status=month_status))
    page.add_field(_field(pn, "year", year, status=year_status))
    return page


def _report(*pages: PageResult, name="fixture.pdf") -> ValidationReport:
    return ValidationReport(
        pdf_path=name,
        template_name="fixture",
        pages=list(pages),
    )


def _field_of(page: PageResult, field_id: str) -> FieldResult:
    return next(field for field in page.fields if field.field_id == field_id)


class TestLogNumberOrder(unittest.TestCase):
    def test_uses_log_number_not_pdf_order(self):
        first = _page(1, "2147301", "20", "JUL", "26")
        middle = _page(2, "2147302", "20", None, None)
        last = _page(3, "2147303", "20", "JUL", "26")

        # El PDF llega en el orden 3, 1, 2. La inferencia debe seguir 1, 2, 3.
        correct_dates_by_book([_report(last, first, middle)])

        month = _field_of(middle, "month")
        year = _field_of(middle, "year")
        self.assertEqual(month.value, "JUL")
        self.assertEqual(year.value, "26")
        self.assertEqual(month.source, "inferred")
        self.assertEqual(year.source, "inferred")
        self.assertEqual(middle.date, "2026/07/20")


class TestMonthAndYearInference(unittest.TestCase):
    def test_infers_between_equal_anchors(self):
        before = _page(1, "2147301", "20", "JUL", "26")
        missing = _page(2, "2147302", "21", None, None)
        after = _page(3, "2147303", "22", "JUL", "26")

        stats = correct_dates_by_book([_report(before, missing, after)])

        self.assertEqual(_field_of(missing, "month").value, "JUL")
        self.assertEqual(_field_of(missing, "year").value, "26")
        self.assertEqual(missing.date, "2026/07/21")
        self.assertEqual(stats["months_filled"], 1)
        self.assertEqual(stats["years_filled"], 1)

    def test_corrects_conflicting_anchor_before_inference(self):
        first = _page(1, "2147301", "20", "JUL", "26")
        middle = _page(2, "2147302", "21", None, None)
        conflicting = _page(3, "2147303", "22", "AUG", "26")
        last = _page(4, "2147304", "23", "JUL", "26")

        stats = correct_dates_by_book([
            _report(first, middle, conflicting, last)
        ])

        self.assertEqual(_field_of(middle, "month").value, "JUL")
        self.assertEqual(_field_of(conflicting, "month").value, "JUL")
        self.assertEqual(_field_of(middle, "year").value, "26")
        self.assertEqual(middle.date, "2026/07/21")
        self.assertEqual(stats["bracket_corrected"], 1)

    def test_short_edge_is_inferred_from_two_local_anchors(self):
        missing = _page(1, "2147301", "20", None, None)
        second = _page(2, "2147302", "20", "JUL", "26")
        third = _page(3, "2147303", "21", "JUL", "26")

        correct_dates_by_book([_report(missing, second, third)])

        self.assertEqual(_field_of(missing, "month").value, "JUL")
        self.assertEqual(_field_of(missing, "year").value, "26")
        self.assertEqual(missing.date, "2026/07/20")

    def test_short_edge_corrects_years_decades_from_local_anchors(self):
        first = _page(
            1, "2147301", "20", "AGO", "01",
            year_status=Status.WARNING,
        )
        second = _page(
            2, "2147302", "20", "AGO", "06",
            year_status=Status.WARNING,
        )
        third = _page(3, "2147303", "20", "AGO", "26")
        last = _page(4, "2147304", "21", "AGO", "26")
        last_year = _field_of(last, "year")
        last_year.source = "date_cells"
        last_year.inference_method = "date_cells"
        last_year.confidence = 0.56
        last_year.alternatives = ["20"]

        correct_dates_by_book([_report(first, second, third, last)])

        self.assertEqual(_field_of(first, "year").value, "26")
        self.assertEqual(_field_of(second, "year").value, "26")
        self.assertEqual(first.date, "2026/08/20")
        self.assertEqual(
            _field_of(first, "year").inference_method,
            "log_number_edge_correction",
        )

    def test_short_edge_corrects_a_month_that_goes_back(self):
        first = _page(1, "2147301", "19", "AGO", "26")
        second = _page(2, "2147302", "20", "AGO", "26")
        wrong = _page(
            3, "2147303", "21", "JUL", "26",
            month_status=Status.WARNING,
        )
        month = _field_of(wrong, "month")
        month.confidence = 0.49
        month.source = "date_cells"
        month.inference_method = "date_cells"

        correct_dates_by_book([_report(first, second, wrong)])

        self.assertEqual(month.value, "AGO")
        self.assertEqual(month.inference_method, "log_number_edge_correction")
        self.assertEqual(wrong.date, "2026/08/21")

    def test_invalid_three_digit_year_can_be_recovered(self):
        first = _page(1, "2147301", "20", "JUL", "26")
        invalid = _page(
            2,
            "2147302",
            "20",
            "JUL",
            "216",
            year_status=Status.ERROR,
        )
        last = _page(3, "2147303", "20", "JUL", "26")

        correct_dates_by_book([_report(first, invalid, last)])

        year = _field_of(invalid, "year")
        self.assertEqual(year.value, "26")
        self.assertEqual(year.source, "inferred")
        self.assertEqual(invalid.date, "2026/07/20")

    def test_valid_conflicting_reading_is_corrected_by_two_anchors(self):
        first = _page(1, "2147301", "20", "JUL", "26")
        conflicting = _page(2, "2147302", "21", "AUG", "26")
        last = _page(3, "2147303", "22", "JUL", "26")

        stats = correct_dates_by_book([_report(first, conflicting, last)])

        month = _field_of(conflicting, "month")
        self.assertEqual(month.value, "JUL")
        self.assertIs(month.status, Status.WARNING)
        self.assertEqual(month.source, "book_correction")
        self.assertEqual(month.inference_method, "log_number_bracket")
        self.assertEqual(stats["bracket_corrected"], 1)

    def test_warning_reading_does_not_become_an_anchor(self):
        doubtful = _page(
            1,
            "2147301",
            "20",
            "JUL",
            "26",
            month_status=Status.WARNING,
        )
        missing = _page(2, "2147302", "21", None, None)
        reliable = _page(3, "2147303", "22", "JUL", "26")

        correct_dates_by_book([_report(doubtful, missing, reliable)])

        self.assertIsNone(_field_of(missing, "month").value)
        self.assertEqual(_field_of(missing, "year").value, "26")

    def test_fuzzy_month_does_not_become_an_anchor(self):
        doubtful = _page(1, "2147301", "20", "DIC", "26")
        fuzzy = _field_of(doubtful, "month")
        fuzzy.comment = "month fuzzy: 50c"
        missing = _page(2, "2147302", "21", None, None)
        reliable = _page(3, "2147303", "22", "DIC", "26")

        correct_dates_by_book([_report(doubtful, missing, reliable)])

        self.assertIsNone(_field_of(missing, "month").value)

    def test_exact_positional_month_warning_can_anchor_inference(self):
        before = _page(
            1, "2147301", "20", "JUL", "26", month_status=Status.WARNING
        )
        before_month = _field_of(before, "month")
        before_month.confidence = 0.40
        before_month.source = "ocr_fallback"
        before_month.inference_method = "ranuras"
        missing = _page(2, "2147302", "21", None, "26")
        after = _page(3, "2147303", "22", "JUL", "26")

        stats = correct_dates_by_book([_report(before, missing, after)])

        self.assertEqual(_field_of(missing, "month").value, "JUL")
        self.assertEqual(missing.date, "2026/07/21")
        self.assertEqual(stats["months_filled"], 1)

    def test_inferred_reading_does_not_feed_a_second_inference(self):
        inferred = _page(1, "2147301", "20", "JUL", "26")
        _field_of(inferred, "month").source = "inferred"
        _field_of(inferred, "year").source = "inferred"
        missing = _page(2, "2147302", "21", None, None)
        reliable = _page(3, "2147303", "22", "JUL", "26")

        correct_dates_by_book([_report(inferred, missing, reliable)])

        self.assertIsNone(_field_of(missing, "month").value)
        self.assertIsNone(_field_of(missing, "year").value)


class TestDayPolicy(unittest.TestCase):
    def test_read_day_is_never_overwritten(self):
        first = _page(1, "2147301", "20", "JUL", "26")
        read = _page(2, "2147302", "25", "JUL", "26")
        last = _page(3, "2147303", "28", "JUL", "26")

        stats = correct_dates_by_book([_report(first, read, last)])

        day = _field_of(read, "day")
        self.assertEqual(day.value, "25")
        self.assertEqual(day.source, "direct")
        self.assertEqual(stats["days_filled"], 0)

    def test_missing_day_takes_the_last_one_that_fits_the_sequence(self):
        first = _page(1, "2147301", "20", "JUL", "26")
        missing_day = _page(2, "2147302", None, "JUL", "26")
        last = _page(3, "2147303", "22", "JUL", "26")

        stats = correct_dates_by_book([_report(first, missing_day, last)])

        day = _field_of(missing_day, "day")
        self.assertEqual(day.value, "22")
        self.assertIs(day.status, Status.WARNING)
        self.assertEqual(day.source, "inferred")
        self.assertEqual(day.inference_method, "month_end_fallback")
        self.assertEqual(missing_day.date, "2026/07/22")
        self.assertEqual(stats["days_filled"], 1)

    def test_missing_day_at_the_end_takes_the_last_day_of_the_month(self):
        first = _page(1, "2147301", "20", "JUL", "26")
        missing_day = _page(2, "2147302", None, "JUL", "26")

        correct_dates_by_book([_report(first, missing_day)])

        self.assertEqual(_field_of(missing_day, "day").value, "31")
        self.assertEqual(missing_day.date, "2026/07/31")

    def test_missing_day_does_not_block_inferred_month_and_year(self):
        first = _page(1, "2147301", "20", "JUL", "26")
        missing_day = _page(2, "2147302", None, None, None)
        last = _page(3, "2147303", "20", "JUL", "26")

        correct_dates_by_book([_report(first, missing_day, last)])

        self.assertEqual(_field_of(missing_day, "month").value, "JUL")
        self.assertEqual(_field_of(missing_day, "year").value, "26")
        self.assertEqual(_field_of(missing_day, "day").value, "20")
        self.assertEqual(missing_day.date, "2026/07/20")

    def test_day_is_not_filled_without_month_and_year(self):
        page = _page(1, "2147301", None, None, None)

        stats = correct_dates_by_book([_report(page)])

        self.assertIsNone(_field_of(page, "day").value)
        self.assertIsNone(page.date)
        self.assertEqual(stats["days_filled"], 0)


class TestSequenceCandidates(unittest.TestCase):
    def test_chooses_ocr_alternatives_only_to_remove_regression(self):
        first = _page(1, "2147301", "28", "JUL", "26")
        ambiguous = _page(2, "2147302", "27", "JUL", "24")
        last = _page(3, "2147303", "30", "JUL", "26")
        _field_of(ambiguous, "day").alternatives = ["29"]
        _field_of(ambiguous, "year").alternatives = ["26"]

        stats = correct_dates_by_book([_report(first, ambiguous, last)])

        self.assertEqual(ambiguous.date, "2026/07/29")
        self.assertEqual(_field_of(ambiguous, "day").value, "29")
        self.assertEqual(_field_of(ambiguous, "year").value, "26")
        self.assertEqual(stats["sequence_candidates"], 1)
        self.assertEqual(stats["years_consensus"], 1)
        self.assertEqual(
            _field_of(ambiguous, "year").inference_method,
            "log_number_year_consensus",
        )
        self.assertEqual(
            _field_of(ambiguous, "day").inference_method,
            "log_number_sequence_candidate",
        )

    def test_does_not_change_nondecreasing_readings(self):
        first = _page(1, "2147301", "25", "JUL", "26")
        second = _page(2, "2147302", "26", "JUL", "26")
        _field_of(second, "day").alternatives = ["28"]

        stats = correct_dates_by_book([_report(first, second)])

        self.assertEqual(second.date, "2026/07/26")
        self.assertEqual(_field_of(second, "day").value, "26")
        self.assertEqual(stats["sequence_candidates"], 0)

    def test_never_uses_an_alternative_from_another_book(self):
        late = _page(1, "2147301", "30", "JUL", "26")
        earlier_other_book = _page(2, "2147351", "20", "JUL", "26")
        _field_of(earlier_other_book, "day").alternatives = ["31"]

        stats = correct_dates_by_book([
            _report(late, earlier_other_book)
        ])

        self.assertEqual(earlier_other_book.date, "2026/07/20")
        self.assertEqual(stats["sequence_candidates"], 0)
        self.assertEqual(stats["days_filled"], 0)


class TestYearConsensus(unittest.TestCase):
    def test_corrects_nonadjacent_year_outliers_to_book_majority(self):
        pages = [
            _page(1, "2147301", "20", "JUL", "26"),
            _page(2, "2147302", "20", "JUL", "24"),
            _page(3, "2147303", "21", "JUL", "26"),
            _page(4, "2147304", "21", "JUL", "21"),
            _page(5, "2147305", "22", "JUL", "26"),
        ]

        stats = correct_dates_by_book([_report(*pages)])

        self.assertEqual(_field_of(pages[1], "year").value, "26")
        self.assertEqual(_field_of(pages[3], "year").value, "26")
        self.assertEqual(pages[1].date, "2026/07/20")
        self.assertEqual(stats["years_consensus"], 2)
        self.assertEqual(
            _field_of(pages[1], "year").inference_method,
            "log_number_year_consensus",
        )

    def test_preserves_real_adjacent_year_rollover_at_book_edge(self):
        previous_year = _page(1, "2147301", "31", "DIC", "25")
        new_year = _page(2, "2147302", "01", "ENE", "26")
        later = _page(3, "2147303", "02", "ENE", "26")

        stats = correct_dates_by_book([
            _report(previous_year, new_year, later)
        ])

        self.assertEqual(previous_year.date, "2025/12/31")
        self.assertEqual(new_year.date, "2026/01/01")
        self.assertEqual(stats["years_consensus"], 0)

    def test_corrects_adjacent_year_when_it_is_inside_majority_block(self):
        first = _page(1, "2147301", "20", "JUL", "26")
        outlier = _page(2, "2147302", "21", "JUL", "25")
        last = _page(3, "2147303", "22", "JUL", "26")

        stats = correct_dates_by_book([_report(first, outlier, last)])

        self.assertEqual(_field_of(outlier, "year").value, "26")
        self.assertEqual(stats["years_consensus"], 1)

    def test_does_not_force_a_year_without_clear_majority(self):
        pages = [
            _page(1, "2147301", "20", "JUL", "25"),
            _page(2, "2147302", "21", "JUL", "25"),
            _page(3, "2147303", "22", "JUL", "26"),
            _page(4, "2147304", "23", "JUL", "26"),
        ]

        stats = correct_dates_by_book([_report(*pages)])

        self.assertEqual([_field_of(page, "year").value for page in pages],
                         ["25", "25", "26", "26"])
        self.assertEqual(stats["years_consensus"], 0)


class TestRunYearConsensus(unittest.TestCase):
    def test_uses_a_widespread_year_only_as_an_ocr_tiebreaker(self):
        pages = []
        page_number = 1
        for prefix in ("21473", "21474", "21475"):
            for suffix in range(1, 5):
                pages.append(_page(
                    page_number, f"{prefix}{suffix:02d}",
                    "20", "AGO", "26",
                ))
                page_number += 1

        doubtful = _page(page_number, "2147601", "20", "AGO", "16")
        doubtful_year = _field_of(doubtful, "year")
        doubtful_year.source = "date_cells"
        doubtful_year.alternatives = ["26"]
        pages.append(doubtful)

        old_anchor = _page(page_number + 1, "2147751", "20", "JUL", "20")
        old_doubtful = _page(
            page_number + 2, "2147752", "21", "JUL", "20"
        )
        _field_of(old_doubtful, "year").alternatives = ["26"]
        pages.extend((old_anchor, old_doubtful))

        stats = correct_dates_by_book([_report(*pages)])

        self.assertEqual(doubtful.date, "2026/08/20")
        self.assertEqual(doubtful_year.inference_method, "run_year_consensus")
        self.assertEqual(stats["run_year_consensus"], 1)
        self.assertEqual(old_anchor.date, "2020/07/20")
        self.assertEqual(old_doubtful.date, "2020/07/21")

    def test_marks_an_isolated_crazy_year_when_ocr_cannot_correct_it(self):
        pages = []
        page_number = 1
        for prefix in ("21473", "21474", "21475"):
            for suffix in range(1, 5):
                pages.append(_page(
                    page_number, f"{prefix}{suffix:02d}",
                    "20", "AGO", "26",
                ))
                page_number += 1

        # Marzo no aparece en la ejecución: nada dice que sea 2026 y no
        # un libro de 2024, así que el año queda para revisión.
        suspicious = _page(
            page_number, "2147601", "20", "MAR", "24"
        )
        pages.append(suspicious)

        stats = correct_dates_by_book([_report(*pages)])

        self.assertIsNone(suspicious.date)
        self.assertEqual(_field_of(suspicious, "year").value, "24")
        self.assertTrue(suspicious.date_review)
        self.assertIs(_field_of(suspicious, "year").status, Status.ERROR)
        self.assertEqual(stats["run_year_review"], 1)

    def test_preserves_a_different_year_supported_twice_in_its_book(self):
        pages = []
        page_number = 1
        for prefix in ("21473", "21474", "21475"):
            for suffix in range(1, 5):
                pages.append(_page(
                    page_number, f"{prefix}{suffix:02d}",
                    "20", "AGO", "26",
                ))
                page_number += 1
        old_first = _page(page_number, "2147601", "20", "AGO", "24")
        old_second = _page(
            page_number + 1, "2147602", "21", "AGO", "24"
        )
        pages.extend((old_first, old_second))

        correct_dates_by_book([_report(*pages)])

        # Dos lecturas del libro sostienen el año: no se corrige ni se revisa.
        self.assertFalse(old_first.date_review)
        self.assertFalse(old_second.date_review)
        self.assertEqual(old_first.date, "2024/08/20")
        self.assertEqual(old_second.date, "2024/08/21")

    def test_a_lone_old_year_in_a_month_of_the_run_takes_the_run_year(self):
        pages = []
        page_number = 1
        for prefix in ("21473", "21474", "21475", "21476", "21477"):
            for suffix in range(1, 5):
                pages.append(_page(
                    page_number, f"{prefix}{suffix:02d}",
                    "20", "AGO", "26",
                ))
                page_number += 1
        far = _page(page_number, "2148601", "14", "AGO", "20")
        near = _page(page_number + 1, "2148801", "15", "AGO", "25")
        pages.extend((far, near))

        stats = correct_dates_by_book([_report(*pages)])

        for page in (far, near):
            year = _field_of(page, "year")
            self.assertEqual(year.value, "26")
            self.assertEqual(year.inference_method, "run_year_month_fit")
            self.assertFalse(page.date_review)
        self.assertEqual(far.date, "2026/08/14")
        self.assertEqual(near.date, "2026/08/15")
        self.assertEqual(stats["run_year_review"], 0)


class TestYearOutliers(unittest.TestCase):
    """El año que un paso anterior dejó fuera de su libro vuelve a él."""

    def test_a_year_a_corrector_left_outside_the_book_is_returned(self):
        from app.validation.date_corrector import _correct_year_outliers

        pages = [
            _page(n, f"21473{n:02d}", f"{10 + n}", "AGO", "26")
            for n in range(1, 6)
        ]
        wrong = _field_of(pages[2], "year")
        wrong.value = "06"
        wrong.source = "book_correction"

        self.assertEqual(_correct_year_outliers(pages), 1)
        self.assertEqual(wrong.value, "26")
        self.assertIn("06", wrong.alternatives)
        self.assertEqual(wrong.inference_method, "book_year_outlier")
        self.assertEqual(pages[2].date, "2026/08/13")

    def test_the_year_change_at_the_edge_of_the_book_is_kept(self):
        from app.validation.date_corrector import _correct_year_outliers

        pages = [
            _page(1, "2147301", "31", "DIC", "25"),
            _page(2, "2147302", "01", "ENE", "26"),
            _page(3, "2147303", "02", "ENE", "26"),
        ]

        self.assertEqual(_correct_year_outliers(pages), 0)
        self.assertEqual(_field_of(pages[0], "year").value, "25")

    def test_the_previous_year_between_two_of_the_majority_is_returned(self):
        from app.validation.date_corrector import _correct_year_outliers

        pages = [
            _page(1, "2147301", "20", "AGO", "26"),
            _page(2, "2147302", "21", "AGO", "25"),
            _page(3, "2147303", "22", "AGO", "26"),
        ]

        self.assertEqual(_correct_year_outliers(pages), 1)
        self.assertEqual(_field_of(pages[1], "year").value, "26")


class TestSafetyBoundaries(unittest.TestCase):
    def test_unreadable_log_number_is_not_positionally_inferred(self):
        """Sin número no hay posición, así que no hay tramo que interpolar.

        El libro trae dos meses, de modo que la página sin número tampoco
        puede resolverse por el consenso del libro: no hay un único valor
        posible y la fecha se queda sin decidir.
        """
        first = _page(1, "2147301", "20", "JUL", "26")
        unknown = _page(2, None, "20", None, None)
        last = _page(3, "2147303", "20", "AGO", "26")

        correct_dates_by_book([_report(first, unknown, last)])

        self.assertIsNone(_field_of(unknown, "month").value)
        self.assertIsNone(unknown.date)

    def test_a_single_month_book_fills_the_pages_it_could_not_read(self):
        """Un libro con un solo mes no deja otra opción para sus huecos."""
        first = _page(1, "2147301", "20", "JUL", "26")
        last = _page(2, "2147303", "22", "JUL", "26")
        # Lejos de las dos anclas: ni el intervalo ni el extremo llegan
        # hasta aquí, solo el consenso del libro.
        unknown = _page(3, "2147330", "24", None, None)

        correct_dates_by_book([_report(first, last, unknown)])

        month = _field_of(unknown, "month")
        self.assertEqual(month.value, "JUL")
        self.assertIs(month.status, Status.WARNING)
        self.assertEqual(month.inference_method, "book_consensus")
        self.assertEqual(unknown.date, "2026/07/24")

    def test_year_change_is_not_filled_across_conflicting_anchors(self):
        first = _page(1, "2147301", "31", "DIC", "25")
        missing = _page(2, "2147302", "01", "ENE", None)
        last = _page(3, "2147303", "02", "ENE", "26")

        correct_dates_by_book([_report(first, missing, last)])

        year = _field_of(missing, "year")
        self.assertIsNone(year.value)
        self.assertIs(year.status, Status.WARNING)

    def test_unresolved_date_is_explicit(self):
        page = _page(1, "2147301", None, None, None)

        correct_dates_by_book([_report(page)])

        self.assertIsNone(page.date)
        self.assertIn("unresolved", _field_of(page, "year").comment)


class TestRunWindow(unittest.TestCase):
    """Un año posterior a la ejecución es una lectura, no una bitácora."""

    def test_year_after_the_run_is_not_a_reading(self):
        first = _page(1, "2147301", "20", "JUL", "26")
        ahead = _page(2, "2147302", "21", "JUL", "96")
        last = _page(3, "2147303", "22", "JUL", "26")

        stats = correct_dates_by_book([_report(first, ahead, last)])

        year = _field_of(ahead, "year")
        self.assertEqual(year.value, "26")
        self.assertEqual(year.source, "inferred")
        self.assertEqual(ahead.date, "2026/07/21")
        self.assertEqual(stats["after_the_run"], 1)

    @patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
    def test_future_year_already_in_error_is_cleared_and_recovered(self, _clock):
        for value in ("28", "2028"):
            with self.subTest(value=value):
                first = _page(1, "2147301", "20", "JUL", "26")
                ahead = _page(2, "2147302", "21", "JUL", value,
                              year_status=Status.ERROR)
                last = _page(3, "2147303", "22", "JUL", "26")

                stats = correct_dates_by_book([_report(first, ahead, last)])

                year = _field_of(ahead, "year")
                self.assertEqual(year.value, "26")
                self.assertIn(value, year.alternatives)
                self.assertEqual(ahead.date, "2026/07/21")
                self.assertEqual(stats["after_the_run"], 1)

    @patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
    def test_future_year_in_error_without_evidence_stays_empty(self, _clock):
        page = _page(1, "2147301", "21", "JUL", "28", year_status=Status.ERROR)

        correct_dates_by_book([_report(page)])

        self.assertIsNone(_field_of(page, "year").value)
        self.assertIn("28", _field_of(page, "year").alternatives)
        self.assertIsNone(page.date)

    def test_year_after_the_run_never_anchors_the_book(self):
        # Sin nada que leer alrededor, la página se queda sin fecha y va a
        # revisión: es preferible a indexarla setenta años fuera de sitio.
        ahead = _page(1, "2147301", "21", "JUL", "96")
        alone = _page(2, "2147302", "22", None, None)

        correct_dates_by_book([_report(ahead, alone)])

        self.assertIsNone(ahead.date)
        self.assertIsNone(alone.date)
        self.assertIn("later than the run", _field_of(ahead, "year").comment)

    def test_an_old_book_is_kept_as_it_was_read(self):
        # Se indexan pocas, pero llegan: la ventana ordena, no descarta.
        first = _page(1, "2147301", "20", "JUL", "20")
        middle = _page(2, "2147302", "21", None, None)
        last = _page(3, "2147303", "22", "JUL", "20")

        correct_dates_by_book([_report(first, middle, last)])

        self.assertEqual(first.date, "2020/07/20")
        self.assertEqual(middle.date, "2020/07/21")
        self.assertEqual(last.date, "2020/07/22")

    def test_an_alternative_years_away_does_not_hide_a_regression(self):
        # La regresión la causa el mes de la última página, no el año de
        # la primera: retroceder dos años deja una fecha peor que la que
        # se quería arreglar.
        first = _page(1, "2147301", "20", "AGO", "26")
        _field_of(first, "year").alternatives = ["24"]
        second = _page(2, "2147302", "20", "JUL", "26")

        correct_dates_by_book([_report(first, second)])

        self.assertEqual(_field_of(first, "year").value, "26")
        self.assertEqual(first.date, "2026/08/20")


    @patch("app.utils.date_window.reference_date", return_value=date(2026, 9, 25))
    def test_month_after_the_run_is_not_a_reading(self, _clock):
        # Un AGO leído OCT en una ejecución de agosto no es una bitácora
        # adelantada: es el mes mal leído.
        run_month = 9
        ahead_month = _format_month(run_month + 1)
        year = "26"
        first = _page(1, "2147301", "19", _format_month(run_month), year)
        ahead = _page(2, "2147302", "20", ahead_month, year)
        last = _page(3, "2147303", "21", _format_month(run_month), year)

        stats = correct_dates_by_book([_report(first, ahead, last)])

        month = _field_of(ahead, "month")
        self.assertEqual(month.value, _format_month(run_month))
        self.assertEqual(month.source, "inferred")
        self.assertIn(ahead_month, month.alternatives)
        self.assertEqual(stats["after_the_run"], 1)


class TestBracketedMonth(unittest.TestCase):
    """El mes que contradice a sus dos vecinas es una lectura equivocada."""

    def test_weak_month_between_two_equal_readings_is_corrected(self):
        first = _page(1, "2147301", "19", "AGO", "26")
        wrong = _page(2, "2147302", "20", "JUL", "26",
                      month_status=Status.WARNING)
        _field_of(wrong, "month").confidence = 0.47
        _field_of(wrong, "month").source = "date_cells"
        last = _page(3, "2147303", "20", "AGO", "26")

        stats = correct_dates_by_book([_report(first, wrong, last)])

        month = _field_of(wrong, "month")
        self.assertEqual(month.value, "AGO")
        self.assertEqual(month.source, "book_correction")
        self.assertEqual(month.inference_method, "log_number_bracket")
        self.assertIn("JUL", month.alternatives)
        self.assertEqual(wrong.date, "2026/08/20")
        self.assertEqual(stats["bracket_corrected"], 1)

    def test_the_corrected_month_stops_blocking_the_pages_around_it(self):
        # Es el daño que más cuesta: la lectura equivocada rompía además
        # el intervalo que habría completado a la página sin leer.
        first = _page(1, "2147301", "19", "AGO", "26")
        blank_month = _page(2, "2147302", "20", None, "26")
        wrong = _page(3, "2147303", "20", "JUL", "26",
                      month_status=Status.WARNING)
        _field_of(wrong, "month").confidence = 0.47
        _field_of(wrong, "month").source = "date_cells"
        last = _page(4, "2147304", "20", "AGO", "26")

        correct_dates_by_book([_report(first, blank_month, wrong, last)])

        self.assertEqual(_field_of(blank_month, "month").value, "AGO")
        self.assertEqual(blank_month.date, "2026/08/20")

    def test_a_firm_impossible_month_is_corrected(self):
        first = _page(1, "2147301", "19", "AGO", "26")
        firm = _page(2, "2147302", "20", "JUL", "26")
        last = _page(3, "2147303", "20", "AGO", "26")

        stats = correct_dates_by_book([_report(first, firm, last)])

        self.assertEqual(_field_of(firm, "month").value, "AGO")
        self.assertEqual(stats["bracket_corrected"], 1)

    def test_two_equal_months_of_different_years_do_not_fix_the_middle(self):
        # Entre JUL de un año y JUL del siguiente cabe cualquier mes.
        first = _page(1, "2147301", "20", "JUL", "25")
        middle = _page(2, "2147302", "20", "AGO", "25",
                       month_status=Status.WARNING)
        _field_of(middle, "month").confidence = 0.47
        _field_of(middle, "month").source = "date_cells"
        last = _page(3, "2147303", "20", "JUL", "26")

        stats = correct_dates_by_book([_report(first, middle, last)])

        self.assertEqual(_field_of(middle, "month").value, "AGO")
        self.assertEqual(stats["bracket_corrected"], 0)


class TestDaySequence(unittest.TestCase):
    """El día que retrocede dentro del libro está mal leído."""

    def test_a_day_that_goes_back_is_pulled_into_the_book(self):
        first = _page(1, "2147301", "19", "AGO", "26")
        lost = _page(2, "2147302", "4", "AGO", "26")
        last = _page(3, "2147303", "19", "AGO", "26")

        stats = correct_dates_by_book([_report(first, lost, last)])

        day = _field_of(lost, "day")
        self.assertEqual(day.value, "19")
        self.assertEqual(day.source, "book_correction")
        self.assertEqual(day.inference_method, "log_number_day_sequence")
        self.assertIn("4", day.alternatives)
        self.assertEqual(lost.date, "2026/08/19")
        self.assertEqual(stats["days_repaired"], 1)

    def test_the_tens_digit_the_ocr_proposed_wins_over_any_other_day(self):
        first = _page(1, "2147301", "17", "AGO", "26")
        lost = _page(2, "2147302", "7", "AGO", "26")
        last = _page(3, "2147303", "18", "AGO", "26")

        correct_dates_by_book([_report(first, lost, last)])

        self.assertEqual(_field_of(lost, "day").value, "17")

    def test_a_firm_day_is_not_moved_by_two_doubtful_ones(self):
        firm = _page(1, "2147301", "17", "AGO", "26")
        doubtful = _page(2, "2147302", "7", "AGO", "26")
        _field_of(doubtful, "day").confidence = 0.4
        second_doubtful = _page(3, "2147303", "7", "AGO", "26")
        _field_of(second_doubtful, "day").confidence = 0.4
        after = _page(4, "2147304", "18", "AGO", "26")

        correct_dates_by_book([
            _report(firm, doubtful, second_doubtful, after)
        ])

        self.assertEqual(_field_of(firm, "day").value, "17")
        self.assertEqual(_field_of(after, "day").value, "18")
        self.assertEqual(_field_of(doubtful, "day").value, "17")

    def test_a_book_that_does_not_go_back_is_left_alone(self):
        pages = [
            _page(1, "2147301", "30", "JUL", "26"),
            _page(2, "2147302", "31", "JUL", "26"),
            _page(3, "2147303", "01", "AGO", "26"),
            _page(4, "2147304", "02", "AGO", "26"),
        ]

        stats = correct_dates_by_book([_report(*pages)])

        self.assertEqual(
            [_field_of(page, "day").value for page in pages],
            ["30", "31", "01", "02"],
        )
        self.assertEqual(stats["days_repaired"], 0)

    def test_a_lone_single_digit_day_recovers_its_tens(self):
        # Abre el tramo, así que no retrocede y nada lo delata salvo la
        # distancia: un libro se llena en días seguidos.
        lost = _page(1, "2147301", "8", "AGO", "26")
        after = _page(2, "2147302", "19", "AGO", "26")
        last = _page(3, "2147303", "19", "AGO", "26")

        stats = correct_dates_by_book([_report(lost, after, last)])

        day = _field_of(lost, "day")
        self.assertEqual(day.value, "18")
        self.assertEqual(day.inference_method, "lost_tens_digit")
        self.assertIn("8", day.alternatives)
        self.assertEqual(stats["days_repaired"], 1)

    def test_a_book_written_at_the_start_of_the_month_keeps_its_days(self):
        first = _page(1, "2147301", "3", "AGO", "26")
        second = _page(2, "2147302", "4", "AGO", "26")
        last = _page(3, "2147303", "5", "AGO", "26")

        stats = correct_dates_by_book([_report(first, second, last)])

        self.assertEqual(first.date, "2026/08/03")
        self.assertEqual(last.date, "2026/08/05")
        self.assertEqual(stats["days_repaired"], 0)


if __name__ == "__main__":
    unittest.main()
