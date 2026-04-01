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

    # ── export options ────────────────────────────────────────────────────
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=True)
    triangulate: BoolProperty(name="Triangulate", default=True)
    smooth_by_angle: BoolProperty(name="Smooth by Angle", default=True)
    export_at_origin: BoolProperty(name="Export at Origin", default=False)


# ── helpers ───────────────────────────────────────────────────────────────

def _group_status(group):
    """Return a status string for the UIList row icon.

    'OK'    – both HP and LP assigned and valid
    'WARN'  – one of HP/LP missing
    'ERROR' – a pointer is set but the collection was deleted
    """
    hp = group.hp_collection
    lp = group.lp_collection

    # Broken pointer: Blender keeps the PointerProperty id but the data is gone
    hp_name = getattr(hp, 'name', None) if hp else None
    lp_name = getattr(lp, 'name', None) if lp else None

    hp_broken = hp is not None and hp_name is None
    lp_broken = lp is not None and lp_name is None

    if hp_broken or lp_broken:
        return 'ERROR'
    if hp is None or lp is None:
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
    bpy.types.Scene.mg_export_log = StringProperty(default="")


def unregister():
    del bpy.types.Scene.mg_export_log
    del bpy.types.Scene.mg_active_group_index
    del bpy.types.Scene.mg_export_groups
