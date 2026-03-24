bl_info = {
    "name": "Gesture Mirror",
    "author": "User",
    "version": (1, 1),
    "blender": (5, 0, 0),
    "location": "View3D > Object/Mesh > Gesture Mirror",
    "description": "Add mirror modifier (Object mode) or mirror geometry (Edit mode) based on mouse gesture direction",
    "category": "3D View",
}

import bpy
import mathutils
import math
import gpu
import bmesh
import blf
from gpu_extras.batch import batch_for_shader

EMPTY_NAME = "world_orig_mirror"

# Remember last pivot mode used in Object Mode across invocations
_last_object_pivot_mode = 'WORLD'

# Font settings for viewport text
FONT_ID = 0
FONT_SIZE_LARGE = 24
FONT_SIZE_SMALL = 14

# Axis colors matching Blender's convention (RGBA)
AXIS_COLORS = {
    'X': (1.0, 0.2, 0.3, 1.0),   # Red
    'Y': (0.5, 1.0, 0.2, 1.0),   # Green  
    'Z': (0.3, 0.5, 1.0, 1.0),   # Blue
    'NONE': (0.8, 0.8, 0.8, 0.5) # Gray (below threshold)
}

# Translucent versions for plane preview
AXIS_COLORS_ALPHA = {
    'X': (1.0, 0.2, 0.3, 0.15),
    'Y': (0.5, 1.0, 0.2, 0.15),
    'Z': (0.3, 0.5, 1.0, 0.15),
    'NONE': (0.5, 0.5, 0.5, 0.05)
}


def get_mirror_empty_location():
    """Get the location of the mirror empty, or origin if it doesn't exist."""
    if EMPTY_NAME in bpy.data.objects:
        return bpy.data.objects[EMPTY_NAME].matrix_world.translation.copy()
    return mathutils.Vector((0, 0, 0))


def get_or_create_mirror_empty():
    """Get existing mirror empty or create a new one."""
    if EMPTY_NAME in bpy.data.objects:
        return bpy.data.objects[EMPTY_NAME]
    
    # Create new empty with Plane Axes display type
    empty = bpy.data.objects.new(EMPTY_NAME, None)
    empty.empty_display_type = 'PLAIN_AXES'
    empty.empty_display_size = 1.0
    bpy.context.collection.objects.link(empty)
    return empty


def has_mirror_modifier(obj):
    """Check if object already has a Mirror_Gesture modifier."""
    return any(mod.type == 'MIRROR' and mod.name == 'Mirror_Gesture' for mod in obj.modifiers)


def apply_mirror_modifier(obj, axis, use_flip, mirror_empty):
    """Apply mirror modifier to object with specified settings."""
    if has_mirror_modifier(obj):
        return False
    
    # Add mirror modifier
    mod = obj.modifiers.new(name="Mirror_Gesture", type='MIRROR')
    
    # Move modifier to the top of the stack
    with bpy.context.temp_override(object=obj):
        while obj.modifiers.find(mod.name) > 0:
            bpy.ops.object.modifier_move_up(modifier=mod.name)
    
    # Set mirror object (None means mirror around object's own origin)
    if mirror_empty is not None:
        mod.mirror_object = mirror_empty
    
    # Reset all axes first
    mod.use_axis[0] = False
    mod.use_axis[1] = False
    mod.use_axis[2] = False
    
    # Enable bisect for all axes (will only affect the active one)
    mod.use_bisect_axis[0] = False
    mod.use_bisect_axis[1] = False
    mod.use_bisect_axis[2] = False
    
    # Reset flips
    mod.use_bisect_flip_axis[0] = False
    mod.use_bisect_flip_axis[1] = False
    mod.use_bisect_flip_axis[2] = False
    
    # Set the chosen axis
    axis_index = {'X': 0, 'Y': 1, 'Z': 2}[axis]
    mod.use_axis[axis_index] = True
    mod.use_bisect_axis[axis_index] = True
    mod.use_bisect_flip_axis[axis_index] = use_flip
    
    # Enable clipping to prevent vertices from crossing the mirror plane
    mod.use_clip = True
    
    return True


class OBJECT_OT_gesture_mirror(bpy.types.Operator):
    """Add mirror modifier based on mouse gesture direction"""
    bl_idname = "object.gesture_mirror"
    bl_label = "Gesture Mirror"
    bl_options = {'REGISTER', 'UNDO'}
    
    # Store start position
    start_mouse_x: bpy.props.IntProperty()
    start_mouse_y: bpy.props.IntProperty()
    
    # Current mouse position (for drawing)
    current_mouse_x: bpy.props.IntProperty()
    current_mouse_y: bpy.props.IntProperty()
    
    # Current detected axis and direction
    current_axis: bpy.props.StringProperty(default='NONE')
    current_direction: bpy.props.StringProperty(default='NONE')
    
    # Draw handler references (2D overlay and 3D plane)
    _handle_2d = None
    _handle_3d = None
    
    # Pivot mode toggle (runtime only)
    # Cycles: 'WORLD' = World Origin, 'OBJECT' = Object Pivot, 'CURSOR' = 3D Cursor
    _pivot_mode = 'WORLD'
    
    # Minimum distance to register gesture
    threshold = 50
    
    # Auto-apply radius - crossing this circle auto-commits the action
    auto_apply_radius = 120
    
    @classmethod
    def poll(cls, context):
        return (context.area.type == 'VIEW_3D' and 
                context.selected_objects and
                context.mode == 'OBJECT')
    
    @staticmethod
    def draw_callback_2d(self, context):
        """Draw 2D overlay: gesture line, text feedback."""
        # Get positions
        start = (self.start_mouse_x, self.start_mouse_y)
        end = (self.current_mouse_x, self.current_mouse_y)
        
        # Calculate distance for visual feedback
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = (dx**2 + dy**2)**0.5
        
        # Determine if we're above threshold
        above_threshold = distance >= self.threshold
        
        # Get color based on current axis
        if not above_threshold:
            color = AXIS_COLORS['NONE']
        else:
            color = AXIS_COLORS.get(self.current_axis, AXIS_COLORS['NONE'])
        
        # === Draw the gesture line ===
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(3.0)
        
        batch = batch_for_shader(shader, 'LINES', {"pos": [start, end]})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)
        
        # === Draw start point (circle indicator) ===
        circle_segments = 16
        circle_radius = 8
        circle_verts = []
        for i in range(circle_segments):
            angle1 = (i / circle_segments) * 2 * math.pi
            angle2 = ((i + 1) / circle_segments) * 2 * math.pi
            circle_verts.append((start[0] + math.cos(angle1) * circle_radius,
                                start[1] + math.sin(angle1) * circle_radius))
            circle_verts.append((start[0] + math.cos(angle2) * circle_radius,
                                start[1] + math.sin(angle2) * circle_radius))
        
        batch_circle = batch_for_shader(shader, 'LINES', {"pos": circle_verts})
        shader.uniform_float("color", (1.0, 1.0, 1.0, 0.8))
        batch_circle.draw(shader)
        
        # === Draw threshold circle (shows minimum gesture distance) ===
        if not above_threshold:
            threshold_verts = []
            for i in range(circle_segments * 2):
                angle1 = (i / (circle_segments * 2)) * 2 * math.pi
                angle2 = ((i + 1) / (circle_segments * 2)) * 2 * math.pi
                threshold_verts.append((start[0] + math.cos(angle1) * self.threshold,
                                       start[1] + math.sin(angle1) * self.threshold))
                threshold_verts.append((start[0] + math.cos(angle2) * self.threshold,
                                       start[1] + math.sin(angle2) * self.threshold))
            
            gpu.state.line_width_set(1.0)
            batch_threshold = batch_for_shader(shader, 'LINES', {"pos": threshold_verts})
            shader.uniform_float("color", (0.5, 0.5, 0.5, 0.3))
            batch_threshold.draw(shader)
        
        # === Draw auto-apply radius circle ===
        auto_radius_verts = []
        auto_segments = circle_segments * 3  # More segments for smoother circle
        for i in range(auto_segments):
            angle1 = (i / auto_segments) * 2 * math.pi
            angle2 = ((i + 1) / auto_segments) * 2 * math.pi
            auto_radius_verts.append((start[0] + math.cos(angle1) * self.auto_apply_radius,
                                     start[1] + math.sin(angle1) * self.auto_apply_radius))
            auto_radius_verts.append((start[0] + math.cos(angle2) * self.auto_apply_radius,
                                     start[1] + math.sin(angle2) * self.auto_apply_radius))
        
        gpu.state.line_width_set(2.0)
        batch_auto_radius = batch_for_shader(shader, 'LINES', {"pos": auto_radius_verts})
        # Color based on whether we're above threshold and approaching the auto-apply zone
        if above_threshold:
            # Pulse effect as user gets closer to the auto-apply radius
            proximity = min(distance / self.auto_apply_radius, 1.0)
            auto_color = (color[0], color[1], color[2], 0.3 + 0.5 * proximity)
        else:
            auto_color = (0.4, 0.4, 0.4, 0.2)
        shader.uniform_float("color", auto_color)
        batch_auto_radius.draw(shader)
        
        # === Draw direction arrow at end of gesture line ===
        if above_threshold and distance > 20:
            arrow_size = 12
            # Calculate arrow direction
            angle = math.atan2(dy, dx)
            arrow_angle = math.pi / 6  # 30 degrees
            
            arrow_verts = [
                end,
                (end[0] - arrow_size * math.cos(angle - arrow_angle),
                 end[1] - arrow_size * math.sin(angle - arrow_angle)),
                end,
                (end[0] - arrow_size * math.cos(angle + arrow_angle),
                 end[1] - arrow_size * math.sin(angle + arrow_angle)),
            ]
            
            gpu.state.line_width_set(3.0)
            batch_arrow = batch_for_shader(shader, 'LINES', {"pos": arrow_verts})
            shader.uniform_float("color", color)
            batch_arrow.draw(shader)
        
        # Reset line width for text
        gpu.state.line_width_set(1.0)
        
        # === Draw viewport text overlay ===
        region = context.region
        
        # Large axis text near cursor
        if above_threshold:
            flip_suffix = "+" if self.current_direction == 'POSITIVE' else "-"
            axis_text = f"{self.current_axis}{flip_suffix}"
            text_color = color[:3]  # RGB only
        else:
            axis_text = "?"
            text_color = (0.6, 0.6, 0.6)
        
        # Position text near the end of the gesture line
        text_offset_x = 25
        text_offset_y = 10
        text_x = end[0] + text_offset_x
        text_y = end[1] + text_offset_y
        
        # Draw axis letter (large)
        blf.size(FONT_ID, FONT_SIZE_LARGE)
        blf.color(FONT_ID, text_color[0], text_color[1], text_color[2], 1.0)
        blf.position(FONT_ID, text_x, text_y, 0)
        blf.draw(FONT_ID, axis_text)
        
        # Draw pivot mode indicator below axis text
        pivot_labels = {'WORLD': 'World Origin', 'OBJECT': 'Object Pivot', 'CURSOR': '3D Cursor'}
        pivot_label = pivot_labels.get(self._pivot_mode, 'World Origin')
        blf.size(FONT_ID, FONT_SIZE_SMALL)
        blf.color(FONT_ID, 0.9, 0.9, 0.9, 0.85)
        blf.position(FONT_ID, text_x, text_y - 20, 0)
        blf.draw(FONT_ID, f"[C] {pivot_label}")
        
        # Reset GPU state
        gpu.state.blend_set('NONE')
    
    @staticmethod
    def draw_callback_3d(self, context):
        """Draw 3D mirror plane preview."""
        # Only draw if we have a valid axis selected
        dx = self.current_mouse_x - self.start_mouse_x
        dy = self.current_mouse_y - self.start_mouse_y
        distance = (dx**2 + dy**2)**0.5
        
        if distance < self.threshold or self.current_axis == 'NONE':
            return
        
        # Get mirror location based on pivot mode
        if self._pivot_mode == 'OBJECT' and context.active_object:
            mirror_loc = context.active_object.matrix_world.translation.copy()
        elif self._pivot_mode == 'CURSOR':
            mirror_loc = context.scene.cursor.location.copy()
        else:
            mirror_loc = get_mirror_empty_location()
        
        # Define plane size based on view
        plane_size = 5.0  # Could be dynamic based on selection bounds
        
        # Create plane vertices based on axis
        if self.current_axis == 'X':
            # YZ plane at mirror X location
            verts = [
                (mirror_loc.x, mirror_loc.y - plane_size, mirror_loc.z - plane_size),
                (mirror_loc.x, mirror_loc.y + plane_size, mirror_loc.z - plane_size),
                (mirror_loc.x, mirror_loc.y + plane_size, mirror_loc.z + plane_size),
                (mirror_loc.x, mirror_loc.y - plane_size, mirror_loc.z + plane_size),
            ]
        elif self.current_axis == 'Y':
            # XZ plane at mirror Y location
            verts = [
                (mirror_loc.x - plane_size, mirror_loc.y, mirror_loc.z - plane_size),
                (mirror_loc.x + plane_size, mirror_loc.y, mirror_loc.z - plane_size),
                (mirror_loc.x + plane_size, mirror_loc.y, mirror_loc.z + plane_size),
                (mirror_loc.x - plane_size, mirror_loc.y, mirror_loc.z + plane_size),
            ]
        else:  # Z
            # XY plane at mirror Z location
            verts = [
                (mirror_loc.x - plane_size, mirror_loc.y - plane_size, mirror_loc.z),
                (mirror_loc.x + plane_size, mirror_loc.y - plane_size, mirror_loc.z),
                (mirror_loc.x + plane_size, mirror_loc.y + plane_size, mirror_loc.z),
                (mirror_loc.x - plane_size, mirror_loc.y + plane_size, mirror_loc.z),
            ]
        
        # Triangle indices for the quad (two triangles)
        indices = [(0, 1, 2), (2, 3, 0)]
        
        # Get plane color
        plane_color = AXIS_COLORS_ALPHA.get(self.current_axis, AXIS_COLORS_ALPHA['NONE'])
        edge_color = AXIS_COLORS.get(self.current_axis, AXIS_COLORS['NONE'])
        
        # Draw filled plane
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.face_culling_set('NONE')
        
        batch_plane = batch_for_shader(shader, 'TRIS', 
                                        {"pos": verts}, 
                                        indices=indices)
        shader.bind()
        shader.uniform_float("color", plane_color)
        batch_plane.draw(shader)
        
        # Draw plane edges
        edge_verts = [
            verts[0], verts[1],
            verts[1], verts[2],
            verts[2], verts[3],
            verts[3], verts[0],
        ]
        
        gpu.state.line_width_set(2.0)
        batch_edges = batch_for_shader(shader, 'LINES', {"pos": edge_verts})
        shader.uniform_float("color", edge_color)
        batch_edges.draw(shader)
        
        # Reset GPU state
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')
        gpu.state.line_width_set(1.0)
    
    def invoke(self, context, event):
        self.start_mouse_x = event.mouse_region_x
        self.start_mouse_y = event.mouse_region_y
        self.current_mouse_x = event.mouse_region_x
        self.current_mouse_y = event.mouse_region_y
        self.current_axis = 'NONE'
        self.current_direction = 'NONE'
        self._pivot_mode = _last_object_pivot_mode
        
        # Count valid objects
        valid_objects = [obj for obj in context.selected_objects 
                        if obj.type == 'MESH' and not has_mirror_modifier(obj)]
        
        if not valid_objects:
            self.report({'WARNING'}, "No valid objects to mirror (all have modifiers or aren't meshes)")
            return {'CANCELLED'}
        
        # Add draw handlers (2D for overlay, 3D for plane preview)
        args = (self, context)
        self._handle_2d = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback_2d, args, 'WINDOW', 'POST_PIXEL')
        self._handle_3d = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback_3d, args, 'WINDOW', 'POST_VIEW')
        
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set("Move mouse in direction to mirror, then release. C: cycle pivot (World/Object/Cursor). ESC to cancel.")
        
        return {'RUNNING_MODAL'}
    
    def cleanup(self, context):
        """Remove draw handlers and reset header."""
        if self._handle_2d:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle_2d, 'WINDOW')
            self._handle_2d = None
        if self._handle_3d:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle_3d, 'WINDOW')
            self._handle_3d = None
        context.area.header_text_set(None)
        context.area.tag_redraw()
    
    def modal(self, context, event):
        global _last_object_pivot_mode
        # Always update current mouse position for drawing
        self.current_mouse_x = event.mouse_region_x
        self.current_mouse_y = event.mouse_region_y
        
        # Force redraw to update gesture line
        context.area.tag_redraw()
        
        if event.type == 'C' and event.value == 'PRESS':
            # Cycle through pivot modes: WORLD → OBJECT → CURSOR → WORLD
            cycle = {'WORLD': 'OBJECT', 'OBJECT': 'CURSOR', 'CURSOR': 'WORLD'}
            self._pivot_mode = cycle.get(self._pivot_mode, 'WORLD')
            pivot_labels = {'WORLD': 'World Origin', 'OBJECT': 'Object Pivot', 'CURSOR': '3D Cursor'}
            pivot_mode = pivot_labels[self._pivot_mode]
            context.area.header_text_set(f"Pivot: {pivot_mode} | Drag to select axis. C: cycle pivot. ESC to cancel.")
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        
        elif event.type == 'MOUSEMOVE':
            # Update header with current direction hint
            dx = event.mouse_region_x - self.start_mouse_x
            dy = event.mouse_region_y - self.start_mouse_y
            
            axis, direction = self.get_axis_from_movement(context, dx, dy)
            self.current_axis = axis
            self.current_direction = direction
            
            flip_suffix = "+" if direction == 'POSITIVE' else "-"
            distance = (dx**2 + dy**2)**0.5
            pivot_labels = {'WORLD': 'World Origin', 'OBJECT': 'Object Pivot', 'CURSOR': '3D Cursor'}
            pivot_mode = pivot_labels.get(self._pivot_mode, 'World Origin')
            
            # Check if user crossed the auto-apply radius
            if distance >= self.auto_apply_radius:
                use_flip = (direction == 'POSITIVE')
                self.cleanup(context)
                _last_object_pivot_mode = self._pivot_mode
                self.execute_mirror(context, axis, use_flip)
                return {'FINISHED'}
            
            if distance > self.threshold:
                context.area.header_text_set(f"Axis: {axis}{flip_suffix} | Pivot: {pivot_mode} | Release to confirm. C: cycle pivot.")
            else:
                context.area.header_text_set(f"Drag further to select axis... | Pivot: {pivot_mode} | C: cycle pivot. ESC to cancel.")
        
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            dx = event.mouse_region_x - self.start_mouse_x
            dy = event.mouse_region_y - self.start_mouse_y
            distance = (dx**2 + dy**2)**0.5
            
            # Check if movement was significant enough
            if distance < self.threshold:
                self.cleanup(context)
                self.report({'WARNING'}, "Gesture too small, cancelled")
                return {'CANCELLED'}
            
            axis, direction = self.get_axis_from_movement(context, dx, dy)
            use_flip = (direction == 'POSITIVE')
            
            self.cleanup(context)
            _last_object_pivot_mode = self._pivot_mode
            self.execute_mirror(context, axis, use_flip)
            return {'FINISHED'}
        
        elif event.type in {'RIGHTMOUSE', 'ESC'}:
            self.cleanup(context)
            self.report({'INFO'}, "Cancelled")
            return {'CANCELLED'}
        
        return {'RUNNING_MODAL'}
    
    def get_axis_from_movement(self, context, dx, dy):
        """
        Determine axis and direction from mouse movement relative to view.
        Returns (axis, direction) where direction is 'POSITIVE' or 'NEGATIVE'
        """
        region = context.region
        rv3d = context.region_data
        
        # Get view vectors
        view_matrix = rv3d.view_matrix.inverted()
        
        # View right vector (screen X)
        view_right = view_matrix.col[0].xyz.normalized()
        # View up vector (screen Y)
        view_up = view_matrix.col[1].xyz.normalized()
        
        # Convert 2D mouse movement to 3D direction
        move_3d = view_right * dx + view_up * dy
        
        # Find dominant world axis
        abs_x = abs(move_3d.x)
        abs_y = abs(move_3d.y)
        abs_z = abs(move_3d.z)
        
        max_val = max(abs_x, abs_y, abs_z)
        
        if max_val == abs_x:
            axis = 'X'
            direction = 'POSITIVE' if move_3d.x > 0 else 'NEGATIVE'
        elif max_val == abs_y:
            axis = 'Y'
            direction = 'POSITIVE' if move_3d.y > 0 else 'NEGATIVE'
        else:
            axis = 'Z'
            direction = 'POSITIVE' if move_3d.z > 0 else 'NEGATIVE'
        
        return axis, direction
    
    def execute_mirror(self, context, axis, use_flip):
        """Apply mirror modifier to all valid selected objects."""
        if self._pivot_mode == 'OBJECT':
            mirror_empty = None  # Mirror around each object's own origin
        elif self._pivot_mode == 'CURSOR':
            # Place the mirror empty at the 3D cursor location
            mirror_empty = get_or_create_mirror_empty()
            mirror_empty.location = context.scene.cursor.location.copy()
        else:
            mirror_empty = get_or_create_mirror_empty()
        
        applied_count = 0
        skipped_count = 0
        
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
                
            if has_mirror_modifier(obj):
                skipped_count += 1
                continue
            
            if apply_mirror_modifier(obj, axis, use_flip, mirror_empty):
                applied_count += 1
        
        pivot_text = {'WORLD': 'world origin', 'OBJECT': 'object pivot', 'CURSOR': '3D cursor'}.get(self._pivot_mode, 'world origin')
        flip_text = " with flip" if use_flip else ""
        self.report({'INFO'}, 
                   f"Applied {axis}-axis mirror{flip_text} at {pivot_text} to {applied_count} object(s). "
                   f"Skipped {skipped_count} (already mirrored).")


class MESH_OT_gesture_mirror_geometry(bpy.types.Operator):
    """Mirror selected geometry based on mouse gesture direction"""
    bl_idname = "mesh.gesture_mirror_geometry"
    bl_label = "Gesture Mirror"
    bl_options = {'REGISTER', 'UNDO'}
    
    # Store start position
    start_mouse_x: bpy.props.IntProperty()
    start_mouse_y: bpy.props.IntProperty()
    
    # Current mouse position (for drawing)
    current_mouse_x: bpy.props.IntProperty()
    current_mouse_y: bpy.props.IntProperty()
    
    # Current detected axis and direction
    current_axis: bpy.props.StringProperty(default='NONE')
    current_direction: bpy.props.StringProperty(default='NONE')
    
    # Draw handler references (2D overlay and 3D plane)
    _handle_2d = None
    _handle_3d = None
    
    # Normal-based mirroring toggle (runtime only)
    _use_normal_axis = False
    # Object Pivot mode: mirror around object origin with bisect
    _use_object_pivot = False
    _active_normal = None  # Stored normal from active element (local Z in normal mode)
    _active_tangent = None  # Tangent vector (local X in normal mode)
    _active_bitangent = None  # Bitangent vector (local Y in normal mode)
    _active_center = None  # Stored center from active element
    _active_is_edge = False  # True if active element is an edge
    _edge_verts = None  # Store edge vertex positions for snapping
    
    # Minimum distance to register gesture
    threshold = 50
    
    # Auto-apply radius
    auto_apply_radius = 120
    
    @classmethod
    def poll(cls, context):
        return (context.area.type == 'VIEW_3D' and 
                context.mode == 'EDIT_MESH' and
                context.edit_object is not None)
    
    @staticmethod
    def draw_callback_2d(self, context):
        """Draw 2D overlay: gesture line, text feedback."""
        start = (self.start_mouse_x, self.start_mouse_y)
        end = (self.current_mouse_x, self.current_mouse_y)
        
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = (dx**2 + dy**2)**0.5
        
        above_threshold = distance >= self.threshold
        
        if not above_threshold:
            color = AXIS_COLORS['NONE']
        else:
            color = AXIS_COLORS.get(self.current_axis, AXIS_COLORS['NONE'])
        
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(3.0)
        
        batch = batch_for_shader(shader, 'LINES', {"pos": [start, end]})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)
        
        # Draw start point circle
        circle_segments = 16
        circle_radius = 8
        circle_verts = []
        for i in range(circle_segments):
            angle1 = (i / circle_segments) * 2 * math.pi
            angle2 = ((i + 1) / circle_segments) * 2 * math.pi
            circle_verts.append((start[0] + math.cos(angle1) * circle_radius,
                                start[1] + math.sin(angle1) * circle_radius))
            circle_verts.append((start[0] + math.cos(angle2) * circle_radius,
                                start[1] + math.sin(angle2) * circle_radius))
        
        batch_circle = batch_for_shader(shader, 'LINES', {"pos": circle_verts})
        shader.uniform_float("color", (1.0, 1.0, 1.0, 0.8))
        batch_circle.draw(shader)
        
        # Draw threshold circle
        if not above_threshold:
            threshold_verts = []
            for i in range(circle_segments * 2):
                angle1 = (i / (circle_segments * 2)) * 2 * math.pi
                angle2 = ((i + 1) / (circle_segments * 2)) * 2 * math.pi
                threshold_verts.append((start[0] + math.cos(angle1) * self.threshold,
                                       start[1] + math.sin(angle1) * self.threshold))
                threshold_verts.append((start[0] + math.cos(angle2) * self.threshold,
                                       start[1] + math.sin(angle2) * self.threshold))
            
            gpu.state.line_width_set(1.0)
            batch_threshold = batch_for_shader(shader, 'LINES', {"pos": threshold_verts})
            shader.uniform_float("color", (0.5, 0.5, 0.5, 0.3))
            batch_threshold.draw(shader)
        
        # Draw auto-apply radius circle
        auto_radius_verts = []
        auto_segments = circle_segments * 3
        for i in range(auto_segments):
            angle1 = (i / auto_segments) * 2 * math.pi
            angle2 = ((i + 1) / auto_segments) * 2 * math.pi
            auto_radius_verts.append((start[0] + math.cos(angle1) * self.auto_apply_radius,
                                     start[1] + math.sin(angle1) * self.auto_apply_radius))
            auto_radius_verts.append((start[0] + math.cos(angle2) * self.auto_apply_radius,
                                     start[1] + math.sin(angle2) * self.auto_apply_radius))
        
        gpu.state.line_width_set(2.0)
        batch_auto_radius = batch_for_shader(shader, 'LINES', {"pos": auto_radius_verts})
        if above_threshold:
            proximity = min(distance / self.auto_apply_radius, 1.0)
            auto_color = (color[0], color[1], color[2], 0.3 + 0.5 * proximity)
        else:
            auto_color = (0.4, 0.4, 0.4, 0.2)
        shader.uniform_float("color", auto_color)
        batch_auto_radius.draw(shader)
        
        # Draw direction arrow
        if above_threshold and distance > 20:
            arrow_size = 12
            angle = math.atan2(dy, dx)
            arrow_angle = math.pi / 6
            
            arrow_verts = [
                end,
                (end[0] - arrow_size * math.cos(angle - arrow_angle),
                 end[1] - arrow_size * math.sin(angle - arrow_angle)),
                end,
                (end[0] - arrow_size * math.cos(angle + arrow_angle),
                 end[1] - arrow_size * math.sin(angle + arrow_angle)),
            ]
            
            gpu.state.line_width_set(3.0)
            batch_arrow = batch_for_shader(shader, 'LINES', {"pos": arrow_verts})
            shader.uniform_float("color", color)
            batch_arrow.draw(shader)
        
        gpu.state.line_width_set(1.0)
        
        # Draw text overlay
        if above_threshold:
            if self._use_normal_axis:
                axis_text = f"{self.current_axis}"  # Show which normal axis (X, Y, Z)
                text_color = (1.0, 0.5, 1.0)  # Magenta for normal mode
            else:
                flip_suffix = "+" if self.current_direction == 'POSITIVE' else "-"
                axis_text = f"{self.current_axis}{flip_suffix}"
                text_color = color[:3]
        else:
            axis_text = "?"
            text_color = (0.6, 0.6, 0.6)
        
        text_offset_x = 25
        text_offset_y = 10
        text_x = end[0] + text_offset_x
        text_y = end[1] + text_offset_y
        
        blf.size(FONT_ID, FONT_SIZE_LARGE)
        blf.color(FONT_ID, text_color[0], text_color[1], text_color[2], 1.0)
        blf.position(FONT_ID, text_x, text_y, 0)
        blf.draw(FONT_ID, axis_text)
        
        # Draw mode indicator below axis
        if self._use_normal_axis:
            blf.size(FONT_ID, FONT_SIZE_SMALL)
            blf.color(FONT_ID, 1.0, 0.5, 1.0, 1.0)  # Magenta
            blf.position(FONT_ID, text_x, text_y - 20, 0)
            blf.draw(FONT_ID, "NORMAL")
        elif self._use_object_pivot:
            blf.size(FONT_ID, FONT_SIZE_SMALL)
            blf.color(FONT_ID, 0.9, 0.9, 0.3, 1.0)  # Yellow
            blf.position(FONT_ID, text_x, text_y - 20, 0)
            blf.draw(FONT_ID, "OBJECT PIVOT (BISECT)")
        
        gpu.state.blend_set('NONE')
    
    @staticmethod
    def draw_callback_3d(self, context):
        """Draw 3D mirror plane preview."""
        dx = self.current_mouse_x - self.start_mouse_x
        dy = self.current_mouse_y - self.start_mouse_y
        distance = (dx**2 + dy**2)**0.5
        
        if distance < self.threshold or self.current_axis == 'NONE':
            return
        
        obj = context.edit_object
        plane_size = 5.0
        
        if self._use_normal_axis and self._active_normal is not None and self._active_center is not None:
            # Normal mode: draw plane at active element with correct orientation
            center_local = self._active_center
            
            # Determine mirror axis vector (same logic as execute_mirror_geometry)
            if self._active_is_edge:
                edge_dir = self._active_tangent
                if self.current_axis == 'X':
                    world_x = mathutils.Vector((1, 0, 0))
                    mirror_axis_vec = (world_x - world_x.project(edge_dir)).normalized()
                    if mirror_axis_vec.length < 0.01:
                        world_y = mathutils.Vector((0, 1, 0))
                        mirror_axis_vec = (world_y - world_y.project(edge_dir)).normalized()
                elif self.current_axis == 'Y':
                    world_y = mathutils.Vector((0, 1, 0))
                    mirror_axis_vec = (world_y - world_y.project(edge_dir)).normalized()
                    if mirror_axis_vec.length < 0.01:
                        world_x = mathutils.Vector((1, 0, 0))
                        mirror_axis_vec = (world_x - world_x.project(edge_dir)).normalized()
                else:  # Z
                    world_z = mathutils.Vector((0, 0, 1))
                    mirror_axis_vec = (world_z - world_z.project(edge_dir)).normalized()
                    if mirror_axis_vec.length < 0.01:
                        world_x = mathutils.Vector((1, 0, 0))
                        mirror_axis_vec = (world_x - world_x.project(edge_dir)).normalized()
            else:
                if self.current_axis == 'X' and self._active_tangent is not None:
                    mirror_axis_vec = self._active_tangent
                elif self.current_axis == 'Y' and self._active_bitangent is not None:
                    mirror_axis_vec = self._active_bitangent
                else:
                    mirror_axis_vec = self._active_normal
            
            # Transform center and mirror axis to world space
            center_world = obj.matrix_world @ center_local
            mat3 = obj.matrix_world.to_3x3()
            mirror_axis_world = (mat3 @ mirror_axis_vec).normalized()
            
            # Build two perpendicular vectors spanning the mirror plane
            if abs(mirror_axis_world.dot(mathutils.Vector((0, 0, 1)))) < 0.99:
                up = mathutils.Vector((0, 0, 1))
            else:
                up = mathutils.Vector((1, 0, 0))
            
            plane_u = mirror_axis_world.cross(up).normalized() * plane_size
            plane_v = mirror_axis_world.cross(plane_u).normalized() * plane_size
            
            verts = [
                tuple(center_world - plane_u - plane_v),
                tuple(center_world + plane_u - plane_v),
                tuple(center_world + plane_u + plane_v),
                tuple(center_world - plane_u + plane_v),
            ]
            
            plane_color = (1.0, 0.3, 1.0, 0.15)   # Magenta tint for normal mode
            edge_color = (1.0, 0.5, 1.0, 1.0)
        else:
            # Standard world-axis plane at pivot location
            if self._use_object_pivot:
                mirror_loc = obj.matrix_world.translation.copy()
            else:
                mirror_loc = self._mirror_preview_world_loc if self._mirror_preview_world_loc else get_mirror_empty_location()
            
            if self.current_axis == 'X':
                verts = [
                    (mirror_loc.x, mirror_loc.y - plane_size, mirror_loc.z - plane_size),
                    (mirror_loc.x, mirror_loc.y + plane_size, mirror_loc.z - plane_size),
                    (mirror_loc.x, mirror_loc.y + plane_size, mirror_loc.z + plane_size),
                    (mirror_loc.x, mirror_loc.y - plane_size, mirror_loc.z + plane_size),
                ]
            elif self.current_axis == 'Y':
                verts = [
                    (mirror_loc.x - plane_size, mirror_loc.y, mirror_loc.z - plane_size),
                    (mirror_loc.x + plane_size, mirror_loc.y, mirror_loc.z - plane_size),
                    (mirror_loc.x + plane_size, mirror_loc.y, mirror_loc.z + plane_size),
                    (mirror_loc.x - plane_size, mirror_loc.y, mirror_loc.z + plane_size),
                ]
            else:
                verts = [
                    (mirror_loc.x - plane_size, mirror_loc.y - plane_size, mirror_loc.z),
                    (mirror_loc.x + plane_size, mirror_loc.y - plane_size, mirror_loc.z),
                    (mirror_loc.x + plane_size, mirror_loc.y + plane_size, mirror_loc.z),
                    (mirror_loc.x - plane_size, mirror_loc.y + plane_size, mirror_loc.z),
                ]
            
            plane_color = AXIS_COLORS_ALPHA.get(self.current_axis, AXIS_COLORS_ALPHA['NONE'])
            edge_color = AXIS_COLORS.get(self.current_axis, AXIS_COLORS['NONE'])
        
        indices = [(0, 1, 2), (2, 3, 0)]
        
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.face_culling_set('NONE')
        
        batch_plane = batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=indices)
        shader.bind()
        shader.uniform_float("color", plane_color)
        batch_plane.draw(shader)
        
        edge_verts = [
            verts[0], verts[1], verts[1], verts[2],
            verts[2], verts[3], verts[3], verts[0],
        ]
        
        gpu.state.line_width_set(2.0)
        batch_edges = batch_for_shader(shader, 'LINES', {"pos": edge_verts})
        shader.uniform_float("color", edge_color)
        batch_edges.draw(shader)
        
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')
        gpu.state.line_width_set(1.0)
    
    def invoke(self, context, event):
        # Pre-check for multiple mesh islands before starting the operation
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.normal_update()
        
        selected_verts = [v for v in bm.verts if v.select]
        
        if not selected_verts:
            self.report({'WARNING'}, "No geometry selected")
            return {'CANCELLED'}
        
        # Check for multiple disconnected meshes
        def get_linked_verts(start_vert, selected_set):
            """Flood-fill to find all vertices connected to start_vert within selected_set."""
            linked = set()
            to_visit = [start_vert]
            while to_visit:
                v = to_visit.pop()
                if v in linked or v not in selected_set:
                    continue
                linked.add(v)
                for edge in v.link_edges:
                    other = edge.other_vert(v)
                    if other in selected_set and other not in linked:
                        to_visit.append(other)
            return linked
        
        selected_set = set(selected_verts)
        linked_from_first = get_linked_verts(selected_verts[0], selected_set)
        
        if len(linked_from_first) < len(selected_verts):
            self.report({'WARNING'}, "Selection contains multiple disconnected meshes. Please select only one continuous mesh.")
            return {'CANCELLED'}
        
        self.start_mouse_x = event.mouse_region_x
        self.start_mouse_y = event.mouse_region_y
        self.current_mouse_x = event.mouse_region_x
        self.current_mouse_y = event.mouse_region_y
        self.current_axis = 'NONE'
        self.current_direction = 'NONE'
        self._use_normal_axis = False
        # Default to Object Pivot when the entire contiguous mesh is selected
        all_verts = set(bm.verts)
        entire_mesh_selected = selected_set == all_verts
        self._use_object_pivot = entire_mesh_selected
        self._active_normal = None
        self._active_tangent = None
        self._active_bitangent = None
        self._active_center = None
        self._active_is_edge = False
        self._edge_verts = None
        self._mirror_preview_world_loc = None
        
        # Pre-calculate pivot-based mirror location for preview
        pivot_type = context.scene.tool_settings.transform_pivot_point
        
        if pivot_type == 'CURSOR':
            self._mirror_preview_world_loc = context.scene.cursor.location.copy()
        elif pivot_type in {'INDIVIDUAL_ORIGINS', 'MEDIAN_POINT'}:
            median = mathutils.Vector((0, 0, 0))
            for v in selected_verts:
                median += v.co
            median /= len(selected_verts)
            self._mirror_preview_world_loc = obj.matrix_world @ median
        elif pivot_type == 'ACTIVE_ELEMENT':
            if bm.select_history:
                active_elem = bm.select_history.active
                if isinstance(active_elem, bmesh.types.BMVert):
                    self._mirror_preview_world_loc = obj.matrix_world @ active_elem.co
                elif isinstance(active_elem, bmesh.types.BMEdge):
                    edge_center = (active_elem.verts[0].co + active_elem.verts[1].co) / 2
                    self._mirror_preview_world_loc = obj.matrix_world @ edge_center
                elif isinstance(active_elem, bmesh.types.BMFace):
                    self._mirror_preview_world_loc = obj.matrix_world @ active_elem.calc_center_median()
            if self._mirror_preview_world_loc is None:
                self._mirror_preview_world_loc = obj.matrix_world.translation.copy()
        else:  # BOUNDING_BOX_CENTER
            min_co = mathutils.Vector((float('inf'), float('inf'), float('inf')))
            max_co = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
            for v in selected_verts:
                for i in range(3):
                    min_co[i] = min(min_co[i], v.co[i])
                    max_co[i] = max(max_co[i], v.co[i])
            center = (min_co + max_co) / 2
            self._mirror_preview_world_loc = obj.matrix_world @ center
        
        # Pre-calculate and store the active element's normal and center (in local space)
        if bm.select_history:
            active = bm.select_history.active
            normal = None
            center = None
            tangent = None  # Will be set explicitly for edges
            
            if isinstance(active, bmesh.types.BMVert):
                # Vertex normal (already in local space)
                normal = active.normal.copy()
                center = active.co.copy()
            elif isinstance(active, bmesh.types.BMEdge):
                # Edge - use edge direction as tangent, selected face's normal for proper unfolding
                edge_dir = (active.verts[1].co - active.verts[0].co).normalized()
                tangent = edge_dir  # Edge direction
                self._active_is_edge = True
                self._edge_verts = (active.verts[0].co.copy(), active.verts[1].co.copy())
                
                if active.link_faces:
                    # Find the SELECTED face that contains this edge for proper unfold
                    selected_face = None
                    for face in active.link_faces:
                        if face.select:
                            selected_face = face
                            break
                    
                    if selected_face is not None:
                        # Use the selected face's normal for unfolding
                        normal = selected_face.normal.copy()
                    else:
                        # Fallback: use first connected face's normal
                        normal = active.link_faces[0].normal.copy()
                else:
                    # No faces - construct a normal perpendicular to edge
                    if abs(edge_dir.dot(mathutils.Vector((0, 0, 1)))) < 0.99:
                        normal = edge_dir.cross(mathutils.Vector((0, 0, 1))).normalized()
                    else:
                        normal = edge_dir.cross(mathutils.Vector((1, 0, 0))).normalized()
                
                center = (active.verts[0].co + active.verts[1].co) / 2
            elif isinstance(active, bmesh.types.BMFace):
                # Face normal (already in local space)
                normal = active.normal.copy()
                center = active.calc_center_median()
            
            if normal is not None and normal.length > 0.001:
                self._active_normal = normal.normalized()
                self._active_center = center
                
                if tangent is not None:
                    # Edge case: tangent was explicitly set (edge direction)
                    self._active_tangent = tangent.normalized()
                    
                    # For edge unfold, create a VERTICAL mirror plane containing the edge
                    # This preserves world Z height during the unfold
                    world_z = mathutils.Vector((0, 0, 1))
                    
                    # Bitangent = tangent × world_z (gives horizontal vector perpendicular to edge)
                    # This makes the mirror plane vertical (perpendicular to world Z)
                    if abs(self._active_tangent.dot(world_z)) < 0.99:
                        self._active_bitangent = self._active_tangent.cross(world_z).normalized()
                    else:
                        # Edge is nearly vertical - use world Y instead
                        world_y = mathutils.Vector((0, 1, 0))
                        self._active_bitangent = self._active_tangent.cross(world_y).normalized()
                else:
                    # Build orthonormal basis (tangent, bitangent, normal)
                    # Find a vector not parallel to normal to compute tangent
                    if abs(normal.dot(mathutils.Vector((0, 0, 1)))) < 0.99:
                        up = mathutils.Vector((0, 0, 1))
                    else:
                        up = mathutils.Vector((1, 0, 0))
                    
                    self._active_tangent = normal.cross(up).normalized()
                    self._active_bitangent = normal.cross(self._active_tangent).normalized()
        
        # Fallback: if select_history was empty, derive normal from first selected element
        if self._active_normal is None:
            ts = context.tool_settings
            if ts.mesh_select_mode[2]:  # Face mode
                for face in bm.faces:
                    if face.select and face.normal.length > 0.01:
                        self._active_normal = face.normal.normalized()
                        self._active_center = face.calc_center_median()
                        if abs(self._active_normal.dot(mathutils.Vector((0, 0, 1)))) < 0.99:
                            up = mathutils.Vector((0, 0, 1))
                        else:
                            up = mathutils.Vector((1, 0, 0))
                        self._active_tangent = self._active_normal.cross(up).normalized()
                        self._active_bitangent = self._active_normal.cross(self._active_tangent).normalized()
                        break
            elif ts.mesh_select_mode[1]:  # Edge mode
                for edge in bm.edges:
                    if edge.select:
                        edge_dir = (edge.verts[1].co - edge.verts[0].co).normalized()
                        if edge.link_faces:
                            sel_face = next((f for f in edge.link_faces if f.select), None) or edge.link_faces[0]
                            fn = sel_face.normal
                            fb_normal = fn.normalized() if fn.length > 0.01 else None
                        else:
                            fb_normal = None
                        if fb_normal is None:
                            if abs(edge_dir.dot(mathutils.Vector((0, 0, 1)))) < 0.99:
                                fb_normal = edge_dir.cross(mathutils.Vector((0, 0, 1))).normalized()
                            else:
                                fb_normal = edge_dir.cross(mathutils.Vector((1, 0, 0))).normalized()
                        self._active_normal = fb_normal
                        self._active_center = (edge.verts[0].co + edge.verts[1].co) / 2
                        self._active_tangent = edge_dir
                        self._active_is_edge = True
                        self._edge_verts = (edge.verts[0].co.copy(), edge.verts[1].co.copy())
                        world_z = mathutils.Vector((0, 0, 1))
                        if abs(self._active_tangent.dot(world_z)) < 0.99:
                            self._active_bitangent = self._active_tangent.cross(world_z).normalized()
                        else:
                            self._active_bitangent = self._active_tangent.cross(mathutils.Vector((0, 1, 0))).normalized()
                        break
            else:  # Vertex mode
                for vert in bm.verts:
                    if vert.select and vert.normal.length > 0.01:
                        self._active_normal = vert.normal.normalized()
                        self._active_center = vert.co.copy()
                        if abs(self._active_normal.dot(mathutils.Vector((0, 0, 1)))) < 0.99:
                            up = mathutils.Vector((0, 0, 1))
                        else:
                            up = mathutils.Vector((1, 0, 0))
                        self._active_tangent = self._active_normal.cross(up).normalized()
                        self._active_bitangent = self._active_normal.cross(self._active_tangent).normalized()
                        break
        
        args = (self, context)
        self._handle_2d = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback_2d, args, 'WINDOW', 'POST_PIXEL')
        self._handle_3d = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback_3d, args, 'WINDOW', 'POST_VIEW')
        
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set("Move mouse to mirror geometry. C: Normal Constraint | V: Object Pivot (Bisect) | ESC: cancel")
        
        return {'RUNNING_MODAL'}
    
    def cleanup(self, context):
        if self._handle_2d:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle_2d, 'WINDOW')
            self._handle_2d = None
        if self._handle_3d:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle_3d, 'WINDOW')
            self._handle_3d = None
        context.area.header_text_set(None)
        context.area.tag_redraw()
    
    def modal(self, context, event):
        self.current_mouse_x = event.mouse_region_x
        self.current_mouse_y = event.mouse_region_y
        context.area.tag_redraw()
        
        # Normal Constraint toggle with 'C' key
        if event.type == 'C' and event.value == 'PRESS':
            if self._active_normal is not None:
                self._use_normal_axis = not self._use_normal_axis
                if self._use_normal_axis:
                    self._use_object_pivot = False
                mode_text = "NORMAL" if self._use_normal_axis else "WORLD"
                self.report({'INFO'}, f"Mirror mode: {mode_text}")
            else:
                self.report({'WARNING'}, "No active element with normal")
        
        # Object Pivot (Bisect) toggle with 'V' key
        if event.type == 'V' and event.value == 'PRESS':
            self._use_object_pivot = not self._use_object_pivot
            if self._use_object_pivot:
                self._use_normal_axis = False
            mode_text = "Object Pivot (Bisect)" if self._use_object_pivot else "Standard Pivot"
            self.report({'INFO'}, f"Pivot mode: {mode_text}")
        
        if event.type == 'MOUSEMOVE':
            dx = event.mouse_region_x - self.start_mouse_x
            dy = event.mouse_region_y - self.start_mouse_y
            
            axis, direction = self.get_axis_from_movement(context, dx, dy)
            self.current_axis = axis
            self.current_direction = direction
            
            flip_suffix = "+" if direction == 'POSITIVE' else "-"
            distance = (dx**2 + dy**2)**0.5
            
            if distance >= self.auto_apply_radius:
                use_flip = (direction == 'POSITIVE')
                self.cleanup(context)
                self.execute_mirror_geometry(context, axis, use_flip)
                return {'FINISHED'}
            
            if self._use_normal_axis:
                mode_text = " [NORMAL]"
            elif self._use_object_pivot:
                mode_text = " [OBJECT PIVOT]"
            else:
                mode_text = ""
            if distance > self.threshold:
                context.area.header_text_set(f"Axis: {axis}{flip_suffix}{mode_text} | C: Normal | V: Object Pivot | Release to confirm")
            else:
                context.area.header_text_set(f"Drag to select axis...{mode_text} | C: Normal | V: Object Pivot | ESC: cancel")
        
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            dx = event.mouse_region_x - self.start_mouse_x
            dy = event.mouse_region_y - self.start_mouse_y
            distance = (dx**2 + dy**2)**0.5
            
            if distance < self.threshold:
                self.cleanup(context)
                self.report({'WARNING'}, "Gesture too small, cancelled")
                return {'CANCELLED'}
            
            axis, direction = self.get_axis_from_movement(context, dx, dy)
            use_flip = (direction == 'POSITIVE')
            
            self.cleanup(context)
            self.execute_mirror_geometry(context, axis, use_flip)
            return {'FINISHED'}
        
        elif event.type in {'RIGHTMOUSE', 'ESC'}:
            self.cleanup(context)
            self.report({'INFO'}, "Cancelled")
            return {'CANCELLED'}
        
        return {'RUNNING_MODAL'}
    
    def get_axis_from_movement(self, context, dx, dy):
        """Determine axis and direction from mouse movement relative to view."""
        rv3d = context.region_data
        view_matrix = rv3d.view_matrix.inverted()
        obj = context.edit_object
        
        view_right = view_matrix.col[0].xyz.normalized()
        view_up = view_matrix.col[1].xyz.normalized()
        
        # Convert 2D gesture to 3D world direction
        move_3d_world = view_right * dx + view_up * dy
        
        # Convert to object local space
        move_3d = (obj.matrix_world.inverted().to_3x3() @ move_3d_world).normalized()
        
        if self._use_normal_axis and self._active_tangent is not None:
            # Project onto local coordinate system (tangent, bitangent, normal)
            proj_tangent = abs(move_3d.dot(self._active_tangent))
            proj_bitangent = abs(move_3d.dot(self._active_bitangent))
            proj_normal = abs(move_3d.dot(self._active_normal))
            
            max_val = max(proj_tangent, proj_bitangent, proj_normal)
            
            if self._active_is_edge:
                # For edges: swap X and Y for intuitive behavior
                # Dragging perpendicular to edge (bitangent) = X (unfold)
                # Dragging along edge (tangent) = Y (flip)
                if max_val == proj_bitangent:
                    axis = 'X'
                    direction = 'POSITIVE' if move_3d.dot(self._active_bitangent) > 0 else 'NEGATIVE'
                elif max_val == proj_tangent:
                    axis = 'Y'
                    direction = 'POSITIVE' if move_3d.dot(self._active_tangent) > 0 else 'NEGATIVE'
                else:
                    axis = 'Z'
                    direction = 'POSITIVE' if move_3d.dot(self._active_normal) > 0 else 'NEGATIVE'
            else:
                # Default behavior for vertices/faces
                if max_val == proj_tangent:
                    axis = 'X'
                    direction = 'POSITIVE' if move_3d.dot(self._active_tangent) > 0 else 'NEGATIVE'
                elif max_val == proj_bitangent:
                    axis = 'Y'
                    direction = 'POSITIVE' if move_3d.dot(self._active_bitangent) > 0 else 'NEGATIVE'
                else:
                    axis = 'Z'
                    direction = 'POSITIVE' if move_3d.dot(self._active_normal) > 0 else 'NEGATIVE'
        else:
            # Standard world axis detection
            abs_x = abs(move_3d_world.x)
            abs_y = abs(move_3d_world.y)
            abs_z = abs(move_3d_world.z)
            
            max_val = max(abs_x, abs_y, abs_z)
            
            if max_val == abs_x:
                axis = 'X'
                direction = 'POSITIVE' if move_3d_world.x > 0 else 'NEGATIVE'
            elif max_val == abs_y:
                axis = 'Y'
                direction = 'POSITIVE' if move_3d_world.y > 0 else 'NEGATIVE'
            else:
                axis = 'Z'
                direction = 'POSITIVE' if move_3d_world.z > 0 else 'NEGATIVE'
        
        return axis, direction
    
    def execute_mirror_geometry(self, context, axis, use_flip):
        """Mirror selected geometry across the specified axis."""
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        
        # Object Pivot mode: bisect at object origin, then mirror
        if self._use_object_pivot:
            mirror_local_loc = mathutils.Vector((0, 0, 0))
            axis_index = {'X': 0, 'Y': 1, 'Z': 2}[axis]
            
            # Build the bisect plane normal
            plane_no = mathutils.Vector((0, 0, 0))
            plane_no[axis_index] = 1.0
            
            # Bisect ALL geometry at the object origin
            all_geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
            
            # clear_inner removes the negative side, clear_outer removes the positive side
            # When use_flip (dragged positive): keep negative side, mirror to positive
            # When not use_flip (dragged negative): keep positive side, mirror to negative
            bmesh.ops.bisect_plane(
                bm,
                geom=all_geom,
                plane_co=mirror_local_loc,
                plane_no=plane_no,
                clear_inner=not use_flip,
                clear_outer=use_flip,
            )
            
            # Select all remaining geometry for duplication
            for v in bm.verts:
                v.select = True
            for e in bm.edges:
                e.select = True
            for f in bm.faces:
                f.select = True
            bm.select_flush(True)
            
            original_verts = list(bm.verts)
            
            # Duplicate all remaining geometry
            geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
            result = bmesh.ops.duplicate(bm, geom=geom)
            new_verts = [elem for elem in result['geom'] if isinstance(elem, bmesh.types.BMVert)]
            new_faces = [elem for elem in result['geom'] if isinstance(elem, bmesh.types.BMFace)]
            
            # Mirror the duplicated vertices across the object origin
            for v in new_verts:
                offset = v.co[axis_index] - mirror_local_loc[axis_index]
                v.co[axis_index] = mirror_local_loc[axis_index] - offset
            
            # Fix normals on mirrored geometry: reverse winding to match original orientation
            if new_faces:
                bmesh.ops.reverse_faces(bm, faces=new_faces)
            
            # Weld overlapping vertices at the seam
            verts_to_weld = [v for v in (original_verts + new_verts) if v.is_valid]
            verts_before = len(bm.verts)
            if verts_to_weld:
                bmesh.ops.remove_doubles(bm, verts=verts_to_weld, dist=0.01)
            merged_count = verts_before - len(bm.verts)
            
            bmesh.update_edit_mesh(obj.data)
            
            mirror_axis_text = f"{axis}-axis (bisect)"
            flip_text = " with flip" if use_flip else ""
            merge_text = f", merged {merged_count} vertices" if merged_count > 0 else ""
            self.report({'INFO'},
                       f"Mirrored {len(new_verts)} vertices across {mirror_axis_text}{flip_text}{merge_text}")
            return
        
        # Get mirror origin based on transform pivot point
        pivot_type = context.scene.tool_settings.transform_pivot_point
        
        if pivot_type == 'CURSOR':
            # Use 3D cursor position
            mirror_world_loc = context.scene.cursor.location.copy()
        elif pivot_type == 'INDIVIDUAL_ORIGINS':
            # For individual origins, use median of selection
            selected_verts = [v for v in bm.verts if v.select]
            if selected_verts:
                median = mathutils.Vector((0, 0, 0))
                for v in selected_verts:
                    median += v.co
                median /= len(selected_verts)
                mirror_world_loc = obj.matrix_world @ median
            else:
                mirror_world_loc = obj.matrix_world.translation.copy()
        elif pivot_type == 'MEDIAN_POINT':
            # Median point of selection
            selected_verts = [v for v in bm.verts if v.select]
            if selected_verts:
                median = mathutils.Vector((0, 0, 0))
                for v in selected_verts:
                    median += v.co
                median /= len(selected_verts)
                mirror_world_loc = obj.matrix_world @ median
            else:
                mirror_world_loc = obj.matrix_world.translation.copy()
        elif pivot_type == 'ACTIVE_ELEMENT':
            # Use active element position
            if bm.select_history:
                active = bm.select_history.active
                if isinstance(active, bmesh.types.BMVert):
                    # Vertex - use its position directly
                    mirror_world_loc = obj.matrix_world @ active.co
                elif isinstance(active, bmesh.types.BMEdge):
                    # Edge - calculate center from its two vertices
                    edge_center = (active.verts[0].co + active.verts[1].co) / 2
                    mirror_world_loc = obj.matrix_world @ edge_center
                elif isinstance(active, bmesh.types.BMFace):
                    # Face - use its center
                    mirror_world_loc = obj.matrix_world @ active.calc_center_median()
                else:
                    mirror_world_loc = obj.matrix_world.translation.copy()
            else:
                mirror_world_loc = obj.matrix_world.translation.copy()
        else:  # BOUNDING_BOX_CENTER
            # Bounding box center of selection
            selected_verts = [v for v in bm.verts if v.select]
            if selected_verts:
                min_co = mathutils.Vector((float('inf'), float('inf'), float('inf')))
                max_co = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
                for v in selected_verts:
                    for i in range(3):
                        min_co[i] = min(min_co[i], v.co[i])
                        max_co[i] = max(max_co[i], v.co[i])
                center = (min_co + max_co) / 2
                mirror_world_loc = obj.matrix_world @ center
            else:
                mirror_world_loc = obj.matrix_world.translation.copy()
        
        mirror_local_loc = obj.matrix_world.inverted() @ mirror_world_loc
        
        # Check if selection is a single element (vert, edge, or face) - if so, expand to entire linked mesh
        selected_faces = [f for f in bm.faces if f.select]
        selected_verts_initial = [v for v in bm.verts if v.select]
        
        # Expand selection if there are selected verts but at most one face
        # (single face follows the same expand behaviour as single vertex/edge)
        if len(selected_verts_initial) > 0 and len(selected_faces) <= 1:
            # Start from all selected vertices' linked faces
            for vert in selected_verts_initial:
                for face in vert.link_faces:
                    face.select = True
                    for v in face.verts:
                        v.select = True
                    for e in face.edges:
                        e.select = True
            
            # Flood-fill to get all connected geometry using BFS
            queue = [f for f in bm.faces if f.select]
            while queue:
                face = queue.pop()
                for edge in face.edges:
                    for linked_face in edge.link_faces:
                        if not linked_face.select:
                            linked_face.select = True
                            for v in linked_face.verts:
                                v.select = True
                            for e in linked_face.edges:
                                e.select = True
                            queue.append(linked_face)
        
        # Get selected vertices
        selected_verts = [v for v in bm.verts if v.select]
        
        if not selected_verts:
            self.report({'WARNING'}, "No geometry selected")
            return
        
        # Store original selected verts for welding later
        original_selected_verts = list(selected_verts)
        
        # Duplicate selected geometry
        geom = []
        geom.extend([v for v in bm.verts if v.select])
        geom.extend([e for e in bm.edges if e.select])
        geom.extend([f for f in bm.faces if f.select])
        
        result = bmesh.ops.duplicate(bm, geom=geom)
        new_verts = [elem for elem in result['geom'] if isinstance(elem, bmesh.types.BMVert)]
        new_faces = [elem for elem in result['geom'] if isinstance(elem, bmesh.types.BMFace)]
        
        # Mirror the duplicated vertices
        if self._use_normal_axis and self._active_normal is not None and self._active_center is not None:
            # Normal-based mirroring: use active element's local coordinate system
            center_local = self._active_center
            
            if self._active_is_edge:
                # For edges: create world-aligned mirror planes containing the edge
                # The gesture direction determines which world axis to preserve
                edge_dir = self._active_tangent
                
                if axis == 'X':
                    # Mirror plane perpendicular to world X, containing the edge
                    world_x = mathutils.Vector((1, 0, 0))
                    # Project world X onto plane perpendicular to edge, then use that
                    mirror_axis_vec = (world_x - world_x.project(edge_dir)).normalized()
                    if mirror_axis_vec.length < 0.01:
                        # Edge is along X - use world Y instead
                        world_y = mathutils.Vector((0, 1, 0))
                        mirror_axis_vec = (world_y - world_y.project(edge_dir)).normalized()
                    mirror_axis_text = "edge-X"
                elif axis == 'Y':
                    # Mirror plane perpendicular to world Y, containing the edge
                    world_y = mathutils.Vector((0, 1, 0))
                    mirror_axis_vec = (world_y - world_y.project(edge_dir)).normalized()
                    if mirror_axis_vec.length < 0.01:
                        # Edge is along Y - use world X instead
                        world_x = mathutils.Vector((1, 0, 0))
                        mirror_axis_vec = (world_x - world_x.project(edge_dir)).normalized()
                    mirror_axis_text = "edge-Y"
                else:  # Z
                    # Mirror plane perpendicular to world Z, containing the edge (vertical plane)
                    world_z = mathutils.Vector((0, 0, 1))
                    mirror_axis_vec = (world_z - world_z.project(edge_dir)).normalized()
                    if mirror_axis_vec.length < 0.01:
                        # Edge is along Z - use world X instead
                        world_x = mathutils.Vector((1, 0, 0))
                        mirror_axis_vec = (world_x - world_x.project(edge_dir)).normalized()
                    mirror_axis_text = "edge-Z"
            else:
                # Default: X -> tangent, Y -> bitangent, Z -> normal
                if axis == 'X' and self._active_tangent is not None:
                    mirror_axis_vec = self._active_tangent
                    mirror_axis_text = "normal-X"
                elif axis == 'Y' and self._active_bitangent is not None:
                    mirror_axis_vec = self._active_bitangent
                    mirror_axis_text = "normal-Y"
                else:
                    mirror_axis_vec = self._active_normal
                    mirror_axis_text = "normal-Z"
            
            # Delete all faces lying on the mirror plane from both original and duplicate
            seam_thresh = 0.0001
            orig_seam = [f for f in bm.faces
                         if f.select and f.is_valid and
                         all(abs((v.co - center_local).dot(mirror_axis_vec)) < seam_thresh
                             for v in f.verts)]
            dup_seam = [f for f in new_faces
                        if f.is_valid and
                        all(abs((v.co - center_local).dot(mirror_axis_vec)) < seam_thresh
                            for v in f.verts)]
            seam_to_delete = orig_seam + dup_seam
            if seam_to_delete:
                seam_edges = {e for f in seam_to_delete for e in f.edges if e.is_valid}
                bmesh.ops.delete(bm, geom=seam_to_delete, context='FACES_ONLY')
                new_faces = [f for f in new_faces if f.is_valid]
                loose_edges = [e for e in seam_edges if e.is_valid and len(e.link_faces) == 0]
                if loose_edges:
                    bmesh.ops.delete(bm, geom=loose_edges, context='EDGES')
                new_verts = [v for v in new_verts if v.is_valid]
            
            for v in new_verts:
                # Mirror point across plane: P' = P - 2 * dot(P - C, N) * N
                to_point = v.co - center_local
                dist = to_point.dot(mirror_axis_vec)
                v.co = v.co - 2 * dist * mirror_axis_vec
            
            # For edge-based mirroring, snap vertices near the edge to lie on the edge line
            if self._active_is_edge and self._edge_verts is not None:
                edge_p0, edge_p1 = self._edge_verts
                edge_dir = (edge_p1 - edge_p0).normalized()
                edge_length = (edge_p1 - edge_p0).length
                
                snap_threshold = 0.0001  # Distance threshold for snapping (precision)
                
                for v in new_verts:
                    # Check if vertex was originally on the edge by computing distance 
                    # from original position (before mirroring)
                    to_vert = v.co - edge_p0
                    t = to_vert.dot(edge_dir)  # Parameter along edge
                    closest_on_edge = edge_p0 + t * edge_dir
                    
                    # If vertex is close to the edge line, snap it exactly
                    dist_to_edge = (v.co - closest_on_edge).length
                    if dist_to_edge < snap_threshold:
                        # Snap to the closest point on the edge (clamped to edge endpoints)
                        t_clamped = max(0, min(edge_length, t))
                        v.co = edge_p0 + t_clamped * edge_dir
        else:
            # Standard axis-aligned mirroring
            axis_index = {'X': 0, 'Y': 1, 'Z': 2}[axis]
            
            # Delete all faces lying on the mirror plane from both original and duplicate
            seam_thresh = 0.0001
            plane_coord = mirror_local_loc[axis_index]
            orig_seam = [f for f in bm.faces
                         if f.select and f.is_valid and
                         all(abs(v.co[axis_index] - plane_coord) < seam_thresh
                             for v in f.verts)]
            dup_seam = [f for f in new_faces
                        if f.is_valid and
                        all(abs(v.co[axis_index] - plane_coord) < seam_thresh
                            for v in f.verts)]
            seam_to_delete = orig_seam + dup_seam
            if seam_to_delete:
                seam_edges = {e for f in seam_to_delete for e in f.edges if e.is_valid}
                bmesh.ops.delete(bm, geom=seam_to_delete, context='FACES_ONLY')
                new_faces = [f for f in new_faces if f.is_valid]
                loose_edges = [e for e in seam_edges if e.is_valid and len(e.link_faces) == 0]
                if loose_edges:
                    bmesh.ops.delete(bm, geom=loose_edges, context='EDGES')
                new_verts = [v for v in new_verts if v.is_valid]
            
            for v in new_verts:
                # Mirror across the plane at mirror_local_loc
                offset = v.co[axis_index] - mirror_local_loc[axis_index]
                v.co[axis_index] = mirror_local_loc[axis_index] - offset
            
            mirror_axis_text = f"{axis}-axis"
        
        # Reverse face winding order to match the original's normal orientation (mirroring inverts winding)
        if new_faces:
            bmesh.ops.reverse_faces(bm, faces=new_faces)
        
        # Weld overlapping vertices at the mirror seam
        # Include both original selected verts and the new mirrored verts
        # Filter to only valid vertices
        verts_to_weld = [v for v in (original_selected_verts + new_verts) if v.is_valid]
        verts_before = len(bm.verts)
        if verts_to_weld:
            bmesh.ops.remove_doubles(bm, verts=verts_to_weld, dist=0.01)
        merged_count = verts_before - len(bm.verts)
        
        # Update mesh
        bmesh.update_edit_mesh(obj.data)
        
        flip_text = " with flip" if use_flip else ""
        merge_text = f", merged {merged_count} vertices" if merged_count > 0 else ""
        self.report({'INFO'}, f"Mirrored {len(new_verts)} vertices across {mirror_axis_text}{flip_text}{merge_text}")


# Menu entries
def menu_func_object(self, context):
    self.layout.operator(OBJECT_OT_gesture_mirror.bl_idname, text="Gesture Mirror")


def menu_func_mesh(self, context):
    self.layout.operator(MESH_OT_gesture_mirror_geometry.bl_idname, text="Gesture Mirror")


def register():
    bpy.utils.register_class(OBJECT_OT_gesture_mirror)
    bpy.utils.register_class(MESH_OT_gesture_mirror_geometry)
    bpy.types.VIEW3D_MT_object.append(menu_func_object)
    bpy.types.VIEW3D_MT_edit_mesh.append(menu_func_mesh)


def unregister():
    bpy.types.VIEW3D_MT_edit_mesh.remove(menu_func_mesh)
    bpy.types.VIEW3D_MT_object.remove(menu_func_object)
    bpy.utils.unregister_class(MESH_OT_gesture_mirror_geometry)
    bpy.utils.unregister_class(OBJECT_OT_gesture_mirror)


if __name__ == "__main__":
    register()