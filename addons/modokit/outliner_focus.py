"""outliner_focus.py — Auto-scroll the Outliner to match the active object on selection change.

Registers a depsgraph_update_post handler that detects when the Object-mode
selection changes and fires ``bpy.ops.outliner.show_active()`` to scroll the
Outliner so the active object (and therefore all selected objects whose parent
collections get expanded) is visible.

Mutations are deferred via a zero-delay timer so they never execute during
depsgraph evaluation.
"""

import bpy
from . import state
from .utils import _get_prefs


# ============================================================================
# Internal helpers
# ============================================================================

def _do_outliner_focus():
    """Timer callback — actually calls show_active() on the Outliner."""
    state._outliner_focus_timer_pending = False

    context = bpy.context
    screen = getattr(context, 'screen', None)
    if screen is None:
        return None

    for area in screen.areas:
        if area.type != 'OUTLINER':
            continue
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        space  = next((s for s in area.spaces  if s.type  == 'OUTLINER'), None)
        if region is None or space is None:
            continue
        with context.temp_override(area=area, region=region, space_data=space):
            try:
                bpy.ops.outliner.show_active()
            except Exception:
                pass
        break  # only need to act on the first Outliner area found

    return None  # do not repeat


# ============================================================================
# Depsgraph handler
# ============================================================================

@bpy.app.handlers.persistent
def _outliner_focus_depsgraph_handler(scene, depsgraph):
    """Detect Object-mode selection changes and queue an Outliner scroll."""
    context = bpy.context

    # Only relevant in Object mode
    if getattr(context, 'mode', None) != 'OBJECT':
        state._outliner_focus_prev_selected = frozenset()
        return

    # Respect the user's pref toggle
    prefs = _get_prefs(context)
    if prefs is not None and not prefs.enable_outliner_focus:
        return

    selected = frozenset(
        obj.name for obj in getattr(context, 'selected_objects', ())
    )

    if selected == state._outliner_focus_prev_selected:
        return

    state._outliner_focus_prev_selected = selected

    if not selected or getattr(context, 'active_object', None) is None:
        return

    # Debounce: if a timer is already queued, let it run rather than stacking more.
    if state._outliner_focus_timer_pending:
        return

    state._outliner_focus_timer_pending = True
    bpy.app.timers.register(_do_outliner_focus, first_interval=0.05)
