"""Los batches abiertos a nombre propio se sueltan; los de otro usuario, no.

AirVault admite un solo dueno por batch. Un batch que quedo tomado por el
usuario del programa (se cerro a media escritura, o AirVault lo tomo y
contesto con un error) dejaba de indexarse para siempre: la cola lo mostraba
abierto por alguien y el programa no toca un batch abierto.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.airvault import identidad
from app.airvault.config import AirVaultConfig
from app.airvault.flujo import (INCOMPLETO, LISTO, TOMADO, Trabajo,
                                comprobar_partes, soltar_propios)
from app.airvault.model import EstadoEtapa
from app.airvault.session import ErrorDeAirVault, ErrorDeConexion
from tests.airvault_fake import ClienteFalso, lote, pagina
from tests.test_airvault_comprobar import corrida

YO = ("77", "Luis Viloria")
OTRO = ("12", "Diego Vargas")


class ClienteConBloqueos(ClienteFalso):
    """AirVault con duenos: toma, suelta y dice en la cola quien tiene que."""

    def _poner(self, batch_id: str, quien: tuple[str, str]) -> None:
        self.lotes = [
            replace(fila, bloqueado_por_id=quien[0], bloqueado_por=quien[1])
            if fila.batch_id == batch_id else fila
            for fila in self.lotes
        ]

    def _fila(self, batch_id: str):
        return next(fila for fila in self.lotes if fila.batch_id == batch_id)

    def abrir_lote(self, batch_id: str):
        if self._fila(batch_id).bloqueado_por:
            # AirVault no contesta «ocupado»: deja la peticion colgada.
            raise ErrorDeConexion(f"sin respuesta al abrir {batch_id}")
        self._poner(batch_id, YO)
        return super().abrir_lote(batch_id)

    def cerrar_lote(self, batch_id: str):
        if self._fila(batch_id).bloqueado_por_id != YO[0]:
            raise ErrorDeAirVault("Batch no esta tomado por este usuario")
        self._poner(batch_id, ("", ""))
        return super().cerrar_lote(batch_id)


def dos_trabajos(tmp_path):
    """Dos batches de la misma instalacion, los dos ya en la cola."""
    csv = corrida(tmp_path)
    trabajos = []
    for carpeta, nombre, batch_id in (
        ("job-a", "DP | BIT A", "003AAA"), ("job-b", "DP | BIT B", "003BBB"),
    ):
        trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / carpeta, csv, nombre)
        trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "entrega.pdf")
        trabajo.manifiesto.batch_id = batch_id
        trabajo.manifiesto.etapa("descubrir").marcar(EstadoEtapa.HECHA, batch_id)
        trabajo.guardar()
        trabajos.append(trabajo)
    cliente = ClienteConBloqueos(
        paginas={n: pagina(n, estado=3) for n in range(1, 3)},
        lotes=[lote("003AAA", "DP | BIT A", 2), lote("003BBB", "DP | BIT B", 2)],
        picklist=["HP-1848CMP"], page_count=2,
    )
    return trabajos, cliente


def test_la_identidad_distingue_por_id_y_a_falta_de_el_por_nombre():
    yo = identidad.Identidad(*YO)
    assert yo.tiene(lote("X", "n", bloqueado_por=YO[1]))
    assert not yo.tiene(lote("X", "n"))
    mio = replace(lote("X", "n", bloqueado_por="Otro Nombre"), bloqueado_por_id=YO[0])
    tocayo = replace(lote("X", "n", bloqueado_por=YO[1]), bloqueado_por_id=OTRO[0])
    assert yo.tiene(mio)
    # Dos personas pueden llamarse igual: manda el identificador.
    assert not yo.tiene(tocayo)


def test_un_batch_abierto_a_nombre_propio_se_suelta_y_se_sigue(tmp_path):
    (a, b), cliente = dos_trabajos(tmp_path)
    cliente._poner("003AAA", YO)

    partes = comprobar_partes([a, b], cliente)

    assert cliente._fila("003AAA").bloqueado_por == ""
    assert partes[0].estado == LISTO
    assert partes[0].se_puede_indexar
    # El usuario se averiguo tomando un instante el batch libre, que queda
    # libre otra vez, y se guarda para la proxima.
    assert cliente._fila("003BBB").bloqueado_por == ""
    assert identidad.cargar(tmp_path) == identidad.Identidad(*YO)


def test_el_usuario_se_averigua_aunque_los_primeros_batches_ya_no_esten(tmp_path):
    """Los mas antiguos salen de la cola al completarse.

    Se probaban solo los tres primeros de la lista, sin mirar si seguian en
    la cola: con varias ejecuciones anteriores se agotaban los intentos, el
    usuario no se averiguaba nunca y los batches abiertos por la misma
    persona se quedaban sin soltar, marcados como de otro usuario.
    """
    (_a, b), cliente = dos_trabajos(tmp_path)
    candidatos = [
        ("003VIEJO1", "DP | YA COMPLETADO 1"),
        ("003VIEJO2", "DP | YA COMPLETADO 2"),
        ("003VIEJO3", "DP | YA COMPLETADO 3"),
        ("003VIEJO4", "DP | YA COMPLETADO 4"),
        (b.manifiesto.batch_id, b.manifiesto.nombre_batch),
    ]

    assert identidad.averiguar(cliente, candidatos) == identidad.Identidad(*YO)
    assert cliente._fila("003BBB").bloqueado_por == ""


def test_el_usuario_se_averigua_con_un_batch_que_no_viene_en_la_revision(tmp_path):
    """La revision puede traer solo lo pendiente, y todo eso estar abierto.

    Cualquier batch de la instalacion que siga libre en la cola sirve para
    saber quien es el usuario, aunque no venga en la tanda que se revisa.
    """
    (a, _b), cliente = dos_trabajos(tmp_path)
    cliente._poner("003AAA", YO)

    partes = comprobar_partes([a], cliente)

    assert cliente._fila("003AAA").bloqueado_por == ""
    assert partes[0].estado == LISTO
    assert identidad.cargar(tmp_path) == identidad.Identidad(*YO)


def test_un_batch_abierto_por_otro_usuario_no_se_toca(tmp_path):
    (a, b), cliente = dos_trabajos(tmp_path)
    cliente._poner("003AAA", OTRO)

    partes = comprobar_partes([a, b], cliente)

    assert cliente._fila("003AAA").bloqueado_por == OTRO[1]
    assert partes[0].estado == TOMADO
    assert not partes[0].se_puede_indexar
    assert "003AAA" not in cliente.cerrados
    assert partes[1].estado == LISTO


def test_sin_saber_quien_es_el_usuario_no_se_suelta_nada(tmp_path):
    """Sin batch libre con que averiguarlo ni nada guardado, se deja."""
    (a, b), cliente = dos_trabajos(tmp_path)
    cliente._poner("003AAA", YO)
    cliente._poner("003BBB", OTRO)

    partes = comprobar_partes([a, b], cliente)

    assert cliente._fila("003AAA").bloqueado_por == YO[1]
    assert [parte.estado for parte in partes] == [TOMADO, TOMADO]
    assert cliente.cerrados == []


def test_lo_guardado_vale_cuando_no_hay_batch_libre_para_averiguarlo(tmp_path):
    (a, b), cliente = dos_trabajos(tmp_path)
    identidad.guardar(tmp_path, identidad.Identidad(*YO))
    cliente._poner("003AAA", YO)
    cliente._poner("003BBB", OTRO)

    partes = comprobar_partes([a, b], cliente)

    assert cliente._fila("003AAA").bloqueado_por == ""
    assert cliente._fila("003BBB").bloqueado_por == OTRO[1]
    assert [parte.estado for parte in partes] == [LISTO, TOMADO]


def test_el_incompleto_abierto_a_nombre_propio_vuelve_a_poder_indexarse(tmp_path):
    (a, b), cliente = dos_trabajos(tmp_path)
    a.manifiesto.solo_subir = True
    a.manifiesto.etapa("verificar").marcar(EstadoEtapa.ERROR, "1/2 revisados")
    cliente._poner("003AAA", YO)

    partes = comprobar_partes([a, b], cliente)

    assert partes[0].estado == INCOMPLETO
    assert partes[0].se_puede_indexar
    assert not partes[0].lote.bloqueado_por


def test_al_detener_se_suelta_lo_propio_y_solo_lo_pendiente(tmp_path):
    (a, b), cliente = dos_trabajos(tmp_path)
    identidad.guardar(tmp_path, identidad.Identidad(*YO))
    # ``b`` ya esta indexado: si figura abierto por el mismo usuario es que
    # lo esta revisando a mano, y eso no se le quita.
    b.manifiesto.etapa("verificar").marcar(EstadoEtapa.HECHA, "2/2")
    cliente._poner("003AAA", YO)
    cliente._poner("003BBB", YO)

    assert soltar_propios([a, b], cliente) == ["003AAA"]

    assert cliente._fila("003AAA").bloqueado_por == ""
    assert cliente._fila("003BBB").bloqueado_por == YO[1]


def test_al_detener_no_se_suelta_lo_de_otro_usuario(tmp_path):
    (a, b), cliente = dos_trabajos(tmp_path)
    identidad.guardar(tmp_path, identidad.Identidad(*YO))
    cliente._poner("003AAA", OTRO)

    assert soltar_propios([a, b], cliente) == []
    assert cliente.cerrados == []


def test_al_detener_tambien_se_suelta_lo_que_el_programa_sabe_que_tomo(tmp_path):
    (a, b), cliente = dos_trabajos(tmp_path)
    a.tomar(cliente)

    soltar_propios([a, b], cliente)

    assert cliente._fila("003AAA").bloqueado_por == ""
    assert not a._tomado


def test_averiguar_el_usuario_suelta_el_batch_aunque_la_cola_falle(tmp_path):
    (_a, _b), cliente = dos_trabajos(tmp_path)
    listar = cliente.listar_lotes
    llamadas = []

    def listar_fallando(filtro=""):
        llamadas.append(filtro)
        if len(llamadas) == 2:
            raise RuntimeError("la cola no contesto")
        return listar(filtro)

    cliente.listar_lotes = listar_fallando

    assert identidad.averiguar(cliente, [("003BBB", "DP | BIT B")]) is None
    assert cliente._fila("003BBB").bloqueado_por == ""


@pytest.fixture(autouse=True)
def _identidad_sin_heredar(monkeypatch):
    """Lo averiguado en una prueba no pasa a la siguiente."""
    monkeypatch.setattr(identidad, "_DE_ESTA_SESION", {})


def test_la_limpieza_de_la_ventana_suelta_tambien_lo_que_muestra_la_cola(tmp_path):
    """Pausar, cancelar y cerrar pasan por aqui: no puede quedar nada tomado."""
    from app.gui.airvault_window import _soltar

    (a, b), cliente = dos_trabajos(tmp_path)
    identidad.guardar(tmp_path, identidad.Identidad(*YO))
    # AirVault lo tomo sin que el programa se enterara: no esta anotado.
    cliente._poner("003AAA", YO)
    assert not a._tomado

    _soltar([a, b], cliente)

    assert cliente._fila("003AAA").bloqueado_por == ""
