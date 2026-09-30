"""Cliente de los endpoints del Web Index de AirVault.

Es una capa fina sobre :class:`SesionAirVault`: traduce nombres en espanol a
las rutas reales y devuelve estructuras de Python. No decide nada; toda la
logica de si algo se escribe o no vive en el indexador y en las guardas,
que se pueden probar sin red.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Protocol, Sequence

from loguru import logger

from app.airvault.config import AirVaultConfig
from app.airvault.encoding import (
    codificar_batch_id,
    codificar_sticky,
    codificar_valores,
)
from app.airvault.naming import limpiar_nombre_remoto

# Orden de las columnas que devuelve GetBatches. La respuesta es una lista
# posicional, no un diccionario, asi que el orden es parte del contrato.
COLUMNAS_LOTE = (
    "appid", "lockeduserid", "lockedusername", "appname", "batchid",
    "imagecount", "userbatchname", "batchreceivedate", "eventlabel",
    "lasteventdate", "rights", "accountid", "domainid", "userid", "eventid",
    "IdentityColumn",
)


@dataclass(frozen=True)
class ResumenLote:
    """Una fila del listado de batches de AirVault."""

    batch_id: str
    nombre: str
    paginas: int
    repo_id: int
    repositorio: str
    paso: str
    bloqueado_por: str
    recibido: str

    @classmethod
    def desde_celdas(cls, celdas: Sequence[Any]) -> "ResumenLote":
        """Arma el resumen desde la lista posicional que manda el servidor."""
        valores = dict(zip(COLUMNAS_LOTE, list(celdas)))
        return cls.desde_fila(valores)

    @classmethod
    def desde_fila(cls, fila: Mapping[str, Any]) -> "ResumenLote":
        def entero(valor: Any) -> int:
            try:
                return int(str(valor).strip())
            except (TypeError, ValueError):
                return 0

        return cls(
            batch_id=str(fila.get("batchid", "")).strip(),
            nombre=limpiar_nombre_remoto(fila.get("userbatchname", "")),
            paginas=entero(fila.get("imagecount")),
            repo_id=entero(fila.get("appid")),
            repositorio=str(fila.get("appname", "")).strip(),
            paso=str(fila.get("eventlabel", "")).strip(),
            bloqueado_por=str(fila.get("lockedusername", "")).strip(),
            recibido=str(fila.get("batchreceivedate", "")).strip(),
        )


@dataclass(frozen=True)
class PaginaIndexada:
    """Lo que AirVault tiene guardado hoy en una pagina."""

    pagina: int
    estado: int
    valores: Dict[int, str]
    columnas: Dict[str, str]


@dataclass(frozen=True)
class PaginaDelLote:
    """Como ve AirVault una pagina del batch, sin traerse sus valores.

    Es el mapa del batch entero en una sola peticion. Sirve para saber que
    paginas quedaron en verde sin releerlas una por una, que es lo que hay
    que mirar antes de dar el batch por terminado.
    """

    pagina: int
    estado: int
    # Primera pagina del documento al que pertenece. AirVault agrupa varias
    # paginas en un documento y el estado que cuenta es el de la primera.
    inicio_documento: int
    borrada: bool = False

    @property
    def encabeza_documento(self) -> bool:
        return self.pagina == self.inicio_documento

    @property
    def valida(self) -> bool:
        return self.estado == 0


def codificar_texto(texto: str) -> str:
    """Base64 del filtro, vacio cuando no hay filtro."""
    import base64

    limpio = str(texto or "")
    if not limpio:
        return ""
    return base64.b64encode(limpio.encode("utf-8")).decode("ascii")


class RespuestaInesperada(RuntimeError):
    """AirVault contesto algo con otra forma de la que se esperaba."""


def _describir(datos: Any) -> str:
    """Describe una respuesta rara sin volcarla entera en el mensaje."""
    if datos is None:
        return "una respuesta vacia"
    if isinstance(datos, (list, tuple)):
        return f"una lista de {len(datos)} elementos"
    texto = str(datos).strip().replace("\n", " ")
    if not texto:
        return "una respuesta vacia"
    return f"«{texto[:120]}»" + ("…" if len(texto) > 120 else "")


class ClienteAirVault(Protocol):
    """Contrato minimo que necesita el indexador.

    Existe para que los tests inyecten un cliente falso y se pueda probar
    todo el recorrido de un batch sin tocar produccion.
    """

    def listar_lotes(self, filtro: str = "") -> List[ResumenLote]: ...

    def abrir_lote(self, batch_id: str) -> Mapping[str, Any]: ...

    def cerrar_lote(self, batch_id: str) -> Mapping[str, Any]: ...

    def renombrar_lote(self, batch_id: str, nombre: str) -> bool: ...

    def leer_pagina(self, batch_id: str, pagina: int) -> PaginaIndexada: ...

    def guardar_pagina(
        self, batch_id: str, pagina: int, valores: Mapping[int, str],
        estado: int, pagina_siguiente: Optional[int] = None,
    ) -> Mapping[str, Any]: ...

    def picklist_matriculas(self) -> List[str]: ...

    def paginas_del_lote(self, batch_id: str) -> List[PaginaDelLote]: ...

    def borrar_pagina(self, batch_id: str, pagina: int,
                      borrada: bool = True) -> bool: ...

    def validar_batch(
        self, batch_id: str, paginas: Sequence[int],
    ) -> List[Mapping[str, Any]]: ...

    def completar_lote(self, batch_id: str) -> Mapping[str, Any]: ...


class ClienteHttp:
    """Implementacion real contra el servidor."""

    def __init__(self, sesion, config: AirVaultConfig):
        self.sesion = sesion
        self.config = config

    # ── carriles paralelos ───────────────────────────────────────────

    def carriles(self) -> int:
        """Cuantas conexiones pueden leer o escribir paginas a la vez.

        Una sola si la sesion no se puede clonar: sin su propia conexion,
        dos hilos se pisarian la misma ``requests.Session``.
        """
        if not callable(getattr(self.sesion, "clonar", None)):
            return 1
        return max(1, int(getattr(self.config, "carriles_indexado", 1) or 1))

    def carril(self) -> "ClienteHttp":
        """Otro cliente con la misma autenticacion, para un hilo aparte.

        No vuelve a entrar solo si AirVault lo rechaza: lo hace la sesion
        principal, que es la unica que puede abrir Edge.
        """
        return ClienteHttp(self.sesion.clonar(renovable=False), self.config)

    def cerrar_conexiones(self) -> None:
        """Suelta las conexiones de un carril que ya no se usa."""
        http = getattr(self.sesion, "http", None)
        if http is not None:
            http.close()

    # ── batches ──────────────────────────────────────────────────────

    def buscar_lotes(
        self, nombres: Sequence[str],
    ) -> Iterator[tuple[str, List[ResumenLote]]]:
        """Entrega las consultas por nombre conforme terminan, con hasta cuatro conexiones.

        Solo se paraleliza el listado. Abrir, identificar y modificar batches
        sigue en el hilo del flujo, sin compartir una sesion HTTP entre hilos.

        Una consulta que falla por la sesion o la red en su conexion aparte
        se repite en la principal, que es la que puede volver a entrar.
        """
        from app.airvault.session import (ErrorDeConexion, ErrorDeSesion,
                                          SesionCancelada)

        nombres = list(dict.fromkeys(nombres))
        if len(nombres) < 2 or not callable(getattr(self.sesion, "clonar", None)):
            for nombre in nombres:
                yield nombre, self.listar_lotes(nombre)
            return

        def consultar(sesion, nombre):
            try:
                return ClienteHttp(sesion, self.config).listar_lotes(nombre)
            except SesionCancelada:
                raise
            except (ErrorDeSesion, ErrorDeConexion) as exc:
                return exc
            finally:
                sesion.http.close()

        restantes = iter(nombres)
        ejecutor = ThreadPoolExecutor(max_workers=4)
        futuros = {}
        try:
            def enviar():
                nombre = next(restantes, None)
                if nombre is not None:
                    # Clonar ocurre en el hilo propietario de la sesion.
                    sesion = self.sesion.clonar(renovable=False)
                    futuros[ejecutor.submit(consultar, sesion, nombre)] = nombre

            for _ in range(min(4, len(nombres))):
                enviar()
            while futuros:
                terminados, _ = wait(futuros, return_when=FIRST_COMPLETED)
                for futuro in terminados:
                    nombre = futuros.pop(futuro)
                    resultado = futuro.result()
                    if isinstance(resultado, Exception):
                        # El carril no puede volver a entrar; la sesion
                        # principal si. Si tampoco puede, el error es suyo.
                        resultado = self.listar_lotes(nombre)
                    enviar()
                    yield nombre, resultado
        finally:
            ejecutor.shutdown(wait=True)

    def listar_lotes(self, filtro: str = "") -> List[ResumenLote]:
        """Lista los batches de la cola, opcionalmente filtrados por nombre.

        El filtro lo aplica el servidor como subcadena sin distinguir
        mayusculas, asi que mandarlo evita traerse la cola entera cuando
        solo interesan los batches de un dia.
        """
        datos = self.sesion.get(
            "/index/Batch/GetBatches",
            {"repoId": -1, "eventLabel": "",
             "encodedFilter": codificar_texto(filtro),
             "encodedKeywordFilter": "",
             "_search": "false", "rows": 1000, "page": 1, "sidx": "",
             "sord": "asc"},
        )
        filas = datos.get("rows", datos) if isinstance(datos, Mapping) else datos
        resultado: List[ResumenLote] = []
        for fila in filas or []:
            celda = fila.get("cell", fila) if isinstance(fila, Mapping) else fila
            if isinstance(celda, Mapping):
                resultado.append(ResumenLote.desde_fila(celda))
            elif isinstance(celda, (list, tuple)):
                resultado.append(ResumenLote.desde_celdas(celda))
        return resultado

    def abrir_lote(self, batch_id: str) -> Mapping[str, Any]:
        """Bloquea el batch y devuelve su informacion.

        Si el mismo usuario tiene el batch abierto en un navegador, el
        servidor deja la peticion colgada. El tiempo limite de la sesion es
        lo que evita que el proceso se quede esperando indefinidamente.
        """
        return self.sesion.get(
            "/index/Batch/LockAndGetBatchInfo",
            {"repoId": self.config.repo_id,
             "encodedBatchId": codificar_batch_id(batch_id)},
        )

    def cerrar_lote(self, batch_id: str) -> Mapping[str, Any]:
        """Suelta el batch que abrio :meth:`abrir_lote`.

        Hay que llamarlo siempre, tambien cuando el indexado se corta a
        medias: AirVault admite un solo dueno por batch, asi que un batch que
        queda bloqueado deja colgada la siguiente apertura (la del propio
        programa o la de la persona que lo abre en el navegador) sin decir
        por que.
        """
        return self.sesion.get(
            "/index/Batch/UnlockBatch",
            {"repoId": self.config.repo_id,
             "encodedBatchId": codificar_batch_id(batch_id)},
        )

    def renombrar_lote(self, batch_id: str, nombre: str) -> bool:
        """Le pone al batch el nombre con el que se le va a reconocer.

        Quick Upload envia el nombre normalmente. Hace falta cuando AirVault
        lo pierde y publica la carga como «Empty-Batch». Es la misma accion
        «Rename» que ofrece el Web Index.

        Devuelve si el servidor acepto la petición. El flujo vuelve a leer la
        cola y exige ver el mismo ID con el título esperado antes de indexar:
        quedarse como ``Empty-Batch`` ya no se considera un éxito parcial.
        """
        limpio = str(nombre or "").strip()
        if not limpio:
            return False
        try:
            self.sesion.post(
                "/index/Batch/UpdateBatchName",
                data={"repoId": self.config.repo_id, "batchId": batch_id,
                      "encodedBatchName": codificar_texto(limpio)},
            )
        except Exception as exc:  # noqa: BLE001 - se informa como False
            logger.info("No se pudo renombrar el batch {}: {}", batch_id, exc)
            return False
        logger.info("El batch {} quedo como {!r}", batch_id, limpio)
        return True

    def borrar_pagina(self, batch_id: str, pagina: int,
                      borrada: bool = True) -> bool:
        """Quita una pagina del batch, o la devuelve.

        Es la papelera de la tira de paginas del Web Index. No borra el
        archivo: marca la pagina, que deja de contar como documento y deja
        de estorbar para dar el batch por terminado. Se puede deshacer
        mientras el batch siga en la cola, con ``borrada=False``.

        Devuelve si AirVault lo acepto. Quitar paginas pide un permiso
        aparte («Delete Batch Image») que no toda cuenta tiene, y quedarse
        sin el no es motivo para tirar el trabajo: lo que sigue es mirar
        las paginas otra vez y decir que el batch no se puede cerrar.
        """
        try:
            self.sesion.post(
                "/index/FormsProcessing/MarkPageDeleted",
                data={"repoId": self.config.repo_id, "batchId": batch_id,
                      "page": int(pagina),
                      "markDeleted": "true" if borrada else "false"},
            )
        except Exception as exc:  # noqa: BLE001 - el batch sigue entero
            logger.info(
                "No se pudo {} la pagina {} del batch {}: {}",
                "quitar" if borrada else "devolver", pagina, batch_id, exc,
            )
            return False
        return True

    def paginas_del_lote(self, batch_id: str) -> List[PaginaDelLote]:
        """El mapa del batch entero en una sola peticion.

        Es lo que dibuja la tira de paginas del Web Index, y trae el estado
        de cada una sin sus valores. Se usa para saber si el batch esta
        entero en verde, que es la condicion para darlo por terminado: con
        una peticion por pagina eso serian cuatrocientas.
        """
        datos = self.sesion.get(
            "/index/FormsProcessing/GetBatchPages",
            {"repoId": self.config.repo_id,
             "encodedBatchId": codificar_batch_id(batch_id)},
        )
        filas = (
            datos.get("fpisForWebIndex") if isinstance(datos, Mapping)
            else datos
        )
        if not isinstance(filas, (list, tuple)):
            raise RespuestaInesperada(
                f"AirVault no devolvio las paginas del batch {batch_id}, sino "
                f"{_describir(datos)}."
            )
        paginas: List[PaginaDelLote] = []
        for fila in filas:
            if not isinstance(fila, Mapping):
                continue
            def entero(clave: str, defecto: int = 0) -> int:
                try:
                    return int(fila.get(clave, defecto) or defecto)
                except (TypeError, ValueError):
                    return defecto

            secuencia = entero("Sequence")
            paginas.append(PaginaDelLote(
                pagina=secuencia,
                estado=entero("Status"),
                inicio_documento=entero("SequenceStart", secuencia) or secuencia,
                borrada=bool(entero("Deleted")),
            ))
        return paginas

    def completar_lote(self, batch_id: str) -> Mapping[str, Any]:
        """Da el batch por terminado: lo indexa y lo manda a Web Search.

        Es el boton «Complete» de la pantalla. AirVault solo lo acepta con
        el batch entero en verde; por eso quien lo llama mira antes las
        paginas y no lo intenta a ciegas.
        """
        respuesta = self.sesion.get(
            "/index/FormsProcessing/CompleteBatch",
            {"repoId": self.config.repo_id,
             "encodedBatchId": codificar_batch_id(batch_id)},
        )
        if not isinstance(respuesta, Mapping) or "IsError" not in respuesta:
            raise RespuestaInesperada(
                f"AirVault no confirmo si completo el batch {batch_id}: "
                f"{_describir(respuesta)}"
            )
        indicador = respuesta.get("IsError")
        if isinstance(indicador, str):
            normalizado = indicador.strip().casefold()
            if normalizado not in ("true", "false"):
                raise RespuestaInesperada(
                    f"AirVault devolvio un resultado desconocido al completar "
                    f"el batch {batch_id}: {_describir(respuesta)}"
                )
            hubo_error = normalizado == "true"
        elif isinstance(indicador, bool):
            hubo_error = indicador
        else:
            raise RespuestaInesperada(
                f"AirVault devolvio un resultado desconocido al completar "
                f"el batch {batch_id}: {_describir(respuesta)}"
            )
        if hubo_error:
            raise RespuestaInesperada(
                f"AirVault no completo el batch {batch_id}: "
                f"{respuesta.get('Message') or 'sin motivo'}"
            )
        return respuesta

    def validar_batch(
        self, batch_id: str, paginas: Sequence[int],
    ) -> List[Mapping[str, Any]]:
        """Ejecuta la validacion de repositorio previa a ``Complete``.

        MXDocs anuncia ``data-batch-level-validation=1``. Por eso el boton
        oficial no pasa directamente de ver las paginas verdes a
        ``CompleteBatch``: primero envia las cabeceras de documento a
        ``UpdateBatchValidationQuery``. Esa consulta puede devolver alguna
        pagina a amarillo aunque sus campos obligatorios esten llenos.

        Se conserva el identificador del batch sin Base64 y la lista
        separada por comas porque ese es el contrato exacto del cliente web
        de AirVault para esta ruta.
        """
        cabeceras = sorted({int(p) for p in paginas if int(p) > 0})
        if not cabeceras:
            return []
        respuesta = self.sesion.post_json(
            "/index/Batch/UpdateBatchValidationQuery",
            data={
                "batchId": batch_id,
                "repoId": self.config.repo_id,
                "pageRangeDelimiter": ",".join(map(str, cabeceras)),
            },
        )
        if not isinstance(respuesta, (list, tuple)):
            raise RespuestaInesperada(
                f"AirVault no devolvio el resultado de validar el batch "
                f"{batch_id}, sino {_describir(respuesta)}."
            )
        return [fila for fila in respuesta if isinstance(fila, Mapping)]

    # ── paginas ────────────────────────────────────────────────────

    def leer_pagina(self, batch_id: str, pagina: int) -> PaginaIndexada:
        datos = self.sesion.get(
            "/index/FormsProcessing/GetIndexFields",
            {"encodedBatchId": codificar_batch_id(batch_id),
             "repoId": self.config.repo_id, "page": pagina},
        )
        if not isinstance(datos, Mapping):
            raise RespuestaInesperada(
                f"AirVault no devolvio los campos de la pagina {pagina} del "
                f"batch {batch_id}, sino {_describir(datos)}. Suele ser que "
                f"el batch ya no tenga esa pagina."
            )
        valores: Dict[int, str] = {}
        columnas: Dict[str, str] = {}
        for campo in (datos.get("RepoFields") or []):
            try:
                field_id = int(campo.get("FieldId"))
            except (TypeError, ValueError):
                continue
            valor = campo.get("Value")
            valores[field_id] = "" if valor is None else str(valor)
            columna = str(campo.get("ColumnName", "")).strip()
            if columna:
                columnas[columna] = valores[field_id]
        try:
            estado = int(datos.get("Status", 0))
        except (TypeError, ValueError):
            estado = 0
        return PaginaIndexada(
            pagina=pagina, estado=estado, valores=valores, columnas=columnas
        )

    def guardar_pagina(
        self, batch_id: str, pagina: int, valores: Mapping[int, str],
        estado: int, pagina_siguiente: Optional[int] = None,
    ) -> Mapping[str, Any]:
        """Guarda los valores de una pagina y deja abierta la siguiente.

        Va por POST, que es como lo manda el Web Index. Por GET la ruta ni
        siquiera existe: ASP.NET contesta «The resource cannot be found»
        con un 404, que se leia como «esa pagina ya no esta en el batch».
        """
        respuesta = self.sesion.post_json(
            "/index/FormsProcessing/SaveAndGetIndexFields",
            data={
                "encodedBatchId": codificar_batch_id(batch_id),
                "repoId": self.config.repo_id,
                "page": pagina,
                "nextPageToOpen": (
                    pagina if pagina_siguiente is None else pagina_siguiente
                ),
                "encodedValues": codificar_valores(valores),
                "encodedSticky": codificar_sticky([]),
                "status": estado,
            },
        )
        if isinstance(respuesta, Mapping) and str(
            respuesta.get("IsError", "false")
        ).strip().casefold() in ("true", "1"):
            from app.airvault.session import ErrorDeAirVault

            raise ErrorDeAirVault(
                f"AirVault rechazo la pagina {pagina}: "
                f"{respuesta.get('Message') or 'no confirmo el guardado'}"
            )
        return respuesta

    # ── catalogos ──────────────────────────────────────────────────

    def picklist_matriculas(self) -> List[str]:
        """Valores validos del campo Aircraft, tal como los define el admin."""
        from app.airvault.config import CAMPO_MATRICULA

        datos = self.sesion.get(
            "/index/PickList/GetPickListViews",
            {"repoId": self.config.repo_id,
             "indexSchemeId": self.config.index_scheme_id,
             "dateTime": 0},
        )
        return _valores_de_picklist(datos, CAMPO_MATRICULA)


def _valores_de_picklist(datos: Any, field_id: int) -> List[str]:
    """Extrae los valores del picklist asociado a un campo.

    La respuesta viene como una lista de vistas y el formato exacto depende
    de la version del servidor, asi que se recorre buscando la vista que
    mencione el campo y de ahi se sacan las cadenas.
    """
    vistas = datos.values() if isinstance(datos, Mapping) else datos
    for vista in vistas or []:
        if not isinstance(vista, Mapping):
            continue
        referencias = {
            str(vista.get(clave, "")) for clave in
            ("FieldId", "fieldId", "PickListId", "picklistId")
        }
        if str(field_id) not in referencias:
            continue
        valores: List[str] = []
        for item in (vista.get("Items") or vista.get("items") or []):
            if isinstance(item, Mapping):
                texto = item.get("Value") or item.get("value") or item.get(
                    "Text"
                )
            else:
                texto = item
            if texto:
                valores.append(str(texto).strip())
        if valores:
            return valores
    return []
