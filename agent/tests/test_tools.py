"""ツール単体の動作確認（Bedrock・AWS は不要。国土地理院のタイル取得にインターネット接続が必要）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geo  # noqa: E402
from tools import assess_home_risk, estimate_route, save_evacuation_plan, search_shelters  # noqa: E402
from ui_events import CTX  # noqa: E402


def call(t, **kw):
    return t._tool_func(**kw)


def homes():
    # テスト用の架空の世帯（実際の浸水想定に合わせた地点に置いている）
    path = Path(__file__).resolve().parent / "fixtures" / "households.json"
    return {h["id"]: h for h in json.loads(path.read_text(encoding="utf-8"))}


def test_shelter_data_loaded():
    assert len(geo.SHELTERS) > 2000
    assert len({s["city"] for s in geo.SHELTERS.values()}) == 44
    assert all(s["emergency_place"] in ("対応", "対応無し", "不明") for s in geo.SHELTERS.values())


def test_home_risks_match_scenarios():
    h = homes()
    naka = call(assess_home_risk, lat=h["H001"]["lat"], lng=h["H001"]["lng"], home_floors=2)
    assert naka["depth_rank"] >= 3 and not naka["vertical_evacuation_possible"]
    sato = call(assess_home_risk, lat=h["H002"]["lat"], lng=h["H002"]["lng"], home_floors=1)
    assert sato["depth_rank"] >= 2 and not sato["vertical_evacuation_possible"]
    nguyen = call(assess_home_risk, lat=h["H003"]["lat"], lng=h["H003"]["lng"], home_floors=1)
    assert nguyen["depth_rank"] >= 2
    takahashi = call(assess_home_risk, lat=h["H004"]["lat"], lng=h["H004"]["lng"], home_floors=2)
    assert not takahashi["in_flood_zone"]


def test_nearest_designated_shelters_are_flooded_for_nguyen():
    h = homes()["H003"]
    near = call(search_shelters, lat=h["lat"], lng=h["lng"])["shelters"]
    designated = [s for s in near if s["designated_shelter"] == "対応"]
    # 最寄りの指定避難所（柳河小など）は浸水想定区域内 → エージェントは遠くの安全な所を選ぶ必要がある
    assert designated[0]["flood_depth"] != "浸水想定なし"
    assert any(s["flood_depth"] == "浸水想定なし" for s in designated)


def test_joso_finds_shelters_across_city_border():
    h = homes()["H005"]
    assert call(assess_home_risk, lat=h["lat"], lng=h["lng"], home_floors=2)["depth_rank"] >= 3
    near = call(search_shelters, lat=h["lat"], lng=h["lng"])["shelters"]
    # 常総市内の近い施設は浸水想定区域かその際にあり、つくばみらい市の安全な施設も候補に入る
    assert any(s["city"] == "つくばみらい市" and s["flood_depth"] == "浸水想定なし" for s in near)


def test_landslide_home_and_shelters():
    h = homes()["H007"]
    risk = call(assess_home_risk, lat=h["lat"], lng=h["lng"], home_floors=2)
    assert risk["in_landslide_zone"] and risk["landslide_types"]
    res = call(search_shelters, lat=h["lat"], lng=h["lng"], hazard="landslide")
    safe = [s for s in res["shelters"] if s["safe_for_landslide"]]
    assert safe and all(s["landslide_zone"] == "区域外" and s["nearby_landslide_rank"] == 0 for s in safe)
    # 大子町は指定避難所の区分がすべて「不明」。それでも候補が出ること
    assert all(s["designated_shelter"] != "対応無し" for s in safe)
    assert "error" in call(search_shelters, lat=h["lat"], lng=h["lng"], hazard="tsunami")


def test_flood_avoids_crossing_river():
    h = homes()["H003"]  # 青柳町（那珂川の北岸）
    res = call(search_shelters, lat=h["lat"], lng=h["lng"], hazard="flood")["shelters"]
    gokenn = next(s for s in res if s["id"] == "S0002")  # 五軒小学校（南岸）
    assert gokenn["rivers_to_cross"] == ["那珂川"] and not gokenn["safe_for_flood"]
    assert any(s["safe_for_flood"] and not s["rivers_to_cross"] for s in res)
    assert call(estimate_route, from_lat=h["lat"], from_lng=h["lng"], shelter_id="S0002", mode="walk")["rivers_to_cross"] == ["那珂川"]


def test_plan_rejected_when_crossing_river():
    h = homes()["H003"]
    CTX.update(household=h, alert={"hazard": "flood"})
    args = dict(decision="立ち退き避難", urgency="今すぐ", headline="t", risk_assessment="r", transport="徒歩",
                depart_by="今すぐ", route_notes="", member_assignments=[], checklist=[], timeline=[], family_message="m")
    bad = call(save_evacuation_plan, primary_shelter_id="S0002", backup_shelter_id="", **args)
    assert "error" in bad and "那珂川" in bad["error"]
    ok = call(save_evacuation_plan, primary_shelter_id="S1098", backup_shelter_id="", **args)  # 津田コミュニティセンター（同じ岸）
    assert ok.get("saved")
    CTX.update(household=None, alert=None, plan=None)


def test_route_and_bad_mode():
    h = homes()["H003"]
    r = call(estimate_route, from_lat=h["lat"], from_lng=h["lng"], shelter_id="S0002", mode="walk_slow")
    assert r["minutes"] > 0 and r["warnings"]
    assert "error" in call(estimate_route, from_lat=h["lat"], from_lng=h["lng"], shelter_id="S0002", mode="boat")


if __name__ == "__main__":
    test_shelter_data_loaded()
    test_home_risks_match_scenarios()
    test_nearest_designated_shelters_are_flooded_for_nguyen()
    test_joso_finds_shelters_across_city_border()
    test_landslide_home_and_shelters()
    test_flood_avoids_crossing_river()
    test_plan_rejected_when_crossing_river()
    test_route_and_bad_mode()
    print("tools OK")
