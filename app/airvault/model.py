"""Modelo del trabajo de indexado: registros y manifiesto.

El manifiesto es la unica fuente de verdad del trabajo. Cada etapa lo lee,
hace lo suyo y lo vuelve a escribir, de modo que un proceso interrumpido se
retoma donde quedo y una etapa se puede correr sola sin volver a ejecutar
las anteriores.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import ClassVar, Dict, List, Optional

from pydantic import BaseModel, Field


class EstadoEtapa(str, Enum):
    """Estado de una etapa del trabajo."""

    PENDIENTE = "pendiente"
    EN_CURSO = "en_curso"
    HECHA = "hecha"
    ERROR = "error"
    OMITIDA = "omitida"


class EstadoRegistro(str, Enum):
    """Estado de una bitacora dentro del trabajo."""

    PENDIENTE = "pendiente"
    ESCRITA = "escrita"
    OMITIDA = "omitida"
    ERROR = "error"


class Etapa(BaseModel):
    """Una etapa del trabajo con su estado y su ultimo mensaje."""

    estado: EstadoEtapa = EstadoEtapa.PENDIENTE
    actualizada: Optional[str] = None
    detalle: str = ""

    def marcar(self, estado: EstadoEtapa, detalle: str = "") -> None:
        self.estado = estado
        self.detalle = detalle
        self.actualizada = datetime.now().isoformat(timespec="seconds")


class Registro(BaseModel):
    """Una bitacora: de donde salio, que se le va a escribir y como quedo."""

    # Posicion dentro del artefacto que se sube. Es la que debe coincidir
    # con la pagina del batch en AirVault cuando el batch se arma con el PDF
    # ordenado de la ejecución.
    seq: int
    archivo_origen: str = ""
    pagina_origen: int = 0

    # Etiqueta del separador cuando esta posicion del batch no es una
    # bitacora sino una pagina divisoria del PDF de entrega («REVISAR»,
    # «POSIBLES DISCREPANCIAS», la matricula de un grupo). Ocupa su lugar en
    # el batch para que la correspondencia por posicion siga en pie, pero no
    # se le escribe nada: no es un documento que indexar.
    separador: str = ""

    matricula: str = ""
    log_number: str = ""
    # Numero de vuelo tal como lo dejo la lectura: un vuelo numerado
    # (``703``, ``CM137``) o un codigo de mantenimiento del vocabulario
    # (``TCK``, ``SPV``). Va al campo Description de AirVault.
    flight_number: str = ""
    # Fecha en el formato del CSV de la ejecución (YYYY/MM/dd). La conversion
    # al formato de AirVault (m/d/Y) se hace al construir los valores, no
    # aqui, para que el manifiesto se siga leyendo igual que el CSV.
    fecha: str = ""
    # La lectura original se conserva en el reporte, pero no se envia al
    # indexar el batch REVISAR porque su ano quedo sin confirmar.
    fecha_dudosa: bool = False
    # Regla con la que se dedujo la fecha cuando la bitacora no la trajo
    # leida (``app/airvault/fechas.py``). Vacio cuando la fecha es la que
    # se leyo de la pagina, que es el caso normal.
    fecha_inferida: str = ""
    fleet: str = ""
    lessor: str = ""
    fleet_inferido: bool = False

    duplicado: bool = False
    discrepancia: bool = False
    discrepancy_fields: List[str] = Field(default_factory=list)
    discrepancy_reason: str = ""
    # True conserva una causa de revision independiente de los faltantes
    # (firmas, fecha dudosa o contradiccion del libro). False permite validar
    # cuando el indice ya esta completo. None conserva la revision de las
    # entregas antiguas que no detallaban el motivo.
    revision_pendiente: Optional[bool] = None

    pagina_batch: Optional[int] = None
    estado: EstadoRegistro = EstadoRegistro.PENDIENTE
    avisos: List[str] = Field(default_factory=list)

    @property
    def es_separador(self) -> bool:
        return bool(self.separador)

    def listo_para_escribir(self) -> bool:
        if self.es_separador:
            return False
        return self.estado is EstadoRegistro.PENDIENTE and not self.avisos


class Manifiesto(BaseModel):
    """Estado completo de un trabajo de indexado."""

    version: int = 1
    job_id: str
    creado: str = Field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    nombre_batch: str = ""
    repo_id: int = 3209
    batch_id: Optional[str] = None
    csv_origen: str = ""
    # Archivo de entrega que forma este batch. Una ejecución repartida en
    # partes tiene un manifiesto por parte, cada uno con su PDF y su batch.
    pdf_origen: str = ""
    parte: int = 1
    partes: int = 1
    # Maximo elegido al preparar los archivos que van a Quick Upload.
    # Cero identifica manifiestos antiguos, anteriores a este reparto.
    paginas_por_batch: int = 0
    # Si el PDF interno que se envia fue rasterizado a 200 DPI. Se guarda
    # para que una reanudacion use exactamente el mismo archivo y ajuste.
    compresion: bool = False
    # Si las fechas de este batch se escribieron con el ultimo dia del mes.
    # Va en el manifiesto y no solo en la ventana porque decide lo que se
    # escribe en AirVault: un trabajo guardado tiene que reanudarse con la
    # misma politica con la que se armo, y cambiarla obliga a rehacerlo.
    fin_de_mes: bool = False
    # El batch recoge bitacoras con algun dato dudoso o en conflicto. Se
    # indexan automaticamente los valores confirmados y queda abierto para
    # revisar solo lo que falta o no es seguro.
    solo_subir: bool = False
    # Foto de la cola inmediatamente anterior a Quick Upload. Si AirVault
    # pierde C_BatchName y publica ``Empty-Batch``, acota los candidatos que
    # despues se confirman por paginas y contenido.
    lotes_previos: List[str] = Field(default_factory=list)
    # Mezclas identificadas por su contenido y apartadas antes de resubir.
    batches_descartados: List[str] = Field(default_factory=list)
    mezcla_pendiente: str = ""
    resubir_por_mezcla: bool = False
    # Antes de considerar perdida una subida, varias revisiones completas
    # recorren nombres, cantidades y contenido. Solo al agotarlas empieza el
    # reloj de espera que eventualmente permite ofrecer una resubida.
    intentos_identificacion: int = 0
    # Cuando arranco ese reloj. El nombre es historico: al vencer no se
    # reenvia nada, solo se avisa y se ofrece subir el archivo a mano.
    espera_reenvio_desde: str = ""
    # Veces que se recorrio la cola entera de AirVault (por nombre visible,
    # nombre embebido, cantidad de paginas y Log Page Number) sin encontrar
    # esta carga. Una sola basta para darla por no publicada: ya no queda
    # nombre bajo el que pueda estar.
    busquedas_amplias_sin_hallar: int = 0
    # Alguien autorizo expresamente subir este batch aunque deje paginas
    # amarillas (sin algun campo obligatorio) en AirVault. Lo normal es
    # evitarlas repartiendo esas bitacoras al batch REVISAR, pero cuando el
    # archivo ya esta hecho rehacerlo cuesta mas que terminar de indexar a
    # mano esas paginas, asi que la decision es de quien sube. Se guarda en
    # el manifiesto para que un reintento no vuelva a preguntar.
    amarillas_permitidas: bool = False
    # Se saco de la cola a mano. No se sube, no se busca y no se indexa
    # hasta que alguien lo reanude; lo que ya este hecho se conserva.
    cancelado: bool = False
    # Por que se sospecha que este batch ya esta en AirVault. Vacio es lo
    # normal; con texto, el batch no se sube, no se reenvia y no se
    # completa solo, y ese mismo texto es lo que se enseña en la cola.
    #
    # Es una sospecha y no una certeza a proposito: las senales que la
    # levantan (una bitacora suya ya anotada en otro batch, o publicada en
    # Web Search) demuestran que el documento esta arriba, no que este
    # batch sea el que lo subio. Decidirlo es de quien mira AirVault, y por
    # eso se puede quitar a mano; lo que el programa no hace es seguir solo.
    posible_duplicado: str = ""
    # El propio programa cerro el batch al terminar de indexarlo. Distingue
    # lo que quedo terminado sin que nadie interviniera de lo que ya estaba
    # cerrado en AirVault cuando se encontro.
    completado_automatico: bool = False
    doc_type: str = "Log Page"
    audit_status: str = "PUBLISHED"
    # Audit Status de las bitacoras marcadas como discrepancia. Viaja en el
    # manifiesto para que un trabajo guardado se reanude con el mismo valor
    # con el que se planifico.
    audit_status_discrepancia: str = "AUDIT IN PROGRESS"

    etapas: Dict[str, Etapa] = Field(default_factory=dict)
    registros: List[Registro] = Field(default_factory=list)

    # Orden canonico de las etapas. Es una constante de clase, no un campo:
    # no tiene por que viajar en el JSON del manifiesto ni cambiar por
    # trabajo.
    ORDEN_ETAPAS: ClassVar[tuple[str, ...]] = (
        "procesar", "preparar", "subir", "descubrir", "indexar", "verificar",
    )

    def etapa(self, nombre: str) -> Etapa:
        """Devuelve la etapa, creandola en pendiente si no existia."""
        if nombre not in self.etapas:
            self.etapas[nombre] = Etapa()
        return self.etapas[nombre]

    def etapa_hecha(self, nombre: str) -> bool:
        etapa = self.etapas.get(nombre)
        return etapa is not None and etapa.estado in (
            EstadoEtapa.HECHA, EstadoEtapa.OMITIDA
        )

    def etapas_previas(self, nombre: str) -> List[str]:
        """Etapas que deben estar hechas antes de correr ``nombre``."""
        if nombre not in self.ORDEN_ETAPAS:
            return []
        return list(self.ORDEN_ETAPAS[: self.ORDEN_ETAPAS.index(nombre)])

    def pendientes(self) -> List[Registro]:
        return [r for r in self.registros
                if r.estado is EstadoRegistro.PENDIENTE
                and not r.es_separador]

    def escritos(self) -> List[Registro]:
        return [r for r in self.registros
                if r.estado is EstadoRegistro.ESCRITA]

    def con_avisos(self) -> List[Registro]:
        return [r for r in self.registros if r.avisos]

    def bitacoras(self) -> List[Registro]:
        """Registros que son documentos, sin las paginas divisorias."""
        return [r for r in self.registros if not r.es_separador]

    def separadores(self) -> List[Registro]:
        return [r for r in self.registros if r.es_separador]

    def separadores_borrados(self) -> int:
        """Divisorias que el ultimo indexado confirmo borradas en AirVault."""
        etapa = self.etapas.get("indexar")
        if etapa is None:
            return 0
        coincidencia = re.search(
            r"separadores borrados\s+(\d+)", etapa.detalle or "",
            flags=re.IGNORECASE,
        )
        if coincidencia is None:
            return 0
        return min(int(coincidencia.group(1)), len(self.separadores()))

    def cantidades_paginas_compatibles(self) -> set[int]:
        """Cantidades validas antes y despues de borrar divisorias."""
        total = len(self.registros)
        borrados = self.separadores_borrados()
        return {total, total - borrados} if borrados else {total}

    def resumen(self) -> Dict[str, int]:
        return {
            "registros": len(self.registros),
            "separadores": len(self.separadores()),
            "pendientes": len(self.pendientes()),
            "escritos": len(self.escritos()),
            "con_avisos": len(self.con_avisos()),
            "con_error": sum(
                1 for r in self.registros
                if r.estado is EstadoRegistro.ERROR
            ),
        }
