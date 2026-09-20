"""Buscar una bitácora entre todos los batches de la cola.

El buscador de la ventana de AirVault responde una sola pregunta: en qué
batch está la bitácora. La respuesta puede ser más de uno, y esa es la
parte que importa: una bitácora dudosa viaja en su parte y en el batch
REVISAR, y una ejecución subida dos veces la deja en los dos batches.
Aquí se comprueba que se encuentre en todos, que la línea de debajo de la
tabla los nombre y que la tabla los deje resaltados a la vez.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.airvault.model import Registro
from app.gui.airvault_busqueda import (
    Hallazgo,
    buscar_en_la_cola,
    frase_de,
    valores_de,
)


def _bitacora(seq: int, log: str = "", **campos) -> Registro:
    datos = {
        "seq": seq,
        "pagina_batch": seq,
        "archivo_origen": "paginas.pdf",
        "pagina_origen": seq,
        "matricula": "HP-1830CMP",
        "log_number": log or str(2271000 + seq),
        "flight_number": "703",
        "fecha": "2026/08/11",
    }
    datos.update(campos)
    return Registro(**datos)


def _separador(seq: int, etiqueta: str = "REVISAR") -> Registro:
    return Registro(seq=seq, pagina_batch=seq, separador=etiqueta)


# ── encontrarla en todos los batches que la llevan ─────────────────

def test_dice_en_que_batch_esta_la_bitacora():
    cola = [
        ("DP | BITS parte 1", [_bitacora(1), _bitacora(2, "2271042")]),
        ("DP | BITS parte 2", [_bitacora(1, "2271900")]),
    ]

    hallazgos = buscar_en_la_cola(cola, "2271042")

    assert hallazgos == [Hallazgo(0, "DP | BITS parte 1", (2,))]


def test_la_encuentra_en_varios_batches_a_la_vez():
    """Una bitácora dudosa va en su parte y en el batch REVISAR."""
    cola = [
        ("DP | BITS parte 1", [_bitacora(11), _bitacora(12, "2271042")]),
        ("DP | BITS parte 2", [_bitacora(1, "2271900")]),
        ("DP | BITS REVISAR", [_separador(1), _bitacora(2, "2271042")]),
    ]

    hallazgos = buscar_en_la_cola(cola, "2271042")

    assert [(h.fila, h.nombre, h.paginas) for h in hallazgos] == [
        (0, "DP | BITS parte 1", (12,)),
        (2, "DP | BITS REVISAR", (2,)),
    ]


def test_dice_la_pagina_del_batch_y_no_la_del_pdf_de_origen():
    """Es la que enseña Web Index, que es donde se va a mirar."""
    cola = [
        (
            "DP | BITS",
            [_bitacora(1, "2271042", pagina_batch=37, pagina_origen=4)],
        ),
    ]

    assert buscar_en_la_cola(cola, "2271042")[0].paginas == (37,)


def test_una_matricula_devuelve_todas_sus_paginas():
    cola = [("DP | BITS", [_bitacora(1), _bitacora(2), _bitacora(3)])]

    hallazgos = buscar_en_la_cola(cola, "hp-1830cmp")

    assert hallazgos[0].paginas == (1, 2, 3)


def test_las_separadoras_no_son_bitacoras():
    """Ocupan página en el batch, pero no hay nada que buscar en ellas."""
    cola = [("DP | BITS", [_separador(1, "POSIBLES DISCREPANCIAS")])]

    assert buscar_en_la_cola(cola, "REVISAR") == []
    assert buscar_en_la_cola(cola, "POSIBLES") == []


def test_se_busca_la_fecha_en_los_dos_formatos():
    """La del CSV y la que se le escribe a AirVault valen igual."""
    cola = [("DP | BITS", [_bitacora(1)])]

    assert buscar_en_la_cola(cola, "2026/08/11")
    assert buscar_en_la_cola(cola, "08/11/2026")


def test_la_coincidencia_exacta_manda():
    """Buscar un Log Page pregunta por esa bitácora, no por el archivo."""
    cola = [
        ("Archivo parecido", [_bitacora(1, archivo_origen="2271042 v2.pdf")]),
        ("La bitácora", [_bitacora(1, "2271042")]),
    ]

    hallazgos = buscar_en_la_cola(cola, "2271042")

    assert [h.nombre for h in hallazgos] == ["La bitácora"]


def test_sin_coincidencia_exacta_valen_las_parciales():
    cola = [("DP | BITS", [_bitacora(1, "2271042")])]

    assert buscar_en_la_cola(cola, "227104")[0].paginas == (1,)


def test_el_texto_vacio_no_busca_nada():
    cola = [("DP | BITS", [_bitacora(1)])]

    assert buscar_en_la_cola(cola, "   ") == []


def test_la_pagina_del_batch_no_es_un_dato_por_el_que_se_busque():
    """Buscar por ella devolvería la misma página de todos los batches."""
    registro = _bitacora(7, "2271042")

    assert "7" not in valores_de(registro)


# ── contarlo en una línea ──────────────────────────────────────────

def test_la_frase_nombra_el_batch_y_la_pagina():
    frase = frase_de("2271042", [Hallazgo(0, "DP | BITS", (12,))])

    assert frase == "«2271042»: en «DP | BITS», página 12."


def test_la_frase_dice_cuantos_batches_son_antes_de_nombrarlos():
    frase = frase_de(
        "2271042",
        [Hallazgo(0, "parte 1", (12,)), Hallazgo(2, "REVISAR", (3, 7))],
    )

    assert frase == (
        "«2271042»: en 2 batches a la vez - «parte 1» (p. 12) y "
        "«REVISAR» (pp. 3 y 7)."
    )


def test_la_frase_resume_cuando_son_demasiados():
    hallazgos = [Hallazgo(i, f"batch {i}", (1,)) for i in range(7)]

    frase = frase_de("HP-1830CMP", hallazgos)

    assert frase.startswith("«HP-1830CMP»: en 7 batches a la vez - ")
    assert "y otros 3." in frase


def test_la_frase_resume_las_paginas_de_un_batch_largo():
    hallazgo = Hallazgo(0, "DP | BITS", tuple(range(1, 12)))

    frase = frase_de("HP-1830CMP", [hallazgo])

    assert "páginas 1, 2, 3, 4, 5, 6 y otras 5." in frase


def test_la_frase_dice_que_no_esta_en_ninguno():
    assert frase_de("2271042", []) == (
        "«2271042»: no está en ninguna bitácora de la cola."
    )


# ── la ventana: resaltar los batches que la llevan ─────────────────

class _ManifiestoFalso:
    def __init__(self, nombre, registros):
        self.nombre_batch = nombre
        self.registros = registros
        self.batch_id = ""
        self.posible_duplicado = False


class _TrabajoFalso:
    def __init__(self, nombre, registros):
        self.manifiesto = _ManifiestoFalso(nombre, registros)
        self.carpeta = nombre


def _parte(nombre, registros):
    from app.airvault.flujo import SIN_SUBIR, EstadoParte

    return EstadoParte(_TrabajoFalso(nombre, registros), SIN_SUBIR)


@pytest.fixture
def ventana(app, tmp_path):
    """La ventana con tres batches en la cola, dos con la misma bitácora."""
    from app.gui.airvault_window import AirVaultWindow
    from app.gui.automatizacion import OpcionesAutomatizacion

    ventana = AirVaultWindow(
        Path(__file__).resolve().parents[1], OpcionesAutomatizacion(tmp_path)
    )
    ventana._estados = [
        _parte("DP | BITS parte 1", [_bitacora(1), _bitacora(2, "2271042")]),
        _parte("DP | BITS parte 2", [_bitacora(1, "2271900")]),
        _parte("DP | BITS REVISAR", [_bitacora(1, "2271042")]),
    ]
    ventana._pintar_lotes()
    return ventana


def _resaltadas(ventana) -> list[int]:
    seleccion = ventana.lotes.selectionModel()
    return sorted(indice.row() for indice in seleccion.selectedRows())


def test_la_ventana_resalta_todos_los_batches_que_la_llevan(ventana):
    ventana.buscar_bitacora_edit.setText("2271042")
    ventana._buscar_bitacora()

    assert _resaltadas(ventana) == [0, 2]
    respuesta = ventana.busqueda_bitacora.text()
    assert "2 batches a la vez" in respuesta
    assert "DP | BITS parte 1" in respuesta
    assert "DP | BITS REVISAR" in respuesta


def test_las_flechas_pasan_de_un_batch_al_siguiente(ventana):
    ventana.buscar_bitacora_edit.setText("2271042")
    ventana._buscar_bitacora()

    assert ventana.buscar_bitacora_siguiente.isEnabled()
    assert ventana.lotes.currentRow() == 0

    ventana._mover_hallazgo(1)

    assert ventana.lotes.currentRow() == 2
    # Y los dos siguen resaltados: la respuesta es que va en los dos.
    assert _resaltadas(ventana) == [0, 2]


def test_repetir_la_busqueda_pasa_al_batch_siguiente(ventana):
    ventana.buscar_bitacora_edit.setText("2271042")
    ventana._buscar_bitacora()
    ventana._buscar_bitacora()

    assert ventana.lotes.currentRow() == 2


def test_con_un_solo_batch_no_hay_a_donde_pasar(ventana):
    ventana.buscar_bitacora_edit.setText("2271900")
    ventana._buscar_bitacora()

    assert _resaltadas(ventana) == [1]
    assert not ventana.buscar_bitacora_siguiente.isEnabled()
    assert not ventana.buscar_bitacora_anterior.isEnabled()


def test_repintar_la_cola_devuelve_el_resaltado(ventana):
    """La tabla se rehace sola cada vez que cambia el estado de un batch."""
    ventana.buscar_bitacora_edit.setText("2271042")
    ventana._buscar_bitacora()

    ventana._pintar_lotes()

    assert _resaltadas(ventana) == [0, 2]


def test_repintar_la_cola_actualiza_la_respuesta(ventana):
    """Si llega otro batch con la misma bitácora, se dice al momento."""
    ventana.buscar_bitacora_edit.setText("2271042")
    ventana._buscar_bitacora()

    ventana._estados.append(
        _parte("DP | BITS parte 3", [_bitacora(1, "2271042")])
    )
    ventana._pintar_lotes()

    assert _resaltadas(ventana) == [0, 2, 3]
    assert "3 batches a la vez" in ventana.busqueda_bitacora.text()


def test_vaciar_la_busqueda_suelta_el_resaltado(ventana):
    ventana.buscar_bitacora_edit.setText("2271042")
    ventana._buscar_bitacora()

    ventana.buscar_bitacora_edit.clear()
    ventana._buscar_bitacora()

    assert _resaltadas(ventana) == []
    from app.gui.airvault_window import AYUDA_BUSCAR_BITACORA

    assert ventana.busqueda_bitacora.text() == AYUDA_BUSCAR_BITACORA


def test_una_bitacora_que_no_esta_lo_dice(ventana):
    ventana.buscar_bitacora_edit.setText("9999999")
    ventana._buscar_bitacora()

    assert _resaltadas(ventana) == []
    assert "no está en ninguna bitácora de la cola" in (
        ventana.busqueda_bitacora.text()
    )
