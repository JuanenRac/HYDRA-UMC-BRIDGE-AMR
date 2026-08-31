<!-- =============================================================================
HYDRA-UMC-BRIDGE-AMR - Change history
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# Changelog

## [Unreleased] - Maturity raised to established

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
