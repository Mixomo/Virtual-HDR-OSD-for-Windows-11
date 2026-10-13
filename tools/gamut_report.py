"""Print active-path gamut reports from the portable app's meter log."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?", type=Path, default=(
        Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
        / "Virtual_HDR_OSD_for_Windows" / "meter_log.jsonl"))
    args = parser.parse_args()
    count = 0
    try:
        with args.log.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    print(f"Ignored malformed log entry at line {line_number}")
                    continue
                if not isinstance(entry, dict) or entry.get("event") != "accepted":
                    continue
                report = entry.get("gamut_report")
                if not isinstance(report, dict) or not report:
                    continue
                count += 1
                print(f"\n{entry.get('at', '?')} — {report.get('scope', 'active HDR output path')}")
                if report.get("refused"):
                    print(f"  Unavailable: {report['refused']}")
                    continue
                print("  CIE 1931 xy coverage (not colour volume or native-panel gamut):")
                for name, value in report.get("coverage_percent", {}).items():
                    print(f"    {name}: {value:.3f}%")
        if not count:
            print("No accepted BT.2020 gamut reports in this log.")
    except OSError as exc:
        parser.exit(1, f"Cannot read meter log: {exc}\n")


if __name__ == "__main__":
    main()
