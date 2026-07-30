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
    st.target_faces = 1000
    st.levels = 2
    st.joint_distance = 0.02
    st.separate_joints = True
    st.joint_margin = 0.3
    st.union_inset = 0.005
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


def test_progress_stages():
    print("== progress stages reported ==")
    sphere, cube = _make_parts()
    sphere.select_set(True)
    cube.select_set(True)
    st = bpy.context.scene.sculptbase
    st.engine = 'QUADRIFLOW'
    st.target_faces = 500
    st.levels = 2
    st.joint_distance = 0.02
    st.joint_margin = 0.3
    st.keep_source = False

    seen = []
    gen = core.iter_convert(bpy.context, st)
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
