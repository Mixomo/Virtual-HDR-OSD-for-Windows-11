"""vhdr-color: the colour and measurement core of Virtual HDR OSD.

Transfer functions, the MHC2 profile writer and parser, the greyscale solver, Delta ITP,
measurement plans and pattern encoding. Standard library only: no Qt, no Windows calls,
so it can be tested anywhere and reused by tools that are not the desktop app
(docs/adr/0001-split.md). tests/test_import_boundary.py enforces that.
"""
