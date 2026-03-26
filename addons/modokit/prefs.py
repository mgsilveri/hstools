"""
Addon preferences for Modo-Style Mouse Selection.
"""

import bpy
from bpy.props import IntProperty, EnumProperty, FloatProperty, BoolProperty, FloatVectorProperty

# bl_idname must match the addon's package/module name as Blender registers it.
# When loaded via script directory the key is the folder name: 'modokit'.
_ADDON_NAME = 'modokit'


class ModoSelectionPreferences(bpy.types.AddonPreferences):
    bl_idname = _ADDON_NAME

    selection_tolerance: IntProperty(
        name="Selection Hit Size (pixels)",
        description="Pixel radius for proximity-based 'lazy' selection. Modo default: remapping.selectionSize",
        default=4,
        min=1,
        max=50,
    )

    paint_selection_size: IntProperty(
        name="Paint Selection Size (pixels)",
        description="Brush radius in pixels for paint (drag) selection",
        default=50,
        min=1,
        max=200,
    )

    double_click_time: FloatProperty(
        name="Double-Click Time (seconds)",
        description="Maximum time between clicks to register as double-click",
        default=0.3,
        min=0.1,
        max=1.0,
    )

    backwire_opacity: FloatProperty(
        name="Back Element Opacity",
        description=(
            "Opacity of back-facing edges drawn through the solid mesh surface in Edit Mode. "
            "Matches Modo's dimmed back-element look. "
            "0 = invisible, 1 = same brightness as front edges"
        ),
        default=0.35,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
    )

    # ── Module toggles ───────────────────────────────────────────────────────
    # update= callback re-registers keymaps immediately when a toggle changes.
    def _refresh_keymaps(self, context):
        from . import keymap
        keymap._schedule_deferred_keymap_setup()

    enable_mouse_selection: BoolProperty(
        name="Mouse Selection (Edit Mode)",
        description=(
            "Click, double-click loop/island, shortest path, and context menu in Edit Mode. "
            "Disabling this removes all Edit Mode click and loop selection."
        ),
        default=True,
        update=_refresh_keymaps,
    )
    enable_lasso_selection: BoolProperty(
        name="Lasso Selection",
        description="RMB / MMB drag lasso selection in Edit Mode and Object Mode",
        default=True,
        update=_refresh_keymaps,
    )
    enable_backface_viz: BoolProperty(
        name="Backface Visualization",
        description="Ghost selected edges through the solid mesh surface when entering Edit Mode",
        default=True,
    )
    enable_component_mode: BoolProperty(
        name="Component Mode & Transforms  (1/2/3/4/5 · W/E/R)",
        description=(
            "Mode switching (1/2/3/5), independent selection memory, W/E/R gizmos, "
            "MMB screen-space move, snap highlight, and boundary select (Ctrl+2)"
        ),
        default=True,
        update=_refresh_keymaps,
    )
    enable_object_mode_selection: BoolProperty(
        name="Object Mode Selection",
        description="Modo-style click and paint selection in Object Mode (double-click enters Edit Mode)",
        default=True,
        update=_refresh_keymaps,
    )
    enable_uv_handle_snap: BoolProperty(
        name="UV Editor Handle Snapping",
        description=(
            "Modo-style W/E/R handle repositioning and snap highlight "
            "in the UV Editor (2D, X/Y only)"
        ),
        default=True,
        update=_refresh_keymaps,
    )
    enable_uv_boundary_overlay: BoolProperty(
        name="UV Seam Partner Highlight",
        description=(
            "When a UV edge is selected (orange), highlight its seam partner — "
            "the same geometric edge on the adjacent UV island — in purple, "
            "matching Modo's UV editor visual style"
        ),
        default=True,
    )
    enable_uv_flipped_face_viz: BoolProperty(
        name="UV Flipped Face Highlight",
        description=(
            "Shade UV faces with negative winding order (flipped) in Modo's "
            "gold/olive colour in the UV Editor"
        ),
        default=True,
    )
    enable_instance_tagging: BoolProperty(
        name="Instance Auto-Tagging",
        description=(
            "Prefix linked duplicates with 'inst_' and collect them in an "
            "Instances collection in the Outliner"
        ),
        default=True,
    )
    enable_preselect_highlight: BoolProperty(
        name="Pre-selection Highlight",
        description=(
            "Highlight geometry under the mouse before clicking, "
            "mirroring Modo's pre-selection system. Always on in both "
            "Object Mode and Edit Mode."
        ),
        default=True,
        update=_refresh_keymaps,
    )
    preselect_color: FloatVectorProperty(
        name="Pre-selection Color",
        description="Color used to highlight hovered geometry (Modo default: #c4dbe5)",
        subtype='COLOR',
        size=3,
        default=(0.549, 0.710, 0.780),
        min=0.0,
        max=1.0,
    )
    preselect_alpha: FloatProperty(
        name="Pre-selection Opacity",
        description="Opacity of the pre-selection highlight overlay",
        default=0.75,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
    )

    uv_scale_sensitivity: FloatProperty(
        name="UV Scale Sensitivity",
        description=(
            "Dampens the R (Scale) tool in the UV Editor. "
            "1.0 = full speed (original behaviour); "
            "lower values make the scale change slower and easier to control"
        ),
        default=0.5,
        min=0.05,
        max=1.0,
        step=5,
        precision=2,
        subtype='FACTOR',
    )

    debug_raycast: BoolProperty(
        name="Debug Raycast",
        description=(
            "Print detailed raycast info to the system console on every click. "
            "Open the console via Window > Toggle System Console, then click in Edit Mode."
        ),
        default=False,
    )

    debug_selection: BoolProperty(
        name="Debug Keymap",
        description=(
            "Print [Modo-Style Selection] keymap diagnostic messages to the system console "
            "(keymap registration, conflict reports). "
            "Open the console via Window > Toggle System Console."
        ),
        default=False,
    )

    debug_uv_seam: BoolProperty(
        name="Debug UV Seam Partner Overlay",
        description=(
            "Print per-frame diagnostics for the purple seam-partner overlay to the "
            "system console: which attribute path is used for edge/vert selection, "
            "raw flag values, and how many partner segments/verts are found. "
            "Open the console via Window > Toggle System Console."
        ),
        default=False,
    )

    debug_uv_handle: BoolProperty(
        name="Debug UV Handle (Crash Tracing)",
        description=(
            "Print a timestamped trace of every UV handle event to the system "
            "console: invoke, modal events, _apply_uvs writes, drop_passthrough. "
            "stdout is flushed after every print so output survives a hard crash. "
            "Enable this, open Window > Toggle System Console, reproduce the "
            "crash, and read the last lines printed before Blender died."
        ),
        default=False,
    )

    # ── Hotkeys ───────────────────────────────────────────────────────────────
    shortest_path_key: EnumProperty(
        name="Key",
        description="Mouse button or keyboard key that triggers Shortest Path (Select Between)",
        items=[
            ('RIGHTMOUSE',  "Right Mouse",   "Right mouse button (handled inside lasso modal)"),
            ('MIDDLEMOUSE', "Middle Mouse",  "Middle mouse button (handled inside lasso modal)"),
            ('A', "A", ""), ('B', "B", ""), ('C', "C", ""),
            ('D', "D", ""), ('E', "E", ""), ('F', "F", ""),
            ('G', "G", ""), ('H', "H", ""), ('I', "I", ""),
            ('J', "J", ""), ('K', "K", ""), ('L', "L", ""),
            ('M', "M", ""), ('N', "N", ""), ('O', "O", ""),
            ('P', "P", ""), ('Q', "Q", ""), ('R', "R", ""),
            ('S', "S", ""), ('T', "T", ""), ('U', "U", ""),
            ('V', "V", ""), ('W', "W", ""), ('X', "X", ""),
            ('Y', "Y", ""), ('Z', "Z", ""),
            ('F1',  "F1",  ""), ('F2',  "F2",  ""), ('F3',  "F3",  ""),
            ('F4',  "F4",  ""), ('F5',  "F5",  ""), ('F6',  "F6",  ""),
            ('F7',  "F7",  ""), ('F8',  "F8",  ""), ('F9',  "F9",  ""),
            ('F10', "F10", ""), ('F11', "F11", ""), ('F12', "F12", ""),
            ('SEMI_COLON', ";", ""), ('COMMA', ",", ""), ('PERIOD', ".", ""),
            ('SLASH', "/", ""), ('BACK_SLASH', "\\", ""),
            ('LEFT_BRACKET', "[", ""), ('RIGHT_BRACKET', "]", ""),
            ('ACCENT_GRAVE', "`", ""), ('QUOTE', "'", ""),
            ('MINUS', "-", ""), ('EQUAL', "=", ""),
        ],
        default='RIGHTMOUSE',
        update=_refresh_keymaps,
    )
    shortest_path_shift: BoolProperty(
        name="Shift",
        description="Require Shift modifier for Shortest Path",
        default=True,
        update=_refresh_keymaps,
    )
    shortest_path_ctrl: BoolProperty(
        name="Ctrl",
        description="Require Ctrl modifier for Shortest Path",
        default=False,
        update=_refresh_keymaps,
    )
    shortest_path_alt: BoolProperty(
        name="Alt",
        description="Require Alt modifier for Shortest Path",
        default=False,
        update=_refresh_keymaps,
    )

    def draw(self, context):
        layout = self.layout

        # ── Modules ───────────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Modules", icon='MODIFIER')
        col = box.column(align=True)
        col.prop(self, "enable_mouse_selection")
        col.separator()
        col.prop(self, "enable_lasso_selection")
        col.prop(self, "enable_backface_viz")
        col.separator()
        col.prop(self, "enable_component_mode")
        col.separator()
        col.prop(self, "enable_object_mode_selection")
        col.separator()
        col.prop(self, "enable_uv_handle_snap")
        col.prop(self, "enable_uv_boundary_overlay")
        col.prop(self, "enable_uv_flipped_face_viz")
        col.separator()
        col.prop(self, "enable_instance_tagging")
        col.separator()
        col.prop(self, "enable_preselect_highlight")

        # ── Settings ──────────────────────────────────────────────────────────
        layout.separator()
        box = layout.box()
        box.label(text="Settings", icon='PREFERENCES')
        box.prop(self, "selection_tolerance")
        box.prop(self, "double_click_time")
        box.prop(self, "backwire_opacity")
        box.prop(self, "uv_scale_sensitivity")
        if self.enable_preselect_highlight:
            row = box.row(align=True)
            row.prop(self, "preselect_color", text="Pre-selection Color")
            row.prop(self, "preselect_alpha", text="Opacity")

        # ── Hotkeys ───────────────────────────────────────────────────────────
        layout.separator()
        box = layout.box()
        box.label(text="Hotkeys", icon='KEYINGSET')
        col = box.column(align=True)
        col.label(text="Shortest Path (Select Between):")
        row = col.row(align=True)
        row.prop(self, "shortest_path_key", text="")
        row.prop(self, "shortest_path_shift", toggle=True, text="Shift")
        row.prop(self, "shortest_path_ctrl",  toggle=True, text="Ctrl")
        row.prop(self, "shortest_path_alt",   toggle=True, text="Alt")
        _mouse_keys = {'RIGHTMOUSE', 'MIDDLEMOUSE'}
        if self.shortest_path_key not in _mouse_keys:
            col.label(
                text="Keyboard keys are registered as direct bindings in Edit Mode.",
                icon='INFO',
            )
        else:
            col.label(
                text="Mouse buttons: click = Shortest Path, drag = Lasso.",
                icon='INFO',
            )

        # ── Debugging ─────────────────────────────────────────────────────────
        layout.separator()
        box = layout.box()
        box.label(text="Debugging", icon='INFO')
        box.prop(self, "debug_raycast")
        box.prop(self, "debug_selection")
        box.prop(self, "debug_uv_seam")
        box.prop(self, "debug_uv_handle")
        if self.debug_raycast or self.debug_selection or self.debug_uv_seam or self.debug_uv_handle:
            box.label(text="Open: Window > Toggle System Console", icon='CONSOLE')
