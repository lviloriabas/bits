"""Las verificaciones comparten un limite; las subidas conservan su turno."""

import base64
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.airvault.client import ClienteHttp, PaginaDelLote
from app.airvault.config import AirVaultConfig
from app.airvault.flujo import EstadoParte, LISTO, Trabajo, detectar_indexados, verificar_partes
from app.airvault.indexer import verificar_lotes
from app.airvault.mapping import valores_de_indice
from app.airvault.memoria import observaciones_de_websearch
from app.airvault.model import Manifiesto, Registro
from app.airvault.session import ErrorDeSesion, SesionCancelada
from app.airvault.uploader import SubidorQuickUpload
from app.airvault.websearch import Buscador, revisar_batch
from tests.airvault_fake import pagina


def manifiesto(batch_id):
    return Manifiesto(
        job_id=batch_id, batch_id=batch_id, nombre_batch=f"DP | {batch_id}",
        registros=[Registro(seq=n, matricula="HP-1848CMP", log_number=f"231223{n}",
                            fecha="2026/08/31", fleet="NG") for n in (1, 2)],
    )


class ClienteLecturas:
    def __init__(self, propios):
        self.propios = {m.batch_id: m for m in propios}
        self.barrera = threading.Barrier(2, timeout=5)
        self.lecturas = []
        self.conexiones = []

    def carriles(self):
        return 2

    def carril(self):
        conexion = SimpleNamespace(
            leer_pagina=self.leer_pagina, paginas_del_lote=self.paginas_del_lote,
            cerrada=False,
        )
        conexion.cerrar_conexiones = lambda: setattr(conexion, "cerrada", True)
        self.conexiones.append(conexion)
        return conexion

    def paginas_del_lote(self, batch_id):
        self.barrera.wait()
        return [PaginaDelLote(n, 3, n) for n in (1, 2)]

    def leer_pagina(self, batch_id, numero):
        self.lecturas.append((batch_id, numero))
        self.barrera.wait()
        propio = self.propios[batch_id]
        registro = propio.registros[numero - 1]
        return pagina(numero, estado=0, valores=valores_de_indice(
            registro, propio.doc_type, propio.audit_status, propio.nombre_batch,
        ))


def test_verifica_paginas_de_dos_batches_a_la_vez_y_reintenta_solo_las_dudosas():
    propios = [manifiesto("003A"), manifiesto("003B")]
    cliente = ClienteLecturas(propios)
    principal = threading.get_ident()
    avances = []
    esperas = []
    original = cliente.leer_pagina
    intentos = {}

    def leer(batch_id, numero):
        intento = intentos.get((batch_id, numero), 0) + 1
        intentos[batch_id, numero] = intento
        if intento == 1:
            remota = original(batch_id, numero)
            # La primera pagina de cada batch aun esta amarilla.
            if numero == 1:
                remota.estado = 3
            return remota
        return original(batch_id, numero)

    cliente.leer_pagina = leer

    def avanzar(propio, hechas, total):
        assert threading.get_ident() == principal
        avances.append((propio.batch_id, hechas, total))

    cuentas = verificar_lotes(cliente, propios, esperas=(1,), dormir=esperas.append,
                              al_avanzar=avanzar)

    assert cuentas == [(2, 2, []), (2, 2, [])]
    assert {batch_id for batch_id, _ in cliente.lecturas[:2]} == {"003A", "003B"}
    assert intentos == {("003A", 1): 2, ("003A", 2): 1,
                        ("003B", 1): 2, ("003B", 2): 1}
    assert esperas == [1]
    assert all(c.cerrada for c in cliente.conexiones)
    assert ("003A", 2, 2) in avances and ("003B", 2, 2) in avances


def test_el_estado_de_varios_batches_se_consulta_simultaneamente():
    propios = [manifiesto("003A"), manifiesto("003B")]
    cliente = ClienteLecturas(propios)
    partes = [EstadoParte(SimpleNamespace(manifiesto=m), LISTO, "") for m in propios]

    assert detectar_indexados(partes, cliente) == partes
    assert cliente.lecturas == []
    assert all(c.cerrada for c in cliente.conexiones)


def test_guarda_las_verificaciones_normales_y_revisar_en_el_hilo_propietario(tmp_path, monkeypatch):
    propios = [manifiesto("003A"), manifiesto("003B")]
    propios[1].solo_subir = True
    trabajos = [Trabajo(AirVaultConfig(), tmp_path / m.job_id, m) for m in propios]
    cliente = ClienteLecturas(propios)
    principal = threading.get_ident()
    guardados = []
    avances = []

    def guardar(trabajo):
        assert threading.get_ident() == principal
        guardados.append(trabajo.manifiesto.batch_id)

    def anotar(*_args):
        assert threading.get_ident() == principal

    monkeypatch.setattr(Trabajo, "guardar", guardar)
    monkeypatch.setattr("app.airvault.flujo.registro_entrega.anotar", anotar)

    def avanzar(_texto, hechas, total):
        assert threading.get_ident() == principal
        avances.append((hechas, total))

    assert verificar_partes(trabajos, cliente, avisar=avanzar) == (4, 4, [])
    assert guardados == ["003A", "003B"]
    assert all(m.etapa_hecha("verificar") for m in propios)
    assert "revision humana" in propios[1].etapa("verificar").detalle
    assert [hechas for hechas, _ in avances] == sorted(hechas for hechas, _ in avances)
    assert avances[-1] == (4, 4)


class SesionBusqueda:
    def __init__(self, dueno=None):
        self.dueno = dueno or self
        if dueno is None:
            self.conexiones = []
            self.barrera = threading.Barrier(2, timeout=5)
            self.pedidos = []
            self.fallo = None
        self.cerrada = False
        self.http = SimpleNamespace(close=lambda: setattr(self, "cerrada", True))

    def clonar(self, renovable=True):
        assert not renovable
        conexion = SesionBusqueda(self)
        self.conexiones.append(conexion)
        return conexion

    def get(self, _ruta, params):
        numero = base64.b64decode(params["encodedValues"]).decode().split("=", 1)[1]
        self.dueno.pedidos.append((numero, self is self.dueno))
        if self is not self.dueno:
            if self.dueno.fallo is not None:
                raise self.dueno.fallo("sin respuesta")
            self.dueno.barrera.wait()
        return {"rows": [{"cell": {"C_LogNo": numero, "Aircraft": "HP-1848CMP"}}]}


def buscador(sesion, controles=("2312238",)):
    return Buscador(sesion, AirVaultConfig(
        ruta_websearch="/zfp/Search/Search", parametros_websearch="encodedValues",
        carriles_indexado=2,
    ), controles=controles)


def test_websearch_consulta_a_la_vez_sin_repetir_numeros_ni_el_control():
    sesion = SesionBusqueda()
    propio = buscador(sesion)

    resultado = revisar_batch(propio, ["2312231", "2312232", "2312231"])
    repetido = revisar_batch(propio, ["2312231", "2312232"])

    assert resultado.publicadas == repetido.publicadas == ["2312231", "2312232"]
    assert sesion.pedidos[0] == ("2312238", True)
    assert sorted(sesion.pedidos[1:]) == [("2312231", False), ("2312232", False)]
    assert all(c.cerrada for c in sesion.conexiones)


def test_websearch_recupera_la_sesion_en_la_principal_y_propaga_la_cancelacion():
    sesion = SesionBusqueda()
    sesion.fallo = ErrorDeSesion
    assert buscador(sesion).consultar_publicadas(["2312231", "2312232"])["2312232"].publicada
    assert {n for n, principal in sesion.pedidos if principal} == {
        "2312238", "2312231", "2312232",
    }
    sesion.fallo = SesionCancelada
    with pytest.raises(SesionCancelada):
        buscador(sesion).consultar_publicadas(["2312231", "2312232"])


def test_la_memoria_consulta_libros_en_paralelo_y_el_progreso_llega_al_propietario():
    sesion = SesionBusqueda()
    principal = threading.get_ident()
    avances = []

    def avanzar(hechas, total):
        assert threading.get_ident() == principal
        avances.append((hechas, total))

    observaciones = observaciones_de_websearch(
        buscador(sesion), ["23122A", "23122B"], cuantas=2,
        al_avanzar=avanzar,
    )

    assert [o.log_number for o in observaciones] == ["2312200", "2312249", "2312250", "2312299"]
    assert avances == [(1, 2), (2, 2)]
    assert any(not principal for _, principal in sesion.pedidos)


def test_descubre_la_ruta_antes_de_repartir_sin_aceptar_ausencias_sin_control():
    sesion = SesionBusqueda()
    propio = buscador(sesion, controles=())
    indices = propio.consultar_indices(["2312231", "2312232", "2312233"])
    assert all(i is not None for i in indices.values())
    assert any(not principal for _, principal in sesion.pedidos)
    antes = len(sesion.pedidos)
    assert propio.consultar_publicadas(["2312240", "2312241"])["2312240"].publicada is None
    assert len(sesion.pedidos) == antes


def test_un_carril_configurado_tambien_serializa_la_busqueda_de_batches():
    sesion = SimpleNamespace(get=lambda *_: [], clonar=lambda **_: pytest.fail("abre carril"))
    cliente = ClienteHttp(sesion, AirVaultConfig(carriles_indexado=1))
    assert list(cliente.buscar_lotes(["A", "B"])) == [("A", []), ("B", [])]


def test_dos_subidas_concurrentes_conservan_todos_los_trozos_y_el_cierre_en_orden(tmp_path):
    barrera = threading.Barrier(2, timeout=5)
    eventos = []
    candado = threading.Lock()
    primera_peticion = threading.Event()
    permitir_cierre = threading.Event()
    activo = None

    class Sesion:
        def post(self, ruta, **extra):
            nonlocal activo
            nombre = extra.get("data", {}).get("filename") or extra["json"]["model"]["FileName"]
            with candado:
                if ruta.endswith("Upload/"):
                    assert activo is None or activo == nombre
                    activo = nombre
                else:
                    assert activo == nombre
                    activo = None
                eventos.append((nombre, ruta.rsplit("/", 2)[-2:]))
            if not primera_peticion.is_set():
                primera_peticion.set()
                assert permitir_cierre.wait(5)

    rutas = [tmp_path / "a.pdf", tmp_path / "b.pdf"]
    for ruta in rutas:
        ruta.write_bytes(b"x" * (1024 * 1024 + 1))

    def subir(ruta):
        barrera.wait()
        return SubidorQuickUpload(Sesion(), 3209).subir(ruta, {})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futuros = [pool.submit(subir, ruta) for ruta in rutas]
        try:
            assert primera_peticion.wait(5)
            # La otra tarea ya paso la barrera y espera el turno de subida.
            # Darle tiempo mientras el primer archivo sigue abierto permite
            # detectar que sus trozos se cuelen antes de FinishUpload.
            assert not permitir_cierre.wait(0.05)
        finally:
            permitir_cierre.set()
        assert all(futuro.result().ok for futuro in futuros)
    assert [nombre for nombre, _ in eventos] in (
        ["a.pdf"] * 3 + ["b.pdf"] * 3,
        ["b.pdf"] * 3 + ["a.pdf"] * 3,
    )
