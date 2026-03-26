"""
Component mode switching and related operators:
  VIEW3D_OT_modo_component_mode  (1/2/3/5 keys)
  MESH_OT_modo_boundary_select   (Ctrl+2)
  MESH_OT_modo_material_mode     (4 key)
Also contains the header patch for the Material Mode button.
"""

import bpy
import bmesh
from bpy.props import EnumProperty, BoolProperty

from . import state
from .utils import _get_prefs


# ── Module-local aliases into state for write access ─────────────────────────
# (Reading state vars works via state.x; writing via state.x = val)

_COMPONENT_MODE_MAP = {
    'VERT': (True,  False, False),
    'EDGE': (False, True,  False),
    'FACE': (False, False, True),
}


class VIEW3D_OT_modo_component_mode(bpy.types.Operator):
    """Modo-style 1/2/3/5 component-mode switching.

    Works in both Object Mode and Edit Mode:
      Object Mode  1/2/3 → enter Edit Mode with the right sub-mode pre-selected
      Edit Mode    1/2/3 → switch sub-mode (vertex/edge/face) in-place
      Edit Mode    5     → return to Object Mode
    """
    bl_idname = 'view3d.modo_component_mode'
    bl_label  = 'Modo Component Mode'
    bl_options = {'REGISTER', 'UNDO'}

    component: EnumProperty(
        name="Component",
        items=[
            ('VERT',   'Vertex',      ''),
            ('EDGE',   'Edge',        ''),
            ('FACE',   'Face',        ''),
            ('OBJECT', 'Object Mode', ''),
        ],
        default='VERT',
    )

    convert: BoolProperty(
        name="Convert Selection",
        description="Convert current selection to the target component type (Modo Alt+mode button)",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        if context.active_object is None:
            return False
        if context.active_object.type != 'MESH':
            return False
        return context.mode in ('OBJECT', 'EDIT_MESH')

    def execute(self, context):
        from .transform_3d import _drop_transform
        _drop_transform(context)
        state._material_mode_active = False

        if self.component == 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        elif context.mode == 'OBJECT':
            context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP[self.component]
            bpy.ops.object.mode_set(mode='EDIT')
        else:  # EDIT_MESH
            if self.convert:
                obj = context.edit_object
                bm  = bmesh.from_edit_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()

                cur = context.tool_settings.mesh_select_mode
                tgt = self.component

                if cur[2] and tgt == 'EDGE':
                    src_sel = {f.index for f in bm.faces if f.select}
                    sel = {e.index for f in bm.faces if f.select for e in f.edges}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['EDGE']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.edges.ensure_lookup_table()
                    bm.verts.ensure_lookup_table()
                    for e in bm.edges:
                        e.select = e.index in sel
                    for v in bm.verts:
                        v.select = any(e.select for e in v.link_edges)
                    bmesh.update_edit_mesh(obj.data)
                    # Save source-mode memory (we're leaving it) but leave target
                    # memory untouched — convert is a preview, not a memory commit.
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['FACE'] = src_sel
                    state._last_switch_was_convert = True
                    return {'FINISHED'}

                elif cur[2] and tgt == 'VERT':
                    src_sel = {f.index for f in bm.faces if f.select}
                    sel = {v.index for f in bm.faces if f.select for v in f.verts}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['VERT']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.verts.ensure_lookup_table()
                    for v in bm.verts:
                        v.select = v.index in sel
                    bmesh.update_edit_mesh(obj.data)
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['FACE'] = src_sel
                    mem['VERT'] = sel
                    return {'FINISHED'}

                elif cur[1] and tgt == 'VERT':
                    src_sel = {e.index for e in bm.edges if e.select}
                    sel = {v.index for e in bm.edges if e.select for v in e.verts}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['VERT']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.verts.ensure_lookup_table()
                    for v in bm.verts:
                        v.select = v.index in sel
                    bmesh.update_edit_mesh(obj.data)
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['EDGE'] = src_sel
                    mem['VERT'] = sel
                    return {'FINISHED'}

                elif cur[1] and tgt == 'FACE':
                    src_sel = {e.index for e in bm.edges if e.select}
                    sel = {f.index for f in bm.faces
                           if sum(1 for e in f.edges if e.select) >= 2}
                    if not sel:
                        sel = {f.index for f in bm.faces if any(e.select for e in f.edges)}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['FACE']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.faces.ensure_lookup_table()
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    for f in bm.faces:
                        f.select = f.index in sel
                    for f in bm.faces:
                        if f.select:
                            for v in f.verts: v.select = True
                            for e in f.edges: e.select = True
                    bmesh.update_edit_mesh(obj.data)
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['EDGE'] = src_sel
                    mem['FACE'] = sel
                    return {'FINISHED'}

                elif cur[0] and tgt == 'EDGE':
                    src_sel = {v.index for v in bm.verts if v.select}
                    sel = {e.index for e in bm.edges if all(v.select for v in e.verts)}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['EDGE']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.edges.ensure_lookup_table()
                    bm.verts.ensure_lookup_table()
                    for e in bm.edges:
                        e.select = e.index in sel
                    for v in bm.verts:
                        v.select = any(e.select for e in v.link_edges)
                    bmesh.update_edit_mesh(obj.data)
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['VERT'] = src_sel
                    mem['EDGE'] = sel
                    return {'FINISHED'}

                elif cur[0] and tgt == 'FACE':
                    src_sel = {v.index for v in bm.verts if v.select}
                    sel = {f.index for f in bm.faces if all(v.select for v in f.verts)}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['FACE']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.faces.ensure_lookup_table()
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    for f in bm.faces:
                        f.select = f.index in sel
                    for f in bm.faces:
                        if f.select:
                            for e in f.edges: e.select = True
                            for v in f.verts: v.select = True
                    bmesh.update_edit_mesh(obj.data)
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['VERT'] = src_sel
                    mem['FACE'] = sel
                    return {'FINISHED'}

                else:
                    bpy.ops.mesh.select_mode(use_extend=False, use_expand=False,
                                             type=tgt)
                    return {'FINISHED'}

            else:
                # Independent mode switching — each sub-mode keeps its own selection.
                cur_v, cur_e, cur_f = context.tool_settings.mesh_select_mode
                cur_key = 'FACE' if cur_f else ('EDGE' if cur_e else 'VERT')
                tgt_key = self.component

                for obj in context.objects_in_mode_unique_data:
                    if obj.type != 'MESH':
                        continue
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    bm.faces.ensure_lookup_table()
                    mem = state._selection_memory.setdefault(
                        obj.data.name,
                        {'VERT': set(), 'EDGE': set(), 'FACE': set()}
                    )
                    if cur_key == 'VERT':
                        mem['VERT'] = {v.index for v in bm.verts if v.select}
                    elif cur_key == 'EDGE':
                        mem['EDGE'] = {e.index for e in bm.edges if e.select}
                    else:
                        mem['FACE'] = {f.index for f in bm.faces if f.select}

                context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP[tgt_key]

                for obj in context.objects_in_mode_unique_data:
                    if obj.type != 'MESH':
                        continue
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    bm.faces.ensure_lookup_table()
                    mem   = state._selection_memory.get(obj.data.name, {})
                    saved = mem.get(tgt_key, set())

                    for v in bm.verts: v.select = False
                    for e in bm.edges: e.select = False
                    for f in bm.faces: f.select = False
                    bm.select_history.clear()

                    if tgt_key == 'VERT':
                        for i in saved:
                            if i < len(bm.verts):
                                bm.verts[i].select = True
                    elif tgt_key == 'EDGE':
                        for i in saved:
                            if i < len(bm.edges):
                                e = bm.edges[i]
                                e.select = True
                                for v in e.verts: v.select = True
                    else:
                        for i in saved:
                            if i < len(bm.faces):
                                f = bm.faces[i]
                                f.select = True
                                for v in f.verts: v.select = True
                                for e in f.edges: e.select = True

                    bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}


class MESH_OT_modo_boundary_select(bpy.types.Operator):
    """Select the boundary (perimeter) edges of the current face selection.
    Ctrl+2: replaces selection with boundary edges.
    Shift+Ctrl+2: adds boundary edges to the existing selection.
    """
    bl_idname = 'mesh.modo_boundary_select'
    bl_label  = 'Modo Boundary Select'
    bl_options = {'REGISTER', 'UNDO'}

    additive: BoolProperty(
        name="Additive",
        description="Keep existing selection and add boundary edges (+Bounds)",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        obj = context.edit_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.edit_object
        bm  = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.verts.ensure_lookup_table()

        boundary_edges = [
            e for e in bm.edges
            if sum(1 for f in e.link_faces if f.select) == 1
        ]

        if not boundary_edges:
            self.report({'INFO'}, 'No boundary edges found — select faces first')
            return {'CANCELLED'}

        if not self.additive:
            for f in bm.faces:
                f.select = False
            for e in bm.edges:
                e.select = False
            for v in bm.verts:
                v.select = False

        for edge in boundary_edges:
            edge.select = True
            for v in edge.verts:
                v.select = True

        bmesh.update_edit_mesh(obj.data)
        bpy.ops.mesh.select_mode(use_extend=False, use_expand=False, type='EDGE')
        return {'FINISHED'}


class MESH_OT_modo_material_mode(bpy.types.Operator):
    """Modo-style 4: enter Materials selection mode.
    Edit Mode: switches to Face sub-mode.  Clicking a face selects all faces
    sharing the same material slot.  Press 1/2/3/5 to leave this mode.
    """
    bl_idname = 'mesh.modo_material_mode'
    bl_label  = 'Modo Material Mode'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        return context.mode in ('OBJECT', 'EDIT_MESH')

    def execute(self, context):
        state._material_mode_active = True
        if context.mode == 'EDIT_MESH':
            context.tool_settings.mesh_select_mode = (False, False, True)
        return {'FINISHED'}


# ── Header patch ──────────────────────────────────────────────────────────────

def _patched_editor_menus_draw_collapsible(cls, context, layout):
    """Replacement for VIEW3D_MT_editor_menus.draw_collapsible.

    Draws the Material Mode button to the outer header layout (non-aligned)
    BEFORE draw_collapsible creates its own align=True row for the View/Select
    etc. menu items.  This gives the button fully-rounded corners on both sides.
    """
    if context.mode == 'EDIT_MESH':
        obj = context.active_object
        if obj is not None and obj.type == 'MESH':
            layout.row(align=True).operator(
                'mesh.modo_material_mode',
                text='',
                icon='MATERIAL',
                depress=state._material_mode_active,
            )
    state._orig_editor_menus_draw_collapsible(context, layout)
