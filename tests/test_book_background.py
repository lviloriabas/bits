"""Pruebas del fondo del libro y de la revisión de firmas inciertas.

El fondo del libro es la mediana de un campo a lo largo de las páginas de la
bitácora: el formulario vacío. Lo que se comprueba aquí es que esa mediana
ignora la escritura de cada página, que la franja de duda se aprende de las
páginas ya resueltas, y (lo más importante) que la segunda opinión **solo se
pronuncia cuando tiene evidencia**: una firma incierta que se convierte en un
veredicto equivocado es el único cambio que empeoraría el sistema.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from app.core import pipeline as pipeline_module
from app.core.config import AppConfig
from app.core.pipeline import Pipeline
from app.models.schemas import FieldResult, PageResult, Status
from app.templates.schema import FieldTemplate, FieldType, Template
from app.vision.book_background import (
    MIN_BAND_WIDTH,
    MIN_BACKGROUND_PAGES,
    build_background,
    confident_band,
    drop_crossing_strokes,
    ink_mask,
    peak_density,
)
from app.vision.signature import UNCLEAR, background_peak, review_with_background

ALTO, ANCHO = 60, 300


class FakeEngine:
    name = "fake"


def _formulario(gris: int = 245) -> np.ndarray:
    """Casilla impresa vacía: recuadro, línea de firma y rótulo."""
    imagen = np.full((ALTO, ANCHO, 3), gris, dtype=np.uint8)
    cv2.rectangle(imagen, (2, 2), (ANCHO - 3, ALTO - 3), (120,) * 3, 1)
    cv2.line(imagen, (10, ALTO - 14), (ANCHO - 10, ALTO - 14), (120,) * 3, 1)
    cv2.putText(imagen, "SIGNATURE", (95, ALTO - 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.3, (150,) * 3, 1)
    return imagen


def _firmada(desplazamiento: int = 0, tinta: int = 20) -> np.ndarray:
    """Página firmada: rúbrica en una posición propia de cada página.

    El desplazamiento reparte la rúbrica a lo ancho, como pasa de verdad: dos
    personas no firman en el mismo sitio, y es eso lo que hace que la mediana
    del libro se quede con el formulario y no con la escritura.
    """
    imagen = _formulario()
    inicio = 40 + (desplazamiento * 37) % 160
    for dx in (0, 30, 60):
        cv2.ellipse(imagen, (inicio + dx, ALTO // 2), (24, 12),
                    20, 0, 300, (tinta,) * 3, 3)
    return imagen


class TestFondoDelLibro(unittest.TestCase):
    def test_la_mediana_es_el_formulario_vacio(self):
        """Cada página firma en un sitio distinto; la mediana no las conserva."""
        paginas = [_firmada(desplazamiento=indice * 7) for indice in range(12)]
        fondo = build_background(paginas)
        self.assertIsNotNone(fondo)
        # El fondo tiene que parecerse al formulario vacío, no a las páginas.
        vacio = np.min(_formulario(), axis=2).astype(float)
        self.assertLess(np.abs(fondo.astype(float) - vacio).mean(), 6.0)

    def test_conserva_lo_impreso(self):
        """La línea y el recuadro salen en todas las páginas: son del fondo."""
        fondo = build_background([_formulario() for _ in range(10)])
        self.assertLess(float(fondo[ALTO - 14, ANCHO // 2]), 160)

    def test_sin_paginas_suficientes_no_hay_fondo(self):
        pocas = [_formulario() for _ in range(MIN_BACKGROUND_PAGES - 1)]
        self.assertIsNone(build_background(pocas))

    def test_tolera_recortes_de_distinto_tamano(self):
        """Las páginas de un PDF no siempre miden lo mismo al píxel."""
        paginas = [_firmada(indice) for indice in range(10)]
        paginas[3] = cv2.resize(paginas[3], (ANCHO - 2, ALTO - 1))
        paginas[7] = cv2.resize(paginas[7], (ANCHO + 3, ALTO + 1))
        fondo = build_background(paginas)
        self.assertIsNotNone(fondo)
        self.assertEqual(fondo.shape, (ALTO, ANCHO))


class TestResiduo(unittest.TestCase):
    def setUp(self):
        self.fondo = build_background(
            [_firmada(desplazamiento=indice * 7) for indice in range(12)]
        )

    def test_la_pagina_vacia_no_deja_residuo(self):
        self.assertLess(background_peak(_formulario(), self.fondo), 0.02)

    def test_la_pagina_firmada_si(self):
        self.assertGreater(background_peak(_firmada(200), self.fondo), 0.10)

    def test_el_rotulo_impreso_no_cuenta_como_tinta(self):
        """Aunque esté dentro de la casilla: está en el fondo, se resta."""
        mascara = ink_mask(_formulario(), self.fondo)
        self.assertLess(float(mascara.mean()), 0.01)

    def test_la_fotocopia_mas_oscura_no_inventa_tinta(self):
        oscura = _formulario(gris=205)
        self.assertLess(background_peak(oscura, self.fondo), 0.05)

    def test_densidad_de_mascara_vacia(self):
        self.assertEqual(peak_density(np.zeros((10, 10), np.uint8)), 0.0)


class TestAtribucionDeTrazos(unittest.TestCase):
    """Tinta que está en la página pero no es de este campo."""

    def _mascara(self, dibujar) -> np.ndarray:
        lienzo = np.zeros((ALTO, ANCHO), np.uint8)
        dibujar(lienzo)
        return lienzo

    def test_descarta_la_raya_que_cruza_la_casilla(self):
        """La X de una página anulada: entra por un lado y sale por el otro."""
        mascara = self._mascara(
            lambda img: cv2.line(img, (0, 5), (ANCHO - 1, ALTO - 5), 1, 2)
        )
        self.assertGreater(mascara.sum(), 0)
        self.assertEqual(int(drop_crossing_strokes(mascara).sum()), 0)

    def test_descarta_el_arco_largo(self):
        """No hace falta que sea recta: basta con que deje el recuadro vacío."""
        mascara = self._mascara(
            lambda img: cv2.ellipse(img, (ANCHO // 2, ALTO), (ANCHO // 2, 40),
                                    0, 180, 360, 1, 2)
        )
        self.assertEqual(int(drop_crossing_strokes(mascara).sum()), 0)

    def test_conserva_una_firma_ancha(self):
        """Una rúbrica ocupa todo el ancho, pero llena su recuadro."""
        def firma(img):
            for dx in (0, 60, 120, 180):
                cv2.ellipse(img, (50 + dx, ALTO // 2), (35, 18), 20, 0, 300,
                            1, 4)
            cv2.line(img, (10, ALTO - 12), (ANCHO - 10, 14), 1, 3)
        mascara = self._mascara(firma)
        conservado = drop_crossing_strokes(mascara)
        self.assertGreater(int(conservado.sum()), int(mascara.sum()) * 0.8)

    def test_conserva_los_trazos_sueltos(self):
        """Filtrar fragmentos se probó y se comía dígitos de licencia."""
        def digitos(img):
            for x in (20, 70, 120, 170, 220):
                cv2.rectangle(img, (x, 20), (x + 14, 40), 1, -1)
        mascara = self._mascara(digitos)
        self.assertEqual(int(drop_crossing_strokes(mascara).sum()),
                         int(mascara.sum()))

    def test_la_pagina_anulada_deja_de_parecer_firmada(self):
        """De punta a punta: el residuo de una casilla vacía tachada."""
        fondo = build_background(
            [_firmada(desplazamiento=indice * 7) for indice in range(12)]
        )
        tachada = _formulario()
        cv2.line(tachada, (0, 4), (ANCHO - 1, ALTO - 4), (30,) * 3, 3)
        cv2.line(tachada, (0, ALTO - 4), (ANCHO - 1, 4), (30,) * 3, 3)
        self.assertLess(background_peak(tachada, fondo), 0.02)


class TestFranjaDeDuda(unittest.TestCase):
    def test_se_aprende_de_las_paginas_resueltas(self):
        picos = [0.01, 0.02, 0.03, 0.30, 0.35, 0.40]
        veredictos = ["false", "false", "false", "true", "true", "true"]
        self.assertEqual(confident_band(picos, veredictos), (0.03, 0.30))

    def test_las_inciertas_no_forman_la_franja(self):
        picos = [0.01, 0.02, 0.03, 0.15, 0.30, 0.35, 0.40]
        veredictos = ["false", "false", "false", UNCLEAR,
                      "true", "true", "true"]
        self.assertEqual(confident_band(picos, veredictos), (0.03, 0.30))

    def test_sin_ejemplos_de_una_clase_no_hay_franja(self):
        """Un libro donde todas las páginas están firmadas no enseña nada
        sobre cuánta tinta tiene una página vacía."""
        picos = [0.30, 0.32, 0.35, 0.40, 0.41]
        self.assertIsNone(confident_band(picos, ["true"] * 5))

    def test_poblaciones_solapadas_no_dan_franja(self):
        """Si lo resuelto se pisa de verdad, el residuo no mide lo que se cree."""
        picos = [0.10, 0.40, 0.05, 0.02, 0.38, 0.08]
        veredictos = ["false", "false", "true", "true", "false", "true"]
        self.assertIsNone(confident_band(picos, veredictos))

    def test_una_firma_flojisima_no_tumba_la_franja(self):
        """Una sola página extrema no puede desperdiciar las otras treinta."""
        picos = [0.00, 0.00, 0.01, 0.02, 0.30, 0.33, 0.36, 0.40]
        veredictos = ["false"] * 4 + ["true"] * 4
        picos.append(0.015)          # una firma casi invisible
        veredictos.append("true")
        banda = confident_band(picos, veredictos)
        self.assertIsNotNone(banda)
        self.assertGreater(banda[1], banda[0])

    def test_la_franja_nunca_queda_pegada(self):
        """Con la franja pegada, cualquier borrón pasaría por firma."""
        # Vacías hasta 0.020 y firmas desde 0.022: separan, pero por nada.
        picos = [0.000, 0.005, 0.010, 0.020, 0.022, 0.30, 0.33, 0.36]
        veredictos = ["false"] * 4 + ["true"] * 4
        banda = confident_band(picos, veredictos)
        self.assertIsNotNone(banda)
        self.assertGreaterEqual(banda[1] - banda[0], MIN_BAND_WIDTH)

    def test_no_descarta_nada_si_no_hace_falta(self):
        """Con las poblaciones separadas y holgadas, la franja es la natural."""
        picos = [0.00, 0.01, 0.02, 0.30, 0.33, 0.36]
        veredictos = ["false"] * 3 + ["true"] * 3
        self.assertEqual(confident_band(picos, veredictos), (0.02, 0.30))


class TestSegundaOpinion(unittest.TestCase):
    BANDA = (0.05, 0.20)

    def test_resuelve_por_encima_de_la_franja(self):
        valor, confianza, _ = review_with_background(0.31, self.BANDA)
        self.assertEqual(valor, "true")
        self.assertGreater(confianza, 0.7)

    def test_resuelve_por_debajo(self):
        valor, _confianza, _ = review_with_background(0.01, self.BANDA)
        self.assertEqual(valor, "false")

    def test_dentro_de_la_franja_se_abstiene(self):
        """Lo que cae en la franja sigue en REVISAR: no se parte por el medio."""
        self.assertIsNone(review_with_background(0.12, self.BANDA))

    def test_los_bordes_cuentan_como_resueltos(self):
        self.assertEqual(review_with_background(0.05, self.BANDA)[0], "false")
        self.assertEqual(review_with_background(0.20, self.BANDA)[0], "true")


def _plantilla() -> Template:
    return Template(name="fixture", fields=[FieldTemplate(
        id="firma", type=FieldType.SIGNATURE, required=True,
        x=0.0, y=0.0, w=1.0, h=1.0,
    )])


def _pagina(numero: int, valor: str, alineacion: str = "ok") -> PageResult:
    pagina = PageResult(page_number=numero)
    pagina.alignment_quality = alineacion
    pagina.fields = [FieldResult(
        page_number=numero, field_id="firma", field_type="signature",
        source="vision", value=valor, confidence=0.4,
        status=Status.WARNING if valor == UNCLEAR else Status.OK,
    )]
    return pagina


class TestRevisionEnElPipeline(unittest.TestCase):
    """La revisión es una segunda opinión: nunca puede tumbar la ejecución."""

    def setUp(self):
        self.pipeline = Pipeline(
            AppConfig(align=False), FakeEngine(), _plantilla(),
        )

    def _correr(self, paginas, imagenes):
        def render(_ruta, numero, _dpi):
            return imagenes[numero]

        with patch.object(pipeline_module, "render_page", side_effect=render):
            return self.pipeline._review_signatures(
                Path("fixture.pdf"), paginas, None, None, None,
                renderer=None, first_page=1,
            )

    def test_resuelve_una_firma_incierta(self):
        paginas, imagenes = [], {}
        for numero in range(1, 15):
            firmada = numero % 2 == 0
            paginas.append(_pagina(numero, "true" if firmada else "false"))
            imagenes[numero] = (_firmada(numero * 3) if firmada
                                else _formulario())
        # Una página firmada que el detector clásico no supo resolver.
        paginas.append(_pagina(15, UNCLEAR))
        imagenes[15] = _firmada(11)

        resultado = self._correr(paginas, imagenes)[-1].fields[0]
        self.assertEqual(resultado.value, "true")
        self.assertEqual(resultado.inference_method, "book_background")
        self.assertEqual(resultado.status, Status.OK)

    def test_no_toca_los_veredictos_firmes(self):
        paginas, imagenes = [], {}
        for numero in range(1, 15):
            paginas.append(_pagina(numero, "false"))
            imagenes[numero] = _formulario()
        paginas.append(_pagina(15, UNCLEAR))
        imagenes[15] = _formulario()
        antes = [p.fields[0].value for p in paginas[:-1]]

        resultado = self._correr(paginas, imagenes)
        self.assertEqual([p.fields[0].value for p in resultado[:-1]], antes)

    def test_sin_paginas_suficientes_no_cambia_nada(self):
        paginas = [_pagina(1, "true"), _pagina(2, UNCLEAR)]
        imagenes = {1: _firmada(), 2: _formulario()}
        resultado = self._correr(paginas, imagenes)
        self.assertEqual(resultado[1].fields[0].value, UNCLEAR)

    def test_las_paginas_mal_alineadas_se_dejan_en_paz(self):
        """Su recorte no cae sobre la misma casilla: no hay nada que comparar."""
        paginas = [_pagina(numero, "true") for numero in range(1, 14)]
        paginas.append(_pagina(14, UNCLEAR, alineacion="low"))
        imagenes = {numero: _firmada(numero) for numero in range(1, 15)}
        resultado = self._correr(paginas, imagenes)
        self.assertEqual(resultado[-1].fields[0].value, UNCLEAR)

    def test_un_fallo_al_renderizar_no_rompe_la_corrida(self):
        paginas = [_pagina(numero, "true") for numero in range(1, 14)]
        paginas.append(_pagina(14, UNCLEAR))
        with patch.object(pipeline_module, "render_page",
                          side_effect=RuntimeError("PDF ilegible")):
            resultado = self.pipeline._review_signatures(
                Path("fixture.pdf"), paginas, None, None, None,
                renderer=None, first_page=1,
            )
        self.assertEqual(resultado[-1].fields[0].value, UNCLEAR)
        self.assertEqual(len(resultado), len(paginas))

    def test_un_campo_puede_quedarse_fuera_de_la_segunda_opinion(self):
        """``book_background=False`` deja el campo como lo dejó el detector.

        La segunda opinión compara densidades de tinta. El bloque de
        corrección no se decide por densidad sino por cuánto se reparte la
        tinta a lo largo de la línea: un sello desbordado desde la fila de
        arriba concentra tanta como una corrección escrita, y darlo por
        escrito abriría una discrepancia que no existe.
        """
        plantilla = Template(name="fixture", fields=[FieldTemplate(
            id="firma", type=FieldType.SIGNATURE, required=True,
            x=0.0, y=0.0, w=1.0, h=1.0, book_background=False,
        )])
        pipeline = Pipeline(AppConfig(align=False), FakeEngine(), plantilla)
        paginas, imagenes = [], {}
        for numero in range(1, 15):
            firmada = numero % 2 == 0
            paginas.append(_pagina(numero, "true" if firmada else "false"))
            imagenes[numero] = (_firmada(numero * 3) if firmada
                                else _formulario())
        paginas.append(_pagina(15, UNCLEAR))
        imagenes[15] = _firmada(11)

        def render(_ruta, numero, _dpi):
            return imagenes[numero]

        with patch.object(pipeline_module, "render_page", side_effect=render):
            resultado = pipeline._review_signatures(
                Path("fixture.pdf"), paginas, None, None, None,
                renderer=None, first_page=1,
            )
        self.assertEqual(resultado[-1].fields[0].value, UNCLEAR)
        self.assertIsNone(resultado[-1].fields[0].inference_method)

    def test_se_puede_apagar(self):
        pipeline = Pipeline(
            AppConfig(align=False, signature_book_background=False),
            FakeEngine(), _plantilla(),
        )
        paginas = [_pagina(numero, "true") for numero in range(1, 14)]
        paginas.append(_pagina(14, UNCLEAR))
        with patch.object(pipeline_module, "render_page") as render:
            resultado = pipeline._review_signatures(
                Path("fixture.pdf"), paginas, None, None, None,
                renderer=None, first_page=1,
            )
        render.assert_not_called()
        self.assertEqual(resultado[-1].fields[0].value, UNCLEAR)

    def test_no_renderiza_nada_tras_una_cancelacion(self):
        """Cancelar tiene que notarse en el acto.

        La revisión renderiza la muestra del libro entera (medido: 93 ms por
        página a 200 DPI, 3 s solo la muestra) y corre después del bucle de
        páginas, así que sin esta guarda el botón Cancelar se quedaba varios
        segundos sin efecto aparente en cada archivo en vuelo.
        """
        paginas = [_pagina(numero, "true" if numero % 2 else "false")
                   for numero in range(1, 14)]
        paginas.append(_pagina(14, UNCLEAR))
        self.pipeline._should_cancel = lambda: True

        with patch.object(pipeline_module, "render_page") as render:
            resultado = self.pipeline._review_signatures(
                Path("fixture.pdf"), paginas, None, None, None,
                renderer=None, first_page=1,
            )

        render.assert_not_called()
        self.assertEqual(resultado[-1].fields[0].value, UNCLEAR)
        self.assertEqual(len(resultado), len(paginas))

    def test_una_cancelacion_a_media_revision_deja_las_paginas_como_estaban(self):
        """A medio muestreo tampoco se sigue: se devuelve lo que ya había."""
        paginas, imagenes = [], {}
        for numero in range(1, 15):
            firmada = numero % 2 == 0
            paginas.append(_pagina(numero, "true" if firmada else "false"))
            imagenes[numero] = (_firmada(numero * 3) if firmada
                                else _formulario())
        paginas.append(_pagina(15, UNCLEAR))
        imagenes[15] = _firmada(11)

        renderizadas: list[int] = []

        def render(_ruta, numero, _dpi):
            renderizadas.append(numero)
            return imagenes[numero]

        # La cancelación llega cuando ya se muestrearon tres páginas.
        self.pipeline._should_cancel = lambda: len(renderizadas) >= 3
        with patch.object(pipeline_module, "render_page", side_effect=render):
            resultado = self.pipeline._review_signatures(
                Path("fixture.pdf"), paginas, None, None, None,
                renderer=None, first_page=1,
            )

        self.assertEqual(len(renderizadas), 3)
        self.assertLess(len(renderizadas), len(imagenes))
        self.assertEqual(resultado[-1].fields[0].value, UNCLEAR)
        self.assertEqual(resultado[-1].fields[0].inference_method, None)

    def test_sin_firmas_inciertas_no_renderiza_nada(self):
        """El coste solo se paga cuando hay algo que resolver."""
        paginas = [_pagina(numero, "true") for numero in range(1, 20)]
        with patch.object(pipeline_module, "render_page") as render:
            self.pipeline._review_signatures(
                Path("fixture.pdf"), paginas, None, None, None,
                renderer=None, first_page=1,
            )
        render.assert_not_called()


def test_licencia_tenue_no_se_declara_ausente_por_restar_el_fondo():
    from app.vision.signature import review_with_background
    assert review_with_background(0.0, (0.01, 0.05), allow_absent=False) is None
    assert review_with_background(0.06, (0.01, 0.05), allow_absent=False)[0] == "true"
    assert review_with_background(0.0, (0.01, 0.05))[0] == "false"


if __name__ == "__main__":
    unittest.main()
