# Technical reference

[Back to the user guide](../README.md)

Read this section for the processing steps, profile handling and measurement limits.
Installation and everyday controls are covered in the README.

## Profiles and display identity

The app writes ICC/ICM profiles containing an MHC2 calibration block. Windows applies
its colour matrix and three per-channel tone tables to the selected display.
This is display-wide calibration, not a per-application filter or a general 3D LUT.

Edits are written to consistently named working profiles, leaving the base file
untouched. Matching file contents skip reinstallation, but the display association
is checked regardless. ICC tags that belong together are handled as a group.

SDR and HDR use separate Windows profile association subtypes. The app only selects
an existing SDR profile; it does not generate one or reload another app's VCGT curves.

Preferences and prepared profile pairs use persistent display identity. Runtime
records can be indexed by adapter LUID but also carry the monitor device path for
matching and stale-record removal. A reboot can change the LUID; moving a monitor
to another port can change the device path.

Native primaries are taken from EDID when available. DXGI is a fallback because its
reported colour data can reflect the currently applied profile. Reusing that data
as a fresh hardware measurement would create a feedback loop.

## Tone and colour

Tone processing follows below order in both working profiles:

```text
PQ -> optional fixed piecewise-sRGB-to-gamma-2.2 correction
-> independent PQ power (Gamma / 2.200) -> Contrast -> Midtone Brightness -> tone LUT
```

The correction itself is identity above SDR reference white. Gamma, Brightness and
Contrast follow it and act on the whole PQ range in both Off and On profiles.
Gamma 2.200 is a neutral adjustment, not a measurement of the resulting desktop
gamma. Brightness uses the same endpoint-preserving lift in both profiles:
`y + (trim / 100) * y * (1 - y)`, with the trim bounded to +/-35%.

Measured greyscale correction is combined with the per-channel tone curves. Each run
records the tone settings in use to account for existing correction on a later pass.
Changing the underlying tone setup can invalidate a previously measured response.

Colour processing follows:

```text
RGB Fine Balance -> Color Saturation -> neutral-luminance normalization
-> XYZ conversion -> Temperature / Tint adaptation -> MHC2 matrix
```

The SDR-in-HDR correction also affects native HDR content in the same luminance
range. Windows does not isolate this display calibration to SDR windows.

## Pattern rendering

Patterns use a Direct3D 11 FP16 scRGB surface at device-pixel resolution. On HDR
outputs, scRGB 1.0 represents 80 nits. SDR reference browsing uses display-relative
white instead and cannot record HDR luminance.

Most visual patterns use a fixed window on black. Full-frame white fills the screen
because it tests a different operating condition. A visual disappearance threshold
is a clipping estimate, not a photometric reading of the panel's output.

The gamma reference alternates sRGB black (0) and desktop white (1) in one-device-pixel
horizontal stripes with equal area. Values are decoded once for linear scRGB. For reference gamma `g`,
the target signal is:

```text
0.5^(1/g)
```

Nine circular targets use a selected gamma goal from 1.6 to 3.0, initially 2.2.
Up/Down adjusts that goal in 0.1 steps; its independent draggable slider snaps
to the same tenths. Home resets it. This lives in the pattern
context and never changes a profile parameter or follows the actual Gamma slider.
The result includes Windows SDR presentation and the active profile; it is not a
measurement of the PQ transfer function.

The circular feather uses an ordered spatial mixture of solid target pixels and
complete black/white horizontal row pairs. Both rows in each pair share a coverage
mask, so a nonlinear profile still leaves the local optical average correct at the
selected goal. Interpolating grey signal values at the edges would instead create
a false halo. The design recreates the classic Windows calibrator's visual principle
without copying its resources.

The 2.2 target is the SDR desktop reference. The 2.4 label refers to the exponent
used by SDR video reference monitoring (BT.1886); this chart does not implement
BT.1886's nonzero-black compensation. The 2.6 label is a DCI cinema gamma reference,
not a complete cinema calibration. HDR television uses PQ or HLG under BT.2100,
and HDR10 uses PQ/ST 2084. Neither reference should be labelled an HDR gamma target.

### Why piecewise decoding is required

For a solid SDR signal `s`, the HDR surface contains `sRGB_EOTF(s) * SDR_white / 80`.
This follows Microsoft's linear scRGB reference-white mapping. The reference is not
silently clamped to the reported peak: doing so would test a different white level
from the desktop and from the Auto correction. If SDR white exceeds reported peak,
the pattern warns that clipping or tone mapping can invalidate the match. A 2.2 target uses
signal `0.5^(1/2.2)` (about 0.72974), which decodes to about 0.49151 of reference
white before display calibration. It must not be sent as linear 0.5: that would
already bake the intended match into the source. Nor should the SDR-to-2.2 fix be
applied while rendering; Windows applies the selected MHC2 adjustment afterwards.

With an ideal PQ display and neutral controls, correction On converts the solid
2.2 target to 0.5 of white, matching the optical average of black and white. With
correction Off, the slight piecewise-sRGB/pure-2.2 difference remains visible.
Real panel processing, black level and spatial response can change that result;
this is an effective visual estimate, not a complete gamma measurement.

The construction follows the optical matching principle described by
[EIZO](https://www.eizo.com/library/basics/lcd_display_gamma/) and uses
[Microsoft's SDR/scRGB mapping](https://learn.microsoft.com/en-us/windows/win32/direct3darticles/high-dynamic-range).
EIZO's online test source uses a two-pixel black/white tile and a solid gamma-2.2
signal of 186/255; this implementation computes its own floating-point references.
These sources establish the underlying method, not a formal standard specifically
for visual SDR calibration inside Windows HDR. No upstream images or code are bundled.

### Guided luminance values

The guide writes black to `minimum_luminance_nits`, central white to
`peak_luminance_nits` (MHC2) and full-frame white to `full_frame_luminance_nits`
(`lumi`). Manual white thresholds use the 0-10,000-nit signal range and are stored
independently: neither the panel declaration nor the other white step caps them.
State reload, ICC export/import and manual GUI edits preserve those values.
A full-frame disappearance threshold describes clipping, not measured sustained
panel output. EDID defaults and instrument measurements remain separate sources.

The introduction and both white warnings render on black before the pattern.
Enter opens the warned pattern; N skips it while keeping the existing value.
Auto-repeated Enter is ignored on warning screens. Pattern UI uses 80-nit text and
controls, 48-nit markers and 120-nit introduction/warning/results text.

### Read-only pattern diagnostics

Diagnostic and guided catalogs are separate. The diagnostic window discards edit,
measurement and apply callbacks, blocks mutation APIs and accepts only navigation,
information-panel placement and closing. Its overlay exposes no tracks or edit keys.
Live Apply is stopped while viewing diagnostics; SDR-white updates refresh the
frame without queuing a profile write. Existing calibration remains active.

## Instrument measurements

ArgyllCMS `spotread` provides absolute emissive XYZ readings. The worker waits for
the UI thread to present each patch before reading it. Cancellation, display-state
drift and presentation failure stop the run without adopting partial calibration.

The full plan contains 100 patches:

| Stage | Patches | Purpose |
| --- | --- | --- |
| Core | 7 | Black, 3% peak white, 10% window white, reference white and RGB balance. |
| Greyscale | 33 | PQ tracking and grey balance across the measured range. |
| BT.709 colours | 30 | Six hues at five saturations. |
| BT.2020 colours | 30 | Effective wide-gamut coverage through the active HDR path. |

Peak uses a 3% screen-area window; other sweep patches use 10%. Instrument placement
must fit inside the smallest window. Primary drive matches reference-white drive,
normally 100 nits, lowered for dim displays. This reduces brightness-limiter effects
on white-balance readings but does not guarantee that every monitor is additive.

The solver checks whether the readings support white-balance correction. When they
do not, that correction is refused explicitly. A reversing greyscale cannot be
inverted safely; such a response is not silently converted into a plausible LUT.
The separate sustained measurement lights the full screen until readings stabilize.

Instrument precision depends on placement, panel behaviour, USB/driver support
and the spectral correction used. A supported protocol does not establish that a
particular instrument/display combination has been physically validated.

### Gamut coverage

BT.709 balance patches discard negative channel values. Separate BT.2020 probes
convert linear BT.2020 to linear BT.709/scRGB and preserve negative components in
FP16. They can address colours beyond BT.709 without changing the balance solve.

Coverage is the CIE 1931 xy intersection area of each reference triangle with the
convex hull of the 30 measured chromaticities. References are BT.709, Display P3
and BT.2020. Incomplete, invalid, below-floor or degenerate sweeps yield a refusal
instead of a coverage percentage.

The reported gamut includes the effects of Windows, the selected profile and the
monitor preset: together they form the **active HDR output path**. They are sampled at one drive/window size,
not native-panel primaries or a luminance-dependent colour-volume measurement.
They do not replace EDID colorants or create a hue-dependent 3D correction.

## Watchdog and startup

The watchdog deploys under `%LOCALAPPDATA%\ColorProfileModeWatchdog`. It runs a
native recovery worker independently of the GUI. It captures the desired SDR/HDR
associations and recovers the active subtype after a mode change.

Task Scheduler is the preferred startup method, using the current user's SID and
interactive token. If registration fails, the installer tries HKCU Run with the
same hidden launcher. Both methods wait ten seconds after sign-in. If neither
startup method can be registered, installation reports a failure.

Only one worker instance runs. Its hotkeys use one thread, its wait handles are
fixed, and its command queue holds at most 16 items. Recovery attempts are limited. Startup has a 25-second guard. The
launcher stops after three consecutive early failures instead of restarting forever.
Calibration sessions publish a renewable recovery pause that expires if the GUI dies.

Alt+1/2 select the app's prepared Off/On profiles; they do not generate new profiles.
Alt+3 requests recovery. The GUI and watchdog coordinate intent to avoid restoring
the opposite correction state. Recovery remains available after the editor closes.

Updating distribution files does not replace an installed worker. Rerun the installer
to deploy updates or capture a changed profile pair. Uninstalling the watchdog and
restoring the previous Windows profile are separate actions.

## Diagnostics and tests

App state and `meter_log.jsonl` are under
`%LOCALAPPDATA%\Virtual_HDR_OSD_for_Windows`. Measurement logs include patch requests,
display/profile state, monitor information and accepted or refused results.

The source tools read those logs without applying profiles:

```powershell
python tools/greyscale_report.py
python tools/greyscale_report.py --curve
python tools/colour_report.py
python tools/gamut_report.py
```

Each accepts a log path. Greyscale uses the recorded profile intent where available.
The colour report scores the BT.709 saturation sweep; the gamut report reads the
separate accepted wide-gamut result. These are source tools, not a CLI built into
the portable EXE.

Set `VIRTUAL_HDR_OSD_TRACE=1` before launching to enable a bounded rotating `trace.log`.
Startup exceptions are written to `startup_error.log` where possible.

Run the public tests with the project environment:

```powershell
.venv\Scripts\python.exe tools/run_tests.py
```

Test groups run separately using temporary files. Instruments and Windows profile
writes are simulated, so test results alone cannot confirm measured colour accuracy
or resource use over several hours. Long-duration resource validation remains
separate. A hidden-window D3D recreation probe previously showed additional handles;
its cause remains unconfirmed, and that surface is not used by the watchdog.

## Portable build

Run **3- (Advanced users & developers) - Build Portable EXE.bat**. It creates a local
build environment, runs the tests and compiles the GUI with Nuitka in one-file mode.
Outputs are:

```text
release/Virtual HDR OSD for Windows.exe
release/Virtual HDR OSD for Windows.sha256.txt
```

The standalone watchdog ZIP contains its installer, uninstaller, license and
third-party notices. Its scripts also ship in the repository root and the portable's
resources; distributed copies must remain synchronized.

## References

- [Windows Advanced Color and scRGB](https://learn.microsoft.com/en-us/windows/win32/direct3darticles/high-dynamic-range)
- [Windows MHC2 calibration pipeline](https://learn.microsoft.com/en-us/windows/win32/wcs/display-calibration-mhc)
- [SDR-in-HDR gamma reference](https://github.com/dylanraga/win11hdr-srgb-to-gamma2.2-icm)
- [EIZO monitor test](https://www.eizo.be/monitor-test/) and [Display Test gamma](https://display-test.app/test/gamma): signal references for original patterns, not bundled code or artwork.

- [ITU-R BT.1886: SDR reference EOTF](https://www.itu.int/dms_pubrec/itu-r/rec/bt/R-REC-BT.1886-0-201103-I!!PDF-E.pdf)
- [ITU-R BT.2100-3: HDR PQ and HLG](https://www.itu.int/dms_pubrec/itu-r/rec/bt/R-REC-BT.2100-3-202502-I!!PDF-E.pdf)
- [DCI: conventional gamma 2.6 and ST 2084](https://www.dcimovies.com/assets/DCI-Memo-On-Direct-View-Displays_2108-0627.CkDNqB-O.pdf)
