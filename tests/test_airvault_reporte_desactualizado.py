"""Duplicadas que siguen en el reporte despues de limpiar Web Search."""

from __future__ import annotations

import json

import pytest

from app.airvault import correcciones as modulo
from app.airvault.config import AirVaultConfig
from app.airvault.correcciones import CorrectorLogPageAudit, copias_en, planificar
from app.airvault.mapping import ResolutorFlota
from app.airvault.web_reports import ConsultaCancelada
from tests.test_airvault_correcciones import _excepciones, _PaginaFalsa, _REJILLA


class _PaginaCompleta(_PaginaFalsa):
    def __init__(self, *lecturas, completa=(True, True)):
        super().__init__(*lecturas)
        self._completa = list(completa)

    def evaluar(self, expresion):
        respuesta = super().evaluar(expresion)
        if expresion == modulo._REJILLA_CON_UNA_FILA:
            if len(self._completa) > 1:
                return self._completa.pop(0)
            return self._completa[0]
        return respuesta


def _correccion(copias=2):
    return planificar(
        _excepciones(("HP-9913CMP", f"DUPLICATED 2008159({copias}x)"))
    )[0]


def _comprobar(pagina, filas=None, copias=2):
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    # La discrepancia no debe descargar imagenes ni pedir una revision.
    def no_revisar(*_args):
        pytest.fail("Una bitacora ya limpia no necesita revisar imagenes")

    corrector.revisar = no_revisar
    corrector._imagen_previa = no_revisar
    resultado = corrector._borrar(
        pagina, _correccion(copias),
        copias_en(filas if filas is not None else [_REJILLA[1]], "2008159"),
        ensayo=False,
    )
    assert not resultado.hecho
    assert not any("onDeletePage" in orden for orden in pagina.ordenes)
    assert not any("onReindexPage" in orden for orden in pagina.ordenes)
    return resultado


@pytest.mark.parametrize("copias", [2, 3, 10])
def test_confirma_la_misma_copia_antes_de_avisar_del_reporte(copias):
    pagina = _PaginaCompleta([_REJILLA[1]])

    resultado = _comprobar(pagina, copias=copias)

    assert resultado.detalle.startswith("Reporte desactualizado:")
    assert f"indica {copias} copias" in resultado.detalle
    assert "Ya no aparece duplicada" in resultado.detalle
    assert sum("location.reload()" in orden for orden in pagina.ordenes) == 1
    assert pagina.ordenes.count(modulo._REJILLA_CON_UNA_FILA) == 2


@pytest.mark.parametrize("completa", [False, None, "true"])
def test_una_busqueda_incompleta_no_demuestra_que_se_haya_limpiado(completa):
    pagina = _PaginaCompleta([_REJILLA[1]], completa=(completa,))

    resultado = _comprobar(pagina)

    assert "No se pudo confirmar" in resultado.detalle
    assert "Reporte desactualizado:" not in resultado.detalle
    assert not any("location.reload()" in orden for orden in pagina.ordenes)


@pytest.mark.parametrize("campo,valor", [
    ("clave", ""), ("imagenes", None), ("imagenes", "2"),
    ("tipo", ""), ("tipo", "FLEET"),
])
def test_un_documento_sin_identidad_o_protegido_no_se_da_por_limpio(campo, valor):
    fila = dict(_REJILLA[1], **{campo: valor})
    pagina = _PaginaCompleta([fila])

    resultado = _comprobar(pagina, filas=[fila])

    assert "No se pudo confirmar" in resultado.detalle
    assert not any("location.reload()" in orden for orden in pagina.ordenes)


@pytest.mark.parametrize("campo,valor", [
    ("clave", "otro-documento"), ("documento", "otro"),
    ("imagenes", "2"), ("tipo", "FLEET"),
    ("matricula", "HP-9813CMP"), ("cuando", "2/9/2025 2:05:00 PM"),
])
def test_la_segunda_lectura_debe_ser_el_mismo_documento(campo, valor):
    pagina = _PaginaCompleta([dict(_REJILLA[1], **{campo: valor})])

    resultado = _comprobar(pagina)

    assert "La búsqueda cambió" in resultado.detalle
    assert "Reporte desactualizado:" not in resultado.detalle
    assert sum("location.reload()" in orden for orden in pagina.ordenes) == 1


@pytest.mark.parametrize("filas", [[], _REJILLA[:2]])
def test_si_cambia_la_cantidad_en_la_segunda_lectura_no_se_borra(filas):
    resultado = _comprobar(_PaginaCompleta(filas))

    assert "La búsqueda cambió" in resultado.detalle
    assert "Reporte desactualizado:" not in resultado.detalle


def test_si_la_recarga_queda_incompleta_no_se_confirma_la_limpieza():
    pagina = _PaginaCompleta([_REJILLA[1]], completa=(True, False))

    resultado = _comprobar(pagina)

    assert "quedó incompleta" in resultado.detalle
    assert "Reporte desactualizado:" not in resultado.detalle


def test_el_orden_de_la_fila_no_cambia_la_identidad_del_documento():
    pagina = _PaginaCompleta([dict(_REJILLA[1], fila="1")])

    assert _comprobar(pagina).detalle.startswith("Reporte desactualizado:")


@pytest.mark.parametrize("filas,copias", [([], 2), (_REJILLA[:2], 3)])
def test_las_otras_discrepancias_no_lanzan_la_comprobacion_adicional(filas, copias):
    pagina = _PaginaCompleta(filas)

    resultado = _comprobar(pagina, filas=filas, copias=copias)

    assert "Reporte desactualizado:" not in resultado.detalle
    assert not any("location.reload()" in orden for orden in pagina.ordenes)
    assert modulo._REJILLA_CON_UNA_FILA not in pagina.ordenes


def test_cancelar_la_confirmacion_no_se_convierte_en_un_reporte_desactualizado():
    class _PaginaCancelada(_PaginaCompleta):
        def esperar(self, condicion, *_args):
            if condicion == modulo._REJILLA_RECARGADA:
                raise ConsultaCancelada()
            return True

    with pytest.raises(ConsultaCancelada):
        _comprobar(_PaginaCancelada([_REJILLA[1]]))


def test_mil_casos_normales_y_uno_viejo_solo_agregan_una_recarga():
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    correccion = _correccion()
    copias = copias_en(_REJILLA, "2008159")
    pagina = _PaginaCompleta([_REJILLA[1]])

    for _ in range(1000):
        resultado = corrector._borrar(pagina, correccion, copias, ensayo=True)
        assert "se borraría una copia" in resultado.detalle
    assert pagina.ordenes == []

    _comprobar(pagina)

    assert sum("location.reload()" in orden for orden in pagina.ordenes) == 1


def test_la_confirmacion_anterior_no_oculta_una_duplicada_nueva():
    pagina = _PaginaCompleta([_REJILLA[1]])
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    correccion = _correccion()
    resultado = corrector._borrar(
        pagina, correccion, copias_en([_REJILLA[1]], "2008159"), ensayo=True,
    )
    assert resultado.detalle.startswith("Reporte desactualizado:")
    ordenes_antes = len(pagina.ordenes)

    resultado = corrector._borrar(
        pagina, correccion, copias_en(_REJILLA, "2008159"), ensayo=True,
    )

    assert "se borraría una copia" in resultado.detalle
    assert len(pagina.ordenes) == ordenes_antes


def test_el_aviso_existente_muestra_el_motivo_sin_contarlo_como_borrado(
    app, tmp_path, monkeypatch,
):
    from app.gui import web_reports_window as ventana_modulo

    resultado = _comprobar(_PaginaCompleta([_REJILLA[1]]))
    avisos = []
    monkeypatch.setattr(
        ventana_modulo.QMessageBox, "exec",
        lambda aviso: avisos.append(aviso.informativeText()) or 0,
    )
    ventana = ventana_modulo.WebReportsWindow(tmp_path)
    try:
        ventana._al_recibir([resultado.correccion.excepcion])
        ventana._al_corregir([resultado])

        assert "Corregidas 0 de 1" in ventana.resumen.text()
        assert "quedó pendiente de corrección" in ventana.resumen.text()
        assert len(avisos) == 1
        assert "2008159: Reporte desactualizado:" in avisos[0]
    finally:
        ventana.close()


def test_limpiar_y_recargar_un_reporte_viejo_no_vuelve_a_borrar(tmp_path):
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    corrector.auditoria = tmp_path / "auditoria.jsonl"
    pagina = _PaginaCompleta(_REJILLA, [_REJILLA[1]])
    primera = corrector._borrar(
        pagina, _correccion(), copias_en(_REJILLA, "2008159"), ensayo=False,
    )
    assert primera.hecho

    # El servidor entrega otra vez DUPLICATED (2x) despues de la limpieza.
    plan_viejo = _correccion()
    ahora = copias_en(corrector._rejilla(pagina), "2008159")
    segunda = corrector._borrar(pagina, plan_viejo, ahora, ensayo=False)

    assert not segunda.hecho
    assert segunda.detalle.startswith("Reporte desactualizado:")
    assert sum("onDeletePage" in orden for orden in pagina.ordenes) == 1
    assert corrector.auditoria.read_text(encoding="utf-8").count(
        '"evento": "borrado_solicitado"',
    ) == 1


@pytest.mark.parametrize("estado,esperado", [
    ({"total": 1, "visibles": 1, "filas": 1}, True),
    ({"total": "1", "visibles": "1", "filas": 1}, True),
    ({"total": 2, "visibles": 1, "filas": 1}, False),
    ({"total": 2, "visibles": 2, "filas": 2}, False),
    ({"total": 1, "visibles": 1, "filas": 0}, False),
    ({"total": None, "visibles": 1, "filas": 1}, False),
    ({"total": True, "visibles": 1, "filas": 1}, False),
    ({"total": "", "visibles": 1, "filas": 1}, False),
    ({"total": 1, "visibles": 1, "filas": 1, "cargando": True}, False),
    ({"total": 1, "visibles": 1, "filas": 1, "tablas": 2}, False),
    ({"total": 1, "visibles": 1, "filas": 1, "error": True}, False),
])
def test_el_javascript_distingue_una_copia_de_una_pagina_parcial(app, estado, esperado):
    from PySide6.QtQml import QJSEngine

    motor = QJSEngine()
    entorno = "var estado = " + json.dumps(estado) + ";" + """
      var tabla = {
        grid: {hDiv: {loading: estado.cargando}},
        querySelectorAll: function() { return {length: estado.filas}; }
      };
      var document = {querySelectorAll: function() {
        var tablas = [];
        for (var i = 0; i < (estado.tablas || 1); i++) tablas.push(tabla);
        return tablas;
      }};
      var jQuery = function() { return {jqGrid: function(metodo, parametro) {
        if (estado.error) throw Error('rejilla ilegible');
        return parametro === 'records' ? estado.total : estado.visibles;
      }}; };
    """
    preparado = motor.evaluate(entorno)
    assert not preparado.isError(), preparado.toString()

    actual = motor.evaluate(modulo._REJILLA_CON_UNA_FILA)

    assert not actual.isError(), actual.toString()
    assert actual.isBool()
    assert actual.toBool() is esperado
