"""La limpieza libera memoria sin retirar paginas de otro trabajo."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.airvault import correcciones, navegador, web_reports


VERSION = {"webSocketDebuggerUrl": "ws://127.0.0.1:4321/browser/prueba"}


@pytest.fixture
def edge(monkeypatch):
    class Edge:
        paginas = {}
        pedidos = []
        contador = 0
        fallar_cierre = set()

        def __init__(self, *_args, **_kwargs):
            pass

        def pedir(self, metodo, **params):
            self.pedidos.append((metodo, params))
            if metodo == "Target.createTarget":
                Edge.contador += 1
                target_id = f"pagina-{Edge.contador}"
                self.paginas[target_id] = {
                    "targetId": target_id, "type": "page", **params,
                }
                return {"targetId": target_id}
            if metodo == "Target.getTargets":
                return {"targetInfos": list(self.paginas.values())}
            if metodo == "Target.closeTarget":
                target_id = params["targetId"]
                if target_id in self.fallar_cierre:
                    raise OSError("El cierre no respondio")
                self.paginas.pop(target_id, None)
                return {"success": True}
            return {}

        def cerrar(self):
            pass

    for modulo in (navegador, correcciones, web_reports):
        monkeypatch.setattr(modulo, "_WebSocket", Edge)
    navegador.mantener_navegadores_hasta_el_cierre()
    return Edge


def _sesion(tmp_path):
    sesion = navegador.SesionDeNavegador(tmp_path, edge=Path("msedge.exe"))
    sesion._version = VERSION
    return sesion


def test_restaura_sin_cerrar_trabajos_lecturas_servicios_ni_controladores(
    edge, tmp_path,
):
    sesion = _sesion(tmp_path)
    reporte = sesion.abrir_pestana("https://airvault/reporte")
    corrector = correcciones._NavegadorDeCorrecciones(tmp_path)
    caso = corrector.abrir_pestana("https://airvault/caso", VERSION)
    lectura = sesion.sumar_pestana("https://airvault/libro")
    edge.paginas.update({
        "vieja": {"type": "page", "targetId": "vieja"},
        "externa": {"type": "page", "targetId": "externa", "attached": True},
        "servicio": {"type": "service_worker", "targetId": "servicio"},
    })
    otra = _sesion(tmp_path)
    nueva = otra.abrir_pestana("https://airvault/otro", insistir=True)

    assert "vieja" not in edge.paginas
    assert {reporte, caso, lectura, nueva, "externa", "servicio"} <= edge.paginas.keys()
    otra.cerrar()
    assert nueva not in edge.paginas
    assert {reporte, caso, lectura} <= edge.paginas.keys()
    assert not any(m == "Browser.close" for m, _ in edge.pedidos)


def test_dos_casos_terminan_sin_retirar_el_reporte(edge, tmp_path):
    reporte = _sesion(tmp_path).abrir_pestana("https://airvault/reporte")
    corrector = correcciones._NavegadorDeCorrecciones(tmp_path)
    barrera = Barrier(2, timeout=3)

    def caso(numero):
        target_id = corrector.abrir_pestana(f"https://airvault/{numero}", VERSION)
        barrera.wait()
        assert reporte in edge.paginas
        assert target_id in edge.paginas
        corrector.cerrar_pestana(VERSION, target_id)
        return target_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        cerradas = list(pool.map(caso, range(2)))

    assert reporte in edge.paginas
    assert not any(target_id in edge.paginas for target_id in cerradas)
    assert navegador._PESTANAS_EN_USO[VERSION["webSocketDebuggerUrl"]] == {reporte}


def test_reintenta_la_pestana_liberada_cuyo_cierre_fallo(edge, tmp_path):
    sesion = _sesion(tmp_path)
    terminado = sesion.abrir_pestana("https://airvault/terminado")
    activo = sesion.abrir_pestana("https://airvault/activo")
    edge.fallar_cierre.add(terminado)
    pagina = object.__new__(web_reports._Pagina)
    pagina.ws = edge()
    pagina._version = VERSION
    pagina._target_id = terminado
    pagina.cerrar()

    assert pagina.ws is None
    assert terminado in edge.paginas
    clave = VERSION["webSocketDebuggerUrl"]
    assert terminado not in navegador._PESTANAS_EN_USO[clave]
    assert terminado in navegador._PESTANAS_LIBERADAS[clave]
    edge.fallar_cierre.clear()
    nueva = sesion.abrir_pestana("https://airvault/nueva")

    assert terminado not in edge.paginas
    assert {activo, nueva} <= edge.paginas.keys()
    assert not navegador._PESTANAS_LIBERADAS[clave]


def test_muchas_consultas_dejan_solo_la_pagina_vacia(edge, tmp_path):
    for numero in range(30):
        sesion = _sesion(tmp_path)
        sesion.abrir_pestana(f"https://airvault/reporte/{numero}")
        sesion.cerrar()

    assert len(edge.paginas) == 1
    assert next(iter(edge.paginas.values()))["url"] == "about:blank"
    assert not navegador._PESTANAS_EN_USO[VERSION["webSocketDebuggerUrl"]]


def test_no_reinicia_el_navegador_si_otra_tarea_lo_usa(edge, tmp_path, monkeypatch):
    version = {**VERSION, "User-Agent": "HeadlessChrome/151"}
    navegador.registrar_pestana(version, "activo")
    monkeypatch.setattr(navegador, "_version_en", lambda _puerto: version)
    sesion = _sesion(tmp_path)
    sesion.visible = True
    with pytest.raises(navegador.ErrorDeNavegador, match="trabajo activo"):
        sesion._aprovechar(4321, "https://airvault/login")

    assert not any(m == "Browser.close" for m, _ in edge.pedidos)


def test_un_error_al_listar_pestanas_no_rompe_la_apertura(edge, tmp_path, monkeypatch):
    pedir = edge.pedir

    def fallar_lista(self, metodo, **params):
        if metodo == "Target.getTargets":
            raise OSError("No contesto la lista")
        return pedir(self, metodo, **params)

    monkeypatch.setattr(edge, "pedir", fallar_lista)
    sesion = _sesion(tmp_path)
    target_id = sesion.abrir_pestana("https://airvault/reporte")
    assert target_id in edge.paginas
    sesion.cerrar()
    assert target_id not in edge.paginas
