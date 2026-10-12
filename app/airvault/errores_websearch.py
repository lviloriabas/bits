"""Motivos de consulta seguros y comprensibles, sin ocultarlos al recibirlos."""

from app.airvault.session import ErrorDeSesion
from app.utils.mensajes import mensaje_error


MOTIVOS = {
    "acceso": "Web Search volvió a pedir acceso. Inicie sesión en AirVault y vuelva a revisar.",
    "carga": "Web Search no terminó de cargar los resultados. El batch queda pendiente de revisión.",
    "lectura": "Web Search respondió, pero no se pudieron leer sus bitácoras. El batch queda pendiente de revisión.",
    "navegador": "Se perdió la comunicación con el navegador de AirVault. Vuelva a revisar el batch.",
    "bloqueada": "Windows bloqueó la comunicación con el navegador de AirVault. Revise el acceso de BITS y Edge en el equipo.",
    "permisos": "Su cuenta no tiene permiso para consultar Web Search. Solicite el acceso al administrador.",
    "rechazo": "AirVault rechazó la consulta de Web Search. El batch queda pendiente de revisión.",
    "desconocido": "No se pudo abrir la búsqueda en Web Search. La causa quedó registrada en BITS.",
}


class ErrorDeWebSearch(RuntimeError):
    def __init__(self, motivo):
        self.motivo = motivo
        super().__init__(MOTIVOS[motivo])


def mensaje_error_websearch(error):
    texto = str(error)
    if texto in MOTIVOS.values():
        return texto
    if isinstance(error, ErrorDeWebSearch):
        return MOTIVOS[error.motivo]
    if isinstance(error, ErrorDeSesion):
        return MOTIVOS["acceso"]
    if isinstance(error, PermissionError) and getattr(error, "winerror", None) == 10013:
        return MOTIVOS["bloqueada"]
    if isinstance(error, (ConnectionError, TimeoutError)):
        return MOTIVOS["navegador"]
    normalizado = texto.casefold()
    if any(marca in normalizado for marca in ("session expired", "session has expired", "sesión caduc", "sesion caduc", "pide iniciar sesión")):
        return MOTIVOS["acceso"]
    if any(marca in normalizado for marca in ("leer los resultados", "resultados ilegibles", "rejilla")):
        return MOTIVOS["lectura"]
    if any(marca in normalizado for marca in ("pestaña", "pestana", "comunicacion con edge", "comunicación con edge", "depuracion")):
        return MOTIVOS["navegador"]
    if any(marca in normalizado for marca in ("access denied", "access is denied", "permission denied",
                                             "not have permission", "no tiene permiso", "sin permisos")):
        return MOTIVOS["permisos"]
    return mensaje_error(error, MOTIVOS["desconocido"])
