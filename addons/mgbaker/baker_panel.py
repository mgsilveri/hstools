"""
mgBaker – N-panel UI.

Layout follows mockup v5: Export Groups, Group Settings (active group),
Export, Preferences (collapsed), Log (collapsed).
"""

from __future__ import annotations

import bpy

from .baker_props import _group_status


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
    bl_label = "Export Groups"
    bl_idname = "MG_PT_ExportGroups"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "mgBaker"
    bl_parent_id = "MG_PT_Baker"

    def draw(self, context):
        layout = self.layout
        scn = context.scene

        row = layout.row()
        row.template_list(
            "MG_UL_ExportGroups", "",
            scn, "mg_export_groups",
            scn, "mg_active_group_index",
            rows=3,
        )

        col = row.column(align=True)
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
        sub = row.row(align=True)
        sub.label(text="", icon='CANCEL')
        sub.label(text="Broken")


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
        scn = context.scene
        return (
            len(scn.mg_export_groups) > 0
            and 0 <= scn.mg_active_group_index < len(scn.mg_export_groups)
        )

    def draw_header(self, context):
        grp = context.scene.mg_export_groups[context.scene.mg_active_group_index]
        self.layout.label(text=f"Group Settings - {grp.name}")

    def draw(self, context):
        layout = self.layout
        scn = context.scene
        idx = scn.mg_active_group_index
        grp = scn.mg_export_groups[idx]

        # ── Collections ──
        layout.label(text="Collections", icon='OUTLINER_COLLECTION')
        col = layout.column(align=True)
        col.prop_search(grp, "hp_collection", bpy.data, "collections", text="HP")
        col.prop_search(grp, "lp_collection", bpy.data, "collections", text="LP")

        layout.separator()

        # ── Cage ──
        layout.label(text="Cage", icon='MOD_LATTICE')
        layout.prop(grp, "cage_offset", text="Offset")

        layout.separator()

        # ── Bake Resolution ──
        layout.label(text="Bake Resolution", icon='IMAGE_DATA')
        col = layout.column(align=True)
        split = col.split(factor=0.05, align=True)
        split.label(text="X")
        split.row(align=True).prop(grp, "res_x", expand=True)
        split = col.split(factor=0.05, align=True)
        split.label(text="Y")
        split.row(align=True).prop(grp, "res_y", expand=True)

        layout.separator()

        # ── Bake Maps ──
        layout.label(text="Bake Maps", icon='RENDERLAYERS')
        col = layout.column(align=True)
        flow = col.grid_flow(row_major=True, columns=2, even_columns=True, even_rows=True, align=True)
        flow.prop(grp, "bake_normal", text="Normal")
        flow.prop(grp, "bake_ao", text="AO")
        flow.prop(grp, "bake_curvature", text="Curvature")
        flow.prop(grp, "bake_world_normal", text="World Normal")
        flow.prop(grp, "bake_id", text="ID / Albedo")
        flow.prop(grp, "bake_thickness", text="Thickness")
        flow.prop(grp, "bake_position", text="Position")
        flow.prop(grp, "bake_uv_islands", text="UV Islands")

        layout.separator()

        # ── Export Options ──
        layout.label(text="Export Options", icon='EXPORT')
        flow = layout.grid_flow(row_major=True, columns=2, even_columns=True, even_rows=True, align=True)
        flow.prop(grp, "apply_modifiers")
        flow.prop(grp, "triangulate")
        flow.prop(grp, "smooth_by_angle")
        flow.prop(grp, "export_at_origin")


# ── Export ────────────────────────────────────────────────────────────────

class MG_PT_Export(bpy.types.Panel):
    bl_label = ""
    bl_idname = "MG_PT_Export"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "mgBaker"
    bl_parent_id = "MG_PT_Baker"

    def draw_header(self, context):
        n = sum(1 for g in context.scene.mg_export_groups if g.include)
        self.layout.label(text=f"Export - {n} group{'s' if n != 1 else ''}")

    def draw(self, context):
        from . import get_icon
        layout = self.layout

        col = layout.column(align=True)
        col.scale_y = 1.2
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

        layout.separator()
        layout.label(text="Painter Plugin", icon='PLUGIN')
        box = layout.box()
        box.scale_y = 0.8
        box.label(text="The mgBaker Painter plugin must be")
        box.label(text="installed once to enable bake automation.")
        layout.operator("mg.install_painter_plugin", icon='IMPORT')


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
