"""Score the colour sweeps the app already measures and has never shown anyone.

A full run reads 70 patches. Thirty of them are the saturation sweeps -- six hues at
five saturations, roughly 105 seconds of a four-minute measurement -- and the result has
never been read by anything: ``Calibration.colours`` is assigned in ``derive`` and has no
consumer anywhere in the project. The confirmation dialog meanwhile promises the user
"a saturation sweep for each colour".

**Reported, never corrected, and the distinction is the point.** MHC2 is a matrix
followed by three 1-D LUTs indexed by PQ code. An error that depends on hue and
saturation jointly has no representation in it at any LUT size, so this measures
something the profile provably cannot fix. That is worth knowing rather than hiding: it
says how far the panel departs from the additive, hue-stable behaviour a matrix-shaper
assumes, which is exactly the assumption the rest of the correction rests on.

The reference is the fork's own machinery, not an imported chart. Each patch is
predicted as an additive display: take each channel's XYZ contribution to the reference
white -- solved by ``channel_contributions``, which recovers magnitudes from the white
measured beside the primaries rather than from the primary patches themselves -- and
weight them by the drives ``_saturated`` asked for. Measured against predicted, scored in
delta ITP. So a large number here means "this panel is not additive at this hue", which
is a fact about the display, not a calibration error.

Usage:

    python tools/colour_report.py            # every run in the default log
    python tools/colour_report.py <path>     # a log kept somewhere else
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vhdr_color import delta_itp as itp  # noqa: E402
from vhdr_color import measure  # noqa: E402

DEFAULT_LOG = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    / "Virtual_HDR_OSD_for_Windows"
    / "meter_log.jsonl"
)


def runs(records):
    """Split the log into runs, the same way greyscale_report does."""
    current = None
    for record in records:
        if record.get("event") == "start":
            if current is not None:
                yield current
            current = {"start": record, "readings": {}}
        elif current is None:
            continue
        elif record.get("event") == "reading":
            current["readings"][record.get("patch", "")] = record
    if current is not None:
        yield current


def _reading(record):
    return measure.Reading(
        X=record["X"], Y=record["Y"], Z=record["Z"], x=record["x"], y=record["y"]
    )


def predicted(matrix, drives):
    """What an additive display would deliver for these drives.

    The matrix from ``channel_contributions`` holds, in column c, channel c's XYZ
    contribution to the reference white. An additive display is one where driving the
    channels at r, g, b delivers exactly r*R + g*G + b*B -- so the prediction is that
    matrix applied to the drives, and nothing more. The failure of that model is the
    thing being measured.
    """
    return measure._matvec3(matrix, tuple(drives))


def describe(index, run):
    start = run["start"]
    at = start.get("at", "?")[:19].replace("T", " ")
    print(f"\nRun {index}  {at}  {start.get('display', '?')}")

    colour_keys = sorted(k for k in run["readings"] if k.startswith("colour-"))
    if not colour_keys:
        print("  No colour sweeps in this run.")
        return

    needed = ("balance-white", "red", "green", "blue")
    if any(key not in run["readings"] for key in needed):
        print("  The run has colour patches but not the white and primary patches the "
              "prediction is built from.")
        return

    readings = {key: _reading(run["readings"][key]) for key in needed}
    solved = measure.channel_contributions(readings)
    if solved is None:
        print("  The three channels do not span a colour space in this run; nothing to "
              "compare against.")
        return
    _contributions, matrix = solved

    print("  measured against an additive model of this display's own primaries")
    print("  reported only -- a matrix-shaper cannot carry a hue-dependent error")
    print()
    print("  hue        " + "".join(f"{int(s * 100):>7}%" for s in measure.SATURATIONS))

    everything = []
    for hue, _mask in measure.HUES:
        row = []
        for saturation in measure.SATURATIONS:
            key = f"colour-{hue}-{int(round(saturation * 100)):03d}"
            record = run["readings"].get(key)
            if record is None:
                row.append(None)
                continue
            drives = measure._saturated(dict(measure.HUES)[hue], saturation)
            want = predicted(matrix, drives)
            got = (record["X"], record["Y"], record["Z"])
            score = itp.delta_itp(got, want)
            row.append(score)
            everything.append(score)
        cells = "".join(f"{value:>8.1f}" if value is not None else "       -" for value in row)
        print(f"  {hue:<10}{cells}")

    if not everything:
        return
    everything.sort()
    middle = everything[len(everything) // 2]
    over = sum(1 for value in everything if value > itp.GOOD)
    print()
    print(f"  median {middle:.1f} dITP   worst {everything[-1]:.1f}   "
          f"{over} of {len(everything)} over {itp.GOOD:g}")
    print("  A large number is this panel departing from additive behaviour at that hue,")
    print("  not a fault in the correction: the correction cannot reach it either way.")
    print()
    print("  Reading the table: at 100% saturation the patch IS the primary, so that")
    print("  column restates the boost the solve already works around -- on this panel")
    print("  the primaries read about 2.1x their share of white. The columns to its left")
    print("  are the new information, and what they show is WHERE the panel's saturation")
    print("  processing engages: a hue that is flat to 60% and large at 80% is being")
    print("  boosted somewhere between the two.")


def main(argv):
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_LOG
    if not path.is_file():
        print(f"No meter log at {path}")
        return 1

    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue

    seen = 0
    for index, run in enumerate(runs(records), start=1):
        if any(k.startswith("colour-") for k in run["readings"]):
            describe(index, run)
            seen += 1
    if seen == 0:
        print("No run in this log carries colour sweeps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
