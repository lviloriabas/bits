"""Las etapas del trabajo tal como las corre la interfaz.

Cubre el recorrido que comparte la ventana con la linea de comandos: que
un trabajo a medias se retome sin repetir escrituras, que la subida no se
haga dos veces y que un CSV de otra ejecución no se cuele en el batch de la
anterior. Todo contra el cliente falso; ninguna prueba toca la red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.airvault import manifest as manifiestos
from app.airvault.config import AirVaultConfig, CAMPO_LOG_NUMBER
from app.airvault.flujo import (
    ErrorDeCorrida,
    Trabajo,
    carpeta_de_corrida,
    carpeta_de_parte,
    carpeta_de_trabajo,
    comprobar_entrega,
    pdfs_de_corrida,
    preparar_partes,
    ruta_indice_paginas,
)
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
            pdfs: tuple[str, ...] = (), con_indice: bool = True):
    """Arma en el temporal la carpeta que deja una ejecución terminada.

    Sin ``pdfs`` deja la entrega en un solo archivo, como la exportacion sin
    repartir; con varios, cada uno lleva una de las dos bitacoras del CSV.
    """
    import json
    import pymupdf as fitz

    carpeta = tmp_path / "output" / nombre
    (carpeta / "datos").mkdir(parents=True)
    csv = carpeta / "datos" / f"{nombre}.CSV"
    csv.write_text(CSV, encoding="utf-8")

    archivos = list(pdfs) or [f"{nombre}.pdf"]
    for pdf in archivos:
        documento = fitz.open()
        for _pagina in range(2 if len(archivos) == 1 else 1):
            documento.new_page()
        documento.save(str(carpeta / pdf))
        documento.close()
    if con_indice:
        paginas = [
            [{"archivo": "Image_001.pdf", "pagina": 1},
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


def cliente_con_lote(batch_id: str = "003SRO", paginas: int = 2,
                     nombre: str = "DP | BIT 18 AUG 2026 05 42"):
    return ClienteFalso(
        paginas={n: pagina(n, estado=3) for n in range(1, paginas + 1)},
        lotes=[lote(batch_id, nombre, paginas)],
        picklist=["HP-1848CMP"],
        page_count=paginas,
    )


# ── ubicacion de los archivos de la ejecución ────────────────────────

def test_la_carpeta_de_la_corrida_sale_del_csv(tmp_path):
    csv = corrida(tmp_path)
    assert carpeta_de_corrida(csv).name == "BITS 18 AUG 2026 05 42"


def test_encuentra_el_pdf_de_entrega(tmp_path):
    csv = corrida(tmp_path)
    partes = comprobar_entrega(csv)
    assert len(partes) == 1
    assert partes[0].pdf.name == "BITS 18 AUG 2026 05 42.pdf"


def test_cada_parte_es_un_archivo(tmp_path):
    csv = corrida(tmp_path, pdfs=("ejecución (1 de 2).pdf",
                                  "ejecución (2 de 2).pdf"))
    partes = comprobar_entrega(csv)
    assert [p.indice for p in partes] == [1, 2]
    assert all(p.total == 2 for p in partes)
    assert [p.pdf.name for p in partes] == [
        "ejecución (1 de 2).pdf", "ejecución (2 de 2).pdf"
    ]
    assert len(pdfs_de_corrida(csv)) == 2


def test_cada_parte_lleva_su_numero_en_el_nombre_del_lote(tmp_path):
    """Los batches se localizan por nombre; dos iguales no se distinguirian."""
    csv = corrida(tmp_path, pdfs=("a.pdf", "b.pdf"))
    partes = comprobar_entrega(csv)
    assert partes[0].nombre_lote("DP | BITS") == "DP | BITS -1"
    assert partes[1].nombre_lote("DP | BITS") == "DP | BITS -2"


def test_una_sola_parte_no_lleva_sufijo(tmp_path):
    csv = corrida(tmp_path)
    assert comprobar_entrega(csv)[0].nombre_lote("DP | BITS") == "DP | BITS"


def test_sin_pdf_no_hay_nada_que_subir(tmp_path):
    carpeta = tmp_path / "output" / "BITS 18 AUG 2026 05 42"
    (carpeta / "datos").mkdir(parents=True)
    csv = carpeta / "datos" / "BITS 18 AUG 2026 05 42.CSV"
    csv.write_text(CSV, encoding="utf-8")
    with pytest.raises(ErrorDeCorrida) as fallo:
        comprobar_entrega(csv)
    assert "exportarla" in str(fallo.value)


def test_sin_indice_no_se_adivina_el_reparto(tmp_path):
    """Sin saber que hay en cada archivo no se puede emparejar nada."""
    csv = corrida(tmp_path, con_indice=False)
    with pytest.raises(ErrorDeCorrida) as fallo:
        comprobar_entrega(csv)
    assert "volver a exportarla" in str(fallo.value)


def test_un_indice_que_nombra_lo_que_no_esta_se_detiene(tmp_path):
    csv = corrida(tmp_path)
    (carpeta_de_corrida(csv) / "BITS 18 AUG 2026 05 42.pdf").unlink()
    with pytest.raises(ErrorDeCorrida) as fallo:
        comprobar_entrega(csv)
    assert "no estan en la carpeta" in str(fallo.value)


def test_cada_parte_tiene_su_carpeta_de_trabajo(tmp_path):
    csv = corrida(tmp_path, pdfs=("a.pdf", "b.pdf"))
    partes = comprobar_entrega(csv)
    assert carpeta_de_parte(Path("job"), partes[0]).name == "parte-01"
    assert carpeta_de_parte(Path("job"), partes[1]).name == "parte-02"


def test_con_una_sola_parte_no_se_crea_subcarpeta(tmp_path):
    """Es el trabajo de siempre, en la carpeta de siempre."""
    csv = corrida(tmp_path)
    parte = comprobar_entrega(csv)[0]
    assert carpeta_de_parte(Path("job"), parte) == Path("job")


def test_preparar_partes_deja_un_manifiesto_por_lote(tmp_path):
    csv = corrida(tmp_path, pdfs=("a.pdf", "b.pdf"))
    trabajos = preparar_partes(AirVaultConfig(), tmp_path / "job", csv)
    assert len(trabajos) == 2
    assert [t.manifiesto.parte for t in trabajos] == [1, 2]
    assert [t.manifiesto.partes for t in trabajos] == [2, 2]
    assert [Path(t.manifiesto.pdf_origen).name for t in trabajos] == [
        "a.pdf", "b.pdf"
    ]
    assert manifiestos.existe(tmp_path / "job" / "parte-01")
    assert manifiestos.existe(tmp_path / "job" / "parte-02")


def test_cada_parte_solo_lleva_sus_bitacoras(tmp_path):
    csv = corrida(tmp_path, pdfs=("a.pdf", "b.pdf"))
    trabajos = preparar_partes(AirVaultConfig(), tmp_path / "job", csv)
    assert [len(t.manifiesto.registros) for t in trabajos] == [1, 1]
    assert trabajos[0].manifiesto.registros[0].log_number == "2312238"
    assert trabajos[1].manifiesto.registros[0].log_number == "2312239"


def test_una_corrida_repartida_no_se_prepara_como_un_solo_lote(tmp_path):
    csv = corrida(tmp_path, pdfs=("a.pdf", "b.pdf"))
    with pytest.raises(ErrorDeCorrida) as fallo:
        Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv)
    assert "2 partes" in str(fallo.value)


def test_el_trabajo_se_llama_como_la_corrida(tmp_path):
    csv = corrida(tmp_path)
    destino = carpeta_de_trabajo(carpeta_de_corrida(csv).name)
    assert destino.name == "BITS 18 AUG 2026 05 42"
    assert destino.parent.name == "airvault"


# ── preparacion y reanudacion ──────────────────────────────────────

def test_preparar_deja_el_manifiesto_en_disco(tmp_path):
    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(
        AirVaultConfig(), tmp_path / "job", csv, "DP | BIT 18 AUG 2026 05 42"
    )
    assert manifiestos.existe(tmp_path / "job")
    assert len(trabajo.manifiesto.registros) == 2
    assert trabajo.manifiesto.nombre_batch == "DP | BIT 18 AUG 2026 05 42"


def test_el_lote_se_llama_como_la_corrida_si_no_se_dice_otra_cosa(tmp_path):
    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv)
    assert trabajo.manifiesto.nombre_batch == "DP | BITS 18 AUG 2026 05 42"


def test_un_csv_sin_bitacoras_no_arma_trabajo(tmp_path):
    csv = tmp_path / "vacio.CSV"
    csv.write_text("file,page,log_number,matricula,date\n", encoding="utf-8")
    with pytest.raises(ErrorDeCorrida):
        Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv)


def test_volver_a_revisar_retoma_el_mismo_trabajo(tmp_path):
    """Apretar el boton dos veces no puede perder lo ya escrito."""
    csv = corrida(tmp_path)
    primero = Trabajo.abrir_o_preparar(AirVaultConfig(), tmp_path / "job", csv)
    primero.manifiesto.registros[0].estado = EstadoRegistro.ESCRITA
    primero.guardar()

    segundo = Trabajo.abrir_o_preparar(AirVaultConfig(), tmp_path / "job", csv)
    assert segundo.manifiesto.registros[0].estado is EstadoRegistro.ESCRITA


def test_otra_corrida_en_la_misma_carpeta_rehace_el_trabajo(tmp_path):
    """Seguir el trabajo anterior escribiria una ejecución en el batch de otra."""
    primera = corrida(tmp_path, "BITS 18 AUG 2026 05 42")
    trabajo = Trabajo.abrir_o_preparar(AirVaultConfig(), tmp_path / "job",
                                       primera)
    trabajo.manifiesto.registros[0].estado = EstadoRegistro.ESCRITA
    trabajo.guardar()

    segunda = corrida(tmp_path, "BITS 19 AUG 2026 06 10")
    rehecho = Trabajo.abrir_o_preparar(AirVaultConfig(), tmp_path / "job",
                                       segunda)
    assert rehecho.manifiesto.registros[0].estado is EstadoRegistro.PENDIENTE
    assert rehecho.manifiesto.nombre_batch == "DP | BITS 19 AUG 2026 06 10"


# ── subida ─────────────────────────────────────────────────────────

class SubidorFalso:
    def __init__(self):
        self.subidos = []
        self.valores = []

    def __call__(self, sesion, repo_id):
        return self

    def subir(self, ruta, valores, avisar=None):
        from app.airvault.uploader import ResultadoSubida

        self.subidos.append(ruta)
        self.valores.append(dict(valores))
        if avisar is not None:
            avisar("Subiendo", 1, 1)
        return ResultadoSubida(str(ruta), True)


def test_el_lote_no_se_sube_dos_veces(tmp_path, monkeypatch):
    """Subirlo otra vez crearia un batch gemelo y no se sabria en cual escribir."""
    from app.airvault import uploader

    csv = corrida(tmp_path)
    falso = SubidorFalso()
    monkeypatch.setattr(uploader, "SubidorQuickUpload", falso)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv)

    trabajo.subir(object())
    trabajo.subir(object())
    assert len(falso.subidos) == 1
    assert trabajo.manifiesto.etapa_hecha("subir")


def test_quick_upload_recibe_el_titulo_del_manifiesto(tmp_path, monkeypatch):
    from app.airvault import uploader
    from app.airvault.config import CAMPO_BATCH_NAME

    csv = corrida(tmp_path)
    falso = SubidorFalso()
    monkeypatch.setattr(uploader, "SubidorQuickUpload", falso)
    trabajo = Trabajo.preparar(
        AirVaultConfig(), tmp_path / "job", csv, "DP | BIT -2"
    )

    trabajo.subir(object())

    assert falso.valores[0][CAMPO_BATCH_NAME] == "DP | BIT -2"


def test_la_subida_guarda_la_cola_previa_para_reconocer_empty_batch(
    tmp_path, monkeypatch
):
    from app.airvault import uploader

    csv = corrida(tmp_path)
    falso = SubidorFalso()
    monkeypatch.setattr(uploader, "SubidorQuickUpload", falso)
    cliente = ClienteFalso(lotes=[lote("003VIEJO", "Empty-Batch", 2)])
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv)

    trabajo.subir(object(), cliente=cliente)

    assert trabajo.manifiesto.lotes_previos == ["003VIEJO"]


def test_descubre_el_empty_batch_que_no_estaba_antes(tmp_path):
    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(
        AirVaultConfig(), tmp_path / "job", csv, "DP | BIT PRUEBA"
    )
    trabajo.manifiesto.lotes_previos = ["003VIEJO"]
    trabajo.guardar()
    cliente = ClienteFalso(lotes=[
        lote("003VIEJO", "Empty-Batch", 2),
        lote("003NUEVO", "Empty-Batch", 2),
    ], paginas={
        registro.seq: pagina(
            registro.seq,
            valores={CAMPO_LOG_NUMBER: registro.log_number},
        )
        for registro in trabajo.manifiesto.bitacoras()
    })

    assert trabajo.descubrir(cliente, esperar=True, dormir=lambda _s: None) == (
        "003NUEVO"
    )
    assert trabajo.manifiesto.nombre_batch == "DP | BIT PRUEBA"


def test_finishupload_sin_respuesta_queda_por_confirmar_sin_fallar(
    tmp_path, monkeypatch
):
    """Los trozos llegaron; lo que no llego fue la respuesta del final.

    Se levantaba como un fallo y cortaba la cadena de subidas sin mirar si
    AirVault tenia la carga. Ahora queda «en curso» y quien sube la busca en
    la cola con la foto previa.
    """
    from app.airvault import uploader
    from app.airvault.uploader import ResultadoSubida

    class SubidorSinRespuesta(SubidorFalso):
        def subir(self, ruta, valores, avisar=None):
            super().subir(ruta, valores, avisar)
            return ResultadoSubida(str(ruta), True, "no contesto", confirmada=False)

    csv = corrida(tmp_path)
    monkeypatch.setattr(uploader, "SubidorQuickUpload", SubidorSinRespuesta())
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv)

    trabajo.subir(object())

    subida = trabajo.manifiesto.etapa("subir")
    assert subida.estado is EstadoEtapa.EN_CURSO
    assert "FinishUpload no contesto" in subida.detalle
    assert not trabajo.manifiesto.etapa_hecha("subir")


def test_encontrar_el_batch_da_por_hecha_la_subida_en_curso(tmp_path):
    """El batch en AirVault prueba que la carga llego, contestara o no."""
    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    subida = trabajo.manifiesto.etapa("subir")
    subida.marcar(EstadoEtapa.EN_CURSO, "FinishUpload no contesto")
    subida.actualizada = "2026-09-27T10:00:00"
    trabajo.guardar()

    trabajo.descubrir(cliente_con_lote(), esperar=False)

    assert trabajo.manifiesto.etapa_hecha("subir")
    # La hora de la carga se conserva: es la que ordena las partes.
    assert trabajo.manifiesto.etapa("subir").actualizada == "2026-09-27T10:00:00"


def test_descubrir_espera_a_que_airvault_termine_de_armar_el_batch(tmp_path):
    """El nombre aparece antes que las paginas: se espera, no se falla.

    La espera anterior se contentaba con ver el nombre y la identificacion,
    que exige las paginas exactas, fallaba enseguida. Eso cortaba la cadena
    de subidas y dejaba el batch sin indexar.
    """
    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    nombre = trabajo.manifiesto.nombre_batch
    esperas: list[float] = []

    class ColaQueSeLlena(ClienteFalso):
        def listar_lotes(self, filtro=""):
            paginas = 1 if len(esperas) < 2 else 2
            return [lote("003SRO", nombre, paginas)]

    avisos: list[str] = []
    batch_id = trabajo.descubrir(
        ColaQueSeLlena(page_count=2), esperar=True, dormir=esperas.append,
        avisar=lambda texto, _h, _t: avisos.append(texto),
    )

    assert batch_id == "003SRO"
    assert esperas == [20.0, 20.0]
    assert any("1 de 2 páginas" in texto for texto in avisos)
    assert trabajo.manifiesto.etapa_hecha("descubrir")


def test_descubrir_vuelve_a_leer_el_nombre_interno_que_aun_no_estaba(tmp_path):
    """Una primera pagina sin procesar no deja el Batch Name por vacio."""
    from app.airvault.config import CAMPO_BATCH_NAME

    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.manifiesto.lotes_previos = ["003VIEJO"]
    trabajo.guardar()
    esperas: list[float] = []

    class PrimeraPaginaTardia(ClienteFalso):
        def leer_pagina(self, batch_id, numero):
            if not esperas:
                raise RuntimeError("todavia sin procesar")
            return super().leer_pagina(batch_id, numero)

    cliente = PrimeraPaginaTardia(
        paginas={1: pagina(1, valores={
            CAMPO_BATCH_NAME: trabajo.manifiesto.nombre_batch,
        })},
        lotes=[lote("003VIEJO", "Empty-Batch", 2),
               lote("003NUEVO", "Empty-Batch", 2)],
        page_count=2,
    )

    assert trabajo.descubrir(
        cliente, esperar=True, dormir=esperas.append
    ) == "003NUEVO"
    assert esperas == [20.0]
    assert cliente.renombrados == [("003NUEVO", trabajo.manifiesto.nombre_batch)]


def test_descubrir_se_rinde_al_agotar_la_espera_maxima(tmp_path):
    csv = corrida(tmp_path)
    config = AirVaultConfig(espera_descubrimiento_s=20, espera_maxima_s=60)
    trabajo = Trabajo.preparar(config, tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    esperas: list[float] = []

    class SiempreAMedias(ClienteFalso):
        def listar_lotes(self, filtro=""):
            return [lote("003SRO", trabajo.manifiesto.nombre_batch, 1)]

    with pytest.raises(ErrorDeCorrida):
        trabajo.descubrir(SiempreAMedias(), esperar=True, dormir=esperas.append)
    assert esperas == [20.0, 20.0, 20.0]
    assert trabajo.manifiesto.batch_id is None


def test_la_subida_a_mano_se_puede_dar_por_hecha(tmp_path):
    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv)
    trabajo.omitir_subida()
    assert trabajo.manifiesto.etapa("subir").estado is EstadoEtapa.OMITIDA
    assert trabajo.manifiesto.etapa_hecha("subir")


# ── busqueda, plan y escritura ─────────────────────────────────────

def test_encuentra_el_lote_y_lo_anota(tmp_path):
    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    batch_id = trabajo.descubrir(cliente_con_lote(), esperar=False)
    assert batch_id == "003SRO"
    assert trabajo.manifiesto.batch_id == "003SRO"


def test_el_plan_no_escribe_nada(tmp_path):
    """La condicion que sostiene los dos tiempos del panel."""
    csv = corrida(tmp_path)
    cliente = cliente_con_lote()
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.descubrir(cliente, esperar=False)
    plan, _indexador = trabajo.planificar(cliente)

    assert cliente.escrituras == []
    assert len(plan.escribibles) == 2


def test_escribe_lo_que_el_plan_habia_anunciado(tmp_path):
    csv = corrida(tmp_path)
    cliente = cliente_con_lote()
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.descubrir(cliente, esperar=False)
    plan, indexador = trabajo.planificar(cliente)
    resultado = trabajo.indexar(indexador, plan)

    assert resultado.escritas == 2
    assert [p for p, _v, _e in cliente.escrituras] == [1, 2]
    validas, total, _problemas = trabajo.verificar(cliente)
    assert (validas, total) == (2, 2)


def test_indexar_no_modifica_el_csv_de_la_corrida(tmp_path):
    csv = corrida(tmp_path)
    contenido_original = csv.read_bytes()
    cliente = cliente_con_lote()
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.descubrir(cliente, esperar=False)
    plan, indexador = trabajo.planificar(cliente)

    trabajo.indexar(indexador, plan)

    assert csv.read_bytes() == contenido_original


def test_un_lote_con_otra_cantidad_de_paginas_no_se_toca(tmp_path):
    """Si la correspondencia por posicion esta rota, no se escribe nada."""
    from app.airvault.guards import ErrorDeGuarda

    csv = corrida(tmp_path)
    cliente = cliente_con_lote(paginas=3)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.fijar_lote("003SRO")
    with pytest.raises(ErrorDeGuarda):
        trabajo.planificar(cliente)
    assert cliente.escrituras == []


def test_sin_lote_no_se_planifica(tmp_path):
    csv = corrida(tmp_path)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv)
    with pytest.raises(ErrorDeCorrida):
        trabajo.planificar(cliente_con_lote())


def test_el_avance_llega_pagina_a_pagina(tmp_path):
    """Es lo que mueve la barra de la ventana mientras se escribe."""
    csv = corrida(tmp_path)
    cliente = cliente_con_lote()
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.descubrir(cliente, esperar=False)
    plan, indexador = trabajo.planificar(cliente)

    avisos = []
    trabajo.indexar(indexador, plan,
                    avisar=lambda t, h, n: avisos.append((h, n)))
    assert avisos == [(1, 2), (2, 2)]


# ── el batch se suelta ──────────────────────────────────────────────

def test_el_lote_se_suelta_al_terminar_cada_etapa(tmp_path):
    """AirVault admite un solo dueno: quedarselo cuelga la proxima apertura.

    Sin soltarlo, la ejecucion siguiente (o la persona que abre el batch en
    el navegador) se encuentra con una peticion que nunca contesta, y el
    programa culpaba al navegador de un candado que habia dejado el.

    Planificar solo lee, asi que suelta en cuanto termina: entre revisar y
    escribir puede pasar un rato largo, y antes el batch se quedaba tomado
    todo ese tiempo. Escribir lo vuelve a tomar, que es lo unico que de
    verdad necesita ser el dueno, y lo suelta al acabar.
    """
    csv = corrida(tmp_path)
    cliente = cliente_con_lote()
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.descubrir(cliente, esperar=False)
    plan, indexador = trabajo.planificar(cliente)
    # Descubrir confirma el Batch Name y la huella de Log Page Number;
    # planificar lo vuelve a abrir para leer el indexado.
    assert cliente.abiertos == ["003SRO", "003SRO", "003SRO"]
    assert cliente.cerrados == ["003SRO", "003SRO", "003SRO"]

    trabajo.indexar(indexador, plan)
    assert cliente.abiertos == ["003SRO"] * 4
    assert cliente.cerrados == ["003SRO"] * 4


def test_un_lote_sin_nada_que_escribir_ni_se_toma(tmp_path):
    """Tomarlo seria bloquearlo para no escribir ni una pagina."""
    csv = corrida(tmp_path)
    cliente = cliente_con_lote()
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.descubrir(cliente, esperar=False)
    plan, indexador = trabajo.planificar(cliente)
    plan.paginas = []
    cliente.abiertos.clear()
    trabajo.indexar(indexador, plan)
    assert cliente.abiertos == []


def test_un_plan_que_falla_no_deja_el_lote_tomado(tmp_path):
    """El plan no sale, nadie va a escribir: quedarselo solo estorba."""
    from app.airvault.guards import ErrorDeGuarda

    csv = corrida(tmp_path)
    cliente = cliente_con_lote(paginas=3)
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.fijar_lote("003SRO")
    with pytest.raises(ErrorDeGuarda):
        trabajo.planificar(cliente)
    assert cliente.cerrados == ["003SRO"]


def test_soltar_un_lote_que_no_se_deja_no_tumba_la_corrida(tmp_path):
    """Cerrar es limpieza: si falla se anota, pero no se pierde lo escrito."""
    csv = corrida(tmp_path)
    cliente = cliente_con_lote()

    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.fijar_lote("003SRO")
    # Hay que tomarlo: fijar el lote no lo toma, y sin tomarlo cerrar ni
    # siquiera llega a preguntarle a AirVault.
    trabajo.tomar(cliente)

    intentos = []

    def no_se_deja(batch_id):
        intentos.append(batch_id)
        raise RuntimeError("AirVault no contesto")

    cliente.cerrar_lote = no_se_deja

    trabajo.cerrar(cliente)

    assert intentos == ["003SRO"]
    # Lo escrito sigue en su sitio: el fallo al soltar no deshace la corrida.
    manifiesto = manifiestos.cargar(tmp_path / "job")
    assert manifiesto.batch_id == "003SRO"
    # Y no se vuelve a pedir: insistir da un 500 que parece otro problema.
    trabajo.cerrar(cliente)
    assert intentos == ["003SRO"]


def test_el_lote_de_revisar_no_queda_tomado(tmp_path):
    """Es el que una persona tiene que abrir a mano; tomarlo la deja fuera."""
    csv = corrida(tmp_path)
    cliente = cliente_con_lote()
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.manifiesto.solo_subir = True
    trabajo.fijar_lote("003SRO")
    trabajo.planificar(cliente)
    assert cliente.cerrados == ["003SRO"]


def test_si_el_lote_esta_tomado_se_dice_quien_lo_tiene(tmp_path):
    """Un tiempo agotado sin explicacion no se puede resolver; un nombre si."""
    from app.airvault.session import ErrorDeConexion

    csv = corrida(tmp_path)
    cliente = cliente_con_lote()
    cliente.lotes = [lote("003SRO", "DP | BIT 18 AUG 2026 05 42", 2,
                          bloqueado_por="jperez@dominio.com")]

    def no_contesta(_batch_id):
        raise ErrorDeConexion("no contesto en 60s")

    cliente.abrir_lote = no_contesta
    trabajo = Trabajo.preparar(AirVaultConfig(), tmp_path / "job", csv,
                               "DP | BIT 18 AUG 2026 05 42")
    trabajo.fijar_lote("003SRO")
    with pytest.raises(ErrorDeConexion) as fallo:
        trabajo.planificar(cliente)
    assert "jperez@dominio.com" in str(fallo.value)
