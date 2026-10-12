"""Controles del proceso, pausa segura y presentacion del registro."""

import time
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from app.gui import airvault_window as modulo
from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker
from tests.test_gui_airvault_window import corrida
from tests.test_gui_batches_filtros import cargada


def esperar(app, condicion):
    limite = time.monotonic() + 4
    while not condicion() and time.monotonic() < limite:
        QTest.qWait(10)
    assert condicion()


def test_pausar_y_continuar_conserva_el_trabajo_y_el_ancho_del_boton(app, tmp_path, monkeypatch):
    class TrabajoPrueba(TrabajoAirVaultWorker):
        def _subir(self):
            guardadas = self.estado["guardadas"]
            for pagina in range(len(guardadas), 40):
                self._avisar("Escribiendo", pagina, 40)
                guardadas.append(pagina)
                self._dormir(0.01)

    monkeypatch.setattr(modulo, "TrabajoAirVaultWorker", TrabajoPrueba)
    corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana.show()
    estado = {"guardadas": [], "trabajos": []}
    ventana._estado = estado
    monkeypatch.setattr(ventana, "_subir_a_mano", lambda: ventana._lanzar("subir", estado))
    monkeypatch.setattr(ventana, "_continuar_pendiente", lambda: ventana._lanzar("subir", estado))
    ancho = ventana.boton_proceso.width()
    ventana.boton_proceso.click()
    esperar(app, lambda: len(estado["guardadas"]) >= 3)
    assert ventana.boton_pausar.isEnabled()
    ventana.boton_pausar.click()
    esperar(app, lambda: ventana.hilo() is None and ventana.boton_proceso.isEnabled())
    guardadas = list(estado["guardadas"])
    assert 0 < len(guardadas) < 40
    assert ventana.boton_proceso.text() == "Continuar proceso"
    assert ventana.boton_proceso.width() == ancho
    QTest.qWait(30)
    assert estado["guardadas"] == guardadas
    ventana.boton_proceso.click()
    esperar(app, lambda: ventana.hilo() is None and len(estado["guardadas"]) == 40)
    assert estado["guardadas"] == list(range(40))
    assert ventana.boton_proceso.width() == ancho
    ventana.close()


def test_la_pausa_detiene_tambien_lecturas_y_comprobaciones_pendientes(app, tmp_path, monkeypatch):
    ventana = AirVaultWindow(tmp_path)
    cancelados = []
    worker = SimpleNamespace(cancelar=lambda: cancelados.append("proceso"))
    trabajo = SimpleNamespace(carpeta=tmp_path / "batch")
    lector = SimpleNamespace(cancelar=lambda: cancelados.append("lectura"), estado={"buscar_trabajos": [trabajo]})
    clave = str(trabajo.carpeta)
    ventana._websearch_revisados[clave] = "consulta en curso"
    activo = [True]
    monkeypatch.setattr(ventana, "hilo", lambda: worker if activo[0] else None)
    monkeypatch.setattr(ventana, "lectura_websearch", lambda: lector if activo[0] else None)
    ventana._comprobar_al_terminar = True
    ventana._indexar_al_terminar = True
    ventana._subir_al_terminar = True
    ventana._pausar_proceso()
    assert cancelados == ["proceso", "lectura"]
    assert not ventana._comprobar_al_terminar
    assert not ventana._indexar_al_terminar
    assert not ventana._subir_al_terminar
    assert clave not in ventana._websearch_revisados
    assert clave in ventana._websearch_inicio_por_revisar
    lanzados = []
    monkeypatch.setattr(ventana, "_comprobar", lambda: lanzados.append("revision"))
    monkeypatch.setattr(ventana, "_iniciar_confirmacion", lambda *a, **k: lanzados.append("web"))
    activo[0] = False
    ventana._comprobar_solo()
    ventana._confirmar_solo()
    ventana._al_terminar()
    assert lanzados == []
    assert ventana.boton_proceso.text() == "Continuar proceso"


def test_continuar_una_lectura_independiente_recupera_tambien_las_consultas_en_cola(app, tmp_path, monkeypatch):
    ventana = AirVaultWindow(tmp_path)
    activo = [True]
    trabajos = [SimpleNamespace(carpeta=tmp_path / str(i)) for i in range(2)]
    lector = SimpleNamespace(cancelar=lambda: None, estado={"buscar_trabajos": trabajos[:1]})
    ventana._websearch_pendientes[str(trabajos[1].carpeta)] = trabajos[1]
    monkeypatch.setattr(ventana, "lectura_websearch", lambda: lector if activo[0] else None)
    ventana._pausar_proceso()
    activo[0] = False
    reiniciados = []
    monkeypatch.setattr(ventana, "_iniciar_confirmacion", lambda lotes, **kw: reiniciados.extend(lotes))
    ventana._empezar_o_continuar()
    assert reiniciados == trabajos
    assert not ventana._pausado


def test_cancelar_un_proceso_pausado_no_lo_reanuda(app, tmp_path, monkeypatch):
    ventana = AirVaultWindow(tmp_path)
    ventana._listo_para_subir = True
    ventana._pausar_proceso()
    ventana.boton_cancelar.click()
    assert not ventana._pausado
    assert ventana.boton_proceso.text() == "Empezar proceso"
    assert not ventana.boton_pausar.isEnabled()
    lanzados = []
    monkeypatch.setattr(ventana, "_comprobar", lambda: lanzados.append("revision"))
    ventana._comprobar_solo()
    assert lanzados == []


def test_el_limite_empieza_en_cinco_y_recuerda_el_valor_elegido(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    assert ventana.porcentaje_duplicados_spin.value() == 5
    ventana.porcentaje_duplicados_spin.setValue(13)
    ventana.close()
    nueva = AirVaultWindow(tmp_path)
    assert nueva.porcentaje_duplicados_spin.value() == 13
    nueva.close()


def test_todas_las_ejecuciones_tienen_casilla_y_se_pueden_marcar(app, tmp_path):
    listo = corrida(tmp_path, "BITS 10 OCT 2026 10 00")
    pendiente = corrida(tmp_path, "BITS 10 OCT 2026 11 00", exportada=False)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    combo = ventana.historial
    for csv in (listo, pendiente):
        assert combo.itemData(combo.findData(str(csv)), Qt.ItemDataRole.CheckStateRole) is not None
    indice = combo.findData(str(pendiente))
    combo.marcar(indice)
    assert combo.itemData(indice, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert str(pendiente) in ventana._corridas_seleccionadas()


def test_el_menu_se_reabre_y_no_ofrece_vista_previa(app, tmp_path):
    ventana = cargada(app, tmp_path)
    lista = ventana.lotes
    punto = lista.visualItemRect(lista.item(0)).center()
    for _ in range(3):
        lista.customContextMenuRequested.emit(punto)
        app.processEvents()
        menu = ventana._menu_batches_abierto
        assert menu is not None and menu.isVisible()
        assert not any("Vista previa" in accion.text() for accion in menu.actions())
        menu.close()
        app.processEvents()
        assert ventana._menu_batches_abierto is None


def test_la_barra_solo_contiene_el_porcentaje_y_no_hay_frase_inferior(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    ventana.show()
    ventana.resumen.setText("Un mensaje largo que no cabe en una franja")
    ventana._marcar_reloj()
    for _ in range(2):
        ventana._pintar_avance()
        assert ventana.progreso.format() == "%p%"
        assert not ventana.reloj_label.isVisibleTo(ventana)
        assert not ventana.resumen.isVisibleTo(ventana)


def test_los_detalles_del_registro_no_se_alinean_con_espacios(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    ventana._anotar("Batch guardado", ["12 bitácoras por revisar", "80 páginas recibidas"])
    texto = ventana.bitacora.item(0).text()
    hora, cuerpo = ventana.bitacora.itemDelegate().separar(texto)
    assert len(hora) == 8
    assert cuerpo.splitlines()[:3] == ["Batch guardado", "12 bitácoras por revisar", "80 páginas recibidas"]
    assert not any(linea.startswith(" ") for linea in cuerpo.splitlines())
