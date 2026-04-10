"""
Modo-style pre-selection highlight.

Highlights geometry under the mouse before the user clicks, in both
Object Mode and Edit Mode.

Architecture (no persistent modal — avoids 'Reload Scripts' blocking):
  A MOUSEMOVE keymap entry in '3D View' invokes this operator on every
  mouse move.  The operator returns FINISHED immediately after updating
  state._preselect_hits.  Navigation modals receive MOUSEMOVE BEFORE
  keymap items, so viewport navigation is never affected.

State storage  →  state._preselect_hits (list of hit dicts)
BVH cache      →  raycast._bvh_cache  (keyed by obj.data.name)
Draw handles   →  state._preselect_draw_handle_3d / _draw_handle_uv

Visual rules
────────────
  Unselected element   →  preselect_color pref  (default #c4dbe5, ~30% alpha for faces)
  Selected element     →  lightened theme selection color (hover-on-selected)
  Object Mode          →  world-space edges of hovered mesh object
  UV sync ON           →  matching UV polygon/edge/vert drawn in Image Editor
  UV sync OFF, selected→  lightened selection color in Image Editor (UV visible)
  UV sync OFF, unsel.  →  nothing in Image Editor (UVs not shown)
  No UV map            →  3D highlight works; Image Editor silently skipped
"""

import math
import bpy
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from bpy_extras import view3d_utils

from . import state
from .utils import get_addon_preferences, _diag
from .raycast import _get_cached_bvh, clear_bvh_cache


# ── Constants ─────────────────────────────────────────────────────────────────

_EDGE_PIXEL_THRESHOLD = 7    # px — screen-space radius for edge/vert proximity
_VERT_PIXEL_THRESHOLD = 12
_POINT_SIZE           = 8    # px — vertex highlight quad size
_Z_BIAS               = 5e-4 # kept for reference; no longer used by _nudge_toward_camera

# Set True to print diagnostics to the Blender System Console (Window → Toggle System Console)
_PRESELECT_DEBUG = False
# Set True to print per-edge ring + distance data for the 3D view edge detection
_EDGE_DEBUG = False

# Stipple (checkerboard) shader — discards every other pixel so the face fill
# appears as solid-colored dots (~50% coverage) rather than a semi-transparent wash.
# Cached at module level; reset to None by Reload Scripts.
_stipple_shader_3d_cache = None   # vec3 pos — used in POST_VIEW (world-space tris)
_stipple_shader_2d_cache = None   # vec2 pos — used in POST_PIXEL (pixel-space tris)

_STIPPLE_FRAG_SRC = (
    'void main() {\n'
    '    float px = floor(gl_FragCoord.x);\n'
    '    float py = floor(gl_FragCoord.y);\n'
    '    if (mod(px + py, 2.0) < 1.0) discard;\n'
    '    fragColor = ucolor;\n'
    '}\n'
)
_STIPPLE_VERT_3D_SRC = (
    'void main() {\n'
    '    gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0);\n'
    '}\n'
)
_STIPPLE_VERT_2D_SRC = (
    'void main() {\n'
    '    gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);\n'
    '}\n'
)


def _make_stipple_shader(mode_2d):
    """Build stipple shader via GPUShaderCreateInfo (Blender 4+/5)."""
    try:
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', 'ModelViewProjectionMatrix')
        info.push_constant('VEC4', 'ucolor')
        if mode_2d:
            info.vertex_in(0, 'VEC2', 'pos')
            info.vertex_source(_STIPPLE_VERT_2D_SRC)
        else:
            info.vertex_in(0, 'VEC3', 'pos')
            info.vertex_source(_STIPPLE_VERT_3D_SRC)
        info.fragment_out(0, 'VEC4', 'fragColor')
        info.fragment_source(_STIPPLE_FRAG_SRC)
        return gpu.shader.create_from_info(info)
    except Exception as e:
        print(f"[modokit] stipple shader failed: {e}")
        return None


def _get_stipple_shader(mode_2d=False):
    """Return (and lazily create) the checkerboard stipple shader."""
    global _stipple_shader_3d_cache, _stipple_shader_2d_cache
    if mode_2d:
        if _stipple_shader_2d_cache is None:
            _stipple_shader_2d_cache = _make_stipple_shader(mode_2d=True)
        return _stipple_shader_2d_cache
    else:
        if _stipple_shader_3d_cache is None:
            _stipple_shader_3d_cache = _make_stipple_shader(mode_2d=False)
        return _stipple_shader_3d_cache


def _nudge_toward_camera(coords, rv3d, scale=1.0):
    """Offset world-space coords slightly toward the camera along the VIEW direction.

    Offsetting along the view vector (not toward the camera point) ensures the
    depth-buffer shift is the same regardless of the angle between the surface
    and the camera — preventing silhouette bleed where a world-space nudge can
    push a vertex past the surface boundary and become visible behind the mesh.
    scale can be increased for coplanar geometry (e.g. object wireframe).

    The nudge is scaled per-vertex by its view-space depth so that the resulting
    NDC depth shift stays roughly constant at any distance.  Without this, a
    fixed world-space offset shrinks to nothing in NDC at large distances and
    the highlight z-fights against the wireframe overlay."""
    if rv3d is None:
        return coords
    # View direction in world space (unit vector pointing from scene toward camera)
    view_dir = rv3d.view_matrix.inverted_safe().to_3x3() @ Vector((0.0, 0.0, 1.0))
    view_dir.normalize()
    view_mat = rv3d.view_matrix
    result = []
    for co in coords:
        v = Vector(co)
        # Scale nudge linearly with view-space depth so the NDC offset is
        # roughly distance-invariant (perspective divides by z, so world-space
        # nudge effectiveness falls off linearly with distance).
        z_view = abs((view_mat @ v).z)
        dist_factor = max(1.0, z_view / 5.0)
        nudge = 0.0020 * scale * dist_factor
        result.append(tuple(v + view_dir * nudge))
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_prefs(context):
    try:
        return get_addon_preferences(context)
    except Exception:
        return None


def _hover_color(prefs):
    """RGBA for an unselected-element hover."""
    try:
        c = prefs.preselect_color
        a = prefs.preselect_alpha
        return (c[0], c[1], c[2], a)
    except Exception:
        return (0.549, 0.710, 0.780, 0.75)


def _selected_hover_color(context):
    """Lightened version of the theme's face/vert select color for hover-on-selected."""
    try:
        t = context.preferences.themes[0].view_3d
        sc = t.face_select
        # Blend toward white by ~40% to signal hover without changing hue.
        r = min(1.0, sc.r + (1.0 - sc.r) * 0.45)
        g = min(1.0, sc.g + (1.0 - sc.g) * 0.45)
        b = min(1.0, sc.b + (1.0 - sc.b) * 0.45)
        return (r, g, b, 0.45)
    except Exception:
        return (1.0, 0.75, 0.35, 0.45)


def _iter_view3d_areas(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                yield area


def _iter_image_editor_areas(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                yield area


def _is_transforming(context):
    """Return True while a transform/extrude-move modal or viewport navigation modal is running.
    modal_operators uses internal RNA names (TRANSFORM_OT_translate, not transform.translate)."""
    try:
        wm = bpy.context.window_manager
        for window in wm.windows:
            for op in window.modal_operators:
                idname = op.bl_idname
                if (idname.startswith('TRANSFORM_OT_')
                        or idname == 'MESH_OT_extrude_region_move'
                        or idname in ('VIEW3D_OT_rotate', 'VIEW3D_OT_move',
                                      'VIEW3D_OT_zoom', 'VIEW3D_OT_dolly',
                                      'VIEW3D_OT_view_roll',
                                      'IMAGE_OT_view_pan', 'IMAGE_OT_view_zoom')):
                    return True
    except Exception:
        pass
    return False


# ── Hit collection — Edit Mode ────────────────────────────────────────────────

def _collect_edit_hits(context, mx, my):
    """Return a list of hit dicts for all elements within pixel threshold
    at screen position (mx, my).  Supports Face / Edge / Vert modes and
    accumulates overlapping candidates instead of stopping at the first.
    """
    region = context.region
    rv3d   = context.region_data
    if region is None or rv3d is None:
        return []

    coord       = (mx, my)
    view_vec    = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    ray_origin  = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    sm          = context.tool_settings.mesh_select_mode
    # Material mode always highlights faces regardless of the active select mode.
    _mat_active = state._material_mode_active
    face_mode   = sm[2] or _mat_active
    edge_mode   = sm[1] and not _mat_active
    vert_mode   = sm[0] and not _mat_active

    hits = []

    edit_objects = [o for o in context.objects_in_mode_unique_data
                    if o.type == 'MESH' and o.mode == 'EDIT']

    for obj in edit_objects:
        bm_obj = bmesh.from_edit_mesh(obj.data)
        bm_obj.verts.ensure_lookup_table()
        bm_obj.edges.ensure_lookup_table()
        bm_obj.faces.ensure_lookup_table()

        if not bm_obj.faces:
            continue

        mx_w    = obj.matrix_world
        mx_inv  = mx_w.inverted()
        ro_loc  = mx_inv @ ray_origin
        rd_loc  = (mx_inv.to_3x3() @ view_vec).normalized()

        # ── Vert mode — no BVH gate needed ───────────────────────────────────
        # Requiring a BVH hit shrinks the detection zone to the face interior,
        # so vertices at silhouette edges/corners are only highlighted when the
        # cursor is over the face — not when it's near the projected vertex itself.
        # Instead, scan all verts directly using screen-space proximity +
        # front-face normal culling.
        if vert_mode:
            mx_w_3x3 = mx_w.to_3x3()
            vert_candidates = []
            for vert in bm_obj.verts:
                if not any(
                    (mx_w_3x3 @ f.normal).normalized().dot(view_vec) < 0
                    for f in vert.link_faces
                ):
                    continue
                vw  = mx_w @ vert.co
                sc  = view3d_utils.location_3d_to_region_2d(region, rv3d, vw)
                if sc is None:
                    continue
                sdist = math.hypot(sc.x - mx, sc.y - my)
                if sdist <= _VERT_PIXEL_THRESHOLD:
                    vert_candidates.append((sdist, {
                        'type': 'VERT',
                        'coords': [tuple(vw)],
                        'selected': vert.select,
                        'obj': obj,
                        'vert_index': vert.index,
                    }))
            vert_candidates.sort(key=lambda x: x[0])
            hits.extend(h for _, h in vert_candidates)
            continue   # skip BVH + face/edge branches for this object

        bvh     = _get_cached_bvh(obj, bm_obj)
        loc_local, _normal, face_index, _dist = bvh.ray_cast(ro_loc, rd_loc)
        bvh_hit = (loc_local is not None
                   and face_index is not None
                   and face_index < len(bm_obj.faces))

        # Edge mode: when BVH misses (cursor near an edge of a topologically
        # isolated face but not over its interior) fall back to a full
        # screen-space scan — same approach vert mode uses above.
        if not bvh_hit and edge_mode:
            mx_w_3x3 = mx_w.to_3x3()
            edge_candidates = []
            for edge in bm_obj.edges:
                if not any(
                    (mx_w_3x3 @ f.normal).normalized().dot(view_vec) < 0
                    for f in edge.link_faces
                ):
                    continue
                v0w = mx_w @ edge.verts[0].co
                v1w = mx_w @ edge.verts[1].co
                sc0 = view3d_utils.location_3d_to_region_2d(region, rv3d, v0w)
                sc1 = view3d_utils.location_3d_to_region_2d(region, rv3d, v1w)
                if sc0 is None or sc1 is None:
                    continue
                sdist = _point_to_segment_2d((mx, my), sc0, sc1)
                near_a = math.hypot(sc0.x - mx, sc0.y - my) <= _VERT_PIXEL_THRESHOLD
                near_b = math.hypot(sc1.x - mx, sc1.y - my) <= _VERT_PIXEL_THRESHOLD
                if sdist <= _EDGE_PIXEL_THRESHOLD or near_a or near_b:
                    edge_candidates.append((sdist, {
                        'type': 'EDGE',
                        'coords': [tuple(v0w), tuple(v1w)],
                        'selected': edge.select,
                        'obj': obj,
                        'edge_index': edge.index,
                    }))
            edge_candidates.sort(key=lambda x: x[0])
            hits.extend(h for _, h in edge_candidates)
            continue

        if not bvh_hit:
            continue

        location   = mx_w @ loc_local

        # ── Face mode ────────────────────────────────────────────────────────
        if face_mode:
            # Collect hit face + all faces sharing any vert of the hit face (vertex 1-ring).
            # Highlight a face if the cursor is within _EDGE_PIXEL_THRESHOLD of any of its
            # projected vertices — mirrors how edges highlight when near a shared vertex.
            hit_face = bm_obj.faces[face_index]
            ring_faces = {hit_face}
            for v in hit_face.verts:
                ring_faces.update(v.link_faces)
            for face in ring_faces:
                near = False
                for v in face.verts:
                    vsc = view3d_utils.location_3d_to_region_2d(region, rv3d, mx_w @ v.co)
                    if vsc is not None and math.hypot(vsc.x - mx, vsc.y - my) <= _EDGE_PIXEL_THRESHOLD:
                        near = True
                        break
                if not near:
                    continue
                coords = [tuple(mx_w @ v.co) for v in face.verts]
                hits.append({
                    'type': 'FACE',
                    'coords': coords,
                    'selected': face.select,
                    'obj': obj,
                    'face_index': face.index,
                })
            # Supplementary pass: catch topologically isolated faces (e.g. cut-pasted
            # islands) whose vertices happen to project near the cursor but have no
            # topological connection to the BVH hit face.
            seen_ring = {h['face_index'] for h in hits}
            mx_w_3x3 = mx_w.to_3x3()
            for face in bm_obj.faces:
                if face.index in seen_ring:
                    continue
                # Front-face cull
                if (mx_w_3x3 @ face.normal).normalized().dot(view_vec) >= 0:
                    continue
                for v in face.verts:
                    vsc = view3d_utils.location_3d_to_region_2d(region, rv3d, mx_w @ v.co)
                    if vsc is not None and math.hypot(vsc.x - mx, vsc.y - my) <= _EDGE_PIXEL_THRESHOLD:
                        coords = [tuple(mx_w @ fv.co) for fv in face.verts]
                        hits.append({
                            'type': 'FACE',
                            'coords': coords,
                            'selected': face.select,
                            'obj': obj,
                            'face_index': face.index,
                        })
                        break
            # Always push at least the BVH-hit face so there's always feedback
            if not hits:
                face = bm_obj.faces[face_index]
                coords = [tuple(mx_w @ v.co) for v in face.verts]
                hits.append({
                    'type': 'FACE',
                    'coords': coords,
                    'selected': face.select,
                    'obj': obj,
                    'face_index': face.index,
                })

        # ── Edge mode ────────────────────────────────────────────────────────
        elif edge_mode:
            # Only consider edges on the BVH hit face + 1-ring neighbours.
            # Sorted by screen distance so the closest edge is always first.
            hit_face = bm_obj.faces[face_index]
            ring_edges = set(hit_face.edges)
            # Expand via vertex 1-ring (not just edge 1-ring) so edges belonging
            # to faces that share only a vertex with the hit face are included.
            # This matches face mode's ring_faces expansion and fixes the
            # "3 of 4 edges near a vertex" miss.
            for v in hit_face.verts:
                for f in v.link_faces:
                    ring_edges.update(f.edges)
            mx_w_3x3 = mx_w.to_3x3()
            edge_candidates = []
            if _EDGE_DEBUG:
                print(f"[EDGE-DBG] BVH hit face={face_index}  ring_edges={len(ring_edges)}  cursor=({mx},{my})")
            for edge in ring_edges:
                # Only show edge if at least one adjacent face is front-facing
                front_facing = any(
                    (mx_w_3x3 @ f.normal).normalized().dot(view_vec) < 0
                    for f in edge.link_faces
                )
                if not front_facing:
                    if _EDGE_DEBUG:
                        print(f"  edge {edge.index}: SKIP (back-facing)")
                    continue
                v0w = mx_w @ edge.verts[0].co
                v1w = mx_w @ edge.verts[1].co
                sc0 = view3d_utils.location_3d_to_region_2d(region, rv3d, v0w)
                sc1 = view3d_utils.location_3d_to_region_2d(region, rv3d, v1w)
                if sc0 is None or sc1 is None:
                    if _EDGE_DEBUG:
                        print(f"  edge {edge.index}: SKIP (off-screen  sc0={sc0} sc1={sc1})")
                    continue
                sdist = _point_to_segment_2d((mx, my), sc0, sc1)
                near_a = math.hypot(sc0.x - mx, sc0.y - my) <= _VERT_PIXEL_THRESHOLD
                near_b = math.hypot(sc1.x - mx, sc1.y - my) <= _VERT_PIXEL_THRESHOLD
                da = math.hypot(sc0.x - mx, sc0.y - my)
                db = math.hypot(sc1.x - mx, sc1.y - my)
                passed = sdist <= _EDGE_PIXEL_THRESHOLD or near_a or near_b
                if _EDGE_DEBUG:
                    print(f"  edge {edge.index}: sdist={sdist:.1f} da={da:.1f} db={db:.1f} "
                          f"near_a={near_a} near_b={near_b} -> {'HIT' if passed else 'miss'}")
                if passed:
                    edge_candidates.append((sdist, {
                        'type': 'EDGE',
                        'coords': [tuple(v0w), tuple(v1w)],
                        'selected': edge.select,
                        'obj': obj,
                        'edge_index': edge.index,
                    }))
            if _EDGE_DEBUG:
                print(f"  => {len(edge_candidates)} candidate(s) from ring")

            # Supplementary pass: catch topologically isolated edges (e.g. a
            # cut-pasted face island) that share a screen-space vertex position
            # with the ring but have no topological connection to the BVH hit face.
            # Only uses endpoint proximity — no segment distance check needed.
            seen_ring = {c[1]['edge_index'] for c in edge_candidates}
            for edge in bm_obj.edges:
                if edge.index in seen_ring:
                    continue
                if not any(
                    (mx_w_3x3 @ f.normal).normalized().dot(view_vec) < 0
                    for f in edge.link_faces
                ):
                    continue
                v0w = mx_w @ edge.verts[0].co
                v1w = mx_w @ edge.verts[1].co
                sc0 = view3d_utils.location_3d_to_region_2d(region, rv3d, v0w)
                sc1 = view3d_utils.location_3d_to_region_2d(region, rv3d, v1w)
                if sc0 is None or sc1 is None:
                    continue
                near_a = math.hypot(sc0.x - mx, sc0.y - my) <= _VERT_PIXEL_THRESHOLD
                near_b = math.hypot(sc1.x - mx, sc1.y - my) <= _VERT_PIXEL_THRESHOLD
                if near_a or near_b:
                    sdist = _point_to_segment_2d((mx, my), sc0, sc1)
                    if _EDGE_DEBUG:
                        print(f"  edge {edge.index} (isolated): sdist={sdist:.1f} "
                              f"da={math.hypot(sc0.x-mx,sc0.y-my):.1f} "
                              f"db={math.hypot(sc1.x-mx,sc1.y-my):.1f} -> HIT")
                    edge_candidates.append((sdist, {
                        'type': 'EDGE',
                        'coords': [tuple(v0w), tuple(v1w)],
                        'selected': edge.select,
                        'obj': obj,
                        'edge_index': edge.index,
                    }))

            if _EDGE_DEBUG:
                print(f"  => {len(edge_candidates)} total candidate(s)")
            edge_candidates.sort(key=lambda x: x[0])
            hits.extend(h for _, h in edge_candidates)

    return hits


# ── Hit collection — Object Mode ──────────────────────────────────────────────

def _collect_object_hits(context, mx, my):
    """Return edge-coord data for the mesh object under the cursor."""
    region = context.region
    rv3d   = context.region_data
    if region is None or rv3d is None:
        return []

    # Use Blender's own over_find for object picking — no BVH needed.
    result = context.scene.ray_cast(
        context.view_layer.depsgraph,
        view3d_utils.region_2d_to_origin_3d(region, rv3d, (mx, my)),
        view3d_utils.region_2d_to_vector_3d(region, rv3d, (mx, my)),
    )
    hit, _loc, _norm, _fi, obj, _mx = result
    if not hit or obj is None or obj.type != 'MESH':
        return []

    mx_w = obj.matrix_world

    # Use bmesh to collect all edges of the object.
    import bmesh as _bmesh
    bm = _bmesh.new()
    bm.from_mesh(obj.data)
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    edges = []
    for edge in bm.edges:
        v0 = mx_w @ edge.verts[0].co
        v1 = mx_w @ edge.verts[1].co
        edges.append((tuple(v0), tuple(v1)))
    bm.free()

    return [{'type': 'OBJECT', 'obj': obj, 'edge_coords': edges,
             'selected': obj.select_get()}]


# ── UV coordinate builder ─────────────────────────────────────────────────────

def _build_uv_hit(context, hit):
    """Given a 3D hit dict, return a UV hit dict or None.

    Rules:
    - UV sync ON  → always build UV hit (hover color)
    - UV sync OFF → only if mesh element is selected (lightened sel color)
    - Object Mode / no UV map → None
    """
    if hit['type'] == 'OBJECT':
        return None

    obj = hit.get('obj')
    if obj is None or obj.type != 'MESH':
        return None

    ts       = context.tool_settings
    sync_on  = ts.use_uv_select_sync

    selected = hit['selected']
    if not sync_on and not selected:
        return None   # UVs not visible in UV editor when sync OFF + unselected

    try:
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            return None

        elem_type = hit['type']

        if elem_type == 'FACE':
            fi = hit.get('face_index')
            if fi is None or fi >= len(bm.faces):
                return None
            bm.faces.ensure_lookup_table()
            face   = bm.faces[fi]
            coords = [tuple(loop[uv_layer].uv) for loop in face.loops]
            return {'type': 'FACE', 'coords': coords, 'selected': selected}

        elif elem_type == 'EDGE':
            ei = hit.get('edge_index')
            if ei is None or ei >= len(bm.edges):
                return None
            bm.edges.ensure_lookup_table()
            edge = bm.edges[ei]
            # Each edge loop has two half-edges → collect both UV endpoint pairs
            seg_pairs = []
            for loop in edge.link_loops:
                uv_a = tuple(loop[uv_layer].uv)
                uv_b = tuple(loop.link_loop_next[uv_layer].uv)
                seg_pairs.append((uv_a, uv_b))
            return {'type': 'EDGE', 'coords': seg_pairs, 'selected': selected}

        elif elem_type == 'VERT':
            vi = hit.get('vert_index')
            if vi is None or vi >= len(bm.verts):
                return None
            bm.verts.ensure_lookup_table()
            vert   = bm.verts[vi]
            coords = [tuple(loop[uv_layer].uv) for loop in vert.link_loops]
            return {'type': 'VERT', 'coords': coords, 'selected': selected}

    except Exception:
        pass
    return None


# ── Hit collection — UV Editor hover ─────────────────────────────────────────

def _collect_uv_hits(context, mx, my):
    """Find 3D mesh elements whose UV coordinates are near (mx, my) in region pixels.

    Called when MOUSEMOVE fires in the Image Editor.  Builds the same hit-dict
    format as _collect_edit_hits so the existing 3D + UV draw callbacks work
    unchanged.  Fires whenever in EDIT_MESH mode regardless of UV sync.
    """
    mode = getattr(context, 'mode', '')
    if _PRESELECT_DEBUG:
        print(f"[preselect UV] _collect_uv_hits called  mode={mode!r}  pos=({mx},{my})")
    if mode != 'EDIT_MESH':
        if _PRESELECT_DEBUG:
            print(f"[preselect UV] early-exit: mode is {mode!r}, need EDIT_MESH")
        return []

    ts = context.tool_settings

    area   = getattr(context, 'area', None)
    region = getattr(context, 'region', None)
    area_type = area.type if area else None
    if _PRESELECT_DEBUG:
        print(f"[preselect UV] area={area_type!r}  region={region!r}")
    if area is None or region is None or area.type != 'IMAGE_EDITOR':
        return []

    sima = None
    for sp in area.spaces:
        if sp.type == 'IMAGE_EDITOR':
            sima = sp
            break
    if sima is None:
        return []

    from .uv_overlays import _uv_view_to_region, _circle_touches_polygon
    sm        = ts.mesh_select_mode
    # UV material mode always highlights faces regardless of the active select mode.
    _uv_mat = state._uv_material_mode_active
    face_mode = sm[2] or _uv_mat
    edge_mode = sm[1] and not _uv_mat
    vert_mode = sm[0] and not _uv_mat

    hits = []
    try:
        edit_objects = [o for o in context.objects_in_mode_unique_data
                        if o.type == 'MESH' and o.mode == 'EDIT']
    except AttributeError:
        ob = getattr(context, 'edit_object', None)
        edit_objects = [ob] if ob and ob.type == 'MESH' else []
    if _PRESELECT_DEBUG:
        print(f"[preselect UV] edit_objects={[o.name for o in edit_objects]}  "
              f"face={face_mode} edge={edge_mode} vert={vert_mode}")

    for obj in edit_objects:
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            continue
        bm.faces.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        mx_w = obj.matrix_world

        if face_mode:
            for face in bm.faces:
                # Project each loop's UV to screen
                screen_pts = []
                ok = True
                for loop in face.loops:
                    uv = loop[uv_layer].uv
                    sc = _uv_view_to_region(region, sima, uv.x, uv.y)
                    if sc is None:
                        ok = False
                        break
                    screen_pts.append(sc)
                if not ok or len(screen_pts) < 3:
                    continue
                # Highlight if the click-select brush circle would touch this face.
                # Uses the same radius formula as IMAGE_OT_modo_uv_paint_selection._paint
                # so the highlight exactly matches what a click will select.
                prefs = get_addon_preferences(context)
                _face_radius = (prefs.paint_selection_size if prefs else 50) / 4
                if not _circle_touches_polygon(mx, my, _face_radius, screen_pts):
                    continue
                coords = [tuple(mx_w @ v.co) for v in face.verts]
                uv_coords = [tuple(loop[uv_layer].uv) for loop in face.loops]
                hits.append({
                    'type':       'FACE',
                    'coords':     coords,
                    'selected':   face.select,
                    'obj':        obj,
                    'face_index': face.index,
                    '_uv': {'type': 'FACE', 'coords': uv_coords, 'selected': face.select},
                })

        elif edge_mode:
            class _V:  # lightweight vec2 duck-type for _point_to_segment_2d
                def __init__(self, x, y): self.x = x; self.y = y
            # Deduplicate by UV endpoint pair — two loops sharing the same 3D edge
            # but different UV positions (seam) are distinct visual edges and must
            # both be checked independently.
            seen_uv_edges = set()
            for face in bm.faces:
                for loop in face.loops:
                    edge = loop.edge
                    uv_a = loop[uv_layer].uv
                    uv_b = loop.link_loop_next[uv_layer].uv
                    uv_key = (round(uv_a.x, 5), round(uv_a.y, 5),
                              round(uv_b.x, 5), round(uv_b.y, 5))
                    uv_key_rev = (uv_key[2], uv_key[3], uv_key[0], uv_key[1])
                    if uv_key in seen_uv_edges or uv_key_rev in seen_uv_edges:
                        continue
                    sa = _uv_view_to_region(region, sima, uv_a.x, uv_a.y)
                    sb = _uv_view_to_region(region, sima, uv_b.x, uv_b.y)
                    if sa is None or sb is None:
                        continue
                    sdist = _point_to_segment_2d((mx, my), _V(*sa), _V(*sb))
                    near_a = math.hypot(sa[0] - mx, sa[1] - my) <= _VERT_PIXEL_THRESHOLD
                    near_b = math.hypot(sb[0] - mx, sb[1] - my) <= _VERT_PIXEL_THRESHOLD
                    if sdist <= _EDGE_PIXEL_THRESHOLD or near_a or near_b:
                        v0w = tuple(mx_w @ edge.verts[0].co)
                        v1w = tuple(mx_w @ edge.verts[1].co)
                        hits.append({
                            'type':       'EDGE',
                            'coords':     [v0w, v1w],
                            'selected':   edge.select,
                            'obj':        obj,
                            'edge_index': edge.index,
                            '_uv': {'type': 'EDGE', 'coords': [(tuple(uv_a), tuple(uv_b))], 'selected': edge.select},
                        })
                        seen_uv_edges.add(uv_key)

        elif vert_mode:
            for vert in bm.verts:
                for loop in vert.link_loops:
                    uv = loop[uv_layer].uv
                    sc = _uv_view_to_region(region, sima, uv.x, uv.y)
                    if sc is None:
                        continue
                    if math.hypot(sc[0] - mx, sc[1] - my) <= _VERT_PIXEL_THRESHOLD:
                        vw = tuple(mx_w @ vert.co)
                        uv_coords = [tuple(uv)]
                        hits.append({
                            'type':       'VERT',
                            'coords':     [vw],
                            'selected':   vert.select,
                            'obj':        obj,
                            'vert_index': vert.index,
                            '_uv': {'type': 'VERT', 'coords': uv_coords, 'selected': vert.select},
                        })
                        break

    if _PRESELECT_DEBUG:
        print(f"[preselect UV] _collect_uv_hits returning {len(hits)} hit(s)")
    return hits

def _point_in_polygon_2d(pt, verts):
    """Ray-casting point-in-polygon test for screen-space coordinates.
    *verts* may be plain (x, y) tuples or objects with .x/.y attributes."""
    x, y   = pt
    n      = len(verts)
    inside = False
    j      = n - 1
    for i in range(n):
        vi = verts[i]
        vj = verts[j]
        xi, yi = (vi[0], vi[1]) if not hasattr(vi, 'x') else (vi.x, vi.y)
        xj, yj = (vj[0], vj[1]) if not hasattr(vj, 'x') else (vj.x, vj.y)
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def _point_to_segment_2d(pt, a, b):
    """Minimum distance from *pt* to segment (a, b) in 2D."""
    ax, ay = a.x, a.y
    bx, by = b.x, b.y
    px, py = pt
    dx, dy = bx - ax, by - ay
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy + 1e-9)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


# ── UV coordinate → region pixel conversion ───────────────────────────────────

def _uv_to_region(area, uv):
    """Convert UV (u, v) to region pixel (x, y) for a given IMAGE_EDITOR area."""
    for region in area.regions:
        if region.type == 'WINDOW':
            sima = None
            for space in area.spaces:
                if space.type == 'IMAGE_EDITOR':
                    sima = space
                    break
            if sima is None:
                return None
            try:
                from .uv_overlays import _uv_view_to_region
                return _uv_view_to_region(region, sima, uv[0], uv[1])
            except Exception:
                return None
    return None


# ── 3D draw callback ──────────────────────────────────────────────────────────

def _preselect_draw_3d():
    try:
        _preselect_draw_3d_inner()
    except Exception:
        pass


def _preselect_draw_3d_inner():
    if not state._preselect_hits:
        return

    try:
        context = bpy.context
        area    = getattr(context, 'area', None)
        if area is None or area.type != 'VIEW_3D':
            return
    except Exception:
        return

    if _is_transforming(context):
        return

    rv3d   = getattr(context, 'region_data', None)
    region = getattr(context, 'region', None)
    if region is None or rv3d is None:
        return

    prefs = _get_prefs(context)
    if prefs is not None and not prefs.enable_preselect_highlight:
        return

    # When a Modo transform tool is active (W/E/R), suppress face highlight
    # entirely — user is manipulating, not selecting.
    if state._active_transform_mode is not None:
        return

    hover_col   = _hover_color(prefs)
    hover_solid = (hover_col[0], hover_col[1], hover_col[2], 1.0)
    sel_col     = _selected_hover_color(context)
    sel_solid   = (sel_col[0], sel_col[1], sel_col[2], 1.0)
    shader      = gpu.shader.from_builtin('UNIFORM_COLOR')

    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.blend_set('ALPHA')

    try:
        for hit in state._preselect_hits:
            htype = hit['type']
            if htype == 'OBJECT':
                color = sel_solid if hit['selected'] else hover_solid
            elif htype == 'FACE':
                color = hover_col
            else:
                continue  # EDGE/VERT drawn in POST_PIXEL callback instead

            if htype == 'OBJECT':
                segs = list(hit['edge_coords'])
                if not segs:
                    continue
                # Nudge all verts toward camera to beat wireframe z-fight
                all_pts = _nudge_toward_camera([p for seg in segs for p in seg], rv3d, scale=1.0)
                segs = [(all_pts[i*2], all_pts[i*2+1]) for i in range(len(segs))]
                gpu.state.depth_test_set('LESS_EQUAL')
                from .uv_overlays import _get_aa_line_3d_shader, _aa_line_quads_3d
                aa3d = _get_aa_line_3d_shader()
                hw = 1.0
                vp = (float(region.width), float(region.height)) if region else (1920.0, 1080.0)
                if aa3d is not None:
                    pos0_l, pos1_l, which_l, side_l = _aa_line_quads_3d(segs, hw)
                    b = batch_for_shader(aa3d, 'TRIS',
                        {'pos0': pos0_l, 'pos1': pos1_l, 'which': which_l, 'side': side_l})
                    aa3d.bind()
                    aa3d.uniform_float('ucolor', color)
                    aa3d.uniform_float('uhalf_w', hw)
                    aa3d.uniform_float('uviewport', vp)
                    b.draw(aa3d)
                else:
                    coords = [p for p0, p1 in segs for p in (p0, p1)]
                    gpu.state.line_width_set(1.0)
                    batch = batch_for_shader(shader, 'LINES', {'pos': coords})
                    shader.bind()
                    shader.uniform_float('color', color)
                    batch.draw(shader)
                    gpu.state.line_width_set(1.0)
                gpu.state.depth_test_set('LESS_EQUAL')

            elif htype == 'FACE':
                verts = _nudge_toward_camera(hit['coords'], rv3d)
                if len(verts) < 3:
                    continue
                tris = []
                for i in range(1, len(verts) - 1):
                    tris.extend([verts[0], verts[i], verts[i + 1]])
                gpu.state.depth_test_set('ALWAYS')  # beat wireframe at any distance
                stip = _get_stipple_shader(mode_2d=False)
                if stip is not None:
                    batch = batch_for_shader(stip, 'TRIS', {"pos": tris})
                    stip.bind()
                    stip.uniform_float("ucolor", hover_col)
                    batch.draw(stip)
                else:
                    batch = batch_for_shader(shader, 'TRIS', {"pos": tris})
                    shader.bind()
                    shader.uniform_float("color", hover_col)
                    batch.draw(shader)
                # AA edge outline on top of face fill
                n = len(hit['coords'])
                if n >= 2:
                    from .uv_overlays import _get_aa_line_3d_shader, _aa_line_quads_3d
                    aa3d = _get_aa_line_3d_shader()
                    hw = 1.0
                    vp = (float(region.width), float(region.height)) if region else (1920.0, 1080.0)
                    edge_segs = [(hit['coords'][i], hit['coords'][(i+1) % n]) for i in range(n)]
                    if aa3d is not None:
                        pos0_l, pos1_l, which_l, side_l = _aa_line_quads_3d(edge_segs, hw)
                        b = batch_for_shader(aa3d, 'TRIS',
                            {'pos0': pos0_l, 'pos1': pos1_l, 'which': which_l, 'side': side_l})
                        aa3d.bind()
                        aa3d.uniform_float('ucolor', hover_solid)
                        aa3d.uniform_float('uhalf_w', hw)
                        aa3d.uniform_float('uviewport', vp)
                        b.draw(aa3d)
                    else:
                        coords = [p for seg in edge_segs for p in seg]
                        gpu.state.line_width_set(1.0)
                        batch = batch_for_shader(shader, 'LINES', {'pos': coords})
                        shader.bind()
                        shader.uniform_float('color', hover_solid)
                        batch.draw(shader)
    finally:
        gpu.state.depth_mask_set(True)
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.blend_set('NONE')


# ── 3D POST_PIXEL draw callback — edges + verts (always on top of overlays) ──

def _preselect_draw_3d_px():
    try:
        _preselect_draw_3d_px_inner()
    except Exception:
        pass


def _preselect_draw_3d_px_inner():
    if not state._preselect_hits:
        return
    try:
        context = bpy.context
        area    = getattr(context, 'area', None)
        if area is None or area.type != 'VIEW_3D':
            return
    except Exception:
        return

    if _is_transforming(context):
        if state._preselect_hits:
            state._preselect_hits = []
        return

    rv3d   = getattr(context, 'region_data', None)
    region = getattr(context, 'region', None)
    if region is None or rv3d is None:
        return

    prefs = _get_prefs(context)
    if prefs is not None and not prefs.enable_preselect_highlight:
        return

    # When a Modo transform tool is active, suppress edges — keep verts for snapping.
    _transform_active = state._active_transform_mode is not None

    hover_solid = tuple(list(_hover_color(prefs)[:3]) + [1.0])
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    try:
        for hit in state._preselect_hits:
            htype = hit['type']
            if htype not in ('EDGE', 'VERT'):
                continue
            # Skip edges when transform tool is active; keep verts for snap targets
            if _transform_active and htype == 'EDGE':
                continue
            color = hover_solid

            if htype == 'EDGE':
                pts = []
                for co in hit['coords']:
                    sc = view3d_utils.location_3d_to_region_2d(region, rv3d, Vector(co))
                    if sc is None:
                        pts = []
                        break
                    pts.append((sc.x, sc.y))
                if len(pts) != 2:
                    continue
                from .uv_overlays import _get_aa_line_shader, _aa_line_quads
                aa = _get_aa_line_shader()
                hw = 1.25
                if aa is not None:
                    pos, t_vals = _aa_line_quads([(pts[0], pts[1])], hw)
                    aa.bind()
                    aa.uniform_float('ucolor', color)
                    aa.uniform_float('uhalf_w', hw)
                    batch_for_shader(aa, 'TRIS', {'pos': pos, 't': t_vals}).draw(aa)
                else:
                    batch = batch_for_shader(shader, 'LINES', {'pos': pts})
                    gpu.state.line_width_set(3.0)
                    shader.bind()
                    shader.uniform_float('color', color)
                    batch.draw(shader)
                    gpu.state.line_width_set(1.0)

            elif htype == 'VERT':
                if not hit['coords']:
                    continue
                sc = view3d_utils.location_3d_to_region_2d(region, rv3d, Vector(hit['coords'][0]))
                if sc is None:
                    continue
                r = _POINT_SIZE / 2.0
                px, py = sc.x, sc.y
                quad = [
                    (px - r, py - r), (px + r, py - r), (px + r, py + r),
                    (px - r, py - r), (px + r, py + r), (px - r, py + r),
                ]
                batch = batch_for_shader(shader, 'TRIS', {"pos": quad})
                shader.bind()
                shader.uniform_float("color", color)
                batch.draw(shader)
    finally:
        gpu.state.blend_set('NONE')


# ── UV draw callback ──────────────────────────────────────────────────────────

def _preselect_draw_uv():
    try:
        _preselect_draw_uv_inner()
    except Exception:
        pass


def _preselect_draw_uv_inner():
    if not state._preselect_hits:
        return
    try:
        context = bpy.context
        area    = getattr(context, 'area', None)
        if area is None or area.type != 'IMAGE_EDITOR':
            return
    except Exception:
        return

    if _is_transforming(context) or state._uv_lmb_down or state._uv_active_transform_mode is not None:
        if state._preselect_hits:
            state._preselect_hits = []
        return

    prefs       = _get_prefs(context)
    if prefs is not None and not prefs.enable_preselect_highlight:
        return
    hover_col   = _hover_color(prefs)
    hover_solid = (hover_col[0], hover_col[1], hover_col[2], 1.0)
    sel_col     = _selected_hover_color(context)
    shader      = gpu.shader.from_builtin('UNIFORM_COLOR')

    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')

    try:
        for hit in state._preselect_hits:
            uv_hit = hit.get('_uv')
            if uv_hit is None:
                continue
            utype = uv_hit['type']
            # Always use hover color regardless of selection state —
            # matches 3D view behaviour where selected elements still show hover blue.
            if utype == 'FACE':
                color = hover_col
            else:
                color = hover_solid

            try:
                area_cur = context.area
                if area_cur is None:
                    continue
                sima = None
                for sp in area_cur.spaces:
                    if sp.type == 'IMAGE_EDITOR':
                        sima = sp
                        break
                region = context.region
                if sima is None or region is None:
                    continue
                from .uv_overlays import _uv_view_to_region

                if utype == 'FACE':
                    px_verts = [_uv_view_to_region(region, sima, u, v)
                                for (u, v) in uv_hit['coords']]
                    if any(p is None for p in px_verts):
                        continue
                    tris = []
                    for i in range(1, len(px_verts) - 1):
                        tris.extend([
                            (px_verts[0][0],   px_verts[0][1]),
                            (px_verts[i][0],   px_verts[i][1]),
                            (px_verts[i+1][0], px_verts[i+1][1]),
                        ])
                    if not tris:
                        continue
                    stip = _get_stipple_shader(mode_2d=True)
                    if stip is not None:
                        batch = batch_for_shader(stip, 'TRIS', {"pos": tris})
                        stip.bind()
                        stip.uniform_float("ucolor", hover_col)
                        batch.draw(stip)
                    else:
                        batch = batch_for_shader(shader, 'TRIS', {"pos": tris})
                        shader.bind()
                        shader.uniform_float("color", hover_col)
                        batch.draw(shader)

                    # AA perimeter edge outline
                    n = len(px_verts)
                    if n >= 2:
                        from .uv_overlays import _get_aa_line_shader, _aa_line_quads
                        aa = _get_aa_line_shader()
                        if aa is not None:
                            hw = 1.0
                            edge_segs = [(px_verts[i], px_verts[(i + 1) % n]) for i in range(n)]
                            pos, t_vals = _aa_line_quads(edge_segs, hw)
                            aa.bind()
                            aa.uniform_float('ucolor', hover_col)
                            aa.uniform_float('uhalf_w', hw)
                            batch_for_shader(aa, 'TRIS', {'pos': pos, 't': t_vals}).draw(aa)

                elif utype == 'EDGE':
                    segs = uv_hit['coords']   # list of ((u0,v0),(u1,v1))
                    seg_pts = []
                    for (u0, v0), (u1, v1) in segs:
                        p0 = _uv_view_to_region(region, sima, u0, v0)
                        p1 = _uv_view_to_region(region, sima, u1, v1)
                        if p0 is None or p1 is None:
                            continue
                        seg_pts.append(((p0[0], p0[1]), (p1[0], p1[1])))
                    if not seg_pts:
                        continue
                    from .uv_overlays import _get_aa_line_shader, _aa_line_quads
                    aa = _get_aa_line_shader()
                    hw = 1.25
                    if aa is not None:
                        pos, t_vals = _aa_line_quads(seg_pts, hw)
                        aa.bind()
                        aa.uniform_float('ucolor', color)
                        aa.uniform_float('uhalf_w', hw)
                        batch_for_shader(aa, 'TRIS', {'pos': pos, 't': t_vals}).draw(aa)
                    else:
                        line_pts = [p for seg in seg_pts for p in seg]
                        batch = batch_for_shader(shader, 'LINES', {'pos': line_pts})
                        gpu.state.line_width_set(1.25)
                        shader.bind()
                        shader.uniform_float('color', color)
                        batch.draw(shader)
                        gpu.state.line_width_set(1.0)

                elif utype == 'VERT':
                    pts = []
                    for (u, v) in uv_hit['coords']:
                        p = _uv_view_to_region(region, sima, u, v)
                        if p is not None:
                            pts.append((p[0], p[1]))
                    if not pts:
                        continue
                    r = 4.0
                    tris = []
                    for (px, py) in pts:
                        tris.extend([
                            (px - r, py - r), (px + r, py - r), (px + r, py + r),
                            (px - r, py - r), (px + r, py + r), (px - r, py + r),
                        ])
                    batch = batch_for_shader(shader, 'TRIS', {"pos": tris})
                    shader.bind()
                    shader.uniform_float("color", color)
                    batch.draw(shader)

            except Exception:
                continue
    finally:
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('LESS_EQUAL')


# ── Draw handle management ────────────────────────────────────────────────────

def _start_preselect():
    """Register all draw handlers (idempotent)."""
    if state._preselect_draw_handle_3d is None:
        state._preselect_draw_handle_3d = bpy.types.SpaceView3D.draw_handler_add(
            _preselect_draw_3d, (), 'WINDOW', 'POST_VIEW')
    if state._preselect_draw_handle_3d_px is None:
        state._preselect_draw_handle_3d_px = bpy.types.SpaceView3D.draw_handler_add(
            _preselect_draw_3d_px, (), 'WINDOW', 'POST_PIXEL')
    if state._preselect_draw_handle_uv is None:
        state._preselect_draw_handle_uv = bpy.types.SpaceImageEditor.draw_handler_add(
            _preselect_draw_uv, (), 'WINDOW', 'POST_PIXEL')


def _stop_preselect():
    """Remove all draw handlers and clear hit cache."""
    state._preselect_hits = []
    if state._preselect_draw_handle_3d is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                state._preselect_draw_handle_3d, 'WINDOW')
        except Exception:
            pass
        state._preselect_draw_handle_3d = None
    if state._preselect_draw_handle_3d_px is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                state._preselect_draw_handle_3d_px, 'WINDOW')
        except Exception:
            pass
        state._preselect_draw_handle_3d_px = None
    if state._preselect_draw_handle_uv is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(
                state._preselect_draw_handle_uv, 'WINDOW')
        except Exception:
            pass
        state._preselect_draw_handle_uv = None


# ── Non-modal operator (invoked from MOUSEMOVE keymap entry) ─────────────────

class VIEW3D_OT_modo_preselect_highlight(bpy.types.Operator):
    """Pre-selection highlight: updates hovered geometry on every mouse move.
    Invoked from a MOUSEMOVE keymap entry, returns FINISHED immediately.
    No persistent modal — safe to use with Reload Scripts."""
    bl_idname  = 'view3d.modo_preselect_highlight'
    bl_label   = 'Modo Pre-selection Highlight'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'VIEW_3D'
                and getattr(context, 'mode', '') in ('EDIT_MESH', 'OBJECT'))

    def invoke(self, context, event):
        prefs = _get_prefs(context)
        if prefs is not None and not prefs.enable_preselect_highlight:
            if state._preselect_hits:
                state._preselect_hits = []
                if context.area:
                    context.area.tag_redraw()
            return {'PASS_THROUGH'}

        navigating = _is_transforming(context)
        if navigating:
            if state._preselect_hits:
                state._preselect_hits = []
                if context.area:
                    context.area.tag_redraw()
            return {'PASS_THROUGH'}

        mx   = event.mouse_region_x
        my   = event.mouse_region_y
        mode = getattr(context, 'mode', '')

        if mode == 'EDIT_MESH':
            raw_hits = _collect_edit_hits(context, mx, my)
            for hit in raw_hits:
                hit['_uv'] = _build_uv_hit(context, hit)
        elif mode == 'OBJECT':
            raw_hits = _collect_object_hits(context, mx, my)
        else:
            raw_hits = []

        state._preselect_hits = raw_hits

        if context.area:
            context.area.tag_redraw()
        if raw_hits:
            for area in _iter_image_editor_areas(context):
                area.tag_redraw()

        return {'PASS_THROUGH'}


# ── UV Editor pre-selection operator ─────────────────────────────────────────

class IMAGE_OT_modo_preselect_highlight(bpy.types.Operator):
    """Pre-selection highlight driven from the UV Editor mouse position."""
    bl_idname  = 'image.modo_preselect_highlight'
    bl_label   = 'Modo Pre-selection Highlight (UV)'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and getattr(context, 'mode', '') == 'EDIT_MESH')

    def invoke(self, context, event):
        if _PRESELECT_DEBUG:
            print(f"[preselect UV] IMAGE_OT.invoke  mode={getattr(context,'mode','?')!r}  "
                  f"space={getattr(context.space_data,'type','?')!r}  "
                  f"pos=({event.mouse_region_x},{event.mouse_region_y})")
        prefs = _get_prefs(context)
        if prefs is not None and not prefs.enable_preselect_highlight:
            if state._preselect_hits:
                state._preselect_hits = []
                context.area.tag_redraw()
                for a in _iter_view3d_areas(context):
                    a.tag_redraw()
            return {'PASS_THROUGH'}

        navigating = _is_transforming(context)
        if navigating or state._uv_lmb_down or state._uv_active_transform_mode is not None:
            if state._preselect_hits:
                state._preselect_hits = []
                context.area.tag_redraw()
                for a in _iter_view3d_areas(context):
                    a.tag_redraw()
            return {'PASS_THROUGH'}

        mx = event.mouse_region_x
        my = event.mouse_region_y
        raw_hits = _collect_uv_hits(context, mx, my)
        state._preselect_hits = raw_hits

        context.area.tag_redraw()
        for a in _iter_view3d_areas(context):
            a.tag_redraw()

        return {'PASS_THROUGH'}


class IMAGE_OT_modo_preselect_lmb_track(bpy.types.Operator):
    """Track LMB press/release to suppress preselect highlight during drag-select."""
    bl_idname  = 'image.modo_preselect_lmb_track'
    bl_label   = 'Modo Preselect LMB Track'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (context.region is not None
                and context.region.type == 'WINDOW'
                and context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR')

    def invoke(self, context, event):
        if event.value == 'PRESS':
            state._uv_lmb_down = True
            if state._preselect_hits:
                state._preselect_hits = []
                if context.area:
                    context.area.tag_redraw()
        else:
            state._uv_lmb_down = False
        return {'PASS_THROUGH'}


# ── Depsgraph handler — mode-change cache invalidation ───────────────────────

@bpy.app.handlers.persistent
def _preselect_depsgraph_handler(scene, depsgraph):
    """Clear BVH cache and highlight state when the context mode changes."""
    try:
        context      = bpy.context
        current_mode = getattr(context, 'mode', None)
        if current_mode == state._preselect_mode:
            return
        state._preselect_mode = current_mode
        clear_bvh_cache()
        state._preselect_hits = []
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in ('VIEW_3D', 'IMAGE_EDITOR'):
                    area.tag_redraw()
    except Exception:
        pass
