"""Sincroniza la memoria de libros con la carpeta compartida.

La memoria (``book_matriculas.json`` y ``book_fechas.json``) es lo que hace
que una matrícula o una fecha leída una vez sirva para las cincuenta páginas
del libro. Cada instalación la aprende sola, así que dos personas que
procesan libros distintos acaban sabiendo cosas distintas. Con una ruta
remota activa (:mod:`app.utils.ruta_remota`) las dos memorias se juntan en
el servidor y cada equipo se queda con una copia completa.

La copia local sigue siendo la de trabajo. El corrector y el aprendizaje
leen y escriben los archivos de siempre, junto al programa, y este módulo
los pone al día con el servidor antes de corregir y después de aprender.
Por eso la inferencia funciona igual sin red: con el servidor caído se usa
lo último que se trajo, y lo que se aprenda mientras tanto sube en la
siguiente sincronización.

Cómo se resuelve cada libro, comparando lo de aquí, lo del servidor y lo
que el servidor decía la última vez que se sincronizó (la base, que se
guarda en ``book_remoto.json`` junto al programa):

* lo que solo tiene un lado pasa al otro: nadie pierde lo que aprendió;
* si los dos tienen el libro y no coinciden, **manda el servidor**. Es la
  copia de todos y una lectura suelta de un equipo no la reemplaza;
* la excepción es una entrada que aquí cambió después de la última
  sincronización mientras el servidor seguía igual. Eso solo lo hacen el
  aprendizaje con respaldo suficiente y la comprobación contra AirVault,
  que es la única salida de una entrada equivocada (ver
  :mod:`app.validation.book_memory`): esa corrección sí sube. Sin esta
  regla el servidor devolvería el error en cada sincronización y nadie
  podría arreglarlo nunca;
* en las fechas, dos entradas distintas que no se contradicen se unen y
  quedan los extremos de las dos, que es lo que haría el aprendizaje si
  hubiera leído todas esas páginas.

Lo que protege al servidor:

* nunca se borra una entrada suya. Que un equipo descarte un libro (porque
  su lista de flota no trae ese avión) se queda en ese equipo;
* que al servidor le falte una entrada tampoco borra la de aquí: se vuelve
  a subir. Un archivo que alguien vació o que quedó dañado se repone con lo
  que cada equipo conserva, en vez de vaciar la memoria de todos;
* un archivo del servidor que no se puede leer no se sobrescribe: se deja
  como está y se avisa;
* cada entrada del servidor que se reemplaza queda anotada en
  ``book_cambios.jsonl``, con el valor anterior, quién y cuándo;
* las escrituras van de una en una, con un candado en la propia carpeta,
  y de una sola pieza.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from loguru import logger

from app.utils import ruta_remota
from app.validation.book_corrector import (
    BOOK_MATRICULAS_FILENAME,
    _load_book_matriculas,
    _save_book_matriculas,
)
from app.validation.date_corrector import (
    BOOK_DATES_FILENAME,
    RegistryAnchor,
    _extremes,
    _load_book_dates,
    _save_book_dates,
)

# Lo que el servidor decía la última vez. Local de cada instalación.
BASE_FILENAME = "book_remoto.json"
# En el servidor: el turno de escritura y el diario de reemplazos.
CANDADO_FILENAME = "book_memoria.lock"
CAMBIOS_FILENAME = "book_cambios.jsonl"

# Cuánto se espera el turno. Sincronizar son dos archivos pequeños: si en
# este tiempo no se libera, se deja para la próxima y se sigue con lo local.
ESPERA_CANDADO_S = 10.0
# Un candado más viejo que esto es de un programa que se cerró a la mitad.
CANDADO_VIEJO_S = 120.0

Entradas = Dict[str, Any]
Combinar = Callable[[Any, Any], Optional[Any]]


class CandadoOcupado(OSError):
    """Otro equipo está sincronizando y no soltó el turno a tiempo."""


class ServidorIlegible(OSError):
    """Un archivo de memoria del servidor no se deja leer; no se toca."""


@dataclass(frozen=True)
class Reemplazo:
    """Una entrada del servidor que cambió de valor."""

    archivo: str
    clave: str
    antes: Any
    despues: Any


@dataclass
class Resumen:
    """Qué hizo una sincronización, para el log y para quien la pidió."""

    subidos: int = 0
    traidos: int = 0
    reemplazos: List[Reemplazo] = field(default_factory=list)

    @property
    def hay_cambios(self) -> bool:
        return bool(self.subidos or self.traidos or self.reemplazos)

    def __str__(self) -> str:
        if not self.hay_cambios:
            return "Memoria de libros compartida: al día"
        partes = []
        # Entradas y no libros: un libro tiene una en cada archivo.
        if self.subidos:
            partes.append(f"{self.subidos} entrada(s) subidas al servidor")
        if self.traidos:
            partes.append(f"{self.traidos} entrada(s) traídas del servidor")
        if self.reemplazos:
            partes.append(
                f"{len(self.reemplazos)} entrada(s) del servidor actualizadas"
            )
        return "Memoria de libros compartida: " + ", ".join(partes)


def _conciliar(
    archivo: str,
    local: Entradas,
    servidor: Entradas,
    base: Entradas,
    resumen: Resumen,
    combinar: Optional[Combinar] = None,
    intocables: frozenset = frozenset(),
) -> Tuple[Entradas, Entradas]:
    """Lo que debe quedar aquí y en el servidor, libro por libro.

    ``intocables`` son los libros que el servidor tiene escritos de una
    forma que esta versión no entiende. No se sabe qué dicen, así que no se
    les sube nada encima.
    """
    nuevo_local: Entradas = {}
    nuevo_servidor: Entradas = dict(servidor)
    for clave in sorted(set(local) | set(servidor)):
        aqui = local.get(clave)
        alla = servidor.get(clave)
        antes = base.get(clave)
        if alla is None and clave in intocables:
            nuevo_local[clave] = aqui
        elif alla is None:
            # Solo está aquí. Aunque el servidor la tuviera antes, no se
            # toma su falta por una orden de borrar: se repone.
            nuevo_local[clave] = aqui
            nuevo_servidor[clave] = aqui
            resumen.subidos += 1
        elif aqui is None:
            if antes is not None and alla == antes:
                # Este equipo la descartó y el servidor no ha dicho nada
                # nuevo: ni se borra allí ni se vuelve a traer.
                continue
            nuevo_local[clave] = alla
            resumen.traidos += 1
        elif aqui == alla:
            nuevo_local[clave] = aqui
        elif antes is not None and alla == antes:
            # Corrección hecha aquí después de la última sincronización.
            nuevo_local[clave] = aqui
            nuevo_servidor[clave] = aqui
            resumen.reemplazos.append(Reemplazo(archivo, clave, alla, aqui))
        elif antes is not None and aqui == antes:
            nuevo_local[clave] = alla
            resumen.traidos += 1
        else:
            # Los dos cambiaron, o es la primera vez que se juntan.
            unido = combinar(alla, aqui) if combinar is not None else None
            if unido is None:
                unido = alla
            nuevo_local[clave] = unido
            if unido != alla:
                nuevo_servidor[clave] = unido
                resumen.reemplazos.append(
                    Reemplazo(archivo, clave, alla, unido)
                )
            if unido != aqui:
                resumen.traidos += 1
    return nuevo_local, nuevo_servidor


def _fechas_a_texto(
    registro: Dict[str, List[RegistryAnchor]]
) -> Entradas:
    """El registro de fechas tal como se escribe en el archivo."""
    return {
        clave: {
            f"{logpage:02d}": valor.isoformat()
            for logpage, valor in sorted(anclas)
        }
        for clave, anclas in registro.items()
    }


def _anclas(entrada: Dict[str, str]) -> List[RegistryAnchor]:
    return sorted(
        (int(logpage), date.fromisoformat(texto))
        for logpage, texto in entrada.items()
    )


def _fechas_de_texto(crudo: Entradas) -> Dict[str, List[RegistryAnchor]]:
    return {clave: _anclas(entrada) for clave, entrada in crudo.items()}


def _unir_fechas(
    servidor: Dict[str, str], local: Dict[str, str]
) -> Optional[Dict[str, str]]:
    """Los extremos de las dos entradas, o nada si se contradicen.

    Se contradicen cuando la misma página tiene dos fechas o cuando juntas
    obligarían al libro a retroceder. Ahí no hay forma de saber cuál de las
    dos describe el libro, y quien llama se queda con la del servidor.
    """
    unidas: Dict[str, str] = dict(servidor)
    for logpage, texto in local.items():
        if unidas.setdefault(logpage, texto) != texto:
            return None
    ordenadas = _anclas(unidas)
    if any(
        posterior < anterior
        for (_a, anterior), (_b, posterior) in zip(ordenadas, ordenadas[1:])
    ):
        return None
    return {
        f"{logpage:02d}": valor.isoformat()
        for logpage, valor in _extremes(ordenadas)
    }


def _leer_crudo(ruta: Path) -> Entradas:
    """El archivo del servidor tal cual está escrito; vacío si no existe.

    Falla si existe y no es un JSON de libros. Los lectores de la memoria
    devuelven un mapa vacío ante un archivo dañado, que está bien para no
    detener una ejecución y muy mal aquí: un servidor «vacío» se rellenaría
    con lo local y el archivo dañado, que quizá alguien podía recuperar,
    quedaría sobrescrito.
    """
    if not ruta.exists():
        return {}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ServidorIlegible(f"{ruta.name}: {exc}") from exc
    if not isinstance(datos, dict):
        raise ServidorIlegible(f"{ruta.name}: no es un mapa de libros")
    return datos


def _escribir_servidor(
    ruta: Path, crudo: Entradas, entendidas: Entradas, nuevas: Entradas
) -> None:
    """Escribe la memoria del servidor sin perder lo que no se entendió.

    Los lectores descartan las entradas que esta versión no reconoce, y
    reescribir solo lo reconocido las borraría para todos. Otra versión del
    programa puede haberlas escrito con razón, así que se conservan como
    estaban. El formato es el de siempre: compacto, ordenado y de una pieza.
    """
    ajenas = {
        clave: valor for clave, valor in crudo.items()
        if clave not in entendidas
    }
    temporal = ruta.with_name(f"{ruta.name}.tmp")
    temporal.write_text(
        json.dumps(
            dict(sorted({**ajenas, **nuevas}.items())),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporal, ruta)


def _leer_base(ruta: Path, remota: Path) -> Tuple[Entradas, Entradas]:
    """Lo que el servidor decía la última vez, si era este mismo servidor."""
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    if (
        not isinstance(datos, dict)
        or str(datos.get("ruta", "")).casefold() != str(remota).casefold()
    ):
        # Otra carpeta compartida no tiene historia con esta instalación:
        # se empieza como la primera vez.
        return {}, {}
    matriculas = datos.get("matriculas")
    fechas = datos.get("fechas")
    return (
        matriculas if isinstance(matriculas, dict) else {},
        fechas if isinstance(fechas, dict) else {},
    )


def _guardar_base(
    ruta: Path, remota: Path, matriculas: Entradas, fechas: Entradas
) -> None:
    temporal = ruta.with_name(f"{ruta.name}.tmp")
    temporal.write_text(
        json.dumps(
            {
                "ruta": str(remota),
                "matriculas": dict(sorted(matriculas.items())),
                "fechas": dict(sorted(fechas.items())),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporal, ruta)


def _quien() -> Dict[str, str]:
    """Equipo y usuario de Windows, para el candado y el diario."""
    try:
        usuario = getpass.getuser()
    except Exception:  # noqa: BLE001 - sin usuario se anota vacío
        usuario = ""
    return {"equipo": platform.node(), "usuario": usuario}


@contextmanager
def _turno(remota: Path, espera_s: float = ESPERA_CANDADO_S) -> Iterator[None]:
    """El turno de escritura en la memoria del servidor.

    Crear un archivo que no existía es lo único que una carpeta de red
    garantiza de una sola pieza, así que el candado es eso. Si quien lo
    puso se cerró a la mitad, el archivo envejece y el siguiente lo retira.
    """
    candado = remota / CANDADO_FILENAME
    limite = time.monotonic() + espera_s
    while True:
        try:
            descriptor = os.open(
                candado, os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
        except FileExistsError:
            try:
                edad = time.time() - candado.stat().st_mtime
            except OSError:
                edad = 0.0
            if edad > CANDADO_VIEJO_S:
                try:
                    candado.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= limite:
                raise CandadoOcupado(
                    "otro equipo está sincronizando la memoria de libros"
                ) from None
            time.sleep(0.25)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            archivo.write(json.dumps(_quien()))
        break
    try:
        yield
    finally:
        try:
            candado.unlink()
        except OSError:
            pass


def _anotar_reemplazos(remota: Path, reemplazos: List[Reemplazo]) -> None:
    """Deja escrito qué decía el servidor antes de cada cambio."""
    if not reemplazos:
        return
    quien = _quien()
    cuando = datetime.now().isoformat(timespec="seconds")
    lineas = [
        json.dumps(
            {
                "cuando": cuando,
                **quien,
                "archivo": cambio.archivo,
                "libro": cambio.clave,
                "antes": cambio.antes,
                "despues": cambio.despues,
            },
            ensure_ascii=True,
        )
        for cambio in reemplazos
    ]
    with (remota / CAMBIOS_FILENAME).open("a", encoding="utf-8") as diario:
        diario.write("\n".join(lineas) + "\n")


def sincronizar_en(
    remota: Path | str,
    matriculas_path: Path | str,
    fechas_path: Path | str,
) -> Resumen:
    """Junta la memoria local con la de esa carpeta y escribe las dos.

    Lanza ``OSError`` si el servidor no se deja leer o escribir; en ese caso
    no queda nada a medias en ningún lado.
    """
    remota = Path(remota)
    matriculas_path = Path(matriculas_path)
    fechas_path = Path(fechas_path)
    matriculas_remota = remota / BOOK_MATRICULAS_FILENAME
    fechas_remota = remota / BOOK_DATES_FILENAME
    resumen = Resumen()
    try:
        misma = matriculas_remota.resolve() == matriculas_path.resolve()
    except OSError:
        misma = False
    if misma:
        # La ruta remota es la propia carpeta del programa: ya es una sola.
        return resumen
    base_path = matriculas_path.with_name(BASE_FILENAME)
    with _turno(remota):
        crudo_m = _leer_crudo(matriculas_remota)
        crudo_f = _leer_crudo(fechas_remota)
        base_matriculas, base_fechas = _leer_base(base_path, remota)

        local_m = _load_book_matriculas(matriculas_path)
        local_f = _fechas_a_texto(_load_book_dates(fechas_path))
        # Una memoria local vacía no es una que descartó todos sus libros:
        # es una instalación nueva o un archivo que se perdió. La base no
        # cuenta ahí, o impediría traer justo lo que hace falta.
        if not local_m:
            base_matriculas = {}
        if not local_f:
            base_fechas = {}

        servidor_m = _load_book_matriculas(matriculas_remota)
        nuevo_local_m, nuevo_servidor_m = _conciliar(
            "matriculas", local_m, servidor_m, base_matriculas, resumen,
            intocables=frozenset(crudo_m) - frozenset(servidor_m),
        )

        servidor_f = _fechas_a_texto(_load_book_dates(fechas_remota))
        nuevo_local_f, nuevo_servidor_f = _conciliar(
            "fechas", local_f, servidor_f, base_fechas, resumen,
            combinar=_unir_fechas,
            intocables=frozenset(crudo_f) - frozenset(servidor_f),
        )

        # Primero el diario: si algo falla después, sobra una línea, que es
        # mejor que un valor reemplazado del que no quedó rastro.
        _anotar_reemplazos(remota, resumen.reemplazos)
        if nuevo_servidor_m != servidor_m:
            _escribir_servidor(
                matriculas_remota, crudo_m, servidor_m, nuevo_servidor_m
            )
        if nuevo_servidor_f != servidor_f:
            _escribir_servidor(
                fechas_remota, crudo_f, servidor_f, nuevo_servidor_f
            )
    if nuevo_local_m != local_m:
        _save_book_matriculas(matriculas_path, nuevo_local_m)
    if nuevo_local_f != local_f:
        _save_book_dates(fechas_path, _fechas_de_texto(nuevo_local_f))
    try:
        _guardar_base(base_path, remota, nuevo_servidor_m, nuevo_servidor_f)
    except OSError as exc:
        # Sin base la próxima vez se concilia como la primera: manda el
        # servidor en todo lo que difiera. Es más lento, no menos seguro.
        logger.warning(f"No se pudo guardar {BASE_FILENAME}: {exc}")
    return resumen


def sincronizar(
    matriculas_path: Path | str, fechas_path: Path | str
) -> Optional[Resumen]:
    """Sincroniza si hay ruta remota activa y responde. Nunca lanza.

    Devuelve ``None`` cuando no había nada que hacer (apagada o sin
    conexión) o cuando no se pudo: la memoria local queda como estaba y la
    ejecución sigue con ella.
    """
    remota = ruta_remota.raiz_remota()
    if remota is None:
        return None
    try:
        resumen = sincronizar_en(remota, matriculas_path, fechas_path)
    except OSError as exc:
        logger.warning(
            f"No se pudo sincronizar la memoria de libros con la ruta "
            f"remota: {exc}. Se sigue con la memoria de este equipo"
        )
        return None
    if resumen.hay_cambios:
        logger.info(str(resumen))
        for cambio in resumen.reemplazos:
            logger.info(
                f"Memoria de libros compartida: {cambio.clave} "
                f"({cambio.archivo}) pasa de {cambio.antes} a {cambio.despues}"
            )
    return resumen
