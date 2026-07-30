"""SculptBase — リトポ・形状転写・接合部保護アドオン(Blender 拡張)。

分割済みの高密度パーツを「四角面ベース + Multires + 接合部マスク」に
変換し、分割後の詳細造形(sculpt フェーズ)を軽いデータで行えるようにする。
"""

from . import core, operators, ui


def register():
    operators.register()
    ui.register()


def unregister():
    ui.unregister()
    operators.unregister()
