"""uv_selection.py — UV selection operators and helpers.

Contains:
 - IMAGE_OT_modo_uv_stitch          (Shift+S  Move and Sew)
 - Helper functions for UV island/edge-loop/path operations
 - IMAGE_OT_modo_uv_double_click_select  (LMB double-click)
 - IMAGE_OT_modo_uv_shortest_path        (UV Dijkstra path)
 - IMAGE_OT_modo_uv_click_select         (single-click wrapper)
 - IMAGE_OT_modo_uv_paint_selection      (drag-paint modal)
 - IMAGE_OT_modo_uv_lasso_select         (RMB/MMB lasso modal)
"""

import heapq
import math
from collections import deque

import bpy
import bmesh
from bpy.props import EnumProperty, BoolProperty
from mathutils import Vector

from . import state
from .utils import get_addon_preferences, _uv_debug_log
from .uv_overlays import (
    _uv_view_to_region,
    _uv_view_to_region_unclamped,
    _uv_region_to_view,
    _dist_point_to_segment_2d,
    _point_in_poly_2d,
    _circle_touches_polygon,
    _clip_segment_to_rect,
    _point_to_segment_dist,
    _resync_uv_editor_selection,
    _compute_uv_boundary_cache,
    _compute_uv_selection_median,
    _start_uv_boundary_overlay,
    _start_uv_flipped_face_viz,
    _compute_flipped_face_uv_cache,
    _refresh_uv_caches_timer,
    _stop_uv_boundary_overlay,
    _stop_uv_flipped_face_viz,
    _uv_undo_redo_handler,
    _uv_seam_redraw_depsgraph_handler,
)
from .uv_snap import _collect_uv_transform_targets
from .utils import point_in_polygon


# ============================================================================
# IMAGE_OT_modo_uv_stitch  (Modo-style Move and Sew)
# ============================================================================

class IMAGE_OT_modo_uv_stitch(bpy.types.Operator):
    """Modo-style Move and Sew: rigidly moves the source UV island to align
    with the adjacent island along the selected seam edge(s), then welds
    the boundary UV vertices.

    Workflow (mirrors Modo's uv.moveAndSew):
      1. In the UV Editor, select one or more UV edges along a seam.
      2. Press Shift+S — the island containing the selected edges
         transforms (translate + rotate + uniform scale, best-fit) to
         align with the neighbouring island, then the seam UV vertices
         are welded together.
    """
    bl_idname = "image.modo_uv_stitch"
    bl_label = "Modo UV Move and Sew"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.area is not None
            and context.area.type == 'IMAGE_EDITOR'
            and context.edit_object is not None
            and context.edit_object.type == 'MESH'
        )

    @staticmethod
    def _build_selected_uv_positions(bm, uv_layer, tol_digits=5):
        sel_uvs = set()
        for face in bm.faces:
            for loop in face.loops:
                if loop.uv_select_vert:
                    uv = loop[uv_layer].uv
                    sel_uvs.add((round(uv.x, tol_digits),
                                 round(uv.y, tol_digits)))
                edge_sel = False
                try:
                    edge_sel = loop.uv_select_edge
                except AttributeError:
                    try:
                        edge_sel = loop[uv_layer].select_edge
                    except (AttributeError, KeyError):
                        pass
                if edge_sel:
                    uv_a = loop[uv_layer].uv
                    uv_b = loop.link_loop_next[uv_layer].uv
                    sel_uvs.add((round(uv_a.x, tol_digits),
                                 round(uv_a.y, tol_digits)))
                    sel_uvs.add((round(uv_b.x, tol_digits),
                                 round(uv_b.y, tol_digits)))
        return frozenset(sel_uvs)

    @staticmethod
    def _loop_uv_in_set(loop, uv_layer, sel_uv_set, tol_digits=5):
        uv_a = loop[uv_layer].uv
        uv_b = loop.link_loop_next[uv_layer].uv
        return (
            (round(uv_a.x, tol_digits), round(uv_a.y, tol_digits)) in sel_uv_set
            and
            (round(uv_b.x, tol_digits), round(uv_b.y, tol_digits)) in sel_uv_set
        )

    @staticmethod
    def _uv_edges_continuous(la, lb, uv_layer, tol=1e-6):
        uv_a0 = la[uv_layer].uv
        uv_a1 = la.link_loop_next[uv_layer].uv
        uv_b0 = lb[uv_layer].uv
        uv_b1 = lb.link_loop_next[uv_layer].uv
        if la.vert == lb.vert:
            return ((uv_a0 - uv_b0).length < tol and
                    (uv_a1 - uv_b1).length < tol)
        else:
            return ((uv_a0 - uv_b1).length < tol and
                    (uv_a1 - uv_b0).length < tol)

    def _get_uv_island_faces(self, seed_loop, uv_layer, bm):
        island = set()
        queue = deque([seed_loop.face])
        while queue:
            face = queue.popleft()
            if face.index in island:
                continue
            island.add(face.index)
            for loop in face.loops:
                for linked in loop.edge.link_loops:
                    if linked.face.index in island:
                        continue
                    if self._uv_edges_continuous(loop, linked, uv_layer):
                        queue.append(linked.face)
        return island

    @staticmethod
    def _similarity_transform_2d(src_pts, tgt_pts):
        n = len(src_pts)
        assert n >= 1, "Need at least one point pair"
        sc = Vector((sum(p.x for p in src_pts) / n,
                     sum(p.y for p in src_pts) / n))
        tc = Vector((sum(p.x for p in tgt_pts) / n,
                     sum(p.y for p in tgt_pts) / n))
        shat = [p - sc for p in src_pts]
        that = [p - tc for p in tgt_pts]
        denom = sum(p.dot(p) for p in shat)
        if denom < 1e-12:
            t = tc - sc
            return lambda p: p + t
        a = sum(s.dot(t) for s, t in zip(shat, that)) / denom
        b = sum(s.x * t.y - s.y * t.x for s, t in zip(shat, that)) / denom

        def transform(p):
            dx = p.x - sc.x
            dy = p.y - sc.y
            return Vector((a * dx - b * dy + tc.x,
                           b * dx + a * dy + tc.y))
        return transform

    def execute(self, context):
        obj = context.edit_object
        if obj is None or obj.type != 'MESH':
            return {'CANCELLED'}

        bm = bmesh.from_edit_mesh(obj.data)
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.verify()
        if uv_layer is None:
            self.report({'WARNING'}, "No UV layer found.")
            return {'CANCELLED'}

        SEA_TOL = 1e-5
        ROUND   = 6

        sel_uv_set = self._build_selected_uv_positions(bm, uv_layer, ROUND)

        one_sided  = []
        both_sided = []

        for edge in bm.edges:
            loops = list(edge.link_loops)
            if len(loops) < 2:
                continue
            l0, l1 = loops[0], loops[1]

            if l0.vert == l1.link_loop_next.vert:
                uv_a0 = Vector(l0[uv_layer].uv)
                uv_a1 = Vector(l0.link_loop_next[uv_layer].uv)
                uv_b0 = Vector(l1.link_loop_next[uv_layer].uv)
                uv_b1 = Vector(l1[uv_layer].uv)
            else:
                uv_a0 = Vector(l0[uv_layer].uv)
                uv_a1 = Vector(l0.link_loop_next[uv_layer].uv)
                uv_b0 = Vector(l1[uv_layer].uv)
                uv_b1 = Vector(l1.link_loop_next[uv_layer].uv)

            if ((uv_a0 - uv_b0).length < SEA_TOL and
                    (uv_a1 - uv_b1).length < SEA_TOL):
                continue

            sel0 = self._loop_uv_in_set(l0, uv_layer, sel_uv_set, ROUND)
            sel1 = self._loop_uv_in_set(l1, uv_layer, sel_uv_set, ROUND)

            if sel0 and sel1:
                both_sided.append((uv_a0, uv_a1, uv_b0, uv_b1))
            elif sel0:
                one_sided.append((l1, l0, uv_b0, uv_b1, uv_a0, uv_a1))
            elif sel1:
                one_sided.append((l0, l1, uv_a0, uv_a1, uv_b0, uv_b1))

        if not one_sided and not both_sided:
            self.report({'WARNING'},
                        "No selected UV seam edges found. "
                        "Select edge(s) along a UV seam and try again.")
            return {'CANCELLED'}

        uv_snap = {loop.index: Vector(loop[uv_layer].uv)
                   for face in bm.faces for loop in face.loops}

        if one_sided:
            source_island = self._get_uv_island_faces(
                one_sided[0][0], uv_layer, bm)
            one_sided = [(lm, ls, ma, mb, sa, sb) for lm, ls, ma, mb, sa, sb
                         in one_sided if lm.face.index in source_island]
            if one_sided:
                src_pts = []
                tgt_pts = []
                for _, _, uv_ma, uv_mb, uv_sa, uv_sb in one_sided:
                    src_pts += [uv_ma, uv_mb]
                    tgt_pts += [uv_sa, uv_sb]
                T = self._similarity_transform_2d(src_pts, tgt_pts)
                weld_map = {}
                for _, _, uv_ma, uv_mb, uv_sa, uv_sb in one_sided:
                    weld_map[(round(uv_ma.x, ROUND), round(uv_ma.y, ROUND))] = uv_sa
                    weld_map[(round(uv_mb.x, ROUND), round(uv_mb.y, ROUND))] = uv_sb
                for face in bm.faces:
                    if face.index not in source_island:
                        continue
                    for loop in face.loops:
                        snap = uv_snap[loop.index]
                        key  = (round(snap.x, ROUND), round(snap.y, ROUND))
                        loop[uv_layer].uv = weld_map[key] if key in weld_map else T(snap)

        if both_sided:
            weld_targets = {}
            for uv_a0, uv_a1, uv_b0, uv_b1 in both_sided:
                mid0 = (uv_a0 + uv_b0) * 0.5
                mid1 = (uv_a1 + uv_b1) * 0.5
                for uv_pos, mid in ((uv_a0, mid0), (uv_a1, mid1),
                                    (uv_b0, mid0), (uv_b1, mid1)):
                    key = (round(uv_pos.x, ROUND), round(uv_pos.y, ROUND))
                    weld_targets[key] = mid
            for face in bm.faces:
                for loop in face.loops:
                    snap = uv_snap[loop.index]
                    key  = (round(snap.x, ROUND), round(snap.y, ROUND))
                    if key in weld_targets:
                        loop[uv_layer].uv = weld_targets[key]

        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, "Stitch: done.")
        return {'FINISHED'}


# ============================================================================
# UV Editor Paint Selection helpers
# ============================================================================

_UV_EPS = 1e-5


def _uv_clear_all_loop_flags(obj, use_sync=True):
    """Clear UV selection state on obj via BMesh.

    use_sync=True : clears mesh-level AND per-loop UV flags.
    use_sync=False: clears ONLY per-loop UV flags (preserves 3D viewport sel).
    """
    import traceback as _tb
    _uv_debug_log(
        f"[UV-CLEAR] _uv_clear_all_loop_flags called for obj={obj.name!r} use_sync={use_sync}\n"
        + ''.join(_tb.format_stack(limit=6))
    )
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.active
        if use_sync:
            for v in bm.verts:
                v.select = False
            for e in bm.edges:
                e.select = False
        for face in bm.faces:
            if use_sync:
                face.select = False
            if uv_layer is None:
                continue
            for loop in face.loops:
                try:
                    loop.uv_select_vert = False
                except AttributeError:
                    pass
                try:
                    loop.uv_select_edge = False
                except AttributeError:
                    pass
        if use_sync:
            bm.select_flush_mode()
        bmesh.update_edit_mesh(obj.data, destructive=False)
    except Exception:
        pass


def _uv_deselect_shared_verts(bm, uv_layer, target_uv):
    tx, ty = target_uv.x, target_uv.y
    for f in bm.faces:
        for lp in f.loops:
            u = lp[uv_layer].uv
            if abs(u.x - tx) < _UV_EPS and abs(u.y - ty) < _UV_EPS:
                lp.uv_select_vert = False


def _uv_deselect_shared_edges(bm, uv_layer, uv_a, uv_b):
    ax, ay = uv_a.x, uv_a.y
    bx, by = uv_b.x, uv_b.y
    for f in bm.faces:
        for lp in f.loops:
            la = lp[uv_layer].uv
            lb = lp.link_loop_next[uv_layer].uv
            if ((abs(la.x - ax) < _UV_EPS and abs(la.y - ay) < _UV_EPS and
                 abs(lb.x - bx) < _UV_EPS and abs(lb.y - by) < _UV_EPS) or
                (abs(la.x - bx) < _UV_EPS and abs(la.y - by) < _UV_EPS and
                 abs(lb.x - ax) < _UV_EPS and abs(lb.y - ay) < _UV_EPS)):
                lp.uv_select_edge = False


def _uv_island_flood_fill(bm, start_face, uv_layer):
    """Flood-fill from start_face through UV-connected edges.
    Returns a set of face indices forming the same UV island."""
    EPS     = 1e-5
    visited = {start_face.index}
    queue   = [start_face]
    while queue:
        face = queue.pop()
        for loop in face.loops:
            other = loop.link_loop_radial_next
            if other is loop or other.face.index in visited:
                continue
            uv_a0 = loop[uv_layer].uv
            uv_a1 = loop.link_loop_next[uv_layer].uv
            uv_b0 = other[uv_layer].uv
            uv_b1 = other.link_loop_next[uv_layer].uv
            if (uv_a0 - uv_b1).length < EPS and (uv_a1 - uv_b0).length < EPS:
                visited.add(other.face.index)
                queue.append(other.face)
    return visited


def _collect_uv_edge_loop(start_loop, uv_layer):
    """Return loops forming the UV edge loop or island boundary through start_loop.

    BOUNDARY edge → island perimeter (flood-fill, returns boundary loops).
    INTERIOR edge → UV edge loop (Alt+click style, quad traversal).
    """
    EPS = 1e-5

    def is_uv_boundary(lp):
        rad = lp.link_loop_radial_next
        if rad is lp:
            return True
        uv_a  = lp[uv_layer].uv
        uv_b  = lp.link_loop_next[uv_layer].uv
        uv_ra = rad[uv_layer].uv
        uv_rb = rad.link_loop_next[uv_layer].uv
        return not ((uv_a - uv_rb).length < EPS and (uv_b - uv_ra).length < EPS)

    def can_cross(lp):
        if is_uv_boundary(lp):
            return None
        return lp.link_loop_radial_next

    if is_uv_boundary(start_loop):
        visited = {start_loop.face.index}
        queue   = [start_loop.face]
        result  = []
        seen    = set()
        while queue:
            face = queue.pop()
            for lp in face.loops:
                if is_uv_boundary(lp):
                    uv0 = lp[uv_layer].uv
                    uv1 = lp.link_loop_next[uv_layer].uv
                    key = (round(uv0.x, 6), round(uv0.y, 6),
                           round(uv1.x, 6), round(uv1.y, 6))
                    if key not in seen:
                        seen.add(key)
                        result.append(lp)
                else:
                    rad = lp.link_loop_radial_next
                    if rad.face.index not in visited:
                        visited.add(rad.face.index)
                        queue.append(rad.face)
        return result

    result     = [start_loop]
    seen_edges = {start_loop.edge.index}

    cur = start_loop
    while len(cur.face.loops) == 4:
        rad = can_cross(cur.link_loop_next)
        if rad is None:
            break
        nxt = rad.link_loop_next
        if nxt.edge.index in seen_edges:
            break
        seen_edges.add(nxt.edge.index)
        result.append(nxt)
        cur = nxt

    cur = start_loop
    bwd = []
    while len(cur.face.loops) == 4:
        rad = can_cross(cur.link_loop_prev)
        if rad is None:
            break
        prv = rad.link_loop_prev
        if prv.edge.index in seen_edges:
            break
        seen_edges.add(prv.edge.index)
        bwd.append(prv)
        cur = prv

    return bwd[::-1] + result


# ============================================================================
# UV Shortest-Path helpers  (UV-space Dijkstra, seam-aware)
# ============================================================================

def _uv_vert_id(loop, uv_layer, eps_digits=5):
    uv = loop[uv_layer].uv
    return (loop.vert.index,
            round(uv.x, eps_digits),
            round(uv.y, eps_digits))


def _uv_find_path_faces(bm, uv_layer, start_face, end_face):
    if start_face.index == end_face.index:
        return [start_face]

    bm.faces.ensure_lookup_table()
    EPS = 1e-6

    adj = {}
    for edge in bm.edges:
        if len(edge.link_faces) != 2:
            continue
        fa, fb = edge.link_faces[0], edge.link_faces[1]
        loop_a = loop_b = None
        for lp in edge.link_loops:
            if lp.face == fa:
                loop_a = lp
            elif lp.face == fb:
                loop_b = lp
        if loop_a is None or loop_b is None:
            continue
        uva0 = loop_a[uv_layer].uv
        uva1 = loop_a.link_loop_next[uv_layer].uv
        uvb0 = loop_b[uv_layer].uv
        uvb1 = loop_b.link_loop_next[uv_layer].uv
        if not (
            ((uva0 - uvb1).length < EPS and (uva1 - uvb0).length < EPS) or
            ((uva0 - uvb0).length < EPS and (uva1 - uvb1).length < EPS)
        ):
            continue

        def _uv_ctr(face):
            lps = list(face.loops)
            n = max(len(lps), 1)
            return Vector((sum(lp[uv_layer].uv.x for lp in lps) / n,
                           sum(lp[uv_layer].uv.y for lp in lps) / n))

        step = (_uv_ctr(fa) - _uv_ctr(fb)).length
        adj.setdefault(fa.index, []).append((fb.index, step))
        adj.setdefault(fb.index, []).append((fa.index, step))

    dist = {start_face.index: 0.0}
    prev = {}
    heap = [(0.0, start_face.index)]
    visited = set()
    while heap:
        d, fi = heapq.heappop(heap)
        if fi in visited:
            continue
        visited.add(fi)
        if fi == end_face.index:
            break
        for nfi, step in adj.get(fi, []):
            if nfi in visited:
                continue
            alt = d + step
            if alt < dist.get(nfi, float('inf')):
                dist[nfi] = alt
                prev[nfi] = fi
                heapq.heappush(heap, (alt, nfi))

    path_indices = []
    fi = end_face.index
    while fi is not None:
        path_indices.append(fi)
        fi = prev.get(fi)
    path_indices.reverse()
    if not path_indices or path_indices[0] != start_face.index:
        return []
    return [bm.faces[i] for i in path_indices if i < len(bm.faces)]


def _uv_find_path_verts(bm, uv_layer, start_loop, end_loop):
    start_id = _uv_vert_id(start_loop, uv_layer)
    end_id   = _uv_vert_id(end_loop,   uv_layer)
    if start_id == end_id:
        return [start_id]

    uv_pos = {}
    adj    = {}

    for face in bm.faces:
        if face.hide:
            continue
        for loop in face.loops:
            vid   = _uv_vert_id(loop,                uv_layer)
            vid_n = _uv_vert_id(loop.link_loop_next, uv_layer)
            uv_pos.setdefault(vid,   loop[uv_layer].uv.copy())
            uv_pos.setdefault(vid_n, loop.link_loop_next[uv_layer].uv.copy())
            adj.setdefault(vid,   set()).add(vid_n)
            adj.setdefault(vid_n, set()).add(vid)

    dist = {start_id: 0.0}
    prev = {}
    heap = [(0.0, start_id)]
    visited = set()
    while heap:
        d, vid = heapq.heappop(heap)
        if vid in visited:
            continue
        visited.add(vid)
        if vid == end_id:
            break
        for nvid in adj.get(vid, set()):
            if nvid in visited:
                continue
            step = (uv_pos[vid] - uv_pos[nvid]).length
            alt = d + step
            if alt < dist.get(nvid, float('inf')):
                dist[nvid] = alt
                prev[nvid] = vid
                heapq.heappush(heap, (alt, nvid))

    path = []
    vid = end_id
    while vid is not None:
        path.append(vid)
        vid = prev.get(vid)
    path.reverse()
    if not path or path[0] != start_id:
        return []
    return path


def _uv_find_path_edges(bm, uv_layer, start_loop, end_loop):
    """Dijkstra path between two UV edges (loop-graph AND ring-graph).
    Returns a list of representative loops (one per edge on the path)."""
    start_edge = start_loop.edge
    end_edge   = end_loop.edge
    if start_edge.index == end_edge.index:
        return [start_loop]

    edge_by_index = {e.index: e for e in bm.edges}

    loop_graph = {e.index: set() for e in bm.edges}
    for e in bm.edges:
        for v in e.verts:
            for le in v.link_edges:
                if le.index != e.index:
                    loop_graph[e.index].add(le.index)

    ring_graph = {e.index: set() for e in bm.edges}
    for face in bm.faces:
        if face.hide or len(face.edges) != 4:
            continue
        fe = list(face.edges)
        for i in range(4):
            opp = fe[(i + 2) % 4]
            ring_graph[fe[i].index].add(opp.index)
            ring_graph[opp.index].add(fe[i].index)

    def _midpt(e):
        return (e.verts[0].co + e.verts[1].co) / 2.0

    def _dijkstra(graph):
        si, ei = start_edge.index, end_edge.index
        dist = {si: 0.0}
        prev = {}
        heap = [(0.0, si)]
        visited = set()
        while heap:
            d, idx = heapq.heappop(heap)
            if idx in visited:
                continue
            visited.add(idx)
            if idx == ei:
                break
            e = edge_by_index.get(idx)
            if e is None:
                continue
            mid = _midpt(e)
            for nidx in graph.get(idx, set()):
                if nidx in visited:
                    continue
                ne = edge_by_index.get(nidx)
                if ne is None:
                    continue
                alt = d + (mid - _midpt(ne)).length
                if alt < dist.get(nidx, float('inf')):
                    dist[nidx] = alt
                    prev[nidx] = idx
                    heapq.heappush(heap, (alt, nidx))

        path_idx = []
        idx = end_edge.index
        while idx is not None:
            path_idx.append(idx)
            idx = prev.get(idx)
        path_idx.reverse()
        if not path_idx or path_idx[0] != start_edge.index:
            return []
        return [edge_by_index[i] for i in path_idx if i in edge_by_index]

    shared_verts = set(start_edge.verts) & set(end_edge.verts)
    if shared_verts:
        path_edges = _dijkstra(loop_graph)
    else:
        loop_path = _dijkstra(loop_graph)
        ring_path = _dijkstra(ring_graph)
        if ring_path and (not loop_path or len(ring_path) <= len(loop_path)):
            path_edges = ring_path
        else:
            path_edges = loop_path

    result = []
    for edge in path_edges:
        for lp in edge.link_loops:
            if not lp.face.hide:
                result.append(lp)
                break
    return result


# ============================================================================
# IMAGE_OT_modo_uv_double_click_select
# ============================================================================

class IMAGE_OT_modo_uv_double_click_select(bpy.types.Operator):
    """Double-click selection in the UV Editor.
    Vertex / Face / Island mode: selects the entire UV island.
    Edge mode: selects the UV edge loop through the clicked edge.
    Shift: add to selection  |  Ctrl: remove from selection."""
    bl_idname  = 'image.modo_uv_double_click_select'
    bl_label   = 'UV Double-Click Select'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name='Mode',
        items=[
            ('set',    'Set',    'Replace selection'),
            ('add',    'Add',    'Add to selection'),
            ('remove', 'Remove', 'Remove from selection'),
        ],
        default='set',
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    def invoke(self, context, event):
        self._mx = event.mouse_region_x
        self._my = event.mouse_region_y
        return self.execute(context)

    def execute(self, context):
        mx = getattr(self, '_mx', None)
        my = getattr(self, '_my', None)
        if mx is None:
            return {'CANCELLED'}

        region   = context.region
        sima     = context.space_data
        obj      = context.edit_object
        if obj is None or obj.type != 'MESH':
            return {'CANCELLED'}

        ts       = context.tool_settings
        use_sync = ts.use_uv_select_sync
        if use_sync:
            sm      = ts.mesh_select_mode
            uv_mode = 'VERTEX' if sm[0] else ('EDGE' if sm[1] else 'FACE')
        else:
            uv_mode = ts.uv_select_mode

        do_select = (self.mode != 'remove')

        if self.mode == 'set' and use_sync:
            _uv_clear_all_loop_flags(obj)

        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            return {'CANCELLED'}

        if self.mode == 'set' and not use_sync:
            for _f in bm.faces:
                for _lp in _f.loops:
                    _lp.uv_select_vert = False
                    _lp.uv_select_edge = False

        # Edge mode + Add: expand ALL selected edges to their UV loops
        if uv_mode == 'EDGE' and self.mode == 'add':
            seed_loops = []
            for face in bm.faces:
                if face.hide:
                    continue
                for li, loop in enumerate(face.loops):
                    try:
                        sel = loop.uv_select_edge
                    except AttributeError:
                        sel = loop.edge.select if use_sync else False
                    if sel:
                        seed_loops.append(loop)
            best_dist_pre = float('inf')
            best_loop_pre = None
            TOLERANCE_PRE = 40
            for face in bm.faces:
                if face.hide:
                    continue
                for li, loop in enumerate(face.loops):
                    uv_a = loop[uv_layer].uv
                    uv_b = loop.link_loop_next[uv_layer].uv
                    sc_a = _uv_view_to_region(region, sima, uv_a.x, uv_a.y)
                    sc_b = _uv_view_to_region(region, sima, uv_b.x, uv_b.y)
                    if sc_a is None or sc_b is None:
                        continue
                    d = _dist_point_to_segment_2d(mx, my,
                                                  sc_a[0], sc_a[1],
                                                  sc_b[0], sc_b[1])
                    if d < best_dist_pre:
                        best_dist_pre = d
                        best_loop_pre = loop
            if best_loop_pre is not None and best_dist_pre <= TOLERANCE_PRE:
                if best_loop_pre not in seed_loops:
                    seed_loops.append(best_loop_pre)
            for seed in seed_loops:
                for lp in _collect_uv_edge_loop(seed, uv_layer):
                    if use_sync:
                        lp.edge.select = True
                        try:
                            lp.uv_select_edge = True
                        except AttributeError:
                            pass
                        try:
                            lp.uv_select_vert = True
                            lp.link_loop_next.uv_select_vert = True
                        except AttributeError:
                            pass
                    else:
                        try:
                            lp.uv_select_edge = True
                            lp.uv_select_vert = True
                            lp.link_loop_next.uv_select_vert = True
                        except AttributeError:
                            pass
            if use_sync:
                bm.select_flush_mode()
            bmesh.update_edit_mesh(obj.data)
            return {'FINISHED'}

        # Hit-test: find nearest UV element to cursor
        best_dist = float('inf')
        best_face = None
        best_li   = -1
        TOLERANCE = 40

        for face in bm.faces:
            if face.hide:
                continue
            loops = list(face.loops)
            if uv_mode == 'VERTEX':
                for li, loop in enumerate(loops):
                    uv = loop[uv_layer].uv
                    sc = _uv_view_to_region(region, sima, uv.x, uv.y)
                    if sc is None:
                        continue
                    d = math.sqrt((sc[0] - mx) ** 2 + (sc[1] - my) ** 2)
                    if d < best_dist:
                        best_dist = d; best_face = face; best_li = li
            elif uv_mode == 'EDGE':
                for li, loop in enumerate(loops):
                    uv_a = loop[uv_layer].uv
                    uv_b = loop.link_loop_next[uv_layer].uv
                    sc_a = _uv_view_to_region(region, sima, uv_a.x, uv_a.y)
                    sc_b = _uv_view_to_region(region, sima, uv_b.x, uv_b.y)
                    if sc_a is None or sc_b is None:
                        continue
                    d = _dist_point_to_segment_2d(mx, my,
                                                  sc_a[0], sc_a[1],
                                                  sc_b[0], sc_b[1])
                    if d < best_dist:
                        best_dist = d; best_face = face; best_li = li
            else:  # FACE or ISLAND
                sc_poly = []
                poly_ok = True
                for lp in loops:
                    sc = _uv_view_to_region(region, sima,
                                            lp[uv_layer].uv.x,
                                            lp[uv_layer].uv.y)
                    if sc is None:
                        poly_ok = False
                        break
                    sc_poly.append(sc)
                if poly_ok and len(sc_poly) >= 3:
                    if _point_in_poly_2d(mx, my, sc_poly):
                        best_dist = -1.0; best_face = face; best_li = -1
                        break
                n  = max(len(loops), 1)
                su = sum(lp[uv_layer].uv.x for lp in loops) / n
                sv = sum(lp[uv_layer].uv.y for lp in loops) / n
                sc = _uv_view_to_region(region, sima, su, sv)
                if sc is None:
                    continue
                d = math.sqrt((sc[0] - mx) ** 2 + (sc[1] - my) ** 2)
                if d < best_dist:
                    best_dist = d; best_face = face; best_li = -1

        if best_face is None or (best_dist > TOLERANCE and best_dist >= 0):
            return {'CANCELLED'}

        if uv_mode == 'EDGE':
            start_loop = list(best_face.loops)[best_li]
            for lp in _collect_uv_edge_loop(start_loop, uv_layer):
                if use_sync:
                    lp.edge.select = do_select
                    try:
                        lp.uv_select_edge = do_select
                    except AttributeError:
                        pass
                    if do_select:
                        try:
                            lp.uv_select_vert = True
                            lp.link_loop_next.uv_select_vert = True
                        except AttributeError:
                            pass
                else:
                    try:
                        lp.uv_select_edge = do_select
                    except AttributeError:
                        pass
                    if do_select:
                        try:
                            lp.uv_select_vert = True
                            lp.link_loop_next.uv_select_vert = True
                        except AttributeError:
                            pass
        else:
            island = _uv_island_flood_fill(bm, best_face, uv_layer)
            bm.faces.ensure_lookup_table()
            for fi in island:
                if fi >= len(bm.faces):
                    continue
                iface = bm.faces[fi]
                if use_sync:
                    iface.select = do_select
                    for lp in iface.loops:
                        lp.vert.select = do_select
                        try:
                            lp.uv_select_vert = do_select
                            lp.uv_select_edge = do_select
                        except AttributeError:
                            pass
                else:
                    for lp in iface.loops:
                        lp.uv_select_vert = do_select
                        if uv_mode != 'VERTEX':
                            lp.uv_select_edge = do_select

        if use_sync:
            bm.select_flush_mode()
        bmesh.update_edit_mesh(obj.data)
        context.area.tag_redraw()
        return {'FINISHED'}


# ============================================================================
# IMAGE_OT_modo_uv_shortest_path
# ============================================================================

class IMAGE_OT_modo_uv_shortest_path(bpy.types.Operator):
    """UV Shortest Path Select.
    Selects the shortest path in UV space from the active element to the
    element under the cursor.  Uses the same 40-pixel tolerance hit-test as
    the rest of this addon and respects UV seams.
    Shift: add path to existing selection."""
    bl_idname  = 'image.modo_uv_shortest_path'
    bl_label   = 'UV Shortest Path Select'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name='Mode',
        items=[
            ('set',    'Set',    'Replace selection with path'),
            ('add',    'Add',    'Add path to existing selection'),
        ],
        default='set',
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    def invoke(self, context, event):
        self._mx = event.mouse_region_x
        self._my = event.mouse_region_y
        return self.execute(context)

    def execute(self, context):
        mx = getattr(self, '_mx', None)
        my = getattr(self, '_my', None)
        if mx is None:
            return {'CANCELLED'}

        region   = context.region
        sima     = context.space_data
        obj      = context.edit_object
        if obj is None or obj.type != 'MESH':
            return {'CANCELLED'}

        ts       = context.tool_settings
        use_sync = ts.use_uv_select_sync
        if use_sync:
            sm      = ts.mesh_select_mode
            uv_mode = 'VERTEX' if sm[0] else ('EDGE' if sm[1] else 'FACE')
        else:
            uv_mode = ts.uv_select_mode

        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            return {'CANCELLED'}

        TOLERANCE = 40

        end_face = None
        end_loop = None

        if uv_mode in ('FACE', 'ISLAND'):
            best_dist = float('inf')
            for face in bm.faces:
                if face.hide:
                    continue
                loops = list(face.loops)
                sc_poly = []
                poly_ok = True
                for lp in loops:
                    sc = _uv_view_to_region(region, sima,
                                            lp[uv_layer].uv.x,
                                            lp[uv_layer].uv.y)
                    if sc is None:
                        poly_ok = False
                        break
                    sc_poly.append(sc)
                if poly_ok and len(sc_poly) >= 3:
                    if _point_in_poly_2d(mx, my, sc_poly):
                        end_face = face
                        best_dist = -1.0
                        break
                n  = max(len(loops), 1)
                su = sum(lp[uv_layer].uv.x for lp in loops) / n
                sv = sum(lp[uv_layer].uv.y for lp in loops) / n
                sc = _uv_view_to_region(region, sima, su, sv)
                if sc is None:
                    continue
                d = math.sqrt((sc[0] - mx) ** 2 + (sc[1] - my) ** 2)
                if d < best_dist:
                    best_dist = d
                    end_face  = face
            if end_face is None or (best_dist > TOLERANCE and best_dist >= 0):
                return {'CANCELLED'}

        elif uv_mode == 'EDGE':
            best_dist = float('inf')
            for face in bm.faces:
                if face.hide:
                    continue
                for loop in face.loops:
                    uv_a = loop[uv_layer].uv
                    uv_b = loop.link_loop_next[uv_layer].uv
                    sc_a = _uv_view_to_region(region, sima, uv_a.x, uv_a.y)
                    sc_b = _uv_view_to_region(region, sima, uv_b.x, uv_b.y)
                    if sc_a is None or sc_b is None:
                        continue
                    d = _dist_point_to_segment_2d(mx, my,
                                                  sc_a[0], sc_a[1],
                                                  sc_b[0], sc_b[1])
                    if d < best_dist:
                        best_dist = d
                        end_loop  = loop
                        end_face  = face
            if end_loop is None or best_dist > TOLERANCE:
                return {'CANCELLED'}

        else:  # VERTEX
            best_dist = float('inf')
            for face in bm.faces:
                if face.hide:
                    continue
                for loop in face.loops:
                    uv = loop[uv_layer].uv
                    sc = _uv_view_to_region(region, sima, uv.x, uv.y)
                    if sc is None:
                        continue
                    d = math.sqrt((sc[0] - mx) ** 2 + (sc[1] - my) ** 2)
                    if d < best_dist:
                        best_dist = d
                        end_loop  = loop
                        end_face  = face
            if end_loop is None or best_dist > TOLERANCE:
                return {'CANCELLED'}

        start_face = None
        start_loop = None

        if uv_mode in ('FACE', 'ISLAND'):
            for face in bm.faces:
                if face.hide:
                    continue
                if use_sync:
                    if face.select:
                        start_face = face
                        break
                else:
                    if any(lp.uv_select_vert for lp in face.loops):
                        start_face = face
                        break
            if start_face is None:
                return {'CANCELLED'}

        elif uv_mode == 'EDGE':
            for face in bm.faces:
                if face.hide:
                    continue
                for loop in face.loops:
                    try:
                        sel = loop.uv_select_edge
                    except AttributeError:
                        sel = loop.edge.select if use_sync else False
                    if sel:
                        start_loop = loop
                        start_face = face
                        break
                if start_loop is not None:
                    break
            if start_loop is None:
                return {'CANCELLED'}

        else:  # VERTEX
            for face in bm.faces:
                if face.hide:
                    continue
                for loop in face.loops:
                    if use_sync:
                        sel = loop.vert.select
                    else:
                        try:
                            sel = loop.uv_select_vert
                        except AttributeError:
                            sel = False
                    if sel:
                        start_loop = loop
                        start_face = face
                        break
                if start_loop is not None:
                    break
            if start_loop is None:
                return {'CANCELLED'}

        if uv_mode in ('FACE', 'ISLAND'):
            path_faces = _uv_find_path_faces(bm, uv_layer, start_face, end_face)
            if not path_faces:
                self.report({'WARNING'}, "No UV path found")
                return {'CANCELLED'}
            if self.mode == 'set':
                for f in bm.faces:
                    if use_sync:
                        f.select = False
                        for lp in f.loops:
                            try:
                                lp.uv_select_vert = False
                                lp.uv_select_edge = False
                            except AttributeError:
                                pass
                    else:
                        for lp in f.loops:
                            lp.uv_select_vert = False
                            lp.uv_select_edge = False
            if uv_mode == 'ISLAND':
                island_done = set()
                for pf in path_faces:
                    if pf.index in island_done:
                        continue
                    island = _uv_island_flood_fill(bm, pf, uv_layer)
                    island_done |= island
                    bm.faces.ensure_lookup_table()
                    for fi in island:
                        if fi < len(bm.faces):
                            iface = bm.faces[fi]
                            if use_sync:
                                iface.select = True
                                for lp in iface.loops:
                                    try:
                                        lp.uv_select_vert = True
                                        lp.uv_select_edge = True
                                    except AttributeError:
                                        pass
                            else:
                                for lp in iface.loops:
                                    lp.uv_select_vert = True
                                    lp.uv_select_edge = True
            else:
                for pf in path_faces:
                    if use_sync:
                        pf.select = True
                        for lp in pf.loops:
                            try:
                                lp.uv_select_vert = True
                                lp.uv_select_edge = True
                            except AttributeError:
                                pass
                    else:
                        for lp in pf.loops:
                            lp.uv_select_vert = True
                            lp.uv_select_edge = True

        elif uv_mode == 'EDGE':
            path_loops = _uv_find_path_edges(bm, uv_layer, start_loop, end_loop)
            if not path_loops:
                self.report({'WARNING'}, "No UV path found")
                return {'CANCELLED'}
            if self.mode == 'set':
                for f in bm.faces:
                    if use_sync:
                        for lp in f.loops:
                            lp.edge.select = False
                            try:
                                lp.uv_select_edge = False
                                lp.uv_select_vert = False
                            except AttributeError:
                                pass
                    else:
                        for lp in f.loops:
                            lp.uv_select_edge = False
                            lp.uv_select_vert = False
            for lp in path_loops:
                if use_sync:
                    lp.edge.select = True
                    try:
                        lp.uv_select_edge = True
                        lp.uv_select_vert = True
                        lp.link_loop_next.uv_select_vert = True
                    except AttributeError:
                        pass
                else:
                    lp.uv_select_edge = True
                    lp.uv_select_vert = True
                    lp.link_loop_next.uv_select_vert = True

        else:  # VERTEX
            path_vids = _uv_find_path_verts(bm, uv_layer, start_loop, end_loop)
            if not path_vids:
                self.report({'WARNING'}, "No UV path found")
                return {'CANCELLED'}
            path_vid_set = set(path_vids)
            if self.mode == 'set':
                for f in bm.faces:
                    if use_sync:
                        for lp in f.loops:
                            lp.vert.select = False
                            try:
                                lp.uv_select_vert = False
                            except AttributeError:
                                pass
                    else:
                        for lp in f.loops:
                            lp.uv_select_vert = False
            for f in bm.faces:
                if f.hide:
                    continue
                for lp in f.loops:
                    if _uv_vert_id(lp, uv_layer) in path_vid_set:
                        if use_sync:
                            lp.vert.select = True
                            try:
                                lp.uv_select_vert = True
                            except AttributeError:
                                pass
                        else:
                            lp.uv_select_vert = True

        if use_sync:
            bm.select_flush_mode()
        bmesh.update_edit_mesh(obj.data)
        context.area.tag_redraw()
        return {'FINISHED'}


# ============================================================================
# IMAGE_OT_modo_uv_click_select
# ============================================================================

class IMAGE_OT_modo_uv_click_select(bpy.types.Operator):
    """Click selection in the UV Editor using the paint-selection radius.
    Selects all UV elements within the brush radius on a single click.
    Delegates directly to paint selection's _paint() for identical behavior."""
    bl_idname  = 'image.modo_uv_click_select'
    bl_label   = 'UV Click Select'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name='Mode',
        items=[
            ('set',    'Set',    'Replace selection'),
            ('add',    'Add',    'Add to selection'),
            ('remove', 'Remove', 'Remove from selection'),
        ],
        default='set',
        options={'HIDDEN'},
    )

    deselect_all: BoolProperty(
        name='Deselect All',
        description='Deselect everything when nothing is under the cursor',
        default=True,
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    def invoke(self, context, event):
        obj = context.edit_object
        if obj is None or obj.type != 'MESH':
            return {'CANCELLED'}

        _uv_debug_log(
            f"[UV-CLICK] invoke: mode={self.mode!r} deselect_all={self.deselect_all} "
            f"mouse=({event.mouse_region_x},{event.mouse_region_y})"
        )

        if self.mode == 'set':
            _use_sync = context.tool_settings.use_uv_select_sync
            _uv_clear_all_loop_flags(obj, use_sync=_use_sync)

        self._cache = IMAGE_OT_modo_uv_paint_selection._build_cache(self, context)
        _uv_debug_log(f"[UV-CLICK] cache size={len(self._cache)}")
        IMAGE_OT_modo_uv_paint_selection._paint(self, context, event)

        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


# ============================================================================
# IMAGE_OT_modo_uv_paint_selection
# ============================================================================

class IMAGE_OT_modo_uv_paint_selection(bpy.types.Operator):
    """Paint selection in the UV Editor.
    Drag over UV elements to select them under the brush.
    Ctrl+Shift+B: set  |  Ctrl+Shift+Alt+B: add  |  Ctrl+Alt+B: remove"""
    bl_idname  = 'image.modo_uv_paint_selection'
    bl_label   = 'UV Paint Selection'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name='Mode',
        items=[
            ('set',    'Set',    'Replace selection (clear, then paint)'),
            ('add',    'Add',    'Add to selection'),
            ('remove', 'Remove', 'Remove from selection'),
        ],
        default='set',
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    def _build_cache(self, context):
        region = context.region
        sima   = context.space_data
        obj    = context.edit_object
        if obj is None or obj.type != 'MESH':
            return []
        ts       = context.tool_settings
        use_sync = ts.use_uv_select_sync
        if use_sync:
            sm      = ts.mesh_select_mode
            uv_mode = 'VERTEX' if sm[0] else ('EDGE' if sm[1] else 'FACE')
        else:
            uv_mode = ts.uv_select_mode
        bm       = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.active
        entries  = []
        for fi, face in enumerate(bm.faces):
            if face.hide or uv_layer is None:
                continue
            if uv_mode == 'VERTEX':
                for li, loop in enumerate(face.loops):
                    uv = loop[uv_layer].uv
                    sc = _uv_view_to_region(region, sima, uv.x, uv.y)
                    if sc is not None:
                        entries.append((sc[0], sc[1], fi, li, 'VERTEX'))
            elif uv_mode == 'EDGE':
                for li, loop in enumerate(face.loops):
                    uv_a = loop[uv_layer].uv
                    uv_b = loop.link_loop_next[uv_layer].uv
                    sc_a = _uv_view_to_region_unclamped(region, uv_a.x, uv_a.y)
                    sc_b = _uv_view_to_region_unclamped(region, uv_b.x, uv_b.y)
                    if sc_a is not None and sc_b is not None:
                        clipped = _clip_segment_to_rect(
                            sc_a[0], sc_a[1], sc_b[0], sc_b[1],
                            0, 0, region.width, region.height,
                        )
                        if clipped is not None:
                            entries.append((clipped[0], clipped[1], fi, li,
                                            'EDGE', clipped[2], clipped[3]))
            else:
                poly = []
                for lp in face.loops:
                    uv = lp[uv_layer].uv
                    sc = _uv_view_to_region(region, sima, uv.x, uv.y)
                    if sc is not None:
                        poly.append((sc[0], sc[1]))
                if len(poly) >= 3:
                    entries.append((poly[0][0], poly[0][1], fi, -1, uv_mode, poly))
        return entries

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            self._mouse_pos = (event.mouse_region_x, event.mouse_region_y)
            self._paint(context, event)
            if context.area:
                context.area.tag_redraw()
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            return {'FINISHED'}
        elif event.type in {'RIGHTMOUSE', 'ESC'}:
            from . import state as _state
            _state._preselect_hits = []
            if context.area:
                context.area.tag_redraw()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        ts = context.tool_settings
        obj = context.edit_object
        _uv_debug_log(
            f"[UV-PAINT] invoke: mode={self.mode!r} "
            f"use_sync={ts.use_uv_select_sync} "
            f"uv_select_mode={getattr(ts, 'uv_select_mode', '?')!r} "
            f"obj={obj.name if obj else None!r}"
        )
        if self.mode == 'set':
            if obj and obj.type == 'MESH':
                _uv_clear_all_loop_flags(obj, use_sync=ts.use_uv_select_sync)
        self._mouse_pos = (event.mouse_region_x, event.mouse_region_y)
        self._cache     = self._build_cache(context)
        self._paint(context, event)
        from . import state as _state
        if _state._preselect_hits:
            _state._preselect_hits = []
            if context.area:
                context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _paint(self, context, event):
        prefs     = get_addon_preferences(context)
        radius_px = (prefs.paint_selection_size if prefs else 50) / 4
        mx, my    = event.mouse_region_x, event.mouse_region_y
        do_select = (self.mode in {'set', 'add'})
        obj = context.edit_object
        if obj is None or obj.type != 'MESH':
            return
        ts       = context.tool_settings
        use_sync = ts.use_uv_select_sync
        if use_sync:
            sm      = ts.mesh_select_mode
            uv_mode = 'VERTEX' if sm[0] else ('EDGE' if sm[1] else 'FACE')
        else:
            uv_mode = ts.uv_select_mode
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.active
        dirty    = False
        island_done = set()
        for entry in self._cache:
            sx, sy, fi, li, etype = entry[:5]
            if etype == 'EDGE' and len(entry) == 7:
                if _point_to_segment_dist(mx, my, sx, sy, entry[5], entry[6]) > radius_px:
                    continue
            elif etype in {'FACE', 'ISLAND'} and len(entry) == 6:
                if not _circle_touches_polygon(mx, my, radius_px, entry[5]):
                    continue
            elif math.sqrt((sx - mx) ** 2 + (sy - my) ** 2) > radius_px:
                continue
            if fi >= len(bm.faces):
                continue
            face = bm.faces[fi]
            if use_sync:
                if uv_mode == 'VERTEX':
                    if 0 <= li < len(face.loops):
                        lp = face.loops[li]
                        lp.vert.select = do_select
                        try:
                            lp.uv_select_vert = do_select
                        except AttributeError:
                            pass
                        dirty = True
                elif uv_mode == 'EDGE':
                    if 0 <= li < len(face.loops):
                        lp = face.loops[li]
                        lp.edge.select = do_select
                        try:
                            lp.uv_select_edge = do_select
                        except AttributeError:
                            pass
                        if do_select:
                            try:
                                lp.uv_select_vert = True
                                lp.link_loop_next.uv_select_vert = True
                            except AttributeError:
                                pass
                        dirty = True
                else:
                    face.select = do_select
                    if do_select and uv_layer is not None:
                        for lp in face.loops:
                            try:
                                lp.uv_select_vert = True
                                lp.uv_select_edge = True
                            except AttributeError:
                                pass
                    dirty = True
            else:
                if uv_layer is None:
                    continue
                if uv_mode == 'VERTEX':
                    if 0 <= li < len(face.loops):
                        lp = face.loops[li]
                        if do_select:
                            lp.uv_select_vert = True
                        else:
                            _uv_deselect_shared_verts(bm, uv_layer,
                                                      lp[uv_layer].uv.copy())
                        dirty = True
                elif uv_mode == 'EDGE':
                    if 0 <= li < len(face.loops):
                        lp = face.loops[li]
                        if do_select:
                            lp.uv_select_edge = True
                            lp.uv_select_vert = True
                            lp.link_loop_next.uv_select_vert = True
                        else:
                            _uv_deselect_shared_edges(bm, uv_layer,
                                                      lp[uv_layer].uv.copy(),
                                                      lp.link_loop_next[uv_layer].uv.copy())
                        dirty = True
                elif uv_mode == 'FACE':
                    for lp in face.loops:
                        lp.uv_select_vert = do_select
                        lp.uv_select_edge = do_select
                    dirty = True
                else:  # ISLAND
                    if face.index in island_done:
                        continue
                    island = _uv_island_flood_fill(bm, face, uv_layer)
                    island_done |= island
                    bm.faces.ensure_lookup_table()
                    for ifi in island:
                        if ifi < len(bm.faces):
                            iface = bm.faces[ifi]
                            for lp in iface.loops:
                                lp.uv_select_vert = do_select
                                lp.uv_select_edge = do_select
                    dirty = True
        if dirty:
            if use_sync:
                bm.select_flush_mode()
            bmesh.update_edit_mesh(obj.data)
            if state._uv_active_transform_mode is not None:
                state._uv_transform_targets = _collect_uv_transform_targets(context)
                _median = _compute_uv_selection_median(context)
                if _median is not None:
                    state._uv_gizmo_center = _median
                    try:
                        _dbg = getattr(get_addon_preferences(context), 'debug_uv_handle', False)
                    except Exception:
                        _dbg = False
                    if _dbg:
                        _uv_debug_log(f"[UV-GIZMO-CTR] box-select updated center={_median}")


# ============================================================================
# IMAGE_OT_modo_uv_lasso_select
# ============================================================================

class IMAGE_OT_modo_uv_lasso_select(bpy.types.Operator):
    """Lasso selection in the UV Editor.
    Right-click or middle-click drag to draw a freehand selection area.
    Shift: add to selection  |  Ctrl: remove from selection.
    A short click without drag opens the UV context menu (RMB only)."""
    bl_idname  = 'image.modo_uv_lasso_select'
    bl_label   = 'UV Lasso Select'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name='Mode',
        items=[
            ('set',    'Set',    'Replace selection'),
            ('add',    'Add',    'Add to selection (Shift)'),
            ('remove', 'Remove', 'Remove from selection (Ctrl)'),
        ],
        default='set',
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        return (context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR'
                and context.mode == 'EDIT_MESH')

    def _draw_lasso_callback(self, context):
        import gpu
        from gpu_extras.batch import batch_for_shader
        pts = self.lasso_points
        if len(pts) < 2:
            return
        try:
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            gpu.state.blend_set('ALPHA')
            shader.bind()

            if len(pts) >= 3:
                def _dp_simplify(points, epsilon):
                    if len(points) < 3:
                        return points
                    ax, ay = points[0];  bx, by = points[-1]
                    dx, dy  = bx - ax, by - ay
                    length_sq = dx * dx + dy * dy
                    max_dist, max_idx = 0.0, 0
                    for i in range(1, len(points) - 1):
                        px, py = points[i]
                        if length_sq == 0:
                            d = math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
                        else:
                            t = max(0.0, min(1.0,
                                    ((px - ax) * dx + (py - ay) * dy) / length_sq))
                            d = math.sqrt((px - (ax + t * dx)) ** 2 +
                                          (py - (ay + t * dy)) ** 2)
                        if d > max_dist:
                            max_dist, max_idx = d, i
                    if max_dist > epsilon:
                        left  = _dp_simplify(points[:max_idx + 1], epsilon)
                        right = _dp_simplify(points[max_idx:], epsilon)
                        return left[:-1] + right
                    return [points[0], points[-1]]

                simplified = _dp_simplify(pts, 2.0)
                if len(simplified) < 3:
                    simplified = pts
                from mathutils.geometry import tessellate_polygon
                pts3d = [(p[0], p[1], 0.0) for p in simplified]
                try:
                    tri_indices = tessellate_polygon([pts3d])
                    tris = []
                    for tri in tri_indices:
                        for idx in tri:
                            tris.append(simplified[idx])
                    if tris:
                        fill_batch = batch_for_shader(shader, 'TRIS', {'pos': tris})
                        shader.uniform_float('color', (0.5, 0.5, 0.5, 0.15))
                        fill_batch.draw(shader)
                except Exception:
                    pass

            closed = pts + [pts[0]]
            DASH = 4;  GAP = 4;  PERIOD = DASH + GAP

            def build_dash_coords(polyline, phase_offset):
                result = []
                phase = phase_offset % PERIOD
                for i in range(len(polyline) - 1):
                    ax, ay = polyline[i];  bx, by = polyline[i + 1]
                    seg_len = math.sqrt((bx - ax) ** 2 + (by - ay) ** 2)
                    if seg_len == 0:
                        continue
                    dx = (bx - ax) / seg_len;  dy = (by - ay) / seg_len
                    t = 0.0
                    while t < seg_len:
                        cycle_pos = phase % PERIOD
                        remaining = PERIOD - cycle_pos
                        step      = min(remaining, seg_len - t)
                        if cycle_pos < DASH:
                            drawn = min(DASH - cycle_pos, step)
                            result.append((ax + dx * t,           ay + dy * t))
                            result.append((ax + dx * (t + drawn), ay + dy * (t + drawn)))
                            phase += drawn;  t += drawn
                        else:
                            skipped = min(GAP - (cycle_pos - DASH), step)
                            phase += skipped;  t += skipped
                return result

            base_batch = batch_for_shader(shader, 'LINE_STRIP', {'pos': closed})
            gpu.state.line_width_set(1.0)
            shader.uniform_float('color', (0.0, 0.0, 0.0, 1.0))
            base_batch.draw(shader)

            white_coords = build_dash_coords(closed, 0)
            if white_coords:
                dash_batch = batch_for_shader(shader, 'LINES', {'pos': white_coords})
                shader.uniform_float('color', (1.0, 1.0, 1.0, 1.0))
                dash_batch.draw(shader)

        except Exception as e:
            print(f'[UV LASSO DRAW ERROR] {e}')
        finally:
            gpu.state.blend_set('NONE')
            gpu.state.line_width_set(1.0)

    def _remove_draw_handler(self):
        if self._draw_handler is not None:
            bpy.types.SpaceImageEditor.draw_handler_remove(
                self._draw_handler, 'WINDOW')
            self._draw_handler = None

    def invoke(self, context, event):
        self._button      = event.type
        self._start_x     = event.mouse_region_x
        self._start_y     = event.mouse_region_y
        self.lasso_points = [(event.mouse_region_x, event.mouse_region_y)]
        self._draw_handler = bpy.types.SpaceImageEditor.draw_handler_add(
            self._draw_lasso_callback, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if context.area:
            context.area.tag_redraw()
        if event.type == 'MOUSEMOVE':
            self.lasso_points.append((event.mouse_region_x, event.mouse_region_y))
        elif event.type == self._button and event.value in {'RELEASE', 'CLICK'}:
            dx = event.mouse_region_x - self._start_x
            dy = event.mouse_region_y - self._start_y
            drag_dist = 0.0 if event.value == 'CLICK' else math.sqrt(dx*dx + dy*dy)
            self._remove_draw_handler()
            if context.area:
                context.area.tag_redraw()
            is_click = drag_dist < 10
            if is_click and self._button == 'RIGHTMOUSE':
                prefs = get_addon_preferences(context)
                _MOUSE_KEYS = {'RIGHTMOUSE', 'MIDDLEMOUSE'}
                sp_key = prefs.shortest_path_key
                event_shift = event.shift or (self.mode == 'add')
                is_sp_trigger = (
                    sp_key in _MOUSE_KEYS
                    and event.type == sp_key
                    and prefs.shortest_path_shift == event_shift
                    and prefs.shortest_path_ctrl  == event.ctrl
                    and prefs.shortest_path_alt   == event.alt
                )
                if is_sp_trigger:
                    sp_mode = 'add' if event_shift else 'set'
                    bpy.ops.image.modo_uv_shortest_path(
                        'INVOKE_DEFAULT', mode=sp_mode)
                    return {'FINISHED'}
                if self.mode == 'set':
                    bpy.ops.wm.call_menu(name='IMAGE_MT_uvs_context_menu')
                return {'FINISHED'}
            if len(self.lasso_points) >= 3 and drag_dist >= 5:
                return self.execute(context)
            return {'CANCELLED'}
        elif event.type in {'ESC', 'LEFTMOUSE'}:
            self._remove_draw_handler()
            if context.area:
                context.area.tag_redraw()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def execute(self, context):
        region  = context.region
        sima    = context.space_data
        polygon = self.lasso_points
        obj     = context.edit_object
        if obj is None or obj.type != 'MESH':
            return {'CANCELLED'}
        ts       = context.tool_settings
        use_sync = ts.use_uv_select_sync
        if use_sync:
            sm      = ts.mesh_select_mode
            uv_mode = 'VERTEX' if sm[0] else ('EDGE' if sm[1] else 'FACE')
        else:
            uv_mode = ts.uv_select_mode
        if self.mode == 'set':
            _uv_clear_all_loop_flags(obj, use_sync=use_sync)
        do_select = (self.mode != 'remove')
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        uv_layer    = bm.loops.layers.uv.active
        dirty       = False
        island_done = set()
        for face in bm.faces:
            if face.hide or uv_layer is None:
                continue
            if uv_mode == 'VERTEX':
                for loop in face.loops:
                    uv = loop[uv_layer].uv
                    sc = _uv_view_to_region(region, sima, uv.x, uv.y)
                    if sc is None or not point_in_polygon(sc, polygon):
                        continue
                    if use_sync:
                        loop.vert.select = do_select
                        try:
                            loop.uv_select_vert = do_select
                        except AttributeError:
                            pass
                    else:
                        loop.uv_select_vert = do_select
                    dirty = True
            elif uv_mode == 'EDGE':
                for loop in face.loops:
                    uv_a = loop[uv_layer].uv
                    uv_b = loop.link_loop_next[uv_layer].uv
                    sc_a = _uv_view_to_region(region, sima, uv_a.x, uv_a.y)
                    sc_b = _uv_view_to_region(region, sima, uv_b.x, uv_b.y)
                    if sc_a is None or sc_b is None:
                        continue
                    if not (point_in_polygon(sc_a, polygon) and point_in_polygon(sc_b, polygon)):
                        continue
                    if use_sync:
                        loop.edge.select = do_select
                        try:
                            loop.uv_select_edge = do_select
                        except AttributeError:
                            pass
                        if do_select:
                            try:
                                loop.uv_select_vert = True
                                loop.link_loop_next.uv_select_vert = True
                            except AttributeError:
                                pass
                    else:
                        loop.uv_select_edge = do_select
                        if do_select:
                            loop.uv_select_vert = True
                            loop.link_loop_next.uv_select_vert = True
                    dirty = True
            elif uv_mode == 'FACE':
                corner_scs = [_uv_view_to_region(region, sima,
                                                  lp[uv_layer].uv.x,
                                                  lp[uv_layer].uv.y)
                              for lp in face.loops]
                if any(sc is None or not point_in_polygon(sc, polygon)
                       for sc in corner_scs):
                    continue
                if use_sync:
                    face.select = do_select
                    for lp in face.loops:
                        try:
                            lp.uv_select_vert = do_select
                            lp.uv_select_edge = do_select
                        except AttributeError:
                            pass
                else:
                    for lp in face.loops:
                        lp.uv_select_vert = do_select
                        lp.uv_select_edge = do_select
                dirty = True
            else:  # ISLAND
                if face.index in island_done:
                    continue
                corner_scs = [_uv_view_to_region(region, sima,
                                                  lp[uv_layer].uv.x,
                                                  lp[uv_layer].uv.y)
                              for lp in face.loops]
                if any(sc is None or not point_in_polygon(sc, polygon)
                       for sc in corner_scs):
                    continue
                island = _uv_island_flood_fill(bm, face, uv_layer)
                island_done |= island
                bm.faces.ensure_lookup_table()
                for ifi in island:
                    if ifi < len(bm.faces):
                        if use_sync:
                            bm.faces[ifi].select = do_select
                            for lp in bm.faces[ifi].loops:
                                try:
                                    lp.uv_select_vert = do_select
                                    lp.uv_select_edge = do_select
                                except AttributeError:
                                    pass
                        else:
                            iface = bm.faces[ifi]
                            for lp in iface.loops:
                                lp.uv_select_vert = do_select
                                lp.uv_select_edge = do_select
                dirty = True
        if dirty:
            if use_sync:
                bm.select_flush_mode()
            bmesh.update_edit_mesh(obj.data)
            if state._uv_active_transform_mode is not None:
                state._uv_transform_targets = _collect_uv_transform_targets(context)
                _median = _compute_uv_selection_median(context)
                if _median is not None:
                    state._uv_gizmo_center = _median
                    try:
                        _dbg = getattr(get_addon_preferences(context), 'debug_uv_handle', False)
                    except Exception:
                        _dbg = False
                    if _dbg:
                        _uv_debug_log(f"[UV-GIZMO-CTR] lasso-select updated center={_median}")
        return {'FINISHED'}
