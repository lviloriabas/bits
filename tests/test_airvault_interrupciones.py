"""Lo que queda cuando el trabajo con AirVault se corta por el medio.

Cancelar, perder la red o quedarse sin respuesta en la peticion que cierra
algo no son rarezas: una ejecucion son horas de subida y miles de paginas.
Lo que no puede pasar es que el corte deje el trabajo en un estado del que
el programa no sale solo: un batch tomado que nadie suelta, una carga dada
por enviada sin haber salido, un cierre hecho en AirVault y sin anotar aqui.
"""

from __future__ import annotations

import pytest

from app.airvault import duplicados, envios_exactos, registro, uploader
from app.airvault.config import AirVaultConfig
from app.airvault.flujo import (
    CARGAS_CORTADAS_MAXIMAS,
    INTENTOS_DE_SOLTAR,
    SIN_SUBIR,
    Trabajo,
    TurnoDeCargaOcupado,
    _reiniciar_subida_ausente,
    cargas_cortadas_seguidas,
    carpeta_del_libro,
    comprobar_partes,
    estado_local,
    partes_por_subir,
    reiniciar_trabajos_incompletos,
    subir_partes,
)
from app.airvault.model import EstadoEtapa, Manifiesto, Registro
from app.airvault.session import (
    ErrorDeAirVault,
    ErrorDeConexion,
    RespuestaPerdida,
    SesionCancelada,
)
from tests.airvault_fake import ClienteFalso
from tests.test_airvault_comprobar import (
    SesionFalsa,
    mapa,
    trabajo_con_divisoria,
    trabajo_subido,
)
from tests.test_airvault_mezclas import escenario
from tests.test_airvault_trabajo import SubidorFalso, cliente_con_lote
from tests.test_airvault_trabajo import corrida as corrida_con_pdf


# ── el batch tomado ─────────────────────────────────────────────────

def tomado(tmp_path):
    """Un batch que este trabajo tiene tomado en AirVault, como al escribir."""
    csv = corrida_con_pdf(tmp_path)
    cliente = cliente_con_lote()
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.fijar_lote("003SRO")
    trabajo.tomar(cliente)
    return trabajo, cliente


def falla_con(cliente, error, intentos=None):
    """Hace que soltar el batch falle con ese error; devuelve como deshacerlo."""
    soltar = cliente.cerrar_lote

    def no_sale(batch_id):
        if intentos is not None:
            intentos.append(batch_id)
        raise error

    cliente.cerrar_lote = no_sale
    return soltar


def test_cancelar_no_da_por_suelto_un_batch_que_no_se_solto(tmp_path):
    """Al cancelar, la sesion esta cortada justo cuando toca soltar.

    El batch se daba por suelto en el primer intento, que ni salia, y la
    limpieza posterior ya no lo pedia: quedaba «abierto por otra persona» a
    nombre propio hasta que alguien lo cerrara en Web Index.
    """
    trabajo, cliente = tomado(tmp_path)
    soltar = falla_con(cliente, SesionCancelada("se cancelo antes de UnlockBatch"))

    trabajo.cerrar(cliente)  # el ``finally`` del indexado
    trabajo.cerrar(cliente)  # el de la tanda
    assert cliente.cerrados == []

    cliente.cerrar_lote = soltar
    trabajo.cerrar(cliente)  # la limpieza, con la sesion ya reanudada
    assert cliente.cerrados == ["003SRO"]
    # Y suelto, no se vuelve a pedir.
    trabajo.cerrar(cliente)
    assert cliente.cerrados == ["003SRO"]


def test_sin_red_el_batch_se_suelta_en_la_revision_siguiente(tmp_path):
    trabajo, cliente = tomado(tmp_path)
    soltar = falla_con(cliente, ErrorDeConexion("sin red"))
    trabajo.cerrar(cliente)
    assert cliente.cerrados == []

    cliente.cerrar_lote = soltar
    comprobar_partes([trabajo], cliente)

    assert "003SRO" in cliente.cerrados
    assert trabajo._tomado is False


def test_soltar_no_se_reintenta_sin_fin(tmp_path):
    """Con AirVault caido de verdad se deja de insistir y queda el aviso."""
    trabajo, cliente = tomado(tmp_path)
    intentos: list = []
    falla_con(cliente, ErrorDeConexion("sin red"), intentos)

    for _vuelta in range(INTENTOS_DE_SOLTAR + 3):
        trabajo.cerrar(cliente)

    assert len(intentos) == INTENTOS_DE_SOLTAR
    assert trabajo._tomado is False


def test_un_rechazo_de_airvault_al_soltar_no_se_repite(tmp_path):
    """AirVault contesto: insistir da un error que parece otro problema."""
    trabajo, cliente = tomado(tmp_path)
    intentos: list = []
    falla_con(cliente, ErrorDeAirVault("Batch no esta tomado por este usuario"),
              intentos)

    trabajo.cerrar(cliente)
    trabajo.cerrar(cliente)

    assert intentos == ["003SRO"]


# ── la carga que se corta ───────────────────────────────────────────

class SubidorQueSeCorta(SubidorFalso):
    """Falla con el error que se le diga y cuenta hasta donde llego."""

    def __init__(self, error, finish_enviado: bool):
        super().__init__()
        self.error = error
        self.finish_enviado = finish_enviado

    def subir(self, ruta, valores, avisar=None):
        self.subidos.append(ruta)
        raise self.error


def por_subir(tmp_path, monkeypatch, subidor):
    monkeypatch.setattr(uploader, "SubidorQuickUpload", subidor)
    csv = corrida_con_pdf(tmp_path)
    return Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv, "DP | UNO")


def reserva(trabajo) -> str:
    return envios_exactos.motivo(
        carpeta_del_libro(trabajo), trabajo.manifiesto.pdf_origen, trabajo.config
    )


def test_una_carga_cortada_antes_de_finishupload_queda_sin_subir(tmp_path, monkeypatch):
    """Solo viajaron trozos: AirVault no tiene ningun batch que publicar.

    Quedaba «en curso» y con el PDF reservado: el programa buscaba durante
    su plazo entero un batch que no existia, con las demas cargas detras, y
    reenviarla pedia ademas autorizar el «mismo PDF».
    """
    trabajo = por_subir(
        tmp_path, monkeypatch,
        SubidorQueSeCorta(ErrorDeConexion("se cayo la red"), finish_enviado=False),
    )

    with pytest.raises(ErrorDeConexion):
        trabajo.subir(object())

    subida = trabajo.manifiesto.etapa("subir")
    assert subida.estado is EstadoEtapa.ERROR
    assert "antes de terminar de enviarse" in subida.detalle
    assert [e.resultado for e in trabajo.manifiesto.eventos("subida")] == ["no_enviada"]
    assert reserva(trabajo) == ""
    assert not trabajo.manifiesto.duplicado_exacto
    assert estado_local(trabajo).estado == SIN_SUBIR
    # Lo anotado esta en disco, no solo en memoria.
    releido = Trabajo.cargar(AirVaultConfig(), trabajo.carpeta)
    assert releido.manifiesto.etapa("subir").estado is EstadoEtapa.ERROR


def test_si_finishupload_ya_salio_la_carga_sigue_en_curso(tmp_path, monkeypatch):
    """AirVault pudo crear el batch: se busca en la cola y no se repite."""
    trabajo = por_subir(
        tmp_path, monkeypatch,
        SubidorQueSeCorta(ErrorDeConexion("se corto esperando"), finish_enviado=True),
    )

    with pytest.raises(ErrorDeConexion):
        trabajo.subir(object())

    assert trabajo.manifiesto.etapa("subir").estado is EstadoEtapa.EN_CURSO
    assert [e.resultado for e in trabajo.manifiesto.eventos("subida")] == ["interrumpida"]
    assert "Este mismo PDF" in reserva(trabajo)


def test_la_carga_cortada_sale_sola_otra_vez_pero_no_sin_fin(tmp_path, monkeypatch):
    """Una red que se cae vuelve; el archivo que se corta siempre, no."""
    trabajo = por_subir(
        tmp_path, monkeypatch,
        SubidorQueSeCorta(ErrorDeConexion("se cayo la red"), finish_enviado=False),
    )
    cliente = ClienteFalso()

    for intento in range(1, CARGAS_CORTADAS_MAXIMAS + 1):
        assert partes_por_subir(comprobar_partes([trabajo], cliente)) == [trabajo]
        with pytest.raises(ErrorDeConexion):
            trabajo.subir(object())
        assert cargas_cortadas_seguidas(trabajo) == intento

    parte, = comprobar_partes([trabajo], cliente)
    assert parte.estado == SIN_SUBIR
    assert partes_por_subir([parte]) == []

    # Una carga que si sale corta la racha.
    monkeypatch.setattr(uploader, "SubidorQuickUpload", SubidorFalso())
    trabajo.subir(object())
    assert trabajo.manifiesto.etapa_hecha("subir")
    assert cargas_cortadas_seguidas(trabajo) == 0


def test_cancelar_mientras_suben_los_trozos_deja_la_carga_por_subir(tmp_path, monkeypatch):
    """Cancelar no es un error de la carga ni la deja «en curso»."""

    class Cancelado(BaseException):
        """Lo que levanta la ventana, que no es una ``Exception``."""

    trabajo = por_subir(
        tmp_path, monkeypatch, SubidorQueSeCorta(Cancelado(), finish_enviado=False)
    )
    with pytest.raises(Cancelado):
        trabajo.subir(object())
    assert trabajo.manifiesto.etapa("subir").estado is EstadoEtapa.PENDIENTE

    monkeypatch.setattr(
        uploader, "SubidorQuickUpload",
        SubidorQueSeCorta(SesionCancelada("se cancelo"), finish_enviado=False),
    )
    with pytest.raises(SesionCancelada):
        trabajo.subir(object())

    assert trabajo.manifiesto.etapa("subir").estado is EstadoEtapa.PENDIENTE
    assert [e.resultado for e in trabajo.manifiesto.eventos("subida")] == [
        "cancelada", "cancelada",
    ]
    assert reserva(trabajo) == ""
    # No cuenta para la racha de cargas cortadas.
    assert cargas_cortadas_seguidas(trabajo) == 0


def test_el_turno_de_otra_ventana_no_marca_el_batch_como_repetido(tmp_path, monkeypatch):
    """Que otra carga este en vuelo no dice nada de este archivo."""
    trabajo = por_subir(tmp_path, monkeypatch, SubidorFalso())

    with envios_exactos._exclusiva(carpeta_del_libro(trabajo)):
        with pytest.raises(TurnoDeCargaOcupado):
            trabajo.subir(object())

    assert not trabajo.manifiesto.duplicado_exacto
    assert not trabajo.manifiesto.posible_duplicado
    assert trabajo.manifiesto.etapa("subir").estado is EstadoEtapa.PENDIENTE
    assert estado_local(trabajo).estado == SIN_SUBIR
    # Con el turno libre sube sin que nadie tenga que autorizar nada.
    trabajo.subir(object())
    assert trabajo.manifiesto.etapa_hecha("subir")


def test_sin_red_no_se_insiste_con_las_cargas_siguientes(tmp_path, monkeypatch):
    """Fallarian igual, una detras de otra y cada una con sus reintentos."""
    trabajos, _cliente = escenario(tmp_path)
    for trabajo in trabajos:
        trabajo.manifiesto.etapas.clear()
        trabajo.guardar()
    subidor = SubidorQueSeCorta(ErrorDeConexion("se cayo la red"), finish_enviado=False)
    monkeypatch.setattr(uploader, "SubidorQuickUpload", subidor)
    monkeypatch.setattr("app.airvault.flujo._paginas_del_pdf", lambda _p: 2)

    fallos = subir_partes(trabajos, SesionFalsa(), cliente=ClienteFalso())

    assert len(subidor.subidos) == 1
    assert [trabajo for trabajo, _detalle in fallos] == trabajos[:1]
    assert trabajos[0].manifiesto.etapa("subir").estado is EstadoEtapa.ERROR
    assert trabajos[1].manifiesto.etapa("subir").estado is EstadoEtapa.PENDIENTE


# ── las divisorias borradas ─────────────────────────────────────────

def test_volver_a_indexar_no_olvida_las_divisorias_ya_borradas(tmp_path):
    """La cuenta estaba en el texto de la etapa, que se borra al empezar.

    Si AirVault descuenta de sus paginas las divisorias borradas, el batch
    vuelve con una menos que el PDF. El segundo indexado marcaba la etapa
    «en curso» antes de comparar y se negaba a escribir por cantidad.
    """
    trabajo, cliente = trabajo_con_divisoria(tmp_path)
    plan, indexador = trabajo.planificar(cliente)
    resultado = trabajo.indexar(indexador, plan)
    assert resultado.separadores_borrados == 1
    assert trabajo.manifiesto.separadores_borrados() == 1

    cliente.page_count = 2  # el batch ya no cuenta la divisoria borrada
    plan, indexador = trabajo.planificar(cliente)
    trabajo.indexar(indexador, plan)

    assert trabajo.manifiesto.cantidades_paginas_compatibles() == {2, 3}
    # Ni un indexado cortado, que no llega a las divisorias, la pierde.
    trabajo.manifiesto.etapa("indexar").marcar(
        EstadoEtapa.ERROR,
        "escritas 0, omitidas 2, fallidas 0, separadores borrados 0",
    )
    assert trabajo.manifiesto.separadores_borrados() == 1


def test_reiniciar_el_indexado_no_olvida_las_divisorias_borradas(tmp_path):
    """Tambien en un manifiesto anterior, que solo las tiene en el texto."""
    trabajo, _cliente = trabajo_con_divisoria(tmp_path)
    trabajo.manifiesto.etapa("indexar").marcar(
        EstadoEtapa.ERROR,
        "escritas 1, omitidas 0, fallidas 1, separadores borrados 1",
    )
    trabajo.guardar()

    assert reiniciar_trabajos_incompletos([trabajo]) == [(trabajo, "indexar")]

    assert trabajo.manifiesto.separadores_borrados() == 1
    assert trabajo.manifiesto.cantidades_paginas_compatibles() == {2, 3}


def test_volver_a_subir_el_archivo_si_olvida_las_divisorias_borradas(tmp_path):
    """El batch nuevo trae otra vez todas sus paginas."""
    trabajo, _cliente = trabajo_con_divisoria(tmp_path)
    trabajo.manifiesto.anotar_separadores_borrados(1)

    _reiniciar_subida_ausente(trabajo)

    assert trabajo.manifiesto.separadores_borrados() == 0
    assert trabajo.manifiesto.cantidades_paginas_compatibles() == {3}


def test_un_manifiesto_anterior_lee_las_divisorias_del_texto_de_la_etapa():
    manifiesto = Manifiesto(job_id="x", registros=[
        Registro(seq=1, separador="HP-1848CMP"),
        Registro(seq=2, log_number="2312238"),
    ])
    manifiesto.etapa("indexar").marcar(
        EstadoEtapa.HECHA,
        "escritas 1, omitidas 0, fallidas 0, separadores borrados 1",
    )
    datos = manifiesto.model_dump(mode="json")
    datos.pop("separadores_quitados")

    anterior = Manifiesto.model_validate(datos)

    assert anterior.separadores_borrados() == 1
    # Nunca mas de las que el PDF trae.
    anterior.anotar_separadores_borrados(7)
    assert anterior.separadores_borrados() == 1


# ── el cierre sin respuesta ─────────────────────────────────────────

def por_completar(tmp_path):
    trabajo, cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    cliente.mapa = mapa(0, 0)
    return trabajo, cliente


def no_contesta(cliente, lo_completa: bool):
    """``CompleteBatch`` sale y su respuesta se pierde."""

    def completar(batch_id):
        cliente.completados.append(batch_id)
        if lo_completa:
            cliente.lotes = [l for l in cliente.lotes if l.batch_id != batch_id]
        raise RespuestaPerdida("AirVault no contesto CompleteBatch")

    cliente.completar_lote = completar


def test_un_cierre_sin_respuesta_se_da_por_hecho_si_el_batch_salio_de_la_cola(tmp_path):
    """Completado, el batch deja Web Index: esa es la respuesta que faltaba.

    Antes la peticion se repetia a ciegas, AirVault contestaba un error
    sobre un batch que ya no estaba y el cierre se quedaba sin anotar.
    """
    trabajo, cliente = por_completar(tmp_path)
    no_contesta(cliente, lo_completa=True)

    resultado = trabajo.completar(cliente, automatico=True)

    assert resultado.completado
    assert cliente.completados == ["003SRO"]
    assert trabajo.manifiesto.cierre_hecho()
    assert trabajo.manifiesto.completado_automatico
    assert "ya salió de la cola" in trabajo.manifiesto.etapa("completar").detalle
    # No se le pide a AirVault que suelte un batch que ya no tiene.
    assert cliente.cerrados == []


def test_un_cierre_sin_respuesta_no_se_da_por_hecho_con_el_batch_en_la_cola(tmp_path):
    trabajo, cliente = por_completar(tmp_path)
    no_contesta(cliente, lo_completa=False)

    resultado = trabajo.completar(cliente)

    assert not resultado.completado
    assert resultado.bloqueadas == []
    assert trabajo.manifiesto.etapa("completar").estado is EstadoEtapa.EN_CURSO
    assert not trabajo.manifiesto.cierre_hecho()
    assert not trabajo.manifiesto.etapa_hecha("completar")
    assert cliente.completados == ["003SRO"]
    assert cliente.cerrados == ["003SRO"]

    # AirVault termino de completarlo despues: basta mirar la cola.
    cliente.lotes = [l for l in cliente.lotes if l.batch_id != "003SRO"]
    otra = trabajo.completar(cliente)

    assert otra.completado
    assert cliente.completados == ["003SRO"]
    assert trabajo.manifiesto.cierre_hecho()


def test_tras_un_cierre_sin_respuesta_se_vuelve_a_pedir_si_el_batch_sigue_ahi(tmp_path):
    trabajo, cliente = por_completar(tmp_path)
    completar = cliente.completar_lote
    no_contesta(cliente, lo_completa=False)
    assert not trabajo.completar(cliente).completado

    cliente.completar_lote = completar
    resultado = trabajo.completar(cliente)

    assert resultado.completado
    assert cliente.completados == ["003SRO", "003SRO"]


def test_un_cierre_omitido_no_se_anota_como_completado(tmp_path):
    """La etapa omitida es el cierre que no se pudo hacer: sigue en la cola.

    Anotado como completado, sus bitacoras se ofrecian de control positivo
    de Web Search, donde no estan.
    """
    trabajo, _cliente = trabajo_subido(tmp_path)
    trabajo.fijar_lote("003SRO")
    trabajo.manifiesto.etapa("completar").marcar(
        EstadoEtapa.OMITIDA, "1 de 2 paginas no estan en verde"
    )
    trabajo.guardar()
    libro = carpeta_del_libro(trabajo)

    assert not trabajo.manifiesto.cierre_hecho()
    anotado, = registro.anotar(trabajo.carpeta, [trabajo]).batches
    assert anotado.subido and not anotado.completado
    assert duplicados.anotar(libro, [trabajo]).controles() == []

    trabajo.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA, "2 paginas en verde")
    anotado, = registro.anotar(trabajo.carpeta, [trabajo]).batches
    assert anotado.completado
    assert duplicados.anotar(libro, [trabajo]).controles()


# ── las memorias locales ────────────────────────────────────────────

def test_recuperar_una_mezcla_no_da_sus_bitacoras_por_eliminadas(tmp_path):
    """Vuelven a estar por subir, no fuera de la entrega.

    Marcadas como eliminadas contaban como ya cubiertas: si el reparto
    cambiaba antes de resubirlas, ningun batch nuevo se las llevaba.
    """
    from app.airvault.mezclas import recuperar_mezclas

    trabajos, cliente = escenario(tmp_path)
    for trabajo in trabajos:
        registro.anotar(trabajo.carpeta, [trabajo])
    assert all(registro.comprometidas(t.carpeta) for t in trabajos)

    assert recuperar_mezclas(trabajos, cliente) == trabajos

    for trabajo in trabajos:
        guardado = registro.leer(trabajo.carpeta)
        assert guardado.batches == []
        assert guardado.eliminadas == []
        assert registro.comprometidas(trabajo.carpeta) == set()


def test_un_archivo_de_memoria_tomado_un_instante_se_vuelve_a_leer(tmp_path, monkeypatch):
    """El antivirus lo suelta enseguida; darlo por vacio borraba lo anotado."""
    trabajo, _cliente = trabajo_subido(tmp_path)
    registro.anotar(trabajo.carpeta, [trabajo])
    ruta = registro.ruta_registro(trabajo.carpeta)
    leer = type(ruta).read_text
    fallos = [PermissionError("lo tiene otro proceso")]

    def tomado(self, *args, **kwargs):
        if self == ruta and fallos:
            raise fallos.pop()
        return leer(self, *args, **kwargs)

    monkeypatch.setattr(type(ruta), "read_text", tomado)
    monkeypatch.setattr(registro.time, "sleep", lambda _s: None)

    assert len(registro.leer(trabajo.carpeta).batches) == 1
    assert fallos == []


def test_un_archivo_de_memoria_danado_se_aparta_antes_de_reemplazarlo(tmp_path):
    """Se sigue trabajando, pero lo que habia queda a mano para recuperarlo."""
    trabajo, _cliente = trabajo_subido(tmp_path)
    libro = carpeta_del_libro(trabajo)
    ruta = duplicados.ruta_libro(libro)
    ruta.write_text('{"version": 1, "envios": {"2312238": [', encoding="utf-8")

    assert duplicados.leer(libro).envios == {}
    duplicados.anotar(libro, [trabajo])

    copia = ruta.with_name(f"{ruta.name}.ilegible")
    assert copia.read_text(encoding="utf-8").startswith('{"version": 1')
    assert duplicados.leer(libro).envios


def test_el_cache_de_flota_se_reemplaza_de_una_vez(tmp_path, monkeypatch):
    """Escrito encima, un corte a mitad lo dejaba truncado y se leia vacio."""
    from app.airvault import mapping
    from app.airvault.mapping import ResolutorFlota

    ruta = tmp_path / mapping.FLOTA_CACHE_FILENAME
    flota = ResolutorFlota()
    flota.aprender("HP-1848CMP", "NG", "COPA")
    flota.guardar(ruta)

    assert ResolutorFlota.load(ruta).resolver("HP-1848CMP") == ("NG", "COPA", False)
    assert [p.name for p in tmp_path.iterdir()] == [ruta.name]

    # Si Windows no deja reemplazarlo, se escribe encima como antes.
    def ocupado(*_a, **_k):
        raise PermissionError("otro proceso lo tiene abierto")

    monkeypatch.setattr(mapping.os, "replace", ocupado)
    flota.aprender("HP-9901CMP", "MAX")
    flota.guardar(ruta)

    assert ResolutorFlota.load(ruta).resolver("HP-9901CMP") == ("MAX", "", False)
    assert [p.name for p in tmp_path.iterdir()] == [ruta.name]
