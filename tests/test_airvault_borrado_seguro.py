"""El borrado usa la seleccion real de AirVault y conserva la original."""

import json

import pytest
from PySide6.QtQml import QJSEngine

from app.airvault import correcciones as modulo
from app.airvault.config import AirVaultConfig
from app.airvault.correcciones import ControlNoEncontrado, CorrectorLogPageAudit, copias_en
from app.airvault.mapping import ResolutorFlota
from tests.test_airvault_correcciones import _excepciones, _PaginaFalsa, _REJILLA, _fila_de_rejilla


_ENTORNO = r"""
var filas = [
  {id: '1', datos: {DocKey: 'original', DocID: '101', LogNo: '2008159',
                   ImageCount: 1, DocType: 'LOG PAGE'}},
  {id: '2', datos: {DocKey: 'sobrante', DocID: '102', LogNo: '2008159',
                   ImageCount: 1, DocType: 'LOG PAGE'}}
];
var seleccion = ['1'];
var ultima = '1';
var abiertas = [];
var borradas = [];
var ordenes = [];
var tabla = {};
var todas = {checked: false, click: function() { this.checked = true; }};
var boton = {innerText: 'Delete', offsetParent: {}, click: function() {
  borradas = abiertas.slice();
}};
var cuadro = {offsetParent: {}, querySelector: function() { return todas; }};
var document = {
  querySelectorAll: function(selector) {
    return selector === '.ui-jqgrid-btable' ? [tabla] : filas;
  },
  getElementById: function(id) {
    return id === 'rdAllPages' ? todas : cuadro;
  }
};
var jQuery = function(elemento) {
  return {
    closest: function(selector) {
      return jQuery(selector === '.ui-dialog' ? cuadro : tabla);
    },
    jqGrid: function(metodo, valor) {
      ordenes.push(metodo);
      if (metodo === fallo) throw Error('selección ilegible');
      if (metodo === ignorado) return this;
      if (metodo === 'resetSelection') { seleccion = []; ultima = null; }
      if (metodo === 'setSelection') {
        var posicion = seleccion.indexOf(valor);
        if (posicion < 0) seleccion.push(valor);
        else seleccion.splice(posicion, 1);
        ultima = valor;
      }
      if (metodo === 'getGridParam') {
        if (valor === 'selarrrow') return seleccion;
        if (valor === 'selrow') return ultima;
        if (valor === 'multiselect') return true;
      }
      return this;
    },
    find: function() { return {each: function(callback) {
      callback.call(boton);
    }}; }
  };
};
jQuery.getResultRowData = function(id) {
  return filas.filter(function(fila) { return fila.id === id; })[0].datos;
};
var window = {onDeletePage: function() {
  abiertas = seleccion.map(function(id) {
    return jQuery.getResultRowData(id).DocKey;
  });
}};
var fallo = '', ignorado = '';
"""


def _motor():
    motor = QJSEngine()
    assert not motor.evaluate(_ENTORNO).isError()
    return motor


def _abrir(motor, original="original"):
    respuesta = motor.evaluate(modulo._ABRIR_CUADRO % (
        json.dumps("sobrante"), json.dumps("onDeletePage"), json.dumps(original),
    ))
    assert not respuesta.isError(), respuesta.toString()
    return respuesta.toString()


def _confirmar(motor):
    respuesta = motor.evaluate(modulo._CONFIRMAR_BORRADO % (
        json.dumps("sobrante"), json.dumps("original"),
        json.dumps(list(modulo.TEXTOS_BORRAR)),
    ))
    assert not respuesta.isError(), respuesta.toString()
    return respuesta.toString()


@pytest.mark.parametrize("previa", [[], ["1"], ["2"], ["1", "2"]])
def test_la_seleccion_previa_no_incluye_la_original_en_el_borrado(app, previa):
    motor = _motor()
    motor.evaluate("seleccion = " + json.dumps(previa))

    assert _abrir(motor) == "OK"
    assert _confirmar(motor) == "OK"
    assert motor.evaluate("borradas").toVariant() == ["sobrante"]


@pytest.mark.parametrize("metodo", ["resetSelection", "setSelection", "getGridParam"])
def test_una_seleccion_ilegible_no_abre_el_borrado(app, metodo):
    motor = _motor()
    motor.evaluate("fallo = " + json.dumps(metodo))

    assert _abrir(motor) != "OK"
    assert motor.evaluate("abiertas").toVariant() == []
    assert motor.evaluate("borradas").toVariant() == []


@pytest.mark.parametrize("metodo", ["resetSelection", "setSelection"])
def test_una_seleccion_que_no_se_limpia_o_no_se_aplica_no_borra(app, metodo):
    motor = _motor()
    motor.evaluate("ignorado = " + json.dumps(metodo))

    assert _abrir(motor) != "OK"
    assert motor.evaluate("abiertas").toVariant() == []


@pytest.mark.parametrize("cambio", [
    "filas.splice(0, 1)",
    "filas.push(filas[0])",
    "filas.push(filas[1])",
    "filas[1].datos.DocID = '101'",
    "filas[0].datos.DocID = ''",
    "filas[0].datos.LogNo = '2008160'",
    "filas[0].datos.ImageCount = 2",
    "filas[1].datos.DocType = 'FLEET'",
])
def test_sin_original_distinta_y_comprobada_no_abre_el_borrado(app, cambio):
    motor = _motor()
    motor.evaluate(cambio)

    assert _abrir(motor) != "OK"
    assert motor.evaluate("abiertas").toVariant() == []


@pytest.mark.parametrize("original", ["", "sobrante", "inexistente"])
def test_es_obligatorio_identificar_la_copia_que_se_conserva(app, original):
    motor = _motor()

    assert _abrir(motor, original) != "OK"
    assert motor.evaluate("abiertas").toVariant() == []


@pytest.mark.parametrize("cambio", [
    "seleccion = ['1', '2']",
    "seleccion = ['1']; ultima = '1'",
    "seleccion = []",
    "filas.splice(0, 1)",
    "filas[0].datos.ImageCount = 2",
    "cuadro.offsetParent = null",
    "fallo = 'getGridParam'",
    "todas.click = function() { seleccion.push('1'); this.checked = true; }",
])
def test_un_cambio_entre_abrir_y_confirmar_detiene_el_borrado(app, cambio):
    motor = _motor()
    assert _abrir(motor) == "OK"
    motor.evaluate(cambio)

    assert _confirmar(motor) != "OK"
    assert motor.evaluate("borradas").toVariant() == []


def test_el_modo_de_seleccion_simple_tambien_aisla_la_copia(app):
    motor = _motor()
    motor.evaluate("""
      var jqueryAnterior = jQuery;
      jQuery = function(elemento) {
        var rejilla = jqueryAnterior(elemento), consultar = rejilla.jqGrid;
        rejilla.jqGrid = function(metodo, valor) {
          if (metodo === 'getGridParam' && valor === 'multiselect') return false;
          if (metodo === 'getGridParam' && valor === 'selarrrow') return [];
          return consultar.call(this, metodo, valor);
        };
        return rejilla;
      };
      jQuery.getResultRowData = jqueryAnterior.getResultRowData;
    """)

    assert _abrir(motor) == "OK"
    assert _confirmar(motor) == "OK"
    assert motor.evaluate("borradas").toVariant() == ["sobrante"]


@pytest.mark.parametrize("campo,valor", [
    ("clave", ""), ("documento", ""), ("documento", _REJILLA[0]["documento"]),
])
def test_la_original_sin_identidad_unica_no_autoriza_ningun_borrado(campo, valor, tmp_path):
    filas = [dict(f) for f in _REJILLA]
    filas[1][campo] = valor
    pagina = _PaginaFalsa(filas)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    corrector.auditoria = tmp_path / "auditoria.jsonl"
    correccion = modulo.planificar(_excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)")))[0]

    resultado = corrector._borrar(pagina, correccion, copias_en(filas, "2008159"), False)

    assert not resultado.hecho
    assert not any("onDeletePage" in orden for orden in pagina.ordenes)
    assert not corrector.auditoria.exists()


@pytest.mark.parametrize("quedan", [[], _REJILLA[:2], [dict(_REJILLA[1], matricula="HP-9813CMP")]])
def test_no_confirma_un_borrado_sin_la_original_intacta(quedan, tmp_path):
    pagina = _PaginaFalsa(_REJILLA, quedan)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    corrector.auditoria = tmp_path / "auditoria.jsonl"
    correccion = modulo.planificar(_excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)")))[0]

    resultado = corrector._borrar(pagina, correccion, copias_en(_REJILLA, "2008159"), False)

    assert not resultado.hecho
    assert "No se pudo confirmar" in resultado.detalle
    assert '"evento": "borrado_verificado"' not in corrector.auditoria.read_text(encoding="utf-8")


@pytest.mark.parametrize("cambio", ["ninguno", "desaparece_original", "desaparece_otra"])
def test_comprueba_el_resultado_de_la_primera_copia_antes_de_borrar_la_siguiente(
    monkeypatch, tmp_path, cambio,
):
    filas = _REJILLA[:2] + [
        _fila_de_rejilla("3", "999", "2008159", "HP-9913CMP", "4/2/2025 8:00:00 AM")
    ]
    presentes = list(filas)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    corrector.auditoria = tmp_path / "auditoria.jsonl"
    correccion = modulo.planificar(_excepciones(("HP-9913CMP", "DUPLICATED 2008159(3x)")))[0]
    borradas = []

    def borrar(_pagina, copia, conservada):
        assert copia.clave != conservada.clave
        borradas.append(copia.clave)
        if cambio != "ninguno":
            presentes.remove(filas[0])
            presentes.remove(filas[1] if cambio == "desaparece_original" else filas[2])

    monkeypatch.setattr(corrector, "_releer", lambda *_: copias_en(presentes, "2008159"))
    monkeypatch.setattr(corrector, "_borrar_copia", borrar)

    with pytest.raises(ControlNoEncontrado, match="cambió"):
        corrector._borrar(_PaginaFalsa(filas), correccion, copias_en(filas, "2008159"), False)

    assert borradas == [filas[0]["clave"]]


@pytest.mark.parametrize("cantidad", [2, 3, 8])
def test_varias_copias_se_borran_una_por_una_y_queda_la_original(monkeypatch, tmp_path, cantidad):
    filas = [
        _fila_de_rejilla(str(i), str(100 + i), "2008159", "HP-9913CMP", f"1/{i}/2025")
        for i in range(1, cantidad + 1)
    ]
    presentes = list(reversed(filas))
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    corrector.auditoria = tmp_path / "auditoria.jsonl"
    correccion = modulo.planificar(_excepciones(("HP-9913CMP", f"DUPLICATED 2008159({cantidad}x)")))[0]
    borradas = []

    def borrar(_pagina, copia, conservada):
        assert conservada.clave == filas[0]["clave"]
        assert copia.clave != conservada.clave
        assert len(presentes) >= 2
        borradas.append(copia.clave)
        presentes[:] = [f for f in presentes if f["clave"] != copia.clave]

    monkeypatch.setattr(corrector, "_releer", lambda *_: copias_en(presentes, "2008159"))
    monkeypatch.setattr(corrector, "_borrar_copia", borrar)

    resultado = corrector._borrar(_PaginaFalsa(filas), correccion, copias_en(filas, "2008159"), False)

    assert resultado.hecho
    assert len(borradas) == cantidad - 1
    assert presentes == [filas[0]]
