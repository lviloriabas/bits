"""Que pasa con las pestañas del Edge del programa según quién conduce.

El trabajo (consultar el reporte, corregir lo que dice) lo conduce el
programa, y ahí una pestaña de más es una copia vieja que se puede acabar
pilotando por error: ese camino deja el navegador con una sola. Las
búsquedas de Web Search las abre una persona para mirarlas, así que se
suman y las cierra ella.
"""

from __future__ import annotations

from pathlib import Path

from app.airvault import navegador
from app.airvault.navegador import SesionDeNavegador

VERSION = {
    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Edg/130.0.0.0",
}
SIN_VENTANA = {
    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0) HeadlessChrome/130.0.0.0",
}


class _EdgeFalso:
    """Habla el protocolo y apunta lo que se le pidió."""

    ultimo: "_EdgeFalso | None" = None

    def __init__(self, _url, timeout=15.0):
        self.pedidos: list[tuple[str, dict]] = []
        _EdgeFalso.ultimo = self

    def pedir(self, metodo: str, **params) -> dict:
        self.pedidos.append((metodo, params))
        if metodo == "Target.createTarget":
            return {"targetId": "nueva"}
        if metodo == "Target.getTargets":
            return {
                "targetInfos": [
                    {"type": "page", "targetId": "vieja"},
                    {"type": "page", "targetId": "nueva"},
                ]
            }
        return {}

    def cerrar(self) -> None:
        return None

    @classmethod
    def metodos(cls) -> list[str]:
        return [metodo for metodo, _params in cls.ultimo.pedidos]


def _sesion(tmp_path: Path) -> SesionDeNavegador:
    return SesionDeNavegador(tmp_path, edge=Path("msedge.exe"))


def test_una_busqueda_mas_no_cierra_las_que_ya_estaban(monkeypatch, tmp_path):
    monkeypatch.setattr(navegador, "_WebSocket", _EdgeFalso)

    _sesion(tmp_path).sumar_pestana("https://airvault/x", version=VERSION)

    assert "Target.createTarget" in _EdgeFalso.metodos()
    assert "Target.closeTarget" not in _EdgeFalso.metodos()
    # Y la nueva queda al frente, que es a lo que se pidió mirar.
    assert ("Target.activateTarget", {"targetId": "nueva"}) in (
        _EdgeFalso.ultimo.pedidos
    )


def test_el_trabajo_del_programa_sigue_dejando_una_sola(monkeypatch, tmp_path):
    monkeypatch.setattr(navegador, "_WebSocket", _EdgeFalso)

    _sesion(tmp_path).abrir_pestana("https://airvault/x", version=VERSION)

    assert ("Target.closeTarget", {"targetId": "vieja"}) in (
        _EdgeFalso.ultimo.pedidos
    )
    assert ("Target.closeTarget", {"targetId": "nueva"}) not in (
        _EdgeFalso.ultimo.pedidos
    )


def test_la_segunda_busqueda_va_al_navegador_que_ya_estaba(
    monkeypatch, tmp_path
):
    """Sin lanzar otro Edge, que se llevaría por delante al primero."""
    monkeypatch.setattr(navegador, "_WebSocket", _EdgeFalso)
    monkeypatch.setattr(navegador, "_puerto_anotado", lambda _perfil: 9222)
    monkeypatch.setattr(
        navegador, "_version_en", lambda _puerto, timeout=2.0: VERSION
    )
    lanzados: list[str] = []
    monkeypatch.setattr(
        SesionDeNavegador,
        "abrir",
        lambda self, url, espera_s=30.0: lanzados.append(url),
    )

    sesion = _sesion(tmp_path)
    version = sesion.abrir_a_la_vista("https://airvault/x")

    assert version is VERSION
    assert not lanzados
    assert "Target.createTarget" in _EdgeFalso.metodos()
    assert sesion.puerto == 9222


def test_una_busqueda_usa_otro_perfil_si_el_edge_trabaja_sin_ventana(
    monkeypatch, tmp_path
):
    """Sumarle una pestaña a un Edge sin ventana no enseña nada."""
    monkeypatch.setattr(navegador, "_WebSocket", _EdgeFalso)
    monkeypatch.setattr(
        navegador, "_puerto_anotado",
        lambda perfil: 9222 if Path(perfil) == tmp_path else None,
    )
    monkeypatch.setattr(
        navegador, "_version_en", lambda _puerto, timeout=2.0: SIN_VENTANA
    )
    lanzados = []
    version_lectura = {**VERSION, "webSocketDebuggerUrl": "ws://127.0.0.1:9333/x"}

    def abrir(self, url, espera_s=30.0):
        lanzados.append((self.perfil, url))
        return version_lectura

    monkeypatch.setattr(
        SesionDeNavegador,
        "abrir",
        abrir,
    )

    _sesion(tmp_path).abrir_a_la_vista("https://airvault/x")

    assert lanzados == [(tmp_path.with_name(tmp_path.name + "-lectura"), "about:blank")]
    assert "Browser.close" not in _EdgeFalso.metodos()


def test_sin_nadie_en_el_perfil_se_abre_el_navegador(monkeypatch, tmp_path):
    monkeypatch.setattr(navegador, "_puerto_anotado", lambda _perfil: None)
    lanzados: list[str] = []
    monkeypatch.setattr(
        SesionDeNavegador,
        "abrir",
        lambda self, url, espera_s=30.0: lanzados.append(url),
    )

    _sesion(tmp_path).abrir_a_la_vista("https://airvault/x")

    assert lanzados == ["https://airvault/x"]
