"""Las paginas de un batch, repartidas entre varias conexiones.

Leer y escribir un batch son cientos de peticiones cortas, una por pagina, y
en fila cada una espera a la anterior. Aqui se reparten entre unos pocos
carriles, cada uno con su propia conexion (ver
:meth:`app.airvault.client.ClienteHttp.carril`), y lo que devuelven se
entrega en el hilo que llama, que es el unico que toca el manifiesto.

Repartir solo sirve si AirVault atiende a la vez las peticiones de una misma
sesion. ASP.NET puede atenderlas de una en una, y entonces varias en vuelo
hacen cola y salen mas lentas que en fila. Por eso cada reparto empieza con
unas pocas en serie para medir, sigue en paralelo, compara y, si repartir no
gana, vuelve a la fila. Un carril que pierde la sesion o la red tampoco corta
nada: su pagina y las que quedan siguen en serie por la sesion principal,
que es la que sabe volver a entrar.
"""

from __future__ import annotations

import queue
import statistics
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import (Callable, Deque, Dict, Iterable, List, Optional, Tuple,
                    TypeVar)

from loguru import logger

from app.airvault.session import (ErrorDeConexion, ErrorDeSesion,
                                  SesionCancelada)

T = TypeVar("T")

# Peticiones que se hacen en serie antes de repartir, para saber cuanto
# tarda una sola. Son las primeras paginas del trabajo, no peticiones extra.
MUESTRA_EN_SERIE = 4

# Respuestas en paralelo con las que se decide si repartir compensa. Se
# cuentan despues de la primera de cada carril, que abre su conexion y tarda
# mas que las siguientes.
MUESTRA_EN_PARALELO = 8

# Repartir se mantiene si cada pagina sale en menos de esta fraccion de lo
# que tarda en serie. Por encima, la cola de la sesion se come la ganancia.
GANANCIA_MINIMA = 0.8

# Por debajo de esto una peticion no se puede medir contra otra (un cliente
# de prueba contesta al instante): se reparte sin comparar.
SERIE_INMEDIBLE_S = 0.02

# Con menos paginas que esto no vale la pena abrir conexiones nuevas.
MINIMO_PARA_REPARTIR = MUESTRA_EN_SERIE + 4

# Lo que un carril no puede arreglar por su cuenta y si la sesion principal:
# una sesion caducada o una red que no contesta. La cancelacion no entra:
# esa se entrega tal cual, igual que en serie.
_FALLOS_DEL_CARRIL = (ErrorDeSesion, ErrorDeConexion)

Hacer = Callable[[object, T], object]
Recibir = Callable[[T, object, Optional[BaseException]], Optional[bool]]


def repartir(
    cliente,
    tareas: Iterable[T],
    hacer: Hacer,
    recibir: Recibir,
) -> None:
    """Hace ``hacer(cliente, tarea)`` con cada tarea y entrega cada resultado.

    ``recibir(tarea, resultado, error)`` corre siempre en el hilo que llama,
    en el orden en que van terminando, con ``error`` en lugar de levantar lo
    que haya fallado: quien recibe decide si una pagina que falla corta el
    trabajo o solo se anota. Devolver ``False`` deja de empezar tareas; las
    que estaban en vuelo se terminan y se entregan igual, porque lo que ya
    se escribio en AirVault esta escrito. Lo que ``recibir`` levante sale de
    aqui tal cual, despues de soltar los carriles.

    Sin carriles (un cliente sin :meth:`carril`, ``carriles_indexado`` en 1
    o pocas tareas) todo va en serie con ``cliente``, igual que siempre.
    """
    pendientes: Deque[T] = deque(tareas)
    carriles = _carriles_de(cliente)
    if carriles < 2 or len(pendientes) < MINIMO_PARA_REPARTIR:
        _en_serie(cliente, pendientes, hacer, recibir)
        return
    tiempos: List[float] = []
    while pendientes and len(tiempos) < MUESTRA_EN_SERIE:
        tarea = pendientes.popleft()
        inicio = time.monotonic()
        resultado, error = _hacer(cliente, hacer, tarea)
        tiempos.append(time.monotonic() - inicio)
        if recibir(tarea, resultado, error) is False:
            return
    if not pendientes:
        return
    resto = _en_paralelo(
        cliente, carriles, pendientes, hacer, recibir,
        statistics.median(tiempos),
    )
    if resto:
        _en_serie(cliente, resto, hacer, recibir)


def _carriles_de(cliente) -> int:
    """Cuantas conexiones admite el cliente; 1 si no sabe abrir carriles."""
    contar = getattr(cliente, "carriles", None)
    if not callable(contar) or not callable(getattr(cliente, "carril", None)):
        return 1
    try:
        return max(1, int(contar()))
    except Exception:  # noqa: BLE001 - sin cuenta, en serie
        return 1


def _hacer(cliente, hacer: Hacer, tarea) -> Tuple[object, Optional[Exception]]:
    try:
        return hacer(cliente, tarea), None
    except Exception as exc:  # noqa: BLE001 - lo decide quien recibe
        return None, exc


def _en_serie(cliente, pendientes: Deque, hacer: Hacer, recibir: Recibir) -> None:
    while pendientes:
        tarea = pendientes.popleft()
        resultado, error = _hacer(cliente, hacer, tarea)
        if recibir(tarea, resultado, error) is False:
            return


def _cerrar(carriles: List[object]) -> None:
    for carril in carriles:
        cerrar = getattr(carril, "cerrar_conexiones", None)
        if not callable(cerrar):
            continue
        try:
            cerrar()
        except Exception:  # noqa: BLE001 - soltar nunca tumba nada
            logger.debug("No se pudo cerrar la conexion de un carril")


def _en_paralelo(
    cliente,
    carriles: int,
    pendientes: Deque[T],
    hacer: Hacer,
    recibir: Recibir,
    en_serie_s: float,
) -> Optional[Deque[T]]:
    """Reparte lo pendiente y devuelve lo que haya que seguir en serie.

    ``None`` si ``recibir`` pidio parar o ya no queda nada.
    """
    abiertos: List[object] = []
    libres: "queue.SimpleQueue[object]" = queue.SimpleQueue()
    try:
        # En este hilo: clonar lee las cookies de la sesion principal, que
        # solo este hilo usa.
        for _ in range(carriles):
            carril = cliente.carril()
            abiertos.append(carril)
            libres.put(carril)
    except Exception as exc:  # noqa: BLE001 - sin carriles se sigue en serie
        logger.info("No se pudieron abrir conexiones paralelas: {}", exc)
        _cerrar(abiertos)
        return pendientes

    def trabajar(tarea):
        # Cada carril lo usa un solo hilo a la vez: ``requests.Session`` no
        # se comparte.
        carril = libres.get()
        try:
            return hacer(carril, tarea)
        finally:
            libres.put(carril)

    ejecutor = ThreadPoolExecutor(
        max_workers=carriles, thread_name_prefix="airvault-carril"
    )
    en_vuelo: Dict[object, T] = {}
    repetir: Deque[T] = deque()
    seguir = True
    compensa = True
    decidido = en_serie_s < SERIE_INMEDIBLE_S
    entregadas = 0
    medidas = 0
    desde: Optional[float] = None
    ordenado = False

    def lanzar() -> None:
        while seguir and compensa and pendientes and len(en_vuelo) < carriles:
            tarea = pendientes.popleft()
            en_vuelo[ejecutor.submit(trabajar, tarea)] = tarea

    try:
        lanzar()
        while en_vuelo:
            hechos, _ = wait(en_vuelo, return_when=FIRST_COMPLETED)
            for futuro in hechos:
                tarea = en_vuelo.pop(futuro)
                error = futuro.exception()
                if error is not None and not isinstance(error, Exception):
                    raise error
                if (
                    isinstance(error, _FALLOS_DEL_CARRIL)
                    and not isinstance(error, SesionCancelada)
                ):
                    if compensa:
                        logger.info(
                            "Un carril perdio la conexion ({}); lo que queda "
                            "sigue en serie por la sesion principal", error,
                        )
                    compensa = False
                    repetir.append(tarea)
                    continue
                entregadas += 1
                if not decidido:
                    ahora = time.monotonic()
                    if entregadas == carriles:
                        desde = ahora
                    elif desde is not None:
                        medidas += 1
                        if medidas >= MUESTRA_EN_PARALELO:
                            decidido = True
                            por_pagina = (ahora - desde) / medidas
                            if por_pagina > GANANCIA_MINIMA * en_serie_s:
                                compensa = False
                                logger.info(
                                    "Repartir no acelera ({:.2f} s por "
                                    "pagina contra {:.2f} s en serie); se "
                                    "sigue con una sola conexion",
                                    por_pagina, en_serie_s,
                                )
                            else:
                                logger.info(
                                    "Paginas repartidas en {} conexiones: "
                                    "{:.2f} s por pagina contra {:.2f} s en "
                                    "serie",
                                    carriles, por_pagina, en_serie_s,
                                )
                resultado = None if error is not None else futuro.result()
                if recibir(tarea, resultado, error) is False:
                    seguir = False
            lanzar()
        ordenado = True
    finally:
        # Si algo corto el reparto (una cancelacion, un error que ``recibir``
        # dejo subir), no se espera a lo que este en vuelo: su resultado ya
        # no lo va a recibir nadie.
        ejecutor.shutdown(wait=ordenado, cancel_futures=True)
        _cerrar(abiertos)
    if not seguir:
        return None
    repetir.extend(pendientes)
    return repetir or None
