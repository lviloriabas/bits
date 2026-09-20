"""Pruebas de la clasificación de discrepancias (faltas de firma).

Reglas: vuelo requiere piloto + capitán + licencia del capitán;
mantenimiento (technician_license presente) requiere piloto + técnico,
y no mira los campos de capitán.
El bloque de corrección escrito («CORRECTION OR DEFERRAL») exige por sí
solo el juego de firmas de mantenimiento, aunque la licencia de técnico
haya quedado en blanco.
Una licencia de técnico ilegible deja el tipo de página INCIERTO (no se
acusan los campos ambiguos). Las lecturas de baja confianza nunca se
acusan como faltas: categoría UNCERTAIN para auditoría, sin apartarlas.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from app.models.schemas import FieldResult, PageResult, Status, ValidationReport
from app.reports.csv_reporter import CsvReporter
from app.reports.organize import por_revisar
from app.templates.manager import TemplateManager
from app.validation.discrepancias import (
    Categoria,
    TipoEntrada,
    clasificar_lote,
    confirmadas_para_revision,
)
from app.vision.signature import _classify, _result

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = TemplateManager().load(
    ROOT / "template/aircraft_log.json"
)


def test_seleccion_de_discrepancias_filtra_solo_lo_solicitado():
    from app.validation.discrepancias import _clasificar_pagina
    page = _vuelo_ok(captain_signature=("false", AUSENTE), captain_license=("false", AUSENTE))
    activa = TEMPLATE.model_copy(update={"discrepancy_fields": ["captain_license"]})
    resultado = _clasificar_pagina(page, activa)
    assert resultado is not None
    assert [c.field_id for c in resultado[2]] == ["captain_license"]
    assert _clasificar_pagina(page, TEMPLATE.model_copy(update={"discrepancy_fields": []})) is None

# Umbrales por defecto de la plantilla
PRESENTE = 0.9  # >= sig_present_conf (0.45)
AUSENTE = 0.85  # >= sig_absent_conf (0.55)
DUDOSA = 0.2    # por debajo de ambos umbrales


def _sig(fid: str, value: str, conf: float) -> FieldResult:
    return FieldResult(page_number=1, field_id=fid, field_type="signature",
                       value=value, confidence=conf,
                       status=Status.OK if value == "true"
                       else Status.ERROR)


def _page(pn: int, log: str, mat: str, **sigs) -> PageResult:
    page = PageResult(page_number=pn)
    page.add_field(FieldResult(page_number=pn, field_id="log_number",
                               field_type="ocr", value=log,
                               confidence=1.0, status=Status.OK))
    page.add_field(FieldResult(page_number=pn, field_id="matricula",
                               field_type="ocr", value=mat,
                               confidence=1.0, status=Status.OK))
    for fid, (value, conf) in sigs.items():
        page.add_field(_sig(fid, value, conf))
    return page


def _vuelo_ok(pn: int = 1, log: str = "2147337", mat: str = "HP-1534CMP",
              **extra) -> PageResult:
    sigs = {
        "pilot_signature": ("true", PRESENTE),
        "captain_signature": ("true", PRESENTE),
        "captain_license": ("true", PRESENTE),
        "technician_signature": ("false", AUSENTE),
        "technician_license": ("false", AUSENTE),
    }
    sigs.update(extra)
    return _page(pn, log, mat, **sigs)


def _mant_ok(pn: int = 1, log: str = "2147337", mat: str = "HP-1534CMP",
             **extra) -> PageResult:
    sigs = {
        "pilot_signature": ("true", PRESENTE),
        "captain_signature": ("false", AUSENTE),
        "captain_license": ("false", AUSENTE),
        "technician_signature": ("true", PRESENTE),
        "technician_license": ("true", PRESENTE),
    }
    sigs.update(extra)
    return _page(pn, log, mat, **sigs)


class TestRepartoDeCorreccion(unittest.TestCase):
    """El veredicto visual llega al reparto sin ocultar faltas reales."""

    def _clasificar(self, page, *, span=0.304, coverage=0.0258):
        campo = TEMPLATE.field("correction_block")
        valor, confianza, comentario = _classify(dict(
            peak=0.1382, weak_peak=0.15, coverage=coverage,
            span=span, dark_ratio=0.05,
        ), campo)
        page.add_field(_result(campo, page.page_number, valor, confianza, comentario))
        reporte = ValidationReport(
            pdf_path="fixture.pdf", template_name=TEMPLATE.name, pages=[page],
        )
        clasificar_lote([reporte], TEMPLATE)
        return page

    def test_sello_no_exige_licencia_de_tecnico_en_vuelo_completo(self):
        page = self._clasificar(_vuelo_ok())
        self.assertFalse(page.discrepancy)
        self.assertFalse(por_revisar(page))

    def test_correccion_repartida_sin_licencia_sigue_en_revision(self):
        page = self._clasificar(_vuelo_ok(), span=0.8, coverage=0.06)
        self.assertTrue(page.discrepancy)
        self.assertIn("technician_license", page.discrepancy_fields)
        self.assertTrue(por_revisar(page))

    def test_sello_no_oculta_firma_de_piloto_ausente(self):
        page = self._clasificar(_vuelo_ok(pilot_signature=("false", AUSENTE)))
        self.assertTrue(page.discrepancy)
        self.assertIn("pilot_signature", page.discrepancy_fields)
        self.assertTrue(por_revisar(page))


def _corregida(pn: int = 1, log: str = "2147337", mat: str = "HP-1534CMP",
               **extra) -> PageResult:
    """Página con el bloque de corrección escrito y nada más firmado.

    Es la hoja del caso: el técnico describió el trabajo y no lo cerró
    nadie. Sin el bloque de corrección esta misma página sería una VOID.
    """
    sigs = {
        "correction_block": ("true", PRESENTE),
        "pilot_signature": ("false", AUSENTE),
        "captain_signature": ("false", AUSENTE),
        "captain_license": ("false", AUSENTE),
        "technician_signature": ("false", AUSENTE),
        "technician_license": ("false", AUSENTE),
    }
    sigs.update(extra)
    return _page(pn, log, mat, **sigs)


def _reporte(*pages: PageResult) -> ValidationReport:
    return ValidationReport(pdf_path="fixture.pdf",
                            template_name=TEMPLATE.name, pages=list(pages))


class TestClasificacion(unittest.TestCase):
    def test_vuelo_completo_sin_discrepancia(self):
        self.assertEqual(clasificar_lote([_reporte(_vuelo_ok())], TEMPLATE),
                         [])

    def test_mantenimiento_completo_sin_discrepancia(self):
        self.assertEqual(clasificar_lote([_reporte(_mant_ok())], TEMPLATE),
                         [])

    def test_vuelo_sin_firma_de_capitan(self):
        pagina = _vuelo_ok(captain_signature=("false", AUSENTE))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        entrada = entradas[0]
        self.assertIs(entrada.tipo, TipoEntrada.VUELO)
        self.assertIs(entrada.categoria, Categoria.MISSING)
        self.assertIn("Falta firma de capitán", entrada.razones())

    def test_vuelo_sin_licencia_de_capitan(self):
        pagina = _vuelo_ok(captain_license=("false", AUSENTE))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertIn("Falta licencia del capitán", entradas[0].razones())

    def test_vuelo_sin_firma_de_piloto(self):
        pagina = _vuelo_ok(pilot_signature=("false", AUSENTE))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertIn("Falta firma de piloto", entradas[0].razones())

    def test_mantenimiento_sin_firma_de_piloto(self):
        pagina = _mant_ok(pilot_signature=("false", AUSENTE))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertIs(entradas[0].tipo, TipoEntrada.MANTENIMIENTO)
        self.assertIn("entrada de mantenimiento", entradas[0].razones()[0])

    def test_mantenimiento_sin_firma_de_tecnico(self):
        pagina = _mant_ok(technician_signature=("false", AUSENTE))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertIs(entradas[0].tipo, TipoEntrada.MANTENIMIENTO)
        self.assertIs(entradas[0].categoria, Categoria.MISSING)
        self.assertIn(
            "Falta firma de técnico (entrada de mantenimiento)",
            entradas[0].razones(),
        )

    def test_mantenimiento_no_requiere_firmas_de_capitan(self):
        pagina = _mant_ok()
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_mantenimiento_con_firma_de_capitan_no_es_discrepancia(self):
        # El formulario F-MNT-001 lleva el bloque de mantenimiento y el de
        # aceptación de la aeronave (que firma el capitán) en la misma hoja.
        # Que estén los dos es lo normal: en la ejecución de referencia le
        # pasa a 114 de las 350 páginas de mantenimiento.
        pagina = _mant_ok(captain_signature=("true", PRESENTE))
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_mantenimiento_con_licencia_de_capitan_no_es_discrepancia(self):
        pagina = _mant_ok(captain_license=("true", PRESENTE))
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_mantenimiento_no_mira_el_capitan_ni_dudoso(self):
        # Ni siquiera una lectura dudosa del capitán se acusa: el campo no
        # entra en los requisitos de una página de mantenimiento.
        pagina = _mant_ok(captain_signature=("false", DUDOSA))
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_un_sello_sobre_la_firma_de_tecnico_no_crea_discrepancia(self):
        # La casilla de firma de técnico recibe los sellos «MXI Entry
        # Performed By» y «DATE / STA», así que darla por escrita no prueba
        # que un técnico firmara. Con la licencia de técnico vacía y el
        # bloque del capitán completo, esto es una entrada de vuelo entera.
        pagina = _page(1, "2147337", "HP-1534CMP",
                       pilot_signature=("true", PRESENTE),
                       captain_signature=("true", PRESENTE),
                       captain_license=("true", PRESENTE),
                       technician_signature=("true", PRESENTE),
                       technician_license=("false", AUSENTE))
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_una_bitacora_void_no_es_discrepancia(self):
        # Se anuló al llenarla y se apartó: lleva el log page y la matrícula
        # y nada más. No le falta ninguna firma porque no llegó a usarse.
        pagina = _page(1, "2147337", "HP-1534CMP",
                       pilot_signature=("false", AUSENTE),
                       captain_signature=("false", AUSENTE),
                       captain_license=("false", AUSENTE),
                       technician_signature=("false", AUSENTE),
                       technician_license=("false", AUSENTE))
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_una_void_sellada_sigue_sin_ser_discrepancia(self):
        # El sello cae sobre la casilla del técnico igual que en cualquier
        # otra hoja. No convierte una bitácora anulada en una discrepancia.
        pagina = _page(1, "2147337", "HP-1534CMP",
                       pilot_signature=("false", AUSENTE),
                       captain_signature=("false", AUSENTE),
                       captain_license=("false", AUSENTE),
                       technician_signature=("true", PRESENTE),
                       technician_license=("false", AUSENTE))
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_vuelo_firmado_sin_capitan_si_es_falta(self):
        # Lo que distingue una VOID de una falta real es la firma de piloto:
        # aquí el vuelo se realizó y el bloque del capitán quedó vacío.
        pagina = _page(1, "2147337", "HP-1534CMP",
                       pilot_signature=("true", PRESENTE),
                       captain_signature=("false", AUSENTE),
                       captain_license=("false", AUSENTE),
                       technician_signature=("false", AUSENTE),
                       technician_license=("false", AUSENTE))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertIs(entradas[0].tipo, TipoEntrada.VUELO)
        self.assertIs(entradas[0].categoria, Categoria.MISSING)
        self.assertIn("Falta firma de capitán", entradas[0].razones())

    def test_licencia_sin_firma_de_tecnico_tambien_es_mantenimiento(self):
        # El caso simétrico: la licencia escrita basta para que la bitácora
        # sea de mantenimiento aunque falte la firma.
        pagina = _page(1, "2147337", "HP-1534CMP",
                       pilot_signature=("true", PRESENTE),
                       captain_signature=("true", PRESENTE),
                       captain_license=("true", PRESENTE),
                       technician_signature=("false", AUSENTE),
                       technician_license=("true", PRESENTE))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertIs(entradas[0].tipo, TipoEntrada.MANTENIMIENTO)
        self.assertIs(entradas[0].categoria, Categoria.MISSING)
        self.assertIn(
            "Falta firma de técnico (entrada de mantenimiento)",
            entradas[0].razones(),
        )

    def test_la_firma_de_tecnico_no_decide_el_tipo_ni_estando_escrita(self):
        # Con la licencia de técnico ilegible el tipo no se puede decidir,
        # por mucha tinta que traiga la casilla de firma: esa es justo la que
        # reciben los sellos. La duda que se reporta es la de la licencia.
        pagina = _page(1, "2147337", "HP-1534CMP",
                       pilot_signature=("true", PRESENTE),
                       captain_signature=("true", PRESENTE),
                       captain_license=("false", AUSENTE),
                       technician_signature=("true", PRESENTE),
                       technician_license=("true", DUDOSA))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertIs(entradas[0].tipo, TipoEntrada.INCIERTO)
        self.assertIs(entradas[0].categoria, Categoria.UNCERTAIN)
        self.assertEqual(
            [c.field_id for c in entradas[0].campos],
            ["technician_license"],
        )

    def test_casillas_limpias_ilegibles_hacen_tipo_incierto(self):
        # Ninguna casilla limpia dice de qué tipo es la bitácora, y las tres
        # que lo impiden se nombran para quien revise.
        pagina = _page(1, "2147337", "HP-1534CMP",
                       pilot_signature=("true", PRESENTE),
                       captain_signature=("true", DUDOSA),
                       captain_license=("true", DUDOSA),
                       technician_signature=("true", DUDOSA),
                       technician_license=("true", DUDOSA))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertIs(entradas[0].tipo, TipoEntrada.INCIERTO)
        self.assertIs(entradas[0].categoria, Categoria.UNCERTAIN)
        self.assertEqual(
            [c.field_id for c in entradas[0].campos],
            ["technician_license", "captain_signature", "captain_license"],
        )

    def test_tipo_incierto_sin_firma_de_piloto_es_falta(self):
        # La firma de piloto se exige en ambas interpretaciones: en una
        # página INCIERTO su ausencia confirmada se reporta como falta.
        pagina = _page(1, "2147337", "HP-1534CMP",
                       pilot_signature=("false", AUSENTE),
                       captain_signature=("true", PRESENTE),
                       captain_license=("false", AUSENTE),
                       technician_signature=("true", DUDOSA),
                       technician_license=("true", DUDOSA))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertIs(entradas[0].tipo, TipoEntrada.INCIERTO)
        self.assertIs(entradas[0].categoria, Categoria.MISSING)
        self.assertTrue(
            any("Falta firma de piloto" in razon
                for razon in entradas[0].razones())
        )

    def test_firma_de_tecnico_dudosa_en_mantenimiento_es_incierta(self):
        pagina = _mant_ok(technician_signature=("false", DUDOSA))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertIs(entradas[0].tipo, TipoEntrada.MANTENIMIENTO)
        self.assertIs(entradas[0].categoria, Categoria.UNCERTAIN)
        self.assertIn(
            "Firma de técnico incierta (entrada de mantenimiento); revisar",
            entradas[0].razones(),
        )

    def test_confianza_baja_no_es_falta_sino_incierta(self):
        pagina = _vuelo_ok(pilot_signature=("false", DUDOSA))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertIs(entradas[0].categoria, Categoria.UNCERTAIN)
        self.assertIn("revisar", entradas[0].razones()[0])

    def test_presencia_de_confianza_baja_tambien_es_incierta(self):
        pagina = _vuelo_ok(pilot_signature=("true", DUDOSA))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertIs(entradas[0].categoria, Categoria.UNCERTAIN)
        self.assertIn("Firma de piloto incierta", entradas[0].razones()[0])

    def test_unclear_no_es_falta_sino_incierta(self):
        pagina = _vuelo_ok(captain_signature=("unclear", DUDOSA))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertIs(entradas[0].categoria, Categoria.UNCERTAIN)

    def test_categoria_missing_domina_a_uncertain(self):
        pagina = _vuelo_ok(pilot_signature=("false", DUDOSA),
                           captain_signature=("false", AUSENTE))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertIs(entradas[0].categoria, Categoria.MISSING)
        self.assertEqual(len(entradas[0].campos), 2)

    def test_solo_ausencias_confirmadas_se_apartan_para_revision(self):
        incierta = _vuelo_ok(pilot_signature=("false", DUDOSA))
        faltante = _vuelo_ok(
            2, "2147338", "HP-1534CMP",
            captain_signature=("false", AUSENTE),
        )
        entradas = clasificar_lote(
            [_reporte(incierta, faltante)], TEMPLATE
        )

        confirmadas = confirmadas_para_revision(entradas)

        self.assertEqual([entrada.page_number for entrada in confirmadas], [2])
        # La incierta sigue en el reporte de discrepancias, pero sin la marca
        # que la mandaria al batch manual y le pondria AUDIT IN PROGRESS.
        # Sus seis index fields estan resueltos y ninguna firma es uno de
        # ellos: apartarla obligaria a teclearlos a mano sin necesidad.
        self.assertEqual([entrada.page_number for entrada in entradas], [1, 2])
        self.assertFalse(incierta.discrepancy)
        self.assertTrue(faltante.discrepancy)

    def test_una_lectura_incierta_no_manda_la_pagina_al_batch_manual(self):
        # El batch REVISAR se sube sin indexar: cada pagina que cae ahi es
        # alguien tecleando a mano los seis index fields. Ninguna firma es
        # uno de ellos, asi que una firma ilegible no justifica ese trabajo
        # en una pagina cuya matricula, log page y fecha estan resueltas.
        incierta = _vuelo_ok(pilot_signature=("false", DUDOSA))
        faltante = _vuelo_ok(
            2, "2147338", "HP-1534CMP",
            captain_signature=("false", AUSENTE),
        )
        clasificar_lote([_reporte(incierta, faltante)], TEMPLATE)

        self.assertFalse(por_revisar(incierta))
        self.assertTrue(por_revisar(faltante))

    def test_pagina_en_blanco_se_ignora(self):
        pagina = _vuelo_ok()
        pagina.blank = True
        pagina.fields = []
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])


class TestOrdenYMarcado(unittest.TestCase):
    def test_orden_global_por_logpage(self):
        pages = [
            _vuelo_ok(1, "2147337", "HP-1538CMP",
                      captain_signature=("false", AUSENTE)),
            _vuelo_ok(2, "2147338", "HP-1534CMP",
                      captain_signature=("false", AUSENTE)),
            _vuelo_ok(3, "2271650", "HP-1538CMP",
                      captain_signature=("false", AUSENTE)),
            _vuelo_ok(4, "2147340", "HP-1534CMP",
                      captain_signature=("false", AUSENTE)),
        ]
        entradas = clasificar_lote([_reporte(*pages)], TEMPLATE)
        matriculas = [e.matricula for e in entradas]
        logs = [e.log_number for e in entradas]
        self.assertEqual(matriculas,
                         ["HP-1538CMP", "HP-1534CMP", "HP-1534CMP",
                          "HP-1538CMP"])
        self.assertEqual(logs, [2147337, 2147338, 2147340, 2271650])
        self.assertEqual(
            [(e.matricula, e.log_number) for e in entradas],
            [
                ("HP-1538CMP", 2147337),
                ("HP-1534CMP", 2147338),
                ("HP-1534CMP", 2147340),
                ("HP-1538CMP", 2271650),
            ],
        )

    def test_marca_discrepancy_en_pagina(self):
        pagina = _vuelo_ok(captain_signature=("false", AUSENTE))
        clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertTrue(pagina.discrepancy)
        pagina_ok = _vuelo_ok()
        clasificar_lote([_reporte(pagina_ok)], TEMPLATE)
        self.assertFalse(pagina_ok.discrepancy)

    def test_sin_matricula_no_crea_una_subdivision(self):
        pagina = _page(1, "2147337", None,
                       pilot_signature=("true", PRESENTE),
                       captain_signature=("true", PRESENTE),
                       captain_license=("false", AUSENTE),
                       technician_signature=("false", AUSENTE),
                       technician_license=("false", AUSENTE))
        pagina2 = _vuelo_ok(2, "2147338", "HP-1534CMP",
                            captain_signature=("false", AUSENTE))
        entradas = clasificar_lote([_reporte(pagina2, pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 2)
        self.assertIsNone(entradas[0].matricula)
        self.assertEqual(entradas[1].matricula, "HP-1534CMP")


class TestColumnaDiscEnElCsv(unittest.TestCase):
    """La columna ``disc`` del CSV sale de esta clasificación."""

    def test_la_pagina_clasificada_llega_al_csv_como_true(self):
        limpia = _vuelo_ok(1, "2147337")
        marcada = _vuelo_ok(2, "2147338",
                            captain_signature=("false", AUSENTE))
        reporte = _reporte(limpia, marcada)

        clasificar_lote([reporte], TEMPLATE)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            CsvReporter().write(reporte, path, TEMPLATE)
            with open(path, encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual([row["disc"] for row in rows], ["false", "true"])

    def test_la_columna_discrepancia_dice_que_falta(self):
        limpia = _vuelo_ok(1, "2147337")
        marcada = _vuelo_ok(2, "2147338",
                            captain_signature=("false", AUSENTE),
                            captain_license=("false", AUSENTE))
        corregida = _corregida(3, "2147339")
        reporte = _reporte(limpia, marcada, corregida)

        clasificar_lote([reporte], TEMPLATE)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            CsvReporter().write(reporte, path, TEMPLATE)
            with open(path, encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual([row["disc_reason"] for row in rows], [
            "",
            "Faltan firma de capitán y licencia de capitán",
            "Corrección escrita: faltan firma de piloto, firma de técnico "
            "y licencia de técnico",
        ])

    def test_la_columna_va_pegada_a_disc(self):
        columnas = CsvReporter.columns_for([_reporte(_vuelo_ok())], TEMPLATE)
        self.assertEqual(columnas[columnas.index("disc") + 1], "disc_reason")


class TestBloqueDeCorreccion(unittest.TestCase):
    """El recuadro «CORRECTION OR DEFERRAL» escrito exige las tres firmas."""

    def test_corregida_sin_ninguna_firma_es_discrepancia(self):
        entradas = clasificar_lote([_reporte(_corregida())], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        entrada = entradas[0]
        self.assertIs(entrada.tipo, TipoEntrada.MANTENIMIENTO)
        self.assertIs(entrada.categoria, Categoria.MISSING)
        self.assertTrue(entrada.por_correccion)
        self.assertEqual(entrada.razones(), [
            "Falta firma de piloto (corrección escrita)",
            "Falta firma de técnico (corrección escrita)",
            "Falta licencia de técnico (corrección escrita)",
        ])

    def test_corregida_y_firmada_no_es_discrepancia(self):
        pagina = _corregida(
            pilot_signature=("true", PRESENTE),
            technician_signature=("true", PRESENTE),
            technician_license=("true", PRESENTE),
        )
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_corregida_sin_licencia_de_tecnico_es_discrepancia(self):
        # El caso del recuadro rojo: hay trabajo descrito y firma de técnico,
        # pero la licencia quedó en blanco.
        pagina = _corregida(
            pilot_signature=("true", PRESENTE),
            technician_signature=("true", PRESENTE),
        )
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertEqual(entradas[0].razones(),
                         ["Falta licencia de técnico (corrección escrita)"])

    def test_no_pide_las_firmas_de_capitan(self):
        # El mismo trato que cualquier entrada de mantenimiento: el bloque
        # del capitán vive en la otra mitad del formulario.
        pagina = _corregida(
            pilot_signature=("true", PRESENTE),
            technician_signature=("true", PRESENTE),
            technician_license=("true", PRESENTE),
            captain_signature=("false", AUSENTE),
            captain_license=("false", AUSENTE),
        )
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_una_void_con_el_recuadro_vacio_sigue_sin_ser_discrepancia(self):
        # La hoja anulada solo se distingue de la corregida por este campo.
        pagina = _corregida(correction_block=("false", AUSENTE))
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_un_recuadro_ilegible_no_convierte_la_void_en_falta(self):
        # Un sello desbordado desde la fila de arriba deja el recuadro en
        # «unclear»: no es una corrección y no puede reclamar firmas.
        pagina = _corregida(correction_block=("unclear", DUDOSA))
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])

    def test_la_licencia_de_tecnico_manda_sobre_el_recuadro(self):
        # Con la licencia escrita la página ya era de mantenimiento por su
        # cuenta: la explicación sigue siendo la de siempre.
        pagina = _mant_ok(correction_block=("true", PRESENTE),
                          technician_signature=("false", AUSENTE))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertEqual(len(entradas), 1)
        self.assertFalse(entradas[0].por_correccion)
        self.assertIn("Falta firma de técnico (entrada de mantenimiento)",
                      entradas[0].razones())

    def test_un_vuelo_con_el_recuadro_escrito_y_cerrado_no_es_falta(self):
        # La hoja que lleva las dos cosas: vuelo completo y corrección
        # firmada. El recuadro exige el juego de mantenimiento y está todo.
        pagina = _vuelo_ok(correction_block=("true", PRESENTE),
                           technician_signature=("true", PRESENTE),
                           technician_license=("true", PRESENTE))
        self.assertEqual(clasificar_lote([_reporte(pagina)], TEMPLATE), [])


class TestResumenDeLaDiscrepancia(unittest.TestCase):
    """La frase de una línea que va a la columna ``disc_reason``."""

    def _resumen(self, pagina: PageResult) -> str:
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        return entradas[0].resumen()

    def test_una_sola_falta_va_en_singular(self):
        pagina = _vuelo_ok(captain_signature=("false", AUSENTE))
        self.assertEqual(self._resumen(pagina), "Falta firma de capitán")

    def test_varias_faltas_van_en_plural_y_encadenadas(self):
        pagina = _vuelo_ok(captain_signature=("false", AUSENTE),
                           captain_license=("false", AUSENTE))
        self.assertEqual(self._resumen(pagina),
                         "Faltan firma de capitán y licencia de capitán")

    def test_tres_faltas_llevan_coma_y_una_sola_conjuncion(self):
        self.assertEqual(
            self._resumen(_corregida()),
            "Corrección escrita: faltan firma de piloto, firma de técnico "
            "y licencia de técnico",
        )

    def test_el_recuadro_escrito_se_nombra_en_el_resumen(self):
        pagina = _corregida(pilot_signature=("true", PRESENTE),
                            technician_signature=("true", PRESENTE))
        self.assertEqual(self._resumen(pagina),
                         "Corrección escrita: falta licencia de técnico")

    def test_una_lectura_incierta_no_deja_resumen(self):
        # UNCERTAIN no acusa, así que no hay nada que escribir en el CSV.
        pagina = _vuelo_ok(captain_signature=("unclear", DUDOSA))
        entradas = clasificar_lote([_reporte(pagina)], TEMPLATE)
        self.assertIs(entradas[0].categoria, Categoria.UNCERTAIN)
        self.assertEqual(entradas[0].resumen(), "")

    def test_la_pagina_guarda_su_resumen_solo_si_esta_marcada(self):
        limpia = _vuelo_ok(1, "2147337")
        incierta = _vuelo_ok(2, "2147338",
                             captain_signature=("unclear", DUDOSA))
        marcada = _vuelo_ok(3, "2147339",
                            captain_signature=("false", AUSENTE))
        clasificar_lote([_reporte(limpia, incierta, marcada)], TEMPLATE)
        self.assertEqual(limpia.discrepancy_note, "")
        self.assertEqual(incierta.discrepancy_note, "")
        self.assertEqual(marcada.discrepancy_note, "Falta firma de capitán")


if __name__ == "__main__":
    unittest.main()
