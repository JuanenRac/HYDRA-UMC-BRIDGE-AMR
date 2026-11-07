# Simulation bench

The AMR bridge publishes VDA 5050 orders and instant actions. The emulator answers like a vehicle (shape-level, not a conformance test) and the schemas in `tests/vda5050_schemas` reject any message a real vehicle would not accept.

The simulation bench replaces the hardware with a VDA 5050 vehicle on the MQTT bus (`tests/vda5050_emulator.py`) and a simulated AMR, checked against the VDA 5050 JSON schemas. It proves the parsing, the gating, the transports and the failure paths of the bridge; it says nothing about
a real machine, real timing or real safety functions.

## Run it

```
python -m unittest -v <modules>
```

from the `tests` folder, with `src` of this repository and `clients/python/src` of the SDK on `PYTHONPATH` (the same layout `python tools/build_test.py` uses). The
workflow named "Simulation bench" does exactly this on a clean Linux runner on every push and pull request, by hand and once a week, with nothing but Python.

## What it runs

| File | What it exercises |
|------|-------------------|
| `tests/test_vda5050_emulator.py` | orders and instant actions against the vehicle emulator |
| `tests/test_shift_scenarios.py` | a shift of orders where the observed state of the emulated vehicle is the gate, with a field violation, an emergency stop, a manual mode, a broken connection and a silent vehicle |
| `tests/test_path_planner_graph.py` | the plant graph made from the waypoints the path planner documents as its output: the chain of nodes, one-way and two-way edges, the order parts and the refusal of an error object or a bad waypoint |
| `tests/test_vda5050_schemas.py` | every message the bridge builds, validated against the VDA 5050 schemas |
| `tests/test_simulated_amr.py` | a simulated AMR with route telemetry and interlocks |
| `tests/test_coordinator.py` | the coordinator and its gate with simulated evidence |
| `tests/test_mqtt_transport.py` | the topic dispatch, QoS, idempotency and limits without a broker |
| `tests/test_broker_integration.py` | the same dispatch against a real MQTT broker; skipped unless the broker checkout is present |
| `tests/test_global_stop_broker.py` | the global stop over a real broker; skipped unless the broker checkout is present |

The broker scenarios start a real MQTT broker from a checkout of HYDRA-UMC-MQTT-BROKER (`HYDRA_UMC_TEST_BROKER_DIR`); the simulation job does not install it, so
those cases skip themselves there and run in the normal test run on a machine that has the broker.

## What it cannot show

A real vehicle and its navigation, a real fleet manager, safety scanners and the radio link of a plant. The hardware row of the status table therefore stays at "simulated" until a person has run the bridge on a real setup.
