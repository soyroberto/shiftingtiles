# Publicador beta reanudable de OneDrive

El publicador de `func-shiftingtiles-beta-onedrive` admite hasta veinte bibliotecas configuradas en `ONEDRIVE_LIBRARIES`. Cada una tiene su propio recorrido, progreso, transacción de publicación, manifiesto público y galería. OneDrive solo se lee con `Files.Read`: los originales no se convierten, redimensionan, renombran ni modifican.

## Configuración

`ONEDRIVE_LIBRARIES` es un JSON, no una variable adicional llamada `on`. La propiedad `enabled` activa o desactiva el trabajo de cada biblioteca. La configuración aprobada de esta beta es:

```json
[
  {"id":"spotify","path":"Musica/Spotify","recursive":true,"enabled":true},
  {"id":"instagram","path":"Instagram","recursive":true,"enabled":true}
]
```

El `id` debe ser único, de 1 a 48 caracteres en minúscula, números, guion o guion bajo, empezando por letra o número. Es la identidad del estado y del prefijo público; no reutilizarlo para una carpeta o cuenta distinta. `path` es relativo a la raíz del OneDrive conectado. `recursive:false` lee únicamente archivos directos; `recursive:true` incluye descendientes de carpetas propias, sin seguir elementos remotos/compartidos ni escapar de esa raíz.

Se incluyen JPG, JPEG, PNG, GIF, WebP y AVIF con MIME y firma raster válidos. Los archivos sin extensión no se descargan. Si el usuario los renombra fuera del publicador con una extensión compatible, podrán incorporarse en una reconciliación posterior. No se deduce ni corrige su extensión automáticamente. Una foto presente en dos bibliotecas autorizadas tiene una copia por biblioteca; no se deduplican ni mezclan bibliotecas por contenido.

`ONEDRIVE_SYNC_ENABLED=false` detiene ambos timers antes de Graph, coordinación o publicación. Las fotos públicas existentes permanecen. `enabled:false` detiene el trabajo de esa biblioteca, pero no borra sus fotos, manifiesto ni checkpoints. Desactivar o quitar una entrada no revoca el acceso público a copias ya publicadas.

`ONEDRIVE_SYNC_SCHEDULE=0 0 * * * *` marca reconciliaciones cada hora UTC. `ONEDRIVE_CONTINUATION_SCHEDULE=0 */5 * * * *` continúa trabajo pendiente cada cinco minutos y no inicia scans de bibliotecas inactivas sin una reconciliación pendiente. Una invocación procesa una biblioteca lista, rotando de manera justa. Una biblioteca nueva se atiende antes de volver a dedicar el turno a una carga larga existente. El timer horario no reinicia una carga que aún está en curso. Los timers son nativos de Azure; no hay ejecuciones recurrentes de Manus.

`ONEDRIVE_BATCH_SIZE=64` limita candidatos por lote (configuración entre 1 y 100; modo progresivo acotado a 64). `ONEDRIVE_RUN_SECONDS=180` es el presupuesto objetivo por invocación, con reserva para journaling y margen respecto del timeout de cuatro minutos. Los límites `ONEDRIVE_MAX_ITEMS=100000` y `ONEDRIVE_MAX_PAGES=500` se aplican por biblioteca y cuentan entradas omitidas y páginas de descendientes. Se mantienen controles de profundidad, cursor, ciclos, duplicados y pertenencia; no se eliminan los guards para forzar recorridos ilimitados.

`ONEDRIVE_PROGRESSIVE_PUBLISH=true` publica cada lote verificado o prefijo parcial al terminar una ejecución. `ONEDRIVE_SKIP_INVALID=true` registra y omite firmas raster inválidas, y cuenta archivos mayores de 50 MiB como omitidos. No transforma errores de autenticación, red, versión, raíz, ownership o almacenamiento en falsas omisiones de fotos.

`ONEDRIVE_RETAIN_INITIAL=true` captura una sola vez las imágenes iniciales de cada vista de biblioteca. Los seis originales de prueba ya capturados permanecen únicamente en Spotify. La vista de Instagram nunca recibe ni captura fotos de Spotify. La opción no conserva ilimitadamente todas las versiones nuevas.

## Destinos y galerías independientes

Storage account `saauxfotos`, contenedor público `$web`:

```text
photos/onedrive/spotify/<hash>.<ext>
photos/onedrive/spotify/gallery.json
photos/onedrive/instagram/<hash>.<ext>
photos/onedrive/instagram/gallery.json
```

Las copias usan rutas opacas, sin los nombres originales de OneDrive. Las subcarpetas afectan al recorrido, pero sus copias siguen dentro del prefijo de su biblioteca, sin reproducir nombres privados de carpetas.

- Spotify predeterminada: https://saauxfotos.z26.web.core.windows.net/
- Spotify explícita: https://saauxfotos.z26.web.core.windows.net/?library=spotify
- Instagram: https://saauxfotos.z26.web.core.windows.net/?library=instagram

El alias `$web/gallery.json` conserva **solo Spotify**, por compatibilidad. Nunca contiene una lista combinada. Cada selección tiene un cache separado; las respuestas o caches que incluyan fotos de otra biblioteca se rechazan íntegramente. Bibliotecas nuevas vacías, inexistentes o indisponibles muestran una galería vacía, no el fallback de Spotify. Spotify conserva su fallback legado.

El navegador consulta cada minuto con ETag/If-None-Match; HTTP 304 conserva el último listado válido sin transferir otra vez el manifiesto. Los cambios se aplican a la animación existente, sin duplicar timers ni precargar toda la colección. Una respuesta válida vacía limpia/oculta la selección actual.

## Estado y recuperación

El contenedor `onedrive-state` es privado. `publisher-state.json` conserva la conexión delegada y todas las galerías confirmadas, con transacciones independientes por biblioteca. Una sola lease primaria serializa escrituras y renovación de credenciales. Las vistas del motor solo ven sus propias fotos, intents y cola de mantenimiento, y sus escrituras se integran sin sustituir datos de otra biblioteca.

- Spotify conserva `publisher-work.json` y las referencias existentes.
- Otras bibliotecas usan `libraries/<id>/publisher-work.json`.
- `publisher-coordinator.json` guarda rotación y reconciliaciones pendientes.
- `jobs/<uuid>/` contiene chunks de scan, intenciones y completions con SHA-256.

El recorrido recursivo se reanuda desde un cursor durable y acotado con IDs de carpetas y cadenas de pertenencia, no nombres ni URLs de descarga. Se valida que cada enlace paginado corresponda al endpoint de hijos de su carpeta. Al descargar se vuelve a comprobar versión, padre y pertenencia a la raíz antes/después; los movimientos fuera del ámbito no autorizan publicación.

La transición aprobada de **no recursivo a recursivo para la misma biblioteca, ruta, cuenta, cliente y almacenamiento** archiva el trabajo anterior y conserva sus pruebas de ownership y SHA, sus copias y la retención inicial. Requiere volver a recorrer metadatos para incluir descendientes, pero no necesita volver a descargar copias previamente verificadas que sigan coincidiendo. No se aplica esa adopción automática a cambios de cuenta, ruta o cliente, ni sobre una publicación ambigua pendiente. No se debe editar el journal o borrar el estado para forzar un reset.

Primero se completa el scan durable de metadatos de cada biblioteca. Después se guardan intenciones de copia antes de subir, y completions verificadas antes de ampliar su manifiesto. El checkpoint precede a la intención transaccional; la intención privada precede al manifiesto público, y el estado se confirma después. La recuperación de una publicación pendiente ocurre antes de nuevas descargas de esa biblioteca. Se preserva la última galería válida mientras quedan candidatos pendientes; solo al finalizar se reconcilia la lista completa.

No se eliminan automáticamente copias ni chunks. Las versiones fuera de la lista activa pueden quedar en `pending_cleanup`. No usar el comando legado `clear-published` como mantenimiento rutinario de galerías multi-biblioteca: la limpieza de todos los manifiestos separados requiere un flujo explícito y una autorización de eliminación independiente.

## Despliegue y costes

Desplegar con `ONEDRIVE_SYNC_ENABLED=false`. El paquete debe incluir los trece archivos de ejecución, incluido `library_sync.py`, sin settings, caches, secretos ni tests, y con permisos Unix ZIP 0644. Verificar el host `Running`, los dos timers indexados y sus guards antes de habilitar. Los settings deben propagarse a la instancia antes de invocar.

Al habilitar, una invocación manual de `OneDrivePublisher` registra las bibliotecas para reconciliación; el coordinador las atiende por turnos y las continuaciones nativas prosiguen desde sus checkpoints. Añadir una biblioteca al JSON no exige cambiar código; su primer scan comienza en la siguiente reconciliación horaria o invocación manual del publisher. Un manifiesto parcial visible no significa que la carga completa haya concluido.

Dos bibliotecas y subcarpetas añaden metadatos, descargas iniciales, almacenamiento y posibles logs/tráfico. No se provisionan recursos nuevos ni se reprocesan fotos mediante conversión. El cómputo, checkpoints, coexistencia de versiones y tráfico público se cobran aparte. 15 AUD mensuales es una referencia, no un tope automático; comprobar Cost Management después considerando el retraso de contabilización. Los originales y sus copias conservan EXIF/GPS si existen, y las copias son públicamente accesibles.
