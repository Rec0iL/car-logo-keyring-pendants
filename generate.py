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
    python3 generate.py --logo-size 28 --base-h 3.5

Per-brand geometry tweaks (for logos that need manual nudging/rotation after
a first look at the STL) can be set directly in manifest.json entries, e.g.:
    {"id": "mazda", ..., "y_offset": -1.5, "rotate": 0, "logo_size": 26}
"""
import argparse
import json
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
    "logo_size": 30.0,
    "emboss_h": 1.0,
    "engrave_d": 1.0,
}

LENGTH_UNITS = ("px", "mm", "cm", "in", "pt", "pc", "%")


def parse_length(value):
    if value is None:
        return None
    value = value.strip()
    for unit in LENGTH_UNITS:
        if value.endswith(unit):
            value = value[: -len(unit)]
            break
    try:
        return float(value)
    except ValueError:
        return None


def svg_is_wide(svg_path: Path) -> bool:
    """True if the SVG's bounding box is wider than it is tall (or square)."""
    try:
        root = ET.parse(svg_path).getroot()
    except ET.ParseError as exc:
        print(f"  ! warning: could not parse {svg_path.name} ({exc}), assuming wide=true")
        return True

    view_box = root.get("viewBox")
    if view_box:
        parts = view_box.replace(",", " ").split()
        if len(parts) == 4:
            try:
                w, h = float(parts[2]), float(parts[3])
                if w > 0 and h > 0:
                    return w >= h
            except ValueError:
                pass

    w = parse_length(root.get("width"))
    h = parse_length(root.get("height"))
    if w and h:
        return w >= h

    print(f"  ! warning: no usable viewBox/width/height in {svg_path.name}, assuming wide=true")
    return True


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
    for logo_size/y_offset/x_offset/rotate come from the manifest entry
    itself, same as the "wide" auto-detection.

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

    wide = entry.get("wide")
    if wide is None:
        wide = svg_is_wide(svg_path)

    out_dir = OUTPUT_DIR / brand_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{brand_id}_{variant}.stl"

    defs = {
        "variant": variant,
        "logo_svg": str(svg_path),
        "logo_wide": bool(wide),
        "pendant_d": params["pendant_d"],
        "base_h": params["base_h"],
        "ring_id": params["ring_id"],
        "ring_od": params["ring_od"],
        "ring_overlap": params["ring_overlap"],
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
    ap.add_argument("--logo-size", type=float, default=DEFAULT_PARAMS["logo_size"])
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
