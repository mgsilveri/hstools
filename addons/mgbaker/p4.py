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


# Cached changelist IDs keyed by description — avoids repeated p4 round-trips
# and prevents a new CL being created when p4 changes output truncates descriptions.
_cl_id_cache: dict = {}


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
    # Return cached value — prevents creating a new CL on every file checkout.
    if description in _cl_id_cache:
        return _cl_id_cache[description]

    client = _p4_client()
    if client is None:
        return None

    # Use -l (long) to get full untruncated descriptions.
    out = _run_p4("changes", "-l", "-s", "pending", "-c", client)
    if out:
        current_cl: Optional[int] = None
        for line in out.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("Change "):
                parts = line_stripped.split()
                try:
                    current_cl = int(parts[1])
                except (IndexError, ValueError):
                    current_cl = None
            elif current_cl is not None and line_stripped == description:
                _cl_id_cache[description] = current_cl
                return current_cl

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
                cl_id = int(word)
                _cl_id_cache[description] = cl_id
                return cl_id
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

    Reads the current project name from the mg_blender addon preferences
    (``mg_blender`` → ``mgProject.current_project``) when available,
    otherwise falls back to the blend filename alone.
    """
    project_name = None
    try:
        # Extension platform registers addons as "bl_ext.<repo>.mg_blender";
        # legacy installs use just "mg_blender".  Match by suffix to cover both.
        mg = bpy.context.preferences.addons.get("mg_blender")
        if mg is None:
            for key in bpy.context.preferences.addons.keys():
                if key.endswith(".mg_blender"):
                    mg = bpy.context.preferences.addons[key]
                    break
        if mg is not None:
            project_name = mg.preferences.mgProject.current_project or None
    except Exception:
        pass

    blend_name = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0]
    if project_name:
        return f"{project_name}: {blend_name}"
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


def p4_revert(filepath: str) -> None:
    """Revert *filepath* in Perforce if it is currently open for edit or add.

    Silently no-ops when P4 auto-checkout is off, ``p4`` is not on PATH,
    the file is not open, or any command fails.
    """
    if not _prefs_enabled() or not _p4_available():
        return
    try:
        fstat = _run_p4("fstat", filepath)
        # "... action" key is only present when the file is open in the workspace.
        if fstat and "... action" in fstat:
            _run_p4("revert", filepath)
    except Exception as exc:
        print(f"[mgBaker P4] revert failed for {filepath}: {exc}")


def p4_delete_cl_if_empty(cl_description: str) -> None:
    """Delete the named pending changelist if it has no open files.

    Also removes it from the local cache so a fresh CL is created next time.
    """
    if not _prefs_enabled() or not _p4_available():
        return
    cl_id = _cl_id_cache.get(cl_description)
    if cl_id is None:
        return
    try:
        # p4 describe -s lists files still open in the CL.
        out = _run_p4("describe", "-s", str(cl_id))
        if out is None:
            return
        # If no "Affected files" section (or it only shows the header with no paths)
        # the changelist is empty and safe to delete.
        has_files = any(
            line.strip().startswith("... //")
            for line in out.splitlines()
        )
        if not has_files:
            _run_p4("change", "-d", str(cl_id))
            _cl_id_cache.pop(cl_description, None)
            print(f"[mgBaker P4] Deleted empty changelist {cl_id}")
    except Exception as exc:
        print(f"[mgBaker P4] delete CL failed for {cl_id}: {exc}")


def p4_file_status(local_path: str) -> str:
    """Return the depot status of *local_path*.

    Returns one of:
      'NONE'        – not in depot and not local (never exported)
      'LOCAL_ONLY'  – exists locally but not mapped in depot
      'DEPOT_ONLY'  – in depot but not synced locally (haveRev missing)
      'OUTDATED'    – local haveRev < headRev
      'CURRENT'     – local haveRev == headRev

    Always returns 'NONE' if P4 is unavailable or the file is not mapped.
    """
    if not _p4_available():
        return 'NONE'
    try:
        out = _run_p4("fstat", local_path)
        if out is None or "no such file" in out.lower():
            # File not in depot at all
            return 'NONE' if not os.path.isfile(local_path) else 'LOCAL_ONLY'

        # Parse headRev and haveRev from fstat output
        head_rev = have_rev = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("... headRev "):
                try:
                    head_rev = int(line.split()[-1])
                except ValueError:
                    pass
            elif line.startswith("... haveRev "):
                try:
                    have_rev = int(line.split()[-1])
                except ValueError:
                    pass
            elif "headAction" in line and "delete" in line:
                return 'NONE' if not os.path.isfile(local_path) else 'LOCAL_ONLY'

        if head_rev is None:
            return 'LOCAL_ONLY' if os.path.isfile(local_path) else 'NONE'
        if have_rev is None:
            return 'DEPOT_ONLY'
        # haveRev is recorded in the workspace DB even if the file was deleted
        # locally — always check actual disk presence before reporting CURRENT.
        if not os.path.isfile(local_path):
            return 'DEPOT_ONLY'
        if have_rev < head_rev:
            return 'OUTDATED'
        return 'CURRENT'
    except Exception as exc:
        print(f"[mgBaker P4] fstat failed for {local_path}: {exc}")
        return 'NONE'


def p4_sync(local_path: str) -> bool:
    """Force-sync *local_path* to the head revision.

    Returns True on success, False on failure.
    """
    if not _p4_available():
        return False
    try:
        out = _run_p4("sync", "-f", local_path)
        return out is not None
    except Exception as exc:
        print(f"[mgBaker P4] sync failed for {local_path}: {exc}")
        return False


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
