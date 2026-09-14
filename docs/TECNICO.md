# BITS: guía técnica de mantenimiento

El [manual](MANUAL.md) describe la operación. Esta guía resume tecnologías, procesos, archivos de estado y puntos de mantenimiento. Las [instrucciones del repositorio](../AGENTS.md) siguen vigentes.

## Tecnologías y arranque

| Componente | Tecnología y función |
|---|---|
| Ejecución | Python 3.12 portable para Windows. `BITS.exe` inicia `run_gui.py` mediante el intérprete incluido. |
| Interfaz | PySide6/Qt: ventanas, tablas con modelos, visor, editor y trabajos de fondo con `QThread`. |
| PDF | PyMuPDF: renderizado, extracción y composición de páginas. |
| Imágenes | OpenCV, NumPy y Pillow: alineación, recortes, análisis de tinta y conversiones. |
| OCR | PaddleOCR 3.7.0, PaddleX 3.7.2 y PaddlePaddle, siempre en CPU. |
| Datos | Pydantic valida configuración, plantillas y resultados; `csv` y `json` generan los reportes. |
| AirVault | `requests` para HTTP, `truststore` para certificados de Windows y Edge para autenticación. |
| Soporte | Loguru para registros, Send2Trash para Papelera y pytest para pruebas. |

Consulte [requirements.txt](../requirements.txt) para las dependencias declaradas. Los modelos habituales son `PP-OCRv6_medium_det` y `PP-OCRv5_mobile_rec`; las marcas VOID usan `PP-OCRv6_medium_rec`.

### Redes convolucionales en el OCR

Sí se usan redes neuronales convolucionales, pero están encapsuladas en los modelos preentrenados de PaddleOCR; BITS no define ni entrena una CNN propia. `PP-OCRv6_medium_det` localiza las regiones con texto mediante el backbone convolucional LCNetV4 y el cuello RepLKFPN. `PP-OCRv5_mobile_rec` reconoce los caracteres de cada recorte con una arquitectura híbrida que contiene convoluciones, un codificador SVTR y una salida CTC. `PP-OCRv6_medium_rec`, usado para leer las marcas VOID, también combina un backbone convolucional con un codificador SVTR. Por eso es más preciso describir estos modelos como redes híbridas con componentes CNN, no como una CNN aislada.

`PaddleOcrEngine`, en `app/ocr/engine.py`, carga el detector y el reconocedor para el OCR completo. En campos configurados como una sola línea omite el detector y ejecuta directamente el reconocedor; `app/vision/void_mark.py` reutiliza ese camino con el modelo de VOID. Todas estas inferencias se ejecutan en CPU. La clasificación de firmas de `app/vision/signature.py` no usa una red neuronal: aplica operaciones morfológicas y umbrales de densidad de tinta.

El ajuste local de campos descrito abajo tampoco agrega una CNN: usa OpenCV y NumPy, ya declarados en `requirements.txt`. Los tres modelos neuronales del OCR se precargan con `tools/precache_paddle.py`; el setup comprueba también el reconocedor de VOID. No se requiere GPU, entrenamiento ni una descarga adicional para localizar las casillas.

La distribución incluye intérprete, bibliotecas y modelos dentro de `portable/`. `ensure_portable_env()`, en `app/utils/portable.py`, configura el entorno antes de importar Paddle:

```text
PADDLE_PDX_CACHE_HOME=<raíz>/portable/paddlex
PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1
PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0
FLAGS_use_mkldnn=0
```

Los motores se crean con `device="cpu"`. oneDNN está desactivado por compatibilidad. El OCR normal no necesita internet ni descarga modelos durante el procesamiento. AirVault sí utiliza servicios de red y Edge instalado.

## Flujo de datos

```text
PDF originales + plantilla
  -> renderizado y calibración
  -> recortes, OCR y análisis de tinta
  -> validación por página y corrección por libro
  -> CSV, JSON y estadísticas
  -> exportación PDF e índice de páginas
  -> carga, revisión, indexado y cierre en AirVault
```

La GUI y la consola comparten `Pipeline`, `process_pdf_batch()` y `write_outputs()`. La GUI ejecuta todos los PDF seleccionados; la consola también permite rangos. `PageRange` numera el conjunto desde 1 y lo divide por archivo.

### 1. Preprocesado

PyMuPDF renderiza las páginas; la GUI configura 200 DPI. OpenCV corrige inclinación y alinea el formulario contra la referencia canónica declarada por la plantilla. Si no está disponible, utiliza la página de referencia como respaldo.

La alineación extrae estructura impresa y estima rotación, escala uniforme y desplazamiento con ORB/AKAZE y RANSAC; puede usar correlación de fase como respaldo. Descarta transformaciones sin evidencia suficiente. La geometría de fecha se localiza dinámicamente para tolerar casillas desplazadas entre escaneos.

**Preprocesar**, por separado, ejecuta la preparación y actualiza la vista previa sin OCR ni entrega. **Procesar** incorpora esa preparación antes de leer.

Código: `app/vision/pdf_loader.py`, `preprocessing.py`, `alignment.py`, `date_geometry.py` y `app/gui/worker.py`.

### Anclajes por página y por campo

`template/aircraft_log.json` guarda un patrón de líneas impresas y las anclas de cada borde del campo. `reticula.py` identifica esas líneas por su orden y separación. El patrón identifica la raya; su posición de recorte se mide en la página actual después de enderezarla y alinearla. Las esquinas exteriores de la hoja no bastan para corregir deformaciones distintas entre cabecera, firmas y pie.

`app/vision/field_geometry.py` añade una comprobación local:

1. Calcula una sola imagen de estructura impresa por página y la reutiliza para el ajuste global y los perfiles de cada campo.
2. Busca cada raya cerca de su posición medida. El radio no supera el 0,6% del eje ni el 30% de la distancia a la raya vecina; exige una línea fina que cubra al menos el 55% de la banda observada.
3. Si falta una raya global, interpola entre rayas identificadas de esa misma página solo para acotar la búsqueda. La interpolación no se acepta como una detección: debe aparecer una única raya local. Sin referencias que encierren el hueco no extrapola desde otras páginas.
4. Rechaza candidatos ambiguos, separaciones incompatibles y rectángulos fuera de la imagen. Una referencia lateral ausente ya no permite presentar una coordenada horizontal fija como un ajuste completo. Las celdas de fecha comparten la banda de observación para evitar confundir un carácter con una línea; el detector específico DD|MMM|AA conserva la última palabra cuando localiza la fecha.

Cuando una raya no aparece localmente pero sí se identificó globalmente, conserva esa medida de la página. Si el campo no puede comprobarse, conserva el ajuste global disponible o el recorte de respaldo y añade `Posicion del campo sin confirmar; revisar recorte` a su comentario; eleva a `WARNING` un campo que estaba en `OK`. Esto no inventa una ausencia de firma, no borra la lectura y no añade columnas ni modifica las reglas de exportación del CSV. Un aviso de geometría no implica por sí solo pertenecer al batch REVISAR.

Los rectángulos efectivos viajan normalizados en `PageResult.preview_boxes`, que no se serializa en los reportes. El OCR, las firmas y la revisión posterior de firmas usan esos recortes. Preprocesar también calcula cajas por página para el visor; una vez procesada, prevalece la geometría final del OCR, incluida la fecha.

Validación: `tests/test_field_geometry.py` cubre desplazamientos por página, movimiento local, rayas que solo sobreviven junto al campo, referencias ausentes, candidatos ambiguos, funcionamiento sin red y correspondencia entre OCR y vista previa. La prueba con páginas reales sirve para medir coste y observar desplazamientos, pero no demuestra exactitud universal: hacen falta casillas etiquetadas para medir errores de localización. Las líneas borradas, formularios distintos o deformaciones mayores que la ventana pueden quedar sin confirmar.

Medición del 13 de septiembre de 2026, en este equipo: páginas 3, 31 y 91 de `Image_001.pdf` a `Image_004.pdf`, renderizadas a 200 DPI y enderezadas. La mediana de detección global más ajuste local fue 79 ms por página; el coste adicional mediano frente al ajuste global fue 33 ms. Excluye renderizado, deskew y OCR. Es una muestra de doce páginas, no un límite de tiempo ni una garantía de precisión para otros equipos o escaneos.

### 2. Procesado: OCR y firmas

1. Detecta páginas en blanco y aplica la geometría calculada.
2. Recorta los campos según la plantilla y prepara contraste, tinta y fondo impreso.
3. Reconoce texto por línea con Paddle; reintenta con detección cuando el formato lo requiere.
4. Normaliza matrícula, número de bitácora, vuelo y componentes de fecha.
5. Analiza presencia de escritura en firmas, licencias y bloque de corrección.
6. Aplica validación por página y luego las reglas del libro completo.

La fecha combina evidencia de la palabra y sus posiciones. `month_evidence.py` conserva candidatos ambiguos; `month_retry.py` limita la relectura a recortes relevantes. La cercanía a la fecha actual ayuda a ordenar candidatos, pero no reemplaza la evidencia. La GUI usa geometría dinámica y no activa el motor separado de OCR por casillas.

Con **Fin de mes**, `read_day=False` omite los campos de día y guarda `dia_leido: false`; esa ejecución no admite indexado con día exacto. La política de representación del CSV se distingue de la lectura original.

Las firmas se clasifican por tinta presente, ausente o incierta, sin verificar identidad. Cuando hay suficientes páginas alineadas, `book_background.py` estima mediante mediana el fondo repetido del libro y separa la escritura variable. Si no hay evidencia suficiente, conserva el detector habitual.

Código: `app/core/pipeline.py`, `app/ocr/`, `app/vision/signature.py` y `book_background.py`.

### 3. Reglas de validación y revisión

| Regla | Comportamiento que debe conservarse |
|---|---|
| Libro | 50 páginas, una aeronave; los siete dígitos de `log_number` separan grupos terminados en `00..49` y `50..99`. Un número ilegible solo se deduce (`log_sequence.py`) cuando las páginas vecinas del PDF, del mismo libro, lo encierran sin hueco, el número no está ya en la ejecución y los dígitos leídos no lo contradicen. |
| Matrícula | Normalización y validación con `fleet.json`; el consenso por libro necesita páginas independientes. Con **Verificar matrículas**, el texto crudo de cada página se lee contra la flota (`fleet_match.py`) antes de votar: la lectura que señala un solo avión vota por él, y lo que no es ningún avión no vota ni aparta la página. Una lectura de otro avión a una sola cifra del consenso con respaldo (o del registro de libros) se corrige sin revisión, salvo que sea la matrícula de otro libro de la ejecución. Un empate con la flota se desempata con las lecturas del libro. `HP-1990WWP` y `HP-1522CMP` tienen normalización específica. |
| Fecha | No retrocede dentro del libro. Las anclas completan o corrigen componentes y conservan alternativas, fuente y confianza. Al final, un año a dos o más de la mayoría directa del libro, o el anterior encerrado entre páginas de la mayoría, vuelve al año del libro. |
| Antigüedad | Fuera de enero se revisan fechas anteriores al año de ejecución, salvo que dos bitácoras distintas del libro lean ese mismo año. En enero también se admite el año anterior. Si la ejecución tiene un año reciente con consenso amplio y la página, sin respaldo en su libro, lleva uno de los meses leídos con ese año, toma el año de la ejecución en vez de ir a revisión. Se conserva la fecha antigua en las alternativas. |
| Futuro | Una fecha manuscrita posterior a la ejecución es inválida. El día generado por una política de fin de mes se valida por mes. |
| Duplicados | `dup` se marca desde la segunda aparición del mismo número válido, sin comparar imágenes. |
| Estado | `OK`, `WARNING` o `ERROR` según los datos principales y su evidencia. Firmas y vuelo opcional no determinan por sí solos ese estado. |
| Revisión | `needs_review()` alimenta tanto `review` del CSV como el reparto de la entrega. Un `WARNING` no implica siempre revisión manual. |

La clasificación de firmas se hace por página. Mantenimiento (licencia de técnico o corrección escrita) requiere firma de piloto, firma de técnico y licencia de técnico. Vuelo requiere firma de piloto y firma y licencia de capitán. La firma de técnico no determina el tipo porque su zona puede recibir sellos ajenos. `disc` y `disc_reason` describen faltas confirmadas; una lectura incierta no equivale a ausencia confirmada.

Antes de confirmar posibles discrepancias, `void_mark.py` busca una marca VOID en regiones propuestas de toda la página. Exige dos lecturas compatibles, limita regiones y variantes y admite cancelación. Una marca confirmada anula el reclamo de firmas, conservando los controles de identidad y fecha. Si falta su modelo portable, registra el motivo y mantiene la discrepancia. Puede omitir marcas de trazo fino, letras muy separadas u orientación difícil; no debe interpretarse la ausencia de detección como prueba de que la página no es VOID. Medido sobre una muestra de 12 páginas etiquetadas a mano (`output/estudio_revision/muestra_void.json`): reconoce 2 de 5 marcas reales y no inventa ninguna en las 7 restantes. Las que omite son marcas enormes y cursivas que el reconocedor no lee ni engordando el trazo, ni con el modelo de manuscrito, ni ampliando los topes de tamaño; el detector de texto tampoco las propone como texto a ninguna escala. Subir ese recall pide un clasificador de la marca entrenado con páginas etiquetadas, no otro ajuste del reconocedor.

Esa búsqueda es una segunda vuelta por documento y la etapa más cara medida: cada hoja pendiente manda hasta seis zonas por seis recortes al reconocedor `PP-OCRv6_medium_rec`. Medido en un equipo de 12 núcleos: unos 10,6 s por hoja en el proceso que la pide y 4,6 s repartiéndola entre los procesos del pool, que es lo que hace cuando el documento se repartió por páginas y el modelo cabe en la memoria libre de todos. Pedirle menos hilos al reconocedor no cambia su tiempo, así que el reparto es por hojas. El avance la lleva en un canal propio (`VOID_STAGE`): cuenta hojas, no páginas, y la barra y el tiempo restante le reservan su parte para no llenarse con la etapa pendiente.

`book_matriculas.json` y `book_fechas.json` conservan anclas entre ejecuciones. El plan de AirVault las contrasta con páginas remotas válidas; reemplazarlas exige respaldo coherente de dos bitácoras distintas. Con `buscar_publicadas` activado, una ronda posterior consulta libros antiguos en Web Search y guarda su turno en `book_ronda.json`.

Código: `app/validation/`, `app/utils/date_window.py`, `app/vision/void_mark.py` y `app/airvault/memoria.py`.

### 4. Paralelismo y cancelación

`QThread` mantiene la interfaz activa; procesos persistentes distribuyen el OCR pesado. `app/core/parallelism.py` calcula procesos e hilos según CPU y memoria. Los resultados se ordenan antes de escribirlos. La cancelación se propaga a los trabajos y corta la cadena automática; lo ya escrito puede permanecer, pero una ejecución OCR cancelada no es una entrega completa.

### 5. Reportes, depuración y exportación

`write_outputs()` guarda los CSV, el JSON y las estadísticas. La exportación compone PDF copiando páginas originales, sin rasterizarlas otra vez, y agrega separadores. En modo único genera `_paginas.json` con correspondencia de páginas, separadores y causas de revisión.

```text
output/
  BITS DD MON YYYY HH MM/
    datos/
      <ejecución>.CSV
      <ejecución>_completo.CSV
      <ejecución>.json
      <ejecución>_paginas.json
    stats.json
    <PDF de entrega>
  airvault/
  logs/
```

El CSV principal usa las columnas importantes; el completo añade evidencia de campos. El JSON conserva alternativas y procedencia. Cambiar la fecha de representación puede reescribir el CSV sin OCR, sujeto a que se haya leído el día.

Depurar modifica el modelo de resultados y vuelve a guardar los datos. El diálogo permite elegir apariciones duplicadas y blancos; conserva una aparición por número y evita vaciar la ejecución. El visor permite además retirar páginas seleccionadas. Los originales permanecen disponibles. Hay que reexportar los PDF después; las copias previas se conservan con numeración para las nuevas.

Código: `app/reports/outputs.py`, `csv_reporter.py`, `json_reporter.py`, `organize.py`, `stats.py` y `app/validation/depuracion.py`.

### 6. Automatización y AirVault

`app/gui/automatizacion.py` comparte y persiste las opciones del botón **Automático** y la ventana de AirVault. Preprocesar, procesar y exportar siempre se ejecutan. Subir, indexar y completar son etapas opcionales dependientes. La espera de publicación pertenece a la subida; depurar queda fuera de la cadena.

| Etapa | Operación interna |
|---|---|
| Sesión | Edge obtiene la autenticación federada y conserva el perfil en `portable/edge-airvault/`. Python reutiliza cookies y tokens antifalsificación. |
| Preparar | Relaciona CSV, PDF e índice de páginas; divide las cargas y separa revisión. La compresión opcional crea copias a 200 DPI. |
| Subir | Envía un PDF por vez a Quick Upload. Registra la aceptación antes de buscar el batch remoto. |
| Revisar | Identifica la carga por nombre, cantidad y contenido. Una aceptación sin descubrimiento no autoriza reenvío automático. |
| Planear | Mapea páginas y campos; comprueba obligatorios, duplicados, valores remotos y matrícula del libro. Una diferencia de cantidad bloquea el batch. |
| Indexar | Escribe las páginas permitidas, relee los valores y actualiza el manifiesto. Conserva páginas válidas, omite conflictos y retira separadores del flujo normal. |
| Completar | Completa solo batches válidos para Web Search. **REVISAR** conserva separadores y no se publica automáticamente. |

Si una página remota válida asigna otra aeronave al mismo libro, la escritura contradictoria se bloquea. Si AirVault contiene matrículas incompatibles para ese libro, no se toma un consenso remoto.

En **REVISAR** se envían los campos disponibles y se omiten obligatorios sin lectura; no se mandan explícitamente vacíos para forzar el guardado. Se confirma cada página por relectura. Una incidencia puede mantener `Need Correction` aunque el guardado haya terminado; una página completa sin incidencia independiente puede quedar `Valid`. Si el servidor rechaza campos omitidos, el fallo sigue pendiente. La aceptación depende de la configuración real de AirVault y debe comprobarse en una carga operativa.

Las causas viajan en el índice JSON y el manifiesto. Para faltas confirmadas se usa `AUDIT IN PROGRESS` y se selecciona un solo `ECN Reason`, respetando uno existente. La prioridad es capitán, piloto y técnico; para técnico, firma antes que licencia. No se escriben el segundo ni el tercer ECN Reason. El catálogo y la conversión están en `app/airvault/ecn.py`.

El manifiesto permite reanudar sin repetir guardados verificados. Reiniciar un paso modifica seguimiento local; eliminar un batch es una operación remota distinta. Las bajas conservan estado local para que la cola no reconstruya automáticamente lo eliminado.

Código: `app/airvault/flujo.py`, `session.py`, `navegador.py`, `uploader.py`, `discovery.py`, `mapping.py`, `guards.py`, `indexer.py`, `manifest.py` y `registro.py`.

### 7. Web Reports: consulta y corrección de excepciones

Log Page Audit es un informe de SSRS, no una API. Se conduce su visor por pantalla con el mismo Edge del perfil `portable/edge-airvault/`. Se entra por el enlace federado de `url_sso`: el enlace del informe lleva a la pantalla de acceso local de AirVault, que pide unas credenciales que en una instalación federada con Entra ID nadie tiene. Esa entrada renueva la sesión sin intervención mientras la sesión de Entra ID siga viva; cuando también ha caducado hace falta un acceso interactivo.

El plan sale entero del reporte y no consulta nada: una mal indexada ya trae las dos matrículas y una duplicada, cuántas copias hay. Lo que el reporte no diga con esas palabras queda como caso a revisar, con el motivo escrito.

| Acción | Operación interna |
|---|---|
| Consultar | Ejecuta cada filtro en la misma sesión y analiza las filas del visor. No escribe nada. |
| Borrar copias | Conserva la aparición más antigua por fecha y borra el resto con `onDeletePage`. Sin una fecha legible en todas, no borra ninguna. |
| Reindexar | Abre `onReindexDocument` y escribe matrícula y flota. Cambiar de aeronave puede cambiar la flota, y conservar la anterior sustituiría un dato malo por otro. |

Tres reglas gobiernan la escritura. Cada caso se contrasta antes con lo que la pantalla muestra: el reporte se generó en su momento y actuar sobre un plan viejo borraría lo que ya estaba bien. De un grupo de copias se conserva la más antigua, y sin columna de fecha legible no se borra ninguna. Un control que no aparece detiene ese caso, no la corrida: se busca por lo que el control dice, no por identificadores copiados de una instalación.

Se conduce por pantalla y no por peticiones sueltas a propósito: así valen los permisos de la cuenta y las validaciones del repositorio, y una cuenta sin permiso para borrar no encuentra el botón. Después de escribir se recarga la búsqueda y se relee: una orden pulsada que no surtió efecto no se da por hecha. Cada caso trabaja en su pestaña y la cierra; si el cuadro se abre y algo falla a mitad, se cancela para no dejar el documento tomado. Guardar un reindexado puede pedir confirmación, que llega después de la respuesta del servidor y se contesta mientras se espera el cierre.

Los resultados se informan uno a uno: la corrida termina con cuántas se corrigieron, cuántas no y el motivo de cada una. Los motivos que devuelve AirVault se copian tal cual, porque dicen más que cualquier frase propia.

Código: `app/airvault/web_reports.py`, `correcciones.py` y `app/gui/web_reports_window.py`.

## Visor y editor

El visor usa un modelo Qt (`csv_model.py`) para cargar y ordenar tablas grandes. La selección y búsqueda resuelven la página original mediante los datos de la ejecución. No edita celdas directamente; las acciones de depuración y exportación utilizan el modelo de resultados. Código: `app/gui/csv_viewer.py` y `csv_utils.py`.

El editor combina `QGraphicsScene` con renderizado de PyMuPDF. Guarda zonas en coordenadas relativas `x`, `y`, `w`, `h` entre 0 y 1. Cada campo incluye identificador, tipo, obligatoriedad, formato, postproceso y umbrales; Pydantic valida el JSON. Al guardar conserva propiedades adicionales de cada campo cargado. Revise también la referencia canónica y los metadatos generales de una plantilla nueva antes de sustituir la de producción. Código: `app/gui/editor_window.py` y `app/templates/`.

## Configuración y diagnóstico

| Ubicación | Contenido |
|---|---|
| `template/` | Plantillas y referencias del formulario. |
| `fleet.json` | Matrículas válidas para OCR. |
| `important_fields.json` | Columnas importantes por plantilla. |
| `airvault.json` | Configuración y preferencias de AirVault; ejemplo en `airvault.example.json`. |
| `airvault_flota.json` | Correspondencias de aeronave, flota y arrendador. |
| `book_*.json` | Memorias de libros y turno de comprobación. |
| `output/airvault/` | Manifiestos y seguimiento de cargas. |
| `output/logs/` | Registros de la GUI; otras ejecuciones pueden guardar registros propios. |
| `portable/paddlex/official_models/` | Modelos locales. |

Para investigar una página, conserve original, plantilla, JSON completo, índice de entrega y manifiesto. Compare su lectura cruda, alternativas, fuente, motivo de revisión y valores remotos antes de cambiar umbrales. El CSV por sí solo no contiene toda la evidencia.

Ejecute desde la raíz del proyecto:

```powershell
.\portable\python312\tools\python.exe run_cli.py --help
.\portable\python312\tools\python.exe run_airvault.py --help
.\portable\python312\tools\python.exe run_editor.py
```

`run_airvault.py plan` prepara el plan; `indexar --revisar` permite revisarlo antes de escribir. `todo` descubre, planea, indexa y verifica un batch ya cargado: no realiza la subida. `memoria` informa diferencias y solo aplica cambios con `--aplicar`. `--sobrescribir` reemplaza datos remotos válidos y requiere una decisión explícita del operador.

## Reconstrucción y comprobación

La preparación del paquete se hace en un equipo con red; el uso posterior del OCR es portable y sin conexión.

```powershell
.\setup.cmd
powershell -ExecutionPolicy Bypass -File setup.ps1 -Check
powershell -ExecutionPolicy Bypass -File setup.ps1 -Launcher
.\portable\python312\tools\python.exe tools/precache_paddle.py
.\portable\python312\tools\python.exe -m pytest tests -q
```

`setup.ps1 -Force` reconstruye intérprete y modelos; úselo sobre una copia controlada. Después de cambiar modelos, precárguelos en `portable/` y compruebe su uso con la red bloqueada.

Para agregar dependencias o reparar faltantes basta volver a ejecutar `setup.cmd`. El setup resuelve siempre `requirements.txt`, incluidos extras nuevos, sin reinstalar los paquetes ya satisfechos. `tools/check_portable.py` comprueba versiones, importaciones de las dependencias directas y `pip check`; si un paquete figura instalado pero no se puede importar, lo reinstala conservando su versión. También repone pip con `ensurepip` si falta. Un fallo de instalación o una comprobación final fallida termina con error, sin anunciar éxito.

Para cada uno de los tres modelos exige `inference.json`, `inference.pdiparams` e `inference.yml`, presentes y no vacíos. Si falta alguno, elimina únicamente la carpeta incompleta dentro de `portable/` y vuelve a ejecutar la precarga; los modelos completos se conservan. La precarga realiza inferencia en CPU. Esta comprobación de archivos detecta ausencias y archivos vacíos, no toda corrupción posible de pesos: en ese caso corresponde reconstruir con `-Force`. `-Check` solo comprueba, no descarga ni repara. Las pruebas de reparación están en `tests/test_setup_portable.py`.

Para liberar cambios: ejecute las pruebas pertinentes, procese una muestra por GUI y consola, compare CSV/JSON/PDF y pruebe la carpeta copiada a otra ubicación sin administrador. Las pruebas de AirVault con clientes simulados no prueban la aceptación real del servidor. No distribuya sesiones personales del perfil `portable/edge-airvault/`.

`tools/estudiar_revision.py` permite reconstruir causas desde ejecuciones guardadas sin repetir OCR; sus cifras describen la clasificación, no la exactitud del reconocimiento. Para evaluar VOID o firmas use muestras visuales etiquetadas y mida también falsos positivos y tiempo por página.
