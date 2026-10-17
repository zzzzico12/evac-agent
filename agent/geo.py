"""浸水想定（国土地理院 重ねるハザードマップ）と避難所オープンデータを扱う。

- 浸水想定：洪水浸水想定区域（想定最大規模）のラスタタイルの画素色から浸水深の区分を読む
  https://disaportaldata.gsi.go.jp/hazardmap/
- 避難所：茨城県 避難施設情報（N2EM / G空間情報センター、政府標準利用規約）の県内全域に、
  scripts/prepare_shelters.py で浸水想定を付けたもの
"""

import io
import json
import math
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

from PIL import Image

# AgentCore Runtime の zip では agent のコードと同じ階層に data/ を同梱する。手元では repo 直下の data/ を使う
DATA_DIR = Path(__file__).resolve().parent / "data"
if not DATA_DIR.exists():
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"

TILE_BASE = "https://disaportaldata.gsi.go.jp/raster"
ZOOM = 16
FLOOD_LAYER = "01_flood_l2_shinsuishin_data"
COLLAPSE_LAYERS = ["01_flood_l2_kaokutoukai_hanran_data", "01_flood_l2_kaokutoukai_kagan_data"]

# 洪水浸水想定区域の凡例色 → (区分, 表示, 想定最大の深さ m)
DEPTH_CLASSES = {
    (247, 245, 169): (1, "0.5m未満", 0.5),
    (255, 216, 192): (2, "0.5m〜3.0m", 3.0),
    (255, 183, 183): (3, "3.0m〜5.0m", 5.0),
    (255, 145, 145): (4, "5.0m〜10.0m", 10.0),
    (242, 133, 201): (5, "10.0m〜20.0m", 20.0),
    (220, 122, 220): (6, "20.0m以上", 30.0),
}
NO_FLOOD = (0, "浸水想定なし", 0.0)

# 土砂災害警戒区域（国土地理院 重ねるハザードマップ）。黄系が警戒区域、赤系（緑成分が小さい）が特別警戒区域
LANDSLIDE_LAYERS = {
    "土石流": "05_dosekiryukeikaikuiki",
    "急傾斜地の崩壊": "05_kyukeishakeikaikuiki",
    "地すべり": "05_jisuberikeikaikuiki",
}
LANDSLIDE_LEVELS = {0: "区域外", 1: "土砂災害警戒区域", 2: "土砂災害特別警戒区域"}

# 歩行・移動速度（km/h）。道路の迂回を考慮して直線距離に係数を掛ける。
SPEED_KMH = {
    "walk": 4.0,
    "walk_slow": 2.5,   # 高齢者・乳幼児連れ
    "wheelchair": 2.5,  # 介助者あり
    "bicycle": 10.0,
    "car": 15.0,        # 避難時の渋滞を見込む
}
ROAD_FACTOR = 1.3


def _load_shelters() -> dict:
    path = DATA_DIR / "shelters.json"
    if not path.exists():
        return {}
    return {s["id"]: s for s in json.loads(path.read_text(encoding="utf-8"))["shelters"]}


SHELTERS = _load_shelters()


def _load_rivers() -> list[dict]:
    """主な河川の流路（OpenStreetMap）。各流路に外接矩形を付けて判定を速くする。"""
    path = DATA_DIR / "rivers.json"
    if not path.exists():
        return []
    rivers = json.loads(path.read_text(encoding="utf-8"))["rivers"]
    for r in rivers:
        lats = [c[0] for c in r["coords"]]
        lngs = [c[1] for c in r["coords"]]
        r["bbox"] = (min(lats), min(lngs), max(lats), max(lngs))
    return rivers


RIVERS = _load_rivers()


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _intersects(p1, p2, q1, q2) -> bool:
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def rivers_crossed(lat1: float, lng1: float, lat2: float, lng2: float) -> list[str]:
    """2地点の間を行き来するのに渡らなければならない川の名前。
    直線が川を奇数回横切るなら2地点は川の反対側にあり、どの道を通っても渡ることになる
    （蛇行で偶数回横切るだけなら同じ側なので数えない）。"""
    a, b = (lat1, lng1), (lat2, lng2)
    lo_lat, hi_lat = min(lat1, lat2), max(lat1, lat2)
    lo_lng, hi_lng = min(lng1, lng2), max(lng1, lng2)
    counts: dict[str, int] = {}
    for r in RIVERS:
        y0, x0, y1, x1 = r["bbox"]
        if y1 < lo_lat or y0 > hi_lat or x1 < lo_lng or x0 > hi_lng:
            continue
        pts = r["coords"]
        n = sum(1 for p, q in zip(pts, pts[1:]) if _intersects(a, b, tuple(p), tuple(q)))
        if n:
            counts[r["name"]] = counts.get(r["name"], 0) + n
    return sorted(name for name, n in counts.items() if n % 2 == 1)


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _tile_pixel(lat: float, lng: float, z: int = ZOOM) -> tuple[int, int, int, int]:
    n = 2**z
    x = (lng + 180) / 360 * n
    lat_r = math.radians(lat)
    y = (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n
    return int(x), int(y), int((x % 1) * 256), int((y % 1) * 256)


@lru_cache(maxsize=8192)
def _tile(layer: str, z: int, x: int, y: int) -> Image.Image | None:
    url = f"{TILE_BASE}/{layer}/{z}/{x}/{y}.png"
    req = urllib.request.Request(url, headers={"User-Agent": "evac-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return Image.open(io.BytesIO(res.read())).convert("RGBA")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # タイルが無い＝その範囲に該当区域なし
            return None
        raise


def _pixel(layer: str, lat: float, lng: float) -> tuple[int, int, int, int] | None:
    x, y, px, py = _tile_pixel(lat, lng)
    img = _tile(layer, ZOOM, x, y)
    if img is None:
        return None
    rgba = img.getpixel((px, py))
    return rgba if rgba[3] > 0 else None


def depth_rank(lat: float, lng: float) -> int:
    return _depth_class(lat, lng)[0]


def _depth_class(lat: float, lng: float) -> tuple[int, str, float]:
    rgba = _pixel(FLOOD_LAYER, lat, lng)
    if rgba is None:
        return NO_FLOOD
    rgb = rgba[:3]
    if rgb in DEPTH_CLASSES:
        return DEPTH_CLASSES[rgb]
    # 境界のアンチエイリアス等で色がずれた場合は最も近い凡例色に寄せる
    nearest = min(DEPTH_CLASSES, key=lambda c: sum((a - b) ** 2 for a, b in zip(c, rgb)))
    return DEPTH_CLASSES[nearest]


def flood_hazard(lat: float, lng: float) -> dict:
    rank, label, max_depth = _depth_class(lat, lng)
    collapse = any(_pixel(layer, lat, lng) is not None for layer in COLLAPSE_LAYERS)
    return {
        "in_flood_zone": rank > 0,
        "expected_depth": label,
        "depth_rank": rank,
        "max_depth_m": max_depth,
        "house_collapse_zone": collapse,
    }


def landslide_rank(lat: float, lng: float) -> tuple[int, list[str]]:
    """0: 区域外 / 1: 土砂災害警戒区域 / 2: 土砂災害特別警戒区域 と、該当する現象の種類を返す。"""
    rank, kinds = 0, []
    for kind, layer in LANDSLIDE_LAYERS.items():
        rgba = _pixel(layer, lat, lng)
        if rgba is None or rgba[3] < 128:  # 境界のにじみは無視する
            continue
        kinds.append(kind)
        rank = max(rank, 2 if rgba[1] < 100 else 1)
    return rank, kinds


def landslide_hazard(lat: float, lng: float) -> dict:
    rank, kinds = landslide_rank(lat, lng)
    return {
        "in_landslide_zone": rank > 0,
        "landslide_zone": LANDSLIDE_LEVELS[rank],
        "landslide_rank": rank,
        "landslide_types": kinds,
    }


def route_landslide_exposure(lat1: float, lng1: float, lat2: float, lng2: float, samples: int = 24) -> dict:
    """直線経路上をサンプリングし、土砂災害警戒区域を通る割合と最も危険な区分を返す。"""
    ranks = [
        landslide_rank(lat1 + (lat2 - lat1) * i / samples, lng1 + (lng2 - lng1) * i / samples)[0]
        for i in range(samples + 1)
    ]
    in_zone = sum(1 for r in ranks if r > 0)
    return {"ratio_in_landslide_zone": round(in_zone / len(ranks), 2), "max_landslide_zone": LANDSLIDE_LEVELS[max(ranks)]}


def route_flood_exposure(lat1: float, lng1: float, lat2: float, lng2: float, samples: int = 24) -> dict:
    """直線経路上をサンプリングし、浸水想定区域を通る割合と最大の浸水深区分を返す。"""
    classes = [
        _depth_class(lat1 + (lat2 - lat1) * i / samples, lng1 + (lng2 - lng1) * i / samples)
        for i in range(samples + 1)
    ]
    worst = max(classes, key=lambda c: c[0])
    in_zone = sum(1 for c in classes if c[0] > 0)
    return {
        "ratio_in_flood_zone": round(in_zone / len(classes), 2),
        "max_depth_rank": worst[0],
        "max_depth": worst[1],
    }
