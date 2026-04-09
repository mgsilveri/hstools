"""
UV editor snap / transform utilities:
  _get_uv_snap_elements, _is_uv_snap_active, _get_uv_grid_size
  _snap_uv_translate, _snap_uv_cursor
  _find_uv_snap_target  +  IMAGE_OT_modo_uv_snap_highlight
  _collect_uv_transform_targets
  _uv_auto_drop_check
  _uv_drop_transform
"""

import math
import bpy
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader

from . import state
from .utils import get_addon_preferences, _uv_debug_log
from .uv_overlays import (
    _uv_view_to_region, _uv_region_to_view,
)


# ── Snap properties helpers ───────────────────────────────────────────────────

def _get_snap_elements(ts):
    """Return a set of generic snap element strings from tool_settings."""
    try:
        return set(ts.snap_elements)
    except Exception:
        pass
    try:
        return set(ts.snap_elements_individual)
    except Exception:
        pass
    return set()


def _get_uv_snap_elements(ts):
    """Return a set of UV snap element type strings."""
    for attr in ('snap_uv_element', 'snap_elements_uv'):
        val = getattr(ts, attr, None)
        if val is None:
            continue
        if isinstance(val, str):
            return {val}
        try:
            return set(val)
        except Exception:
            pass
    return _get_snap_elements(ts)


_snap_props_dumped: bool = False


def _dump_snap_props_once(ts) -> None:
    global _snap_props_dumped
    if _snap_props_dumped:
        return
    _snap_props_dumped = True
    snap_props = {}
    for attr in dir(ts):
        if 'snap' in attr.lower():
            try:
                snap_props[attr] = getattr(ts, attr)
            except Exception:
                snap_props[attr] = '<error>'
    lines = [f'  {k} = {v!r}' for k, v in sorted(snap_props.items())]
    _uv_debug_log('[UV-SNAP] tool_settings snap properties dump:\n' + '\n'.join(lines))


def _is_uv_snap_active(ts, ctrl_held):
    """Return True if UV snapping should be active (Ctrl inverts the toggle)."""
    uv_snap = getattr(ts, 'use_snap_uv', None)
    snap_on = uv_snap if isinstance(uv_snap, bool) else ts.use_snap
    return (not snap_on) if ctrl_held else snap_on


def _get_uv_grid_size(sima):
    """Return (grid_u, grid_v) — 1 texel if image loaded, else 1/8."""
    try:
        img = sima.image
        if img and img.size[0] > 0 and img.size[1] > 0:
            return 1.0 / img.size[0], 1.0 / img.size[1]
    except Exception:
        pass
    return 0.125, 0.125


# ── UV snap translate ─────────────────────────────────────────────────────────

def _snap_uv_translate(context, raw_du, raw_dv, uv_info, ctrl_held=False,
                       gizmo_center=None, mouse_screen=None):
    """Apply snap to a translate delta.  Returns (final_du, final_dv, snap_target)."""
    ts   = context.tool_settings
    region = context.region
    sima   = context.space_data
    _dbg   = getattr(get_addon_preferences(context), 'debug_uv_handle', False)

    snap_active = _is_uv_snap_active(ts, ctrl_held)
    if not snap_active:
        return raw_du, raw_dv, None
    if region is None or sima is None:
        return raw_du, raw_dv, None

    snap_els       = _get_uv_snap_elements(ts)
    want_vertex    = 'VERTEX' in snap_els
    want_increment = bool(snap_els & {'INCREMENT', 'GRID', 'PIXEL'})

    # ── Vertex snap ───────────────────────────────────────────────────────────
    if want_vertex:
        try:
            edit_objects = [o for o in context.objects_in_mode_unique_data if o.type == 'MESH']
        except AttributeError:
            o = context.edit_object
            edit_objects = [o] if o and o.type == 'MESH' else []
        if edit_objects:
            PREC = 6
            # Exclude the UVs that are being moved (initial positions from uv_info 5-tuple).
            excluded = set()
            for oname, fi, li, iu, iv in uv_info:
                excluded.add((round(iu, PREC), round(iv, PREC)))

            # Collect all non-excluded UVs from every object in edit mode.
            all_uvs = []
            for obj in edit_objects:
                bm = bmesh.from_edit_mesh(obj.data)
                uv_layer = bm.loops.layers.uv.verify()
                if not uv_layer:
                    continue
                for face in bm.faces:
                    for loop in face.loops:
                        uv_co = loop[uv_layer].uv
                        key = (round(uv_co.x, PREC), round(uv_co.y, PREC))
                        if key not in excluded:
                            all_uvs.append((uv_co.x, uv_co.y))

            if gizmo_center is not None:
                ref_u, ref_v = gizmo_center
            elif uv_info:
                ref_u, ref_v = uv_info[0][3], uv_info[0][4]  # 5-tuple: (oname,fi,li,u,v)
            else:
                ref_u, ref_v = 0.0, 0.0

            if mouse_screen is not None:
                sc_probe = mouse_screen
            else:
                tent_u = ref_u + raw_du
                tent_v = ref_v + raw_dv
                sc_probe = _uv_view_to_region(region, sima, tent_u, tent_v)

            UV_SNAP_R = 25.0
            best_screen_d = float('inf')
            best_off_u = best_off_v = None
            best_target = None

            if sc_probe is not None:
                for (tu, tv) in all_uvs:
                    sc_tgt = _uv_view_to_region(region, sima, tu, tv)
                    if sc_tgt is None:
                        continue
                    dx = sc_probe[0] - sc_tgt[0]
                    dy = sc_probe[1] - sc_tgt[1]
                    d  = math.sqrt(dx * dx + dy * dy)
                    if d < UV_SNAP_R and d < best_screen_d:
                        best_screen_d = d
                        best_off_u    = tu - ref_u
                        best_off_v    = tv - ref_v
                        best_target   = (tu, tv)

            if best_off_u is not None:
                if _dbg:
                    _uv_debug_log(
                        f"[UV-SNAP] vertex snap HIT: target={best_target} "
                        f"off=({best_off_u:.5f},{best_off_v:.5f}) "
                        f"screen_dist={best_screen_d:.1f}px"
                    )
                return best_off_u, best_off_v, best_target
            if not want_increment:
                return raw_du, raw_dv, None

    # ── Increment / grid snap ─────────────────────────────────────────────────
    if want_increment:
        gu, gv = _get_uv_grid_size(sima)
        return (round(raw_du / gu) * gu, round(raw_dv / gv) * gv, None)

    return raw_du, raw_dv, None


def _snap_uv_cursor(context, mx, my, ctrl_held=False):
    """Return a snapped UV (u, v) for the mouse position, or None."""
    ts = context.tool_settings
    if not _is_uv_snap_active(ts, ctrl_held):
        return None
    region = context.region
    sima   = context.space_data
    if region is None or sima is None or sima.type != 'IMAGE_EDITOR':
        return None
    snap_els       = _get_uv_snap_elements(ts)
    want_vertex    = 'VERTEX' in snap_els
    want_increment = 'INCREMENT' in snap_els or 'GRID' in snap_els or 'PIXEL' in snap_els
    if not want_vertex and not want_increment:
        want_vertex = True

    if want_vertex:
        try:
            edit_objects = [o for o in context.objects_in_mode_unique_data if o.type == 'MESH']
        except AttributeError:
            o = context.edit_object
            edit_objects = [o] if o and o.type == 'MESH' else []
        UV_SNAP_R = 25.0
        best_d = float('inf'); best_uv = None
        for obj in edit_objects:
            bm = bmesh.from_edit_mesh(obj.data)
            uv_layer = bm.loops.layers.uv.verify()
            if not uv_layer:
                continue
            for face in bm.faces:
                for loop in face.loops:
                    uv_co = loop[uv_layer].uv
                    sc = _uv_view_to_region(region, sima, uv_co.x, uv_co.y)
                    if sc is not None:
                        dx = sc[0] - mx; dy = sc[1] - my
                        d = math.sqrt(dx * dx + dy * dy)
                        if d < UV_SNAP_R and d < best_d:
                            best_d = d; best_uv = (uv_co.x, uv_co.y)
        if best_uv is not None:
            return best_uv

    if want_increment:
        raw = _uv_region_to_view(region, sima, mx, my)
        if raw is None:
            return None
        gu, gv = _get_uv_grid_size(sima)
        return (round(raw[0] / gu) * gu, round(raw[1] / gv) * gv)

    return None


# ── UV snap highlight ─────────────────────────────────────────────────────────

def _find_uv_snap_target(context, mx, my, ctrl_held=False):
    """Find nearest snappable UV element. Returns dict or None."""
    UV_SNAP_SCREEN_RADIUS = 20

    region = context.region
    sima   = context.space_data
    if region is None or sima is None or sima.type != 'IMAGE_EDITOR':
        return None

    ts = context.tool_settings
    _dbg = getattr(get_addon_preferences(context), 'debug_uv_handle', False)
    if _dbg:
        _dump_snap_props_once(ts)
    if not _is_uv_snap_active(ts, ctrl_held):
        return None

    snap_els  = _get_uv_snap_elements(ts)
    want_vert = 'VERTEX' in snap_els or 'INCREMENT' in snap_els
    want_edge = 'EDGE_MIDPOINT' in snap_els or 'EDGE' in snap_els
    if not want_vert and not want_edge:
        want_vert = True

    try:
        edit_objects = [o for o in context.objects_in_mode_unique_data if o.type == 'MESH']
    except AttributeError:
        o = context.edit_object
        edit_objects = [o] if o and o.type == 'MESH' else []
    if not edit_objects:
        return None

    best_sdist = float('inf')
    best       = None
    use_sync   = ts.use_uv_select_sync

    for obj in edit_objects:
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.verify()
        if uv_layer is None:
            continue
        for face in bm.faces:
            if not use_sync and not face.select:
                continue
            loops   = face.loops
            n_loops = len(loops)
            for i, loop in enumerate(loops):
                uv_co = loop[uv_layer].uv
                if want_vert:
                    sc = _uv_view_to_region(region, sima, uv_co.x, uv_co.y)
                    if sc is not None:
                        dx, dy = sc[0] - mx, sc[1] - my
                        sd = math.sqrt(dx * dx + dy * dy)
                        if sd < UV_SNAP_SCREEN_RADIUS and sd < best_sdist:
                            best_sdist = sd
                            best = {'screen_pos': sc,
                                    'uv_pos': (uv_co.x, uv_co.y),
                                    'elem_type': 'UV_VERTEX'}
                if want_edge:
                    next_uv = loops[(i + 1) % n_loops][uv_layer].uv
                    mid_u = (uv_co.x + next_uv.x) * 0.5
                    mid_v = (uv_co.y + next_uv.y) * 0.5
                    sc = _uv_view_to_region(region, sima, mid_u, mid_v)
                    if sc is not None:
                        dx, dy = sc[0] - mx, sc[1] - my
                        sd = math.sqrt(dx * dx + dy * dy)
                        if sd < UV_SNAP_SCREEN_RADIUS and sd < best_sdist:
                            best_sdist = sd
                            best = {'screen_pos': sc,
                                    'uv_pos': (mid_u, mid_v),
                                    'elem_type': 'UV_EDGE_MIDPOINT'}
    return best


def _uv_snap_highlight_draw_callback():
    """GPU POST_PIXEL — draw a square vertex highlight at the UV snap target."""
    if state._uv_snap_highlight is None:
        return
    try:
        sx, sy = state._uv_snap_highlight['screen_pos']
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


class IMAGE_OT_modo_uv_snap_highlight(bpy.types.Operator):
    """Tracks mouse hover to highlight the nearest UV snap target."""
    bl_idname  = 'image.modo_uv_snap_highlight'
    bl_label   = 'Modo UV Snap Highlight'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    def invoke(self, context, event):
        self._ctrl_override = False
        self._snap_was_on   = False
        self._last_mx       = event.mouse_region_x
        self._last_my       = event.mouse_region_y
        self._last_ctrl     = event.ctrl
        context.window_manager.modal_handler_add(self)
        if state._uv_snap_highlight_draw_handle is None:
            state._uv_snap_highlight_draw_handle = bpy.types.SpaceImageEditor.draw_handler_add(
                _uv_snap_highlight_draw_callback, (), 'WINDOW', 'POST_PIXEL')
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
        if state._uv_active_transform_mode is None:
            self._apply_ctrl_snap(context, False)
            self._cleanup()
            return {'FINISHED'}

        self._apply_ctrl_snap(context, event.ctrl)

        if event.type == 'MOUSEMOVE':
            self._last_mx   = event.mouse_region_x
            self._last_my   = event.mouse_region_y
            self._last_ctrl = event.ctrl

        if state._uv_handle_modal_active:
            if state._uv_snap_highlight is not None:
                state._uv_snap_highlight = None
                if context.area:
                    context.area.tag_redraw()
        else:
            state._uv_snap_highlight = _find_uv_snap_target(
                context, self._last_mx, self._last_my,
                ctrl_held=self._last_ctrl)

        # ── Gizmo axis hover ──────────────────────────────────────────────────
        state._uv_gizmo_hover_axis = None
        if (state._uv_gizmo_center is not None
                and state._uv_active_transform_mode in ('TRANSLATE', 'RESIZE')):
            region = context.region
            sima   = context.space_data
            if region is not None and sima is not None:
                sc = _uv_view_to_region(region, sima,
                                        state._uv_gizmo_center[0],
                                        state._uv_gizmo_center[1])
                if sc is not None:
                    lx = self._last_mx - sc[0]
                    ly = self._last_my - sc[1]
                    ARM = 80.0; GAP = 10.0; PERP = 16.0
                    dist_sq = lx * lx + ly * ly
                    DOT_HIT = 12.0
                    if dist_sq <= DOT_HIT * DOT_HIT:
                        state._uv_gizmo_hover_axis = 'CENTER'
                    elif GAP <= lx <= ARM + 5 and abs(ly) <= PERP:
                        state._uv_gizmo_hover_axis = 'X'
                    elif GAP <= ly <= ARM + 5 and abs(lx) <= PERP:
                        state._uv_gizmo_hover_axis = 'Y'

        if context.area:
            context.area.tag_redraw()
        return {'PASS_THROUGH'}

    def _cleanup(self):
        state._uv_snap_highlight = None
        if state._uv_snap_highlight_draw_handle is not None:
            bpy.types.SpaceImageEditor.draw_handler_remove(
                state._uv_snap_highlight_draw_handle, 'WINDOW')
            state._uv_snap_highlight_draw_handle = None
        try:
            if bpy.context.area:
                bpy.context.area.tag_redraw()
        except Exception:
            pass


def _stop_uv_snap_highlight():
    state._uv_snap_highlight = None


# ── Collect UV transform targets ──────────────────────────────────────────────

def _collect_uv_transform_targets(context, override_sticky=None):
    """Return list of (obj_name, face_index, loop_offset, init_u, init_v) for selected UV corners.

    Iterates all mesh objects in edit mode so multi-object transforms work correctly.
    Vertex indices are scoped per-object (BMesh vert 0 is different in each mesh),
    so sticky-select logic runs independently per object.
    """
    try:
        edit_objects = [o for o in context.objects_in_mode_unique_data if o.type == 'MESH']
    except AttributeError:
        o = context.edit_object
        edit_objects = [o] if o and o.type == 'MESH' else []
    if not edit_objects:
        return []

    ts = context.tool_settings
    use_sync = ts.use_uv_select_sync
    sticky = getattr(ts, 'uv_sticky_select_mode', 'SHARED_VERTEX')
    PREC = 5

    if use_sync:
        mesh_mode = ts.mesh_select_mode

    result = []
    for obj in edit_objects:
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.verify()
        if uv_layer is None:
            continue
        bm.faces.ensure_lookup_table()

        all_loops = []
        for face in bm.faces:
            if not use_sync and not face.select:
                continue
            for li, loop in enumerate(face.loops):
                uv_data = loop[uv_layer]
                u, v = uv_data.uv.x, uv_data.uv.y
                vi = loop.vert.index
                if use_sync:
                    if mesh_mode[2]:
                        flag = face.select
                        all_loops.append((face.index, li, u, v, vi, flag, flag))
                    elif mesh_mode[1]:
                        uv_edge_flag  = (loop.uv_select_edge or loop.link_loop_prev.uv_select_edge)
                        mesh_edge_flag = (loop.edge.select or loop.link_loop_prev.edge.select)
                        all_loops.append((face.index, li, u, v, vi, uv_edge_flag, mesh_edge_flag))
                    else:
                        all_loops.append((face.index, li, u, v, vi,
                                          loop.uv_select_vert, loop.vert.select))
                else:
                    flag = loop.uv_select_vert
                    all_loops.append((face.index, li, u, v, vi, flag, flag))

        if use_sync and not (mesh_mode[2] if use_sync else False):
            use_uv_flag = any(is_uv for _, _, _, _, _, is_uv, _ in all_loops)
        else:
            use_uv_flag = True

        sel_verts = set()
        vert_sel_positions = {}
        for fi, li, u, v, vi, is_uv, is_mesh in all_loops:
            is_sel = is_uv if use_uv_flag else is_mesh
            if is_sel:
                sel_verts.add(vi)
                vert_sel_positions.setdefault(vi, set()).add((round(u, PREC), round(v, PREC)))

        effective_sticky = 'SHARED_LOCATION' if use_sync else sticky
        if override_sticky is not None:
            effective_sticky = override_sticky

        if effective_sticky == 'SHARED_VERTEX':
            for fi, li, u, v, vi, is_uv, is_mesh in all_loops:
                if vi in sel_verts:
                    result.append((obj.name, fi, li, u, v))
        elif effective_sticky == 'SHARED_LOCATION':
            for fi, li, u, v, vi, is_uv, is_mesh in all_loops:
                if vi in vert_sel_positions:
                    if (round(u, PREC), round(v, PREC)) in vert_sel_positions[vi]:
                        result.append((obj.name, fi, li, u, v))
        else:  # DISABLED
            for fi, li, u, v, vi, is_uv, is_mesh in all_loops:
                is_sel = is_uv if use_uv_flag else is_mesh
                if is_sel:
                    result.append((obj.name, fi, li, u, v))

    try:
        _dbg = getattr(get_addon_preferences(bpy.context), 'debug_uv_handle', False)
    except Exception:
        _dbg = False
    if _dbg:
        _uv_debug_log(
            f"[UV-COLLECT] sticky={sticky!r} use_sync={use_sync} result={len(result)}"
        )
    return result


# ── Auto-drop and drop transform ──────────────────────────────────────────────

def _uv_auto_drop_check():
    """Timer: auto-drop the UV gizmo when an external operation moves UV coords."""
    if state._uv_active_transform_mode is None:
        return None
    if state._uv_handle_modal_active:
        return 0.25
    if not state._uv_transform_targets:
        return 0.25
    try:
        context = bpy.context
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return 0.25
        try:
            obj_by_name = {o.name: o for o in context.objects_in_mode_unique_data
                           if o.type == 'MESH'}
        except AttributeError:
            obj = context.edit_object
            obj_by_name = {obj.name: obj} if obj and obj.type == 'MESH' else {}
        if not obj_by_name:
            return 0.25
        bm_cache = {}
        for oname, fi, li, u, v in state._uv_transform_targets[:5]:
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
                    cur = loops[li][uv_t].uv
                    if abs(cur.x - u) > 1e-6 or abs(cur.y - v) > 1e-6:
                        _uv_drop_transform(context)
                        return None
        return 0.25
    except Exception:
        return 0.25


def _uv_drop_transform(context):
    """Drop whichever UV W/E/R tool is active."""
    if state._uv_active_transform_mode is None:
        return
    _stop_uv_snap_highlight()
    from .uv_overlays import _stop_uv_gizmo
    _stop_uv_gizmo()
    state._uv_gizmo_center          = None
    state._uv_active_transform_mode = None
    state._uv_transform_targets     = None
    state._uv_sel_targets           = None
    if bpy.app.timers.is_registered(_uv_auto_drop_check):
        bpy.app.timers.unregister(_uv_auto_drop_check)
    try:
        bpy.ops.wm.tool_set_by_id(name='builtin.select', space_type='IMAGE_EDITOR')
    except Exception:
        pass
    sima = getattr(context, 'space_data', None)
    if sima and getattr(sima, 'type', '') == 'IMAGE_EDITOR':
        sima.show_gizmo = True
