"""Which build of the watchdog a script is.

The installer carries the watchdog script as a payload after a marker and writes it to
the watchdog's install folder. Nothing in the deployed copy records which build it is,
which is how an August portable build kept in another folder installed its own watchdog
over a September one on 2026-09-03 -- silently, and with every check the app performs
still reporting success. Comparing what is on disk against what the app ships is the only
way to see that, so both sides are reduced to one short id.
"""

from __future__ import annotations

import hashlib

WATCHDOG_INSTALLER_NAME = "2- OPTIONAL - Install-Watchdog.bat"
WATCHDOG_PAYLOAD_MARKER = ":__WATCHDOG_POWERSHELL_PAYLOAD__"


def watchdog_payload(installer_text: str) -> str:
    """The script the installer would deploy, or "" when the marker is missing.

    Searched from the end for the same reason the .bat uses ``LastIndexOf``: the marker
    also appears in the extraction command near the top of the file, and matching that
    one would return the whole installer instead of the payload.
    """
    index = installer_text.rfind(WATCHDOG_PAYLOAD_MARKER)
    if index < 0:
        return ""
    return installer_text[index + len(WATCHDOG_PAYLOAD_MARKER):]


def watchdog_build_id(script_text: str) -> str:
    """A short identity for a watchdog script, stable across how it reached us.

    Normalised rather than hashed raw, because the two sides arrive differently: the
    deployed copy is written by PowerShell's ``Set-Content -Encoding UTF8``, which adds
    a byte-order mark and keeps CRLF, while the shipped side is sliced out of the .bat.
    Reading both with ``utf-8-sig`` already folds the mark and the line endings, so this
    is belt and braces -- but it is the half worth stating, since a normalisation that
    disagreed would report every build as wrong and be worse than no check at all.
    """
    body = script_text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return ""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
