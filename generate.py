#!/usr/bin/env python3
"""
Batch-generate car logo keyring pendants from pendant.scad.

For every brand listed in logos/manifest.json, this calls the OpenSCAD CLI
three times to produce:
  output/<id>/<id>_emboss.stl   - single-piece, logo raised
  output/<id>/<id>_base.stl     - single-piece, logo recessed (MMU body color)
  output/<id>/<id>_inlay.stl    - logo plug that fills the base's cavity
                                   (MMU logo color; same coordinates as base)

Usage:
    python3 generate.py                  # build all brands, all variants
    python3 generate.py --only toyota bmw
    python3 generate.py --variants emboss
    python3 generate.py --openscad /usr/bin/openscad
    python3 generate.py --logo-margin 4 --base-h 3.5

Every logo is automatically scaled (aspect ratio preserved) so its bounding
box's *diagonal* fits within a circle of diameter (pendant_d - 2*logo_margin)
- this guarantees no logo can overhang the pendant edge, regardless of shape.
--logo-size overrides that auto-computed circle diameter directly if you want
an explicit size instead.

Per-brand geometry tweaks (for logos that need manual nudging/rotation after
a first look at the STL) can be set directly in manifest.json entries, e.g.:
    {"id": "mazda", ..., "y_offset": -1.5, "rotate": 0, "logo_size": 26}
Advanced: aspect_w/aspect_h override the auto-detected content aspect ratio,
for the rare SVG where the geometry-based auto-detection guesses wrong.
"""
import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCAD_FILE = ROOT / "pendant.scad"
MANIFEST_FILE = ROOT / "logos" / "manifest.json"
OUTPUT_DIR = ROOT / "output"

VARIANTS = ["emboss", "base", "inlay"]

DEFAULT_PARAMS = {
    "pendant_d": 40.0,
    "base_h": 3.0,
    "ring_id": 5.0,
    "ring_od": 9.0,
    "ring_overlap": 0.8,
    "logo_margin": 3.0,
    "logo_size": 0.0,
    "emboss_h": 1.0,
    "engrave_d": 1.0,
}


# ---------------------------------------------------------------------------
# SVG content bounding box (transform-aware), used to fit each logo's
# diagonal into a circle so it can never overhang the pendant edge - see
# svg_content_bbox() below for why this replaced a simpler viewBox-based guess.
# ---------------------------------------------------------------------------

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_NUM_RE = r"-?\d*\.?\d+(?:[eE][-+]?\d+)?"


def _mat_mul(m1, m2):
    """Compose two 2D affine matrices (a,b,c,d,e,f), applying m2 then m1."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _mat_apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _parse_transform(value):
    """Parse an SVG transform="..." attribute into a single composed 2D affine matrix."""
    mat = _IDENTITY
    if not value:
        return mat
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", value):
        nums = [float(x) for x in re.findall(_NUM_RE, args)]
        if name == "translate" and nums:
            tx = nums[0]
            ty = nums[1] if len(nums) > 1 else 0.0
            m = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif name == "scale" and nums:
            sx = nums[0]
            sy = nums[1] if len(nums) > 1 else sx
            m = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "rotate" and nums:
            ang = math.radians(nums[0])
            ca, sa = math.cos(ang), math.sin(ang)
            rot = (ca, sa, -sa, ca, 0.0, 0.0)
            if len(nums) >= 3:
                cx, cy = nums[1], nums[2]
                m = _mat_mul(_mat_mul((1, 0, 0, 1, cx, cy), rot), (1, 0, 0, 1, -cx, -cy))
            else:
                m = rot
        elif name == "matrix" and len(nums) >= 6:
            m = tuple(nums[:6])
        elif name == "skewX" and nums:
            m = (1.0, 0.0, math.tan(math.radians(nums[0])), 1.0, 0.0, 0.0)
        elif name == "skewY" and nums:
            m = (1.0, math.tan(math.radians(nums[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        mat = _mat_mul(mat, m)
    return mat


_FLOAT_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
_FLAG_RE = re.compile(r"[01]")


class _PathCursor:
    """Minimal scanner over an SVG path 'd' string. Needed instead of a flat
    regex token list because arc flags are exactly one digit and can appear
    with NO separator between them or the next number (e.g. "a5 5 0 01 5 5"
    is legal - the two flags are "0" and "1", not the number 1). A generic
    number regex would greedily swallow "01" as one token and desync every
    argument after it."""

    def __init__(self, s):
        self.s = s
        self.i = 0
        self.n = len(s)

    def skip_sep(self):
        while self.i < self.n and self.s[self.i] in " \t\r\n,":
            self.i += 1

    def peek_is_command(self):
        self.skip_sep()
        return self.i < self.n and self.s[self.i].isalpha()

    def read_command(self):
        self.skip_sep()
        c = self.s[self.i]
        self.i += 1
        return c

    def read_number(self):
        self.skip_sep()
        m = _FLOAT_RE.match(self.s, self.i)
        if not m:
            raise ValueError(f"expected number at offset {self.i}")
        self.i = m.end()
        return float(m.group())

    def read_flag(self):
        self.skip_sep()
        m = _FLAG_RE.match(self.s, self.i)
        if not m:
            raise ValueError(f"expected arc flag (0/1) at offset {self.i}")
        self.i = m.end()
        return int(m.group())

    def at_end(self):
        self.skip_sep()
        return self.i >= self.n


def _path_points(d):
    """Extract every anchor + control point from a path's d="..." attribute, with
    relative commands resolved to absolute local coordinates. Curve control points
    are included (not just endpoints): by the convex-hull property of Bezier
    curves, the curve always lies within its control points' bounding box, so
    this is always a safe (never-too-small) approximation of the true bbox -
    an elliptical arc's bulge is padded in similarly (see 'A'/'a' below).

    Parse errors (malformed/truncated data) return whatever points were
    collected before the error, rather than failing the whole file."""
    c = _PathCursor(d)
    points = []
    cmd = None
    cx = cy = 0.0
    start_x = start_y = 0.0

    try:
        while not c.at_end():
            if c.peek_is_command():
                cmd = c.read_command()
            elif cmd is None:
                break  # malformed: numbers with no command yet - bail safely

            if cmd in ("M", "m"):
                x, y = c.read_number(), c.read_number()
                if cmd == "m" and points:
                    x += cx
                    y += cy
                cx, cy = x, y
                start_x, start_y = cx, cy
                points.append((cx, cy))
                cmd = "L" if cmd == "M" else "l"  # subsequent pairs are implicit lineto
            elif cmd in ("L", "l"):
                x, y = c.read_number(), c.read_number()
                if cmd == "l":
                    x += cx
                    y += cy
                cx, cy = x, y
                points.append((cx, cy))
            elif cmd in ("H", "h"):
                x = c.read_number()
                if cmd == "h":
                    x += cx
                cx = x
                points.append((cx, cy))
            elif cmd in ("V", "v"):
                y = c.read_number()
                if cmd == "v":
                    y += cy
                cy = y
                points.append((cx, cy))
            elif cmd in ("C", "c"):
                x1, y1, x2, y2, x, y = (c.read_number() for _ in range(6))
                if cmd == "c":
                    x1 += cx; y1 += cy; x2 += cx; y2 += cy; x += cx; y += cy
                points += [(x1, y1), (x2, y2), (x, y)]
                cx, cy = x, y
            elif cmd in ("S", "s"):
                x2, y2, x, y = (c.read_number() for _ in range(4))
                if cmd == "s":
                    x2 += cx; y2 += cy; x += cx; y += cy
                points += [(x2, y2), (x, y)]
                cx, cy = x, y
            elif cmd in ("Q", "q"):
                x1, y1, x, y = (c.read_number() for _ in range(4))
                if cmd == "q":
                    x1 += cx; y1 += cy; x += cx; y += cy
                points += [(x1, y1), (x, y)]
                cx, cy = x, y
            elif cmd in ("T", "t"):
                x, y = c.read_number(), c.read_number()
                if cmd == "t":
                    x += cx
                    y += cy
                points.append((x, y))
                cx, cy = x, y
            elif cmd in ("A", "a"):
                rx, ry = c.read_number(), c.read_number()
                _rot = c.read_number()
                _laf, _sf = c.read_flag(), c.read_flag()
                x, y = c.read_number(), c.read_number()
                if cmd == "a":
                    x += cx
                    y += cy
                # Not solving true arc extrema - pad both ends by the radii so the
                # bulge can't exceed this box (safe overestimate, never too small).
                for px, py in ((cx, cy), (x, y)):
                    points += [(px - rx, py - ry), (px + rx, py + ry)]
                cx, cy = x, y
            elif cmd in ("Z", "z"):
                cx, cy = start_x, start_y
                cmd = None  # closepath never implicitly repeats; require an explicit next command
            else:
                break  # unrecognized command letter - bail out safely
    except ValueError:
        pass  # malformed/truncated data - keep whatever points were already found

    return points


def _shape_local_points(elem, tag):
    def f(attr, default=0.0):
        v = elem.get(attr)
        try:
            return float(v) if v not in (None, "") else default
        except ValueError:
            return default

    if tag == "path":
        d = elem.get("d")
        return _path_points(d) if d else []
    if tag == "rect":
        x, y, w, h = f("x"), f("y"), f("width"), f("height")
        return [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
    if tag in ("circle", "ellipse"):
        cx, cy = f("cx"), f("cy")
        rx = f("r") if tag == "circle" else f("rx")
        ry = f("r") if tag == "circle" else f("ry")
        n = 16
        return [
            (cx + rx * math.cos(2 * math.pi * k / n), cy + ry * math.sin(2 * math.pi * k / n))
            for k in range(n)
        ]
    if tag in ("polygon", "polyline"):
        nums = [float(x) for x in re.findall(_NUM_RE, elem.get("points", ""))]
        return list(zip(nums[0::2], nums[1::2]))
    return []


_NON_RENDERED_TAGS = {"defs", "clippath", "mask", "symbol", "pattern", "metadata", "title", "desc"}


def svg_content_bbox(svg_path: Path):
    """Compute the bounding box of everything actually drawn in an SVG, walking
    the tree and composing ancestor transforms so nested <g transform=...>
    groups (common in real-world logo exports) don't throw off the result.

    Returns (xmin, ymin, xmax, ymax) in the SVG's own local units, or None if
    no drawable geometry was found. Used only to derive an aspect ratio for
    fitting the logo into a circle - see build_command().
    """
    try:
        root = ET.parse(svg_path).getroot()
    except ET.ParseError:
        return None

    bounds = [math.inf, math.inf, -math.inf, -math.inf]  # xmin, ymin, xmax, ymax

    def walk(elem, matrix):
        tag = _local_tag(elem).lower()
        if tag in _NON_RENDERED_TAGS:
            return
        m = _mat_mul(matrix, _parse_transform(elem.get("transform")))
        if tag in SHAPE_TAGS:
            for x, y in _shape_local_points(elem, tag):
                px, py = _mat_apply(m, x, y)
                if px < bounds[0]:
                    bounds[0] = px
                if py < bounds[1]:
                    bounds[1] = py
                if px > bounds[2]:
                    bounds[2] = px
                if py > bounds[3]:
                    bounds[3] = py
        for child in elem:
            walk(child, m)

    walk(root, _IDENTITY)
    if bounds[0] is math.inf:
        return None
    return tuple(bounds)


FILTERED_DIR = ROOT / "logos" / "_filtered"

NAMED_COLORS = {"white": "#ffffff", "black": "#000000", "none": "none"}
SHAPE_TAGS = {"path", "circle", "rect", "ellipse", "polygon", "polyline"}


def _local_tag(elem):
    return elem.tag.split("}")[-1]


def _normalize_color(value):
    """Normalize a fill value for comparison: lowercase, expand #abc -> #aabbcc,
    map a few common names. Gradient/pattern references (url(#...)) collapse to
    the sentinel "gradient" so callers can keep/drop them as a group without
    knowing the specific def id."""
    v = value.strip().lower()
    if v.startswith("url("):
        return "gradient"
    if v in NAMED_COLORS:
        return NAMED_COLORS[v]
    if v.startswith("#") and len(v) == 4:
        return "#" + "".join(ch * 2 for ch in v[1:])
    return v


def _get_fill(elem):
    style = elem.get("style")
    if style:
        for part in style.split(";"):
            if ":" in part:
                key, val = part.split(":", 1)
                if key.strip() == "fill":
                    return val.strip()
    return elem.get("fill", "#000000")  # SVG default fill is black


def filter_svg_by_fill(svg_path: Path, keep_fill=None, drop_fill=None) -> Path:
    """Return a path to a version of svg_path containing only shapes whose
    fill matches `keep_fill` (if given) and doesn't match `drop_fill` (if
    given). Group/transform structure is preserved (elements are pruned
    in-place, not re-parented), so ancestor transforms still apply.

    Used for "badge" logos that layer a solid background shape (a disc,
    oval, shield...) underneath a lighter foreground detail: OpenSCAD's
    SVG import unions every path regardless of paint order, so without
    filtering, the background shape swallows the detail entirely (see
    Ford: navy oval + white ring + white script -> importing all three
    collapses to just the oval, since the white shapes are wholly inside
    it). keep_fill=["#ffffff"] keeps just the ring+script.

    Returns svg_path unchanged if neither keep_fill nor drop_fill is given.
    """
    if not keep_fill and not drop_fill:
        return svg_path

    keep_norm = {_normalize_color(c) for c in keep_fill} if keep_fill else None
    drop_norm = {_normalize_color(c) for c in drop_fill} if drop_fill else None

    # Keep the SVG namespace unprefixed on output (<svg>/<path>, not <ns0:svg>/<ns0:path>) -
    # some lenient SVG parsers don't do full namespace resolution.
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree = ET.parse(svg_path)
    root = tree.getroot()

    def prune(elem):
        for child in list(elem):
            if _local_tag(child) in SHAPE_TAGS:
                fill = _normalize_color(_get_fill(child))
                if keep_norm is not None and fill not in keep_norm:
                    elem.remove(child)
                    continue
                if drop_norm is not None and fill in drop_norm:
                    elem.remove(child)
                    continue
            else:
                prune(child)

    prune(root)

    FILTERED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = FILTERED_DIR / svg_path.name
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
    return out_path


def find_openscad_or_none(explicit=None):
    """Resolve the openscad binary path, or None if it can't be found."""
    if explicit:
        return explicit if shutil.which(explicit) or Path(explicit).exists() else None
    return shutil.which("openscad")


def find_openscad(explicit):
    found = find_openscad_or_none(explicit)
    if found:
        return found
    sys.exit(
        "error: 'openscad' not found on PATH. Install it (e.g. `sudo dnf install openscad`)\n"
        "       or pass --openscad /path/to/openscad."
    )


def load_manifest_or_raise():
    """Load logos/manifest.json, raising on any problem. Used by the GUI,
    which needs to report errors in a dialog rather than exiting the process."""
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(f"manifest not found at {MANIFEST_FILE}")
    with open(MANIFEST_FILE) as f:
        entries = json.load(f)
    for e in entries:
        for key in ("name", "id", "file"):
            if key not in e:
                raise ValueError(f"manifest entry missing '{key}': {e}")
    return entries


def load_manifest():
    try:
        return load_manifest_or_raise()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        sys.exit(f"error: {exc}")


def scad_str(value: str) -> str:
    """Escape a string for safe use inside an OpenSCAD -D "..." literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_command(openscad, entry, variant, params):
    """Build the OpenSCAD CLI command for one brand/variant.

    `params` is a dict with the keys in DEFAULT_PARAMS. Per-brand overrides
    for logo_size/y_offset/x_offset/rotate/aspect_w/aspect_h come from the
    manifest entry itself.

    Returns (cmd, out_file, error). `cmd`/`out_file` are None if `error` is set.
    """
    brand_id = entry["id"]
    svg_path = ROOT / entry["file"]
    if not svg_path.exists():
        return None, None, f"{svg_path} not found"

    keep_fill = entry.get("keep_fill")
    drop_fill = entry.get("drop_fill")
    if keep_fill or drop_fill:
        svg_path = filter_svg_by_fill(svg_path, keep_fill=keep_fill, drop_fill=drop_fill)

    # Aspect ratio of the actual drawn content (post keep_fill/drop_fill), used
    # by pendant.scad to fit the logo's bounding-box *diagonal* into a circle
    # so it can't overhang the pendant edge regardless of shape. Only the
    # ratio matters here, not absolute units - see svg_content_bbox().
    aspect_w = entry.get("aspect_w")
    aspect_h = entry.get("aspect_h")
    if aspect_w is None or aspect_h is None:
        bbox = svg_content_bbox(svg_path)
        if bbox is None:
            print(f"  ! warning: no drawable geometry found in {svg_path.name}, assuming square aspect")
            aspect_w, aspect_h = 1.0, 1.0
        else:
            xmin, ymin, xmax, ymax = bbox
            aspect_w, aspect_h = max(xmax - xmin, 1e-6), max(ymax - ymin, 1e-6)

    out_dir = OUTPUT_DIR / brand_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{brand_id}_{variant}.stl"

    defs = {
        "variant": variant,
        "logo_svg": str(svg_path),
        "logo_aspect_w": aspect_w,
        "logo_aspect_h": aspect_h,
        "pendant_d": params["pendant_d"],
        "base_h": params["base_h"],
        "ring_id": params["ring_id"],
        "ring_od": params["ring_od"],
        "ring_overlap": params["ring_overlap"],
        "logo_margin": params["logo_margin"],
        "logo_size": entry.get("logo_size", params["logo_size"]),
        "logo_y_offset": entry.get("y_offset", 0),
        "logo_x_offset": entry.get("x_offset", 0),
        "logo_rotate": entry.get("rotate", 0),
        "emboss_h": params["emboss_h"],
        "engrave_d": params["engrave_d"],
    }

    cmd = [openscad, "-o", str(out_file)]
    for key, val in defs.items():
        if isinstance(val, bool):
            literal = "true" if val else "false"
        elif isinstance(val, (int, float)):
            literal = repr(val)
        else:
            literal = f'"{scad_str(str(val))}"'
        cmd += ["-D", f"{key}={literal}"]
    cmd.append(str(SCAD_FILE))

    return cmd, out_file, None


def build_one(openscad, entry, variant, params):
    brand_id = entry["id"]
    cmd, out_file, error = build_command(openscad, entry, variant, params)
    if error:
        print(f"  ! skip {brand_id}/{variant}: {error}")
        return False

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  x FAILED {brand_id}/{variant}")
        print("    " + result.stderr.strip().replace("\n", "\n    "))
        return False

    print(f"  - {out_file.relative_to(ROOT)}")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="only build these brand ids")
    ap.add_argument("--variants", nargs="*", choices=VARIANTS, default=VARIANTS)
    ap.add_argument("--openscad", help="path to openscad binary")
    ap.add_argument("--pendant-d", type=float, default=DEFAULT_PARAMS["pendant_d"])
    ap.add_argument("--base-h", type=float, default=DEFAULT_PARAMS["base_h"])
    ap.add_argument("--ring-id", type=float, default=DEFAULT_PARAMS["ring_id"])
    ap.add_argument("--ring-od", type=float, default=DEFAULT_PARAMS["ring_od"])
    ap.add_argument("--ring-overlap", type=float, default=DEFAULT_PARAMS["ring_overlap"])
    ap.add_argument(
        "--logo-margin", type=float, default=DEFAULT_PARAMS["logo_margin"],
        help="gap (mm) between the logo's fitted bounding circle and the pendant edge",
    )
    ap.add_argument(
        "--logo-size", type=float, default=DEFAULT_PARAMS["logo_size"],
        help="explicit logo bounding-circle diameter (mm), overriding --logo-margin. 0 = auto (default)",
    )
    ap.add_argument("--emboss-h", type=float, default=DEFAULT_PARAMS["emboss_h"])
    ap.add_argument("--engrave-d", type=float, default=DEFAULT_PARAMS["engrave_d"])
    args = ap.parse_args()

    openscad = find_openscad(args.openscad)
    entries = load_manifest()
    if args.only:
        wanted = set(args.only)
        entries = [e for e in entries if e["id"] in wanted]
        missing = wanted - {e["id"] for e in entries}
        if missing:
            print(f"warning: unknown brand ids ignored: {', '.join(sorted(missing))}")

    if not entries:
        sys.exit("error: nothing to build (empty/filtered manifest)")

    params = {
        "pendant_d": args.pendant_d,
        "base_h": args.base_h,
        "ring_id": args.ring_id,
        "ring_od": args.ring_od,
        "ring_overlap": args.ring_overlap,
        "logo_margin": args.logo_margin,
        "logo_size": args.logo_size,
        "emboss_h": args.emboss_h,
        "engrave_d": args.engrave_d,
    }

    total = ok = 0
    for entry in entries:
        print(f"[{entry['name']}]")
        for variant in args.variants:
            total += 1
            if build_one(openscad, entry, variant, params):
                ok += 1

    print(f"\n{ok}/{total} STL files generated into {OUTPUT_DIR}")
    if ok < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
