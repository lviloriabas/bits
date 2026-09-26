"""Los datos legibles se guardan y solo lo pendiente requiere una persona."""

import pytest

from app.airvault.config import (
    CAMPOS_OBLIGATORIOS, CAMPO_DESCRIPCION, CAMPO_END_DATE, CAMPO_FLEET, CAMPO_LOG_NUMBER,
    CAMPO_MATRICULA, ESTADO_NECESITA_CORRECCION, ESTADO_VALIDO,
)
from app.airvault.guards import verificar_obligatorios
from app.airvault.indexer import Indexador
from app.airvault.indexer import verificar_revision
from app.airvault.mapping import registros_desde_entrega
from app.airvault.mapping import valores_de_indice
from app.airvault.model import EstadoRegistro
from tests.airvault_fake import ClienteFalso, pagina
from tests.test_airvault_indexer import manifiesto, PICKLIST


@pytest.mark.parametrize("campo,atributo", [
    (CAMPO_END_DATE, "fecha"), (CAMPO_MATRICULA, "matricula"),
    (CAMPO_FLEET, "fleet"),
])
def test_guarda_logpage_aunque_falte_otro_obligatorio(campo, atributo):
    m = manifiesto(1)
    m.solo_subir = True
    setattr(m.registros[0], atributo, "")
    cli = ClienteFalso(page_count=1)
    indexador = Indexador(cli, m, PICKLIST)
    resultado = indexador.aplicar(indexador.planificar(1))
    assert (resultado.escritas, resultado.fallidas) == (1, 0)
    _, valores, estado = cli.escrituras[0]
    assert valores[CAMPO_LOG_NUMBER] == "2287321"
    assert campo not in valores
    assert all(valores[c] for c in CAMPOS_OBLIGATORIOS if c in valores)
    assert estado == ESTADO_NECESITA_CORRECCION
    assert cli.lecturas == [1, 1]


def test_permitir_parcial_no_autoriza_enviar_un_vacio_explicito():
    r = manifiesto(1).registros[0]
    avisos = verificar_obligatorios(r, {CAMPO_LOG_NUMBER: ""}, permitir_incompletos=True)
    assert any(a.codigo == "obligatorio_vacio" and "Log Page Number" in a.detalle
               for a in avisos)


@pytest.mark.parametrize("pendiente,discrepancia,esperado", [
    (False, False, ESTADO_VALIDO),
    (True, False, ESTADO_NECESITA_CORRECCION),
    (False, True, ESTADO_NECESITA_CORRECCION),
    (None, False, ESTADO_NECESITA_CORRECCION),
])
def test_decide_por_pagina_y_conserva_motivos_antiguos(pendiente, discrepancia, esperado):
    m = manifiesto(1)
    m.solo_subir = True
    m.registros[0].revision_pendiente = pendiente
    m.registros[0].discrepancia = discrepancia
    cli = ClienteFalso(page_count=1)
    indexador = Indexador(cli, m, PICKLIST)
    assert indexador.aplicar(indexador.planificar(1)).escritas == 1
    assert cli.escrituras[0][2] == esperado
    assert not cli.completados


def test_la_correspondencia_incorrecta_sigue_bloqueando_el_guardado_parcial():
    m = manifiesto(1)
    m.solo_subir = True
    m.registros[0].fecha = ""
    cli = ClienteFalso(paginas={1: pagina(1, valores={CAMPO_LOG_NUMBER: "9999999"})})
    indexador = Indexador(cli, m, PICKLIST)
    assert not indexador.planificar(1).escribibles
    assert not cli.escrituras


def test_no_da_por_guardada_una_pagina_si_el_servidor_perdio_el_log():
    class PierdeLog(ClienteFalso):
        def guardar_pagina(self, *args):
            resultado = super().guardar_pagina(*args)
            if args[1] == 1:
                self.paginas[1].valores.pop(CAMPO_LOG_NUMBER)
            return resultado

    m = manifiesto(2)
    m.solo_subir = True
    cli = PierdeLog(page_count=2)
    indexador = Indexador(cli, m, PICKLIST)
    resultado = indexador.aplicar(indexador.planificar(2))
    assert (resultado.escritas, resultado.fallidas) == (1, 1)
    assert m.registros[0].estado == EstadoRegistro.ERROR
    assert m.registros[1].estado == EstadoRegistro.ESCRITA


@pytest.mark.parametrize("marca,esperado", [(False, False), (True, True), (None, None), ("false", None)])
def test_la_causa_de_revision_viaja_del_indice_al_manifiesto(marca, esperado):
    registros = registros_desde_entrega(
        [{"file": "x.pdf", "page": "1", "log_number": "2287321",
          "matricula": "HP-1848CMP", "date": "2026/08/31"}],
        [{"archivo": "x.pdf", "pagina": 1, "revision_pendiente": marca}],
    )
    assert registros[0].revision_pendiente is esperado


def test_revision_termina_con_datos_parciales_y_no_los_reescribe():
    m = manifiesto(2)
    m.solo_subir = True
    m.registros[0].fecha = ""
    m.registros[1].discrepancia = True
    cli = ClienteFalso(page_count=2)
    indexador = Indexador(cli, m, PICKLIST)
    assert indexador.aplicar(indexador.planificar(2)).escritas == 2
    assert verificar_revision(cli, m) == (2, 2, [])
    assert indexador.aplicar(indexador.planificar(2)).escritas == 0
    assert len(cli.escrituras) == 2
    assert not cli.completados


def test_doc_type_en_mayusculas_no_se_toma_por_guardado_perdido():
    """AirVault guarda «LOG PAGE» aunque reciba «Log Page».

    Asi estan las paginas de REVISAR que si conservaron fecha, log y razon
    ECN. Compararlo tal cual marcaba todas como rechazadas y las reescribia.
    """
    from app.airvault.config import CAMPO_DOC_TYPE

    class Mayusculas(ClienteFalso):
        def guardar_pagina(self, *args):
            resultado = super().guardar_pagina(*args)
            valores = self.paginas[args[1]].valores
            valores[CAMPO_DOC_TYPE] = valores[CAMPO_DOC_TYPE].upper()
            return resultado

    m = manifiesto(2)
    m.solo_subir = True
    cli = Mayusculas(page_count=2)
    indexador = Indexador(cli, m, PICKLIST)
    resultado = indexador.aplicar(indexador.planificar(2))
    assert (resultado.escritas, resultado.fallidas) == (2, 0)
    assert cli.paginas[1].valores[CAMPO_DOC_TYPE] == "LOG PAGE"
    assert verificar_revision(cli, m) == (2, 2, [])


def test_verificar_revision_no_confirma_datos_que_se_perdieron():
    m = manifiesto(1)
    m.solo_subir = True
    cli = ClienteFalso(page_count=1)
    indexador = Indexador(cli, m, PICKLIST)
    indexador.aplicar(indexador.planificar(1))
    cli.paginas[1].valores[CAMPO_LOG_NUMBER] = "9999999"
    confirmadas, total, problemas = verificar_revision(cli, m)
    assert (confirmadas, total) == (0, 1)
    assert "Log Page Number" in problemas[0]


def test_una_revision_resuelta_pasa_de_amarillo_a_valido():
    m = manifiesto(1)
    m.solo_subir = True
    cli = ClienteFalso(page_count=1)
    indexador = Indexador(cli, m, PICKLIST)
    indexador.aplicar(indexador.planificar(1))
    m.registros[0].revision_pendiente = False
    assert indexador.aplicar(indexador.planificar(1)).escritas == 1
    assert cli.paginas[1].estado == ESTADO_VALIDO


def test_trabajo_terminado_persiste_y_sale_de_la_busqueda(tmp_path):
    from app.airvault.config import AirVaultConfig
    from app.airvault.flujo import Trabajo, estado_local, INDEXADO
    from app.airvault.model import EstadoEtapa

    m = manifiesto(1)
    m.solo_subir = True
    m.registros[0].fecha = ""
    cli = ClienteFalso(page_count=1)
    indexador = Indexador(cli, m, PICKLIST)
    indexador.aplicar(indexador.planificar(1))
    trabajo = Trabajo(AirVaultConfig(), tmp_path, m)
    m.etapa("subir").marcar(EstadoEtapa.HECHA)
    avances = []
    assert trabajo.verificar(cli, al_avanzar=lambda n, t: avances.append((n, t))) == (1, 1, [])
    assert m.etapa("verificar").estado is EstadoEtapa.HECHA
    assert estado_local(trabajo).estado == INDEXADO
    assert avances == [(0, 1), (1, 1)]
    assert not cli.completados


def test_rechazo_de_obligatorio_omitido_no_finge_exito_ni_corta_las_demas():
    from app.airvault.session import ErrorDeAirVault

    class ExigeAircraft(ClienteFalso):
        def guardar_pagina(self, batch, pagina, valores, estado, siguiente=None):
            if CAMPO_MATRICULA not in valores:
                raise ErrorDeAirVault("Field Aircraft value is required")
            return super().guardar_pagina(batch, pagina, valores, estado, siguiente)

    m = manifiesto(2)
    m.solo_subir = True
    m.registros[0].matricula = ""
    cli = ExigeAircraft(page_count=2)
    indexador = Indexador(cli, m, PICKLIST)
    resultado = indexador.aplicar(indexador.planificar(2))
    assert (resultado.escritas, resultado.fallidas) == (1, 1)
    assert verificar_revision(cli, m)[:2] == (1, 2)


@pytest.mark.parametrize("campo", [*CAMPOS_OBLIGATORIOS, CAMPO_DESCRIPCION])
@pytest.mark.parametrize("vacio", [None, "", "  "])
def test_revisar_completa_datos_ausentes_aunque_airvault_diga_valid(campo, vacio):
    m = manifiesto(1)
    m.solo_subir = True
    m.registros[0].flight_number = "CM137"
    m.registros[0].revision_pendiente = False
    m.registros[0].estado = EstadoRegistro.ESCRITA
    esperados = valores_de_indice(
        m.registros[0], m.doc_type, m.audit_status, m.nombre_batch,
    )
    remotos = dict(esperados)
    if vacio is None:
        remotos.pop(campo)
    else:
        remotos[campo] = vacio
    cli = ClienteFalso(paginas={1: pagina(1, estado=ESTADO_VALIDO, valores=remotos)})
    indexador = Indexador(cli, m, PICKLIST)

    resultado = indexador.aplicar(indexador.planificar(1))

    assert (resultado.escritas, resultado.fallidas) == (1, 0)
    assert cli.paginas[1].valores[campo] == esperados[campo]
    assert verificar_revision(cli, m) == (1, 1, [])
    assert indexador.aplicar(indexador.planificar(1)).escritas == 0
    assert len(cli.escrituras) == 1
    assert not cli.completados


def test_completar_valid_conserva_lo_existente_y_recupera_varios_vacios():
    m = manifiesto(1)
    m.solo_subir = True
    m.registros[0].flight_number = "CM137"
    cli = ClienteFalso(paginas={1: pagina(1, estado=ESTADO_VALIDO, valores={
        CAMPO_LOG_NUMBER: "2287321", CAMPO_MATRICULA: "HP-1848CMP",
        CAMPO_END_DATE: "", CAMPO_FLEET: "", CAMPO_DESCRIPCION: "Nota manual",
    })})
    indexador = Indexador(cli, m, PICKLIST)
    assert indexador.aplicar(indexador.planificar(1)).escritas == 1
    valores = cli.escrituras[0][1]
    assert valores[CAMPO_END_DATE] == "08/31/2026"
    assert valores[CAMPO_FLEET] == "NG"
    assert valores[CAMPO_DESCRIPCION] == "Nota manual"


def test_completar_valid_no_elude_la_correspondencia_del_log():
    m = manifiesto(1)
    m.solo_subir = True
    cli = ClienteFalso(paginas={1: pagina(1, estado=ESTADO_VALIDO, valores={
        CAMPO_LOG_NUMBER: "9999999", CAMPO_MATRICULA: "HP-1848CMP",
    })})
    indexador = Indexador(cli, m, PICKLIST)
    assert not indexador.planificar(1).escribibles
    assert not cli.escrituras


def test_batch_automatico_no_recupera_otros_campos_de_una_pagina_valid():
    m = manifiesto(1)
    remotos = valores_de_indice(m.registros[0], m.doc_type, m.audit_status)
    remotos.pop(CAMPO_LOG_NUMBER)
    cli = ClienteFalso(paginas={1: pagina(1, estado=ESTADO_VALIDO, valores=remotos)})
    indexador = Indexador(cli, m, PICKLIST)
    assert not indexador.planificar(1).escribibles
    assert not cli.escrituras


def _trabajo_con_csv(tmp_path, m):
    from app.airvault.config import AirVaultConfig
    from app.airvault.flujo import Trabajo

    csv = tmp_path / "procesado.csv"
    csv.write_text(
        "file,page,log_number,matricula,date,flight_number\n"
        "Image_001.pdf,1,2287321,HP-1848CMP,2026/08/20,CM137\n",
        encoding="utf-8",
    )
    m.csv_origen = str(csv)
    return Trabajo(AirVaultConfig(), tmp_path / "job", m)


@pytest.mark.parametrize("atributo,campo,esperado", [
    ("log_number", CAMPO_LOG_NUMBER, "2287321"),
    ("matricula", CAMPO_MATRICULA, "HP-1848CMP"),
    ("fecha", CAMPO_END_DATE, "08/31/2026"),
    ("flight_number", CAMPO_DESCRIPCION, "CM137"),
    ("fleet", CAMPO_FLEET, "NG"),
])
def test_retomar_revision_recupera_del_csv_el_campo_y_lo_guarda(
    tmp_path, atributo, campo, esperado,
):
    from app.airvault.flujo import Trabajo
    from app.airvault.model import EstadoEtapa

    m = manifiesto(1)
    m.solo_subir = True
    m.fin_de_mes = True
    m.registros[0].revision_pendiente = True
    m.registros[0].discrepancia = True
    m.registros[0].discrepancy_fields = ["date"]
    m.registros[0].pagina_batch = 4
    m.registros[0].estado = EstadoRegistro.ESCRITA
    setattr(m.registros[0], atributo, "")
    for etapa in ("subir", "indexar", "verificar"):
        m.etapa(etapa).marcar(EstadoEtapa.HECHA)
    trabajo = _trabajo_con_csv(tmp_path, m)
    trabajo.guardar()
    retomado = Trabajo.cargar(trabajo.config, trabajo.carpeta)
    cli = ClienteFalso(page_count=1, picklist=PICKLIST)

    plan, indexador = retomado.planificar(cli)
    assert indexador.aplicar(plan).escritas == 1

    assert cli.escrituras[0][0] == 4
    assert cli.escrituras[0][1][campo] == esperado
    assert cli.escrituras[0][2] == ESTADO_NECESITA_CORRECCION
    assert retomado.manifiesto.batch_id == m.batch_id
    assert retomado.manifiesto.etapa_hecha("subir")
    assert not retomado.manifiesto.etapa_hecha("verificar")
    registro = retomado.manifiesto.registros[0]
    assert registro.revision_pendiente is True
    assert registro.discrepancy_fields == ["date"]
    assert verificar_revision(cli, retomado.manifiesto) == (1, 1, [])
    assert retomado.rehidratar_registros_huerfanos(m.csv_origen) == 0


@pytest.mark.parametrize("revision,dudosa,log,fecha", [
    (False, False, "2287321", ""),
    (True, True, "2287321", ""),
    (True, False, "9999999", ""),
    (True, False, "2287321", "2026/07/31"),
])
def test_recuperar_csv_conserva_fechas_dudosas_presentes_y_otras_identidades(
    tmp_path, revision, dudosa, log, fecha,
):
    m = manifiesto(1)
    m.solo_subir = revision
    m.registros[0].fecha_dudosa = dudosa
    m.registros[0].fecha = fecha
    m.registros[0].log_number = log
    trabajo = _trabajo_con_csv(tmp_path, m)
    trabajo.rehidratar_registros_huerfanos(m.csv_origen)
    assert m.registros[0].fecha == fecha
    assert m.registros[0].fecha_dudosa == dudosa
    assert m.registros[0].log_number == log


def test_recuperar_huerfano_de_revision_conserva_las_incidencias_del_csv(tmp_path):
    m = manifiesto(1)
    m.solo_subir = True
    m.registros[0].matricula = ""
    m.registros[0].log_number = ""
    m.registros[0].fecha = ""
    trabajo = _trabajo_con_csv(tmp_path, m)
    from pathlib import Path

    csv = Path(m.csv_origen)
    csv.write_text(
        "file,page,log_number,matricula,date,disc,dup\n"
        "Image_001.pdf,1,2287321,HP-1848CMP,2026/08/20,true,true\n",
        encoding="utf-8",
    )
    assert trabajo.rehidratar_registros_huerfanos(csv) == 1
    assert m.registros[0].discrepancia
    assert m.registros[0].duplicado
