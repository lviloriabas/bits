"""El motor OCR debe inicializarse siempre para ejecución en CPU."""

from __future__ import annotations

import sys
import numpy as np
import pytest
from types import ModuleType

from app.ocr.engine import PaddleOcrEngine


def _install_fake_paddleocr(monkeypatch, paddle_class) -> None:
    module = ModuleType("paddleocr")
    module.PaddleOCR = paddle_class
    monkeypatch.setitem(sys.modules, "paddleocr", module)


def test_paddle_v3_forces_cpu_even_if_caller_requests_other_device(monkeypatch):
    calls = []

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    _install_fake_paddleocr(monkeypatch, FakePaddleOCR)
    PaddleOcrEngine(device="gpu")._ensure_engine()

    assert calls[0]["device"] == "cpu"


def test_paddle_v2_disables_gpu_even_if_caller_requests_it(monkeypatch):
    calls = []

    class FakePaddleOCR:
        def __init__(
            self, use_angle_cls=False, use_gpu=True, lang="en", show_log=True,
            **kwargs,
        ):
            calls.append({"use_gpu": use_gpu, **kwargs})

    _install_fake_paddleocr(monkeypatch, FakePaddleOCR)
    PaddleOcrEngine(use_gpu=True)._ensure_engine()

    assert calls[0]["use_gpu"] is False


@pytest.mark.parametrize("fallar_al_cargar", [True, False])
def test_void_repite_mismo_modelo_en_cpu_si_onednn_falla(monkeypatch, fallar_al_cargar):
    llamadas = []
    class Predictor:
        def __init__(self, acelerado):
            self.acelerado = acelerado
        def predict(self, imagenes):
            if self.acelerado:
                raise RuntimeError("oneDNN no compatible")
            return [{"rec_text": "VOID", "rec_score": .9} for _ in imagenes]
    def crear(**kwargs):
        llamadas.append(kwargs)
        acelerado = "engine_config" in kwargs
        if acelerado and fallar_al_cargar:
            raise RuntimeError("oneDNN no compatible")
        return Predictor(acelerado)
    modulo = ModuleType("paddlex")
    modulo.create_predictor = crear
    monkeypatch.setitem(sys.modules, "paddlex", modulo)
    motor = PaddleOcrEngine(rec_model="PP-OCRv6_medium_rec", cpu_threads=1, rec_mkldnn=True)
    assert motor.recognize_lines([np.zeros((20, 80, 3), np.uint8)])[0][0].text == "VOID"
    assert len(llamadas) == 2
    assert all(c["device"] == "cpu" and c["model_name"] == "PP-OCRv6_medium_rec" for c in llamadas)
    assert llamadas[0]["engine_config"] == {"run_mode": "mkldnn", "cpu_threads": 1}
    assert "engine_config" not in llamadas[1]
