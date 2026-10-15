"""Configuración de Loguru para toda la aplicación."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    rotation: str = "1 day",
    retention: int = 7,
) -> None:
    """Configura Loguru: consola + archivo rotativo.

    Args:
        log_dir: Directorio para los archivos de log.
        level: Nivel mínimo (DEBUG, INFO, WARNING, ERROR).
        rotation: Tamaño o tiempo de rotación ("1 day", "10 MB").
        retention: Días de retención de logs antiguos.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    # pythonw no tiene consola y deja stderr en None.
    if sys.stderr is not None:
        logger.add(
            sys.stderr,
            level=level,
            format=("<green>{time:HH:mm:ss}</green> | "
                    "<level>{level: <8}</level> | "
                    "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
                    "{message}"),
        )
    logger.add(
        log_dir / "app_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True,
    )
