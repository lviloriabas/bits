"""La ventana dice en el registro qué batches sobran en Web Index."""

from app.airvault.copias_en_index import COMPLETADO, EN_INDEX, Copia, Repetido, RevisionDeIndex
from app.airvault.flujo import BUSCANDO, INDEXADO, EstadoParte
from app.gui.airvault_window import TrabajoAirVaultWorker
from tests.airvault_fake import ClienteFalso, lote
from tests.test_airvault_copias_en_index import NOMBRE, PAGINAS, contenido
from tests.test_airvault_copias_en_index import trabajo as trabajo_con_batch
from tests.test_airvault_fin import preparar
from tests.test_gui_airvault_continuidad import anotado, cola


def revision(*copias, repetidos=(), ids=()):
    return RevisionDeIndex(ids=frozenset(ids), copias=list(copias), repetidos=list(repetidos))


def copia(batch_id="003TJF", original="003TIH", situacion=EN_INDEX, nombre="subido"):
    return Copia(lote(batch_id, f"{nombre}-Batch", 7), nombre, original, situacion)


def comprobado(ventana, web_index):
    ventana._al_comprobar({
        "estados": list(ventana._estados), "planes": {}, "partes": [],
        "recuperados": [], "cliente": object(), "acotado": True,
        "web_index": web_index,
    })


def test_las_copias_se_anotan_cuando_cambian_y_no_en_cada_vuelta(app, tmp_path):
    ventana, _trabajos = cola(tmp_path, [("subido", BUSCANDO)])

    comprobado(ventana, revision(copia()))
    texto = anotado(ventana)
    assert "En Web Index hay 1 batch repetido que se debe borrar:" in texto
    assert "003TJF «subido-Batch», 7 páginas" in texto
    assert "que sigue en Web Index como 003TIH. Se debe borrar." in texto

    comprobado(ventana, revision(copia()))
    # Sin respuesta de la cola se queda lo último que se supo.
    comprobado(ventana, None)
    assert anotado(ventana).count("batch repetido que se debe borrar") == 1

    otra = copia("003TJI", "003THZ", COMPLETADO, nombre="otro")
    comprobado(ventana, revision(copia(), otra))
    assert "En Web Index hay 2 batches repetidos que se deben borrar:" in anotado(ventana)
    assert "que ya está completado como 003THZ" in anotado(ventana)

    # Abierta por alguien no se lee, pero sigue en la cola: no se anuncia
    # como borrada ni, al soltarla, como nueva.
    comprobado(ventana, revision(otra, ids={"003TJF", "003TJI"}))
    comprobado(ventana, revision(copia(), otra, ids={"003TJF", "003TJI"}))
    assert "Ya no sobra" not in anotado(ventana)
    assert anotado(ventana).count("se deben borrar:") == 1

    comprobado(ventana, revision(otra, ids={"003TJI"}))
    assert "Ya no sobra en Web Index:\n003TJF «subido-Batch»" in anotado(ventana)
    assert anotado(ventana).count("se deben borrar:") == 1
    assert ventana._copias_por_borrar() == 1


def test_el_mismo_batch_de_otro_equipo_pide_borrar_uno_de_los_dos(app, tmp_path):
    ventana, _trabajos = cola(tmp_path, [("subido", BUSCANDO)])
    nombre = "DP | BITS 12 OCT 2026 10 41 REVISAR"
    dos = Repetido(nombre, (lote("003TIO", nombre, 467), lote("003TIP", nombre, 467)))

    comprobado(ventana, revision(repetidos=[dos]))

    texto = anotado(ventana)
    assert "En Web Index hay 1 batch repetido que se debe borrar:" in texto
    assert f"003TIO y 003TIP «{nombre}», 467 páginas cada uno" in texto
    assert "Se debe borrar el que no lleve la barra lateral del equipo que lo subió." in texto

    # Borrado uno, el otro ya no sobra.
    comprobado(ventana, revision(ids={"003TIO"}))
    assert f"Ya no sobra en Web Index:\n«{nombre}» (003TIO y 003TIP)" in anotado(ventana)
    assert ventana._copias_por_borrar() == 0


def test_el_final_del_proceso_recuerda_lo_que_queda_por_borrar(app, tmp_path):
    ventana, _t = preparar(app, tmp_path)
    ventana._anotar_web_index(revision(copia()))

    ventana._al_terminar()

    assert "Todo lo solicitado se completó" in ventana.resumen.text()
    assert "En Web Index queda 1 batch repetido por borrar" in ventana.resumen.text()


def test_la_revision_lee_la_cola_entera_con_todos_los_manifiestos(tmp_path):
    t = trabajo_con_batch(tmp_path, completado=True)
    cola_web = [lote("003TJF", f"{NOMBRE}-Batch", PAGINAS)]
    cliente = ClienteFalso(lotes=cola_web, paginas_por_lote={"003TJF": contenido()})
    # El manifiesto completado no viaja en la tanda que se revisa, pero sí
    # cuenta para reconocer su copia.
    worker = TrabajoAirVaultWorker("comprobar", {"trabajos": [], "todos_los_trabajos": [t]})

    hallado = worker._revisar_web_index(cliente)

    assert "" in cliente.filtros
    assert [(c.batch_id, c.situacion) for c in hallado.copias] == [("003TJF", COMPLETADO)]
    assert ("copia", "003TJF", str(t.carpeta), PAGINAS) in worker.estado["copias_vistas"]


def test_si_la_cola_entera_no_llega_la_revision_sigue_sin_copias(tmp_path):
    class SinCola(ClienteFalso):
        def listar_lotes(self, filtro=""):
            raise RuntimeError("AirVault rechazo /index/Batch/GetBatches: 500")

    worker = TrabajoAirVaultWorker("comprobar", {"trabajos": []})

    assert worker._revisar_web_index(SinCola()) is None
    assert worker._revisar_web_index(object()) is None


def test_el_revisar_indexado_que_sale_de_web_index_se_avisa_una_vez(app, tmp_path):
    ventana, (revisar, otro) = cola(tmp_path, [("revisar", INDEXADO), ("otro", INDEXADO)])
    for t, batch_id in ((revisar, "003TIJ"), (otro, "003TIK")):
        t.manifiesto.batch_id = batch_id
    revisar.manifiesto.solo_subir = True
    ventana.completar_check.setChecked(True)
    ventana.ocultar_indexados_check.setChecked(False)
    ventana._estados = [EstadoParte(revisar, INDEXADO), EstadoParte(otro, INDEXADO)]

    ventana._anotar_salidos_de_index(revision(ids={"003TIK"}))
    ventana._anotar_salidos_de_index(revision(ids=set()))

    texto = anotado(ventana)
    assert texto.count("ya no está en Web Index") == 1
    assert "«revisar»" in texto
    # El que el programa completa solo no entra: su cierre ya lo comprueba.
    assert "«otro»" not in texto
    assert "«Verificar subidas en Web Search»" in texto


def test_el_revisar_completado_a_mano_queda_completado_al_revisarlo_en_web_search(app, tmp_path):
    """Lo completa una persona; la barra lo dice en cuanto Web Search lo confirma."""
    from app.airvault.flujo import COMPLETADO as ESTADO_COMPLETADO
    from app.airvault.flujo import estado_local
    from app.airvault.model import EstadoEtapa
    from app.gui.airvault_window import AirVaultWindow
    from tests.test_airvault_chequeo_completo import comprobar as buscar_en_websearch
    from tests.test_airvault_confirmacion import caso

    t, buscador, _filas, _consultas = caso(tmp_path, 50)
    t.manifiesto.solo_subir = True
    t.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    t.manifiesto.etapa("verificar").marcar(EstadoEtapa.HECHA)
    t.guardar()
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [t]
    ventana._estados = [estado_local(t)]
    assert ventana._estados[0].estado == INDEXADO

    _worker, avisos = buscar_en_websearch(t, buscador, app)
    for aviso in avisos:
        ventana._al_buscar_websearch(aviso)

    assert ventana._estados[0].estado == ESTADO_COMPLETADO
    assert estado_local(t).estado == ESTADO_COMPLETADO
