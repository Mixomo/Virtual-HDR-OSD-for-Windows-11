# Third-Party Notices

## PySide6 / Qt for Python

Virtual HDR OSD for Windows uses PySide6 / Qt for Python. Redistribution must comply with the applicable Qt and PySide6 licensing terms.

## PySide6-Fluent-Widgets

The graphical interface uses the PySide6 build of PyQt-Fluent-Widgets / PySide6-Fluent-Widgets by zhiyiYo. Review the upstream project license before redistribution, especially for commercial use.

Project: https://github.com/zhiyiYo/PyQt-Fluent-Widgets

## SDR-in-HDR gamma-correction reference

The optional Windows piecewise-sRGB to pure-gamma-2.2 correction is implemented from the publicly documented transfer-function method and current LUT-generator formulas in dylanraga's reference project:

https://github.com/dylanraga/win11hdr-srgb-to-gamma2.2-icm

Virtual HDR OSD does not bundle that project's ICC/ICM profile binaries, ArgyllCMS, or the community AutoHotkey script.


## Integrated fork improvements

Corrections, the shared `vhdr_color` core, calibration patterns, measurement tools,
accessibility improvements and related tests are adapted from dkflint723's GPL-3.0
fork of this project:

https://github.com/dkflint723/Virtual-HDR-OSD-for-Windows-11

Reviewed source snapshot: `6aafef412d0f12666aca9ed6d6508ecc9a4af71f`.
Original project authorship and the existing GPL-3.0 license are retained.