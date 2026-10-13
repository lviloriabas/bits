"""Confirma la publicacion de un batch con una muestra distribuida."""

from dataclasses import dataclass, is_dataclass, replace
import json
import hashlib
import re
import time
from loguru import logger

from app.airvault.config import CAMPO_LOG_NUMBER
from app.airvault.errores_websearch import mensaje_error_websearch
from app.airvault.session import ErrorDeSesion, SesionCancelada
from app.airvault.websearch import PLANTILLAS, _aparece


METODO_MUESTRA = "muestra_por_numero"
METODOS_MUESTRA = (METODO_MUESTRA, "muestra_distribuida")
# Cuantos batches se leen a la vez, como mucho. Todas las pestanas comparten
# la sesion de AirVault, que atiende sus busquedas de una en una: abrir mas
# reparte la misma cola entre mas batches, y lo que se gana es lo que cada
# pestana tarda en cargar su pagina, que si va en paralelo. Con 32 cada
# bitacora tardaba unos doce segundos y, con el plazo de diez que habia
# entonces, ningun batch terminaba. Con el plazo de un minuto por bitacora
# (ver ``SEGUNDOS_POR_BITACORA``) veinte caben sin que ninguno se quede sin
# tiempo. Cuantos se abren de verdad lo decide la memoria libre del equipo;
# ver :func:`batches_paralelos`.
MAX_BATCHES_PARALELOS = 20
# Sin poder medir la memoria se usa el valor que ya estaba probado.
BATCHES_PARALELOS_SIN_MEDIDA = 10
# Con el equipo justo de memoria se sigue leyendo, pero de dos en dos.
MINIMO_BATCHES_PARALELOS = 2
# Lo que ocupa una pestana de Web Search con su tabla cargada. Medido sobre
# el Edge del programa con trece pestanas abiertas: 97 MB de media y 138 la
# mayor. Se redondea hacia arriba porque quedarse corto es lo que congela.
MB_POR_PESTANA = 150
# Memoria libre que el chequeo no toca: la cuarta parte del equipo y nunca
# menos de 3 GB. Es lo que deja sitio al resto del programa (la lectura de
# paginas puede estar corriendo a la vez), al propio Edge y a Windows.
FRACCION_DE_MEMORIA_A_CONSERVAR = 0.25
MB_LIBRES_A_CONSERVAR = 3072


def batches_paralelos(libre_mb: int | None = None, total_mb: int | None = None) -> int:
    """Cuantos batches se leen a la vez sin dejar al equipo sin memoria.

    Cada batch en lectura es una pestana de Edge. Se abren las que caben en
    la memoria libre despues de apartar la reserva, entre el minimo y el
    tope. Se mide en el momento de empezar: un equipo que esta leyendo
    paginas a la vez tiene menos libre y abre menos.
    """
    if libre_mb is None or total_mb is None:
        from app.core.parallelism import available_memory_mb, total_memory_mb

        libre_mb = available_memory_mb() if libre_mb is None else libre_mb
        total_mb = total_memory_mb() if total_mb is None else total_mb
    if libre_mb <= 0:
        return BATCHES_PARALELOS_SIN_MEDIDA
    reserva = max(MB_LIBRES_A_CONSERVAR, int(total_mb * FRACCION_DE_MEMORIA_A_CONSERVAR))
    caben = (libre_mb - reserva) // MB_POR_PESTANA
    return int(max(MINIMO_BATCHES_PARALELOS, min(MAX_BATCHES_PARALELOS, caben)))
PORCENTAJE_MUESTRA = 5
MINIMO_MUESTRA = 7

# Un batch que no se pudo leer (un corte, una pagina que no cargo, la sesion
# que caduco a mitad) se vuelve a consultar al final del chequeo. Son las
# esperas antes de cada vuelta extra: dos vueltas, la segunda mas espaciada.
# Sin ellas el batch quedaba «pendiente de revision» hasta que alguien
# volviera a lanzar el chequeo entero, y mientras tanto nadie sabia si
# estaba subido.
ESPERAS_ENTRE_VUELTAS_S = (20.0, 60.0)
# En las vueltas extra van pocos a la vez: si la primera pasada fallo con
# AirVault cargado, repetirla igual de ancha es pedir el mismo fallo.
BATCHES_PARALELOS_AL_REINTENTAR = 4
# El plazo de un batch sale de lo que puede tardar cada bitacora de su
# muestra. Con treinta pestanas consultando a la vez, Web Search contesta
# cada busqueda en diez o quince segundos, y con diez por bitacora los
# batches se quedaban sin tiempo a dos o tres numeros del final: llevaban
# encontradas 22 de 25 y salian como «no termino de cargar». Ningun batch
# llegaba a contarse y el chequeo se quedaba en cero.
SEGUNDOS_POR_BITACORA = 60.0
PLAZO_MINIMO_DE_BATCH_S = 120.0


def plazo_de_batch(bitacoras: int) -> float:
    """Segundos que se le dan a un batch para leer esa cantidad de bitacoras."""
    return max(PLAZO_MINIMO_DE_BATCH_S, bitacoras * SEGUNDOS_POR_BITACORA)


# Esperas antes de volver a pedir el acceso a Web Search cuando AirVault no
# lo da (su servidor contesta con error, o la sesion no llega a renovarse).
# Antes un solo tropiezo ahi abortaba el chequeo sin mirar un solo batch.
ESPERAS_DE_ACCESO_S = (15.0, 30.0, 60.0, 120.0)


@dataclass(frozen=True)
class ConfirmacionBatch:
    confirmado: bool
    encontradas: int
    esperadas: int
    detalle: str
    batch_id: str = ""
    muestra: tuple[str, ...] = ()
    consulta_completa: bool = False


def _numeros_validos(manifiesto):
    return list(dict.fromkeys(
        str(r.log_number).strip()
        for r in manifiesto.bitacoras()
        if re.fullmatch(r"\d{7}", str(r.log_number).strip())
    ))


def _muestra(manifiesto):
    """Elige identidades distintas desde el principio hasta el final.

    Se consulta al menos el 5 % con un minimo de siete. Es una
    senal de publicacion del batch, no una auditoria de todas sus paginas.
    Los extremos permiten detectar una publicacion que solo llego al inicio.
    """
    unicos = _numeros_validos(manifiesto)
    cantidad = min(len(unicos),
                   max(MINIMO_MUESTRA, (len(unicos) * PORCENTAJE_MUESTRA + 99) // 100))
    if len(unicos) <= cantidad:
        return unicos
    return [unicos[round(i * (len(unicos) - 1) / (cantidad - 1))]
            for i in range(cantidad)]


def muestra_de_batch(manifiesto) -> list[str]:
    return _muestra(manifiesto)


def numeros_de_batch(manifiesto) -> list[str]:
    """Todas las bitacoras del batch que se pueden buscar, sin repetir.

    Es lo que consulta la revision completa, la que alguien pide cuando la
    muestra no le basta.
    """
    return _numeros_validos(manifiesto)


def _filas(datos):
    if isinstance(datos, list):
        return [fila for fila in datos if isinstance(fila, dict)]
    if isinstance(datos, dict):
        for clave in ("rows", "data", "results", "items", "documents", "Results"):
            if isinstance(datos.get(clave), (dict, list)):
                return _filas(datos[clave])
    return []


def _respuesta_legible(datos):
    if isinstance(datos, list):
        return all(isinstance(fila, dict) for fila in datos)
    if isinstance(datos, dict):
        return any(_respuesta_legible(datos[clave]) for clave in
                   ("rows", "data", "results", "items", "documents", "Results")
                   if isinstance(datos.get(clave), (dict, list)))
    return False


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


def huella_de_numeros(manifiesto):
    """La publicacion anterior no depende del nombre ni del ID local actual."""
    datos = [(r.seq, str(r.log_number).strip()) for r in manifiesto.bitacoras()]
    return hashlib.sha256(json.dumps(datos).encode("utf-8")).hexdigest()


class _LecturaAcotada:
    """El descubrimiento de rutas comparte el presupuesto de la consulta."""

    def __init__(self, sesion, fin, cancelado):
        self._sesion = sesion
        self._fin = fin
        self._cancelado = cancelado

    def __getattr__(self, nombre):
        return getattr(self._sesion, nombre)

    def get(self, *args, **kwargs):
        if args and str(args[0]).lower().startswith("/index/"):
            raise ValueError("La confirmación solo consulta Web Search.")
        if self._cancelado():
            raise SesionCancelada("Se canceló la verificación en Web Search")
        if time.monotonic() >= self._fin:
            raise TimeoutError("Se agotó el tiempo para verificar Web Search")
        return self._sesion.get(*args, **kwargs)


def verificar_batch(buscador, trabajo, avisar=None, presupuesto_s=60.0, cancelado=lambda: False,
                    revisar_todas=False, consultas=None) -> ConfirmacionBatch:
    """Busca solo la muestra y confirma cuando aparece toda en Web Search.

    ``consultas`` guarda lo leido de cada numero. Quien repite la consulta
    de un batch pasa el mismo diccionario y solo se piden las bitacoras que
    faltaban: lo que ya contesto Web Search no se vuelve a preguntar.
    """
    manifiesto = trabajo.manifiesto
    muestra = _numeros_validos(manifiesto) if revisar_todas else _muestra(manifiesto)
    numeros = tuple(muestra)
    esperadas = len(muestra)
    encontradas = 0
    identidad = str(manifiesto.batch_id or "").casefold()

    def resultado(confirmado, detalle, consulta_completa=False):
        return ConfirmacionBatch(confirmado, encontradas, esperadas, detalle,
                                 identidad, numeros, consulta_completa)

    if not muestra:
        return resultado(False, "Faltan números de bitácora válidos para buscar una muestra.")
    inicio = time.monotonic()
    directa = getattr(buscador, "consultar_numero", None)
    original = buscador
    if is_dataclass(buscador):
        buscador = replace(buscador, sesion=_LecturaAcotada(buscador.sesion, inicio + presupuesto_s, cancelado))
    if cancelado():
        raise SesionCancelada("Se canceló la verificación en Web Search")
    try:
        if not callable(directa) and is_dataclass(buscador) and not buscador.ruta:
            ruta, plantilla = buscador._guardada()
            if ruta:
                buscador._ruta, buscador._plantilla = ruta, plantilla
        if not callable(directa) and not buscador.ruta and not buscador._adoptar_ruta(numeros[0]):
            if cancelado() or getattr(buscador.sesion, "cancelada", False):
                raise SesionCancelada("Se canceló la verificación en Web Search")
            return resultado(False, buscador.motivo or "Web Search no está disponible.")
    except (ErrorDeSesion, SesionCancelada):
        raise
    except Exception as exc:
        logger.warning("Web Search no preparo la consulta del batch {}: {}", manifiesto.nombre_batch, exc)
        return resultado(False, mensaje_error_websearch(exc))
    if is_dataclass(original):
        for campo in ("_ruta", "_plantilla", "_probado", "_sin_control", "_motivo", "_candidatas", "_tanteos"):
            setattr(original, campo, getattr(buscador, campo))
    if not callable(directa) and (buscador._plantilla not in PLANTILLAS
                                 or not buscador.ruta.lower().startswith("/zfp/")):
        return resultado(False, "Web Search no tiene una consulta válida para verificar esta carga.")
    forma = None if callable(directa) else PLANTILLAS[buscador._plantilla]
    consultas = {} if consultas is None else consultas
    for revisadas, numero in enumerate(muestra):
        if cancelado():
            raise SesionCancelada("Se canceló la verificación en Web Search")
        if time.monotonic() - inicio >= presupuesto_s:
            return resultado(False, "La consulta tardó demasiado. Se conserva el avance y se verificará de nuevo.")
        if avisar:
            avisar(f"Revisando números de bitácora del batch «{manifiesto.nombre_batch}» en Web Search", revisadas, esperadas)
        try:
            if numero not in consultas:
                datos = (directa(numero, max(0.0, presupuesto_s - (time.monotonic() - inicio)))
                         if callable(directa) else buscador.sesion.get(
                             buscador.ruta, forma.construir(numero, buscador.config)))
                if not _respuesta_legible(datos):
                    return resultado(False, "Web Search devolvió resultados ilegibles. Se verificará de nuevo.")
                consultas[numero] = _filas(datos)
        except (ErrorDeSesion, SesionCancelada):
            raise
        except Exception as exc:
            logger.warning("Web Search no pudo leer la bitacora {} del batch {}: {}", numero, manifiesto.nombre_batch, exc)
            return resultado(False, mensaje_error_websearch(exc))
        if cancelado():
            raise SesionCancelada("Se canceló la verificación en Web Search")
        if time.monotonic() - inicio >= presupuesto_s:
            return resultado(False, "La consulta tardó demasiado. Se conserva el avance y se verificará de nuevo.")
        aparece = False
        for fila in consultas[numero]:
            hallado = _campo(fila, {"cdocno", "clogno", "logno", "logpagenumber", "lognumber", "docno", str(CAMPO_LOG_NUMBER)})
            if hallado != numero and (hallado or not _aparece(numero, fila)):
                continue
            aparece = True
            break
        encontradas += bool(aparece)
    if avisar:
        avisar(f"Revisión de números de bitácora del batch «{manifiesto.nombre_batch}» en Web Search terminada", esperadas, esperadas)
    de_donde = "del batch" if revisar_todas else "de la muestra"
    if encontradas != esperadas:
        return resultado(False, f"{encontradas} de {esperadas} números de bitácora {de_donde} encontrados en Web Search. Falta confirmar la subida.", True)
    if avisar:
        avisar(f"Muestra de «{manifiesto.nombre_batch}» encontrada en Web Search", encontradas, esperadas)
    if revisar_todas:
        return resultado(True, f"Las {esperadas} bitácoras del batch están en Web Search. Batch marcado como completado.", True)
    return resultado(True, f"Muestra de {esperadas} bitácoras encontrada en Web Search, de {len(manifiesto.bitacoras())} en el batch. Batch marcado como completado.", True)
