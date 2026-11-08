"""Native Azure timers. Imports never perform remote IO; disable gates both timers."""
import logging
import time

import azure.functions as func
from azure.core.exceptions import HttpResponseError
from config import Config
from batch_storage import BatchStore
from library_sync import run_libraries as run_resumable

app = func.FunctionApp()
# Preserve the existing test/admin factory seam; runtime uses the extended store.
BlobStore = BatchStore


def _run(start_new):
    started = time.monotonic()
    store = None
    # Dependency diagnostics can contain source names or preauthenticated URLs.
    for name in ("azure", "msal", "urllib3"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        config = Config.from_env()
        if not config.enabled:
            logging.info("OneDrive synchronization disabled; published copies retained")
            return
        store = BlobStore(config)
        with store.locked():
            result = run_resumable(store, config, start_new=start_new,
                                   deadline=started + config.run_seconds)
        logging.info("OneDrive resumable: library=%s status=%s phase=%s scanned=%s copied=%s images=%s omitted=%s pending_cleanup=%s elapsed=%.2f",
                     result.get("library", "none"), result.get("status", "unknown"), result.get("phase", "idle"),
                     result.get("scanned", 0), result.get("copied", 0),
                     result.get("images", 0), result.get("omitted", 0), result.get("pending_cleanup", 0),
                     time.monotonic() - started)
    except HttpResponseError as error:
        # Two timers may wake together. Only the lease winner works; no token/URL text.
        if getattr(error, "status_code", None) == 409:
            logging.info("OneDrive worker already active; this invocation deferred")
        else:
            logging.warning("OneDrive storage unavailable; last good gallery and checkpoints retained")
    except Exception:
        # No raw SDK/MSAL/Graph exception text or private state in telemetry.
        logging.warning("OneDrive sync unavailable; last good gallery and checkpoints retained")
    finally:
        if store is not None:
            store.close()


@app.function_name(name="OneDrivePublisher")
@app.timer_trigger(schedule="%ONEDRIVE_SYNC_SCHEDULE%", arg_name="timer",
                   run_on_startup=False, use_monitor=True)
def onedrive_publisher(timer: func.TimerRequest) -> None:
    _run(start_new=True)


@app.function_name(name="OneDriveContinuation")
@app.timer_trigger(schedule="%ONEDRIVE_CONTINUATION_SCHEDULE%", arg_name="timer",
                   run_on_startup=False, use_monitor=True)
def onedrive_continuation(timer: func.TimerRequest) -> None:
    _run(start_new=False)
