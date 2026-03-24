"""instance_tagging.py — Automatic instance tagging for linked duplicates.

For every object that shares its data-block with at least one other object
(i.e. a linked duplicate — Modo-style "instance") this handler:
  1. Prefixes the object name with "inst_" (strips it when no longer shared)
  2. Adds the object to a managed "Instances" collection with a colour tag
     so it is clearly marked in the native Outliner.
"""

import bpy
from . import state


# ============================================================================
# Collection helpers
# ============================================================================

def _get_or_create_instances_collection(scene):
    col = bpy.data.collections.get(state._INST_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(state._INST_COLLECTION)
        scene.collection.children.link(col)
    col.color_tag = state._INST_COLLECTION_TAG
    return col


def _remove_instances_collection_if_empty():
    col = bpy.data.collections.get(state._INST_COLLECTION)
    if col is None:
        return
    if len(col.objects) == 0:
        for scene in bpy.data.scenes:
            for parent_col in scene.collection.children:
                if parent_col == col:
                    scene.collection.children.unlink(col)
                    break
        bpy.data.collections.remove(col)


def _obj_user_collections(obj):
    return [c for c in bpy.data.collections if obj.name in c.objects]


def _move_to_instances_col(obj, inst_col):
    if '_modo_orig_cols' not in obj:
        orig = [c.name for c in _obj_user_collections(obj)
                if c.name != state._INST_COLLECTION]
        in_scene_root = obj.name in bpy.context.scene.collection.objects
        obj['_modo_orig_cols'] = orig
        obj['_modo_in_scene_root'] = in_scene_root

        for cname in orig:
            c = bpy.data.collections.get(cname)
            if c and obj.name in c.objects:
                c.objects.unlink(obj)
        if in_scene_root and obj.name in bpy.context.scene.collection.objects:
            bpy.context.scene.collection.objects.unlink(obj)

    if obj.name not in inst_col.objects:
        inst_col.objects.link(obj)


def _restore_from_instances_col(obj):
    col = bpy.data.collections.get(state._INST_COLLECTION)
    if col and obj.name in col.objects:
        col.objects.unlink(obj)

    orig_cols = obj.get('_modo_orig_cols', [])
    in_scene_root = obj.get('_modo_in_scene_root', False)

    for cname in orig_cols:
        c = bpy.data.collections.get(cname)
        if c and obj.name not in c.objects:
            c.objects.link(obj)

    if in_scene_root and obj.name not in bpy.context.scene.collection.objects:
        bpy.context.scene.collection.objects.link(obj)

    for key in ('_modo_orig_cols', '_modo_in_scene_root'):
        if key in obj:
            del obj[key]


# ============================================================================
# Depsgraph handler
# ============================================================================

@bpy.app.handlers.persistent
def _instance_tag_depsgraph_handler(scene, depsgraph):
    """Prefix and move linked-duplicate objects into the Instances collection.

    Mutations are deferred via a bpy.app.timers callback so they never happen
    while Blender is evaluating the depsgraph.
    """
    import time
    from collections import defaultdict

    now = time.monotonic()
    if now - state._instance_tag_last_run < 0.5:
        return
    state._instance_tag_last_run = now

    try:
        _prefs = bpy.context.preferences.addons[
            'modo_style_selection_for_blender'].preferences
        if not _prefs.enable_instance_tagging:
            return
    except (KeyError, AttributeError):
        pass

    try:
        groups = defaultdict(list)
        for obj in scene.objects:
            data = getattr(obj, 'data', None)
            if data is not None:
                groups[data].append(obj)

        pending = []

        for data, objects in groups.items():
            if len(objects) == 1:
                obj = objects[0]
                if obj.name.startswith(state._INST_PREFIX):
                    pending.append(('restore', obj.name,
                                    obj.name[len(state._INST_PREFIX):]))
                elif '_modo_source_id' in data:
                    pending.append(('clear_source', id(data)))
            else:
                source_id = data.get('_modo_source_id')
                source_obj = None
                if source_id:
                    for o in objects:
                        bare = (o.name[len(state._INST_PREFIX):]
                                if o.name.startswith(state._INST_PREFIX)
                                else o.name)
                        if bare == source_id or o.name == source_id:
                            source_obj = o
                            break
                if source_obj is None:
                    candidates = [o for o in objects
                                  if not o.name.startswith(state._INST_PREFIX)]
                    source_obj = candidates[0] if candidates else objects[0]
                for obj in objects:
                    if obj is source_obj:
                        if obj.name.startswith(state._INST_PREFIX):
                            pending.append(('restore_source', obj.name,
                                            obj.name[len(state._INST_PREFIX):],
                                            id(data)))
                        else:
                            pending.append(('ensure_source', obj.name,
                                            obj.name, id(data)))
                    else:
                        if not obj.name.startswith(state._INST_PREFIX):
                            import re
                            src_name = source_obj.name
                            if src_name.startswith(state._INST_PREFIX):
                                src_name = src_name[len(state._INST_PREFIX):]
                            base = re.sub(r'\.\d+$', '', obj.name)
                            chosen = base if base == src_name else obj.name
                            pending.append(('tag', obj.name,
                                            state._INST_PREFIX + chosen))

        if not pending:
            return

        def _apply_pending():
            try:
                _apply_instance_tag_mutations(pending)
            except Exception:
                pass
            return None

        bpy.app.timers.register(_apply_pending, first_interval=0.0)
    except Exception:
        pass


def _apply_instance_tag_mutations(pending):
    """Apply deferred instance-tag mutations outside the depsgraph update."""
    scene = bpy.context.scene if bpy.context else None
    if scene is None:
        return
    inst_col = None
    data_map = {id(d): d for d in bpy.data.meshes}
    data_map.update({id(d): d for d in bpy.data.curves})
    data_map.update({id(d): d for d in bpy.data.metaballs})

    for entry in pending:
        action = entry[0]
        if action == 'restore':
            _, old_name, new_name = entry
            obj = bpy.data.objects.get(old_name)
            if obj:
                _restore_from_instances_col(obj)
                obj.name = new_name
        elif action == 'clear_source':
            data = data_map.get(entry[1])
            if data and '_modo_source_id' in data:
                del data['_modo_source_id']
        elif action in ('restore_source', 'ensure_source'):
            _, old_name, new_name, data_id = entry
            obj = bpy.data.objects.get(old_name)
            if obj:
                _restore_from_instances_col(obj)
                if obj.name != new_name:
                    obj.name = new_name
                data = data_map.get(data_id)
                if data:
                    data['_modo_source_id'] = obj.name
        elif action == 'tag':
            _, old_name, new_name = entry
            obj = bpy.data.objects.get(old_name)
            if obj:
                obj.name = new_name
                if inst_col is None:
                    inst_col = _get_or_create_instances_collection(scene)
                _move_to_instances_col(obj, inst_col)

    _remove_instances_collection_if_empty()
