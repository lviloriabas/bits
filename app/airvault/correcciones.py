"""Que hay que corregir de cada excepcion de Log Page Audit, y como.

El reporte dice lo que esta mal pero no lo arregla, y arreglarlo a mano son
dos pantallas por caso: buscar la bitacora, mirar sus apariciones y borrar
las que sobran, o abrir la mal indexada y cambiarle el avion. Este modulo
parte ese trabajo en dos mitades que conviene no mezclar.

**El plan** (:func:`planificar`) sale entero del propio reporte y no toca
nada. Una mal indexada ya trae las dos matriculas: la del libro al que
pertenece la bitacora y aquella bajo la que quedo archivada, asi que no hay
nada que adivinar. Una duplicada trae cuantas copias hay, y de ahi salen
cuantas sobran. Lo que el reporte no diga con esas palabras no se deduce:
queda como caso a revisar, con el motivo escrito.

**La aplicacion** (:class:`CorrectorLogPageAudit`) conduce el cliente de
AirVault por el mismo camino que una persona, con el navegador del perfil
portable, igual que :mod:`app.airvault.web_reports` conduce el visor de
SSRS. Va por la pantalla y no por peticiones sueltas a proposito: por la
pantalla valen los permisos de la cuenta y las validaciones del
repositorio, asi que una cuenta sin permiso para borrar no encuentra el
boton y el caso se queda como estaba, en vez de mandar media operacion por
una ruta que nadie documento.

Tres reglas gobiernan la aplicacion, y las tres estan para lo mismo: que un
caso que no se entiende no se toque.

* **Se comprueba antes de escribir.** Cada caso se contrasta con lo que la
  pantalla ensena ahora: que la duplicada siga teniendo las copias que
  decia el reporte y que la mal indexada siga estando donde decia. El
  reporte se genero en su momento; si desde entonces alguien lo corrigio,
  actuar sobre el plan viejo borraria lo que ya estaba bien.
* **No se elige a ciegas cual sobra.** De un grupo de copias se conserva la
  mas antigua, y para eso hace falta una columna de fecha que se pueda
  leer. Sin ella no se sabe cual es la primera, y entonces no se borra
  ninguna.
* **Un control que no aparece detiene ese caso, no la corrida.** Se busca
  por lo que el control dice, no por identificadores copiados de una
  instalacion. Si no esta, ese caso se informa sin tocarlo y se sigue con
  el siguiente.
"""

from __future__ import annotations

import base64
import json
import re
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Iterable, Mapping, Sequence

from loguru import logger

from app.airvault.config import AirVaultConfig
from app.airvault.mapping import FLOTA_CACHE_FILENAME, ResolutorFlota
from app.airvault.navegador import (
    PERFIL_POR_DEFECTO,
    ErrorDeNavegador,
    SesionDeNavegador,
    registrar_pestana,
    cerrar_pestana,
    cookies_de,
    _CANDADO_PESTANAS,
    _edges_del_perfil,
    _puerto_anotado,
    _version_en,
    _WebSocket,
)
from app.airvault.web_reports import (
    TIPO_DUPLICADA,
    TIPO_MAL_INDEXADA,
    ConsultaCancelada,
    ErrorDePaginaAirVault,
    ExcepcionLogPageAudit,
    entrada_federada,
    esperar_acceso,
    _Pagina,
)
from app.core import parallelism
from app.utils.portable import app_root

ACCION_BORRAR = "Borrar copias"
ACCION_REINDEXAR = "Reindexar"
ACCION_REVISAR = "Revisar a mano"

# Presupuesto prudente por pestaña, incluida la carga de imágenes. No es una
# medida del consumo de AirVault: se vuelve a consultar la RAM al repartir.
_MEMORIA_PESTANA_MB = 768
_CANDADO_AUDITORIA = Lock()


def _paralelismo_correcciones(en_curso: int = 0) -> int:
    """Casos que caben ahora, contando los que ya ocupan memoria."""
    libre = parallelism.available_memory_mb()
    if libre <= 0:
        return 1
    adicionales = (
        max(0, libre - parallelism.reserved_memory_mb()) // _MEMORIA_PESTANA_MB
    )
    # Las tareas esperan al servidor en sockets independientes. No ocupan
    # un núcleo continuamente, por eso el reparto lo limita la RAM.
    return max(1, en_curso + adicionales)

# Lo que dice el boton que cierra cada cuadro de AirVault. Se busca por
# su texto porque los cuadros de jQuery UI no le ponen identificador a
# sus botones; se acepta lo que digan en los dos idiomas en los que puede
# estar instalado el cliente, y se compara en minusculas.
TEXTOS_BORRAR = (
    "delete page", "delete pages", "delete", "borrar", "eliminar",
)
TEXTOS_GUARDAR = ("save", "guardar")
TEXTOS_SEGUIR = ("continue to save", "continue", "continuar")

_FORMATOS_FECHA = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


class ControlNoEncontrado(RuntimeError):
    """La pantalla no traia el control que hacia falta para este caso."""


def mensaje_airvault(aviso: object, accion: str) -> str:
    """Explica rechazos conocidos sin atribuir causas a avisos desconocidos."""
    texto = " ".join(str(aviso or "").split())
    normalizado = "".join(
        letra for letra in unicodedata.normalize("NFKD", texto.casefold())
        if not unicodedata.combining(letra)
    )
    if re.search(
        r"\b(?:is (?:currently |already )?locked|has been locked|page locked|"
        r"document locked|locked by|esta bloquead[ao]|bloquead[ao] por)\b",
        normalizado,
    ):
        por_otro = any(marca in normalizado for marca in (
            "another user", "other user", "otro usuario",
        ))
        quien = " por otro usuario" if por_otro else ""
        return (
            f"La bitácora está bloqueada{quien} en AirVault. No se puede "
            f"{accion} mientras siga bloqueada. Espere a que se libere o "
            "solicite al responsable que la desbloquee."
        )
    if any(marca in normalizado for marca in (
        "access denied", "permission denied", "not authorized", "not authorised",
        "do not have permission", "does not have permission", "no permission",
        "not have sufficient permission", "insufficient permissions",
        "insufficient privileges", "no tiene permiso", "sin permisos",
        "acceso denegado",
    )):
        return (
            f"Su cuenta no tiene permiso para {accion} en AirVault. "
            "Solicite el permiso al administrador de AirVault."
        )
    if any(marca in normalizado for marca in (
        "session expired", "session has expired", "login timeout",
        "sesion caduc", "sesion ha caduc", "sesion expir",
    )):
        return (
            "La sesión de AirVault caducó. Vuelva a iniciar sesión y "
            "consulte el reporte antes de continuar la corrección."
        )
    if re.search(r"(?:document|page|bitacora).*(?:not found|no longer exists|has been deleted|no existe|ya no existe|no se encontr)", normalizado):
        return (
            "La bitácora ya no está disponible en AirVault. Vuelva a "
            "buscarla en Web Search y consulte el reporte antes de corregirla."
        )
    return (
        f"AirVault no permitió {accion}. Revise la bitácora en Web Search "
        f"antes de intentarlo de nuevo. Mensaje de AirVault: {texto}"
    )


def mensaje_error_correccion(error: Exception) -> str:
    """Conserva los motivos del proceso y evita mostrar errores internos."""
    if isinstance(error, ControlNoEncontrado):
        return str(error)
    if isinstance(error, ErrorDePaginaAirVault):
        return (
            f"{error}. Vuelva a abrir AirVault y compruebe la bitácora "
            "en Web Search antes de reintentar la corrección."
        )
    if isinstance(error, (TimeoutError, ConnectionError, ErrorDeNavegador)):
        return (
            "No se pudo abrir o mantener la comunicación con Edge para "
            "completar la corrección. Vuelva a abrir AirVault y "
            "compruebe la bitácora en Web Search antes de reintentar."
        )
    return (
        "Un error inesperado impidió completar la corrección. Compruebe en "
        "Web Search si se eliminaron copias o cambió la matrícula, y vuelva "
        "a consultar el reporte antes de reintentar. El detalle técnico "
        "quedó en el registro de errores de BITS."
    )


class _NavegadorDeCorrecciones:
    """Abre pestañas temporales sin tocar las que abrió la persona.

    El corrector comparte el perfil de AirVault para usar la sesión iniciada.
    Reutiliza el Edge del perfil, con o sin ventana, y protege cada pestaña
    temporal antes de conectar su controlador. Si el perfil está libre, abre
    Edge sin ventana. Cada caso cierra su pestaña al terminar o cancelarse.
    """

    def __init__(self, perfil: Path) -> None:
        self.perfil = Path(perfil)
        self._sesion: SesionDeNavegador | None = None

    def __enter__(self) -> "_NavegadorDeCorrecciones":
        return self

    def __exit__(self, *_exc) -> None:
        if self._sesion is not None:
            self._sesion.cerrar()

    def abrir(self, url: str, espera_s: float) -> dict:
        version = self._edge_del_perfil()
        if version is not None:
            return version
        self._sesion = SesionDeNavegador(self.perfil, visible=False)
        return self._sesion.abrir(url, espera_s=espera_s)

    def abrir_pestana(self, url: str, version: dict) -> str:
        # Crear directamente conserva las pestañas de otros casos también
        # fuera de la GUI, donde abrir_pestana de la sesión limpia las demás.
        ws = _WebSocket(version["webSocketDebuggerUrl"])
        try:
            with _CANDADO_PESTANAS:
                creada = ws.pedir(
                    "Target.createTarget", url=url, background=True
                )
                target_id = str(creada.get("targetId", ""))
                if target_id:
                    registrar_pestana(version, target_id)
        finally:
            ws.cerrar()
        target_id = str(creada.get("targetId", ""))
        if not target_id:
            raise ErrorDeNavegador(
                "Edge no abrió la pestaña temporal de corrección"
            )
        return target_id

    @staticmethod
    def cerrar_pestana(version: dict, target_id: str) -> None:
        """Retira una pestaña cuyo controlador no llegó a abrirse."""
        cerrar_pestana(version, target_id, crear_socket=_WebSocket)

    def cookies(self, version: dict) -> dict:
        """Las cookies del perfil, sea propio el navegador o prestado."""
        return cookies_de(version)

    def _edge_del_perfil(self) -> dict | None:
        anotado = _puerto_anotado(self.perfil)
        if anotado is not None:
            version = _version_en(anotado)
            if version is not None:
                return version
        for _pid, puerto in _edges_del_perfil(self.perfil):
            if puerto is None or puerto == anotado:
                continue
            version = _version_en(puerto)
            if version is not None:
                return version
        return None


@dataclass(frozen=True)
class Correccion:
    """Lo que hay que hacerle a una excepcion, ya decidido."""

    accion: str
    excepcion: ExcepcionLogPageAudit
    sobran: int = 0
    matricula_actual: str = ""
    matricula_correcta: str = ""
    motivo: str = ""

    @property
    def log_number(self) -> str:
        return self.excepcion.log_number

    @property
    def url_busqueda(self) -> str:
        return self.excepcion.url_busqueda

    @property
    def descripcion(self) -> str:
        """Una linea que dice lo que va a pasar, en el idioma de la ventana."""
        if self.accion == ACCION_BORRAR:
            # El caso corriente es de dos copias y una que sobra, y en
            # plural quedaba «borrar las 1 restantes».
            sobrantes = (
                "borrar la que sobra"
                if self.sobran == 1
                else f"borrar las {self.sobran} que sobran"
            )
            return (
                f"Bitácora {self.log_number}: conservar la más antigua de "
                f"{self.sobran + 1} copias y {sobrantes}."
            )
        if self.accion == ACCION_REINDEXAR:
            return (
                f"Bitácora {self.log_number}: pasarla de "
                f"{self.matricula_actual} a {self.matricula_correcta}."
            )
        return f"Bitácora {self.log_number}: {self.motivo}"


@dataclass
class Resultado:
    """Como quedo un caso despues de intentarlo."""

    correccion: Correccion
    hecho: bool = False
    detalle: str = ""

    @property
    def log_number(self) -> str:
        return self.correccion.log_number


def planificar(
    excepciones: Iterable[ExcepcionLogPageAudit],
) -> list[Correccion]:
    """Que hacer con cada excepcion, sin consultar nada ni tocar nada.

    Las duplicadas van primero porque quitan documentos: si una bitacora
    esta ademas mal indexada, puede que la copia que sobra sea justo la mal
    archivada y que al borrarla se arreglen las dos. Por eso la mal indexada
    de una bitacora que tambien esta repetida no se toca en esta pasada y se
    deja dicho que hay que volver a consultar el reporte.
    """
    excepciones = list(excepciones)
    duplicadas = [
        excepcion
        for excepcion in excepciones
        if excepcion.tipo == TIPO_DUPLICADA
    ]
    mal_indexadas = [
        excepcion
        for excepcion in excepciones
        if excepcion.tipo == TIPO_MAL_INDEXADA
    ]
    repetidas = {
        excepcion.log_number
        for excepcion in duplicadas
        if (excepcion.copias or 0) >= 2
    }

    plan: list[Correccion] = []
    for excepcion in duplicadas:
        copias = excepcion.copias or 0
        if copias < 2:
            plan.append(
                Correccion(
                    ACCION_REVISAR,
                    excepcion,
                    motivo=(
                        "el reporte no dice cuántas copias hay, así que no "
                        "se sabe cuántas sobran"
                    ),
                )
            )
            continue
        plan.append(Correccion(ACCION_BORRAR, excepcion, sobran=copias - 1))

    for excepcion in mal_indexadas:
        actual = (excepcion.destino or "").strip()
        correcta = (excepcion.matricula_libro or "").strip()
        if not actual or not correcta:
            motivo = (
                "el reporte no dice de qué matrícula a cuál hay que moverla"
            )
        elif actual.casefold() == correcta.casefold():
            motivo = "el reporte la señala en la matrícula en la que ya está"
        elif excepcion.log_number in repetidas:
            motivo = (
                "esa bitácora además está repetida: primero se quitan las "
                "copias que sobran y después se vuelve a consultar"
            )
        else:
            plan.append(
                Correccion(
                    ACCION_REINDEXAR,
                    excepcion,
                    matricula_actual=actual,
                    matricula_correcta=correcta,
                )
            )
            continue
        plan.append(Correccion(ACCION_REVISAR, excepcion, motivo=motivo))

    return plan


def resumen_del_plan(plan: Sequence[Correccion]) -> str:
    """Lo que se le enseña a quien tiene que autorizar la corrección."""
    borrar = [c for c in plan if c.accion == ACCION_BORRAR]
    reindexar = [c for c in plan if c.accion == ACCION_REINDEXAR]
    revisar = [c for c in plan if c.accion == ACCION_REVISAR]
    copias = sum(c.sobran for c in borrar)

    partes: list[str] = []
    if borrar:
        copia = "copia sobrante" if copias == 1 else "copias sobrantes"
        bitacora = "bitácora" if len(borrar) == 1 else "bitácoras"
        partes.append(
            f"borrar {copias} {copia} de {len(borrar)} {bitacora}"
        )
    if reindexar:
        cantidad = len(reindexar)
        pagina = "página mal indexada" if cantidad == 1 else (
            "páginas mal indexadas"
        )
        partes.append(f"reindexar {cantidad} {pagina}")
    if not partes:
        texto = "No hay nada que corregir automáticamente."
    else:
        texto = "Se va a " + " y ".join(partes) + "."
    if revisar:
        if len(revisar) == 1:
            texto += " Queda 1 caso para revisar a mano; no se toca."
        else:
            texto += (
                f" Quedan {len(revisar)} casos para revisar a mano; no se "
                "tocan."
            )
    return texto


# ── leer la pantalla ────────────────────────────────────────────────

# Cuando la busqueda ya se puede leer. Web Search monta la rejilla con
# jqGrid despues de que conteste el servidor, asi que la pagina existe mucho
# antes que los resultados: mirarla en cuanto hubiera filas en el documento
# devolvia las de la maquetacion y las del pie. Lo que marca el final es el
# pie de la rejilla, que solo se escribe con la consulta ya contestada, y lo
# escribe igual con resultados («View 1 - 2 of 2») que sin ellos («No
# records to view»). Las dos cosas son un final; leerla antes, no.
_REJILLA_CARGADA = (
    "document.querySelector('.ui-jqgrid-btable') && "
    "document.querySelector('[id$=Pager_right]') && "
    "document.querySelector('[id$=Pager_right]').innerText.trim()"
)

# Lo mismo, despues de pedirle a la pagina que se recargue: la marca que se
# deja antes de recargar se va con el documento viejo, asi que mientras siga
# ahi lo que se esta mirando es la rejilla de antes.
_REJILLA_RECARGADA = (
    "typeof window.__bits_recarga === 'undefined' && " + _REJILLA_CARGADA
)

# La rejilla de resultados, leida por el nombre de cada campo y no por la
# posicion de cada celda. AirVault monta sus rejillas con jqGrid y sirve la
# fila entera con «jQuery.getResultRowData», que devuelve el registro con
# sus nombres: «DocKey» (lo que identifica al documento), «DocID», «LogNo»,
# «ACREG» y «LastFileDate». Por las celdas habia que adivinar cual columna
# era cual, y el orden de las columnas lo decide quien monto la busqueda en
# AirVault.
#
# El id del «<tr>» no identifica nada: es el numero de orden dentro de la
# pagina, y dos consultas seguidas de la misma bitacora devuelven las mismas
# copias en distinto orden.
_LEER_REJILLA = r"""(function(){
  var salida = [];
  var filas = document.querySelectorAll('.ui-jqgrid-btable tr.jqgrow');
  Array.prototype.forEach.call(filas, function(fila){
    var datos;
    try { datos = jQuery.getResultRowData(fila.id) || {}; }
    catch (e) { return; }
    salida.push({
      fila: String(fila.id || ''),
      clave: String(datos.DocKey || ''),
      documento: String(datos.DocID || ''),
      log: String(datos.LogNo || '').trim(),
      matricula: String(datos.ACREG || '').trim(),
      cuando: String(datos.LastFileDate || '').trim(),
      imagenes: datos.ImageCount ?? null,
      tipo: String(datos.DocType || '').trim()
    });
  });
  return salida;
})()"""

# Solo se consulta en la discrepancia que puede dejar una limpieza previa.
# Una fila visible no demuestra que sea la unica si hay paginas sin leer.
_REJILLA_CON_UNA_FILA = r"""(function(){
  var tablas = document.querySelectorAll('.ui-jqgrid-btable');
  if (tablas.length !== 1) return false;
  var tabla = tablas[0];
  try {
    var total = jQuery(tabla).jqGrid('getGridParam', 'records');
    var visibles = jQuery(tabla).jqGrid('getGridParam', 'reccount');
    if (!/^\d+$/.test(String(total)) || !/^\d+$/.test(String(visibles))) {
      return false;
    }
    var filas = tabla.querySelectorAll('tr.jqgrow').length;
    return Number(total) === 1 && Number(visibles) === 1 && filas === 1
      && !(tabla.grid && tabla.grid.hDiv && tabla.grid.hDiv.loading);
  } catch (e) { return false; }
})()"""

# Se selecciona la fila y se llama a la misma funcion que AirVault cuelga de
# su menu contextual («Delete Page(s)», «Reindex Page(s)»). Se conduce por
# ahi y no por el menu porque el menu lo abre el boton derecho del raton y
# lo que hace al soltarlo es justo esta llamada; ademas el menu no lleva
# identificadores estables y el cliente lo comparte con el visor.
_ABRIR_CUADRO = r"""(function(clave, operacion){
  var fila = null;
  var filas = document.querySelectorAll('.ui-jqgrid-btable tr.jqgrow');
  Array.prototype.forEach.call(filas, function(otra){
    if (fila) return;
    var datos;
    try { datos = jQuery.getResultRowData(otra.id) || {}; }
    catch (e) { return; }
    if (String(datos.DocKey || '') === clave) fila = otra;
  });
  if (!fila) return 'la copia elegida ya no aparece en Web Search; vuelva a consultar el reporte';
  if (operacion === 'onDeletePage') {
    var comprobada = jQuery.getResultRowData(fila.id) || {};
    if (String(comprobada.ImageCount) !== '1' || String(comprobada.DocType || '').trim().toUpperCase() !== 'LOG PAGE') {
      return 'no se elimina: no se pudo confirmar que la copia sea una bitácora de una sola imagen';
    }
  }
  var accion = window[operacion];
  if (typeof accion !== 'function') {
    return 'AirVault no ofrece la opción de ' +
      (operacion === 'onDeletePage' ? 'eliminar copias' : 'reindexar bitácoras') +
      ' en esta búsqueda; revise los permisos de su cuenta con el administrador de AirVault';
  }
  try {
    jQuery(fila).closest('table.ui-jqgrid-btable')
      .jqGrid('setSelection', fila.id);
  } catch (e) { }
  accion(fila);
  return 'OK';
})(%s, %s)"""

# Lo que AirVault contesta cuando no deja hacer la operacion: un cuadro de
# aviso con el motivo escrito («The specified document page is locked by
# another user»). Los rechazos conocidos se explican en español; un aviso
# desconocido conserva su texto para no atribuirle un motivo inventado.
_MENSAJE = r"""(function(){
  var aviso = document.getElementById('messageDialog');
  if (!aviso || aviso.offsetParent === null) return '';
  return (aviso.innerText || '').replace(/\s+/g, ' ').trim();
})()"""

_CONFIRMAR_BORRADO = r"""(function(textos){
  var cuadro = document.getElementById('deletePageDialog');
  if (!cuadro) return 'la ventana de eliminación se cerró antes de confirmar';
  var todas = document.getElementById('rdAllPages');
  if (!todas) return 'la ventana de eliminación no permite seleccionar la copia completa';
  if (!todas.checked) todas.click();
  if (!todas.checked) return 'AirVault no permitió seleccionar la copia completa';
  var boton = null;
  jQuery(cuadro).closest('.ui-dialog')
    .find('.ui-dialog-buttonpane button').each(function(){
      if (boton) return;
      var texto = (this.innerText || '').replace(/\s+/g, ' ')
        .trim().toLowerCase();
      if (textos.indexOf(texto) >= 0 && this.offsetParent !== null) {
        boton = this;
      }
    });
  if (!boton) return 'la ventana de eliminación no muestra el botón para eliminar la copia';
  boton.click();
  return 'OK';
})(%s)"""

# El cuadro de reindexado esta listo cuando trae el campo del avion, que
# es el ultimo que llega y el unico que aqui se reescribe.
_CAMPO_DEL_AVION = (
    "document.querySelector('#reindexDialog [data-name=C_ACREG]')"
)

_CONFIRMAR_REINDEXADO = r"""(function(matricula, flota, textos){
  var cuadro = document.getElementById('reindexDialog');
  if (!cuadro) return 'la ventana de reindexación se cerró antes de guardar';
  var todas = document.getElementById('rdAllPages');
  if (todas && !todas.checked) todas.click();
  function elegir(nombre, valor){
    var etiqueta = nombre === 'C_ACREG' ? 'matrícula' : 'flota';
    var campo = cuadro.querySelector('[data-name="' + nombre + '"]');
    if (!campo) return 'la ventana de reindexación no muestra el campo de ' + etiqueta;
    var hay = false;
    Array.prototype.forEach.call(campo.options || [], function(opcion){
      if (opcion.value === valor) hay = true;
    });
    if (!hay) return 'AirVault no ofrece ' + valor + ' entre las opciones de ' + etiqueta + '; revise la configuración con el administrador de AirVault';
    if (campo.value !== valor) {
      campo.value = valor;
      jQuery(campo).trigger('change');
    }
    return '';
  }
  var fallo = elegir('C_ACREG', matricula);
  if (fallo) return fallo;
  if (flota) {
    fallo = elegir('C_Fleet', flota);
    if (fallo) return fallo;
  }
  var boton = null;
  jQuery(cuadro).closest('.ui-dialog')
    .find('.ui-dialog-buttonpane button').each(function(){
      if (boton) return;
      var texto = (this.innerText || '').replace(/\s+/g, ' ')
        .trim().toLowerCase();
      if (textos.indexOf(texto) >= 0 && this.offsetParent !== null) {
        boton = this;
      }
    });
  if (!boton) return 'la ventana de reindexación no muestra el botón Guardar';
  boton.click();
  return 'OK';
})(%s, %s, %s)"""

# Cerrar el cuadro sin hacer nada. Abrirlo toma el documento, y un caso
# que se corta a la mitad (un campo que no llega, una matricula que la
# instalacion no ofrece) lo dejaria tomado para todo el mundo hasta que
# alguien lo suelte a mano.
_CANCELAR_CUADRO = r"""(function(cuadro){
  var caja = document.getElementById(cuadro);
  if (!caja) return 'OK';
  var marco = jQuery(caja).closest('.ui-dialog');
  var boton = null;
  marco.find('.ui-dialog-buttonpane button').each(function(){
    if (boton) return;
    var texto = (this.innerText || '').replace(/\s+/g, ' ')
      .trim().toLowerCase();
    if (texto === 'cancel' || texto === 'cancelar') boton = this;
  });
  if (!boton) boton = marco.find('.ui-dialog-titlebar-close')[0];
  if (!boton) return 'sin botón';
  boton.click();
  return 'OK';
})(%s)"""

# Guardar un reindexado puede pararse a medio camino para que alguien lo
# confirme («Continue to save»). El cuadro sigue abierto y sin pulsar eso no
# se escribe nada, asi que se contesta y se sigue esperando.
_SEGUIR_GUARDANDO = r"""(function(textos){
  var boton = null;
  jQuery('.ui-dialog:visible .ui-dialog-buttonpane button').each(function(){
    if (boton) return;
    var texto = (this.innerText || '').replace(/\s+/g, ' ')
      .trim().toLowerCase();
    if (textos.indexOf(texto) >= 0 && this.offsetParent !== null) {
      boton = this;
    }
  });
  if (!boton) return '';
  boton.click();
  return 'OK';
})(%s)"""


def _cuadro_abierto(identificador: str) -> str:
    return (
        f"document.getElementById({json.dumps(identificador)})"
        " || document.getElementById('messageDialog')"
    )


def _cuadro_cerrado(identificador: str) -> str:
    return (
        f"!document.getElementById({json.dumps(identificador)})"
        f" || document.getElementById({json.dumps(identificador)})"
        ".offsetParent === null"
    )


def _fecha_de(texto: str) -> datetime | None:
    """La fecha de una celda, si esa celda trae una que se pueda leer."""
    limpio = " ".join(str(texto or "").split())
    if not limpio:
        return None
    for formato in _FORMATOS_FECHA:
        try:
            return datetime.strptime(limpio, formato)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Copia:
    """Una aparicion de la bitacora en la rejilla de resultados."""

    clave: str
    documento: str
    fila: str
    cuando: datetime | None
    matricula: str
    imagenes: int | None = None
    tipo: str = ""


def _cantidad_imagenes(valor: object) -> int | None:
    """Un recuento ausente o ambiguo nunca autoriza eliminar un documento."""
    texto = str(valor).strip()
    return int(texto) if texto.isascii() and texto.isdigit() else None


def copias_en(
    rejilla: Sequence[Mapping[str, object]], log_number: str
) -> list[Copia]:
    """Las filas de la rejilla que son ese numero de bitacora."""
    numero = str(log_number).strip()
    if not numero:
        return []
    copias: list[Copia] = []
    for fila in rejilla:
        if str(fila.get("log", "") or "").strip() != numero:
            continue
        copias.append(
            Copia(
                clave=str(fila.get("clave", "") or ""),
                documento=str(fila.get("documento", "") or ""),
                fila=str(fila.get("fila", "") or ""),
                cuando=_fecha_de(str(fila.get("cuando", "") or "")),
                matricula=str(fila.get("matricula", "") or "").strip(),
                imagenes=_cantidad_imagenes(fila.get("imagenes")),
                tipo=str(fila.get("tipo", "") or "").strip(),
            )
        )
    return copias


def por_antiguedad(
    copias: Sequence[Copia],
) -> tuple[Copia | None, list[Copia]]:
    """La copia que se queda y las que sobran, de mas antigua a mas nueva.

    Si alguna no trae fecha legible no se devuelve ninguna. Sin fecha no se
    sabe cual es la primera, y borrar «las demas» sin saber cual se queda es
    exactamente lo que no puede hacer sola una corrección automática.
    """
    if len(copias) < 2 or any(copia.cuando is None for copia in copias):
        return None, []
    ordenadas = sorted(
        copias, key=lambda copia: (copia.cuando, copia.documento)
    )
    return ordenadas[0], list(ordenadas[1:])


class CorrectorLogPageAudit:
    """Aplica el plan en el cliente de AirVault, caso por caso."""

    def __init__(
        self,
        config: AirVaultConfig,
        flota: ResolutorFlota | None = None,
    ) -> None:
        self.config = config
        self.revisar = None
        self.revision_activa = None
        self.cache_previa: dict[tuple, bytes] = {}
        self._candado_previa = Lock()
        self.auditoria = app_root() / "output" / "airvault" / "correcciones_auditoria.jsonl"
        # La misma tabla de matricula a flota con la que se indexa. Mover una
        # pagina de un avion a otro puede cambiarle la flota (las HP-99 son
        # MAX y las HP-15 NG), y dejar la de antes seria cambiar un dato malo
        # por otro.
        self.flota = flota or ResolutorFlota.load(
            app_root() / FLOTA_CACHE_FILENAME
        )

    def aplicar(
        self,
        plan: Sequence[Correccion],
        avisar: Callable[[str], None] | None = None,
        cancelar: Callable[[], bool] | None = None,
        ensayo: bool = True,
        progreso: Callable[[int, int], None] | None = None,
    ) -> list[Resultado]:
        """Recorre el plan. Con ``ensayo`` comprueba pero no escribe nada.

        El ensayo no es un modo de prueba: es la primera mitad de cada caso
        y se hace igual en los dos. Abre la busqueda de esa bitacora, lee la
        rejilla y contrasta lo que hay ahora con lo que decia el reporte. Lo
        unico que cambia es si despues se toca algo.

        ``avisar`` es la frase de estado y ``progreso`` la cuenta: cuantas
        bitacoras van de cuantas. La cuenta la usa el cronometro de la
        ventana, que necesita numeros y no texto, y por eso no viaja el plan
        entero sino lo que de verdad se abre en Edge: las filas de revision
        manual salen resueltas sin tocar el navegador. El primer aviso llega
        con cero hechas, en cuanto la sesion esta en pie.
        """
        notificar = avisar or (lambda _texto: None)
        esta_cancelado = cancelar or (lambda: False)
        avanzar = progreso or (lambda _hechos, _total: None)
        pendientes = [
            correccion
            for correccion in plan
            if correccion.accion in (ACCION_BORRAR, ACCION_REINDEXAR)
        ]
        resultados: list[Resultado] = [
            Resultado(
                correccion,
                detalle=f"Requiere revisión manual: {correccion.motivo}.",
            )
            for correccion in plan
            if correccion.accion == ACCION_REVISAR
        ]
        if not pendientes:
            return resultados

        perfil = (
            Path(self.config.perfil_navegador)
            if self.config.perfil_navegador
            else PERFIL_POR_DEFECTO
        )
        notificar("Abriendo AirVault en Edge")
        with _NavegadorDeCorrecciones(perfil) as navegador:
            # Por el enlace federado, no por la raiz: es el que rehace
            # la sesion sin que nadie teclee nada.
            version = navegador.abrir(
                entrada_federada(self.config),
                espera_s=self.config.espera_login_s,
            )
            esperar_acceso(lambda: navegador.cookies(version), self.config)
            avanzar(0, len(pendientes))
            notificar(
                "Revisando bitácoras en AirVault" if ensayo
                else "Corrigiendo bitácoras en AirVault"
            )
            resultados.extend(self._aplicar_pendientes(
                navegador, version, pendientes, notificar,
                esta_cancelado, ensayo, avanzar,
            ))
        return resultados

    def _aplicar_pendientes(
        self,
        navegador: _NavegadorDeCorrecciones,
        version: dict,
        pendientes: Sequence[Correccion],
        notificar: Callable[[str], None],
        esta_cancelado: Callable[[], bool],
        ensayo: bool,
        avanzar: Callable[[int, int], None],
    ) -> list[Resultado]:
        """Reparte casos completos y reduce nuevas aperturas si baja la RAM."""
        if not pendientes:
            return []
        sin_empezar = list(enumerate(pendientes))
        terminadas: dict[int, Resultado] = {}
        en_curso: dict[Future[Resultado], tuple[int, Correccion]] = {}
        parar = Event()

        def cancelado() -> bool:
            return parar.is_set() or esta_cancelado()

        def atender(correccion: Correccion) -> Resultado:
            if cancelado():
                raise ConsultaCancelada()
            return self._un_caso(
                navegador, version, correccion, cancelado, ensayo,
            )

        # La RAM de una pestaña tarda en ocuparse mientras carga. Conservar
        # el techo inicial evita repartir otra vez ese mismo presupuesto.
        techo = min(
            _paralelismo_correcciones(0),
            len({correccion.log_number for correccion in pendientes}),
        )
        with ThreadPoolExecutor(max_workers=techo) as pool:
            try:
                while sin_empezar or en_curso:
                    if cancelado():
                        raise ConsultaCancelada()
                    limite = min(techo, _paralelismo_correcciones(len(en_curso)))
                    ocupadas = {c.log_number for _i, c in en_curso.values()}
                    for indice, correccion in list(sin_empezar):
                        if len(en_curso) >= limite:
                            break
                        if correccion.log_number in ocupadas:
                            continue
                        if cancelado():
                            raise ConsultaCancelada()
                        futuro = pool.submit(atender, correccion)
                        en_curso[futuro] = (indice, correccion)
                        ocupadas.add(correccion.log_number)
                        sin_empezar.remove((indice, correccion))
                    listas, _ = wait(
                        en_curso, timeout=0.2, return_when=FIRST_COMPLETED,
                    )
                    for futuro in sorted(listas, key=lambda f: en_curso[f][0]):
                        indice, correccion = en_curso.pop(futuro)
                        terminadas[indice] = futuro.result()
                        hechas = len(terminadas)
                        notificar(
                            f"Bitácora {correccion.log_number} "
                            f"({hechas} de {len(pendientes)})"
                        )
                        avanzar(hechas, len(pendientes))
            finally:
                # El navegador sigue vivo hasta que todos suelten sus casos.
                parar.set()
                for futuro in en_curso:
                    futuro.cancel()
        return [terminadas[indice] for indice in range(len(pendientes))]

    def _un_caso(
        self,
        navegador: _NavegadorDeCorrecciones,
        version: dict,
        correccion: Correccion,
        cancelar: Callable[[], bool],
        ensayo: bool,
    ) -> Resultado:
        """Un caso entero, con su pestana propia y sin dejarla abierta."""
        pagina: _Pagina | None = None
        target_id: str | None = None
        try:
            target_id = navegador.abrir_pestana(
                correccion.url_busqueda, version=version
            )
            pagina = _Pagina(version, target_id, cancelar)
            copias = copias_en(self._rejilla(pagina), correccion.log_number)
            if not copias:
                return Resultado(
                    correccion,
                    detalle=(
                        "La bitácora no aparece en Web Search. No se puede "
                        "corregir sin localizarla. Vuelva a consultar el reporte."
                    ),
                )
            if correccion.accion == ACCION_BORRAR:
                return self._borrar(pagina, correccion, copias, ensayo)
            return self._reindexar(pagina, correccion, copias, ensayo)
        except ConsultaCancelada:
            raise
        except ControlNoEncontrado as exc:
            return Resultado(correccion, detalle=str(exc))
        except Exception as exc:  # noqa: BLE001 - llega a la interfaz
            logger.exception("No se pudo corregir la bitácora {}", correccion.log_number)
            return Resultado(
                correccion,
                detalle=mensaje_error_correccion(exc),
            )
        finally:
            if pagina is not None:
                pagina.cerrar(forzar=True)
            elif target_id is not None:
                navegador.cerrar_pestana(version, target_id)

    @staticmethod
    def _rejilla(pagina: _Pagina) -> list[Mapping[str, object]]:
        if not pagina.esperar(_REJILLA_CARGADA, 180.0):
            raise ControlNoEncontrado(
                "Web Search no terminó de cargar la búsqueda. No se pudieron "
                "comprobar las copias de la bitácora. Vuelva a abrir la búsqueda "
                "y compruebe que tiene acceso a AirVault."
            )
        leidas = pagina.evaluar(_LEER_REJILLA)
        if not isinstance(leidas, list):
            raise ControlNoEncontrado(
                "No se pudo leer la tabla de Web Search ni comprobar las copias "
                "de la bitácora. Vuelva a cargar la búsqueda antes de corregirla."
            )
        return [fila for fila in leidas if isinstance(fila, Mapping)]

    @classmethod
    def _releer(cls, pagina: _Pagina, log_number: str) -> list[Copia]:
        """Vuelve a correr la busqueda y lee lo que hay ahora.

        Se recarga la pagina entera en vez de refrescar la rejilla: lo que
        se comprueba con esto es que AirVault haya escrito de verdad, y una
        rejilla repintada con lo que ya tenia en memoria no lo dice.
        """
        pagina.evaluar("window.__bits_recarga = 1; location.reload(); true")
        if not pagina.esperar(_REJILLA_RECARGADA, 180.0):
            raise ControlNoEncontrado(
                "Web Search no volvió a cargar la búsqueda. No se pudo verificar "
                "el estado de la bitácora. Compruebe las copias y la matrícula "
                "en Web Search antes de reintentar la corrección."
            )
        return copias_en(cls._rejilla(pagina), log_number)

    def _borrar(
        self,
        pagina: _Pagina,
        correccion: Correccion,
        copias: Sequence[Copia],
        ensayo: bool,
    ) -> Resultado:
        """Deja una sola copia: la mas antigua."""
        esperadas = correccion.sobran + 1
        if len(copias) != esperadas:
            if len(copias) == 1 and esperadas >= 2:
                return self._comprobar_reporte_desactualizado(
                    pagina, correccion, copias[0], esperadas,
                )
            return Resultado(
                correccion,
                detalle=(
                    f"El reporte indica {esperadas} copias; Web Search "
                    f"muestra {len(copias)}. No se elimina ninguna porque "
                    "la cantidad no coincide. Vuelva a consultar el reporte."
                ),
            )
        se_queda, sobran = por_antiguedad(copias)
        if se_queda is None:
            return Resultado(
                correccion,
                detalle=(
                    "No se pudo identificar la copia más antigua porque falta "
                    "la fecha de archivo de alguna copia o no se puede leer. "
                    "No se elimina ninguna: revise las fechas en Web Search "
                    "para decidir cuál conservar."
                ),
            )
        if any(copia.imagenes != 1 or copia.tipo.upper() != "LOG PAGE" for copia in copias):
            return Resultado(
                correccion,
                detalle=(
                    "No se elimina: alguna copia no está identificada como "
                    "bitácora de una sola imagen. Puede tener varias imágenes "
                    "o pertenecer a otra área. Revise las copias en Web Search."
                ),
            )
        if ensayo:
            se_van = (
                "se borraría una copia"
                if len(sobran) == 1
                else f"se borrarían {len(sobran)} copias"
            )
            return Resultado(
                correccion,
                detalle=(
                    f"Se conservaría la del {se_queda.cuando:%d/%m/%Y} y "
                    f"{se_van}."
                ),
            )
        if any(not copia.clave for copia in sobran):
            return Resultado(
                correccion,
                detalle=(
                    "Web Search no permitió identificar todas las copias. "
                    "No se elimina ninguna para evitar borrar la equivocada. "
                    "Vuelva a cargar la búsqueda."
                ),
            )
        if len({c.clave for c in copias}) != len(copias):
            return Resultado(correccion, detalle=(
                "Web Search muestra el mismo documento más de una vez; no se "
                "puede confirmar que sean copias distintas. No se elimina "
                "ninguna. Vuelva a cargar la búsqueda."
            ))
        if self.revisar is not None and (self.revision_activa is None or self.revision_activa()):
            vistas = [(c, self._imagen_previa(pagina, c)) for c in copias]
            elegidas = self.revisar(correccion, vistas, {c.clave for c in sobran})
            if not elegidas:
                return Resultado(correccion, detalle="Omitida en la revisión de imágenes.")
            if not set(elegidas) < {c.clave for c in copias}:
                return Resultado(correccion, detalle="Debe conservar al menos una copia.")
            if se_queda.clave in elegidas:
                return Resultado(correccion, detalle="Debe conservar la copia original (la más antigua).")
            sobran = [c for c in copias if c.clave in elegidas]
        for copia in sobran:
            actuales = self._releer(pagina, correccion.log_number)
            conocidas = {c.clave: c for c in copias}
            if any(c.clave not in conocidas or not self._misma_copia(c, conocidas[c.clave]) for c in actuales):
                raise ControlNoEncontrado(
                    "La búsqueda de Web Search cambió desde la revisión. "
                    "Se detiene la eliminación para evitar borrar una copia "
                    "equivocada. Revise las que quedan y vuelva a consultar el reporte."
                )
            if not any(c.clave == se_queda.clave for c in actuales):
                raise ControlNoEncontrado(
                    "La copia más antigua, que debía conservarse, ya no aparece "
                    "en Web Search. Se detiene la eliminación. Revise las copias "
                    "que quedan y vuelva a consultar el reporte."
                )
            if not any(c.clave == copia.clave for c in actuales):
                raise ControlNoEncontrado(
                    "La copia elegida para eliminar ya no aparece en Web Search. "
                    "Se detiene la eliminación. Revise las copias que quedan "
                    "y vuelva a consultar el reporte."
                )
            self._registrar("borrado_solicitado", correccion, copia)
            self._borrar_copia(pagina, copia)
        # Se vuelve a mirar la pantalla en vez de dar por hecho que la orden
        # surtio efecto. Un control que se pulsa pero no borra (permisos,
        # una confirmacion que nadie contesto) dejaria si no una corrida que
        # dice haber limpiado lo que sigue repetido.
        quedan = self._releer(pagina, correccion.log_number)
        una = len(sobran) == 1
        cuantas = "1 copia" if una else f"{len(sobran)} copias"
        if len(quedan) != len(copias) - len(sobran) or not any(c.clave == se_queda.clave for c in quedan):
            return Resultado(
                correccion,
                detalle=(
                    f"Se intentó borrar {cuantas}; Web Search muestra "
                    f"{len(quedan)} copias, pero debían quedar "
                    f"{len(copias) - len(sobran)} y conservarse la más antigua. "
                    "No se pudo confirmar ese resultado. "
                    "Revise las copias antes de reintentar."
                ),
            )
        self._registrar("borrado_verificado", correccion, se_queda)
        if len(quedan) > 1:
            return Resultado(
                correccion,
                detalle=(
                    f"{'Borrada' if una else 'Borradas'} {cuantas}; quedan "
                    f"{len(quedan)} copias para revisar. La bitácora sigue "
                    "duplicada. Vuelva a consultar el reporte antes de continuar."
                ),
            )
        return Resultado(
            correccion,
            hecho=True,
            detalle=(
                f"{'Borrada' if una else 'Borradas'} {cuantas}; queda la "
                f"del {se_queda.cuando:%d/%m/%Y}."
            ),
        )

    def _comprobar_reporte_desactualizado(
        self,
        pagina: _Pagina,
        correccion: Correccion,
        copia: Copia,
        esperadas: int,
    ) -> Resultado:
        """Confirma solo el caso raro: el reporte duplica una pagina ya limpia.

        No se consulta toda la lista ni se recuerda una ausencia para futuras
        corridas. Cada discrepancia se verifica de nuevo sin abrir imagenes.
        """
        detalle = (
            f"El reporte indica {esperadas} copias; Web Search muestra 1."
        )
        if (
            not copia.clave
            or copia.imagenes != 1
            or copia.tipo.upper() != "LOG PAGE"
            or pagina.evaluar(_REJILLA_CON_UNA_FILA) is not True
        ):
            return Resultado(
                correccion,
                detalle=(
                    detalle + " No se pudo confirmar que sea la única copia; "
                    "no se elimina nada."
                ),
            )
        actuales = self._releer(pagina, correccion.log_number)
        if (
            len(actuales) != 1
            or not self._misma_copia(actuales[0], copia)
            or pagina.evaluar(_REJILLA_CON_UNA_FILA) is not True
        ):
            return Resultado(
                correccion,
                detalle=(
                    detalle + " La búsqueda cambió o quedó incompleta al "
                    "comprobarla; no se elimina nada. Vuelva a consultar "
                    "el reporte."
                ),
            )
        return Resultado(
            correccion,
            detalle=(
                f"Reporte desactualizado: indica {esperadas} copias, pero "
                "Web Search confirma una sola después de volver a cargar "
                "la búsqueda. Ya no aparece duplicada; no se elimina nada. "
                "Vuelva a consultar el reporte con Actualizar en YES."
            ),
        )

    @staticmethod
    def _misma_copia(actual: Copia, anterior: Copia) -> bool:
        return (actual.clave, actual.documento, actual.cuando, actual.matricula, actual.imagenes, actual.tipo) == (
            anterior.clave, anterior.documento, anterior.cuando, anterior.matricula, anterior.imagenes, anterior.tipo
        )

    def _registrar(self, evento: str, correccion: Correccion, copia: Copia) -> None:
        """Registra identidad y evidencia sin cookies ni credenciales."""
        self.auditoria.parent.mkdir(parents=True, exist_ok=True)
        registro = {"fecha": datetime.now().astimezone().isoformat(), "evento": evento,
                    "bitacora": correccion.log_number, "clave": copia.clave,
                    "documento": copia.documento, "imagenes": copia.imagenes,
                    "tipo": copia.tipo, "matricula": copia.matricula,
                    "fecha_documento": copia.cuando.isoformat() if copia.cuando else None}
        with _CANDADO_AUDITORIA:
            with self.auditoria.open("a", encoding="utf-8") as salida:
                salida.write(json.dumps(registro, ensure_ascii=False) + "\n")

    def _imagen_previa(self, pagina: _Pagina, copia: Copia) -> bytes:
        """Vista legible de la misma imagen PNG que abre el visor de AirVault."""
        clave = (copia.clave, copia.cuando, copia.imagenes)
        with self._candado_previa:
            if clave in self.cache_previa:
                return self.cache_previa[clave]
        codificada = base64.b64encode(copia.clave.encode("utf-8")).decode("ascii")
        url = ("/zfp/Document/GetHiglightedPage/?docKey=" + codificada
               + "&searchId=0&pageNum=1&encodedHighlightSearchInputs=&encodedFullTextKeyWord="
               + "&preferredFileFormat=png&realizeAnnotations=true")
        dato = pagina.evaluar("""(url => new Promise(resolve => {
          const img = new Image();
          const timer = setTimeout(() => resolve(''), 45000);
          img.onerror = () => { clearTimeout(timer); resolve(''); };
          img.onload = () => {
            clearTimeout(timer);
            try {
              const scale = Math.min(1, 2400 / Math.max(img.naturalWidth, img.naturalHeight));
              const canvas = document.createElement('canvas');
              canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
              canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
              canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
              resolve(canvas.toDataURL('image/png').split(',')[1]);
            } catch (_) { resolve(''); }
          };
          img.src = url;
        }))(%s)""" % json.dumps(url))
        if not dato or not isinstance(dato, str):
            raise ControlNoEncontrado(
                "No se pudo cargar la imagen para comparar las copias. "
                "No se elimina ninguna. Compruebe la imagen en Web Search "
                "antes de reintentar."
            )
        try:
            imagen = base64.b64decode(dato, validate=True)
        except ValueError as exc:
            raise ControlNoEncontrado(
                "AirVault no entregó una imagen válida para comparar las "
                "copias. No se elimina ninguna. Revise las imágenes en Web Search."
            ) from exc
        if not imagen.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ControlNoEncontrado(
                "AirVault no entregó una imagen válida para comparar las "
                "copias. No se elimina ninguna. Revise las imágenes en Web Search."
            )
        with self._candado_previa:
            if len(self.cache_previa) >= 64:
                self.cache_previa.pop(next(iter(self.cache_previa)))
            self.cache_previa[clave] = imagen
        return imagen

    def _reindexar(
        self,
        pagina: _Pagina,
        correccion: Correccion,
        copias: Sequence[Copia],
        ensayo: bool,
    ) -> Resultado:
        """Le pone a la pagina el avion del libro al que pertenece."""
        if len(copias) != 1:
            return Resultado(
                correccion,
                detalle=(
                    f"Web Search muestra {len(copias)} copias de la bitácora. "
                    "No se reindexa porque no se puede elegir una sola copia. "
                    "Revise las duplicadas y vuelva a consultar el reporte."
                ),
            )
        copia = copias[0]
        actual = copia.matricula
        if actual and actual.casefold() != (
            correccion.matricula_actual.casefold()
        ):
            return Resultado(
                correccion,
                detalle=(
                    f"El reporte indica {correccion.matricula_actual}; Web "
                    f"Search muestra {actual}. No se reindexa porque la "
                    "matrícula actual no coincide con el reporte. Vuelva "
                    "a consultar el reporte antes de continuar."
                ),
            )
        if ensayo:
            return Resultado(
                correccion,
                detalle=(
                    f"Se pasaría de {correccion.matricula_actual} a "
                    f"{correccion.matricula_correcta}."
                ),
            )
        if not copia.clave:
            return Resultado(
                correccion,
                detalle=(
                    "Web Search no permitió identificar el documento de esta "
                    "bitácora. No se reindexa para evitar cambiar la copia "
                    "equivocada. Vuelva a cargar la búsqueda."
                ),
            )
        flota, _arrendador, _inferida = self.flota.resolver(
            correccion.matricula_correcta
        )
        self._reindexar_documento(
            pagina, copia, correccion.matricula_correcta, flota
        )
        quedan = self._releer(pagina, correccion.log_number)
        ahora = quedan[0].matricula if len(quedan) == 1 else ""
        if ahora.casefold() != correccion.matricula_correcta.casefold():
            if not quedan:
                observado = "la bitácora ya no aparece en Web Search"
            elif len(quedan) > 1:
                observado = f"Web Search muestra {len(quedan)} copias"
            elif not ahora:
                observado = "Web Search no muestra la matrícula de la bitácora"
            else:
                observado = f"Web Search sigue mostrando la matrícula {ahora}"
            return Resultado(
                correccion,
                detalle=(
                    f"No se pudo confirmar la reindexación en "
                    f"{correccion.matricula_correcta}: {observado}. "
                    "Revise la bitácora antes de reintentar."
                ),
            )
        return Resultado(
            correccion,
            hecho=True,
            detalle=f"Reindexada en {correccion.matricula_correcta}.",
        )

    # ── los cuadros de la pantalla ──────────────────────────────────
    #
    # Los dos van igual: se abre el cuadro desde la fila, se comprueba que
    # sea el que se pidio (AirVault contesta con un aviso cuando no deja) y
    # se pulsa su boton. El boton se busca por lo que dice, que es lo unico
    # que lleva: los de un cuadro de jQuery UI no tienen identificador.

    @classmethod
    def _abrir_cuadro(
        cls,
        pagina: _Pagina,
        copia: Copia,
        operacion: str,
        cuadro: str,
        control: str,
    ) -> None:
        """Abre el cuadro de esa fila y no vuelve hasta que se puede usar.

        El cuadro llega en dos tiempos: primero el marco, que es lo que
        contesta ``getElementById``, y despues los campos, que vienen del
        servidor. Entre los dos hay un cuadro montado y vacio, y ahi es donde
        se buscaba el campo del avion y no estaba.
        """
        abierto = pagina.evaluar(
            _ABRIR_CUADRO % (json.dumps(copia.clave), json.dumps(operacion))
        )
        accion = (
            "eliminar la copia" if cuadro == "deletePageDialog"
            else "reindexar la bitácora"
        )
        if abierto != "OK":
            raise ControlNoEncontrado(
                f"No se pudo {accion}: {abierto or 'AirVault no respondió'}."
            )
        if not pagina.esperar(_cuadro_abierto(cuadro), 120.0):
            raise ControlNoEncontrado(
                f"AirVault no abrió la ventana para {accion}. "
                "Vuelva a cargar la búsqueda y compruebe su acceso a AirVault."
            )
        aviso = pagina.evaluar(_MENSAJE)
        if aviso:
            raise ControlNoEncontrado(mensaje_airvault(aviso, accion))
        with cls._soltando(pagina, cuadro):
            if not pagina.esperar(control, 120.0):
                aviso = pagina.evaluar(_MENSAJE)
                if aviso:
                    raise ControlNoEncontrado(mensaje_airvault(aviso, accion))
                raise ControlNoEncontrado(
                    f"AirVault abrió la ventana para {accion}, pero no cargó "
                    "los campos necesarios. Vuelva a cargar la búsqueda "
                    "antes de reintentar."
                )

    @classmethod
    @contextmanager
    def _soltando(cls, pagina: _Pagina, cuadro: str):
        """Cierra el cuadro si lo de dentro no llega al final.

        Mientras el cuadro esta abierto AirVault tiene tomado el documento.
        Dejarlo asi (por un campo que no llega, o por una matricula que esta
        instalacion no ofrece) lo bloquea para todo el mundo, incluido el
        siguiente intento de este mismo programa.
        """
        try:
            yield
        except Exception:
            try:
                # Cancelar también debe soltar un documento ya tomado; la
                # evaluación normal rechaza cualquier orden al cancelar.
                cerrar = getattr(pagina, "evaluar_al_cerrar", pagina.evaluar)
                cerrar(_CANCELAR_CUADRO % json.dumps(cuadro))
            except Exception:  # noqa: BLE001 - el fallo de arriba manda
                pass
            raise

    @classmethod
    def _cerrar_cuadro(
        cls,
        pagina: _Pagina,
        cuadro: str,
        que: str,
        contestando: Sequence[str] = (),
    ) -> None:
        """Espera a que el cuadro se cierre, contestando lo que pregunte.

        AirVault puede pararse a medio guardar para pedir confirmacion, y
        ese cuadro no aparece en el instante en que se pulsa el boton: llega
        cuando contesta el servidor. Preguntar una sola vez, nada mas
        pulsar, era preguntar antes de que hubiera nada que contestar, y el
        caso se quedaba los cinco minutos esperando a que se cerrara un
        cuadro que nadie iba a cerrar.
        """
        accion = (
            "eliminar la copia" if cuadro == "deletePageDialog"
            else "reindexar la bitácora"
        )
        limite = time.monotonic() + 300.0
        while not pagina.esperar(_cuadro_cerrado(cuadro), 1.0):
            if time.monotonic() >= limite:
                raise ControlNoEncontrado(
                    f"AirVault no terminó de confirmar el {que}. La ventana "
                    "sigue abierta y no se pudo verificar el resultado. "
                    "Compruebe la bitácora en Web Search antes de reintentar."
                )
            if contestando:
                respuesta = pagina.evaluar(
                    _SEGUIR_GUARDANDO % json.dumps(list(contestando))
                )
                if respuesta == "OK":
                    continue
            aviso = pagina.evaluar(_MENSAJE)
            if aviso:
                raise ControlNoEncontrado(mensaje_airvault(aviso, accion))
        aviso = pagina.evaluar(_MENSAJE)
        if aviso:
            raise ControlNoEncontrado(mensaje_airvault(aviso, accion))

    @classmethod
    def _borrar_copia(cls, pagina: _Pagina, copia: Copia) -> None:
        """Elimina únicamente documentos de bitácora con una sola imagen."""
        if copia.imagenes != 1 or copia.tipo.upper() != "LOG PAGE":
            raise ControlNoEncontrado(
                "No se elimina la copia porque no se pudo confirmar que sea "
                "una bitácora de una sola imagen. Revise el tipo de documento "
                "y sus imágenes en Web Search."
            )
        cls._abrir_cuadro(
            pagina,
            copia,
            "onDeletePage",
            "deletePageDialog",
            "document.getElementById('rdAllPages')",
        )
        with cls._soltando(pagina, "deletePageDialog"):
            hecho = pagina.evaluar(
                _CONFIRMAR_BORRADO % json.dumps(list(TEXTOS_BORRAR))
            )
            if hecho != "OK":
                raise ControlNoEncontrado(
                    f"No se pudo eliminar la copia: {hecho or 'AirVault no respondió'}. "
                    "Compruebe la copia en Web Search antes de reintentar."
                )
            cls._cerrar_cuadro(pagina, "deletePageDialog", "borrado")

    @classmethod
    def _reindexar_documento(
        cls, pagina: _Pagina, copia: Copia, matricula: str, flota: str
    ) -> None:
        cls._abrir_cuadro(
            pagina,
            copia,
            "onReindexDocument",
            "reindexDialog",
            _CAMPO_DEL_AVION,
        )
        with cls._soltando(pagina, "reindexDialog"):
            hecho = pagina.evaluar(
                _CONFIRMAR_REINDEXADO
                % (
                    json.dumps(matricula),
                    json.dumps(flota),
                    json.dumps(list(TEXTOS_GUARDAR)),
                )
            )
            if hecho != "OK":
                raise ControlNoEncontrado(
                    f"No se pudo reindexar la bitácora en {matricula}: "
                    f"{hecho or 'AirVault no respondió'}. "
                    "Revise la matrícula en Web Search antes de reintentar."
                )
            cls._cerrar_cuadro(
                pagina, "reindexDialog", "reindexado", TEXTOS_SEGUIR,
            )
