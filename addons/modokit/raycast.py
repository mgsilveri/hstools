"""
Raycast and mesh topology helpers for Edit Mode selection.
"""

import bpy
import bmesh
import math
from mathutils.bvhtree import BVHTree
from bpy_extras import view3d_utils

from .utils import get_addon_preferences


def raycast_mesh(context, coord, bm=None):
    """Perform raycast at a specific screen coordinate using BVH trees built
    directly from the live BMesh of every object in Edit Mode.

    This avoids the evaluated-depsgraph mesh whose face indices can diverge
    from BMesh indices (especially with modifiers) and which is unreliable at
    certain camera angles in Edit Mode.
    """
    region = context.region
    rv3d   = context.region_data

    prefs = get_addon_preferences(context)
    debug = prefs.debug_raycast

    view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    ray_origin  = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)

    select_mode = context.tool_settings.mesh_select_mode
    mode_name   = ['VERT', 'EDGE', 'FACE'][[select_mode[0], select_mode[1], select_mode[2]].index(True)]
    max_screen_dist = 20

    best_dist = float('inf')
    best_hit  = None

    edit_objects = [o for o in context.objects_in_mode_unique_data
                    if o.type == 'MESH' and o.mode == 'EDIT']

    if debug:
        print(f"\n[ModoSelect] ── raycast_mesh ──────────────────────")
        print(f"  coord={coord}  mode={mode_name}  edit_objects={[o.name for o in edit_objects]}")
        print(f"  ray_origin={tuple(round(v,4) for v in ray_origin)}")
        print(f"  view_vector={tuple(round(v,4) for v in view_vector)}")

    for obj in edit_objects:
        bm_obj = bmesh.from_edit_mesh(obj.data)
        bm_obj.verts.ensure_lookup_table()
        bm_obj.edges.ensure_lookup_table()
        bm_obj.faces.ensure_lookup_table()

        if not bm_obj.faces:
            if debug:
                print(f"  [{obj.name}] skipped – no faces")
            continue

        bvh = BVHTree.FromBMesh(bm_obj)
        mx     = obj.matrix_world
        mx_inv = mx.inverted()
        ray_origin_local = mx_inv @ ray_origin
        ray_dir_local    = (mx_inv.to_3x3() @ view_vector).normalized()

        loc_local, normal_local, face_index, _dist_bvh = bvh.ray_cast(
            ray_origin_local, ray_dir_local
        )

        if debug:
            print(f"  [{obj.name}] BVH hit: loc_local={tuple(round(v,4) for v in loc_local) if loc_local else None}"
                  f"  face_index={face_index}  dist_bvh={round(_dist_bvh,4) if _dist_bvh is not None else None}"
                  f"  total_faces={len(bm_obj.faces)}")

        if loc_local is None or face_index is None:
            continue
        if face_index >= len(bm_obj.faces):
            if debug:
                print(f"  [{obj.name}] face_index {face_index} out of range – skipped")
            continue

        location   = mx @ loc_local
        world_dist = (location - ray_origin).length

        if debug:
            print(f"  [{obj.name}] world_dist={round(world_dist,4)}  best_dist={round(best_dist,4)}")

        if world_dist >= best_dist:
            continue

        # ── Face mode ────────────────────────────────────────────────────────
        if select_mode[2]:
            best_dist = world_dist
            best_hit  = {'location': location, 'normal': normal_local,
                         'index': face_index, 'obj': obj}
            if debug:
                print(f"  [{obj.name}] FACE hit → index={face_index}")

        # ── Edge mode ────────────────────────────────────────────────────────
        elif select_mode[1]:
            hit_face     = bm_obj.faces[face_index]
            closest_edge = None
            min_edge_dist = float('inf')
            for edge in hit_face.edges:
                v1 = mx @ edge.verts[0].co
                v2 = mx @ edge.verts[1].co
                edge_vec  = v2 - v1
                point_vec = location - v1
                edge_len  = edge_vec.length
                if edge_len == 0:
                    continue
                t = max(0.0, min(1.0, point_vec.dot(edge_vec) / (edge_len * edge_len)))
                projection = v1 + t * edge_vec
                dist = (location - projection).length
                if dist >= min_edge_dist:
                    continue
                screen_co = view3d_utils.location_3d_to_region_2d(region, rv3d, projection)
                if screen_co:
                    sdist = math.sqrt((screen_co.x - coord[0])**2 + (screen_co.y - coord[1])**2)
                    if debug:
                        print(f"    edge {edge.index}: 3d_dist={round(dist,4)}  screen_dist={round(sdist,1)}px")
                    if sdist <= max_screen_dist:
                        min_edge_dist = dist
                        closest_edge = edge
            if closest_edge is not None:
                best_dist = world_dist
                best_hit  = {'location': location, 'normal': normal_local,
                             'index': closest_edge.index, 'obj': obj,
                             'hit_face_index': face_index}
                if debug:
                    print(f"  [{obj.name}] EDGE hit → index={closest_edge.index} face={face_index}")
            elif debug:
                print(f"  [{obj.name}] EDGE – no edge within {max_screen_dist}px screen threshold")

        # ── Vertex mode ───────────────────────────────────────────────────────
        elif select_mode[0]:
            hit_face     = bm_obj.faces[face_index]
            closest_vert = None
            min_vert_dist = float('inf')
            for vert in hit_face.verts:
                vert_co = mx @ vert.co
                dist = (location - vert_co).length
                if dist >= min_vert_dist:
                    continue
                screen_co = view3d_utils.location_3d_to_region_2d(region, rv3d, vert_co)
                if screen_co:
                    sdist = math.sqrt((screen_co.x - coord[0])**2 + (screen_co.y - coord[1])**2)
                    if debug:
                        print(f"    vert {vert.index}: 3d_dist={round(dist,4)}  screen_dist={round(sdist,1)}px")
                    if sdist <= max_screen_dist:
                        min_vert_dist = dist
                        closest_vert = vert
            if closest_vert is not None:
                best_dist = world_dist
                best_hit  = {'location': location, 'normal': normal_local,
                             'index': closest_vert.index, 'obj': obj}
                if debug:
                    print(f"  [{obj.name}] VERT hit → index={closest_vert.index}")
            elif debug:
                print(f"  [{obj.name}] VERT – no vertex within {max_screen_dist}px screen threshold")

    if debug:
        print(f"  FINAL result: {best_hit}")
    return best_hit


def collect_edge_loop(seed_edge):
    """Walk the edge loop through seed_edge in both directions.

    Returns a set of all BMEdge in the loop.  Stops at poles, mesh corners,
    or when the loop closes.  Handles boundary vertices (valence 2-3) correctly
    so loops on open mesh borders are selected in full.
    """
    def _walk(start_edge, start_vert):
        current_edge = start_edge
        current_vert = start_vert
        while True:
            linked = current_vert.link_edges
            current_faces = set(current_edge.link_faces)
            candidates = [
                e for e in linked
                if e is not current_edge
                and not any(f in current_faces for f in e.link_faces)
            ]
            if len(candidates) != 1:
                break
            next_edge = candidates[0]
            if next_edge is start_edge:
                break
            yield next_edge
            current_vert = next_edge.other_vert(current_vert)
            current_edge = next_edge

    loop = {seed_edge}
    for vert in seed_edge.verts:
        for e in _walk(seed_edge, vert):
            if e in loop:
                break
            loop.add(e)
    return loop


def collect_edge_loop_modo(seed_edge, preferred_face=None):
    """Walk the edge loop through seed_edge using Modo-style logic.

    - Regular quad topology (valence-4 vertices): behaves identically to
      collect_edge_loop (normal edge-loop traversal).
    - Pole topology (e.g. cube corners, valence-3): both endpoints have no
      standard loop continuation, so the function falls back to selecting the
      perimeter of an adjacent face — replicating Modo's double-click behaviour
      where clicking a cube edge loops around the face.

    preferred_face: BMFace that was raycasted onto when the user clicked.  When
                    provided it is tried first so the loop wraps the face the
                    user was looking at, matching Modo's visual expectation.
    """
    loop = collect_edge_loop(seed_edge)
    if len(loop) > 1:
        return loop

    # Both endpoints are poles — no standard loop continuation found.
    # Fall back to the perimeter of an adjacent face.
    faces = list(seed_edge.link_faces)
    if not faces:
        return loop

    # Honour the hit face (the one the user raycasted onto).
    if preferred_face is not None and preferred_face in faces:
        faces = [preferred_face] + [f for f in faces if f is not preferred_face]

    return set(faces[0].edges)


def select_connected_faces_from(bm, start_face):
    """Flood-fill select all faces reachable from start_face via shared edges.

    Replicates Modo: double-click on a polygon selects the entire island.
    """
    to_visit = [start_face]
    visited = {start_face}
    start_face.select = True
    while to_visit:
        face = to_visit.pop()
        for edge in face.edges:
            for linked_face in edge.link_faces:
                if linked_face not in visited:
                    visited.add(linked_face)
                    linked_face.select = True
                    to_visit.append(linked_face)


def select_connected_verts_from(bm, start_vert):
    """Flood-fill select all vertices reachable from start_vert via edges.

    Replicates Modo: double-click on a vertex selects all connected vertices.
    """
    to_visit = [start_vert]
    visited = {start_vert}
    start_vert.select = True
    while to_visit:
        vert = to_visit.pop()
        for edge in vert.link_edges:
            other = edge.other_vert(vert)
            if other not in visited:
                visited.add(other)
                other.select = True
                to_visit.append(other)


def raycast_with_tolerance(context, coord, tolerance):
    """Raycast with pixel tolerance for 'lazy' selection.

    Replicates Modo's remapping.selectionSize preference.
    Tries exact hit first, then expands outward within *tolerance* pixels.
    """
    result = raycast_mesh(context, coord)
    if result:
        return result
    for offset_x in range(-tolerance, tolerance + 1):
        for offset_y in range(-tolerance, tolerance + 1):
            if offset_x == 0 and offset_y == 0:
                continue
            if math.sqrt(offset_x**2 + offset_y**2) <= tolerance:
                test_coord = (coord[0] + offset_x, coord[1] + offset_y)
                result = raycast_mesh(context, test_coord)
                if result:
                    return result
    return None
