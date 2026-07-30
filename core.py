"""SculptBase のコアロジック。

分割済みの高密度パーツ(三角スープ)を、マルチレゾ詳細造形に適した
「四角面ベース + Multires + 形状転写 + 接合部保護マスク」に変換する。

パイプライン(パーツごと):

1. 四角リメッシュ — ベースメッシュを生成する。エンジンは2種:
   * QUADWILD: QRemeshify 拡張(QuadWild + Bi-MDF ソルバー。
     Pietroni et al., "Reliable Feature-Line Driven Quad-Remeshing",
     SIGGRAPH 2021 / Heistermann et al. の Bi-MDF, SIGGRAPH Asia 2023)
     がインストールされていれば利用できる。
   * QUADRIFLOW: Blender 組み込み(Huang et al., SGP 2018)。依存なしで動く。
2. 形状転写 — ベースに Multires を付け、レベルごとに
   「細分化 → (サブディビ+シュリンクラップした参照メッシュを作る) →
   multires_reshape」を繰り返して元の高密度形状を各レベルに焼き込む。
   参照メッシュのサブディビは Multires と同じ Catmull-Clark なので
   頂点数・順序が一致し、reshape がそのまま通る。
3. 接合部保護 — 他パーツの表面から一定距離内にあるベース頂点を
   「接合部(分割面・ダボ)」として検出し、スカルプトマスク(=1.0)・
   頂点グループ・フェイスセットの三重で保護する。スカルプトブラシは
   マスク済み頂点を動かせないため、ダボを崩す事故を機械的に防ぐ。

接合部の原形保持(v0.2.0〜, 既定 ON):
   リメッシュ + Catmull-Clark 転写は近似なので、ダボのような小さく鋭い
   形状は僅かに崩れる。これを許容しないため、接合部(ダボ + 周辺スカート)
   の面をリメッシュ前に「<名前>_joint」オブジェクトへ分離し、元の
   ジオメトリを 1 頂点も変えずに保持する。本体側は穴を塞いでから
   リメッシュ・転写する。出力時(finalize)に Multires を適用した本体と
   _joint を exact ブーリアンで統合し、ダボはビット単位で元のまま出力される。

UI に依存しない関数のみを置き、ヘッドレステストから直接呼べるようにする。
"""

import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

RESULT_TAG = "sculptbase_result"
JOINT_TAG = "sculptbase_joint"
FINAL_TAG = "sculptbase_final"
JOINT_GROUP = "SB_JointGuard"
SOURCE_COLLECTION = "SB_Source"
SCULPT_COLLECTION = "SB_Sculpt"
MULTIRES_NAME = "SB_Multires"
JOINT_SUFFIX = "_joint"


# --------------------------------------------------------------------------- #
# 汎用ヘルパー
# --------------------------------------------------------------------------- #
def _override(context, active, selected):
    return context.temp_override(
        active_object=active,
        object=active,
        selected_objects=list(selected),
        selected_editable_objects=list(selected),
    )


def _bake_evaluated(context, obj):
    """``obj`` のメッシュをモディファイア適用済みの形に置き換える。"""
    context.view_layer.update()
    depsgraph = context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    new_mesh = bpy.data.meshes.new_from_object(eval_obj)
    old = obj.data
    obj.modifiers.clear()
    obj.data = new_mesh
    if old.users == 0:
        bpy.data.meshes.remove(old)


def duplicate_object(context, obj, name=None):
    dup = obj.copy()
    dup.data = obj.data.copy()
    if name:
        dup.name = name
        dup.data.name = name
    context.collection.objects.link(dup)
    return dup


def build_bvh(obj):
    """``obj`` のワールド空間 BVH ツリーを返す。"""
    mesh = obj.data
    mw = obj.matrix_world
    verts = [mw @ v.co for v in mesh.vertices]
    mesh.calc_loop_triangles()
    tris = [tuple(lt.vertices) for lt in mesh.loop_triangles]
    if not tris:
        return None
    return BVHTree.FromPolygons(verts, tris, all_triangles=True)


# --------------------------------------------------------------------------- #
# 四角リメッシュ エンジン
# --------------------------------------------------------------------------- #
def qremeshify_available():
    """QRemeshify 拡張(QuadWild + Bi-MDF)が使えるか。"""
    try:
        return bpy.ops.qremeshify.remesh.poll is not None
    except AttributeError:
        return False


def quad_remesh(context, obj, engine, target_faces):
    """``obj`` のメッシュを四角面ベースに置き換える。

    QUADWILD は QRemeshify のオペレーターに委譲する(密度等の詳細設定は
    QRemeshify 側のパネル設定に従う)。オペレーターが元オブジェクトを
    隠して新オブジェクトを作る流儀なので、結果のメッシュを ``obj`` に
    移し替えて後段(転写)のパイプラインを共通化する。
    """
    if engine == 'QUADWILD' and qremeshify_available():
        before = set(bpy.data.objects)
        with _override(context, obj, [obj]):
            bpy.ops.qremeshify.remesh()
        new = [o for o in bpy.data.objects if o not in before]
        if new:
            result = new[0]
            old = obj.data
            obj.data = result.data
            bpy.data.objects.remove(result, do_unlink=True)
            if old.users == 0:
                bpy.data.meshes.remove(old)
            obj.hide_set(False)
            obj.hide_viewport = False
        return
    with _override(context, obj, [obj]):
        bpy.ops.object.quadriflow_remesh(
            mode='FACES',
            target_faces=max(target_faces, 4),
            use_mesh_symmetry=False,
            use_preserve_sharp=False,
            use_preserve_boundary=False,
            preserve_attributes=False,
            smooth_normals=False,
            seed=0,
        )


# --------------------------------------------------------------------------- #
# 形状転写(Multires 焼き込み)
# --------------------------------------------------------------------------- #
def transfer_to_multires(context, base, dense, levels):
    """``base`` に Multires を付け、``dense`` の形状を各レベルへ焼き込む。"""
    mr = base.modifiers.new(MULTIRES_NAME, 'MULTIRES')
    for _lvl in range(1, levels + 1):
        with _override(context, base, [base]):
            bpy.ops.object.multires_subdivide(modifier=MULTIRES_NAME,
                                              mode='CATMULL_CLARK')
        ref = _make_reference(context, base, dense, _lvl)
        try:
            with _override(context, base, [base, ref]):
                bpy.ops.object.multires_reshape(modifier=MULTIRES_NAME)
        finally:
            mesh = ref.data
            bpy.data.objects.remove(ref, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
    mr.levels = min(1, levels)          # ビューポートは軽いレベルで表示
    mr.sculpt_levels = levels
    mr.render_levels = levels
    return mr


def _make_reference(context, base, dense, level):
    """``base`` のレベル0形状をサブディビ+シュリンクラップした参照を返す。

    Multires の Catmull-Clark 細分化と同一の頂点数・順序になるため、
    multires_reshape のコピー元として使える。
    """
    ref = bpy.data.objects.new("SB_ref", _base_level_mesh(base))
    ref.matrix_world = base.matrix_world.copy()
    context.collection.objects.link(ref)
    sub = ref.modifiers.new("SB_Sub", 'SUBSURF')
    sub.subdivision_type = 'CATMULL_CLARK'
    sub.levels = level
    sub.render_levels = level
    sw = ref.modifiers.new("SB_Wrap", 'SHRINKWRAP')
    sw.target = dense
    sw.wrap_method = 'TARGET_PROJECT'
    _bake_evaluated(context, ref)
    return ref


def _base_level_mesh(base):
    """Multires を無視した ``base`` のレベル0メッシュのコピーを返す。"""
    return base.data.copy()


# --------------------------------------------------------------------------- #
# 接合部の分離(原形保持)
# --------------------------------------------------------------------------- #
def fill_holes_mesh(mesh):
    """境界ループ(穴)を面で塞ぐ。塞いだ面の数を返す。"""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    boundary = [e for e in bm.edges if e.is_boundary]
    if not boundary:
        bm.free()
        return 0
    result = bmesh.ops.holes_fill(bm, edges=boundary, sides=0)
    n = len(result.get("faces", ()))
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return n


def count_boundary_edges(mesh):
    """境界エッジ数(0 なら水密の必要条件を満たす)。"""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    n = sum(1 for e in bm.edges if e.is_boundary)
    bm.free()
    return n


def _faces_subset_object(context, src, face_indices, keep, name):
    """``src`` のコピーから面集合の片側だけを残したオブジェクトを作る。

    ``keep`` が真なら ``face_indices`` の面を残し、偽なら取り除く。
    残った境界の穴は塞いで水密なソリッドにする。
    """
    obj = duplicate_object(context, src, name=name)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    doomed = [f for f in bm.faces if (f.index in face_indices) != keep]
    bmesh.ops.delete(bm, geom=doomed, context='FACES')
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context='VERTS')
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    fill_holes_mesh(obj.data)
    return obj


def separate_joint(context, dense, other_bvhs, distance):
    """``dense`` の接合部(ダボ+スカート)と本体を分けたコピーを返す。

    他パーツ表面から ``distance`` 以内の頂点を含む面を接合部とみなす。
    ``(body, joint)`` を返す。接合部が無い(または全面が接合部)場合は
    ``(None, None)``。``dense`` 自体は変更しない。
    """
    mesh = dense.data
    near = joint_vertex_indices(dense, other_bvhs, distance)
    if not near:
        return None, None
    joint_faces = {p.index for p in mesh.polygons
                   if any(v in near for v in p.vertices)}
    if not joint_faces or len(joint_faces) == len(mesh.polygons):
        return None, None
    body = _faces_subset_object(context, dense, joint_faces, False,
                                dense.name + "_bodytmp")
    joint = _faces_subset_object(context, dense, joint_faces, True,
                                 dense.name + "_jointtmp")
    return body, joint


# --------------------------------------------------------------------------- #
# 接合部(分割面・ダボ)の検出と保護
# --------------------------------------------------------------------------- #
def joint_vertex_indices(obj, other_bvhs, distance):
    """他パーツ表面から ``distance`` 以内にある頂点インデックス集合。"""
    mw = obj.matrix_world
    hits = set()
    for i, v in enumerate(obj.data.vertices):
        co = mw @ v.co
        for bvh in other_bvhs:
            found = bvh.find_nearest(co, distance)
            if found[0] is not None:
                hits.add(i)
                break
    return hits


def protect_joints(obj, joint_verts):
    """接合部頂点をマスク・頂点グループ・フェイスセットの三重で保護する。

    * ``.sculpt_mask`` = 1.0 — スカルプトブラシが動かせなくなる(実効の保護)
    * 頂点グループ ``SB_JointGuard`` — 可視化・再マスクやウェイト利用のため
    * フェイスセット — スカルプトモードでの表示/自動マスク連携のため
    """
    mesh = obj.data
    vg = obj.vertex_groups.get(JOINT_GROUP)
    if vg is None:
        vg = obj.vertex_groups.new(name=JOINT_GROUP)
    if joint_verts:
        vg.add(list(joint_verts), 1.0, 'REPLACE')

    mask = mesh.attributes.get(".sculpt_mask")
    if mask is None:
        mask = mesh.attributes.new(".sculpt_mask", 'FLOAT', 'POINT')
    for i in joint_verts:
        mask.data[i].value = 1.0

    fs = mesh.attributes.get(".sculpt_face_set")
    if fs is None:
        fs = mesh.attributes.new(".sculpt_face_set", 'INT', 'FACE')
    for poly in mesh.polygons:
        joint = all(v in joint_verts for v in poly.vertices)
        fs.data[poly.index].value = 2 if joint else 1
    return len(joint_verts)


# --------------------------------------------------------------------------- #
# ソース退避
# --------------------------------------------------------------------------- #
def stash_source(context, obj, coll_name=SOURCE_COLLECTION):
    """``obj`` を退避コレクションへ移し、ビューレイヤーから除外する。

    除外されたコレクションは依存グラフの評価対象外になるため、高密度
    ソースがシーンに残っていても操作を重くしない。
    """
    coll = bpy.data.collections.get(coll_name)
    if coll is None:
        coll = bpy.data.collections.new(coll_name)
        context.scene.collection.children.link(coll)
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    coll.objects.link(obj)

    def _find(layer):
        if layer.collection == coll:
            return layer
        for child in layer.children:
            found = _find(child)
            if found:
                return found
        return None

    layer = _find(context.view_layer.layer_collection)
    if layer:
        layer.exclude = True


# --------------------------------------------------------------------------- #
# パーツ変換(1個分のフルパイプライン)
# --------------------------------------------------------------------------- #
def _remove_object(obj):
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def convert_part(context, dense, settings, other_bvhs):
    """``dense`` パーツをスカルプト用ベースへ変換して返す。

    接合部の原形保持が有効で接合部が見つかった場合、ダボ+スカートは
    「<名前>_joint」として元ジオメトリのまま分離され、本体だけが
    リメッシュ・転写される。``dense`` 自体は SB_Source に退避される
    (keep_source が偽なら削除)。結果オブジェクトは元の名前を引き継ぐ。
    """
    name = dense.name
    dense.name = name + "_src"

    body = joint = None
    if getattr(settings, "separate_joints", True):
        body, joint = separate_joint(
            context, dense, other_bvhs,
            settings.joint_distance + settings.joint_margin)

    transfer_src = body if joint is not None else dense
    base = duplicate_object(context, transfer_src, name=name)

    quad_remesh(context, base, settings.engine, settings.target_faces)
    transfer_to_multires(context, base, transfer_src, settings.levels)

    # 接合部チャンク自身も保護対象に含めてベースをマスクする。
    guard_bvhs = list(other_bvhs)
    if joint is not None:
        jb = build_bvh(joint)
        if jb is not None:
            guard_bvhs.append(jb)
    joints = joint_vertex_indices(base, guard_bvhs, settings.joint_distance)
    n_protected = protect_joints(base, joints)

    if joint is not None:
        joint.name = name + JOINT_SUFFIX
        joint.data.name = joint.name
        joint[JOINT_TAG] = True
        _remove_object(body)                 # 転写targetの本体コピーは破棄
    if settings.keep_source:
        stash_source(context, dense)
    else:
        _remove_object(dense)

    base[RESULT_TAG] = True
    base["sculptbase_protected_verts"] = n_protected
    return base


# --------------------------------------------------------------------------- #
# 出力用の統合(Multires 適用 + 接合部ブーリアン)
# --------------------------------------------------------------------------- #
def finalize_part(context, base):
    """``base`` の Multires を適用し、_joint と統合した出力メッシュを返す。

    ベースと _joint は SB_Sculpt コレクションへ退避され(再編集用に温存)、
    出力オブジェクトが元の名前を引き継ぐ。接合部は exact ブーリアンで
    統合されるため、ダボのジオメトリは元のまま出力される。
    """
    name = base.name
    joint = bpy.data.objects.get(name + JOINT_SUFFIX)

    out = duplicate_object(context, base, name=name + "_out_tmp")
    mr = out.modifiers.get(MULTIRES_NAME)
    if mr is not None:
        mr.levels = mr.sculpt_levels or mr.total_levels
    _bake_evaluated(context, out)

    warn = None
    if joint is not None:
        mod = out.modifiers.new("SB_Union", 'BOOLEAN')
        mod.operation = 'UNION'
        mod.solver = 'EXACT'
        mod.object = joint
        _bake_evaluated(context, out)
    if count_boundary_edges(out.data) != 0:
        warn = "'{}' の出力メッシュに境界エッジが残っています".format(name)

    base.name = name + "_sculpt"
    stash_source(context, base, SCULPT_COLLECTION)
    if joint is not None:
        stash_source(context, joint, SCULPT_COLLECTION)
    out.name = name
    out.data.name = name
    out[FINAL_TAG] = True
    if RESULT_TAG in out:
        del out[RESULT_TAG]
    return out, warn


def finalize_selection(context):
    """選択中の変換済みベースを一括で出力用に統合する。"""
    bases = [o for o in context.selected_objects if o.get(RESULT_TAG)]
    if not bases:
        raise RuntimeError(
            "変換済みのベース(スカルプトベースに変換した結果)を選択してください。")
    results = []
    warnings = []
    for base in bases:
        out, warn = finalize_part(context, base)
        results.append(out)
        if warn:
            warnings.append(warn)
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in results:
        obj.select_set(True)
    if results:
        context.view_layer.objects.active = results[0]
    return results, warnings


def convert_selection(context, settings):
    """選択中のメッシュ全部を変換する。``(results, n_protected)`` を返す。"""
    parts = [o for o in context.selected_objects if o.type == 'MESH']
    if not parts:
        raise RuntimeError("変換対象のメッシュを選択してください。")
    bvhs = {o: build_bvh(o) for o in parts}
    results = []
    total_protected = 0
    for dense in parts:
        others = [bvh for o, bvh in bvhs.items()
                  if o is not dense and bvh is not None]
        base = convert_part(context, dense, settings, others)
        results.append(base)
        total_protected += base.get("sculptbase_protected_verts", 0)
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in results:
        obj.select_set(True)
    if results:
        context.view_layer.objects.active = results[0]
    return results, total_protected
