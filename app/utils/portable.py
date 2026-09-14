"""Helpers de portabilidad: mantienen TODO dentro de la carpeta del proyecto.

La aplicación debe funcionar copiando la carpeta completa a cualquier
máquina Windows, sin instalar nada ni tocar el perfil del usuario. Este
módulo redirige los caches que por defecto escriben fuera de la carpeta:
modelos PaddleOCR/paddlex y variables de red. Tambien
conecta TLS al almacen de certificados confiables de Windows.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.utils.no_console import suppress_child_consoles


_TLS_DEL_SISTEMA_ACTIVO = False


def app_root() -> Path:
    """Raíz del proyecto (BITS/)."""
    return Path(__file__).resolve().parents[2]


def usar_certificados_de_windows() -> bool:
    """Hace que Python confie en el mismo almacen TLS que Edge.

    ``requests`` trae su propio lote de autoridades publicas mediante
    ``certifi``. En una red con inspeccion TLS eso no alcanza: el proxy firma
    el sitio con una CA corporativa que Windows conoce, pero certifi no. El
    puente de ``truststore`` conserva la validacion completa y delega la
    confianza a CryptoAPI, incluido el almacen administrado por la empresa.

    Se llama antes de importar los clientes HTTP. La operacion es global al
    proceso e idempotente; fuera de Windows no hace falta cambiar nada.
    """
    global _TLS_DEL_SISTEMA_ACTIVO
    if os.name != "nt":
        return False
    if _TLS_DEL_SISTEMA_ACTIVO:
        return True

    import truststore

    truststore.inject_into_ssl()
    _TLS_DEL_SISTEMA_ACTIVO = True
    return True


def ensure_portable_env() -> Path:
    """Fija el entorno portable y devuelve la raíz del proyecto.

    Debe llamarse lo antes posible en cada punto de entrada, ANTES de
    importar paddleocr/paddlex, porque esos módulos leen las variables
    al momento del import. Configura:

    - PADDLE_PDX_CACHE_HOME  -> <raíz>/portable/paddlex
      (los modelos OCR se descargan/leen desde la carpeta del proyecto,
       nunca de ~/.paddlex del usuario).
    - PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1
      (omite el chequeo de conectividad a los servidores de modelos).
    - PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0
      (evita el bug de oneDNN en Windows: ConvertPirAttribute2Runtime...).
    - TLS -> almacen de certificados de Windows mediante truststore.

    También instala la supresión de consolas de los subprocesos: la GUI
    corre sin consola y cualquier dependencia que llame a ``subprocess``
    haría aparecer una ventana negra (ver ``app.utils.no_console``).
    """
    suppress_child_consoles()
    usar_certificados_de_windows()
    root = app_root()
    os.environ.setdefault(
        "PADDLE_PDX_CACHE_HOME", str(root / "portable" / "paddlex")
    )
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "1")
    os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    return root
