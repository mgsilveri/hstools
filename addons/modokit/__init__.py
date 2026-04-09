"""
ModoKit for Blender (package init)

Replicates Modo's interaction system with tolerance-based selection,
loop/ring selection, shortest path, UV overlays and transform handles.
"""

bl_info = {
    "name": "ModoKit",
    "author": "Based on Modo 15.2v1 Selection System",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "3D Viewport > Edit Mode",
    "description": (
        "Modo-style mouse selection with tolerance, double-click loops, "
        "shortest path, UV overlays and transform handles"
    ),
    "category": "Mesh",
}

import bpy

from . import (
    state,
    utils,
    prefs,
    shortest_path,
    raycast,
    backface_viz,
    preselect,
    ops_edit,
    ops_object,
    panel_menu,
    component_mode,
    transform_3d,
    uv_overlays,
    uv_snap,
    ops_uv,
    uv_selection,
    instance_tagging,
    keymap,
)

from .utils import _uv_debug_log, get_addon_preferences


# ── UV Editor Overlays dropdown injection ─────────────────────────────────────

def _draw_uv_overlays_panel(self, context):
    prefs = get_addon_preferences(context)
    if prefs is None:
        return
    layout = self.layout
    layout.separator()
    layout.label(text="ModoKit")
    col = layout.column_flow(columns=2, align=True)
    col.prop(prefs, "enable_uv_flipped_face_viz",  text="Show Flipped")
    col.prop(prefs, "enable_uv_overlap",            text="Show Overlap")
    col.prop(prefs, "enable_uv_distortion",         text="Show Distortion")
    col.prop(prefs, "enable_uv_coverage_hud",       text="Show UV Coverage")
    layout.separator()
    layout.prop(prefs, "uv_overlay_opacity",           text="Opacity")


# ============================================================================
# All operator / panel / menu classes, in registration order
# ============================================================================

_ALL_CLASSES = (
    transform_3d.ModoKitFalloffProps,
    prefs.ModoSelectionPreferences,
    prefs.MODOKIT_OT_perf_report,
    preselect.VIEW3D_OT_modo_preselect_highlight,
    preselect.IMAGE_OT_modo_preselect_highlight,
    preselect.IMAGE_OT_modo_preselect_lmb_track,
    ops_edit.MESH_OT_modo_select_element_under_mouse,
    ops_edit.MESH_OT_modo_select_shortest_path,
    ops_edit.MESH_OT_modo_lasso_select,
    ops_object.OBJECT_OT_modo_click_select,
    ops_object.OBJECT_OT_modo_lasso_select,
    panel_menu.VIEW3D_PT_modo_selection,
    panel_menu.MESH_MT_modo_selection_context_menu,
    component_mode.VIEW3D_OT_modo_component_mode,
    component_mode.MESH_OT_modo_boundary_select,
    component_mode.MESH_OT_modo_material_mode,
    transform_3d.VIEW3D_OT_modo_snap_highlight,
    transform_3d.VIEW3D_OT_modo_transform,
    transform_3d.VIEW3D_OT_modo_drop_transform,
    transform_3d.VIEW3D_OT_modo_screen_move,
    transform_3d.VIEW3D_OT_modo_scale_gizmo_hover,
    transform_3d.VIEW3D_OT_modo_scale_gizmo_drag,
    transform_3d.VIEW3D_OT_modo_linear_falloff,
    transform_3d.VIEW3D_OT_modo_falloff_auto_size,
    transform_3d.VIEW3D_OT_modo_falloff_reverse,
    transform_3d.VIEW3D_OT_modo_falloff_handle_hover,
    transform_3d.VIEW3D_OT_modo_falloff_handle_drag,
    uv_snap.IMAGE_OT_modo_uv_snap_highlight,
    ops_uv.IMAGE_OT_modo_uv_transform,
    ops_uv.IMAGE_OT_modo_uv_component_mode,
    ops_uv.IMAGE_OT_modo_uv_drop_transform,
    ops_uv.IMAGE_OT_modo_uv_handle_reposition,
    ops_uv.IMAGE_OT_modo_uv_selection_guard,
    ops_uv.IMAGE_OT_modo_uv_rip,
    uv_selection.IMAGE_OT_modo_uv_stitch,
    uv_selection.IMAGE_OT_modo_uv_double_click_select,
    uv_selection.IMAGE_OT_modo_uv_shortest_path,
    uv_selection.IMAGE_OT_modo_uv_click_select,
    uv_selection.IMAGE_OT_modo_uv_paint_selection,
    uv_selection.IMAGE_OT_modo_uv_lasso_select,
)


# ============================================================================
# register / unregister
# ============================================================================

def register():
    for cls in _ALL_CLASSES:
        bpy.utils.register_class(cls)

    # Falloff scene property
    bpy.types.Scene.modokit_falloff = bpy.props.PointerProperty(
        type=transform_3d.ModoKitFalloffProps)

    # Pre-selection highlight handler
    if preselect._preselect_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(
            preselect._preselect_depsgraph_handler)
    # Start draw handlers immediately (keymap MOUSEMOVE drives hit updates)
    preselect._start_preselect()

    # Backface visualisation handler (always registered; checks prefs at runtime)
    if backface_viz._backface_viz_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(
            backface_viz._backface_viz_depsgraph_handler)

    # Instance tagging handler
    if instance_tagging._instance_tag_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(
            instance_tagging._instance_tag_depsgraph_handler)

    # UV seam-partner redraw handler
    if uv_overlays._uv_seam_redraw_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(
            uv_overlays._uv_seam_redraw_depsgraph_handler)

    # Seed UV boundary / flipped-face caches if already in Edit Mode
    def _initial_uv_cache_populate():
        try:
            ctx = bpy.context
            mode = getattr(ctx, 'mode', None)
            if mode in ('EDIT_MESH', 'OBJECT'):
                p = ctx.preferences.addons.get('modokit')
                if p is None or p.preferences.enable_preselect_highlight:
                    pass  # draw handles already started in register()
            if mode == 'EDIT_MESH':
                _uv_debug_log("[UV-INIT] seeding UV boundary cache on addon load")
                uv_overlays._start_uv_boundary_overlay()
                uv_overlays._start_uv_flipped_face_viz()
                uv_overlays._start_uv_distortion_viz()   # must register before overlap
                uv_overlays._start_uv_overlap_viz()      # composites on top of distortion
                uv_overlays._start_uv_coverage_hud()
                uv_overlays._start_uv_active_face_viz()
                uv_overlays._compute_flipped_face_uv_cache(ctx)
                uv_overlays._compute_uv_boundary_cache(ctx)
                screen = getattr(ctx, 'screen', None)
                if screen:
                    for area in screen.areas:
                        if area.type == 'IMAGE_EDITOR':
                            area.tag_redraw()
        except Exception as _ie:
            _uv_debug_log(f"[UV-INIT] EXCEPTION: {_ie}")
        return None
    bpy.app.timers.register(_initial_uv_cache_populate, first_interval=0.2)

    # UV gizmo: resync handle after undo / redo
    if uv_overlays._uv_undo_redo_handler not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(uv_overlays._uv_undo_redo_handler)
    if uv_overlays._uv_undo_redo_handler not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(uv_overlays._uv_undo_redo_handler)

    # UV cache clear on file load
    if backface_viz._uv_cache_clear_load_post_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(backface_viz._uv_cache_clear_load_post_handler)

    # Keymap load_post handler
    if keymap._keymap_load_post_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(keymap._keymap_load_post_handler)

    # Defer keymap registration
    keymap._schedule_deferred_keymap_setup()

    # Sync perf timing flag from saved prefs — the update callback only fires
    # on user interaction, not on addon load, so we must read it explicitly here.
    def _sync_perf_flag():
        try:
            import modokit.utils as _u
            p = bpy.context.preferences.addons.get('modokit')
            if p is not None:
                _u._perf_enabled = bool(p.preferences.debug_perf)
                _u._diag_enabled = bool(p.preferences.debug_crash_trace)
        except Exception:
            pass
        return None
    bpy.app.timers.register(_sync_perf_flag, first_interval=0.0)

    # Start UV tool guardian
    if not state._uv_tool_guardian_running:
        state._uv_tool_guardian_running = True
        bpy.app.timers.register(keymap._uv_tool_guardian,
                                first_interval=state._UV_TOOL_GUARDIAN_INTERVAL)

    # UV Editor Overlays dropdown
    bpy.types.IMAGE_PT_overlay.append(_draw_uv_overlays_panel)

    # Falloff — View menu Show Falloff toggle
    bpy.types.VIEW3D_MT_view.append(panel_menu._draw_falloff_view_menu)

    # Patch VIEW3D_MT_editor_menus.draw_collapsible for Material Mode button
    if state._orig_editor_menus_draw_collapsible is None:
        state._orig_editor_menus_draw_collapsible = (
            bpy.types.VIEW3D_MT_editor_menus.draw_collapsible
        )
        bpy.types.VIEW3D_MT_editor_menus.draw_collapsible = classmethod(
            component_mode._patched_editor_menus_draw_collapsible
        )



def unregister():
    keymap._backup_all_addon_prefs()

    state._deferred_timer_registered = False
    try:
        bpy.app.timers.unregister(keymap._deferred_keymap_setup)
    except Exception:
        pass

    state._uv_tool_guardian_running = False
    try:
        bpy.app.timers.unregister(keymap._uv_tool_guardian)
    except Exception:
        pass

    keymap.unregister_keymaps()

    if backface_viz._uv_cache_clear_load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(backface_viz._uv_cache_clear_load_post_handler)
    if keymap._keymap_load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(keymap._keymap_load_post_handler)

    if preselect._preselect_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(
            preselect._preselect_depsgraph_handler)
    preselect._stop_preselect()

    if backface_viz._backface_viz_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(
            backface_viz._backface_viz_depsgraph_handler)
    backface_viz._restore_bfv_from_all(bpy.context)

    uv_overlays._stop_uv_boundary_overlay()
    uv_overlays._stop_uv_flipped_face_viz()
    uv_overlays._stop_uv_overlap_viz()
    uv_overlays._stop_uv_distortion_viz()
    uv_overlays._stop_uv_coverage_hud()
    uv_overlays._stop_uv_active_face_viz()

    if uv_overlays._uv_seam_redraw_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(
            uv_overlays._uv_seam_redraw_depsgraph_handler)

    try:
        from .uv_snap import _uv_drop_transform
        _uv_drop_transform(bpy.context)
    except Exception:
        pass

    state._uv_snap_highlight = None
    if state._uv_snap_highlight_draw_handle is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(
                state._uv_snap_highlight_draw_handle, 'WINDOW')
        except Exception:
            pass
        state._uv_snap_highlight_draw_handle = None
    uv_overlays._stop_uv_gizmo()

    if uv_overlays._uv_undo_redo_handler in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(uv_overlays._uv_undo_redo_handler)
    if uv_overlays._uv_undo_redo_handler in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.remove(uv_overlays._uv_undo_redo_handler)

    if instance_tagging._instance_tag_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(
            instance_tagging._instance_tag_depsgraph_handler)
    try:
        col = bpy.data.collections.get(state._INST_COLLECTION)
        if col:
            for obj in list(col.objects):
                instance_tagging._restore_from_instances_col(obj)
                if obj.name.startswith(state._INST_PREFIX):
                    obj.name = obj.name[len(state._INST_PREFIX):]
            instance_tagging._remove_instances_collection_if_empty()
        for obj in bpy.context.scene.objects:
            if obj.name.startswith(state._INST_PREFIX):
                obj.name = obj.name[len(state._INST_PREFIX):]
    except Exception:
        pass

    bpy.types.IMAGE_PT_overlay.remove(_draw_uv_overlays_panel)
    bpy.types.VIEW3D_MT_view.remove(panel_menu._draw_falloff_view_menu)

    # Stop falloff draw handlers if active
    transform_3d._stop_falloff_handles()
    transform_3d._stop_falloff_mesh_overlay()

    # Remove scene property
    try:
        del bpy.types.Scene.modokit_falloff
    except Exception:
        pass

    for cls in reversed(_ALL_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

    # Restore original VIEW3D_MT_editor_menus.draw_collapsible
    if state._orig_editor_menus_draw_collapsible is not None:
        bpy.types.VIEW3D_MT_editor_menus.draw_collapsible = (
            state._orig_editor_menus_draw_collapsible
        )
        state._orig_editor_menus_draw_collapsible = None

    # Close UV debug log file if open
    import time as _time
    try:
        if utils._uv_debug_log_file is not None and not utils._uv_debug_log_file.closed:
            utils._uv_debug_log_file.write(
                f"=== log closed {_time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            utils._uv_debug_log_file.close()
    except Exception:
        pass
    utils._uv_debug_log_file = None
