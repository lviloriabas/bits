"""La corrección automática vista desde la ventana de Web Reports."""

from __future__ import annotations

from datetime import datetime
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QPixmap

from app.gui import web_reports_window as modulo_ventana
from app.airvault.config import AirVaultConfig
from app.airvault.correcciones import (
    ACCION_BORRAR,
    ACCION_REINDEXAR,
    ACCION_REVISAR,
    Copia,
    Resultado,
)
from app.airvault.web_reports import TIPO_MAL_INDEXADA, parsear_filas
from app.gui.web_reports_window import ADVERTENCIA_CORRECCION, WebReportsWindow
from app.gui.revision_copias_dialog import RevisionCopiasDialog


def _fila(matricula: str, detalle: str) -> list[dict[str, str]]:
    return [
        {"t": matricula},
        {"t": "Copa-7 (50)"},
        {"t": ""},
        {"t": "2008150 - 2008199"},
        {"t": "1/1/2025 - 1/31/2025"},
        {"t": ""},
        {"t": ""},
        {"t": ""},
        {"t": ""},
        {"t": detalle},
    ]


def _excepciones(*detalles: tuple[str, str]):
    return parsear_filas(
        [_fila(matricula, detalle) for matricula, detalle in detalles],
        AirVaultConfig(),
    )


def _elegir(ventana, tipo: str) -> None:
    """Elige la fila de esa excepcion, sin depender de donde caiga.

    La tabla se ordena sola al llenarse, asi que el numero de fila no es el
    orden en el que vinieron del reporte.
    """
    for fila in range(ventana.tabla.rowCount()):
        if ventana.tabla.item(fila, 0).text() == tipo:
            ventana.tabla.selectRow(fila)
            return
    raise AssertionError(f"no hay ninguna fila {tipo}")


def test_corregir_empieza_apagado_y_no_toca_nada(app, tmp_path) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        assert ventana.boton_corregir_todas.text() == "Corregir todas…"
        assert (
            ventana.boton_corregir.text() == "Corregir seleccionadas…"
        )
        assert not ventana.boton_corregir_todas.isEnabled()
        assert not ventana.boton_corregir.isEnabled()
        assert ventana.plan() == []
    finally:
        ventana.close()


def test_el_aviso_de_borrado_es_breve() -> None:
    assert ADVERTENCIA_CORRECCION == (
        "Las copias borradas no se pueden recuperar desde BITS."
    )
    assert "archiv" not in ADVERTENCIA_CORRECCION.casefold()


def test_una_correccion_completa_no_abre_otro_aviso(
    app, tmp_path, monkeypatch
) -> None:
    ventana = WebReportsWindow(tmp_path)
    plan = modulo_ventana.planificar(
        _excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)"))
    )

    class _SinAviso:
        Icon = modulo_ventana.QMessageBox.Icon

        def __init__(self, *_args):
            raise AssertionError("no debe abrir un aviso de éxito")

    monkeypatch.setattr(
        modulo_ventana,
        "QMessageBox",
        _SinAviso,
    )
    try:
        ventana._al_corregir([Resultado(plan[0], hecho=True)])
        assert "Corregidas 1 bitácora" in ventana.resumen.text()
    finally:
        ventana.close()


def test_el_aviso_de_fallo_muestra_el_primer_motivo(
    app, tmp_path, monkeypatch
) -> None:
    visto: dict[str, str] = {}

    class _AvisoFalso:
        Icon = modulo_ventana.QMessageBox.Icon

        def __init__(self, _parent):
            pass

        def setIcon(self, _icon):
            pass

        def setWindowTitle(self, texto):
            visto["titulo"] = texto

        def setText(self, texto):
            visto["texto"] = texto

        def setInformativeText(self, texto):
            visto["motivo"] = texto

        def setDetailedText(self, texto):
            visto["detalle"] = texto

        def findChildren(self, _tipo):
            return []

        def exec(self):
            pass

    plan = modulo_ventana.planificar(
        _excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)"))
    )
    monkeypatch.setattr(modulo_ventana, "QMessageBox", _AvisoFalso)
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_corregir(
            [Resultado(plan[0], detalle="No apareció el botón Borrar.")]
        )
    finally:
        ventana.close()

    assert visto["titulo"] == "Corrección incompleta"
    assert "2008159" in visto["motivo"]
    assert "botón Borrar" in visto["motivo"]


def test_se_enciende_con_lo_que_el_reporte_deja_resuelto(app, tmp_path) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(
                ("HP-9913CMP", "DUPLICATED 2008159(3x)"),
                ("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]"),
            )
        )
        ventana._habilitar(True)

        assert [correccion.accion for correccion in ventana.plan()] == [
            ACCION_BORRAR,
            ACCION_REINDEXAR,
        ]
        assert ventana.boton_corregir_todas.isEnabled()
        # Sin filas elegidas, la otra no tiene sobre qué actuar.
        assert not ventana.boton_corregir.isEnabled()
        assert ventana.resumen.text() == (
            "1 mal indexada, 1 duplicada. 2 se pueden corregir."
        )
    finally:
        ventana.close()


def test_sigue_apagado_si_todo_queda_para_revisar(app, tmp_path) -> None:
    """Una duplicada sin cuántas copias hay no la resuelve el reporte."""
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(("HP-9913CMP", "DUPLICATED 2008159(1x)"))
        )
        ventana._habilitar(True)

        assert ventana.plan()
        assert not ventana.boton_corregir_todas.isEnabled()
        assert "Requieren revisión manual" in ventana.resumen.text()
    finally:
        ventana.close()


def test_no_autorizar_no_abre_el_navegador(app, tmp_path, monkeypatch) -> None:
    """Es la única puerta: sin el sí de alguien no se lanza ningún hilo."""
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(("HP-9913CMP", "DUPLICATED 2008159(3x)"))
        )
        monkeypatch.setattr(
            WebReportsWindow, "_autorizado", lambda self, plan: False
        )

        ventana._corregir_todas()

        assert ventana.hilo() is None
    finally:
        ventana.close()


def test_el_cuadro_de_aviso_enumera_lo_que_se_va_a_hacer(
    app, tmp_path, monkeypatch
) -> None:
    visto: dict[str, object] = {}

    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(
                ("HP-9913CMP", "DUPLICATED 2008159(3x)"),
                ("HP-1523CMP", "2284906 MIS-INDEX to ACN [HP-9813CMP]"),
            )
        )

        def _mirar(self, plan):
            visto["plan"] = list(plan)
            return False

        monkeypatch.setattr(WebReportsWindow, "_autorizado", _mirar)
        ventana._corregir_todas()

        descripciones = [
            correccion.descripcion for correccion in visto["plan"]
        ]
        assert "conservar la más antigua de 3 copias" in descripciones[0]
        assert "de HP-9813CMP a HP-1523CMP" in descripciones[1]
    finally:
        ventana.close()


def test_solo_se_corrigen_las_elegidas(app, tmp_path, monkeypatch) -> None:
    """El botón de la selección no lleva a AirVault lo que nadie eligió."""
    visto: dict[str, object] = {}

    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(
                ("HP-9913CMP", "DUPLICATED 2008159(3x)"),
                ("HP-1523CMP", "2284906 MIS-INDEX to ACN [HP-9813CMP]"),
            )
        )
        ventana._habilitar(True)
        assert not ventana.boton_corregir.isEnabled()

        _elegir(ventana, TIPO_MAL_INDEXADA)
        assert ventana.boton_corregir.isEnabled()
        assert [
            excepcion.log_number for excepcion in ventana.seleccionadas()
        ] == ["2284906"]

        def _mirar(self, plan):
            visto["plan"] = list(plan)
            return False

        monkeypatch.setattr(WebReportsWindow, "_autorizado", _mirar)
        ventana._corregir_seleccion()

        assert [
            correccion.accion for correccion in visto["plan"]
        ] == [ACCION_REINDEXAR]
        assert visto["plan"][0].log_number == "2284906"
        assert ventana.hilo() is None
    finally:
        ventana.close()


def test_sin_filas_elegidas_lo_dice_y_no_corrige(app, tmp_path) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(("HP-9913CMP", "DUPLICATED 2008159(3x)"))
        )
        ventana.tabla.clearSelection()

        ventana._corregir_seleccion()

        assert ventana.resumen.text() == "Seleccione al menos una fila."
        assert ventana.hilo() is None
    finally:
        ventana.close()


def test_elegir_la_mal_indexada_sola_no_la_da_por_reindexable(
    app, tmp_path
) -> None:
    """El plan cruza las dos excepciones aunque solo se elija una.

    Esa bitácora está además repetida, y quitar las copias que sobran puede
    arreglar de paso el archivado. Planificando solo la fila elegida esa
    regla no se habría aplicado.
    """
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(
                ("HP-9913CMP", "DUPLICATED 2008159(3x)"),
                ("HP-1523CMP", "2008159 MIS-INDEX to ACN [HP-9813CMP]"),
            )
        )
        _elegir(ventana, TIPO_MAL_INDEXADA)

        plan = ventana.plan_seleccionado()

        assert [correccion.accion for correccion in plan] == [ACCION_REVISAR]
        assert "primero se quitan las copias" in plan[0].motivo
    finally:
        ventana.close()


def test_desactivar_revision_durante_el_trabajo_llega_al_worker(app, tmp_path):
    ventana = WebReportsWindow(tmp_path)
    worker = modulo_ventana.CorreccionWorker(AirVaultConfig(), [], ventana)
    worker.mostrar_previas = True
    ventana._worker = worker
    ventana._habilitar(False)
    assert ventana.mostrar_previas.isEnabled()
    ventana.mostrar_previas.setChecked(False)
    assert not worker.mostrar_previas
    ventana.mostrar_previas.setChecked(True)
    assert worker.mostrar_previas


def test_la_previa_que_ya_estaba_en_cola_respeta_desactivar(app, tmp_path, monkeypatch):
    ventana = WebReportsWindow(tmp_path)
    worker = modulo_ventana.CorreccionWorker(AirVaultConfig(), [], ventana)
    ventana._worker = worker
    monkeypatch.setattr(ventana, "sender", lambda: worker)
    ventana._revisar_copias(None, [], {"posterior"})
    assert worker._respuesta_previa.is_set()
    assert worker._seleccion_previa == {"posterior"}


def test_el_boton_del_visor_continua_y_apaga_las_siguientes_previas(app, tmp_path, monkeypatch):
    ventana = WebReportsWindow(tmp_path)
    worker = modulo_ventana.CorreccionWorker(AirVaultConfig(), [], ventana)
    worker.mostrar_previas = True
    ventana._worker = worker
    monkeypatch.setattr(ventana, "sender", lambda: worker)
    pixmap = QPixmap(100, 100)
    pixmap.fill(Qt.GlobalColor.white)
    datos = QByteArray()
    buffer = QBuffer(datos)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    vistas = [
        (Copia(str(i), str(i), str(i), datetime(2026, 1, i + 1), "HP-9913CMP", 1, "LOG PAGE"), bytes(datos))
        for i in range(3)
    ]
    correccion = _excepciones(("HP-9913CMP", "DUPLICATED 2008159(3x)"))[0]

    ventana._revisar_copias(correccion, vistas, {"1", "2"})
    dialogo = ventana._revision_actual
    dialogo.lista.item(0).setCheckState(Qt.CheckState.Checked)
    dialogo.predeterminadas.click()
    assert worker._seleccion_previa == {"1", "2"}
    assert not worker.mostrar_previas
    assert not ventana.mostrar_previas.isChecked()
    assert ventana._revision_actual is dialogo
    assert dialogo.isVisible()


def _revision_abierta(app, tmp_path, monkeypatch):
    from tests.test_revision_copias import _vistas

    ventana = WebReportsWindow(tmp_path)
    worker = modulo_ventana.CorreccionWorker(AirVaultConfig(), [], ventana)
    worker.mostrar_previas = True
    ventana._worker = worker
    monkeypatch.setattr(ventana, "sender", lambda: worker)
    correccion = _excepciones(("HP-9913CMP", "DUPLICATED 2008159(3x)"))[0]
    ventana._revisar_copias(correccion, _vistas(3), {"1", "2"})
    return ventana, worker, correccion


def test_reutiliza_el_visor_tras_eliminar_y_omitir(app, tmp_path, monkeypatch):
    from tests.test_revision_copias import _vistas

    ventana, worker, correccion = _revision_abierta(app, tmp_path, monkeypatch)
    dialogo = ventana._revision_actual
    dialogo.aplicar.click()
    assert worker._seleccion_previa == {"1", "2"}
    assert dialogo.isVisible()
    assert not dialogo.aplicar.isEnabled()
    worker._respuesta_previa.clear()
    siguiente = _excepciones(("HP-9913CMP", "DUPLICATED 2008160(2x)"))[0]
    ventana._revisar_copias(siguiente, _vistas(2), {"1"})
    assert ventana._revision_actual is dialogo
    assert "2008160" in dialogo.windowTitle()
    assert dialogo.lista.count() == 2
    dialogo.omitir.click()
    assert worker._respuesta_previa.is_set()
    assert worker._seleccion_previa is None
    assert not worker._cancelado()
    assert dialogo.isVisible()
    ventana._al_terminar()
    assert ventana._revision_actual is None
    assert not dialogo.isVisible()
    assert not worker._cancelado()


def test_la_x_del_visor_cancela_tambien_despues_de_aplicar(app, tmp_path, monkeypatch):
    ventana, worker, _ = _revision_abierta(app, tmp_path, monkeypatch)
    dialogo = ventana._revision_actual
    dialogo.aplicar.click()
    dialogo.close()
    assert worker._cancelado()
    assert not dialogo.isVisible()
    assert ventana.resumen.text() == "Cancelando…"
    ventana._al_cancelar()
    assert ventana.resumen.text() == "Corrección cancelada."


def test_cancelar_en_el_visor_detiene_la_revision(app, tmp_path, monkeypatch):
    ventana, worker, _ = _revision_abierta(app, tmp_path, monkeypatch)
    ventana._revision_actual.boton_cancelar.click()
    assert worker._cancelado()
    assert not worker._respuesta_previa.is_set()


def test_la_barra_muestra_la_correccion_y_conserva_el_porcentaje_al_cancelar(app, tmp_path):
    ventana = WebReportsWindow(tmp_path)
    worker = modulo_ventana.CorreccionWorker(AirVaultConfig(), [], ventana)
    ventana._worker = worker
    ventana.progreso.setFormat("Corrección: %p%")
    ventana._al_pasar(1, 4)
    assert ventana.progreso.value() == 25
    ventana._al_pasar(2, 4)
    assert ventana.progreso.value() == 50
    ventana._al_cancelar()
    ventana._al_terminar()
    assert ventana.progreso.value() == 50
    assert ventana.progreso.format() == "Corrección: %p%"
