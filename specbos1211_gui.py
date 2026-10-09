"""Optional Tk GUI for the specbos 1211 spectroradiometer.

Run: python -m specbos1211_gui   (pick "Demo" as device to try it without hardware)

Click Measure for a single-shot spectral radiance. Luminance, radiance and the
CIE 1976 colour are computed from the measured spectrum with colour-science.
The measurement runs in a worker thread, so long (auto) integration times do
not freeze the window. The File writer tab appends each measurement to a CSV
file.
"""

from __future__ import annotations

import csv
import threading
import tkinter as tk
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime
from functools import partial
from pathlib import Path
from tkinter import filedialog, ttk

import colour
import numpy as np
from matplotlib.axes import Axes
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from specbos1211 import JetiError, Specbos1211, find_serial_ports

BG: str = "#1d1f24"
PANEL: str = "#272a31"
EDGE: str = "#3a3e47"
FG: str = "#e8e8e8"
MUTED: str = "#8b909a"
ACCENT: str = "#4fa3ff"
FIG_BG: str = "#22252b"
DEMO: str = "Demo (no hardware)"
AUTO: str = "Auto (USB serial)"


def _it_label(ms: float) -> str:
    return f"{ms:g} ms" if ms < 1000 else f"{ms / 1000:g} s"


IT_AUTO: str = "Auto"
IT_MS: dict[str, float] = {
    _it_label(ms): ms
    for ms in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000)
}
IT_LABELS: list[str] = [IT_AUTO, *IT_MS]
PLACEHOLDER: str = "-"
CSV_META: list[str] = [
    "timestamp",
    "integration_time_ms",
    "luminance_cd_m2",
    "radiance_W_sr_m2",
    "Y",
    "u_prime",
    "v_prime",
]
CONNECT_TIMEOUT_S: float = 2.0  # *IDN? / *VERS? answer at once
BAUDS: tuple[int, ...] = (921600, 115200, 38400)  # factory default first
# The BELL only arrives after the whole integration, and auto exposure may run
# several trial integrations up to max_tint (60 s) first, so a per-read timeout
# during a measurement must cover all of that (README, "Timeout during
# measurement").
AUTO_TIMEOUT_S: float = 300.0
POLL_MS: int = 100

Yuv = tuple[float, float, float]
Spectrum = tuple[np.ndarray, np.ndarray]  # wavelengths (nm), radiance


def analyse(wavelengths: np.ndarray, spectrum: np.ndarray) -> tuple[float, float, Yuv]:
    """Luminance (cd/m2), radiance (W sr-1 m-2) and CIE 1976 (Y, u', v') from
    one spectral radiance.

    colour-science sd_to_XYZ with k=683 lm/W (CIE 1931 2 deg CMFs) returns
    absolute XYZ, so Y of a radiance is the luminance in cd/m2.
    """
    # sd_to_XYZ (ASTM E308) needs a regular 1 nm-compatible interval; configure()
    # step can be up to 10 nm, so resample linearly onto a 1 nm grid first.
    grid: np.ndarray = np.arange(np.ceil(wavelengths[0]), np.floor(wavelengths[-1]) + 1)
    sd: colour.SpectralDistribution = colour.SpectralDistribution(
        np.interp(grid, wavelengths, spectrum), grid
    )
    xyz: np.ndarray = colour.sd_to_XYZ(sd, k=683)  # absolute XYZ
    luminance: float = float(xyz[1])
    radiance: float = float(np.trapezoid(spectrum, wavelengths))
    u, v = (float(c) for c in colour.xy_to_Luv_uv(colour.XYZ_to_xy(xyz)))
    return luminance, radiance, (luminance, u, v)


def append_csv(
    path: Path,
    wavelengths: np.ndarray,
    spectrum: np.ndarray,
    tint_ms: float,
    luminance: float,
    radiance: float,
    yuv: Yuv,
) -> None:
    """Append one spectrum as one row: CSV_META columns, then one column per
    wavelength (header = wavelength in nm). tint_ms 0 is written as "auto".
    A new or empty file gets the header first; an existing file with a
    different header (other wavelength grid) raises ValueError instead of
    mixing incompatible rows."""
    header: list[str] = CSV_META + [f"{wl:.2f}" for wl in wavelengths]
    new: bool = not path.exists() or path.stat().st_size == 0
    if not new:
        with path.open(newline="") as f:
            if next(csv.reader(f), None) != header:
                raise ValueError(
                    f"{path.name} has a different header (other wavelength "
                    "grid); choose another file"
                )
    with path.open("a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(header)
        writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                f"{tint_ms:g}" if tint_ms else "auto",
                f"{luminance:.7g}",
                f"{radiance:.7g}",
                *(f"{c:.7g}" for c in yuv),
            ]
            + [f"{v:.7g}" for v in spectrum]
        )


class DemoSpecbos:
    """Stand-in with the subset of the Specbos1211 API the GUI uses.

    A warm-white LED: blue pump at 448 nm plus a phosphor bump at 596 nm,
    deterministic, in honest W sr^-1 m^-2 nm^-1 (~100 cd/m2, ~3000 K).
    """

    def __init__(self) -> None:
        self.timeout: float = CONNECT_TIMEOUT_S
        self.wavelengths: np.ndarray | None = None

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def firmware_version(self) -> str:
        return "DEMO"

    def configure(
        self, wbeg: int = 380, wend: int = 780, step: int = 1, tint: float = 0.0
    ) -> None:
        self.wavelengths = np.arange(wbeg, wend + step, step, dtype=float)

    def measure(self, quantity: str = "sprad") -> np.ndarray:
        wl: np.ndarray = np.asarray(self.wavelengths)
        blue: np.ndarray = 6.15e-4 * np.exp(-0.5 * ((wl - 448.0) / 15.0) ** 2)
        phosphor: np.ndarray = 1.9e-3 * np.exp(-0.5 * ((wl - 596.0) / 58.0) ** 2)
        return blue + phosphor


Device = Specbos1211 | DemoSpecbos


def _measure(fut: Future[Spectrum], dev: Device, tint_ms: float) -> None:
    """Worker thread: configure and measure; the result or error goes to fut."""
    try:
        dev.timeout = AUTO_TIMEOUT_S if tint_ms == 0 else tint_ms / 1e3 + 30.0
        dev.configure(tint=tint_ms)  # 380-780 nm, 1 nm, no averaging
        spectrum: np.ndarray = dev.measure("sprad")
        wl: np.ndarray | None = dev.wavelengths
        if wl is None or wl.shape != spectrum.shape:
            raise JetiError(
                f"got {spectrum.size} values for a {0 if wl is None else wl.size}"
                "-point wavelength grid"
            )
        fut.set_result((wl, spectrum))
    except BaseException as e:  # handed to the Tk thread via fut
        fut.set_exception(e)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root: tk.Tk = root
        self.dev: Device | None = None
        self.job: Future[Spectrum] | None = None  # running measurement
        self.status: tk.StringVar = tk.StringVar(value="Not connected")

        root.title("specbos 1211 Spectrum")
        root.configure(bg=BG)
        self._style()

        top = ttk.Frame(root, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text="Device").pack(side="left")
        self.ports: dict[str, str | None] = {}  # label -> serial port (None: auto)
        self.dev_var: tk.StringVar = tk.StringVar()
        self.dev_box = ttk.Combobox(top, textvariable=self.dev_var, width=34)
        self.dev_box.pack(side="left", padx=6)
        ttk.Button(top, text="Rescan", command=self.scan_devices).pack(side="left")
        self.conn_btn = ttk.Button(top, text="Connect", command=self.toggle_connection)
        self.conn_btn.pack(side="left", padx=6)
        ttk.Label(
            top, text="LAN: type the IP (host[:port]) into the box", foreground=MUTED
        ).pack(side="left", padx=(6, 0))
        self.scan_devices()

        tabs = ttk.Notebook(root, padding=(12, 0))
        tabs.pack(fill="both", expand=True)
        body = ttk.Frame(tabs, padding=(10, 10))
        tabs.add(body, text="Measure")
        writer_tab = ttk.Frame(tabs, padding=(12, 12))
        tabs.add(writer_tab, text="File writer")

        self.fig: Figure = Figure(figsize=(6.4, 4.6), dpi=100, facecolor=FIG_BG)
        self.fig.subplots_adjust(left=0.11, right=0.97, top=0.95, bottom=0.11)
        self.ax: Axes = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=body)
        self.canvas.get_tk_widget().pack(side="left", fill="both", expand=True)

        panel = ttk.Frame(body, padding=(12, 0, 0, 0))
        panel.pack(side="left", fill="y")
        ttk.Label(panel, text="Integration time", foreground=MUTED).pack(anchor="w")
        self.it_var: tk.StringVar = tk.StringVar(value=IT_LABELS[0])
        ttk.Combobox(
            panel,
            textvariable=self.it_var,
            width=12,
            state="readonly",
            values=IT_LABELS,
        ).pack(fill="x", pady=(2, 6))
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x")
        self.measure_btn = ttk.Button(
            buttons, text="Measure", style="Go.TButton", command=self.measure
        )
        self.measure_btn.pack(side="left", fill="x", expand=True)
        ttk.Button(buttons, text="Clear", command=self.clear).pack(
            side="left", fill="y", padx=(6, 0)
        )
        ttk.Label(
            panel,
            text="auto integration time can take up to minutes on dim targets",
            foreground=MUTED,
            wraplength=216,
            justify="left",
        ).pack(fill="x", pady=(4, 10))

        self.luminance_var: tk.StringVar = self._card(
            panel, "Luminance (cd/m^2)", "Value.TLabel"
        )[1]
        self.radiance_var: tk.StringVar = self._card(
            panel, "Radiance (W sr^-1 m^-2)", "Value.TLabel"
        )[1]
        colour_box: ttk.Frame
        colour_box, self.colour_var = self._card(
            panel, "Colour (CIE 1976 u'v')", "Wide.TLabel"
        )
        self._build_diagram(colour_box)

        self._build_writer_tab(writer_tab)

        ttk.Label(
            root, textvariable=self.status, foreground=MUTED, padding=(12, 8)
        ).pack(fill="x")

        root.protocol("WM_DELETE_WINDOW", self.close)
        self._draw_empty()
        root.update_idletasks()  # floor = natural size: shrinking never clips
        root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())

    def _card(
        self,
        parent: ttk.Frame,
        title: str,
        style: str,
    ) -> tuple[ttk.Frame, tk.StringVar]:
        """Titled value card; returns the card and the variable holding its
        value."""
        box = ttk.Frame(parent, style="Card.TFrame", padding=(10, 6))
        box.pack(fill="x", pady=(0, 8))
        ttk.Label(box, text=title, style="CardMuted.TLabel").pack(anchor="w")
        var = tk.StringVar(value=PLACEHOLDER)
        ttk.Label(
            box, textvariable=var, style=style, wraplength=190, justify="left"
        ).pack(anchor="w")
        return box, var

    def _build_diagram(self, parent: ttk.Frame) -> None:
        """CIE 1976 u'v' diagram with the measured colour marked by an x."""
        fig: Figure = Figure(figsize=(2.3, 2.3), dpi=100, facecolor=PANEL)
        fig.subplots_adjust(left=0.18, right=0.97, top=0.97, bottom=0.14)
        ax: Axes = fig.add_subplot(111)
        colour.plotting.plot_chromaticity_diagram_CIE1976UCS(axes=ax, show=False)
        ax.set_title("")
        for artist in [*ax.texts, *ax.lines, *ax.collections]:  # keep colour fill
            artist.remove()
        uv: np.ndarray = colour.xy_to_Luv_uv(
            colour.XYZ_to_xy(
                colour.MSDS_CMFS["CIE 1931 2 Degree Standard Observer"].values
            )
        )  # spectral locus, 360-830 nm
        ax.plot(uv[:, 0], uv[:, 1], color=FG, linewidth=1.5)
        ax.plot(
            [uv[-1, 0], uv[0, 0]], [uv[-1, 1], uv[0, 1]], color=FG, linewidth=1.5
        )  # closing purple line
        ax.set_facecolor(PANEL)
        ax.set_xlim(0.0, 0.65)
        ax.set_ylim(0.0, 0.62)
        ax.set_xlabel("u'", color=FG, fontsize=8, labelpad=1)
        ax.set_ylabel("v'", color=FG, fontsize=8, labelpad=4, rotation=0)
        ax.tick_params(colors=MUTED, labelsize=7)
        for spine in ax.spines.values():
            spine.set_color(EDGE)
        self._marker: Line2D = ax.plot(
            [], [], "x", color="black", markersize=9, markeredgewidth=2.2
        )[0]
        self.diagram = FigureCanvasTkAgg(fig, master=parent)
        self.diagram.get_tk_widget().pack(anchor="w", pady=(6, 0))
        self.diagram.draw()

    def _build_writer_tab(self, tab: ttk.Frame) -> None:
        self.csv_path: tk.StringVar = tk.StringVar()
        self.csv_on: tk.BooleanVar = tk.BooleanVar(value=False)
        self.csv_rows: int = 0
        self.csv_info: tk.StringVar = tk.StringVar(value="Writer off")

        ttk.Label(tab, text="CSV file", foreground=MUTED).pack(anchor="w")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(2, 10))
        ttk.Entry(row, textvariable=self.csv_path, width=70).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(row, text="Browse...", command=self._browse_csv).pack(
            side="left", padx=(6, 0)
        )
        ttk.Checkbutton(
            tab,
            text="Append every measurement to this file",
            variable=self.csv_on,
            command=self._toggle_csv,
        ).pack(anchor="w")
        ttk.Label(tab, textvariable=self.csv_info).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            tab,
            text=(
                "One row per measurement: " + ", ".join(CSV_META) + ", then "
                "one column per wavelength (header = wavelength in nm, values "
                "in W sr^-1 m^-2 nm^-1). Existing files are appended to if "
                "their header matches."
            ),
            foreground=MUTED,
            wraplength=640,
            justify="left",
        ).pack(anchor="w", pady=(10, 0))

    def _browse_csv(self) -> None:
        path: str = filedialog.asksaveasfilename(
            parent=self.root,
            title="Write spectra to",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*")],
            confirmoverwrite=False,  # rows are appended, nothing is overwritten
        )
        if path:
            self.csv_path.set(path)

    def _toggle_csv(self) -> None:
        if self.csv_on.get() and not self.csv_path.get().strip():
            self.csv_on.set(False)
            self.csv_info.set("Choose a file first")
            return
        self.csv_rows = 0
        self.csv_info.set(
            f"Writing to {self.csv_path.get().strip()}"
            if self.csv_on.get()
            else "Writer off"
        )

    def _style(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        s.configure(
            "TButton", background=PANEL, foreground=FG, borderwidth=0, padding=6
        )
        s.map(
            "TButton",
            background=[("disabled", BG), ("active", "#353943")],
            foreground=[("disabled", MUTED)],
        )
        s.configure(
            "Go.TButton",
            font=("TkDefaultFont", 13, "bold"),
            padding=(18, 12),
            background=ACCENT,
            foreground="#0d1117",
        )
        s.map(
            "Go.TButton",
            background=[("disabled", PANEL), ("active", "#7bbcff")],
            foreground=[("disabled", MUTED)],
        )
        edge: dict[str, str] = {
            "bordercolor": EDGE,
            "lightcolor": PANEL,
            "darkcolor": PANEL,
        }
        s.configure(
            "TCombobox", arrowcolor=FG, background=PANEL, insertcolor=FG, **edge
        )
        s.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL)],
            foreground=[("readonly", FG)],
            background=[("active", "#353943"), ("readonly", PANEL)],
            selectbackground=[("readonly", PANEL)],
            selectforeground=[("readonly", FG)],
        )
        for opt, val in (
            ("background", PANEL),
            ("foreground", FG),
            ("selectBackground", ACCENT),
            ("selectForeground", BG),
        ):
            self.root.option_add(f"*TCombobox*Listbox.{opt}", val)
        s.configure("TEntry", foreground=FG, insertcolor=FG, **edge)
        s.configure("TCheckbutton", indicatorbackground=PANEL, indicatorforeground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TNotebook", background=BG, tabmargins=0, **edge)
        s.configure(
            "TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(14, 5), **edge
        )
        s.map(
            "TNotebook.Tab",
            background=[("selected", BG)],
            foreground=[("selected", FG)],
        )
        s.configure("Card.TFrame", background=PANEL)
        s.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED)
        s.configure(
            "Value.TLabel", background=PANEL, font=("TkDefaultFont", 15, "bold")
        )
        s.configure("Wide.TLabel", background=PANEL, font=("TkDefaultFont", 11, "bold"))

    # --- plot -------------------------------------------------------------

    def _style_axes(self) -> None:
        ax = self.ax
        ax.set_facecolor(FIG_BG)
        ax.tick_params(colors=MUTED, labelsize=8)
        for side, spine in ax.spines.items():
            spine.set_color(EDGE)
            spine.set_visible(side in ("bottom", "left"))
        ax.grid(True, color=EDGE, linewidth=0.5, alpha=0.6)
        ax.set_axisbelow(True)
        ax.set_xlabel("Wavelength (nm)", color=FG, fontsize=9)
        ax.set_ylabel(
            r"Spectral radiance (W sr$^{-1}$ m$^{-2}$ nm$^{-1}$)", color=FG, fontsize=9
        )

    def _draw_empty(self) -> None:
        self.ax.clear()
        self._style_axes()
        self.ax.set_xlim(380, 780)
        self.ax.set_ylim(0, 1)
        self.ax.text(
            0.5,
            0.5,
            "no measurement yet",
            color=MUTED,
            transform=self.ax.transAxes,
            ha="center",
            va="center",
        )
        self.canvas.draw()

    def _draw_spectrum(self, wavelengths: np.ndarray, spectrum: np.ndarray) -> None:
        self.ax.clear()
        self._style_axes()
        peak: float = float(np.max(spectrum)) if spectrum.size else 0.0
        self.ax.set_xlim(float(wavelengths[0]), float(wavelengths[-1]))
        self.ax.set_ylim(0, peak * 1.08 if peak > 0 else 1.0)
        self.ax.fill_between(
            wavelengths, spectrum, color=ACCENT, alpha=0.30, linewidth=0
        )
        self.ax.plot(wavelengths, spectrum, color=FG, linewidth=1.4)
        self.canvas.draw()

    def _mark_colour(self, uv: tuple[float, float] | None) -> None:
        if uv is None:
            self._marker.set_data([], [])
        else:
            self._marker.set_data([uv[0]], [uv[1]])
        self.diagram.draw()

    def clear(self) -> None:
        """Reset the plot, the values and the colour marker."""
        for var in (self.luminance_var, self.radiance_var, self.colour_var):
            var.set(PLACEHOLDER)
        self._mark_colour(None)
        self._draw_empty()

    # --- connection -------------------------------------------------------

    def scan_devices(self) -> None:
        """Fill the device list with the specbos 1211 serial ports found."""
        self.ports = {AUTO: None, DEMO: None}
        for p in find_serial_ports():
            label: str = p.device
            if p.serial_number:
                label += f"  |  SN {p.serial_number}"
            self.ports[label] = p.device
        labels: list[str] = list(self.ports)
        self.dev_box.config(values=labels)
        if not self.dev_var.get().strip():
            self.dev_var.set(labels[0])

    def _connect(self, choice: str) -> tuple[Device, str]:
        """Open and identify the device of a device-box entry; returns it and
        its firmware version. Anything not in the list is taken as a LAN
        address host[:port]. Serial ports are tried at every baud rate the
        firmware supports, since units are not all set to the default."""
        openers: list[Callable[[], Device]]
        if choice == DEMO:
            openers = [DemoSpecbos]
        elif choice in self.ports:
            openers = [
                partial(
                    Specbos1211.from_serial,
                    self.ports[choice],
                    baud=baud,
                    timeout=CONNECT_TIMEOUT_S,
                )
                for baud in BAUDS
            ]
        else:
            host, _, port = choice.partition(":")
            openers = [
                partial(
                    Specbos1211.from_tcp,
                    host,
                    int(port) if port else 2101,
                    timeout=CONNECT_TIMEOUT_S,
                )
            ]
        err: Exception = JetiError("no answer")
        for open_dev in openers:
            dev: Device = open_dev()
            try:
                dev.connect()  # *IDN? identity check
                return dev, dev.firmware_version()
            except (JetiError, ValueError) as e:  # wrong baud: silence or garbage
                err = e
                dev.disconnect()
            except BaseException:
                dev.disconnect()
                raise
        raise err

    def toggle_connection(self) -> None:
        if self.dev is not None:
            self.disconnect()
            return
        choice: str = self.dev_var.get().strip()
        self.status.set(f"Connecting to {choice} ...")
        self.root.update_idletasks()  # blocks up to len(BAUDS) * CONNECT_TIMEOUT_S
        try:
            dev, fw = self._connect(choice)
        except (JetiError, OSError, ValueError) as e:
            self.status.set(f"Connect failed: {e}")
            return
        self.dev = dev
        self.conn_btn.config(text="Disconnect")
        self.status.set(f"Connected to {choice}  |  {fw}")

    def disconnect(self) -> None:
        if self.dev is not None:
            try:
                self.dev.disconnect()
            except (JetiError, OSError):
                pass
        self.dev = None
        self.job = None  # a running measurement's result is dropped
        self.measure_btn.state(["!disabled"])
        self.conn_btn.config(text="Connect")
        self.clear()
        self.status.set("Not connected")

    # --- measurement ------------------------------------------------------

    def measure(self) -> None:
        dev = self.dev
        if dev is None:
            self.status.set("Connect first")
            return
        tint_ms: float = IT_MS.get(self.it_var.get(), 0.0)  # 0: auto
        job: Future[Spectrum] = Future()
        self.job = job
        self.measure_btn.state(["disabled"])
        self.status.set("Measuring ...")
        threading.Thread(target=_measure, args=(job, dev, tint_ms), daemon=True).start()
        self.root.after(POLL_MS, self._poll, job, tint_ms)

    def _poll(self, job: Future[Spectrum], tint_ms: float) -> None:
        if not job.done():
            self.root.after(POLL_MS, self._poll, job, tint_ms)
            return
        if job is not self.job:  # disconnected while measuring
            return
        self.job = None
        self.measure_btn.state(["!disabled"])
        try:
            wavelengths, spectrum = job.result()
        except (JetiError, OSError, ValueError) as e:
            # A timed-out read leaves the device mid-answer; reconnecting is
            # the only clean way back in sync.
            self.disconnect()
            self.status.set(f"Measure failed, disconnected: {e}")
            return
        self._show(wavelengths, spectrum, tint_ms)

    def _show(
        self, wavelengths: np.ndarray, spectrum: np.ndarray, tint_ms: float
    ) -> None:
        luminance, radiance, yuv = analyse(wavelengths, spectrum)
        self._draw_spectrum(wavelengths, spectrum)
        self.luminance_var.set(f"{luminance:.4g}")
        self.radiance_var.set(f"{radiance:.4g}")
        self.colour_var.set(f"Y = {yuv[0]:.4g}, u' = {yuv[1]:.4f}, v' = {yuv[2]:.4f}")
        self._mark_colour((yuv[1], yuv[2]))
        status: str = "Measured  |  integration time " + (
            _it_label(tint_ms) if tint_ms else "auto"
        )
        if self.csv_on.get():
            status += "  |  " + self._write_csv(
                wavelengths, spectrum, tint_ms, luminance, radiance, yuv
            )
        self.status.set(status)

    def _write_csv(
        self,
        wavelengths: np.ndarray,
        spectrum: np.ndarray,
        tint_ms: float,
        luminance: float,
        radiance: float,
        yuv: Yuv,
    ) -> str:
        """Append the spectrum to the CSV file; returns a status note. A failed
        write turns the writer off so the error is not repeated silently."""
        path: Path = Path(self.csv_path.get().strip()).expanduser()
        try:
            append_csv(path, wavelengths, spectrum, tint_ms, luminance, radiance, yuv)
        except (OSError, ValueError) as e:
            self.csv_on.set(False)
            self.csv_info.set(f"Writer stopped: {e}")
            return "CSV write failed, writer stopped (see File writer tab)"
        self.csv_rows += 1
        self.csv_info.set(f"Writing to {path}  |  {self.csv_rows} rows this session")
        return f"saved to {path.name}"

    def close(self) -> None:
        self.disconnect()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
