"""Registro durable de los batches de una entrega.

El manifiesto de cada batch dice en que va ese batch. Lo que no dice
ninguno es que se hizo con la entrega **entera**: que bitacoras se llevo
cada uno, cuales llegaron a AirVault y cuales quedan por mandar. Esa
respuesta se sacaba juntando los manifiestos que hubiera en disco en ese
momento, y por eso cambiaba cuando cambiaba el reparto: al repartir de
nuevo con otro maximo de paginas, los manifiestos viejos se apartan y con
ellos se iba la unica memoria de lo que ya se habia subido.

Este registro es esa memoria, y es independiente de la configuracion. Vive
en la carpeta del trabajo, junto a los manifiestos. Al eliminar los batches
locales se vacia el historial visible, pero conserva las claves minimas que
impiden reconstruirlos durante el siguiente arranque. Guarda ademas unas
cuantas versiones anteriores mientras el historial siga vigente.

La identidad de una bitacora es la de siempre: el archivo del que salio y
su pagina dentro de el. Solo es unica dentro de su entrega, asi que el
registro anota tambien de que CSV viene.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

from loguru import logger
from pydantic import BaseModel, Field

REGISTRO_FILENAME = "registro-de-batches.json"

# Pausas antes de volver a leer un archivo que Windows no dejo abrir. Pasa en
# un instante y se resuelve solo; son menos de medio segundo en total.
PAUSAS_DE_LECTURA = (0.05, 0.15, 0.3)

# Repartos anteriores que se conservan. Cada uno es la lista completa de
# batches tal como estaba antes de rehacerla, asi que sirve para ver que
# se descarto y para reconstruirlo a mano si hiciera falta. Sin tope, una
# ejecucion que se reparte muchas veces acabaria arrastrando un archivo
# que crece sin motivo.
MAXIMO_HISTORIAL = 10

_NOMBRE_DE_PARTE = re.compile(r"(parte|revisar)(-\d+)?$", re.IGNORECASE)


class BatchAnotado(BaseModel):
    """Lo que hay que recordar de un batch aunque cambie el reparto."""

    carpeta: str = ""
    parte: int = 1
    nombre_batch: str = ""
    batch_id: str = ""
    # Bitacoras que lleva, por archivo de origen y pagina dentro de el. Sin
    # separadores: una pagina divisoria no es un documento y no se indexa.
    paginas: List[Tuple[str, int]] = Field(default_factory=list)
    subido: bool = False
    indexado: bool = False
    completado: bool = False
    actualizado: str = ""

    def claves(self) -> Set[Tuple[str, int]]:
        return {(str(archivo).casefold(), int(pagina))
                for archivo, pagina in self.paginas}


class RepartoArchivado(BaseModel):
    """Un reparto anterior, guardado tal como estaba antes de rehacerlo."""

    guardado: str = ""
    motivo: str = ""
    batches: List[BatchAnotado] = Field(default_factory=list)


class RegistroDeEntrega(BaseModel):
    """Todos los batches de una entrega, con su historia reciente."""

    version: int = 1
    csv_origen: str = ""
    actualizado: str = ""
    batches: List[BatchAnotado] = Field(default_factory=list)
    historial: List[RepartoArchivado] = Field(default_factory=list)
    # Bitacoras de batches locales que alguien elimino expresamente. No son
    # historial visible ni un trabajo recuperable, pero deben sobrevivir al
    # reinicio para que preparar la misma entrega no los construya otra vez.
    eliminadas: List[Tuple[str, int]] = Field(default_factory=list)

    def por_carpeta(self) -> Dict[str, BatchAnotado]:
        return {batch.carpeta: batch for batch in self.batches}

    def comprometidas(self) -> Set[Tuple[str, int]]:
        """Bitacoras que no deben volver a formar un batch local."""
        claves: Set[Tuple[str, int]] = {
            (str(archivo).casefold(), int(pagina))
            for archivo, pagina in self.eliminadas
        }
        for batch in self.batches:
            if batch.subido or batch.batch_id:
                claves |= batch.claves()
        return claves

    def anotadas(self) -> Set[Tuple[str, int]]:
        """Todas las bitacoras que el registro conoce, se subieran o no."""
        claves: Set[Tuple[str, int]] = set()
        for batch in self.batches:
            claves |= batch.claves()
        return claves


def raiz_de_registro(carpeta: Path | str) -> Path:
    """La carpeta de la entrega a partir de la de un batch.

    Una entrega repartida deja cada parte en su subcarpeta (``parte-02``,
    ``revisar``) y sin repartir el trabajo vive directamente en la carpeta
    de la ejecucion. El registro es de la entrega, asi que siempre sube a
    esa carpeta comun.
    """
    carpeta = Path(carpeta)
    if _NOMBRE_DE_PARTE.fullmatch(carpeta.name):
        return carpeta.parent
    return carpeta


def ruta_registro(carpeta: Path | str) -> Path:
    return raiz_de_registro(carpeta) / REGISTRO_FILENAME


def leer_json_de_memoria(ruta: Path):
    """Lo que guarda un archivo de memoria local, o ``None`` si no se pudo leer.

    Estos archivos se leen, se cambian y se vuelven a escribir enteros, asi
    que una lectura fallida que se tome por «vacio» acaba en una escritura
    que borra lo que habia. Dos cosas para que eso no pase por un tropiezo:
    el archivo que Windows tiene tomado un instante (el antivirus, otra
    ventana) se vuelve a pedir, y el que esta danado se aparta con otro
    nombre antes de seguir, para poder recuperarlo a mano.
    """
    if not ruta.is_file():
        return None
    texto = ""
    for pausa in (*PAUSAS_DE_LECTURA, None):
        try:
            texto = ruta.read_text(encoding="utf-8")
            break
        except OSError as exc:
            if pausa is None:
                logger.warning("No se pudo leer {}: {}", ruta.name, exc)
                return None
            time.sleep(pausa)
    try:
        return json.loads(texto)
    except ValueError as exc:
        apartar_ilegible(ruta, exc)
        return None


def apartar_ilegible(ruta: Path, motivo: object) -> None:
    """Deja una copia del archivo danado antes de que se reemplace."""
    copia = ruta.with_name(f"{ruta.name}.ilegible")
    try:
        if copia.is_file() and copia.stat().st_size == ruta.stat().st_size:
            return
        shutil.copy2(ruta, copia)
    except OSError:
        return
    logger.warning(
        "{} esta danado ({}); se conserva una copia en {} y se sigue sin "
        "esa memoria", ruta.name, motivo, copia.name,
    )


def leer(carpeta: Path | str) -> RegistroDeEntrega:
    """El registro de la entrega, o uno vacio si todavia no hay ninguno.

    Un archivo ilegible se trata como ausente en vez de cortar el trabajo:
    el registro acelera y protege, pero los manifiestos siguen estando y
    con ellos se puede reconstruir.
    """
    ruta = ruta_registro(carpeta)
    datos = leer_json_de_memoria(ruta)
    if datos is None:
        return RegistroDeEntrega()
    try:
        return RegistroDeEntrega.model_validate(datos)
    except ValueError as exc:
        apartar_ilegible(ruta, exc)
        return RegistroDeEntrega()


def guardar(registro: RegistroDeEntrega, carpeta: Path | str) -> Path:
    """Escribe el registro de forma atomica, como el manifiesto."""
    destino = ruta_registro(carpeta)
    destino.parent.mkdir(parents=True, exist_ok=True)
    registro.actualizado = datetime.now().isoformat(timespec="seconds")
    contenido = registro.model_dump_json(indent=2)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(destino.parent), prefix=".registro-", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(contenido)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, destino)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return destino


def _anotacion_de(trabajo) -> BatchAnotado:
    """Lo que el registro guarda de un trabajo, leido de su manifiesto."""
    manifiesto = trabajo.manifiesto
    paginas = [
        (registro.archivo_origen, int(registro.pagina_origen))
        for registro in manifiesto.registros
        if not registro.es_separador and registro.archivo_origen
    ]
    return BatchAnotado(
        carpeta=str(trabajo.carpeta),
        parte=int(manifiesto.parte or 1),
        nombre_batch=manifiesto.nombre_batch,
        batch_id=str(manifiesto.batch_id or ""),
        paginas=paginas,
        subido=manifiesto.etapa_hecha("subir"),
        indexado=manifiesto.etapa_hecha("verificar"),
        # El cierre hecho, no el omitido: ese batch sigue en la cola.
        completado=manifiesto.cierre_hecho(),
        actualizado=datetime.now().isoformat(timespec="seconds"),
    )


def anotar(
    carpeta: Path | str,
    trabajos: Sequence,
    csv_origen: str = "",
) -> RegistroDeEntrega:
    """Deja constancia de en que van estos batches.

    Actualiza los que se le pasan y **conserva** los demas. Es la parte que
    importa: un batch cuyo manifiesto se aparto al rehacer el reparto sigue
    anotado con las bitacoras que se llevo, asi que no se vuelven a subir
    aunque su manifiesto ya no este en disco.
    """
    registro = leer(carpeta)
    existentes = registro.por_carpeta()
    for trabajo in trabajos:
        anotacion = _anotacion_de(trabajo)
        anterior = existentes.get(anotacion.carpeta)
        if anterior is not None:
            # Lo que ya llego a AirVault no se desanota porque un manifiesto
            # nuevo todavía no lo diga: se suman, nunca se restan.
            anotacion.subido = anotacion.subido or anterior.subido
            anotacion.indexado = anotacion.indexado or anterior.indexado
            anotacion.completado = anotacion.completado or anterior.completado
            anotacion.batch_id = anotacion.batch_id or anterior.batch_id
        existentes[anotacion.carpeta] = anotacion
    if csv_origen:
        registro.csv_origen = str(csv_origen)
    elif not registro.csv_origen and trabajos:
        registro.csv_origen = str(trabajos[0].manifiesto.csv_origen or "")
    registro.batches = sorted(
        existentes.values(), key=lambda batch: (batch.parte, batch.carpeta)
    )
    guardar(registro, carpeta)
    return registro


def archivar(carpeta: Path | str, motivo: str = "") -> RegistroDeEntrega:
    """Guarda el reparto vigente en el historial antes de rehacerlo."""
    registro = leer(carpeta)
    if not registro.batches:
        return registro
    registro.historial.insert(
        0,
        RepartoArchivado(
            guardado=datetime.now().isoformat(timespec="seconds"),
            motivo=motivo,
            batches=list(registro.batches),
        ),
    )
    del registro.historial[MAXIMO_HISTORIAL:]
    guardar(registro, carpeta)
    return registro


def comprometidas(carpeta: Path | str) -> Set[Tuple[str, int]]:
    """Bitacoras que el registro da por enviadas a AirVault."""
    return leer(carpeta).comprometidas()


def rutas_del_registro(carpeta: Path | str) -> List[Path]:
    """Archivos de memoria local que se van con el registro de la entrega.

    Son el registro y los manifiestos apartados al rehacer un reparto. Los
    manifiestos vivos los enumera quien los borra; estos no aparecen en esa
    busqueda porque ya no se llaman ``manifiesto.json``, y quedarse serian
    los unicos restos de una ejecucion que se pidio olvidar.
    """
    raiz = raiz_de_registro(carpeta)
    if not raiz.is_dir():
        return []
    archivo_registro = ruta_registro(raiz)
    rutas: List[Path] = []
    if archivo_registro.is_file():
        try:
            datos = json.loads(archivo_registro.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Un registro ilegible tambien se ofrece para eliminar. Solo se
            # oculta el archivo valido que conserva exclusivamente las
            # marcas necesarias para no resucitar batches borrados.
            rutas.append(archivo_registro)
        else:
            if datos.get("batches") or datos.get("historial"):
                rutas.append(archivo_registro)
    rutas.extend(
        sorted(
            ruta for ruta in raiz.rglob("manifiesto-reemplazado-*.json")
            if ruta.is_file()
        )
    )
    return rutas


def olvidar(
    carpeta: Path | str,
    carpetas_de_batch: Sequence[Path | str],
    paginas: Sequence[Tuple[str, int]] = (),
) -> RegistroDeEntrega:
    """Saca batches del registro sin permitir que vuelvan a construirse.

    Mandar el manifiesto a la Papelera lo saca de la cola actual. Las claves
    se conservan aparte como eliminadas para que preparar la entrega durante
    un arranque posterior no recree el batch a partir del CSV y los PDF.

    Lo que ya viajo a AirVault no se deshace con esto: el batch remoto
    sigue donde estaba.
    """
    registro = leer(carpeta)
    fuera = {str(Path(ruta)).casefold() for ruta in carpetas_de_batch}
    quitados = [
        batch for batch in registro.batches
        if str(Path(batch.carpeta)).casefold() in fuera
    ]
    quedan = [
        batch for batch in registro.batches
        if str(Path(batch.carpeta)).casefold() not in fuera
    ]
    eliminadas = {
        (str(archivo).casefold(), int(pagina))
        for archivo, pagina in registro.eliminadas
    }
    eliminadas.update(
        clave for batch in quitados for clave in batch.claves()
    )
    eliminadas.update(
        (str(archivo).casefold(), int(pagina))
        for archivo, pagina in paginas
    )
    nuevas = sorted(eliminadas)
    if (
        len(quedan) == len(registro.batches)
        and nuevas == sorted(registro.eliminadas)
    ):
        return registro
    registro.batches = quedan
    registro.eliminadas = nuevas
    guardar(registro, carpeta)
    return registro


def desanotar(
    carpeta: Path | str,
    carpetas_de_batch: Sequence[Path | str],
) -> RegistroDeEntrega:
    """Quita batches del registro sin darlos por eliminados.

    Es para la carga que hay que volver a mandar: AirVault la junto con otra
    y el batch mezclado se aparto, asi que sus bitacoras vuelven a estar por
    subir. :func:`olvidar` no sirve para eso: deja las paginas marcadas como
    eliminadas, que cuentan como ya cubiertas, y al cambiar el reparto antes
    de resubirlas se quedaban fuera de todos los batches sin ningun aviso.
    """
    registro = leer(carpeta)
    fuera = {str(Path(ruta)).casefold() for ruta in carpetas_de_batch}
    quedan = [
        batch for batch in registro.batches
        if str(Path(batch.carpeta)).casefold() not in fuera
    ]
    if len(quedan) == len(registro.batches):
        return registro
    registro.batches = quedan
    guardar(registro, carpeta)
    return registro


def eliminar_historial(
    carpeta: Path | str,
    paginas: Sequence[Tuple[str, int]] = (),
) -> RegistroDeEntrega:
    """Vacía el registro visible y conserva solo la supresion permanente.

    Se llama despues de retirar los manifiestos locales. Las paginas del
    reparto vigente y de sus versiones archivadas quedan marcadas para que
    el arranque automatico no reconstruya lo que se pidio eliminar.
    """
    registro = leer(carpeta)
    eliminadas = {
        (str(archivo).casefold(), int(pagina))
        for archivo, pagina in registro.eliminadas
    }
    eliminadas.update(
        clave for batch in registro.batches for clave in batch.claves()
    )
    eliminadas.update(
        clave
        for reparto in registro.historial
        for batch in reparto.batches
        for clave in batch.claves()
    )
    eliminadas.update(
        (str(archivo).casefold(), int(pagina))
        for archivo, pagina in paginas
    )
    registro.batches = []
    registro.historial = []
    registro.eliminadas = sorted(eliminadas)
    guardar(registro, carpeta)
    return registro
