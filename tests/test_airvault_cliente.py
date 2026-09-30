"""La forma de las peticiones que el cliente le manda a AirVault.

No es cosmética. El Web Index guarda una página por POST y esa ruta **no
existe** por GET: ASP.NET contesta «The resource cannot be found» con un
404, que llegaba al reporte como «esa página ya no está en el batch» y dejó
un batch entero sin escribir una sola página. Aquí se fija el método y los
parámetros de cada llamada que escribe.
"""

from __future__ import annotations

import pytest
import threading
from types import SimpleNamespace

from app.airvault.client import ClienteHttp, RespuestaInesperada
from app.airvault.config import AirVaultConfig
from app.airvault.encoding import codificar_batch_id, decodificar_valores


class SesionFalsa:
    """Anota lo que se le pide sin tocar la red."""

    def __init__(self, respuesta=None):
        self.gets: list = []
        self.posts: list = []
        self.respuesta = respuesta if respuesta is not None else {}

    def get(self, ruta, params=None, **_extra):
        self.gets.append((ruta, dict(params or {})))
        return self.respuesta

    def post_json(self, ruta, **extra):
        self.posts.append((ruta, dict(extra.get("data") or {})))
        return self.respuesta

    def post(self, ruta, **extra):
        self.posts.append((ruta, dict(extra.get("data") or {})))
        return self.respuesta


def cliente(respuesta=None):
    return ClienteHttp(SesionFalsa(respuesta), AirVaultConfig())


def test_busqueda_paralela_acotada_con_sesiones_independientes():
    barrera = threading.Barrier(4)
    creador = threading.get_ident()
    sesiones = []

    class Conexion:
        def __init__(self):
            self.cerrada = False
            self.http = SimpleNamespace(close=self.cerrar)

        def cerrar(self):
            self.cerrada = True

        def get(self, ruta, params):
            assert threading.get_ident() != creador
            barrera.wait(timeout=5)
            return []

    class Sesion:
        def clonar(self, renovable=True):
            assert threading.get_ident() == creador
            # Un carril no abre Edge por su cuenta.
            assert renovable is False
            conexion = Conexion()
            sesiones.append(conexion)
            return conexion

    cli = ClienteHttp(Sesion(), AirVaultConfig())
    nombres = [f"Batch {n}" for n in range(8)]
    resultados = list(cli.buscar_lotes(nombres + nombres))
    assert dict(resultados) == dict.fromkeys(nombres, [])
    assert len(sesiones) == 8
    assert all(sesion.cerrada for sesion in sesiones)


def test_busqueda_paralela_propaga_error_y_cierra_conexiones():
    conexiones = []

    class Sesion:
        def clonar(self, renovable=True):
            conexion = Sesion()
            conexion.cerrada = False
            conexion.http = SimpleNamespace(close=lambda: setattr(conexion, "cerrada", True))
            conexiones.append(conexion)
            return conexion

        def get(self, *_args):
            raise RuntimeError("sin conexion")

    cli = ClienteHttp(Sesion(), AirVaultConfig())
    with pytest.raises(RuntimeError, match="sin conexion"):
        list(cli.buscar_lotes([f"Batch {n}" for n in range(20)]))
    assert len(conexiones) == 4
    assert all(conexion.cerrada for conexion in conexiones)


def test_busqueda_paralela_repite_en_la_sesion_principal_lo_que_el_carril_no_pudo():
    """Un carril con la sesion caducada no abre Edge: lo repite la principal."""
    from app.airvault.session import ErrorDeSesion

    principales: list[str] = []

    class Carril:
        http = SimpleNamespace(close=lambda: None)

        def get(self, _ruta, params):
            raise ErrorDeSesion("AirVault volvio a pedir acceso")

    class Sesion:
        def clonar(self, renovable=True):
            return Carril()

        def get(self, _ruta, params):
            principales.append(params["encodedFilter"])
            return [["003A", "Batch", 1, 3209, "", "", "", ""]]

    cli = ClienteHttp(Sesion(), AirVaultConfig())
    resultados = dict(cli.buscar_lotes(["Batch 1", "Batch 2"]))

    assert set(resultados) == {"Batch 1", "Batch 2"}
    assert all(len(lotes) == 1 for lotes in resultados.values())
    assert len(principales) == 2


def test_guardar_una_pagina_va_por_post():
    """Por GET la ruta ni existe y el 404 se leía como página borrada."""
    cli = cliente({"ok": True})
    cli.guardar_pagina("003SUS", 7, {9633: "HP-1848CMP"}, 0)
    assert cli.sesion.gets == []
    ruta, datos = cli.sesion.posts[0]
    assert ruta.endswith("/FormsProcessing/SaveAndGetIndexFields")
    assert datos["encodedBatchId"] == codificar_batch_id("003SUS")
    assert datos["page"] == 7
    assert decodificar_valores(datos["encodedValues"]) == {9633: "HP-1848CMP"}


def test_guardar_deja_abierta_la_pagina_que_se_pida():
    cli = cliente({"ok": True})
    cli.guardar_pagina("003SUS", 7, {}, 0, pagina_siguiente=8)
    _ruta, datos = cli.sesion.posts[0]
    assert datos["nextPageToOpen"] == 8


@pytest.mark.parametrize("indicador", [True, "true", "True", 1])
def test_guardar_no_confunde_rechazo_de_fecha_con_exito(indicador):
    from app.airvault.session import ErrorDeAirVault

    cli = cliente({"IsError": indicador, "Message": "End Date is required"})
    with pytest.raises(ErrorDeAirVault, match="End Date is required"):
        cli.guardar_pagina("003SUS", 7, {9593: "08/31/2026"}, 0)


def test_ponerle_nombre_al_lote_va_por_post_y_en_base64():
    """Quick Upload no admite nombre: todos llegan como «Empty-Batch».

    El nombre se le pone después, con la misma acción «Rename» que ofrece
    el Web Index; sin ella, en la pantalla no se distingue un batch de otro.
    """
    cli = cliente({})
    assert cli.renombrar_lote("003SUS", "DP | BITS 19 AUG 2026 17 33")
    ruta, datos = cli.sesion.posts[0]
    assert ruta.endswith("/Batch/UpdateBatchName")
    assert datos["batchId"] == "003SUS"
    import base64
    nombre = base64.b64decode(datos["encodedBatchName"]).decode("utf-8")
    assert nombre == "DP | BITS 19 AUG 2026 17 33"


def test_un_nombre_vacio_no_se_manda():
    """Renombrar a nada dejaría el batch peor de lo que estaba."""
    cli = cliente({})
    assert not cli.renombrar_lote("003SUS", "  ")
    assert cli.sesion.posts == []


def test_si_el_renombrado_falla_el_cliente_lo_informa():
    """El flujo usa False para impedir que un Empty-Batch llegue a indexarse."""
    class SesionRota(SesionFalsa):
        def post(self, *_a, **_k):
            raise RuntimeError("500")

    cli = ClienteHttp(SesionRota(), AirVaultConfig())
    assert not cli.renombrar_lote("003SUS", "DP | BITS")


def test_completar_exige_una_confirmacion_explicita_de_airvault():
    cli = cliente({})

    with pytest.raises(RespuestaInesperada, match="no confirmo"):
        cli.completar_lote("003SUS")


def test_completar_acepta_iserror_false():
    cli = cliente({"IsError": False, "Message": ""})

    respuesta = cli.completar_lote("003SUS")

    assert respuesta["IsError"] is False


def test_completar_rechaza_el_error_que_devuelve_airvault():
    cli = cliente({"IsError": True, "Message": "faltan paginas"})

    with pytest.raises(RespuestaInesperada, match="faltan paginas"):
        cli.completar_lote("003SUS")


def test_validar_batch_usa_la_ruta_y_el_formato_del_boton_complete():
    cli = cliente([{"Sequence": 2, "Status": 0}])

    respuesta = cli.validar_batch("003SUS", [7, 2, 7])

    ruta, datos = cli.sesion.posts[0]
    assert ruta.endswith("/Batch/UpdateBatchValidationQuery")
    assert datos == {
        "batchId": "003SUS",
        "repoId": 3209,
        "pageRangeDelimiter": "2,7",
    }
    assert respuesta == [{"Sequence": 2, "Status": 0}]


def test_validar_batch_rechaza_una_respuesta_que_no_se_puede_comprobar():
    cli = cliente({"ok": True})

    with pytest.raises(RespuestaInesperada, match="resultado de validar"):
        cli.validar_batch("003SUS", [1])
