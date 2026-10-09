"""Mensajes breves para el usuario; los detalles quedan en el registro."""

from __future__ import annotations

import errno
import re
import unicodedata


def _normalizar(texto: object) -> str:
    return " ".join(
        "".join(
            letra for letra in unicodedata.normalize("NFKD", str(texto).casefold())
            if not unicodedata.combining(letra)
        ).split()
    )


_MOTIVOS = (
    (("certificado ssl", "certificate_verify_failed", "sslerror"),
     "No se pudo verificar la conexión segura con AirVault. Revise la fecha y hora del equipo."),
    (("access denied", "permission denied", "no tiene permiso", "acceso denegado", "sin permisos"),
     "Su cuenta no tiene permiso para esta acción. Solicítelo al administrador."),
    (("session expired", "session has expired", "sesion caduco", "sesion caducada", "sesion de airvault cadu",
      "sesion no esta autenticada", "ninguna sesion", "ninguna cookie", "cookie viene vacia",
      "cabecera cookie", "formulario de acceso", "no llego a dar una sesion",
      "airvault volvio a pedir acceso", "airvault cerro la sesion", "cookie de airvault ya no vale"),
     "Inicie sesión en AirVault y vuelva a intentarlo."),
    (("usuario y contrasena", "rechazo el acceso"),
     "Revise el usuario y la contraseña de AirVault."),
    (("fechas no pueden ser posteriores",), "Elija una fecha que no sea futura."),
    (("fecha inicial no puede ser posterior",), "La fecha inicial debe ser anterior o igual a la final."),
    (("elija duplicadas, mal indexadas",), "Elija duplicadas, mal indexadas o ambas."),
    (("porcentaje de duplicados debe",), "El porcentaje debe estar entre 1 y 100."),
    (("limite de una pagina",), "Elija al menos 2 páginas por batch."),
    (("limite de paginas por batch",), "Revise el máximo de páginas por batch."),
    (("no tiene ningun pdf de entrega",), "Exporte la ejecución antes de subirla."),
    (("ninguna bitacora utilizable", "encontro su fila", "indice declara", "indice nombra archivos",
      "antes de que existiera el indice", "manifiesto ilegible", "manifiesto version"),
     "La entrega está incompleta o no coincide con los datos. Vuelva a exportar la ejecución."),
    (("supera el maximo elegido", "pesa mas de 2048 mb"),
     "El batch es demasiado grande. Reduzca las páginas por batch y reinicie el registro local."),
    (("hay una carga en curso", "otra ventana esta subiendo"), "Hay una subida en curso. Espere a que termine."),
    (("hay un batch sin titulo", "hay que decir el nombre del batch"), "Escriba el nombre del batch."),
    (("mismo titulo", "titulo corresponde a otra clase"), "Revise los nombres de los batches antes de subirlos."),
    (("no hay ningun batch llamado",), "El batch aún no aparece en AirVault. Vuelva a revisar."),
    (("no tiene batch", "hay que buscarlo primero"), "Busque el batch en AirVault antes de continuar."),
    (("mas de un batch", "posible duplicado", "ya se mandaron", "ya viajan", "ya estan en airvault"),
     "Estas bitácoras pueden estar duplicadas. Revise AirVault antes de reenviarlas."),
    (("batches nuevos que podrian", "batches que coinciden", "batches sin nombre"),
     "Hay varios batches que podrían ser esta carga. Revise cuál corresponde en AirVault."),
    (("batch cambio desde",), "El batch cambió. Vuelva a revisarlo antes de indexar."),
    (("batch esta abierto", "esta abierto por"),
     "El batch está abierto en AirVault. Ciérrelo y vuelva a intentarlo."),
    (("no dijo cuantas paginas", "no devolvio las paginas", "no devolvio los campos"),
     "No se pudieron leer las páginas del batch. Vuelva a revisar en AirVault."),
    (("escribir asi correria", "no se renombro", "cargas mezcladas", "batch mezclado", "juntar las cargas"),
     "Las páginas del batch no coinciden con la entrega. Revise el batch en AirVault."),
    (("no conservo el guardado", "no confirmo el guardado"),
     "AirVault no confirmó el guardado. Revise la bitácora antes de reintentar."),
    (("no confirmo si completo", "no completo el batch", "resultado desconocido al completar"),
     "AirVault no confirmó que el batch esté completado. Vuelva a revisar."),
    (("obligatorios", "value is required", "end date", "fecha vacia", "fecha invalida"),
     "Faltan datos por confirmar. Revise la matrícula, el número de bitácora y la fecha."),
    (("ya no esta abierta",), "La pestaña de AirVault se cerró. Vuelva a abrirla."),
    (("entre las opciones de",), "La matrícula o la flota no están disponibles en AirVault. Consulte al administrador."),
    (("paginas amarillas", "no estan en verde", "paginas fuera de verde"), "Quedan páginas por revisar en AirVault."),
    (("permiso «delete batch image»",), "No se pudieron quitar los separadores. Solicite permiso al administrador de AirVault."),
    (("airvault dice que eso no existe",), "La página o el batch ya no están disponibles en AirVault. Vuelva a buscarlos."),
    (("abrir la sesion",), "Inicie sesión en AirVault y vuelva a intentarlo."),
    (("edge no", "edge cerro", "edge arranco", "edge rechazo", "comunicacion con edge", "puerto de depuracion"),
     "No se pudo abrir AirVault. Cierre Edge y vuelva a intentarlo."),
    (("no se pudo conectar", "se corto la conexion", "no contesto", "algo que no es json",
      "no se pudo completar /", "el servidor respondio"),
     "AirVault no responde. Revise la conexión y vuelva a intentarlo."),
    (("reporte esta incompleto", "no termino de cargar la pagina", "leer las filas del reporte",
      "leer el estado de log page audit", "visor cambio de pagina"),
     "No se pudo cargar el reporte completo. Vuelva a consultarlo."),
    (("formulario de log page audit cambio", "no mostro el repositorio", "no habilito los parametros"),
     "AirVault no cargó las opciones del reporte. Vuelva a consultarlo."),
    (("cambio los filtros", "cambio el limite de fechas"),
     "Los filtros o las fechas cambiaron. Vuelva a consultar el reporte."),
    (("log page audit no quedo listo",), "Inicie sesión en AirVault y vuelva a consultar el reporte."),
    (("esa fila no trae ninguna busqueda",), "Esta fila no tiene una búsqueda disponible."),
)

_AVISOS = {
    "matricula_vacia": "Falta la matrícula.",
    "matricula_desconocida": "La matrícula no está disponible en AirVault.",
    "matricula_del_libro": "La matrícula no coincide con la del libro.",
    "matricula_distinta": "La matrícula no coincide con AirVault.",
    "desalineado": "El número de bitácora no coincide con AirVault.",
    "ya_indexada": "La bitácora ya está indexada.",
    "log_duplicado": "El número de bitácora está repetido.",
    "fecha_dudosa": "El año de la fecha está sin confirmar.",
    "sin_fila": "La página no coincide con los datos. Vuelva a exportar.",
    "no_cargo": "AirVault no cargó la página. Vuelva a revisar.",
}

_ARCHIVOS = (
    "No se pudo acceder al archivo. Ciérrelo si está abierto y vuelva a intentarlo.",
    "No se encontró el archivo. Vuelva a seleccionarlo.",
    "No hay espacio disponible. Libere espacio y vuelva a intentarlo.",
    "Se perdió la conexión. Vuelva a intentarlo.",
)
_SEGUROS = {_normalizar(m): m for m in _ARCHIVOS}
_SEGUROS.update({_normalizar(m): m for _marcas, m in _MOTIVOS})


def mensaje_aviso(aviso: object) -> str:
    """Traduce los avisos guardados en una página sin sus códigos internos."""
    texto = str(aviso)
    if _normalizar(texto) in ("sin matricula", "no se pudo leer la matricula"):
        return _AVISOS["matricula_vacia"]
    codigo = re.match(r"\[([a-z_]+)\]", texto)
    if codigo:
        clave = codigo.group(1)
        if clave in _AVISOS:
            return _AVISOS[clave]
        if clave in ("obligatorio_vacio", "indice_incompleto"):
            normalizado = _normalizar(texto)
            for campo, etiqueta in (("aircraft", "la matrícula"), ("log page number", "el número de bitácora"), ("end date", "la fecha")):
                if campo in normalizado:
                    return f"Falta confirmar {etiqueta}."
            return "Faltan datos por confirmar."
    return mensaje_error(texto, "Revise los datos de la bitácora.")


def mensaje_error(error: object, defecto: str = "No se pudo completar la operación. Vuelva a intentarlo.") -> str:
    """Reconoce motivos conocidos sin mostrar excepciones ni texto del servidor."""
    if isinstance(error, PermissionError):
        return "No se pudo acceder al archivo. Ciérrelo si está abierto y vuelva a intentarlo."
    if isinstance(error, FileNotFoundError):
        return "No se encontró el archivo. Vuelva a seleccionarlo."
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "No hay espacio disponible. Libere espacio y vuelva a intentarlo."
    if isinstance(error, (ConnectionError, TimeoutError)):
        return "Se perdió la conexión. Vuelva a intentarlo."
    texto = _normalizar(error)
    if texto in _SEGUROS:
        return _SEGUROS[texto]
    if texto == _normalizar(defecto):
        return defecto
    for marcas, mensaje in _MOTIVOS:
        if texto == _normalizar(mensaje) or any(marca in texto for marca in marcas):
            return mensaje
    if (not re.search(r"\bnot locked\b|\bno esta bloquead[ao]\b", texto)
            and re.search(r"\b(?:is (?:currently |already )?locked|has been locked|page locked|"
                          r"document locked|locked by|esta bloquead[ao]|bloquead[ao] por)\b", texto)):
        return "La bitácora está bloqueada en AirVault. Espere a que se libere."
    return defecto
