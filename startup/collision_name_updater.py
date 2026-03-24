import bpy
from bpy.app.handlers import persistent


@persistent
def update_collision_names(scene, depsgraph):
    for obj in bpy.data.objects:
        for child in obj.children:
            ctype = child.get("collision")
            if ctype == "collision_ld":
                expected = f"{obj.name}_col_ld"
            elif ctype == "collision_hd":
                expected = f"{obj.name}_col_hd"
            else:
                continue
            if child.name != expected:
                old_name = child.name
                child.name = expected
                print(f"[Collision Updater] Renamed '{old_name}' -> '{expected}'")


def register():
    bpy.app.handlers.depsgraph_update_post.append(update_collision_names)


def unregister():
    if update_collision_names in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(update_collision_names)


if __name__ == "__main__":
    register()
