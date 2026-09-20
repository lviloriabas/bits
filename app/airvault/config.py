"""Configuracion del indexado en AirVault.

Todo lo que cambia entre instalaciones vive en ``airvault.json``, junto al
programa. Nada de credenciales: eso se resuelve en ``app.airvault.session``
y no se guarda en disco.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

AIRVAULT_FILENAME = "airvault.json"
# Los valores de partida, que si viajan en el repositorio. El archivo real es
# local de cada instalacion: se escribe solo al mover un control y no se
# versiona, para que un pull no choque con las preferencias de la maquina.
AIRVAULT_EXAMPLE_FILENAME = "airvault.example.json"
_CONFIG_WRITE_LOCK = threading.Lock()

# Identificadores de campo del repositorio MXDocs (repoId 3209). Son
# estables: los asigna el administrador de AirVault al definir el
# repositorio, no cambian entre batches.
CAMPO_DOC_TYPE = 9586
CAMPO_WORK_LOCATION = 9624
CAMPO_WORK_TYPE = 9627
CAMPO_MATRICULA = 9633
CAMPO_DESCRIPCION = 9752
CAMPO_FLEET = 9699
CAMPO_LESSOR = 9783
CAMPO_LOG_NUMBER = 9675
CAMPO_AUDIT_STATUS = 9754
CAMPO_START_DATE = 9594
CAMPO_END_DATE = 9593
CAMPO_BATCH_NAME = 9631
# Solo existe en Quick Upload. Viaja con el mismo nombre del batch: ver
# :func:`app.airvault.uploader.valores_quick_upload`.
CAMPO_BATCH_USERNAME = 9809

# Los seis obligatorios del tipo de documento por defecto. Si alguno queda
# vacio la pagina se guarda en amarillo (estado 3, "Need Correction"), asi
# que el indexador se niega a escribir antes de llegar a eso.
CAMPOS_OBLIGATORIOS = (
    CAMPO_DOC_TYPE,
    CAMPO_MATRICULA,
    CAMPO_FLEET,
    CAMPO_LOG_NUMBER,
    CAMPO_AUDIT_STATUS,
    CAMPO_END_DATE,
)

ESTADO_VALIDO = 0
ESTADO_SIN_PLANTILLA = 1
ESTADO_SEPARADOR = 2
ESTADO_NECESITA_CORRECCION = 3

NOMBRE_ESTADO = {
    ESTADO_VALIDO: "Valid",
    ESTADO_SIN_PLANTILLA: "No Template Match",
    ESTADO_SEPARADOR: "Separator",
    ESTADO_NECESITA_CORRECCION: "Need Correction",
}

# Como se llama cada campo en la pantalla de AirVault. Un aviso que dice
# «el campo 9633 quedaria vacio» no se puede leer sin abrir el codigo; el
# mismo aviso con el nombre se resuelve mirando la bitacora.
NOMBRE_CAMPO = {
    9692: "ECN Reason",
    9781: "2nd ECN Reason",
    9782: "3rd ECN Reason",
    CAMPO_DOC_TYPE: "Doc Type",
    CAMPO_WORK_LOCATION: "Work Location",
    CAMPO_WORK_TYPE: "Work Type",
    CAMPO_MATRICULA: "Aircraft",
    CAMPO_DESCRIPCION: "Description",
    CAMPO_FLEET: "Fleet",
    CAMPO_LESSOR: "Lessor",
    CAMPO_LOG_NUMBER: "Log Page Number",
    CAMPO_AUDIT_STATUS: "Audit Status",
    CAMPO_START_DATE: "Start Date",
    CAMPO_END_DATE: "End Date",
    CAMPO_BATCH_NAME: "Batch Name",
    CAMPO_BATCH_USERNAME: "Batch Username",
}


def nombre_campo(field_id: int) -> str:
    """Nombre legible de un campo; el numero solo si no se conoce."""
    return NOMBRE_CAMPO.get(field_id, f"campo {field_id}")


@dataclass(frozen=True)
class AirVaultConfig:
    """Parametros de conexion y valores por defecto del indexado."""

    base_url: str = "https://airvault.criticaltech.com"
    # Enlace de acceso federado. Es el que dispara la redireccion a Entra ID;
    # entrar por la raiz deja la sesion sin la cookie de federacion.
    url_sso: str = (
        "https://airvault.criticaltech.com/zfp/?whr="
        "https://login.microsoftonline.com/"
        "9767f0dc-e83f-4cc1-94e1-0d5f9d287d32/wsfed"
    )
    repo_id: int = 3209
    index_scheme_id: int = 137
    # El picklist de Doc Type contiene "Log Page"; los batches cargados hasta
    # hoy llevan "LOG PAGE", que no existe en ese picklist y la interfaz
    # solo conserva porque lo agrega al combo. Se deja configurable para no
    # decidir por el administrador, con el valor valido como defecto.
    doc_type: str = "Log Page"
    audit_status: str = "PUBLISHED"
    # Audit Status de las bitacoras marcadas como discrepancia, las que van
    # bajo el separador «POSIBLES DISCREPANCIAS». Se escribe distinto para
    # que en AirVault se vea de una vez cual es la que hay que auditar. Es
    # configurable porque el picklist del campo no se lee de la API: si el
    # valor no existiera en el repositorio, se corrige aqui sin tocar el
    # codigo. Vacio deja a todas con el Audit Status normal.
    audit_status_discrepancia: str = "AUDIT IN PROGRESS"
    # Preferencia portable de la interfaz. No hay un valor fijo en el
    # codigo: la instalacion conserva aqui la ultima cantidad elegida tanto
    # al repartir la entrega como al preparar los batches de Quick Upload.
    paginas_por_batch: int | None = None
    # Preferencia de la interfaz sin valor impuesto por el programa. Cuando
    # la persona marca o desmarca «Completar batch», se conserva exactamente
    # ese último estado en la carpeta portable.
    completar_batch: bool | None = None
    # La comprobacion sigue avisando; solo esta preferencia decide si bloquea.
    detener_por_duplicados: bool = False
    # Como se representa la fecha en el CSV: «month_end» o «specific_day».
    # Tampoco tiene valor impuesto por el programa. Es una decision de quien
    # entrega, no del archivo que se abre, asi que la instalacion conserva
    # aqui la ultima elegida y el desplegable abre en ella. Vacio o con un
    # valor que no existe abre en la primera opcion del desplegable.
    csv_date_mode: str | None = None
    # Hasta donde llega el boton «Automatico» de la ventana principal. Son
    # preferencias de la interfaz, no del indexado, pero viven aqui por lo
    # mismo que «Completar batch»: es el unico archivo portable que la
    # instalacion se lleva consigo, y dos de los tres pasos son de AirVault.
    # Procesar y exportar no aparecen porque siempre se hacen; esperar a
    # AirVault tampoco, porque va dentro de subir y no se elige aparte. Un
    # «auto_esperar» y «auto_depurar» de versiones anteriores se ignoran al
    # leer el archivo: esperar va dentro de subir, y depurar ya no se hace
    # solo, porque de una bitacora repetida hay que ver las dos apariciones
    # antes de borrar ninguna.
    auto_subir: bool = True
    auto_indexar: bool = True
    # Si antes de subir se le pregunta a Web Search si esas bitacoras ya
    # estan publicadas. Apagado de fabrica: la ruta de busqueda no esta
    # documentada y el programa la adivina en ejecucion, asi que la consulta
    # cuesta varias peticiones y solo contesta cuando ese descubrimiento
    # acierta. Apagado queda el libro de envios, que es local, no falla y
    # tambien frena un batch repetido; lo que se pierde es ver un batch que
    # ya se completo y por eso salio de la cola de Web Index.
    buscar_publicadas: bool = False
    # Ruta de consulta de Web Search y nombre de la forma en que se le
    # mandan los parametros. AirVault no documenta su API, asi que las
    # descubre el programa la primera vez (ver
    # :mod:`app.airvault.websearch`) y las conserva aqui para no repetir el
    # recorrido en cada consulta. Vacias significa «todavia sin descubrir»,
    # nunca «no hay»: se vuelven a buscar en la siguiente comprobacion.
    ruta_websearch: str = ""
    parametros_websearch: str = ""
    # Segundos de espera entre sondeos al buscar el batch por nombre.
    espera_descubrimiento_s: float = 20.0
    espera_maxima_s: float = 900.0
    # Quick Upload puede tardar bastante en publicar el batch en Web Index.
    # Tras agotar las revisiones de nombres y contenido empieza esta espera;
    # al terminar se avisa de que la carga se perdio y se ofrece subirla a
    # mano. El nombre es historico: nada se reenvia solo.
    espera_reenvio_s: float = 1800.0
    # Tiempo limite de cada peticion. El servidor cuelga la peticion de
    # apertura cuando el mismo usuario tiene el batch abierto en otra sesion,
    # asi que sin limite el proceso se queda esperando para siempre.
    timeout_s: float = 60.0
    reintentos: int = 3
    # Espera antes de reintentar, multiplicada por el numero de intento. Un
    # corte de red o un servidor ocupado no se arreglan reintentando al
    # instante; darle aire evita convertir un tropiezo en un fallo.
    espera_reintento_s: float = 5.0
    # Cuanto se espera a que alguien entre a AirVault en la ventana que abre
    # el programa. Cinco minutos dan de sobra para un segundo factor.
    espera_login_s: float = 300.0
    # Perfil de Edge propio del programa. Vacio usa el de portable/.
    perfil_navegador: str = ""
    usuario: str = ""

    @classmethod
    def load(cls, path: Path | str) -> "AirVaultConfig":
        """Carga la configuracion; si no existe el archivo, usa defectos.

        Un clon recien hecho todavia no tiene ``airvault.json``, asi que
        antes de caer a los defectos del codigo se mira el ejemplo que si
        viene versionado: es de donde salen las paginas por batch y demas
        valores de partida.
        """
        ruta = Path(path)
        if not ruta.is_file():
            ruta = ruta.with_name(AIRVAULT_EXAMPLE_FILENAME)
        if not ruta.is_file():
            return cls()
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(datos, Mapping):
            return cls()
        # Guardar otra preferencia puede crear un JSON parcial. La cantidad
        # inicial sigue saliendo del archivo portable hasta que se elija una.
        if datos.get("paginas_por_batch") is None:
            ejemplo = ruta.with_name(AIRVAULT_EXAMPLE_FILENAME)
            try:
                iniciales = json.loads(ejemplo.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                iniciales = {}
            if isinstance(iniciales, Mapping) and iniciales.get("paginas_por_batch") is not None:
                datos = {**datos, "paginas_por_batch": iniciales["paginas_por_batch"]}
        return cls.from_mapping(datos)

    @classmethod
    def from_mapping(cls, datos: Mapping[str, Any]) -> "AirVaultConfig":
        campos = {f.name for f in cls.__dataclass_fields__.values()}
        limpio = {k: v for k, v in datos.items() if k in campos}
        return cls(**limpio)

    def with_overrides(self, **cambios: Any) -> "AirVaultConfig":
        """Copia con los valores que llegan por linea de comandos."""
        utiles = {k: v for k, v in cambios.items() if v is not None}
        return replace(self, **utiles) if utiles else self

    def url(self, ruta: str) -> str:
        return f"{self.base_url.rstrip('/')}/{ruta.lstrip('/')}"


def _actualizar(path: Path | str, valores: Mapping[str, Any]) -> bool:
    """Cambia unas claves del JSON portable sin perder las demas.

    Es la escritura que comparten todas las preferencias: se relee el
    archivo, se sustituye lo que cambia y se reemplaza entero de forma
    atomica. Con el candado puesto, porque la ventana de AirVault y la
    principal escriben aqui desde hilos distintos y una lectura a medias
    dejaria el archivo con la mitad de las opciones.
    """
    if not valores:
        return False
    ruta = Path(path)
    try:
        with _CONFIG_WRITE_LOCK:
            if ruta.is_file():
                datos = json.loads(ruta.read_text(encoding="utf-8"))
                if not isinstance(datos, Mapping):
                    return False
                datos = dict(datos)
            else:
                datos = {}
            datos.update(valores)
            ruta.parent.mkdir(parents=True, exist_ok=True)
            temporal = ruta.with_name(f"{ruta.name}.tmp")
            temporal.write_text(
                json.dumps(datos, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporal, ruta)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return True


def guardar_ruta_websearch(
    path: Path | str, ruta: str, parametros: str
) -> bool:
    """Conserva la consulta de Web Search que el programa encontro.

    Descubrirla cuesta leer la portada del modulo y probar varias rutas.
    Guardarla evita repetir ese recorrido en cada comprobacion, y borrarla
    del archivo basta para que se vuelva a descubrir.
    """
    limpia = str(ruta or "").strip()
    forma = str(parametros or "").strip()
    if not limpia or not forma:
        return False
    return _actualizar(
        path, {"ruta_websearch": limpia, "parametros_websearch": forma}
    )


def guardar_paginas_por_batch(path: Path | str, cantidad: int) -> bool:
    """Conserva la ultima cantidad elegida sin perder otras opciones.

    La preferencia vive en el JSON portable de AirVault, no en QSettings,
    porque la aplicacion completa debe poder moverse a otro Windows sin
    depender del registro ni de una ruta del perfil del usuario.
    """
    try:
        valor = int(cantidad)
    except (TypeError, ValueError):
        return False
    if valor <= 0:
        return False
    return _actualizar(path, {"paginas_por_batch": valor})


def guardar_csv_date_mode(path: Path | str, modo: str) -> bool:
    """Conserva la ultima politica de fecha elegida para el CSV.

    Junto a las demas preferencias portables, por lo mismo que las paginas
    por batch: la instalacion se mueve a otro Windows con su archivo.
    """
    valor = str(modo or "").strip()
    if not valor:
        return False
    return _actualizar(path, {"csv_date_mode": valor})


def guardar_preferencias(path: Path | str, **valores: bool) -> bool:
    """Conserva casillas de la interfaz sin perder el resto del archivo.

    Vale para «Completar batch» y para los pasos del proceso automatico:
    todas son casillas, todas viven en el mismo JSON portable y escribir
    varias a la vez evita releer el archivo una vez por cada una.
    """
    return _actualizar(
        path, {clave: bool(valor) for clave, valor in valores.items()}
    )


def guardar_completar_batch(path: Path | str, marcado: bool) -> bool:
    """Conserva el último estado de «Completar batch» en el JSON portable."""
    return guardar_preferencias(path, completar_batch=marcado)
