"""N パネル UI (View3D > Sidebar > SculptBase)。"""

import bpy
from bpy.types import Panel

from . import core


class SCULPTBASE_PT_main(Panel):
    bl_idname = "SCULPTBASE_PT_main"
    bl_label = "SculptBase"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SculptBase"

    def draw(self, context):
        st = context.scene.sculptbase
        layout = self.layout

        box = layout.box()
        box.label(text="リトポ(四角リメッシュ)", icon='MOD_REMESH')
        box.prop(st, "engine", text="")
        if not core.qremeshify_available():
            box.label(text="QRemeshify 導入で QuadWild が使えます",
                      icon='INFO')
        if st.engine == 'QUADRIFLOW':
            box.prop(st, "target_faces")

        box = layout.box()
        box.label(text="形状転写", icon='MOD_MULTIRES')
        box.prop(st, "levels")

        box = layout.box()
        box.label(text="接合部保護(分割面・ダボ)", icon='LOCKED')
        box.prop(st, "joint_distance")
        box.prop(st, "separate_joints")
        sub = box.column()
        sub.enabled = st.separate_joints
        sub.prop(st, "joint_margin")

        layout.prop(st, "keep_source")

        n_sel = sum(1 for o in context.selected_objects if o.type == 'MESH')
        layout.label(text="選択中のメッシュ: {}".format(n_sel))
        col = layout.column(align=True)
        col.operator("sculptbase.convert", icon='SCULPTMODE_HLT')
        col.operator("sculptbase.remask", icon='LOCKED')

        layout.separator()
        layout.label(text="出力(print_ 移行前)", icon='EXPORT')
        layout.operator("sculptbase.finalize", icon='MOD_BOOLEAN')


CLASSES = (
    SCULPTBASE_PT_main,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
