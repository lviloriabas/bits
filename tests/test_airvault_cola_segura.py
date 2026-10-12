"""Ausencias, cargas parciales y turnos entre ejecuciones sin servidor real."""

from threading import Event, Thread
from types import SimpleNamespace

import pytest

from app.airvault import flujo
from app.airvault.config import AirVaultConfig
from app.airvault.flujo import NO_ENCONTRADO, Trabajo, estado_local, subir_partes
from app.airvault.model import EstadoEtapa, Manifiesto, Registro
from app.airvault.session import SesionCancelada
from tests.airvault_fake import AirVaultSimulado, lote, subida_simulada


def trabajos(raiz, cantidad=2, paginas=5, **config):
    resultado = []
    for n in range(cantidad):
        m = Manifiesto(job_id=str(n), nombre_batch=f"DP | EJECUCION {n}",
                       registros=[Registro(seq=i + 1, log_number=str(2000000 + n * 10000 + i))
                                  for i in range(paginas)])
        t = Trabajo(AirVaultConfig(**config), raiz / "output" / "airvault" / str(n), m)
        t.guardar()
        resultado.append(t)
    return resultado


@pytest.mark.parametrize("paginas,segundos", [(1, 300), (50, 300), (500, 1800), (2000, 3600)])
def test_plazo_por_tamano(tmp_path, paginas, segundos):
    t = trabajos(tmp_path, 1, paginas)[0]
    assert flujo.espera_para_darla_por_perdida(t) == segundos
    assert segundos - 2 <= flujo.limite_para_publicacion(t) <= segundos


def test_la_ausencia_vencida_se_conserva_y_la_siguiente_carga_continua(tmp_path, monkeypatch):
    uno, dos = trabajos(tmp_path, espera_reenvio_s=30, espera_maxima_s=30, espera_descubrimiento_s=20)
    cliente = AirVaultSimulado()
    cargas, esperas, indexados = [], [], []
    normal = subida_simulada(cliente, vueltas=0)

    def subir(self, *args, **kwargs):
        cargas.append(self)
        if self is uno:
            self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
            self.guardar()
        else:
            normal(self, *args, **kwargs)

    monkeypatch.setattr(Trabajo, "subir", subir)
    fallos = subir_partes([uno, dos], SimpleNamespace(cancelada=False), cliente=cliente,
                         dormir=esperas.append, al_encontrar=lambda t, todos: indexados.append(t))
    assert cargas == [uno, dos]
    assert indexados == [dos]
    assert fallos and fallos[0][0] is uno
    assert 29 <= sum(esperas) <= 30
    recargado = Trabajo.cargar(uno.config, uno.carpeta)
    assert estado_local(recargado).estado == NO_ENCONTRADO
    assert estado_local(recargado).se_puede_subir
    assert recargado.manifiesto.etapa_hecha("subir")
    assert dos.manifiesto.batch_id
    # Otra vuelta automática conserva el ausente y no crea una segunda copia.
    subir_partes([uno], SimpleNamespace(cancelada=False), cliente=cliente, dormir=lambda s: None)
    assert cargas == [uno, dos]


def test_una_carga_parcial_impide_que_airvault_junte_la_siguiente(tmp_path, monkeypatch):
    uno, dos = trabajos(tmp_path, espera_reenvio_s=1, espera_maxima_s=1)
    cliente = AirVaultSimulado()
    cargas = []

    def subir(self, *args, **kwargs):
        cargas.append(self)
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
        self.guardar()
        cliente.lotes.append(lote("PARCIAL", self.manifiesto.nombre_batch, 1))

    monkeypatch.setattr(Trabajo, "subir", subir)
    assert subir_partes([uno, dos], None, cliente=cliente, dormir=lambda s: None)
    assert cargas == [uno]
    assert not uno.manifiesto.no_encontrado_desde
    assert not dos.manifiesto.etapa_hecha("subir")


@pytest.mark.parametrize("cancelado", [False, True])
def test_una_carga_parcial_en_otra_ventana_conserva_la_proteccion(tmp_path, monkeypatch, cancelado):
    uno, dos = trabajos(tmp_path, espera_reenvio_s=1, espera_maxima_s=1)
    uno.manifiesto.etapa("subir").marcar(EstadoEtapa.EN_CURSO)
    uno.manifiesto.cancelado = cancelado
    uno.guardar()
    cliente = AirVaultSimulado()
    cliente.lotes.append(lote("PARCIAL", uno.manifiesto.nombre_batch, 1))
    cargas = []
    monkeypatch.setattr(Trabajo, "subir", lambda self, *a, **k: cargas.append(self))
    fallos = subir_partes([dos], None, cliente=cliente, dormir=lambda s: None)
    assert not cargas
    assert fallos and fallos[0][0] is dos
    assert "confirmar la anterior" in fallos[0][1]
    assert Trabajo.cargar(uno.config, uno.carpeta).manifiesto.cancelado == cancelado


def test_una_carga_ausente_de_otra_ejecucion_no_se_reenvia_al_continuar(tmp_path, monkeypatch):
    uno, dos = trabajos(tmp_path, espera_reenvio_s=1, espera_maxima_s=1)
    uno.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    uno.manifiesto.etapa("subir").actualizada = "2020-01-01T00:00:00"
    uno.guardar()
    cliente = AirVaultSimulado()
    normal = subida_simulada(cliente, vueltas=0)
    cargas = []

    def subir(self, *args, **kwargs):
        cargas.append(self)
        normal(self, *args, **kwargs)

    monkeypatch.setattr(Trabajo, "subir", subir)
    assert not subir_partes([dos], None, cliente=cliente, dormir=lambda s: None)
    assert cargas == [dos]
    assert dos.manifiesto.batch_id
    assert estado_local(Trabajo.cargar(uno.config, uno.carpeta)).estado == NO_ENCONTRADO


@pytest.mark.parametrize("error", [RuntimeError("sin conexión"), SesionCancelada("cancelado")])
def test_un_error_de_lectura_no_demuestra_una_ausencia(tmp_path, error):
    t = trabajos(tmp_path, 1, espera_reenvio_s=0)[0]
    t.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    t.guardar()

    def listar():
        raise error

    with pytest.raises(type(error)):
        t.descubrir(SimpleNamespace(listar_lotes=listar), esperar=True,
                    limite_s=0, dormir=lambda s: None)
    assert not t.manifiesto.no_encontrado_desde


def test_dos_ejecuciones_esperan_turno_hasta_terminar_la_carga_anterior(tmp_path, monkeypatch):
    uno, dos = trabajos(tmp_path)
    entro, liberar, segundo_entro, esperando = Event(), Event(), Event(), Event()
    orden, errores = [], []

    def subir(self, *args, **kwargs):
        orden.append(self)
        if self is uno:
            entro.set()
            assert liberar.wait(5)
        else:
            segundo_entro.set()
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)

    def avisar(texto, *args):
        if "Esperando turno" in texto:
            esperando.set()

    def ejecutar(t):
        try:
            subir_partes([t], SimpleNamespace(cancelada=False), avisar=avisar)
        except BaseException as exc:
            errores.append(exc)

    monkeypatch.setattr(Trabajo, "subir", subir)
    primero, segundo = Thread(target=ejecutar, args=(uno,)), Thread(target=ejecutar, args=(dos,))
    primero.start()
    try:
        assert entro.wait(5)
        segundo.start()
        assert esperando.wait(5)
        assert not segundo_entro.is_set()
    finally:
        liberar.set()
        primero.join(5)
        if segundo.ident is not None:
            segundo.join(5)
    assert not primero.is_alive() and not segundo.is_alive()
    assert not errores
    assert orden == [uno, dos]
