"""Un batch que Web Search no encontro se vuelve a subir aunque sea REVISAR."""

import base64

import pytest

from app.airvault.confirmacion import huella_de_numeros
from app.airvault.flujo import estado_local
from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker
from tests.airvault_fake import ClienteFalso
from tests.test_airvault_chequeo_completo import comprobar
from tests.test_airvault_confirmacion import caso


def revisar_ausente(tmp_path, app, sin_numero=(3, 9, 20), publicadas=()):
    """Un REVISAR con bitacoras sin numero que el chequeo dejo en rojo."""
    t, buscador, filas, consultas = caso(tmp_path, 50)
    t.manifiesto.solo_subir = True
    for indice in sin_numero:
        t.manifiesto.registros[indice].log_number = ""
    t.guardar()
    filas[:] = [filas[indice] for indice in publicadas]
    _, avisos = comprobar(t, buscador, app)
    return t, buscador, consultas, avisos[-1]["ausentes"]


def recuperar(ausentes, buscador, monkeypatch):
    worker = TrabajoAirVaultWorker("recuperar_ausentes", {
        "pendientes_subida": ausentes, "buscador": buscador,
    })
    envios, avisos = [], []
    monkeypatch.setattr(worker, "_conectar", lambda: ClienteFalso())
    monkeypatch.setattr(worker, "_enviar", lambda trabajos, _cliente: envios.extend(trabajos))
    worker.subido.connect(avisos.append)
    worker._recuperar_ausentes()
    return envios, avisos


def test_las_bitacoras_sin_numero_no_impiden_volver_a_subir(app, tmp_path, monkeypatch):
    """Un REVISAR siempre trae alguna, y por eso nunca se reenviaba."""
    t, buscador, consultas, ausentes = revisar_ausente(tmp_path, app)
    consultas.clear()

    envios, avisos = recuperar(ausentes, buscador, monkeypatch)

    assert [trabajo.carpeta for trabajo in envios] == [t.carpeta]
    # Antes de reenviar se pregunto por todas las que tienen numero.
    assert len(consultas) == 47
    assert not avisos


def test_sin_ningun_numero_buscable_no_se_reenvia_a_ciegas(app, tmp_path, monkeypatch):
    t, buscador, _, ausentes = revisar_ausente(tmp_path, app)
    for registro in t.manifiesto.registros:
        registro.log_number = ""
    # La prueba de ausencia sigue siendo la del contenido que se reviso.
    t.manifiesto.websearch_huella = huella_de_numeros(t.manifiesto)
    t.guardar()

    envios, avisos = recuperar(ausentes, buscador, monkeypatch)

    assert not envios
    assert [nombre for nombre, _ in avisos[0]["fallos"]] == [t.manifiesto.nombre_batch]


def test_el_motivo_de_no_reenviar_llega_a_la_bitacora(app, tmp_path, monkeypatch):
    """Decia «No se pudo subir. Vuelva a intentarlo» de algo que no era eso."""
    t, buscador, _, ausentes = revisar_ausente(tmp_path, app)
    # Una bitacora fuera de la muestra aparece publicada al revisarlas todas.
    numero = t.manifiesto.registros[1].log_number
    buscador.sesion.get = lambda _ruta, params, _get=buscador.sesion.get: (
        {"rows": [{"C_DocNo": numero}], "records": 1}
        if _numero_pedido(params) == numero else _get(_ruta, params)
    )

    envios, avisos = recuperar(ausentes, buscador, monkeypatch)

    assert not envios
    motivo = avisos[0]["fallos"][0][1]
    assert "1 de 47" in motivo
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [t]
    ventana._estados = [estado_local(t)]
    ventana._al_subir(avisos[0])
    lineas = [ventana.bitacora.item(i).text() for i in range(ventana.bitacora.count())]
    assert any(motivo in linea for linea in lineas)
    assert not any("Vuelva a intentarlo" in linea for linea in lineas)


def _numero_pedido(params):
    return base64.b64decode(params["encodedValues"]).decode().split("=", 1)[1]


@pytest.mark.parametrize("detalle,visible", [
    ("No esta el archivo de entrega BITS 05 OCT 2026 12 17 REVISAR.pdf",
     "No se encontró el PDF de la entrega. Vuelva a exportar la ejecución."),
    ("No se inicia otra carga hasta confirmar la anterior: DP | BITS REVISAR",
     "AirVault aún no confirma una carga anterior. Vuelva a revisar en AirVault para continuar."),
    ("algo que nadie previo", "No se pudo subir. Vuelva a intentarlo."),
])
def test_una_carga_fallida_dice_su_motivo(app, tmp_path, detalle, visible):
    t, _, _, _ = caso(tmp_path, 5)
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [t]
    ventana._estados = [estado_local(t)]
    ventana._al_subir({"trabajos": [t], "cliente": object(),
                       "fallos": [(t.manifiesto.nombre_batch, detalle)]})
    lineas = [ventana.bitacora.item(i).text() for i in range(ventana.bitacora.count())]
    assert any(visible in linea for linea in lineas)
