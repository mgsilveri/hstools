"""
Shared mutable state for modo_style_selection_for_blender.

All module-level globals that are read or written by more than one submodule
live here.  Import with ``from . import state`` then access as
``state._varname``.  Assign with ``state._varname = new_value``
(no ``global`` keyword required in the calling function).
"""

# ── Backface visualization ────────────────────────────────────────────────────
_saved_viewport_settings: dict = {}   # space_id → dict of saved values
_bfv_previous_mode: str = ""          # tracks last-seen context.mode
_back_edge_draw_handle = None         # handle returned by draw_handler_add
_back_edge_cache: list = []           # world-space edge-coord pairs for GPU draw
_uv_cache_dirty_time: float = 0.0    # timestamp of last depsgraph MESH update
_uv_cache_dirty_gen:  int   = 0      # incremented on every MESH depsgraph update
_UV_STABLE_DELAY: float = 0.0        # seconds to wait before accessing BMesh after a MESH update

# ── Backface viz timing debug (set True to print timestamps to System Console) ─
_BFV_TIMING_DEBUG: bool = False

# ── 3D View transform tools (W / E / R) ──────────────────────────────────────
# 'TRANSLATE' | 'ROTATE' | 'RESIZE' | None
_active_transform_mode = None

# True while Modo Materials mode (key 4) is active.
_material_mode_active: bool = False

# Per-mesh independent selection memory (Modo: each mode keeps its own selection).
# Structure: {mesh_data_name: {'VERT': {indices}, 'EDGE': {indices}, 'FACE': {indices}}}
_selection_memory: dict = {}

# Set by ALT+mode (convert) operations so the non-convert path knows:
#   - skip_save: don't overwrite this mode's independent memory with the
#     temporary converted selection
#   - if pressing the SAME mode key as the convert target, bail early —
#     the converted selection is already displayed, nothing to restore
_last_convert_target: str = ''   # sub-mode the last convert went TO ('VERT'/'EDGE'/'FACE')

# Per-mesh independent UV selection memory (sync OFF only).
_uv_selection_memory: dict = {}

# Saved pivot / cursor before W snapped them to CURSOR.
_saved_pivot_point = None          # str | None
_saved_cursor_location = None      # Vector | None
_saved_snap_target = None          # str | None  (snap_target / snap_source)

# Anchor tracking for the Move tool.
_reposition_anchor = None          # Vector | None
_last_known_median = None          # Vector | None
_anchor_timer_running: bool = False

# Draw handle for the pivot crosshair overlay.
_pivot_crosshair_draw_handle = None
# Running-average interval between crosshair draw callbacks (= viewport frame time).
_viewport_draw_interval: float = 0.016   # starts at 60 fps, self-calibrates
_last_crosshair_draw_time: float = 0.0   # monotonic timestamp of last draw

# True when the gizmo activated with nothing selected (auto-selected all).
_implicit_select_all: bool = False

# ── Snap highlight (3D View) ──────────────────────────────────────────────────
# Keys: 'screen_pos' (x,y), 'world_pos' Vector, 'elem_type' str
_snap_highlight = None             # dict | None
_snap_highlight_draw_handle = None

# ── Scale gizmo (3D viewport R tool) ──────────────────────────────────────────
_scale_gizmo_draw_handle = None
# Cached per-frame screen-space handle positions for hit-testing.
# Keys: 'pivot' (px,py), 'X/Y/Z' (ex,ey), 'XY/XZ/YZ' (cx,cy),
#       'X_dir/Y_dir/Z_dir' (ndx,ndy)  — all in region pixels.
_scale_gizmo_screen_handles: dict = {}
_scale_gizmo_hover: str = ''       # '' | 'X' | 'Y' | 'Z' | 'XY' | 'XZ' | 'YZ' | 'XYZ'

# ── Linear Falloff ─────────────────────────────────────────────────────────────
_falloff_draw_handle        = None   # handles + gradient line (POST_PIXEL)
_falloff_mesh_draw_handle   = None   # per-vertex weight overlay (POST_VIEW)
_falloff_hover_handle: str  = ''     # 'START' | 'END' | ''
_falloff_define_active: bool = False  # True while drag-to-define modal is running# Cached per-frame screen-space positions for hit-testing (set by draw callback).
# Keys: 'START' → (sx, sy), 'END' → (ex, ey) — region pixel coords.
_falloff_screen_handles: dict = {}
# ── UV transform ─────────────────────────────────────────────────────────────
_uv_active_transform_mode = None   # 'TRANSLATE' | 'ROTATE' | 'RESIZE' | None
_uv_gizmo_center = None            # (u, v) tuple | None
_uv_transform_targets = None       # list | None
_uv_sel_targets = None             # list | None
_uv_sel_corner_set = None          # dict | None
_uv_handle_modal_active: bool = False

# ── UV snap highlight ─────────────────────────────────────────────────────────
_uv_snap_highlight = None          # dict | None — keys: screen_pos, uv_pos, elem_type
_uv_snap_highlight_draw_handle = None

# ── UV gizmo ──────────────────────────────────────────────────────────────────
_uv_gizmo_draw_handle = None
_uv_gizmo_hover_axis = None        # None | 'X' | 'Y' | 'CENTER'

# ── UV preselect drag suppression ─────────────────────────────────────────────
_uv_lmb_down: bool = False         # True while LMB is held in the UV editor

# ── UV overlays ───────────────────────────────────────────────────────────────
_uv_boundary_draw_handle = None
_uv_flipped_face_draw_handle = None
_uv_overlap_draw_handle = None
_uv_distortion_draw_handle = None
_uv_coverage_hud_draw_handle = None
_uv_active_face_draw_handle = None
_flipped_face_uv_cache: list = []
_distortion_uv_cache: list = []        # list of (uv_polygon, rgba) per face
_uv_boundary_cache: dict = {'uv_mode': None, 'points': [], 'segments': []}
_uv_coverage_pct: float = 0.0          # 0–100 % of 0–1 tile covered
_uv_coverage_dirty: bool = True        # recompute on next draw

# ── Instance tagging ─────────────────────────────────────────────────────────
_INST_PREFIX = 'inst_'
_INST_COLLECTION = 'Instances'
_INST_COLLECTION_TAG = 'COLOR_07'   # pink
_instance_tag_last_run: float = 0.0

# ── Pre-selection highlight ───────────────────────────────────────────────────
# Each entry in the list is a dict:
#   '3d':  {'type': 'FACE'|'EDGE'|'VERT'|'OBJECT', 'coords': [...], 'selected': bool, 'obj': obj}
#   'uv':  {'type': 'FACE'|'EDGE'|'VERT', 'coords': [...], 'selected': bool}  (or absent)
_preselect_hits: list = []          # populated on MOUSEMOVE, consumed by draw + click
_preselect_draw_handle_3d    = None  # SpaceView3D  POST_VIEW  (faces)
_preselect_draw_handle_3d_px = None  # SpaceView3D  POST_PIXEL (edges + verts, always on top)
_preselect_draw_handle_uv    = None  # SpaceImageEditor draw handler
_preselect_mode: str = ''           # last-seen context.mode, for mode-change detection

# ── Keymap ────────────────────────────────────────────────────────────────────
addon_keymaps = []
_registered_kmi_ids = []
_disabled_kmi_ids = []
_saved_rmb_menus = {}

_OUR_IDNAMES = {
    'mesh.modo_select_element_under_mouse',
    'mesh.modo_select_shortest_path',
    'mesh.modo_lasso_select',
    'mesh.modo_boundary_select',
    'mesh.modo_material_mode',
    'object.modo_click_select',
    'object.modo_lasso_select',
    'view3d.modo_component_mode',
    'view3d.modo_transform',
    'view3d.modo_drop_transform',
    'view3d.modo_screen_move',
    'view3d.modo_scale_gizmo_hover',
    'view3d.modo_scale_gizmo_drag',
    'view3d.modo_linear_falloff',
    'view3d.modo_falloff_handle_hover',
    'view3d.modo_falloff_handle_drag',
    'view3d.modo_preselect_highlight',
    'image.modo_preselect_highlight',
    'image.modo_uv_snap_highlight',
    'image.modo_uv_transform',
    'image.modo_uv_drop_transform',
    'image.modo_uv_handle_reposition',
    'image.modo_uv_stitch',
    'image.modo_uv_component_mode',
    'image.modo_uv_paint_selection',
    'image.modo_uv_click_select',
    'image.modo_uv_lasso_select',
    'image.modo_uv_double_click_select',
    'image.modo_uv_rip',
    'image.modo_preselect_lmb_track',
}

_NAV_IDNAMES = {
    'view3d.rotate',
    'view3d.move',
    'view3d.zoom',
    'view3d.zoom_border',
    'view3d.view_axis',
    'view3d.view_center_pick',
    'image.view_pan',
    'image.view_zoom',
}

# ── Deferred keymap setup ─────────────────────────────────────────────────────
_deferred_retry_count = 0
_DEFERRED_MAX_RETRIES = 10
_DEFERRED_RETRY_INTERVAL = 0.5
_deferred_timer_registered = False

_UV_TOOL_GUARDIAN_INTERVAL = 5.0
_uv_tool_guardian_running = False

# ── Header patch ─────────────────────────────────────────────────────────────
_orig_editor_menus_draw_collapsible = None

# ── Mesh modal safety gate ────────────────────────────────────────────────────
# Set True by _uv_seam_redraw_depsgraph_handler when an operator that modifies
# UV loop data live in C (e.g. TRANSFORM_OT_edge_slide with correct_uv=True)
# is detected in window.modal_operators.  Cleared when no such modal is running.
# Checked by all UV overlay draw callbacks before calling bmesh.from_edit_mesh()
# to avoid a tbbmalloc access-violation crash (Blender 5.0.1, 2025).
_mesh_modal_unsafe: bool = False
# True while a deferred timer is pending to clear _mesh_modal_unsafe.
# Prevents multiple clear timers being stacked.
_mesh_modal_unsafe_clear_pending: bool = False
