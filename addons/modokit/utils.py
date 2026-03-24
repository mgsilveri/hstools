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


def _diag(msg: str) -> None:
    """Write a flushed diagnostic breadcrumb via a persistent raw fd.

    Uses os.write + os.fsync so data hits disk even on a hard segfault.
    """
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


# ── Addon preferences helpers ─────────────────────────────────────────────────
_ADDON_NAME = 'modo_style_selection_for_blender'


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
