"""Subida por Quick Upload: trozos y confirmacion de indices."""

from __future__ import annotations

from app.airvault.config import (
    CAMPO_BATCH_NAME,
    CAMPO_BATCH_USERNAME,
    CAMPO_END_DATE,
    CAMPO_LOG_NUMBER,
    CAMPO_MATRICULA,
)
from app.airvault.uploader import (
    CAMPOS_QUICK_UPLOAD,
    TROZO_BYTES,
    trozos,
    valores_quick_upload,
)


def test_solo_viajan_los_campos_que_quick_upload_admite():
    valores = {
        CAMPO_MATRICULA: "HP-1848CMP",
        CAMPO_LOG_NUMBER: "2287325",
        CAMPO_END_DATE: "08/31/2026",
    }
    enviados = valores_quick_upload(valores)
    ids = {int(v["FieldId"]) for v in enviados}
    assert ids == set(CAMPOS_QUICK_UPLOAD)
    # Log Page Number y End Date no estan habilitados en ese modulo, asi
    # que el indexado posterior sigue siendo obligatorio.
    assert CAMPO_LOG_NUMBER not in ids
    assert CAMPO_END_DATE not in ids


def test_la_matricula_si_viaja():
    enviados = valores_quick_upload({CAMPO_MATRICULA: "HP-1848CMP"})
    matricula = next(v for v in enviados
                     if int(v["FieldId"]) == CAMPO_MATRICULA)
    assert matricula["Value"] == "HP-1848CMP"
    assert matricula["Dirty"] is True


def test_campo_sin_valor_va_vacio_y_no_sucio():
    enviados = valores_quick_upload({})
    assert all(v["Value"] == "" for v in enviados)
    assert all(v["Dirty"] is False for v in enviados)


def test_archivo_pequeno_va_en_un_solo_trozo(tmp_path):
    ruta = tmp_path / "chico.pdf"
    ruta.write_bytes(b"x" * 100)
    partes = list(trozos(ruta))
    assert len(partes) == 1
    assert partes[0][0] == 0 and partes[0][1] == 1
    assert partes[0][2] == b"x" * 100


def test_archivo_grande_se_parte(tmp_path):
    ruta = tmp_path / "grande.pdf"
    ruta.write_bytes(b"y" * (TROZO_BYTES + 10))
    partes = list(trozos(ruta))
    assert [p[0] for p in partes] == [0, 1]
    assert all(p[1] == 2 for p in partes)
    assert b"".join(p[2] for p in partes) == b"y" * (TROZO_BYTES + 10)


def test_archivo_vacio_manda_un_trozo(tmp_path):
    ruta = tmp_path / "vacio.pdf"
    ruta.write_bytes(b"")
    partes = list(trozos(ruta))
    assert len(partes) == 1 and partes[0][2] == b""


def test_la_subida_no_lleva_el_vuelo():
    """``Description`` es de cada pagina, no del batch.

    La subida clasifica el archivo entero con un solo juego de valores; el
    vuelo cambia de una bitacora a la siguiente y ponerlo ahi seria darle a
    las 400 paginas el vuelo de la primera. Quick Upload ni siquiera expone
    ese campo: lo escribe el indexado, pagina por pagina.
    """
    from app.airvault.config import CAMPO_DESCRIPCION
    from app.airvault.uploader import CAMPOS_QUICK_UPLOAD, valores_quick_upload

    assert CAMPO_DESCRIPCION not in CAMPOS_QUICK_UPLOAD
    salida = valores_quick_upload({CAMPO_DESCRIPCION: "CM137"})
    assert all(v["FieldId"] != str(CAMPO_DESCRIPCION) for v in salida)


def test_el_nombre_del_batch_tambien_viaja_en_batch_username():
    """Los dos campos de nombre salen con el mismo valor.

    Quick Upload expone ``C_BatchName`` y ``C_BUName``. Las cargas que
    dejaban el segundo vacio son las que AirVault publicaba como
    ``Empty-Batch``, asi que ahora los dos llevan el nombre del batch.
    """
    enviados = valores_quick_upload({CAMPO_BATCH_NAME: "BITS 28 AUG 2026"})
    nombres = {
        int(v["FieldId"]): v
        for v in enviados
        if int(v["FieldId"]) in (CAMPO_BATCH_NAME, CAMPO_BATCH_USERNAME)
    }
    assert nombres[CAMPO_BATCH_NAME]["Value"] == "BITS 28 AUG 2026"
    assert nombres[CAMPO_BATCH_USERNAME]["Value"] == "BITS 28 AUG 2026"
    assert nombres[CAMPO_BATCH_USERNAME]["Dirty"] is True


def test_finishupload_se_manda_una_sola_vez_si_no_contesta(tmp_path):
    """Sin respuesta no se sabe si AirVault creo el batch: se busca en la cola."""
    from app.airvault.session import RespuestaPerdida
    from app.airvault.uploader import SubidorQuickUpload

    class Sesion:
        def __init__(self):
            self.rutas = []
            self.opciones = []

        def post(self, ruta, **extra):
            self.rutas.append(ruta)
            self.opciones.append({
                clave: extra[clave]
                for clave in ("repetir_sin_respuesta", "tiempo_limite")
                if clave in extra
            })
            if ruta.endswith("FinishUpload"):
                raise RespuestaPerdida("no contesto")

    archivo = tmp_path / "entrega.pdf"
    archivo.write_bytes(b"%PDF-1.4\n")
    sesion = Sesion()

    resultado = SubidorQuickUpload(sesion, 3209).subir(archivo, {})

    assert resultado.ok and not resultado.confirmada
    assert sesion.rutas == [
        "/quickuploadex/Home/Upload/", "/quickuploadex/Home/FinishUpload",
    ]
    # Los trozos se pueden repetir; el cierre de la carga no.
    assert sesion.opciones[0] == {}
    assert sesion.opciones[1]["repetir_sin_respuesta"] is False
    assert sesion.opciones[1]["tiempo_limite"] >= 600


def test_un_batch_username_propio_no_se_pisa():
    enviados = valores_quick_upload({
        CAMPO_BATCH_NAME: "BITS 28 AUG 2026",
        CAMPO_BATCH_USERNAME: "otro",
    })
    usuario = next(v for v in enviados
                   if int(v["FieldId"]) == CAMPO_BATCH_USERNAME)
    assert usuario["Value"] == "otro"
