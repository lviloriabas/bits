"""Comprueba una entrega completa por su identidad, con lecturas paginadas."""

from collections import Counter
from dataclasses import dataclass, is_dataclass, replace
import json
import hashlib
import math
import re
import time

from app.airvault.config import CAMPO_BATCH_NAME, CAMPO_LOG_NUMBER, CAMPO_MATRICULA
from app.airvault.session import SesionCancelada
from app.airvault.websearch import PLANTILLAS, _b64


@dataclass(frozen=True)
class ConfirmacionBatch:
    confirmado: bool
    encontradas: int
    esperadas: int
    detalle: str
    batch_id: str = ""


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


def verificar_batch(buscador, trabajo, avisar=None, presupuesto_s=120.0, cancelado=lambda: False) -> ConfirmacionBatch:
    """Lee por Batch Name, no una peticion por cada bitacora.

    Solo cuenta filas del nombre exacto, con su numero de bitacora. Un ID
    de batch contradictorio, una matricula distinta o resultados repetidos
    no pueden completar el recuento de otro batch.
    """
    manifiesto = trabajo.manifiesto
    registros = manifiesto.bitacoras()
    esperadas = len(registros)
    numeros = [str(r.log_number).strip() for r in registros]
    if not esperadas or any(not re.fullmatch(r"\d{7}", n) for n in numeros):
        return ConfirmacionBatch(False, 0, esperadas, "Faltan números válidos para comprobar todas las bitácoras.")
    inicio = time.monotonic()
    original = buscador
    if is_dataclass(buscador):
        buscador = replace(buscador, sesion=_LecturaAcotada(buscador.sesion, inicio + presupuesto_s, cancelado))
    try:
        if not buscador.ruta and not buscador.preparar() and not buscador._adoptar_ruta(numeros[0]):
            if cancelado() or getattr(buscador.sesion, "cancelada", False):
                raise SesionCancelada("Se canceló la verificación en Web Search")
            return ConfirmacionBatch(False, 0, esperadas, buscador.motivo or "Web Search no está disponible.")
    except SesionCancelada:
        raise
    except Exception:
        return ConfirmacionBatch(False, 0, esperadas, "No se pudo consultar Web Search. Se verificará de nuevo.")
    if is_dataclass(original):
        for campo in ("_ruta", "_plantilla", "_probado", "_sin_control", "_motivo", "_candidatas", "_tanteos"):
            setattr(original, campo, getattr(buscador, campo))
    if buscador._plantilla not in PLANTILLAS:
        return ConfirmacionBatch(False, 0, esperadas, "Web Search no tiene una consulta válida para verificar esta carga.")
    forma = PLANTILLAS[buscador._plantilla]
    parametros = forma.construir(manifiesto.nombre_batch, buscador.config)
    if forma.nombre == "encodedValues":
        parametros["encodedValues"] = _b64(f"{CAMPO_BATCH_NAME}={manifiesto.nombre_batch}")
    elif forma.nombre == "fieldId":
        parametros["fieldId"] = CAMPO_BATCH_NAME
    parametros["rows"] = 50
    esperados = Counter((str(r.log_number).strip(), r.matricula.strip().upper()) for r in registros)
    numeros_esperados = set(numeros)
    encontrados = Counter()
    documentos = set()
    paginas = set()
    ids_batch = set()
    sin_identidad = False
    agotada = False
    # Se admite margen para duplicados, sin recorrer una busqueda sin filtro
    # que el servidor haya devuelto por no admitir la consulta por batch.
    maximo = max(2, math.ceil(esperadas / 50) + 5)
    for pagina in range(1, maximo + 1):
        if cancelado():
            raise SesionCancelada("Se canceló la verificación en Web Search")
        if time.monotonic() - inicio >= presupuesto_s:
            return ConfirmacionBatch(False, sum(encontrados.values()), esperadas,
                                     "La consulta tardó demasiado. Se conserva el avance del proceso y se verificará de nuevo.")
        if avisar:
            avisar(f"Verificando «{manifiesto.nombre_batch}» en Web Search", sum(encontrados.values()), esperadas)
        parametros["page"] = pagina
        try:
            datos = buscador.sesion.get(buscador.ruta, dict(parametros))
        except SesionCancelada:
            raise
        except Exception:
            return ConfirmacionBatch(False, sum(encontrados.values()), esperadas,
                                     "Web Search no respondió. Se conserva el estado y se comprobará de nuevo.")
        filas = _filas(datos)
        huella = json.dumps(filas, sort_keys=True, ensure_ascii=False, default=str)
        if not filas or huella in paginas:
            agotada = not filas
            break
        paginas.add(huella)
        for fila in filas:
            nombre = _campo(fila, {"cbatchname", "batchname", "batchnombre", str(CAMPO_BATCH_NAME)})
            if _normalizar(nombre) != _normalizar(manifiesto.nombre_batch):
                continue
            batch_id = _campo(fila, {"batchid", "batchkey", "cbatchid"})
            if not batch_id:
                sin_identidad = True
                continue
            if batch_id and manifiesto.batch_id and batch_id.casefold() != manifiesto.batch_id.casefold():
                continue
            ids_batch.add(batch_id.casefold())
            if len(ids_batch) > 1:
                return ConfirmacionBatch(False, 0, esperadas, "Hay varios batches con el mismo nombre. No se combinan sus bitácoras para confirmar una carga.")
            numero = _campo(fila, {"cdocno", "logpagenumber", "lognumber", "docno", str(CAMPO_LOG_NUMBER)})
            if numero not in numeros_esperados:
                continue
            matricula = _campo(fila, {"cacreg", "acreg", "aircraft", "matricula", str(CAMPO_MATRICULA)})
            clave = (numero, matricula.upper())
            if clave not in esperados:
                clave = (numero, "")
            if clave not in esperados:
                continue
            documento = _campo(fila, {"docid", "documentid", "documentkey", "id"})
            # Sin ID de documento, varias filas del mismo número y avión
            # solo prueban una bitácora, aunque cambien sus otros campos.
            identidad = ("documento", documento) if documento else ("bitacora", *clave)
            if identidad in documentos:
                continue
            documentos.add(identidad)
            if encontrados[clave] < esperados[clave]:
                encontrados[clave] += 1
        total = sum(encontrados.values())
        if encontrados == esperados and manifiesto.batch_id:
            return ConfirmacionBatch(True, total, esperadas,
                                     f"{total} de {esperadas} bitácoras confirmadas en el batch «{manifiesto.nombre_batch}».",
                                     next(iter(ids_batch)))
        if len(filas) < 50:
            agotada = True
            break
    total = sum(encontrados.values())
    if agotada and encontrados == esperados and len(ids_batch) == 1 and not sin_identidad:
        return ConfirmacionBatch(True, total, esperadas,
                                 f"{total} de {esperadas} bitácoras confirmadas en el batch «{manifiesto.nombre_batch}».",
                                 next(iter(ids_batch)))
    return ConfirmacionBatch(False, total, esperadas,
                             "Web Search no devuelve el identificador del batch para distinguir sus copias." if sin_identidad else
                             f"{total} de {esperadas} bitácoras identificadas en este batch. Falta confirmar su publicación completa.")
