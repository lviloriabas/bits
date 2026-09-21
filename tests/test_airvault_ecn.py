"""Categorias exactas, multiples faltas y conservacion de anotaciones."""

import pytest

from app.airvault.ecn import (
    CAMPOS_ECN,
    RAZON_POR_CAMPO,
    conservar_razones,
    razones_ecn,
)
from app.airvault.indexer import Indexador, verificar_revision
from app.airvault.ecn import campos_del_resumen
from app.airvault.mapping import registros_desde_entrega, valores_de_indice
from tests.airvault_fake import ClienteFalso, pagina
from tests.test_airvault_indexer import manifiesto, PICKLIST


def test_capitan_comparte_categoria_y_tecnico_prioriza_firma():
    assert razones_ecn(["captain_signature", "captain_license"]) == [
        "DISCREPANCY NOTE: MISSING SIGNATURE AND/OR LICENSE NUMBER OF THE CAPTAIN",
    ]
    assert razones_ecn(["technician_signature", "technician_license"]) == [
        "DISCREPANCY NOTE: MISSING TECHNICIAN SIGNATURE",
    ]
    assert razones_ecn(["technician_license"]) == ["DISCREPANCY NOTE: MISSING LICENSE NUMBER"]
    assert razones_ecn(["correction_block", "desconocido"]) == []


@pytest.mark.parametrize("razon", [
    "MISSING SIGNATURE AND/OR LICENSE NUMBER OF THE CAPTAIN",
    "MISSING SIGNATURE AND/OR LICENSE NUMBER OF THE PILOT",
    "MISSING TECHNICIAN SIGNATURE",
    "MISSING LICENSE NUMBER",
])
def test_airvault_recibe_la_razon_del_csv_con_su_prefijo(razon):
    assert razones_ecn([], razon) == [f"DISCREPANCY NOTE: {razon}"]


@pytest.mark.parametrize("faltantes,esperada", [
    (["technician_license", "pilot_signature", "captain_signature"], "captain_signature"),
    (["captain_license", "pilot_signature", "technician_signature"], "captain_license"),
    (["technician_signature", "technician_license", "pilot_signature"], "pilot_signature"),
    (["technician_license", "technician_signature"], "technician_signature"),
    (["technician_license"], "technician_license"),
])
def test_guarda_solo_una_razon_segun_prioridad_y_confirma_el_final(faltantes, esperada):
    m = manifiesto(1)
    m.solo_subir = True
    r = m.registros[0]
    r.discrepancia = True
    r.discrepancy_fields = faltantes
    cli = ClienteFalso(page_count=1)
    indexador = Indexador(cli, m, PICKLIST)
    assert indexador.aplicar(indexador.planificar(1)).escritas == 1
    valores = cli.escrituras[0][1]
    assert valores[9692] == RAZON_POR_CAMPO[esperada]
    assert 9781 not in valores and 9782 not in valores
    assert verificar_revision(cli, m) == (1, 1, [])
    assert indexador.aplicar(indexador.planificar(1)).escritas == 0


def test_preserva_razon_manual_y_evitar_duplicarla():
    razon = RAZON_POR_CAMPO["pilot_signature"]
    assert conservar_razones({9692: razon}, {9692: "manual"}) == {9692: "manual"}
    assert conservar_razones({9692: razon}, {9782: razon}) == {9692: razon}
    assert conservar_razones({9692: razon, 9781: "otra", 9782: "otra"}, {}) == {9692: razon}
    assert conservar_razones({}, {9692: "manual"}) == {}


def test_no_pisa_categorias_manual_al_verificar():
    m = manifiesto(1)
    m.solo_subir = True
    m.registros[0].discrepancia = True
    m.registros[0].discrepancy_fields = ["captain_license"]
    cli = ClienteFalso(paginas={1: pagina(1, valores={9692: "manual"})})
    indexador = Indexador(cli, m, PICKLIST)
    assert indexador.aplicar(indexador.planificar(1)).escritas == 1
    assert cli.escrituras[0][1][9692] == "manual"
    assert 9781 not in cli.escrituras[0][1] and 9782 not in cli.escrituras[0][1]
    assert verificar_revision(cli, m) == (1, 1, [])


def test_entrega_anterior_conserva_razones_del_resumen_sin_cambiar_csv():
    filas = [{"file": "x.pdf", "page": "1", "disc": "true",
              "disc_reason": "Faltan firma de capitán y licencia de capitán"}]
    registro = registros_desde_entrega(filas, [{"archivo": "x.pdf", "pagina": 1}])[0]
    assert valores_de_indice(registro, "Log Page", "PUBLISHED")[9692] == RAZON_POR_CAMPO["captain_license"]
    assert campos_del_resumen("Corrección escrita: falta licencia de técnico") == ["technician_license"]
    assert campos_del_resumen("No falta firma de piloto") == []
    assert campos_del_resumen("Falta firma de piloto; incierta") == []


def test_la_razon_guardada_distingue_piloto_de_tecnico():
    filas = [{
        "file": "x.pdf",
        "page": "1",
        "disc": "true",
        "disc_reason": "MISSING TECHNICIAN SIGNATURE",
    }]
    indice = [{
        "archivo": "x.pdf",
        "pagina": 1,
        "discrepancy_fields": ["pilot_signature"],
        "disc_reason": "MISSING TECHNICIAN SIGNATURE",
    }]
    registro = registros_desde_entrega(filas, indice)[0]

    assert registro.discrepancy_fields == ["pilot_signature"]
    assert valores_de_indice(registro, "Log Page", "PUBLISHED")[9692] == (
        "DISCREPANCY NOTE: MISSING TECHNICIAN SIGNATURE"
    )
