"""WAKAREME テストピース WK-01（ケーブルフック）の3Dモデル（STL）を作る。
条件を守らないと失敗する「分かれ目」を、わざと入れてある（README.md を参照）。
実行：uv run --no-project --with manifold3d --with numpy python samples/wk01/make_model.py
単位は mm。X＝幅、Y＝奥行き、Z＝高さ（Z=0 がベッド）。
"""
import math
from pathlib import Path

import numpy as np
from manifold3d import CrossSection, JoinType, Manifold

OUT = Path(__file__).with_name("wk01_cable_hook.stl")


def box(x0, y0, z0, x1, y1, z1):
    return Manifold.cube([x1 - x0, y1 - y0, z1 - z0]).translate([x0, y0, z0])


def prism_yz(points_yz, x0, x1):
    """YZ 平面の多角形を、X 方向に x0〜x1 だけ押し出す。"""
    cs = CrossSection([points_yz])
    m = cs.extrude(x1 - x0)            # XY の形を Z へ押し出したもの
    # (x, y, z) → (z, x, y)：押し出し方向を X に、形を YZ に向ける
    return m.transform([[0, 0, 1, x0], [1, 0, 0, 0], [0, 1, 0, 0]])


# 1. 底板 50×30×3（角R3）＋ M5 用の穴 Ø5.2 ×2（穴は縮みやすい＝寸法の分かれ目）
plate = CrossSection.square([44, 24]).translate([3, 3]).offset(3, JoinType.Round, circular_segments=48)
base = plate.extrude(3)
for x in (6, 44):
    base = base - Manifold.cylinder(10, 2.6, circular_segments=64).translate([x, 8, -1])

# 2. 後ろの壁 50×4×27 ＋ 幅20×高さ8の窓（窓の上が20mmのブリッジ＝冷却・ブリッジ設定の分かれ目）
wall = box(0, 26, 3, 50, 30, 30) - box(25, 25, 6, 45, 31, 14)

# 3. アーム（前へ16mm張り出す・厚さ4）。上面と先の立ち上がりが意匠面
arm = box(10, 10, 26, 40, 26, 30)
lip = box(10, 10, 30, 40, 13, 34)

# 4. アームの下：3区画で張り出し角を変える（垂直からの角度 35° / 60° / 90°＝サポートの分かれ目）
def gusset(angle_from_vertical, x0, x1):
    h = 16 / math.tan(math.radians(angle_from_vertical))  # 16mm 張り出すのに必要な高さ
    return prism_yz([(26, 26), (10, 26), (26, 26 - h)], x0, x1)

g35 = gusset(35, 10, 20)   # 急な斜面：サポートなしでもきれい
g60 = gusset(60, 20, 30)   # ゆるい斜面：サポートなしだと下面が荒れる・垂れる
# 30〜40 は斜面なし＝真横（90°）：サポートがないと崩れる

part = base + wall + arm + lip + g35 + g60

# STL（バイナリ）で書き出す
mesh = part.to_mesh()
v = np.asarray(mesh.vert_properties)[:, :3].astype(np.float32)
f = np.asarray(mesh.tri_verts)
tri = v[f]
n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
data = np.zeros(len(f), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
data["n"], data["v"] = n, tri
with OUT.open("wb") as fp:
    fp.write(b"WAKAREME WK-01 cable hook (mm)".ljust(80, b" "))
    fp.write(np.uint32(len(f)).tobytes())
    fp.write(data.tobytes())

bb = part.bounding_box()
print(f"書き出し：{OUT.name}  三角形 {len(f)}  体積 {part.volume() / 1000:.1f} cm3  "
      f"大きさ {bb[3] - bb[0]:.1f} x {bb[4] - bb[1]:.1f} x {bb[5] - bb[2]:.1f} mm  閉じた立体: {part.status()}")
