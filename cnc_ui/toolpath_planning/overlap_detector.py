"""
Polygon overlap detection for toolpath safety checks.

Given a dict of shape_name -> list of (x, y) polygon vertices (in mm), find
pairs whose interiors overlap. Used to warn the user before generating a
toolpath that would cut overlapping shapes.
"""

from typing import Dict, List, Tuple

Point = Tuple[float, float]
Polygon = List[Point]

# Tolerance (mm) used to ignore numerically-touching edges/vertices.
_EPS = 1e-6


def _bbox(poly: Polygon) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _bboxes_overlap(a: Tuple[float, float, float, float],
                    b: Tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 < bx0 - _EPS or bx1 < ax0 - _EPS or
                ay1 < by0 - _EPS or by1 < ay0 - _EPS)


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_properly_intersect(p1: Point, p2: Point,
                                 p3: Point, p4: Point) -> bool:
    """True if segments p1p2 and p3p4 cross with non-zero overlap of interiors.

    Endpoint-only touches do not count (so shapes that merely share a vertex
    or kiss along an edge are not flagged as overlapping)."""
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    # Strict sign change on both segments => proper crossing.
    if ((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS)) and \
       ((d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS)):
        return True
    return False


def _point_strictly_inside(point: Point, poly: Polygon) -> bool:
    """Ray-cast point-in-polygon. Returns False for points on the boundary."""
    x, y = point
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        # Reject points lying (approximately) on this edge.
        if _point_on_segment(point, (xi, yi), (xj, yj)):
            return False
        if ((yi > y) != (yj > y)):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def _point_on_segment(p: Point, a: Point, b: Point) -> bool:
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > _EPS * max(1.0, abs(b[0] - a[0]) + abs(b[1] - a[1])):
        return False
    dot = (p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])
    sq = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
    return -_EPS <= dot <= sq + _EPS


def _polygons_overlap(a: Polygon, b: Polygon) -> bool:
    if len(a) < 3 or len(b) < 3:
        return False
    if not _bboxes_overlap(_bbox(a), _bbox(b)):
        return False
    # Any properly-crossing edge pair => overlap.
    na, nb = len(a), len(b)
    for i in range(na):
        a1, a2 = a[i], a[(i + 1) % na]
        for j in range(nb):
            b1, b2 = b[j], b[(j + 1) % nb]
            if _segments_properly_intersect(a1, a2, b1, b2):
                return True
    # Containment: any strictly-interior vertex of one inside the other.
    for v in a:
        if _point_strictly_inside(v, b):
            return True
    for v in b:
        if _point_strictly_inside(v, a):
            return True
    return False


def find_overlapping_pairs(shapes: Dict[str, Polygon]) -> List[Tuple[str, str]]:
    """Return pairs of shape names whose polygon interiors overlap."""
    names = list(shapes.keys())
    bboxes = {n: _bbox(shapes[n]) for n in names if len(shapes[n]) >= 3}
    pairs: List[Tuple[str, str]] = []
    for i in range(len(names)):
        ni = names[i]
        if ni not in bboxes:
            continue
        for j in range(i + 1, len(names)):
            nj = names[j]
            if nj not in bboxes:
                continue
            if not _bboxes_overlap(bboxes[ni], bboxes[nj]):
                continue
            if _polygons_overlap(shapes[ni], shapes[nj]):
                pairs.append((ni, nj))
    return pairs
