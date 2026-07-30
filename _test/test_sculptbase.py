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


def _make_parts():
    """細かい球(パーツA)と、表面に食い込む立方体(パーツB)を作る。"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=1)
    sphere = bpy.context.active_object
    sphere.name = "part_A"
    sphere.scale = (1.0, 1.0, 1.4)
    bpy.ops.object.transform_apply(scale=True)

    bpy.ops.mesh.primitive_cube_add(size=0.6, location=(1.05, 0, 0))
    cube = bpy.context.active_object
    cube.name = "part_B"
    return sphere, cube


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

    # 形状転写の確認: トップレベル評価時の寸法が元とほぼ一致する
    mr.levels = 2
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    dims = base.evaluated_get(depsgraph).dimensions
    for i in range(3):
        if abs(dims[i] - dense_dims[i]) / dense_dims[i] > 0.05:
            _fail("transferred shape deviates: {} vs {}".format(
                tuple(dims), tuple(dense_dims)))
    print("  multires L2 dims {} ~= source {}".format(
        tuple(round(d, 3) for d in dims),
        tuple(round(d, 3) for d in dense_dims)))

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
        test_remask()
    finally:
        addon.unregister()
    print("\nALL SCULPTBASE TESTS PASSED")


if __name__ == "__main__":
    main()
