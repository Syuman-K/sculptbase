"""SculptBase のオペレーターと設定。"""

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
)
from bpy.types import Operator, PropertyGroup

from . import core


def _engine_items(self, context):
    items = [('QUADRIFLOW', "QuadriFlow(組み込み)",
              "Blender 組み込みの四角リメッシュ(Huang et al., SGP 2018)。"
              "依存なしで動作する")]
    if core.qremeshify_available():
        items.insert(0, ('QUADWILD', "QuadWild / Bi-MDF(QRemeshify)",
                         "QRemeshify 拡張経由の QuadWild + Bi-MDF ソルバー"
                         "(SIGGRAPH 2021 / SIGGRAPH Asia 2023)。"
                         "シャープ特徴に沿った高品質な四角面を生成する。"
                         "密度等の詳細は QRemeshify パネルの設定に従う"))
    return items


class SculptBaseSettings(PropertyGroup):
    engine: EnumProperty(
        name="リメッシュエンジン",
        items=_engine_items,
    )
    target_faces: IntProperty(
        name="目標四角面数",
        default=20000, min=100, soft_max=200000,
        description="ベースメッシュの目標面数(QuadriFlow エンジン時)。"
                    "マルチレゾ レベル3なら実効密度は約64倍になる")
    levels: IntProperty(
        name="Multires レベル",
        default=3, min=1, max=6,
        description="形状を焼き込む Multires の段数。ビューポート表示は"
                    "レベル1に設定される(スカルプト時に上げる)")
    joint_distance: FloatProperty(
        name="接合部判定距離",
        default=0.001, min=0.0, soft_max=0.1, precision=4, unit='LENGTH',
        description="他パーツの表面からこの距離以内の頂点を接合部"
                    "(分割面・ダボ)としてマスク保護する")
    separate_joints: BoolProperty(
        name="接合部の原形を保持", default=True,
        description="接合部(ダボ+周辺スカート)をリメッシュ前に"
                    "「<名前>_joint」として分離し、元のジオメトリを1頂点も"
                    "変えずに保持する。リメッシュ+転写の近似でダボが崩れる"
                    "のを防ぐ。出力時に「出力用に統合」で本体と exact "
                    "ブーリアン統合される")
    joint_margin: FloatProperty(
        name="分離マージン",
        default=0.003, min=0.0, soft_max=0.1, precision=4, unit='LENGTH',
        description="接合部を分離する際、判定距離にこの量を足した範囲まで"
                    "スカートとして含める。広いほどブーリアン統合が確実に"
                    "なるが、その分スカルプトできない領域が増える")
    keep_source: BoolProperty(
        name="ソースを退避して残す", default=True,
        description="変換後の高密度ソースを SB_Source コレクションに移して"
                    "ビューレイヤーから除外する(依存グラフの評価対象外に"
                    "なるため重くならない)。無効なら削除する")


class SCULPTBASE_OT_convert(Operator):
    bl_idname = "sculptbase.convert"
    bl_label = "スカルプトベースに変換"
    bl_description = ("選択パーツを四角リメッシュし、Multires に元形状を"
                     "焼き込み、接合部(分割面・ダボ)をマスク保護する。"
                     "元の高密度パーツは SB_Source に退避される")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        st = context.scene.sculptbase
        wm = context.window_manager
        parts = [o for o in context.selected_objects if o.type == 'MESH']
        wm.progress_begin(0, len(parts))
        try:
            results, n_protected = core.convert_selection(context, st)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:  # noqa: BLE001 - surface any failure
            self.report({'ERROR'}, "SculptBase 失敗: {}".format(exc))
            return {'CANCELLED'}
        finally:
            wm.progress_end()
        self.report(
            {'INFO'},
            "SculptBase: {} パーツを変換, 接合部 {} 頂点をマスク保護".format(
                len(results), n_protected))
        return {'FINISHED'}


class SCULPTBASE_OT_remask(Operator):
    bl_idname = "sculptbase.remask"
    bl_label = "接合部マスクを再検出"
    bl_description = ("選択中のメッシュ同士で接合部を検出し直し、マスク・"
                     "頂点グループ・フェイスセットを更新する(リメッシュは"
                     "行わない)。手動リトポしたパーツにも使える")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return sum(1 for o in context.selected_objects
                   if o.type == 'MESH') >= 2

    def execute(self, context):
        st = context.scene.sculptbase
        parts = [o for o in context.selected_objects if o.type == 'MESH']
        bvhs = {o: core.build_bvh(o) for o in parts}
        total = 0
        for obj in parts:
            others = [bvh for o, bvh in bvhs.items()
                      if o is not obj and bvh is not None]
            joints = core.joint_vertex_indices(obj, others, st.joint_distance)
            total += core.protect_joints(obj, joints)
        self.report({'INFO'},
                    "SculptBase: 接合部 {} 頂点をマスク保護".format(total))
        return {'FINISHED'}


class SCULPTBASE_OT_finalize(Operator):
    bl_idname = "sculptbase.finalize"
    bl_label = "出力用に統合"
    bl_description = ("選択中の変換済みベースの Multires をトップレベルで"
                     "適用し、分離してあった接合部(_joint)を exact "
                     "ブーリアンで統合した出力メッシュを作る。ベースと "
                     "_joint は SB_Sculpt に退避され再編集できる")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.get(core.RESULT_TAG) for o in context.selected_objects)

    def execute(self, context):
        try:
            results, warnings = core.finalize_selection(context)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:  # noqa: BLE001 - surface any failure
            self.report({'ERROR'}, "SculptBase 統合失敗: {}".format(exc))
            return {'CANCELLED'}
        for warn in warnings:
            self.report({'WARNING'}, warn)
        self.report(
            {'INFO'},
            "SculptBase: {} パーツを出力用に統合しました"
            "(PhasePorter で print_ へ移行できます)".format(len(results)))
        return {'FINISHED'}


CLASSES = (
    SculptBaseSettings,
    SCULPTBASE_OT_convert,
    SCULPTBASE_OT_remask,
    SCULPTBASE_OT_finalize,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sculptbase = PointerProperty(type=SculptBaseSettings)


def unregister():
    del bpy.types.Scene.sculptbase
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
