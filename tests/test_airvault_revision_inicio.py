"""La revision inicial conserva su alcance y cuenta batches en paralelo."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QListWidgetItem

from app.airvault.config import AirVaultConfig
from app.airvault.flujo import Trabajo, estado_local
from app.airvault.model import EstadoEtapa, Manifiesto, Registro
from app.gui import airvault_window as modulo


class WorkerFalso(QObject):
    """Simula los limites de cada worker sin red ni hilos reales."""

    def __init__(self, modo, estado, parent=None):
        super().__init__(parent)
        self.modo = modo
        self.estado = estado
        self.corriendo = False
        for nombre in (
            "paso", "subidas_actualizadas", "batch_encontrado", "batch_indexado",
            "batch_indexando", "subido", "comprobado", "buscado", "indexado",
            "fallo", "cancelado", "finished",
        ):
            setattr(self, nombre, SimpleNamespace(connect=lambda _callback: None))

    def start(self):
        self.corriendo = True

    def isRunning(self):
        return self.corriendo

    def hay_que_parar(self):
        return False

    def cancelar(self):
        self.corriendo = False

    def wait(self, _limite):
        return True


def crear_trabajo(raiz, nombre, subida=None, batch_id=None, completado=False):
    manifiesto = Manifiesto(
        job_id=nombre,
        nombre_batch=nombre,
        csv_origen=str(raiz / nombre / "reporte.csv"),
        batch_id=batch_id,
        registros=[Registro(seq=1, log_number="2000001")],
    )
    if subida is not None:
        manifiesto.etapa("subir").marcar(subida)
    if completado:
        manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
    return Trabajo(AirVaultConfig(), raiz / "output" / "airvault" / nombre, manifiesto)


@pytest.fixture
def ventana(app, tmp_path, monkeypatch):
    monkeypatch.setattr(modulo, "TrabajoAirVaultWorker", WorkerFalso)
    return modulo.AirVaultWindow(tmp_path)


def preparar(ventana, trabajos):
    ventana._trabajos = trabajos
    ventana._estados = [estado_local(t) for t in trabajos]
    ventana._estado["trabajos"] = trabajos
    ventana._estado["sesion"] = SimpleNamespace(cancelada=False)
    ventana._pintar_lotes()
    return ventana._estado


def test_inicio_revisa_subidos_pendientes_y_deja_nuevos_fuera(ventana, tmp_path):
    anterior = crear_trabajo(tmp_path, "anterior", EstadoEtapa.HECHA)
    nuevo = crear_trabajo(tmp_path, "nuevo")
    terminado = crear_trabajo(tmp_path, "terminado", EstadoEtapa.HECHA, completado=True)
    preparar(ventana, [anterior, nuevo, terminado])
    ventana._lanzar("subir", ventana._estado)
    ventana._confirmar_solo()
    assert ventana._worker.isRunning()
    assert ventana._worker_websearch.isRunning()
    assert ventana._worker_websearch.estado["buscar_trabajos"] == [anterior]
    assert ventana._worker_websearch.estado is not ventana._estado

    nuevo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    assert nuevo not in ventana._pendientes_websearch_automaticos()


def test_otra_accion_vuelve_a_revisar_aunque_la_firma_no_cambie(ventana, tmp_path):
    anterior = crear_trabajo(tmp_path, "anterior", EstadoEtapa.HECHA)
    preparar(ventana, [anterior])
    ventana._lanzar("comprobar", ventana._estado)
    ventana._confirmar_solo()
    assert not ventana._pendientes_websearch_automaticos()
    ventana._worker_websearch.corriendo = False
    ventana._al_terminar_websearch()
    ventana._worker.corriendo = False
    ventana._al_terminar()

    ventana._lanzar("comprobar", ventana._estado)
    assert ventana._websearch_revisados == {}
    ventana._confirmar_solo()
    assert ventana._worker_websearch.estado["buscar_trabajos"] == [anterior]


def test_cadena_conserva_foto_y_no_incorpora_recien_subidos(ventana, tmp_path, monkeypatch):
    anterior = crear_trabajo(tmp_path, "anterior", EstadoEtapa.HECHA)
    nuevo = crear_trabajo(tmp_path, "nuevo")
    preparar(ventana, [anterior, nuevo])
    ventana._lanzar("subir", ventana._estado)
    ventana._confirmar_solo()
    firma = dict(ventana._websearch_revisados)
    nuevo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    ventana._worker.corriendo = False
    ventana._comprobar_al_terminar = True
    monkeypatch.setattr(ventana, "_comprobar", lambda: ventana._lanzar("comprobar", ventana._estado))
    ventana._al_terminar()

    assert ventana._worker.modo == "comprobar"
    assert ventana._websearch_proceso_activo
    assert ventana._websearch_inicio == {str(anterior.carpeta)}
    assert ventana._websearch_revisados == firma
    assert not ventana._pendientes_websearch_automaticos()


def test_primera_revision_es_obligatoria_con_revisar_cada_apagado(ventana, tmp_path):
    anterior = crear_trabajo(tmp_path, "anterior", EstadoEtapa.HECHA)
    preparar(ventana, [anterior])
    ventana.auto_check.setChecked(False)
    ventana._lanzar("indexar", ventana._estado)
    assert ventana._confirmador.isActive()
    assert ventana._confirmador.interval() == 0
    ventana._confirmar_solo()
    assert ventana._worker_websearch.estado["buscar_trabajos"] == [anterior]
    assert not ventana._websearch_inicio_por_revisar

    anterior.manifiesto.etapa("subir").actualizada = "2026-10-10T12:00:00"
    assert not ventana._pendientes_websearch_automaticos()


def test_revision_manual_puede_incluir_nuevos_y_anteriores(ventana, tmp_path):
    anterior = crear_trabajo(tmp_path, "anterior", EstadoEtapa.HECHA)
    nuevo = crear_trabajo(tmp_path, "nuevo")
    preparar(ventana, [anterior, nuevo])
    ventana._lanzar("subir", ventana._estado)
    ventana._buscar_websearch()
    assert ventana._worker_websearch.estado["buscar_trabajos"] == [anterior, nuevo]
    assert not ventana._worker_websearch.estado["confirmacion_automatica"]


def test_inicio_revisa_aunque_index_aun_no_tenga_sesion(ventana, tmp_path):
    anterior = crear_trabajo(tmp_path, "anterior", EstadoEtapa.HECHA)
    preparar(ventana, [anterior])
    ventana.auto_check.setChecked(False)
    ventana._estado.pop("sesion")
    ventana._lanzar("subir", ventana._estado)
    assert ventana._worker_websearch is None
    assert ventana._websearch_inicio_por_revisar == {str(anterior.carpeta)}
    assert ventana._confirmador.isActive()
    ventana._confirmar_solo()
    assert ventana._worker_websearch.estado["buscar_trabajos"] == [anterior]
    assert "sesion_base" not in ventana._worker_websearch.estado


def test_lectura_previa_no_consume_la_revision_del_nuevo_proceso(ventana, tmp_path):
    anterior = crear_trabajo(tmp_path, "anterior", EstadoEtapa.HECHA)
    preparar(ventana, [anterior])
    ventana.auto_check.setChecked(False)
    ventana._buscar_websearch()
    lector_previo = ventana._worker_websearch
    ventana._lanzar("subir", ventana._estado)
    ventana._confirmar_solo()
    assert ventana._worker_websearch is lector_previo
    assert ventana._websearch_inicio_por_revisar == {str(anterior.carpeta)}

    lector_previo.corriendo = False
    ventana._al_terminar_websearch()
    ventana._confirmar_solo()
    assert ventana._worker_websearch is not lector_previo
    assert ventana._worker_websearch.estado["buscar_trabajos"] == [anterior]


@pytest.mark.parametrize("subida, batch_id", [
    (EstadoEtapa.HECHA, None),
    (EstadoEtapa.EN_CURSO, None),
    (EstadoEtapa.OMITIDA, None),
    (None, "B123"),
])
def test_revision_inicial_incluye_cargas_rastreables_sin_id(ventana, tmp_path, subida, batch_id):
    anterior = crear_trabajo(tmp_path, "anterior", subida, batch_id)
    preparar(ventana, [anterior])
    ventana._lanzar("subir", ventana._estado)
    assert ventana._websearch_inicio == {str(anterior.carpeta)}


@pytest.mark.parametrize("en_paralelo", [False, True])
def test_contador_de_revision_indica_batches(ventana, monkeypatch, en_paralelo):
    lector = SimpleNamespace(modo="buscar_websearch", hay_que_parar=lambda: False)
    principal = SimpleNamespace(modo="subir", hay_que_parar=lambda: False) if en_paralelo else None
    monkeypatch.setattr(ventana, "hilo", lambda: principal)
    monkeypatch.setattr(ventana, "lectura_websearch", lambda: lector)
    ventana._linea_viva = QListWidgetItem()
    ventana._cuenta_websearch = (2, 7)
    ventana._cuenta_paso = (4, 100)
    ventana._pintar_linea_viva()
    assert "2 de 7 batches" in ventana._linea_viva.text()
    if en_paralelo:
        assert "Web Search: 2 de 7 batches" in ventana._linea_viva.text()
        assert "4 de 100 batches" not in ventana._linea_viva.text()
