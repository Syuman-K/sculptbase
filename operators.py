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
    density_mode: EnumProperty(
        name="ベース密度",
        items=[
            ('BUDGET', "合計面数から",
             "ベース合計面数の予算を決め、そこからエッジ長を逆算して"
             "各パーツへ表面積の比で配分する。全体の重さを直接指定できる"),
            ('MANUAL', "エッジ長を指定",
             "四角面1辺の目標長さを実寸で直接指定する"),
        ],
        default='BUDGET')
    base_budget: IntProperty(
        name="ベース合計面数",
        default=200000, min=100, soft_max=1000000,
        description="選択パーツ全体のベース面数の目安。表面積の比で各パーツ"
                    "へ配分するので、パーツの大小によらず面あたりの密度が"
                    "揃う。実効面数はここに Multires レベル分(4^レベル)が"
                    "掛かる")
    edge_length: FloatProperty(
        name="エッジ長",
        default=0.01, min=1e-5, soft_max=1.0, precision=5, unit='LENGTH',
        description="四角面1辺の目標長さ(実寸)。シーンの単位を出力サイズに"
                    "合わせておけば、これがそのままベースの目の細かさになる")
    min_faces: IntProperty(
        name="最小面数",
        default=200, min=4, soft_max=20000,
        description="小さいパーツがこの面数を下回らないようにする"
                    "(密度統一の例外。細かい小物が潰れるのを防ぐ)")
    levels: IntProperty(
        name="Multires レベル",
        default=2, min=1, max=6,
        description="形状を焼き込む Multires の段数。1段ごとに面数が4倍に"
                    "なる。ビューポート表示はレベル1に設定される"
                    "(スカルプト時に上げる)")
    joint_distance: FloatProperty(
        name="接合部判定距離",
        default=1.0, min=0.0, soft_max=20.0, precision=3, unit='LENGTH',
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
        default=3.0, min=0.0, soft_max=50.0, precision=3, unit='LENGTH',
        description="接合部を分離する際、判定距離にこの量を足した範囲まで"
                    "スカートとして含める。広いほどブーリアン統合が確実に"
                    "なるが、その分スカルプトできない領域が増える")
    joint_blend: FloatProperty(
        name="なじませ幅",
        default=4.0, min=0.0, soft_max=50.0, precision=3, unit='LENGTH',
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
    _stopping = False

    def invoke(self, context, event):
        if context.window is None:
            return self.execute(context)
        st = context.scene.sculptbase
        self._stopping = False
        try:
            self._gen = self._iter(context, st, self._should_cancel)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        wm = context.window_manager
        wm.progress_begin(0.0, 1.0)
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        self._set_status(context)
        return {'RUNNING_MODAL'}

    def _should_cancel(self):
        return self._stopping

    def _set_status(self, context):
        filled = int(round(20 * self._frac))
        bar = "█" * filled + "░" * (20 - filled)
        tail = ("中断待ち — 現在のパーツを終えたら止まります"
                if self._stopping else "Esc で中止")
        try:
            context.workspace.status_text_set(
                "{}: [{}] {}%  {}  —  {}".format(
                    self.bl_label, bar, int(round(100 * self._frac)),
                    self._label, tail))
        except Exception:
            pass

    def modal(self, context, event):
        if event.type == 'ESC' and not self._stopping:
            # ここで即座に打ち切ると、ソース名だけ変わった半端なパーツや
            # 一時オブジェクトが残る。現在のパーツを最後まで処理させてから
            # 止めることで、常に「N個完了・残りは手つかず」の状態にする。
            self._stopping = True
            self._set_status(context)
            self.report({'INFO'},
                        "SculptBase: 現在のパーツを終えたら中断します")
            return {'RUNNING_MODAL'}
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

    def _iter(self, context, settings, should_cancel=None):
        return core.iter_convert(context, settings, should_cancel)

    def _report_done(self, value):
        results, n_protected, warnings = value
        for warn in warnings:
            self.report({'WARNING'}, "SculptBase: " + warn)
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

    def _iter(self, context, settings, should_cancel=None):
        return core.iter_finalize(context, settings, should_cancel)

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
