<!-- =============================================================================
HYDRA-UMC-BRIDGE-AMR - Contribution guide
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# Contributing

Keep this bridge a coordination layer: it maps a factory-frame coordinate to
the AMR's own local pose target and emits named order actions - real-time
obstacle avoidance, localization and motion execution remain the AMR's own
onboard authority (or its fleet manager), never this repository.

Before opening a change, run `build-test.bat` on Windows or `bash build-test.sh`
on Linux. Add a focused test for each coordinate mapping or admission rule
changed - a frame-transform change needs a hand-checkable numeric test, not
just a passing assertion. Hardware-dependent behavior must state its tested
platform/fleet manager, transport and safe failure mode; unverified hardware
support must not be presented as ready.
