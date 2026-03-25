"""
Edit Mode selection operators:
  MESH_OT_modo_select_element_under_mouse  (click / double-click / paint)
  MESH_OT_modo_select_shortest_path        (select between)
  MESH_OT_modo_lasso_select                (freehand lasso)
"""

import bpy
import bmesh
import math
from mathutils import Vector
from bpy.props import IntProperty, EnumProperty, BoolProperty

from . import state
from .utils import get_addon_preferences, _uv_debug_log
from .raycast import raycast_with_tolerance, collect_edge_loop, collect_edge_loop_modo
from .raycast import select_connected_faces_from, select_connected_verts_from
from .shortest_path import (
    find_shortest_path_vertices,
    find_shortest_path_edges,
    find_shortest_path_faces,
)
from .utils import point_in_polygon
from . import preselect as _preselect_mod


# ── Pre-selection → selection bridge ─────────────────────────────────────────

def _candidate_from_highlight(context):
    """Return a raycast-compatible dict from the live pre-selection state.

    Reads state._preselect_hits[0] directly — the same data that was just
    rendered — so selection is guaranteed to match the visual highlight.
    Falls back to None if state is empty or doesn't match the current mode.
    """
    if not state._preselect_hits:
        return None
    sm  = context.tool_settings.mesh_select_mode
    hit = state._preselect_hits[0]  # sorted closest-first by _collect_edit_hits
    obj = hit.get('obj')
    if obj is None:
        return None
    htype = hit.get('type')
    if sm[0] and htype == 'VERT' and 'vert_index' in hit:
        return {'index': hit['vert_index'], 'obj': obj, 'location': None, 'normal': None}
    if sm[1] and htype == 'EDGE' and 'edge_index' in hit:
        return {'index': hit['edge_index'], 'obj': obj, 'location': None, 'normal': None,
                'hit_face_index': hit.get('face_index')}
    if sm[2] and htype == 'FACE' and 'face_index' in hit:
        return {'index': hit['face_index'], 'obj': obj, 'location': None, 'normal': None}
    return None


# ============================================================================
# Main Selection Operator
# ============================================================================

class MESH_OT_modo_select_element_under_mouse(bpy.types.Operator):
    """Modo-style mouse selection with tolerance.

    Based on Modo's select.3DElementUnderMouse command:
    - Supports set/add/toggle/remove modes
    - Pixel-based hit tolerance (lazy selection)
    - Double-click for loop selection
    - Right-click context menu with shortest path
    """
    bl_idname = "mesh.modo_select_element_under_mouse"
    bl_label = "Select Element Under Mouse (Modo Style)"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name="Mode",
        items=[
            ('set',    "Set",    "Replace selection (Modo default click)"),
            ('add',    "Add",    "Add to selection (Shift-click)"),
            ('toggle', "Toggle", "Toggle selection state"),
            ('remove', "Remove", "Remove from selection (Ctrl-click)"),
        ],
        default='set',
        options={'HIDDEN'},
    )

    mouse_x: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    mouse_y: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH' and
                context.object and
                context.object.type == 'MESH')

    def invoke(self, context, event):
        self.mouse_x = event.mouse_region_x
        self.mouse_y = event.mouse_region_y
        self.start_mouse_x = self.mouse_x
        self.start_mouse_y = self.mouse_y

        # Snapshot the pre-selection candidate on PRESS — reads the live
        # preselect state which is exactly what was just rendered.
        self._preselect_candidate = _candidate_from_highlight(context)

        if event.value == 'DOUBLE_CLICK':
            return self.execute_loop_selection(context)

        # ── Modo handle reposition (Edit Mode) ───────────────────────────────
        if state._active_transform_mode in ('TRANSLATE', 'ROTATE', 'RESIZE'):
            from bpy_extras import view3d_utils
            from .raycast import raycast_mesh
            from .transform_3d import _compute_selection_median, _start_anchor_timer, _start_pivot_crosshair
            coord  = (self.mouse_x, self.mouse_y)
            region = context.region
            rv3d   = context.region_data
            if state._snap_highlight is not None:
                world_point = state._snap_highlight['world_pos'].copy()
            else:
                hit = raycast_mesh(context, coord)
                if hit:
                    obj_hit = hit.get('obj', context.edit_object)
                    idx     = hit['index']
                    sm      = context.tool_settings.mesh_select_mode
                    mx      = obj_hit.matrix_world
                    bm_hit  = bmesh.from_edit_mesh(obj_hit.data)
                    if sm[0]:
                        bm_hit.verts.ensure_lookup_table()
                        world_point = (mx @ bm_hit.verts[idx].co
                                       if idx < len(bm_hit.verts)
                                       else hit['location'])
                    elif sm[1]:
                        bm_hit.edges.ensure_lookup_table()
                        if idx < len(bm_hit.edges):
                            e = bm_hit.edges[idx]
                            world_point = mx @ ((e.verts[0].co + e.verts[1].co) / 2.0)
                        else:
                            world_point = hit['location']
                    else:
                        bm_hit.faces.ensure_lookup_table()
                        world_point = (mx @ bm_hit.faces[idx].calc_center_median()
                                       if idx < len(bm_hit.faces)
                                       else hit['location'])
                else:
                    world_point = view3d_utils.region_2d_to_location_3d(
                        region, rv3d, coord, context.scene.cursor.location)
            if world_point is not None:
                if state._saved_pivot_point is None:
                    state._saved_pivot_point = context.scene.tool_settings.transform_pivot_point
                    state._saved_cursor_location = context.scene.cursor.location.copy()
                state._reposition_anchor = world_point.copy()
                state._last_known_median = _compute_selection_median(context)
                context.scene.cursor.location = world_point.copy()
                context.scene.tool_settings.transform_pivot_point = 'CURSOR'
                if state._active_transform_mode == 'TRANSLATE':
                    _start_anchor_timer()
                _start_pivot_crosshair()
            return {'FINISHED'}

        self.is_dragging = False
        self.drag_threshold = 3
        self._drag_cleared = False
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            delta_x = abs(event.mouse_region_x - self.start_mouse_x)
            delta_y = abs(event.mouse_region_y - self.start_mouse_y)
            if delta_x > self.drag_threshold or delta_y > self.drag_threshold:
                self.is_dragging = True
                if self.mode == 'set' and not self._drag_cleared:
                    for obj in context.objects_in_mode_unique_data:
                        if obj.type != 'MESH':
                            continue
                        bm_clear = bmesh.from_edit_mesh(obj.data)
                        for v in bm_clear.verts:  v.select = False
                        for e in bm_clear.edges:  e.select = False
                        for f in bm_clear.faces:  f.select = False
                        bm_clear.select_history.clear()
                        bmesh.update_edit_mesh(obj.data)
                    self._drag_cleared = True
                self.mouse_x = event.mouse_region_x
                self.mouse_y = event.mouse_region_y
                self.paint_selection(context)

        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if self.is_dragging:
                return {'FINISHED'}
            else:
                return self.execute(context)

        elif event.type in {'RIGHTMOUSE', 'ESC'}:
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    @staticmethod
    def _flush_uv_sync(bm, context):
        """Mirror BMesh mesh-level selection flags to UV loop flags.

        Only does work when use_uv_select_sync is ON.  Must be called
        after all mesh-flag writes and before bmesh.update_edit_mesh.
        """
        ts       = context.tool_settings
        use_sync = ts.use_uv_select_sync
        sm       = ts.mesh_select_mode
        mode_str = 'VERTEX' if sm[0] else ('EDGE' if sm[1] else 'FACE')
        n_sel_faces = sum(1 for f in bm.faces if f.select)
        n_sel_verts = sum(1 for v in bm.verts if v.select)
        _uv_debug_log(
            f"[UV-SYNC] _flush_uv_sync called: use_sync={use_sync} "
            f"mesh_mode={mode_str} sel_faces={n_sel_faces} sel_verts={n_sel_verts} "
            f"total_faces={len(bm.faces)}"
        )
        if not use_sync:
            return
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            return
        # In edge mode, skip select_flush_mode() — it promotes faces when all
        # their edges are selected (unlike Blender's own per-click selection).
        # Vert flags are already correct because e.select = True/False cascades
        # to verts automatically via BMesh.
        if not sm[1]:
            bm.select_flush_mode()
        if sm[1]:
            for face in bm.faces:
                for loop in face.loops:
                    loop.uv_select_edge = loop.edge.select
                    loop.uv_select_vert = loop.vert.select
        elif sm[0]:
            for face in bm.faces:
                for loop in face.loops:
                    loop.uv_select_vert = loop.vert.select
                    loop.uv_select_edge = False
        else:
            for face in bm.faces:
                for loop in face.loops:
                    loop.uv_select_vert = face.select
                    loop.uv_select_edge = face.select

    def _deselect_all_objects(self, context):
        """Clear selection on every mesh object currently in Edit Mode."""
        for obj in context.objects_in_mode_unique_data:
            if obj.type != 'MESH':
                continue
            bm = bmesh.from_edit_mesh(obj.data)
            for v in bm.verts: v.select = False
            for e in bm.edges: e.select = False
            for f in bm.faces: f.select = False
            bm.select_history.clear()
            self._flush_uv_sync(bm, context)
            bmesh.update_edit_mesh(obj.data)

    def paint_selection(self, context):
        """Paint selection while dragging — works across all objects in Edit Mode."""
        prefs      = get_addon_preferences(context)
        coord      = (self.mouse_x, self.mouse_y)
        hit_result = raycast_with_tolerance(context, coord, prefs.selection_tolerance)
        if hit_result:
            obj = hit_result.get('obj', context.edit_object)
            bm  = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            hit_index   = hit_result['index']
            select_mode = context.tool_settings.mesh_select_mode
            element = None
            if select_mode[0] and hit_index < len(bm.verts):
                element = bm.verts[hit_index]
            elif select_mode[1] and hit_index < len(bm.edges):
                element = bm.edges[hit_index]
            elif select_mode[2] and hit_index < len(bm.faces):
                element = bm.faces[hit_index]
            if element:
                element.select = (self.mode in {'set', 'add'})
                bmesh.update_edit_mesh(obj.data)

    def execute_loop_selection(self, context):
        """Modo-style double-click selection:
          - Face mode  : select the entire connected face island
          - Edge mode  : select the entire edge loop
          - Vertex mode: select all connected vertices
          - Empty space: deselect everything (set mode only)
        """
        prefs       = get_addon_preferences(context)
        coord       = (self.mouse_x, self.mouse_y)
        # Use element highlighted at click time; fall back to raycast if none captured
        hit_result  = getattr(self, '_preselect_candidate', None)
        if hit_result is None:
            hit_result = raycast_with_tolerance(context, coord, prefs.selection_tolerance)
        select_mode = context.tool_settings.mesh_select_mode

        # ── Edge mode + Add: expand ALL selected edges to loops ───────────────
        if select_mode[1] and self.mode == 'add':
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm_obj = bmesh.from_edit_mesh(obj.data)
                bm_obj.edges.ensure_lookup_table()
                seed_edges = [e for e in bm_obj.edges if e.select]
                if hit_result and hit_result.get('obj', context.edit_object) is obj:
                    hovered_idx = hit_result['index']
                    if hovered_idx < len(bm_obj.edges):
                        hovered_edge = bm_obj.edges[hovered_idx]
                        if hovered_edge not in seed_edges:
                            seed_edges.append(hovered_edge)
                if not seed_edges:
                    continue
                all_loop_edges = set()
                for seed in seed_edges:
                    all_loop_edges.update(collect_edge_loop_modo(seed))
                for e in all_loop_edges:
                    e.select = True
                self._flush_uv_sync(bm_obj, context)
                bmesh.update_edit_mesh(obj.data)
            return {'FINISHED'}

        # ── Empty space ───────────────────────────────────────────────────────
        if not hit_result:
            if self.mode == 'set':
                self._deselect_all_objects(context)
            elif self.mode == 'add':
                if select_mode[2]:
                    for obj in context.objects_in_mode_unique_data:
                        if obj.type != 'MESH':
                            continue
                        bm_obj = bmesh.from_edit_mesh(obj.data)
                        bm_obj.faces.ensure_lookup_table()
                        for seed in [f for f in bm_obj.faces if f.select]:
                            select_connected_faces_from(bm_obj, seed)
                        self._flush_uv_sync(bm_obj, context)
                        bmesh.update_edit_mesh(obj.data)
                elif select_mode[0]:
                    for obj in context.objects_in_mode_unique_data:
                        if obj.type != 'MESH':
                            continue
                        bm_obj = bmesh.from_edit_mesh(obj.data)
                        bm_obj.verts.ensure_lookup_table()
                        for seed in [v for v in bm_obj.verts if v.select]:
                            select_connected_verts_from(bm_obj, seed)
                        self._flush_uv_sync(bm_obj, context)
                        bmesh.update_edit_mesh(obj.data)
            return {'FINISHED'}

        obj      = hit_result.get('obj', context.edit_object)
        bm       = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        hit_index = hit_result['index']

        # ── Face mode → connected island ──────────────────────────────────────
        if select_mode[2]:
            if hit_index >= len(bm.faces):
                return {'FINISHED'}
            start_face = bm.faces[hit_index]
            if self.mode == 'set':
                self._deselect_all_objects(context)
                bm = bmesh.from_edit_mesh(obj.data)
                bm.faces.ensure_lookup_table()
                start_face = bm.faces[hit_index]
                select_connected_faces_from(bm, start_face)
            elif self.mode == 'add':
                select_connected_faces_from(bm, start_face)
            elif self.mode == 'remove':
                to_visit = [start_face]
                visited  = {start_face}
                start_face.select = False
                while to_visit:
                    face = to_visit.pop()
                    for edge in face.edges:
                        for linked_face in edge.link_faces:
                            if linked_face not in visited:
                                visited.add(linked_face)
                                linked_face.select = False
                                to_visit.append(linked_face)
            elif self.mode == 'toggle':
                new_state = not start_face.select
                to_visit  = [start_face]
                visited   = {start_face}
                start_face.select = new_state
                while to_visit:
                    face = to_visit.pop()
                    for edge in face.edges:
                        for linked_face in edge.link_faces:
                            if linked_face not in visited:
                                visited.add(linked_face)
                                linked_face.select = new_state
                                to_visit.append(linked_face)
            bm.faces.active = start_face
            self._flush_uv_sync(bm, context)
            bmesh.update_edit_mesh(obj.data)
            from .uv_overlays import _resync_uv_editor_selection
            _resync_uv_editor_selection(context, obj, select_mode, bm)

        # ── Edge mode → edge loop ─────────────────────────────────────────────
        elif select_mode[1]:
            if hit_index >= len(bm.edges):
                return {'FINISHED'}
            if self.mode == 'set':
                self._deselect_all_objects(context)
                bm = bmesh.from_edit_mesh(obj.data)
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
            clicked_edge = bm.edges[hit_index]
            # Resolve the raycasted face so Modo-style pole fallback wraps the
            # face the user was actually looking at.
            hit_face = None
            hf_idx = hit_result.get('hit_face_index')
            if hf_idx is not None and hf_idx < len(bm.faces):
                hit_face = bm.faces[hf_idx]
            loop_edges = collect_edge_loop_modo(clicked_edge, preferred_face=hit_face)
            if self.mode == 'set':
                for e in loop_edges: e.select = True
            elif self.mode == 'remove':
                for e in loop_edges: e.select = False
            elif self.mode == 'toggle':
                new_state = not clicked_edge.select
                for e in loop_edges: e.select = new_state
            self._flush_uv_sync(bm, context)
            bmesh.update_edit_mesh(obj.data)
            from .uv_overlays import _resync_uv_editor_selection
            _resync_uv_editor_selection(context, obj, select_mode, bm)

        # ── Vertex mode → connected mesh ──────────────────────────────────────
        else:
            if hit_index >= len(bm.verts):
                return {'FINISHED'}
            start_vert = bm.verts[hit_index]
            if self.mode == 'set':
                self._deselect_all_objects(context)
                bm = bmesh.from_edit_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                start_vert = bm.verts[hit_index]
                select_connected_verts_from(bm, start_vert)
            elif self.mode == 'add':
                select_connected_verts_from(bm, start_vert)
            elif self.mode == 'remove':
                to_visit = [start_vert]
                visited  = {start_vert}
                start_vert.select = False
                while to_visit:
                    vert = to_visit.pop()
                    for edge in vert.link_edges:
                        other = edge.other_vert(vert)
                        if other not in visited:
                            visited.add(other)
                            other.select = False
                            to_visit.append(other)
            elif self.mode == 'toggle':
                new_state = not start_vert.select
                to_visit  = [start_vert]
                visited   = {start_vert}
                start_vert.select = new_state
                while to_visit:
                    vert = to_visit.pop()
                    for edge in vert.link_edges:
                        other = edge.other_vert(vert)
                        if other not in visited:
                            visited.add(other)
                            other.select = new_state
                            to_visit.append(other)
            self._flush_uv_sync(bm, context)
            bmesh.update_edit_mesh(obj.data)

        return {'FINISHED'}

    def execute(self, context):
        select_mode = context.tool_settings.mesh_select_mode

        # The preselect highlight IS the selection preview.
        # Use ALL highlighted elements from state directly.
        # If there are none (cursor over empty space), fall back to raycast.
        hits = list(state._preselect_hits)
        if not hits:
            prefs      = get_addon_preferences(context)
            coord      = (self.mouse_x, self.mouse_y)
            ray_result = raycast_with_tolerance(context, coord, prefs.selection_tolerance)
            if ray_result:
                # Wrap single raycast result to reuse loop below
                hits = [{'type': (['VERT','EDGE','FACE'][[select_mode[0],select_mode[1],select_mode[2]].index(True)]),
                         'obj': ray_result.get('obj', context.edit_object),
                         'vert_index': ray_result['index'] if select_mode[0] else None,
                         'edge_index': ray_result['index'] if select_mode[1] else None,
                         'face_index': ray_result['index'] if select_mode[2] else None,
                         'hit_face_index': ray_result.get('hit_face_index')}]

        if not hits:
            if self.mode == 'set':
                self._deselect_all_objects(context)
            return {'FINISHED'}

        # ── Material Mode shortcut (delegates to first hit only) ──────────────
        first = hits[0]
        first_obj = first.get('obj', context.edit_object)
        first_bm  = bmesh.from_edit_mesh(first_obj.data)
        first_bm.faces.ensure_lookup_table()
        first_idx = first.get('face_index') if select_mode[2] else (
                    first.get('edge_index') if select_mode[1] else first.get('vert_index'))
        first_element = None
        if first_idx is not None:
            if select_mode[2] and first_idx < len(first_bm.faces):
                first_element = first_bm.faces[first_idx]
            elif select_mode[1]:
                first_bm.edges.ensure_lookup_table()
                if first_idx < len(first_bm.edges):
                    first_element = first_bm.edges[first_idx]
            elif select_mode[0]:
                first_bm.verts.ensure_lookup_table()
                if first_idx < len(first_bm.verts):
                    first_element = first_bm.verts[first_idx]

        if (state._material_mode_active and select_mode[2]
                and isinstance(first_element, bmesh.types.BMFace)):
            mat_nr      = first_element.material_index
            mats        = first_obj.data.materials
            clicked_mat = mats[mat_nr] if mat_nr < len(mats) else None
            if self.mode == 'set':
                self._deselect_all_objects(context)
            should_select = self.mode != 'remove'
            if self.mode == 'toggle':
                should_select = not first_element.select

            def _apply_material_selection(bm_obj, mesh_data):
                bm_obj.faces.ensure_lookup_table()
                slots = mesh_data.materials
                for f in bm_obj.faces:
                    fi       = f.material_index
                    face_mat = slots[fi] if fi < len(slots) else None
                    if face_mat is clicked_mat:
                        f.select = (False if self.mode == 'remove' else should_select)
                for v in bm_obj.verts:
                    v.select = any(f.select for f in v.link_faces)
                for e in bm_obj.edges:
                    e.select = (e.verts[0].select and e.verts[1].select
                                and any(f.select for f in e.link_faces))

            for edit_obj in context.objects_in_mode_unique_data:
                if edit_obj.type != 'MESH':
                    continue
                bm_cur = bmesh.from_edit_mesh(edit_obj.data)
                _apply_material_selection(bm_cur, edit_obj.data)
                if edit_obj is first_obj:
                    bm_cur.faces.active = first_element
                self._flush_uv_sync(bm_cur, context)
                bmesh.update_edit_mesh(edit_obj.data)
            return {'FINISHED'}

        # ── Normal selection: apply to EVERY highlighted element ──────────────
        if self.mode == 'set':
            self._deselect_all_objects(context)

        last_obj = None
        last_bm  = None
        for hit in hits:
            h_obj = hit.get('obj')
            if h_obj is None:
                continue
            bm = bmesh.from_edit_mesh(h_obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            element = None
            htype   = hit.get('type', '')
            if select_mode[0] and htype == 'VERT':
                vi = hit.get('vert_index')
                if vi is not None and vi < len(bm.verts):
                    element = bm.verts[vi]
            elif select_mode[1] and htype == 'EDGE':
                ei = hit.get('edge_index')
                if ei is not None and ei < len(bm.edges):
                    element = bm.edges[ei]
            elif select_mode[2] and htype == 'FACE':
                fi = hit.get('face_index')
                if fi is not None and fi < len(bm.faces):
                    element = bm.faces[fi]
            if element is None:
                continue

            if self.mode in ('set', 'add'):
                element.select = True
            elif self.mode == 'remove':
                element.select = False
            elif self.mode == 'toggle':
                element.select = not element.select

            last_obj = h_obj
            last_bm  = bm

        if last_bm is None:
            return {'FINISHED'}

        # Active element = first hit
        if first_element is not None:
            if select_mode[2] and isinstance(first_element, bmesh.types.BMFace):
                first_bm.faces.active = first_element
            elif select_mode[0] and isinstance(first_element, bmesh.types.BMVert):
                first_bm.select_history.clear()
                first_bm.select_history.add(first_element)

        self._flush_uv_sync(last_bm, context)
        bmesh.update_edit_mesh(last_obj.data)
        from .uv_overlays import _resync_uv_editor_selection
        _resync_uv_editor_selection(context, last_obj, select_mode, last_bm)
        return {'FINISHED'}


# ============================================================================
# Shortest Path Selection (Select Between)
# ============================================================================

class MESH_OT_modo_select_shortest_path(bpy.types.Operator):
    """Select shortest path between first and last selected elements.
    Replicates Modo's select.shortestPath / select.between command.
    """
    bl_idname = "mesh.modo_select_shortest_path"
    bl_label = "Select Shortest Path (Between)"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    use_3d: BoolProperty(
        name="3D Topology",
        description="Use 3D distance (True) or edge count (False). Modo's 'space' argument",
        default=True,
        options={'HIDDEN'},
    )

    mouse_x: IntProperty(default=0, options={'HIDDEN'})
    mouse_y: IntProperty(default=0, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH' and
                context.object and
                context.object.type == 'MESH')

    def invoke(self, context, event):
        self.mouse_x = event.mouse_region_x
        self.mouse_y = event.mouse_region_y
        return self.execute(context)

    def execute(self, context):
        mesh = context.edit_object
        bm   = bmesh.from_edit_mesh(mesh.data)
        select_mode = context.tool_settings.mesh_select_mode

        if self.mouse_x != 0 or self.mouse_y != 0:
            prefs      = get_addon_preferences(context)
            coord      = (self.mouse_x, self.mouse_y)
            hit_result = raycast_with_tolerance(context, coord, prefs.selection_tolerance)
            if hit_result:
                hit_obj = hit_result.get('obj', mesh)
                hit_bm  = bmesh.from_edit_mesh(hit_obj.data)
                if select_mode[0] and 'index' in hit_result:
                    hit_bm.verts.ensure_lookup_table()
                    if hit_result['index'] < len(hit_bm.verts):
                        hit_bm.verts[hit_result['index']].select = True
                        bmesh.update_edit_mesh(hit_obj.data)
                elif select_mode[1] and 'index' in hit_result:
                    hit_bm.edges.ensure_lookup_table()
                    if hit_result['index'] < len(hit_bm.edges):
                        hit_bm.edges[hit_result['index']].select = True
                        bmesh.update_edit_mesh(hit_obj.data)
                elif select_mode[2] and 'index' in hit_result:
                    hit_bm.faces.ensure_lookup_table()
                    if hit_result['index'] < len(hit_bm.faces):
                        hit_bm.faces[hit_result['index']].select = True
                        bmesh.update_edit_mesh(hit_obj.data)

        if select_mode[0]:
            selected = [v for v in bm.verts if v.select]
            if len(selected) < 2:
                self.report({'WARNING'}, "Select at least 2 vertices")
                return {'CANCELLED'}
            path = find_shortest_path_vertices(bm, selected[0], selected[-1], self.use_3d)
            if not path:
                self.report({'WARNING'}, "No path found")
                return {'CANCELLED'}
            for v in bm.verts: v.select = False
            for v in path:     v.select = True
            bmesh.update_edit_mesh(mesh.data)

        elif select_mode[1]:
            selected = [e for e in bm.edges if e.select]
            if len(selected) < 2:
                self.report({'WARNING'}, "Select at least 2 edges")
                return {'CANCELLED'}
            start, end = selected[0], selected[-1]
            shared_verts = set(start.verts) & set(end.verts)
            if shared_verts:
                path = find_shortest_path_edges(bm, start, end, self.use_3d, use_ring=False)
            else:
                loop_path = find_shortest_path_edges(bm, start, end, self.use_3d, use_ring=False)
                ring_path = find_shortest_path_edges(bm, start, end, self.use_3d, use_ring=True)
                if ring_path and (not loop_path or len(ring_path) <= len(loop_path)):
                    path = ring_path
                else:
                    path = loop_path
            if not path:
                self.report({'WARNING'}, "No path found")
                return {'CANCELLED'}
            for e in bm.edges: e.select = False
            for v in bm.verts: v.select = False
            for f in bm.faces: f.select = False
            for e in path:     e.select = True
            bmesh.update_edit_mesh(mesh.data)

        elif select_mode[2]:
            selected = [f for f in bm.faces if f.select]
            if len(selected) < 2:
                self.report({'WARNING'}, "Select at least 2 faces")
                return {'CANCELLED'}
            path = find_shortest_path_faces(bm, selected[0], selected[-1], self.use_3d)
            if not path:
                self.report({'WARNING'}, "No path found")
                return {'CANCELLED'}
            for f in bm.faces: f.select = False
            for f in path:     f.select = True
            bmesh.update_edit_mesh(mesh.data)

        return {'FINISHED'}


# ============================================================================
# Lasso Selection
# ============================================================================

class MESH_OT_modo_lasso_select(bpy.types.Operator):
    """Lasso selection — right-click drag draws a freehand selection area.

    Replicates Modo's lasso selection system:
    - Right-click drag: lasso with backface cull in Shaded, through in Wireframe
    - Middle-click drag: inverts the backface cull behaviour
    - Shift: add to selection
    - Ctrl: remove from selection
    """
    bl_idname = "mesh.modo_lasso_select"
    bl_label = "Lasso Select (Modo Style)"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name="Mode",
        items=[
            ('set',    "Set",    "Replace selection"),
            ('add',    "Add",    "Add to selection (Shift)"),
            ('remove', "Remove", "Remove from selection (Ctrl)"),
        ],
        default='set',
        options={'HIDDEN'},
    )

    use_middle_click: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH' and
                context.object is not None and
                context.object.type == 'MESH')

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _is_wireframe(self, context):
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        return space.shading.type == 'WIREFRAME'
        return False

    def _is_backfacing(self, context, face, mat, rv3d):
        normal_world = (mat.to_3x3() @ face.normal).normalized()
        if rv3d.is_perspective:
            face_center = mat @ face.calc_center_median()
            cam_pos  = rv3d.view_matrix.inverted().translation
            view_dir = (face_center - cam_pos).normalized()
        else:
            m = rv3d.view_matrix
            view_dir = Vector((m[2][0], m[2][1], m[2][2])).normalized()
        return normal_world.dot(-view_dir) < 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # Draw
    # ──────────────────────────────────────────────────────────────────────────

    def _draw_lasso_callback(self, context):
        import gpu
        from gpu_extras.batch import batch_for_shader

        pts = self.lasso_points
        if len(pts) < 2:
            return
        try:
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            gpu.state.blend_set('ALPHA')
            shader.bind()

            if len(pts) >= 3:
                def _dp_simplify(points, epsilon):
                    if len(points) < 3:
                        return points
                    ax, ay = points[0];  bx, by = points[-1]
                    dx, dy = bx - ax, by - ay
                    length_sq = dx * dx + dy * dy
                    max_dist, max_idx = 0.0, 0
                    for i in range(1, len(points) - 1):
                        px, py = points[i]
                        if length_sq == 0:
                            d = math.sqrt((px - ax)**2 + (py - ay)**2)
                        else:
                            t = max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / length_sq))
                            d = math.sqrt((px-(ax+t*dx))**2 + (py-(ay+t*dy))**2)
                        if d > max_dist:
                            max_dist, max_idx = d, i
                    if max_dist > epsilon:
                        left  = _dp_simplify(points[:max_idx+1], epsilon)
                        right = _dp_simplify(points[max_idx:],   epsilon)
                        return left[:-1] + right
                    return [points[0], points[-1]]

                simplified = _dp_simplify(pts, 2.0)
                if len(simplified) < 3:
                    simplified = pts
                from mathutils.geometry import tessellate_polygon
                pts3d = [(p[0], p[1], 0.0) for p in simplified]
                try:
                    tri_indices = tessellate_polygon([pts3d])
                    tris = []
                    for tri in tri_indices:
                        for idx in tri:
                            tris.append(simplified[idx])
                    if tris:
                        shader.uniform_float("color", (0.5, 0.5, 0.5, 0.15))
                        batch_for_shader(shader, 'TRIS', {"pos": tris}).draw(shader)
                except Exception:
                    pass

            closed = pts + [pts[0]]
            DASH, GAP = 4, 4
            PERIOD = DASH + GAP

            def build_dash_coords(polyline, phase_offset):
                result = []
                phase = phase_offset % PERIOD
                for i in range(len(polyline) - 1):
                    ax, ay = polyline[i]; bx, by = polyline[i+1]
                    seg_len = math.sqrt((bx-ax)**2 + (by-ay)**2)
                    if seg_len == 0:
                        continue
                    dx = (bx-ax)/seg_len; dy = (by-ay)/seg_len
                    t = 0.0
                    while t < seg_len:
                        cycle_pos = phase % PERIOD
                        remaining = PERIOD - cycle_pos
                        step = min(remaining, seg_len - t)
                        if cycle_pos < DASH:
                            drawn = min(DASH - cycle_pos, step)
                            result.append((ax + dx*t, ay + dy*t))
                            result.append((ax + dx*(t+drawn), ay + dy*(t+drawn)))
                            phase += drawn; t += drawn
                        else:
                            skipped = min(GAP - (cycle_pos - DASH), step)
                            phase += skipped; t += skipped
                return result

            base_batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": closed})
            gpu.state.line_width_set(1.0)
            shader.uniform_float("color", (0.0, 0.0, 0.0, 1.0))
            base_batch.draw(shader)

            white_coords = build_dash_coords(closed, 0)
            if white_coords:
                shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
                batch_for_shader(shader, 'LINES', {"pos": white_coords}).draw(shader)

        except Exception as e:
            print(f"[LASSO DRAW ERROR] {e}")
        finally:
            import gpu as _gpu
            _gpu.state.blend_set('NONE')
            _gpu.state.line_width_set(1.0)

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    def invoke(self, context, event):
        self.use_middle_click = (event.type == 'MIDDLEMOUSE')
        self.lasso_points = [(event.mouse_region_x, event.mouse_region_y)]
        self._start_x = event.mouse_region_x
        self._start_y = event.mouse_region_y
        self._draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_lasso_callback, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if context.area:
            context.area.tag_redraw()

        if event.type == 'MOUSEMOVE':
            self.lasso_points.append((event.mouse_region_x, event.mouse_region_y))

        elif event.type in {'RIGHTMOUSE', 'MIDDLEMOUSE'} and event.value in {'RELEASE', 'CLICK'}:
            dx = event.mouse_region_x - self._start_x
            dy = event.mouse_region_y - self._start_y
            drag_dist = math.sqrt(dx*dx + dy*dy) if event.value != 'CLICK' else 0.0
            self._remove_draw_handler()
            if context.area:
                context.area.tag_redraw()

            is_click   = drag_dist < 10
            event_shift = event.shift or (self.mode == 'add')

            if is_click:
                prefs = get_addon_preferences(context)
                _MOUSE_KEYS = {'RIGHTMOUSE', 'MIDDLEMOUSE'}
                sp_key = prefs.shortest_path_key
                is_sp_trigger = (
                    sp_key in _MOUSE_KEYS
                    and event.type == sp_key
                    and prefs.shortest_path_shift == event_shift
                    and prefs.shortest_path_ctrl == event.ctrl
                    and prefs.shortest_path_alt == event.alt
                )
                if is_sp_trigger:
                    bpy.ops.mesh.modo_select_shortest_path('INVOKE_DEFAULT')
                    return {'FINISHED'}
                if event.type == 'RIGHTMOUSE':
                    if self.mode == 'set':
                        menu = state._saved_rmb_menus.get('Mesh', 'VIEW3D_MT_edit_mesh_context_menu')
                        bpy.ops.wm.call_menu(name=menu)
                    return {'FINISHED'}

            if len(self.lasso_points) >= 3 and drag_dist >= 5:
                return self.execute(context)
            return {'CANCELLED'}

        elif event.type == 'ESC':
            self._remove_draw_handler()
            if context.area:
                context.area.tag_redraw()
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _remove_draw_handler(self):
        if self._draw_handler is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handler, 'WINDOW')
            self._draw_handler = None

    def execute(self, context):
        from bpy_extras import view3d_utils
        region      = context.region
        rv3d        = context.region_data
        select_mode = context.tool_settings.mesh_select_mode
        polygon     = self.lasso_points

        is_wireframe = self._is_wireframe(context)
        use_cull = (not self.use_middle_click) if not is_wireframe else self.use_middle_click

        if self.mode == 'set':
            bpy.ops.mesh.select_all(action='DESELECT')

        do_select = (self.mode != 'remove')

        for obj in context.objects_in_mode_unique_data:
            if obj.type != 'MESH':
                continue
            mat = obj.matrix_world
            bm  = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

            if select_mode[2]:
                for face in bm.faces:
                    all_inside = True
                    for vert in face.verts:
                        sc = view3d_utils.location_3d_to_region_2d(region, rv3d, mat @ vert.co)
                        if not sc or not point_in_polygon(sc, polygon):
                            all_inside = False
                            break
                    if not all_inside:
                        continue
                    if use_cull and self._is_backfacing(context, face, mat, rv3d):
                        continue
                    face.select = do_select
            elif select_mode[1]:
                for edge in bm.edges:
                    all_inside = True
                    for vert in edge.verts:
                        sc = view3d_utils.location_3d_to_region_2d(region, rv3d, mat @ vert.co)
                        if not sc or not point_in_polygon(sc, polygon):
                            all_inside = False
                            break
                    if not all_inside:
                        continue
                    if use_cull and edge.link_faces:
                        if all(self._is_backfacing(context, f, mat, rv3d) for f in edge.link_faces):
                            continue
                    edge.select = do_select
            elif select_mode[0]:
                for vert in bm.verts:
                    vert_world = mat @ vert.co
                    sc = view3d_utils.location_3d_to_region_2d(region, rv3d, vert_world)
                    if not sc or not point_in_polygon(sc, polygon):
                        continue
                    if use_cull and vert.link_faces:
                        if all(self._is_backfacing(context, f, mat, rv3d) for f in vert.link_faces):
                            continue
                    vert.select = do_select

            bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}
