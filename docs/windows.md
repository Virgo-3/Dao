# Dao on Windows

Dao 0.4.0 builds two standalone **Windows x64 applications**: general-purpose `Dao.exe` and writing-focused `DaoNarrative.exe`. Each includes Python and both terminal and browser interfaces.

The [Windows executable workflow](https://github.com/Virgo-3/Dao/actions/workflows/windows-build.yml) provides separate `Dao-windows-x64` and `DaoNarrative-windows-x64` artifacts from current source. Each contains a runnable ZIP. Extract `Dao-windows-x64-0.4.0.zip` or `DaoNarrative-windows-x64-0.4.0.zip` to its own folder; each ZIP contains its executable, `SHA256SUMS`, `LICENSE`, and `README.txt`. Tagged builds are published under [GitHub Releases](https://github.com/Virgo-3/Dao/releases). Releases before 0.4.0 have only the earlier Dao app.

The examples below launch Dao. Use `DaoNarrative.exe` for the separate writing app, whose browser port defaults to 8766 and whose terminal commands are described in the [Narrative guide](narrative.md).

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

Dao defaults to `%LOCALAPPDATA%\Dao\state.sqlite3`; Dao Narrative uses `%LOCALAPPDATA%\DaoNarrative\state.sqlite3`. Both databases are outside the extracted packages, so updating an executable preserves its state. Source launches use `.dao/state.sqlite3` and `.dao-narrative/state.sqlite3` respectively. Existing databases stay in place. Use an explicit path to open earlier writing work in Dao Narrative or to share a database deliberately:

```powershell
.\Dao.exe --db "C:\Users\YourName\Documents\Dao\state.sqlite3"
.\Dao.exe --web --open-browser --db "C:\Users\YourName\Documents\Dao\state.sqlite3"
```

Terminal and browser use the same state engine. The default branch is `main`; `--branch NAME` selects an existing branch for a terminal launch. Concurrent views use optimistic head checks; `/head` refreshes a terminal checkpoint after a stale write is rejected.

| Terminal command | Purpose |
| --- | --- |
| `/help`, `/quit` | Show help or leave the terminal |
| `/branches`, `/branch NAME`, `/switch NAME` | List, fork from the current checkpoint, or switch branches |
| `/history`, `/restore ID_PREFIX`, `/head` | Inspect revisions, restore a reachable checkpoint as a new revision, or refresh |
| `/memory`, `/remember KEY=VALUE` | Inspect or save memory in the current branch |
| `/relationships`, `/conflicts`, `/relate PATH` | Inspect the graph, review unresolved conflicts, or apply an operation JSON file |
| `/usage`, `/verify` | Inspect lifetime accounting or verify state integrity |
| `/decide`, `/decision PATH` | Run the example or a JSON decision model |
| `/audit PATH`, `/artifact PATH` | Review a claim and evidence, or save an artifact with its current matching verdict |
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
python scripts/build_windows.py --app narrative
```

Dao outputs appear in `dist/windows`; Dao Narrative outputs appear in `dist/windows-narrative`. Use `--output-dir PATH` to choose another directory. The script validates the Windows platform, interpreter architecture, and builder version; invokes PyInstaller with the chosen application's entry point and assets; creates the executable checksum; and assembles its runnable ZIP. It does not install dependencies or bundle credentials or state.

Verify each real executable:

```powershell
python scripts/smoke_windows.py dist/windows/Dao.exe
python scripts/smoke_windows.py dist/windows-narrative/DaoNarrative.exe --app narrative
```

The relevant options are documented in the [PyInstaller usage reference](https://pyinstaller.org/en/stable/usage.html). Equivalent inputs produce the same application behavior, but binary-identical output is not promised across different Windows or Python installations. The workflow tests and builds every main-branch update, pull request, and `v*` tag; a successfully built tag is published as a GitHub Release.
