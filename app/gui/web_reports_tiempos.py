"""Cuánto tarda de verdad cada trabajo de Web Reports en este equipo.

El cronómetro de la ventana necesita una cuenta de lo que falta, y para eso
hace falta saber qué cuesta cada cosa. Ninguno de los tres trabajos que la
ventana lanza a Edge se puede saber por adelantado: dependen de la red, del
servidor de informes y de cuántas copias tenga cada bitácora. Así que se
miden al correr y se guardan, igual que la ventana principal guarda lo que le
costó una página.

Cada trabajo se descompone igual: una apertura (levantar Edge y rehacer la
sesión de AirVault, que no depende de cuánto haya por hacer) y después tantas
unidades como piezas tenga el trabajo. La unidad es el reporte en la consulta
y la bitácora en la corrección, que es lo que la ventana ve terminar una por
una. Abrir una búsqueda en Web Search es apertura y nada más.

Las cifras que se escriben son las de la última corrida completa. Una
cancelada no cuenta: deja fuera lo que faltaba y enseñaría un trabajo más
corto de lo que es.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from app.gui.eta import estimate_remaining_seconds
from app.utils.portable import app_root

# Junto al del rendimiento del OCR y por lo mismo: son medidas de este equipo,
# no del proyecto, y la carpeta de salidas es la que se puede borrar entera
# sin perder nada que no se vuelva a medir solo.
ARCHIVO = app_root() / "output" / ".web_reports.json"

TAREA_CONSULTA = "consulta"
TAREA_CORRECCION = "correccion"
TAREA_BUSQUEDA = "busqueda"

# Ritmo al que el tiempo observado le gana al guardado. Aquí una unidad es un
# reporte o una bitácora entera, no una página: con tres ya se sabe a qué
# velocidad va esta corrida, y esperar a veinte sería no mirarla nunca,
# porque casi ninguna corrida tiene tantas.
CALENTAMIENTO = 3.0

# Entre qué valores se admite una medida, en segundos. Fuera de ahí no es el
# ritmo del equipo sino un trabajo que se quedó esperando algo, y guardarlo
# estropearía la estimación de la corrida siguiente.
MINIMO_S = 1.0
MAXIMO_S = 3600.0


@dataclass(frozen=True)
class Tiempos:
    """Lo que cuesta abrir el trabajo y lo que cuesta cada unidad suya."""

    apertura_s: float
    unidad_s: float


# Con qué se estima antes de haber medido nada en este equipo. Son órdenes de
# magnitud de una red de oficina: levantar Edge y volver a entrar ronda el
# medio minuto, un reporte de Log Page Audit tarda más que eso y una bitácora
# se corrige en unos treinta segundos. La primera corrida completa los
# sustituye por los de aquí.
POR_OMISION = {
    TAREA_CONSULTA: Tiempos(apertura_s=30.0, unidad_s=90.0),
    TAREA_CORRECCION: Tiempos(apertura_s=30.0, unidad_s=30.0),
    TAREA_BUSQUEDA: Tiempos(apertura_s=15.0, unidad_s=0.0),
}


def _acotar(segundos: float, minimo: float = MINIMO_S) -> float:
    return min(max(float(segundos), minimo), MAXIMO_S)


def _ahora(ahora: float | None) -> float:
    """El reloj de pared del trabajo, o el que pase quien lo mide."""
    return time.monotonic() if ahora is None else ahora


def _guardadas() -> dict:
    try:
        with open(ARCHIVO, encoding="utf-8") as archivo:
            datos = json.load(archivo)
        return datos if isinstance(datos, dict) else {}
    except Exception:  # noqa: BLE001 - sin medidas se estima con las de fábrica
        return {}


def cargar(tarea: str) -> Tiempos:
    """Lo que costó ese trabajo la última vez, o lo que se supone que cuesta."""
    omision = POR_OMISION.get(tarea, POR_OMISION[TAREA_CONSULTA])
    medidas = _guardadas().get(tarea)
    if not isinstance(medidas, dict):
        return omision
    try:
        return Tiempos(
            apertura_s=_acotar(medidas.get("apertura_s", omision.apertura_s)),
            unidad_s=_acotar(
                medidas.get("unidad_s", omision.unidad_s), minimo=0.0
            ),
        )
    except (TypeError, ValueError):
        return omision


def guardar(tarea: str, tiempos: Tiempos) -> None:
    """Deja las medidas de esta corrida para la estimación de la siguiente."""
    datos = _guardadas()
    datos[tarea] = {
        "apertura_s": round(_acotar(tiempos.apertura_s), 1),
        "unidad_s": round(_acotar(tiempos.unidad_s, minimo=0.0), 1),
    }
    try:
        ARCHIVO.parent.mkdir(parents=True, exist_ok=True)
        with open(ARCHIVO, "w", encoding="utf-8") as archivo:
            json.dump(datos, archivo)
    except Exception:  # noqa: BLE001 - no poder medir no rompe el trabajo
        pass


class Estimacion:
    """Lo que lleva y lo que le falta al trabajo que corre ahora en Edge.

    La ventana la crea al lanzar el hilo, le dice cuántas unidades van hechas
    y le pregunta las dos cifras cada vez que repinta el cronómetro. Al
    terminar bien, ``aprender`` guarda lo que de verdad costó.
    """

    def __init__(
        self,
        tarea: str,
        unidades: int,
        tiempos: Tiempos | None = None,
        inicio: float | None = None,
    ) -> None:
        self.tarea = tarea
        self.tiempos = tiempos or cargar(tarea)
        self.unidades = max(0, int(unidades))
        self._inicio = _ahora(inicio)
        self._abierto: float | None = None
        self._hechas = 0

    @property
    def hechas(self) -> int:
        """Unidades terminadas hasta ahora."""
        return self._hechas

    def abrio(self, ahora: float | None = None) -> None:
        """La apertura quedó atrás: desde aquí se cuentan las unidades."""
        if self._abierto is None:
            self._abierto = _ahora(ahora)

    def avanzo(
        self,
        hechas: int,
        total: int | None = None,
        ahora: float | None = None,
    ) -> None:
        """Unidades terminadas, contadas por quien hace el trabajo.

        El primer aviso llega con cero hechas en cuanto la sesión está en
        pie, que es la señal de que la apertura terminó. El total viaja con
        cada aviso porque quien corre el trabajo es el que sabe cuántas
        piezas tenía de verdad: el plan de la ventana incluye filas que solo
        se pueden revisar a mano, y esas no se abren en Edge.
        """
        self.abrio(ahora)
        if total is not None:
            self.unidades = max(int(total), int(hechas))
        self._hechas = max(self._hechas, int(hechas))

    def transcurrido(self, ahora: float | None = None) -> float:
        """Lo que lleva corriendo el trabajo, desde que se lanzó el hilo."""
        return max(0.0, _ahora(ahora) - self._inicio)

    def restante(self, ahora: float | None = None) -> float:
        """Lo que falta: lo que quede de apertura más lo que quede de trabajo.

        Mientras la sesión se levanta no hay nada medido todavía, así que se
        cuenta con lo guardado. Ya abierta, el ritmo de esta corrida manda
        sobre el histórico en cuanto hay unidades terminadas que mirar.
        """
        momento = _ahora(ahora)
        if self._abierto is None:
            falta_abrir = max(
                0.0, self.tiempos.apertura_s - self.transcurrido(momento)
            )
            return falta_abrir + self.unidades * self.tiempos.unidad_s
        pendiente = estimate_remaining_seconds(
            total_pages=self.unidades,
            completed_pages=self._hechas,
            elapsed_seconds=momento - self._abierto,
            cached_ms_per_page=self.tiempos.unidad_s * 1000.0,
            warmup_units=CALENTAMIENTO,
        )
        return pendiente or 0.0

    def medido(self, ahora: float | None = None) -> Tiempos | None:
        """Lo que costó de verdad, o nada si no llegó ni a abrir la sesión.

        Sin unidades terminadas se conserva el costo por unidad que ya había:
        el trabajo no dio ninguna medida nueva de esa parte, y escribir un
        cero haría que la próxima estimación diera todo por instantáneo.
        """
        if self._abierto is None:
            return None
        apertura_s = self._abierto - self._inicio
        if not self._hechas:
            return Tiempos(apertura_s, self.tiempos.unidad_s)
        return Tiempos(
            apertura_s, (_ahora(ahora) - self._abierto) / self._hechas
        )

    def aprender(self, ahora: float | None = None) -> Tiempos | None:
        """Guarda lo medido. Solo lo llama quien vio terminar el trabajo."""
        medido = self.medido(ahora)
        if medido is not None:
            guardar(self.tarea, medido)
        return medido
