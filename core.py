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
   リメッシュ・転写する。

出力の組み立て(v0.5.0〜, ``rebuild_from_source``):
   出力は**ソースメッシュそのもの**を土台にする。接合部の頂点は 1 つも
   動かさず、それ以外の頂点だけを Multires 適用後の造形面へ投影する。
   トポロジーが変わらないので必ず水密で、ダボはソースとビット単位で同一。

   v0.4.0 までは _joint を本体にブーリアン統合していたが、本体側と _joint
   側の「蓋」は必ず同一面で重なるため退化し、実データで接合部が壊れた
   (円形に破断する / ダボ穴が埋まる)。蓋の押し出しで回避しようとしても、
   蓋が複数の島に分かれて向きが逆になる場合に破綻するため、ブーリアンに
   依存しないこの方式へ置き換えた。

UI に依存しない関数のみを置き、ヘッドレステストから直接呼べるようにする。
"""

import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

RESULT_TAG = "sculptbase_result"
JOINT_TAG = "sculptbase_joint"
FINAL_TAG = "sculptbase_final"
# パーツの識別子。オブジェクト名は工程ごとに変わる(foo -> foo_sculpt)ので、
# 関連オブジェクト(_joint / _src)を引くときは名前ではなくこれを使う。
PART_PROP = "sculptbase_part"
HAS_JOINT_PROP = "sculptbase_has_joint"
SCULPT_SUFFIX = "_sculpt"
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
def _transfer_level(context, base, dense, level):
    """Multires を1段細分化し、``dense`` の形状をそのレベルへ焼き込む。"""
    with _override(context, base, [base]):
        bpy.ops.object.multires_subdivide(modifier=MULTIRES_NAME,
                                          mode='CATMULL_CLARK')
    ref = _make_reference(context, base, dense, level)
    try:
        with _override(context, base, [base, ref]):
            bpy.ops.object.multires_reshape(modifier=MULTIRES_NAME)
    finally:
        mesh = ref.data
        bpy.data.objects.remove(ref, do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def _finish_multires(mr, levels):
    mr.levels = min(1, levels)          # ビューポートは軽いレベルで表示
    mr.sculpt_levels = levels
    mr.render_levels = levels


def transfer_to_multires(context, base, dense, levels):
    """``base`` に Multires を付け、``dense`` の形状を各レベルへ焼き込む。"""
    mr = base.modifiers.new(MULTIRES_NAME, 'MULTIRES')
    for _lvl in range(1, levels + 1):
        _transfer_level(context, base, dense, _lvl)
    _finish_multires(mr, levels)
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
def _boundary_loops(edges):
    """境界エッジ集合を連結ループごとの頂点リストに分解する。"""
    adj = {}
    for e in edges:
        for v in e.verts:
            adj.setdefault(v, []).append(e)
    unvisited = set(edges)
    loops = []
    while unvisited:
        e = unvisited.pop()
        loop = [e.verts[0], e.verts[1]]
        while True:
            nxt = None
            for cand in adj.get(loop[-1], ()):
                if cand in unvisited:
                    nxt = cand
                    break
            if nxt is None:
                break
            unvisited.discard(nxt)
            loop.append(nxt.other_vert(loop[-1]))
        if loop[0] == loop[-1]:
            loop.pop()
        loops.append(loop)
    return loops


def _match_winding(face, olds):
    """``face`` の向きを、隣接する既存面 ``olds`` と整合させる。

    共有エッジを両者が同じ向きにたどっていれば裏返っているので反転する。
    全体の recalc は使わない — 元パッチの向き(パーツ外向き)を保つことが、
    後段で「出っ張り(ダボ)か窪み(ダボ穴)か」を符号付き体積で判定する
    根拠になるため。
    """
    for loop in face.loops:
        for other in loop.edge.link_faces:
            if other is face or other not in olds:
                continue
            for ol in other.loops:
                if ol.edge is loop.edge:
                    if ol.vert is loop.vert:      # 同じ向き = 不整合
                        face.normal_flip()
                    return True
    return False


def _fill_holes_bm(bm):
    """``bm`` の境界ループを塞ぎ、新しくできた面の**インデックス集合**を返す。

    面オブジェクトではなくインデックスを返すのは、呼び出し側でカスタム
    データレイヤーを作るとメッシュ要素の同一性判定が当てにならなくなる
    ため(蓋の記録が全て 0 になる不具合の原因だった)。
    """
    boundary = [e for e in bm.edges if e.is_boundary]
    if not boundary:
        return set()
    olds = set(bm.faces)
    new_faces = list(bmesh.ops.holes_fill(bm, edges=boundary,
                                          sides=0).get("faces", ()))
    remaining = [e for e in bm.edges if e.is_boundary]
    if remaining:
        # holes_fill が扱えないループ(非平面・ねじれ)は中心頂点を立てた
        # 扇形で確実に塞ぐ。
        for loop in _boundary_loops(remaining):
            if len(loop) < 3:
                continue
            center = Vector()
            for v in loop:
                center += v.co
            center /= len(loop)
            cv = bm.verts.new(center)
            for a, b in zip(loop, loop[1:] + loop[:1]):
                try:
                    new_faces.append(bm.faces.new((a, b, cv)))
                except ValueError:
                    pass
    for f in new_faces:
        _match_winding(f, olds)
    # holes_fill は境界ループ全体を1枚の巨大 n-gon で塞ぐことがある
    # (実データでは430角形)。そのままではブーリアンが破綻するので
    # 三角化して素性のよい面にしておく。
    if new_faces:
        tri = bmesh.ops.triangulate(bm, faces=new_faces)
        new_faces = list(tri.get("faces", ())) or new_faces
    bm.normal_update()
    bm.faces.index_update()
    return {f.index for f in new_faces}


def fill_holes_mesh(mesh, cap_layer=None):
    """境界ループ(穴)を面で塞ぐ。塞いだ面の数を返す。

    ``cap_layer`` に名前を渡すと、塞いだ面を 1、それ以外を 0 とする面
    整数属性を書き込む(出力統合で「蓋」だけを押し出すために使う)。
    印は面インデックスで付ける — レイヤー作成をまたいだ要素の同一性
    判定は信頼できず、以前は蓋が1つも記録されなかった。
    """
    bm = bmesh.new()
    bm.from_mesh(mesh)
    cap_indices = _fill_holes_bm(bm)
    if cap_layer is not None:
        layer = bm.faces.layers.int.get(cap_layer) \
            or bm.faces.layers.int.new(cap_layer)
        bm.faces.ensure_lookup_table()
        for f in bm.faces:
            f[layer] = 1 if f.index in cap_indices else 0
    n = len(cap_indices)
    if n or cap_layer is not None:
        bm.to_mesh(mesh)
        mesh.update()
    bm.free()
    return n


def count_boundary_edges(mesh):
    """境界エッジ数(0 なら水密の必要条件を満たす)。"""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    n = sum(1 for e in bm.edges if e.is_boundary)
    bm.free()
    return n


CAP_LAYER = "SB_cap"


def _faces_subset_object(context, src, face_indices, keep, name):
    """``src`` のコピーから面集合の片側だけを残したオブジェクトを作る。

    ``keep`` が真なら ``face_indices`` の面を残し、偽なら取り除く。
    残った境界の穴は塞いで水密なソリッドにし、塞いだ「蓋」の面は
    ``CAP_LAYER`` に記録する。元パッチの面の向きは変更しない。
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
    fill_holes_mesh(obj.data, cap_layer=CAP_LAYER)
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


def _convert_part_stages(context, dense, settings, other_bvhs):
    """``convert_part`` の段階実行ジェネレーター。

    重い処理の直前に ``(パーツ内進捗 0..1, ラベル)`` を yield し、最後に
    変換結果のベースオブジェクトを return する(モーダルの進捗表示用)。
    """
    name = dense.name
    dense.name = name + "_src"

    body = joint = None
    if getattr(settings, "separate_joints", True):
        yield 0.02, "接合部を分離"
        body, joint = separate_joint(
            context, dense, other_bvhs,
            settings.joint_distance + settings.joint_margin)

    transfer_src = body if joint is not None else dense
    base = duplicate_object(context, transfer_src, name=name)

    yield 0.1, "四角リメッシュ"
    quad_remesh(context, base, settings.engine, settings.target_faces)
    # リメッシュ器が閉じ損ねた場合に備えて塞ぐ(後段は水密前提)。
    filled = fill_holes_mesh(base.data)
    if filled:
        print("[SculptBase] '{}': リメッシュ後の穴 {} 面を補修".format(
            name, filled))

    levels = settings.levels
    mr = base.modifiers.new(MULTIRES_NAME, 'MULTIRES')
    for lvl in range(1, levels + 1):
        yield 0.15 + 0.75 * (lvl - 1) / levels, "形状転写 L{}/{}".format(
            lvl, levels)
        _transfer_level(context, base, transfer_src, lvl)
    _finish_multires(mr, levels)

    yield 0.92, "接合部マスク"
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
    # 接合部を分離した場合、出力時にダボを厳密復元する土台がソースなので
    # 必ず退避する(除外コレクション行きなので評価コストは無い)。
    if settings.keep_source or joint is not None:
        stash_source(context, dense)
    else:
        _remove_object(dense)

    base[RESULT_TAG] = True
    base[PART_PROP] = name
    base[HAS_JOINT_PROP] = joint is not None
    base["sculptbase_protected_verts"] = n_protected
    return base


def convert_part(context, dense, settings, other_bvhs):
    """``dense`` パーツをスカルプト用ベースへ変換して返す(同期実行)。

    接合部の原形保持が有効で接合部が見つかった場合、ダボ+スカートは
    「<名前>_joint」として元ジオメトリのまま分離され、本体だけが
    リメッシュ・転写される。``dense`` 自体は SB_Source に退避される
    (keep_source が偽なら削除)。結果オブジェクトは元の名前を引き継ぐ。
    """
    gen = _convert_part_stages(context, dense, settings, other_bvhs)
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


# --------------------------------------------------------------------------- #
# 出力用の統合(Multires 適用 + 接合部ブーリアン)
# --------------------------------------------------------------------------- #
def _point_inside(tree, point):
    """レイの交差回数の偶奇で ``point`` がソリッド内部かを判定する。"""
    direction = Vector((0.7071, 0.5, 0.5)).normalized()
    origin = point.copy()
    hits = 0
    for _ in range(64):
        loc = tree.ray_cast(origin, direction)[0]
        if loc is None:
            break
        hits += 1
        origin = loc + direction * 1e-5
    return hits % 2 == 1


def verify_joint_region(out, source, joint, probe):
    """統合結果が接合部でソースと食い違う割合を返す(0.0〜1.0)。

    接合部チャンクの各頂点から法線方向 ``probe`` だけ離れた点について、
    ソースと出力で内部/外部が一致するかを調べる。ダボ穴が埋まる・ダボが
    欠けるといった破綻は、この不一致率として現れる。
    """
    tree_src, tree_out = build_bvh(source), build_bvh(out)
    if tree_src is None or tree_out is None:
        return 0.0
    mw = joint.matrix_world
    nmat = mw.inverted().transposed().to_3x3()
    verts = joint.data.vertices
    if not len(verts):
        return 0.0
    step = max(1, len(verts) // 400)          # 最大400点のサンプリング
    bad = total = 0
    for i in range(0, len(verts), step):
        v = verts[i]
        n = (nmat @ v.normal).normalized()
        p = (mw @ v.co) + n * probe
        total += 1
        if _point_inside(tree_src, p) != _point_inside(tree_out, p):
            bad += 1
    return bad / max(total, 1)


def _linear_subdivide(obj, times):
    """形状を変えずに(リニア分割で)面数を増やす。"""
    for _ in range(max(0, times)):
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1,
                                  use_grid_fill=True, smooth=0.0)
        bm.to_mesh(obj.data)
        bm.free()
    if times > 0:
        obj.data.update()


def _joint_weights(out, joint, inner, outer):
    """造形面へ寄せる度合い(0=ソースのまま, 1=造形面)を頂点グループに書く。

    接合部から ``inner`` 以内は 0、``outer`` 以遠は 1、その間はスムーズに
    補間する。距離計算は接合部のバウンディングボックス近傍の頂点だけに
    絞る(遠くの頂点は問答無用で 1)ので、実データでも一瞬で終わる。
    """
    vg = out.vertex_groups.get(JOINT_GROUP) or \
        out.vertex_groups.new(name=JOINT_GROUP)
    mesh = out.data
    n = len(mesh.vertices)
    vg.add(list(range(n)), 1.0, 'REPLACE')
    if joint is None:
        return vg, 0

    tree = build_bvh(joint)
    if tree is None:
        return vg, 0
    jmin, jmax = world_aabb(joint)
    pad = Vector((outer, outer, outer))
    jmin -= pad
    jmax += pad

    mw = out.matrix_world
    protected = 0
    span = max(outer - inner, 1e-9)
    for v in mesh.vertices:
        co = mw @ v.co
        if not all(jmin[i] <= co[i] <= jmax[i] for i in range(3)):
            continue
        near = tree.find_nearest(co, outer)[0]
        if near is None:
            continue
        d = (near - co).length
        if d <= inner:
            vg.add([v.index], 0.0, 'REPLACE')
            protected += 1
        else:
            t = (d - inner) / span
            vg.add([v.index], t * t * (3.0 - 2.0 * t), 'REPLACE')
    return vg, protected


def world_aabb(obj):
    """ワールド空間の軸平行バウンディングボックス ``(min, max)``。"""
    mw = obj.matrix_world
    corners = [mw @ Vector(c) for c in obj.bound_box]
    mins = Vector((min(c[i] for c in corners) for i in range(3)))
    maxs = Vector((max(c[i] for c in corners) for i in range(3)))
    return mins, maxs


def rebuild_from_source(context, base, source, joint, settings):
    """ソースの形状を土台に、造形面を非接合部だけへ転写した出力を作る。

    ブーリアンを使わない:

    * 出力はソースメッシュそのもの(トポロジー不変)なので**必ず水密**
    * 接合部(ダボ・ダボ穴・分割面)の頂点は**1つも動かさない**ので、
      形状はソースとビット単位で同一
    * それ以外の頂点だけを、Multires を適用した造形面へ投影する

    補完的な蓋どうしが必ず重なるブーリアン方式(v0.4.0 まで)は、実データで
    退化して破綻していたため、この方式に置き換えた。
    """
    sculpt = duplicate_object(context, base, name=base.name + "_evaltmp")
    mr = sculpt.modifiers.get(MULTIRES_NAME)
    if mr is not None:
        mr.levels = mr.sculpt_levels or mr.total_levels
    _bake_evaluated(context, sculpt)

    out = duplicate_object(context, source, name=base.name + "_out_tmp")
    # 造形面のディテールを拾えるだけの密度に(形状は変えずに)引き上げる
    times = 0
    while (len(out.data.polygons) * (4 ** times)
           < len(sculpt.data.polygons)) and times < 2:
        times += 1
    _linear_subdivide(out, times)

    inner = settings.joint_distance + settings.joint_margin
    outer = inner + max(settings.joint_blend, 1e-9)
    vg, protected = _joint_weights(out, joint, inner, outer)

    # 投影はシュリンクラップ モディファイア(C 実装)に任せる。頂点グループの
    # ウェイトがそのまま影響度になるので、接合部(0)は 1 頂点も動かない。
    mod = out.modifiers.new("SB_Project", 'SHRINKWRAP')
    mod.target = sculpt
    mod.wrap_method = 'PROJECT'
    mod.use_negative_direction = True
    mod.use_positive_direction = True
    mod.vertex_group = vg.name
    _bake_evaluated(context, out)

    _remove_object(sculpt)
    return out, len(out.data.vertices) - protected, protected


def finalize_part(context, base, settings):
    """``base`` の Multires を適用し、_joint と統合した出力メッシュを返す。

    ベースと _joint は SB_Sculpt コレクションへ退避され(再編集用に温存)、
    出力オブジェクトが元の名前を引き継ぐ。接合部はブーリアンで統合される
    ため、ダボのジオメトリは元のまま出力される。
    """
    # ベース名は初回統合で "<パーツ名>_sculpt" に変わるため、関連オブジェクトは
    # 名前ではなく識別子から引く(造形を足して再統合しても迷子にならない)。
    name = base.get(PART_PROP)
    if not name:
        name = base.name
        if name.endswith(SCULPT_SUFFIX):
            name = name[:-len(SCULPT_SUFFIX)]
    joint = bpy.data.objects.get(name + JOINT_SUFFIX)
    source = bpy.data.objects.get(name + "_src")

    warn = None
    if source is None:
        # ソースが無いと接合部を厳密に復元できない。Multires を適用した
        # 造形面をそのまま出力する(ダボはリメッシュ精度どまり)。
        out = duplicate_object(context, base, name=name + "_out_tmp")
        mr = out.modifiers.get(MULTIRES_NAME)
        if mr is not None:
            mr.levels = mr.sculpt_levels or mr.total_levels
        _bake_evaluated(context, out)
        fill_holes_mesh(out.data)
        if joint is not None:
            warn = ("'{}' のソース(SB_Source)が見つからないため、接合部を"
                    "厳密に復元できませんでした。「ソースを退避して残す」を"
                    "有効にして変換し直してください").format(name)
    else:
        out, moved, protected = rebuild_from_source(
            context, base, source, joint, settings)
        print("[SculptBase] '{}': {} 頂点を造形面へ投影 / {} 頂点を"
              "接合部として保持".format(name, moved, protected))
        if count_boundary_edges(out.data) != count_boundary_edges(source.data):
            warn = "'{}' の出力メッシュの水密性がソースと異なります".format(
                name)
        elif joint is not None and settings.verify_joints:
            probe = max(settings.joint_distance + settings.joint_margin,
                        max(joint.dimensions) * 0.01)
            ratio = verify_joint_region(out, source, joint, probe)
            if ratio > 0.05:
                warn = ("'{}' の接合部がソースと {:.0%} 食い違っています"
                        "(ダボ/ダボ穴が正しく再現されていない可能性)"
                        ).format(name, ratio)

    if joint is None and settings.separate_joints and not warn:
        warn = ("'{}' には接合部が検出されていません。ダボはリメッシュ精度の"
                "ままです(変換時に全パーツを組み立て位置のまま選択して"
                "いたか確認してください)").format(name)

    # 再統合(造形を足してもう一度出力)に備え、前回この機能で作った出力を
    # 置き換える。SculptBase 自身が付けた印を持つものだけを対象にする。
    prev = bpy.data.objects.get(name)
    if (prev is not None and prev is not base and prev is not out
            and prev.get(FINAL_TAG) and prev.get(PART_PROP) == name):
        _remove_object(prev)

    if base.name != name + SCULPT_SUFFIX:
        base.name = name + SCULPT_SUFFIX
    stash_source(context, base, SCULPT_COLLECTION)
    if joint is not None:
        stash_source(context, joint, SCULPT_COLLECTION)
    out.name = name
    out.data.name = name
    out[FINAL_TAG] = True
    out[PART_PROP] = name
    if RESULT_TAG in out:
        del out[RESULT_TAG]
    return out, warn


def _select_only(context, objects):
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    if objects:
        context.view_layer.objects.active = objects[0]


def iter_finalize(context, settings):
    """出力統合の段階実行ジェネレーター(進捗表示用)。

    各パーツの直前に ``(進捗 0..1, ラベル)`` を yield し、完了時に
    ``(results, warnings)`` を return する。
    """
    bases = [o for o in context.selected_objects if o.get(RESULT_TAG)]
    if not bases:
        raise RuntimeError(
            "変換済みのベース(スカルプトベースに変換した結果)を選択してください。")
    results = []
    warnings = []
    for i, base in enumerate(bases):
        yield i / len(bases), "統合 {} ({}/{})".format(
            base.name, i + 1, len(bases))
        out, warn = finalize_part(context, base, settings)
        if out is not None:
            results.append(out)
        if warn:
            warnings.append(warn)
    _select_only(context, results)
    return results, warnings


def finalize_selection(context, settings):
    """選択中の変換済みベースを一括で出力用に統合する(同期実行)。"""
    gen = iter_finalize(context, settings)
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


def iter_convert(context, settings):
    """変換の段階実行ジェネレーター(進捗表示用)。

    重い処理の直前に ``(全体進捗 0..1, ラベル)`` を yield し、完了時に
    ``(results, n_protected)`` を return する。
    """
    selected = [o for o in context.selected_objects if o.type == 'MESH']
    # 変換済み・接合部チャンク・出力済みを二重に変換すると名前が壊れる
    # (実データで "Part_00_src_src_src" が発生した)ため除外する。
    parts = [o for o in selected
             if not (o.get(RESULT_TAG) or o.get(FINAL_TAG)
                     or o.get(JOINT_TAG))]
    skipped = len(selected) - len(parts)
    if not parts:
        if skipped:
            raise RuntimeError(
                "選択中の {} 個はすべて変換済みです。造形を続けるなら"
                "そのままスカルプトし、出力するなら「出力用に統合」を"
                "使ってください。".format(skipped))
        raise RuntimeError("変換対象のメッシュを選択してください。")

    warnings = []
    if skipped:
        warnings.append(
            "変換済みの {} 個を除外しました(二重変換を防ぐため)".format(
                skipped))
    if settings.separate_joints and len(parts) < 2:
        warnings.append(
            "パーツが1個だけなので接合部を検出できません。ダボを厳密に"
            "保持するには、噛み合う相手のパーツも一緒に選択してください")

    bvhs = {o: build_bvh(o) for o in parts}
    results = []
    total_protected = 0
    n = len(parts)
    for i, dense in enumerate(parts):
        others = [bvh for o, bvh in bvhs.items()
                  if o is not dense and bvh is not None]
        gen = _convert_part_stages(context, dense, settings, others)
        base = None
        while base is None:
            try:
                frac, label = next(gen)
                yield (i + frac) / n, "{} ({}/{}) — {}".format(
                    dense.name.removesuffix("_src"), i + 1, n, label)
            except StopIteration as stop:
                base = stop.value
        results.append(base)
        total_protected += base.get("sculptbase_protected_verts", 0)

    # 接合部が1つも見つからないパーツは、ダボがリメッシュ精度どまりになる。
    # 多くは「組み立て位置から動かした後に変換した」「一部しか選んでいない」
    # のどちらかなので、その場で気づけるように知らせる。
    if settings.separate_joints and len(parts) >= 2:
        none_found = [o for o in results if not o.get(HAS_JOINT_PROP)]
        if none_found:
            warnings.append(
                "{} 個のパーツで接合部が見つかりませんでした({} など)。"
                "パーツを組み立て位置のまま、噛み合う相手ごと選択して"
                "変換し直してください".format(
                    len(none_found), none_found[0].name))

    _select_only(context, results)
    return results, total_protected, warnings


def convert_selection(context, settings):
    """選択中のメッシュ全部を変換する(同期実行)。"""
    gen = iter_convert(context, settings)
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value
