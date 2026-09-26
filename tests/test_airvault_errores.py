"""Lo que pasa cuando AirVault falla a mitad de un batch.

Un batch son cientos de peticiones y una subida completa casi dos mil: a esa
escala un corte de red o una página que no carga dejan de ser raros. Lo que
no puede pasar es que un tropiezo tire el trabajo entero, ni que una caída
marque como fallidas cuatrocientas páginas que nadie llegó a intentar.
"""

from __future__ import annotations

import pytest
import requests

from app.airvault.config import AirVaultConfig
from app.airvault.indexer import Indexador
from app.airvault.model import EstadoRegistro, Manifiesto, Registro
from app.airvault.session import (
    ErrorDeAirVault,
    ErrorDeConexion,
    ErrorDeSesion,
    SesionAirVault,
)
from tests.airvault_fake import ClienteFalso, pagina

PICKLIST = ["HP-1848CMP"]


class HttpFalso:
    """Sustituye al transporte de requests sin abrir ninguna conexion."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.pedidas = []
        self.cabeceras: list = []
        self.portadas: list = []
        self.headers: dict = {}
        self.cookies = _Tarro()

    def request(self, metodo, url, **extra):
        self.pedidas.append((metodo, url))
        self.cabeceras.append(dict(extra.get("headers") or {}))
        siguiente = self.respuestas.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente

    def get(self, url, **_extra):
        """La portada de la que el sitio sirve su token antiforgery.

        No sale del guion a proposito: no es una peticion del trabajo sino
        lo que AirVault exige para aceptar cualquier escritura, y meterla
        en el guion obligaria a repetirla en todas las pruebas que escriben.
        """
        self.portadas.append(url)
        return RespuestaFalsa(
            text='<div id="ct-antiforgery" data-root-antiforgery="tok-123">'
        )


class _Tarro:
    def __init__(self):
        self.vaciado = 0

    def set(self, *_a, **_k):
        return None

    def clear(self, *_a, **_k):
        self.vaciado += 1


class RespuestaFalsa:
    def __init__(self, status_code=200, json_data=None, text="{}"):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.url = "https://airvault.criticaltech.com/index/"

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def sesion(respuestas, reintentos=3):
    config = AirVaultConfig(reintentos=reintentos, espera_reintento_s=0.0)
    s = SesionAirVault(config, sesion=HttpFalso(respuestas))
    s.usar_cookie("FedAuth=x")
    s.dormir = lambda _s: None
    return s


# ── reintentos ─────────────────────────────────────────────────────

def test_una_conexion_cortada_se_reintenta():
    s = sesion([
        requests.ConnectionError("se cayo la red"),
        RespuestaFalsa(json_data={"records": 3}),
    ])
    assert s.get("/index/Batch/GetBatches") == {"records": 3}
    assert len(s.http.pedidas) == 2


def test_un_tiempo_agotado_se_reintenta():
    s = sesion([
        requests.Timeout("no contesto"),
        RespuestaFalsa(json_data={"ok": True}),
    ])
    assert s.get("/x") == {"ok": True}


def test_un_certificado_invalido_no_se_reintenta_y_se_explica():
    s = sesion([
        requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"),
        RespuestaFalsa(json_data={"no": "deberia llegar"}),
    ])

    with pytest.raises(ErrorDeConexion) as fallo:
        s.get("/x")

    motivo = str(fallo.value)
    assert "certificado SSL" in motivo
    assert "certificados confiables de Windows" in motivo
    assert "fecha y hora" in motivo
    assert "REQUESTS_CA_BUNDLE" in motivo
    assert len(s.http.pedidas) == 1


def test_el_servidor_ocupado_se_reintenta():
    """503 es «vuelve luego», no «esto esta mal»."""
    s = sesion([
        RespuestaFalsa(status_code=503),
        RespuestaFalsa(json_data={"ok": True}),
    ])
    assert s.get("/x") == {"ok": True}
    assert len(s.http.pedidas) == 2


def test_lo_que_no_mejora_insistiendo_no_se_reintenta():
    """Un 404 se responde igual las tres veces; insistir solo hace esperar."""
    s = sesion([RespuestaFalsa(status_code=404)])
    with pytest.raises(ErrorDeAirVault) as fallo:
        s.get("/x")
    assert len(s.http.pedidas) == 1
    # El motivo tiene que decir que fallo y por que, no un codigo suelto.
    assert "no existe" in str(fallo.value)
    assert "404" in str(fallo.value)


_ERROR_CON_MOTIVO = (
    "<!DOCTYPE html> <html> <head> <title>Client Error</title> </head> "
    "<body> <div> <h2>Info</h2> <form id=\"ErrorDetails\"> <p> Message: "
    "Field Aircraft value is required </p> </form> </div> </body> </html>"
)


def test_un_500_que_dice_por_que_no_se_reintenta():
    """Guardar una pagina incompleta contesta 500 con el motivo dentro.

    Se leia como «la red o AirVault ocupado»: tres intentos identicos y
    despues un ErrorDeConexion que cortaba el batch entero.
    """
    s = sesion([RespuestaFalsa(status_code=500, text=_ERROR_CON_MOTIVO)])

    with pytest.raises(ErrorDeAirVault) as fallo:
        s.post("/index/FormsProcessing/SaveAndGetIndexFields")

    assert len(s.http.pedidas) == 1
    assert "Field Aircraft value is required" in str(fallo.value)


def test_un_500_sin_motivo_sigue_siendo_un_servidor_ocupado():
    """La pagina de error generica no explica nada; ahi si vale insistir."""
    s = sesion([
        RespuestaFalsa(status_code=500, text="<html>Client Error</html>"),
        RespuestaFalsa(json_data={"ok": True}),
    ])

    assert s.post("/x").json() == {"ok": True}
    assert len(s.http.pedidas) == 2


def test_un_rechazo_de_una_pagina_no_es_un_fallo_del_camino():
    """Un 404 frena esa pagina; el batch entero sigue.

    Si se anunciara como error de conexion, el indexador daria por caido el
    camino y marcaria como fallidas cientos de paginas que nadie intento.
    """
    from app.airvault.indexer import FALLOS_DE_CAMINO

    assert not issubclass(ErrorDeAirVault, FALLOS_DE_CAMINO)


def test_agotados_los_intentos_se_dice_que_paso():
    s = sesion([requests.ConnectionError("cortada")] * 3)
    with pytest.raises(ErrorDeConexion) as fallo:
        s.get("/index/Batch/GetBatches")
    assert "3 intentos" in str(fallo.value)
    assert "no se pudo conectar" in str(fallo.value)


def test_el_tiempo_agotado_menciona_el_lote_abierto():
    """Es la causa habitual: AirVault deja la peticion colgada sin contestar."""
    s = sesion([requests.Timeout("nada")] * 3)
    with pytest.raises(ErrorDeConexion) as fallo:
        s.get("/index/Batch/LockAndGetBatchInfo")
    motivo = str(fallo.value)
    assert "un solo dueno por batch" in motivo
    # Tambien cuando lo dejo tomado el propio programa, no solo el navegador.
    assert "intento anterior no llego a desbloquearlo" in motivo


def test_la_espera_crece_con_cada_intento():
    """Reintentar al instante contra un servidor ahogado solo lo empeora."""
    config = AirVaultConfig(reintentos=3, espera_reintento_s=2.0)
    s = SesionAirVault(config, sesion=HttpFalso(
        [requests.ConnectionError("x")] * 3
    ))
    s.usar_cookie("FedAuth=x")
    esperas = []
    s.dormir = esperas.append
    with pytest.raises(ErrorDeConexion):
        s.get("/x")
    assert esperas == [2.0, 4.0]


def test_la_sesion_caducada_no_se_reintenta():
    """Insistir con una cookie muerta no la revive; hay que decirlo."""
    s = sesion([RespuestaFalsa(text="dosignin")])
    with pytest.raises(ErrorDeSesion):
        s.get("/x")
    assert len(s.http.pedidas) == 1


def test_la_subida_reintenta_cada_trozo():
    """Sin esto, un trozo perdido obliga a repetir casi dos gigas."""
    s = sesion([
        requests.ConnectionError("cortada"),
        RespuestaFalsa(),
    ])
    assert s.post("/quickuploadex/Home/Upload/", data={}).status_code == 200
    assert [m for m, _u in s.http.pedidas] == ["POST", "POST"]


# ── paginas que no cargan ──────────────────────────────────────────

def manifiesto(paginas=3):
    return Manifiesto(
        job_id="x", nombre_batch="DP | BIT", batch_id="003SRO",
        registros=[
            Registro(seq=n, matricula="HP-1848CMP",
                     log_number=f"231223{n}", fecha="2026/08/12", fleet="NG")
            for n in range(1, paginas + 1)
        ],
    )


class ClienteQueFalla(ClienteFalso):
    """Cliente que se atraganta con paginas concretas."""

    def __init__(self, ilegibles, error=None, **extra):
        super().__init__(**extra)
        self.ilegibles = set(ilegibles)
        self.error = error or RuntimeError("la pagina no cargo")

    def leer_pagina(self, batch_id, pagina):
        if pagina in self.ilegibles:
            self.lecturas.append(pagina)
            raise self.error
        return super().leer_pagina(batch_id, pagina)


def test_una_pagina_que_no_carga_no_detiene_el_lote():
    cliente = ClienteQueFalla(
        [2], paginas={n: pagina(n) for n in (1, 3)}, picklist=PICKLIST,
        page_count=3,
    )
    plan = Indexador(cliente, manifiesto(), PICKLIST).planificar(3)

    assert len(plan.escribibles) == 2
    assert [p.seq for p in plan.bloqueadas] == [2]


def test_la_pagina_que_no_carga_dice_por_que():
    cliente = ClienteQueFalla(
        [2], paginas={n: pagina(n) for n in (1, 3)}, picklist=PICKLIST,
        page_count=3,
    )
    plan = Indexador(cliente, manifiesto(), PICKLIST).planificar(3)
    aviso = plan.bloqueadas[0].avisos[0]
    assert aviso.codigo == "no_cargo"
    assert "no cargo" in aviso.detalle


def test_en_la_pagina_que_no_cargo_no_se_escribe():
    """Sin leerla no se puede comprobar que hablan de la misma bitacora."""
    cliente = ClienteQueFalla(
        [2], paginas={n: pagina(n) for n in (1, 3)}, picklist=PICKLIST,
        page_count=3,
    )
    indexador = Indexador(cliente, manifiesto(), PICKLIST)
    indexador.aplicar(indexador.planificar(3))
    assert [p for p, _v, _e in cliente.escrituras] == [1, 3]


def test_la_sesion_caida_al_leer_corta_la_planificacion():
    """Leer las demas no va a ir mejor, y el mensaje que importa es ese."""
    cliente = ClienteQueFalla(
        [2], error=ErrorDeSesion("La sesion de AirVault caduco."),
        paginas={n: pagina(n) for n in (1, 3)}, picklist=PICKLIST,
        page_count=3,
    )
    with pytest.raises(ErrorDeSesion):
        Indexador(cliente, manifiesto(), PICKLIST).planificar(3)


# ── la sesion se cae escribiendo ───────────────────────────────────

class ClienteQueSeCae(ClienteFalso):
    """Escribe hasta cierta pagina y despues pierde la sesion."""

    def __init__(self, cae_en, error=None, **extra):
        super().__init__(**extra)
        self.cae_en = cae_en
        self.error = error or ErrorDeSesion("La sesion de AirVault caduco.")

    def guardar_pagina(self, batch_id, pagina, valores, estado,
                       pagina_siguiente=None):
        if pagina >= self.cae_en:
            raise self.error
        return super().guardar_pagina(
            batch_id, pagina, valores, estado, pagina_siguiente
        )


def test_la_caida_no_marca_como_fallidas_las_que_nadie_intento():
    cliente = ClienteQueSeCae(
        2, paginas={n: pagina(n) for n in (1, 2, 3)}, picklist=PICKLIST,
        page_count=3,
    )
    manifiesto_ = manifiesto()
    indexador = Indexador(cliente, manifiesto_, PICKLIST)
    resultado = indexador.aplicar(indexador.planificar(3))

    assert resultado.escritas == 1
    assert resultado.fallidas == 0
    assert resultado.interrumpido
    assert "caduco" in resultado.interrumpido


def test_lo_que_quedo_sin_escribir_sigue_pendiente():
    """Es lo que permite retomar sin repetir ni saltarse nada."""
    cliente = ClienteQueSeCae(
        2, paginas={n: pagina(n) for n in (1, 2, 3)}, picklist=PICKLIST,
        page_count=3,
    )
    manifiesto_ = manifiesto()
    indexador = Indexador(cliente, manifiesto_, PICKLIST)
    indexador.aplicar(indexador.planificar(3))

    estados = [r.estado for r in manifiesto_.registros]
    assert estados == [
        EstadoRegistro.ESCRITA,
        EstadoRegistro.PENDIENTE,
        EstadoRegistro.PENDIENTE,
    ]


def test_un_fallo_de_una_pagina_si_se_marca_en_esa_pagina():
    """Distinto de una caida: aqui el problema es de la bitacora, no del camino."""
    cliente = ClienteFalso(
        paginas={n: pagina(n) for n in (1, 2, 3)}, picklist=PICKLIST,
        page_count=3, fallar_en={2},
    )
    manifiesto_ = manifiesto()
    indexador = Indexador(cliente, manifiesto_, PICKLIST)
    resultado = indexador.aplicar(
        indexador.planificar(3), detener_en_error=False
    )

    assert resultado.escritas == 2
    assert resultado.fallidas == 1
    assert not resultado.interrumpido
    assert manifiesto_.registros[1].estado is EstadoRegistro.ERROR


def test_una_pagina_rechazada_no_deja_sin_indexar_a_las_demas():
    """El motivo es de esa bitacora, no del batch.

    Cuando el rechazo viajaba disfrazado de corte de red, la primera pagina
    que AirVault no aceptaba se llevaba por delante todo lo que venia
    detras: el batch se quedaba en amarillo y sin una sola pagina escrita.
    """
    cliente = ClienteFalso(
        paginas={n: pagina(n) for n in (1, 2, 3)}, picklist=PICKLIST,
        page_count=3, rechazar_en={2},
    )
    manifiesto_ = manifiesto()
    indexador = Indexador(cliente, manifiesto_, PICKLIST)

    resultado = indexador.aplicar(indexador.planificar(3))

    assert resultado.escritas == 2
    assert resultado.fallidas == 1
    assert not resultado.interrumpido
    assert manifiesto_.registros[1].estado is EstadoRegistro.ERROR


def test_una_pagina_que_airvault_rechaza_una_vez_se_repite():
    """Los datos del batch automatico ya estan validados: el fallo es de AirVault.

    Antes la pagina quedaba marcada como fallida al primer rechazo y el
    batch terminaba en amarillo hasta la siguiente vuelta.
    """
    class RechazaUnaVez(ClienteFalso):
        rechazos = 0

        def guardar_pagina(self, batch_id, pagina, *args):
            if pagina == 2 and not self.rechazos:
                self.rechazos += 1
                raise ErrorDeAirVault("AirVault rechazo la pagina 2: 500")
            return super().guardar_pagina(batch_id, pagina, *args)

    cliente = RechazaUnaVez(
        paginas={n: pagina(n) for n in (1, 2, 3)}, picklist=PICKLIST,
        page_count=3,
    )
    manifiesto_ = manifiesto()
    pausas: list[float] = []
    indexador = Indexador(cliente, manifiesto_, PICKLIST, dormir=pausas.append)

    resultado = indexador.aplicar(indexador.planificar(3))

    assert (resultado.escritas, resultado.fallidas) == (3, 0)
    assert [p for p, _v, _e in cliente.escrituras] == [1, 2, 3]
    assert len(pausas) == 1
    assert all(r.estado is EstadoRegistro.ESCRITA for r in manifiesto_.registros)


# ── verificacion ───────────────────────────────────────────────────

def test_verificar_cuenta_aparte_la_pagina_que_no_pudo_leerse():
    from app.airvault.config import CAMPO_DOC_TYPE, CAMPO_FLEET, CAMPO_AUDIT_STATUS
    from app.airvault.indexer import verificar_lote
    from app.airvault.config import CAMPO_LOG_NUMBER, CAMPO_MATRICULA, CAMPO_END_DATE
    from app.airvault.mapping import fecha_airvault

    manifiesto_ = manifiesto()
    cliente = ClienteQueFalla(
        [2], paginas={
            numero: pagina(numero, estado=0, valores={
                CAMPO_DOC_TYPE: "Log Page", CAMPO_FLEET: "NG", CAMPO_AUDIT_STATUS: "PUBLISHED",
                CAMPO_END_DATE: fecha_airvault(manifiesto_.registros[numero - 1].fecha),
                CAMPO_LOG_NUMBER: manifiesto_.registros[numero - 1].log_number,
                CAMPO_MATRICULA: manifiesto_.registros[numero - 1].matricula,
            })
            for numero in (1, 3)
        },
        page_count=3,
    )
    validas, total, problemas = verificar_lote(cliente, manifiesto_)
    assert (validas, total) == (2, 3)
    assert any("no se pudo leer" in p for p in problemas)


# ── la sesion guardada que ya no vale ──────────────────────────────

def test_una_sesion_caducada_se_renueva_sin_ventana_y_sigue(monkeypatch):
    """La sesion se cae a mitad del trabajo y se rehace ahi mismo.

    El perfil de Edge sabe volver a entrar solo (pasa otra vez por el
    enlace federado y Microsoft lo reconoce sin preguntar nada), asi que
    una caducidad no tiene por que tumbar un batch de cuatrocientas
    paginas: se rehace la sesion y se repite la peticion.
    """
    from app.airvault import navegador
    from app.airvault.session import ORIGEN_EDGE

    s = sesion([
        RespuestaFalsa(status_code=401),
        RespuestaFalsa(json_data={"records": 7}),
    ])
    s._origen = ORIGEN_EDGE
    pedidos: list = []

    def entrar(*_a, forzar_login=False, **_k):
        pedidos.append(forzar_login)
        return {"FedAuth": "nueva"}

    monkeypatch.setattr(navegador, "obtener_cookies", entrar)
    assert s.comprobar() == 7
    # Sin ventana: se lee el perfil, que es lo que ya vale otra vez.
    assert pedidos == [False]


def test_no_se_reentra_una_y_otra_vez_en_la_misma_peticion(monkeypatch):
    """Se prueba el perfil y una ventana, no Edge en cada intento."""
    from app.airvault import navegador
    from app.airvault.session import ORIGEN_EDGE

    s = sesion([RespuestaFalsa(status_code=401)] * 3)
    s._origen = ORIGEN_EDGE
    pedidos: list = []

    def entrar(*_a, forzar_login=False, **_k):
        pedidos.append(forzar_login)
        return {"FedAuth": "nueva"}

    monkeypatch.setattr(navegador, "obtener_cookies", entrar)
    with pytest.raises(ErrorDeSesion):
        s.get("/x")
    assert pedidos == [False, True]


def test_cuando_ni_renovando_vale_se_vuelve_a_entrar_con_ventana(monkeypatch):
    """Ultimo recurso: pedir el acceso en la ventana del navegador."""
    from app.airvault import navegador
    from app.airvault.session import ORIGEN_EDGE, comprobar_o_renovar

    s = sesion([
        RespuestaFalsa(status_code=401),
        RespuestaFalsa(status_code=401),
        RespuestaFalsa(json_data={"records": 3}),
    ])
    s._origen = ORIGEN_EDGE
    pedidos: list = []

    def entrar(*_a, forzar_login=False, **_k):
        pedidos.append(forzar_login)
        return {"FedAuth": "nueva"}

    monkeypatch.setattr(navegador, "obtener_cookies", entrar)
    assert comprobar_o_renovar(s) == 3
    # Primero en silencio; como el servidor siguio diciendo que no, con
    # ventana y sin mirar lo que guarda el perfil.
    assert pedidos == [False, True]
    # Y la vieja sale del tarro: requests mandaria las dos y AirVault se
    # quedaria con la que acaba de rechazar.
    assert s.http.cookies.vaciado == 1


def test_reentrar_repite_la_peticion_y_continua_el_trabajo(monkeypatch):
    """La ventana de acceso no deja el paso actual detenido.

    La renovacion de sesion no es un reintento de red. Incluso si la
    configuracion permite una sola peticion, al terminar de entrar en Edge
    se repite la que quedo pendiente y el proceso sigue solo.
    """
    from app.airvault import navegador
    from app.airvault.session import ORIGEN_EDGE

    s = sesion([
        RespuestaFalsa(status_code=401),
        RespuestaFalsa(status_code=401),
        RespuestaFalsa(json_data={"records": 8}),
    ], reintentos=1)
    s._origen = ORIGEN_EDGE
    pedidos: list[bool] = []

    def entrar(*_a, forzar_login=False, **_k):
        pedidos.append(forzar_login)
        return {"FedAuth": "nueva"}

    monkeypatch.setattr(navegador, "obtener_cookies", entrar)

    assert s.get("/index/Batch/GetBatches") == {"records": 8}
    assert pedidos == [False, True]
    assert len(s.http.pedidas) == 3


def test_lo_que_escribe_lleva_el_token_del_sitio():
    """Sin la cabecera ``AntiForgery`` el servidor contesta 500 y ya esta.

    No da 403 ni dice que falta nada: devuelve su pagina de error generica,
    que ademas se reintenta por transitoria. Asi murio la subida durante
    dias, siempre en ``FinishUpload`` y despues de mandar el archivo entero.
    """
    s = sesion([RespuestaFalsa(json_data={"ok": True})])
    s.post("/quickuploadex/Home/FinishUpload", json={"model": {}})
    assert s.http.cabeceras[-1].get("AntiForgery") == "tok-123"
    # Y se lee de la portada de esa aplicacion, no de otra.
    assert s.http.portadas[-1].endswith("/quickuploadex/")


def test_leer_no_pide_token_ni_gasta_una_peticion_de_mas():
    """Son cientos de lecturas por batch; una portada por cada una sobra."""
    s = sesion([RespuestaFalsa(json_data={"rows": []})])
    s.get("/index/Batch/GetBatches")
    assert s.http.portadas == []
    assert "AntiForgery" not in s.http.cabeceras[-1]


def test_un_500_al_escribir_hace_releer_el_token():
    """Un token caducado se ve como un 500; releerlo es lo que lo arregla."""
    s = sesion([
        RespuestaFalsa(status_code=500, text="error"),
        RespuestaFalsa(json_data={"ok": True}),
    ])
    s.post("/index/Batch/UpdateBatchName", data={})
    # Dos portadas: la del primer intento y la que se relee al fallar.
    assert len(s.http.portadas) == 2


def test_un_440_es_que_caduco_la_sesion_y_no_un_rechazo_del_sitio(monkeypatch):
    """IIS contesta 440 («Login Timeout») con su pagina de error generica.

    Sin nombrarlo se leia como un rechazo de AirVault y el trabajo moria en
    medio de una espera larga en vez de volver a entrar.
    """
    from app.airvault import navegador
    from app.airvault.session import ORIGEN_EDGE

    s = sesion([
        RespuestaFalsa(status_code=440, text="<html>error</html>"),
        RespuestaFalsa(json_data={"records": 1}),
    ])
    s._origen = ORIGEN_EDGE
    monkeypatch.setattr(navegador, "obtener_cookies",
                        lambda *_a, **_k: {"FedAuth": "nueva"})
    assert s.comprobar() == 1


def test_una_cookie_pegada_a_mano_no_se_renueva_sola(monkeypatch):
    """No hay navegador del que sacarla; hay que decirlo y no insistir."""
    from app.airvault.session import comprobar_o_renovar

    s = sesion([RespuestaFalsa(status_code=401)])
    with pytest.raises(ErrorDeSesion) as fallo:
        comprobar_o_renovar(s)
    assert "F12" in str(fallo.value) or "herramientas de" in str(fallo.value)


def test_la_sesion_caducada_del_navegador_no_manda_a_copiar_cookies(monkeypatch):
    """Mandar a copiar con F12 a quien entro por el navegador es el camino largo."""
    from app.airvault import navegador
    from app.airvault.navegador import ErrorDeNavegador
    from app.airvault.session import ORIGEN_EDGE

    def no_hay_navegador(*_a, **_k):
        raise ErrorDeNavegador("no arranco")

    monkeypatch.setattr(navegador, "obtener_cookies", no_hay_navegador)
    s = sesion([RespuestaFalsa(status_code=401)])
    s._origen = ORIGEN_EDGE
    with pytest.raises(ErrorDeSesion) as fallo:
        s.get("/x")
    motivo = str(fallo.value)
    assert "perfil de Edge" in motivo
    assert "F12" not in motivo
