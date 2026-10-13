"""Memoria de todo lo que el programa ya mando a AirVault, por bitacora.

El registro de la entrega (:mod:`app.airvault.registro`) recuerda un reparto
y vive dentro de la carpeta de esa ejecucion, asi que se va con ella: al
borrar el registro local para empezar de nuevo, o al procesar los mismos
escaneos otra vez en otra carpeta, esa memoria desaparece y con ella la
unica prueba de que aquellas bitacoras ya viajaron.

Este libro vive una sola vez, junto a los trabajos, y sobrevive a cambios de
reparto. Una eliminacion expresa si retira de el el batch correspondiente,
porque conservarlo bloquearia como posible duplicado algo que la persona ya
pidio olvidar. No se guia por nombres de batch, sino por **numeros de
bitacora**, que son lo que identifica a un documento igual en el escaneo, en
el CSV, en Quick Upload y en Web Search.

Con eso responde la pregunta que ninguna consulta a la cola puede responder
cuando el batch ya se completo: «esta bitacora, ¿ya la mande yo alguna
vez?». Si la respuesta es que si y fue en otro batch, subirla otra vez la
publicaria por duplicado.

No sustituye a Web Search, la complementa. El libro sabe lo que hizo este
programa; Web Search sabe lo que hay publicado, lo subiera quien lo
subiera. El libro siempre esta disponible y decide solo; Web Search puede
no responder, y entonces queda el libro.
"""

from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence

from pydantic import BaseModel, Field

from app.airvault.registro import apartar_ilegible, leer_json_de_memoria

LIBRO_FILENAME = "bitacoras-enviadas.json"

_ESCRITURA = threading.Lock()

# Cuantos numeros ya completados se ofrecen como control positivo de Web
# Search. Con un puñado basta: se prueban en orden hasta que uno responda,
# y pedir mas solo alarga el descubrimiento cuando la ruta no es esa.
CONTROLES_POR_DEFECTO = 5


def _entrega(csv_origen: str) -> str:
    """La entrega a la que pertenece un envio, normalizada para comparar."""
    texto = str(csv_origen or "").strip()
    if not texto:
        return ""
    try:
        return str(Path(texto).resolve()).casefold()
    except OSError:
        return texto.casefold()


class Bitacora(BaseModel):
    """Una bitacora tal como hay que preguntar por ella al libro."""

    numero: str
    archivo: str = ""
    pagina: int = 0


class Envio(BaseModel):
    """Una bitacora que salio hacia AirVault dentro de un batch."""

    nombre_batch: str = ""
    batch_id: str = ""
    # Carpeta del trabajo que la llevo. Es lo que distingue «la mande dos
    # veces» de «la estoy mirando otra vez en el mismo sitio».
    carpeta: str = ""
    csv_origen: str = ""
    archivo: str = ""
    pagina: int = 0
    cuando: str = ""
    completado: bool = False


class Libro(BaseModel):
    """Todos los envios conocidos, por numero de bitacora."""

    version: int = 1
    actualizado: str = ""
    envios: Dict[str, List[Envio]] = Field(default_factory=dict)

    def de(self, numero: str) -> List[Envio]:
        return list(self.envios.get(str(numero).strip(), ()))

    def controles(self, cuantos: int = CONTROLES_POR_DEFECTO) -> List[str]:
        """Numeros que tienen que estar en Web Search si Web Search sirve.

        Solo los de batches completados: completar es lo que manda un batch
        a Web Search. Uno indexado pero todavia en la cola no esta alli, y
        usarlo de control daria la ruta buena por mala.
        """
        elegidos: List[str] = []
        for numero, envios in self.envios.items():
            if any(envio.completado for envio in envios):
                elegidos.append(numero)
            if len(elegidos) >= max(0, cuantos):
                break
        return elegidos

    def repetidas(
        self,
        bitacoras: Sequence["Bitacora"],
        carpeta: str,
        csv_origen: str = "",
    ) -> List[str]:
        """De esas bitacoras, las que ya viajaron en **otro** batch.

        La identidad cambia segun de donde venga el envio anterior, y no es
        un detalle: usar una sola deja pasar duplicados o inventa otros.

        Dentro de **la misma entrega** una bitacora es su pagina de origen
        (el archivo escaneado y el numero de pagina). Dos paginas distintas
        pueden traer el mismo numero de bitacora, porque leerlo mal pasa, y
        darlas por la misma dejaria sin subir una pagina que nadie subio.

        Entre **entregas distintas** esa pagina de origen no significa nada:
        los mismos escaneos procesados otra vez producen otros archivos y
        otra numeracion. Ahi lo unico que identifica al documento es su
        numero de bitacora, y es justamente el caso que no tenia defensa:
        reprocesar unos escaneos ya subidos y volver a mandarlos.

        Se compara por carpeta del trabajo y no por nombre de batch: al
        rehacer un reparto los nombres se reutilizan, y dos batches
        distintos pueden llamarse igual en momentos distintos.
        """
        propia = str(carpeta or "").casefold()
        entrega = _entrega(csv_origen)
        repetidas: List[str] = []
        for bitacora in bitacoras:
            numero = str(bitacora.numero or "").strip()
            if not numero:
                continue
            for envio in self.envios.get(numero, ()):
                if str(envio.carpeta or "").casefold() == propia:
                    continue
                if _entrega(envio.csv_origen) == entrega and not (
                    str(envio.archivo or "").casefold()
                    == str(bitacora.archivo or "").casefold()
                    and int(envio.pagina or 0) == int(bitacora.pagina or 0)
                ):
                    continue
                repetidas.append(numero)
                break
        return repetidas


def ruta_libro(raiz: Path | str) -> Path:
    return Path(raiz) / LIBRO_FILENAME


def leer(raiz: Path | str) -> Libro:
    """El libro, o uno vacio si todavia no hay ninguno.

    Un archivo ilegible se trata como ausente: esta memoria protege, y una
    proteccion que no se puede leer no puede ademas impedir trabajar.
    """
    ruta = ruta_libro(raiz)
    datos = leer_json_de_memoria(ruta)
    if datos is None:
        return Libro()
    try:
        return Libro.model_validate(datos)
    except ValueError as exc:
        apartar_ilegible(ruta, exc)
        return Libro()


def guardar(libro: Libro, raiz: Path | str) -> Path:
    """Escritura atomica, como la del manifiesto y la del registro."""
    destino = ruta_libro(raiz)
    destino.parent.mkdir(parents=True, exist_ok=True)
    libro.actualizado = datetime.now().isoformat(timespec="seconds")
    contenido = libro.model_dump_json(indent=2)
    descriptor, temporal = tempfile.mkstemp(
        dir=str(destino.parent), prefix=".bitacoras-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            archivo.write(contenido)
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(temporal, destino)
    except BaseException:
        Path(temporal).unlink(missing_ok=True)
        raise
    return destino


def _envios_de(trabajo) -> Dict[str, Envio]:
    """Lo que este batch anota de si mismo, por numero de bitacora."""
    manifiesto = trabajo.manifiesto
    # Solo el cierre hecho: el omitido (paginas en amarillo, separadoras sin
    # borrar) deja el batch en la cola, y sus bitacoras no sirven de control
    # de lo que ya esta en Web Search.
    completado = manifiesto.cierre_hecho()
    momento = datetime.now().isoformat(timespec="seconds")
    anotados: Dict[str, Envio] = {}
    for registro in manifiesto.registros:
        numero = str(registro.log_number or "").strip()
        if registro.es_separador or not numero:
            continue
        anotados[numero] = Envio(
            nombre_batch=manifiesto.nombre_batch,
            batch_id=str(manifiesto.batch_id or ""),
            carpeta=str(trabajo.carpeta),
            csv_origen=str(manifiesto.csv_origen or ""),
            archivo=str(registro.archivo_origen or ""),
            pagina=int(registro.pagina_origen or 0),
            cuando=momento,
            completado=completado,
        )
    return anotados


def anotar(raiz: Path | str, trabajos: Sequence) -> Libro:
    """Deja constancia de las bitacoras que estos batches ya se llevaron.

    Solo de los que llegaron a Quick Upload: antes de eso no hay nada
    enviado y anotarlo convertiria un reparto descartado en un falso
    duplicado del reparto siguiente.

    Un envio ya anotado se actualiza en su sitio (por carpeta) en vez de
    sumarse otra vez: reanudar una ejecucion no la duplica en el libro, y
    completarla despues solo cambia esa marca.
    """
    with _ESCRITURA:
        libro = leer(raiz)
        cambio = False
        for trabajo in trabajos:
            if not trabajo.manifiesto.etapa_hecha("subir"):
                continue
            for numero, envio in _envios_de(trabajo).items():
                lista = libro.envios.setdefault(numero, [])
                propia = envio.carpeta.casefold()
                for indice, anterior in enumerate(lista):
                    if str(anterior.carpeta or "").casefold() == propia:
                        # Completado no retrocede: un batch cerrado sigue
                        # cerrado aunque el manifiesto se reinicie despues.
                        envio.completado = (
                            envio.completado or anterior.completado
                        )
                        envio.cuando = anterior.cuando or envio.cuando
                        lista[indice] = envio
                        break
                else:
                    lista.append(envio)
                cambio = True
        if cambio:
            guardar(libro, raiz)
        return libro


def olvidar(
    raiz: Path | str,
    carpetas: Sequence[Path | str],
    incluir_hijas: bool = False,
) -> Libro:
    """Retira del libro los batches locales eliminados por la persona.

    Esta es la contraparte de eliminar un manifiesto o una ejecución. Si el
    envío siguiera en el libro, una carga posterior de esas bitácoras se
    marcaría como posible duplicado por un batch que ya se pidió olvidar.
    """
    objetivos = [Path(carpeta) for carpeta in carpetas]

    def corresponde(envio: Envio) -> bool:
        propia = Path(str(envio.carpeta or ""))
        for objetivo in objetivos:
            if str(propia).casefold() == str(objetivo).casefold():
                return True
            if incluir_hijas:
                try:
                    if propia.resolve().is_relative_to(objetivo.resolve()):
                        return True
                except OSError:
                    continue
        return False

    with _ESCRITURA:
        libro = leer(raiz)
        cambio = False
        for numero in list(libro.envios):
            quedan = [
                envio for envio in libro.envios[numero]
                if not corresponde(envio)
            ]
            if len(quedan) == len(libro.envios[numero]):
                continue
            cambio = True
            if quedan:
                libro.envios[numero] = quedan
            else:
                del libro.envios[numero]
        if cambio:
            guardar(libro, raiz)
        return libro


def bitacoras_de(trabajo) -> List[Bitacora]:
    """Lo que hay que preguntarle al libro por cada bitacora del batch."""
    return [
        Bitacora(
            numero=str(registro.log_number).strip(),
            archivo=str(registro.archivo_origen or ""),
            pagina=int(registro.pagina_origen or 0),
        )
        for registro in trabajo.manifiesto.registros
        if not registro.es_separador and str(registro.log_number or "").strip()
    ]


def repetidas(raiz: Path | str, trabajo) -> List[str]:
    """Numeros de este batch que ya viajaron antes en otro batch."""
    return leer(raiz).repetidas(
        bitacoras_de(trabajo),
        str(trabajo.carpeta),
        str(trabajo.manifiesto.csv_origen or ""),
    )
