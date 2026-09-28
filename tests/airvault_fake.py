"""Cliente falso de AirVault para probar el indexado sin tocar el servidor.

Guarda en memoria lo que le escriben, de modo que un test puede afirmar
exactamente que paginas se tocaron, con que valores y en que orden.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Mapping, Optional

from app.airvault.client import PaginaDelLote, PaginaIndexada, ResumenLote
from app.airvault.session import ErrorDeAirVault


class ClienteFalso:
    def __init__(
        self,
        paginas: Optional[Dict[int, PaginaIndexada]] = None,
        lotes: Optional[List[ResumenLote]] = None,
        picklist: Optional[List[str]] = None,
        page_count: int = 0,
        fallar_en: Optional[set[int]] = None,
        rechazar_en: Optional[set[int]] = None,
        mapa: Optional[List[PaginaDelLote]] = None,
        no_se_pueden_borrar: Optional[set[int]] = None,
        estados_tras_validar: Optional[Dict[int, int]] = None,
        paginas_por_lote: Optional[Dict[str, Dict[int, PaginaIndexada]]] = None,
    ):
        self.paginas = paginas or {}
        self.lotes = lotes or []
        self.picklist = picklist or []
        self.page_count = page_count or len(self.paginas)
        self.fallar_en = fallar_en or set()
        # Paginas que AirVault contesta explicando que no las acepta, como
        # hace con «Field Aircraft value is required».
        self.rechazar_en = rechazar_en or set()
        # Como ve AirVault el batch entero. Sin decir nada, cada pagina es
        # su propio documento y esta en verde: lo que hace falta para que
        # el batch se pueda dar por terminado.
        self.mapa = mapa
        # Paginas que AirVault no deja quitar: es lo que pasa sin el
        # permiso «Delete Batch Image».
        self.no_se_pueden_borrar = no_se_pueden_borrar or set()
        self.estados_tras_validar = estados_tras_validar or {}
        self.paginas_por_lote = {
            str(batch_id).strip().upper(): dict(paginas)
            for batch_id, paginas in (paginas_por_lote or {}).items()
        }
        self.borradas: List[int] = []
        self.validaciones_batch: List[tuple[str, List[int]]] = []
        self.escrituras: List[tuple[int, Dict[int, str], int]] = []
        self.filtros: List[str] = []
        self.lecturas: List[int] = []
        self.abiertos: List[str] = []
        self.cerrados: List[str] = []
        self.completados: List[str] = []
        self.renombrados: List[tuple[str, str]] = []

    # ── contrato que usa el indexador ──────────────────────────────

    def listar_lotes(self, filtro: str = "") -> List[ResumenLote]:
        self.filtros.append(filtro)
        if not filtro:
            return list(self.lotes)
        objetivo = filtro.strip().lower()
        return [l for l in self.lotes if objetivo in l.nombre.lower()]

    def abrir_lote(self, batch_id: str) -> Mapping[str, object]:
        self.abiertos.append(batch_id)
        return {"pageCount": self.page_count, "batchId": batch_id}

    def cerrar_lote(self, batch_id: str) -> Mapping[str, object]:
        self.cerrados.append(batch_id)
        return {"ok": True}

    def renombrar_lote(self, batch_id: str, nombre: str) -> bool:
        """Imita Rename y hace visible el título en la siguiente consulta."""
        encontrado = False
        nuevos = []
        for lote in self.lotes:
            if lote.batch_id.strip().upper() == str(batch_id).strip().upper():
                nuevos.append(replace(lote, nombre=str(nombre)))
                encontrado = True
            else:
                nuevos.append(lote)
        if encontrado:
            self.lotes = nuevos
            self.renombrados.append((str(batch_id), str(nombre)))
        return encontrado

    def leer_pagina(self, batch_id: str, pagina: int) -> PaginaIndexada:
        self.lecturas.append(pagina)
        propias = self.paginas_por_lote.get(
            str(batch_id).strip().upper(), self.paginas
        )
        return propias.get(
            pagina,
            PaginaIndexada(pagina=pagina, estado=3, valores={}, columnas={}),
        )

    def guardar_pagina(self, batch_id, pagina, valores, estado,
                       pagina_siguiente=None):
        if pagina in self.fallar_en:
            raise RuntimeError(f"fallo simulado en la pagina {pagina}")
        if pagina in self.rechazar_en:
            raise ErrorDeAirVault(
                f"AirVault rechazo la pagina {pagina}: "
                f"Field Aircraft value is required."
            )
        self.escrituras.append((pagina, dict(valores), estado))
        self.paginas[pagina] = PaginaIndexada(
            pagina=pagina, estado=estado, valores=dict(valores), columnas={}
        )
        return {"ok": True}

    def picklist_matriculas(self) -> List[str]:
        return list(self.picklist)

    def paginas_del_lote(self, batch_id: str) -> List[PaginaDelLote]:
        if self.mapa is not None:
            return list(self.mapa)
        return [
            PaginaDelLote(pagina=n, estado=0, inicio_documento=n)
            for n in range(1, (self.page_count or len(self.paginas)) + 1)
        ]

    def borrar_pagina(self, batch_id: str, pagina: int,
                      borrada: bool = True) -> bool:
        if pagina in self.no_se_pueden_borrar:
            return False
        self.borradas.append(pagina)
        if self.mapa is not None:
            self.mapa = [
                PaginaDelLote(p.pagina, p.estado, p.inicio_documento, borrada)
                if p.pagina == pagina else p
                for p in self.mapa
            ]
        return True

    def completar_lote(self, batch_id: str) -> Mapping[str, object]:
        self.completados.append(batch_id)
        return {"IsError": False}

    def validar_batch(
        self, batch_id: str, paginas: List[int],
    ) -> List[Mapping[str, object]]:
        cabeceras = list(paginas)
        self.validaciones_batch.append((batch_id, cabeceras))
        if self.mapa is not None and self.estados_tras_validar:
            self.mapa = [
                PaginaDelLote(
                    p.pagina,
                    self.estados_tras_validar.get(p.pagina, p.estado),
                    p.inicio_documento,
                    p.borrada,
                )
                for p in self.mapa
            ]
        return [
            {"Sequence": p.pagina, "Status": p.estado}
            for p in (self.mapa or []) if p.pagina in cabeceras
        ]


class AirVaultSimulado(ClienteFalso):
    """AirVault con cola, paginas por batch y cargas que tardan en armarse.

    Cada carga publicada aparece en la cola con cero paginas hasta que
    pasan ``vueltas`` llamadas a :meth:`avanzar`, que es lo que hace el
    ``dormir`` de las pruebas: el tiempo que AirVault tarda en armar lo
    recibido. Guarda en ``eventos`` lo que se sube y lo que se escribe, en
    orden, para poder afirmar el recorrido entero.
    """

    def __init__(self, picklist: Optional[List[str]] = None):
        super().__init__(picklist=picklist or ["HP-1848CMP"])
        self.por_lote: Dict[str, Dict[int, PaginaIndexada]] = {}
        self.faltan_vueltas: Dict[str, int] = {}
        self.eventos: List[tuple[str, str]] = []

    def publicar(self, nombre: str, paginas: int, vueltas: int = 2) -> str:
        batch_id = f"003B{len(self.lotes) + 1}"
        self.lotes.append(lote(batch_id, nombre, paginas))
        self.por_lote[batch_id] = {}
        self.faltan_vueltas[batch_id] = vueltas
        self.eventos.append(("subir", nombre))
        return batch_id

    def avanzar(self, _segundos: float = 0) -> None:
        for batch_id, faltan in self.faltan_vueltas.items():
            self.faltan_vueltas[batch_id] = max(0, faltan - 1)

    def terminar_de_armar(self) -> None:
        self.faltan_vueltas = {batch_id: 0 for batch_id in self.faltan_vueltas}

    def listar_lotes(self, filtro: str = "") -> List[ResumenLote]:
        self.filtros.append(filtro)
        visibles = [
            replace(l, paginas=0) if self.faltan_vueltas.get(l.batch_id) else l
            for l in self.lotes
        ]
        if not filtro:
            return visibles
        return [l for l in visibles if filtro.lower() in l.nombre.lower()]

    def abrir_lote(self, batch_id: str) -> Mapping[str, object]:
        self.abiertos.append(batch_id)
        actual = next(l for l in self.lotes if l.batch_id == batch_id)
        return {"pageCount": actual.paginas, "batchId": batch_id}

    def leer_pagina(self, batch_id: str, pagina: int) -> PaginaIndexada:
        self.lecturas.append(pagina)
        return self.por_lote[batch_id].get(
            pagina,
            PaginaIndexada(pagina=pagina, estado=3, valores={}, columnas={}),
        )

    def guardar_pagina(self, batch_id, pagina, valores, estado,
                       pagina_siguiente=None):
        self.eventos.append(("escribir", batch_id))
        self.escrituras.append((pagina, dict(valores), estado))
        self.por_lote[batch_id][pagina] = PaginaIndexada(
            pagina=pagina, estado=estado, valores=dict(valores), columnas={}
        )
        return {"ok": True}

    def paginas_del_lote(self, batch_id: str) -> List[PaginaDelLote]:
        actual = next(l for l in self.lotes if l.batch_id == batch_id)
        propias = self.por_lote[batch_id]
        return [
            PaginaDelLote(
                pagina=n,
                estado=propias[n].estado if n in propias else 3,
                inicio_documento=n,
            )
            for n in range(1, actual.paginas + 1)
        ]

    def completar_lote(self, batch_id: str) -> Mapping[str, object]:
        self.eventos.append(("completar", batch_id))
        return super().completar_lote(batch_id)


def subida_simulada(simulado: AirVaultSimulado, vueltas=2):
    """``Trabajo.subir`` contra el AirVault simulado.

    Hace lo que la subida de verdad deja hecho: la foto de la cola previa,
    la etapa en «hecha» y la carga publicada en la cola, que tarda
    ``vueltas`` en armarse. ``vueltas`` puede ser un numero o un
    diccionario por nombre de batch.
    """
    from app.airvault.model import EstadoEtapa

    def subir(self, sesion, pdf="", avisar=None, cliente=None):
        self.manifiesto.lotes_previos = [
            actual.batch_id for actual in simulado.listar_lotes()
        ]
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA, "ok")
        self.guardar()
        nombre = self.manifiesto.nombre_batch
        simulado.publicar(
            nombre,
            len(self.manifiesto.registros),
            vueltas.get(nombre, 2) if isinstance(vueltas, dict) else vueltas,
        )

    return subir


def pagina(numero: int, estado: int = 3,
           valores: Optional[Mapping[int, str]] = None) -> PaginaIndexada:
    """Pagina tal como la devolveria AirVault.

    Los valores llegan en un diccionario y no como argumentos con nombre
    porque las claves son identificadores numericos de campo.
    """
    return PaginaIndexada(
        pagina=numero, estado=estado,
        valores={int(k): str(v) for k, v in (valores or {}).items()},
        columnas={},
    )


def lote(batch_id: str, nombre: str, paginas: int = 0,
         repo_id: int = 3209, bloqueado_por: str = "") -> ResumenLote:
    return ResumenLote(
        batch_id=batch_id, nombre=nombre, paginas=paginas, repo_id=repo_id,
        repositorio="MXDocs", paso="Web Index",
        bloqueado_por=bloqueado_por, recibido="",
    )
