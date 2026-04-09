"""
3D View panel and context menu for Modo-Style Selection.
"""

import bpy
from .utils import _get_prefs
from . import transform_3d


class VIEW3D_PT_modo_selection(bpy.types.Panel):
    """Panel for Modo-style selection tools."""
    bl_label      = "ModoKit"
    bl_idname     = "VIEW3D_PT_modo_selection"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category   = 'ModoKit'

    @classmethod
    def poll(cls, context):
        return context.mode in ('OBJECT', 'EDIT_MESH')

    def draw(self, context):
        layout = self.layout
        if context.mode != 'EDIT_MESH':
            return

        layout.separator()
        layout.label(text="Selection Tools:")
        row = layout.row()
        row.operator("mesh.modo_select_shortest_path", text="Select Between (Shift+G)")
        layout.separator()
        row = layout.row()
        row.operator("mesh.loop_multi_select", text="Select Loop (L)").ring = False
        row = layout.row()
        row.operator("mesh.loop_multi_select", text="Select Ring").ring = True
        layout.separator()
        layout.label(text="Mouse Selection:")
        row = layout.row()
        row.operator("mesh.modo_select_element_under_mouse", text="Set").mode = 'set'
        row.operator("mesh.modo_select_element_under_mouse", text="Add").mode = 'add'
        row = layout.row()
        row.operator("mesh.modo_select_element_under_mouse", text="Remove").mode = 'remove'
        row.operator("mesh.modo_select_element_under_mouse", text="Toggle").mode = 'toggle'

        # ── Falloff ───────────────────────────────────────────────────────────
        layout.separator()
        fp = getattr(context.scene, 'modokit_falloff', None)
        if fp is None:
            return

        box = layout.box()
        row = box.row()
        icon = 'CHECKBOX_HLT' if fp.enabled else 'CHECKBOX_DEHLT'
        row.operator('view3d.modo_linear_falloff',
                     text="Linear Falloff",
                     icon=icon,
                     depress=fp.enabled)

        if fp.enabled:
            # Auto-size axis buttons + Reverse
            row2 = box.row(align=True)
            row2.label(text="Auto Size:")
            for ax in ('X', 'Y', 'Z'):
                op = row2.operator('view3d.modo_falloff_auto_size', text=ax)
                op.axis = ax
            row2.operator('view3d.modo_falloff_reverse', text="", icon='ARROW_LEFTRIGHT')

            col = box.column(align=True)
            col.prop(fp, 'symmetric')
            col.prop(fp, 'shape_preset', text="Shape")
            if fp.shape_preset == 'CUSTOM':
                sub = col.row(align=True)
                sub.prop(fp, 'curve_in',  text="In")
                sub.prop(fp, 'curve_out', text="Out")
            col.separator()
            col.prop(fp, 'mix_mode')
            col.prop(fp, 'use_world')
            col.separator()
            col.prop(fp, 'show', text="Show Falloff")


class MESH_MT_modo_selection_context_menu(bpy.types.Menu):
    """Context menu for Modo-style selection.
    Appears on right-click when 2+ elements selected.
    """
    bl_label  = "Selection"
    bl_idname = "MESH_MT_modo_selection_context_menu"

    def draw(self, context):
        layout = self.layout
        layout.operator("mesh.modo_select_shortest_path", text="Select Between (Shortest Path)")
        layout.separator()
        layout.operator("mesh.loop_multi_select", text="Select Loop").ring = False
        layout.operator("mesh.loop_multi_select", text="Select Ring").ring = True
        layout.separator()
        layout.operator("mesh.select_more", text="Grow Selection")
        layout.operator("mesh.select_less", text="Shrink Selection")


# ── View menu injection ───────────────────────────────────────────────────────

def _draw_falloff_view_menu(self, context):
    """Appended to VIEW3D_MT_view — adds the Show Falloff toggle."""
    fp = getattr(context.scene, 'modokit_falloff', None)
    if fp is None:
        return
    layout = self.layout
    layout.separator()
    sub = layout.column()
    sub.enabled = fp.enabled
    sub.prop(fp, 'show', text="Show Falloff")
