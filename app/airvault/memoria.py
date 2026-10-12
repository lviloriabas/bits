"""Lo que AirVault ya tiene indexado, puesto a comprobar la memoria local.

El programa recuerda entre ejecuciones la matricula de cada libro y los
extremos de fecha que confirmo. Esa memoria se aprende del propio OCR, asi
que se puede equivocar, y cuando se equivoca no hay nada que la corrija:
:mod:`app.validation.book_memory` lo explica entero. Este modulo es la
mitad que trae la evidencia de fuera.

Hay dos formas de preguntarle a AirVault, y las dos evitan cualquier
intervencion:

**Las paginas que el plan ya lee.** Antes de escribir nada, el indexado se
trae de AirVault las paginas del batch para contrastarlas. Las que estan en
verde traen el indice que la empresa da por bueno: lo escribio este
programa, o lo corrigio una persona en Web Index. No cuesta ni una peticion
de mas, va en cada plan y en cada dry run, y no hay que encender nada.

Solo cuentan las verdes. En cualquier otro estado lo que se ve en Aircraft
es la clasificacion inicial de Quick Upload, que pone en todas las paginas
del archivo el avion de la primera bitacora; contrastar contra eso acusaria
a media entrega (es la misma razon por la que
:func:`app.airvault.guards.verificar_alineacion` tampoco la mira).

**Web Search.** Alcanza a los libros que no vienen en el batch de hoy, que
son casi todos los de la memoria, y sobre todo a los que se publicaron
antes de que existiera este programa: ahi AirVault es de verdad una fuente
externa. Cuesta unas peticiones por libro y la consulta no esta documentada
(ver :mod:`app.airvault.websearch`), asi que no se pregunta por todos a la
vez: despues de cada indexado le toca el turno a unos pocos, los que llevan
mas tiempo sin mirarse, y en unas cuantas ejecuciones se recorre la memoria
entera. Sigue habiendo una comprobacion completa que se pide a mano desde
la consola. Las dos dependen de ``buscar_publicadas``, que viene apagado.

Queda la objecion evidente de la primera forma: si la memoria estaba mal y
este programa escribio esa matricula en AirVault, releerla parece darse la
razon solo. Es cierto que no la confirma, pero tampoco hace dano: cuando lo
leido coincide con lo guardado no se toca nada. Lo unico que cambia la
memoria es que AirVault diga algo **distinto**, y eso solo pasa cuando
alguien lo corrigio o cuando esas paginas se indexaron sin este programa.
Que son justo los dos casos en los que AirVault tiene razon y la memoria
no.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from loguru import logger

from app.airvault.config import (
    CAMPO_END_DATE,
    CAMPO_LOG_NUMBER,
    CAMPO_MATRICULA,
    ESTADO_VALIDO,
)
from app.airvault.mapping import (
    fecha_desde_airvault,
    normalizar_log_number,
    normalizar_matricula,
)
from app.airvault.websearch import Buscador, muestra_de
from app.utils.fleet import FLEET_FILENAME, load_fleet
from app.validation.book_corrector import BOOK_MATRICULAS_FILENAME
from app.validation.book_memory import (
    Informe,
    Observacion,
    libros_guardados,
    verificar,
)
from app.validation.date_corrector import BOOK_DATES_FILENAME

# Cuantas bitacoras de cada libro se le preguntan a Web Search. Con una sola
# no se puede corregir nada (reemplazar una entrada pide dos), y de un libro
# publicado a medias las primeras paginas no dicen lo mismo que las ultimas,
# asi que se preguntan unas cuantas repartidas por el libro.
BITACORAS_POR_LIBRO = 4

# Paginas de un libro. La mitad A son las 00 a 49 y la B las 50 a 99.
PAGINAS_POR_LIBRO = 50

# Cuantos libros se comprueban despues de cada indexado. La memoria entera
# se recorre en unas cuantas ejecuciones sin que ninguna pague de golpe las
# peticiones de todos los libros que conoce.
LIBROS_POR_TANDA = 5

# Cuando se le pregunto por ultima vez a Web Search por cada libro. Es un
# turno, no un aval: una entrada se cree igual este comprobada o no, y esta
# fecha solo decide a quien le toca en la tanda siguiente. Por eso vive
# aparte y no dentro de los dos archivos de memoria.
RONDA_FILENAME = "book_ronda.json"


def _fecha(valor: object) -> Optional[date]:
    """La fecha de un campo de AirVault, o None si no se entiende."""
    iso = fecha_desde_airvault(valor)
    return date.fromisoformat(iso) if iso else None


def observaciones_de_paginas(paginas: Iterable[object]) -> List[Observacion]:
    """Lo que AirVault ya daba por bueno en las paginas que se leyeron.

    Recibe las :class:`app.airvault.client.PaginaIndexada` que el plan trajo
    del batch. Se queda con las que estan en verde y traen numero de
    bitacora: sin numero no se sabe de que libro hablan, y sin verde lo que
    se ve no es un indice sino la clasificacion del archivo entero.
    """
    vistas: Dict[str, Observacion] = {}
    for pagina in paginas:
        valores = getattr(pagina, "valores", None)
        if getattr(pagina, "estado", None) != ESTADO_VALIDO:
            continue
        if not isinstance(valores, Mapping):
            continue
        numero = normalizar_log_number(valores.get(CAMPO_LOG_NUMBER, ""))
        if not numero:
            continue
        matricula = normalizar_matricula(valores.get(CAMPO_MATRICULA, ""))
        fecha = _fecha(valores.get(CAMPO_END_DATE, ""))
        if not matricula and fecha is None:
            continue
        vistas[numero] = Observacion(
            log_number=numero,
            matricula=matricula,
            fecha=fecha,
            fuente="pagina en verde",
        )
    return list(vistas.values())


def bitacoras_del_libro(
    clave: str, cuantas: int = BITACORAS_POR_LIBRO
) -> List[str]:
    """Numeros de bitacora repartidos a lo largo de ese libro.

    Repartidos y no los primeros: de un libro que se publico a medias, las
    primeras paginas estan y las ultimas no, y mirando solo un extremo se
    saca la conclusion contraria a la del otro.
    """
    clave = str(clave or "").strip().upper()
    if len(clave) != 6 or not clave[:5].isdigit() or clave[5] not in "AB":
        return []
    inicio = 0 if clave[5] == "A" else PAGINAS_POR_LIBRO
    numeros = [
        f"{clave[:5]}{pagina:02d}"
        for pagina in range(inicio, inicio + PAGINAS_POR_LIBRO)
    ]
    return muestra_de(numeros, cuantas)


def observaciones_de_websearch(
    buscador: Buscador,
    claves: Sequence[str],
    cuantas: int = BITACORAS_POR_LIBRO,
    al_avanzar: Optional[Callable[[int, int], None]] = None,
) -> List[Observacion]:
    """Lo que Web Search publica de unas cuantas bitacoras de cada libro.

    Una bitacora que no aparece no dice nada y no se anota: puede que no
    este publicada o que la consulta no llegara. Solo se recoge lo que la
    respuesta trae con claridad.
    """
    observaciones: List[Observacion] = []
    total = len(claves)
    consultar = getattr(buscador, "consultar_indices", None)
    if callable(consultar):
        por_libro = [bitacoras_del_libro(clave, cuantas) for clave in claves]
        vistos: set[str] = set()
        hechos = 0

        def recibir(numero, _indice):
            nonlocal hechos
            vistos.add(numero)
            terminados = sum(all(n in vistos for n in grupo) for grupo in por_libro)
            while hechos < terminados:
                hechos += 1
                if al_avanzar is not None:
                    al_avanzar(hechos, total)

        indices = consultar(
            [numero for grupo in por_libro for numero in grupo],
            al_recibir=recibir,
        )
        for grupo in por_libro:
            for numero in grupo:
                indice = indices.get(numero)
                if indice is not None and indice.util:
                    observaciones.append(Observacion(
                        log_number=numero,
                        matricula=normalizar_matricula(indice.matricula),
                        fecha=_fecha(indice.fecha),
                        fuente="Web Search",
                    ))
        # Un libro con clave invalida no genera consultas, pero cuenta como
        # revisado igual que en el recorrido en serie.
        while hechos < total:
            hechos += 1
            if al_avanzar is not None:
                al_avanzar(hechos, total)
        return observaciones
    for hechos, clave in enumerate(claves, start=1):
        for numero in bitacoras_del_libro(clave, cuantas):
            indice = buscador.indice(numero)
            if indice is None or not indice.util:
                continue
            observaciones.append(Observacion(
                log_number=numero,
                matricula=normalizar_matricula(indice.matricula),
                fecha=_fecha(indice.fecha),
                fuente="Web Search",
            ))
        if al_avanzar is not None:
            al_avanzar(hechos, total)
    return observaciones


def _rutas(raiz: Path | str) -> Dict[str, Path]:
    """Los archivos de la instalacion que intervienen."""
    raiz = Path(raiz)
    return {
        "matriculas": raiz / BOOK_MATRICULAS_FILENAME,
        "fechas": raiz / BOOK_DATES_FILENAME,
        "flota": raiz / FLEET_FILENAME,
        "ronda": raiz / RONDA_FILENAME,
    }


def libros_de_la_memoria(raiz: Path | str) -> List[str]:
    """Las claves de libro que la memoria de esta instalacion conoce."""
    rutas = _rutas(raiz)
    return libros_guardados(rutas["matriculas"], rutas["fechas"])


def verificar_con_el_batch(
    paginas: Iterable[object], raiz: Path | str, escribir: bool = True
) -> Informe:
    """Comprueba la memoria con las paginas que el plan acaba de leer.

    Es la comprobacion que va sola: no pregunta nada, no pide red de mas y
    corre igual en el dry run que en el indexado, porque solo lee lo que ya
    estaba leido.
    """
    rutas = _rutas(raiz)
    # Aunque el batch no aporte ninguna pagina en verde se sigue: la
    # comprobacion contra la flota no necesita evidencia de nadie y es la
    # que descarta una matricula que no es de ningun avion.
    return verificar(
        observaciones_de_paginas(paginas),
        rutas["matriculas"],
        rutas["fechas"],
        flota=load_fleet(rutas["flota"]),
        escribir=escribir,
    )


def verificar_con_websearch(
    buscador: Buscador,
    raiz: Path | str,
    claves: Optional[Sequence[str]] = None,
    cuantas: int = BITACORAS_POR_LIBRO,
    escribir: bool = False,
    al_avanzar: Optional[Callable[[int, int], None]] = None,
) -> Informe:
    """Comprueba en Web Search los libros que la memoria ya conoce.

    Sin ``claves`` se revisan todos. Es la comprobacion que alcanza a los
    libros que no vienen en ningun batch de hoy, y la unica que ve las
    bitacoras que se publicaron sin este programa.
    """
    rutas = _rutas(raiz)
    if claves is None:
        claves = libros_de_la_memoria(raiz)
    if not claves:
        logger.info("Memoria de libros: no hay nada guardado que comprobar")
        return Informe()
    observaciones = observaciones_de_websearch(
        buscador, claves, cuantas, al_avanzar
    )
    return verificar(
        observaciones,
        rutas["matriculas"],
        rutas["fechas"],
        flota=load_fleet(rutas["flota"]),
        escribir=escribir,
    )


def _leer_ronda(ruta: Path) -> Dict[str, date]:
    """Los turnos anotados; un archivo dañado solo pierde el orden."""
    ruta = Path(ruta)
    if not ruta.is_file():
        return {}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"No se pudo leer la ronda de libros {ruta}: {exc}")
        return {}
    if not isinstance(datos, dict):
        logger.warning(f"Ronda de libros invalida: {ruta}")
        return {}
    turnos: Dict[str, date] = {}
    for clave, texto in datos.items():
        if not isinstance(clave, str) or not isinstance(texto, str):
            continue
        try:
            turnos[clave] = date.fromisoformat(texto)
        except ValueError:
            continue
    return turnos


def _guardar_ronda(
    ruta: Path, turnos: Mapping[str, date], conocidos: Iterable[str]
) -> None:
    """Anota los turnos de los libros que la memoria todavia conoce.

    Un libro que salio de la memoria (porque su matricula no era de ningun
    avion) tambien sale de la ronda: si volviera a aprenderse tendria que
    esperar su turno con un aval que ya no existe.
    """
    vivos = set(conocidos)
    datos = {
        clave: valor.isoformat()
        for clave, valor in sorted(turnos.items())
        if clave in vivos
    }
    try:
        Path(ruta).write_text(
            json.dumps(datos, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(f"No se pudo guardar la ronda de libros: {exc}")


def libros_por_verificar(
    raiz: Path | str, cuantos: int = LIBROS_POR_TANDA
) -> List[str]:
    """Los libros a los que les toca turno, del que lleva mas sin mirarse.

    El que nunca se consulto va primero. Entre los demas manda la fecha de
    la ultima consulta, y la clave desempata para que la tanda sea la misma
    aunque cambie el orden en que se leyeron los archivos.
    """
    if cuantos <= 0:
        return []
    rutas = _rutas(raiz)
    turnos = _leer_ronda(rutas["ronda"])
    claves = libros_guardados(rutas["matriculas"], rutas["fechas"])
    ordenados = sorted(claves, key=lambda c: (turnos.get(c, date.min), c))
    return ordenados[:cuantos]


def verificar_por_tandas(
    buscador: Buscador,
    raiz: Path | str,
    cuantos: int = LIBROS_POR_TANDA,
    cuantas: int = BITACORAS_POR_LIBRO,
    escribir: bool = True,
    al_avanzar: Optional[Callable[[int, int], None]] = None,
    hoy: Optional[date] = None,
) -> Informe:
    """Comprueba la tanda de libros a la que le toca turno.

    Es lo unico que alcanza a un libro que dejo de aparecer en los batches,
    que con el tiempo son casi todos: la comprobacion del plan solo ve los
    de hoy, asi que una entrada vieja y equivocada se quedaba inferiendo
    sobre las cincuenta paginas de su libro sin que nada la contrastara.

    Va por tandas para que ninguna ejecucion pague de golpe las peticiones
    de toda la memoria, y en unas cuantas las recorre todas.

    El turno se anota de los libros que se preguntaron, contestara Web
    Search o no. Anotar solo a los que contestan dejaria la cola atascada en
    los que no estan publicados, que son justo los que no van a contestar
    nunca, y los demas no llegarian a mirarse jamas.
    """
    rutas = _rutas(raiz)
    claves = libros_por_verificar(raiz, cuantos)
    if not claves:
        logger.info("Memoria de libros: no hay nada guardado que comprobar")
        return Informe()
    informe = verificar_con_websearch(
        buscador, raiz, claves, cuantas, escribir, al_avanzar
    )
    turnos = _leer_ronda(rutas["ronda"])
    turnos.update({clave: hoy or date.today() for clave in claves})
    _guardar_ronda(
        rutas["ronda"], turnos,
        libros_guardados(rutas["matriculas"], rutas["fechas"]),
    )
    return informe
