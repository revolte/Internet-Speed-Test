import csv
import datetime
import json
import math
import os
import queue
import threading
import time
import tkinter as tk
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

COLORS = {
    "ink": "#14213d", "muted": "#5b6b86", "line": "#dde4f0", "paper": "#f3f6fb",
    "card": "#ffffff", "accent": "#2447d6", "accent_dark": "#1a36a8",
    "ok": "#1d7a55", "err": "#b3261e",
}
PHASES = [("server", "Find server"), ("download", "Download"), ("upload", "Upload")]
PHASE_ORDER = [key for key, _ in PHASES]
EMPTY = "–"
USER_AGENT = {"User-Agent": "Mozilla/5.0"}

# The server search is the slow part, so its result is reused for a few minutes.
SERVER_CACHE_TTL_SECONDS = 300
HISTORY_FILE = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / \
    "internet-speed-test" / "speed_test_history.csv"
_session = {"tester": None, "server": None, "isp": None, "cached_at": None}


def format_mbps(bits_per_second):
    """Convert bits per second to Mbps. Raises if the measurement returned nothing usable."""
    try:
        value = float(bits_per_second) / 1_000_000
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        raise RuntimeError("The measurement returned no data")
    return value


def describe_server(server):
    if not isinstance(server, dict):
        return {"name": "Unknown server", "location": "Unknown", "distance_km": None}

    name = server.get("sponsor") or server.get("host") or "Unknown server"
    place = ", ".join(part for part in (server.get("name"), server.get("country")) if part)
    distance = server.get("d")
    try:
        distance_km = float(distance) if distance not in (None, "") else None
    except (TypeError, ValueError):
        distance_km = None
    return {"name": name, "location": place or "Unknown", "distance_km": distance_km}


def append_history(download_mbps, upload_mbps, ping_ms, server, isp):
    """Append one completed speed test to the CSV history file."""
    fields = (
        "timestamp_utc", "download_mbps", "upload_mbps", "ping_ms",
        "server", "location", "distance_km", "isp",
    )
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as history:
        writer = csv.DictWriter(history, fieldnames=fields)
        if needs_header:
            writer.writeheader()
        writer.writerow({
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "download_mbps": f"{download_mbps:.2f}",
            "upload_mbps": f"{upload_mbps:.2f}",
            "ping_ms": f"{ping_ms:.2f}" if ping_ms is not None else "",
            "server": server.get("name", ""),
            "location": server.get("location", ""),
            "distance_km": server.get("distance_km") if server.get("distance_km") is not None else "",
            "isp": isp or "",
        })


# ---------- window icon ----------
def _hex_to_rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def create_window_icon(root):
    """Draw a small speedometer icon (no asset file needed), smoothed by drawing at 4x and averaging down."""
    size, scale = 48, 4
    big = size * scale
    background = _hex_to_rgb(COLORS["ink"])
    canvas = [[background] * big for _ in range(big)]
    cx, cy = 24 * scale, 26 * scale

    def disk(x, y, radius, color):
        rgb = _hex_to_rgb(color)
        for py in range(max(0, int(y - radius)), min(big, int(y + radius) + 1)):
            for px in range(max(0, int(x - radius)), min(big, int(x + radius) + 1)):
                if (px - x) ** 2 + (py - y) ** 2 <= radius ** 2:
                    canvas[py][px] = rgb

    def line(x0, y0, x1, y1, color, thickness):
        steps = max(int(abs(x1 - x0)), int(abs(y1 - y0)), 1)
        for step in range(steps + 1):
            fraction = step / steps
            disk(x0 + (x1 - x0) * fraction, y0 + (y1 - y0) * fraction, thickness / 2, color)

    disk(cx, cy, 20 * scale, "#ffffff")
    disk(cx, cy, 17 * scale, COLORS["paper"])
    for start, end, color in ((200, 244, COLORS["muted"]), (248, 292, COLORS["accent"]), (296, 340, COLORS["ok"])):
        for half_degrees in range(start * 2, end * 2 + 1):
            angle = math.radians(half_degrees / 2)
            disk(cx + 13 * scale * math.cos(angle), cy + 13 * scale * math.sin(angle), 1.5 * scale, color)

    needle = math.radians(225)
    line(cx, cy, cx + 11 * scale * math.cos(needle), cy + 11 * scale * math.sin(needle),
         COLORS["accent_dark"], 3 * scale)
    disk(cx, cy, 3 * scale, COLORS["accent_dark"])

    image = tk.PhotoImage(master=root, width=size, height=size)
    area = scale * scale
    for y in range(size):
        row = []
        for x in range(size):
            block = [canvas[y * scale + dy][x * scale + dx] for dy in range(scale) for dx in range(scale)]
            red, green, blue = (sum(pixel[i] for pixel in block) // area for i in range(3))
            row.append(f"#{red:02x}{green:02x}{blue:02x}")
        image.put("{" + " ".join(row) + "}", to=(0, y))
    return image


# ---------- finding a server ----------
def fetch_nearby_servers(limit=10):
    """Ask speedtest.net for the servers closest to this connection (one small request)."""
    url = f"https://www.speedtest.net/api/js/servers?engine=js&limit={limit}"
    request = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(request, timeout=6) as response:
        data = json.load(response)
    servers = [
        {"url": item["url"], "host": item.get("host"), "name": item.get("name"),
         "country": item.get("country"), "sponsor": item.get("sponsor"),
         "d": item.get("distance"), "id": item.get("id")}
        for item in data if item.get("url")
    ]
    if not servers:
        raise ValueError("empty server list")
    return servers


def probe_latency(server, attempts=3, timeout=3):
    """Lowest round-trip time to a server in ms, or None if it doesn't answer quickly."""
    url = server["url"].rsplit("/", 1)[0] + "/latency.txt"
    best = None
    for _ in range(attempts):
        started = time.perf_counter()
        try:
            request = urllib.request.Request(url, headers=USER_AGENT)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if not response.read(64).startswith(b"test=test"):
                    return best
        except Exception:
            return best  # unreachable or slow: don't wait for more attempts
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best


def pick_fastest(servers):
    """Probe every candidate at the same time and return (server, latency_ms) for the fastest."""
    with ThreadPoolExecutor(max_workers=len(servers)) as pool:
        latencies = list(pool.map(probe_latency, servers))
    reachable = [(latency, server) for server, latency in zip(servers, latencies) if latency is not None]
    if not reachable:
        raise RuntimeError("No test server responded")
    latency, server = min(reachable, key=lambda pair: pair[0])
    return server, latency


def make_tester(speedtest):
    try:
        return speedtest.Speedtest(secure=True, timeout=10)
    except Exception:
        return speedtest.Speedtest(timeout=10)


def search_for_server(speedtest, events):
    """Pick the closest, fastest-responding server. Candidates are checked in parallel with short timeouts."""
    events.put(("phase", "server", "Finding nearby servers…"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        tester_future = pool.submit(make_tester, speedtest)
        servers_future = pool.submit(fetch_nearby_servers)
        tester = tester_future.result()
        try:
            servers = servers_future.result()
        except Exception:
            servers = None

    if servers is None:  # backup route: the library's own (slower) server list
        events.put(("phase", "server", "Using the backup server list (slower)…"))
        tester.get_servers()
        servers = tester.get_closest_servers(10)

    events.put(("phase", "server", "Checking which server responds fastest…"))
    best, latency = pick_fastest(servers)

    # Hand the choice to the speedtest library so it runs the download and upload against it.
    tester._best = best  # `best` is a read-only property in speedtest-cli; _best is what it returns
    tester.results.server = best
    tester.results.ping = latency

    client = getattr(tester.results, "client", None) or {}
    _session.update(tester=tester, server=describe_server(best), isp=client.get("isp"),
                    cached_at=time.monotonic())
    return tester


def get_tester(speedtest, events):
    """Reuse the cached server while it is fresh. Otherwise search again, falling back to the old one."""
    cached = _session["tester"]
    cached_at = _session["cached_at"]
    if cached is not None and cached_at is not None and time.monotonic() - cached_at < SERVER_CACHE_TTL_SECONDS:
        events.put(("phase", "server", "Using the server found earlier…"))
        return cached
    try:
        return search_for_server(speedtest, events)
    except Exception:
        if cached is not None:
            events.put(("phase", "server", "Server search failed, using the previous server…"))
            return cached
        raise


def run_speed_test(events):
    """Runs in a background thread and reports progress through the events queue."""
    try:
        try:
            import speedtest
        except ImportError:
            events.put(("error", "The speedtest-cli package is missing. Run: pip install speedtest-cli"))
            return

        tester = get_tester(speedtest, events)
        ping = getattr(getattr(tester, "results", None), "ping", None)
        events.put(("server", _session["server"], _session["isp"], ping))

        events.put(("phase", "download", "Measuring download speed…"))
        download_mbps = format_mbps(tester.download())
        events.put(("download", download_mbps))

        events.put(("phase", "upload", "Measuring upload speed…"))
        upload_mbps = format_mbps(tester.upload())
        events.put(("upload", upload_mbps))

        server = _session["server"] or {}
        try:
            append_history(download_mbps, upload_mbps, ping, server, _session["isp"])
            events.put(("done", str(HISTORY_FILE), None))
        except (OSError, csv.Error) as error:
            events.put(("done", None, f"Could not save test history: {error}"))
    except Exception as error:
        _session.update(tester=None, server=None, isp=None, cached_at=None)  # search afresh next time
        events.put(("error", f"Test failed: {error or 'unknown error'}. Check your connection and try again."))


class SpeedTestApp:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.running = False
        self.started = 0.0
        self.base_status = ""

        root.title("Internet Speed Test")
        root.configure(bg=COLORS["paper"])
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.window_icon = create_window_icon(root)
        root.iconphoto(True, self.window_icon)

        families = set(tkfont.families())
        self.family = next(
            (f for f in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Cantarell", "DejaVu Sans")
             if f in families),
            "TkDefaultFont",
        )
        self._build_styles()
        self._build_ui()
        self.fit()
        self.root.after(100, self._poll_events)
        self.root.after(500, self._tick)

    # ---------- UI ----------
    def font(self, size, weight="normal"):
        return (self.family, size, weight)

    def fit(self):
        """Resize the window to exactly fit its current content."""
        self.root.update_idletasks()
        self.root.geometry("")

    def _build_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Accent.TButton", background=COLORS["accent"], foreground="white", borderwidth=0,
            focuscolor=COLORS["accent"], padding=(22, 10), font=self.font(11, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("disabled", "#9fb0ea"), ("active", COLORS["accent_dark"])],
            foreground=[("disabled", "white")],
        )
        style.configure(
            "Speed.Horizontal.TProgressbar", troughcolor=COLORS["line"], background=COLORS["accent"],
            borderwidth=0, thickness=4,
        )

    def _label(self, parent, text="", size=10, weight="normal", color="ink", bg="card", **kwargs):
        return tk.Label(parent, text=text, font=self.font(size, weight), fg=COLORS[color],
                        bg=COLORS[bg], **kwargs)

    def _build_ui(self):
        outer = tk.Frame(self.root, bg=COLORS["paper"], padx=24, pady=22)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=COLORS["paper"])
        header.pack(fill="x", pady=(0, 18))
        tk.Label(header, text="Internet Speed Test", image=self.window_icon, compound="left",
                 font=self.font(22, "bold"), fg=COLORS["ink"], bg=COLORS["paper"], padx=12).pack(anchor="w")
        self._label(header, "Measure your download and upload speed against the nearest test server.",
                    10, color="muted", bg="paper", wraplength=440, justify="left").pack(anchor="w", pady=(4, 0))

        card = tk.Frame(outer, bg=COLORS["card"], highlightthickness=1,
                        highlightbackground=COLORS["line"], padx=22, pady=20)
        card.pack(fill="both", expand=True)

        # Progress steps
        steps = tk.Frame(card, bg=COLORS["card"])
        steps.pack(fill="x")
        self.step_bars, self.step_labels = {}, {}
        for column, (key, text) in enumerate(PHASES):
            steps.columnconfigure(column, weight=1, uniform="steps")
            cell = tk.Frame(steps, bg=COLORS["card"])
            cell.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 4, 0))
            bar = tk.Frame(cell, bg=COLORS["line"], height=4)
            bar.pack(fill="x")
            label = self._label(cell, text, 9, color="muted")
            label.pack(anchor="w", pady=(6, 0))
            self.step_bars[key], self.step_labels[key] = bar, label

        # Download / upload readings
        readings = tk.Frame(card, bg=COLORS["card"])
        readings.pack(fill="x", pady=(18, 0))
        self.reading_vars = {}
        for column, (key, title) in enumerate((("download", "Download"), ("upload", "Upload"))):
            readings.columnconfigure(column, weight=1, uniform="readings")
            box = tk.Frame(readings, bg=COLORS["paper"], padx=16, pady=14)
            box.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 7, 7 if column == 0 else 0))
            self._label(box, title, 10, color="muted", bg="paper").pack(anchor="w")
            var = tk.StringVar(value=EMPTY)
            self.reading_vars[key] = var
            tk.Label(box, textvariable=var, font=self.font(32, "bold"), fg=COLORS["ink"],
                     bg=COLORS["paper"]).pack(anchor="w", pady=(6, 0))
            self._label(box, "Mbps", 10, color="muted", bg="paper").pack(anchor="w")

        # Server and connection details
        tk.Frame(card, bg=COLORS["line"], height=1).pack(fill="x", pady=(20, 8))
        details = tk.Frame(card, bg=COLORS["card"])
        details.pack(fill="x")
        details.columnconfigure(0, weight=1, uniform="details")
        details.columnconfigure(1, weight=1, uniform="details")
        self.detail_vars = {}
        fields = [
            ("server", "Test server", 0, 0, 2, 420),
            ("location", "Location", 1, 0, 1, 200),
            ("distance", "Distance", 1, 1, 1, 200),
            ("ping", "Ping", 2, 0, 1, 200),
            ("isp", "Your provider", 2, 1, 1, 200),
        ]
        for key, title, row, column, span, wrap in fields:
            cell = tk.Frame(details, bg=COLORS["card"])
            cell.grid(row=row, column=column, columnspan=span, sticky="ew", pady=6)
            self._label(cell, title, 9, color="muted").pack(anchor="w")
            var = tk.StringVar(value=EMPTY)
            self.detail_vars[key] = var
            tk.Label(cell, textvariable=var, font=self.font(11, "bold"), fg=COLORS["ink"],
                     bg=COLORS["card"], anchor="w", justify="left", wraplength=wrap).pack(anchor="w")

        # Action row
        actions = tk.Frame(card, bg=COLORS["card"])
        actions.pack(fill="x", pady=(16, 0))
        controls = tk.Frame(actions, bg=COLORS["card"])
        controls.pack(fill="x")
        self.button = ttk.Button(controls, text="Start test", style="Accent.TButton", command=self.start)
        self.button.pack(side="left", anchor="n")
        self.history_button = ttk.Button(controls, text="Open history", command=self.toggle_history)
        self.history_button.pack(side="right", anchor="n")
        self.status = tk.Label(actions, text="Ready when you are.", font=self.font(10), fg=COLORS["muted"],
                               bg=COLORS["card"], anchor="w", justify="left", wraplength=520)
        self.status.pack(fill="x", pady=(10, 0), anchor="w")

        self.progress = ttk.Progressbar(card, mode="indeterminate", style="Speed.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(16, 0))

        self.history_frame = tk.Frame(card, bg=COLORS["card"])
        self._label(self.history_frame, "Test history", 12, "bold").pack(anchor="w", pady=(14, 6))
        table_frame = tk.Frame(self.history_frame, bg=COLORS["card"])
        table_frame.pack(fill="both", expand=True)
        columns = ("timestamp", "download", "upload", "ping", "server", "location")
        history_style = ttk.Style(self.root)
        history_style.configure(
            "History.Treeview",
            font=self.font(10),
            rowheight=28,
            background=COLORS["card"],
            fieldbackground=COLORS["card"],
            foreground=COLORS["ink"],
        )
        history_style.configure(
            "History.Treeview.Heading",
            font=self.font(10, "bold"),
            background=COLORS["paper"],
            foreground=COLORS["ink"],
        )
        self.history_table = ttk.Treeview(
            table_frame, columns=columns, show="headings", height=6, style="History.Treeview",
        )
        headings = {
            "timestamp": ("Time (UTC)", 145),
            "download": ("Download Mbps", 105),
            "upload": ("Upload Mbps", 105),
            "ping": ("Ping ms", 80),
            "server": ("Server", 150),
            "location": ("Location", 150),
        }
        for column, (title, width) in headings.items():
            self.history_table.heading(column, text=title)
            self.history_table.column(
                column,
                width=width,
                minwidth=70,
                stretch=False,
                anchor="e" if column in ("download", "upload", "ping") else "w",
            )
        vertical_scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.history_table.yview,
        )
        horizontal_scrollbar = ttk.Scrollbar(
            table_frame, orient="horizontal", command=self.history_table.xview,
        )
        self.history_table.configure(
            yscrollcommand=vertical_scrollbar.set,
            xscrollcommand=horizontal_scrollbar.set,
        )
        horizontal_scrollbar.pack(side="bottom", fill="x", pady=(2, 0))
        vertical_scrollbar.pack(side="right", fill="y")
        self.history_table.pack(side="left", fill="both", expand=True)
        self.history_table.tag_configure("alternate", background=COLORS["paper"])
        self.root.bind("<Return>", lambda _event: self.start())

    # ---------- state ----------
    def set_status(self, text, error=False):
        self.base_status = text
        self.status.configure(text=text, fg=COLORS["err"] if error else COLORS["muted"])
        self.fit()

    def on_close(self):
        self.running = False
        self.root.destroy()

    def _tick(self):
        """While a test runs, show how long it has been going so slow steps don't look frozen."""
        if self.running:
            elapsed = int(time.monotonic() - self.started)
            self.status.configure(text=f"{self.base_status} ({elapsed}s)")
        self.root.after(500, self._tick)

    def set_phase(self, phase):
        """Mark steps before `phase` done, `phase` active, and the rest pending. 'done' completes all."""
        current = PHASE_ORDER.index(phase) if phase in PHASE_ORDER else -1
        for index, key in enumerate(PHASE_ORDER):
            if phase == "done" or index < current:
                bar, text_color, weight = COLORS["ok"], "ink", "normal"
            elif index == current:
                bar, text_color, weight = COLORS["accent"], "ink", "bold"
            else:
                bar, text_color, weight = COLORS["line"], "muted", "normal"
            self.step_bars[key].configure(bg=bar)
            self.step_labels[key].configure(fg=COLORS[text_color], font=self.font(9, weight))

    def reset_results(self):
        for var in self.reading_vars.values():
            var.set(EMPTY)
        for var in self.detail_vars.values():
            var.set(EMPTY)

    def toggle_history(self):
        if self.history_frame.winfo_manager():
            self.history_frame.pack_forget()
            self.history_button.configure(text="Open history")
            self.fit()
            return

        try:
            with HISTORY_FILE.open(newline="", encoding="utf-8") as history_file:
                rows = list(csv.DictReader(history_file))
        except FileNotFoundError:
            self.set_status("No test history has been saved yet.", error=True)
            return
        except (OSError, csv.Error) as error:
            self.set_status(f"Could not open test history: {error}", error=True)
            return

        self.history_table.delete(*self.history_table.get_children())
        for index, row in enumerate(reversed(rows)):
            timestamp = row.get("timestamp_utc", "")
            timestamp = timestamp.replace("T", " ")[:16]
            self.history_table.insert("", "end", values=(
                timestamp,
                row.get("download_mbps", ""),
                row.get("upload_mbps", ""),
                row.get("ping_ms", ""),
                row.get("server", ""),
                row.get("location", ""),
            ), tags=("alternate",) if index % 2 else ())
        self.history_frame.pack(fill="x", pady=(8, 0))
        self.history_button.configure(text="Close history")
        self.fit()

    def finish(self):
        self.running = False
        self.progress.stop()
        self.button.configure(text="Test again", state="normal")

    def start(self):
        if self.running:
            return
        self.running = True
        self.started = time.monotonic()
        self.reset_results()
        self.set_phase("server")
        self.set_status("Preparing test…")
        self.button.configure(state="disabled")
        self.progress.start(12)
        threading.Thread(target=run_speed_test, args=(self.events,), daemon=True).start()

    def _poll_events(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle(self, event):
        kind = event[0]
        if kind == "phase":
            self.set_phase(event[1])
            self.set_status(event[2])
        elif kind == "server":
            _, server, isp, ping = event
            server = server or {}
            distance = server.get("distance_km")
            self.detail_vars["server"].set(server.get("name") or "Unknown server")
            self.detail_vars["location"].set(server.get("location") or "Unknown")
            self.detail_vars["distance"].set(f"{distance:,.0f} km" if distance is not None else EMPTY)
            self.detail_vars["ping"].set(f"{ping:.0f} ms" if ping is not None else EMPTY)
            self.detail_vars["isp"].set(isp or EMPTY)
            self.fit()
        elif kind in ("download", "upload"):
            self.reading_vars[kind].set(f"{float(event[1]):.2f}")
        elif kind == "done":
            _, history_path, history_error = event
            self.set_phase("done")
            self.finish()
            if history_error:
                self.set_status(f"Test complete. {history_error}", error=True)
            else:
                self.set_status(f"Test complete. Saved to {Path(history_path).name}.")
        elif kind == "error":
            self.set_phase("none")
            self.finish()
            self.set_status(event[1], error=True)


def main():
    root = tk.Tk()
    SpeedTestApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()