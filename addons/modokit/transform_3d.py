"""
3-D viewport transform helpers and operators:
  helpers: _has_any_selection, _implicit_select/deselect_all_geometry
  _drop_transform       — drops the active W/E/R gizmo
  _compute_selection_median, anchor timer, pivot crosshair overlay
  snap highlight: _find_snap_target, VIEW3D_OT_modo_snap_highlight
  VIEW3D_OT_modo_transform  (W / E / R)
  VIEW3D_OT_modo_drop_transform
  VIEW3D_OT_modo_screen_move
"""

import math
import bpy
import bmesh
from bpy.props import EnumProperty
from mathutils import Vector as _Vector, Matrix as _Matrix

from . import state
from .utils import get_addon_preferences, _diag


# ── Geometry-selection helpers ────────────────────────────────────────────────

def _has_any_selection(context):
    """Return True if anything is selected in the current mode."""
    try:
        if context.mode == 'EDIT_MESH':
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm_obj = bmesh.from_edit_mesh(obj.data)
                sm = context.tool_settings.mesh_select_mode
                if sm[0] and any(v.select for v in bm_obj.verts):
                    return True
                if sm[1] and any(e.select for e in bm_obj.edges):
                    return True
                if sm[2] and any(f.select for f in bm_obj.faces):
                    return True
        elif context.mode == 'OBJECT':
            return bool(context.selected_objects)
    except Exception:
        pass
    return False


def _implicit_select_all_geometry(context):
    """Select all geometry/objects for the implicit 'nothing selected' state."""
    try:
        if context.mode == 'EDIT_MESH':
            bpy.ops.mesh.select_all(action='SELECT')
        elif context.mode == 'OBJECT':
            bpy.ops.object.select_all(action='SELECT')
    except Exception:
        pass


def _implicit_deselect_all_geometry(context):
    """Undo the implicit select-all when the tool is dropped."""
    try:
        if context.mode == 'EDIT_MESH':
            bpy.ops.mesh.select_all(action='DESELECT')
        elif context.mode == 'OBJECT':
            bpy.ops.object.select_all(action='DESELECT')
    except Exception:
        pass


# ── Drop-transform ────────────────────────────────────────────────────────────

def _drop_transform(context):
    """Drop whichever W/E/R gizmo is active, restoring pivot and cursor.
    Safe to call even when no transform tool is active."""
    if state._active_transform_mode is None:
        return
    attr_map = {
        'TRANSLATE': 'show_gizmo_object_translate',
        'ROTATE':    'show_gizmo_object_rotate',
        'RESIZE':    'show_gizmo_object_scale',
    }
    sv3d = getattr(context, 'space_data', None)
    if sv3d and sv3d.type == 'VIEW_3D':
        attr = attr_map.get(state._active_transform_mode)
        if attr:
            setattr(sv3d, attr, False)
    _stop_snap_highlight()
    _stop_anchor_timer()
    _stop_scale_gizmo()   # also restores show_gizmo_tool = True
    _stop_pivot_crosshair()
    try:
        bpy.ops.wm.tool_set_by_id(name='builtin.select_box')
    except Exception:
        pass
    if state._saved_pivot_point is not None:
        context.scene.tool_settings.transform_pivot_point = state._saved_pivot_point
        state._saved_pivot_point = None
    if state._saved_cursor_location is not None:
        context.scene.cursor.location = state._saved_cursor_location.copy()
        state._saved_cursor_location = None
    if state._saved_snap_target is not None:
        ts = context.tool_settings
        for _attr in ('snap_target', 'snap_source'):
            if hasattr(ts, _attr):
                try:
                    setattr(ts, _attr, state._saved_snap_target)
                except Exception:
                    pass
                break
        state._saved_snap_target = None
    state._reposition_anchor     = None
    state._last_known_median     = None
    state._active_transform_mode = None
    if state._implicit_select_all:
        _implicit_deselect_all_geometry(context)
        state._implicit_select_all = False


# ── Selection median ──────────────────────────────────────────────────────────

def _compute_selection_median(context):
    """Return the world-space median of the current selection, or None."""
    from mathutils import Vector
    try:
        if context.mode == 'EDIT_MESH':
            coords = []
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm_obj = bmesh.from_edit_mesh(obj.data)
                mx = obj.matrix_world
                sm = context.tool_settings.mesh_select_mode
                if sm[0]:
                    coords.extend(mx @ v.co for v in bm_obj.verts if v.select)
                elif sm[1]:
                    for e in bm_obj.edges:
                        if e.select:
                            coords.append(mx @ ((e.verts[0].co + e.verts[1].co) / 2.0))
                else:
                    for f in bm_obj.faces:
                        if f.select:
                            coords.append(mx @ f.calc_center_median())
            if coords:
                return sum(coords, Vector()) / len(coords)
        elif context.mode == 'OBJECT':
            sel = list(context.selected_objects)
            if sel:
                coords = [o.matrix_world.translation.copy() for o in sel]
                return sum(coords, Vector()) / len(coords)
    except Exception:
        pass
    return None


# ── Anchor tracking timer ─────────────────────────────────────────────────────

def _anchor_tracking_timer():
    """Polling timer: runs every ~16 ms (≈60 fps) while the Move tool is active.
    Tracks geometry movement and keeps the cursor/gizmo pinned to the anchor."""
    if state._active_transform_mode != 'TRANSLATE' or state._reposition_anchor is None:
        state._anchor_timer_running = False
        return None  # stop timer

    # Skip bmesh access while an unsafe modal (e.g. edge_slide) is live.
    if state._mesh_modal_unsafe:
        _diag("anchor_tracking_timer: SKIPPED (unsafe modal)")
        return state._viewport_draw_interval

    try:
        ctx = bpy.context
        _diag("anchor_tracking_timer: calling _compute_selection_median")
        new_median = _compute_selection_median(ctx)
        if new_median is not None and state._last_known_median is not None:
            delta = new_median - state._last_known_median
            if delta.length > 1e-6:
                new_anchor = state._reposition_anchor + delta
                ctx.scene.cursor.location = new_anchor.copy()
                state._reposition_anchor  = new_anchor
                state._last_known_median  = new_median
        elif new_median is not None and state._last_known_median is None:
            state._last_known_median = new_median

        # Force viewport redraw so the crosshair updates at viewport FPS even
        # when no other event is triggering a redraw.  The actual redraw rate
        # is self-calibrated from the draw callback's own fire interval.
        for window in ctx.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass

    return state._viewport_draw_interval  # reschedule to match viewport FPS


def _start_anchor_timer():
    """Start the anchor-tracking timer if not already running."""
    if not state._anchor_timer_running:
        bpy.app.timers.register(_anchor_tracking_timer, first_interval=0.016)
        state._anchor_timer_running = True


def _stop_anchor_timer():
    """Stop the anchor-tracking timer (called on W toggle-off)."""
    state._anchor_timer_running = False


# ── Pivot crosshair overlay ───────────────────────────────────────────────────

def _pivot_crosshair_draw_callback():
    """GPU POST_VIEW callback: draw a 3D world-space crosshair at the pivot."""
    if state._reposition_anchor is None:
        return
    try:
        import time as _t
        import gpu
        from gpu_extras.batch import batch_for_shader
        from mathutils import Vector, Matrix

        # Measure real viewport frame time so the anchor timer can match it.
        now = _t.monotonic()
        if state._last_crosshair_draw_time > 0.0:
            interval = now - state._last_crosshair_draw_time
            if 0.004 < interval < 0.25:   # sane range: 4 Hz – 250 Hz
                state._viewport_draw_interval = (
                    state._viewport_draw_interval * 0.85 + interval * 0.15
                )
        state._last_crosshair_draw_time = now
        ctx  = bpy.context
        rv3d = ctx.region_data
        if rv3d is None:
            return

        center = Vector(state._reposition_anchor)
        cam_pos = Matrix(rv3d.view_matrix).inverted().col[3].xyz
        dist    = (center - cam_pos).length
        arm     = max(0.005, dist * 0.0070)

        cx, cy, cz = center
        lines = [
            (cx - arm, cy, cz), (cx + arm, cy, cz),
            (cx, cy - arm, cz), (cx, cy + arm, cz),
            (cx, cy, cz - arm), (cx, cy, cz + arm),
        ]

        CYAN  = (0.0, 0.85, 1.0, 1.0)

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        shader.bind()

        gpu.state.line_width_set(2.0)
        shader.uniform_float('color', CYAN)
        batch_for_shader(shader, 'LINES', {'pos': lines}).draw(shader)

        gpu.state.line_width_set(1.0)
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _start_pivot_crosshair():
    """Register the pivot crosshair draw handler if not already running."""
    if state._pivot_crosshair_draw_handle is None:
        state._pivot_crosshair_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _pivot_crosshair_draw_callback, (), 'WINDOW', 'POST_VIEW')


def _stop_pivot_crosshair():
    """Remove the pivot crosshair draw handler."""
    if state._pivot_crosshair_draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(
            state._pivot_crosshair_draw_handle, 'WINDOW')
        state._pivot_crosshair_draw_handle = None
    state._last_crosshair_draw_time = 0.0
    try:
        if bpy.context.area:
            bpy.context.area.tag_redraw()
    except Exception:
        pass


# ── Snap highlight ────────────────────────────────────────────────────────────

def _get_snap_elements(ts):
    """Return a set of active snap element type strings, or empty set."""
    try:
        return set(ts.snap_elements)
    except Exception:
        pass
    try:
        return set(ts.snap_elements_individual)
    except Exception:
        pass
    return set()


def _find_snap_target(context, mx, my):
    """Find the nearest snappable element within SNAP_SCREEN_RADIUS pixels.
    Returns a dict {'screen_pos', 'world_pos', 'elem_type'} or None."""
    from bpy_extras import view3d_utils

    SNAP_SCREEN_RADIUS = 20  # pixels

    region = context.region
    rv3d   = context.region_data
    if region is None or rv3d is None:
        return None

    ts = context.tool_settings
    if not ts.use_snap:
        return None

    snap_els      = _get_snap_elements(ts)
    want_vert     = 'VERTEX'        in snap_els
    want_edge_mid = 'EDGE_MIDPOINT' in snap_els or 'EDGE' in snap_els
    want_face     = 'FACE'          in snap_els or 'FACE_PROJECT' in snap_els
    if not (want_vert or want_edge_mid or want_face):
        return None

    # Honour the "Backface Culling" snap option added in Blender 3.x+
    cull = getattr(ts, 'use_snap_backface_culling', False)
    if cull:
        from mathutils import Vector as _V
        if rv3d.is_perspective:
            _cam_pos = rv3d.view_matrix.inverted().translation
            def _front_facing(co_world, n_world):
                to_cam = _cam_pos - co_world
                if to_cam.length < 1e-8:
                    return True
                return to_cam.dot(n_world) > 0.0
        else:
            _vfwd = (rv3d.view_matrix.inverted().to_3x3() @ _V((0.0, 0.0, -1.0))).normalized()
            def _front_facing(co_world, n_world):
                return _vfwd.dot(n_world) > 0.0
    else:
        def _front_facing(co_world, n_world):
            return True

    best_sdist = float('inf')
    best       = None

    def _check(co_world, elem_type):
        nonlocal best_sdist, best
        sc = view3d_utils.location_3d_to_region_2d(region, rv3d, co_world)
        if sc is None:
            return
        dx, dy = sc.x - mx, sc.y - my
        sd = math.sqrt(dx * dx + dy * dy)
        if sd < SNAP_SCREEN_RADIUS and sd < best_sdist:
            best_sdist = sd
            best = {'screen_pos': (sc.x, sc.y), 'world_pos': co_world.copy(),
                    'elem_type': elem_type}

    if context.mode == 'EDIT_MESH':
        for obj in context.objects_in_mode_unique_data:
            if obj.type != 'MESH':
                continue
            bm_obj = bmesh.from_edit_mesh(obj.data)
            mx_w   = obj.matrix_world
            mx_n   = mx_w.inverted_safe().transposed().to_3x3()
            if want_vert:
                bm_obj.verts.ensure_lookup_table()
                for v in bm_obj.verts:
                    co_w = mx_w @ v.co
                    if cull:
                        lf = v.link_faces
                        # A vertex is snappable if at least one adjacent face is front-facing.
                        # Using vertex normal would cull silhouette verts incorrectly.
                        if lf and not any(_front_facing(co_w, mx_n @ f.normal) for f in lf):
                            continue
                    _check(co_w, 'VERTEX')
            if want_edge_mid:
                bm_obj.edges.ensure_lookup_table()
                for e in bm_obj.edges:
                    mid = mx_w @ ((e.verts[0].co + e.verts[1].co) / 2.0)
                    if cull:
                        lf = e.link_faces
                        if lf and not any(_front_facing(mid, mx_n @ f.normal) for f in lf):
                            continue
                    _check(mid, 'EDGE_MIDPOINT')
            if want_face:
                bm_obj.faces.ensure_lookup_table()
                for f in bm_obj.faces:
                    co_w = mx_w @ f.calc_center_median()
                    if not _front_facing(co_w, mx_n @ f.normal):
                        continue
                    _check(co_w, 'FACE_CENTER')

        in_mode_names = {obj.name for obj in context.objects_in_mode_unique_data}
        depsgraph = context.evaluated_depsgraph_get()
        for obj in context.visible_objects:
            if obj.name in in_mode_names:
                continue
            if obj.type != 'MESH':
                continue
            obj_eval  = obj.evaluated_get(depsgraph)
            mesh_eval = obj_eval.to_mesh()
            if mesh_eval is None:
                continue
            try:
                mx_w  = obj.matrix_world
                mx_n  = mx_w.inverted_safe().transposed().to_3x3()
                verts = mesh_eval.vertices
                polys = mesh_eval.polygons
                if cull and (want_vert or want_edge_mid):
                    vert_poly_norms = [[] for _ in range(len(verts))]
                    edge_poly_norms = {}
                    for f in polys:
                        fn = f.normal
                        fv = list(f.vertices)
                        for vi in fv:
                            vert_poly_norms[vi].append(fn)
                        if want_edge_mid:
                            for i in range(len(fv)):
                                key = (min(fv[i], fv[(i+1) % len(fv)]),
                                       max(fv[i], fv[(i+1) % len(fv)]))
                                edge_poly_norms.setdefault(key, []).append(fn)
                if want_vert:
                    for v in verts:
                        co_w = mx_w @ v.co
                        if cull:
                            pns = vert_poly_norms[v.index]
                            if pns and not any(_front_facing(co_w, mx_n @ n) for n in pns):
                                continue
                        _check(co_w, 'VERTEX')
                if want_edge_mid:
                    for e in mesh_eval.edges:
                        vi0, vi1 = e.vertices[0], e.vertices[1]
                        mid = mx_w @ ((verts[vi0].co + verts[vi1].co) / 2.0)
                        if cull:
                            key = (min(vi0, vi1), max(vi0, vi1))
                            pns = edge_poly_norms.get(key, [])
                            if pns and not any(_front_facing(mid, mx_n @ n) for n in pns):
                                continue
                        _check(mid, 'EDGE_MIDPOINT')
                if want_face:
                    for f in polys:
                        co_w = mx_w @ f.center
                        if not _front_facing(co_w, mx_n @ f.normal):
                            continue
                        _check(co_w, 'FACE_CENTER')
            finally:
                obj_eval.to_mesh_clear()

    elif context.mode == 'OBJECT':
        depsgraph = context.evaluated_depsgraph_get()
        if want_vert:
            for obj in context.visible_objects:
                _check(obj.matrix_world.translation.copy(), 'OBJECT_ORIGIN')
        if want_vert or want_edge_mid or want_face:
            for obj in context.visible_objects:
                if obj.type != 'MESH':
                    continue
                obj_eval = obj.evaluated_get(depsgraph)
                mesh_eval = obj_eval.to_mesh()
                if mesh_eval is None:
                    continue
                try:
                    mx_w  = obj.matrix_world
                    mx_n  = mx_w.inverted_safe().transposed().to_3x3()
                    verts = mesh_eval.vertices
                    polys = mesh_eval.polygons
                    if cull and (want_vert or want_edge_mid):
                        vert_poly_norms = [[] for _ in range(len(verts))]
                        edge_poly_norms = {}
                        for f in polys:
                            fn = f.normal
                            fv = list(f.vertices)
                            for vi in fv:
                                vert_poly_norms[vi].append(fn)
                            if want_edge_mid:
                                for i in range(len(fv)):
                                    key = (min(fv[i], fv[(i+1) % len(fv)]),
                                           max(fv[i], fv[(i+1) % len(fv)]))
                                    edge_poly_norms.setdefault(key, []).append(fn)
                    if want_vert:
                        for v in verts:
                            co_w = mx_w @ v.co
                            if cull:
                                pns = vert_poly_norms[v.index]
                                if pns and not any(_front_facing(co_w, mx_n @ n) for n in pns):
                                    continue
                            _check(co_w, 'VERTEX')
                    if want_edge_mid:
                        for e in mesh_eval.edges:
                            vi0, vi1 = e.vertices[0], e.vertices[1]
                            mid = mx_w @ ((verts[vi0].co + verts[vi1].co) / 2.0)
                            if cull:
                                key = (min(vi0, vi1), max(vi0, vi1))
                                pns = edge_poly_norms.get(key, [])
                                if pns and not any(_front_facing(mid, mx_n @ n) for n in pns):
                                    continue
                            _check(mid, 'EDGE_MIDPOINT')
                    if want_face:
                        for f in polys:
                            co_w = mx_w @ f.center
                            if not _front_facing(co_w, mx_n @ f.normal):
                                continue
                            _check(co_w, 'FACE_CENTER')
                finally:
                    obj_eval.to_mesh_clear()

    return best


def _snap_highlight_draw_callback():
    """GPU POST_PIXEL callback: draw a square vertex highlight at the snap target."""
    if state._snap_highlight is None:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        sx, sy = state._snap_highlight['screen_pos']
        r      = 8.0

        try:
            p = get_addon_preferences(bpy.context)
            c = p.preselect_color
            color = (c[0], c[1], c[2], 1.0)
        except Exception:
            color = (0.0, 0.85, 1.0, 1.0)

        quad = [
            (sx - r, sy - r), (sx + r, sy - r), (sx + r, sy + r),
            (sx - r, sy - r), (sx + r, sy + r), (sx - r, sy + r),
        ]

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float('color', color)
        batch_for_shader(shader, 'TRIS', {'pos': quad}).draw(shader)
        gpu.state.blend_set('NONE')
    except Exception:
        pass


class VIEW3D_OT_modo_snap_highlight(bpy.types.Operator):
    """Tracks mouse hover to highlight the nearest snap target while the
    Modo Move tool (W) is active and Blender snapping is enabled."""
    bl_idname  = 'view3d.modo_snap_highlight'
    bl_label   = 'Modo Snap Highlight'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'VIEW_3D')

    def invoke(self, context, event):
        self._ctrl_override = False
        self._snap_was_on   = False
        self._last_mx       = event.mouse_region_x
        self._last_my       = event.mouse_region_y
        context.window_manager.modal_handler_add(self)
        if state._snap_highlight_draw_handle is None:
            state._snap_highlight_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                _snap_highlight_draw_callback, (), 'WINDOW', 'POST_PIXEL')
        if event.ctrl:
            self._apply_ctrl_snap(context, True)
        return {'RUNNING_MODAL'}

    def _apply_ctrl_snap(self, context, ctrl_held):
        ts = context.tool_settings
        if ctrl_held and not self._ctrl_override:
            self._snap_was_on   = ts.use_snap
            ts.use_snap         = True
            self._ctrl_override = True
        elif not ctrl_held and self._ctrl_override:
            ts.use_snap         = self._snap_was_on
            self._ctrl_override = False

    def modal(self, context, event):
        if state._active_transform_mode is None:
            self._apply_ctrl_snap(context, False)
            self._cleanup()
            return {'FINISHED'}

        self._apply_ctrl_snap(context, event.ctrl)

        if event.type == 'MOUSEMOVE':
            self._last_mx = event.mouse_region_x
            self._last_my = event.mouse_region_y

        state._snap_highlight = _find_snap_target(
            context, self._last_mx, self._last_my)
        if context.area:
            context.area.tag_redraw()

        return {'PASS_THROUGH'}

    def _cleanup(self):
        state._snap_highlight = None
        if state._snap_highlight_draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(
                state._snap_highlight_draw_handle, 'WINDOW')
            state._snap_highlight_draw_handle = None
        try:
            if bpy.context.area:
                bpy.context.area.tag_redraw()
        except Exception:
            pass


def _start_snap_highlight_modal():
    """No-op kept for compatibility; invocation is done directly."""
    return None


def _stop_snap_highlight():
    """Clear state so the running modal exits cleanly on its next tick."""
    state._snap_highlight = None


# ── VIEW3D_OT_modo_transform  (W / E / R) ────────────────────────────────────

class VIEW3D_OT_modo_transform(bpy.types.Operator):
    """Modo-style W/E/R: toggle Move / Rotate / Scale gizmo"""
    bl_idname  = 'view3d.modo_transform'
    bl_label   = 'Modo Transform'
    bl_options = {'REGISTER', 'UNDO'}

    _GIZMO_ATTRS = {
        'TRANSLATE': 'show_gizmo_object_translate',
        'ROTATE':    'show_gizmo_object_rotate',
        'RESIZE':    'show_gizmo_object_scale',
    }

    _TOOL_IDS = {
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
                and context.space_data.type == 'VIEW_3D'
                and context.mode in ('OBJECT', 'EDIT_MESH', 'EDIT_CURVE',
                                     'EDIT_ARMATURE', 'POSE'))

    def _active_tool_id(self, context):
        """Return the idname of the current workspace tool, or '' on failure."""
        try:
            tool = context.workspace.tools.from_space_view3d_mode(context.mode)
            return tool.idname if tool else ''
        except Exception:
            return ''

    def invoke(self, context, event):
        sv3d = context.space_data
        attr = self._GIZMO_ATTRS[self.transform_type]
        other_attrs   = [a for a in self._GIZMO_ATTRS.values() if a != attr]

        # For RESIZE we own the tool state ourselves; for W/E use the active tool id.
        if self.transform_type == 'RESIZE':
            already_on = (state._active_transform_mode == 'RESIZE')
        else:
            active_tool = self._active_tool_id(context)
            already_on  = (active_tool == self._TOOL_IDS[self.transform_type])

        if already_on:
            # Toggle off — already on this transform tool, return to box select
            setattr(sv3d, attr, False)
            if self.transform_type == 'RESIZE':
                sv3d.show_gizmo_tool = True
                _stop_scale_gizmo()
            try:
                bpy.ops.wm.tool_set_by_id(name='builtin.select_box')
            except Exception:
                pass
            state._active_transform_mode = None
            _stop_snap_highlight()
            if self.transform_type == 'TRANSLATE':
                _stop_anchor_timer()
            _stop_pivot_crosshair()
            if state._saved_pivot_point is not None:
                context.scene.tool_settings.transform_pivot_point = state._saved_pivot_point
                state._saved_pivot_point = None
            if state._saved_cursor_location is not None:
                context.scene.cursor.location = state._saved_cursor_location.copy()
                state._saved_cursor_location = None
            if state._saved_snap_target is not None:
                ts = context.tool_settings
                for _attr in ('snap_target', 'snap_source'):
                    if hasattr(ts, _attr):
                        try:
                            setattr(ts, _attr, state._saved_snap_target)
                        except Exception:
                            pass
                        break
                state._saved_snap_target = None
            state._reposition_anchor = None
            state._last_known_median = None
            if state._implicit_select_all:
                _implicit_deselect_all_geometry(context)
                state._implicit_select_all = False
        else:
            # Activate this transform type (from any tool: extrude, bevel, box select, etc.)
            if (state._active_transform_mode is not None
                    and state._active_transform_mode != self.transform_type):
                _stop_snap_highlight()
                if state._active_transform_mode == 'TRANSLATE':
                    _stop_anchor_timer()
                if state._active_transform_mode == 'RESIZE':
                    _stop_scale_gizmo()
            for a in other_attrs:
                setattr(sv3d, a, False)
            if self.transform_type == 'RESIZE':
                # Activate builtin.scale (exits extrude/bevel) but suppress
                # its built-in gizmo so only our custom one draws.
                try:
                    bpy.ops.wm.tool_set_by_id(name='builtin.scale')
                except Exception:
                    pass
                sv3d.show_gizmo_tool = False
                _start_scale_gizmo()
            else:
                setattr(sv3d, attr, True)
                if not sv3d.show_gizmo:
                    sv3d.show_gizmo = True
                try:
                    bpy.ops.wm.tool_set_by_id(name=self._TOOL_IDS[self.transform_type])
                except Exception:
                    pass
            state._active_transform_mode = self.transform_type
            # Force snap base to CENTER
            if state._saved_snap_target is None:
                ts = context.tool_settings
                for _attr in ('snap_target', 'snap_source'):
                    _val = getattr(ts, _attr, None)
                    if isinstance(_val, str):
                        state._saved_snap_target = _val
                        try:
                            setattr(ts, _attr, 'CENTER')
                        except Exception:
                            pass
                        break
            # Implicit select all if nothing is selected
            if not state._implicit_select_all and not _has_any_selection(context):
                _implicit_select_all_geometry(context)
                state._implicit_select_all = True
            # Start snap highlight modal (only if not already running)
            if state._snap_highlight_draw_handle is None:
                try:
                    bpy.ops.view3d.modo_snap_highlight('INVOKE_DEFAULT')
                except Exception:
                    pass
        return {'FINISHED'}


class VIEW3D_OT_modo_drop_transform(bpy.types.Operator):
    """Drop the active W/E/R gizmo (Space while Move/Rotate/Scale is on)."""
    bl_idname  = 'view3d.modo_drop_transform'
    bl_label   = 'Drop Transform'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (state._active_transform_mode is not None
                and context.space_data is not None
                and context.space_data.type == 'VIEW_3D')

    def execute(self, context):
        _drop_transform(context)
        return {'FINISHED'}


class VIEW3D_OT_modo_screen_move(bpy.types.Operator):
    """MMB while the Move tool (W) is active: translate on the screen plane."""
    bl_idname  = 'view3d.modo_screen_move'
    bl_label   = 'Screen Space Move'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (state._active_transform_mode == 'TRANSLATE'
                and context.space_data is not None
                and context.space_data.type == 'VIEW_3D'
                and context.mode in ('OBJECT', 'EDIT_MESH'))

    def invoke(self, context, event):
        from mathutils import Vector

        rv3d = context.region_data
        if rv3d is not None:
            view_depth = Vector((rv3d.view_matrix[2][0],
                                 rv3d.view_matrix[2][1],
                                 rv3d.view_matrix[2][2])).normalized()
            dots = [abs(view_depth.dot(Vector(a)))
                    for a in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
            depth_axis = dots.index(max(dots))
            constraint = [True, True, True]
            constraint[depth_axis] = False
        else:
            constraint = [True, True, False]

        bpy.ops.transform.translate(
            'INVOKE_DEFAULT',
            orient_type='GLOBAL',
            constraint_axis=constraint,
        )
        return {'FINISHED'}


# ── Scale Gizmo ───────────────────────────────────────────────────────────────
# Custom POST_PIXEL 3-D scale gizmo that mirrors Blender's visual style while
# owning the drag interaction so S (flatten) and D (flip) work mid-drag.
#
# Handles:  X / Y / Z axis cubes  ·  XY / XZ / YZ plane squares  ·  XYZ dot
# Draw:     same AA-line + SDF-dot shaders used by the UV gizmo
# Drag:     live bmesh / matrix preview → confirms via transform.resize (undo)
# ─────────────────────────────────────────────────────────────────────────────

_SCALE_ARM_PX    = 90.0   # arm length in region pixels
_SCALE_GAP_PX    = 14.0   # gap at pivot before arm starts
_SCALE_HANDLE_HW = 5.0    # half-size of axis-end diamond handle
_SCALE_PLANE_FRAC = 0.42  # plane handles positioned at this fraction of arm
_SCALE_PLANE_SZ  = 14.0   # half-size of each side of the plane square
_SCALE_DOT_R     = 6.0    # radius of uniform-scale dot
_SCALE_HIT_R     = 20.0   # pixel hit radius for axis tips and plane centers
_SCALE_DOT_HIT_R = 12.0   # pixel hit radius for uniform dot

_SCALE_COL_X     = (0.93, 0.21, 0.31, 1.0)
_SCALE_COL_Y     = (0.55, 0.86, 0.00, 1.0)
_SCALE_COL_Z     = (0.13, 0.55, 0.86, 1.0)
_SCALE_COL_HL    = (1.00, 0.83, 0.00, 1.0)   # yellow hover highlight
_SCALE_COL_WHITE = (1.00, 1.00, 1.00, 0.90)

_SCALE_AXIS_CONSTRAINTS = {
    'X':   (True,  False, False),
    'Y':   (False, True,  False),
    'Z':   (False, False, True),
    'XY':  (True,  True,  False),
    'XZ':  (True,  False, True),
    'YZ':  (False, True,  True),
    'XYZ': (True,  True,  True),
}


def _sg_get_pivot_world(context):
    """Return world-space pivot respecting transform_pivot_point setting."""
    ts = context.tool_settings
    pp = ts.transform_pivot_point
    try:
        if pp == 'CURSOR':
            return context.scene.cursor.location.copy()
        if pp == 'ACTIVE_ELEMENT':
            if context.mode == 'OBJECT':
                obj = context.active_object
                if obj:
                    return obj.matrix_world.translation.copy()
            elif context.mode == 'EDIT_MESH':
                obj = context.edit_object
                if obj and obj.type == 'MESH':
                    bm = bmesh.from_edit_mesh(obj.data)
                    if bm.select_history:
                        elem = bm.select_history.active
                        mx = obj.matrix_world
                        if hasattr(elem, 'co'):
                            return (mx @ elem.co).copy()
                        if hasattr(elem, 'verts'):
                            mid = sum((v.co for v in elem.verts), _Vector()) / len(elem.verts)
                            return (mx @ mid).copy()
        if pp == 'BOUNDING_BOX_CENTER':
            if context.mode == 'EDIT_MESH':
                coords = []
                for obj in context.objects_in_mode_unique_data:
                    if obj.type != 'MESH':
                        continue
                    bm = bmesh.from_edit_mesh(obj.data)
                    mx = obj.matrix_world
                    coords.extend(mx @ v.co for v in bm.verts if v.select)
                if coords:
                    xs = [c.x for c in coords]
                    ys = [c.y for c in coords]
                    zs = [c.z for c in coords]
                    return _Vector(((min(xs)+max(xs))/2,
                                   (min(ys)+max(ys))/2,
                                   (min(zs)+max(zs))/2))
            elif context.mode == 'OBJECT':
                objs = context.selected_objects
                if objs:
                    locs = [o.matrix_world.translation for o in objs]
                    xs = [c.x for c in locs]
                    ys = [c.y for c in locs]
                    zs = [c.z for c in locs]
                    return _Vector(((min(xs)+max(xs))/2,
                                   (min(ys)+max(ys))/2,
                                   (min(zs)+max(zs))/2))
    except Exception:
        pass
    return _compute_selection_median(context)


def _sg_get_orient_matrix_3x3(context):
    """Return 3×3 rotation matrix for the current transform orientation."""
    try:
        slot  = context.scene.transform_orientation_slots[0]
        otype = slot.type
        if otype == 'GLOBAL':
            return _Matrix.Identity(3)
        if otype == 'LOCAL':
            obj = context.active_object
            if obj:
                return obj.matrix_world.to_3x3().normalized()
            return _Matrix.Identity(3)
        if otype == 'CURSOR':
            return context.scene.cursor.matrix.to_3x3()
        if otype == 'CUSTOM':
            co = slot.custom_orientation
            if co:
                return co.matrix.copy()
            return _Matrix.Identity(3)
        # NORMAL / GIMBAL / VIEW → global fallback
        return _Matrix.Identity(3)
    except Exception:
        return _Matrix.Identity(3)


def _sg_build_scale_matrix(pivot_w, orient_3x3, sx, sy, sz):
    """Return a 4×4 matrix that scales by (sx,sy,sz) around pivot_w
    in the given orientation space."""
    R     = orient_3x3.to_4x4()
    R_inv = orient_3x3.inverted().to_4x4()
    S     = _Matrix.Diagonal(_Vector((sx, sy, sz, 1.0)))
    T_b   = _Matrix.Translation(pivot_w)
    T_f   = _Matrix.Translation(-pivot_w)
    return T_b @ R @ S @ R_inv @ T_f


# ── Scale gizmo shaders (matrix-free, pixel coords → NDC directly) ────────────
# These shaders do NOT use ModelViewProjectionMatrix at all — they compute NDC
# from pixel coordinates using uwidth/uheight uniforms.  This makes them immune
# to whatever 3D camera matrix happens to be on the GPU stack in POST_PIXEL.

_sg_aa_shader_cache   = None
_sg_flat_shader_cache = None
_sg_dot_shader_cache  = None

_SG_AA_VERT = (
    'void main() {\n'
    '    gl_Position = vec4(2.0*pos.x/uwidth - 1.0, 2.0*pos.y/uheight - 1.0, 0.0, 1.0);\n'
    '    vT = t;\n'
    '}\n'
)
_SG_AA_FRAG = (
    'void main() {\n'
    '    float d = abs(vT);\n'
    '    float a = 1.0 - smoothstep(uhalf_w - 1.0, uhalf_w, d);\n'
    '    fragColor = vec4(ucolor.rgb, ucolor.a * a);\n'
    '}\n'
)
_SG_FLAT_VERT = (
    'void main() {\n'
    '    gl_Position = vec4(2.0*pos.x/uwidth - 1.0, 2.0*pos.y/uheight - 1.0, 0.0, 1.0);\n'
    '}\n'
)
_SG_FLAT_FRAG = (
    'void main() {\n'
    '    fragColor = ucolor;\n'
    '}\n'
)
_SG_DOT_VERT = _SG_FLAT_VERT
_SG_DOT_FRAG = (
    'void main() {\n'
    '    float d = length(gl_FragCoord.xy - ucenter) - uradius;\n'
    '    float a = 1.0 - smoothstep(-0.5, 0.5, d);\n'
    '    fragColor = vec4(ucolor.rgb, ucolor.a * a);\n'
    '}\n'
)


def _get_sg_aa_shader():
    global _sg_aa_shader_cache
    if _sg_aa_shader_cache is None:
        try:
            import gpu
            info = gpu.types.GPUShaderCreateInfo()
            info.push_constant('FLOAT', 'uwidth')
            info.push_constant('FLOAT', 'uheight')
            info.push_constant('VEC4',  'ucolor')
            info.push_constant('FLOAT', 'uhalf_w')
            info.vertex_in(0, 'VEC2',  'pos')
            info.vertex_in(1, 'FLOAT', 't')
            iface = gpu.types.GPUStageInterfaceInfo('sg_aa_iface')
            iface.smooth('FLOAT', 'vT')
            info.vertex_out(iface)
            info.fragment_out(0, 'VEC4', 'fragColor')
            info.vertex_source(_SG_AA_VERT)
            info.fragment_source(_SG_AA_FRAG)
            _sg_aa_shader_cache = gpu.shader.create_from_info(info)
        except Exception as e:
            print(f'[modokit] sg_aa shader: {e}')
    return _sg_aa_shader_cache


def _get_sg_flat_shader():
    global _sg_flat_shader_cache
    if _sg_flat_shader_cache is None:
        try:
            import gpu
            info = gpu.types.GPUShaderCreateInfo()
            info.push_constant('FLOAT', 'uwidth')
            info.push_constant('FLOAT', 'uheight')
            info.push_constant('VEC4',  'ucolor')
            info.vertex_in(0, 'VEC2', 'pos')
            info.fragment_out(0, 'VEC4', 'fragColor')
            info.vertex_source(_SG_FLAT_VERT)
            info.fragment_source(_SG_FLAT_FRAG)
            _sg_flat_shader_cache = gpu.shader.create_from_info(info)
        except Exception as e:
            print(f'[modokit] sg_flat shader: {e}')
    return _sg_flat_shader_cache


def _get_sg_dot_shader():
    global _sg_dot_shader_cache
    if _sg_dot_shader_cache is None:
        try:
            import gpu
            info = gpu.types.GPUShaderCreateInfo()
            info.push_constant('FLOAT', 'uwidth')
            info.push_constant('FLOAT', 'uheight')
            info.push_constant('VEC4',  'ucolor')
            info.push_constant('VEC2',  'ucenter')
            info.push_constant('FLOAT', 'uradius')
            info.vertex_in(0, 'VEC2', 'pos')
            info.fragment_out(0, 'VEC4', 'fragColor')
            info.vertex_source(_SG_DOT_VERT)
            info.fragment_source(_SG_DOT_FRAG)
            _sg_dot_shader_cache = gpu.shader.create_from_info(info)
        except Exception as e:
            print(f'[modokit] sg_dot shader: {e}')
    return _sg_dot_shader_cache


def _scale_gizmo_draw_callback():
    """POST_PIXEL callback — Blender-style scale gizmo drawn as 2-D screen overlay."""
    if state._active_transform_mode != 'RESIZE':
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        from bpy_extras import view3d_utils

        from .uv_overlays import _get_aa_line_shader, _aa_line_quads, _get_dot_shader

        ctx    = bpy.context
        region = ctx.region
        rv3d   = ctx.region_data
        if region is None or rv3d is None:
            return

        pivot_w = _sg_get_pivot_world(ctx)
        if pivot_w is None:
            return

        orient = _sg_get_orient_matrix_3x3(ctx)

        pivot_s = view3d_utils.location_3d_to_region_2d(region, rv3d, pivot_w)
        if pivot_s is None:
            return
        px, py = pivot_s.x, pivot_s.y

        pivot_s = view3d_utils.location_3d_to_region_2d(region, rv3d, pivot_w)
        if pivot_s is None:
            return
        px, py = pivot_s.x, pivot_s.y

        ARM  = _SCALE_ARM_PX
        GAP  = _SCALE_GAP_PX
        HW   = _SCALE_HANDLE_HW
        PF   = _SCALE_PLANE_FRAC
        PSZ  = _SCALE_PLANE_SZ
        DOTR = _SCALE_DOT_R

        # Project each axis direction into screen space using the view matrix
        # directly instead of projecting a nearby 3D point.  Projecting
        # pivot + ax * step can flip when the offset ends up behind/near the
        # camera (perspective division goes negative), making arms point toward
        # screen centre.  The view matrix 3×3 is stable at all camera angles.
        axis_world = {
            'X': _Vector(orient.col[0]).normalized(),
            'Y': _Vector(orient.col[1]).normalized(),
            'Z': _Vector(orient.col[2]).normalized(),
        }
        view_3x3 = rv3d.view_matrix.to_3x3()
        screen_dirs = {}
        arm_ends    = {}
        for name, ax in axis_world.items():
            ax_view = view_3x3 @ ax
            ndx, ndy = ax_view.x, ax_view.y
            dl = math.sqrt(ndx*ndx + ndy*ndy)
            if dl < 0.02:
                screen_dirs[name] = arm_ends[name] = None
                continue
            screen_dirs[name] = (ndx/dl, ndy/dl)
            arm_ends[name]    = (px + (ndx/dl) * ARM, py + (ndy/dl) * ARM)

        # Plane handle centers
        plane_centers = {}
        for pa, pb in (('X', 'Y'), ('X', 'Z'), ('Y', 'Z')):
            da = screen_dirs.get(pa)
            db = screen_dirs.get(pb)
            if da is None or db is None:
                plane_centers[pa+pb] = None
                continue
            cross = abs(da[0]*db[1] - da[1]*db[0])
            if cross < 0.22:
                plane_centers[pa+pb] = None
                continue
            fa = (px + da[0]*ARM*PF, py + da[1]*ARM*PF)
            fb = (px + db[0]*ARM*PF, py + db[1]*ARM*PF)
            plane_centers[pa+pb] = ((fa[0]+fb[0])/2, (fa[1]+fb[1])/2)

        # Cache for hit testing
        state._scale_gizmo_screen_handles = {
            'pivot': (px, py),
            'X': arm_ends.get('X'), 'Y': arm_ends.get('Y'), 'Z': arm_ends.get('Z'),
            'XY': plane_centers.get('XY'),
            'XZ': plane_centers.get('XZ'),
            'YZ': plane_centers.get('YZ'),
            'X_dir': screen_dirs.get('X'),
            'Y_dir': screen_dirs.get('Y'),
            'Z_dir': screen_dirs.get('Z'),
        }

        hover   = state._scale_gizmo_hover
        aa      = _get_aa_line_shader()
        flat    = gpu.shader.from_builtin('UNIFORM_COLOR')
        sdot    = _get_dot_shader()
        gpu.state.blend_set('ALPHA')

        def _draw_aa(segs, color, half_w):
            if aa:
                pos, tvs = _aa_line_quads(segs, half_w)
                if not pos:
                    return
                b = batch_for_shader(aa, 'TRIS', {'pos': pos, 't': tvs})
                aa.bind()
                aa.uniform_float('ucolor',  color)
                aa.uniform_float('uhalf_w', half_w)
                b.draw(aa)
            else:
                pts = [p for seg in segs for p in seg]
                flat.bind()
                flat.uniform_float('color', color)
                gpu.state.line_width_set((half_w - 0.5) * 2)
                batch_for_shader(flat, 'LINES', {'pos': pts}).draw(flat)
                gpu.state.line_width_set(1.0)

        def _draw_flat(verts, color):
            flat.bind()
            flat.uniform_float('color', color)
            batch_for_shader(flat, 'TRIS', {'pos': verts}).draw(flat)

        # ── Axis arms + end-diamond handles ─────────────────────────────────
        axis_colors = {'X': _SCALE_COL_X, 'Y': _SCALE_COL_Y, 'Z': _SCALE_COL_Z}
        for aname, base_col in axis_colors.items():
            end = arm_ends.get(aname)
            if end is None:
                continue
            ex, ey = end

            hover_hit = (hover == aname
                         or (len(hover) == 2 and aname in hover)
                         or hover == 'XYZ')
            color    = _SCALE_COL_HL if hover_hit else base_col
            shaft_hw = 1.5 if hover_hit else 0.9

            sd = screen_dirs[aname]
            gx = px + sd[0] * GAP
            gy = py + sd[1] * GAP
            ax = px + sd[0] * (ARM - HW - 1.0)
            ay = py + sd[1] * (ARM - HW - 1.0)
            _draw_aa([((gx, gy), (ax, ay))], color, shaft_hw * 0.5 + 1.0)

            dt   = (ex,      ey + HW)
            dr   = (ex + HW, ey      )
            db   = (ex,      ey - HW)
            dl_p = (ex - HW, ey      )
            _draw_flat([dt, dr, db, dt, db, dl_p], color)
            _draw_aa([(dt, dr), (dr, db), (db, dl_p), (dl_p, dt)], color, 1.5)

        # ── Plane handles ─────────────────────────────────────────────────
        plane_axis_cols = {
            'XY': (_SCALE_COL_X, _SCALE_COL_Y),
            'XZ': (_SCALE_COL_X, _SCALE_COL_Z),
            'YZ': (_SCALE_COL_Y, _SCALE_COL_Z),
        }
        for pname, (col_a, col_b) in plane_axis_cols.items():
            pc = plane_centers.get(pname)
            if pc is None:
                continue
            cx_p, cy_p = pc
            is_hl = (hover == pname)
            blend_r = (col_a[0] + col_b[0]) * 0.5
            blend_g = (col_a[1] + col_b[1]) * 0.5
            blend_b = (col_a[2] + col_b[2]) * 0.5
            fill_col   = (blend_r, blend_g, blend_b, 0.35 if is_hl else 0.18)
            border_col = _SCALE_COL_HL if is_hl else (blend_r, blend_g, blend_b, 0.85)
            d1 = screen_dirs.get(pname[0])
            d2 = screen_dirs.get(pname[1])
            if d1 is None or d2 is None:
                continue
            half = PSZ
            c1 = (cx_p + d1[0]*half + d2[0]*half, cy_p + d1[1]*half + d2[1]*half)
            c2 = (cx_p - d1[0]*half + d2[0]*half, cy_p - d1[1]*half + d2[1]*half)
            c3 = (cx_p - d1[0]*half - d2[0]*half, cy_p - d1[1]*half - d2[1]*half)
            c4 = (cx_p + d1[0]*half - d2[0]*half, cy_p + d1[1]*half - d2[1]*half)
            _draw_flat([c1, c2, c3, c1, c3, c4], fill_col)
            _draw_aa([(c1, c2), (c2, c3), (c3, c4), (c4, c1)], border_col, 1.5)

        # ── Centre dot (XYZ uniform) ──────────────────────────────────────
        dot_col = _SCALE_COL_HL if hover == 'XYZ' else _SCALE_COL_WHITE
        dot_r   = DOTR * 1.3   if hover == 'XYZ' else DOTR
        if sdot:
            hw = dot_r + 2.0
            dq = [(px-hw, py-hw), (px+hw, py-hw), (px+hw, py+hw),
                  (px-hw, py-hw), (px+hw, py+hw), (px-hw, py+hw)]
            b = batch_for_shader(sdot, 'TRIS', {'pos': dq})
            sdot.bind()
            sdot.uniform_float('ucolor',   dot_col)
            sdot.uniform_float('ucenter',  (px, py))
            sdot.uniform_float('uradius',  dot_r)
            b.draw(sdot)

        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _start_scale_gizmo():
    if state._scale_gizmo_draw_handle is None:
        state._scale_gizmo_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _scale_gizmo_draw_callback, (), 'WINDOW', 'POST_PIXEL')


def _stop_scale_gizmo():
    if state._scale_gizmo_draw_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                state._scale_gizmo_draw_handle, 'WINDOW')
        except Exception:
            pass
        state._scale_gizmo_draw_handle = None
    state._scale_gizmo_screen_handles = {}
    state._scale_gizmo_hover = ''
    try:
        sv3d = bpy.context.space_data
        if sv3d and sv3d.type == 'VIEW_3D':
            sv3d.show_gizmo_tool = True
    except Exception:
        pass
    try:
        if bpy.context.area:
            bpy.context.area.tag_redraw()
    except Exception:
        pass


def _sg_hit_test(mx, my):
    """Return the handle name at screen pos (mx, my), or '' if none."""
    h = state._scale_gizmo_screen_handles
    if not h:
        return ''

    # Centre dot has priority
    pivot = h.get('pivot')
    if pivot:
        if math.sqrt((mx-pivot[0])**2 + (my-pivot[1])**2) <= _SCALE_DOT_HIT_R:
            return 'XYZ'

    best, best_d = '', float('inf')
    for name in ('X', 'Y', 'Z', 'XY', 'XZ', 'YZ'):
        pos = h.get(name)
        if pos is None:
            continue
        d = math.sqrt((mx-pos[0])**2 + (my-pos[1])**2)
        if d <= _SCALE_HIT_R and d < best_d:
            best_d, best = d, name
    return best


# ── VIEW3D_OT_modo_scale_gizmo_hover  (MOUSEMOVE) ────────────────────────────

class VIEW3D_OT_modo_scale_gizmo_hover(bpy.types.Operator):
    """MOUSEMOVE: update scale gizmo hover highlight."""
    bl_idname  = 'view3d.modo_scale_gizmo_hover'
    bl_label   = 'Scale Gizmo Hover'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (state._active_transform_mode == 'RESIZE'
                and context.space_data is not None
                and context.space_data.type == 'VIEW_3D')

    def invoke(self, context, event):
        hit = _sg_hit_test(event.mouse_region_x, event.mouse_region_y)
        if hit != state._scale_gizmo_hover:
            state._scale_gizmo_hover = hit
            if context.area:
                context.area.tag_redraw()
        return {'PASS_THROUGH'}


# ── VIEW3D_OT_modo_scale_gizmo_drag  (LMB modal) ─────────────────────────────

class VIEW3D_OT_modo_scale_gizmo_drag(bpy.types.Operator):
    """LMB on a scale gizmo handle: drag to scale.
    S mid-drag → flatten (×0).  D mid-drag → flip (×-1)."""
    bl_idname  = 'view3d.modo_scale_gizmo_drag'
    bl_label   = 'Scale Gizmo Drag'
    bl_options = {'INTERNAL', 'BLOCKING'}

    @classmethod
    def poll(cls, context):
        return (state._active_transform_mode == 'RESIZE'
                and context.space_data is not None
                and context.space_data.type == 'VIEW_3D'
                and context.mode in ('OBJECT', 'EDIT_MESH'))

    def invoke(self, context, event):
        mx, my = event.mouse_region_x, event.mouse_region_y
        hit = _sg_hit_test(mx, my)
        if not hit:
            return {'PASS_THROUGH'}

        self._axis      = hit
        self._start_mx  = mx
        self._start_my  = my
        self._last_s    = 1.0

        pivot = _sg_get_pivot_world(context)
        if pivot is None:
            return {'CANCELLED'}
        self._pivot_w  = pivot
        self._orient   = _sg_get_orient_matrix_3x3(context)
        self._orient_t = context.scene.transform_orientation_slots[0].type

        ph = state._scale_gizmo_screen_handles.get('pivot')
        self._pivot_sx, self._pivot_sy = ph if ph else (mx, my)

        # Capture originals for live preview + restore
        self._orig_verts    = {}   # obj_name -> {vi: world_co}
        self._orig_matrices = {}   # obj_name -> Matrix
        if context.mode == 'EDIT_MESH':
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm = bmesh.from_edit_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                mx_w = obj.matrix_world
                self._orig_verts[obj.name] = {
                    v.index: (mx_w @ v.co).copy()
                    for v in bm.verts if v.select
                }
        elif context.mode == 'OBJECT':
            for obj in context.selected_objects:
                self._orig_matrices[obj.name] = obj.matrix_world.copy()

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    # ── scale factor ─────────────────────────────────────────────────────────

    def _compute_scale(self, mx, my):
        px, py = self._pivot_sx, self._pivot_sy
        if self._axis in ('XYZ', 'XY', 'XZ', 'YZ'):
            # Uniform / plane: total distance from pivot
            cur = math.sqrt((mx-px)**2 + (my-py)**2)
            st  = math.sqrt((self._start_mx-px)**2 + (self._start_my-py)**2)
            return cur / st if st > 1.0 else 1.0
        # Single axis: project onto axis screen direction
        d = state._scale_gizmo_screen_handles.get(self._axis + '_dir')
        if d is None:
            return 1.0
        cur   = (mx - px) * d[0] + (my - py) * d[1]
        start = (self._start_mx - px) * d[0] + (self._start_my - py) * d[1]
        return cur / start if abs(start) > 1.0 else 1.0

    def _sv(self, s):
        """Scale-value vector for axis + factor s."""
        a = self._axis
        if a == 'X':   return (s, 1, 1)
        if a == 'Y':   return (1, s, 1)
        if a == 'Z':   return (1, 1, s)
        if a == 'XY':  return (s, s, 1)
        if a == 'XZ':  return (s, 1, s)
        if a == 'YZ':  return (1, s, s)
        return (s, s, s)

    # ── apply / restore ──────────────────────────────────────────────────────

    def _apply_live(self, context, s):
        sv = self._sv(s)
        M  = _sg_build_scale_matrix(self._pivot_w, self._orient, *sv)
        if context.mode == 'EDIT_MESH':
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm = bmesh.from_edit_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                inv = obj.matrix_world.inverted()
                for vi, orig_w in self._orig_verts.get(obj.name, {}).items():
                    if vi < len(bm.verts):
                        bm.verts[vi].co = inv @ (M @ orig_w)
                bmesh.update_edit_mesh(obj.data, destructive=False)
        elif context.mode == 'OBJECT':
            for obj in context.selected_objects:
                orig = self._orig_matrices.get(obj.name)
                if orig is not None:
                    obj.matrix_world = M @ orig

    def _restore(self, context):
        if context.mode == 'EDIT_MESH':
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm = bmesh.from_edit_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                inv = obj.matrix_world.inverted()
                for vi, orig_w in self._orig_verts.get(obj.name, {}).items():
                    if vi < len(bm.verts):
                        bm.verts[vi].co = inv @ orig_w
                bmesh.update_edit_mesh(obj.data, destructive=False)
        elif context.mode == 'OBJECT':
            for obj in context.selected_objects:
                orig = self._orig_matrices.get(obj.name)
                if orig is not None:
                    obj.matrix_world = orig

    def _finalize(self, context, s):
        """Restore originals then replay with transform.resize for clean undo."""
        self._restore(context)
        sv = self._sv(s)
        if all(abs(v - 1.0) < 1e-9 for v in sv):
            return  # no effective change
        constraint = _SCALE_AXIS_CONSTRAINTS.get(self._axis, (True, True, True))
        old_pp     = context.tool_settings.transform_pivot_point
        old_cursor = context.scene.cursor.location.copy()
        context.tool_settings.transform_pivot_point = 'CURSOR'
        context.scene.cursor.location = self._pivot_w
        try:
            bpy.ops.transform.resize(
                'EXEC_DEFAULT',
                value=sv,
                constraint_axis=constraint,
                orient_type=self._orient_t,
            )
        except Exception:
            pass
        finally:
            context.tool_settings.transform_pivot_point = old_pp
            context.scene.cursor.location = old_cursor

    # ── modal ────────────────────────────────────────────────────────────────

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            s = self._compute_scale(event.mouse_region_x, event.mouse_region_y)
            self._last_s = s
            self._apply_live(context, s)
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self._finalize(context, self._last_s)
            if context.area:
                context.area.tag_redraw()
            return {'FINISHED'}

        if event.type == 'S' and event.value == 'PRESS':
            self._finalize(context, 0.0)
            if context.area:
                context.area.tag_redraw()
            return {'FINISHED'}

        if event.type == 'D' and event.value == 'PRESS':
            self._finalize(context, -1.0)
            if context.area:
                context.area.tag_redraw()
            return {'FINISHED'}

        if event.type in ('RIGHTMOUSE', 'ESC'):
            self._restore(context)
            if context.area:
                context.area.tag_redraw()
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}
