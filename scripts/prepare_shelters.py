"""茨城県 避難施設情報（オープンデータ・県内全域）に浸水想定を付けて data/shelters.json を作る。

出典：茨城県 避難施設情報（指定緊急避難場所、指定避難所、福祉避難所）
      N2EM / G空間情報センター（政府標準利用規約）
      https://www.geospatial.jp/ckan/dataset/ibaraki-shelter
浸水想定：国土地理院 重ねるハザードマップ 洪水浸水想定区域（想定最大規模）・家屋倒壊等氾濫想定区域
土砂災害：国土地理院 重ねるハザードマップ 土砂災害警戒区域（土石流・急傾斜地の崩壊・地すべり）

使い方（agent の venv で。国土地理院のタイルを数千枚取得するので数分かかる）:
  agent/.venv/bin/python scripts/prepare_shelters.py
"""

import csv
import io
import json
import math
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import geo  # noqa: E402

SOURCE_URL = (
    "https://www.geospatial.jp/ckan/dataset/b3e1a2b5-0178-4fb7-8c4e-931c156f7ebb"
    "/resource/dc682370-9364-4b16-a80d-3f8898e0abdf/download/.csv"
)
RAW_PATH = ROOT / "data" / "ibaraki_shelter.csv"
OUT_PATH = ROOT / "data" / "shelters.json"
CITIES_PATH = ROOT / "data" / "cities.json"

FLAG = {"0": "対応無し", "1": "対応", "9": "不明"}

# 全国地方公共団体コード（チェックディジットなし、先頭0なし）→ 市町村名
CITY_NAMES = {
    "8201": "水戸市", "8202": "日立市", "8203": "土浦市", "8204": "古河市", "8205": "石岡市",
    "8207": "結城市", "8208": "龍ケ崎市", "8210": "下妻市", "8211": "常総市", "8212": "常陸太田市",
    "8214": "高萩市", "8215": "北茨城市", "8216": "笠間市", "8217": "取手市", "8219": "牛久市",
    "8220": "つくば市", "8221": "ひたちなか市", "8222": "鹿嶋市", "8223": "潮来市", "8224": "守谷市",
    "8225": "常陸大宮市", "8226": "那珂市", "8227": "筑西市", "8228": "坂東市", "8229": "稲敷市",
    "8230": "かすみがうら市", "8231": "桜川市", "8232": "神栖市", "8233": "行方市", "8234": "鉾田市",
    "8235": "つくばみらい市", "8236": "小美玉市", "8302": "茨城町", "8309": "大洗町", "8310": "城里町",
    "8341": "東海村", "8364": "大子町", "8442": "美浦村", "8443": "阿見町", "8447": "河内町",
    "8521": "八千代町", "8542": "五霞町", "8546": "境町", "8564": "利根町",
}
# 元データで市町村コード欄が壊れている行は、出典組織の名称から補う
CODE_BY_SOURCE_ORG = {"八千代町": "8521"}

RING = [(r, k * math.pi / 4) for r in (60, 120) for k in range(8)]
RING_LANDSLIDE = [(60, k * math.pi / 4) for k in range(8)]


def nearby_max_depth_rank(lat: float, lng: float) -> int:
    """周辺（60m・120m の8方位）で最も深い浸水深の区分。河道や堤防の際にある施設を見分けるため。"""
    cos = math.cos(math.radians(lat))
    return max(
        geo.depth_rank(lat + r * math.sin(a) / 110_540, lng + r * math.cos(a) / (111_320 * cos))
        for r, a in RING
    )


def nearby_landslide_rank(lat: float, lng: float) -> int:
    """周辺60m（8方位）で最も危険な土砂災害警戒区域の区分。崖や沢のすぐ下にある施設を見分けるため。"""
    cos = math.cos(math.radians(lat))
    return max(
        geo.landslide_rank(lat + r * math.sin(a) / 110_540, lng + r * math.cos(a) / (111_320 * cos))[0]
        for r, a in RING_LANDSLIDE
    )


def build(item: tuple[int, list[str]]) -> dict:
    i, r = item
    lat, lng = float(r[3]), float(r[4])
    hz = geo.flood_hazard(lat, lng)
    ls = geo.landslide_hazard(lat, lng)
    code = r[0] if r[0] in CITY_NAMES else CODE_BY_SOURCE_ORG[r[10]]
    return {
        "id": f"S{i:04d}",
        "city": CITY_NAMES[code],
        "name": r[1],
        "address": r[2],
        "lat": lat,
        "lng": lng,
        "emergency_place": FLAG[r[6]],
        "designated_shelter": FLAG[r[7]],
        "welfare_shelter": FLAG[r[8]],
        "flood_depth": hz["expected_depth"],
        "flood_depth_rank": hz["depth_rank"],
        "house_collapse_zone": hz["house_collapse_zone"],
        "nearby_max_depth_rank": nearby_max_depth_rank(lat, lng),
        "landslide_zone": ls["landslide_zone"],
        "landslide_rank": ls["landslide_rank"],
        "landslide_types": ls["landslide_types"],
        "nearby_landslide_rank": nearby_landslide_rank(lat, lng),
        "source_updated": r[12],
    }


def write_cities() -> None:
    """市町村コード（国土地理院の逆ジオコーダーが返す5桁、例：08201）→ 市町村名。画面で「いまいる場所」の市町村を決めるのに使う。"""
    CITIES_PATH.write_text(
        json.dumps({f"0{code}": name for code, name in CITY_NAMES.items()}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    write_cities()
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as res:
        text = res.read().decode("utf-8-sig")
    RAW_PATH.write_text(text, encoding="utf-8")  # 元データをそのまま保存

    rows = list(csv.reader(io.StringIO(text)))[1:]
    usable = [r for r in rows if r[3] and r[4]]
    print(f"{len(rows)}件中、座標のある{len(usable)}件を処理します")

    done = 0
    shelters = []
    # 国土地理院のサーバーに負荷をかけすぎないよう並列数は控えめにする
    with ThreadPoolExecutor(max_workers=8) as pool:
        for s in pool.map(build, enumerate(usable, start=1)):
            shelters.append(s)
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(usable)}")

    OUT_PATH.write_text(
        json.dumps(
            {
                "source": "茨城県 避難施設情報（N2EM / G空間情報センター、政府標準利用規約）",
                "source_url": "https://www.geospatial.jp/ckan/dataset/ibaraki-shelter",
                "hazard_source": "国土地理院 重ねるハザードマップ 洪水浸水想定区域（想定最大規模）・土砂災害警戒区域",
                "shelters": shelters,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    safe = sum(1 for s in shelters if s["flood_depth_rank"] == 0)
    ls_out = sum(1 for s in shelters if s["landslide_rank"] == 0)
    cities = len({s["city"] for s in shelters})
    print(f"\n{cities}市町村・{len(shelters)}件を書き出し（浸水想定なし {safe}件、土砂災害警戒区域外 {ls_out}件）→ {OUT_PATH}")


if __name__ == "__main__":
    main()
