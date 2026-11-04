# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - switches announced by the server
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The switches that an administrator sets in Server Admin reach this bridge on one retained topic.

`hydra/bridges/amr/settings` carries `{"schema": 1, "settings": {"publishState": bool, "publishOpcua": bool, "zoneInterlock": bool, "zoneUnknown": "pause"|"ignore"}}`.
The bridge listens to it only when the host of the bridge allows it: `HYDRA_UMC_AMR_REMOTE_SETTINGS=1` (off by default). Without that switch the topic is not even
subscribed and the bridge keeps what its own environment says (`HYDRA_UMC_AMR_PUBLISH_STATE`, `HYDRA_UMC_AMR_PUBLISH_OPCUA`, `HYDRA_UMC_AMR_ZONE_INTERLOCK`,
`HYDRA_UMC_AMR_ZONE_UNKNOWN`). A message that is too large, unreadable, of another schema, or that carries a value of the wrong type changes nothing; an unknown key
is ignored. The state reporter and the zone interlock read their switch on every step, so a change applies without a restart.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Mapping, Protocol

REMOTE_ENVIRONMENT = "HYDRA_UMC_AMR_REMOTE_SETTINGS"
SETTINGS_TOPIC = "hydra/bridges/amr/settings"
MAX_SETTINGS_BYTES = 4096
BOOLEAN_SWITCHES = ("publishState", "publishOpcua", "zoneInterlock")
UNKNOWN_POLICIES = ("pause", "ignore")
_TRUE = {"1", "true", "yes", "on"}
_LOG = logging.getLogger(__name__)


def remote_settings_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """True only when the host of the bridge switched remote settings on."""
    source = os.environ if environ is None else environ
    return source.get(REMOTE_ENVIRONMENT, "").strip().lower() in _TRUE


def parse_remote_settings(payload: bytes) -> dict[str, bool | str] | None:
    """The known switches of a message, or None when it is too large, unreadable or of another schema. A wrong type or an unknown word drops the whole message."""
    if len(payload) > MAX_SETTINGS_BYTES:
        return None
    try:
        body = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(body, dict) or body.get("schema") != 1 or not isinstance(body.get("settings"), dict):
        return None
    settings = body["settings"]
    found: dict[str, bool | str] = {}
    for key in BOOLEAN_SWITCHES:
        if key in settings:
            if not isinstance(settings[key], bool):
                return None
            found[key] = settings[key]
    if "zoneUnknown" in settings:
        if settings["zoneUnknown"] not in UNKNOWN_POLICIES:
            return None
        found["zoneUnknown"] = settings["zoneUnknown"]
    return found


class _Subscriber(Protocol):
    def subscribe(self, subscriptions: object) -> object: ...


class RemoteSettings:
    """Applies the settings message to the state reporter and the zone interlock of this process (either may be absent)."""

    topic = SETTINGS_TOPIC

    def __init__(self, zone_interlock: object | None = None, state_reporter: object | None = None, *, allowed: bool | None = None) -> None:
        self._zone = zone_interlock
        self._reporter = state_reporter
        self.enabled = remote_settings_allowed() if allowed is None else bool(allowed)

    def subscriptions(self) -> list[tuple[str, int]]:
        return [(SETTINGS_TOPIC, 1)] if self.enabled else []

    def handles(self, topic: str) -> bool:
        return self.enabled and topic == SETTINGS_TOPIC

    def on_message(self, client: _Subscriber | None, payload: bytes) -> dict[str, bool | str]:
        """Apply one message and return what was applied ({} when the host does not allow remote settings or the message is not valid)."""
        if not self.enabled:
            return {}
        found = parse_remote_settings(payload)
        if not found:
            return {}
        reporter, zone = self._reporter, self._zone
        if reporter is not None:
            if "publishState" in found:
                reporter.publish_state = bool(found["publishState"])  # type: ignore[attr-defined]
            if "publishOpcua" in found:
                reporter.publish_opcua = bool(found["publishOpcua"])  # type: ignore[attr-defined]
        if zone is not None:
            if "zoneUnknown" in found:
                zone.set_unknown_policy(str(found["zoneUnknown"]))  # type: ignore[attr-defined]
            if "zoneInterlock" in found:
                was_on = bool(zone.enabled)  # type: ignore[attr-defined]
                zone.enabled = bool(found["zoneInterlock"])  # type: ignore[attr-defined]
                if zone.enabled and not was_on and client is not None:  # type: ignore[attr-defined]
                    client.subscribe(zone.subscriptions())  # type: ignore[attr-defined]
        _LOG.info("remote settings applied: %s", ", ".join(f"{key}={value}" for key, value in sorted(found.items())))
        return found
