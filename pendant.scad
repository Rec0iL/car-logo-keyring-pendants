// ============================================================================
// Car Logo Keyring Pendant Generator
// ============================================================================
// Parametric pendant: a round disc with a keyring loop at the top, and a car
// logo either embossed (raised) or engraved (recessed) into the top face.
//
// This file is normally driven by generate.py, which invokes OpenSCAD once
// per brand/variant via -D command line overrides. It can also be opened
// directly in the OpenSCAD GUI to preview/tweak a single logo (edit the
// "single-run defaults" block below).
//
// Variants (set via `variant`):
//   "emboss"  -> single-piece pendant, logo raised above the top face.
//   "base"    -> single-piece pendant, logo recessed (cut) into the top face.
//                Prints as the "body" color for MMU / colour-change prints.
//   "inlay"   -> just the logo, extruded to exactly fill the "base" cavity.
//                Prints as the "logo" color; loads at the SAME coordinates
//                as "base" so both STLs can be imported together (e.g. as
//                separate objects in PrusaSlicer/OrcaSlicer / a multi-part
//                3MF) and they will align without any manual positioning.
//   "preview" -> base (grey) + inlay (colour) shown together, for a quick
//                visual sanity check of logo scale/centering in the GUI.
// ============================================================================

/* [Pendant] */
pendant_d      = 40;    // overall pendant diameter (mm)
base_h         = 3.0;   // base disc thickness (mm)

/* [Keyring loop] */
ring_id        = 5;     // keyring loop inner diameter (mm) - fits a standard split ring
ring_od        = 9;     // keyring loop outer diameter (mm)
ring_h         = base_h;// loop thickness (mm), defaults to base thickness
ring_overlap   = 0.8;   // how far the loop's inner circle bites into the disc's
                         // outer circle, in mm. 0 = the loop ID exactly touches
                         // the disc OD at a single tangent point (fragile, not
                         // recommended for printing). ~0.6-1.0mm gives a solid
                         // printable weld between loop and disc.

/* [Logo] */
logo_svg       = "";    // path to the logo SVG, e.g. "logos/toyota.svg". Empty = blank disc.
logo_margin    = 3;     // gap (mm) between the logo's fitted bounding circle and the pendant edge
logo_size      = 0;     // explicit bounding-circle diameter (mm), overriding logo_margin. 0 = auto
logo_aspect_w  = 1;     // content bounding-box width (auto-computed by generate.py; only the
logo_aspect_h  = 1;     // aspect_w:aspect_h ratio matters, not absolute units)
logo_y_offset  = 0;     // manual nudge, mm, for logos whose visual center isn't their bbox center
logo_x_offset  = 0;
logo_rotate    = 0;     // manual rotation in degrees, for SVGs that import sideways/upside-down
emboss_h       = 1.0;   // how far the logo stands proud of the top face (mm), emboss variant
engrave_d      = 1.0;   // how deep the logo is cut into the top face (mm), base/inlay variant

/* [Render] */
variant        = "preview"; // "emboss" | "base" | "inlay" | "preview"
$fn            = 128;

// ----------------------------------------------------------------------------
// Derived geometry
// ----------------------------------------------------------------------------
pendant_r = pendant_d / 2;
ring_ir   = ring_id / 2;
ring_or   = ring_od / 2;
ring_cy   = pendant_r + ring_ir - ring_overlap; // loop center, along +Y

// Diameter of the circle the logo's bounding-box diagonal must fit within.
// logo_size > 0 overrides the auto (pendant-relative) value.
logo_fit_d    = (logo_size > 0) ? logo_size : (pendant_d - 2 * logo_margin);
// Split logo_fit_d between width/height in proportion to the content's own
// aspect ratio, such that target_w^2 + target_h^2 = logo_fit_d^2 exactly.
// resize() below then forces the *actual* geometry to exactly these
// dimensions - so the diagonal-fits-in-a-circle guarantee holds regardless
// of any approximation error in how logo_aspect_w/h were measured.
_logo_aspect_norm = sqrt(logo_aspect_w * logo_aspect_w + logo_aspect_h * logo_aspect_h);
logo_target_w = logo_fit_d * logo_aspect_w / _logo_aspect_norm;
logo_target_h = logo_fit_d * logo_aspect_h / _logo_aspect_norm;

// ----------------------------------------------------------------------------
// 2D profile: disc + keyring loop, with the loop hole punched through the
// final union so it stays a clean through-hole even where it overlaps the
// disc's own edge.
// ----------------------------------------------------------------------------
module pendant_outline_2d() {
    difference() {
        union() {
            circle(r = pendant_r);
            translate([0, ring_cy]) circle(r = ring_or);
        }
        translate([0, ring_cy]) circle(r = ring_ir);
    }
}

// ----------------------------------------------------------------------------
// Logo footprint, imported from SVG and scaled (aspect ratio preserved) so
// its bounding box is exactly logo_target_w x logo_target_h - sized so that
// box's diagonal equals logo_fit_d, guaranteeing it fits within a circle of
// that diameter no matter the logo's shape. Centered at origin.
// ----------------------------------------------------------------------------
module logo_2d() {
    if (logo_svg != "") {
        translate([logo_x_offset, logo_y_offset])
        rotate([0, 0, logo_rotate])
        resize([logo_target_w, logo_target_h, 0], auto = [false, false, true])
            import(logo_svg, center = true);
    }
}

// ----------------------------------------------------------------------------
// Solids
// ----------------------------------------------------------------------------
module pendant_base_solid() {
    linear_extrude(height = base_h)
        pendant_outline_2d();
}

module logo_emboss_solid() {
    translate([0, 0, base_h])
        linear_extrude(height = emboss_h)
            logo_2d();
}

// Slightly overshoots top/bottom of the cut so the subtraction leaves a
// clean face with no coincident (zero-thickness) surfaces.
module logo_cavity_cutter() {
    eps = 0.02;
    translate([0, 0, base_h - engrave_d])
        linear_extrude(height = engrave_d + eps)
            logo_2d();
}

// The plug that exactly fills the engraved cavity - same footprint, no
// eps overshoot, so it sits flush with the top face when inserted.
module logo_inlay_solid() {
    translate([0, 0, base_h - engrave_d])
        linear_extrude(height = engrave_d)
            logo_2d();
}

module pendant_emboss() {
    union() {
        pendant_base_solid();
        logo_emboss_solid();
    }
}

module pendant_engraved_base() {
    difference() {
        pendant_base_solid();
        logo_cavity_cutter();
    }
}

// ----------------------------------------------------------------------------
// Output selection
// ----------------------------------------------------------------------------
if (variant == "emboss") {
    pendant_emboss();
} else if (variant == "base") {
    pendant_engraved_base();
} else if (variant == "inlay") {
    logo_inlay_solid();
} else {
    // preview: base in grey, inlay in a contrasting color, shown together
    color("lightgrey") pendant_engraved_base();
    color("crimson")   logo_inlay_solid();
}
