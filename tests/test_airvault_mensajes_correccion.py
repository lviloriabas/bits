"""Motivos de corrección comprensibles y resultados sin falsas certezas."""

from types import SimpleNamespace

import pytest

from app.airvault import correcciones as modulo
from app.airvault.config import AirVaultConfig
from app.airvault.correcciones import (
    ControlNoEncontrado, CorrectorLogPageAudit, Resultado, copias_en,
    mensaje_airvault, planificar,
)
from app.airvault.mapping import ResolutorFlota
from app.airvault.web_reports import ErrorDePaginaAirVault, _Pagina
from app.gui import web_reports_window as gui
from tests.test_airvault_correcciones import (
    _PaginaFalsa, _REJILLA, _excepciones, _fila_de_rejilla,
)


@pytest.mark.parametrize("detalle,accion", [
    ("DUPLICATED 2008159(2x)", "eliminar la copia"),
    ("2008159 MIS-INDEX to ACN [HP-9813CMP]", "reindexar la bitácora"),
])
@pytest.mark.parametrize("aviso,motivo,ayuda", [
    ("The specified document page is locked by another user.",
     "bloqueada por otro usuario", "desbloquee"),
    ("The specified document page is locked.", "bloqueada en AirVault", "libere"),
    ("The document is currently locked.", "bloqueada en AirVault", "libere"),
    ("La bitácora está bloqueada por otro usuario.",
     "bloqueada por otro usuario", "desbloquee"),
    ("You do not have permission to perform this operation.",
     "no tiene permiso", "administrador"),
    ("Access denied.", "no tiene permiso", "administrador"),
    ("Your session has expired.", "sesión de AirVault caducó", "iniciar sesión"),
    ("The document page was not found.", "ya no está disponible", "buscarla"),
])
def test_el_rechazo_se_explica_en_el_resultado_sin_guardar(
    monkeypatch, tmp_path, detalle, accion, aviso, motivo, ayuda,
):
    correccion = planificar(_excepciones(("HP-9913CMP", detalle)))[0]
    rejilla = _REJILLA if accion == "eliminar la copia" else [
        _fila_de_rejilla("1", "551", "2008159", "HP-9813CMP", "1/9/2025")
    ]
    pagina = _PaginaFalsa(rejilla)
    pagina.aviso = aviso
    cerradas = []
    pagina.cerrar = lambda forzar: cerradas.append(forzar)
    monkeypatch.setattr(modulo, "_Pagina", lambda *_: pagina)
    navegador = SimpleNamespace(abrir_pestana=lambda *_a, **_k: "temporal")
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    corrector.auditoria = tmp_path / "auditoria.jsonl"

    resultado = corrector._un_caso(navegador, {}, correccion, lambda: False, False)

    assert not resultado.hecho
    assert motivo in resultado.detalle
    assert ayuda in resultado.detalle
    if "bloqueada" in motivo or "permiso" in motivo:
        assert accion in resultado.detalle
    assert cerradas == [True]
    assert not any("boton.click()" in orden for orden in pagina.ordenes)


def test_el_aviso_desconocido_conserva_la_evidencia_sin_inventar_un_bloqueo():
    aviso = "Cannot complete operation: repository is read-only."
    texto = mensaje_airvault(aviso, "reindexar la bitácora")
    assert aviso in texto
    assert "Revise la bitácora en Web Search" in texto
    assert "bloqueada" not in texto
    assert "no tiene permiso" not in texto


def test_no_deduce_un_bloqueo_de_una_negacion():
    aviso = "The document page is not locked; the operation failed."
    assert aviso in mensaje_airvault(aviso, "eliminar la copia")


@pytest.mark.parametrize("cuadro", ["deletePageDialog", "reindexDialog"])
def test_un_rechazo_al_confirmar_no_se_oculta_como_una_espera(cuadro):
    class PaginaSinCerrar(_PaginaFalsa):
        def esperar(self, *_a, **_k):
            return False

        def evaluar(self, expresion):
            if expresion == modulo._MENSAJE:
                return "The specified document page is locked by another user."
            return ""

    pagina = PaginaSinCerrar([])
    with pytest.raises(ControlNoEncontrado, match="bloqueada por otro usuario"):
        CorrectorLogPageAudit._cerrar_cuadro(
            pagina, cuadro, "guardado", modulo.TEXTOS_SEGUIR,
        )


@pytest.mark.parametrize("error,motivo", [
    (ConnectionResetError("WinError 10054: socket"), "comunicación con Edge"),
    (ValueError("Invalid data in C:\\interno\\perfil"), "error inesperado"),
    (ErrorDePaginaAirVault("La pestaña de AirVault ya no está abierta en Edge"),
     "ya no está abierta"),
])
def test_los_errores_internos_se_registran_y_no_se_muestran(
    app, monkeypatch, error, motivo,
):
    registrados = []
    monkeypatch.setattr(gui.logger, "exception", lambda *_: registrados.append(True))
    worker = gui.CorreccionWorker(AirVaultConfig(), [])
    recibidos = []
    worker.fallo.connect(recibidos.append)

    def fallar():
        raise error

    monkeypatch.setattr(worker, "_trabajar", fallar)
    worker.run()

    assert registrados
    assert len(recibidos) == 1
    assert motivo in recibidos[0]
    assert "Web Search" in recibidos[0]
    if not isinstance(error, ErrorDePaginaAirVault):
        assert str(error) not in recibidos[0]


def test_una_pestana_cerrada_identifica_airvault_sin_confundir_el_reporte():
    pagina = object.__new__(_Pagina)
    pagina.ws = None
    with pytest.raises(ErrorDePaginaAirVault, match="AirVault ya no está abierta"):
        pagina._evaluar("true")


@pytest.mark.parametrize("quedan,observado", [
    ([], "ya no aparece en Web Search"),
    ([dict(fila, log="2008152") for fila in _REJILLA[:2]],
     "Web Search muestra 2 copias"),
    ([_fila_de_rejilla("1", "551", "2008152", "", "1/9/2025")],
     "no muestra la matrícula"),
])
def test_la_reindexacion_no_confirmada_dice_que_muestra_web_search(
    monkeypatch, quedan, observado,
):
    correccion = planificar(_excepciones((
        "HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]",
    )))[0]
    rejilla = [_fila_de_rejilla("1", "551", "2008152", "HP-9813CMP", "1/9/2025")]
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    monkeypatch.setattr(corrector, "_reindexar_documento", lambda *_: None)
    monkeypatch.setattr(corrector, "_releer", lambda *_: copias_en(quedan, "2008152"))

    resultado = corrector._reindexar(
        _PaginaFalsa(rejilla), correccion, copias_en(rejilla, "2008152"), False,
    )
    assert not resultado.hecho
    assert "No se pudo confirmar la reindexación en HP-9913CMP" in resultado.detalle
    assert observado in resultado.detalle
    assert "otra cosa" not in resultado.detalle


def test_el_resumen_no_afirma_que_no_hubo_cambios_si_quedan_copias(
    app, tmp_path, monkeypatch,
):
    correccion = planificar(_excepciones(("HP-9913CMP", "DUPLICATED 2008159(3x)")))[0]
    resultado = Resultado(correccion, detalle=(
        "Borrada 1 copia; quedan 2 copias para revisar. La bitácora sigue duplicada."
    ))
    avisos = []
    monkeypatch.setattr(gui.QMessageBox, "exec", lambda aviso: avisos.append(aviso) or 0)
    ventana = gui.WebReportsWindow(tmp_path)
    try:
        ventana._al_corregir([resultado])
        assert "quedó pendiente de corrección" in ventana.resumen.text()
        assert "no se modific" not in ventana.resumen.text()
        assert "Borrada 1 copia" in avisos[0].informativeText()
        assert "2008159" in avisos[0].informativeText()
    finally:
        ventana.close()
