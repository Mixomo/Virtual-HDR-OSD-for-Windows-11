"""Run the publishable suite with GUI classes isolated in separate processes."""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INTERNAL_MODULES = {"test_recovery", "test_mode_recovery"}


def groups() -> list[str]:
    names = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.stem in INTERNAL_MODULES:
            continue
        if path.stem == "test_gui":
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names.extend(f"test_gui.{node.name}" for node in tree.body
                         if isinstance(node, ast.ClassDef) and any(
                             isinstance(member, ast.FunctionDef) and member.name.startswith("test_")
                             for member in node.body))
        else:
            names.append(path.stem)
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, choices=range(1, 5), default=3)
    parser.add_argument("--group", action="append", help="Run a specific module or GUI class")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if args.child:
        sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
        suite = unittest.defaultTestLoader.loadTestsFromName(args.child)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        Path(args.result).write_text(json.dumps({"tests": result.testsRun,
            "skipped": len(result.skipped), "ok": result.wasSuccessful()}), encoding="utf-8")
        return 0 if result.wasSuccessful() else 1

    selected = args.group or groups()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests"))))
    with tempfile.TemporaryDirectory(prefix="vhdr-tests-") as temporary:
        def run(item):
            index, name = item
            result_path = Path(temporary) / f"result-{index}.json"
            log_path = Path(temporary) / f"output-{index}.log"
            timed_out = False
            with log_path.open("w", encoding="utf-8") as output:
                try:
                    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                        "--child", name, "--result", str(result_path)], cwd=ROOT, env=env,
                        stdout=output, stderr=subprocess.STDOUT, timeout=180)
                    code = completed.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                    code = 1
            data = {"tests": 0, "skipped": 0, "ok": False}
            if result_path.exists():
                data = json.loads(result_path.read_text(encoding="utf-8"))
            ok = code == 0 and data["ok"]
            detail = ""
            if not ok:
                with log_path.open("rb") as output:
                    output.seek(max(0, log_path.stat().st_size - 8000))
                    detail = output.read().decode("utf-8", errors="replace")
                if timed_out:
                    detail += "\nGroup exceeded 180 seconds."
            return name, data, ok, detail

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(run, enumerate(selected)))
        for name, _, ok, detail in results:
            if not ok:
                print(f"FAIL: {name}\n{detail}")
        tests = sum(data["tests"] for _, data, _, _ in results)
        skipped = sum(data["skipped"] for _, data, _, _ in results)
        failures = sum(not ok for _, _, ok, _ in results)
        print(f"{tests} tests; {skipped} skipped; {len(results)} isolated groups; {failures} failed")
        return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
