"""Hasta dónde llega el botón «Automático» de la ventana principal.

El proceso automático es la cadena entera puesta en un solo botón: leer los
PDF, escribir las salidas y, si se pide, subir la entrega a AirVault y
escribirla allí sin volver a pulsar nada. Qué tramo de esa cadena se recorre
se elige aquí, y la elección sobrevive al cierre del programa porque vive en
el JSON portable, junto al programa y no en el registro de Windows.

Las opciones estaban en la ventana de AirVault, donde solo se veían después
de haber procesado y solo decidían el tramo final. Puestas en la ventana
principal deciden la cadena completa, que es lo que el botón ejecuta.

La lista es un menú que se abre encima de todo, como el del botón derecho,
y no un panel que se despliega dentro de la ventana. Empotrado le quitaba
alto a lo que se está mirando y en una pantalla baja los pasos de abajo
quedaban fuera del borde; un menú se dibuja sobre la ventana, no dentro de
ella, así que cabe entero sin robarle sitio a nada. Se queda abierto entre
clic y clic: son varias casillas y cerrarlo en la primera obligaba a
volver a abrirlo por cada paso.

Tres pasos no se eligen: preprocesar, procesar y exportar. Sin enderezar y
alinear las páginas el OCR no lee, sin OCR no hay datos y sin salidas no hay
nada que subir, así que aparecen marcados y apagados, para que la lista diga
todo lo que va a pasar y no solo la parte opcional.

Depurar tampoco se elige, y por el motivo contrario: no está. Borrar páginas
sin que nadie las mire es lo único de la cadena que quita datos, y de una
bitácora repetida no se sabe cuál de las dos apariciones sobra sin leerlas
las dos. Se quita a mano, desde «Depurar», que ahora enseña la matrícula, la
fecha y el vuelo de cada aparición para poder decidir.

Los tres de AirVault van uno detrás de otro y se marcan juntos: marcar
«Indexar páginas» enciende subir, porque no se puede indexar lo que no está
arriba; apagar «Subir a AirVault» apaga los dos de abajo, que sin la carga
no tienen sobre qué trabajar.

Esperar a que AirVault deje los batches listos no es un paso que se elija:
va dentro de subir. Subir sin esperar la respuesta no deja nada terminado
(el batch se queda en la cola y nadie vuelve a mirarlo), así que la casilla
solo servía para dejar la cadena a medias. Por eso la entrada dice solo
«Subir a AirVault» y no nombra la espera: la espera no es algo que se
marque, sino parte de lo que hace subir. Se sigue viendo en la línea de
pasos, que es donde importa saber que el tiempo se va ahí, y el intervalo
se elige en «Revisar cada», en la ventana de AirVault.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QSignalBlocker, QSize, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLayout,
    QSizePolicy,
    QWidget,
)

from app.airvault.config import (
    AIRVAULT_FILENAME,
    AirVaultConfig,
    guardar_preferencias,
)
from app.gui.tokens import paleta
from app.gui.widgets import MultiSelectMenu, pintar_del_tema

# Pasos que la persona elige, en el orden en que ocurren. Los tres son la
# cadena de AirVault y se arrastran entre ellos.
SUBIR = "subir"
INDEXAR = "indexar"
COMPLETAR = "completar"

# La espera no se elige: ocurre dentro de subir. Es un paso del recorrido
# (se ve en la línea y se cuenta), pero no una casilla del menú.
ESPERAR = "esperar"

CADENA = (SUBIR, INDEXAR, COMPLETAR)
PASOS = CADENA

# Los tres que siempre se hacen. No son opciones (no se pueden desmarcar),
# pero sí son pasos, y la cadena que se enseña mientras corre tiene que
# contarlos: sin ellos, «va por 2 de 5» no diría por dónde va de verdad.
#
# «Preprocesar» es la primera parte del procesamiento, no un botón aparte:
# el pipeline recorre el batch entero enderezando y alineando cada página
# (la calibración) antes de leer ninguna. Es un tramo largo (en un libro de
# 50 páginas son unos diez segundos) y hasta ahora la línea de pasos lo
# contaba como si ya estuviera procesando, así que el primer paso parecía
# atascado. Como paso propio se ve dónde está de verdad.
PREPROCESAR = "preprocesar"
PROCESAR = "procesar"
EXPORTAR = "exportar"

# La cadena entera, en el orden en que ocurre.
RECORRIDO = (
    PREPROCESAR, PROCESAR, EXPORTAR, SUBIR, ESPERAR, INDEXAR, COMPLETAR,
)

# Cómo se llama cada paso cuando hay siete en una sola línea. En el menú
# cada uno tiene sitio para explicarse; aquí no, y lo que importa es
# reconocerlo de un vistazo.
NOMBRES_CORTOS = {
    PREPROCESAR: "Preprocesar",
    PROCESAR: "Procesar",
    EXPORTAR: "Exportar",
    SUBIR: "Subir",
    ESPERAR: "Esperar",
    INDEXAR: "Indexar",
    COMPLETAR: "Completar",
}

# Cómo se llama cada preferencia dentro de ``airvault.json``. «Completar
# batch» conserva el nombre que ya tenía escrito en las instalaciones.
_CLAVES = {
    SUBIR: "auto_subir",
    INDEXAR: "auto_indexar",
    COMPLETAR: "completar_batch",
}

# Lo que dice cada casilla y qué se explica al pasar por encima.
ETIQUETAS = {
    SUBIR: "Subir a AirVault",
    INDEXAR: "Indexar páginas",
    COMPLETAR: "Completar batch",
}

AYUDAS = {
    SUBIR: (
        "Sube los batches a Quick Upload al terminar la exportación y espera "
        "a que AirVault los deje listos."
    ),
    INDEXAR: (
        "Escribe los datos de cada batch en cuanto está listo, sin esperar a "
        "los demás. También borra las páginas separadoras."
    ),
    COMPLETAR: (
        "Al terminar de escribir, da el batch por terminado y lo manda a Web "
        "Search."
    ),
}

class OpcionesAutomatizacion(QObject):
    """Los pasos elegidos, con memoria portable y una sola copia viva.

    La comparten la ventana principal y todas las de AirVault: «Completar
    batch» se ve marcado igual en los dos sitios porque es el mismo valor,
    no dos casillas que había que acordarse de igualar.
    """

    cambiado = Signal(str, bool)

    def __init__(self, raiz: Path | str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ruta = Path(raiz) / AIRVAULT_FILENAME
        config = AirVaultConfig.load(self._ruta)
        self._valores = {
            SUBIR: bool(config.auto_subir),
            INDEXAR: bool(config.auto_indexar),
            # Sin preferencia guardada no se inventa ninguna ni se escribe
            # el archivo: quien nunca tocó la casilla la encuentra apagada.
            COMPLETAR: bool(config.completar_batch),
        }

    # ── consulta ───────────────────────────────────────────────────

    def valor(self, paso: str) -> bool:
        """Si ese paso se va a hacer. La espera va dentro de subir."""
        return self._valores[SUBIR if paso == ESPERAR else paso]

    @property
    def subir(self) -> bool:
        return self._valores[SUBIR]

    @property
    def indexar(self) -> bool:
        return self._valores[INDEXAR]

    @property
    def completar(self) -> bool:
        return self._valores[COMPLETAR]

    # ── cambio ─────────────────────────────────────────────────────

    def fijar(self, paso: str, marcado: bool) -> None:
        """Cambia un paso, arrastra la cadena y lo deja guardado."""
        marcado = bool(marcado)
        if paso not in self._valores:
            raise KeyError(f"«{paso}» no es un paso que se elija")
        if self._valores[paso] == marcado:
            return
        cambios = {paso: marcado}
        if paso in CADENA:
            posicion = CADENA.index(paso)
            arrastrados = (
                # Marcar uno enciende todo lo que hace falta antes.
                CADENA[:posicion] if marcado
                # Apagarlo apaga lo que ya no tiene sobre qué trabajar.
                else CADENA[posicion + 1:]
            )
            cambios.update({
                otro: marcado for otro in arrastrados
                if self._valores[otro] != marcado
            })
        self._valores.update(cambios)
        guardar_preferencias(
            self._ruta,
            **{_CLAVES[nombre]: valor for nombre, valor in cambios.items()},
        )
        for nombre, valor in cambios.items():
            self.cambiado.emit(nombre, valor)


class MenuAutomatizacion(MultiSelectMenu):
    """Opciones marcables de la cadena automática en un menú estándar."""

    def __init__(
        self,
        opciones: OpcionesAutomatizacion,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._opciones = opciones
        # Sin esto las explicaciones de cada paso no salen: un menú no
        # enseña las ayudas de sus acciones a menos que se le pida.
        self.setToolTipsVisible(True)

        self._acciones: dict[str, object] = {}
        for paso in PASOS:
            accion = self.addAction(ETIQUETAS[paso])
            accion.setCheckable(True)
            accion.setChecked(opciones.valor(paso))
            accion.setToolTip(AYUDAS[paso])
            accion.toggled.connect(
                lambda marcado, paso=paso: self._opciones.fijar(paso, marcado)
            )
            self._acciones[paso] = accion

        opciones.cambiado.connect(self._refrescar)

    def accion(self, paso: str):
        """La entrada de un paso; la usan las pruebas y las ventanas."""
        return self._acciones[paso]

    def abrir_sobre(self, boton: QWidget) -> None:
        """Despliega la lista pegada al botón que la pidió.

        ``popup`` la coloca sola dentro de la pantalla: si abajo no queda
        alto, la sube por encima del botón en vez de recortarla.
        """
        self.popup(boton.mapToGlobal(boton.rect().bottomLeft()))

    def _refrescar(self, paso: str, marcado: bool) -> None:
        """Refleja lo que cambió en otro sitio sin volver a anunciarlo."""
        accion = self._acciones.get(paso)
        if accion is None or accion.isChecked() == marcado:
            return
        with QSignalBlocker(accion):
            accion.setChecked(marcado)


# ── hasta dónde llegó ──────────────────────────────────────────────
#
# El menú dice hasta dónde se va a llegar. Esto dice hasta dónde se llegó,
# que no es lo mismo y hasta ahora no se veía en ninguna parte: la ventana
# principal soltaba la cadena al exportar y lo que seguía pasaba en la de
# AirVault, así que quien miraba la principal no sabía si la entrega había
# terminado de subirse, se había quedado esperando o se había cortado.

PENDIENTE = "pendiente"
EN_CURSO = "en_curso"
HECHO = "hecho"
CORTADO = "cortado"
# El paso no está marcado en «Automatización…»: no se va a hacer, y no
# tenerlo en cuenta al contar es lo que hace que «5 de 5» signifique algo.
OMITIDO = "omitido"


class CadenaAutomatica(QWidget):
    """Los ocho pasos en una línea, cada uno con lo que le pasó.

    Va debajo de la barra de progreso porque cuenta lo mismo que ella pero
    a otra escala: la barra dice cuánto falta del paso en curso, y esta,
    cuántos pasos faltan de la entrega. Los pasos apagados son los que no
    se eligieron; los demás van cambiando de color según ocurren.

    Los cuatro últimos ocurren en la ventana de AirVault, así que los marca
    esa ventana a través de la principal. Sin eso, la línea se quedaba en
    «Exportar» y no había forma de saber desde aquí si el batch llegó.
    """

    @staticmethod
    def _color(estado: str) -> str:
        """El color de un estado, en los tonos del tema puesto ahora.

        Gris del texto de ayuda, el verde y el rojo de los estados de panel, y
        el azul de la selección. No se estrena ningún color. Va como función
        porque los tres primeros cambian con el tema; el de la selección no,
        que lo resuelve QSS solo contra la paleta de la aplicación.
        """
        c = paleta()
        return {
            PENDIENTE: c.TEXT_TERTIARY,
            OMITIDO: c.TEXT_DISABLED,
            EN_CURSO: "palette(highlight)",
            HECHO: c.STATUS_OK,
            CORTADO: c.STATUS_ERROR,
        }[estado]

    _AYUDAS = {
        PENDIENTE: "todavía no empezó",
        OMITIDO: "no está marcado en el menú de «Automático»; no se hace",
        EN_CURSO: "en curso",
        HECHO: "terminado",
        CORTADO: "se cortó aquí",
    }

    def __init__(
        self,
        opciones: OpcionesAutomatizacion,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._opciones = opciones
        self._estados: dict[str, str] = {}
        self._etiquetas: dict[str, QLabel] = {}

        fila = QHBoxLayout(self)
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(8)
        # Ocho nombres seguidos suman más ancho que muchos de los cuadros
        # de arriba, y el ancho mínimo de la ventana es el del contenido más
        # ancho que tenga: sin esto, esta línea decidía sola que la ventana
        # no se puede abrir en una pantalla de 1024. Es un rótulo, no un
        # control: si no cabe entero, se recorta por la derecha y el resto
        # sigue en el tooltip de cada paso.
        fila.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        for indice, paso in enumerate(RECORRIDO):
            if indice:
                flecha = QLabel("›")
                pintar_del_tema(
                    flecha, lambda: f"color: {self._color(OMITIDO)};"
                )
                fila.addWidget(flecha)
            etiqueta = QLabel(NOMBRES_CORTOS[paso])
            etiqueta.setAccessibleName(f"Paso {NOMBRES_CORTOS[paso]}")
            fila.addWidget(etiqueta)
            self._etiquetas[paso] = etiqueta
        fila.addStretch(1)

        opciones.cambiado.connect(lambda *_args: self.reiniciar())
        self.reiniciar()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - API Qt
        """Alto sí, ancho no: el ancho lo deciden los cuadros de arriba."""
        return QSize(0, super().minimumSizeHint().height())

    # ── consulta ───────────────────────────────────────────────────

    def elegido(self, paso: str) -> bool:
        """Si ese paso se va a hacer con las opciones de ahora.

        «Esperar» no tiene casilla propia: se hace siempre que se suba,
        porque es lo que convierte la carga en un batch listo para indexar.
        """
        if paso in (PREPROCESAR, PROCESAR, EXPORTAR):
            return True
        return self._opciones.valor(paso)

    def estado(self, paso: str) -> str:
        return self._estados[paso]

    def resumen(self) -> str:
        """Una línea con hasta dónde llegó, para la bitácora y las pruebas."""
        elegidos = [paso for paso in RECORRIDO if self.elegido(paso)]
        hechos = sum(1 for paso in elegidos if self._estados[paso] == HECHO)
        cortado = next(
            (paso for paso in RECORRIDO if self._estados[paso] == CORTADO),
            "",
        )
        if cortado:
            return (
                f"Automático: se cortó en «{NOMBRES_CORTOS[cortado]}» "
                f"({hechos} de {len(elegidos)} pasos)"
            )
        if hechos == len(elegidos):
            return "Automático: completo"
        en_curso = next(
            (paso for paso in RECORRIDO if self._estados[paso] == EN_CURSO),
            "",
        )
        if en_curso:
            return (
                f"Automático: {NOMBRES_CORTOS[en_curso].lower()} "
                f"({hechos} de {len(elegidos)} pasos)"
            )
        return f"Automático: {hechos} de {len(elegidos)} pasos"

    # ── cambio ─────────────────────────────────────────────────────

    def reiniciar(self) -> None:
        """Deja la línea como al empezar, con lo elegido de ahora."""
        self._estados = {
            paso: PENDIENTE if self.elegido(paso) else OMITIDO
            for paso in RECORRIDO
        }
        self._repintar()

    def marcar(self, paso: str, estado: str) -> None:
        """Anota lo que le pasó a un paso y arrastra lo que implica.

        Empezar un paso da por terminados los anteriores que estaban en
        curso: la cadena avanza en un solo sentido y no hay dos pasos
        corriendo a la vez, así que enterarse del cuarto es enterarse de
        que el tercero acabó. Hace falta porque los pasos de AirVault
        llegan desde otra ventana y alguno puede perderse por el camino.
        """
        if paso not in self._estados:
            return
        if estado == EN_CURSO:
            for anterior in RECORRIDO[: RECORRIDO.index(paso)]:
                if self._estados[anterior] == EN_CURSO:
                    self._estados[anterior] = HECHO
        self._estados[paso] = estado
        self._repintar()

    def cortar(self) -> None:
        """La cadena se soltó: el paso que estuviera en curso se cortó."""
        for paso in RECORRIDO:
            if self._estados[paso] == EN_CURSO:
                self._estados[paso] = CORTADO
        self._repintar()

    def _repintar(self) -> None:
        for paso, etiqueta in self._etiquetas.items():
            estado = self._estados[paso]
            peso = "600" if estado in (EN_CURSO, CORTADO) else "400"
            # Anotada y no escrita a secas: el paso se queda con su estado
            # hasta que la cadena avanza, y al cambiar de tema hay que volver
            # a resolver el color de ese mismo estado.
            pintar_del_tema(
                etiqueta,
                lambda estado=estado, peso=peso: (
                    f"color: {self._color(estado)}; font-weight: {peso};"
                ),
            )
            etiqueta.setToolTip(
                f"{NOMBRES_CORTOS[paso]}: {self._AYUDAS[estado]}"
            )
