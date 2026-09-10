<!-- =============================================================================
HYDRA-UMC-BRIDGE-AMR - Change history
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# Changelog

## [0.0.7] - A protocol-faithful VDA 5050 2.0.0 AGV emulator, not a record-only fake

Until now the only MQTT double here was `FakeMqttClient` - it records
`(topic, payload)` and never inspects either. New
`tests/vda5050_emulator.py` is a real `MqttPublisher`-seam AGV that, on
every `publish()`: splits the real
`{interfaceName}/{majorVersion}/{manufacturer}/{serialNumber}/{channel}`
topic and rejects one addressed to a different AGV; validates the JSON
body against the real VDA 5050 2.0.0 order/instantActions schema shape
(the required header with a strictly-increasing integer `headerId`; for
`order` - `orderId`/`orderUpdateId`/`nodes`/`edges`, each node's
`nodeId`/`sequenceId`/`released`, a `nodePosition` carrying `x` AND `y`
AND `mapId` when present, every action's `actionId`/`actionType`/
`blockingType` in {NONE,SOFT,SINGLE,HARD}; for `instantActions` - a
non-empty `actions` list); then executes it - an accepted order becomes
the active order whose single node action goes WAITING -> RUNNING -> 
FINISHED on `step()`, and a `CANCEL_ORDER` instant action preempts it
(running action -> FAILED, order dropped, `driving` -> False) exactly
like real `cancelOrder` on the instantActions topic. `set_estop()`,
`set_field_violation()`, `set_operating_mode()` drive the physical side;
`emit_state()` returns a real VDA 5050 `state` message with
`safetyState`/`operatingMode`/`batteryState`/`actionStates`. New
`tests/test_vda5050_emulator.py` runs this bridge's real
`AmrCoordinator.dispatch()` + `Vda5050Publisher.publish()` end to end
against it (8 tests). 42 tests total.

## [0.0.6] - V07-014: the SDK's own real phase-construction rejection reached this bridge's test suite

A second independent revalidation audit found this bridge's own
`test_unknown_sdk_phase_fails_closed_instead_of_guessing_an_action` still
constructed a `BridgeJob` directly with a raw `"SOME_FUTURE_PHASE"`
string - HYDRA-UMC-SDK's own real fix (REV-008) now rejects that AT
CONSTRUCTION TIME, so the test never even reached the coordinator's own
assertion. Split in two, same as HYDRA-UMC-BRIDGE-UAV's own
already-updated test: a new
`test_constructing_a_bridge_job_with_an_unknown_phase_is_refused_by_the_sdk_itself`
proves the SDK's own real rejection, and the original test now uses a
minimal explicit double (`SimpleNamespace(phase=...)`) to keep proving
`AmrCoordinator.dispatch()`'s own defensive `_phase_actions.get(...)`
fallback still fails closed - real defense-in-depth, not weakened to let
the old construction succeed again.

## [0.0.5] - Real publish-result rc check and a real map identity

- **AMR-01 (found in an ecosystem-wide software-improvements audit):**
  `Vda5050Publisher.publish()` ignored the return value of
  `client.publish()` entirely and always reported `published=True`
  unless an `OSError` was raised - but paho-mqtt's own real
  `Client.publish()` does NOT raise when there's no live connection, it
  returns an `MQTTMessageInfo` whose `.rc` field (`0` on success,
  `MQTT_ERR_NO_CONN == 4` with no connection, among other real result
  codes) is the only signal. A disconnected client previously reported a
  dispatch as delivered while silently discarding it. Fixed: `rc` is
  now checked (read defensively via `getattr`), and a successful publish
  is now reported honestly as "queued for MQTT delivery" rather than
  implying AGV acceptance - a real order-ACK/state-topic correlation is
  separate, larger future work, not folded into this fix.
- **AMR-02 (found in the same audit):** a real VDA 5050 2.0.0
  `<nodePosition>` requires `x`, `y` AND `mapId` together
  (github.com/VDA5050/VDA5050/blob/2.0.0/json_schemas/order.schema) -
  this bridge had no concept of a map at all, so its emitted order could
  never be spec-compliant regardless of how correct the x/y transform
  itself was. `FrameTransform` gained a required (no default) `map_id`
  field - a real map identity must come from that AMR's own validated
  site-survey/fleet profile, never invented just to fill the field - now
  threaded through `AmrDispatch.map_id` and emitted as `nodePosition.mapId`.
  The publisher refuses to publish a movement dispatch missing a real
  `map_id` rather than emit a spec-invalid message.
- 6 new tests across `test_coordinator.py`/`test_mqtt_transport.py`
  (33 total, up from 29) - explicit regressions for both findings,
  including a disconnected-client (`rc=4`) probe and a movement dispatch
  missing `map_id`.

## Maturity raised to established

- **`open_mqtt_client()`'s initial MQTT connect now retries with backoff**
  (`connect_with_retry()`, new) - found in an ecosystem-wide
  software-improvements audit: this bridge used to fail outright if
  called before HYDRA-UMC-MQTT-BROKER was listening yet, a real race
  between two independent systemd units with no ordering guarantee
  across a reboot. Only `OSError` (what an unreachable broker actually
  raises) is retried; anything else surfaces immediately as a real bug.
  Once connected, paho-mqtt's own `loop_start()` background thread
  already handles a later mid-session drop on its own - only the first
  connect needed this.
- **`hydra-umc.project.json`** - `maturity` raised from `functional` to
  `established`, matching the real substance already shipped in 0.0.4
  (real, gated coordination logic plus a real VDA 5050 MQTT publisher,
  lazily imported, same rigor and scope as sibling bridges already
  marked `established` - e.g. HYDRA-UMC-BRIDGE-CNC). Metadata-only, no
  code change, no version bump.

## [0.0.4] - Real VDA 5050 MQTT publisher (pre-real: connected, not simulated)

- **`mqtt_transport.py`** (new) - this bridge's first real transport:
  `Vda5050Publisher.publish()` sends an already-gated `AmrDispatch` as a
  real, spec-shaped VDA 5050 MQTT message. Real topic template
  (`{interfaceName}/{majorVersion}/{manufacturer}/{serialNumber}/{topic}`,
  researched against
  [github.com/VDA5050/VDA5050](https://github.com/VDA5050/VDA5050/blob/main/VDA5050_EN.md)
  section 4.2). A real `order` message describes exactly ONE destination
  node (this bridge's own resolved `local_x`/`local_y`) with the action
  attached and an empty edge list - a real, valid VDA 5050 shape for "go
  here and do this", not a multi-waypoint route this bridge was never
  meant to plan. A real `instantActions` message carries the action
  directly with `blockingType: "HARD"`. Only a dispatch the shared SDK gate
  already accepted is ever published - a rejected `AmrDispatch` never
  reaches the network. `open_mqtt_client()` is the one place `paho-mqtt`
  (new optional `[mqtt]` extra) is imported, lazily, degrading to a clear
  `RuntimeError` instead of a bare `ImportError` when it isn't installed.
- 9 new regression tests against an in-memory fake MQTT client (no real
  broker needed) - 25/25 tests passing.

## [0.0.3] - Real VDA 5050 order/instantActions channel split

- **`coordinator.py`** - `CANCEL_ORDER` now reports on the real, separate
  VDA 5050 `instantActions` channel instead of the `order` channel every
  other action uses. Researched against the
  [official VDA 5050 JSON schemas](https://github.com/VDA5050/VDA5050/tree/main/json_schemas):
  the real spec publishes 8 topics, 2 of which carry actions - `order`
  (a node/edge route with actions embedded, queued) and `instantActions`
  (immediate, bypasses the order queue - the spec's own real, documented
  examples are `cancelOrder`, `startPause`, `stopPause`, `stateRequest`,
  `factsheetRequest`). Modeling `cancelOrder` as a queued order action
  was real but not real wire-compatible: a fleet manager expecting it on
  `instantActions` would never see it published to `order` instead.
- `AmrDispatch` gained a `channel` field (`"order"` | `"instantActions"`),
  `AmrOrderPlan` gained a separate `instant_actions` tuple/dict key
  alongside the existing `actions` one - schema bumped `1.0` -> `1.1`
  since the plan's own output shape changed.
- 3 new regression tests (channel split on `order_plan()`, `channel` on
  a movement dispatch, `channel` on a `CANCEL_ORDER` dispatch) -
  17/17 tests passing.

## [0.0.2] - Finite coordinate-frame gate

- **`coordinator.py`** - AMR frame origins, heading and movement targets now
  require finite values. `NaN`/infinite coordinates are rejected before an
  order plan can carry a non-physical local target to a future transport.
- Added frame and dispatch regression tests for the fail-closed path.
- 14/14 tests passing.

## [0.0.1]

- Added a dependency-free AMR/AGV coordination core (`AmrCoordinator`)
  and a real, hand-checkable 2D rigid-body coordinate transform
  (`FrameTransform`) mapping the factory's shared coordinate frame into
  a specific AMR's own local frame - verified with identity, pure
  translation, a 90-degree heading case and a real round-trip test.
- A minimal, VDA-5050-inspired order-action vocabulary (`MOVE_TO_STAGING`/
  `PICK_LOAD`/`MOVE_TO_DESTINATION`/`DROP_LOAD`/`MOVE_TO_HOME`/
  `CANCEL_ORDER`), each gated through the shared `HYDRA-UMC-SDK` safety
  contract before being forwarded.
- Added non-mutating build-test scripts and CI SDK checkout, matching
  the rest of the External Automation / Mobile Bridges family.
- Standardized README in all 7 ecosystem languages (English, Spanish,
  French, Italian, German, Simplified Chinese, Japanese), project banner
  and manifest to match the ecosystem's established-project structure.
- No real fleet-manager REST/WebSocket adapter or physical AMR
  validated yet - this is a plan-only coordination boundary.
