"""Integración de la ventana de la aplicación con el shell de Windows."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from app.branding import APPLICATION_USER_MODEL_PREFIX


WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXICON = 11
SM_CYICON = 12
SM_CXSMICON = 49
SM_CYSMICON = 50
GCLP_HICON = -14
GCLP_HICONSM = -34
DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWM_WINDOW_CORNER_PREFERENCE_ROUND = 2
DWM_SYSTEMBACKDROP_TYPE_MAINWINDOW = 2


def set_windows_process_taskbar_identity() -> str | None:
    """Da a esta instancia de la GUI un grupo propio en la barra de tareas.

    Windows agrupa por ``AppUserModelID`` las ventanas de procesos que nacen
    del mismo ejecutable. El PID hace distinta cada GUI principal que esté
    abierta a la vez; las ventanas secundarias del proceso heredan el mismo
    identificador y permanecen agrupadas con su ventana principal.

    Debe llamarse antes de crear ``QApplication``.
    """
    if sys.platform != "win32":
        return None
    app_id = f"{APPLICATION_USER_MODEL_PREFIX}.Instance.{os.getpid()}"
    try:
        import ctypes
        from ctypes import wintypes

        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = (wintypes.LPCWSTR,)
        setter.restype = ctypes.c_long
        if setter(app_id) < 0:
            return None
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return app_id


def set_windows_native_window_style(window: Any, oscuro: bool = True) -> bool:
    """Activa el marco, las esquinas y el fondo nativos de Windows.

    Son atributos de DWM y no sustituyen ningun control Qt. En versiones de
    Windows que no los conocen, cada atributo se omite sin impedir que la
    ventana se abra.

    ``oscuro`` es lo unico que decide el tema aqui: pinta la barra de titulo,
    que es la unica parte de la ventana que no dibuja Qt. Sin este parametro,
    la aplicacion en tema claro se quedaba con una barra de titulo negra sobre
    una ventana blanca.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
        setter.argtypes = (
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        setter.restype = ctypes.c_long
        hwnd = wintypes.HWND(int(window.winId()))

        def set_int(attribute: int, value: int) -> bool:
            native_value = ctypes.c_int(value)
            return setter(
                hwnd,
                attribute,
                ctypes.byref(native_value),
                ctypes.sizeof(native_value),
            ) == 0

        modo = 1 if oscuro else 0
        dark = set_int(DWMWA_USE_IMMERSIVE_DARK_MODE, modo)
        if not dark:
            dark = set_int(DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1, modo)
        rounded = set_int(
            DWMWA_WINDOW_CORNER_PREFERENCE,
            DWM_WINDOW_CORNER_PREFERENCE_ROUND,
        )
        backdrop = set_int(
            DWMWA_SYSTEMBACKDROP_TYPE,
            DWM_SYSTEMBACKDROP_TYPE_MAINWINDOW,
        )
        return dark or rounded or backdrop
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def set_windows_taskbar_icon(
    window: Any,
    icon_path: Path | str,
) -> bool:
    """Instala el ICO en la ventana nativa que consulta la barra de tareas.

    ``QApplication.setWindowIcon`` sigue siendo el mecanismo portable. Este
    refuerzo se ejecuta después de ``show()`` y envía ``WM_SETICON`` con ambos
    tamaños al HWND real. La identidad del grupo se fija por separado, antes
    de crear ``QApplication``; reafirmar aquí el icono evita que el grupo use
    el icono genérico del intérprete portable.
    """
    path = Path(icon_path)
    if sys.platform != "win32" or path.suffix.lower() != ".ico":
        return False
    if not path.is_file():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        load_image = user32.LoadImageW
        load_image.argtypes = (
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        load_image.restype = ctypes.c_void_p
        send_message = user32.SendMessageW
        send_message.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            ctypes.c_ssize_t,
        )
        send_message.restype = ctypes.c_ssize_t
        set_class_icon = user32.SetClassLongPtrW
        set_class_icon.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_void_p,
        )
        set_class_icon.restype = ctypes.c_void_p
        hwnd = int(window.winId())

        def load(metric_x: int, metric_y: int) -> int:
            width = user32.GetSystemMetrics(metric_x)
            height = user32.GetSystemMetrics(metric_y)
            return int(load_image(
                None,
                str(path.resolve()),
                IMAGE_ICON,
                width,
                height,
                LR_LOADFROMFILE,
            ) or 0)

        big = load(SM_CXICON, SM_CYICON)
        small = load(SM_CXSMICON, SM_CYSMICON)
        if not big or not small:
            return False
        send_message(hwnd, WM_SETICON, ICON_BIG, big)
        send_message(hwnd, WM_SETICON, ICON_SMALL, small)
        # Windows 11 puede consultar el icono de la clase en lugar de enviar
        # WM_GETICON cuando crea o reagrupa el botón de la barra de tareas.
        # Se actualizan ambos orígenes con los mismos handles.
        set_class_icon(hwnd, GCLP_HICON, big)
        set_class_icon(hwnd, GCLP_HICONSM, small)
        # Los HICON deben permanecer válidos durante toda la vida del HWND.
        # El proceso los libera al cerrarse; dos handles por ventana son
        # preferibles a destruirlos mientras Windows aún puede consultarlos.
        window._bits_native_icon_handles = (big, small)
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    return True
