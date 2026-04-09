"""
mgBaker – Export‑group PropertyGroup and Scene registration.
"""

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)


_RES_ITEMS = [
    ('256',  '256', ''),
    ('512',  '512', ''),
    ('1024', '1K',  ''),
    ('2048', '2K',  ''),
    ('4096', '4K',  ''),
    ('8192', '8K',  ''),
]


class MG_ExportGroup(bpy.types.PropertyGroup):
    """One bake‑export group (HP + LP + settings)."""

    # ── identity ──────────────────────────────────────────────────────────
    name: StringProperty(name="Name", default="Group")
    include: BoolProperty(name="Include", description="Include this group in the next export", default=True)

    # ── collections ───────────────────────────────────────────────────────
    hp_collection: PointerProperty(
        name="HP Collection",
        description="High-poly source collection for this bake group",
        type=bpy.types.Collection,
    )
    lp_collection: PointerProperty(
        name="LP Collection",
        description="Low-poly target collection for this bake group",
        type=bpy.types.Collection,
    )

    # ── cage ──────────────────────────────────────────────────────────────
    cage_offset: FloatProperty(
        name="Cage Offset",
        description="Cage offset (0.0 – 1.0) — maps to Toolbag maxOffset and Painter MaxFrontalDistance",
        default=0.3,
        min=0.0,
        max=1.0,
        precision=3,
    )

    # ── resolution ────────────────────────────────────────────────────────
    res_x: EnumProperty(
        name="Resolution X",
        description="Bake texture width in pixels",
        items=_RES_ITEMS,
        default='2048',
    )
    res_y: EnumProperty(
        name="Resolution Y",
        description="Bake texture height in pixels",
        items=_RES_ITEMS,
        default='2048',
    )

    # ── bake maps ─────────────────────────────────────────────────────────
    bake_normal: BoolProperty(name="Normal", description="Bake tangent-space normal map", default=True)
    bake_ao: BoolProperty(name="AO", description="Bake ambient occlusion map", default=True)
    bake_curvature: BoolProperty(name="Curvature", description="Bake surface curvature map", default=True)
    bake_world_normal: BoolProperty(name="World Normal", description="Bake object-space (world) normal map", default=True)
    bake_id: BoolProperty(name="Object ID", description="Bake per-object color ID map", default=True)
    bake_thickness: BoolProperty(name="Thickness", description="Bake mesh thickness map", default=False)
    bake_opacity: BoolProperty(name="Opacity", description="Bake opacity/transparency map", default=False)
    bake_height: BoolProperty(name="Height", description="Bake surface height/displacement map", default=False)
    bake_position: BoolProperty(name="Position", description="Bake world-space position map", default=False)

    # ── export options ────────────────────────────────────────────────────
    apply_modifiers: BoolProperty(name="Apply Modifiers", description="Apply all modifiers before exporting", default=True)
    triangulate: BoolProperty(name="Triangulate", description="Triangulate faces before exporting", default=True)
    smooth_by_uv: BoolProperty(name="Smooth by UV", description="Mark edges sharp at UV seam boundaries", default=True)
    export_at_origin: BoolProperty(name="Export at Origin", description="Reset object transforms to world origin before exporting", default=False)
    instance_uv_offset: BoolProperty(
        name="UV Offset",
        description="Shift instanced LP meshes sharing the same source data by +1 U (tile 1002)",
        default=True,
    )


class MG_Project(bpy.types.PropertyGroup):
    """A named project that owns a set of export groups."""

    name: StringProperty(name="Name", default="Project")
    groups: CollectionProperty(type=MG_ExportGroup)
    active_group_index: IntProperty()


class MG_LogLine(bpy.types.PropertyGroup):
    """One log line stored in the export log collection."""

    text: StringProperty(name="Text", default="")
    # INFO | OK | WARN | SECTION
    level: StringProperty(name="Level", default="INFO")


# ── helpers ─────────────────────────────────────────────────────

def _set_collection_hide(col, hide):
    """Recursively set hide_viewport on a collection and all its children."""
    if col is None:
        return
    col.hide_viewport = hide
    for child in col.children:
        _set_collection_hide(child, hide)


def _on_hp_hidden_update(self, context):
    """Scene-level update: hide/show all HP collections across all projects."""
    for proj in self.mg_projects:
        for grp in proj.groups:
            _set_collection_hide(grp.hp_collection, self.mg_hp_hidden)


def _on_lp_hidden_update(self, context):
    """Scene-level update: hide/show all LP collections across all projects."""
    for proj in self.mg_projects:
        for grp in proj.groups:
            _set_collection_hide(grp.lp_collection, self.mg_lp_hidden)

def _group_status(group):
    """Return a status string for the UIList row icon.

    'OK'   – both HP and LP assigned
    'WARN' – one or both of HP/LP missing
    """
    if group.hp_collection is None or group.lp_collection is None:
        return 'WARN'
    return 'OK'


def get_output_name(group):
    """Derive output file base name from the first LP material, fallback to blend name."""
    lp = group.lp_collection
    if lp is not None:
        for obj in lp.objects:
            if obj.type == 'MESH' and obj.data.materials:
                mat = obj.data.materials[0]
                if mat is not None:
                    return mat.name
    import os
    return os.path.splitext(bpy.path.basename(bpy.data.filepath))[0]


def get_active_project(scene):
    """Return the active MG_Project, or None if there are no projects."""
    if not scene.mg_projects:
        return None
    idx = scene.mg_active_project_index
    if 0 <= idx < len(scene.mg_projects):
        return scene.mg_projects[idx]
    return None


def get_project_output_name(project, scene=None):
    """Return the base filename for .tbscene / .spp.

    Single-project mode: uses the blend filename so the output tracks the
    asset rather than a potentially stale project name.
    Multi-project mode: uses project.name to distinguish between outputs.
    """
    if scene is not None and len(scene.mg_projects) > 1:
        return project.name
    import os
    blend_name = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0]
    return blend_name or project.name


# ── Migration ──────────────────────────────────────────────────────────────

_MIGRATE_FIELDS = (
    "name", "include", "cage_offset", "res_x", "res_y",
    "bake_normal", "bake_ao", "bake_curvature", "bake_world_normal",
    "bake_id", "bake_thickness", "bake_position",
    "bake_opacity", "bake_height",
    "apply_modifiers", "triangulate", "smooth_by_uv", "export_at_origin",
    "instance_uv_offset",
)


@bpy.app.handlers.persistent
def _migrate_legacy_groups(dummy):
    """Auto-migrate old mg_export_groups → mg_projects on file load."""
    for scene in bpy.data.scenes:
        if scene.mg_projects:
            continue  # Already using projects — skip
        if not scene.mg_export_groups:
            continue  # Nothing to migrate
        proj = scene.mg_projects.add()
        proj.name = "Project"
        for old_g in scene.mg_export_groups:
            new_g = proj.groups.add()
            for field in _MIGRATE_FIELDS:
                setattr(new_g, field, getattr(old_g, field))
            new_g.hp_collection = old_g.hp_collection
            new_g.lp_collection = old_g.lp_collection
        scene.mg_export_groups.clear()
        print(
            f"[mgBaker] Migrated {len(proj.groups)} group(s) to project "
            f"'{proj.name}' in scene '{scene.name}'"
        )


# ── registration ──────────────────────────────────────────────────────────

def register():
    # Legacy — kept for migration only; not shown in UI
    bpy.types.Scene.mg_export_groups = CollectionProperty(type=MG_ExportGroup)
    bpy.types.Scene.mg_active_group_index = IntProperty()
    # Projects
    bpy.types.Scene.mg_projects = CollectionProperty(type=MG_Project)
    bpy.types.Scene.mg_active_project_index = IntProperty()
    # Log
    bpy.types.Scene.mg_export_log = CollectionProperty(type=MG_LogLine)
    bpy.types.Scene.mg_export_log_index = IntProperty()
    # Visibility toggles
    bpy.types.Scene.mg_hp_hidden = BoolProperty(
        name="Hide HP",
        description="Hide all HP collections in the viewport",
        default=False,
        update=_on_hp_hidden_update,
    )
    bpy.types.Scene.mg_lp_hidden = BoolProperty(
        name="Hide LP",
        description="Hide all LP collections in the viewport",
        default=False,
        update=_on_lp_hidden_update,
    )
    bpy.app.handlers.load_post.append(_migrate_legacy_groups)


def unregister():
    if _migrate_legacy_groups in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_migrate_legacy_groups)
    del bpy.types.Scene.mg_lp_hidden
    del bpy.types.Scene.mg_hp_hidden
    del bpy.types.Scene.mg_export_log_index
    del bpy.types.Scene.mg_export_log
    del bpy.types.Scene.mg_active_project_index
    del bpy.types.Scene.mg_projects
    del bpy.types.Scene.mg_active_group_index
    del bpy.types.Scene.mg_export_groups
