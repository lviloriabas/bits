"""Si las bitacoras de un batch ya estan publicadas, antes de subirlo.

Un batch subido dos veces solo se arregla a mano, en AirVault. Por eso,
antes de cada carga (la nueva, la reanudada y la que se ordena a mano) el
coordinador de subidas (:func:`app.airvault.flujo.subir_partes`) hace tres
preguntas, en este orden, y basta un si para no subir:

1. **¿Este equipo ya mando este mismo PDF?** Lo responde
   :mod:`app.airvault.envios_exactos`, sin red. El mismo archivo no sale dos
   veces salvo que una persona lo autorice.
2. **¿Web Index ya tiene esta carga?** Con su titulo, con el «-Batch» que
   agrega la carga por la web o como Empty-Batch con su nombre por dentro
   (:func:`app.airvault.flujo.comprobar_partes`,
   :func:`app.airvault.flujo.copia_en_la_cola` y
   :func:`app.airvault.flujo.ya_esta_en_airvault`). Si la tiene, se toma
   esa en vez de subir otra.
3. **¿Web Search ya tiene publicadas sus bitacoras?** Este modulo.

La tercera es la unica que ve un batch ya completado, porque completar lo
saca de Web Index; sin ella, otro equipo que no sabe que una ejecucion ya
se subio la vuelve a subir. Se hacia por una ruta directa de Web Search
que AirVault dejo de contestar: con «Detener subida si se detectan
duplicados» marcada lo frenaba todo («No se pudo verificar todo el batch»)
y sin marcar ni se intentaba. Ahora se pregunta por la misma pagina de Web
Search que usa «Verificar subidas en Web Search», con una muestra
repartida por todo el batch (:data:`MUESTRA`). Se pregunta por tandas y se
para en cuanto la respuesta esta clara, asi que un batch nuevo se sabe en
la primera tanda (:data:`AUSENTES_PARA_DESCARTAR` sin publicar bastan) y
nunca se espera mas de :data:`PRESUPUESTO_S`.

Lo que se decide con la respuesta (:func:`decidir`):

- **Casi todo lo contestado ya esta publicado** (:data:`UMBRAL_CARGA_PUBLICADA`,
  con al menos :data:`PUBLICADAS_PARA_CONFIRMAR`): la carga entera ya esta
  en AirVault. No se sube, este como este la casilla de duplicados, y la
  fila queda como posible duplicado con el motivo. Si en AirVault no esta,
  «No es duplicado: volver a subir» la manda.
- **Algunas publicadas**: son bitacoras sueltas repetidas (los mismos
  escaneos en dos ejecuciones). Eso lo sigue decidiendo la casilla, con su
  porcentaje, como siempre.
- **Web Search no contesta lo bastante**: no se sube a ciegas. La carga
  espera a la revision siguiente; una orden dada a mano si sigue, porque
  quien la da ya miro AirVault.
"""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

# Bitacoras que se pueden llegar a buscar de cada batch, repartidas de
# principio a fin. Con treinta, un batch nuevo que repite por error dos
# tercios de otra ejecucion no llega al umbral.
MUESTRA = 30

# Se preguntan por tandas, repartidas tambien, y se para en cuanto la
# respuesta esta clara: lo normal (un batch nuevo) se sabe en la primera.
TANDA = 8

# Desde que parte de lo contestado se da la carga entera por publicada. No
# es el cien por cien porque quien completa un batch corrige numeros mal
# leidos: el REVISAR del 1 de octubre se subio dos veces con 59 de sus 64
# bitacoras ya publicadas.
UMBRAL_CARGA_PUBLICADA = 90

# Cuantas publicadas hacen falta, como poco, para darla por publicada sin
# haber preguntado por toda la muestra.
PUBLICADAS_PARA_CONFIRMAR = 10

# Con estas ausentes ya no es la carga entera: es un batch nuevo, o uno con
# algunas bitacoras repetidas, y eso lo decide la casilla de duplicados.
AUSENTES_PARA_DESCARTAR = 4

# Lo mas que se espera a Web Search por batch. Si no alcanza para decidir,
# la carga automatica espera a la revision siguiente.
PRESUPUESTO_S = 120.0
ESPERA_POR_BITACORA_S = 45.0

# Cuantas pestanas de Web Search se leen a la vez.
PESTANAS = 4

Consultar = Callable[[Sequence[str]], Mapping[str, Optional[bool]]]

# Lo que se hace con el batch.
SUBIR = "subir"
NO_SUBIR = "no_subir"
ESPERAR = "esperar"


@dataclass(frozen=True)
class EnWebSearch:
    """Lo que contesto Web Search de la muestra de un batch.

    ``sin_respuesta`` son las que no contesto y las que no hizo falta
    preguntar.
    """

    publicadas: Tuple[str, ...] = ()
    ausentes: Tuple[str, ...] = ()
    sin_respuesta: Tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.publicadas) + len(self.ausentes) + len(self.sin_respuesta)

    @property
    def consultadas(self) -> int:
        return len(self.publicadas) + len(self.ausentes)

    @property
    def porcentaje(self) -> float:
        return len(self.publicadas) * 100 / self.consultadas if self.consultadas else 0.0

    @property
    def completa(self) -> bool:
        return self.total > 0 and not self.sin_respuesta

    @property
    def carga_publicada(self) -> bool:
        """Si la carga entera ya esta en AirVault."""
        if self.porcentaje < UMBRAL_CARGA_PUBLICADA:
            return False
        if self.completa:
            return True
        return len(self.publicadas) >= PUBLICADAS_PARA_CONFIRMAR

    @property
    def no_es_carga_entera(self) -> bool:
        """Si ya se sabe que no esta publicada entera."""
        if not self.consultadas or self.porcentaje >= UMBRAL_CARGA_PUBLICADA:
            return False
        return self.completa or len(self.ausentes) >= AUSENTES_PARA_DESCARTAR

    @property
    def decidida(self) -> bool:
        return self.carga_publicada or self.no_es_carga_entera


@dataclass(frozen=True)
class Decision:
    """Que se hace con el batch y por que, con las palabras del registro."""

    accion: str
    motivo: str = ""
    # La carga entera ya esta publicada: no la sube ninguna preferencia.
    carga_publicada: bool = False


def muestra(trabajo) -> List[str]:
    """Las bitacoras que se pueden buscar, repartidas por todo el batch."""
    from app.airvault.confirmacion import numeros_de_batch
    from app.airvault.websearch import muestra_de

    return muestra_de(numeros_de_batch(trabajo.manifiesto), MUESTRA)


def _tandas(numeros: Sequence[str]) -> List[List[str]]:
    """Las tandas en que se pregunta, cada una repartida por todo el batch."""
    salto = max(1, math.ceil(len(numeros) / TANDA))
    orden = [n for inicio in range(salto) for n in numeros[inicio::salto]]
    return [orden[i:i + TANDA] for i in range(0, len(orden), TANDA)]


def _revision(numeros: Sequence[str], respuestas: Mapping[str, Optional[bool]]) -> EnWebSearch:
    return EnWebSearch(
        publicadas=tuple(n for n in numeros if respuestas.get(n) is True),
        ausentes=tuple(n for n in numeros if respuestas.get(n) is False),
        sin_respuesta=tuple(n for n in numeros if respuestas.get(n) is None),
    )


def en_web_search(trabajo, consultar: Consultar,
                  reloj: Callable[[], float] = time.monotonic) -> EnWebSearch:
    """Pregunta por la muestra del batch hasta que la respuesta esta clara."""
    numeros = muestra(trabajo)
    respuestas: Dict[str, Optional[bool]] = {}
    revision = _revision(numeros, respuestas)
    inicio = reloj()
    for tanda in _tandas(numeros):
        respuestas.update(consultar(tanda))
        revision = _revision(numeros, respuestas)
        if revision.decidida or reloj() - inicio >= PRESUPUESTO_S:
            break
    return revision


def decidir(revision: EnWebSearch, porcentaje_duplicados: float,
            orden_a_mano: bool = False) -> Decision:
    """Que hacer con un batch segun lo que contesto Web Search."""
    if revision.total == 0:
        # Sin numeros que buscar no hay pregunta posible; decide lo demas.
        return Decision(SUBIR)
    if revision.carga_publicada:
        return Decision(
            NO_SUBIR,
            f"Web Search ya tiene publicadas {len(revision.publicadas)} de las "
            f"{revision.consultadas} bitácoras que se buscaron de este batch: "
            "la carga entera ya está en AirVault",
            carga_publicada=True,
        )
    if not revision.no_es_carga_entera:
        if orden_a_mano:
            return Decision(SUBIR)
        return Decision(
            ESPERAR,
            "Web Search no contestó y sin esa comprobación no se sabe si ya "
            "está publicado; se vuelve a intentar en la revisión siguiente",
        )
    if revision.publicadas and revision.porcentaje >= porcentaje_duplicados:
        return Decision(
            SUBIR,
            f"Web Search ya tiene publicadas {len(revision.publicadas)} de las "
            f"{revision.consultadas} bitácoras que se buscaron "
            f"({revision.porcentaje:.0f}%; límite {porcentaje_duplicados:g}%)",
        )
    return Decision(SUBIR)


class ConsultaPorPaginas:
    """Pregunta a Web Search por su pagina, como «Verificar subidas en Web Search».

    Abre el acceso la primera vez que hace falta y lo reusa para todos los
    batches de la tanda. Un numero que no se pudo leer queda en ``None``:
    no cuenta ni como publicado ni como ausente. :meth:`cerrar` suelta el
    navegador.
    """

    def __init__(self, config, sesion, cancelado: Callable[[], bool] = lambda: False):
        self.config = config
        self.sesion = sesion
        self.cancelado = cancelado
        self._acceso = None
        self._fallo = ""

    def _abrir(self):
        if self._acceso is None and not self._fallo:
            from app.airvault.websearch_pagina import AccesoWebSearch

            acceso = AccesoWebSearch(self.config, self.sesion, cancelado=self.cancelado,
                                     permitir_ventana=False)
            try:
                acceso.__enter__()
            except Exception as exc:  # noqa: BLE001 - se dice y se espera
                self._fallo = str(exc)
                logger.info("Web Search no abrió para comprobar antes de subir: {}", exc)
                return None
            self._acceso = acceso
        return self._acceso

    def __call__(self, numeros: Sequence[str]) -> Dict[str, Optional[bool]]:
        from app.airvault.session import SesionCancelada

        respuestas: Dict[str, Optional[bool]] = {numero: None for numero in numeros}
        acceso = self._abrir()
        if acceso is None or not numeros:
            return respuestas
        candado = threading.Lock()

        def leer(trozo: Sequence[str]) -> None:
            lector = acceso.crear_lector()
            try:
                for numero in trozo:
                    if self.cancelado():
                        raise SesionCancelada("Se canceló la comprobación en Web Search")
                    try:
                        datos = lector.consultar_numero(numero, ESPERA_POR_BITACORA_S)
                    except SesionCancelada:
                        raise
                    except Exception as exc:  # noqa: BLE001 - ese numero queda sin saber
                        logger.debug("Web Search no contestó por {}: {}", numero, exc)
                        continue
                    filas = datos.get("rows", []) if isinstance(datos, Mapping) else []
                    publicada = any(
                        str(fila.get("LogNo", "")).strip() == numero
                        for fila in filas if isinstance(fila, Mapping)
                    )
                    with candado:
                        respuestas[numero] = publicada
            finally:
                lector.cerrar()

        cuantas = max(1, min(PESTANAS, len(numeros)))
        trozos = [list(numeros[i::cuantas]) for i in range(cuantas)]
        with ThreadPoolExecutor(max_workers=cuantas, thread_name_prefix="antes-de-subir") as pool:
            for futuro in [pool.submit(leer, trozo) for trozo in trozos]:
                futuro.result()
        return respuestas

    def cerrar(self) -> None:
        if self._acceso is not None:
            try:
                self._acceso.__exit__(None, None, None)
            finally:
                self._acceso = None
