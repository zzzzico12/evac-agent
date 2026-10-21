"""茨城県内のアンダーパス（線路や道路の下をくぐる道）を OpenStreetMap から取得し、data/underpasses.json を作る。

大雨のときに冠水しやすい場所として、避難経路がアンダーパスを通るかを調べるために使う。
OpenStreetMap でわかるのは「道が地下を通っている」ことだけで、冠水しやすさや、いま冠水しているかはわからない。
出典：© OpenStreetMap contributors（ODbL）

使い方:
  agent/.venv/bin/python scripts/prepare_underpasses.py
"""

import json
import math
import re
import urllib.parse
import urllib.request
from pathlib import Path

from prepare_rivers import _simplify

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "underpasses.json"

SIMPLIFY_M = 5
# 山を抜けるトンネルは冠水しにくいので除く。アンダーパスは短いので、長いものと名前でわかるものを外す
MAX_LENGTH_M = 200
EXCLUDE_NAME = re.compile(r"トンネル|共同溝|加速器")
# 避難で通る道（高速道路・農道などは除く）
HIGHWAYS = [
    "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link",
    "unclassified", "residential", "living_street", "service", "pedestrian", "footway", "path", "cycleway", "steps",
]

# 地下を通る道路（tunnel=yes）。建物の中の通路（building_passage）や暗渠（culvert）は含めない
QUERY = f"""
[out:json][timeout:180];
area["name"="茨城県"]["admin_level"="4"]->.ibaraki;
way["highway"~"^({'|'.join(HIGHWAYS)})$"]["tunnel"="yes"](area.ibaraki);
out tags geom;
"""


def _length_m(pts: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot((b[0] - a[0]) * 110_540, (b[1] - a[1]) * 111_320 * math.cos(math.radians(a[0])))
        for a, b in zip(pts, pts[1:])
    )


def main() -> None:
    req = urllib.request.Request(
        "https://overpass-api.de/api/interpreter",
        data=urllib.parse.urlencode({"data": QUERY}).encode(),
        headers={"User-Agent": "evac-agent/1.0"},
    )
    with urllib.request.urlopen(req, timeout=240) as res:
        elements = json.load(res)["elements"]

    items = []
    for e in elements:
        pts = [(round(g["lat"], 6), round(g["lon"], 6)) for g in e.get("geometry", [])]
        if len(pts) < 2:
            continue
        t = e["tags"]
        name = t.get("name") or t.get("tunnel:name") or ""
        if _length_m(pts) > MAX_LENGTH_M or EXCLUDE_NAME.search(name):
            continue
        items.append({
            "id": e["id"],
            "name": name,
            "highway": t["highway"],
            "coords": _simplify(pts, SIMPLIFY_M),
        })

    OUT_PATH.write_text(
        json.dumps(
            {"source": "© OpenStreetMap contributors (ODbL)", "underpasses": items},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    named = sum(1 for i in items if i["name"])
    print(f"{len(items)}か所（名前あり {named}）→ {OUT_PATH}")


if __name__ == "__main__":
    main()
