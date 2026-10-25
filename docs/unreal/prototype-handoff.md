# WiSense Unreal Integration — WebSocket & JSON Protocol Handoff

> [!WARNING]
> ### SUPERSEDED PROTOCOL — HISTORICAL PROTOTYPE HANDOFF
> **Notice:** The 2-field JSON protocol described in this document (`{"zone": <int>, "occupied": <bool>}`) has been **SUPERSEDED** by the authoritative WiSense-Twin wire specification: **`StateMessage` v1**.
>
> For the authoritative protocol specification, schema definitions, and invariant rules, see:
> [`backend_pipeline/docs/architecture/communication-contracts.md`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/backend_pipeline/docs/architecture/communication-contracts.md).
>
> This document is preserved below for architectural provenance and historical reference only. Unreal client protocol reconciliation is scheduled for Section 17.

---

## 1. Purpose

This document describes the communication contract and Unreal Engine implementation established for the WiSense digital twin.

A separate Unreal Engine 5.6 C++ test project was created to verify the protocol independently before integrating it with the main WiSense system.

The goal was to establish a simple, stable interface between the Python backend and Unreal.

The architecture is:

```text
ESP32 Sensors
     |
     v
Python Backend
     |
     | WebSocket
     v
Unreal Engine Digital Twin
```

**Python is the WebSocket server. Unreal is the WebSocket client.**

The Python backend owns the actual sensor/occupancy data. Unreal consumes that data and updates the digital twin.

---

# 2. Communication Protocol

The protocol was deliberately kept simple.

Every WebSocket message represents an update to **one zone**.

The JSON format is:

```json
{
  "zone": 2,
  "occupied": true
}
```

There are exactly two fields:

| Field | Type | Description |
|---|---|---|
| `zone` | integer | ID of the zone being updated |
| `occupied` | boolean | Whether that zone is currently occupied |

## 2.1 Occupied

```json
{
  "zone": 2,
  "occupied": true
}
```

Meaning:

> Zone 2 is occupied.

## 2.2 Empty

```json
{
  "zone": 2,
  "occupied": false
}
```

Meaning:

> Zone 2 is empty.

---

# 3. Incremental Updates

The backend does **not** need to send the complete state of every zone whenever something changes.

Only the zone whose state changed needs to be sent.

For example, if Zone 3 changes from empty to occupied:

```json
{
  "zone": 3,
  "occupied": true
}
```

That's the complete message.

There is no need to send:

```json
{
  "zone1": false,
  "zone2": false,
  "zone3": true,
  "zone4": false
}
```

The Unreal application maintains the state of its zones locally.

This makes the protocol small and easy to extend.

---

# 4. Zone IDs

The current test environment contains four zones:

```text
Zone 1
Zone 2
Zone 3
Zone 4
```

The zones are arranged approximately as:

```text
+-----------+-----------+
|           |           |
|  Zone 1   |  Zone 2   |
|           |           |
+-----------+-----------+
|           |           |
|  Zone 3   |  Zone 4   |
|           |           |
+-----------+-----------+
```

The current Unreal positions are:

```cpp
Zone 1: FVector(-300.0f, -300.0f, 50.0f)
Zone 2: FVector( 300.0f, -300.0f, 50.0f)
Zone 3: FVector(-300.0f,  300.0f, 50.0f)
Zone 4: FVector( 300.0f,  300.0f, 50.0f)
```

These coordinates are only for the current test scene. They are **not part of the communication protocol**.

The important part is the zone ID.

---

# 5. WebSocket Architecture

The WebSocket connection is:

```text
Python Backend
      |
      | WebSocket
      |
      v
Unreal Engine
```

The Unreal client connects to:

```text
ws://127.0.0.1:8765
```

For the current local test, both programs run on the same machine.

In the eventual deployment, `127.0.0.1` will be replaced with the actual address/host of the Python backend if the backend and Unreal run on different machines.

---

# 6. Unreal Implementation

A C++ Actor named:

```text
AWiSenseManager
```

was created.

The manager is placed directly into the Unreal level.

It is responsible for:

- Creating the four zones
- Storing references to the zone mesh components
- Connecting to the WebSocket server
- Receiving WebSocket messages
- Parsing JSON
- Extracting `zone`
- Extracting `occupied`
- Updating the corresponding zone

There is no Blueprint architecture involved.

The communication and zone logic are handled in C++.

---

# 7. Unreal WebSocket Module

The Unreal project uses Unreal's built-in WebSockets module.

The module dependency is:

```csharp
"WebSockets"
```

The implementation includes:

```cpp
#include "WebSocketsModule.h"
#include "IWebSocket.h"
```

The WebSocket is created with:

```cpp
const FString ServerURL = TEXT("ws://127.0.0.1:8765");
const FString ServerProtocol = TEXT("ws");

WebSocket = FWebSocketsModule::Get().CreateWebSocket(
    ServerURL,
    ServerProtocol
);
```

The client registers callbacks for:

- Connection success
- Connection failure
- Connection closed
- Incoming messages

The WebSocket is event-driven.

There is no need for a Tick function or per-frame polling.

---

# 8. WebSocket Lifecycle

The sequence is:

```text
Unreal starts
     |
     v
AWiSenseManager::BeginPlay()
     |
     +--> SpawnZones()
     |
     +--> ConnectWebSocket()
              |
              v
       WebSocket connection
              |
              v
       OnWebSocketConnected()
```

When a message arrives:

```text
WebSocket
    |
    v
OnWebSocketMessage()
    |
    v
Parse JSON
    |
    +--> zone
    |
    +--> occupied
    |
    v
UpdateZone()
```

---

# 9. JSON Processing in Unreal

Incoming messages are received as `FString`.

Example:

```text
{"zone":2,"occupied":true}
```

The message is parsed using Unreal's JSON system:

```cpp
TSharedPtr<FJsonObject> JsonObject;

TSharedRef<TJsonReader<>> Reader =
    TJsonReaderFactory<>::Create(Message);

FJsonSerializer::Deserialize(Reader, JsonObject);
```

The zone is read as a number:

```cpp
double ZoneNumber;

JsonObject->TryGetNumberField(
    TEXT("zone"),
    ZoneNumber
);
```

The occupancy state is read as a boolean:

```cpp
bool Occupied;

JsonObject->TryGetBoolField(
    TEXT("occupied"),
    Occupied
);
```

The zone number is then converted to an Unreal integer:

```cpp
const int32 ZoneID = static_cast<int32>(ZoneNumber);
```

Finally:

```cpp
UpdateZone(ZoneID, Occupied);
```

---

# 10. Zone Storage

The manager maintains the zones using:

```cpp
TMap<int32, UStaticMeshComponent*> Zones;
```

The map effectively looks like:

```text
1 -> Zone 1 mesh
2 -> Zone 2 mesh
3 -> Zone 3 mesh
4 -> Zone 4 mesh
```

This allows an incoming message such as:

```json
{
  "zone": 3,
  "occupied": true
}
```

to directly find the corresponding Unreal component.

---

# 11. Visual Representation

The initial implementation represented occupancy by changing the height of a cube.

However, this was changed.

The current behavior is:

```text
occupied = true
    |
    v
Cube visible


occupied = false
    |
    v
Cube hidden
```

Therefore:

```json
{
  "zone": 2,
  "occupied": true
}
```

makes Zone 2 appear.

And:

```json
{
  "zone": 2,
  "occupied": false
}
```

makes Zone 2 disappear.

The zone itself still exists internally; only its visibility changes.

---

# 12. Test WebSocket Server

A temporary Python script named:

```text
test_ws.py
```

was created.

Its purpose is to simulate the real Python backend.

It is **not** intended to be the final backend.

It allows the Unreal implementation to be tested independently.

The server listens on:

```text
ws://127.0.0.1:8765
```

The test server provides a command-line prompt where JSON messages can be typed manually.

---

# 13. Important Python Async Detail

The first version of the test server used normal Python:

```python
input()
```

inside the async code.

That blocked the asyncio event loop.

The working implementation uses:

```python
await asyncio.to_thread(input, "> ")
```

This allows the console input to run without blocking the WebSocket event loop.

The working test server is:

```python
import asyncio
import websockets

connected_client = None


async def handler(websocket):
    global connected_client

    connected_client = websocket

    print("Unreal connected.")

    try:
        await websocket.wait_closed()
    finally:
        connected_client = None
        print("Unreal disconnected.")


async def input_loop():
    while True:
        message = await asyncio.to_thread(input, "> ")

        if connected_client is None:
            print("No Unreal client connected.")
            continue

        await connected_client.send(message)

        print(f"Sent: {message}")


async def main():
    print("Starting WebSocket server on ws://127.0.0.1:8765")

    async with websockets.serve(
        handler,
        "127.0.0.1",
        8765
    ):
        print("Waiting for Unreal...")

        await input_loop()


asyncio.run(main())
```

---

# 14. Running the Test Server

The machine has multiple Python installations, so the working Python 3.12 executable is explicitly used.

Run:

```powershell
& "C:\Users\haris\AppData\Local\Programs\Python\Python312\python.exe" test_ws.py
```

Expected output:

```text
Starting WebSocket server on ws://127.0.0.1:8765
Waiting for Unreal...
```

After Unreal starts and connects:

```text
Unreal connected.
>
```

The prompt can then be used to send JSON directly to Unreal.

---

# 15. Testing Zone Updates

With Unreal running and connected:

Send:

```json
{"zone":2,"occupied":true}
```

Expected result:

```text
Zone 2 appears.
```

Then send:

```json
{"zone":2,"occupied":false}
```

Expected result:

```text
Zone 2 disappears.
```

All four zones can be tested:

```json
{"zone":1,"occupied":true}
```

```json
{"zone":2,"occupied":true}
```

```json
{"zone":3,"occupied":true}
```

```json
{"zone":4,"occupied":true}
```

Then:

```json
{"zone":1,"occupied":false}
```

```json
{"zone":2,"occupied":false}
```

```json
{"zone":3,"occupied":false}
```

```json
{"zone":4,"occupied":false}
```

---

# 16. Successful End-to-End Test

The complete test was successfully performed.

Python produced:

```text
Starting WebSocket server on ws://127.0.0.1:8765
Waiting for Unreal...
Unreal connected.
```

Messages were entered:

```json
{"zone":2,"occupied":false}
```

```json
{"zone":2,"occupied":true}
```

```json
{"zone":2,"occupied":false}
```

The WebSocket connection and message delivery were successfully verified.

Unreal's log showed:

```text
WiSense: Spawned Zone 1
WiSense: Spawned Zone 2
WiSense: Spawned Zone 3
WiSense: Spawned Zone 4
WiSense: Connecting to ws://127.0.0.1:8765
WiSense: WebSocket connected.
```

After fixing the asynchronous Python test server, Unreal successfully received the messages and updated the corresponding zone.

Therefore the complete pipeline has been verified:

```text
Python
  |
  | JSON over WebSocket
  v
Unreal
  |
  | JSON parsing
  v
Zone lookup
  |
  v
Visibility update
```

---

# 17. Protocol Contract for the Backend Team

The backend should implement the following contract.

## WebSocket

Unreal connects as a WebSocket client.

The backend should expose a WebSocket server.

Current test endpoint:

```text
ws://127.0.0.1:8765
```

For deployment, the address may change.

## Message direction

Messages travel:

```text
Backend -> Unreal
```

The backend sends occupancy updates.

## Message format

Every update is:

```json
{
  "zone": <integer>,
  "occupied": <boolean>
}
```

## Example

If the sensor detects that Zone 4 became occupied:

```json
{
  "zone": 4,
  "occupied": true
}
```

If Zone 4 becomes empty:

```json
{
  "zone": 4,
  "occupied": false
}
```

---

# 18. Data Type Requirements

The backend must send actual JSON types.

Correct:

```json
{
  "zone": 2,
  "occupied": true
}
```

Incorrect:

```json
{
  "zone": "2",
  "occupied": "true"
}
```

The difference is important.

`zone` must be a JSON number/integer.

`occupied` must be a JSON boolean.

Do not encode them as strings.

---

# 19. Invalid / Unsupported Formats

Do not use alternate field names unless the Unreal protocol is intentionally changed.

For example, this is not the current protocol:

```json
{
  "id": 2,
  "status": true
}
```

Nor:

```json
{
  "zone": 2,
  "state": "occupied"
}
```

Nor:

```json
{
  "zone_id": 2,
  "occupied": 1
}
```

The agreed format is:

```json
{
  "zone": 2,
  "occupied": true
}
```

---

# 20. Why the Protocol Is Incremental

The protocol represents state changes rather than snapshots.

Suppose the current state is:

```text
Zone 1 = empty
Zone 2 = occupied
Zone 3 = empty
Zone 4 = empty
```

If Zone 3 becomes occupied, the backend only sends:

```json
{
  "zone": 3,
  "occupied": true
}
```

Unreal changes its local representation of Zone 3.

This avoids unnecessary network traffic and keeps the protocol simple.

---

# 21. Current Responsibilities

## ESP32

Responsible for:

- Reading physical sensors
- Determining the relevant zone
- Providing sensor data to the backend

## Python Backend

Responsible for:

- Receiving/processing ESP32 sensor data
- Determining occupancy state
- Hosting the WebSocket server
- Sending occupancy updates to Unreal

## Unreal

Responsible for:

- Connecting to the backend's WebSocket server
- Receiving JSON
- Validating/parsing the JSON
- Finding the corresponding zone
- Updating the digital twin visualization

The responsibilities are intentionally separated.

---

# 22. Important Architectural Rule

The backend should **not** need to know how Unreal visually represents a zone.

The backend only communicates:

```text
Zone X is occupied
```

or:

```text
Zone X is empty
```

Unreal decides how to visualize that state.

Currently:

```text
occupied -> visible cube
empty    -> hidden cube
```

This can later be changed without changing the JSON protocol.

For example, Unreal could eventually use:

- Different colors
- Materials
- Animated indicators
- 3D models
- UI labels
- Lighting
- Heatmaps

The backend protocol remains the same.

---

# 23. Current Test Project

The test project was created separately from the main WiSense project to avoid introducing unfinished networking code into the team's primary project.

The important Unreal class is:

```text
AWiSenseManager
```

The manager is an Actor placed in the level.

The test level contains the four dynamically created zones.

The cubes use Unreal's built-in basic cube mesh:

```text
/Engine/BasicShapes/Cube.Cube
```

The zones are created at runtime rather than manually placed in the level.

---

# 24. No Tick Required

The manager does not need Unreal's Tick system for WebSocket communication.

The WebSocket API is event-driven.

Instead of checking every frame:

```text
Tick
  -> check socket
  -> check socket
  -> check socket
```

the implementation waits for WebSocket events:

```text
Message arrives
      |
      v
OnWebSocketMessage()
```

This is the preferred approach for the current communication system.

---

# 25. Current State of the Integration

The Unreal-side proof of concept is complete.

Verified:

- [x] Unreal C++ project
- [x] `AWiSenseManager`
- [x] Runtime zone spawning
- [x] Four zone IDs
- [x] WebSockets module
- [x] WebSocket connection
- [x] Python WebSocket test server
- [x] JSON transmission
- [x] JSON parsing
- [x] Zone ID lookup
- [x] Occupancy state handling
- [x] Zone visibility updates
- [x] End-to-end communication

The remaining work is integration with the actual WiSense Python backend and ESP32 data source.

---

# 26. Short Version for the Backend Developer

If the backend developer only needs the protocol:

```text
Unreal is a WebSocket CLIENT.

Python backend is a WebSocket SERVER.

Unreal connects to:

ws://<backend-ip>:8765

Backend sends JSON messages to Unreal.

Message format:

{"zone": 2, "occupied": true}

or:

{"zone": 2, "occupied": false}

`zone` = integer zone ID.
`occupied` = JSON boolean.

Only send the zone that changed.

Example:

Zone 3 becomes occupied:
{"zone": 3, "occupied": true}

Zone 3 becomes empty:
{"zone": 3, "occupied": false}

Current test zones:
1, 2, 3, 4.

Do not send `zone` or `occupied` as strings.
```

---

# 27. Final Protocol Summary

The entire communication contract can be summarized as:

```text
             WebSocket
Python Backend ─────────────────> Unreal
    SERVER                         CLIENT

                  JSON

        {
          "zone": 2,
          "occupied": true
        }

                  |

                  v

             Zone 2 visible
```

and:

```text
        {
          "zone": 2,
          "occupied": false
        }

                  |

                  v

             Zone 2 hidden
```

**This is the protocol currently implemented and successfully tested on the Unreal side.**
