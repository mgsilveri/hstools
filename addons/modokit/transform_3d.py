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
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, StringProperty
from mathutils import Vector as _Vector, Matrix as _Matrix

from . import state
from .utils import get_addon_preferences, _diag


# ── Falloff PropertyGroup ─────────────────────────────────────────────────────

class ModoKitFalloffProps(bpy.types.PropertyGroup):
    """Scene-level properties for the Modo-style linear falloff."""
    enabled: BoolProperty(
        name="Enabled",
        description="Falloff is active",
        default=False,
    )
    show: BoolProperty(
        name="Show Falloff",
        description="Draw falloff handles and vertex weight overlay",
        default=False,
    )
    start: FloatVectorProperty(
        name="Start",
        description="Falloff start position (100% weight)",
        subtype='XYZ',
        size=3,
        default=(0.0, 0.0, 0.0),
    )
    end: FloatVectorProperty(
        name="End",
        description="Falloff end position (0% weight)",
        subtype='XYZ',
        size=3,
        default=(1.0, 0.0, 0.0),
    )
    symmetric: EnumProperty(
        name="Symmetric",
        description="Symmetry mode",
        items=[
            ('NONE',  'None',  'No symmetry'),
            ('START', 'Start', 'Mirror across the Start position'),
            ('END',   'End',   'Mirror across the End position'),
        ],
        default='NONE',
    )
    shape_preset: EnumProperty(
        name="Shape",
        description="Falloff shape for weight interpolation",
        items=[
            ('LINEAR',   'Linear',   'Linear falloff'),
            ('EASE_IN',  'Ease In',  'Slow start, fast end'),
            ('EASE_OUT', 'Ease Out', 'Fast start, slow end'),
            ('SMOOTH',   'Smooth',   'Smooth S-curve'),
            ('CUSTOM',   'Custom',   'Custom in/out curve'),
        ],
        default='LINEAR',
    )
    curve_in: FloatProperty(
        name="In",
        description="Custom shape curve-in factor",
        default=0.0, min=0.0, max=1.0,
    )
    curve_out: FloatProperty(
        name="Out",
        description="Custom shape curve-out factor",
        default=0.0, min=0.0, max=1.0,
    )
    mix_mode: EnumProperty(
        name="Mix Mode",
        description="How the falloff weight is applied",
        items=[
            ('MULTIPLY',  'Multiply',  ''),
            ('ADD',       'Add',       ''),
            ('SUBTRACT',  'Subtract',  ''),
            ('MIN',       'Min',       ''),
            ('MAX',       'Max',       ''),
            ('REPLACE',   'Replace',   ''),
        ],
        default='MULTIPLY',
    )
    use_world: BoolProperty(
        name="Use World Transforms",
        description=(
            "ON: treat all selected objects as one unified surface for falloff. "
            "OFF: evaluate each object independently in its own object space"
        ),
        default=True,
    )


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

_SCALE_ARM_PX    = 118.0  # arm length in region pixels
_SCALE_GAP_PX    = 21.0   # gap at pivot before arm starts
_SCALE_HANDLE_HW = 6.0    # half-size of axis-end cube handle
_SCALE_PLANE_FRAC = 0.54  # plane handles positioned at this fraction of arm
_SCALE_PLANE_SZ  = 8.0   # half-size of each side of the plane square
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


# ── Linear Falloff helpers ─────────────────────────────────────────────────────

_FALLOFF_COL_START = (1.00, 0.83, 0.00, 1.0)   # yellow      = 100%
_FALLOFF_COL_END   = (0.15, 0.00, 0.35, 1.0)   # dark purple = 0%
_FALLOFF_HIT_R     = 14.0                        # pixel hit radius for handles


def _falloff_weight_color(w):
    """Linearly interpolate from dark purple (w=0) to yellow (w=1)."""
    cs, ce = _FALLOFF_COL_START, _FALLOFF_COL_END
    return (
        w * cs[0] + (1.0 - w) * ce[0],
        w * cs[1] + (1.0 - w) * ce[1],
        w * cs[2] + (1.0 - w) * ce[2],
        1.0,
    )


def _apply_shape(w, props):
    """Apply the shape preset to a normalised falloff weight w in [0, 1]."""
    p = props.shape_preset
    if p == 'LINEAR':   return w
    if p == 'EASE_IN':  return w * w * w
    if p == 'EASE_OUT': return 1.0 - (1.0 - w) ** 3
    if p == 'SMOOTH':   return w * w * (3.0 - 2.0 * w)
    if p == 'CUSTOM':
        ci = props.curve_in
        co = props.curve_out
        return w + ci * w * (1.0 - w) - co * (1.0 - w) * w
    return w


def _falloff_linear_weight(world_co, props):
    """Return a weight in [0, 1] for world_co given the linear falloff props.
    Returns 1.0 at Start (100% influence) and 0.0 at End (0% influence)."""
    start  = _Vector(props.start)
    end    = _Vector(props.end)
    axis   = end - start
    length = axis.length
    if length < 1e-6:
        return 1.0
    t = (world_co - start).dot(axis) / (length * length)
    t = max(0.0, min(1.0, t))
    w = 1.0 - t   # t=0 at Start → w=1.0 ; t=1 at End → w=0.0
    sym = props.symmetric
    if sym == 'START':
        w = 1.0 - abs(1.0 - w * 2.0)
    elif sym == 'END':
        w = 1.0 - abs(w * 2.0 - 1.0)
    return _apply_shape(max(0.0, min(1.0, w)), props)


def _pca_dominant_axis(coords):
    """Return (centroid, unit_direction) of the dominant principal component
    of a point cloud, found via power iteration on the 3x3 covariance matrix.
    The direction points toward the end with the largest projection (i.e. the
    'positive' extreme). Converges in <20 iterations for any realistic mesh."""
    n = len(coords)
    cx = sum(c.x for c in coords) / n
    cy = sum(c.y for c in coords) / n
    cz = sum(c.z for c in coords) / n
    centroid = _Vector((cx, cy, cz))

    # Build symmetric 3x3 covariance matrix
    cxx = cxy = cxz = cyy = cyz = czz = 0.0
    for c in coords:
        dx = c.x - cx; dy = c.y - cy; dz = c.z - cz
        cxx += dx * dx; cxy += dx * dy; cxz += dx * dz
        cyy += dy * dy; cyz += dy * dz; czz += dz * dz

    # Initial guess: column of largest diagonal (avoids zero start)
    diag = (cxx, cyy, czz)
    mi   = diag.index(max(diag))
    v    = _Vector((1.0 if mi == 0 else 0.0,
                    1.0 if mi == 1 else 0.0,
                    1.0 if mi == 2 else 0.0))

    for _ in range(32):
        nx = cxx * v.x + cxy * v.y + cxz * v.z
        ny = cxy * v.x + cyy * v.y + cyz * v.z
        nz = cxz * v.x + cyz * v.y + czz * v.z
        nv = _Vector((nx, ny, nz))
        L  = nv.length
        if L < 1e-10:
            break
        nv /= L
        if (nv - v).length < 1e-7:
            v = nv
            break
        v = nv

    return centroid, v


def _auto_size_falloff(context, axis=None):
    """Auto-size the falloff handles to the selection.
    axis=None  → PCA dominant axis (follows geometry inclination, matches Modo).
    axis='X/Y/Z' → forced world axis; midpoint on the other two axes."""
    props  = context.scene.modokit_falloff
    coords = []
    if context.mode == 'EDIT_MESH':
        for obj in context.objects_in_mode_unique_data:
            if obj.type != 'MESH':
                continue
            bm_obj = bmesh.from_edit_mesh(obj.data)
            mx     = obj.matrix_world
            sel    = [mx @ v.co for v in bm_obj.verts if v.select]
            coords.extend(sel if sel else [mx @ v.co for v in bm_obj.verts])
    elif context.mode == 'OBJECT':
        coords = [obj.matrix_world.translation.copy()
                  for obj in context.selected_objects]
    if not coords:
        return

    if axis is None:
        if len(coords) < 2:
            # Single point — nothing to orient
            props.start = tuple(coords[0])
            props.end   = tuple(coords[0])
            return

        centroid, direction = _pca_dominant_axis(coords)

        # Project every point onto the dominant axis to find extent
        projs  = [(c - centroid).dot(direction) for c in coords]
        p_min  = min(projs)
        p_max  = max(projs)

        pt_start = centroid + direction * p_max  # high end
        pt_end   = centroid + direction * p_min  # low end

        # Start = whichever end is higher in Z (Modo default: top = 100%)
        if pt_start.z >= pt_end.z:
            props.start = tuple(pt_start)
            props.end   = tuple(pt_end)
        else:
            props.start = tuple(pt_end)
            props.end   = tuple(pt_start)
    else:
        # Axis-constrained: bbox extreme on chosen axis, midpoint on the others
        xs = [c.x for c in coords]; ys = [c.y for c in coords]; zs = [c.z for c in coords]
        min_co = _Vector((min(xs), min(ys), min(zs)))
        max_co = _Vector((max(xs), max(ys), max(zs)))
        cx = (min_co.x + max_co.x) * 0.5
        cy = (min_co.y + max_co.y) * 0.5
        cz = (min_co.z + max_co.z) * 0.5
        if axis == 'X':
            props.start = (max_co.x, cy, cz)
            props.end   = (min_co.x, cy, cz)
        elif axis == 'Y':
            props.start = (cx, max_co.y, cz)
            props.end   = (cx, min_co.y, cz)
        else:  # Z
            props.start = (cx, cy, max_co.z)
            props.end   = (cx, cy, min_co.z)


# ── Falloff draw callbacks ─────────────────────────────────────────────────────

def _falloff_handles_draw_callback():
    """POST_PIXEL callback — draw the falloff tapered wedge and START/END crosshair handles.
    Always visible when enabled; 'show' only controls the mesh weight overlay."""
    try:
        ctx   = bpy.context
        props = getattr(ctx.scene, 'modokit_falloff', None)
        if props is None or not props.enabled:   # no 'show' guard — handles always visible
            return
        import gpu
        from gpu_extras.batch import batch_for_shader
        from bpy_extras import view3d_utils

        region = ctx.region
        rv3d   = ctx.region_data
        if region is None or rv3d is None:
            return

        start_s = view3d_utils.location_3d_to_region_2d(
            region, rv3d, _Vector(props.start))
        end_s   = view3d_utils.location_3d_to_region_2d(
            region, rv3d, _Vector(props.end))
        if start_s is None or end_s is None:
            return

        sx, sy = start_s.x, start_s.y
        ex, ey = end_s.x,   end_s.y
        # Cache for hit-testing by the hover/drag operators
        state._falloff_screen_handles = {'START': (sx, sy), 'END': (ex, ey)}
        dx, dy = ex - sx, ey - sy
        seg_len = math.sqrt(dx * dx + dy * dy)
        if seg_len > 0.5:
            px, py = -dy / seg_len, dx / seg_len   # perpendicular (90° CCW)
        else:
            px, py = 0.0, 1.0

        hover = state._falloff_hover_handle

        gpu.state.blend_set('ALPHA')
        flat = gpu.shader.from_builtin('UNIFORM_COLOR')
        flat.bind()

        # ── Tapered wedge outline only ─────────────────────────────────────────
        # Wide at Start (100% influence), tapers to point at End (0%).
        W = 34.0   # half-width at Start, in pixels
        w1x = sx + px * W;  w1y = sy + py * W   # Start left edge
        w2x = sx - px * W;  w2y = sy - py * W   # Start right edge
        # Desaturated magenta outline
        flat.uniform_float('color', (0.75, 0.48, 0.82, 0.85))
        gpu.state.line_width_set(1.5)
        batch_for_shader(flat, 'LINES', {
            'pos': [(w1x, w1y), (w2x, w2y),
                    (w1x, w1y), (ex,  ey),
                    (w2x, w2y), (ex,  ey)]
        }).draw(flat)
        gpu.state.line_width_set(1.0)

        # ── 3-D crosshair handles ──────────────────────────────────────────────
        # Project RGB XYZ arms from world space so the crosshair looks 3-D.
        cam_right = _Vector(rv3d.view_matrix.inverted().col[0][:3]).normalized()
        ref_s_start = view3d_utils.location_3d_to_region_2d(
            region, rv3d, _Vector(props.start) + cam_right)
        if ref_s_start is not None:
            ppu   = math.sqrt((ref_s_start.x - sx)**2 + (ref_s_start.y - sy)**2)
            arm_w = max(0.001, 20.0 / ppu) if ppu > 0.01 else 0.15
        else:
            arm_w = 0.15

        _AX_VECS = (
            (_Vector((1.0, 0.0, 0.0)), (0.93, 0.21, 0.31, 1.0)),   # X red
            (_Vector((0.0, 1.0, 0.0)), (0.55, 0.86, 0.00, 1.0)),   # Y green
            (_Vector((0.0, 0.0, 1.0)), (0.13, 0.55, 0.86, 1.0)),   # Z blue
        )

        def _draw_handle_crosshair(center_w, dot_col):
            cs = view3d_utils.location_3d_to_region_2d(region, rv3d, center_w)
            if cs is None:
                return
            cx2, cy2 = cs.x, cs.y
            for ax, ac in _AX_VECS:
                p0 = view3d_utils.location_3d_to_region_2d(
                    region, rv3d, center_w - ax * arm_w)
                p1 = view3d_utils.location_3d_to_region_2d(
                    region, rv3d, center_w + ax * arm_w)
                if p0 is None or p1 is None:
                    continue
                # Dark outline
                flat.uniform_float('color', (ac[0]*0.2, ac[1]*0.2, ac[2]*0.2, 1.0))
                gpu.state.line_width_set(3.5)
                batch_for_shader(flat, 'LINES',
                                 {'pos': [(p0.x, p0.y), (p1.x, p1.y)]}).draw(flat)
                # Bright arm
                flat.uniform_float('color', ac)
                gpu.state.line_width_set(1.8)
                batch_for_shader(flat, 'LINES',
                                 {'pos': [(p0.x, p0.y), (p1.x, p1.y)]}).draw(flat)
            gpu.state.line_width_set(1.0)
            # Small filled dot at handle centre
            R = 5.5
            segs = 16
            vtri = []
            for i in range(segs):
                a0 = math.tau * i / segs
                a1 = math.tau * (i + 1) / segs
                vtri += [(cx2, cy2),
                         (cx2 + R * math.cos(a0), cy2 + R * math.sin(a0)),
                         (cx2 + R * math.cos(a1), cy2 + R * math.sin(a1))]
            # White border
            flat.uniform_float('color', (1.0, 1.0, 1.0, 0.80))
            Rb = R + 1.5
            vborder = []
            for i in range(segs):
                a0 = math.tau * i / segs
                a1 = math.tau * (i + 1) / segs
                vborder += [(cx2, cy2),
                            (cx2 + Rb * math.cos(a0), cy2 + Rb * math.sin(a0)),
                            (cx2 + Rb * math.cos(a1), cy2 + Rb * math.sin(a1))]
            batch_for_shader(flat, 'TRIS', {'pos': vborder}).draw(flat)
            flat.uniform_float('color', dot_col)
            batch_for_shader(flat, 'TRIS', {'pos': vtri}).draw(flat)

        start_col = (1.0, 0.95, 0.3, 1.0)  if hover == 'START' else _FALLOFF_COL_START
        end_col   = (0.6,  0.1, 1.0, 1.0)  if hover == 'END'   else _FALLOFF_COL_END
        _draw_handle_crosshair(_Vector(props.start), start_col)
        _draw_handle_crosshair(_Vector(props.end),   end_col)

        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _falloff_mesh_overlay_draw_callback():
    """POST_VIEW callback — draw falloff weight influence as a coloured face overlay.
    Dark purple = 0 % influence, yellow = 100 %.
    Controlled exclusively by props.show; the handles are separate."""
    try:
        ctx   = bpy.context
        props = getattr(ctx.scene, 'modokit_falloff', None)
        if props is None or not props.enabled or not props.show:
            return
        if ctx.mode != 'EDIT_MESH':
            return
        import gpu
        from gpu_extras.batch import batch_for_shader

        tri_verts  = []
        tri_colors = []
        for obj in ctx.objects_in_mode_unique_data:
            if obj.type != 'MESH':
                continue
            bm_obj = bmesh.from_edit_mesh(obj.data)
            mx     = obj.matrix_world
            bm_obj.verts.ensure_lookup_table()
            bm_obj.faces.ensure_lookup_table()
            # Precompute per-vertex weights (avoid repeated per-face lookups)
            vert_w = [_falloff_linear_weight(mx @ v.co, props) for v in bm_obj.verts]
            # Fan-triangulate every face (does not modify the bmesh)
            for f in bm_obj.faces:
                fv = f.verts[:]
                v0 = fv[0]
                for i in range(1, len(fv) - 1):
                    v1, v2 = fv[i], fv[i + 1]
                    for v in (v0, v1, v2):
                        tri_verts.append(tuple(mx @ v.co))
                        w = vert_w[v.index]
                        # interpolate dark purple (w=0) → yellow (w=1), semi-transparent
                        tri_colors.append((
                            w * 1.00 + (1.0 - w) * 0.15,
                            w * 0.83 + (1.0 - w) * 0.00,
                            w * 0.00 + (1.0 - w) * 0.35,
                            0.55,
                        ))

        if not tri_verts:
            return
        shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        batch_for_shader(shader, 'TRIS',
                         {'pos': tri_verts, 'color': tri_colors}).draw(shader)
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _start_falloff_handles():
    """Register the falloff handle draw handler if not already running."""
    if state._falloff_draw_handle is None:
        state._falloff_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _falloff_handles_draw_callback, (), 'WINDOW', 'POST_PIXEL')


def _stop_falloff_handles():
    """Remove the falloff handle draw handler."""
    if state._falloff_draw_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                state._falloff_draw_handle, 'WINDOW')
        except Exception:
            pass
        state._falloff_draw_handle = None
    state._falloff_screen_handles = {}
    state._falloff_hover_handle   = ''


def _start_falloff_mesh_overlay():
    """Register the per-vertex weight overlay draw handler if not already running."""
    if state._falloff_mesh_draw_handle is None:
        state._falloff_mesh_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _falloff_mesh_overlay_draw_callback, (), 'WINDOW', 'POST_VIEW')


def _stop_falloff_mesh_overlay():
    """Remove the per-vertex weight overlay draw handler."""
    if state._falloff_mesh_draw_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                state._falloff_mesh_draw_handle, 'WINDOW')
        except Exception:
            pass
        state._falloff_mesh_draw_handle = None


def _falloff_hit_test(mx, my):
    """Return 'START', 'END', or '' for the handle under screen pixel (mx, my)."""
    h = state._falloff_screen_handles
    for name in ('START', 'END'):
        pos = h.get(name)
        if pos and math.sqrt((mx - pos[0])**2 + (my - pos[1])**2) <= _FALLOFF_HIT_R:
            return name
    return ''


# ── VIEW3D_OT_modo_falloff_handle_hover  (MOUSEMOVE) ─────────────────────────

class VIEW3D_OT_modo_falloff_handle_hover(bpy.types.Operator):
    """MOUSEMOVE: highlight the falloff handle under the cursor."""
    bl_idname  = 'view3d.modo_falloff_handle_hover'
    bl_label   = 'Falloff Handle Hover'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (getattr(getattr(context.scene, 'modokit_falloff', None), 'enabled', False)
                and context.space_data is not None
                and context.space_data.type == 'VIEW_3D')

    def invoke(self, context, event):
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        props = getattr(context.scene, 'modokit_falloff', None)
        # Stop if falloff was disabled
        if props is None or not props.enabled:
            state._falloff_hover_handle = ''
            return {'FINISHED'}

        if event.type == 'MOUSEMOVE':
            hit = _falloff_hit_test(event.mouse_region_x, event.mouse_region_y)
            if hit != state._falloff_hover_handle:
                state._falloff_hover_handle = hit
                if context.area:
                    context.area.tag_redraw()

        return {'PASS_THROUGH'}


# ── VIEW3D_OT_modo_falloff_handle_drag  (LMB modal) ──────────────────────────

class VIEW3D_OT_modo_falloff_handle_drag(bpy.types.Operator):
    """LMB on a falloff handle: drag to reposition it in world space.
    Respects Blender snap settings; Ctrl toggles snap on/off mid-drag."""
    bl_idname  = 'view3d.modo_falloff_handle_drag'
    bl_label   = 'Falloff Handle Drag'
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    @classmethod
    def poll(cls, context):
        return (getattr(getattr(context.scene, 'modokit_falloff', None), 'enabled', False)
                and context.region is not None
                and context.region.type == 'WINDOW'
                and context.space_data is not None
                and context.space_data.type == 'VIEW_3D')

    def invoke(self, context, event):
        mx, my = event.mouse_region_x, event.mouse_region_y
        hit = _falloff_hit_test(mx, my)
        if not hit:
            return {'PASS_THROUGH'}

        self._handle    = hit   # 'START' or 'END'
        props           = context.scene.modokit_falloff
        self._orig_pos  = tuple(props.start if hit == 'START' else props.end)

        rv3d = context.region_data
        if rv3d is None:
            return {'CANCELLED'}
        self._rv3d   = rv3d
        self._region = context.region
        self._start_world = _Vector(self._orig_pos)
        self._depth_co    = self._start_world

        # Snap: Ctrl overrides use_snap just like the Move tool modal
        self._ctrl_override   = False
        self._snap_was_on     = False
        self._snap_hl_owned   = False   # True if we registered the snap highlight handler
        if state._snap_highlight_draw_handle is None:
            state._snap_highlight_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                _snap_highlight_draw_callback, (), 'WINDOW', 'POST_PIXEL')
            self._snap_hl_owned = True
        self._apply_ctrl_snap(context, event.ctrl)

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    # ── snap helpers ──────────────────────────────────────────────────────────

    def _apply_ctrl_snap(self, context, ctrl_held):
        ts = context.tool_settings
        if ctrl_held and not self._ctrl_override:
            self._snap_was_on   = ts.use_snap
            ts.use_snap         = True
            self._ctrl_override = True
        elif not ctrl_held and self._ctrl_override:
            ts.use_snap         = self._snap_was_on
            self._ctrl_override = False

    def _restore_snap(self, context):
        self._apply_ctrl_snap(context, False)
        # Clear any leftover snap highlight
        state._snap_highlight = None
        # Remove the snap highlight draw handler if we registered it
        if self._snap_hl_owned and state._snap_highlight_draw_handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    state._snap_highlight_draw_handle, 'WINDOW')
            except Exception:
                pass
            state._snap_highlight_draw_handle = None
            self._snap_hl_owned = False

    # ── world pos from mouse ──────────────────────────────────────────────────

    def _mouse_to_world(self, mx, my):
        """Map region pixel coords to world space at the handle's view depth,
        or use the snap target world pos if snapping is active."""
        # Try snap first
        snap = _find_snap_target(bpy.context, mx, my)
        if snap is not None:
            state._snap_highlight = snap
            return _Vector(snap['world_pos'])
        state._snap_highlight = None

        from bpy_extras import view3d_utils
        origin = view3d_utils.region_2d_to_origin_3d(self._region, self._rv3d, (mx, my))
        dir_   = view3d_utils.region_2d_to_vector_3d(self._region, self._rv3d, (mx, my))
        if origin is None or dir_ is None:
            return self._start_world.copy()
        view_normal = _Vector(self._rv3d.view_matrix.row[2][:3]).normalized()
        denom = view_normal.dot(dir_)
        if abs(denom) < 1e-8:
            return self._start_world.copy()
        t = view_normal.dot(self._depth_co - origin) / denom
        return origin + dir_ * t

    def modal(self, context, event):
        props = getattr(context.scene, 'modokit_falloff', None)
        if props is None:
            self._restore_snap(context)
            return {'CANCELLED'}

        if event.type in ('LEFT_CTRL', 'RIGHT_CTRL', 'MOUSEMOVE'):
            self._apply_ctrl_snap(context, event.ctrl)

        if event.type == 'MOUSEMOVE':
            new_w = self._mouse_to_world(event.mouse_region_x, event.mouse_region_y)
            if self._handle == 'START':
                props.start = tuple(new_w)
            else:
                props.end = tuple(new_w)
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self._restore_snap(context)
            if context.area:
                context.area.tag_redraw()
            return {'FINISHED'}

        if event.type in ('RIGHTMOUSE', 'ESC'):
            if self._handle == 'START':
                props.start = self._orig_pos
            else:
                props.end = self._orig_pos
            self._restore_snap(context)
            if context.area:
                context.area.tag_redraw()
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}


# ── VIEW3D_OT_modo_linear_falloff  (Alt+F) ───────────────────────────────────

class VIEW3D_OT_modo_linear_falloff(bpy.types.Operator):
    """Toggle Modo-style linear falloff (Alt+F).
    First press enables falloff and auto-sizes to the selection bounding box.
    Second press disables falloff."""
    bl_idname  = 'view3d.modo_linear_falloff'
    bl_label   = 'Modo Linear Falloff'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'VIEW_3D'
                and context.mode in ('OBJECT', 'EDIT_MESH'))

    def execute(self, context):
        props = context.scene.modokit_falloff
        if props.enabled:
            props.enabled = False
            _stop_falloff_handles()
            _stop_falloff_mesh_overlay()
        else:
            props.enabled = True
            _auto_size_falloff(context)
            _start_falloff_handles()
            _start_falloff_mesh_overlay()
            # Start the handle hover modal
            try:
                bpy.ops.view3d.modo_falloff_handle_hover('INVOKE_DEFAULT')
            except Exception:
                pass
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


# ── VIEW3D_OT_modo_falloff_auto_size  (per-axis bbox sizing) ──────────────────

class VIEW3D_OT_modo_falloff_auto_size(bpy.types.Operator):
    """Auto-size falloff to selection bounding box on a specific axis."""
    bl_idname  = 'view3d.modo_falloff_auto_size'
    bl_label   = 'Auto Size Falloff'
    bl_options = {'REGISTER', 'UNDO'}

    axis: bpy.props.EnumProperty(
        items=[('X', 'X', 'Size to selection bbox along world X'),
               ('Y', 'Y', 'Size to selection bbox along world Y'),
               ('Z', 'Z', 'Size to selection bbox along world Z')],
        default='X',
    )

    @classmethod
    def poll(cls, context):
        fp = getattr(context.scene, 'modokit_falloff', None)
        return (fp is not None and fp.enabled
                and context.mode in ('OBJECT', 'EDIT_MESH'))

    def execute(self, context):
        _auto_size_falloff(context, axis=self.axis)
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


# ── VIEW3D_OT_modo_falloff_reverse  (swap start ↔ end) ───────────────────────

class VIEW3D_OT_modo_falloff_reverse(bpy.types.Operator):
    """Swap falloff Start and End positions (flips 100% ↔ 0% influence)."""
    bl_idname  = 'view3d.modo_falloff_reverse'
    bl_label   = 'Reverse Falloff'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        fp = getattr(context.scene, 'modokit_falloff', None)
        return fp is not None and fp.enabled

    def execute(self, context):
        props = context.scene.modokit_falloff
        old_start = tuple(props.start)
        props.start = tuple(props.end)
        props.end   = old_start
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


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
        # True-3D gizmo: every handle position is a real world-space point
        # projected through the full perspective matrix.  This guarantees
        # correct foreshortening, perspective convergence and consistent shape
        # at all camera angles, just like Blender's native scale gizmo.
        #
        # Step 1 — measure pixels-per-world-unit at the pivot depth using the
        #   camera-right vector (always perpendicular to view → stable reference).
        # Step 2 — derive arm_world so the arm appears ~ARM px on screen.
        # Step 3 — project every 3D endpoint with location_3d_to_region_2d.
        axis_vecs = {
            'X': _Vector(orient.col[0]).normalized(),
            'Y': _Vector(orient.col[1]).normalized(),
            'Z': _Vector(orient.col[2]).normalized(),
        }

        cam_right = _Vector(rv3d.view_matrix.inverted().col[0][:3]).normalized()
        cam_pos_w = _Vector(rv3d.view_matrix.inverted().col[3][:3])
        ref_s = view3d_utils.location_3d_to_region_2d(
            region, rv3d, pivot_w + cam_right)
        if ref_s is not None:
            ppu = math.sqrt((ref_s.x - px)**2 + (ref_s.y - py)**2)
            arm_world = ARM / ppu if ppu > 0.1 else ARM / 100.0
        else:
            arm_world = ARM / 100.0
        sz_world  = arm_world * PSZ / ARM   # world-space half-size for plane squares
        cube_hw   = arm_world * HW  / ARM   # world-space cube half-size
        gap_world = arm_world * GAP / ARM   # world-space gap at pivot before shaft starts
        xu = axis_vecs['X'];  yv = axis_vecs['Y'];  zw = axis_vecs['Z']
        # 6 cube faces: (world-space normal, 4 corner sign-tuples in winding order)
        _CUBE_FACES = [
            ( xu,  [(+1,-1,-1),(+1,+1,-1),(+1,+1,+1),(+1,-1,+1)]),
            (-xu,  [(-1,+1,-1),(-1,-1,-1),(-1,-1,+1),(-1,+1,+1)]),
            ( yv,  [(-1,+1,-1),(+1,+1,-1),(+1,+1,+1),(-1,+1,+1)]),
            (-yv,  [(+1,-1,-1),(-1,-1,-1),(-1,-1,+1),(+1,-1,+1)]),
            ( zw,  [(-1,-1,+1),(+1,-1,+1),(+1,+1,+1),(-1,+1,+1)]),
            (-zw,  [(-1,+1,-1),(+1,+1,-1),(+1,-1,-1),(-1,-1,-1)]),
        ]

        screen_dirs = {}
        arm_ends    = {}
        axis_fades  = {}   # 0.0 = invisible (camera along axis), 1.0 = full
        # Use cam→pivot ray for fade, not the global view_fwd.  In perspective
        # the off-centre ray is what actually determines apparent foreshortening.
        view_fwd = _Vector(rv3d.view_matrix.row[2][:3]).normalized()  # kept for ortho fallback
        if rv3d.is_perspective:
            cam_to_pivot = (pivot_w - cam_pos_w).normalized()
        else:
            cam_to_pivot = view_fwd
        for name, ax in axis_vecs.items():
            dot = abs(cam_to_pivot.dot(ax))   # 0 = perp to cam ray, 1 = along cam ray
            # fade: full alpha when dot < FADE_START, zero when dot > FADE_END
            FADE_START, FADE_END = 0.88, 0.98
            if dot >= FADE_END:
                axis_fades[name] = 0.0
                screen_dirs[name] = arm_ends[name] = None
                continue
            elif dot > FADE_START:
                axis_fades[name] = 1.0 - (dot - FADE_START) / (FADE_END - FADE_START)
            else:
                axis_fades[name] = 1.0
            end_s = view3d_utils.location_3d_to_region_2d(
                region, rv3d, pivot_w + ax * arm_world)
            if end_s is None:
                screen_dirs[name] = arm_ends[name] = None
                continue
            dx = end_s.x - px
            dy = end_s.y - py
            dl = math.sqrt(dx * dx + dy * dy)
            if dl < 1.0:   # axis pointing at/past camera
                screen_dirs[name] = arm_ends[name] = None
                continue
            screen_dirs[name] = (dx / dl, dy / dl)
            arm_ends[name]    = (end_s.x, end_s.y)

        # Plane handle centers and corners — all projected from true 3D positions
        plane_centers = {}
        plane_corners = {}   # 4 screen-space corners per plane, projected from 3D
        for pa, pb in (('X', 'Y'), ('X', 'Z'), ('Y', 'Z')):
            da = screen_dirs.get(pa)
            db = screen_dirs.get(pb)
            if da is None or db is None:
                plane_centers[pa+pb] = plane_corners[pa+pb] = None
                continue
            cross = abs(da[0]*db[1] - da[1]*db[0])
            if cross < 0.22:
                plane_centers[pa+pb] = plane_corners[pa+pb] = None
                continue
            ax_a = axis_vecs[pa]
            ax_b = axis_vecs[pb]
            ctr_w = pivot_w + (ax_a + ax_b) * (arm_world * PF)
            ctr_s = view3d_utils.location_3d_to_region_2d(region, rv3d, ctr_w)
            if ctr_s is None:
                plane_centers[pa+pb] = plane_corners[pa+pb] = None
                continue
            plane_centers[pa+pb] = (ctr_s.x, ctr_s.y)
            c1_s = view3d_utils.location_3d_to_region_2d(
                region, rv3d, ctr_w + ax_a * sz_world + ax_b * sz_world)
            c2_s = view3d_utils.location_3d_to_region_2d(
                region, rv3d, ctr_w - ax_a * sz_world + ax_b * sz_world)
            c3_s = view3d_utils.location_3d_to_region_2d(
                region, rv3d, ctr_w - ax_a * sz_world - ax_b * sz_world)
            c4_s = view3d_utils.location_3d_to_region_2d(
                region, rv3d, ctr_w + ax_a * sz_world - ax_b * sz_world)
            if None in (c1_s, c2_s, c3_s, c4_s):
                plane_corners[pa+pb] = None
            else:
                plane_corners[pa+pb] = (
                    (c1_s.x, c1_s.y), (c2_s.x, c2_s.y),
                    (c3_s.x, c3_s.y), (c4_s.x, c4_s.y),
                )

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
            fade = axis_fades.get(aname, 1.0)

            hover_hit = (hover == aname
                         or (len(hover) == 2 and aname in hover)
                         or hover == 'XYZ')
            if hover_hit:
                color = _SCALE_COL_HL
            else:
                color = (base_col[0], base_col[1], base_col[2], base_col[3] * fade)
            shaft_hw = 1.5 if hover_hit else 0.9

            # Shaft: both endpoints projected from true 3D world positions
            # so the arm has consistent world-space length at any camera angle.
            shaft_start_w = pivot_w + axis_vecs[aname] * gap_world
            shaft_end_w   = pivot_w + axis_vecs[aname] * (arm_world - cube_hw)
            ss_s = view3d_utils.location_3d_to_region_2d(region, rv3d, shaft_start_w)
            se_s = view3d_utils.location_3d_to_region_2d(region, rv3d, shaft_end_w)
            if ss_s is not None and se_s is not None:
                _draw_aa([((ss_s.x, ss_s.y), (se_s.x, se_s.y))], color, shaft_hw * 0.5 + 1.0)

            # ── 3-D cube at arm end ──────────────────────────────────────
            ctr_c = pivot_w + axis_vecs[aname] * arm_world
            cpts  = {}
            for su in (-1, 1):
                for sv in (-1, 1):
                    for sw in (-1, 1):
                        p3 = ctr_c + xu*(su*cube_hw) + yv*(sv*cube_hw) + zw*(sw*cube_hw)
                        s  = view3d_utils.location_3d_to_region_2d(region, rv3d, p3)
                        cpts[(su, sv, sw)] = (s.x, s.y) if s else None
            for fnorm, findices in _CUBE_FACES:
                # Per-face perspective-correct culling: use cam→face_center,
                # not the global view_fwd (which is only valid at screen centre).
                face_ctr_w = _Vector((0.0, 0.0, 0.0))
                for su2, sv2, sw2 in findices:
                    face_ctr_w += ctr_c + xu*(su2*cube_hw) + yv*(sv2*cube_hw) + zw*(sw2*cube_hw)
                face_ctr_w /= len(findices)
                to_cam = (cam_pos_w - face_ctr_w).normalized()
                ndot = to_cam.dot(fnorm)
                if ndot <= 0.0:
                    continue
                fp = [cpts.get(k) for k in findices]
                if any(p is None for p in fp):
                    continue
                shade    = ndot
                face_col = (color[0], color[1], color[2], color[3] * (0.3 + 0.7 * shade))
                a, b, cp, d = fp
                _draw_flat([a, b, cp, a, cp, d], face_col)
                _draw_aa([(a, b), (b, cp), (cp, d), (d, a)], color, 1.5)

        # ── Plane handles ─────────────────────────────────────────────────
        plane_axis_cols = {
            'XY': (_SCALE_COL_X, _SCALE_COL_Y),
            'XZ': (_SCALE_COL_X, _SCALE_COL_Z),
            'YZ': (_SCALE_COL_Y, _SCALE_COL_Z),
        }
        for pname, (col_a, col_b) in plane_axis_cols.items():
            corners = plane_corners.get(pname)
            if corners is None:
                continue
            is_hl = (hover == pname)
            # Plane fades when either contributing axis is strongly foreshortened
            fa = axis_fades.get(pname[0], 1.0)
            fb = axis_fades.get(pname[1], 1.0)
            plane_fade = fa * fb
            if plane_fade <= 0.0:
                continue
            blend_r = (col_a[0] + col_b[0]) * 0.5
            blend_g = (col_a[1] + col_b[1]) * 0.5
            blend_b = (col_a[2] + col_b[2]) * 0.5
            fill_col   = (blend_r, blend_g, blend_b, (0.35 if is_hl else 0.18) * plane_fade)
            border_col = _SCALE_COL_HL if is_hl else (blend_r, blend_g, blend_b, 0.85 * plane_fade)
            c1, c2, c3, c4 = corners
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

    _SHAFT_HIT_R = 8.0   # px proximity to arm shaft line segment

    def _seg_dist(ax, ay, bx, by):
        """Distance from (mx,my) to segment (ax,ay)-(bx,by)."""
        dx, dy = bx - ax, by - ay
        lsq = dx*dx + dy*dy
        if lsq < 1e-6:
            return math.sqrt((mx-ax)**2 + (my-ay)**2)
        t = max(0.0, min(1.0, ((mx-ax)*dx + (my-ay)*dy) / lsq))
        return math.sqrt((mx - ax - t*dx)**2 + (my - ay - t*dy)**2)

    best, best_d = '', float('inf')
    px, py = pivot if pivot else (mx, my)

    for name in ('X', 'Y', 'Z'):
        pos = h.get(name)
        if pos is None:
            continue
        d_tip   = math.sqrt((mx-pos[0])**2 + (my-pos[1])**2)
        d_shaft = _seg_dist(px, py, pos[0], pos[1])
        # Tip gets a generous click radius; shaft gets a tighter one.
        if d_tip <= _SCALE_HIT_R and d_tip < best_d:
            best_d, best = d_tip, name
        elif d_shaft <= _SHAFT_HIT_R and d_shaft < best_d:
            best_d, best = d_shaft, name

    for name in ('XY', 'XZ', 'YZ'):
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
                and context.region is not None
                and context.region.type == 'WINDOW'
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
    C mid-drag → flatten (×0).  V mid-drag → flip (×-1)."""
    bl_idname  = 'view3d.modo_scale_gizmo_drag'
    bl_label   = 'Scale Gizmo Drag'
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    scale_x:     FloatProperty(name="X", default=100.0,
                                soft_min=-500.0, soft_max=500.0, precision=1, step=100)
    scale_y:     FloatProperty(name="Y", default=100.0,
                                soft_min=-500.0, soft_max=500.0, precision=1, step=100)
    scale_z:     FloatProperty(name="Z", default=100.0,
                                soft_min=-500.0, soft_max=500.0, precision=1, step=100)
    pivot_loc:   FloatVectorProperty(name="Pivot", default=(0.0, 0.0, 0.0),
                                     size=3, options={'HIDDEN'})
    orient_type: StringProperty(name="Orientation", default="GLOBAL",
                                options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return (state._active_transform_mode == 'RESIZE'
                and context.region is not None
                and context.region.type == 'WINDOW'
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
        """Scale-value tuple for axis + factor s (used during live preview)."""
        a = self._axis
        if a == 'X':   return (s,   1.0, 1.0)
        if a == 'Y':   return (1.0, s,   1.0)
        if a == 'Z':   return (1.0, 1.0, s  )
        if a == 'XY':  return (s,   s,   1.0)
        if a == 'XZ':  return (s,   1.0, s  )
        if a == 'YZ':  return (1.0, s,   s  )
        return (s, s, s)

    # ── apply / restore ──────────────────────────────────────────────────────

    def _apply_live(self, context, s):
        sv = self._sv(s)
        M  = _sg_build_scale_matrix(self._pivot_w, self._orient, *sv)
        falloff_props = getattr(context.scene, 'modokit_falloff', None)
        use_falloff   = (falloff_props is not None and falloff_props.enabled)
        if context.mode == 'EDIT_MESH':
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm = bmesh.from_edit_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                inv = obj.matrix_world.inverted()
                for vi, orig_w in self._orig_verts.get(obj.name, {}).items():
                    if vi < len(bm.verts):
                        scaled_w = M @ orig_w
                        if use_falloff:
                            w = _falloff_linear_weight(orig_w, falloff_props)
                            bm.verts[vi].co = inv @ orig_w.lerp(scaled_w, w)
                        else:
                            bm.verts[vi].co = inv @ scaled_w
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

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        col = layout.column(align=True)
        col.prop(self, 'scale_x', text="Scale X %", slider=False)
        col.prop(self, 'scale_y', text="Y %",       slider=False)
        col.prop(self, 'scale_z', text="Z %",       slider=False)

    def execute(self, context):
        """Apply the scale — called on first finish (from modal) and on redo."""
        # On first call from modal the mesh is still in preview state; restore first.
        if hasattr(self, '_orig_verts') or hasattr(self, '_orig_matrices'):
            self._restore(context)
        sv = (self.scale_x / 100.0, self.scale_y / 100.0, self.scale_z / 100.0)
        if all(abs(v - 1.0) < 1e-9 for v in sv):
            return {'FINISHED'}
        pivot_w    = _Vector(self.pivot_loc)
        old_pp     = context.tool_settings.transform_pivot_point
        old_cursor = context.scene.cursor.location.copy()
        context.tool_settings.transform_pivot_point = 'CURSOR'
        context.scene.cursor.location = pivot_w
        is_flip = any(v < 0.0 for v in sv)
        try:
            bpy.ops.transform.resize(
                'EXEC_DEFAULT',
                value=sv,
                constraint_axis=(True, True, True),
                orient_type=self.orient_type,
            )
            if is_flip and context.mode == 'EDIT_MESH':
                bpy.ops.mesh.normals_make_consistent(inside=False)
        except Exception:
            pass
        finally:
            context.tool_settings.transform_pivot_point = old_pp
            context.scene.cursor.location = old_cursor
        return {'FINISHED'}

    def _commit(self, context, s):
        """Snapshot sv as percentages into operator properties (enables redo panel)."""
        sv = self._sv(s)
        self.scale_x     = sv[0] * 100.0
        self.scale_y     = sv[1] * 100.0
        self.scale_z     = sv[2] * 100.0
        self.pivot_loc   = self._pivot_w.to_tuple()
        self.orient_type = self._orient_t
        return self.execute(context)

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
            result = self._commit(context, self._last_s)
            if context.area:
                context.area.tag_redraw()
            return result

        if event.type == 'C' and event.value == 'PRESS':
            result = self._commit(context, 0.0)
            if context.area:
                context.area.tag_redraw()
            return result

        if event.type == 'V' and event.value == 'PRESS':
            result = self._commit(context, -1.0)
            if context.area:
                context.area.tag_redraw()
            return result

        if event.type in ('RIGHTMOUSE', 'ESC'):
            self._restore(context)
            if context.area:
                context.area.tag_redraw()
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}
