"""La ventana trabaja sobre la carpeta compartida cuando la ruta remota manda."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QDialog, QMessageBox

from app.reports.outputs import new_run_dir
from app.utils import ruta_remota
from app.utils.io import PROCESSED_DIRNAME


@pytest.fixture(autouse=True)
def _sin_conexiones_recordadas():
    ruta_remota.olvidar_conexion()
    yield
    ruta_remota.olvidar_conexion()


@pytest.fixture
def carpetas(tmp_path):
    """La carpeta del programa y la compartida, las dos con su entrada."""
    local = tmp_path / "equipo"
    servidor = tmp_path / "servidor"
    (local / "input").mkdir(parents=True)
    (servidor / "input").mkdir(parents=True)
    return local, servidor


def _pdf(ruta: Path) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text("%PDF-1.4\n", encoding="utf-8")
    return ruta


def _report(pdf_path: Path):
    from app.models.schemas import ValidationReport

    return ValidationReport(pdf_path=str(pdf_path), template_name="t", pages=[])


def test_el_menu_de_carpetas_ofrece_la_ruta_remota(window):
    textos = [
        accion.text() for accion in window.input_actions_button.menu().actions()
    ]

    assert textos[-1] == "Ruta remota…"
    assert window.btn_ruta_remota.text() == "Ruta remota…"


def test_apagada_la_entrada_es_la_del_equipo(window, carpetas):
    local, servidor = carpetas
    propio = _pdf(local / "input" / "local.pdf")
    _pdf(servidor / "input" / "compartido.pdf")
    ruta_remota.guardar_ajustes(False, str(servidor))

    with patch("app.gui.main_window.SCRIPT_DIR", local):
        window._load_default_input()

    assert window._pdf_paths == [propio]


def test_activa_la_entrada_es_la_del_servidor(window, carpetas):
    local, servidor = carpetas
    _pdf(local / "input" / "local.pdf")
    compartido = _pdf(servidor / "input" / "compartido.pdf")
    ruta_remota.guardar_ajustes(True, str(servidor))

    with patch("app.gui.main_window.SCRIPT_DIR", local):
        window._load_default_input()

    assert window._pdf_paths == [compartido]
    assert (servidor / "input" / PROCESSED_DIRNAME).is_dir()


def test_sin_conexion_la_entrada_vuelve_a_ser_la_del_equipo(window, carpetas):
    local, servidor = carpetas
    propio = _pdf(local / "input" / "local.pdf")
    ruta_remota.guardar_ajustes(True, str(servidor / "caida"))

    with patch("app.gui.main_window.SCRIPT_DIR", local):
        window._load_default_input()

    assert window._pdf_paths == [propio]


def test_lo_procesado_se_aparta_dentro_de_la_carpeta_compartida(window, carpetas):
    local, servidor = carpetas
    pdf = _pdf(servidor / "input" / "bitacora.pdf")
    ruta_remota.guardar_ajustes(True, str(servidor))
    window._reports = [_report(pdf)]
    window._pdf_paths = [pdf]

    with patch("app.gui.main_window.SCRIPT_DIR", local):
        window._archive_processed_inputs()

    apartado = servidor / "input" / PROCESSED_DIRNAME / "bitacora.pdf"
    assert apartado.is_file()
    assert not pdf.exists()
    assert window._pdf_paths == [apartado]
    assert not (local / "input" / PROCESSED_DIRNAME).exists()


def test_un_pdf_de_la_entrada_local_se_sigue_apartando_con_la_ruta_activa(
    window, carpetas
):
    local, servidor = carpetas
    pdf = _pdf(local / "input" / "bitacora.pdf")
    ruta_remota.guardar_ajustes(True, str(servidor))
    window._reports = [_report(pdf)]
    window._pdf_paths = [pdf]

    with patch("app.gui.main_window.SCRIPT_DIR", local):
        window._archive_processed_inputs()

    assert (local / "input" / PROCESSED_DIRNAME / "bitacora.pdf").is_file()


def test_las_ejecuciones_se_escriben_en_la_carpeta_compartida(window, carpetas):
    local, servidor = carpetas
    ruta_remota.guardar_ajustes(True, str(servidor))
    window._processed_template = object()

    with patch("app.gui.main_window.SCRIPT_DIR", local), patch.object(
        window, "_important_columns_for_export", return_value=[]
    ):
        opciones = window._export_options()

    assert opciones.output_root == servidor / "output"


def test_apagada_las_ejecuciones_se_escriben_en_el_equipo(window, carpetas):
    local, servidor = carpetas
    ruta_remota.guardar_ajustes(False, str(servidor))
    window._processed_template = object()

    with patch("app.gui.main_window.SCRIPT_DIR", local), patch.object(
        window, "_important_columns_for_export", return_value=[]
    ):
        opciones = window._export_options()

    assert opciones.output_root == local / "output"


@pytest.mark.parametrize("vaciar", ["_clear_input_folder", "_clear_output_folder"])
def test_la_carpeta_compartida_no_se_vacia_desde_la_ventana(
    window, carpetas, vaciar
):
    local, servidor = carpetas
    _pdf(local / "input" / "local.pdf")
    (local / "output" / "BITS TEST").mkdir(parents=True)
    _pdf(servidor / "input" / "compartido.pdf")
    ruta_remota.guardar_ajustes(True, str(servidor))

    with patch("app.gui.main_window.SCRIPT_DIR", local), patch(
        "app.gui.main_window.QMessageBox.information"
    ) as aviso, patch(
        "app.gui.main_window.QMessageBox.warning",
        return_value=QMessageBox.StandardButton.Yes,
    ), patch("app.gui.main_window.send_to_trash") as papelera:
        getattr(window, vaciar)()

    papelera.assert_not_called()
    assert "ruta remota" in aviso.call_args.args[2]
    assert (servidor / "input" / "compartido.pdf").is_file()


def test_guardar_la_ruta_sincroniza_la_memoria_y_recarga_la_entrada(
    window, carpetas
):
    local, servidor = carpetas
    compartido = _pdf(servidor / "input" / "compartido.pdf")
    (local / "book_matriculas.json").write_text(
        json.dumps({"23001B": "HP-1728CMP"}), encoding="utf-8"
    )

    def aceptar(_dialogo):
        ruta_remota.guardar_ajustes(True, str(servidor))
        return QDialog.DialogCode.Accepted

    with patch("app.gui.main_window.SCRIPT_DIR", local), patch(
        "app.gui.ruta_remota_dialog.RutaRemotaDialog.exec", aceptar
    ):
        window._open_ruta_remota()

    assert json.loads(
        (servidor / "book_matriculas.json").read_text(encoding="utf-8")
    ) == {"23001B": "HP-1728CMP"}
    assert window._pdf_paths == [compartido]


def test_cancelar_el_cuadro_no_cambia_nada(window, carpetas):
    local, servidor = carpetas
    propio = _pdf(local / "input" / "local.pdf")

    with patch("app.gui.main_window.SCRIPT_DIR", local):
        window._load_default_input()
        with patch(
            "app.gui.ruta_remota_dialog.RutaRemotaDialog.exec",
            lambda _dialogo: QDialog.DialogCode.Rejected,
        ):
            window._open_ruta_remota()

    assert ruta_remota.leer_ajustes() == ruta_remota.Ajustes()
    assert window._pdf_paths == [propio]


# ── El cuadro ──────────────────────────────────────────────────────────


@pytest.fixture
def cuadro(app):
    from app.gui.ruta_remota_dialog import RutaRemotaDialog

    def abrir():
        return RutaRemotaDialog()

    return abrir


def test_el_cuadro_abre_desactivado_y_sin_ruta(cuadro):
    dialogo = cuadro()

    assert not dialogo.activa_check.isChecked()
    assert dialogo.ruta_edit.text() == ""
    # Desactivada no hay nada que decir: la linea de estado no ocupa sitio.
    assert dialogo.estado.isHidden()
    assert dialogo.estado.text() == ""


def test_el_cuadro_recuerda_lo_guardado(cuadro, carpetas):
    _local, servidor = carpetas
    ruta_remota.guardar_ajustes(True, str(servidor))

    dialogo = cuadro()

    assert dialogo.activa_check.isChecked()
    assert dialogo.ruta_edit.text() == str(servidor)
    assert not dialogo.estado.isHidden()
    assert dialogo.estado.text() == "Conectada."


def test_el_estado_solo_habla_de_la_ruta_guardada(cuadro, carpetas):
    _local, servidor = carpetas
    ruta_remota.guardar_ajustes(True, str(servidor))
    dialogo = cuadro()
    alto_con_estado = dialogo.height()

    dialogo.activa_check.setChecked(False)

    assert dialogo.estado.isHidden()
    assert dialogo.height() < alto_con_estado

    dialogo.activa_check.setChecked(True)
    assert dialogo.estado.text() == "Conectada."
    assert dialogo.height() == alto_con_estado

    dialogo.ruta_edit.setText(str(servidor / "otra"))
    assert dialogo.estado.isHidden()


def test_sin_conexion_el_cuadro_lo_dice(cuadro, tmp_path):
    ruta_remota.guardar_ajustes(True, str(tmp_path / "caida"))

    dialogo = cuadro()

    assert dialogo.estado.text().startswith("Sin conexión")


def test_guardar_activa_la_ruta_y_prepara_la_carpeta(cuadro, tmp_path):
    servidor = tmp_path / "servidor"
    servidor.mkdir()
    dialogo = cuadro()
    dialogo.activa_check.setChecked(True)
    dialogo.ruta_edit.setText(str(servidor))

    dialogo._guardar()

    assert dialogo.result() == QDialog.DialogCode.Accepted
    assert ruta_remota.leer_ajustes() == ruta_remota.Ajustes(True, str(servidor))
    assert (servidor / "input" / PROCESSED_DIRNAME).is_dir()
    assert (servidor / "output").is_dir()


def test_desactivar_guarda_la_ruta_para_despues(cuadro, carpetas):
    _local, servidor = carpetas
    ruta_remota.guardar_ajustes(True, str(servidor))
    dialogo = cuadro()
    dialogo.activa_check.setChecked(False)

    dialogo._guardar()

    assert ruta_remota.leer_ajustes() == ruta_remota.Ajustes(False, str(servidor))
    assert ruta_remota.raiz_remota() is None


def test_no_se_activa_sin_ruta(cuadro):
    dialogo = cuadro()
    dialogo.activa_check.setChecked(True)

    with patch("app.gui.ruta_remota_dialog.QMessageBox.warning") as aviso:
        dialogo._guardar()

    aviso.assert_called_once()
    assert dialogo.result() != QDialog.DialogCode.Accepted
    assert ruta_remota.leer_ajustes() == ruta_remota.Ajustes()


@pytest.mark.parametrize(
    "respuesta, guardada",
    [
        (QMessageBox.StandardButton.No, False),
        (QMessageBox.StandardButton.Yes, True),
    ],
)
def test_una_carpeta_que_no_responde_se_guarda_solo_si_se_confirma(
    cuadro, tmp_path, respuesta, guardada
):
    caida = tmp_path / "caida"
    dialogo = cuadro()
    dialogo.activa_check.setChecked(True)
    dialogo.ruta_edit.setText(str(caida))

    with patch(
        "app.gui.ruta_remota_dialog.QMessageBox.question", return_value=respuesta
    ):
        dialogo._guardar()

    assert ruta_remota.leer_ajustes().activa is guardada
    # Sin conexion no se crea nada: se trabaja con lo local.
    assert not caida.exists()
    assert ruta_remota.raiz_remota() is None


# ── Lo que cuelga de la carpeta ────────────────────────────────────────


def test_dos_ejecuciones_del_mismo_minuto_no_comparten_carpeta(tmp_path):
    salida = tmp_path / "servidor" / "output"

    with patch("app.reports.outputs.run_csv_name", return_value="BITS TEST.CSV"):
        primera = new_run_dir(salida)
        (primera / "datos").mkdir()
        segunda = new_run_dir(salida)

    assert primera == salida / "BITS TEST"
    assert segunda == salida / "BITS TEST-2"
    assert segunda.is_dir()


def test_el_visor_busca_los_pdf_en_las_dos_entradas(carpetas):
    from app.gui import csv_viewer

    local, servidor = carpetas
    ruta_remota.guardar_ajustes(True, str(servidor))
    csv = servidor / "output" / "BITS TEST" / "datos" / "BITS TEST.CSV"

    with patch.object(csv_viewer, "_PROGRAM_DIR", local):
        carpetas_de_busqueda = csv_viewer._source_search_folders(csv)

    assert carpetas_de_busqueda == [
        csv.parent,
        csv.parent.parent,
        servidor / "input" / PROCESSED_DIRNAME,
        servidor / "input",
        local / "input" / PROCESSED_DIRNAME,
        local / "input",
    ]


def test_el_visor_apagado_busca_solo_en_la_entrada_del_equipo(carpetas):
    from app.gui import csv_viewer

    local, _servidor = carpetas
    csv = local / "output" / "BITS TEST" / "datos" / "BITS TEST.CSV"

    with patch.object(csv_viewer, "_PROGRAM_DIR", local):
        carpetas_de_busqueda = csv_viewer._source_search_folders(csv)

    assert carpetas_de_busqueda == [
        csv.parent,
        csv.parent.parent,
        local / "input" / PROCESSED_DIRNAME,
        local / "input",
    ]


def test_la_comprobacion_de_airvault_sube_lo_que_corrige(carpetas):
    from app.airvault.flujo import comprobar_memoria_de_libros

    local, servidor = carpetas
    (local / "book_matriculas.json").write_text(
        json.dumps({"23001B": "HP-1728CMP"}), encoding="utf-8"
    )
    ruta_remota.guardar_ajustes(True, str(servidor))

    comprobar_memoria_de_libros(SimpleNamespace(remotas={}), local)

    assert json.loads(
        (servidor / "book_matriculas.json").read_text(encoding="utf-8")
    ) == {"23001B": "HP-1728CMP"}
