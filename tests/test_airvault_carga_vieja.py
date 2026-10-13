"""Una carga que lleva rato sin aparecer no se queda en «Páginas no coinciden»."""

from datetime import datetime, timedelta

import pytest

from app.airvault.config import AirVaultConfig
from app.airvault.discovery import LoteNoEncontrado
from app.airvault.flujo import (
    DESCUADRADO,
    NO_ENCONTRADO,
    Trabajo,
    comprobar_partes,
)
from app.airvault.model import EstadoEtapa
from tests.airvault_fake import lote
from tests.test_airvault_comprobar import corrida, trabajo_subido


def envejecer(trabajo, dias=11):
    """Deja la subida con fecha de hace dias, como una que nadie publico."""
    trabajo.manifiesto.etapa("subir").actualizada = (
        datetime.now() - timedelta(days=dias)
    ).isoformat(timespec="seconds")
    trabajo.guardar()


def test_un_batch_ajeno_sin_titulo_no_descuadra_una_carga_vieja(tmp_path):
    """Tras la foto de la cola llegaron las cargas de todo el mundo."""
    trabajo, cliente = trabajo_subido(tmp_path)
    envejecer(trabajo)
    cliente.lotes = [lote("003OTRO", "Empty-Batch", 300)]

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == NO_ENCONTRADO
    assert parte.se_puede_subir
    assert trabajo.manifiesto.no_encontrado_desde
    assert cliente.renombrados == []


def test_recien_subida_el_mismo_batch_si_la_detiene(tmp_path):
    """Mientras AirVault puede estar armandola, la foto de la cola vale."""
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = [lote("003OTRO", "Empty-Batch", 300)]

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == DESCUADRADO


def test_un_batch_con_su_titulo_y_otras_paginas_sigue_descuadrado(tmp_path):
    """Ese si es suyo: resubirlo dejaria dos con el mismo titulo."""
    trabajo, cliente = trabajo_subido(tmp_path)
    envejecer(trabajo)
    cliente.lotes = [lote("003JUNTO", trabajo.manifiesto.nombre_batch, 200)]

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == DESCUADRADO
    assert not trabajo.manifiesto.no_encontrado_desde


def test_la_suma_de_dos_cargas_perdidas_sigue_pareciendo_una_mezcla(tmp_path):
    """Si las paginas cuadran con una mezcla, se conserva para demostrarla."""
    primera, cliente = trabajo_subido(tmp_path)
    csv = corrida(tmp_path, "BITS 19 AUG 2026 06 10")
    segunda = Trabajo.preparar(
        AirVaultConfig(), tmp_path / "otro", csv, "DP | BIT 19 AUG 2026 06 10"
    )
    segunda.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "entrega.pdf")
    segunda.manifiesto.lotes_previos = ["003VIEJO"]
    for trabajo in (primera, segunda):
        envejecer(trabajo)
    juntas = len(primera.manifiesto.registros) + len(segunda.manifiesto.registros)
    cliente.lotes = [lote("003MEZCLA", "Empty-Batch", juntas)]

    estados = comprobar_partes([primera, segunda], cliente)

    assert [parte.estado for parte in estados] == [DESCUADRADO, DESCUADRADO]

    # Otra cantidad no es la suma de nada nuestro: es de otra persona.
    cliente.lotes = [lote("003AJENO", "Empty-Batch", juntas + 7)]
    estados = comprobar_partes([primera, segunda], cliente)
    assert [parte.estado for parte in estados] == [NO_ENCONTRADO, NO_ENCONTRADO]


def test_al_esperarla_una_carga_vieja_no_frena_a_las_siguientes(tmp_path):
    """Quedaba «en camino» para siempre por cualquier Empty-Batch de la cola."""
    trabajo, cliente = trabajo_subido(tmp_path)
    envejecer(trabajo)
    cliente.lotes = [lote("003OTRO", "Empty-Batch", 300)]

    with pytest.raises(LoteNoEncontrado):
        trabajo.descubrir(cliente, esperar=True, dormir=lambda _s: None, limite_s=0)

    assert trabajo.manifiesto.no_encontrado_desde
