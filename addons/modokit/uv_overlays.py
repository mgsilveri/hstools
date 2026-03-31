"""
UV editor overlay systems:
  UV gizmo (handle) draw + _sync/_read BMesh center
  _uv_undo_redo_handler
  _compute_uv_selection_median
  UV island boundary overlay (seam-partner highlight)
  UV flipped-face visualisation
  _resync_uv_editor_selection
  UV region utilities (_uv_view_to_region, clip helpers, etc.)
"""

import math
import time
import bpy
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader

from . import state
from .utils import get_addon_preferences, _uv_debug_log, _diag, perf_record

# ── Back-edge cache rebuild throttle ─────────────────────────────────────────
# Rebuilds are driven INLINE from the GPU draw callback (which fires at display
# fps), so the cache is always current by the time each frame is drawn.
# GPU draw callbacks fire after the active operator's modal/execute has returned,
# so the BMesh is always in a consistent state when we read it here.
#
# Adaptive interval: 3× measured rebuild time, clamped [4 ms, 16 ms].
# Hard max is ONE display frame (16ms / 60fps) so a single GPU-sync spike
# can never lock out rebuilds for more than one frame.
#
# _back_edge_dirty: set by the depsgraph handler when a confirmed mesh edit
# lands (select, extrude, confirm transform).  Causes the NEXT draw-callback
# rebuild to bypass the throttle entirely, ensuring the final committed
# position is always captured immediately.
#
# The depsgraph path is kept to handle UV cache updates and as a fallback for
# discrete edits (select, extrude, etc.) when the viewport isn't fully active.
_back_edge_min_interval: float = 0.016    # adapts after first rebuild
_back_edge_last_rebuild_time: float = 0.0
_back_edge_last_callback_time: float = 0.0  # time of last _bfv_rebuild_callback fire
_back_edge_last_drag_callback_time: float = 0.0  # time of last callback while TRANSFORM active
_back_edge_trailing_timer_pending: bool = False
_back_edge_dirty: bool = False            # bypass throttle on next rebuild


def _do_back_edge_rebuild(context, topo_only: bool = False) -> None:
    """Execute a back-edge cache rebuild and update adaptive interval."""
    global _back_edge_last_rebuild_time, _back_edge_min_interval, _back_edge_dirty
    _back_edge_dirty = False
    t0 = time.monotonic()
    t0_perf = time.perf_counter()  # high-res timer for duration measurement (monotonic ~10ms res on Windows)
    from .backface_viz import _compute_back_edge_cache, _bec_topo_valid
    # Fall back to full rebuild if topo cache isn't populated yet.
    if topo_only and not _bec_topo_valid:
        topo_only = False
    _compute_back_edge_cache(context, topo_only=topo_only)
    dur = time.perf_counter() - t0_perf  # sub-ms accurate
    if topo_only:
        perf_record("bec: topo/frame", dur, is_interval=True)
    if _back_edge_last_rebuild_time > 0:
        perf_record("bec: inter-rebuild interval",
                    time.monotonic() - _back_edge_last_rebuild_time, is_interval=True)
    _back_edge_last_rebuild_time = t0
    # Cap at 16ms (one 60fps frame) — prevents a single GPU-sync spike from
    # locking out all subsequent rebuilds for up to 50ms.
    _back_edge_min_interval = max(0.004, min(0.016, dur * 3))


def _has_active_mesh_transform(context) -> bool:
    """Return True if a live mesh-deforming modal operator is running (G/R/S etc.)."""
    op = getattr(context, 'active_operator', None)
    if op is None:
        return False
    return getattr(op, 'bl_idname', '').startswith('TRANSFORM_OT_')


def maybe_rebuild_back_edge(context) -> None:
    """Inline rebuild check — called from the GPU draw callback each frame.

    Three paths:
      1. _back_edge_dirty set (depsgraph confirmed change) → rebuild immediately.
      2. Live TRANSFORM_OT_* modal active → throttled per-frame rebuild to track
         vertex positions in real time (depsgraph fires too slowly during drags).
      3. No transform, not dirty → mesh unchanged, skip rebuild entirely.
    """
    global _back_edge_last_callback_time, _back_edge_last_drag_callback_time
    now = time.monotonic()
    if _back_edge_last_callback_time > 0:
        perf_record("bec: callback interval", now - _back_edge_last_callback_time, is_interval=True)
    _back_edge_last_callback_time = now

    # Path 1: depsgraph confirmed a mesh change — rebuild unconditionally.
    # POST_VIEW fires after all event/modal processing for the frame, so
    # calling from_edit_mesh here is safe even during native C modal operators.
    if _back_edge_dirty:
        perf_record("bec: rebuild reason", 2)  # 2=dirty flag
        _do_back_edge_rebuild(context)
        return

    # Path 2: live mesh transform (G/R/S etc.) — throttled per-frame rebuild.
    # Selection is stable during a transform, so reuse the topology cache and
    # only recompute vertex positions (skips the costly full-mesh select scan).
    if _has_active_mesh_transform(context):
        # Track drag-specific frame interval separately so perf_report shows
        # actual viewport fps during G/R/S without idle frames diluting it.
        if _back_edge_last_drag_callback_time > 0 and (now - _back_edge_last_drag_callback_time) < 1.0:
            perf_record("bec: drag/frame interval", now - _back_edge_last_drag_callback_time, is_interval=True)
        _back_edge_last_drag_callback_time = now
        elapsed = now - _back_edge_last_rebuild_time
        if elapsed < _back_edge_min_interval:
            perf_record("bec: throttle skip (ms remaining)", (_back_edge_min_interval - elapsed) * 1000)
            return
        perf_record("bec: rebuild reason", 1)  # 1=transform active
        _do_back_edge_rebuild(context, topo_only=True)
        return

    # Path 3: orbiting/idle — mesh unchanged, no work needed.
    # Reset drag timer so it doesn't carry a stale timestamp into the next drag.
    _back_edge_last_drag_callback_time = 0.0
    perf_record("bec: skip (no change)", 1)


def _back_edge_trailing_timer():
    """Trailing-edge timer: catches the final position after a transform ends.
    Fallback for cases where the draw callback doesn't fire (e.g. lost focus).
    No gen-counter check — always runs if still in EDIT_MESH.
    """
    global _back_edge_trailing_timer_pending
    _back_edge_trailing_timer_pending = False
    try:
        context = bpy.context
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return None
        # If the draw callback has fired since this timer was scheduled, it
        # already rebuilt — skip to avoid a redundant double rebuild.
        if _back_edge_last_callback_time > _back_edge_last_rebuild_time:
            return None
        _do_back_edge_rebuild(context)
    except Exception:
        pass
    return None  # self-terminate


# _get_snap_elements is used by uv_snap.py — imported lazily there.

# ── AA line shader (soft-edge quads, Vulkan/OpenGL safe) ──────────────────────
# Draws lines as screen-space quads with a smoothstep alpha falloff toward the
# edges, giving antialiased appearance without relying on gl_LineWidth (which
# is an optional Vulkan feature and often clamped to 1px).
_aa_line_shader_cache = None
_aa_line_3d_shader_cache = None

_AA_LINE_VERT_SRC = (
    'void main() {\n'
    '    gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);\n'
    '    vT = t;\n'   # t is in screen pixels: 0=centerline, ±half_w=geometric edge
    '}\n'
)
_AA_LINE_FRAG_SRC = (
    # vT in pixel distance from centerline; uhalf_w = quad half-width in pixels.
    # Solid core from 0 to (uhalf_w - 1.0), then 1px linear fade to transparent.
    'void main() {\n'
    '    float d = abs(vT);\n'
    '    float a = 1.0 - smoothstep(uhalf_w - 1.0, uhalf_w, d);\n'
    '    fragColor = vec4(ucolor.rgb, ucolor.a * a);\n'
    '}\n'
)


def _get_aa_line_shader():
    global _aa_line_shader_cache
    if _aa_line_shader_cache is None:
        try:
            info = gpu.types.GPUShaderCreateInfo()
            info.push_constant('MAT4', 'ModelViewProjectionMatrix')
            info.push_constant('VEC4', 'ucolor')
            info.push_constant('FLOAT', 'uhalf_w')
            info.vertex_in(0, 'VEC2', 'pos')
            info.vertex_in(1, 'FLOAT', 't')
            iface = gpu.types.GPUStageInterfaceInfo('aa_iface')
            iface.smooth('FLOAT', 'vT')
            info.vertex_out(iface)
            info.fragment_out(0, 'VEC4', 'fragColor')
            info.vertex_source(_AA_LINE_VERT_SRC)
            info.fragment_source(_AA_LINE_FRAG_SRC)
            _aa_line_shader_cache = gpu.shader.create_from_info(info)
        except Exception as e:
            print(f'[modokit] aa_line shader failed: {e}')
    return _aa_line_shader_cache


def _aa_line_quads(segments, half_w):
    """Build pos + t arrays for AA line quads from a list of (p0, p1) segments.

    t is in screen pixels (0 = centerline, ±half_w = geometric quad edge).
    half_w = visual_half_width + 1.0 to leave room for the 1px fringe.
    Returns (pos_list, t_list).
    """
    pos = []
    t_vals = []
    for (x0, y0), (x1, y1) in segments:
        dx = x1 - x0
        dy = y1 - y0
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-6:
            continue
        # Perpendicular unit vector scaled to half_w pixels
        nx = -dy / length * half_w
        ny =  dx / length * half_w
        a = (x0 + nx, y0 + ny);  ta =  half_w
        b = (x0 - nx, y0 - ny);  tb = -half_w
        c = (x1 + nx, y1 + ny);  tc =  half_w
        d = (x1 - nx, y1 - ny);  td = -half_w
        pos    += [a, b, c, b, d, c]
        t_vals += [ta, tb, tc, tb, td, tc]
    return pos, t_vals


# ── AA line shader — 3D world-space (POST_VIEW callbacks) ─────────────────────
# Uses "screen-space line expansion": each vertex carries its own 3D pos plus
# the other endpoint of the segment. The vertex shader projects both to NDC,
# computes the perpendicular in screen space, and offsets by ±half_w pixels.
# This gives true sub-pixel AA regardless of backend or camera angle.
_aa_line_3d_shader_cache = None

_AA_LINE_3D_VERT_SRC = (
    'void main() {\n'
    '    vec4 c0 = ModelViewProjectionMatrix * vec4(pos0, 1.0);\n'
    '    vec4 c1 = ModelViewProjectionMatrix * vec4(pos1, 1.0);\n'
    # Select the actual clip position for this vertex (which=0→p0, 1→p1)
    '    vec4 cp = mix(c0, c1, which);\n'
    # Direction always p0→p1 in NDC so perp is consistent for all 6 verts
    '    vec2 n0 = c0.xy / c0.w;\n'
    '    vec2 n1 = c1.xy / c1.w;\n'
    '    vec2 d  = n1 - n0;\n'
    '    float dl = length(d);\n'
    '    if (dl < 0.0001) { gl_Position = cp; vT = 0.0; return; }\n'
    '    d /= dl;\n'
    '    vec2 perp = vec2(-d.y, d.x);\n'
    '    vec2 off = perp * side * vec2(2.0 / uviewport.x, 2.0 / uviewport.y);\n'
    '    cp.xy += off * cp.w;\n'
    '    gl_Position = cp;\n'
    '    vT = side;\n'
    '}\n'
)
# Fragment shader is identical to the 2D version — reuse _AA_LINE_FRAG_SRC


def _get_aa_line_3d_shader():
    global _aa_line_3d_shader_cache
    if _aa_line_3d_shader_cache is None:
        try:
            info = gpu.types.GPUShaderCreateInfo()
            info.push_constant('MAT4', 'ModelViewProjectionMatrix')
            info.push_constant('VEC4', 'ucolor')
            info.push_constant('FLOAT', 'uhalf_w')
            info.push_constant('VEC2', 'uviewport')
            info.vertex_in(0, 'VEC3', 'pos0')
            info.vertex_in(1, 'VEC3', 'pos1')
            info.vertex_in(2, 'FLOAT', 'which')
            info.vertex_in(3, 'FLOAT', 'side')
            iface = gpu.types.GPUStageInterfaceInfo('aa3d_iface')
            iface.smooth('FLOAT', 'vT')
            info.vertex_out(iface)
            info.fragment_out(0, 'VEC4', 'fragColor')
            info.vertex_source(_AA_LINE_3D_VERT_SRC)
            info.fragment_source(_AA_LINE_FRAG_SRC)
            _aa_line_3d_shader_cache = gpu.shader.create_from_info(info)
        except Exception as e:
            print(f'[modokit] aa_line_3d shader failed: {e}')
    return _aa_line_3d_shader_cache


def _aa_line_quads_3d(segments, half_w):
    """Build pos0/pos1/which/side arrays for the 3D AA line shader.

    All 6 verts per segment carry both endpoints; direction is always p0→p1
    so the perpendicular is consistent and the quad doesn't twist.
    """
    pos0_l  = []
    pos1_l  = []
    which_l = []
    side_l  = []
    hw = float(half_w)
    for p0, p1 in segments:
        # 6 verts: tri1=(v0+,v0-,v1+)  tri2=(v0-,v1-,v1+)
        # which=0.0 → at p0, which=1.0 → at p1
        pos0_l  += [p0, p0, p0,  p0, p0, p0]
        pos1_l  += [p1, p1, p1,  p1, p1, p1]
        which_l += [0.0, 0.0, 1.0,  0.0, 1.0, 1.0]
        side_l  += [hw, -hw,  hw,  -hw,  hw, -hw]
    return pos0_l, pos1_l, which_l, side_l


# ── SDF circle shader (for the centre dot) ────────────────────────────────────
_dot_shader_cache = None

_DOT_VERT_SRC = (
    'void main() {\n'
    '    gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);\n'
    '}\n'
)
_DOT_FRAG_SRC = (
    # gl_FragCoord.xy is in region pixel space (same as cx/cy from _uv_view_to_region).
    'void main() {\n'
    '    float d = length(gl_FragCoord.xy - ucenter) - uradius;\n'
    '    float a = 1.0 - smoothstep(-0.5, 0.5, d);\n'
    '    fragColor = vec4(ucolor.rgb, ucolor.a * a);\n'
    '}\n'
)


def _get_dot_shader():
    global _dot_shader_cache
    if _dot_shader_cache is None:
        try:
            info = gpu.types.GPUShaderCreateInfo()
            info.push_constant('MAT4', 'ModelViewProjectionMatrix')
            info.push_constant('VEC4', 'ucolor')
            info.push_constant('VEC2', 'ucenter')
            info.push_constant('FLOAT', 'uradius')
            info.vertex_in(0, 'VEC2', 'pos')
            info.fragment_out(0, 'VEC4', 'fragColor')
            info.vertex_source(_DOT_VERT_SRC)
            info.fragment_source(_DOT_FRAG_SRC)
            _dot_shader_cache = gpu.shader.create_from_info(info)
        except Exception as e:
            print(f'[modokit] dot shader failed: {e}')
    return _dot_shader_cache


# ── Sync / read gizmo centre to/from BMesh ────────────────────────────────────

def _sync_uv_gizmo_center_to_bmesh(context):
    """Write state._uv_gizmo_center into BMesh custom float layers on vertex 0."""
    if state._uv_gizmo_center is None:
        return
    try:
        obj = context.edit_object
        if obj is None or obj.type != 'MESH':
            return
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        if len(bm.verts) == 0:
            return
        lu = bm.verts.layers.float.get('_gc_u') or bm.verts.layers.float.new('_gc_u')
        lv = bm.verts.layers.float.get('_gc_v') or bm.verts.layers.float.new('_gc_v')
        bm.verts[0][lu] = state._uv_gizmo_center[0]
        bm.verts[0][lv] = state._uv_gizmo_center[1]
    except Exception:
        pass


def _read_uv_gizmo_center_from_bmesh(context):
    """Read the gizmo centre from BMesh custom layers.  Returns (u, v) or None."""
    try:
        obj = context.edit_object
        if obj is None or obj.type != 'MESH':
            return None
        bm = bmesh.from_edit_mesh(obj.data)
        lu = bm.verts.layers.float.get('_gc_u')
        lv = bm.verts.layers.float.get('_gc_v')
        if lu is None or lv is None:
            return None
        bm.verts.ensure_lookup_table()
        if len(bm.verts) == 0:
            return None
        return (bm.verts[0][lu], bm.verts[0][lv])
    except Exception:
        return None


# ── Undo / redo handler ───────────────────────────────────────────────────────

@bpy.app.handlers.persistent
def _uv_undo_redo_handler(scene):
    """Restore the gizmo centre from BMesh custom layers after undo/redo."""
    if state._uv_active_transform_mode is None:
        return
    try:
        stored = _read_uv_gizmo_center_from_bmesh(bpy.context)
        if stored is not None and stored != (0.0, 0.0):
            state._uv_gizmo_center = stored
            _dbg = getattr(get_addon_preferences(bpy.context), 'debug_uv_handle', False)
            if _dbg:
                _uv_debug_log(f"[UV-GIZMO-CTR] undo/redo: restored from bmesh stored={stored}")
        else:
            median = _compute_uv_selection_median(bpy.context)
            if median is not None:
                state._uv_gizmo_center = median
                _dbg = getattr(get_addon_preferences(bpy.context), 'debug_uv_handle', False)
                if _dbg:
                    _uv_debug_log(f"[UV-GIZMO-CTR] undo/redo: fallback median={median}")
    except Exception:
        pass


# ── UV selection median ───────────────────────────────────────────────────────

def _compute_uv_selection_median(context):
    """Return the median UV position of all selected UV vertices as (u, v), or None.

    Respects *uv_sticky_select_mode* (Blender 5.0).
    """
    obj = context.edit_object
    if obj is None or obj.type != 'MESH':
        return None
    bm = bmesh.from_edit_mesh(obj.data)
    uv_layer = bm.loops.layers.uv.verify()
    if uv_layer is None:
        return None
    ts = context.tool_settings
    use_sync = ts.use_uv_select_sync

    PREC = 5
    seen = set()
    sticky = getattr(ts, 'uv_sticky_select_mode', 'SHARED_VERTEX')

    if use_sync:
        mesh_mode = ts.mesh_select_mode

    all_loops_raw = []
    for face in bm.faces:
        if not use_sync and not face.select:
            continue
        for loop in face.loops:
            uv_data = loop[uv_layer]
            u, v = uv_data.uv.x, uv_data.uv.y
            vi = loop.vert.index
            if use_sync:
                if mesh_mode[2]:
                    flag = face.select
                    all_loops_raw.append((vi, u, v, flag, flag))
                elif mesh_mode[1]:
                    uv_edge_flag = (loop.uv_select_edge or loop.link_loop_prev.uv_select_edge)
                    mesh_edge_flag = (loop.edge.select or loop.link_loop_prev.edge.select)
                    all_loops_raw.append((vi, u, v, uv_edge_flag, mesh_edge_flag))
                else:
                    all_loops_raw.append((vi, u, v, loop.uv_select_vert, loop.vert.select))
            else:
                flag = loop.uv_select_vert
                all_loops_raw.append((vi, u, v, flag, flag))

    if use_sync and not (mesh_mode[2] if use_sync else False):
        use_uv_flag = any(is_uv for _, _, _, is_uv, _ in all_loops_raw)
    else:
        use_uv_flag = True

    all_loops = []
    sel_verts = set()
    vert_sel_positions = {}
    for vi, u, v, is_uv, is_mesh in all_loops_raw:
        is_sel = is_uv if use_uv_flag else is_mesh
        all_loops.append((vi, u, v, is_sel))
        if is_sel:
            sel_verts.add(vi)
            pos_key = (round(u, PREC), round(v, PREC))
            vert_sel_positions.setdefault(vi, set()).add(pos_key)

    total_u, total_v, count = 0.0, 0.0, 0
    effective_sticky = 'DISABLED' if use_sync else sticky

    for vi, u, v, is_sel in all_loops:
        include = False
        if effective_sticky == 'SHARED_VERTEX':
            include = vi in sel_verts
        elif effective_sticky == 'SHARED_LOCATION':
            if vi in vert_sel_positions:
                include = (round(u, PREC), round(v, PREC)) in vert_sel_positions[vi]
        else:
            include = is_sel

        if not include:
            continue
        key = (round(u, 6), round(v, 6), vi)
        if key in seen:
            continue
        seen.add(key)
        total_u += u
        total_v += v
        count += 1

    result = (total_u / count, total_v / count) if count else None
    try:
        _dbg = getattr(get_addon_preferences(context), 'debug_uv_handle', False)
    except Exception:
        _dbg = False
    if _dbg:
        _uv_debug_log(
            f"[UV-MEDIAN] use_sync={use_sync} use_uv_flag={use_uv_flag} "
            f"sticky={sticky!r} effective={effective_sticky!r} "
            f"corners_included={count} result={result}"
        )
    return result


# ── UV gizmo draw ─────────────────────────────────────────────────────────────

def _uv_gizmo_draw_callback():
    """GPU POST_PIXEL callback — Blender-style transform gizmo for the UV editor."""
    if state._uv_active_transform_mode is None:
        return
    try:
        import math as _math
        context = bpy.context
        sima    = context.space_data
        if sima is None or sima.type != 'IMAGE_EDITOR':
            return
        region = context.region
        if region is None:
            return
        if state._uv_gizmo_center is None:
            return
        sc = _uv_view_to_region(region, sima, state._uv_gizmo_center[0], state._uv_gizmo_center[1])
        if sc is None:
            return
        cx, cy = sc

        ARM = 80.0; GAP = 10.0; SHAFT_W = 1.0; HL_W = 1.75
        ARROW_L = 14.0; ARROW_HW = 5.5; SQ = 5.0; DOT_R = 5.0

        COL_X  = (0.93, 0.21, 0.31, 1.0)
        COL_Y  = (0.55, 0.86, 0.0,  1.0)
        COL_HL = (1.0,  1.0,  0.0,  1.0)
        COL_WHITE = (1.0, 1.0, 1.0, 0.9)

        hover = state._uv_gizmo_hover_axis
        x_col = COL_HL if hover == 'X' else COL_X
        y_col = COL_HL if hover == 'Y' else COL_Y
        x_hw  = (HL_W   if hover == 'X' else SHAFT_W) * 0.5 + 1.0   # core_half + 1px fringe
        y_hw  = (HL_W   if hover == 'Y' else SHAFT_W) * 0.5 + 1.0

        flat   = gpu.shader.from_builtin('UNIFORM_COLOR')
        aa     = _get_aa_line_shader()
        sdot   = _get_dot_shader()
        gpu.state.blend_set('ALPHA')

        def _draw_aa_line(segments, color, half_w, shader=aa, fallback=flat):
            """Draw line segments as AA quads if shader available, else LINES."""
            if shader is not None:
                pos, t_vals = _aa_line_quads(segments, half_w)
                if not pos:
                    return
                batch = batch_for_shader(shader, 'TRIS', {'pos': pos, 't': t_vals})
                shader.bind()
                shader.uniform_float('ucolor', color)
                shader.uniform_float('uhalf_w', half_w)
                batch.draw(shader)
            else:
                pts = []
                for p0, p1 in segments:
                    pts += [p0, p1]
                fallback.bind()
                fallback.uniform_float('color', color)
                gpu.state.line_width_set((half_w - 0.5) * 2)
                batch_for_shader(fallback, 'LINES', {'pos': pts}).draw(fallback)
                gpu.state.line_width_set(1.0)

        def _draw_arrow_aa(tip, bl, br, color):
            """Draw a filled arrow triangle then add AA outline along its 3 edges."""
            flat.bind()
            flat.uniform_float('color', color)
            batch_for_shader(flat, 'TRIS', {'pos': [tip, bl, br]}).draw(flat)
            # 1px AA fringe along each edge
            _draw_aa_line([(tip, br), (br, bl), (bl, tip)], color, 1.5)

        mode = state._uv_active_transform_mode
        if mode == 'TRANSLATE':
            _draw_aa_line(
                [((cx + GAP, cy), (cx + ARM - ARROW_L, cy))],
                x_col, x_hw)
            _draw_arrow_aa(
                (cx + ARM, cy),
                (cx + ARM - ARROW_L, cy - ARROW_HW),
                (cx + ARM - ARROW_L, cy + ARROW_HW),
                x_col)

            _draw_aa_line(
                [((cx, cy + GAP), (cx, cy + ARM - ARROW_L))],
                y_col, y_hw)
            _draw_arrow_aa(
                (cx, cy + ARM),
                (cx - ARROW_HW, cy + ARM - ARROW_L),
                (cx + ARROW_HW, cy + ARM - ARROW_L),
                y_col)

        elif mode == 'ROTATE':
            SEGMENTS = 64; RADIUS = ARM * 0.65
            arc_segs = []
            for i in range(SEGMENTS):
                a0 = 2.0 * _math.pi * i / SEGMENTS
                a1 = 2.0 * _math.pi * (i + 1) / SEGMENTS
                arc_segs.append((
                    (cx + RADIUS * _math.cos(a0), cy + RADIUS * _math.sin(a0)),
                    (cx + RADIUS * _math.cos(a1), cy + RADIUS * _math.sin(a1)),
                ))
            _draw_aa_line(arc_segs, COL_WHITE, SHAFT_W * 0.5 + 1.0)

        elif mode == 'RESIZE':
            _draw_aa_line(
                [((cx + GAP, cy), (cx + ARM - SQ, cy))],
                x_col, x_hw)
            ex = cx + ARM
            flat.bind()
            flat.uniform_float('color', x_col)
            sq_x = [(ex-SQ, cy-SQ), (ex+SQ, cy-SQ), (ex+SQ, cy+SQ),
                    (ex-SQ, cy-SQ), (ex+SQ, cy+SQ), (ex-SQ, cy+SQ)]
            batch_for_shader(flat, 'TRIS', {'pos': sq_x}).draw(flat)
            _draw_aa_line([(p, q) for p, q in [
                ((ex-SQ,cy-SQ),(ex+SQ,cy-SQ)), ((ex+SQ,cy-SQ),(ex+SQ,cy+SQ)),
                ((ex+SQ,cy+SQ),(ex-SQ,cy+SQ)), ((ex-SQ,cy+SQ),(ex-SQ,cy-SQ))
            ]], x_col, 1.5)

            _draw_aa_line(
                [((cx, cy + GAP), (cx, cy + ARM - SQ))],
                y_col, y_hw)
            ey = cy + ARM
            flat.bind()
            flat.uniform_float('color', y_col)
            sq_y = [(cx-SQ,ey-SQ),(cx+SQ,ey-SQ),(cx+SQ,ey+SQ),
                    (cx-SQ,ey-SQ),(cx+SQ,ey+SQ),(cx-SQ,ey+SQ)]
            batch_for_shader(flat, 'TRIS', {'pos': sq_y}).draw(flat)
            _draw_aa_line([(p, q) for p, q in [
                ((cx-SQ,ey-SQ),(cx+SQ,ey-SQ)), ((cx+SQ,ey-SQ),(cx+SQ,ey+SQ)),
                ((cx+SQ,ey+SQ),(cx-SQ,ey+SQ)), ((cx-SQ,ey+SQ),(cx-SQ,ey-SQ))
            ]], y_col, 1.5)

        # Centre dot — SDF circle for perfect AA
        dot_col = COL_HL if hover == 'CENTER' else COL_WHITE
        dot_r   = DOT_R * 1.3 if hover == 'CENTER' else DOT_R
        if sdot is not None:
            hw = dot_r + 2.0
            quad = [(cx-hw, cy-hw), (cx+hw, cy-hw), (cx+hw, cy+hw),
                    (cx-hw, cy-hw), (cx+hw, cy+hw), (cx-hw, cy+hw)]
            batch = batch_for_shader(sdot, 'TRIS', {'pos': quad})
            sdot.bind()
            sdot.uniform_float('ucolor', dot_col)
            sdot.uniform_float('ucenter', (cx, cy))
            sdot.uniform_float('uradius', dot_r)
            batch.draw(sdot)
        else:
            DOT_SEGS = 24
            flat.bind()
            flat.uniform_float('color', dot_col)
            dot_tris = []
            for i in range(DOT_SEGS):
                a0 = 2.0 * _math.pi * i / DOT_SEGS
                a1 = 2.0 * _math.pi * (i + 1) / DOT_SEGS
                dot_tris += [(cx, cy),
                             (cx + dot_r * _math.cos(a0), cy + dot_r * _math.sin(a0)),
                             (cx + dot_r * _math.cos(a1), cy + dot_r * _math.sin(a1))]
            batch_for_shader(flat, 'TRIS', {'pos': dot_tris}).draw(flat)

        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _start_uv_gizmo():
    if state._uv_gizmo_draw_handle is None:
        state._uv_gizmo_draw_handle = bpy.types.SpaceImageEditor.draw_handler_add(
            _uv_gizmo_draw_callback, (), 'WINDOW', 'POST_PIXEL')


def _stop_uv_gizmo():
    if state._uv_gizmo_draw_handle is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(
                state._uv_gizmo_draw_handle, 'WINDOW')
        except Exception:
            pass
        state._uv_gizmo_draw_handle = None
    try:
        if bpy.context.area:
            bpy.context.area.tag_redraw()
    except Exception:
        pass


# ── UV seam partner helpers ───────────────────────────────────────────────────

def _compute_uv_seam_partner_verts(obj):
    """Return (u, v) tuples of seam-partner UV verts for the current selection."""
    try:
        _dbg = getattr(get_addon_preferences(bpy.context), 'debug_uv_seam', False)
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            return []
        ts = bpy.context.tool_settings
        use_sync = ts.use_uv_select_sync

        def rnd(x): return round(x, 5)

        vert_loops = {}
        for face in bm.faces:
            if not use_sync and not face.select:
                continue
            for loop in face.loops:
                vi = loop.vert.index
                uv = loop[uv_layer].uv
                if use_sync:
                    if not loop.vert.select:
                        sel = False
                    else:
                        try:
                            sel = loop.uv_select_vert
                        except AttributeError:
                            sel = True
                else:
                    try:
                        sel = loop.uv_select_vert
                    except AttributeError:
                        try:
                            sel = loop[uv_layer].select
                        except (AttributeError, KeyError):
                            sel = False
                vert_loops.setdefault(vi, []).append((uv.x, uv.y, sel))

        points = []
        seen = set()
        for vi, entries in vert_loops.items():
            sel_uvs = {(rnd(u), rnd(v)) for u, v, sel in entries if sel}
            if not sel_uvs:
                continue
            for (u, v, sel) in entries:
                if sel:
                    continue
                key = (rnd(u), rnd(v))
                if key in sel_uvs or key in seen:
                    continue
                seen.add(key)
                points.append((u, v))

        if _dbg:
            _uv_debug_log(f'[UV-Seam-DBG] verts: found {len(points)} partner verts (sync={use_sync})')
        return points
    except Exception as _e:
        if getattr(get_addon_preferences(bpy.context), 'debug_uv_seam', False):
            _uv_debug_log(f'[UV-Seam-DBG] verts: EXCEPTION {_e}')
        return []


def _compute_uv_seam_partner_segments(obj):
    """Return (u0,v0,u1,v1) tuples for seam-partner UV edges of the current selection."""
    EPS = 1e-5
    try:
        _dbg = getattr(get_addon_preferences(bpy.context), 'debug_uv_seam', False)
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            return []
        ts = bpy.context.tool_settings
        use_sync = ts.use_uv_select_sync

        segments = []
        seen = set()

        # Build set of selected UV edge keys (for suppressing already-orange partners)
        selected_uv_edge_keys: set = set()
        for _face in bm.faces:
            if _face.hide:
                continue
            for _loop in _face.loops:
                _nl = _loop.link_loop_next
                if use_sync:
                    try:
                        _esel = _loop.uv_select_edge
                    except AttributeError:
                        _esel = _loop.edge.select
                else:
                    try:
                        _esel = _loop.uv_select_edge
                    except AttributeError:
                        try:
                            _esel = _loop[uv_layer].select_edge
                        except (AttributeError, KeyError):
                            _esel = _loop.uv_select_vert and _nl.uv_select_vert
                if _esel:
                    _u0 = _loop[uv_layer].uv; _u1 = _nl[uv_layer].uv
                    selected_uv_edge_keys.add((round(_u0.x, 6), round(_u0.y, 6),
                                               round(_u1.x, 6), round(_u1.y, 6)))

        _sel_count = 0
        for face in bm.faces:
            if not use_sync and not face.select:
                continue
            for loop in face.loops:
                next_loop = loop.link_loop_next
                if use_sync:
                    if not loop.edge.select:
                        continue
                    try:
                        edge_sel = loop.uv_select_edge
                    except AttributeError:
                        edge_sel = True
                else:
                    edge_sel = False
                    try:
                        edge_sel = loop.uv_select_edge
                    except AttributeError:
                        try:
                            edge_sel = loop[uv_layer].select_edge
                        except (AttributeError, KeyError):
                            edge_sel = loop.uv_select_vert and next_loop.uv_select_vert

                if not edge_sel:
                    continue
                _sel_count += 1

                uv0 = loop[uv_layer].uv
                uv1 = next_loop[uv_layer].uv
                geo_edge = loop.edge

                for other in geo_edge.link_loops:
                    if other is loop:
                        continue
                    other_uv0 = other[uv_layer].uv
                    other_uv1 = other.link_loop_next[uv_layer].uv
                    positions_match = (
                        abs(uv0.x - other_uv1.x) < EPS and abs(uv0.y - other_uv1.y) < EPS
                        and abs(uv1.x - other_uv0.x) < EPS and abs(uv1.y - other_uv0.y) < EPS
                    )
                    if positions_match:
                        continue
                    partner_key = (round(other_uv0.x, 6), round(other_uv0.y, 6),
                                   round(other_uv1.x, 6), round(other_uv1.y, 6))
                    if partner_key in selected_uv_edge_keys or partner_key in seen:
                        continue
                    seen.add(partner_key)
                    segments.append((other_uv0.x, other_uv0.y, other_uv1.x, other_uv1.y))

        if _dbg:
            _uv_debug_log(f'[UV-Seam-DBG] edges: sel_loops={_sel_count} partner_segs={len(segments)} sync={use_sync}')
        return segments
    except Exception as _e:
        if getattr(get_addon_preferences(bpy.context), 'debug_uv_seam', False):
            _uv_debug_log(f'[UV-Seam-DBG] edges: EXCEPTION {_e}')
        return []


def _compute_uv_selected_verts(obj):
    """Return (u, v) tuples of currently-selected UV verts."""
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            return []
        ts = bpy.context.tool_settings
        use_sync = ts.use_uv_select_sync
        points = []
        seen = set()
        for face in bm.faces:
            if not use_sync and not face.select:
                continue
            for loop in face.loops:
                if use_sync:
                    if not loop.vert.select:
                        continue
                else:
                    try:
                        sel = loop.uv_select_vert
                    except AttributeError:
                        try:
                            sel = loop[uv_layer].select
                        except (AttributeError, KeyError):
                            sel = False
                    if not sel:
                        continue
                uv = loop[uv_layer].uv
                key = (round(uv.x, 5), round(uv.y, 5))
                if key in seen:
                    continue
                seen.add(key)
                points.append((uv.x, uv.y))
        return points
    except Exception:
        return []


def _compute_uv_boundary_cache(context):
    """Populate state._uv_boundary_cache from live BMesh. Safe Python context only."""
    state._uv_boundary_cache = {'uv_mode': None, 'points': [], 'sel_points': [], 'segments': []}
    _uv_debug_log("[UV-UBC] _compute_uv_boundary_cache called")
    try:
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return
        prefs = get_addon_preferences(context)
        if not getattr(prefs, 'enable_uv_boundary_overlay', True):
            return
        obj = getattr(context, 'edit_object', None)
        if obj is None or obj.type != 'MESH':
            return
        ts = context.tool_settings
        use_sync = ts.use_uv_select_sync
        if use_sync:
            mesh_sel = ts.mesh_select_mode
            if mesh_sel[1]:
                uv_mode = 'EDGE'
            elif mesh_sel[0]:
                uv_mode = 'VERTEX'
            else:
                return
        else:
            sima = getattr(context, 'space_data', None)
            raw = getattr(ts, 'uv_select_mode', None) or getattr(sima, 'uv_select_mode', 'EDGE')
            if isinstance(raw, set):
                uv_mode = 'VERTEX' if 'VERTEX' in raw else ('EDGE' if 'EDGE' in raw else 'FACE')
            else:
                uv_mode = str(raw)

        if uv_mode == 'VERTEX':
            state._uv_boundary_cache['uv_mode'] = 'VERTEX'
            state._uv_boundary_cache['points'] = _compute_uv_seam_partner_verts(obj)
            state._uv_boundary_cache['sel_points'] = _compute_uv_selected_verts(obj)
        elif uv_mode == 'EDGE':
            state._uv_boundary_cache['uv_mode'] = 'EDGE'
            state._uv_boundary_cache['segments'] = _compute_uv_seam_partner_segments(obj)
        else:
            state._uv_boundary_cache['uv_mode'] = uv_mode
    except Exception as _exc:
        _uv_debug_log(f"[UV-UBC] EXCEPTION: {_exc}")
        state._uv_boundary_cache = {'uv_mode': None, 'points': [], 'segments': []}


def _uv_boundary_draw_callback():
    """GPU POST_PIXEL — highlight seam-partner UV edges/verts in Modo purple."""
    try:
        if state._bfv_previous_mode != 'EDIT_MESH':
            return
        _diag("DRAW uv_boundary enter")
        prefs = get_addon_preferences(bpy.context)
        if not getattr(prefs, 'enable_uv_boundary_overlay', True):
            return
        context = bpy.context
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return
        sima = context.space_data
        if sima is None or sima.type != 'IMAGE_EDITOR':
            return
        region = context.region
        if region is None:
            return
        # Recompute live every draw so that UV selection changes are reflected
        # immediately — UV selection does not trigger a MESH depsgraph update,
        # so the timer-driven cache can be stale by several frames.
        _compute_uv_boundary_cache(context)
        uv_mode = state._uv_boundary_cache.get('uv_mode')
        if uv_mode is None:
            return

        COLOR = (0.66, 0.66, 1.0, 1.0)
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float('color', COLOR)

        if uv_mode == 'VERTEX':
            r = 4.0  # half-size of the square

            # Selected verts — use theme vertex_select color
            sel_points = state._uv_boundary_cache.get('sel_points', [])
            if sel_points:
                try:
                    sc_theme = bpy.context.preferences.themes[0].image_editor.vertex_select
                    sel_color = (sc_theme.r, sc_theme.g, sc_theme.b, 1.0)
                except Exception:
                    sel_color = (1.0, 0.62, 0.0, 1.0)
                shader.uniform_float('color', sel_color)
                tris = []
                for (u, v) in sel_points:
                    sc = _uv_view_to_region(region, sima, u, v)
                    if sc is None:
                        continue
                    cx, cy = sc
                    tris += [
                        (cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r),
                        (cx - r, cy - r), (cx + r, cy + r), (cx - r, cy + r),
                    ]
                if tris:
                    batch_for_shader(shader, 'TRIS', {'pos': tris}).draw(shader)

            # Seam-partner verts — purple
            points = state._uv_boundary_cache.get('points', [])
            if not points:
                gpu.state.blend_set('NONE')
                return
            shader.uniform_float('color', COLOR)
            tris = []
            for (u, v) in points:
                sc = _uv_view_to_region(region, sima, u, v)
                if sc is None:
                    continue
                cx, cy = sc
                tris += [
                    (cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r),
                    (cx - r, cy - r), (cx + r, cy + r), (cx - r, cy + r),
                ]
            if tris:
                batch_for_shader(shader, 'TRIS', {'pos': tris}).draw(shader)

        elif uv_mode == 'EDGE':
            segments = state._uv_boundary_cache.get('segments', [])
            if not segments:
                gpu.state.blend_set('NONE')
                return
            rw = region.width; rh = region.height
            seg_pts = []
            for (u0, v0, u1, v1) in segments:
                s0 = _uv_view_to_region_unclamped(region, u0, v0)
                s1 = _uv_view_to_region_unclamped(region, u1, v1)
                if s0 is None or s1 is None:
                    continue
                clipped = _clip_segment_to_rect(s0[0], s0[1], s1[0], s1[1], 0, 0, rw, rh)
                if clipped is not None:
                    seg_pts.append(((clipped[0], clipped[1]), (clipped[2], clipped[3])))
            if seg_pts:
                aa = _get_aa_line_shader()
                if aa is not None:
                    hw = 1.25
                    pos, t_vals = _aa_line_quads(seg_pts, hw)
                    aa.bind()
                    aa.uniform_float('ucolor', COLOR)
                    aa.uniform_float('uhalf_w', hw)
                    batch_for_shader(aa, 'TRIS', {'pos': pos, 't': t_vals}).draw(aa)
                else:
                    coords = [p for seg in seg_pts for p in seg]
                    gpu.state.line_width_set(1.5)
                    batch_for_shader(shader, 'LINES', {'pos': coords}).draw(shader)
                    gpu.state.line_width_set(1.0)

        gpu.state.blend_set('NONE')
    except Exception as _exc:
        try:
            _uv_debug_log(f"[UV-OVERLAY] draw callback EXCEPTION: {_exc}")
        except Exception:
            pass


@bpy.app.handlers.persistent
def _uv_seam_redraw_depsgraph_handler(scene, depsgraph):
    """Force-redraw IMAGE_EDITOR after edit-mesh updates; schedule UV cache timer."""
    try:
        context = bpy.context
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            if state._flipped_face_uv_cache or state._uv_boundary_cache.get('uv_mode') is not None:
                state._flipped_face_uv_cache = []
                state._uv_boundary_cache = {'uv_mode': None, 'points': [], 'segments': []}
            return
        if not depsgraph.id_type_updated('MESH'):
            return
        _uv_debug_log("[UV-DEPSGRAPH] MESH update detected, scheduling UV cache timer")
        global _back_edge_dirty
        _back_edge_dirty = True   # force next draw frame to rebuild regardless of throttle
        state._uv_cache_dirty_time = time.monotonic()
        state._uv_cache_dirty_gen += 1
        _gen = state._uv_cache_dirty_gen
        def _deferred(_captured_gen=_gen):
            return _refresh_uv_caches_timer(_captured_gen)
        bpy.app.timers.register(_deferred, first_interval=state._UV_STABLE_DELAY)
        prefs = get_addon_preferences(context)
        if not getattr(prefs, 'enable_uv_boundary_overlay', True):
            return
        screen = getattr(context, 'screen', None)
        if screen:
            for area in screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    area.tag_redraw()
    except Exception as _exc:
        _uv_debug_log(f"[UV-DEPSGRAPH] EXCEPTION: {_exc}")


def _refresh_uv_caches_timer(scheduled_gen=None, skip_back_edge=False):
    """Recompute UV draw caches — safe Python context only (timer or operator).

    *scheduled_gen* is the generation counter value at the time the depsgraph
    handler registered this timer.  If the counter has advanced since then,
    a newer MESH update is in flight and we bail completely — the newer timer
    will do the work.  This guarantees bmesh.from_edit_mesh() is never called
    while a modal operator (e.g. Loop Cut & Slide) is still modifying the mesh.
    """
    global _back_edge_trailing_timer_pending
    try:
        context = bpy.context
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            _uv_debug_log("[UV-TIMER] not in EDIT_MESH, skipping")
            return None
        # If a newer MESH update arrived since we were scheduled, bail — the
        # newer timer will run instead.
        if scheduled_gen is not None and scheduled_gen != state._uv_cache_dirty_gen:
            _uv_debug_log(f"[UV-TIMER] stale gen {scheduled_gen} vs {state._uv_cache_dirty_gen}, skipping")
            return None
        # If any modal operator is running it may be modifying the mesh —
        # reschedule until the modal exits rather than risk a crash.
        _active_op = getattr(context, 'active_operator', None)
        if _active_op is not None:
            _uv_debug_log(f"[UV-TIMER] modal op active ({getattr(_active_op, 'bl_idname', '?')}), rescheduling")
            _sg, _sb = scheduled_gen, skip_back_edge
            bpy.app.timers.register(
                lambda: _refresh_uv_caches_timer(_sg, _sb),
                first_interval=state._UV_STABLE_DELAY,
            )
            return None
        _uv_debug_log("[UV-TIMER] _refresh_uv_caches_timer firing")
        if not skip_back_edge:
            # Adaptive throttle for back-edge cache rebuilds.
            _now = time.monotonic()
            _elapsed = _now - _back_edge_last_rebuild_time
            # If the draw callback is actively firing, it owns all back-edge
            # rebuilds every frame — skip both the immediate rebuild and the
            # trailing timer to avoid redundant work.
            _draw_callback_active = (_now - _back_edge_last_callback_time) < 0.1
            if _draw_callback_active:
                pass  # draw callback will handle it
            elif _elapsed >= _back_edge_min_interval:
                _do_back_edge_rebuild(context)
            elif not _back_edge_trailing_timer_pending:
                # Too soon: schedule one trailing rebuild for the remaining interval.
                _back_edge_trailing_timer_pending = True
                _remaining = _back_edge_min_interval - _elapsed
                _uv_debug_log(f"[UV-TIMER] back-edge throttle: trailing in {_remaining:.3f}s")
                bpy.app.timers.register(_back_edge_trailing_timer, first_interval=_remaining)
        _compute_flipped_face_uv_cache(context)
        _compute_uv_boundary_cache(context)
        prefs = get_addon_preferences(context)
        if getattr(prefs, 'enable_uv_boundary_overlay', True):
            screen = getattr(context, 'screen', None)
            if screen:
                for area in screen.areas:
                    if area.type == 'IMAGE_EDITOR':
                        area.tag_redraw()
    except Exception as _exc:
        _uv_debug_log(f"[UV-TIMER] EXCEPTION: {_exc}")
    return None  # self-terminate; a new closure is registered on the next MESH update


def _start_uv_boundary_overlay():
    if state._uv_boundary_draw_handle is None:
        state._uv_boundary_draw_handle = bpy.types.SpaceImageEditor.draw_handler_add(
            _uv_boundary_draw_callback, (), 'WINDOW', 'POST_PIXEL')


def _stop_uv_boundary_overlay():
    if state._uv_boundary_draw_handle is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(
                state._uv_boundary_draw_handle, 'WINDOW')
        except Exception:
            pass
        state._uv_boundary_draw_handle = None


# ── Flipped UV face visualisation ─────────────────────────────────────────────

def _signed_area_uv(uvs):
    """Shoelace signed area. Negative => clockwise (flipped)."""
    area = 0.0
    n = len(uvs)
    for i in range(n):
        x0, y0 = uvs[i]; x1, y1 = uvs[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return area * 0.5


def _compute_flipped_face_uv_cache(context):
    """Populate state._flipped_face_uv_cache from live BMesh. Safe Python context only."""
    state._flipped_face_uv_cache = []
    _uv_debug_log("[UV-FFUVC] _compute_flipped_face_uv_cache called")
    try:
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return
        obj = getattr(context, 'edit_object', None)
        if obj is None or obj.type != 'MESH':
            return
        prefs = get_addon_preferences(context)
        if not getattr(prefs, 'enable_uv_flipped_face_viz', True):
            return
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            return
        ts = context.tool_settings
        use_sync = ts.use_uv_select_sync
        new_cache = []
        for face in bm.faces:
            if face.hide:
                continue
            if not use_sync and not face.select:
                continue
            if len(face.loops) < 3:
                continue
            uvs = [(lp[uv_layer].uv.x, lp[uv_layer].uv.y) for lp in face.loops]
            if _signed_area_uv(uvs) < 0.0:
                new_cache.append(uvs)
        state._flipped_face_uv_cache = new_cache
        _uv_debug_log(f"[UV-FFUVC] done faces={len(new_cache)} use_sync={use_sync}")
    except Exception as _exc:
        _uv_debug_log(f"[UV-FFUVC] EXCEPTION: {_exc}")
        state._flipped_face_uv_cache = []


def _uv_flipped_face_draw_callback():
    """GPU POST_PIXEL — shade flipped UV faces with Modo's olive/gold tint."""
    try:
        if not state._flipped_face_uv_cache:
            return
        if state._bfv_previous_mode != 'EDIT_MESH':
            return
        _diag("DRAW flipped_face enter")
        prefs = get_addon_preferences(bpy.context)
        if not getattr(prefs, 'enable_uv_flipped_face_viz', True):
            return
        context = bpy.context
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return
        sima = context.space_data
        if sima is None or sima.type != 'IMAGE_EDITOR':
            return
        region = context.region
        if region is None:
            return

        tris = []
        for uvs in state._flipped_face_uv_cache:
            sc = [_uv_view_to_region_unclamped(region, u, v) for (u, v) in uvs]
            v0 = sc[0]
            for i in range(1, len(sc) - 1):
                tris += [v0, sc[i], sc[i + 1]]

        if not tris:
            return

        FLIPPED_COLOR = (0.65, 0.62, 0.08, 0.45)
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float('color', FLIPPED_COLOR)
        batch_for_shader(shader, 'TRIS', {'pos': tris}).draw(shader)
        gpu.state.blend_set('NONE')
    except Exception as _exc:
        try:
            _uv_debug_log(f"[UV-FFCB] draw callback EXCEPTION: {_exc}")
        except Exception:
            pass


def _start_uv_flipped_face_viz():
    if state._uv_flipped_face_draw_handle is None:
        state._uv_flipped_face_draw_handle = bpy.types.SpaceImageEditor.draw_handler_add(
            _uv_flipped_face_draw_callback, (), 'WINDOW', 'POST_PIXEL')


def _stop_uv_flipped_face_viz():
    if state._uv_flipped_face_draw_handle is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(
                state._uv_flipped_face_draw_handle, 'WINDOW')
        except Exception:
            pass
        state._uv_flipped_face_draw_handle = None


# ── UV editor selection resync ────────────────────────────────────────────────

def _resync_uv_editor_selection(context, obj, select_mode, bm):
    """Rebuild Blender's UV element map by running uv.select_all(DESELECT) in
    IMAGE_EDITOR context, then re-applying the current BMesh selection."""
    if not context.tool_settings.use_uv_select_sync:
        return
    screen = getattr(context, 'screen', None)
    if not screen:
        return

    target_area = target_region = None
    for area in screen.areas:
        if area.type != 'IMAGE_EDITOR':
            continue
        for region in area.regions:
            if region.type == 'WINDOW':
                target_area = area; target_region = region; break
        if target_area:
            break
    if target_area is None:
        return

    bm.faces.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    sel_face_idxs = [f.index for f in bm.faces if f.select]
    sel_edge_idxs = [e.index for e in bm.edges if e.select]
    sel_vert_idxs = [v.index for v in bm.verts if v.select]
    act_face_idx  = bm.faces.active.index if bm.faces.active else None

    try:
        with context.temp_override(area=target_area, region=target_region):
            bpy.ops.uv.select_all(action='DESELECT')
    except Exception as _exc:
        _uv_debug_log(f"[UV-RESYNC] uv.select_all DESELECT failed: {_exc}")
        return

    bm2 = bmesh.from_edit_mesh(obj.data)
    bm2.faces.ensure_lookup_table()
    bm2.edges.ensure_lookup_table()
    bm2.verts.ensure_lookup_table()
    uv_layer = bm2.loops.layers.uv.active

    if select_mode[2]:
        for fi in sel_face_idxs:
            if fi < len(bm2.faces):
                face = bm2.faces[fi]
                face.select = True
                if uv_layer:
                    for lp in face.loops:
                        lp.uv_select_vert = True
                        lp.uv_select_edge = True
        if act_face_idx is not None and act_face_idx < len(bm2.faces):
            bm2.faces.active = bm2.faces[act_face_idx]
    elif select_mode[1]:
        for ei in sel_edge_idxs:
            if ei < len(bm2.edges):
                edge = bm2.edges[ei]
                edge.select = True
                if uv_layer:
                    for lp in edge.link_loops:
                        lp.uv_select_edge = True
                        lp.uv_select_vert = True
    else:
        for vi in sel_vert_idxs:
            if vi < len(bm2.verts):
                vert = bm2.verts[vi]
                vert.select = True
                if uv_layer:
                    for face in vert.link_faces:
                        for lp in face.loops:
                            if lp.vert == vert:
                                lp.uv_select_vert = True

    # In edge mode, skip select_flush_mode() to avoid auto-promoting faces
    # when all edges of a face are selected (matches Blender's own behaviour).
    if not select_mode[1]:
        bm2.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)
    _uv_debug_log(
        f"[UV-RESYNC] rebuilt: sel_faces={len(sel_face_idxs)} "
        f"sel_edges={len(sel_edge_idxs)} sel_verts={len(sel_vert_idxs)}"
    )


# ── UV region utilities ───────────────────────────────────────────────────────

def _uv_region_to_view(region, sima, x, y):
    try:
        uv_x, uv_y = region.view2d.region_to_view(x, y)
        return (uv_x, uv_y)
    except Exception:
        return None


def _uv_view_to_region(region, sima, uv_x, uv_y):
    """UV → region pixels; returns None if out of view."""
    try:
        rx, ry = region.view2d.view_to_region(uv_x, uv_y, clip=True)
        if rx >= 10000 or ry >= 10000:
            return None
        return (rx, ry)
    except Exception:
        return None


def _uv_view_to_region_unclamped(region, uv_x, uv_y):
    """UV → region pixels without clipping (may return off-screen coords)."""
    try:
        rx, ry = region.view2d.view_to_region(uv_x, uv_y, clip=False)
        return (rx, ry)
    except Exception:
        return None


def _clip_segment_to_rect(x0, y0, x1, y1, xmin, ymin, xmax, ymax):
    """Cohen-Sutherland line clip. Returns (cx0,cy0,cx1,cy1) or None."""
    INSIDE, LEFT, RIGHT, BOTTOM, TOP = 0, 1, 2, 4, 8

    def _code(x, y):
        c = INSIDE
        if x < xmin: c |= LEFT
        elif x > xmax: c |= RIGHT
        if y < ymin: c |= BOTTOM
        elif y > ymax: c |= TOP
        return c

    c0, c1 = _code(x0, y0), _code(x1, y1)
    while True:
        if not (c0 | c1):   return x0, y0, x1, y1
        if c0 & c1:         return None
        c_out = c0 if c0 else c1
        dx, dy = x1 - x0, y1 - y0
        if c_out & TOP:
            x = x0 + dx * (ymax - y0) / dy if dy else x0; y = ymax
        elif c_out & BOTTOM:
            x = x0 + dx * (ymin - y0) / dy if dy else x0; y = ymin
        elif c_out & RIGHT:
            y = y0 + dy * (xmax - x0) / dx if dx else y0; x = xmax
        else:
            y = y0 + dy * (xmin - x0) / dx if dx else y0; x = xmin
        if c_out == c0:
            x0, y0, c0 = x, y, _code(x, y)
        else:
            x1, y1, c1 = x, y, _code(x, y)


def _dist_point_to_segment_2d(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-12:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.sqrt((px - cx) ** 2 + (py - cy) ** 2)


def _point_in_poly_2d(px, py, poly):
    """Ray-casting point-in-polygon (2-D screen space)."""
    n = len(poly); inside = False; j = n - 1
    for i in range(n):
        xi, yi = poly[i]; xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_to_segment_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0.0:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    return math.sqrt((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2)


def _circle_touches_polygon(cx, cy, r, poly):
    """True if circle (cx, cy, r) touches the 2-D screen polygon."""
    from .utils import point_in_polygon
    r2 = r * r
    for (px2, py2) in poly:
        if (px2 - cx) ** 2 + (py2 - cy) ** 2 <= r2:
            return True
    if point_in_polygon((cx, cy), poly):
        return True
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]; bx, by = poly[(i + 1) % n]
        if _point_to_segment_dist(cx, cy, ax, ay, bx, by) <= r:
            return True
    return False
