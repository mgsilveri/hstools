"""
mgBaker – N-panel UI.

Layout follows mockup v5: Export Groups, Group Settings (active group),
Export, Preferences (collapsed), Log (collapsed).
"""

from __future__ import annotations

import bpy

from .baker_props import _group_status, get_active_project


def _fit(text, context, prefix="", reserved_px=36):
    """Truncate *text* so that *prefix* + *text* fits the N-panel header.

    *reserved_px* covers the fold arrow and surrounding chrome (~36 px).
    Blender's default UI font is roughly 7 px per character at scale 1.
    """
    scale = getattr(getattr(context, 'preferences', None) and context.preferences.system, 'ui_scale', 1.0)
    region_px = context.region.width if (context.region and context.region.width) else 200
    total_avail = max(4, int((region_px - reserved_px) / (7 * scale)))
    text_avail = max(4, total_avail - len(prefix))
    if len(text) <= text_avail:
        return text
    return text[:max(1, text_avail - 1)] + "\u2026"


def _get_lp_materials(group):
    """Return ordered unique materials from all LP mesh objects in *group*.

    Traverses the LP collection and all nested child collections.
    Returns a list of ``bpy.types.Material`` (no duplicates, order of first
    encounter preserved).
    """
    if group.lp_collection is None:
        return []

    seen = set()
    materials = []

    def _collect(col):
        for obj in col.objects:
            if obj.type != 'MESH':
                continue
            for slot in obj.material_slots:
                mat = slot.material
                if mat is not None and mat.name not in seen:
                    seen.add(mat.name)
                    materials.append(mat)
        for child in col.children:
            _collect(child)

    _collect(group.lp_collection)
    return materials


def _has_empty_material_slots(group):
    """Return True if any LP mesh object has an empty material slot."""
    if group.lp_collection is None:
        return False

    def _check(col):
        for obj in col.objects:
            if obj.type != 'MESH':
                continue
            for slot in obj.material_slots:
                if slot.material is None:
                    return True
        for child in col.children:
            if _check(child):
                return True
        return False

    return _check(group.lp_collection)


# ── Main panel (tab) ─────────────────────────────────────────────────────

class MG_PT_Baker(bpy.types.Panel):
    bl_label = "mgBaker"
    bl_idname = "MG_PT_Baker"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "mgBaker"

    def draw(self, context):
        pass  # sub-panels only


# ── Export Groups ─────────────────────────────────────────────────────────

class MG_PT_ExportGroups(bpy.types.Panel):
    bl_label = ""
    bl_idname = "MG_PT_ExportGroups"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "mgBaker"
    bl_parent_id = "MG_PT_Baker"

    def draw_header(self, context):
        import os
        proj = get_active_project(context.scene)
        multi = len(context.scene.mg_projects) > 1
        if proj is not None:
            if multi:
                raw = proj.name
            else:
                raw = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0] if bpy.data.filepath else proj.name
            label = _fit(raw, context, prefix="Baker Setup — ")
            self.layout.label(text=f"Baker Setup — {label}")
        else:
            self.layout.label(text="Baker Setup")

    def draw(self, context):
        layout = self.layout
        scn = context.scene
        projects = scn.mg_projects
        multi = len(projects) > 1

        proj = get_active_project(scn)

        if proj is None:
            row = layout.row(align=True)
            row.label(text="No project yet", icon='INFO')
            row.operator("mg.add_project", icon='ADD', text="Add Project")
            return

        # ── Projects list (only shown when more than one exists) ──────────
        if multi:
            _pname = _fit(proj.name, context, prefix="Projects  —  ")
            row = layout.row(align=True)
            row.scale_y = 0.75
            row.label(text=f"Projects  \u2014  {_pname}", icon='FILE_3D')

            row = layout.row()
            row.template_list(
                "MG_UL_Projects", "",
                scn, "mg_projects",
                scn, "mg_active_project_index",
                rows=2,
            )
            col = row.column(align=True)
            col.operator("mg.add_project", icon='ADD', text="")
            col.operator("mg.remove_project", icon='REMOVE', text="")
            col.separator()
            op_up = col.operator("mg.move_project", icon='TRIA_UP', text="")
            op_up.direction = 'UP'
            op_down = col.operator("mg.move_project", icon='TRIA_DOWN', text="")
            op_down.direction = 'DOWN'

        # ── Groups bridge label ───────────────────────────────────────────
        # A tight label row that names which project's groups are shown below.
        if multi:
            _gname = _fit(proj.name, context, prefix="Groups  —  ")
            row = layout.row(align=True)
            row.scale_y = 0.75
            row.label(text=f"Groups  \u2014  {_gname}", icon='OBJECT_DATAMODE')

        # ── Groups list ───────────────────────────────────────────────────
        row = layout.row()
        row.template_list(
            "MG_UL_ExportGroups", "",
            proj, "groups",
            proj, "active_group_index",
            rows=3,
        )
        col = row.column(align=True)
        if not multi:
            col.operator("mg.add_project", text="", icon='FILE_NEW')
            col.separator()
        col.operator("mg.add_export_group", icon='ADD', text="")
        col.operator("mg.remove_export_group", icon='REMOVE', text="")
        col.separator()
        op_up = col.operator("mg.move_export_group", icon='TRIA_UP', text="")
        op_up.direction = 'UP'
        op_down = col.operator("mg.move_export_group", icon='TRIA_DOWN', text="")
        op_down.direction = 'DOWN'

        # Legend
        row = layout.row(align=True)
        row.scale_y = 0.7
        sub = row.row(align=True)
        sub.label(text="", icon='CHECKMARK')
        sub.label(text="Ready")
        sub = row.row(align=True)
        sub.label(text="", icon='ERROR')
        sub.label(text="Incomplete")


# ── Group Settings ────────────────────────────────────────────────────────

class MG_PT_GroupSettings(bpy.types.Panel):
    bl_label = ""
    bl_idname = "MG_PT_GroupSettings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "mgBaker"
    bl_parent_id = "MG_PT_Baker"

    @classmethod
    def poll(cls, context):
        proj = get_active_project(context.scene)
        return (
            proj is not None
            and len(proj.groups) > 0
            and 0 <= proj.active_group_index < len(proj.groups)
        )

    def draw_header(self, context):
        proj = get_active_project(context.scene)
        grp = proj.groups[proj.active_group_index]
        name = _fit(grp.name, context, prefix="Group Settings — ")
        self.layout.label(text=f"Group Settings — {name}")

    def draw(self, context):
        layout = self.layout
        scn = context.scene
        proj = get_active_project(scn)
        grp = proj.groups[proj.active_group_index]

        # ── Collections ──
        layout.label(text="Collections", icon='OUTLINER_COLLECTION')
        box = layout.box()
        col = box.column(align=True)
        split = col.split(factor=0.12, align=True)
        split.label(text="HP:")
        split.prop_search(grp, "hp_collection", bpy.data, "collections", text="")
        split = col.split(factor=0.12, align=True)
        split.label(text="LP:")
        split.prop_search(grp, "lp_collection", bpy.data, "collections", text="")

        # Visibility toggles — affect all groups
        row = box.row(align=True)
        row.prop(scn, "mg_hp_hidden", text="Hide HP", toggle=True,
                 icon='HIDE_ON' if scn.mg_hp_hidden else 'HIDE_OFF')
        row.prop(scn, "mg_lp_hidden", text="Hide LP", toggle=True,
                 icon='HIDE_ON' if scn.mg_lp_hidden else 'HIDE_OFF')

        layout.separator(type='LINE')

        # ── Materials ──
        mats = _get_lp_materials(grp)
        layout.label(text="Materials", icon='MATERIAL')
        box = layout.box()
        if mats:
            if _has_empty_material_slots(grp):
                box.label(text="Empty material slot(s) detected", icon='ERROR')
            col = box.column(align=True)
            for i, mat in enumerate(mats, 1):
                row = col.row(align=True)
                has_problem = not mat.name or any(
                    c in mat.name for c in (' ', '\t', '/', '\\', ':', '*', '?', '"', '<', '>', '|')
                )
                row.alert = has_problem
                split = row.split(factor=0.08, align=True)
                split.label(text=f"{i}:")
                split.prop(mat, "name", text="", icon='ERROR' if has_problem else 'MATERIAL')
        else:
            box.label(text="No LP collection assigned", icon='INFO')

        layout.separator(type='LINE')

        # ── Cage ──
        layout.label(text="Cage", icon='MOD_LATTICE')
        layout.prop(grp, "cage_offset", text="Offset")

        layout.separator(type='LINE')

        # ── Bake Resolution ──
        layout.label(text="Bake Resolution", icon='IMAGE_DATA')
        col = layout.column(align=True)
        split = col.split(factor=0.05, align=True)
        split.label(text="X")
        split.row(align=True).prop(grp, "res_x", expand=True)
        split = col.split(factor=0.05, align=True)
        split.label(text="Y")
        split.row(align=True).prop(grp, "res_y", expand=True)

        layout.separator(type='LINE')

        # ── Bake Maps ──
        layout.label(text="Bake Maps", icon='RENDERLAYERS')
        flow = layout.grid_flow(row_major=True, columns=2, even_columns=True, even_rows=True, align=True)
        flow.prop(grp, "bake_normal", text="Normal")
        flow.prop(grp, "bake_ao", text="AO")
        flow.prop(grp, "bake_curvature", text="Curvature")
        flow.prop(grp, "bake_world_normal", text="World Normal")
        flow.prop(grp, "bake_id", text="Object ID")
        flow.prop(grp, "bake_thickness", text="Thickness")
        flow.prop(grp, "bake_position", text="Position")
        flow.prop(grp, "bake_opacity", text="Opacity")
        flow.prop(grp, "bake_height", text="Height")

        layout.separator(type='LINE')

        # ── Export Options ──
        layout.label(text="Export Options", icon='EXPORT')
        flow = layout.grid_flow(row_major=True, columns=2, even_columns=True, even_rows=True, align=True)
        flow.prop(grp, "apply_modifiers")
        flow.prop(grp, "triangulate")
        flow.prop(grp, "smooth_by_uv")
        flow.prop(grp, "export_at_origin")
        flow.prop(grp, "instance_uv_offset")


# ── Export ────────────────────────────────────────────────────────────────

class MG_PT_Export(bpy.types.Panel):
    bl_label = ""
    bl_idname = "MG_PT_Export"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "mgBaker"
    bl_parent_id = "MG_PT_Baker"

    def draw_header(self, context):
        proj = get_active_project(context.scene)
        n = sum(1 for g in proj.groups if g.include) if proj is not None else 0
        self.layout.label(text=f"Export — {n} group{'s' if n != 1 else ''}")

    def draw(self, context):
        from . import get_icon
        layout = self.layout
        wm = context.window_manager
        delete_mode = wm.mg_baker_delete_mode

        col = layout.column(align=True)
        col.scale_y = 1.2
        if delete_mode:
            col.alert = True
            col.operator("mg.delete_toolbag_files", icon='TRASH')
            col.operator("mg.delete_painter_files", icon='TRASH')
            col.alert = False
        else:
            icon_tb = get_icon("toolbag")
            icon_pa = get_icon("painter")
            col.operator("mg.export_to_toolbag", icon_value=icon_tb if icon_tb else 0,
                         icon='SHADING_RENDERED' if not icon_tb else 'NONE')
            col.operator("mg.export_to_painter", icon_value=icon_pa if icon_pa else 0,
                         icon='BRUSH_DATA' if not icon_pa else 'NONE')

        layout.separator()
        col = layout.column(align=True)
        col.operator("mg.export_fbx_only", icon='EXPORT')
        col.operator("mg.open_bakes_folder", icon='FILE_FOLDER')
        col.operator("mg.open_textures_folder", icon='FILE_FOLDER')


# ── Preferences (inline) ─────────────────────────────────────────────────

class MG_PT_PanelPrefs(bpy.types.Panel):
    bl_label = "Preferences"
    bl_idname = "MG_PT_PanelPrefs"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "mgBaker"
    bl_parent_id = "MG_PT_Baker"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        addon = context.preferences.addons.get(__package__)
        if addon is None:
            layout.label(text="Addon preferences not available", icon='ERROR')
            return
        prefs = addon.preferences

        layout.label(text="Executables", icon='FILE_BLANK')
        col = layout.column(align=True)
        col.prop(prefs, "toolbag_exe", text="Toolbag")
        col.prop(prefs, "painter_exe", text="Painter")
        col.prop(prefs, "spp_template", text=".spp")

        layout.separator()
        layout.prop(prefs, "p4_auto_checkout")
        layout.prop(prefs, "launch_app_after_export")


# ── Log ───────────────────────────────────────────────────────────────────

class MG_PT_Log(bpy.types.Panel):
    bl_label = "Log"
    bl_idname = "MG_PT_Log"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "mgBaker"
    bl_parent_id = "MG_PT_Baker"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        log = context.scene.mg_export_log
        if not log:
            layout.label(text="No export log yet.", icon='INFO')
            return
        layout.template_list(
            "MG_UL_LogList", "",
            context.scene, "mg_export_log",
            context.scene, "mg_export_log_index",
            rows=6,
        )
        layout.operator("mg.copy_log", icon='COPYDOWN')
