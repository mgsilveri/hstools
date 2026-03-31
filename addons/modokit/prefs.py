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
    enable_uv_overlap: BoolProperty(
        name="UV Overlap Highlight",
        description=(
            "Shade all UV faces with a thin red layer in the UV Editor. "
            "Overlapping UV islands stack layers — the intersection zone "
            "saturates toward red while unique areas stay nearly invisible"
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

    def _toggle_perf(self, context):
        from .utils import _perf_enabled, perf_reset
        import modokit.utils as _u
        _u._perf_enabled = self.debug_perf
        if not self.debug_perf:
            perf_reset()   # discard partial data when turning off

    debug_perf: BoolProperty(
        name="Performance Timing",
        description=(
            "Record per-call timing for the hot paths: back-edge cache rebuild, "
            "edge/vert/face draw callbacks.  Use 'Print & Reset' to dump a report "
            "to the system console showing call counts, average, max, and total "
            "time per label.  Zero overhead when disabled."
        ),
        default=False,
        update=_toggle_perf,
    )

    show_debug: BoolProperty(
        name="Show Debug Options",
        description="Expand the developer / debug section",
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

    move_and_sew_key: EnumProperty(
        name="Key",
        description="Keyboard key that triggers Move and Sew in the UV Editor",
        items=[
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
        default='S',
        update=_refresh_keymaps,
    )
    move_and_sew_shift: BoolProperty(
        name="Shift",
        description="Require Shift modifier for Move and Sew",
        default=True,
        update=_refresh_keymaps,
    )
    move_and_sew_ctrl: BoolProperty(
        name="Ctrl",
        description="Require Ctrl modifier for Move and Sew",
        default=False,
        update=_refresh_keymaps,
    )
    move_and_sew_alt: BoolProperty(
        name="Alt",
        description="Require Alt modifier for Move and Sew",
        default=False,
        update=_refresh_keymaps,
    )

    uv_rip_key: EnumProperty(
        name="Key",
        description="Keyboard key that triggers UV Rip (Unstitch) in the UV Editor",
        items=[
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
        default='V',
        update=_refresh_keymaps,
    )
    uv_rip_shift: BoolProperty(
        name="Shift",
        description="Require Shift modifier for UV Rip",
        default=False,
        update=_refresh_keymaps,
    )
    uv_rip_ctrl: BoolProperty(
        name="Ctrl",
        description="Require Ctrl modifier for UV Rip",
        default=False,
        update=_refresh_keymaps,
    )
    uv_rip_alt: BoolProperty(
        name="Alt",
        description="Require Alt modifier for UV Rip",
        default=False,
        update=_refresh_keymaps,
    )

    def draw(self, context):
        layout = self.layout

        # ── Edit Mode ─────────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Edit Mode", icon='EDITMODE_HLT')
        col = box.column(align=True)

        col.prop(self, "enable_mouse_selection")
        if self.enable_mouse_selection:
            sub = col.column(align=True)
            sub.use_property_split = True
            sub.separator(factor=0.5)
            sub.prop(self, "selection_tolerance")
            sub.prop(self, "paint_selection_size")
            sub.prop(self, "double_click_time")

        col.separator()
        col.prop(self, "enable_lasso_selection")

        col.separator()
        col.prop(self, "enable_backface_viz")
        if self.enable_backface_viz:
            sub = col.column(align=True)
            sub.use_property_split = True
            sub.separator(factor=0.5)
            sub.prop(self, "backwire_opacity")

        col.separator()
        col.prop(self, "enable_component_mode")

        # ── Viewport ───────────────────────────────────────────────────────────
        layout.separator(factor=0.5)
        box = layout.box()
        box.label(text="Viewport", icon='VIEW3D')
        col = box.column(align=True)
        col.prop(self, "enable_preselect_highlight")
        if self.enable_preselect_highlight:
            sub = col.column(align=True)
            sub.use_property_split = True
            sub.separator(factor=0.5)
            row = sub.row(align=True)
            row.prop(self, "preselect_color", text="Color")
            row.prop(self, "preselect_alpha", text="Opacity")

        # ── Object Mode ───────────────────────────────────────────────────────
        layout.separator(factor=0.5)
        box = layout.box()
        box.label(text="Object Mode", icon='OBJECT_DATA')
        col = box.column(align=True)
        col.prop(self, "enable_object_mode_selection")

        # ── UV Editor ─────────────────────────────────────────────────────────
        layout.separator(factor=0.5)
        box = layout.box()
        box.label(text="UV Editor", icon='UV')
        col = box.column(align=True)

        col.prop(self, "enable_uv_handle_snap")
        if self.enable_uv_handle_snap:
            sub = col.column(align=True)
            sub.use_property_split = True
            sub.separator(factor=0.5)
            sub.prop(self, "uv_scale_sensitivity")

        col.separator()
        col.prop(self, "enable_uv_boundary_overlay")
        col.prop(self, "enable_uv_flipped_face_viz")

        # ── Miscellaneous ─────────────────────────────────────────────────────
        layout.separator(factor=0.5)
        box = layout.box()
        box.label(text="Miscellaneous", icon='SETTINGS')
        box.prop(self, "enable_instance_tagging")

        # ── Hotkeys ───────────────────────────────────────────────────────────
        layout.separator(factor=0.5)
        box = layout.box()
        box.label(text="Hotkeys", icon='KEYINGSET')
        col = box.column(align=True)
        col.label(text="Shortest Path (Select Between):")
        row = col.row(align=True)
        row.prop(self, "shortest_path_key", text="")
        row.prop(self, "shortest_path_shift", toggle=True, text="Shift")
        row.prop(self, "shortest_path_ctrl",  toggle=True, text="Ctrl")
        row.prop(self, "shortest_path_alt",   toggle=True, text="Alt")

        col.separator()
        col.label(text="Move and Sew (UV Editor):")
        row = col.row(align=True)
        row.prop(self, "move_and_sew_key", text="")
        row.prop(self, "move_and_sew_shift", toggle=True, text="Shift")
        row.prop(self, "move_and_sew_ctrl",  toggle=True, text="Ctrl")
        row.prop(self, "move_and_sew_alt",   toggle=True, text="Alt")

        col.separator()
        col.label(text="Rip / Unstitch (UV Editor):")
        row = col.row(align=True)
        row.prop(self, "uv_rip_key", text="")
        row.prop(self, "uv_rip_shift", toggle=True, text="Shift")
        row.prop(self, "uv_rip_ctrl",  toggle=True, text="Ctrl")
        row.prop(self, "uv_rip_alt",   toggle=True, text="Alt")

        # ── Developer / Debug ─────────────────────────────────────────────────
        layout.separator(factor=0.5)
        box = layout.box()
        row = box.row()
        row.prop(
            self, "show_debug",
            icon='TRIA_DOWN' if self.show_debug else 'TRIA_RIGHT',
            icon_only=True, emboss=False,
        )
        row.label(text="Developer / Debug")
        if self.show_debug:
            col = box.column(align=True)
            col.prop(self, "debug_raycast")
            col.prop(self, "debug_selection")
            col.prop(self, "debug_uv_seam")
            col.prop(self, "debug_uv_handle")
            col.separator(factor=0.5)
            col.prop(self, "debug_perf")
            if self.debug_perf:
                col.operator("modokit.perf_report", icon='CONSOLE')
            if self.debug_raycast or self.debug_selection or self.debug_uv_seam or self.debug_uv_handle or self.debug_perf:
                col.label(text="Open: Window > Toggle System Console", icon='CONSOLE')


class MODOKIT_OT_perf_report(bpy.types.Operator):
    """Print the modokit performance timing report to the system console and reset counters"""
    bl_idname = "modokit.perf_report"
    bl_label  = "Print & Reset Perf Report"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        from .utils import perf_report, _PERF_LOG_PATH
        perf_report()
        self.report({'INFO'}, f"modokit: perf report written to {_PERF_LOG_PATH}")
        return {'FINISHED'}
