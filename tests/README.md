# Running the tests

These tests check the colour calculations, profile files, editor behaviour,
measurement workflow, watchdog decisions and distribution files.

Run commands from the repository root, not from this folder. Examples below use
PowerShell on Windows and the project's local Python environment.

## Requirements

- Prepare the project environment with **1- Install & Run.bat** if you have not
  already done so. Tests use the same dependencies as the source application.
- Use 64-bit Windows for the complete set of Windows layout and watchdog checks.
- Keep Windows PowerShell available for the embedded-script tests. Some watchdog
  tests also use `cscript.exe` with harmless temporary child processes.

A colorimeter, ArgyllCMS installation and HDR monitor are not required. Instruments,
display surfaces and profile-changing Windows calls are replaced by test fixtures.
The tests do not install the watchdog or apply real colour profiles.

## Run everything

```powershell
.venv\Scripts\python.exe tools/run_tests.py
```

Use the project's runner instead of a single `unittest discover` process. It runs
modules separately and gives each GUI test class its own process, avoiding shared
Qt state between classes. It sets `QT_QPA_PLATFORM=offscreen`, so GUI tests do not
open visible windows.

Three groups run at a time by default. To run one at a time:

```powershell
.venv\Scripts\python.exe tools/run_tests.py --jobs 1
```

`--jobs` accepts 1 through 4. Each group has a 180-second timeout. Temporary logs
and results are removed after the run. Normal output appears at the end; failures
include the group name and the end of its log.

The portable build script also runs this suite before compiling, using its own
`.build-venv` environment.

## Run a module or a GUI class

Use the filename without `.py`:

```powershell
.venv\Scripts\python.exe tools/run_tests.py --group test_gamut
.venv\Scripts\python.exe tools/run_tests.py --group test_packaging
```

For `test_gui.py`, choose a class to keep its fixtures isolated:

```powershell
.venv\Scripts\python.exe tools/run_tests.py --group test_gui.ProfileBindingTests
```

Repeat `--group` to select several groups:

```powershell
.venv\Scripts\python.exe tools/run_tests.py --jobs 1 --group test_gamut --group test_gui.ProfileBindingTests
```

You can also select one test method using its full name:

```powershell
.venv\Scripts\python.exe tools/run_tests.py --group test_gamut.WideGamutTests.test_neutral_white_and_linear_scaling
```

## Read the result

The final line has this form:

```text
<count> tests; <count> skipped; <count> isolated groups; <count> failed
```

- **Pass:** `0 failed` and process exit code `0`.
- **Failure:** `FAIL: <group>` with details and a nonzero exit code. A timeout is
  also a failure.
- **Skipped:** a test was not run, usually because its platform or dependency was
  unavailable. A skip can accompany exit code `0`; it is not evidence that the
  skipped feature passed. A complete run on the prepared Windows environment
  should have `0 skipped`.

To inspect the exit code immediately after the command:

```powershell
$LASTEXITCODE
```

The total depends on the selected groups and the repository version. For each
file below, the expected result is that all included assertions pass. Rejection
tests pass when invalid input is rejected, not when it is accepted.

## Python test files

| File | What it checks | Expected behaviour |
| --- | --- | --- |
| `test_app_split_guard.py` | Imports and calls in modules split out of the main window. | Those modules cannot bypass the mocked Windows/hardware calls used by GUI tests. |
| `test_core.py` | Tone and colour transforms, ICC/MHC2 layout and merging, display structures, profile naming and state normalization. | Valid profiles retain consistent data; gamma uses the intended desktop curve; malformed state and incompatible profile data are handled correctly. |
| `test_ddc.py` | DDC/CI reads, retries, physical-monitor structures and handle release through a fake link. | Supported values are read, failed reads are handled and handles are released, including on errors. |
| `test_delta_itp.py` | Colour-difference calculations and their mathematical properties. | Identical colours have zero difference, neutral white stays achromatic and the metric behaves consistently across luminance levels. |
| `test_edid.py` | EDID blocks, declared luminance, chromaticities and display-path parsing. | Valid declarations decode correctly; missing or malformed data is not treated as a measured value. |
| `test_elevation.py` | Administrator detection and relaunch command construction, using injected Windows calls. | Commands and arguments are preserved; cancelled or failed UAC requests are reported without actually elevating a process. |
| `test_gamut.py` | BT.2020 conversion, signed FP16 patches, coverage geometry and the separate gamut sweep. | Wide-colour components survive encoding; coverage uses intersection area; invalid sweeps are refused without changing the balance correction. |
| `test_golden_vectors.py` | PQ, sRGB, Bradford adaptation, ICtCp and generated profiles against independent reference calculations. | Results agree with the reference values within the tests' stated tolerances. |
| `test_greyscale.py` | Measured-response storage, interpolation, inverse correction and simulated closed-loop calibration. | Curves correct the simulated response, survive profile round trips and handle clipping without building an invalid inverse. |
| `test_gui.py` | The editor's controls, profile selection/application, persistence, monitor isolation, guidance, measurements and watchdog integration. | User actions update the intended display/state, preserve the base profile and report failures; real colour/HDR writes remain mocked. Run its classes separately. |
| `test_hdr_display.py` | scRGB luminance encoding, metadata checks, display/window association, presentation results and structure sizes. | Encoding and output selection are correct; failed presentation and implausible metadata are detected. These tests do not exercise a real swapchain. |
| `test_import_boundary.py` | The colour core's imports and module coverage. | Core modules depend only on the standard library and their own package, with no Windows or Qt dependency. |
| `test_measure.py` | Patch plans, white-balance solving, reading validation, greyscale response, sustained readings, cancellation and display drift. | Valid readings produce the intended calibration; invalid or incomplete runs are refused; cancellation and state changes stop the sequence. |
| `test_measure_view.py` | Measurement-window frames, UI-thread presentation, worker results, placement, cancellation and shutdown with fake surfaces. | A patch is presented before reading, signed gamut values reach the surface, and the worker reports one outcome and shuts down correctly. |
| `test_meter.py` | `spotread` commands, reading parsing, instrument discovery and subprocess error handling. | Valid output becomes a reading; meter errors, malformed output and cancellation are handled without requiring an instrument. |
| `test_packaging.py` | Launch/build scripts, dependency pins, watchdog copies and ZIP, license inclusion, entry points and documentation accuracy. | Distributed scripts match, the archive contains its required files, downloads require the expected hash and documented options match the code. |
| `test_pattern_view.py` | Fullscreen pattern controls, guided/free sessions, recording, overlays, typed values, dragging and application feedback. | Inputs affect the intended control; sessions stay separate; skipped or refused readings are not saved as valid measurements. |
| `test_patterns.py` | Frame geometry, luminance levels, device-pixel gamma references, colour patches and overlays. | Frames have the required size and levels; reference pixels remain correctly placed; gamma changes affect the intended target. |
| `test_persistence.py` | Atomic JSON writes, loading missing/corrupt state and watchdog build identity. | Saved data is complete, failures are reported appropriately and the build identity follows the relevant payload. |
| `test_watchdog.py` | Embedded-script integrity, profile decisions, gamma hotkeys, logging limits, native recovery and startup fallback. | Recovery follows the active mode and intent; queues/retries/logging stay bounded; fallback failures are reported. Registry and scheduler writes are simulated. |

## PowerShell helpers

The Python tests launch these helpers with extracted functions or temporary
fixtures. Normally, run the owning Python group rather than calling a helper
directly: most require a generated `FunctionsPath`, and the UV helper also needs
a temporary working directory.

| Helper | Called by | Purpose and expected result |
| --- | --- | --- |
| `install_uv_pin.ps1` | `test_packaging.UvBootstrapTests` | Uses a synthetic download to check pinned hashes and failure cleanup. Prints `ALL PASS` and exits `0`. |
| `watchdog_gamma_decision.ps1` | `test_watchdog.GammaDecisionTests` | Checks profile choice for mode, timestamps and malformed runtime data. Prints `ALL PASS` and exits `0`. |
| `watchdog_gamma_hotkey.ps1` | `test_watchdog.GammaHotkeyTests` | Checks prepared Off/On profile selection and runtime updates. Prints `ALL PASS` and exits `0`. |
| `watchdog_log_throttle.ps1` | `test_watchdog.LogThrottleBehaviourTests` | Checks repeated-message suppression and reset behaviour. Prints `ALL PASS` and exits `0`. |
| `watchdog_startup_fallback.ps1` | `test_watchdog.StartupFallbackTests` | Checks fallback quoting, warnings, command-length limits and uninstall failures with fake registry/scheduler calls. Prints `PASS` messages and exits `0` without real registry writes. |

`test_recovery.py` and `test_mode_recovery.py` are local internal diagnostics.
They are ignored by Git and excluded from the public runner; a published checkout
does not need them.

## What a passing run establishes

A pass confirms the calculations and simulated workflows checked here. It does
not measure your monitor, verify a colorimeter's accuracy or demonstrate stable
RAM/handle use over several hours. Those checks require separate hardware or
long-duration runs.

For application setup and measurement limits, see the [main guide](../README.md)
and [technical reference](../docs/TECHNICAL.md).
