"""Confirma la publicacion de un batch con una muestra distribuida."""

from dataclasses import dataclass, is_dataclass, replace
import json
import hashlib
import re
import time

from app.airvault.config import CAMPO_BATCH_NAME, CAMPO_LOG_NUMBER, CAMPO_MATRICULA
from app.airvault.session import SesionCancelada
from app.airvault.websearch import PLANTILLAS


METODO_MUESTRA = "muestra_distribuida"
PORCENTAJE_MUESTRA = 2
MINIMO_MUESTRA = 7
MAXIMO_MUESTRA = 15


@dataclass(frozen=True)
class ConfirmacionBatch:
    confirmado: bool
    encontradas: int
    esperadas: int
    detalle: str
    batch_id: str = ""
    muestra: tuple[str, ...] = ()


def _muestra(manifiesto):
    """Elige identidades distintas desde el principio hasta el final.

    Se consulta el 2 % con un minimo de siete y un maximo de quince. Es una
    senal de publicacion del batch, no una auditoria de todas sus paginas.
    Los extremos permiten detectar una publicacion que solo llego al inicio.
    """
    unicos = list(dict.fromkeys(
        (str(r.log_number).strip(), r.matricula.strip().upper())
        for r in manifiesto.bitacoras()
        if re.fullmatch(r"\d{7}", str(r.log_number).strip())
    ))
    cantidad = min(len(unicos), MAXIMO_MUESTRA,
                   max(MINIMO_MUESTRA, (len(unicos) * PORCENTAJE_MUESTRA + 99) // 100))
    if len(unicos) <= cantidad:
        return unicos
    return [unicos[round(i * (len(unicos) - 1) / (cantidad - 1))]
            for i in range(cantidad)]


def muestra_de_batch(manifiesto) -> list[str]:
    return [numero for numero, _matricula in _muestra(manifiesto)]


def _filas(datos):
    if isinstance(datos, list):
        return [fila for fila in datos if isinstance(fila, dict)]
    if isinstance(datos, dict):
        for clave in ("rows", "data", "results", "items", "documents", "Results"):
            if isinstance(datos.get(clave), (dict, list)):
                return _filas(datos[clave])
    return []


def _campo(fila, nombres):
    etiqueta = str(fila.get("FieldId", fila.get("fieldId", "")))
    if etiqueta in nombres:
        return str(fila.get("Value", fila.get("value", "")) or "").strip()
    for clave, valor in fila.items():
        limpio = re.sub(r"[^a-z0-9]", "", str(clave).casefold())
        if limpio in nombres and isinstance(valor, (str, int)):
            return str(valor).strip()
    for valor in fila.values():
        candidatos = [valor] if isinstance(valor, dict) else valor if isinstance(valor, list) else []
        for candidato in candidatos:
            if isinstance(candidato, dict):
                encontrado = _campo(candidato, nombres)
                if encontrado:
                    return encontrado
    return ""


def _normalizar(texto):
    return " ".join(str(texto).split()).casefold()


def huella_de_batch(manifiesto, batch_id=None):
    """Vincula la prueba al contenido esperado, incluso si se edita después."""
    datos = [_normalizar(manifiesto.nombre_batch), str(batch_id or manifiesto.batch_id or "").casefold(),
             [(r.seq, str(r.log_number).strip(), r.matricula.strip().upper())
              for r in manifiesto.bitacoras()]]
    return hashlib.sha256(json.dumps(datos, ensure_ascii=False).encode("utf-8")).hexdigest()


class _LecturaAcotada:
    """El descubrimiento de rutas comparte el presupuesto de la consulta."""

    def __init__(self, sesion, fin, cancelado):
        self._sesion = sesion
        self._fin = fin
        self._cancelado = cancelado

    def __getattr__(self, nombre):
        return getattr(self._sesion, nombre)

    def get(self, *args, **kwargs):
        if self._cancelado():
            raise SesionCancelada("Se canceló la verificación en Web Search")
        if time.monotonic() >= self._fin:
            raise TimeoutError("Se agotó el tiempo para verificar Web Search")
        return self._sesion.get(*args, **kwargs)


def verificar_batch(buscador, trabajo, avisar=None, presupuesto_s=60.0, cancelado=lambda: False) -> ConfirmacionBatch:
    """Busca solo la muestra y confirma cuando aparece toda en Web Search."""
    manifiesto = trabajo.manifiesto
    muestra = _muestra(manifiesto)
    numeros = tuple(numero for numero, _matricula in muestra)
    esperadas = len(muestra)
    encontradas = 0
    identidad = str(manifiesto.batch_id or "").casefold()

    def resultado(confirmado, detalle):
        return ConfirmacionBatch(confirmado, encontradas, esperadas, detalle,
                                 identidad, numeros)

    if not muestra:
        return resultado(False, "Faltan números válidos para buscar una muestra de bitácoras.")
    inicio = time.monotonic()
    original = buscador
    if is_dataclass(buscador):
        buscador = replace(buscador, sesion=_LecturaAcotada(buscador.sesion, inicio + presupuesto_s, cancelado))
    if cancelado():
        raise SesionCancelada("Se canceló la verificación en Web Search")
    try:
        if not buscador.ruta and not buscador.preparar() and not buscador._adoptar_ruta(numeros[0]):
            if cancelado() or getattr(buscador.sesion, "cancelada", False):
                raise SesionCancelada("Se canceló la verificación en Web Search")
            return resultado(False, buscador.motivo or "Web Search no está disponible.")
    except SesionCancelada:
        raise
    except Exception:
        return resultado(False, "No se pudo consultar Web Search. Se verificará de nuevo.")
    if is_dataclass(original):
        for campo in ("_ruta", "_plantilla", "_probado", "_sin_control", "_motivo", "_candidatas", "_tanteos"):
            setattr(original, campo, getattr(buscador, campo))
    if buscador._plantilla not in PLANTILLAS:
        return resultado(False, "Web Search no tiene una consulta válida para verificar esta carga.")
    forma = PLANTILLAS[buscador._plantilla]
    consultas = {}
    for numero, matricula in muestra:
        if cancelado():
            raise SesionCancelada("Se canceló la verificación en Web Search")
        if time.monotonic() - inicio >= presupuesto_s:
            return resultado(False, "La consulta tardó demasiado. Se conserva el avance y se verificará de nuevo.")
        if avisar:
            avisar(f"Buscando una muestra de «{manifiesto.nombre_batch}» en Web Search", encontradas, esperadas)
        try:
            if numero not in consultas:
                consultas[numero] = _filas(buscador.sesion.get(
                    buscador.ruta, forma.construir(numero, buscador.config)))
        except SesionCancelada:
            raise
        except Exception:
            return resultado(False, "Web Search no respondió. Se conserva el estado y se comprobará de nuevo.")
        if cancelado():
            raise SesionCancelada("Se canceló la verificación en Web Search")
        if time.monotonic() - inicio >= presupuesto_s:
            return resultado(False, "La consulta tardó demasiado. Se conserva el avance y se verificará de nuevo.")
        coincidentes = []
        for fila in consultas[numero]:
            hallado = _campo(fila, {"cdocno", "logpagenumber", "lognumber", "docno", str(CAMPO_LOG_NUMBER)})
            if hallado != numero:
                continue
            nombre = _campo(fila, {"cbatchname", "batchname", "batchnombre", str(CAMPO_BATCH_NAME)})
            if nombre and _normalizar(nombre) != _normalizar(manifiesto.nombre_batch):
                continue
            batch_id = _campo(fila, {"batchid", "batchkey", "cbatchid"})
            if batch_id and identidad and batch_id.casefold() != identidad:
                continue
            # El nombre exacto basta si Web Search no expone el ID. Si no
            # trae nombre, se exige el ID ya conocido de esta carga.
            if not nombre and not (batch_id and identidad):
                continue
            avion = _campo(fila, {"cacreg", "acreg", "aircraft", "matricula", str(CAMPO_MATRICULA)})
            if matricula and avion.upper() != matricula:
                continue
            coincidentes.append(batch_id.casefold())
        ids = {valor for valor in coincidentes if valor}
        if len(ids) > 1:
            return resultado(False, "Hay varios batches con el mismo nombre. No se combinan sus bitácoras para confirmar una carga.")
        if not coincidentes:
            return resultado(False, f"{encontradas} de {esperadas} bitácoras de la muestra encontradas. El batch queda pendiente de confirmar.")
        if ids:
            identidad = next(iter(ids))
        encontradas += 1
    if avisar:
        avisar(f"Muestra de «{manifiesto.nombre_batch}» encontrada en Web Search", encontradas, esperadas)
    return resultado(True, f"Muestra de {esperadas} bitácoras encontrada en Web Search, de {len(manifiesto.bitacoras())} en el batch. Batch marcado como completado.")
