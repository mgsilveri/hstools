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
    include: BoolProperty(name="Include", default=True)

    # ── collections ───────────────────────────────────────────────────────
    hp_collection: PointerProperty(
        name="HP Collection",
        type=bpy.types.Collection,
    )
    lp_collection: PointerProperty(
        name="LP Collection",
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
        items=_RES_ITEMS,
        default='2048',
    )
    res_y: EnumProperty(
        name="Resolution Y",
        items=_RES_ITEMS,
        default='2048',
    )

    # ── bake maps ─────────────────────────────────────────────────────────
    bake_normal: BoolProperty(name="Normal", default=True)
    bake_ao: BoolProperty(name="AO", default=True)
    bake_curvature: BoolProperty(name="Curvature", default=True)
    bake_world_normal: BoolProperty(name="World Normal", default=True)
    bake_id: BoolProperty(name="ID / Albedo", default=True)
    bake_thickness: BoolProperty(name="Thickness", default=False)
    bake_position: BoolProperty(name="Position", default=False)
    bake_uv_islands: BoolProperty(name="UV Islands", default=False)
    bake_opacity: BoolProperty(name="Opacity", default=False)
    bake_height: BoolProperty(name="Height", default=False)

    # ── export options ────────────────────────────────────────────────────
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=True)
    triangulate: BoolProperty(name="Triangulate", default=True)
    smooth_by_uv: BoolProperty(name="Smooth by UV", default=True)
    export_at_origin: BoolProperty(name="Export at Origin", default=False)


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
    """Scene-level update: hide/show all HP collections across every group."""
    for grp in self.mg_export_groups:
        _set_collection_hide(grp.hp_collection, self.mg_hp_hidden)


def _on_lp_hidden_update(self, context):
    """Scene-level update: hide/show all LP collections across every group."""
    for grp in self.mg_export_groups:
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


# ── registration ──────────────────────────────────────────────────────────

def register():
    bpy.types.Scene.mg_export_groups = CollectionProperty(type=MG_ExportGroup)
    bpy.types.Scene.mg_active_group_index = IntProperty()
    bpy.types.Scene.mg_export_log = CollectionProperty(type=MG_LogLine)
    bpy.types.Scene.mg_export_log_index = IntProperty()
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


def unregister():
    del bpy.types.Scene.mg_lp_hidden
    del bpy.types.Scene.mg_hp_hidden
    del bpy.types.Scene.mg_export_log_index
    del bpy.types.Scene.mg_export_log
    del bpy.types.Scene.mg_active_group_index
    del bpy.types.Scene.mg_export_groups
