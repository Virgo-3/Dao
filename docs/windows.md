# Dao on Windows

Dao 0.3.0 has a standalone **Windows x64 terminal executable**. It includes the Python runtime, so you do not need to install Python to run `Dao.exe`.

Download `Dao-windows-x64-0.3.0.zip` from [GitHub Releases](https://github.com/Virgo-3/Dao/releases/latest), then extract it to a folder of your choice. The package contains `Dao.exe`, `SHA256SUMS`, `LICENSE`, and `README.txt`. The [Windows executable workflow](https://github.com/Virgo-3/Dao/actions/workflows/windows-build.yml) also provides build artifacts.

## Start a conversation

Open PowerShell in the extracted folder:

```powershell
.\Dao.exe
```

The executable starts an interactive terminal. Type a message and press Enter to see the response stream. The default **offline demo** produces deterministic replies without an account, API key, or AI model call. Enter `/help` for commands, or `/quit` to exit. End-of-input also exits cleanly.

To use the browser workspace instead:

```powershell
.\Dao.exe --web --open-browser
```

The browser server binds to `127.0.0.1` and defaults to port 8765. The bundled HTML, CSS, and JavaScript are available offline. Press Ctrl+C in its console to stop the server. Use `--port 9000` if the default port is occupied.

Check the executable version:

```powershell
.\Dao.exe --version
```

## Save state and create branches

Standalone launches default to `%LOCALAPPDATA%\Dao\state.sqlite3`. The database is outside the extracted package, so updating the executable preserves your state. Source launches default to `.dao/state.sqlite3` under the working directory. Select an explicit database when moving between the two:

```powershell
.\Dao.exe --db "C:\Users\YourName\Documents\Dao\state.sqlite3"
.\Dao.exe --web --open-browser --db "C:\Users\YourName\Documents\Dao\state.sqlite3"
```

Terminal and browser use the same state engine and writing language. The default draft is `main`; `--branch NAME` selects an existing draft for a terminal launch. Concurrent views use optimistic version checks; `/version` refreshes the terminal's viewed version after a stale write is rejected. Earlier command names remain aliases.

| Terminal command | Purpose |
| --- | --- |
| `/help`, `/quit` | Show help or leave the terminal |
| `/drafts`, `/draft NAME`, `/switch NAME` | List drafts, create an alternate draft from the current version, or switch drafts |
| `/versions`, `/restore ID_PREFIX`, `/version` | Inspect versions, restore a reachable version as a new version, or refresh |
| `/notes`, `/remember KEY=VALUE` | Inspect or save story notes in the current draft |
| `/connections`, `/conflicts`, `/connect PATH` | Inspect connections, review conflicts, or apply an operation JSON file |
| `/activity`, `/verify` | Inspect lifetime accounting or verify saved versions |
| `/explore`, `/explore PATH` | Compare the illustrative story example or a JSON decision model |
| `/review PATH`, `/manuscript PATH` | Review a claim against sources, or save a document with its current matching review |
| `/export PATH` | Create a JSON export; existing output files are never overwritten |
| `/say TEXT` | Send a literal message, including text beginning with `/` |

Import paths may contain spaces without quoting. Imported files must be JSON objects and are limited to 128 KiB. Restoring a checkpoint preserves audit events, provider usage, and recorded costs. Artifacts are named content in Dao's database. This release has no tools that perform external actions.

## Connect a live model

Set environment variables in the PowerShell session that starts Dao:

```powershell
$env:DAO_PROVIDER = "openai"
$env:OPENAI_API_KEY = "your-own-key"
$env:DAO_MODEL = "gpt-4.1-mini"
.\Dao.exe
```

Use a Responses API model available to your account. The executable reads process environment variables and does not load `.env` files automatically. Credentials and conversation state are not included in the downloadable package. Live inference sends the current conversation, saved memory, and relationship context to OpenAI. The API key is sent for provider authentication and excluded from Dao's public configuration, state, and exports. See the [project guide](overview.md) for provider limits, token reservation, optional price settings, and security boundaries.

## Verify the download

The executable is **unsigned**. The release provides a SHA-256 checksum to compare with the downloaded file:

```powershell
Get-Content .\SHA256SUMS
Get-FileHash .\Dao.exe -Algorithm SHA256
```

Compare the displayed hashes, ignoring letter case. A matching checksum verifies that the file matches the published checksum; it is not a code-signing identity. Keep the executable and checksum from the same release.

## Build from source

The build runs on Windows with a 64-bit Python interpreter. The CI build uses Python 3.14 and pinned PyInstaller 6.22.3. PyInstaller bundles the interpreter, application, and static assets into a single console executable using its [one-file mode](https://pyinstaller.org/en/stable/operating-mode.html#bundling-to-one-file).

From the repository root:

```powershell
python -m pip install pyinstaller==6.22.3
python -m unittest discover -s tests -v
python scripts/build_windows.py
```

The outputs appear in `dist/windows`. To choose another directory, use `python scripts/build_windows.py --output-dir "C:\path\to\package"`. The script validates the Windows platform, interpreter architecture, and builder version; invokes PyInstaller with bundled static assets; creates the executable checksum; and assembles the runnable ZIP package. It does not install dependencies or bundle your credentials or state.

The relevant options are documented in the [PyInstaller usage reference](https://pyinstaller.org/en/stable/usage.html). Equivalent inputs produce the same application behavior, but binary-identical output is not promised across different Windows or Python installations. The workflow tests and builds every main-branch update, pull request, and `v*` tag; a successfully built tag is published as a GitHub Release.
