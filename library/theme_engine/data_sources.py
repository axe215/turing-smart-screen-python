"""DataSourceRegistry — wires `source: ...` strings in theme.yaml to live values.

Layered backend:

  - On Windows (and only Windows), try to use upstream's LHM module
    (library.sensors.sensors_librehardwaremonitor) which gives accurate
    CPU/GPU temp/power/fan/load. Requires admin rights.
  - Fall back to psutil for what's portable (CPU%, RAM, clock, net).
  - For sources that have no backend available, return empty string —
    the renderer skips empty values rather than drawing a placeholder.

LHM is imported lazily so a non-Windows host (or a Windows host running
without admin) doesn't fail at import time — they just get the psutil
subset.
"""
from __future__ import annotations

import logging
import math
import platform
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Optional

import psutil

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend resolver — LHM first on Windows, psutil otherwise
# ---------------------------------------------------------------------------

_lhm_cache = None
_lhm_tried = False
_lhm_lock = threading.Lock()


def _try_load_lhm():
    """Import upstream's LHM sensor module lazily. Returns the module or None.

    The upstream module:
      - only works on Windows,
      - calls sys.exit(0) if not running as admin (we catch SystemExit),
      - reads LibreHardwareMonitorLib.dll at import time from CWD/external/.
    """
    global _lhm_cache, _lhm_tried
    if _lhm_tried:
        return _lhm_cache
    with _lhm_lock:
        if _lhm_tried:
            return _lhm_cache
        _lhm_tried = True
        if platform.system() != "Windows":
            log.info("LHM not used (not Windows) — falling back to psutil")
            return None
        try:
            import library.sensors.sensors_librehardwaremonitor as lhm  # type: ignore
            _lhm_cache = lhm
            log.info("LHM sensor backend initialized (Cpu/Gpu/Memory/Net)")
            return lhm
        except SystemExit:
            log.warning("LHM requires admin rights — falling back to psutil")
        except Exception as exc:
            log.warning("LHM import failed (%s) — falling back to psutil", exc)
        return None


# ---------------------------------------------------------------------------
# Caching for GPU stats — LHM's Gpu.stats() returns 5 values in one call;
# we cache for ~500ms so single-value source funcs don't hammer the API.
# ---------------------------------------------------------------------------

_gpu_stats_cache: Dict[str, object] = {"t": 0.0, "v": None}


def _gpu_stats():
    """Returns (load%, mem%, used_mb, total_mb, temp_C) or all-NaN tuple."""
    lhm = _try_load_lhm()
    if lhm is None:
        return (math.nan, math.nan, math.nan, math.nan, math.nan)
    now = time.monotonic()
    if _gpu_stats_cache["v"] is None or now - _gpu_stats_cache["t"] > 0.5:
        try:
            _gpu_stats_cache["v"] = lhm.Gpu.stats()
            _gpu_stats_cache["t"] = now
        except Exception as exc:
            log.warning("Gpu.stats failed: %s", exc)
            _gpu_stats_cache["v"] = (math.nan, math.nan, math.nan, math.nan, math.nan)
    return _gpu_stats_cache["v"]


# ---------------------------------------------------------------------------
# Per-source value getters — return Optional[float | str] (None when no data)
# ---------------------------------------------------------------------------


def _cpu_percentage():
    lhm = _try_load_lhm()
    if lhm:
        try:
            v = lhm.Cpu.percentage(interval=None)
            if v is not None and not math.isnan(v):
                return float(v)
        except Exception:
            pass
    return psutil.cpu_percent(interval=None)


def _cpu_temp():
    lhm = _try_load_lhm()
    if lhm:
        try:
            v = lhm.Cpu.temperature()
            if v is not None and not math.isnan(v):
                return float(v)
        except Exception:
            pass
    # psutil sensors_temperatures: Windows usually returns empty
    try:
        temps = psutil.sensors_temperatures()
        for entries in (temps or {}).values():
            for e in entries:
                if e.current and e.current > 0:
                    return float(e.current)
    except AttributeError:
        pass
    return None


def _cpu_freq_mhz():
    lhm = _try_load_lhm()
    if lhm:
        try:
            v = lhm.Cpu.frequency()
            if v is not None and not math.isnan(v):
                return float(v)
        except Exception:
            pass
    cf = psutil.cpu_freq()
    return cf.current if cf else None


def _cpu_fan_pct():
    lhm = _try_load_lhm()
    if lhm:
        try:
            v = lhm.Cpu.fan_percent()
            if v is not None and not math.isnan(v):
                return float(v)
        except Exception:
            pass
    return None


def _cpu_power():
    """No psutil equivalent; LHM-only."""
    lhm = _try_load_lhm()
    if lhm is None:
        return None
    # LHM upstream doesn't expose cpu_power directly in the sensors.py API,
    # but the raw hardware enumeration has it. Walk sensors manually.
    try:
        from LibreHardwareMonitor import Hardware  # type: ignore

        cpu = lhm.get_hw_and_update(Hardware.HardwareType.Cpu)
        if cpu is None:
            return None
        for sensor in cpu.Sensors:
            if (
                sensor.SensorType == Hardware.SensorType.Power
                and str(sensor.Name).startswith("CPU Package")
                and sensor.Value is not None
            ):
                return float(sensor.Value)
        # Fallback to any power sensor
        for sensor in cpu.Sensors:
            if sensor.SensorType == Hardware.SensorType.Power and sensor.Value is not None:
                return float(sensor.Value)
    except Exception:
        pass
    return None


def _cpu_model():
    """Return the CPU model name (rarely changes — read once)."""
    lhm = _try_load_lhm()
    if lhm:
        try:
            from LibreHardwareMonitor import Hardware  # type: ignore
            for hw in lhm.handle.Hardware:
                if hw.HardwareType == Hardware.HardwareType.Cpu:
                    return str(hw.Name)
        except Exception:
            pass
    return platform.processor() or "CPU"


def _gpu_load():
    v = _gpu_stats()[0]
    return None if math.isnan(v) else float(v)


def _gpu_temp():
    v = _gpu_stats()[4]
    return None if math.isnan(v) else float(v)


def _gpu_power():
    """LHM-only: walk GPU sensors for Power."""
    lhm = _try_load_lhm()
    if lhm is None:
        return None
    try:
        from LibreHardwareMonitor import Hardware  # type: ignore

        gpu = lhm.Gpu.get_gpu_to_use()
        if gpu is None:
            return None
        for sensor in gpu.Sensors:
            if sensor.SensorType == Hardware.SensorType.Power and sensor.Value is not None:
                return float(sensor.Value)
    except Exception:
        pass
    return None


def _gpu_fan_pct():
    lhm = _try_load_lhm()
    if lhm is None:
        return None
    try:
        v = lhm.Gpu.fan_percent()
        if v is not None and not math.isnan(v):
            return float(v)
    except Exception:
        pass
    return None


def _gpu_model():
    lhm = _try_load_lhm()
    if lhm:
        try:
            return lhm.get_gpu_name() or "GPU"
        except Exception:
            pass
    # psutil-less fallback: try GPUtil
    try:
        import GPUtil  # type: ignore
        gpus = GPUtil.getGPUs()
        if gpus:
            return gpus[0].name
    except Exception:
        pass
    return "GPU"


def _gpu_fps():
    lhm = _try_load_lhm()
    if lhm:
        try:
            v = lhm.Gpu.fps()
            if v is not None and not math.isnan(v):
                return float(v)
        except Exception:
            pass
    return None


def _ram_percent():
    return psutil.virtual_memory().percent


def _ram_total_gb():
    return psutil.virtual_memory().total / (1024 ** 3)


def _ram_used_gb():
    return psutil.virtual_memory().used / (1024 ** 3)


_NET_LAST: Dict[str, tuple] = {}


def _net_rate(direction: str) -> float:
    now = time.monotonic()
    counters = psutil.net_io_counters(pernic=False)
    cur = counters.bytes_sent if direction == "up" else counters.bytes_recv
    prev = _NET_LAST.get(direction, (now, cur))
    dt = now - prev[0]
    rate = (cur - prev[1]) / dt if dt > 0 else 0.0
    _NET_LAST[direction] = (now, cur)
    return max(rate, 0.0)


def _fmt_net(rate: float) -> str:
    if rate < 1024:
        return f"{rate:.0f} B/s"
    if rate < 1024 * 1024:
        return f"{rate / 1024:.1f} KB/s"
    return f"{rate / (1024 * 1024):.2f} MB/s"


# ---------------------------------------------------------------------------
# Source dispatcher
# ---------------------------------------------------------------------------


def _renderer(value_fn: Callable[[], Optional[float]], unit: str = "", fmt: str = "{:.0f}"):
    """Build a callable(show_unit) → str. Empty string when no value."""
    def render(show_unit: bool = False) -> str:
        v = value_fn()
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return ""
        s = fmt.format(v) if isinstance(v, (int, float)) else str(v)
        if show_unit and unit:
            s = f"{s}{unit}"
        return s
    return render


def _str_renderer(value_fn: Callable[[], str]):
    """For string-typed sources (model names, clock)."""
    def render(show_unit: bool = False) -> str:
        try:
            return value_fn() or ""
        except Exception:
            return ""
    return render


DEFAULT_SOURCES: Dict[str, Callable[[bool], str]] = {
    # CPU
    "cpu_percentage": _renderer(_cpu_percentage, unit="%", fmt="{:.0f}"),
    "cpu_temp": _renderer(_cpu_temp, unit="°C", fmt="{:.0f}"),
    "cpu_freq": _renderer(_cpu_freq_mhz, unit=" MHz", fmt="{:.0f}"),
    "cpu_fan_speed": _renderer(_cpu_fan_pct, unit="%", fmt="{:.0f}"),
    "cpu_power": _renderer(_cpu_power, unit=" W", fmt="{:.1f}"),
    "cpu_model": _str_renderer(_cpu_model),
    # GPU
    "gpu_percentage": _renderer(_gpu_load, unit="%", fmt="{:.0f}"),
    "gpu_temp": _renderer(_gpu_temp, unit="°C", fmt="{:.0f}"),
    "gpu_power": _renderer(_gpu_power, unit=" W", fmt="{:.1f}"),
    "gpu_fan_speed": _renderer(_gpu_fan_pct, unit="%", fmt="{:.0f}"),
    "gpu_model": _str_renderer(_gpu_model),
    "fps": _renderer(_gpu_fps, unit="", fmt="{:.0f}"),
    # RAM
    "ram_percentage": _renderer(_ram_percent, unit="%", fmt="{:.0f}"),
    "ram_total": _renderer(_ram_total_gb, unit=" GB", fmt="{:.1f}"),
    "ram_used_gb": _renderer(_ram_used_gb, unit=" GB", fmt="{:.1f}"),
    # Clock
    "clock_time": _str_renderer(lambda: datetime.now().strftime("%H:%M")),
    "clock_date": _str_renderer(lambda: datetime.now().strftime("%Y-%m-%d")),
    # Network
    "net_upload": _str_renderer(lambda: _fmt_net(_net_rate("up"))),
    "net_download": _str_renderer(lambda: _fmt_net(_net_rate("down"))),
}


@dataclass
class DataSourceRegistry:
    """Resolve `source` strings to callable(show_unit) -> str."""

    overrides: Dict[str, Callable[[bool], str]] = field(default_factory=dict)

    def get(self, source: str) -> Callable[[bool], str]:
        if not source:
            return lambda show_unit=False: ""
        if source in self.overrides:
            return self.overrides[source]
        if source in DEFAULT_SOURCES:
            return DEFAULT_SOURCES[source]
        # Unknown source — log once, return empty (renderer will skip)
        log.warning("unknown data source: %s (returning empty)", source)
        return lambda show_unit=False: ""
