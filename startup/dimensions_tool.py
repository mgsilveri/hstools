"""Dimensions Tool — startup script

Mimics Modo's "View > Dimensions Tool":
Displays the bounding-box dimensions of the current object/component
selection as a viewport overlay.  Toggle via View menu or the operator
`view3d.dimensions_tool`.
"""

import bpy
import bmesh
import gpu
import blf
import math
from gpu_extras.batch import batch_for_shader
from bpy_extras.view3d_utils import location_3d_to_region_2d
from mathutils import Vector
from bpy.app.handlers import persistent

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_draw_handle = None
_bbox_min = None
_bbox_max = None
_has_selection = False

_WM_PROP = "dimensions_tool_enabled"


# ---------------------------------------------------------------------------
# Bounding box computation  (called from depsgraph handler – no GPU access)
# ---------------------------------------------------------------------------

def _compute_bbox(context):
    global _bbox_min, _bbox_max, _has_selection
    verts = []
    try:
        if context.mode == 'EDIT_MESH':
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm = bmesh.from_edit_mesh(obj.data)
                mx = obj.matrix_world
                for v in bm.verts:
                    if v.select:
                        verts.append(mx @ v.co)
        else:
            for obj in context.selected_objects:
                if obj.type == 'MESH':
                    mx = obj.matrix_world
                    for corner in obj.bound_box:
                        verts.append(mx @ Vector(corner))
                else:
                    verts.append(obj.location.copy())
    except Exception:
        pass

    if verts:
        xs = [v.x for v in verts]
        ys = [v.y for v in verts]
        zs = [v.z for v in verts]
        _bbox_min = Vector((min(xs), min(ys), min(zs)))
        _bbox_max = Vector((max(xs), max(ys), max(zs)))
        _has_selection = True
    else:
        _bbox_min = _bbox_max = None
        _has_selection = False


@persistent
def _on_depsgraph_update(scene, depsgraph):
    if not getattr(bpy.context.window_manager, _WM_PROP, False):
        return
    _compute_bbox(bpy.context)


# ---------------------------------------------------------------------------
# Unit formatting
# ---------------------------------------------------------------------------

def _fmt(blender_units):
    try:
        us = bpy.context.scene.unit_settings
        v = blender_units * us.scale_length
        sys = us.system
    except Exception:
        return f"{blender_units:.4g}"

    if sys == 'METRIC':
        if abs(v) >= 1.0:
            return f"{v:.4g} m"
        elif abs(v) >= 0.01:
            return f"{v * 100:.4g} cm"
        else:
            return f"{v * 1000:.4g} mm"
    elif sys == 'IMPERIAL':
        feet = v * 3.28084
        return f"{feet:.4g} ft" if abs(feet) >= 1.0 else f"{feet * 12:.4g} in"
    return f"{blender_units:.4g}"


# ---------------------------------------------------------------------------
# Draw callback (POST_PIXEL)
# ---------------------------------------------------------------------------

def _draw_callback():
    try:
        _draw_callback_inner()
    except Exception:
        pass


def _tick_coords(a3, b3, a2, b2, px_per_unit, p2d, perp_2d,
                 cap_size=24, major_size=24, minor_size=8,
                 target_minor_px=15, major_every=2):
    """World-space adaptive ticks projected to 2D.

    perp_2d is a pre-computed (px, py) unit vector in screen space, derived
    from a fixed world-space axis so ticks don't rotate with the camera.
    """
    world_len = (b3 - a3).length
    if world_len < 1e-6 or px_per_unit < 1e-6:
        return []

    direction = (b3 - a3) / world_len
    perp_x, perp_y = perp_2d

    # Choose a nice world-space interval so minor ticks are ~target_minor_px apart
    raw = target_minor_px / px_per_unit
    mag = 10 ** math.floor(math.log10(max(raw, 1e-9)))
    world_interval = mag * 10
    for step in (1, 2, 5, 10):
        if mag * step >= raw:
            world_interval = mag * step
            break

    coords = []

    # End caps
    hs = cap_size * 0.5
    coords += [
        (a2.x - perp_x * hs, a2.y - perp_y * hs), (a2.x + perp_x * hs, a2.y + perp_y * hs),
        (b2.x - perp_x * hs, b2.y - perp_y * hs), (b2.x + perp_x * hs, b2.y + perp_y * hs),
    ]

    # Ruler ticks at world-space intervals
    num = int(world_len / world_interval)
    for i in range(1, num):
        world_pos = i * world_interval
        if world_pos >= world_len:
            break
        pos_3d = a3 + direction * world_pos
        pos_2d = p2d(pos_3d)
        if pos_2d is None:
            continue
        is_major = (i % major_every == 0)
        hs_t = major_size * 0.5 if is_major else minor_size * 0.5
        coords += [
            (pos_2d.x - perp_x * hs_t, pos_2d.y - perp_y * hs_t),
            (pos_2d.x + perp_x * hs_t, pos_2d.y + perp_y * hs_t),
        ]

    return coords


def _world_perp_2d(mid3, world_perp_options, bbox_centre_2d, p2d):
    """Project world-space perpendicular candidates, pick the one with most
    screen extent, then flip to point away from bbox centre."""
    best = None
    best_mag = -1.0
    for wp in world_perp_options:
        p = p2d(mid3 + wp * 0.5)
        m = p2d(mid3)
        if p is None or m is None:
            continue
        dx, dy = p.x - m.x, p.y - m.y
        mag = math.sqrt(dx * dx + dy * dy)
        if mag > best_mag:
            best_mag = mag
            best = (dx / mag, dy / mag) if mag > 1e-6 else (1.0, 0.0)
    if best is None:
        return (0.0, 1.0)
    # Flip so it points away from bbox centre
    if bbox_centre_2d is not None:
        cx = mid3.x  # approximate – use 2D midpoint instead
    px, py = best
    if bbox_centre_2d is not None:
        m2 = p2d(mid3)
        if m2 is not None:
            to_cx = m2.x - bbox_centre_2d.x
            to_cy = m2.y - bbox_centre_2d.y
            if px * to_cx + py * to_cy < 0:
                px, py = -px, -py
    return (px, py)


def _draw_callback_inner():
    if not _has_selection or _bbox_min is None:
        return

    ctx = bpy.context
    region = ctx.region
    rv3d = ctx.region_data
    if region is None or rv3d is None:
        return

    bmin, bmax = _bbox_min, _bbox_max
    sx = bmax.x - bmin.x
    sy = bmax.y - bmin.y
    sz = bmax.z - bmin.z

    def p2d(v):
        return location_3d_to_region_2d(region, rv3d, v)

    # Three dimension lines from the min corner along each world axis
    dim_lines_3d = [
        (bmin, Vector((bmax.x, bmin.y, bmin.z)), sx),  # X width
        (bmin, Vector((bmin.x, bmax.y, bmin.z)), sy),  # Y depth
        (bmin, Vector((bmin.x, bmin.y, bmax.z)), sz),  # Z height
    ]
    lines = []  # (a2, b2, a3, b3, size)
    for a3, b3, size in dim_lines_3d:
        a2 = p2d(a3)
        b2 = p2d(b3)
        if a2 is not None and b2 is not None:
            lines.append((a2, b2, a3, b3, size))

    if not lines:
        return

    # Compute px_per_unit by probing laterally from the orbit pivot.
    # Using rv3d.view_location (orbit center) keeps the camera distance
    # constant during orbit, so px_per_unit is zoom-only, angle-independent.
    view_origin = Vector(rv3d.view_location)
    view_right = Vector(rv3d.view_matrix[0][:3]).normalized()
    probe_dist = max((_bbox_max - _bbox_min).length * 0.01, 0.001)
    c2 = p2d(view_origin)
    p2_probe = p2d(view_origin + view_right * probe_dist)
    if c2 is None or p2_probe is None:
        return
    px_per_unit = math.sqrt((p2_probe.x - c2.x) ** 2 + (p2_probe.y - c2.y) ** 2) / probe_dist
    if px_per_unit < 1e-6:
        return

    color_line = (0.78, 0.70, 0.92, 0.65)  # soft lavender/lilac – matches Modo
    color_tick = (0.78, 0.70, 0.92, 0.40)  # same, less opaque
    color_text = (0.3, 0.85, 0.85, 1.0) # teal/cyan – labels (Modo)

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    # --- Solid dimension lines ---
    line_coords = []
    for a2, b2, a3, b3, _ in lines:
        line_coords += [(a2.x, a2.y), (b2.x, b2.y)]

    shader.bind()
    shader.uniform_float("color", color_line)
    gpu.state.line_width_set(1.0)

    if line_coords:
        batch_for_shader(shader, 'LINES', {"pos": line_coords}).draw(shader)

    # World-space perpendicular candidates per axis line.
    # X line: perp in -Z or -Y (prefer Z – points down in world)
    # Y line: perp in -Z or -X
    # Z line: perp in -X or -Y (prefer X – points left in world)
    world_perp_candidates = [
        [Vector((0, 0, -1)), Vector((0, -1, 0))],  # X line
        [Vector((0, 0, -1)), Vector((-1, 0, 0))],  # Y line
        [Vector((-1, 0, 0)), Vector((0, -1, 0))],  # Z line
    ]

    bbox_centre_2d = p2d((_bbox_min + _bbox_max) * 0.5)

    # Draw world-space adaptive ticks per line
    shader.uniform_float("color", color_tick)
    for idx, (a2, b2, a3, b3, _) in enumerate(lines):
        mid3 = (a3 + b3) * 0.5
        perp_2d = _world_perp_2d(mid3, world_perp_candidates[idx], bbox_centre_2d, p2d)
        ticks = _tick_coords(a3, b3, a2, b2, px_per_unit, p2d, perp_2d)
        if ticks:
            batch_for_shader(shader, 'LINES', {"pos": ticks}).draw(shader)

    gpu.state.blend_set('NONE')

    # --- Text labels at the midpoint of each dimension line ---
    fid = 0
    blf.size(fid, 20)
    blf.color(fid, *color_text)

    # Project bbox centre once – labels push away from this point
    bbox_centre_2d = p2d((_bbox_min + _bbox_max) * 0.5)

    for a2, b2, a3, b3, size in lines:
        if abs(size) < 1e-6:
            continue
        label = _fmt(size)
        mid_x = (a2.x + b2.x) * 0.5
        mid_y = (a2.y + b2.y) * 0.5
        w, h = blf.dimensions(fid, label)

        dx = b2.x - a2.x
        dy = b2.y - a2.y
        line_len = math.sqrt(dx * dx + dy * dy)
        if line_len > 1e-6:
            perp_x = -dy / line_len
            perp_y =  dx / line_len
            # Flip so the label is pushed away from the projected bbox centre
            if bbox_centre_2d is not None:
                to_cx = mid_x - bbox_centre_2d.x
                to_cy = mid_y - bbox_centre_2d.y
            else:
                to_cx = mid_x - region.width * 0.5
                to_cy = mid_y - region.height * 0.5
            if perp_x * to_cx + perp_y * to_cy < 0:
                perp_x, perp_y = -perp_x, -perp_y
        else:
            perp_x, perp_y = 0.0, 1.0

        offset = 10
        # Shift the near edge of the text away from the line, not just the centre
        if abs(perp_x) >= abs(perp_y):
            # Primarily horizontal push
            tx = (mid_x - w - offset) if perp_x < 0 else (mid_x + offset)
            ty = mid_y - h * 0.5
        else:
            # Primarily vertical push
            tx = mid_x - w * 0.5
            ty = (mid_y - h - offset) if perp_y < 0 else (mid_y + offset)
        blf.position(fid, tx, ty, 0)
        blf.draw(fid, label)


# ---------------------------------------------------------------------------
# Toggle operator
# ---------------------------------------------------------------------------

class VIEW3D_OT_dimensions_tool(bpy.types.Operator):
    """Toggle selection dimensions overlay (Modo-style Dimensions Tool)"""
    bl_idname = "view3d.dimensions_tool"
    bl_label = "Dimensions Tool"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == 'VIEW_3D'

    def execute(self, context):
        global _draw_handle

        wm = context.window_manager
        enabled = not wm.dimensions_tool_enabled
        wm.dimensions_tool_enabled = enabled

        if enabled:
            _compute_bbox(context)
            _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                _draw_callback, (), 'WINDOW', 'POST_PIXEL'
            )
        else:
            if _draw_handle is not None:
                bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
                _draw_handle = None

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# View menu entry
# ---------------------------------------------------------------------------

def _menu_func(self, context):
    wm = context.window_manager
    active = getattr(wm, _WM_PROP, False)
    self.layout.operator(
        "view3d.dimensions_tool",
        text="Dimensions Tool",
        icon='CHECKMARK' if active else 'BLANK1',
        depress=active,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (VIEW3D_OT_dimensions_tool,)


def register():
    bpy.types.WindowManager.dimensions_tool_enabled = bpy.props.BoolProperty(
        name="Dimensions Tool",
        default=False,
    )
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_view.append(_menu_func)
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)


def unregister():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    bpy.types.VIEW3D_MT_view.remove(_menu_func)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.dimensions_tool_enabled
