"""Small SVG-to-polyline loader for the Makeblock XY plotter GUI."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import xml.etree.ElementTree as ET


Point = tuple[float, float]
Polyline = list[Point]


@dataclass(frozen=True)
class SvgDrawing:
    polylines: list[Polyline]
    bbox: tuple[float, float, float, float]
    width: float | None = None
    height: float | None = None
    viewbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class Affine:
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def apply(self, p: Point) -> Point:
        x, y = p
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)

    def then(self, other: "Affine") -> "Affine":
        """Return a transform equivalent to applying self, then other."""
        return Affine(
            other.a * self.a + other.c * self.b,
            other.b * self.a + other.d * self.b,
            other.a * self.c + other.c * self.d,
            other.b * self.c + other.d * self.d,
            other.a * self.e + other.c * self.f + other.e,
            other.b * self.e + other.d * self.f + other.f,
        )


_FLOAT = r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?"
_PATH_TOKEN_RE = re.compile(rf"[AaCcHhLlMmQqSsTtVvZz]|{_FLOAT}")
_TRANSFORM_RE = re.compile(r"([a-zA-Z]+)\(([^)]*)\)")
_NUMBER_RE = re.compile(_FLOAT)


def _num(value: str | None, default: float | None = None) -> float | None:
    if value is None:
        return default
    m = _NUMBER_RE.search(value)
    if not m:
        return default
    n = float(m.group(0))
    unit = value[m.end() :].strip().lower()
    if unit == "in":
        return n * 96.0
    if unit == "cm":
        return n * 96.0 / 2.54
    if unit == "mm":
        return n * 96.0 / 25.4
    if unit == "pt":
        return n * 96.0 / 72.0
    if unit == "pc":
        return n * 16.0
    return n


def _numbers(text: str) -> list[float]:
    return [float(v) for v in _NUMBER_RE.findall(text or "")]


def parse_transform(text: str | None) -> Affine:
    if not text:
        return Affine()
    transform = Affine()
    for name, args_text in _TRANSFORM_RE.findall(text):
        args = _numbers(args_text)
        name = name.lower()
        op = Affine()
        if name == "matrix" and len(args) >= 6:
            op = Affine(*args[:6])
        elif name == "translate" and args:
            op = Affine(e=args[0], f=args[1] if len(args) > 1 else 0.0)
        elif name == "scale" and args:
            sx = args[0]
            sy = args[1] if len(args) > 1 else sx
            op = Affine(a=sx, d=sy)
        elif name == "rotate" and args:
            angle = math.radians(args[0])
            ca = math.cos(angle)
            sa = math.sin(angle)
            rot = Affine(ca, sa, -sa, ca, 0.0, 0.0)
            if len(args) >= 3:
                cx, cy = args[1], args[2]
                op = Affine(e=-cx, f=-cy).then(rot).then(Affine(e=cx, f=cy))
            else:
                op = rot
        elif name == "skewx" and args:
            op = Affine(c=math.tan(math.radians(args[0])))
        elif name == "skewy" and args:
            op = Affine(b=math.tan(math.radians(args[0])))
        transform = transform.then(op)
    return transform


def load_svg(path: str, curve_steps: int = 24) -> SvgDrawing:
    tree = ET.parse(path)
    root = tree.getroot()
    width = _num(root.get("width"))
    height = _num(root.get("height"))
    viewbox = _parse_viewbox(root.get("viewBox"))
    polylines: list[Polyline] = []
    _walk(root, Affine(), polylines, curve_steps)
    clean = [_dedupe(poly) for poly in polylines if len(poly) >= 2]
    clean = [poly for poly in clean if len(poly) >= 2]
    bbox = _bbox(clean)
    return SvgDrawing(clean, bbox, width, height, viewbox)


def _parse_viewbox(text: str | None) -> tuple[float, float, float, float] | None:
    vals = _numbers(text or "")
    if len(vals) == 4:
        return (vals[0], vals[1], vals[2], vals[3])
    return None


def _walk(elem: ET.Element, transform: Affine, out: list[Polyline], curve_steps: int) -> None:
    if _is_hidden(elem):
        return
    t = transform.then(parse_transform(elem.get("transform")))
    tag = elem.tag.rsplit("}", 1)[-1]
    created: list[Polyline] = []
    if tag == "path":
        d = elem.get("d")
        if d:
            created.extend(_parse_path(d, curve_steps))
    elif tag == "line":
        x1, y1 = _num(elem.get("x1"), 0.0), _num(elem.get("y1"), 0.0)
        x2, y2 = _num(elem.get("x2"), 0.0), _num(elem.get("y2"), 0.0)
        created.append([(x1 or 0.0, y1 or 0.0), (x2 or 0.0, y2 or 0.0)])
    elif tag in {"polyline", "polygon"}:
        pts = _parse_points(elem.get("points") or "")
        if tag == "polygon" and pts:
            pts = pts + [pts[0]]
        created.append(pts)
    elif tag == "rect":
        created.append(_rect(elem, curve_steps))
    elif tag == "circle":
        cx, cy = _num(elem.get("cx"), 0.0) or 0.0, _num(elem.get("cy"), 0.0) or 0.0
        r = _num(elem.get("r"), 0.0) or 0.0
        created.append(_ellipse(cx, cy, r, r, max(curve_steps * 3, 48)))
    elif tag == "ellipse":
        cx, cy = _num(elem.get("cx"), 0.0) or 0.0, _num(elem.get("cy"), 0.0) or 0.0
        rx, ry = _num(elem.get("rx"), 0.0) or 0.0, _num(elem.get("ry"), 0.0) or 0.0
        created.append(_ellipse(cx, cy, rx, ry, max(curve_steps * 3, 48)))

    for poly in created:
        if len(poly) >= 2:
            out.append([t.apply(p) for p in poly])

    for child in list(elem):
        _walk(child, t, out, curve_steps)


def _is_hidden(elem: ET.Element) -> bool:
    style = elem.get("style", "")
    hidden = "display:none" in style.replace(" ", "") or "visibility:hidden" in style.replace(" ", "")
    return hidden or elem.get("display") == "none" or elem.get("visibility") == "hidden"


def _parse_points(text: str) -> Polyline:
    vals = _numbers(text)
    return [(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]


def _rect(elem: ET.Element, curve_steps: int) -> Polyline:
    x, y = _num(elem.get("x"), 0.0) or 0.0, _num(elem.get("y"), 0.0) or 0.0
    w, h = _num(elem.get("width"), 0.0) or 0.0, _num(elem.get("height"), 0.0) or 0.0
    rx = min(_num(elem.get("rx"), 0.0) or 0.0, w / 2.0)
    ry = min(_num(elem.get("ry"), rx) or 0.0, h / 2.0)
    if rx <= 0 or ry <= 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    pts: Polyline = []
    corners = [
        (x + w - rx, y + ry, -90, 0),
        (x + w - rx, y + h - ry, 0, 90),
        (x + rx, y + h - ry, 90, 180),
        (x + rx, y + ry, 180, 270),
    ]
    pts.append((x + rx, y))
    for cx, cy, a0, a1 in corners:
        for i in range(1, max(4, curve_steps // 4) + 1):
            a = math.radians(a0 + (a1 - a0) * i / max(4, curve_steps // 4))
            pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    pts.append(pts[0])
    return pts


def _ellipse(cx: float, cy: float, rx: float, ry: float, steps: int) -> Polyline:
    if rx <= 0 or ry <= 0:
        return []
    return [
        (cx + rx * math.cos(2 * math.pi * i / steps), cy + ry * math.sin(2 * math.pi * i / steps))
        for i in range(steps + 1)
    ]


class _PathCursor:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.i = 0

    def peek(self) -> str | None:
        if self.i >= len(self.tokens):
            return None
        return self.tokens[self.i]

    def pop(self) -> str:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def has_number(self) -> bool:
        token = self.peek()
        return token is not None and not token.isalpha()

    def number(self) -> float:
        return float(self.pop())


def _parse_path(d: str, curve_steps: int) -> list[Polyline]:
    cur = _PathCursor(_PATH_TOKEN_RE.findall(d.replace(",", " ")))
    polys: list[Polyline] = []
    poly: Polyline = []
    pos = (0.0, 0.0)
    start = (0.0, 0.0)
    last_cmd = ""
    last_cubic: Point | None = None
    last_quad: Point | None = None

    def move_to(p: Point) -> None:
        nonlocal poly, pos, start
        if len(poly) >= 2:
            polys.append(poly)
        poly = [p]
        pos = start = p

    def line_to(p: Point) -> None:
        nonlocal pos, last_cubic, last_quad
        if not poly:
            poly.append(pos)
        poly.append(p)
        pos = p
        last_cubic = None
        last_quad = None

    while cur.peek() is not None:
        token = cur.peek()
        if token.isalpha():
            cmd = cur.pop()
        else:
            cmd = last_cmd
        if not cmd:
            raise ValueError("SVG path data starts with coordinates before a command")
        last_cmd = cmd
        rel = cmd.islower()
        op = cmd.upper()

        if op == "M":
            first = True
            while cur.has_number():
                p = _relative((cur.number(), cur.number()), pos, rel)
                if first:
                    move_to(p)
                    first = False
                else:
                    line_to(p)
            last_cubic = last_quad = None
        elif op == "L":
            while cur.has_number():
                line_to(_relative((cur.number(), cur.number()), pos, rel))
        elif op == "H":
            while cur.has_number():
                x = cur.number() + (pos[0] if rel else 0.0)
                line_to((x, pos[1]))
        elif op == "V":
            while cur.has_number():
                y = cur.number() + (pos[1] if rel else 0.0)
                line_to((pos[0], y))
        elif op == "C":
            while cur.has_number():
                p1 = _relative((cur.number(), cur.number()), pos, rel)
                p2 = _relative((cur.number(), cur.number()), pos, rel)
                p3 = _relative((cur.number(), cur.number()), pos, rel)
                poly.extend(_cubic(pos, p1, p2, p3, curve_steps)[1:])
                pos = p3
                last_cubic = p2
                last_quad = None
        elif op == "S":
            while cur.has_number():
                p1 = _reflect(last_cubic, pos) if last_cmd.upper() in {"C", "S"} else pos
                p2 = _relative((cur.number(), cur.number()), pos, rel)
                p3 = _relative((cur.number(), cur.number()), pos, rel)
                poly.extend(_cubic(pos, p1, p2, p3, curve_steps)[1:])
                pos = p3
                last_cubic = p2
                last_quad = None
        elif op == "Q":
            while cur.has_number():
                p1 = _relative((cur.number(), cur.number()), pos, rel)
                p2 = _relative((cur.number(), cur.number()), pos, rel)
                poly.extend(_quadratic(pos, p1, p2, curve_steps)[1:])
                pos = p2
                last_quad = p1
                last_cubic = None
        elif op == "T":
            while cur.has_number():
                p1 = _reflect(last_quad, pos) if last_cmd.upper() in {"Q", "T"} else pos
                p2 = _relative((cur.number(), cur.number()), pos, rel)
                poly.extend(_quadratic(pos, p1, p2, curve_steps)[1:])
                pos = p2
                last_quad = p1
                last_cubic = None
        elif op == "A":
            while cur.has_number():
                rx, ry, angle = cur.number(), cur.number(), cur.number()
                large, sweep = int(cur.number()), int(cur.number())
                end = _relative((cur.number(), cur.number()), pos, rel)
                poly.extend(_arc(pos, rx, ry, angle, large, sweep, end, curve_steps * 2)[1:])
                pos = end
                last_cubic = last_quad = None
        elif op == "Z":
            line_to(start)
            if len(poly) >= 2:
                polys.append(poly)
            poly = []
            pos = start
            last_cubic = last_quad = None
        else:
            raise ValueError(f"Unsupported SVG path command {cmd!r}")

    if len(poly) >= 2:
        polys.append(poly)
    return polys


def _relative(p: Point, origin: Point, rel: bool) -> Point:
    if rel:
        return (origin[0] + p[0], origin[1] + p[1])
    return p


def _reflect(p: Point | None, around: Point) -> Point:
    if p is None:
        return around
    return (2 * around[0] - p[0], 2 * around[1] - p[1])


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, steps: int) -> Polyline:
    pts: Polyline = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1.0 - t
        pts.append(
            (
                mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0],
                mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1],
            )
        )
    return pts


def _quadratic(p0: Point, p1: Point, p2: Point, steps: int) -> Polyline:
    pts: Polyline = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1.0 - t
        pts.append((mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0], mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]))
    return pts


def _arc(start: Point, rx: float, ry: float, angle_deg: float, large_arc: int, sweep: int, end: Point, steps: int) -> Polyline:
    if rx == 0 or ry == 0 or start == end:
        return [start, end]
    phi = math.radians(angle_deg % 360.0)
    cos_phi = math.cos(phi)
    sin_phi = math.sin(phi)
    dx = (start[0] - end[0]) / 2.0
    dy = (start[1] - end[1]) / 2.0
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    rx, ry = abs(rx), abs(ry)
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        scale = math.sqrt(lam)
        rx *= scale
        ry *= scale
    sign = -1.0 if large_arc == sweep else 1.0
    denom = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = 0.0 if denom == 0 else sign * math.sqrt(max(0.0, (rx * rx * ry * ry - denom) / denom))
    cxp = coef * rx * y1p / ry
    cyp = coef * -ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (start[0] + end[0]) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (start[1] + end[1]) / 2.0

    def angle(u: Point, v: Point) -> float:
        dot = u[0] * v[0] + u[1] * v[1]
        det = u[0] * v[1] - u[1] * v[0]
        return math.atan2(det, dot)

    v1 = ((x1p - cxp) / rx, (y1p - cyp) / ry)
    v2 = ((-x1p - cxp) / rx, (-y1p - cyp) / ry)
    theta1 = angle((1.0, 0.0), v1)
    delta = angle(v1, v2)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    n = max(2, int(abs(delta) / (2 * math.pi) * steps))
    pts: Polyline = []
    for i in range(n + 1):
        theta = theta1 + delta * i / n
        x = cos_phi * rx * math.cos(theta) - sin_phi * ry * math.sin(theta) + cx
        y = sin_phi * rx * math.cos(theta) + cos_phi * ry * math.sin(theta) + cy
        pts.append((x, y))
    return pts


def _bbox(polys: list[Polyline]) -> tuple[float, float, float, float]:
    xs = [p[0] for poly in polys for p in poly]
    ys = [p[1] for poly in polys for p in poly]
    if not xs or not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _dedupe(poly: Polyline) -> Polyline:
    out: Polyline = []
    for p in poly:
        if not out or abs(out[-1][0] - p[0]) > 1e-9 or abs(out[-1][1] - p[1]) > 1e-9:
            out.append(p)
    return out
