"""Numero, ID y pasos de cada batch; consultas a Web Search que no se repiten."""

from types import SimpleNamespace

from PySide6.QtWidgets import QListWidgetItem

from app.airvault import registro as registro_entrega
from app.airvault.flujo import (BUSCANDO, COMPLETADO, INDEXADO, LISTO,
                                NO_ENCONTRADO, SIN_SUBIR, EstadoParte,
                                id_recordado)
from app.airvault.model import EstadoEtapa
from app.gui import airvault_window as modulo
from app.gui.airvault_window import AirVaultWindow
from app.gui.batches_sidebar import ROL_NUMERO
from test_airvault_rework import trabajo


def cola(app, tmp_path, estados):
    ventana = AirVaultWindow(tmp_path)
    trabajos = [trabajo(tmp_path, nombre) for nombre, _estado in estados]
    ventana._trabajos = trabajos
    ventana._estados = [EstadoParte(t, estado) for t, (_n, estado) in zip(trabajos, estados)]
    ventana._numeros.clear()
    ventana._pintar_lotes()
    return ventana, trabajos


def test_cada_batch_conserva_su_numero_aunque_cambien_los_filtros(app, tmp_path):
    ventana, trabajos = cola(app, tmp_path, [
        ("uno", COMPLETADO), ("dos", SIN_SUBIR), ("tres", INDEXADO), ("cuatro", SIN_SUBIR),
    ])
    assert [ventana.lotes.item(f).data(ROL_NUMERO) for f in range(4)] == [1, 2, 3, 4]
    assert ventana.lotes.item(1).toolTip().startswith("Batch 2: dos")
    ventana.ocultar_completados_check.setChecked(True)
    ventana.ocultar_indexados_check.setChecked(True)
    assert [ventana.lotes.item(f).data(ROL_NUMERO) for f in range(ventana.lotes.count())] == [2, 4]
    # El orden en que llegan los estados no mueve la numeracion ni la lista.
    ventana._estados.reverse()
    ventana._pintar_lotes()
    assert [p.nombre for p in ventana._partes_en_cola()] == ["dos", "cuatro"]
    nuevo = trabajo(tmp_path, "nuevo")
    ventana._estados.insert(0, EstadoParte(nuevo, SIN_SUBIR))
    ventana._pintar_lotes()
    assert ventana._numero_de(nuevo) == 5
    assert ventana._rotulo(trabajos[1]) == "Batch 2 «dos»"


def test_la_tarjeta_ensena_el_id_vigente_o_el_ultimo_que_tuvo(app, tmp_path):
    ventana, (con_id, sin_id, perdido) = cola(app, tmp_path, [
        ("con-id", LISTO), ("sin-id", SIN_SUBIR), ("perdido", NO_ENCONTRADO),
    ])
    con_id.manifiesto.batch_id = "003AAA"
    # El registro de la entrega recuerda el ID que el manifiesto ya no trae.
    perdido.manifiesto.batch_id = "003ZZZ"
    registro_entrega.anotar(perdido.carpeta, [perdido])
    perdido.manifiesto.batch_id = None
    assert id_recordado(perdido) == "003ZZZ"
    assert id_recordado(sin_id) == ""
    # La ventana lo busca una vez por batch; aqui se anoto despues de pintar.
    ventana._ids_recordados.clear()
    ventana._pintar_lotes()
    assert "ID 003AAA" in ventana.lotes.item(0).text()
    assert "ID" not in ventana.lotes.item(1).text()
    assert "último ID 003ZZZ" in ventana.lotes.item(2).text()
    # La celda del ID sigue diciendo solo el vigente: es el que se copia.
    assert ventana.lotes.item(2, 0).text() == ""


def test_la_lista_de_pasos_dice_lo_hecho_lo_que_corre_y_lo_pendiente(app, tmp_path):
    ventana, (subido, nuevo, listo) = cola(app, tmp_path, [
        ("subido", BUSCANDO), ("nuevo", SIN_SUBIR), ("listo", LISTO),
    ])
    ventana.completar_check.setChecked(True)
    assert ventana._pasos_de(ventana._estados[1]) == (
        [], [], ["subir", "detectar en AirVault", "indexar", "completar",
                 "confirmar en Web Search"],
    )
    ventana._indexando.add(str(listo.carpeta))
    hechos, en_proceso, pendientes = ventana._pasos_de(ventana._estados[2])
    assert (hechos, en_proceso) == (["subir", "detectar en AirVault"], ["indexar"])
    assert pendientes == ["completar", "confirmar en Web Search"]

    def anotado(encabezado):
        textos = [ventana.bitacora.item(f).text() for f in range(ventana.bitacora.count())]
        return next(texto for texto in reversed(textos) if encabezado in texto)

    ventana._anotar_plan(completo=True)
    texto = anotado("Pasos por batch")
    assert "Pasos por batch: 3 en la cola, 3 con pasos por hacer y 0 terminados." in texto
    assert "2. nuevo - Pendiente: subir" in texto
    assert "3. listo - En proceso: indexar. Pendiente: completar" in texto
    # Despues solo se anota el batch que cambio.
    cuantas = ventana.bitacora.count()
    ventana._anotar_plan()
    assert ventana.bitacora.count() == cuantas
    ventana._indexando.clear()
    ventana._estados[2] = EstadoParte(listo, INDEXADO)
    ventana._anotar_plan()
    cambio = anotado("Cambios en los pasos de la cola:")
    assert "3. listo" in cambio and "1. subido" not in cambio
    ventana._pintar_lotes()
    assert "Pendiente: completar, confirmar en Web Search." in ventana.lotes.item(2).toolTip()


def test_la_linea_viva_dice_el_batch_y_el_paso_en_curso(app, tmp_path, monkeypatch):
    ventana, (listo,) = cola(app, tmp_path, [("listo", LISTO)])
    listo.manifiesto.batch_id = "003AAA"
    hilo = SimpleNamespace(modo="indexar", estado={}, hay_que_parar=lambda: False)
    monkeypatch.setattr(ventana, "hilo", lambda: hilo)
    ventana._linea_viva = QListWidgetItem()
    ventana._pintar_linea_viva()
    assert ventana._linea_viva.text().startswith("Ahora: Escribiendo en AirVault los batches listos")
    ventana._mostrar_paso("Batch 003AAA: Escribiendo en AirVault", 3, 10)
    ventana._pintar_linea_viva()
    assert ventana._linea_viva.text().startswith(
        "Ahora: Batch 1 «listo»: Escribiendo en AirVault ("
    )
    assert "3 de 10" in ventana._linea_viva.text()
    assert "trabajando" not in ventana._linea_viva.text()


def test_una_ausencia_se_recupera_una_sola_vez_por_orden(app, tmp_path, monkeypatch):
    ventana, (ausente,) = cola(app, tmp_path, [("ausente", NO_ENCONTRADO)])
    encoladas = []
    monkeypatch.setattr(ventana, "_encolar", lambda modo, trabajos, _texto: encoladas.append(modo))
    ventana._websearch_resubidas[str(ausente.carpeta)] = ausente
    ventana._al_terminar_websearch()
    assert encoladas == ["recuperar_ausentes"]
    ventana._websearch_resubidas[str(ausente.carpeta)] = ausente
    ventana._al_terminar_websearch()
    assert encoladas == ["recuperar_ausentes"]
    assert str(ausente.carpeta) in ventana._websearch_recuperados
    # Pedirlo sobre la fila devuelve el intento.
    monkeypatch.setattr(ventana, "_iniciar_confirmacion", lambda trabajos, automatico: None)
    ventana._verificar_estas([ventana._estados[0]])
    assert not ventana._websearch_recuperados


def test_la_revision_automatica_se_detiene_si_nada_cambia(app, tmp_path, monkeypatch):
    ventana, _trabajos = cola(app, tmp_path, [("subido", BUSCANDO)])
    reloj = [1000.0]
    monkeypatch.setattr(modulo.time, "monotonic", lambda: reloj[0])
    ventana.auto_check.setChecked(True)
    ventana._ajustar_vigilancia()
    assert ventana._vigilando()
    reloj[0] += modulo.HORAS_SIN_CAMBIOS_ANTES_DE_PARAR * 3600 - 60
    ventana._ajustar_vigilancia()
    assert ventana._vigilando()
    reloj[0] += 120
    ventana._ajustar_vigilancia()
    assert not ventana._vigilando()
    assert "Revisión automática detenida" in ventana.resumen.text()
    # Un cambio en la cola o una orden devuelven el plazo entero.
    ventana._estados = [EstadoParte(ventana._estados[0].trabajo, SIN_SUBIR)]
    ventana._estados[0].trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    ventana._estados = [EstadoParte(ventana._estados[0].trabajo, BUSCANDO, "otra vuelta")]
    ventana._firma_vigilada = None
    ventana._ajustar_vigilancia()
    assert ventana._vigilando()
    ventana._parar_vigilancia()
