from __future__ import annotations

import os
from pathlib import Path
from string import Template

DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"


def render_prompt(name: str, **variables: str) -> str:
    """Load a prompt template and substitute all named variables.

    The template directory defaults to the package's prompts/ directory and can be
    overridden with the DEV_PIPELINE_PROMPTS_DIR environment variable. Missing
    variables raise so template edits stay in sync with the code.
    """
    directory = Path(os.environ.get("DEV_PIPELINE_PROMPTS_DIR") or DEFAULT_PROMPTS_DIR)
    path = directory / f"{name}.txt"
    return Template(path.read_text(encoding="utf-8")).substitute(**variables)
