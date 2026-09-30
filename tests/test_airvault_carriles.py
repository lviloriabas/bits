"""Las paginas de un batch repartidas entre varias conexiones.

Repartir tiene que dar lo mismo que ir en fila: cada resultado llega al hilo
que llama, el manifiesto no lo toca nadie mas, y si AirVault atiende de una
en una o un carril pierde la sesion, el trabajo sigue en serie sin perder
ninguna pagina.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.airvault import carriles
from app.airvault.carriles import repartir
from app.airvault.session import ErrorDeSesion

HILO_PRINCIPAL = threading.get_ident


class Conexion:
    """Un carril: anota en que hilo atiende cada tarea."""

    def __init__(self, dueno, principal=False):
        self.dueno = dueno
        self.principal = principal
        self.cerrada = False

    def hacer(self, tarea):
        self.dueno.hilos.add(threading.get_ident())
        if self.principal:
            self.dueno.en_principal.append(tarea)
        else:
            self.dueno.en_carril.append(tarea)
        return self.dueno.responder(self, tarea)

    def cerrar_conexiones(self):
        self.cerrada = True


class Cliente:
    """Cliente con carriles; ``responder`` decide que contesta cada uno."""

    def __init__(self, cuantos=4, responder=None):
        self.cuantos = cuantos
        self.responder = responder or (lambda _conexion, tarea: tarea * 10)
        self.hilos: set[int] = set()
        self.en_principal: list[int] = []
        self.en_carril: list[int] = []
        self.abiertos: list[Conexion] = []
        self.propia = Conexion(self, principal=True)

    def carriles(self):
        return self.cuantos

    def carril(self):
        conexion = Conexion(self)
        self.abiertos.append(conexion)
        return conexion


def hacer(conexion, tarea):
    if isinstance(conexion, Cliente):
        return conexion.propia.hacer(tarea)
    return conexion.hacer(tarea)


def test_sin_carriles_va_en_serie_y_en_orden():
    class Sencillo:
        def hacer(self, tarea):
            return tarea + 1

    recibidas = []

    repartir(
        Sencillo(), range(20),
        lambda cliente, tarea: cliente.hacer(tarea),
        lambda tarea, resultado, error: recibidas.append((tarea, resultado)),
    )

    assert recibidas == [(n, n + 1) for n in range(20)]


def test_reparte_entre_carriles_y_entrega_en_el_hilo_que_llama():
    barrera = threading.Barrier(4, timeout=5)

    def responder(conexion, tarea):
        # Las cuatro primeras en paralelo solo pasan si de verdad estan a la
        # vez en cuatro hilos.
        if not conexion.principal and tarea < 8:
            barrera.wait()
        return tarea * 10

    cliente = Cliente(responder=responder)
    principal = threading.get_ident()
    recibidas: dict[int, int] = {}

    def recibir(tarea, resultado, error):
        assert threading.get_ident() == principal
        assert error is None
        recibidas[tarea] = resultado

    repartir(cliente, range(40), hacer, recibir)

    assert recibidas == {n: n * 10 for n in range(40)}
    assert len(cliente.en_carril) > 0
    assert len(cliente.hilos) > 1
    assert all(conexion.cerrada for conexion in cliente.abiertos)


def test_vuelve_a_serie_si_airvault_atiende_de_una_en_una():
    """Varias en vuelo que hacen cola en la sesion no aceleran nada."""
    turno = threading.Lock()

    def responder(_conexion, tarea):
        with turno:
            time.sleep(0.05)
        return tarea

    cliente = Cliente(responder=responder)
    recibidas: list[int] = []

    repartir(
        cliente, range(30), hacer,
        lambda tarea, resultado, error: recibidas.append(tarea),
    )

    assert sorted(recibidas) == list(range(30))
    # Muestra en serie, un tramo en paralelo para medir y el resto en serie.
    muestra = carriles.MUESTRA_EN_SERIE
    medidas = cliente.cuantos + carriles.MUESTRA_EN_PARALELO
    assert len(cliente.en_carril) <= medidas + cliente.cuantos
    assert len(cliente.en_principal) >= 30 - muestra - medidas - cliente.cuantos
    assert cliente.en_principal[:muestra] == list(range(muestra))


def test_se_queda_en_paralelo_si_acelera():
    def responder(_conexion, tarea):
        time.sleep(0.05)
        return tarea

    cliente = Cliente(responder=responder)

    repartir(cliente, range(40), hacer, lambda *_: None)

    assert len(cliente.en_principal) == carriles.MUESTRA_EN_SERIE
    assert len(cliente.en_carril) == 40 - carriles.MUESTRA_EN_SERIE


def test_un_carril_sin_sesion_deja_el_resto_a_la_sesion_principal():
    """El carril no puede volver a entrar; la sesion principal si."""

    def responder(conexion, tarea):
        if not conexion.principal and tarea == 10:
            raise ErrorDeSesion("AirVault volvio a pedir acceso")
        return tarea

    cliente = Cliente(responder=responder)
    recibidas: dict[int, object] = {}

    def recibir(tarea, resultado, error):
        assert error is None
        recibidas[tarea] = resultado

    repartir(cliente, range(30), hacer, recibir)

    assert recibidas == {n: n for n in range(30)}
    # La que fallo en el carril se repitio por la sesion principal.
    assert 10 in cliente.en_principal


def test_un_error_de_pagina_llega_a_quien_recibe():
    def responder(_conexion, tarea):
        if tarea == 12:
            raise ValueError("pagina 12 rota")
        return tarea

    cliente = Cliente(responder=responder)
    errores: dict[int, str] = {}

    repartir(
        cliente, range(20), hacer,
        lambda tarea, resultado, error: errores.__setitem__(tarea, str(error))
        if error else None,
    )

    assert errores == {12: "pagina 12 rota"}


def test_parar_no_empieza_mas_pero_entrega_lo_que_estaba_en_vuelo():
    cliente = Cliente()
    recibidas: list[int] = []

    def recibir(tarea, resultado, error):
        recibidas.append(tarea)
        return tarea != 6

    repartir(cliente, range(200), hacer, recibir)

    assert 6 in recibidas
    # Lo que ya estaba en vuelo se entrega; lo demas no se llega a empezar.
    assert len(recibidas) <= 6 + 1 + cliente.cuantos
    assert len(cliente.en_principal) + len(cliente.en_carril) == len(recibidas)


def test_lo_que_levanta_quien_recibe_sale_y_suelta_los_carriles():
    cliente = Cliente()

    class Cancelado(BaseException):
        pass

    def recibir(tarea, resultado, error):
        if tarea == 9:
            raise Cancelado()

    with pytest.raises(Cancelado):
        repartir(cliente, range(100), hacer, recibir)

    assert cliente.abiertos
    assert all(conexion.cerrada for conexion in cliente.abiertos)


def test_con_un_carril_configurado_no_se_reparte():
    cliente = Cliente(cuantos=1)

    repartir(cliente, range(30), hacer, lambda *_: None)

    assert cliente.abiertos == []
    assert cliente.en_principal == list(range(30))


def test_el_indexado_repartido_escribe_y_verifica_lo_mismo_que_en_serie():
    """Planificar, escribir y verificar un batch con carriles da lo mismo."""
    from app.airvault.indexer import Indexador, verificar_lote
    from app.airvault.model import EstadoRegistro, Manifiesto, Registro
    from tests.airvault_fake import ClienteFalso

    def manifiesto():
        return Manifiesto(
            job_id="t", nombre_batch="DP | PRUEBA", batch_id="003TEST",
            registros=[
                Registro(seq=i, matricula="HP-1848CMP",
                         log_number=f"22873{i:02d}", fecha="2026/08/31",
                         fleet="NG", archivo_origen="Image_001.pdf",
                         pagina_origen=i)
                for i in range(1, 41)
            ],
        )

    class ConCarriles(ClienteFalso):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.hilos: set[int] = set()

        def carriles(self):
            return 4

        def carril(self):
            return self

        def guardar_pagina(self, *args, **kwargs):
            self.hilos.add(threading.get_ident())
            return super().guardar_pagina(*args, **kwargs)

    resultados = []
    for cliente in (ClienteFalso(page_count=40), ConCarriles(page_count=40)):
        propio = manifiesto()
        indexador = Indexador(cliente, propio, ["HP-1848CMP"])
        resultado = indexador.aplicar(indexador.planificar(40), False)
        resultados.append((
            resultado.escritas, resultado.fallidas,
            sorted(pagina for pagina, _v, _e in cliente.escrituras),
            [r.estado for r in propio.registros],
            verificar_lote(cliente, propio)[:2],
        ))

    assert resultados[0] == resultados[1]
    assert resultados[1][0] == 40
    assert set(resultados[1][3]) == {EstadoRegistro.ESCRITA}
    assert resultados[1][4] == (40, 40)
    assert len(cliente.hilos) > 1
