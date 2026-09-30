"""La cadena de la ventana de AirVault, de un botón hasta el final.

Las demás pruebas de la ventana miran cada paso por separado. Estas pulsan
un botón y dejan que la ventana encadene sola lo que haga falta (revisar,
subir, indexar, completar y volver a revisar) contra un AirVault simulado,
sin red. El hilo corre en línea: ``_lanzar`` ejecuta el trabajo en el acto
y cierra con ``_al_terminar``, que es lo que decide el paso siguiente.

El caso es el de una ejecución que se quedó a medias: el primer batch
terminado, el segundo subido y todavía armándose en AirVault, y el tercero
sin subir.
"""

from __future__ import annotations

import json

import pytest

from app.airvault import manifest as manifiestos
from app.airvault.flujo import Trabajo, ruta_indice_paginas
from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker
from app.gui.automatizacion import OpcionesAutomatizacion
from tests.airvault_fake import AirVaultSimulado, subida_simulada

NOMBRE = "BITS 18 AUG 2026 05 42"

CSV = (
    "﻿file,page,log_number,dup,disc,matricula,flight_number,"
    "pilot_signature,captain_signature,captain_license,"
    "technician_signature,date,time_ms\n"
    "Image_001.pdf,1,2312238,false,false,HP-1848CMP,472,true,true,true,"
    "true,2026/08/12,10372.0\n"
    "Image_001.pdf,2,2312239,false,false,HP-1848CMP,389,true,true,true,"
    "true,2026/08/13,11268.3\n"
    "Image_001.pdf,3,2312240,false,false,HP-1848CMP,390,true,true,true,"
    "true,2026/08/14,10987.1\n"
)


class SesionFalsa:
    """La sesión no se usa: la subida está simulada."""

    cancelada = False

    def reanudar(self):
        pass


def corrida(raiz):
    """Una ejecución exportada en tres archivos de entrega, uno por batch."""
    import pymupdf as fitz

    carpeta = raiz / "output" / NOMBRE
    (carpeta / "datos").mkdir(parents=True)
    csv = carpeta / "datos" / f"{NOMBRE}.CSV"
    csv.write_text(CSV, encoding="utf-8")
    (carpeta / "stats.json").write_text(
        json.dumps({"corrida": NOMBRE, "total_paginas": 3}), encoding="utf-8",
    )
    archivos = [f"parte-{numero}.pdf" for numero in (1, 2, 3)]
    for archivo in archivos:
        documento = fitz.open()
        documento.new_page()
        documento.save(str(carpeta / archivo))
        documento.close()
    ruta_indice_paginas(csv).write_text(
        json.dumps({"version": 2, "partes": [
            {"pdf": archivo,
             "paginas": [{"archivo": "Image_001.pdf", "pagina": numero}]}
            for numero, archivo in enumerate(archivos, start=1)
        ]}),
        encoding="utf-8",
    )
    return csv


@pytest.fixture
def cadena(app, tmp_path, monkeypatch):
    """La ventana sobre la ejecución, con el hilo corriendo en línea."""
    cliente = AirVaultSimulado()
    # El segundo batch no termina de armarse mientras dura la primera
    # tanda: es la carga que queda en vuelo.
    monkeypatch.setattr(
        Trabajo, "subir",
        subida_simulada(cliente, {f"DP | {NOMBRE} -2": 10_000}),
    )
    # Lo que AirVault seguía armando cada vez que salió una carga. Tiene que
    # estar siempre vacío: con dos en vuelo, AirVault las junta.
    cliente.en_vuelo_al_subir = []
    publicar = cliente.publicar

    def publicar_anotando(nombre, paginas, vueltas=2):
        cliente.en_vuelo_al_subir.append([
            batch_id for batch_id, faltan in cliente.faltan_vueltas.items()
            if faltan
        ])
        return publicar(nombre, paginas, vueltas)

    cliente.publicar = publicar_anotando
    ventana = AirVaultWindow(tmp_path, OpcionesAutomatizacion(tmp_path))
    ventana.completar_check.setChecked(True)
    ventana.fijar_corrida(corrida(tmp_path))
    modos: list[str] = []
    fallos: list[str] = []

    def lanzar(modo, estado):
        modos.append(modo)
        assert len(modos) < 60, f"la cadena no termina: {modos}"
        ventana._worker_filtrado = ventana.solo_ejecucion_check.isChecked()
        if modo != "resubir":
            estado["forzados"] = []
        estado.update(cliente=cliente, sesion=SesionFalsa(), tanda_hecha=True)
        worker = TrabajoAirVaultWorker(modo, estado, ventana)

        def conectar():
            worker._detectar_pendientes()
            return cliente

        worker._conectar = conectar
        worker._dormir = cliente.avanzar
        worker.paso.connect(ventana._mostrar_paso)
        worker.subidas_actualizadas.connect(ventana._al_actualizar_subidas)
        worker.batch_encontrado.connect(ventana._al_batch_encontrado)
        worker.batch_indexado.connect(ventana._al_batch_indexado)
        worker.batch_indexando.connect(ventana._al_batch_indexando)
        worker.subido.connect(ventana._al_subir)
        worker.comprobado.connect(ventana._al_comprobar)
        worker.indexado.connect(ventana._al_indexar)
        worker.fallo.connect(fallos.append)
        worker.fallo.connect(ventana._al_fallar)
        worker.cancelado.connect(ventana._al_cancelar)
        ventana._worker = worker
        worker.run()
        ventana._al_terminar()

    monkeypatch.setattr(ventana, "_lanzar", lanzar)

    # La primera tanda: sube el primero, lo indexa y lo completa; sube el
    # segundo, que AirVault no termina de armar, y ahí se detiene para no
    # mandar otro archivo con una carga en vuelo.
    ventana._subir_a_mano()
    trabajos = sorted(ventana._trabajos, key=lambda t: t.manifiesto.parte)
    primero, segundo, tercero = trabajos
    assert primero.manifiesto.etapa_hecha("completar")
    assert segundo.manifiesto.etapa_hecha("subir")
    assert not segundo.manifiesto.batch_id
    assert not tercero.manifiesto.etapa_hecha("subir")
    assert [e for e in cliente.eventos if e[0] == "subir"] == [
        ("subir", f"DP | {NOMBRE} -1"),
        ("subir", f"DP | {NOMBRE} -2"),
    ]
    assert fallos == []

    # Después AirVault termina de armar el segundo.
    cliente.terminar_de_armar()
    modos.clear()
    yield ventana, cliente, trabajos, fallos, modos
    ventana.close()


def _todo_terminado(cliente, trabajos, fallos):
    assert fallos == []
    # Cada archivo se subió una sola vez.
    assert [e for e in cliente.eventos if e[0] == "subir"] == [
        ("subir", f"DP | {NOMBRE} -1"),
        ("subir", f"DP | {NOMBRE} -2"),
        ("subir", f"DP | {NOMBRE} -3"),
    ]
    # Lo que cuenta es lo que quedó en disco: «Subir» vuelve a cargar los
    # trabajos de la ejecución y los objetos de antes se quedan viejos.
    guardados = [manifiestos.cargar(t.carpeta) for t in trabajos]
    assert [m.batch_id for m in guardados] == ["003B1", "003B2", "003B3"]
    for manifiesto in guardados:
        assert manifiesto.etapa_hecha("verificar")
        assert manifiesto.etapa_hecha("completar")
    assert cliente.completados == ["003B1", "003B2", "003B3"]
    # Ninguna carga salió con otra todavía armándose en AirVault.
    assert all(en_vuelo == [] for en_vuelo in cliente.en_vuelo_al_subir)
    # El segundo se escribió mientras AirVault armaba el tercero, no al
    # terminar todas las cargas.
    eventos = cliente.eventos
    assert (
        eventos.index(("subir", f"DP | {NOMBRE} -3"))
        < eventos.index(("escribir", "003B2"))
        < eventos.index(("escribir", "003B3"))
    )


def test_subir_con_batches_ya_subidos_retoma_los_que_faltan(cadena):
    ventana, cliente, trabajos, fallos, _modos = cadena

    ventana._subir_a_mano()

    _todo_terminado(cliente, trabajos, fallos)


@pytest.mark.parametrize("reloj", [True, False], ids=["con-reloj", "sin-reloj"])
def test_revisar_en_airvault_termina_todo_lo_que_falta(cadena, reloj):
    """Sube lo que falta, indexa y completa, con o sin revisión periódica."""
    ventana, cliente, trabajos, fallos, modos = cadena
    ventana.auto_check.setChecked(reloj)

    ventana.boton_revisar.click()

    _todo_terminado(cliente, trabajos, fallos)
    assert modos[0] == "comprobar"


def test_continuar_pendiente_termina_todo_lo_que_falta(cadena):
    ventana, cliente, trabajos, fallos, _modos = cadena

    ventana._continuar_pendiente()

    _todo_terminado(cliente, trabajos, fallos)


def test_la_cadena_termina_y_no_queda_nada_por_hacer(cadena):
    """Al acabar, una revisión más no sube ni escribe nada."""
    ventana, cliente, trabajos, fallos, modos = cadena
    ventana.boton_revisar.click()
    eventos = list(cliente.eventos)
    modos.clear()

    ventana.boton_revisar.click()

    assert cliente.eventos == eventos
    assert modos == ["comprobar"]
    assert fallos == []
