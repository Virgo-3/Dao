import argparse
from pathlib import Path

from .config import Config
from .server import make_server
from .service import Dao
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Dao — a reversible conversational workspace")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=".dao/state.sqlite3")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    try:
        config = Config.from_env()
    except ValueError as exc:
        parser.error(str(exc))
    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(path))
    if not store.verify()["ok"]:
        parser.error("State integrity failed; restore a trusted database backup before starting")
    server = make_server(Dao(store, config), args.port)
    print(f"Dao · {config.provider} · http://127.0.0.1:{args.port}", flush=True)
    print(f"Durable state: {path.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
