"""Consulta a Web Search para saber si una bitacora ya esta publicada.

Todas las defensas contra duplicados que existian antes miran la **cola**
de Web Index: los batches que estan esperando a que alguien los indexe. El
problema es que un batch completado sale de esa cola y se va a Web Search,
asi que a partir de ese momento el programa deja de verlo. Si la memoria
local se pierde o se borra a proposito, nada impide volver a subir lo mismo:
la cola esta limpia y el manifiesto ya no existe. Ese es justo el hueco por
el que se cuelan los duplicados.

Web Search si tiene esas bitacoras, y las tiene por su numero de bitacora,
que es la identidad de verdad: no depende de como se llamo el batch, ni de
en que carpeta se preparo, ni de si el reparto se rehizo.

**El endpoint no esta documentado.** AirVault no publica su API, asi que la
ruta de busqueda se descubre en ejecucion: se lee la portada de ``/zfp/``,
se sacan las rutas que aparecen en ella y en sus scripts, se prueban las que
parecen de busqueda y se conserva la que funciona en el JSON portable, para
no repetir el recorrido en cada consulta.

Un endpoint que no se ha probado no sirve para decir «esta bitacora no
esta»: una ruta equivocada tambien contesta que no hay nada. Por eso una
ruta solo se da por buena cuando encuentra un **control positivo**, un
numero de bitacora que el propio programa completo antes y que por lo tanto
tiene que estar publicado. Sin ese control, la respuesta se devuelve como
indeterminada y quien pregunta la trata como un «no se pudo comprobar», que
nunca autoriza nada.

De la misma consulta sale lo que Web Search tiene **escrito** de una
bitacora: su avion y su fecha (:meth:`Buscador.indice`). Con eso se
comprueba la memoria de libros contra el indice que la empresa da por
bueno, que es de lo que se ocupa :mod:`app.airvault.memoria`. Leer no pide
el mismo control que negar: encontrar la bitacora que se buscaba ya prueba
que la ruta mira donde hay que mirar, porque una ruta equivocada tendria
que devolver una fila con ese numero de siete digitos dentro.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from loguru import logger

from app.airvault.config import (
    CAMPO_LOG_NUMBER,
    AirVaultConfig,
    guardar_ruta_websearch,
)
from app.airvault.mapping import fecha_desde_airvault

# Portada del modulo. De aqui salen la ruta base y los scripts que se leen.
PORTADA = "/zfp/"

# Cuantas bitacoras del batch se consultan para decidir si ya esta subido.
# No hacen falta todas: si tres numeros distintos del mismo batch ya estan
# publicados, el batch esta publicado. Y si el batch se subio a medias, con
# tres repartidas por el se nota igual.
MUESTRA_POR_BATCH = 3

# Rutas que se prueban al final, cuando la portada no delata ninguna. Son
# las del patron de controlador que AirVault usa en el resto de sus modulos
# (``/index/Batch/GetBatches``, ``/quickuploadex/Home/GetRepositories``), y
# probarlas cuesta una peticion que se descarta sola si no contesta JSON.
RUTAS_CONOCIDAS = (
    "/zfp/Search/GetSearchResults",
    "/zfp/Search/Search",
    "/zfp/Search/GetResults",
    "/zfp/Home/Search",
    "/zfp/Home/GetSearchResults",
    "/zfp/Document/Search",
)

# Que en una ruta la delata como la busqueda y no como otra cosa del modulo.
_VERBOS = ("search", "query", "find", "result", "lookup")
# Lo que descarta una ruta aunque lleve uno de esos verbos: guardar una
# busqueda, borrarla o exportarla no es consultarla, y probarlas escribiria
# en la cuenta de la persona.
_PROHIBIDOS = (
    "save", "delete", "remove", "update", "insert", "create", "export",
    "download", "print", "email", "share", "upload",
)

_RUTA_EN_TEXTO = re.compile(r"[\"'](/?zfp/[A-Za-z0-9_/.\-]+)[\"']")
_SCRIPT_EN_HTML = re.compile(
    r"<script[^>]+src\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)

# Con cuantas bitacoras distintas se intenta descubrir la ruta cuando no hay
# un control sabido de antemano. Si ninguna aparece, o la ruta no es esa o
# esos libros no estan publicados, y en los dos casos seguir probando cuesta
# una vuelta entera de peticiones por cada numero.
LIMITE_DE_TANTEOS = 5

# Lo que delata a cada campo dentro de una fila de resultados. La matricula
# se reconoce por su forma y no por el nombre de su columna: la forma es la
# misma en cualquier instalacion y ningun otro indice del repositorio se le
# parece, mientras que los nombres de columna son justo lo que esta consulta
# ya tiene que descubrir sola.
_MATRICULA_EN_TEXTO = re.compile(r"\b(?:HP|HK)-\d{4}(?:CMP|WWP)\b")
# La fecha si necesita el nombre de la columna: una fila puede traer varias
# (la de inicio, la de recepcion del batch) y quedarse con cualquiera
# pondria en la memoria una fecha que no es la de la bitacora.
_CLAVE_DE_FECHA = re.compile(r"end.?date|fecha.?fin", re.IGNORECASE)


def _b64(texto: str) -> str:
    return base64.b64encode(str(texto).encode("utf-8")).decode("ascii")


@dataclass(frozen=True)
class Plantilla:
    """Una forma de preguntar: como se llaman los parametros de la ruta.

    AirVault no usa los mismos nombres en todos sus modulos, asi que en vez
    de adivinar uno se prueban los que ya se le conocen. La que conteste se
    guarda junto con la ruta y no se vuelve a probar.
    """

    nombre: str
    # ``valor`` es el numero de bitacora que se busca; ``config`` da repoId.
    construir: Callable[[str, AirVaultConfig], Dict[str, object]]


def _plantillas() -> List[Plantilla]:
    """Las formas conocidas de mandar una busqueda por Log Page Number."""
    campo = CAMPO_LOG_NUMBER
    return [
        Plantilla(
            "encodedValues",
            lambda valor, config: {
                "repoId": config.repo_id,
                "encodedValues": _b64(f"{campo}={valor}"),
                "rows": 50, "page": 1,
            },
        ),
        Plantilla(
            "encodedKeywordFilter",
            lambda valor, config: {
                "repoId": config.repo_id,
                "encodedKeywordFilter": _b64(str(valor)),
                "encodedFilter": "",
                "rows": 50, "page": 1,
            },
        ),
        Plantilla(
            "fieldId",
            lambda valor, config: {
                "repoId": config.repo_id,
                "fieldId": campo,
                "value": valor,
                "rows": 50, "page": 1,
            },
        ),
        Plantilla(
            "searchText",
            lambda valor, config: {
                "repoId": config.repo_id,
                "searchText": valor,
                "rows": 50, "page": 1,
            },
        ),
    ]


PLANTILLAS = {plantilla.nombre: plantilla for plantilla in _plantillas()}


def _ruta_absoluta(ruta: str) -> str:
    ruta = str(ruta or "").strip()
    if not ruta:
        return ""
    if ruta.startswith("//") or "://" in ruta:
        return ""
    return ruta if ruta.startswith("/") else f"/{ruta}"


def _parece_busqueda(ruta: str) -> bool:
    bajo = ruta.lower()
    if any(malo in bajo for malo in _PROHIBIDOS):
        return False
    if bajo.endswith((".js", ".css", ".png", ".gif", ".jpg", ".svg", ".map")):
        return False
    return any(verbo in bajo for verbo in _VERBOS)


def _rutas_del_texto(texto: str) -> List[str]:
    """Rutas de ``/zfp/`` que aparecen escritas en una pagina o un script."""
    vistas: List[str] = []
    for hallada in _RUTA_EN_TEXTO.findall(texto or ""):
        ruta = _ruta_absoluta(hallada)
        if ruta and _parece_busqueda(ruta) and ruta not in vistas:
            vistas.append(ruta)
    return vistas


def candidatas(sesion, limite_scripts: int = 12) -> List[str]:
    """Rutas de busqueda que la portada de Web Search deja ver.

    Primero las que estan escritas en la propia pagina, despues las de sus
    scripts, y al final las que ya se le conocen al patron de AirVault. El
    orden importa: lo que el modulo declara de si mismo vale mas que una
    lista escrita aqui, que envejece.
    """
    encontradas: List[str] = []

    def sumar(rutas: Iterable[str]) -> None:
        for ruta in rutas:
            if ruta not in encontradas:
                encontradas.append(ruta)

    try:
        portada = sesion.get(PORTADA, json_esperado=False)
    except Exception as exc:  # noqa: BLE001 - sin portada quedan las conocidas
        logger.info("No se pudo leer la portada de Web Search: {}", exc)
        portada = ""
    if not isinstance(portada, str):
        portada = ""
    sumar(_rutas_del_texto(portada))

    for src in _SCRIPT_EN_HTML.findall(portada)[:limite_scripts]:
        ruta = _ruta_absoluta(src.split("?", 1)[0])
        if not ruta or not ruta.lower().endswith(".js"):
            continue
        try:
            guion = sesion.get(ruta, json_esperado=False)
        except Exception as exc:  # noqa: BLE001 - un script menos, no un fallo
            logger.debug("No se pudo leer {}: {}", ruta, exc)
            continue
        sumar(_rutas_del_texto(guion if isinstance(guion, str) else ""))

    sumar(RUTAS_CONOCIDAS)
    return encontradas


def _textos(datos: object, profundidad: int = 0) -> Iterable[str]:
    """Todo lo que en una respuesta puede ser un valor de campo."""
    if profundidad > 6:
        return
    if isinstance(datos, str):
        yield datos
    elif isinstance(datos, bool):
        return
    elif isinstance(datos, (int, float)):
        yield str(datos)
    elif isinstance(datos, Mapping):
        for valor in datos.values():
            yield from _textos(valor, profundidad + 1)
    elif isinstance(datos, (list, tuple)):
        for valor in datos:
            yield from _textos(valor, profundidad + 1)


def _aparece(numero: str, datos: object) -> bool:
    """Si la respuesta trae ese numero de bitacora como un valor entero.

    Se compara contra los valores y no contra el JSON en crudo: un numero
    de siete digitos aparece por casualidad dentro de un identificador
    largo, y eso daria por publicada una bitacora que no lo esta.
    """
    buscado = str(numero).strip()
    if not buscado:
        return False
    for texto in _textos(datos):
        if buscado == str(texto).strip():
            return True
    return False


def _contenedores(
    datos: object, numero: str, profundidad: int = 0
) -> List[Mapping]:
    """Los registros mas pequenos de la respuesta que traen ese numero.

    Una respuesta de busqueda envuelve cada fila en varias capas, y la capa
    de arriba las contiene todas. Quedarse con la mas pequena que contiene
    el numero es quedarse con la fila de esa bitacora y no con las de al
    lado, que son de otros aviones.
    """
    if profundidad > 6:
        return []
    if isinstance(datos, Mapping):
        dentro: List[Mapping] = []
        for valor in datos.values():
            dentro.extend(_contenedores(valor, numero, profundidad + 1))
        if dentro:
            return dentro
        return [datos] if _aparece(numero, datos) else []
    if isinstance(datos, (list, tuple)):
        sueltos: List[Mapping] = []
        for valor in datos:
            sueltos.extend(_contenedores(valor, numero, profundidad + 1))
        return sueltos
    return []


def _fechas_de(fila: Mapping) -> List[str]:
    """Fechas de la fila, con la de la bitacora delante si se distingue."""
    preferidas: List[str] = []
    todas: List[str] = []
    for clave, valor in fila.items():
        for texto in _textos(valor):
            fecha = fecha_desde_airvault(texto)
            if not fecha:
                continue
            if fecha not in todas:
                todas.append(fecha)
            if _CLAVE_DE_FECHA.search(str(clave)) and fecha not in preferidas:
                preferidas.append(fecha)
    return preferidas or todas


@dataclass(frozen=True)
class Indice:
    """Los indices que Web Search muestra de una bitacora publicada."""

    numero: str
    matricula: str = ""
    fecha: str = ""

    @property
    def util(self) -> bool:
        return bool(self.matricula or self.fecha)


def indice_en(datos: object, numero: str) -> Optional[Indice]:
    """Lo que la respuesta dice de esa bitacora, o None si no la trae.

    Un valor solo se da por bueno cuando la fila entera esta de acuerdo:
    dos matriculas distintas para la misma bitacora no son un dato, son un
    motivo para no tocar nada. Es la misma cautela que con la ruta: de Web
    Search se aprovecha lo que contesta con claridad y lo demas se queda
    como no comprobado.
    """
    filas = _contenedores(datos, numero)
    if not filas:
        return None
    matriculas: List[str] = []
    fechas: List[str] = []
    for fila in filas:
        for texto in _textos(fila):
            encontrada = _MATRICULA_EN_TEXTO.search(str(texto).upper())
            if encontrada is None or encontrada.group(0) in matriculas:
                continue
            matriculas.append(encontrada.group(0))
        for fecha in _fechas_de(fila):
            if fecha not in fechas:
                fechas.append(fecha)
    return Indice(
        numero=numero,
        matricula=matriculas[0] if len(matriculas) == 1 else "",
        fecha=fechas[0] if len(fechas) == 1 else "",
    )


@dataclass
class Consulta:
    """Como quedo una pregunta a Web Search."""

    # ``True`` publicada, ``False`` no publicada, ``None`` no se pudo saber.
    publicada: Optional[bool]
    motivo: str = ""


@dataclass
class Buscador:
    """La conexion con Web Search, con su ruta ya probada o por probar.

    Se construye una vez por ejecucion y se reutiliza: descubrir la ruta
    cuesta varias peticiones y no cambia entre batches.
    """

    sesion: object
    config: AirVaultConfig
    ruta_config: Optional[Path] = None
    # Numeros de bitacora que se sabe publicados, para probar la ruta. Sin
    # al menos uno, ninguna respuesta negativa se da por buena.
    controles: Sequence[str] = ()
    _ruta: str = ""
    _plantilla: str = ""
    _probado: bool = False
    # Si la ruta se adopto con la bitacora que se estaba buscando en vez de
    # con un control sabido de antemano (ver :meth:`_adoptar_ruta`). Sirve
    # para leer lo que contesta, no para dar por buena una ausencia.
    _sin_control: bool = False
    _motivo: str = ""
    _memoria: Dict[str, bool] = field(default_factory=dict)
    # Descubrir las rutas cuesta leer la portada y sus scripts, y no cambia
    # entre consultas: se hace una vez y se conserva la lista.
    _candidatas: List[str] = field(default_factory=list)
    _tanteos: int = 0

    # ── ruta ───────────────────────────────────────────────────────

    def _guardada(self) -> tuple[str, str]:
        ruta = str(self.config.ruta_websearch or "").strip()
        plantilla = str(self.config.parametros_websearch or "").strip()
        if ruta and plantilla in PLANTILLAS:
            return ruta, plantilla
        return "", ""

    def _pedir(self, ruta: str, plantilla: str, valor: str):
        forma = PLANTILLAS[plantilla]
        return self.sesion.get(ruta, forma.construir(valor, self.config))

    def _encuentra(
        self, ruta: str, plantilla: str, numero: str
    ) -> Optional[bool]:
        """Si esa ruta devuelve esa bitacora. None si ni siquiera contesto."""
        try:
            datos = self._pedir(ruta, plantilla, numero)
        except Exception as exc:  # noqa: BLE001 - se prueba la siguiente
            logger.debug("Web Search {} ({}): {}", ruta, plantilla, exc)
            return None
        return _aparece(numero, datos)

    def _probar(self, ruta: str, plantilla: str) -> bool:
        """Si esa ruta encuentra un control positivo.

        Encontrarlo es la unica prueba que sirve. Que conteste JSON solo
        dice que la ruta existe, no que este mirando donde hay que mirar.
        """
        for control in self.controles:
            encontrado = self._encuentra(ruta, plantilla, control)
            if encontrado is None:
                return False
            if encontrado:
                return True
        return False

    def _candidatas_de_la_portada(self) -> List[str]:
        """Las rutas por probar, descubiertas una sola vez."""
        if not self._candidatas:
            self._candidatas = candidatas(self.sesion)
        return self._candidatas

    def _adoptar_ruta(self, numero: str) -> bool:
        """Busca una ruta que encuentre esa bitacora y la conserva.

        Es el mismo descubrimiento de :meth:`preparar` con la bitacora que
        se esta consultando como control. Encontrarla prueba que la ruta
        mira donde hay que mirar, asi que sirve para leer sus indices. No
        sirve para lo contrario: para afirmar que una bitacora **no** esta
        publicada hace falta un control sabido de antemano, y por eso
        :meth:`preparar` no se conforma con esto.
        """
        if self._tanteos >= LIMITE_DE_TANTEOS:
            return False
        self._tanteos += 1
        ruta, plantilla = self._guardada()
        if ruta and self._encuentra(ruta, plantilla, numero) is True:
            self._ruta, self._plantilla = ruta, plantilla
            self._sin_control = True
            return True
        for candidata in self._candidatas_de_la_portada():
            for nombre in PLANTILLAS:
                if self._encuentra(candidata, nombre, numero) is not True:
                    continue
                self._ruta, self._plantilla = candidata, nombre
                self._sin_control = True
                logger.info(
                    "Web Search responde en {} con {}", candidata, nombre
                )
                if self.ruta_config is not None:
                    guardar_ruta_websearch(self.ruta_config, candidata, nombre)
                return True
        return False

    def preparar(self) -> bool:
        """Deja lista la ruta de busqueda; dice si se puede consultar.

        Solo dice que si cuando la ruta paso un control positivo. Que haya
        una ruta no basta: :meth:`indice` adopta la que devuelve la propia
        bitacora que estaba buscando, y esa sirve para leer lo que
        contesta, no para dar por buena la ausencia de nada.
        """
        if self._probado:
            return bool(self._ruta) and not self._sin_control
        self._probado = True
        if not self.controles:
            self._motivo = (
                "todavia no hay ninguna bitacora completada con la que "
                "comprobar que la busqueda responde"
            )
            return False
        ruta, plantilla = self._guardada()
        if ruta and self._probar(ruta, plantilla):
            self._ruta, self._plantilla = ruta, plantilla
            self._sin_control = False
            return True
        for candidata in self._candidatas_de_la_portada():
            for nombre in PLANTILLAS:
                if self._probar(candidata, nombre):
                    self._ruta, self._plantilla = candidata, nombre
                    self._sin_control = False
                    logger.info(
                        "Web Search responde en {} con {}", candidata, nombre
                    )
                    if self.ruta_config is not None:
                        guardar_ruta_websearch(
                            self.ruta_config, candidata, nombre
                        )
                    return True
        self._motivo = (
            "no se encontro en Web Search ninguna consulta que devuelva las "
            "bitacoras ya publicadas"
        )
        logger.info("Web Search: {}", self._motivo)
        return False

    @property
    def ruta(self) -> str:
        return self._ruta

    @property
    def motivo(self) -> str:
        return self._motivo

    # ── consulta ───────────────────────────────────────────────────

    def publicada(self, numero: str) -> Consulta:
        """Si esa bitacora ya esta en Web Search."""
        numero = str(numero or "").strip()
        if not numero:
            return Consulta(None, "la bitacora no trae numero")
        if numero in self._memoria:
            return Consulta(self._memoria[numero])
        if not self.preparar():
            return Consulta(None, self._motivo)
        try:
            datos = self._pedir(self._ruta, self._plantilla, numero)
        except Exception as exc:  # noqa: BLE001 - una consulta que no sale
            motivo = f"Web Search no contesto: {exc}"
            logger.info(motivo)
            return Consulta(None, motivo)
        resultado = _aparece(numero, datos)
        self._memoria[numero] = resultado
        return Consulta(resultado)

    def renovar_consultas(self) -> None:
        """Una revision manual debe ver lo publicado desde la consulta anterior."""
        self._memoria.clear()

    def indice(self, numero: str) -> Optional[Indice]:
        """Los indices que Web Search tiene de esa bitacora, si la tiene.

        Devuelve None cuando no aparece y tambien cuando no se pudo
        preguntar: aqui las dos cosas valen igual, porque de esta consulta
        solo se actua sobre lo que aparece. Una ruta equivocada no puede
        inventar un resultado; tendria que devolver una fila con ese numero
        de siete digitos dentro.
        """
        numero = str(numero or "").strip()
        if not numero:
            return None
        if (
            not self._ruta
            and not self.preparar()
            and not self._adoptar_ruta(numero)
        ):
            return None
        try:
            datos = self._pedir(self._ruta, self._plantilla, numero)
        except Exception as exc:  # noqa: BLE001 - una consulta que no sale
            logger.info("Web Search no contesto por {}: {}", numero, exc)
            return None
        encontrado = indice_en(datos, numero)
        if encontrado is not None:
            # Que aparezca si vale para las dos preguntas: la fila trae el
            # numero entero, y eso no lo devuelve una ruta equivocada. Que
            # no aparezca no se anota: por esta via la ruta puede no haber
            # pasado un control positivo, y entonces la ausencia no dice
            # nada.
            self._memoria[numero] = True
        return encontrado


def muestra_de(
    numeros: Sequence[str], cuantas: int = MUESTRA_POR_BATCH
) -> List[str]:
    """Bitacoras repartidas a lo largo del batch, no las primeras.

    Un batch que se subio a medias tiene publicadas las de arriba y no las
    de abajo. Mirando solo el principio se daria por publicado entero; y
    mirando solo el final, por no publicado. Repartidas se nota cual de las
    dos cosas paso.
    """
    unicos: List[str] = []
    for numero in numeros:
        limpio = str(numero or "").strip()
        if limpio and limpio not in unicos:
            unicos.append(limpio)
    if cuantas <= 0:
        return []
    if len(unicos) <= cuantas:
        return unicos
    if cuantas == 1:
        return [unicos[0]]
    paso = (len(unicos) - 1) / (cuantas - 1)
    return [unicos[round(indice * paso)] for indice in range(cuantas)]


@dataclass
class Veredicto:
    """Que dice Web Search de las bitacoras de un batch."""

    # Numeros consultados que ya estaban publicados.
    publicadas: List[str] = field(default_factory=list)
    # Numeros consultados que no aparecieron.
    ausentes: List[str] = field(default_factory=list)
    # Por que no se pudo consultar, si es que no se pudo.
    motivo: str = ""

    @property
    def concluyente(self) -> bool:
        """Si la consulta llego a responder algo de lo que fiarse."""
        return bool(self.publicadas or self.ausentes)

    @property
    def ya_publicado(self) -> bool:
        """Si hay que dar el batch por subido y no volver a mandarlo."""
        return bool(self.publicadas)

    def resumen(self, cuantas: int = 3) -> str:
        if self.publicadas:
            lista = ", ".join(self.publicadas[:cuantas])
            resto = len(self.publicadas) - cuantas
            cola = f" y {resto} mas" if resto > 0 else ""
            return f"Web Search ya tiene publicadas las bitacoras {lista}{cola}"
        if self.ausentes:
            return "Web Search no tiene ninguna de las bitacoras consultadas"
        return self.motivo or "no se pudo consultar Web Search"


def revisar_batch(
    buscador: Buscador,
    numeros: Sequence[str],
    cuantas: int = MUESTRA_POR_BATCH,
) -> Veredicto:
    """Consulta unas cuantas bitacoras del batch y resume lo que salio."""
    muestra = muestra_de(numeros, cuantas)
    if not muestra:
        return Veredicto(
            motivo="el batch no trae numeros de bitacora que buscar"
        )
    veredicto = Veredicto()
    for numero in muestra:
        consulta = buscador.publicada(numero)
        if consulta.publicada is True:
            veredicto.publicadas.append(numero)
        elif consulta.publicada is False:
            veredicto.ausentes.append(numero)
        elif not veredicto.motivo:
            veredicto.motivo = consulta.motivo
    return veredicto


def numeros_de(trabajo) -> List[str]:
    """Los numeros de bitacora que lleva un batch, en su orden."""
    return [
        str(registro.log_number).strip()
        for registro in trabajo.manifiesto.registros
        if not registro.es_separador and str(registro.log_number or "").strip()
    ]
