"""La cola se confirma de una vez y cada batch queda con su destino.

Antes la subida esperaba a cada carga en vuelo, una detras de otra, sin
decir nada de las demas, y Web Search solo se consultaba al terminar toda
la cadena. Ahora lo primero es saber donde esta cada batch: cuales se
suben, cuales esperan a AirVault, cuales estan en Web Index o en Web Search
y cuales faltan. Lo unico que una carga en vuelo impide es subir otra.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.airvault.config import AirVaultConfig
from app.airvault.flujo import (A_MANO, A_RESUBIR, A_SUBIR, BUSCANDO, CANCELADO,
                                COMPLETADO, DESCUADRADO, EN_ESPERA,
                                EN_WEB_INDEX, EN_WEB_SEARCH, INCOMPLETO,
                                INDEXADO, LISTO, NO_ENCONTRADO,
                                POSIBLE_DUPLICADO, PROCESANDO, PUBLICADO,
                                SIN_SUBIR, SIN_UBICAR, SOLO_REVISAR, TOMADO,
                                EstadoParte, Trabajo, anotar_reenvio,
                                clasificar_partes, destino_de,
                                partes_por_ubicar, resumen_de_clasificacion,
                                subir_partes)
from app.airvault.model import EstadoEtapa
from app.gui import airvault_window as modulo
from tests.airvault_fake import AirVaultSimulado, subida_simulada
from tests.test_airvault_comprobar import (SesionFalsa, _dos_partes, corrida,
                                           trabajo_subido)
from tests.test_airvault_reenvio_automatico import (lineas, perdida,
                                                     sin_lectura_real)


def ausente_ahora(trabajo) -> None:
    """Web Search acaba de contestar que sus bitacoras no estan publicadas."""
    trabajo.manifiesto.websearch_resultado = "ausente"
    trabajo.manifiesto.websearch_revision = datetime.now().isoformat(
        timespec="microseconds"
    )


# ── el destino de cada batch ────────────────────────────────────────

@pytest.mark.parametrize("estado, destino", [
    (SIN_SUBIR, A_SUBIR),
    (BUSCANDO, EN_ESPERA),
    (PROCESANDO, EN_ESPERA),
    (LISTO, EN_WEB_INDEX),
    (SOLO_REVISAR, EN_WEB_INDEX),
    (INCOMPLETO, EN_WEB_INDEX),
    (INDEXADO, EN_WEB_INDEX),
    (TOMADO, EN_WEB_INDEX),
    (COMPLETADO, EN_WEB_SEARCH),
    (PUBLICADO, EN_WEB_SEARCH),
    (DESCUADRADO, A_MANO),
    (POSIBLE_DUPLICADO, A_MANO),
    (CANCELADO, ""),
])
def test_cada_estado_tiene_su_destino(tmp_path, estado, destino):
    trabajo, _cliente = trabajo_subido(tmp_path)

    assert destino_de(EstadoParte(trabajo, estado)) == destino


def test_lo_que_la_cola_no_tiene_se_busca_en_web_search_antes_de_decidir(tmp_path):
    trabajo, _cliente, parte = perdida(tmp_path)

    assert destino_de(parte) == SIN_UBICAR
    assert partes_por_ubicar([parte]) == [trabajo]

    # Web Search contesta que tampoco lo tiene: falta, y vuelve a subirse.
    ausente_ahora(trabajo)
    assert destino_de(parte) == A_RESUBIR

    # El que ya se resubio, aparecio y volvio a faltar lo decide una persona.
    trabajo.manifiesto.batch_id = "003YA"
    anotar_reenvio(trabajo, automatico=True)
    assert destino_de(parte) == A_MANO


def test_la_clasificacion_agrupa_y_se_resume_en_una_linea(tmp_path):
    trabajo, _cliente = trabajo_subido(tmp_path)
    estados = [
        EstadoParte(trabajo, SIN_SUBIR),
        EstadoParte(trabajo, LISTO),
        EstadoParte(trabajo, INDEXADO),
        EstadoParte(trabajo, COMPLETADO),
        EstadoParte(trabajo, PROCESANDO),
        EstadoParte(trabajo, CANCELADO),
    ]

    grupos = clasificar_partes(estados)

    assert list(grupos) == [A_SUBIR, EN_ESPERA, EN_WEB_INDEX, EN_WEB_SEARCH]
    assert [len(partes) for partes in grupos.values()] == [1, 1, 2, 1]
    assert resumen_de_clasificacion(estados) == (
        "Confirmación de 5 batches: 1 por subir, 1 en espera de AirVault, "
        "2 en Web Index, 1 en Web Search o completados"
    )


# ── la subida ───────────────────────────────────────────────────────

def _tres_trabajos(tmp_path):
    """Tres batches de nombres distintos, cada uno con sus bitacoras."""
    csv = corrida(tmp_path)
    trabajos = []
    for indice, nombre in enumerate(("DP | UNO", "DP | DOS", "DP | TRES"), start=1):
        trabajo = Trabajo.preparar(
            AirVaultConfig(), tmp_path / f"job-{indice}", csv, nombre
        )
        pdf = csv.parent.parent / f"parte-{indice}.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + str(indice).encode())
        trabajo.manifiesto.pdf_origen = str(pdf)
        for registro in trabajo.manifiesto.registros:
            if not registro.es_separador:
                registro.archivo_origen = f"Image_{indice:03d}.pdf"
        trabajo.guardar()
        trabajos.append(trabajo)
    return trabajos


def test_la_clasificacion_sale_antes_de_esperar_a_ninguna_carga(tmp_path, monkeypatch):
    """La cola se pinta de una vez; la espera viene despues."""
    primera, segunda = _dos_partes(tmp_path)
    cliente = AirVaultSimulado()
    monkeypatch.setattr(Trabajo, "subir", subida_simulada(cliente))
    primera.subir(SesionFalsa(), cliente=cliente)
    pasos: list = []

    def dormir(segundos):
        pasos.append("esperar")
        cliente.avanzar()

    def clasificar(estados):
        pasos.append({
            parte.nombre: destino_de(parte) for parte in estados
        })

    subir_partes(
        [primera, segunda], SesionFalsa(), cliente=cliente, dormir=dormir,
        al_clasificar=clasificar,
    )

    assert pasos[0] == {"DP | BIT -1": EN_ESPERA, "DP | BIT -2": A_SUBIR}
    assert "esperar" in pasos[1:]


def test_lo_que_solo_viene_a_confirmarse_no_se_sube(tmp_path, monkeypatch):
    """Retomar la cola confirma todos los batches, no solo los que suben."""
    primera, segunda = _dos_partes(tmp_path)
    cliente = AirVaultSimulado()
    monkeypatch.setattr(Trabajo, "subir", subida_simulada(cliente, vueltas=0))
    primera.subir(SesionFalsa(), cliente=cliente)
    clasificados: list = []
    indexados: list = []

    fallos = subir_partes(
        [primera], SesionFalsa(), cliente=cliente, dormir=cliente.avanzar,
        en_la_ejecucion=[primera, segunda],
        tambien_confirmar=[segunda],
        al_clasificar=lambda estados: clasificados.append({
            parte.nombre: destino_de(parte) for parte in estados
        }),
        al_encontrar=lambda trabajo, _todos: indexados.append(
            trabajo.manifiesto.nombre_batch
        ),
    )

    assert fallos == []
    assert clasificados == [{
        "DP | BIT -1": EN_WEB_INDEX, "DP | BIT -2": A_SUBIR,
    }]
    # La segunda quedo clasificada, pero nadie pidio subirla en esta tanda.
    assert cliente.eventos == [("subir", "DP | BIT -1")]
    assert not segunda.manifiesto.etapa_hecha("subir")
    assert indexados == ["DP | BIT -1"]


def test_las_cargas_en_vuelo_se_miran_todas_en_cada_vuelta(tmp_path, monkeypatch):
    """La que ya esta en la cola no espera detras de otra que no aparece.

    Antes se esperaba a la primera hasta agotar su plazo (cinco minutos
    como poco) y solo entonces se miraba la segunda, que llevaba todo ese
    rato armada en AirVault y sin indexar.
    """
    ausente, armandose, sin_subir = _tres_trabajos(tmp_path)
    cliente = AirVaultSimulado()
    monkeypatch.setattr(Trabajo, "subir", subida_simulada(cliente, vueltas=3))
    # Quick Upload acepto la primera y la cola nunca la mostro.
    ausente.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "a.pdf")
    ausente.guardar()
    armandose.subir(SesionFalsa(), cliente=cliente)
    esperas: list[float] = []
    vueltas_al_indexar: dict[str, int] = {}

    def dormir(segundos):
        esperas.append(segundos)
        cliente.avanzar()

    subir_partes(
        [ausente, armandose, sin_subir], SesionFalsa(), cliente=cliente,
        dormir=dormir,
        al_encontrar=lambda trabajo, _todos: vueltas_al_indexar.setdefault(
            trabajo.manifiesto.nombre_batch, len(esperas)
        ),
    )

    assert armandose.manifiesto.batch_id == "003B1"
    # Se confirmo e indexo en cuanto AirVault termino de armarla.
    assert vueltas_al_indexar["DP | DOS"] <= 4
    # La otra conservo su plazo entero.
    assert sum(esperas) >= 290
    assert ausente.manifiesto.batch_id is None


# ── la ventana ──────────────────────────────────────────────────────

@pytest.fixture
def ventana(app, tmp_path):
    return modulo.AirVaultWindow(tmp_path)


def test_al_clasificar_se_pinta_la_cola_y_se_busca_en_web_search_lo_que_falta(ventana, tmp_path):
    trabajo, _cliente, parte = perdida(tmp_path)
    ventana._trabajos = [trabajo]
    ventana._cadena_manual = True
    sin_lectura_real(ventana)

    ventana._al_clasificar({"estados": [parte], "trabajos": [trabajo]})

    assert [p.estado for p in ventana._estados] == [NO_ENCONTRADO]
    # La consulta sale ya, con la subida todavia en marcha.
    assert ventana._consultas == [([trabajo], {"automatico": True})]
    assert str(trabajo.carpeta) in ventana._reenvios_automaticos
    registro = lineas(ventana)
    assert "Confirmación de 1 batch: 1 fuera de Web Index, por buscar en Web Search." in registro
    assert "No están en Web Index, se buscan en Web Search: batch 1" in registro
    assert f"Batch 1 «{trabajo.manifiesto.nombre_batch}»: " in registro


def test_la_bitacora_nombra_cada_batch_con_su_numero(ventana, tmp_path):
    trabajo, _cliente = trabajo_subido(tmp_path)
    nombre = trabajo.manifiesto.nombre_batch
    ventana._trabajos = [trabajo]
    ventana._estados = [EstadoParte(trabajo, LISTO)]

    ventana._anotar(f"«{nombre}»: no se pudo subir")
    ventana._anotar(f"Batch «{nombre}» indexado")
    ventana._anotar(f"No se pudo completar «{nombre}»: falta una fecha")
    ventana._anotar("Detalles:", [f"Batch 1 «{nombre}»: listo", "«OTRO»: sin numero"])

    registro = lineas(ventana)
    assert f"Batch 1 «{nombre}»: no se pudo subir" in registro
    assert f"Batch 1 «{nombre}» indexado" in registro
    assert f"No se pudo completar batch 1 «{nombre}»: falta una fecha" in registro
    # Lo que ya lleva su numero no lo repite, y un nombre ajeno queda igual.
    assert f"Batch 1 Batch 1 «{nombre}»" not in registro
    assert "«OTRO»: sin numero" in registro


def test_un_reenvio_que_ya_espera_turno_no_se_lanza_otra_vez(ventana, tmp_path, monkeypatch):
    trabajo, _cliente, parte = perdida(tmp_path)
    ausente_ahora(trabajo)
    ventana._trabajos = [trabajo]
    ventana._estados = [parte]
    ventana._perdidos_por_recuperar = [trabajo]
    ventana._cola_de_acciones.append(("recuperar_ausentes", [trabajo]))
    monkeypatch.setattr(
        ventana, "_lanzar", lambda *_a, **_k: pytest.fail("Ya estaba en cola"),
    )

    assert ventana._recuperar_perdidos() is False


def test_el_ausente_que_ya_se_resubio_no_vuelve_a_subirse_solo(ventana, tmp_path, monkeypatch):
    """Lo busca el programa, pero si falta otra vez lo decide una persona."""
    trabajo, _cliente, parte = perdida(tmp_path)
    trabajo.manifiesto.batch_id = "003YA"
    anotar_reenvio(trabajo, automatico=True)
    ausente_ahora(trabajo)
    clave = str(trabajo.carpeta)
    ventana._trabajos = [trabajo]
    ventana._estados = [parte]
    ventana._ubicando.add(clave)
    ventana._websearch_resubidas[clave] = trabajo
    ventana._worker_websearch = SimpleNamespace(
        estado={"buscar_trabajos": [trabajo], "confirmacion_automatica": True},
        deleteLater=lambda: None,
    )
    encolados = []
    monkeypatch.setattr(
        ventana, "_encolar", lambda modo, trabajos, _texto: encolados.append(modo),
    )

    ventana._al_terminar_websearch()

    assert encolados == []
    assert "queda para decidir a mano" in lineas(ventana)
    # Tampoco al terminar la cadena.
    ventana._perdidos_por_recuperar = [trabajo]
    monkeypatch.setattr(
        ventana, "_lanzar", lambda *_a, **_k: pytest.fail("No se resube solo"),
    )
    assert ventana._recuperar_perdidos() is False
