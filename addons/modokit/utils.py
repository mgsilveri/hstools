"""
Shared utility functions and diagnostic helpers.
"""

import bpy
import os
import time

# ── UV handle crash-trace log ─────────────────────────────────────────────────
# Set to True to enable verbose UV debug output.
_UV_DEBUG = False

_UV_DEBUG_LOG_PATH = os.path.join(
    os.environ.get('TEMP', os.path.expanduser('~')),
    'blender_uv_handle_debug.log',
)
_uv_debug_log_file = None  # opened on first use, closed on unregister


def _uv_debug_log(msg: str) -> None:
    """Append *msg* to the crash-trace log file (line-buffered, always flushed).

    Also prints to stdout so it appears in an open System Console.
    No-op when _UV_DEBUG is False.
    """
    if not _UV_DEBUG:
        return
    global _uv_debug_log_file
    try:
        if _uv_debug_log_file is None or _uv_debug_log_file.closed:
            _uv_debug_log_file = open(_UV_DEBUG_LOG_PATH, 'a', encoding='utf-8',
                                      buffering=1)
            _uv_debug_log_file.write(
                f"\n=== Blender UV-Handle debug log opened "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            )
        ts = time.strftime('%H:%M:%S') + f'.{int(time.time() * 1000) % 1000:03d}'
        stamped = f'[{ts}] {msg}'
        _uv_debug_log_file.write(stamped + '\n')
        _uv_debug_log_file.flush()
    except Exception:
        pass
    ts = time.strftime('%H:%M:%S') + f'.{int(time.time() * 1000) % 1000:03d}'
    print(f'[{ts}] {msg}', flush=True)


# ── Crash diagnostics ─────────────────────────────────────────────────────────
# Every BMesh-touching function writes a flushed breadcrumb here BEFORE and
# AFTER the risky access.  The last line in the file tells you exactly where
# a hard segfault happened.
_MODO_DIAG_PATH = os.path.join(
    os.environ.get('TEMP', os.path.expanduser('~')),
    'modo_addon_diag.txt',
)
_diag_seq = [0]
try:
    _diag_fd = os.open(_MODO_DIAG_PATH,
                       os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    _sep = (f"\n{'='*60}\n=== Addon loaded {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{'='*60}\n").encode()
    os.write(_diag_fd, _sep)
    os.fsync(_diag_fd)
except Exception:
    _diag_fd = -1


_diag_enabled: bool = False   # toggled from AddonPreferences.debug_crash_trace


def _diag(msg: str) -> None:
    """Write a flushed diagnostic breadcrumb via a persistent raw fd.

    Uses os.write + os.fsync so data hits disk even on a hard segfault.
    Gated by _diag_enabled — off by default, enabled from addon prefs.
    """
    if not _diag_enabled:
        return
    try:
        if _diag_fd < 0:
            return
        _diag_seq[0] += 1
        ts = time.strftime('%H:%M:%S')
        line = f"[{ts}] #{_diag_seq[0]:06d} {msg}\n"
        os.write(_diag_fd, line.encode())
        os.fsync(_diag_fd)
    except Exception:
        pass


# ── Performance timing ────────────────────────────────────────────────────────
# Lightweight call-counter + elapsed accumulator.
# Zero overhead when _perf_enabled is False.
#
# Two entry points:
#   with perf_time("label"):    — measures duration of a code block
#   perf_record("label", sec)   — records a pre-measured value directly
#                                 (used for inter-event intervals)
#
# Call perf_report() to dump a summary to the log file and system console.

import contextlib

_perf_enabled: bool = False
_perf_stats: dict = {}   # label → [call_count, total_sec, max_sec]
# Labels whose "avg" is an interval — report shows derived fps alongside ms.
_INTERVAL_LABELS: set = set()

_PERF_LOG_PATH = os.path.join(
    os.environ.get('TEMP', os.path.expanduser('~')),
    'modokit_perf.log',
)


def perf_reset() -> None:
    _perf_stats.clear()
    _INTERVAL_LABELS.clear()


def perf_record(label: str, seconds: float, is_interval: bool = False) -> None:
    """Record a pre-measured *seconds* value directly under *label*.

    Use *is_interval=True* when *seconds* is the time between two events
    (e.g. inter-rebuild gap) so perf_report() also shows derived fps.
    """
    if not _perf_enabled:
        return
    if is_interval:
        _INTERVAL_LABELS.add(label)
    entry = _perf_stats.get(label)
    if entry is None:
        _perf_stats[label] = [1, seconds, seconds]
    else:
        entry[0] += 1
        entry[1] += seconds
        if seconds > entry[2]:
            entry[2] = seconds


def perf_report() -> None:
    lines = []
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    lines.append(f"\n=== modokit perf report {ts} ===")
    if not _perf_stats:
        lines.append("  (no data recorded)")
    else:
        rows = sorted(_perf_stats.items(), key=lambda kv: -kv[1][1])
        for label, (n, total, mx) in rows:
            avg_ms = (total / n * 1000.0) if n else 0.0
            max_ms = mx * 1000.0
            tot_ms = total * 1000.0
            if label in _INTERVAL_LABELS:
                avg_fps = (1.0 / (total / n)) if (n and total > 0) else 0.0
                lines.append(
                    f"  {label:<36s}  samples={n:>5d}  avg={avg_ms:>8.3f}ms"
                    f"  max={max_ms:>8.3f}ms  ->  avg_fps={avg_fps:>6.1f}"
                )
            else:
                lines.append(
                    f"  {label:<36s}  calls={n:>6d}  avg={avg_ms:>8.3f}ms  "
                    f"max={max_ms:>8.3f}ms  total={tot_ms:>10.3f}ms"
                )
    lines.append("=" * 60)
    report = "\n".join(lines) + "\n"

    # Print to system console
    print(report, flush=True)

    # Append to log file
    try:
        with open(_PERF_LOG_PATH, 'a', encoding='utf-8') as _f:
            _f.write(report)
    except Exception:
        pass

    perf_reset()


@contextlib.contextmanager
def perf_time(label: str):
    """Context manager that records elapsed time under *label* when enabled."""
    if not _perf_enabled:
        yield
        return
    _t0 = time.perf_counter()
    try:
        yield
    finally:
        _elapsed = time.perf_counter() - _t0
        entry = _perf_stats.get(label)
        if entry is None:
            _perf_stats[label] = [1, _elapsed, _elapsed]
        else:
            entry[0] += 1
            entry[1] += _elapsed
            if _elapsed > entry[2]:
                entry[2] = _elapsed


# ── Addon preferences helpers ─────────────────────────────────────────────────
# Matches the folder name when loaded via script directory, and the
# blender_manifest.toml 'id' field when installed as an Extension.
_ADDON_NAME = 'modokit'


def get_addon_preferences(context):
    """Safely get addon preferences with fallback defaults."""
    try:
        return context.preferences.addons[_ADDON_NAME].preferences
    except (KeyError, AttributeError):
        class DefaultPrefs:
            selection_tolerance = 4
            double_click_time = 0.3
            backwire_opacity = 0.35
            debug_raycast = False
            debug_selection = False
            enable_mouse_selection = True
            enable_lasso_selection = True
            enable_backface_viz = True
            enable_component_mode = True
            enable_object_mode_selection = True
            enable_uv_handle_snap = True
            enable_uv_boundary_overlay = True
            enable_uv_flipped_face_viz = True
            enable_instance_tagging = True
            enable_preselect_highlight = True
            preselect_color = (0.549, 0.710, 0.780)
            preselect_alpha = 0.75
            uv_scale_sensitivity = 0.5
            shortest_path_key = 'RIGHTMOUSE'
            shortest_path_shift = True
            shortest_path_ctrl = False
            shortest_path_alt = False
            paint_selection_size = 50
            debug_uv_handle = False
            debug_uv_seam = False
        return DefaultPrefs()


def _get_prefs(context):
    """Return addon preferences or None."""
    try:
        return context.preferences.addons[_ADDON_NAME].preferences
    except (KeyError, AttributeError):
        return None


# ── Geometry utility ──────────────────────────────────────────────────────────

def point_in_polygon(point, polygon):
    """Ray casting algorithm for 2D point-in-polygon test.

    Args:
        point: (x, y) tuple in screen space
        polygon: list of (x, y) tuples forming the polygon boundary
    Returns:
        True if point is inside the polygon
    """
    x, y = point
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-10) + xi):
            inside = not inside
        j = i
    return inside
