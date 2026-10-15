"""Batches subidos dos veces: se reconocen en la cola entera de Web Index."""

from __future__ import annotations

import pytest

from app.airvault import copias_en_index as copias
from app.airvault.config import CAMPO_BATCH_NAME, CAMPO_LOG_NUMBER, AirVaultConfig
from app.airvault.copias_en_index import COMPLETADO, EN_INDEX, INDEXADO, revisar, titulo_base
from app.airvault.flujo import Trabajo
from app.airvault.model import EstadoEtapa, Manifiesto, Registro
from tests.airvault_fake import ClienteFalso, lote, pagina

NOMBRE = "DP | BITS 05 OCT 2026 12 17 -2"
PAGINAS = 10


def trabajo(raiz, carpeta="parte-02", nombre=NOMBRE, batch_id="003TIH",
            verificado=True, completado=False, cantidad=PAGINAS):
    manifiesto = Manifiesto(
        job_id=carpeta, nombre_batch=nombre, repo_id=3209, batch_id=batch_id,
        registros=[Registro(seq=i + 1, log_number=str(2300000 + i)) for i in range(cantidad)],
    )
    manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    if batch_id:
        manifiesto.etapa("descubrir").marcar(EstadoEtapa.HECHA)
    if verificado:
        manifiesto.etapa("verificar").marcar(EstadoEtapa.HECHA)
    if completado:
        manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
    t = Trabajo(AirVaultConfig(), raiz / "output" / "airvault" / carpeta, manifiesto)
    t.guardar()
    return t


def contenido(nombre=NOMBRE, cantidad=PAGINAS, logs=True, desde=2300000):
    """Las paginas de una carga tal como AirVault las tiene recien llegada."""
    paginas = {}
    for numero in range(1, cantidad + 1):
        valores = {CAMPO_LOG_NUMBER: str(desde + numero - 1)} if logs else {}
        if numero == 1 and nombre:
            valores[CAMPO_BATCH_NAME] = nombre
        paginas[numero] = pagina(numero, valores=valores)
    return paginas


def test_el_titulo_de_la_carga_por_la_web_se_compara_sin_su_sufijo():
    assert titulo_base(f"{NOMBRE}-Batch") == titulo_base(NOMBRE)
    assert titulo_base("DP | BITS 31 AUG 2026 15 42 REVISAR-Batch") == titulo_base(
        "DP | BITS 31 AUG 2026 15 42 REVISAR"
    )
    assert titulo_base("Empty-Batch") == "empty"


@pytest.mark.parametrize("original_en_cola, completado, situacion", [
    (True, False, EN_INDEX),
    (False, True, COMPLETADO),
    (False, False, INDEXADO),
])
def test_la_copia_de_un_batch_de_la_barra_se_marca_para_borrar(
    tmp_path, original_en_cola, completado, situacion,
):
    t = trabajo(tmp_path, completado=completado)
    cola = [lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)]
    if original_en_cola:
        cola.append(lote("003TIH", NOMBRE, PAGINAS))
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={"003TJF": contenido()})

    revision = revisar([t], cola, cliente)

    assert [(c.batch_id, c.nombre, c.original, c.situacion) for c in revision.copias] == [
        ("003TJF", NOMBRE, "003TIH", situacion)
    ]
    assert revision.por_borrar == ["003TJF"]
    # Se lee sin tomarlo: nadie lo abre ni lo suelta.
    assert not cliente.abiertos and not cliente.cerrados and not cliente.escrituras


def test_la_carga_recien_llegada_sin_numeros_se_reconoce_por_su_nombre_interno(tmp_path):
    """AirVault tarda en leer los Log Page Number de una carga nueva."""
    t = trabajo(tmp_path)
    cola = [lote("003TIH", NOMBRE, PAGINAS), lote("003TJJ", f"{NOMBRE}-Batch", PAGINAS)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={"003TJJ": contenido(logs=False)})

    assert [c.batch_id for c in revisar([t], cola, cliente).copias] == ["003TJJ"]


def test_con_otros_numeros_no_es_copia_aunque_lleve_el_nombre(tmp_path):
    t = trabajo(tmp_path)
    cola = [lote("003TIH", NOMBRE, PAGINAS), lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)]
    cliente = ClienteFalso(
        lotes=cola, paginas_por_lote={"003TJF": contenido(desde=2400000)},
    )

    assert revisar([t], cola, cliente).copias == []


def test_sin_nombre_ni_numeros_no_se_decide_y_se_vuelve_a_mirar_mas_tarde(tmp_path, monkeypatch):
    t = trabajo(tmp_path)
    cola = [lote("003TIH", NOMBRE, PAGINAS), lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)]
    vacio = ClienteFalso(lotes=cola, paginas_por_lote={"003TJF": contenido(nombre="", logs=False)})
    reloj = [1000.0]
    monkeypatch.setattr(copias.time, "monotonic", lambda: reloj[0])
    memoria: dict = {}

    assert revisar([t], cola, vacio, memoria).copias == []
    leidas = len(vacio.lecturas)
    assert leidas
    # La vuelta siguiente del reloj no relee lo mismo...
    assert revisar([t], cola, vacio, memoria).copias == []
    assert len(vacio.lecturas) == leidas
    # ...hasta que pasa su rato, y entonces AirVault ya leyo los numeros.
    reloj[0] += copias.SEGUNDOS_PARA_VOLVER_A_MIRAR + 1
    leido = ClienteFalso(lotes=cola, paginas_por_lote={"003TJF": contenido(nombre="")})
    assert [c.batch_id for c in revisar([t], cola, leido, memoria).copias] == ["003TJF"]


def test_lo_ya_decidido_no_se_vuelve_a_leer(tmp_path):
    t = trabajo(tmp_path)
    cola = [lote("003TIH", NOMBRE, PAGINAS), lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={"003TJF": contenido()})
    memoria: dict = {}
    revisar([t], cola, cliente, memoria)
    leidas = len(cliente.lecturas)

    assert [c.batch_id for c in revisar([t], cola, cliente, memoria).copias] == ["003TJF"]
    assert len(cliente.lecturas) == leidas


def test_sin_id_en_la_barra_no_hay_original_y_la_carga_puede_ser_la_esperada(tmp_path):
    """Eso lo resuelve la identificacion de siempre, no esta busqueda."""
    t = trabajo(tmp_path, batch_id=None, verificado=False)
    cola = [lote("003TJF", NOMBRE, PAGINAS), lote("003TJG", f"{NOMBRE}-Batch", PAGINAS)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={
        "003TJF": contenido(), "003TJG": contenido(),
    })

    revision = revisar([t], cola, cliente)

    assert revision.copias == [] and revision.repetidos == []
    assert not cliente.lecturas


def test_ni_lo_abierto_por_alguien_ni_otras_paginas_cuentan(tmp_path):
    t = trabajo(tmp_path)
    cola = [
        lote("003TIH", NOMBRE, PAGINAS),
        lote("003TJF", f"{NOMBRE}-Batch", PAGINAS, bloqueado_por="Otra Persona"),
        lote("003TJG", f"{NOMBRE}-Batch", PAGINAS - 3),
    ]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={
        "003TJF": contenido(), "003TJG": contenido(cantidad=PAGINAS - 3),
    })

    assert revisar([t], cola, cliente).copias == []
    assert not cliente.lecturas


def test_un_batch_que_lleva_otro_manifiesto_no_es_copia(tmp_path):
    t = trabajo(tmp_path)
    otro = trabajo(tmp_path, carpeta="otra-entrega", batch_id="003TJF")
    cola = [lote("003TIH", NOMBRE, PAGINAS), lote("003TJF", NOMBRE, PAGINAS)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={"003TJF": contenido()})

    assert revisar([t, otro], cola, cliente).copias == []


def test_un_empty_batch_con_el_nombre_dentro_tambien_es_copia(tmp_path):
    t = trabajo(tmp_path, completado=True)
    cola = [
        lote("003TJ2", "Empty-Batch", PAGINAS),
        lote("003TJ3", "Empty-Batch", 280),
    ]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={
        "003TJ2": contenido(), "003TJ3": contenido(nombre="F&A SHOP VISIT", cantidad=280),
    })

    revision = revisar([t], cola, cliente)

    assert [(c.batch_id, c.situacion) for c in revision.copias] == [("003TJ2", COMPLETADO)]
    # El de otras paginas no se lee: no puede ser ninguno de los propios.
    assert len(cliente.lecturas) <= copias.MUESTRAS_POR_BATCH + 1


def test_el_mismo_batch_de_otro_equipo_dos_veces_se_avisa_con_los_dos(tmp_path):
    nombre = "DP | BITS 12 OCT 2026 10 41 REVISAR"
    cola = [lote("003TIP", nombre, 40), lote("003TIO", nombre, 40)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={
        "003TIO": contenido(nombre, 40), "003TIP": contenido(nombre, 40, logs=False),
    })

    revision = revisar([], cola, cliente)

    assert revision.copias == []
    assert [(r.nombre, r.ids) for r in revision.repetidos] == [(nombre, ("003TIO", "003TIP"))]
    assert revision.por_borrar == ["003TIO", "003TIP"]


@pytest.mark.parametrize("cola", [
    # Otras paginas: son dos batches distintos.
    [lote("003TIO", "DP | BITS 12 OCT 2026 10 41 REVISAR", 40),
     lote("003TIP", "DP | BITS 12 OCT 2026 10 41 REVISAR", 39)],
    # No los sube este programa: no se sabe si sobra alguno.
    [lote("0004NB", "HP-1826CMP | ORIGINALS DOCS-Batch", 40),
     lote("0004NC", "HP-1826CMP | ORIGINALS DOCS-Batch", 40)],
])
def test_lo_que_solo_se_parece_no_se_da_por_repetido(cola):
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={
        l.batch_id: contenido(l.nombre, l.paginas) for l in cola
    })

    assert revisar([], cola, cliente).repetidos == []


def test_dos_cargas_de_otro_equipo_con_otros_numeros_no_se_dan_por_repetidas():
    nombre = "DP | BITS 12 OCT 2026 10 41 REVISAR"
    cola = [lote("003TIO", nombre, 40), lote("003TIP", nombre, 40)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={
        "003TIO": contenido(nombre, 40), "003TIP": contenido(nombre, 40, desde=2400000),
    })

    assert revisar([], cola, cliente).repetidos == []
