"""Escritura reversible de exclusiones fuera del hilo de la interfaz."""

from pathlib import Path

from loguru import logger

from app.gui.worker import OutputsWorker
from app.reports.exclusiones import guardar_con_respaldo, restaurar_exclusion
from app.utils.mensajes import mensaje_error


class ExclusionWorker(OutputsWorker):
    def __init__(self, reports, options, parent=None, *, restaurar=False):
        super().__init__(reports, options, parent)
        self.restaurar = restaurar

    def run(self) -> None:
        try:
            from app.reports.outputs import write_outputs

            run_dir = Path(self.options.run_dir)
            if self.restaurar:
                self.progress.emit("Restaurando la ultima exclusion…", 50)
                self.reports = restaurar_exclusion(run_dir)
                resultado = run_dir
            else:
                resultado = guardar_con_respaldo(run_dir, lambda: write_outputs(
                    self.reports, self.options, on_stage=self._on_stage,
                ))
        except Exception as exc:
            logger.exception("No se pudo guardar la exclusion o su restauracion")
            self.failed.emit(mensaje_error(exc, "No se pudo completar el cambio. Vuelva a intentar o restaure la última exclusión."))
        else:
            self.succeeded.emit(resultado)
