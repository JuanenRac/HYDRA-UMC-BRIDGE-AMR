# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - switches announced by the server
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The switches that an administrator sets in Server Admin reach this bridge on one retained topic.

`hydra/bridges/amr/settings` carries `{"schema": 2, "settings": {"publishState": bool, "publishOpcua": bool, "zoneInterlock": bool, "zoneUnknown": "pause"|"ignore"}}`.
The bridge listens to it only when the host of the bridge allows it: `HYDRA_UMC_AMR_REMOTE_SETTINGS=1` (off by default). Without that switch the topic is not even
subscribed and the bridge keeps what its own environment says (`HYDRA_UMC_AMR_PUBLISH_STATE`, `HYDRA_UMC_AMR_PUBLISH_OPCUA`, `HYDRA_UMC_AMR_ZONE_INTERLOCK`,
`HYDRA_UMC_AMR_ZONE_UNKNOWN`). A message that is too large, unreadable, of another schema, or that carries a value of the wrong type changes nothing; an unknown key
is ignored. The state reporter and the zone interlock read their switch on every step, so a change applies without a restart.

The message is signed with the key of the server for this bridge (the shared signed-command format of the SDK, `hydra_umc_sdk.signed_commands`); the key is in the file named by
`HYDRA_UMC_AMR_SETTINGS_KEY_FILE`, and remote settings need BOTH the host switch and a usable key.
A message is applied only when its signature is valid, it has not expired, it is not from the future and its counter (`seq`) is not older than the last one applied
(a replay of an old message is refused; the same message again changes nothing). An unsigned message or one of the old unsigned format (schema 1) changes nothing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable, Mapping, Protocol

from hydra_umc_sdk.signed_commands import CommandSigningError, load_key, verify_command

REMOTE_ENVIRONMENT = "HYDRA_UMC_AMR_REMOTE_SETTINGS"
SETTINGS_TOPIC = "hydra/bridges/amr/settings"
KEY_ENVIRONMENT = "HYDRA_UMC_AMR_SETTINGS_KEY_FILE"
SIGNED_SCHEMA = 2
MAX_SKEW_MS = 5_000
MAX_EXPIRES_MS = 300_000
MAX_SETTINGS_BYTES = 4096
BOOLEAN_SWITCHES = ("publishState", "publishOpcua", "zoneInterlock")
UNKNOWN_POLICIES = ("pause", "ignore")
_TRUE = {"1", "true", "yes", "on"}
_LOG = logging.getLogger(__name__)


def remote_settings_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """True only when the host of the bridge switched remote settings on."""
    source = os.environ if environ is None else environ
    return source.get(REMOTE_ENVIRONMENT, "").strip().lower() in _TRUE


class SettingsVerifier:
    """Judges the signed settings message: signature, expiry and counter. Without a key it refuses everything (`usable` is False)."""

    def __init__(self, key: bytes | None, *, clock: Callable[[], float] = time.time) -> None:
        self._key = key
        self._clock = clock
        self._last_seq = -1
        self.last_reason = ""

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, *, clock: Callable[[], float] = time.time) -> "SettingsVerifier":
        """The key comes from the file named by `HYDRA_UMC_AMR_SETTINGS_KEY_FILE`; a missing or unusable key gives a verifier that refuses everything."""
        source = os.environ if environ is None else environ
        path = source.get(KEY_ENVIRONMENT, "").strip()
        if not path:
            return cls(None, clock=clock)
        try:
            return cls(load_key(path), clock=clock)
        except CommandSigningError as error:
            _LOG.warning("the key of the signed settings cannot be used, remote settings stay off: %s", error)
            return cls(None, clock=clock)

    @property
    def usable(self) -> bool:
        return self._key is not None

    def accept(self, body: Mapping[str, object]) -> bool:
        """True when the body is signed with the key, current, and not older than the last one accepted."""
        if self._key is None:
            self.last_reason = "no key"
            return False
        verdict = verify_command(SETTINGS_TOPIC, body, self._key, now_ms=int(self._clock() * 1000), max_clock_skew_ms=MAX_SKEW_MS, max_expires_in_ms=MAX_EXPIRES_MS)
        if not verdict.ok:
            self.last_reason = verdict.reason
            return False
        seq = body.get("seq")
        if type(seq) is not int or seq < 0:
            self.last_reason = "the counter is missing"
            return False
        if seq < self._last_seq:
            self.last_reason = "replay: the counter is older than the last one applied"
            return False
        self._last_seq = seq
        self.last_reason = ""
        return True


def parse_remote_settings(payload: bytes, verifier: SettingsVerifier | None = None) -> dict[str, bool | str] | None:
    """The known switches of a message, or None when it is too large, unreadable or of another schema. A wrong type or an unknown word drops the whole message."""
    if verifier is None or len(payload) > MAX_SETTINGS_BYTES:
        return None
    try:
        body = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(body, dict) or body.get("schema") != SIGNED_SCHEMA or not isinstance(body.get("settings"), dict):
        return None
    if not verifier.accept(body):
        _LOG.warning("a settings message was refused: %s", verifier.last_reason)
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

    def __init__(
        self, zone_interlock: object | None = None, state_reporter: object | None = None, *, allowed: bool | None = None, verifier: SettingsVerifier | None = None
    ) -> None:
        self._zone = zone_interlock
        self._reporter = state_reporter
        wanted = remote_settings_allowed() if allowed is None else bool(allowed)
        # followed only when the host allows it AND holds the key that signs the message; an unsigned message never counts
        self._verifier = (verifier if verifier is not None else SettingsVerifier.from_environment()) if wanted else None
        self.enabled = bool(wanted and self._verifier is not None and self._verifier.usable)
        if wanted and not self.enabled:
            _LOG.warning("remote settings are allowed but there is no usable key for them: they stay off")

    def subscriptions(self) -> list[tuple[str, int]]:
        return [(SETTINGS_TOPIC, 1)] if self.enabled else []

    def handles(self, topic: str) -> bool:
        return self.enabled and topic == SETTINGS_TOPIC

    def on_message(self, client: _Subscriber | None, payload: bytes) -> dict[str, bool | str]:
        """Apply one message and return what was applied ({} when the host does not allow remote settings or the message is not valid)."""
        if not self.enabled:
            return {}
        found = parse_remote_settings(payload, self._verifier)
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
