#!/usr/bin/env python3
"""
CustomTkinter GUI for generate.py.

Lets you pick which brands/variants to build, tweak pendant geometry, and
watch OpenSCAD render progress live, without touching the command line.

Requires (not installed in the dev sandbox this was written in - install on
your actual machine):
    pip install customtkinter
    # tkinter itself is a system package, not pip-installable:
    sudo dnf install python3-tkinter      # Fedora/Nobara
    sudo apt install python3-tk           # Debian/Ubuntu

Run with:
    python3 gui.py
"""
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError:
    sys.exit(
        "error: customtkinter is not installed.\n"
        "       pip install customtkinter"
    )

import generate as gen

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

OUTPUT_MODES = {
    "Embossed": ["emboss"],
    "Recessed": ["base"],
    "Recessed with inlays": ["base", "inlay"],
}
DEFAULT_OUTPUT_MODE = "Recessed with inlays"

PARAM_SPECS = [
    # (key, label, default)
    ("pendant_d", "Pendant diameter (mm)", gen.DEFAULT_PARAMS["pendant_d"]),
    ("base_h", "Base thickness (mm)", gen.DEFAULT_PARAMS["base_h"]),
    ("ring_id", "Ring inner diameter (mm)", gen.DEFAULT_PARAMS["ring_id"]),
    ("ring_od", "Ring outer diameter (mm)", gen.DEFAULT_PARAMS["ring_od"]),
    ("ring_overlap", "Ring/disc weld overlap (mm)", gen.DEFAULT_PARAMS["ring_overlap"]),
    ("logo_size", "Logo size (mm, long side)", gen.DEFAULT_PARAMS["logo_size"]),
    ("emboss_h", "Emboss height (mm)", gen.DEFAULT_PARAMS["emboss_h"]),
    ("engrave_d", "Engrave depth (mm)", gen.DEFAULT_PARAMS["engrave_d"]),
]


class PendantGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Car Logo Pendant Generator")
        self.geometry("980x680")
        self.minsize(820, 560)

        self.log_queue = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.current_proc = None
        self.brand_vars = {}
        self.param_entries = {}

        try:
            self.manifest = gen.load_manifest_or_raise()
        except Exception as exc:
            messagebox.showerror("Manifest error", str(exc))
            self.manifest = []

        self._build_layout()
        self.after(100, self._drain_log_queue)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_layout(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, width=260)
        sidebar.grid(row=0, column=0, sticky="nsw", padx=(10, 5), pady=10)
        sidebar.grid_propagate(False)

        ctk.CTkLabel(sidebar, text="Brands", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=10, pady=(10, 0)
        )

        btn_row = ctk.CTkFrame(sidebar, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=5)
        ctk.CTkButton(btn_row, text="All", width=70, command=self._select_all).pack(side="left", padx=(0, 5))
        ctk.CTkButton(btn_row, text="None", width=70, command=self._select_none).pack(side="left")

        brand_scroll = ctk.CTkScrollableFrame(sidebar, width=230, height=440)
        brand_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        for entry in self.manifest:
            var = tk.BooleanVar(value=True)
            self.brand_vars[entry["id"]] = var
            ctk.CTkCheckBox(brand_scroll, text=entry["name"], variable=var).pack(
                anchor="w", pady=2
            )

        if not self.manifest:
            ctk.CTkLabel(brand_scroll, text="(no brands found in\nlogos/manifest.json)").pack(pady=10)

    def _build_main(self):
        main = ctk.CTkFrame(self)
        main.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(4, weight=1)

        # --- what to generate ---
        variant_frame = ctk.CTkFrame(main)
        variant_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        ctk.CTkLabel(variant_frame, text="What to generate:", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=(10, 15), pady=10
        )
        self.output_mode_menu = ctk.CTkOptionMenu(variant_frame, values=list(OUTPUT_MODES.keys()), width=220)
        self.output_mode_menu.set(DEFAULT_OUTPUT_MODE)
        self.output_mode_menu.pack(side="left", padx=8)
        self.output_mode_hint = ctk.CTkLabel(variant_frame, text="", text_color="gray60")
        self.output_mode_hint.pack(side="left", padx=(10, 8))
        self.output_mode_menu.configure(command=self._on_output_mode_change)
        self._on_output_mode_change(DEFAULT_OUTPUT_MODE)

        # --- openscad path ---
        openscad_frame = ctk.CTkFrame(main)
        openscad_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        ctk.CTkLabel(openscad_frame, text="OpenSCAD binary:").pack(side="left", padx=(10, 5), pady=10)
        self.openscad_entry = ctk.CTkEntry(openscad_frame, width=420)
        self.openscad_entry.pack(side="left", padx=5, fill="x", expand=True)
        detected = gen.find_openscad_or_none()
        self.openscad_entry.insert(0, detected or "")
        if not detected:
            self.openscad_entry.configure(placeholder_text="not found - browse for it")
        ctk.CTkButton(openscad_frame, text="Browse...", width=90, command=self._browse_openscad).pack(
            side="left", padx=(5, 10)
        )

        # --- parameters ---
        param_frame = ctk.CTkFrame(main)
        param_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=5)
        ctk.CTkLabel(param_frame, text="Geometry", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(10, 5)
        )
        for i, (key, label, default) in enumerate(PARAM_SPECS):
            r, c = divmod(i, 4)
            cell = ctk.CTkFrame(param_frame, fg_color="transparent")
            cell.grid(row=r + 1, column=c, sticky="w", padx=10, pady=5)
            ctk.CTkLabel(cell, text=label, anchor="w").pack(anchor="w")
            entry = ctk.CTkEntry(cell, width=140)
            entry.insert(0, str(default))
            entry.pack(anchor="w")
            self.param_entries[key] = entry

        # --- run controls ---
        run_frame = ctk.CTkFrame(main, fg_color="transparent")
        run_frame.grid(row=3, column=0, sticky="ew", padx=10, pady=10)
        self.run_button = ctk.CTkButton(run_frame, text="Generate", command=self._start_build, width=140)
        self.run_button.pack(side="left")
        self.stop_button = ctk.CTkButton(
            run_frame, text="Stop", command=self._stop_build, width=100, state="disabled",
            fg_color="#a03030", hover_color="#c04040",
        )
        self.stop_button.pack(side="left", padx=8)
        self.open_output_button = ctk.CTkButton(
            run_frame, text="Open Output Folder", command=self._open_output, width=160
        )
        self.open_output_button.pack(side="left", padx=8)

        self.progress = ctk.CTkProgressBar(run_frame)
        self.progress.set(0)
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.progress_label = ctk.CTkLabel(run_frame, text="")
        self.progress_label.pack(side="left", padx=(0, 10))

        # --- log ---
        self.log_box = ctk.CTkTextbox(main, wrap="word", font=ctk.CTkFont(family="monospace", size=12))
        self.log_box.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _select_all(self):
        for var in self.brand_vars.values():
            var.set(True)

    def _select_none(self):
        for var in self.brand_vars.values():
            var.set(False)

    def _browse_openscad(self):
        path = filedialog.askopenfilename(title="Select openscad binary")
        if path:
            self.openscad_entry.delete(0, "end")
            self.openscad_entry.insert(0, path)

    def _on_output_mode_change(self, choice):
        hints = {
            "Embossed": "1 STL per brand - logo raised above the disc, single color print.",
            "Recessed": "1 STL per brand - logo cut into the disc, for paint-fill or single color.",
            "Recessed with inlays": "2 STLs per brand (base + inlay) - for MMU / color-change printing.",
        }
        self.output_mode_hint.configure(text=hints.get(choice, ""))

    def _open_output(self):
        gen.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        opener = {"linux": "xdg-open", "darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
        try:
            subprocess.Popen([opener, str(gen.OUTPUT_DIR)])
        except OSError as exc:
            messagebox.showerror("Could not open folder", str(exc))

    def _log(self, text):
        self.log_queue.put(text)

    def _drain_log_queue(self):
        try:
            while True:
                text = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", text + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    def _read_params(self):
        params = {}
        for key, label, _default in PARAM_SPECS:
            raw = self.param_entries[key].get().strip()
            try:
                params[key] = float(raw)
            except ValueError:
                raise ValueError(f"'{label}' must be a number, got: {raw!r}")
        return params

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def _start_build(self):
        if self.worker and self.worker.is_alive():
            return

        openscad = self.openscad_entry.get().strip()
        openscad = gen.find_openscad_or_none(openscad)
        if not openscad:
            messagebox.showerror(
                "OpenSCAD not found",
                "Could not find the openscad binary. Install it or use Browse... to point at it.",
            )
            return

        try:
            params = self._read_params()
        except ValueError as exc:
            messagebox.showerror("Invalid parameter", str(exc))
            return

        selected_brands = [e for e in self.manifest if self.brand_vars[e["id"]].get()]
        selected_variants = OUTPUT_MODES[self.output_mode_menu.get()]

        if not selected_brands:
            messagebox.showwarning("Nothing selected", "Select at least one brand.")
            return

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        self.stop_event.clear()
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.set(0)

        self.worker = threading.Thread(
            target=self._run_build,
            args=(openscad, selected_brands, selected_variants, params),
            daemon=True,
        )
        self.worker.start()

    def _stop_build(self):
        self.stop_event.set()
        if self.current_proc is not None:
            self.current_proc.terminate()
        self._log("--- stop requested, finishing current file then halting ---")

    def _run_build(self, openscad, brands, variants, params):
        tasks = [(b, v) for b in brands for v in variants]
        total = len(tasks)
        ok = 0

        for i, (entry, variant) in enumerate(tasks, start=1):
            if self.stop_event.is_set():
                self._log("Stopped.")
                break

            cmd, out_file, error = gen.build_command(openscad, entry, variant, params)
            if error:
                self._log(f"[{entry['name']}/{variant}] skipped: {error}")
                self.after(0, self._set_progress, i, total)
                continue

            self._log(f"[{entry['name']}/{variant}] rendering...")
            try:
                self.current_proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                )
                output, _ = self.current_proc.communicate()
                returncode = self.current_proc.returncode
            finally:
                self.current_proc = None

            if returncode == 0:
                ok += 1
                self._log(f"  -> {out_file.relative_to(gen.ROOT)}")
            else:
                self._log(f"  x FAILED ({returncode})")
                if output.strip():
                    self._log("    " + output.strip().replace("\n", "\n    "))

            self.after(0, self._set_progress, i, total)

        self._log(f"\n{ok}/{total} STL files generated into {gen.OUTPUT_DIR}")
        self.after(0, self._build_finished)

    def _set_progress(self, i, total):
        self.progress.set(i / total if total else 0)
        self.progress_label.configure(text=f"{i}/{total}")

    def _build_finished(self):
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")


if __name__ == "__main__":
    app = PendantGUI()
    app.mainloop()
