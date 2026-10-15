"""La espera del servidor informa actividad sin inventar avance."""

from datetime import date

import pytest
from loguru import logger

from app.airvault import web_reports
from app.airvault.config import AirVaultConfig
from app.airvault.web_reports import ClienteLogPageAudit
from app.gui.web_reports_window import WebReportsWindow, WebReportsWorker


def test_la_espera_informa_cada_quince_segundos_hasta_recibir_el_reporte(monkeypatch):
    ahora = [0.0]
    monkeypatch.setattr(web_reports.time, "monotonic", lambda: ahora[0])
    monkeypatch.setattr(
        ClienteLogPageAudit, "_estado_reporte",
        staticmethod(lambda _p: {
            "ocupada": ahora[0] < 31, "huella": "nuevo",
            "texto": "Log Page Audit", "actual": 1,
        }),
    )

    class Pagina:
        def _dormir(self, segundos):
            ahora[0] += segundos

    avisos = []
    ClienteLogPageAudit._esperar_reporte(Pagina(), avisar=avisos.append)

    assert avisos == ["Esperando que AirVault genere el reporte"] * 3
    assert ahora[0] == 31


@pytest.mark.parametrize("final", ["resultado", "fallo", "cancelacion"])
def test_la_consulta_informa_en_terminal_y_barra_y_detiene_el_arco(
    app, tmp_path, monkeypatch, final,
):
    ventana = WebReportsWindow(tmp_path)
    ventana._worker = WebReportsWorker(
        AirVaultConfig(), date(2026, 1, 1), date(2026, 1, 2), ("10",), ventana,
    )
    monkeypatch.setattr("app.gui.web_reports_tiempos.ARCHIVO", tmp_path / "tiempos.json")
    mensajes = []
    destino = logger.add(lambda mensaje: mensajes.append(mensaje.record["message"]))
    try:
        ventana._arrancar_cronometro("consulta", 1)
        ventana.progreso.setValue(25)
        texto = "Esperando que AirVault genere el reporte"
        ventana._al_avanzar(texto)

        assert ventana.resumen.text() == texto
        assert ventana.progreso.text() == f"25% - {texto}"
        assert ventana.actividad._timer.isActive()
        assert mensajes == [f"Web Reports: {texto}"]
        assert ventana.progreso.value() == 25

        if final == "resultado":
            ventana._al_recibir([])
            assert ventana.progreso.text() == "Consulta terminada: 100%"
        elif final == "fallo":
            ventana._al_fallar("AirVault no respondió")
            assert ventana.progreso.text() == "Consulta interrumpida: 25%"
        else:
            ventana._al_cancelar()
            assert ventana.progreso.text() == "Consulta cancelada: 25%"
        assert not ventana.actividad._timer.isActive()
    finally:
        logger.remove(destino)
        ventana._al_terminar()
        ventana.close()
