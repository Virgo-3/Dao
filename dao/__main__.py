import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import webbrowser

from . import __version__
from .config import Config
from .server import STATIC, make_server
from .service import Dao
from .store import Store
from .terminal import Terminal


@dataclass(frozen=True)
class Application:
    """Fixed launch settings owned by each application's entry point."""
    name: str = "Dao"
    description: str = "Dao — conversation with versioned state and decisions under uncertainty"
    data_folder: str = "Dao"
    source_folder: str = ".dao"
    port: int = 8765
    service_type: type = Dao
    terminal_type: type = Terminal
    static_dir: Path = STATIC
    terminal_help: str = "Start a streaming conversation in this terminal"
    web_help: str = "Open the local browser workspace"
    branch_help: str = "Initial terminal branch"


DEFAULT_APPLICATION = Application()


def default_database(application=DEFAULT_APPLICATION):
    """Frozen apps retain data outside the temporary executable extraction path."""
    if not getattr(sys, "frozen", False):
        return Path(application.source_folder) / "state.sqlite3"
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / application.data_folder / "state.sqlite3"
    base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / application.source_folder.lstrip(".") / "state.sqlite3"


def main(argv=None, *, default_terminal=False, application=DEFAULT_APPLICATION):
    parser = argparse.ArgumentParser(prog=application.name.lower().replace(" ", "-"), description=application.description)
    parser.add_argument("--version", action="version", version=f"{application.name} {__version__}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--terminal", dest="interface", action="store_const", const="terminal",
                      help=application.terminal_help)
    mode.add_argument("--web", dest="interface", action="store_const", const="web",
                      help=application.web_help)
    parser.set_defaults(interface="terminal" if default_terminal else "web")
    parser.add_argument("--port", type=int, default=application.port)
    parser.add_argument("--db", help="SQLite database path (shared by both interfaces)")
    parser.add_argument("--branch", default="main", help=application.branch_help)
    parser.add_argument("--open-browser", action="store_true", help="Open the browser on startup")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    if args.open_browser and args.interface != "web":
        parser.error("--open-browser requires --web")
    try:
        config = Config.from_env()
    except ValueError as exc:
        parser.error(str(exc))
    path = Path(args.db) if args.db is not None else default_database(application)
    try:
        store = Store(str(path))
    except (OSError, sqlite3.Error) as exc:
        parser.error(f"Cannot open state database: {exc}")
    server = None
    break_handler = None
    try:
        if not store.verify()["ok"]:
            parser.error("State integrity failed; restore a trusted database backup before starting")
        app = application.service_type(store, config)
        if args.interface == "terminal":
            try:
                store.head(args.branch)
            except (ValueError, KeyError) as exc:
                parser.error(str(exc))
            print(f"State database: {path.resolve()}", flush=True)
            return application.terminal_type(app, args.branch).run()
        try:
            server = make_server(app, args.port, static_dir=application.static_dir)
        except OSError as exc:
            parser.error(f"Cannot bind local port {args.port}: {exc}")
        url = f"http://127.0.0.1:{args.port}"
        print(f"{application.name} · {config.provider} · {url}", flush=True)
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
