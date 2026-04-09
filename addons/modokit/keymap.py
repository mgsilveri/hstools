"""keymap.py — Keymap registration and conflict management.

Registers all addon keymaps directly into keyconfigs.user (the only keyconfig
that dispatches input in Blender 5.0).  Handles startup-adoption of previously
saved keymaps, deferred setup via a repeating timer, and conflict scanning.
"""

import bpy
from . import state
from .utils import get_addon_preferences, _uv_debug_log


# ============================================================================
# Conflict management
# ============================================================================

def _disable_conflicting_kmis():
    """Disable every active user-keyconfig item that shares a key combo with
    one of our registered bindings — except our own operators and nav ops."""
    state._disabled_kmi_ids.clear()
    state._saved_rmb_menus.clear()

    our_combos = set()
    for km, kmi in state.addon_keymaps:
        our_combos.add((km.name, kmi.type, kmi.shift, kmi.ctrl, kmi.alt))

    if not our_combos:
        return

    wm = bpy.context.window_manager
    kc_user = wm.keyconfigs.user
    if kc_user is None:
        return

    for km in kc_user.keymaps:
        for kmi in km.keymap_items:
            if not kmi.active:
                continue
            if kmi.idname in state._OUR_IDNAMES:
                continue
            combo = (km.name, kmi.type, kmi.shift, kmi.ctrl, kmi.alt)
            if combo not in our_combos:
                continue
            if kmi.idname in state._NAV_IDNAMES:
                continue
            if (km.name in ('UV Editor', 'Image')
                    and kmi.idname in {'uv.select_all'}):
                continue
            if (kmi.idname == 'wm.call_menu'
                    and kmi.type == 'RIGHTMOUSE'
                    and not kmi.shift and not kmi.ctrl
                    and km.name not in state._saved_rmb_menus):
                try:
                    state._saved_rmb_menus[km.name] = kmi.properties.name
                except Exception:
                    pass

            identity = (km.name, kmi.idname, kmi.type, kmi.value,
                        kmi.shift, kmi.ctrl, kmi.alt)
            kmi.active = False
            state._disabled_kmi_ids.append(identity)


def _restore_conflicting_kmis():
    """Re-enable items that were disabled for our addon's keymaps."""
    wm = bpy.context.window_manager
    kc_user = wm.keyconfigs.user if wm else None
    state._disabled_kmi_ids.clear()

    if kc_user is None:
        return

    if not state._registered_kmi_ids:
        _RESTORE_KMS = {'3D View', 'Object Mode', 'Mesh', 'UV Editor', 'Image',
                        'Object Non-modal'}
        for km in kc_user.keymaps:
            if km.name not in _RESTORE_KMS:
                continue
            for kmi in km.keymap_items:
                if kmi.active:
                    continue
                if kmi.idname in state._OUR_IDNAMES:
                    continue
                try:
                    kmi.active = True
                except (Exception, SystemError, ReferenceError):
                    pass
        return

    our_combos = set()
    for entry in state._registered_kmi_ids:
        km_name, idname, ktype, value, shift, ctrl, alt = entry
        our_combos.add((km_name, ktype, shift, ctrl, alt))

    for km in kc_user.keymaps:
        for kmi in km.keymap_items:
            if kmi.active:
                continue
            if kmi.idname in state._OUR_IDNAMES:
                continue
            if (km.name in ('UV Editor', 'Image')
                    and kmi.idname in {'uv.select_box', 'uv.select_lasso'}
                    and kmi.type == 'LEFTMOUSE'):
                continue
            combo = (km.name, kmi.type, kmi.shift, kmi.ctrl, kmi.alt)
            if combo in our_combos:
                try:
                    kmi.active = True
                except (Exception, SystemError, ReferenceError):
                    pass


# ============================================================================
# Registration
# ============================================================================

def register_keymaps():
    wm = bpy.context.window_manager
    prefs = get_addon_preferences(bpy.context)

    # Purge stale entries for OUR operators saved from a previous session.
    kc_purge = wm.keyconfigs.user
    if kc_purge:
        for _km in kc_purge.keymaps:
            _stale = [_kmi for _kmi in _km.keymap_items
                      if _kmi.idname in state._OUR_IDNAMES]
            for _kmi in _stale:
                try:
                    _km.keymap_items.remove(_kmi)
                except (RuntimeError, ReferenceError):
                    pass

    kc = wm.keyconfigs.user
    if kc is None:
        kc = wm.keyconfigs.addon

    if kc:
        km = kc.keymaps.new(name='Mesh', space_type='EMPTY')

        if prefs.enable_lasso_selection:
            for btn_type, shift_val, ctrl_val, sel_mode, use_mmb in (
                ('RIGHTMOUSE', False, False, 'set',    False),
                ('RIGHTMOUSE', True,  False, 'add',    False),
                ('RIGHTMOUSE', False, True,  'remove', False),
                ('MIDDLEMOUSE', False, False, 'set',   True),
                ('MIDDLEMOUSE', True,  False, 'add',   True),
                ('MIDDLEMOUSE', False, True,  'remove', True),
            ):
                kmi = km.keymap_items.new(
                    'mesh.modo_lasso_select',
                    type=btn_type,
                    value='PRESS',
                    shift=shift_val,
                    ctrl=ctrl_val,
                    head=True,
                )
                kmi.properties.mode = sel_mode
                kmi.properties.use_middle_click = use_mmb
                state.addon_keymaps.append((km, kmi))

        km_obj = kc.keymaps.new(name='Object Mode', space_type='EMPTY')

        if prefs.enable_object_mode_selection:
            for shift_val, ctrl_val, sel_mode in (
                (False, False, 'set'),
                (True,  False, 'add'),
                (False, True,  'remove'),
            ):
                kmi = km_obj.keymap_items.new(
                    'object.modo_click_select',
                    type='LEFTMOUSE',
                    value='PRESS',
                    shift=shift_val,
                    ctrl=ctrl_val,
                    head=True,
                )
                kmi.properties.mode = sel_mode
                state.addon_keymaps.append((km_obj, kmi))

        if prefs.enable_lasso_selection:
            for btn_type, shift_val, ctrl_val, sel_mode in (
                ('RIGHTMOUSE',  False, False, 'set'),
                ('RIGHTMOUSE',  True,  False, 'add'),
                ('RIGHTMOUSE',  False, True,  'remove'),
                ('MIDDLEMOUSE', False, False, 'set'),
                ('MIDDLEMOUSE', True,  False, 'add'),
                ('MIDDLEMOUSE', False, True,  'remove'),
            ):
                kmi = km_obj.keymap_items.new(
                    'object.modo_lasso_select',
                    type=btn_type,
                    value='PRESS',
                    shift=shift_val,
                    ctrl=ctrl_val,
                    head=True,
                )
                kmi.properties.mode = sel_mode
                state.addon_keymaps.append((km_obj, kmi))

        km_nav = kc.keymaps.new(name='3D View', space_type='VIEW_3D')

        # Pre-selection highlight — non-modal, fires on every mouse move
        if getattr(prefs, 'enable_preselect_highlight', True):
            kmi = km_nav.keymap_items.new(
                'view3d.modo_preselect_highlight',
                type='MOUSEMOVE',
                value='ANY',
                any=True,
                head=False,
            )
            state.addon_keymaps.append((km_nav, kmi))

            # Same for UV editor — register in both UV Editor and Image keymaps,
            # matching the same pattern every other UV operator uses here.
            for _uv_name, _uv_stype in (('UV Editor', 'EMPTY'), ('Image', 'IMAGE_EDITOR')):
                try:
                    km_img = kc.keymaps.new(name=_uv_name, space_type=_uv_stype)
                    kmi_img = km_img.keymap_items.new(
                        'image.modo_preselect_highlight',
                        type='MOUSEMOVE',
                        value='ANY',
                        any=True,
                        head=False,
                    )
                    state.addon_keymaps.append((km_img, kmi_img))
                except Exception as e:
                    print(f"[preselect] keymap '{_uv_name}' registration failed: {e}")

        if prefs.enable_mouse_selection:
            for shift_val, ctrl_val, sel_mode, ev_value in (
                (False, False, 'set',    'PRESS'),
                (False, False, 'set',    'DOUBLE_CLICK'),
                (True,  False, 'add',    'PRESS'),
                (True,  False, 'add',    'DOUBLE_CLICK'),
                (False, True,  'remove', 'PRESS'),
                (False, True,  'remove', 'DOUBLE_CLICK'),
            ):
                kmi = km_nav.keymap_items.new(
                    'mesh.modo_select_element_under_mouse',
                    type='LEFTMOUSE',
                    value=ev_value,
                    shift=shift_val,
                    ctrl=ctrl_val,
                    head=True,
                )
                kmi.properties.mode = sel_mode
                state.addon_keymaps.append((km_nav, kmi))

        _MOUSE_BUTTONS = {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE',
                          'BUTTON4MOUSE', 'BUTTON5MOUSE'}
        if (prefs.enable_mouse_selection
                and prefs.shortest_path_key not in _MOUSE_BUTTONS):
            kmi = km_nav.keymap_items.new(
                'mesh.modo_select_shortest_path',
                type=prefs.shortest_path_key,
                value='PRESS',
                shift=prefs.shortest_path_shift,
                ctrl=prefs.shortest_path_ctrl,
                alt=prefs.shortest_path_alt,
                head=True,
            )
            state.addon_keymaps.append((km_nav, kmi))

        # UV Editor keymaps
        _uv_km_targets = []
        for _km_name, _km_stype in (('UV Editor', 'EMPTY'),
                                     ('Image', 'IMAGE_EDITOR')):
            try:
                _uv_km_targets.append(
                    kc.keymaps.new(name=_km_name, space_type=_km_stype))
            except Exception:
                pass

        # LMB press/release tracker — suppresses preselect highlight during drag-select
        if getattr(prefs, 'enable_preselect_highlight', True):
            for km_uv in _uv_km_targets:
                for _lmb_val in ('PRESS', 'RELEASE'):
                    kmi = km_uv.keymap_items.new(
                        'image.modo_preselect_lmb_track',
                        type='LEFTMOUSE',
                        value=_lmb_val,
                        any=True,
                        head=True,
                    )
                    state.addon_keymaps.append((km_uv, kmi))

        # Move and Sew — key configurable via prefs (default Shift+S)
        for km_uv in _uv_km_targets:
            kmi = km_uv.keymap_items.new(
                'image.modo_uv_stitch',
                type=prefs.move_and_sew_key,
                value='PRESS',
                shift=prefs.move_and_sew_shift,
                ctrl=prefs.move_and_sew_ctrl,
                alt=prefs.move_and_sew_alt,
                head=True,
            )
            state.addon_keymaps.append((km_uv, kmi))

        # UV Rip — key configurable via prefs (default V)
        for km_uv in _uv_km_targets:
            kmi = km_uv.keymap_items.new(
                'image.modo_uv_rip',
                type=prefs.uv_rip_key,
                value='PRESS',
                shift=prefs.uv_rip_shift,
                ctrl=prefs.uv_rip_ctrl,
                alt=prefs.uv_rip_alt,
                head=True,
            )
            state.addon_keymaps.append((km_uv, kmi))

        # Explicitly disable mesh.select_mode / uv.select_mode on 1/2/3
        _UV_CONFLICT_IDNAMES = {'mesh.select_mode', 'uv.select_mode'}
        _UV_MODE_KEYS = {'ONE', 'TWO', 'THREE'}
        kc_user = wm.keyconfigs.user
        if kc_user:
            for _km in kc_user.keymaps:
                if _km.name not in ('UV Editor', 'Image'):
                    continue
                for _kmi in _km.keymap_items:
                    if (_kmi.type in _UV_MODE_KEYS
                            and not _kmi.shift and not _kmi.ctrl and not _kmi.alt
                            and _kmi.idname in _UV_CONFLICT_IDNAMES
                            and _kmi.active):
                        _kmi.active = False

        # Disable uv.select_box / uv.select_lasso on LMB
        _UV_LMB_BOX_NAMES = {'uv.select_box', 'uv.select_lasso'}
        kc_user_ref = wm.keyconfigs.user
        if kc_user_ref:
            for _km in kc_user_ref.keymaps:
                if _km.name not in ('UV Editor', 'Image'):
                    continue
                for _kmi in _km.keymap_items:
                    if (_kmi.type == 'LEFTMOUSE'
                            and _kmi.idname in _UV_LMB_BOX_NAMES
                            and _kmi.active):
                        if prefs.debug_selection:
                            print(f'[Modo UV] Disabling {_kmi.idname} '
                                  f'value={_kmi.value} in {_km.name}')
                        _kmi.active = False

        # LMB paint selection (set/add/remove)
        for km_uv in _uv_km_targets:
            for ps_shift, ps_ctrl, ps_mode in (
                (False, False, 'set'),
                (True,  False, 'add'),
                (False, True,  'remove'),
            ):
                kmi = km_uv.keymap_items.new(
                    'image.modo_uv_paint_selection',
                    type='LEFTMOUSE',
                    value='PRESS',
                    shift=ps_shift,
                    ctrl=ps_ctrl,
                    head=True,
                )
                kmi.properties.mode = ps_mode
                state.addon_keymaps.append((km_uv, kmi))

            # LMB double-click UV island / edge-loop select
            for dbl_shift, dbl_ctrl, dbl_mode in (
                (False, False, 'set'),
                (True,  False, 'add'),
                (False, True,  'remove'),
            ):
                kmi = km_uv.keymap_items.new(
                    'image.modo_uv_double_click_select',
                    type='LEFTMOUSE',
                    value='DOUBLE_CLICK',
                    shift=dbl_shift,
                    ctrl=dbl_ctrl,
                    head=True,
                )
                kmi.properties.mode = dbl_mode
                state.addon_keymaps.append((km_uv, kmi))

            # 1/2/3 — UV component mode
            for key, uv_mode in (('ONE', 'VERTEX'), ('TWO', 'EDGE'), ('THREE', 'FACE')):
                kmi = km_uv.keymap_items.new(
                    'image.modo_uv_component_mode',
                    type=key,
                    value='PRESS',
                    head=True,
                )
                kmi.properties.mode = uv_mode
                state.addon_keymaps.append((km_uv, kmi))

        if prefs.enable_uv_handle_snap:
            for km_uv in _uv_km_targets:
                for key, ttype in (('W', 'TRANSLATE'), ('E', 'ROTATE'), ('R', 'RESIZE')):
                    kmi = km_uv.keymap_items.new(
                        'image.modo_uv_transform',
                        type=key,
                        value='PRESS',
                        head=True,
                    )
                    kmi.properties.transform_type = ttype
                    state.addon_keymaps.append((km_uv, kmi))

                kmi = km_uv.keymap_items.new(
                    'image.modo_uv_drop_transform',
                    type='SPACE',
                    value='PRESS',
                    head=True,
                )
                state.addon_keymaps.append((km_uv, kmi))

                for shift, ctrl in ((False, False), (True, False), (False, True)):
                    for ev_value in ('PRESS', 'CLICK'):
                        kmi = km_uv.keymap_items.new(
                            'image.modo_uv_handle_reposition',
                            type='LEFTMOUSE',
                            value=ev_value,
                            shift=shift,
                            ctrl=ctrl,
                            head=True,
                        )
                        state.addon_keymaps.append((km_uv, kmi))

            # Also register image.modo_uv_handle_reposition in the UV editor TOOL
            # keymaps.  Blender dispatches tool keymaps (UV Editor Tool: UV Transform,
            # UV Editor Tool: Tweak, etc.) BEFORE editor keymaps (UV Editor), so without
            # this the builtin uv.select fires first and changes the selection even when
            # our gizmo is active.  Our operator's poll() returns False when the gizmo
            # is not active, so it only fires (and blocks uv.select) when W/E/R is on.
            _UV_TOOL_KM_NAMES = (
                'UV Editor Tool: UV Transform',  # builtin.move / rotate / scale
                'UV Editor Tool: Tweak',         # builtin.select (Tweak mode)
                'UV Editor Tool: Select',        # builtin.select (Select mode)
            )
            for _tool_km_name in _UV_TOOL_KM_NAMES:
                try:
                    _tkm = kc.keymaps.new(name=_tool_km_name,
                                          space_type='IMAGE_EDITOR')
                except Exception:
                    continue
                for _shift, _ctrl in ((False, False), (True, False), (False, True)):
                    for _ev_value in ('PRESS', 'CLICK'):
                        _tkmi = _tkm.keymap_items.new(
                            'image.modo_uv_handle_reposition',
                            type='LEFTMOUSE',
                            value=_ev_value,
                            shift=_shift,
                            ctrl=_ctrl,
                            head=True,
                        )
                        state.addon_keymaps.append((_tkm, _tkmi))

        # UV navigation: Alt+LMB pan, Alt+MMB zoom
        for km_uv in _uv_km_targets:
            kmi = km_uv.keymap_items.new(
                'image.view_pan',
                type='LEFTMOUSE',
                value='PRESS',
                alt=True,
                head=True,
            )
            state.addon_keymaps.append((km_uv, kmi))

            kmi = km_uv.keymap_items.new(
                'image.view_pan',
                type='LEFTMOUSE',
                value='PRESS',
                shift=True,
                alt=True,
                head=True,
            )
            state.addon_keymaps.append((km_uv, kmi))

            kmi = km_uv.keymap_items.new(
                'image.view_zoom',
                type='MIDDLEMOUSE',
                value='PRESS',
                alt=True,
                head=True,
            )
            state.addon_keymaps.append((km_uv, kmi))

        # UV lasso selection
        if prefs.enable_lasso_selection:
            for km_uv in _uv_km_targets:
                for btn in ('RIGHTMOUSE', 'MIDDLEMOUSE'):
                    for shift, ctrl, sel_mode in (
                        (False, False, 'set'),
                        (True,  False, 'add'),
                        (False, True,  'remove'),
                    ):
                        kmi = km_uv.keymap_items.new(
                            'image.modo_uv_lasso_select',
                            type=btn, value='PRESS',
                            shift=shift, ctrl=ctrl, head=True,
                        )
                        kmi.properties.mode = sel_mode
                        state.addon_keymaps.append((km_uv, kmi))

        # 3D View navigation: Alt+LMB orbit, Shift+Alt pan, Ctrl+Alt zoom
        kmi = km_nav.keymap_items.new('view3d.rotate', type='LEFTMOUSE',
                                      value='PRESS', alt=True, head=True)
        state.addon_keymaps.append((km_nav, kmi))

        kmi = km_nav.keymap_items.new('view3d.rotate', type='MIDDLEMOUSE',
                                      value='PRESS', alt=True, head=True)
        state.addon_keymaps.append((km_nav, kmi))

        kmi = km_nav.keymap_items.new('view3d.rotate', type='RIGHTMOUSE',
                                      value='PRESS', alt=True, head=True)
        state.addon_keymaps.append((km_nav, kmi))

        kmi = km_nav.keymap_items.new('view3d.move', type='LEFTMOUSE',
                                      value='PRESS', shift=True, alt=True, head=True)
        state.addon_keymaps.append((km_nav, kmi))

        kmi = km_nav.keymap_items.new('view3d.move', type='RIGHTMOUSE',
                                      value='PRESS', shift=True, alt=True, head=True)
        state.addon_keymaps.append((km_nav, kmi))

        kmi = km_nav.keymap_items.new('view3d.zoom', type='LEFTMOUSE',
                                      value='PRESS', ctrl=True, alt=True, head=True)
        state.addon_keymaps.append((km_nav, kmi))

        kmi = km_nav.keymap_items.new('view3d.zoom_border', type='RIGHTMOUSE',
                                      value='PRESS', ctrl=True, alt=True, head=True)
        state.addon_keymaps.append((km_nav, kmi))

        # Component mode 1/2/3/5, Material mode 4, boundary select, transform W/E/R
        if prefs.enable_component_mode:
            for key, comp in (('ONE', 'VERT'), ('TWO', 'EDGE'), ('THREE', 'FACE')):
                kmi = km_obj.keymap_items.new(
                    'view3d.modo_component_mode',
                    type=key, value='PRESS', head=True,
                )
                kmi.properties.component = comp
                kmi.properties.convert = False
                state.addon_keymaps.append((km_obj, kmi))

            for key, comp in (('ONE', 'VERT'), ('TWO', 'EDGE'),
                               ('THREE', 'FACE'), ('FIVE', 'OBJECT')):
                kmi = km.keymap_items.new('view3d.modo_component_mode',
                                          type=key, value='PRESS', head=True)
                kmi.properties.component = comp
                kmi.properties.convert = False
                state.addon_keymaps.append((km, kmi))

            for km_ctx in (km, km_obj):
                kmi = km_ctx.keymap_items.new(
                    'mesh.modo_material_mode',
                    type='FOUR', value='PRESS', head=True,
                )
                state.addon_keymaps.append((km_ctx, kmi))

            for key, comp in (('ONE', 'VERT'), ('TWO', 'EDGE'), ('THREE', 'FACE')):
                kmi = km.keymap_items.new(
                    'view3d.modo_component_mode',
                    type=key, value='PRESS', alt=True, head=True,
                )
                kmi.properties.component = comp
                kmi.properties.convert = True
                state.addon_keymaps.append((km, kmi))

            kmi = km.keymap_items.new('mesh.modo_boundary_select',
                                      type='TWO', value='PRESS',
                                      ctrl=True, head=True)
            kmi.properties.additive = False
            state.addon_keymaps.append((km, kmi))

            kmi = km.keymap_items.new('mesh.modo_boundary_select',
                                      type='TWO', value='PRESS',
                                      ctrl=True, shift=True, head=True)
            kmi.properties.additive = True
            state.addon_keymaps.append((km, kmi))

            for key, ttype in (('W', 'TRANSLATE'), ('E', 'ROTATE'), ('R', 'RESIZE')):
                for target_km in (km_nav, km_obj, km):
                    kmi = target_km.keymap_items.new(
                        'view3d.modo_transform',
                        type=key, value='PRESS', head=True,
                    )
                    kmi.properties.transform_type = ttype
                    state.addon_keymaps.append((target_km, kmi))

            for target_km in (km_nav, km_obj, km):
                kmi = target_km.keymap_items.new(
                    'view3d.modo_drop_transform',
                    type='SPACE', value='PRESS', head=True,
                )
                state.addon_keymaps.append((target_km, kmi))

            for target_km in (km_nav, km_obj, km):
                kmi = target_km.keymap_items.new(
                    'view3d.modo_screen_move',
                    type='MIDDLEMOUSE', value='PRESS', head=True,
                )
                state.addon_keymaps.append((target_km, kmi))

            # Scale gizmo: hover highlight + LMB drag (active when RESIZE tool on)
            kmi = km_nav.keymap_items.new(
                'view3d.modo_scale_gizmo_hover',
                type='MOUSEMOVE', value='ANY', any=True, head=True,
            )
            state.addon_keymaps.append((km_nav, kmi))

            kmi = km_nav.keymap_items.new(
                'view3d.modo_scale_gizmo_drag',
                type='LEFTMOUSE', value='PRESS', head=True,
            )
            state.addon_keymaps.append((km_nav, kmi))

            # Linear falloff: Alt+F — register across all 3D-view keymaps
            for target_km in (km_nav, km_obj, km):
                kmi = target_km.keymap_items.new(
                    'view3d.modo_linear_falloff',
                    type='F', value='PRESS', alt=True, head=True,
                )
                state.addon_keymaps.append((target_km, kmi))

            # Falloff handle drag (LMB — head=True so it intercepts before selection)
            kmi = km_nav.keymap_items.new(
                'view3d.modo_falloff_handle_drag',
                type='LEFTMOUSE', value='PRESS', head=True,
            )
            state.addon_keymaps.append((km_nav, kmi))

    # Build identity tuples for all registered items
    state._registered_kmi_ids.clear()
    for km, kmi in state.addon_keymaps:
        state._registered_kmi_ids.append(
            (km.name, kmi.idname, kmi.type, kmi.value,
             kmi.shift, kmi.ctrl, kmi.alt)
        )

    _disable_conflicting_kmis()

    # Reset UV editor active tool to plain Select
    def _reset_uv_tool():
        _CANDIDATES = ('builtin.select', 'builtin_brush.select', 'builtin.tweak')
        found_uv_area = False
        try:
            for win in bpy.context.window_manager.windows:
                for area in win.screen.areas:
                    if area.type != 'IMAGE_EDITOR':
                        continue
                    found_uv_area = True
                    region = next((r for r in area.regions
                                   if r.type == 'WINDOW'), None)
                    if region is None:
                        continue
                    with bpy.context.temp_override(window=win, area=area,
                                                   region=region):
                        try:
                            cur_tool = bpy.context.workspace.tools\
                                .from_space_image_mode('UV', create=False)
                            cur_name = cur_tool.idname if cur_tool else 'None'
                        except Exception:
                            cur_name = 'unknown'
                        _dbg = get_addon_preferences(bpy.context).debug_selection
                        if _dbg:
                            print(f'[Modo UV] area {id(area)}: current UV tool = {cur_name}')
                        if 'select_box' in cur_name or 'select_circle' in cur_name:
                            for tid in _CANDIDATES:
                                try:
                                    bpy.ops.wm.tool_set_by_id(name=tid,
                                                              space_type='IMAGE_EDITOR')
                                    if _dbg:
                                        print(f'[Modo UV] Reset UV tool → {tid}')
                                    break
                                except Exception:
                                    pass
        except Exception as e:
            print(f'[Modo UV] _reset_uv_tool error: {e}')
        if not found_uv_area:
            return 1.0
        return None
    bpy.app.timers.register(_reset_uv_tool, first_interval=0.1)

    _dump_remaining_conflicts()


def _dump_remaining_conflicts():
    wm = bpy.context.window_manager
    kc_user = wm.keyconfigs.user

    our_bindings = set()
    our_km_names = set()
    for km, kmi in state.addon_keymaps:
        our_bindings.add((km.name, kmi.type, kmi.shift, kmi.ctrl, kmi.alt))
        our_km_names.add(km.name)

    present_in_user = 0
    missing_from_user = []
    if kc_user:
        for km, kmi in state.addon_keymaps:
            user_km = kc_user.keymaps.get(km.name)
            if user_km is None:
                missing_from_user.append(
                    f"  keymap '{km.name}' NOT IN keyconfigs.user"
                )
                continue
            found = False
            for item in user_km.keymap_items:
                if (item.idname == kmi.idname
                        and item.type == kmi.type
                        and item.shift == kmi.shift
                        and item.ctrl == kmi.ctrl
                        and item.alt == kmi.alt
                        and item.active):
                    found = True
                    break
            if found:
                present_in_user += 1
            else:
                missing_from_user.append(
                    f"  {kmi.idname} ({kmi.type} shift={kmi.shift} "
                    f"ctrl={kmi.ctrl} alt={kmi.alt}) "
                    f"NOT FOUND (active) in user '{km.name}'"
                )

    true_conflicts = []
    if kc_user:
        for km in kc_user.keymaps:
            if km.name not in our_km_names:
                continue
            our_pos = {}
            for idx, kmi in enumerate(km.keymap_items):
                if not kmi.active:
                    continue
                combo = (km.name, kmi.type, kmi.shift, kmi.ctrl, kmi.alt)
                if combo not in our_bindings:
                    continue
                if kmi.idname in state._OUR_IDNAMES and combo not in our_pos:
                    our_pos[combo] = idx

            for idx, kmi in enumerate(km.keymap_items):
                if not kmi.active or kmi.idname in state._OUR_IDNAMES:
                    continue
                combo = (km.name, kmi.type, kmi.shift, kmi.ctrl, kmi.alt)
                if combo not in our_bindings:
                    continue
                pos = our_pos.get(combo)
                if pos is None or idx < pos:
                    true_conflicts.append(
                        f"  {km.name:25s} pos={idx:<4d} | "
                        f"{kmi.idname:40s} | {kmi.type:12s} "
                        f"shift={kmi.shift} ctrl={kmi.ctrl} alt={kmi.alt}"
                    )

    _prefs = get_addon_preferences(bpy.context)
    if _prefs.debug_selection:
        print(f"[Modo-Style Selection] --- Diagnostic Summary ---")
        print(f"  Evaluated keyconfig: {kc_user.name if kc_user else 'None'}")
        print(f"  Addon keymaps registered: {len(state.addon_keymaps)}")
        print(f"  Conflicts disabled: {len(state._disabled_kmi_ids)}")
        if missing_from_user:
            print(f"  *** {len(missing_from_user)} bindings MISSING from keyconfigs.user ***")
            for line in missing_from_user[:30]:
                print(line)
        else:
            print(f"  All {present_in_user} bindings confirmed.")
        if true_conflicts:
            print(f"  *** {len(true_conflicts)} items SHADOW our bindings ***")
            for line in sorted(set(true_conflicts)):
                print(line)
        else:
            print(f"  No shadowing conflicts found.")
        _dump_uv_lmb_keymaps()


def _dump_uv_lmb_keymaps():
    wm = bpy.context.window_manager
    kc_user = wm.keyconfigs.user if wm else None
    if kc_user is None:
        return
    print('[Modo UV] === UV Editor LEFTMOUSE keymap order ===')
    for km in kc_user.keymaps:
        if km.name not in ('UV Editor', 'Image'):
            continue
        lmb_items = [(idx, kmi) for idx, kmi in enumerate(km.keymap_items)
                     if kmi.type == 'LEFTMOUSE']
        if not lmb_items:
            continue
        print(f'  [{km.name}]')
        for idx, kmi in lmb_items:
            active_str = 'ACTIVE  ' if kmi.active else 'disabled'
            mods = ''.join(['S' if kmi.shift else '-',
                            'C' if kmi.ctrl  else '-',
                            'A' if kmi.alt   else '-'])
            print(f'    pos={idx:<4d} {active_str} {mods} '
                  f'val={kmi.value:<12s} {kmi.idname}')
    print('[Modo UV] ===================================================')

    wm2 = bpy.context.window_manager
    print('[Modo UV] === UV Editor active tool per area ===')
    found_uv_area = False
    for win in wm2.windows:
        for area in win.screen.areas:
            if area.type != 'IMAGE_EDITOR':
                continue
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region is None:
                continue
            try:
                with bpy.context.temp_override(window=win, area=area, region=region):
                    tool = bpy.context.workspace.tools.from_space_image_mode(
                        'UV', create=False)
                    tname = tool.idname if tool else 'None'
            except Exception as e:
                tname = f'ERROR: {e}'
            print(f'  area id={id(area)} → tool={tname}')
            found_uv_area = True
    if not found_uv_area:
        print('  (no UV Editor areas open right now)')
    print('[Modo UV] ===================================================')


def unregister_keymaps():
    from collections import Counter
    wm = bpy.context.window_manager
    kc_user = wm.keyconfigs.user if wm else None

    if kc_user is not None:
        if state._registered_kmi_ids:
            remaining = Counter(state._registered_kmi_ids)
            for km in kc_user.keymaps:
                to_remove = []
                for kmi in km.keymap_items:
                    identity = (km.name, kmi.idname, kmi.type, kmi.value,
                                kmi.shift, kmi.ctrl, kmi.alt)
                    if remaining[identity] > 0:
                        to_remove.append(kmi)
                        remaining[identity] -= 1
                for kmi in to_remove:
                    try:
                        km.keymap_items.remove(kmi)
                    except (RuntimeError, ReferenceError):
                        pass

        for km in kc_user.keymaps:
            stale = [kmi for kmi in km.keymap_items
                     if kmi.idname in state._OUR_IDNAMES]
            for kmi in stale:
                try:
                    km.keymap_items.remove(kmi)
                except (RuntimeError, ReferenceError):
                    pass

    _restore_conflicting_kmis()

    state._registered_kmi_ids.clear()
    state.addon_keymaps.clear()


# ============================================================================
# Deferred setup timer
# ============================================================================

def _deferred_keymap_setup():
    """Repeating timer: re-scan and disable conflicting keymaps.

    Runs up to _DEFERRED_MAX_RETRIES times so it catches preset keymaps that
    load after the addon's register().
    """
    state._deferred_retry_count += 1

    try:
        wm = bpy.context.window_manager
        if wm is None or wm.keyconfigs.addon is None:
            if state._deferred_retry_count < state._DEFERRED_MAX_RETRIES:
                return state._DEFERRED_RETRY_INTERVAL
            state._deferred_timer_registered = False
            return None

        # Startup-adoption path
        if not state._registered_kmi_ids:
            kc_user = wm.keyconfigs.user
            if kc_user:
                saved = [
                    (km, kmi)
                    for km in kc_user.keymaps
                    for kmi in km.keymap_items
                    if kmi.idname in state._OUR_IDNAMES
                ]
                if saved:
                    state.addon_keymaps.clear()
                    state.addon_keymaps.extend(saved)
                    state._registered_kmi_ids.clear()
                    state._registered_kmi_ids.extend(
                        (km.name, kmi.idname, kmi.type, kmi.value,
                         kmi.shift, kmi.ctrl, kmi.alt)
                        for km, kmi in saved
                    )
                    _disable_conflicting_kmis()
                    disabled_count = len(state._disabled_kmi_ids)
                    _prefs = get_addon_preferences(bpy.context)

                    _restore_all_addon_prefs('adoption')

                    if _prefs.debug_selection:
                        print(
                            f"[Modo-Style Selection] startup adoption pass "
                            f"{state._deferred_retry_count}: adopted "
                            f"{len(saved)} saved bindings, disabled "
                            f"{disabled_count} conflicts"
                        )
                    if disabled_count == 0 and state._deferred_retry_count < state._DEFERRED_MAX_RETRIES:
                        return state._DEFERRED_RETRY_INTERVAL
                    _backup_all_addon_prefs()
                    state._deferred_timer_registered = False
                    return None

        # Normal (re-)registration path
        try:
            _real_prefs = bpy.context.preferences.addons[
                'modo_style_selection_for_blender'].preferences
        except (KeyError, AttributeError):
            _real_prefs = None
        if _real_prefs is None:
            if state._deferred_retry_count < state._DEFERRED_MAX_RETRIES:
                return state._DEFERRED_RETRY_INTERVAL

        unregister_keymaps()
        _restore_all_addon_prefs('normal')
        register_keymaps()

        disabled_count = len(state._disabled_kmi_ids)
        _prefs = get_addon_preferences(bpy.context)
        if _prefs.debug_selection:
            print(f"[Modo-Style Selection] keymap pass {state._deferred_retry_count}: "
                  f"registered {len(state.addon_keymaps)} bindings, "
                  f"disabled {disabled_count} conflicts")

        if disabled_count == 0 and state._deferred_retry_count < state._DEFERRED_MAX_RETRIES:
            return state._DEFERRED_RETRY_INTERVAL

    except Exception as ex:
        _prefs = get_addon_preferences(bpy.context)
        if _prefs.debug_selection:
            print(f"[Modo-Style Selection] keymap pass {state._deferred_retry_count} error: {ex}")
        if state._deferred_retry_count < state._DEFERRED_MAX_RETRIES:
            return state._DEFERRED_RETRY_INTERVAL

    _backup_all_addon_prefs()
    state._deferred_timer_registered = False
    return None


def _schedule_deferred_keymap_setup():
    if state._deferred_timer_registered:
        return
    state._deferred_retry_count = 0
    state._deferred_timer_registered = True
    bpy.app.timers.register(_deferred_keymap_setup,
                            first_interval=state._DEFERRED_RETRY_INTERVAL)


def _uv_tool_guardian():
    """Periodic timer: resets UV editor active tool to plain Select."""
    if not state._uv_tool_guardian_running:
        return None

    _CANDIDATES = ('builtin.select', 'builtin_brush.select', 'builtin.tweak')
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type != 'IMAGE_EDITOR':
                    continue
                region = next((r for r in area.regions
                               if r.type == 'WINDOW'), None)
                if region is None:
                    continue
                with bpy.context.temp_override(window=win, area=area,
                                               region=region):
                    try:
                        cur_tool = bpy.context.workspace.tools\
                            .from_space_image_mode('UV', create=False)
                        cur_name = cur_tool.idname if cur_tool else 'None'
                    except Exception:
                        cur_name = 'unknown'
                    if 'select_box' in cur_name or 'select_circle' in cur_name:
                        for tid in _CANDIDATES:
                            try:
                                bpy.ops.wm.tool_set_by_id(
                                    name=tid, space_type='IMAGE_EDITOR')
                                print(f'[Modo UV] guardian: Reset UV tool → {tid}')
                                break
                            except Exception:
                                pass
    except Exception as e:
        print(f'[Modo UV] _uv_tool_guardian error: {e}')

    return state._UV_TOOL_GUARDIAN_INTERVAL


# ============================================================================
# Preferences backup / restore (survives Reload Scripts)
# ============================================================================

def _backup_all_addon_prefs():
    """Snapshot every enabled addon's preferences into driver_namespace."""
    all_backup = dict(bpy.app.driver_namespace.get('_modo_all_prefs_backup') or {})
    try:
        for _addon_name, _addon_ref in bpy.context.preferences.addons.items():
            try:
                _aprefs = _addon_ref.preferences
                if _aprefs is None:
                    continue
                _aprops = {}
                for _p in _aprefs.bl_rna.properties:
                    if _p.identifier in ('rna_type', 'bl_idname'):
                        continue
                    try:
                        _aprops[_p.identifier] = getattr(_aprefs, _p.identifier)
                    except Exception:
                        pass
                if _aprops:
                    all_backup[_addon_name] = _aprops
            except Exception:
                pass
    except Exception:
        pass
    bpy.app.driver_namespace['_modo_all_prefs_backup'] = all_backup
    _uv_debug_log(
        f'[PREFS-BACKUP] backed_up={len(all_backup)} addons'
    )


def _restore_all_addon_prefs(label=''):
    """Restore addon preferences from driver_namespace backup."""
    all_backup = bpy.app.driver_namespace.pop('_modo_all_prefs_backup', None)
    bpy.app.driver_namespace.pop('_modo_prefs_backup', None)
    if not all_backup:
        _uv_debug_log(f'[PREFS-RESTORE] {label}: no backup')
        return
    total_restored = 0
    _our_saved = all_backup.get('modo_style_selection_for_blender', {})
    if _our_saved:
        try:
            _addon_ref = bpy.context.preferences.addons.get(
                'modo_style_selection_for_blender')
            if _addon_ref is not None:
                _aprefs = _addon_ref.preferences
                if _aprefs is not None:
                    for _k, _v in _our_saved.items():
                        try:
                            setattr(_aprefs, _k, _v)
                            total_restored += 1
                        except Exception:
                            pass
        except Exception:
            pass
    _uv_debug_log(
        f'[PREFS-RESTORE] {label}: restored {total_restored} props'
    )


# ============================================================================
# Persistent handlers
# ============================================================================

@bpy.app.handlers.persistent
def _keymap_load_post_handler(dummy):
    """Re-register keymaps after every file load."""
    _schedule_deferred_keymap_setup()
