"""
UV editor operators:
  IMAGE_OT_modo_uv_transform       (W/E/R toggle)
  IMAGE_OT_modo_uv_component_mode  (1/2/3)
  IMAGE_OT_modo_uv_drop_transform  (Space)
  IMAGE_OT_modo_uv_handle_reposition  (LMB drag/click on the gizmo)
  IMAGE_OT_modo_uv_rip             (Tear/Unstitch UV by selection)
"""

import json
import math
import time
from collections import defaultdict
import bpy
import bmesh
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from . import state
from .utils import get_addon_preferences, _uv_debug_log
from .uv_overlays import (
    _compute_uv_selection_median, _sync_uv_gizmo_center_to_bmesh,
    _start_uv_gizmo, _stop_uv_gizmo,
    _uv_view_to_region, _uv_region_to_view,
)
from .uv_snap import (
    _collect_uv_transform_targets, _uv_drop_transform, _stop_uv_snap_highlight,
    _is_uv_snap_active, _get_uv_snap_elements, _snap_uv_translate,
    _uv_auto_drop_check, _get_uv_grid_size,
)


class IMAGE_OT_modo_uv_transform(bpy.types.Operator):
    """Modo-style W/E/R: toggle Move / Rotate / Scale in the UV Editor"""
    bl_idname  = 'image.modo_uv_transform'
    bl_label   = 'Modo UV Transform'
    bl_options = {'REGISTER', 'UNDO'}

    _last_invoke_time: float = 0.0

    _UV_TOOL_IDS = {
        'TRANSLATE': 'builtin.move',
        'ROTATE':    'builtin.rotate',
        'RESIZE':    'builtin.scale',
    }

    transform_type: EnumProperty(
        name='Transform Type',
        items=[
            ('TRANSLATE', 'Move',   ''),
            ('ROTATE',    'Rotate', ''),
            ('RESIZE',    'Scale',  ''),
        ],
        default='TRANSLATE',
    )

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    def _active_uv_tool_id(self, context):
        """Return the idname of the current UV workspace tool, or '' on failure."""
        try:
            tool = context.workspace.tools.from_space_image_mode('UV', create=False)
            return tool.idname if tool else ''
        except Exception:
            return ''

    def invoke(self, context, event):
        now = time.monotonic()
        if now - IMAGE_OT_modo_uv_transform._last_invoke_time < 0.05:
            return {'CANCELLED'}
        IMAGE_OT_modo_uv_transform._last_invoke_time = now

        # Always check state — we keep builtin.select as the workspace tool while our
        # custom gizmo is active, so checking the active tool id would always return False.
        already_on = (state._uv_active_transform_mode == self.transform_type)

        if already_on:
            _uv_drop_transform(context)
            self.report({'INFO'}, f"UV {self.transform_type.title()} tool OFF")
        else:
            if state._uv_active_transform_mode is not None:
                _stop_uv_snap_highlight()
            state._uv_active_transform_mode = self.transform_type

            state._uv_transform_targets = _collect_uv_transform_targets(context)
            state._uv_sel_targets = _collect_uv_transform_targets(context, override_sticky='DISABLED')

            median = _compute_uv_selection_median(context)
            state._uv_gizmo_center = median if median else (0.5, 0.5)
            try:
                _dbg = getattr(get_addon_preferences(context), 'debug_uv_handle', False)
            except Exception:
                _dbg = False
            if _dbg:
                _uv_debug_log(
                    f"[UV-GIZMO-CTR] W/E/R activate: median={median} "
                    f"→ center={state._uv_gizmo_center} targets={len(state._uv_transform_targets)}"
                )
            _sync_uv_gizmo_center_to_bmesh(context)
            obj = context.edit_object
            if obj and obj.type == 'MESH':
                bmesh.update_edit_mesh(obj.data, destructive=False)

            if state._uv_snap_highlight_draw_handle is None:
                try:
                    bpy.ops.image.modo_uv_snap_highlight('INVOKE_DEFAULT')
                except Exception:
                    pass

            _start_uv_gizmo()
            _TOOL_IDS = {
                'TRANSLATE': 'builtin.move',
                'ROTATE':    'builtin.rotate',
                'RESIZE':    'builtin.scale',
            }
            try:
                bpy.ops.wm.tool_set_by_id(
                    name=_TOOL_IDS[self.transform_type], space_type='IMAGE_EDITOR')
            except Exception:
                pass
            sima = context.space_data
            if sima:
                sima.show_gizmo = False

            # Start the selection guard modal (if not already running).
            # It sits at the top of Blender's modal stack and consumes all LMB
            # events so the tool-keymap's uv.select never fires while our gizmo
            # is active.  The guard delegates each LMB to handle_reposition instead.
            if not state._uv_selection_guard_running:
                try:
                    bpy.ops.image.modo_uv_selection_guard('INVOKE_DEFAULT')
                except Exception:
                    pass

            if not bpy.app.timers.is_registered(_uv_auto_drop_check):
                bpy.app.timers.register(_uv_auto_drop_check, first_interval=0.25)

            self.report({'INFO'}, f"UV {self.transform_type.title()} tool ON")

        return {'FINISHED'}


class IMAGE_OT_modo_uv_component_mode(bpy.types.Operator):
    """Modo-style 1/2/3 UV select-mode switching."""
    bl_idname = 'image.modo_uv_component_mode'
    bl_label  = 'Modo UV Component Mode'
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name='Mode',
        items=[
            ('VERTEX', 'Vertex', ''),
            ('EDGE',   'Edge',   ''),
            ('FACE',   'Face',   ''),
        ],
        default='VERTEX',
    )

    convert: BoolProperty(
        name='Convert Selection',
        description='Convert current UV selection to the target component type (Alt+mode key)',
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    def execute(self, context):
        ts  = context.tool_settings
        obj = context.edit_object
        if obj is None or obj.type != 'MESH':
            return {'CANCELLED'}

        _uv_drop_transform(context)
        _was_material = state._uv_material_mode_active
        state._uv_material_mode_active = False
        tgt_mode = self.mode

        # ── Convert selection (Alt+1/2/3) ────────────────────────────────────
        if self.convert:
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

            if ts.use_uv_select_sync:
                cur_v, cur_e, cur_f = ts.mesh_select_mode
                cur_key = 'FACE' if cur_f else ('EDGE' if cur_e else 'VERT')
                comp_map = {'VERTEX': 'VERT', 'EDGE': 'EDGE', 'FACE': 'FACE'}
                tgt_key  = comp_map[tgt_mode]
                mode_map = {'VERT': (True, False, False),
                            'EDGE': (False, True,  False),
                            'FACE': (False, False, True)}

                if cur_key == tgt_key and not _was_material:
                    return {'FINISHED'}

                if cur_key == 'FACE' and tgt_key == 'EDGE':
                    sel = {e.index for f in bm.faces if f.select for e in f.edges}
                elif cur_key == 'FACE' and tgt_key == 'VERT':
                    sel = {v.index for f in bm.faces if f.select for v in f.verts}
                elif cur_key == 'EDGE' and tgt_key == 'VERT':
                    sel = {v.index for e in bm.edges if e.select for v in e.verts}
                elif cur_key == 'EDGE' and tgt_key == 'FACE':
                    sel = {f.index for f in bm.faces
                           if sum(1 for e in f.edges if e.select) >= 2}
                    if not sel:
                        sel = {f.index for f in bm.faces
                               if any(e.select for e in f.edges)}
                elif cur_key == 'VERT' and tgt_key == 'EDGE':
                    sel = {e.index for e in bm.edges
                           if all(v.select for v in e.verts)}
                elif cur_key == 'VERT' and tgt_key == 'FACE':
                    sel = {f.index for f in bm.faces
                           if all(v.select for v in f.verts)}
                else:
                    sel = set()

                for v in bm.verts: v.select = False
                for e in bm.edges: e.select = False
                for f in bm.faces: f.select = False
                bm.select_history.clear()
                ts.mesh_select_mode = mode_map[tgt_key]
                bm = bmesh.from_edit_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()

                if tgt_key == 'VERT':
                    for i in sel:
                        if i < len(bm.verts): bm.verts[i].select = True
                elif tgt_key == 'EDGE':
                    for i in sel:
                        if i < len(bm.edges):
                            bm.edges[i].select = True
                            for v in bm.edges[i].verts: v.select = True
                else:
                    for i in sel:
                        if i < len(bm.faces):
                            f = bm.faces[i]
                            f.select = True
                            for v in f.verts: v.select = True
                            for e in f.edges: e.select = True

                bm.select_flush_mode()
                uv_layer = bm.loops.layers.uv.active
                if uv_layer is not None:
                    if tgt_key == 'VERT':
                        for face in bm.faces:
                            for loop in face.loops:
                                loop.uv_select_vert = loop.vert.select
                                loop.uv_select_edge = False
                    elif tgt_key == 'EDGE':
                        for face in bm.faces:
                            for loop in face.loops:
                                loop.uv_select_edge = loop.edge.select
                                loop.uv_select_vert = loop.vert.select
                    else:
                        for face in bm.faces:
                            s = face.select
                            for loop in face.loops:
                                loop.uv_select_vert = s
                                loop.uv_select_edge = s
                bmesh.update_edit_mesh(obj.data)

            else:  # Sync OFF
                uv_layer = bm.loops.layers.uv.active
                cur_mode = ts.uv_select_mode

                if cur_mode == tgt_mode and not _was_material:
                    return {'FINISHED'}

                if uv_layer is not None:
                    if cur_mode == 'FACE' and tgt_mode == 'VERTEX':
                        sel_faces = {f.index for f in bm.faces
                                     if f.loops
                                     and all(l.uv_select_vert for l in f.loops)}
                        bpy.ops.uv.select_all(action='DESELECT')
                        ts.uv_select_mode = 'VERTEX'
                        bm = bmesh.from_edit_mesh(obj.data)
                        bm.faces.ensure_lookup_table()
                        for f in bm.faces:
                            if f.index in sel_faces:
                                for loop in f.loops:
                                    loop.uv_select_vert = True

                    elif cur_mode == 'FACE' and tgt_mode == 'EDGE':
                        sel_faces = {f.index for f in bm.faces
                                     if f.loops
                                     and all(l.uv_select_vert for l in f.loops)}
                        bpy.ops.uv.select_all(action='DESELECT')
                        ts.uv_select_mode = 'EDGE'
                        bm = bmesh.from_edit_mesh(obj.data)
                        bm.faces.ensure_lookup_table()
                        for f in bm.faces:
                            if f.index in sel_faces:
                                for loop in f.loops:
                                    loop.uv_select_edge = True
                                    loop.uv_select_vert = True

                    elif cur_mode == 'EDGE' and tgt_mode == 'VERTEX':
                        sel_verts = set()
                        for f in bm.faces:
                            fi = f.index
                            loops = list(f.loops)
                            n = len(loops)
                            for li, loop in enumerate(loops):
                                if loop.uv_select_edge:
                                    sel_verts.add((fi, li))
                                    sel_verts.add((fi, (li + 1) % n))
                        bpy.ops.uv.select_all(action='DESELECT')
                        ts.uv_select_mode = 'VERTEX'
                        bm = bmesh.from_edit_mesh(obj.data)
                        bm.faces.ensure_lookup_table()
                        for f in bm.faces:
                            fi = f.index
                            for li, loop in enumerate(f.loops):
                                if (fi, li) in sel_verts:
                                    loop.uv_select_vert = True

                    elif cur_mode == 'EDGE' and tgt_mode == 'FACE':
                        edge_counts = {f.index: sum(1 for l in f.loops
                                                    if l.uv_select_edge)
                                       for f in bm.faces}
                        sel_faces = {fi for fi, c in edge_counts.items() if c >= 2}
                        if not sel_faces:
                            sel_faces = {fi for fi, c in edge_counts.items() if c >= 1}
                        bpy.ops.uv.select_all(action='DESELECT')
                        ts.uv_select_mode = 'FACE'
                        bm = bmesh.from_edit_mesh(obj.data)
                        bm.faces.ensure_lookup_table()
                        for f in bm.faces:
                            if f.index in sel_faces:
                                for loop in f.loops:
                                    loop.uv_select_vert = True
                                    loop.uv_select_edge = True

                    elif cur_mode == 'VERTEX' and tgt_mode == 'EDGE':
                        sel_vert_loops = {(f.index, li)
                                          for f in bm.faces
                                          for li, loop in enumerate(f.loops)
                                          if loop.uv_select_vert}
                        bpy.ops.uv.select_all(action='DESELECT')
                        ts.uv_select_mode = 'EDGE'
                        bm = bmesh.from_edit_mesh(obj.data)
                        bm.faces.ensure_lookup_table()
                        for f in bm.faces:
                            fi = f.index
                            loops = list(f.loops)
                            n = len(loops)
                            for li, loop in enumerate(loops):
                                next_li = (li + 1) % n
                                if (fi, li) in sel_vert_loops and (fi, next_li) in sel_vert_loops:
                                    loop.uv_select_edge = True
                                    loop.uv_select_vert = True
                                    loops[next_li].uv_select_vert = True

                    elif cur_mode == 'VERTEX' and tgt_mode == 'FACE':
                        sel_faces = {f.index for f in bm.faces
                                     if f.loops
                                     and all(l.uv_select_vert for l in f.loops)}
                        bpy.ops.uv.select_all(action='DESELECT')
                        ts.uv_select_mode = 'FACE'
                        bm = bmesh.from_edit_mesh(obj.data)
                        bm.faces.ensure_lookup_table()
                        for f in bm.faces:
                            if f.index in sel_faces:
                                for loop in f.loops:
                                    loop.uv_select_vert = True
                                    loop.uv_select_edge = True

                    bmesh.update_edit_mesh(obj.data)

            for win in context.window_manager.windows:
                for a in win.screen.areas:
                    a.tag_redraw()
            return {'FINISHED'}
        # ────────────────────────────────────────────────────────────────────

        if ts.use_uv_select_sync:
            comp_map = {'VERTEX': 'VERT', 'EDGE': 'EDGE', 'FACE': 'FACE'}
            tgt_key = comp_map[tgt_mode]
            mode_map = {'VERT': (True, False, False),
                        'EDGE': (False, True,  False),
                        'FACE': (False, False, True)}

            cur_v, cur_e, cur_f = ts.mesh_select_mode
            cur_key = 'FACE' if cur_f else ('EDGE' if cur_e else 'VERT')
            if cur_key == tgt_key and not _was_material:
                return {'FINISHED'}
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            mem = state._selection_memory.setdefault(
                obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()}
            )
            if not _was_material:
                if cur_key == 'VERT':
                    mem['VERT'] = {v.index for v in bm.verts if v.select}
                    uv_layer_snap = bm.loops.layers.uv.active
                    if uv_layer_snap is not None:
                        mem['VERT_UV'] = {
                            (f.index, li)
                            for f in bm.faces
                            for li, loop in enumerate(f.loops)
                            if loop.uv_select_vert
                        }
                    else:
                        mem.pop('VERT_UV', None)
                elif cur_key == 'EDGE':
                    mem['EDGE'] = {e.index for e in bm.edges if e.select}
                else:
                    mem['FACE'] = {f.index for f in bm.faces if f.select}

            v3d_win = v3d_area = v3d_region = None
            for win in context.window_manager.windows:
                for area in win.screen.areas:
                    if area.type == 'VIEW_3D':
                        rgn = next((r for r in area.regions if r.type == 'WINDOW'), None)
                        if rgn:
                            v3d_win, v3d_area, v3d_region = win, area, rgn; break
                if v3d_area:
                    break

            def _apply():
                bpy.ops.mesh.select_all(action='DESELECT')
                ts.mesh_select_mode = mode_map[tgt_key]
                bm2 = bmesh.from_edit_mesh(obj.data)
                bm2.verts.ensure_lookup_table()
                bm2.edges.ensure_lookup_table()
                bm2.faces.ensure_lookup_table()
                saved = mem.get(tgt_key, set())
                if tgt_key == 'VERT':
                    for i in saved:
                        if i < len(bm2.verts): bm2.verts[i].select = True
                elif tgt_key == 'EDGE':
                    for i in saved:
                        if i < len(bm2.edges): bm2.edges[i].select = True
                else:
                    for i in saved:
                        if i < len(bm2.faces): bm2.faces[i].select = True
                bm2.select_flush_mode()
                uv_layer2 = bm2.loops.layers.uv.active
                if uv_layer2 is not None:
                    if tgt_key == 'VERT':
                        vert_uv_snap = mem.get('VERT_UV')
                        for fi, face in enumerate(bm2.faces):
                            for li, loop in enumerate(face.loops):
                                if vert_uv_snap is not None:
                                    sel = (fi, li) in vert_uv_snap
                                else:
                                    sel = loop.vert.select
                                loop.uv_select_vert = sel
                                loop.uv_select_edge = False
                    elif tgt_key == 'EDGE':
                        for face in bm2.faces:
                            for loop in face.loops:
                                loop.uv_select_edge = loop.edge.select
                                loop.uv_select_vert = loop.vert.select
                    else:
                        for face in bm2.faces:
                            sel = face.select
                            for loop in face.loops:
                                loop.uv_select_vert = sel
                                loop.uv_select_edge = sel
                bmesh.update_edit_mesh(obj.data)

            if v3d_area:
                with context.temp_override(window=v3d_win, area=v3d_area, region=v3d_region):
                    _apply()
            else:
                _apply()

            for win in context.window_manager.windows:
                for a in win.screen.areas:
                    a.tag_redraw()
            return {'FINISHED'}

        # Sync OFF
        cur_mode = ts.uv_select_mode
        if cur_mode == tgt_mode and not _was_material:
            return {'FINISHED'}
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.active

        _uv_mem = state._uv_selection_memory.setdefault(
            obj.data.name, {'VERTEX': set(), 'EDGE': set(), 'FACE': set()}
        )

        if uv_layer is not None and not _was_material:
            if cur_mode == 'VERTEX':
                _uv_mem['VERTEX'] = {
                    (f.index, li)
                    for f in bm.faces
                    for li, loop in enumerate(f.loops)
                    if loop.uv_select_vert
                }
            elif cur_mode == 'EDGE':
                _uv_mem['EDGE'] = {
                    (f.index, li)
                    for f in bm.faces
                    for li, loop in enumerate(f.loops)
                    if loop.uv_select_edge
                }
            else:
                _uv_mem['FACE'] = {
                    f.index for f in bm.faces
                    if f.loops and all(loop.uv_select_vert for loop in f.loops)
                }

        bpy.ops.uv.select_all(action='DESELECT')
        ts.uv_select_mode = tgt_mode

        if uv_layer is not None:
            bm = bmesh.from_edit_mesh(obj.data)
            bm.faces.ensure_lookup_table()
            saved = _uv_mem.get(tgt_mode, set())
            if tgt_mode == 'VERTEX':
                for f in bm.faces:
                    for li, loop in enumerate(f.loops):
                        loop.uv_select_vert = (f.index, li) in saved
            elif tgt_mode == 'EDGE':
                for f in bm.faces:
                    for li, loop in enumerate(f.loops):
                        val = (f.index, li) in saved
                        loop.uv_select_edge = val
                        if val: loop.uv_select_vert = True
            else:
                for f in bm.faces:
                    val = f.index in saved
                    for loop in f.loops:
                        loop.uv_select_vert = val
                        loop.uv_select_edge = val
            bmesh.update_edit_mesh(obj.data)

        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


class IMAGE_OT_modo_uv_material_mode(bpy.types.Operator):
    """Modo-style 4: toggle UV material-selection mode.
    Clicking a UV face selects all UV faces on the same material slot.
    Press 4 again or 1/2/3 to leave this mode."""
    bl_idname  = 'image.modo_uv_material_mode'
    bl_label   = 'Modo UV Material Mode'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    def execute(self, context):
        if state._uv_material_mode_active:
            return {'FINISHED'}
        # Save current component selection before activating material mode
        # so 1/2/3 can restore it when exiting.
        ts  = context.tool_settings
        obj = context.edit_object
        if obj and obj.type == 'MESH':
            if not ts.use_uv_select_sync:
                cur_mode = ts.uv_select_mode
                bm = bmesh.from_edit_mesh(obj.data)
                bm.faces.ensure_lookup_table()
                uv_layer = bm.loops.layers.uv.active
                _uv_mem = state._uv_selection_memory.setdefault(
                    obj.data.name, {'VERTEX': set(), 'EDGE': set(), 'FACE': set()}
                )
                if uv_layer is not None:
                    if cur_mode == 'VERTEX':
                        _uv_mem['VERTEX'] = {
                            (f.index, li)
                            for f in bm.faces
                            for li, loop in enumerate(f.loops)
                            if loop.uv_select_vert
                        }
                    elif cur_mode == 'EDGE':
                        _uv_mem['EDGE'] = {
                            (f.index, li)
                            for f in bm.faces
                            for li, loop in enumerate(f.loops)
                            if loop.uv_select_edge
                        }
                    else:
                        _uv_mem['FACE'] = {
                            f.index for f in bm.faces
                            if f.loops and all(loop.uv_select_vert for loop in f.loops)
                        }
            else:
                cur_v, cur_e, cur_f = ts.mesh_select_mode
                cur_key = 'FACE' if cur_f else ('EDGE' if cur_e else 'VERT')
                bm = bmesh.from_edit_mesh(obj.data)
                mem = state._selection_memory.setdefault(
                    obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()}
                )
                if cur_key == 'VERT':
                    bm.verts.ensure_lookup_table()
                    mem['VERT'] = {v.index for v in bm.verts if v.select}
                elif cur_key == 'EDGE':
                    bm.edges.ensure_lookup_table()
                    mem['EDGE'] = {e.index for e in bm.edges if e.select}
                else:
                    bm.faces.ensure_lookup_table()
                    mem['FACE'] = {f.index for f in bm.faces if f.select}
        state._uv_material_mode_active = True
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


class IMAGE_OT_modo_uv_drop_transform(bpy.types.Operator):
    """Drop the active UV W/E/R tool."""
    bl_idname  = 'image.modo_uv_drop_transform'
    bl_label   = 'Drop UV Transform'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (state._uv_active_transform_mode is not None
                and context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR')

    def execute(self, context):
        _uv_drop_transform(context)
        return {'FINISHED'}


class IMAGE_OT_modo_uv_handle_reposition(bpy.types.Operator):
    """LMB on the gizmo: drag-transform UVs or reposition the pivot."""
    bl_idname  = 'image.modo_uv_handle_reposition'
    bl_label   = 'Modo UV Handle Reposition'
    bl_options = {'REGISTER', 'UNDO'}

    transform_mode: EnumProperty(
        name='Mode', default='RESIZE', options={'HIDDEN'},
        items=[('TRANSLATE', 'Move', ''), ('ROTATE', 'Rotate', ''),
               ('RESIZE', 'Scale', '')],
    )
    scale_x: FloatProperty(name='Scale X', default=100.0,
                           soft_min=-500.0, soft_max=500.0, step=10, precision=1)
    scale_y: FloatProperty(name='Scale Y', default=100.0,
                           soft_min=-500.0, soft_max=500.0, step=10, precision=1)
    offset_u: FloatProperty(name='Offset U', default=0.0, step=1, precision=2)
    offset_v: FloatProperty(name='Offset V', default=0.0, step=1, precision=2)
    angle: FloatProperty(name='Angle', default=0.0, subtype='ANGLE', precision=2)
    center_u: FloatProperty(options={'HIDDEN'})
    center_v: FloatProperty(options={'HIDDEN'})
    original_targets_json: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    _HANDLE_HIT_RADIUS = 85.0
    _DRAG_THRESHOLD    = 3
    _ARM_LENGTH        = 80.0
    _ARM_GAP           = 10.0
    _ARM_PERP_TOL      = 16.0

    @classmethod
    def poll(cls, context):
        return (state._uv_active_transform_mode is not None
                and context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    # ── helpers ──────────────────────────────────────────────────────────────

    def _refresh_uv_positions(self, context, snapshot):
        _dbg = getattr(get_addon_preferences(context), 'debug_uv_handle', False)
        try:
            obj_by_name = {o.name: o for o in context.objects_in_mode_unique_data
                           if o.type == 'MESH'}
        except AttributeError:
            obj = context.edit_object
            obj_by_name = {obj.name: obj} if obj and obj.type == 'MESH' else {}
        if not obj_by_name:
            return snapshot
        bm_cache = {}
        refreshed = []
        for oname, fi, li, _u, _v in snapshot:
            if oname not in obj_by_name:
                continue
            if oname not in bm_cache:
                bm_t = bmesh.from_edit_mesh(obj_by_name[oname].data)
                uv_t = bm_t.loops.layers.uv.active
                if uv_t is None:
                    continue
                bm_t.faces.ensure_lookup_table()
                bm_cache[oname] = (bm_t, uv_t)
            bm_t, uv_t = bm_cache[oname]
            if fi < len(bm_t.faces):
                loops = bm_t.faces[fi].loops
                if li < len(loops):
                    uv = loops[li][uv_t].uv
                    refreshed.append((oname, fi, li, uv.x, uv.y))
        if _dbg:
            _uv_debug_log(f"[UV-HANDLE] _refresh_uv_positions: {len(refreshed)}/{len(snapshot)} valid")
        return refreshed if refreshed else snapshot

    def _apply_uvs(self, context, positions, finish=False):
        _dbg = getattr(get_addon_preferences(context), 'debug_uv_handle', False)
        try:
            obj_by_name = {o.name: o for o in context.objects_in_mode_unique_data
                           if o.type == 'MESH'}
        except AttributeError:
            obj = context.edit_object
            obj_by_name = {obj.name: obj} if obj and obj.type == 'MESH' else {}
        if not obj_by_name:
            return
        bm_cache = {}
        dirty = set()
        for oname, fi, li, u, v in positions:
            if oname not in obj_by_name:
                continue
            if oname not in bm_cache:
                bm_t = bmesh.from_edit_mesh(obj_by_name[oname].data)
                uv_t = bm_t.loops.layers.uv.verify()
                if uv_t is None:
                    continue
                bm_t.faces.ensure_lookup_table()
                bm_cache[oname] = (bm_t, uv_t)
            bm_t, uv_t = bm_cache[oname]
            if fi < len(bm_t.faces):
                loops = bm_t.faces[fi].loops
                if li < len(loops):
                    loops[li][uv_t].uv.x = u
                    loops[li][uv_t].uv.y = v
                    dirty.add(oname)
        for oname in dirty:
            bmesh.update_edit_mesh(obj_by_name[oname].data, destructive=finish)

    def execute(self, context):
        cx = self.center_u
        cy = self.center_v
        if cx == 0.0 and cy == 0.0 and state._uv_gizmo_center is not None:
            cx, cy = state._uv_gizmo_center
        targets = None
        if self.original_targets_json:
            targets = [tuple(t) for t in json.loads(self.original_targets_json)]
        if not targets:
            targets = _collect_uv_transform_targets(context)
        if not targets:
            return {'CANCELLED'}
        new_positions = []
        mode = self.transform_mode
        if mode == 'RESIZE':
            sx = self.scale_x / 100.0; sy = self.scale_y / 100.0
            for oname, fi, li, iu, iv in targets:
                new_positions.append((oname, fi, li, cx + (iu - cx) * sx, cy + (iv - cy) * sy))
        elif mode == 'TRANSLATE':
            du, dv = self.offset_u, self.offset_v
            for oname, fi, li, iu, iv in targets:
                new_positions.append((oname, fi, li, iu + du, iv + dv))
        elif mode == 'ROTATE':
            cos_a = math.cos(self.angle); sin_a = math.sin(self.angle)
            for oname, fi, li, iu, iv in targets:
                ox, oy = iu - cx, iv - cy
                new_positions.append((oname, fi, li,
                                      cx + ox * cos_a - oy * sin_a,
                                      cy + ox * sin_a + oy * cos_a))
        else:
            return {'CANCELLED'}
        self._apply_uvs(context, new_positions, finish=True)
        state._uv_transform_targets = list(new_positions)
        if mode == 'TRANSLATE':
            state._uv_gizmo_center = (cx + self.offset_u, cy + self.offset_v)
        else:
            state._uv_gizmo_center = (cx, cy)
        self._restore_uv_selection(context)
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        mode = self.transform_mode
        if mode == 'RESIZE':
            col = layout.column(align=True)
            col.prop(self, 'scale_x', text='Scale X %')
            col.prop(self, 'scale_y', text='Scale Y %')
        elif mode == 'TRANSLATE':
            layout.prop(self, 'offset_u')
            layout.prop(self, 'offset_v')
        elif mode == 'ROTATE':
            layout.prop(self, 'angle')

    # ── Selection highlight restore ───────────────────────────────────────────

    def _restore_uv_selection(self, context):
        try:
            snap = state._uv_sel_corner_set
            if not snap or not isinstance(snap, dict):
                return
            use_sync = snap['use_sync']
            sm       = snap['sm']
            edge_set   = snap['edges']
            vert_set   = snap['verts']
            face_set   = snap['faces']
            corner_set = snap['corners']
            if not (edge_set or vert_set or face_set or corner_set):
                return
            try:
                edit_objects = [o for o in context.objects_in_mode_unique_data
                                if o.type == 'MESH']
            except AttributeError:
                obj = context.edit_object
                edit_objects = [obj] if obj and obj.type == 'MESH' else []
            for obj in edit_objects:
                bm = bmesh.from_edit_mesh(obj.data)
                uv_layer = bm.loops.layers.uv.active
                if uv_layer is None:
                    continue
                bm.faces.ensure_lookup_table()
                oname = obj.name
                for face in bm.faces:
                    fi = face.index
                    for li, loop in enumerate(face.loops):
                        if use_sync:
                            if sm[2]:
                                sel = (oname, fi) in face_set
                                loop.uv_select_edge = sel
                                loop.uv_select_vert = sel
                                face.select = sel
                            elif sm[1]:
                                sel_edge = (oname, loop.edge.index) in edge_set
                                sel_vert = (oname, loop.vert.index) in vert_set
                                loop.uv_select_edge = sel_edge
                                loop.uv_select_vert = sel_vert
                                loop.edge.select    = sel_edge
                            else:
                                sel_uv = (oname, fi, li) in corner_set
                                loop.uv_select_vert = sel_uv
                                loop.uv_select_edge = False
                                loop.vert.select    = (oname, loop.vert.index) in vert_set
                        else:
                            sel = (oname, fi, li) in corner_set
                            loop.uv_select_vert = sel
                            loop.uv_select_edge = sel
                if use_sync and not sm[0]:
                    bm.select_flush_mode()
                bmesh.update_edit_mesh(obj.data, destructive=False)
            if context.area:
                context.area.tag_redraw()
        except Exception:
            pass

    @staticmethod
    def _build_sel_corner_set(context):
        try:
            try:
                obj_by_name = {o.name: o for o in context.objects_in_mode_unique_data
                               if o.type == 'MESH'}
            except AttributeError:
                obj = context.edit_object
                obj_by_name = {obj.name: obj} if obj and obj.type == 'MESH' else {}
            if not obj_by_name:
                return None
            ts = context.tool_settings
            use_sync = ts.use_uv_select_sync
            sm       = tuple(ts.mesh_select_mode)

            targets = (state._uv_sel_targets if state._uv_sel_targets is not None
                       else state._uv_transform_targets)
            if not targets:
                return None

            # Group targets by object name (5-tuple: oname, fi, li, u, v)
            by_obj = {}
            for oname, fi, li, _u, _v in targets:
                by_obj.setdefault(oname, []).append((fi, li))

            corners: set = set(); edges: set = set()
            verts: set = set();   faces: set = set()

            for oname, obj_items in by_obj.items():
                if oname not in obj_by_name:
                    continue
                bm = bmesh.from_edit_mesh(obj_by_name[oname].data)
                bm.faces.ensure_lookup_table()
                obj_verts = set()
                for fi, li in obj_items:
                    if fi < len(bm.faces):
                        loops = bm.faces[fi].loops
                        if li < len(loops):
                            corners.add((oname, fi, li))
                            vi = loops[li].vert.index
                            verts.add((oname, vi))
                            obj_verts.add(vi)
                            if use_sync and sm[2]:
                                faces.add((oname, fi))
                if use_sync and sm[1]:
                    for fi, li in obj_items:
                        if fi < len(bm.faces):
                            loops = bm.faces[fi].loops
                            if li < len(loops):
                                edge = loops[li].edge
                                if (edge.verts[0].index in obj_verts
                                        and edge.verts[1].index in obj_verts):
                                    edges.add((oname, edge.index))

            return {
                'corners':  frozenset(corners),
                'edges':    frozenset(edges),
                'verts':    frozenset(verts),
                'faces':    frozenset(faces),
                'use_sync': use_sync,
                'sm':       sm,
            }
        except Exception:
            return None

    # ── invoke ────────────────────────────────────────────────────────────────

    def invoke(self, context, event):
        if event.value == 'CLICK':
            self._restore_uv_selection(context)
            return {'FINISHED'}

        self.scale_x  = 100.0;  self.scale_y  = 100.0
        self.offset_u = 0.0;    self.offset_v = 0.0
        self.angle    = 0.0

        _dbg = getattr(get_addon_preferences(context), 'debug_uv_handle', False)

        state._uv_sel_corner_set = self._build_sel_corner_set(context)

        mx = event.mouse_region_x; my = event.mouse_region_y
        region = context.region;   sima = context.space_data

        # Auto-drop if external operation moved UVs
        if state._uv_transform_targets:
            try:
                obj_by_name_chk = {o.name: o for o in context.objects_in_mode_unique_data
                                   if o.type == 'MESH'}
            except AttributeError:
                obj_chk = context.edit_object
                obj_by_name_chk = {obj_chk.name: obj_chk} if obj_chk and obj_chk.type == 'MESH' else {}
            bm_chk_cache = {}
            for oname_chk, fi, li, u, v in state._uv_transform_targets[:5]:
                if oname_chk not in obj_by_name_chk:
                    continue
                if oname_chk not in bm_chk_cache:
                    bm_c = bmesh.from_edit_mesh(obj_by_name_chk[oname_chk].data)
                    uv_c = bm_c.loops.layers.uv.active
                    if uv_c is None:
                        continue
                    bm_c.faces.ensure_lookup_table()
                    bm_chk_cache[oname_chk] = (bm_c, uv_c)
                bm_c, uv_c = bm_chk_cache[oname_chk]
                if fi < len(bm_c.faces):
                    ls = bm_c.faces[fi].loops
                    if li < len(ls):
                        cur = ls[li][uv_c].uv
                        if abs(cur.x - u) > 1e-6 or abs(cur.y - v) > 1e-6:
                            _uv_drop_transform(context)
                            return {'PASS_THROUGH'}

        # Detect handle hit
        on_handle = False
        sc = None
        if state._uv_gizmo_center is not None:
            sc = _uv_view_to_region(region, sima,
                                    state._uv_gizmo_center[0], state._uv_gizmo_center[1])
            if sc is not None:
                dx = mx - sc[0]; dy = my - sc[1]
                on_handle = (math.sqrt(dx * dx + dy * dy) <= self._HANDLE_HIT_RADIUS)

        if on_handle:
            axis_constraint = None
            if sc is not None and state._uv_active_transform_mode in ('TRANSLATE', 'RESIZE'):
                lx = mx - sc[0]; ly = my - sc[1]
                if (self._ARM_GAP <= lx <= self._ARM_LENGTH + 5 and abs(ly) <= self._ARM_PERP_TOL):
                    axis_constraint = 'X'
                elif (self._ARM_GAP <= ly <= self._ARM_LENGTH + 5 and abs(lx) <= self._ARM_PERP_TOL):
                    axis_constraint = 'Y'

            self._mode     = state._uv_active_transform_mode
            self._axis     = axis_constraint
            self._start_mx = mx; self._start_my = my
            self._dragging = False

            _snapshot = (state._uv_transform_targets
                         if state._uv_transform_targets
                         else _collect_uv_transform_targets(context))
            self._uv_info = self._refresh_uv_positions(context, _snapshot)
            self._center_start = (state._uv_gizmo_center[0], state._uv_gizmo_center[1])

            self._last_sx = 1.0; self._last_sy = 1.0
            self._last_du = 0.0; self._last_dv = 0.0
            self._last_angle_rad = 0.0

            self._start_uv = _uv_region_to_view(region, sima, mx, my)
            _pivot_sc = _uv_view_to_region(region, sima,
                                           state._uv_gizmo_center[0],
                                           state._uv_gizmo_center[1])
            self._pivot_sx = _pivot_sc[0] if _pivot_sc else mx
            self._pivot_sy = _pivot_sc[1] if _pivot_sc else my
            self._start_mx_px = float(mx); self._start_my_px = float(my)
            self._eff_mx = float(mx); self._eff_my = float(my)
            self._prev_real_mx = float(mx); self._prev_real_my = float(my)
            self._snap_ctrl = event.ctrl
            self._detach_on_drag = event.shift
            self._suppress_slow_rate = event.shift

            self._restore_uv_selection(context)
            state._uv_handle_modal_active = True
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}

        # Click away — reposition gizmo pivot. Guard against clicks that land
        # outside the WINDOW region (header, toolbar, N-panel, menus, etc.).
        area = context.area
        if area:
            abs_mx, abs_my = event.mouse_x, event.mouse_y
            win_region = next(
                (r for r in area.regions if r.type == 'WINDOW'), None)
            if win_region is None:
                return {'PASS_THROUGH'}
            if not (win_region.x <= abs_mx < win_region.x + win_region.width and
                    win_region.y <= abs_my < win_region.y + win_region.height):
                return {'PASS_THROUGH'}
        if state._uv_snap_highlight is not None:
            uv_point = state._uv_snap_highlight['uv_pos']
        else:
            result = _uv_region_to_view(region, sima, mx, my)
            if result is None:
                return {'CANCELLED'}
            uv_point = result

            ts = context.tool_settings
            if _is_uv_snap_active(ts, event.ctrl):
                snap_els = _get_uv_snap_elements(ts)
                vertex_snapped = False
                if 'VERTEX' in snap_els:
                    du, dv, snap_tgt = _snap_uv_translate(
                        context, 0.0, 0.0, [],
                        ctrl_held=event.ctrl,
                        gizmo_center=uv_point,
                        mouse_screen=(mx, my))
                    if snap_tgt is not None:
                        uv_point = snap_tgt
                        vertex_snapped = True
                        sc_snap = _uv_view_to_region(region, sima, snap_tgt[0], snap_tgt[1])
                        if sc_snap is not None:
                            state._uv_snap_highlight = {
                                'screen_pos': sc_snap,
                                'uv_pos': snap_tgt,
                                'elem_type': 'SNAP_VERTEX',
                            }
                if not vertex_snapped and bool(snap_els & {'INCREMENT', 'GRID', 'PIXEL'}):
                    # Snap the click position to the UV grid
                    gu = gv = 0.125
                    if 'PIXEL' in snap_els:
                        gu, gv = _get_uv_grid_size(sima)
                    uv_point = (round(uv_point[0] / gu) * gu,
                                round(uv_point[1] / gv) * gv)

        state._uv_gizmo_center = uv_point
        _sync_uv_gizmo_center_to_bmesh(context)
        obj = context.edit_object
        if obj and obj.type == 'MESH':
            bmesh.update_edit_mesh(obj.data, destructive=False)
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}

    # ── modal ─────────────────────────────────────────────────────────────────

    def modal(self, context, event):
        _dbg = getattr(get_addon_preferences(context), 'debug_uv_handle', False)
        region = context.region; sima = context.space_data

        if event.type == 'MOUSEMOVE':
            mx = event.mouse_region_x; my = event.mouse_region_y
            self._snap_ctrl  = event.ctrl
            _rate = 0.1 if (event.shift and not self._suppress_slow_rate) else 1.0
            self._eff_mx += (mx - self._prev_real_mx) * _rate
            self._eff_my += (my - self._prev_real_my) * _rate
            self._prev_real_mx = mx; self._prev_real_my = my

            if not self._dragging:
                if (abs(mx - self._start_mx) <= self._DRAG_THRESHOLD
                        and abs(my - self._start_my) <= self._DRAG_THRESHOLD):
                    return {'RUNNING_MODAL'}
                self._dragging = True
                self._restore_uv_selection(context)
                if self._detach_on_drag:
                    # Use non-sticky targets so co-located sticky neighbours
                    # are seen as "outside" the selection and get ripped away.
                    rip_src = state._uv_sel_targets or self._uv_info
                    result = _do_uv_rip_from_targets(context, rip_src)
                    self._detach_on_drag = False
                    state._uv_transform_targets = _collect_uv_transform_targets(context)
                    state._uv_sel_targets = _collect_uv_transform_targets(
                        context, override_sticky='DISABLED')
                    self._uv_info = self._refresh_uv_positions(
                        context, state._uv_transform_targets)

            cx, cy = self._center_start
            eff_uv = _uv_region_to_view(region, sima,
                                        int(self._eff_mx), int(self._eff_my))
            curr_uv = _uv_region_to_view(region, sima, mx, my)
            if eff_uv is None: eff_uv = curr_uv
            if curr_uv is None or self._start_uv is None:
                return {'RUNNING_MODAL'}

            new_positions = []

            if self._mode == 'TRANSLATE':
                du = eff_uv[0] - self._start_uv[0]
                dv = eff_uv[1] - self._start_uv[1]
                du, dv, snap_target = _snap_uv_translate(
                    context, du, dv, self._uv_info,
                    ctrl_held=self._snap_ctrl,
                    gizmo_center=self._center_start,
                    mouse_screen=(mx, my))
                if snap_target is not None:
                    sc_snap = _uv_view_to_region(region, sima, snap_target[0], snap_target[1])
                    if sc_snap is not None:
                        state._uv_snap_highlight = {
                            'screen_pos': sc_snap,
                            'uv_pos': snap_target,
                            'elem_type': 'SNAP_VERTEX',
                        }
                    else:
                        state._uv_snap_highlight = None
                else:
                    state._uv_snap_highlight = None
                if self._axis == 'X': dv = 0.0
                elif self._axis == 'Y': du = 0.0
                self._last_du = du; self._last_dv = dv
                for oname, fi, li, iu, iv in self._uv_info:
                    new_positions.append((oname, fi, li, iu + du, iv + dv))
                state._uv_gizmo_center = (cx + du, cy + dv)

            elif self._mode == 'ROTATE':
                a_start = math.atan2(self._start_uv[1] - cy, self._start_uv[0] - cx)
                a_curr  = math.atan2(eff_uv[1] - cy, eff_uv[0] - cx)
                angle   = (a_curr - a_start + math.pi) % math.tau - math.pi
                ts = context.tool_settings
                if _is_uv_snap_active(ts, self._snap_ctrl):
                    snap_step = math.radians(15.0)
                    for attr in ('snap_angle_increment_2d', 'snap_angle_increment',
                                 'snap_rotate_angle_increment'):
                        val = getattr(ts, attr, None)
                        if val is not None and 1e-6 < val < math.tau:
                            snap_step = val; break
                    if snap_step > 1e-6:
                        angle = round(angle / snap_step) * snap_step
                self._last_angle_rad = angle
                cos_a = math.cos(angle); sin_a = math.sin(angle)
                for oname, fi, li, iu, iv in self._uv_info:
                    ox = iu - cx; oy = iv - cy
                    new_positions.append((oname, fi, li,
                                          cx + ox * cos_a - oy * sin_a,
                                          cy + ox * sin_a + oy * cos_a))

            elif self._mode == 'RESIZE':
                psx = self._pivot_sx; psy = self._pivot_sy
                sv_x = self._start_mx_px - psx; sv_y = self._start_my_px - psy
                d_start = math.sqrt(sv_x * sv_x + sv_y * sv_y)
                _MIN_REF = self._ARM_LENGTH * 0.5
                emx = getattr(self, '_eff_mx', float(mx))
                emy = getattr(self, '_eff_my', float(my))
                dmx = emx - self._start_mx_px; dmy = emy - self._start_my_px
                if self._axis == 'X':
                    scale = 1.0 + dmx / self._ARM_LENGTH
                elif self._axis == 'Y':
                    scale = 1.0 + dmy / self._ARM_LENGTH
                else:
                    if d_start >= _MIN_REF:
                        unit_x = sv_x / d_start; unit_y = sv_y / d_start
                    else:
                        unit_x, unit_y = 1.0, 0.0
                    projected_delta = dmx * unit_x + dmy * unit_y
                    scale = 1.0 + projected_delta / self._ARM_LENGTH
                ts = context.tool_settings
                if _is_uv_snap_active(ts, self._snap_ctrl):
                    step = 0.1
                    for attr in ('snap_scale_increment', 'snap_increment'):
                        val = getattr(ts, attr, None)
                        if val is not None and val > 1e-6:
                            step = val; break
                    scale = round(scale / step) * step
                sx = scale if self._axis != 'Y' else 1.0
                sy = scale if self._axis != 'X' else 1.0
                self._last_sx = sx; self._last_sy = sy
                for oname, fi, li, iu, iv in self._uv_info:
                    new_positions.append((oname, fi, li,
                                          cx + (iu - cx) * sx,
                                          cy + (iv - cy) * sy))

            # Keep operator properties in sync so the live redo panel shows current values
            self.transform_mode = self._mode
            if self._mode == 'TRANSLATE':
                self.offset_u = self._last_du
                self.offset_v = self._last_dv
            elif self._mode == 'ROTATE':
                self.angle = self._last_angle_rad
            elif self._mode == 'RESIZE':
                self.scale_x = self._last_sx * 100.0
                self.scale_y = self._last_sy * 100.0

            self._apply_uvs(context, new_positions)
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            state._uv_handle_modal_active = False
            _sync_uv_gizmo_center_to_bmesh(context)
            try:
                obj_by_name = {o.name: o for o in context.objects_in_mode_unique_data
                               if o.type == 'MESH'}
            except AttributeError:
                obj = context.edit_object
                obj_by_name = {obj.name: obj} if obj and obj.type == 'MESH' else {}
            if obj_by_name:
                # Group uv_info entries by object name
                by_obj = {}
                for entry in self._uv_info:
                    by_obj.setdefault(entry[0], []).append(entry)
                new_targets = []
                for oname, obj in obj_by_name.items():
                    obj_entries = by_obj.get(oname, [])
                    if not obj_entries:
                        continue
                    bmesh.update_edit_mesh(obj.data, destructive=True)
                    bm_post = bmesh.from_edit_mesh(obj.data)
                    uv_layer_post = bm_post.loops.layers.uv.verify()
                    if uv_layer_post is None:
                        continue
                    bm_post.faces.ensure_lookup_table()
                    moved_keys = set()
                    for oname2, fi, li, _iu, _iv in obj_entries:
                        if fi < len(bm_post.faces):
                            loops_post = list(bm_post.faces[fi].loops)
                            if li < len(loops_post):
                                uv_co = loops_post[li][uv_layer_post].uv
                                new_targets.append((oname, fi, li, uv_co.x, uv_co.y))
                                moved_keys.add((fi, li))
                    moved_verts = set()
                    for oname2, fi, li, _iu, _iv in obj_entries:
                        if fi < len(bm_post.faces):
                            loops_post = list(bm_post.faces[fi].loops)
                            if li < len(loops_post):
                                moved_verts.add(loops_post[li].vert.index)
                    cleared = 0
                    for face in bm_post.faces:
                        for li2, loop in enumerate(face.loops):
                            if loop.vert.index in moved_verts:
                                if (face.index, li2) not in moved_keys:
                                    if loop.uv_select_vert:
                                        loop.uv_select_vert = False
                                        cleared += 1
                    if cleared:
                        bmesh.update_edit_mesh(obj.data, destructive=False)
                state._uv_transform_targets = (new_targets if new_targets
                                               else _collect_uv_transform_targets(context))
                state._uv_sel_targets = _collect_uv_transform_targets(context, override_sticky='DISABLED')
            else:
                state._uv_transform_targets = _collect_uv_transform_targets(context)
                state._uv_sel_targets = _collect_uv_transform_targets(context, override_sticky='DISABLED')

            self._restore_uv_selection(context)

            if self._dragging:
                self.original_targets_json = json.dumps(self._uv_info)
            return {'FINISHED'}

        elif event.type in {'RIGHTMOUSE', 'ESC'}:
            state._uv_handle_modal_active = False
            self._apply_uvs(context, self._uv_info, finish=True)
            state._uv_gizmo_center = self._center_start
            _sync_uv_gizmo_center_to_bmesh(context)
            obj = context.edit_object
            if obj and obj.type == 'MESH':
                bmesh.update_edit_mesh(obj.data, destructive=False)
            if context.area:
                context.area.tag_redraw()
            state._uv_transform_targets = _collect_uv_transform_targets(context)
            state._uv_sel_targets = _collect_uv_transform_targets(context, override_sticky='DISABLED')
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}


class IMAGE_OT_modo_uv_selection_guard(bpy.types.Operator):
    """Running modal that sits at the top of Blender's modal stack and
    consumes all LMB events while the UV W/E/R gizmo is active.

    Blender's tool keymaps (UV Editor Tool: UV Transform, Tweak, etc.) fire
    BEFORE editor keymaps, so uv.select can fire before image.modo_uv_handle_reposition
    even if the latter has head=True.  A modal operator beats all keymaps, so this
    guard guarantees our gizmo handler owns every LMB click/press without any
    selection change in between.
    """
    bl_idname  = 'image.modo_uv_selection_guard'
    bl_label   = 'UV Selection Guard'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (state._uv_active_transform_mode is not None
                and context.region is not None
                and context.region.type == 'WINDOW'
                and context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    def invoke(self, context, event):
        if state._uv_selection_guard_running:
            return {'CANCELLED'}
        state._uv_selection_guard_running = True
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        # Exit when the transform tool is dropped.
        if state._uv_active_transform_mode is None:
            state._uv_selection_guard_running = False
            return {'FINISHED'}

        if event.type == 'LEFTMOUSE' and event.value in {'PRESS', 'CLICK'}:
            # Pass through Alt-modified clicks — these are view navigation
            # gestures (e.g. Alt+Shift+LMB pan) and must not be consumed.
            if event.alt:
                return {'PASS_THROUGH'}
            # Pass through clicks that land on the header, toolbar, sidebar, or
            # any other non-WINDOW region (e.g. snap menu in the header bar).
            # Use absolute screen coordinates so the check is region-agnostic.
            area = context.area
            if area:
                mx, my = event.mouse_x, event.mouse_y
                for reg in area.regions:
                    if reg.type == 'WINDOW':
                        continue
                    if (reg.x <= mx < reg.x + reg.width and
                            reg.y <= my < reg.y + reg.height):
                        return {'PASS_THROUGH'}
            # Delegate to handle_reposition; it will do the hit test and
            # either start a drag modal or reposition the pivot — all without
            # any selection change.
            try:
                bpy.ops.image.modo_uv_handle_reposition('INVOKE_DEFAULT')
            except Exception:
                pass
            # Consume this event so the tool-keymap uv.select never fires.
            return {'RUNNING_MODAL'}

        return {'PASS_THROUGH'}


def _do_uv_rip(context):
    """Core UV rip logic, callable without an operator instance.
    Returns True if any loops were ripped, False otherwise."""
    _TOL    = 6
    _OFFSET = 1e-5

    def _uv_key(uv):
        return (round(uv.x, _TOL), round(uv.y, _TOL))

    def _loop_edge_sel(loop, uv_layer):
        try:
            return loop.uv_select_edge
        except AttributeError:
            try:
                return loop[uv_layer].select_edge
            except (AttributeError, KeyError):
                return False

    def _fan_group_of(seed_loop, sel_eids):
        v = seed_loop.vert
        visited_id = set()
        queue = [seed_loop]
        group = []
        while queue:
            l = queue.pop()
            if id(l) in visited_id:
                continue
            if l.vert != v:
                continue
            visited_id.add(id(l))
            group.append(l)
            for e_loop in (l, l.link_loop_prev):
                if e_loop.edge.index in sel_eids:
                    continue
                rad = e_loop.link_loop_radial_next
                if rad is e_loop:
                    continue
                for candidate in rad.face.loops:
                    if candidate.vert == v and id(candidate) not in visited_id:
                        queue.append(candidate)
                        break
        return group

    def _targets_vertex(bm, uv_layer, sync):
        groups = defaultdict(list)
        for face in bm.faces:
            for loop in face.loops:
                is_sel = loop.vert.select if sync else loop.uv_select_vert
                if not is_sel:
                    continue
                k = (loop.vert.index, _uv_key(loop[uv_layer].uv))
                groups[k].append(loop)
        targets = set()
        for loops in groups.values():
            if len(loops) > 1:
                targets.update(loops[1:])
        return targets

    def _targets_edge(bm, uv_layer, sync):
        targets = set()
        visited = set()
        for face in bm.faces:
            for loop in face.loops:
                eid = loop.edge.index
                if eid in visited:
                    continue
                visited.add(eid)
                partner = loop.link_loop_radial_next
                if partner is loop:
                    continue
                if sync:
                    if not loop.edge.select:
                        continue
                    targets.add(partner)
                    targets.add(partner.link_loop_next)
                else:
                    a_sel = _loop_edge_sel(loop, uv_layer)
                    b_sel = _loop_edge_sel(partner, uv_layer)
                    if a_sel == b_sel:
                        continue
                    unsel = partner if a_sel else loop
                    targets.add(unsel)
                    targets.add(unsel.link_loop_next)
        if sync:
            sel_eids = frozenset(
                l.edge.index for f in bm.faces for l in f.loops if l.edge.select)
        else:
            sel_eids = frozenset(
                l.edge.index for f in bm.faces for l in f.loops
                if _loop_edge_sel(l, uv_layer))
        extra = set()
        seen_keys = set()
        for init in list(targets):
            uv_k = _uv_key(init[uv_layer].uv)
            k = (init.vert.index, uv_k)
            if k in seen_keys:
                continue
            seen_keys.add(k)
            for loop in _fan_group_of(init, sel_eids):
                if loop not in targets and _uv_key(loop[uv_layer].uv) == uv_k:
                    extra.add(loop)
        targets.update(extra)
        return targets

    def _targets_face(bm, uv_layer, sync):
        def is_face_sel(f):
            return f.select if sync else all(l.uv_select_vert for l in f.loops)
        targets = set()
        visited = set()
        for face in bm.faces:
            for loop in face.loops:
                eid = loop.edge.index
                if eid in visited:
                    continue
                visited.add(eid)
                partner = loop.link_loop_radial_next
                if partner is loop:
                    continue
                a_sel = is_face_sel(loop.face)
                b_sel = is_face_sel(partner.face)
                if a_sel == b_sel:
                    continue
                unsel = partner if a_sel else loop
                targets.add(unsel)
                targets.add(unsel.link_loop_next)
        return targets

    obj = context.edit_object
    if obj is None or obj.type != 'MESH':
        return False
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.active
    if uv_layer is None:
        return False
    ts   = context.tool_settings
    sync = ts.use_uv_select_sync
    if sync:
        sm      = ts.mesh_select_mode
        uv_mode = 'VERTEX' if sm[0] else ('EDGE' if sm[1] else 'FACE')
    else:
        uv_mode = ts.uv_select_mode
    if uv_mode == 'VERTEX':
        targets = _targets_vertex(bm, uv_layer, sync)
    elif uv_mode == 'EDGE':
        targets = _targets_edge(bm, uv_layer, sync)
    else:
        targets = _targets_face(bm, uv_layer, sync)
    if not targets:
        return False
    off = _OFFSET
    for loop in targets:
        loop[uv_layer].uv.x += off
        loop[uv_layer].uv.y += off
    bmesh.update_edit_mesh(obj.data, destructive=False)
    if context.area:
        context.area.tag_redraw()
    return True


class IMAGE_OT_modo_uv_rip(bpy.types.Operator):
    """Rip (Unstitch) UV connectivity along the selection.
Vertex: detach the selected UV vertices from unselected neighbours.
Edge: tear along selected UV edges.
Face: tear the outer boundary of the selected face(s)."""
    bl_idname  = 'image.modo_uv_rip'
    bl_label   = 'Modo UV Rip'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.area is not None
                and context.area.type == 'IMAGE_EDITOR'
                and context.edit_object is not None
                and context.edit_object.type == 'MESH')

    # -- execute ---------------------------------------------------------------

    def execute(self, context):
        if _do_uv_rip(context):
            self.report({'INFO'}, "Ripped UV loop(s)")
            return {'FINISHED'}
        self.report({'INFO'}, "Nothing to rip — select elements along a UV seam")
        return {'CANCELLED'}


def _do_uv_rip_from_targets(context, uv_targets):
    """Rip UV connectivity using the explicit transform-target list.

    More reliable than _do_uv_rip inside a running modal because it never
    reads the BMesh UV selection state — it uses the known (oname, fi, li, u, v)
    targets directly.

    For each selected loop in uv_targets, walks link_loops at that vertex and
    offsets any co-located loop that is NOT itself in the selected set, thereby
    splitting the UV connection.

    Returns True if any loops were ripped.
    """
    _TOL    = 6
    _OFFSET = 1e-5

    if not uv_targets:
        return False

    try:
        obj_by_name = {o.name: o for o in context.objects_in_mode_unique_data
                       if o.type == 'MESH'}
    except AttributeError:
        obj = context.edit_object
        obj_by_name = {obj.name: obj} if obj and obj.type == 'MESH' else {}
    if not obj_by_name:
        return False

    # Group targets by object name
    by_obj = {}
    for oname, fi, li, u, v in uv_targets:
        by_obj.setdefault(oname, []).append((fi, li, u, v))

    ripped_any = False
    for oname, items in by_obj.items():
        if oname not in obj_by_name:
            continue
        bm = bmesh.from_edit_mesh(obj_by_name[oname].data)
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            continue

        # Build a set of the actual BMLoop objects that belong to the selection
        # so we can exclude them quickly when walking link_loops.
        sel_loops = set()
        for fi, li, _u, _v in items:
            if fi < len(bm.faces):
                fls = list(bm.faces[fi].loops)
                if li < len(fls):
                    sel_loops.add(fls[li])

        offsets = []
        seen_ids = set()
        for fi, li, u, v in items:
            if fi >= len(bm.faces):
                continue
            fls = list(bm.faces[fi].loops)
            if li >= len(fls):
                continue
            sel_loop = fls[li]
            uv_key   = (round(u, _TOL), round(v, _TOL))

            # Walk every loop at this mesh vertex and collect those that are
            #  (a) not in the selected set, and
            #  (b) UV-co-located with the selected loop.
            for other in sel_loop.vert.link_loops:
                if other in sel_loops:
                    continue
                oid = id(other)
                if oid in seen_ids:
                    continue
                ouv = other[uv_layer].uv
                if (round(ouv.x, _TOL), round(ouv.y, _TOL)) == uv_key:
                    offsets.append(other)
                    seen_ids.add(oid)

        if offsets:
            for loop in offsets:
                loop[uv_layer].uv.x += _OFFSET
                loop[uv_layer].uv.y += _OFFSET
            bmesh.update_edit_mesh(obj_by_name[oname].data, destructive=False)
            ripped_any = True

    if ripped_any and context.area:
        context.area.tag_redraw()
    return ripped_any


# ── UV header patch ───────────────────────────────────────────────────────────

def _patched_image_ht_header_draw(self, context):
    """Replacement for IMAGE_HT_header.draw.

    Identical to the original except the ``use_uv_select_island`` toggle is
    replaced by the Material Mode button at the same header position (between
    the vertex/edge/face buttons and the sticky-select mode button).
    """
    layout = self.layout

    sima = context.space_data
    overlay = sima.overlay
    ima = sima.image
    iuser = sima.image_user
    tool_settings = context.tool_settings

    show_render = sima.show_render
    show_uvedit = sima.show_uvedit
    show_maskedit = sima.show_maskedit

    layout.template_header()

    if sima.mode != 'UV':
        layout.prop(sima, "ui_mode", text="")

    # UV editing.
    if show_uvedit:
        layout.prop(tool_settings, "use_uv_select_sync", text="")

        if tool_settings.use_uv_select_sync:
            if state._uv_material_mode_active:
                # Draw manual buttons with depress=False — material mode is its own mode.
                row = layout.row(align=True)
                is_vert, is_edge, is_face = tool_settings.mesh_select_mode
                row.operator('mesh.select_mode', text='', icon='VERTEXSEL',
                             depress=False).type = 'VERT'
                row.operator('mesh.select_mode', text='', icon='EDGESEL',
                             depress=False).type = 'EDGE'
                row.operator('mesh.select_mode', text='', icon='FACESEL',
                             depress=False).type = 'FACE'
            else:
                layout.template_edit_mode_selection()
        else:
            row = layout.row(align=True)
            uv_select_mode = tool_settings.uv_select_mode[:]
            _mat = state._uv_material_mode_active
            row.operator(
                "uv.select_mode", text="", icon='UV_VERTEXSEL',
                depress=(uv_select_mode == 'VERTEX') and not _mat,
            ).type = 'VERTEX'
            row.operator(
                "uv.select_mode", text="", icon='UV_EDGESEL',
                depress=(uv_select_mode == 'EDGE') and not _mat,
            ).type = 'EDGE'
            row.operator(
                "uv.select_mode", text="", icon='UV_FACESEL',
                depress=(uv_select_mode == 'FACE') and not _mat,
            ).type = 'FACE'

        # Material Mode button in place of the island-select toggle.
        layout.row(align=True).operator(
            'image.modo_uv_material_mode',
            text='',
            icon='MATERIAL',
            depress=state._uv_material_mode_active,
        )
        layout.prop(tool_settings, "uv_sticky_select_mode", icon_only=True)

    bpy.types.IMAGE_MT_editor_menus.draw_collapsible(context, layout)

    layout.separator_spacer()

    bpy.types.IMAGE_HT_header.draw_xform_template(layout, context)

    layout.template_ID(sima, "image", new="image.new", open="image.open")

    if show_maskedit:
        layout.template_ID(sima, "mask", new="mask.new")
        layout.prop(sima, "pivot_point", icon_only=True)

        row = layout.row(align=True)
        row.prop(tool_settings, "use_proportional_edit_mask", text="", icon_only=True)
        sub = row.row(align=True)
        sub.active = tool_settings.use_proportional_edit_mask
        sub.prop_with_popover(
            tool_settings,
            "proportional_edit_falloff",
            text="",
            icon_only=True,
            panel="IMAGE_PT_proportional_edit",
        )

    if not show_render:
        layout.prop(sima, "use_image_pin", text="", emboss=False)

    layout.separator_spacer()

    # Gizmo toggle & popover.
    row = layout.row(align=True)
    row.prop(sima, "show_gizmo", icon='GIZMO', text="")
    sub = row.row(align=True)
    sub.active = sima.show_gizmo
    sub.popover(panel="IMAGE_PT_gizmo_display", text="")

    # Overlay toggle & popover.
    row = layout.row(align=True)
    row.prop(overlay, "show_overlays", icon='OVERLAY', text="")
    sub = row.row(align=True)
    sub.active = overlay.show_overlays
    sub.popover(panel="IMAGE_PT_overlay", text="")

    if show_uvedit:
        mesh = context.edit_object.data
        layout.prop_search(mesh.uv_layers, "active", mesh, "uv_layers", text="")

    if ima:
        seq_scene = getattr(context, 'sequencer_scene', None)
        scene = context.scene

        if show_render and seq_scene and (seq_scene != scene):
            row = layout.row()
            row.prop(sima, "show_sequencer_scene", text="")

        if ima.is_stereo_3d:
            row = layout.row()
            row.prop(sima, "show_stereo_3d", text="")

        # layers.
        layout.template_image_layers(ima, iuser)

        # draw options.
        row = layout.row()
        row.prop(sima, "display_channels", icon_only=True)
