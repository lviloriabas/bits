"""Pruebas de la colocación de campos sobre las rayas impresas."""

from __future__ import annotations

import unittest

import numpy as np
import pytest
from pydantic import ValidationError

from app.templates.schema import (
    AnclaCampo,
    FieldTemplate,
    FieldType,
    PatronReticula,
    Template,
)
from app.vision.reticula import (
    MIN_RAYAS_X,
    MIN_RAYAS_Y,
    Reticula,
    casar_rayas,
    centros_de_rayas,
    leer_reticula,
    plantilla_ajustada,
    rect_de_campo,
)


def _patron(n: int, inicio: float = 0.1, paso: float = 0.02) -> list:
    """Rayas regularmente espaciadas, como los renglones del formulario."""
    return [round(inicio + paso * i, 6) for i in range(n)]


def _plantilla(**campo) -> Template:
    base = dict(
        id="firma", type=FieldType.SIGNATURE, x=0.2, y=0.14, w=0.3, h=0.02,
    )
    base.update(campo)
    return Template(
        name="prueba",
        reticula=PatronReticula(x=_patron(MIN_RAYAS_X + 2),
                                y=_patron(MIN_RAYAS_Y + 2)),
        fields=[FieldTemplate(**base)],
    )


class TestCentrosDeRayas:
    """El centro de la raya es más estable que su primer píxel."""

    def test_devuelve_el_centro_del_tramo(self):
        perfil = np.zeros(100)
        perfil[20:30] = 1.0
        centros = centros_de_rayas(perfil, umbral=0.5)
        assert centros == pytest.approx([0.245], abs=1e-6)

    def test_separa_dos_tramos(self):
        perfil = np.zeros(100)
        perfil[10:14] = 1.0
        perfil[60:64] = 1.0
        centros = centros_de_rayas(perfil, umbral=0.5)
        assert len(centros) == 2
        assert centros[0] < centros[1]

    def test_perfil_plano_no_da_rayas(self):
        assert centros_de_rayas(np.zeros(50), umbral=0.5).size == 0

    def test_perfil_vacio_no_revienta(self):
        assert centros_de_rayas(np.empty(0)).size == 0


class TestCasarRayas:
    """El patrón nombra las rayas; la posición sale de la página."""

    def test_devuelve_la_posicion_medida_no_la_del_patron(self):
        patron = _patron(MIN_RAYAS_Y + 2)
        medidas = np.array(patron) * 1.004 + 0.003
        casadas, _escala = casar_rayas(medidas, patron, MIN_RAYAS_Y)
        assert len(casadas) == len(patron)
        for indice, posicion in casadas.items():
            assert posicion == pytest.approx(medidas[indice], abs=1e-9)

    def test_una_raya_que_falta_no_renombra_a_las_demas(self):
        """El fallo que motivó la alineación por orden.

        Con el vecino más cercano, no detectar una raya corría el nombre de
        todas las de abajo y el campo anclado se iba un renglón entero.
        """
        patron = _patron(MIN_RAYAS_Y + 2)
        sin_una = np.delete(np.array(patron), 5)
        casadas, _escala = casar_rayas(sin_una, patron, MIN_RAYAS_Y)
        assert 5 not in casadas
        for indice, posicion in casadas.items():
            assert posicion == pytest.approx(patron[indice], abs=1e-6)

    def test_una_raya_ajena_no_desplaza_la_identificacion(self):
        """Un sello o una etiqueta añaden rayas que no son del formulario."""
        patron = _patron(MIN_RAYAS_Y + 2)
        con_extra = np.sort(np.append(np.array(patron), patron[7] + 0.006))
        casadas, _escala = casar_rayas(con_extra, patron, MIN_RAYAS_Y)
        assert len(casadas) == len(patron)
        for indice, posicion in casadas.items():
            assert posicion == pytest.approx(patron[indice], abs=1e-6)

    def test_sin_rayas_suficientes_no_opina(self):
        patron = _patron(MIN_RAYAS_Y + 2)
        casadas, escala = casar_rayas(np.array(patron[:3]), patron, MIN_RAYAS_Y)
        assert casadas == {}
        assert escala == 1.0

    def test_patron_vacio_no_revienta(self):
        casadas, _escala = casar_rayas(np.array([0.1, 0.2]), [], 2)
        assert casadas == {}


class TestRectDeCampo:
    """El rectángulo sale de las rayas de esta página."""

    def _reticula(self, plantilla, escala=1.0, desfase=0.0):
        return Reticula(
            y={i: v * escala + desfase
               for i, v in enumerate(plantilla.reticula.y)},
            x={i: v * escala + desfase
               for i, v in enumerate(plantilla.reticula.x)},
            escala_y=escala, escala_x=escala, fiable=True,
        )

    def test_sin_ancla_no_devuelve_nada(self):
        plantilla = _plantilla()
        campo = plantilla.fields[0]
        assert rect_de_campo(campo, self._reticula(plantilla), plantilla) is None

    def test_reproduce_el_rectangulo_sobre_el_patron(self):
        plantilla = _plantilla(
            ancla=AnclaCampo(raya_arriba=2, arriba=0.0,
                             raya_abajo=3, abajo=0.0)
        )
        campo = plantilla.fields[0]
        rect = rect_de_campo(campo, self._reticula(plantilla), plantilla)
        assert rect is not None
        _x, y, _w, h = rect
        assert y == pytest.approx(plantilla.reticula.y[2], abs=1e-9)
        assert h == pytest.approx(
            plantilla.reticula.y[3] - plantilla.reticula.y[2], abs=1e-9
        )

    def test_sigue_a_la_pagina_cuando_la_reticula_se_desplaza(self):
        """Lo que importa: el campo se mueve con la raya, no con el lienzo."""
        plantilla = _plantilla(
            ancla=AnclaCampo(raya_arriba=2, arriba=0.25,
                             raya_abajo=3, abajo=0.25)
        )
        campo = plantilla.fields[0]
        quieta = rect_de_campo(campo, self._reticula(plantilla), plantilla)
        movida = rect_de_campo(
            campo, self._reticula(plantilla, desfase=0.01), plantilla
        )
        assert movida[1] - quieta[1] == pytest.approx(0.01, abs=1e-9)
        assert movida[3] == pytest.approx(quieta[3], abs=1e-9)

    def test_sin_su_raya_no_coloca_el_campo(self):
        plantilla = _plantilla(
            ancla=AnclaCampo(raya_arriba=2, arriba=0.0,
                             raya_abajo=3, abajo=0.0)
        )
        campo = plantilla.fields[0]
        reticula = self._reticula(plantilla)
        reticula.y.pop(2)
        assert rect_de_campo(campo, reticula, plantilla) is None

    def test_el_hueco_se_estima_con_la_escala_de_esta_pagina(self):
        """Sin la raya siguiente, el hueco del patrón se lleva a esta página."""
        plantilla = _plantilla(
            ancla=AnclaCampo(raya_arriba=2, arriba=0.5,
                             raya_abajo=3, abajo=0.5)
        )
        campo = plantilla.fields[0]
        reticula = self._reticula(plantilla, escala=1.0)
        reticula.y.pop(3)
        rect = rect_de_campo(campo, reticula, plantilla)
        assert rect is None  # falta la raya del borde inferior

    def test_no_se_sale_del_lienzo(self):
        plantilla = _plantilla(
            ancla=AnclaCampo(raya_arriba=0, arriba=-40.0,
                             raya_abajo=1, abajo=0.0)
        )
        campo = plantilla.fields[0]
        rect = rect_de_campo(campo, self._reticula(plantilla), plantilla)
        assert rect is None or (0.0 <= rect[1] and rect[1] + rect[3] <= 1.0)


class TestPlantillaAjustada:
    """La copia ajustada deja intacto al resto del programa."""

    def _reticula(self, plantilla, desfase=0.0):
        return Reticula(
            y={i: v + desfase for i, v in enumerate(plantilla.reticula.y)},
            x={i: v + desfase for i, v in enumerate(plantilla.reticula.x)},
            fiable=True,
        )

    def test_reticula_no_fiable_devuelve_la_plantilla_original(self):
        plantilla = _plantilla(
            ancla=AnclaCampo(raya_arriba=2, arriba=0.0,
                             raya_abajo=3, abajo=0.0)
        )
        assert plantilla_ajustada(plantilla, Reticula()) is plantilla

    def test_sin_campos_anclados_devuelve_la_plantilla_original(self):
        plantilla = _plantilla()
        ajustada = plantilla_ajustada(plantilla, self._reticula(plantilla))
        assert ajustada is plantilla

    def test_mueve_el_campo_anclado_y_conserva_sus_umbrales(self):
        plantilla = _plantilla(
            ancla=AnclaCampo(raya_arriba=2, arriba=0.0,
                             raya_abajo=3, abajo=0.0),
            min_ink_peak=0.31,
        )
        ajustada = plantilla_ajustada(
            plantilla, self._reticula(plantilla, desfase=0.01)
        )
        movido = ajustada.field("firma")
        assert movido.y != plantilla.field("firma").y
        assert movido.min_ink_peak == 0.31
        assert movido.id == "firma"

    def test_no_toca_la_plantilla_de_entrada(self):
        plantilla = _plantilla(
            ancla=AnclaCampo(raya_arriba=2, arriba=0.0,
                             raya_abajo=3, abajo=0.0)
        )
        antes = plantilla.field("firma").y
        plantilla_ajustada(plantilla, self._reticula(plantilla, desfase=0.01))
        assert plantilla.field("firma").y == antes


class TestValidacionDelAncla:
    """Un ancla mal escrita es un error de plantilla, no un recorte torcido."""

    def test_rayas_invertidas(self):
        with pytest.raises(ValidationError):
            AnclaCampo(raya_arriba=5, arriba=0.0, raya_abajo=3, abajo=0.0)

    def test_bordes_invertidos_sobre_la_misma_raya(self):
        with pytest.raises(ValidationError):
            AnclaCampo(raya_arriba=3, arriba=0.5, raya_abajo=3, abajo=0.2)

    def test_media_pareja_vertical(self):
        with pytest.raises(ValidationError):
            AnclaCampo(raya_arriba=1, arriba=0.0, raya_abajo=2, abajo=0.0,
                       raya_izquierda=4)

    def test_patron_desordenado(self):
        with pytest.raises(ValidationError):
            PatronReticula(y=[0.3, 0.1])


class TestPlantillaReal:
    """La plantilla del proyecto trae patrón y anclas coherentes."""

    def test_todos_los_campos_estan_anclados(self):
        from app.templates.manager import TemplateManager

        plantilla = TemplateManager().load("template/aircraft_log.json")
        assert plantilla.reticula is not None
        assert len(plantilla.reticula.y) > MIN_RAYAS_Y
        assert len(plantilla.reticula.x) > MIN_RAYAS_X
        sin_ancla = [c.id for c in plantilla.fields if c.ancla is None]
        assert sin_ancla == []

    def test_las_anclas_apuntan_a_rayas_del_patron(self):
        from app.templates.manager import TemplateManager

        plantilla = TemplateManager().load("template/aircraft_log.json")
        for campo in plantilla.fields:
            ancla = campo.ancla
            assert ancla.raya_arriba < len(plantilla.reticula.y)
            assert ancla.raya_abajo < len(plantilla.reticula.y)
            if ancla.raya_izquierda is not None:
                assert ancla.raya_izquierda < len(plantilla.reticula.x)
                assert ancla.raya_derecha < len(plantilla.reticula.x)

    def test_el_ancla_reproduce_el_rectangulo_de_la_plantilla(self):
        """Anclar no mueve el campo: lo ata a la raya donde ya estaba."""
        from app.templates.manager import TemplateManager

        plantilla = TemplateManager().load("template/aircraft_log.json")
        reticula = Reticula(
            y={i: v for i, v in enumerate(plantilla.reticula.y)},
            x={i: v for i, v in enumerate(plantilla.reticula.x)},
            fiable=True,
        )
        for campo in plantilla.fields:
            rect = rect_de_campo(campo, reticula, plantilla)
            assert rect is not None, campo.id
            assert rect[0] == pytest.approx(campo.x, abs=1e-4), campo.id
            assert rect[1] == pytest.approx(campo.y, abs=1e-4), campo.id
            assert rect[2] == pytest.approx(campo.w, abs=1e-4), campo.id
            assert rect[3] == pytest.approx(campo.h, abs=1e-4), campo.id


class TestSobreUnaPaginaDibujada(unittest.TestCase):
    """La detección completa, sobre una retícula sintética que sí se desplaza.

    Las demás pruebas parten de rayas ya identificadas; esta ejerce la cadena
    entera (estructura impresa, perfiles, centros, casado y colocación) sobre
    una página dibujada, que es lo que permite comprobar que el campo sigue al
    papel y no al lienzo.
    """

    ALTO, ANCHO = 900, 1400
    # Separaciones irregulares, como las del formulario real (las suyas van
    # de 0,0095 a 0,0213). Un peine regular no sirve ni para probar ni para
    # trabajar: desplazado casi un paso es indistinguible de si mismo corrido
    # una raya, y no hay identificacion posible.
    _PASOS = [0.030, 0.022, 0.035, 0.018, 0.026]
    RAYAS_Y = [0.08]
    for _i in range(29):
        RAYAS_Y.append(round(RAYAS_Y[-1] + _PASOS[_i % len(_PASOS)], 6))
    RAYAS_X = [0.05]
    for _i in range(14):
        RAYAS_X.append(round(RAYAS_X[-1] + _PASOS[_i % len(_PASOS)] * 1.8, 6))

    def _pagina(self, desplaza_y=0.0, escala_y=1.0):
        pagina = np.full((self.ALTO, self.ANCHO, 3), 255, np.uint8)
        for y in self.RAYAS_Y:
            fila = int((y * escala_y + desplaza_y) * self.ALTO)
            if 0 <= fila < self.ALTO - 2:
                pagina[fila:fila + 2, :] = 30
        for x in self.RAYAS_X:
            col = int(x * self.ANCHO)
            pagina[:, col:col + 2] = 30
        return pagina

    def _plantilla(self):
        return Template(
            name="dibujada",
            reticula=PatronReticula(x=[round(v, 6) for v in self.RAYAS_X],
                                    y=[round(v, 6) for v in self.RAYAS_Y]),
            fields=[FieldTemplate(
                id="firma", type=FieldType.SIGNATURE,
                x=0.2, y=self.RAYAS_Y[10], w=0.3,
                h=self.RAYAS_Y[11] - self.RAYAS_Y[10],
                ancla=AnclaCampo(raya_arriba=10, arriba=0.0,
                                 raya_abajo=11, abajo=0.0),
            )],
        )

    def test_identifica_las_rayas_dibujadas(self):
        plantilla = self._plantilla()
        reticula = leer_reticula(plantilla, self._pagina())
        self.assertTrue(reticula.fiable)
        self.assertEqual(len(reticula.y), len(self.RAYAS_Y))

    def test_el_campo_sigue_a_la_reticula_desplazada(self):
        plantilla = self._plantilla()
        quieta = leer_reticula(plantilla, self._pagina())
        movida = leer_reticula(plantilla, self._pagina(desplaza_y=0.02))
        self.assertTrue(movida.fiable)
        antes = rect_de_campo(plantilla.fields[0], quieta, plantilla)
        despues = rect_de_campo(plantilla.fields[0], movida, plantilla)
        # El campo se mueve con el papel, no se queda en su coordenada.
        self.assertAlmostEqual(despues[1] - antes[1], 0.02, delta=0.003)
        self.assertAlmostEqual(despues[3], antes[3], delta=0.003)

    def test_el_campo_sigue_a_la_reticula_estirada(self):
        plantilla = self._plantilla()
        estirada = leer_reticula(plantilla, self._pagina(escala_y=1.02))
        self.assertTrue(estirada.fiable)
        rect = rect_de_campo(plantilla.fields[0], estirada, plantilla)
        self.assertAlmostEqual(rect[1], self.RAYAS_Y[10] * 1.02, delta=0.003)

    def test_una_pagina_en_blanco_no_inventa_reticula(self):
        plantilla = self._plantilla()
        blanco = np.full((self.ALTO, self.ANCHO, 3), 255, np.uint8)
        reticula = leer_reticula(plantilla, blanco)
        self.assertFalse(reticula.fiable)
        self.assertIs(plantilla_ajustada(plantilla, reticula), plantilla)
