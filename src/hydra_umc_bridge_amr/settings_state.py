# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - the memory of the switches announced by the server
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""What the signed settings of the server leave on disk, so that a restart forgets neither of two things.

A signed message expires after a few minutes. Without a memory a bridge that restarts while the server is away comes back with the values of its own file, which can be
less strict than the ones an administrator had set, and its replay counter starts again from nothing. The state file keeps the counter of the last message applied and
the values it carried. The counter is taken back whole. Of the values, only those that make the bridge stricter are applied again at the start: a value that relaxes
anything needs a current signed message.

The file is named by `HYDRA_UMC_AMR_SETTINGS_STATE_FILE`; without it, it is the key file of the settings with `.state.json` after its name. A file that cannot be read or written is
said in the log and changes nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Mapping

STATE_ENVIRONMENT = "HYDRA_UMC_AMR_SETTINGS_STATE_FILE"
MAX_STATE_BYTES = 4096
_LOG = logging.getLogger(__name__)


class SettingsState:
    """The counter and the values of the last settings applied, in one small JSON file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    @classmethod
    def from_environment(cls, key_path: str, environ: Mapping[str, str] | None = None) -> "SettingsState | None":
        source = os.environ if environ is None else environ
        named = source.get(STATE_ENVIRONMENT, "").strip()
        if named:
            return cls(named)
        return cls(key_path + ".state.json") if key_path else None

    def load(self) -> tuple[int, dict[str, bool | str]]:
        """(counter, values); (-1, {}) when there is no file or it cannot be trusted."""
        try:
            with open(self.path, "rb") as handle:
                data = handle.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return -1, {}
        except OSError as error:
            _LOG.warning("the state of the remote settings cannot be read, nothing is restored: %s", error)
            return -1, {}
        try:
            body = json.loads(data) if len(data) <= MAX_STATE_BYTES else None
        except (ValueError, RecursionError):
            body = None
        seq = body.get("seq") if isinstance(body, dict) else None
        values = body.get("settings") if isinstance(body, dict) else None
        if type(seq) is not int or seq < 0 or not isinstance(values, dict):
            _LOG.warning("the state of the remote settings is not readable, nothing is restored: %s", self.path)
            return -1, {}
        return seq, {key: value for key, value in values.items() if isinstance(value, (bool, str))}

    def save(self, seq: int, values: Mapping[str, bool | str]) -> bool:
        """Write the file whole under another name and rename it over the old one; False (and a warning) when it cannot be written."""
        temporary = ""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"seq": seq, "settings": dict(values)}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            _LOG.warning("the state of the remote settings cannot be written, a restart will forget them: %s", error)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            return False
        return True
