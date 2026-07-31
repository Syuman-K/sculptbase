"""SculptBase のヘッドレステスト。

    blender -b --factory-startup --python _test/test_sculptbase.py
"""

import os
import sys

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PARENT = os.path.dirname(_REPO)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

_PKG = os.path.basename(_REPO)
addon = __import__(_PKG)
core = addon.core


def _fail(msg):
    print("  FAIL:", msg)
    raise SystemExit(1)


DOWEL_CORNER = (1.35, 0.15, 0.15)   # ダボ先端の正確な角座標(保持の検証に使う)


def _make_parts():
    """ダボ付きの球(パーツA)と、ダボを受ける立方体(パーツB)を作る。

    パーツAは球 + ダボ(小さな直方体, x 1.05..1.35)を結合したもの。
    パーツBはダボ先端が食い込む位置に置いた立方体。
    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=1)
    sphere = bpy.context.active_object
    sphere.name = "part_A"
    sphere.scale = (1.0, 1.0, 1.4)
    bpy.ops.object.transform_apply(scale=True)

    bpy.ops.mesh.primitive_cube_add(size=0.3, location=(1.2, 0, 0))
    dowel = bpy.context.active_object
    with bpy.context.temp_override(active_object=sphere,
                                   selected_editable_objects=[sphere, dowel]):
        bpy.ops.object.join()
    sphere = bpy.data.objects["part_A"]

    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(1.8, 0, 0))
    cube = bpy.context.active_object
    cube.name = "part_B"
    return sphere, cube


def _unexclude(coll_name):
    """退避コレクションをビューレイヤーに戻す(選択できるようにする)。"""
    coll = bpy.data.collections.get(coll_name)
    if coll is None:
        return

    def _find(layer):
        if layer.collection == coll:
            return layer
        for child in layer.children:
            f = _find(child)
            if f:
                return f
        return None

    layer = _find(bpy.context.view_layer.layer_collection)
    if layer:
        layer.exclude = False


def _has_exact_vertex(obj, target, tol=1e-6):
    from mathutils import Vector
    t = Vector(target)
    mw = obj.matrix_world
    return any((mw @ v.co - t).length < tol for v in obj.data.vertices)


def test_convert():
    print("== convert: remesh + multires transfer + joint guard ==")
    sphere, cube = _make_parts()
    dense_dims = sphere.dimensions.copy()
    sphere.select_set(True)
    cube.select_set(True)

    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 0.12
    st.min_faces = 200
    st.levels = 2
    st.joint_distance = 0.02
    st.separate_joints = True
    st.joint_margin = 0.3
    st.joint_blend = 0.02
    st.keep_source = True

    result = bpy.ops.sculptbase.convert()
    if result != {'FINISHED'}:
        _fail("operator returned {}".format(result))

    results = [o for o in bpy.data.objects if o.get(core.RESULT_TAG)]
    if {o.name for o in results} != {"part_A", "part_B"}:
        _fail("results keep original names, got {}".format(
            [o.name for o in results]))

    base = bpy.data.objects["part_A"]
    quads = sum(1 for p in base.data.polygons if len(p.vertices) == 4)
    ratio = quads / max(len(base.data.polygons), 1)
    if len(base.data.polygons) > 4000 or ratio < 0.8:
        _fail("base not a reasonable quad mesh ({} polys, {:.0%} quads)".format(
            len(base.data.polygons), ratio))
    print("  base: {} polys, {:.0%} quads".format(
        len(base.data.polygons), ratio))

    mr = base.modifiers.get(core.MULTIRES_NAME)
    if mr is None or mr.type != 'MULTIRES' or mr.sculpt_levels != 2:
        _fail("multires missing or wrong levels")

    # 形状転写の確認: 本体(ダボ抜き)のY/Z寸法が元とほぼ一致する
    mr.levels = 2
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    dims = base.evaluated_get(depsgraph).dimensions
    for i in (1, 2):
        if abs(dims[i] - dense_dims[i]) / dense_dims[i] > 0.05:
            _fail("transferred shape deviates: {} vs {}".format(
                tuple(dims), tuple(dense_dims)))
    print("  multires L2 dims {} ~= source {}".format(
        tuple(round(d, 3) for d in dims),
        tuple(round(d, 3) for d in dense_dims)))

    # 接合部の原形保持: _joint にダボ先端の角が1頂点も動かず残っている
    joint = bpy.data.objects.get("part_A_joint")
    if joint is None:
        _fail("part_A_joint was not created")
    if not joint.get(core.JOINT_TAG):
        _fail("joint object not tagged")
    if core.count_boundary_edges(joint.data) != 0:
        _fail("joint chunk is not watertight")
    if not _has_exact_vertex(joint, DOWEL_CORNER):
        _fail("exact dowel corner missing from joint chunk")
    print("  joint chunk: watertight, dowel corner preserved exactly")

    # 接合部保護: 立方体と接する側の頂点がマスクされている
    vg = base.vertex_groups.get(core.JOINT_GROUP)
    if vg is None:
        _fail("joint vertex group missing")
    protected = base.get("sculptbase_protected_verts", 0)
    if protected <= 0:
        _fail("no joint vertices were protected")
    mask = base.data.attributes.get(".sculpt_mask")
    if mask is None or not any(d.value >= 1.0 for d in mask.data):
        _fail("sculpt mask not written")
    fs = base.data.attributes.get(".sculpt_face_set")
    if fs is None:
        _fail("face sets not written")
    print("  joint guard: {} verts masked".format(protected))

    # ソース退避: SB_Source コレクションに移され、ビューレイヤーから除外
    coll = bpy.data.collections.get(core.SOURCE_COLLECTION)
    if coll is None or len(coll.objects) != 2:
        _fail("sources not stashed")
    if "part_A_src" not in coll.objects:
        _fail("source rename missing")

    def _find(layer):
        if layer.collection == coll:
            return layer
        for child in layer.children:
            f = _find(child)
            if f:
                return f
        return None

    layer = _find(bpy.context.view_layer.layer_collection)
    if layer is None or not layer.exclude:
        _fail("SB_Source not excluded from view layer")
    print("  sources stashed in excluded {}".format(core.SOURCE_COLLECTION))


def test_finalize():
    print("== finalize: multires apply + exact joint union ==")
    base = bpy.data.objects["part_A"]
    for o in bpy.context.selected_objects:
        o.select_set(False)
    base.select_set(True)
    bpy.context.view_layer.objects.active = base

    result = bpy.ops.sculptbase.finalize()
    if result != {'FINISHED'}:
        _fail("finalize returned {}".format(result))

    out = bpy.data.objects.get("part_A")
    if out is None or not out.get(core.FINAL_TAG):
        _fail("finalized object missing or untagged")
    if out.modifiers:
        _fail("finalized object still has modifiers")
    if core.count_boundary_edges(out.data) != 0:
        _fail("finalized mesh is not watertight")
    if not _has_exact_vertex(out, DOWEL_CORNER):
        _fail("exact dowel corner missing from finalized mesh")
    if len(out.data.polygons) < 10000:
        _fail("finalized mesh unexpectedly coarse ({} polys)".format(
            len(out.data.polygons)))
    sculpt_coll = bpy.data.collections.get(core.SCULPT_COLLECTION)
    if sculpt_coll is None or "part_A_sculpt" not in sculpt_coll.objects \
            or "part_A_joint" not in sculpt_coll.objects:
        _fail("base/joint were not stashed into SB_Sculpt")
    print("  output: watertight, {} polys, dowel corner exact, "
          "base+joint stashed".format(len(out.data.polygons)))


def test_scale_sanity_warning():
    """接合部判定距離がパーツ寸法に対して極端なら警告すること。

    回帰: 既定値が 1単位=1m 前提(0.001)のまま、mm スケールのシーンで使うと
    判定距離が実質ゼロになり、接合部が一切検出されないまま処理が通っていた。
    """
    print("== joint distance sanity vs part size ==")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # 100 単位の大きなパーツ2個(mm 運用なら 100mm)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16,
                                         radius=50, location=(0, 0, 0))
    a = bpy.context.active_object
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16,
                                         radius=50, location=(100, 0, 0))
    b = bpy.context.active_object

    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 12.0
    st.min_faces = 100
    st.levels = 1
    st.separate_joints = True
    st.joint_margin = 3.0

    a.select_set(True)
    b.select_set(True)
    bpy.context.view_layer.objects.active = a

    st.joint_distance = 0.001          # 旧既定 = 100単位のパーツに対し極小
    _r, _n, warnings = core.convert_selection(bpy.context, st)
    if not any("小さすぎます" in w for w in warnings):
        _fail("no warning for an implausibly small joint distance: {}".format(
            warnings))
    print("  0.001 (パーツ100) -> 小さすぎ警告あり")

    # 大きすぎる側
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16,
                                         radius=50, location=(0, 0, 0))
    a = bpy.context.active_object
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16,
                                         radius=50, location=(100, 0, 0))
    b = bpy.context.active_object
    a.select_set(True)
    b.select_set(True)
    bpy.context.view_layer.objects.active = a
    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 12.0
    st.min_faces = 100
    st.levels = 1
    st.separate_joints = True
    st.joint_distance = 50.0           # パーツの半分
    st.joint_margin = 3.0
    _r, _n, warnings = core.convert_selection(bpy.context, st)
    if not any("大きすぎます" in w for w in warnings):
        _fail("no warning for an implausibly large joint distance: {}".format(
            warnings))
    print("  50 (パーツ100) -> 大きすぎ警告あり")


def test_modifiers_applied():
    """モディファイアが処理前に適用され、接合部判定にも反映されること。

    回帰: 接合部分離は素のメッシュ、形状転写は評価後、と食い違っていたため
    モディファイア付きパーツの結果が予測できなかった。
    """
    print("== modifiers are applied before processing ==")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=8)
    a = bpy.context.active_object
    a.name = "shelled"
    mod = a.modifiers.new("Shell", 'SOLIDIFY')
    mod.thickness = 2.0
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=8,
                                         location=(30, 0, 0))
    b = bpy.context.active_object
    b.name = "plain"

    raw_polys = len(a.data.polygons)
    bpy.context.view_layer.update()          # モディファイア追加を反映させる
    dg = bpy.context.evaluated_depsgraph_get()
    eval_mesh = a.evaluated_get(dg).to_mesh()
    eval_polys = len(eval_mesh.polygons)
    a.evaluated_get(dg).to_mesh_clear()
    if eval_polys <= raw_polys:
        _fail("test setup: solidify should add faces ({} -> {})".format(
            raw_polys, eval_polys))

    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 1.5
    st.min_faces = 200
    st.levels = 1
    st.separate_joints = False
    st.keep_source = True

    a.select_set(True)
    b.select_set(True)
    bpy.context.view_layer.objects.active = a
    _results, _n, warnings = core.convert_selection(bpy.context, st)

    if not any("モディファイアを適用" in w for w in warnings):
        _fail("no notice that modifiers were applied: {}".format(warnings))
    src = bpy.data.objects.get("shelled_src")
    if src is None:
        _fail("source not stashed")
    if src.modifiers:
        _fail("source still carries modifiers")
    # 退避されたソースが「適用後の形」になっていること。ここが素のままだと
    # 接合部判定(素のメッシュ)と形状転写(評価後)が食い違う。
    if len(src.data.polygons) != eval_polys:
        _fail("source is not the evaluated mesh: {} faces (raw {} / "
              "evaluated {})".format(len(src.data.polygons), raw_polys,
                                     eval_polys))
    print("  素 {} 面 -> 適用後 {} 面、退避ソースも {} 面で一致".format(
        raw_polys, eval_polys, len(src.data.polygons)))


def test_all_joint_warning():
    """全面が接合部になったパーツを警告すること。"""
    print("== part that is entirely joint is reported ==")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # 大きな球2つに挟まれた小片 -> 全面が接合部になる
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=20,
                                         location=(0, 0, 0))
    big_a = bpy.context.active_object
    big_a.name = "big_a"
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=20,
                                         location=(44, 0, 0))
    big_b = bpy.context.active_object
    big_b.name = "big_b"
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=1.5,
                                         location=(22, 0, 0))
    thin = bpy.context.active_object
    thin.name = "thin"

    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 4.0
    st.min_faces = 100
    st.levels = 1
    st.separate_joints = True
    st.joint_distance = 1.0
    st.joint_margin = 5.0        # 小片の直径より大きい -> 全面が接合部
    st.keep_source = True

    for o in (big_a, big_b, thin):
        o.select_set(True)
    bpy.context.view_layer.objects.active = big_a
    results, _n, warnings = core.convert_selection(bpy.context, st)

    if not any("全面が接合部" in w for w in warnings):
        _fail("no warning for an all-joint part: {}".format(warnings))
    thin_base = next(o for o in results if o.get(core.PART_PROP) == "thin")
    if thin_base.get(core.NO_JOINT_REASON) != 'all':
        _fail("reason should be 'all', got {}".format(
            thin_base.get(core.NO_JOINT_REASON)))
    print("  警告あり / 理由コード = {}".format(
        thin_base.get(core.NO_JOINT_REASON)))


def test_graceful_cancel():
    """中断はパーツの切れ目で効き、半端な状態を残さないこと。"""
    print("== cancelling stops at a part boundary ==")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for i in range(3):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8,
                                             radius=10,
                                             location=(i * 40, 0, 0))
        bpy.context.active_object.name = "p{}".format(i)
    parts = [o for o in bpy.data.objects if o.type == 'MESH']
    for o in parts:
        o.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]

    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 4.0
    st.min_faces = 100
    st.levels = 1
    st.separate_joints = False
    st.keep_source = True

    state = {"cancel": False}
    gen = core.iter_convert(bpy.context, st, lambda: state["cancel"])
    # 最初のパーツが終わったところで中断を要求する
    ticks = 0
    while True:
        try:
            next(gen)
            ticks += 1
            if ticks > 4:
                state["cancel"] = True
        except StopIteration as stop:
            results, _n, warnings = stop.value
            break

    if not any("中断しました" in w for w in warnings):
        _fail("no cancellation notice: {}".format(warnings))
    if not results:
        _fail("cancel produced nothing; expected at least one finished part")
    if len(results) >= 3:
        _fail("cancel did not actually stop early ({} parts)".format(
            len(results)))
    # 完了したものは完全な形、未処理のものは手つかず
    for base in results:
        if not base.get(core.RESULT_TAG) or base.modifiers[0].type != 'MULTIRES':
            _fail("finished part {} is incomplete".format(base.name))
    done = {o.get(core.PART_PROP) for o in results}
    for name in ("p0", "p1", "p2"):
        if name in done:
            continue
        untouched = bpy.data.objects.get(name)
        if untouched is None:
            _fail("unprocessed part {} went missing".format(name))
        if untouched.get(core.RESULT_TAG):
            _fail("unprocessed part {} was half-converted".format(name))
    leftovers = [o.name for o in bpy.data.objects
                 if "tmp" in o.name or o.name.endswith("_bodytmp")]
    if leftovers:
        _fail("temporary objects left behind: {}".format(leftovers))
    print("  {} / 3 パーツ完了、残りは手つかず、一時オブジェクト無し".format(
        len(results)))


def test_area_proportional_density():
    """ベース密度が面積比例になり、パーツ間で面あたり密度が揃うこと。

    従来はパーツごとに固定面数だったため、小さいパーツほど過密・
    大きいパーツほど粗くなっていた(eula 実データで小物が必要量の
    3倍以上の面数になっていた)。
    """
    print("== base density is proportional to surface area ==")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # 一辺 4 と 1 の立方体(表面積比 16:1)
    bpy.ops.mesh.primitive_cube_add(size=4, location=(0, 0, 0))
    big = bpy.context.active_object
    big.name = "big"
    bpy.ops.mesh.primitive_cube_add(size=1, location=(10, 0, 0))
    small = bpy.context.active_object
    small.name = "small"

    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 0.2
    st.min_faces = 4

    bases, edge = core.estimate_bases(st, [big, small])
    got = {o.name: n for o, n in bases}
    # 表面積: big = 6*4^2 = 96, small = 6 -> 面数比は 16:1 になるはず
    exp_big = round(96 / (0.2 ** 2))
    exp_small = round(6 / (0.2 ** 2))
    if abs(got["big"] - exp_big) > 2 or abs(got["small"] - exp_small) > 2:
        _fail("target faces not area-proportional: {} (expected ~{}/{})".format(
            got, exp_big, exp_small))
    ratio = got["big"] / got["small"]
    if abs(ratio - 16.0) > 0.5:
        _fail("face-count ratio {:.2f} should track the 16:1 area ratio"
              .format(ratio))
    print("  edge {:.3f}: big {:,} faces / small {:,} faces (比 {:.1f})".format(
        edge, got["big"], got["small"], ratio))

    # 予算モード: 合計面数が予算に収まり、配分は面積比になる
    st.density_mode = 'BUDGET'
    st.base_budget = 5100
    st.min_faces = 4
    bases, edge_budget = core.estimate_bases(st, [big, small])
    total = sum(n for _o, n in bases)
    if abs(total - 5100) > 5100 * 0.02:
        _fail("budget not honoured: {} vs 5100".format(total))
    got = {o.name: n for o, n in bases}
    if abs(got["big"] / got["small"] - 16.0) > 0.5:
        _fail("budget split is not area-proportional: {}".format(got))
    print("  予算 5,100 面 -> 合計 {:,} 面 (big {:,} / small {:,}), "
          "エッジ長 {:.3f}".format(total, got["big"], got["small"],
                                   edge_budget))

    # 予算方式は「選んだものへ配分する」ので、対象が増えれば1個あたりは
    # 粗くなる。合計が予算に収まり続けることを確認しておく。
    bpy.ops.mesh.primitive_cube_add(size=60, location=(0, 0, 200))
    stray = bpy.context.active_object
    stray.name = "stray_helper"
    bases3, _e = core.estimate_bases(st, [big, small, stray])
    total3 = sum(n for _o, n in bases3)
    if abs(total3 - 5100) > 5100 * 0.05:
        _fail("budget not honoured with 3 objects: {}".format(total3))
    print("  対象が増えても合計は予算内: {:,} 面".format(total3))
    bpy.data.objects.remove(stray, do_unlink=True)

    # 最小面数の下限が効くこと
    st.density_mode = 'MANUAL'
    st.edge_length = 5.0          # 粗すぎてほぼ 0 面になる設定
    st.min_faces = 250
    bases, _e = core.estimate_bases(st, [big, small])
    if any(n < 250 for _o, n in bases):
        _fail("min_faces floor not applied: {}".format(
            [(o.name, n) for o, n in bases]))
    print("  最小面数 250 の下限が適用される")


def test_refinalize():
    """造形を足して再度「出力用に統合」しても接合部が復元されること。

    回帰: 初回統合でベース名が <パーツ名>_sculpt に変わるため、名前から
    _joint / _src を引いていた頃は2回目で両方見失い、警告も出さずに
    ダボがリメッシュ精度の出力になっていた。
    """
    print("== re-finalize after more sculpting ==")
    base = bpy.data.objects.get("part_A_sculpt")
    if base is None:
        _fail("stashed base part_A_sculpt not found")
    if not base.get(core.RESULT_TAG):
        _fail("stashed base lost its result tag")
    if base.get(core.PART_PROP) != "part_A":
        _fail("part id missing on stashed base: {}".format(
            base.get(core.PART_PROP)))

    # SB_Sculpt を選択可能に戻す(通常はユーザがチェックを外す操作)
    _unexclude(core.SCULPT_COLLECTION)

    first_out = bpy.data.objects.get("part_A")
    if first_out is None or not first_out.get(core.FINAL_TAG):
        _fail("first output missing")
    n_objs = len([o for o in bpy.data.objects if o.type == 'MESH'])

    for o in bpy.context.selected_objects:
        o.select_set(False)
    base.select_set(True)
    bpy.context.view_layer.objects.active = base
    result = bpy.ops.sculptbase.finalize()
    if result != {'FINISHED'}:
        _fail("re-finalize returned {}".format(result))

    out = bpy.data.objects.get("part_A")
    if out is None or not out.get(core.FINAL_TAG):
        _fail("re-finalize did not produce part_A")
    if not _has_exact_vertex(out, DOWEL_CORNER):
        _fail("RE-FINALIZE LOST THE EXACT DOWEL — joint was not found")
    if core.count_boundary_edges(out.data) != 0:
        _fail("re-finalized mesh is not watertight")
    # 前回出力は置き換えられ、part_A.001 のような複製が増えていないこと
    if bpy.data.objects.get("part_A.001") is not None:
        _fail("re-finalize duplicated the output instead of replacing it")
    if base.name != "part_A_sculpt":
        _fail("base name drifted to {}".format(base.name))
    now = len([o for o in bpy.data.objects if o.type == 'MESH'])
    if now != n_objs:
        _fail("object count changed {} -> {}".format(n_objs, now))
    print("  dowel still exact, output replaced in place, names stable")


def test_double_convert_skipped():
    """変換済みを再変換しないこと(名前が _src_src_src に壊れる回帰)。"""
    print("== converting already-converted parts is skipped ==")
    _unexclude(core.SCULPT_COLLECTION)
    bases = [o for o in bpy.data.objects
             if o.get(core.RESULT_TAG) and o.name in bpy.context.scene.objects]
    if not bases:
        _fail("no converted bases to test with")
    for o in bpy.context.selected_objects:
        o.select_set(False)
    for o in bases:
        o.select_set(True)
    bpy.context.view_layer.objects.active = bases[0]
    names_before = {o.name for o in bpy.data.objects}

    result = bpy.ops.sculptbase.convert()
    if result != {'CANCELLED'}:
        _fail("expected CANCELLED for an all-converted selection, got "
              "{}".format(result))
    if {o.name for o in bpy.data.objects} != names_before:
        _fail("objects changed even though conversion should be skipped")
    if any("_src_src" in n for n in names_before):
        _fail("name corruption present")
    print("  {} converted parts skipped, no objects touched".format(
        len(bases)))


def test_no_joint_warning():
    """接合部が見つからない配置では警告を出すこと。"""
    print("== warns when parts are not in assembled position ==")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=1)
    a = bpy.context.active_object
    a.name = "far_A"
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=1,
                                         location=(20, 0, 0))
    b = bpy.context.active_object
    b.name = "far_B"

    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 0.25
    st.min_faces = 100
    st.levels = 1
    st.joint_distance = 0.01
    st.joint_margin = 0.02
    st.separate_joints = True

    a.select_set(True)
    b.select_set(True)
    bpy.context.view_layer.objects.active = a
    results, _n, warnings = core.convert_selection(bpy.context, st)
    if not any("接合部が見つかりません" in w for w in warnings):
        _fail("no warning about missing joints: {}".format(warnings))
    if any(o.get(core.HAS_JOINT_PROP) for o in results):
        _fail("joints should not have been found for separated parts")
    print("  warned: {}".format(warnings[-1][:48] + "..."))

    # 単体選択でも注意を出す
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=1)
    solo = bpy.context.active_object
    solo.select_set(True)
    bpy.context.view_layer.objects.active = solo
    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 0.25
    st.min_faces = 100
    st.levels = 1
    st.separate_joints = True
    _r, _n, warnings = core.convert_selection(bpy.context, st)
    if not any("1個" in w for w in warnings):
        _fail("no warning for a single-part selection: {}".format(warnings))
    print("  single-part selection also warned")


def test_progress_stages():
    print("== progress stages reported ==")
    sphere, cube = _make_parts()
    sphere.select_set(True)
    cube.select_set(True)
    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 0.18
    st.min_faces = 100
    st.levels = 2
    st.joint_distance = 0.02
    st.joint_margin = 0.3
    st.keep_source = False

    seen = []
    gen = core.iter_convert(bpy.context, st)  # noqa: これは段階実行の検証
    while True:
        try:
            frac, label = next(gen)
            seen.append((frac, label))
        except StopIteration:
            break
    if len(seen) < 8:
        _fail("expected many progress ticks, got {}".format(len(seen)))
    fracs = [f for f, _ in seen]
    if fracs != sorted(fracs) or not 0.0 <= fracs[0] <= fracs[-1] < 1.0:
        _fail("progress fractions not monotonic in [0,1): {}".format(fracs))
    if not any("形状転写" in lab for _, lab in seen):
        _fail("no transfer-stage label reported")
    print("  {} ticks, {:.0%}..{:.0%}, e.g. '{}'".format(
        len(seen), fracs[0], fracs[-1], seen[len(seen) // 2][1]))


def test_socket_not_filled():
    """ダボ穴(窪み)側のパーツで、穴が埋まらずに残ることを検証する。

    実データ(sculpt_hoge_260731_04)で発生した回帰: 接合部チャンクが
    「空洞そのもの」になる窪み側で、常に UNION していたため穴が埋まった。
    """
    print("== socket (female) side is not filled in ==")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # ソケット付きの箱: 上面から円筒を掘る
    bpy.ops.mesh.primitive_cube_add(size=2)
    box = bpy.context.active_object
    box.name = "socket_part"
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.25, depth=1.0,
                                        location=(0, 0, 0.8))
    drill = bpy.context.active_object
    mod = box.modifiers.new("hole", 'BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.object = drill
    with bpy.context.temp_override(object=box, active_object=box):
        bpy.ops.object.modifier_apply(modifier="hole")
    bpy.data.objects.remove(drill, do_unlink=True)

    # ソケットに差し込まれる相手パーツ(接合部検出のトリガー)
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.25, depth=0.6,
                                        location=(0, 0, 1.0))
    pin = bpy.context.active_object
    pin.name = "pin_part"

    def _is_open(ob, z):
        """(0,0,z) がメッシュの外(=穴が空いている)かを判定する。"""
        from mathutils import Vector
        bvh = core.build_bvh(ob)
        p = Vector((0, 0, z))
        d = Vector((0.577, 0.577, 0.577))
        n, origin = 0, p.copy()
        for _ in range(64):
            hit = bvh.ray_cast(origin, d)
            if hit[0] is None:
                break
            n += 1
            origin = hit[0] + d * 1e-5
        return n % 2 == 0

    probe_z = 0.6                      # ソケット内部(掘られた領域)
    if not _is_open(box, probe_z):
        _fail("test setup: socket should be hollow at z={}".format(probe_z))

    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.density_mode = 'MANUAL'
    st.edge_length = 0.10
    st.min_faces = 200
    st.levels = 2
    st.joint_distance = 0.02
    st.separate_joints = True
    st.joint_margin = 0.15
    st.joint_blend = 0.02
    st.keep_source = False

    box.select_set(True)
    pin.select_set(True)
    bpy.context.view_layer.objects.active = box
    if bpy.ops.sculptbase.convert() != {'FINISHED'}:
        _fail("convert failed on socket part")

    joint = bpy.data.objects.get("socket_part_joint")
    if joint is None:
        _fail("socket joint chunk not separated")
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(joint.data)
    vol = bm.calc_volume(signed=True)
    bm.free()
    print("  socket joint chunk signed volume: {:+.5f} (負なら窪み)".format(vol))
    if vol >= 0.0:
        _fail("socket chunk should have negative signed volume")

    for o in bpy.context.selected_objects:
        o.select_set(False)
    base = bpy.data.objects["socket_part"]
    base.select_set(True)
    bpy.context.view_layer.objects.active = base
    if bpy.ops.sculptbase.finalize() != {'FINISHED'}:
        _fail("finalize failed on socket part")

    out = bpy.data.objects["socket_part"]
    if core.count_boundary_edges(out.data) != 0:
        _fail("socket output is not watertight")
    if not _is_open(out, probe_z):
        _fail("SOCKET WAS FILLED IN — the dowel hole disappeared")
    print("  socket still hollow after finalize, output watertight")


def test_cap_marking():
    """蓋(CAP_LAYER)が実際に記録され、三角化されることを検証する。

    実データで発生した回帰: holes_fill が境界ループ全体を1枚の巨大 n-gon
    で塞ぎ、さらに蓋の記録が全て 0 になっていたため、統合時の押し出しが
    一度も走らずベース側の蓋と完全に重なってブーリアンが破綻していた。
    """
    print("== cap faces are marked and triangulated ==")
    import bmesh
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=1)
    obj = bpy.context.active_object
    # 上部のキャップを削って大きな境界ループを作る
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    doomed = [f for f in bm.faces if f.calc_center_median().z > 0.55]
    bmesh.ops.delete(bm, geom=doomed, context='FACES')
    bm.to_mesh(obj.data)
    bm.free()
    n_before = len(obj.data.polygons)
    n_bnd = core.count_boundary_edges(obj.data)
    if n_bnd < 20:
        _fail("test setup: expected a large boundary loop")

    n_cap = core.fill_holes_mesh(obj.data, cap_layer=core.CAP_LAYER)
    if core.count_boundary_edges(obj.data) != 0:
        _fail("mesh not closed after fill")
    if n_cap < 3:
        _fail("cap face count implausible: {}".format(n_cap))

    attr = obj.data.attributes.get(core.CAP_LAYER)
    if attr is None:
        _fail("cap attribute missing")
    marked = sum(1 for d in attr.data if d.value)
    if marked != n_cap:
        _fail("cap faces marked {} but {} were created".format(
            marked, n_cap))
    if marked == 0:
        _fail("NO cap faces were marked — extrusion would never run")
    # 巨大 n-gon ではなく三角形になっていること
    cap_sizes = {len(p.vertices) for p, d in
                 zip(obj.data.polygons, attr.data) if d.value}
    if cap_sizes != {3}:
        _fail("cap faces are not triangles: sizes {}".format(cap_sizes))
    print("  boundary {} edges -> {} triangular cap faces, all marked".format(
        n_bnd, marked))
    print("  faces {} -> {}".format(n_before, len(obj.data.polygons)))


def test_holes_fill_fallback():
    print("== hole fill fallback on non-planar loop ==")
    import bmesh
    from mathutils import Vector
    mesh = bpy.data.meshes.new("holey")
    bm = bmesh.new()
    # ねじれた6角ループ(holes_fill が扱いにくい形)を1枚の帯として作る
    ring = []
    import math
    for i in range(6):
        a = i * math.pi / 3
        z = 0.4 if i % 2 else -0.4
        ring.append(bm.verts.new(Vector((math.cos(a), math.sin(a), z))))
    top = [bm.verts.new(v.co + Vector((0, 0, 2))) for v in ring]
    for i in range(6):
        bm.faces.new((ring[i], ring[(i + 1) % 6],
                      top[(i + 1) % 6], top[i]))
    bm.to_mesh(mesh)
    bm.free()
    if core.count_boundary_edges(mesh) == 0:
        _fail("test setup: open tube should have boundaries")
    core.fill_holes_mesh(mesh)
    if core.count_boundary_edges(mesh) != 0:
        _fail("fallback fan-fill left boundary edges")
    print("  twisted open tube -> watertight")


def test_remask():
    print("== remask on plain meshes ==")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
    a = bpy.context.active_object
    bpy.ops.mesh.primitive_cube_add(size=2, location=(2.0, 0, 0))
    b = bpy.context.active_object
    a.select_set(True)
    b.select_set(True)
    st = bpy.context.scene.sculptbase
    st.joint_distance = 0.01
    result = bpy.ops.sculptbase.remask()
    if result != {'FINISHED'}:
        _fail("remask returned {}".format(result))
    vg = a.vertex_groups.get(core.JOINT_GROUP)
    if vg is None:
        _fail("remask did not create vertex group")
    # 接触面(x=+1)の4頂点だけが保護される
    masked = [v.index for v in a.data.vertices
              if v.co.x > 0.9 and _in_group(a, vg, v.index)]
    if len(masked) != 4:
        _fail("expected the 4 contact-face verts, got {}".format(len(masked)))
    print("  contact face verts masked: {}".format(len(masked)))


def _in_group(obj, vg, index):
    try:
        return vg.weight(index) > 0.5
    except RuntimeError:
        return False


def main():
    addon.register()
    try:
        test_convert()
        print()
        test_finalize()
        print()
        test_refinalize()
        print()
        test_double_convert_skipped()
        print()
        test_no_joint_warning()
        print()
        test_modifiers_applied()
        print()
        test_all_joint_warning()
        print()
        test_graceful_cancel()
        print()
        test_scale_sanity_warning()
        print()
        test_area_proportional_density()
        print()
        test_socket_not_filled()
        print()
        test_cap_marking()
        print()
        test_holes_fill_fallback()
        print()
        test_progress_stages()
        print()
        test_remask()
    finally:
        addon.unregister()
    print("\nALL SCULPTBASE TESTS PASSED")


if __name__ == "__main__":
    main()
