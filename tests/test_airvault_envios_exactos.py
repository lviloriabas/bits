"""Carga unica por PDF y porcentaje del batch, sin tocar AirVault real."""
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess
import sys
import threading

import pytest

from app.airvault import envios_exactos, duplicados
from app.airvault.config import AirVaultConfig, guardar_politica_duplicados
from app.airvault.flujo import (
    Trabajo, ErrorDeCorrida, autorizar_posible_duplicado, carpeta_del_libro,
    completar_partes, duplicado_bloquea, revisar_duplicado, subir_partes,
)
from app.airvault.model import EstadoEtapa, Registro
from tests.test_airvault_sin_duplicar import _trabajo, BuscadorFalso
from tests.test_airvault_comprobar import SesionFalsa
from tests.airvault_fake import ClienteFalso


def subidor(llamadas, resultado=None):
    def enviar(archivo, valores, avisar=None):
        llamadas.append(Path(archivo))
        return resultado or SimpleNamespace(ok=True)
    return SimpleNamespace(subir=enviar)


def test_reserva_antes_de_la_peticion_y_sobrevive_a_reabrir(tmp_path):
    trabajo = _trabajo(tmp_path)
    raiz = carpeta_del_libro(trabajo)
    archivo = trabajo.manifiesto.pdf_origen
    def enviar(*args, **kwargs):
        assert envios_exactos.motivo(raiz, archivo, trabajo.config)
        raise ConnectionError("respuesta perdida")
    with pytest.raises(ConnectionError):
        envios_exactos.enviar(raiz, trabajo, archivo, SimpleNamespace(subir=enviar), {})
    reabierto = Trabajo.cargar(trabajo.config, trabajo.carpeta)
    assert "Este mismo PDF" in revisar_duplicado(reabierto)
    assert duplicado_bloquea(reabierto) is False  # el motivo aun no se ha marcado
    llamadas = []
    with pytest.raises(envios_exactos.CargaRepetida):
        envios_exactos.enviar(raiz, reabierto, archivo, subidor(llamadas), {})
    assert llamadas == []


def test_pdf_renombrado_y_sin_manifiesto_se_bloquea_con_casilla_apagada(tmp_path):
    original = _trabajo(tmp_path, "lunes", "job-1")
    raiz = carpeta_del_libro(original)
    envios_exactos.enviar(raiz, original, original.manifiesto.pdf_origen, subidor([]), {})
    copia = tmp_path / "renombrado.pdf"
    shutil.copyfile(original.manifiesto.pdf_origen, copia)
    shutil.rmtree(original.carpeta)
    nuevo = _trabajo(tmp_path, "martes", "job-2")
    nuevo.config = nuevo.config.with_overrides(detener_por_duplicados=False, porcentaje_duplicados=100)
    nuevo.manifiesto.pdf_origen = str(copia)
    with pytest.raises(ErrorDeCorrida, match="mismo documento dos veces"):
        nuevo.subir(object())
    assert duplicado_bloquea(nuevo)
    assert Trabajo.cargar(nuevo.config, nuevo.carpeta).manifiesto.duplicado_exacto


def test_autorizacion_sirve_para_un_solo_intento(tmp_path):
    trabajo = _trabajo(tmp_path)
    raiz = carpeta_del_libro(trabajo)
    archivo = trabajo.manifiesto.pdf_origen
    llamadas = []
    envios_exactos.enviar(raiz, trabajo, archivo, subidor(llamadas), {})
    autorizar_posible_duplicado(trabajo)
    envios_exactos.enviar(raiz, trabajo, archivo, subidor(llamadas), {})
    with pytest.raises(envios_exactos.CargaRepetida):
        envios_exactos.enviar(raiz, trabajo, archivo, subidor(llamadas), {})
    assert len(llamadas) == 2
    assert not trabajo._duplicado_permitido


def test_autorizacion_no_se_conserva_al_cerrar(tmp_path):
    trabajo = _trabajo(tmp_path)
    autorizar_posible_duplicado(trabajo)
    assert not Trabajo.cargar(trabajo.config, trabajo.carpeta)._duplicado_permitido


def test_rechazo_confirmado_deja_reintentar(tmp_path):
    trabajo = _trabajo(tmp_path)
    raiz = carpeta_del_libro(trabajo)
    archivo = trabajo.manifiesto.pdf_origen
    envios_exactos.enviar(raiz, trabajo, archivo, subidor([], SimpleNamespace(ok=False)), {})
    assert envios_exactos.motivo(raiz, archivo, trabajo.config) == ""
    envios_exactos.enviar(raiz, trabajo, archivo, subidor([]), {})


def test_otro_proceso_no_puede_cargar_durante_un_envio(tmp_path):
    script = (
        "from app.airvault.envios_exactos import _exclusiva, CargaRepetida; "
        "import sys\n"
        "try:\n with _exclusiva(sys.argv[1]): sys.exit(2)\n"
        "except CargaRepetida: sys.exit(0)\n"
    )
    with envios_exactos._exclusiva(tmp_path):
        resultado = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=15)
    assert resultado.returncode == 0


def test_dos_hilos_del_mismo_pdf_solo_envian_una_vez(tmp_path):
    trabajo = _trabajo(tmp_path)
    raiz = carpeta_del_libro(trabajo)
    comenzo, terminar = threading.Event(), threading.Event()
    llamadas, errores = [], []
    def enviar(*args, **kwargs):
        llamadas.append(1)
        comenzo.set()
        assert terminar.wait(5)
        return SimpleNamespace(ok=True)
    def cargar():
        try:
            envios_exactos.enviar(raiz, trabajo, trabajo.manifiesto.pdf_origen,
                                 SimpleNamespace(subir=enviar), {})
        except Exception as error:
            errores.append(error)
    hilo = threading.Thread(target=cargar)
    hilo.start()
    try:
        assert comenzo.wait(5)
        with pytest.raises(envios_exactos.CargaRepetida):
            envios_exactos.enviar(raiz, trabajo, trabajo.manifiesto.pdf_origen, subidor([]), {})
    finally:
        terminar.set()
        hilo.join(5)
    assert llamadas == [1]
    assert errores == []


@pytest.mark.parametrize("existentes,umbral,bloquea", [(1,25,True), (1,26,False), (4,100,True), (3,100,False)])
def test_porcentaje_sobre_todo_el_batch_sin_separadores(tmp_path, existentes, umbral, bloquea):
    trabajo = _trabajo(tmp_path)
    trabajo.config = trabajo.config.with_overrides(porcentaje_duplicados=umbral)
    trabajo.manifiesto.registros = [Registro(seq=i+1, log_number=str(2000000+i)) for i in range(4)]
    trabajo.manifiesto.registros.append(Registro(seq=5, es_separador=True))
    motivo = revisar_duplicado(trabajo, BuscadorFalso([str(2000000+i) for i in range(existentes)]))
    assert bool(motivo) is bloquea
    if bloquea:
        assert f"{existentes} de 4" in motivo


def test_consulta_todas_y_no_solo_tres_bitacoras(tmp_path):
    trabajo = _trabajo(tmp_path)
    trabajo.manifiesto.registros = [Registro(seq=i+1, log_number=str(2000000+i)) for i in range(20)]
    assert revisar_duplicado(trabajo, BuscadorFalso(["2000001"]))


def test_migra_historial_previo_incluso_completado(tmp_path):
    trabajo = _trabajo(tmp_path)
    trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    trabajo.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
    trabajo.guardar()
    nuevo = _trabajo(tmp_path, "otra", "job-2")
    shutil.copyfile(trabajo.manifiesto.pdf_origen, nuevo.manifiesto.pdf_origen)
    assert "Este mismo PDF" in revisar_duplicado(nuevo)


def test_completar_directamente_respeta_el_bloqueo_exacto(tmp_path):
    trabajo = _trabajo(tmp_path)
    trabajo.config = trabajo.config.with_overrides(detener_por_duplicados=False)
    trabajo.manifiesto.posible_duplicado = "mismo PDF"
    trabajo.manifiesto.duplicado_exacto = True
    resultado = trabajo.completar(object())
    assert not resultado.completado
    assert "no se cierra" in resultado.detalle


def test_memoria_danada_no_permite_subir(tmp_path):
    trabajo = _trabajo(tmp_path)
    raiz = carpeta_del_libro(trabajo)
    raiz.mkdir(parents=True, exist_ok=True)
    (raiz / envios_exactos.ARCHIVO).write_bytes(b"no es una base valida")
    with pytest.raises(Exception):
        trabajo.subir(object())


def test_guardar_porcentaje_conserva_otras_opciones(tmp_path):
    import json
    ruta = tmp_path / "airvault.json"
    ruta.write_text(json.dumps({"repo_id": 77}))
    assert guardar_politica_duplicados(ruta, True, 35)
    config = AirVaultConfig.load(ruta)
    assert config.repo_id == 77
    assert config.porcentaje_duplicados == 35
    assert config.detener_por_duplicados


@pytest.mark.parametrize("porcentaje", [0, 101, -1, 1.5, True, "25"])
def test_porcentaje_invalido_se_rechaza(porcentaje):
    with pytest.raises(ValueError):
        AirVaultConfig(porcentaje_duplicados=porcentaje)



def test_orden_forzada_requiere_autorizacion_para_pdf_previo(tmp_path, monkeypatch):
    trabajo = _trabajo(tmp_path)
    trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    trabajo.guardar()
    llamadas = []
    monkeypatch.setattr(Trabajo, "subir", lambda *a, **k: llamadas.append(1))
    fallos = subir_partes([trabajo], SesionFalsa(), cliente=ClienteFalso(),
                         forzados=[str(trabajo.carpeta)])
    assert fallos
    assert llamadas == []
    assert duplicado_bloquea(trabajo)
    autorizar_posible_duplicado(trabajo)
    monkeypatch.setattr(Trabajo, "descubrir", lambda *a, **k: "nuevo")
    subir_partes([trabajo], SesionFalsa(), cliente=ClienteFalso(),
                 forzados=[str(trabajo.carpeta)])
    assert llamadas == [1]


def test_controles_de_busqueda_salen_de_la_carpeta_de_trabajos(tmp_path):
    from app.airvault.flujo import buscador_de
    trabajo = _trabajo(tmp_path)
    trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    trabajo.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
    duplicados.anotar(carpeta_del_libro(trabajo), [trabajo])
    buscador = buscador_de(SesionFalsa(), trabajo.config, tmp_path / "airvault.json")
    assert buscador.controles
    assert trabajo.manifiesto.registros[0].log_number in buscador.controles



def test_cierre_abrupto_conserva_reserva_y_libera_candado(tmp_path):
    archivo = tmp_path / "batch.pdf"
    archivo.write_bytes(b"PDF enviado antes de cerrar")
    raiz = tmp_path / "historial"
    codigo = (
        "import os,sys; from types import SimpleNamespace; "
        "from app.airvault import envios_exactos; "
        "from app.airvault.config import AirVaultConfig; "
        "trabajo=SimpleNamespace(config=AirVaultConfig(), "
        "manifiesto=SimpleNamespace(nombre_batch='batch'), _duplicado_permitido=False); "
        "subidor=SimpleNamespace(subir=lambda *a, **k: os._exit(0)); "
        "envios_exactos.enviar(sys.argv[1], trabajo, sys.argv[2], subidor, {})"
    )
    proceso = subprocess.run([sys.executable, "-c", codigo, str(raiz), str(archivo)], timeout=15)
    assert proceso.returncode == 0
    with envios_exactos._exclusiva(raiz):
        assert "Este mismo PDF" in envios_exactos.motivo(raiz, archivo, AirVaultConfig())
