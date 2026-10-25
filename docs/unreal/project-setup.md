# Unreal Engine 5 Digital Twin — Project Setup & Architecture

## 1. Overview & Context

This document details the configuration, architecture, and manual verification procedures for the WiSense Unreal Engine digital twin client located in `unreal_digital_twin/`.

In Section 16, a working Unreal client prototype originally developed and verified outside this repository (in Unreal Engine 5.6) was adopted as the foundational project base. This prototype supersedes the earlier planned empty scaffold (`WiSenseTwin` under UE 5.8).

> [!NOTE]
> Unreal Engine is not installed on the primary pipeline development environment. All Unreal Engine compilation, project generation, and editor verification are conducted on a dedicated Unreal machine and validated using the manual verification checklist in Section 5.

---

## 2. Engine Version & Technical Evidence

* **Target Engine Version:** **Unreal Engine 5.6**
* **Evidence from Target Configuration Files:**
  The Unreal Build Tool target files `Source/WiSense.Target.cs` and `Source/WiSenseEditor.Target.cs` contain the following explicit settings:
  ```csharp
  DefaultBuildSettings = BuildSettingsVersion.V5;
  IncludeOrderVersion = EngineIncludeOrderVersion.Unreal5_6;
  ExtraModuleNames.Add("WiSense");
  ```
  `EngineIncludeOrderVersion.Unreal5_6` confirms that the codebase is authored against the UE 5.6 include hierarchy and build system rules.

---

## 3. Module & Class Architecture

### 3.1 Primary Game Module
* **Module Name:** `WiSense`
* **Declaration:** `Source/WiSense/WiSense.Build.cs`
* **Implementation:** `Source/WiSense/WiSense.cpp` using `IMPLEMENT_PRIMARY_GAME_MODULE(FDefaultGameModuleImpl, WiSense, "WiSense")`.
* **Module Dependencies:**
  * **Public:** `Core`, `CoreUObject`, `Engine`, `InputCore`, `EnhancedInput`
  * **Private:** `Json`, `WebSockets`

### 3.2 Primary Actor
* **Class Name:** `AWiSenseManager` (`Source/WiSense/Public/WiSenseManager.h`, `Source/WiSense/Private/WiSenseManager.cpp`)
* **Inheritance:** `AActor`
* **Lifecycle:**
  * `BeginPlay()`: Programmatically spawns zone representation meshes via `SpawnZones()`, then initializes the WebSocket connection via `ConnectWebSocket()`.
  * `EndPlay()`: Closes and resets the WebSocket connection cleanly.
  * `PrimaryActorTick.bCanEverTick = false`: The actor is fully event-driven (driven by WebSocket message callbacks) and does not incur per-frame tick overhead.

### 3.3 Visual Representation (C++-Only, Runtime-Spawned)
* The prototype does not rely on pre-placed level assets.
* Static meshes are loaded dynamically at runtime via `LoadObject<UStaticMesh>(nullptr, TEXT("/Engine/BasicShapes/Cube.Cube"))`.
* Four zones are instantiated as `UStaticMeshComponent` instances at spatial offsets:
  * Zone 1: `FVector(-300.0f, -300.0f, 50.0f)`
  * Zone 2: `FVector( 300.0f, -300.0f, 50.0f)`
  * Zone 3: `FVector(-300.0f,  300.0f, 50.0f)`
  * Zone 4: `FVector( 300.0f,  300.0f, 50.0f)`

---

## 4. Known Issues & Architectural Constraints

1. **All Four Cubes Start Visible (Known Issue):**
   * Upon spawning in `SpawnZones()`, Unreal Engine static mesh components are visible by default.
   * Consequently, before any incoming WebSocket message arrives, all four zone cubes render as visible.
   * *Resolution:* This behavior is preserved byte-identical in Section 16 (no behavioral modifications permitted) and will be resolved in Section 17 (Contract Reconciliation) by setting initial zone visibility to hidden until active motion/presence is received.
2. **Protocol Deprecation in Prototype:**
   * The prototype currently parses a legacy 2-field JSON format: `{"zone": <int>, "occupied": <bool>}`.
   * Per [`communication-contracts.md`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/backend_pipeline/docs/architecture/communication-contracts.md), the authoritative wire protocol is `StateMessage` v1 (`schema_version`, `timestamp`, `zone_id`, `is_motion`, `presence`, `separation_margin`, `calibration_version`).
   * Protocol reconciliation occurs in Section 17.
3. **Project Files Integrity:**
   * `WiSense.uproject`, `Config/*.ini`, and `Content/` originate from the prototype machine. Per project instructions, `WiSense.uproject` is never invented or fabricated on machines without Unreal Engine.

---

## 5. How to Generate Project Files & Build

On a machine with **Unreal Engine 5.6** and **Visual Studio 2022** (with C++ Game Development workload) installed:

1. **Verify Files:** Ensure `WiSense.uproject` and `Source/` exist inside `unreal_digital_twin/`.
2. **Generate Project Files:**
   * Right-click `WiSense.uproject` in Windows Explorer and select **Generate Visual Studio project files**.
   * *Or via CLI:*
     ```bat
     "<UnrealEngine_5.6_Path>\Engine\Binaries\DotNET\UnrealBuildTool\UnrealBuildTool.exe" -projectfiles -project="<Path>\unreal_digital_twin\WiSense.uproject" -game -engine -progress
     ```
3. **Build Solution:**
   * Open the generated `WiSense.sln` in Visual Studio 2022.
   * Set configuration to `Development Editor` and platform to `Win64`.
   * Press **Build Solution** (`Ctrl+Shift+B`).
4. **Launch Editor:**
   * Launch `WiSense.uproject` directly in Unreal Editor 5.6, or run under the Visual Studio debugger (`F5`).

---

## 6. Manual Verification Checklist (Unreal Machine)

The following checklist must be executed and confirmed on the Unreal machine prior to marking Section 16 as fully done:

- [ ] **Prerequisites:**
  - [ ] Unreal Engine 5.6 is installed.
  - [ ] Visual Studio 2022 with MSVC v143 and Windows 10/11 SDK is installed.
- [ ] **Project Generation:**
  - [ ] `WiSense.uproject` is placed at `unreal_digital_twin/WiSense.uproject`.
  - [ ] "Generate Visual Studio project files" completes with exit code 0 and creates `WiSense.sln`.
- [ ] **Compilation:**
  - [ ] Visual Studio builds `WiSense` module in `Development Editor | Win64` with 0 compile errors and 0 link errors.
- [ ] **Editor Startup:**
  - [ ] Unreal Editor 5.6 launches without missing plugin or missing module warnings.
  - [ ] The `WiSense` C++ classes appear under C++ Classes in the Content Browser.
- [ ] **Runtime Execution (PIE):**
  - [ ] `AWiSenseManager` is placed in the active level.
  - [ ] Pressing **Play in Editor (PIE)** spawns all four cubes at their designated spatial offsets.
  - [ ] **Known Issue Verification:** All four cubes appear visible immediately upon spawning.
  - [ ] Output Log confirms:
    ```text
    WiSense: Spawned Zone 1
    WiSense: Spawned Zone 2
    WiSense: Spawned Zone 3
    WiSense: Spawned Zone 4
    WiSense: Connecting to ws://127.0.0.1:8765
    ```
  - [ ] Graceful failure is verified: If the Python backend WebSocket server is not running, the Output Log logs a connection error without crashing the Unreal Editor.
