"""Contador de páginas de los mensajes de avance.

Dos cosas iban mal en el texto que acompaña a la barra, y las dos nacen de
quién pone las cifras.

El total era el del documento abierto, no el del batch: en una ejecución de tres
bitácoras de 100 páginas, la página 52 de la segunda se anunciaba como "52 de
100" cuando el usuario está mirando la 152 de 300. El Pipeline no puede
arreglarlo por su cuenta (solo ve el PDF que tiene abierto), así que la etapa
de lectura de páginas viaja sin cifras (``PAGES_STAGE``) y el contador lo pone
quien conoce el par global, que es la misma capa que dibuja la barra. Así el
texto y la barra no pueden contar cosas distintas.

Y el número se devolvía: la etapa nombraba la última página en llegar, pero
con un proceso por hilo lógico hay una docena de páginas en vuelo y terminan
desordenadas, así que el texto iba 52, 48, 53… El contador cuenta páginas
terminadas, que es una cifra que solo puede subir.
"""

from __future__ import annotations

# Etiqueta de la etapa que lee páginas. El contador se añade al final, de
# modo que el prefijo del archivo en curso sigue quedando delante.
PAGES_STAGE = "Procesando páginas"

# Segunda vuelta de un documento: busca la marca VOID en las hojas con una
# posible discrepancia de firmas. Cuenta hojas de esa etapa y no páginas del
# documento, así que las capas de avance la llevan aparte: sumarla como
# páginas hacía que la fila del archivo volviera a empezar con menos páginas
# y que el tiempo restante cayera a cero con la etapa todavía en marcha.
VOID_STAGE = "Comprobando marcas VOID en hojas sin firma"


def with_page_counter(done: int, total: int, message: str) -> str:
    """Cierra la etapa de páginas con el ``hechas/total`` del batch.

    Los demás mensajes (calibración, generación del reporte, revisión de
    firmas) hablan de un archivo concreto y pasan intactos.
    """
    if total <= 0 or not message.endswith(PAGES_STAGE):
        return message
    return f"{message} {done}/{total}"
