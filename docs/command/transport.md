# Command transport: stations, hub and broker

Stations and the hub talk through one MQTT broker (Mosquitto) over mutual TLS. Every message is an
[envelope](schemas/envelope-v1.schema.json). The hub is a mode of the RF Sentinel dashboard, not a separate service.

## Identities and topics

- Each station's certificate common name is its identity: `<hostname>/<arch>` (for example `node-7/aarch64`). The
  broker uses it as the MQTT username.
- The hub's certificate common name is `rfs-hub`.
- Topics: `rfs/v1/<site>/stations/<hostname>/<arch>/<kind>`.
  - Stations publish `observation`, `heartbeat`, `capabilities`, `mission_ack`, `mission_status` and
    `mission_result`, only under their own identity.
  - Only the hub publishes `mission_request`, and a station reads only its own.
- The broker accepts TLS connections with a site certificate only. There is no plain-text listener and no anonymous
  access.

The hub also checks every envelope. Its `station.id` and `kind` must match the topic it arrived on, and duplicates
(same envelope id) are kept once. The hub records its own receive time next to the station's source time and clock
state.

## Missions (this release)

A station screens each mission request: it must be addressed to this station, unexpired (TTL from its source time)
and not already seen (idempotency key). It answers every request with a `mission_ack`. Running remote missions is not
enabled yet, so accepted requests are recorded and acknowledged as "not run". Running them comes with the mission
broker, which goes through the station's own policy gateway and approvals.

## Setup

On the machine that holds the site CA (keep `ca.key` there only):

```
python -m rf_platform.command pki init site-pki --broker-host <broker name or address>
python -m rf_platform.command pki issue site-pki --hub
python -m rf_platform.command pki issue site-pki --station --hostname <station hostname> --arch <x86_64|aarch64>
python -m rf_platform.command broker-config site-pki --port 8883 --persistence site-pki/data
mosquitto -c site-pki/mosquitto.conf
```

`python -m rf_platform.command identity` prints the identity a station will use. Copy `ca.crt` and the station's
`station-<hostname>-<arch>.crt` and `.key` to that station.

Start each dashboard in its role:

```
# hub
RF_SENTINEL_COMMAND=hub RF_SENTINEL_COMMAND_PKI=<dir with ca.crt, hub.crt, hub.key> \
RF_SENTINEL_MQTT_HOST=<broker> RF_SENTINEL_SITE=<site> python -m rf_platform.ui

# station
RF_SENTINEL_COMMAND=station RF_SENTINEL_COMMAND_PKI=<dir with ca.crt and the station's certificate and key> \
RF_SENTINEL_MQTT_HOST=<broker> RF_SENTINEL_SITE=<site> python -m rf_platform.ui
```

- A station publishes a heartbeat every 15 s, its capabilities when it connects, and a bounded summary of each agent
  detection (never IQ).
- `GET /api/command/status` shows the link on either side. On the hub it also shows station health:
  - `online`;
  - `stale` (no heartbeat recently);
  - `offline` (the broker delivered the station's last will).
- `GET /api/command/messages` lists recent envelopes on the hub.

A station that is stale or offline is not reporting. That says nothing about what is or is not on the air there.

## Tests

`tests/test_command_transport.py` starts a real Mosquitto broker with generated certificates. It checks that:
- an observation arrives with a receive time;
- redelivery is kept once;
- a station cannot publish as another station or send missions;
- expired and replayed missions are rejected, and accepted ones are acknowledged;
- the last will marks a station offline;
- nothing connects without a site certificate;
- both dashboard modes work.
