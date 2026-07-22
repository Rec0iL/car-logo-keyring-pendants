# Car Logo Keyring Pendant Creator

Generates 3D-printable keyring pendants (40mm disc + keyring loop) with car
brand logos, either embossed or engraved. Engraved pendants are split into a
base part + logo inlay for dual-color / MMU printing.

## Layout

```
pendant.scad        parametric OpenSCAD model (single source of truth for geometry)
generate.py          CLI batch driver: manifest.json -> openscad CLI -> STL files
gui.py                customtkinter GUI wrapper around generate.py
logos/
  manifest.json       brand -> SVG file mapping (+ optional per-brand tweaks)
  SOURCES.md          where each logo came from + its license tag on Commons
  *.svg                the downloaded logo vector files
output/
  <brand_id>/
    <brand_id>_emboss.stl   single-piece print, logo raised above the disc
    <brand_id>_base.stl     single-piece print, logo recessed into the disc
    <brand_id>_inlay.stl    logo plug that exactly fills the _base.stl cavity
```

## Requirements

- OpenSCAD >= 2019.05 (needs SVG import support). Not installed in this
  sandbox — install it on your actual machine, e.g.:
  `sudo dnf install openscad` (Fedora/Nobara) or grab an AppImage from
  https://openscad.org/downloads.html
- Python 3.8+ (`generate.py` itself is stdlib only, no extra pip packages needed)
- For the GUI (`gui.py`) only: `tkinter` (a system package, not pip-installable)
  and `customtkinter`:
  ```sh
  sudo dnf install python3-tkinter     # Fedora/Nobara
  # sudo apt install python3-tk        # Debian/Ubuntu
  pip install customtkinter
  ```

## GUI

```sh
python3 gui.py
```

- Left panel: check which brands to build (All/None shortcuts at the top).
- Top of the main panel: which variants to build (emboss / base / inlay),
  and the OpenSCAD binary path (auto-detected from PATH; use Browse... if
  it's not found).
- Geometry fields mirror `generate.py`'s `--pendant-d`, `--base-h`, etc.
  flags, pre-filled with the same defaults.
- **Generate** runs OpenSCAD in a background thread (GUI stays responsive),
  streaming each render's status into the log box and updating the progress
  bar; **Stop** finishes the file currently rendering and halts before the
  next one. **Open Output Folder** opens `output/` in your file manager.
- Per-brand tweaks (`y_offset`, `rotate`, `logo_size`) are still edited in
  `logos/manifest.json` directly — the GUI doesn't expose those, since
  they're meant to be set once per problem logo, not per run.

## CLI Usage

```sh
# build everything (all brands, all 3 variants)
python3 generate.py

# just a couple of brands
python3 generate.py --only bmw toyota porsche

# just the single-piece embossed versions
python3 generate.py --variants emboss

# override geometry globally
python3 generate.py --logo-size 26 --base-h 3.5 --engrave-d 1.2

# point at a specific openscad binary if it's not on PATH
python3 generate.py --openscad /usr/bin/openscad
```

Each run prints progress and a final `<ok>/<total> STL files generated`
summary; any OpenSCAD errors (e.g. a malformed SVG) are printed inline and
that brand is skipped rather than aborting the whole batch.

## Geometry

- **Pendant disc**: 40mm diameter (`--pendant-d`), 3mm thick (`--base-h`) by default.
- **Keyring loop**: 5mm ID / 9mm OD (`--ring-id` / `--ring-od`), positioned at
  the top of the disc so the loop's inner circle touches the disc's outer
  circle. A literal tangent point is a single-point (non-manifold-friendly,
  fragile) connection, so by default the loop is nudged in by `--ring-overlap`
  (0.8mm) to weld it solidly to the disc. Set `--ring-overlap 0` for an exact
  tangent if you really want it, but that's not recommended for printing.
- **Logo**: scaled to fit within a `--logo-size` (default 30mm) box on the
  longer side, aspect ratio preserved, centered on the disc.
- **Emboss**: logo stands `--emboss-h` (1.0mm) proud of the top face.
- **Engrave**: logo is cut `--engrave-d` (1.0mm) deep into the top face,
  leaving a 2mm floor with the default 3mm base thickness.

## Printing: single color (emboss) vs. dual color (engrave + inlay)

- **Emboss**: print `<brand>_emboss.stl` as-is, any single color. The logo
  reads via light/shadow on the raised relief.
- **Engrave + inlay (MMU / color-change)**: `_base.stl` and `_inlay.stl` are
  generated at *identical coordinates*, so you can:
  - Load both STLs into the same PrusaSlicer/OrcaSlicer project (Add ->
    both files, don't move either one) and assign each a different
    filament/extruder — they'll interlock perfectly since the inlay was
    extruded to exactly fill the base's cavity.
  - Or, for a single-extruder printer, print `_base.stl`, pause at the
    layer where the cavity starts, and manually swap filament color (classic
    "color change" keychain technique) — though since inlay/base are two
    separate solids here, gluing a separately-printed inlay in is also an option.
  - Or just print `_base.stl` alone and paint-fill the recessed logo by hand.

## Adjusting individual logos

Some source SVGs may need a manual nudge (off-center art within the SVG
canvas, or a shape that imports mirrored/rotated). Add overrides directly to
the brand's entry in `logos/manifest.json`:

```json
{
  "name": "Mazda",
  "id": "mazda",
  "file": "logos/mazda.svg",
  "y_offset": -1.5,
  "x_offset": 0,
  "rotate": 0,
  "logo_size": 26
}
```

To preview a single logo interactively before batch-rendering everything,
open `pendant.scad` in the OpenSCAD GUI, set `logo_svg` (and `variant =
"preview"`) in the Customizer or at the top of the file, and hit F5.

## Two-tone / "badge" logos: keep_fill and drop_fill

OpenSCAD's SVG import does **not** composite shapes in paint order like a
browser does — it just unions every shape's filled area into one flat 2D
region, regardless of color or z-order. That's invisible for a single-color
logo, but it breaks any "badge" logo built as a solid background shape (an
oval, circle, shield...) with a lighter detail drawn on top: the detail is
geometrically *inside* the background shape, so unioning it in is a no-op
and all you get is the plain background silhouette.

This bit us with Ford: `ford.svg` is a navy oval (`fill:#00095b`) plus a
white ring and white "Ford" script (`fill:#ffffff`) on top of it. Importing
all three collapsed to just the oval — no ring, no script.

Fix: add `keep_fill` (allow-list) or `drop_fill` (block-list) to that
brand's `manifest.json` entry, and `generate.py` strips non-matching shapes
out of the SVG before handing it to OpenSCAD:

```json
{
  "name": "Ford",
  "id": "ford",
  "file": "logos/ford.svg",
  "keep_fill": ["#ffffff"]
}
```

Notes:
- Colors are matched case-insensitively; `#fff` and `#FFFFFF` are treated
  as identical.
- Use the special value `"gradient"` in either list to match/exclude any
  `fill="url(#...)"` shape (gradients and patterns) as a group, without
  needing to know the specific def id — handy for stripping decorative
  shading off a glossy "3D badge" render.
- To find the right colors, open the SVG in a text editor and check each
  `<path>`'s `fill="..."` attribute (or `fill:` inside a `style="..."`
  attribute — `style` wins if both are present on the same element).
- This can't rescue a logo whose *meaningful* shape is itself gradient-filled
  (no way to `keep_fill=["gradient"]` and get just the one shape you want
  if there are several gradient shapes) — those need a cleaner source SVG
  instead (see below).

## Known logo quirks (see logos/SOURCES.md for full detail)

- **Peugeot** and **Jeep**: Commons had no clean vector of their current pictorial
  emblems (Peugeot's lion head, Jeep's 7-slot grille), so these use the brand
  wordmark instead.
- **Subaru**: ~1.5MB SVG (much heavier than the others) — renders correctly but
  OpenSCAD may take noticeably longer on it; run it through `svgo` or Inkscape's
  "Simplify" if it's too slow.
- **BMW** and **Mercedes-Benz**: the original Commons uploads were glossy
  gradient-shaded "3D badge" renders unsuitable for flat print geometry;
  both were replaced with genuinely flat vector alternatives (no filter
  needed for either — see `logos/SOURCES.md` for why their structure is
  safe to union directly).
- **Škoda**: no flat replacement exists on Commons. Uses
  `"keep_fill": ["#42bd3b"]` to isolate just the green winged-arrow from the
  gradient-shaded ring bezel — as a side effect this also drops the "ŠKODA"
  wordmark, since it happens to share the exact same hex color as the ring
  and can't be separated by fill alone. Worth a look in the OpenSCAD GUI
  before printing; see `logos/SOURCES.md` for the full trade-off writeup.
- **Wordmark logos** (Hyundai, Mazda, Kia, Chevrolet, Ford, Audi, etc.) are very
  wide/thin (aspect ratios up to ~8:1). At the default `logo_size=30`, the short
  axis can end up just a few mm tall once engraved. If text detail gets lost,
  either bump that brand's `logo_size` in `manifest.json`, or reduce `engrave_d`/
  increase `emboss_h` so shallower relief keeps thin strokes structurally sound.
- If a logo ever imports blank, mirrored, or oddly cropped, the usual fix is to
  re-save the source SVG in Inkscape as "Plain SVG" (flattens transforms/CSS
  classes/defs into plain paths) and re-point `manifest.json` at the cleaned file.

## License

The code in this repo (`pendant.scad`, `generate.py`, `gui.py`) is MIT
licensed — see `LICENSE`.

That license does **not** extend to the contents of `logos/`. Car brand
logos are trademarks of their respective manufacturers. The SVGs there were
sourced from Wikimedia Commons for personal, non-commercial hobby use (see
`logos/SOURCES.md` for provenance/license tags per file). This is a common
practice for personal 3D-printed keychains, but these files/models are not
licensed for resale or commercial distribution.
