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
| 8 | With `HYDRA_UMC_AMR_PLANT_GRAPH` set to the real graph, send a job with `goal_node` three nodes away. | One order with every node and edge of the shortest route; the vehicle drives it lane by lane and the state shows each node reached. | not executed |
| 9 | Send a job with a `goal_node` that is not in the graph, and one while the vehicle reports no `lastNodeId`. | Both are refused with a reason; nothing is sent to the vehicle. | not executed |
| 10 | With `HYDRA_UMC_AMR_PUBLISH_STATE=1`, read `hydra/bridges/amr/state` while the vehicle drives and discharges. | The battery, position and error counts follow the vehicle and the data lake shows them; with the vehicle switched off nothing new is published. | not executed |
| 11 | With `HYDRA_UMC_AMR_PUBLISH_OPCUA=1`, read `hydra/bridges/amr/opcua` through the gateway of the plant. | A SCADA browses `Fleet/<vehicle>/...` with the right types, read-only. | not executed |
| 12 | With `HYDRA_UMC_AMR_ZONE_INTERLOCK=1`, occupy the zone of the next node of a driving vehicle. | The vehicle pauses before the zone; when the zone is freed it goes on. A vehicle paused by hand stays paused. | not executed |
| 13 | Stop publishing the occupancy of the zones while a vehicle approaches a zone. | After the maximum age the vehicle is paused (default `pause`), and goes on when the occupancy is heard again and free. | not executed |

## What this bench cannot show

Whether the real fleet manager accepts a multi-node order the way the schema check says, how an OPC UA server reads `hydra/bridges/amr/opcua` (the server does not subscribe to it yet) and which service publishes the occupancy of the zones (the safety service does not publish `hydra/safety/zones/occupied` yet) are not part of this bench.
