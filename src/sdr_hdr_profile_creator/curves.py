"""Compatibility import for the shared colour core."""
import sys
from vhdr_color import curves as _core
sys.modules[__name__] = _core
