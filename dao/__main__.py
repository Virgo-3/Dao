import argparse
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import webbrowser

from . import __version__
from .config import Config
from .server import make_server
from .service import Dao
from .store import Store


def default_database():
    """Frozen apps retain data outside the temporary executable extraction path."""
    if not getattr(sys, "frozen", False):
        return Path(".dao/state.sqlite3")
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Dao" / "state.sqlite3"
    base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "dao" / "state.sqlite3"


def main(argv=None, *, default_terminal=False):
    parser = argparse.ArgumentParser(description="Dao — a writing room with alternate drafts and saved versions")
    parser.add_argument("--version", action="version", version=f"Dao {__version__}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--terminal", dest="interface", action="store_const", const="terminal",
                      help="Develop your story in this terminal")
    mode.add_argument("--web", dest="interface", action="store_const", const="web",
                      help="Open the local browser writing room")
    parser.set_defaults(interface="terminal" if default_terminal else "web")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", help="SQLite database path (shared by both interfaces)")
    parser.add_argument("--branch", default="main", help="Initial terminal draft (stored as a branch)")
    parser.add_argument("--open-browser", action="store_true", help="Open the browser writing room on startup")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    if args.open_browser and args.interface != "web":
        parser.error("--open-browser requires --web")
    try:
        config = Config.from_env()
    except ValueError as exc:
        parser.error(str(exc))
    path = Path(args.db) if args.db is not None else default_database()
    try:
        store = Store(str(path))
    except (OSError, sqlite3.Error) as exc:
        parser.error(f"Cannot open state database: {exc}")
    server = None
    break_handler = None
    try:
        if not store.verify()["ok"]:
            parser.error("State integrity failed; restore a trusted database backup before starting")
        app = Dao(store, config)
        if args.interface == "terminal":
            from .terminal import Terminal
            try:
                store.head(args.branch)
            except (ValueError, KeyError) as exc:
                parser.error(str(exc))
            print(f"State database: {path.resolve()}", flush=True)
            return Terminal(app, args.branch).run()
        try:
            server = make_server(app, args.port)
        except OSError as exc:
            parser.error(f"Cannot bind local port {args.port}: {exc}")
        url = f"http://127.0.0.1:{args.port}"
        print(f"Dao · {config.provider} · {url}", flush=True)
        print(f"State database: {path.resolve()}", flush=True)
        if args.open_browser:
            webbrowser.open(url)
        if hasattr(signal, "SIGBREAK") and threading.current_thread() is threading.main_thread():
            break_handler = signal.getsignal(signal.SIGBREAK)
            signal.signal(signal.SIGBREAK, signal.default_int_handler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        if server is not None:
            server.server_close()
        if break_handler is not None:
            signal.signal(signal.SIGBREAK, break_handler)
        store.close()


def terminal_main(argv=None):
    return main(argv, default_terminal=True)


if __name__ == "__main__":
    raise SystemExit(main())
