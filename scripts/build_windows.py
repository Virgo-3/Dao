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
    parser = argparse.ArgumentParser(description="Build a standalone Windows x64 Dao application")
    parser.add_argument("--app", choices=("dao", "narrative"), default="dao", help="Application to package")
    parser.add_argument("--output-dir", type=Path, help="Package output directory")
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
        parser.error(f"This build requires PyInstaller {PYINSTALLER_VERSION}; found {installed}. Install the pinned version first.")

    narrative = args.app == "narrative"
    name = "DaoNarrative" if narrative else "Dao"
    display_name = "Dao Narrative" if narrative else "Dao"
    output = (args.output_dir or repo / "dist" / ("windows-narrative" if narrative else "windows")).resolve()
    work = repo / "build" / "windows" / name
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--console", "--noupx", "--name", name,
        "--icon", str(repo / "dao" / "static" / "dao.ico"),
        "--add-data", f"{repo / 'dao' / 'static'}:dao/static",
        "--paths", str(repo),
        "--distpath", str(output),
        "--workpath", str(work),
        "--specpath", str(work),
    ]
    if narrative:
        command.extend(["--add-data", f"{repo / 'dao_narrative' / 'static'}:dao_narrative/static"])
    command.append(str(repo / "packaging" / ("dao_narrative_entry.py" if narrative else "dao_entry.py")))
    try:
        subprocess.run(command, cwd=repo, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"PyInstaller build failed with exit code {exc.returncode}.", file=sys.stderr)
        return exc.returncode

    executable = output / f"{name}.exe"
    if not executable.is_file() or executable.stat().st_size == 0:
        print(f"Build did not produce a nonempty {name}.exe.", file=sys.stderr)
        return 1
    with (repo / "pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    shutil.copyfile(repo / "LICENSE", output / "LICENSE")
    readme = output / "README.txt"
    purpose = ("A writing room with alternate drafts and saved versions." if narrative else
               "A conversational workspace with branches, revisions, evidence review, and decisions.")
    commands = ("/draft NAME, /notes, /explore, /review PATH, and /activity" if narrative else
                "/branch NAME, /memory, /decide, /audit PATH, and /usage")
    readme.write_text(
        f"{display_name} {version} — Windows x64\n\n"
        "No Python installation is needed. Extract the package before running.\n"
        "Open PowerShell in this directory and run:\n"
        f"  .\\{name}.exe\n"
        f"  .\\{name}.exe --web --open-browser\n\n"
        f"{purpose}\n"
        "It starts with the offline demo. Enter /help for terminal commands.\n"
        f"Try {commands}.\n"
        f"State persists at %LOCALAPPDATA%\\{name}\\state.sqlite3.\n"
        "Use --db PATH to select a separate database; terminal and browser can share it.\n"
        "The Python runtime is bundled. Credentials and conversation state are not included.\n"
        "The executable is unsigned. Its SHA-256 is in SHA256SUMS.\n\n"
        "Live AI reads process environment settings (DAO_PROVIDER=openai,\n"
        "OPENAI_API_KEY, and optionally DAO_MODEL). Keep keys private.\n\n"
        "Launch, configuration, checksums, and build instructions:\n"
        "https://github.com/Virgo-3/Dao/blob/main/docs/windows.md\n"
        "Source and license: https://github.com/Virgo-3/Dao\n",
        encoding="utf-8",
    )
    with executable.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    checksum = output / "SHA256SUMS"
    checksum.write_text(f"{digest}  {name}.exe\n", encoding="ascii")
    (output / f"{name}.exe.sha256").write_text(checksum.read_text(encoding="ascii"), encoding="ascii")
    archive = output / f"{name}-windows-x64-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in (executable, checksum, output / "LICENSE", readme):
            package.write(path, arcname=path.name)
    print(f"Executable: {executable}")
    print(f"SHA-256: {digest}")
    print(f"Runnable package: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
