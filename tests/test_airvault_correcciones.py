"""Plan de corrección de las excepciones de Log Page Audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.airvault import correcciones as modulo_correcciones
from app.airvault.config import AirVaultConfig
from app.airvault.correcciones import (
    ACCION_BORRAR,
    ACCION_REINDEXAR,
    ACCION_REVISAR,
    ControlNoEncontrado,
    CorrectorLogPageAudit,
    _NavegadorDeCorrecciones,
    copias_en,
    planificar,
    por_antiguedad,
    resumen_del_plan,
)
from app.airvault.mapping import ResolutorFlota
from app.airvault.web_reports import parsear_filas


def _fila(matricula: str, detalle: str) -> list[dict[str, str]]:
    return [
        {"t": matricula},
        {"t": "Copa-7 (50)"},
        {"t": ""},
        {"t": "2008150 - 2008199"},
        {"t": "1/1/2025 - 1/31/2025"},
        {"t": ""},
        {"t": ""},
        {"t": ""},
        {"t": ""},
        {"t": detalle},
    ]


def _excepciones(*detalles: tuple[str, str]):
    return parsear_filas(
        [_fila(matricula, detalle) for matricula, detalle in detalles],
        AirVaultConfig(),
    )


def test_una_duplicada_conserva_una_copia_y_borra_el_resto() -> None:
    plan = planificar(
        _excepciones(("HP-9913CMP", "DUPLICATED 2008159(3x)"))
    )

    assert [correccion.accion for correccion in plan] == [ACCION_BORRAR]
    assert plan[0].sobran == 2
    assert "2008159" in plan[0].descripcion


def test_la_duplicada_de_dos_copias_habla_de_una_sola_que_sobra() -> None:
    """Es el caso corriente del reporte y se leía «borrar las 1 restantes»."""
    plan = planificar(
        _excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)"))
    )

    assert plan[0].sobran == 1
    assert "borrar la que sobra" in plan[0].descripcion
    assert "1 restantes" not in plan[0].descripcion


def test_el_ensayo_del_borrado_tambien_habla_de_una_sola_copia() -> None:
    """El ensayo es lo que se lee antes de autorizar; tiene que leerse bien."""
    correccion = planificar(
        _excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)"))
    )[0]
    rejilla = [
        _fila_de_rejilla("1", "551", "2008159", "HP-9913CMP", "1/9/2025 9:00:00 AM"),
        _fila_de_rejilla("2", "552", "2008159", "HP-9913CMP", "2/9/2025 9:00:00 AM"),
    ]
    pagina = _PaginaFalsa(rejilla)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())

    resultado = corrector._borrar(
        pagina,
        correccion,
        copias_en(corrector._rejilla(pagina), "2008159"),
        ensayo=True,
    )

    assert "se borraría una copia" in resultado.detalle
    assert "borrarían 1" not in resultado.detalle
    # Y el ensayo no escribe: ni abre el cuadro de borrado.
    assert not any("deletePageDialog" in orden for orden in pagina.ordenes)


def test_la_duplicada_de_mas_copias_dice_cuantas_sobran() -> None:
    plan = planificar(
        _excepciones(("HP-9913CMP", "DUPLICATED 2008159(4x)"))
    )

    assert "conservar la más antigua de 4 copias" in plan[0].descripcion
    assert "borrar las 3 que sobran" in plan[0].descripcion


def test_una_mal_indexada_sale_del_reporte_sin_adivinar_nada() -> None:
    """Las dos matrículas vienen en la fila: la del libro y la de destino."""
    plan = planificar(
        _excepciones(
            ("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]")
        )
    )

    assert plan[0].accion == ACCION_REINDEXAR
    assert plan[0].matricula_actual == "HP-9813CMP"
    assert plan[0].matricula_correcta == "HP-9913CMP"


def test_una_duplicada_sin_cuantas_copias_hay_queda_para_revisar() -> None:
    # Sin el «(3x)» el reporte no dice cuántas sobran, y «borrar las demás»
    # sin saber cuántas son es lo que no puede decidir sola la corrección.
    excepciones = _excepciones(("HP-9913CMP", "DUPLICATED 2008159(1x)"))

    plan = planificar(excepciones)

    assert plan[0].accion == ACCION_REVISAR
    assert "cuántas copias" in plan[0].motivo


def test_una_bitacora_repetida_y_mal_indexada_espera_a_la_otra_pasada() -> None:
    """Quitar la copia sobrante puede arreglar de paso el mal indexado."""
    plan = planificar(
        _excepciones(
            ("HP-9913CMP", "DUPLICATED 2008159(2x)"),
            ("HP-9913CMP", "2008159 MIS-INDEX to ACN [HP-9813CMP]"),
        )
    )

    acciones = [correccion.accion for correccion in plan]
    assert acciones == [ACCION_BORRAR, ACCION_REVISAR]
    assert "repetida" in plan[1].motivo


def test_las_duplicadas_se_corrigen_antes_que_las_mal_indexadas() -> None:
    plan = planificar(
        _excepciones(
            ("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]"),
            ("HP-1523CMP", "DUPLICATED 2284906(2x)"),
        )
    )

    assert [correccion.accion for correccion in plan] == [
        ACCION_BORRAR,
        ACCION_REINDEXAR,
    ]


def test_el_resumen_cuenta_copias_y_paginas_por_separado() -> None:
    plan = planificar(
        _excepciones(
            ("HP-9913CMP", "DUPLICATED 2008159(3x)"),
            ("HP-1523CMP", "DUPLICATED 2284906(2x)"),
            ("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]"),
            ("HP-9913CMP", "DUPLICATED 2008161(1x)"),
        )
    )

    texto = resumen_del_plan(plan)

    # Tres copias sobrantes (2 + 1) de dos bitácoras, y una reindexada.
    assert "borrar 3 copias sobrantes de 2 bitácoras" in texto
    assert "reindexar 1 página mal indexada" in texto
    assert "Queda 1 caso para revisar" in texto


def test_sin_nada_que_corregir_el_resumen_lo_dice() -> None:
    assert "No hay nada que corregir" in resumen_del_plan([])


# ── lo que se lee de la pantalla de búsqueda ────────────────────────

# Una fila de Web Search tal y como llega: el registro que guarda jqGrid,
# con sus nombres. «fila» es el número de orden dentro de la página y no
# identifica nada; el documento es «clave» (el DocKey de AirVault).


def _fila_de_rejilla(
    fila: str,
    documento: str,
    log: str,
    matricula: str,
    cuando: str,
) -> dict[str, str]:
    return {
        "fila": fila,
        "clave": f"1_3209_{documento}_1_1_0",
        "documento": documento,
        "log": log,
        "matricula": matricula,
        "cuando": cuando,
        "imagenes": "1",
        "tipo": "LOG PAGE",
    }


@pytest.fixture(autouse=True)
def auditoria_temporal(monkeypatch, tmp_path):
    inicializar = CorrectorLogPageAudit.__init__
    def iniciar(self, *args, **kwargs):
        inicializar(self, *args, **kwargs)
        self.auditoria = tmp_path / "auditoria.jsonl"
    monkeypatch.setattr(CorrectorLogPageAudit, "__init__", iniciar)


_REJILLA = [
    _fila_de_rejilla("1", "884", "2008159", "HP-9913CMP", "3/14/2025 9:12:00 AM"),
    _fila_de_rejilla("2", "231", "2008159", "HP-9913CMP", "1/9/2025 2:05:00 PM"),
    _fila_de_rejilla("3", "907", "2008160", "HP-9913CMP", "2/2/2025 8:00:00 AM"),
]


class _PaginaFalsa:
    """Una pantalla de Web Search que contesta lo que se le diga.

    Guarda las órdenes que recibe: lo que se le pide es la mitad de lo que
    hay que comprobar, porque una orden que sale con el documento
    equivocado se ve aquí y no en el resultado.
    """

    def __init__(self, *lecturas: list[dict[str, str]]) -> None:
        self._lecturas = list(lecturas)
        self.ordenes: list[str] = []
        self.aviso = ""

    def esperar(self, _condicion, _segundos, cada: float = 0.5) -> bool:
        return True

    def evaluar(self, expresion: str):
        self.ordenes.append(expresion)
        if expresion == modulo_correcciones._LEER_REJILLA:
            if len(self._lecturas) > 1:
                return self._lecturas.pop(0)
            return self._lecturas[0]
        if expresion == modulo_correcciones._MENSAJE:
            return self.aviso
        return "OK"


def test_solo_se_toman_las_filas_de_esa_bitacora() -> None:
    copias = copias_en(_REJILLA, "2008159")

    assert len(copias) == 2
    assert {copia.matricula for copia in copias} == {"HP-9913CMP"}


def test_se_conserva_la_copia_mas_antigua() -> None:
    """La fecha viene en su campo, no en la columna que toque ese día."""
    se_queda, sobran = por_antiguedad(copias_en(_REJILLA, "2008159"))

    assert se_queda is not None
    assert se_queda.cuando.month == 1
    assert [copia.cuando.month for copia in sobran] == [3]


def test_sin_fecha_legible_no_se_borra_ninguna_copia() -> None:
    rejilla = [
        _fila_de_rejilla("1", "884", "2008159", "HP-9913CMP", ""),
        _fila_de_rejilla("2", "231", "2008159", "HP-9913CMP", ""),
    ]

    se_queda, sobran = por_antiguedad(copias_en(rejilla, "2008159"))

    assert se_queda is None
    assert sobran == []


def test_una_sola_copia_no_deja_nada_por_borrar() -> None:
    se_queda, sobran = por_antiguedad(copias_en(_REJILLA, "2008160"))

    assert se_queda is None
    assert sobran == []


@pytest.mark.parametrize("campo,valor", [("imagenes", "2"), ("imagenes", None), ("imagenes", "1 of 2"), ("tipo", "FLEET"), ("tipo", "")])
def test_documentos_de_varias_imagenes_o_de_otra_area_no_se_eliminan(campo, valor):
    filas = [dict(f) for f in _REJILLA]
    filas[0][campo] = valor
    correccion = planificar(_excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)")))[0]
    pagina = _PaginaFalsa(filas)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    resultado = corrector._borrar(pagina, correccion, copias_en(filas, "2008159"), False)
    assert not resultado.hecho
    assert "otra área" in resultado.detalle
    assert not any("onDeletePage" in orden for orden in pagina.ordenes)


def test_si_se_agrega_una_imagen_despues_del_preview_no_se_borra():
    filas = [dict(f) for f in _REJILLA]
    filas[0]["imagenes"] = "2"
    pagina = _PaginaFalsa(filas)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    correccion = planificar(_excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)")))[0]
    with pytest.raises(ControlNoEncontrado, match="cambió"):
        corrector._borrar(pagina, correccion, copias_en(_REJILLA, "2008159"), False)
    assert not any("onDeletePage" in orden for orden in pagina.ordenes)


def test_preview_permite_conservar_la_copia_nueva(monkeypatch, tmp_path):
    pagina = _PaginaFalsa(_REJILLA, [_REJILLA[0]])
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    corrector.auditoria = tmp_path / "auditoria.jsonl"
    monkeypatch.setattr(corrector, "_imagen_previa", lambda *_: b"png")
    corrector.revisar = lambda c, vistas, elegidas: {vistas[1][0].clave}
    correccion = planificar(_excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)")))[0]
    resultado = corrector._borrar(pagina, correccion, copias_en(_REJILLA, "2008159"), False)
    assert resultado.hecho
    pedidas = [orden for orden in pagina.ordenes if "onDeletePage" in orden]
    assert len(pedidas) == 1 and ')("1_3209_231_1_1_0"' in pedidas[0]
    assert 'borrado_verificado' in corrector.auditoria.read_text(encoding="utf-8")


def test_omitir_preview_no_abre_borrado(monkeypatch):
    pagina = _PaginaFalsa(_REJILLA)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())
    monkeypatch.setattr(corrector, "_imagen_previa", lambda *_: b"png")
    corrector.revisar = lambda *args: None
    correccion = planificar(_excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)")))[0]
    assert not corrector._borrar(pagina, correccion, copias_en(_REJILLA, "2008159"), False).hecho
    assert not any("onDeletePage" in orden for orden in pagina.ordenes)


def test_cada_copia_se_queda_con_la_clave_de_su_documento() -> None:
    """Borrar por posición cae en otro documento en cuanto falta una fila.

    El id del ``<tr>`` que monta jqGrid es el número de orden dentro de la
    página: dos consultas seguidas de la misma bitácora devuelven las mismas
    copias en distinto orden, y al borrar una las de abajo se corren. Lo que
    identifica al documento es su DocKey.
    """
    se_queda, sobran = por_antiguedad(copias_en(_REJILLA, "2008159"))

    assert se_queda.clave == "1_3209_231_1_1_0"
    assert [copia.clave for copia in sobran] == ["1_3209_884_1_1_0"]


def test_la_rejilla_se_lee_entera_tal_y_como_llega() -> None:
    pagina = _PaginaFalsa(_REJILLA)

    leida = CorrectorLogPageAudit._rejilla(pagina)

    assert leida == _REJILLA
    assert copias_en(leida, "2008159")[0].documento == "884"


def test_el_borrado_llega_hasta_airvault_y_se_comprueba_despues() -> None:
    correccion = planificar(
        _excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)"))
    )[0]
    queda = [_REJILLA[1], _REJILLA[2]]
    pagina = _PaginaFalsa(_REJILLA, _REJILLA, queda)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())

    resultado = corrector._borrar(
        pagina,
        correccion,
        copias_en(corrector._rejilla(pagina), "2008159"),
        ensayo=False,
    )

    assert resultado.hecho
    assert "Borrada 1 copia" in resultado.detalle
    # Se pidió por la clave de la copia que sobra, que es la más nueva.
    pedidas = [orden for orden in pagina.ordenes if "onDeletePage" in orden]
    assert len(pedidas) == 1
    assert "1_3209_884_1_1_0" in pedidas[0]


def test_un_borrado_que_no_borro_no_se_da_por_hecho() -> None:
    """AirVault puede aceptar la orden y no llegar a borrar nada."""
    correccion = planificar(
        _excepciones(("HP-9913CMP", "DUPLICATED 2008159(2x)"))
    )[0]
    pagina = _PaginaFalsa(_REJILLA)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())

    resultado = corrector._borrar(
        pagina,
        correccion,
        copias_en(corrector._rejilla(pagina), "2008159"),
        ensayo=False,
    )

    assert not resultado.hecho
    assert "todavía aparecen 2" in resultado.detalle


def test_el_reindexado_escribe_la_matricula_y_la_flota_que_le_toca() -> None:
    correccion = planificar(
        _excepciones(("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]"))
    )[0]
    antes = [
        _fila_de_rejilla("1", "551", "2008152", "HP-9813CMP", "1/9/2025 9:00:00 AM")
    ]
    despues = [
        _fila_de_rejilla("1", "551", "2008152", "HP-9913CMP", "1/9/2025 9:00:00 AM")
    ]
    pagina = _PaginaFalsa(antes, despues)
    corrector = CorrectorLogPageAudit(
        AirVaultConfig(), ResolutorFlota({"HP-9913CMP": {"fleet": "MAX"}})
    )

    resultado = corrector._reindexar(
        pagina,
        correccion,
        copias_en(corrector._rejilla(pagina), "2008152"),
        ensayo=False,
    )

    assert resultado.hecho
    assert "HP-9913CMP" in resultado.detalle
    escritas = [
        orden for orden in pagina.ordenes if "C_ACREG" in orden
    ]
    assert escritas and '"HP-9913CMP"' in escritas[0]
    # Mover una página de una HP-15 a una HP-99 le cambia la flota, y
    # dejarle la de antes sería cambiar un dato malo por otro.
    assert '"MAX"' in escritas[0]


class _PaginaQuePregunta(_PaginaFalsa):
    """Una que no cierra el cuadro hasta que alguien contesta «Continue».

    AirVault se para a medio guardar y pregunta, pero no en el instante en
    que se pulsa «Save»: el aviso llega cuando contesta el servidor.
    """

    def __init__(self, *lecturas: list[dict[str, str]]) -> None:
        super().__init__(*lecturas)
        self.contestado = False

    def esperar(self, condicion, segundos, cada: float = 0.5) -> bool:
        cerrado = modulo_correcciones._cuadro_cerrado("reindexDialog")
        if str(condicion) == cerrado:
            return self.contestado
        return True

    def evaluar(self, expresion: str):
        if expresion.startswith(modulo_correcciones._SEGUIR_GUARDANDO[:40]):
            self.contestado = True
        return super().evaluar(expresion)


def test_el_reindexado_contesta_el_aviso_que_llega_despues_de_guardar() -> None:
    correccion = planificar(
        _excepciones(("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]"))
    )[0]
    antes = [
        _fila_de_rejilla("1", "551", "2008152", "HP-9813CMP", "1/9/2025 9:00:00 AM")
    ]
    despues = [
        _fila_de_rejilla("1", "551", "2008152", "HP-9913CMP", "1/9/2025 9:00:00 AM")
    ]
    pagina = _PaginaQuePregunta(antes, despues)
    corrector = CorrectorLogPageAudit(
        AirVaultConfig(), ResolutorFlota({"HP-9913CMP": {"fleet": "MAX"}})
    )

    resultado = corrector._reindexar(
        pagina,
        correccion,
        copias_en(corrector._rejilla(pagina), "2008152"),
        ensayo=False,
    )

    assert pagina.contestado
    assert resultado.hecho


def test_un_reindexado_que_no_cuajo_no_se_da_por_hecho() -> None:
    correccion = planificar(
        _excepciones(("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]"))
    )[0]
    sin_cambiar = [
        _fila_de_rejilla("1", "551", "2008152", "HP-9813CMP", "1/9/2025 9:00:00 AM")
    ]
    pagina = _PaginaFalsa(sin_cambiar)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())

    resultado = corrector._reindexar(
        pagina,
        correccion,
        copias_en(corrector._rejilla(pagina), "2008152"),
        ensayo=False,
    )

    assert not resultado.hecho
    assert "HP-9813CMP" in resultado.detalle


def test_un_aviso_de_airvault_detiene_ese_caso_sin_tocar_nada() -> None:
    """«Locked by another user» es un motivo, no un fallo del programa."""
    correccion = planificar(
        _excepciones(("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]"))
    )[0]
    rejilla = [
        _fila_de_rejilla("1", "551", "2008152", "HP-9813CMP", "1/9/2025 9:00:00 AM")
    ]
    pagina = _PaginaFalsa(rejilla)
    pagina.aviso = "The specified document page is locked by another user."
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())

    with pytest.raises(ControlNoEncontrado) as fallo:
        corrector._reindexar(
            pagina,
            correccion,
            copias_en(corrector._rejilla(pagina), "2008152"),
            ensayo=False,
        )

    assert "locked by another user" in str(fallo.value)
    # Y no se llegó a escribir nada: el aviso llega en lugar del cuadro.
    assert not any("C_ACREG" in orden for orden in pagina.ordenes)

def test_un_caso_que_se_corta_suelta_el_documento() -> None:
    """El cuadro abierto deja el documento tomado para todo el mundo."""
    correccion = planificar(
        _excepciones(("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]"))
    )[0]
    rejilla = [
        _fila_de_rejilla(
            "1", "551", "2008152", "HP-9813CMP", "1/9/2025 9:00:00 AM"
        )
    ]

    class _PaginaQueNoGuarda(_PaginaFalsa):
        def evaluar(self, expresion: str):
            respuesta = super().evaluar(expresion)
            if "C_ACREG" in expresion:
                return "AirVault no ofrece HP-9913CMP en C_ACREG"
            return respuesta

    pagina = _PaginaQueNoGuarda(rejilla)
    corrector = CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota())

    with pytest.raises(ControlNoEncontrado):
        corrector._reindexar(
            pagina,
            correccion,
            copias_en(corrector._rejilla(pagina), "2008152"),
            ensayo=False,
        )

    assert any(
        "ui-dialog-titlebar-close" in orden for orden in pagina.ordenes
    )


def test_la_correccion_entra_por_el_enlace_federado(monkeypatch) -> None:
    """Por la raiz sale el formulario local, que en la empresa nadie usa."""
    entradas: list[str] = []

    class _NavegadorFalso:
        def __init__(self, _perfil):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def abrir(self, url, espera_s):
            entradas.append(url)
            return {"webSocketDebuggerUrl": "ws://x"}

        def cookies(self, _version):
            return {"airvault.criticaltech.com": [
                {"name": "Critical", "value": "x"}
            ]}

    monkeypatch.setattr(
        modulo_correcciones, "_NavegadorDeCorrecciones", _NavegadorFalso
    )
    monkeypatch.setattr(
        CorrectorLogPageAudit,
        "_un_caso",
        lambda self, *_args: modulo_correcciones.Resultado(_args[1]),
    )
    config = AirVaultConfig()
    plan = planificar(
        _excepciones(("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]"))
    )

    CorrectorLogPageAudit(config, ResolutorFlota()).aplicar(plan)

    assert entradas == [config.url_sso]


def test_la_correccion_usa_una_pestana_de_fondo_y_deja_edge_abierto(
    monkeypatch, tmp_path: Path
) -> None:
    """Una ventana abierta desde el reporte pertenece a la persona."""
    version = {
        "User-Agent": "Mozilla/5.0 Edg/151",
        "webSocketDebuggerUrl": "ws://127.0.0.1:4321/x",
    }
    pedidos: list[tuple[str, dict]] = []

    class _WebSocketFalso:
        def __init__(self, _url):
            pass

        def pedir(self, metodo, **parametros):
            pedidos.append((metodo, parametros))
            return {"targetId": "temporal"}

        def cerrar(self):
            pass

    class _SesionProhibida:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("no debe abrir otro Edge")

    monkeypatch.setattr(
        modulo_correcciones, "_puerto_anotado", lambda _perfil: 4321
    )
    monkeypatch.setattr(
        modulo_correcciones, "_version_en", lambda _puerto: version
    )
    monkeypatch.setattr(
        modulo_correcciones, "_edges_del_perfil", lambda _perfil: []
    )
    monkeypatch.setattr(modulo_correcciones, "_WebSocket", _WebSocketFalso)
    monkeypatch.setattr(
        modulo_correcciones, "SesionDeNavegador", _SesionProhibida
    )

    with _NavegadorDeCorrecciones(tmp_path) as navegador:
        encontrada = navegador.abrir("https://airvault", espera_s=30.0)
        target_id = navegador.abrir_pestana(
            "https://airvault/busqueda", encontrada
        )

    assert target_id == "temporal"
    assert pedidos == [
        (
            "Target.createTarget",
            {"url": "https://airvault/busqueda", "background": True},
        )
    ]


def test_la_correccion_sin_edge_visible_abre_uno_oculto(
    monkeypatch, tmp_path: Path
) -> None:
    estados: list[object] = []

    class _SesionFalsa:
        def __init__(self, _perfil, visible=True):
            estados.append(("visible", visible))

        def abrir(self, url, espera_s):
            estados.append(("abrir", url, espera_s))
            return {"webSocketDebuggerUrl": "ws://oculto"}

        def abrir_pestana(self, url, version):
            estados.append(("pestaña", url, version))
            return "temporal"

        def cerrar(self):
            estados.append("cerrar")

    monkeypatch.setattr(
        modulo_correcciones, "_puerto_anotado", lambda _perfil: None
    )
    monkeypatch.setattr(
        modulo_correcciones, "_edges_del_perfil", lambda _perfil: []
    )
    monkeypatch.setattr(
        modulo_correcciones, "SesionDeNavegador", _SesionFalsa
    )

    with _NavegadorDeCorrecciones(tmp_path) as navegador:
        version = navegador.abrir("https://airvault", espera_s=30.0)
        navegador.abrir_pestana("https://airvault/busqueda", version)

    assert estados[0] == ("visible", False)
    assert estados[-1] == "cerrar"


def test_la_correccion_cuenta_las_bitacoras_que_abre_en_edge(
    monkeypatch,
) -> None:
    """La cuenta mueve el cronometro, y solo cuenta lo que cuesta tiempo."""
    pasos: list[tuple[int, int]] = []

    class _NavegadorFalso:
        def __init__(self, _perfil):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def abrir(self, url, espera_s):
            return {"webSocketDebuggerUrl": "ws://x"}

        def cookies(self, _version):
            return {"airvault.criticaltech.com": [
                {"name": "Critical", "value": "x"}
            ]}

    monkeypatch.setattr(
        modulo_correcciones, "_NavegadorDeCorrecciones", _NavegadorFalso
    )
    monkeypatch.setattr(
        CorrectorLogPageAudit,
        "_un_caso",
        lambda self, *_args: modulo_correcciones.Resultado(_args[1]),
    )
    plan = planificar(
        _excepciones(
            ("HP-9913CMP", "DUPLICATED 2008159(2x)"),
            ("HP-9913CMP", "2008152 MIS-INDEX to ACN [HP-9813CMP]"),
            # Esta queda para revisar a mano: se resuelve sin navegador y no
            # tiene por que estirar el pronostico de las que si lo usan.
            ("HP-9913CMP", "DUPLICATED 2008170(1x)"),
        )
    )

    CorrectorLogPageAudit(AirVaultConfig(), ResolutorFlota()).aplicar(
        plan,
        progreso=lambda hechas, total: pasos.append((hechas, total)),
    )

    assert pasos == [(0, 2), (1, 2), (2, 2)]
