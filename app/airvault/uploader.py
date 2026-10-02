"""Subida de archivos a AirVault por Quick Upload.

Quick Upload crea un batch nuevo a partir de los archivos que se le envian y
lo deja en la cola de Web Index. La subida va por trozos, como la hace la
propia pagina, y despues se confirma cada archivo con sus valores de
indice.

Aviso importante: Quick Upload solo expone los campos marcados para ese
modulo, y entre ellos no estan Log Page Number, Fleet ni End Date. Por eso
la subida deja el batch clasificado pero no indexado, y el indexado real lo
hace :mod:`app.airvault.indexer` despues. Si el administrador habilita esos
campos para Quick Upload, esta etapa podria cerrarlo todo de una vez.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from app.airvault.config import CAMPO_BATCH_NAME, CAMPO_BATCH_USERNAME

# Campos que Quick Upload acepta hoy en el repositorio MXDocs.
CAMPOS_QUICK_UPLOAD = {
    9586: "C_DocType",
    9754: "C_AuditStatus",
    9633: "C_ACREG",
    9630: "C_DocNo",
    9750: "C_SN",
    9749: "C_PN",
    9631: "C_BatchName",
    9812: "C_EmergencyResponse",
    9813: "C_AirworthinessCertificate",
    9809: "C_BUName",
}

TROZO_BYTES = 1024 * 1024
_TURNO_CARGA = RLock()

# Cuanto se espera la respuesta de ``FinishUpload``. Es la peticion que arma
# el archivo con los trozos ya enviados y crea el batch, y con un PDF de
# cientos de megas tarda mas que el minuto de las demas.
ESPERA_FINISH_UPLOAD_S = 600.0


def serializar_cargas(funcion):
    """Una sola carga de la aplicacion puede estar en vuelo, entre ventanas."""
    @wraps(funcion)
    def ejecutar(*args, **kwargs):
        sesion = kwargs.get("sesion") or (args[1] if len(args) > 1 else None)
        if args and getattr(args[0], "sesion", None) is not None:
            sesion = args[0].sesion
        sesion = getattr(sesion, "sesion", sesion)
        while not _TURNO_CARGA.acquire(timeout=0.25):
            if getattr(sesion, "cancelada", False):
                from app.airvault.session import SesionCancelada

                raise SesionCancelada("Se cancelo esperando el turno de subida")
        try:
            return funcion(*args, **kwargs)
        finally:
            _TURNO_CARGA.release()
    return ejecutar


@dataclass(frozen=True)
class ResultadoSubida:
    archivo: str
    ok: bool
    detalle: str = ""
    # ``False`` cuando FinishUpload salio y su respuesta se perdio: AirVault
    # pudo haber creado el batch, asi que no se repite y se confirma en la
    # cola.
    confirmada: bool = True


def valores_quick_upload(valores: Mapping[int, str]) -> List[Dict[str, Any]]:
    """Arma el ``InputValues`` que espera ``Home/FinishUpload``.

    Solo viajan los campos que el modulo admite; el resto se descarta en
    silencio porque el servidor los rechazaria.

    ``C_BUName`` sale con el mismo nombre del batch cuando nadie le dio uno
    propio. Es el otro campo de nombre que expone Quick Upload, y las cargas
    que lo dejaban vacio son las que AirVault publicaba como
    ``Empty-Batch``.
    """
    nombre_batch = str(valores.get(CAMPO_BATCH_NAME, "") or "")
    salida: List[Dict[str, Any]] = []
    for field_id, columna in CAMPOS_QUICK_UPLOAD.items():
        valor = str(valores.get(field_id, "") or "")
        if field_id == CAMPO_BATCH_USERNAME and not valor:
            valor = nombre_batch
        salida.append({
            "FieldId": str(field_id),
            "WarnEmpty": False,
            "Key": columna,
            "Value": valor,
            "Valid": True,
            "Dirty": bool(valor),
            "OriginalValue": "",
        })
    return salida


def trozos(ruta: Path, tamano: int = TROZO_BYTES):
    """Parte el archivo tal como lo hace el cargador de la pagina."""
    total = max(1, -(-ruta.stat().st_size // tamano))
    with ruta.open("rb") as handle:
        for indice in range(total):
            yield indice, total, handle.read(tamano)


class SubidorQuickUpload:
    """Sube archivos y confirma sus indices."""

    def __init__(self, sesion, repo_id: int):
        self.sesion = sesion
        self.repo_id = repo_id

    @serializar_cargas
    def subir(
        self, ruta: Path | str, valores: Mapping[int, str],
        avisar: Optional[Callable[[str, int, int], None]] = None,
    ) -> ResultadoSubida:
        """Sube un archivo y confirma sus valores de indice.

        Los PDF de una ejecución completa pesan casi dos gigas y viajan en
        trozos de un mega, asi que sin ``avisar`` la subida parece colgada
        durante media hora.
        """
        archivo = Path(ruta)
        if not archivo.is_file():
            return ResultadoSubida(str(archivo), False, "no existe")
        for indice, total, datos in trozos(archivo):
            if avisar is not None:
                avisar(f"Subiendo {archivo.name}", indice, total)
            # Reenviar un trozo con el mismo indice es inocuo: el servidor
            # arma el archivo por posicion, no por orden de llegada.
            self.sesion.post(
                "/quickuploadex/Home/Upload/",
                data={
                    "repoId": self.repo_id,
                    "filename": archivo.name,
                    "name": archivo.name,
                    "chunk": indice,
                    "chunks": total,
                },
                files={"file": (archivo.name, datos,
                                "application/octet-stream")},
            )
        from app.airvault.session import RespuestaPerdida

        if avisar is not None:
            avisar(f"AirVault está recibiendo {archivo.name}", 0, 0)
        try:
            # Una sola vez si la respuesta no llega. Repetir FinishUpload a
            # ciegas, como se hacia al agotar el minuto de espera, es pedirle
            # a AirVault un segundo batch con el mismo archivo o que junte
            # los dos. Un rechazo del servidor si se repite: ese dice que no
            # se hizo.
            self.sesion.post(
                "/quickuploadex/Home/FinishUpload",
                json={"model": {
                    "RepoId": self.repo_id,
                    "FileName": archivo.name,
                    "InputValues": valores_quick_upload(valores),
                }},
                repetir_sin_respuesta=False,
                tiempo_limite=ESPERA_FINISH_UPLOAD_S,
            )
        except RespuestaPerdida as exc:
            logger.warning(
                "FinishUpload de {} no contesto ({}); no se repite y se "
                "confirma en la cola de AirVault",
                archivo.name, exc,
            )
            return ResultadoSubida(archivo.name, True, str(exc), confirmada=False)
        logger.info("Subido {}", archivo.name)
        return ResultadoSubida(archivo.name, True)

    def subir_varios(
        self, rutas: Sequence[Path], valores: Mapping[int, str]
    ) -> List[ResultadoSubida]:
        return [self.subir(ruta, valores) for ruta in rutas]
