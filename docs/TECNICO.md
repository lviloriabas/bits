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

Esa búsqueda es una segunda vuelta por documento: cada hoja pendiente manda hasta seis zonas por seis recortes al reconocedor `PP-OCRv6_medium_rec`. El motor de reconocimiento configura explícitamente `engine_config={"run_mode": "mkldnn", "cpu_threads": 1}`; pasar solo `cpu_threads` a PaddleOCR no conseguía ese ajuste. El detector conserva su configuración anterior por la incompatibilidad de su ruta oneDNN en Windows. Si falla la carga o inferencia acelerada, se repite con el mismo modelo en CPU sin oneDNN. No se cambian recortes, modelo ni umbrales. En las 12 páginas etiquetadas, la etapa pasa de 124,265 a 31,946 segundos y conserva las 12 decisiones, incluidas las tres marcas reales que ambas rutas omiten. Esta medida no representa el tiempo total de un lote. Se reproduce con `tools/benchmark_void.py` y se detalla en [la auditoría de cambios](AUDITORIA_CAMBIOS_2026-09.md).

El modelo sigue reutilizándose por proceso y la etapa puede repartirse en el pool si hay memoria. El avance usa `VOID_STAGE`: cuenta hojas y reserva tiempo para esta revisión. La segunda opinión por fondo del libro solo mide campos con dudas pendientes. Para `captain_license` y `technician_license` puede confirmar escritura, pero no convertir una lectura incierta en ausencia por haber restado la tinta tenue del número.

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

El cronómetro de la ventana es el mismo widget de la principal (`app/gui/cronometro.py`), pero contando otra cosa: aquí la unidad es el reporte en la consulta y la bitácora en la corrección, y abrir una búsqueda en Web Search no tiene unidades porque es apertura y nada más. Cada trabajo se descompone en apertura (levantar Edge y rehacer la sesión, que no depende de cuánto haya por hacer) más tantas unidades como piezas tenga. Los dos backends cuentan lo que van terminando por un callback `progreso(hechas, total)` separado del de las frases de estado, y el total lo manda quien hace el trabajo: el plan de la ventana incluye filas de revisión manual que nunca tocan el navegador. Lo medido en cada corrida completa se guarda por tarea en `output/.web_reports.json` y es con lo que se estima la siguiente; una cancelada o fallida no escribe nada, porque dejó fuera lo que faltaba. El reparto entre el histórico y el ritmo observado es el de `app/gui/eta.py`, con un calentamiento de tres unidades en vez de veinte: una bitácora entera da mucha más información que una página, y casi ninguna corrida llega a veinte.

Código: `app/airvault/web_reports.py`, `correcciones.py`, `app/gui/web_reports_window.py` y `app/gui/web_reports_tiempos.py`.

## Visor y editor

El visor usa un modelo Qt (`csv_model.py`) para cargar y ordenar tablas grandes. La selección y búsqueda resuelven la página original mediante los datos de la ejecución. No edita celdas directamente; las acciones de depuración y exportación utilizan el modelo de resultados. Código: `app/gui/csv_viewer.py` y `csv_utils.py`.

El editor combina `QGraphicsScene` con renderizado de PyMuPDF. Guarda zonas en coordenadas relativas `x`, `y`, `w`, `h` entre 0 y 1. Cada campo incluye identificador, tipo, obligatoriedad, formato, postproceso y umbrales; Pydantic valida el JSON. Al guardar conserva propiedades adicionales de cada campo cargado. Revise también la referencia canónica y los metadatos generales de una plantilla nueva antes de sustituir la de producción. Código: `app/gui/editor_window.py` y `app/templates/`.

## Tema claro y oscuro

`app/gui/tokens.py` define dos paletas (`OSCURA` y `CLARA`) con los mismos papeles: superficies de la más honda a la más cercana, textos, estados y los nombres `PANE_*` y `TABLE_*` que usan las hojas. Ningún color se importa suelto; se pide con `paleta()` en el momento de pintar, porque una constante de módulo se copia en quien la importa y se queda con el tema del arranque.

`app/gui/theme.py` instala y cambia el tema. Los colores llegan a la pantalla por varios caminos y cada uno se actualiza al cambiar el tema:

| Camino | Quién lo rehace |
|---|---|
| Paleta de Qt y hoja de la aplicación | `aplicar_tema()`, que las vuelve a poner. |
| Hoja propia de cada ventana (lleva su fragmento de densidad) | La ventana, suscrita a `gestor_tema().cambiado`. |
| Hoja de una línea de un rótulo y paletas fijadas a mano (tabla, panel del visor) | `repintar_del_tema()`, sobre lo anotado con `pintar_del_tema` / `al_cambiar_tema`. |
| Colores de estado de las celdas de AirVault y sus vistas previas | `pintar_celda_del_tema()` guarda el papel en la celda; `repintar_del_tema()` actualiza el color sin recrear las filas ni perder la selección o el orden. |
| Barra de título, que dibuja DWM y no Qt | `set_windows_native_window_style(ventana, oscuro)`. |

Los iconos SVG llevan el color dentro del dibujo, así que ninguna hoja los alcanza: los de los botones se tiñen al cargarlos (`load_icon`) y la flecha de los desplegables, que QSS pide con `image: url(...)`, viaja en dos archivos y la hoja elige el del tema.

La transición suspende tanto la pintura como los layouts habilitados. QSS
retira y repone fuentes y márgenes al sustituir una hoja; si el layout mide
ese estado intermedio, algunos botones saltan y regresan después de pintar.
Los layouts se reactivan y calculan con el tema completo antes de volver a
pintar. Los que ya estaban deshabilitados conservan su estado. No se usa
`processEvents()`.

Antes de sustituir la hoja global se invalida el estilo de la aplicación y
después se repule cada widget una vez. Esto evita que el repulido global de
Qt recorra repetidamente los descendientes de cada control cacheado. El
esquema nativo y los títulos cambian al final, seguidos del repintado inmediato
del contenido. Solo se actualizan los marcos de ventanas visibles: pedir
`winId()` para menús o diálogos ocultos creaba ventanas nativas innecesarias.

El tema elegido se guarda en `interfaz.json` (`app/utils/preferencias_ui.py`), junto al programa y no en el registro de Windows, para que viaje con la copia portable.

### Memoria de las opciones

Cada casilla, desplegable o contador que alguien mueve queda anotado en ese mismo `interfaz.json`. Lo ata `app/gui/memoria.py` con una sola llamada al lado del control: `recordar(seccion, nombre, control)` lo deja en lo último guardado y conecta su señal para anotar cada cambio. El valor de partida es el que el control trae escrito en el código, así que quien nunca tocó una opción la encuentra como estaba y no se escribe nada hasta que la mueva. Restaurar se hace con la señal bloqueada: abrir una ventana no puede disparar lo que el control hace al moverse.

Las claves llevan delante la ventana (`principal`, `salida`, `airvault`, `visor`, `web_reports`) para que dos ventanas con la misma casilla no se pisen. Los desplegables se guardan por el texto de la opción elegida y no por su posición, que cambia en cuanto se agrega o reordena una opción.

Las opciones del indexado que ya tenían memoria (`Completar batch`, los pasos del proceso automático, la política de duplicados, las páginas por batch y la política de fecha del CSV) siguen en `airvault.json`: mismo tipo de archivo local, y moverlas ahora le borraría a cada instalación lo que ya tiene elegido.

Nada de esto se versiona. Los dos archivos se reescriben solos en cuanto alguien toca un control, así que tenerlos en el repositorio convertía cada casilla marcada en una modificación pendiente y bloqueaba el `git pull` en la otra máquina. Por el mismo motivo, la selección de «Discrepancias a detectar» dejó de escribirse dentro del JSON de la plantilla (que sí se versiona) y pasó a `interfaz.json`, con una entrada por plantilla; el archivo de la plantilla conserva el valor de partida y `con_discrepancias_elegidas` (`app/validation/discrepancias.py`) le pone lo elegido al cargarla para procesar, tanto desde la interfaz como desde `run_cli.py`.

## Configuración y diagnóstico

| Ubicación | Contenido |
|---|---|
| `template/` | Plantillas y referencias del formulario. |
| `fleet.json` | Matrículas válidas para OCR. |
| `important_fields.json` | Columnas importantes por plantilla. |
| `interfaz.json` | Preferencias de la interfaz: tema, casillas de salida, filtros de cada ventana, plantilla elegida y discrepancias a detectar por plantilla. No se versiona. |
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
