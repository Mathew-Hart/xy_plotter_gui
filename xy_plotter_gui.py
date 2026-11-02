#!/usr/bin/env python3
"""Tkinter GUI for plotting SVG files on a Makeblock XY plotter."""

from __future__ import annotations

import math
import os
import queue
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

try:
    from .serial_link import PosixSerial, SerialError, list_serial_ports
    from .svg_model import Point, Polyline, SvgDrawing, load_svg
except ImportError:
    from serial_link import PosixSerial, SerialError, list_serial_ports
    from svg_model import Point, Polyline, SvgDrawing, load_svg


DEFAULT_PORT = "/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0"


class PlotterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Makeblock XY Plotter")
        self.minsize(1120, 720)

        self.serial = PosixSerial()
        self.drawing: SvgDrawing | None = None
        self.svg_path = ""
        self.machine_polys: list[Polyline] = []
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.cancel_event = threading.Event()
        self.ready_event = threading.Event()
        self.serial_lock = threading.Lock()
        self.live_state_lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.sequence_running = False
        self.limit_monitoring = False
        self.limit_states: tuple[bool, bool, bool, bool] | None = None
        self.tool_crosshair_active = False
        self.tool_position: Point | None = None
        self.position_relative_mode = False
        self.pending_tool_target: Point | None = None
        self.motion_start: Point | None = None
        self.motion_target: Point | None = None
        self.motion_started_at = 0.0
        self.motion_duration = 0.0
        self.last_limit_poll = 0.0
        self._last_crosshair_visible = False

        self._vars()
        self._build()
        self.refresh_ports()
        self.after(100, self._drain_log)
        self.after(100, self._refresh_live_ui)

    def _vars(self) -> None:
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        self.baud_var = tk.StringVar(value="115200")
        self.dtr_var = tk.BooleanVar(value=False)
        self.rts_var = tk.BooleanVar(value=False)
        self.dry_run_var = tk.BooleanVar(value=True)
        self.laser_armed_var = tk.BooleanVar(value=False)

        self.area_w_var = tk.StringVar(value="380")
        self.area_h_var = tk.StringVar(value="310")
        self.margin_var = tk.StringVar(value="10")
        self.offset_x_var = tk.StringVar(value="0")
        self.offset_y_var = tk.StringVar(value="0")
        self.scale_pct_var = tk.StringVar(value="100")
        self.flip_y_var = tk.BooleanVar(value=True)
        self.mirror_x_var = tk.BooleanVar(value=False)
        self.mirror_y_var = tk.BooleanVar(value=False)

        self.feed_var = tk.StringVar(value="1200")
        self.rapid_var = tk.StringVar(value="2000")
        self.jog_var = tk.StringVar(value="10")
        self.overcut_var = tk.StringVar(value="3")
        self.tool_mode_var = tk.StringVar(value="Laser M4")
        self.tool_off_var = tk.StringVar(value="0")
        self.tool_on_var = tk.StringVar(value="255")
        self.status_var = tk.StringVar(value="Load an SVG to begin.")
        self.stats_var = tk.StringVar(value="")
        self.limit_status_var = tk.StringVar(value="Idle")

    def _build(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.grid(row=0, column=0, sticky="nsew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(0, weight=1)

        top = ttk.Frame(root)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        top.grid_columnconfigure(4, weight=1)

        ttk.Button(top, text="Load SVG", command=self.load_svg_file).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(top, text="Save G-code", command=self.save_gcode).grid(row=0, column=1, padx=(0, 14))
        ttk.Label(top, textvariable=self.status_var).grid(row=0, column=2, sticky="w")

        main = ttk.Frame(root)
        main.grid(row=1, column=0, sticky="nsew")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(main, bg="#f7f7f4", highlightthickness=1, highlightbackground="#b8b8b8")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _e: self.redraw_preview())

        side = ttk.Frame(root, width=330)
        side.grid(row=1, column=1, sticky="ns", padx=(10, 0))
        side.grid_propagate(False)

        self._connection_panel(side).pack(fill="x", pady=(0, 8))
        self._limit_panel(side).pack(fill="x", pady=(0, 8))
        self._layout_panel(side).pack(fill="x", pady=(0, 8))
        self._motion_panel(side).pack(fill="x", pady=(0, 8))
        self._plot_panel(side).pack(fill="x", pady=(0, 8))

        log_frame = ttk.LabelFrame(side, text="Serial / G-code Log")
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=10, wrap="word", font=("TkFixedFont", 9))
        self.log_text.pack(fill="both", expand=True)

        bottom = ttk.Frame(root)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bottom.grid_columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.stats_var).grid(row=0, column=0, sticky="w")

    def _connection_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text="Connection")
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Port").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        self.port_combo = ttk.Combobox(f, textvariable=self.port_var, values=[], width=34)
        self.port_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(0, 8), pady=(8, 2))
        ttk.Button(f, text="Refresh", command=self.refresh_ports).grid(row=1, column=0, padx=8, pady=2)
        ttk.Entry(f, textvariable=self.baud_var, width=10).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(f, text="baud").grid(row=1, column=2, sticky="w", padx=(4, 8), pady=2)
        ttk.Checkbutton(f, text="DTR", variable=self.dtr_var).grid(row=2, column=0, sticky="w", padx=8)
        ttk.Checkbutton(f, text="RTS", variable=self.rts_var).grid(row=2, column=1, sticky="w")
        self.connect_btn = ttk.Button(f, text="Connect", command=self.toggle_connection)
        self.connect_btn.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(6, 8))
        ttk.Label(f, text="Connecting may reset/home Arduino-style controllers.", foreground="#684500").grid(
            row=4, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8)
        )
        return f

    def _limit_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text="Limit Switches")
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        f.columnconfigure(2, weight=1)
        f.columnconfigure(3, weight=1)
        ttk.Label(f, textvariable=self.limit_status_var).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 4))
        self.limit_labels: list[tk.Label] = []
        for col, name in enumerate(("Y1", "Y2", "X1", "X2")):
            cell = ttk.Frame(f)
            cell.grid(row=1, column=col, sticky="ew", padx=(8 if col == 0 else 3, 8 if col == 3 else 3), pady=(0, 8))
            ttk.Label(cell, text=name, anchor="center").pack(fill="x")
            label = tk.Label(cell, text="Idle", bg="#d0d3d6", fg="#555555", padx=6, pady=3, relief="groove", width=7)
            label.pack(fill="x", pady=(2, 0))
            self.limit_labels.append(label)
        return f

    def _layout_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text="Layout")
        labels = [
            ("Width mm", self.area_w_var),
            ("Height mm", self.area_h_var),
            ("Margin mm", self.margin_var),
            ("Offset X", self.offset_x_var),
            ("Offset Y", self.offset_y_var),
            ("Scale %", self.scale_pct_var),
        ]
        for row, (label, var) in enumerate(labels):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=2)
            ent = ttk.Entry(f, textvariable=var, width=10)
            ent.grid(row=row, column=1, sticky="w", padx=(4, 8), pady=2)
            ent.bind("<Return>", lambda _e: self.recompute_preview())
        ttk.Checkbutton(f, text="Flip SVG Y", variable=self.flip_y_var, command=self.recompute_preview).grid(
            row=6, column=0, sticky="w", padx=8, pady=2
        )
        ttk.Checkbutton(f, text="Mirror X", variable=self.mirror_x_var, command=self.recompute_preview).grid(
            row=6, column=1, sticky="w", pady=2
        )
        ttk.Checkbutton(f, text="Mirror Y", variable=self.mirror_y_var, command=self.recompute_preview).grid(
            row=7, column=0, sticky="w", padx=8, pady=(2, 8)
        )
        ttk.Button(f, text="Apply Layout", command=self.recompute_preview).grid(row=7, column=1, sticky="ew", padx=(4, 8), pady=(2, 8))
        return f

    def _motion_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text="Manual Controls")
        for col in range(3):
            f.columnconfigure(col, weight=1)
        ttk.Label(f, text="Jog mm").grid(row=0, column=0, padx=8, pady=(8, 2), sticky="w")
        ttk.Entry(f, textvariable=self.jog_var, width=8).grid(row=0, column=1, pady=(8, 2), sticky="w")
        ttk.Label(f, text="Feed").grid(row=1, column=0, padx=8, pady=2, sticky="w")
        ttk.Entry(f, textvariable=self.feed_var, width=8).grid(row=1, column=1, pady=2, sticky="w")
        ttk.Label(f, text="Rapid").grid(row=2, column=0, padx=8, pady=2, sticky="w")
        ttk.Entry(f, textvariable=self.rapid_var, width=8).grid(row=2, column=1, pady=2, sticky="w")
        ttk.Label(f, text="Overcut").grid(row=3, column=0, padx=8, pady=2, sticky="w")
        ttk.Entry(f, textvariable=self.overcut_var, width=8).grid(row=3, column=1, pady=2, sticky="w")

        ttk.Button(f, text="Y+", command=lambda: self.jog(0, self._float(self.jog_var))).grid(row=4, column=1, sticky="ew", padx=3, pady=3)
        ttk.Button(f, text="X-", command=lambda: self.jog(-self._float(self.jog_var), 0)).grid(row=5, column=0, sticky="ew", padx=(8, 3), pady=3)
        ttk.Button(f, text="X+", command=lambda: self.jog(self._float(self.jog_var), 0)).grid(row=5, column=2, sticky="ew", padx=(3, 8), pady=3)
        ttk.Button(f, text="Y-", command=lambda: self.jog(0, -self._float(self.jog_var))).grid(row=6, column=1, sticky="ew", padx=3, pady=3)

        ttk.Button(f, text="Home G28", command=lambda: self.run_commands(["G28"], "home")).grid(row=7, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 2))
        ttk.Button(f, text="Set Zero G92", command=lambda: self.run_commands(["G92 X0 Y0"], "set zero")).grid(row=7, column=2, sticky="ew", padx=(3, 8), pady=(8, 2))

        ttk.Label(f, text="Tool mode").grid(row=8, column=0, padx=8, pady=(8, 2), sticky="w")
        ttk.Combobox(f, textvariable=self.tool_mode_var, values=["Laser M4", "Z servo", "M1 servo"], width=9, state="readonly").grid(
            row=8, column=1, columnspan=2, sticky="ew", padx=(0, 8), pady=(8, 2)
        )
        ttk.Checkbutton(f, text="Arm laser", variable=self.laser_armed_var).grid(row=9, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        ttk.Label(f, text="Off").grid(row=10, column=0, padx=8, pady=2, sticky="w")
        ttk.Entry(f, textvariable=self.tool_off_var, width=8).grid(row=10, column=1, pady=2, sticky="w")
        ttk.Button(f, text="Tool Off", command=lambda: self.tool(False)).grid(row=10, column=2, sticky="ew", padx=(3, 8), pady=2)
        ttk.Label(f, text="On").grid(row=11, column=0, padx=8, pady=2, sticky="w")
        ttk.Entry(f, textvariable=self.tool_on_var, width=8).grid(row=11, column=1, pady=2, sticky="w")
        ttk.Button(f, text="Tool On", command=lambda: self.tool(True)).grid(row=11, column=2, sticky="ew", padx=(3, 8), pady=(2, 8))
        return f

    def _plot_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text="Plot")
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        ttk.Checkbutton(f, text="Dry run", variable=self.dry_run_var).grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        ttk.Button(f, text="Plot SVG", command=self.plot_svg).grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 8))
        ttk.Button(f, text="Cancel", command=self.cancel_event.set).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(4, 8))
        return f

    def refresh_ports(self) -> None:
        ports = list_serial_ports()
        self.port_combo.configure(values=ports)
        if os.path.exists(DEFAULT_PORT):
            self.port_var.set(DEFAULT_PORT)
        elif ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def load_svg_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("SVG files", "*.svg"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.drawing = load_svg(path)
        except Exception as exc:
            messagebox.showerror("SVG load failed", str(exc))
            return
        self.svg_path = path
        self.status_var.set(os.path.basename(path))
        self.scale_pct_var.set("100")
        self.recompute_preview()

    def recompute_preview(self) -> None:
        if not self.drawing:
            self.machine_polys = []
            self.redraw_preview()
            return
        try:
            self.machine_polys = self._machine_polylines(self.drawing)
        except ValueError as exc:
            messagebox.showerror("Layout error", str(exc))
            return
        segments = sum(max(0, len(poly) - 1) for poly in self.machine_polys)
        distance = sum(_polyline_length(poly) for poly in self.machine_polys)
        bounds = _bounds(self.machine_polys)
        if bounds:
            min_x, min_y, max_x, max_y = bounds
            self.stats_var.set(
                f"{len(self.machine_polys)} paths, {segments} segments, {distance:.1f} mm pen-down distance; "
                f"bounds X {min_x:.1f}..{max_x:.1f}, Y {min_y:.1f}..{max_y:.1f}"
            )
        else:
            self.stats_var.set(f"{len(self.machine_polys)} paths, {segments} segments, {distance:.1f} mm pen-down distance")
        self.redraw_preview()

    def _machine_polylines(self, drawing: SvgDrawing) -> list[Polyline]:
        area_w = self._float(self.area_w_var)
        area_h = self._float(self.area_h_var)
        margin = self._float(self.margin_var)
        offset_x = self._float(self.offset_x_var)
        offset_y = self._float(self.offset_y_var)
        pct = self._float(self.scale_pct_var)
        if area_w <= 0 or area_h <= 0:
            raise ValueError("Machine area must be positive")
        x0, y0, x1, y1 = drawing.bbox
        src_w = max(1e-9, x1 - x0)
        src_h = max(1e-9, y1 - y0)
        fit_w = max(1e-9, area_w - 2 * margin)
        fit_h = max(1e-9, area_h - 2 * margin)
        scale = min(fit_w / src_w, fit_h / src_h) * pct / 100.0
        out: list[Polyline] = []
        for poly in drawing.polylines:
            new: Polyline = []
            for sx, sy in poly:
                x = (sx - x0) * scale + margin + offset_x
                if self.flip_y_var.get():
                    y = (src_h - (sy - y0)) * scale + margin + offset_y
                else:
                    y = (sy - y0) * scale + margin + offset_y
                if self.mirror_x_var.get():
                    x = area_w - x
                if self.mirror_y_var.get():
                    y = area_h - y
                new.append((x, y))
            out.append(new)
        return out

    def redraw_preview(self) -> None:
        self.canvas.delete("all")
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        area_w = max(1.0, self._float(self.area_w_var, 310))
        area_h = max(1.0, self._float(self.area_h_var, 390))
        pad = 28
        scale = min((w - 2 * pad) / area_w, (h - 2 * pad) / area_h)
        scale = max(0.1, scale)
        ox = (w - area_w * scale) / 2
        oy = (h - area_h * scale) / 2

        def cxy(p: Point) -> tuple[float, float]:
            x, y = p
            return ox + x * scale, oy + (area_h - y) * scale

        x0, y0 = cxy((0, 0))
        x1, y1 = cxy((area_w, area_h))
        self.canvas.create_rectangle(x0, y1, x1, y0, outline="#333333", width=2)
        self.canvas.create_text(x0 + 8, y0 - 12, text="0,0", anchor="w", fill="#444")
        self.canvas.create_line(x0, y0, x0 + 40, y0, fill="#bf3030", arrow=tk.LAST)
        self.canvas.create_line(x0, y0, x0, y0 - 40, fill="#2870b8", arrow=tk.LAST)
        self.canvas.create_text(x0 + 44, y0, text="X", anchor="w", fill="#bf3030")
        self.canvas.create_text(x0, y0 - 44, text="Y", anchor="s", fill="#2870b8")

        for poly in self.machine_polys:
            if len(poly) < 2:
                continue
            out_of_bounds = any(px < 0 or px > area_w or py < 0 or py > area_h for px, py in poly)
            coords: list[float] = []
            for p in poly:
                coords.extend(cxy(p))
            self.canvas.create_line(*coords, fill="#c93628" if out_of_bounds else "#0f6f8f", width=1.2)

        tool_pos = self._display_tool_position()
        if tool_pos is not None:
            tx, ty = cxy(tool_pos)
            self.canvas.create_line(tx, y1, tx, y0, fill="#d18b00", width=1.4, dash=(5, 3))
            self.canvas.create_line(x0, ty, x1, ty, fill="#d18b00", width=1.4, dash=(5, 3))
            self.canvas.create_oval(tx - 5, ty - 5, tx + 5, ty + 5, outline="#7a2f00", fill="#ffd166", width=2)
            self.canvas.create_text(
                tx + 8,
                ty - 8,
                text=f"X{tool_pos[0]:.1f} Y{tool_pos[1]:.1f}",
                anchor="sw",
                fill="#7a2f00",
                font=("TkDefaultFont", 9, "bold"),
            )

    def toggle_connection(self) -> None:
        if self.serial.is_open:
            self.serial.close()
            self.ready_event.clear()
            self.connect_btn.configure(text="Connect")
            self._log("Disconnected.")
            return
        try:
            self.serial.open(
                self.port_var.get(),
                int(self.baud_var.get()),
                dtr=self.dtr_var.get(),
                rts=self.rts_var.get(),
            )
        except (OSError, SerialError, ValueError) as exc:
            messagebox.showerror("Connection failed", str(exc))
            return
        self.connect_btn.configure(text="Disconnect")
        self._log(f"Connected to {self.port_var.get()} at {self.baud_var.get()} baud.")
        self._read_startup()

    def _read_startup(self) -> None:
        def worker() -> None:
            deadline = time.monotonic() + 90.0
            assume_ready_deadline = time.monotonic() + 3.0
            saw_startup = False
            while time.monotonic() < deadline and self.serial.is_open:
                for line in self.serial.read_available_lines(timeout=0.1):
                    saw_startup = True
                    self._thread_log(f"< {line}")
                    if line.lower() == "ok":
                        self.ready_event.set()
                        self._thread_log("Controller ready.")
                        return
                if not saw_startup and time.monotonic() > assume_ready_deadline:
                    self.ready_event.set()
                    self._thread_log("No startup text; assuming controller is already ready.")
                    return
            if self.serial.is_open and not self.ready_event.is_set():
                self._thread_log("Controller did not report ready before timeout.")
        threading.Thread(target=worker, daemon=True).start()

    def tool(self, on: bool) -> None:
        if on and not self._laser_is_armed():
            return
        self.run_commands([self._tool_command(on)], "tool on" if on else "tool off")

    def jog(self, dx: float, dy: float) -> None:
        cmds = ["G91", f"G1 X{dx:.3f} Y{dy:.3f} F{self._float(self.feed_var):.0f}", "G90"]
        self.run_commands(cmds, f"jog X{dx:.3f} Y{dy:.3f}")

    def run_commands(self, commands: list[str], label: str) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A command sequence is already running.")
            return
        dry_run = self.dry_run_var.get()
        tool_mode = self.tool_mode_var.get()
        rapid_fallback = self._float(self.rapid_var, 1200.0)
        if not dry_run and self._contains_laser_on(commands) and not self._laser_is_armed():
            return
        if not dry_run and not self.serial.is_open:
            messagebox.showerror("Not connected", "Connect to the plotter, or enable Dry run.")
            return
        self.cancel_event.clear()
        self.worker = threading.Thread(
            target=self._command_worker,
            args=(commands, label, dry_run, tool_mode, rapid_fallback),
            daemon=True,
        )
        self.worker.start()

    def _command_worker(self, commands: list[str], label: str, dry_run: bool, tool_mode: str, rapid_fallback: float) -> None:
        self._begin_sequence_state(dry_run)
        tool_active = False
        try:
            self._thread_log(f"-- {label} --")
            if not dry_run and not self._wait_until_ready():
                self._thread_log("Stopped: controller is not ready.")
                return
            if not dry_run:
                self._send_startup_tool_off(tool_mode)
                self._poll_live_status(dry_run, tool_mode, force=True, poll_position=True, tool_active=False)
            for cmd in commands:
                if self.cancel_event.is_set():
                    self._send_tool_off_after_abort(dry_run, tool_mode)
                    self._thread_log("Canceled.")
                    return
                if not dry_run:
                    self._poll_live_status(dry_run, tool_mode, tool_active=tool_active)
                self._prepare_preview_for_command(cmd, rapid_fallback)
                if dry_run:
                    self._thread_log(f"> {cmd}")
                    self._commit_preview_for_command(True)
                    tool_active = self._tool_active_after_command(cmd, tool_active, tool_mode)
                    continue
                with self.serial_lock:
                    self._thread_log(f"> {cmd}")
                    reply = self.serial.command(cmd, timeout=45.0)
                for line in reply.lines:
                    self._thread_log(f"< {line}")
                self._update_live_state_from_lines(reply.lines)
                limit_hit = self._reply_has_limit(reply.lines)
                self._commit_preview_for_command(reply.ok and not limit_hit)
                tool_active = self._tool_active_after_command(cmd, tool_active, tool_mode)
                if limit_hit:
                    self._send_tool_off_after_abort(dry_run, tool_mode)
                    self._poll_live_status(dry_run, tool_mode, force=True, poll_position=True, tool_active=False)
                    self._thread_log("Stopped: limit switch closed.")
                    return
                if not reply.ok:
                    detail = "timeout" if reply.timed_out else "controller rejected command"
                    self._send_tool_off_after_abort(dry_run, tool_mode)
                    self._thread_log(f"Stopped: {detail}.")
                    return
            if not dry_run:
                self._poll_live_status(dry_run, tool_mode, force=True, poll_position=True, tool_active=False)
            self._thread_log("Done.")
        except Exception:
            self._thread_log("Internal worker error:")
            for line in traceback.format_exc().rstrip().splitlines():
                self._thread_log(line)
        finally:
            self._end_sequence_state()

    def _begin_sequence_state(self, dry_run: bool) -> None:
        with self.live_state_lock:
            self.sequence_running = True
            self.limit_monitoring = (not dry_run) and self.serial.is_open
            self.limit_states = None
            self.tool_crosshair_active = True
            self.pending_tool_target = None
            self.motion_start = None
            self.motion_target = None
            self.motion_started_at = 0.0
            self.motion_duration = 0.0
            self.last_limit_poll = 0.0

    def _end_sequence_state(self) -> None:
        with self.live_state_lock:
            self.sequence_running = False
            self.limit_monitoring = False
            self.limit_states = None
            self.tool_crosshair_active = False
            self.pending_tool_target = None
            self.motion_start = None
            self.motion_target = None
            self.motion_started_at = 0.0
            self.motion_duration = 0.0

    def _refresh_live_ui(self) -> None:
        with self.live_state_lock:
            monitoring = self.limit_monitoring
            states = self.limit_states
            crosshair_visible = self.tool_crosshair_active and (
                self.tool_position is not None or self.motion_start is not None or self.motion_target is not None
            )
        if not monitoring:
            self.limit_status_var.set("Idle")
            for label in getattr(self, "limit_labels", []):
                label.configure(text="Idle", bg="#d0d3d6", fg="#555555")
        elif states is None:
            self.limit_status_var.set("Running")
            for label in self.limit_labels:
                label.configure(text="...", bg="#eceff1", fg="#555555")
        else:
            self.limit_status_var.set("Running")
            for label, closed in zip(self.limit_labels, states):
                if closed:
                    label.configure(text="Closed", bg="#c9352b", fg="#ffffff")
                else:
                    label.configure(text="Open", bg="#2d8a45", fg="#ffffff")

        if crosshair_visible or self._last_crosshair_visible:
            self.redraw_preview()
        self._last_crosshair_visible = crosshair_visible
        self.after(100, self._refresh_live_ui)

    def _display_tool_position(self) -> Point | None:
        now = time.monotonic()
        with self.live_state_lock:
            if not self.tool_crosshair_active:
                return None
            if self.motion_start is not None and self.motion_target is not None:
                if self.motion_duration <= 0:
                    return self.motion_target
                t = (now - self.motion_started_at) / self.motion_duration
                t = max(0.0, min(1.0, t))
                sx, sy = self.motion_start
                tx, ty = self.motion_target
                return sx + (tx - sx) * t, sy + (ty - sy) * t
            return self.tool_position

    def _poll_live_status(
        self,
        dry_run: bool,
        tool_mode: str,
        force: bool = False,
        poll_position: bool = False,
        tool_active: bool = False,
    ) -> None:
        if dry_run or not self.serial.is_open:
            return
        if tool_active and tool_mode == "Laser M4":
            return
        now = time.monotonic()
        with self.live_state_lock:
            if not self.sequence_running:
                return
            if not force and now - self.last_limit_poll < 0.75:
                return
            self.last_limit_poll = now
        try:
            with self.serial_lock:
                reply = self.serial.command("M11", timeout=4.0)
            self._update_live_state_from_lines(reply.lines)
        except Exception as exc:
            self._thread_log(f"Limit poll warning: {exc}")
            return
        if not poll_position:
            return
        try:
            with self.serial_lock:
                reply = self.serial.command("P", timeout=4.0)
            self._update_live_state_from_lines(reply.lines)
        except Exception as exc:
            self._thread_log(f"Position poll warning: {exc}")

    def _update_live_state_from_lines(self, lines: list[str]) -> None:
        for line in lines:
            states = self._parse_limit_states(line)
            if states is not None:
                with self.live_state_lock:
                    self.limit_states = states
            pos = self._parse_position_line(line)
            if pos is not None:
                with self.live_state_lock:
                    self.tool_position = pos
                    self.pending_tool_target = None
                    self.motion_start = None
                    self.motion_target = None

    def _parse_limit_states(self, line: str) -> tuple[bool, bool, bool, bool] | None:
        parts = line.strip().split()
        if len(parts) >= 5 and parts[0].upper() == "M11":
            values = parts[1:5]
        elif len(parts) >= 6 and parts[0].upper() == "LIMIT":
            values = parts[-4:]
        else:
            return None
        try:
            return tuple(int(value) == 0 for value in values)  # type: ignore[return-value]
        except ValueError:
            return None

    def _parse_position_line(self, line: str) -> Point | None:
        if not line.strip().upper().startswith("POS"):
            return None
        values = self._command_words(line)
        if "X" not in values or "Y" not in values:
            return None
        return values["X"], values["Y"]

    def _reply_has_limit(self, lines: list[str]) -> bool:
        return any(line.strip().upper().startswith("LIMIT") for line in lines)

    def _prepare_preview_for_command(self, cmd: str, rapid_fallback: float) -> None:
        code, number, words = self._command_code(cmd)
        if code == "G" and number == 90:
            with self.live_state_lock:
                self.position_relative_mode = False
                self.pending_tool_target = None
                self.motion_start = None
                self.motion_target = None
            return
        if code == "G" and number == 91:
            with self.live_state_lock:
                self.position_relative_mode = True
                self.pending_tool_target = None
                self.motion_start = None
                self.motion_target = None
            return
        if code == "G" and number == 92:
            x = words.get("X", 0.0)
            y = words.get("Y", 0.0)
            with self.live_state_lock:
                self.tool_position = (x, y)
                self.pending_tool_target = None
                self.motion_start = None
                self.motion_target = None
            return
        if code == "G" and number == 28:
            self._begin_preview_motion((0.0, 0.0), cmd, rapid_fallback)
            return
        if code == "G" and number in (0, 1):
            if "X" not in words and "Y" not in words:
                return
            with self.live_state_lock:
                start = self.tool_position or (0.0, 0.0)
                relative = self.position_relative_mode
            if relative:
                target = (start[0] + words.get("X", 0.0), start[1] + words.get("Y", 0.0))
            else:
                target = (words.get("X", start[0]), words.get("Y", start[1]))
            self._begin_preview_motion(target, cmd, rapid_fallback)

    def _begin_preview_motion(self, target: Point, cmd: str, rapid_fallback: float) -> None:
        with self.live_state_lock:
            start = self.tool_position or (0.0, 0.0)
            self.pending_tool_target = target
            self.motion_start = start
            self.motion_target = target
            self.motion_started_at = time.monotonic()
            self.motion_duration = self._estimate_motion_duration(start, target, cmd, rapid_fallback)

    def _commit_preview_for_command(self, ok: bool) -> None:
        with self.live_state_lock:
            if ok and self.pending_tool_target is not None:
                self.tool_position = self.pending_tool_target
            self.pending_tool_target = None
            self.motion_start = None
            self.motion_target = None
            self.motion_started_at = 0.0
            self.motion_duration = 0.0

    def _estimate_motion_duration(self, start: Point, target: Point, cmd: str, rapid_fallback: float) -> float:
        distance = _dist(start, target)
        if distance <= 0.001:
            return 0.05
        words = self._command_words(cmd)
        feed = words.get("F", rapid_fallback)
        feed = max(1.0, feed)
        return max(0.08, min(45.0, distance / (feed / 60.0)))

    def _tool_active_after_command(self, cmd: str, current: bool, tool_mode: str) -> bool:
        if tool_mode != "Laser M4":
            return False
        parts = cmd.strip().split()
        if not parts or parts[0].upper() != "M4":
            return current
        return self._is_laser_on_command(cmd)

    def _command_code(self, cmd: str) -> tuple[str | None, int | None, dict[str, float]]:
        parts = cmd.strip().split()
        if not parts:
            return None, None, {}
        first = parts[0].upper()
        if len(first) < 2 or first[0] not in ("G", "M"):
            return None, None, self._command_words(cmd)
        try:
            number = int(float(first[1:]))
        except ValueError:
            number = None
        return first[0], number, self._command_words(cmd)

    def _command_words(self, cmd: str) -> dict[str, float]:
        words: dict[str, float] = {}
        for part in cmd.strip().split():
            if len(part) < 2:
                continue
            key = part[0].upper()
            if key not in "XYZFA":
                continue
            try:
                words[key] = float(part[1:])
            except ValueError:
                continue
        return words

    def _wait_until_ready(self) -> bool:
        if self.ready_event.is_set():
            return True
        self._thread_log("Waiting for controller ready/homing completion...")
        end = time.monotonic() + 95.0
        while time.monotonic() < end:
            if self.cancel_event.is_set():
                return False
            if self.ready_event.wait(timeout=0.2):
                return True
        return False

    def plot_svg(self) -> None:
        if not self.machine_polys:
            messagebox.showinfo("No SVG", "Load an SVG first.")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A command sequence is already running.")
            return
        commands = self._gcode_for_plot()
        if not commands:
            messagebox.showinfo("No paths", "The SVG did not produce plottable paths.")
            return
        bounds_error = self._bounds_error()
        if bounds_error:
            messagebox.showerror("Bounds check failed", bounds_error)
            return
        dry_run = self.dry_run_var.get()
        tool_mode = self.tool_mode_var.get()
        rapid_fallback = self._float(self.rapid_var, 1200.0)
        if not dry_run and not self.serial.is_open:
            messagebox.showerror("Not connected", "Connect to the plotter, or enable Dry run.")
            return
        if not dry_run and not self._laser_is_armed():
            return
        self.cancel_event.clear()
        self.worker = threading.Thread(
            target=self._command_worker,
            args=(commands, "plot SVG", dry_run, tool_mode, rapid_fallback),
            daemon=True,
        )
        self.worker.start()

    def _gcode_for_plot(self) -> list[str]:
        rapid = self._float(self.rapid_var)
        feed = self._float(self.feed_var)
        overcut = max(0.0, self._float(self.overcut_var))
        cmds = ["G21", "G90", self._tool_command(False)]
        last: Point | None = None
        for poly in self.machine_polys:
            cleaned = _simplify(poly)
            if len(cleaned) < 2:
                continue
            start = cleaned[0]
            if last is None or _dist(last, start) > 0.01:
                cmds.append(f"G1 X{start[0]:.3f} Y{start[1]:.3f} F{rapid:.0f}")
            cmds.append(self._tool_command(True))
            for x, y in cleaned[1:]:
                cmds.append(f"G1 X{x:.3f} Y{y:.3f} F{feed:.0f}")
            for x, y in _overcut_points(cleaned, overcut):
                cmds.append(f"G1 X{x:.3f} Y{y:.3f} F{feed:.0f}")
            cmds.append(self._tool_command(False))
            last = cleaned[-1]
        cmds.extend([self._tool_command(False), "G28", self._tool_command(False)])
        return cmds

    def _bounds_error(self) -> str | None:
        bounds = _bounds(self.machine_polys)
        if not bounds:
            return None
        area_w = self._float(self.area_w_var)
        area_h = self._float(self.area_h_var)
        min_x, min_y, max_x, max_y = bounds
        eps = 0.001
        if min_x >= -eps and min_y >= -eps and max_x <= area_w + eps and max_y <= area_h + eps:
            return None
        return (
            f"Toolpath exceeds the configured machine area. "
            f"Path bounds are X {min_x:.1f}..{max_x:.1f} mm, Y {min_y:.1f}..{max_y:.1f} mm; "
            f"machine area is X 0..{area_w:.1f} mm, Y 0..{area_h:.1f} mm."
        )

    def _tool_command(self, on: bool) -> str:
        value = self._float(self.tool_on_var if on else self.tool_off_var)
        mode = self.tool_mode_var.get()
        if mode == "Laser M4":
            return f"M4 {value:.0f}"
        if mode == "M1 servo":
            return f"M1 {value:.0f}"
        return f"G1 Z{value:.0f} F{self._float(self.rapid_var):.0f}"

    def _contains_laser_on(self, commands: list[str]) -> bool:
        if self.tool_mode_var.get() != "Laser M4":
            return False
        return any(self._is_laser_on_command(cmd) for cmd in commands)

    def _is_laser_on_command(self, cmd: str) -> bool:
        parts = cmd.strip().split()
        if len(parts) < 2 or parts[0].upper() != "M4":
            return False
        try:
            return float(parts[1]) > 0
        except ValueError:
            return False

    def _laser_is_armed(self) -> bool:
        if self.tool_mode_var.get() != "Laser M4" or self.dry_run_var.get():
            return True
        if self.laser_armed_var.get():
            return True
        messagebox.showerror("Laser not armed", "Enable Arm laser before sending laser-on commands.")
        return False

    def _send_startup_tool_off(self, tool_mode: str) -> None:
        if not self.serial.is_open or tool_mode != "Laser M4":
            return
        try:
            with self.serial_lock:
                self._thread_log("> M4 0")
                reply = self.serial.command("M4 0", timeout=8.0)
            for line in reply.lines:
                self._thread_log(f"< {line}")
        except Exception as exc:
            self._thread_log(f"Laser-off warning: {exc}")

    def _send_tool_off_after_abort(self, dry_run: bool, tool_mode: str) -> None:
        if dry_run or tool_mode != "Laser M4" or not self.serial.is_open:
            return
        try:
            with self.serial_lock:
                self.serial.command("M4 0", timeout=8.0)
        except Exception:
            pass

    def save_gcode(self) -> None:
        if not self.machine_polys:
            messagebox.showinfo("No SVG", "Load an SVG first.")
            return
        bounds_error = self._bounds_error()
        if bounds_error:
            messagebox.showerror("Bounds check failed", bounds_error)
            return
        path = filedialog.asksaveasfilename(defaultextension=".gcode", filetypes=[("G-code", "*.gcode *.nc *.cnc"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self._gcode_for_plot()))
            f.write("\n")
        self._log(f"Saved {path}")

    def _float(self, var: tk.StringVar, default: float = 0.0) -> float:
        try:
            return float(var.get())
        except ValueError:
            return default

    def _thread_log(self, text: str) -> None:
        self.log_queue.put(text)

    def _log(self, text: str) -> None:
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def _drain_log(self) -> None:
        try:
            while True:
                self._log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

    def destroy(self) -> None:
        self.cancel_event.set()
        if self.serial.is_open:
            if not self.dry_run_var.get() and self.tool_mode_var.get() == "Laser M4":
                try:
                    self.serial.command("M4 0", timeout=2.0)
                except Exception:
                    pass
            self.serial.close()
        super().destroy()


def _polyline_length(poly: Polyline) -> float:
    return sum(_dist(a, b) for a, b in zip(poly, poly[1:]))


def _bounds(polys: list[Polyline]) -> tuple[float, float, float, float] | None:
    points = [p for poly in polys for p in poly]
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _overcut_points(poly: Polyline, distance: float) -> Polyline:
    if distance <= 0 or len(poly) < 3 or _dist(poly[0], poly[-1]) > 0.05:
        return []
    out: Polyline = []
    remaining = distance
    current = poly[0]
    for nxt in poly[1:]:
        seg = _dist(current, nxt)
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


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _simplify(poly: Polyline, min_step: float = 0.05) -> Polyline:
    out: Polyline = []
    for p in poly:
        if not out or _dist(out[-1], p) >= min_step:
            out.append(p)
    if len(out) == 1 and len(poly) > 1:
        out.append(poly[-1])
    elif out and poly[-1] != out[-1]:
        out.append(poly[-1])
    return out


if __name__ == "__main__":
    app = PlotterApp()
    app.mainloop()
