"""
3D View panel and context menu for Modo-Style Selection.
"""

import bpy
from .utils import _get_prefs


class VIEW3D_PT_modo_selection(bpy.types.Panel):
    """Panel for Modo-style selection tools."""
    bl_label      = "ModoKit"
    bl_idname     = "VIEW3D_PT_modo_selection"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category   = 'Edit'

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
