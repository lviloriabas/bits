"""Del CSV de la ejecución a los valores de indice de AirVault.

Aqui viven las tres traducciones que hacen falta: el formato de fecha, la
matricula contra el picklist de AirVault y la flota que AirVault deduce de
la matricula con su propio lookup.
"""

from __future__ import annotations

import csv
import json
import re
from calendar import monthrange
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

from app.airvault.config import (
    CAMPOS_OBLIGATORIOS,
    CAMPO_AUDIT_STATUS,
    CAMPO_BATCH_NAME,
    CAMPO_DESCRIPCION,
    CAMPO_DOC_TYPE,
    CAMPO_END_DATE,
    CAMPO_FLEET,
    CAMPO_LESSOR,
    CAMPO_LOG_NUMBER,
    CAMPO_MATRICULA,
    nombre_campo,
)
from app.airvault.fechas import fechas_inferidas
from app.airvault.model import Registro

FLOTA_CACHE_FILENAME = "airvault_flota.json"

_FECHA_CSV_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")
# Como vuelve una fecha leida de AirVault: la que se escribio, o el ISO que
# entrega alguna de sus vistas. La hora que a veces acompana no estorba.
_FECHA_AIRVAULT_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(20\d{2})\b")
_FECHA_ISO_RE = re.compile(r"^(20\d{2})-(\d{2})-(\d{2})(?=$|[Tt\s])")
# Sufijo con el que se numera un nombre repetido al apartar la entrada a
# «input/processed» (``bitacora-2.pdf``).
_SUFIJO_DE_COPIA_RE = re.compile(r"^(?P<base>.+)-\d+$")
_MATRICULA_RE = re.compile(r"^(HP|HK)-\d{4}(CMP|WWP)?$")
# Aviones que alguna vez se escribieron con un sufijo distinto al de su
# picklist. El 1522 es el unico: en las bitacoras aparece de las dos
# maneras, pero AirVault solo lo tiene como HP-1522CMP, asi que el
# postproceso, «fleet.json» y el CSV ya lo escriben siempre asi. La
# traduccion se queda para lo que se escribio antes de esa regla (un
# manifiesto viejo, un cache de flota heredado, una lista de flota que
# alguien no actualizo): sin ella esa carga no encontraria su avion.
_ALIAS_PICKLIST = {"HP-1522WWP": "HP-1522CMP"}
_LOG_NUMBER_RE = re.compile(r"^\d{7}$")

# Regla de respaldo cuando la matricula no esta en el cache. AirVault
# resuelve la flota con un procedimiento almacenado contra su base, asi que
# esto es solo una aproximacion: lo que salga de aqui se marca como
# inferido y el reporte de revision lo muestra aparte para que alguien lo
# confirme antes de escribir.
_PREFIJOS_FLOTA = (
    ("HK-", "EMB"),
    ("HP-98", "MAX"),
    ("HP-99", "MAX"),
)
_FLOTA_POR_DEFECTO = "NG"


def fecha_airvault(fecha_csv: str) -> str:
    """Convierte ``YYYY/MM/dd`` del CSV al ``MM/DD/YYYY`` de AirVault.

    Devuelve cadena vacia si la fecha no viene o no tiene el formato del
    CSV: es preferible que la guarda posterior acuse el campo obligatorio
    vacio a mandar una fecha inventada.
    """
    match = _FECHA_CSV_RE.match(str(fecha_csv or "").strip())
    if not match:
        return ""
    anio, mes, dia = match.groups()
    try:
        date(int(anio), int(mes), int(dia))
    except ValueError:
        return ""
    return f"{mes}/{dia}/{anio}"


def fecha_desde_airvault(valor: object) -> str:
    """La vuelta: lo que AirVault devuelve, en ``AAAA-MM-DD``, o vacio.

    Se acepta el ``MM/DD/AAAA`` que escribe :func:`fecha_airvault`, con o
    sin la hora detras, y tambien el ISO por si otra vista lo entrega asi.
    Una fecha en formato de epoca (``/Date(...)/``) se deja pasar de largo
    a proposito: convertirla obliga a decidir una zona horaria, y
    equivocarse ahi corre el dia de la bitacora.
    """
    limpio = str(valor or "").strip()
    encontrada = _FECHA_AIRVAULT_RE.match(limpio)
    if encontrada is not None:
        mes, dia, anio = encontrada.groups()
    else:
        encontrada = _FECHA_ISO_RE.match(limpio)
        if encontrada is None:
            return ""
        anio, mes, dia = encontrada.groups()
    try:
        return date(int(anio), int(mes), int(dia)).isoformat()
    except ValueError:
        return ""


def fecha_a_fin_de_mes(fecha_csv: str) -> str:
    """La misma fecha con el dia puesto en el ultimo del mes.

    Es la unica forma de representar la fecha que AirVault recibe cuando se
    indexa a fin de mes. Se aplica sobre lo que trae el CSV, asi que una
    ejecucion exportada con el dia exacto puede indexarse a fin de mes sin
    volver a procesarla; una que ya venia a fin de mes no cambia, porque su
    dia ya es el ultimo.

    Lo que no sea una fecha del CSV se devuelve tal cual: aqui no se
    inventa una fecha que no estaba.
    """
    encontrada = _FECHA_CSV_RE.match(str(fecha_csv or "").strip())
    if encontrada is None:
        return str(fecha_csv or "").strip()
    anio, mes, _dia = (int(parte) for parte in encontrada.groups())
    try:
        ultimo = monthrange(anio, mes)[1]
    except (ValueError, IndexError):
        return str(fecha_csv or "").strip()
    return f"{anio:04d}/{mes:02d}/{ultimo:02d}"


def normalizar_matricula(valor: str) -> str:
    """Deja la matricula como la escribe el picklist de AirVault.

    Incluye los alias de :data:`_ALIAS_PICKLIST`, los aviones que AirVault
    tiene con otro sufijo. Por aqui pasan tanto lo que se escribe como lo
    que se lee de vuelta, asi que comparar una pagina remota con su
    registro sigue funcionando.
    """
    limpio = str(valor or "").strip().upper().replace(" ", "")
    if not _MATRICULA_RE.fullmatch(limpio):
        return ""
    return _ALIAS_PICKLIST.get(limpio, limpio)


def normalizar_log_number(valor: str) -> str:
    """El log number son siete digitos exactos; cualquier otra cosa no va."""
    limpio = re.sub(r"\D", "", str(valor or ""))
    return limpio if _LOG_NUMBER_RE.fullmatch(limpio) else ""


class ResolutorFlota:
    """Traduce matricula a flota y arrendador.

    Primero mira el cache local, que se alimenta de lo que AirVault ya tiene
    indexado. Solo si no hay dato aplica la regla de prefijos, y en ese caso
    avisa de que el valor es inferido.
    """

    def __init__(self, cache: Mapping[str, Mapping[str, str]] | None = None):
        self._cache: Dict[str, Dict[str, str]] = {
            normalizar_matricula(k): {
                "fleet": str(v.get("fleet", "")),
                "lessor": str(v.get("lessor", "")),
            }
            for k, v in (cache or {}).items()
            if normalizar_matricula(k)
        }

    @classmethod
    def load(cls, path: Path | str) -> "ResolutorFlota":
        ruta = Path(path)
        if not ruta.is_file():
            return cls()
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if isinstance(datos, Mapping):
            return cls(datos.get("matriculas", datos))
        return cls()

    def guardar(self, path: Path | str) -> Path:
        ruta = Path(path)
        ruta.write_text(
            json.dumps(
                {"version": 1, "matriculas": self._cache},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return ruta

    def aprender(self, matricula: str, fleet: str, lessor: str = "") -> None:
        """Guarda un par visto en AirVault para no volver a inferirlo."""
        clave = normalizar_matricula(matricula)
        if not clave or not fleet:
            return
        actual = self._cache.setdefault(clave, {"fleet": "", "lessor": ""})
        actual["fleet"] = fleet
        if lessor:
            actual["lessor"] = lessor

    def resolver(self, matricula: str) -> tuple[str, str, bool]:
        """Devuelve ``(fleet, lessor, inferido)`` para la matricula."""
        clave = normalizar_matricula(matricula)
        if not clave:
            return "", "", False
        conocido = self._cache.get(clave)
        if conocido and conocido.get("fleet"):
            return conocido["fleet"], conocido.get("lessor", ""), False
        for prefijo, flota in _PREFIJOS_FLOTA:
            if clave.startswith(prefijo):
                return flota, "", True
        return _FLOTA_POR_DEFECTO, "", True

    def conocidas(self) -> int:
        return len(self._cache)


def leer_csv_corrida(path: Path | str) -> List[dict]:
    """Lee el CSV minimo de una ejecución respetando el BOM que escribe Excel."""
    ruta = Path(path)
    with ruta.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def obligatorios_vacios_por_pagina(
    filas: Iterable[Mapping[str, str]],
    resolutor: ResolutorFlota | None = None,
) -> Dict[tuple[str, int], tuple[str, ...]]:
    """Campos que dejarían amarilla cada página usando solo el CSV local.

    Recorre también las filas completamente vacías, que
    :func:`registros_desde_csv` omite al preparar un batch antiguo. La
    división de la entrega necesita conservarlas en ``REVISAR`` y saber que
    no son páginas completas solo porque AirVault todavía no tenga índices.

    Las fechas se infieren con el CSV entero por la misma ruta usada al
    construir el manifiesto. Por eso una fecha deducible no manda la página
    a revisión, pero una ``End Date`` que realmente quedaría vacía sí.
    """
    filas = list(filas)
    inferidas = fechas_inferidas(filas)
    resolutor = resolutor or ResolutorFlota()
    faltantes: Dict[tuple[str, int], tuple[str, ...]] = {}
    for seq, fila in enumerate(filas, start=1):
        archivo = str(fila.get("file", "")).strip()
        try:
            pagina = int(str(fila.get("page", "")).strip())
        except (TypeError, ValueError):
            continue
        registro = _registro_de_fila(seq, fila, resolutor, inferidas)
        # Doc Type y Audit Status son constantes no vacías aquí. Los demás
        # valores salen de la fila y del resolutor local, igual que durante
        # la preparación real del manifiesto.
        valores = valores_de_indice(registro, "Log Page", "PUBLISHED")
        vacios = tuple(
            nombre_campo(campo)
            for campo in CAMPOS_OBLIGATORIOS
            if not str(valores.get(campo, "")).strip()
        )
        if vacios:
            faltantes[(archivo, pagina)] = vacios
    return faltantes


def registros_desde_csv(
    filas: Iterable[Mapping[str, str]],
    resolutor: ResolutorFlota | None = None,
    orden: Sequence[tuple[str, int]] | None = None,
    fin_de_mes: bool = False,
) -> List[Registro]:
    """Construye los registros del manifiesto a partir del CSV.

    Args:
        filas: filas del CSV de la ejecución.
        resolutor: traductor de matricula a flota.
        orden: pares ``(archivo, pagina)`` en el orden exacto en que las
            paginas quedaron en el PDF que se sube. Sin esta lista se asume
            que el batch conserva el orden del CSV, que solo es cierto
            cuando el PDF se genero sin separar ni reordenar.

    Las paginas en blanco del CSV (sin log number ni matricula) se
    descartan: no llegan al PDF que se sube, asi que incluirlas descuadraria
    la correspondencia con las paginas del batch.
    """
    resolutor = resolutor or ResolutorFlota()
    filas = list(filas)
    inferidas = fechas_inferidas(filas)
    por_clave: Dict[tuple[str, int], Mapping[str, str]] = {}
    secuencia: List[Mapping[str, str]] = []
    for fila in filas:
        archivo = str(fila.get("file", "")).strip()
        try:
            pagina = int(str(fila.get("page", "")).strip())
        except ValueError:
            pagina = 0
        por_clave[(archivo, pagina)] = fila
        secuencia.append(fila)

    if orden is not None:
        elegidas = [por_clave[c] for c in orden if c in por_clave]
    else:
        elegidas = secuencia

    registros: List[Registro] = []
    seq = 0
    for fila in elegidas:
        if _en_blanco(fila):
            continue
        seq += 1
        registros.append(
            _registro_de_fila(seq, fila, resolutor, inferidas, fin_de_mes)
        )
    return registros


def _en_blanco(fila: Mapping[str, str]) -> bool:
    """La pagina no aporta ningun dato de indice, asi que no llega al PDF."""
    return not any((
        normalizar_matricula(fila.get("matricula", "")),
        normalizar_log_number(fila.get("log_number", "")),
        str(fila.get("date", "")).strip(),
    ))


def _registro_de_fila(
    seq: int,
    fila: Mapping[str, str],
    resolutor: ResolutorFlota,
    inferidas: Mapping[tuple[str, int], tuple[str, str]] | None = None,
    fin_de_mes: bool = False,
    fecha_dudosa: bool = False,
    revision_pendiente: bool | None = None,
) -> Registro:
    """Traduce una fila del CSV al registro que viaja en el manifiesto.

    ``inferidas`` trae las fechas deducidas para las bitacoras que llegaron
    sin ella (ver :mod:`app.airvault.fechas`). Solo se usa cuando la fila no
    trae una fecha propia: una lectura nunca se pisa con una deduccion.
    """
    matricula = normalizar_matricula(fila.get("matricula", ""))
    fleet, lessor, inferido = resolutor.resolver(matricula)
    archivo = str(fila.get("file", "")).strip()
    pagina = int(str(fila.get("page", "0")).strip() or 0)
    fecha = str(fila.get("date", "")).strip()
    fecha_inferida = ""
    if fecha_dudosa:
        fecha = ""
    elif not _FECHA_CSV_RE.match(fecha):
        fecha, fecha_inferida = (inferidas or {}).get(
            (archivo, pagina), (fecha, "")
        )
    if fin_de_mes:
        # Va detras de la deduccion a proposito: una fecha deducida se
        # indexa con la misma politica que una leida.
        fecha = fecha_a_fin_de_mes(fecha)
    from app.airvault.ecn import campos_del_resumen, normalizar_razon
    resumen_discrepancia = fila.get("disc_reason") or fila.get("discrepancia")
    return Registro(
        seq=seq,
        archivo_origen=archivo,
        pagina_origen=pagina,
        matricula=matricula,
        log_number=normalizar_log_number(fila.get("log_number", "")),
        flight_number=str(fila.get("flight_number", "")).strip().upper(),
        fecha=fecha,
        fecha_dudosa=fecha_dudosa,
        fecha_inferida=fecha_inferida,
        fleet=fleet if matricula else "",
        lessor=lessor,
        fleet_inferido=inferido and bool(matricula),
        duplicado=str(fila.get("dup", "")).strip().lower() == "true",
        discrepancia=str(fila.get("disc", "")).strip().lower() == "true",
        discrepancy_fields=campos_del_resumen(resumen_discrepancia),
        discrepancy_reason=normalizar_razon(resumen_discrepancia),
        revision_pendiente=revision_pendiente,
    )


def leer_indice_paginas(path: Path | str) -> List[dict]:
    """Lee el indice de la entrega que escribe la ejecución.

    Devuelve una entrada por archivo de entrega, ``{"pdf", "paginas"}``, con
    las paginas en el orden en que estan dentro de ese archivo. Cada archivo
    es un batch distinto en AirVault, asi que el reparto importa tanto como
    el orden.

    Sin indice devuelve una lista vacia y quien llame decide si puede seguir
    sin el.
    """
    ruta = Path(path)
    if not ruta.is_file():
        return []
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(datos, Mapping):
        return []
    if datos.get("partes") is not None:
        crudas = datos.get("partes") or []
    else:
        # Version 1: un unico archivo, con las paginas en la raiz.
        crudas = [{"pdf": datos.get("pdf", ""),
                   "paginas": datos.get("paginas") or []}]
    partes: List[dict] = []
    for parte in crudas:
        if not isinstance(parte, Mapping):
            continue
        paginas = [
            p for p in (parte.get("paginas") or []) if isinstance(p, Mapping)
        ]
        if paginas:
            partes.append({"pdf": str(parte.get("pdf", "")),
                           "revisar": bool(parte.get("revisar", False)),
                           "paginas": paginas})
    return partes


def _sin_sufijo_de_copia(nombre: str) -> str:
    """El nombre original de un PDF que se aparto con el nombre numerado.

    Al terminar una ejecucion sus PDF se guardan en ``input/processed``, y
    si alli ya habia uno igual el nuevo se numera. El reporte pasa a
    apuntar al archivo numerado y el CSV conserva el nombre con el que se
    leyo, asi que un indice escrito con el numerado no encuentra ni una
    sola fila y el batch entero sale sin matricula, sin log y sin fecha.
    Los indices nuevos ya guardan el nombre del CSV; esto rescata los que
    quedaron escritos antes, sin volver a exportar la entrega.
    """
    ruta = Path(nombre)
    match = _SUFIJO_DE_COPIA_RE.match(ruta.stem)
    return f"{match.group('base')}{ruta.suffix}" if match else ruta.name


def registros_desde_entrega(
    filas: Iterable[Mapping[str, str]],
    indice: Sequence[Mapping[str, object]],
    resolutor: ResolutorFlota | None = None,
    fin_de_mes: bool = False,
) -> List[Registro]:
    """Construye los registros siguiendo el PDF que se sube, no el CSV.

    El batch de AirVault tiene una pagina por cada pagina del PDF, y el PDF
    lleva separadores que el CSV no tiene. Recorrer el indice en vez del
    CSV es lo que mantiene ``seq`` igual a la pagina del batch: los
    separadores ocupan su sitio y quedan marcados para que nadie les
    escriba nada.
    """
    resolutor = resolutor or ResolutorFlota()
    filas = list(filas)
    # Las fechas se deducen con el CSV entero, no con las paginas de esta
    # parte: una ejecucion repartida en varios batches sigue siendo un solo
    # juego de libros, y las anclas de un libro pueden haber caido en otra
    # parte.
    inferidas = fechas_inferidas(filas)
    por_clave: Dict[tuple[str, int], Mapping[str, str]] = {}
    for fila in filas:
        archivo = str(fila.get("file", "")).strip()
        try:
            pagina = int(str(fila.get("page", "")).strip())
        except ValueError:
            continue
        por_clave[(archivo, pagina)] = fila

    # Cuando la ejecucion salio de un solo PDF no hay ambiguedad posible: la
    # pagina 7 del indice es la pagina 7 del CSV, se llame como se llame el
    # archivo. Es el rescate que vale para cualquier renombrado, sin tener
    # que acertar con la regla con la que se renombro.
    nombres = {archivo for archivo, _pagina in por_clave}
    por_pagina: Dict[int, Mapping[str, str]] = (
        {pagina: fila for (_archivo, pagina), fila in por_clave.items()}
        if len(nombres) == 1 else {}
    )

    registros: List[Registro] = []
    for seq, entrada in enumerate(indice, start=1):
        etiqueta = str(entrada.get("separador", "") or "").strip()
        if etiqueta:
            registros.append(Registro(seq=seq, separador=etiqueta))
            continue
        archivo = str(entrada.get("archivo", "") or "").strip()
        try:
            pagina = int(str(entrada.get("pagina", 0)))
        except (TypeError, ValueError):
            pagina = 0
        fila = por_clave.get((archivo, pagina))
        if fila is None:
            fila = por_clave.get((_sin_sufijo_de_copia(archivo), pagina))
        if fila is None:
            fila = por_pagina.get(pagina)
        if fila is None:
            registros.append(Registro(
                seq=seq, archivo_origen=archivo, pagina_origen=pagina,
                avisos=[
                    f"[sin_fila] la pagina {pagina} de {archivo} esta en el "
                    f"PDF pero no en el CSV"
                ],
            ))
            continue
        registros.append(
            _registro_de_fila(
                seq, fila, resolutor, inferidas, fin_de_mes,
                fecha_dudosa=bool(entrada.get("fecha_dudosa", False)),
                revision_pendiente=(
                    entrada["revision_pendiente"]
                    if isinstance(entrada.get("revision_pendiente"), bool)
                    else None
                ),
            )
        )
        campos = entrada.get("discrepancy_fields")
        if isinstance(campos, list):
            registros[-1].discrepancy_fields = [c for c in campos if isinstance(c, str)]
        razon = entrada.get("disc_reason")
        if isinstance(razon, str) and razon.strip():
            from app.airvault.ecn import normalizar_razon
            registros[-1].discrepancy_reason = normalizar_razon(razon)
    return registros


def valores_de_indice(
    registro: Registro,
    doc_type: str,
    audit_status: str,
    nombre_batch: str = "",
    audit_status_discrepancia: str = "",
) -> Dict[int, str]:
    """Diccionario ``{fieldId: valor}`` que se manda al guardar la pagina.

    Solo se incluyen los campos que el sistema controla. Los demas se dejan
    fuera a proposito: lo que no se manda, AirVault lo conserva tal como
    estaba, asi que un indexado no pisa datos que alguien haya puesto a
    mano.

    Una bitacora marcada como discrepancia (la columna ``disc`` del CSV, la
    que va bajo el separador «POSIBLES DISCREPANCIAS») lleva su propio
    Audit Status cuando se da uno: es lo unico que la distingue en AirVault
    de las demas paginas del batch.
    """
    if registro.discrepancia and audit_status_discrepancia:
        audit_status = audit_status_discrepancia
    valores: Dict[int, str] = {
        CAMPO_DOC_TYPE: doc_type,
        CAMPO_MATRICULA: registro.matricula,
        CAMPO_FLEET: registro.fleet,
        CAMPO_LOG_NUMBER: registro.log_number,
        CAMPO_AUDIT_STATUS: audit_status,
    }
    if not registro.fecha_dudosa:
        valores[CAMPO_END_DATE] = fecha_airvault(registro.fecha)
    if registro.lessor:
        valores[CAMPO_LESSOR] = registro.lessor
    if registro.flight_number:
        # El vuelo de la bitacora, en Description. Solo cuando la lectura
        # lo trajo: mandarlo vacio borraria lo que alguien haya escrito a
        # mano, y este campo no es de los que el sistema controla siempre.
        valores[CAMPO_DESCRIPCION] = registro.flight_number
    if nombre_batch:
        valores[CAMPO_BATCH_NAME] = nombre_batch
    if registro.discrepancia:
        from app.airvault.ecn import CAMPOS_ECN, razones_ecn
        valores.update(zip(
            CAMPOS_ECN,
            razones_ecn(registro.discrepancy_fields, registro.discrepancy_reason),
        ))
    return valores
