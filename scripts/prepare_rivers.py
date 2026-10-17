"""茨城県の主な河川の流路を OpenStreetMap から取得し、簡略化して data/rivers.json を作る。

洪水のときに「避難経路が川を渡るか」を判定するために使う。
出典：© OpenStreetMap contributors（ODbL）

使い方:
  agent/.venv/bin/python scripts/prepare_rivers.py
"""

import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "rivers.json"

# 警報の対象にしている河川と、その主な支流・湖
RIVER_NAMES = [
    "那珂川", "久慈川", "鬼怒川", "利根川", "小貝川", "桜川", "涸沼川", "大北川", "花貫川",
    "恋瀬川", "里川", "山田川", "緒川", "藤井川", "中川", "田川", "常陸利根川", "新利根川", "北浦", "西浦",
]
BBOX = (35.7, 139.6, 37.0, 140.9)  # 茨城県とその周辺
SIMPLIFY_M = 25  # 形を保ったまま点を間引く許容誤差（m）

QUERY = f"""
[out:json][timeout:180];
way["waterway"="river"]["name"~"^({'|'.join(RIVER_NAMES)})$"]{BBOX};
out geom;
"""


def _xy(lat: float, lng: float) -> tuple[float, float]:
    return lng * 111_320 * math.cos(math.radians(36.3)), lat * 110_540


def _simplify(points: list[tuple[float, float]], tol_m: float) -> list[tuple[float, float]]:
    """Douglas-Peucker 法で点を間引く。"""
    if len(points) < 3:
        return points
    (ax, ay), (bx, by) = _xy(*points[0]), _xy(*points[-1])
    dx, dy = bx - ax, by - ay
    norm = math.hypot(dx, dy) or 1e-9
    worst, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        px, py = _xy(*points[i])
        d = abs(dy * (px - ax) - dx * (py - ay)) / norm
        if d > worst:
            worst, idx = d, i
    if worst <= tol_m:
        return [points[0], points[-1]]
    return _simplify(points[: idx + 1], tol_m)[:-1] + _simplify(points[idx:], tol_m)


def main() -> None:
    req = urllib.request.Request(
        "https://overpass-api.de/api/interpreter",
        data=urllib.parse.urlencode({"data": QUERY}).encode(),
        headers={"User-Agent": "evac-agent/1.0"},
    )
    with urllib.request.urlopen(req, timeout=240) as res:
        elements = json.load(res)["elements"]

    rivers = []
    before = after = 0
    for e in elements:
        pts = [(round(g["lat"], 5), round(g["lon"], 5)) for g in e.get("geometry", [])]
        if len(pts) < 2:
            continue
        simple = _simplify(pts, SIMPLIFY_M)
        before += len(pts)
        after += len(simple)
        rivers.append({"name": e["tags"]["name"], "coords": simple})

    OUT_PATH.write_text(
        json.dumps(
            {"source": "© OpenStreetMap contributors (ODbL)", "rivers": rivers},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    names = sorted({r["name"] for r in rivers})
    print(f"{len(rivers)}本の流路（{len(names)}河川）、{before}点 → {after}点に簡略化 → {OUT_PATH}")


if __name__ == "__main__":
    main()
