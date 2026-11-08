# Publicador beta reanudable de OneDrive

El modo reanudable de `func-shiftingtiles-beta-onedrive` procesa una biblioteca habilitada, no recursiva. Enumera todos los archivos directos, omite carpetas y archivos sin las extensiones JPG, JPEG, PNG, GIF, WebP o AVIF, y copia originales sin convertir, redimensionar ni modificar metadatos. Se conserva `Files.Read`: OneDrive solo se lee.

## Variables

`ONEDRIVE_SYNC_ENABLED=false` desactiva ambos timers sin operaciones de Graph o publicación. Los archivos públicos ya publicados permanecen visibles. `ONEDRIVE_SYNC_SCHEDULE=0 0 * * * *` inicia una nueva reconciliación cada hora UTC. `ONEDRIVE_CONTINUATION_SCHEDULE=0 */5 * * * *` avanza trabajos pendientes cada cinco minutos; no inicia otra enumeración cuando no hay trabajo. Los timers usan el scheduler nativo de Azure, sin invocaciones recurrentes de Manus.

`ONEDRIVE_BATCH_SIZE=64` controla el máximo de candidatos por lote, entre 1 y 100. `ONEDRIVE_RUN_SECONDS=180` limita el presupuesto objetivo por invocación, entre 60 y 180 segundos, con margen respecto del `functionTimeout` de cuatro minutos. `ONEDRIVE_MAX_ITEMS=100000` cuenta todas las entradas, incluidos archivos omitidos y carpetas. `ONEDRIVE_MAX_PAGES=500` mantiene un límite de paginación. La primera protección ya no es el límite legacy de 10.000 entradas; los límites no se eliminan.

`ONEDRIVE_RETAIN_INITIAL=true` captura una sola vez las fotos ya publicadas al inicializar el control reanudable y las conserva en la secuencia final. En esta beta se utiliza para las seis fotos de prueba de la subcarpeta anterior. No hace que se sigan escaneando subcarpetas ni conserva automáticamente todas las nuevas versiones de cada ejecución en la secuencia. El ejemplo local deja esta opción en false para que la retención inicial sea explícita.

`ONEDRIVE_LIBRARIES` sigue definiendo identificador, ruta, recursive y enabled. Esta entrega solo admite una biblioteca activa y no recursiva. Si se cambia la identidad o configuración de un trabajo, se invalida su progreso de forma segura; no se debe editar su journal a mano ni borrar el estado para forzar un reset.

## Durabilidad y publicación

`publisher-state.json` sigue siendo privado, contiene la conexión delegada y los datos de la galería activa. Su lease serializa los dos timers. `publisher-work.json` contiene progreso pequeño, y `jobs/` contiene chunks privados de páginas, intenciones y copias verificadas. Las referencias usan SHA-256, con controles de esquema, rutas, tamaño, duplicados y cursor de la misma carpeta. Los nombres de fuente y las URLs preautenticadas no se incluyen en esos chunks ni en logs.

La intención de copia se guarda antes de subir; las copias usan rutas opacas, creación sin overwrite y digest SHA-256. Una reanudación conserva lotes terminados y solo reconcilia trabajo ambiguo. Las comprobaciones de existencia de la galería se hacen mediante listados paginados, no un HEAD por cada foto en cada hora. Las lecturas antes/después de descargar comprueban versión y pertenencia a la carpeta.

La última galería válida permanece hasta completar el scan y todas las copias. Solo entonces se guarda la intención transaccional, se publica `gallery.json` y se confirma la nueva galería privada. La publicación usa también una lease de manifest. Una pausa o pérdida de lease no permite sustituir un manifest con un worker obsoleto. Las actualizaciones repetidas sin cambios no necesitan republicar el manifest ni descargar imágenes.

No se eliminan automáticamente copias o chunks en esta entrega. Las bajas/versiones no activas pueden quedar en la cola privada `pending_cleanup`; se difieren para no inferir eliminaciones de scans parciales o de una carpeta que cambió entre páginas. No usar `clear-published --confirm` como mantenimiento rutinario: desconecta y vacía deliberadamente la galería activa, y requiere una autorización de eliminación aparte. Si hay trabajo en curso, sus intenciones privadas deben revisarse antes de cualquier mantenimiento manual.

Los originales siguen intactos. Sus copias públicas conservan EXIF/GPS y otros metadatos si están presentes. Los archivos sin extensiones compatibles no se sincronizan; si el usuario los renombra fuera del publicador, podrán entrar en una reconciliación posterior, siempre que contenido, MIME y firma raster sean válidos.

## Operación

Desplegar siempre con `ONEDRIVE_SYNC_ENABLED=false` y ambos schedules ya configurados. El paquete debe incluir los doce módulos/archivos de ejecución, sin settings, caches, secretos ni tests, y con permisos Unix ZIP 0644. Verificar que `OneDrivePublisher` y `OneDriveContinuation` están indexadas, que el host está Running y que ambas respetan la desactivación antes de habilitar.

Tras habilitar, una invocación manual de `OneDrivePublisher` inicia la carga. Las continuaciones nativas seguirán desde los checkpoints. Una configuración habilitada y un progreso positivo no equivalen a una publicación completa: solo se considera terminada cuando se confirma el resultado del motor y el nuevo manifest público. Si fallan repetidamente el arranque o el progreso, volver a false y conservar los estados privados para diagnosticar, sin reset.

La capacidad de las fotos actuales es aproximadamente 1,09 GiB, pero el cómputo, checkpoints, coexistencia de versiones, retención de borrados durante siete días, logs y tráfico público se cobran aparte. La referencia de 15 AUD mensuales no es un tope automático. Consultar Cost Management después de la carga considerando el retraso de contabilización. Cambiar la frecuencia reduce escaneos, pero no elimina el coste de servir fotos a visitantes.
