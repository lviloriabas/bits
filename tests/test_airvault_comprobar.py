"""Esperar a AirVault y cerrar el batch cuando lo acepta.

Subir no es lo mismo que estar listo. AirVault mete el batch en su cola y
tarda (minutos, a veces mucho mas) en dejarlo indexable, asi que el
programa pregunta cada tanto en vez de quedarse esperando delante. Aqui se
fija que responde esa pregunta en cada momento, y en que condiciones el
batch se puede dar por terminado.

Nada de esto toca la red: todo va contra el cliente falso.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.airvault.client import PaginaDelLote
from app.airvault.config import (
    AirVaultConfig,
    CAMPO_BATCH_NAME,
    CAMPO_END_DATE,
    CAMPO_LOG_NUMBER,
    CAMPO_MATRICULA,
)
from app.airvault.discovery import LoteAmbiguo, LoteNoEncontrado
from app.airvault.flujo import (
    AUTOCOMPLETADO,
    BUSCANDO,
    COMPLETADO,
    DESCUADRADO,
    ErrorDeCorrida,
    EstadoParte,
    INCOMPLETO,
    INDEXADO,
    LISTO,
    PROCESANDO,
    SIN_SUBIR,
    SOLO_REVISAR,
    TOMADO,
    Trabajo,
    cargar_partes,
    cargar_trabajos_pendientes,
    comprobar_entrega,
    comprobar_partes,
    detectar_indexados,
    completar_partes,
    estado_local,
    indexar_partes,
    preparar_partes,
    _reconciliar_batches,
    reiniciar_trabajos_incompletos,
    ruta_indice_paginas,
    partes_por_subir,
    subida_estancada,
    subida_perdida,
    subida_rebasada,
    subir_partes,
)
from app.airvault.mapping import fecha_airvault
from app.airvault.model import EstadoEtapa, EstadoRegistro
from tests.airvault_fake import ClienteFalso, lote, pagina

CSV = (
    "﻿file,page,log_number,dup,disc,matricula,flight_number,"
    "pilot_signature,captain_signature,captain_license,"
    "technician_signature,date,time_ms\n"
    "Image_001.pdf,1,2312238,false,false,HP-1848CMP,472,true,false,false,"
    "true,2026/08/12,10372.0\n"
    "Image_001.pdf,2,2312239,false,false,HP-1848CMP,389,true,true,true,"
    "false,2026/08/13,11268.3\n"
)


def corrida(tmp_path, nombre: str = "BITS 18 AUG 2026 05 42",
            pdfs: tuple[str, ...] = (), con_divisoria: bool = False):
    """La carpeta que deja una ejecucion exportada.

    Con ``con_divisoria`` el PDF lleva delante la pagina que abre el grupo
    de un avion, como la entrega de verdad: ocupa sitio en el batch sin ser
    una bitacora.
    """
    carpeta = tmp_path / "output" / nombre
    (carpeta / "datos").mkdir(parents=True)
    csv = carpeta / "datos" / f"{nombre}.CSV"
    csv.write_text(CSV, encoding="utf-8")
    archivos = list(pdfs) or [f"{nombre}.pdf"]
    for pdf in archivos:
        (carpeta / pdf).write_bytes(b"%PDF-1.4\n")
    delante = [{"separador": "HP-1848CMP"}] if con_divisoria else []
    paginas = [
        delante + [{"archivo": "Image_001.pdf", "pagina": 1},
                   {"archivo": "Image_001.pdf", "pagina": 2}]
        if len(archivos) == 1
        else [{"archivo": "Image_001.pdf", "pagina": n + 1}]
        for n in range(len(archivos))
    ]
    ruta_indice_paginas(csv).write_text(
        json.dumps({"version": 2, "partes": [
            {"pdf": archivo, "paginas": suyas}
            for archivo, suyas in zip(archivos, paginas)
        ]}),
        encoding="utf-8",
    )
    return csv


def trabajo_subido(tmp_path, paginas_en_airvault: int = 2,
                   batch_id: str = "003SRO"):
    """Un trabajo de una parte, ya subida, y el cliente que la ve."""
    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "entrega.pdf")
    trabajo.manifiesto.lotes_previos = ["003VIEJO"]
    trabajo.guardar()
    cliente = ClienteFalso(
        paginas={n: pagina(n, estado=3) for n in range(1, 3)},
        lotes=[lote("003VIEJO", "DP | LO DE ANTES", 9),
               lote(batch_id, trabajo.manifiesto.nombre_batch,
                    paginas_en_airvault)],
        picklist=["HP-1848CMP"],
        page_count=2,
    )
    return trabajo, cliente


def paginas_ocr(trabajo, nombre_interno: str = ""):
    """OCR que permite reconocer el contenido de un batch provisional."""
    paginas = {}
    for registro in trabajo.manifiesto.bitacoras():
        valores = {
            CAMPO_LOG_NUMBER: registro.log_number,
            CAMPO_MATRICULA: registro.matricula,
        }
        if nombre_interno and registro.seq == 1:
            valores[CAMPO_BATCH_NAME] = nombre_interno
        paginas[registro.seq] = pagina(
            registro.seq, estado=3, valores=valores
        )
    return paginas


# ── en que va cada parte ───────────────────────────────────────────

def test_sin_subir_lo_dice_y_no_pregunta_por_ningun_lote(tmp_path):
    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    cliente = ClienteFalso()
    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == SIN_SUBIR
    assert not parte.se_puede_indexar


def test_subido_pero_todavía_no_en_la_cola_no_es_un_fallo(tmp_path):
    """AirVault tarda en sacar un batch recien subido; eso es lo normal."""
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = []
    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == PROCESANDO
    assert "(1/3)" in parte.detalle
    assert not parte.se_puede_indexar
    assert not parte.se_acabo


def test_un_lote_a_medio_procesar_todavía_no_esta_listo(tmp_path):
    """Aparece en la cola antes de tener todas sus paginas.

    Escribir asi correria cada dato a la bitacora de al lado, asi que
    mientras las paginas no cuadren la parte no se toca.
    """
    trabajo, cliente = trabajo_subido(tmp_path, paginas_en_airvault=1)
    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == PROCESANDO
    assert "1 de 2 páginas" in parte.detalle
    assert not parte.se_puede_indexar


def test_con_todas_las_paginas_queda_listo_para_indexar(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == LISTO
    assert parte.se_puede_indexar
    assert parte.batch_id == "003SRO"


def test_el_aviso_de_escritura_identifica_el_batch(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    comprobar_partes([trabajo], cliente)
    plan = trabajo.planificar(cliente)
    mensajes = []

    indexar_partes(
        [trabajo],
        [plan],
        avisar=lambda texto, _hechas, _total: mensajes.append(texto),
    )

    assert any(
        texto == "Batch 003SRO: Escribiendo en AirVault"
        for texto in mensajes
    )


def test_un_batch_mayor_que_el_manifiesto_se_detiene(tmp_path):
    """Un 200 para una parte de 100 no va a reducirse mientras se espera."""
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = [lote(
        "003JUNTO", trabajo.manifiesto.nombre_batch, 200
    )]

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == DESCUADRADO
    assert parte.se_acabo
    assert not parte.se_puede_indexar
    assert "AirVault junto dos cargas" in parte.detalle


def test_el_lote_encontrado_queda_anotado_y_con_su_nombre(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    comprobar_partes([trabajo], cliente)
    assert trabajo.manifiesto.batch_id == "003SRO"
    assert trabajo.manifiesto.etapa_hecha("descubrir")


def test_sin_registro_recupera_por_nombre_y_detecta_el_indexado(
    tmp_path, monkeypatch,
):
    from app.airvault.config import CAMPO_DOC_TYPE, CAMPO_FLEET, CAMPO_AUDIT_STATUS
    """Un manifiesto reconstruido no autoriza otra carga del mismo PDF."""
    csv = corrida(tmp_path)
    carpeta = tmp_path / "registro-reconstruido"
    trabajo = Trabajo.preparar(
        AirVaultConfig(), carpeta, csv, "DP | BIT 18 AUG 2026 05 42"
    )
    paginas = {
        registro.seq: pagina(
            registro.seq,
            estado=0,
            valores={
                CAMPO_DOC_TYPE: "Log Page", CAMPO_FLEET: "NG", CAMPO_AUDIT_STATUS: "PUBLISHED",
                CAMPO_END_DATE: fecha_airvault(registro.fecha),
                CAMPO_LOG_NUMBER: registro.log_number,
                CAMPO_MATRICULA: registro.matricula,
            },
        )
        for registro in trabajo.manifiesto.bitacoras()
    }
    cliente = ClienteFalso(
        paginas=paginas,
        lotes=[lote(
            "003SRO", trabajo.manifiesto.nombre_batch,
            len(trabajo.manifiesto.registros),
        )],
        page_count=len(trabajo.manifiesto.registros),
    )
    subidas = []
    monkeypatch.setattr(
        Trabajo, "subir", lambda *args, **kwargs: subidas.append(True)
    )

    subir_partes([trabajo], SesionFalsa(), cliente=cliente)

    assert subidas == []
    assert trabajo.manifiesto.batch_id == "003SRO"
    assert trabajo.manifiesto.etapa_hecha("subir")
    assert trabajo.manifiesto.etapa_hecha("verificar")
    assert estado_local(trabajo).estado == INDEXADO
    assert (carpeta / "manifiesto.json").is_file()


def test_revisar_detecta_paginas_completadas_a_mano(tmp_path):
    from app.airvault.config import CAMPO_DOC_TYPE, CAMPO_FLEET, CAMPO_AUDIT_STATUS
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    trabajo.manifiesto.etapa("verificar").marcar(
        EstadoEtapa.ERROR, "1/2 en Valid"
    )
    cliente.paginas = {
        registro.seq: pagina(
            registro.seq,
            estado=0,
            valores={
                CAMPO_DOC_TYPE: "Log Page", CAMPO_FLEET: "NG", CAMPO_AUDIT_STATUS: "PUBLISHED",
                CAMPO_END_DATE: fecha_airvault(registro.fecha),
                CAMPO_LOG_NUMBER: registro.log_number,
                CAMPO_MATRICULA: registro.matricula,
            },
        )
        for registro in trabajo.manifiesto.bitacoras()
    }
    estados = comprobar_partes([trabajo], cliente)

    detectado, = detectar_indexados(estados, cliente)

    assert detectado.estado == INDEXADO
    assert trabajo.manifiesto.etapa_hecha("verificar")


def test_un_batch_sin_paginas_en_verde_no_se_relee_pagina_por_pagina(
    tmp_path,
):
    """El mapa del batch basta para saber que aun no hay nada indexado."""
    from app.airvault.client import PaginaDelLote

    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = [PaginaDelLote(n, 3, n) for n in (1, 2)]
    estados = comprobar_partes([trabajo], cliente)
    cliente.lecturas.clear()

    detectado, = detectar_indexados(estados, cliente)

    assert detectado.estado == LISTO
    assert cliente.lecturas == []
    assert not trabajo.manifiesto.etapa_hecha("verificar")

    # Con una sola pagina en verde ya hay algo que confirmar.
    cliente.mapa = [PaginaDelLote(1, 0, 1), PaginaDelLote(2, 3, 2)]
    detectar_indexados(estados, cliente)
    assert sorted(cliente.lecturas) == [1, 2]


def test_worker_de_revision_descarta_el_plan_anterior_al_indexado_manual(
    tmp_path,
):
    from app.airvault.config import CAMPO_DOC_TYPE, CAMPO_FLEET, CAMPO_AUDIT_STATUS
    from app.gui.airvault_window import TrabajoAirVaultWorker

    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    trabajo.manifiesto.etapa("verificar").marcar(
        EstadoEtapa.ERROR, "1/2 en Valid"
    )
    cliente.paginas = {
        registro.seq: pagina(
            registro.seq,
            estado=0,
            valores={
                CAMPO_DOC_TYPE: "Log Page", CAMPO_FLEET: "NG", CAMPO_AUDIT_STATUS: "PUBLISHED",
                CAMPO_END_DATE: fecha_airvault(registro.fecha),
                CAMPO_LOG_NUMBER: registro.log_number,
                CAMPO_MATRICULA: registro.matricula,
            },
        )
        for registro in trabajo.manifiesto.bitacoras()
    }
    clave = str(trabajo.carpeta)
    estado = {
        "config": AirVaultConfig(),
        "cliente": cliente,
        "trabajos": [trabajo],
        "raiz": tmp_path,
        "carpeta_job": tmp_path / "reporte",
        "planes": {clave: object()},
    }
    comprobaciones = []
    worker = TrabajoAirVaultWorker("comprobar", estado)
    worker.comprobado.connect(comprobaciones.append)

    worker._comprobar()

    assert comprobaciones[0]["estados"][0].estado == INDEXADO
    assert clave not in comprobaciones[0]["planes"]


def test_la_comprobacion_periodica_recupera_un_empty_batch_interrumpido(
    tmp_path,
):
    """Cerrar la UI tras Quick Upload no deja el batch perdido ni lo repite."""
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = [
        lote("003VIEJO", "DP | LO DE ANTES", 9),
        lote("003NUEVO", "Empty-Batch", 2),
    ]
    cliente.paginas_por_lote["003NUEVO"] = paginas_ocr(trabajo)

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert parte.batch_id == "003NUEVO"
    assert trabajo.manifiesto.batch_id == "003NUEVO"
    assert trabajo.manifiesto.etapa_hecha("subir")
    assert trabajo.manifiesto.etapa_hecha("descubrir")
    assert cliente.renombrados == [
        ("003NUEVO", trabajo.manifiesto.nombre_batch)
    ]


def test_avisos_cortos_al_detectar_y_corregir_empty_batch(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = [lote("003NUEVO", "Empty-Batch", 2)]
    cliente.paginas_por_lote["003NUEVO"] = paginas_ocr(trabajo)
    mensajes = []

    comprobar_partes(
        [trabajo],
        cliente,
        avisar=lambda texto, _hechas, _total: mensajes.append(texto),
    )

    assert any(
        texto.startswith("Batch 003NUEVO: Empty-Batch detectado")
        for texto in mensajes
    )
    assert any(
        texto.startswith("Batch 003NUEVO: corrigiendo nombre")
        for texto in mensajes
    )
    assert "Batch 003NUEVO: nombre corregido" in mensajes


def test_identifica_empty_batch_aunque_falte_un_log_ocr(tmp_path):
    """Un OCR ausente no invalida las demas coincidencias del contenido."""
    trabajo, cliente = trabajo_subido(tmp_path)
    primero, segundo = trabajo.manifiesto.bitacoras()
    cliente.lotes = [lote("003NUEVO", "Empty-Batch", 2)]
    cliente.paginas_por_lote["003NUEVO"] = {
        primero.seq: pagina(
            primero.seq,
            estado=3,
            valores={
                CAMPO_LOG_NUMBER: primero.log_number,
                CAMPO_MATRICULA: primero.matricula,
                CAMPO_END_DATE: "08/12/2026",
            },
        ),
        segundo.seq: pagina(segundo.seq, estado=3, valores={}),
    }

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert parte.batch_id == "003NUEVO"


def test_no_renombra_empty_batch_con_cantidad_distinta(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = [lote("003OTRO", "Empty-Batch", 3)]
    cliente.paginas_por_lote["003OTRO"] = paginas_ocr(
        trabajo, trabajo.manifiesto.nombre_batch
    )

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == DESCUADRADO
    assert parte.se_acabo
    assert cliente.renombrados == []


def test_no_renombra_empty_batch_si_el_ocr_contradice_el_manifiesto(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = [lote("003OTRO", "Empty-Batch", 2)]
    cliente.paginas_por_lote["003OTRO"] = {
        registro.seq: pagina(
            registro.seq,
            estado=3,
            valores={CAMPO_LOG_NUMBER: "9999999"},
        )
        for registro in trabajo.manifiesto.bitacoras()
    }

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == PROCESANDO
    assert cliente.renombrados == []


def test_revisar_no_confia_en_un_nombre_completo_con_logs_ajenos(tmp_path):
    """El título correcto ya no evita la verificación del contenido."""
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.paginas_por_lote["003SRO"] = {
        registro.seq: pagina(
            registro.seq,
            estado=3,
            valores={CAMPO_LOG_NUMBER: "9999999"},
        )
        for registro in trabajo.manifiesto.bitacoras()
    }

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == DESCUADRADO
    assert trabajo.manifiesto.batch_id is None
    assert "contenido no corresponde" in parte.detalle
    assert cliente.renombrados == []


def test_revisar_corrige_otro_nombre_completo_si_la_huella_coincide(tmp_path):
    """Un nombre equivocado no obliga a resubir el PDF ni cambia su ID."""
    trabajo, _ = trabajo_subido(tmp_path)
    cliente = ClienteFalso(
        lotes=[lote("003CRUZADO", "DP | NOMBRE EQUIVOCADO", 2)],
        paginas_por_lote={"003CRUZADO": paginas_ocr(trabajo)},
    )

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert parte.batch_id == "003CRUZADO"
    assert cliente.renombrados == [
        ("003CRUZADO", trabajo.manifiesto.nombre_batch)
    ]


def test_aviso_corto_al_detectar_un_nombre_incorrecto(tmp_path):
    trabajo, _ = trabajo_subido(tmp_path)
    cliente = ClienteFalso(
        lotes=[lote("003CRUZADO", "DP | NOMBRE EQUIVOCADO", 2)],
        paginas_por_lote={"003CRUZADO": paginas_ocr(trabajo)},
    )
    mensajes = []

    comprobar_partes(
        [trabajo],
        cliente,
        avisar=lambda texto, _hechas, _total: mensajes.append(texto),
    )

    assert any(
        texto.startswith(
            "Batch 003CRUZADO: nombre incorrecto «DP | NOMBRE EQUIVOCADO»"
        )
        for texto in mensajes
    )


def test_confirma_pronto_el_nombre_sin_repetir_el_renombrado(
    tmp_path, monkeypatch
):
    trabajo, _ = trabajo_subido(tmp_path)

    class ClienteConPropagacionDiferida(ClienteFalso):
        def __init__(self):
            super().__init__(lotes=[lote("003LENTO", "Empty-Batch", 2)])
            self.nombre_pendiente = ""
            self.intentos_renombrado = 0
            self.consultas_confirmacion = 0

        def renombrar_lote(self, _batch_id, nombre):
            self.intentos_renombrado += 1
            self.nombre_pendiente = nombre
            return True

        def listar_lotes(self, filtro=""):
            self.consultas_confirmacion += 1
            if self.consultas_confirmacion == 3:
                self.lotes = [
                    lote("003LENTO", self.nombre_pendiente, 2)
                ]
            return super().listar_lotes(filtro)

    cliente = ClienteConPropagacionDiferida()
    pausas = []
    monkeypatch.setattr("app.airvault.flujo.time.sleep", pausas.append)

    trabajo._ponerle_nombre(cliente, cliente.lotes[0])

    assert cliente.intentos_renombrado == 1
    assert pausas == [0.25, 0.5]


def test_revisar_corrige_un_nombre_incompleto_sin_resubir(tmp_path):
    trabajo, _ = trabajo_subido(tmp_path)
    incompleto = trabajo.manifiesto.nombre_batch.rsplit(" ", 1)[0]
    cliente = ClienteFalso(
        lotes=[lote("003CORTO", incompleto, 2)],
        paginas_por_lote={
            "003CORTO": paginas_ocr(
                trabajo, trabajo.manifiesto.nombre_batch
            )
        },
    )

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert parte.batch_id == "003CORTO"
    assert cliente.renombrados == [
        ("003CORTO", trabajo.manifiesto.nombre_batch)
    ]


def test_un_candidato_provisional_no_se_reenvia_por_antiguo(
    tmp_path, monkeypatch
):
    trabajo, _ = trabajo_subido(tmp_path)
    subida = trabajo.manifiesto.etapa("subir")
    subida.actualizada = "2020-01-01T00:00:00"
    trabajo.guardar()
    cliente = ClienteFalso(lotes=[lote("003PEND", "Empty-Batch", 2)])
    subidas = []
    monkeypatch.setattr(
        Trabajo, "subir", lambda *args, **kwargs: subidas.append(True)
    )

    estados = comprobar_partes([trabajo], cliente)
    subir_partes([trabajo], SesionFalsa(), cliente=cliente)

    assert estados[0].estado == PROCESANDO
    assert subidas == []


def test_recupera_varios_empty_batch_por_las_instantaneas_de_subida(tmp_path):
    """El orden de las instantáneas separa incluso batches de igual tamaño."""
    ids = ["003E01", "003E02", "003E03"]
    trabajos = []
    for indice in range(3):
        trabajo, _ = trabajo_subido(tmp_path / f"parte-{indice + 1}")
        trabajo.manifiesto.nombre_batch = f"DP | BITS PRUEBA -{indice + 1}"
        trabajo.manifiesto.lotes_previos = ["003VIEJO", *ids[:indice]]
        trabajo.guardar()
        trabajos.append(trabajo)
    cliente = ClienteFalso(lotes=[
        lote("003VIEJO", "DP | ANTERIOR", 9),
        *(lote(batch_id, "Empty-Batch", 2) for batch_id in ids),
    ], paginas_por_lote={
        batch_id: paginas_ocr(trabajo, trabajo.manifiesto.nombre_batch)
        for batch_id, trabajo in zip(ids, trabajos)
    })

    estados = comprobar_partes(trabajos, cliente)

    assert [parte.batch_id for parte in estados] == ids
    assert [lote.nombre for lote in cliente.lotes if lote.batch_id in ids] == [
        trabajo.manifiesto.nombre_batch for trabajo in trabajos
    ]


def test_el_batch_name_interno_evita_el_recorrido_ocr_cruzado(tmp_path):
    """Una pagina por batch basta cuando el PDF conserva su nombre interno."""
    ids = ["003E01", "003E02", "003E03"]
    trabajos = []
    for indice, batch_id in enumerate(ids, start=1):
        trabajo, _ = trabajo_subido(tmp_path / f"parte-{indice}")
        trabajo.manifiesto.nombre_batch = f"DP | BITS RAPIDO -{indice}"
        trabajo.guardar()
        trabajos.append(trabajo)
    cliente = ClienteFalso(
        lotes=[lote(batch_id, "Empty-Batch", 2) for batch_id in ids],
        paginas_por_lote={
            batch_id: paginas_ocr(trabajo, trabajo.manifiesto.nombre_batch)
            for batch_id, trabajo in zip(ids, trabajos)
        },
    )

    estados = comprobar_partes(trabajos, cliente)

    assert all(estado.estado == LISTO for estado in estados)
    assert cliente.lecturas == [1, 1, 1]
    assert [trabajo.manifiesto.batch_id for trabajo in trabajos] == ids


def test_un_renombrado_fallido_no_frena_los_siguientes(tmp_path):
    class ClienteConUnNombreRechazado(ClienteFalso):
        def renombrar_lote(self, batch_id, nombre):
            if batch_id == "003E01":
                return False
            return super().renombrar_lote(batch_id, nombre)

    ids = ["003E01", "003E02"]
    trabajos = []
    for indice, batch_id in enumerate(ids, start=1):
        trabajo, _ = trabajo_subido(tmp_path / f"parte-{indice}")
        trabajo.manifiesto.nombre_batch = f"DP | BITS CONTINUA -{indice}"
        trabajo.guardar()
        trabajos.append(trabajo)
    cliente = ClienteConUnNombreRechazado(
        lotes=[lote(batch_id, "Empty-Batch", 2) for batch_id in ids],
        paginas_por_lote={
            batch_id: paginas_ocr(trabajo, trabajo.manifiesto.nombre_batch)
            for batch_id, trabajo in zip(ids, trabajos)
        },
    )

    renombrados = _reconciliar_batches(trabajos, cliente, cliente.lotes)

    assert renombrados == 1
    assert trabajos[0].manifiesto.batch_id is None
    assert trabajos[1].manifiesto.batch_id == "003E02"
    assert cliente.renombrados == [
        ("003E02", trabajos[1].manifiesto.nombre_batch)
    ]


def test_corrige_un_id_cruzado_y_asigna_el_liberado_en_el_mismo_sondeo(
    tmp_path,
):
    primero, _ = trabajo_subido(tmp_path / "primero")
    segundo, _ = trabajo_subido(tmp_path / "segundo")
    primero.manifiesto.nombre_batch = "DP | BITS CRUZADO -1"
    segundo.manifiesto.nombre_batch = "DP | BITS CRUZADO -2"
    segundo.fijar_lote("003E01")
    for trabajo in (primero, segundo):
        trabajo.manifiesto.intentos_identificacion = 3
        trabajo.manifiesto.espera_reenvio_desde = "2020-01-01T00:00:00"
        trabajo.guardar()
    cliente = ClienteFalso(
        lotes=[
            lote("003E01", segundo.manifiesto.nombre_batch, 2),
            lote("003E02", primero.manifiesto.nombre_batch, 2),
        ],
        paginas_por_lote={
            "003E01": paginas_ocr(
                primero, primero.manifiesto.nombre_batch
            ),
            "003E02": paginas_ocr(
                segundo, segundo.manifiesto.nombre_batch
            ),
        },
    )

    estados = comprobar_partes([primero, segundo], cliente)

    assert [primero.manifiesto.batch_id, segundo.manifiesto.batch_id] == [
        "003E01", "003E02",
    ]
    assert all(estado.estado == LISTO for estado in estados)
    assert all(
        trabajo.manifiesto.intentos_identificacion == 0
        and not trabajo.manifiesto.espera_reenvio_desde
        for trabajo in (primero, segundo)
    )


def test_no_indexa_si_airvault_no_confirma_el_nombre_del_empty_batch(tmp_path):
    class ClienteSinRename(ClienteFalso):
        def renombrar_lote(self, _batch_id, _nombre):
            return False

    trabajo, _ = trabajo_subido(tmp_path)
    cliente = ClienteSinRename(lotes=[
        lote("003VIEJO", "DP | ANTERIOR", 9),
        lote("003VACIO", "Empty-Batch", 2),
    ], paginas_por_lote={"003VACIO": paginas_ocr(trabajo)})

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == PROCESANDO
    assert trabajo.manifiesto.batch_id is None
    assert not trabajo.manifiesto.etapa_hecha("descubrir")


def test_el_nombre_corregido_a_mano_reemplaza_un_id_de_index_batch(tmp_path):
    trabajo, _cliente = trabajo_subido(tmp_path, batch_id="003MAL")
    trabajo.fijar_lote("003MAL")
    cliente = ClienteFalso(lotes=[
        lote("003MAL", "Index Batch", 2),
        lote("003BIEN", trabajo.manifiesto.nombre_batch, 2),
    ])

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert trabajo.manifiesto.batch_id == "003BIEN"


def test_un_id_cuyo_titulo_no_coincide_se_elimina(tmp_path):
    trabajo, _cliente = trabajo_subido(tmp_path, batch_id="003MAL")
    trabajo.fijar_lote("003MAL")
    cliente = ClienteFalso(lotes=[lote("003MAL", "Index Batch", 2)])

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == PROCESANDO
    assert parte.batch_id == ""
    assert trabajo.manifiesto.batch_id is None
    assert not trabajo.manifiesto.etapa_hecha("descubrir")


def _trabajos_principal_division_y_revisar(tmp_path):
    """Tres nombres esperados, incluidos los dos que no terminan en numero."""
    csv = corrida(tmp_path)
    nombres = ("DP | BIT", "DP | BIT -2", "DP | BIT REVISAR")
    trabajos = []
    for indice, nombre in enumerate(nombres, start=1):
        trabajo = Trabajo.preparar(
            AirVaultConfig(), tmp_path / f"job-{indice}", csv, nombre
        )
        trabajo.manifiesto.solo_subir = nombre.endswith("REVISAR")
        trabajo.manifiesto.lotes_previos = ["003VIEJO"]
        trabajo.manifiesto.parte = indice
        # Cada parte se lleva sus propias bitacoras, como en una entrega
        # repartida de verdad. Compartirlas seria un batch subido dos veces,
        # que es justo lo que la guarda de reparto no deja mandar.
        for registro in trabajo.manifiesto.registros:
            if not registro.es_separador:
                registro.archivo_origen = f"Image_{indice:03d}.pdf"
        trabajo.guardar()
        trabajos.append(trabajo)
    return trabajos


def test_comprobar_busca_principal_divisiones_y_revisar(tmp_path):
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    cliente = ClienteFalso(lotes=[
        lote("003PRI", "DP | BIT", 2),
        lote("003DOS", "DP | BIT -2", 2),
        lote("003REV", "DP | BIT REVISAR", 2),
    ])

    estados = comprobar_partes(trabajos, cliente)

    assert [t.manifiesto.batch_id for t in trabajos] == [
        "003PRI", "003DOS", "003REV"
    ]
    assert [parte.estado for parte in estados] == [
        LISTO, LISTO, SOLO_REVISAR
    ]
    assert all(t.manifiesto.etapa_hecha("subir") for t in trabajos)


def test_revisar_no_se_asigna_a_las_divisiones_sin_titulo(tmp_path):
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    for trabajo in trabajos:
        trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "subido")
        trabajo.manifiesto.batch_id = "003REV"
        trabajo.manifiesto.etapa("descubrir").marcar(
            EstadoEtapa.HECHA, "003REV"
        )
        trabajo.guardar()
    cliente = ClienteFalso(lotes=[
        lote("003REV", "DP | BIT REVISAR", 2),
    ])

    estados = comprobar_partes(trabajos, cliente)

    assert [t.manifiesto.batch_id for t in trabajos] == [None, None, "003REV"]
    assert [parte.estado for parte in estados] == [
        PROCESANDO, PROCESANDO, SOLO_REVISAR
    ]


def test_descubrir_tampoco_toma_revisar_para_un_automatico(tmp_path):
    automatico, _division, revisar = (
        _trabajos_principal_division_y_revisar(tmp_path)
    )
    cliente = ClienteFalso(lotes=[
        lote("003REV", revisar.manifiesto.nombre_batch, 2),
    ])

    with pytest.raises(LoteNoEncontrado):
        automatico.descubrir(cliente, esperar=False)

    assert automatico.manifiesto.batch_id is None


def test_un_lote_abierto_por_otro_no_se_ofrece_para_indexar(tmp_path):
    """AirVault no lo entrega: abrirlo deja la peticion colgada."""
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = [lote("003VIEJO", "DP | LO DE ANTES", 9),
                     lote("003SRO", trabajo.manifiesto.nombre_batch, 2,
                          bloqueado_por="Diego Vargas")]
    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == TOMADO
    assert "Diego Vargas" in parte.detalle
    assert not parte.se_puede_indexar


def test_el_lote_de_revisar_se_ofrece_para_indexar_lo_disponible(tmp_path):
    """Los datos del CSV se escriben y la persona confirma lo dudoso."""
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.manifiesto.solo_subir = True
    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == SOLO_REVISAR
    assert parte.se_puede_indexar
    assert not parte.se_acabo


def test_lo_ya_indexado_deja_de_esperar_a_nada(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.manifiesto.etapa("indexar").marcar(EstadoEtapa.HECHA, "escritas 2")
    trabajo.manifiesto.etapa("verificar").marcar(EstadoEtapa.HECHA, "2/2 en Valid")
    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == INDEXADO
    assert parte.se_acabo


def test_indexar_hecho_no_oculta_paginas_que_no_se_verificaron(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.manifiesto.etapa("indexar").marcar(EstadoEtapa.HECHA, "escritas 2")

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == LISTO
    assert parte.se_puede_indexar


def test_verificacion_incompleta_muestra_el_progreso_real_y_se_reintenta(
    tmp_path,
):
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.manifiesto.etapa("verificar").marcar(
        EstadoEtapa.ERROR, "1/2 en Valid"
    )

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == INCOMPLETO
    assert "1/2 en Valid" in str(parte)
    assert parte.se_puede_indexar


def test_estado_local_no_disfraza_una_verificacion_incompleta(tmp_path):
    trabajo, _cliente = trabajo_subido(tmp_path)
    trabajo.manifiesto.etapa("verificar").marcar(
        EstadoEtapa.ERROR, "1/2 en Valid"
    )

    parte = estado_local(trabajo)

    assert parte.estado == INCOMPLETO
    assert "1/2 en Valid" in str(parte)


def test_un_lote_ya_cerrado_no_se_vuelve_a_buscar(tmp_path):
    """Cerrado ya no aparece listado: buscarlo alli seria no encontrarlo nunca."""
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA, "29 en verde")
    cliente.lotes = []
    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == COMPLETADO
    assert parte.se_acabo


def test_un_empty_batch_tardio_no_reabre_ni_resube_un_lote_completado(
    tmp_path, monkeypatch
):
    """Un duplicado que aparece tarde no se confunde con el original cerrado."""
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003ORIGINAL")
    trabajo.manifiesto.etapa("completar").marcar(
        EstadoEtapa.HECHA, "2 paginas en verde"
    )
    trabajo.guardar()
    cliente.lotes = [lote("003TARDIO", "Empty-Batch", 2)]
    subidas = []
    monkeypatch.setattr(
        Trabajo, "subir", lambda *args, **kwargs: subidas.append(True)
    )

    subir_partes([trabajo], SesionFalsa(), cliente=cliente)

    assert subidas == []
    assert trabajo.manifiesto.batch_id == "003ORIGINAL"
    assert trabajo.manifiesto.etapa_hecha("completar")


def test_un_cierre_omitido_no_se_confunde_con_un_batch_completado(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.manifiesto.etapa("verificar").marcar(
        EstadoEtapa.HECHA, "2/2 en Valid"
    )
    trabajo.manifiesto.etapa("completar").marcar(
        EstadoEtapa.OMITIDA, "AirVault no lo acepto"
    )

    parte, = comprobar_partes([trabajo], cliente)

    assert parte.estado == INDEXADO


def test_comprobar_busca_los_nombres_de_la_tabla_en_orden(tmp_path):
    """La cola local dirige la búsqueda antes de revisar nombres extraños."""
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    cliente = ClienteFalso(lotes=[
        lote("003PRI", "DP | BIT", 2),
        lote("003DOS", "DP | BIT -2", 2),
        lote("003REV", "DP | BIT REVISAR", 2),
    ])
    cliente.filtros.clear()
    mensajes = []

    comprobar_partes(
        trabajos,
        cliente,
        avisar=lambda texto, _hechas, _total: mensajes.append(texto),
    )

    nombres = [trabajo.manifiesto.nombre_batch for trabajo in trabajos]
    assert cliente.filtros == nombres
    busquedas = [texto for texto in mensajes if texto.startswith("Buscando ")]
    assert busquedas == [
        f"Buscando {numero}/3 en AirVault: «{nombre}»"
        for numero, nombre in enumerate(nombres, start=1)
    ]


def test_el_estado_local_no_le_pregunta_nada_a_airvault(tmp_path):
    """Al elegir una ejecucion la lista se pinta antes de tocar la red."""
    trabajo, _cliente = trabajo_subido(tmp_path)
    parte = estado_local(trabajo)
    assert parte.estado == BUSCANDO
    assert "falta revisar" in parte.detalle


def test_busqueda_amplia_no_inspecciona_batches_ya_indexados(tmp_path):
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    terminado, pendiente, _ = trabajos
    terminado.fijar_lote("003VIEJO")
    terminado.manifiesto.etapa("verificar").marcar(EstadoEtapa.HECHA, "2/2 en Valid")
    pendiente.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "parte.pdf")
    pendiente.manifiesto.lotes_previos = ["003OTRO"]
    cliente = ClienteFalso(lotes=[lote("003VIEJO", "Empty-Batch", 2)])

    def no_abrir(*args):
        pytest.fail("No debe inspeccionar el batch ya indexado")

    cliente.abrir_lote = no_abrir
    estados = comprobar_partes([terminado, pendiente], cliente)
    assert estados[0].estado == INDEXADO
    assert terminado.manifiesto.batch_id == "003VIEJO"
    assert terminado.manifiesto.nombre_batch not in cliente.filtros
    assert "" in cliente.filtros


def test_reconciliar_no_busca_verificados_sin_id(tmp_path, monkeypatch):
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.manifiesto.etapa("verificar").marcar(EstadoEtapa.HECHA, "2/2 en Valid")

    def no_identificar(*args, **kwargs):
        pytest.fail("Un trabajo verificado no vuelve a buscarse por contenido")

    monkeypatch.setattr("app.airvault.flujo._lote_por_identidad_y_contenido", no_identificar)
    assert _reconciliar_batches([trabajo], cliente, cliente.lotes) == 0


def test_consultas_fuera_de_orden_conservan_el_orden_de_las_partes(tmp_path):
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    cliente = ClienteFalso(lotes=[
        lote("003PRI", "DP | BIT", 2),
        lote("003DOS", "DP | BIT -2", 2),
        lote("003REV", "DP | BIT REVISAR", 2),
    ])
    nombres = [trabajo.manifiesto.nombre_batch for trabajo in trabajos]

    def buscar(pedidos):
        assert pedidos == nombres
        for nombre in reversed(pedidos):
            yield nombre, cliente.listar_lotes(nombre)

    cliente.buscar_lotes = buscar
    estados = comprobar_partes(trabajos, cliente)
    assert [estado.trabajo for estado in estados] == trabajos
    assert [estado.batch_id for estado in estados] == ["003PRI", "003DOS", "003REV"]
    assert cliente.filtros == list(reversed(nombres))


def test_revisar_subido_sigue_esperando_hasta_que_tenga_id(tmp_path):
    revisar = _trabajos_principal_division_y_revisar(tmp_path)[2]
    revisar.manifiesto.etapa("subir").marcar(
        EstadoEtapa.HECHA, "revisar.pdf"
    )
    revisar.guardar()

    parte = estado_local(revisar)

    assert parte.estado == BUSCANDO
    assert parte.batch_id == ""


# ── retomar una ejecucion de ayer ──────────────────────────────────

def test_una_ejecucion_ya_preparada_se_retoma_sin_rehacerla(tmp_path):
    csv = corrida(tmp_path)
    config = AirVaultConfig()
    preparar_partes(config, tmp_path / "job", csv, "DP | BIT")
    trabajos = cargar_partes(config, tmp_path / "job", csv)
    assert len(trabajos) == 1
    assert trabajos[0].manifiesto.nombre_batch == "DP | BIT"


def test_un_batch_antiguo_sin_numero_se_reubica_y_no_se_reparte(tmp_path):
    csv = corrida(tmp_path)
    parte_original = comprobar_entrega(csv)[0]
    trabajo = Trabajo.preparar(
        AirVaultConfig(), tmp_path / "job", csv,
        "DP | BIT", parte=parte_original, paginas_por_batch=0,
    )
    trabajo.fijar_lote("003ANT")
    trabajo.manifiesto.csv_origen = str(
        Path("C:/Users/otro/Desktop/BITS/output")
        / csv.parent.parent.name / "datos" / csv.name
    )
    trabajo.manifiesto.pdf_origen = str(
        Path("C:/Users/otro/Desktop/BITS/output")
        / csv.parent.parent.name / parte_original.pdf.name
    )
    trabajo.guardar()

    retomados = preparar_partes(
        AirVaultConfig(), tmp_path / "job", csv,
        "DP | BIT", paginas_por_batch=1,
    )

    assert len(retomados) == 1
    assert retomados[0].manifiesto.nombre_batch == "DP | BIT"
    assert retomados[0].manifiesto.batch_id == "003ANT"
    assert Path(retomados[0].manifiesto.csv_origen) == csv.resolve()
    assert (
        Path(retomados[0].manifiesto.pdf_origen)
        == parte_original.pdf.resolve()
    )


def test_sin_manifiesto_no_hay_nada_que_retomar(tmp_path):
    csv = corrida(tmp_path)
    assert cargar_partes(AirVaultConfig(), tmp_path / "job", csv) == []


def test_un_trabajo_de_otra_ejecucion_no_se_retoma(tmp_path):
    """Seguir con el anterior escribiria los datos de una en el batch de otra."""
    config = AirVaultConfig()
    primera = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    segunda = corrida(tmp_path, "BITS 19 AUG 2026 06 10")
    preparar_partes(config, tmp_path / "job", primera, "DP | BIT")
    assert cargar_partes(config, tmp_path / "job", segunda) == []


def test_al_conectar_se_recuperan_manifiestos_subidos_y_pendientes(tmp_path):
    csv = corrida(tmp_path)
    raiz = tmp_path / "output" / "airvault"
    trabajo = Trabajo.preparar(
        AirVaultConfig(), raiz / "anterior", csv, "DP | BIT"
    )
    trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "ok")
    trabajo.fijar_lote("003ANT")

    encontrados = cargar_trabajos_pendientes(AirVaultConfig(), raiz)

    assert [t.manifiesto.batch_id for t in encontrados] == ["003ANT"]


def test_al_conectar_recupera_y_prioriza_los_batches_sin_subir(tmp_path):
    raiz = tmp_path / "output" / "airvault"
    csv_pendiente = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    csv_subido = corrida(tmp_path, "BITS 19 AUG 2026 06 10")
    pendiente = Trabajo.preparar(
        AirVaultConfig(), raiz / "pendiente", csv_pendiente,
        "DP | PENDIENTE",
    )
    subido = Trabajo.preparar(
        AirVaultConfig(), raiz / "subido", csv_subido, "DP | SUBIDO"
    )
    subido.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "ok")
    subido.fijar_lote("003SUB")

    encontrados = cargar_trabajos_pendientes(AirVaultConfig(), raiz)

    assert [trabajo.manifiesto.nombre_batch for trabajo in encontrados] == [
        pendiente.manifiesto.nombre_batch,
        subido.manifiesto.nombre_batch,
    ]
    assert estado_local(encontrados[0]).estado == SIN_SUBIR


def test_al_conectar_tambien_se_recupera_un_revisar_pendiente(tmp_path):
    csv = corrida(tmp_path)
    raiz = tmp_path / "output" / "airvault"
    trabajo = Trabajo.preparar(
        AirVaultConfig(), raiz / "revisar", csv, "DP | BIT REVISAR"
    )
    trabajo.manifiesto.solo_subir = True
    trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "ok")
    trabajo.fijar_lote("003REV")

    encontrados = cargar_trabajos_pendientes(AirVaultConfig(), raiz)

    assert [t.manifiesto.batch_id for t in encontrados] == ["003REV"]


def test_reiniciar_indexado_incompleto_conserva_la_subida(tmp_path):
    trabajo, _cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    trabajo.manifiesto.registros[0].estado = EstadoRegistro.ESCRITA
    trabajo.manifiesto.etapa("indexar").marcar(EstadoEtapa.HECHA, "ok")
    trabajo.manifiesto.etapa("verificar").marcar(
        EstadoEtapa.ERROR, "1/2 en Valid"
    )

    reiniciados = reiniciar_trabajos_incompletos([trabajo])

    assert reiniciados[0][1] == "indexar"
    assert trabajo.manifiesto.etapa("subir").estado is EstadoEtapa.HECHA
    assert trabajo.manifiesto.batch_id == "003SRO"
    assert trabajo.manifiesto.registros[0].estado is EstadoRegistro.PENDIENTE


def test_el_worker_reintenta_una_pagina_que_airvault_deja_amarilla(tmp_path):
    from app.gui.airvault_window import TrabajoAirVaultWorker

    class AmarillaUnaVez(ClienteFalso):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.intentos: dict[int, int] = {}

        def guardar_pagina(self, batch_id, numero, valores, estado,
                           pagina_siguiente=None):
            respuesta = super().guardar_pagina(
                batch_id, numero, valores, estado, pagina_siguiente
            )
            self.intentos[numero] = self.intentos.get(numero, 0) + 1
            if numero == 1 and self.intentos[numero] == 1:
                self.paginas[numero] = pagina(
                    numero, estado=3, valores=valores
                )
            return respuesta

    trabajo, base = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente = AmarillaUnaVez(
        paginas=base.paginas, lotes=base.lotes,
        picklist=base.picklist, page_count=2,
    )
    plan = trabajo.planificar(cliente)
    estado = {
        "cliente": cliente,
        "listos": [trabajo],
        "planes": {str(trabajo.carpeta): plan},
        "completar": False,
    }
    terminado: list[dict] = []
    worker = TrabajoAirVaultWorker("indexar", estado)
    worker._dormir = lambda _segundos: None
    worker.indexado.connect(terminado.append)

    worker._indexar()

    assert cliente.intentos == {1: 2, 2: 1}
    assert terminado[0]["validas"] == terminado[0]["total"] == 2


# ── subir sin quedarse esperando ───────────────────────────────────

class SesionFalsa:
    """Se traga la subida sin red."""


def test_worker_reenvia_la_fecha_si_airvault_no_la_conservo(tmp_path):
    from app.gui.airvault_window import TrabajoAirVaultWorker

    class PierdeFecha(ClienteFalso):
        def guardar_pagina(self, batch_id, numero, valores, estado, siguiente=None):
            respuesta = super().guardar_pagina(batch_id, numero, valores, estado, siguiente)
            if len(self.escrituras) == 1:
                guardados = dict(valores)
                guardados.pop(CAMPO_END_DATE)
                self.paginas[numero] = pagina(numero, estado=0, valores=guardados)
            return respuesta

    trabajo, base = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cli = PierdeFecha(paginas=base.paginas, picklist=base.picklist, page_count=2)
    plan = trabajo.planificar(cli)
    worker = TrabajoAirVaultWorker("indexar", {"tanda_hecha": True})
    worker._dormir = lambda _segundos: None
    datos = worker._ejecutar_indexado([trabajo], [plan], cli)
    assert datos["validas"] == datos["total"] == 2
    assert [p for p, _v, _e in cli.escrituras] == [1, 2, 1]
    assert cli.escrituras[-1][1][CAMPO_END_DATE] == "08/12/2026"


def test_worker_completa_aunque_el_mapa_tarde_en_ver_la_pagina_verde(tmp_path):
    """Recién verificado, el mapa del batch puede traer aún una amarilla.

    Antes el cierre se intentaba una sola vez y el batch quedaba en la cola
    hasta la siguiente comprobación periódica.
    """
    from app.airvault.client import PaginaDelLote
    from app.gui.airvault_window import ESPERAS_CIERRE, TrabajoAirVaultWorker

    class MapaAtrasado(ClienteFalso):
        mapas = 0

        def paginas_del_lote(self, batch_id):
            self.mapas += 1
            paginas = super().paginas_del_lote(batch_id)
            if self.mapas > 1:
                return paginas
            return [
                PaginaDelLote(p.pagina, 3, p.inicio_documento, p.borrada)
                if p.pagina == 1 else p
                for p in paginas
            ]

    trabajo, base = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cli = MapaAtrasado(
        paginas=base.paginas, lotes=base.lotes, picklist=base.picklist,
        page_count=2,
    )
    plan = trabajo.planificar(cli)
    worker = TrabajoAirVaultWorker("indexar", {"tanda_hecha": True})
    dormidas: list = []
    worker._dormir = dormidas.append

    datos = worker._ejecutar_indexado([trabajo], [plan], cli, completar=True)

    assert datos["validas"] == datos["total"] == 2
    assert cli.completados == ["003SRO"]
    assert [cierre.completado for _t, cierre in datos["cierres"]] == [True]
    assert dormidas == [ESPERAS_CIERRE[0]]


def test_worker_aplica_completar_si_se_activa_mientras_escribe(tmp_path):
    from app.gui.airvault_window import TrabajoAirVaultWorker

    trabajo, base = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente = ClienteFalso(
        paginas=base.paginas, lotes=base.lotes, picklist=base.picklist,
        page_count=2,
    )
    plan = trabajo.planificar(cliente)
    estado = {"tanda_hecha": True, "completar": False}
    worker = TrabajoAirVaultWorker("indexar", estado)

    def activar_al_verificar(texto, _hechas, _total):
        if texto == "Verificando batches":
            estado["completar"] = True

    worker._avisar = activar_al_verificar
    datos = worker._ejecutar_indexado(
        [trabajo], [plan], cliente, completar=False
    )

    assert cliente.completados == ["003SRO"]
    assert [cierre.completado for _t, cierre in datos["cierres"]] == [True]


class _ManifiestoDeTanda:
    """Lo minimo que miran el reintento y el cierre de una tanda."""

    def __init__(self) -> None:
        self.verificado = False
        self.solo_subir = False

    def etapa_hecha(self, nombre: str) -> bool:
        return nombre == "verificar" and self.verificado

    def bitacoras(self) -> list:
        return [1, 2]


def _tanda_con_un_batch_amarillo(tmp_path, monkeypatch):
    """Dos batches: el primero queda entero en verde y el segundo no."""
    from app.airvault.flujo import ResultadoCompletar
    from app.airvault.indexer import Resultado

    verde = SimpleNamespace(carpeta=tmp_path / "verde", manifiesto=_ManifiestoDeTanda())
    amarillo = SimpleNamespace(carpeta=tmp_path / "amarillo", manifiesto=_ManifiestoDeTanda())
    llamadas: dict[str, list] = {"planificar": [], "completar": [], "verificar": []}

    def verificar(trabajos, _cliente, **_kwargs):
        trabajo, = trabajos
        llamadas["verificar"].append(trabajo)
        if trabajo is verde:
            trabajo.manifiesto.verificado = True
            return 2, 2, []
        return 1, 2, ["pagina 2: faltan datos obligatorios en AirVault: End Date"]

    def planificar(trabajos, _cliente, **_kwargs):
        llamadas["planificar"].append(list(trabajos))
        return [(None, SimpleNamespace(resolutor=None)) for _t in trabajos]

    def completar(trabajos, _cliente, **_kwargs):
        llamadas["completar"].append(list(trabajos))
        return [(t, ResultadoCompletar(True, [], 2, "ok")) for t in trabajos]

    monkeypatch.setattr(
        "app.airvault.flujo.indexar_partes", lambda *a, **k: Resultado()
    )
    monkeypatch.setattr("app.airvault.flujo.verificar_partes", verificar)
    monkeypatch.setattr("app.airvault.flujo.planificar_partes", planificar)
    monkeypatch.setattr("app.airvault.flujo.completar_partes", completar)
    monkeypatch.setattr("app.airvault.flujo.cerrar_partes", lambda *a, **k: None)
    return verde, amarillo, llamadas


def test_worker_completa_el_batch_verde_aunque_otro_quede_amarillo(
    tmp_path, monkeypatch
):
    """Una pagina amarilla de un batch no deja sin cerrar a los demas."""
    from app.gui.airvault_window import TrabajoAirVaultWorker

    verde, amarillo, llamadas = _tanda_con_un_batch_amarillo(
        tmp_path, monkeypatch
    )
    worker = TrabajoAirVaultWorker("indexar", {"tanda_hecha": True})
    worker._dormir = lambda _s: None
    planes = [(None, SimpleNamespace(resolutor=None))] * 2

    datos = worker._ejecutar_indexado(
        [verde, amarillo], planes, object(), completar=True
    )

    assert datos["incompleto"]
    assert llamadas["completar"] == [[verde]]
    # El verde ya confirmado no se vuelve a leer ni a escribir.
    assert all(tanda == [amarillo] for tanda in llamadas["planificar"])
    assert llamadas["verificar"].count(verde) == 1


def test_indexar_tambien_cierra_los_batches_ya_verificados(
    tmp_path, monkeypatch
):
    """Un batch pendiente de escribir no retiene el cierre de los verificados."""
    from app.gui.airvault_window import TrabajoAirVaultWorker

    verde, amarillo, llamadas = _tanda_con_un_batch_amarillo(
        tmp_path, monkeypatch
    )
    verde.manifiesto.verificado = True
    estado = {
        "tanda_hecha": True, "cliente": object(), "completar": True,
        "listos": [amarillo],
        "planes": {str(amarillo.carpeta): (None, SimpleNamespace(resolutor=None))},
        "completar_tambien": [verde],
    }
    worker = TrabajoAirVaultWorker("indexar", estado)
    worker._dormir = lambda _s: None
    emitidos: list = []
    worker.indexado.connect(emitidos.append)

    worker._indexar()

    assert llamadas["completar"] == [[verde]]
    assert [t for t, _cierre in emitidos[0]["cierres"]] == [verde]
    assert "completar_tambien" not in estado


@pytest.mark.parametrize("casilla,acotado,esperado", [
    (True, False, True),    # automatico con la casilla marcada: cierra
    (False, False, False),  # automatico con la casilla desmarcada: no cierra
    (False, True, True),    # orden del menu sobre esa fila: es explicita
])
def test_el_cierre_automatico_obedece_a_la_casilla(
    tmp_path, monkeypatch, casilla, acotado, esperado
):
    from app.gui.airvault_window import TrabajoAirVaultWorker

    verde, _amarillo, llamadas = _tanda_con_un_batch_amarillo(
        tmp_path, monkeypatch
    )
    estado = {
        "tanda_hecha": True, "completar": casilla,
        "por_completar": [verde], "completar_acotado": acotado,
    }
    worker = TrabajoAirVaultWorker("completar", estado)
    monkeypatch.setattr(worker, "_conectar", lambda: object())

    worker._completar()

    assert (llamadas["completar"] == [[verde]]) is esperado


def test_indexar_termina_cada_batch_antes_de_empezar_el_siguiente(
    tmp_path, monkeypatch
):
    """El primero se escribe, verifica y completa sin esperar a los demás."""
    from app.airvault.indexer import Resultado
    from app.gui.airvault_window import TrabajoAirVaultWorker

    primero = SimpleNamespace(carpeta=tmp_path / "uno", manifiesto=_ManifiestoDeTanda())
    segundo = SimpleNamespace(carpeta=tmp_path / "dos", manifiesto=_ManifiestoDeTanda())
    plan = (None, SimpleNamespace(resolutor=None))
    estado = {
        "tanda_hecha": True, "cliente": object(), "completar": True,
        "listos": [primero, segundo],
        "planes": {str(primero.carpeta): plan, str(segundo.carpeta): plan},
    }
    worker = TrabajoAirVaultWorker("indexar", estado)
    tandas: list[list] = []

    def ejecutar(trabajos, _planes, _cliente, completar=False):
        tandas.append(list(trabajos))
        resultado = Resultado(escritas=2)
        return {
            "resultado": resultado, "validas": 2, "total": 2, "lotes": 1,
            "cierres": [(trabajos[0], "cerrado")], "incompleto": False,
            "incluye_revision": False, "carpetas": [str(trabajos[0].carpeta)],
        }

    monkeypatch.setattr(worker, "_ejecutar_indexado", ejecutar)
    emitidos: list = []
    worker.indexado.connect(emitidos.append)

    worker._indexar()

    assert tandas == [[primero], [segundo]]
    datos, = emitidos
    assert (datos["validas"], datos["total"], datos["lotes"]) == (4, 4, 2)
    assert datos["resultado"].escritas == 4
    assert [t for t, _c in datos["cierres"]] == [primero, segundo]


def test_worker_retoma_el_indexado_cortado_y_solo_escribe_lo_que_falta(tmp_path):
    """Una escritura que se corta a la mitad se retoma sola, sin repetirla.

    Es lo que pasaba delante del usuario: la sesion se caia a mitad del
    batch, el bucle se rendia en el acto y el lote se quedaba a medias
    hasta que alguien volvia a pulsar. Ahora la pasada siguiente
    replanifica, ve en verde lo que ya quedo escrito y vuelve solo por las
    que faltan, asi que el batch termina y se puede cerrar.
    """
    from app.airvault.session import ErrorDeConexion
    from app.gui.airvault_window import TrabajoAirVaultWorker

    class SeCortaUnaVez(ClienteFalso):
        cortes = 0

        def guardar_pagina(self, batch_id, pagina, valores, estado,
                           pagina_siguiente=None):
            if pagina == 2 and not self.cortes:
                self.cortes += 1
                raise ErrorDeConexion("se corto la sesion a mitad del batch")
            return super().guardar_pagina(
                batch_id, pagina, valores, estado, pagina_siguiente
            )

    trabajo, base = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cli = SeCortaUnaVez(
        paginas=base.paginas, lotes=base.lotes, picklist=base.picklist,
        page_count=2,
    )
    plan = trabajo.planificar(cli)
    worker = TrabajoAirVaultWorker("indexar", {"tanda_hecha": True})
    worker._dormir = lambda _segundos: None

    datos = worker._ejecutar_indexado([trabajo], [plan], cli, completar=True)

    assert datos["validas"] == datos["total"] == 2
    # La 1 se escribe una sola vez: la segunda pasada ya la ve en verde y no
    # la vuelve a tocar. Solo se reescribe la 2, que es la que falto.
    assert [p for p, _v, _e in cli.escrituras] == [1, 2]
    assert cli.completados == ["003SRO"]


# ── la orden de subir dada a mano ──────────────────────────────────
#
# La automatizacion es prudente a proposito: ante la duda espera, cuenta
# revisiones y no manda nada. Quien tiene Web Index delante sabe mas que
# ella, y cuando dice «sube» el programa obedece en el acto. Lo unico que
# no cede es lo irreversible: publicar dos veces la misma bitacora.

def test_la_orden_de_subir_a_mano_no_espera_a_la_comprobacion(
    tmp_path, monkeypatch
):
    """La regla automatica dice «espera»; la persona dice «sube». Manda ella."""
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = []
    subidas: list[str] = []

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        subidas.append(self.manifiesto.nombre_batch)
        self.manifiesto.batch_id = "003NUEVO"

    monkeypatch.setattr(Trabajo, "subir", subir)

    # Por su cuenta el programa no lo mandaria: lleva una revision de tres.
    assert comprobar_partes([trabajo], cliente)[0].estado == PROCESANDO

    subir_partes(
        [trabajo],
        SesionFalsa(),
        cliente=cliente,
        forzados=[str(trabajo.carpeta)],
    )

    assert subidas == [trabajo.manifiesto.nombre_batch]


def test_la_orden_a_mano_lee_la_cola_una_vez_y_no_abre_ningun_batch(
    tmp_path, monkeypatch
):
    """Rapida no es a ciegas: se mira la cola una vez y se sube.

    Lo caro de la comprobacion completa nunca fue el listado, sino abrir
    cada batch y contrastar su contenido. Eso es lo que aqui no se hace.
    """
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = []
    monkeypatch.setattr(
        Trabajo, "subir",
        lambda self, *a, **k: setattr(self.manifiesto, "batch_id", "003NUEVO"),
    )

    cliente.filtros.clear()
    subir_partes(
        [trabajo],
        SesionFalsa(),
        cliente=cliente,
        forzados=[str(trabajo.carpeta)],
    )

    # Una sola lectura, y sin filtro: un Empty-Batch no responde a ningun
    # nombre, asi que filtrar en el servidor lo dejaria fuera.
    assert cliente.filtros == [""]
    assert cliente.abiertos == []
    assert cliente.lecturas == []


def test_la_orden_a_mano_reconoce_su_carga_publicada_sin_nombre(tmp_path):
    """El caso que mas se da, y el que la persona no puede ver.

    AirVault publica lo cargado como ``Empty-Batch`` aunque Quick Upload
    reciba el nombre. Quien mira Web Index no encuentra su titulo, da la
    orden, y sin esta comprobacion se crearia la segunda copia.
    """
    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = [
        lote("003VIEJO", "DP | LO DE ANTES", 9),
        lote("003SINNOMBRE", "Empty-Batch", 2),
    ]
    subidas: list[str] = []

    class SesionQueCuenta(SesionFalsa):
        def post(self, *args, **kwargs):
            subidas.append(args[0] if args else "")

    subir_partes(
        [trabajo],
        SesionQueCuenta(),
        cliente=cliente,
        forzados=[str(trabajo.carpeta)],
    )

    assert subidas == []
    assert trabajo.manifiesto.batch_id == "003SINNOMBRE"


def test_un_empty_batch_que_ya_estaba_no_frena_la_orden(tmp_path, monkeypatch):
    """La foto de la cola distingue el propio del ajeno.

    Sin ella, cualquier batch sin nombre del tamaño justo dejaria la orden
    bloqueada para siempre, que es lo contrario de obedecer.
    """
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.manifiesto.lotes_previos = ["003VIEJO", "003AJENO"]
    trabajo.guardar()
    cliente.lotes = [
        lote("003VIEJO", "DP | LO DE ANTES", 9),
        lote("003AJENO", "Empty-Batch", 2),
    ]
    subidas: list[str] = []

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        subidas.append(self.manifiesto.nombre_batch)
        self.manifiesto.batch_id = "003NUEVO"

    monkeypatch.setattr(Trabajo, "subir", subir)

    subir_partes(
        [trabajo],
        SesionFalsa(),
        cliente=cliente,
        forzados=[str(trabajo.carpeta)],
    )

    assert subidas == [trabajo.manifiesto.nombre_batch]


def test_varios_empty_batch_posibles_no_eligen_ninguno(tmp_path):
    """Escribir en el batch equivocado es peor que parar y preguntar."""
    from app.airvault.flujo import ya_esta_en_airvault

    trabajo, cliente = trabajo_subido(tmp_path)
    cliente.lotes = [
        lote("003UNO", "Empty-Batch", 2),
        lote("003DOS", "Empty-Batch", 2),
    ]

    with pytest.raises(LoteAmbiguo, match="podrían ser"):
        ya_esta_en_airvault(trabajo, cliente)


def test_sin_haber_subido_nunca_ningun_empty_batch_es_propio(tmp_path):
    """Una primera carga no puede estar ya publicada sin nombre."""
    from app.airvault.flujo import ya_esta_en_airvault

    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(
        AirVaultConfig(), tmp_path / "job", csv, "DP | BIT 18 AUG 2026 05 42"
    )
    cliente = ClienteFalso(lotes=[lote("003AJENO", "Empty-Batch", 2)])

    assert ya_esta_en_airvault(trabajo, cliente) is None


def test_la_orden_a_mano_no_publica_dos_veces_la_misma_bitacora(tmp_path):
    """Lo unico que no cede ante la orden: AirVault ya lo publico.

    Entre la ultima comprobacion y el clic el servidor pudo sacar el batch
    de su cola. Mandarlo otra vez dejaria la misma bitacora publicada por
    duplicado, y eso hay que deshacerlo a mano en AirVault.
    """
    trabajo, cliente = trabajo_subido(tmp_path)
    subidas: list[str] = []

    class SesionQueCuenta(SesionFalsa):
        def post(self, *args, **kwargs):
            subidas.append(args[0] if args else "")

    subir_partes(
        [trabajo],
        SesionQueCuenta(),
        cliente=cliente,
        forzados=[str(trabajo.carpeta)],
    )

    assert subidas == []
    # Y el ID que la orden habia olvidado queda recuperado.
    assert trabajo.manifiesto.batch_id == "003SRO"


def test_la_orden_a_mano_de_una_parte_no_arrastra_a_las_demas(
    tmp_path, monkeypatch
):
    """Elegir una fila no convierte la ejecucion entera en una orden."""
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    primero, segundo, _tercero = trabajos
    cliente = ClienteFalso()
    subidas: list[str] = []

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        subidas.append(self.manifiesto.nombre_batch)
        self.manifiesto.batch_id = f"ID-{len(subidas)}"

    monkeypatch.setattr(Trabajo, "subir", subir)

    subir_partes(
        trabajos,
        SesionFalsa(),
        cliente=cliente,
        forzados=[str(segundo.carpeta)],
    )

    # Todas acaban subiendo (ninguna estaba en AirVault), pero solo la
    # elegida se salto la comprobacion completa.
    assert set(subidas) == {
        trabajo.manifiesto.nombre_batch for trabajo in trabajos
    }
    assert primero.manifiesto.nombre_batch in cliente.filtros


def test_cada_faltante_se_busca_se_sube_y_el_proceso_continua(
    tmp_path, monkeypatch,
):
    """Un ausente no detiene la cola ni exige otro clic para cargarlo."""
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    primero, segundo, tercero = trabajos
    eventos: list[tuple[str, str]] = []

    class ClienteConHallazgoTardio(ClienteFalso):
        def __init__(self):
            super().__init__()
            self.consultas: dict[str, int] = {}

        def listar_lotes(self, filtro=""):
            eventos.append(("buscar", filtro))
            self.consultas[filtro] = self.consultas.get(filtro, 0) + 1
            if (
                filtro == primero.manifiesto.nombre_batch
                and self.consultas[filtro] >= 2
            ):
                return [lote("003TAR", filtro, 2)]
            return []

    cliente = ClienteConHallazgoTardio()
    subidas: list[str] = []

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        nombre = self.manifiesto.nombre_batch
        eventos.append(("subir", nombre))
        subidas.append(nombre)
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "ok")
        self.manifiesto.batch_id = f"ID-{len(subidas)}"
        self.guardar()

    monkeypatch.setattr(Trabajo, "subir", subir)

    subir_partes(trabajos, SesionFalsa(), cliente=cliente)

    assert primero.manifiesto.batch_id == "003TAR"
    assert subidas == [
        segundo.manifiesto.nombre_batch,
        tercero.manifiesto.nombre_batch,
    ]
    for trabajo in (segundo, tercero):
        carga = eventos.index(("subir", trabajo.manifiesto.nombre_batch))
        assert eventos[carga - 1] == (
            "buscar", trabajo.manifiesto.nombre_batch
        )


def test_subir_confirma_todos_y_carga_solo_la_division_pendiente(
    tmp_path, monkeypatch
):
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    for indice, trabajo in enumerate(trabajos):
        if indice != 1:
            trabajo.manifiesto.etapa("subir").marcar(
                EstadoEtapa.HECHA, "subido"
            )
        trabajo.manifiesto.batch_id = f"ID-VIEJO-{indice}"
        trabajo.guardar()
    cliente = ClienteFalso(lotes=[
        lote("003PRI", "DP | BIT", 2),
        lote("003REV", "DP | BIT REVISAR", 2),
    ])
    subidas = []
    eventos: list[tuple[str, str]] = []

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        subidas.append(self.manifiesto.nombre_batch)
        eventos.append(("subir", self.manifiesto.nombre_batch))
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "subido")
        cliente.lotes.append(
            lote("003DOS", self.manifiesto.nombre_batch, 2)
        )

    def encontrado(trabajo, _todos):
        eventos.append(("encontrado", trabajo.manifiesto.nombre_batch))

    monkeypatch.setattr(Trabajo, "subir", subir)

    subir_partes(
        trabajos,
        SesionFalsa(),
        cliente=cliente,
        al_encontrar=encontrado,
    )

    assert subidas == ["DP | BIT -2"]
    # Los ya confirmados pasan al indexado antes de la carga pendiente.
    assert eventos.index(("encontrado", "DP | BIT")) < eventos.index(
        ("subir", "DP | BIT -2")
    )
    assert eventos.index(("encontrado", "DP | BIT REVISAR")) < eventos.index(
        ("subir", "DP | BIT -2")
    )
    assert eventos[-1] == ("encontrado", "DP | BIT -2")
    assert trabajos[0].manifiesto.batch_id == "003PRI"
    assert trabajos[2].manifiesto.batch_id == "003REV"


def test_no_repite_un_batch_que_quick_upload_ya_confirmo_mientras_procesa(
    tmp_path, monkeypatch
):
    """La demora entre Quick Upload y Web Index no autoriza un duplicado."""
    trabajo = _trabajos_principal_division_y_revisar(tmp_path)[1]
    trabajo.manifiesto.etapa("subir").marcar(
        EstadoEtapa.HECHA, "division-02.pdf"
    )
    trabajo.guardar()
    subidas = []
    avisos = []
    monkeypatch.setattr(
        Trabajo, "subir", lambda *args, **kwargs: subidas.append(True)
    )

    subir_partes(
        [trabajo], SesionFalsa(), cliente=ClienteFalso(),
        avisar=lambda texto, hechas, total: avisos.append(texto),
    )

    assert subidas == []
    assert trabajo.manifiesto.etapa("subir").estado is EstadoEtapa.HECHA
    assert any("1 sigue procesándose" in texto for texto in avisos)


def test_subir_de_nuevo_reenvia_el_que_no_aparecio_durante_mucho_tiempo(
    tmp_path, monkeypatch
):
    """El segundo clic es una recuperación expresa, no un sondeo infinito."""
    trabajo = _trabajos_principal_division_y_revisar(tmp_path)[1]
    subida = trabajo.manifiesto.etapa("subir")
    subida.marcar(EstadoEtapa.HECHA, "division-02.pdf")
    subida.actualizada = "2020-01-01T00:00:00"
    trabajo.manifiesto.intentos_identificacion = 3
    trabajo.manifiesto.espera_reenvio_desde = "2020-01-01T00:00:00"
    trabajo.guardar()
    cliente = ClienteFalso()
    subidas = []

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        subidas.append(self.manifiesto.nombre_batch)
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "reenviado")
        cliente.lotes.append(
            lote("003NUE", self.manifiesto.nombre_batch, 2)
        )
        self.guardar()

    monkeypatch.setattr(Trabajo, "subir", subir)

    subir_partes(
        [trabajo],
        SesionFalsa(),
        cliente=cliente,
        forzados=[str(trabajo.carpeta)],
    )

    assert subidas == ["DP | BIT -2"]
    assert trabajo.manifiesto.batch_id == "003NUE"


def test_una_carga_perdida_se_avisa_pero_no_se_reenvia_sola(
    tmp_path, monkeypatch
):
    """Nadie manda el mismo archivo dos veces sin que lo pidan.

    El programa no distingue una carga perdida de una cola lenta, y por ese
    margen es por donde aparecen dos copias del mismo batch el dia que
    AirVault las publica todas. Se avisa, se sigue mirando la cola, y subir
    otra vez es la orden expresa de quien mira Web Index.
    """
    from app.airvault.flujo import espera_para_darla_por_perdida

    perdida = _trabajos_principal_division_y_revisar(tmp_path)[1]
    perdida.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "x.pdf")
    perdida.manifiesto.intentos_identificacion = 3
    perdida.manifiesto.espera_reenvio_desde = "2020-01-01T00:00:00"
    perdida.guardar()

    # La espera es la configurada, sin multiplicadores: lo unico que vence
    # con ella es el aviso.
    assert (
        espera_para_darla_por_perdida(perdida)
        == perdida.config.espera_reenvio_s
    )
    estado = EstadoParte(perdida, BUSCANDO, "no aparecio")
    assert subida_estancada(perdida, espera_para_darla_por_perdida(perdida))
    assert subida_perdida(estado)
    # Darla por perdida avisa; no la pone en la lista de lo que sale solo.
    assert partes_por_subir([estado]) == []

    cliente = ClienteFalso()
    subidas = []
    monkeypatch.setattr(
        Trabajo, "subir", lambda *args, **kwargs: subidas.append(True)
    )
    monkeypatch.setattr(
        Trabajo,
        "descubrir",
        lambda self, *a, **k: setattr(self.manifiesto, "batch_id", "003NUE"),
    )

    subir_partes([perdida], SesionFalsa(), cliente=cliente)
    assert subidas == []

    # La orden expresa si la manda.
    subir_partes(
        [perdida], SesionFalsa(), cliente=cliente,
        forzados=[str(perdida.carpeta)],
    )
    assert subidas == [True]


def test_lo_que_falta_por_subir_sale_de_una_sola_regla(tmp_path):
    """La ventana y el boton tienen que decidir exactamente lo mismo.

    Y lo que sale solo es unicamente lo que nunca llego a Quick Upload: el
    estancado ya se subio una vez, asi que se avisa de el y lo manda quien
    mire Web Index.
    """
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    sin_subir, estancado, listo = trabajos
    subida = estancado.manifiesto.etapa("subir")
    subida.marcar(EstadoEtapa.HECHA, "division-02.pdf")
    estancado.manifiesto.intentos_identificacion = 3
    estancado.manifiesto.espera_reenvio_desde = "2020-01-01T00:00:00"
    estancado.guardar()
    listo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "revisar.pdf")
    listo.guardar()
    cliente = ClienteFalso()
    cliente.lotes.append(
        lote("003LIS", listo.manifiesto.nombre_batch, 2)
    )

    estados = comprobar_partes(trabajos, cliente)

    assert [t.carpeta for t in partes_por_subir(estados)] == [
        sin_subir.carpeta
    ]
    assert subida_perdida(
        next(e for e in estados if e.trabajo is estancado), trabajos
    )


def test_una_carga_vieja_que_no_esta_en_la_cola_se_da_por_perdida_enseguida(
    tmp_path, monkeypatch,
):
    """La búsqueda amplia se hace igual; lo que no se hace es repetirla.

    Recorrer la cola entera (por nombre visible, por nombre embebido, por
    páginas y por Log Page Number) y no encontrar nada no deja ningún sitio
    donde la carga pueda estar escondida. Si además el archivo se subió hace
    mucho, no viene en camino: se volvió a borrar o nunca entró. Repetir esa
    misma búsqueda dos veces más y esperar después media hora es tiempo
    perdido.

    Darla por perdida es lo que avisa; mandarla otra vez sigue siendo una
    orden a mano.
    """
    trabajo = _trabajos_principal_division_y_revisar(tmp_path)[1]
    subida = trabajo.manifiesto.etapa("subir")
    subida.marcar(EstadoEtapa.HECHA, "division-02.pdf")
    subida.actualizada = "2020-01-01T00:00:00"
    trabajo.guardar()
    cliente = ClienteFalso()
    subidas = []
    monkeypatch.setattr(
        Trabajo, "subir", lambda *args, **kwargs: subidas.append(True)
    )
    monkeypatch.setattr(
        Trabajo,
        "descubrir",
        lambda self, *a, **k: setattr(self.manifiesto, "batch_id", "003NUE"),
    )

    estado = comprobar_partes([trabajo], cliente)[0]

    assert estado.estado == BUSCANDO
    assert "ningún nombre" in estado.detalle
    assert trabajo.manifiesto.busquedas_amplias_sin_hallar == 1
    assert subida_perdida(estado)
    assert partes_por_subir([estado]) == []

    subir_partes([trabajo], SesionFalsa(), cliente=cliente)
    assert subidas == []

    subir_partes(
        [trabajo], SesionFalsa(), cliente=cliente,
        forzados=[str(trabajo.carpeta)],
    )
    assert subidas == [True]
    # Reenviada a mano, la cuenta vuelve a cero: la carga nueva empieza de
    # nuevo con todo el margen de identificacion por delante.
    assert trabajo.manifiesto.busquedas_amplias_sin_hallar == 0


def test_cada_carga_se_confirma_antes_de_mandar_la_siguiente(
    tmp_path, monkeypatch
):
    """AirVault junta cargas seguidas: solo una puede estar sin nombre."""
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    cliente = ClienteFalso()
    eventos: list[tuple[str, str]] = []

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        eventos.append(("subir", self.manifiesto.nombre_batch))
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "ok")

    def descubrir(
        self, cliente, esperar=True, dormir=None, avisar=None, cache=None
    ):
        eventos.append(("confirmar", self.manifiesto.nombre_batch))
        self.manifiesto.batch_id = f"ID-{len(eventos)}"
        return self.manifiesto.batch_id

    def actualizar(trabajos_actualizados):
        eventos.append(("actualizar", str(len(trabajos_actualizados))))

    monkeypatch.setattr(Trabajo, "subir", subir)
    monkeypatch.setattr(Trabajo, "descubrir", descubrir)

    subir_partes(
        trabajos, SesionFalsa(), cliente=cliente,
        al_finalizar_subidas=actualizar,
    )

    assert eventos == [
        ("subir", "DP | BIT"),
        ("actualizar", "3"),
        ("confirmar", "DP | BIT"),
        ("subir", "DP | BIT -2"),
        ("actualizar", "3"),
        ("confirmar", "DP | BIT -2"),
        ("subir", "DP | BIT REVISAR"),
        ("actualizar", "3"),
        ("confirmar", "DP | BIT REVISAR"),
    ]


def test_la_carga_siguiente_ya_ve_nombrado_el_batch_anterior(
    tmp_path, monkeypatch
):
    """Solo un ``Empty-Batch`` propio puede estar en vuelo a la vez."""
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)[:2]
    cliente = ClienteFalso(lotes=[lote("003VIEJO", "DP | LO DE ANTES", 9)])
    fotos: list[list[str]] = []

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        self.manifiesto.lotes_previos = [
            actual.batch_id for actual in cliente.listar_lotes()
        ]
        fotos.append(list(self.manifiesto.lotes_previos))
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "ok")
        self.guardar()
        # AirVault publica la carga sin titulo, como hace de verdad.
        cliente.lotes.append(
            lote(f"003NUEVO{len(cliente.lotes)}", "Empty-Batch", 2)
        )

    def descubrir(self, cliente, esperar=True, dormir=None, avisar=None,
                  cache=None):
        conocidos = set(self.manifiesto.lotes_previos or [])
        nuevos = [
            actual for actual in cliente.listar_lotes()
            if actual.batch_id not in conocidos
        ]
        assert len(nuevos) == 1, "hay mas de una carga sin identificar"
        return self.anotar_lote(cliente, nuevos[0])

    monkeypatch.setattr(Trabajo, "subir", subir)
    monkeypatch.setattr(Trabajo, "descubrir", descubrir)

    subir_partes(trabajos, SesionFalsa(), cliente=cliente)

    assert [trabajo.manifiesto.batch_id for trabajo in trabajos] == [
        "003NUEVO1", "003NUEVO2",
    ]
    assert cliente.renombrados == [
        ("003NUEVO1", "DP | BIT"),
        ("003NUEVO2", "DP | BIT -2"),
    ]
    # La segunda carga salio con el primer batch ya en su foto de la cola.
    assert "003NUEVO1" in fotos[1]


def test_varios_empty_batch_del_mismo_tamano_conservan_su_id(
    tmp_path, monkeypatch
):
    """Las instantaneas anidadas se concilian despues de subirlos todos."""
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    cliente = ClienteFalso(lotes=[
        lote("003VIEJO", "DP | LO DE ANTES", 9),
    ])
    ids = iter(("003UNO", "003DOS", "003TRE"))

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        self.manifiesto.lotes_previos = [
            actual.batch_id for actual in cliente.listar_lotes()
        ]
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "ok")
        batch_id = next(ids)
        cliente.lotes.append(lote(batch_id, "Empty-Batch", 2))
        cliente.paginas_por_lote[batch_id] = paginas_ocr(
            self, self.manifiesto.nombre_batch
        )
        self.guardar()

    monkeypatch.setattr(Trabajo, "subir", subir)

    subir_partes(
        trabajos, SesionFalsa(), cliente=cliente, dormir=lambda _s: None,
    )

    assert [trabajo.manifiesto.batch_id for trabajo in trabajos] == [
        "003UNO", "003DOS", "003TRE",
    ]
    assert all(
        trabajo.manifiesto.etapa_hecha("descubrir")
        for trabajo in trabajos
    )
    assert cliente.lecturas == [1, 1, 1]


def test_cada_hallazgo_se_publica_antes_de_subir_el_siguiente(
    tmp_path, monkeypatch
):
    """El indexado de un batch confirmado no espera a las cargas restantes."""
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    cliente = ClienteFalso()
    eventos: list[tuple[str, str]] = []

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        eventos.append(("subir", self.manifiesto.nombre_batch))
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "ok")

    def descubrir(
        self, cliente, esperar=True, dormir=None, avisar=None, cache=None
    ):
        self.manifiesto.batch_id = f"ID-{self.manifiesto.parte}"
        return self.manifiesto.batch_id

    def encontrado(trabajo, todos):
        eventos.append(("id", trabajo.manifiesto.nombre_batch))

    monkeypatch.setattr(Trabajo, "subir", subir)
    monkeypatch.setattr(Trabajo, "descubrir", descubrir)

    subir_partes(
        trabajos, SesionFalsa(), cliente=cliente,
        al_encontrar=encontrado,
    )

    assert eventos == [
        ("subir", "DP | BIT"),
        ("id", "DP | BIT"),
        ("subir", "DP | BIT -2"),
        ("id", "DP | BIT -2"),
        ("subir", "DP | BIT REVISAR"),
        ("id", "DP | BIT REVISAR"),
    ]


def test_un_batch_que_falla_no_impide_subir_los_otros(tmp_path, monkeypatch):
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    intentados: list[str] = []

    def subir(self, _sesion, **_kwargs):
        nombre = self.manifiesto.nombre_batch
        intentados.append(nombre)
        if nombre == "DP | BIT -2":
            raise ErrorDeCorrida("fallo aislado")

    monkeypatch.setattr(Trabajo, "subir", subir)

    fallos = subir_partes(trabajos, SesionFalsa())

    assert intentados == [
        "DP | BIT",
        "DP | BIT -2",
        "DP | BIT REVISAR",
    ]
    assert [(t.manifiesto.nombre_batch, detalle) for t, detalle in fallos] == [
        ("DP | BIT -2", "fallo aislado")
    ]


def test_el_indexado_avanza_mientras_sube_el_siguiente_archivo(
    tmp_path, monkeypatch
):
    """Las cargas van de una en una; el indexado de lo confirmado, en paralelo."""
    from app.airvault.flujo import EstadoParte
    from app.gui.airvault_window import TrabajoAirVaultWorker

    trabajos = _trabajos_principal_division_y_revisar(tmp_path)[:2]
    cliente = ClienteFalso()
    eventos: list[tuple[str, str]] = []
    indexando = threading.Event()

    monkeypatch.setattr(
        "app.airvault.flujo.comprobar_entrega",
        lambda _csv: [SimpleNamespace(paginas=[1, 2])],
    )
    monkeypatch.setattr(
        "app.airvault.flujo.preparar_partes",
        lambda *args, **kwargs: trabajos,
    )
    monkeypatch.setattr(
        "app.airvault.flujo.comprobar_partes",
        lambda lotes, _cliente, avisar=None: [
            EstadoParte(lotes[0], LISTO, "2 paginas")
        ],
    )

    def subir_falso(lotes, _sesion, al_encontrar=None, **_kwargs):
        primero, segundo = lotes
        eventos.append(("subir", primero.manifiesto.nombre_batch))
        primero.manifiesto.batch_id = "ID-1"
        eventos.append(("encontrar", primero.manifiesto.nombre_batch))
        al_encontrar(primero, lotes)
        assert indexando.wait(1), "el indexado no arranco en paralelo"
        eventos.append(("subir", segundo.manifiesto.nombre_batch))
        segundo.manifiesto.batch_id = "ID-2"
        eventos.append(("encontrar", segundo.manifiesto.nombre_batch))
        al_encontrar(segundo, lotes)

    monkeypatch.setattr("app.airvault.flujo.subir_partes", subir_falso)
    estado = {
        "config": AirVaultConfig(), "csv": tmp_path / "corrida.csv",
        "raiz": tmp_path, "carpeta_job": tmp_path / "job",
        "nombre_lote": "DP | BIT", "paginas_por_batch": 100,
        "sesion": SesionFalsa(), "indexar_al_encontrar": True,
        "completar": False,
    }
    worker = TrabajoAirVaultWorker("subir", estado)
    monkeypatch.setattr(worker, "_conectar", lambda: cliente)
    monkeypatch.setattr(worker, "_cliente_paralelo", lambda _c: cliente)

    def indexar_falso(trabajo, _cliente, _raiz):
        eventos.append(("indexar", trabajo.manifiesto.nombre_batch))
        indexando.set()
        return {}

    monkeypatch.setattr(worker, "_indexar_batch_encontrado", indexar_falso)

    worker._subir()

    primer_indexado = eventos.index(
        ("indexar", trabajos[0].manifiesto.nombre_batch)
    )
    assert primer_indexado < eventos.index(
        ("subir", trabajos[1].manifiesto.nombre_batch)
    )
    assert ("indexar", trabajos[1].manifiesto.nombre_batch) in eventos


def test_un_batch_que_no_se_indexa_no_cancela_a_los_que_esperan(
    tmp_path, monkeypatch
):
    """El fallo de uno se informa y el siguiente se indexa igual."""
    from app.airvault.flujo import EstadoParte
    from app.gui.airvault_window import TrabajoAirVaultWorker

    trabajos = _trabajos_principal_division_y_revisar(tmp_path)[:2]
    cliente = ClienteFalso()
    indexados: list[str] = []

    monkeypatch.setattr(
        "app.airvault.flujo.comprobar_entrega",
        lambda _csv: [SimpleNamespace(paginas=[1, 2])],
    )
    monkeypatch.setattr(
        "app.airvault.flujo.preparar_partes", lambda *args, **kwargs: trabajos,
    )
    monkeypatch.setattr(
        "app.airvault.flujo.comprobar_partes",
        lambda lotes, _cliente, avisar=None: [
            EstadoParte(lotes[0], LISTO, "2 paginas")
        ],
    )

    def subir_falso(lotes, _sesion, al_encontrar=None, **_kwargs):
        for numero, trabajo in enumerate(lotes, start=1):
            trabajo.manifiesto.batch_id = f"ID-{numero}"
            al_encontrar(trabajo, lotes)

    monkeypatch.setattr("app.airvault.flujo.subir_partes", subir_falso)
    estado = {
        "config": AirVaultConfig(), "csv": tmp_path / "corrida.csv",
        "raiz": tmp_path, "carpeta_job": tmp_path / "job",
        "nombre_lote": "DP | BIT", "paginas_por_batch": 100,
        "sesion": SesionFalsa(), "indexar_al_encontrar": True,
        "completar": False,
    }
    worker = TrabajoAirVaultWorker("subir", estado)
    monkeypatch.setattr(worker, "_conectar", lambda: cliente)
    monkeypatch.setattr(worker, "_cliente_paralelo", lambda _c: cliente)

    def indexar_falso(trabajo, _cliente, _raiz):
        if trabajo is trabajos[0]:
            raise PermissionError(5, "Access is denied")
        indexados.append(trabajo.manifiesto.nombre_batch)
        return {}

    monkeypatch.setattr(worker, "_indexar_batch_encontrado", indexar_falso)
    subidos: list[dict] = []
    worker.subido.connect(subidos.append)

    worker._subir()

    assert indexados == [trabajos[1].manifiesto.nombre_batch]
    nombre, detalle = subidos[0]["fallos_indexado"][0]
    assert nombre == trabajos[0].manifiesto.nombre_batch
    assert "Access is denied" in detalle


def test_el_worker_sube_tambien_los_pendientes_de_otras_ejecuciones(
    tmp_path, monkeypatch,
):
    """La conexión amplía la tabla y esa lista ampliada dirige la carga."""
    from app.gui.airvault_window import TrabajoAirVaultWorker

    seleccionado, otro = _trabajos_principal_division_y_revisar(tmp_path)[:2]
    estado = {
        "config": AirVaultConfig(),
        "csv": Path(seleccionado.manifiesto.csv_origen),
        "raiz": tmp_path,
        "carpeta_job": tmp_path / "job",
        "nombre_lote": "DP | BIT",
        "paginas_por_batch": 100,
        "compresion": False,
        "sesion": SesionFalsa(),
    }
    recibidos = []
    monkeypatch.setattr(
        "app.airvault.flujo.comprobar_entrega",
        lambda _csv: [SimpleNamespace(paginas=[1, 2])],
    )
    monkeypatch.setattr(
        "app.airvault.flujo.preparar_partes",
        lambda *args, **kwargs: [seleccionado],
    )

    def subir_todos(trabajos, *_args, **_kwargs):
        recibidos.extend(trabajos)
        return []

    monkeypatch.setattr("app.airvault.flujo.subir_partes", subir_todos)
    worker = TrabajoAirVaultWorker("subir", estado)

    def conectar():
        estado["trabajos"] = [seleccionado, otro]
        return ClienteFalso()

    monkeypatch.setattr(worker, "_conectar", conectar)
    monkeypatch.setattr(worker, "_cliente_paralelo", lambda cliente: cliente)

    worker._subir()

    assert recibidos == [seleccionado, otro]


def test_un_automatico_con_titulo_revisar_no_se_sube(tmp_path, monkeypatch):
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)
    trabajos[0].manifiesto.nombre_batch = "DP | BIT REVISAR -1"
    subidas = []
    monkeypatch.setattr(
        Trabajo, "subir", lambda *args, **kwargs: subidas.append(True)
    )

    with pytest.raises(ErrorDeCorrida, match="marcado como automatico"):
        subir_partes(trabajos, SesionFalsa(), cliente=ClienteFalso())

    assert subidas == []


# ── dar el batch por terminado ──────────────────────────────────────

def mapa(*estados: int) -> list[PaginaDelLote]:
    """El batch tal como lo devuelve AirVault, una pagina por documento."""
    return [
        PaginaDelLote(pagina=n, estado=e, inicio_documento=n)
        for n, e in enumerate(estados, start=1)
    ]


def test_un_lote_entero_en_verde_se_cierra(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = mapa(0, 0)
    resultado = trabajo.completar(cliente)
    assert resultado.completado
    assert cliente.validaciones_batch == [("003SRO", [1, 2])]
    assert cliente.completados == ["003SRO"]
    assert trabajo.manifiesto.etapa_hecha("completar")


def test_una_pagina_fuera_de_verde_impide_cerrarlo_y_se_dice_cual(tmp_path):
    """Casi siempre es la fecha, que AirVault deja en «Need Correction».

    No se intenta cerrarlo: AirVault lo rechazaria igual, y el batch tiene
    que quedarse en la cola para que alguien arregle esa pagina.
    """
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = mapa(0, 3)
    resultado = trabajo.completar(cliente)
    assert not resultado.completado
    assert resultado.bloqueadas == [2]
    assert "2" in resultado.detalle
    assert cliente.completados == []


def test_una_pagina_sin_plantilla_tambien_impide_cerrar(tmp_path):
    """«No Template Match» tampoco es verde, aunque no sea amarillo.

    Medido en el batch 003SUS: sus trece paginas separadoras quedaron en
    estado 1 y AirVault las contaba igual que a una bitacora incompleta.
    """
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = mapa(1, 0)
    resultado = trabajo.completar(cliente)
    assert not resultado.completado
    assert resultado.bloqueadas == [1]


def trabajo_con_divisoria(tmp_path, batch_id: str = "003SRO"):
    """Una parte ya subida cuyo PDF lleva una divisoria delante."""
    csv = corrida(tmp_path, con_divisoria=True)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "entrega.pdf")
    trabajo.manifiesto.lotes_previos = ["003VIEJO"]
    trabajo.guardar()
    trabajo.fijar_lote(batch_id)
    cliente = ClienteFalso(
        lotes=[lote("003VIEJO", "DP | LO DE ANTES", 9),
               lote(batch_id, trabajo.manifiesto.nombre_batch, 3)],
        page_count=3,
    )
    return trabajo, cliente


def test_un_batch_con_separadores_ya_borrados_vuelve_a_quedar_listo(tmp_path):
    trabajo, cliente = trabajo_con_divisoria(tmp_path)
    trabajo.manifiesto.etapa("indexar").marcar(
        EstadoEtapa.HECHA,
        "escritas 2, omitidas 0, fallidas 0, separadores borrados 1",
    )
    trabajo.guardar()
    cliente.lotes = [lote("003SRO", trabajo.manifiesto.nombre_batch, 2)]
    cliente.page_count = 2

    parte, = comprobar_partes([trabajo], cliente)
    plan, _indexador = trabajo.planificar(cliente)

    assert parte.estado == LISTO
    assert plan.batch_id == "003SRO"


def test_las_divisorias_se_quitan_del_lote_para_poder_cerrarlo(tmp_path):
    """No son documentos: no tienen fecha ni avion que escribirles.

    Quitarlas es lo mismo que hace a mano quien indexa, y es lo unico que
    deja cerrar una entrega que se subio con sus separadores.
    """
    trabajo, cliente = trabajo_con_divisoria(tmp_path)
    cliente.mapa = mapa(1, 0, 0)
    resultado = trabajo.completar(cliente)
    assert resultado.completado
    assert resultado.quitadas == [1]
    assert cliente.borradas == [1]
    assert cliente.completados == ["003SRO"]


def test_una_divisoria_verde_tambien_se_quita_antes_de_completar(tmp_path):
    trabajo, cliente = trabajo_con_divisoria(tmp_path)
    cliente.mapa = mapa(0, 0, 0)

    resultado = trabajo.completar(cliente)

    assert resultado.completado
    assert resultado.quitadas == [1]
    assert cliente.borradas == [1]
    assert cliente.validaciones_batch == [("003SRO", [2, 3])]


def test_la_validacion_final_puede_devolver_una_pagina_a_amarillo(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = mapa(0, 0)
    cliente.estados_tras_validar = {2: 3}

    resultado = trabajo.completar(cliente)

    assert not resultado.completado
    assert resultado.bloqueadas == [2]
    assert "no se envio CompleteBatch" in resultado.detalle
    assert cliente.completados == []
    assert cliente.cerrados == ["003SRO"]
    assert (
        trabajo.manifiesto.etapa("verificar").estado
        is EstadoEtapa.ERROR
    )


def test_reiniciar_un_cierre_pendiente_no_reinicia_la_subida(tmp_path):
    """El PDF no vuelve a Quick Upload si Complete necesita otro intento."""
    trabajo, _cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    trabajo.manifiesto.etapa("verificar").marcar(
        EstadoEtapa.HECHA, "2/2 en Valid"
    )
    trabajo.manifiesto.etapa("completar").marcar(
        EstadoEtapa.OMITIDA, "validacion pendiente"
    )

    reiniciados = reiniciar_trabajos_incompletos([trabajo])

    assert reiniciados == [(trabajo, "completar")]
    assert trabajo.manifiesto.etapa("subir").estado is EstadoEtapa.HECHA
    assert trabajo.manifiesto.batch_id == "003SRO"


def test_con_una_bitacora_en_amarillo_no_se_toca_ninguna_divisoria(tmp_path):
    """El batch no se va a cerrar hoy; mas vale dejarlo como estaba."""
    trabajo, cliente = trabajo_con_divisoria(tmp_path)
    cliente.mapa = mapa(1, 0, 3)
    resultado = trabajo.completar(cliente)
    assert not resultado.completado
    assert resultado.bloqueadas == [3]
    assert cliente.borradas == []
    assert cliente.completados == []


def test_sin_permiso_para_quitar_paginas_el_lote_no_se_cierra(tmp_path):
    """Quitar paginas pide «Delete Batch Image», que no toda cuenta tiene."""
    trabajo, cliente = trabajo_con_divisoria(tmp_path)
    cliente.mapa = mapa(1, 0, 0)
    cliente.no_se_pueden_borrar = {1}
    resultado = trabajo.completar(cliente)
    assert not resultado.completado
    assert resultado.bloqueadas == [1]
    assert "Delete Batch Image" in resultado.detalle
    assert cliente.completados == []
    # Y el batch queda suelto: nadie se queda con el en la mano.
    assert cliente.cerrados == ["003SRO"]


def test_una_pagina_borrada_no_impide_cerrar_el_lote(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = [
        PaginaDelLote(pagina=1, estado=3, inicio_documento=1, borrada=True),
        PaginaDelLote(pagina=2, estado=0, inicio_documento=2),
    ]
    assert trabajo.completar(cliente).completado


def test_solo_cuenta_la_pagina_que_encabeza_cada_documento(tmp_path):
    """AirVault agrupa varias paginas en un documento; manda la primera."""
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = [
        PaginaDelLote(pagina=1, estado=0, inicio_documento=1),
        PaginaDelLote(pagina=2, estado=3, inicio_documento=1),
    ]
    assert trabajo.completar(cliente).completado


def test_cerrar_el_lote_no_lo_deja_tomado(tmp_path):
    """Cerrado, ya no esta tomado: soltarlo despues seria pedir un imposible."""
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = mapa(0, 0)
    trabajo.completar(cliente)
    trabajo.cerrar(cliente)
    assert cliente.cerrados == []


def test_si_airvault_rechaza_el_cierre_el_lote_no_queda_bloqueado(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = mapa(0, 0)

    def no_se_deja(_batch_id):
        raise RuntimeError("AirVault dijo que no")

    cliente.completar_lote = no_se_deja
    with pytest.raises(RuntimeError):
        trabajo.completar(cliente)
    assert cliente.cerrados == ["003SRO"]


def test_el_lote_de_revisar_no_se_cierra_nunca(tmp_path):
    """Se sube justo para que una persona lo mire; cerrarlo lo archivaria."""
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    trabajo.manifiesto.solo_subir = True
    assert completar_partes([trabajo], cliente) == []
    assert cliente.completados == []

    # La proteccion tambien vive en el trabajo, no solo en el recorrido de
    # varias partes: una llamada directa tampoco lo borra ni lo completa.
    resultado = trabajo.completar(cliente)
    assert not resultado.completado
    assert cliente.borradas == []
    assert cliente.completados == []


def test_un_lote_que_no_se_deja_cerrar_no_corta_a_los_demas(tmp_path):
    """Son batches distintos y lo escrito en cada uno ya esta escrito."""
    primero, cliente = trabajo_subido(tmp_path)
    segundo, _ = trabajo_subido(tmp_path / "otra")
    primero.fijar_lote("003SRO")
    segundo.fijar_lote("003SRP")
    cliente.mapa = mapa(0, 0)

    llamadas: list[str] = []

    def a_veces(batch_id):
        llamadas.append(batch_id)
        if batch_id == "003SRO":
            raise RuntimeError("AirVault dijo que no")
        return {"IsError": False}

    cliente.completar_lote = a_veces
    hechos = completar_partes([primero, segundo], cliente)
    assert [r.completado for _t, r in hechos] == [False, True]
    assert llamadas == ["003SRO", "003SRP"]


def _subida_en(trabajo, momento: datetime) -> None:
    """Deja el manifiesto diciendo cuando Quick Upload acepto el archivo."""
    etapa = trabajo.manifiesto.etapa("subir")
    etapa.marcar(EstadoEtapa.HECHA, "entrega.pdf")
    etapa.actualizada = momento.isoformat(timespec="seconds")
    trabajo.guardar()


def test_una_carga_que_rebasaron_las_siguientes_se_pierde_sin_esperar(
    tmp_path,
):
    """Si las partes posteriores ya se indexaron, la cola paso de largo.

    Es la senal que no depende del reloj: la carga no se perdio de camino a
    Quick Upload, se perdio en AirVault, y esperar mas no la va a publicar.
    """
    ahora = datetime.now()
    primera, segunda, tercera = _trabajos_principal_division_y_revisar(tmp_path)
    _subida_en(primera, ahora - timedelta(minutes=2))
    _subida_en(segunda, ahora - timedelta(minutes=1))
    _subida_en(tercera, ahora)
    primera.manifiesto.intentos_identificacion = 3
    primera.manifiesto.espera_reenvio_desde = ahora.isoformat(
        timespec="seconds"
    )
    primera.guardar()
    for trabajo in (segunda, tercera):
        trabajo.manifiesto.etapa("verificar").marcar(
            EstadoEtapa.HECHA, "2/2 en Valid"
        )
        trabajo.guardar()
    estados = [
        EstadoParte(primera, BUSCANDO, "no aparecio"),
        EstadoParte(segunda, INDEXADO, "2/2 en Valid"),
        EstadoParte(tercera, INDEXADO, "2/2 en Valid"),
    ]
    ejecucion = [primera, segunda, tercera]

    # Dos minutos no agotan la espera de media hora. Lo que la da por
    # perdida es que las dos que se enviaron despues ya esten indexadas.
    assert not subida_estancada(primera, primera.config.espera_reenvio_s)
    assert subida_rebasada(primera, ejecucion)
    assert subida_perdida(estados[0], ejecucion)
    # Se avisa de ella, pero sale a Quick Upload solo si alguien lo ordena.
    assert partes_por_subir(estados) == []


def test_la_carga_mas_reciente_no_se_pierde_por_las_anteriores(tmp_path):
    """Que las partes previas esten indexadas no dice nada de la ultima.

    La cola solo demuestra haber pasado de largo hacia delante: lo que se
    subio despues de esta carga. Al reves, la ultima todavía puede estar
    esperando su turno.
    """
    ahora = datetime.now()
    primera, segunda, tercera = _trabajos_principal_division_y_revisar(tmp_path)
    _subida_en(primera, ahora - timedelta(minutes=2))
    _subida_en(segunda, ahora - timedelta(minutes=1))
    _subida_en(tercera, ahora)
    for trabajo in (primera, segunda):
        trabajo.manifiesto.etapa("verificar").marcar(
            EstadoEtapa.HECHA, "2/2 en Valid"
        )
        trabajo.guardar()
    tercera.manifiesto.intentos_identificacion = 3
    tercera.manifiesto.espera_reenvio_desde = ahora.isoformat(
        timespec="seconds"
    )
    tercera.guardar()
    estados = [
        EstadoParte(primera, INDEXADO, "2/2 en Valid"),
        EstadoParte(segunda, INDEXADO, "2/2 en Valid"),
        EstadoParte(tercera, BUSCANDO, "no aparecio"),
    ]

    assert not subida_rebasada(tercera, [primera, segunda, tercera])
    assert partes_por_subir(estados) == []


def test_la_espera_se_cuenta_desde_que_se_subio_el_archivo(tmp_path):
    """Una ejecucion retomada dias despues no vuelve a esperar de cero.

    La marca de espera se estrena cuando el programa se da cuenta, que en
    una ejecucion retomada es ahora mismo. La fecha del batch es la que
    dice cuanto lleva sin publicarse de verdad.
    """
    trabajo = _trabajos_principal_division_y_revisar(tmp_path)[1]
    ahora = datetime.now()
    _subida_en(trabajo, ahora - timedelta(days=3))
    trabajo.manifiesto.intentos_identificacion = 3
    trabajo.manifiesto.espera_reenvio_desde = ahora.isoformat(
        timespec="seconds"
    )
    trabajo.guardar()

    assert subida_estancada(trabajo, trabajo.config.espera_reenvio_s)
    assert subida_perdida(EstadoParte(trabajo, BUSCANDO, "no aparecio"))


def test_una_subida_reciente_sigue_esperando_su_turno(tmp_path):
    """AirVault tarda: media hora de cola no es una carga perdida."""
    trabajo = _trabajos_principal_division_y_revisar(tmp_path)[1]
    ahora = datetime.now()
    _subida_en(trabajo, ahora - timedelta(minutes=3))
    trabajo.manifiesto.intentos_identificacion = 3
    trabajo.manifiesto.espera_reenvio_desde = ahora.isoformat(
        timespec="seconds"
    )
    trabajo.guardar()

    assert not subida_estancada(trabajo, trabajo.config.espera_reenvio_s)
    assert not subida_perdida(EstadoParte(trabajo, BUSCANDO, "no aparecio"))


def test_el_batch_que_cierra_el_programa_queda_como_autocompletado(tmp_path):
    """La tabla distingue lo que cerro el programa de lo que ya estaba cerrado."""
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = mapa(0, 0)

    hechos = completar_partes([trabajo], cliente, automatico=True)

    assert [r.completado for _t, r in hechos] == [True]
    assert trabajo.manifiesto.completado_automatico
    parte = estado_local(trabajo)
    assert parte.estado == AUTOCOMPLETADO
    assert parte.se_acabo


def test_un_batch_cerrado_por_fuera_no_se_da_por_autocompletado(tmp_path):
    """Lo cerro otra persona o una version anterior: se dice tal cual."""
    trabajo = _trabajos_principal_division_y_revisar(tmp_path)[0]
    trabajo.manifiesto.etapa("completar").marcar(
        EstadoEtapa.HECHA, "2 paginas en verde"
    )
    trabajo.guardar()

    parte = estado_local(trabajo)

    assert parte.estado == COMPLETADO
    assert parte.se_acabo


def test_una_carga_recien_subida_no_se_reenvia_por_no_estar_todavia(
    tmp_path, monkeypatch,
):
    """AirVault tarda: que aun no aparezca es lo normal, no una perdida.

    La resubida inmediata es para la carga vieja que ya no esta en la cola.
    Una recien enviada conserva el camino de siempre (tres revisiones y
    despues la espera) porque volver a subirla ahora publicaria el mismo
    archivo dos veces.
    """
    trabajo = _trabajos_principal_division_y_revisar(tmp_path)[1]
    _subida_en(trabajo, datetime.now() - timedelta(minutes=2))
    cliente = ClienteFalso()
    subidas = []
    monkeypatch.setattr(
        Trabajo, "subir", lambda *args, **kwargs: subidas.append(True)
    )

    estados = [comprobar_partes([trabajo], cliente)[0] for _ in range(3)]

    assert [estado.estado for estado in estados] == [
        PROCESANDO, PROCESANDO, BUSCANDO,
    ]
    assert trabajo.manifiesto.busquedas_amplias_sin_hallar == 0
    assert trabajo.manifiesto.intentos_identificacion == 3
    assert not subida_estancada(trabajo, trabajo.config.espera_reenvio_s)
    assert not subida_perdida(estados[-1], [trabajo])
    subir_partes([trabajo], SesionFalsa(), cliente=cliente)
    assert subidas == []
