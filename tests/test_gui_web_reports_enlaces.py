"""Las celdas de la tabla que llevan a Web Search."""

from __future__ import annotations

from threading import Event
from time import monotonic

import pytest
from PySide6.QtCore import QThread, Qt

from app.airvault.config import AirVaultConfig
from app.airvault.web_reports import (
    parsear_filas,
    url_busqueda_del_libro,
    url_busqueda_rango,
)
from app.gui.web_reports_window import (
    COLUMNA_BITACORA,
    COLUMNA_MATRICULA_INDEXADA,
    COLUMNA_RANGO_LIBRO,
    ROL_ENLACE,
    WebReportsWindow,
    _TrabajoEnEdge,
)
from app.gui import web_reports_window as modulo_ventana
from app.gui.web_reports_tiempos import TAREA_BUSQUEDA, TAREA_CONSULTA, TAREA_CORRECCION


def _fila(matricula: str, detalle: str, rango: str = "2008150 - 2008199"):
    return [
        {"t": matricula},
        {"t": "Copa-7 (50)"},
        {"t": ""},
        {"t": rango},
        {"t": "1/1/2025 - 1/31/2025"},
        {"t": ""},
        {"t": ""},
        {"t": ""},
        {"t": ""},
        {"t": detalle},
    ]


def _excepciones(*filas):
    return parsear_filas(list(filas), AirVaultConfig())


def test_el_rango_abre_el_libro_entero() -> None:
    config = AirVaultConfig(base_url="https://airvault.example", repo_id=77)

    url = url_busqueda_del_libro(config, "2008150 - 2008199")

    assert url == url_busqueda_rango(config, "2008150", "2008199")
    assert "3=2008150%094=2008199" in url


def test_un_rango_que_no_se_lee_no_inventa_enlace() -> None:
    config = AirVaultConfig()

    assert url_busqueda_del_libro(config, "") == ""
    assert url_busqueda_del_libro(config, "sin numeros") == ""


def test_la_excepcion_trae_las_dos_busquedas() -> None:
    excepcion = _excepciones(
        _fila("HP-9913CMP", "DUPLICATED 2008159(3x)")
    )[0]

    assert "3=2008159%094=2008159" in excepcion.url_busqueda
    assert "3=2008150%094=2008199" in excepcion.url_busqueda_libro


def test_la_mal_indexada_ensena_las_dos_matriculas_juntas(
    app, tmp_path
) -> None:
    """La del libro es la que le toca; la de al lado, donde quedó."""
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(
                _fila("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]")
            )
        )

        assert ventana.tabla.item(0, 1).text() == "HP-9913CMP"
        assert (
            ventana.tabla.item(0, COLUMNA_MATRICULA_INDEXADA).text()
            == "HP-9813CMP"
        )
    finally:
        ventana.close()


def test_la_duplicada_deja_vacia_la_matricula_indexada(
    app, tmp_path
) -> None:
    """El reporte no la dice, y una celda en blanco no afirma nada."""
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(_fila("HP-9913CMP", "DUPLICATED 2008159(3x)"))
        )

        assert ventana.tabla.item(0, 1).text() == "HP-9913CMP"
        assert ventana.tabla.item(0, COLUMNA_MATRICULA_INDEXADA).text() == ""
    finally:
        ventana.close()


def test_las_dos_columnas_quedan_subrayadas_y_con_su_direccion(
    app, tmp_path
) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(_fila("HP-9913CMP", "DUPLICATED 2008159(3x)"))
        )

        bitacora = ventana.tabla.item(0, COLUMNA_BITACORA)
        libro = ventana.tabla.item(0, COLUMNA_RANGO_LIBRO)
        assert bitacora.font().underline()
        assert libro.font().underline()
        assert "3=2008159%094=2008159" in bitacora.data(ROL_ENLACE)
        assert "3=2008150%094=2008199" in libro.data(ROL_ENLACE)
        assert "Web Search" in bitacora.toolTip()
        assert "Web Search" in libro.toolTip()
        # Las demas columnas siguen siendo texto y no abren nada.
        assert not ventana.tabla.item(0, 0).font().underline()
        assert not ventana.tabla.item(0, 0).data(ROL_ENLACE)
    finally:
        ventana.close()


def test_sin_rango_legible_la_celda_no_se_marca(app, tmp_path) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(
                _fila("HP-9913CMP", "DUPLICATED 2008159(3x)", rango="")
            )
        )

        libro = ventana.tabla.item(0, COLUMNA_RANGO_LIBRO)
        assert not libro.font().underline()
        assert not libro.data(ROL_ENLACE)
        assert ventana.tabla.item(0, COLUMNA_BITACORA).data(ROL_ENLACE)
    finally:
        ventana.close()


def test_el_clic_manda_a_web_search_lo_que_dice_la_celda(
    app, tmp_path, monkeypatch
) -> None:
    ventana = WebReportsWindow(tmp_path)
    pedidos: list[tuple[str, str]] = []
    monkeypatch.setattr(
        WebReportsWindow,
        "_abrir_en_web_search",
        lambda _yo, url, etiqueta: pedidos.append((url, etiqueta)),
    )
    try:
        ventana._al_recibir(
            _excepciones(_fila("HP-9913CMP", "DUPLICATED 2008159(3x)"))
        )

        ventana._al_pulsar_la_celda(0, COLUMNA_BITACORA)
        ventana._al_pulsar_la_celda(0, COLUMNA_RANGO_LIBRO)
        # Una columna sin enlace no lanza nada.
        ventana._al_pulsar_la_celda(0, 0)

        assert [etiqueta for _url, etiqueta in pedidos] == [
            "la bitácora 2008159",
            "el libro 2008150 - 2008199",
        ]
        assert "3=2008159%094=2008159" in pedidos[0][0]
        assert "3=2008150%094=2008199" in pedidos[1][0]
    finally:
        ventana.close()


def test_el_cursor_avisa_solo_sobre_lo_que_abre(app, tmp_path) -> None:
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir(
            _excepciones(_fila("HP-9913CMP", "DUPLICATED 2008159(3x)"))
        )

        ventana._al_pasar_por_la_celda(0, COLUMNA_BITACORA)
        assert (
            ventana.tabla.viewport().cursor().shape()
            == Qt.CursorShape.PointingHandCursor
        )
        ventana._al_pasar_por_la_celda(0, 0)
        assert (
            ventana.tabla.viewport().cursor().shape()
            == Qt.CursorShape.ArrowCursor
        )
    finally:
        ventana.close()


def _esperar(app, condicion):
    limite = monotonic() + 5
    while not condicion() and monotonic() < limite:
        app.processEvents()
        QThread.msleep(5)
    app.processEvents()
    assert condicion()


class _TrabajoBloqueado(_TrabajoEnEdge):
    def __init__(self, parent):
        super().__init__(AirVaultConfig(), parent)
        self.liberado = Event()

    def _trabajar(self):
        assert self.liberado.wait(5), "El trabajo debe seguir vivo durante la apertura"
        return []


@pytest.fixture
def aperturas(app, tmp_path, monkeypatch):
    ventana = WebReportsWindow(tmp_path)
    liberadas = {"https://airvault/pagina": Event(), "https://airvault/libro": Event()}
    abiertas = {url: Event() for url in liberadas}
    principal = _TrabajoBloqueado(ventana)
    principal.finished.connect(ventana._al_terminar)
    ventana._worker = principal
    ventana._habilitar(False)
    ventana.progreso.setRange(0, 100)
    ventana.progreso.setValue(37)
    ventana.resumen.setText("Corrigiendo bitácoras")

    def abrir(_config, url, avisar=None):
        if avisar:
            avisar("Abriendo Web Search en Edge")
        abiertas[url].set()
        assert liberadas[url].wait(5), "Las dos aperturas deben poder solaparse"

    monkeypatch.setattr(modulo_ventana, "abrir_en_web_search", abrir)
    principal.start()
    try:
        yield ventana, principal, abiertas, liberadas
    finally:
        hilos = [principal, *ventana._aperturas]
        principal.liberado.set()
        for evento in liberadas.values():
            evento.set()
        for hilo in hilos:
            hilo.wait(5000)
        app.processEvents()
        ventana.close()


@pytest.mark.parametrize("tarea", [TAREA_CORRECCION, TAREA_CONSULTA, TAREA_BUSQUEDA])
def test_abre_varios_enlaces_sin_interrumpir_el_trabajo(app, aperturas, tarea):
    ventana, principal, abiertas, liberadas = aperturas
    ventana._arrancar_cronometro(tarea, 5)
    ventana._al_pasar(2, 5)
    estimacion = ventana._estimacion
    for url in abiertas:
        ventana._abrir_en_web_search(url, "la bitácora")
    _esperar(app, lambda: all(e.is_set() for e in abiertas.values()))
    assert len(ventana._aperturas) == 2
    assert ventana._worker is principal
    assert len(ventana.hilos()) == 3
    for evento in liberadas.values():
        evento.set()
    _esperar(app, lambda: not ventana._aperturas)
    assert principal.isRunning()
    assert not principal._cancelado()
    assert ventana._worker is principal
    assert ventana._estimacion is estimacion
    assert ventana._estimacion.hechas == 2
    assert ventana._timer.isActive()
    assert ventana.progreso.value() == 37
    assert ventana.resumen.text() == "Corrigiendo bitácoras"
    assert not ventana.boton_consultar.isEnabled()
    assert ventana.boton_cancelar.isEnabled()


def test_el_cierre_espera_todas_las_aperturas(app, aperturas):
    ventana, principal, abiertas, liberadas = aperturas
    ventana.show()
    for url in abiertas:
        ventana._abrir_en_web_search(url, "el libro")
    _esperar(app, lambda: all(e.is_set() for e in abiertas.values()))
    hilos = ventana.hilos()
    ventana.close()
    assert ventana.isVisible()
    assert all(hilo._cancelado() for hilo in hilos)
    principal.liberado.set()
    _esperar(app, lambda: ventana._worker is None)
    assert ventana.isVisible()
    liberadas["https://airvault/pagina"].set()
    _esperar(app, lambda: len(ventana._aperturas) == 1)
    assert ventana.isVisible()
    liberadas["https://airvault/libro"].set()
    _esperar(app, lambda: not ventana.isVisible())
    assert not ventana.hilos()


def test_abrir_entre_el_fin_del_hilo_y_su_senal_no_pierde_la_apertura(app, aperturas):
    ventana, principal, abiertas, liberadas = aperturas
    principal.liberado.set()
    assert principal.wait(5000)
    # El hilo ya salió, pero la interfaz aún debe atender su señal finished.
    assert ventana._worker is principal
    ventana._abrir_en_web_search("https://airvault/pagina", "la bitácora")
    _esperar(app, lambda: abiertas["https://airvault/pagina"].is_set())
    assert ventana._worker is None
    assert len(ventana._aperturas) == 1
    assert len(ventana.hilos()) == 1
    assert not ventana.boton_consultar.isEnabled()
    liberadas["https://airvault/pagina"].set()
    _esperar(app, lambda: not ventana._aperturas)
    assert ventana.boton_consultar.isEnabled()


def test_el_cierre_principal_reconoce_todos_los_hilos(app, aperturas, window):
    ventana, principal, abiertas, _liberadas = aperturas
    window._web_reports_window = ventana
    for url in abiertas:
        ventana._abrir_en_web_search(url, "el libro")
    _esperar(app, lambda: all(e.is_set() for e in abiertas.values()))
    try:
        assert set(ventana.hilos()).issubset(set(window._running_workers()))
        ventana.detener()
        assert all(hilo._cancelado() for hilo in ventana.hilos())
    finally:
        window._web_reports_window = None


def test_un_fallo_de_apertura_no_para_el_reloj_de_la_correccion(
    app, aperturas, monkeypatch,
):
    ventana, principal, _abiertas, _liberadas = aperturas
    ventana._arrancar_cronometro(TAREA_CORRECCION, 5)
    estimacion = ventana._estimacion
    def fallar(*_args, **_kwargs):
        raise RuntimeError("Edge no responde")
    monkeypatch.setattr(modulo_ventana, "abrir_en_web_search", fallar)
    ventana._abrir_en_web_search("https://airvault/pagina", "la bitácora")
    _esperar(app, lambda: not ventana._aperturas)
    assert ventana._worker is principal
    assert principal.isRunning()
    assert ventana._estimacion is estimacion
    assert ventana._timer.isActive()
    assert ventana.progreso.value() == 37
    assert "Edge no responde" in ventana.resumen.text()
