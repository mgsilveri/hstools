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

from .baker_props import get_output_name, get_active_project, get_project_output_name
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
    ("bake_id",           "Object ID",          "_id"),
    ("bake_thickness",    "Thickness",           "_thickness"),
    ("bake_position",     "Position",            "_position"),

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


def _check_groups_complete(groups):
    """Return a list of error strings for groups missing HP or LP collections."""
    errors = []
    for g in groups:
        if g.hp_collection is None and g.lp_collection is None:
            errors.append(f"'{g.name}': missing HP and LP")
        elif g.hp_collection is None:
            errors.append(f"'{g.name}': missing HP")
        elif g.lp_collection is None:
            errors.append(f"'{g.name}': missing LP")
    return errors


def _bakes_dir() -> str:
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


def _apply_uv_u_offset(obj, offset_u: float):
    """Shift all UV loops on *obj* by *offset_u* in the U direction."""
    for uv_layer in obj.data.uv_layers:
        for loop_data in uv_layer.data:
            loop_data.uv.x += offset_u


def _offset_overlapping_uv_islands(objects):
    """Assign unique U-offset tiles to identical UV islands across a list of objects.

    Replaces both the per-object intra-array dedup and the cross-object linked-
    duplicate shift.  A single coordinated pass avoids the tile collision that
    occurs when both mechanisms independently choose U+1.

    Algorithm:
      1. Union-find within each object to identify UV islands.
      2. Compute centroid (rounded to 2 dp) for each island as its signature.
      3. Group all islands from all objects by centroid.
      4. First occurrence of each centroid stays at U+0; duplicates get U+1, U+2, …

    Handles:
      - Geo-nodes / classic Array objects (intra-object stacking)
      - Linked duplicates (cross-object stacking)
      - Any combination of both
    """
    import bmesh as bmesh_mod

    # ── Phase 1: collect islands from every object ────────────────────────
    # bm_islands: list of (obj, bm, uv_layer, [face_indices])
    bm_islands = []
    centroid_entries = []  # parallel list: centroid key for bm_islands[i]

    for obj in objects:
        if obj.type != 'MESH':
            continue
        bm = bmesh_mod.new()
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            bm.free()
            continue

        n_faces = len(bm.faces)
        parent = list(range(n_faces))

        # Union-find with default-arg capture so each loop iteration is isolated
        def _find(x, p=parent):
            while p[x] != x:
                p[x] = p[p[x]]
                x = p[x]
            return x

        for f in bm.faces:
            for loop in f.loops:
                for link in loop.edge.link_loops:
                    if link == loop:
                        continue
                    if link.vert == loop.vert:
                        same = (
                            (loop[uv_layer].uv - link[uv_layer].uv).length < 1e-6
                            and (loop.link_loop_next[uv_layer].uv
                                 - link.link_loop_next[uv_layer].uv).length < 1e-6
                        )
                    else:
                        same = (
                            (loop[uv_layer].uv - link.link_loop_next[uv_layer].uv).length < 1e-6
                            and (loop.link_loop_next[uv_layer].uv
                                 - link[uv_layer].uv).length < 1e-6
                        )
                    if same:
                        ra, rb = _find(f.index), _find(link.face.index)
                        if ra != rb:
                            parent[ra] = rb

        island_map: dict = {}
        for f in bm.faces:
            island_map.setdefault(_find(f.index), []).append(f.index)

        for face_idxs in island_map.values():
            sx = sy = n = 0.0
            for fi in face_idxs:
                for lp in bm.faces[fi].loops:
                    sx += lp[uv_layer].uv.x
                    sy += lp[uv_layer].uv.y
                    n += 1.0
            key = (round(sx / n, 2), round(sy / n, 2)) if n else (0.0, 0.0)
            centroid_entries.append(key)
            bm_islands.append((obj, bm, uv_layer, face_idxs))

    # ── Phase 2: group by centroid, assign unique tiles ───────────────────
    groups: dict = {}
    for i, key in enumerate(centroid_entries):
        groups.setdefault(key, []).append(i)

    shift_total = 0
    for idxs in groups.values():
        for tile, island_idx in enumerate(idxs):
            if tile == 0:
                continue
            _, bm, uv_layer, face_idxs = bm_islands[island_idx]
            for fi in face_idxs:
                for lp in bm.faces[fi].loops:
                    lp[uv_layer].uv.x += 1.0
            shift_total += 1

    # ── Phase 3: write back (once per bmesh, not per island) ─────────────
    written: set = set()
    for obj, bm, _uv, _ in bm_islands:
        bid = id(bm)
        if bid not in written:
            written.add(bid)
            bm.to_mesh(obj.data)
            bm.free()
            obj.data.update()

    print(f"[mgBaker] UV island offset: {len(bm_islands)} islands across "
          f"{len(written)} objects — {shift_total} shifted")


def _prepare_collection(source_col, group, offset_instance_uvs=False):
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

    # Single global pass: handles both intra-object (geo-nodes array) and
    # cross-object (linked duplicate) stacking in one coordinated assignment.
    if offset_instance_uvs:
        _offset_overlapping_uv_islands(list(temp_col.objects))

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


def _export_combined_lp_fbx(groups, bakes_dir, out_name):
    """Export LP objects from all groups into a single combined FBX.

    Each group's LP objects keep their materials, so Painter sees one
    texture set per material.  Returns the output filepath on success,
    None on failure.
    """
    filepath = os.path.join(bakes_dir, f"{out_name}_lp.fbx")

    temp_cols = []
    try:
        combined_col = bpy.data.collections.new("__mgbaker_combined_lp__")
        bpy.context.scene.collection.children.link(combined_col)

        for group in groups:
            if group.lp_collection is None:
                continue
            temp_col = _prepare_collection(
                group.lp_collection, group,
                offset_instance_uvs=group.instance_uv_offset,
            )
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
                lp_temp = _prepare_collection(
                    group.lp_collection, group,
                    offset_instance_uvs=group.instance_uv_offset,
                )
                lp_objs = [o for o in lp_temp.objects if o.type == 'MESH']
                for i, obj in enumerate(lp_objs):
                    obj.name = f"{group.name}_low_{i}"
                    obj.data.name = obj.name
                    for col in list(obj.users_collection):
                        col.objects.unlink(obj)
                    temp_col.objects.link(obj)
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


# ── Toolbag script generation ─────────────────────────────────────────────

def _generate_toolbag_script(group_fbx_pairs, bakes_dir, out_name):
    """Generate a Toolbag 5 Python script.

    Creates one ``BakerObject`` per group in Multiple texture-set mode
    (tileMode=1).  ``outputPath`` is set to the bakes directory so Toolbag
    produces ``<bakes_dir>/<TextureSetName><suffix>.png`` — one file per
    material, named after the material/texture-set.

    Returns ``(script_path, tbscene_path)``.
    """
    tbscene_path = os.path.join(bakes_dir, f"{out_name}.tbscene").replace("\\", "/")

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
            f"{var}.outputSamples = 32",
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
            ]
            if map_name == "Ambient Occlusion":
                lines.append(f"    _m.rayCount = 4096")
            lines += [
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

        proj = get_active_project(context.scene)
        if proj is None:
            self.report({'ERROR'}, "No active project.")
            return {'CANCELLED'}
        groups = [g for g in proj.groups if g.include]
        if not groups:
            self.report({'ERROR'}, "No groups are checked for export.")
            return {'CANCELLED'}

        incomplete = _check_groups_complete(groups)
        if incomplete:
            self.report({'ERROR'}, "Incomplete groups: " + "; ".join(incomplete))
            return {'CANCELLED'}

        prefs = _prefs()
        if prefs is None:
            self.report({'ERROR'}, "Cannot access addon preferences.")
            return {'CANCELLED'}

        bakes = _bakes_dir()
        out_name = get_project_output_name(proj, context.scene)
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

        script_path, tbscene_out = _generate_toolbag_script(group_fbx_pairs, bakes, out_name)

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

        proj = get_active_project(context.scene)
        if proj is None:
            self.report({'ERROR'}, "No active project.")
            return {'CANCELLED'}
        groups = [g for g in proj.groups if g.include]
        if not groups:
            self.report({'ERROR'}, "No groups are checked for export.")
            return {'CANCELLED'}

        incomplete = _check_groups_complete(groups)
        if incomplete:
            self.report({'ERROR'}, "Incomplete groups: " + "; ".join(incomplete))
            return {'CANCELLED'}

        prefs = _prefs()
        if prefs is None:
            self.report({'ERROR'}, "Cannot access addon preferences.")
            return {'CANCELLED'}

        bakes = _bakes_dir()
        blend_dir = os.path.dirname(bpy.data.filepath)
        out_name = get_project_output_name(proj, context.scene)
        log_lines = []

        plugin_log = _ensure_painter_plugin()
        if plugin_log:
            log_lines.append(plugin_log)

        # Export combined LP FBX (all groups into one file so Painter sees all texture sets)
        lp_fbx = _export_combined_lp_fbx(groups, bakes, out_name)
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
        spp_out_path = os.path.join(textures_dir, f"{out_name}.spp")

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

        _store_log(context, log_lines)

        self.report({'INFO'}, f"Exported {len(groups)} group(s) to Painter")
        return {'FINISHED'}


# ── Delete source files helpers ───────────────────────────────────────────

def _collect_toolbag_files(context) -> list:
    """Return existing Toolbag source files for the active project (FBX + .tbscene)."""
    if not bpy.data.filepath:
        return []
    proj = get_active_project(context.scene)
    if proj is None:
        return []
    bakes = _bakes_dir()
    out_name = get_project_output_name(proj, context.scene)
    candidates = [os.path.join(bakes, f"{g.name}.fbx") for g in proj.groups]
    candidates.append(os.path.join(bakes, f"{out_name}.tbscene"))
    return [p for p in candidates if os.path.isfile(p)]


def _collect_painter_files(context) -> list:
    """Return existing Painter source files for the active project (LP FBX + HP FBX + .spp)."""
    if not bpy.data.filepath:
        return []
    proj = get_active_project(context.scene)
    if proj is None:
        return []
    bakes = _bakes_dir()
    out_name = get_project_output_name(proj, context.scene)
    blend_dir = os.path.dirname(bpy.data.filepath)
    candidates = [
        os.path.join(bakes, f"{out_name}_lp.fbx"),
        os.path.join(blend_dir, "textures", f"{out_name}.spp"),
    ]
    for g in proj.groups:
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

        proj = get_active_project(context.scene)
        if proj is None:
            self.report({'ERROR'}, "No active project.")
            return {'CANCELLED'}
        groups = [g for g in proj.groups if g.include]
        if not groups:
            self.report({'ERROR'}, "No groups are checked for export.")
            return {'CANCELLED'}

        incomplete = _check_groups_complete(groups)
        if incomplete:
            self.report({'ERROR'}, "Incomplete groups: " + "; ".join(incomplete))
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
