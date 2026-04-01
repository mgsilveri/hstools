"""
mgBaker – Addon preferences.
"""

import bpy
from bpy.props import StringProperty, BoolProperty


class MG_BakerPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    toolbag_exe: StringProperty(
        name="Toolbag Executable",
        description="Path to Marmoset Toolbag 5 executable",
        default=r"C:\Program Files\Marmoset\Toolbag 5\toolbag.exe",
        subtype='FILE_PATH',
    )

    painter_exe: StringProperty(
        name="Painter Executable",
        description="Path to Adobe Substance 3D Painter executable",
        default=r"C:\Program Files\Adobe\Adobe Substance 3D Painter\Adobe Substance 3D Painter.exe",
        subtype='FILE_PATH',
    )

    spp_template: StringProperty(
        name=".spp Template",
        description="Override path to .spp template. Leave blank to use the bundled default",
        default="",
        subtype='FILE_PATH',
    )

    bakes_subfolder: StringProperty(
        name="Bakes Subfolder",
        description="Subfolder name for bake outputs (relative to .blend dir)",
        default="bakes",
    )

    p4_auto_checkout: BoolProperty(
        name="P4 Auto-checkout",
        description="Automatically checkout files via Perforce before writing",
        default=True,
    )

    launch_app_after_export: BoolProperty(
        name="Launch App After Export",
        description="Automatically launch Toolbag / Painter after exporting",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "toolbag_exe")
        layout.prop(self, "painter_exe")
        layout.prop(self, "spp_template")
        layout.prop(self, "bakes_subfolder")
        layout.separator()
        layout.prop(self, "p4_auto_checkout")
        layout.prop(self, "launch_app_after_export")
