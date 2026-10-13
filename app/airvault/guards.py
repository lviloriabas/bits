"""Guardas previas a escribir en AirVault.

Escribir en la pagina equivocada es el unico error de este modulo que no se
puede deshacer con comodidad: quedaria una bitacora publicada con la
matricula de otro avion. Por eso todas las comprobaciones viven juntas,
son puras y se ejecutan igual en dry run que en automatico. Si alguna falla
el indexado se detiene; nunca escribe "lo que pueda".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Set

from app.airvault.config import (
    CAMPO_END_DATE,
    CAMPO_LOG_NUMBER,
    CAMPO_MATRICULA,
    CAMPOS_OBLIGATORIOS,
    ESTADO_VALIDO,
    nombre_campo,
)
from app.airvault.mapping import normalizar_log_number, normalizar_matricula
from app.airvault.model import Registro
from app.validation.book_memory import clave_de_libro


@dataclass(frozen=True)
class Aviso:
    """Un problema detectado, con la pagina a la que corresponde."""

    seq: int
    codigo: str
    detalle: str

    def __str__(self) -> str:
        return f"[{self.codigo}] pagina {self.seq}: {self.detalle}"


class ErrorDeGuarda(RuntimeError):
    """El batch no cumple una condicion que impide escribir nada."""


def verificar_cantidad(
    registros: Sequence[Registro],
    paginas_lote: int,
    separadores_borrados: int = 0,
) -> None:
    """El manifiesto y el batch tienen que tener las mismas paginas.

    Es la guarda mas importante: si sobran o faltan paginas, la
    correspondencia por posicion esta rota y cualquier escritura cae en la
    bitacora de al lado.

    Los separadores cuentan: en el batch ocupan una pagina cada uno, igual
    que en el PDF que se subio.
    """
    cantidades_validas = {len(registros)}
    if separadores_borrados:
        cantidades_validas.add(len(registros) - separadores_borrados)
    if paginas_lote in cantidades_validas:
        return
    separadores = sum(1 for r in registros if r.es_separador)
    detalle = f", {separadores} de ellos separadores" if separadores else ""
    if paginas_lote <= 0:
        raise ErrorDeGuarda(
            f"AirVault no dijo cuantas paginas tiene el batch, y el manifiesto "
            f"espera {len(registros)}{detalle}. Suele pasar cuando el batch "
            f"todavía se esta procesando en el servidor o cuando el batch "
            f"anotado ya no existe: conviene mirarlo en AirVault antes de "
            f"volver a intentar. No se escribe nada."
        )
    faltan = len(registros) - paginas_lote
    causa = (
        f"al batch le faltan {faltan} paginas de las que trae el PDF"
        if faltan > 0
        else f"el batch tiene {-faltan} paginas de mas que el PDF de la ejecución"
    )
    raise ErrorDeGuarda(
        f"El batch tiene {paginas_lote} paginas y el manifiesto "
        f"{len(registros)}{detalle}: {causa}. Escribir asi correria cada "
        f"dato a la bitacora de al lado, asi que no se escribe nada. Casi "
        f"siempre es que se subio un PDF distinto al que se preparo, o que "
        f"el batch quedo a medio subir."
    )


def verificar_matriculas(
    registros: Iterable[Registro], picklist: Iterable[str]
) -> List[Aviso]:
    """Toda matricula debe existir en el picklist de AirVault."""
    validas = {str(v).strip().upper() for v in picklist if str(v).strip()}
    avisos: List[Aviso] = []
    for registro in registros:
        if registro.es_separador:
            continue
        if not registro.matricula:
            avisos.append(
                Aviso(
                    registro.seq,
                    "matricula_vacia",
                    "no se pudo leer la matricula",
                )
            )
        elif validas and registro.matricula.upper() not in validas:
            avisos.append(
                Aviso(
                    registro.seq,
                    "matricula_desconocida",
                    f"{registro.matricula} no esta en el picklist de AirVault",
                )
            )
    return avisos


def matriculas_por_libro(paginas_remotas: Iterable[object]) -> Dict[str, str]:
    """El avion que AirVault ya tiene escrito en cada libro del batch.

    Un libro fisico tiene cincuenta paginas y una sola aeronave, asi que
    cualquier pagina suya que AirVault de por buena responde por todas las
    demas. Sale de las paginas que el plan acaba de leer, asi que no cuesta
    ni una peticion de mas.

    Solo cuentan las verdes: en cualquier otro estado lo que se ve en
    Aircraft es la clasificacion inicial de Quick Upload, que pone en todo
    el archivo el avion de la primera bitacora (el mismo motivo por el que
    :func:`verificar_alineacion` tampoco la mira). Un libro del que AirVault
    dice dos aviones distintos se queda fuera: ahi no hay una autoridad,
    hay un desacuerdo, y elegir uno seria elegir al azar.
    """
    vistas: Dict[str, Set[str]] = {}
    for pagina in paginas_remotas:
        if getattr(pagina, "estado", None) != ESTADO_VALIDO:
            continue
        valores = getattr(pagina, "valores", None)
        if not isinstance(valores, Mapping):
            continue
        clave = clave_de_libro(
            normalizar_log_number(valores.get(CAMPO_LOG_NUMBER, ""))
        )
        matricula = normalizar_matricula(valores.get(CAMPO_MATRICULA, ""))
        if not clave or not matricula:
            continue
        vistas.setdefault(clave, set()).add(matricula)
    return {
        clave: next(iter(matriculas))
        for clave, matriculas in vistas.items()
        if len(matriculas) == 1
    }


def verificar_matricula_del_libro(
    registros: Iterable[Registro], por_libro: Mapping[str, str]
) -> List[Aviso]:
    """Ninguna pagina lleva un avion distinto al de su libro.

    Es la comprobacion que contrasta lo leido con AirVault mas alla de la
    propia pagina. :func:`verificar_alineacion` compara cada pagina con la
    suya, y por eso no ve nada cuando la pagina esta vacia en AirVault, que
    es el caso normal: es la primera vez que se indexa. Esta compara con las
    hermanas del mismo libro que si estan escritas, que es de donde sale la
    evidencia de que un digito se leyo mal.

    Bloquea en vez de avisar. Una matricula equivocada es el unico error de
    este modulo que no se deshace con comodidad: queda una bitacora
    publicada a nombre de otro avion, y corregirla despues pide encontrarla
    primero. Dejar la pagina sin indexar cuesta escribirla a mano.
    """
    if not por_libro:
        return []
    avisos: List[Aviso] = []
    for registro in registros:
        if registro.es_separador or not registro.matricula:
            continue
        clave = clave_de_libro(normalizar_log_number(registro.log_number))
        esperada = por_libro.get(clave)
        if not esperada:
            continue
        if normalizar_matricula(registro.matricula) == esperada:
            continue
        avisos.append(
            Aviso(
                registro.seq,
                "matricula_del_libro",
                f"AirVault tiene el libro {clave} en {esperada} y esta "
                f"pagina trae {registro.matricula}",
            )
        )
    return avisos


def verificar_obligatorios(
    registro: Registro,
    valores: Mapping[int, str],
    permitir_fecha_dudosa: bool = False,
    permitir_incompletos: bool = False,
) -> List[Aviso]:
    """Ningun campo obligatorio puede ir vacio.

    No es que la pagina quede amarilla: AirVault ni siquiera la guarda.
    Contesta 500 con «Field <campo> value is required», asi que mandarla no
    consigue una pagina pendiente de revision, consigue un rechazo.

    El aviso nombra el campo como se llama en la pantalla de AirVault: quien
    lo lee tiene que poder ir a la bitacora y ver que falta, sin traducir un
    numero de campo.
    """
    avisos: List[Aviso] = []
    for campo in CAMPOS_OBLIGATORIOS:
        if not str(valores.get(campo, "")).strip():
            if (
                campo == CAMPO_END_DATE
                and registro.fecha_dudosa
                and permitir_fecha_dudosa
            ):
                avisos.append(
                    Aviso(
                        registro.seq,
                        "fecha_dudosa",
                        "End Date se omite en REVISAR porque el ano leido "
                        "no pudo confirmarse",
                    )
                )
                continue
            if permitir_incompletos and campo not in valores:
                avisos.append(Aviso(
                    registro.seq,
                    "indice_incompleto",
                    f"{nombre_campo(campo)} queda pendiente; se guardan "
                    "los demas datos disponibles sin enviar este campo vacio",
                ))
                continue
            avisos.append(
                Aviso(
                    registro.seq,
                    "obligatorio_vacio",
                    f"{nombre_campo(campo)} quedaria vacio y AirVault no "
                    f"acepta la pagina: contesta «Field "
                    f"{nombre_campo(campo)} value is required»",
                )
            )
    return avisos


def verificar_alineacion(
    registro: Registro,
    valores_en_airvault: Mapping[int, str],
    permitir_log_distinto: bool = False,
    estado_pagina: int | None = None,
) -> List[Aviso]:
    """Contrasta la pagina del batch con lo que dice el manifiesto.

    Cuando AirVault ya trae un log number para esa pagina, es el mejor
    ancla que existe: si no coincide con el nuestro, o el PDF se subio en
    otro orden o el CSV no corresponde a este batch. En los dos casos hay que
    parar.

    Si AirVault no trae nada, no se puede contrastar y se sigue por
    posicion, que es lo unico disponible. La matricula remota no sirve de
    ancla: Quick Upload clasifica el archivo entero con el Aircraft de su
    primera bitacora, asi que la trae puesta cada pagina del batch y es
    nuestra, no de AirVault.

    Tampoco sirve de ancla un log remoto que no es un numero de bitacora.
    AirVault lee la letra por su cuenta al recibir el archivo y a veces deja
    «00» o «1» en una pagina que nadie ha indexado. Eso no es el numero de
    otra bitacora: es una lectura fallida, y tomarla por un PDF corrido
    dejaba esa pagina sin escribir y el batch en amarillo por ella sola. En
    una pagina ya verde si cuenta: ahi lo dejo el indexado o una persona.
    """
    avisos: List[Aviso] = []
    log_remoto = str(valores_en_airvault.get(CAMPO_LOG_NUMBER, "")).strip()
    lectura_fallida = (
        estado_pagina is not None
        and estado_pagina != ESTADO_VALIDO
        and not normalizar_log_number(log_remoto)
    )
    if (
        log_remoto
        and registro.log_number
        and log_remoto != registro.log_number
        and not permitir_log_distinto
        and not lectura_fallida
    ):
        avisos.append(
            Aviso(
                registro.seq,
                "desalineado",
                f"AirVault tiene el log {log_remoto} y el manifiesto "
                f"{registro.log_number}",
            )
        )
    mat_remota = str(valores_en_airvault.get(CAMPO_MATRICULA, "")).strip()
    if (
        mat_remota
        and registro.matricula
        and mat_remota.upper() != registro.matricula.upper()
        and not log_remoto
        # Solo la matricula de una pagina ya verde es evidencia: ahi la
        # escribio el indexado o una persona. En cualquier otro estado
        # (amarilla, sin plantilla, recien subida) lo que hay es la
        # clasificacion inicial de Quick Upload, que es la misma para todo
        # el archivo. Contrastarla no descubre un PDF corrido; acusa a toda
        # pagina cuyo avion no sea el de la primera bitacora y las deja sin
        # indexar, que era justo lo que dejaba el batch entero en amarillo.
        and estado_pagina == ESTADO_VALIDO
    ):
        avisos.append(
            Aviso(
                registro.seq,
                "matricula_distinta",
                f"AirVault tiene {mat_remota} y el manifiesto {registro.matricula}",
            )
        )
    return avisos


def verificar_no_pisar(
    registro: Registro, estado_pagina: int, sobrescribir: bool
) -> List[Aviso]:
    """Una pagina ya validada no se toca salvo que se pida expresamente."""
    if estado_pagina == ESTADO_VALIDO and not sobrescribir:
        return [
            Aviso(
                registro.seq,
                "ya_indexada",
                "la pagina ya esta en Valid en AirVault; se deja como esta",
            )
        ]
    return []


def verificar_duplicados(registros: Sequence[Registro]) -> List[Aviso]:
    """Dos bitacoras con el mismo log number son un error de lectura."""
    vistos: dict[str, int] = {}
    avisos: List[Aviso] = []
    for registro in registros:
        if registro.es_separador or not registro.log_number:
            continue
        anterior = vistos.get(registro.log_number)
        if anterior is not None:
            avisos.append(
                Aviso(
                    registro.seq,
                    "log_duplicado",
                    f"el log {registro.log_number} ya salio en la pagina {anterior}",
                )
            )
        else:
            vistos[registro.log_number] = registro.seq
    return avisos
