"""Code moved out of app.py may not reach Windows, the hardware, or Qt.

The window tests fake every Windows call by patching names on the app module
(test_gui.WindowTestCase), and FixtureSafetyTests checks that those fakes are in place.
That only protects code that looks the names up in app.py. A module split out of it that
imported, say, install_and_associate_profile itself would call the real one from a test.
So the modules split out of app.py are held to having nothing to call: no import from the
Windows layer, the meter, DDC/CI or the UI, no PySide6, no platform half of the standard
library, and no reading of the environment to find a path of their own. Every path they
use is handed to them by app.py, where the tests redirect it.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "src" / "sdr_hdr_profile_creator"

SPLIT_MODULES = ("persistence", "watchdog_identity")

# Siblings that touch Windows, hardware or Qt. The colour core, vhdr_color, is allowed.
FORBIDDEN_SIBLINGS = frozenset({
    "app", "controls", "ddc", "dialogs", "edid", "elevation", "hdr_display", "hotkeys",
    "measure_view", "meter", "pattern_view", "windows_api", "__main__",
})
FORBIDDEN_TOP = frozenset({"PySide6", "qfluentwidgets", "qframelesswindow", "ctypes", "winreg",
                           "subprocess", "msvcrt", "_winapi", "sdr_hdr_profile_creator"})


def problems_in(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_TOP:
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level >= 1:
                targets = [node.module.split(".")[0]] if node.module else [a.name for a in node.names]
                found.extend(f"from .{t}" for t in targets if t in FORBIDDEN_SIBLINGS)
            elif node.module and node.module.split(".")[0] in FORBIDDEN_TOP:
                found.append(f"from {node.module}")
        elif isinstance(node, ast.Attribute) and node.attr == "environ":
            found.append("os.environ")
        elif isinstance(node, ast.Constant) and node.value == "LOCALAPPDATA":
            found.append("LOCALAPPDATA")
    return sorted(found)


class SplitModuleTests(unittest.TestCase):
    def test_split_modules_have_nothing_to_call(self):
        for module in SPLIT_MODULES:
            with self.subTest(module=module):
                source = (PACKAGE / f"{module}.py").read_text(encoding="utf-8")
                self.assertEqual([], problems_in(source))


class CheckerTests(unittest.TestCase):
    def test_it_finds_every_way_to_the_machine(self):
        source = (
            "import os\n"
            "from .windows_api import install_and_associate_profile\n"
            "from . import hotkeys\n"
            "from PySide6.QtCore import QTimer\n"
            "import ctypes\n"
            "root = os.environ.get('LOCALAPPDATA')\n"
            "def late():\n"
            "    from .app import STATE_PATH\n"
        )
        self.assertEqual(
            ["LOCALAPPDATA", "from .app", "from .hotkeys", "from .windows_api",
             "from PySide6.QtCore", "import ctypes", "os.environ"],
            problems_in(source),
        )

    def test_it_allows_the_core_and_plain_stdlib(self):
        source = (
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "from vhdr_color.model import ApplicationState\n"
            "def save(path: Path):\n"
            "    os.replace(path, path)\n"
        )
        self.assertEqual([], problems_in(source))


if __name__ == "__main__":
    unittest.main()
