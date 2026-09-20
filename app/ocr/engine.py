"""Motor OCR de la aplicación (PaddleOCR).

Arquitectura: el pipeline depende del protocolo ``OcrEngine``, de modo que
se puede añadir otro motor sin tocar el núcleo.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import List, Optional, Protocol

import numpy as np
from loguru import logger

from app.models.schemas import OcrResult
from app.utils.portable import ensure_portable_env


def _init_params(cls) -> set:
    """Parámetros aceptados por el constructor de una clase."""
    return set(inspect.signature(cls.__init__).parameters)


class OcrEngine(Protocol):
    """Interfaz común de un motor de reconocimiento de texto."""

    name: str

    def recognize(self, image: np.ndarray) -> List[OcrResult]:
        """Reconoce el texto de una imagen.

        Args:
            image: Imagen BGR.

        Returns:
            Lista de líneas de texto con confianza.
        """
        ...

    def recognize_batch(self, images: List[np.ndarray]) -> List[List[OcrResult]]:
        """Reconoce varias imágenes en una sola llamada al motor.

        Args:
            images: Imágenes BGR.

        Returns:
            Lista (una por imagen) de líneas de texto con confianza.
        """
        ...

    def recognize_lines(self, images: List[np.ndarray]) -> List[List[OcrResult]]:
        """Reconoce recortes que ya son una sola línea de texto.

        Permite saltarse la fase de detección cuando el recorte de la
        plantilla contiene exactamente un valor. Un motor que no pueda
        separar ambas fases devuelve lo mismo que ``recognize_batch``.
        """
        ...


class PaddleOcrEngine:
    """Motor OCR basado en PaddleOCR (carga perezosa).

    La aplicación usa la pareja validada ``PP-OCRv6_medium_det`` y
    ``PP-OCRv5_mobile_rec``. Los parámetros explícitos se conservan para las
    herramientas diagnósticas, pero no existe selección automática de un
    modelo alternativo en producción.
    """

    name = "paddle"

    _HANDWRITTEN_REC_MODEL = "PP-OCRv5_mobile_rec"
    _HANDWRITTEN_DET_MODEL = "PP-OCRv6_medium_det"

    # Recortes por llamada al reconocedor. Medido: 229 ms/recorte con batches
    # de 1 frente a 172 ms con batches de 16 (el resto de la curva es plana).
    _REC_BATCH_SIZE = 16

    def __init__(self, lang: str = "en", cpu_threads: Optional[int] = None,
                 det_model: Optional[str] = None,
                 rec_model: Optional[str] = None, rec_mkldnn: bool = False,
                 **kwargs) -> None:
        self.lang = lang
        self._cpu_threads = cpu_threads
        self._det_model = det_model or self._auto_det_model()
        self._rec_model = rec_model or self._auto_rec_model()
        self._rec_mkldnn = rec_mkldnn
        self._extra_kwargs = kwargs
        self._engine = None
        self._recognizer = None

    @classmethod
    def _auto_rec_model(cls) -> str:
        """Modelo de reconocimiento fijo validado para escritura a mano."""
        return cls._HANDWRITTEN_REC_MODEL

    @classmethod
    def _auto_det_model(cls) -> str:
        """Detector fijo validado para manuscrito pequeño."""
        return cls._HANDWRITTEN_DET_MODEL

    @classmethod
    def describe_models(cls) -> str:
        """Descripción de los modelos reconocidos disponibles (log/info)."""
        root = Path(__file__).resolve().parents[2]
        det = cls._auto_det_model()
        rec = cls._auto_rec_model()
        det_ok = (root / "portable" / "paddlex" / "official_models" / det).is_dir()
        rec_ok = (root / "portable" / "paddlex" / "official_models" / rec).is_dir()
        return (f"det={det}({det_ok}) rec={rec}({rec_ok})")

    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        # Mantiene los modelos dentro de la carpeta del proyecto (portable).
        ensure_portable_env()
        # Evita el modo oneDNN de paddlex (bug conocido en Windows):
        # ConvertPirAttribute2RuntimeAttribute not supported.
        os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError(
                "PaddleOCR no está instalado. Ejecute:\n"
                "  python -m pip install --user paddlepaddle paddleocr"
            ) from exc
        logger.info("Inicializando PaddleOCR (primera carga puede tardar)...")
        if "use_angle_cls" in _init_params(PaddleOCR):
            kwargs = dict(self._extra_kwargs)
            # La API 2.x usa este interruptor. Se fija después de mezclar los
            # argumentos extra para que ningún llamador pueda habilitar otro
            # dispositivo accidentalmente.
            kwargs["use_gpu"] = False
            if self._cpu_threads is not None:
                kwargs["cpu_threads"] = self._cpu_threads
            self._engine = PaddleOCR(
                use_angle_cls=True, lang=self.lang, show_log=False,
                **kwargs,
            )
        else:
            # API v3.x: use_angle_cls/show_log eliminados. Se desactivan los
            # preprocesadores de documento (orientación, desenvolvimiento y
            # orientación de líneas): recortan/transforman la imagen y
            # desplazan las coordenadas de los recortes regionales.
            defaults = {
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
            }
            kwargs = {**defaults, **self._extra_kwargs}
            # PaddleOCR/PaddleX 3.x acepta ``device`` mediante **kwargs.
            # Declararlo evita que una instalación distinta autodetecte un
            # acelerador y mantiene el portable compatible con equipos Intel.
            kwargs["device"] = "cpu"
            kwargs["text_detection_model_name"] = self._det_model
            kwargs["text_recognition_model_name"] = self._rec_model
            if self._cpu_threads is not None:
                kwargs["cpu_threads"] = self._cpu_threads
            self._engine = PaddleOCR(lang=self.lang, **kwargs)
        return self._engine

    def _ensure_recognizer(self):
        """Carga solo el reconocedor, sin el detector de texto.

        El detector es la fase cara (646 ms por recorte frente a 175 ms del
        reconocedor). Los campos declarados ``ocr_mode='line'`` ya entregan
        un recorte con un único valor, así que detectar dónde está el texto
        no aporta nada. Se instancia con ``device='cpu'`` explícito y usa el
        mismo cache de modelos portable que el resto de la aplicación.
        """
        if self._recognizer is not None:
            return self._recognizer
        ensure_portable_env()
        os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
        try:
            from paddlex import create_predictor
        except ImportError:
            logger.debug(
                "paddlex no disponible: los campos de una sola línea usan "
                "el detector completo"
            )
            self._recognizer = False
            return False
        kwargs = {"batch_size": self._REC_BATCH_SIZE, "device": "cpu"}
        if self._cpu_threads is not None:
            kwargs["cpu_threads"] = self._cpu_threads
        if self._rec_mkldnn:
            # Solo el reconocedor validado para VOID. El detector mantiene
            # su configuracion porque su ruta oneDNN falla en Windows.
            kwargs["engine_config"] = {
                "run_mode": "mkldnn", "cpu_threads": self._cpu_threads or 1,
            }
        try:
            self._recognizer = create_predictor(
                model_name=self._rec_model, **kwargs
            )
        except Exception:  # noqa: BLE001 - se degrada al detector completo
            if self._rec_mkldnn:
                self._rec_mkldnn = False
                return self._ensure_recognizer()
            logger.warning(
                "No se pudo cargar el reconocedor sin detector; "
                "los campos de una sola línea usan el pipeline completo",
                exc_info=True,
            )
            self._recognizer = False
        return self._recognizer

    def recognize(self, image: np.ndarray) -> List[OcrResult]:
        engine = self._ensure_engine()
        image = self._to_bgr(image)
        try:
            raw = engine.ocr(image, cls=True)  # API v2.x
        except TypeError:
            raw = engine.predict(image)  # API v3.x
        return self._parse(raw)

    def recognize_lines(
        self, images: List[np.ndarray]
    ) -> List[List[OcrResult]]:
        """Reconoce recortes de una sola línea sin ejecutar el detector."""
        if not images:
            return []
        recognizer = self._ensure_recognizer()
        if recognizer is False:
            return self.recognize_batch(images)
        prepared = [self._to_bgr(img) for img in images]
        try:
            raw = list(recognizer.predict(prepared))
        except Exception:  # noqa: BLE001 - fallback robusto
            if self._rec_mkldnn:
                # Una CPU o version incompatible vuelve al mismo modelo sin
                # aceleracion. No se omite la lectura ni se cambia de modelo.
                self._rec_mkldnn = False
                self._recognizer = None
                return self.recognize_lines(images)
            logger.debug(
                "Reconocimiento sin detector falló; se usa el pipeline "
                "completo", exc_info=True,
            )
            return self.recognize_batch(images)
        if len(raw) != len(prepared):
            return self.recognize_batch(images)
        results: List[List[OcrResult]] = []
        for item in raw:
            text = item.get("rec_text") or ""
            score = item.get("rec_score") or 0.0
            results.append(
                [OcrResult(text=str(text), confidence=float(score))]
                if str(text).strip() else []
            )
        return results

    def recognize_batch(self, images: List[np.ndarray]) -> List[List[OcrResult]]:
        """Reconoce varias imágenes en una sola llamada (API v3.x).

        En API v2.x, o si la llamada en batch falla, se procesa una por una.
        """
        engine = self._ensure_engine()
        images = [self._to_bgr(img) for img in images]
        if not images:
            return []
        try:
            raw = engine.predict(images)
            parsed = [self._parse(item) for item in raw]
            if len(parsed) == len(images):
                return parsed
        except Exception:  # noqa: BLE001 - fallback robusto
            logger.debug("OCR en batch falló; se procesa imagen por imagen",
                         exc_info=True)
        return [self.recognize(img) for img in images]

    @staticmethod
    def _to_bgr(image: np.ndarray) -> np.ndarray:
        if image.ndim == 2:
            import cv2

            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        return image

    @staticmethod
    def _parse(raw) -> List[OcrResult]:
        """Normaliza la salida de PaddleOCR v2.x y v3.x."""
        if raw is None:
            return []

        results: List[OcrResult] = []

        # Formato v2.x: list[ list[ [box, (texto, conf)] ] ]
        try:
            for page_lines in raw:
                if page_lines is None:
                    continue
                for line in page_lines:
                    box, (text, conf) = line
                    results.append(
                        OcrResult(text=str(text), confidence=float(conf),
                                  box=box)
                    )
        except (TypeError, ValueError):
            results = []

        if results:
            return results

        # Formato v3.x: objetos OCRResult con atributos rec_texts/rec_scores
        try:
            first = raw[0] if isinstance(raw, (list, tuple)) else raw
            texts = getattr(first, "rec_texts", None) or first.get("rec_texts")
            scores = getattr(first, "rec_scores", None) or first.get("rec_scores")
            polys = getattr(first, "rec_polys", None) or first.get("rec_polys")
            if texts:
                for i, text in enumerate(texts):
                    conf = float(scores[i]) if scores and i < len(scores) else 0.0
                    box = polys[i] if polys and i < len(polys) else None
                    results.append(
                        OcrResult(text=str(text), confidence=conf, box=box)
                    )
        except (TypeError, ValueError, AttributeError):
            pass

        return results


def create_engine(name: str, lang: str = "en", **kwargs) -> OcrEngine:
    """Fábrica de motores OCR.

    Args:
        name: "paddle".
        lang: Idioma (por defecto "en").
        kwargs: Parámetros adicionales del motor.

    Returns:
        Una instancia de OcrEngine.

    Raises:
        ValueError: Si el motor no está registrado.
    """
    engines = {"paddle": PaddleOcrEngine}
    if name not in engines:
        raise ValueError(
            f"Motor OCR desconocido: {name}. "
            f"Disponibles: {', '.join(engines)}"
        )
    logger.info(f"Creando motor OCR: {name} (lang={lang})")
    return engines[name](lang=lang, **kwargs)
