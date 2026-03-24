"""
Shortest path algorithms (Dijkstra / BFS) for mesh topology.
No Blender-specific imports — pure bmesh geometry.
"""

import heapq
from collections import deque


def find_shortest_path_vertices(bm, start_vert, end_vert, use_3d=True):
    """Find shortest path between two vertices using Dijkstra's algorithm.

    Args:
        bm: BMesh
        start_vert: Starting BMVert
        end_vert: Target BMVert
        use_3d: Use 3D distance (True) or edge count (False)

    Returns:
        List of BMVert along the shortest path, or [] if no path.
    """
    if start_vert == end_vert:
        return [start_vert]

    distances = {}
    previous = {}
    heap = [(0.0, start_vert.index, start_vert)]
    distances[start_vert] = 0.0
    visited = set()

    while heap:
        dist, _, current = heapq.heappop(heap)
        if current in visited:
            continue
        visited.add(current)
        if current == end_vert:
            break
        for edge in current.link_edges:
            neighbor = edge.other_vert(current)
            if neighbor in visited:
                continue
            step = (current.co - neighbor.co).length if use_3d else 1.0
            alt = dist + step
            if alt < distances.get(neighbor, float('inf')):
                distances[neighbor] = alt
                previous[neighbor] = current
                heapq.heappush(heap, (alt, neighbor.index, neighbor))

    path = []
    current = end_vert
    while current is not None:
        path.append(current)
        current = previous.get(current)
    path.reverse()
    return path if path and path[0] == start_vert else []


def find_shortest_path_edges(bm, start_edge, end_edge, use_3d=True, use_ring=False):
    """Find shortest path between two edges via their midpoints."""
    edge_graph = {e: set() for e in bm.edges}

    if use_ring:
        for e in bm.edges:
            for face in e.link_faces:
                if len(face.edges) == 4:
                    face_edges = list(face.edges)
                    idx = face_edges.index(e)
                    opposite_edge = face_edges[(idx + 2) % 4]
                    edge_graph[e].add(opposite_edge)
    else:
        for e in bm.edges:
            for v in e.verts:
                for linked_e in v.link_edges:
                    if linked_e != e:
                        edge_graph[e].add(linked_e)

    if not use_3d:
        queue = deque([(start_edge, [start_edge])])
        visited = {start_edge}
        while queue:
            current, path = queue.popleft()
            if current == end_edge:
                return path
            for neighbor in edge_graph[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))
        return []
    else:
        distances = {start_edge: 0.0}
        previous = {}
        heap = [(0.0, start_edge.index, start_edge)]
        visited = set()
        while heap:
            dist, _, current = heapq.heappop(heap)
            if current in visited:
                continue
            visited.add(current)
            if current == end_edge:
                break
            for neighbor in edge_graph[current]:
                if neighbor in visited:
                    continue
                current_mid = (current.verts[0].co + current.verts[1].co) / 2.0
                neighbor_mid = (neighbor.verts[0].co + neighbor.verts[1].co) / 2.0
                step = (current_mid - neighbor_mid).length
                alt = dist + step
                if alt < distances.get(neighbor, float('inf')):
                    distances[neighbor] = alt
                    previous[neighbor] = current
                    heapq.heappush(heap, (alt, neighbor.index, neighbor))

        path = []
        current = end_edge
        while current is not None:
            path.append(current)
            current = previous.get(current)
        path.reverse()
        return path if path and path[0] == start_edge else []


def find_shortest_path_faces(bm, start_face, end_face, use_3d=True):
    """Find shortest path between two faces via their centers."""
    face_graph = {f: set() for f in bm.faces}
    for f in bm.faces:
        for e in f.edges:
            for linked_f in e.link_faces:
                if linked_f != f:
                    face_graph[f].add(linked_f)

    if not use_3d:
        queue = deque([(start_face, [start_face])])
        visited = {start_face}
        while queue:
            current, path = queue.popleft()
            if current == end_face:
                return path
            for neighbor in face_graph[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))
        return []
    else:
        distances = {start_face: 0.0}
        previous = {}
        heap = [(0.0, start_face.index, start_face)]
        visited = set()
        while heap:
            dist, _, current = heapq.heappop(heap)
            if current in visited:
                continue
            visited.add(current)
            if current == end_face:
                break
            for neighbor in face_graph[current]:
                if neighbor in visited:
                    continue
                step = (current.calc_center_median() - neighbor.calc_center_median()).length
                alt = dist + step
                if alt < distances.get(neighbor, float('inf')):
                    distances[neighbor] = alt
                    previous[neighbor] = current
                    heapq.heappush(heap, (alt, neighbor.index, neighbor))

        path = []
        current = end_face
        while current is not None:
            path.append(current)
            current = previous.get(current)
        path.reverse()
        return path if path and path[0] == start_face else []
