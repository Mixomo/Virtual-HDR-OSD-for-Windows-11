# Changelog

[Back to the user guide](README.md)

## 2026-10-13 - Gamma compatibility, guided calibration and diagnostics

- Restored hotfix-3 tone processing: the optional fixed SDR-to-gamma-2.2 correction
  precedes the same Gamma, Contrast and Midtone Brightness adjustments in both
  Off/On profiles. Equal brightness trims no longer change behavior when toggling.
- Kept a four-step guide: black, central white, full-frame white and SDR gamma.
  Added an introduction about reported panel data and dark Enter/N warning screens
  before both white patterns. Holding Enter cannot bypass a warning.
- Manual white thresholds now save independently of each other and the panel's
  declaration, without the old 80-nit floor. Values in the 0-10,000-nit PQ domain
  survive state reload and ICC roundtrips. Full-frame visual clipping remains
  distinct from instrument-measured sustained luminance.
- Recreated classic Windows-style gamma matching with nine feathered dots over
  horizontal native-pixel lines. The separate pattern-goal slider and Up/Down
  select 1.6-3.0 in round 0.1 steps; Home restores 2.2. Profile controls remain
  independent. Piecewise sRGB is decoded once at the actual Windows SDR white.
- Increased pattern UI brightness to 80 nits for controls/text, 48 nits for labels
  and 120 nits for introduction, warning and results screens.
- Replaced free browsing with **Diagnostic patterns**: nine fixed inspection views,
  excluding guided targets. No sliders, editing, recording or Apply action. Live
  Apply pauses without changing its saved preference and restores on exit.
- Retained the existing hotkeys and bounded watchdog recovery. Added regressions
  for legacy tone vectors, white-threshold persistence and diagnostic isolation.

## 2026-10-12 - Fork integration and follow-up refinements

This entry groups the fork integration and the adjustments made afterwards.
Its date marks this project's documented update; it does not date the original
upstream work. Contributions were adapted from
[dkflint723/Virtual-HDR-OSD-for-Windows-11](https://github.com/dkflint723/Virtual-HDR-OSD-for-Windows-11),
reviewed at `6aafef412d0f12666aca9ed6d6508ecc9a4af71f`.

### Adapted from the fork

- Corrected DisplayConfig structure layouts and added ABI checks.
- Corrected ICC adaptation data and kept related profile tags consistent when
  merging a base profile with generated calibration data.
- Kept monitors' settings separate, retained their identity across sessions and
  removed outdated runtime records.
- Preserved explicitly chosen base profiles and used filenames rather than ICC
  descriptions to identify profiles; migrated older saved state.
- Rechecked profile associations even when generated content was unchanged, and
  repaired installed copies when Windows did not replace their contents.
- Excluded generated working profiles from original-profile capture and restoration.
- Coordinated gamma-correction intent between GUI and watchdog; added startup guards.
- Added per-display SDR/HDR selectors, an HDR switch and the Leave unmanaged SDR policy.
- Skipped unnecessary profile reinstalls and delayed edit saves, writing each
  settings file as a complete replacement.
- Added panel-data profile creation, the shared colour core, setup guidance,
  accessibility improvements and keyboard/numeric controls.
- Added scRGB test patterns, optional ArgyllCMS measurements, a 33-point greyscale
  sweep, colour sweeps and sustained full-frame measurements.
- Added measured white-balance/greyscale correction, validation of unsuitable readings,
  display-state drift checks and read-only monitor-state queries.
- Added source report tools and tests that simulate Windows profile changes.

### Follow-up corrections in this project

- Separated guided calibration from free pattern browsing. Number keys no longer
  move a guided session into unrelated tests.
- Made the guided run four steps: black, central peak white, full-frame white and
  gamma. Added skipping and removed the separate follow-up tone-tracking step.
- Stored full-frame readings separately and rejected values above peak white.
- Gave each pattern its own test level and input state so switching patterns does
  not reuse the previous pattern's settings.
- Reworked gamma matching around a neutral grey background and an adjustable target.
  Added fixed reference rows, a fine custom gamma readout and exact numeric input.
- Placed profile Gamma, Midtone Brightness and Contrast controls in the gamma step.
  Kept the pattern readout separate from the actual profile correction.
- Corrected Gamma with the fix Off to work through the piecewise SDR desktop curve
  instead of applying a power directly to PQ. Gamma alone now preserves SDR white
  and the higher HDR range; fix On keeps its absolute target-gamma behaviour.
- Improved bounded recovery after SDR/HDR transitions while preserving Alt+1/2/3.
  Added recovery pauses during calibration and stricter display-identity checks.
- Restored HKCU Run as a fallback when Task Scheduler registration fails. Both
  startup methods use the same hidden launcher, sign-in delay and bounded supervisor.
- Improved cancellation, measurement-thread shutdown, fullscreen focus and surface
  cleanup. These changes do not establish that every driver-related handle issue
  or long-duration resource concern is resolved.

### Wide-gamut measurement extension

- Added 30 BT.2020 saturation probes after the existing 70 measurement patches.
- Preserved signed scRGB components so those probes can address colours beyond BT.709.
- Added effective BT.709, Display P3 and BT.2020 coverage reports using CIE 1931 xy
  intersection area. Invalid or incomplete sweeps do not produce percentages.
- Logged stimulus details, measured samples and coverage separately from the
  white-balance/greyscale correction; added `tools/gamut_report.py`.
- Continued using panel metadata for native ICC primaries. The new gamut report
  covers the current display setup; it does not measure the bare panel or colour
  volume, or produce a 3D LUT correction.
