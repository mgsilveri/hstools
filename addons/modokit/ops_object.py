"""
Object Mode selection operators:
  OBJECT_OT_modo_click_select   (click / double-click / paint)
  OBJECT_OT_modo_lasso_select   (freehand lasso)
"""

import bpy
import math
import time
from mathutils import Vector
from bpy.props import IntProperty, EnumProperty, BoolProperty

from . import state
from .utils import get_addon_preferences, point_in_polygon


class OBJECT_OT_modo_click_select(bpy.types.Operator):
    """Modo-style click/paint selection for Object Mode.

    - Plain click:    replace selection (deselects on empty space)
    - Shift-click:    add to selection
    - Ctrl-click:     remove from selection
    - Left-drag:      paint-select objects under cursor while dragging
    - Double-click:   enter Edit Mode on the clicked object
    """
    bl_idname = "object.modo_click_select"
    bl_label  = "Modo Click/Paint Select (Object Mode)"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name="Mode",
        items=[
            ('set',    "Set",    "Replace selection (plain click)"),
            ('add',    "Add",    "Add to selection (Shift-click)"),
            ('toggle', "Toggle", "Toggle selection state"),
            ('remove', "Remove", "Remove from selection (Ctrl-click)"),
        ],
        default='set',
        options={'HIDDEN'},
    )

    mouse_x: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    mouse_y: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    _last_click_time: float = 0.0

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and getattr(context.area, 'type', None) == 'VIEW_3D')

    def _raycast_obj(self, context, mx, my):
        from bpy_extras import view3d_utils
        region = context.region
        rv3d   = context.region_data
        if not region or not rv3d:
            return None
        coord     = (mx, my)
        origin    = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        depsgraph = context.evaluated_depsgraph_get()
        hit, _loc, _nor, _idx, obj, _mat = context.scene.ray_cast(depsgraph, origin, direction)
        return obj if hit else None

    def _apply_mode(self, context, obj):
        if obj is None:
            if self.mode == 'set':
                bpy.ops.object.select_all(action='DESELECT')
            return
        if self.mode == 'set':
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
        elif self.mode == 'add':
            obj.select_set(True)
            context.view_layer.objects.active = obj
        elif self.mode == 'remove':
            obj.select_set(False)
            if context.view_layer.objects.active == obj:
                sel = context.selected_objects
                context.view_layer.objects.active = sel[0] if sel else None
        elif self.mode == 'toggle':
            new_state = not obj.select_get()
            obj.select_set(new_state)
            if new_state:
                context.view_layer.objects.active = obj
            elif context.view_layer.objects.active == obj:
                sel = context.selected_objects
                context.view_layer.objects.active = sel[0] if sel else None

    def invoke(self, context, event):
        self.mouse_x       = event.mouse_region_x
        self.mouse_y       = event.mouse_region_y
        self.start_mouse_x = self.mouse_x
        self.start_mouse_y = self.mouse_y
        self.is_dragging   = False
        self._drag_cleared = False
        self._drag_threshold = 4

        # ── Modo handle reposition (Object Mode) ──────────────────────────────
        if state._active_transform_mode in ('TRANSLATE', 'ROTATE', 'RESIZE'):
            from bpy_extras import view3d_utils
            from .transform_3d import _compute_selection_median, _start_anchor_timer, _start_pivot_crosshair
            coord     = (self.mouse_x, self.mouse_y)
            region    = context.region
            rv3d      = context.region_data
            depsgraph = context.evaluated_depsgraph_get()
            if region and rv3d:
                if state._snap_highlight is not None:
                    world_point = state._snap_highlight['world_pos'].copy()
                else:
                    origin    = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
                    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
                    hit, loc, _nor, _idx, _obj, _mat = context.scene.ray_cast(
                        depsgraph, origin, direction)
                    world_point = loc if hit else view3d_utils.region_2d_to_location_3d(
                        region, rv3d, coord, context.scene.cursor.location)
                if world_point is not None:
                    if state._saved_pivot_point is None:
                        state._saved_pivot_point = context.scene.tool_settings.transform_pivot_point
                        state._saved_cursor_location = context.scene.cursor.location.copy()
                    state._reposition_anchor = world_point.copy()
                    state._last_known_median = _compute_selection_median(context)
                    context.scene.cursor.location = world_point.copy()
                    context.scene.tool_settings.transform_pivot_point = 'CURSOR'
                    if state._active_transform_mode == 'TRANSLATE':
                        _start_anchor_timer()
                    _start_pivot_crosshair()
            return {'FINISHED'}

        prefs = get_addon_preferences(context)
        now   = time.time()
        if now - OBJECT_OT_modo_click_select._last_click_time < prefs.double_click_time:
            OBJECT_OT_modo_click_select._last_click_time = 0.0
            _EDIT_MODE_TYPES = {
                'MESH', 'CURVE', 'SURFACE', 'META', 'FONT',
                'ARMATURE', 'LATTICE',
            }
            hit = self._raycast_obj(context, self.mouse_x, self.mouse_y)
            if hit and hit.type in _EDIT_MODE_TYPES:
                bpy.ops.object.select_all(action='DESELECT')
                hit.select_set(True)
                context.view_layer.objects.active = hit
                bpy.ops.object.editmode_toggle()
            return {'FINISHED'}

        OBJECT_OT_modo_click_select._last_click_time = now
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            dx = abs(event.mouse_region_x - self.start_mouse_x)
            dy = abs(event.mouse_region_y - self.start_mouse_y)
            if dx > self._drag_threshold or dy > self._drag_threshold:
                if not self.is_dragging:
                    self.is_dragging = True
                    if self.mode == 'set' and not self._drag_cleared:
                        bpy.ops.object.select_all(action='DESELECT')
                        self._drag_cleared = True
                self.mouse_x = event.mouse_region_x
                self.mouse_y = event.mouse_region_y
                hit = self._raycast_obj(context, self.mouse_x, self.mouse_y)
                if hit:
                    if self.mode in {'set', 'add'}:
                        hit.select_set(True)
                        context.view_layer.objects.active = hit
                    elif self.mode == 'remove':
                        hit.select_set(False)

        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if not self.is_dragging:
                hit = self._raycast_obj(context, self.mouse_x, self.mouse_y)
                self._apply_mode(context, hit)
            return {'FINISHED'}

        elif event.type in {'RIGHTMOUSE', 'ESC'}:
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def execute(self, context):
        hit = self._raycast_obj(context, self.mouse_x, self.mouse_y)
        self._apply_mode(context, hit)
        return {'FINISHED'}


class OBJECT_OT_modo_lasso_select(bpy.types.Operator):
    """Modo-style lasso selection for Object Mode.

    Right-click drag draws a freehand lasso; any object whose origin or a
    bounding-box corner falls inside the lasso is selected.
    """
    bl_idname  = "object.modo_lasso_select"
    bl_label   = "Lasso Select Objects (Modo Style)"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name="Mode",
        items=[
            ('set',    "Set",    "Replace selection"),
            ('add',    "Add",    "Add to selection (Shift)"),
            ('remove', "Remove", "Remove from selection (Ctrl)"),
        ],
        default='set',
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        if state._active_transform_mode is not None:
            return False
        return (context.mode == 'OBJECT'
                and getattr(context.area, 'type', None) == 'VIEW_3D')

    def _draw_lasso_callback(self, context):
        import gpu
        from gpu_extras.batch import batch_for_shader
        pts = self.lasso_points
        if len(pts) < 2:
            return
        try:
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            gpu.state.blend_set('ALPHA')
            shader.bind()

            if len(pts) >= 3:
                def _dp_simplify(points, epsilon):
                    if len(points) < 3:
                        return points
                    ax, ay = points[0]; bx, by = points[-1]
                    dx, dy = bx-ax, by-ay
                    length_sq = dx*dx + dy*dy
                    max_dist, max_idx = 0.0, 0
                    for i in range(1, len(points)-1):
                        px, py = points[i]
                        d = (math.sqrt((px-ax)**2+(py-ay)**2) if length_sq == 0
                             else math.sqrt((px-(ax+max(0.,min(1.,((px-ax)*dx+(py-ay)*dy)/length_sq))*dx))**2+
                                            (py-(ay+max(0.,min(1.,((px-ax)*dx+(py-ay)*dy)/length_sq))*dy))**2))
                        if d > max_dist:
                            max_dist, max_idx = d, i
                    if max_dist > epsilon:
                        return _dp_simplify(points[:max_idx+1], epsilon)[:-1] + _dp_simplify(points[max_idx:], epsilon)
                    return [points[0], points[-1]]

                simplified = _dp_simplify(pts, 2.0)
                if len(simplified) < 3:
                    simplified = pts
                from mathutils.geometry import tessellate_polygon
                pts3d = [(p[0], p[1], 0.0) for p in simplified]
                try:
                    tris = []
                    for tri in tessellate_polygon([pts3d]):
                        for idx in tri:
                            tris.append(simplified[idx])
                    if tris:
                        shader.uniform_float("color", (0.5, 0.5, 0.5, 0.15))
                        batch_for_shader(shader, 'TRIS', {"pos": tris}).draw(shader)
                except Exception:
                    pass

            closed = pts + [pts[0]]
            DASH, GAP, PERIOD = 4, 4, 8

            def build_dash_coords(polyline):
                result = []; phase = 0
                for i in range(len(polyline)-1):
                    ax, ay = polyline[i]; bx, by = polyline[i+1]
                    seg_len = math.sqrt((bx-ax)**2+(by-ay)**2)
                    if seg_len == 0: continue
                    dx = (bx-ax)/seg_len; dy = (by-ay)/seg_len
                    t = 0.0
                    while t < seg_len:
                        cp = phase % PERIOD; step = min(PERIOD-cp, seg_len-t)
                        if cp < DASH:
                            drawn = min(DASH-cp, step)
                            result += [(ax+dx*t, ay+dy*t), (ax+dx*(t+drawn), ay+dy*(t+drawn))]
                            phase += drawn; t += drawn
                        else:
                            skip = min(GAP-(cp-DASH), step); phase += skip; t += skip
                return result

            gpu.state.line_width_set(1.0)
            shader.uniform_float("color", (0.,0.,0.,1.))
            batch_for_shader(shader, 'LINE_STRIP', {"pos": closed}).draw(shader)
            wc = build_dash_coords(closed)
            if wc:
                shader.uniform_float("color", (1.,1.,1.,1.))
                batch_for_shader(shader, 'LINES', {"pos": wc}).draw(shader)
        except Exception as e:
            print(f"[OBJECT LASSO DRAW ERROR] {e}")
        finally:
            gpu.state.blend_set('NONE')
            gpu.state.line_width_set(1.0)

    def _remove_draw_handler(self):
        if self._draw_handler is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handler, 'WINDOW')
            self._draw_handler = None

    def _apply_lasso(self, context):
        from bpy_extras import view3d_utils
        region = context.region
        rv3d   = context.region_data
        pts    = self.lasso_points
        if len(pts) < 3 or not region or not rv3d:
            return
        if self.mode == 'set':
            bpy.ops.object.select_all(action='DESELECT')
        last_selected = None
        for obj in context.visible_objects:
            test_pts_3d = [obj.matrix_world.translation.copy()]
            try:
                for corner in obj.bound_box:
                    test_pts_3d.append(obj.matrix_world @ Vector(corner))
            except Exception:
                pass
            inside = False
            for pt3d in test_pts_3d:
                sc = view3d_utils.location_3d_to_region_2d(region, rv3d, pt3d)
                if sc and point_in_polygon((sc.x, sc.y), pts):
                    inside = True
                    break
            if inside:
                if self.mode in {'set', 'add'}:
                    obj.select_set(True)
                    last_selected = obj
                elif self.mode == 'remove':
                    obj.select_set(False)
        if last_selected:
            context.view_layer.objects.active = last_selected
        elif self.mode == 'remove' and context.view_layer.objects.active:
            active = context.view_layer.objects.active
            if not active.select_get():
                sel = context.selected_objects
                context.view_layer.objects.active = sel[0] if sel else None

    def invoke(self, context, event):
        self.lasso_points = [(event.mouse_region_x, event.mouse_region_y)]
        self._button = event.type
        self._start_x = event.mouse_region_x
        self._start_y = event.mouse_region_y
        self._draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_lasso_callback, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if context.area:
            context.area.tag_redraw()
        if event.type == 'MOUSEMOVE':
            self.lasso_points.append((event.mouse_region_x, event.mouse_region_y))
        elif event.type == self._button and event.value in {'RELEASE', 'CLICK'}:
            self._remove_draw_handler()
            if context.area:
                context.area.tag_redraw()
            dx = event.mouse_region_x - self._start_x
            dy = event.mouse_region_y - self._start_y
            drag_dist = 0.0 if event.value == 'CLICK' else math.sqrt(dx*dx + dy*dy)
            is_click = drag_dist < 10
            if is_click and self._button == 'RIGHTMOUSE':
                if self.mode == 'set':
                    menu = state._saved_rmb_menus.get('Object Mode', 'VIEW3D_MT_object_context_menu')
                    bpy.ops.wm.call_menu(name=menu)
                return {'FINISHED'}
            if len(self.lasso_points) >= 3 and drag_dist >= 5:
                self._apply_lasso(context)
            return {'FINISHED'}
        elif event.type in {'ESC', 'LEFTMOUSE'}:
            self._remove_draw_handler()
            if context.area:
                context.area.tag_redraw()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}
