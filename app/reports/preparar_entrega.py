"""Completa los PDF de una ejecucion guardada sin repetir OCR ni reescribir datos."""

from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from app.airvault.flujo import carpeta_de_corrida, ruta_indice_paginas, ErrorDeCorrida
from app.airvault.mapping import obligatorios_vacios_por_pagina
from app.gui.csv_utils import read_csv_file
from app.models.schemas import ValidationReport
from app.reports.exclusiones import turno_datos
from app.reports.organize import escribir_entrega, escribir_indice_paginas
from app.utils.io import resolve_processed_path, unique_path


class ErrorPreparacionEntrega(ValueError):
    """Motivo local que puede mostrarse sin rutas ni detalles internos."""


def _clave(archivo, pagina):
    return Path(str(archivo)).name.casefold(), int(pagina)


def _reportes(csv: Path, raiz: Path, resolutor):
    companion = csv.with_suffix(".json")
    if not companion.is_file() and csv.stem.casefold().endswith("_completo"):
        companion = csv.with_name(f"{csv.stem[:-len('_completo')]}.json")
    try:
        payload = json.loads(companion.read_text(encoding="utf-8"))
        reportes = [ValidationReport.model_validate(r) for r in payload["reportes"]]
    except (OSError, ValueError, KeyError, TypeError):
        raise ErrorPreparacionEntrega("Faltan los datos procesados de esta ejecución. Vuelva a procesarla antes de subirla.") from None
    if not reportes or not any(r.pages for r in reportes) or any(r.cancelled for r in reportes):
        raise ErrorPreparacionEntrega("La ejecución quedó incompleta. Termine de procesarla antes de subirla.")
    _columnas, filas = read_csv_file(csv)
    try:
        claves = [_clave(fila.get("file", ""), fila.get("page", "")) for fila in filas]
        paginas = [_clave(r.source_filename, p.page_number) for r in reportes for p in r.pages]
    except (TypeError, ValueError):
        raise ErrorPreparacionEntrega("Los datos de las páginas no coinciden con el reporte de esta ejecución.") from None
    if Counter(claves) != Counter(paginas) or any(n != 1 for n in Counter(claves).values()):
        raise ErrorPreparacionEntrega("Los datos de las páginas no coinciden con el reporte de esta ejecución.")
    por_pagina = dict(zip(claves, filas))
    faltantes = {_clave(archivo, pagina) for archivo, pagina in
                 obligatorios_vacios_por_pagina(filas, resolutor)}
    for reporte in reportes:
        fuente = resolve_processed_path(Path(reporte.pdf_path))
        if not fuente.is_file():
            candidatos = {p.resolve() for p in (
                raiz / "input" / Path(reporte.pdf_path).name,
                raiz / "input" / "processed" / Path(reporte.pdf_path).name,
            ) if p.is_file()}
            if len(candidatos) != 1:
                raise ErrorPreparacionEntrega("No se encontró un original inequívoco para preparar la entrega. Ubique los PDF originales antes de continuar.")
            fuente = candidatos.pop()
        nombre = reporte.source_filename
        reporte.source_name = nombre
        reporte.pdf_path = str(fuente)
        for pagina in reporte.pages:
            clave = _clave(nombre, pagina.page_number)
            fila = por_pagina[clave]
            pagina.airvault_review = (str(fila.get("review", "")).strip().lower() == "true"
                                     or clave in faltantes)
            pagina.airvault_discrepancy = str(fila.get("disc", "")).strip().lower() == "true"
    return reportes


def preparar_entrega(csv: Path | str, raiz: Path, *, avisar=None, comprobar_cancelacion=None,
                     resolutor=None):
    """Reutiliza entregas validas y completa solo las que no se pueden subir."""
    from app.airvault.flujo import comprobar_entrega

    csv = Path(csv)
    carpeta = carpeta_de_corrida(csv)
    avisar = avisar or (lambda texto, hechas, total: None)
    comprobar_cancelacion = comprobar_cancelacion or (lambda: None)
    with turno_datos(carpeta):
        comprobar_cancelacion()
        try:
            return comprobar_entrega(csv), False
        except (ErrorDeCorrida, OSError, ValueError):
            pass
        avisar(f"Preparando la entrega de {carpeta.name}", 0, 0)
        reportes = _reportes(csv, Path(raiz), resolutor)
        logs = carpeta / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        publicados = []
        indice = ruta_indice_paginas(csv)
        anterior = indice.read_bytes() if indice.is_file() else None
        # Los archivos incompletos se quedan en una carpeta temporal. Solo
        # se publican despues de copiar todas las paginas correctamente.
        with TemporaryDirectory(prefix="entrega-", dir=logs) as temporal:
            destino = Path(temporal) / carpeta.name
            partes = escribir_entrega(reportes, destino, ("avion", "mes"),
                                      antes_de_pagina=comprobar_cancelacion)
            if not partes:
                raise ErrorPreparacionEntrega("No hay páginas disponibles para preparar esta entrega.")
            comprobar_cancelacion()
            indice_publicado = False
            try:
                for parte in partes:
                    ruta = unique_path(carpeta / parte.ruta.name)
                    os.replace(parte.ruta, ruta)
                    publicados.append(ruta)
                partes = [replace(parte, ruta=ruta) for parte, ruta in zip(partes, publicados)]
                indice_temporal = Path(temporal) / "paginas.json"
                escribir_indice_paginas(partes, indice_temporal)
                comprobar_cancelacion()
                indice.parent.mkdir(parents=True, exist_ok=True)
                os.replace(indice_temporal, indice)
                indice_publicado = True
                entrega = comprobar_entrega(csv)
            except BaseException:
                if indice_publicado:
                    if anterior is None:
                        indice.unlink(missing_ok=True)
                    else:
                        indice_temporal.write_bytes(anterior)
                        os.replace(indice_temporal, indice)
                for ruta in publicados:
                    ruta.unlink(missing_ok=True)
                raise
        avisar(f"Entrega preparada: {carpeta.name}", 0, 0)
        return entrega, True
