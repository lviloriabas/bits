"""Parseo del formulario de acceso, sin tocar la red."""

from __future__ import annotations

import pytest
import requests

from app.airvault.config import AirVaultConfig
from app.airvault.session import (
    Credenciales,
    ErrorDeSesion,
    SesionAirVault,
    _formularios,
    abrir_sesion,
)

LOGIN = """
<html><body>
<form action="/signin2/dosignin" method="post">
  <input type="hidden" name="__RequestVerificationToken" value="tok">
  <input type="text" name="UserName" value="">
  <input type="password" name="Password">
  <input type="submit" value="Sign In">
</form>
</body></html>
"""

WSFED = """
<html><body onload="document.forms[0].submit()">
<form method="post" action="https://airvault.criticaltech.com/index/">
  <input type="hidden" name="wa" value="wsignin1.0">
  <input type="hidden" name="wresult" value="&lt;xml/&gt;">
</form>
</body></html>
"""


def test_formularios_extrae_campos_ocultos():
    formularios = _formularios(LOGIN)
    assert len(formularios) == 1
    accion, metodo, campos = formularios[0]
    assert accion == "/signin2/dosignin" and metodo == "post"
    assert campos["__RequestVerificationToken"] == "tok"


def test_formulario_sin_cierre_no_se_pierde():
    accion, _m, campos = _formularios(
        '<form action="/x"><input name="a" value="1">'
    )[0]
    assert accion == "/x" and campos == {"a": "1"}


def test_encuentra_los_campos_de_usuario_y_clave():
    sesion = SesionAirVault(AirVaultConfig())
    accion, campos, usuario, clave = sesion._formulario_de_login(LOGIN)
    assert (usuario, clave) == ("UserName", "Password")
    assert campos["__RequestVerificationToken"] == "tok"


def test_pagina_sin_formulario_de_acceso():
    sesion = SesionAirVault(AirVaultConfig())
    assert sesion._formulario_de_login("<html></html>") is None


def test_reconoce_el_formulario_de_wsfed():
    campos = _formularios(WSFED)[0][2]
    assert "wresult" in campos and campos["wa"] == "wsignin1.0"


def test_detecta_que_la_pagina_pide_login():
    assert SesionAirVault._pide_login("", "https://x/signin2/") is True
    assert SesionAirVault._pide_login("dosignin", "https://x/index/") is True
    assert SesionAirVault._pide_login("todo bien", "https://x/index/") is False


def test_cookie_vacia_se_rechaza():
    with pytest.raises(ErrorDeSesion):
        SesionAirVault(AirVaultConfig()).usar_cookie("   ")


def test_sin_autenticar_no_se_hacen_peticiones():
    with pytest.raises(ErrorDeSesion):
        SesionAirVault(AirVaultConfig()).get("/index/Batch/GetBatches")


def test_credenciales_desde_entorno(monkeypatch):
    monkeypatch.setenv("AIRVAULT_USER", "luis")
    monkeypatch.setenv("AIRVAULT_PASSWORD", "x")
    credenciales = Credenciales.desde_entorno()
    assert credenciales is not None and credenciales.usuario == "luis"


def test_sin_variables_no_hay_credenciales(monkeypatch):
    monkeypatch.delenv("AIRVAULT_USER", raising=False)
    monkeypatch.delenv("AIRVAULT_PASSWORD", raising=False)
    assert Credenciales.desde_entorno() is None


# ── sesion por cookie (el camino de Entra ID) ──────────────────────

FEDAUTH = "77u/PD94bWwgdmVyc2lvbj0iMS4wIiA/Pg=="


def test_la_cookie_pegada_va_al_tarro_y_no_a_una_cabecera_fija():
    """Puesta a mano en la cabecera, requests la pisa al primer Set-Cookie."""
    sesion = SesionAirVault(AirVaultConfig())
    sesion.usar_cookie(f"FedAuth={FEDAUTH}; ASP.NET_SessionId=abc")
    assert "Cookie" not in sesion.http.headers
    assert sesion.http.cookies.get("FedAuth") == FEDAUTH
    assert sesion.autenticada and sesion.origen == "cookie pegada"


def test_la_sesion_paralela_aisla_conexiones_cookies_y_tokens():
    sesion = SesionAirVault(AirVaultConfig()).usar_cookie("FedAuth=abc")
    sesion._tokens["index"] = "token-original"

    paralela = sesion.clonar()

    assert paralela is not sesion
    assert paralela.http is not sesion.http
    assert paralela.http.cookies is not sesion.http.cookies
    assert paralela.http.cookies.get("FedAuth") == "abc"
    assert paralela._tokens == {"index": "token-original"}
    paralela._tokens["index"] = "token-paralelo"
    paralela.http.cookies.set("SoloParalela", "1")
    assert sesion._tokens["index"] == "token-original"
    assert sesion.http.cookies.get("SoloParalela") is None


def test_cancelar_websearch_no_cancela_la_subida_ni_el_indexado():
    sesion = SesionAirVault(AirVaultConfig()).usar_cookie("FedAuth=abc")
    lectura = sesion.clonar(renovable=False, cancelacion_independiente=True)
    carril = lectura.clonar(renovable=False)
    try:
        lectura.cancelar()
        assert lectura.cancelada and carril.cancelada
        assert not sesion.cancelada
        sesion.cancelar()
        nueva = sesion.clonar(cancelacion_independiente=True)
        assert not nueva.cancelada
        nueva.http.close()
    finally:
        carril.http.close()
        lectura.http.close()
        sesion.http.close()


def test_una_cookie_del_servidor_no_borra_la_pegada():
    """El fallo que esto evita: perder la sesion a mitad de un batch."""
    config = AirVaultConfig()
    sesion = SesionAirVault(config)
    sesion.usar_cookie(f"FedAuth={FEDAUTH}")
    sesion.http.cookies.set(
        "ASP.NET_SessionId", "puesta-por-el-servidor",
        domain="airvault.criticaltech.com", path="/",
    )
    peticion = sesion.http.prepare_request(
        requests.Request("GET", config.url("/index/Batch/GetBatches"))
    )
    enviada = peticion.headers["Cookie"]
    assert f"FedAuth={FEDAUTH}" in enviada
    assert "ASP.NET_SessionId=puesta-por-el-servidor" in enviada


def test_lo_que_no_es_una_cookie_se_rechaza():
    sesion = SesionAirVault(AirVaultConfig())
    with pytest.raises(ErrorDeSesion):
        sesion.usar_cookie("https://airvault.criticaltech.com/index/")


def test_detecta_la_pagina_de_microsoft():
    assert SesionAirVault._pide_login(
        "", "https://login.microsoftonline.com/9767f0dc/wsfed"
    ) is True


def test_comprobar_devuelve_cuantos_lotes_ve(monkeypatch):
    sesion = SesionAirVault(AirVaultConfig())
    sesion.usar_cookie(f"FedAuth={FEDAUTH}")
    monkeypatch.setattr(sesion, "get", lambda *a, **k: {"records": 22})
    assert sesion.comprobar() == 22


def test_comprobar_avisa_de_la_sesion_caducada(monkeypatch):
    sesion = SesionAirVault(AirVaultConfig())
    sesion.usar_cookie(f"FedAuth={FEDAUTH}")

    def caducada(*_a, **_k):
        raise ErrorDeSesion("La sesion de AirVault caduco.")

    monkeypatch.setattr(sesion, "get", caducada)
    with pytest.raises(ErrorDeSesion):
        sesion.comprobar()


# ── orden de preferencia al abrir sesion ───────────────────────────

def _sin_entorno(monkeypatch):
    for variable in ("AIRVAULT_COOKIE", "AIRVAULT_USER", "AIRVAULT_PASSWORD"):
        monkeypatch.delenv(variable, raising=False)


def test_la_cookie_explicita_gana_a_la_del_entorno(monkeypatch):
    _sin_entorno(monkeypatch)
    monkeypatch.setenv("AIRVAULT_COOKIE", "FedAuth=del-entorno")
    sesion = abrir_sesion(AirVaultConfig(), cookie="FedAuth=explicita")
    assert sesion.http.cookies.get("FedAuth") == "explicita"


def test_sin_cookie_explicita_se_usa_la_del_entorno(monkeypatch):
    _sin_entorno(monkeypatch)
    monkeypatch.setenv("AIRVAULT_COOKIE", "FedAuth=del-entorno")
    sesion = abrir_sesion(AirVaultConfig())
    assert sesion.http.cookies.get("FedAuth") == "del-entorno"
    assert sesion.origen == "cookie pegada"


def test_sin_cookie_se_toma_del_navegador(monkeypatch):
    _sin_entorno(monkeypatch)
    from app.airvault import navegador

    monkeypatch.setattr(
        navegador, "obtener_cookies",
        lambda *a, **k: {"FedAuth": "del-navegador"},
    )
    sesion = abrir_sesion(AirVaultConfig())
    assert sesion.http.cookies.get("FedAuth") == "del-navegador"
    assert sesion.origen == "navegador"


def test_el_navegador_no_se_abre_cuando_se_pide_que_no(monkeypatch):
    """Nadie quiere que le salte una ventana sin haberla pedido."""
    _sin_entorno(monkeypatch)
    from app.airvault import navegador

    def no_deberia(*_a, **_k):
        raise AssertionError("no habia que abrir el navegador")

    monkeypatch.setattr(navegador, "obtener_cookies", no_deberia)
    with pytest.raises(ErrorDeSesion):
        abrir_sesion(AirVaultConfig(), usar_edge=False)


def test_si_el_navegador_falla_se_explica_por_que(monkeypatch):
    """Sin el motivo, 'no hay sesion' no le dice nada a nadie."""
    _sin_entorno(monkeypatch)
    from app.airvault import navegador

    def sin_edge(*_a, **_k):
        raise navegador.ErrorDeNavegador("no se encontro Microsoft Edge")

    monkeypatch.setattr(navegador, "obtener_cookies", sin_edge)
    with pytest.raises(ErrorDeSesion) as fallo:
        abrir_sesion(AirVaultConfig())
    assert "no se encontro Microsoft Edge" in str(fallo.value)
    assert "AIRVAULT_COOKIE" in str(fallo.value)


def test_sin_ninguna_fuente_se_dice_como_conseguir_la_cookie(monkeypatch):
    _sin_entorno(monkeypatch)
    from app.airvault import navegador

    monkeypatch.setattr(
        navegador, "obtener_cookies",
        lambda *a, **k: (_ for _ in ()).throw(
            navegador.ErrorDeNavegador("no arranco")
        ),
    )
    with pytest.raises(ErrorDeSesion) as fallo:
        abrir_sesion(AirVaultConfig())
    assert "AIRVAULT_COOKIE" in str(fallo.value)
