"""WK-01 の2D図面（三面図・第三角法・尺度2:1・A4横）を PDF と PNG で書き出す。
実行：uv run --no-project --with matplotlib python samples/wk01/make_drawing.py
形は make_model.py と同じ寸法で描いている（寸法を変えたら両方直す）。
"""
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Arc, Circle, FancyBboxPatch

HERE = Path(__file__).parent
for f in ("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"):
    if Path(f).exists():
        font_manager.fontManager.addfont(f)
plt.rcParams["font.family"] = ["Hiragino Sans", "Hiragino Kaku Gothic ProN", "sans-serif"]

S = 2.0                              # 尺度 2:1
H35 = 16 / math.tan(math.radians(35))  # 35°斜面の高さ
H60 = 16 / math.tan(math.radians(60))  # 60°斜面の高さ
fig = plt.figure(figsize=(11.69, 8.27))
ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 297); ax.set_ylim(0, 210); ax.set_aspect("equal"); ax.axis("off")
VIS = dict(color="black", lw=1.1)
HID = dict(color="black", lw=0.6, ls=(0, (4, 2)))
THIN = dict(color="black", lw=0.4)
RED = "#c0392b"


class View:
    def __init__(self, ox, oy):
        self.ox, self.oy = ox, oy

    def p(self, u, v):
        return self.ox + u * S, self.oy + v * S

    def line(self, pts, **st):
        xs, ys = zip(*[self.p(*q) for q in pts]); ax.plot(xs, ys, solid_capstyle="round", **(st or VIS))

    def rect(self, u0, v0, u1, v1, **st):
        self.line([(u0, v0), (u1, v0), (u1, v1), (u0, v1), (u0, v0)], **st)

    def text(self, u, v, s, **kw):
        x, y = self.p(u, v); ax.text(x, y, s, **{"fontsize": 7, "ha": "center", "va": "center", **kw})

    def dim(self, a, b, off, label, vertical=False):
        """寸法線。a・b は (u, v)。off は外へ出す量（図面上mm）。"""
        (x1, y1), (x2, y2) = self.p(*a), self.p(*b)
        if vertical:
            x = max(x1, x2) + off if off > 0 else min(x1, x2) + off
            for (xx, yy) in ((x1, y1), (x2, y2)):
                ax.plot([xx, x + (1.5 if off > 0 else -1.5)], [yy, yy], **THIN)
            ax.annotate("", (x, y1), (x, y2), arrowprops=dict(arrowstyle="<|-|>", lw=.5, color="black", shrinkA=0, shrinkB=0, mutation_scale=6))
            ax.text(x + (1.2 if off > 0 else -1.2), (y1 + y2) / 2, label, fontsize=7, rotation=90, ha="left" if off > 0 else "right", va="center")
        else:
            y = max(y1, y2) + off if off > 0 else min(y1, y2) + off
            for (xx, yy) in ((x1, y1), (x2, y2)):
                ax.plot([xx, xx], [yy, y + (1.5 if off > 0 else -1.5)], **THIN)
            ax.annotate("", (x1, y), (x2, y), arrowprops=dict(arrowstyle="<|-|>", lw=.5, color="black", shrinkA=0, shrinkB=0, mutation_scale=6))
            ax.text((x1 + x2) / 2, y + (1.0 if off > 0 else -1.0), label, fontsize=7, ha="center", va="bottom" if off > 0 else "top")

    def mark(self, u, v, n, dx=0, dy=0):
        """分かれ目の番号（赤丸）。"""
        x, y = self.p(u, v); X, Y = x + dx, y + dy
        if dx or dy:
            ax.plot([x, X], [y, Y], color=RED, lw=.6)
        ax.add_patch(Circle((X, Y), 3.0, fc="white", ec=RED, lw=1)); ax.text(X, Y, str(n), color=RED, fontsize=7.5, ha="center", va="center", fontweight="bold")


# ---------- 正面図（X–Z）：手前（Y=0）から見る ----------
F = View(28, 36)
ax.text(*F.p(25, -9), "正面図", fontsize=9, ha="center")
F.rect(0, 0, 50, 3)                                  # 底板
F.line([(0, 3), (0, 30), (50, 30), (50, 3)])         # 壁
F.rect(25, 6, 45, 14)                                # 窓（ブリッジ）
F.rect(10, 26, 40, 34)                               # アーム＋立ち上がり（手前の面）
F.line([(10, 3 + 0.2), (10, 26)]); F.line([(20, 3.2), (20, 26)])          # 35°斜面
F.line([(20, 26 - H60), (30, 26 - H60), (30, 26)])                         # 60°斜面
for x in (6, 44):                                    # 穴（かくれ線）
    F.line([(x - 2.6, 0), (x - 2.6, 3)], **HID); F.line([(x + 2.6, 0), (x + 2.6, 3)], **HID)
F.text(15, 14, "35°", fontsize=6.5, color="#333"); F.text(25, 21.5, "60°", fontsize=6.5, color="#333"); F.text(35, 22.5, "90°", fontsize=6.5, color="#333")
F.dim((0, 0), (50, 0), -7, "50")
F.dim((0, 0), (0, 34), -7, "34", vertical=True)
F.dim((0, 0), (0, 3), -15, "3", vertical=True)
F.dim((50, 26), (50, 30), 4, "4", vertical=True)
F.dim((50, 30), (50, 34), 4, "4", vertical=True)
F.dim((50, 0), (50, 26), 13, "26", vertical=True)
F.dim((25, 14), (45, 14), 3, "20（窓）")
F.dim((45, 6), (45, 14), 2.5, "8", vertical=True)
F.dim((0, 6), (25, 6), -2.5, "25")
F.dim((10, 34), (40, 34), 4, "30（アーム）")
F.mark(35, 26, 2, dx=8, dy=-16)       # ② 90°の張り出し
F.mark(35, 14, 3, dx=8, dy=-6)        # ③ ブリッジ
F.mark(6, 1.5, 4, dx=-10, dy=-12)     # ④ 穴

# ---------- 平面図（X–Y）：上から見る（第三角法：正面図の上） ----------
T = View(28, 36 + 34 * S + 22)
ax.text(*T.p(25, 33.5), "平面図", fontsize=9, ha="center")
r = 3
pts = []
for cx, cy, a0 in ((47, 3, -90), (47, 27, 0), (3, 27, 90), (3, 3, 180)):
    pts += [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in range(a0, a0 + 91, 10)]
T.line(pts + [pts[0]])
for x in (6, 44):
    ax.add_patch(Circle(T.p(x, 8), 2.6 * S, fill=False, **VIS))
    ax.plot(*zip(T.p(x - 4, 8), T.p(x + 4, 8)), color="black", lw=.3, ls="-."); ax.plot(*zip(T.p(x, 4), T.p(x, 12)), color="black", lw=.3, ls="-.")
T.line([(0, 26), (10, 26)]); T.line([(40, 26), (50, 26)])           # 壁の前の縁（アームの所は面がつながる）
T.rect(10, 10, 40, 26)                                              # アーム上面（意匠面）
T.line([(10, 13), (40, 13)])                                        # 立ち上がり
T.line([(25, 26), (25, 30)], **HID); T.line([(45, 26), (45, 30)], **HID)   # 窓（かくれ線）
T.text(25, 19.5, "意匠面（上向き）", fontsize=6.5, color=RED)
T.dim((0, 0), (0, 30), -7, "30", vertical=True)
T.dim((50, 10), (50, 26), 5, "16（張り出し）", vertical=True)
T.dim((50, 10), (50, 13), 13, "3", vertical=True)
T.dim((0, 8), (6, 8), -3, "6")
T.dim((44, 8), (50, 8), -3, "6")
T.dim((44, 0), (44, 8), 7, "8", vertical=True)
x, y = T.p(44, 8); ax.annotate("2-Ø5.2 貫通（M5用）", (x + 3.5, y + 3.5), (x + 18, y - 15), fontsize=7, arrowprops=dict(arrowstyle="-", lw=.5))
ax.text(*T.p(1.2, 1.2), "R3", fontsize=6.5)
T.mark(25, 22, 1, dx=-30, dy=8)        # ① 向き・意匠面
T.mark(6, 8, 4, dx=-12, dy=10)

# ---------- 右側面図（Y–Z）：右から見る（正面図の右） ----------
R = View(28 + 50 * S + 26, 36)
ax.text(*R.p(15, -9), "右側面図", fontsize=9, ha="center")
R.rect(0, 0, 30, 3)
R.line([(26, 3), (26, 30), (30, 30), (30, 3)])
R.line([(26, 26), (10, 26), (10, 34), (13, 34), (13, 30), (26, 30)])     # アーム端＋立ち上がり
R.line([(10, 26), (26, 26 - H60)])                                        # 60°斜面の線
R.line([(10, 26), (26, 26 - H35)])                                        # 35°斜面の線
R.line([(26, 6), (30, 6)], **HID); R.line([(26, 14), (30, 14)], **HID)   # 窓
R.dim((26, 30), (30, 30), 3, "4（壁）")
R.dim((10, 34), (26, 34), 9, "16")
R.dim((10, 34), (13, 34), 3, "3")
R.dim((0, 0), (30, 0), -7, "30")
R.text(21, 22, "60°", fontsize=6.5); R.text(15, 15, "35°", fontsize=6.5)
ax.add_patch(Arc(R.p(10, 26), 14, 14, theta1=270, theta2=270 + 55, lw=.4))
R.mark(14, 21, 2, dx=-14, dy=-2)
R.mark(20, 10, 5, dx=-12, dy=-8)       # ⑤ サポート外し

# ---------- 分かれ目（注記） ----------
nx, ny = 196, 196
ax.text(nx, ny, "分かれ目（守らないと失敗する条件）", fontsize=9, fontweight="bold", va="top")
notes = [
    ("1", "向き：底板をベッドに。アーム上面・立ち上がりは意匠面。\n逆さや横置きにすると意匠面にサポート跡が付く。"),
    ("2", "張り出し角：35°はサポートなしでOK、60°は荒れる、\n90°（アーム右1/3）はサポートが要る。目安は45°。"),
    ("3", "ブリッジ20mm：冷却ファン強め・速度控えめ。\n「ブリッジにはサポートしない」にしないと窓に入る。"),
    ("4", "穴Ø5.2：縦の穴は細く仕上がりやすい。M5が通るか\n確かめ、きつければ穴補正かドリル5.2で整える。"),
    ("5", "サポート外し：ニッパーの平らな面を部品側に、根元\nから。意匠面に当たる側は当て板。400番で軽く。"),
]
yy = ny - 9
for n, t in notes:
    ax.add_patch(Circle((nx + 3, yy - 1.5), 3.0, fc="white", ec=RED, lw=1)); ax.text(nx + 3, yy - 1.5, n, color=RED, fontsize=7.5, ha="center", va="center", fontweight="bold")
    ax.text(nx + 8, yy + 1, t, fontsize=7, va="top", linespacing=1.4); yy -= 15

# ---------- 表題欄 ----------
bx, by, bw, bh = 217, 9, 72, 35
ax.add_patch(FancyBboxPatch((bx, by), bw, bh, boxstyle="square,pad=0", fill=False, lw=1))
rows = [("品名", "WAKAREME テストピース WK-01"), ("図番", "WK-01"), ("材料", "PLA ／ ノズル0.4・積層0.2"),
        ("尺度・単位", "2:1 ／ mm"), ("投影法", "第三角法"), ("作成", "2026-11-08　WAKAREME AI")]
for i, (k, v) in enumerate(rows):
    y = by + bh - (i + 1) * bh / len(rows)
    if i: ax.plot([bx, bx + bw], [y + bh / len(rows), y + bh / len(rows)], **THIN)
    ax.text(bx + 1.5, y + 2.0, k, fontsize=5.8, color="#555"); ax.text(bx + 15.5, y + 2.0, v, fontsize=6.2)
ax.plot([bx + 14, bx + 14], [by, by + bh], **THIN)
# 投影記号（第三角法）
sx, sy = 200, 12
ax.add_patch(Circle((sx + 12, sy), 3.2, fill=False, lw=.6)); ax.add_patch(Circle((sx + 12, sy), 1.4, fill=False, lw=.6))
ax.plot([sx, sx + 6, sx + 6, sx, sx], [sy - 1.5, sy - 3.2, sy + 3.2, sy + 1.5, sy - 1.5], color="black", lw=.6)
ax.add_patch(FancyBboxPatch((5, 5), 287, 200, boxstyle="square,pad=0", fill=False, lw=1.4))

for ext in ("pdf", "png"):
    fig.savefig(HERE / f"wk01_drawing.{ext}", dpi=200 if ext == "png" else None)
print("書き出し：wk01_drawing.pdf / wk01_drawing.png")
