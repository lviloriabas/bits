# BITS - Clasificación de Bitácoras

BITS lee bitácoras de vuelo y mantenimiento escaneadas y las convierte en una entrega revisable: reconoce los datos de cada página, los valida contra las reglas del libro y la flota, arma los PDF y los CSV de la entrega, y la sube e indexa en AirVault.

Es una aplicación de escritorio para Windows. El OCR corre local, en CPU y sin internet; solo la parte de AirVault necesita conexión, Microsoft Edge y una cuenta autorizada.

## Qué hace

- **Lee.** Alinea cada página contra una plantilla y reconoce matrícula, número de bitácora, fecha y vuelo. Analiza las zonas de firma y licencia para detectar faltas, y busca la marca VOID antes de reclamar una.
- **Valida.** Aplica las reglas del libro: 50 páginas de una sola aeronave, fecha que no retrocede, matrícula contrastada con `fleet.json`, duplicados marcados. Lo que no sostiene, lo manda a **REVISAR** en vez de inventarlo.
- **Entrega.** Exporta un PDF único o varios, separados por matrícula, mes, errores o posibles discrepancias, con CSV, JSON y estadísticas de la ejecución.
- **Indexa.** Sube los batches a AirVault, escribe los datos de búsqueda de cada página y completa los que quedan válidos. Con **Web Reports** corrige bitácoras duplicadas o mal indexadas que ya están publicadas.

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
