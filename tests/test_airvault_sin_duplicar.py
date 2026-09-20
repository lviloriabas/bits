"""Que la misma bitacora no se publique dos veces en AirVault.

Es el unico error de este modulo que no se deshace desde el programa: hay
que ir a borrar la copia a mano en Web Index. Las defensas que ya existian
miran la **cola** de Web Index, y ahi esta el hueco: completar un batch lo
saca de esa cola y lo manda a Web Search, asi que a partir de ese momento
ninguna consulta a la cola lo ve. Si ademas se reprocesan los escaneos en
otra carpeta, nada impedia volver a subirlo. Eliminar un batch expresamente
es distinto: tambien debe retirarlo de esta memoria para no dejar una alerta
falsa.

Lo que se comprueba aqui son las dos que si llegan a ese caso, el tope de
reenvios y lo que pasa cuando una de ellas salta.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.airvault import duplicados as libro_de_envios
from app.airvault.config import AirVaultConfig
from app.airvault.flujo import (
    POSIBLE_DUPLICADO,
    ErrorDeCorrida,
    Trabajo,
    autorizar_posible_duplicado,
    buscador_de,
    carpeta_del_libro,
    completar_partes,
    es_posible_duplicado,
    estado_local,
    limpiar_posible_duplicado,
    partes_por_subir,
    revisar_duplicado,
    subir_partes,
)
from app.airvault.model import EstadoEtapa
from app.airvault.websearch import Buscador
from tests.airvault_fake import ClienteFalso
from tests.test_airvault_comprobar import SesionFalsa, corrida


def _trabajo(tmp_path, entrega="hoy", carpeta="job-1", nombre="DP | BIT"):
    """Un batch preparado de verdad, con su CSV y su manifiesto.

    Todos los trabajos cuelgan de la misma carpeta de la instalacion, que
    es donde vive el libro de envios: la memoria es de la instalacion, no
    de una sola entrega.
    """
    csv = corrida(tmp_path / entrega, nombre=f"BITS {entrega}")
    trabajo = Trabajo.preparar(
        AirVaultConfig(detener_por_duplicados=True),
        tmp_path / "output" / "airvault" / carpeta,
        csv,
        nombre,
    )
    trabajo.guardar()
    return trabajo


def _dado_por_subido(trabajo) -> None:
    trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "entrega.pdf")
    trabajo.guardar()


class BuscadorFalso(Buscador):
    """Contesta lo que se le diga, sin red y sin descubrir nada."""

    def __init__(self, publicadas=()):
        super().__init__(sesion=object(), config=AirVaultConfig())
        self._publicadas = {str(n) for n in publicadas}
        self._probado = True
        self._ruta = "/zfp/Search/GetSearchResults"
        self._plantilla = "encodedValues"

    def _pedir(self, ruta, plantilla, valor):
        return {"rows": [{"C_LogNo": valor}] if valor in self._publicadas else []}


# ── el libro de envios, que siempre responde ───────────────────────

def test_una_bitacora_ya_mandada_en_otra_entrega_para_la_carga(tmp_path):
    """Reprocesar unos escaneos ya subidos y volver a mandarlos."""
    lunes = _trabajo(tmp_path, "lunes", "job-1", "DP | LUNES")
    _dado_por_subido(lunes)
    libro_de_envios.anotar(carpeta_del_libro(lunes), [lunes])
    # Los mismos escaneos procesados otra vez: otra entrega, otra carpeta,
    # otra numeracion, las mismas bitacoras.
    martes = _trabajo(tmp_path, "martes", "job-2", "DP | MARTES")

    motivo = revisar_duplicado(martes)

    assert "ya se mandaron a AirVault en otro batch" in motivo


def test_eliminar_un_batch_lo_saca_del_libro_de_posibles_duplicados(tmp_path):
    lunes = _trabajo(tmp_path, "lunes", "job-1", "DP | LUNES")
    _dado_por_subido(lunes)
    raiz = carpeta_del_libro(lunes)
    libro_de_envios.anotar(raiz, [lunes])
    martes = _trabajo(tmp_path, "martes", "job-2", "DP | MARTES")
    assert revisar_duplicado(martes)

    libro_de_envios.olvidar(raiz, [lunes.carpeta])

    assert revisar_duplicado(martes) == ""


def test_subir_se_niega_y_deja_la_marca_puesta(tmp_path):
    """Por ``Trabajo.subir`` pasan todas las cargas, venga de donde venga."""
    lunes = _trabajo(tmp_path, "lunes", "job-1", "DP | LUNES")
    _dado_por_subido(lunes)
    libro_de_envios.anotar(carpeta_del_libro(lunes), [lunes])
    martes = _trabajo(tmp_path, "martes", "job-2", "DP | MARTES")

    with pytest.raises(ErrorDeCorrida) as fallo:
        martes.subir(SesionFalsa())

    assert "dos veces en AirVault" in str(fallo.value)
    assert es_posible_duplicado(martes)
    # Y queda escrito, para que no dependa de que el proceso siga vivo.
    assert Trabajo.cargar(
        AirVaultConfig(), martes.carpeta
    ).manifiesto.posible_duplicado


def test_la_autorizacion_expresa_permite_reenviar_el_posible_duplicado(
    tmp_path, monkeypatch,
):
    from app.airvault import uploader
    from app.airvault.uploader import ResultadoSubida

    lunes = _trabajo(tmp_path, "lunes", "job-1", "DP | LUNES")
    _dado_por_subido(lunes)
    libro_de_envios.anotar(carpeta_del_libro(lunes), [lunes])
    martes = _trabajo(tmp_path, "martes", "job-2", "DP | MARTES")
    martes.manifiesto.posible_duplicado = revisar_duplicado(martes)
    martes.guardar()
    enviados = []

    class SubidorQueAcepta:
        def __init__(self, *args, **kwargs):
            pass

        def subir(self, ruta, valores, avisar=None):
            enviados.append(Path(ruta))
            return ResultadoSubida(str(ruta), True)

    monkeypatch.setattr(uploader, "SubidorQuickUpload", SubidorQueAcepta)
    monkeypatch.setattr(
        "app.airvault.flujo._paginas_del_pdf",
        lambda _ruta: len(martes.manifiesto.registros),
    )

    autorizar_posible_duplicado(martes)
    martes.subir(object())

    assert enviados == [Path(martes.manifiesto.pdf_origen)]
    assert martes.manifiesto.etapa_hecha("subir")
    assert not martes._duplicado_permitido


# ── Web Search, que ve lo que la cola ya no tiene ──────────────────

def test_un_batch_ya_publicado_no_se_vuelve_a_subir(tmp_path, monkeypatch):
    """El batch se completo y salio de la cola: la cola dice que no esta."""
    trabajo = _trabajo(tmp_path)
    numeros = [
        r.log_number for r in trabajo.manifiesto.registros if r.log_number
    ]
    subidas = []
    monkeypatch.setattr(
        Trabajo, "subir",
        lambda self, *a, **k: subidas.append(self.manifiesto.nombre_batch),
    )

    fallos = subir_partes(
        [trabajo], SesionFalsa(), cliente=ClienteFalso(),
        buscador=BuscadorFalso(numeros),
    )

    assert subidas == []
    assert es_posible_duplicado(trabajo)
    assert "Web Search ya tiene publicadas" in fallos[0][1]


def test_repetir_la_orden_autorizada_supera_la_alerta_de_web_search(
    tmp_path, monkeypatch,
):
    trabajo = _trabajo(tmp_path)
    numeros = [
        r.log_number for r in trabajo.manifiesto.registros if r.log_number
    ]
    trabajo.manifiesto.posible_duplicado = "ya aparece en Web Search"
    trabajo.guardar()
    subidas = []
    monkeypatch.setattr(
        Trabajo,
        "subir",
        lambda self, *args, **kwargs: subidas.append(
            self.manifiesto.nombre_batch
        ),
    )
    monkeypatch.setattr(Trabajo, "descubrir", lambda *args, **kwargs: "003SRO")

    autorizar_posible_duplicado(trabajo)
    fallos = subir_partes(
        [trabajo],
        SesionFalsa(),
        cliente=ClienteFalso(),
        forzados=[str(trabajo.carpeta)],
        buscador=BuscadorFalso(numeros),
    )

    assert fallos == []
    assert subidas == [trabajo.manifiesto.nombre_batch]


def test_si_web_search_no_lo_tiene_la_carga_sigue(tmp_path, monkeypatch):
    trabajo = _trabajo(tmp_path)
    subidas = []
    monkeypatch.setattr(
        Trabajo, "subir",
        lambda self, *a, **k: subidas.append(self.manifiesto.nombre_batch),
    )
    # Buscar el batch recien subido espera a que AirVault lo publique; eso
    # es asunto de otra prueba y aqui solo alargaria esta.
    monkeypatch.setattr(Trabajo, "descubrir", lambda self, *a, **k: "003SRO")

    subir_partes(
        [trabajo], SesionFalsa(), cliente=ClienteFalso(),
        buscador=BuscadorFalso(["9999999"]),
    )

    assert subidas == ["DP | BIT"]
    assert not es_posible_duplicado(trabajo)


def test_sin_poder_consultar_no_se_inventa_un_motivo(tmp_path):
    """Un fallo de red no puede parar el trabajo del dia."""
    trabajo = _trabajo(tmp_path)

    class BuscadorMudo(BuscadorFalso):
        def _pedir(self, ruta, plantilla, valor):
            raise RuntimeError("sin red")

    assert revisar_duplicado(trabajo, BuscadorMudo()) == ""


def test_desmarcada_advierte_y_sube_aunque_web_search_detecte_duplicados(tmp_path, monkeypatch):
    trabajo = _trabajo(tmp_path)
    trabajo.config = trabajo.config.with_overrides(detener_por_duplicados=False)
    numeros = [r.log_number for r in trabajo.manifiesto.registros if r.log_number]
    subidas, avisos = [], []
    monkeypatch.setattr(Trabajo, "subir", lambda self, *a, **k: subidas.append(self))
    monkeypatch.setattr(Trabajo, "descubrir", lambda self, *a, **k: setattr(self.manifiesto, "batch_id", "NUEVO"))
    fallos = subir_partes([trabajo], SesionFalsa(), cliente=ClienteFalso(),
                         buscador=BuscadorFalso(numeros), avisar=lambda texto, *_: avisos.append(texto))
    assert subidas == [trabajo]
    assert not fallos
    assert es_posible_duplicado(trabajo)
    assert any("duplicado" in aviso and "continúa" in aviso for aviso in avisos)
    assert "sin detener" in str(estado_local(trabajo))


def test_desmarcada_no_impide_completar_y_conserva_la_alerta(tmp_path, monkeypatch):
    from app.airvault.flujo import ResultadoCompletar
    trabajo = _trabajo(tmp_path)
    trabajo.config = trabajo.config.with_overrides(detener_por_duplicados=False)
    trabajo.manifiesto.posible_duplicado = "ya hay bitácoras publicadas"
    llamadas = []
    def completar(cliente, automatico=False):
        llamadas.append(automatico)
        return ResultadoCompletar(True, [], 2, "ok")
    monkeypatch.setattr(trabajo, "completar", completar)
    assert completar_partes([trabajo], ClienteFalso())[0][1].completado
    assert llamadas
    assert es_posible_duplicado(trabajo)


# ── preguntarle a Web Search es opcional ───────────────────────────

def test_de_fabrica_no_se_le_pregunta_a_web_search(tmp_path):
    """La ruta de busqueda se adivina en ejecucion; no se paga sin pedirlo.

    Sin buscador no se deja de vigilar: el libro de envios es local, no
    falla y frena igual un batch que ya se mando.
    """
    assert buscador_de(SesionFalsa(), AirVaultConfig(), tmp_path / "a.json") is None


def test_encendida_la_opcion_si_se_construye_el_buscador(tmp_path):
    buscador = buscador_de(
        SesionFalsa(),
        AirVaultConfig(buscar_publicadas=True),
        tmp_path / "a.json",
    )

    assert isinstance(buscador, Buscador)


# ── lo que la marca impide ─────────────────────────────────────────

def test_un_posible_duplicado_no_se_completa(tmp_path):
    """Completar lo manda a Web Search: seria la segunda copia archivada."""
    trabajo = _trabajo(tmp_path)
    trabajo.manifiesto.posible_duplicado = "sus bitacoras ya estan publicadas"
    trabajo.manifiesto.batch_id = "003SRO"
    trabajo.guardar()

    hechos = completar_partes([trabajo], ClienteFalso())

    assert not hechos[0][1].completado
    assert "no se cierra porque" in hechos[0][1].detalle
    assert not trabajo.manifiesto.etapa_hecha("completar")


def test_un_posible_duplicado_no_entra_en_lo_que_se_sube_solo(tmp_path):
    trabajo = _trabajo(tmp_path)
    trabajo.manifiesto.posible_duplicado = "sus bitacoras ya estan publicadas"
    trabajo.guardar()

    assert partes_por_subir([estado_local(trabajo)]) == []


def test_la_fila_dice_por_que_se_paro(tmp_path):
    trabajo = _trabajo(tmp_path)
    trabajo.manifiesto.posible_duplicado = "2 de sus bitacoras ya se mandaron"
    trabajo.guardar()

    parte = estado_local(trabajo)

    assert parte.estado == POSIBLE_DUPLICADO
    assert "ya se mandaron" in str(parte)
    # Y la orden de subir a mano se apaga mientras la duda siga puesta.
    assert not parte.se_puede_subir


def test_quitar_la_marca_devuelve_el_batch_a_la_cola(tmp_path):
    trabajo = _trabajo(tmp_path)
    trabajo.manifiesto.posible_duplicado = "2 de sus bitacoras ya se mandaron"
    trabajo.guardar()

    limpiar_posible_duplicado(trabajo)

    assert estado_local(trabajo).se_puede_subir
    assert not Trabajo.cargar(
        AirVaultConfig(), trabajo.carpeta
    ).manifiesto.posible_duplicado


# ── el PDF que se manda ────────────────────────────────────────────

def test_un_tramo_de_carga_no_se_reusa_si_la_entrega_cambio(tmp_path):
    """La causa de que un batch subiera paginas que no eran las suyas.

    Los tramos que van a Quick Upload se guardan y se reaprovechan. Se
    reconocian por la ruta del PDF y por que paginas se pidieron, y las dos
    cosas siguen igual despues de depurar y volver a exportar: el archivo
    conserva el nombre y las paginas se numeran otra vez desde uno. El
    tramo viejo encajaba, asi que se subia tal cual.
    """
    import pymupdf as fitz

    from app.airvault.flujo import ParteDeEntrega, _pdf_de_carga

    def entrega(paginas: int, texto: str) -> Path:
        ruta = tmp_path / "entrega.pdf"
        documento = fitz.open()
        for _ in range(paginas):
            documento.new_page().insert_text((72, 72), texto)
        documento.save(str(ruta))
        documento.close()
        return ruta

    ruta = entrega(4, "primera exportacion")
    parte = ParteDeEntrega(
        indice=1, total=1, pdf=ruta,
        paginas=[{"archivo": "Image_001.pdf", "pagina": n + 1}
                 for n in range(4)],
    )
    antes = _pdf_de_carga(parte, 1, [0, 1], tmp_path / "job")

    # Se depura y se vuelve a exportar: mismo nombre, otras paginas.
    entrega(4, "despues de depurar")
    despues = _pdf_de_carga(parte, 1, [0, 1], tmp_path / "job")

    assert despues != antes, "se reuso el tramo de la exportacion anterior"
    documento = fitz.open(str(despues))
    try:
        assert "despues de depurar" in documento.load_page(0).get_text()
    finally:
        documento.close()


def test_un_tramo_se_reusa_mientras_la_entrega_no_cambie(tmp_path):
    """Sin esto, cada intento volveria a cortar (y a comprimir) la entrega."""
    import pymupdf as fitz

    from app.airvault.flujo import ParteDeEntrega, _pdf_de_carga

    ruta = tmp_path / "entrega.pdf"
    documento = fitz.open()
    for _ in range(4):
        documento.new_page()
    documento.save(str(ruta))
    documento.close()
    parte = ParteDeEntrega(
        indice=1, total=1, pdf=ruta,
        paginas=[{"archivo": "Image_001.pdf", "pagina": n + 1}
                 for n in range(4)],
    )

    primero = _pdf_de_carga(parte, 1, [0, 1], tmp_path / "job")
    segundo = _pdf_de_carga(parte, 1, [0, 1], tmp_path / "job")

    assert primero == segundo
