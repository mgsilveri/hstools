"""
Modo-Style Backface Visualization.

Automatically enables the Wireframe overlay when entering Edit Mode so edge
topology is always visible, and restores the original setting on exit.
(X-Ray is intentionally left untouched — the user controls it manually.)
"""

import bpy
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader

from bpy_extras import view3d_utils

from . import state
from .utils import _diag, _get_prefs

# ── Module-local state (only accessed within this module) ─────────────────────
_saved_viewport_settings: dict = {}   # space_id → dict of saved values
# NOTE: _bfv_previous_mode lives in state.py so uv_overlays.py can read it too.
_back_edge_draw_handle = None         # handle returned by draw_handler_add (POST_VIEW)

# ── Stipple (checkerboard) shader — POST_VIEW, vec3 pos ───────────────────────
_stipple_shader_cache = None

_STIPPLE_VERT_SRC = (
    'void main() {\n'
    '    gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0);\n'
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
            info.vertex_in(0, 'VEC3', 'pos')
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


def _compute_back_edge_cache(context):
    """Populate _back_edge_cache and _back_vert_cache from the live BMesh.

    Must only be called from a safe Python execution context (e.g. a
    depsgraph handler), never from a GPU draw callback.
    """
    global _back_edge_cache, _back_vert_cache, _back_face_cache
    _back_edge_cache = []
    _back_vert_cache = []
    _back_face_cache = []
    _diag("BEC enter")
    try:
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return
        ts = context.tool_settings
        vert_mode, edge_mode, face_mode = ts.mesh_select_mode
        new_edges = []
        new_verts = []
        new_faces = []
        for obj in context.objects_in_mode_unique_data:
            if obj.type != 'MESH':
                continue
            if not obj.data.is_editmode:
                continue
            try:
                _diag("BEC from_edit_mesh " + obj.name)
                bm = bmesh.from_edit_mesh(obj.data)
                mx = obj.matrix_world
                if vert_mode:
                    for vert in bm.verts:
                        if vert.select:
                            vw = mx @ vert.co
                            new_verts.append((vw.x, vw.y, vw.z))
                elif edge_mode:
                    for edge in bm.edges:
                        if edge.select:
                            v0 = mx @ edge.verts[0].co
                            v1 = mx @ edge.verts[1].co
                            new_edges.append(((v0.x, v0.y, v0.z), (v1.x, v1.y, v1.z)))
                elif face_mode:
                    for face in bm.faces:
                        if face.select:
                            for edge in face.edges:
                                v0 = mx @ edge.verts[0].co
                                v1 = mx @ edge.verts[1].co
                                new_edges.append(((v0.x, v0.y, v0.z), (v1.x, v1.y, v1.z)))
                            # Fan-triangulate for fill drawing.
                            # Store unbiased positions + face normal so the draw
                            # callback can apply a view-distance-proportional offset
                            # at draw time (fixed world offsets become negligible in
                            # the depth buffer at large camera distances).
                            n_world = (mx.to_3x3() @ face.normal).normalized()
                            nw = (n_world.x, n_world.y, n_world.z)
                            loops = face.loops
                            v0w = mx @ loops[0].vert.co
                            for i in range(1, len(loops) - 1):
                                v1w = mx @ loops[i].vert.co
                                v2w = mx @ loops[i + 1].vert.co
                                new_faces.append((
                                    (v0w.x, v0w.y, v0w.z),
                                    (v1w.x, v1w.y, v1w.z),
                                    (v2w.x, v2w.y, v2w.z),
                                    nw,
                                ))
            except Exception:
                continue
        _back_edge_cache = new_edges
        _back_vert_cache = new_verts
        _back_face_cache = new_faces
    except Exception:
        _back_edge_cache = []
        _back_vert_cache = []
        _back_face_cache = []
    _diag("BEC done n=" + str(len(_back_edge_cache)))


def _back_edge_draw_callback() -> None:
    """GPU draw callback — draws only SELECTED edges without depth testing.

    Unselected back geometry is not drawn (invisible, like default Blender).
    """
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
    _diag("DRAW back_edge enter")
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
        theme_3d = context.preferences.themes[0].view_3d
        ts        = context.tool_settings
        sm        = ts.mesh_select_mode
        if sm[0]:    sc = theme_3d.vertex_select   # vert mode: edges on selected verts
        elif sm[1]:  sc = theme_3d.edge_select      # edge mode: selected edges
        else:        sc = theme_3d.face_select       # face mode: edges of selected faces
        color = (sc.r, sc.g, sc.b, alpha)
    except Exception:
        color = (1.0, 0.6, 0.0, alpha)
    coords = []
    for (p0, p1) in _back_edge_cache:
        coords.append(p0)
        coords.append(p1)
    if not coords:
        return

    _diag("DRAW back_edge GPU start n=" + str(len(coords)))
    try:
        vert_mode = sm[0]
        viewport = (float(region.width), float(region.height)) if region else (1920.0, 1080.0)

        from .uv_overlays import _get_aa_line_3d_shader, _aa_line_quads_3d
        aa3d = _get_aa_line_3d_shader()
        hw = 1.0

        def _draw_aa3d(color_arg, depth_test):
            if aa3d is not None:
                pos0_l, pos1_l, which_l, side_l = _aa_line_quads_3d(_back_edge_cache, hw)
                batch = batch_for_shader(aa3d, 'TRIS',
                                         {'pos0': pos0_l, 'pos1': pos1_l,
                                          'which': which_l, 'side': side_l})
                gpu.state.depth_test_set(depth_test)
                aa3d.bind()
                aa3d.uniform_float('ucolor', color_arg)
                aa3d.uniform_float('uhalf_w', hw)
                aa3d.uniform_float('uviewport', viewport)
                batch.draw(aa3d)
            else:
                shader = gpu.shader.from_builtin('UNIFORM_COLOR')
                batch  = batch_for_shader(shader, 'LINES', {"pos": coords})
                gpu.state.depth_test_set(depth_test)
                shader.bind()
                shader.uniform_float("color", color_arg)
                batch.draw(shader)

        gpu.state.blend_set('ALPHA')
        if vert_mode:
            _draw_aa3d(color, 'LESS_EQUAL')
        else:
            lum = color[0] * 0.299 + color[1] * 0.587 + color[2] * 0.114
            t = 0.65
            ghost = (color[0]*(1-t)+lum*t, color[1]*(1-t)+lum*t, color[2]*(1-t)+lum*t)
            _draw_aa3d((ghost[0], ghost[1], ghost[2], 0.5), 'GREATER')
        _diag("DRAW back_edge GPU done")
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _back_vert_draw_callback() -> None:
    """GPU POST_VIEW callback — draws selected vertex billboards with proper depth
    testing (full opacity when visible, 50% when occluded)."""
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

    # Camera right/up vectors in world space (from view matrix rows)
    vm    = rv3d.view_matrix
    right = _Vec((vm[0][0], vm[0][1], vm[0][2])).normalized()
    up    = _Vec((vm[1][0], vm[1][1], vm[1][2])).normalized()
    cam_pos = vm.inverted_safe().translation.copy()

    # Scale factor: maps pixel radius to world size at a given depth
    # POST_VIEW uses perspective_matrix for projection, so:
    # world_size = px * 2 / (window_matrix[1][1] * region.height) * depth
    win_y  = abs(rv3d.window_matrix[1][1])
    px_scale = (2.0 / (win_y * region.height)) if (win_y > 0 and region.height > 0) else 0.001
    PIXEL_RADIUS = 4.0

    tris = []
    for (wx, wy, wz) in _back_vert_cache:
        p    = _Vec((wx, wy, wz))
        dist = (p - cam_pos).length if rv3d.is_perspective else 1.0
        s    = PIXEL_RADIUS * px_scale * dist
        # Build camera-facing (billboard) quad as 2 triangles
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
        # Pass 1: fully opaque where not occluded
        gpu.state.depth_test_set('LESS_EQUAL')
        shader.uniform_float('color', (color[0], color[1], color[2], 1.0))
        batch.draw(shader)
        # Pass 2: ghostly (desaturated + dimmed) where occluded
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
    try:
        _back_face_draw_callback_inner()
    except Exception:
        pass


def _back_face_draw_callback_inner() -> None:
    if state._bfv_previous_mode != 'EDIT_MESH':
        return
    if not _back_face_cache:
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

    # Apply a view-distance-proportional normal offset so the fill geometry
    # sits reliably in front of the mesh surface in the depth buffer at any
    # camera distance.  A fixed world-space nudge becomes negligible in NDC
    # precision when the camera is far away, causing GREATER to spuriously
    # pass on frontface pixels (z-fighting). Scaling with view_distance keeps
    # the separation proportional to the depth-buffer granularity at that range.
    rv3d = getattr(context, 'region_data', None)
    view_dist = rv3d.view_distance if rv3d is not None else 10.0
    offset_scale = max(0.001, view_dist * 0.0005)

    tris = []
    for entry in _back_face_cache:
        p0, p1, p2, n = entry
        nx, ny, nz = n[0] * offset_scale, n[1] * offset_scale, n[2] * offset_scale
        tris += [
            (p0[0] + nx, p0[1] + ny, p0[2] + nz),
            (p1[0] + nx, p1[1] + ny, p1[2] + nz),
            (p2[0] + nx, p2[1] + ny, p2[2] + nz),
        ]

    if not tris:
        return

    try:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch  = batch_for_shader(shader, 'TRIS', {'pos': tris})
        gpu.state.blend_set('ALPHA')
        shader.bind()
        # Only draw where occluded — stipple (checkerboard discard) gives ~50%
        # pixel coverage at full per-pixel alpha, avoiding the washed-out look
        # of a uniform semi-transparent fill.
        lum = color[0] * 0.299 + color[1] * 0.587 + color[2] * 0.114
        t = 0.65
        ghost = (color[0]*(1-t)+lum*t, color[1]*(1-t)+lum*t, color[2]*(1-t)+lum*t)
        stipple = _get_stipple_shader()
        if stipple is not None:
            s_batch = batch_for_shader(stipple, 'TRIS', {'pos': tris})
            stipple.bind()
            gpu.state.depth_test_set('GREATER')
            stipple.uniform_float('ucolor', (ghost[0], ghost[1], ghost[2], 0.5))
            s_batch.draw(stipple)
        else:
            # Fallback: plain GREATER pass
            gpu.state.depth_test_set('GREATER')
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
    global _back_edge_draw_handle, _back_vert_draw_handle, _back_face_draw_handle
    for space in _iter_view3d_spaces(context):
        _save_and_apply_bfv(space)
    # Register face fill FIRST so it draws before edges — this ensures edges
    # always composite on top of the stipple face pattern with no fighting.
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
    global _back_edge_draw_handle, _back_vert_draw_handle, _back_face_draw_handle
    for space in _iter_view3d_spaces(context):
        _restore_bfv(space)
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
                _refresh_uv_caches_timer,
            )
            _start_uv_boundary_overlay()
            _start_uv_flipped_face_viz()

            def _edit_mode_entry_uv_seed():
                try:
                    ctx = bpy.context
                    if getattr(ctx, 'mode', None) == 'EDIT_MESH':
                        _compute_flipped_face_uv_cache(ctx)
                        _compute_uv_boundary_cache(ctx)
                        if not bpy.app.timers.is_registered(_refresh_uv_caches_timer):
                            bpy.app.timers.register(_refresh_uv_caches_timer,
                                                    first_interval=0.0)
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
                    _refresh_uv_caches_timer,
                )
                _start_uv_boundary_overlay()
                _start_uv_flipped_face_viz()
                _compute_flipped_face_uv_cache(ctx)
                _compute_uv_boundary_cache(ctx)
                if not bpy.app.timers.is_registered(_refresh_uv_caches_timer):
                    bpy.app.timers.register(_refresh_uv_caches_timer,
                                            first_interval=0.0)
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
