"""Batches que estan dos veces en la cola de Web Index.

Una carga repetida no deberia existir, pero pasa: alguien sube por la web un
PDF que el programa ya habia mandado (AirVault le pone de titulo el nombre
con «-Batch» al final), otro equipo vuelve a mandar una ejecucion que no
sabe subida, o el reenvio de una carga que parecia perdida coincide con la
original. La copia se queda en la cola sin que nadie la indexe y, si alguien
la completa, publica las mismas bitacoras dos veces.

Este modulo la reconoce mirando la cola entera en cada revision. No borra
nada: el programa no tiene permiso para borrar batches y elegir cual sobra
es una decision que, mal tomada, se lleva el trabajo hecho. Dice cual es la
copia, de que batch y en que quedo el que se conserva, y la ventana lo
escribe en el registro.

Una copia es un batch de la cola que la barra lateral no lleva, con el
titulo de uno que si lleva (con o sin el «-Batch» de la carga por la web, o
como ``Empty-Batch`` con ese nombre por dentro) y sus mismas paginas, cuyo
contenido coincide con el manifiesto: los Log Page Number de unas paginas
repartidas, leidos sin tomar el batch. Solo cuenta si el original ya esta a
salvo: sigue en la cola con su ID, ya se completo o ya se indexo entero.
Mientras el batch de la barra no tiene ID no hay original con que comparar,
y la copia puede ser justo la carga que se espera; eso lo resuelve la
identificacion de siempre.

Tambien se reconocen los batches del programa que ningun manifiesto de este
equipo lleva y que estan dos o mas veces con el mismo titulo, las mismas
paginas y el mismo contenido: los subio otro equipo mas de una vez. Desde
aqui no se sabe cual conserva aquel, asi que se dice cuales son y que sobra
una.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Iterable, List, MutableMapping, Optional, Sequence, Tuple

from loguru import logger

from app.airvault.client import ResumenLote
from app.airvault.config import CAMPO_BATCH_NAME, CAMPO_LOG_NUMBER
from app.airvault.discovery import normalizar_nombre
from app.airvault.mapping import normalizar_log_number
from app.airvault.naming import PREFIJO_POR_DEFECTO

if TYPE_CHECKING:
    from app.airvault.flujo import Trabajo

# En que quedo el batch que se conserva. Es lo que la ventana cuenta de cada
# copia, porque decide lo urgente que es borrarla: la de un batch ya
# completado publica sus bitacoras otra vez en cuanto alguien la complete.
EN_INDEX = "en_index"
COMPLETADO = "completado"
INDEXADO = "indexado"

# Lo que AirVault agrega al titulo de lo que recibe, tambien de lo que manda
# este programa, hasta que se le pone el suyo. Normalizado, el guion se
# vuelve espacio como cualquier otro separador.
_SUFIJO_WEB = " batch"

# Paginas que se leen de cada batch para comparar contenido, y cuantas tienen
# que coincidir. Siete repartidas son las mismas que usa la identificacion de
# una carga; tres coincidencias dejan fuera el log suelto que se repite en
# otra entrega.
MUESTRAS_POR_BATCH = 7
COINCIDENCIAS_MINIMAS = 3


@dataclass(frozen=True)
class Copia:
    """Un batch de Web Index que repite otro de este equipo."""

    lote: ResumenLote
    # El batch de la barra lateral que la copia repite.
    nombre: str
    # El ID que se conserva. Vacio cuando el original se confirmo en Web
    # Search sin haber guardado su ID.
    original: str
    situacion: str

    @property
    def batch_id(self) -> str:
        return self.lote.batch_id.strip().upper()


@dataclass(frozen=True)
class Repetido:
    """Un batch que ningun manifiesto de este equipo lleva y esta varias veces."""

    nombre: str
    lotes: Tuple[ResumenLote, ...]

    @property
    def ids(self) -> Tuple[str, ...]:
        return tuple(lote.batch_id.strip().upper() for lote in self.lotes)


@dataclass
class RevisionDeIndex:
    """Lo que dijo la cola entera en esta revision."""

    ids: frozenset = frozenset()
    copias: List[Copia] = field(default_factory=list)
    repetidos: List[Repetido] = field(default_factory=list)

    @property
    def por_borrar(self) -> List[str]:
        """Los IDs que hay que borrar, o entre los que hay que elegir."""
        ids = [copia.batch_id for copia in self.copias]
        for repetido in self.repetidos:
            ids.extend(repetido.ids)
        return ids


def titulo_base(nombre: str) -> str:
    """El titulo para comparar, sin el «-Batch» de la carga por la web."""
    normal = normalizar_nombre(nombre)
    if normal.endswith(_SUFIJO_WEB):
        normal = normal[: -len(_SUFIJO_WEB)].rstrip()
    return normal


def _es_temporal(nombre: str) -> bool:
    return normalizar_nombre(nombre) in {"empty batch", "index batch"}


def _clave_id(valor) -> str:
    return str(valor or "").strip().upper()


def _original(trabajo, ids: frozenset) -> Optional[Tuple[str, str]]:
    """El ID que este batch conserva y en que quedo, si ya esta a salvo."""
    from app.airvault.flujo import websearch_confirmacion_valida

    manifiesto = trabajo.manifiesto
    batch_id = _clave_id(manifiesto.batch_id)
    if batch_id and batch_id in ids:
        return batch_id, EN_INDEX
    if manifiesto.cierre_hecho() or websearch_confirmacion_valida(manifiesto):
        return batch_id, COMPLETADO
    if batch_id and manifiesto.etapa_hecha("verificar"):
        return batch_id, INDEXADO
    return None


def _muestra_del_manifiesto(manifiesto) -> Dict[int, Tuple[str, ...]]:
    """Pagina del batch y los numeros que puede traer, repartidas por todo el batch.

    El numero que AirVault leyo por su cuenta puede ser el que el programa
    leyo o el que adopto de AirVault: los dos valen para reconocer la pagina.
    """
    from app.airvault.flujo import _registros_de_huella

    muestra: Dict[int, Tuple[str, ...]] = {}
    for pagina, registro in _registros_de_huella(manifiesto, maximo=MUESTRAS_POR_BATCH):
        numeros = tuple(
            numero for numero in (
                normalizar_log_number(registro.log_number),
                normalizar_log_number(getattr(registro, "log_leido", "")),
            ) if numero
        )
        if numeros:
            muestra[int(pagina)] = numeros
    return muestra


def _repartidas(paginas: int, cuantas: int = MUESTRAS_POR_BATCH) -> List[int]:
    if paginas <= 0:
        return []
    if paginas <= cuantas:
        return list(range(1, paginas + 1))
    return sorted({
        1 + round(posicion * (paginas - 1) / (cuantas - 1))
        for posicion in range(cuantas)
    })


def _leer(cliente, batch_id: str, pagina: int):
    """Una pagina leida sin tomar el batch, o ``None`` si no se pudo."""
    from app.airvault.indexer import FALLOS_DE_CAMINO

    try:
        return cliente.leer_pagina(batch_id, pagina)
    except FALLOS_DE_CAMINO:
        raise
    except Exception as exc:  # noqa: BLE001 - una pagina no decide nada
        logger.debug(
            "No se pudo leer la pagina {} de {} al buscar copias: {}",
            pagina, batch_id, exc,
        )
        return None


def _log_de(pagina) -> str:
    if pagina is None:
        return ""
    return normalizar_log_number(pagina.valores.get(CAMPO_LOG_NUMBER, ""))


def _veredicto(
    coincidencias: int, discrepancias: int, posibles: int, mismo_nombre: bool,
) -> Optional[bool]:
    """Si el contenido es el mismo; ``None`` si lo leido no alcanza para decirlo.

    Una carga recien llegada casi no trae Log Page Number: AirVault los lee
    despues, y en la cola hay copias con cinco de siete paginas vacias. Lo
    que si trae desde el principio es el Batch Name que el programa le dio al
    subirla, dentro de la primera pagina. Ese nombre, con el titulo y las
    paginas de siempre y sin ningun numero que diga otra cosa, ya es la
    misma carga: el nombre lleva la hora de la ejecucion y la parte, y no lo
    repite ninguna otra.
    """
    from app.airvault.flujo import _contenido_ajeno

    if _contenido_ajeno(coincidencias, discrepancias):
        return False
    if coincidencias >= min(COINCIDENCIAS_MINIMAS, posibles):
        return True
    if mismo_nombre and not discrepancias:
        return True
    return None


class _Lector:
    """Lee cada pagina una sola vez por revision, sin tomar el batch."""

    def __init__(self, cliente):
        self.cliente = cliente
        self._leidas: Dict[tuple, object] = {}

    def pagina(self, batch_id: str, numero: int):
        clave = (_clave_id(batch_id), int(numero))
        if clave not in self._leidas:
            self._leidas[clave] = _leer(self.cliente, batch_id, numero)
        return self._leidas[clave]

    def log(self, batch_id: str, numero: int) -> str:
        return _log_de(self.pagina(batch_id, numero))

    def nombre(self, batch_id: str) -> Optional[str]:
        """El Batch Name que la carga dejo dentro de la primera pagina.

        ``None`` si la pagina no se pudo leer: vacio tambien es una
        respuesta.
        """
        pagina = self.pagina(batch_id, 1)
        if pagina is None:
            return None
        return normalizar_nombre(
            pagina.valores.get(CAMPO_BATCH_NAME)
            or pagina.columnas.get("Batch Name")
            or pagina.columnas.get("C_BatchName")
            or ""
        )


def _mismo_contenido_que(lector: _Lector, lote: ResumenLote, manifiesto) -> Optional[bool]:
    """Si ese batch de la cola trae las mismas bitacoras que el manifiesto."""
    muestra = _muestra_del_manifiesto(manifiesto)
    coincidencias = discrepancias = 0
    for pagina, numeros in sorted(muestra.items()):
        remoto = lector.log(lote.batch_id, pagina)
        if not remoto:
            continue
        if remoto in numeros:
            coincidencias += 1
        else:
            discrepancias += 1
    interno = lector.nombre(lote.batch_id)
    mismo_nombre = bool(interno) and titulo_base(interno) == normalizar_nombre(manifiesto.nombre_batch)
    return _veredicto(coincidencias, discrepancias, len(muestra), mismo_nombre)


def _mismo_contenido_entre(lector: _Lector, lotes: Sequence[ResumenLote]) -> Optional[bool]:
    """Si todos esos batches traen lo mismo en las mismas paginas."""
    paginas = _repartidas(min(lote.paginas for lote in lotes))
    if not paginas:
        return None
    coincidencias = discrepancias = 0
    for pagina in paginas:
        valores = [lector.log(lote.batch_id, pagina) for lote in lotes]
        if not all(valores):
            continue
        if all(valor == valores[0] for valor in valores):
            coincidencias += 1
        else:
            discrepancias += 1
    titulo = titulo_base(lotes[0].nombre)
    internos = [lector.nombre(lote.batch_id) for lote in lotes]
    mismo_nombre = all(interno and titulo_base(interno) == titulo for interno in internos)
    return _veredicto(coincidencias, discrepancias, len(paginas), mismo_nombre)


# Cada cuanto se vuelve a leer una copia posible de la que no se pudo decidir
# nada. Lo que AirVault aun no lee aparece en sus paginas con el tiempo, y
# leerla en cada vuelta del reloj, que puede ser de un minuto, era gastar
# peticiones en lo mismo.
SEGUNDOS_PARA_VOLVER_A_MIRAR = 1800.0


def _recordado(memoria: MutableMapping, clave: tuple, calcular) -> Optional[bool]:
    """Lo ya decidido de ese par no se vuelve a leer en cada revision."""
    if clave in memoria:
        return memoria[clave]
    duda = ("duda",) + clave
    if duda in memoria and time.monotonic() - memoria[duda] < SEGUNDOS_PARA_VOLVER_A_MIRAR:
        return None
    resultado = calcular()
    if resultado is None:
        memoria[duda] = time.monotonic()
    else:
        memoria.pop(duda, None)
        memoria[clave] = resultado
    return resultado


def revisar(
    trabajos: Iterable,
    lotes: Sequence[ResumenLote],
    cliente,
    memoria: Optional[MutableMapping] = None,
    prefijo: str = PREFIJO_POR_DEFECTO,
) -> RevisionDeIndex:
    """Las copias que hay en la cola entera de Web Index. No escribe nada.

    ``trabajos`` son todos los manifiestos de este equipo, tambien los ya
    completados: la copia de un batch cerrado hace mucho es la que mas urge
    borrar. ``memoria`` guarda lo ya comparado entre revisiones, para que
    cada copia se lea una sola vez.
    """
    memoria = memoria if memoria is not None else {}
    trabajos = list(trabajos)
    ids = frozenset(_clave_id(lote.batch_id) for lote in lotes if lote.batch_id)
    llevados = {_clave_id(t.manifiesto.batch_id) for t in trabajos if t.manifiesto.batch_id}
    for trabajo in trabajos:
        llevados.update(_clave_id(valor) for valor in trabajo.manifiesto.batches_descartados)

    originales: Dict[str, List[Tuple["Trabajo", str, str]]] = {}
    nombres_locales = set()
    for trabajo in trabajos:
        nombre = normalizar_nombre(trabajo.manifiesto.nombre_batch)
        if not nombre:
            continue
        nombres_locales.add(nombre)
        if trabajo.manifiesto.cancelado:
            continue
        original = _original(trabajo, ids)
        if original is not None:
            originales.setdefault(nombre, []).append((trabajo, original[0], original[1]))

    sueltos = [
        lote for lote in lotes
        if lote.batch_id and _clave_id(lote.batch_id) not in llevados
    ]
    revision = RevisionDeIndex(ids=ids)
    lector = _Lector(cliente)
    for lote in sueltos:
        nombre = titulo_base(lote.nombre)
        candidatos = originales.get(nombre)
        if not candidatos and _es_temporal(lote.nombre) and not lote.bloqueado_por:
            # Sin titulo, el nombre esta dentro de la primera pagina. Solo se
            # lee la de los que tienen las paginas de algun batch propio: la
            # mayoria de los Empty-Batch de la cola son de otras personas.
            if any(lote.paginas in t.manifiesto.cantidades_paginas_compatibles()
                   for grupo in originales.values() for t, _id, _situacion in grupo):
                clave = ("interno", _clave_id(lote.batch_id), lote.paginas)
                if clave not in memoria:
                    interno = lector.nombre(lote.batch_id)
                    if interno is not None:
                        memoria[clave] = interno
                candidatos = originales.get(titulo_base(memoria.get(clave) or ""))
        for trabajo, original, situacion in candidatos or ():
            manifiesto = trabajo.manifiesto
            if lote.repo_id and manifiesto.repo_id and lote.repo_id != manifiesto.repo_id:
                continue
            if lote.paginas not in manifiesto.cantidades_paginas_compatibles():
                continue
            if lote.bloqueado_por:
                # Abierto por alguien no se lee: se reconoce cuando lo suelte.
                continue
            par = ("copia", _clave_id(lote.batch_id), str(trabajo.carpeta), lote.paginas)
            if _recordado(memoria, par, lambda: _mismo_contenido_que(lector, lote, manifiesto)):
                revision.copias.append(Copia(lote, manifiesto.nombre_batch, original, situacion))
                break

    vistas = {copia.batch_id for copia in revision.copias}
    propio = normalizar_nombre(prefijo)
    grupos: Dict[tuple, List[ResumenLote]] = {}
    for lote in sueltos:
        nombre = titulo_base(lote.nombre)
        if (
            _clave_id(lote.batch_id) in vistas or nombre in nombres_locales
            or not propio or not nombre.startswith(propio + " ")
        ):
            continue
        grupos.setdefault((nombre, lote.paginas, lote.repo_id), []).append(lote)
    for (_nombre, _paginas, _repo), grupo in grupos.items():
        if len(grupo) < 2 or any(lote.bloqueado_por for lote in grupo):
            continue
        grupo = sorted(grupo, key=lambda lote: _clave_id(lote.batch_id))
        juego = ("repetido", tuple(_clave_id(lote.batch_id) for lote in grupo), grupo[0].paginas)
        if _recordado(memoria, juego, lambda: _mismo_contenido_entre(lector, grupo)):
            revision.repetidos.append(Repetido(grupo[0].nombre, tuple(grupo)))
    if revision.copias or revision.repetidos:
        logger.info(
            "Web Index tiene batches repetidos: {}",
            ", ".join(revision.por_borrar),
        )
    return revision


def copias_de_la_carga(
    manifiesto, lotes: Sequence[ResumenLote], cliente,
) -> Tuple[List[ResumenLote], List[ResumenLote]]:
    """Los batches de la cola que ya son esta carga con otro titulo.

    Antes de subir un archivo que no tiene batch se mira si alguien ya lo
    subio: por la web, que le pone «-Batch» al titulo, o desde otro equipo,
    que lo deja como ``Empty-Batch`` con su nombre por dentro. La
    identificacion de siempre solo reconoce el titulo exacto y el
    Empty-Batch que llego despues de la carga propia, asi que esa copia se
    quedaba sin ver y el archivo se subia otra vez.

    Devuelve ``(iguales, dudosos)``: los primeros tienen sus paginas y su
    contenido; los segundos, el titulo y las paginas, pero estan abiertos
    por alguien o lo leido no alcanza para decirlo. El titulo exacto no
    entra: ese lo resuelve la identificacion de siempre.
    """
    nombre = normalizar_nombre(manifiesto.nombre_batch)
    if not nombre:
        return [], []
    compatibles = manifiesto.cantidades_paginas_compatibles()
    descartados = {_clave_id(valor) for valor in manifiesto.batches_descartados}
    lector = _Lector(cliente)
    iguales: List[ResumenLote] = []
    dudosos: List[ResumenLote] = []
    for lote in lotes:
        if not lote.batch_id or _clave_id(lote.batch_id) in descartados:
            continue
        if lote.repo_id and manifiesto.repo_id and lote.repo_id != manifiesto.repo_id:
            continue
        if lote.paginas not in compatibles or normalizar_nombre(lote.nombre) == nombre:
            continue
        if titulo_base(lote.nombre) != nombre:
            # Sin titulo, el nombre esta dentro de la primera pagina. El
            # abierto por alguien no se lee: no hay de donde sacarlo.
            if not _es_temporal(lote.nombre) or lote.bloqueado_por:
                continue
            interno = lector.nombre(lote.batch_id)
            if not interno or titulo_base(interno) != nombre:
                continue
        if lote.bloqueado_por:
            dudosos.append(lote)
            continue
        veredicto = _mismo_contenido_que(lector, lote, manifiesto)
        if veredicto:
            iguales.append(lote)
        elif veredicto is None:
            dudosos.append(lote)
    return iguales, dudosos


def armandose_en_la_cola(manifiesto, lotes: Sequence[ResumenLote]) -> List[ResumenLote]:
    """Los batches con el titulo de esta carga y «-Batch» que AirVault todavia arma.

    Lo recien recibido aparece en la cola con ese titulo y menos paginas de
    las que tendra. Puede ser la carga que este equipo mando hace rato o la
    que mando otro: el nombre lleva la hora de la ejecucion y la parte, y no
    lo repite ninguna otra. No se compara contenido, porque sus paginas aun
    no estan, y no hace falta: subir otra vez mientras tanto es lo que deja
    dos.
    """
    nombre = normalizar_nombre(manifiesto.nombre_batch)
    if not nombre:
        return []
    menor = min(manifiesto.cantidades_paginas_compatibles())
    descartados = {_clave_id(valor) for valor in manifiesto.batches_descartados}
    return [
        lote for lote in lotes
        if lote.batch_id and _clave_id(lote.batch_id) not in descartados
        and not (lote.repo_id and manifiesto.repo_id and lote.repo_id != manifiesto.repo_id)
        and normalizar_nombre(lote.nombre) != nombre
        and titulo_base(lote.nombre) == nombre
        and lote.paginas < menor
    ]
