"""BT.2020 probes and CIE 1931 xy coverage of the active HDR output path.

Coverage is the intersection of the measured convex hull and a reference triangle,
not the ratio of their areas, and not a colour-volume or native-panel measurement.
No measured vertices are installed as ICC primaries: Windows and the active profile
can map or clip these probes before the panel sees them.
"""
from __future__ import annotations

import math

# Linear-light BT.2020 to linear-light BT.709 (D65 in both spaces).
# Derived from the standard primaries; negative components MUST reach FP16 intact.
BT2020_TO_SCRGB = (
    (1.6604910021, -0.5876411388, -0.0728498633),
    (-0.1245504745, 1.1328998971, -0.0083494226),
    (-0.0181507634, -0.1005788980, 1.1187296614),
)
REFERENCES = {
    "BT.709": ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06)),
    "Display P3": ((0.68, 0.32), (0.265, 0.69), (0.15, 0.06)),
    "BT.2020": ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046)),
}


def to_scrgb(rgb):
    """Convert a bounded linear BT.2020 stimulus; never clamp the result."""
    if len(rgb) != 3 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in rgb):
        raise ValueError("BT.2020 drive must have three finite components in [0, 1]")
    return tuple(sum(a * b for a, b in zip(row, rgb)) for row in BT2020_TO_SCRGB)


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def hull(points):
    points = sorted(set(points))
    if len(points) < 3:
        return tuple(points)
    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and _cross(out[-2], out[-1], p) <= 0:
                out.pop()
            out.append(p)
        return out
    return tuple(half(points)[:-1] + half(reversed(points))[:-1])


def area(poly):
    if len(poly) < 3:
        return 0.0
    return abs(sum(a[0] * b[1] - a[1] * b[0]
                   for a, b in zip(poly, (*poly[1:], poly[0])))) / 2


def coverage(poly, reference):
    """Sutherland-Hodgman intersection with a counterclockwise reference hull."""
    clipped = list(poly)
    clip = hull(reference)
    for a, b in zip(clip, (*clip[1:], clip[0])):
        old, clipped = clipped, []
        if not old:
            break
        previous = old[-1]
        previous_side = _cross(a, b, previous)
        for current in old:
            side = _cross(a, b, current)
            if (side >= 0) != (previous_side >= 0):
                t = previous_side / (previous_side - side)
                clipped.append(tuple(p + t * (q - p) for p, q in zip(previous, current)))
            if side >= 0:
                clipped.append(current)
            previous, previous_side = current, side
    return min(1.0, max(0.0, area(clipped) / area(clip)))


def report(readings):
    """Validate a complete 30-patch sweep, or return an explicit refusal.

    Kept diagnostic-only so a noisy colour reading cannot overwrite good white balance.
    XYZ/xy consistency prevents a failed instrument response fabricating coverage.
    """
    expected = {f"gamut-{h}-{s:03d}" for h in
                ("red", "yellow", "green", "cyan", "blue", "magenta")
                for s in (20, 40, 60, 80, 100)}
    selected = {k: v for k, v in readings.items() if k.startswith("gamut-")}
    if not selected:
        return {}
    base = {"scope": "active HDR output path", "stimulus": "linear BT.2020 via signed scRGB",
            "metric": "CIE 1931 xy intersection area; not colour volume",
            "patch_count": len(selected)}
    if set(selected) != expected:
        return {**base, "refused": "Incomplete BT.2020 saturation sweep"}
    for key, r in selected.items():
        vals = (r.X, r.Y, r.Z, r.x, r.y)
        total = r.X + r.Y + r.Z
        if (any(not math.isfinite(v) for v in vals)
                or min(r.X, r.Y, r.Z) < -1e-10 * max(1.0, total)
                or r.Y <= 0.01 or not 0 < r.y < 1 or not 0 <= r.x <= 1
                or r.x + r.y > 1.00001 or total <= 0
                or abs(r.x - r.X / total) > 0.002 or abs(r.y - r.Y / total) > 0.002):
            return {**base, "refused": f"Invalid or below-floor colour reading: {key}"}
    boundary = hull((r.x, r.y) for r in selected.values())
    if area(boundary) < 0.005:
        return {**base, "refused": "Measured colours do not span a usable gamut"}
    return {**base, "boundary_xy": [list(p) for p in boundary],
            "coverage_percent": {name: round(100 * coverage(boundary, triangle), 3)
                                 for name, triangle in REFERENCES.items()},
            "samples": {key: {"x": r.x, "y": r.y, "nits": r.Y}
                        for key, r in sorted(selected.items())}}
