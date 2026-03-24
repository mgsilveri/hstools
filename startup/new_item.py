import bpy

bl_info = {
    "name": "Create Empty Mesh",
    "author": "Your Name",
    "version": (1, 0),
    "blender": (2, 80, 0),
    "location": "Object Menu > Create Empty / Mesh Menu > Create Empty",
    "description": "Creates a new empty mesh in the active collection",
    "category": "Object",
}

class OBJECT_OT_create_empty_mesh(bpy.types.Operator):
    """Create a new empty mesh object in the active collection"""
    bl_idname = "object.create_empty_mesh"
    bl_label = "Create Empty"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Store the current mode and active object (before any deselection)
        original_mode = context.mode
        source_object = context.active_object

        # Determine parent to inherit (if the source object is nested under another)
        target_parent = source_object.parent if source_object else None

        # Determine target collection from the source object or active collection
        if source_object:
            target_collection = None
            for collection in source_object.users_collection:
                target_collection = collection
                break
            if target_collection is None:
                target_collection = context.view_layer.active_layer_collection.collection
        else:
            target_collection = context.view_layer.active_layer_collection.collection

        # Deselect all currently selected objects
        for obj in context.selected_objects:
            obj.select_set(False)

        # If in Edit Mode, switch to Object Mode
        if original_mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')

        # Create a new empty mesh
        mesh = bpy.data.meshes.new(name="EmptyMesh")

        # Create a new object with the mesh
        obj = bpy.data.objects.new(name="EmptyMeshObject", object_data=mesh)

        # Link the object to the target collection
        target_collection.objects.link(obj)

        # If the source object had a parent, nest the new object under the same parent
        if target_parent is not None:
            obj.parent = target_parent
            obj.matrix_parent_inverse = target_parent.matrix_world.inverted()

        # Make the new object the active object
        context.view_layer.objects.active = obj
        obj.select_set(True)

        # Return to Edit Mode if originally in Edit Mode
        if original_mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, f"Created empty mesh '{obj.name}' in collection '{target_collection.name}'")
        return {'FINISHED'}


def menu_func(self, context):
    self.layout.operator(OBJECT_OT_create_empty_mesh.bl_idname)


def register():
    bpy.utils.register_class(OBJECT_OT_create_empty_mesh)
    bpy.types.VIEW3D_MT_object.append(menu_func)
    bpy.types.VIEW3D_MT_edit_mesh.append(menu_func)


def unregister():
    bpy.utils.unregister_class(OBJECT_OT_create_empty_mesh)
    bpy.types.VIEW3D_MT_object.remove(menu_func)
    bpy.types.VIEW3D_MT_edit_mesh.remove(menu_func)


if __name__ == "__main__":
    register()