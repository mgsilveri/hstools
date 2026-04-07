"""
mgBaker - Substance Painter plugin.

Reads ``mgbaker_config.json`` from the bakes directory on project open,
imports any existing Toolbag bake outputs as mesh maps, and configures
baking parameters (resolution, cage offset, enabled maps).

Install via the mgBaker Blender addon: Preferences -> Install Painter Plugin.
"""

from __future__ import annotations

PLUGIN_VERSION = "1.1.0"

import json
import math
import os

import substance_painter.baking as baking
import substance_painter.event as event
import substance_painter.project as project
import substance_painter.resource as resource
import substance_painter.textureset as textureset
try:
    from PySide6.QtCore import QUrl, QFileSystemWatcher
except ImportError:
    from PySide2.QtCore import QUrl, QFileSystemWatcher


PLUGIN_NAME = "mgBaker"

# Suffix -> MeshMapUsage.
# MeshMapUsage lives on substance_painter.textureset, not baking.
_SUFFIX_TO_USAGE = {
    "_normal_base":         textureset.MeshMapUsage.Normal,
    "_ambient_occlusion":   textureset.MeshMapUsage.AO,
    "_curvature":           textureset.MeshMapUsage.Curvature,
    "_world_space_normals": textureset.MeshMapUsage.WorldSpaceNormal,
    "_id":                  textureset.MeshMapUsage.ID,
    "_thickness":           textureset.MeshMapUsage.Thickness,
    "_position":            textureset.MeshMapUsage.Position,
}

_SUFFIX_NO_UNDERSCORE_TO_USAGE = {
    k.lstrip("_"): v for k, v in _SUFFIX_TO_USAGE.items()
}


def _find_config(bakes_dir=None):
    """Locate and read mgbaker_config.json."""
    if bakes_dir:
        config_path = os.path.join(bakes_dir, "mgbaker_config.json")
    else:
        proj_path = project.file_path()
        if not proj_path:
            return None
        proj_dir = os.path.dirname(proj_path)
        parent = os.path.dirname(proj_dir)
        config_path = os.path.join(parent, "bakes", "mgbaker_config.json")

    if not os.path.isfile(config_path):
        return None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[{PLUGIN_NAME}] Failed to read config: {exc}")
        return None


def _import_texture_as_mesh_map(ts, usage, file_path):
    """Import file_path as a project resource and assign as a mesh map."""
    try:
        res = resource.import_project_resource(file_path, resource.Usage.TEXTURE)
        ts.set_mesh_map_resource(usage, res.identifier())
        print(f"[{PLUGIN_NAME}] {ts.name}: {usage.name} <- {os.path.basename(file_path)}")
    except Exception as exc:
        print(f"[{PLUGIN_NAME}] {ts.name}: failed to set {usage.name}: {exc}")


def _import_existing_bakes_for_ts(ts, bakes_dir):
    """Import bake files for ts by scanning bakes_dir for files ending in
    <ts.name><suffix>.png (case-insensitive).  Handles any prefix that
    Toolbag may prepend (e.g. group_name_) in tileMode=1.
    """
    ts_lower = ts.name.lower()
    try:
        dir_files = os.listdir(bakes_dir)
    except Exception:
        return
    files_lower = {f.lower(): f for f in dir_files}
    for suffix, usage in _SUFFIX_TO_USAGE.items():
        # suffix has a leading underscore e.g. "_normal_base"
        target = f"{ts_lower}{suffix.lower()}.png"
        matched = next((orig for low, orig in files_lower.items() if low.endswith(target)), None)
        if matched:
            _import_texture_as_mesh_map(ts, usage, os.path.join(bakes_dir, matched))


def _import_existing_bakes(config):
    """Import Toolbag bake outputs as mesh maps for each texture set."""
    bakes_dir = config.get("bakes_dir", "")
    if not bakes_dir or not os.path.isdir(bakes_dir):
        print(f"[{PLUGIN_NAME}] Bakes dir not found: {bakes_dir}")
        return

    all_ts = textureset.all_texture_sets()
    if not all_ts:
        print(f"[{PLUGIN_NAME}] No texture sets found")
        return

    # With tileMode=1 Toolbag produces <prefix>_<TextureSetName>_<suffix>.png.
    # _import_existing_bakes_for_ts scans by endswith so any prefix is handled.
    for ts in all_ts:
        _import_existing_bakes_for_ts(ts, bakes_dir)


def _configure_baking_for_ts(ts, grp):
    """Configure resolution, HP mesh, cage offset and enabled bakers for ts."""
    ts_name = ts.name
    width = grp.get("output_width", 2048)
    height = grp.get("output_height", 2048)
    log2_w = int(math.log2(width)) if width > 0 else 11
    log2_h = int(math.log2(height)) if height > 0 else 11

    # Resolution — Resolution() takes actual pixel values, not log2
    try:
        ts.set_resolution(textureset.Resolution(width, height))
        print(f"[{PLUGIN_NAME}] {ts_name}: resolution {width}x{height}")
    except Exception as exc:
        print(f"[{PLUGIN_NAME}] {ts_name}: resolution failed: {exc}")

    # Unlink common params so each texture set can have its own HP mesh
    try:
        baking.unlink_all_common_parameters()
    except Exception:
        pass

    # Common baking params via BakingParameters.set() with Property keys
    try:
        params = baking.BakingParameters.from_texture_set_name(ts_name)
        common = params.common()

        hp_fbx = grp.get("hp_fbx", "")
        cage_offset = grp.get("cage_offset", 0.01)

        set_values = {
            common["OutputSize"]: (log2_w, log2_h),
            common["LowAsHigh"]: not bool(hp_fbx and os.path.isfile(hp_fbx)),
            common["MaxHeight"]: cage_offset,
            common["MaxDepth"]: cage_offset,
            common["DilationWidth"]: 128,
        }

        if hp_fbx and os.path.isfile(hp_fbx):
            set_values[common["HipolyMesh"]] = QUrl.fromLocalFile(hp_fbx).toString()

        baking.BakingParameters.set(set_values)
        print(f"[{PLUGIN_NAME}] {ts_name}: baking params configured (HP: {os.path.basename(hp_fbx) if hp_fbx else 'none'})")
    except Exception as exc:
        print(f"[{PLUGIN_NAME}] {ts_name}: baking params failed: {exc}")

    # Enabled bakers
    maps = grp.get("maps", [])
    enabled_usages = [_SUFFIX_NO_UNDERSCORE_TO_USAGE[m] for m in maps if m in _SUFFIX_NO_UNDERSCORE_TO_USAGE]
    if enabled_usages:
        try:
            params = baking.BakingParameters.from_texture_set_name(ts_name)
            params.set_enabled_bakers(enabled_usages)
            print(f"[{PLUGIN_NAME}] {ts_name}: {len(enabled_usages)} bakers enabled")
        except Exception as exc:
            print(f"[{PLUGIN_NAME}] {ts_name}: enable bakers failed: {exc}")


def _configure_baking(config):
    """Configure baking params for all texture sets."""
    all_ts = textureset.all_texture_sets()
    if not all_ts:
        return

    groups = config.get("groups")
    if groups:
        ts_by_name = {ts.name: ts for ts in all_ts}
        for grp in groups:
            mat_name = grp.get("mat_name", "")
            ts = ts_by_name.get(mat_name)
            if ts is None:
                print(f"[{PLUGIN_NAME}] No texture set for '{mat_name}'")
                continue
            _configure_baking_for_ts(ts, grp)
    else:
        legacy_grp = {
            "output_width": config.get("output_width", 2048),
            "output_height": config.get("output_height", 2048),
            "cage_offset": config.get("cage_offset", 1.0),
            "hp_fbx": config.get("hp_fbx", ""),
            "maps": config.get("maps", []),
        }
        _configure_baking_for_ts(all_ts[0], legacy_grp)


def _delete_config(config):
    """Delete the relay config so it does not trigger again."""
    bakes_dir = config.get("bakes_dir", "")
    config_path = os.path.join(bakes_dir, "mgbaker_config.json")
    try:
        if os.path.isfile(config_path):
            os.remove(config_path)
            print(f"[{PLUGIN_NAME}] Relay config deleted")
    except Exception as exc:
        print(f"[{PLUGIN_NAME}] Failed to delete config: {exc}")


# -- File watcher for live reload when Painter is already open -------------

_watcher = None


def _setup_watcher(bakes_dir: str):
    """Watch bakes_dir for mgbaker_config.json appearing (re-export from Blender)."""
    global _watcher
    if not bakes_dir or not os.path.isdir(bakes_dir):
        return
    if _watcher is None:
        _watcher = QFileSystemWatcher()
        _watcher.directoryChanged.connect(_on_bakes_dir_changed)
    existing = _watcher.directories()
    if existing:
        _watcher.removePaths(existing)
    _watcher.addPath(bakes_dir)
    print(f"[{PLUGIN_NAME}] Watching '{bakes_dir}'")


def _on_bakes_dir_changed(dir_path: str):
    """Called when a file is added/changed in the watched bakes directory."""
    config_path = os.path.join(dir_path, "mgbaker_config.json")
    if not os.path.isfile(config_path):
        return
    config = _find_config(dir_path)
    if config is None:
        return

    lp_fbx = config.get("lp_fbx", "")
    if lp_fbx and os.path.isfile(lp_fbx):
        print(f"[{PLUGIN_NAME}] Re-export detected - reloading mesh")
        settings = project.MeshReloadingSettings(preserve_strokes=True)

        def _on_reload(status):
            if status == project.ReloadMeshStatus.SUCCESS:
                _import_existing_bakes(config)
                _configure_baking(config)
                _delete_config(config)
            else:
                print(f"[{PLUGIN_NAME}] Mesh reload failed")

        project.reload_mesh(lp_fbx, settings, _on_reload)
    else:
        print(f"[{PLUGIN_NAME}] Re-export detected - updating project")
        _import_existing_bakes(config)
        _configure_baking(config)
        _delete_config(config)


def _on_project_opened(e):
    """Handle ProjectOpened: set up file watcher.

    Actual import and baking configuration is deferred to
    ``_on_edition_entered`` so texture sets are fully initialized.
    """
    bakes_dir = _bakes_dir_from_project()
    _setup_watcher(bakes_dir)


def _on_edition_entered(e):
    """Handle ProjectEditionEntered: import bakes and configure baking once.

    This fires after Painter has fully loaded the mesh and created all texture
    sets, so ``textureset.all_texture_sets()`` is reliably populated here.
    The config JSON acts as a one-shot token — it is deleted after use so
    subsequent Edition-Entered events are no-ops.
    """
    config = _find_config()
    if config is None:
        return
    print(f"[{PLUGIN_NAME}] Config found on edition enter — configuring project")
    _import_existing_bakes(config)
    _configure_baking(config)
    bakes_dir = config.get("bakes_dir", "")
    _delete_config(config)
    # Ensure watcher is pointed at the correct bakes dir
    if bakes_dir:
        _setup_watcher(bakes_dir)


# -- Plugin entry points ----------------------------------------------------

def _bakes_dir_from_project() -> str:
    """Derive the bakes directory path from the currently open project."""
    try:
        proj_path = project.file_path()
        if proj_path:
            return os.path.join(os.path.dirname(os.path.dirname(proj_path)), "bakes")
    except Exception:
        pass
    return ""


def start_plugin():
    print(f"[{PLUGIN_NAME}] Plugin loaded")
    event.DISPATCHER.connect(event.ProjectOpened, _on_project_opened)
    event.DISPATCHER.connect(event.ProjectEditionEntered, _on_edition_entered)
    try:
        if project.is_open():
            bakes_dir = _bakes_dir_from_project()
            _setup_watcher(bakes_dir)
    except Exception as exc:
        print(f"[{PLUGIN_NAME}] start_plugin watcher init error: {exc}")


def close_plugin():
    global _watcher
    print(f"[{PLUGIN_NAME}] Plugin unloaded")
    event.DISPATCHER.disconnect(event.ProjectOpened, _on_project_opened)
    event.DISPATCHER.disconnect(event.ProjectEditionEntered, _on_edition_entered)
    if _watcher is not None:
        _watcher.deleteLater()
        _watcher = None
