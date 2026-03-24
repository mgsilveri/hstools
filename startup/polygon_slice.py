bl_info = {
    "name": "Polygon Slice Tool",
    "author": "Custom",
    "version": (1, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Edit Mode > Mesh > Polygon Slice",
    "description": "Modo-style interactive polygon slice tool",
    "category": "Mesh",
}

import bpy
import bmesh
import gpu
import mathutils
import time  # For debug timing
from mathutils import Vector
from gpu_extras.batch import batch_for_shader
from bpy_extras.view3d_utils import region_2d_to_location_3d, region_2d_to_vector_3d, location_3d_to_region_2d

class MESH_OT_modo_polygon_slice(bpy.types.Operator):
    bl_idname = "mesh.modo_polygon_slice"
    bl_label = "Polygon Slice"
    bl_options = {'REGISTER'}  # No UNDO - bmesh changes are already undoable via edit mode undo
    
    start: bpy.props.FloatVectorProperty(name="Start", size=3, subtype='XYZ')
    end: bpy.props.FloatVectorProperty(name="End", size=3, subtype='XYZ')
    axis: bpy.props.EnumProperty(
        name="Axis",
        items=[
            ('X', "X", "Plane perpendicular to X axis"),
            ('Y', "Y", "Plane perpendicular to Y axis"),
            ('Z', "Z", "Plane perpendicular to Z axis"),
            ('CUSTOM', "Custom", "Plane perpendicular to view"),
        ],
        default='Y'
    )
    split: bpy.props.BoolProperty(name="Split", default=False)
    cap_sections: bpy.props.BoolProperty(name="Cap Sections", default=True)
    gap: bpy.props.FloatProperty(name="Gap", default=0.0, min=0.0)
    infinite: bpy.props.BoolProperty(name="Infinite", default=True)
    use_selection: bpy.props.BoolProperty(name="Use Selection", default=False)
    weld_threshold: bpy.props.FloatProperty(name="Weld Threshold", default=0.0001, min=0.0, max=1.0)
    snap_edge_intersection: bpy.props.BoolProperty(name="Snap Edge Intersection", default=False)
    snap_edge_center: bpy.props.BoolProperty(name="Snap Edge Center", default=False)
    
    def modal(self, context, event):
        t_modal_start = time.perf_counter()
        
        # Safety check: ensure we're still in edit mode with valid object
        if not context.edit_object or context.mode != 'EDIT_MESH':
            self.cleanup(context)
            return {'CANCELLED'}
        
        # Track if we need redraw (only redraw when something visual changes)
        needs_redraw = False
        
        # Update header text
        edge_int = "ON" if self.snap_edge_intersection else "OFF"
        edge_center = "ON" if self.snap_edge_center else "OFF"
        if self.stage == 0:
            header = f"SLICE: Click 1st point | Edge Intersection:{edge_int} (X) | Edge Center:{edge_center} (C)"
        elif self.stage == 1:
            snap_status = "ON" if context.scene.tool_settings.use_snap else "OFF"
            header = f"SLICE: Click 2nd point | [{self.axis}] Z to change axis | Snap:{snap_status} (Ctrl) | Edge Intersection:{edge_int} (X) | Edge Center:{edge_center} (C)"
        else:
            inf = "ON" if self.infinite else "OFF"
            snap_status = "ON" if context.scene.tool_settings.use_snap else "OFF"
            header = f"SLICE: [{self.axis}] Z to change axis | Infinite:{inf} (I) | Snap:{snap_status} (Ctrl) | Edge Intersection:{edge_int} (X) | Edge Center:{edge_center} (C) | SPACE=Cut | ESC=Cancel"
        context.area.header_text_set(header)
        
        # Cancel
        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self.cleanup(context)
            return {'CANCELLED'}
        
        # Confirm
        if event.type == 'SPACE' and event.value == 'PRESS' and self.stage == 2:
            # Clear preview state before final execution
            self.restore_original_mesh(context)
            if hasattr(self, 'original_mesh_data'):
                self.original_mesh_data.free()
                delattr(self, 'original_mesh_data')

            self.execute_slice(context)
            self.cleanup(context)
            return {'FINISHED'}
        
        # Axis switching (only capture without modifiers)
        if event.value == 'PRESS' and not event.ctrl and not event.alt and not event.shift:
            if event.type == 'Z':
                # Cycle through axes: X -> Y -> Z -> CUSTOM -> X...
                axis_cycle = ['X', 'Y', 'Z', 'CUSTOM']
                current_index = axis_cycle.index(self.axis) if self.axis in axis_cycle else 0
                next_index = (current_index + 1) % len(axis_cycle)
                self.axis = axis_cycle[next_index]
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            elif event.type == 'I':
                self.infinite = not self.infinite
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            elif event.type == 'X':
                self.snap_edge_intersection = not self.snap_edge_intersection
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            elif event.type == 'C':
                self.snap_edge_center = not self.snap_edge_center
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
        
        # Left mouse for slice points (allow Ctrl for snapping, but block Alt and Shift)
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and not event.alt and not event.shift:
            if self.stage == 0:
                # Diagnostic: Check for hidden geometry
                obj = context.edit_object
                if obj:
                    bm = bmesh.from_edit_mesh(obj.data)
                    hidden_verts = sum(1 for v in bm.verts if hasattr(v, 'hide') and v.hide)
                    hidden_edges = sum(1 for e in bm.edges if hasattr(e, 'hide') and e.hide)
                    hidden_faces = sum(1 for f in bm.faces if hasattr(f, 'hide') and f.hide)
                    print(f"DEBUG: Hidden geometry - verts:{hidden_verts}, edges:{hidden_edges}, faces:{hidden_faces}")
                    
                    # Check if any faces are hidden
                    if hidden_faces > 0:
                        print(f"DEBUG: WARNING - {hidden_faces} hidden faces detected!")
                    
                self.stage = 1
                # Reset start point intersection flag when confirmed (keep visual as sphere)
                self._start_snapped_to_intersection = False
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            elif self.stage == 1:
                self.stage = 2
                self.start = self.start_pos[:]
                self.end = self.end_pos[:]
                # Reset end point intersection flag when confirmed (keep visual as sphere)
                self._end_snapped_to_intersection = False
                # Ensure preview is up-to-date when entering stage 2
                self.update_preview(context)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
        
        # Tweak points in stage 2
        if self.stage == 2 and event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if not event.alt and not event.shift and not event.ctrl:
                # Check which point is closer to click
                region = context.region
                rv3d = context.region_data
                coord = Vector((event.mouse_region_x, event.mouse_region_y))
                
                start_2d = location_3d_to_region_2d(region, rv3d, self.start_pos)
                end_2d = location_3d_to_region_2d(region, rv3d, self.end_pos)
                
                if start_2d and end_2d:
                    start_2d = Vector(start_2d)
                    end_2d = Vector(end_2d)
                    dist_start = (coord - start_2d).length
                    dist_end = (coord - end_2d).length
                    
                    if dist_start < 30:  # Click threshold for handles
                        self.tweaking = 'start'
                        return {'RUNNING_MODAL'}
                    elif dist_end < 30:
                        self.tweaking = 'end'
                        return {'RUNNING_MODAL'}
                    else:
                        # Check if click is on the line between start and end
                        line_vec = end_2d - start_2d
                        line_len = line_vec.length
                        if line_len > 0.001:
                            # Project click point onto line
                            click_vec = coord - start_2d
                            t = click_vec.dot(line_vec) / (line_len * line_len)
                            
                            # Check if projection is within the line segment
                            if 0 <= t <= 1:
                                # Calculate distance from click to line
                                closest_point = start_2d + line_vec * t
                                dist_to_line = (coord - closest_point).length
                                
                                if dist_to_line < 15:  # Click threshold for line
                                    self.tweaking = 'line'
                                    # Store initial positions and mouse pos for line dragging
                                    self._drag_start_mouse = coord.copy()
                                    self._drag_start_pos = self.start_pos.copy()
                                    self._drag_end_pos = self.end_pos.copy()
                                    return {'RUNNING_MODAL'}
        
        # Drag to reposition
        if self.stage == 2 and event.type == 'MOUSEMOVE' and hasattr(self, 'tweaking') and self.tweaking:
            pos = self.get_3d_pos(context, event)
            if self.tweaking == 'start':
                self.start_pos = pos
                self._start_snapped_to_intersection = getattr(self, '_current_snap_is_intersection', False)
            elif self.tweaking == 'end':
                self.end_pos = pos
                self._end_snapped_to_intersection = getattr(self, '_current_snap_is_intersection', False)
            elif self.tweaking == 'line':
                # Drag the entire line (both points) by calculating offset from initial mouse position
                region = context.region
                rv3d = context.region_data
                current_mouse = Vector((event.mouse_region_x, event.mouse_region_y))
                
                # Calculate the 3D offset based on mouse movement
                # Use the midpoint of the line as reference for depth
                mid_point = (self._drag_start_pos + self._drag_end_pos) / 2
                
                # Get 3D positions at the initial and current mouse locations at the same depth
                initial_3d = region_2d_to_location_3d(region, rv3d, self._drag_start_mouse, mid_point)
                current_3d = region_2d_to_location_3d(region, rv3d, current_mouse, mid_point)
                
                if initial_3d and current_3d:
                    offset = current_3d - initial_3d
                    self.start_pos = self._drag_start_pos + offset
                    self.end_pos = self._drag_end_pos + offset
            
            # Light throttle for stage 2 preview updates (50ms) to reduce lag
            now = time.perf_counter()
            last_preview = getattr(self, '_last_stage2_preview_time', 0)
            if (now - last_preview) > 0.05:  # 50ms throttle for smoother dragging
                self.update_preview(context)
                self._last_stage2_preview_time = now
            
            context.area.tag_redraw()
            
            return {'RUNNING_MODAL'}
        
        # Release tweaking
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and hasattr(self, 'tweaking') and self.tweaking:
            # Clean up line drag temporary variables
            if self.tweaking == 'line':
                if hasattr(self, '_drag_start_mouse'):
                    del self._drag_start_mouse
                if hasattr(self, '_drag_start_pos'):
                    del self._drag_start_pos
                if hasattr(self, '_drag_end_pos'):
                    del self._drag_end_pos
            # Force final preview update on release to ensure accuracy
            self.update_preview(context)
            self.tweaking = None
            # Reset intersection snap flags when releasing
            self._start_snapped_to_intersection = False
            self._end_snapped_to_intersection = False
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        
        # Mouse move for positioning (allow Ctrl for snapping, but block Alt and Shift)
        if event.type == 'MOUSEMOVE' and not event.alt and not event.shift:
            if self.stage == 0 or self.stage == 1:
                t0 = time.perf_counter()
                
                # Check if snapping should be enabled BEFORE restoring mesh
                scene_snap = context.scene.tool_settings.use_snap
                ctrl_pressed = event.ctrl
                should_snap = scene_snap != ctrl_pressed  # XOR logic
                
                t_restore = 0
                
                # Check throttle FIRST to avoid expensive restore when we won't update preview anyway
                now = time.perf_counter()
                last_preview = getattr(self, '_last_preview_time', 0)
                time_passed = (now - last_preview) > 0.2  # 200ms throttle
                
                # Only restore original mesh if we're going to snap AND preview will update
                if self.stage == 1 and should_snap and hasattr(self, 'original_mesh_data') and time_passed:
                    # Temporarily restore original mesh for accurate snapping
                    t_r0 = time.perf_counter()
                    self.restore_original_mesh(context)
                    t_restore = (time.perf_counter() - t_r0) * 1000

                t1 = time.perf_counter()
                pos = self.get_3d_pos(context, event)
                t2 = time.perf_counter()

                t_preview = 0
                if self.stage == 0:
                    self.start_pos = pos
                    self.end_pos = pos
                    # Track intersection snap state for start point
                    self._start_snapped_to_intersection = getattr(self, '_current_snap_is_intersection', False)
                    self._end_snapped_to_intersection = self._start_snapped_to_intersection
                elif self.stage == 1:
                    self.end_pos = pos
                    # Track intersection snap state for end point
                    self._end_snapped_to_intersection = getattr(self, '_current_snap_is_intersection', False)
                    # Use time_passed computed earlier for throttle
                    last_preview_pos = getattr(self, '_last_preview_pos', None)
                    
                    # Update preview if: 200ms passed AND position changed by at least 0.05 units
                    pos_changed = last_preview_pos is None or (pos - last_preview_pos).length > 0.05
                    
                    if pos_changed and time_passed:
                        t_p0 = time.perf_counter()
                        self.update_preview(context)
                        t_preview = (time.perf_counter() - t_p0) * 1000
                        self._last_preview_time = now
                        self._last_preview_pos = pos.copy()

                t3 = time.perf_counter()
                total_ms = (t3 - t0) * 1000
                get3d_ms = (t2 - t1) * 1000
                if total_ms > 5:  # Only log if > 5ms
                    print(f"DEBUG MODAL: total={total_ms:.1f}ms, restore={t_restore:.1f}ms, get3d={get3d_ms:.1f}ms, preview={t_preview:.1f}ms, stage={self.stage}, snap={should_snap}")
                
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
        
        # Pass through everything else (viewport navigation, etc.)
        # Invalidate snap cache when view might change (e.g., after middle mouse navigation)
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            self._snap_cache = None  # Rebuild cache after viewport changes
            self._cached_depsgraph = None  # Depsgraph may be stale too
        # Handle modifier+LMB viewport navigation (Shift/Alt + LMB only - NOT Ctrl which is snap toggle)
        if event.type == 'LEFTMOUSE' and (event.shift or event.alt) and not event.ctrl:
            tweaking = getattr(self, 'tweaking', None)
            if self.stage == 2 and not tweaking:
                # Only pass through for viewport nav when in stage 2 and not tweaking
                self._snap_cache = None  # Rebuild cache after viewport changes
                self._cached_depsgraph = None
        return {'PASS_THROUGH'}
    
    def get_3d_pos(self, context, event):
        # Safety check
        if not context.edit_object:
            return context.scene.cursor.location
        
        region = context.region
        rv3d = context.region_data
        coord = (event.mouse_region_x, event.mouse_region_y)
        
        # Calculate these FIRST
        view_vec = region_2d_to_vector_3d(region, rv3d, coord)
        ray_origin = region_2d_to_location_3d(region, rv3d, coord, Vector())
        
        # Check if snapping should be enabled:
        # 1. Scene snap is ON and Ctrl is NOT pressed, OR
        # 2. Scene snap is OFF and Ctrl IS pressed
        scene_snap = context.scene.tool_settings.use_snap
        ctrl_pressed = event.ctrl
        should_snap = scene_snap != ctrl_pressed  # XOR logic
        
        if should_snap:
            t_total = time.perf_counter()
            
            t0 = time.perf_counter()
            snap_pos = self.try_snap_to_vertex(context, coord)
            t1 = time.perf_counter()
            if snap_pos:
                print(f"DEBUG SNAP: vertex took {(t1-t0)*1000:.1f}ms, TOTAL {(t1-t_total)*1000:.1f}ms")
                self._current_snap_is_intersection = True  # Draw as crosshair
                return snap_pos
            
            # Edge center snapping (only if enabled)
            if self.snap_edge_center:
                snap_pos = self.try_snap_to_edge_center(context, coord)
                if snap_pos:
                    t_edge_center = time.perf_counter()
                    print(f"DEBUG SNAP: edge_center took {(t_edge_center-t1)*1000:.1f}ms, TOTAL {(t_edge_center-t_total)*1000:.1f}ms")
                    self._current_snap_is_intersection = True  # Draw as crosshair like intersections
                    return snap_pos
            
            t2 = time.perf_counter()
            t3 = t2  # Default if skipped
            # Edge-polygon intersection (only if enabled - can be slow)
            if self.snap_edge_intersection:
                snap_pos = self.try_snap_to_edge_closest_point(context, coord, ray_origin, view_vec)
                t3 = time.perf_counter()
                if snap_pos:
                    print(f"DEBUG SNAP: vertex={(t1-t0)*1000:.1f}ms, edge_isect={(t3-t2)*1000:.1f}ms, TOTAL {(t3-t_total)*1000:.1f}ms")
                    self._current_snap_is_intersection = True  # Mark as intersection snap
                    return snap_pos
            
            print(f"DEBUG SNAP (no hit): vertex={(t1-t0)*1000:.1f}ms, edge_isect={(t3-t2)*1000:.1f}ms, TOTAL {(t3-t_total)*1000:.1f}ms")
        
        # No snap - reset flag
        self._current_snap_is_intersection = False
        
        obj = context.edit_object
        mx = obj.matrix_world
        mx_inv = mx.inverted()
        
        ray_orig_local = mx_inv @ ray_origin
        ray_dir_local = mx_inv.to_3x3() @ view_vec
        
        hit, loc, norm, idx = obj.ray_cast(ray_orig_local, ray_dir_local)
        
        if hit:
            return mx @ loc
        
        return region_2d_to_location_3d(region, rv3d, coord, context.scene.cursor.location)
    
    def is_point_occluded(self, context, world_co):
        """Check if a point is occluded by geometry (not visible to camera)

        Args:
            context: Blender context
            world_co: World coordinates of the point to check
        """
        # Check if x-ray mode is enabled
        shading = context.space_data.shading

        # Only check shading.show_xray - this is the actual x-ray toggle (Alt+Z)
        if shading.show_xray:
            return False  # X-ray enabled, nothing is occluded

        # Perform ray cast from camera to point
        region = context.region
        rv3d = context.region_data

        # Get ray from viewport to the point
        screen_co = location_3d_to_region_2d(region, rv3d, world_co)
        if not screen_co:
            return True  # If can't project to screen, consider occluded

        # Get view vector and ray origin from the viewport camera position
        view_vec = region_2d_to_vector_3d(region, rv3d, screen_co)

        # For perspective view, ray origin is the camera position
        # For orthographic, ray origin is behind the point
        if rv3d.is_perspective:
            ray_origin = rv3d.view_matrix.inverted().translation
        else:
            # For ortho view, start from far behind the point
            ray_origin = world_co - view_vec * 10000

        # Calculate distance from ray origin to the point
        target_distance = (world_co - ray_origin).length

        # Use a relative tolerance based on the distance
        tolerance = max(0.001, target_distance * 0.001)  # 0.1% of distance, minimum 1mm

        # Use cached depsgraph if available, otherwise get a fresh one
        if not hasattr(self, '_cached_depsgraph') or self._cached_depsgraph is None:
            self._cached_depsgraph = context.evaluated_depsgraph_get()
        depsgraph = self._cached_depsgraph

        # Cast ray from camera toward infinity to find first hit
        result = context.scene.ray_cast(depsgraph, ray_origin, view_vec)
        hit, location, _normal, _index, hit_obj, _matrix = result

        if hit and hit_obj:
            hit_distance = (location - ray_origin).length
            distance_to_target = (location - world_co).length

            # Check if the hit point is very close to our target point
            # This handles the case where we're raycasting to a vertex on the surface
            if distance_to_target < tolerance:
                return False

            # If something is closer to the camera than our target point, it's occluded
            if hit_distance < target_distance - tolerance:
                return True  # Something is in front of our point

        return False

    def try_snap_to_vertex(self, context, coord):
        """Snap to nearest vertex using cached geometry."""
        snap_distance = 20
        cursor_vec = Vector((coord[0], coord[1]))
        
        # Use cached geometry
        cache = self._get_snap_cache(context, coord)
        if not cache or not cache['vertices']:
            return None
        
        closest_dist = snap_distance
        best_candidate = None
        
        for world_co, screen_co, obj, is_edit, vert in cache['vertices']:
            dist = (cursor_vec - Vector(screen_co)).length
            if dist < closest_dist:
                closest_dist = dist
                best_candidate = world_co
        
        # Only do expensive occlusion check on the single best candidate
        if best_candidate is not None:
            if not self.is_point_occluded(context, best_candidate):
                return best_candidate
        
        return None

    def _get_snap_cache(self, context, coord):
        """Get or build the snap cache, rebuilding if cursor moved significantly."""
        # Check if we have a valid cache
        if self._snap_cache is not None:
            # Check if cursor moved significantly (rebuild if moved > 150 pixels)
            cached_coord = self._snap_cache.get('coord', (0, 0))
            dist = ((coord[0] - cached_coord[0])**2 + (coord[1] - cached_coord[1])**2)**0.5
            # Also rebuild if edge_intersection or edge_center setting changed
            cached_edge_int = self._snap_cache.get('edge_intersection', False)
            cached_edge_center = self._snap_cache.get('edge_center', False)
            if dist < 150 and cached_edge_int == self.snap_edge_intersection and cached_edge_center == self.snap_edge_center:
                return self._snap_cache
            if dist >= 150:
                print(f"DEBUG CACHE: Rebuilding cache, cursor moved {dist:.0f}px")
            else:
                print(f"DEBUG CACHE: Rebuilding cache, edge settings changed")
        else:
            print("DEBUG CACHE: Building initial cache")
        
        # Build new cache
        t0 = time.perf_counter()
        self._snap_cache = self._build_snap_cache(context, coord)
        t1 = time.perf_counter()
        v_count = len(self._snap_cache.get('vertices', []))
        e_count = len(self._snap_cache.get('edges', []))
        f_count = len(self._snap_cache.get('faces', []))
        print(f"DEBUG CACHE: Built in {(t1-t0)*1000:.1f}ms - {v_count} verts, {e_count} edges, {f_count} faces")
        return self._snap_cache

    def try_snap_to_edge_center(self, context, coord):
        """Snap to edge midpoints using cached geometry."""
        snap_distance = 20
        cursor_vec = Vector((coord[0], coord[1]))
        
        # Use cached geometry
        cache = self._get_snap_cache(context, coord)
        if not cache or not cache['edge_centers']:
            return None
        
        closest_dist = snap_distance
        best_candidate = None
        
        for world_co, screen_co in cache['edge_centers']:
            dist = (cursor_vec - Vector(screen_co)).length
            if dist < closest_dist:
                closest_dist = dist
                best_candidate = world_co
        
        # Only do expensive occlusion check on the single best candidate
        if best_candidate is not None:
            if not self.is_point_occluded(context, best_candidate):
                return best_candidate
        
        return None
    
    def _build_snap_cache(self, context, coord):
        """Build a unified cache of nearby snap targets in screen space.
        
        Only caches geometry that is within snap range of the cursor to avoid
        iterating all geometry on every mouse move.
        
        When edge intersection is disabled, only caches vertices (much faster).
        
        Returns dict with 'vertices', 'edge_centers', 'edges', 'faces'
        """
        region = context.region
        rv3d = context.region_data
        view_dir = rv3d.view_rotation @ Vector((0, 0, -1))
        cursor_2d = Vector((coord[0], coord[1]))
        
        # Screen-space search radius for snap candidates
        # Use larger radius so cache stays valid longer as cursor moves
        search_radius = 200  # pixels - covers cursor movement up to 150px before rebuild
        
        cached_verts = []      # (world_co, screen_co, obj, is_edit)
        cached_edge_centers = []  # (world_co, screen_co)
        cached_edges = []      # For edge-polygon intersection
        cached_faces = []      # For edge-polygon intersection
        
        # Cache edges/faces if edge intersection is enabled
        # Cache edge centers if edge center snapping OR edge intersection is enabled
        need_edges_and_faces = self.snap_edge_intersection
        need_edge_centers = self.snap_edge_center or self.snap_edge_intersection
        
        for obj in context.visible_objects:
            if obj.type != 'MESH':
                continue
            
            mx = obj.matrix_world
            is_edit_mode = obj.mode == 'EDIT'
            normal_matrix = mx.to_3x3() if is_edit_mode else mx.to_3x3().inverted().transposed()
            
            if is_edit_mode:
                bm = bmesh.from_edit_mesh(obj.data)
                
                # Cache vertices near cursor
                for v in bm.verts:
                    if v.hide:
                        continue
                    world_co = mx @ v.co
                    screen_co = location_3d_to_region_2d(region, rv3d, world_co)
                    if screen_co:
                        dist = (cursor_2d - Vector(screen_co)).length
                        if dist < search_radius:
                            # Check backface culling
                            has_front_face = False
                            if v.link_faces:
                                for face in v.link_faces:
                                    face_normal = normal_matrix @ face.normal
                                    if face_normal.dot(view_dir) < 0:
                                        has_front_face = True
                                        break
                            else:
                                has_front_face = True  # No faces = always visible
                            
                            if has_front_face:
                                cached_verts.append((world_co, screen_co, obj, True, v))
                
                # Cache edge centers if edge center snapping or edge intersection is enabled
                if need_edge_centers:
                    for i, edge in enumerate(bm.edges):
                        if edge.hide:
                            continue
                        v1_world = mx @ edge.verts[0].co
                        v2_world = mx @ edge.verts[1].co
                        
                        v1_screen = location_3d_to_region_2d(region, rv3d, v1_world)
                        v2_screen = location_3d_to_region_2d(region, rv3d, v2_world)
                        
                        if v1_screen and v2_screen:
                            # Check if any part of edge is near cursor
                            center_screen = (Vector(v1_screen) + Vector(v2_screen)) * 0.5
                            min_dist = min(
                                (cursor_2d - Vector(v1_screen)).length,
                                (cursor_2d - Vector(v2_screen)).length,
                                (cursor_2d - center_screen).length
                            )
                            
                            if min_dist < search_radius:
                                # Check backface culling for edge
                                is_visible = True
                                if edge.link_faces:
                                    is_visible = False
                                    for face in edge.link_faces:
                                        face_normal = normal_matrix @ face.normal
                                        if face_normal.dot(view_dir) < 0:
                                            is_visible = True
                                            break
                                
                                if is_visible:
                                    # Edge center
                                    center_world = (v1_world + v2_world) * 0.5
                                    cached_edge_centers.append((center_world, center_screen))
                                    
                                    # If edge intersection is also needed, cache full edge data
                                    if need_edges_and_faces:
                                        edge_vert_indices = (edge.verts[0].index, edge.verts[1].index)
                                        cached_edges.append((v1_world, v2_world, obj, i, edge_vert_indices))
                
                # Cache faces near cursor (for edge-polygon intersection only)
                if need_edges_and_faces:
                    face_search_radius = search_radius * 2
                    for i, face in enumerate(bm.faces):
                        if face.hide:
                            continue
                        face_normal = normal_matrix @ face.normal
                        if face_normal.dot(view_dir) < 0:  # Front-facing
                            # Check if face center is near cursor
                            face_center_world = mx @ face.calc_center_median()
                            face_center_screen = location_3d_to_region_2d(region, rv3d, face_center_world)
                            if face_center_screen:
                                dist = (cursor_2d - Vector(face_center_screen)).length
                                if dist < face_search_radius:
                                    face_verts = [mx @ v.co for v in face.verts]
                                    face_vert_indices = set(v.index for v in face.verts)
                                    cached_faces.append((face_verts, face_normal.normalized(), obj, i, face_vert_indices))
            else:
                mesh = obj.data
                verts = mesh.vertices
                
                # Cache vertices near cursor
                for v in verts:
                    world_co = mx @ v.co
                    screen_co = location_3d_to_region_2d(region, rv3d, world_co)
                    if screen_co:
                        dist = (cursor_2d - Vector(screen_co)).length
                        if dist < search_radius:
                            cached_verts.append((world_co, screen_co, obj, False, None))
                
                # Cache edge centers if edge center snapping or edge intersection is enabled
                if need_edge_centers:
                    for i, edge in enumerate(mesh.edges):
                        v1_world = mx @ verts[edge.vertices[0]].co
                        v2_world = mx @ verts[edge.vertices[1]].co
                        
                        v1_screen = location_3d_to_region_2d(region, rv3d, v1_world)
                        v2_screen = location_3d_to_region_2d(region, rv3d, v2_world)
                        
                        if v1_screen and v2_screen:
                            center_screen = (Vector(v1_screen) + Vector(v2_screen)) * 0.5
                            min_dist = min(
                                (cursor_2d - Vector(v1_screen)).length,
                                (cursor_2d - Vector(v2_screen)).length,
                                (cursor_2d - center_screen).length
                            )
                            
                            if min_dist < search_radius:
                                center_world = (v1_world + v2_world) * 0.5
                                cached_edge_centers.append((center_world, center_screen))
                                
                                # If edge intersection is also needed, cache full edge data
                                if need_edges_and_faces:
                                    edge_vert_indices = (edge.vertices[0], edge.vertices[1])
                                    cached_edges.append((v1_world, v2_world, obj, i, edge_vert_indices))
                
                # Cache faces near cursor (for edge-polygon intersection only)
                if need_edges_and_faces:
                    normal_matrix_obj = mx.to_3x3().inverted().transposed()
                    face_search_radius = search_radius * 2
                    for i, poly in enumerate(mesh.polygons):
                        face_normal = normal_matrix_obj @ poly.normal
                        if face_normal.dot(view_dir) < 0:
                            # Check if face center is near cursor
                            face_center_world = mx @ poly.center
                            face_center_screen = location_3d_to_region_2d(region, rv3d, face_center_world)
                            if face_center_screen:
                                dist = (cursor_2d - Vector(face_center_screen)).length
                                if dist < face_search_radius:
                                    face_verts = [mx @ verts[vi].co for vi in poly.vertices]
                                    face_vert_indices = set(poly.vertices)
                                    cached_faces.append((face_verts, face_normal.normalized(), obj, i, face_vert_indices))
        
        return {
            'vertices': cached_verts,
            'edge_centers': cached_edge_centers,
            'edges': cached_edges,
            'faces': cached_faces,
            'coord': coord,  # Store coord to detect if cursor moved significantly
            'edge_intersection': self.snap_edge_intersection,  # Store setting for cache invalidation
            'edge_center': self.snap_edge_center  # Store setting for cache invalidation
        }
    
    def try_snap_to_edge_closest_point(self, context, coord, ray_origin, view_vec):
        """Snap to edge-polygon intersections (Modo-style).
        
        Finds where edges pierce through polygons/faces in 3D space.
        Uses cached geometry for performance.
        """
        region = context.region
        rv3d = context.region_data
        
        snap_distance = 20
        closest_snap = None
        closest_screen_dist = snap_distance
        
        cursor_2d = Vector(coord)
        
        # Use cached geometry
        cache = self._get_snap_cache(context, coord)
        if not cache:
            return None
        
        cached_edges = cache['edges']
        cached_faces = cache['faces']
        
        if not cached_edges or not cached_faces:
            return None
        
        # Test nearby edges (already filtered by cache) against faces
        for v1_world, v2_world, edge_obj, edge_idx, edge_vert_indices in cached_edges:
            edge_vec = v2_world - v1_world
            edge_len_sq = edge_vec.length_squared
            if edge_len_sq < 0.00000001:
                continue
            
            # Compute edge bounding box for fast face rejection
            edge_min = Vector((min(v1_world.x, v2_world.x), min(v1_world.y, v2_world.y), min(v1_world.z, v2_world.z)))
            edge_max = Vector((max(v1_world.x, v2_world.x), max(v1_world.y, v2_world.y), max(v1_world.z, v2_world.z)))
            
            for face_verts, face_normal, face_obj, face_idx, face_vert_indices in cached_faces:
                # Skip if edge is connected to the face (shares vertices)
                if edge_obj == face_obj:
                    if edge_vert_indices[0] in face_vert_indices or edge_vert_indices[1] in face_vert_indices:
                        continue
                
                # Quick bounding box rejection - compute face bbox
                face_min_x = min(v.x for v in face_verts)
                face_max_x = max(v.x for v in face_verts)
                face_min_y = min(v.y for v in face_verts)
                face_max_y = max(v.y for v in face_verts)
                face_min_z = min(v.z for v in face_verts)
                face_max_z = max(v.z for v in face_verts)
                
                # Check if bounding boxes overlap (with small margin)
                margin = 0.001
                if (edge_max.x < face_min_x - margin or edge_min.x > face_max_x + margin or
                    edge_max.y < face_min_y - margin or edge_min.y > face_max_y + margin or
                    edge_max.z < face_min_z - margin or edge_min.z > face_max_z + margin):
                    continue
                
                # Use the first vertex of the face as a point on the plane
                plane_co = face_verts[0]
                plane_no = face_normal
                
                # Find intersection of edge (as infinite line) with the face plane
                isect = mathutils.geometry.intersect_line_plane(
                    v1_world, v2_world, plane_co, plane_no
                )
                
                if isect is None:
                    continue
                
                # Check if intersection point is within the edge segment
                to_isect = isect - v1_world
                
                # Project to find t
                t = to_isect.dot(edge_vec) / edge_len_sq
                
                # Must be strictly within the edge (not at endpoints)
                if t <= 0.001 or t >= 0.999:
                    continue
                
                # Check if intersection point is inside the polygon
                is_inside = False
                if len(face_verts) == 3:
                    is_inside = self._point_in_triangle(isect, face_verts[0], face_verts[1], face_verts[2], plane_no)
                elif len(face_verts) == 4:
                    is_inside = (self._point_in_triangle(isect, face_verts[0], face_verts[1], face_verts[2], plane_no) or
                                 self._point_in_triangle(isect, face_verts[0], face_verts[2], face_verts[3], plane_no))
                else:
                    # N-gon - fan triangulation from first vertex
                    for i in range(1, len(face_verts) - 1):
                        if self._point_in_triangle(isect, face_verts[0], face_verts[i], face_verts[i+1], plane_no):
                            is_inside = True
                            break
                
                if not is_inside:
                    continue
                
                # Project intersection to screen space and check distance to cursor
                isect_screen = location_3d_to_region_2d(region, rv3d, isect)
                if isect_screen is None:
                    continue
                
                screen_dist = (Vector(isect_screen) - cursor_2d).length
                if screen_dist >= closest_screen_dist:
                    continue
                
                # This is our best candidate so far (defer occlusion check)
                closest_screen_dist = screen_dist
                closest_snap = isect
        
        # Only do expensive occlusion check on the single best candidate
        if closest_snap is not None:
            if self.is_point_occluded(context, closest_snap):
                return None
        
        return closest_snap
    
    def _point_in_triangle(self, p, v0, v1, v2, normal):
        """Check if point p lies inside triangle v0-v1-v2.
        
        Uses cross product method.
        Assumes p is already on the plane of the triangle.
        """
        edge0 = v1 - v0
        edge1 = v2 - v1
        edge2 = v0 - v2
        
        c0 = p - v0
        c1 = p - v1
        c2 = p - v2
        
        cross0 = edge0.cross(c0)
        cross1 = edge1.cross(c1)
        cross2 = edge2.cross(c2)
        
        dot0 = cross0.dot(normal)
        dot1 = cross1.dot(normal)
        dot2 = cross2.dot(normal)
        
        epsilon = -0.0001
        return (dot0 >= epsilon and dot1 >= epsilon and dot2 >= epsilon) or \
               (dot0 <= -epsilon and dot1 <= -epsilon and dot2 <= -epsilon)
    
    def try_snap_to_edge(self, context, coord, ray_origin, view_vec):
        """Snap to nearest point on edges using cached geometry."""
        # Safety check
        if not context.edit_object:
            return None

        region = context.region
        rv3d = context.region_data

        snap_distance = 20
        best_candidate = None
        closest_dist = snap_distance
        
        cursor_vec = Vector((coord[0], coord[1]))
        ray_end = ray_origin + view_vec * 1000

        # Use cached geometry
        cache = self._get_snap_cache(context, coord)
        if not cache or not cache['edges']:
            return None

        # Check edge intersections using cached edges
        for v1_world, v2_world, edge_obj, edge_idx, edge_vert_indices in cache['edges']:
            # Only snap to edit object's edges for this function
            if edge_obj != context.edit_object:
                continue

            # Ray-line intersection
            point = mathutils.geometry.intersect_line_line(
                ray_origin, ray_end,
                v1_world, v2_world
            )

            if point:
                edge_point = point[1]
                # Check if point is on edge segment
                edge_vec = v2_world - v1_world
                edge_len_sq = edge_vec.length_squared
                if edge_len_sq > 0.0001:
                    t = (edge_point - v1_world).dot(edge_vec) / edge_len_sq
                    if 0 <= t <= 1:
                        screen_co = location_3d_to_region_2d(region, rv3d, edge_point)
                        if screen_co:
                            dist = (cursor_vec - Vector(screen_co)).length
                            if dist < closest_dist:
                                closest_dist = dist
                                best_candidate = edge_point

        # Only do expensive occlusion check on the single best candidate
        if best_candidate is not None:
            if not self.is_point_occluded(context, best_candidate):
                return best_candidate

        return None
    
    def has_face_selection(self, context):
        """Check if any faces are selected"""
        if not context.edit_object or context.mode != 'EDIT_MESH':
            return False

        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        return any(f.select for f in bm.faces)

    def get_plane_normal(self, context):
        line_vec = (self.end_pos - self.start_pos)
        if line_vec.length < 0.001:
            line_vec = Vector((1, 0, 0))
        
        if self.axis == 'X':
            axis_vec = Vector((1, 0, 0))
            normal = axis_vec.cross(line_vec)
            if normal.length < 0.001:
                normal = Vector((0, 0, 1))
        elif self.axis == 'Y':
            axis_vec = Vector((0, 1, 0))
            normal = axis_vec.cross(line_vec)
            if normal.length < 0.001:
                normal = Vector((1, 0, 0))
        elif self.axis == 'Z':
            axis_vec = Vector((0, 0, 1))
            normal = axis_vec.cross(line_vec)
            if normal.length < 0.001:
                normal = Vector((1, 0, 0))
        else:  # CUSTOM
            rv3d = context.space_data.region_3d
            view_rot = rv3d.view_rotation
            return view_rot @ Vector((0, 0, -1))
        
        return normal.normalized()
    
    def get_plane_tangents(self, context):
        plane_no = self.get_plane_normal(context)
        
        if abs(plane_no.x) < abs(plane_no.y) and abs(plane_no.x) < abs(plane_no.z):
            tangent = Vector((1, 0, 0)).cross(plane_no)
        elif abs(plane_no.y) < abs(plane_no.z):
            tangent = Vector((0, 1, 0)).cross(plane_no)
        else:
            tangent = Vector((0, 0, 1)).cross(plane_no)
        
        if tangent.length > 0.001:
            tangent = tangent.normalized()
        else:
            tangent = Vector((1, 0, 0))
            
        bitangent = plane_no.cross(tangent).normalized()
        return tangent, bitangent
    
    def get_viewport_plane_size(self, context, plane_center, direction):
        """Calculate the size needed for the plane to fill the entire viewport along a given direction.
        
        This computes how far the plane should extend along the 'direction' axis to cover
        the full visible viewport area, adapting to both perspective and orthographic views
        as well as zoom level changes.
        
        Args:
            context: Blender context
            plane_center: The center point of the plane in world coordinates
            direction: The normalized direction vector to extend along
        
        Returns:
            float: The half-size needed to fill the viewport along the direction
        """
        # Get region from the space_data for the 3D view
        region = None
        rv3d = None
        
        # Try to get region from context.region first
        if hasattr(context, 'region') and context.region:
            region = context.region
        
        # Try to get rv3d from space_data
        if hasattr(context, 'space_data') and context.space_data:
            if hasattr(context.space_data, 'region_3d'):
                rv3d = context.space_data.region_3d
        
        # Fallback: search through areas
        if region is None or rv3d is None:
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    for r in area.regions:
                        if r.type == 'WINDOW':
                            region = r
                            break
                    if hasattr(area.spaces.active, 'region_3d'):
                        rv3d = area.spaces.active.region_3d
                    break
        
        if region is None or rv3d is None:
            # Fallback to a reasonable default size
            return 100.0
        
        # Get the four corners of the viewport in 2D
        corners_2d = [
            (0, 0),                          # Bottom-left
            (region.width, 0),               # Bottom-right
            (region.width, region.height),   # Top-right
            (0, region.height)               # Top-left
        ]
        
        # Project viewport corners to 3D rays and find intersection with the plane
        # containing plane_center with normal = view direction
        view_vec = rv3d.view_rotation @ Vector((0, 0, -1))
        
        max_extent = 0.0
        
        for corner in corners_2d:
            # Get ray from viewport corner
            ray_dir = region_2d_to_vector_3d(region, rv3d, corner)
            ray_origin = region_2d_to_location_3d(region, rv3d, corner, plane_center)
            
            # For orthographic view, ray_origin is already on a plane perpendicular to view
            # For perspective, we need to find where the ray intersects the plane at plane_center depth
            if rv3d.is_perspective:
                # Find intersection of ray with plane at plane_center depth
                # Plane equation: (P - plane_center) · view_vec = 0
                denom = ray_dir.dot(view_vec)
                if abs(denom) > 0.0001:
                    t = (plane_center - ray_origin).dot(view_vec) / denom
                    corner_3d = ray_origin + ray_dir * t
                else:
                    corner_3d = ray_origin
            else:
                # Orthographic: project corner directly to the plane depth
                corner_3d = ray_origin
            
            # Measure distance from plane_center along the direction axis
            offset = corner_3d - plane_center
            extent = abs(offset.dot(direction))
            max_extent = max(max_extent, extent)
        
        # Ensure we have at least a minimum size, and add margin
        if max_extent < 0.001:
            return 100.0
        
        # Add a margin to ensure we cover beyond visible edges (20% extra)
        return max_extent * 1.2
    
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D' or context.mode != 'EDIT_MESH':
            self.report({'WARNING'}, "Must be in Edit Mode")
            return {'CANCELLED'}

        # Auto-detect if we should use selection
        self.use_selection = self.has_face_selection(context)
        
        # Auto-detect initial axis based on camera view direction
        rv3d = context.region_data
        view_dir = rv3d.view_rotation @ Vector((0, 0, -1))
        
        # Find which axis is most aligned with view direction
        abs_x = abs(view_dir.x)
        abs_y = abs(view_dir.y)
        abs_z = abs(view_dir.z)
        
        if abs_x > abs_y and abs_x > abs_z:
            self.axis = 'X'
        elif abs_y > abs_z:
            self.axis = 'Y'
        else:
            self.axis = 'Z'

        cursor = context.scene.cursor.location
        self.start_pos = cursor.copy()
        self.end_pos = cursor.copy()
        self.stage = 0
        self.tweaking = None
        self._snap_cache = None  # Unified snap cache
        self._cached_depsgraph = None  # Cache for occlusion ray casting
        self._start_snapped_to_intersection = False  # Track if start point is snapped to intersection
        self._end_snapped_to_intersection = False  # Track if end point is snapped to intersection
        self._rv3d = context.region_data  # Store region_3d for draw callback

        args = (self, context)
        self.draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback_3d, args, 'WINDOW', 'POST_VIEW')

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def draw_callback_3d(self, context, *args):
        t_draw_start = time.perf_counter()
        
        if not hasattr(self, 'start_pos') or not hasattr(self, 'end_pos'):
            return
        
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(3.0)
        
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        shader.bind()
        
        # Draw cross line
        verts = [(self.start_pos.x, self.start_pos.y, self.start_pos.z),
                 (self.end_pos.x, self.end_pos.y, self.end_pos.z)]
        batch = batch_for_shader(shader, 'LINES', {"pos": verts})
        shader.uniform_float("color", (1.0, 0.5, 0.0, 1.0))
        batch.draw(shader)
        
        # Draw endpoints - crosshair when snapped to intersection, sphere otherwise
        # Use depth test settings for proper rendering like interactive_mirror
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)
        
        # Calculate view-independent handle sizes (constant screen size regardless of zoom)
        # Base size at "normal" viewing distance, then scale with view_distance
        base_size = 0.05  # Smaller base size
        handle_size = base_size  # Default fallback
        
        # Try to adapt to viewport zoom using stored rv3d
        rv3d = getattr(self, '_rv3d', None)
        if rv3d:
            # view_distance represents zoom level - larger = zoomed out more
            view_distance = rv3d.view_distance
            # Scale proportionally: at view_distance=10, use base_size
            # Zooming in (smaller view_distance) = smaller handle
            # Zooming out (larger view_distance) = larger handle
            handle_size = base_size * (view_distance / 10.0)
            # Clamp to reasonable range
            handle_size = max(0.005, min(handle_size, 5.0))
        
        outline_size = handle_size * 1.16
        handle_color = (0.0, 1.0, 1.0, 1.0)  # Cyan
        outline_color = (0.0, 0.0, 0.0, 1.0)  # Black
        
        # Check intersection snap states
        start_snapped = getattr(self, '_start_snapped_to_intersection', False)
        end_snapped = getattr(self, '_end_snapped_to_intersection', False)
        
        # Draw start point
        if start_snapped:
            self.draw_point_crosshair(shader, self.start_pos, handle_size * 1.5, outline_color, handle_color)
        else:
            self.draw_point_sphere(shader, self.start_pos, outline_size, outline_color)
            self.draw_point_sphere(shader, self.start_pos, handle_size, handle_color)
        
        # Draw end point
        if end_snapped:
            self.draw_point_crosshair(shader, self.end_pos, handle_size * 1.5, outline_color, handle_color)
        else:
            self.draw_point_sphere(shader, self.end_pos, outline_size, outline_color)
            self.draw_point_sphere(shader, self.end_pos, handle_size, handle_color)
        
        # Draw plane
        plane_co = (self.start_pos + self.end_pos) / 2
        plane_no = self.get_plane_normal(context)
        
        line_vec = self.end_pos - self.start_pos
        line_length = line_vec.length
        
        # Use the actual line direction as tangent so plane passes through both handles
        if line_length > 0.001:
            tangent = line_vec.normalized()
        else:
            tangent = Vector((1, 0, 0))
        
        # Bitangent should be the constrained axis direction (which lies in the plane)
        # since plane_no = axis.cross(line_vec), the axis is in the plane
        if self.axis == 'X':
            bitangent = Vector((1, 0, 0))
        elif self.axis == 'Y':
            bitangent = Vector((0, 1, 0))
        elif self.axis == 'Z':
            bitangent = Vector((0, 0, 1))
        else:  # CUSTOM - use view direction
            rv3d = context.space_data.region_3d
            view_up = rv3d.view_rotation @ Vector((0, 1, 0))
            # Project view up onto the plane
            bitangent = view_up - plane_no * view_up.dot(plane_no)
            if bitangent.length > 0.001:
                bitangent = bitangent.normalized()
            else:
                bitangent = Vector((0, 0, 1))
        
        # Calculate viewport-filling size for the perpendicular (bitangent) direction
        # This makes the plane extend to fill the entire visible viewport on that axis
        size_along = line_length / 2
        size_perp = self.get_viewport_plane_size(context, plane_co, bitangent)
        
        plane_verts = [
            plane_co + tangent * size_along + bitangent * size_perp,
            plane_co - tangent * size_along + bitangent * size_perp,
            plane_co - tangent * size_along - bitangent * size_perp,
            plane_co + tangent * size_along - bitangent * size_perp,
        ]
        plane_verts = [(v.x, v.y, v.z) for v in plane_verts]
        
        batch = batch_for_shader(shader, 'TRI_FAN', {"pos": plane_verts})
        shader.uniform_float("color", (1.0, 0.7, 0.0, 0.25))
        batch.draw(shader)
        
        batch = batch_for_shader(shader, 'LINE_LOOP', {"pos": plane_verts})
        shader.uniform_float("color", (1.0, 0.5, 0.0, 0.8))
        gpu.state.line_width_set(2.0)
        batch.draw(shader)
        
        gpu.state.blend_set('NONE')
        
        t_draw_end = time.perf_counter()
        draw_ms = (t_draw_end - t_draw_start) * 1000
        if draw_ms > 5:  # Only log if > 5ms
            print(f"DEBUG DRAW: {draw_ms:.1f}ms")

    def draw_point_sphere(self, shader, center, radius, color):
        """Draw a point as a UV sphere that works with both OpenGL and Vulkan"""
        import math
        
        segments = 24  # Horizontal segments
        rings = 12     # Vertical rings
        
        # Generate sphere vertices
        vertices = []
        for ring in range(rings + 1):
            theta = (ring / rings) * math.pi  # 0 to pi
            sin_theta = math.sin(theta)
            cos_theta = math.cos(theta)
            
            for seg in range(segments):
                phi = (seg / segments) * 2 * math.pi  # 0 to 2pi
                sin_phi = math.sin(phi)
                cos_phi = math.cos(phi)
                
                x = center.x + radius * sin_theta * cos_phi
                y = center.y + radius * sin_theta * sin_phi
                z = center.z + radius * cos_theta
                
                vertices.append((x, y, z))
        
        # Generate triangle indices
        indices = []
        for ring in range(rings):
            for seg in range(segments):
                # Current quad indices
                current = ring * segments + seg
                next_seg = ring * segments + ((seg + 1) % segments)
                next_ring = (ring + 1) * segments + seg
                next_both = (ring + 1) * segments + ((seg + 1) % segments)
                
                # Two triangles per quad
                indices.append((current, next_ring, next_seg))
                indices.append((next_seg, next_ring, next_both))
        
        # Build triangle vertices for batch rendering
        tri_verts = [vertices[i] for tri in indices for i in tri]
        
        # Disable depth test to ensure proper drawing order
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)
        
        batch = batch_for_shader(shader, 'TRIS', {"pos": tri_verts})
        shader.uniform_float("color", color)
        batch.draw(shader)
    
    def draw_point_crosshair(self, shader, center, size, outline_color, fill_color):
        """Draw a 3D crosshair at the given position (used when snapped to intersection)"""
        # Create 3D crosshair with three perpendicular lines
        crosshair_verts = []
        # X axis
        crosshair_verts.append(center - Vector((size, 0, 0)))
        crosshair_verts.append(center + Vector((size, 0, 0)))
        # Y axis
        crosshair_verts.append(center - Vector((0, size, 0)))
        crosshair_verts.append(center + Vector((0, size, 0)))
        # Z axis
        crosshair_verts.append(center - Vector((0, 0, size)))
        crosshair_verts.append(center + Vector((0, 0, size)))
        
        # Draw black outline
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(4.0)
        
        crosshair_batch = batch_for_shader(shader, 'LINES', {"pos": crosshair_verts})
        shader.uniform_float("color", outline_color)
        crosshair_batch.draw(shader)
        
        # Draw colored crosshair on top
        gpu.state.line_width_set(2.5)
        shader.uniform_float("color", fill_color)
        crosshair_batch.draw(shader)
        
        gpu.state.line_width_set(1.0)
    
    def cleanup(self, context):
        # Only restore if we're still in edit mode with a valid object
        if context.edit_object and context.mode == 'EDIT_MESH':
            self.restore_original_mesh(context)
        
        # Clean up stored mesh data
        if hasattr(self, 'original_mesh_data'):
            self.original_mesh_data.free()
            delattr(self, 'original_mesh_data')
        
        # Clear caches
        self._snap_cache = None
        self._cached_depsgraph = None

        if hasattr(self, 'draw_handler') and self.draw_handler:
            bpy.types.SpaceView3D.draw_handler_remove(self.draw_handler, 'WINDOW')
            self.draw_handler = None
        context.area.header_text_set(None)
        context.area.tag_redraw()
    
    def update_preview(self, context):
        """Apply temporary bisect to show preview"""
        # Safety check
        if not context.edit_object or context.mode != 'EDIT_MESH':
            return

        if not hasattr(self, 'original_mesh_data'):
            # Store original mesh state
            obj = context.edit_object
            bm = bmesh.from_edit_mesh(obj.data)
            self.original_mesh_data = bm.copy()

            # bm.copy() doesn't preserve bevel_weight and crease, store them separately
            self.original_edge_bevel = {}
            self.original_edge_crease = {}
            self.original_vert_bevel = {}
            self.original_vert_crease = {}

            # Check if bevel weight is stored in layers (Blender 5.0+)
            edge_bevel_layer = bm.edges.layers.float.get('bevel_weight_edge')
            vert_bevel_layer = bm.verts.layers.float.get('bevel_weight_vert')

            for e in bm.edges:
                # Try layer first, then attribute
                if edge_bevel_layer:
                    self.original_edge_bevel[e.index] = e[edge_bevel_layer]
                elif hasattr(e, 'bevel_weight'):
                    self.original_edge_bevel[e.index] = e.bevel_weight

                if hasattr(e, 'crease'):
                    self.original_edge_crease[e.index] = e.crease

            for v in bm.verts:
                if vert_bevel_layer:
                    self.original_vert_bevel[v.index] = v[vert_bevel_layer]
                elif hasattr(v, 'bevel_weight'):
                    self.original_vert_bevel[v.index] = v.bevel_weight

                if hasattr(v, 'crease'):
                    self.original_vert_crease[v.index] = v.crease

        # Restore original, then apply preview cut
        self.restore_original_mesh(context)
        
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        
        plane_co = (self.start_pos + self.end_pos) / 2
        plane_no = self.get_plane_normal(context)
        
        mx_inv = obj.matrix_world.inverted()
        plane_co_local = mx_inv @ plane_co
        plane_no_local = mx_inv.to_3x3() @ plane_no
        
        # Get geometry
        if self.use_selection:
            geom = [v for v in bm.verts if v.select and not v.hide] + \
                   [e for e in bm.edges if e.select and not e.hide] + \
                   [f for f in bm.faces if f.select and not f.hide]
        else:
            geom = [v for v in bm.verts if not v.hide] + \
                   [e for e in bm.edges if not e.hide] + \
                   [f for f in bm.faces if not f.hide]
        
        if geom:
            # Ensure UV layer access
            uv_layer = bm.loops.layers.uv.active
            
            # In selection mode, store which faces were originally selected
            if self.use_selection:
                original_selected_faces = {f for f in bm.faces if f.select and not f.hide}
            
            bmesh.ops.bisect_plane(
                bm, geom=geom,
                plane_co=plane_co_local,
                plane_no=plane_no_local,
                clear_inner=False,
                clear_outer=False
            )
            
            # In selection mode, select all faces that are part of originally selected geometry
            # This includes new faces created by the cut
            if self.use_selection:
                # Build a set of all vertices that belong to originally selected faces
                selected_verts = set()
                for f in original_selected_faces:
                    if f.is_valid:  # Face may have been modified by bisect
                        for v in f.verts:
                            selected_verts.add(v)
                
                # Now select all faces that have vertices from the original selection
                for f in bm.faces:
                    if not f.hide:
                        # Select face if any of its vertices were in the original selection
                        if any(v in selected_verts for v in f.verts):
                            f.select = True
                        else:
                            f.select = False
                
                # Update vertex and edge selection to match face selection
                for v in bm.verts:
                    v.select = any(f.select for f in v.link_faces if not f.hide)
                for e in bm.edges:
                    e.select = any(f.select for f in e.link_faces if not f.hide)
            
            bmesh.update_edit_mesh(obj.data)

    def restore_original_mesh(self, context):
        """Restore mesh to original state before preview"""
        if not hasattr(self, 'original_mesh_data'):
            return

        # Verify we still have a valid edit object
        if not context.edit_object or context.mode != 'EDIT_MESH':
            return

        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)

        # Detect crease API
        use_direct_crease = hasattr(bmesh.types.BMEdge, 'crease')
        use_direct_vert_crease = hasattr(bmesh.types.BMVert, 'crease')

        # Store layer names before clearing
        uv_layer_names = [layer.name for layer in bm.loops.layers.uv]

        # Clear current bmesh
        bm.clear()

        # Recreate UV layers
        for uv_name in uv_layer_names:
            if uv_name not in bm.loops.layers.uv:
                bm.loops.layers.uv.new(uv_name)

        # Recreate deform layer
        deform_layer = None
        orig_deform = self.original_mesh_data.verts.layers.deform.active
        if orig_deform:
            if not bm.verts.layers.deform.active:
                deform_layer = bm.verts.layers.deform.new()
            else:
                deform_layer = bm.verts.layers.deform.active

        # Recreate crease layers for older versions
        crease_layer = None
        vert_crease_layer = None

        if not use_direct_crease:
            orig_crease = self.original_mesh_data.edges.layers.float.get("crease_edge")
            if orig_crease:
                crease_layer = bm.edges.layers.float.new("crease_edge")
        if not use_direct_vert_crease:
            orig_vert_crease = self.original_mesh_data.verts.layers.float.get("crease_vert")
            if orig_vert_crease:
                vert_crease_layer = bm.verts.layers.float.new("crease_vert")

        # Get/create bevel weight layers for restoration
        edge_bevel_layer = bm.edges.layers.float.get('bevel_weight_edge')
        if not edge_bevel_layer:
            edge_bevel_layer = bm.edges.layers.float.new('bevel_weight_edge')

        vert_bevel_layer = bm.verts.layers.float.get('bevel_weight_vert')
        if not vert_bevel_layer:
            vert_bevel_layer = bm.verts.layers.float.new('bevel_weight_vert')

        # Copy vertices with all attributes
        for v in self.original_mesh_data.verts:
            new_v = bm.verts.new(v.co)
            new_v.select = v.select
            new_v.hide = v.hide  # Preserve hidden state

            # Copy vertex crease from stored dict (bm.copy() doesn't preserve it)
            if hasattr(self, 'original_vert_crease') and v.index in self.original_vert_crease:
                new_v.crease = self.original_vert_crease[v.index]
            elif use_direct_vert_crease:
                new_v.crease = v.crease
            elif vert_crease_layer:
                orig_vert_crease = self.original_mesh_data.verts.layers.float.get("crease_vert")
                if orig_vert_crease:
                    new_v[vert_crease_layer] = v[orig_vert_crease]

            # Copy vertex bevel weight from stored dict (bm.copy() doesn't preserve it)
            if hasattr(self, 'original_vert_bevel') and v.index in self.original_vert_bevel:
                if vert_bevel_layer:
                    new_v[vert_bevel_layer] = self.original_vert_bevel[v.index]
                elif hasattr(new_v, 'bevel_weight'):
                    new_v.bevel_weight = self.original_vert_bevel[v.index]

            # Copy vertex weights
            if deform_layer and orig_deform:
                try:
                    # Check if vertex has deform weights
                    if len(v[orig_deform]) > 0:
                        for group_idx, weight in v[orig_deform].items():
                            new_v[deform_layer][group_idx] = weight
                except (KeyError, TypeError):
                    pass

        bm.verts.ensure_lookup_table()
        bm.verts.index_update()

        # Copy edges with all attributes
        for e in self.original_mesh_data.edges:
            new_e = bm.edges.new([bm.verts[v.index] for v in e.verts])
            new_e.select = e.select
            new_e.smooth = e.smooth
            new_e.hide = e.hide  # Preserve hidden state

            # Copy edge crease from stored dict (bm.copy() doesn't preserve it)
            if hasattr(self, 'original_edge_crease') and e.index in self.original_edge_crease:
                new_e.crease = self.original_edge_crease[e.index]
            elif use_direct_crease:
                new_e.crease = e.crease
            elif crease_layer:
                orig_crease = self.original_mesh_data.edges.layers.float.get("crease_edge")
                if orig_crease:
                    new_e[crease_layer] = e[orig_crease]

            # Copy edge bevel weight from stored dict (bm.copy() doesn't preserve it)
            if hasattr(self, 'original_edge_bevel') and e.index in self.original_edge_bevel:
                if edge_bevel_layer:
                    new_e[edge_bevel_layer] = self.original_edge_bevel[e.index]
                elif hasattr(new_e, 'bevel_weight'):
                    new_e.bevel_weight = self.original_edge_bevel[e.index]

        bm.edges.ensure_lookup_table()

        # Copy faces with UV data and smoothing
        orig_uv_layers = self.original_mesh_data.loops.layers.uv
        new_uv_layers = bm.loops.layers.uv

        for f in self.original_mesh_data.faces:
            try:
                new_face = bm.faces.new([bm.verts[v.index] for v in f.verts])
                new_face.select = f.select
                new_face.smooth = f.smooth
                new_face.material_index = f.material_index
                new_face.hide = f.hide  # Preserve hidden state

                # Copy UV coordinates for each layer
                for orig_uv_layer in orig_uv_layers:
                    if orig_uv_layer.name in [layer.name for layer in new_uv_layers]:
                        new_uv_layer = new_uv_layers[orig_uv_layer.name]
                        for i, loop in enumerate(f.loops):
                            new_face.loops[i][new_uv_layer].uv = loop[orig_uv_layer].uv
            except:
                pass

        bmesh.update_edit_mesh(obj.data)
    
    def execute_slice(self, context):
        t_start = time.perf_counter()
        
        # Safety check
        if not context.edit_object or context.mode != 'EDIT_MESH':
            self.report({'WARNING'}, "Must be in Edit Mode with valid object")
            return

        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)

        # Detect Blender version's crease/bevel API early (before any bmesh operations)
        use_direct_crease = hasattr(bmesh.types.BMEdge, 'crease')
        use_direct_vert_crease = hasattr(bmesh.types.BMVert, 'crease')
        use_direct_bevel = hasattr(bmesh.types.BMEdge, 'bevel_weight')
        use_direct_vert_bevel = hasattr(bmesh.types.BMVert, 'bevel_weight')

        # Ensure UV layer access
        uv_layer = bm.loops.layers.uv.active

        # Get deform layer for vertex weights
        deform_layer = bm.verts.layers.deform.active

        # Setup crease layers for older Blender versions
        crease_layer = None
        vert_crease_layer = None

        if not use_direct_crease:
            crease_layer = bm.edges.layers.float.get("crease_edge")
            if crease_layer is None:
                crease_layer = bm.edges.layers.float.new("crease_edge")
        if not use_direct_vert_crease:
            vert_crease_layer = bm.verts.layers.float.get("crease_vert")
            if vert_crease_layer is None:
                vert_crease_layer = bm.verts.layers.float.new("crease_vert")

        t_setup = time.perf_counter()
        print(f"DEBUG SLICE: Setup took {(t_setup-t_start)*1000:.1f}ms")

        plane_co = (self.start_pos + self.end_pos) / 2
        plane_no = self.get_plane_normal(context)

        mx_inv = obj.matrix_world.inverted()
        plane_co_local = mx_inv @ plane_co
        plane_no_local = mx_inv.to_3x3() @ plane_no

        # Get geometry to cut - always exclude hidden geometry
        if self.use_selection:
            geom = [v for v in bm.verts if v.select and not v.hide] + \
                   [e for e in bm.edges if e.select and not e.hide] + \
                   [f for f in bm.faces if f.select and not f.hide]
        else:
            geom = [v for v in bm.verts if not v.hide] + \
                   [e for e in bm.edges if not e.hide] + \
                   [f for f in bm.faces if not f.hide]

        if not geom:
            self.report({'WARNING'}, "No geometry to cut")
            return

        t_geom = time.perf_counter()
        print(f"DEBUG SLICE: Collect geom took {(t_geom-t_setup)*1000:.1f}ms ({len(geom)} elements)")

        # Store original attributes BEFORE bisect
        # Use the BMesh element object itself as the key (not index) because indices change after bisect
        orig_vert_data = {}
        orig_edge_data = {}

        for v in bm.verts:
            vdata = {
                'crease': v.crease if use_direct_vert_crease else (v[vert_crease_layer] if vert_crease_layer else 0)
            }
            if use_direct_vert_bevel:
                vdata['bevel'] = v.bevel_weight
            if deform_layer:
                try:
                    if len(v[deform_layer]) > 0:
                        vdata['weights'] = dict(v[deform_layer].items())
                except (KeyError, TypeError):
                    pass
            orig_vert_data[v] = vdata

        for e in bm.edges:
            edata = {
                'crease': e.crease if use_direct_crease else (e[crease_layer] if crease_layer else 0),
                'smooth': e.smooth
            }
            if use_direct_bevel:
                edata['bevel'] = e.bevel_weight
            orig_edge_data[e] = edata

        # Store original face data (smooth, material) for proper attribute inheritance
        orig_face_data = {}
        for f in bm.faces:
            orig_face_data[f] = {
                'smooth': f.smooth,
                'material_index': f.material_index
            }

        t_store = time.perf_counter()
        print(f"DEBUG SLICE: Store attrs took {(t_store-t_geom)*1000:.1f}ms")

        result = bmesh.ops.bisect_plane(
            bm, geom=geom,
            plane_co=plane_co_local,
            plane_no=plane_no_local,
            clear_inner=False,
            clear_outer=False
        )

        # Get new geometry created by bisect
        new_geom = result.get('geom_cut', []) + result.get('geom', [])
        new_geom_set = set(new_geom)  # Convert to set for O(1) lookups

        t_bisect = time.perf_counter()
        print(f"DEBUG SLICE: Bisect took {(t_bisect-t_store)*1000:.1f}ms ({len(new_geom)} new elements)")

        # Pre-categorize new geometry for faster access
        new_verts_set = set(elem for elem in new_geom if isinstance(elem, bmesh.types.BMVert))
        new_edges_set = set(elem for elem in new_geom if isinstance(elem, bmesh.types.BMEdge))
        new_faces = [elem for elem in new_geom if isinstance(elem, bmesh.types.BMFace)]

        # Collect adjacent faces to new geometry for attribute interpolation
        adjacent_faces = set()
        for v in new_verts_set:
            for face in v.link_faces:
                if face not in new_geom_set:
                    adjacent_faces.add(face)

        # Pre-compute smooth/material from adjacent faces (only once)
        # If no adjacent faces (e.g., slicing entire mesh), use the original face data
        if adjacent_faces:
            smooth_count = sum(1 for f in adjacent_faces if f.smooth)
            default_smooth = smooth_count > len(adjacent_faces) / 2
            default_material = next(iter(adjacent_faces)).material_index
        elif orig_face_data:
            # No adjacent faces - use statistics from original faces to preserve overall smoothness
            smooth_count = sum(1 for fdata in orig_face_data.values() if fdata['smooth'])
            default_smooth = smooth_count > len(orig_face_data) / 2
            # Get most common material index
            default_material = max(set(fdata['material_index'] for fdata in orig_face_data.values()),
                                   key=lambda m: sum(1 for fdata in orig_face_data.values() if fdata['material_index'] == m))
        else:
            default_smooth = False
            default_material = 0

        # Interpolate attributes for NEW vertices
        for elem in new_verts_set:
            if not elem.is_valid:
                continue

            # Find linked verts that are NOT new (for interpolation)
            linked_verts = []
            for edge in elem.link_edges:
                if edge.is_valid:
                    other_vert = edge.other_vert(elem)
                    if other_vert.is_valid and other_vert not in new_verts_set:
                        linked_verts.append(other_vert)

            if linked_verts and deform_layer:
                try:
                    weight_groups = {}
                    for vert in linked_verts:
                        if vert.is_valid:
                            try:
                                deform_data = vert[deform_layer]
                                if len(deform_data) > 0:
                                    for group_idx, weight in deform_data.items():
                                        if group_idx not in weight_groups:
                                            weight_groups[group_idx] = []
                                        weight_groups[group_idx].append(weight)
                            except (KeyError, TypeError):
                                pass

                    # Apply averaged weights
                    for group_idx, weights in weight_groups.items():
                        elem[deform_layer][group_idx] = sum(weights) / len(weights)
                except (ReferenceError, RuntimeError):
                    pass

        # Interpolate attributes for NEW edges
        # New cut edges should generally be smooth (not sharp) to blend with surrounding geometry
        for elem in new_edges_set:
            if not elem.is_valid:
                continue

            # Find linked edges that are NOT new and check their smooth status
            # Use original edge data when available for accurate smooth inheritance
            smooth_neighbors = 0
            total_neighbors = 0
            for vert in elem.verts:
                for edge in vert.link_edges:
                    if edge != elem and edge.is_valid:
                        # Check original data first, then current state
                        if edge in orig_edge_data:
                            total_neighbors += 1
                            if orig_edge_data[edge]['smooth']:
                                smooth_neighbors += 1
                        elif edge not in new_edges_set:
                            total_neighbors += 1
                            if edge.smooth:
                                smooth_neighbors += 1
            
            # Inherit smooth if majority of neighbors are smooth
            # Default to smooth=True for cut edges to avoid unwanted sharp edges
            if total_neighbors > 0:
                elem.smooth = smooth_neighbors >= total_neighbors / 2
            else:
                # No neighbors found - default to smooth (not sharp)
                elem.smooth = True

        # Interpolate attributes for NEW faces (smooth, material, UVs)
        for elem in new_faces:
            if not elem.is_valid:
                continue
            
            # Find adjacent original face to inherit material from
            # This ensures new faces get the material from the face they were split from
            inherited_material = None
            inherited_smooth = None
            for v in elem.verts:
                for linked_face in v.link_faces:
                    if linked_face != elem and linked_face in orig_face_data:
                        inherited_material = orig_face_data[linked_face]['material_index']
                        inherited_smooth = orig_face_data[linked_face]['smooth']
                        break
                if inherited_material is not None:
                    break
            
            # Apply inherited or default smooth/material
            elem.smooth = inherited_smooth if inherited_smooth is not None else default_smooth
            elem.material_index = inherited_material if inherited_material is not None else default_material

            # Interpolate UVs for new faces
            if uv_layer:
                for loop in elem.loops:
                    if loop.vert in new_verts_set:
                        # Find adjacent faces with UV data and interpolate
                        linked_faces = [f for f in loop.vert.link_faces if f != elem and f not in new_geom_set]
                        if linked_faces:
                            ref_face = linked_faces[0]
                            for ref_loop in ref_face.loops:
                                if ref_loop.vert == loop.vert:
                                    loop[uv_layer].uv = ref_loop[uv_layer].uv
                                    break

        # Restore original attributes for existing geometry
        # (Elements that existed before bisect and weren't newly created)
        # Note: bisect_plane can split edges, creating new edges that aren't in new_geom_set
        # but also aren't in orig_edge_data. These split edges should inherit smooth from neighbors.

        t_interp = time.perf_counter()
        print(f"DEBUG SLICE: Interpolate attrs took {(t_interp-t_bisect)*1000:.1f}ms")

        for v in bm.verts:
            if v not in new_geom_set and v in orig_vert_data:
                vdata = orig_vert_data[v]
                if use_direct_vert_crease:
                    v.crease = vdata['crease']
                elif vert_crease_layer:
                    v[vert_crease_layer] = vdata['crease']
                if 'bevel' in vdata and use_direct_vert_bevel:
                    v.bevel_weight = vdata['bevel']
                if 'weights' in vdata and deform_layer:
                    for group_idx, weight in vdata['weights'].items():
                        v[deform_layer][group_idx] = weight

        # First pass: restore smooth for edges we have data for
        for e in bm.edges:
            if e in orig_edge_data:
                edata = orig_edge_data[e]
                if use_direct_crease:
                    e.crease = edata['crease']
                elif crease_layer:
                    e[crease_layer] = edata['crease']
                if 'bevel' in edata and use_direct_bevel:
                    e.bevel_weight = edata['bevel']
                e.smooth = edata['smooth']
        
        # Second pass: for edges NOT in orig_edge_data (split edges or new edges),
        # inherit smooth from connected edges that have original data
        for e in bm.edges:
            if e not in orig_edge_data:
                # This is either a new edge from the cut, or a split edge
                # Check neighboring edges to inherit smooth attribute (majority voting)
                smooth_neighbors = 0
                total_neighbors = 0
                for vert in e.verts:
                    for linked_edge in vert.link_edges:
                        if linked_edge != e and linked_edge in orig_edge_data:
                            total_neighbors += 1
                            if orig_edge_data[linked_edge]['smooth']:
                                smooth_neighbors += 1
                
                # If we found neighbor data, use majority vote (>= to favor smooth)
                # Otherwise default to smooth=True to avoid unwanted sharp edges
                if total_neighbors > 0:
                    e.smooth = smooth_neighbors >= total_neighbors / 2
                else:
                    e.smooth = True

        # Restore original face attributes (smooth, material_index)
        # This ensures original faces keep their materials after bisect operations
        for f in bm.faces:
            if f.is_valid and f in orig_face_data:
                fdata = orig_face_data[f]
                f.smooth = fdata['smooth']
                f.material_index = fdata['material_index']

        t_restore = time.perf_counter()
        print(f"DEBUG SLICE: Restore attrs took {(t_restore-t_interp)*1000:.1f}ms")

        # Weld vertices that are very close to each other (remove doubles)
        # Only weld within connected geometry islands and exclude hidden geometry
        # This prevents separate objects from being welded together
        
        # Get vertices from the geometry that was operated on (respects selection mode)
        # Include all vertices connected to the cut geometry (not just the new ones)
        if self.use_selection:
            # In selection mode, weld all visible verts in selected faces
            affected_verts = set()
            for f in bm.faces:
                if f.select and not f.hide:
                    for v in f.verts:
                        if not v.hide:
                            affected_verts.add(v)
            weld_verts = list(affected_verts)
        else:
            # In non-selection mode, weld all visible vertices
            weld_verts = [v for v in bm.verts if not v.hide]
        
        # Find connected components to prevent welding between separate islands
        visited = set()
        islands = []
        
        for start_vert in weld_verts:
            if start_vert in visited:
                continue
                
            # BFS to find all connected vertices in this island
            island = set()
            queue = [start_vert]
            island.add(start_vert)
            visited.add(start_vert)
            
            while queue:
                v = queue.pop(0)
                for edge in v.link_edges:
                    if edge.hide:
                        continue
                    other = edge.other_vert(v)
                    if other in weld_verts and other not in visited:
                        visited.add(other)
                        island.add(other)
                        queue.append(other)
            
            if island:
                islands.append(list(island))
        
        # Weld within each island separately
        for island_verts in islands:
            if len(island_verts) > 1:
                bmesh.ops.remove_doubles(bm, verts=island_verts, dist=self.weld_threshold)

        t_weld = time.perf_counter()
        print(f"DEBUG SLICE: Weld (islands) took {(t_weld-t_restore)*1000:.1f}ms ({len(islands)} islands, {len(weld_verts)} verts)")

        if not self.infinite:
            line_vec_local = mx_inv.to_3x3() @ (self.end_pos - self.start_pos)
            if line_vec_local.length > 0.001:
                line_dir = line_vec_local.normalized()
                start_local = mx_inv @ self.start_pos
                end_local = mx_inv @ self.end_pos
                
                # Get visible geometry for cutting
                visible_geom = [v for v in bm.verts if not v.hide] + \
                               [e for e in bm.edges if not e.hide] + \
                               [f for f in bm.faces if not f.hide]
                
                bmesh.ops.bisect_plane(
                    bm, geom=visible_geom,
                    plane_co=start_local,
                    plane_no=-line_dir,
                    clear_inner=False,
                    clear_outer=False
                )
                
                bmesh.ops.bisect_plane(
                    bm, geom=visible_geom,
                    plane_co=end_local,
                    plane_no=line_dir,
                    clear_inner=False,
                    clear_outer=False
                )
        
        if self.split:
            # Get visible geometry for splitting
            visible_geom = [v for v in bm.verts if not v.hide] + \
                           [e for e in bm.edges if not e.hide] + \
                           [f for f in bm.faces if not f.hide]
            
            if self.gap > 0:
                offset = plane_no_local * (self.gap / 2)
                bmesh.ops.bisect_plane(
                    bm, geom=visible_geom,
                    plane_co=plane_co_local + offset, 
                    plane_no=plane_no_local, 
                    clear_outer=True
                )
                bmesh.ops.bisect_plane(
                    bm, geom=visible_geom,
                    plane_co=plane_co_local - offset, 
                    plane_no=plane_no_local, 
                    clear_inner=True
                )
            
            bpy.ops.mesh.separate(type='LOOSE')
            
            if self.cap_sections:
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.edge_face_add()

        t_normals = time.perf_counter()

        # Deselect all geometry after operation
        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False

        bmesh.update_edit_mesh(obj.data)
        obj.data.update()

        t_end = time.perf_counter()
        print(f"DEBUG SLICE: Update mesh took {(t_end-t_normals)*1000:.1f}ms")
        print(f"DEBUG SLICE: TOTAL {(t_end-t_start)*1000:.1f}ms")

        inf_msg = "infinite" if self.infinite else "bounded"
        self.report({'INFO'}, f"Slice complete ({inf_msg}, axis: {self.axis})")
        
    def execute(self, context):
        if not hasattr(self, 'start_pos'):
            self.start_pos = Vector(self.start)
            self.end_pos = Vector(self.end)
        self.execute_slice(context)
        return {'FINISHED'}

def menu_func(self, context):
    self.layout.operator(MESH_OT_modo_polygon_slice.bl_idname)

def register():
    bpy.utils.register_class(MESH_OT_modo_polygon_slice)
    bpy.types.VIEW3D_MT_edit_mesh.append(menu_func)

def unregister():
    bpy.utils.unregister_class(MESH_OT_modo_polygon_slice)
    bpy.types.VIEW3D_MT_edit_mesh.remove(menu_func)

if __name__ == "__main__":
    register()