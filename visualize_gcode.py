#!/usr/bin/env python3
"""
fabCNC G-code Visualizer
Usage: python visualize_gcode.py <path/to/file.gcode> [options]

Options:
  -o, --output FILE    Save to image file instead of displaying
  --no-angles          Hide blade orientation arrows
  --no-corners         Hide corner markers
  --no-labels          Hide shape labels
  --mm                 Display coordinates in mm (default)
  --in                 Display coordinates in inches (divide by 25.4)
"""

import sys
import re
import argparse
import math
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
from matplotlib.collections import LineCollection
import numpy as np


# ── colour palette for shapes ──────────────────────────────────────────────
SHAPE_COLORS = [
    "#2196F3", "#E91E63", "#4CAF50", "#FF9800", "#9C27B0",
    "#00BCD4", "#F44336", "#8BC34A", "#FF5722", "#3F51B5",
    "#009688", "#FFC107", "#673AB7", "#CDDC39", "#795548",
]

TRAVEL_COLOR  = "#AAAAAA"
TRAVEL_ALPHA  = 0.45
CUT_ALPHA     = 0.85
ARROW_COLOR   = "#222222"
CORNER_COLOR  = "#FF1744"
START_COLOR   = "#00C853"
END_COLOR     = "#FF1744"


# ── parser ──────────────────────────────────────────────────────────────────

class GCodeParser:
    def __init__(self):
        self.moves = []          # list of dicts per point
        self.shapes = []         # list of dicts: {name, start_idx, end_idx}
        self.corners = []        # list of point indices where a corner lift happened

        self._cx = self._cy = self._cz = self._ca = 0.0
        self._current_shape = None
        self._shape_start = 0
        self._pending_corner = False

    def parse(self, path: str):
        with open(path, "r") as f:
            lines = f.readlines()

        for raw in lines:
            line = raw.strip()
            if not line:
                continue

            # shape name from comment
            if line.startswith("; Shape:"):
                name = line[len("; Shape:"):].strip()
                if self._current_shape is not None:
                    self.shapes[-1]["end_idx"] = len(self.moves) - 1
                self._current_shape = name
                self._shape_start = len(self.moves)
                self.shapes.append({"name": name, "start_idx": self._shape_start, "end_idx": None})
                continue

            if line.startswith(";"):
                # detect corner raise in comment
                if "Raise Z for corner" in line or "corner" in line.lower():
                    self._pending_corner = True
                continue

            # parse coords
            xm = re.search(r'X([-\d.]+)', line)
            ym = re.search(r'Y([-\d.]+)', line)
            zm = re.search(r'Z([-\d.]+)', line)
            am = re.search(r'A([-\d.]+)', line)
            fm = re.search(r'F([-\d.]+)', line)

            if xm: self._cx = float(xm.group(1))
            if ym: self._cy = float(ym.group(1))
            if zm: self._cz = float(zm.group(1))
            if am: self._ca = float(am.group(1))

            is_g0 = line.startswith("G0")
            is_g1 = line.startswith("G1")

            if not (is_g0 or is_g1):
                continue

            # A corner is flagged when Z is raised above the typical cut depth.
            # We track the lift comment instead.
            is_travel = is_g0 and (not xm and not ym)   # pure Z or A move
            if is_g0 and not (xm or ym):
                # Z-only or A-only rapid — not an XY move
                continue

            move = {
                "x": self._cx,
                "y": self._cy,
                "z": self._cz,
                "a": self._ca,
                "f": float(fm.group(1)) if fm else 0.0,
                "cut": is_g1,           # G1 = cutting, G0 = travel
                "shape_idx": len(self.shapes) - 1,
                "corner": self._pending_corner,
            }
            if self._pending_corner:
                self.corners.append(len(self.moves))
                self._pending_corner = False

            self.moves.append(move)

        # close last shape
        if self.shapes:
            self.shapes[-1]["end_idx"] = len(self.moves) - 1

    def stats(self):
        if not self.moves:
            return {}
        xs = [m["x"] for m in self.moves]
        ys = [m["y"] for m in self.moves]
        zs = [m["z"] for m in self.moves]

        cut_len = travel_len = 0.0
        for i in range(1, len(self.moves)):
            dx = self.moves[i]["x"] - self.moves[i-1]["x"]
            dy = self.moves[i]["y"] - self.moves[i-1]["y"]
            d = math.sqrt(dx*dx + dy*dy)
            if self.moves[i]["cut"]:
                cut_len += d
            else:
                travel_len += d

        return {
            "points": len(self.moves),
            "shapes": len(self.shapes),
            "corners": len(self.corners),
            "x_range": (min(xs), max(xs)),
            "y_range": (min(ys), max(ys)),
            "z_range": (min(zs), max(zs)),
            "cut_length_mm": cut_len,
            "travel_length_mm": travel_len,
        }


# ── visualizer ──────────────────────────────────────────────────────────────

def build_segments(moves, shape_color_map, use_inches):
    """Return (cut_segs, cut_colors, travel_segs) as numpy arrays."""
    scale = 1/25.4 if use_inches else 1.0

    cut_segs, cut_colors = [], []
    travel_segs = []

    for i in range(1, len(moves)):
        p0, p1 = moves[i-1], moves[i]
        seg = [[p0["x"]*scale, p0["y"]*scale],
               [p1["x"]*scale, p1["y"]*scale]]
        if p1["cut"]:
            cut_segs.append(seg)
            cidx = p1["shape_idx"]
            cut_colors.append(shape_color_map.get(cidx, SHAPE_COLORS[0]))
        else:
            travel_segs.append(seg)

    return (np.array(cut_segs) if cut_segs else np.empty((0,2,2)),
            cut_colors,
            np.array(travel_segs) if travel_segs else np.empty((0,2,2)))


def add_blade_arrows(ax, moves, use_inches, n_arrows=60):
    """Draw blade angle indicators as short line segments."""
    scale = 1/25.4 if use_inches else 1.0
    step = max(1, len(moves) // n_arrows)
    arrow_len = _estimate_arrow_len(moves, scale)

    for i in range(0, len(moves), step):
        m = moves[i]
        if not m["cut"]:
            continue
        a_rad = math.radians(m["a"])
        # blade is perpendicular to travel; A-axis is rotation of the blade
        cx, cy = m["x"] * scale, m["y"] * scale
        dx = math.cos(a_rad) * arrow_len * 0.5
        dy = math.sin(a_rad) * arrow_len * 0.5
        ax.plot([cx - dx, cx + dx], [cy - dy, cy + dy],
                color=ARROW_COLOR, lw=0.6, alpha=0.45, solid_capstyle="round")


def _estimate_arrow_len(moves, scale):
    if len(moves) < 2:
        return 1.0
    xs = [m["x"]*scale for m in moves]
    ys = [m["y"]*scale for m in moves]
    span = max(max(xs)-min(xs), max(ys)-min(ys))
    return max(span * 0.015, 0.5)


def add_shape_labels(ax, moves, shapes, shape_color_map, use_inches):
    scale = 1/25.4 if use_inches else 1.0
    for i, shape in enumerate(shapes):
        s, e = shape["start_idx"], shape["end_idx"]
        if e is None or s >= len(moves):
            continue
        seg = moves[s:e+1]
        if not seg:
            continue
        xs = [m["x"]*scale for m in seg]
        ys = [m["y"]*scale for m in seg]
        cx, cy = (min(xs)+max(xs))/2, (min(ys)+max(ys))/2
        color = shape_color_map.get(i, SHAPE_COLORS[0])
        ax.text(cx, cy, shape["name"],
                fontsize=6.5, ha="center", va="center",
                color=color, alpha=0.75,
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=color,
                          lw=0.5, alpha=0.6))


def visualize(gcode_path: str, output: str = None,
              show_angles=True, show_corners=True, show_labels=True,
              use_inches=False):

    parser = GCodeParser()
    parser.parse(gcode_path)

    if not parser.moves:
        print("No movement data found in file.")
        sys.exit(1)

    stats = parser.stats()
    unit = "in" if use_inches else "mm"
    scale = 1/25.4 if use_inches else 1.0

    # assign colours to shapes
    shape_color_map = {i: SHAPE_COLORS[i % len(SHAPE_COLORS)]
                       for i in range(len(parser.shapes))}

    # ── figure ──
    fig, ax = plt.subplots(figsize=(14, 10))
    fig.patch.set_facecolor("#1A1A2E")
    ax.set_facecolor("#16213E")
    for spine in ax.spines.values():
        spine.set_edgecolor("#444466")

    # ── draw travel moves ──
    cut_segs, cut_colors, travel_segs = build_segments(
        parser.moves, shape_color_map, use_inches)

    if len(travel_segs) > 0:
        lc_travel = LineCollection(travel_segs, colors=TRAVEL_COLOR,
                                   linewidths=0.6, alpha=TRAVEL_ALPHA,
                                   linestyles="dashed", zorder=2)
        ax.add_collection(lc_travel)

    # ── draw cut moves ──
    if len(cut_segs) > 0:
        lc_cut = LineCollection(cut_segs, colors=cut_colors,
                                linewidths=1.2, alpha=CUT_ALPHA, zorder=3)
        ax.add_collection(lc_cut)

    # ── blade arrows ──
    if show_angles:
        add_blade_arrows(ax, parser.moves, use_inches)

    # ── corner markers ──
    if show_corners and parser.corners:
        cx_list = [parser.moves[i]["x"]*scale for i in parser.corners
                   if i < len(parser.moves)]
        cy_list = [parser.moves[i]["y"]*scale for i in parser.corners
                   if i < len(parser.moves)]
        ax.scatter(cx_list, cy_list, c=CORNER_COLOR, s=25, marker="x",
                   linewidths=1.2, zorder=6, label=f"Corners ({len(cx_list)})")

    # ── start / end markers ──
    first_cut = next((m for m in parser.moves if m["cut"]), parser.moves[0])
    last_cut  = next((m for m in reversed(parser.moves) if m["cut"]), parser.moves[-1])
    ax.scatter([first_cut["x"]*scale], [first_cut["y"]*scale],
               c=START_COLOR, s=60, marker="o", zorder=7, label="Start")
    ax.scatter([last_cut["x"]*scale], [last_cut["y"]*scale],
               c=END_COLOR, s=60, marker="s", zorder=7, label="End")

    # ── shape labels ──
    if show_labels:
        add_shape_labels(ax, parser.moves, parser.shapes, shape_color_map, use_inches)

    # ── legend for shapes ──
    shape_handles = []
    for i, shape in enumerate(parser.shapes):
        color = shape_color_map.get(i, SHAPE_COLORS[0])
        shape_handles.append(
            mpatches.Patch(color=color, label=shape["name"], alpha=0.8))

    # system handles
    system_handles = []
    system_handles.append(Line2D([0],[0], color=TRAVEL_COLOR, lw=1,
                                 linestyle="dashed", alpha=0.7, label="Travel"))
    if show_corners and parser.corners:
        system_handles.append(
            Line2D([0],[0], marker="x", color=CORNER_COLOR, lw=0,
                   markersize=6, label=f"Corners ({len(parser.corners)})"))
    system_handles.append(
        Line2D([0],[0], marker="o", color=START_COLOR, lw=0,
               markersize=6, label="Start"))
    system_handles.append(
        Line2D([0],[0], marker="s", color=END_COLOR, lw=0,
               markersize=6, label="End"))

    all_handles = shape_handles + system_handles
    legend = ax.legend(handles=all_handles, loc="upper right",
                       fontsize=7, framealpha=0.7,
                       facecolor="#1A1A2E", edgecolor="#555577",
                       labelcolor="white", ncol=max(1, len(all_handles)//12))

    # ── stats box ──
    xlo, xhi = stats["x_range"]
    ylo, yhi = stats["y_range"]
    w = (xhi - xlo) * scale
    h = (yhi - ylo) * scale
    cut_m   = stats["cut_length_mm"] * scale
    trav_m  = stats["travel_length_mm"] * scale

    stats_text = (
        f"File: {Path(gcode_path).name}\n"
        f"Shapes: {stats['shapes']}  |  Points: {stats['points']:,}  |  Corners: {stats['corners']}\n"
        f"Bounds: {w:.1f} × {h:.1f} {unit}\n"
        f"Cut length: {cut_m:.1f} {unit}  |  Travel: {trav_m:.1f} {unit}"
    )
    ax.text(0.01, 0.01, stats_text, transform=ax.transAxes,
            fontsize=7.5, color="#CCCCDD", va="bottom",
            bbox=dict(boxstyle="round,pad=0.4", fc="#1A1A2E",
                      ec="#555577", alpha=0.85))

    # ── axes ──
    ax.autoscale_view()
    ax.set_aspect("equal")
    ax.tick_params(colors="#888899", labelsize=8)
    ax.set_xlabel(f"X ({unit})", color="#888899", fontsize=9)
    ax.set_ylabel(f"Y ({unit})", color="#888899", fontsize=9)
    ax.set_title(Path(gcode_path).stem, color="white", fontsize=12, pad=10)
    ax.grid(True, color="#2A2A4A", linewidth=0.5, alpha=0.7)

    plt.tight_layout()

    if output:
        plt.savefig(output, dpi=200, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        print(f"Saved to {output}")
    else:
        plt.show()


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="fabCNC G-code Visualizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("gcode_file", help="Path to .gcode file")
    parser.add_argument("-o", "--output",
                        help="Save to image file (png/pdf/svg) instead of displaying")
    parser.add_argument("--no-angles",  action="store_true",
                        help="Hide blade orientation indicators")
    parser.add_argument("--no-corners", action="store_true",
                        help="Hide corner lift markers")
    parser.add_argument("--no-labels",  action="store_true",
                        help="Hide shape name labels")
    parser.add_argument("--in", dest="use_inches", action="store_true",
                        help="Display in inches instead of mm")

    args = parser.parse_args()

    if not Path(args.gcode_file).exists():
        print(f"Error: file not found: {args.gcode_file}")
        sys.exit(1)

    visualize(
        gcode_path=args.gcode_file,
        output=args.output,
        show_angles=not args.no_angles,
        show_corners=not args.no_corners,
        show_labels=not args.no_labels,
        use_inches=args.use_inches,
    )


if __name__ == "__main__":
    main()
