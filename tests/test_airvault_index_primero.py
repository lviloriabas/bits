"""Lo que ya está en Web Index se atiende antes de subir nada.

Una carga tarda en aparecer en AirVault más de media hora, y lo que ya está
en Web Index no depende de ella: indexar el batch listo, reintentar el
incompleto al que le toca y completar el indexado. Por eso van siempre
primero, también cuando lo que se sube es el reenvío de un batch que ni la
cola ni Web Search tienen. Con todo eso hecho se sigue con el resto.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.airvault.config import AirVaultConfig
from app.airvault.flujo import (
    BUSCANDO,
    INCOMPLETO,
    INDEXADO,
    LISTO,
    NO_ENCONTRADO,
    SIN_SUBIR,
    TOMADO,
    EstadoParte,
    Trabajo,
    subir_partes,
)
from app.airvault.model import EstadoEtapa
from app.gui import airvault_window as modulo
from app.gui.automatizacion import INDEXAR
from tests.airvault_fake import AirVaultSimulado, ClienteFalso, lote, subida_simulada
from tests.test_airvault_comprobar import (
    SesionFalsa,
    _trabajos_principal_division_y_revisar,
    corrida,
)
from tests.test_airvault_revision_inicio import WorkerFalso, crear_trabajo


# ── el recorrido ────────────────────────────────────────────────────

def test_un_reenvio_atiende_antes_lo_que_ya_esta_en_web_index(tmp_path, monkeypatch):
    """Volver a subir un archivo no deja esperando a lo que AirVault ya tiene."""
    csv = corrida(tmp_path)
    ausente = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    cliente = AirVaultSimulado()
    monkeypatch.setattr(Trabajo, "subir", subida_simulada(cliente, vueltas=0))
    en_index = SimpleNamespace(
        carpeta=tmp_path / "otro",
        manifiesto=SimpleNamespace(nombre_batch="DP | YA EN INDEX"),
    )
    avisos: list[str] = []

    def atender(trabajo, _todos):
        cliente.eventos.append(("atender", trabajo.manifiesto.nombre_batch))

    fallos = subir_partes(
        [ausente], SesionFalsa(), cliente=cliente,
        dormir=lambda _segundos: cliente.avanzar(),
        al_encontrar=atender, forzados=[str(ausente.carpeta)],
        ya_confirmados=[en_index],
        avisar=lambda texto, _hechas, _total: avisos.append(texto),
    )

    assert fallos == []
    assert cliente.eventos == [
        ("atender", "DP | YA EN INDEX"),
        ("subir", "DP | BIT 18 AUG 2026 05 42"),
        ("atender", "DP | BIT 18 AUG 2026 05 42"),
    ]
    assert any(
        texto.startswith("Primero se indexan los batches ya subidos")
        for texto in avisos
    )
    # Resubido, se espero a que AirVault lo cargara y quedo con su batch.
    assert ausente.manifiesto.batch_id


def test_el_hilo_que_reenvia_atiende_primero_lo_que_la_ventana_vio_en_index(
    tmp_path, monkeypatch
):
    """El reenvío automático llega con la lista de lo que hay que hacer antes."""
    from app.gui.airvault_window import TrabajoAirVaultWorker

    indexado, ausente, incompleto = _trabajos_principal_division_y_revisar(tmp_path)
    recibido: dict = {}

    def subir_falso(lotes, _sesion, ya_confirmados=(), forzados=(), **_kwargs):
        recibido["por_subir"] = list(lotes)
        recibido["antes"] = list(ya_confirmados)
        recibido["forzados"] = list(forzados)
        return []

    monkeypatch.setattr("app.airvault.flujo.subir_partes", subir_falso)
    estado = {
        "config": AirVaultConfig(), "raiz": tmp_path, "sesion": SesionFalsa(),
        "indexar_al_encontrar": True, "completar": True,
        # El hilo del reenvío solo recibe lo que vuelve a subir.
        "trabajos": [ausente], "forzados": [str(ausente.carpeta)],
        "en_index_primero": [incompleto, indexado, ausente],
    }
    worker = TrabajoAirVaultWorker("recuperar_ausentes", estado)

    worker._enviar([ausente], ClienteFalso())

    assert recibido["por_subir"] == [ausente]
    assert recibido["forzados"] == [str(ausente.carpeta)]
    # Lo que se va a subir no se atiende «antes» de sí mismo.
    assert recibido["antes"] == [incompleto, indexado]
    # La lista vale para una subida: no se arrastra a la acción siguiente.
    assert "en_index_primero" not in estado

    # Con el indexado automático apagado no se toca nada por delante.
    estado.update(indexar_al_encontrar=False,
                  en_index_primero=[incompleto, indexado])
    worker._enviar([ausente], ClienteFalso())
    assert recibido["antes"] == []


def test_lo_omitido_no_se_atiende_por_delante(tmp_path, monkeypatch):
    from app.gui.airvault_window import TrabajoAirVaultWorker, _clave_estable

    indexado, ausente, incompleto = _trabajos_principal_division_y_revisar(tmp_path)
    recibido: dict = {}
    monkeypatch.setattr(
        "app.airvault.flujo.subir_partes",
        lambda lotes, _sesion, ya_confirmados=(), **_k: recibido.update(
            antes=list(ya_confirmados)
        ) or [],
    )
    estado = {
        "config": AirVaultConfig(), "raiz": tmp_path, "sesion": SesionFalsa(),
        "indexar_al_encontrar": True, "completar": False,
        "trabajos": [ausente], "forzados": [str(ausente.carpeta)],
        "en_index_primero": [incompleto, indexado],
        "omitidos": {_clave_estable(incompleto.carpeta)},
    }

    TrabajoAirVaultWorker("recuperar_ausentes", estado)._enviar(
        [ausente], ClienteFalso()
    )

    assert recibido["antes"] == [indexado]


# ── lo que la ventana manda atender ─────────────────────────────────

@pytest.fixture
def ventana(app, tmp_path, monkeypatch):
    monkeypatch.setattr(modulo, "TrabajoAirVaultWorker", WorkerFalso)
    ventana = modulo.AirVaultWindow(tmp_path)
    ventana.solo_ejecucion_check.setChecked(False)
    yield ventana
    ventana.close()


def cola(ventana, tmp_path, filas):
    """Pone en la cola un batch por fila: nombre, estado, ID y quien lo tiene."""
    partes = []
    for nombre, estado, batch_id, abierto_por in filas:
        trabajo = crear_trabajo(
            tmp_path, nombre, EstadoEtapa.HECHA if batch_id else None, batch_id
        )
        remoto = (
            lote(batch_id, nombre, 1, bloqueado_por=abierto_por)
            if batch_id else None
        )
        partes.append(EstadoParte(trabajo, estado, "", remoto))
    ventana._trabajos = [parte.trabajo for parte in partes]
    ventana._estados = partes
    ventana._estado["trabajos"] = list(ventana._trabajos)
    ventana._estado["sesion"] = SimpleNamespace(cancelada=False)
    ventana._pintar_lotes()
    return [parte.trabajo for parte in partes]


def nombres(trabajos) -> list[str]:
    return [trabajo.manifiesto.nombre_batch for trabajo in trabajos]


def test_la_ventana_elige_lo_que_esta_en_index_y_tiene_algo_por_hacer(ventana, tmp_path):
    cola(ventana, tmp_path, [
        ("listo", LISTO, "003A", ""),
        ("subido", BUSCANDO, "003B", ""),
        ("incompleto", INCOMPLETO, "003C", ""),
        ("indexado", INDEXADO, "003D", ""),
        ("abierto", TOMADO, "003E", "jperez@dominio.com"),
        ("por-subir", SIN_SUBIR, None, ""),
        ("ausente", NO_ENCONTRADO, None, ""),
    ])

    ventana.completar_check.setChecked(False)
    assert nombres(ventana._por_atender_en_index()) == ["listo", "subido", "incompleto"]

    # Con «Completar batch», el indexado también tiene algo pendiente.
    ventana.completar_check.setChecked(True)
    assert nombres(ventana._por_atender_en_index()) == [
        "listo", "subido", "incompleto", "indexado",
    ]

    # Sin indexado automático el programa no toca nada por su cuenta.
    ventana._opciones.fijar(INDEXAR, False)
    assert ventana._por_atender_en_index() == []


def test_un_incompleto_que_gasto_sus_intentos_no_se_reescribe_antes_de_cada_subida(
    ventana, tmp_path
):
    """Tiene su propio ritmo de reintentos; una subida no se lo salta."""
    (incompleto,) = cola(ventana, tmp_path, [("incompleto", INCOMPLETO, "003C", "")])
    clave = str(incompleto.carpeta)

    ventana._reconfirmaciones[clave] = 0
    assert ventana._por_atender_en_index() == []

    ventana._reconfirmaciones[clave] = 2
    assert ventana._por_atender_en_index() == [incompleto]


def test_un_cierre_que_airvault_ya_rechazo_tres_veces_no_se_insiste(ventana, tmp_path):
    (indexado,) = cola(ventana, tmp_path, [("indexado", INDEXADO, "003D", "")])
    ventana.completar_check.setChecked(True)
    assert ventana._por_atender_en_index() == [indexado]

    ventana._cierres_fallidos[str(indexado.carpeta)] = (3, 0.0)
    assert ventana._por_atender_en_index() == []


@pytest.mark.parametrize("modo", ["subir_pendientes", "recuperar_ausentes"])
def test_todo_lo_que_sube_solo_lleva_por_delante_lo_que_esta_en_index(
    ventana, tmp_path, modo
):
    listo, _por_subir = cola(ventana, tmp_path, [
        ("listo", LISTO, "003A", ""),
        ("por-subir", SIN_SUBIR, None, ""),
    ])

    ventana._lanzar(modo, ventana._estado)

    assert ventana._worker.estado["en_index_primero"] == [listo]
    # Lo que quede incompleto ahí lo retoma la revisión siguiente, no la
    # misma cadena en cuanto termina de subir.
    assert ventana._atendido_hace_poco(str(listo.carpeta))


def test_la_orden_a_mano_sobre_una_fila_no_arrastra_a_las_demas(ventana, tmp_path):
    cola(ventana, tmp_path, [
        ("listo", LISTO, "003A", ""),
        ("por-subir", SIN_SUBIR, None, ""),
    ])
    ventana._estado["en_index_primero"] = ["de una acción anterior"]

    ventana._lanzar("resubir", ventana._estado)

    assert "en_index_primero" not in ventana._worker.estado
