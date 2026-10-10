"""El proceso termina al cumplir sus metas y deja de consultar AirVault."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QTimer

from app.airvault.flujo import (AUTOCOMPLETADO, BUSCANDO, COMPLETADO, INDEXADO,
                               EstadoParte)
from app.airvault.model import EstadoEtapa
from app.gui.airvault_window import AirVaultWindow
from tests.test_airvault_rework import trabajo


class LecturaLenta(QObject):
    """La lectura reconoce la cancelacion despues del aviso de fin."""

    def __init__(self, automatico, parent):
        super().__init__(parent)
        self.estado = {"confirmacion_automatica": automatico}
        self.modo = "buscar_websearch"
        self.cancelaciones = 0
        self.corriendo = True

    def isRunning(self):
        return self.corriendo

    def hay_que_parar(self):
        return bool(self.cancelaciones)

    def cancelar(self):
        self.cancelaciones += 1

    def wait(self, _limite):
        self.corriendo = False
        return True


def preparar(app, tmp_path, estado=COMPLETADO, completar=True):
    ventana = AirVaultWindow(tmp_path)
    t = trabajo(tmp_path, "actual")
    t.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    t.manifiesto.etapa("verificar").marcar(EstadoEtapa.HECHA)
    if estado in (COMPLETADO, AUTOCOMPLETADO):
        t.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
    t.guardar()
    ventana.completar_check.setChecked(completar)
    ventana._trabajos = [t]
    ventana._estados = [EstadoParte(t, estado)]
    ventana._alcance_proceso = {str(t.carpeta)}
    ventana._estado["trabajos"] = [t]
    return ventana, t


@pytest.mark.parametrize("estado, completar", [
    (COMPLETADO, True), (AUTOCOMPLETADO, True), (INDEXADO, False),
])
def test_fin_corta_la_cadena_y_la_lectura_auxiliar(app, tmp_path, monkeypatch, estado, completar):
    ventana, _t = preparar(app, tmp_path, estado, completar)
    lector = LecturaLenta(True, ventana)
    ventana._worker_websearch = lector
    ventana._vigilante = QTimer(ventana)
    ventana._vigilante.start(60_000)
    ventana._confirmador = QTimer(ventana)
    ventana._confirmador.start(60_000)
    ventana._comprobar_al_terminar = True
    ventana._subir_al_terminar = True
    ventana._indexar_al_terminar = True
    ventana._websearch_proceso_activo = True
    ventana._actualizar_latido()
    avisos = []
    ventana.proceso_terminado.connect(avisos.append)
    monkeypatch.setattr(ventana, "_comprobar", lambda: pytest.fail("No hay otra consulta pendiente"))
    ventana._al_terminar()

    assert lector.isRunning(), "El fin no espera la respuesta de AirVault"
    assert lector.cancelaciones == 1
    assert ventana.progreso.text() == "100% - Proceso terminado"
    assert "Todo lo solicitado se completó" in ventana.resumen.text()
    assert not ventana._vigilante.isActive()
    assert not ventana._confirmador.isActive()
    assert not ventana._latido.isActive()
    assert ventana._linea_viva is None
    assert not ventana._websearch_proceso_activo
    assert not ventana.boton_cancelar.isEnabled()

    resumen = ventana.resumen.text()
    mensajes = ventana.bitacora.count()
    ventana._al_fallar_websearch("AirVault no responde")
    ventana._mostrar_paso_websearch("Esperando a AirVault", 0, 0)
    ventana._al_buscar_websearch({"automatico": True, "resultados": [(ventana._trabajos[0], False)]})
    lector.corriendo = False
    ventana._al_terminar_websearch()
    ventana._comprobar_solo()
    ventana._ajustar_vigilancia()
    ventana._al_terminar()
    assert ventana.resumen.text() == resumen
    assert ventana.bitacora.count() == mensajes
    assert ventana.estado_label.text() == "Proceso terminado"
    assert avisos == [resumen]
    assert not ventana._vigilando()
    assert not ventana._confirmador.isActive()


def test_ultimo_indexado_termina_sin_otra_revision_del_servidor(app, tmp_path, monkeypatch):
    ventana, t = preparar(app, tmp_path, INDEXADO)
    t.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
    t.guardar()
    monkeypatch.setattr(ventana, "_comprobar", lambda: pytest.fail("El cierre ya esta confirmado"))
    ventana._al_indexar({
        "resultado": SimpleNamespace(interrumpido="", escritas=10, omitidas=0, fallidas=0),
        "carpetas": [str(t.carpeta)], "validas": 10, "total": 10,
    })
    ventana._al_terminar()
    assert ventana._estados[0].estado == COMPLETADO
    assert ventana.progreso.value() == 100
    assert not ventana._comprobar_al_terminar


def test_revision_manual_sigue_pendiente_hasta_acabar(app, tmp_path):
    ventana, _t = preparar(app, tmp_path)
    lector = LecturaLenta(False, ventana)
    ventana._worker_websearch = lector
    ventana._al_terminar()
    assert ventana.progreso.value() == 99
    assert lector.cancelaciones == 0
    assert "Todo lo solicitado" not in ventana.resumen.text()
    lector.corriendo = False
    ventana._al_terminar_websearch()
    assert ventana.progreso.value() == 100


def test_un_batch_de_otro_proceso_no_mantiene_la_espera(app, tmp_path):
    ventana, _t = preparar(app, tmp_path)
    otro = trabajo(tmp_path, "otro")
    ventana._trabajos.append(otro)
    ventana._estados.append(EstadoParte(otro, BUSCANDO))
    ventana._ajustar_vigilancia()
    ventana._al_terminar()
    assert ventana.progreso.value() == 100
    assert not ventana._falta_esperar()
    assert not ventana._vigilando()


def test_pedir_completar_despues_de_indexar_reabre_la_meta(app, tmp_path):
    ventana, _t = preparar(app, tmp_path, INDEXADO, False)
    ventana._al_terminar()
    assert ventana.progreso.value() == 100
    ventana.completar_check.setChecked(True)
    assert ventana.progreso.value() < 100
    assert ventana._firma_de_fin() is None
    assert ventana._falta_esperar()


def test_el_aviso_llega_a_la_principal_con_airvault_oculto(app, tmp_path, monkeypatch):
    from app.gui import main_window as modulo

    ventana, _t = preparar(app, tmp_path)
    monkeypatch.setattr(modulo, "AirVaultWindow", lambda *_args: ventana)
    alertas = []
    monkeypatch.setattr(modulo.QApplication, "alert", lambda widget, ms: alertas.append((widget, ms)))
    principal = modulo.MainWindow()
    principal._open_airvault()
    ventana.hide()
    ventana._al_terminar()
    ventana._al_terminar()
    assert "Todo lo solicitado se completó" in principal.statusBar().currentMessage()
    assert alertas == [(principal, 5000)]
