"""Local administration, using an already-authenticated Azure CLI operator identity.

This is NOT an HTTP endpoint and is NOT called by the timer. No login command is run
implicitly. Running connect explicitly starts Microsoft's personal device-code flow.
"""
import argparse
import logging
import sys

from auth import connect, disconnect
from config import Config, PublisherError
from storage import BlobStore
from sync import clear_published


def main(argv=None):
    parser = argparse.ArgumentParser(description="OneDrive beta local administration (never modifies originals)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("connect", help="Connect the single personal owner with interactive device code")
    commands.add_parser("disconnect", help="Erase active private cache; retain all published copies")
    clear = commands.add_parser("clear-published", help="Disconnect and clear ONLY journaled managed public copies")
    clear.add_argument("--confirm", action="store_true", help="Required explicit deletion confirmation")
    args = parser.parse_args(argv)
    if args.command == "clear-published" and not args.confirm:
        parser.error("clear-published requires --confirm; originals are never touched")
    # Suppress dependency diagnostics that can include source/download URLs. Do not enable DEBUG.
    for name in ("azure", "msal", "urllib3"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    store = None
    try:
        config = Config.from_env()
        store = BlobStore(config, operator=True)
        with store.locked():
            state = store.load()
            if args.command == "connect":
                connect(store, state, config.client_id)
                print("Connected. Enable ONEDRIVE_SYNC_ENABLED in the beta Function settings when ready.")
            elif args.command == "disconnect":
                disconnect(store, state)
                print("Disconnected. Published copies retained; clear-published --confirm removes managed copies.")
            else:
                pending = clear_published(store, state)
                print(f"Disconnected and empty gallery published. Pending managed copy deletions: {pending}.")
                if pending:
                    print("Rerun clear-published --confirm to retry cleanup; check Blob permissions/network.")
                    return 2
        return 0
    except PublisherError as exc:
        print("Operation stopped: " + str(exc), file=sys.stderr)  # These are application-defined safe codes.
        return 1
    except Exception:
        print("Operation failed; no diagnostics/token data printed. Check private container, roles, lease and credentials.",
              file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
