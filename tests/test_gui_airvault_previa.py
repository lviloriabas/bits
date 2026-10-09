"""Los cuadros de la vista previa: los batches y las páginas de cada uno.

No tocan la red ni el disco: reciben lo que el módulo ya calculó y solo
tienen que enseñarlo entero. Lo que se comprueba es que no se pierda ni se
invente ninguna fila, que la lista de un batch enseñe sus páginas con los
valores que van a quedar escritos en AirVault, que los separadores ocupen su
fila sin pasar por bitácoras y que elegir un batch lleve a su lista.
"""

from __future__ import annotations

from app.airvault.flujo import BatchPrevisto
from app.airvault.model import EstadoRegistro, Registro
from app.gui.airvault_previa import BitacorasDelBatch, VistaPreviaBatches


def bitacora(seq, matricula="HP-1848CMP", log="2271620", **extra):
    return Registro(
        seq=seq,
        archivo_origen="paginas.pdf",
        pagina_origen=seq,
        matricula=matricula,
        log_number=log,
        flight_number="703",
        fecha="2026/08/11",
        **extra,
    )


def separador(seq, etiqueta="HP-1848CMP"):
    return Registro(seq=seq, separador=etiqueta)


def previsto(nombre="DP | BITS", registros=None, **extra):
    return BatchPrevisto(
        nombre=nombre,
        parte=1,
        partes=1,
        revisar=False,
        pdf="entrega.pdf",
        registros=list(registros or []),
        **extra,
    )


# ── la lista de bitácoras de un batch ──────────────────────────────

# Columnas de la lista de páginas de un batch, por si cambian de sitio.
PAGINA, ORIGEN, MATRICULA, LOG_PAGE = 0, 1, 2, 3
VUELO, END_DATE, ESTADO, AVISOS = 4, 5, 6, 7


def test_los_separadores_ocupan_su_fila_sin_ser_bitacoras(app):
    """Corren la numeración del batch: sin ellos la página no cuadra."""
    registros = [separador(1), bitacora(2), bitacora(3, log="2271621")]

    cuadro = BitacorasDelBatch("DP | BITS", registros)

    assert cuadro.tabla.rowCount() == 3
    # La divisoria no es un documento: no lleva datos de índice, y lo único
    # suyo es la etiqueta con la que se imprimió.
    assert cuadro.tabla.item(0, MATRICULA).text() == "HP-1848CMP"
    assert cuadro.tabla.item(0, ORIGEN).text() == ""
    assert cuadro.tabla.item(0, LOG_PAGE).text() == ""
    assert cuadro.tabla.item(0, ESTADO).text() == "Separador"
    assert cuadro.tabla.item(1, LOG_PAGE).text() == "2271620"
    assert cuadro.tabla.item(2, LOG_PAGE).text() == "2271621"


def test_cada_pagina_dice_el_numero_que_ocupa_en_el_batch(app):
    """Es el número con el que se la busca en Web Index."""
    registros = [separador(1), bitacora(2), bitacora(3, log="2271621")]

    cuadro = BitacorasDelBatch("DP | BITS", registros)

    assert cuadro.tabla.item(0, PAGINA).text() == "1"
    assert cuadro.tabla.item(1, PAGINA).text() == "2"
    assert cuadro.tabla.item(2, PAGINA).text() == "3"
    assert cuadro.tabla.item(1, ORIGEN).text() == "paginas.pdf, p. 2"


def test_las_columnas_son_los_campos_importantes(app):
    """Los que el programa marca, no todo lo que lleva el registro.

    Lo que se le escribe a la página además de esto (Doc Type, Fleet,
    Lessor, Audit Status) está en el reporte de revisión: aquí sobra.
    """
    cuadro = BitacorasDelBatch("DP | BITS", [bitacora(1, fleet="737")])

    assert cuadro.COLUMNAS == (
        "Página", "Origen", "Matrícula", "Log Page", "Vuelo", "End Date",
        "Estado", "Avisos",
    )
    assert cuadro.tabla.item(0, MATRICULA).text() == "HP-1848CMP"
    assert cuadro.tabla.item(0, VUELO).text() == "703"
    # La fecha sale en el formato de AirVault, no en el del CSV.
    assert cuadro.tabla.item(0, END_DATE).text() == "08/11/2026"


def test_el_aviso_no_tapa_el_estado_de_la_pagina(app):
    """Son dos preguntas: cómo quedó y qué le impide escribirse."""
    registros = [
        bitacora(1),
        bitacora(2, log="2271621", avisos=["sin matricula"]),
        bitacora(3, log="2271622", estado=EstadoRegistro.ESCRITA),
        bitacora(4, log="2271623", duplicado=True, discrepancia=True),
    ]

    cuadro = BitacorasDelBatch("DP | BITS", registros)

    assert cuadro.tabla.item(0, ESTADO).text() == "Por indexar"
    assert cuadro.tabla.item(0, AVISOS).text() == ""
    assert cuadro.tabla.item(1, ESTADO).text() == "Por indexar"
    assert cuadro.tabla.item(1, AVISOS).text() == "Falta la matrícula."
    assert cuadro.tabla.item(2, ESTADO).text() == "Indexada"
    assert cuadro.tabla.item(3, AVISOS).text() == "duplicada; discrepancia"


def test_un_batch_completado_dice_que_sus_bitacoras_lo_estan(app):
    """Cerrado el batch, «indexada» se queda corto: ya no queda nada."""
    registros = [
        bitacora(1, estado=EstadoRegistro.ESCRITA),
        bitacora(2, log="2271621", estado=EstadoRegistro.OMITIDA),
    ]

    cuadro = BitacorasDelBatch("DP | BITS", registros, completado=True)

    assert cuadro.tabla.item(0, ESTADO).text() == "Completada"
    # Lo que no se escribió no se completó con el batch.
    assert cuadro.tabla.item(1, ESTADO).text() == "Omitida"


# ── la vista previa de los batches ─────────────────────────────────

def test_una_fila_por_batch_con_sus_cuentas(app):
    previstos = [
        previsto("DP | BITS -1", [separador(1), bitacora(2), bitacora(3)]),
        previsto("DP | BITS -2", [bitacora(1)]),
    ]

    cuadro = VistaPreviaBatches(previstos)

    assert cuadro.tabla.rowCount() == 2
    assert cuadro.tabla.item(0, 0).text() == "DP | BITS -1"
    assert cuadro.tabla.item(0, 1).text() == "3", "páginas, separador incluido"
    assert cuadro.tabla.item(0, 2).text() == "2", "bitácoras, separador aparte"


def test_el_que_todavia_no_existe_dice_que_esta_por_subir(app):
    previstos = [
        previsto("DP | BITS -1", [bitacora(1)]),
        previsto(
            "DP | BITS -2", [bitacora(1)],
            existe=True, subido=True, estado="Indexado: 8 de 8",
        ),
    ]

    cuadro = VistaPreviaBatches(previstos)

    assert cuadro.tabla.item(0, 3).text() == "Por subir"
    assert cuadro.tabla.item(1, 3).text() == "Indexado: 8 de 8"


def test_elegir_un_batch_habilita_ver_sus_bitacoras(app):
    cuadro = VistaPreviaBatches([previsto("DP | BITS", [bitacora(1)])])

    # Se abre con el primero elegido: no hay que adivinar que hay que
    # pulsar una fila antes de que el botón sirva para algo.
    assert cuadro.tabla.currentRow() == 0
    assert cuadro.boton_bitacoras.isEnabled()
    assert cuadro._elegido().nombre == "DP | BITS"


def test_sin_batches_no_hay_nada_que_abrir(app):
    cuadro = VistaPreviaBatches([])

    assert cuadro.tabla.rowCount() == 0
    assert not cuadro.boton_bitacoras.isEnabled()
