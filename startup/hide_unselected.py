import bpy

# Dictionary to store original visibility states
original_states = {}

class OBJECT_OT_ToggleHideUnselected(bpy.types.Operator):
    """Toggle hide/show unselected objects while preserving their original visibility state"""
    bl_idname = "object.toggle_hide_unselected"
    bl_label = "Isolate Selected"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        global original_states
        
        mode = context.mode
        
        if mode not in ['OBJECT', 'EDIT_MESH']:
            self.report({'WARNING'}, "Operator only works in Object or Edit mode")
            return {'CANCELLED'}
        
        # Check if we're currently in isolated mode
        if original_states:
            # Restore mode - unhide and restore original states
            view_layer_objects = context.view_layer.objects
            for obj in view_layer_objects:
                if obj.name in original_states:
                    obj.hide_set(original_states[obj.name])
            
            original_states.clear()
            self.report({'INFO'}, "Restored original visibility states")
        else:
            # Isolate mode - store states and hide unselected
            
            # Get objects in the current view layer
            view_layer_objects = context.view_layer.objects
            
            # Store original states (only for objects in view layer)
            for obj in view_layer_objects:
                original_states[obj.name] = obj.hide_get()
            
            if mode == 'OBJECT':
                # Hide unselected objects
                for obj in view_layer_objects:
                    if obj not in context.selected_objects:
                        obj.hide_set(True)
                
                self.report({'INFO'}, f"Isolated {len(context.selected_objects)} selected object(s)")
            
            elif mode == 'EDIT_MESH':
                # Get all objects in edit mode (selected objects)
                edit_objects = set(context.selected_objects)
                
                # Hide all objects not in edit mode
                for obj in view_layer_objects:
                    if obj not in edit_objects:
                        obj.hide_set(True)
                
                self.report({'INFO'}, f"Isolated {len(edit_objects)} object(s) in edit mode")
        
        return {'FINISHED'}


def menu_func(self, context):
    global original_states
    layout = self.layout
    layout.separator()
    
    # Change the text based on current state
    if original_states:
        layout.operator("object.toggle_hide_unselected", text="End Isolation", icon='HIDE_OFF')
    else:
        layout.operator("object.toggle_hide_unselected", text="Isolate Selected", icon='HIDE_ON')


def register():
    bpy.utils.register_class(OBJECT_OT_ToggleHideUnselected)
    bpy.types.VIEW3D_MT_view.append(menu_func)


def unregister():
    bpy.types.VIEW3D_MT_view.remove(menu_func)
    bpy.utils.unregister_class(OBJECT_OT_ToggleHideUnselected)
    global original_states
    original_states.clear()


if __name__ == "__main__":
    register()
    
    # You can also run the operator directly with:
    # bpy.ops.object.toggle_hide_unselected()