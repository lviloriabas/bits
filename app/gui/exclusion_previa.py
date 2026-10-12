"""Comparacion legible de paginas, con los controles de zoom de BITS."""

from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QVBoxLayout

from app.gui.tokens import SPACE_S
from app.gui.widgets import ZoomableScrollArea, ZoomOverlay, style_pdf_surface


class PreviaExclusion(QGroupBox):
    def __init__(self, titulo: str, parent=None):
        super().__init__(titulo, parent)
        self._clave = None
        self._ruta = None
        self._pagina = 1
        self._zoom = 1.0
        self._pixmap = None
        self._render_size = None
        self._page_size = None
        layout = QVBoxLayout(self)
        layout.setSpacing(SPACE_S)
        self.datos = QLabel("Seleccione una página para compararla.")
        self.datos.setWordWrap(True)
        layout.addWidget(self.datos)
        self.scroll = ZoomableScrollArea()
        style_pdf_surface(self.scroll)
        self.scroll.setFrameShape(ZoomableScrollArea.Shape.NoFrame)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setMinimumSize(160, 100)
        self.scroll.set_zoom_callback(self._acercar)
        self.imagen = QLabel("Seleccione una página")
        self.imagen.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.imagen)
        layout.addWidget(self.scroll, 1)
        # Debajo de la imagen para que no tape los datos manuscritos.
        controles = QHBoxLayout()
        self.zoom = ZoomOverlay(
            ("Acercar la página", "Acercar la página", lambda: self._acercar(1.25)),
            ("Ajustar la página al panel", "Ajustar la página al panel", self._ajustar),
            ("Alejar la página", "Alejar la página", lambda: self._acercar(0.8)),
            self,
        )
        controles.addStretch()
        controles.addWidget(self.zoom)
        controles.addStretch()
        layout.addLayout(controles)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._pintar)
        self.scroll.viewport().installEventFilter(self)
        self.mostrar(None)

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._timer.start(30)
        return super().eventFilter(watched, event)

    def mostrar(self, pagina, ruta=None, *, excluida=False):
        clave = (str(ruta), pagina.pagina) if pagina is not None else None
        if clave != self._clave:
            self._zoom = 1.0
            self._pixmap = None
            self._render_size = None
            self._page_size = None
        self._clave = clave
        self._ruta = Path(ruta) if ruta is not None else None
        if pagina is None:
            self.setTitle("Sin página para comparar")
            self.datos.setText("Seleccione una página en la lista.")
            self.imagen.clear()
            self.imagen.setText("Sin página para comparar")
            self.imagen.adjustSize()
        else:
            self._pagina = pagina.pagina
            estado = "Se excluirá" if excluida else "Se conservará"
            self.setTitle(f"Página {pagina.pagina}: {estado}")
            self.datos.setText(
                f"Archivo: {pagina.archivo}\n"
                f"Matrícula: {pagina.matricula or 'No disponible'}, "
                f"fecha: {pagina.fecha or 'No disponible'}\n"
                f"Vuelo: {pagina.vuelo or 'No disponible'}"
            )
            self.datos.setToolTip(self.datos.text())
            self._pintar()
        self._controles()

    def _controles(self):
        for boton in (self.zoom.btn_in, self.zoom.btn_out, self.zoom.btn_fit):
            boton.setEnabled(self._ruta is not None and self._pixmap is not None)
        self.zoom.value_label.setText(f"{self._zoom:.0%}")

    def _acercar(self, factor):
        self._zoom = max(0.5, min(8.0, self._zoom * factor))
        self._pintar()

    def _ajustar(self):
        self._zoom = 1.0
        self._pintar()

    def _pintar(self):
        if self._ruta is None:
            return
        espacio = self.scroll.viewport().size()
        if self._page_size is not None and self._pixmap is not None:
            ancho, alto = self._page_size
            escala = min(max(80, espacio.width() - 8) / ancho,
                         max(80, espacio.height() - 8) / alto) * self._zoom
            escala = min(escala, 4096 / max(ancho, alto))
            medida = (round(ancho * escala), round(alto * escala))
            if medida == self._render_size:
                self._controles()
                return
        try:
            import pymupdf
            from app.utils.io import resolve_processed_path

            with pymupdf.open(resolve_processed_path(self._ruta)) as documento:
                pagina = documento[self._pagina - 1]
                self._page_size = (pagina.rect.width, pagina.rect.height)
                escala = min(max(80, espacio.width() - 8) / pagina.rect.width,
                             max(80, espacio.height() - 8) / pagina.rect.height) * self._zoom
                escala = min(escala, 4096 / max(pagina.rect.width, pagina.rect.height))
                medida = (round(pagina.rect.width * escala), round(pagina.rect.height * escala))
                if medida != self._render_size or self._pixmap is None:
                    pixels = pagina.get_pixmap(matrix=pymupdf.Matrix(escala, escala),
                                              colorspace=pymupdf.csRGB, alpha=False)
                    imagen = QImage(pixels.samples, pixels.width, pixels.height,
                                    pixels.stride, QImage.Format.Format_RGB888).copy()
                    self._pixmap = QPixmap.fromImage(imagen)
                    self._render_size = medida
            self.imagen.setPixmap(self._pixmap)
            self.imagen.resize(self._pixmap.size())
        except (OSError, RuntimeError, ValueError, IndexError):
            self._pixmap = None
            self.imagen.clear()
            self.imagen.setText("No se pudo abrir la página original")
            self.imagen.adjustSize()
        self._controles()
