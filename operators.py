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
        default=8000, min=100, soft_max=200000,
        description="ベースメッシュの目標面数(QuadriFlow エンジン時)。"
                    "実効面数は Multires レベルごとに4倍になるので、"
                    "レベル3なら64倍。大きすぎると変換も統合も重くなる")
    levels: IntProperty(
        name="Multires レベル",
        default=2, min=1, max=6,
        description="形状を焼き込む Multires の段数。1段ごとに面数が4倍に"
                    "なる。ビューポート表示はレベル1に設定される"
                    "(スカルプト時に上げる)")
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
    joint_blend: FloatProperty(
        name="なじませ幅",
        default=0.004, min=0.0, soft_max=0.1, precision=4, unit='LENGTH',
        description="出力時、接合部(完全にソースのまま)から造形面へ"
                    "切り替わる遷移帯の幅。ここを 0 にすると境目に段差が"
                    "出ることがある")
    verify_joints: BoolProperty(
        name="統合後に接合部を検証", default=True,
        description="出力統合のあと、接合部の形状がソース(SB_Source に"
                    "退避された元パーツ)と一致するかを抜き取り検査し、"
                    "食い違いが大きければ警告する。「ソースを退避して残す」"
                    "が有効なときだけ動作する")
    keep_source: BoolProperty(
        name="ソースを退避して残す", default=True,
        description="変換後の高密度ソースを SB_Source コレクションに移して"
                    "ビューレイヤーから除外する(依存グラフの評価対象外に"
                    "なるため重くならない)。無効なら削除するが、"
                    "「接合部の原形を保持」が有効なときは出力時にダボを"
                    "厳密復元する土台として必ず保持される")


class _ProgressModal:
    """段階実行ジェネレーターをタイマーで回し、進捗バーを出すミックスイン。

    ヘッダーの進捗バー(レンダリング時と同じもの)とステータス行のゲージを
    更新しながら、1ティックにつき1段階ずつ処理する。Esc で中断できる。
    サブクラスは ``_iter(context)`` と ``_report_done(result)`` を実装する。
    """

    _timer = None
    _gen = None
    _frac = 0.0
    _label = ""

    def invoke(self, context, event):
        if context.window is None:
            return self.execute(context)
        st = context.scene.sculptbase
        try:
            self._gen = self._iter(context, st)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        wm = context.window_manager
        wm.progress_begin(0.0, 1.0)
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        self._set_status(context)
        return {'RUNNING_MODAL'}

    def _set_status(self, context):
        filled = int(round(20 * self._frac))
        bar = "█" * filled + "░" * (20 - filled)
        try:
            context.workspace.status_text_set(
                "{}: [{}] {}%  {}  —  Esc で中止".format(
                    self.bl_label, bar, int(round(100 * self._frac)),
                    self._label))
        except Exception:
            pass

    def modal(self, context, event):
        if event.type == 'ESC':
            self._cleanup(context)
            self.report({'WARNING'},
                        "SculptBase: 中止しました({}% 時点)".format(
                            int(round(100 * self._frac))))
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'RUNNING_MODAL'}
        try:
            self._frac, self._label = next(self._gen)
            context.window_manager.progress_update(self._frac)
            self._set_status(context)
        except StopIteration as stop:
            self._cleanup(context)
            self._report_done(stop.value)
            return {'FINISHED'}
        except Exception as exc:  # noqa: BLE001 - surface any failure
            self._cleanup(context)
            self.report({'ERROR'}, "SculptBase 失敗: {}".format(exc))
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _cleanup(self, context):
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        try:
            wm.progress_end()
        except Exception:
            pass
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass


class SCULPTBASE_OT_convert(_ProgressModal, Operator):
    bl_idname = "sculptbase.convert"
    bl_label = "スカルプトベースに変換"
    bl_description = ("選択パーツを四角リメッシュし、Multires に元形状を"
                     "焼き込み、接合部(分割面・ダボ)をマスク保護する。"
                     "元の高密度パーツは SB_Source に退避される")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def _iter(self, context, settings):
        return core.iter_convert(context, settings)

    def _report_done(self, value):
        results, n_protected = value
        self.report(
            {'INFO'},
            "SculptBase: {} パーツを変換, 接合部 {} 頂点をマスク保護".format(
                len(results), n_protected))

    def execute(self, context):
        st = context.scene.sculptbase
        try:
            value = core.convert_selection(context, st)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:  # noqa: BLE001 - surface any failure
            self.report({'ERROR'}, "SculptBase 失敗: {}".format(exc))
            return {'CANCELLED'}
        self._report_done(value)
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


class SCULPTBASE_OT_finalize(_ProgressModal, Operator):
    bl_idname = "sculptbase.finalize"
    bl_label = "出力用に統合"
    bl_description = ("選択中の変換済みベースの Multires をトップレベルで"
                     "適用し、分離してあった接合部(_joint)をブーリアンで"
                     "統合した出力メッシュを作る。ベースと _joint は "
                     "SB_Sculpt に退避され再編集できる")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.get(core.RESULT_TAG) for o in context.selected_objects)

    def _iter(self, context, settings):
        return core.iter_finalize(context, settings)

    def _report_done(self, value):
        results, warnings = value
        for warn in warnings:
            self.report({'WARNING'}, warn)
        self.report(
            {'INFO'},
            "SculptBase: {} パーツを出力用に統合しました"
            "(PhasePorter で print_ へ移行できます)".format(len(results)))

    def execute(self, context):
        st = context.scene.sculptbase
        try:
            value = core.finalize_selection(context, st)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:  # noqa: BLE001 - surface any failure
            self.report({'ERROR'}, "SculptBase 統合失敗: {}".format(exc))
            return {'CANCELLED'}
        self._report_done(value)
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
