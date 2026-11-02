# Bench tests

Every test below is **not executed**: the bridge has run against the VDA 5050 emulator and the simulated vehicle of its tests, never against a real vehicle. Write the result next to a step only after a person ran it.

## What the software already covers (no vehicle needed)

- The unit tests, the VDA 5050 schema checks, the emulator in `tests/vda5050_emulator.py` and the broker integration test run with `python tools/hydra_project_tool.py test`.
- The global stop is checked with the simulated vehicle and with a broker.

## Setup

- An AGV or AMR whose controller speaks VDA 5050 over MQTT, on a floor with free space and with its physical e-stop within reach.
- An MQTT broker that both the vehicle and the bridge reach, with the credentials of the bridge in the environment.
- The job entry on `cmd/job` enabled with `HYDRA_UMC_AMR_JOB_ENTRY` and the global stop with `HYDRA_UMC_AMR_GLOBAL_STOP`, as the guide says.

## Steps

| # | Step | Expected result | Result |
|---|------|-----------------|--------|
| 1 | Start the bridge with the vehicle idle. | The published `state` shows the vehicle as idle with an `observed_at` that advances. | not executed |
| 2 | Send an order with two nodes 2 m apart. | The vehicle drives to the second node; the order id and the sequence number match in the state. | not executed |
| 3 | Send the same job twice with the same idempotency key. | The second answer is the stored one and the vehicle does not repeat the order. | not executed |
| 4 | Send a state message larger than `HYDRA_UMC_AMR_MAX_STATE_BYTES`. | It is refused; the bridge keeps running. | not executed |
| 5 | Send a global stop while the vehicle drives. | The vehicle pauses within the time its controller documents. | not executed |
| 6 | Stop the vehicle controller. | The bridge reports the vehicle as unavailable (Last Will or stale state) and refuses new jobs. | not executed |
| 7 | Stop the broker for 30 s and start it again. | The bridge reconnects by itself and publishes a fresh state. | not executed |

## What this bench cannot show

Planning orders from a plant graph, publishing the battery and the errors to a data store, exposing the fleet to an OPC UA client and pausing a vehicle from a zone interlock are not part of the bridge yet; there is nothing to test for them.
