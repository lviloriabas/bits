# BITS: manual de uso

BITS lee bitácoras escaneadas, organiza los resultados y prepara su carga en AirVault. Una **ejecución** es el conjunto de PDF procesados juntos y sus resultados. Una **plantilla** indica dónde buscar cada dato del formulario. El **OCR** convierte la escritura de la imagen en texto.

Abra `BITS.exe` desde la carpeta completa del programa. La lectura funciona localmente, sin internet y usando el procesador. Para AirVault necesita conexión, Microsoft Edge y una cuenta autorizada. Consulte la [guía técnica](TECNICO.md) para mantenimiento.

## Proceso paso por paso

### 1. Seleccionar los archivos y la plantilla

1. Pulse **Seleccionar…**, junto a **Archivos**, y elija uno o varios PDF. Si los copió a `input/`, use **Carpetas -> Detectar input**.
2. Compruebe los archivos y la cantidad de páginas indicada. La interfaz procesa todos los PDF seleccionados; el cuadro **Página** sirve para navegar, no para limitar el procesamiento.
3. Elija la **Plantilla** correspondiente al formulario. Para el formato habitual, use `aircraft_log`. **Buscar…** permite abrir otra plantilla.
4. Mantenga **Verificar matrículas** activado para contrastar las lecturas con la flota. **Editar flota…** permite actualizar esa lista.

No mueva ni modifique los originales durante el trabajo. Al terminar correctamente, los PDF tomados directamente de `input/` pasan a `input/processed/`; los elegidos fuera de esa carpeta permanecen en su ubicación.

### 2. Elegir la salida

Configure el recuadro **Salida** antes de procesar:

| Opción                                   | Para qué sirve                                                                                                 |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| **Formato: Un solo PDF**                 | Reúne la entrega con sus secciones. Es el formato necesario para AirVault, aunque después se divida en partes. |
| **Formato: Varios PDF**                  | Crea archivos separados según las agrupaciones elegidas.                                                       |
| **Separación -> Matrícula / Mes**        | Agrupa por aeronave, por mes o por ambos.                                                                      |
| **Separación -> Posibles discrepancias** | Añade un apartado para revisar posibles faltas de firmas o licencias.                                          |
| **Separación -> Errores**                | Genera `errores.pdf` con páginas que requieren revisión manual.                                                |
| **Dividir cada**                         | Divide la entrega única según el máximo de páginas indicado, contando separadores y respetando las secciones.  |
| **Fecha: Fin de mes**                    | Escribe el último día del mes reconocido. Si procesa así, BITS omite la lectura del día manuscrito.            |
| **Fecha: Día exacto**                    | Lee el día y lo usa en el CSV; si falta, representa la fecha a fin de mes cuando dispone de mes y año.         |

Una ejecución que leyó el día puede exportarse o indexarse a fin de mes sin repetir OCR. Para recuperar el día manuscrito de una ejecución procesada con **Fin de mes**, debe procesarla otra vez con **Día exacto**.

### 3. Preprocesar y comprobar las zonas

**Preprocesar** prepara la imagen para leerla: corrige la inclinación y alinea el formulario con la plantilla. Sirve para comprobar que las zonas de lectura caen sobre los datos correctos. Esta acción no reconoce texto ni genera la entrega.

1. Abra **Campos** y marque **Visualizar campos**.
2. Pulse **Más acciones -> Preprocesar** y espere a que termine.
3. Recorra varias páginas con las flechas o escribiendo el número en **Página**. Compruebe especialmente matrícula, número de bitácora y fecha.
4. Use los controles de acercar, alejar y ajustar la página para inspeccionar los recuadros.
5. Si las zonas quedan desplazadas, revise la plantilla antes de procesar.

**Campos -> Columnas importantes** limita los recuadros a los campos elegidos. **Elegir columnas importantes…** permite cambiar esa selección, que también se usa en las tablas y el CSV principal. Los recuadros son ayudas de pantalla y no aparecen en los PDF exportados.

### 4. Procesar

**Procesar** realiza la lectura completa: prepara las imágenes, reconoce los datos, comprueba fechas y matrículas, detecta duplicados y examina las firmas. Guarda los resultados para revisarlos y exportarlos.

1. Confirme archivos, plantilla y opciones de salida.
2. Pulse **Procesar**. Incluye el preprocesado; no es obligatorio ejecutarlo antes por separado.
3. Espere a **Procesamiento terminado**. La pantalla muestra el avance, los tiempos y los resultados.
   Cada documento hace una segunda vuelta sobre las hojas a las que les falta una firma, buscando en la imagen una marca **VOID** que anule ese reclamo. La barra la anuncia como *Comprobando marcas VOID en hojas sin firma*, cuenta hojas (no páginas) y está incluida en el tiempo estimado.
4. Revise los datos antes de crear la entrega con **Exportar**.

La aplicación ajusta internamente la alineación, los recortes y el uso del procesador. Para interrumpirla, pulse **Cancelar** y espere a que se detenga; una ejecución cancelada no es una entrega final.

### 5. Revisar los resultados

Haga doble clic en una fila para localizar su página. También puede buscar un número de bitácora, matrícula u otro texto y recorrer las coincidencias.

| Dato o estado | Qué significa                                                                                                   |
| ------------- | --------------------------------------------------------------------------------------------------------------- |
| `OK`          | Los datos principales se leyeron sin dudas pendientes.                                                          |
| `WARNING`     | Hay una lectura débil, un dato inferido o algo por confirmar.                                                   |
| `ERROR`       | La página está en blanco o no se pudieron resolver los datos principales.                                       |
| `review`      | La página se separa en **REVISAR**. El sistema puede guardar los datos disponibles, pero queda revisión humana. |
| `dup`         | El número de bitácora ya apareció en la ejecución.                                                              |
| `disc`        | Se confirmó una falta de firma o licencia requerida.                                                            |
| `disc_reason` | Explica la falta confirmada.                                                                                    |

Compruebe matrícula, número de bitácora y fecha. El número tiene siete dígitos. Cada libro contiene 50 páginas de una sola aeronave: los finales `00` a `49` pertenecen a un libro y `50` a `99` al siguiente. Dentro del libro, la fecha puede repetirse, pero no retroceder al aumentar el número.

Las fechas de años anteriores pasan a revisión, salvo durante enero, cuando también se admite el año anterior. Una fecha manuscrita futura necesita corrección; una fecha representada a fin de mes se comprueba por mes. La marca **VOID** confirmada elimina el reclamo de firmas de una página anulada, pero no resuelve datos de identidad o fecha faltantes.

### 6. Depurar

Depurar retira páginas de los resultados, como duplicados o blancos, después de que usted las revise.

1. Pulse **Más acciones -> Depurar**.
2. Elija **Duplicados**, **Páginas en blanco** o ambas opciones.
3. Seleccione una aparición para ver su miniatura y otra copia al lado. Las páginas en blanco también muestran su imagen. Marque o desmarque cada página con su casilla o con Enter. Las marcas se pueden cambiar; todavía no eliminan nada.
4. Pulse **Eliminar** para aplicar las marcas elegidas.
5. Exporte de nuevo para que la entrega refleje la depuración.

Se conserva al menos una aparición de cada bitácora repetida y no se permite vaciar toda la ejecución. Depurar es una acción manual y no forma parte del proceso automático.

### 7. Exportar

1. Revise las opciones de **Salida**.
2. Pulse **Más acciones -> Exportar**.
3. Abra la carpeta de la ejecución en `output/` y compruebe los PDF generados.

La carpeta contiene `datos/` con el CSV principal, el CSV completo y el JSON, además de `stats.json` y los PDF de entrega. Con **Un solo PDF** se genera también `_paginas.json`, que relaciona cada página exportada con sus datos para AirVault.

Exportar vuelve a generar los datos y PDF sin repetir OCR. Conserva los PDF anteriores y numera las nuevas copias con sufijos como `-2` o `-3`. Para reexportar necesita el JSON, la plantilla y los PDF originales.

### 8. Subir e indexar en AirVault

**Subir** envía los PDF. **Indexar** asigna a cada página sus datos de búsqueda: aeronave, número de bitácora, fecha y demás campos configurados. Un **batch** es un lote de páginas dentro de AirVault. **Completar** cierra un batch válido y lo envía a Web Search.

1. Exporte con **Un solo PDF** y conserve matrícula, número de bitácora y fecha en el CSV principal.
2. Pulse **Indexar en AirVault…**. Seleccione la ejecución en el historial: es el único sitio desde el que se elige lo que se sube, y lista las últimas 25 ejecuciones procesadas.
3. Revise **Nombre del batch**, **Máximo por batch** y **Fecha**. Deje **Sesión** vacío: BITS abre su propio Edge si hace falta. Complete el inicio de sesión y el segundo factor cuando se soliciten.
4. Para controlar cada etapa manualmente, desmarque **Indexar páginas** y **Completar batch** en **Automatización**.
5. Abra **Acciones -> Vista previa…**. Compruebe el reparto y use **Ver las bitácoras…** para revisar el contenido previsto. El recuadro **Resultado** resume cuántas van al flujo normal y cuántas a **REVISAR**.
6. Pulse **Subir a AirVault**. Espere a que los batches aparezcan confirmados. Si la revisión periódica está apagada, pulse **Revisar en AirVault**.
7. Revise las incidencias y pulse **Indexar** para escribir las páginas que estén listas. Los conflictos permanecen visibles.
8. Para publicar los batches normales, active **Completar batch** antes de indexar o use **Completar el batch** en el menú de una fila indexada. Solo se completa cuando todas sus páginas son válidas.
9. Revise el estado final y atienda por separado el batch **REVISAR**.

En **REVISAR**, BITS guarda los campos disponibles y verifica lo escrito. Las páginas con incidencias pueden seguir amarillas, pendientes de corrección humana, aunque termine el trabajo automático. Ese batch conserva sus separadores y no se completa ni publica automáticamente.

La cola muestra azul durante el indexado y mientras el batch esté indexado o incompleto. Solo muestra verde al completar. Una página que conserve el estado 3 no convierte el batch en completado. Los mensajes inferiores ocupan una línea; coloque el puntero encima para leer el texto completo.

Los PDF se suben de uno en uno. La cola identifica cada carga y sigue con los demás batches. Para entrar en AirVault, BITS usa el enlace SSO en Edge y reutiliza su sesión.

#### Opciones de AirVault

| Opción                                     | Función                                                                                                                 |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| **Máximo por batch**                       | Limita cada carga, incluidos sus separadores. Conserva los batches ya subidos.                                          |
| **Compresión**                             | Envía copias a 200 DPI; conserva los PDF exportados.                                                                    |
| **Fecha**                                  | Permite fin de mes o día exacto, si la ejecución leyó el día.                                                           |
| **Revisar cada**                           | Consulta la disponibilidad cada 1 a 60 minutos mientras la ventana sigue abierta. Apagado, use **Revisar en AirVault**. |
| **Automatización**                         | Elige hasta dónde continúa el trabajo sin nuevos clics.                                                                 |
| **Continuar pendiente**                    | Retoma desde el primer paso sin terminar y conserva lo confirmado.                                                      |
| **Reiniciar paso incompleto**              | Reinicia el estado local del batch elegido, o de los incompletos si no seleccionó uno. No borra datos remotos.          |
| **Buscar** y flechas                       | Localizan bitácoras y recorren las coincidencias.                                                                       |
| **Mostrar solo la ejecución seleccionada** | Limita la cola visible a esa ejecución.                                                                                 |
| **Cancelar**                               | Detiene el trabajo y libera los batches abiertos; conserva lo escrito.                                                  |
| **Detener subida si se detectan duplicados** | Marcada: bloquea el batch sospechoso. Desmarcada: muestra el aviso y continúa con la subida, el indexado y el completado solicitado. Se recuerda al cerrar; inicialmente está desmarcada. |

Con clic derecho sobre un batch puede subirlo, revisarlo, indexarlo, completarlo, cancelar o reanudar su cola, ver sus bitácoras y copiar su nombre o identificador. Solo se habilitan las acciones que corresponden a su estado.

**No es duplicado: volver a subir** autoriza un reenvío: úselo después de revisar en AirVault que la carga realmente falta. **Eliminar el batch…** actúa sobre el batch remoto; **Eliminar el registro de AirVault** quita el seguimiento local. **Eliminar la ejecución…**, en el historial, envía la carpeta local a la Papelera y conserva lo que ya está en AirVault.

La comprobación de duplicados sigue generando avisos con la casilla desmarcada. La alerta queda guardada en el manifiesto y visible en la cola. Esto no vuelve a subir automáticamente una carga ya aceptada: primero se identifica el batch existente.

Mientras **Revisar cada** esté activo, la cola sigue consultando los batches incompletos. Si **Completar batch** está marcado, también sigue pendiente de los indexados que todavía no se han completado. Los batches completados salen de la espera; **REVISAR** sigue reservado para corrección humana. Puede cancelar o desactivar la revisión periódica en cualquier momento.

### 9. Corregir excepciones con Web Reports

Log Page Audit es un reporte de AirVault que señala dos defectos de lo ya publicado: una bitácora **duplicada**, que aparece más de una vez, y una **mal indexada**, archivada bajo una aeronave que no es la de su libro. **Web Reports…**, en la ventana principal, consulta ese reporte y corrige lo que el propio reporte deja decidido.

1. Pulse **Web Reports…** y elija **Desde** y **Hasta**. En **Mostrar** decida si quiere mal indexadas, duplicadas o ambas.
2. Pulse **Consultar**. Consultar no modifica nada en AirVault.
3. Revise la tabla. **Matrícula del libro** es la aeronave que le corresponde a la bitácora; **Matrícula indexada**, aquella bajo la que quedó archivada. En una mal indexada las dos difieren, y esa diferencia es el defecto. En una duplicada la segunda queda vacía: el reporte no la indica.
4. Las celdas subrayadas abren Web Search: la de **Página**, sus apariciones; la de **Rango del libro**, el libro entero.
5. Pulse **Corregir todas…**, o seleccione filas con Ctrl o Mayús y pulse **Corregir seleccionadas…**. Confirme el resumen.

**Revisar imágenes antes de eliminar copias** abre una comparación para cada bitácora duplicada. La selección inicial conserva la más antigua. Puede cambiar las marcas, omitir la bitácora o pulsar **Eliminar seleccionadas**. Siempre debe conservar una copia. Las miniaturas se guardan en memoria mientras la ventana siga abierta, para reutilizarlas si el documento no cambió. Si desactiva la opción, la corrección conserva automáticamente la copia más antigua.

Solo se eliminan documentos de tipo **LOG PAGE** con **Images = 1**. Si cualquiera tiene varias imágenes, falta ese recuento o el tipo corresponde a otra área, se bloquea el grupo y se informa el motivo. Justo antes de eliminar se vuelve a comprobar que las copias coincidan con las revisadas y que la que debe conservarse siga presente. Esto protege documentos de Fleet u otras áreas incluidos en la búsqueda.

Una mal indexada se pasa a la aeronave de su libro. Antes de escribir, cada caso se contrasta con lo que Web Search muestra en ese momento: si el reporte quedó viejo y alguien ya lo corrigió, la bitácora se deja como está.

Mientras algo corre en Edge, el cronómetro junto a la barra de progreso dice lo mismo que el de la ventana principal: **Estimado**, **Restante** y **Transcurrido**. La cuenta va por bitácora, no por página, y se ajusta con lo que tardan de verdad las de esta corrida. La primera vez parte de una estimación de fábrica; a partir de ahí usa lo que costó la última consulta o corrección completa en este equipo. Una corrida cancelada no cuenta para eso.

Los casos se corrigen uno a uno y son independientes. Que uno falle no detiene los demás: al terminar, el resumen dice cuántas se corrigieron y cuántas no, y un aviso enumera cada bitácora que quedó sin cambiar con el motivo que dio AirVault. Los motivos habituales son que la página esté tomada por otro usuario y que Web Search ya no muestre lo que decía el reporte.

**Las copias borradas no se pueden recuperar desde BITS.**

## Proceso automático

1. Seleccione PDF y plantilla y configure **Salida**, como en los pasos 1 y 2.
2. Si va a usar AirVault, revise antes sus opciones de carga e inicio de sesión.
3. Abra la flecha de **Automático** y elija hasta dónde continuar.
4. Pulse el cuerpo del botón **Automático** para iniciar la cadena.
5. Siga la línea de pasos y revise el resultado final, especialmente **REVISAR**.

| Paso                 | Qué hace y si se puede elegir                                                                           |
| -------------------- | ------------------------------------------------------------------------------------------------------- |
| **Preprocesar**      | Prepara las imágenes. Siempre incluido.                                                                 |
| **Procesar**         | Lee y valida los datos. Siempre incluido.                                                               |
| **Exportar**         | Genera la entrega. Siempre incluido.                                                                    |
| **Subir a AirVault** | Opcional. Carga los batches y espera a que estén disponibles.                                           |
| **Indexar páginas**  | Opcional. Escribe cada batch listo y retira los separadores del flujo normal. Activa también la subida. |
| **Completar batch**  | Opcional. Completa los batches normales válidos. Activa los pasos anteriores de AirVault.               |

Si desactiva **Subir a AirVault**, la cadena termina al exportar. La espera forma parte de la subida y se muestra como **Esperar**. Las opciones se conservan al cerrar y se comparten con el menú **Automatización** de AirVault.

**Cancelar** detiene la cadena. Para una interrupción de AirVault, vuelva a abrir la ejecución y use **Continuar pendiente**. La depuración sigue siendo manual.

## Visor de CSV

Abra **Herramientas -> Visor de CSV…** para consultar una ejecución guardada sin volver a procesarla.

1. Elija una ejecución en **Historial**, o pulse **Seleccionar carpeta…** o **Seleccionar CSV…**.
2. Elija el **Archivo CSV**. Seleccione una fila para abrir su página original.
3. Busque texto con **Buscar** o Enter y recorra las coincidencias con las flechas.

| Control                                      | Qué permite                                                                                                                    |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Cabeceras de la tabla                        | Ordenar los resultados.                                                                                                        |
| Selector de columnas, cuando está disponible | Alternar entre columnas importantes y todas las del CSV completo. El botón contiguo permite elegir las importantes.            |
| **PDF de origen**                            | Cambiar de documento dentro de la ejecución.                                                                                   |
| **Página** y flechas                         | Ir a una página o recorrer el conjunto.                                                                                        |
| Acercar, alejar y ajustar                    | Inspeccionar la imagen o verla completa.                                                                                       |
| **Ubicar PDF…**                              | Localizar los originales si se movieron.                                                                                       |
| Casillas y **Supr**                          | Marcar páginas y quitarlas de la ejecución tras confirmar. La barra espaciadora alterna las marcas de las filas seleccionadas. |
| **Depurar**                                  | Revisar y retirar duplicados o blancos.                                                                                        |
| **Salida** y **Exportar**                    | Cambiar la organización y volver a generar la entrega sin OCR.                                                                 |

La tabla permite consultar datos; sus celdas no se editan directamente. Depurar, quitar páginas y exportar requieren los archivos asociados de la ejecución. Después de retirar páginas, vuelva a exportar.

Los cambios de **Campos importantes** de la ventana principal se reflejan también en el visor de CSV abierto, para la misma plantilla.

Cada exportación conserva en `logs/` su plantilla y las opciones aplicadas. Consulte [la auditoría de cambios](AUDITORIA_CAMBIOS_2026-09.md) para ubicar las evidencias de procesamiento y los registros de limpieza de AirVault.

## Discrepancias a detectar

En **Herramientas -> Discrepancias a detectar**, active los tipos de discrepancia que desea reclamar. Hay tres. Cada uno es una casilla que se marca al pulsarla, y la flecha de su derecha abre las casillas que ese tipo exige:

- **De vuelo:** firma de piloto, firma de capitán y licencia de capitán.
- **De mantenimiento:** firma de piloto (el bloque superior, que en la hoja firma el técnico), firma de técnico y licencia de técnico.
- **Del bloque de correcciones (mantenimiento):** las mismas tres casillas, pero reclamadas porque hay trabajo escrito en «CORRECTION OR DEFERRAL» aunque la licencia del técnico haya quedado en blanco. Apagarlo deja de reclamar firmas por trabajo descrito, sin tocar el resto del mantenimiento.

Cada tipo lleva su propia selección: apagar la firma de piloto en vuelo no la apaga en mantenimiento. Quitar el tipo entero apaga sus tres casillas; volver a marcarlo las devuelve todas.

La selección se guarda en la plantilla y se aplica al siguiente procesamiento. No cambia los campos que se leen para identificar el documento, ni cambia qué cuenta como discrepancia: solo decide cuáles se reportan.

También se puede editar en el JSON de la plantilla: `discrepancy_types: null` reclama todo; si no, cada clave (`vuelo`, `mantenimiento`, `correccion`) lleva la lista de campos activos, y una lista vacía apaga ese tipo.

Las páginas donde no se pudo determinar el tipo quedan fuera de este menú y se comportan igual que siempre. Las lecturas inciertas se distinguen de las ausencias confirmadas. Una licencia tenue no se considera ausente solamente porque desaparezca al compararla con el fondo del libro.

## Editor de plantilla

Abra **Herramientas -> Editor de plantilla…**. Una zona de plantilla es un rectángulo que indica dónde buscar un dato, por ejemplo matrícula, fecha o firma.

1. Pulse **Abrir PDF** (`Ctrl+O`) y elija un documento representativo.
2. Pulse **Cargar plantilla** para abrir la plantilla que desea ajustar.
3. Seleccione un campo en **Campos de la plantilla** y dibuje su rectángulo sobre el dato. La marca de verificación indica que ya está colocado.
4. Para reposicionarlo, seleccione el campo y dibuje de nuevo. También puede mover el rectángulo y ajustar sus bordes. **Quitar campo** o **Supr** retira la zona seleccionada.
5. Use las flechas de página anterior y siguiente, el zoom y el ajuste a ventana para comprobar varias hojas. Las teclas izquierda y derecha también cambian de página. `Ctrl++` y `Ctrl+-` cambian el zoom.
6. Pulse **Guardar plantilla** (`Ctrl+S`), escriba un nombre y guarde una copia JSON.
7. Seleccione esa copia en la ventana principal, ejecute **Más acciones -> Preprocesar** y pruebe el procesamiento con unos pocos PDF antes de usarla en una entrega completa.

**Campo seleccionado** muestra sus características: el tipo indica si lee texto, firma o casilla; obligatorio indica si se espera el dato; formato y postproceso describen cómo se valida y normaliza; los umbrales de tinta se usan para detectar escritura. El editor modifica las zonas; las reglas avanzadas se mantienen en el JSON.

## Tema claro y tema oscuro

La aplicacion se abre en oscuro. Use el toggle con el icono de luna situado
en el extremo derecho de la fila de busqueda para pasar al tema claro. En el
tema claro muestra un sol; vuelva a pulsarlo para regresar al oscuro.

El cambio es inmediato y vale para todas las ventanas abiertas a la vez, incluidas AirVault, Web Reports, el visor de CSV y el editor de plantillas. No hace falta cerrar nada ni volver a abrir la aplicacion, y no se interrumpe un procesamiento en marcha.

La eleccion se recuerda: la proxima vez que abra el programa lo hara con el tema que dejo puesto. Se guarda en `interfaz.json`, junto al programa, asi que viaja con la copia portable y no depende del perfil de Windows.

## Las opciones se quedan como las dejo

Lo mismo vale para todo lo demas que se elige moviendo un control, no solo
para el tema. Al volver a abrir el programa estan como las dejo la ultima
vez:

- En la ventana principal: la plantilla, «Verificar matriculas», lo que
  muestra el menu «Campos» y las discrepancias marcadas en «Discrepancias a
  detectar».
- En «Salida»: el formato (un solo PDF o varios), la politica de fecha, lo
  marcado en «Separacion» (matricula, mes, posibles discrepancias, errores)
  y si la entrega se reparte en partes, con su cantidad de paginas.
- En AirVault: «Completar batch», «Detener subida si se detectan
  duplicados», «Compresion», «Mostrar solo la ejecucion seleccionada»,
  «Revisar cada» con sus minutos, el maximo por batch y los pasos de
  «Automatizacion».
- En el visor de CSV: «Mostrar campos» y si la tabla abre con las columnas
  importantes o con todas.
- En Web Reports: que excepciones se traen y si se revisan las imagenes
  antes de eliminar copias.

No se recuerda lo que cambia en cada consulta y no es una preferencia: los
PDF de entrada, la ejecucion elegida en el historial y el rango de fechas de
Web Reports abren en blanco, como siempre.

Todo esto vive en `interfaz.json` y `airvault.json`, junto al programa. Son
archivos de cada instalacion y no se suben al repositorio, asi que marcar
una casilla en esta maquina no estorba al `git pull` de la otra; lo que
cambia en una no se lleva a la otra.

## Resolver problemas frecuentes

| Problema                                          | Acción                                                                                                                                                  |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Los recuadros no cubren los datos                 | Compruebe la plantilla y la vista preprocesada en varias páginas.                                                                                       |
| No encuentra el PDF original                      | Abra el visor y pulse **Ubicar PDF…**.                                                                                                                  |
| La sesión de AirVault venció                      | Inicie sesión de nuevo en Edge y use **Continuar pendiente**.                                                                                           |
| Web Reports no corrigió una bitácora              | Lea el motivo en el aviso final. Si está tomada por otro usuario, reintente más tarde; si Web Search ya no coincide con el reporte, vuelva a consultar. |
| Un batch tiene distinta cantidad de páginas       | Revise la entrega y el batch antes de indexar; la diferencia bloquea el trabajo afectado.                                                               |
| Hay valores remotos distintos o posible duplicado | Revise el conflicto en AirVault antes de reintentar.                                                                                                    |
| Necesita liberar espacio                          | **Carpetas -> Vaciar input / Vaciar output** envía esos archivos a la Papelera. `input/processed/` se conserva al vaciar input.                         |
