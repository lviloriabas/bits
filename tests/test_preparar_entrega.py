"""Preparacion automatica de entregas sin OCR ni cambios en los reportes."""

import csv
import json

import pymupdf
import pytest
from PySide6.QtCore import Qt

from app.airvault.flujo import comprobar_entrega, ruta_indice_paginas
from app.airvault.mapping import ResolutorFlota
from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker, TrabajoCancelado, ROL_SE_PUEDE_SUBIR
from app.models.schemas import FieldResult, PageResult, ValidationReport
from app.reports import preparar_entrega as modulo
from app.reports.json_reporter import JsonReporter
from app.reports.preparar_entrega import preparar_entrega, ErrorPreparacionEntrega


def guardada(raiz, nombre="BITS prueba", *, revisar=True):
    fuente = raiz / "input" / "processed" / "original-2.pdf"
    fuente.parent.mkdir(parents=True, exist_ok=True)
    if not fuente.exists():
        with pymupdf.open() as pdf:
            for n in range(3):
                p = pdf.new_page()
                p.insert_text((40, 40), f"Pagina fisica {n + 1}")
            pdf.save(fuente)
    carpeta = raiz / "output" / nombre
    datos = carpeta / "datos"
    datos.mkdir(parents=True)
    ruta = datos / f"{nombre}.CSV"
    filas = [dict(file="original.pdf", page=n, log_number=str(2357644 + n),
                  matricula="HP-1832CMP", date="2026/09/27",
                  review="true" if n == 2 and revisar else "false") for n in range(1, 4)]
    with ruta.open("w", encoding="utf-8-sig", newline="") as archivo:
        writer = csv.DictWriter(archivo, fieldnames=list(filas[0]))
        writer.writeheader()
        writer.writerows(filas)
    completo = ruta.with_name(f"{ruta.stem}_completo.CSV")
    completo.write_bytes(ruta.read_bytes())
    report = ValidationReport(pdf_path=str(fuente), source_name="original.pdf", template_name="fixture", pages=[
        PageResult(page_number=n, date="2026/09/27", fields=[
            FieldResult(page_number=n, field_id=campo, field_type="ocr", value=str(valor))
            for campo, valor in (("log_number", 2357644+n), ("matricula", "HP-1832CMP"))
        ]) for n in range(1, 4)
    ])
    JsonReporter().write_consolidated([report], ruta.with_suffix(".json"), corrida=nombre)
    (carpeta / "stats.json").write_text(json.dumps({"total_paginas": 3}), encoding="utf-8")
    return ruta, fuente


def resolutor():
    return ResolutorFlota({"HP-1832CMP": {"fleet": "737NG", "lessor": "COPA"}})


def datos(csv):
    carpeta = csv.parent.parent
    return {str(p.relative_to(carpeta)): p.read_bytes()
            for p in (csv, csv.with_name(f"{csv.stem}_completo.CSV"), csv.with_suffix(".json"), carpeta / "stats.json")}


def test_prepara_pdf_e_indice_respetando_review_y_todos_los_datos(tmp_path, monkeypatch):
    ruta, fuente = guardada(tmp_path)
    antes, original = datos(ruta), fuente.read_bytes()
    monkeypatch.setattr("app.reports.outputs.write_outputs", lambda *a, **k: pytest.fail("No debe reescribir los datos"))
    partes, hecha = preparar_entrega(ruta, tmp_path, resolutor=resolutor())
    assert hecha and len(partes) == 2
    assert datos(ruta) == antes and fuente.read_bytes() == original
    normales = [p for parte in partes if not parte.revisar for p in parte.paginas if "archivo" in p]
    revisar = [p for parte in partes if parte.revisar for p in parte.paginas if "archivo" in p]
    assert [p["pagina"] for p in normales] == [1, 3]
    assert [p["pagina"] for p in revisar] == [2]
    assert all(p["archivo"] == "original.pdf" for p in normales + revisar)
    textos = ""
    for parte in partes:
        with pymupdf.open(parte.pdf) as pdf:
            textos += "".join(page.get_text() for page in pdf)
    assert all(f"Pagina fisica {n}" in textos for n in (1, 2, 3))


def test_reutiliza_entrega_valida_sin_crear_mas_pdf(tmp_path, monkeypatch):
    ruta, _ = guardada(tmp_path)
    preparar_entrega(ruta, tmp_path, resolutor=resolutor())
    antes = {p.name: p.read_bytes() for p in ruta.parent.parent.glob("*.pdf")}
    monkeypatch.setattr(modulo, "escribir_entrega", lambda *a, **k: pytest.fail("Ya esta preparada"))
    _partes, hecha = preparar_entrega(ruta, tmp_path)
    assert not hecha
    assert {p.name: p.read_bytes() for p in ruta.parent.parent.glob("*.pdf")} == antes


def test_prepara_originales_tras_mover_la_carpeta_portable(tmp_path):
    ruta, _ = guardada(tmp_path)
    companion = ruta.with_suffix(".json")
    payload = json.loads(companion.read_text(encoding="utf-8"))
    payload["reportes"][0]["pdf_path"] = "X:/EquipoAnterior/input/processed/original-2.pdf"
    companion.write_text(json.dumps(payload), encoding="utf-8")
    antes = datos(ruta)
    entrega, hecha = preparar_entrega(ruta, tmp_path, resolutor=resolutor())
    assert hecha and entrega
    assert datos(ruta) == antes


def test_reexporta_entrega_antigua_o_con_pdf_faltante_y_conserva_copias(tmp_path):
    ruta, _ = guardada(tmp_path)
    vieja = ruta.parent.parent / f"{ruta.stem}.pdf"
    vieja.write_bytes(b"entrega anterior intacta")
    preparar_entrega(ruta, tmp_path, resolutor=resolutor())
    assert vieja.read_bytes() == b"entrega anterior intacta"
    comprobar_entrega(ruta)[0].pdf.unlink()
    _partes, hecha = preparar_entrega(ruta, tmp_path, resolutor=resolutor())
    assert hecha and comprobar_entrega(ruta)
    assert vieja.read_bytes() == b"entrega anterior intacta"


@pytest.mark.parametrize("falta", ["original", "json", "cancelada", "filas"])
def test_informa_datos_que_no_puede_reconstruir_sin_generar_entrega(tmp_path, falta):
    ruta, fuente = guardada(tmp_path)
    if falta == "original":
        fuente.unlink()
    elif falta == "json":
        ruta.with_suffix(".json").unlink()
    else:
        payload = json.loads(ruta.with_suffix(".json").read_text(encoding="utf-8"))
        if falta == "cancelada":
            payload["reportes"][0]["cancelled"] = True
        else:
            payload["reportes"][0]["pages"].pop()
        ruta.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ErrorPreparacionEntrega):
        preparar_entrega(ruta, tmp_path)
    assert not ruta_indice_paginas(ruta).exists()
    assert not list(ruta.parent.parent.glob("*.pdf"))


def test_cancelar_durante_la_copia_no_deja_entrega_parcial(tmp_path):
    ruta, _ = guardada(tmp_path)
    antes = datos(ruta)
    visitas = 0

    def parar():
        nonlocal visitas
        visitas += 1
        if visitas == 4:
            raise TrabajoCancelado()

    with pytest.raises(TrabajoCancelado):
        preparar_entrega(ruta, tmp_path, resolutor=resolutor(), comprobar_cancelacion=parar)
    assert datos(ruta) == antes
    assert not list(ruta.parent.parent.glob("*.pdf"))
    assert not ruta_indice_paginas(ruta).exists()
    assert not list((ruta.parent.parent / "logs").iterdir())


def test_fallo_al_publicar_indice_recupera_indice_anterior_y_quita_pdf_nuevos(tmp_path, monkeypatch):
    ruta, _ = guardada(tmp_path)
    indice = ruta_indice_paginas(ruta)
    indice.write_bytes(b"indice anterior incompleto")
    monkeypatch.setattr(modulo, "escribir_indice_paginas", lambda *a: (_ for _ in ()).throw(OSError("disco lleno")))
    with pytest.raises(OSError):
        preparar_entrega(ruta, tmp_path, resolutor=resolutor())
    assert indice.read_bytes() == b"indice anterior incompleto"
    assert not list(ruta.parent.parent.glob("*.pdf"))


def test_indice_bloqueado_conserva_datos_y_quita_pdf_nuevos(tmp_path, monkeypatch):
    ruta, _ = guardada(tmp_path)
    indice = ruta_indice_paginas(ruta)
    indice.write_bytes(b"indice anterior incompleto")
    reemplazar = modulo.os.replace

    def bloquear(origen, destino):
        if destino == indice:
            raise PermissionError("indice abierto")
        reemplazar(origen, destino)

    monkeypatch.setattr(modulo.os, "replace", bloquear)
    with pytest.raises(PermissionError):
        preparar_entrega(ruta, tmp_path, resolutor=resolutor())
    assert indice.read_bytes() == b"indice anterior incompleto"
    assert not list(ruta.parent.parent.glob("*.pdf"))


def test_fallo_despues_de_publicar_recupera_indice_anterior(tmp_path, monkeypatch):
    ruta, _ = guardada(tmp_path)
    indice = ruta_indice_paginas(ruta)
    indice.write_bytes(b"indice anterior incompleto")
    monkeypatch.setattr("app.airvault.flujo.comprobar_entrega",
                        lambda *a: (_ for _ in ()).throw(ValueError("indice invalido")))
    with pytest.raises(ValueError):
        preparar_entrega(ruta, tmp_path, resolutor=resolutor())
    assert indice.read_bytes() == b"indice anterior incompleto"
    assert not list(ruta.parent.parent.glob("*.pdf"))


def test_el_hilo_prepara_toda_la_seleccion_antes_de_conectar(app, tmp_path, monkeypatch):
    primera, _ = guardada(tmp_path)
    segunda, _ = guardada(tmp_path, "BITS segunda")
    eventos = []
    worker = TrabajoAirVaultWorker("subir", {"raiz": tmp_path, "config": object(),
        "paginas_por_batch": 500, "ejecuciones_subida": [
            {"csv": str(csv), "carpeta_job": tmp_path / f"job-{n}", "nombre_lote": csv.stem}
            for n, csv in enumerate((primera, segunda))
        ]})
    monkeypatch.setattr(worker, "_conectar", lambda: eventos.append("conectar") or object())
    monkeypatch.setattr(worker, "_enviar", lambda *a: eventos.append("enviar"))
    monkeypatch.setattr("app.airvault.flujo.preparar_partes", lambda *a, **k: [])
    worker.entrega_preparada.connect(lambda csv: eventos.append(csv))
    worker._subir()
    assert eventos == [str(primera), str(segunda), "conectar", "enviar"]


def test_fallo_de_una_ejecucion_no_inicia_ninguna_subida(app, tmp_path, monkeypatch):
    primera, _ = guardada(tmp_path)
    segunda, _ = guardada(tmp_path, "BITS segunda")
    segunda.with_suffix(".json").unlink()
    worker = TrabajoAirVaultWorker("subir", {"raiz": tmp_path, "config": object(),
        "paginas_por_batch": 500, "ejecuciones_subida": [
            {"csv": str(csv), "carpeta_job": tmp_path / "job", "nombre_lote": csv.stem}
            for csv in (primera, segunda)
        ]})
    monkeypatch.setattr(worker, "_conectar", lambda: pytest.fail("No debe conectar"))
    monkeypatch.setattr("app.airvault.flujo.preparar_partes", lambda *a, **k: pytest.fail("Todavia no debe preparar cargas"))
    errores = []
    worker.preparacion_fallida.connect(errores.append)
    worker._subir()
    assert len(errores) == 1 and "datos procesados" in errores[0]


def test_seleccion_gris_se_conserva_y_cambia_de_estado_al_prepararse(app, tmp_path):
    ruta, _ = guardada(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    indice = ventana.historial.findData(str(ruta))
    assert not ventana.historial.itemData(indice, ROL_SE_PUEDE_SUBIR)
    assert ventana.historial.itemData(indice, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    ventana._refrescar_historial()
    assert str(ruta) in ventana._corridas_seleccionadas()
    preparar_entrega(ruta, tmp_path, resolutor=resolutor())
    ventana._al_preparar_entrega(str(ruta))
    assert ventana.historial.itemData(indice, ROL_SE_PUEDE_SUBIR)
    assert ventana.historial.itemData(indice, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    ventana.close()


def test_error_local_se_muestra_sin_reintentar_subidas(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    ventana._proceso_iniciado = True
    ventana._comprobar_al_terminar = True
    texto = "Faltan los datos procesados de esta ejecución. Vuelva a procesarla antes de subirla."
    ventana._al_fallar(texto, preparacion=True)
    assert texto in ventana.resumen.text()
    assert not ventana._proceso_iniciado
    assert not ventana._comprobar_al_terminar
    assert not ventana._vigilando()
    ventana.close()


def test_continuar_preparacion_pausada_conserva_toda_la_seleccion(app, tmp_path, monkeypatch):
    primera, _ = guardada(tmp_path)
    segunda, _ = guardada(tmp_path, "BITS segunda")
    ventana = AirVaultWindow(tmp_path)
    ventana._refrescar_historial()
    indice = ventana.historial.findData(str(primera))
    ventana.historial.marcar(indice)
    assert set(ventana._corridas_seleccionadas()) == {str(primera), str(segunda)}
    lanzados = []
    monkeypatch.setattr(ventana, "_lanzar", lambda modo, estado: lanzados.append((modo, estado.copy())))
    ventana._subir_a_mano()
    ventana._pausar_proceso()
    assert ventana._preparacion_pendiente
    ventana._empezar_o_continuar()
    assert [modo for modo, _ in lanzados] == ["subir", "subir"]
    assert {e["csv"] for e in lanzados[-1][1]["ejecuciones_subida"]} == {str(primera), str(segunda)}
    ventana._cancelar()
    assert not ventana._preparacion_pendiente
    ventana.close()
