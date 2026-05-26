"""Resolve the Hermes home directory.

Prefers `hermes_cli.config.get_hermes_home()` if importable (the documented
helper), otherwise falls back to the `HERMES_HOME` env var, then to
`~/.hermes/`. We use this for state and plugin-private config, never for
loading the plugin code itself (that's resolved by Hermes' own plugin loader).
"""

from __future__ import annotations

import os
from pathlib import Path


def get_hermes_home() -> Path:
    try:
        from hermes_cli.config import get_hermes_home as _h  # type: ignore

        return Path(_h()).expanduser()
    except Exception:
        env = os.environ.get("HERMES_HOME")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".hermes"


def plugin_data_dir() -> Path:
    """Per-plugin config + state dir.

    Default: $HERMES_HOME/email-plugin/. Override with EMAIL_PLUGIN_DIR
    (used by tests).
    """
    override = os.environ.get("EMAIL_PLUGIN_DIR")
    if override:
        return Path(override).expanduser()
    return get_hermes_home() / "email-plugin"
