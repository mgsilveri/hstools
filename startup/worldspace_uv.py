import bpy
import bmesh
from mathutils import Vector

class MESH_OT_world_space_uvs(bpy.types.Operator):
    """Apply world-space planar UV projection based on polygon normals"""
    bl_idname = "mesh.world_space_uvs"
    bl_label = "World Space UVs"
    bl_options = {'REGISTER', 'UNDO'}
    
    texture_preset: bpy.props.EnumProperty(
        name="Tex Density",
        description="Base texture resolution for texel density calculation",
        items=[
            ('256', "256", "256x256 base resolution"),
            ('512', "512", "512x512 base resolution"),
            ('768', "768", "768x768 base resolution"),
            ('1024', "1024", "1024x1024 base resolution"),
            ('2048', "2048", "2048x2048 base resolution"),
            ('4096', "4096", "4096x4096 base resolution"),
        ],
        default='1024'
    )
    
    align_to_origin: bpy.props.BoolProperty(
        name="Align Islands to Origin",
        description="Move each UV island so its bottom-left corner starts at 0,0 (useful for tiling modules)",
        default=False
    )
    
    def execute(self, context):
        # Get all selected mesh objects
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'ERROR'}, "Please select at least one mesh object")
            return {'CANCELLED'}
        
        preset_value = float(self.texture_preset)
        total_objects_processed = 0
        total_faces = 0
        
        # Store original mode and active object
        original_mode = context.mode
        original_active = context.view_layer.objects.active
        
        # Process each selected mesh object
        for obj in selected_objects:
            # Skip objects that can't be edited
            if obj.library or obj.override_library:
                self.report({'WARNING'}, f"Skipping linked object: {obj.name}")
                continue
            
            # Set as active
            context.view_layer.objects.active = obj
            
            # Try to enter edit mode
            try:
                if context.mode != 'EDIT_MESH':
                    bpy.ops.object.mode_set(mode='EDIT')
            except RuntimeError as e:
                self.report({'WARNING'}, f"Cannot edit object {obj.name}: {str(e)}")
                continue
            
            bm = bmesh.from_edit_mesh(obj.data)
            
            # Get or create UV layer
            uv_layer = bm.loops.layers.uv.active
            if not uv_layer:
                uv_layer = bm.loops.layers.uv.new("UVMap")
            
            # Get selected faces (or all if none selected)
            faces = [f for f in bm.faces if f.select]
            if not faces:
                faces = list(bm.faces)
            
            if not faces:
                continue
            
            # Group by texture size
            texture_groups = self.group_by_texture_size(faces, obj)
            
            # Process each texture size group
            for tex_size, group_faces in texture_groups.items():
                x_faces, y_faces, z_faces = self.split_by_axis(group_faces, obj)
                
                # Calculate scale: texture_resolution / preset_value
                scale_factor_x = tex_size[0] / preset_value
                scale_factor_y = tex_size[1] / preset_value
                
                if x_faces:
                    self.project_axis(x_faces, uv_layer, 'X', obj.matrix_world, scale_factor_x, scale_factor_y)
                    total_faces += len(x_faces)
                if y_faces:
                    self.project_axis(y_faces, uv_layer, 'Y', obj.matrix_world, scale_factor_x, scale_factor_y)
                    total_faces += len(y_faces)
                if z_faces:
                    self.project_axis(z_faces, uv_layer, 'Z', obj.matrix_world, scale_factor_x, scale_factor_y)
                    total_faces += len(z_faces)
            
            # Align UV islands to origin if requested
            if self.align_to_origin:
                self.align_islands_to_origin(bm, uv_layer, faces)
            
            bmesh.update_edit_mesh(obj.data)
            total_objects_processed += 1
        
        # Restore original active object and mode
        if original_active:
            context.view_layer.objects.active = original_active
        
        # Restore original mode
        try:
            if original_mode == 'OBJECT' and context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:
            pass  # Ignore if we can't switch back
        
        if total_objects_processed == 0:
            self.report({'ERROR'}, "No objects could be processed")
            return {'CANCELLED'}
        
        self.report({'INFO'}, f"Projected {total_faces} faces across {total_objects_processed} objects (preset: {self.texture_preset})")
        
        return {'FINISHED'}
    
    def split_by_axis(self, faces, obj):
        """Separate faces by dominant axis in world space"""
        x_faces = []
        y_faces = []
        z_faces = []
        
        # Get the 3x3 rotation+scale matrix
        transform_matrix = obj.matrix_world.to_3x3()
        
        for face in faces:
            # Transform local normal to world space (includes rotation and scale effects)
            world_normal = transform_matrix @ face.normal
            world_normal.normalize()
            
            abs_x = abs(world_normal.x)
            abs_y = abs(world_normal.y)
            abs_z = abs(world_normal.z)
            
            # 0.7 threshold from Modo script
            if abs_x > 0.7:
                x_faces.append(face)
            elif abs_y > 0.7:
                y_faces.append(face)
            else:
                z_faces.append(face)
        
        return x_faces, y_faces, z_faces
    
    def project_axis(self, faces, uv_layer, axis, matrix_world, scale_u, scale_v):
        """Project UVs along specified axis with scaling"""
        
        # Get the 3x3 rotation+scale matrix for normal transformation
        transform_matrix = matrix_world.to_3x3()
        
        for face in faces:
            # Get world space normal to determine orientation
            world_normal = transform_matrix @ face.normal
            world_normal.normalize()
            
            for loop in face.loops:
                # Transform to world space (includes location, rotation, and scale)
                vert_world = matrix_world @ loop.vert.co
                
                if axis == 'X':
                    # YZ projection - flip U based on normal direction
                    u = vert_world.y / scale_u if world_normal.x >= 0 else -vert_world.y / scale_u
                    v = vert_world.z / scale_v
                elif axis == 'Y':
                    # XZ projection - flip U based on normal direction
                    u = vert_world.x / scale_u if world_normal.y >= 0 else -vert_world.x / scale_u
                    v = vert_world.z / scale_v
                else:  # Z
                    # XY projection - flip U based on normal direction
                    u = vert_world.x / scale_u if world_normal.z >= 0 else -vert_world.x / scale_u
                    v = -vert_world.y / scale_v
                
                loop[uv_layer].uv = (u, v)
    
    def group_by_texture_size(self, faces, obj):
        """Group faces by their material's texture size"""
        groups = {}
        
        for face in faces:
            tex_size = self.get_texture_size(face, obj)
            if tex_size not in groups:
                groups[tex_size] = []
            groups[tex_size].append(face)
        
        return groups
    
    def get_texture_size(self, face, obj):
        """Get texture dimensions from face material"""
        if face.material_index < len(obj.data.materials):
            mat = obj.data.materials[face.material_index]
            if mat and mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        return (node.image.size[0], node.image.size[1])
        
        return (1024, 1024)  # Default 1024x1024
    
    def align_islands_to_origin(self, bm, uv_layer, processed_faces):
        """Move each UV island so its bottom-left corner is at 0,0"""
        # For world-space UVs, treat each face as its own island
        for face in processed_faces:
            # Calculate bounding box for this face
            min_u = float('inf')
            min_v = float('inf')
            
            for loop in face.loops:
                uv = loop[uv_layer].uv
                min_u = min(min_u, uv.x)
                min_v = min(min_v, uv.y)
            
            # Offset all UVs in this face to align bottom-left to origin
            offset_u = -min_u
            offset_v = -min_v
            
            for loop in face.loops:
                uv = loop[uv_layer].uv
                loop[uv_layer].uv = (uv.x + offset_u, uv.y + offset_v)

# Add to Object menu (Object mode)
def object_menu_func(self, context):
    self.layout.separator()
    self.layout.operator(MESH_OT_world_space_uvs.bl_idname)

# Add to UV menu (Edit mode)
def edit_uv_menu_func(self, context):
    self.layout.separator()
    self.layout.operator(MESH_OT_world_space_uvs.bl_idname)

# Add to UV Editor menu
def uv_editor_menu_func(self, context):
    self.layout.separator()
    self.layout.operator(MESH_OT_world_space_uvs.bl_idname)

def register():
    bpy.utils.register_class(MESH_OT_world_space_uvs)
    
    # Add to Object menu (Object mode)
    bpy.types.VIEW3D_MT_object.append(object_menu_func)
    
    # Add to UV menu (Edit mode)
    bpy.types.VIEW3D_MT_uv_map.append(edit_uv_menu_func)
    
    # Add to UV Editor menu
    bpy.types.IMAGE_MT_uvs.append(uv_editor_menu_func)

def unregister():
    bpy.utils.unregister_class(MESH_OT_world_space_uvs)
    
    # Remove from menus
    bpy.types.VIEW3D_MT_object.remove(object_menu_func)
    bpy.types.VIEW3D_MT_uv_map.remove(edit_uv_menu_func)
    bpy.types.IMAGE_MT_uvs.remove(uv_editor_menu_func)

if __name__ == "__main__":
    register()