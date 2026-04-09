"""
mgBaker – UIList, group CRUD operators, outliner right-click, Install Painter Plugin.
"""

from __future__ import annotations

import os
import shutil

import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty

from .baker_props import _group_status, get_active_project


# ── UIList ────────────────────────────────────────────────────────────────
class MG_UL_MaterialSlots(bpy.types.UIList):
    bl_idname = "MG_UL_MaterialSlots"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        mat = item.mat
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            if mat:
                has_problem = not mat.name or any(
                    c in mat.name
                    for c in (' ', '\t', '/', '\\', ':', '*', '?', '"', '<', '>', '|')
                )
                row.alert = has_problem
                split = row.split(factor=0.08, align=True)
                split.label(text=f"{index + 1}:")
                split.prop(mat, "name", text="",
                           icon='ERROR' if has_problem else 'MATERIAL')
            else:
                row.label(text=f"{index + 1}: (empty slot)", icon='ERROR')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='MATERIAL')

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
            else:
                row.label(text="", icon='ERROR')

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


class MG_UL_Projects(bpy.types.UIList):
    bl_idname = "MG_UL_Projects"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            n_groups = len(item.groups)
            n_ready = sum(1 for g in item.groups
                          if g.hp_collection and g.lp_collection)
            row.prop(item, "name", text="", emboss=False, icon='FILE_3D')
            sub = row.row(align=True)
            sub.scale_x = 0.55
            sub.label(text=f"{n_ready}/{n_groups}")
        elif self.layout_type == 'GRID':
            layout.label(text=item.name, icon='FILE_3D')


# ── Group CRUD ────────────────────────────────────────────────────────────

class MG_OT_AddGroup(bpy.types.Operator):
    bl_idname = "mg.add_export_group"
    bl_label = "Add Export Group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.mg_projects)

    def execute(self, context):
        proj = get_active_project(context.scene)
        if proj is None:
            return {'CANCELLED'}
        g = proj.groups.add()
        g.name = f"Group.{len(proj.groups):03d}"
        proj.active_group_index = len(proj.groups) - 1
        return {'FINISHED'}


class MG_OT_RemoveGroup(bpy.types.Operator):
    bl_idname = "mg.remove_export_group"
    bl_label = "Remove Export Group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        proj = get_active_project(context.scene)
        return proj is not None and len(proj.groups) > 0

    def execute(self, context):
        proj = get_active_project(context.scene)
        if proj is None:
            return {'CANCELLED'}
        idx = proj.active_group_index
        proj.groups.remove(idx)
        proj.active_group_index = min(idx, len(proj.groups) - 1)
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
        proj = get_active_project(context.scene)
        return proj is not None and len(proj.groups) > 1

    def execute(self, context):
        proj = get_active_project(context.scene)
        if proj is None:
            return {'CANCELLED'}
        idx = proj.active_group_index
        new_idx = idx + (-1 if self.direction == 'UP' else 1)
        if 0 <= new_idx < len(proj.groups):
            proj.groups.move(idx, new_idx)
            proj.active_group_index = new_idx
        return {'FINISHED'}

# ── Project CRUD ───────────────────────────────────────────────────────────

class MG_OT_AddProject(bpy.types.Operator):
    bl_idname = "mg.add_project"
    bl_label = "Add Project"
    bl_description = "Each project has its own export groups and produces a separate .tbscene / .spp file."
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scn = context.scene
        projects = scn.mg_projects

        proj = projects.add()
        if context.active_object and context.active_object.type == 'MESH':
            proj.name = context.active_object.name
        else:
            proj.name = f"Project.{len(projects):03d}"
        scn.mg_active_project_index = len(projects) - 1
        return {'FINISHED'}


class MG_OT_RemoveProject(bpy.types.Operator):
    bl_idname = "mg.remove_project"
    bl_label = "Remove Project"
    bl_description = (
        "Remove the active project and all its export groups.\n\n"
        "At least one project must exist — the last one cannot be removed."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.scene.mg_projects) > 1

    def execute(self, context):
        scn = context.scene
        idx = scn.mg_active_project_index
        scn.mg_projects.remove(idx)
        scn.mg_active_project_index = min(idx, len(scn.mg_projects) - 1)
        return {'FINISHED'}


class MG_OT_MoveProject(bpy.types.Operator):
    bl_idname = "mg.move_project"
    bl_label = "Move Project"
    bl_description = "Reorder the active project in the list"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        items=[('UP', "Up", ""), ('DOWN', "Down", "")],
        default='UP',
    )

    @classmethod
    def poll(cls, context):
        return len(context.scene.mg_projects) > 1

    def execute(self, context):
        scn = context.scene
        idx = scn.mg_active_project_index
        new_idx = idx + (-1 if self.direction == 'UP' else 1)
        if 0 <= new_idx < len(scn.mg_projects):
            scn.mg_projects.move(idx, new_idx)
            scn.mg_active_project_index = new_idx
        return {'FINISHED'}


class MG_OT_NavigateProject(bpy.types.Operator):
    bl_idname = "mg.navigate_project"
    bl_label = "Navigate Project"
    bl_description = "Switch to the previous or next project"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        items=[('PREV', "Previous", ""), ('NEXT', "Next", "")],
        default='NEXT',
    )

    @classmethod
    def poll(cls, context):
        return len(context.scene.mg_projects) > 1

    def execute(self, context):
        scn = context.scene
        n = len(scn.mg_projects)
        idx = scn.mg_active_project_index + (-1 if self.direction == 'PREV' else 1)
        scn.mg_active_project_index = max(0, min(idx, n - 1))
        return {'FINISHED'}

# ── Outliner right-click ──────────────────────────────────────────────────

class MG_OT_AssignCollectionHP(bpy.types.Operator):
    bl_idname = "mg.assign_collection_hp"
    bl_label = "Set as HP"
    bl_description = "Assign the selected collection as HP for the active export group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        proj = get_active_project(context.scene)
        return (
            proj is not None
            and len(proj.groups) > 0
            and context.collection is not None
            and context.collection != context.scene.collection
        )

    def execute(self, context):
        proj = get_active_project(context.scene)
        grp = proj.groups[proj.active_group_index]
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
        proj = get_active_project(context.scene)
        return (
            proj is not None
            and len(proj.groups) > 0
            and context.collection is not None
            and context.collection != context.scene.collection
        )

    def execute(self, context):
        proj = get_active_project(context.scene)
        grp = proj.groups[proj.active_group_index]
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
        proj = get_active_project(context.scene)
        return (
            proj is not None
            and len(proj.groups) > 0
            and context.collection is not None
        )

    def execute(self, context):
        proj = get_active_project(context.scene)
        grp = proj.groups[proj.active_group_index]
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
    proj = get_active_project(context.scene)
    if proj is None or not proj.groups:
        return
    idx = proj.active_group_index
    if idx < 0 or idx >= len(proj.groups):
        return
    grp = proj.groups[idx]
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