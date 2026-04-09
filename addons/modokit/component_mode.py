"""
Component mode switching and related operators:
  VIEW3D_OT_modo_component_mode  (1/2/3/5 keys)
  MESH_OT_modo_boundary_select   (Ctrl+2)
  MESH_OT_modo_material_mode     (4 key)
Also contains the header patch for the Material Mode button.
"""

import bpy
import bmesh
from bpy.props import EnumProperty, BoolProperty

from . import state
from .utils import _get_prefs


# ── Module-local aliases into state for write access ─────────────────────────
# (Reading state vars works via state.x; writing via state.x = val)

_COMPONENT_MODE_MAP = {
    'VERT': (True,  False, False),
    'EDGE': (False, True,  False),
    'FACE': (False, False, True),
}


class VIEW3D_OT_modo_component_mode(bpy.types.Operator):
    """Modo-style 1/2/3/5 component-mode switching.

    Works in both Object Mode and Edit Mode:
      Object Mode  1/2/3 → enter Edit Mode with the right sub-mode pre-selected
      Edit Mode    1/2/3 → switch sub-mode (vertex/edge/face) in-place
      Edit Mode    5     → return to Object Mode
    """
    bl_idname = 'view3d.modo_component_mode'
    bl_label  = 'Modo Component Mode'
    bl_options = {'REGISTER', 'UNDO'}

    component: EnumProperty(
        name="Component",
        items=[
            ('VERT',   'Vertex',      ''),
            ('EDGE',   'Edge',        ''),
            ('FACE',   'Face',        ''),
            ('OBJECT', 'Object Mode', ''),
        ],
        default='VERT',
    )

    convert: BoolProperty(
        name="Convert Selection",
        description="Convert current selection to the target component type (Modo Alt+mode button)",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        if context.active_object is None:
            return False
        if context.active_object.type != 'MESH':
            return False
        return context.mode in ('OBJECT', 'EDIT_MESH')

    def execute(self, context):
        from .transform_3d import _drop_transform
        _drop_transform(context)
        _was_material = state._material_mode_active
        state._material_mode_active = False

        if self.component == 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        elif context.mode == 'OBJECT':
            context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP[self.component]
            bpy.ops.object.mode_set(mode='EDIT')
        else:  # EDIT_MESH
            if self.convert:
                obj = context.edit_object
                bm  = bmesh.from_edit_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()

                cur = context.tool_settings.mesh_select_mode
                tgt = self.component

                if cur[2] and tgt == 'EDGE':
                    src_sel = {f.index for f in bm.faces if f.select}
                    sel = {e.index for f in bm.faces if f.select for e in f.edges}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['EDGE']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.edges.ensure_lookup_table()
                    bm.verts.ensure_lookup_table()
                    for e in bm.edges:
                        e.select = e.index in sel
                    for v in bm.verts:
                        v.select = any(e.select for e in v.link_edges)
                    bmesh.update_edit_mesh(obj.data)
                    # Save source-mode memory (we're leaving it) but leave target
                    # memory untouched — convert is a preview, not a memory commit.
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['FACE'] = src_sel
                    state._last_switch_was_convert = True
                    return {'FINISHED'}

                elif cur[2] and tgt == 'VERT':
                    src_sel = {f.index for f in bm.faces if f.select}
                    sel = {v.index for f in bm.faces if f.select for v in f.verts}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['VERT']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.verts.ensure_lookup_table()
                    for v in bm.verts:
                        v.select = v.index in sel
                    bmesh.update_edit_mesh(obj.data)
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['FACE'] = src_sel
                    mem['VERT'] = sel
                    return {'FINISHED'}

                elif cur[1] and tgt == 'VERT':
                    src_sel = {e.index for e in bm.edges if e.select}
                    sel = {v.index for e in bm.edges if e.select for v in e.verts}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['VERT']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.verts.ensure_lookup_table()
                    for v in bm.verts:
                        v.select = v.index in sel
                    bmesh.update_edit_mesh(obj.data)
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['EDGE'] = src_sel
                    mem['VERT'] = sel
                    return {'FINISHED'}

                elif cur[1] and tgt == 'FACE':
                    src_sel = {e.index for e in bm.edges if e.select}
                    sel = {f.index for f in bm.faces
                           if sum(1 for e in f.edges if e.select) >= 2}
                    if not sel:
                        sel = {f.index for f in bm.faces if any(e.select for e in f.edges)}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['FACE']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.faces.ensure_lookup_table()
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    for f in bm.faces:
                        f.select = f.index in sel
                    for f in bm.faces:
                        if f.select:
                            for v in f.verts: v.select = True
                            for e in f.edges: e.select = True
                    bmesh.update_edit_mesh(obj.data)
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['EDGE'] = src_sel
                    mem['FACE'] = sel
                    return {'FINISHED'}

                elif cur[0] and tgt == 'EDGE':
                    src_sel = {v.index for v in bm.verts if v.select}
                    sel = {e.index for e in bm.edges if all(v.select for v in e.verts)}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['EDGE']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.edges.ensure_lookup_table()
                    bm.verts.ensure_lookup_table()
                    for e in bm.edges:
                        e.select = e.index in sel
                    for v in bm.verts:
                        v.select = any(e.select for e in v.link_edges)
                    bmesh.update_edit_mesh(obj.data)
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['VERT'] = src_sel
                    mem['EDGE'] = sel
                    return {'FINISHED'}

                elif cur[0] and tgt == 'FACE':
                    src_sel = {v.index for v in bm.verts if v.select}
                    sel = {f.index for f in bm.faces if all(v.select for v in f.verts)}
                    bpy.ops.mesh.select_all(action='DESELECT')
                    context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP['FACE']
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.faces.ensure_lookup_table()
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    for f in bm.faces:
                        f.select = f.index in sel
                    for f in bm.faces:
                        if f.select:
                            for e in f.edges: e.select = True
                            for v in f.verts: v.select = True
                    bmesh.update_edit_mesh(obj.data)
                    mem = state._selection_memory.setdefault(
                        obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()})
                    mem['VERT'] = src_sel
                    mem['FACE'] = sel
                    return {'FINISHED'}

                else:
                    bpy.ops.mesh.select_mode(use_extend=False, use_expand=False,
                                             type=tgt)
                    return {'FINISHED'}

            else:
                # Independent mode switching — each sub-mode keeps its own selection.
                cur_v, cur_e, cur_f = context.tool_settings.mesh_select_mode
                cur_key = 'FACE' if cur_f else ('EDGE' if cur_e else 'VERT')
                tgt_key = self.component

                for obj in context.objects_in_mode_unique_data:
                    if obj.type != 'MESH':
                        continue
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    bm.faces.ensure_lookup_table()
                    mem = state._selection_memory.setdefault(
                        obj.data.name,
                        {'VERT': set(), 'EDGE': set(), 'FACE': set()}
                    )
                    if not _was_material:
                        if cur_key == 'VERT':
                            mem['VERT'] = {v.index for v in bm.verts if v.select}
                        elif cur_key == 'EDGE':
                            mem['EDGE'] = {e.index for e in bm.edges if e.select}
                        else:
                            mem['FACE'] = {f.index for f in bm.faces if f.select}

                context.tool_settings.mesh_select_mode = _COMPONENT_MODE_MAP[tgt_key]

                for obj in context.objects_in_mode_unique_data:
                    if obj.type != 'MESH':
                        continue
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    bm.faces.ensure_lookup_table()
                    mem   = state._selection_memory.get(obj.data.name, {})
                    saved = mem.get(tgt_key, set())

                    for v in bm.verts: v.select = False
                    for e in bm.edges: e.select = False
                    for f in bm.faces: f.select = False
                    bm.select_history.clear()

                    if tgt_key == 'VERT':
                        for i in saved:
                            if i < len(bm.verts):
                                bm.verts[i].select = True
                    elif tgt_key == 'EDGE':
                        for i in saved:
                            if i < len(bm.edges):
                                e = bm.edges[i]
                                e.select = True
                                for v in e.verts: v.select = True
                    else:
                        for i in saved:
                            if i < len(bm.faces):
                                f = bm.faces[i]
                                f.select = True
                                for v in f.verts: v.select = True
                                for e in f.edges: e.select = True

                    bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}


class MESH_OT_modo_boundary_select(bpy.types.Operator):
    """Select the boundary (perimeter) edges of the current face selection.
    Ctrl+2: replaces selection with boundary edges.
    Shift+Ctrl+2: adds boundary edges to the existing selection.
    """
    bl_idname = 'mesh.modo_boundary_select'
    bl_label  = 'Modo Boundary Select'
    bl_options = {'REGISTER', 'UNDO'}

    additive: BoolProperty(
        name="Additive",
        description="Keep existing selection and add boundary edges (+Bounds)",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        obj = context.edit_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.edit_object
        bm  = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.verts.ensure_lookup_table()

        boundary_edges = [
            e for e in bm.edges
            if sum(1 for f in e.link_faces if f.select) == 1
        ]

        if not boundary_edges:
            self.report({'INFO'}, 'No boundary edges found — select faces first')
            return {'CANCELLED'}

        if not self.additive:
            for f in bm.faces:
                f.select = False
            for e in bm.edges:
                e.select = False
            for v in bm.verts:
                v.select = False

        for edge in boundary_edges:
            edge.select = True
            for v in edge.verts:
                v.select = True

        bmesh.update_edit_mesh(obj.data)
        bpy.ops.mesh.select_mode(use_extend=False, use_expand=False, type='EDGE')
        return {'FINISHED'}


class MESH_OT_modo_material_mode(bpy.types.Operator):
    """Modo-style 4: toggle Materials selection mode.
    Edit Mode: switches to Face sub-mode.  Clicking a face selects all faces
    sharing the same material slot.  Press 4 again, or 1/2/3/5, to leave.
    """
    bl_idname = 'mesh.modo_material_mode'
    bl_label  = 'Modo Material Mode'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        return context.mode in ('OBJECT', 'EDIT_MESH')

    def execute(self, context):
        if state._material_mode_active:
            return {'FINISHED'}
        # Save current component selection before activating material mode
        # so 1/2/3 can restore it when exiting.
        if context.mode == 'EDIT_MESH':
            cur_v, cur_e, cur_f = context.tool_settings.mesh_select_mode
            cur_key = 'FACE' if cur_f else ('EDGE' if cur_e else 'VERT')
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm = bmesh.from_edit_mesh(obj.data)
                mem = state._selection_memory.setdefault(
                    obj.data.name, {'VERT': set(), 'EDGE': set(), 'FACE': set()}
                )
                if cur_key == 'VERT':
                    bm.verts.ensure_lookup_table()
                    mem['VERT'] = {v.index for v in bm.verts if v.select}
                elif cur_key == 'EDGE':
                    bm.edges.ensure_lookup_table()
                    mem['EDGE'] = {e.index for e in bm.edges if e.select}
                else:
                    bm.faces.ensure_lookup_table()
                    mem['FACE'] = {f.index for f in bm.faces if f.select}
        state._material_mode_active = True
        return {'FINISHED'}


# ── Header patches ─────────────────────────────────────────────────────────────

def _patched_view3d_ht_header_draw(self, context):
    """Replacement for VIEW3D_HT_header.draw.

    Identical to the built-in draw except that when Material Mode is active in
    EDIT_MESH we skip template_header_3D_mode() (which always shows the active
    mesh_select_mode as depressed) and instead draw the vertex/edge/face buttons
    manually with depress=False, so none of them appears highlighted.
    """
    import bpy
    layout = self.layout
    tool_settings = context.tool_settings
    view = context.space_data
    shading = view.shading

    layout.row(align=True).template_header()

    row = layout.row(align=True)
    obj = context.active_object
    mode_string = context.mode
    object_mode = 'OBJECT' if obj is None else obj.mode
    has_pose_mode = (
        (object_mode == 'POSE') or
        (object_mode == 'WEIGHT_PAINT' and context.pose_object is not None)
    )

    act_mode_item = bpy.types.Object.bl_rna.properties["mode"].enum_items[object_mode]
    act_mode_i18n_context = bpy.types.Object.bl_rna.properties["mode"].translation_context

    sub = row.row(align=True)
    sub.operator_menu_enum(
        "object.mode_set", "mode",
        text=bpy.app.translations.pgettext_iface(act_mode_item.name, act_mode_i18n_context),
        icon=act_mode_item.icon,
    )
    del act_mode_item

    # Material mode in EDIT_MESH: draw V/E/F buttons with depress=False
    # so none of them appears highlighted (material mode is its own "mode").
    if (state._material_mode_active
            and mode_string == 'EDIT_MESH'
            and obj is not None and obj.type == 'MESH'):
        row2 = layout.row(align=True)
        row2.operator("mesh.select_mode", text="", icon='VERTEXSEL', depress=False).type = 'VERT'
        row2.operator("mesh.select_mode", text="", icon='EDGESEL',   depress=False).type = 'EDGE'
        row2.operator("mesh.select_mode", text="", icon='FACESEL',   depress=False).type = 'FACE'
    else:
        layout.template_header_3D_mode()

    # Mode-specific extra buttons (particle edit, curves domain, grease pencil)
    if obj:
        if object_mode == 'PARTICLE_EDIT':
            row = layout.row()
            row.prop(tool_settings.particle_edit, "select_mode", text="", expand=True)
        elif object_mode in {'EDIT', 'SCULPT_CURVES'} and obj.type == 'CURVES':
            curves = obj.data
            row = layout.row(align=True)
            domain = curves.selection_domain
            row.operator(
                "curves.set_selection_domain",
                text="", icon='CURVE_BEZCIRCLE',
                depress=(domain == 'POINT'),
            ).domain = 'POINT'
            row.operator(
                "curves.set_selection_domain",
                text="", icon='CURVE_PATH',
                depress=(domain == 'CURVE'),
            ).domain = 'CURVE'

    # Grease pencil mode-specific buttons
    if obj and obj.type == 'GREASEPENCIL':
        if object_mode == 'EDIT':
            row = layout.row(align=True)
            row.operator(
                "grease_pencil.set_selection_mode",
                text="", icon='GP_SELECT_POINTS',
                depress=(tool_settings.gpencil_selectmode_edit == 'POINT'),
            ).mode = 'POINT'
            row.operator(
                "grease_pencil.set_selection_mode",
                text="", icon='GP_SELECT_STROKES',
                depress=(tool_settings.gpencil_selectmode_edit == 'STROKE'),
            ).mode = 'STROKE'
            row.operator(
                "grease_pencil.set_selection_mode",
                text="", icon='GP_SELECT_BETWEEN_STROKES',
                depress=(tool_settings.gpencil_selectmode_edit == 'SEGMENT'),
            ).mode = 'SEGMENT'

        if object_mode == 'SCULPT_GREASE_PENCIL':
            row = layout.row(align=True)
            row.prop(tool_settings, "use_gpencil_select_mask_point", text="")
            row.prop(tool_settings, "use_gpencil_select_mask_stroke", text="")
            row.prop(tool_settings, "use_gpencil_select_mask_segment", text="")

        if object_mode == 'VERTEX_GREASE_PENCIL':
            row = layout.row(align=True)
            row.prop(tool_settings, "use_gpencil_vertex_select_mask_point", text="")
            row.prop(tool_settings, "use_gpencil_vertex_select_mask_stroke", text="")
            row.prop(tool_settings, "use_gpencil_vertex_select_mask_segment", text="")

    overlay = view.overlay
    bpy.types.VIEW3D_MT_editor_menus.draw_collapsible(context, layout)

    layout.separator_spacer()

    # Mode-specific right-side buttons (grease pencil / sculpt / paint modes)
    if object_mode in {'PAINT_GREASE_PENCIL', 'SCULPT_GREASE_PENCIL'}:
        if object_mode == 'PAINT_GREASE_PENCIL':
            sub = layout.row(align=True)
            sub.prop_with_popover(
                tool_settings,
                "gpencil_stroke_placement_view3d",
                text="",
                panel="VIEW3D_PT_grease_pencil_origin",
            )

        sub = layout.row(align=True)
        sub.active = tool_settings.gpencil_stroke_placement_view3d != 'SURFACE'
        sub.prop_with_popover(
            tool_settings.gpencil_sculpt,
            "lock_axis",
            text="",
            panel="VIEW3D_PT_grease_pencil_lock",
        )

    elif object_mode == 'SCULPT':
        from bl_ui.space_toolsystem_common import ToolSelectPanelHelper
        tool = ToolSelectPanelHelper.tool_active_from_context(context)
        is_paint_tool = False
        if tool and tool.use_brushes:
            paint = tool_settings.sculpt
            brush = paint.brush
            if brush:
                is_paint_tool = brush.sculpt_brush_type in {'PAINT', 'SMEAR'}
        else:
            is_paint_tool = tool and tool.use_paint_canvas

        from bl_ui.space_view3d import VIEW3D_PT_shading
        shading_obj = VIEW3D_PT_shading.get_shading(context)
        color_type = shading_obj.color_type

        row = layout.row()
        row.active = is_paint_tool and color_type == 'VERTEX'

        if context.preferences.experimental.use_sculpt_texture_paint:
            canvas_source = tool_settings.paint_mode.canvas_source
            icon = 'GROUP_VCOL' if canvas_source == 'COLOR_ATTRIBUTE' else canvas_source
            row.popover(panel="VIEW3D_PT_slots_paint_canvas", icon=icon)
            row.active = is_paint_tool
        else:
            row.popover(panel="VIEW3D_PT_slots_color_attributes", icon='GROUP_VCOL')

        layout.popover(
            panel="VIEW3D_PT_sculpt_snapping",
            icon='SNAP_INCREMENT',
            text="",
            translate=False,
        )
        layout.popover(
            panel="VIEW3D_PT_sculpt_automasking",
            text="",
            icon=bpy.types.VIEW3D_HT_header._mesh_paint_automasking_icon(tool_settings.sculpt),
        )

    elif object_mode == 'VERTEX_PAINT':
        row = layout.row()
        row.popover(panel="VIEW3D_PT_slots_color_attributes", icon='GROUP_VCOL')
    elif object_mode == 'VERTEX_GREASE_PENCIL':
        from bl_ui.space_view3d import draw_topbar_grease_pencil_layer_panel
        draw_topbar_grease_pencil_layer_panel(context, layout)
    elif object_mode == 'WEIGHT_PAINT':
        row = layout.row()
        row.popover(panel="VIEW3D_PT_slots_vertex_groups", icon='GROUP_VERTEX')
        layout.popover(
            panel="VIEW3D_PT_sculpt_snapping",
            icon='SNAP_INCREMENT',
            text="",
            translate=False,
        )
    elif object_mode == 'WEIGHT_GREASE_PENCIL':
        row = layout.row()
        row.popover(panel="VIEW3D_PT_slots_vertex_groups", icon='GROUP_VERTEX')
        from bl_ui.space_view3d import draw_topbar_grease_pencil_layer_panel
        draw_topbar_grease_pencil_layer_panel(context, layout)
    elif object_mode == 'TEXTURE_PAINT':
        tool_mode = tool_settings.image_paint.mode
        icon = 'MATERIAL' if tool_mode == 'MATERIAL' else 'IMAGE_DATA'
        row = layout.row()
        row.popover(panel="VIEW3D_PT_slots_projectpaint", icon=icon)
        row.popover(
            panel="VIEW3D_PT_mask",
            icon=bpy.types.VIEW3D_HT_header._texture_mask_icon(tool_settings.image_paint),
            text="",
        )
    else:
        # Transform settings depending on tool header visibility
        bpy.types.VIEW3D_HT_header.draw_xform_template(layout, context)

    layout.separator_spacer()

    # Viewport Settings
    layout.popover(
        panel="VIEW3D_PT_object_type_visibility",
        icon_value=view.icon_from_show_object_viewport,
        text="",
    )

    # Gizmo toggle & popover
    row = layout.row(align=True)
    row.prop(view, "show_gizmo", text="", toggle=True, icon='GIZMO')
    sub = row.row(align=True)
    sub.active = view.show_gizmo
    sub.popover(panel="VIEW3D_PT_gizmo_display", text="")

    # Overlay toggle & popover
    row = layout.row(align=True)
    row.prop(overlay, "show_overlays", icon='OVERLAY', text="")
    sub = row.row(align=True)
    sub.active = overlay.show_overlays
    sub.popover(panel="VIEW3D_PT_overlay", text="")

    if mode_string == 'EDIT_MESH':
        sub.popover(panel="VIEW3D_PT_overlay_edit_mesh", text="", icon='EDITMODE_HLT')
    elif mode_string == 'EDIT_CURVE':
        sub.popover(panel="VIEW3D_PT_overlay_edit_curve", text="", icon='EDITMODE_HLT')
    elif mode_string == 'EDIT_CURVES':
        sub.popover(panel="VIEW3D_PT_overlay_edit_curves", text="", icon='EDITMODE_HLT')
    elif mode_string == 'SCULPT':
        sub.popover(panel="VIEW3D_PT_overlay_sculpt", text="", icon='SCULPTMODE_HLT')
    elif mode_string == 'SCULPT_CURVES':
        sub.popover(panel="VIEW3D_PT_overlay_sculpt_curves", text="", icon='SCULPTMODE_HLT')
    elif mode_string == 'PAINT_WEIGHT':
        sub.popover(panel="VIEW3D_PT_overlay_weight_paint", text="", icon='WPAINT_HLT')
    elif mode_string == 'PAINT_TEXTURE':
        sub.popover(panel="VIEW3D_PT_overlay_texture_paint", text="", icon='TPAINT_HLT')
    elif mode_string == 'PAINT_VERTEX':
        sub.popover(panel="VIEW3D_PT_overlay_vertex_paint", text="", icon='VPAINT_HLT')
    elif obj is not None and obj.type == 'GREASEPENCIL':
        sub.popover(panel="VIEW3D_PT_overlay_grease_pencil_options", text="",
                    icon='OUTLINER_DATA_GREASEPENCIL')

    # Armature overlay (may co-exist with weight-paint)
    if (has_pose_mode or
            (object_mode in {'EDIT_ARMATURE', 'OBJECT'} and
             _armature_is_wireframe(context))):
        sub.popover(panel="VIEW3D_PT_overlay_bones", text="", icon='POSE_HLT')

    row = layout.row()
    row.active = (object_mode == 'EDIT') or (shading.type in {'WIREFRAME', 'SOLID'})
    from bl_ui.space_view3d import _toggle_xray_operator
    _toggle_xray_operator(row, context, text="")

    row = layout.row(align=True)
    row.prop(shading, "type", text="", expand=True)
    sub = row.row(align=True)
    sub.popover(panel="VIEW3D_PT_shading", text="")


def _armature_is_wireframe(context):
    """Helper used by the header patch to check armature wireframe state."""
    from bl_ui.space_view3d import VIEW3D_PT_overlay_bones
    return VIEW3D_PT_overlay_bones.is_using_wireframe(context)


def _patched_editor_menus_draw_collapsible(cls, context, layout):
    """Replacement for VIEW3D_MT_editor_menus.draw_collapsible.

    Draws the Material Mode button to the outer header layout (non-aligned)
    BEFORE draw_collapsible creates its own align=True row for the View/Select
    etc. menu items.  This gives the button fully-rounded corners on both sides.
    """
    if context.mode == 'EDIT_MESH':
        obj = context.active_object
        if obj is not None and obj.type == 'MESH':
            layout.row(align=True).operator(
                'mesh.modo_material_mode',
                text='',
                icon='MATERIAL',
                depress=state._material_mode_active,
            )
    state._orig_editor_menus_draw_collapsible(context, layout)
