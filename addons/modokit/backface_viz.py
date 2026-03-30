"""
Modo-Style Backface Visualization.

Automatically enables the Wireframe overlay when entering Edit Mode so edge
topology is always visible, and restores the original setting on exit.
(X-Ray is intentionally left untouched — the user controls it manually.)
"""

import bpy
import bmesh
import numpy as np
import gpu
from gpu_extras.batch import batch_for_shader

from bpy_extras import view3d_utils

from . import state
from .utils import _diag, _get_prefs, perf_time, perf_record

# ── Module-local state (only accessed within this module) ─────────────────────
_saved_viewport_settings: dict = {}   # space_id → dict of saved values
# NOTE: _bfv_previous_mode lives in state.py so uv_overlays.py can read it too.
_back_edge_draw_handle = None         # handle returned by draw_handler_add (POST_VIEW)
_bfv_rebuild_handle   = None          # rebuild-only callback, fires before all draw callbacks

# ── Stipple (checkerboard) shader — POST_VIEW, accepts (pos, normal) ──────────
# The vertex shader offsets vertices along their world-space normal by a
# view-distance-proportional amount (uniform uoffset) before projecting.
# This avoids z-fighting without rebuilding the GPU batch per frame — only
# the scalar uniform needs updating each draw.
_stipple_shader_cache = None

_STIPPLE_VERT_SRC = (
    'void main() {\n'
    '    vec3 displaced = pos + normal * uoffset;\n'
    '    gl_Position = ModelViewProjectionMatrix * vec4(displaced, 1.0);\n'
    '}\n'
)
_STIPPLE_FRAG_SRC = (
    'void main() {\n'
    '    float px = floor(gl_FragCoord.x);\n'
    '    float py = floor(gl_FragCoord.y);\n'
    '    if (mod(px + py, 2.0) < 1.0) discard;\n'
    '    fragColor = ucolor;\n'
    '}\n'
)


def _get_stipple_shader():
    global _stipple_shader_cache
    if _stipple_shader_cache is None:
        try:
            info = gpu.types.GPUShaderCreateInfo()
            info.push_constant('MAT4', 'ModelViewProjectionMatrix')
            info.push_constant('VEC4', 'ucolor')
            info.push_constant('FLOAT', 'uoffset')
            info.vertex_in(0, 'VEC3', 'pos')
            info.vertex_in(1, 'VEC3', 'normal')
            info.vertex_source(_STIPPLE_VERT_SRC)
            info.fragment_out(0, 'VEC4', 'fragColor')
            info.fragment_source(_STIPPLE_FRAG_SRC)
            _stipple_shader_cache = gpu.shader.create_from_info(info)
        except Exception as e:
            print(f'[modokit] backface stipple shader failed: {e}')
    return _stipple_shader_cache
_back_vert_draw_handle = None         # handle returned by draw_handler_add (POST_VIEW)
_back_face_draw_handle = None         # handle returned by draw_handler_add (POST_VIEW)
_back_edge_cache: list = []           # world-space edge-coord pairs for GPU draw
_back_vert_cache: list = []           # world-space positions of selected verts (vert mode)
_back_face_cache: list = []           # world-space triangle coords for selected faces (face mode)

# ── Cached GPU batches — rebuilt only when the CPU caches change, not per frame ─
# Keyed by the id() of the shader so they're invalidated if the shader is recreated.
_gpu_batch_edge: object = None        # GPUBatch for AA edge quads
_gpu_batch_face: object = None        # GPUBatch for face fill triangles (UNIFORM_COLOR)
_gpu_batch_face_stipple: object = None  # GPUBatch for face fill triangles (stipple shader)
_gpu_batch_edge_coords: list = []     # flat coord list cached alongside the edge batch

# ── Selection topology cache — stable during transforms, only rebuilt on dirty ─
# Stores selected element structure (indices only, no positions) per object name.
# During G/R/S transforms positions change but topology/selection don't, so we
# skip the full bm.faces/edges/verts iteration and just recompute positions.
_bec_face_topo: dict = {}   # obj.name → [(fi, [(v0i,v1i),...edges], [(v0i,v1i,v2i),...tris])]
_bec_edge_topo: dict = {}   # obj.name → [(v0i, v1i) per selected edge]
_bec_vert_topo: dict = {}   # obj.name → [vi per selected vert]
_bec_topo_valid: bool = False  # True after a full rebuild; False on dirty/mode-change


def _compute_back_edge_cache(context, topo_only: bool = False):
    """Populate _back_edge_cache and _back_vert_cache from the live BMesh.

    Vertex positions read directly from bm.verts[i].co.  This is safe and
    accurate when called from the POST_VIEW draw callback: at that point all
    mouse/keyboard events for the frame have already been processed and the
    transform operator has committed the current delta to the BMesh, so .co
    matches exactly what the viewport just drew.

    Reading .co from a *timer* was what caused trailing — timers can fire
    between mouse events and see a stale intermediate state.  Moving the read
    to the draw callback fixes the timing without needing the more expensive
    evaluated-depsgraph path.
    """
    global _back_edge_cache, _back_vert_cache, _back_face_cache
    global _gpu_batch_edge, _gpu_batch_face, _gpu_batch_face_stipple, _gpu_batch_edge_coords
    global _bec_face_topo, _bec_edge_topo, _bec_vert_topo, _bec_topo_valid
    _back_edge_cache = []
    _back_vert_cache = []
    _back_face_cache = []
    _gpu_batch_edge = None
    _gpu_batch_face = None
    _gpu_batch_face_stipple = None
    _gpu_batch_edge_coords = []
    if not topo_only:
        _bec_topo_valid = False
    try:
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return
        ts = context.tool_settings
        vert_mode, edge_mode, face_mode = ts.mesh_select_mode
        new_edges = []
        new_verts = []
        new_faces = []
        with perf_time("bec: bmesh traversal"):
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                if not obj.data.is_editmode:
                    continue
                try:
                    mesh = obj.data
                    mx   = obj.matrix_world
                    mx3_mm = mx.to_3x3()  # mathutils Matrix3 — reused for positions (numpy) and per-face normal inline

                    with perf_time("bec: from_edit_mesh"):
                        bm = bmesh.from_edit_mesh(mesh)
                    nv = len(bm.verts)
                    perf_record("bec: mesh verts", nv)

                    with perf_time("bec: co_flat fill"):
                        co_flat = np.empty(nv * 3, dtype='f')
                        idx = 0
                        for v in bm.verts:
                            co = v.co
                            co_flat[idx]   = co.x
                            co_flat[idx+1] = co.y
                            co_flat[idx+2] = co.z
                            idx += 3

                    with perf_time("bec: numpy transform"):
                        cos  = co_flat.reshape(-1, 3)
                        mx3  = np.array(mx3_mm, dtype='f')
                        t    = np.array(mx.translation, dtype='f')
                        wcos = (cos @ mx3.T + t).tolist()

                    if vert_mode:
                        perf_record("bec: mesh elements iterated", nv)
                        if topo_only:
                            with perf_time("bec: topo scan"):
                                for vi in _bec_vert_topo.get(obj.name, []):
                                    w = wcos[vi]
                                    new_verts.append((w[0], w[1], w[2]))
                        else:
                            obj_vert_topo = []
                            with perf_time("bec: select scan"):
                                for v in bm.verts:
                                    if v.select:
                                        obj_vert_topo.append(v.index)
                                        w = wcos[v.index]
                                        new_verts.append((w[0], w[1], w[2]))
                            _bec_vert_topo[obj.name] = obj_vert_topo
                    elif edge_mode:
                        ne = len(bm.edges)
                        perf_record("bec: mesh elements iterated", ne)
                        if topo_only:
                            with perf_time("bec: topo scan"):
                                for (v0i, v1i) in _bec_edge_topo.get(obj.name, []):
                                    v0 = wcos[v0i]
                                    v1 = wcos[v1i]
                                    new_edges.append(((v0[0], v0[1], v0[2]),
                                                      (v1[0], v1[1], v1[2])))
                        else:
                            obj_edge_topo = []
                            with perf_time("bec: select scan"):
                                for edge in bm.edges:
                                    if edge.select:
                                        v0i = edge.verts[0].index
                                        v1i = edge.verts[1].index
                                        obj_edge_topo.append((v0i, v1i))
                                        v0 = wcos[v0i]
                                        v1 = wcos[v1i]
                                        new_edges.append(((v0[0], v0[1], v0[2]),
                                                          (v1[0], v1[1], v1[2])))
                            _bec_edge_topo[obj.name] = obj_edge_topo
                    elif face_mode:
                        nf = len(bm.faces)
                        perf_record("bec: mesh elements iterated", nf)
                        if topo_only:
                            with perf_time("bec: topo scan"):
                                for (fi, edge_pairs, tris) in _bec_face_topo.get(obj.name, []):
                                    face = bm.faces[fi]
                                    nw_v = (mx3_mm @ face.normal).normalized()
                                    nw = (nw_v.x, nw_v.y, nw_v.z)
                                    for (v0i, v1i) in edge_pairs:
                                        v0 = wcos[v0i]
                                        v1 = wcos[v1i]
                                        new_edges.append(((v0[0], v0[1], v0[2]),
                                                          (v1[0], v1[1], v1[2])))
                                    for (v0i, v1i, v2i) in tris:
                                        v0w = wcos[v0i]
                                        v1w = wcos[v1i]
                                        v2w = wcos[v2i]
                                        new_faces.append((
                                            (v0w[0], v0w[1], v0w[2]),
                                            (v1w[0], v1w[1], v1w[2]),
                                            (v2w[0], v2w[1], v2w[2]),
                                            (nw[0],  nw[1],  nw[2]),
                                        ))
                        else:
                            obj_face_topo = []
                            with perf_time("bec: select scan"):
                                for face in bm.faces:
                                    if face.select:
                                        fi = face.index
                                        nw_v = (mx3_mm @ face.normal).normalized()
                                        nw = (nw_v.x, nw_v.y, nw_v.z)
                                        edge_pairs = []
                                        for edge in face.edges:
                                            v0i = edge.verts[0].index
                                            v1i = edge.verts[1].index
                                            edge_pairs.append((v0i, v1i))
                                            v0 = wcos[v0i]
                                            v1 = wcos[v1i]
                                            new_edges.append(((v0[0], v0[1], v0[2]),
                                                              (v1[0], v1[1], v1[2])))
                                        loops = face.loops
                                        fan_root_i = loops[0].vert.index
                                        v0w = wcos[fan_root_i]
                                        tris = []
                                        for i in range(1, len(loops) - 1):
                                            v1i = loops[i].vert.index
                                            v2i = loops[i + 1].vert.index
                                            tris.append((fan_root_i, v1i, v2i))
                                            v1w = wcos[v1i]
                                            v2w = wcos[v2i]
                                            new_faces.append((
                                                (v0w[0], v0w[1], v0w[2]),
                                                (v1w[0], v1w[1], v1w[2]),
                                                (v2w[0], v2w[1], v2w[2]),
                                                (nw[0],  nw[1],  nw[2]),
                                            ))
                                        obj_face_topo.append((fi, edge_pairs, tris))
                            _bec_face_topo[obj.name] = obj_face_topo
                except Exception:
                    continue
        _back_edge_cache = new_edges
        _back_vert_cache = new_verts
        _back_face_cache = new_faces
        if not topo_only:
            _bec_topo_valid = True
    except Exception:
        _back_edge_cache = []
        _back_vert_cache = []
        _back_face_cache = []

    # ── Build GPU batches now, while we're in a safe non-draw context ──────────
    # This avoids re-uploading geometry to the GPU on every single draw frame.
    try:
        with perf_time("bec: batch build"):
            if _back_edge_cache:
                from .uv_overlays import _get_aa_line_3d_shader, _aa_line_quads_3d
                aa3d = _get_aa_line_3d_shader()
                if aa3d is not None:
                    pos0_l, pos1_l, which_l, side_l = _aa_line_quads_3d(_back_edge_cache, 1.0)
                    _gpu_batch_edge = batch_for_shader(
                        aa3d, 'TRIS',
                        {'pos0': pos0_l, 'pos1': pos1_l, 'which': which_l, 'side': side_l},
                    )
                else:
                    coords = []
                    for (p0, p1) in _back_edge_cache:
                        coords.append(p0)
                        coords.append(p1)
                    _gpu_batch_edge_coords = coords
                    fallback = gpu.shader.from_builtin('UNIFORM_COLOR')
                    _gpu_batch_edge = batch_for_shader(fallback, 'LINES', {'pos': coords})
            if _back_face_cache:
                # Build the face batch with per-vertex normals.  The vertex
                # shader applies the view-distance offset (uoffset uniform) on
                # the GPU, so this batch is valid for all camera distances and
                # only needs rebuilding when the mesh changes — not every frame.
                stipple = _get_stipple_shader()
                if stipple is not None:
                    pos_l = []
                    nor_l = []
                    for (p0, p1, p2, nw) in _back_face_cache:
                        pos_l += [p0, p1, p2]
                        nor_l += [nw, nw, nw]
                    _gpu_batch_face = batch_for_shader(
                        stipple, 'TRIS', {'pos': pos_l, 'normal': nor_l}
                    )
    except Exception:
        pass


def _bfv_rebuild_callback() -> None:
    """POST_VIEW callback registered FIRST — rebuilds caches before any drawing.

    All three draw callbacks (face, vert, edge) fire after this one in the same
    frame, so they always draw from a fresh cache.  Registering the rebuild
    here (rather than inside the edge callback) fixes the face/vert "one frame
    behind" bug caused by the face callback firing before the edge callback.
    """
    with perf_time("bfv: rebuild_callback"):
        try:
            context = bpy.context
            if getattr(context, 'mode', None) == 'EDIT_MESH':
                from .uv_overlays import maybe_rebuild_back_edge
                maybe_rebuild_back_edge(context)
        except Exception:
            pass


def _back_edge_draw_callback() -> None:
    with perf_time("draw: back_edge"):
        try:
            _back_edge_draw_callback_inner()
        except Exception:
            pass


def _back_edge_draw_callback_inner() -> None:
    """Render pre-computed back edges.  Zero BMesh access — reads only from
    _back_edge_cache which is populated by the depsgraph handler.

    Accesses state._bfv_previous_mode rather than bpy.context for
    the early-exit mode check to avoid EXCEPTION_ACCESS_VIOLATION when
    Blender mutates the scene at the same time as the draw fires.
    """
    if state._bfv_previous_mode != 'EDIT_MESH':
        return
    if not _back_edge_cache:
        return

    try:
        context = bpy.context
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return
        area = getattr(context, 'area', None)
        if area is None or area.type != 'VIEW_3D':
            return
        region = getattr(context, 'region', None)
    except Exception:
        return

    prefs = _get_prefs(context)
    alpha = prefs.backwire_opacity if prefs is not None else 0.35
    if alpha <= 0.0:
        return

    try:
        with perf_time("draw: back_edge / rna color"):
            theme_3d = context.preferences.themes[0].view_3d
            ts        = context.tool_settings
            sm        = ts.mesh_select_mode
            if sm[0]:    sc = theme_3d.vertex_select
            elif sm[1]:  sc = theme_3d.edge_select
            else:        sc = theme_3d.face_select
            color = (sc.r, sc.g, sc.b, alpha)
    except Exception:
        color = (1.0, 0.6, 0.0, alpha)

    if _gpu_batch_edge is None:
        return

    try:
        vert_mode = sm[0]
        viewport = (float(region.width), float(region.height)) if region else (1920.0, 1080.0)

        from .uv_overlays import _get_aa_line_3d_shader
        aa3d = _get_aa_line_3d_shader()
        _fallback_shader = None if aa3d is not None else gpu.shader.from_builtin('UNIFORM_COLOR')
        _active_shader = aa3d if aa3d is not None else _fallback_shader

        def _draw_cached(color_arg, depth_test):
            gpu.state.depth_test_set(depth_test)
            if aa3d is not None:
                aa3d.bind()
                aa3d.uniform_float('ucolor', color_arg)
                aa3d.uniform_float('uhalf_w', 1.0)
                aa3d.uniform_float('uviewport', viewport)
            else:
                _fallback_shader.bind()
                _fallback_shader.uniform_float('color', color_arg)
            with perf_time("draw: back_edge / gpu draw"):
                _gpu_batch_edge.draw(_active_shader)

        gpu.state.blend_set('ALPHA')
        if vert_mode:
            _draw_cached(color, 'LESS_EQUAL')
        else:
            lum = color[0] * 0.299 + color[1] * 0.587 + color[2] * 0.114
            t = 0.65
            ghost = (color[0]*(1-t)+lum*t, color[1]*(1-t)+lum*t, color[2]*(1-t)+lum*t)
            _draw_cached((ghost[0], ghost[1], ghost[2], 0.5), 'GREATER')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _back_vert_draw_callback() -> None:
    """GPU POST_VIEW callback — draws selected vertex billboards with proper depth
    testing (full opacity when visible, 50% when occluded)."""
    with perf_time("draw: back_vert"):
        try:
            _back_vert_draw_callback_inner()
        except Exception:
            pass


def _back_vert_draw_callback_inner() -> None:
    if state._bfv_previous_mode != 'EDIT_MESH':
        return
    if not _back_vert_cache:
        return
    try:
        context = bpy.context
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return
        area = getattr(context, 'area', None)
        if area is None or area.type != 'VIEW_3D':
            return
        ts = context.tool_settings
        if not ts.mesh_select_mode[0]:   # only in vert mode
            return
        rv3d   = getattr(context, 'region_data', None)
        region = getattr(context, 'region', None)
        if rv3d is None or region is None:
            return
    except Exception:
        return

    prefs = _get_prefs(context)
    alpha = prefs.backwire_opacity if prefs is not None else 0.35
    if alpha <= 0.0:
        return

    try:
        theme_3d = context.preferences.themes[0].view_3d
        sc = theme_3d.vertex_select
        color = (sc.r, sc.g, sc.b, alpha)
    except Exception:
        color = (1.0, 0.6, 0.0, alpha)

    from mathutils import Vector as _Vec

    # Camera right/up vectors in world space (from view matrix rows).
    # Vertex billboards are view-dependent so their geometry must be rebuilt
    # every frame — but the batch_for_shader upload cost is unavoidable here.
    # Vert selections are typically small so the impact is minor.
    vm    = rv3d.view_matrix
    right = _Vec((vm[0][0], vm[0][1], vm[0][2])).normalized()
    up    = _Vec((vm[1][0], vm[1][1], vm[1][2])).normalized()
    cam_pos = vm.inverted_safe().translation.copy()

    win_y    = abs(rv3d.window_matrix[1][1])
    px_scale = (2.0 / (win_y * region.height)) if (win_y > 0 and region.height > 0) else 0.001
    PIXEL_RADIUS = 4.0

    tris = []
    for (wx, wy, wz) in _back_vert_cache:
        p    = _Vec((wx, wy, wz))
        dist = (p - cam_pos).length if rv3d.is_perspective else 1.0
        s    = PIXEL_RADIUS * px_scale * dist
        c0 = tuple(p + (-right - up) * s)
        c1 = tuple(p + ( right - up) * s)
        c2 = tuple(p + ( right + up) * s)
        c3 = tuple(p + (-right + up) * s)
        tris += [c0, c1, c2, c0, c2, c3]

    if not tris:
        return

    try:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch  = batch_for_shader(shader, 'TRIS', {'pos': tris})
        gpu.state.blend_set('ALPHA')
        shader.bind()
        gpu.state.depth_test_set('LESS_EQUAL')
        shader.uniform_float('color', (color[0], color[1], color[2], 1.0))
        batch.draw(shader)
        lum = color[0] * 0.299 + color[1] * 0.587 + color[2] * 0.114
        t = 0.65
        ghost = (color[0]*(1-t)+lum*t, color[1]*(1-t)+lum*t, color[2]*(1-t)+lum*t)
        gpu.state.depth_test_set('GREATER')
        shader.uniform_float('color', (ghost[0], ghost[1], ghost[2], 0.5))
        batch.draw(shader)
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _back_face_draw_callback() -> None:
    """GPU POST_VIEW callback — draws selected face fills with proper depth:
    fully opaque when visible, 50% alpha when occluded."""
    with perf_time("draw: back_face"):
        try:
            _back_face_draw_callback_inner()
        except Exception:
            pass


def _back_face_draw_callback_inner() -> None:
    if state._bfv_previous_mode != 'EDIT_MESH':
        return
    if _gpu_batch_face is None:
        return
    try:
        context = bpy.context
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return
        area = getattr(context, 'area', None)
        if area is None or area.type != 'VIEW_3D':
            return
        ts = context.tool_settings
        if not ts.mesh_select_mode[2]:   # only in face mode
            return
    except Exception:
        return

    prefs = _get_prefs(context)
    alpha = prefs.backwire_opacity if prefs is not None else 0.35
    if alpha <= 0.0:
        return

    try:
        theme_3d = context.preferences.themes[0].view_3d
        sc = theme_3d.face_select
        color = (sc.r, sc.g, sc.b, alpha)
    except Exception:
        color = (1.0, 0.6, 0.0, alpha)

    lum = color[0] * 0.299 + color[1] * 0.587 + color[2] * 0.114
    t = 0.65
    ghost = (color[0]*(1-t)+lum*t, color[1]*(1-t)+lum*t, color[2]*(1-t)+lum*t)

    rv3d = getattr(context, 'region_data', None)
    view_dist = rv3d.view_distance if rv3d is not None else 10.0
    offset_scale = max(0.001, view_dist * 0.0005)

    try:
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('GREATER')
        stipple = _get_stipple_shader()
        if stipple is not None:
            with perf_time("draw: back_face / gpu draw"):
                stipple.bind()
                stipple.uniform_float('uoffset', offset_scale)
                stipple.uniform_float('ucolor', (ghost[0], ghost[1], ghost[2], 0.5))
                _gpu_batch_face.draw(stipple)
        else:
            # Stipple shader unavailable — fall back to a per-frame CPU tris build
            # with UNIFORM_COLOR.  This path should be rare.
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            tris = []
            for (p0, p1, p2, n) in _back_face_cache:
                nx = n[0] * offset_scale; ny = n[1] * offset_scale; nz = n[2] * offset_scale
                tris += [(p0[0]+nx, p0[1]+ny, p0[2]+nz),
                         (p1[0]+nx, p1[1]+ny, p1[2]+nz),
                         (p2[0]+nx, p2[1]+ny, p2[2]+nz)]
            if tris:
                batch = batch_for_shader(shader, 'TRIS', {'pos': tris})
                shader.bind()
                shader.uniform_float('color', (ghost[0], ghost[1], ghost[2], 0.15))
                batch.draw(shader)
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _save_and_apply_bfv(space) -> None:
    """Save original overlay/shading settings for *space* then apply Modo look."""
    sid = id(space)
    if sid not in _saved_viewport_settings:
        _saved_viewport_settings[sid] = {
            'show_wireframes': space.overlay.show_wireframes,
            'show_xray':       space.shading.show_xray,
        }
    space.shading.show_xray = False
    space.overlay.show_wireframes = True


def _restore_bfv(space) -> None:
    """Restore previously saved settings for *space*."""
    sid = id(space)
    saved = _saved_viewport_settings.pop(sid, None)
    if saved is None:
        return
    space.overlay.show_wireframes = saved['show_wireframes']
    if 'show_xray' in saved:
        space.shading.show_xray = saved['show_xray']


def _iter_view3d_spaces(context):
    """Yield all SpaceView3D objects in every open window."""
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        yield space


def _apply_bfv_to_all(context) -> None:
    global _back_edge_draw_handle, _back_vert_draw_handle, _back_face_draw_handle, _bfv_rebuild_handle
    for space in _iter_view3d_spaces(context):
        _save_and_apply_bfv(space)
    # FIRST: register the rebuild callback so the cache is fresh before any draw.
    if _bfv_rebuild_handle is None:
        _bfv_rebuild_handle = bpy.types.SpaceView3D.draw_handler_add(
            _bfv_rebuild_callback, (), 'WINDOW', 'POST_VIEW'
        )
    # Then face, vert, edge — all draw from the already-refreshed cache.
    if _back_face_draw_handle is None:
        _back_face_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _back_face_draw_callback, (), 'WINDOW', 'POST_VIEW'
        )
    if _back_vert_draw_handle is None:
        _back_vert_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _back_vert_draw_callback, (), 'WINDOW', 'POST_VIEW'
        )
    if _back_edge_draw_handle is None:
        _back_edge_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _back_edge_draw_callback, (), 'WINDOW', 'POST_VIEW'
        )


def _restore_bfv_from_all(context) -> None:
    global _back_edge_draw_handle, _back_vert_draw_handle, _back_face_draw_handle, _bfv_rebuild_handle
    for space in _iter_view3d_spaces(context):
        _restore_bfv(space)
    if _bfv_rebuild_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_bfv_rebuild_handle, 'WINDOW')
        _bfv_rebuild_handle = None
    if _back_edge_draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_back_edge_draw_handle, 'WINDOW')
        _back_edge_draw_handle = None
    if _back_vert_draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_back_vert_draw_handle, 'WINDOW')
        _back_vert_draw_handle = None
    if _back_face_draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_back_face_draw_handle, 'WINDOW')
        _back_face_draw_handle = None


@bpy.app.handlers.persistent
def _backface_viz_depsgraph_handler(scene, depsgraph):
    """Fires on every depsgraph update.  Detects Edit Mode entry / exit and
    automatically applies / removes the Modo backface visualization.
    """
    try:
        context = bpy.context
        current_mode = getattr(context, 'mode', None)
        if current_mode == state._bfv_previous_mode:
            return

        state._bfv_previous_mode = current_mode

        if current_mode == 'EDIT_MESH':
            prefs = _get_prefs(context)
            if prefs is None or prefs.enable_backface_viz:
                _apply_bfv_to_all(context)
            # Import lazily to avoid circular imports at module initialisation.
            from .uv_overlays import (
                _start_uv_boundary_overlay, _stop_uv_boundary_overlay,
                _start_uv_flipped_face_viz, _stop_uv_flipped_face_viz,
                _compute_flipped_face_uv_cache, _compute_uv_boundary_cache,
            )
            _start_uv_boundary_overlay()
            _start_uv_flipped_face_viz()

            def _edit_mode_entry_uv_seed():
                try:
                    ctx = bpy.context
                    if getattr(ctx, 'mode', None) == 'EDIT_MESH':
                        _compute_flipped_face_uv_cache(ctx)
                        _compute_uv_boundary_cache(ctx)
                        screen = getattr(ctx, 'screen', None)
                        if screen:
                            for area in screen.areas:
                                if area.type == 'IMAGE_EDITOR':
                                    area.tag_redraw()
                except Exception:
                    pass
                return None
            bpy.app.timers.register(_edit_mode_entry_uv_seed, first_interval=0.15)
        else:
            _restore_bfv_from_all(context)
            from .uv_overlays import (
                _stop_uv_boundary_overlay, _stop_uv_flipped_face_viz,
            )
            _stop_uv_boundary_overlay()
            _stop_uv_flipped_face_viz()
            if state._active_transform_mode is not None:
                try:
                    from .transform_3d import _drop_transform
                    _drop_transform(context)
                except Exception:
                    pass
    except Exception:
        pass


@bpy.app.handlers.persistent
def _uv_cache_clear_load_post_handler(dummy):
    """Clear UV overlay caches after every file / scene load.

    Guarantees a clean slate so stale geometry from the previous file is
    never drawn in the new scene.
    """
    state._flipped_face_uv_cache = []
    state._uv_boundary_cache = {'uv_mode': None, 'points': [], 'segments': []}
    _back_edge_cache.clear()
    state._selection_memory.clear()
    state._uv_selection_memory.clear()

    # Force mode-transition re-evaluation on the next depsgraph event.
    state._bfv_previous_mode = ""

    def _post_load_uv_reinit():
        try:
            ctx = bpy.context
            if getattr(ctx, 'mode', None) == 'EDIT_MESH':
                from .utils import _uv_debug_log
                _uv_debug_log("[UV-LOAD] re-seeding UV overlays after file load")
                state._bfv_previous_mode = 'EDIT_MESH'
                from .uv_overlays import (
                    _start_uv_boundary_overlay, _start_uv_flipped_face_viz,
                    _compute_flipped_face_uv_cache, _compute_uv_boundary_cache,
                )
                _start_uv_boundary_overlay()
                _start_uv_flipped_face_viz()
                _compute_flipped_face_uv_cache(ctx)
                _compute_uv_boundary_cache(ctx)
                screen = getattr(ctx, 'screen', None)
                if screen:
                    for area in screen.areas:
                        if area.type == 'IMAGE_EDITOR':
                            area.tag_redraw()
        except Exception as _e:
            from .utils import _uv_debug_log
            _uv_debug_log(f"[UV-LOAD] _post_load_uv_reinit EXCEPTION: {_e}")
        return None
    bpy.app.timers.register(_post_load_uv_reinit, first_interval=0.3)
