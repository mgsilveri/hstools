"""
mgBaker – UIList, group CRUD operators, outliner right-click, Install Painter Plugin.
"""

from __future__ import annotations

import os
import shutil

import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty

from .baker_props import _group_status


# ── UIList ────────────────────────────────────────────────────────────────

class MG_UL_ExportGroups(bpy.types.UIList):
    bl_idname = "MG_UL_ExportGroups"

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)

            # Include checkbox
            row.prop(item, "include", text="", icon='CHECKBOX_HLT' if item.include else 'CHECKBOX_DEHLT', emboss=False)

            # Status icon
            status = _group_status(item)
            if status == 'OK':
                row.label(text="", icon='CHECKMARK')
            elif status == 'WARN':
                row.label(text="", icon='ERROR')
            else:
                row.label(text="", icon='CANCEL')

            # Name
            row.prop(item, "name", text="", emboss=False)

            # Badges — always rendered so columns stay aligned
            sub = row.row(align=True)
            sub.scale_x = 0.5
            hp = sub.row(align=True)
            hp.active = bool(item.hp_collection)
            hp.label(text="HP")
            lp = sub.row(align=True)
            lp.active = bool(item.lp_collection)
            lp.label(text="LP")

        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text=item.name)


# ── Group CRUD ────────────────────────────────────────────────────────────

class MG_OT_AddGroup(bpy.types.Operator):
    bl_idname = "mg.add_export_group"
    bl_label = "Add Export Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        groups = context.scene.mg_export_groups
        g = groups.add()
        g.name = f"Group.{len(groups):03d}"
        context.scene.mg_active_group_index = len(groups) - 1
        return {'FINISHED'}


class MG_OT_RemoveGroup(bpy.types.Operator):
    bl_idname = "mg.remove_export_group"
    bl_label = "Remove Export Group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.scene.mg_export_groups) > 0

    def execute(self, context):
        idx = context.scene.mg_active_group_index
        context.scene.mg_export_groups.remove(idx)
        context.scene.mg_active_group_index = min(idx, len(context.scene.mg_export_groups) - 1)
        return {'FINISHED'}


class MG_OT_MoveGroup(bpy.types.Operator):
    bl_idname = "mg.move_export_group"
    bl_label = "Move Export Group"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        items=[('UP', "Up", ""), ('DOWN', "Down", "")],
        default='UP',
    )

    @classmethod
    def poll(cls, context):
        return len(context.scene.mg_export_groups) > 1

    def execute(self, context):
        groups = context.scene.mg_export_groups
        idx = context.scene.mg_active_group_index
        new_idx = idx + (-1 if self.direction == 'UP' else 1)
        if 0 <= new_idx < len(groups):
            groups.move(idx, new_idx)
            context.scene.mg_active_group_index = new_idx
        return {'FINISHED'}


# ── Outliner right-click ──────────────────────────────────────────────────

class MG_OT_AssignCollectionHP(bpy.types.Operator):
    bl_idname = "mg.assign_collection_hp"
    bl_label = "Set as HP"
    bl_description = "Assign the selected collection as HP for the active export group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            len(context.scene.mg_export_groups) > 0
            and context.collection is not None
            and context.collection != context.scene.collection
        )

    def execute(self, context):
        grp = context.scene.mg_export_groups[context.scene.mg_active_group_index]
        grp.hp_collection = context.collection
        self.report({'INFO'}, f"HP → {context.collection.name} (group: {grp.name})")
        return {'FINISHED'}


class MG_OT_AssignCollectionLP(bpy.types.Operator):
    bl_idname = "mg.assign_collection_lp"
    bl_label = "Set as LP"
    bl_description = "Assign the selected collection as LP for the active export group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            len(context.scene.mg_export_groups) > 0
            and context.collection is not None
            and context.collection != context.scene.collection
        )

    def execute(self, context):
        grp = context.scene.mg_export_groups[context.scene.mg_active_group_index]
        grp.lp_collection = context.collection
        self.report({'INFO'}, f"LP → {context.collection.name} (group: {grp.name})")
        return {'FINISHED'}


class MG_OT_ClearCollectionAssignment(bpy.types.Operator):
    bl_idname = "mg.clear_collection_assignment"
    bl_label = "Clear Assignment"
    bl_description = "Remove HP/LP assignment of this collection from the active export group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            len(context.scene.mg_export_groups) > 0
            and context.collection is not None
        )

    def execute(self, context):
        grp = context.scene.mg_export_groups[context.scene.mg_active_group_index]
        col = context.collection
        cleared = False
        if grp.hp_collection == col:
            grp.hp_collection = None
            cleared = True
        if grp.lp_collection == col:
            grp.lp_collection = None
            cleared = True
        if cleared:
            self.report({'INFO'}, f"Cleared assignment for {col.name}")
        else:
            self.report({'WARNING'}, f"{col.name} is not assigned to {grp.name}")
        return {'FINISHED'}


# ── Outliner menu draw function ───────────────────────────────────────────

def _draw_outliner_collection_menu(self, context):
    if not context.scene.mg_export_groups:
        return
    idx = context.scene.mg_active_group_index
    if idx < 0 or idx >= len(context.scene.mg_export_groups):
        return
    grp = context.scene.mg_export_groups[idx]
    layout = self.layout
    layout.separator()
    layout.label(text=f"mgBaker → {grp.name}")
    layout.operator("mg.assign_collection_hp", icon='MESH_CUBE')
    layout.operator("mg.assign_collection_lp", icon='MESH_PLANE')
    layout.operator("mg.clear_collection_assignment", icon='X')


# ── Install Painter Plugin ────────────────────────────────────────────────

# ── Log UIList + Copy Log operator ────────────────────────────────────────

class MG_UL_LogList(bpy.types.UIList):
    bl_idname = "MG_UL_LogList"

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            level = item.level
            if level == 'OK':
                row.label(text="", icon='CHECKMARK')
            elif level == 'WARN':
                row.label(text="", icon='ERROR')
            elif level == 'SECTION':
                row.label(text="", icon='DISCLOSURE_TRI_DOWN')
            else:
                row.label(text="", icon='NONE')
            row.prop(item, "text", text="", emboss=False)

    def filter_items(self, context, data, propname):
        # No filtering – preserve insertion order
        return [], []


class MG_OT_CopyLog(bpy.types.Operator):
    bl_idname = "mg.copy_log"
    bl_label = "Copy Log"
    bl_description = "Copy the full export log to the clipboard"

    def execute(self, context):
        lines = [item.text for item in context.scene.mg_export_log]
        context.window_manager.clipboard = "\n".join(lines)
        self.report({'INFO'}, "Log copied to clipboard")
        return {'FINISHED'}


# ── Install Painter Plugin ────────────────────────────────────────────────

class MG_OT_InstallPainterPlugin(bpy.types.Operator):
    bl_idname = "mg.install_painter_plugin"
    bl_label = "Install Painter Plugin"
    bl_description = "Copy the mgBaker plugin files to the Substance Painter plugins directory"

    def execute(self, context):
        user_profile = os.environ.get("USERPROFILE", "")
        if not user_profile:
            self.report({'ERROR'}, "Cannot determine USERPROFILE")
            return {'CANCELLED'}

        dest_dir = os.path.join(
            user_profile,
            "Documents",
            "Adobe",
            "Adobe Substance 3D Painter",
            "python",
            "plugins",
            "mgbaker",
        )

        # Source: bundled painter plugin next to this file
        src_dir = os.path.join(os.path.dirname(__file__), "painter_plugin")
        src_file = os.path.join(src_dir, "__init__.py")

        if not os.path.isfile(src_file):
            self.report({'ERROR'}, f"Plugin source not found: {src_file}")
            return {'CANCELLED'}

        os.makedirs(dest_dir, exist_ok=True)
        dest_file = os.path.join(dest_dir, "__init__.py")
        shutil.copy2(src_file, dest_file)

        self.report({'INFO'}, f"Plugin installed to {dest_dir}")
        return {'FINISHED'}
