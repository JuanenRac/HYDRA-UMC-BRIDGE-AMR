<!-- =============================================================================
HYDRA-UMC-BRIDGE-AMR - Change history
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# Changelog

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
