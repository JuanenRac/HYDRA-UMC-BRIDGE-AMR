<!-- =============================================================================
HYDRA-UMC-BRIDGE-AMR - Technical bridge guide
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-AMR Technical Guide

## Scope and operating model

This bridge does exactly two real things before a job ever reaches an AMR: resolves a factory-frame coordinate into that AMR's own local frame via a real 2D rigid-body transform (`FrameTransform`), and maps a job phase onto a minimal, VDA-5050-inspired order action (`MOVE_TO_STAGING`, `PICK_LOAD`, `MOVE_TO_DESTINATION`, `DROP_LOAD`, `MOVE_TO_HOME`, `CANCEL_ORDER`). It has no navigation, localization or obstacle-avoidance logic - those stay the AMR's own onboard authority, or its fleet manager's.

`PREPARE` maps to staging, `LOAD`/`UNLOAD` to pick/drop, `PROCESS` to the destination move, `COMPLETE` to home, and `ABORT` to `CANCEL_ORDER` - the internal name of VDA 5050's `cancelOrder` instant action (on the wire the action type is `cancelOrder`, case-sensitive; `PICK_LOAD` / `DROP_LOAD` go out as `pick` / `drop`, and a movement carries no action type at all), reserved so it is always reachable regardless of cell state. `COMPLETE` -> `MOVE_TO_HOME` goes through the same gate as every productive phase: with the cell inhibited, or the vehicle reporting itself as anything but `IDLE`, it is refused and only the cancel gets through. "Go home" is deliberately not exempt like the cancel: it is a motion. The abort phase sends `cancelOrder` by default; `AmrCoordinator(abort_action=AmrCoordinator.START_PAUSE)` makes it hold the vehicle with `startPause` instead (the order is kept), and both are always forwarded; `abort_action=AmrCoordinator.ABORT_BOTH` sends `cancelOrder` and then `startPause` (`dispatch_all()` returns both). `AmrCoordinator.resume()` builds `stopPause`, which is a motion and not a safety action: it is accepted only for a READY cell and a vehicle read as HOLDING, never blindly. A movement action missing its `x`/`y` parameters, or carrying a non-numeric one, is rejected before any transform runs. `AmrCoordinator.dispatch()` itself is still `plan-only`, never a live fleet-manager call - only `mqtt_transport.py`'s `Vda5050Publisher`, given that already-gated dispatch explicitly, ever reaches the network.

`mqtt_transport.py` is this bridge's first real transport: `Vda5050Publisher.publish()` builds and sends a real, spec-shaped VDA 5050 message on the real topic template (`{interfaceName}/{majorVersion}/{manufacturer}/{serialNumber}/{order|instantActions}`, VDA5050_EN.md section 4.2). A real `order` message describes exactly one destination node (carrying the `pick` / `drop` action when there is one) and an empty edge list - this bridge still never plans a multi-waypoint route. That is the limit to know about: a VDA 5050 AGV expects the first node of an order to be the node it stands on, followed by an edge and the destination, which needs its reported position and the plant graph; those are not built, so a real AGV is expected to refuse these single-node orders. A rejected `AmrDispatch` is never published, and a message that would not be valid JSON (a coordinate that is not finite) is never sent. `actionId`, `nodeId` and `orderId` carry a token that differs after every restart; `header_id` and `timestamp_iso` may be left out and are then counted per topic and stamped in UTC. `FrameTransform.bounds` optionally limits the factory-frame area a target may be in.

`open_mqtt_client()` is the one place `paho-mqtt` (optional `[mqtt]` extra) is imported, lazily. The broker authenticates a credential by the prefix of the client id, so the client has an explicit `client_id` (default `hydra-umc-bridge-amr`), uses callback API version 2, and waits for the broker's answer: a refused login or no answer is an error at start, not a client whose every publish fails. A TLS failure is not retried. The broker's ACL must also allow this credential to publish on the `vda5050/...` order and instantActions topics and to subscribe to state and connection: the ACL provisioned for the other bridges covers only `hydra/bridges/...`.

`observer.py`'s `Vda5050Observer` is the other direction: given to `open_mqtt_client(observer=...)` it subscribes (again after every reconnect) to the AGV's `state` and `connection` topics and answers `machine_state()` for the gate: OFFLINE when nothing was heard, the connection is not ONLINE, the last state is older than `max_age_seconds` (default 90 s, three of VDA 5050's 30 s state periods) or the AGV is not in AUTOMATIC mode; SAFE_STOP for an active emergency stop; FAULT for a field violation or a FATAL error; HOLDING when paused; RUNNING while driving or while an order has actions or nodes left; otherwise IDLE. Pass it to `AmrCoordinator.dispatch(..., observed_machine_state=observer.machine_state())`. The observer trusts the messages on the topics it listens to, so the broker's ACL decides who can write there. It has not been run against a real AGV or fleet manager.

## Compatible platforms

The order boundary is for AMR/AGV platforms reachable through a documented interface: commercial fleet managers exposing a real REST/WebSocket API (MiR, Omron-class), or an open-source stack's own 2D navigation topics. A real VDA 5050 MQTT publisher (`Vda5050Publisher`, above) is now implemented and tested, since the order-action vocabulary was already shaped around VDA 5050 - the open, vendor-neutral fleet-integration standard several commercial fleets already support. A vendor-specific REST/WebSocket adapter for a non-VDA-5050 fleet manager is still future work, introduced only after that platform is selected and tested. Publishing a real message still requires a real broker connection and a real AGV subscribed to it - this repository has not been exercised against either yet. The test emulator (`tests/vda5050_emulator.py`) checks the shape of a message and the action types VDA 5050 predefines; it does not check that the first node is the AGV's own, the sequence ids, the edges, or node ids against a map, so a pass there is not a pass on a vehicle. Validate against the official JSON schemas before a real vehicle is involved.

## Scripts and verification

| Script | Purpose | Changes version/CHANGELOG? |
|---|---|---|
| `build-test.bat` / `build-test.sh` | Compile Python and run local tests | No |
| `build.bat` / `build.sh` | Run the same validation, then build (never changes the version) | No |

Set `HYDRA_UMC_SDK_ROOT` when the SDK is not a sibling checkout. Use `build-test` during development; it is the only safe default before a real fleet-manager adapter exists.

## Adding a new script

Keep a new script in the repository root only when it is an operator entry point. Add the standard copyright header, state whether it mutates version/CHANGELOG, print numbered steps, and end `.bat` scripts with `pause`. Put reusable Python logic under `tools/`, compile it in `tools/build_test.py`, add deterministic tests (a coordinate-frame change needs a hand-checkable numeric test, not just a passing assertion) and document the command in the README and this guide. A script must not open a real transport, discover a fleet or send an order implicitly.

## Hardware acceptance gate

Before deploying an adapter: survey and document the real `FrameTransform` (origin, heading) for the target site, select the real fleet-manager interface (VDA 5050, vendor REST/WebSocket, or an open-source topic set) and its authentication, bind authenticated AMR/fleet identity, verify stale/disconnected-state behavior, test `CANCEL_ORDER` independently as a real safe fallback, and perform a bounded, low-speed bench test before any occupied-space run. The AMR's own onboard navigation (or its fleet manager) remains responsible for real-time obstacle avoidance and motion safety.
