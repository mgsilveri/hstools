"""
mgBaker – Perforce helpers via raw ``p4`` CLI.

All functions silently no-op when:
  - The *P4 Auto-checkout* preference is off.
  - ``p4`` is not found on PATH.
  - Any P4 command fails (bad login, file not mapped, etc.).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

import bpy


# ── internal helpers ──────────────────────────────────────────────────────

def _prefs_enabled() -> bool:
    """Return ``True`` when the user has *P4 Auto-checkout* turned on."""
    addon = bpy.context.preferences.addons.get(__package__)
    if addon is None:
        return False
    return addon.preferences.p4_auto_checkout


def _p4_available() -> bool:
    return shutil.which("p4") is not None


def _run_p4(*args: str) -> Optional[str]:
    """Run a ``p4`` command and return its stdout, or ``None`` on failure."""
    try:
        result = subprocess.run(
            ["p4", *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except Exception:
        return None


def _p4_client() -> Optional[str]:
    """Return the current P4CLIENT name."""
    out = _run_p4("set", "P4CLIENT")
    if out is None:
        return None
    # Output looks like: P4CLIENT=my-client (set)
    for line in out.splitlines():
        if line.startswith("P4CLIENT="):
            val = line.split("=", 1)[1]
            # strip trailing "(set)" or "(config)"
            val = val.split("(")[0].strip()
            return val if val else None
    return None


def _p4_get_or_create_cl(description: str) -> Optional[int]:
    """Find or create a pending changelist matching *description*.

    Returns the changelist number, or ``None`` on failure.
    """
    client = _p4_client()
    if client is None:
        return None

    # Look for an existing pending CL with this description.
    out = _run_p4("changes", "-s", "pending", "-c", client)
    if out:
        for line in out.splitlines():
            # "Change 12345 on 2026/04/01 by user@client 'desc text …'"
            if description in line:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return int(parts[1])
                    except ValueError:
                        pass

    # Create a new changelist.
    spec = (
        f"Change: new\n"
        f"Client: {client}\n"
        f"Description: {description}\n"
    )
    try:
        result = subprocess.run(
            ["p4", "change", "-i"],
            input=spec,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        # Output: "Change 12345 created."
        for word in result.stdout.split():
            try:
                return int(word)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _p4_add_or_edit(filepath: str, cl_id: int) -> None:
    """``p4 add`` or ``p4 edit`` *filepath* into changelist *cl_id*."""
    # Resolve to depot/workspace path via ``p4 where``.
    where_out = _run_p4("where", filepath)
    if where_out is None:
        return
    # ``p4 where`` outputs: //depot/path //client/path C:\local\path
    # We need the depot path (first token).
    depot_path = where_out.split()[0] if where_out.strip() else None
    if not depot_path or depot_path.startswith("-"):
        return  # file is not mapped

    # Determine if the file is already in the depot.
    fstat_out = _run_p4("fstat", depot_path)
    needs_add = True
    if fstat_out and "no such file" not in fstat_out.lower():
        # Check for headAction == delete
        if "headAction" in fstat_out:
            for line in fstat_out.splitlines():
                if "headAction" in line and "delete" in line:
                    needs_add = True
                    break
            else:
                needs_add = False
        else:
            needs_add = False

    if needs_add:
        _run_p4("add", "-c", str(cl_id), filepath)
    else:
        _run_p4("edit", "-c", str(cl_id), filepath)


# ── public API ────────────────────────────────────────────────────────────

def get_cl_description() -> str:
    """Build a changelist description.

    Uses ``mg_project.projects.current_project.friendly_name`` when available,
    otherwise falls back to the blend filename.
    """
    friendly = None
    try:
        from mg_project import projects as _mg_projects
        _proj = _mg_projects.current_project
        if _proj is not None:
            friendly = getattr(_proj, "friendly_name", None)
    except Exception:
        pass

    blend_name = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0]
    if friendly:
        return f"{friendly}: {blend_name}"
    return blend_name


def p4_checkout(filepath: str, cl_description: str) -> None:
    """Checkout (or add) *filepath* into a pending CL described by *cl_description*.

    Silently returns on any error — never raises.
    """
    if not _prefs_enabled():
        return
    if not _p4_available():
        return
    try:
        cl_id = _p4_get_or_create_cl(cl_description)
        if cl_id is None:
            return
        _p4_add_or_edit(filepath, cl_id)
    except Exception as exc:
        print(f"[mgBaker P4] checkout failed for {filepath}: {exc}")


def delayed_checkout_tbscene(tbscene_path: str, cl_description: str, max_wait: float = 120.0) -> None:
    """Poll for *tbscene_path* to appear on disk, then P4 checkout it.

    Registers a ``bpy.app.timers`` callback that fires every 2 s.
    Gives up after *max_wait* seconds.
    """
    if not _prefs_enabled() or not _p4_available():
        return

    elapsed = [0.0]

    def _poll() -> Optional[float]:
        if elapsed[0] >= max_wait:
            print(f"[mgBaker P4] gave up waiting for {tbscene_path}")
            return None
        elapsed[0] += 2.0
        if os.path.exists(tbscene_path) and os.path.getsize(tbscene_path) > 0:
            p4_checkout(tbscene_path, cl_description)
            return None  # stop timer
        return 2.0  # retry

    bpy.app.timers.register(_poll, first_interval=2.0)
