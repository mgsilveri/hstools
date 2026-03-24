import bpy
import bmesh
import mathutils
import mathutils.geometry
from mathutils import Vector, Matrix, Euler
from bpy.props import (
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    BoolProperty,
    IntProperty,
)
import math
import gpu
import bpy_extras
from gpu_extras.batch import batch_for_shader


class MESH_OT_interactive_mirror(bpy.types.Operator):
    """Interactive Mirror Tool - Mirror geometry with visual manipulation"""
    bl_idname = "mesh.interactive_mirror"
    bl_label = "Interactive Mirror"
    bl_options = {'REGISTER', 'UNDO'}
    
    # Mirror axis
    axis: EnumProperty(
        name="Axis",
        description="Mirror axis direction",
        items=[
            ('X', "X", "Mirror across X axis"),
            ('Y', "Y", "Mirror across Y axis"),
            ('Z', "Z", "Mirror across Z axis"),
        ],
        default='X',
    )
    
    # Center position
    center: FloatVectorProperty(
        name="Center",
        description="Mirror plane center position",
        default=(0.0, 0.0, 0.0),
        subtype='TRANSLATION',
    )
    
    # Angle
    angle: FloatProperty(
        name="Angle",
        description="Rotation angle of the mirror plane",
        default=0.0,
        min=0.0,
        max=360.0,
        subtype='ANGLE',
    )
    
    # Options
    replace_source: BoolProperty(
        name="Replace Source",
        description="Remove the original geometry and keep only the mirrored result",
        default=False,
    )
    
    slice_along_mirror: BoolProperty(
        name="Slice Along Mirror",
        description="Cut the mesh along the mirror plane when inside mesh boundaries. When outside, reflection is treated as part of the originating mesh",
        default=True,
    )
    
    weld_seam: BoolProperty(
        name="Weld Seam",
        description="Automatically weld vertices along the mirror seam",
        default=True,
    )
    
    flip_side: BoolProperty(
        name="Flip",
        description="Flip the symmetry to the opposite side of the mirror plane",
        default=False,
    )
    
    # Internal state for modal interaction
    _handle = None
    _is_dragging = False
    _is_rotating = False
    _mouse_start = None
    _center_start = None
    _angle_start = None
    _plane_normal = None
    _preview_obj = None
    _original_mode = None
    _original_verts = []  # Store original vertex positions
    _hovered_handle = None  # Track which handle is hovered ('DRAG', 'ROTATE', or None)
    _handle_distance = 3.0  # Distance from drag handle to rotate handle (controls plane width)
    _plane_height = 3.0  # Fixed height of the plane (v direction)
    _drag_handle_pos_3d = None  # Actual 3D position of drag handle
    _edge_cache = None  # Cached edges for intersection snapping (built once per drag)
    _rotate_handle_pos_3d = None  # Actual 3D position of rotate handle
    _snapped_to_intersection = False  # True when drag handle is snapped to an edge-polygon intersection
    
    @classmethod
    def poll(cls, context):
        return (context.object is not None and 
                context.object.type == 'MESH')
    
    def invoke(self, context, event):
        """Start modal interaction with visual gizmo"""
        obj = context.object
        
        # Reset properties to defaults
        self.replace_source = False
        self.slice_along_mirror = True
        self.weld_seam = True
        self.flip_side = False
        self.angle = 0.0
        self._handle_distance = 3.0  # Initialize handle distance
        
        # Auto-detect best mirror axis based on viewport orientation
        rv3d = context.region_data
        if rv3d:
            # Get view direction (the direction camera is looking)
            view_direction = rv3d.view_rotation @ Vector((0, 0, -1))
            
            # Find which world axis the view direction is most aligned with
            abs_x = abs(view_direction.dot(Vector((1, 0, 0))))
            abs_y = abs(view_direction.dot(Vector((0, 1, 0))))
            abs_z = abs(view_direction.dot(Vector((0, 0, 1))))
            
            # If looking along X, use Y mirror; if looking along Y, use X mirror; if along Z, use Z
            if abs_x > abs_y and abs_x > abs_z:
                self.axis = 'Y'  # Looking along X → mirror across Y
            elif abs_y > abs_z:
                self.axis = 'X'  # Looking along Y → mirror across X
            else:
                self.axis = 'Z'  # Looking along Z → mirror across Z
        
        # Initialize handle positions (will be set properly after center is calculated)
        self._drag_handle_pos_3d = None
        self._rotate_handle_pos_3d = None
        
        # Get pivot point based on Blender's transform pivot setting
        pivot_point = context.scene.tool_settings.transform_pivot_point
        
        if obj.mode == 'EDIT':
            mesh = obj.data
            bm = bmesh.from_edit_mesh(mesh)
            selected_verts = [v for v in bm.verts if v.select]
            
            if not selected_verts:
                self.report({'WARNING'}, "No vertices selected")
                return {'CANCELLED'}
            
            # Determine center based on pivot point setting
            if pivot_point == 'ACTIVE_ELEMENT':
                # Use active element (vertex, edge, or face)
                active_elem = bm.select_history.active
                if active_elem:
                    if isinstance(active_elem, bmesh.types.BMVert):
                        center = active_elem.co.copy()
                    elif isinstance(active_elem, bmesh.types.BMEdge):
                        center = (active_elem.verts[0].co + active_elem.verts[1].co) / 2
                    elif isinstance(active_elem, bmesh.types.BMFace):
                        center = active_elem.calc_center_median()
                    else:
                        center = sum((v.co for v in selected_verts), Vector()) / len(selected_verts)
                else:
                    # No active element, fall back to median
                    center = sum((v.co for v in selected_verts), Vector()) / len(selected_verts)
                self.center = tuple(obj.matrix_world @ center)
                
            elif pivot_point == 'CURSOR':
                # Use 3D cursor
                self.center = tuple(context.scene.cursor.location)
                
            elif pivot_point == 'BOUNDING_BOX_CENTER':
                # Use bounding box center of selection
                min_co = Vector(selected_verts[0].co)
                max_co = Vector(selected_verts[0].co)
                for v in selected_verts:
                    for i in range(3):
                        min_co[i] = min(min_co[i], v.co[i])
                        max_co[i] = max(max_co[i], v.co[i])
                center = (min_co + max_co) / 2
                self.center = tuple(obj.matrix_world @ center)
                
            else:  # MEDIAN_POINT or INDIVIDUAL_ORIGINS
                # Use median point of selection
                center = sum((v.co for v in selected_verts), Vector()) / len(selected_verts)
                self.center = tuple(obj.matrix_world @ center)
        else:
            # Object mode - use object-based pivot
            if pivot_point == 'CURSOR':
                self.center = tuple(context.scene.cursor.location)
            elif pivot_point == 'BOUNDING_BOX_CENTER':
                bbox_center = sum((Vector(b) for b in obj.bound_box), Vector()) / 8
                self.center = tuple(obj.matrix_world @ bbox_center)
            else:  # Use object origin
                self.center = tuple(obj.location)
        
        # Calculate initial plane normal
        self._update_plane_normal()
        
        # Calculate initial handle positions
        self._update_handle_positions()
        
        # Store original mode and hide state
        self._original_mode = context.object.mode
        self._original_hide = context.object.hide_get()
        
        # Store and hide gizmo visibility
        self._original_show_gizmo = None
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        self._original_show_gizmo = space.show_gizmo
                        space.show_gizmo = False
                        break
                break
        
        # Create preview object
        self._create_preview(context)
        
        # Set header text with current status
        self._update_status_text(context)
        
        # Add draw handlers
        args = (self, context)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            draw_mirror_gizmo, args, 'WINDOW', 'POST_VIEW'
        )
        
        # Store rv3d reference for view-independent handle sizing
        self._rv3d = context.region_data
        
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        
        return {'RUNNING_MODAL'}
    
    def _update_plane_normal(self):
        """Update the plane normal based on current axis and angle"""
        if self.axis == 'X':
            self._plane_normal = Vector((1, 0, 0))
        elif self.axis == 'Y':
            self._plane_normal = Vector((0, 1, 0))
        else:
            self._plane_normal = Vector((0, 0, 1))
    
    def _update_handle_positions(self):
        """Update the 3D positions of handles based on center, angle, and distance"""
        center = Vector(self.center)
        
        # Get basis vectors
        if self.axis == 'X':
            u = Vector((0, 1, 0))
            v = Vector((0, 0, 1))
            rotation_axis = Vector((0, 0, 1))
        elif self.axis == 'Y':
            u = Vector((1, 0, 0))
            v = Vector((0, 0, 1))
            rotation_axis = Vector((0, 0, 1))
        else:  # Z
            u = Vector((1, 0, 0))
            v = Vector((0, 1, 0))
            rotation_axis = Vector((1, 0, 0))
        
        # Apply rotation
        angle_rad = math.radians(self.angle)
        if abs(angle_rad) > 0.001:
            rot_matrix = Matrix.Rotation(angle_rad, 3, rotation_axis)
            u = rot_matrix @ u
            v = rot_matrix @ v
        
        # Calculate handle positions
        # Drag handle is at the center of the plane
        self._drag_handle_pos_3d = center
        # Rotate handle is at the edge of the plane
        self._rotate_handle_pos_3d = center + u * self._handle_distance
        
        # Store the actual u and v vectors for drawing
        self._plane_u_vector = u
        self._plane_v_vector = v
    
    def modal(self, context, event):
        """Handle mouse events for gizmo interaction"""
        context.area.tag_redraw()
        
        # Block undo/redo while modal operator is running to prevent crashes
        if event.type == 'Z' and event.value == 'PRESS' and event.ctrl:
            # Block undo during modal operation
            return {'RUNNING_MODAL'}
        
        # Allow viewport navigation with middle mouse
        # Invalidate edge cache when view changes (backface culling depends on view direction)
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            self._edge_cache = None  # Rebuild cache after viewport changes
            return {'PASS_THROUGH'}
        
        # Handle modifier+LMB viewport navigation (Ctrl/Shift/Alt + LMB)
        # But NOT when already dragging (Ctrl is used for snap toggle during drag)
        if event.type == 'LEFTMOUSE' and (event.ctrl or event.shift or event.alt):
            if not self._is_dragging and not self._is_rotating:
                self._edge_cache = None  # Rebuild cache after viewport changes
                return {'PASS_THROUGH'}
        
        # Mouse movement - update dragging or rotating
        if event.type == 'MOUSEMOVE':
            if self._is_dragging:
                self._update_center_from_mouse(context, event)
                self._update_preview_transform(context)
                return {'RUNNING_MODAL'}
            elif self._is_rotating:
                self._update_rotation_from_mouse(context, event)
                self._update_preview_transform(context)
                return {'RUNNING_MODAL'}
            else:
                # Update hover state when not dragging/rotating
                self._hovered_handle = self._get_handle_at_mouse(context, event)
            return {'PASS_THROUGH'}
        
        # Left mouse - start/stop dragging or rotating
        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            # Pass through if modifiers are pressed (viewport navigation)
            if event.ctrl or event.shift or event.alt:
                return {'PASS_THROUGH'}
            
            handle_type = self._get_handle_at_mouse(context, event)
            if handle_type == 'DRAG':
                self._is_dragging = True
                self._mouse_start = Vector((event.mouse_region_x, event.mouse_region_y))
                self._center_start = Vector(self.center)
                # Store initial handle positions for offset calculation
                if hasattr(self, '_drag_handle_pos_3d') and hasattr(self, '_rotate_handle_pos_3d'):
                    self._drag_handle_start = self._drag_handle_pos_3d.copy()
                    self._rotate_handle_start = self._rotate_handle_pos_3d.copy()
                # Build edge cache for intersection snapping
                self._edge_cache = self._build_edge_cache(context)
                return {'RUNNING_MODAL'}
            elif handle_type == 'ROTATE':
                self._is_rotating = True
                self._mouse_start = Vector((event.mouse_region_x, event.mouse_region_y))
                self._angle_start = self.angle
                self._handle_distance_start = self._handle_distance
                # Store the drag handle position (rotation pivot) in 3D
                center = Vector(self.center)
                if self.axis == 'X':
                    u = Vector((0, 1, 0))
                    v = Vector((0, 0, 1))
                    rotation_axis = Vector((0, 0, 1))
                elif self.axis == 'Y':
                    u = Vector((1, 0, 0))
                    v = Vector((0, 0, 1))
                    rotation_axis = Vector((0, 0, 1))
                else:  # Z
                    u = Vector((1, 0, 0))
                    v = Vector((0, 1, 0))
                    rotation_axis = Vector((1, 0, 0))
                angle_rad = math.radians(self.angle)
                if abs(angle_rad) > 0.001:
                    rot_matrix = Matrix.Rotation(angle_rad, 3, rotation_axis)
                    u = rot_matrix @ u
                    v = rot_matrix @ v
                self._drag_handle_pivot = center  # Drag handle is at center
                return {'RUNNING_MODAL'}
            # Consume the click to prevent selection changes (like Modo)
            return {'RUNNING_MODAL'}
        
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            # If we're dragging or rotating, end it (even with modifiers pressed)
            if self._is_dragging:
                self._is_dragging = False
                self._edge_cache = None  # Clear edge cache when drag ends
                self._snapped_to_intersection = False  # Reset to sphere when drag ends
                return {'RUNNING_MODAL'}
            elif self._is_rotating:
                self._is_rotating = False
                return {'RUNNING_MODAL'}
            
            # Pass through if modifiers are pressed and we're NOT in an operation (viewport navigation)
            if event.ctrl or event.shift or event.alt:
                return {'PASS_THROUGH'}
            
            # Consume the release to prevent disruptions
            return {'RUNNING_MODAL'}
        
        # Keyboard shortcuts for axis (without modifiers)
        elif event.type == 'Z' and event.value == 'PRESS' and not event.ctrl and not event.shift and not event.alt:
            # Store current handle positions and distance
            if hasattr(self, '_drag_handle_pos_3d') and hasattr(self, '_rotate_handle_pos_3d'):
                drag_handle_pos = self._drag_handle_pos_3d.copy()
                rotate_handle_pos = self._rotate_handle_pos_3d.copy()
                # Calculate actual distance (rotate handle is at plane edge)
                handle_vec = rotate_handle_pos - drag_handle_pos
                current_distance = handle_vec.length
            else:
                # Fallback to current distance
                current_distance = self._handle_distance
                
                center = Vector(self.center)
                old_axis = self.axis
                
                # Get old basis vectors
                if old_axis == 'X':
                    u_old = Vector((0, 1, 0))
                    v_old = Vector((0, 0, 1))
                elif old_axis == 'Y':
                    u_old = Vector((1, 0, 0))
                    v_old = Vector((0, 0, 1))
                else:  # Z
                    u_old = Vector((1, 0, 0))
                    v_old = Vector((0, 1, 0))
                
                # Apply current rotation
                angle_rad_old = math.radians(self.angle)
                if abs(angle_rad_old) > 0.001:
                    if old_axis == 'X' or old_axis == 'Y':
                        rotation_axis_old = Vector((0, 0, 1))
                    else:
                        rotation_axis_old = Vector((1, 0, 0))
                    rot_matrix_old = Matrix.Rotation(angle_rad_old, 3, rotation_axis_old)
                    u_old = rot_matrix_old @ u_old
                    v_old = rot_matrix_old @ v_old
                
                drag_handle_pos = center - u_old * self._handle_distance - v_old * self._plane_height
            
            # Toggle through axes: X -> Y -> Z -> X
            if self.axis == 'X':
                self.axis = 'Y'
            elif self.axis == 'Y':
                self.axis = 'Z'
            else:
                self.axis = 'X'
            
            # Keep the handle distance the same
            self._handle_distance = current_distance
            
            # Keep the current angle (don't reset)
            # The angle will be applied to the new axis
            
            # Get new basis vectors for the new axis
            if self.axis == 'X':
                u_new = Vector((0, 1, 0))
                v_new = Vector((0, 0, 1))
                rotation_axis_new = Vector((0, 0, 1))
            elif self.axis == 'Y':
                u_new = Vector((1, 0, 0))
                v_new = Vector((0, 0, 1))
                rotation_axis_new = Vector((0, 0, 1))
            else:  # Z
                u_new = Vector((1, 0, 0))
                v_new = Vector((0, 1, 0))
                rotation_axis_new = Vector((1, 0, 0))
            
            # Apply the current angle to the new axis
            angle_rad = math.radians(self.angle)
            if abs(angle_rad) > 0.001:
                rot_matrix = Matrix.Rotation(angle_rad, 3, rotation_axis_new)
                u_new = rot_matrix @ u_new
                v_new = rot_matrix @ v_new
            
            # Calculate new center to keep drag handle at same position
            # Drag handle is now at center, so just use its position
            new_center = drag_handle_pos
            self.center = tuple(new_center)
            
            self._update_plane_normal()
            self._update_handle_positions()
            self._update_status_text(context)
            self._update_preview_transform(context)
            return {'RUNNING_MODAL'}
        
        # X key - rotate plane by 45 degrees
        elif event.type == 'X' and event.value == 'PRESS' and not event.ctrl and not event.shift and not event.alt:
            # Get current drag handle position (this is our pivot point that should stay fixed)
            # Also preserve the current handle distance from actual handle positions
            if self._drag_handle_pos_3d and self._rotate_handle_pos_3d:
                drag_handle_pos = self._drag_handle_pos_3d.copy()
                rotate_handle_pos = self._rotate_handle_pos_3d.copy()
                # Calculate actual distance (rotate handle is at plane edge)
                handle_vec = rotate_handle_pos - drag_handle_pos
                current_distance = handle_vec.length
                self._handle_distance = current_distance
            else:
                # Fallback: calculate it
                center = Vector(self.center)
                if self.axis == 'X':
                    u = Vector((0, 1, 0))
                    v = Vector((0, 0, 1))
                    rotation_axis = Vector((0, 0, 1))
                elif self.axis == 'Y':
                    u = Vector((1, 0, 0))
                    v = Vector((0, 0, 1))
                    rotation_axis = Vector((0, 0, 1))
                else:  # Z
                    u = Vector((1, 0, 0))
                    v = Vector((0, 1, 0))
                    rotation_axis = Vector((1, 0, 0))
                
                angle_rad_old = math.radians(self.angle)
                if abs(angle_rad_old) > 0.001:
                    rot_matrix_old = Matrix.Rotation(angle_rad_old, 3, rotation_axis)
                    u = rot_matrix_old @ u
                    v = rot_matrix_old @ v
                
                drag_handle_pos = center - u * self._handle_distance - v * self._plane_height
            
            # Update angle - snap to next 45 degree increment
            import math as m
            new_angle = (m.floor(self.angle / 45.0) + 1) * 45.0
            new_angle = new_angle % 360.0
            
            self.angle = new_angle
            
            # Recalculate handle positions - this will update center to keep drag handle fixed
            # by using the stored drag_handle_pos
            center = Vector(self.center)
            if self.axis == 'X':
                u = Vector((0, 1, 0))
                v = Vector((0, 0, 1))
                rotation_axis = Vector((0, 0, 1))
            elif self.axis == 'Y':
                u = Vector((1, 0, 0))
                v = Vector((0, 0, 1))
                rotation_axis = Vector((0, 0, 1))
            else:  # Z
                u = Vector((1, 0, 0))
                v = Vector((0, 1, 0))
                rotation_axis = Vector((1, 0, 0))
            
            angle_rad = math.radians(new_angle)
            if abs(angle_rad) > 0.001:
                rot_matrix = Matrix.Rotation(angle_rad, 3, rotation_axis)
                u = rot_matrix @ u
                v = rot_matrix @ v
            
            # Calculate new center to keep drag handle at same position
            # Drag handle is now at center, so just use its position
            new_center = drag_handle_pos
            self.center = tuple(new_center)
            
            self._update_handle_positions()
            self._update_status_text(context)
            self._update_preview_transform(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        
        # R key - toggle Replace Source
        elif event.type == 'R' and event.value == 'PRESS' and not event.ctrl and not event.shift and not event.alt:
            self.replace_source = not self.replace_source
            self._update_status_text(context)
            # Update preview appearance
            self._update_preview_appearance()
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        
        # S key - toggle Slice Along Mirror
        elif event.type == 'S' and event.value == 'PRESS' and not event.ctrl and not event.shift and not event.alt:
            self.slice_along_mirror = not self.slice_along_mirror
            self._update_status_text(context)
            # Rebuild preview with/without slice
            self._rebuild_preview(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        
        # W key - toggle Weld Seam
        elif event.type == 'W' and event.value == 'PRESS' and not event.ctrl and not event.shift and not event.alt:
            self.weld_seam = not self.weld_seam
            self._update_status_text(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        
        # F key - toggle Flip
        elif event.type == 'F' and event.value == 'PRESS' and not event.ctrl and not event.shift and not event.alt:
            self.flip_side = not self.flip_side
            self._update_status_text(context)
            # Rebuild preview with flipped side
            self._rebuild_preview(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        
        # Mouse wheel - allow zoom (no rotation control)
        elif event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}
        
        # Cancel
        elif event.type == 'ESC' and event.value == 'PRESS':
            self.cleanup(context)
            return {'CANCELLED'}
        
        # Right mouse - allow viewport navigation unless used for cancel
        elif event.type == 'RIGHTMOUSE':
            return {'PASS_THROUGH'}
        
        # Confirm and execute
        elif event.type == 'SPACE' and event.value == 'PRESS':
            # In Edit Mode, preview geometry is part of the mesh - don't remove it
            # In Object Mode, the separate preview object must be removed
            is_edit_mode = context.object and context.object.mode == 'EDIT'
            result = self.execute(context)
            self.cleanup(context, skip_preview_removal=is_edit_mode)
            return result
        
        # Pass through other events for viewport navigation
        return {'PASS_THROUGH'}
    
    def _get_handle_at_mouse(self, context, event):
        """Check which handle (if any) is under the mouse. Returns 'DRAG', 'ROTATE', or None"""
        region = context.region
        rv3d = context.region_data
        
        # Calculate view-independent handle size
        base_size = 0.05
        drag_handle_size = base_size  # Default fallback
        rv3d_ref = getattr(self, '_rv3d', rv3d)
        if rv3d_ref:
            view_distance = rv3d_ref.view_distance
            drag_handle_size = base_size * (view_distance / 10.0)
            drag_handle_size = max(0.005, min(drag_handle_size, 5.0))
        rotate_handle_size = drag_handle_size  # Same size as drag handle for easier clicking
        
        # Use stored handle positions if available, otherwise calculate from center
        drag_handle_pos = getattr(self, '_drag_handle_pos_3d', None)
        rotate_handle_pos = getattr(self, '_rotate_handle_pos_3d', None)
        
        if drag_handle_pos is None or rotate_handle_pos is None:
            # Fallback: calculate from center
            center = Vector(self.center)
            
            # Get plane basis vectors
            if self.axis == 'X':
                u = Vector((0, 1, 0))
                v = Vector((0, 0, 1))
                rotation_axis = Vector((0, 0, 1))
            elif self.axis == 'Y':
                u = Vector((1, 0, 0))
                v = Vector((0, 0, 1))
                rotation_axis = Vector((0, 0, 1))
            else:  # Z
                u = Vector((1, 0, 0))
                v = Vector((0, 1, 0))
                rotation_axis = Vector((1, 0, 0))
            
            # Apply rotation
            angle_rad = math.radians(self.angle)
            if abs(angle_rad) > 0.001:
                rot_matrix = Matrix.Rotation(angle_rad, 3, rotation_axis)
                u = rot_matrix @ u
                v = rot_matrix @ v
            
            # Calculate handle positions
            drag_handle_pos = center  # Center of plane
            rotate_handle_pos = center + u * self._handle_distance  # At edge of plane
        
        # Convert handle centers to 2D
        drag_2d = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, drag_handle_pos)
        rotate_2d = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, rotate_handle_pos)
        
        if drag_2d is None or rotate_2d is None:
            return None
        
        # Calculate pixel-perfect radius by projecting handle edge to screen space
        # Project a point at the edge of the drag handle sphere
        drag_edge_pos = drag_handle_pos + Vector((drag_handle_size, 0, 0))
        drag_edge_2d = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, drag_edge_pos)
        
        rotate_edge_pos = rotate_handle_pos + Vector((rotate_handle_size, 0, 0))
        rotate_edge_2d = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, rotate_edge_pos)
        
        if drag_edge_2d is None or rotate_edge_2d is None:
            return None
        
        # Calculate screen-space radius (including outline)
        drag_radius_pixels = (drag_2d - drag_edge_2d).length * 1.16  # Account for outline
        rotate_radius_pixels = (rotate_2d - rotate_edge_2d).length * 1.16  # Account for outline
        
        mouse_pos = Vector((event.mouse_region_x, event.mouse_region_y))
        
        # Check rotation handle first (smaller, higher priority)
        if (mouse_pos - rotate_2d).length <= rotate_radius_pixels:
            return 'ROTATE'
        
        # Check drag handle
        if (mouse_pos - drag_2d).length <= drag_radius_pixels:
            return 'DRAG'
        
        return None
    
    def is_point_occluded(self, context, world_co):
        """Check if a point is occluded by geometry (not visible to camera)"""
        # Check if x-ray mode is enabled
        shading = context.space_data.shading
        if shading.show_xray:
            return False  # X-ray enabled, nothing is occluded

        # Perform ray cast from camera to point
        region = context.region
        rv3d = context.region_data

        # Get ray from viewport to the point
        screen_co = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, world_co)
        if not screen_co:
            return True  # If can't project to screen, consider occluded

        # Get view vector and ray origin from the viewport camera position
        view_vec = bpy_extras.view3d_utils.region_2d_to_vector_3d(region, rv3d, screen_co)

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

        # Use scene.ray_cast for viewport-based occlusion checking
        depsgraph = context.evaluated_depsgraph_get()

        # Cast ray from camera toward infinity to find first hit
        result = context.scene.ray_cast(depsgraph, ray_origin, view_vec)
        hit, location, _normal, _index, hit_obj, _matrix = result

        if hit and hit_obj:
            hit_distance = (location - ray_origin).length
            distance_to_target = (location - world_co).length

            # Check if the hit point is very close to our target point
            if distance_to_target < tolerance:
                return False

            # If something is closer to the camera than our target point, it's occluded
            if hit_distance < target_distance - tolerance:
                return True  # Something is in front of our point

        return False

    def _raycast_edit_mode_object(self, context, obj, ray_origin, ray_direction):
        """Manually ray-cast against an object in edit mode.

        Returns: (hit, location, normal, face_index) or (False, None, None, None)
        """
        if obj.mode != 'EDIT' or obj.type != 'MESH':
            return False, None, None, None

        # Get bmesh data
        bm = bmesh.from_edit_mesh(obj.data)
        matrix = obj.matrix_world
        matrix_inv = matrix.inverted()

        # Transform ray to object space
        ray_origin_local = matrix_inv @ ray_origin
        ray_dir_local = matrix_inv.to_3x3() @ ray_direction
        ray_dir_local.normalize()

        closest_hit = None
        closest_distance = float('inf')
        closest_face_idx = None
        closest_normal = None

        # Check all faces
        for face_idx, face in enumerate(bm.faces):
            if face.hide:
                continue

            # Get face vertices
            verts = [v.co for v in face.verts]

            # Triangulate face and test each triangle
            for i in range(1, len(verts) - 1):
                v0, v1, v2 = verts[0], verts[i], verts[i + 1]

                # Möller-Trumbore ray-triangle intersection
                edge1 = v1 - v0
                edge2 = v2 - v0
                h = ray_dir_local.cross(edge2)
                a = edge1.dot(h)

                # Ray parallel to triangle
                if abs(a) < 0.0000001:
                    continue

                f = 1.0 / a
                s = ray_origin_local - v0
                u = f * s.dot(h)

                if u < 0.0 or u > 1.0:
                    continue

                q = s.cross(edge1)
                v = f * ray_dir_local.dot(q)

                if v < 0.0 or u + v > 1.0:
                    continue

                # Compute intersection distance
                t = f * edge2.dot(q)

                if t > 0.0000001 and t < closest_distance:
                    closest_distance = t
                    closest_hit = ray_origin_local + ray_dir_local * t
                    closest_face_idx = face_idx
                    closest_normal = face.normal

        if closest_hit:
            # Transform back to world space
            hit_world = matrix @ closest_hit
            normal_world = (matrix.to_3x3() @ closest_normal).normalized()
            return True, hit_world, normal_world, closest_face_idx

        return False, None, None, None

    def _build_edge_cache(self, context):
        """Build a cache of visible edges and faces for intersection snapping.
        
        Called once when dragging starts to avoid recollecting geometry on every mouse move.
        Returns a dict with 'edges' and 'faces' lists.
        - edges: (v1_world, v2_world, obj, edge_idx, edge_vert_indices)
        - faces: (face_verts_world, face_normal, obj, face_idx, face_vert_indices)
        """
        region = context.region
        rv3d = context.region_data
        view_dir = rv3d.view_rotation @ Vector((0, 0, -1))
        
        cached_edges = []
        cached_faces = []
        
        for obj in context.visible_objects:
            if obj.type != 'MESH':
                continue
            
            mx = obj.matrix_world
            normal_matrix = mx.to_3x3().inverted().transposed()  # Correct normal transformation
            is_edit_mode = obj.mode == 'EDIT'
            
            if is_edit_mode:
                bm = bmesh.from_edit_mesh(obj.data)
                
                # Cache edges
                for i, edge in enumerate(bm.edges):
                    if edge.hide:
                        continue
                    v1_world = mx @ edge.verts[0].co
                    v2_world = mx @ edge.verts[1].co
                    edge_vert_indices = (edge.verts[0].index, edge.verts[1].index)
                    
                    # Check edge visibility via backface culling
                    is_visible = True
                    if edge.link_faces:
                        is_visible = False
                        for face in edge.link_faces:
                            face_normal = normal_matrix @ face.normal
                            if face_normal.dot(view_dir) < 0:
                                is_visible = True
                                break
                    if is_visible:
                        cached_edges.append((v1_world, v2_world, obj, i, edge_vert_indices))
                
                # Cache faces
                for i, face in enumerate(bm.faces):
                    if face.hide:
                        continue
                    face_normal = normal_matrix @ face.normal
                    # Only include front-facing faces
                    if face_normal.dot(view_dir) < 0:
                        face_verts = [mx @ v.co for v in face.verts]
                        face_vert_indices = set(v.index for v in face.verts)
                        # Compute face bounding box for early rejection
                        f_min = Vector((min(v.x for v in face_verts), min(v.y for v in face_verts), min(v.z for v in face_verts)))
                        f_max = Vector((max(v.x for v in face_verts), max(v.y for v in face_verts), max(v.z for v in face_verts)))
                        cached_faces.append((face_verts, face_normal.normalized(), obj, i, face_vert_indices, (f_min, f_max)))
            else:
                mesh = obj.data
                
                # Cache edges
                for i, edge in enumerate(mesh.edges):
                    if hasattr(edge, 'hide') and edge.hide:
                        continue
                    v1_world = mx @ mesh.vertices[edge.vertices[0]].co
                    v2_world = mx @ mesh.vertices[edge.vertices[1]].co
                    edge_vert_indices = (edge.vertices[0], edge.vertices[1])
                    cached_edges.append((v1_world, v2_world, obj, i, edge_vert_indices))
                
                # Cache faces
                for i, poly in enumerate(mesh.polygons):
                    if hasattr(poly, 'hide') and poly.hide:
                        continue
                    face_normal = normal_matrix @ poly.normal
                    # Only include front-facing faces
                    if face_normal.dot(view_dir) < 0:
                        face_verts = [mx @ mesh.vertices[vi].co for vi in poly.vertices]
                        face_vert_indices = set(poly.vertices)
                        # Compute face bounding box for early rejection
                        f_min = Vector((min(v.x for v in face_verts), min(v.y for v in face_verts), min(v.z for v in face_verts)))
                        f_max = Vector((max(v.x for v in face_verts), max(v.y for v in face_verts), max(v.z for v in face_verts)))
                        cached_faces.append((face_verts, face_normal.normalized(), obj, i, face_vert_indices, (f_min, f_max)))

        # Also include the original object if it's hidden (not in visible_objects)
        if hasattr(self, '_original_obj') and self._original_obj:
            obj = self._original_obj
            # Check if object was already processed
            if obj not in context.visible_objects and obj.type == 'MESH':
                mx = obj.matrix_world
                normal_matrix = mx.to_3x3().inverted().transposed()
                is_edit_mode = obj.mode == 'EDIT'

                if is_edit_mode:
                    bm = bmesh.from_edit_mesh(obj.data)

                    # Cache edges
                    for i, edge in enumerate(bm.edges):
                        if edge.hide:
                            continue
                        v1_world = mx @ edge.verts[0].co
                        v2_world = mx @ edge.verts[1].co
                        edge_vert_indices = (edge.verts[0].index, edge.verts[1].index)

                        # Check edge visibility via backface culling
                        is_visible = True
                        if edge.link_faces:
                            is_visible = False
                            for face in edge.link_faces:
                                face_normal = normal_matrix @ face.normal
                                if face_normal.dot(view_dir) < 0:
                                    is_visible = True
                                    break
                        if is_visible:
                            cached_edges.append((v1_world, v2_world, obj, i, edge_vert_indices))

                    # Cache faces
                    for i, face in enumerate(bm.faces):
                        if face.hide:
                            continue
                        face_normal = normal_matrix @ face.normal
                        # Only include front-facing faces
                        if face_normal.dot(view_dir) < 0:
                            face_verts = [mx @ v.co for v in face.verts]
                            face_vert_indices = set(v.index for v in face.verts)
                            # Compute face bounding box for early rejection
                            f_min = Vector((min(v.x for v in face_verts), min(v.y for v in face_verts), min(v.z for v in face_verts)))
                            f_max = Vector((max(v.x for v in face_verts), max(v.y for v in face_verts), max(v.z for v in face_verts)))
                            cached_faces.append((face_verts, face_normal.normalized(), obj, i, face_vert_indices, (f_min, f_max)))
                else:
                    mesh = obj.data

                    # Cache edges
                    for i, edge in enumerate(mesh.edges):
                        if hasattr(edge, 'hide') and edge.hide:
                            continue
                        v1_world = mx @ mesh.vertices[edge.vertices[0]].co
                        v2_world = mx @ mesh.vertices[edge.vertices[1]].co
                        edge_vert_indices = (edge.vertices[0], edge.vertices[1])
                        cached_edges.append((v1_world, v2_world, obj, i, edge_vert_indices))

                    # Cache faces
                    for i, poly in enumerate(mesh.polygons):
                        if hasattr(poly, 'hide') and poly.hide:
                            continue
                        face_normal = normal_matrix @ poly.normal
                        # Only include front-facing faces
                        if face_normal.dot(view_dir) < 0:
                            face_verts = [mx @ mesh.vertices[vi].co for vi in poly.vertices]
                            face_vert_indices = set(poly.vertices)
                            # Compute face bounding box for early rejection
                            f_min = Vector((min(v.x for v in face_verts), min(v.y for v in face_verts), min(v.z for v in face_verts)))
                            f_max = Vector((max(v.x for v in face_verts), max(v.y for v in face_verts), max(v.z for v in face_verts)))
                            cached_faces.append((face_verts, face_normal.normalized(), obj, i, face_vert_indices, (f_min, f_max)))

        return {'edges': cached_edges, 'faces': cached_faces}
    
    def try_snap_to_edge_intersection(self, context, coord):
        """Snap to edge-polygon intersections (Modo-style).
        
        Finds where edges pierce through polygons/faces in 3D space.
        Optimized with screen-space culling and deferred occlusion checking.
        """
        region = context.region
        rv3d = context.region_data
        
        snap_distance = 20  # Screen-space snap radius in pixels
        snap_distance_sq = snap_distance * snap_distance  # Pre-compute squared distance
        closest_snap = None
        closest_screen_dist_sq = snap_distance_sq
        
        cursor_x = coord[0]
        cursor_y = coord[1]
        
        # Use cached geometry if available, otherwise build cache on-the-fly
        if self._edge_cache is not None:
            cache = self._edge_cache
        else:
            cache = self._build_edge_cache(context)
        
        cached_edges = cache['edges']
        cached_faces = cache['faces']
        
        if not cached_edges or not cached_faces:
            return None
        
        # Use cached screen-space edge data if available, otherwise compute it
        # This is expensive so we cache it per-frame
        edge_screen_data = cache.get('edge_screen_data')
        if edge_screen_data is None:
            edge_screen_data = []
            loc_3d_to_2d = bpy_extras.view3d_utils.location_3d_to_region_2d
            for v1_world, v2_world, edge_obj, edge_idx, edge_vert_indices in cached_edges:
                v1_screen = loc_3d_to_2d(region, rv3d, v1_world)
                v2_screen = loc_3d_to_2d(region, rv3d, v2_world)
                if v1_screen is None or v2_screen is None:
                    edge_screen_data.append(None)
                    continue
                
                # Compute screen-space bounding box with snap_distance margin
                v1x, v1y = v1_screen.x, v1_screen.y
                v2x, v2y = v2_screen.x, v2_screen.y
                if v1x < v2x:
                    min_x, max_x = v1x - snap_distance, v2x + snap_distance
                else:
                    min_x, max_x = v2x - snap_distance, v1x + snap_distance
                if v1y < v2y:
                    min_y, max_y = v1y - snap_distance, v2y + snap_distance
                else:
                    min_y, max_y = v2y - snap_distance, v1y + snap_distance
                edge_screen_data.append((min_x, max_x, min_y, max_y))
            cache['edge_screen_data'] = edge_screen_data
        
        # Local reference for faster lookup in tight loop
        loc_3d_to_2d = bpy_extras.view3d_utils.location_3d_to_region_2d
        intersect_line_plane = mathutils.geometry.intersect_line_plane
        
        # Test each edge against each face for intersections
        for edge_i, (v1_world, v2_world, edge_obj, edge_idx, edge_vert_indices) in enumerate(cached_edges):
            edge_screen = edge_screen_data[edge_i]
            if edge_screen is None:
                continue
            
            e_min_x, e_max_x, e_min_y, e_max_y = edge_screen
            
            # Quick screen-space culling: skip edges whose bounding box doesn't contain cursor
            if cursor_x < e_min_x or cursor_x > e_max_x or cursor_y < e_min_y or cursor_y > e_max_y:
                continue
            
            edge_vec = v2_world - v1_world
            edge_len_sq = edge_vec.length_squared
            if edge_len_sq < 0.00000001:
                continue
            
            # Pre-compute edge bounding box once per edge (not per face!)
            v1x, v1y, v1z = v1_world.x, v1_world.y, v1_world.z
            v2x, v2y, v2z = v2_world.x, v2_world.y, v2_world.z
            if v1x < v2x:
                e_min_x_3d, e_max_x_3d = v1x, v2x
            else:
                e_min_x_3d, e_max_x_3d = v2x, v1x
            if v1y < v2y:
                e_min_y_3d, e_max_y_3d = v1y, v2y
            else:
                e_min_y_3d, e_max_y_3d = v2y, v1y
            if v1z < v2z:
                e_min_z_3d, e_max_z_3d = v1z, v2z
            else:
                e_min_z_3d, e_max_z_3d = v2z, v1z
            
            # Extract edge vertex indices once
            ev0, ev1 = edge_vert_indices
            
            for face_verts, face_normal, face_obj, face_idx, face_vert_indices, face_bbox in cached_faces:
                # Skip if edge is connected to the face (shares vertices)
                if edge_obj == face_obj:
                    if ev0 in face_vert_indices or ev1 in face_vert_indices:
                        continue
                
                # Bounding box early rejection: check if edge intersects face bbox
                f_min, f_max = face_bbox
                
                if e_max_x_3d < f_min.x or e_min_x_3d > f_max.x:
                    continue
                if e_max_y_3d < f_min.y or e_min_y_3d > f_max.y:
                    continue
                if e_max_z_3d < f_min.z or e_min_z_3d > f_max.z:
                    continue
                
                # Find intersection of edge (as infinite line) with the face plane
                isect = intersect_line_plane(v1_world, v2_world, face_verts[0], face_normal)
                
                if isect is None:
                    continue
                
                # Check if intersection point is within the edge segment
                to_isect = isect - v1_world
                
                # Project to find t
                t = to_isect.dot(edge_vec) / edge_len_sq
                
                # Must be strictly within the edge (not at endpoints)
                if t <= 0.001 or t >= 0.999:
                    continue
                
                # Project intersection to screen space and check distance to cursor EARLY
                # This is a cheap check that can save expensive point-in-polygon tests
                isect_screen = loc_3d_to_2d(region, rv3d, isect)
                if isect_screen is None:
                    continue
                
                dx = isect_screen.x - cursor_x
                dy = isect_screen.y - cursor_y
                screen_dist_sq = dx * dx + dy * dy
                if screen_dist_sq >= closest_screen_dist_sq:
                    continue
                
                # Check if intersection point is inside the polygon (inlined for speed)
                is_inside = False
                nv = len(face_verts)
                if nv == 3:
                    is_inside = self._point_in_triangle_fast(isect, face_verts[0], face_verts[1], face_verts[2], face_normal)
                elif nv == 4:
                    is_inside = (self._point_in_triangle_fast(isect, face_verts[0], face_verts[1], face_verts[2], face_normal) or
                                 self._point_in_triangle_fast(isect, face_verts[0], face_verts[2], face_verts[3], face_normal))
                else:
                    fv0 = face_verts[0]
                    for i in range(1, nv - 1):
                        if self._point_in_triangle_fast(isect, fv0, face_verts[i], face_verts[i+1], face_normal):
                            is_inside = True
                            break
                
                if not is_inside:
                    continue
                
                # Found a valid candidate - update closest
                closest_screen_dist_sq = screen_dist_sq
                closest_snap = isect
        
        # Only check occlusion for the single best candidate (not inside the loop!)
        if closest_snap is not None:
            if self.is_point_occluded(context, closest_snap):
                return None
        
        return closest_snap
    
    def _point_in_triangle_fast(self, p, v0, v1, v2, normal):
        """Optimized point-in-triangle test using barycentric coordinates."""
        # Compute vectors
        v0v1 = v1 - v0
        v0v2 = v2 - v0
        v0p = p - v0
        
        # Compute dot products
        dot00 = v0v1.x * v0v1.x + v0v1.y * v0v1.y + v0v1.z * v0v1.z
        dot01 = v0v1.x * v0v2.x + v0v1.y * v0v2.y + v0v1.z * v0v2.z
        dot02 = v0v1.x * v0p.x + v0v1.y * v0p.y + v0v1.z * v0p.z
        dot11 = v0v2.x * v0v2.x + v0v2.y * v0v2.y + v0v2.z * v0v2.z
        dot12 = v0v2.x * v0p.x + v0v2.y * v0p.y + v0v2.z * v0p.z
        
        # Compute barycentric coordinates
        inv_denom = dot00 * dot11 - dot01 * dot01
        if abs(inv_denom) < 1e-10:
            return False
        inv_denom = 1.0 / inv_denom
        u = (dot11 * dot02 - dot01 * dot12) * inv_denom
        v = (dot00 * dot12 - dot01 * dot02) * inv_denom
        
        # Check if point is in triangle (with small tolerance)
        return (u >= -0.0001) and (v >= -0.0001) and (u + v <= 1.0001)
    
    def cleanup(self, context, skip_preview_removal=False):
        """Remove draw handler and preview object"""
        if self._handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None
        # Clear edge cache
        self._edge_cache = None
        # Restore original object visibility (only if object still exists)
        if hasattr(self, '_original_obj') and self._original_obj:
            try:
                # Check if object still exists before accessing it
                if self._original_obj.name in bpy.data.objects:
                    self._original_obj.hide_set(self._original_hide)
            except ReferenceError:
                # Object was deleted, ignore
                pass
        # Restore gizmo visibility
        if hasattr(self, '_original_show_gizmo') and self._original_show_gizmo is not None:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            space.show_gizmo = self._original_show_gizmo
                            break
                    break
        if not skip_preview_removal:
            self._remove_preview(context)
        # Clear stored mesh backup AFTER _remove_preview has used it
        if hasattr(self, '_original_mesh_backup'):
            self._original_mesh_backup = None
        context.area.header_text_set(None)
        context.area.tag_redraw()
    
    def cancel(self, context):
        """Called by Blender when the operator is externally cancelled (e.g., tool switch, mode change).
        This ensures proper cleanup even when the operator is dropped without ESC."""
        self.cleanup(context)
    
    def _remove_preview(self, context):
        """Remove preview and restore original mesh state"""
        obj = self._original_obj if hasattr(self, '_original_obj') and self._original_obj else context.object
        
        if obj and obj.mode == 'EDIT':
            # In Edit Mode: Restore original mesh from backup
            # This is necessary because slice_along_mirror uses bisect which modifies the original geometry
            if hasattr(self, '_original_mesh_backup') and self._original_mesh_backup:
                mesh = obj.data
                bm = bmesh.from_edit_mesh(mesh)
                
                # Remove all existing geometry
                bmesh.ops.delete(bm, geom=list(bm.verts), context='VERTS')
                
                # Restore from backup
                backup = self._original_mesh_backup
                
                # Recreate vertices
                new_verts = []
                for co, selected in backup['verts']:
                    v = bm.verts.new(co)
                    v.select = selected
                    new_verts.append(v)
                
                bm.verts.ensure_lookup_table()
                bm.verts.index_update()
                
                # Recreate edges
                for vert_indices, selected, smooth in backup['edges']:
                    try:
                        e = bm.edges.new([new_verts[i] for i in vert_indices])
                        e.select = selected
                        e.smooth = smooth
                    except (ValueError, IndexError):
                        pass
                
                bm.edges.ensure_lookup_table()
                
                # Recreate faces
                for vert_indices, selected, smooth, mat_idx in backup['faces']:
                    try:
                        f = bm.faces.new([new_verts[i] for i in vert_indices])
                        f.select = selected
                        f.smooth = smooth
                        f.material_index = mat_idx
                    except (ValueError, IndexError):
                        pass
                
                bm.faces.ensure_lookup_table()
                bm.normal_update()
                bmesh.update_edit_mesh(mesh)
                
                self._preview_vert_indices = []
                self._original_mesh_backup = None
            elif hasattr(self, '_preview_vert_indices') and self._preview_vert_indices:
                # Fallback: just delete preview vertices (for cases without bisect)
                mesh = obj.data
                bm = bmesh.from_edit_mesh(mesh)
                bm.verts.ensure_lookup_table()
                
                # First, unhide any hidden original faces
                face_layer = bm.faces.layers.int.get("original_mirror_faces")
                if face_layer:
                    for f in bm.faces:
                        if f[face_layer] == 1:
                            f.hide_set(False)
                
                # Get valid vertices by index
                geom_to_delete = []
                for idx in self._preview_vert_indices:
                    if idx < len(bm.verts):
                        v = bm.verts[idx]
                        if v.is_valid:
                            geom_to_delete.append(v)
                
                if geom_to_delete:
                    bmesh.ops.delete(bm, geom=geom_to_delete, context='VERTS')
                
                # Restore original selection
                if hasattr(self, '_original_selection'):
                    bm.verts.ensure_lookup_table()
                    for v in bm.verts:
                        v.select = v.index in self._original_selection
                
                bmesh.update_edit_mesh(mesh)
                
                self._preview_vert_indices = []
        else:
            # In Object Mode: Remove separate preview object
            if self._preview_obj is not None:
                bpy.data.objects.remove(self._preview_obj, do_unlink=True)
                self._preview_obj = None
                self._original_verts = []
    
    def _update_preview_appearance(self):
        """Update the visual appearance of the preview based on replace_source mode"""
        import bpy
        
        # Get the current mode
        is_edit_mode = hasattr(self, '_original_mode') and self._original_mode == 'EDIT'
        
        if is_edit_mode:
            # In Edit Mode: hide/show the original selection to preview replacement
            if hasattr(self, '_original_obj') and self._original_obj:
                mesh = self._original_obj.data
                bm = bmesh.from_edit_mesh(mesh)
                bm.faces.ensure_lookup_table()
                
                face_layer = bm.faces.layers.int.get("original_mirror_faces")
                
                if face_layer:
                    if self.replace_source:
                        # Hide the original selection faces to show what replace will look like
                        for f in bm.faces:
                            if f[face_layer] == 1:
                                f.hide_set(True)
                    else:
                        # Show the original selection faces
                        for f in bm.faces:
                            if f[face_layer] == 1:
                                f.hide_set(False)
                    
                    bmesh.update_edit_mesh(mesh)
        else:
            # In Object Mode: update separate preview object appearance
            if not self._preview_obj:
                return
            
            if self.replace_source:
                # Hide the original object in Object Mode
                if hasattr(self, '_original_obj') and self._original_obj:
                    self._original_obj.hide_set(True)
                self._preview_obj.show_transparent = True
                self._preview_obj.color = (1.0, 1.0, 1.0, 0.9)  # More opaque
            else:
                # Show the original object
                if hasattr(self, '_original_obj') and self._original_obj:
                    self._original_obj.hide_set(self._original_hide)
                self._preview_obj.show_transparent = True
                self._preview_obj.color = (0.5, 0.7, 1.0, 0.5)  # Semi-transparent blue tint
    
    def _update_status_text(self, context):
        """Update the header text with current axis"""
        replace_status = "ON" if self.replace_source else "OFF"
        slice_status = "ON" if self.slice_along_mirror else "OFF"
        weld_status = "ON" if self.weld_seam else "OFF"
        flip_status = "ON" if self.flip_side else "OFF"
        context.area.header_text_set(
            f"Mirror [{self.axis}] | Angle: {self.angle:.0f}° | Slice: {slice_status} (S) | Weld: {weld_status} (W) | Flip: {flip_status} (F) | Replace: {replace_status} (R)  |  Z: Axis  |  X: Rotate  |  Space: Confirm  |  Esc: Cancel"
        )
    
    def _update_center_from_mouse(self, context, event):
        """Update center position based on mouse movement with full snap support"""
        region = context.region
        rv3d = context.region_data
        
        if not self._mouse_start or not self._center_start:
            return
        
        # Get current mouse position
        mouse_current = Vector((event.mouse_region_x, event.mouse_region_y))
        tool_settings = context.scene.tool_settings
        
        # Toggle snapping with CTRL (like default Blender tools)
        snap_enabled = tool_settings.use_snap
        if event.ctrl:
            snap_enabled = not snap_enabled
        
        # Calculate handle offset from center (handle is at center - u*size - v*size)
        size = 2.0
        if self.axis == 'X':
            u = Vector((0, 1, 0))
            v = Vector((0, 0, 1))
            rotation_axis = Vector((0, 0, 1))
        elif self.axis == 'Y':
            u = Vector((1, 0, 0))
            v = Vector((0, 0, 1))
            rotation_axis = Vector((0, 0, 1))
        else:  # Z
            u = Vector((1, 0, 0))
            v = Vector((0, 1, 0))
            rotation_axis = Vector((1, 0, 0))
        
        # Apply current rotation
        angle_rad = math.radians(self.angle)
        if abs(angle_rad) > 0.001:
            rot_matrix = Matrix.Rotation(angle_rad, 3, rotation_axis)
            u = rot_matrix @ u
            v = rot_matrix @ v
        
        # Try snapping to scene geometry if enabled
        snapped_handle_location = None
        self._snapped_to_intersection = False  # Reset snap state
        if snap_enabled:
            snap_elements = tool_settings.snap_elements
            
            # First try edge intersection snapping (highest priority - always try when snap enabled)
            intersection_snap = self.try_snap_to_edge_intersection(context, mouse_current)
            if intersection_snap:
                snapped_handle_location = intersection_snap
                self._snapped_to_intersection = True  # Mark that we snapped to intersection
            
            # Geometric snapping (VERTEX, EDGE, FACE, etc.) - only if no intersection found
            # This prevents vertex snapping from overriding intersection snapping
            if snapped_handle_location is None and any(elem in snap_elements for elem in {'VERTEX', 'EDGE', 'FACE', 'VOLUME',
                                                       'EDGE_MIDPOINT', 'EDGE_PERPENDICULAR'}):
                # Cast ray from mouse position
                view_vector = bpy_extras.view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse_current)
                ray_origin = bpy_extras.view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse_current)

                # First try ray-casting against active edit-mode object (scene.ray_cast skips it)
                result = False
                location = None
                normal = None
                face_index = None
                obj = None
                matrix = None

                active_obj = context.active_object
                if active_obj and active_obj.mode == 'EDIT' and active_obj.type == 'MESH':
                    result, location, normal, face_index = self._raycast_edit_mode_object(
                        context, active_obj, ray_origin, view_vector
                    )
                    if result:
                        obj = active_obj
                        matrix = active_obj.matrix_world

                # If no hit on edit-mode object, use scene ray cast for other objects
                if not result:
                    result, location, normal, face_index, obj, matrix = context.scene.ray_cast(
                        context.view_layer.depsgraph, ray_origin, view_vector
                    )
                
                if result and obj and obj.type == 'MESH':
                    # Get mesh data (use bmesh for edit mode, evaluated mesh otherwise)
                    is_edit_mode = obj.mode == 'EDIT'
                    if is_edit_mode:
                        bm = bmesh.from_edit_mesh(obj.data)
                        bm.faces.ensure_lookup_table()
                    else:
                        depsgraph = context.view_layer.depsgraph
                        obj_eval = obj.evaluated_get(depsgraph)
                        mesh = obj_eval.data

                    # Transform hit location to object local space
                    location_local = matrix.inverted() @ location

                    # Maximum screen-space distance for snapping (pixels)
                    max_snap_pixels = 15

                    # Track all snap candidates across different snap types
                    snap_candidates = []  # List of (screen_dist, location_local, snap_type)
                    
                    # Check VERTEX snapping
                    if 'VERTEX' in snap_elements:
                        # Get vertices from the hit face
                        if is_edit_mode:
                            if face_index < len(bm.faces):
                                face = bm.faces[face_index]
                                for vert in face.verts:
                                    vert_world = matrix @ vert.co
                                    vert_screen = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, vert_world)

                                    if vert_screen:
                                        screen_dist = (Vector(vert_screen) - mouse_current).length
                                        if screen_dist < max_snap_pixels:
                                            snap_candidates.append((screen_dist, vert.co.copy(), 'VERTEX'))
                        else:
                            if face_index < len(mesh.polygons):
                                face = mesh.polygons[face_index]
                                for vert_idx in face.vertices:
                                    vert = mesh.vertices[vert_idx]
                                    vert_world = matrix @ vert.co
                                    vert_screen = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, vert_world)

                                    if vert_screen:
                                        screen_dist = (Vector(vert_screen) - mouse_current).length
                                        if screen_dist < max_snap_pixels:
                                            snap_candidates.append((screen_dist, vert.co.copy(), 'VERTEX'))
                    
                    # Check EDGE_MIDPOINT snapping
                    if 'EDGE_MIDPOINT' in snap_elements:
                        if is_edit_mode:
                            if face_index < len(bm.faces):
                                face = bm.faces[face_index]
                                # Get face edges
                                for i, vert in enumerate(face.verts):
                                    next_vert = face.verts[(i + 1) % len(face.verts)]
                                    v1 = vert.co
                                    v2 = next_vert.co
                                    midpoint = (v1 + v2) / 2
                                    midpoint_world = matrix @ midpoint
                                    midpoint_screen = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, midpoint_world)

                                    if midpoint_screen:
                                        screen_dist = (Vector(midpoint_screen) - mouse_current).length
                                        if screen_dist < max_snap_pixels:
                                            snap_candidates.append((screen_dist, midpoint, 'EDGE_MIDPOINT'))
                        else:
                            if face_index < len(mesh.polygons):
                                face = mesh.polygons[face_index]
                                # Get face edges
                                for i, vert_idx in enumerate(face.vertices):
                                    next_idx = face.vertices[(i + 1) % len(face.vertices)]
                                    v1 = mesh.vertices[vert_idx].co
                                    v2 = mesh.vertices[next_idx].co
                                    midpoint = (v1 + v2) / 2
                                    midpoint_world = matrix @ midpoint
                                    midpoint_screen = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, midpoint_world)

                                    if midpoint_screen:
                                        screen_dist = (Vector(midpoint_screen) - mouse_current).length
                                        if screen_dist < max_snap_pixels:
                                            snap_candidates.append((screen_dist, midpoint, 'EDGE_MIDPOINT'))
                    
                    # Check EDGE snapping
                    if 'EDGE' in snap_elements or 'EDGE_PERPENDICULAR' in snap_elements:
                        if is_edit_mode:
                            if face_index < len(bm.faces):
                                face = bm.faces[face_index]
                                for i, vert in enumerate(face.verts):
                                    next_vert = face.verts[(i + 1) % len(face.verts)]
                                    v1 = vert.co
                                    v2 = next_vert.co

                                    # Find closest point on edge
                                    edge_vec = v2 - v1
                                    edge_len = edge_vec.length
                                    if edge_len > 0:
                                        edge_dir = edge_vec / edge_len
                                        t = max(0, min(edge_len, (location_local - v1).dot(edge_dir)))
                                        point_on_edge = v1 + edge_dir * t
                                        point_world = matrix @ point_on_edge
                                        point_screen = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, point_world)

                                        if point_screen:
                                            screen_dist = (Vector(point_screen) - mouse_current).length
                                            if screen_dist < max_snap_pixels:
                                                snap_candidates.append((screen_dist, point_on_edge, 'EDGE'))
                        else:
                            if face_index < len(mesh.polygons):
                                face = mesh.polygons[face_index]
                                for i, vert_idx in enumerate(face.vertices):
                                    next_idx = face.vertices[(i + 1) % len(face.vertices)]
                                    v1 = mesh.vertices[vert_idx].co
                                    v2 = mesh.vertices[next_idx].co

                                    # Find closest point on edge
                                    edge_vec = v2 - v1
                                    edge_len = edge_vec.length
                                    if edge_len > 0:
                                        edge_dir = edge_vec / edge_len
                                        t = max(0, min(edge_len, (location_local - v1).dot(edge_dir)))
                                        point_on_edge = v1 + edge_dir * t
                                        point_world = matrix @ point_on_edge
                                        point_screen = bpy_extras.view3d_utils.location_3d_to_region_2d(region, rv3d, point_world)

                                        if point_screen:
                                            screen_dist = (Vector(point_screen) - mouse_current).length
                                            if screen_dist < max_snap_pixels:
                                                snap_candidates.append((screen_dist, point_on_edge, 'EDGE'))
                    
                    # Find the closest snap candidate across all types
                    if snap_candidates:
                        # Sort by screen distance and pick the closest
                        snap_candidates.sort(key=lambda x: x[0])
                        closest_screen_dist, closest_location_local, snap_type = snap_candidates[0]
                        snapped_handle_location = matrix @ closest_location_local
                        self._snapped_to_intersection = True  # Mark that we snapped
                    elif 'FACE' in snap_elements or 'VOLUME' in snap_elements:
                        # FACE or VOLUME - use ray cast result
                        snapped_handle_location = location
                        self._snapped_to_intersection = True  # Mark that we snapped to face/volume
        
        # If no geometric snap, calculate handle position from mouse movement
        if snapped_handle_location is None:
            # Convert to 3D movement
            mouse_3d_start = bpy_extras.view3d_utils.region_2d_to_location_3d(
                region, rv3d, self._mouse_start, self._center_start
            )
            mouse_3d_current = bpy_extras.view3d_utils.region_2d_to_location_3d(
                region, rv3d, mouse_current, self._center_start
            )
            
            if mouse_3d_start and mouse_3d_current:
                # Calculate 3D offset
                offset_3d = mouse_3d_current - mouse_3d_start
                new_center = self._center_start + offset_3d
                
                # Apply grid/increment snapping if enabled
                if snap_enabled and ('INCREMENT' in tool_settings.snap_elements or 
                                               'GRID' in tool_settings.snap_elements):
                    grid_size = tool_settings.snap_grid_size if hasattr(tool_settings, 'snap_grid_size') else 1.0
                    snapped_center = Vector((
                        round(new_center.x / grid_size) * grid_size,
                        round(new_center.y / grid_size) * grid_size,
                        round(new_center.z / grid_size) * grid_size
                    ))
                    # Recalculate offset based on snapped center
                    offset_3d = snapped_center - self._center_start
                    new_center = snapped_center
                
                # Move both handles by the same offset (like Modo)
                # This maintains the rotation handle's position relative to drag handle
                if hasattr(self, '_drag_handle_start') and hasattr(self, '_rotate_handle_start'):
                    self._drag_handle_pos_3d = self._drag_handle_start + offset_3d
                    self._rotate_handle_pos_3d = self._rotate_handle_start + offset_3d
                    
                    # Recalculate plane vectors from new handle positions
                    handle_vec = self._rotate_handle_pos_3d - self._drag_handle_pos_3d
                    if handle_vec.length > 0.001:
                        self._plane_u_vector = handle_vec.normalized()
                        # v is perpendicular to u in the mirror plane
                        if self.axis == 'X':
                            plane_normal = Vector((1, 0, 0))
                        elif self.axis == 'Y':
                            plane_normal = Vector((0, 1, 0))
                        else:  # Z
                            plane_normal = Vector((0, 0, 1))
                        # Use consistent cross product order to avoid flipping
                        v_candidate = plane_normal.cross(self._plane_u_vector).normalized()
                        # Ensure v points generally upward (positive Z for X and Y axes)
                        if self.axis in ['X', 'Y'] and v_candidate.z < 0:
                            v_candidate = -v_candidate
                        self._plane_v_vector = v_candidate
                
                # Update center
                self.center = tuple(new_center)
                return
        
        # Calculate center from snapped handle position
        # When snapping, we need to move both handles by the offset
        if snapped_handle_location:
            # Calculate the offset from the original drag handle position to the snapped position
            if hasattr(self, '_drag_handle_start'):
                old_drag_pos = self._drag_handle_start
            else:
                # Drag handle is now at center
                old_drag_pos = self._center_start
            
            offset = snapped_handle_location - old_drag_pos
            
            # Apply offset to both handles
            if hasattr(self, '_drag_handle_start') and hasattr(self, '_rotate_handle_start'):
                self._drag_handle_pos_3d = snapped_handle_location
                self._rotate_handle_pos_3d = self._rotate_handle_start + offset
                
                # Recalculate plane vectors from new handle positions
                handle_vec = self._rotate_handle_pos_3d - self._drag_handle_pos_3d
                if handle_vec.length > 0.001:
                    self._plane_u_vector = handle_vec.normalized()
                    # v is perpendicular to u in the mirror plane
                    if self.axis == 'X':
                        plane_normal = Vector((1, 0, 0))
                    elif self.axis == 'Y':
                        plane_normal = Vector((0, 1, 0))
                    else:  # Z
                        plane_normal = Vector((0, 0, 1))
                    # Use consistent cross product order to avoid flipping
                    v_candidate = plane_normal.cross(self._plane_u_vector).normalized()
                    # Ensure v points generally upward (positive Z for X and Y axes)
                    if self.axis in ['X', 'Y'] and v_candidate.z < 0:
                        v_candidate = -v_candidate
                    self._plane_v_vector = v_candidate
            
            # Calculate new center from the snapped drag handle position
            # Drag handle is now at center, so just use its position
            new_center = snapped_handle_location
            self.center = tuple(new_center)
    
    def _update_rotation_from_mouse(self, context, event):
        """Update rotation angle and handle distance based on mouse movement"""
        if not self._mouse_start or self._angle_start is None or not hasattr(self, '_drag_handle_pivot'):
            return
        
        region = context.region
        rv3d = context.region_data
        tool_settings = context.scene.tool_settings
        
        # Toggle snapping with CTRL (like default Blender tools)
        snap_enabled = tool_settings.use_snap
        if event.ctrl:
            snap_enabled = not snap_enabled
        
        # Use the stored drag handle position (rotation pivot)
        drag_handle_pos = self._drag_handle_pivot
        
        # Get plane basis vectors (unrotated)
        if self.axis == 'X':
            u = Vector((0, 1, 0))
            v = Vector((0, 0, 1))
            rotation_axis = Vector((0, 0, 1))
        elif self.axis == 'Y':
            u = Vector((1, 0, 0))
            v = Vector((0, 0, 1))
            rotation_axis = Vector((0, 0, 1))
        else:  # Z
            u = Vector((1, 0, 0))
            v = Vector((0, 1, 0))
            rotation_axis = Vector((1, 0, 0))
        
        # Get current mouse position
        mouse_current = Vector((event.mouse_region_x, event.mouse_region_y))
        
        # Try snapping the rotation handle position if enabled (same system as drag handle)
        snapped_rotate_handle_pos = None
        if snap_enabled:
            snap_elements = tool_settings.snap_elements
            
            # Geometric snapping (VERTEX, EDGE, FACE, etc.)
            if any(elem in snap_elements for elem in {'VERTEX', 'EDGE', 'FACE', 'VOLUME',
                                                       'EDGE_MIDPOINT', 'EDGE_PERPENDICULAR'}):
                # Cast ray from mouse position
                view_vector = bpy_extras.view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse_current)
                ray_origin = bpy_extras.view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse_current)

                # First try ray-casting against active edit-mode object (scene.ray_cast skips it)
                result = False
                location = None
                normal = None
                face_index = None
                obj = None
                matrix = None

                active_obj = context.active_object
                if active_obj and active_obj.mode == 'EDIT' and active_obj.type == 'MESH':
                    result, location, normal, face_index = self._raycast_edit_mode_object(
                        context, active_obj, ray_origin, view_vector
                    )
                    if result:
                        obj = active_obj
                        matrix = active_obj.matrix_world

                # If no hit on edit-mode object, use scene ray cast for other objects
                if not result:
                    result, location, normal, face_index, obj, matrix = context.scene.ray_cast(
                        context.view_layer.depsgraph, ray_origin, view_vector
                    )

                if result and obj and obj.type == 'MESH':
                    # Get mesh data (use bmesh for edit mode, evaluated mesh otherwise)
                    is_edit_mode = obj.mode == 'EDIT'
                    if is_edit_mode:
                        bm = bmesh.from_edit_mesh(obj.data)
                        bm.faces.ensure_lookup_table()
                    else:
                        depsgraph = context.view_layer.depsgraph
                        obj_eval = obj.evaluated_get(depsgraph)
                        mesh = obj_eval.data
                    
                    # Transform hit location to object local space
                    location_local = matrix.inverted() @ location

                    # Find closest element based on snap mode
                    if 'VERTEX' in snap_elements:
                        # Find closest vertex
                        min_dist = float('inf')
                        closest_vert = None

                        if is_edit_mode:
                            if face_index < len(bm.faces):
                                face = bm.faces[face_index]
                                for vert in face.verts:
                                    dist = (vert.co - location_local).length
                                    if dist < min_dist:
                                        min_dist = dist
                                        closest_vert = vert.co.copy()
                        else:
                            if face_index < len(mesh.polygons):
                                face = mesh.polygons[face_index]
                                for vert_idx in face.vertices:
                                    vert = mesh.vertices[vert_idx]
                                    dist = (vert.co - location_local).length
                                    if dist < min_dist:
                                        min_dist = dist
                                        closest_vert = vert.co.copy()

                        if closest_vert:
                            snapped_rotate_handle_pos = matrix @ closest_vert

                    elif 'EDGE_MIDPOINT' in snap_elements:
                        # Find closest edge midpoint
                        min_dist = float('inf')
                        closest_midpoint = None

                        if is_edit_mode:
                            if face_index < len(bm.faces):
                                face = bm.faces[face_index]
                                for i, vert in enumerate(face.verts):
                                    next_vert = face.verts[(i + 1) % len(face.verts)]
                                    v1 = vert.co
                                    v2 = next_vert.co
                                    midpoint = (v1 + v2) / 2

                                    dist = (midpoint - location_local).length
                                    if dist < min_dist:
                                        min_dist = dist
                                        closest_midpoint = midpoint
                        else:
                            if face_index < len(mesh.polygons):
                                face = mesh.polygons[face_index]
                                for i, vert_idx in enumerate(face.vertices):
                                    next_idx = face.vertices[(i + 1) % len(face.vertices)]
                                    v1 = mesh.vertices[vert_idx].co
                                    v2 = mesh.vertices[next_idx].co
                                    midpoint = (v1 + v2) / 2

                                    dist = (midpoint - location_local).length
                                    if dist < min_dist:
                                        min_dist = dist
                                        closest_midpoint = midpoint

                        if closest_midpoint:
                            snapped_rotate_handle_pos = matrix @ closest_midpoint

                    elif 'EDGE' in snap_elements or 'EDGE_PERPENDICULAR' in snap_elements:
                        # Find closest point on edges
                        min_dist = float('inf')
                        closest_point = None

                        if is_edit_mode:
                            if face_index < len(bm.faces):
                                face = bm.faces[face_index]
                                for i, vert in enumerate(face.verts):
                                    next_vert = face.verts[(i + 1) % len(face.verts)]
                                    v1 = vert.co
                                    v2 = next_vert.co

                                    edge_vec = v2 - v1
                                    edge_len = edge_vec.length
                                    if edge_len > 0:
                                        edge_dir = edge_vec / edge_len
                                        t = max(0, min(edge_len, (location_local - v1).dot(edge_dir)))
                                        point_on_edge = v1 + edge_dir * t

                                        dist = (point_on_edge - location_local).length
                                        if dist < min_dist:
                                            min_dist = dist
                                            closest_point = point_on_edge
                        else:
                            if face_index < len(mesh.polygons):
                                face = mesh.polygons[face_index]
                                for i, vert_idx in enumerate(face.vertices):
                                    next_idx = face.vertices[(i + 1) % len(face.vertices)]
                                    v1 = mesh.vertices[vert_idx].co
                                    v2 = mesh.vertices[next_idx].co

                                    edge_vec = v2 - v1
                                    edge_len = edge_vec.length
                                    if edge_len > 0:
                                        edge_dir = edge_vec / edge_len
                                        t = max(0, min(edge_len, (location_local - v1).dot(edge_dir)))
                                        point_on_edge = v1 + edge_dir * t

                                        dist = (point_on_edge - location_local).length
                                        if dist < min_dist:
                                            min_dist = dist
                                            closest_point = point_on_edge

                        if closest_point:
                            snapped_rotate_handle_pos = matrix @ closest_point
                    
                    else:  # FACE or VOLUME - use ray cast result
                        snapped_rotate_handle_pos = location
        
        # If no geometric snap, calculate handle position from mouse movement (same as drag handle)
        rotate_handle_3d = None
        if snapped_rotate_handle_pos is None:
            # Convert to 3D movement using drag handle as depth reference
            mouse_3d_current = bpy_extras.view3d_utils.region_2d_to_location_3d(
                region, rv3d, mouse_current, drag_handle_pos
            )
            
            if mouse_3d_current:
                # Apply grid/increment snapping if enabled
                if snap_enabled and ('INCREMENT' in tool_settings.snap_elements or 
                                               'GRID' in tool_settings.snap_elements):
                    grid_size = tool_settings.snap_grid_size if hasattr(tool_settings, 'snap_grid_size') else 1.0
                    mouse_3d_current = Vector((
                        round(mouse_3d_current.x / grid_size) * grid_size,
                        round(mouse_3d_current.y / grid_size) * grid_size,
                        round(mouse_3d_current.z / grid_size) * grid_size
                    ))
                
                rotate_handle_3d = mouse_3d_current
        else:
            # Use snapped position
            rotate_handle_3d = snapped_rotate_handle_pos
        
        if rotate_handle_3d:
            # The rotation handle must stay on the same horizontal plane as the drag handle
            drag_handle_pos = self._drag_handle_pos_3d
            
            # Get the base vectors for this axis
            if self.axis == 'X':
                base_u = Vector((0, 1, 0))
                base_v = Vector((0, 0, 1))
                rotation_axis = Vector((0, 0, 1))
                plane_normal = Vector((1, 0, 0))
            elif self.axis == 'Y':
                base_u = Vector((1, 0, 0))
                base_v = Vector((0, 0, 1))
                rotation_axis = Vector((0, 0, 1))
                plane_normal = Vector((0, 1, 0))
            else:  # Z
                base_u = Vector((1, 0, 0))
                base_v = Vector((0, 1, 0))
                rotation_axis = Vector((1, 0, 0))
                plane_normal = Vector((0, 0, 1))
            
            # Project rotate_handle_3d onto the horizontal plane defined by the drag handle
            # The plane passes through drag_handle_pos and is perpendicular to base_v
            to_target = rotate_handle_3d - drag_handle_pos
            # Remove the v component to keep it at same height
            v_component = to_target.dot(base_v)
            rotate_handle_constrained = rotate_handle_3d - base_v * v_component
            
            # Update the rotate handle position (constrained to same height)
            self._rotate_handle_pos_3d = rotate_handle_constrained
            
            # Calculate the vector from drag to rotate handle
            handle_vec = self._rotate_handle_pos_3d - drag_handle_pos
            
            # DON'T project - use the full handle_vec to allow rotation!
            # The rotation handle has already been constrained to the same height as drag handle,
            # so handle_vec lies in the correct plane (perpendicular to base_v)
            
            if handle_vec.length > 0.001:
                # Calculate actual u direction from handle vector
                u_actual = handle_vec.normalized()
                
                # Calculate distance
                self._handle_distance = max(0.5, handle_vec.length)
                
                # Calculate v as perpendicular to u in the plane
                # For correct orientation: v = plane_normal cross u
                v_actual = plane_normal.cross(u_actual).normalized()
                
                # Ensure v points in the correct direction (upward)
                if v_actual.dot(base_v) < 0:
                    v_actual = -v_actual
                
                # Calculate angle from base_u to u_actual
                # We need to measure the angle in the plane perpendicular to the mirror normal
                # For X axis: angle is measured in YZ plane, but we constrained Z=0, so it's in Y direction
                # We need to look at the actual vector components in the rotation plane
                if self.axis == 'X':
                    # Rotation in YZ plane (actually Y since Z is constrained to 0)
                    # base_u = (0, 1, 0), so unrotated points in +Y
                    # Measure angle from +Y axis
                    angle_rad = math.atan2(u_actual.x, u_actual.y)
                elif self.axis == 'Y':
                    # Rotation in XZ plane (actually X since Z is constrained to 0)
                    # base_u = (1, 0, 0), so unrotated points in +X
                    # Measure angle from +X axis
                    angle_rad = math.atan2(u_actual.y, u_actual.x)
                else:  # Z
                    # Rotation in XY plane
                    # base_u = (1, 0, 0), so unrotated points in +X
                    # Measure angle from +X axis
                    angle_rad = math.atan2(u_actual.y, u_actual.x)
                
                new_angle = math.degrees(angle_rad) % 360.0
                self.angle = new_angle
                
                # Calculate center from drag handle and actual vectors
                # Drag handle is now at center, so just use its position
                new_center = drag_handle_pos
                self.center = tuple(new_center)
                
                # Store the actual vectors for drawing
                self._plane_u_vector = u_actual
                self._plane_v_vector = v_actual
        
        self._update_status_text(context)
    
    def _rebuild_preview(self, context):
        """Rebuild the preview with current settings - restores original mesh and recreates preview"""
        obj = self._original_obj if hasattr(self, '_original_obj') and self._original_obj else context.object
        if not obj or obj.type != 'MESH':
            return
        
        if obj.mode != 'EDIT':
            return
        
        if not hasattr(self, '_original_mesh_backup') or not self._original_mesh_backup:
            print("DEBUG: No original mesh backup found")
            return
        
        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        
        # Remove all existing geometry using bmesh ops (more reliable than bm.clear())
        bmesh.ops.delete(bm, geom=list(bm.verts), context='VERTS')
        
        # Restore from backup - this is a dict with verts, edges, faces data
        backup = self._original_mesh_backup
        
        # Recreate vertices
        new_verts = []
        for co, selected in backup['verts']:
            v = bm.verts.new(co)
            v.select = selected
            new_verts.append(v)
        
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        
        # Recreate edges
        for vert_indices, selected, smooth in backup['edges']:
            try:
                e = bm.edges.new([new_verts[i] for i in vert_indices])
                e.select = selected
                e.smooth = smooth
            except (ValueError, IndexError):
                pass  # Edge might already exist or invalid indices
        
        bm.edges.ensure_lookup_table()
        
        # Recreate faces
        for vert_indices, selected, smooth, mat_idx in backup['faces']:
            try:
                f = bm.faces.new([new_verts[i] for i in vert_indices])
                f.select = selected
                f.smooth = smooth
                f.material_index = mat_idx
            except (ValueError, IndexError):
                pass  # Face might already exist or invalid indices
        
        bm.faces.ensure_lookup_table()
        bm.normal_update()
        
        bmesh.update_edit_mesh(mesh)
        bm = bmesh.from_edit_mesh(mesh)
        
        self._preview_vert_indices = []
        self._original_selection = []
        
        # Now create the preview with current settings
        self._create_preview_geometry(context, obj, bm)
        bmesh.update_edit_mesh(mesh)
    
    def _create_preview_geometry(self, context, obj, bm):
        """Create the preview geometry in the bmesh (helper for _create_preview and _rebuild_preview)"""
        mirror_normal = self.get_mirror_matrix()
        plane_co = Vector(self.center)
        
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        
        # Get selected geometry
        selected_verts = [v for v in bm.verts if v.select]
        selected_edges = [e for e in bm.edges if e.select]
        selected_faces = [f for f in bm.faces if f.select]
        
        if not selected_verts:
            return
        
        # Create/get custom layers to mark original selection
        if "original_mirror_verts" not in bm.verts.layers.int:
            bm.verts.layers.int.new("original_mirror_verts")
        if "original_mirror_faces" not in bm.faces.layers.int:
            bm.faces.layers.int.new("original_mirror_faces")
        
        vert_layer = bm.verts.layers.int.get("original_mirror_verts")
        face_layer = bm.faces.layers.int.get("original_mirror_faces")
        
        # Mark the selected vertices and faces
        for v in bm.verts:
            v[vert_layer] = 1 if v.select else 0
        for f in bm.faces:
            f[face_layer] = 1 if f.select else 0
        
        # If slice is enabled, bisect the geometry first
        if self.slice_along_mirror:
            # Re-fetch selected geometry fresh (in case layers invalidated refs)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            selected_verts = [v for v in bm.verts if v.select]
            selected_edges = [e for e in bm.edges if e.select]
            selected_faces = [f for f in bm.faces if f.select]
            
            if not selected_verts:
                return
            
            geom = list(selected_verts) + list(selected_edges) + list(selected_faces)
            
            # Convert plane to local coordinates
            mx_inv = obj.matrix_world.inverted()
            plane_co_local = mx_inv @ plane_co
            plane_no_local = (mx_inv.to_3x3() @ mirror_normal).normalized()
            
            # Check if the plane actually intersects the geometry
            # by seeing if there are vertices on both sides of the plane
            has_positive = False
            has_negative = False
            for v in selected_verts:
                to_vert = v.co - plane_co_local
                d = to_vert.dot(plane_no_local)
                if d > 0.0001:
                    has_positive = True
                elif d < -0.0001:
                    has_negative = True
                if has_positive and has_negative:
                    break
            
            plane_intersects = has_positive and has_negative
            
            # Determine which side of the plane the geometry center is on
            # We want to KEEP geometry on the source side and REMOVE geometry on the mirror target side
            geom_center = Vector((0, 0, 0))
            for v in selected_verts:
                geom_center += v.co
            geom_center /= len(selected_verts)
            
            # Vector from plane to geometry center
            to_geom = geom_center - plane_co_local
            # Dot product tells us which side the geometry is on
            dot = to_geom.dot(plane_no_local)
            
            # Only clear geometry if the plane actually intersects the mesh
            if plane_intersects:
                # If dot > 0, geometry is on the positive (outer) side of the normal
                # If dot < 0, geometry is on the negative (inner) side of the normal
                # We want to clear the side OPPOSITE to where the geometry center is
                if dot > 0:
                    # Geometry center is on outer side, clear inner (where mirror will be)
                    clear_inner = True
                    clear_outer = False
                else:
                    # Geometry center is on inner side, clear outer (where mirror will be)
                    clear_inner = False
                    clear_outer = True
                
                # If flip is enabled, swap which side gets cleared
                if self.flip_side:
                    clear_inner, clear_outer = clear_outer, clear_inner
            else:
                # Plane doesn't intersect - don't clear anything
                clear_inner = False
                clear_outer = False
            
            # Bisect the geometry and clear the appropriate side
            result = bmesh.ops.bisect_plane(
                bm, geom=geom,
                plane_co=plane_co_local,
                plane_no=plane_no_local,
                clear_inner=clear_inner,
                clear_outer=clear_outer
            )
            
            # Select the new geometry created by bisect (the cut edges/verts)
            for elem in result.get('geom_cut', []):
                if hasattr(elem, 'select'):
                    elem.select = True
            
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            
            # After bisect, faces may have been split into new faces that aren't selected
            # Re-select all faces where ALL vertices are selected
            for f in bm.faces:
                if all(v.select for v in f.verts):
                    f.select = True
            
            # Also select edges where both verts are selected
            for e in bm.edges:
                if all(v.select for v in e.verts):
                    e.select = True
        
        # Get geometry to duplicate (may include new bisect verts)
        geom_to_dup = [v for v in bm.verts if v.select] + \
                      [e for e in bm.edges if e.select] + \
                      [f for f in bm.faces if f.select]
        
        # Store original selection indices (after potential bisect)
        self._original_selection = [v.index for v in bm.verts if v.select]
        
        # Duplicate geometry
        ret = bmesh.ops.duplicate(bm, geom=geom_to_dup)
        
        # Store preview vertex indices
        self._preview_vert_indices = [elem.index for elem in ret['geom'] if isinstance(elem, bmesh.types.BMVert)]
        
        # Unmark the duplicated geometry
        duplicated_verts = [elem for elem in ret['geom'] if isinstance(elem, bmesh.types.BMVert)]
        duplicated_faces = [elem for elem in ret['geom'] if isinstance(elem, bmesh.types.BMFace)]
        for v in duplicated_verts:
            v[vert_layer] = 0
        for f in duplicated_faces:
            f[face_layer] = 0
        
        # Deselect original, select preview
        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False
        
        for elem in ret['geom']:
            if hasattr(elem, 'select'):
                elem.select = True
        
        # Mirror the preview geometry
        for vert in duplicated_verts:
            world_co = obj.matrix_world @ vert.co
            mirrored = self.mirror_vertex(world_co, mirror_normal)
            vert.co = obj.matrix_world.inverted() @ mirrored
        
        # Flip normals of mirrored faces (mirroring reverses winding order)
        for face in duplicated_faces:
            face.normal_flip()
    
    def _create_preview(self, context):
        """Create a temporary preview object showing the mirrored result"""
        obj = context.object
        if not obj or obj.type != 'MESH':
            return
        
        # Store reference to original object
        self._original_obj = obj
        
        if obj.mode == 'EDIT':
            # In Edit Mode: Create mirrored geometry directly in the same mesh
            mesh = obj.data
            bm = bmesh.from_edit_mesh(mesh)
            
            # Ensure lookup table is current
            bm.verts.ensure_lookup_table()
            
            # Check if we have selected geometry
            selected_verts = [v for v in bm.verts if v.select]
            if not selected_verts:
                return
            
            # Store full mesh backup for rebuilding (bisect changes topology, not just positions)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            
            self._original_mesh_backup = {
                'verts': [(v.co.copy(), v.select) for v in bm.verts],
                'edges': [(tuple(v.index for v in e.verts), e.select, e.smooth) for e in bm.edges],
                'faces': [(tuple(v.index for v in f.verts), f.select, f.smooth, f.material_index) for f in bm.faces]
            }
            
            # Create the preview geometry using the helper
            self._create_preview_geometry(context, obj, bm)
            
            bmesh.update_edit_mesh(mesh)
            
            # Apply initial appearance based on replace_source setting
            self._update_preview_appearance()
            
        else:
            # In Object Mode: Create separate preview object (original behavior)
            self._original_verts = []
            
            for vert in obj.data.vertices:
                world_co = obj.matrix_world @ vert.co
                self._original_verts.append(world_co.copy())
            
            preview_mesh = obj.data.copy()
        
            # Create new object with the duplicated mesh
            self._preview_obj = bpy.data.objects.new(".Mirror_Preview", preview_mesh)
            context.collection.objects.link(self._preview_obj)
            
            # Set identity matrix for preview object (vertices will be in world space)
            self._preview_obj.matrix_world = Matrix.Identity(4)
            
            # Hide from outliner and make it clearly temporary
            self._preview_obj.hide_select = True
            self._preview_obj.hide_render = True
            
            # Exclude from view layer (won't appear in outliner)
            context.view_layer.objects.active = obj  # Keep original as active
            layer_collection = context.view_layer.layer_collection
            if self._preview_obj.name in context.view_layer.objects:
                # Hide it from the view layer to prevent it from appearing in outliner
                self._preview_obj.hide_set(False)  # But keep it visible in viewport
            
            # Copy materials and settings
            self._preview_obj.data.materials.clear()
            for mat in obj.data.materials:
                self._preview_obj.data.materials.append(mat)
            
            # Flip normals once (since mirroring inverts them)
            bm = bmesh.new()
            bm.from_mesh(preview_mesh)
            for face in bm.faces:
                face.normal_flip()
            bm.to_mesh(preview_mesh)
            bm.free()
            
            # If replace_source is OFF, make preview semi-transparent to show it's a duplicate
            # If replace_source is ON, make preview more opaque since it will replace the original
            if self.replace_source:
                self._preview_obj.show_transparent = True
                self._preview_obj.color = (1.0, 1.0, 1.0, 0.9)  # More opaque
            else:
                self._preview_obj.show_transparent = True
                self._preview_obj.color = (0.5, 0.7, 1.0, 0.5)  # Semi-transparent blue tint
            
            # Apply initial mirror transform
            self._update_preview_transform(context)
    
    def _update_preview_transform(self, context):
        """Update the preview object's transform to show mirrored position"""
        obj = self._original_obj if hasattr(self, '_original_obj') and self._original_obj else context.object
        
        if obj and obj.mode == 'EDIT':
            # If slice is enabled, we need to rebuild the preview since the cut position changes
            if self.slice_along_mirror:
                self._rebuild_preview(context)
                return
            
            # In Edit Mode: Update the mirrored geometry within the mesh
            if not hasattr(self, '_preview_vert_indices') or not self._preview_vert_indices:
                return
            
            mesh = obj.data
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()
            
            mirror_normal = self.get_mirror_matrix()
            
            # Get original and preview vertices by index
            if not hasattr(self, '_original_selection'):
                return
            
            for i, preview_idx in enumerate(self._preview_vert_indices):
                if i < len(self._original_selection) and preview_idx < len(bm.verts):
                    orig_idx = self._original_selection[i]
                    if orig_idx < len(bm.verts):
                        orig_vert = bm.verts[orig_idx]
                        preview_vert = bm.verts[preview_idx]
                        
                        world_co = obj.matrix_world @ orig_vert.co
                        mirrored = self.mirror_vertex(world_co, mirror_normal)
                        preview_vert.co = obj.matrix_world.inverted() @ mirrored
            
            bmesh.update_edit_mesh(mesh)
        else:
            # In Object Mode: Update separate preview object
            if not self._preview_obj or not self._original_verts:
                return
            
            mirror_normal = self.get_mirror_matrix()
            mesh = self._preview_obj.data
            
            for i, vert in enumerate(mesh.vertices):
                if i < len(self._original_verts):
                    world_co = self._original_verts[i]
                    mirrored = self.mirror_vertex(world_co, mirror_normal)
                    vert.co = mirrored
            
            mesh.update()
    
    def execute(self, context):
        # Restore original object visibility before executing (only if object still exists)
        if hasattr(self, '_original_obj') and self._original_obj:
            try:
                if self._original_obj.name in bpy.data.objects:
                    self._original_obj.hide_set(self._original_hide)
            except ReferenceError:
                pass
        
        # Clear stored mesh backup
        if hasattr(self, '_original_mesh_backup'):
            self._original_mesh_backup = None
        
        # Use the stored original object if available
        obj = self._original_obj if hasattr(self, '_original_obj') and self._original_obj else context.object
        
        if obj.mode == 'EDIT':
            # Edit Mode: The preview is already in the mesh (including slice if enabled)
            # Just need to handle replace_source and cleanup
            if hasattr(self, '_preview_vert_indices') and self._preview_vert_indices:
                if self.replace_source:
                    # Delete the original selection using the custom layers
                    mesh = obj.data
                    bm = bmesh.from_edit_mesh(mesh)
                    
                    # Get the custom layers
                    vert_layer = bm.verts.layers.int.get("original_mirror_verts")
                    face_layer = bm.faces.layers.int.get("original_mirror_faces")
                    
                    if face_layer:
                        # Collect marked faces (original selection only)
                        faces_to_delete = []
                        for f in bm.faces:
                            if f[face_layer] == 1:
                                faces_to_delete.append(f)
                        
                        if faces_to_delete:
                            bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES')
                        
                        # Clean up the layers
                        bm.faces.layers.int.remove(face_layer)
                    
                    if vert_layer:
                        bm.verts.layers.int.remove(vert_layer)
                    
                    bmesh.update_edit_mesh(mesh)
                    self.report({'INFO'}, f"Replaced with mirrored geometry across {self.axis} axis")
                else:
                    # Keep both original and mirrored - just clean up the layers
                    mesh = obj.data
                    bm = bmesh.from_edit_mesh(mesh)
                    
                    vert_layer = bm.verts.layers.int.get("original_mirror_verts")
                    face_layer = bm.faces.layers.int.get("original_mirror_faces")
                    
                    if face_layer:
                        # Unhide faces
                        for f in bm.faces:
                            if f[face_layer] == 1:
                                f.hide_set(False)
                            f[face_layer] = 0
                        bm.faces.layers.int.remove(face_layer)
                    
                    if vert_layer:
                        # Clear vertex marks
                        for v in bm.verts:
                            v[vert_layer] = 0
                        bm.verts.layers.int.remove(vert_layer)
                    
                    bmesh.update_edit_mesh(mesh)
                    self.report({'INFO'}, f"Mirrored geometry across {self.axis} axis")
                
                # Weld seam if enabled
                if self.weld_seam:
                    mesh = obj.data
                    bm = bmesh.from_edit_mesh(mesh)
                    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=0.0001)
                    bmesh.update_edit_mesh(mesh)
            else:
                # Fallback to normal mirror operation
                self.mirror_edit_mode(context, obj)
        else:
            # Object Mode: Mirror entire object(s)
            # Pass the original object directly instead of using selected_objects
            self.mirror_object_mode(context, [obj])
        
        return {'FINISHED'}
    
    def get_mirror_matrix(self):
        """Calculate the mirror transformation matrix based on axis, center, and angle"""
        # Use stored actual vectors if available (from interactive rotation)
        if hasattr(self, '_plane_u_vector') and hasattr(self, '_plane_v_vector'):
            u = self._plane_u_vector
            v = self._plane_v_vector
            
            # Mirror normal is perpendicular to the plane (u × v gives the normal)
            # But we need to be careful about the order - it affects direction
            # The plane is defined by u (horizontal) and v (vertical)
            # For a plane, the normal should be perpendicular to both
            
            # Get the axis normal for reference
            if self.axis == 'X':
                base_normal = Vector((1, 0, 0))
            elif self.axis == 'Y':
                base_normal = Vector((0, 1, 0))
            else:  # Z
                base_normal = Vector((0, 0, 1))
            
            # The mirror normal should be close to the base normal
            # Calculate cross product - try both orders
            normal1 = u.cross(v).normalized()
            normal2 = v.cross(u).normalized()
            
            # Pick the one that's more aligned with base normal
            if abs(normal1.dot(base_normal)) > abs(normal2.dot(base_normal)):
                mirror_normal = normal1
            else:
                mirror_normal = normal2
            
            # Ensure it points in the positive direction
            if mirror_normal.dot(base_normal) < 0:
                mirror_normal = -mirror_normal
            
            return mirror_normal
        
        # Fallback: calculate from angle
        angle_rad = math.radians(self.angle)
        
        if self.axis == 'X':
            base_normal = Vector((1, 0, 0))
            rotation_axis = Vector((0, 0, 1))
        elif self.axis == 'Y':
            base_normal = Vector((0, 1, 0))
            rotation_axis = Vector((0, 0, 1))
        else:  # Z
            base_normal = Vector((0, 0, 1))
            rotation_axis = Vector((1, 0, 0))
        
        # Apply rotation to tilt the mirror plane
        if abs(angle_rad) > 0.001:
            rot_matrix = Matrix.Rotation(angle_rad, 3, rotation_axis)
            mirror_normal = rot_matrix @ base_normal
        else:
            mirror_normal = base_normal
        
        return mirror_normal
    
    def mirror_vertex(self, vert_co, mirror_normal):
        """Mirror a vertex position across the plane"""
        # Translate to origin
        relative_pos = vert_co - Vector(self.center)
        
        # Mirror across plane
        distance = relative_pos.dot(mirror_normal)
        mirrored = relative_pos - 2 * distance * mirror_normal
        
        # Translate back
        return mirrored + Vector(self.center)
    
    def mirror_edit_mode(self, context, obj):
        """Mirror selected geometry in edit mode"""
        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        
        # Get selected geometry
        selected_verts = [v for v in bm.verts if v.select]
        
        if not selected_verts:
            self.report({'WARNING'}, "No vertices selected")
            return
        
        mirror_normal = self.get_mirror_matrix()
        plane_co = Vector(self.center)
        
        # If Slice Along Mirror is enabled, bisect the geometry first
        if self.slice_along_mirror:
            # Get all geometry for bisecting
            geom = [v for v in bm.verts if v.select] + \
                   [e for e in bm.edges if e.select] + \
                   [f for f in bm.faces if f.select]
            
            # Convert plane to local coordinates
            mx_inv = obj.matrix_world.inverted()
            plane_co_local = mx_inv @ plane_co
            plane_no_local = mx_inv.to_3x3() @ mirror_normal
            
            # Bisect the geometry - keep both sides
            result = bmesh.ops.bisect_plane(
                bm, geom=geom,
                plane_co=plane_co_local,
                plane_no=plane_no_local,
                clear_inner=False,
                clear_outer=False
            )
            
            # Select the new geometry created by bisect (the cut edge vertices)
            for elem in result.get('geom_cut', []):
                if hasattr(elem, 'select'):
                    elem.select = True
            
            # Re-select geometry after bisect (bisect may have added new verts/edges)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
        
        # Get selected geometry (may have changed after bisect)
        selected_verts = [v for v in bm.verts if v.select]
        
        # Duplicate selected geometry
        ret = bmesh.ops.duplicate(bm, geom=[v for v in bm.verts if v.select] + 
                                       [e for e in bm.edges if e.select] + 
                                       [f for f in bm.faces if f.select])
        
        # Mirror the duplicated vertices
        duplicated_verts = [elem for elem in ret['geom'] if isinstance(elem, bmesh.types.BMVert)]
        
        for vert in duplicated_verts:
            world_co = obj.matrix_world @ vert.co
            mirrored = self.mirror_vertex(world_co, mirror_normal)
            vert.co = obj.matrix_world.inverted() @ mirrored
        
        # Flip normals of duplicated faces
        duplicated_faces = [elem for elem in ret['geom'] if isinstance(elem, bmesh.types.BMFace)]
        for face in duplicated_faces:
            face.normal_flip()
        
        # If replace source, delete the original selection
        if self.replace_source:
            # Delete original selected geometry
            geom_to_delete = [v for v in bm.verts if v.select and v not in duplicated_verts] + \
                            [e for e in bm.edges if e.select and e not in [elem for elem in ret['geom'] if isinstance(elem, bmesh.types.BMEdge)]] + \
                            [f for f in bm.faces if f.select and f not in duplicated_faces]
            bmesh.ops.delete(bm, geom=geom_to_delete, context='VERTS')
        
        # Update mesh
        bmesh.update_edit_mesh(mesh)
        
        action = "Replaced with mirrored" if self.replace_source else "Mirrored"
        self.report({'INFO'}, f"{action} {len(selected_verts)} vertices across {self.axis} axis")
    
    def mirror_object_mode(self, context, objects=None):
        """Mirror entire objects in object mode"""
        if objects is None:
            selected_objects = [o for o in context.selected_objects if o.type == 'MESH']
        else:
            selected_objects = [o for o in objects if o.type == 'MESH']
        
        if not selected_objects:
            self.report({'WARNING'}, "No mesh objects selected")
            return
        
        mirror_normal = self.get_mirror_matrix()
        plane_co = Vector(self.center)
        mirrored_objects = []
        
        for obj in selected_objects:
            # Always create a duplicate
            new_obj = obj.copy()
            new_obj.data = obj.data.copy()
            context.collection.objects.link(new_obj)
            
            # Create BMesh for the new object
            bm = bmesh.new()
            bm.from_mesh(new_obj.data)
            
            # If Slice Along Mirror is enabled, bisect the geometry first
            if self.slice_along_mirror:
                # Get all geometry for bisecting
                geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
                
                # Convert plane to local coordinates
                mx_inv = obj.matrix_world.inverted()
                plane_co_local = mx_inv @ plane_co
                plane_no_local = mx_inv.to_3x3() @ mirror_normal
                
                # Bisect the geometry - keep only the side that will be mirrored
                # For mirror, we want to keep the side opposite to the normal direction
                bmesh.ops.bisect_plane(
                    bm, geom=geom,
                    plane_co=plane_co_local,
                    plane_no=plane_no_local,
                    clear_inner=False,
                    clear_outer=False
                )
            
            # Mirror all vertices
            for vert in bm.verts:
                world_co = obj.matrix_world @ vert.co
                mirrored_world = self.mirror_vertex(world_co, mirror_normal)
                vert.co = obj.matrix_world.inverted() @ mirrored_world
            
            # Flip normals
            for face in bm.faces:
                face.normal_flip()
            
            # Write back to mesh
            bm.to_mesh(new_obj.data)
            bm.free()
            
            new_obj.data.update()
            mirrored_objects.append(new_obj)
            
            # If replace source, delete the original
            if self.replace_source:
                bpy.data.objects.remove(obj, do_unlink=True)
        
        # Select the mirrored objects
        bpy.ops.object.select_all(action='DESELECT')
        for obj in mirrored_objects:
            obj.select_set(True)
        if mirrored_objects:
            context.view_layer.objects.active = mirrored_objects[0]
        
        action = "Replaced with mirrored" if self.replace_source else "Mirrored"
        self.report({'INFO'}, f"{action} {len(selected_objects)} object(s) across {self.axis} axis")


# Draw function for the visual gizmo
def draw_mirror_gizmo(self, context):
    """Draw the mirror plane and square handle in the viewport"""
    
    # Safety check: ensure operator is still valid
    try:
        # Try to access a property to check if operator is still valid
        axis = self.axis
        center = Vector(self.center)
        width = self._handle_distance  # Plane width (u direction)
        height = self._plane_height    # Plane height (v direction, fixed)
        hovered_handle = self._hovered_handle
        angle = self.angle
        # Try to get stored actual vectors
        plane_u = getattr(self, '_plane_u_vector', None)
        plane_v = getattr(self, '_plane_v_vector', None)
    except (ReferenceError, AttributeError):
        # Operator has been removed, stop drawing
        return
    
    # Use stored actual plane vectors if available, otherwise calculate from angle
    if plane_u is not None and plane_v is not None:
        u = plane_u
        v = plane_v
    else:
        # Fallback: calculate from angle
        if axis == 'X':
            u = Vector((0, 1, 0))
            v = Vector((0, 0, 1))
            rotation_axis = Vector((0, 0, 1))
        elif axis == 'Y':
            u = Vector((1, 0, 0))
            v = Vector((0, 0, 1))
            rotation_axis = Vector((0, 0, 1))
        else:  # Z
            u = Vector((1, 0, 0))
            v = Vector((0, 1, 0))
            rotation_axis = Vector((1, 0, 0))
        
        # Apply rotation based on angle
        angle_rad = math.radians(angle)
        if abs(angle_rad) > 0.001:
            rot_matrix = Matrix.Rotation(angle_rad, 3, rotation_axis)
            u = rot_matrix @ u
            v = rot_matrix @ v
    
    # Determine axis color
    if axis == 'X':
        axis_color = (0.8, 0.2, 0.2)  # Red for X axis
    elif axis == 'Y':
        axis_color = (0.4, 0.8, 0.2)  # Green for Y axis
    else:  # Z
        axis_color = (0.2, 0.4, 0.9)  # Blue for Z axis
    
    # Calculate plane corners
    # Use stored handle positions if available to ensure plane spans exactly from drag to rotate handle
    drag_pos = getattr(self, '_drag_handle_pos_3d', None)
    rotate_pos = getattr(self, '_rotate_handle_pos_3d', None)
    
    if drag_pos is not None and rotate_pos is not None:
        # Plane is centered at drag handle, extends in +/- u and v directions
        # Calculate actual width from handle positions (rotate handle is at edge)
        handle_vec = rotate_pos - drag_pos
        actual_width = handle_vec.length  # Rotate handle is at the edge, not beyond
        # Order: top-right, top-left, bottom-left, bottom-right
        corners = [
            drag_pos + u * actual_width + v * height,    # 0: top-right (at rotate handle edge)
            drag_pos - u * actual_width + v * height,    # 1: top-left
            drag_pos - u * actual_width - v * height,    # 2: bottom-left
            drag_pos + u * actual_width - v * height,    # 3: bottom-right
        ]
    else:
        # Fallback: use center-based calculation
        corners = [
            center + u * width + v * height,    # 0: top-right (dashed, dashed) - most transparent
            center - u * width + v * height,    # 1: top-left (solid, dashed) - fade from left
            center - u * width - v * height,    # 2: bottom-left (solid, solid) - full opacity
            center + u * width - v * height,    # 3: bottom-right (dashed, solid) - fade from bottom
        ]
    
    # Draw semi-transparent plane with uniform color
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')  # Draw behind geometry
    gpu.state.depth_mask_set(False)  # Don't write to depth buffer
    
    # Use UNIFORM_COLOR shader for solid color
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    
    # Create plane as two triangles
    indices = [(0, 1, 2), (0, 2, 3)]
    
    batch = batch_for_shader(shader, 'TRIS', {
        "pos": [corners[i] for tri in indices for i in tri]
    })
    
    shader.bind()
    shader.uniform_float("color", (*axis_color, 0.15))  # Uniform semi-transparent color
    batch.draw(shader)
    
    # Draw plane outline with brighter axis color - all edges dashed
    # Ensure depth state is maintained
    outline_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.depth_test_set('LESS_EQUAL')  # Draw behind geometry
    gpu.state.depth_mask_set(False)
    gpu.state.line_width_set(2.0)
    
    # All four edges as dashed lines
    all_edges = [
        [corners[0], corners[1]],  # Top edge
        [corners[1], corners[2]],  # Left edge
        [corners[2], corners[3]],  # Bottom edge
        [corners[3], corners[0]],  # Right edge
    ]
    
    # Create dashed lines by drawing multiple segments
    dash_length = 0.15
    gap_length = 0.1
    
    for edge in all_edges:
        start = edge[0]
        end = edge[1]
        edge_vec = end - start
        edge_length = edge_vec.length
        edge_dir = edge_vec.normalized()
        
        dash_verts = []
        current_pos = 0.0
        
        while current_pos < edge_length:
            dash_start = start + edge_dir * current_pos
            dash_end_pos = min(current_pos + dash_length, edge_length)
            dash_end = start + edge_dir * dash_end_pos
            
            dash_verts.extend([dash_start, dash_end])
            current_pos += dash_length + gap_length
        
        if dash_verts:
            dashed_batch = batch_for_shader(outline_shader, 'LINES', {"pos": dash_verts})
            outline_shader.bind()
            outline_shader.uniform_float("color", (*axis_color, 0.9))
            dashed_batch.draw(outline_shader)
    
    # Prepare shader for handles
    handle_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    
    # Draw large drag handle using stored 3D position
    # Shows as crosshair when snapped to intersection, sphere otherwise
    # Calculate view-independent handle size
    base_size = 0.05
    drag_handle_size = base_size  # Default fallback
    rv3d = getattr(self, '_rv3d', None)
    if rv3d:
        view_distance = rv3d.view_distance
        drag_handle_size = base_size * (view_distance / 10.0)
        drag_handle_size = max(0.005, min(drag_handle_size, 5.0))
    
    # Use stored position if available, otherwise calculate it
    if hasattr(self, '_drag_handle_pos_3d') and self._drag_handle_pos_3d:
        drag_handle_center = self._drag_handle_pos_3d
    else:
        drag_handle_center = center  # Drag handle is at center of plane
    
    # Check if snapped to intersection
    snapped_to_intersection = getattr(self, '_snapped_to_intersection', False)
    
    # Get rotate handle position for drawing connection line
    if hasattr(self, '_rotate_handle_pos_3d') and self._rotate_handle_pos_3d:
        rotate_handle_center = self._rotate_handle_pos_3d
    else:
        rotate_handle_center = center + u * width  # At the edge of the plane
    
    # Draw magenta line connecting drag handle to rotate handle
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.state.line_width_set(2.0)
    
    connection_line_verts = [drag_handle_center, rotate_handle_center]
    connection_line_batch = batch_for_shader(handle_shader, 'LINES', {"pos": connection_line_verts})
    handle_shader.bind()
    # Cyan with 60% opacity (less than the handles which are at 100%)
    handle_shader.uniform_float("color", (0.0, 0.8, 0.8, 0.6))
    connection_line_batch.draw(handle_shader)
    
    gpu.state.line_width_set(1.0)
    
    # Disable depth test to ensure proper drawing order
    gpu.state.depth_test_set('NONE')
    
    # Use yellow for hovered handle, cyan otherwise
    drag_color = (1.0, 0.9, 0.0, 1.0) if hovered_handle == 'DRAG' else (0.0, 0.8, 0.8, 1.0)
    
    if snapped_to_intersection:
        # Draw crosshair when snapped to intersection
        crosshair_size = drag_handle_size * 1.5  # Slightly larger than sphere
        
        # Create 3D crosshair with three perpendicular lines
        drag_crosshair_verts = []
        # X axis
        drag_crosshair_verts.append(drag_handle_center - Vector((crosshair_size, 0, 0)))
        drag_crosshair_verts.append(drag_handle_center + Vector((crosshair_size, 0, 0)))
        # Y axis
        drag_crosshair_verts.append(drag_handle_center - Vector((0, crosshair_size, 0)))
        drag_crosshair_verts.append(drag_handle_center + Vector((0, crosshair_size, 0)))
        # Z axis
        drag_crosshair_verts.append(drag_handle_center - Vector((0, 0, crosshair_size)))
        drag_crosshair_verts.append(drag_handle_center + Vector((0, 0, crosshair_size)))
        
        # Draw black outline for crosshair
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(4.0)  # Thick outline
        
        drag_crosshair_batch = batch_for_shader(handle_shader, 'LINES', {"pos": drag_crosshair_verts})
        handle_shader.bind()
        handle_shader.uniform_float("color", (0.0, 0.0, 0.0, 1.0))  # Black outline
        drag_crosshair_batch.draw(handle_shader)
        
        # Draw colored crosshair on top
        gpu.state.line_width_set(2.5)
        handle_shader.uniform_float("color", drag_color)
        drag_crosshair_batch.draw(handle_shader)
        
        gpu.state.line_width_set(1.0)
    else:
        # Draw sphere when not snapped
        # Create sphere vertices (higher resolution for smoother appearance)
        sphere_verts = []
        segments = 32
        rings = 16
        for i in range(rings + 1):
            theta = i * math.pi / rings
            for j in range(segments):
                phi = j * 2 * math.pi / segments
                x = drag_handle_size * math.sin(theta) * math.cos(phi)
                y = drag_handle_size * math.sin(theta) * math.sin(phi)
                z = drag_handle_size * math.cos(theta)
                sphere_verts.append(drag_handle_center + Vector((x, y, z)))
        
        # Create sphere indices
        sphere_indices = []
        for i in range(rings):
            for j in range(segments):
                next_j = (j + 1) % segments
                # First triangle
                sphere_indices.append((i * segments + j, i * segments + next_j, (i + 1) * segments + j))
                # Second triangle
                sphere_indices.append((i * segments + next_j, (i + 1) * segments + next_j, (i + 1) * segments + j))
        
        # Draw black outline sphere (slightly larger)
        outline_size = drag_handle_size * 1.16
        outline_verts = []
        for i in range(rings + 1):
            theta = i * math.pi / rings
            for j in range(segments):
                phi = j * 2 * math.pi / segments
                x = outline_size * math.sin(theta) * math.cos(phi)
                y = outline_size * math.sin(theta) * math.sin(phi)
                z = outline_size * math.cos(theta)
                outline_verts.append(drag_handle_center + Vector((x, y, z)))
        
        outline_batch = batch_for_shader(handle_shader, 'TRIS', {
            "pos": [outline_verts[i] for tri in sphere_indices for i in tri]
        })
        handle_shader.bind()
        handle_shader.uniform_float("color", (0.0, 0.0, 0.0, 1.0))  # Black outline
        outline_batch.draw(handle_shader)
        
        # Draw colored drag handle sphere
        drag_batch = batch_for_shader(handle_shader, 'TRIS', {
            "pos": [sphere_verts[i] for tri in sphere_indices for i in tri]
        })
        
        handle_shader.uniform_float("color", drag_color)
        drag_batch.draw(handle_shader)
    
    # Draw 3D crosshair rotation handle using stored 3D position
    crosshair_size = drag_handle_size  # Use same view-independent size as drag handle
    # Position already calculated earlier for connection line
    
    # Create 3D crosshair with three perpendicular lines (X, Y, Z axes)
    crosshair_verts = []
    # X axis (red in standard 3D view)
    crosshair_verts.append(rotate_handle_center - Vector((crosshair_size, 0, 0)))
    crosshair_verts.append(rotate_handle_center + Vector((crosshair_size, 0, 0)))
    # Y axis (green in standard 3D view)
    crosshair_verts.append(rotate_handle_center - Vector((0, crosshair_size, 0)))
    crosshair_verts.append(rotate_handle_center + Vector((0, crosshair_size, 0)))
    # Z axis (blue in standard 3D view)
    crosshair_verts.append(rotate_handle_center - Vector((0, 0, crosshair_size)))
    crosshair_verts.append(rotate_handle_center + Vector((0, 0, crosshair_size)))
    
    # Enable line smoothing and set line width
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(3.0)  # Thicker outline
    
    # Draw black outline for crosshair
    crosshair_outline_batch = batch_for_shader(handle_shader, 'LINES', {"pos": crosshair_verts})
    handle_shader.bind()
    handle_shader.uniform_float("color", (0.0, 0.0, 0.0, 1.0))  # Black outline
    crosshair_outline_batch.draw(handle_shader)
    
    # Draw colored crosshair (slightly thinner on top)
    gpu.state.line_width_set(2.0)
    
    # Use yellow for hovered handle, cyan otherwise
    rotate_color = (1.0, 0.9, 0.0, 1.0) if hovered_handle == 'ROTATE' else (0.0, 0.8, 0.8, 1.0)
    handle_shader.uniform_float("color", rotate_color)
    crosshair_outline_batch.draw(handle_shader)
    
    # Reset GPU state
    gpu.state.blend_set('NONE')
    gpu.state.line_width_set(1.0)


# Menu integration
def menu_func_edit(self, context):
    self.layout.operator(MESH_OT_interactive_mirror.bl_idname)

def menu_func_object(self, context):
    self.layout.operator(MESH_OT_interactive_mirror.bl_idname)


def register():
    bpy.utils.register_class(MESH_OT_interactive_mirror)
    bpy.types.VIEW3D_MT_edit_mesh.append(menu_func_edit)
    bpy.types.VIEW3D_MT_object.append(menu_func_object)


def unregister():
    bpy.types.VIEW3D_MT_edit_mesh.remove(menu_func_edit)
    bpy.types.VIEW3D_MT_object.remove(menu_func_object)
    bpy.utils.unregister_class(MESH_OT_interactive_mirror)


if __name__ == "__main__":
    register()
