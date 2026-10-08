"""Separate Dao Narrative browser and terminal launchers."""

from pathlib import Path

from dao.__main__ import Application, main as launch

from .app import NarrativeDao
from .terminal import NarrativeTerminal


APPLICATION = Application(
    name="Dao Narrative",
    description="Dao Narrative — a writing room with alternate drafts and saved versions",
    data_folder="DaoNarrative",
    source_folder=".dao-narrative",
    port=8766,
    service_type=NarrativeDao,
    terminal_type=NarrativeTerminal,
    static_dir=Path(__file__).parent / "static",
    terminal_help="Develop your story in this terminal",
    web_help="Open the local browser writing room",
    branch_help="Initial terminal draft (stored as a branch)",
)


def main(argv=None):
    return launch(argv, application=APPLICATION)


def terminal_main(argv=None):
    return launch(argv, default_terminal=True, application=APPLICATION)


if __name__ == "__main__":
    raise SystemExit(main())
