"""Busqueda de numeros de bitacora en reportes locales, sin releer el OCR."""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


FileStamp = tuple[int, int]
_SKIP_DIRS = {".git", "portable", "__pycache__", "logs", "recortes_firmas"}


def file_stamp(path: Path) -> FileStamp:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


@dataclass(frozen=True)
class LogbookQuery:
    number: str

    @property
    def exact(self) -> bool:
        return len(self.number) == 7

    def matches(self, number: str) -> bool:
        return number == self.number if self.exact else self.number in number


def logbook_query(text: str) -> LogbookQuery | None:
    """Siete digitos buscan globalmente; ``bit:`` permite un fragmento."""
    text = text.strip()
    if re.fullmatch(r"[0-9]{7}", text):
        return LogbookQuery(text)
    match = re.fullmatch(r"(?:bit|bit[aá]cora)\s*:\s*([0-9]{1,7})", text, re.I)
    return LogbookQuery(match[1]) if match else None


@dataclass(frozen=True)
class LogbookMatch:
    csv_path: Path
    source_row: int
    number: str
    filename: str
    page: str
    stamp: FileStamp


@dataclass(frozen=True)
class LogbookSearchResult:
    matches: tuple[LogbookMatch, ...]
    reports: int
    unavailable: tuple[Path, ...]


class SearchCancelled(Exception):
    """La consulta dejo de ser la que el usuario quiere ver."""


class LocalLogbookIndex:
    """Recuerda solo numeros y ubicaciones; invalida cada CSV que cambia.

    Se usa desde un solo hilo a la vez. Cada consulta descubre de nuevo los
    archivos para incluir ejecuciones nuevas y quitar las que ya no existen.
    El indice vive en memoria, sin base de datos ni rutas ajenas al programa.
    """

    def __init__(self) -> None:
        self._files: dict[Path, tuple[FileStamp, tuple[LogbookMatch, ...]]] = {}

    def search(
        self,
        roots: Iterable[Path],
        query: LogbookQuery,
        preferred: Path | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> LogbookSearchResult:
        def check() -> None:
            if cancelled():
                raise SearchCancelled

        unavailable: set[Path] = set()
        found: dict[str, Path] = {}
        visited: set[Path] = set()

        def on_error(error: OSError) -> None:
            unavailable.add(Path(error.filename) if error.filename else Path("."))

        for root in roots:
            check()
            root = Path(root).resolve()
            if not root.exists():
                continue
            for directory, dirs, names in os.walk(root, onerror=on_error):
                check()
                folder = Path(directory)
                if folder in visited:
                    dirs.clear()
                    continue
                visited.add(folder)
                dirs[:] = [
                    name for name in dirs
                    if name.casefold() not in _SKIP_DIRS
                    and not (folder / name).is_symlink()
                    and not (folder / name).is_junction()
                ]
                for name in names:
                    if Path(name).suffix.casefold() != ".csv":
                        continue
                    path = folder / name
                    if path.is_symlink():
                        continue
                    stem = path.stem.casefold()
                    complete = stem.endswith("_completo")
                    base = stem[:-len("_completo")] if complete else stem
                    key = str(folder / base).casefold()
                    if key not in found or complete:
                        found[key] = path

        paths = set(found.values())
        self._files = {path: entry for path, entry in self._files.items() if path in paths}
        stamps: dict[Path, FileStamp] = {}
        for path in paths:
            check()
            try:
                stamps[path] = file_stamp(path)
            except OSError:
                unavailable.add(path)

        preferred = preferred.resolve() if preferred is not None else None
        ordered = sorted(
            stamps,
            key=lambda path: (path != preferred, -stamps[path][0], str(path).casefold()),
        )
        matches: list[LogbookMatch] = []
        reports = 0
        for path in ordered:
            check()
            stamp = stamps[path]
            cached = self._files.get(path)
            if cached is None or cached[0] != stamp:
                try:
                    rows = self._read(path, stamp, check)
                    if file_stamp(path) != stamp:
                        # Una exportacion lo esta escribiendo: no publicar
                        # ubicaciones de una version incompleta del reporte.
                        unavailable.add(path)
                        continue
                except (OSError, ValueError, csv.Error):
                    unavailable.add(path)
                    continue
                self._files[path] = stamp, rows
            else:
                rows = cached[1]
            if rows:
                reports += 1
            for row in rows:
                check()
                if query.matches(row.number):
                    matches.append(row)
        return LogbookSearchResult(tuple(matches), reports, tuple(sorted(unavailable)))

    @staticmethod
    def _read(
        path: Path, stamp: FileStamp, check: Callable[[], None]
    ) -> tuple[LogbookMatch, ...]:
        """Lee en flujo y conserva solo lo necesario para ubicar una pagina."""
        rows: list[LogbookMatch] = []
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "log_number" not in reader.fieldnames:
                return ()
            for index, row in enumerate(reader):
                check()
                number = (row.get("log_number") or "").strip()
                rows.append(LogbookMatch(
                    path, index, number, (row.get("file") or "").strip(),
                    (row.get("page") or "").strip(), stamp,
                ))
        return tuple(rows)
