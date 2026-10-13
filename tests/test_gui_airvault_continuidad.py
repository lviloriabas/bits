"""El proceso sigue hasta terminar todos los batches y dice en que esta.

Un batch que falla, un cierre que AirVault no acepto o unas horas sin
novedades no pueden dejar a los demas batches de la cola sin procesar, ni a
la ventana callada con trabajo pendiente.
"""

from types import SimpleNamespace

from app.airvault.flujo import (BUSCANDO, COMPLETADO, INCOMPLETO, INDEXADO,
                                LISTO, POSIBLE_DUPLICADO, SIN_SUBIR,
                                EstadoParte)
from app.airvault.model import EstadoEtapa
from app.airvault.session import ErrorDeAirVault, ErrorDeConexion
from app.gui import airvault_window as modulo
from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker
from tests.test_airvault_rework import trabajo


def cola(tmp_path, estados):
    ventana = AirVaultWindow(tmp_path)
    trabajos = [trabajo(tmp_path, nombre) for nombre, _estado in estados]
    ventana._trabajos = trabajos
    ventana._estados = [
        EstadoParte(t, estado) for t, (_n, estado) in zip(trabajos, estados)
    ]
    ventana._estado["trabajos"] = list(trabajos)
    ventana._numeros.clear()
    ventana._pintar_lotes()
    return ventana, trabajos


def anotado(ventana) -> str:
    return "\n".join(
        ventana.bitacora.item(i).text() for i in range(ventana.bitacora.count())
    )


# ── el hilo: un batch que falla no deja sin procesar a los demas ──────


def _parte_indexada(trabajos):
    from app.airvault.indexer import Resultado

    return {
        "resultado": Resultado(escritas=2), "validas": 2, "total": 2,
        "lotes": 1, "cierres": [], "incompleto": False,
        "incluye_revision": False, "carpetas": [str(trabajos[0].carpeta)],
        "amarillas": {str(trabajos[0].carpeta): 0},
    }


def _tres_listos(tmp_path):
    trabajos = [
        SimpleNamespace(
            carpeta=tmp_path / nombre,
            manifiesto=SimpleNamespace(nombre_batch=nombre, batch_id=f"003{nombre.upper()}"),
        )
        for nombre in ("uno", "dos", "tres")
    ]
    plan = (None, SimpleNamespace(resolutor=None))
    estado = {
        "tanda_hecha": True, "cliente": object(), "completar": True,
        "listos": list(trabajos),
        "planes": {str(t.carpeta): plan for t in trabajos},
    }
    return trabajos, estado


def test_un_batch_que_no_se_deja_indexar_no_corta_a_los_siguientes(tmp_path, monkeypatch):
    """AirVault rechazo abrir el primero: los otros dos se indexan igual."""
    (uno, dos, tres), estado = _tres_listos(tmp_path)
    worker = TrabajoAirVaultWorker("indexar", estado)
    intentados: list = []

    def ejecutar(trabajos, _planes, _cliente, completar=False):
        intentados.append(trabajos[0])
        if trabajos[0] is uno:
            raise ErrorDeAirVault(
                "AirVault rechazo /index/Batch/LockAndGetBatchInfo: "
                "Sql command has timed out.."
            )
        return _parte_indexada(trabajos)

    monkeypatch.setattr(worker, "_ejecutar_indexado", ejecutar)
    emitidos: list = []
    fallos: list = []
    worker.indexado.connect(emitidos.append)
    worker.fallo.connect(fallos.append)

    worker.run()

    assert intentados == [uno, dos, tres]
    assert fallos == []
    datos, = emitidos
    assert datos["carpetas"] == [str(dos.carpeta), str(tres.carpeta)]
    assert [(clave, nombre) for clave, nombre, _detalle in datos["fallos"]] == [
        (str(uno.carpeta), "uno"),
    ]
    assert "timed out" in datos["fallos"][0][2]


def test_sin_sesion_el_indexado_no_insiste_con_los_demas(tmp_path, monkeypatch):
    """Sin red los siguientes fallarian igual: se corta como antes."""
    (uno, dos, tres), estado = _tres_listos(tmp_path)
    worker = TrabajoAirVaultWorker("indexar", estado)
    intentados: list = []

    def ejecutar(trabajos, _planes, _cliente, completar=False):
        intentados.append(trabajos[0])
        raise ErrorDeConexion("sin red")

    monkeypatch.setattr(worker, "_ejecutar_indexado", ejecutar)
    fallos: list = []
    worker.fallo.connect(fallos.append)

    worker.run()

    assert intentados == [uno]
    assert len(fallos) == 1


def test_un_plan_que_falla_no_tumba_la_revision_de_los_demas(tmp_path, monkeypatch):
    """Leer un batch para planificarlo fallo: la revision entrega lo demas."""
    from app.airvault import flujo

    leidos: list = []

    def planificar(nombre):
        def plan(_cliente, _resolutor, avisar=None):
            leidos.append(nombre)
            if nombre == "uno":
                raise ErrorDeAirVault("Sql command has timed out..")
            return (f"plan-{nombre}", SimpleNamespace(resolutor=None))
        return plan

    trabajos = [
        SimpleNamespace(
            carpeta=tmp_path / nombre, planificar=planificar(nombre),
            manifiesto=SimpleNamespace(
                nombre_batch=nombre, batch_id=f"003{nombre.upper()}",
                solo_subir=False, mezcla_pendiente=False, resubir_por_mezcla=False,
            ),
        )
        for nombre in ("uno", "dos")
    ]
    estados = [EstadoParte(t, LISTO) for t in trabajos]
    monkeypatch.setattr(flujo, "comprobar_partes", lambda *_a, **_k: list(estados))
    monkeypatch.setattr(flujo, "detectar_indexados", lambda partes, *_a, **_k: list(partes))
    monkeypatch.setattr(flujo, "comprobar_memoria_de_libros", lambda *_a, **_k: None)
    estado = {"raiz": tmp_path, "trabajos": list(trabajos)}
    worker = TrabajoAirVaultWorker("comprobar", estado)
    monkeypatch.setattr(worker, "_conectar", lambda: object())
    emitidos: list = []
    fallos: list = []
    worker.comprobado.connect(emitidos.append)
    worker.fallo.connect(fallos.append)

    worker.run()

    assert leidos == ["uno", "dos"]
    assert fallos == []
    datos, = emitidos
    assert datos["estados"] == estados
    assert list(datos["planes"]) == [str(trabajos[1].carpeta)]
    assert [(clave, nombre) for clave, nombre, _d in datos["fallos_de_plan"]] == [
        (str(trabajos[0].carpeta), "uno"),
    ]


# ── la ventana: que no se reintente en bucle ni se calle el fallo ─────


def test_el_batch_que_fallo_espera_a_la_revision_siguiente(app, tmp_path, monkeypatch):
    ventana, (malo, bueno) = cola(tmp_path, [("malo", LISTO), ("bueno", LISTO)])
    reloj = [5000.0]
    monkeypatch.setattr(modulo.time, "monotonic", lambda: reloj[0])
    plan = (None, SimpleNamespace(resolutor=None))
    ventana._estado["planes"] = {str(malo.carpeta): plan, str(bueno.carpeta): plan}
    from app.airvault.indexer import Resultado

    # «bueno» no entraba en esta tanda: sigue listo y con su plan.
    ventana._al_indexar({
        "resultado": Resultado(), "validas": 0, "total": 0,
        "lotes": 1, "cierres": [], "incompleto": False,
        "carpetas": [], "amarillas": {},
        "fallos": [(str(malo.carpeta), "malo", "Sql command has timed out..")],
    })

    texto = anotado(ventana)
    assert "«malo»: AirVault no dejó indexarlo. Se sigue con los demás batches" in texto
    assert "«malo»" in ventana.resumen.text()
    # En la misma cadena no se vuelve a intentar: seria un bucle sin espera.
    assert ventana._listos_automaticos() == [bueno]
    assert not ventana._fin_pendiente
    # Pasado el intervalo de la revision, vuelve a entrar solo.
    reloj[0] += ventana.minutos_spin.value() * 60 + 1
    assert ventana._listos_automaticos() == [malo, bueno]
    # Y la orden dada a mano no espera a nada.
    reloj[0] -= ventana.minutos_spin.value() * 60
    assert ventana._listos() == [malo, bueno]


def test_un_plan_fallido_se_anota_con_su_batch(app, tmp_path):
    ventana, (malo, bueno) = cola(tmp_path, [("malo", LISTO), ("bueno", LISTO)])
    ventana._al_comprobar({
        "estados": list(ventana._estados), "planes": {}, "partes": [],
        "recuperados": [], "cliente": object(), "acotado": False,
        "fallos_de_plan": [(str(malo.carpeta), "malo", "Sql command has timed out..")],
    })
    texto = anotado(ventana)
    assert "«malo»" in texto
    assert "se vuelve a leer en la próxima revisión" in texto
    ventana._parar_vigilancia()


# ── el alcance: el cierre omitido sigue siendo trabajo pendiente ──────


def test_el_cierre_que_airvault_no_acepto_sigue_dentro_del_proceso(app, tmp_path, monkeypatch):
    """Un batch con el cierre omitido sigue en la cola de Web Index."""
    ventana, (omitido, cerrado, nuevo) = cola(tmp_path, [
        ("omitido", INDEXADO), ("cerrado", COMPLETADO), ("nuevo", SIN_SUBIR),
    ])
    omitido.manifiesto.etapa("completar").marcar(
        EstadoEtapa.OMITIDA, "1 de 10 paginas no estan en verde",
    )
    cerrado.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
    ventana.completar_check.setChecked(True)
    monkeypatch.setattr(TrabajoAirVaultWorker, "start", lambda self: None)

    ventana._lanzar("comprobar", ventana._estado)

    assert ventana._alcance_proceso == {str(omitido.carpeta), str(nuevo.carpeta)}
    # Con el nuevo ya completado, el omitido sigue contando: ni se anuncia el
    # final ni queda fuera de lo que la cadena completa sola.
    ventana._estados[2] = EstadoParte(nuevo, COMPLETADO)
    assert ventana._firma_de_fin() is None
    assert ventana._por_completar(automatico=True) == [omitido]
    assert ventana._falta_esperar()


# ── la revision periodica no se rinde ─────────────────────────────────


def test_la_revision_automatica_se_espacia_pero_no_se_detiene(app, tmp_path, monkeypatch):
    ventana, _trabajos = cola(tmp_path, [("subido", BUSCANDO)])
    reloj = [1000.0]
    monkeypatch.setattr(modulo.time, "monotonic", lambda: reloj[0])
    ventana.auto_check.setChecked(True)
    ventana._ajustar_vigilancia()
    normal = ventana._vigilante.interval()
    assert normal == ventana.minutos_spin.value() * 60_000
    reloj[0] += modulo.HORAS_SIN_CAMBIOS_ANTES_DE_ESPACIAR * 3600 + 60
    ventana._ajustar_vigilancia()
    assert ventana._vigilando()
    assert ventana._vigilante.interval() == modulo.MINUTOS_SIN_CAMBIOS * 60_000
    assert "sigue" in ventana.resumen.text()
    assert "detenida" not in anotado(ventana)
    avisos = anotado(ventana).count("sin ningún cambio")
    # No lo repite en cada vuelta.
    ventana._ajustar_vigilancia()
    assert anotado(ventana).count("sin ningún cambio") == avisos == 1
    ventana._pintar_linea_viva()
    assert "sin cambios" in ventana._linea_viva.text().lower()
    # Un cambio en la cola devuelve el ritmo normal.
    ventana._estados = [EstadoParte(ventana._estados[0].trabajo, BUSCANDO, "otra vuelta")]
    ventana._estados[0].trabajo.manifiesto.batch_id = "003AAA"
    ventana._ajustar_vigilancia()
    assert ventana._vigilante.interval() == normal
    ventana._parar_vigilancia()


def test_una_orden_sobre_filas_no_apaga_la_revision_en_marcha(app, tmp_path, monkeypatch):
    """Revisar una fila a mano no deja parado el proceso que esperaba."""
    ventana, (subido, otro) = cola(tmp_path, [("subido", BUSCANDO), ("otro", BUSCANDO)])
    ventana.auto_check.setChecked(True)
    ventana._ajustar_vigilancia()
    assert ventana._vigilando()
    monkeypatch.setattr(TrabajoAirVaultWorker, "start", lambda self: None)
    ventana._estado["comprobar_trabajos"] = [subido]
    ventana._lanzar("comprobar", ventana._estado)

    ventana._al_comprobar({
        "estados": [ventana._estados[0]], "planes": {}, "partes": [],
        "recuperados": [], "cliente": object(), "acotado": True,
    })

    assert ventana._vigilando()
    ventana._parar_vigilancia()


def test_una_orden_sobre_filas_no_arranca_una_revision_que_no_habia(app, tmp_path, monkeypatch):
    ventana, (subido,) = cola(tmp_path, [("subido", BUSCANDO)])
    ventana._parar_vigilancia()
    monkeypatch.setattr(TrabajoAirVaultWorker, "start", lambda self: None)
    ventana._lanzar("comprobar", ventana._estado)

    ventana._al_comprobar({
        "estados": list(ventana._estados), "planes": {}, "partes": [],
        "recuperados": [], "cliente": object(), "acotado": True,
    })

    assert not ventana._vigilando()


# ── al soltar el hilo se dice en que queda ────────────────────────────


def test_si_el_proceso_se_detiene_con_pendientes_dice_cuales_y_por_que(app, tmp_path):
    ventana, (duda, hecho) = cola(tmp_path, [
        ("duda", POSIBLE_DUPLICADO), ("hecho", COMPLETADO),
    ])
    hecho.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
    ventana.completar_check.setChecked(True)
    ventana._proceso_iniciado = True

    ventana._al_terminar()

    assert not ventana._vigilando()
    texto = anotado(ventana)
    assert "El proceso se detuvo con 1 batch sin terminar" in texto
    assert "«duda»" in texto and "decidir si es un duplicado" in texto
    assert "se detuvo con 1 batch sin terminar" in ventana.resumen.text()
    # No lo repite si nada cambio.
    ventana._al_terminar()
    assert anotado(ventana).count("El proceso se detuvo con") == 1


def test_con_la_revision_apagada_dice_que_no_sigue_solo(app, tmp_path):
    ventana, _trabajos = cola(tmp_path, [("subido", BUSCANDO)])
    ventana.auto_check.setChecked(False)
    ventana._proceso_iniciado = True

    ventana._al_terminar()

    assert not ventana._vigilando()
    texto = anotado(ventana)
    assert "El proceso se detuvo con 1 batch sin terminar" in texto
    assert "«Revisar cada»" in texto


def test_mientras_la_revision_sigue_no_se_anuncia_ninguna_detencion(app, tmp_path):
    ventana, _trabajos = cola(tmp_path, [("subido", BUSCANDO)])
    ventana.auto_check.setChecked(True)
    ventana._proceso_iniciado = True

    ventana._al_terminar()

    assert ventana._vigilando()
    assert "se detuvo" not in anotado(ventana)
    # Con el reloj en marcha el proceso sigue: se puede pausar, no empezar.
    assert ventana.boton_pausar.isEnabled()
    assert not ventana.boton_proceso.isEnabled()
    ventana._pintar_linea_viva()
    assert "próxima revisión automática en" in ventana._linea_viva.text()
    assert "1 subido" in ventana._linea_viva.text()
    ventana._parar_vigilancia()


def test_tras_un_fallo_la_espera_dice_que_es_un_reintento(app, tmp_path):
    ventana, _trabajos = cola(tmp_path, [("listo", INCOMPLETO)])
    ventana.auto_check.setChecked(True)
    ventana._proceso_iniciado = True

    ventana._al_fallar("AirVault rechazo /index/Batch/GetBatches: Sql command has timed out..")
    ventana._al_terminar()

    assert ventana._vigilando()
    ventana._pintar_linea_viva()
    assert ventana._linea_viva.text().startswith("El último intento falló: se reintenta en ")
    ventana._parar_vigilancia()
