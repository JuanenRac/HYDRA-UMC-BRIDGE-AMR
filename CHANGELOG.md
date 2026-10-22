<!-- =============================================================================
HYDRA-UMC-BRIDGE-AMR - Change history
Copyright (C) JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# Changelog

## [Unreleased]

- The banner follows the animated style of the ecosystem banners, with the name, the kind and the technologies of the project.
- Added `tools/hydra_project_tool.py`: the same `validate`, `test`, `version`, `status` and `bump` commands in every repository. The checks run with `HYDRA_UMC_CI=1` and fail if the version changes; only `bump` changes it.
- The SDK is now fetched at a release tag instead of the main branch, so a change of the SDK cannot break this repository by itself.

## [0.1.3]

- Successful incremental build: synchronized package metadata and `hydra-umc.project.json`.
- The licence is written as GPL-3.0-or-later in every header and in the package metadata.

## [0.1.2] - Maturity lowered to functional
- Maturity is functional: the code, tests and documentation are in place, but nothing here has been verified on real hardware yet, so the manifest no longer claims the established level.

## [0.1.1] - VDA 5050 names on the wire, a client the broker accepts and an observer that listens

- **The cancel reaches the AGV.** VDA 5050 action types are case-sensitive: the internal `CANCEL_ORDER` / `PICK_LOAD` / `DROP_LOAD` now go out as `cancelOrder` / `pick` / `drop` (`AmrCoordinator.VDA_ACTION_TYPES`). Before, `CANCEL_ORDER` went out as it is, which an AGV does not recognize as a cancel, and the emulator hid it by accepting both spellings (it now accepts only the action types VDA 5050 predefines). A movement is only its destination node: `MOVE_TO_*` were never VDA 5050 action types and are no longer put on the node.
- **A client the broker accepts.** `open_mqtt_client()` takes a `client_id` (default `hydra-umc-bridge-amr`; the broker authenticates a credential by the prefix of the client id and the old client had none), uses callback API version 2 (the optional dependency is `paho-mqtt>=2.1,<3`), sets `reconnect_delay_set`, and waits for the broker's answer: a refused login, or no answer within `connack_timeout_seconds`, is an error at start instead of a client whose every publish failed with `rc=4`. A TLS failure is no longer retried.
- **`Vda5050Observer`** listens to the AGV's `state` and `connection` topics (subscribed again after each reconnect) and gives the gate the machine state the vehicle reports: OFFLINE when nothing was heard, the connection is not ONLINE, the state is stale or the AGV is not in AUTOMATIC mode; SAFE_STOP for an emergency stop; FAULT for a field violation or a FATAL error; HOLDING when paused; RUNNING while driving or with nodes or actions left; otherwise IDLE. Before, the bridge only published and could only believe the state a requester wrote in the job.
- **Unique ids.** `actionId` and `nodeId` carry the per-run token the `orderId` already had; a restarted bridge could repeat them, and the AGV correlates its action states by `actionId`.
- **Valid JSON.** A coordinate that overflows while being transformed, or any non-finite number, is refused instead of being sent as `Infinity` (`allow_nan=False`); `FrameTransform` accepts optional `bounds` for the area an AGV may be sent to.
- **Headers.** `header_id` and `timestamp_iso` may be left out (`None`): the publisher counts per topic and stamps UTC with milliseconds; given ones are validated (a non-negative integer, an ISO 8601 UTC timestamp).
- **Documentation.** The README and the guide no longer claim a name that was not on the wire, mention Bluetooth, say the VDA 5050 publisher is missing or draw a path to the server that does not exist; the emulator says what it does not check; `LICENSE` carries the full GPL-3.0-or-later text.
- Not run against a real AGV, fleet manager or broker. A VDA 5050 order should start at the node the AGV stands on, followed by an edge and the destination; that route (it needs the AGV's reported position and the plant graph), validation against the official JSON schemas, pause and resume actions and the dedup of repeated jobs are not built, so a real AGV is expected to refuse these single-destination-node orders. The broker's ACL must also allow this credential on the `vda5050/...` topics.
- 25 new tests (83 in total); an unused import in the CI validator removed.

## [0.1.0] - Connects to a broker that demands credentials: user, password and TLS from the environment

- The MQTT client now takes its credentials and TLS from the environment of the service (`HYDRA_MQTT_USERNAME`, `HYDRA_MQTT_PASSWORD` or `HYDRA_MQTT_PASSWORD_FILE`, `HYDRA_MQTT_TLS_CA`) through `hydra_umc_sdk.mqtt_connection`, so it can reach a production HYDRA-UMC-MQTT-BROKER, which refuses anonymous clients and evaluates its ACL for the identity of the credential.
- An explicit `username`/`password` argument still wins, and a password without a user name is refused.
- The client ID must start with the `clientIdPrefix` of the bridge's credential.
- New `tests/test_mqtt_environment.py` (a fake paho client, so it runs without paho-mqtt) proves that the environment credentials and the CA reach the client, that the explicit ones win, that nothing set is an anonymous plain connection, and that a lonely password is refused.

## [0.0.9] - The gate can use the machine state the fleet reports, and VDA 5050 order ids no longer repeat after a restart

- **Observed machine state.** `AmrCoordinator.dispatch()` takes the state the fleet manager reported for the vehicle as an optional argument; when it is given it replaces the `machine_state` a requester wrote in the job (a message anybody on the broker may publish) in the shared gate. Without it the claim is still used.
- **Order ids.** VDA 5050 refuses a new order that reuses the `orderId` of an earlier one with `orderUpdateId` 0, and the order id was made from the header id alone, which starts again after a restart. `Vda5050Publisher` now adds a random token of its own to it (one per publisher), and a caller may name the order with `order_id`.
- Six new tests (tests/test_observed_and_order_ids.py).

## [0.0.8] - Explicit AMR state table and a simulated AMR

- **Explicit order-state table and a simulated AMR** (`simulated_amr.py`):
  `TRANSITIONS` states which order action is legal from which AMR state
  (`IDLE`, `MOVING`, `LOADED_WAITING`, `UNLOADING`); a step that is not a
  legal move from the current state is refused and leaves the state
  untouched. `CANCEL_ORDER` is legal from every state and always ends in
  `IDLE`. `SimulatedAmr` follows the table with pure bookkeeping - it
  imports no transport and cannot move anything real - so a whole job,
  including stop and recovery, can be rehearsed before a vehicle is
  connected. A dispatch the coordinator already refused never changes
  state. Six new tests (48 in total).
- Tidied a comment in `coordinator.py` that had lost its first words.

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

## [0.0.6] - The SDK's own real phase-construction rejection reached this bridge's test suite

A second, closer review found this bridge's own
`test_unknown_sdk_phase_fails_closed_instead_of_guessing_an_action` still
constructed a `BridgeJob` directly with a raw `"SOME_FUTURE_PHASE"`
string - HYDRA-UMC-SDK's own real fix now rejects that AT
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

- **Fixed a real, silently-swallowed publish failure:**
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
- **Fixed a missing required map identity:** a real VDA 5050 2.0.0
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
  (`connect_with_retry()`, new) - this bridge used to fail outright if
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
