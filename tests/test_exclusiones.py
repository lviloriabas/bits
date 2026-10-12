"""Recuperacion exacta de exclusiones, errores y cambios posteriores."""

import json
from zipfile import ZipFile

import pytest

from app.models.schemas import FieldResult, PageResult, ValidationReport
from app.reports import exclusiones
from app.reports.outputs import OutputOptions, write_outputs
from app.templates.schema import FieldTemplate, Template
from app.validation.depuracion import depurar_claves


def ejecucion(tmp_path):
    carpeta = tmp_path / "BITS prueba"
    plantilla = Template(name="fixture", fields=[
        FieldTemplate(id="log_number", x=0.1, y=0.1, w=0.2, h=0.1),
    ])
    reportes = [ValidationReport(pdf_path=str(tmp_path / "original.pdf"), template_name="fixture", pages=[
        PageResult(page_number=n, fields=[FieldResult(page_number=n, field_id="log_number",
                    field_type="ocr", value=valor)])
        for n, valor in ((1, "2357645"), (2, "2357645"), (3, "2357646"))
    ])]
    opciones = OutputOptions(template=plantilla, output_root=tmp_path, dpi=100,
                             crop_padding=0.01, run_dir=carpeta, skip_pdfs=True,
                             important_csv_columns=("log_number",))
    write_outputs(reportes, opciones)
    return carpeta, reportes, opciones


def excluir(carpeta, reportes, opciones, claves):
    quedan, _ = depurar_claves([r.model_copy(deep=True) for r in reportes], claves)
    exclusiones.guardar_con_respaldo(carpeta, lambda: write_outputs(quedan, opciones))
    return quedan


def test_restaura_bytes_y_paginas_sin_tocar_originales_ni_entregas(tmp_path):
    carpeta, reportes, opciones = ejecucion(tmp_path)
    original = tmp_path / "original.pdf"
    original.write_bytes(b"original intacto")
    entrega = carpeta / "entrega.pdf"
    entrega.write_bytes(b"entrega intacta")
    antes = exclusiones._capturar(carpeta)
    excluir(carpeta, reportes, opciones, {(0, 2)})
    assert exclusiones.motivo_restauracion(carpeta) == ""
    restaurados = exclusiones.restaurar_exclusion(carpeta)
    assert exclusiones._capturar(carpeta) == antes
    assert [p.page_number for p in restaurados[0].pages] == [1, 2, 3]
    assert original.read_bytes() == b"original intacto"
    assert entrega.read_bytes() == b"entrega intacta"
    assert not exclusiones.ruta_respaldo(carpeta).exists()


def test_segunda_exclusion_restaura_solo_la_ultima(tmp_path):
    carpeta, reportes, opciones = ejecucion(tmp_path)
    quedan = excluir(carpeta, reportes, opciones, {(0, 2)})
    intermedio = exclusiones._capturar(carpeta)
    excluir(carpeta, quedan, opciones, {(0, 3)})
    restaurados = exclusiones.restaurar_exclusion(carpeta)
    assert [p.page_number for p in restaurados[0].pages] == [1, 3]
    assert exclusiones._capturar(carpeta) == intermedio


def test_fallo_de_escritura_recupera_datos_y_respaldo_previo(tmp_path):
    carpeta, reportes, opciones = ejecucion(tmp_path)
    excluir(carpeta, reportes, opciones, {(0, 2)})
    antes = exclusiones._capturar(carpeta)
    respaldo = exclusiones.ruta_respaldo(carpeta).read_bytes()

    def falla():
        (carpeta / "datos" / f"{carpeta.name}.CSV").write_bytes(b"incompleto")
        (carpeta / "stats.json").unlink()
        raise OSError("archivo en uso")

    with pytest.raises(OSError, match="archivo en uso"):
        exclusiones.guardar_con_respaldo(carpeta, falla)
    assert exclusiones._capturar(carpeta) == antes
    assert exclusiones.ruta_respaldo(carpeta).read_bytes() == respaldo
    assert exclusiones.motivo_restauracion(carpeta) == ""


def test_respaldo_existe_antes_de_la_primera_escritura(tmp_path):
    carpeta, _reportes, _opciones = ejecucion(tmp_path)
    antes = exclusiones._capturar(carpeta)

    def escribir():
        with ZipFile(exclusiones.ruta_respaldo(carpeta)) as archivo:
            meta = json.loads(archivo.read("manifest.json"))
            assert meta["despues"] is None
            assert archivo.read(f"antes/datos/{carpeta.name}.CSV") == antes[f"datos/{carpeta.name}.CSV"]
        return carpeta

    exclusiones.guardar_con_respaldo(carpeta, escribir)


def test_interrupcion_inesperada_deja_respaldo_recuperable(tmp_path):
    carpeta, _reportes, _opciones = ejecucion(tmp_path)
    antes = exclusiones._capturar(carpeta)

    def interrumpir():
        (carpeta / "datos" / f"{carpeta.name}.CSV").write_bytes(b"guardado parcial")
        raise SystemExit()

    with pytest.raises(SystemExit):
        exclusiones.guardar_con_respaldo(carpeta, interrumpir)
    assert exclusiones.motivo_restauracion(carpeta) == ""
    exclusiones.restaurar_exclusion(carpeta)
    assert exclusiones._capturar(carpeta) == antes


def test_reexportacion_conserva_respaldo_y_bloquea_cambios_posteriores(tmp_path):
    carpeta, reportes, opciones = ejecucion(tmp_path)
    quedan = excluir(carpeta, reportes, opciones, {(0, 2)})
    write_outputs(quedan, opciones)
    assert exclusiones.ruta_respaldo(carpeta).is_file()
    csv = carpeta / "datos" / f"{carpeta.name}.CSV"
    csv.write_bytes(csv.read_bytes() + b"cambio posterior")
    actual = exclusiones._capturar(carpeta)
    assert "cambiaron" in exclusiones.motivo_restauracion(carpeta)
    with pytest.raises(ValueError, match="cambiaron"):
        exclusiones.restaurar_exclusion(carpeta)
    assert exclusiones._capturar(carpeta) == actual


def test_respaldo_corrupto_no_habilita_restauracion(tmp_path):
    carpeta, _reportes, _opciones = ejecucion(tmp_path)
    exclusiones.ruta_respaldo(carpeta).write_bytes(b"no es zip")
    assert "No se pudo verificar" in exclusiones.motivo_restauracion(carpeta)


def test_manifiesto_invalido_no_impide_abrir_la_revision(tmp_path):
    carpeta, _reportes, _opciones = ejecucion(tmp_path)
    with ZipFile(exclusiones.ruta_respaldo(carpeta), "w") as archivo:
        archivo.writestr("manifest.json", "[]")
    assert "No se pudo verificar" in exclusiones.motivo_restauracion(carpeta)


def test_respaldo_no_puede_restaurar_archivos_fuera_de_la_ejecucion(tmp_path):
    carpeta, _reportes, _opciones = ejecucion(tmp_path)
    with ZipFile(exclusiones.ruta_respaldo(carpeta), "w") as archivo:
        archivo.writestr("manifest.json", json.dumps({"version": 1, "ejecucion": carpeta.name,
                        "archivos": ["../ajeno.txt"], "despues": None}))
    assert "No se pudo verificar" in exclusiones.motivo_restauracion(carpeta)
    assert not (tmp_path / "ajeno.txt").exists()


def test_fallo_de_restauracion_deja_datos_actuales_y_respaldo(tmp_path, monkeypatch):
    carpeta, reportes, opciones = ejecucion(tmp_path)
    excluir(carpeta, reportes, opciones, {(0, 2)})
    actuales = exclusiones._capturar(carpeta)
    atomico = exclusiones._atomico
    llamadas = 0

    def fallar_una_vez(ruta, datos):
        nonlocal llamadas
        llamadas += 1
        if llamadas == 2:
            raise OSError("archivo ocupado")
        atomico(ruta, datos)

    monkeypatch.setattr(exclusiones, "_atomico", fallar_una_vez)
    with pytest.raises(OSError, match="ocupado"):
        exclusiones.restaurar_exclusion(carpeta)
    assert exclusiones._capturar(carpeta) == actuales
    assert exclusiones.motivo_restauracion(carpeta) == ""


def test_dos_ventanas_no_escriben_la_misma_ejecucion_a_la_vez(tmp_path):
    import threading

    fallos = []
    with exclusiones.turno_datos(tmp_path):
        def intentar():
            try:
                with exclusiones.turno_datos(tmp_path):
                    fallos.append("entro")
            except ValueError as exc:
                fallos.append(str(exc))
        hilo = threading.Thread(target=intentar)
        hilo.start()
        hilo.join()
    assert len(fallos) == 1 and "Otra ventana" in fallos[0]
