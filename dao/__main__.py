"""Run with python -m dao, python -m dao chat, or python -m dao demo."""

import argparse
import json
from pathlib import Path

from .agent import Agent
from .decision import evaluate
from .examples import launch_decision
from .provider import OpenAIProvider, ProviderError
from .store import Store, StoreError
from .terminal import Terminal, terminal_text


def main():
    parser = argparse.ArgumentParser(description="Dao — think clearly, keep your options open.")
    parser.add_argument("command", nargs="?", choices=["chat", "demo"], default="chat")
    parser.add_argument("--db", default=".dao/state.sqlite3", help="Local state database")
    parser.add_argument("--ai", action="store_true", help="Use OPENAI_API_KEY and DAO_MODEL for conversation")
    parser.add_argument("--branch", default="main", help="Conversation branch for the terminal")
    args = parser.parse_args()
    if args.command == "demo":
        print(json.dumps(evaluate(launch_decision()), indent=2))
        return
    try:
        provider = OpenAIProvider() if args.ai else None
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        with Store(args.db) as store:
            Terminal(Agent(store, provider), args.branch).run()
    except (ProviderError, StoreError, OSError, ValueError) as exc:
        parser.exit(1, terminal_text(exc) + "\n")
    except KeyboardInterrupt:
        print("\nDao stopped.")


if __name__ == "__main__":
    main()
