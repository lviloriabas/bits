# BITS - Clasificación de Bitácoras

BITS lee bitácoras de vuelo y mantenimiento escaneadas y las convierte en una entrega revisable: reconoce los datos de cada página, los valida contra las reglas del libro y la flota, arma los PDF y los CSV de la entrega, y la sube e indexa en AirVault.

Es una aplicación de escritorio para Windows. El OCR corre local, en CPU y sin internet; solo la parte de AirVault necesita conexión, Microsoft Edge y una cuenta autorizada.

## Qué hace

- **Lee.** Alinea cada página contra una plantilla y reconoce matrícula, número de bitácora, fecha y vuelo. Analiza las zonas de firma y licencia para detectar faltas, y busca la marca VOID antes de reclamar una.
- **Valida.** Aplica las reglas del libro: 50 páginas de una sola aeronave, fecha que no retrocede, matrícula contrastada con `fleet.json`, duplicados marcados. Lo que no sostiene, lo manda a **REVISAR** en vez de inventarlo.
- **Entrega.** Exporta un PDF único o varios, separados por matrícula, mes, errores o posibles discrepancias, con CSV, JSON y estadísticas de la ejecución.
- **Indexa.** Sube los batches a AirVault, escribe los datos de búsqueda de cada página y completa los que quedan válidos. Con **Web Reports** corrige bitácoras duplicadas o mal indexadas que ya están publicadas.

En **Indexar en AirVault**, marque las casillas del selector de ejecuciones y pulse **Subir a AirVault** para enviar varias entregas juntas. Cada ejecución conserva su nombre y fecha de indexado; los batches ya subidos se retoman. El panel lateral incluye todos los batches locales, incluso los terminados. Inicia con 235 píxeles de ancho, se puede redimensionar arrastrando su división y contiene el progreso global. **Ocultar batches indexados** oculta los indexados que aún no se completaron; **Ocultar batches completados** oculta los completados. Ambas opciones son independientes y conservan visibles los cancelados. Estos filtros solo cambian la vista. Su menú contextual permite revisar, subir, indexar, completar, cancelar, reanudar y eliminar los seleccionados. La selección se conserva cuando cambian sus estados.

En **Acciones**, use **Verificar subidas en Web Search** para buscar los números de bitácora de todos los batches sin confirmar, incluidos los ocultos y los que todavía no tienen ID ni subida local registrada. Encontrarlos confirma que esas bitácoras ya se subieron anteriormente, aunque AirVault las conserve bajo otro nombre o en distintos batches. La muestra es del 2 % de los números válidos distintos, con un mínimo de siete y un máximo de quince, repartidos entre el inicio y el final; si hay menos de siete, consulta todos los disponibles. Consulta hasta 32 batches simultáneamente mediante conexiones de Web Search independientes y muestra cada resultado al recibirlo. No espera el turno de subida, el indexado ni el intervalo de revisión de Web Index. Si aparece toda la muestra, marca el batch local como completado y evita volver a enviarlo; si falta algún número o la consulta falla, conserva el estado pendiente. Con una sesión disponible y **Revisar cada** activado, la lectura arranca inmediatamente al incorporar batches o cambiar su avance; el botón permite repetirla a mano. La prueba queda vinculada a los números consultados y no adopta el ID de una carga anterior. Cambiar los números obliga a verificar de nuevo. Cada consulta tiene un presupuesto de 60 segundos para las respuestas de Web Search, sin esperas añadidas entre batches. La muestra confirma publicación; no audita cada página.

Las ejecuciones pueden ponerse en cola juntas o abrirse en ventanas independientes. Un turno compartido mantiene una sola subida en vuelo: cada carga se identifica antes de iniciar la siguiente, mientras el mismo hilo aprovecha la espera para indexar batches confirmados. La protección consulta también cargas sin identificar de otras ejecuciones, incluso cuando su ventana terminó o canceló el trabajo. Si el batch no aparece después del plazo y una lectura correcta de la cola confirma su ausencia, queda rojo como **No encontrado en AirVault**, se conserva para reenvío manual y se continúa con el siguiente. El margen predeterminado es de 3,6 segundos por página, entre cinco minutos y una hora: 500 páginas reciben 30 minutos. Es una estimación operativa, no un plazo garantizado por AirVault. Las cargas parciales o ambiguas y los errores de conexión mantienen la protección contra mezclas; no autorizan otra carga.

El registro distingue el trabajo de BITS de la espera por AirVault. El porcentaje pesa las páginas de cada batch, conserva el avance durante los reintentos y muestra el 100 % al finalizar la meta elegida. Sin **Completar batch**, la meta es el indexado; la comprobación de publicación puede continuar después sin hacer retroceder el porcentaje. Un cierre rechazado se reintenta espaciadamente hasta tres veces; después se puede pedir de nuevo desde el menú del batch.

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
