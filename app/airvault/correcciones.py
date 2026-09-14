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

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from app.airvault.config import AirVaultConfig
from app.airvault.mapping import FLOTA_CACHE_FILENAME, ResolutorFlota
from app.airvault.navegador import (
    PERFIL_POR_DEFECTO,
    ErrorDeNavegador,
    SesionDeNavegador,
    cookies_de,
    _edges_del_perfil,
    _puerto_anotado,
    _sin_ventana,
    _version_en,
    _WebSocket,
)
from app.airvault.web_reports import (
    TIPO_DUPLICADA,
    TIPO_MAL_INDEXADA,
    ConsultaCancelada,
    ExcepcionLogPageAudit,
    entrada_federada,
    esperar_acceso,
    _Pagina,
)
from app.utils.portable import app_root

ACCION_BORRAR = "Borrar copias"
ACCION_REINDEXAR = "Reindexar"
ACCION_REVISAR = "Revisar a mano"

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


class _NavegadorDeCorrecciones:
    """Abre pestañas temporales sin tocar las que abrió la persona.

    El corrector comparte el perfil de AirVault para usar la sesión iniciada.
    Si ese perfil ya está abierto en una ventana visible, trabaja en pestañas
    de fondo y deja la ventana abierta. Si el perfil está libre, usa el Edge
    oculto habitual y lo cierra al terminar.
    """

    def __init__(self, perfil: Path) -> None:
        self.perfil = Path(perfil)
        self._sesion: SesionDeNavegador | None = None
        self._version_visible: dict | None = None

    def __enter__(self) -> "_NavegadorDeCorrecciones":
        return self

    def __exit__(self, *_exc) -> None:
        if self._sesion is not None:
            self._sesion.cerrar()

    def abrir(self, url: str, espera_s: float) -> dict:
        version = self._edge_visible()
        if version is not None:
            self._version_visible = version
            return version
        self._sesion = SesionDeNavegador(self.perfil, visible=False)
        return self._sesion.abrir(url, espera_s=espera_s)

    def abrir_pestana(self, url: str, version: dict) -> str:
        if self._version_visible is None:
            if self._sesion is None:
                raise ErrorDeNavegador("No hay una sesión de Edge abierta")
            return self._sesion.abrir_pestana(url, version=version)

        ws = _WebSocket(version["webSocketDebuggerUrl"])
        try:
            creada = ws.pedir(
                "Target.createTarget", url=url, background=True
            )
        finally:
            ws.cerrar()
        target_id = str(creada.get("targetId", ""))
        if not target_id:
            raise ErrorDeNavegador(
                "Edge no abrió la pestaña temporal de corrección"
            )
        return target_id

    def cookies(self, version: dict) -> dict:
        """Las cookies del perfil, sea propio el navegador o prestado."""
        return cookies_de(version)

    def _edge_visible(self) -> dict | None:
        anotado = _puerto_anotado(self.perfil)
        if anotado is not None:
            version = _version_en(anotado)
            if version is not None and not _sin_ventana(version):
                return version
        for _pid, puerto in _edges_del_perfil(self.perfil):
            if puerto is None or puerto == anotado:
                continue
            version = _version_en(puerto)
            if version is not None and not _sin_ventana(version):
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
      cuando: String(datos.LastFileDate || '').trim()
    });
  });
  return salida;
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
  if (!fila) return 'ese documento ya no está en la búsqueda';
  var accion = window[operacion];
  if (typeof accion !== 'function') {
    return 'este AirVault no ofrece ' + operacion;
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
# another user»). Se copia tal cual al resultado del caso, que dice mas que
# cualquier frase que se pudiera inventar aqui.
_MENSAJE = r"""(function(){
  var aviso = document.getElementById('messageDialog');
  if (!aviso || aviso.offsetParent === null) return '';
  return (aviso.innerText || '').replace(/\s+/g, ' ').trim();
})()"""

_CONFIRMAR_BORRADO = r"""(function(textos){
  var cuadro = document.getElementById('deletePageDialog');
  if (!cuadro) return 'el cuadro de borrado ya no está';
  var todas = document.getElementById('rdAllPages');
  if (!todas) return 'el cuadro de borrado no ofrece «All Pages»';
  if (!todas.checked) todas.click();
  if (!todas.checked) return 'AirVault no dejó marcar «All Pages»';
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
  if (!boton) return 'el cuadro de borrado no traía el botón de borrar';
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
  if (!cuadro) return 'el cuadro de reindexado ya no está';
  var todas = document.getElementById('rdAllPages');
  if (todas && !todas.checked) todas.click();
  function elegir(nombre, valor){
    var campo = cuadro.querySelector('[data-name="' + nombre + '"]');
    if (!campo) return 'el cuadro de reindexado no trae ' + nombre;
    var hay = false;
    Array.prototype.forEach.call(campo.options || [], function(opcion){
      if (opcion.value === valor) hay = true;
    });
    if (!hay) return 'AirVault no ofrece ' + valor + ' en ' + nombre;
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
  if (!boton) return 'el cuadro de reindexado no traía el botón de guardar';
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
                detalle="Revisión manual.",
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
            for numero, correccion in enumerate(pendientes, start=1):
                if esta_cancelado():
                    raise ConsultaCancelada()
                notificar(
                    f"Bitácora {correccion.log_number} "
                    f"({numero} de {len(pendientes)})"
                )
                resultados.append(
                    self._un_caso(
                        navegador, version, correccion,
                        esta_cancelado, ensayo,
                    )
                )
                avanzar(numero, len(pendientes))
        return resultados

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
        try:
            target_id = navegador.abrir_pestana(
                correccion.url_busqueda, version=version
            )
            pagina = _Pagina(version, target_id, cancelar)
            copias = copias_en(self._rejilla(pagina), correccion.log_number)
            if not copias:
                return Resultado(
                    correccion,
                    detalle="No aparece en Web Search.",
                )
            if correccion.accion == ACCION_BORRAR:
                return self._borrar(pagina, correccion, copias, ensayo)
            return self._reindexar(pagina, correccion, copias, ensayo)
        except ConsultaCancelada:
            raise
        except ControlNoEncontrado as exc:
            return Resultado(correccion, detalle=str(exc))
        except Exception as exc:  # noqa: BLE001 - llega a la interfaz
            return Resultado(
                correccion,
                detalle=f"Error en Edge: {exc}",
            )
        finally:
            if pagina is not None:
                pagina.cerrar()

    @staticmethod
    def _rejilla(pagina: _Pagina) -> list[Mapping[str, object]]:
        if not pagina.esperar(_REJILLA_CARGADA, 180.0):
            raise ControlNoEncontrado(
                "Web Search no terminó de cargar la búsqueda."
            )
        leidas = pagina.evaluar(_LEER_REJILLA)
        if not isinstance(leidas, list):
            raise ControlNoEncontrado(
                "No se pudo leer la tabla de Web Search."
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
                "Web Search no volvió a cargar la búsqueda."
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
            return Resultado(
                correccion,
                detalle=(
                    f"El reporte indica {esperadas} copias; Web Search "
                    f"muestra {len(copias)}."
                ),
            )
        se_queda, sobran = por_antiguedad(copias)
        if se_queda is None:
            return Resultado(
                correccion,
                detalle="No se pudo identificar la copia más antigua.",
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
                detalle="Web Search no dio la clave de todas las copias.",
            )
        for copia in sobran:
            self._borrar_copia(pagina, copia)
        # Se vuelve a mirar la pantalla en vez de dar por hecho que la orden
        # surtio efecto. Un control que se pulsa pero no borra (permisos,
        # una confirmacion que nadie contesto) dejaria si no una corrida que
        # dice haber limpiado lo que sigue repetido.
        quedan = self._releer(pagina, correccion.log_number)
        una = len(sobran) == 1
        cuantas = "1 copia" if una else f"{len(sobran)} copias"
        if len(quedan) != 1:
            return Resultado(
                correccion,
                detalle=(
                    f"Se intentó borrar {cuantas}; todavía aparecen "
                    f"{len(quedan)}."
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
                    f"Aparece {len(copias)} veces; no se puede elegir una "
                    "sola página."
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
                    f"Search muestra {actual}."
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
                detalle="Web Search no dio la clave de esa página.",
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
            return Resultado(
                correccion,
                detalle=(
                    f"Se pidió pasarla a {correccion.matricula_correcta}; "
                    f"Web Search muestra {ahora or 'otra cosa'}."
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
        if abierto != "OK":
            raise ControlNoEncontrado(
                f"No se pudo abrir el cuadro ({abierto})."
            )
        if not pagina.esperar(_cuadro_abierto(cuadro), 120.0):
            raise ControlNoEncontrado("AirVault no abrió el cuadro.")
        aviso = pagina.evaluar(_MENSAJE)
        if aviso:
            raise ControlNoEncontrado(f"AirVault contestó: {aviso}")
        with cls._soltando(pagina, cuadro):
            if not pagina.esperar(control, 120.0):
                aviso = pagina.evaluar(_MENSAJE)
                if aviso:
                    raise ControlNoEncontrado(f"AirVault contestó: {aviso}")
                raise ControlNoEncontrado(
                    "El cuadro de AirVault se quedó sin sus campos."
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
                pagina.evaluar(_CANCELAR_CUADRO % json.dumps(cuadro))
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
        limite = time.monotonic() + 300.0
        while not pagina.esperar(_cuadro_cerrado(cuadro), 1.0):
            if time.monotonic() >= limite:
                raise ControlNoEncontrado(
                    f"AirVault dejó abierto el cuadro de {que}."
                )
            if contestando:
                pagina.evaluar(
                    _SEGUIR_GUARDANDO % json.dumps(list(contestando))
                )
        aviso = pagina.evaluar(_MENSAJE)
        if aviso:
            raise ControlNoEncontrado(f"AirVault contestó: {aviso}")

    @classmethod
    def _borrar_copia(cls, pagina: _Pagina, copia: Copia) -> None:
        """Borra ese documento entero: todas sus páginas."""
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
                    f"No se pudo borrar la copia ({hecho})."
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
                    f"No se pudo cambiar la matrícula ({hecho})."
                )
            cls._cerrar_cuadro(
                pagina, "reindexDialog", "reindexado", TEXTOS_SEGUIR,
            )
