"""La ventana «Indexar en AirVault».

Comprueba lo que la ventana promete: que el historial enseñe las
ejecuciones y diga cuáles se pueden subir, que subir y indexar sean dos
tiempos separados por la espera de AirVault, que esa espera se pregunte
sola sin congelar nada, y que cerrar el programa espere al batch a medio
escribir.

Nada de esto toca la red: la ventana solo prepara el trabajo y arranca el
hilo, y lo que decide si una página se escribe vive en ``app.airvault``.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QGroupBox,
    QMessageBox,
    QToolButton,
)

from app.airvault.config import AirVaultConfig
from app.core.parallelism import available_cpu_threads
from app.gui.automatizacion import OpcionesAutomatizacion
from app.gui.airvault_window import (
    ANCHO_MAXIMO_NOMBRE_BATCH,
    ANCHO_MINIMO_NOMBRE_BATCH,
    color_indexado,
    AirVaultWindow,
    TrabajoAirVaultWorker,
    csv_de_corrida,
)

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture
def ventana(app, tmp_path):
    """La ventana sobre el historial real, con sus opciones en un aparte.

    Los pasos del proceso automático tienen memoria en disco, y esa memoria
    es el ``airvault.json`` del repositorio: sin darle uno propio, cada
    prueba que toca «Revisar cada» o «Completar batch» le cambia la
    configuración a quien ejecuta el programa, y a la prueba siguiente.
    """
    return AirVaultWindow(tmp_path, OpcionesAutomatizacion(tmp_path))


def test_el_resumen_de_la_ejecucion_se_llama_resultado(ventana):
    assert ventana.reparto_total.parentWidget().title() == "Resultado"
    assert isinstance(ventana.reparto_total.parentWidget(), QGroupBox)


def test_casilla_duplicados_recuerda_ambos_estados(app, tmp_path):
    for marcada in (True, False):
        ventana = AirVaultWindow(tmp_path, OpcionesAutomatizacion(tmp_path))
        ventana.detener_duplicados_check.setChecked(marcada)
        ventana.close()
        nueva = AirVaultWindow(tmp_path, OpcionesAutomatizacion(tmp_path))
        assert nueva.detener_duplicados_check.isChecked() is marcada
        assert nueva._config.detener_por_duplicados is marcada
        nueva.close()


def test_completar_activo_mantiene_vigilancia_hasta_confirmar_cierre(ventana):
    from app.airvault.flujo import INDEXADO, COMPLETADO
    ventana._estados = [parte(INDEXADO)]
    ventana.completar_check.setChecked(False)
    assert not ventana._falta_esperar()
    ventana.completar_check.setChecked(True)
    assert ventana._falta_esperar()
    ventana._ajustar_vigilancia()
    assert ventana._vigilante.isActive()
    ventana._estados = [parte(COMPLETADO)]
    ventana._ajustar_vigilancia()
    assert not ventana._vigilante.isActive()


def corrida(
    raiz, nombre="BITS 18 AUG 2026 05 42", exportada=True, paginas=19,
):
    """Deja en ``raiz/output`` una ejecución como la que escribe la exportación.

    Sin ``exportada`` queda solo lo que produce el procesamiento: los datos,
    sin PDF de entrega ni índice de páginas, que es lo que hace falta para
    subirla.
    """
    carpeta = raiz / "output" / nombre
    (carpeta / "datos").mkdir(parents=True)
    csv = carpeta / "datos" / f"{nombre}.CSV"
    csv.write_text("file,page,log_number,matricula,date\n", encoding="utf-8")
    (carpeta / "stats.json").write_text(
        json.dumps({"corrida": nombre, "total_paginas": paginas}),
        encoding="utf-8",
    )
    if exportada:
        (carpeta / f"{nombre}.pdf").write_bytes(b"%PDF-1.4\n")
        csv.with_name(f"{csv.stem}_paginas.json").write_text(
            json.dumps([{"pdf": f"{nombre}.pdf", "paginas": [1]}]),
            encoding="utf-8",
        )
    return csv


def registrar_en_airvault(raiz: Path, csv: Path) -> Path:
    """Deja el manifiesto local mínimo de la ejecución seleccionada."""
    from app.airvault.flujo import carpeta_de_corrida, carpeta_de_trabajo
    from app.airvault.manifest import guardar
    from app.airvault.model import Manifiesto

    nombre = carpeta_de_corrida(csv).name
    carpeta = raiz / carpeta_de_trabajo(nombre)
    pdf = csv.parent.parent / f"{nombre}.pdf"
    manifiesto = Manifiesto(
        job_id=nombre,
        nombre_batch=f"DP | {nombre}",
        csv_origen=str(csv.resolve()),
        pdf_origen=str(pdf.resolve()),
    )
    return guardar(manifiesto, carpeta)


# ── forma de la ventana ────────────────────────────────────────────

def test_es_una_ventana_aparte_y_arranca_escondida(ventana):
    """Empotrada le quitaba alto a la vista previa y rompía el reparto."""
    assert ventana.isHidden()
    assert ventana.windowTitle() == "Indexar en AirVault"
    assert ventana.parent() is None
    assert ventana.windowType() == Qt.WindowType.Window


def test_la_cookie_no_se_ve_al_teclearla(ventana):
    """Vale tanto como la contraseña: no puede quedar a la vista."""
    assert ventana.cookie_edit.echoMode() == QLineEdit.EchoMode.Password


def test_indexar_esta_apagado_hasta_que_haya_un_lote_listo(ventana):
    assert not ventana.boton_indexar.isEnabled()


def test_la_bitacora_de_airvault_se_puede_copiar(ventana):
    ventana.bitacora.addItems([
        "10:00  Entrando a AirVault",
        "10:01  Revisando la sesión",
    ])
    ventana.bitacora.item(0).setSelected(True)
    ventana.bitacora.item(1).setSelected(True)

    ventana.bitacora.copySelectedItems()

    assert QApplication.clipboard().text() == (
        "10:00  Entrando a AirVault\n10:01  Revisando la sesión"
    )


def test_sin_corrida_elegida_no_hay_nada_que_subir(ventana):
    assert not ventana.boton_subir.isEnabled()


def test_sin_nada_subido_no_hay_nada_que_comprobar(ventana):
    """Comprobar es preguntar por batches; sin subir no hay ninguno."""
    assert not ventana.boton_comprobar.isEnabled()


def test_sin_registro_no_hay_nada_que_eliminar(ventana):
    assert not ventana.boton_eliminar_registro.isEnabled()


def test_completar_el_batch_sin_historial_no_impone_un_default(app, tmp_path):
    """Sin preferencia guardada, la interfaz no escribe ni impone una."""
    ventana = AirVaultWindow(tmp_path)

    assert not ventana.completar_check.isChecked()
    assert not (tmp_path / "airvault.json").exists()
    ventana.close()


@pytest.mark.parametrize("ultimo_estado", [True, False])
def test_completar_batch_recuerda_exactamente_el_ultimo_estado(
    app, tmp_path, ultimo_estado,
):
    primera = AirVaultWindow(tmp_path)
    primera.completar_check.setChecked(not ultimo_estado)
    primera.completar_check.setChecked(ultimo_estado)
    primera.close()

    segunda = AirVaultWindow(tmp_path)

    assert segunda.completar_check.isChecked() is ultimo_estado
    assert json.loads((tmp_path / "airvault.json").read_text(
        encoding="utf-8"
    ))["completar_batch"] is ultimo_estado
    segunda.close()


def test_el_limite_de_quick_upload_toma_las_400_paginas_guardadas(app, tmp_path):
    from app.gui.widgets import SpinBoxWithButtons

    (tmp_path / "airvault.json").write_text(
        json.dumps({"paginas_por_batch": 400}), encoding="utf-8"
    )
    ventana = AirVaultWindow(tmp_path, OpcionesAutomatizacion(tmp_path))

    assert ventana.limite_batch_spin.value() == 400
    assert isinstance(ventana.limite_batch_control, SpinBoxWithButtons)
    assert ventana.limite_batch_spin.parentWidget() is (
        ventana.limite_batch_control
    )
    ventana.close()


def test_configuracion_parcial_y_reapertura_conservan_la_cantidad(app, tmp_path):
    from app.airvault.config import guardar_preferencias
    from app.gui.export_options import ExportOptionsGroup

    (tmp_path / "airvault.example.json").write_text(
        json.dumps({"paginas_por_batch": 400}), encoding="utf-8",
    )
    guardar_preferencias(tmp_path / "airvault.json", auto_subir=False)
    primera = AirVaultWindow(tmp_path)
    assert primera.limite_batch_spin.value() == 400
    primera.limite_batch_spin.setValue(475)
    primera.close()
    guardar_preferencias(tmp_path / "airvault.json", completar_batch=False)

    segunda = AirVaultWindow(tmp_path)
    exportacion = ExportOptionsGroup(raiz=tmp_path)
    assert segunda.limite_batch_spin.value() == 475
    assert exportacion.partes_spin.value() == 475
    segunda.close()
    exportacion.close()


def test_un_batch_antiguo_no_reemplaza_la_cantidad_configurada(app, tmp_path):
    csv = corrida(tmp_path)
    registro = registrar_en_airvault(tmp_path, csv)
    datos = json.loads(registro.read_text(encoding="utf-8"))
    datos["paginas_por_batch"] = 10
    registro.write_text(json.dumps(datos), encoding="utf-8")
    ventana = AirVaultWindow(tmp_path)
    ventana.limite_batch_spin.setValue(475)

    ventana.fijar_corrida(csv)

    assert len(ventana._trabajos) == 1
    assert ventana._trabajos[0].manifiesto.paginas_por_batch == 10
    assert ventana.limite_batch_spin.value() == 475
    assert ventana._base_del_estado()["paginas_por_batch"] == 475
    assert AirVaultConfig.load(tmp_path / "airvault.json").paginas_por_batch == 475
    ventana.close()


def test_al_elegir_corrida_recupera_la_preferencia_cambiada_en_exportacion(app, tmp_path):
    from app.gui.export_options import ExportOptionsGroup

    ventana = AirVaultWindow(tmp_path)
    ventana.limite_batch_spin.setValue(300)
    exportacion = ExportOptionsGroup(raiz=tmp_path)
    exportacion.partes_spin.setValue(550)

    ventana.fijar_corrida(corrida(tmp_path))

    assert ventana.limite_batch_spin.value() == 550
    assert ventana._config.paginas_por_batch == 550
    ventana.close()
    exportacion.close()


def test_la_compresion_es_opcional_y_explica_los_200_dpi(ventana):
    assert ventana.compresion_check.text() == "Compresión"
    assert not ventana.compresion_check.isChecked()
    assert "200 DPI" in ventana.compresion_check.toolTip()


def test_la_espera_automatica_empieza_en_dos_minutos(ventana):
    from app.gui.widgets import SpinBoxWithButtons

    assert ventana.minutos_spin.value() == 2
    assert isinstance(ventana.minutos_control, SpinBoxWithButtons)
    assert ventana.minutos_spin.parentWidget() is ventana.minutos_control


def test_los_pasos_automaticos_son_un_menu_y_no_un_panel(ventana):
    """Empotrados ocupaban media ventana; ahora se abren encima de ella."""
    from app.gui.automatizacion import INDEXAR, MenuAutomatizacion

    assert not hasattr(ventana, "auto_subir_check")
    assert not hasattr(ventana, "auto_esperar_check")
    assert not hasattr(ventana, "auto_indexar_check")
    assert isinstance(ventana.menu_automatizacion, MenuAutomatizacion)
    # Un menú no forma parte del reparto de la ventana: no ocupa alto.
    assert ventana.menu_automatizacion.parentWidget() is ventana
    assert not ventana.menu_automatizacion.isVisible()
    assert ventana.menu_automatizacion.accion(INDEXAR).isCheckable()
    # Continuar y reiniciar estaban escondidos detrás de ese botón; son
    # acciones de esta ventana y se quedan a la vista.
    assert ventana.boton_continuar is not None
    assert ventana.boton_reiniciar is not None


def test_comprobar_cada_no_es_un_paso_de_la_automatizacion(app, tmp_path):
    """Esperar va dentro de subir; esto solo decide cada cuánto se pregunta.

    Apagarla para en el acto la comprobación periódica, pero no desmarca
    nada del menú: los pasos elegidos siguen siendo los mismos.
    """
    opciones = OpcionesAutomatizacion(tmp_path)
    ventana = AirVaultWindow(RAIZ, opciones)

    assert ventana.auto_check.isChecked()

    ventana.auto_check.setChecked(False)

    assert opciones.subir
    assert opciones.indexar
    assert ventana.menu_automatizacion.accion("subir").isChecked()
    ventana.close()


def test_la_ventana_usa_batch_en_sus_campos_y_tabla(ventana):
    assert not ventana.lotes.isHidden()
    assert [
        ventana.lotes.horizontalHeaderItem(columna).text()
        for columna in range(ventana.lotes.columnCount())
    ] == ["ID", "Batch", "Páginas", "Estado"]
    etiquetas = {
        etiqueta.text() for etiqueta in ventana.findChildren(QLabel)
    }
    assert "Nombre del batch:" in etiquetas
    assert ventana.lote_edit.placeholderText() == (
        "Se completa al elegir la ejecución"
    )


def test_la_cola_tiene_su_espacio_y_la_barra_bajo_el_header(app, ventana):
    """El historial ya no es tabla: el alto que ocupaba es de la cola."""
    from app.gui.widgets import FlatSelectionDelegate

    ventana.show()
    app.processEvents()
    tabla = ventana.lotes
    assert tabla.minimumHeight() == ventana._densidad.airvault_table_min_height
    assert isinstance(tabla.itemDelegate(), FlatSelectionDelegate)
    assert (
        f"margin-top: {tabla.horizontalHeader().height()}px"
        in tabla.verticalScrollBar().styleSheet()
    )


def test_revisar_airvault_esta_a_la_derecha_de_subir(ventana):
    botones = ventana.layout().itemAt(ventana.layout().count() - 1).layout()
    widgets = [
        botones.itemAt(indice).widget()
        for indice in range(botones.count())
        if botones.itemAt(indice).widget() is not None
    ]

    assert ventana.boton_revisar.text() == "Revisar en AirVault"
    assert widgets.index(ventana.boton_revisar) == (
        widgets.index(ventana.boton_subir) + 1
    )


def test_el_usuario_puede_elegir_el_limite_antes_de_subir(ventana, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    ventana.fijar_corrida(corrida(tmp_path))
    ventana.limite_batch_spin.setValue(450)

    estado = ventana._base_del_estado()

    assert estado["paginas_por_batch"] == 450
    assert json.loads((tmp_path / "airvault.json").read_text(
        encoding="utf-8"
    ))["paginas_por_batch"] == 450


def test_el_usuario_puede_activar_la_compresion_antes_de_subir(
    ventana, tmp_path,
):
    ventana.fijar_corrida(corrida(tmp_path))
    ventana.compresion_check.setChecked(True)

    estado = ventana._base_del_estado()

    assert estado["compresion"] is True


# ── el historial ───────────────────────────────────────────────────

def test_el_historial_lista_las_corridas_de_la_mas_reciente(app, tmp_path):
    corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    # La primera opción es con la que abre la lista, no una ejecución.
    assert ventana.historial.itemText(0) == "Seleccionar ejecución"
    assert ventana.historial.itemData(0) is None
    assert ventana.historial.minimumContentsLength() == 22
    assert ventana.historial.maxVisibleItems() == 12
    nombres = [
        ventana.historial.itemText(indice)
        for indice in range(1, ventana.historial.count())
    ]
    assert nombres == ["BITS 18 AUG 2026 05 42", "BITS 17 AUG 2026 05 50"]


def test_la_lista_ensena_el_nombre_y_el_resto_al_posarse_encima(app, tmp_path):
    """En la línea va el nombre y nada más: es lo que se busca al abrirla."""
    corrida(tmp_path, paginas=19)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()

    assert ventana.historial.itemText(1) == "BITS 18 AUG 2026 05 42"
    aviso = ventana.historial.itemData(1, Qt.ItemDataRole.ToolTipRole)
    assert aviso.startswith("BITS 18 AUG 2026 05 42\n")
    assert "19 pág." in aviso
    assert "1 archivo" in aviso


def test_elegir_del_historial_apunta_a_esa_corrida(app, tmp_path):
    corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    csv = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    ventana._al_elegir_del_historial(1)
    assert ventana.corrida() == csv
    assert ventana.lote_edit.text() == "DP | BITS 18 AUG 2026 05 42"


def test_sin_elegir_nada_se_propone_la_mas_reciente(app, tmp_path):
    corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    csv = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    assert ventana.corrida() == csv


def test_se_propone_la_exportada_aunque_no_sea_la_ultima(app, tmp_path):
    """Procesar sin exportar es normal; abrir señalando esa ejecución, no."""
    csv = corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    corrida(tmp_path, "BITS 18 AUG 2026 05 42", exportada=False)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    assert ventana.corrida() == csv
    assert ventana.boton_subir.isEnabled()


def test_una_corrida_sin_exportar_se_ve_pero_no_se_sube(app, tmp_path):
    """Sin PDF de entrega no hay nada que subir, y hay que decir por qué."""
    corrida(tmp_path, exportada=False)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    assert "Sin exportar" in ventana.historial.itemData(
        1, Qt.ItemDataRole.ToolTipRole
    )
    assert not ventana.boton_subir.isEnabled()
    assert "Expórtela" in ventana.resumen.text()


def test_una_corrida_exportada_se_puede_subir(app, tmp_path):
    corrida(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    assert ventana.boton_subir.isEnabled()
    assert "Sin subir" in ventana.resumen.text()


def test_sin_corridas_procesadas_lo_dice(app, tmp_path):
    """Una lista con solo su opción de abrir no dice que no hay nada."""
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    assert ventana.historial.count() == 1
    assert "No hay ejecuciones procesadas" in ventana.resumen.text()


def test_el_csv_que_se_sube_es_el_minimo_no_el_completo(tmp_path):
    """El ``_completo`` trae el detalle de lectura; AirVault no lo entiende."""
    csv = corrida(tmp_path)
    csv.with_name(f"{csv.stem}_completo.CSV").write_text("x\n", encoding="utf-8")
    assert csv_de_corrida(csv.parent.parent) == csv


# ── la ejecución elegida ─────────────────────────────────────────────

def test_apunta_a_la_corrida_y_propone_el_nombre_del_lote(ventana, tmp_path):
    csv = corrida(tmp_path)
    ventana.fijar_corrida(csv)
    assert ventana.corrida() == csv
    assert ventana.lote_edit.text() == "DP | BITS 18 AUG 2026 05 42"


def test_la_ejecucion_solo_se_elige_en_el_historial(ventana):
    """No hay forma de apuntar la ventana a un CSV cualquiera."""
    assert not hasattr(ventana, "corrida_edit")
    assert not hasattr(ventana, "boton_buscar")


def test_cambiar_de_corrida_tira_lo_que_se_sabia_de_la_anterior(ventana,
                                                                tmp_path):
    """Los batches de una ejecución no dicen nada de los de otra."""
    ventana._estado = {"planes": {"x": 1}}
    ventana.boton_indexar.setEnabled(True)
    ventana.fijar_corrida(corrida(tmp_path))
    assert ventana._estado == {}
    assert ventana._estados == []
    # Indexar tambien sirve para conectarse y recuperar batches que esta
    # aplicacion hubiera subido en una ejecucion anterior.
    assert ventana.boton_indexar.isEnabled()


def test_la_tabla_incluye_sin_subir_de_otras_ejecuciones(app, tmp_path):
    from app.airvault.flujo import SIN_SUBIR

    primera = corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    segunda = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    registrar_en_airvault(tmp_path, primera)
    registrar_en_airvault(tmp_path, segunda)
    ventana = AirVaultWindow(tmp_path)

    ventana.fijar_corrida(primera)

    assert len(ventana._trabajos) == 2
    assert all(estado.estado == SIN_SUBIR for estado in ventana._estados)
    assert {
        trabajo.manifiesto.job_id for trabajo in ventana._trabajos
    } == {
        primera.parent.parent.name,
        segunda.parent.parent.name,
    }
    ventana.close()


def test_eliminar_registro_reinicia_solo_el_estado_local(
    app, tmp_path, monkeypatch
):
    csv = corrida(tmp_path)
    pdf = csv.parent.parent / f"{csv.parent.parent.name}.pdf"
    manifiesto = registrar_en_airvault(tmp_path, csv)
    ventana = AirVaultWindow(tmp_path)
    ventana.fijar_corrida(csv)
    enviados = []

    def papelera(rutas):
        enviados.extend(rutas)
        for ruta in rutas:
            ruta.unlink()
        return list(rutas), []

    monkeypatch.setattr(
        "app.gui.airvault_window.send_to_trash", papelera
    )
    monkeypatch.setattr(
        "app.gui.airvault_window.QMessageBox.warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    assert ventana.boton_eliminar_registro.isEnabled()
    ventana._eliminar_registro()

    assert enviados == [manifiesto]
    assert not manifiesto.exists()
    assert csv.exists() and pdf.exists()
    assert ventana._trabajos == []
    assert ventana.lotes.rowCount() == 0
    assert not ventana.boton_eliminar_registro.isEnabled()
    assert "batches remotos no se modificaron" in ventana.resumen.text()


# ── no se arranca sin lo imprescindible ────────────────────────────

def test_sin_corrida_no_arranca_nada(ventana):
    ventana._subir()
    assert ventana._worker is None
    assert "Falta elegir la ejecución" in ventana.resumen.text()


def test_sin_nombre_de_lote_no_arranca_nada(ventana, tmp_path):
    ventana.fijar_corrida(corrida(tmp_path))
    ventana.lote_edit.setText("   ")
    ventana._subir()
    assert ventana._worker is None
    assert "Falta el nombre del batch" in ventana.resumen.text()


def test_indexar_sin_ningun_lote_listo_no_hace_nada(ventana):
    ventana._indexar()
    assert ventana._worker is None


def test_indexar_termina_primero_todas_las_subidas(ventana, monkeypatch):
    class ManifiestoPendiente:
        def etapa_hecha(self, nombre):
            return nombre != "subir"

    class TrabajoPendiente:
        manifiesto = ManifiestoPendiente()

    continuaciones = []
    ventana._trabajos = [TrabajoPendiente()]
    monkeypatch.setattr(
        ventana, "_continuar_pendiente", lambda: continuaciones.append(True)
    )

    ventana._indexar()

    assert continuaciones == [True]


# ── lo que cuenta al terminar ──────────────────────────────────────

class PlanFalso:
    batch_id = "003SRO"

    def resumen(self):
        return {"total": 5, "escribibles": 2, "bloqueadas": 1,
                "separadores": 2, "avisos_globales": 0}


class RegistroFalso:
    """Una pagina del manifiesto: lo justo para contarla y reiniciarla."""

    def __init__(self):
        self.estado = None
        self.avisos = []


class ManifiestoFalso:
    def __init__(self, nombre, paginas, batch_id):
        self.nombre_batch = nombre
        self.registros = [RegistroFalso() for _ in range(paginas)]
        self.batch_id = batch_id
        self.etapas = {}
        self.solo_subir = False
        self.amarillas_permitidas = False
        self.lotes_previos = []
        self.intentos_identificacion = 0
        self.espera_reenvio_desde = ""
        self.busquedas_amplias_sin_hallar = 0
        self.completado_automatico = False
        self.cancelado = False
        self.posible_duplicado = ""
        # Que una parte este verificada es lo que demuestra que AirVault
        # publico lo que se subio despues de otra, asi que la regla que da
        # una carga por perdida lo pregunta.
        self.verificado = False

    def bitacoras(self):
        """Las paginas que se indexan. En el doble no falta ningun campo."""
        return []

    def etapa_hecha(self, nombre):
        if nombre == "verificar":
            return self.verificado
        return nombre == "subir"


class TrabajoFalso:
    def __init__(self, nombre="DP | BITS", paginas=5, batch_id="003SRO",
                 carpeta="job"):
        self.manifiesto = ManifiestoFalso(nombre, paginas, batch_id)
        self.carpeta = carpeta
        # La regla de reenvio mira la espera configurada del propio trabajo,
        # igual que hace con un Trabajo de verdad.
        self.config = AirVaultConfig()
        self.guardados = 0

    def guardar(self):
        """Sacar un batch de la cola se anota en su manifiesto."""
        self.guardados += 1


def parte(estado, nombre="DP | BITS", detalle="", carpeta="job", lote=None):
    """Una fila de la lista de batches, como la devuelve el módulo."""
    from app.airvault.flujo import EstadoParte

    return EstadoParte(
        TrabajoFalso(nombre, carpeta=carpeta), estado, detalle, lote
    )


def cola_de_dos_ejecuciones(ventana):
    from app.airvault.flujo import LISTO

    actual = parte(LISTO, "Actual", carpeta="actual")
    anterior = parte(LISTO, "Anterior", carpeta="anterior")
    actual.trabajo.manifiesto.csv_origen = "C:/entregas/actual.csv"
    anterior.trabajo.manifiesto.csv_origen = "C:/entregas/anterior.csv"
    ventana._corrida = actual.trabajo.manifiesto.csv_origen
    ventana.lote_edit.setText("Actual")
    ventana._trabajos = [anterior.trabajo, actual.trabajo]
    ventana._estados = [anterior, actual]
    ventana._estado["planes"] = {"actual": object(), "anterior": object()}
    ventana._pintar_lotes()
    return actual, anterior


def test_filtrar_cola_conserva_correspondencia_de_filas_y_acciones(ventana):
    actual, anterior = cola_de_dos_ejecuciones(ventana)
    ventana.solo_ejecucion_check.setChecked(True)
    assert ventana.lotes.rowCount() == 1
    assert ventana.lotes.item(0, 1).text() == "Actual"
    ventana.lotes.selectRow(0)
    assert ventana._seleccionadas() == [actual]
    assert ventana._elegidas(0) == [actual]
    assert ventana._listos() == [actual.trabajo]
    assert ventana._ejecucion() == [actual.trabajo]
    assert [nombre for nombre, _registros in ventana._batches_de_la_cola()] == ["Actual"]
    ventana.solo_ejecucion_check.setChecked(False)
    assert ventana.lotes.rowCount() == 2
    assert ventana._seleccionadas() == []
    assert ventana._ejecucion() == [anterior.trabajo, actual.trabajo]


@pytest.mark.parametrize("modo,clave", [
    ("comprobar", "comprobar_trabajos"), ("indexar", "listos"),
    ("completar", "por_completar"), ("resubir", "pendientes_subida"),
])
def test_acciones_en_espera_excluyen_batches_ocultos(ventana, monkeypatch, modo, clave):
    actual, anterior = cola_de_dos_ejecuciones(ventana)
    ventana.solo_ejecucion_check.setChecked(True)
    llamadas = []
    monkeypatch.setattr(ventana, "_lanzar", lambda modo, estado: llamadas.append((modo, dict(estado))))
    assert ventana._ejecutar_accion(modo, [anterior.trabajo, actual.trabajo])
    assert llamadas[0][1][clave] == [actual.trabajo]
    assert llamadas[0][1]["trabajos"] == [actual.trabajo]
    assert not ventana._ejecutar_accion(modo, [anterior.trabajo])
    assert len(llamadas) == 1


def test_subir_filtrado_no_recupera_ejecuciones_anteriores(ventana, monkeypatch):
    actual, _anterior = cola_de_dos_ejecuciones(ventana)
    ventana.solo_ejecucion_check.setChecked(True)
    llamadas = []
    monkeypatch.setattr(ventana, "_lanzar", lambda modo, estado: llamadas.append(dict(estado)))
    ventana._subir_a_mano()
    assert llamadas[0]["trabajos"] == [actual.trabajo]
    assert llamadas[0]["recuperar_pendientes"] is False


def test_actualizacion_filtrada_conserva_ocultos_sin_meterlos_en_el_worker(ventana):
    actual, anterior = cola_de_dos_ejecuciones(ventana)
    ventana.solo_ejecucion_check.setChecked(True)
    ventana._al_actualizar_subidas({"trabajos": [actual.trabajo]})
    assert ventana._estado["trabajos"] == [actual.trabajo]
    assert anterior.trabajo in ventana._trabajos
    ventana.solo_ejecucion_check.setChecked(False)
    assert ventana.lotes.rowCount() == 2


def test_filtro_cambia_en_vuelo_sin_perder_lotes_ocultos(ventana):
    actual, anterior = cola_de_dos_ejecuciones(ventana)
    ventana.solo_ejecucion_check.setChecked(True)
    ventana._worker_filtrado = True

    ventana.solo_ejecucion_check.setChecked(False)
    ventana._al_actualizar_subidas({"trabajos": [actual.trabajo]})

    assert anterior.trabajo in ventana._trabajos
    assert ventana._estado["trabajos"] == [actual.trabajo]
    assert ventana.lotes.rowCount() == 2


def test_subir_no_indexa_nada_y_dice_que_falta_esperar(ventana):
    """Subir y estar listo son cosas distintas: entre medias está AirVault."""
    ventana._al_subir({"trabajos": [TrabajoFalso()], "cliente": object()})
    texto = ventana.resumen.text()
    assert "Subida terminada" in texto
    assert "procesar" in texto
    assert not ventana.boton_indexar.isEnabled()


def test_la_tabla_marca_subido_antes_de_que_airvault_devuelva_el_id(ventana):
    trabajo = TrabajoFalso(batch_id=None)

    ventana._al_actualizar_subidas({"trabajos": [trabajo]})

    assert ventana.lotes.item(0, 0).text() == ""
    assert "Subido pendiente confirmación" in (
        ventana.lotes.item(0, 3).text()
    )


def test_la_tabla_dice_subido_confirmado_al_encontrar_el_batch(ventana):
    from app.airvault.client import ResumenLote
    from app.airvault.flujo import LISTO

    confirmado = ResumenLote(
        batch_id="003SRO", nombre="DP | BITS", paginas=5, repo_id=3209,
        repositorio="MXDocs", paso="Web Index", bloqueado_por="",
        recibido="",
    )
    ventana._estados = [parte(LISTO, lote=confirmado)]

    ventana._pintar_lotes()

    texto = ventana.lotes.item(0, 3).text()
    assert "Subido confirmado" in texto
    assert "Listo para indexar" in texto


def test_cada_click_en_subir_confirma_los_batches_en_airvault(
    ventana, monkeypatch
):
    """La marca local de subida no basta para habilitar el indexado."""
    comprobaciones = []
    monkeypatch.setattr(
        ventana, "_comprobar", lambda: comprobaciones.append(True)
    )
    ventana.auto_check.setChecked(False)
    ventana._comprobar_al_terminar = False

    ventana._al_subir({"trabajos": [TrabajoFalso()], "cliente": object()})
    ventana._al_terminar()

    assert comprobaciones == [True]
    assert "confirma cada batch" in ventana.bitacora.item(
        ventana.bitacora.count() - 1
    ).text()


def test_la_lista_dice_en_que_va_cada_lote(ventana):
    from app.airvault.flujo import LISTO, PROCESANDO

    ventana._estados = [
        parte(LISTO, "DP | BITS", "5 paginas"),
        parte(PROCESANDO, "DP | BITS REVISAR", "2 de 5 paginas",
              carpeta="revisar"),
    ]
    ventana._pintar_lotes()
    assert ventana.lotes.rowCount() == 2
    assert ventana.lotes.item(0, 0).text() == "003SRO"
    assert ventana.lotes.item(0, 1).text() == "DP | BITS"
    assert ventana.lotes.item(0, 2).text() == "5"
    assert "Listo para indexar" in ventana.lotes.item(0, 3).text()
    assert "Procesándose" in ventana.lotes.item(1, 3).text()


def test_el_id_solo_aparece_cuando_airvault_lo_encuentra(ventana):
    from app.airvault.flujo import BUSCANDO

    sin_encontrar = parte(BUSCANDO)
    sin_encontrar.trabajo.manifiesto.batch_id = None
    ventana._estados = [sin_encontrar]

    ventana._pintar_lotes()

    assert ventana.lotes.item(0, 0).text() == ""


def test_batch_conserva_el_nombre_hasta_un_ancho_razonable(ventana):
    from app.airvault.flujo import LISTO

    nombre = "DP | BIT 18 AUG 2026 05 42 - DIVISION PRINCIPAL"
    ventana._estados = [parte(LISTO, nombre)]

    ventana._pintar_lotes()

    assert ventana.lotes.item(0, 1).text() == nombre
    assert ventana.lotes.item(0, 1).toolTip() == nombre
    assert ventana.lotes.columnWidth(1) == min(
        max(
            ventana.lotes.sizeHintForColumn(1) + 16,
            ANCHO_MINIMO_NOMBRE_BATCH,
        ),
        ANCHO_MAXIMO_NOMBRE_BATCH,
    )


def test_todos_los_batches_confirmados_quedan_activos_en_blanco(ventana):
    from app.airvault.flujo import LISTO, SOLO_REVISAR

    ventana._estados = [
        parte(LISTO, "DP | BIT"),
        parte(LISTO, "DP | BIT -2", carpeta="parte-02"),
        parte(SOLO_REVISAR, "DP | BIT REVISAR", carpeta="revisar"),
    ]

    ventana._pintar_lotes()

    assert all(
        ventana.lotes.item(fila, 0).foreground().style()
        is Qt.BrushStyle.NoBrush
        for fila in range(ventana.lotes.rowCount())
    )


def test_gris_solo_significa_sin_subir_y_subido_queda_blanco(ventana):
    from app.airvault.flujo import BUSCANDO, SIN_SUBIR
    from app.gui.tokens import paleta

    ventana._estados = [
        parte(SIN_SUBIR, "DP | FALTA"),
        parte(BUSCANDO, "DP | SUBIDO", carpeta="parte-02"),
    ]

    ventana._pintar_lotes()

    assert ventana.lotes.item(0, 0).foreground().color() == QColor(
        paleta().TEXT_TERTIARY
    )
    assert ventana.lotes.item(1, 0).foreground().style() is Qt.BrushStyle.NoBrush


def test_azul_hasta_completar_y_verde_al_terminar(ventana):
    from app.airvault.flujo import COMPLETADO, INDEXADO, INCOMPLETO, LISTO
    from app.gui.airvault_window import color_indexando

    activo = parte(LISTO)
    terminado = parte(COMPLETADO, carpeta="terminado")
    ventana._estados = [activo, terminado]
    ventana._al_batch_indexando(activo.trabajo, True)
    for columna in range(ventana.lotes.columnCount()):
        assert ventana.lotes.item(0, columna).foreground().color() == QColor(color_indexando())
        assert ventana.lotes.item(1, columna).foreground().color() == QColor(color_indexado())
    ventana._al_batch_indexando(activo.trabajo, False)
    assert ventana.lotes.item(0, 0).foreground().style() is Qt.BrushStyle.NoBrush
    for estado in (INDEXADO, INCOMPLETO):
        ventana._estados = [parte(estado)]
        ventana._pintar_lotes()
        assert ventana.lotes.item(0, 0).foreground().color() == QColor(color_indexando())


def test_cambiar_tema_repinta_estados_sin_perder_las_filas(ventana, app, monkeypatch):
    from app.airvault.flujo import COMPLETADO, LISTO, SIN_SUBIR
    from app.gui.airvault_window import color_indexando
    from app.gui.theme import aplicar_tema, install_application_theme
    from app.gui.tokens import TEMA_CLARO, TEMA_OSCURO, paleta

    monkeypatch.setattr("app.gui.theme.leer_tema", lambda: None)
    monkeypatch.setattr("app.gui.theme.guardar_tema", lambda nombre: True)
    install_application_theme(app)
    activo = parte(LISTO, "Activo", carpeta="activo")
    ventana._estados = [activo, parte(COMPLETADO), parte(SIN_SUBIR, carpeta="falta")]
    ventana._al_batch_indexando(activo.trabajo, True)
    ventana.lotes.selectRow(1)
    celdas = [ventana.lotes.item(fila, 0) for fila in range(3)]
    try:
        for nombre in (TEMA_CLARO, TEMA_OSCURO):
            aplicar_tema(nombre)
            assert [ventana.lotes.item(fila, 0) for fila in range(3)] == celdas
            assert ventana.lotes.currentRow() == 1
            colores = (color_indexando(), paleta().STATUS_OK, paleta().TEXT_TERTIARY)
            for celda, color in zip(celdas, colores):
                assert celda.foreground().color() == QColor(color)
    finally:
        aplicar_tema(TEMA_OSCURO)


def test_un_batch_parcial_no_se_pinta_como_terminado(ventana):
    from app.airvault.flujo import LISTO
    from app.airvault.model import EstadoEtapa, Etapa

    indexado = parte(LISTO)
    indexado.trabajo.manifiesto.etapas = {
        "indexar": Etapa(estado=EstadoEtapa.ERROR),
    }
    ventana._estados = [indexado]

    ventana._pintar_lotes()

    assert all(
        ventana.lotes.item(0, columna).foreground().color()
        != QColor(color_indexado())
        for columna in range(ventana.lotes.columnCount())
    )


def test_un_lote_listo_se_puede_indexar_y_dice_cuanto_escribiria(ventana):
    from app.airvault.flujo import LISTO

    listo = parte(LISTO)
    ventana._al_comprobar({
        "estados": [listo],
        "planes": {"job": (PlanFalso(), None)},
        "partes": [("DP | BIT 003SRO", PlanFalso())],
    })
    texto = ventana.resumen.text()
    assert "5 páginas" in texto and "2 se escribirían" in texto
    assert "Nada se ha escrito todavía" in texto
    assert ventana.boton_indexar.isEnabled()


def test_un_lote_listo_sin_plan_todavía_no_se_indexa(ventana):
    """El plan es lo que dice qué se escribiría; sin él no hay qué aprobar."""
    from app.airvault.flujo import LISTO

    ventana._al_comprobar({
        "estados": [parte(LISTO)], "planes": {}, "partes": [],
    })
    assert not ventana.boton_indexar.isEnabled()


def test_revisar_con_completar_marcado_encadena_lotes_ya_indexados(
    app, tmp_path,
):
    """El cierre releera AirVault aunque el indexado haya sido manual."""
    from app.airvault.flujo import INDEXADO

    ventana = AirVaultWindow(tmp_path)
    ventana.completar_check.setChecked(True)
    ventana._al_comprobar({
        "estados": [parte(INDEXADO)], "planes": {}, "partes": [],
    })

    assert ventana.boton_indexar.isEnabled()
    assert ventana._indexar_al_terminar
    ventana.close()


class ResultadoFalso:
    escritas, omitidas, fallidas = 2, 1, 0
    detalles: list = []
    interrumpido = ""


def test_al_indexar_cuenta_como_quedo_el_lote(ventana):
    ventana._al_indexar({"resultado": ResultadoFalso(), "validas": 2, "total": 3})
    texto = ventana.resumen.text()
    assert "Escritas 2" in texto and "2 de 3 páginas revisadas" in texto


def test_revisar_distingue_fin_del_guardado_y_revision_humana(ventana):
    ventana._al_indexar({"resultado": ResultadoFalso(), "validas": 3,
                         "total": 3, "incluye_revision": True})
    assert ventana.estado_label.text() == "Indexado terminado"
    assert "El guardado de REVISAR terminó" in ventana.resumen.text()
    assert "revisión humana" in ventana.resumen.text()


def test_un_indexado_cortado_dice_que_lo_que_falta_se_retoma(ventana):
    """Con la sesión o la red caídas, lo escrito no se pierde ni se repite."""
    class Cortado(ResultadoFalso):
        interrumpido = "La sesion de AirVault caduco."

    ventana._al_indexar({"resultado": Cortado(), "validas": 2, "total": 5})
    texto = ventana.resumen.text()
    assert "se cortó" in texto and "caduco" in texto
    assert "sin repetir lo escrito" in texto


def test_paginas_amarillas_agotan_sus_reintentos_y_el_proceso_termina(ventana):
    """Lo que requiere correccion manual no mantiene vivo el vigilante."""
    from app.airvault.flujo import LISTO

    ventana._estados = [parte(LISTO)]
    ventana._al_indexar({
        "resultado": ResultadoFalso(),
        "validas": 2,
        "total": 3,
        "incompleto": True,
    })

    assert ventana.estado_label.text() == "Indexado incompleto"
    assert ventana._vigilante is None or not ventana._vigilante.isActive()


def test_un_indexado_sin_confirmar_se_vuelve_a_comprobar_solo(ventana):
    """La verificación pudo leer de AirVault algo que aún no reflejaba.

    Sin esto el reloj se paraba si no quedaba otro batch esperando, y el
    batch se quedaba sin completar aunque sus datos ya estuvieran.
    """
    from app.airvault.flujo import INCOMPLETO, LISTO
    from app.gui.airvault_window import RECONFIRMACIONES_TRAS_INDEXAR

    incompleto = {
        "resultado": ResultadoFalso(), "validas": 2, "total": 3,
        "incompleto": True, "carpetas": ["job"],
    }
    ventana._estados = [parte(LISTO)]
    ventana._al_indexar(dict(incompleto))

    assert ventana._vigilante.isActive()
    assert "Se vuelve a revisar solo" in ventana.resumen.text()

    for _ in range(RECONFIRMACIONES_TRAS_INDEXAR - 1):
        ventana._al_comprobar({
            "estados": [parte(INCOMPLETO)], "planes": {}, "partes": [],
        })
        assert ventana._vigilante.isActive()
    ventana._al_comprobar({
        "estados": [parte(INCOMPLETO)], "planes": {}, "partes": [],
    })
    assert ventana._vigilante.isActive()

    # Sigue consultando aunque se agoten los reintentos de escritura.
    ventana._al_indexar(dict(incompleto))
    assert ventana._vigilante.isActive()
    ventana.close()


def test_el_batch_confirmado_deja_de_contar_comprobaciones(ventana):
    from app.airvault.flujo import INDEXADO, LISTO

    ventana._estados = [parte(LISTO)]
    ventana._al_indexar({
        "resultado": ResultadoFalso(), "validas": 2, "total": 3,
        "incompleto": True, "carpetas": ["job"],
    })
    ventana._al_comprobar({
        "estados": [parte(INDEXADO)], "planes": {}, "partes": [],
    })

    assert ventana._reconfirmaciones == {}
    ventana.close()


def test_indexar_desde_la_cola_no_encadena_una_revision_global(ventana):
    """Una accion acotada termina sin volver a recorrer toda la tabla."""
    from app.airvault.flujo import PROCESANDO

    ventana._estados = [parte(PROCESANDO, carpeta="otro-job")]
    ventana._ajustar_vigilancia()
    assert ventana._vigilante.isActive()

    ventana._al_indexar({
        "resultado": ResultadoFalso(),
        "validas": 3,
        "total": 3,
        "incompleto": False,
        "acotado": True,
    })

    assert not ventana._comprobar_al_terminar
    assert not ventana._vigilante.isActive()


# ── la espera de AirVault ──────────────────────────────────────────

def test_mientras_falte_un_lote_se_sigue_preguntando_solo(ventana):
    """Un batch tarda en salir de la cola; nadie va a estar pulsando."""
    from app.airvault.flujo import PROCESANDO

    ventana._al_comprobar({
        "estados": [parte(PROCESANDO, detalle="2 de 5 paginas")],
        "planes": {}, "partes": [],
    })
    assert ventana._vigilante is not None
    assert ventana._vigilante.isActive()
    assert ventana._vigilante.interval() == 2 * 60_000


def estancada(
    nombre="DP | BITS SIN PUBLICAR",
    carpeta="job",
    subida="2020-01-01T00:00:00",
    espera="2020-01-01T00:00:00",
):
    """Una carga que Quick Upload acepto y AirVault nunca publico."""
    from app.airvault.flujo import BUSCANDO
    from app.airvault.model import EstadoEtapa, Etapa

    fila = parte(BUSCANDO, nombre, carpeta=carpeta)
    fila.trabajo.manifiesto.etapas["subir"] = Etapa(
        estado=EstadoEtapa.HECHA,
        actualizada=subida,
    )
    fila.trabajo.manifiesto.intentos_identificacion = 3
    fila.trabajo.manifiesto.espera_reenvio_desde = espera
    return fila


def indexada(nombre, carpeta, subida):
    """Una parte que AirVault si publico y el programa ya verifico."""
    from app.airvault.flujo import INDEXADO
    from app.airvault.model import EstadoEtapa, Etapa

    fila = parte(INDEXADO, nombre, carpeta=carpeta)
    fila.trabajo.manifiesto.etapas["subir"] = Etapa(
        estado=EstadoEtapa.HECHA, actualizada=subida,
    )
    fila.trabajo.manifiesto.verificado = True
    return fila


def test_una_subida_que_no_aparece_se_avisa_y_no_se_manda_sola(ventana):
    """Insistir es como acaban varias copias del mismo batch en la cola.

    Las tres señales que dan una carga por perdida pueden dispararse
    mientras AirVault todavía la está procesando, así que la decisión de
    volver a mandarla es de quien mira Web Index: es el único que distingue
    «no llegó» de «va lento».
    """
    ventana._al_comprobar({
        "estados": [estancada()], "planes": {}, "partes": [],
    })

    assert "probable que la carga no vaya a aparecer" in ventana.resumen.text()
    assert "No se vuelve a mandar solo" in ventana.resumen.text()
    assert "Subir a AirVault ahora" in ventana.resumen.text()
    assert not ventana._subir_al_terminar
    # El aviso dice el tiempo de espera configurado, sin multiplicarlo.
    assert "30 minutos" in ventana.resumen.text()


def test_sin_comprobacion_automatica_la_resubida_se_pide_a_mano(ventana):
    ventana.auto_check.setChecked(False)
    ventana._al_comprobar({
        "estados": [estancada()], "planes": {}, "partes": [],
    })

    assert "Subir a AirVault" in ventana.resumen.text()
    assert not ventana._subir_al_terminar


def test_una_carga_perdida_se_sigue_mirando_en_la_cola(ventana):
    """No mandarla otra vez no es dejar de buscarla.

    AirVault publica cargas horas después de aceptarlas. Parar el reloj al
    darla por perdida dejaba el batch en la tabla esperando a que alguien
    volviera a pulsar, cuando lo único que hacía falta era seguir mirando:
    preguntar no escribe nada y no duplica nada.
    """
    ventana._al_comprobar({
        "estados": [estancada()], "planes": {}, "partes": [],
    })

    assert ventana._vigilante is not None and ventana._vigilante.isActive()
    assert "Se sigue mirando la cola" in ventana.resumen.text()


def test_un_archivo_sin_subir_no_se_queda_esperando(ventana):
    """El reloj solo preguntaba; la fila sin subir no salia nunca de ahi."""
    from app.airvault.flujo import SIN_SUBIR

    ventana._al_comprobar({
        "estados": [parte(SIN_SUBIR, detalle="todavía sin subir")],
        "planes": {}, "partes": [],
    })

    assert ventana._subir_al_terminar
    assert ventana._estado["pendientes_subida"]
    assert ventana._vigilante is not None and ventana._vigilante.isActive()


def test_una_subida_fallida_no_se_reintenta_hasta_la_vuelta_siguiente(
    ventana,
):
    """Comprobar y subir se llamarian el uno al otro sin parar."""
    from app.airvault.flujo import SIN_SUBIR

    datos = {
        "estados": [parte(SIN_SUBIR)], "planes": {}, "partes": [],
    }
    ventana._al_comprobar(datos)
    assert ventana._subir_al_terminar
    ventana._subir_al_terminar = False

    # La subida falla y la fila vuelve a aparecer sin subir en la misma vuelta.
    ventana._al_comprobar(datos)
    assert not ventana._subir_al_terminar

    # El reloj da permiso otra vez.
    ventana._subidas_del_ciclo.clear()
    ventana._al_comprobar(datos)
    assert ventana._subir_al_terminar


def test_cuando_no_queda_nada_que_esperar_deja_de_preguntar(ventana):
    """Ya listo, AirVault no va a cambiarlo solo: preguntar sobra."""
    from app.airvault.flujo import LISTO

    ventana._al_comprobar({
        "estados": [parte(LISTO)], "planes": {}, "partes": [],
    })
    assert ventana._vigilante is None or not ventana._vigilante.isActive()


def test_sin_la_comprobacion_automatica_no_pregunta_sola(ventana):
    from app.airvault.flujo import PROCESANDO

    ventana._estados = [parte(PROCESANDO)]
    ventana._ajustar_vigilancia()
    assert ventana._vigilante.isActive()
    ventana.auto_check.setChecked(False)
    assert not ventana._vigilante.isActive()


def test_cerrar_la_ventana_no_apaga_la_comprobacion_automatica(ventana):
    """Esperar a AirVault lleva horas; lo normal es cerrarla mientras tanto."""
    from app.airvault.flujo import PROCESANDO

    ventana._estados = [parte(PROCESANDO)]
    ventana._ajustar_vigilancia()
    ventana.close()
    assert ventana._vigilante.isActive()


def test_el_reloj_no_pregunta_con_algo_en_vuelo(ventana):
    """Dos trabajos a la vez contra el mismo batch se estorban."""
    ventana._worker = HiloFalso()
    try:
        ventana._comprobar_solo()
        assert ventana._worker is not None
        assert isinstance(ventana._worker, HiloFalso)
    finally:
        ventana._worker = None


def test_un_fallo_suelto_no_para_la_comprobacion_automatica(ventana):
    """AirVault falla un rato y sigue; parar ahí mataba la ejecución.

    La sesión se renueva sola y un error del servidor no se repite
    necesariamente en el intervalo siguiente. Nadie está delante para
    volver a pulsar, así que el reloj sigue.
    """
    from app.airvault.flujo import PROCESANDO

    ventana._estados = [parte(PROCESANDO)]
    ventana._ajustar_vigilancia()
    assert ventana._vigilante.isActive()

    ventana._al_fallar("La sesion de AirVault caduco.")

    assert ventana._vigilante.isActive()
    assert ventana._fallos_seguidos == 1


def test_fallar_siempre_acaba_parando_la_comprobacion_automatica(ventana):
    """Repetir el mismo error toda la tarde no lo arregla."""
    from app.airvault.flujo import PROCESANDO
    from app.gui.airvault_window import FALLOS_SEGUIDOS_ANTES_DE_PARAR

    ventana._estados = [parte(PROCESANDO)]
    ventana._ajustar_vigilancia()
    for _ in range(FALLOS_SEGUIDOS_ANTES_DE_PARAR):
        ventana._al_fallar("La sesion de AirVault caduco.")

    assert not ventana._vigilante.isActive()


def test_una_comprobacion_buena_borra_la_racha_de_fallos(ventana):
    """Lo que llevaba fallando dejó de fallar: se empieza a contar de cero."""
    from app.airvault.flujo import PROCESANDO

    ventana._estados = [parte(PROCESANDO)]
    ventana._ajustar_vigilancia()
    ventana._al_fallar("AirVault no contestó.")

    ventana._al_comprobar({
        "estados": [parte(PROCESANDO)], "planes": {}, "partes": [],
    })

    assert ventana._fallos_seguidos == 0
    assert ventana._vigilante.isActive()


# ── completar el batch ─────────────────────────────────────────────

class Cierre:
    def __init__(self, completado, detalle="", quitadas=()):
        self.completado = completado
        self.detalle = detalle
        self.bloqueadas: list = []
        self.paginas = 5
        self.quitadas = list(quitadas)


def test_si_el_lote_se_cerro_se_dice(ventana):
    ventana._al_indexar({
        "resultado": ResultadoFalso(), "validas": 2, "total": 2, "lotes": 1,
        "cierres": [(TrabajoFalso(), Cierre(True))],
    })
    assert "completó en AirVault" in ventana.resumen.text()
    assert "se mandó a Web Search" in ventana.resumen.text()


def test_si_airvault_no_deja_cerrarlo_se_dice_por_que(ventana):
    """Es lo que hay que mirar: qué página falta completar y dónde."""
    ventana._al_indexar({
        "resultado": ResultadoFalso(), "validas": 2, "total": 3, "lotes": 1,
        "cierres": [(TrabajoFalso(), Cierre(
            False, "2 de 5 paginas no estan en verde (3, 4)"
        ))],
    })
    texto = ventana.resumen.text()
    assert "no se pudo completar" in texto
    assert "no estan en verde" in texto


def test_si_hubo_que_quitar_separadores_se_dice(ventana):
    """Es un cambio en el batch: esas páginas ya no están en AirVault."""
    ventana._al_indexar({
        "resultado": ResultadoFalso(), "validas": 2, "total": 2, "lotes": 1,
        "cierres": [(TrabajoFalso(), Cierre(True, quitadas=[1, 4, 6]))],
    })
    texto = ventana.resumen.text()
    assert "completó en AirVault" in texto
    assert "3 páginas separadoras" in texto


def test_sin_completar_marcado_no_se_dice_nada_de_cerrar(ventana):
    ventana._al_indexar({
        "resultado": ResultadoFalso(), "validas": 2, "total": 3, "lotes": 1,
        "cierres": [],
    })
    assert "cerr" not in ventana.resumen.text()


def test_el_fallo_se_cuenta_donde_se_lee(ventana):
    ventana._al_fallar("La sesion de AirVault caduco.")
    assert "caduco" in ventana.resumen.text()


def test_el_avance_sale_por_la_barra_de_la_ventana(ventana):
    """La ventana dibuja su propio avance: ya no cuelga de la principal."""
    ventana._mostrar_paso("Escribiendo en AirVault", 30, 120)
    assert ventana.estado_label.text() == "Escribiendo en AirVault"
    assert ventana.progreso.maximum() == 120
    assert ventana.progreso.value() == 30


# ── mientras escribe ───────────────────────────────────────────────

def test_con_un_lote_a_medias_otra_ejecucion_sigue_disponible(ventana):
    ventana.completar_check.setChecked(False)
    ventana._habilitar(False)
    assert ventana.historial.isEnabled()
    assert ventana.detener_duplicados_check.isEnabled()
    assert ventana.completar_check.isEnabled()
    assert ventana.solo_ejecucion_check.isEnabled()
    assert not ventana.boton_subir.isEnabled()
    assert not ventana.boton_comprobar.isEnabled()

    ventana.completar_check.setChecked(True)
    assert ventana._estado["completar"] is True


def test_politica_de_duplicados_cambia_en_trabajos_en_vuelo(ventana):
    trabajo = TrabajoFalso()
    ventana._trabajos = [trabajo]
    ventana._habilitar(False)
    nueva = not ventana.detener_duplicados_check.isChecked()

    ventana.detener_duplicados_check.setChecked(nueva)

    assert trabajo.config.detener_por_duplicados is nueva
    assert ventana._estado["config"].detener_por_duplicados is nueva


def test_elegir_otra_ejecucion_en_marcha_la_abre_en_paralelo(
    ventana, tmp_path, monkeypatch
):
    primera = corrida(tmp_path, "BITS 17 AUG 2026 05 50")
    segunda = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    ventana.fijar_corrida(primera)
    # La ventana de esta prueba mira el historial real del repositorio, así
    # que la otra ejecución se pone a mano en la lista.
    ventana.historial.clear()
    ventana.historial.addItem("Seleccionar ejecución")
    ventana.historial.addItem("otra", str(segunda))
    solicitadas = []
    ventana.abrir_corrida_paralela.connect(solicitadas.append)
    monkeypatch.setattr(ventana, "hilo", lambda: object())

    ventana._al_elegir_del_historial(1)

    assert solicitadas == [str(segunda)]
    assert ventana.corrida() == primera


def test_mientras_trabaja_siempre_hay_algo_que_pulsar(ventana):
    """Una espera larga no puede dejar la ventana sin salida.

    Entrar a AirVault espera hasta cinco minutos y un batch puede tardar
    quince en salir de la cola del servidor. Con todo apagado, eso era una
    ventana congelada sin nada que hacer más que matar el programa.
    """
    ventana._habilitar(False)
    assert ventana.boton_cancelar.isEnabled()
    assert ventana.boton_cerrar.isEnabled()
    ventana._habilitar(True)
    assert not ventana.boton_cancelar.isEnabled()


class HiloFalso:
    """Un hilo en marcha al que se le puede pedir que pare."""

    def __init__(self) -> None:
        self.interrumpido = False

    @staticmethod
    def isRunning() -> bool:
        return True

    def cancelar(self) -> None:
        self.interrumpido = True


def test_cerrar_con_trabajo_en_vuelo_lo_cancela_en_vez_de_negarse(ventana):
    """Antes se negaba a cerrar, y el trabajo podía estar esperando minutos.

    La ventana se quedaba sin salida: ni cerraba, ni avanzaba, ni había
    nada que pulsar. Ahora se pide la cancelación y se cierra en cuanto el
    hilo suelta los batches que tuviera tomados, que es lo único que no se
    puede dejar a medias.
    """
    hilo = HiloFalso()
    ventana._worker = hilo
    try:
        ventana.close()
        assert hilo.interrumpido
        assert ventana._cerrar_al_terminar
        assert "Cancelando" in ventana.resumen.text()
    finally:
        ventana._worker = None
        ventana._cerrar_al_terminar = False


def test_cancelar_le_pide_al_hilo_que_pare_sin_esperarlo(ventana):
    """Esperar al hilo aquí congelaría justo lo que se quiere destrabar."""
    hilo = HiloFalso()
    ventana._worker = hilo
    try:
        ventana._cancelar()
        assert hilo.interrumpido
        assert not ventana.boton_cancelar.isEnabled()
        assert "Cancelando" in ventana.estado_label.text()
    finally:
        ventana._worker = None


def test_una_etapa_sin_cuenta_deja_la_barra_en_marcha(ventana):
    """Parada en cero se lee como que no está pasando nada."""
    ventana._mostrar_paso("Entrando a AirVault", 0, 0)
    assert ventana.progreso.maximum() == 0
    assert ventana.progreso.minimum() == 0


def test_la_bitacora_cuenta_los_pasos_y_no_repite_el_mismo(ventana):
    """Los avisos de una subida llegan por trozo; la bitácora cuenta etapas."""
    ventana._mostrar_paso("Subiendo entrega.pdf", 1, 40)
    ventana._mostrar_paso("Subiendo entrega.pdf", 2, 40)
    ventana._mostrar_paso("Buscando el batch", 0, 0)
    assert ventana.bitacora.count() == 2
    assert "Subiendo entrega.pdf" in ventana.bitacora.item(0).text()
    assert "Buscando el batch" in ventana.bitacora.item(1).text()


def test_cancelado_se_cuenta_y_no_deja_la_barra_girando(ventana):
    ventana._al_cancelar()
    assert "canceló" in ventana.resumen.text()
    assert "desbloqueados" in ventana.resumen.text()
    assert ventana.progreso.maximum() == 100


# ── integración con la ventana principal ───────────────────────────

def test_las_acciones_quedan_compactas_sin_panel_avanzado(app):
    from app.gui.main_window import MainWindow

    principal = MainWindow()
    try:
        fila = principal._fleet_row
        widgets = [fila.itemAt(i).widget() for i in range(fila.count())]
        assert principal.btn_airvault in widgets
        assert isinstance(principal.btn_automatico, QToolButton)
        assert principal.btn_automatico.menu() is principal.menu_automatizacion
        for desplegable in (
            principal.input_actions_button,
            principal.template_actions_button,
            principal.view_button,
            principal.more_actions_button,
        ):
            assert isinstance(desplegable, QToolButton)
            assert desplegable.menu() is not None
            assert desplegable.popupMode() == (
                QToolButton.ToolButtonPopupMode.InstantPopup
            )
        assert not hasattr(principal, "advanced_btn")
        assert principal._reference_page == 1
        assert principal._effective_threads() == max(
            1, available_cpu_threads() - 1
        )
        assert principal._airvault_window is None
    finally:
        principal.close()


def test_el_cierre_espera_al_lote_a_medio_escribir(app):
    """Destruir el hilo con un batch a medias mata el programa."""
    from app.gui.main_window import MainWindow

    class HiloFalso:
        @staticmethod
        def isRunning() -> bool:
            return True

    principal = MainWindow()
    try:
        principal._open_airvault()
        falso = HiloFalso()
        with patch.object(
            principal._airvault_window, "hilo", return_value=falso
        ):
            assert falso in principal._running_workers()
    finally:
        principal.close()


def test_abre_otro_hilo_de_airvault_si_el_anterior_sigue_activo(app):
    """Cada ejecución conserva su ventana y puede avanzar simultáneamente."""
    from app.gui.main_window import MainWindow

    principal = MainWindow()
    try:
        principal._open_airvault()
        primera = principal._airvault_window
        with patch.object(primera, "hilo", return_value=object()):
            principal._open_airvault()

        assert len(principal._airvault_windows) == 2
        assert principal._airvault_window is not primera
    finally:
        principal.close()


def test_al_terminar_una_exportacion_se_apunta_a_esa_corrida(app, tmp_path):
    """Aunque la ventana no se haya abierto todavía: se guarda para cuando."""
    from app.gui.main_window import MainWindow

    principal = MainWindow()
    try:
        destino = tmp_path / "BITS 18 AUG 2026 05 42"
        (destino / "datos").mkdir(parents=True)
        principal._outputs_context = "export"
        principal._on_outputs_written(destino)
        assert str(principal._airvault_corrida).endswith(
            "BITS 18 AUG 2026 05 42.CSV"
        )
        principal._open_airvault()
        assert str(principal._airvault_window.corrida()).endswith(
            "BITS 18 AUG 2026 05 42.CSV"
        )
    finally:
        principal.close()


# ── el procesamiento ya no exporta ─────────────────────────────────

def test_procesar_guarda_los_datos_pero_no_arma_los_pdfs(app):
    """Los PDF son la entrega, y se arman al exportar, no al procesar.

    Componerlos vuelve a abrir cada original y tarda; imponerlo al terminar
    el OCR obligaba a esperarlo a quien todavía iba a cambiar la separación
    y a exportar otra vez.
    """
    from app.models.schemas import ValidationReport
    from app.gui.main_window import MainWindow

    principal = MainWindow()
    try:
        reportes = [ValidationReport(pdf_path="a.pdf", template_name="x")]
        with patch.object(MainWindow, "_start_outputs") as salidas:
            principal._on_succeeded(reportes)
        salidas.assert_called_once()
        assert salidas.call_args.kwargs["skip_pdfs"] is True
        assert salidas.call_args.kwargs["context"] == "proceso"
    finally:
        principal.close()


def test_exportar_si_arma_los_pdfs(app):
    from app.models.schemas import ValidationReport
    from app.gui.main_window import MainWindow

    principal = MainWindow()
    try:
        principal._reports = [ValidationReport(pdf_path="a.pdf",
                                               template_name="x")]
        with patch.object(MainWindow, "_start_outputs") as salidas:
            principal._exportar()
        salidas.assert_called_once()
        assert salidas.call_args.kwargs.get("skip_pdfs", False) is False
        assert salidas.call_args.kwargs["context"] == "export"
    finally:
        principal.close()


# ── el hilo se puede parar ─────────────────────────────────────────

def test_cancelar_corta_dentro_de_una_subida_de_mil_trozos(app):
    """La cancelación entra por donde el trabajo cuenta lo que va haciendo.

    Así se puede parar dentro de una subida por trozos o de una espera de
    quince minutos sin que el recorrido del indexado sepa que existe un
    botón de cancelar.
    """
    from app.gui.airvault_window import TrabajoAirVaultWorker, TrabajoCancelado

    worker = TrabajoAirVaultWorker("comprobar", {})
    worker._avisar("Subiendo entrega.pdf", 1, 40)
    worker.cancelar()
    with pytest.raises(TrabajoCancelado):
        worker._avisar("Subiendo entrega.pdf", 2, 40)


def test_cancelar_corta_la_espera_del_lote(app):
    """Dormir de una vez dejaba el botón sin efecto hasta el otro sondeo."""
    from app.gui.airvault_window import TrabajoAirVaultWorker, TrabajoCancelado

    worker = TrabajoAirVaultWorker("comprobar", {})
    worker.cancelar()
    with pytest.raises(TrabajoCancelado):
        worker._dormir(600.0)


def test_cancelar_corta_tambien_la_sesion_que_esta_esperando(app):
    """Entre dos pasos puede haber tres minutos de espera al servidor.

    La bandera del hilo se mira entre paso y paso. Si el paso es una
    petición, entre uno y otro caben sesenta segundos por intento, tres
    intentos y sus esperas: cancelar no se notaba hasta entonces.
    """
    from app.gui.airvault_window import TrabajoAirVaultWorker

    class SesionFalsa:
        def __init__(self):
            self.cancelada = False

        def cancelar(self):
            self.cancelada = True

    sesion = SesionFalsa()
    worker = TrabajoAirVaultWorker("comprobar", {"sesion": sesion})

    worker.cancelar()

    assert sesion.cancelada
    assert worker.hay_que_parar()


def test_cancelar_sin_sesion_abierta_no_falla(app):
    """Se puede cancelar antes de que llegue a entrar a AirVault."""
    from app.gui.airvault_window import TrabajoAirVaultWorker

    worker = TrabajoAirVaultWorker("comprobar", {})
    worker.cancelar()
    assert worker.hay_que_parar()


def test_soltar_los_batches_reanuda_la_sesion_cancelada(app, tmp_path):
    """Soltar es trabajo que existe *porque* se canceló.

    Con la sesión cortada, cada petición se negaría y los batches se
    quedarían tomados en AirVault, que es justo lo que soltarlos evita.
    """
    class SesionFalsa:
        def __init__(self):
            self.cancelada = True

        def reanudar(self):
            self.cancelada = False

    ventana = AirVaultWindow(tmp_path)
    try:
        sesion = SesionFalsa()
        soltados = []
        ventana._estado["sesion"] = sesion
        ventana._estado["cliente"] = object()
        ventana._trabajos = [object()]
        with patch(
            "app.gui.airvault_window._soltar",
            lambda trabajos, cliente: soltados.append(trabajos),
        ):
            ventana._soltar_lotes(esperar=True)

        assert not sesion.cancelada
        assert len(soltados) == 1
    finally:
        ventana._trabajos = []
        ventana.close()


def test_una_cancelacion_no_se_confunde_con_el_fallo_de_una_pagina(app):
    """Por eso no es una ``Exception``.

    El indexado atrapa ``Exception`` alrededor de cada escritura para anotar
    la página que falló y seguir con las demás; una cancelación ahí quedaría
    anotada como si una bitácora estuviera rota.
    """
    from app.gui.airvault_window import TrabajoCancelado

    assert not issubclass(TrabajoCancelado, Exception)


def test_una_carga_que_rebasaron_las_siguientes_se_da_por_perdida(ventana):
    """Lo que se subio despues ya esta indexado: no hay nada que esperar.

    Sin esta regla la fila se quedaba en «Subido pendiente confirmación»
    hasta que alguien la mirara: la espera del reloj empieza cuando el
    programa se da cuenta, y en una ejecución recién abierta eso es ahora.
    Darla por perdida es lo que avisa; mandarla otra vez sigue siendo una
    orden a mano.
    """
    ahora = datetime.now()
    perdida = estancada(
        "DP | BITS -2",
        carpeta="job-2",
        subida=(ahora - timedelta(minutes=2)).isoformat(timespec="seconds"),
        espera=ahora.isoformat(timespec="seconds"),
    )
    siguiente = indexada(
        "DP | BITS -3", "job-3", ahora.isoformat(timespec="seconds")
    )

    ventana._al_comprobar({
        "estados": [perdida, siguiente], "planes": {}, "partes": [],
    })

    assert "ya están indexadas" in ventana.resumen.text()
    assert "No se vuelve a mandar solo" in ventana.resumen.text()
    assert not ventana._subir_al_terminar


def test_un_batch_que_cerro_el_programa_se_pinta_como_terminado(ventana):
    """El autocompletado es un final, no algo que quede por hacer."""
    from app.airvault.flujo import AUTOCOMPLETADO

    fila = parte(AUTOCOMPLETADO, "DP | BITS", "lo cerro el programa")

    ventana._estados = [fila]
    ventana._pintar_lotes()

    assert fila.se_acabo
    assert not ventana._falta_esperar()
    assert ventana.lotes.item(0, 3).text().startswith("Terminado por el programa")
    assert ventana.lotes.item(0, 0).foreground().color() == QColor(color_indexado())


def _acciones(ventana, *filas):
    """El menú de esas filas de la cola, por acción."""
    elegidas = [ventana._estados[f] for f in (filas or (0,))]
    menu = ventana._acciones_de_la_cola(elegidas)
    return {
        accion.text(): accion for accion in menu.actions()
        if not accion.isSeparator()
    }


def test_la_cola_ofrece_subir_solo_el_batch_que_falta(ventana):
    """Clic derecho sobre una fila: la acción es de esa fila, no de todas."""
    from app.airvault.flujo import INDEXADO, SIN_SUBIR

    ventana._estados = [
        parte(SIN_SUBIR, "DP | BITS -1", carpeta="job-1"),
        parte(INDEXADO, "DP | BITS -2", carpeta="job-2"),
    ]
    ventana._pintar_lotes()

    acciones = _acciones(ventana, 0)

    assert acciones["Subir a AirVault ahora"].isEnabled()
    assert not acciones["Completar el batch"].isEnabled()
    assert not acciones["Indexar ahora"].isEnabled()
    assert "Cancelar en la cola" in acciones


def test_la_cola_ofrece_completar_el_que_ya_esta_indexado(ventana):
    from app.airvault.flujo import INDEXADO

    ventana._estados = [parte(INDEXADO, "DP | BITS", carpeta="job-1")]
    ventana._pintar_lotes()

    acciones = _acciones(ventana)

    assert acciones["Completar el batch"].isEnabled()
    assert not acciones["Subir a AirVault ahora"].isEnabled()


def test_revisar_desde_la_cola_solo_encola_los_batches_elegidos(ventana):
    """El menu contextual no hereda el alcance global del boton inferior."""
    from app.airvault.flujo import PROCESANDO

    primera = parte(PROCESANDO, "DP | BITS -1", carpeta="job-1")
    segunda = parte(PROCESANDO, "DP | BITS -2", carpeta="job-2")
    ventana._estados = [primera, segunda]
    encoladas = []
    ventana._encolar = lambda modo, trabajos, texto: encoladas.append(
        (modo, list(trabajos), texto)
    )

    acciones = _acciones(ventana, 0)
    acciones["Revisar en AirVault"].trigger()

    assert encoladas[0][0] == "comprobar"
    assert encoladas[0][1] == [primera.trabajo]


def test_boton_inferior_conserva_el_alcance_global(ventana):
    """El boton de abajo elimina cualquier seleccion contextual anterior."""
    estado = {"comprobar_trabajos": [object()]}
    lanzados = []
    ventana._base_del_estado = lambda: estado
    ventana._lanzar = lambda modo, datos: lanzados.append((modo, datos))

    ventana._comprobar()

    assert "comprobar_trabajos" not in estado
    assert lanzados == [("comprobar", estado)]


def test_una_revision_acotada_no_reemplaza_las_otras_filas(ventana):
    """La respuesta parcial se mezcla con la tabla completa por carpeta."""
    from app.airvault.flujo import LISTO, PROCESANDO

    primera = parte(PROCESANDO, "DP | BITS -1", carpeta="job-1")
    segunda = parte(PROCESANDO, "DP | BITS -2", carpeta="job-2")
    revisada = parte(LISTO, "DP | BITS -1", carpeta="job-1")
    ventana._trabajos = [primera.trabajo, segunda.trabajo]
    ventana._estado["trabajos"] = list(ventana._trabajos)
    ventana._estados = [primera, segunda]

    ventana._al_comprobar({
        "estados": [revisada],
        "planes": {"job-1": (PlanFalso(), None)},
        "partes": [("DP | BITS -1", PlanFalso())],        "acotado": True,
    })

    assert [fila.nombre for fila in ventana._estados] == [
        "DP | BITS -1", "DP | BITS -2",
    ]
    assert ventana._estados[0].estado == LISTO
    assert ventana._estados[1] is segunda


def test_cancelar_saca_el_batch_de_la_cola_sin_deshacer_nada(ventana):
    """Cancelar es dejar de trabajar en él, no borrar lo hecho."""
    from app.airvault.flujo import CANCELADO, SIN_SUBIR

    fila = parte(SIN_SUBIR, "DP | BITS", carpeta="job-1")
    fila.trabajo.manifiesto.batch_id = "003SRO"
    ventana._estados = [fila]
    ventana._pintar_lotes()

    ventana._cancelar_una(fila, True)

    assert fila.trabajo.manifiesto.cancelado
    assert fila.trabajo.manifiesto.batch_id == "003SRO"
    assert ventana._estados[0].estado == CANCELADO
    assert ventana._estados[0].se_acabo
    # Cancelado deja de contar como algo que esperar: el reloj se para.
    assert not ventana._falta_esperar()
    # Y el menú pasa a ofrecer lo contrario.
    acciones = _acciones(ventana)
    assert "Reanudar en la cola" in acciones
    assert not acciones["Subir a AirVault ahora"].isEnabled()


def test_reanudar_devuelve_el_batch_a_la_cola(ventana):
    from app.airvault.flujo import CANCELADO, SIN_SUBIR

    fila = parte(SIN_SUBIR, "DP | BITS", carpeta="job-1")
    ventana._estados = [fila]
    ventana._cancelar_una(fila, True)

    ventana._cancelar_una(ventana._estados[0], False)

    assert not fila.trabajo.manifiesto.cancelado
    assert ventana._estados[0].estado != CANCELADO
    assert not ventana._estados[0].se_acabo
    # Vuelve a ser algo que esperar, así que el reloj puede reanudarse.
    assert ventana._falta_esperar()


def test_copiar_el_nombre_del_batch_lo_deja_en_el_portapapeles(ventana):
    from PySide6.QtGui import QGuiApplication
    from app.airvault.flujo import LISTO

    fila = parte(LISTO, "DP | BITS -4", carpeta="job-1")
    ventana._estados = [fila]

    ventana._copiar_al_portapapeles(fila.nombre)

    assert QGuiApplication.clipboard().text() == "DP | BITS -4"


def test_varios_batches_se_mandan_a_la_cola_de_una_vez(ventana):
    """Elegir cinco filas mezcladas hace en cada una lo que corresponde."""
    from app.airvault.flujo import INDEXADO, SIN_SUBIR

    ventana._estados = [
        parte(SIN_SUBIR, "DP | BITS -1", carpeta="job-1"),
        parte(SIN_SUBIR, "DP | BITS -2", carpeta="job-2"),
        parte(INDEXADO, "DP | BITS -3", carpeta="job-3"),
    ]
    ventana._pintar_lotes()
    lanzados = []
    ventana._ejecutar_accion = lambda modo, trabajos: (
        lanzados.append((modo, [t.manifiesto.nombre_batch for t in trabajos]))
        or True
    )

    acciones = _acciones(ventana, 0, 1, 2)
    # La acción dice a cuántos se aplicaría, que no son todos los elegidos.
    assert "Subir a AirVault ahora (2)" in acciones
    assert "Completar el batch" in acciones
    acciones["Subir a AirVault ahora (2)"].trigger()

    assert lanzados == [
        ("resubir", ["DP | BITS -1", "DP | BITS -2"]),
    ]


# ── subir a mano lo que AirVault no devuelve ───────────────────────

def test_la_cola_deja_subir_lo_que_esta_a_medio_identificar(ventana):
    """Esperar es una suposición del programa; quien mira la cola sabe más.

    Antes la acción solo valía en dos estados, así que una carga que el
    programa estaba revisando no se podía volver a mandar aunque quien
    tenía Web Index delante ya supiera que no está.
    """
    from app.airvault.flujo import PROCESANDO

    ventana._estados = [parte(
        PROCESANDO, "DP | BITS -1", "revisando nombres (1/3)", carpeta="job-1"
    )]
    ventana._pintar_lotes()

    assert _acciones(ventana)["Subir a AirVault ahora"].isEnabled()


def test_la_cola_deja_subir_el_que_aparecio_descuadrado(ventana):
    """Sin lote confirmado no hay nada que duplicar."""
    from app.airvault.flujo import DESCUADRADO

    ventana._estados = [parte(
        DESCUADRADO, "DP | BITS -1", "páginas incorrectas", carpeta="job-1"
    )]
    ventana._pintar_lotes()

    assert _acciones(ventana)["Subir a AirVault ahora"].isEnabled()


def test_la_cola_no_deja_subir_lo_que_airvault_ya_devolvio(ventana):
    """Con el batch confirmado, subirlo otra vez lo publicaría dos veces."""
    from app.airvault.client import ResumenLote
    from app.airvault.flujo import PROCESANDO

    confirmado = ResumenLote(
        batch_id="003SRO", nombre="DP | BITS -1", paginas=3, repo_id=3209,
        repositorio="MXDocs", paso="Web Index", bloqueado_por="", recibido="",
    )
    ventana._estados = [parte(
        PROCESANDO, "DP | BITS -1", "3 de 5 páginas", carpeta="job-1",
        lote=confirmado,
    )]
    ventana._pintar_lotes()

    assert not _acciones(ventana)["Subir a AirVault ahora"].isEnabled()


def test_subir_a_mano_conserva_la_foto_de_la_cola_hasta_comprobar(ventana):
    """La ventana no reinicia el manifiesto, y es a propósito.

    ``lotes_previos`` es la foto de la cola anterior a la carga, y es lo
    único que distingue un ``Empty-Batch`` propio de uno ajeno. Borrarla
    aquí dejaba a la comprobación sin con qué reconocerlo, y la orden
    acababa publicando la misma bitácora dos veces. La reinicia
    ``subir_partes``, después de comprobar y justo antes de enviar.
    """
    from app.airvault.flujo import PROCESANDO

    fila = parte(PROCESANDO, "DP | BITS -1", carpeta="job-1")
    fila.trabajo.manifiesto.lotes_previos = ["003VIEJO"]
    ventana._estados = [fila]
    lanzados = []
    ventana._ejecutar_accion = lambda modo, trabajos: (
        lanzados.append((modo, list(trabajos))) or True
    )

    ventana._subir_estas([fila])

    assert fila.trabajo.manifiesto.lotes_previos == ["003VIEJO"]
    assert lanzados[0][0] == "resubir"
    assert lanzados[0][1] == [fila.trabajo]


def test_la_espera_dice_que_se_puede_subir_sin_esperarla(ventana):
    """El «cómo darle que sí» tiene que estar donde se lee la espera."""
    from app.airvault.flujo import PROCESANDO

    ventana._al_comprobar({
        "estados": [parte(PROCESANDO, "DP | BITS -1", carpeta="job-1")],
        "planes": {}, "partes": [],
    })

    texto = ventana.resumen.text()
    assert "Subir a AirVault ahora" in texto
    assert "no espere" in texto


# ── páginas amarillas: se pregunta, no se prohíbe ──────────────────

def test_se_pregunta_antes_de_subir_paginas_amarillas_y_se_puede_decir_si(
    ventana, monkeypatch
):
    """El batch ya está hecho: rehacerlo cuesta más que indexarlas a mano."""
    from app.airvault import flujo
    from app.airvault.flujo import PROCESANDO

    fila = parte(PROCESANDO, "DP | BITS -1", carpeta="job-1")
    monkeypatch.setattr(
        flujo, "paginas_amarillas",
        lambda trabajo: ["página 3: Aircraft", "página 7: Log Page Number"],
    )
    preguntas = []
    ventana._confirmar_amarillas = lambda con_amarillas, cuantas: (
        preguntas.append(cuantas) or True
    )
    lanzados = []
    ventana._ejecutar_accion = lambda modo, trabajos: (
        lanzados.append(modo) or True
    )

    ventana._subir_estas([fila])

    assert preguntas == [2]
    assert fila.trabajo.manifiesto.amarillas_permitidas
    assert lanzados == ["resubir"]


def test_decir_que_no_a_las_amarillas_no_sube_nada(ventana, monkeypatch):
    from app.airvault import flujo
    from app.airvault.flujo import PROCESANDO

    fila = parte(PROCESANDO, "DP | BITS -1", carpeta="job-1")
    monkeypatch.setattr(
        flujo, "paginas_amarillas", lambda trabajo: ["página 3: Aircraft"]
    )
    ventana._confirmar_amarillas = lambda con_amarillas, cuantas: False
    lanzados = []
    ventana._ejecutar_accion = lambda modo, trabajos: (
        lanzados.append(modo) or True
    )

    ventana._subir_estas([fila])

    assert lanzados == []
    assert not fila.trabajo.manifiesto.amarillas_permitidas


def test_autorizado_una_vez_no_se_vuelve_a_preguntar(ventana, monkeypatch):
    """Un reintento no puede pedir la misma autorización otra vez."""
    from app.airvault import flujo
    from app.airvault.flujo import PROCESANDO

    fila = parte(PROCESANDO, "DP | BITS -1", carpeta="job-1")
    fila.trabajo.manifiesto.amarillas_permitidas = True
    monkeypatch.setattr(
        flujo, "paginas_amarillas", lambda trabajo: ["página 3: Aircraft"]
    )
    preguntas = []
    ventana._confirmar_amarillas = lambda *args: preguntas.append(args) or True
    ventana._ejecutar_accion = lambda modo, trabajos: True

    ventana._subir_estas([fila])

    assert preguntas == []


def test_la_bitacora_lista_cada_batch_en_su_propia_linea(ventana):
    """Todos seguidos en una linea habia que leerla entera para dar con uno."""
    from app.airvault.flujo import LISTO, SIN_SUBIR

    ventana._al_comprobar({
        "estados": [
            parte(LISTO, "DP | BITS -1", carpeta="job-1"),
            parte(SIN_SUBIR, "DP | BITS -2", carpeta="job-2"),
        ],
        "planes": {}, "partes": [],
    })

    revisado = [
        ventana.bitacora.item(i).text()
        for i in range(ventana.bitacora.count())
        if "Revisado:" in ventana.bitacora.item(i).text()
    ][-1]
    lineas = revisado.splitlines()
    assert len(lineas) == 3
    assert "DP | BITS -1" in lineas[1]
    assert "DP | BITS -2" in lineas[2]
    # Sangradas bajo el encabezado, no bajo la hora: la columna de horas
    # se sigue leyendo de un vistazo.
    assert lineas[1].startswith(" " * 10)


def test_un_mensaje_de_varios_parrafos_se_apunta_por_su_primera_frase(ventana):
    """El motivo entero esta en el resumen; en la bitacora tapaba las horas."""
    ventana._al_fallar(
        "No se pudo abrir la sesion. Revise el usuario y la contrasenya en "
        "la configuracion de AirVault y vuelva a intentarlo."
    )

    ultima = ventana.bitacora.item(ventana.bitacora.count() - 1).text()
    assert ultima.endswith("Se detuvo: No se pudo abrir la sesion.")


def test_una_carga_que_no_salio_no_se_cuenta_como_subida(ventana):
    """Decir «subida terminada» de un archivo que nunca se envió engaña."""
    ventana._al_subir({
        "trabajos": [TrabajoFalso()], "cliente": object(),
        "fallos": [("DP | BITS -2", "dejaría 4 páginas amarillas")],
    })

    assert "1 batch no se subió" in ventana.resumen.text()
    anotado = [
        ventana.bitacora.item(i).text()
        for i in range(ventana.bitacora.count())
    ]
    assert any("dejaría 4 páginas amarillas" in linea for linea in anotado)


def test_la_tabla_permite_elegir_varias_filas(ventana):
    from PySide6.QtWidgets import QAbstractItemView

    assert ventana.lotes.selectionMode() is (
        QAbstractItemView.SelectionMode.ExtendedSelection
    )
    assert ventana.lotes.selectionBehavior() is (
        QAbstractItemView.SelectionBehavior.SelectRows
    )


def test_una_accion_pedida_mientras_trabaja_espera_su_turno(ventana):
    """La tabla es una cola: lo que se pide no se pierde, se apunta."""
    from app.airvault.flujo import SIN_SUBIR

    fila = parte(SIN_SUBIR, "DP | BITS -1", carpeta="job-1")
    ventana._estados = [fila]
    lanzados = []
    ventana._ejecutar_accion = lambda modo, trabajos: (
        lanzados.append(modo) or True
    )
    ventana.hilo = lambda: object()   # hay algo en vuelo

    ventana._subir_estas([fila])

    assert lanzados == []
    assert len(ventana._cola_de_acciones) == 1
    anotado = [
        ventana.bitacora.item(i).text()
        for i in range(ventana.bitacora.count())
    ]
    assert any("en cola" in linea for linea in anotado)

    # Al quedar libre, arranca sola.
    ventana.hilo = lambda: None
    assert ventana._siguiente_de_la_cola()
    assert lanzados == ["resubir"]
    assert ventana._cola_de_acciones == []


def test_cancelar_saca_tambien_lo_que_esperaba_turno(ventana):
    """Encolar y después cancelar no puede acabar subiéndolo igual."""
    from app.airvault.flujo import SIN_SUBIR

    fila = parte(SIN_SUBIR, "DP | BITS -1", carpeta="job-1")
    ventana._estados = [fila]
    ventana.hilo = lambda: object()
    ventana._subir_estas([fila])
    assert ventana._cola_de_acciones

    ventana._cancelar_una(ventana._estados[0], True)

    assert ventana._cola_de_acciones == []
    ventana.hilo = lambda: None
    assert not ventana._siguiente_de_la_cola()


# ── la bitácora y la lista de bitácoras del batch ──────────────────

def test_la_bitacora_crece_con_la_ventana_y_no_se_queda_en_tres_lineas(ventana):
    """Un mensaje largo se envuelve; con el tope de 110 px se leía a trozos.

    El suelo lo pone la densidad: en una pantalla baja la bitácora cede
    cuarenta píxeles, que son los que hacen que la ventana entre entera.
    """
    minimo = ventana._densidad.airvault_log_min_height

    assert ventana.bitacora.minimumHeight() == minimo
    assert ventana.bitacora.maximumHeight() > minimo * 2
    assert ventana.bitacora.wordWrap()


def test_la_cola_deja_ver_las_bitacoras_de_un_batch(ventana):
    """Es una lista por batch: con varios elegidos no hay cuál enseñar."""
    from app.airvault.flujo import SIN_SUBIR

    ventana._estados = [
        parte(SIN_SUBIR, "DP | BITS -1", carpeta="job-1"),
        parte(SIN_SUBIR, "DP | BITS -2", carpeta="job-2"),
    ]
    ventana._pintar_lotes()

    assert _acciones(ventana, 0)["Ver las bitácoras del batch"].isEnabled()
    assert not _acciones(
        ventana, 0, 1
    )["Ver las bitácoras del batch"].isEnabled()


def test_la_vista_previa_esta_apagada_hasta_elegir_una_ejecucion(ventana):
    assert not ventana.boton_previa.isEnabled()


def test_la_vista_previa_abre_los_batches_de_la_ejecucion(app, tmp_path):
    """El botón calcula el reparto y se lo pasa al cuadro, sin subir nada."""
    from app.gui import airvault_previa
    from tests.test_airvault_entrega import corrida as corrida_exportada

    salida = tmp_path / "output"
    salida.mkdir()
    csv_path, _partes = corrida_exportada(salida)
    ventana = AirVaultWindow(tmp_path)
    ventana.limite_batch_spin.setValue(6)
    ventana.fijar_corrida(csv_path)
    assert ventana.boton_previa.isEnabled()

    abiertos = []
    with patch.object(
        airvault_previa.VistaPreviaBatches, "mostrar",
        lambda self: abiertos.append(self),
    ):
        ventana._vista_previa()

    assert len(abiertos) == 1
    cuadro = abiertos[0]
    # Es una ventana aparte, no un cuadro que bloquee esta: la referencia la
    # conserva la ventana que la abrió, que es lo que la mantiene viva.
    assert cuadro in ventana._ventanas_de_consulta
    assert cuadro.parent() is None
    assert cuadro.tabla.rowCount() > 1, "12 paginas con tope de 6 son varios"
    assert all(
        cuadro.tabla.item(fila, 3).text() == "Por subir"
        for fila in range(cuadro.tabla.rowCount())
    )
    # Mirar no prepara: la carpeta de trabajo sigue sin existir.
    assert not (tmp_path / "output" / "airvault").exists()
    ventana.close()


def test_la_vista_previa_avisa_de_la_ejecucion_sin_exportar(app, tmp_path, monkeypatch):
    """Sin PDF de entrega no hay reparto, y se dice en vez de fallar."""
    csv_path = corrida(tmp_path, exportada=False)
    ventana = AirVaultWindow(tmp_path)
    ventana._corrida = str(csv_path)

    avisos = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda *args, **kwargs: avisos.append(args[2]),
    )
    ventana._vista_previa()

    assert avisos and "exportar" in avisos[0]


# ── que lo automático no salga de la ejecución elegida ─────────────
#
# La ventana puede retomar batches que quedaron a medias en ejecuciones de
# días anteriores. Eso vale cuando alguien lo pide, porque está mirando;
# hacerlo solo, en mitad de la cadena automática o en una vuelta del reloj,
# mandaba a AirVault batches de otro día que nadie había pedido, y es por
# donde acababan subidos dos veces.

class WorkerDePrueba(TrabajoAirVaultWorker):
    """El worker sin hilo: solo se le pregunta a qué ejecuciones llega."""

    def __init__(self, estado):
        super().__init__("comprobar", estado)


def _worker_que_puede_recuperar(tmp_path, monkeypatch, recuperar):
    """Un worker con un batch pendiente de otro día esperando ahí fuera."""
    from app.airvault import flujo

    ajeno = TrabajoFalso(nombre="DP | AYER", carpeta=str(tmp_path / "ayer"))
    ajeno.carpeta = Path(ajeno.carpeta)
    monkeypatch.setattr(
        flujo, "cargar_trabajos_pendientes", lambda *a, **k: [ajeno]
    )
    monkeypatch.setattr(flujo, "estado_local", lambda t: parte("buscando"))
    estado = {
        "config": AirVaultConfig(),
        "raiz": tmp_path,
        "trabajos": [],
        "recuperar_pendientes": recuperar,
    }
    return WorkerDePrueba(estado), estado


def test_la_cadena_automatica_no_recoge_batches_de_otros_dias(
    app, tmp_path, monkeypatch
):
    worker, estado = _worker_que_puede_recuperar(tmp_path, monkeypatch, False)

    worker._detectar_pendientes()

    assert estado["trabajos"] == []


def test_el_boton_de_subir_si_recoge_lo_que_quedo_a_medias(
    app, tmp_path, monkeypatch
):
    """Pulsarlo significa «ponte al día», y quien lo pulsa está mirando."""
    worker, estado = _worker_que_puede_recuperar(tmp_path, monkeypatch, True)

    worker._detectar_pendientes()

    assert [t.manifiesto.nombre_batch for t in estado["trabajos"]] == [
        "DP | AYER"
    ]


def test_solo_el_boton_pide_recuperar_y_solo_por_una_vez(ventana, tmp_path):
    csv = corrida(RAIZ, nombre="BITS 26 AUG 2026 05 00")
    try:
        ventana._corrida = str(csv)
        ventana.lote_edit.setText("DP | BIT")

        ventana._recuperar_pendientes = True
        assert ventana._base_del_estado()["recuperar_pendientes"]
        # Y la siguiente acción, que puede ser una vuelta del reloj, ya no.
        assert not ventana._base_del_estado()["recuperar_pendientes"]
    finally:
        shutil.rmtree(csv.parent.parent, ignore_errors=True)


# ── el posible duplicado ───────────────────────────────────────────

def test_quitar_la_marca_autoriza_y_reenvia_la_fila(ventana, monkeypatch):
    """La sospecha prueba que las bitácoras están en AirVault.

    No prueba quién las subió: pueden haber llegado por otro batch, por
    otra persona o por una carga anterior de esta misma ejecución. Eso lo
    decide quien mira AirVault, y aquí solo se quita la marca.
    """
    from app.airvault.flujo import POSIBLE_DUPLICADO

    fila = parte(POSIBLE_DUPLICADO, "DP | BIT -2", "ya se mandaron")
    fila.trabajo.manifiesto.posible_duplicado = "ya se mandaron"
    ventana._estados = [fila]
    ventana._trabajos = [fila.trabajo]
    reenviadas = []
    monkeypatch.setattr(
        ventana,
        "_subir_estas",
        lambda partes, duplicados_autorizados=False: reenviadas.append(
            (list(partes), duplicados_autorizados)
        ),
    )

    ventana._quitar_sospecha([fila])

    assert not fila.trabajo.manifiesto.posible_duplicado
    assert fila.trabajo._duplicado_permitido
    assert ventana._estados[0].estado != POSIBLE_DUPLICADO
    assert reenviadas == [([fila], True)]


def test_reenviar_posible_duplicado_lo_deja_claro_en_la_bitacora(
    ventana, monkeypatch,
):
    from app.airvault.flujo import POSIBLE_DUPLICADO

    fila = parte(POSIBLE_DUPLICADO, "DP | BIT -2", "ya se mandaron")
    fila.trabajo.manifiesto.posible_duplicado = "ya se mandaron"
    ventana._estados = [fila]
    ventana._trabajos = [fila.trabajo]
    monkeypatch.setattr(ventana, "_ejecutar_accion", lambda *args: False)

    ventana._quitar_sospecha([fila])

    assert "Reenvío autorizado tras revisar posible duplicado" in (
        ventana.bitacora.item(ventana.bitacora.count() - 1).text()
    )


def test_volver_a_pulsar_subir_autoriza_los_posibles_duplicados(
    ventana, monkeypatch,
):
    from app.airvault.flujo import POSIBLE_DUPLICADO

    fila = parte(POSIBLE_DUPLICADO, "DP | BIT -2", "ya se mandaron")
    fila.trabajo.manifiesto.posible_duplicado = "ya se mandaron"
    ventana._estados = [fila]
    ventana._trabajos = [fila.trabajo]
    autorizadas = []
    monkeypatch.setattr(
        ventana, "_quitar_sospecha",
        lambda partes: autorizadas.extend(partes),
    )

    ventana._subir_a_mano()

    assert autorizadas == [fila]
