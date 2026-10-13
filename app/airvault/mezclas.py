"""Reconoce concatenaciones completas y aparta la carga antes de reemplazarla."""

from pathlib import Path

from app.airvault.config import CAMPO_LOG_NUMBER, ESTADO_VALIDO
from app.airvault.mapping import normalizar_log_number
from app.airvault.uploader import serializar_cargas

PREFIJO_APARTADO = "NO INDEXAR - CARGA MEZCLADA "


def identificar_partes(paginas, trabajos):
    """Exige todos los logs en su orden, sin paginas validas ni asignaciones ambiguas."""
    if not paginas or any(p.estado == ESTADO_VALIDO for p in paginas):
        return []
    posicion = 0
    encontrados = []
    while posicion < len(paginas):
        candidatos = []
        for trabajo in trabajos:
            if trabajo in encontrados:
                continue
            registros = trabajo.manifiesto.registros
            if not registros or not trabajo.manifiesto.bitacoras():
                continue
            tramo = paginas[posicion:posicion + len(registros)]
            if len(tramo) != len(registros):
                continue
            coincide = True
            for registro, remota in zip(registros, tramo):
                log = normalizar_log_number(remota.valores.get(CAMPO_LOG_NUMBER, ""))
                if registro.es_separador:
                    coincide = not log and remota.estado == 2
                else:
                    esperado = normalizar_log_number(registro.log_number)
                    coincide = bool(esperado and log == esperado)
                if not coincide:
                    break
            if coincide:
                candidatos.append(trabajo)
        if len(candidatos) != 1:
            return []
        elegido = candidatos[0]
        encontrados.append(elegido)
        posicion += len(elegido.manifiesto.registros)
    return encontrados if len(encontrados) >= 2 else []


@serializar_cargas
def recuperar_mezclas(trabajos, cliente, avisar=None):
    """Aparta solo una mezcla demostrada de partes presentes en esta cola.

    Conserva el original remoto con un titulo que impide reutilizarlo. La
    marca pendiente permite retomar si se corta entre renombrar y guardar.
    """
    from app.airvault.flujo import (
        ErrorDeCorrida, _candidatos_provisionales_descuadrados,
        _pendiente_de_busqueda, _reiniciar_subida_ausente,
    )
    from app.airvault import registro

    elegibles = [t for t in trabajos if _pendiente_de_busqueda(t)]
    if len(elegibles) < 2:
        return []
    lotes = list(cliente.listar_lotes())
    ids = {t.manifiesto.mezcla_pendiente for t in elegibles if t.manifiesto.mezcla_pendiente}
    for trabajo in elegibles:
        ids.update(l.batch_id for l in _candidatos_provisionales_descuadrados(trabajo, lotes))
    recuperados = []
    for lote in lotes:
        if lote.batch_id not in ids or lote.bloqueado_por:
            continue
        propios = [
            t for t in elegibles if t not in recuperados
            and (not lote.repo_id or lote.repo_id == t.manifiesto.repo_id)
        ]
        if lote.paginas > sum(len(t.manifiesto.registros) for t in propios):
            continue
        cliente.abrir_lote(lote.batch_id)
        try:
            paginas = [cliente.leer_pagina(lote.batch_id, n) for n in range(1, lote.paginas + 1)]
            involucrados = identificar_partes(paginas, propios)
        finally:
            cliente.cerrar_lote(lote.batch_id)
        if not involucrados:
            continue
        if any(len(t.manifiesto.batches_descartados) >= 2 for t in involucrados):
            raise ErrorDeCorrida(
                "AirVault volvio a juntar las cargas tras dos recuperaciones; "
                "se detiene la resubida automatica para no acumular copias"
            )
        if any(not Path(t.manifiesto.pdf_origen).is_file() for t in involucrados):
            raise ErrorDeCorrida("Se detectaron cargas mezcladas, pero falta un PDF original para recuperarlas")
        for trabajo in involucrados:
            trabajo.manifiesto.mezcla_pendiente = lote.batch_id
            trabajo.guardar()
        nombre = PREFIJO_APARTADO + lote.batch_id
        if lote.nombre != nombre and not cliente.renombrar_lote(lote.batch_id, nombre):
            raise ErrorDeCorrida("No se pudo apartar el batch mezclado; no se resube para evitar duplicados")
        confirmado = any(
            l.batch_id == lote.batch_id and l.nombre == nombre
            for l in cliente.listar_lotes()
        )
        if not confirmado:
            raise ErrorDeCorrida("AirVault aun no confirma el nombre del batch mezclado; se reintentara")
        for trabajo in involucrados:
            m = trabajo.manifiesto
            if lote.batch_id not in m.batches_descartados:
                m.batches_descartados.append(lote.batch_id)
            m.mezcla_pendiente = ""
            m.resubir_por_mezcla = True
            _reiniciar_subida_ausente(trabajo)
            # Sin darlas por eliminadas: estas bitacoras vuelven a subir.
            registro.desanotar(trabajo.carpeta, [trabajo.carpeta])
            recuperados.append(trabajo)
        if avisar:
            avisar(
                f"Batch {lote.batch_id}: mezcla confirmada; se aparto como NO INDEXAR "
                f"y se recuperaran {len(involucrados)} batches por separado", 0, 0,
            )
    return recuperados
