# BITS - Clasificación de Bitácoras

BITS lee bitácoras de vuelo y mantenimiento escaneadas y las convierte en una entrega revisable: reconoce los datos de cada página, los valida contra las reglas del libro y la flota, arma los PDF y los CSV de la entrega, y la sube e indexa en AirVault.

Es una aplicación de escritorio para Windows. El OCR corre local, en CPU y sin internet; solo la parte de AirVault necesita conexión, Microsoft Edge y una cuenta autorizada.

## Qué hace

- **Lee.** Alinea cada página contra una plantilla y reconoce matrícula, número de bitácora, fecha y vuelo. Analiza las zonas de firma y licencia para detectar faltas, y busca la marca VOID antes de reclamar una.
- **Valida.** Aplica las reglas del libro: 50 páginas de una sola aeronave, fecha que no retrocede, matrícula contrastada con `fleet.json`, duplicados marcados. Lo que no sostiene, lo manda a **REVISAR** en vez de inventarlo.
- **Entrega.** Exporta un PDF único o varios, separados por matrícula, mes, errores o posibles discrepancias, con CSV, JSON y estadísticas de la ejecución.
- **Indexa.** Sube los batches a AirVault, escribe los datos de búsqueda de cada página y completa los que quedan válidos. Con **Web Reports** corrige bitácoras duplicadas o mal indexadas que ya están publicadas.

En **Indexar en AirVault**, marque las casillas del selector de ejecuciones y pulse **Subir a AirVault** para enviar varias entregas juntas. Cada ejecución conserva su nombre y fecha de indexado; los batches ya subidos se retoman. El panel lateral incluye todos los batches locales, incluso los terminados. Su menú contextual permite revisar, subir, indexar, completar, cancelar, reanudar y eliminar los seleccionados. La selección se conserva cuando cambian sus estados.

En **Acciones**, use **Confirmar pendientes en Web Search** para revisar una muestra de cinco bitácoras repartidas por batch, o todas si tiene menos. Solo se confirma cuando aparecen todas las consultadas; la marca queda guardada y evita otra subida. El registro distingue el trabajo de BITS de la espera por AirVault. El porcentaje pesa las páginas de cada batch, conserva el avance durante los reintentos y muestra el 100 % al finalizar el proceso confirmado. Un cierre rechazado se reintenta espaciadamente hasta tres veces; después se puede pedir de nuevo desde el menú del batch.

Para elegir otra versión, abra **Herramientas**, **Elegir rama de actualización**. La aplicación consulta las ramas de origin, guarda un respaldo antes de cambiar y se reinicia. Los cambios locales inesperados y los commits anteriores quedan conservados; los datos de entrada, salida y las preferencias ignoradas se protegen. Debe terminar o cancelar los procesos antes de cambiar de rama.

## Uso

Abra `BITS.exe` desde la carpeta completa del programa. No requiere instalación: el intérprete, las bibliotecas y los modelos van en `portable/`.

Para reconstruir ese entorno desde el código, ejecute `setup.cmd` (o `setup.ps1`). Los puntos de entrada son:

| Archivo           | Para qué                                      |
| ----------------- | --------------------------------------------- |
| `run_gui.py`      | Ventana principal. Es lo que abre `BITS.exe`. |
| `run_cli.py`      | Procesado por consola, con rangos de páginas. |
| `run_airvault.py` | Carga e indexado en AirVault sin la interfaz. |
| `run_editor.py`   | Editor de plantilla.                          |

Pruebas: `python -m pytest`.

## Documentación

- [Manual de uso](docs/MANUAL.md): el proceso completo, paso a paso.
- [Guía técnica](docs/TECNICO.md): tecnologías, flujo de datos, reglas de validación y mantenimiento.

Construido con PySide6, PyMuPDF, OpenCV y PaddleOCR.
