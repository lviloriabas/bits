"""Los errores públicos conservan el motivo y ocultan datos internos."""

import errno

import pytest

from app.utils.mensajes import mensaje_aviso, mensaje_error


@pytest.mark.parametrize("error", [
    ValueError("Invalid data in C:\\interno\\perfil: token=secreto"),
    RuntimeError('<html>HTTP 500 at /index/FormsProcessing: secreto</html>'),
    "Traceback: TypeError at C:\\interno\\clase.py line 10: secreto",
])
def test_un_error_desconocido_no_publica_su_contenido(error):
    texto = mensaje_error(error)
    assert "secreto" not in texto
    assert "C:" not in texto
    assert "HTTP" not in texto
    assert len(texto) <= 140


@pytest.mark.parametrize("error,motivo", [
    (PermissionError("C:\\privado"), "acceder al archivo"),
    (FileNotFoundError("C:\\privado"), "No se encontró el archivo"),
    (OSError(errno.ENOSPC, "C:\\privado"), "espacio"),
    ("La sesion de AirVault caduco. Cookie: privado", "Inicie sesión"),
    ("SSLError CERTIFICATE_VERIFY_FAILED: C:\\privado", "fecha y hora"),
    ("AirVault no conservo el guardado de la pagina 3: C:\\privado", "no confirmó el guardado"),
])
def test_un_motivo_conocido_es_breve_y_no_se_pierde_al_recibir_la_senal(error, motivo):
    texto = mensaje_error(error)
    assert motivo in texto
    assert "privado" not in texto
    assert len(texto) <= 140
    assert mensaje_error(texto) == texto


@pytest.mark.parametrize("aviso", [
    "The page is not locked; HTTP 500",
    "The page is not locked by another user; HTTP 500",
    "La bitácora no está bloqueada por otro usuario; error",
])
def test_no_atribuye_un_bloqueo_si_airvault_lo_niega(aviso):
    assert "bloqueada" not in mensaje_error(aviso)


def test_los_avisos_guardados_no_muestran_codigos_ni_campos_internos():
    assert mensaje_aviso("[obligatorio_vacio] pagina 8: End Date value is required") == "Falta confirmar la fecha."
    texto = mensaje_aviso("[rechazada] HTTP 500: C:\\interno\\privado")
    assert "rechazada" not in texto
    assert "HTTP" not in texto
    assert "privado" not in texto


def test_el_registro_visual_no_muestra_la_traza_y_evitan_repetirse_los_errores(app):
    from loguru import logger
    from app.gui.main_window import QtLogSink

    sink = QtLogSink()
    visibles, tecnicos = [], []
    sink.message.connect(visibles.append)
    visual = logger.add(sink, level="ERROR")
    tecnico = logger.add(lambda mensaje: tecnicos.append(str(mensaje)), level="ERROR")
    try:
        for _ in range(2):
            try:
                raise ValueError("C:\\interno\\privado: token=secreto")
            except ValueError:
                logger.exception("No se pudo completar la operación")
        assert len(visibles) == 1
        assert "privado" not in visibles[0]
        assert "Traceback" not in visibles[0]
        assert any("privado" in mensaje for mensaje in tecnicos)
    finally:
        logger.remove(visual)
        logger.remove(tecnico)


def test_un_lanzador_que_no_puede_iniciar_no_muestra_rutas(monkeypatch, tmp_path):
    import launcher_gui

    (tmp_path / "run_gui.py").write_text("", encoding="utf-8")
    mensajes = []
    monkeypatch.setattr(launcher_gui, "app_root", lambda: tmp_path)
    monkeypatch.setattr(launcher_gui, "_pythonw", lambda _raiz: tmp_path / "privado.exe")
    monkeypatch.setattr(launcher_gui, "_show_error", lambda texto: mensajes.append(texto) or 1)
    def fallar(*_args, **_kwargs):
        raise OSError("C:\\interno\\privado.exe")
    monkeypatch.setattr(launcher_gui.subprocess, "Popen", fallar)
    assert launcher_gui.main() == 1
    assert len(mensajes) == 1
    assert "privado" not in mensajes[0]
    assert "no pudo iniciarse" in mensajes[0]
