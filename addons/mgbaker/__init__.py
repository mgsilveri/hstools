"""
mgBaker – Toolbag 5 / Substance Painter bake pipeline addon.
"""

bl_info = {
    "name": "mgBaker",
    "author": "Hector Silveri",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "3D Viewport > N-Panel > mgBaker",
    "description": "Toolbag 5 and Substance Painter bake pipeline",
    "category": "Pipeline",
}

import bpy

from . import (
    prefs,
    baker_props,
    baker_ops,
    export_ops,
    baker_panel,
)


# ============================================================================
# All classes, in registration order (dependencies first)
# ============================================================================

_ALL_CLASSES = (
    # Preferences
    prefs.MG_BakerPreferences,
    # PropertyGroup
    baker_props.MG_ExportGroup,
    # UIList
    baker_ops.MG_UL_ExportGroups,
    # Group CRUD operators
    baker_ops.MG_OT_AddGroup,
    baker_ops.MG_OT_RemoveGroup,
    baker_ops.MG_OT_MoveGroup,
    # Outliner assign operators
    baker_ops.MG_OT_AssignCollectionHP,
    baker_ops.MG_OT_AssignCollectionLP,
    baker_ops.MG_OT_ClearCollectionAssignment,
    # Install Painter plugin
    baker_ops.MG_OT_InstallPainterPlugin,
    # Export operators
    export_ops.MG_OT_ExportToToolbag,
    export_ops.MG_OT_ExportToPainter,
    export_ops.MG_OT_ExportFBXOnly,
    export_ops.MG_OT_OpenBakesFolder,
    # Panels (parent first, then children)
    baker_panel.MG_PT_Baker,
    baker_panel.MG_PT_ExportGroups,
    baker_panel.MG_PT_GroupSettings,
    baker_panel.MG_PT_Export,
    baker_panel.MG_PT_PanelPrefs,
    baker_panel.MG_PT_Log,
)


# ============================================================================
# register / unregister
# ============================================================================

def register():
    for cls in _ALL_CLASSES:
        bpy.utils.register_class(cls)

    baker_props.register()

    # Outliner right-click menu injection
    bpy.types.OUTLINER_MT_collection.append(baker_ops._draw_outliner_collection_menu)


def unregister():
    bpy.types.OUTLINER_MT_collection.remove(baker_ops._draw_outliner_collection_menu)

    baker_props.unregister()

    for cls in reversed(_ALL_CLASSES):
        bpy.utils.unregister_class(cls)
