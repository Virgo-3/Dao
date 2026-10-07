"""Build a standalone Windows x64 Dao console executable and runnable package.

Install the pinned build dependency first; this script never installs packages,
deletes directories, or includes a user's database or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tomllib
import zipfile


PYINSTALLER_VERSION = "6.22.3"


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build the standalone Windows x64 Dao executable")
    parser.add_argument("--output-dir", type=Path, default=repo / "dist" / "windows", help="Package output directory (default: dist/windows)")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Windows executable builds must run on Windows; use the Windows build GitHub Actions workflow on other systems.")
    if struct.calcsize("P") != 8:
        parser.error("Windows x64 builds require a 64-bit Python interpreter.")
    try:
        installed = metadata.version("pyinstaller")
    except metadata.PackageNotFoundError:
        parser.error(f"Install the build dependency with: python -m pip install pyinstaller=={PYINSTALLER_VERSION}")
    if installed != PYINSTALLER_VERSION:
        parser.error(f"This reproducible build requires PyInstaller {PYINSTALLER_VERSION}; found {installed}. Install the pinned version first.")

    output = args.output_dir.resolve()
    work = repo / "build" / "windows"
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--console", "--noupx", "--name", "Dao",
        "--add-data", f"{repo / 'dao' / 'static'}:dao/static",
        "--paths", str(repo),
        "--distpath", str(output),
        "--workpath", str(work),
        "--specpath", str(work),
        str(repo / "packaging" / "dao_entry.py"),
    ]
    try:
        subprocess.run(command, cwd=repo, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"PyInstaller build failed with exit code {exc.returncode}.", file=sys.stderr)
        return exc.returncode

    executable = output / "Dao.exe"
    if not executable.is_file() or executable.stat().st_size == 0:
        print("Build did not produce a nonempty Dao.exe.", file=sys.stderr)
        return 1
    with (repo / "pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    shutil.copyfile(repo / "LICENSE", output / "LICENSE")
    readme = output / "README.txt"
    readme.write_text(
        f"Dao {version} — Windows x64\n\n"
        "No Python installation is needed. Extract the package before running.\n"
        "Open PowerShell in this directory and run:\n"
        "  .\\Dao.exe\n"
        "  .\\Dao.exe --web --open-browser\n\n"
        "The default is an offline simulator. Enter /help for terminal commands.\n"
        "State persists at %LOCALAPPDATA%\\Dao\\state.sqlite3.\n"
        "Use --db PATH to select a separate database; terminal and browser can share it.\n"
        "No credentials, conversation state, or Python installation are bundled.\n"
        "The executable is unsigned. Its SHA-256 is in SHA256SUMS.\n\n"
        "Live AI reads process environment settings (DAO_PROVIDER=openai,\n"
        "OPENAI_API_KEY, and optionally DAO_MODEL). Keep keys private.\n\n"
        "Launch, configuration, checksums, and build instructions:\n"
        "https://github.com/Virgo-3/Dao-1/blob/main/docs/windows.md\n"
        "Source and license: https://github.com/Virgo-3/Dao-1\n",
        encoding="utf-8",
    )
    with executable.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    checksum = output / "SHA256SUMS"
    checksum.write_text(f"{digest}  Dao.exe\n", encoding="ascii")
    archive = output / f"Dao-windows-x64-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in (executable, checksum, output / "LICENSE", readme):
            package.write(path, arcname=path.name)
    print(f"Executable: {executable}")
    print(f"SHA-256: {digest}")
    print(f"Runnable package: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
