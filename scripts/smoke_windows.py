"""Exercise the real standalone EXE's terminal, persistence, and bundled web UI."""

import argparse
import hashlib
import json
from pathlib import Path
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import tomllib
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    executable = args.executable.resolve()
    environment = {**os.environ, "DAO_PROVIDER": "demo", "DAO_TOKEN_BUDGET": "100000",
                   "DAO_MAX_OUTPUT_TOKENS": "2048"}
    for key in ("OPENAI_API_KEY", "DAO_INPUT_USD_PER_MILLION", "DAO_OUTPUT_USD_PER_MILLION"):
        environment.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="dao-exe-smoke-") as folder:
        isolated = Path(folder)
        copied = isolated / "Dao.exe"
        shutil.copyfile(executable, copied)
        environment["LOCALAPPDATA"] = str(isolated / "userdata")
        database = isolated / "userdata" / "Dao" / "state.sqlite3"
        version = subprocess.run([str(copied), "--version"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", env=environment, cwd=isolated, timeout=60)
        with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as manifest:
            expected_version = tomllib.load(manifest)["project"]["version"]
        assert version.returncode == 0 and f"Dao {expected_version}" in version.stdout, version.stderr
        operations = [
            {"operation": "node", "node": {"id": "goal", "label": "Recoverable plan", "kind": "goal", "importance": 1}},
            {"operation": "node", "node": {"id": "person", "label": "Affected person", "kind": "person", "importance": 1}},
            {"operation": "relation", "relation": {"id": "effect", "source": "goal", "target": "person", "kind": "effect",
                                                   "weight": 2, "severe": False, "actions": []}},
            {"operation": "assess", "relation_id": "effect", "belief": {"positive": 0, "neutral": 0, "negative": 1},
             "source": "Smoke fixture", "content": "Illustrative adverse effect"},
        ]
        relationship_commands = ""
        for index, operation in enumerate(operations):
            operation_path = isolated / f"relationship-{index}.json"
            operation_path.write_text(json.dumps(operation), encoding="utf-8")
            relationship_commands += f'/relate "{operation_path}"\n'
        commands = ("/remember proof=packaged\n/branch standalone\n/remember proof=branch\n"
                    + relationship_commands + "/relationships\n/conflicts\n"
                    "Should we run a reversible pilot?\n/verify\n/usage\n/quit\n")
        terminal = subprocess.run([str(copied)], input=commands, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", env=environment,
                                  cwd=isolated, timeout=60)
        assert terminal.returncode == 0, terminal.stderr
        assert "Integrity: OK" in terminal.stdout and "Error:" not in terminal.stdout, terminal.stdout
        assert database.is_file(), "Frozen default database did not use persistent application data"
        with socket.socket() as temporary_socket:
            temporary_socket.bind(("127.0.0.1", 0))
            port = temporary_socket.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        server = subprocess.Popen([str(copied), "--web", "--port", str(port)],
                                  env=environment, cwd=isolated, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, creationflags=flags)
        try:
            deadline = time.monotonic() + 60
            while True:
                if server.poll() is not None:
                    raise RuntimeError(f"Packaged web server stopped with {server.returncode}")
                try:
                    with urlopen(url + "/api/bootstrap", timeout=2) as response:
                        main_state = json.load(response)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Packaged web server did not start in 60 seconds")
                    time.sleep(0.2)
            assert main_state["head"]["state"]["memory"] == {"proof": "packaged"}
            with urlopen(url + "/api/state?branch=standalone", timeout=5) as response:
                branch_state = json.load(response)
            assert branch_state["head"]["state"]["memory"] == {"proof": "branch"}
            assert branch_state["usage"]["total_tokens"] > 0
            assert branch_state["relationships"]["negative_weight"] == 2
            assert len(branch_state["relationships"]["unresolved_conflicts"]) == 1
            assert main_state["relationships"]["relation_count"] == 0
            for route, marker in (("/", b"Dao"), ("/app.js", b"fetch"), ("/style.css", b"background")):
                with urlopen(url + route, timeout=5) as response:
                    assert marker in response.read(), f"Bundled asset missing: {route}"
            with urlopen(url + "/api/verify", timeout=5) as response:
                assert json.load(response)["ok"]
        finally:
            if server.poll() is None:
                server.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                try:
                    server.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    else:
                        server.kill()
                    server.wait(timeout=10)
            assert server.returncode == 0, f"Packaged web server shutdown failed: {server.returncode}"
        print("PASS: standalone terminal streaming, relationships, conflicts, branches, persistence, usage, web assets, integrity, shutdown")
    with executable.open("rb") as source:
        print("SHA256:", hashlib.file_digest(source, "sha256").hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
