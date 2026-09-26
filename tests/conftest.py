"""Piezas comunes de todas las pruebas.

Pytest carga este archivo antes que cualquier modulo de prueba, asi que es el
unico sitio donde se puede elegir la plataforma de Qt: PySide6 la lee al
importarse y despues ya no cambia. Antes cada archivo de interfaz repetia la
misma linea antes de sus propios imports, con el riesgo de que uno la olvidara
y abriera ventanas de verdad en la pantalla de quien ejecuta la suite.

Aqui vive tambien la limpieza de ventanas. ``close()`` solo esconde: el arbol
de widgets sigue vivo y la aplicacion lo arrastra hasta el final de la sesion.
Con mil seiscientas pruebas eso llegaba a once mil widgets y setecientas
ventanas de primer nivel, y las dos pruebas que repintan la aplicacion entera
(el tema y la banda de seleccion) pasaban de centesimas a mas de un minuto
cada una porque Qt tenia que reestilar todo lo que quedo colgando.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _aplicacion():
    """La ``QApplication`` viva, o nada si la prueba no toco la interfaz.

    Se mira ``sys.modules`` en vez de importar: mil de las pruebas no abren
    ninguna ventana y no tienen por que cargar Qt para que las limpien.
    """
    widgets = sys.modules.get("PySide6.QtWidgets")
    return widgets.QApplication.instance() if widgets else None


@pytest.fixture(scope="session", autouse=True)
def _preferencias_portables_intactas():
    """Devuelve ``airvault.json`` como estaba antes de la sesion.

    Las preferencias de la interfaz (paginas por batch, politica de fecha)
    se guardan solas en el archivo portable del programa en cuanto alguien
    toca el control. Una prueba que mueve un desplegable cambiaria con que
    valor abre la aplicacion de verdad, asi que el contenido previo se
    apunta al empezar y se repone al terminar.
    """
    from pathlib import Path

    from app.airvault.config import AIRVAULT_FILENAME

    ruta = Path(__file__).resolve().parents[1] / AIRVAULT_FILENAME
    previo = ruta.read_text(encoding="utf-8") if ruta.is_file() else None

    yield

    if previo is None:
        ruta.unlink(missing_ok=True)
    elif ruta.read_text(encoding="utf-8") != previo:
        ruta.write_text(previo, encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def opciones_de_interfaz(tmp_path_factory):
    """Manda ``interfaz.json`` a un archivo de la sesion, no al de verdad.

    Casi cada control de la interfaz anota lo que se elige (ver
    :mod:`app.gui.memoria`), asi que una prueba que marca una casilla le
    cambiaria a quien ejecute el programa despues con que valores abre. Se
    devuelve la ruta para las pruebas que quieran leer lo escrito.
    """
    from pathlib import Path

    import app.utils.preferencias_ui as preferencias

    archivo = (
        tmp_path_factory.mktemp("interfaz") / preferencias.INTERFAZ_FILENAME
    )
    parche = pytest.MonkeyPatch()
    parche.setattr(
        preferencias,
        "ruta_preferencias",
        lambda raiz=None: (
            Path(raiz) / preferencias.INTERFAZ_FILENAME if raiz else archivo
        ),
    )
    yield archivo
    parche.undo()


@pytest.fixture(autouse=True)
def _opciones_sin_heredar(opciones_de_interfaz):
    """Cada prueba empieza sin preferencias, con los valores de fabrica.

    Sin esto, una prueba que apaga una casilla decide con que valor nace la
    ventana de la siguiente, y cual falla depende del orden en que corran.
    """
    opciones_de_interfaz.unlink(missing_ok=True)
    yield


@pytest.fixture(autouse=True)
def _reintentos_de_pagina_sin_espera(monkeypatch):
    """Una pagina que AirVault no acepta se repite tras unos segundos.

    Las pruebas simulan fallos que no se arreglan solos; esperar de verdad
    entre intentos solo alargaria la suite.
    """
    monkeypatch.setattr(
        "app.airvault.indexer.ESPERAS_REINTENTO_PAGINA", (0.0, 0.0)
    )


@pytest.fixture(scope="session")
def app():
    """La ``QApplication`` unica de la sesion.

    Qt no admite dos, asi que crear una por modulo era crear una y reutilizarla
    sin decirlo. Declararla de sesion lo deja escrito.
    """
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def soltar_hilos(ventana) -> None:
    """Para los hilos de la ventana antes de que Qt la destruya.

    La ventana principal lleva su propio ``QThread`` para la vista previa y
    lo suelta en ``_teardown``. Cerrarla no basta: si hay trabajo en curso el
    cierre se aplaza y ``_teardown`` no llega a correr, asi que destruirla
    despues aborta el proceso con 0xC0000409, que es justo lo que el cierre
    de la ventana esta escrito para evitar.
    """
    teardown = getattr(ventana, "_teardown", None)
    if teardown is None:
        return
    try:
        teardown()
    except RuntimeError:
        # El objeto C++ ya no esta; no queda nada que soltar.
        pass


@pytest.fixture(autouse=True)
def _ventanas_de_la_prueba():
    """Destruye las ventanas que la prueba deje abiertas.

    Se apunta lo que habia antes y se borra lo que aparecio, de forma que una
    prueba no se lleve por delante nada que otra tenga en pie. Es ``autouse``
    porque el olvido es justo lo que se quiere evitar: si hubiera que pedirlo,
    la ventana numero mil uno volveria a quedarse.
    """
    aplicacion = _aplicacion()
    previas = set()
    if aplicacion is not None:
        previas = {id(v) for v in aplicacion.topLevelWidgets()}

    yield

    aplicacion = _aplicacion()
    if aplicacion is None:
        return
    from PySide6.QtCore import QEvent

    for ventana in aplicacion.topLevelWidgets():
        if id(ventana) not in previas:
            soltar_hilos(ventana)
            ventana.deleteLater()
    aplicacion.processEvents()
    # ``processEvents`` no reparte los borrados aplazados: sin esto la ventana
    # queda marcada para morir pero sigue en pie hasta que haya un bucle real,
    # que en las pruebas no lo hay.
    aplicacion.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def window(app):
    """La ventana principal, cerrada al terminar la prueba.

    Se llama ``window`` porque asi la nombran las pruebas que la usan.
    """
    from app.gui.main_window import MainWindow

    ventana = MainWindow()
    yield ventana
    ventana.close()
    soltar_hilos(ventana)
    app.processEvents()
