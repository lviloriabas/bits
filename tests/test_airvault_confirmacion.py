"""Muestreo acotado de publicacion sin confundir bitacoras de otra carga."""

import base64
from types import SimpleNamespace

import pytest

from app.airvault.confirmacion import METODO_MUESTRA, huella_de_batch, muestra_de_batch, verificar_batch
from app.airvault.config import AirVaultConfig
from app.airvault.flujo import Trabajo
from app.airvault.model import Manifiesto, Registro


def caso(tmp_path, cantidad=500, conocido=True):
    m = Manifiesto(job_id="prueba", nombre_batch="DP | ENTREGA", batch_id="B01" if conocido else None,
                   registros=[Registro(seq=i + 1, log_number=str(2000000 + i), matricula="HP-1848CMP")
                              for i in range(cantidad)])
    t = Trabajo(AirVaultConfig(), tmp_path / "batch", m)
    t.guardar()
    filas = [{"DocId": str(i), "BatchId": "B01", "C_BatchName": m.nombre_batch,
              "C_DocNo": r.log_number, "C_ACREG": r.matricula} for i, r in enumerate(m.registros)]
    pedidos = []

    def get(ruta, params):
        pedidos.append(dict(params))
        if "encodedValues" in params:
            numero = base64.b64decode(params["encodedValues"]).decode().split("=", 1)[1]
        elif "encodedKeywordFilter" in params:
            numero = base64.b64decode(params["encodedKeywordFilter"]).decode()
        else:
            numero = params.get("value", params.get("searchText", ""))
        encontradas = [f for f in filas if f.get("C_DocNo") == numero or "fields" in f]
        return {"rows": encontradas[:50], "records": len(encontradas)}

    b = SimpleNamespace(ruta="/zfp/Search/GetSearchResults", _plantilla="encodedValues",
                        config=t.config, sesion=SimpleNamespace(get=get))
    return t, b, filas, pedidos


@pytest.mark.parametrize("cantidad,esperadas", [(1, 1), (2, 2), (5, 5), (7, 7), (50, 7), (350, 7),
                                              (351, 8), (500, 10), (750, 15), (1000, 15), (10000, 15)])
def test_consulta_el_porcentaje_acotado_y_no_todo_el_batch(tmp_path, cantidad, esperadas):
    t, b, _, pedidos = caso(tmp_path, cantidad)
    resultado = verificar_batch(b, t)
    assert resultado.confirmado
    assert resultado.encontradas == resultado.esperadas == esperadas
    assert len(pedidos) == esperadas
    assert resultado.muestra == tuple(muestra_de_batch(t.manifiesto))
    assert all(p["page"] == 1 for p in pedidos)
    assert resultado.batch_id == "b01"


@pytest.mark.parametrize("campo,valor", [("C_BatchName", "DP | OTRA CARGA"), ("BatchId", "B02"),
                                          ("C_ACREG", "HP-1550CMP")])
def test_no_confirma_numeros_iguales_en_otra_identidad(tmp_path, campo, valor):
    t, b, filas, _ = caso(tmp_path, 5)
    for fila in filas:
        fila[campo] = valor
    assert not verificar_batch(b, t).confirmado


def test_la_muestra_incluye_extremos_y_puntos_intermedios(tmp_path):
    t, _, _, _ = caso(tmp_path)
    assert muestra_de_batch(t.manifiesto) == ["2000000", "2000055", "2000111", "2000166", "2000222",
                                            "2000277", "2000333", "2000388", "2000444", "2000499"]


@pytest.mark.parametrize("plantilla", ["encodedValues", "encodedKeywordFilter", "fieldId", "searchText"])
def test_consulta_numeros_con_cualquiera_de_las_formas_guardadas(tmp_path, plantilla):
    t, b, _, pedidos = caso(tmp_path)
    b._plantilla = plantilla
    assert verificar_batch(b, t).confirmado
    assert len(pedidos) == 10


def test_una_bitacora_fuera_de_la_muestra_no_exige_auditar_todo(tmp_path):
    t, b, filas, _ = caso(tmp_path)
    filas.pop(270)
    resultado = verificar_batch(b, t)
    assert resultado.confirmado
    assert resultado.encontradas == 10


def test_resultados_repetidos_no_reemplazan_una_bitacora_ausente(tmp_path):
    t, b, filas, _ = caso(tmp_path)
    filas[-1] = dict(filas[0])
    resultado = verificar_batch(b, t)
    assert not resultado.confirmado
    assert resultado.encontradas == 9


def test_dos_batches_del_mismo_nombre_no_se_mezclan(tmp_path):
    t, b, filas, _ = caso(tmp_path, conocido=False)
    for fila in filas[250:]:
        fila["BatchId"] = "B02"
    assert not verificar_batch(b, t).confirmado


def test_el_nombre_exacto_permite_confirmar_sin_id_expuesto(tmp_path):
    t, b, filas, _ = caso(tmp_path, 5, conocido=False)
    for fila in filas:
        del fila["BatchId"]
    resultado = verificar_batch(b, t)
    assert resultado.confirmado
    assert not resultado.batch_id


def test_sin_nombre_ni_id_no_confirma_una_copia_de_otra_carga(tmp_path):
    t, b, filas, _ = caso(tmp_path, 5)
    for fila in filas:
        del fila["BatchId"]
        del fila["C_BatchName"]
    assert not verificar_batch(b, t).confirmado


def test_no_toma_un_resumen_de_records_como_documentos_confirmados(tmp_path):
    t, b, _, _ = caso(tmp_path)
    b.sesion.get = lambda *a: {"records": 500, "rows": []}
    assert not verificar_batch(b, t).confirmado


def test_una_respuesta_sin_filtrar_no_obliga_a_recorrer_todo(tmp_path):
    t, b, filas, pedidos = caso(tmp_path)

    def get(ruta, params):
        pedidos.append(params)
        return {"rows": filas[:50]}

    b.sesion.get = get
    assert not verificar_batch(b, t).confirmado
    assert len(pedidos) == 2


def test_una_fila_con_campos_por_identificador_se_entiende(tmp_path):
    from app.airvault.config import CAMPO_BATCH_NAME, CAMPO_LOG_NUMBER, CAMPO_MATRICULA
    t, b, filas, _ = caso(tmp_path, 1)
    filas[:] = [{"BatchId": "B01", "DocId": "D1", "fields": [
        {"FieldId": CAMPO_BATCH_NAME, "Value": "DP | ENTREGA"},
        {"FieldId": CAMPO_LOG_NUMBER, "Value": "2000000"},
        {"FieldId": CAMPO_MATRICULA, "Value": "HP-1848CMP"},
    ]}]
    assert verificar_batch(b, t).confirmado


def test_dos_copias_de_una_matricula_no_reemplazan_la_otra(tmp_path):
    t, b, filas, _ = caso(tmp_path, 2)
    t.manifiesto.registros[1].log_number = t.manifiesto.registros[0].log_number
    t.manifiesto.registros[1].matricula = "HP-1550CMP"
    filas[1]["C_DocNo"] = filas[0]["C_DocNo"]
    assert not verificar_batch(b, t).confirmado


def test_la_muestra_no_busca_varias_veces_la_misma_bitacora(tmp_path):
    t, b, filas, _ = caso(tmp_path, 2)
    t.manifiesto.registros[1].log_number = t.manifiesto.registros[0].log_number
    filas[1]["C_DocNo"] = filas[0]["C_DocNo"]
    for i, fila in enumerate(filas):
        del fila["DocId"]
        fila["metadata"] = str(i)
    resultado = verificar_batch(b, t)
    assert resultado.confirmado
    assert resultado.encontradas == resultado.esperadas == 1


def test_busqueda_con_el_cliente_real_y_ruta_guardada(tmp_path):
    from app.airvault.websearch import Buscador
    t, b, filas, _ = caso(tmp_path, 2)
    config = t.config.with_overrides(ruta_websearch=b.ruta, parametros_websearch="encodedValues")
    real = Buscador(b.sesion, config, controles=["2000000"])
    assert verificar_batch(real, t).confirmado
    assert real.ruta == b.ruta


def test_confirmar_no_reemplaza_el_avance_guardado_por_otro_hilo(tmp_path):
    from app.airvault.manifest import guardar_confirmacion, cargar, guardar
    from app.airvault.confirmacion import huella_de_batch
    from app.airvault.model import EstadoRegistro
    t, _, _, _ = caso(tmp_path, 2)
    antiguo = cargar(t.carpeta)
    t.manifiesto.registros[0].estado = EstadoRegistro.ESCRITA
    t.guardar()
    datos = dict(websearch_confirmado="2026-10-09T10:00:00.123456", websearch_muestra=[],
                 websearch_detalle="2 de 2", websearch_metodo="completo_por_identidad",
                 websearch_batch_id="b01", websearch_revision="2026-10-09T10:00:00.123456",
                 websearch_cotejadas=2, websearch_huella=huella_de_batch(t.manifiesto), batch_id="b01")
    guardar_confirmacion(datos, t.carpeta)
    assert cargar(t.carpeta).registros[0].estado == EstadoRegistro.ESCRITA
    antiguo.registros[0].estado = antiguo.registros[1].estado = EstadoRegistro.ESCRITA
    guardar(antiguo, t.carpeta)
    final = cargar(t.carpeta)
    assert final.websearch_confirmado == datos["websearch_confirmado"]
    assert all(r.estado == EstadoRegistro.ESCRITA for r in final.registros)
    datos["batch_id"] = "OTRO"
    with pytest.raises(ValueError, match="identidad"):
        guardar_confirmacion(datos, t.carpeta)
    assert cargar(t.carpeta).batch_id == "B01"


def test_la_prueba_de_un_contenido_anterior_no_confirma_un_batch_editado(tmp_path):
    from app.airvault.flujo import websearch_confirmacion_valida
    t, _, _, _ = caso(tmp_path, 2)
    from app.airvault.confirmacion import huella_de_batch
    t.manifiesto.websearch_confirmado = "2026-10-09T10:00:00"
    t.manifiesto.websearch_metodo = "completo_por_identidad"
    t.manifiesto.websearch_batch_id = "b01"
    t.manifiesto.websearch_cotejadas = 2
    t.manifiesto.websearch_huella = huella_de_batch(t.manifiesto)
    assert websearch_confirmacion_valida(t.manifiesto)
    t.manifiesto.registros[1].log_number = "2999999"
    assert not websearch_confirmacion_valida(t.manifiesto)


def test_descubrir_la_ruta_respeta_el_presupuesto_y_la_cancelacion(tmp_path, monkeypatch):
    from app.airvault.websearch import Buscador
    from app.airvault.session import SesionCancelada
    reloj, pedidos = [0], []
    monkeypatch.setattr("app.airvault.confirmacion.time.monotonic", lambda: reloj[0])
    t, _, _, _ = caso(tmp_path, 2)

    def get(*args, **kwargs):
        pedidos.append(args)
        reloj[0] += 6
        return {"rows": []}

    b = Buscador(SimpleNamespace(get=get), t.config)
    assert not verificar_batch(b, t, presupuesto_s=15).confirmado
    assert len(pedidos) == 3
    pedidos.clear()
    with pytest.raises(SesionCancelada):
        verificar_batch(b, t, cancelado=lambda: True)
    assert not pedidos


def datos_de(resultado, manifiesto):
    return dict(websearch_confirmado="2026-10-10T10:00:00" if resultado.confirmado else "",
                websearch_muestra=list(resultado.muestra), websearch_detalle=resultado.detalle,
                websearch_metodo=METODO_MUESTRA, websearch_batch_id=resultado.batch_id,
                websearch_revision="2026-10-10T10:00:00", websearch_cotejadas=resultado.encontradas,
                websearch_huella=huella_de_batch(manifiesto, resultado.batch_id), batch_id=resultado.batch_id)


@pytest.mark.parametrize("conocido,expone_id", [(True, True), (True, False), (False, True), (False, False)])
def test_la_muestra_persiste_el_cierre_local_sin_completar_en_el_servidor(tmp_path, conocido, expone_id):
    from app.airvault.flujo import COMPLETADO, estado_local, websearch_confirmacion_valida
    from app.airvault.manifest import guardar_confirmacion
    t, b, filas, pedidos = caso(tmp_path, conocido=conocido)
    if not expone_id:
        for fila in filas:
            del fila["BatchId"]
    r = verificar_batch(b, t)
    guardar_confirmacion(datos_de(r, t.manifiesto), t.carpeta)
    final = Trabajo.cargar(t.config, t.carpeta)
    assert websearch_confirmacion_valida(final.manifiesto)
    assert final.manifiesto.etapa_hecha("completar")
    assert estado_local(final).estado == COMPLETADO
    assert not estado_local(final).se_puede_subir
    assert len(pedidos) == 10


def test_otro_worker_no_borra_el_cierre_por_muestra_y_editar_invalida_la_prueba(tmp_path):
    from app.airvault.flujo import COMPLETADO, estado_local, websearch_confirmacion_valida
    from app.airvault.manifest import cargar, guardar, guardar_confirmacion
    from app.airvault.model import EstadoRegistro
    t, b, _, _ = caso(tmp_path)
    antiguo = cargar(t.carpeta)
    r = verificar_batch(b, t)
    guardar_confirmacion(datos_de(r, t.manifiesto), t.carpeta)
    antiguo.registros[0].estado = EstadoRegistro.ESCRITA
    guardar(antiguo, t.carpeta)
    final = cargar(t.carpeta)
    assert final.etapa_hecha("completar")
    assert final.registros[0].estado == EstadoRegistro.ESCRITA
    final.registros[270].log_number = "2999999"
    guardar(final, t.carpeta)
    assert not websearch_confirmacion_valida(final)
    assert not final.etapa_hecha("completar")
    assert estado_local(Trabajo(t.config, t.carpeta, final)).estado != COMPLETADO


def test_faltan_numeros_validos_no_consulta(tmp_path):
    t, b, _, pedidos = caso(tmp_path, 1)
    t.manifiesto.registros[0].log_number = "123"
    assert not verificar_batch(b, t).confirmado
    assert not pedidos


def test_no_consulta_separadores_ni_numeros_dudosos(tmp_path):
    t, b, _, _ = caso(tmp_path, 5)
    t.manifiesto.registros[0].separador = "REVISAR"
    t.manifiesto.registros[-1].log_number = ""
    assert muestra_de_batch(t.manifiesto) == ["2000001", "2000002", "2000003"]


def test_la_cancelacion_entre_consultas_corta_la_muestra(tmp_path):
    from app.airvault.session import SesionCancelada
    t, b, _, pedidos = caso(tmp_path)
    with pytest.raises(SesionCancelada):
        verificar_batch(b, t, cancelado=lambda: len(pedidos) == 1)
    assert len(pedidos) == 1


def test_la_ultima_respuesta_tardia_no_confirma_fuera_del_presupuesto(tmp_path, monkeypatch):
    reloj = [0]
    monkeypatch.setattr("app.airvault.confirmacion.time.monotonic", lambda: reloj[0])
    t, b, _, pedidos = caso(tmp_path, 2)
    get_original = b.sesion.get

    def get(*args):
        reloj[0] += 6
        return get_original(*args)

    b.sesion.get = get
    resultado = verificar_batch(b, t, presupuesto_s=10)
    assert not resultado.confirmado
    assert resultado.encontradas == 1
    assert len(pedidos) == 2
