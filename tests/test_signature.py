"""Pruebas de la detección de firmas sobre recortes sintéticos.

Los casos reproducen lo que trae de verdad el recorte de un campo de firma
en estas bitácoras y que antes provocaba fallos: el rótulo impreso dentro
del campo, la fotocopia gris sin escritura, la escritura clara sobre esa
misma fotocopia, la línea preimpresa, la calca de la página vecina y el
sello que invade una casilla ancha desde la fila de al lado.
"""

from __future__ import annotations

import unittest

import cv2
import numpy as np

from app.templates.schema import FieldTemplate
from app.vision.signature import UNCLEAR, _classify, detect_signature

ANCHO, ALTO = 360, 90


def _campo(**kw) -> FieldTemplate:
    valores = dict(id="firma", type="signature", required=True,
                   x=0, y=0, w=1, h=1)
    valores.update(kw)
    return FieldTemplate(**valores)


def _papel(gris: int = 255) -> np.ndarray:
    """Hoja limpia; ``gris`` simula el fondo de una fotocopia."""
    return np.full((ALTO, ANCHO, 3), gris, dtype=np.uint8)


def _linea_impresa(img: np.ndarray, gris: int = 90) -> np.ndarray:
    """Línea preimpresa sobre la que se firma."""
    cv2.line(img, (5, ALTO - 25), (ANCHO - 5, ALTO - 25), (gris,) * 3, 2)
    return img


def _rotulo(img: np.ndarray) -> np.ndarray:
    """Rótulo impreso del formulario, gris claro y de letra pequeña."""
    cv2.putText(img, "(XII) CAPTAIN SIGNATURE", (70, ALTO - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.32, (150,) * 3, 1)
    return img


def _rubrica(img: np.ndarray, tinta: int = 25) -> np.ndarray:
    """Rúbrica manuscrita: trazos largos y solapados."""
    for dx in (0, 55, 110):
        cv2.ellipse(img, (70 + dx, ALTO // 2), (42, 20), 20, 0, 300,
                    (tinta,) * 3, 3)
    cv2.line(img, (30, ALTO - 20), (250, 25), (tinta,) * 3, 3)
    return img


def _numero_licencia(img: np.ndarray) -> np.ndarray:
    """Número de licencia: dígitos sueltos repartidos a lo ancho."""
    cv2.putText(img, "8-805-1A7", (20, ALTO - 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1.4, (25, 25, 25), 3)
    return img


class TestFirmaPresente(unittest.TestCase):
    def test_rubrica_sobre_papel_limpio(self):
        campo = _campo()
        resultado = detect_signature(_rubrica(_papel()), campo, 1)
        self.assertEqual(resultado.value, "true")
        self.assertGreaterEqual(resultado.confidence, campo.sig_present_conf)
        self.assertEqual(resultado.status.value, "OK")

    def test_rubrica_cruzando_la_linea_preimpresa(self):
        img = _rubrica(_linea_impresa(_papel()))
        self.assertEqual(detect_signature(img, _campo(), 1).value, "true")

    def test_escritura_clara_sobre_fotocopia_gris(self):
        """El fondo gris no debe tapar la escritura: se mide contra el papel
        local, no contra un umbral global."""
        img = _rubrica(_linea_impresa(_papel(gris=200)), tinta=60)
        self.assertEqual(detect_signature(img, _campo(), 1).value, "true")

    def test_numero_de_licencia_disperso(self):
        """Los dígitos sueltos no concentran tinta como una rúbrica, pero se
        reparten a lo ancho del campo."""
        img = _numero_licencia(_linea_impresa(_papel()))
        self.assertEqual(detect_signature(img, _campo(), 1).value, "true")

    def test_rubrica_de_boligrafo_azul_claro(self):
        """El gris aplana un azul claro hasta confundirlo con el papel; la
        tinta se mide sobre el canal donde es más oscura."""
        img = _linea_impresa(_papel())
        for dx in (0, 55, 110):
            cv2.ellipse(img, (70 + dx, ALTO // 2), (42, 20), 20, 0, 300,
                        (205, 90, 40), 3)
        self.assertEqual(detect_signature(img, _campo(), 1).value, "true")

    def test_confianza_crece_con_la_evidencia(self):
        floja = detect_signature(_numero_licencia(_papel()), _campo(), 1)
        fuerte = detect_signature(_rubrica(_papel()), _campo(), 1)
        self.assertEqual(floja.value, "true")
        self.assertEqual(fuerte.value, "true")
        self.assertLess(floja.confidence, fuerte.confidence)

    def test_misma_decision_al_doble_de_resolucion(self):
        """Los umbrales son densidades, no cuentas de píxeles: doblar el DPI
        no cambia el veredicto."""
        img = _rubrica(_linea_impresa(_papel()))
        grande = cv2.resize(img, None, fx=2.0, fy=2.0,
                            interpolation=cv2.INTER_CUBIC)
        self.assertEqual(detect_signature(img, _campo(), 1, dpi=200).value,
                         detect_signature(grande, _campo(), 1, dpi=400).value)


class TestFirmaAusente(unittest.TestCase):
    def test_papel_limpio(self):
        resultado = detect_signature(_papel(), _campo(), 1)
        self.assertEqual(resultado.value, "false")
        self.assertGreaterEqual(resultado.confidence, _campo().sig_absent_conf)

    def test_solo_linea_preimpresa(self):
        resultado = detect_signature(_linea_impresa(_papel()), _campo(), 1)
        self.assertEqual(resultado.value, "false")
        self.assertGreaterEqual(resultado.confidence, _campo().sig_absent_conf)

    def test_rotulo_impreso_no_es_firma(self):
        """El rótulo del formulario cae dentro del recorte y antes se
        contaba como firma."""
        img = _rotulo(_linea_impresa(_papel()))
        self.assertEqual(detect_signature(img, _campo(), 1).value, "false")

    def test_fotocopia_gris_sin_escritura(self):
        img = _rotulo(_linea_impresa(_papel(gris=195)))
        self.assertEqual(detect_signature(img, _campo(), 1).value, "false")

    def test_speckles_de_escaneo(self):
        img = _papel()
        rng = np.random.default_rng(42)
        for yi, xi in zip(rng.integers(2, ALTO - 2, size=60),
                          rng.integers(2, ANCHO - 2, size=60)):
            cv2.circle(img, (int(xi), int(yi)), 1, (60, 60, 60), -1)
        self.assertNotEqual(detect_signature(img, _campo(), 1).value, "true")

    def test_ausencia_es_error_solo_si_el_campo_es_obligatorio(self):
        obligatorio = detect_signature(_papel(), _campo(required=True), 1)
        opcional = detect_signature(_papel(), _campo(required=False), 1)
        self.assertEqual(obligatorio.status.value, "ERROR")
        self.assertEqual(opcional.status.value, "WARNING")


class TestFirmaIncierta(unittest.TestCase):
    """Evidencia que no alcanza para afirmar ni para negar.

    Nunca debe acusarse una falta con estas entradas: quedan en WARNING
    para revisión manual.
    """

    def test_calca_de_la_pagina_vecina(self):
        """Arcos finos y largos que atraviesan el campo: hay tinta, pero no
        tiene forma de escritura."""
        img = _papel(gris=205)
        for cx in (60, 180, 300):
            cv2.ellipse(img, (cx, ALTO), (55, 40), 0, 180, 360, (95,) * 3, 2)
        resultado = detect_signature(img, _campo(), 1)
        self.assertEqual(resultado.value, UNCLEAR)
        self.assertEqual(resultado.status.value, "WARNING")

    def test_escritura_que_se_sale_del_campo(self):
        """Solo entra la base de unos dígitos escritos demasiado arriba."""
        img = _linea_impresa(_papel())
        cv2.putText(img, "0400", (90, 14), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (30, 30, 30), 3)
        self.assertEqual(detect_signature(img, _campo(), 1).value, UNCLEAR)

    def test_sello_impreso_en_el_campo(self):
        img = _linea_impresa(_papel())
        cv2.rectangle(img, (15, 12), (95, 45), (60, 60, 60), 2)
        cv2.putText(img, "STA", (20, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (90, 90, 90), 1)
        self.assertEqual(detect_signature(img, _campo(), 1).value, UNCLEAR)

    def test_recorte_quemado_es_indeterminable(self):
        """Sin papel blanco no hay contra qué medir la tinta."""
        rng = np.random.default_rng(7)
        img = _papel()
        img[:, :, :] = rng.integers(18, 23, size=(ALTO, ANCHO),
                                    dtype=np.uint8)[:, :, None]
        resultado = detect_signature(img, _campo(max_ink_ratio=0.60), 1)
        self.assertEqual(resultado.value, UNCLEAR)
        self.assertLess(resultado.confidence, _campo().sig_present_conf)


class TestCoberturaMinima(unittest.TestCase):
    """La puerta que separa una corrección escrita de un sello invasor.

    El bloque «CORRECTION OR DEFERRAL» es una casilla mucho más ancha que
    alta, y ahí la extensión deja de discriminar: lo que la invade desde la
    fila de arriba son los sellos «MXI Entry Performed By» y «DATE / STA»,
    que con su recuadro, su número y su raya cruzan más de medio ancho sin
    ser escritura.

    Los números son los que midió el detector sobre las 122 páginas de un
    libro real. Las cuatro que daba por escritas sin serlo eran las cuatro
    un sello; van aquí con su número de página para poder volver a mirarlas.
    """

    # (página, densidad, extensión, cobertura)
    SELLOS = (
        (2, 0.1231, 0.700, 0.0356),
        (8, 0.2373, 0.563, 0.0394),
        (115, 0.1391, 0.568, 0.0253),
        (122, 0.1445, 0.657, 0.0360),
    )
    CORRECCIONES = (
        (42, 0.1042, 0.726, 0.0565),
        (60, 0.1330, 0.792, 0.0629),
        (56, 0.1401, 0.607, 0.0656),
        (19, 0.1096, 0.714, 0.0662),
        (11, 0.1367, 0.687, 0.0670),
    )

    def _bloque(self, **kw) -> FieldTemplate:
        """El campo tal como lo define la plantilla real."""
        valores = dict(min_ink_peak=0.28, max_empty_peak=0.10,
                       min_ink_span=0.55, min_ink_coverage=0.05)
        valores.update(kw)
        return _campo(required=False, **valores)

    def _metricas(self, pico, ext, cob) -> dict:
        # ``weak_peak`` y ``dark_ratio`` no intervienen en esta decisión: la
        # primera solo puede evitar un «false» y la segunda descarta recortes
        # quemados, que no es el caso de ninguna de estas páginas.
        return {"peak": pico, "span": ext, "coverage": cob,
                "weak_peak": pico, "dark_ratio": 0.05}

    def test_los_sellos_del_libro_quedan_inciertos(self):
        campo = self._bloque()
        for pagina, pico, ext, cob in self.SELLOS:
            with self.subTest(pagina=pagina):
                valor, _conf, _motivo = _classify(
                    self._metricas(pico, ext, cob), campo
                )
                self.assertEqual(valor, UNCLEAR)

    def test_las_correcciones_del_libro_siguen_detectandose(self):
        campo = self._bloque()
        for pagina, pico, ext, cob in self.CORRECCIONES:
            with self.subTest(pagina=pagina):
                valor, _conf, _motivo = _classify(
                    self._metricas(pico, ext, cob), campo
                )
                self.assertEqual(valor, "true")

    def test_sin_la_puerta_los_cuatro_sellos_pasaban(self):
        """El contraste: si no, la prueba de arriba no diría de qué depende."""
        campo = self._bloque(min_ink_coverage=0.0)
        for pagina, pico, ext, cob in self.SELLOS:
            with self.subTest(pagina=pagina):
                valor, _conf, _motivo = _classify(
                    self._metricas(pico, ext, cob), campo
                )
                self.assertEqual(valor, "true")

    def test_por_defecto_la_puerta_no_pide_nada(self):
        """Los demás campos de firma no cambian: siguen con su regla."""
        self.assertEqual(_campo().min_ink_coverage, 0.0)
        valor, _conf, _motivo = _classify(
            self._metricas(0.15, 0.60, 0.0001), _campo()
        )
        self.assertEqual(valor, "true")

    def test_un_trazo_denso_no_pasa_por_la_puerta(self):
        """La cobertura solo condiciona la vía de la tinta repartida.

        Una rúbrica concentra tinta en un punto y puede dejar limpio el resto
        del recorte; eso ya lo resuelve ``min_ink_peak`` y no tiene que pedir
        permiso a la cobertura.
        """
        campo = self._bloque()
        valor, _conf, _motivo = _classify(
            self._metricas(0.40, 0.20, 0.0100), campo
        )
        self.assertEqual(valor, "true")


if __name__ == "__main__":
    unittest.main()


class TestTintaRepartida(unittest.TestCase):
    """Una rúbrica grande de trazo fino no es un campo vacío.

    Los números son los medidos en la bitácora fix.pdf: la firma del capitán
    de la página 1 cruza la casilla con trazos largos y finos y deja densidad
    0,044, por debajo del umbral de vacío, así que el sistema reclamaba una
    firma que estaba ahí. De los 144 recortes etiquetados como ausentes, uno
    solo llega a la vez a esa densidad y a esa extensión.
    """

    # Los de la plantilla para un campo de firma.
    CAMPO = dict(ink_delta=50.0, min_ink_peak=0.28, max_empty_peak=0.07,
                 min_ink_span=0.30)

    def _veredicto(self, **metricas):
        base = dict(peak=0.0, weak_peak=0.0, coverage=0.0, span=0.0,
                    dark_ratio=0.0)
        base.update(metricas)
        return _classify(base, _campo(**self.CAMPO))[0]

    def test_una_rubrica_de_trazo_fino_no_se_declara_ausente(self):
        self.assertEqual(
            self._veredicto(peak=0.044, weak_peak=0.053, coverage=0.0202,
                            span=0.363), UNCLEAR)

    def test_la_casilla_de_verdad_vacia_sigue_siendo_ausente(self):
        # Lo que mide una casilla limpia de la misma bitácora.
        self.assertEqual(
            self._veredicto(peak=0.003, weak_peak=0.003, coverage=0.0010,
                            span=0.016), "false")

    def test_la_tinta_agrupada_en_una_esquina_sigue_siendo_ausente(self):
        """La guarda pide tinta repartida: un borrón no basta."""
        self.assertEqual(
            self._veredicto(peak=0.059, weak_peak=0.059, coverage=0.0116,
                            span=0.074), "false")

    def test_una_hoja_limpia_sigue_siendo_ausente(self):
        self.assertEqual(detect_signature(_papel(), _campo(), 1).value, "false")


class TestBloqueDeCorreccion(unittest.TestCase):
    """La casilla que se decide por reparto, no por densidad.

    El bloque de corrección son tres renglones impresos de lado a lado, y su
    densidad de tinta es prácticamente la misma escrito que vacío: medido
    sobre 120 recortes etiquetados a mano de ocho bitácoras, la mediana es
    0,089 con escritura y 0,058 sin ella, con los dos rangos solapados. Lo
    que sí los separa es que la escritura cruza la casilla y la ensucia.

    Los números de cada prueba son los medidos en la página que se cita.
    """

    CAMPO = dict(min_ink_peak=0.13, max_empty_peak=0.02,
                 min_ink_span=0.5, min_ink_coverage=0.024)

    def _veredicto(self, **metricas):
        base = dict(peak=0.0, weak_peak=0.0, coverage=0.0, span=0.0,
                    dark_ratio=0.0)
        base.update(metricas)
        return _classify(base, _campo(**self.CAMPO))[0]

    def test_tres_renglones_escritos_son_escritura(self):
        # fix.pdf p5: "PERFORMED UNIVERSAL PRECAUTION KIT RESTORATION...".
        self.assertEqual(
            self._veredicto(peak=0.196, weak_peak=0.207, coverage=0.1165,
                            span=0.916), "true")

    def test_dos_renglones_escritos_son_escritura(self):
        # fix.pdf p12: "PERFORMED TIRE PRESSURE CHECK IAW T/C...".
        self.assertEqual(
            self._veredicto(peak=0.088, weak_peak=0.092, coverage=0.0440,
                            span=0.691), "true")

    def test_la_casilla_vacia_es_ausencia(self):
        # fix.pdf p7: solo el gris del escaneo sobre el papel rayado.
        self.assertEqual(
            self._veredicto(peak=0.024, weak_peak=0.022, coverage=0.0076,
                            span=0.363), "false")

    def test_la_densidad_alta_de_un_sello_no_es_escritura(self):
        """El sello concentra tinta pero no cruza la casilla.

        Image_003.pdf p156: el sello "MXI Entry Performed By" cae dentro del
        bloque con densidad 0,119, más alta que la de dos renglones escritos
        de verdad. Sin el reparto no es escritura.
        """
        self.assertEqual(
            self._veredicto(peak=0.119, weak_peak=0.130, coverage=0.0212,
                            span=0.211), "false")

    def test_una_raya_suelta_que_cruza_queda_en_duda(self):
        """Image_005.pdf p213: un trazo diagonal cruza la casilla vacía.

        Cruza lo suficiente para no poder afirmar que está vacía, pero no
        ensucia lo bastante para afirmar que hay escritura: incierta, que es
        lo que la manda a revisar.
        """
        self.assertEqual(
            self._veredicto(peak=0.070, weak_peak=0.050, coverage=0.0204,
                            span=0.546), "unclear")
