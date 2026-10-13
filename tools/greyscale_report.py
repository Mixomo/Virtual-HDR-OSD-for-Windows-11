"""Read the meter log and say how well each run tracked what the profile asked for.

Usage:

    python tools/greyscale_report.py            # every run in the default log
    python tools/greyscale_report.py <path>     # a log kept somewhere else
    python tools/greyscale_report.py --curve    # dITP for every ramp point, not banded

Against the profile's intent, not against PQ. The two are the same only while every
control is neutral. The SDR-in-HDR correction deliberately darkens the low end -- at 0.5
nits of PQ it asks for 0.105 -- so comparing a reading against the PQ level reports that
setting as a 70% error, in exactly the part of the range someone would look at hardest.
Runs logged before the intent was recorded fall back to PQ and say so.

Two runs either side of an Apply is the test that matters: the second should track far
closer than the first, and a third should barely differ from the second. A second run
that is *worse* than the first is the signature of a correction being applied twice.

Nothing here reads the display or the profile. It only reads the log, so it can be run
long after the fact and on a different machine.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vhdr_color import delta_itp as itp  # noqa: E402
from vhdr_color.measure import D65_XY  # noqa: E402

DEFAULT_LOG = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    / "Virtual_HDR_OSD_for_Windows"
    / "meter_log.jsonl"
)

# --curve prints dITP for every ramp point rather than the banded summary.
SHOW_CURVE = False


def runs(records):
    """Split the log into runs. A run is a ``start`` and everything up to the next one."""
    current = None
    for record in records:
        if record.get("event") == "start":
            if current is not None:
                yield current
            current = {"start": record, "readings": {}, "outcome": None}
        elif current is None:
            continue
        elif record.get("event") == "reading":
            current["readings"][record.get("patch", "")] = record
        elif record.get("event") in ("accepted", "refused", "cancelled", "response",
                                     "response-unavailable"):
            current.setdefault("events", []).append(record)
            if record["event"] in ("accepted", "refused", "cancelled"):
                current["outcome"] = record["event"]
    if current is not None:
        yield current


def describe(index, run):
    start = run["start"]
    plan = start.get("plan") or {}
    at = start.get("at", "?")[:19].replace("T", " ")
    print(f"\nRun {index}  {at}  {start.get('display', '?')}  ({start.get('instrument', '?')})")

    if not plan:
        print("  No plan recorded -- this run predates the log carrying its targets.")
        return

    # Against what the profile asks for, not against PQ. They differ whenever a control
    # is not neutral: the SDR-in-HDR correction deliberately darkens the low end, and
    # comparing a reading to the PQ level reports that setting as a 70% error.
    intent = start.get("intent") or {}
    grey = sorted(key for key in plan if key.startswith("grey-"))
    paired = [
        (intent.get(key, plan[key]), run["readings"][key])
        for key in grey
        if key in run["readings"] and (intent.get(key, plan[key]) or 0) > 0
    ]
    if not paired:
        print(f"  No greyscale readings ({run['outcome'] or 'run did not finish'}).")
        return

    against = "what the profile asks for" if intent else "PQ (no intent recorded)"
    correction = start.get("sdr_gamma_correction")
    print(f"  measured against: {against}"
          + (f"   SDR-in-HDR correction: {correction}" if correction else ""))

    errors = [abs(reading["Y"] - target) / target for target, reading in paired]
    drifts = [math.dist((reading["x"], reading["y"]), D65_XY) for _t, reading in paired]
    worst = max(range(len(errors)), key=lambda i: errors[i])

    print(f"  {len(paired)} of {len(grey)} ramp points read"
          f"   outcome: {run['outcome'] or 'incomplete'}")
    print(f"  luminance error   median {_pct(_median(errors))}   worst {_pct(max(errors))}"
          f"  (asked {paired[worst][0]:.4g} nits, read {paired[worst][1]['Y']:.4g})")
    print(f"  drift from D65    median {_median(drifts):.4f}   worst {max(drifts):.4f}")

    # The two lines above are the tone error and the white error, in different units,
    # and neither says whether anyone would see it. dITP does, on one scale where 1.0 is
    # a just-noticeable difference wherever on the ramp it falls: a 5% miss at 1 nit
    # scores 1.8 and the same 5% at 1000 nits scores 3.8, which is the part a median
    # percentage cannot express. It also catches a tinted grey whose luminance is
    # exactly right -- an error the percentage above reports as 0.00%.
    scored = itp.curve(
        [(target, (reading["X"], reading["Y"], reading["Z"])) for target, reading in paired]
    )
    if scored:
        values = [value for _nits, value in scored]
        over = sum(1 for value in values if value > itp.GOOD)
        peak = max(scored, key=lambda item: item[1])
        print(f"  dITP (BT.2124)    median {_median(values):5.2f}   worst {max(values):5.2f}"
              f"   at {peak[0]:.4g} nits"
              f"   ({over} of {len(values)} over {itp.GOOD:g})")

    # The bottom of the range is where a display is usually worst and where the ramp is
    # deliberately densest, so it is reported on its own rather than hidden in a median.
    low = [error for (target, _r), error in zip(paired, errors) if target <= 10.0]
    if low:
        print(f"  below 10 nits     {len(low)} points, median {_pct(_median(low))}"
              f"   worst {_pct(max(low))}")

    # The curve, banded. A single median hides the shape that matters -- a display that
    # is excellent through the midrange and falls apart in the shadows reports the same
    # median as one that is mediocre everywhere, and only the first is worth another
    # pass with the meter.
    if scored:
        bands = (
            ("below 10 nits ", lambda n: n <= 10.0),
            ("10 to 100     ", lambda n: 10.0 < n <= 100.0),
            ("100 to 400    ", lambda n: 100.0 < n <= 400.0),
            ("above 400     ", lambda n: n > 400.0),
        )
        print("  dITP by band")
        for label, inside in bands:
            band = [value for nits, value in scored if inside(nits)]
            if band:
                print(f"    {label}  {len(band):>2} points   median {_median(band):5.2f}"
                      f"   worst {max(band):5.2f}   {_bar(_median(band))}")
        if SHOW_CURVE:
            print("  dITP per point")
            for nits, value in scored:
                print(f"    {nits:>9.4g} nits   {value:5.2f}   {_bar(value)}")

    for record in run.get("events", []):
        if record["event"] == "response":
            weights = ", ".join(f"{value:.4f}" for value in record.get("weights", ()))
            print(f"  stored a correction from {record.get('points', '?')} points"
                  f"   white split ({weights})")
        elif record["event"] == "response-unavailable":
            print(f"  NO correction stored (kept previous: "
                  f"{record.get('kept_previous')}) -- {record.get('ramp_points')} ramp points")


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _pct(value):
    return f"{value * 100:6.2f}%"


def _bar(value):
    """One JND per block, so the threshold is a place on the line rather than a number
    to remember. Capped at 20 -- past that the point is made."""
    blocks = min(20, int(round(value)))
    return ("#" * blocks) + ("." if blocks == 0 else "")


def main(argv):
    global SHOW_CURVE
    arguments = [value for value in argv[1:] if value != "--curve"]
    SHOW_CURVE = "--curve" in argv[1:]

    path = Path(arguments[0]) if arguments else DEFAULT_LOG
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
        except json.JSONDecodeError:
            continue

    found = list(runs(records))
    if not found:
        print(f"{path} has no measurement runs in it yet.")
        return 0

    print(f"{path}\n{len(found)} run(s)")
    for index, run in enumerate(found, start=1):
        describe(index, run)

    print("\nWhat to look for: the run after an Apply should track far closer than the one")
    print("before it, and a third should barely differ from the second. A second run that")
    print("is worse than the first means a correction is landing twice.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
