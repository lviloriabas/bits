# Auditoría de cambios de septiembre de 2026

Base revisada: `780744b`, en `main`. Se compararon también `336ffac`, `8a6dfce`, `39fb720` y `cae396e` para separar el coste de ajuste geométrico del segundo reconocimiento de VOID. La revisión usa modelos existentes en `portable/`, siempre en CPU.

## Evidencia de rendimiento

`tools/benchmark_void.py` ejecuta ambas configuraciones sobre los mismos recortes: anterior con cuatro hilos solicitados a PaddleOCR, optimizada con oneDNN y un hilo aplicado al predictor. La carga de modelos se mide por separado. Sobre `output/estudio_revision/muestra_void.json`, 12 páginas:

| Medida | Anterior | Optimizada |
|---|---:|---:|
| Tiempo de la etapa VOID | 124,265 s | 31,946 s |
| Decisiones iguales | 12 de 12 | 12 de 12 |
| Marcas reales detectadas | 2 de 5 | 2 de 5 |
| Falsos positivos | 0 de 7 | 0 de 7 |

Reducción medida: 74,3%. No se atribuye esa reducción al lote completo. Se conservan las limitaciones previas de detección de VOID. Evidencia local: `output/auditoria_2026-09-19/void.json`.

En una página real que recorre también VOID, el procesamiento completo con modelos precargados pasó de 14,928 s a 7,255 s. Es una medición de una página, no una predicción para todos los lotes. Evidencia: `output/auditoria_2026-09-19/procesamiento_void.json`. En tres páginas sin esa etapa no se observó mejora del tiempo total.

Reproducción desde la raíz, con los PDF locales de la muestra:

```powershell
.\portable\python312\tools\python.exe tools/benchmark_void.py output/estudio_revision/muestra_void.json --salida output/auditoria_void.json
```

## Correspondencia entre solicitudes y cambios

| Solicitud | Implementación | Verificación |
|---|---|---|
| Recuperar velocidad | `app/ocr/engine.py`, `app/vision/void_mark.py` | Comparación real de 12 páginas y pruebas de retorno al mismo modelo si oneDNN falla |
| Texto de subida y nombre de editor | Ventanas principal, editor y AirVault | Subida secuencial; «Editor de plantilla» |
| Campos importantes en visor CSV | `CsvViewerWindow.refresh_important_columns` | Actualización del visor abierto al cambiar la selección principal |
| Elegir discrepancias, sin editor de tipos | `Template.discrepancy_fields`, menú «Discrepancias a detectar» | Lista vacía, selección parcial y comportamiento anterior con null |
| Azul hasta completar | `_pintar_lotes` | INDEXADO e INCOMPLETO azules; COMPLETADO y AUTOCOMPLETADO verdes; cambio de tema |
| Licencias acusadas incorrectamente | `review_with_background(..., allow_absent=False)` en licencias | 265 recortes etiquetados con licencia escrita: ninguna falsa ausencia del detector base. Caso `rev1_p0157_technician_license`: la segunda opinión antes lo convertía en ausente; ahora conserva la duda |
| ECN Reason | `app/airvault/ecn.py` | Bloque superior: TECHNICIAN SIGNATURE; firma/licencia del capitán: razón de PILOT. Identificadores de campos y columnas conservados |
| Previews locales | `DepurarPaginasDialog` | Imagen elegida y otra copia; selección modificable antes de aplicar, blancos incluidos |
| Previews Web Reports | `RevisionCopiasDialog`, `CorreccionWorker` | Revisión opcional por grupo, omitir, cambiar copia conservada, bloqueo si no carga imagen |
| Proteger documentos de varias imágenes | `CorrectorLogPageAudit` | Bloqueo por ImageCount desconocido o distinto de 1, tipo distinto de LOG PAGE, claves repetidas o cambios posteriores |
| Mensajes inferiores cortos | `ElidedLabel` | Una línea y texto completo al pasar el puntero |
| Autocompletar y no repetir lo confirmado | Flujo e indexador existentes | Pruebas de reanudación, verificación y completado; no se debilita la exigencia de páginas válidas |
| Detener o avisar de duplicados | Casilla persistente `detener_por_duplicados`, desactivada inicialmente | Desmarcada conserva el aviso y permite subir y completar; marcada bloquea. Se verifica guardar, cerrar y volver a abrir |
| Seguir hasta completar los batches | `_falta_esperar` y señal de «Completar batch» | Los incompletos siguen en vigilancia; los indexados continúan cuando falta completar y la opción está activa; los completados salen de la espera |
| Volver a subir tras revisar duplicados | Acción existente «No es duplicado: volver a subir» | Reenvío explícito después de revisar el conflicto; no se habilita reenvío automático por cantidad |

La integración con otro servidor y la limpieza por fechas quedaron fuera por indicación posterior del usuario.

## Comprobación de AirVault

Se entró por SSO en Edge, siguiendo el enlace usado por el software. Web Search mostró `ImageCount` bajo «Images» y `DocType` bajo el tipo documental. «View Document» abrió el visor con una página; sus imágenes se cargaron correctamente con `Document/GetHiglightedPage` y formato PNG. La vista previa utiliza esa misma imagen autenticada, reducida en memoria. No se realizaron borrados ni modificaciones reales para probar el cambio.

La protección se comprueba al preparar el grupo, después de revisar las imágenes y al abrir la operación de eliminación. Una copia que deba conservarse tiene que seguir presente. El estado final se vuelve a leer antes de dar la corrección por hecha. Una respuesta ambigua no cuenta como éxito.

## Registros de auditoría

Cada exportación conserva en `logs/` la plantilla serializada con SHA-256 y un registro `exportaciones.jsonl` de inicio y final. Incluye selección de discrepancias, columnas importantes, DPI y política de fecha. Una entrada de inicio sin final no prueba que la exportación terminara. El CSV mantiene sus columnas.

La limpieza remota escribe `output/airvault/correcciones_auditoria.jsonl`: fecha con zona horaria, bitácora, clave documental, cantidad de imágenes, tipo y matrícula. Registra la solicitud de borrado antes de enviarla y el resultado verificado al concluir. No incluye cookies ni credenciales. Son registros locales para reconstruir lo ocurrido, no un sistema que impida su modificación manual.

## Validación antes de despliegue

El 20 de septiembre de 2026, `portable/python312/tools/python.exe -m pytest -q` terminó con 2198 pruebas y 16 subtests aprobados, en 92,78 s. `tools/check_portable.py --requirements requirements.txt --check` no encontró dependencias rotas. `git diff --check` terminó sin errores.

Se revisaron visualmente los previews locales y remotos con imágenes reales, sin efectuar borrados. La página usada para comparar el procesamiento completo conservó los mismos valores de campos. La validación remota fue de lectura; las subidas, eliminaciones y transiciones automáticas se comprobaron con pruebas simuladas, no con operaciones destructivas sobre producción.
