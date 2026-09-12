"""Contador de páginas del avance: solo sube y cuenta el batch entero.

Dos fallos que se veían en pantalla: el número se devolvía (52, 48, 53…)
porque la etapa nombraba la última página que entregaba el pool, y el total
era el del documento abierto en vez del de la ejecución.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.core.config import AppConfig
from app.core.pipeline import Pipeline, process_pdf_batch
from app.core.progress import PAGES_STAGE, VOID_STAGE, with_page_counter
from app.models.schemas import PageResult, ValidationReport
from app.templates.schema import Template


class _FakePool:
    def __init__(self, root: Path, workers: int = 4):
        self.max_workers = workers
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self.root = root

    def temporary_path(self, name: str) -> Path:
        return self.root / name

    def prepare(self, _state: dict) -> Path:
        path = self.root / "state.pickle"
        path.touch()
        return path

    @staticmethod
    def release(path: Path) -> None:
        path.unlink(missing_ok=True)


class _FakeRenderer:
    """Renderer de batch: solo hace falta el recuento de páginas."""

    counts = {"a.pdf": 10, "b.pdf": 20}

    def __init__(self, path):
        self.name = Path(path).name

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def page_count(self) -> int:
        return self.counts[self.name]


class _FakeEngine:
    name = "fake"


def test_parallel_pages_report_a_counter_that_only_grows(tmp_path):
    """Con varias páginas en vuelo el contador no puede retroceder."""
    pool = _FakePool(tmp_path, workers=4)
    pipeline = Pipeline(
        AppConfig(align=False),
        _FakeEngine(),
        Template(name="empty"),
        workers=4,
        process_pool=pool,
    )
    updates: list[tuple[int, str]] = []
    pipeline.on_progress = lambda done, _total, message: updates.append(
        (done, message)
    )
    delivered: list[int] = []

    def worker(page: int, _state):
        # Las impares tardan más: el pool entrega las páginas desordenadas,
        # que es justo lo que hacía retroceder al número de la etapa.
        time.sleep(0.03 if page % 2 else 0.001)
        delivered.append(page)
        return PageResult(page_number=page)

    with patch("app.core.pipeline._process_page_worker", side_effect=worker):
        pages = pipeline._process_parallel(
            Path("book.pdf"), 1, 20, np.zeros((2, 2, 3), np.uint8)
        )
    pool.executor.shutdown()

    assert [page.page_number for page in pages] == list(range(1, 21))
    # El escenario es el que se veía en producción: entregas desordenadas.
    assert delivered != sorted(delivered)
    counters = [done for done, _message in updates]
    assert counters == sorted(counters) == list(range(1, 21))
    # La etapa ya no nombra ninguna página: no hay "página en curso" cuando
    # hay tantas en vuelo como workers.
    assert {message for _done, message in updates} == {PAGES_STAGE}


def test_sequential_pages_report_the_same_stage(tmp_path):
    """La ruta de un worker cuenta igual: páginas terminadas del tramo."""
    pipeline = Pipeline(
        AppConfig(align=False, deskew=False),
        _FakeEngine(),
        Template(name="empty"),
    )
    updates: list[tuple[int, int, str]] = []
    pipeline.on_progress = lambda done, total, message: updates.append(
        (done, total, message)
    )

    with patch(
        "app.core.pipeline.render_page",
        return_value=np.zeros((4, 4, 3), np.uint8),
    ), patch(
        "app.core.pipeline.process_page_image",
        side_effect=lambda _image, number, *a, **k: PageResult(
            page_number=number
        ),
    ):
        pages = pipeline._process_sequential(
            Path("book.pdf"), 11, 5, np.zeros((2, 2, 3), np.uint8)
        )

    assert [page.page_number for page in pages] == [11, 12, 13, 14, 15]
    assert updates == [(index, 5, PAGES_STAGE) for index in range(5)]


def test_batch_message_counts_the_pages_of_the_whole_run(tmp_path):
    """El texto habla del batch (30), no del documento en curso (20)."""
    paths = [tmp_path / "a.pdf", tmp_path / "b.pdf"]
    pool = _FakePool(tmp_path, workers=4)
    updates: list[tuple[int, int, str]] = []

    class _FakePipeline:
        def __init__(self, *_args, **_kwargs):
            self.on_progress = None

        def process(self, path, page_range=None, should_cancel=None):
            count = 10 if Path(path).name == "a.pdf" else 20
            for done in range(count):
                self.on_progress(done, count, PAGES_STAGE)
            return ValidationReport(
                pdf_path=str(path),
                template_name="empty",
                pages=[PageResult(page_number=n) for n in range(1, count + 1)],
            )

    with patch("app.core.pipeline.PdfPageRenderer", _FakeRenderer), patch(
        "app.core.pipeline.config_for_pdf", side_effect=lambda config, _p: config
    ), patch("app.core.pipeline.Pipeline", _FakePipeline):
        process_pdf_batch(
            paths,
            AppConfig(),
            Template(name="empty"),
            pool,
            _FakeEngine(),
            on_progress=lambda done, total, message: updates.append(
                (done, total, message)
            ),
        )
    pool.executor.shutdown()

    stages = [
        (done, total, message)
        for done, total, message in updates
        if message.endswith(PAGES_STAGE)
    ]
    assert all(total == 30 for _done, total, _message in stages)
    # El contador global no reinicia al cambiar de archivo.
    counters = [done for done, _total, _message in stages]
    assert counters == sorted(counters)
    assert counters[-1] == 29
    # Cada etapa dice de qué archivo del batch está hablando.
    assert stages[0][2].startswith("Archivo 1/2: a.pdf - ")
    assert stages[-1][2].startswith("Archivo 2/2: b.pdf - ")
    assert with_page_counter(*stages[-1]) == (
        "Archivo 2/2: b.pdf - Procesando páginas 29/30"
    )


def test_file_strategy_message_carries_the_page_counter(tmp_path):
    """Repartiendo PDFs completos el texto también cuenta páginas del batch."""
    paths = [tmp_path / "a.pdf", tmp_path / "b.pdf"]
    pool = _FakePool(tmp_path, workers=2)
    updates: list[tuple[int, int, str]] = []

    def worker(path, *args):
        counter = Path(args[-1])
        count = 10 if Path(path).name == "a.pdf" else 20
        # El segundo archivo va despacio a propósito: el padre tiene que
        # alcanzar a leer su contador con el primero ya terminado.
        delay = 0.001 if count == 10 else 0.03
        for page in range(1, count + 1):
            counter.write_text(f"{page}/{count}", encoding="ascii")
            time.sleep(delay)
        return ValidationReport(
            pdf_path=str(path),
            template_name="empty",
            pages=[PageResult(page_number=n) for n in range(1, count + 1)],
        )

    with patch("app.core.pipeline.PdfPageRenderer", _FakeRenderer), patch(
        "app.core.pipeline._process_pdf_worker", side_effect=worker
    ):
        process_pdf_batch(
            paths,
            AppConfig(),
            Template(name="empty"),
            pool,
            _FakeEngine(),
            on_progress=lambda done, total, message: updates.append(
                (done, total, message)
            ),
        )
    pool.executor.shutdown()

    live = [
        (done, total, message)
        for done, total, message in updates
        if message.endswith(PAGES_STAGE)
    ]
    assert live, "la estrategia por archivos no publicó ninguna etapa"
    assert all(total == 30 for _done, total, _message in live)
    counters = [done for done, _total, _message in live]
    assert counters == sorted(counters)
    assert with_page_counter(*live[-1]).endswith(f"{counters[-1]}/30")


def test_the_void_stage_is_not_counted_as_pages(tmp_path):
    """La segunda vuelta cuenta hojas: el archivo no vuelve a empezar."""
    paths = [tmp_path / "a.pdf", tmp_path / "b.pdf"]
    pool = _FakePool(tmp_path, workers=4)
    updates: list[tuple[int, int, str]] = []
    files: list[tuple[int, int, int]] = []
    reviews: list[tuple[int, int, int]] = []

    class _FakePipeline:
        def __init__(self, *_args, **_kwargs):
            self.on_progress = None

        def process(self, path, page_range=None, should_cancel=None):
            count = 10 if Path(path).name == "a.pdf" else 20
            for done in range(count + 1):
                self.on_progress(done, count, PAGES_STAGE)
            for done in range(4):
                self.on_progress(done, 3, VOID_STAGE)
            return ValidationReport(
                pdf_path=str(path),
                template_name="empty",
                pages=[PageResult(page_number=n) for n in range(1, count + 1)],
            )

    with patch("app.core.pipeline.PdfPageRenderer", _FakeRenderer), patch(
        "app.core.pipeline.config_for_pdf", side_effect=lambda config, _p: config
    ), patch("app.core.pipeline.Pipeline", _FakePipeline):
        process_pdf_batch(
            paths,
            AppConfig(),
            Template(name="empty"),
            pool,
            _FakeEngine(),
            on_progress=lambda done, total, message: updates.append(
                (done, total, message)
            ),
            on_file_progress=lambda *args: files.append(args),
            on_review_progress=lambda *args: reviews.append(args),
        )
    pool.executor.shutdown()

    counters = [done for done, _total, _message in updates]
    assert counters == sorted(counters)
    void = [item for item in updates if VOID_STAGE in item[2]]
    assert [done for done, _t, _m in void[:4]] == [10] * 4
    assert void[3][2] == f"Archivo 1/2: a.pdf - {VOID_STAGE} 3/3"
    first_file = [done for index, done, _total in files if index == 1]
    assert first_file == sorted(first_file)
    assert reviews == [(1, n, 3) for n in range(4)] + [(2, n, 3) for n in range(4)]


def test_the_worker_counter_keeps_pages_and_void_apart(tmp_path):
    from app.core.pipeline import (
        _page_counter_writer,
        _read_page_counter,
        _read_review_counter,
    )

    path = tmp_path / "pages_0.count"
    write = _page_counter_writer(path)
    write(50, 50, PAGES_STAGE)
    assert _read_review_counter(path) is None
    write(3, 40, VOID_STAGE)
    assert _read_page_counter(path) == (50, 50)
    assert _read_review_counter(path) == (3, 40)
    # El formato de siempre sigue leyéndose.
    path.write_text("7/10", encoding="ascii")
    assert _read_page_counter(path) == (7, 10)
    assert _read_review_counter(path) is None


def test_with_page_counter_only_touches_the_pages_stage():
    assert with_page_counter(5, 30, PAGES_STAGE) == "Procesando páginas 5/30"
    assert with_page_counter(5, 30, "Generando reporte") == "Generando reporte"
    # Sin total no hay contador que poner (arranque de la ejecución).
    assert with_page_counter(0, 0, PAGES_STAGE) == PAGES_STAGE
