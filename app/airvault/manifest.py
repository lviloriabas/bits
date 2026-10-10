"""Lectura y escritura del manifiesto de un trabajo de indexado.

La escritura es atomica (archivo temporal y ``os.replace``) porque el
indexado guarda el manifiesto despues de cada pagina: si el proceso muere a
mitad de la escritura, un JSON truncado dejaria el trabajo irrecuperable y
habria que volver a indexar todo el batch.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from functools import wraps
from pathlib import Path
from threading import RLock

from app.airvault.model import Manifiesto

MANIFIESTO_FILENAME = "manifiesto.json"
_CANDADOS: dict[str, RLock] = {}
_GUARDIA = RLock()
_CAMPOS_CONFIRMACION = ("websearch_confirmado", "websearch_muestra", "websearch_detalle",
                        "websearch_metodo", "websearch_batch_id", "websearch_revision", "websearch_cotejadas",
                        "websearch_huella")
_CIERRE_POR_MUESTRA = "Confirmado por muestra en Web Search"


def copiar_confirmacion(origen: Manifiesto, destino: Manifiesto) -> None:
    """Integra solo la prueba y su cierre, sin reemplazar las paginas en vuelo."""
    if origen is destino or destino.websearch_revision > origen.websearch_revision:
        return
    for campo in _CAMPOS_CONFIRMACION:
        if campo != "websearch_revision":
            valor = getattr(origen, campo)
            setattr(destino, campo, list(valor) if isinstance(valor, list) else valor)
    if origen.websearch_confirmado:
        destino.batch_id = destino.batch_id or origen.batch_id
        for nombre in ("subir", "completar"):
            if nombre in origen.etapas:
                destino.etapas[nombre] = origen.etapas[nombre].model_copy(deep=True)
        destino.no_encontrado_desde = ""
    elif (destino.etapas.get("completar") is not None
          and destino.etapas["completar"].detalle == _CIERRE_POR_MUESTRA
          and "completar" not in origen.etapas):
        destino.etapas.pop("completar")
    destino.websearch_revision = origen.websearch_revision


def _aplicar_cierre_por_muestra(manifiesto):
    """Mantiene el cierre local ligado a la muestra y al contenido comprobado."""
    from app.airvault.confirmacion import METODOS_MUESTRA

    if manifiesto.websearch_metodo not in METODOS_MUESTRA:
        return
    from app.airvault.flujo import websearch_confirmacion_valida
    from app.airvault.model import EstadoEtapa
    if websearch_confirmacion_valida(manifiesto):
        subida = manifiesto.etapas.get("subir")
        if subida is None or subida.estado is not EstadoEtapa.HECHA:
            manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, _CIERRE_POR_MUESTRA)
        cierre = manifiesto.etapas.get("completar")
        if cierre is None or cierre.estado is not EstadoEtapa.HECHA:
            manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA, _CIERRE_POR_MUESTRA)
        manifiesto.no_encontrado_desde = ""
    else:
        cierre = manifiesto.etapas.get("completar")
        if cierre and cierre.detalle == _CIERRE_POR_MUESTRA:
            manifiesto.etapas.pop("completar")


def _serializar(funcion):
    @wraps(funcion)
    def ejecutar(manifiesto, carpeta_job):
        clave = str(Path(carpeta_job).resolve()).casefold()
        with _GUARDIA:
            candado = _CANDADOS.setdefault(clave, RLock())
        with candado:
            return funcion(manifiesto, carpeta_job)
    return ejecutar

# Windows no deja reemplazar un archivo mientras otro lo tiene abierto: el
# antivirus que lo revisa, el indexador de busqueda u otra ventana que lee
# los manifiestos. Pasa en un instante y se resuelve solo, pero el indexado
# guarda tras cada pagina y un solo ``PermissionError`` cortaba el batch a
# la mitad sin que el registro dijera por que. Se reintenta durante unos
# dos segundos y medio antes de darlo por fallo.
PAUSAS_REEMPLAZO = (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6)


def ruta_manifiesto(carpeta_job: Path | str) -> Path:
    return Path(carpeta_job) / MANIFIESTO_FILENAME


def cargar(carpeta_job: Path | str) -> Manifiesto:
    """Carga el manifiesto de un trabajo.

    Raises:
        FileNotFoundError: si el trabajo no existe todavía.
        ValueError: si el archivo esta corrupto o es de otra version.
    """
    ruta = ruta_manifiesto(carpeta_job)
    if not ruta.is_file():
        raise FileNotFoundError(f"No hay manifiesto en {ruta}")
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Manifiesto ilegible en {ruta}: {exc}") from exc
    manifiesto = Manifiesto.model_validate(datos)
    if manifiesto.version != 1:
        raise ValueError(f"Manifiesto version {manifiesto.version}, esperada 1")
    return manifiesto


@_serializar
def guardar(manifiesto: Manifiesto, carpeta_job: Path | str) -> Path:
    """Guarda el manifiesto de forma atomica y devuelve su ruta."""
    carpeta = Path(carpeta_job)
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = ruta_manifiesto(carpeta)
    if destino.is_file():
        try:
            actual = cargar(carpeta)
        except (OSError, ValueError):
            actual = None
        if actual is not None and actual.websearch_revision > manifiesto.websearch_revision:
            for campo in _CAMPOS_CONFIRMACION:
                setattr(manifiesto, campo, getattr(actual, campo))
            if actual.websearch_confirmado and not manifiesto.batch_id:
                manifiesto.batch_id = actual.websearch_batch_id
    _aplicar_cierre_por_muestra(manifiesto)
    contenido = manifiesto.model_dump_json(indent=2)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(carpeta), prefix=".manifiesto-", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(contenido)
            handle.flush()
            os.fsync(handle.fileno())
        for pausa in (*PAUSAS_REEMPLAZO, None):
            try:
                os.replace(tmp_name, destino)
                break
            except PermissionError:
                if pausa is None:
                    raise
                time.sleep(pausa)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return destino


@_serializar
def guardar_confirmacion(datos: dict, carpeta_job: Path | str) -> Manifiesto:
    """Actualiza la confirmacion sin reemplazar el avance de otro worker."""
    manifiesto = cargar(carpeta_job)
    from app.airvault.confirmacion import METODO_MUESTRA, huella_de_numeros
    por_numero = datos["websearch_metodo"] == METODO_MUESTRA
    if por_numero and datos["websearch_confirmado"] and manifiesto.cancelado:
        raise ValueError("El batch se canceló durante la búsqueda en Web Search.")
    if datos["websearch_confirmado"] and not por_numero and manifiesto.batch_id and (
        manifiesto.batch_id.casefold() != datos["batch_id"].casefold()
    ):
        raise ValueError("La identidad del batch cambió durante la consulta.")
    if datos["websearch_confirmado"]:
        from app.airvault.confirmacion import huella_de_batch
        huella = huella_de_numeros(manifiesto) if por_numero else huella_de_batch(manifiesto, datos["batch_id"])
        if datos["websearch_huella"] != huella:
            raise ValueError("El contenido del batch cambió durante la consulta.")
    for campo in _CAMPOS_CONFIRMACION:
        setattr(manifiesto, campo, datos[campo])
    if datos["websearch_confirmado"]:
        from app.airvault.model import EstadoEtapa
        if not por_numero:
            manifiesto.batch_id = manifiesto.batch_id or datos["batch_id"]
        manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "Confirmado en Web Search")
        manifiesto.no_encontrado_desde = ""
    guardar(manifiesto, carpeta_job)
    return manifiesto


def existe(carpeta_job: Path | str) -> bool:
    return ruta_manifiesto(carpeta_job).is_file()
