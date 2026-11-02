#!/usr/bin/env python3
"""Generate binary-laser hatch G-code from a filled SVG."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from svg_model import Polyline, load_svg


def transform_polys(polys: list[Polyline], bbox: tuple[float, float, float, float], x: float, y: float, width: float) -> list[Polyline]:
    x0, y0, x1, y1 = bbox
    src_w = x1 - x0
    src_h = y1 - y0
    if src_w <= 0 or src_h <= 0:
        raise ValueError("empty SVG bounds")
    scale = width / src_w
    out: list[Polyline] = []
    for poly in polys:
        new: Polyline = []
        for sx, sy in poly:
            px = x + (sx - x0) * scale
            py = y + (src_h - (sy - y0)) * scale
            new.append((px, py))
        out.append(new)
    return out


def hatch_segments(polys: list[Polyline], spacing: float) -> list[Polyline]:
    ys = [p[1] for poly in polys for p in poly]
    if not ys:
        return []
    y_min = math.floor(min(ys) / spacing) * spacing
    y_max = math.ceil(max(ys) / spacing) * spacing
    segments: list[Polyline] = []
    y = y_min
    row = 0
    while y <= y_max + 1e-9:
        xs: list[float] = []
        for poly in polys:
            if len(poly) < 2:
                continue
            for (x1, y1), (x2, y2) in zip(poly, poly[1:]):
                if abs(y2 - y1) < 1e-9:
                    continue
                if (y1 <= y < y2) or (y2 <= y < y1):
                    t = (y - y1) / (y2 - y1)
                    xs.append(x1 + t * (x2 - x1))
        xs.sort()
        pairs = []
        for i in range(0, len(xs) - 1, 2):
            if xs[i + 1] - xs[i] > 0.15:
                pairs.append((xs[i], xs[i + 1]))
        if row % 2:
            pairs.reverse()
            segments.extend([[(b, y), (a, y)] for a, b in pairs])
        else:
            segments.extend([[(a, y), (b, y)] for a, b in pairs])
        y += spacing
        row += 1
    return segments


def poly_length(poly: Polyline) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(poly, poly[1:]))


def overcut_points(poly: Polyline, distance: float) -> Polyline:
    if distance <= 0 or len(poly) < 3 or math.hypot(poly[-1][0] - poly[0][0], poly[-1][1] - poly[0][1]) > 0.05:
        return []
    out: Polyline = []
    remaining = distance
    current = poly[0]
    for nxt in poly[1:]:
        seg = math.hypot(nxt[0] - current[0], nxt[1] - current[1])
        if seg <= 1e-9:
            current = nxt
            continue
        if seg >= remaining:
            t = remaining / seg
            out.append((current[0] + (nxt[0] - current[0]) * t, current[1] + (nxt[1] - current[1]) * t))
            break
        out.append(nxt)
        remaining -= seg
        current = nxt
    return out


def gcode(paths: list[Polyline], rapid: float, feed: float, power: int, overcut: float) -> list[str]:
    lines = [
        "(NASA worm hatch engraving)",
        "G21",
        "G90",
        "M4 0",
    ]
    for path in paths:
        if len(path) < 2:
            continue
        start = path[0]
        lines.append(f"G1 X{start[0]:.3f} Y{start[1]:.3f} F{rapid:.0f}")
        lines.append(f"M4 {power}")
        for x, y in path[1:]:
            lines.append(f"G1 X{x:.3f} Y{y:.3f} F{feed:.0f}")
        for x, y in overcut_points(path, overcut):
            lines.append(f"G1 X{x:.3f} Y{y:.3f} F{feed:.0f}")
        lines.append("M4 0")
    lines.append("M4 0")
    lines.append("G28")
    lines.append("M4 0")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("svg")
    parser.add_argument("out")
    parser.add_argument("--x", type=float, default=90.0)
    parser.add_argument("--y", type=float, default=70.0)
    parser.add_argument("--width", type=float, default=85.0)
    parser.add_argument("--spacing", type=float, default=1.4)
    parser.add_argument("--feed", type=float, default=2400.0)
    parser.add_argument("--rapid", type=float, default=3000.0)
    parser.add_argument("--power", type=int, default=255)
    parser.add_argument("--overcut", type=float, default=3.0)
    parser.add_argument("--outline", action="store_true")
    args = parser.parse_args()

    drawing = load_svg(args.svg, curve_steps=20)
    polys = transform_polys(drawing.polylines, drawing.bbox, args.x, args.y, args.width)
    hatches = hatch_segments(polys, args.spacing)
    paths = hatches + (polys if args.outline else [])
    lines = gcode(paths, args.rapid, args.feed, args.power, args.overcut)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")

    xs = [p[0] for poly in paths for p in poly]
    ys = [p[1] for poly in paths for p in poly]
    print(f"paths={len(paths)} commands={len(lines)}")
    print(f"bounds=({min(xs):.2f},{min(ys):.2f})-({max(xs):.2f},{max(ys):.2f})")
    print(f"burn_distance={sum(poly_length(p) for p in paths):.1f}mm")


if __name__ == "__main__":
    main()
