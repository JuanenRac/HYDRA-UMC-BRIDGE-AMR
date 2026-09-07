<!-- =============================================================================
HYDRA-UMC-BRIDGE-AMR - Technical bridge guide
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-AMR Technical Guide

## Scope and operating model

This bridge does exactly two real things before a job ever reaches an AMR: resolves a factory-frame coordinate into that AMR's own local frame via a real 2D rigid-body transform (`FrameTransform`), and maps a job phase onto a minimal, VDA-5050-inspired order action (`MOVE_TO_STAGING`, `PICK_LOAD`, `MOVE_TO_DESTINATION`, `DROP_LOAD`, `MOVE_TO_HOME`, `CANCEL_ORDER`). It has no navigation, localization or obstacle-avoidance logic - those stay the AMR's own onboard authority, or its fleet manager's.

`PREPARE` maps to staging, `LOAD`/`UNLOAD` to pick/drop, `PROCESS` to the destination move, `COMPLETE` to home, and `ABORT` to `CANCEL_ORDER` - VDA 5050's own real action name for exactly this purpose, reserved so it is always reachable regardless of cell state. A movement action missing its `x`/`y` parameters, or carrying a non-numeric one, is rejected before any transform runs. `AmrCoordinator.dispatch()` itself is still `plan-only`, never a live fleet-manager call - only `mqtt_transport.py`'s `Vda5050Publisher`, given that already-gated dispatch explicitly, ever reaches the network.

`mqtt_transport.py` is this bridge's first real transport: `Vda5050Publisher.publish()` builds and sends a real, spec-shaped VDA 5050 message on the real topic template (`{interfaceName}/{majorVersion}/{manufacturer}/{serialNumber}/{order|instantActions}`, VDA5050_EN.md section 4.2). A real `order` message describes exactly one destination node with the action attached and an empty edge list - this bridge still never plans a multi-waypoint route. A rejected `AmrDispatch` is never published. `open_mqtt_client()` is the one place `paho-mqtt` (optional `[mqtt]` extra) is imported, lazily.

## Compatible platforms

The order boundary is for AMR/AGV platforms reachable through a documented interface: commercial fleet managers exposing a real REST/WebSocket API (MiR, Omron-class), or an open-source stack's own 2D navigation topics. A real VDA 5050 MQTT publisher (`Vda5050Publisher`, above) is now implemented and tested, since the order-action vocabulary was already shaped around VDA 5050 - the open, vendor-neutral fleet-integration standard several commercial fleets already support. A vendor-specific REST/WebSocket adapter for a non-VDA-5050 fleet manager is still future work, introduced only after that platform is selected and tested. Publishing a real message still requires a real broker connection and a real AGV subscribed to it - this repository has not been exercised against either yet.

## Scripts and verification

| Script | Purpose | Changes version/CHANGELOG? |
|---|---|---|
| `build-test.bat` / `build-test.sh` | Compile Python and run local tests | No |
| `build.bat` / `build.sh` | Run the same validation, then increment the project version | Yes, after success |

Set `HYDRA_UMC_SDK_ROOT` when the SDK is not a sibling checkout. Use `build-test` during development; it is the only safe default before a real fleet-manager adapter exists.

## Adding a new script

Keep a new script in the repository root only when it is an operator entry point. Add the standard copyright header, state whether it mutates version/CHANGELOG, print numbered steps, and end `.bat` scripts with `pause`. Put reusable Python logic under `tools/`, compile it in `tools/build_test.py`, add deterministic tests (a coordinate-frame change needs a hand-checkable numeric test, not just a passing assertion) and document the command in the README and this guide. A script must not open a real transport, discover a fleet or send an order implicitly.

## Hardware acceptance gate

Before deploying an adapter: survey and document the real `FrameTransform` (origin, heading) for the target site, select the real fleet-manager interface (VDA 5050, vendor REST/WebSocket, or an open-source topic set) and its authentication, bind authenticated AMR/fleet identity, verify stale/disconnected-state behavior, test `CANCEL_ORDER` independently as a real safe fallback, and perform a bounded, low-speed bench test before any occupied-space run. The AMR's own onboard navigation (or its fleet manager) remains responsible for real-time obstacle avoidance and motion safety.
