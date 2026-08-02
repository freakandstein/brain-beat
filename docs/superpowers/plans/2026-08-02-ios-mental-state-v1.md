# iOS Mental State Monitor v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone iOS app that connects to a Muse 2 headset via BLE, computes calm/flow/tense mental state in real time using the formulas from the design spec, and displays it live on screen.

**Architecture:** Four independent Swift modules — (1) a pure signal-processing module (Welch PSD band power via Accelerate/vDSP), (2) a pure mental-state classifier (EMA smoothing → arousal → spectrum position → vote-buffered state), (3) a CoreBluetooth acquisition layer talking to the Muse 2 GATT profile, (4) a SwiftUI screen wiring 1–3 together. Modules 1 and 2 are built and unit-tested against fixture data (derived from this repo's recorded `eeg_session_*.csv` files) before any BLE code is written, so classification correctness is verified independent of hardware.

**Tech Stack:** Swift, SwiftUI, CoreBluetooth, Accelerate (vDSP), XCTest. New standalone Xcode project — no dependency on this Python repo at build time; this repo is used only as the read-only spec/fixture source during development.

**Spec reference:** `docs/superpowers/specs/2026-08-02-ios-mental-state-v1-design.md` in this repo — all formulas, constants, and scope boundaries below are copied verbatim from there.

## Global Constraints

- Fixed tick rate: 100ms (10Hz) — the Python BPM-driven tick rate does not apply to v1; EMA/buffer constants below assume ~100ms ticks.
- EEG channels: TP9, AF7, AF8, TP10 @ 256Hz only. No PPG, no IMU/accelerometer, no gyroscope in v1.
- No gesture detection (wink/jaw/eyebrow/tilt), no OBS/keyboard/mouse control, no drum engine, no session persistence — out of scope, do not build.
- All formula constants (EMA alphas, thresholds, buffer sizes, zone bands, vote supermajority) must match the design spec exactly — these were tuned over many iterations in the Python original; do not "improve" or re-derive them.
- New Xcode project, minimum iOS target: iOS 16 (CoreBluetooth + SwiftUI stable baseline).

---

## File Structure

New Xcode project `BrainBeatMentalState` (name placeholder — user names the actual Xcode project):

```
BrainBeatMentalState/
  BrainBeatMentalState/
    App/
      BrainBeatMentalStateApp.swift       # App entry point
      ContentView.swift                    # Task 5 — live display screen
    SignalProcessing/
      BandPower.swift                      # Task 1 — Welch PSD band power
    Classification/
      MentalStateClassifier.swift          # Task 2 — EMA/arousal/vote logic
    Bluetooth/
      MuseBLEConstants.swift               # Task 3 — GATT UUIDs, packet formats
      MuseBLEManager.swift                 # Task 4 — CoreBluetooth central + parsing
  BrainBeatMentalStateTests/
    BandPowerTests.swift                   # Task 1
    MentalStateClassifierTests.swift       # Task 2
    Fixtures/
      calm_session_fixture.json            # Task 1 — derived from eeg_session_*.csv
      tense_session_fixture.json           # Task 1
```

**Responsibility split:**
- `BandPower.swift`: pure function, raw EEG samples → theta/alpha/beta power per channel. No BLE, no classifier knowledge.
- `MentalStateClassifier.swift`: pure struct/class, band power → calm/flow/tense + spectrum position. No BLE, no UI.
- `MuseBLEManager.swift`: CoreBluetooth central manager, connects to Muse 2, parses raw characteristic data into sample arrays, feeds `BandPower` → `MentalStateClassifier` on each tick, publishes state via `@Published` for SwiftUI.
- `ContentView.swift`: SwiftUI view observing `MuseBLEManager`, displays state + spectrum position, no logic.

---

## Task 1: Band Power Module (pure signal processing)

**Files:**
- Create: `BrainBeatMentalState/SignalProcessing/BandPower.swift`
- Test: `BrainBeatMentalStateTests/BandPowerTests.swift`
- Create: `BrainBeatMentalStateTests/Fixtures/calm_session_fixture.json`

**Interfaces:**
- Produces:
  ```swift
  struct BandPowerResult {
      let theta: Double
      let alpha: Double
      let beta: Double
  }

  enum BandPower {
      /// samples: raw EEG samples for one channel, sampleRate: Hz (256 for Muse 2)
      static func compute(samples: [Double], sampleRate: Double) -> BandPowerResult
  }
  ```
  Consumed by Task 2 (`MentalStateClassifier`) and Task 4 (`MuseBLEManager`).

- [ ] **Step 1: Generate a fixture file from an existing recorded session**

Pick one CSV from the repo root, e.g. `eeg_session_20260717_195213.csv` (the largest one, ~80KB, likely has more variety). Extract a ~2-second window (512 samples @ 256Hz) of raw AF7 channel values into a JSON array for the test fixture. Use a one-off Python script (not part of the iOS project) to do this extraction:

```python
import csv, json

with open("eeg_session_20260717_195213.csv") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

# Adjust column name to match actual CSV header (check first row of the file)
af7_samples = [float(r["AF7"]) for r in rows[:512] if r.get("AF7")]
with open("calm_session_fixture.json", "w") as out:
    json.dump({"sampleRate": 256.0, "af7": af7_samples}, out)
```

Run this, inspect the CSV header first with `head -1 eeg_session_20260717_195213.csv` to confirm the actual column name for AF7 before running. Copy the resulting `calm_session_fixture.json` into `BrainBeatMentalStateTests/Fixtures/`.

- [ ] **Step 2: Write the failing test**

```swift
import XCTest
@testable import BrainBeatMentalState

final class BandPowerTests: XCTestCase {
    func testComputeReturnsNonNegativePowers() throws {
        let url = Bundle(for: Self.self).url(forResource: "calm_session_fixture", withExtension: "json")!
        let data = try Data(contentsOf: url)
        let fixture = try JSONDecoder().decode(Fixture.self, from: data)

        let result = BandPower.compute(samples: fixture.af7, sampleRate: fixture.sampleRate)

        XCTAssertGreaterThanOrEqual(result.theta, 0)
        XCTAssertGreaterThanOrEqual(result.alpha, 0)
        XCTAssertGreaterThanOrEqual(result.beta, 0)
    }

    func testComputeOnConstantSignalHasNearZeroPower() {
        let samples = [Double](repeating: 100.0, count: 512)
        let result = BandPower.compute(samples: samples, sampleRate: 256.0)

        // A DC-only signal has ~zero power in theta/alpha/beta bands (4-30Hz)
        XCTAssertLessThan(result.theta, 1.0)
        XCTAssertLessThan(result.alpha, 1.0)
        XCTAssertLessThan(result.beta, 1.0)
    }
}

private struct Fixture: Decodable {
    let sampleRate: Double
    let af7: [Double]
}
```

- [ ] **Step 3: Run test to verify it fails**

Run in Xcode (Cmd+U) or `xcodebuild test -scheme BrainBeatMentalState -destination 'platform=iOS Simulator,name=iPhone 15'`.
Expected: FAIL — `BandPower` type does not exist yet.

- [ ] **Step 4: Implement BandPower using Accelerate/vDSP**

```swift
import Accelerate

struct BandPowerResult {
    let theta: Double
    let alpha: Double
    let beta: Double
}

enum BandPower {
    // Band edges in Hz, matching the Python BrainFlow DataFilter band definitions.
    private static let thetaRange = 4.0...8.0
    private static let alphaRange = 8.0...13.0
    private static let betaRange = 13.0...30.0

    static func compute(samples: [Double], sampleRate: Double) -> BandPowerResult {
        let n = samples.count
        guard n > 0 else { return BandPowerResult(theta: 0, alpha: 0, beta: 0) }

        // Detrend (remove mean) — matches BrainFlow's DetrendOperations.CONSTANT
        let mean = samples.reduce(0, +) / Double(n)
        var detrended = samples.map { $0 - mean }

        // Hann window (matches BrainFlow WindowOperations.HANNING)
        var window = [Double](repeating: 0, count: n)
        vDSP_hann_windowD(&window, vDSP_Length(n), Int32(vDSP_HANN_NORM))
        vDSP_vmulD(detrended, 1, window, 1, &detrended, 1, vDSP_Length(n))

        // FFT magnitude via vDSP (real-to-complex)
        let log2n = vDSP_Length(log2(Double(n)).rounded(.up))
        let paddedN = 1 << Int(log2n)
        var padded = detrended + [Double](repeating: 0, count: paddedN - n)

        guard let fftSetup = vDSP_create_fftsetupD(log2n, FFTRadix(kFFTRadix2)) else {
            return BandPowerResult(theta: 0, alpha: 0, beta: 0)
        }
        defer { vDSP_destroy_fftsetupD(fftSetup) }

        var realp = [Double](repeating: 0, count: paddedN / 2)
        var imagp = [Double](repeating: 0, count: paddedN / 2)
        var magnitudes = [Double](repeating: 0, count: paddedN / 2)

        realp.withUnsafeMutableBufferPointer { realPtr in
            imagp.withUnsafeMutableBufferPointer { imagPtr in
                var splitComplex = DSPDoubleSplitComplex(realp: realPtr.baseAddress!, imagp: imagPtr.baseAddress!)
                padded.withUnsafeBufferPointer { paddedPtr in
                    paddedPtr.baseAddress!.withMemoryRebound(to: DSPDoubleComplex.self, capacity: paddedN / 2) { complexPtr in
                        vDSP_ctozD(complexPtr, 2, &splitComplex, 1, vDSP_Length(paddedN / 2))
                    }
                }
                vDSP_fft_zripD(fftSetup, &splitComplex, 1, log2n, FFTDirection(FFT_FORWARD))
                vDSP_zvmagsD(&splitComplex, 1, &magnitudes, 1, vDSP_Length(paddedN / 2))
            }
        }

        let freqResolution = sampleRate / Double(paddedN)

        func bandPower(_ range: ClosedRange<Double>) -> Double {
            let lowBin = Int((range.lowerBound / freqResolution).rounded())
            let highBin = min(Int((range.upperBound / freqResolution).rounded()), magnitudes.count - 1)
            guard lowBin <= highBin else { return 0 }
            return magnitudes[lowBin...highBin].reduce(0, +)
        }

        return BandPowerResult(
            theta: bandPower(thetaRange),
            alpha: bandPower(alphaRange),
            beta: bandPower(betaRange)
        )
    }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `xcodebuild test -scheme BrainBeatMentalState -destination 'platform=iOS Simulator,name=iPhone 15'`
Expected: PASS for both `testComputeReturnsNonNegativePowers` and `testComputeOnConstantSignalHasNearZeroPower`.

- [ ] **Step 6: Commit**

```bash
git add BrainBeatMentalState/SignalProcessing/BandPower.swift BrainBeatMentalStateTests/BandPowerTests.swift BrainBeatMentalStateTests/Fixtures/calm_session_fixture.json
git commit -m "feat: add BandPower module for theta/alpha/beta computation"
```

---

## Task 2: Mental State Classifier (pure logic)

**Files:**
- Create: `BrainBeatMentalState/Classification/MentalStateClassifier.swift`
- Test: `BrainBeatMentalStateTests/MentalStateClassifierTests.swift`

**Interfaces:**
- Consumes: `BandPowerResult` from Task 1 (`theta`, `alpha`, `beta` as `Double`).
- Produces:
  ```swift
  enum MentalState: String {
      case calm, flow, tense
  }

  struct ClassifierOutput {
      let state: MentalState
      let spectrumPosition: Double  // 0.0..1.0
  }

  final class MentalStateClassifier {
      init()
      /// Call once per tick (100ms) with the latest frontal (AF7/AF8 averaged)
      /// and temporal band power. For v1, pass the same BandPowerResult for
      /// both parameters if only one channel's power is computed — using
      /// AF7/AF8 average as the primary source, matching the Python engine's
      /// frontal-channel-driven arousal calculation.
      func tick(bandPower: BandPowerResult) -> ClassifierOutput
  }
  ```
  Consumed by Task 4 (`MuseBLEManager`).

- [ ] **Step 1: Write the failing tests**

```swift
import XCTest
@testable import BrainBeatMentalState

final class MentalStateClassifierTests: XCTestCase {
    func testInitialStateIsCalm() {
        let classifier = MentalStateClassifier()
        // First tick with balanced power should not immediately jump to tense —
        // vote buffer requires 70% supermajority before ever changing away
        // from "calm", and current_state starts as "calm".
        let neutral = BandPowerResult(theta: 0.5, alpha: 0.5, beta: 0.5)
        let output = classifier.tick(bandPower: neutral)
        XCTAssertEqual(output.state, .calm)
    }

    func testSustainedHighBetaEventuallyReachesTense() {
        let classifier = MentalStateClassifier()
        let highBeta = BandPowerResult(theta: 0.1, alpha: 0.1, beta: 5.0)

        var lastOutput: ClassifierOutput!
        // Feed enough ticks to fill the 20-sample vote buffer with "tense"
        // votes and clear the 70% supermajority (>=14 of 20).
        for _ in 0..<40 {
            lastOutput = classifier.tick(bandPower: highBeta)
        }

        XCTAssertEqual(lastOutput.state, .tense)
        XCTAssertGreaterThan(lastOutput.spectrumPosition, 0.65)
    }

    func testSpectrumPositionStaysWithinBounds() {
        let classifier = MentalStateClassifier()
        let extreme = BandPowerResult(theta: 0, alpha: 0, beta: 100.0)
        for _ in 0..<10 {
            let output = classifier.tick(bandPower: extreme)
            XCTAssertGreaterThanOrEqual(output.spectrumPosition, 0.0)
            XCTAssertLessThanOrEqual(output.spectrumPosition, 1.0)
        }
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `xcodebuild test -scheme BrainBeatMentalState -destination 'platform=iOS Simulator,name=iPhone 15'`
Expected: FAIL — `MentalStateClassifier` does not exist yet.

- [ ] **Step 3: Implement MentalStateClassifier**

All constants below are copied verbatim from `docs/superpowers/specs/2026-08-02-ios-mental-state-v1-design.md` sections 1, 2, 4, 5, 6, 7.

```swift
import Foundation

enum MentalState: String {
    case calm, flow, tense
}

struct ClassifierOutput {
    let state: MentalState
    let spectrumPosition: Double
}

final class MentalStateClassifier {
    private let emaAlpha = 0.20
    private let spectrumEmaAlpha = 0.15
    private let voteBufferSize = 20
    private let arousalBufferSize = 480
    private let warmupMinSamples = 60
    private let thresholdRecalcInterval = 120

    private var emaA = 0.5
    private var emaB = 0.5
    private var emaT = 0.5
    private var emaTbr = 0.5

    private var adaptiveThreshold = 0.02
    private var arousalBuffer: [Double] = []
    private var ticksSinceThresholdUpdate = 0

    private var spectrumPosSmooth = 0.4
    private var voteBuffer: [MentalState] = []
    private var currentState: MentalState = .calm

    func tick(bandPower: BandPowerResult) -> ClassifierOutput {
        // Step 1: EMA smoothing (theta/beta ratio computed from raw band power)
        let tbrRaw = bandPower.beta > 0 ? bandPower.theta / bandPower.beta : 1.0
        emaA = emaA * (1 - emaAlpha) + bandPower.alpha * emaAlpha
        emaB = emaB * (1 - emaAlpha) + bandPower.beta * emaAlpha
        emaT = emaT * (1 - emaAlpha) + bandPower.theta * emaAlpha
        emaTbr = emaTbr * (1 - emaAlpha) + tbrRaw * emaAlpha

        // Step 2: Arousal
        let arousal = 0.50 * emaB - 0.30 * emaA - 0.20 * emaTbr

        // Step 4: Adaptive threshold
        arousalBuffer.append(arousal)
        if arousalBuffer.count > arousalBufferSize {
            arousalBuffer.removeFirst(arousalBuffer.count - arousalBufferSize)
        }
        ticksSinceThresholdUpdate += 1
        if arousalBuffer.count >= warmupMinSamples && ticksSinceThresholdUpdate >= thresholdRecalcInterval {
            let sorted = arousalBuffer.sorted()
            let median = sorted[sorted.count / 2]
            adaptiveThreshold = (median + 0.03 * 10000).rounded() / 10000
            ticksSinceThresholdUpdate = 0
        }

        // Step 5: Spectrum position
        let delta = (arousal - adaptiveThreshold) / 0.15
        let raw = min(max((delta + 1.0) / 2.0, 0.0), 1.0)
        spectrumPosSmooth += (raw - spectrumPosSmooth) * spectrumEmaAlpha

        // Step 6: Zone bands
        let rawState: MentalState
        if spectrumPosSmooth > 0.65 {
            rawState = .tense
        } else if spectrumPosSmooth >= 0.35 {
            rawState = .flow
        } else {
            rawState = .calm
        }

        // Step 7: Vote buffer with 70% supermajority
        voteBuffer.append(rawState)
        if voteBuffer.count > voteBufferSize {
            voteBuffer.removeFirst(voteBuffer.count - voteBufferSize)
        }
        let counts = Dictionary(grouping: voteBuffer, by: { $0 }).mapValues { $0.count }
        let best = counts.max(by: { $0.value < $1.value })!
        let required = max(1, Int(Double(voteBuffer.count) * 0.70))
        currentState = best.value >= required ? best.key : currentState

        return ClassifierOutput(state: currentState, spectrumPosition: spectrumPosSmooth)
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `xcodebuild test -scheme BrainBeatMentalState -destination 'platform=iOS Simulator,name=iPhone 15'`
Expected: PASS for all three tests.

- [ ] **Step 5: Commit**

```bash
git add BrainBeatMentalState/Classification/MentalStateClassifier.swift BrainBeatMentalStateTests/MentalStateClassifierTests.swift
git commit -m "feat: add MentalStateClassifier matching Python eeg_engine formulas"
```

---

## Task 3: Muse 2 BLE Constants

**Files:**
- Create: `BrainBeatMentalState/Bluetooth/MuseBLEConstants.swift`

**Interfaces:**
- Produces:
  ```swift
  enum MuseBLEConstants {
      static let serviceUUID: CBUUID
      static let eegChannelUUIDs: [String: CBUUID]  // keys: "TP9", "AF7", "AF8", "TP10"
      static let sampleRate: Double  // 256.0
  }
  ```
  Consumed by Task 4 (`MuseBLEManager`).

This task has no automated test — GATT UUIDs are hardware protocol constants
that can only be verified against a real device (done in Task 4's manual
verification step). Muse 2's standard BLE GATT UUIDs are publicly documented
by the Muse LSL / muse-js open-source projects.

- [ ] **Step 1: Write the constants file**

```swift
import CoreBluetooth

enum MuseBLEConstants {
    // Muse 2 GATT service/characteristic UUIDs, as used by the muse-js and
    // muselsl open-source Muse BLE clients.
    static let serviceUUID = CBUUID(string: "0000fe8d-0000-1000-8000-00805f9b34fb")

    static let eegChannelUUIDs: [String: CBUUID] = [
        "TP9":  CBUUID(string: "273e0003-4c4d-454d-96be-f03bac821358"),
        "AF7":  CBUUID(string: "273e0004-4c4d-454d-96be-f03bac821358"),
        "AF8":  CBUUID(string: "273e0005-4c4d-454d-96be-f03bac821358"),
        "TP10": CBUUID(string: "273e0006-4c4d-454d-96be-f03bac821358"),
    ]

    static let controlCharacteristicUUID = CBUUID(string: "273e0001-4c4d-454d-96be-f03bac821358")

    static let sampleRate: Double = 256.0
}
```

- [ ] **Step 2: Commit**

```bash
git add BrainBeatMentalState/Bluetooth/MuseBLEConstants.swift
git commit -m "feat: add Muse 2 BLE GATT constants"
```

---

## Task 4: CoreBluetooth Acquisition Layer

**Files:**
- Create: `BrainBeatMentalState/Bluetooth/MuseBLEManager.swift`
- Modify: `BrainBeatMentalState/App/Info.plist` (add Bluetooth usage description)

**Interfaces:**
- Consumes: `MuseBLEConstants` (Task 3), `BandPower.compute` (Task 1), `MentalStateClassifier` (Task 2).
- Produces:
  ```swift
  final class MuseBLEManager: NSObject, ObservableObject {
      @Published var connectionStatus: String  // "disconnected", "scanning", "connected"
      @Published var currentState: MentalState = .calm
      @Published var spectrumPosition: Double = 0.4

      func startScanning()
      func disconnect()
  }
  ```
  Consumed by Task 5 (`ContentView`).

This task has no meaningful unit test — CoreBluetooth requires real hardware
and cannot be exercised in the simulator. Verification is manual (Step 4
below). The parsing logic (raw bytes → sample values) is the one piece that
could be unit tested if Muse 2's packet byte layout is confirmed during
manual testing; if so, extract it into a standalone pure function and add a
test at that point — not blocking for this task.

- [ ] **Step 1: Add Bluetooth usage description to Info.plist**

Add to `Info.plist`:
```xml
<key>NSBluetoothAlwaysUsageDescription</key>
<string>This app connects to your Muse 2 headset to read brain activity.</string>
```

- [ ] **Step 2: Implement MuseBLEManager**

```swift
import CoreBluetooth
import Combine

final class MuseBLEManager: NSObject, ObservableObject {
    @Published var connectionStatus: String = "disconnected"
    @Published var currentState: MentalState = .calm
    @Published var spectrumPosition: Double = 0.4

    private var centralManager: CBCentralManager!
    private var musePeripheral: CBPeripheral?
    private let classifier = MentalStateClassifier()

    // Rolling sample buffers per channel — 512 samples (~2s @ 256Hz) window
    // for band power computation, matching Task 1's fixture window size.
    private var af7Buffer: [Double] = []
    private var af8Buffer: [Double] = []
    private let windowSize = 512

    override init() {
        super.init()
        centralManager = CBCentralManager(delegate: self, queue: nil)
    }

    func startScanning() {
        guard centralManager.state == .poweredOn else { return }
        connectionStatus = "scanning"
        centralManager.scanForPeripherals(withServices: [MuseBLEConstants.serviceUUID])
    }

    func disconnect() {
        if let peripheral = musePeripheral {
            centralManager.cancelPeripheralConnection(peripheral)
        }
        connectionStatus = "disconnected"
    }

    private func processTick() {
        guard af7Buffer.count >= windowSize, af8Buffer.count >= windowSize else { return }

        let af7Power = BandPower.compute(samples: Array(af7Buffer.suffix(windowSize)), sampleRate: MuseBLEConstants.sampleRate)
        let af8Power = BandPower.compute(samples: Array(af8Buffer.suffix(windowSize)), sampleRate: MuseBLEConstants.sampleRate)

        // Average AF7/AF8 (frontal channels) as the classifier input, matching
        // the Python engine's frontal-channel-driven arousal calculation.
        let averaged = BandPowerResult(
            theta: (af7Power.theta + af8Power.theta) / 2.0,
            alpha: (af7Power.alpha + af8Power.alpha) / 2.0,
            beta: (af7Power.beta + af8Power.beta) / 2.0
        )

        let output = classifier.tick(bandPower: averaged)
        DispatchQueue.main.async {
            self.currentState = output.state
            self.spectrumPosition = output.spectrumPosition
        }
    }
}

extension MuseBLEManager: CBCentralManagerDelegate {
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn {
            startScanning()
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral, advertisementData: [String: Any], rssi RSSI: NSNumber) {
        musePeripheral = peripheral
        peripheral.delegate = self
        centralManager.stopScan()
        centralManager.connect(peripheral)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        connectionStatus = "connected"
        peripheral.discoverServices([MuseBLEConstants.serviceUUID])
    }
}

extension MuseBLEManager: CBPeripheralDelegate {
    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard let services = peripheral.services else { return }
        for service in services where service.uuid == MuseBLEConstants.serviceUUID {
            peripheral.discoverCharacteristics(Array(MuseBLEConstants.eegChannelUUIDs.values), for: service)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard let characteristics = service.characteristics else { return }
        for characteristic in characteristics {
            peripheral.setNotifyValue(true, for: characteristic)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard let data = characteristic.value else { return }
        let samples = Self.parseEEGPacket(data)

        if characteristic.uuid == MuseBLEConstants.eegChannelUUIDs["AF7"] {
            af7Buffer.append(contentsOf: samples)
            if af7Buffer.count > windowSize { af7Buffer.removeFirst(af7Buffer.count - windowSize) }
        } else if characteristic.uuid == MuseBLEConstants.eegChannelUUIDs["AF8"] {
            af8Buffer.append(contentsOf: samples)
            if af8Buffer.count > windowSize { af8Buffer.removeFirst(af8Buffer.count - windowSize) }
        }

        processTick()
    }

    // Muse 2 EEG packets: 2-byte sequence ID followed by 12 samples, each
    // 12-bit unsigned, packed big-endian — matches the muse-js/muselsl
    // packet format. Values are converted to microvolts using the Muse
    // 2's known ADC scale factor.
    private static func parseEEGPacket(_ data: Data) -> [Double] {
        var samples: [Double] = []
        let bytes = [UInt8](data)
        guard bytes.count > 2 else { return samples }

        var bitBuffer: UInt32 = 0
        var bitCount = 0
        for byte in bytes.dropFirst(2) {
            bitBuffer = (bitBuffer << 8) | UInt32(byte)
            bitCount += 8
            if bitCount >= 12 {
                bitCount -= 12
                let raw = (bitBuffer >> UInt32(bitCount)) & 0xFFF
                // Muse 2 ADC scale: 0.48828125 microvolts/count, centered at 2048
                let microvolts = (Double(raw) - 2048.0) * 0.48828125
                samples.append(microvolts)
            }
        }
        return samples
    }
}
```

- [ ] **Step 3: Build the project**

Run: `xcodebuild build -scheme BrainBeatMentalState -destination 'platform=iOS Simulator,name=iPhone 15'`
Expected: BUILD SUCCEEDED (this task has no simulator-runnable test — CoreBluetooth needs real hardware).

- [ ] **Step 4: Manual verification on real device**

Install the app on a physical iPhone (CoreBluetooth does not work in the
simulator). Power on a Muse 2, launch the app, confirm `connectionStatus`
transitions `disconnected` → `scanning` → `connected`, and that
`currentState`/`spectrumPosition` update. If the packet parsing produces
implausible values (e.g. spectrum position stuck at 0 or 1, or NaN), the
`parseEEGPacket` bit-unpacking or ADC scale factor needs adjustment — cross-check
against `muselsl`'s Python source (`muselsl/muse.py` in the `muselsl` pip
package) for the exact packet layout if available.

- [ ] **Step 5: Commit**

```bash
git add BrainBeatMentalState/Bluetooth/MuseBLEManager.swift BrainBeatMentalState/App/Info.plist
git commit -m "feat: add CoreBluetooth acquisition layer for Muse 2"
```

---

## Task 5: Live Display UI

**Files:**
- Create: `BrainBeatMentalState/App/ContentView.swift`
- Modify: `BrainBeatMentalState/App/BrainBeatMentalStateApp.swift`

**Interfaces:**
- Consumes: `MuseBLEManager` (Task 4) — `@Published var connectionStatus: String`, `currentState: MentalState`, `spectrumPosition: Double`; `func startScanning()`.

- [ ] **Step 1: Implement ContentView**

```swift
import SwiftUI

struct ContentView: View {
    @StateObject private var bleManager = MuseBLEManager()

    var body: some View {
        VStack(spacing: 24) {
            Text(bleManager.connectionStatus.capitalized)
                .font(.headline)
                .foregroundStyle(.secondary)

            Text(bleManager.currentState.rawValue.capitalized)
                .font(.system(size: 48, weight: .bold))
                .foregroundStyle(color(for: bleManager.currentState))

            ProgressView(value: bleManager.spectrumPosition, total: 1.0)
                .tint(color(for: bleManager.currentState))
                .padding(.horizontal, 40)

            Text(String(format: "spectrum: %.2f", bleManager.spectrumPosition))
                .font(.caption)
                .foregroundStyle(.tertiary)

            if bleManager.connectionStatus == "disconnected" {
                Button("Connect to Muse 2") {
                    bleManager.startScanning()
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .padding()
    }

    private func color(for state: MentalState) -> Color {
        switch state {
        case .calm: return .blue
        case .flow: return .green
        case .tense: return .red
        }
    }
}

#Preview {
    ContentView()
}
```

- [ ] **Step 2: Wire up the App entry point**

```swift
import SwiftUI

@main
struct BrainBeatMentalStateApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
```

- [ ] **Step 3: Build and run in simulator to verify UI renders**

Run: `xcodebuild build -scheme BrainBeatMentalState -destination 'platform=iOS Simulator,name=iPhone 15'`
Expected: BUILD SUCCEEDED. Launch in simulator (or Xcode Preview) and confirm
the screen shows "Disconnected", a "Connect to Muse 2" button, and the
default calm-blue state text (BLE won't actually connect in the simulator,
but the initial render should be visually correct).

- [ ] **Step 4: Commit**

```bash
git add BrainBeatMentalState/App/ContentView.swift BrainBeatMentalState/App/BrainBeatMentalStateApp.swift
git commit -m "feat: add live mental state display screen"
```

---

## Self-Review Notes

- **Spec coverage**: All 7 formula steps from the design spec are implemented in Task 2. BLE acquisition (Task 3/4) and UI (Task 5) cover the components section. Out-of-scope items (gestures, OBS, persistence, PPG/IMU) have no tasks, as intended.
- **Type consistency**: `BandPowerResult` (Task 1) is consumed identically in Task 2's `tick(bandPower:)` and Task 4's `processTick()`. `MentalState` and `ClassifierOutput` (Task 2) are consumed identically in Task 4 and published as-is to Task 5's `ContentView`.
- **Known risk flagged explicitly**: Task 4's `parseEEGPacket` byte layout and ADC scale factor are based on the commonly documented Muse 2 protocol but are unverified against real hardware — Step 4 of Task 4 calls this out as the point where it may need correction, and points to `muselsl`'s Python source as the cross-check reference.
