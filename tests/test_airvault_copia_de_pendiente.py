"""Antes de subir un batch sin ID se busca en Web Index la misma carga con otro titulo.

Alguien puede haberla subido ya por la web (AirVault le pone «-Batch» al
titulo) o desde otro equipo (queda como Empty-Batch con su nombre dentro).
La busqueda por nombre no la ve, y el archivo se subia otra vez.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.airvault.copias_en_index import copias_de_la_carga
from app.airvault.flujo import (CopiaEnLaCola, Trabajo, subir_partes,
                                ya_esta_en_airvault)
from app.airvault.model import Etapa
from tests.airvault_fake import ClienteFalso, lote
from tests.test_airvault_copias_en_index import NOMBRE, PAGINAS, contenido, trabajo


def pendiente(raiz, subido=True):
    """Un batch de la barra que no tiene ID: nunca aparecio en Web Index."""
    t = trabajo(raiz, batch_id=None, verificado=False)
    if not subido:
        t.manifiesto.etapas["subir"] = Etapa()
        t.guardar()
    return t


def test_la_carga_subida_por_la_web_es_la_misma(tmp_path):
    t = pendiente(tmp_path)
    cola = [lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={"003TJF": contenido()})

    iguales, dudosos = copias_de_la_carga(t.manifiesto, cola, cliente)

    assert [c.batch_id for c in iguales] == ["003TJF"] and dudosos == []
    # Se lee sin tomarla.
    assert not cliente.abiertos and not cliente.escrituras


def test_el_empty_batch_con_su_nombre_dentro_es_la_misma(tmp_path):
    t = pendiente(tmp_path)
    cola = [lote("003TJK", "Empty-Batch", PAGINAS)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={"003TJK": contenido(logs=False)})

    iguales, _dudosos = copias_de_la_carga(t.manifiesto, cola, cliente)

    assert [c.batch_id for c in iguales] == ["003TJK"]


@pytest.mark.parametrize("cola, paginas", [
    # Otro contenido con el mismo titulo.
    ([lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)], {"003TJF": contenido(desde=2400000)}),
    # Otras paginas.
    ([lote("003TJF", f"{NOMBRE}-Batch", PAGINAS + 7)], {"003TJF": contenido(cantidad=PAGINAS + 7)}),
    # Un Empty-Batch de otra carga.
    ([lote("003TJK", "Empty-Batch", PAGINAS)], {"003TJK": contenido(nombre="DP | OTRO")}),
    # El titulo exacto lo resuelve la identificacion de siempre.
    ([lote("003TIH", NOMBRE, PAGINAS)], {"003TIH": contenido()}),
], ids=["otro_contenido", "otras_paginas", "empty_ajeno", "titulo_exacto"])
def test_lo_que_no_es_esta_carga(tmp_path, cola, paginas):
    t = pendiente(tmp_path)
    cliente = ClienteFalso(lotes=cola, paginas_por_lote=paginas)

    assert copias_de_la_carga(t.manifiesto, cola, cliente) == ([], [])


def test_lo_que_no_se_puede_leer_queda_en_duda(tmp_path):
    t = pendiente(tmp_path)
    sin_leer = lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)
    abierto = lote("003TJG", f"{NOMBRE}-Batch", PAGINAS, bloqueado_por="Otra Persona")
    cola = [sin_leer, abierto]
    cliente = ClienteFalso(
        lotes=cola, paginas_por_lote={"003TJF": contenido(nombre="", logs=False)},
    )

    iguales, dudosos = copias_de_la_carga(t.manifiesto, cola, cliente)

    assert iguales == [] and [c.batch_id for c in dudosos] == ["003TJF", "003TJG"]


def test_la_orden_de_subir_toma_la_copia_en_vez_de_subir_otra(tmp_path):
    t = pendiente(tmp_path)
    cola = [lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={"003TJF": contenido()})

    assert ya_esta_en_airvault(t, cliente).batch_id == "003TJF"


def test_la_copia_que_no_se_puede_comprobar_detiene_la_orden_y_dice_que_hacer(tmp_path):
    t = pendiente(tmp_path)
    cola = [lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)]
    cliente = ClienteFalso(
        lotes=cola, paginas_por_lote={"003TJF": contenido(nombre="", logs=False)},
    )

    with pytest.raises(CopiaEnLaCola) as error:
        ya_esta_en_airvault(t, cliente)

    texto = str(error.value)
    assert "003TJF" in texto and f"«{NOMBRE}-Batch»" in texto
    assert f"póngale en Web Index el título «{NOMBRE}»" in texto


def test_dos_copias_iguales_no_se_eligen(tmp_path):
    t = pendiente(tmp_path)
    cola = [
        lote("003TJF", f"{NOMBRE}-Batch", PAGINAS),
        lote("003TJK", "Empty-Batch", PAGINAS),
    ]
    cliente = ClienteFalso(
        lotes=cola,
        paginas_por_lote={"003TJF": contenido(), "003TJK": contenido()},
    )

    with pytest.raises(CopiaEnLaCola, match="2 copias"):
        ya_esta_en_airvault(t, cliente)


def test_la_subida_automatica_toma_la_copia_y_no_sube_el_archivo(tmp_path, monkeypatch):
    t = pendiente(tmp_path, subido=False)
    cola = [lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={"003TJF": contenido()})
    monkeypatch.setattr(
        Trabajo, "subir", lambda *_a, **_k: pytest.fail("Ya estaba en Web Index"),
    )
    avisos = []

    fallos = subir_partes(
        [t], SimpleNamespace(cancelada=False), cliente=cliente,
        avisar=lambda texto, *_a: avisos.append(texto), dormir=lambda _s: None,
    )

    assert fallos == []
    assert t.manifiesto.batch_id == "003TJF"
    # Le devuelve su titulo y lo dice en el registro.
    assert cliente.renombrados == [("003TJF", NOMBRE)]
    assert any("Web Index ya lo tiene como 003TJF" in aviso for aviso in avisos)
    assert t.manifiesto.eventos("deteccion", "encontrado")


def test_la_subida_automatica_no_sube_si_hay_una_copia_en_duda(tmp_path, monkeypatch):
    t = pendiente(tmp_path, subido=False)
    cola = [lote("003TJF", f"{NOMBRE}-Batch", PAGINAS, bloqueado_por="Otra Persona")]
    cliente = ClienteFalso(lotes=cola)
    monkeypatch.setattr(
        Trabajo, "subir", lambda *_a, **_k: pytest.fail("Podia ser la misma carga"),
    )
    avisos = []

    fallos = subir_partes(
        [t], SimpleNamespace(cancelada=False), cliente=cliente,
        avisar=lambda texto, *_a: avisos.append(texto), dormir=lambda _s: None,
    )

    (fallido, motivo), = fallos
    assert fallido is t and "003TJF" in motivo
    assert not t.manifiesto.batch_id
    assert any("003TJF" in aviso and "no se puede comprobar" in aviso for aviso in avisos)
