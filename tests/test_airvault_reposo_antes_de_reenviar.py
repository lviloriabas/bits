"""Una carga que AirVault acepto no se vuelve a mandar mientras pueda seguir en camino.

AirVault tarda entre unos minutos y mas de una hora en publicar lo que
recibe, sin que importe el tamano, y hasta entonces no la muestra en ninguna
parte. El plazo de la espera entre cargas era de cinco minutos para 78
paginas: el programa daba la carga por perdida, Web Search tampoco la tenia
y la volvia a subir. AirVault terminaba publicando las dos, y la que no se
renombraba se quedaba en Web Index con «-Batch» al final.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.airvault.flujo import (BUSCANDO, EN_ESPERA, NO_ENCONTRADO,
                                REPOSO_ANTES_DE_REENVIAR_S, A_RESUBIR,
                                CopiaEnLaCola, EstadoParte, Trabajo,
                                a_la_hora, anotar_reenvio, comprobar_partes,
                                destino_de, detalle_de_no_encontrada,
                                envio_en_camino, estado_local,
                                motivo_envio_en_camino,
                                motivo_para_no_reenviar, partes_por_recuperar,
                                partes_por_ubicar, se_reenvia_sola,
                                subir_partes, ya_esta_en_airvault)
from app.airvault.copias_en_index import armandose_en_la_cola
from app.airvault.model import Etapa, Registro
from app.gui import airvault_window as modulo
from app.utils.mensajes import mensaje_error
from tests.airvault_fake import ClienteFalso, lote
from tests.test_airvault_copia_de_pendiente import pendiente
from tests.test_airvault_copias_en_index import NOMBRE, PAGINAS, contenido, trabajo
from tests.test_airvault_reenvio_automatico import perdida


def enviada(trabajo, hace_s: float = 0.0, resultado: str = "aceptada"):
    """Anota un envio en el que salio FinishUpload, hace ``hace_s`` segundos."""
    trabajo.manifiesto.anotar("subida", resultado, "entrega.pdf")
    trabajo.manifiesto.historial[-1].momento = (
        datetime.now() - timedelta(seconds=hace_s)
    ).isoformat(timespec="seconds")
    trabajo.guardar()
    return trabajo


def ausente_en_web_search(trabajo):
    trabajo.manifiesto.websearch_resultado = "ausente"
    trabajo.manifiesto.websearch_revision = datetime.now().isoformat(timespec="microseconds")
    trabajo.guardar()


# ── cuando una carga sigue en camino ─────────────────────────────────

def test_lo_que_nunca_salio_no_esta_en_camino(tmp_path):
    t = pendiente(tmp_path, subido=False)

    assert envio_en_camino(t) is None


@pytest.mark.parametrize("resultado", ["aceptada", "sin_respuesta", "interrumpida"])
def test_lo_que_llego_a_finishupload_esta_en_camino_tres_horas(tmp_path, resultado):
    t = enviada(pendiente(tmp_path), resultado=resultado)

    limite = envio_en_camino(t)

    assert limite is not None
    espera = (limite - datetime.now()).total_seconds()
    assert REPOSO_ANTES_DE_REENVIAR_S - 60 < espera <= REPOSO_ANTES_DE_REENVIAR_S
    assert REPOSO_ANTES_DE_REENVIAR_S == 3 * 3600


@pytest.mark.parametrize("resultado", ["no_enviada", "cancelada", "rechazada"])
def test_lo_que_no_llego_a_finishupload_no_esta_en_camino(tmp_path, resultado):
    """Sin FinishUpload, AirVault no tiene nada que publicar."""
    t = enviada(pendiente(tmp_path), resultado=resultado)

    assert envio_en_camino(t) is None


def test_pasado_el_reposo_ya_no_esta_en_camino(tmp_path):
    t = enviada(pendiente(tmp_path), hace_s=REPOSO_ANTES_DE_REENVIAR_S + 60)

    assert envio_en_camino(t) is None


def test_la_que_aparecio_en_web_index_ya_llego(tmp_path):
    t = enviada(pendiente(tmp_path))
    t.manifiesto.anotar("deteccion", "encontrado", "003TIH (10 paginas)")

    assert envio_en_camino(t) is None


def test_cuenta_el_ultimo_envio_no_el_primero(tmp_path):
    """El hallazgo era de la carga anterior; la reenviada sigue en camino."""
    t = enviada(pendiente(tmp_path), hace_s=REPOSO_ANTES_DE_REENVIAR_S * 2)
    t.manifiesto.anotar("deteccion", "encontrado", "003TIH (10 paginas)")
    anotar_reenvio(t, automatico=False, motivo="orden expresa")
    enviada(t, hace_s=600)

    assert envio_en_camino(t) is not None


def test_la_apartada_por_mezclada_se_resube_sin_esperar(tmp_path):
    """Esa carga si llego, dentro del batch que AirVault junto, y se aparto."""
    t = enviada(pendiente(tmp_path))
    t.manifiesto.resubir_por_mezcla = True

    assert envio_en_camino(t) is None


# ── lo que ve el usuario ─────────────────────────────────────────────

def test_mientras_puede_llegar_se_ve_subida_y_hasta_que_hora_se_espera(tmp_path):
    """No es una carga perdida: no va en rojo ni dice «No encontrado»."""
    trabajo_perdido, cliente, _parte = perdida(tmp_path)
    enviada(trabajo_perdido, hace_s=600)

    parte, = comprobar_partes([trabajo_perdido], cliente)

    assert parte.estado == BUSCANDO and parte.titulo == "Subido"
    assert "AirVault todavía no la publica" in parte.detalle
    assert "No se vuelve a subir hasta las" in parte.detalle
    assert "se vuelve a subir sola" in parte.detalle
    assert modulo.papel_de_estado(parte.estado) != "STATUS_ERROR"
    # Sigue sin frenar a las cargas siguientes.
    assert trabajo_perdido.manifiesto.no_encontrado_desde
    assert estado_local(trabajo_perdido).estado == BUSCANDO
    # Tampoco cuando Web Search ya contesto que no la tiene.
    ausente_en_web_search(trabajo_perdido)
    assert estado_local(trabajo_perdido).estado == BUSCANDO
    assert partes_por_ubicar([parte]) == []


def test_pasado_el_reposo_se_ve_no_encontrada_y_se_busca_para_resubir(tmp_path):
    """El reposo no deja la carga olvidada: al terminar sigue su camino."""
    trabajo_perdido, cliente, _parte = perdida(tmp_path)
    enviada(trabajo_perdido, hace_s=REPOSO_ANTES_DE_REENVIAR_S + 60)

    parte, = comprobar_partes([trabajo_perdido], cliente)

    assert parte.estado == NO_ENCONTRADO
    assert partes_por_ubicar([parte]) == [trabajo_perdido]
    assert se_reenvia_sola(parte)


def test_la_que_ya_no_se_resube_sola_tambien_dice_hasta_cuando_se_espera(tmp_path):
    trabajo_perdido, cliente, _parte = perdida(tmp_path)
    anotar_reenvio(trabajo_perdido, automatico=True)
    anotar_reenvio(trabajo_perdido, automatico=True)
    enviada(trabajo_perdido, hace_s=600)

    parte, = comprobar_partes([trabajo_perdido], cliente)

    assert parte.estado == BUSCANDO
    assert "después queda para decidir a mano" in parte.detalle


def test_el_plazo_que_pasa_de_medianoche_lleva_la_fecha():
    manana = datetime.now().replace(hour=1, minute=5) + timedelta(days=1)

    assert a_la_hora(manana) == f"el {manana:%d/%m} a las 01:05"
    assert "hasta el " in motivo_envio_en_camino(manana)


# ── el reenvio automatico ────────────────────────────────────────────

def test_la_no_encontrada_espera_el_reposo_antes_de_volver_sola(tmp_path):
    trabajo_perdido, _cliente, parte = perdida(tmp_path)
    enviada(trabajo_perdido, hace_s=600)
    ausente_en_web_search(trabajo_perdido)

    assert not se_reenvia_sola(parte)
    assert partes_por_recuperar([parte]) == []
    # No es para decidir a mano: AirVault todavia puede publicarla.
    assert destino_de(parte) == EN_ESPERA
    motivo = motivo_para_no_reenviar(trabajo_perdido)
    assert "puede seguir llegando a AirVault" in motivo
    assert "no se vuelve a subir hasta las" in motivo
    detalle = detalle_de_no_encontrada(trabajo_perdido)
    assert "se vuelve a subir sola" in detalle and "hasta las" in detalle


def test_pasado_el_reposo_vuelve_sola_como_antes(tmp_path):
    trabajo_perdido, _cliente, parte = perdida(tmp_path)
    enviada(trabajo_perdido, hace_s=REPOSO_ANTES_DE_REENVIAR_S + 60)
    ausente_en_web_search(trabajo_perdido)

    assert se_reenvia_sola(parte)
    assert partes_por_recuperar([parte]) == [trabajo_perdido]
    assert destino_de(parte) == A_RESUBIR
    assert motivo_para_no_reenviar(trabajo_perdido) == ""


def test_el_motivo_de_siempre_manda_sobre_el_reposo(tmp_path):
    """La que ya tuvo ID no vuelve sola nunca; el reposo no cambia eso."""
    trabajo_perdido, _cliente, parte = perdida(tmp_path)
    enviada(trabajo_perdido, hace_s=600)
    trabajo_perdido.manifiesto.batch_id = "003YA"

    assert not se_reenvia_sola(parte)
    assert "003YA" in motivo_para_no_reenviar(trabajo_perdido)
    assert "003YA" in detalle_de_no_encontrada(trabajo_perdido)


def test_la_ventana_no_autoriza_el_reenvio_de_una_carga_en_camino(app, tmp_path):
    trabajo_perdido, _cliente, _parte = perdida(tmp_path)
    enviada(trabajo_perdido, hace_s=600)
    ausente_en_web_search(trabajo_perdido)
    clave = str(trabajo_perdido.carpeta)

    for estado in ({"reenvios_automaticos": {clave}}, {}):
        worker = modulo.TrabajoAirVaultWorker("recuperar_ausentes", estado)
        autorizados, fallos = worker._revisar_reenvios([trabajo_perdido], ClienteFalso())

        assert autorizados == []
        (nombre, motivo), = fallos
        assert nombre == trabajo_perdido.manifiesto.nombre_batch
        assert "puede seguir llegando a AirVault" in motivo
    assert not trabajo_perdido.manifiesto.eventos("reenvio")
    assert not trabajo_perdido._duplicado_permitido


# ── la subida ────────────────────────────────────────────────────────

def otra_carga(raiz):
    """Una carga de la misma ejecucion que nunca se subio."""
    t = trabajo(raiz, carpeta="parte-03", nombre="DP | BITS 05 OCT 2026 12 17 -3",
                batch_id=None, verificado=False)
    t.manifiesto.etapas["subir"] = Etapa()
    t.manifiesto.registros = [
        Registro(seq=i + 1, log_number=str(2400000 + i)) for i in range(PAGINAS)
    ]
    t.guardar()
    return t


def test_la_orden_de_subir_no_manda_otra_vez_la_carga_en_camino(tmp_path, monkeypatch):
    """Ni por orden: nadie ve la carga en AirVault hasta que se publica.

    Las demas cargas siguen subiendo, y lo que reconoce a la que llega (la
    foto de la cola, el plazo vencido) no se borra.
    """
    en_camino = enviada(pendiente(tmp_path), hace_s=600)
    en_camino.manifiesto.no_encontrado_desde = datetime.now().isoformat(timespec="seconds")
    en_camino.manifiesto.lotes_previos = ["003VIEJO"]
    en_camino.guardar()
    nueva = otra_carga(tmp_path)
    subidas = []

    def subir(self, *_a, **_k):
        subidas.append(self.manifiesto.nombre_batch)
        raise RuntimeError("se corta aqui para no seguir con la espera")

    monkeypatch.setattr(Trabajo, "subir", subir)
    avisos = []

    fallos = subir_partes(
        [en_camino, nueva], SimpleNamespace(cancelada=False),
        cliente=ClienteFalso(lotes=[]), forzados=[str(en_camino.carpeta)],
        avisar=lambda texto, *_a: avisos.append(texto), dormir=lambda _s: None,
    )

    assert subidas == [nueva.manifiesto.nombre_batch]
    motivo = dict((t.manifiesto.nombre_batch, m) for t, m in fallos)[NOMBRE]
    assert "puede seguir llegando a AirVault" in motivo
    assert any("No se sube otra vez" in aviso for aviso in avisos)
    assert en_camino.manifiesto.lotes_previos == ["003VIEJO"]
    assert en_camino.manifiesto.no_encontrado_desde
    assert en_camino.manifiesto.etapa_hecha("subir")


def test_la_ventana_explica_el_motivo_sin_mandar_a_reintentar():
    detalle = (
        "No se subió otra vez porque la carga enviada puede seguir llegando a "
        "AirVault, que a veces tarda más de una hora en publicarla; no se "
        "vuelve a subir hasta las 18:05 para no dejarla dos veces."
    )

    texto = mensaje_error(detalle, "No se pudo subir. Vuelva a intentarlo.")

    assert "Vuelva a intentarlo" not in texto
    assert "todavía puede llegar a AirVault" in texto


# ── la carga que AirVault todavia arma ───────────────────────────────

def test_el_titulo_con_batch_y_menos_paginas_es_la_carga_que_se_arma(tmp_path):
    t = pendiente(tmp_path, subido=False)
    cola = [
        lote("003TJF", f"{NOMBRE}-Batch", PAGINAS - 6),
        # Lo demas no es esta carga a medio armar.
        lote("003TJG", NOMBRE, PAGINAS - 6),
        lote("003TJH", "DP | BITS 05 OCT 2026 12 17 -3-Batch", 2),
        lote("003TJI", f"{NOMBRE}-Batch", PAGINAS - 6, repo_id=9999),
    ]

    assert [c.batch_id for c in armandose_en_la_cola(t.manifiesto, cola)] == ["003TJF"]

    t.manifiesto.batches_descartados = ["003TJF"]
    assert armandose_en_la_cola(t.manifiesto, cola) == []


def test_no_se_sube_mientras_airvault_arma_la_misma_carga(tmp_path):
    t = pendiente(tmp_path, subido=False)
    cola = [lote("003TJF", f"{NOMBRE}-Batch", 3)]
    cliente = ClienteFalso(lotes=cola, paginas_por_lote={"003TJF": contenido(cantidad=3)})

    with pytest.raises(CopiaEnLaCola) as error:
        ya_esta_en_airvault(t, cliente)

    texto = str(error.value)
    assert "003TJF" in texto and f"3 de {PAGINAS} páginas" in texto
    assert "todavía la está armando" in texto
    # Mirarla no la toma ni la renombra.
    assert not cliente.abiertos and not cliente.renombrados


def test_la_subida_automatica_espera_a_la_carga_que_se_arma(tmp_path, monkeypatch):
    t = pendiente(tmp_path, subido=False)
    cliente = ClienteFalso(lotes=[lote("003TJF", f"{NOMBRE}-Batch", 3)])
    monkeypatch.setattr(
        Trabajo, "subir", lambda *_a, **_k: pytest.fail("AirVault ya la estaba armando"),
    )

    fallos = subir_partes(
        [t], SimpleNamespace(cancelada=False), cliente=cliente, dormir=lambda _s: None,
    )

    (fallido, motivo), = fallos
    assert fallido is t and "todavía la está armando" in motivo
