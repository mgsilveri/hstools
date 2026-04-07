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
            if want_vert:
                bm_obj.verts.ensure_lookup_table()
                for v in bm_obj.verts:
                    _check(mx_w @ v.co, 'VERTEX')
            if want_edge_mid:
                bm_obj.edges.ensure_lookup_table()
                for e in bm_obj.edges:
                    mid = mx_w @ ((e.verts[0].co + e.verts[1].co) / 2.0)
                    _check(mid, 'EDGE_MIDPOINT')
            if want_face:
                bm_obj.faces.ensure_lookup_table()
                for f in bm_obj.faces:
                    _check(mx_w @ f.calc_center_median(), 'FACE_CENTER')

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
                mx_w = obj.matrix_world
                if want_vert:
                    for v in mesh_eval.vertices:
                        _check(mx_w @ v.co, 'VERTEX')
                if want_edge_mid:
                    for e in mesh_eval.edges:
                        mid = mx_w @ ((mesh_eval.vertices[e.vertices[0]].co
                                       + mesh_eval.vertices[e.vertices[1]].co) / 2.0)
                        _check(mid, 'EDGE_MIDPOINT')
                if want_face:
                    for f in mesh_eval.polygons:
                        _check(mx_w @ f.center, 'FACE_CENTER')
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
                    mx_w = obj.matrix_world
                    if want_vert:
                        for v in mesh_eval.vertices:
                            _check(mx_w @ v.co, 'VERTEX')
                    if want_edge_mid:
                        for e in mesh_eval.edges:
                            mid = mx_w @ ((mesh_eval.vertices[e.vertices[0]].co
                                           + mesh_eval.vertices[e.vertices[1]].co) / 2.0)
                            _check(mid, 'EDGE_MIDPOINT')
                    if want_face:
                        for f in mesh_eval.polygons:
                            _check(mx_w @ f.center, 'FACE_CENTER')
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

    def invoke(self, context, event):
        sv3d = context.space_data
        attr = self._GIZMO_ATTRS[self.transform_type]
        other_attrs   = [a for a in self._GIZMO_ATTRS.values() if a != attr]
        currently_on  = getattr(sv3d, attr)
        others_on     = any(getattr(sv3d, a) for a in other_attrs)

        if currently_on and not others_on:
            # Toggle off — same tool pressed again
            setattr(sv3d, attr, False)
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
            # Switch to (or activate) this transform type
            if (state._active_transform_mode is not None
                    and state._active_transform_mode != self.transform_type):
                _stop_snap_highlight()
                if state._active_transform_mode == 'TRANSLATE':
                    _stop_anchor_timer()
            for a in other_attrs:
                setattr(sv3d, a, False)
            setattr(sv3d, attr, True)
            if not sv3d.show_gizmo:
                sv3d.show_gizmo = True
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
