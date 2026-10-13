"""Un batch que esta en la cola de Web Index nunca se da por perdido.

Con el ID, el nombre y las paginas correctos delante, el programa llego a
pintar el batch como «No encontrado» y a ponerlo en turno para volver a
subirlo. Eran varias causas distintas con el mismo final, y aqui se fija
cada una:

- Web Search solo tiene lo completado, y su «no esta publicado» se imponia
  sobre la cola aunque esta mostrara el batch justo antes o despues.
- Una sola pagina con otro Log Page Number descartaba el batch entero, y con
  el su ID.
- Un cierre omitido dejaba al batch fuera de la busqueda y aun asi se le
  borraba el ID por no haberlo buscado.
- Dos copias con el mismo titulo, o un titulo cambiado a mano, no dejaban
  elegir ninguna y la carga acababa como no encontrada.

Nada de esto toca la red: todo va contra el cliente falso.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.airvault.config import (
    AirVaultConfig,
    CAMPO_BATCH_NAME,
    CAMPO_LOG_NUMBER,
)
from app.airvault.confirmacion import METODO_MUESTRA, huella_de_numeros
from app.airvault.discovery import LoteAmbiguo
from app.airvault.flujo import (
    DESCUADRADO,
    INCOMPLETO,
    LISTO,
    NO_ENCONTRADO,
    PROCESANDO,
    Trabajo,
    comprobar_partes,
    estado_local,
    partes_por_recuperar,
    partes_por_subir,
    ruta_indice_paginas,
    ya_esta_en_airvault,
)
from app.airvault.manifest import guardar_confirmacion, ruta_manifiesto
from app.airvault.model import EstadoEtapa
from tests.airvault_fake import ClienteFalso, lote, pagina
from tests.test_airvault_comprobar import paginas_ocr

NOMBRE = "DP | BITS 02 OCT 2026 11 24 REVISAR"
BITACORAS = 8
CABECERA = (
    "﻿file,page,log_number,dup,disc,matricula,flight_number,"
    "pilot_signature,captain_signature,captain_license,"
    "technician_signature,date,time_ms\n"
)


def corrida(tmp_path, nombre: str = "BITS 02 OCT 2026 11 24"):
    """Una ejecucion exportada con ocho bitacoras en un solo PDF."""
    carpeta = tmp_path / "output" / nombre
    (carpeta / "datos").mkdir(parents=True)
    csv = carpeta / "datos" / f"{nombre}.CSV"
    csv.write_text(
        CABECERA + "".join(
            f"Image_001.pdf,{n},{2334700 + n},false,false,HP-1848CMP,472,"
            "true,false,false,true,2026/08/12,10372.0\n"
            for n in range(1, BITACORAS + 1)
        ),
        encoding="utf-8",
    )
    (carpeta / f"{nombre}.pdf").write_bytes(b"%PDF-1.4\n")
    ruta_indice_paginas(csv).write_text(
        json.dumps({"version": 2, "partes": [{
            "pdf": f"{nombre}.pdf",
            "paginas": [
                {"archivo": "Image_001.pdf", "pagina": n}
                for n in range(1, BITACORAS + 1)
            ],
        }]}),
        encoding="utf-8",
    )
    return csv


def en_la_cola(tmp_path, batch_id: str = "003TGP"):
    """Un trabajo subido y su batch en la cola, con el OCR de sus paginas."""
    trabajo = Trabajo.preparar(
        AirVaultConfig(), tmp_path / "job", corrida(tmp_path), NOMBRE
    )
    trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "entrega.pdf")
    trabajo.manifiesto.lotes_previos = ["003VIEJO"]
    trabajo.guardar()
    cliente = ClienteFalso(
        lotes=[lote("003VIEJO", "DP | LO DE ANTES", 9),
               lote(batch_id, NOMBRE, BITACORAS)],
        paginas_por_lote={batch_id: paginas_ocr(trabajo)},
        page_count=BITACORAS,
    )
    return trabajo, cliente


def identificado(tmp_path, batch_id: str = "003TGP"):
    """El mismo trabajo despues de que una revision ya lo encontro."""
    trabajo, cliente = en_la_cola(tmp_path, batch_id)
    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == LISTO
    assert trabajo.manifiesto.batch_id == batch_id
    return trabajo, cliente


def otro_log(cliente, batch_id: str, numero_de_pagina: int) -> None:
    """Deja esa pagina con el numero que leyo o corrigio otra mano."""
    cliente.paginas_por_lote[batch_id][numero_de_pagina] = pagina(
        numero_de_pagina, estado=3, valores={CAMPO_LOG_NUMBER: "2324732"}
    )


def envejecer(trabajo, dias: int = 5) -> None:
    """Hace que la carga lleve subida mas de lo que AirVault tarda."""
    subida = trabajo.manifiesto.etapa("subir")
    subida.actualizada = (
        datetime.now() - timedelta(days=dias)
    ).isoformat(timespec="microseconds")
    trabajo.guardar()


def marcar_ausente(trabajo) -> None:
    """Lo que deja el chequeo cuando Web Search no tiene sus bitacoras."""
    manifiesto = trabajo.manifiesto
    manifiesto.websearch_resultado = "ausente"
    manifiesto.websearch_metodo = METODO_MUESTRA
    manifiesto.websearch_huella = huella_de_numeros(manifiesto)
    manifiesto.websearch_detalle = "0 de 7 números de bitácora encontrados."
    manifiesto.websearch_revision = datetime.now().isoformat(
        timespec="microseconds"
    )
    trabajo.guardar()
    assert trabajo.manifiesto.websearch_resultado == "ausente"


# ── la cola manda sobre la ausencia de Web Search ──────────────────


def test_la_ausencia_de_web_search_cede_ante_la_cola(tmp_path):
    """Sin completar no esta en Web Search, y eso no es que falte."""
    trabajo, cliente = identificado(tmp_path)
    marcar_ausente(trabajo)
    assert estado_local(trabajo).estado == NO_ENCONTRADO

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert trabajo.manifiesto.batch_id == "003TGP"
    assert trabajo.manifiesto.websearch_resultado == "en_cola"
    guardado = Trabajo.cargar(trabajo.config, trabajo.carpeta)
    assert guardado.manifiesto.websearch_resultado == "en_cola"
    assert estado_local(guardado).estado != NO_ENCONTRADO


def test_un_manifiesto_guardado_como_ausente_se_corrige_al_cargarlo(tmp_path):
    """Los que ya quedaron mal escritos no esperan a otro guardado."""
    trabajo, _cliente = identificado(tmp_path)
    manifiesto = trabajo.manifiesto
    manifiesto.websearch_resultado = "ausente"
    manifiesto.websearch_metodo = METODO_MUESTRA
    manifiesto.websearch_huella = huella_de_numeros(manifiesto)
    manifiesto.websearch_revision = "2026-10-12T20:00:14.423535"
    manifiesto.etapa("descubrir").actualizada = "2026-10-12T23:05:19.880772"
    ruta_manifiesto(trabajo.carpeta).write_text(
        manifiesto.model_dump_json(indent=2), encoding="utf-8"
    )

    cargado = Trabajo.cargar(trabajo.config, trabajo.carpeta)

    assert cargado.manifiesto.websearch_resultado == "en_cola"
    assert estado_local(cargado).estado != NO_ENCONTRADO


def test_la_ausencia_posterior_a_la_ultima_vez_en_la_cola_se_conserva(tmp_path):
    """Si Web Search contesto despues de verlo en la cola, hay que mirar."""
    trabajo, _cliente = identificado(tmp_path)
    marcar_ausente(trabajo)

    cargado = Trabajo.cargar(trabajo.config, trabajo.carpeta)

    assert cargado.manifiesto.websearch_resultado == "ausente"
    assert estado_local(cargado).estado == NO_ENCONTRADO


def chequeo_sin_hallar(trabajo) -> dict:
    """Lo que el chequeo guarda cuando Web Search no tiene sus bitacoras."""
    manifiesto = trabajo.manifiesto
    cierre = manifiesto.etapas.get("completar")
    return {
        "websearch_confirmado": "",
        "websearch_muestra": [],
        "websearch_detalle": "0 de 7 números de bitácora encontrados.",
        "websearch_metodo": METODO_MUESTRA,
        "websearch_batch_id": str(manifiesto.batch_id or ""),
        "websearch_revision": datetime.now().isoformat(timespec="microseconds"),
        "websearch_cotejadas": 0,
        "websearch_huella": huella_de_numeros(manifiesto),
        "batch_id": str(manifiesto.batch_id or ""),
        "websearch_resultado": "ausente",
        "subida_revisada": manifiesto.etapa("subir").actualizada,
        "cierre_revisado": cierre.actualizada if cierre else "",
    }


def test_el_chequeo_no_da_por_ausente_lo_que_la_cola_acaba_de_mostrar(tmp_path):
    """Ni lo pinta como no encontrado ni le borra lo ya verificado."""
    trabajo, _cliente = identificado(tmp_path)
    trabajo.manifiesto.etapa("verificar").marcar(EstadoEtapa.ERROR, "7/8 en verde")
    trabajo.guardar()

    guardado = guardar_confirmacion(chequeo_sin_hallar(trabajo), trabajo.carpeta)

    assert guardado.websearch_resultado == "en_cola"
    assert "verificar" in guardado.etapas
    cargado = Trabajo.cargar(trabajo.config, trabajo.carpeta)
    assert estado_local(cargado).estado == INCOMPLETO


def test_el_chequeo_da_por_ausente_el_batch_que_el_programa_completo(tmp_path):
    """Completado ya salio de la cola: ahi Web Search si es quien responde."""
    trabajo, _cliente = identificado(tmp_path)
    trabajo.manifiesto.etapa("verificar").marcar(EstadoEtapa.HECHA, "8/8 en verde")
    trabajo.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA, "8 en verde")
    trabajo.guardar()

    guardado = guardar_confirmacion(chequeo_sin_hallar(trabajo), trabajo.carpeta)

    assert guardado.websearch_resultado == "ausente"
    cargado = Trabajo.cargar(trabajo.config, trabajo.carpeta)
    assert estado_local(cargado).estado == NO_ENCONTRADO


def test_el_chequeo_da_por_ausente_lo_que_la_cola_mostro_hace_mucho(tmp_path):
    """Desde entonces pudieron completarlo o borrarlo: hay que mirar la cola."""
    trabajo, _cliente = identificado(tmp_path)
    trabajo.manifiesto.etapa("descubrir").actualizada = (
        datetime.now() - timedelta(hours=3)
    ).isoformat(timespec="microseconds")
    trabajo.guardar()

    guardado = guardar_confirmacion(chequeo_sin_hallar(trabajo), trabajo.carpeta)

    assert guardado.websearch_resultado == "ausente"


# ── una pagina distinta no es otro batch ───────────────────────────


def test_un_log_distinto_no_le_quita_el_id_al_batch_identificado(tmp_path):
    """AirVault tenia 2324732 y el manifiesto 2334732: una pagina de 227."""
    trabajo, cliente = identificado(tmp_path)
    otro_log(cliente, "003TGP", 5)

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert trabajo.manifiesto.batch_id == "003TGP"
    assert cliente.renombrados == []


def test_un_log_distinto_no_impide_reconocer_un_empty_batch(tmp_path):
    trabajo, cliente = en_la_cola(tmp_path, "003NUEVO")
    cliente.lotes = [lote("003NUEVO", "Empty-Batch", BITACORAS)]
    otro_log(cliente, "003NUEVO", 5)

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert trabajo.manifiesto.batch_id == "003NUEVO"
    assert cliente.renombrados == [("003NUEVO", NOMBRE)]


def test_un_batch_con_todos_los_logs_ajenos_se_sigue_descartando(tmp_path):
    """La tolerancia es para una pagina suelta, no para otro contenido."""
    trabajo, cliente = identificado(tmp_path)
    for numero_de_pagina in range(1, BITACORAS + 1):
        cliente.paginas_por_lote["003TGP"][numero_de_pagina] = pagina(
            numero_de_pagina, estado=3,
            valores={CAMPO_LOG_NUMBER: str(9000000 + numero_de_pagina)},
        )

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == DESCUADRADO
    assert "contenido no corresponde" in parte.detalle
    assert trabajo.manifiesto.batch_id is None


# ── un cierre omitido no saca al batch de la busqueda ──────────────


def test_un_cierre_omitido_con_paginas_sin_verificar_conserva_su_id(tmp_path):
    """Seguia en la cola y perdia el ID por no haberlo buscado siquiera."""
    trabajo, cliente = identificado(tmp_path)
    manifiesto = trabajo.manifiesto
    manifiesto.etapa("indexar").marcar(EstadoEtapa.HECHA, "escritas 7")
    manifiesto.etapa("verificar").marcar(
        EstadoEtapa.ERROR, "7/8 en verde; pagina 5: falta confirmar"
    )
    manifiesto.etapa("completar").marcar(
        EstadoEtapa.OMITIDA, "1 pagina fuera de verde"
    )
    trabajo.guardar()

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == INCOMPLETO
    assert trabajo.manifiesto.batch_id == "003TGP"
    assert trabajo.manifiesto.etapa_hecha("descubrir")


# ── el ID que ya se conocia decide entre copias y titulos ──────────


def test_un_titulo_cambiado_a_mano_no_esconde_el_batch_conocido(tmp_path):
    """El ID y el contenido son los suyos: se le devuelve el titulo."""
    trabajo, cliente = identificado(tmp_path)
    trabajo.manifiesto.lotes_previos = ["003VIEJO", "003TGP"]
    trabajo.guardar()
    cliente.lotes = [lote("003TGP", "DP | REVISADO POR AUDITORIA", BITACORAS)]

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert trabajo.manifiesto.batch_id == "003TGP"
    assert cliente.renombrados == [("003TGP", NOMBRE)]


def test_entre_dos_copias_se_conserva_la_que_ya_tenia_el_trabajo(tmp_path):
    """Una segunda copia no deja a la primera sin dueño."""
    trabajo, cliente = identificado(tmp_path)
    cliente.lotes.append(lote("003THZ", NOMBRE, BITACORAS))
    cliente.paginas_por_lote["003THZ"] = paginas_ocr(trabajo)
    # La revision que le quito el ID no borro el recuerdo de cual era.
    trabajo.manifiesto.batch_id = None
    trabajo.manifiesto.etapas.pop("descubrir")
    trabajo.manifiesto.websearch_batch_id = "003tgp"
    trabajo.guardar()

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert trabajo.manifiesto.batch_id == "003TGP"


def test_dos_copias_sin_saber_cual_es_no_se_dan_por_no_encontradas(tmp_path):
    """Estan en la cola: lo que falta es que alguien deje una sola."""
    trabajo, cliente = en_la_cola(tmp_path)
    cliente.lotes.append(lote("003THZ", NOMBRE, BITACORAS))
    cliente.paginas_por_lote["003THZ"] = paginas_ocr(trabajo)
    envejecer(trabajo)

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == PROCESANDO
    assert "003TGP" in parte.detalle and "003THZ" in parte.detalle
    assert not trabajo.manifiesto.no_encontrado_desde
    assert partes_por_subir([parte]) == []
    assert partes_por_recuperar([parte]) == []


def test_un_titulo_puesto_a_otra_carga_no_acaba_como_no_encontrado(tmp_path):
    """Su Batch Name interno es de otro trabajo: se avisa, no se resube."""
    trabajo, cliente = en_la_cola(tmp_path)
    cliente.paginas_por_lote["003TGP"] = {
        1: pagina(1, estado=3, valores={CAMPO_BATCH_NAME: "DP | OTRA CARGA"}),
    }
    envejecer(trabajo)

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == DESCUADRADO
    assert "contenido no corresponde" in parte.detalle
    assert not trabajo.manifiesto.no_encontrado_desde
    assert partes_por_recuperar([parte]) == []


# ── la orden de subir a mano tampoco duplica ───────────────────────


def test_la_orden_a_mano_reconoce_su_batch_entre_dos_copias(tmp_path):
    trabajo, cliente = identificado(tmp_path)
    cliente.lotes.append(lote("003THZ", NOMBRE, BITACORAS))

    publicado = ya_esta_en_airvault(trabajo, cliente)

    assert publicado is not None and publicado.batch_id == "003TGP"


def test_la_orden_a_mano_no_sube_un_batch_que_sigue_con_otro_titulo(tmp_path):
    trabajo, cliente = identificado(tmp_path)
    cliente.lotes = [lote("003TGP", "DP | REVISADO POR AUDITORIA", BITACORAS)]

    with pytest.raises(LoteAmbiguo, match="003TGP"):
        ya_esta_en_airvault(trabajo, cliente)
