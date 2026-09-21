"""Memoria de las opciones de la interfaz: un control, una clave.

Cada casilla, desplegable o contador que alguien mueve es una eleccion suya
y tiene que seguir ahi en el siguiente arranque. Hasta ahora cada ventana
nacia con el valor escrito en el codigo y lo que se hubiera elegido se
perdia al cerrar, asi que el mismo ajuste habia que rehacerlo en cada sesion.

La pieza que lo evita es :func:`recordar`: recibe la clave y el control, lo
deja en lo ultimo guardado y conecta su senal para anotar cada cambio. Es
una sola llamada al lado del control, que es donde se lee que esa opcion
tiene memoria; repartir un ``leer`` en el constructor y un ``guardar`` en un
metodo lejano era como se perdian.

Todo va a ``interfaz.json``, junto al programa y fuera del repositorio (ver
:mod:`app.utils.preferencias_ui`). Las opciones del indexado, que ya tenian
memoria, siguen en ``airvault.json``: son las mismas reglas y el mismo tipo
de archivo local, y moverlas ahora le borraria a cada instalacion lo que ya
tiene elegido.

Restaurar no es elegir: el valor guardado se pone con la senal bloqueada,
para que abrir una ventana no dispare lo que hace el control al moverse (ni
reescriba el archivo, ni rehaga un CSV, ni vuelva a pintar la vista previa).
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QSignalBlocker
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QAbstractButton, QComboBox, QSpinBox

from app.utils.preferencias_ui import guardar_opcion, leer_opcion

# Prefijos por ventana. Un archivo plano con la ventana delante se lee de
# corrido y no deja dos «mostrar_campos» distintos pisandose.
PRINCIPAL = "principal"
SALIDA = "salida"
AIRVAULT = "airvault"
VISOR = "visor"
WEB_REPORTS = "web_reports"
# El menu «Discrepancias a detectar» guarda una lista por plantilla y no una
# casilla suelta, asi que no pasa por aqui: su seccion y su lectura estan en
# :mod:`app.validation.discrepancias`, que es quien las aplica al clasificar
# y no puede importar la interfaz.


def recordar(
    seccion: str,
    nombre: str,
    control: Any,
    *,
    al_restaurar: Callable[[], None] | None = None,
) -> str:
    """Deja el control en lo ultimo elegido y anota lo que se elija.

    El valor de partida es el que el control ya trae puesto: quien nunca
    toco la opcion la encuentra como estaba, y no se escribe nada hasta que
    la mueva.

    ``al_restaurar`` corre despues de reponer el valor, para lo que el
    control arrastra consigo (habilitar a su vecino, ajustar una fila) y que
    no puede salir de su propia senal porque va bloqueada.

    Devuelve la clave, que es lo que las pruebas miran en el archivo.
    """
    llave = f"{seccion}.{nombre}"
    if isinstance(control, QComboBox):
        _recordar_combo(llave, control)
    elif isinstance(control, QSpinBox):
        _recordar_spin(llave, control)
    elif isinstance(control, (QAbstractButton, QAction)):
        _recordar_marca(llave, control)
    else:
        raise TypeError(f"«{llave}»: no se sabe recordar {type(control)}")
    if al_restaurar is not None:
        al_restaurar()
    return llave


def _recordar_marca(nombre: str, control: QAbstractButton | QAction) -> None:
    """Casillas y entradas de menu marcables, que son casi todas."""
    guardado = leer_opcion(nombre, control.isChecked())
    with QSignalBlocker(control):
        control.setChecked(bool(guardado))
    control.toggled.connect(
        lambda marcado, nombre=nombre: guardar_opcion(nombre, bool(marcado))
    )


def _recordar_spin(nombre: str, control: QSpinBox) -> None:
    """Contadores; el rango manda, un valor fuera de el se ignora."""
    guardado = leer_opcion(nombre, control.value())
    try:
        valor = int(guardado)
    except (TypeError, ValueError):
        valor = control.value()
    if control.minimum() <= valor <= control.maximum():
        with QSignalBlocker(control):
            control.setValue(valor)
    control.valueChanged.connect(
        lambda cantidad, nombre=nombre: guardar_opcion(nombre, int(cantidad))
    )


def _recordar_combo(nombre: str, control: QComboBox) -> None:
    """Desplegables, guardados por el texto de la opcion y no por su sitio.

    El indice cambia en cuanto se agrega o se reordena una opcion, y el dato
    de la opcion no siempre se puede escribir en un JSON (hay tuplas y
    rutas). El texto es lo que la persona eligio, se lee en el archivo y, si
    esa opcion ya no existe, el desplegable abre donde abria antes.
    """
    guardado = leer_opcion(nombre)
    if isinstance(guardado, str) and guardado:
        indice = control.findText(guardado)
        if indice >= 0:
            with QSignalBlocker(control):
                control.setCurrentIndex(indice)
    control.currentIndexChanged.connect(
        lambda _indice, nombre=nombre, control=control:
        guardar_opcion(nombre, control.currentText())
    )
