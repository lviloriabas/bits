"""Generación de reportes CSV (compatibles con Excel)."""

from __future__ import annotations

import csv
import re
from calendar import monthrange
from pathlib import Path
from typing import List, Optional, Union

from loguru import logger

from app.models.schemas import PageResult, Status, ValidationReport
from app.templates.schema import FieldType, Template
from app.utils.postprocess import MONTH_WORDS
from app.validation.duplicates import detect_duplicate_log_pages
from app.validation.page_status import needs_review

_DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
_MATRICULA_RE = re.compile(r"^HP-\d{4}(CMP|WWP)$")
_DAY_RE = re.compile(r"^\d{1,2}$")
_MONTH_NAMES = tuple(word for word, _number in MONTH_WORDS)
_MONTH_RE = re.compile(r"^(\d{1,2}|" + "|".join(_MONTH_NAMES) + r")$")
# Celdas de carácter de la banda de fecha (una casilla por campo).
_CHAR_DIGIT_CELL_RE = re.compile(r"^(day|year)_\d+$")
_CHAR_LETTER_CELL_RE = re.compile(r"^month_\d+$")

CSV_DATE_SPECIFIC = "specific_day"
CSV_DATE_MONTH_END = "month_end"
CSV_DATE_MODES = frozenset({CSV_DATE_SPECIFIC, CSV_DATE_MONTH_END})
_MONTH_NUMBER = {word: number for word, number in MONTH_WORDS}


class CsvReporter:
    """Escribe el reporte de validación en CSV ancho (en inglés).

    Una fila por página del PDF:
        file, page, <field>, review, dup, disc, disc_reason, <field>_conf,
        <field>_status, <field>_comment, <field>_source, ..., date, time_ms

    - ``file``: nombre del PDF del que proviene la página.
    - ``review``: ``true`` cuando la bitácora no se indexa sola y viaja en el
      batch REVISAR. Recoge todo lo que aparta una página: un campo
      obligatorio del Web Index que no pudo completarse, una fecha que
      contradice al libro, una página en blanco y también las discrepancias,
      que además llevan su propia columna ``disc``. Es la columna con la que
      se sabe, sin abrir los PDFs, cuánto de la ejecución hay que indexar a
      mano. Sale de ``needs_review`` (``app/validation/page_status.py``), el
      mismo criterio con el que la exportación reparte la entrega, así que
      el archivo y los PDFs nunca cuentan cosas distintas.
    - ``dup``: ``true`` cuando el ``log_number`` ya apareció antes en el batch.
    - ``disc``: ``true`` cuando a la bitácora le falta una firma exigida y
      esa ausencia se leyó con seguridad. Sale de ``page.discrepancy``, que
      fija ``clasificar_lote`` (``app/validation/discrepancias.py``) antes de
      escribir el reporte; si esa clasificación no se ejecutó, la columna
      queda en ``false``. Es la columna que le pone AUDIT IN PROGRESS a la
      página al indexarla, así que las lecturas inciertas no entran.
    - ``disc_reason``: razón formal que se seleccionará en AirVault, sin el
      prefijo ``DISCREPANCY NOTE:``. Solo se escribe cuando ``disc`` es
      ``true``. Sale de ``page.discrepancy_note``, que fija la misma pasada
      de ``clasificar_lote``.
    - ``date``: fecha normalizada (YYYY/MM/dd) combinando day/month/year.
    - ``time_ms``: tiempo de procesamiento de la página, repartido sobre el
      reloj real de la ejecución (ver ``page_time_ms``), de modo que la suma
      de la columna es lo que tardó el procesamiento completo.

    Las columnas ``<field>_status`` y ``<field>_comment`` se omiten para
    los campos de firma: quedan ``<field>``, ``<field>_conf`` y
    ``<field>_source``.

    Puertas finales de formato: los valores de matrícula, día, mes, año y
    celdas de carácter solo se escriben si cumplen su formato canónico
    (HP-XXXXCMP/WWP, dígitos, mes canónico, año de 2-4 dígitos, un dígito
    en day_X/year_X, una letra en month_X); cualquier lectura basura del
    OCR queda como celda vacía.
    """

    def write(
        self,
        report_or_reports: Union[ValidationReport, List[ValidationReport]],
        path: Path,
        template: Optional[Template] = None,
        date_mode: str = CSV_DATE_MONTH_END,
    ) -> Path:
        """Guarda el reporte como CSV (UTF-8 con BOM para Excel).

        Args:
            report_or_reports: Reporte único o lista de reportes (uno por
                PDF). Con una lista se genera una tabla consolidada.
            path: Ruta de salida.
            template: Plantilla usada (define el orden y los campos). Si no
                se provee, se usan los campos de la primera página no vacía.
            date_mode: ``specific_day`` conserva el día leído y usa fin de
                mes solo cuando falta; ``month_end`` representa todas las
                fechas con el último día calendario del mes.

        Returns:
            La ruta del archivo generado.
        """
        reports = (
            [report_or_reports]
            if isinstance(report_or_reports, ValidationReport)
            else list(report_or_reports)
        )
        if not reports:
            raise ValueError("No hay reportes para generar el CSV")
        if date_mode not in CSV_DATE_MODES:
            raise ValueError(f"Política de fecha CSV no válida: {date_mode}")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        skip_ids = self._skip_ids(reports, template)
        columns = self.columns_for_fields(
            self.fields_for(reports, template), skip_ids=skip_ids
        )
        rows = self.rows_for(reports, template, date_mode=date_mode)

        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        logger.info(f"Reporte CSV generado: {path} "
                    f"({sum(len(r.pages) for r in reports)} páginas)")
        return path

    @classmethod
    def rows_for(
        cls,
        reports: List[ValidationReport],
        template: Optional[Template] = None,
        date_mode: str = CSV_DATE_MONTH_END,
    ) -> List[dict[str, object]]:
        """Las filas del CSV, en el mismo orden en que se escriben.

        Existe para poder mirar la fila final sin haberla escrito todavía.
        Quién va al batch REVISAR se decide sobre ella (una matrícula que el
        CSV no llega a traer deja la página amarilla en AirVault, y eso solo
        se ve en la fila), pero esa decisión es a su vez la columna
        ``review``: sin este paso previo el archivo tendría que escribirse
        dos veces para poder contarse a sí mismo.
        """
        fields = cls.fields_for(reports, template)
        duplicates = iter(detect_duplicate_log_pages(reports))
        time_factor = cls.run_time_factor(reports)
        return [
            cls.row_for_page(
                report,
                page,
                fields,
                date_mode=date_mode,
                duplicate=next(duplicates).duplicate,
                time_factor=time_factor,
            )
            for report in reports
            for page in report.pages
        ]

    @classmethod
    def columns_for_fields(cls, fields: List[str],
                           skip_ids: frozenset[str] = frozenset()) -> List[str]:
        """Devuelve las columnas del CSV para una lista de campos.

        Args:
            fields: Identificadores de campos en el orden de las columnas.
            skip_ids: Campos a los que no se les emiten columnas ``_status``
                ni ``_comment`` (las firmas, p. ej.).
        """
        columns = ["file", "page"]
        for field_id in fields:
            columns.append(field_id)
            if field_id == "log_number":
                columns.extend(["review", "dup", "disc", "disc_reason"])
            columns.append(f"{field_id}_conf")
            if field_id not in skip_ids:
                columns.extend([
                    f"{field_id}_status",
                    f"{field_id}_comment",
                ])
            columns.append(f"{field_id}_source")
        # Las banderas de la página acompañan al ``log_number``, que es lo
        # que identifica la bitácora; sin ese campo se emiten igual al final.
        if "review" not in columns:
            columns.extend(["review", "dup", "disc", "disc_reason"])
        columns.extend(["date", "time_ms"])
        return columns

    @classmethod
    def columns_for(
        cls,
        reports: List[ValidationReport],
        template: Optional[Template] = None,
    ) -> List[str]:
        """Devuelve las columnas que usaría ``write`` para esos reportes."""
        fields = cls.fields_for(reports, template)
        skip_ids = cls._skip_ids(reports, template)
        return cls.columns_for_fields(fields, skip_ids=skip_ids)

    @classmethod
    def fields_for(
        cls,
        reports: List[ValidationReport],
        template: Optional[Template] = None,
    ) -> List[str]:
        """Devuelve los identificadores de campos usados por el CSV."""
        return cls._columns_fields(reports, template)

    @classmethod
    def row_for_page(
        cls,
        report: ValidationReport,
        page: PageResult,
        fields: List[str],
        date_mode: str = CSV_DATE_MONTH_END,
        duplicate: bool = False,
        time_factor: Optional[float] = None,
    ) -> dict[str, object]:
        """Construye la fila CSV correspondiente a una página."""
        row: dict[str, object] = {
            "file": report.source_filename,
            "page": page.page_number,
            "review": str(needs_review(page)).lower(),
            "dup": str(duplicate).lower(),
            "disc": str(bool(page.discrepancy)).lower(),
            "disc_reason": page.discrepancy_note if page.discrepancy else "",
        }
        by_id = {field.field_id: field for field in page.fields}

        for field_id in fields:
            result = by_id.get(field_id)
            if result is None:
                row[field_id] = ""
                row[f"{field_id}_conf"] = ""
                row[f"{field_id}_status"] = ""
                row[f"{field_id}_comment"] = ""
                row[f"{field_id}_source"] = ""
                continue
            row[field_id] = cls._gated_value(field_id, result.value)
            row[f"{field_id}_conf"] = round(result.confidence, 3)
            row[f"{field_id}_status"] = result.status.value
            row[f"{field_id}_comment"] = result.comment
            row[f"{field_id}_source"] = result.source

        policy = cls._date_policy(page, by_id, date_mode)
        if policy is not None:
            day_value, date_value, changed, reason = policy
            row["date"] = date_value
            if changed and "day" in fields:
                day = by_id.get("day")
                confidences = [
                    field.confidence for field in (
                        by_id.get("month"), by_id.get("year")
                    ) if field is not None
                ]
                row["day"] = day_value
                row["day_conf"] = round(min(confidences, default=0.0), 3)
                row["day_status"] = Status.WARNING.value
                row["day_comment"] = reason
                row["day_source"] = "csv_date_policy"
        else:
            row["date"] = cls._date(page)
        row["time_ms"] = round(cls.page_time_ms(report, page, time_factor), 1)
        return row

    @staticmethod
    def run_wall_ms(reports: List[ValidationReport]) -> float:
        """Reloj de pared de la ejecución completa, sin contar dos veces.

        Con un proceso por archivo las bitácoras se solapan: cada una mide su
        propio reloj mientras comparte la CPU con las demás, así que sumar
        ``processing_ms`` cuenta el mismo minuto una vez por archivo. El
        tiempo real de la ejecución es el intervalo que va del primer arranque
        al último final.
        """
        stamped = [
            report for report in reports
            if report.started_at > 0 and report.processing_ms > 0
        ]
        if len(stamped) != len(reports) or not stamped:
            # Reportes sin marca de arranque (pruebas, JSON reconstruido):
            # se cae al reloj por bitácora, que es lo único medible.
            return sum(report.processing_ms for report in reports)
        start = min(report.started_at for report in stamped)
        end = max(
            report.started_at + report.processing_ms / 1000.0
            for report in stamped
        )
        return max(0.0, (end - start) * 1000.0)

    @classmethod
    def run_time_factor(cls, reports: List[ValidationReport]) -> float:
        """Escala que lleva los tiempos medidos al reloj real de la ejecución."""
        measured = sum(
            page.processing_ms for report in reports for page in report.pages
        )
        wall = cls.run_wall_ms(reports)
        if measured <= 0 or wall <= 0:
            # Sin reloj de ejecución no hay nada contra qué normalizar: se
            # conserva el tiempo medido en cada página.
            return 1.0
        return wall / measured

    @staticmethod
    def page_time_ms(
        report: ValidationReport,
        page: PageResult,
        factor: Optional[float] = None,
    ) -> float:
        """Tiempo de la página repartido sobre el reloj real de la ejecución.

        ``page.processing_ms`` es tiempo de pared medido *dentro* del proceso
        que atendió la página. Con el OCR repartido en un proceso por núcleo,
        varias páginas transcurren a la vez y además cada una tarda más por
        competir por CPU y memoria, así que sumar la columna no daba el
        tiempo de la ejecución sino varias veces ese tiempo: 50 páginas que el
        reloj midió en 150 s sumaban 693 s en el CSV.

        Se conserva la proporción entre páginas (una página lenta sigue
        destacando frente a las demás) y se escala el conjunto para que la
        suma sea lo que tardó realmente la ejecución. ``factor`` viene de
        ``run_time_factor`` y cubre el batch completo; sin él se normaliza
        contra el reloj de la propia bitácora, que es lo correcto cuando el
        reporte se mira por separado.
        """
        if factor is not None:
            return page.processing_ms * factor
        measured = sum(other.processing_ms for other in report.pages)
        if report.processing_ms <= 0 or measured <= 0:
            return page.processing_ms
        return page.processing_ms * report.processing_ms / measured

    @staticmethod
    def _date_policy(
        page: PageResult,
        by_id: dict[str, object],
        date_mode: str,
    ) -> Optional[tuple[str, str, bool, str]]:
        """Calcula la fecha de salida sin mutar el resultado OCR original."""
        if date_mode not in CSV_DATE_MODES:
            raise ValueError(f"Política de fecha CSV no válida: {date_mode}")
        month_field = by_id.get("month")
        year_field = by_id.get("year")
        day_field = by_id.get("day")
        month_value = getattr(month_field, "value", None)
        year_value = getattr(year_field, "value", None)
        day_value = getattr(day_field, "value", None)
        month = _MONTH_NUMBER.get(str(month_value).upper())
        if month is None and str(month_value).isdigit():
            numeric_month = int(str(month_value))
            month = numeric_month if 1 <= numeric_month <= 12 else None
        if year_value is None or not str(year_value).isdigit():
            return None
        year_text = str(year_value)
        if len(year_text) == 2:
            year = 2000 + int(year_text)
        elif len(year_text) == 4 and 2000 <= int(year_text) <= 2100:
            year = int(year_text)
        else:
            return None
        if month is None:
            return None
        last_day = monthrange(year, month)[1]
        detected_day = (
            int(str(day_value))
            if day_value is not None and str(day_value).isdigit()
            else None
        )
        if detected_day is not None and not 1 <= detected_day <= last_day:
            detected_day = None

        if date_mode == CSV_DATE_SPECIFIC and detected_day is not None:
            selected_day = detected_day
            changed = False
            reason = ""
        elif date_mode == CSV_DATE_MONTH_END:
            selected_day = last_day
            changed = detected_day != last_day
            reason = (
                "CSV configurado para usar el último día del mes; "
                f"día OCR: {day_value or 'vacío'}"
            )
        else:
            selected_day = last_day
            changed = True
            reason = (
                "Día OCR sin resolver; se usó el último día calendario "
                "del mes en el CSV"
            )
        return (
            str(selected_day),
            f"{year:04d}/{month:02d}/{selected_day:02d}",
            changed,
            reason,
        )

    @staticmethod
    def _gated_value(field_id: str, value: Optional[str]) -> str:
        """Aplica la puerta de formato canónico al valor del campo.

        Solo matrícula, día, mes, año y las celdas de carácter tienen
        puerta; el resto de campos se escribe tal cual. Un valor que no
        cumple el formato canónico se descarta (celda vacía).
        """
        if not value:
            return ""
        if field_id == "matricula":
            return value if _MATRICULA_RE.fullmatch(value) else ""
        if field_id == "day":
            return value if _DAY_RE.fullmatch(value) else ""
        if field_id == "month":
            return value if _MONTH_RE.fullmatch(value) else ""
        if field_id == "year":
            if len(value) == 2 and value.isdigit():
                return value
            if len(value) == 4 and value.isdigit() and 2000 <= int(value) <= 2100:
                return value
            return ""
        if _CHAR_DIGIT_CELL_RE.match(field_id):
            return value if value.isdigit() and len(value) == 1 else ""
        if _CHAR_LETTER_CELL_RE.match(field_id):
            return value if value.isalpha() and len(value) == 1 else ""
        return value

    @staticmethod
    def _columns_fields(
        reports: List[ValidationReport], template: Optional[Template]
    ) -> List[str]:
        if template is not None:
            return [f.id for f in template.fields]
        for report in reports:
            for page in report.pages:
                if page.fields:
                    return [f.field_id for f in page.fields]
        return []

    @classmethod
    def _skip_ids(
        cls,
        reports: List[ValidationReport],
        template: Optional[Template] = None,
    ) -> frozenset[str]:
        """Identificadores que no llevan columnas ``_status``/``_comment``."""
        if template is not None:
            return frozenset(
                f.id for f in template.fields
                if f.type is FieldType.SIGNATURE
            )
        ids = set()
        for report in reports:
            for page in report.pages:
                ids.update(
                    f.field_id for f in page.fields
                    if f.field_type == "signature"
                )
        return frozenset(ids)

    @staticmethod
    def _date(page) -> str:
        """Fecha normalizada (YYYY/MM/dd) de la página, si está disponible."""
        if page.date and _DATE_RE.match(page.date):
            return page.date
        return ""
