"""VOID exige una palabra grande confirmada y conserva los datos del indice."""

import numpy as np
import pytest

from app.models.schemas import OcrResult
from app.vision import void_mark
from app.validation.discrepancias import clasificar_lote
from tests.test_discrepancias import TEMPLATE, _corregida, _reporte


@pytest.mark.parametrize("texto,score,esperado", [
    ("VOID", .95, True), ("V01D", .90, True), ("VOlD", .85, True),
    ("VOID", .79, False), ("VOD", .99, False), ("GOLD", .99, False),
    ("COLOCAR VOID", .99, False), ("AVOID", .99, False), ("VOYD", .99, False),
])
def test_exige_las_cuatro_letras(texto, score, esperado):
    assert void_mark.es_lectura_void(texto, score) is esperado


def test_las_casillas_vacias_no_crean_evidencia_void():
    page = _corregida(correction_block=("false", .99))
    clasificar_lote([_reporte(page)], TEMPLATE)
    assert page.void_mark is None


def test_void_con_correccion_escrita_conserva_indice_y_no_reclama_firmas():
    page = _corregida()
    page.date = "2026-08-17"
    campos = [f.model_dump() for f in page.fields]
    assert clasificar_lote([_reporte(page)], TEMPLATE)
    page.void_mark = OcrResult(text="VOID", confidence=.92)
    assert clasificar_lote([_reporte(page)], TEMPLATE) == []
    assert not page.discrepancy and not page.discrepancy_fields
    assert page.date == "2026-08-17"
    assert [f.model_dump() for f in page.fields] == campos


def test_una_lectura_incierta_no_borra_la_discrepancia():
    page = _corregida()
    page.void_mark = OcrResult(text="VOID", confidence=.6)
    assert clasificar_lote([_reporte(page)], TEMPLATE)
    assert page.discrepancy


def test_sin_letras_grandes_no_invoca_el_reconocedor():
    class NoDebeLlamarse:
        def recognize_lines(self, _):
            raise AssertionError("No hay regiones para OCR")
    assert void_mark.detectar_void(np.full((800, 1000, 3), 255, np.uint8), NoDebeLlamarse()) is None


def test_exige_dos_lecturas_y_admite_cancelar(monkeypatch):
    monkeypatch.setattr(void_mark, "candidatos", lambda _: [
        (1, np.array([400., 300.]), 350., 160., -25.),
    ])
    class Motor:
        def __init__(self, cantidad):
            self.cantidad = cantidad
        def recognize_lines(self, _):
            return [[OcrResult(text="VOID", confidence=.92)] for _ in range(self.cantidad)]
    image = np.full((800, 1000, 3), 255, np.uint8)
    assert void_mark.detectar_void(image, Motor(1)) is None
    assert void_mark.detectar_void(image, Motor(2)).text == "VOID"
    assert void_mark.detectar_void(image, Motor(2), cancelado=lambda: True) is None


def test_cada_zona_es_una_llamada_y_la_confirmada_corta_la_busqueda(monkeypatch):
    """Una llamada por zona: la que confirma detiene el resto."""
    zonas = [(5 - n, np.array([200. + 150 * n, 300. + 90 * n]), 300., 120., 0.)
             for n in range(5)]
    monkeypatch.setattr(void_mark, "candidatos", lambda _: zonas)

    class Motor:
        def __init__(self):
            self.llamadas = []

        def recognize_lines(self, crops):
            self.llamadas.append(len(crops))
            return [
                [OcrResult(text="VOID", confidence=.9)]
                if len(self.llamadas) == 4 else []
                for _ in crops
            ]

    motor = Motor()
    marca = void_mark.detectar_void(np.full((1000, 1300, 3), 255, np.uint8), motor)
    assert motor.llamadas == [6, 6, 6, 6]
    assert marca is not None and marca.text == "VOID"
    centro = np.mean(np.array(marca.box), axis=0) * np.array([1300, 1000])
    assert np.allclose(centro, zonas[3][1], atol=1)


def test_el_modelo_se_carga_una_vez_por_proceso(monkeypatch):
    creados = []

    class Motor:
        def __init__(self, **kwargs):
            creados.append(kwargs)

    monkeypatch.setattr(void_mark, "modelo_disponible", lambda: True)
    monkeypatch.setattr(void_mark, "_MOTOR", None)
    monkeypatch.setattr("app.ocr.engine.PaddleOcrEngine", Motor)
    assert void_mark.motor_void() is void_mark.motor_void()
    assert creados == [{
        "cpu_threads": void_mark._HILOS_RECONOCEDOR,
        "rec_model": void_mark.MODELO_VOID,
    }]


def _hojas(*numeros):
    from app.models.schemas import PageResult
    return [PageResult(page_number=numero) for numero in numeros]


def test_con_pool_libre_las_hojas_se_reparten_y_el_avance_cuenta_hojas(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from app.core.progress import VOID_STAGE

    hojas = _hojas(3, 5, 8, 9)
    pedidas = []

    def comprobar(pdf, numero):
        pedidas.append(numero)
        return OcrResult(text="VOID", confidence=.9) if numero == 8 else None

    monkeypatch.setattr(void_mark, "paginas_por_revisar", lambda pages, _t: list(pages))
    monkeypatch.setattr(void_mark, "modelo_disponible", lambda: True)
    monkeypatch.setattr(void_mark, "_cabe_en_el_pool", lambda _pool: True)
    monkeypatch.setattr(void_mark, "comprobar_hoja_en_worker", comprobar)

    class Pool:
        max_workers = 3
        executor = ThreadPoolExecutor(max_workers=3)

    avisos = []
    revisadas = void_mark.revisar_voids(
        "libro.pdf", hojas, None, None,
        lambda hechas, total, etapa: avisos.append((hechas, total, etapa)),
        lambda: False, pool=Pool(),
    )
    Pool.executor.shutdown()
    assert revisadas == 4
    assert sorted(pedidas) == [3, 5, 8, 9]
    assert avisos == [(n, 4, VOID_STAGE) for n in range(5)]
    assert [bool(hoja.void_mark) for hoja in hojas] == [False, False, True, False]


def test_sin_pool_la_comprobacion_sigue_en_el_proceso(monkeypatch):
    from app.core.progress import VOID_STAGE

    hojas = _hojas(1, 2)
    monkeypatch.setattr(void_mark, "paginas_por_revisar", lambda pages, _t: list(pages))
    monkeypatch.setattr(void_mark, "modelo_disponible", lambda: True)
    monkeypatch.setattr(void_mark, "motor_void", lambda *_a: object())
    monkeypatch.setattr(void_mark, "detectar_void", lambda *_a, **_k: None)

    class Renderer:
        def render_page(self, _numero, dpi):
            assert dpi == void_mark.DPI_VOID
            return np.full((10, 10, 3), 255, np.uint8)

    avisos = []
    assert void_mark.revisar_voids(
        "libro.pdf", hojas, None, Renderer(),
        lambda hechas, total, etapa: avisos.append((hechas, total, etapa)),
        lambda: False,
    ) == 2
    assert avisos == [(0, 2, VOID_STAGE), (1, 2, VOID_STAGE), (2, 2, VOID_STAGE)]
