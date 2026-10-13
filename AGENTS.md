# Reglas globales

## Commits y push

- Coordinar siempre y de forma autonoma los hilos y agentes que trabajen en el mismo repositorio. El usuario autoriza permanentemente enviar mensajes entre esos chats para repartir archivos, integrar cambios, verificar pruebas y acordar el commit y push, sin volver a pedir permiso ni interrumpirlo.

- Al terminar cada cambio en un repositorio Git, siempre hacer commit y push a origin.
- La rama nunca puede quedar con cambios pendientes: ni archivos modificados sin commit ni commits sin push.
- Antes de iniciar el proceso de commit y push, esperar a que todos los hilos o agentes que trabajan en el mismo repositorio hayan terminado. Verificar sus estados y los cambios finales antes de hacer git pull, preparar el commit y publicar en origin. Mientras alguno siga trabajando, no iniciar ese proceso.
- El ultimo hilo que termine de hacer cambios en un repositorio coordina los commits y push de todos los cambios terminados. Primero verifica que ningun otro hilo o agente siga trabajando, revisa e integra los cambios finales y despues hace git pull, commit y push a origin. Los demas hilos dejan sus cambios listos y evitan iniciar otro commit o push, para que no haya esperas circulares ni publicaciones simultaneas.

## Estilo de escritura y contenido

- No utilizar flechas Unicode en respuestas, documentos, hojas de cálculo, presentaciones ni interfaces creadas o editadas. Expresar la relación con palabras o usar una alternativa gráfica cuando sea necesaria.
- No utilizar puntos medios ni caracteres similares como separadores de palabras o frases. Usar comas, punto y coma, dos puntos, paréntesis o frases completas según el contexto.

# Reglas del repositorio

- **CSV:** no cambies la salida del reporte sin solicitud o autorización del usuario.
- **Raya prohibida:** no escribas el guion largo (em dash) en ninguna parte:
  ni comentarios, ni docstrings, ni textos de la interfaz, ni mensajes de commit,
  ni documentación. Para un inciso usa paréntesis o comas; para introducir una
  explicación, dos puntos; para separar dos datos en una línea de estado, un
  guion normal.

## Git

- Coordinar siempre y de forma autonoma los hilos y agentes que trabajen en este repositorio. El usuario autoriza permanentemente enviar mensajes entre esos chats para repartir archivos, integrar cambios, verificar pruebas y acordar el commit y push, sin volver a pedir permiso ni interrumpirlo.
- Haz siempre commit y push a `origin` al terminar cada cambio que funciona y esta probado, sin esperar a que el usuario lo pida. Ninguna tarea se da por terminada mientras queden archivos modificados sin commit o commits sin push.
- Trabaja siempre en `main`. Crea otra rama solo si el usuario lo solicita explicitamente.
- Las ramas usan solo nombres descriptivos del trabajo, sin `codex`, `opus` ni prefijos de agentes o modelos. Conserva solo `main` y las ramas de trabajo que el usuario haya pedido mantener; elimina las ramas atrasadas cuando sus cambios ya esten integrados.
- Usa la identidad del usuario, que es su perfil de GitHub: `lviloriabas <lviloriabas@gmail.com>`. Antes de hacer commit comprueba que git la tenga (`git config user.name` y `git config user.email`); si falta o es otra, configúrala en este repositorio con `git config user.name lviloriabas` y `git config user.email lviloriabas@gmail.com`. Nunca publiques con la identidad que git deduce del equipo. No agregues firmas, coautorías ni marcas como `Co-Authored-By` o `Generated with`.
- Imita el historial: español sin acentos; asunto presente, en tercera persona y descriptivo, por ejemplo `Cuenta paginas terminadas del batch, no la ultima que entrega el pool`; cuerpo sobre motivo y consecuencias, no sobre archivos.
- Múltiples agentes pueden trabajar simultáneamente: antes de commitear, verifica que tu rama esté al día (`git pull`), que no haya conflictos con el trabajo de otros, y que hayas aislado tus cambios sin tocar archivos que otros estén editando. Si dos agentes editaron lo mismo, coordina un merge manual sin perder trabajo de ninguno.

## Interfaz

- Al editarla, replica los tonos `TABLE_BASE_BG` y `PANE_*` de `tokens.Paleta`, las fuentes y tamaños de `tokens.py`, y el radio de 6 px de `QGroupBox`, `#timeSummary`, `#embeddedPdfPane` y tablas. Prohibidas las esquinas en pico y otros colores.
- Ningún color se escribe a mano ni se importa suelto: se pide con `paleta()` en el momento de pintar. Hay dos temas (claro y oscuro) y una constante de módulo se queda con el del arranque. Si el color va en una hoja de una línea de un rótulo, ponla con `pintar_del_tema`; si va en la hoja de una ventana, conéctala a `gestor_tema().cambiado`; si va dentro de un icono, vuelve a teñirlo al cambiar el tema.
- No agregues por iniciativa propia botones, paneles, iconos, diálogos, columnas o indicadores. Si la solicitud los requiere, proponlos y espera.
- Todo control nuevo debe igualar a sus vecinos en alto, espacio, color e idioma. Usa español; no muestres jerga interna, perfiles de rendimiento, clases ni rutas.

## Plataforma

- **Solo CPU:** crea cada motor con `device="cpu"`; no detectes GPU ni aceleradores. Se permiten oneDNN/MKL-DNN, hilos, batches y cuantización int8.
- **Portable:** la carpeta completa debe funcionar en cualquier PC Windows sin administrador ni instalación. No dependas de rutas del sistema, registro o descargas en ejecución. Intérprete, dependencias y modelos viven en `portable/`; dirige allí `PADDLE_PDX_CACHE_HOME`.
- Precarga modelos nuevos en `portable/` con `tools/precache_paddle.py` y comprueba su uso sin internet.

## Bitácoras

- Cada libro físico tiene 50 páginas y una sola aeronave.
- `log_number` tiene exactamente siete dígitos: `00` a `49` forman un libro; `50` a `99`, el siguiente.
- Dentro del libro, la fecha no retrocede al aumentar `log_number`; puede repetirse. La regla no cruza libros.
- La fecha manuscrita suele usar casillas `DD|MMM|AA` con separadores verticales; rara vez el mes es numérico.
- Las casillas cambian de posición entre escaneos; no dependas solo de coordenadas fijas.
