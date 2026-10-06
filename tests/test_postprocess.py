"""Pruebas de postprocesadores de fecha (día/mes/año y combinación)."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from app.utils.postprocess import AMBIGUOUS_MATRICULA_NOTE, \
    WEAK_MATRICULA_NOTE, apply_postprocess, combine_date


class TestDay(unittest.TestCase):
    def test_valid_days(self):
        for value, expected in (("20", "20"), ("05", "05"), ("1", "1")):
            self.assertEqual(apply_postprocess("x", "day", value),
                             (expected, ""))

    def test_two_digit_run_preferred(self):
        self.assertEqual(apply_postprocess("x", "day", "21 0"), ("21", ""))
        self.assertEqual(apply_postprocess("x", "day", "11 6"), ("11", ""))

    def test_split_by_cell_separator_joined(self):
        # Separador vertical de casilla impreso parte el día en dos tokens.
        self.assertEqual(apply_postprocess("x", "day", "2 0"), ("20", ""))
        self.assertEqual(apply_postprocess("x", "day", "1 5"), ("15", ""))
        self.assertEqual(apply_postprocess("x", "day", "2|6"), ("26", ""))

    def test_split_invalid_range_still_rejected(self):
        value, note = apply_postprocess("x", "day", "3 5")
        self.assertEqual(value, "")
        self.assertIn("invalid day", note)

    def test_three_digit_rejected(self):
        value, note = apply_postprocess("x", "day", "210")
        self.assertEqual(value, "")
        self.assertIn("invalid day", note)

    def test_out_of_range_rejected(self):
        value, note = apply_postprocess("x", "day", "35")
        self.assertEqual(value, "")
        self.assertIn("invalid day", note)
        value, note = apply_postprocess("x", "day", "0")
        self.assertEqual(value, "")
        self.assertIn("invalid day", note)

    def test_no_digits_rejected(self):
        value, note = apply_postprocess("x", "day", "Non-Schedule")
        self.assertEqual(value, "")
        self.assertIn("invalid day", note)

    def test_label_contamination_rejected(self):
        value, note = apply_postprocess(
            "x", "day", "Schedule Fit (X) Non-Schedule Flt (II) 211 DATE"
        )
        self.assertEqual(value, "")
        self.assertIn("invalid day", note)


class TestMonth(unittest.TestCase):
    def test_digits(self):
        value, note = apply_postprocess("x", "month", "07")
        self.assertEqual(value, "7")
        self.assertIn("numeric handwritten month", note)
        value, note = apply_postprocess("x", "month", "001")
        self.assertEqual(value, "")
        self.assertIn("invalid month", note)
        value, note = apply_postprocess("x", "month", "13")
        self.assertEqual(value, "")
        self.assertIn("invalid month", note)

    def test_letters_exact(self):
        self.assertEqual(apply_postprocess("x", "month", "JUL"), ("JUL", ""))
        self.assertEqual(apply_postprocess("x", "month", "dic"),
                         ("DIC", ""))

    def test_fuzzy(self):
        value, note = apply_postprocess("x", "month", "JUIL")
        self.assertEqual(value, "JUL")
        self.assertIn("fuzzy", note)
        value, note = apply_postprocess("x", "month", "GUL")
        self.assertEqual(value, "JUL")
        self.assertIn("fuzzy", note)

    def test_digit_misread_as_letter(self):
        # '1' del separador de casilla leído como dígito -> letra 'i'.
        value, note = apply_postprocess("x", "month", "JU1")
        self.assertEqual(value, "JUL")
        self.assertEqual(note, "")
        value, note = apply_postprocess("x", "month", "JUI")
        self.assertEqual(value, "JUL")
        self.assertEqual(note, "")

    def test_split_by_cell_separator(self):
        # Separador vertical impreso: el OCR devuelve letras partidas.
        value, note = apply_postprocess("x", "month", "J U L")
        self.assertEqual(value, "JUL")
        self.assertEqual(note, "")

    def test_label_contamination(self):
        value, note = apply_postprocess("x", "month", "JUL Month")
        self.assertEqual(value, "JUL")
        self.assertEqual(note, "")

    def test_invalid(self):
        value, note = apply_postprocess("x", "month", "JJ")
        self.assertEqual(value, "")
        self.assertIn("invalid month", note)
        value, note = apply_postprocess("x", "month", "2241 Month YR")
        self.assertEqual(value, "")
        self.assertIn("invalid month", note)
        value, note = apply_postprocess("x", "month", "0")
        self.assertEqual(value, "")
        self.assertIn("invalid month", note)
        value, note = apply_postprocess("x", "month", "51012")
        self.assertEqual(value, "")
        self.assertIn("invalid month", note)
        value, note = apply_postprocess("x", "month", "50c")
        self.assertEqual(value, "")
        self.assertIn("invalid month", note)


class TestYear(unittest.TestCase):
    @patch("app.utils.date_window.reference_date", return_value=date(2026, 10, 6))
    def test_future_years_are_rejected_in_every_ocr_format(self, _clock):
        for raw in ("27", "28", "96", "2027", "2096", "2|8", "Year YR 28"):
            with self.subTest(raw=raw):
                value, note = apply_postprocess("year", "year", raw)
                self.assertEqual(value, "")
                self.assertIn("invalid year", note)

    @patch("app.utils.date_window.reference_date", return_value=date(2027, 1, 2))
    def test_new_year_accepts_current_and_previous_year(self, _clock):
        for raw in ("26", "2026", "27", "2027"):
            self.assertEqual(apply_postprocess("year", "year", raw), (raw, ""))
        self.assertEqual(apply_postprocess("year", "year", "28")[0], "")

    def test_valid(self):
        self.assertEqual(apply_postprocess("x", "year", "26"), ("26", ""))
        self.assertEqual(apply_postprocess("x", "year", "2026"), ("2026", ""))

    def test_label_contamination_extracts_digits(self):
        self.assertEqual(
            apply_postprocess("x", "year", "05450 Year YR 26"), ("26", "")
        )
        self.assertEqual(
            apply_postprocess("x", "year", "Year YR 26"), ("26", "")
        )

    def test_three_digit_kept_for_corrector(self):
        value, note = apply_postprocess("x", "year", "216")
        self.assertEqual(value, "216")
        self.assertIn("invalid year", note)
        self.assertEqual(
            apply_postprocess("x", "year", "05332 Year YR 216"),
            ("216", "invalid year: 216"),
        )

    def test_split_by_cell_separator_joined(self):
        self.assertEqual(apply_postprocess("x", "year", "2 6"), ("26", ""))
        self.assertEqual(apply_postprocess("x", "year", "2|6"), ("26", ""))
        self.assertEqual(apply_postprocess("x", "year", "Year YR 2 6"),
                         ("26", ""))
        # Cuatro dígitos partidos por separadores forman el año completo.
        self.assertEqual(apply_postprocess("x", "year", "2 0 2 6"),
                         ("2026", ""))

    def test_invalid(self):
        value, note = apply_postprocess("x", "year", "Year")
        self.assertEqual(value, "")
        self.assertIn("invalid year", note)
        value, note = apply_postprocess("x", "year", "1")
        self.assertEqual(value, "")
        self.assertIn("invalid year", note)

    def test_implausible_four_digit_rejected(self):
        # Restos del log_number (p. ej. "8313", "5102") no son años.
        value, note = apply_postprocess("x", "year", "8313")
        self.assertEqual(value, "")
        self.assertIn("invalid year", note)
        value, note = apply_postprocess("x", "year", "5102")
        self.assertEqual(value, "")
        self.assertIn("invalid year", note)


class TestMatricula(unittest.TestCase):
    def test_valid(self):
        for value in ("1717", "hp1717", "1717cmp", "HP-1717", "1717 CMP"):
            self.assertEqual(apply_postprocess("x", "matricula", value),
                             ("HP-1717CMP", ""))

    def test_wwp_exceptions(self):
        self.assertEqual(apply_postprocess("x", "matricula", "HP-1990WWP"),
                         ("HP-1990WWP", ""))
        self.assertEqual(apply_postprocess("x", "matricula", "1990"),
                         ("HP-1990WWP", ""))

    def test_el_1522_sale_siempre_como_cmp(self):
        """En la bitacora se escribe de las dos maneras; el avion es CMP.

        AirVault solo lo tiene en su picklist como HP-1522CMP, asi que un
        WWP en el CSV obligaba a corregirlo a mano antes de indexar.
        """
        for value in ("1522", "HP-1522", "HP-1522WWP", "1522 wwp"):
            self.assertEqual(apply_postprocess("x", "matricula", value),
                             ("HP-1522CMP", ""))

    def test_handwritten_characters_read_as_digits(self):
        # El '1' de estas bitácoras es un palo sin base y el '7' lleva
        # travesaño: el reconocedor devuelve I/L/F donde hay dígitos. La
        # ventana anclada entre "HP" y "CMP" recupera la matrícula entera
        # en vez de descartar la página por no tener 4 dígitos seguidos.
        for value, expected in (
            ("HP-I7I7CMP", "HP-1717CMP"),
            ("HP-1F17CMP", "HP-1717CMP"),
            ("HP-1S34CMP", "HP-1534CMP"),
            ("HO1S31CMe", "HP-1531CMP"),
            ("wAT 1Hp i712cmp", "HP-1712CMP"),
        ):
            processed, note = apply_postprocess("x", "matricula", value)
            self.assertEqual(processed, expected, value)
            self.assertEqual(note, AMBIGUOUS_MATRICULA_NOTE, value)

    def test_window_is_not_shifted_by_noise(self):
        # Un dígito suelto detrás delata una ventana ejecución: el número es
        # el que queda entre el prefijo y el sufijo, no el primero que cabe.
        for value, expected in (
            ("40171900", "HP-1719CMP"),
            ("H91534G070", "HP-1534CMP"),
            ("All H89916cmp FL /C", "HP-9916CMP"),
        ):
            self.assertEqual(
                apply_postprocess("x", "matricula", value)[0], expected, value
            )

    def test_scattered_digits_weak(self):
        value, note = apply_postprocess("x", "matricula", "4P-996CmP")
        self.assertEqual(value, "HP-4996CMP")
        self.assertEqual(note, WEAK_MATRICULA_NOTE)

    def test_garbage_returns_empty(self):
        for value in ("All HP-GGIFCal R0", "All itp-g916", "AI", "HP"):
            value, note = apply_postprocess("x", "matricula", value)
            self.assertEqual(value, "")
            self.assertIn("registration without 4-digit number", note)


class TestFlightNumber(unittest.TestCase):
    """Las cuatro formas que de verdad se escriben en el casillero."""

    def _valor(self, texto: str) -> str:
        return apply_postprocess("flight_number", "flight_number", texto)[0]

    def test_vuelo_numerado_se_conserva(self):
        for value, expected in (
            ("802", "802"),
            ("41", "41"),
            ("4605", "4605"),
            ("A + 123", "A123"),
            ("CM188", "CM188"),
            ("CM 472", "CM472"),
        ):
            self.assertEqual(self._valor(value), expected)

    def test_letras_alrededor_de_tres_cifras_son_un_vuelo_CM(self):
        """El prefijo manuscrito es CM aunque el OCR lea otra cosa."""
        for value, expected in (
            ("CMp472", "CM472"),
            ("CN364", "CM364"),
            ("M395", "CM395"),
            ("CMP7S9", "CM759"),
        ):
            self.assertEqual(self._valor(value), expected)

    def test_cifras_leidas_como_letras_se_recuperan(self):
        for value, expected in (
            ("7S8", "758"),
            ("2o0", "200"),
            ("4C2", "402"),
            ("CMIO3", "CM103"),
        ):
            self.assertEqual(self._valor(value), expected)

    def test_codigos_se_ajustan_al_vocabulario(self):
        for value, expected in (
            ("TCK", "TCK"),
            ("Tek", "TCK"),
            ("TLK", "TCK"),
            ("CCk", "CCK"),
            ("SPV", "SPV"),
            ("SUP", "SUP"),
            ("MTC", "MTC"),
            ("SVC VISIT", "SVC"),
            ("9643TCK", "TCK"),
        ):
            self.assertEqual(self._valor(value), expected)

    def test_el_codigo_se_reconoce_por_el_trazo_no_por_la_letra(self):
        """La P vuelve como 9, D o R, y la S como 5: sigue siendo SPV."""
        for value, expected in (
            ("S9V", "SPV"),
            ("SDv", "SPV"),
            ("SRV", "SPV"),
            ("52V", "SPV"),
            ("JCK", "TCK"),
        ):
            self.assertEqual(self._valor(value), expected)

    def test_la_cifra_del_codigo_se_conserva(self):
        self.assertEqual(self._valor("SV3"), "SV3")
        self.assertEqual(self._valor("5U#2"), "SV2")

    def test_el_trazo_parecido_no_alcanza_para_dos_letras(self):
        """``ZCC`` es un 700 escrito a mano, no un CCK."""
        for value in ("ZCC", "CDV", "GK", "CK"):
            self.assertEqual(self._valor(value), "")

    def test_un_CM_limpio_sostiene_un_vuelo_de_dos_cifras(self):
        self.assertEqual(self._valor("CM 40"), "CM40")
        self.assertEqual(self._valor("CM4i"), "CM41")

    def test_descarta_etiqueta_impresa(self):
        self.assertEqual(self._valor("FLT. NO / CHECK CM403"), "CM403")

    def test_lo_que_no_es_ninguna_de_las_formas_queda_vacio(self):
        """Antes se escribían tal cual en el CSV: BSO, SYZ, MF7, E…"""
        for value in ("BSO", "SYZ", "MF7", "E", "CMOS", "CMZD",
                      "FLEET INTERCHANGE"):
            self.assertEqual(self._valor(value), "")

    def test_un_CM_leido_entero_manda_sobre_lo_que_lleve_detras(self):
        """Con el prefijo delante, lo de atrás son las cifras del vuelo."""
        for value, expected in (
            ("CMPlOS", "CM105"),
            ("cmloy", "CM104"),
            ("CMYTO", "CM470"),
            ("CMPBA3", "CM843"),
        ):
            self.assertEqual(self._valor(value), expected)

    def test_un_cero_suelto_no_es_un_vuelo(self):
        """Ese trazo es una marca del casillero, no un número."""
        self.assertEqual(self._valor("0"), "")
        self.assertEqual(self._valor("00"), "")

    def test_texto_largo_no_llega_al_csv(self):
        value, note = apply_postprocess(
            "flight_number", "flight_number", "FLEET INTERCHANGE"
        )
        self.assertEqual(value, "")
        self.assertIn("invalid flight number", note)


class TestCombineDate(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(combine_date("4", "JUL", "26"),
                         ("2026/07/04", ""))
        self.assertEqual(combine_date("16", "7", "2026"),
                         ("2026/07/16", ""))

    def test_three_digit_year_rejected(self):
        value, note = combine_date("16", "JUL", "216")
        self.assertIn("invalid year", note)

    def test_implausible_year_rejected(self):
        value, note = combine_date("16", "JUL", "1751")
        self.assertIn("invalid year", note)
        value, note = combine_date("16", "JUL", "2216")
        self.assertIn("invalid year", note)

    def test_bad_month_rejected(self):
        value, note = combine_date("16", "JJ", "26")
        self.assertIn("invalid month", note)

    def test_bad_day_rejected(self):
        value, note = combine_date("35", "JUL", "26")
        self.assertIn("invalid day", note)
        value, note = combine_date("210", "JUL", "26")
        self.assertIn("invalid day", note)

    def test_incomplete(self):
        value, note = combine_date("16", None, "26")
        self.assertIn("incomplete date", note)


if __name__ == "__main__":
    unittest.main()
