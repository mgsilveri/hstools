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

from . import state
from .utils import _diag, _get_prefs

# ── Module-local state (only accessed within this module) ─────────────────────
_saved_viewport_settings: dict = {}   # space_id → dict of saved values
# NOTE: _bfv_previous_mode lives in state.py so uv_overlays.py can read it too.
_back_edge_draw_handle = None         # handle returned by draw_handler_add
_back_edge_cache: list = []           # world-space edge-coord pairs for GPU draw


def _compute_back_edge_cache(context):
    """Populate _back_edge_cache from the live BMesh.

    Must only be called from a safe Python execution context (e.g. a
    depsgraph handler), never from a GPU draw callback.
    """
    global _back_edge_cache
    _back_edge_cache = []
    _diag("BEC enter")
    try:
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return
        ts = context.tool_settings
        vert_mode, edge_mode, face_mode = ts.mesh_select_mode
        new_cache = []
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
                    for edge in bm.edges:
                        if edge.verts[0].select or edge.verts[1].select:
                            v0 = mx @ edge.verts[0].co
                            v1 = mx @ edge.verts[1].co
                            new_cache.append(((v0.x, v0.y, v0.z), (v1.x, v1.y, v1.z)))
                elif edge_mode:
                    for edge in bm.edges:
                        if edge.select:
                            v0 = mx @ edge.verts[0].co
                            v1 = mx @ edge.verts[1].co
                            new_cache.append(((v0.x, v0.y, v0.z), (v1.x, v1.y, v1.z)))
                elif face_mode:
                    for face in bm.faces:
                        if face.select:
                            for edge in face.edges:
                                v0 = mx @ edge.verts[0].co
                                v1 = mx @ edge.verts[1].co
                                new_cache.append(((v0.x, v0.y, v0.z), (v1.x, v1.y, v1.z)))
            except Exception:
                continue
        _back_edge_cache = new_cache
    except Exception:
        _back_edge_cache = []
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

    coords = []
    for (p0, p1) in _back_edge_cache:
        coords.append(p0)
        coords.append(p1)
    if not coords:
        return

    _diag("DRAW back_edge GPU start n=" + str(len(coords)))
    try:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch  = batch_for_shader(shader, 'LINES', {"pos": coords})
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('ALPHA')
        try:
            shader.bind()
            shader.uniform_float("color", color)
            batch.draw(shader)
            _diag("DRAW back_edge GPU done")
        finally:
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
    global _back_edge_draw_handle
    for space in _iter_view3d_spaces(context):
        _save_and_apply_bfv(space)
    if _back_edge_draw_handle is None:
        _back_edge_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _back_edge_draw_callback, (), 'WINDOW', 'POST_VIEW'
        )


def _restore_bfv_from_all(context) -> None:
    global _back_edge_draw_handle
    for space in _iter_view3d_spaces(context):
        _restore_bfv(space)
    if _back_edge_draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_back_edge_draw_handle, 'WINDOW')
        _back_edge_draw_handle = None


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
