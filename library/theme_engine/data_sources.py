"""Registry mapping `source` strings to callables that return formatted values.

Used by WidgetRenderer to populate Data widgets with live values.

For the MVP we use psutil (cross-platform). LHM integration on Windows
can be added later via a different registry implementation that pulls
from `library/sensors/sensors_librehardwaremonitor.py`.

Each source returns a string ready to display. Widgets in YAML have
`show_unit: True` if they want the unit suffix appended (e.g. "%", "°C",
"GB", "MHz").
"""
from __future__ import annotations

import logging
import platform
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Optional

import psutil

log = logging.getLogger(__name__)


_NET_LAST: Dict[str, tuple] = {}


def _net_rate(direction: str) -> float:
    """Bytes/sec since last call. direction = 'up' | 'down'."""
    now = time.monotonic()
    counters = psutil.net_io_counters(pernic=False)
    cur = counters.bytes_sent if direction == "up" else counters.bytes_recv
    prev = _NET_LAST.get(direction, (now, cur))
    rate = 0.0
    dt = now - prev[0]
    if dt > 0:
        rate = (cur - prev[1]) / dt
    _NET_LAST[direction] = (now, cur)
    return max(rate, 0.0)


def _fmt_mbps(rate_bytes: float) -> str:
    """Display network rate similar to UsbMonitorL: B/s, KB/s, MB/s."""
    if rate_bytes < 1024:
        return f"{rate_bytes:.0f} B/s"
    if rate_bytes < 1024 * 1024:
        return f"{rate_bytes / 1024:.1f} KB/s"
    return f"{rate_bytes / (1024 * 1024):.2f} MB/s"


def _cpu_temp() -> Optional[float]:
    try:
        temps = psutil.sensors_temperatures()
    except AttributeError:
        return None  # not on Windows
    if not temps:
        return None
    # Pick the first sensor reporting >0
    for label, entries in temps.items():
        for e in entries:
            if e.current and e.current > 0:
                return float(e.current)
    return None


def _cpu_fan() -> Optional[int]:
    try:
        fans = psutil.sensors_fans()
    except AttributeError:
        return None
    if not fans:
        return None
    for label, entries in fans.items():
        for e in entries:
            if e.current and e.current > 0:
                return int(e.current)
    return None


# Each function returns a tuple (raw_value, unit_string).
# Display formatter pulls the raw_value and appends unit if show_unit=True.
def _src(value_fn: Callable[[], Optional[float]], unit: str = "", fmt: str = "{:.0f}") -> Callable[[bool], str]:
    def render(show_unit: bool = False) -> str:
        v = value_fn()
        if v is None:
            return "—"
        s = fmt.format(v) if isinstance(v, (int, float)) else str(v)
        if show_unit and unit:
            s = f"{s}{unit}"
        return s
    return render


def _gpu_first_field(field: str):
    """Helper: read first GPU's field via GPUtil (Nvidia) — returns None if unavailable."""
    def fn():
        try:
            import GPUtil  # type: ignore
            gpus = GPUtil.getGPUs()
            if not gpus:
                return None
            return getattr(gpus[0], field, None)
        except Exception:
            return None
    return fn


@dataclass
class DataSourceRegistry:
    """Resolve `source` strings to callable(show_unit) -> str."""

    overrides: Dict[str, Callable[[bool], str]] = None  # type: ignore

    def __post_init__(self):
        if self.overrides is None:
            self.overrides = {}

    def get(self, source: str) -> Callable[[bool], str]:
        if not source:
            return lambda show_unit=False: ""
        if source in self.overrides:
            return self.overrides[source]
        if source in DEFAULT_SOURCES:
            return DEFAULT_SOURCES[source]
        return lambda show_unit=False, _s=source: f"?{_s}"


# ---------------------------------------------------------------------------
# Default registry — psutil-backed where possible
# ---------------------------------------------------------------------------


DEFAULT_SOURCES: Dict[str, Callable[[bool], str]] = {
    # CPU
    "cpu_percentage": _src(lambda: psutil.cpu_percent(interval=None), unit="%", fmt="{:.0f}"),
    "cpu_temp": _src(_cpu_temp, unit="°C", fmt="{:.0f}"),
    "cpu_fan_speed": _src(_cpu_fan, unit=" RPM", fmt="{:d}"),
    "cpu_power": _src(lambda: None, unit="W", fmt="{:.1f}"),  # not in psutil; needs LHM
    "cpu_model": (lambda show_unit=False: platform.processor() or "CPU"),
    "cpu_freq": _src(
        lambda: (psutil.cpu_freq().current if psutil.cpu_freq() else None),
        unit=" MHz",
        fmt="{:.0f}",
    ),
    # GPU (NVIDIA via GPUtil if installed)
    "gpu_percentage": _src(_gpu_first_field("load"), unit="%", fmt="{:.0f}"),
    "gpu_temp": _src(_gpu_first_field("temperature"), unit="°C", fmt="{:.0f}"),
    "gpu_fan_speed": _src(lambda: None, unit=" RPM", fmt="{:d}"),  # GPUtil doesn't expose fan
    "gpu_power": _src(lambda: None, unit="W", fmt="{:.1f}"),
    "gpu_model": (lambda show_unit=False: _gpu_first_field("name")() or "GPU"),
    # RAM
    "ram_percentage": _src(lambda: psutil.virtual_memory().percent, unit="%", fmt="{:.0f}"),
    "ram_total": _src(
        lambda: psutil.virtual_memory().total / (1024 ** 3), unit=" GB", fmt="{:.1f}"
    ),
    "ram_used_gb": _src(
        lambda: psutil.virtual_memory().used / (1024 ** 3), unit=" GB", fmt="{:.1f}"
    ),
    # Clock
    "clock_time": (lambda show_unit=False: datetime.now().strftime("%H:%M")),
    "clock_date": (lambda show_unit=False: datetime.now().strftime("%Y-%m-%d")),
    # Network
    "net_upload": (lambda show_unit=False: _fmt_mbps(_net_rate("up"))),
    "net_download": (lambda show_unit=False: _fmt_mbps(_net_rate("down"))),
    # Unmapped placeholders
    "fps": (lambda show_unit=False: "—"),  # would need RTSS integration
}


# GPUtil normalizes percent as 0..1, not 0..100 — wrap that one
def _gpu_pct(show_unit=False):
    fn = _gpu_first_field("load")
    v = fn()
    if v is None:
        return "—"
    return f"{v * 100:.0f}{'%' if show_unit else ''}"


DEFAULT_SOURCES["gpu_percentage"] = _gpu_pct
