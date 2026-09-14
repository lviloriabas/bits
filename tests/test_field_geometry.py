"""Anclas locales: desplazamientos distintos, rayas cortadas y ambiguedad."""

from unittest.mock import patch

import numpy as np
import pytest

from app.templates.schema import AnclaCampo, FieldTemplate, FieldType, PatronReticula, Template
from app.vision.field_geometry import place_fields


H, W = 900, 1400
Y = [0.08]
X = [0.05]
for i in range(29):
    Y.append(round(Y[-1] + (0.030, 0.022, 0.035, 0.018, 0.026)[i % 5], 6))
for i in range(14):
    X.append(round(X[-1] + (0.030, 0.022, 0.035, 0.018, 0.026)[i % 5] * 1.8, 6))


def fixture():
    template = Template(
        name="local", reticula=PatronReticula(x=X, y=Y),
        fields=[FieldTemplate(
            id="log_number", type=FieldType.OCR,
            x=X[4], w=X[7] - X[4], y=Y[10], h=Y[11] - Y[10],
            ancla=AnclaCampo(raya_arriba=10, arriba=0, raya_abajo=11, abajo=0,
                             raya_izquierda=4, izquierda=0, raya_derecha=7, derecha=0),
        )],
    )
    image = np.full((H, W, 3), 255, np.uint8)
    for value in Y:
        row = round(value * H)
        image[row:row + 2, :] = 0
    for value in X:
        col = round(value * W)
        image[:, col:col + 2] = 0
    return template, image


def test_refina_la_raya_en_el_campo_sin_mover_el_resto_de_la_hoja():
    template, image = fixture()
    before = place_fields(template, image)
    left, right = round(X[4] * W) + 2, round(X[7] * W)
    for index in (10, 11, 12):
        row = round(Y[index] * H)
        image[row:row + 2, left:right] = 255
        image[row + 4:row + 6, left:right] = 0
    after = place_fields(template, image)
    assert after.unverified == ()
    assert (after.template.fields[0].y - before.template.fields[0].y) * H == pytest.approx(4, abs=0.6)
    assert template.fields[0].y == Y[10]


@pytest.mark.parametrize("axis", ["x", "y"])
def test_recupera_raya_que_solo_existe_cerca_del_campo(axis):
    template, image = fixture()
    if axis == "y":
        row = round(Y[10] * H)
        image[row:row + 2, :] = 255
        image[row:row + 2, round(X[4] * W):round(X[7] * W)] = 0
    else:
        col = round(X[4] * W)
        image[:, col:col + 2] = 255
        image[round((Y[10] - .025) * H):round((Y[11] + .025) * H), col:col + 2] = 0
    result = place_fields(template, image)
    assert result.unverified == ()
    field = result.template.fields[0]
    assert field.x == pytest.approx(X[4], abs=.002)
    assert field.y == pytest.approx(Y[10], abs=.002)


def test_no_da_por_verificada_una_raya_vertical_ausente():
    template, image = fixture()
    col = round(X[4] * W)
    image[:, col:col + 2] = 255
    assert place_fields(template, image).unverified == ("log_number",)


def test_dos_rayas_en_la_misma_ventana_dejan_el_campo_en_duda():
    template, image = fixture()
    row = round(Y[10] * H) + 4
    image[row:row + 2, round(X[4] * W):round(X[7] * W)] = 0
    assert place_fields(template, image).unverified == ("log_number",)


def test_blanco_no_se_usa_como_prueba_de_posicion():
    template, image = fixture()
    image[:] = 255
    result = place_fields(template, image)
    assert result.unverified == ("log_number",)
    assert result.template.fields[0] == template.fields[0]


def test_cada_pagina_mide_su_propio_desplazamiento_sin_red():
    import cv2

    template, image = fixture()
    with patch("socket.socket", side_effect=AssertionError("No debe usar red")):
        first = place_fields(template, image)
        shifted = cv2.warpAffine(image, np.float32([[1, 0, 12], [0, 1, 18]]),
                                 (W, H), borderValue=(255, 255, 255))
        second = place_fields(template, shifted)
    assert second.unverified == ()
    assert (second.template.fields[0].x - first.template.fields[0].x) * W == pytest.approx(12, abs=1)
    assert (second.template.fields[0].y - first.template.fields[0].y) * H == pytest.approx(18, abs=1)


def test_pipeline_usa_el_mismo_rectangulo_para_ocr_y_previa():
    from app.core.config import AppConfig
    from app.core.pipeline import process_page_image

    template, image = fixture()
    expected = place_fields(template, image).template.fields[0]
    captured = []

    def ocr(_engine, _image, fields, *_args, **_kwargs):
        captured.extend(fields)
        return [("1234567", .99)]

    with patch("app.core.pipeline.ocr_regions", side_effect=ocr):
        result = process_page_image(image, 1, AppConfig(align=False, deskew=False),
                                    object(), template, reference=None)
    assert captured[0] == expected
    assert result.preview_boxes["log_number"] == [expected.x, expected.y, expected.w, expected.h]


def test_pipeline_advierte_solo_el_campo_sin_posicion_confirmada():
    from app.core.config import AppConfig
    from app.core.pipeline import process_page_image
    from app.models.schemas import Status

    template, image = fixture()
    col = round(X[4] * W)
    image[:, col:col + 2] = 255
    with patch("app.core.pipeline.ocr_regions", return_value=[("1234567", .99)]):
        result = process_page_image(image, 1, AppConfig(align=False, deskew=False),
                                    object(), template, reference=None)
    assert result.fields[0].status == Status.WARNING
    assert "Posicion del campo sin confirmar" in result.fields[0].comment


def test_preprocesado_envia_las_cajas_ajustadas_sin_ocr(tmp_path):
    from app.core.config import AppConfig
    from app.gui.worker import PreprocessWorker

    template, image = fixture()
    expected = place_fields(template, image).template.fields[0]

    class Renderer:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def render_page(self, *_args):
            return image

    config = AppConfig(align=False, deskew=False)
    worker = PreprocessWorker([tmp_path / "pagina.pdf"], tmp_path / "template.json", config)
    ready, errors = [], []
    worker.page_ready.connect(lambda *args: ready.append(args))
    worker.failed.connect(errors.append)
    with patch("app.gui.worker.TemplateManager.load", return_value=template), \
         patch("app.vision.pdf_loader.PdfPageRenderer", Renderer), \
         patch("app.vision.pdf_loader.page_count", return_value=1), \
         patch("app.core.config.config_for_pdf", return_value=config):
        worker.run()
    assert errors == []
    assert ready[0][2]["boxes"]["log_number"] == [expected.x, expected.y, expected.w, expected.h]


def test_visor_usa_cajas_de_preprocesado_antes_del_ocr(app):
    from PySide6.QtGui import QImage
    from app.gui.main_window import MainWindow

    window = MainWindow()
    try:
        template, _image = fixture()
        boxes = {"log_number": [.3, .4, .2, .05]}
        window._processed_template = template
        window._preview_pending = (3, "pagina.pdf")
        window._preprocess_geometry[("pagina.pdf", 3)] = {"boxes": boxes}
        window._preview_base_image = QImage(140, 90, QImage.Format.Format_RGB32)
        window.fields_check.setChecked(True)
        with patch.object(window, "_current_preview_result", return_value=None), \
             patch.object(window, "_draw_template_boxes") as draw:
            window._apply_preview_overlay()
        assert draw.call_args.kwargs["boxes"] == boxes
    finally:
        window.close()
