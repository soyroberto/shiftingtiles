"""Azure Functions Python v2 timer. Importing this module performs no remote IO."""
import logging
import azure.functions as func
from config import Config
from storage import BlobStore
from sync import sync_locked

app = func.FunctionApp()


@app.function_name(name="OneDrivePublisher")
@app.timer_trigger(schedule="0 */15 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def onedrive_publisher(timer: func.TimerRequest) -> None:
    store = None
    try:
        config = Config.from_env()
        if not config.enabled:
            logging.info("OneDrive synchronization disabled; published copies retained")
            return
        store = BlobStore(config)
        with store.locked():
            result = sync_locked(store, store.load(), config)
        skipped = {}
        for counts in result.get("libraries", {}).values():
            for kind, count in counts["skipped"].items():
                skipped[kind] = skipped.get(kind, 0) + count
        images = sum(counts["images"] for counts in result.get("libraries", {}).values())
        logging.info("OneDrive sync completed: status=%s failed=%s images=%s skipped=%s pending_cleanup=%s",
                     result["status"], result.get("failed", 0), images, skipped, result.get("pending_cleanup", 0))
    except Exception:
        # Azure/requests/MSAL exceptions can contain URLs or sensitive metadata.
        # Never log exception text, full state, token/cache, or source names.
        logging.warning("OneDrive sync unavailable; last good gallery retained; check setup and reconnect if needed")
    finally:
        if store is not None:
            store.close()
