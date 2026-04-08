"""
mgBaker – Export operators (Toolbag, Painter, FBX-only).

Non-destructive FBX export: duplicate into temp collection → apply modifiers
via depsgraph → optional triangulate → export → cleanup in ``finally``.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time

import bpy
from bpy.props import EnumProperty

from .baker_props import get_output_name
from . import p4


# ── Preferences shortcut ─────────────────────────────────────────────────

def _prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


# ── Bake-map definitions ─────────────────────────────────────────────────

# (prop_name, toolbag_map_name, output_suffix)
_MAP_DEFS = [
    ("bake_normal",       "Normals",            "_normal_base"),
    ("bake_ao",           "Ambient Occlusion",  "_ambient_occlusion"),
    ("bake_curvature",    "Curvature",           "_curvature"),
    ("bake_world_normal", "Normals (Object)",    "_world_space_normals"),
    ("bake_id",           "Object ID",           "_id"),
    ("bake_thickness",    "Thickness",           "_thickness"),
    ("bake_position",     "Position",            "_position"),
    ("bake_uv_islands",   "UV Island",           "_uv_islands"),
    ("bake_opacity",      "Transparency",        "_opacity"),
    ("bake_height",       "Height",              "_height"),
]


# ── Helpers ───────────────────────────────────────────────────────────────

# ── Painter plugin helpers ───────────────────────────────────────────────

_PAINTER_PLUGIN_DIR = os.path.join(
    os.environ.get("USERPROFILE", ""),
    "Documents", "Adobe", "Adobe Substance 3D Painter",
    "python", "plugins", "mgbaker",
)
_PAINTER_PLUGIN_SRC = os.path.join(os.path.dirname(__file__), "painter_plugin", "__init__.py")


def _read_plugin_version(filepath: str) -> str:
    """Read PLUGIN_VERSION = "x.y.z" from a plugin __init__.py without importing it."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PLUGIN_VERSION"):
                    return line.split('"')[1]
    except Exception:
        pass
    return ""


def _ensure_painter_plugin() -> str:
    """Install or update the Painter plugin if needed.

    Returns a log line describing what happened, or "" if already up to date.
    """
    if not os.path.isfile(_PAINTER_PLUGIN_SRC):
        return ""

    src_version = _read_plugin_version(_PAINTER_PLUGIN_SRC)
    dest_file = os.path.join(_PAINTER_PLUGIN_DIR, "__init__.py")

    if os.path.isfile(dest_file):
        dest_version = _read_plugin_version(dest_file)
        if dest_version == src_version:
            return ""  # already up to date
        action = f"updated {dest_version} → {src_version}"
    else:
        action = f"installed v{src_version}"

    try:
        os.makedirs(_PAINTER_PLUGIN_DIR, exist_ok=True)
        shutil.copy2(_PAINTER_PLUGIN_SRC, dest_file)
        return f"✓ Painter plugin {action}"
    except Exception as exc:
        return f"⚠ Painter plugin install failed: {exc}"


def _bakes_dir() -> str:
    """Return the absolute bakes directory, creating it if needed."""
    prefs = _prefs()
    sub = prefs.bakes_subfolder if prefs else "bakes"
    blend_dir = os.path.dirname(bpy.data.filepath)
    d = os.path.join(blend_dir, sub)
    os.makedirs(d, exist_ok=True)
    return d


def _store_log(context, log_lines):
    """Persist log lines to scene property and print to console."""
    for line in log_lines:
        print(f"[mgBaker] {line}")
    log = context.scene.mg_export_log
    log.clear()
    for line in log_lines:
        if not line:
            continue
        item = log.add()
        if line.startswith("\u2713"):
            item.level = 'OK'
            item.text = line[1:].lstrip()
        elif line.startswith("\u2717") or line.startswith("\u26a0"):
            item.level = 'WARN'
            item.text = line[1:].lstrip()
        elif line.startswith("\u25bc"):
            item.level = 'SECTION'
            item.text = line[1:].lstrip()
        else:
            item.level = 'INFO'
            item.text = line


def _apply_modifiers_depsgraph(obj):
    """Bake all modifiers into the mesh data via depsgraph evaluation."""
    dg = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(dg)
    new_mesh = bpy.data.meshes.new_from_object(obj_eval)
    obj.data = new_mesh
    obj.modifiers.clear()


def _add_triangulate_modifier(obj):
    mod = obj.modifiers.new("__mgbaker_tri__", 'TRIANGULATE')
    mod.quad_method = 'SHORTEST_DIAGONAL'
    mod.ngon_method = 'BEAUTY'


def _apply_smooth_by_uv(obj):
    """Mark edges sharp at UV island boundaries; smooth all other faces.

    Ensures tangent-space normal-map splits align exactly with UV seams,
    preventing bake artifacts in Toolbag / Painter.  Must be called after
    modifiers are applied so the final UV layout is used.
    """
    import bmesh
    mesh = obj.data

    # Smooth all faces first.
    for poly in mesh.polygons:
        poly.use_smooth = True

    bm = bmesh.new()
    bm.from_mesh(mesh)

    uv_layer = bm.loops.layers.uv.active
    if uv_layer is None:
        bm.free()
        return

    for edge in bm.edges:
        if not edge.is_manifold or len(edge.link_loops) != 2:
            # Boundary / non-manifold edge — keep sharp.
            edge.smooth = False
            continue

        l0 = edge.link_loops[0]
        l1 = edge.link_loops[1]
        # l0 and l1 wind in opposite directions around the shared edge:
        #   l0.vert       == l1.link_loop_next.vert  (vertex A)
        #   l0.link_loop_next.vert == l1.vert         (vertex B)
        uv_a0 = l0[uv_layer].uv
        uv_a1 = l1.link_loop_next[uv_layer].uv
        uv_b0 = l0.link_loop_next[uv_layer].uv
        uv_b1 = l1[uv_layer].uv

        seam = (
            (uv_a0 - uv_a1).length > 1e-5
            or (uv_b0 - uv_b1).length > 1e-5
        )
        edge.smooth = not seam

    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


def _prepare_collection(source_col, group):
    """Duplicate all meshes from *source_col* into a temp collection.

    Applies modifiers, smooth-by-uv, triangulate, and export-at-origin
    according to group settings.  Returns the temp collection (caller must
    clean up via ``_cleanup_temp_collection``).
    """
    temp_col = bpy.data.collections.new("__mgbaker_temp__")
    bpy.context.scene.collection.children.link(temp_col)

    for obj in source_col.objects:
        if obj.type != 'MESH':
            continue
        dup = obj.copy()
        dup.data = obj.data.copy()
        temp_col.objects.link(dup)

        if group.apply_modifiers:
            _apply_modifiers_depsgraph(dup)

        if group.smooth_by_uv:
            _apply_smooth_by_uv(dup)

        if group.triangulate:
            _add_triangulate_modifier(dup)
            _apply_modifiers_depsgraph(dup)

        if group.export_at_origin:
            dup.location = (0, 0, 0)
            dup.rotation_euler = (0, 0, 0)
            dup.scale = (1, 1, 1)

    return temp_col


def _cleanup_temp_collection(temp_col):
    orphan_meshes = []
    for obj in list(temp_col.objects):
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh and mesh.users == 0:
            orphan_meshes.append(mesh)
    for mesh in orphan_meshes:
        try:
            bpy.data.meshes.remove(mesh)
        except ReferenceError:
            pass
    bpy.data.collections.remove(temp_col)
    # Also purge any intermediate meshes left by _apply_modifiers_depsgraph
    for mesh in list(bpy.data.meshes):
        if mesh.users == 0 and mesh.name.startswith("__mgbaker_temp__"):
            bpy.data.meshes.remove(mesh)


def _export_fbx(filepath, collection):
    """Export *collection* to FBX at *filepath*."""
    # Ensure Object mode
    if bpy.context.active_object and bpy.context.active_object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    # Deselect all, select only objects in collection
    bpy.ops.object.select_all(action='DESELECT')
    for obj in collection.objects:
        obj.select_set(True)

    bpy.ops.export_scene.fbx(
        filepath=filepath,
        use_selection=True,
        apply_scale_options='FBX_SCALE_ALL',
        mesh_smooth_type='FACE',
        use_mesh_modifiers=True,
        add_leaf_bones=False,
        bake_anim=False,
    )


def _export_combined_lp_fbx(groups, bakes_dir):
    """Export LP objects from all groups into a single combined FBX.

    Each group's LP objects keep their materials, so Painter sees one
    texture set per material.  Returns the output filepath on success,
    None on failure.
    """
    blend_name = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0]
    filepath = os.path.join(bakes_dir, f"{blend_name}_lp.fbx")

    temp_cols = []
    try:
        combined_col = bpy.data.collections.new("__mgbaker_combined_lp__")
        bpy.context.scene.collection.children.link(combined_col)

        for group in groups:
            if group.lp_collection is None:
                continue
            temp_col = _prepare_collection(group.lp_collection, group)
            temp_cols.append(temp_col)
            for obj in list(temp_col.objects):
                combined_col.objects.link(obj)

        if not combined_col.objects:
            return None

        _export_fbx(filepath, combined_col)
    except Exception as exc:
        print(f"[mgBaker] Combined LP FBX export failed: {exc}")
        filepath = None
    finally:
        # Unlink objects from combined col so they are owned by temp_cols
        for obj in list(combined_col.objects):
            combined_col.objects.unlink(obj)
        bpy.data.collections.remove(combined_col)
        for tc in temp_cols:
            _cleanup_temp_collection(tc)

    return filepath


def _export_group_fbx(group, suffix, collection):
    """Non-destructive FBX export for one collection of a group.

    Returns the output filepath on success, None on failure.
    """
    if collection is None:
        return None

    out_name = get_output_name(group)
    filepath = os.path.join(_bakes_dir(), f"{out_name}{suffix}.fbx")

    temp_col = None
    try:
        temp_col = _prepare_collection(collection, group)
        _export_fbx(filepath, temp_col)
    except Exception as exc:
        print(f"[mgBaker] FBX export failed for {filepath}: {exc}")
        return None
    finally:
        if temp_col is not None:
            _cleanup_temp_collection(temp_col)

    return filepath


# ── Per-group FBX export (_high/_low per group, one file each) ───────────

def _export_per_group_fbx(groups, bakes_dir):
    """Export one FBX per group, each containing ``<name>_high`` and
    ``<name>_low`` meshes.  Toolbag's quick loader auto-creates bake
    groups from this naming convention.

    Returns a list of ``(group, fbx_path)`` tuples for successfully
    exported groups.
    """
    results = []
    for group in groups:
        fbx_path = os.path.join(bakes_dir, f"{group.name}.fbx")

        temp_col = bpy.data.collections.new("__mgbaker_combined__")
        bpy.context.scene.collection.children.link(temp_col)
        ok = False
        try:
            if group.hp_collection:
                hp_temp = _prepare_collection(group.hp_collection, group)
                hp_objs = [o for o in hp_temp.objects if o.type == 'MESH']
                for i, obj in enumerate(hp_objs):
                    # All HP objects share the group name prefix so Toolbag's
                    # quick loader puts them all in the same bake group as
                    # {group.name}_low.  Using a numeric suffix keeps names
                    # unique while still matching the LP by base name.
                    obj.name = f"{group.name}_high_{i}"
                    obj.data.name = obj.name
                    for col in list(obj.users_collection):
                        col.objects.unlink(obj)
                    temp_col.objects.link(obj)
                _cleanup_temp_collection(hp_temp)

            if group.lp_collection:
                lp_temp = _prepare_collection(group.lp_collection, group)
                lp_objs = [o for o in lp_temp.objects if o.type == 'MESH']
                if lp_objs:
                    _join_to_single(lp_objs, f"{group.name}_low", temp_col)
                _cleanup_temp_collection(lp_temp)

            _export_fbx(fbx_path, temp_col)
            ok = True
        except Exception as exc:
            print(f"[mgBaker] FBX export failed for {group.name}: {exc}")
        finally:
            _cleanup_temp_collection(temp_col)

        if ok:
            results.append((group, fbx_path))

    return results


def _join_to_single(objects, name, dest_col):
    """Join *objects* into a single mesh named *name* inside *dest_col*.

    Objects are expected to already live in a temp collection (they'll be
    unlinked from their source).  The joined result is linked into
    *dest_col*.
    """
    if not objects:
        return None

    # Move all into dest_col so join has a common collection context
    for obj in objects:
        for col in list(obj.users_collection):
            col.objects.unlink(obj)
        dest_col.objects.link(obj)

    bpy.ops.object.select_all(action='DESELECT')
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]

    if len(objects) > 1:
        bpy.ops.object.join()

    joined = bpy.context.view_layer.objects.active
    joined.name = name
    joined.data.name = name
    return joined


# ── Toolbag script generation ─────────────────────────────────────────────

def _generate_toolbag_script(group_fbx_pairs, bakes_dir):
    """Generate a Toolbag 5 Python script.

    Creates one ``BakerObject`` per group in Multiple texture-set mode
    (tileMode=1).  ``outputPath`` is set to the bakes directory so Toolbag
    produces ``<bakes_dir>/<TextureSetName><suffix>.png`` — one file per
    material, named after the material/texture-set.

    Returns ``(script_path, tbscene_path)``.
    """
    tbscene_name = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0]
    tbscene_path = os.path.join(bakes_dir, f"{tbscene_name}.tbscene").replace("\\", "/")

    lines = [
        "import mset",
        "",
        "# Save .tbscene early so the file exists even if baker setup fails",
        f"mset.saveScene(r'{tbscene_path}')",
        "",
    ]

    for group, fbx_path in group_fbx_pairs:
        fbx_esc = fbx_path.replace("\\", "/")
        # outputPath must have a .png extension so Toolbag outputs PNG (not PSD).
        # With tileMode=1 Toolbag produces: <group.name>_<TextureSetName>_<suffix>.png
        output_path = os.path.join(bakes_dir, f"{group.name}.png").replace("\\", "/")

        enabled_maps = []
        for prop_name, tb_map_name, suffix in _MAP_DEFS:
            if getattr(group, prop_name, False):
                enabled_maps.append((tb_map_name, suffix))

        var = f"baker_{group.name}"
        lines += [
            f"# ── {group.name} ──",
            f"{var} = mset.BakerObject()",
            f"{var}.importModel(r'{fbx_esc}')",
            f"{var}.outputPath = r'{output_path}'",
            f"{var}.outputWidth = {group.res_x}",
            f"{var}.outputHeight = {group.res_y}",
            f"{var}.outputSamples = 4",
            f"{var}.outputBits = 8",
            f"{var}.edgePadding = 'Extreme'",
            f"{var}.tileMode = 1  # Multiple — one output file per texture set",
            "",
        ]

        for map_name, suffix in enabled_maps:
            tb_suffix = suffix.lstrip("_")  # Toolbag adds its own _ separator
            lines += [
                f"try:",
                f"    _m = {var}.getMap('{map_name}')",
                f"    _m.enabled = True",
                f"    _m.suffix = '{tb_suffix}'",
                f"except Exception as _e:",
                f"    print('[mgBaker] {group.name} map {map_name}:', _e)",
            ]
        lines.append("")

        # Cage offset — baker -> BakeGroup -> BakerTargetObject
        lines += [
            f"try:",
            f"    for _bg in {var}.getChildren():",
            f"        for _t in _bg.getChildren():",
            f"            _t.maxOffset = {round(group.cage_offset, 4)}",
            f"except Exception as _e:",
            f"    print('[mgBaker] {group.name} cage offset:', _e)",
            "",
        ]

    lines.append(f"mset.saveScene(r'{tbscene_path}')")

    script_content = "\n".join(lines)
    script_path = os.path.join(
        tempfile.gettempdir(),
        f"mgbaker_toolbag_{int(time.time())}.py",
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_content)

    print(f"[mgBaker] Toolbag script written to {script_path}")
    return script_path, tbscene_path


# ── Export to Toolbag ─────────────────────────────────────────────────────

class MG_OT_ExportToToolbag(bpy.types.Operator):
    bl_idname = "mg.export_to_toolbag"
    bl_label = "Export to Toolbag"
    bl_description = (
        "Export and launch Marmoset Toolbag | "
        "Shift: Delete mode | "
        "Ctrl+Shift: Open .tbscene folder"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath)

    def invoke(self, context, event):
        if event.shift and event.ctrl:
            bakes = _bakes_dir()
            os.startfile(bakes)
            return {'FINISHED'}
        if event.shift:
            context.window_manager.mg_baker_delete_mode = True
            for area in context.screen.areas:
                area.tag_redraw()
            return {'FINISHED'}
        return self.execute(context)

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({'ERROR'}, "Save the .blend file before exporting.")
            return {'CANCELLED'}

        groups = [g for g in context.scene.mg_export_groups if g.include]
        if not groups:
            self.report({'ERROR'}, "No groups are checked for export.")
            return {'CANCELLED'}

        prefs = _prefs()
        if prefs is None:
            self.report({'ERROR'}, "Cannot access addon preferences.")
            return {'CANCELLED'}

        bakes = _bakes_dir()
        log_lines = []

        toolbag_exe = prefs.toolbag_exe
        if not os.path.isfile(toolbag_exe):
            self.report({'WARNING'}, f"Toolbag not found at {toolbag_exe}")
            return {'FINISHED'}

        # Always re-export per-group FBX files so the .tbscene reflects
        # the current mesh state.
        group_fbx_pairs = _export_per_group_fbx(groups, bakes)
        if not group_fbx_pairs:
            self.report({'ERROR'}, "All FBX exports failed.")
            return {'CANCELLED'}
        for _, fbx_path in group_fbx_pairs:
            log_lines.append(f"✓ {os.path.basename(fbx_path)} exported")

        script_path, tbscene_out = _generate_toolbag_script(group_fbx_pairs, bakes)

        if prefs.launch_app_after_export:
            exe_stem = os.path.splitext(os.path.basename(toolbag_exe))[0]  # "toolbag"
            try:
                result = subprocess.run(
                    [
                        "powershell", "-NoProfile", "-Command",
                        f"if (Get-Process -Name '{exe_stem}' -ErrorAction SilentlyContinue) {{ 'yes' }} else {{ 'no' }}",
                    ],
                    capture_output=True, text=True, timeout=5,
                )
                tb_running = result.stdout.strip().lower() == "yes"
            except Exception:
                tb_running = False

            if tb_running:
                log_lines.append("⚠ Toolbag open — FBX updated on disk (auto-reloads). Close Toolbag and re-export to update baker settings.")
            else:
                os.system(f'start "" "{toolbag_exe}" "{script_path}"')
                log_lines.append("✓ Toolbag launched")

                # Delayed P4 checkout for the .tbscene
                p4.delayed_checkout_tbscene(tbscene_out, p4.get_cl_description())

                # Clean up script after 30s
                def _delayed_script_cleanup():
                    try:
                        if os.path.isfile(script_path):
                            os.remove(script_path)
                    except Exception:
                        pass
                    return None
                bpy.app.timers.register(_delayed_script_cleanup, first_interval=30.0)

        for line in log_lines:
            print(f"[mgBaker] {line}")
        _store_log(context, log_lines)

        if any(line.startswith("⚠") for line in log_lines):
            self.report({'WARNING'}, "Toolbag is already open. Mesh will auto-reload. Close Toolbag and re-export if you changed bake settings.")
        else:
            self.report({'INFO'}, f"Exported {len(groups)} group(s) to Toolbag")
        return {'FINISHED'}


# ── Export to Painter ─────────────────────────────────────────────────────

class MG_OT_ExportToPainter(bpy.types.Operator):
    bl_idname = "mg.export_to_painter"
    bl_label = "Export to Painter"
    bl_description = (
        "Export and launch Substance Painter | "
        "Shift: delete mode | "
        "Ctrl+Shift: Open .spp folder"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath)

    def invoke(self, context, event):
        if event.shift and event.ctrl:
            blend_dir = os.path.dirname(bpy.data.filepath)
            textures_dir = os.path.join(blend_dir, "textures")
            folder = textures_dir if os.path.isdir(textures_dir) else blend_dir
            os.startfile(folder)
            return {'FINISHED'}
        if event.shift:
            context.window_manager.mg_baker_delete_mode = True
            for area in context.screen.areas:
                area.tag_redraw()
            return {'FINISHED'}
        return self.execute(context)

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({'ERROR'}, "Save the .blend file before exporting.")
            return {'CANCELLED'}

        groups = [g for g in context.scene.mg_export_groups if g.include]
        if not groups:
            self.report({'ERROR'}, "No groups are checked for export.")
            return {'CANCELLED'}

        prefs = _prefs()
        if prefs is None:
            self.report({'ERROR'}, "Cannot access addon preferences.")
            return {'CANCELLED'}

        bakes = _bakes_dir()
        blend_dir = os.path.dirname(bpy.data.filepath)
        blend_name = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0]
        log_lines = []

        plugin_log = _ensure_painter_plugin()
        if plugin_log:
            log_lines.append(plugin_log)

        # Export combined LP FBX (all groups into one file so Painter sees all texture sets)
        lp_fbx = _export_combined_lp_fbx(groups, bakes)
        if lp_fbx:
            log_lines.append(f"✓ {os.path.basename(lp_fbx)} exported")
        else:
            self.report({'ERROR'}, "Combined LP FBX export failed.")
            return {'CANCELLED'}

        # Export HP FBX per group
        hp_fbx_by_group = {}
        for g in groups:
            hp_path = _export_group_fbx(g, "_hp", g.hp_collection)
            if hp_path:
                hp_fbx_by_group[g.name] = hp_path
                log_lines.append(f"✓ {os.path.basename(hp_path)} exported")

        # Copy .spp template if output doesn't exist
        textures_dir = os.path.join(blend_dir, "textures")
        os.makedirs(textures_dir, exist_ok=True)
        spp_out_path = os.path.join(textures_dir, f"{blend_name}.spp")

        if not os.path.isfile(spp_out_path):
            template_path = prefs.spp_template
            if not template_path or not os.path.isfile(template_path):
                # Try bundled template
                template_path = os.path.join(os.path.dirname(__file__), "templates", "defaultProject.spp")
            if os.path.isfile(template_path):
                shutil.copy2(template_path, spp_out_path)
                p4.p4_checkout(spp_out_path, p4.get_cl_description())
                log_lines.append(f"✓ .spp copied to {os.path.basename(spp_out_path)}")
            else:
                self.report({'WARNING'}, "No .spp template found — Painter will create a blank project")

        # Build relay config JSON — one entry per group so the Painter plugin
        # can configure each texture set independently.
        groups_config = []
        for g in groups:
            enabled_maps = []
            for prop_name, _tb_name, suffix in _MAP_DEFS:
                if getattr(g, prop_name, False):
                    enabled_maps.append(suffix.lstrip("_"))
            groups_config.append({
                "mat_name": get_output_name(g),
                "hp_fbx": hp_fbx_by_group.get(g.name, ""),
                "output_width": int(g.res_x),
                "output_height": int(g.res_y),
                "cage_offset": g.cage_offset,
                "maps": enabled_maps,
            })

        config = {
            "lp_fbx": lp_fbx,
            "groups": groups_config,
            "bakes_dir": bakes,
            "spp_out_path": spp_out_path,
        }

        config_path = os.path.join(bakes, "mgbaker_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        log_lines.append("✓ mgbaker_config.json written")

        # Launch Painter
        painter_exe = prefs.painter_exe
        if not os.path.isfile(painter_exe):
            self.report({'WARNING'}, f"Painter not found at {painter_exe}")
        elif prefs.launch_app_after_export:
            # Check if Painter is already running.
            # tasklist truncates process names >25 chars, so use PowerShell Get-Process.
            try:
                proc_name = os.path.splitext(os.path.basename(painter_exe))[0]
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f'Get-Process -ErrorAction SilentlyContinue | Where-Object {{ $_.Name -eq "{proc_name}" }}'],
                    capture_output=True, text=True, timeout=5,
                )
                already_running = bool(result.stdout.strip())
            except Exception:
                already_running = False

            if already_running:
                # Painter is open — just write the config; the plugin watcher will reload
                log_lines.append("✓ Painter running — mesh will reload automatically")
            else:
                cmd = [painter_exe, spp_out_path, "--mesh", lp_fbx]
                subprocess.Popen(cmd)
                log_lines.append("✓ Painter launched")

        for line in log_lines:
            print(f"[mgBaker] {line}")
        _store_log(context, log_lines)

        self.report({'INFO'}, f"Exported {len(groups)} group(s) to Painter")
        return {'FINISHED'}


# ── Delete source files helpers ───────────────────────────────────────────

def _collect_toolbag_files(context) -> list:
    """Return existing Toolbag source files (per-group FBX + .tbscene)."""
    if not bpy.data.filepath:
        return []
    bakes = _bakes_dir()
    blend_name = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0]
    candidates = [os.path.join(bakes, f"{g.name}.fbx")
                  for g in context.scene.mg_export_groups]
    candidates.append(os.path.join(bakes, f"{blend_name}.tbscene"))
    return [p for p in candidates if os.path.isfile(p)]


def _collect_painter_files(context) -> list:
    """Return existing Painter source files (combined LP FBX + per-group HP FBX)."""
    if not bpy.data.filepath:
        return []
    bakes = _bakes_dir()
    blend_name = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0]
    candidates = [os.path.join(bakes, f"{blend_name}_lp.fbx")]
    for g in context.scene.mg_export_groups:
        candidates.append(os.path.join(bakes, f"{get_output_name(g)}_hp.fbx"))
    return [p for p in candidates if os.path.isfile(p)]


def _exit_delete_mode(context) -> None:
    context.window_manager.mg_baker_delete_mode = False
    for area in context.screen.areas:
        area.tag_redraw()


# ── Delete Toolbag Source Files ───────────────────────────────────────────

class MG_OT_DeleteToolbagFiles(bpy.types.Operator):
    bl_idname = "mg.delete_toolbag_files"
    bl_label = "Delete Toolbag Source Files"
    bl_description = "Delete Toolbag FBX and .tbscene files from disk and revert them from Perforce. Shift+Click to cancel"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath)

    def invoke(self, context, event):
        if event.shift:
            _exit_delete_mode(context)
            return {'FINISHED'}
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        files = _collect_toolbag_files(context)
        deleted = []
        for f in files:
            p4.p4_revert(f)
            try:
                os.remove(f)
                deleted.append(os.path.basename(f))
            except Exception as exc:
                print(f"[mgBaker] Delete failed for {f}: {exc}")
        p4.p4_delete_cl_if_empty(p4.get_cl_description())
        _exit_delete_mode(context)
        if deleted:
            self.report({'INFO'}, f"Deleted: {', '.join(deleted)}")
        else:
            self.report({'WARNING'}, "No Toolbag source files found to delete.")
        return {'FINISHED'}


# ── Delete Painter Source Files ───────────────────────────────────────────

class MG_OT_DeletePainterFiles(bpy.types.Operator):
    bl_idname = "mg.delete_painter_files"
    bl_label = "Delete Painter Source Files"
    bl_description = "Delete Painter LP/HP FBX files from disk and revert them from Perforce. Shift+Click to cancel"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath)

    def invoke(self, context, event):
        if event.shift:
            _exit_delete_mode(context)
            return {'FINISHED'}
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        files = _collect_painter_files(context)
        deleted = []
        for f in files:
            p4.p4_revert(f)
            try:
                os.remove(f)
                deleted.append(os.path.basename(f))
            except Exception as exc:
                print(f"[mgBaker] Delete failed for {f}: {exc}")
        p4.p4_delete_cl_if_empty(p4.get_cl_description())
        _exit_delete_mode(context)
        if deleted:
            self.report({'INFO'}, f"Deleted: {', '.join(deleted)}")
        else:
            self.report({'WARNING'}, "No Painter source files found to delete.")
        return {'FINISHED'}


# ── Export FBX Only ───────────────────────────────────────────────────────

class MG_OT_ExportFBXOnly(bpy.types.Operator):
    bl_idname = "mg.export_fbx_only"
    bl_label = "Export FBX Only"
    bl_description = "Export checked groups as FBX without launching any external application"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath)

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({'ERROR'}, "Save the .blend file before exporting.")
            return {'CANCELLED'}

        groups = [g for g in context.scene.mg_export_groups if g.include]
        if not groups:
            self.report({'ERROR'}, "No groups are checked for export.")
            return {'CANCELLED'}

        count = 0
        log_lines = []
        for g in groups:
            hp_path = _export_group_fbx(g, "_hp", g.hp_collection)
            if hp_path:
                count += 1
                log_lines.append(f"\u2713 {os.path.basename(hp_path)} exported")
            lp_path = _export_group_fbx(g, "_lp", g.lp_collection)
            if lp_path:
                count += 1
                log_lines.append(f"\u2713 {os.path.basename(lp_path)} exported")

        _store_log(context, log_lines)
        self.report({'INFO'}, f"Exported {count} FBX file(s)")
        return {'FINISHED'}


class MG_OT_OpenBakesFolder(bpy.types.Operator):
    bl_idname = "mg.open_bakes_folder"
    bl_label = "Open Bakes Folder"
    bl_description = "Open the bakes output folder in Explorer"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath)

    def execute(self, context):
        folder = _bakes_dir()
        os.startfile(folder)
        return {'FINISHED'}


class MG_OT_OpenTexturesFolder(bpy.types.Operator):
    bl_idname = "mg.open_textures_folder"
    bl_label = "Open Textures Folder"
    bl_description = "Open the textures folder (.spp output) in Explorer"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath)

    def execute(self, context):
        blend_dir = os.path.dirname(bpy.data.filepath)
        folder = os.path.join(blend_dir, "textures")
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)
        return {'FINISHED'}
