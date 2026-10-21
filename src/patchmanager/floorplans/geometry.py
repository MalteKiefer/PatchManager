"""Plane geometry on plan pixels."""

import math
from collections.abc import Sequence

Point = tuple[float, float]


def contains(polygon: Sequence[Sequence[float]], x: float, y: float) -> bool:
    """Ray casting: True if (x, y) lies inside the polygon (edges count as inside)."""
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if _on_segment(x, y, x1, y1, x2, y2):
            return True
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _on_segment(x: float, y: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > 1e-9:
        return False
    return (
        min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9
        and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9
    )


def distance(a: Point, b: Point) -> float:
    """Euclidean distance."""
    return math.hypot(b[0] - a[0], b[1] - a[1])


def area(polygon: Sequence[Sequence[float]]) -> float:
    """Shoelace area (absolute)."""
    n = len(polygon)
    return (
        abs(
            sum(
                polygon[i][0] * polygon[(i + 1) % n][1] - polygon[(i + 1) % n][0] * polygon[i][1]
                for i in range(n)
            )
        )
        / 2
    )
