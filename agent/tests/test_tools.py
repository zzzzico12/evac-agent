"""ツール単体の動作確認（Bedrock・AWS は不要。国土地理院のタイル取得にインターネット接続が必要）。"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["ROUTES_ENABLED"] = "0"  # テストでは Amazon Location Service を呼ばない（直線の見積もりになる）

import geo  # noqa: E402
from tools import assess_home_risk, estimate_route, get_household_profile, save_evacuation_plan, search_shelters  # noqa: E402
from ui_events import ctx, start_run  # noqa: E402


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


PLAN_ARGS = dict(decision="立ち退き避難", urgency="今すぐ", headline="t", risk_assessment="r", transport="徒歩",
                 depart_by="今すぐ", route_notes="", member_assignments=[], checklist=[], timeline=[], family_message="m")


def save(**kw):
    return call(save_evacuation_plan, **{**PLAN_ARGS, "backup_shelter_id": "", **kw})


def test_plan_rejected_when_crossing_river():
    start_run(homes()["H003"], {"hazard": "flood"})
    bad = save(primary_shelter_id="S0002")  # 五軒小学校（那珂川の対岸）
    assert "error" in bad and "那珂川" in bad["error"]
    ok = save(primary_shelter_id="S1098")  # 津田コミュニティセンター（同じ岸）
    assert ok.get("saved")


def test_plan_checks():
    h = homes()["H003"]
    start_run(h, {"hazard": "flood"})
    assert "存在しません" in save(primary_shelter_id="S9999")["error"]
    yanagawa = next(s for s in geo.SHELTERS.values() if s["name"] == "柳河小学校")  # 浸水想定 5〜10m
    assert "浸水想定区域" in save(primary_shelter_id=yanagawa["id"])["error"]
    # 差し戻しが3回で中止し、それ以降は保存できない
    assert "中止" in save(primary_shelter_id="S9998")["error"]
    assert ctx()["abort"]
    assert "中止" in save(primary_shelter_id="S1098")["error"] and ctx()["plan"] is None

    start_run(h, {"hazard": "flood"})
    assert "decision" in save(decision="逃げる", primary_shelter_id="S1098")["error"]
    assert "primary_shelter_id" in save(primary_shelter_id="")["error"]
    assert "別の避難所" in save(primary_shelter_id="S1098", backup_shelter_id="S1098")["error"]
    river_bed = next(s for s in geo.SHELTERS.values() if "河川敷" in s["name"])
    start_run(h, {"hazard": "flood"})
    assert "河川敷" in save(primary_shelter_id=river_bed["id"])["error"]
    assert save(decision="在宅避難（自宅待機）", urgency="準備して待機", primary_shelter_id="").get("saved")
    assert "すでに保存" in save(decision="在宅避難（自宅待機）", urgency="準備して待機", primary_shelter_id="")["error"]


def test_outdoor_is_temporary_refuge():
    import tools

    # 洪水に安全な屋外の場所（公園など）のすぐそばを起点にする
    park = next(s for s in geo.SHELTERS.values() if tools.is_outdoor(s["name"]) and tools._is_safe(s, "flood")
                and s["designated_shelter"] != "対応無し")
    me = {"id": "SELF", "name": "いまいる場所の自分", "lat": park["lat"] + 0.001, "lng": park["lng"],
          "members": [{"relation": "本人", "age": 40}]}
    res = call(search_shelters, lat=me["lat"], lng=me["lng"], hazard="flood")["shelters"]
    outdoor = [s for s in res if s["outdoor"] and s["safe_for_flood"]]
    indoor = [s for s in res if not s["outdoor"] and s["safe_for_flood"]]
    assert outdoor and indoor, "安全な屋外の場所も、屋内と並んで候補に残る"
    river_bed = next(s for s in geo.SHELTERS.values() if tools.is_riverbed(s["name"]))
    assert not tools._is_safe(river_bed, "flood") and not tools._is_safe(river_bed, "landslide")

    # 屋外を第一避難先にすると、近くに屋内の安全な避難所があれば差し戻す。予備なら使える
    start_run(me, {"hazard": "flood"})
    assert "屋外の一時的な避難場所" in save(primary_shelter_id=outdoor[0]["id"])["error"]
    assert save(primary_shelter_id=indoor[0]["id"], backup_shelter_id=outdoor[0]["id"]).get("saved")

    # 屋内の安全な避難所が十分遠ければ（間に合わないとき）、屋外を第一避難先にしてよい
    original = tools.OUTDOOR_MARGIN_M
    try:
        tools.OUTDOOR_MARGIN_M = -10**9
        start_run(me, {"hazard": "flood"})
        assert save(primary_shelter_id=outdoor[0]["id"]).get("saved")
    finally:
        tools.OUTDOOR_MARGIN_M = original


def test_landslide_plan_checks():
    h = homes()["H007"]  # 大子町（土石流の警戒区域）
    start_run(h, {"hazard": "landslide"})
    in_zone = next(s for s in geo.SHELTERS.values() if s["city"] == "大子町" and s["landslide_rank"] > 0)
    assert "土砂災害" in save(primary_shelter_id=in_zone["id"])["error"]
    safe = next(s for s in call(search_shelters, lat=h["lat"], lng=h["lng"], hazard="landslide")["shelters"] if s["safe_for_landslide"])
    assert save(primary_shelter_id=safe["id"]).get("saved")


def test_input_validation():
    start_run({"id": "SELF", "name": "いまいる場所の自分", "members": [{"relation": "本人", "age": None}]}, None)
    assert "None" not in call(get_household_profile, household_id="SELF")["summary"]
    assert "error" in call(assess_home_risk, lat=36.39, lng=140.48, home_floors=0)
    assert "存在しません" in call(estimate_route, from_lat=36.39, from_lng=140.48, shelter_id="S9999", mode="walk")["error"]


def test_road_route_is_used_for_plan():
    import tools

    h = homes()["H003"]
    gokenn = geo.SHELTERS["S0002"]  # 五軒小学校（那珂川の対岸）
    # 道路に沿った経路の代わりに、川を渡る折れ線を返す
    path = [[h["lat"], h["lng"]], [h["lat"] - 0.008, h["lng"] - 0.004], [gokenn["lat"], gokenn["lng"]]]
    original = geo.road_route
    try:
        geo.road_route = lambda *a, **k: {"distance_m": 2300, "duration_s": 3300, "coords": path, "approximate": False}
        start_run(h, {"hazard": "landslide"})
        r = call(estimate_route, from_lat=h["lat"], from_lng=h["lng"], shelter_id="S0002", mode="walk_slow")
        assert r["road_route"] and r["minutes"] == 55 and r["distance_km"] == 2.3
        assert r["rivers_to_cross"] == ["那珂川"] and any("橋で渡る" in w for w in r["warnings"])
        saved = save(primary_shelter_id="S0002")
        assert saved.get("saved") and ctx()["plan"]["primary_shelter"]["path"] == path
    finally:
        geo.road_route = original


def test_plan_rejected_when_road_route_crosses_river():
    """直線では同じ岸でも、道路に沿った経路が橋で川を渡る避難先は差し戻す。候補を調べ尽くしたら通す。"""
    h = homes()["H003"]  # 青柳町（那珂川の北岸）
    tsuda = geo.SHELTERS["S1098"]  # 津田コミュニティセンター（同じ岸）
    gokenn = geo.SHELTERS["S0002"]  # 五軒小学校（対岸）
    # 対岸を回って戻ってくる道路の経路（那珂川を2回渡る）
    detour = [[h["lat"], h["lng"]], [gokenn["lat"], gokenn["lng"]], [tsuda["lat"], tsuda["lng"]]]
    straight = lambda a, b, c, d, mode: {"distance_m": 1000, "duration_s": 900, "coords": [[a, b], [c, d]], "approximate": False}
    original = geo.road_route
    try:
        geo.road_route = lambda a, b, c, d, mode: (
            {"distance_m": 5000, "duration_s": 4000, "coords": detour, "approximate": False}
            if (c, d) == (tsuda["lat"], tsuda["lng"]) else straight(a, b, c, d, mode))
        start_run(h, {"hazard": "flood"})
        bad = save(primary_shelter_id="S1098")
        assert "橋で渡ります" in bad["error"] and "那珂川" in bad["error"]
        # 同じ岸で、道路でも川を渡らない避難所なら保存できる
        other = next(o["id"] for o in call(search_shelters, lat=h["lat"], lng=h["lng"], hazard="flood")["shelters"]
                     if o["safe_for_flood"] and o["id"] != "S1098")
        assert save(primary_shelter_id=other).get("saved")

        # どの候補も道路では川を渡るとわかっているなら、通す（差し戻しが続いて中止にならない）
        geo.road_route = lambda a, b, c, d, mode: {"distance_m": 5000, "duration_s": 4000, "approximate": False,
                                                   "coords": [[a, b], [gokenn["lat"], gokenn["lng"]], [c, d]]}
        start_run(h, {"hazard": "flood"})
        import tools
        for o in geo.SHELTERS.values():
            if geo.distance_m(h["lat"], h["lng"], o["lat"], o["lng"]) <= tools.ALTERNATIVE_RADIUS_M:
                tools._route_for(o["id"])
        assert save(primary_shelter_id="S1098").get("saved")
    finally:
        geo.road_route = original


def _akatsuka():
    return next(u for u in geo.UNDERPASSES if u["name"] == "赤塚駅西線アンダーパス")


def test_underpass_data():
    assert len(geo.UNDERPASSES) > 500
    # 山を抜けるトンネルや共同溝は入れていない
    assert not any("トンネル" in u["name"] or "共同溝" in u["name"] for u in geo.UNDERPASSES)


def test_underpass_on_path():
    (lat1, lng1), (lat2, lng2) = _akatsuka()["coords"][0], _akatsuka()["coords"][-1]
    dlat, dlng = lat2 - lat1, lng2 - lng1
    # アンダーパスの中を通り抜ける経路
    through = [[lat1 - dlat, lng1 - dlng], [lat2 + dlat, lng2 + dlng]]
    assert _akatsuka()["id"] in [u["id"] for u in geo.underpasses_on_path(through)]
    # アンダーパスの上を横切るだけの経路（向きが直交する）
    mlat, mlng = (lat1 + lat2) / 2, (lng1 + lng2) / 2
    over = [[mlat - dlng, mlng - dlat * 2], [mlat + dlng, mlng + dlat * 2]]
    assert _akatsuka()["id"] not in [u["id"] for u in geo.underpasses_on_path(over)]


def test_road_route_avoids_underpass():
    """最短の経路がアンダーパスを通るときは避ける経路を使う。遠回りが大きすぎるときは最短のまま注意を出す。"""
    u = _akatsuka()
    (lat1, lng1), (lat2, lng2) = u["coords"][0], u["coords"][-1]
    dlat, dlng = lat2 - lat1, lng2 - lng1
    through = [[lat1 - dlat, lng1 - dlng], [lat2 + dlat, lng2 + dlng]]
    around = [through[0], [lat1, lng1 + 0.003], through[1]]
    calls = []

    def fake(a, b, c, d, mode, avoid=None):
        calls.append(avoid)
        if avoid:
            return {"distance_m": 1200, "duration_s": 900 + extra, "coords": around, "approximate": False}
        return {"distance_m": 900, "duration_s": 900, "coords": through, "approximate": False}

    original = geo._calculate, geo.ROUTES_ENABLED
    try:
        geo._calculate, geo.ROUTES_ENABLED = fake, True
        extra = 300
        geo.road_route.cache_clear()
        r = geo.road_route(*through[0], *through[1], "walk")
        assert r["coords"] == around and r["detour_s"] == 300 and not r["underpasses"]
        assert [x["id"] for x in r["avoided_underpasses"]] == [u["id"]] and calls[-1][0]["id"] == u["id"]
        extra = 3600
        geo.road_route.cache_clear()
        r = geo.road_route(*through[0], *through[1], "walk")
        assert r["coords"] == through and [x["id"] for x in r["underpasses"]] == [u["id"]] and not r["avoided_underpasses"]
    finally:
        geo._calculate, geo.ROUTES_ENABLED = original
        geo.road_route.cache_clear()


def test_underpass_warning_and_plan_marker():
    h = homes()["H003"]
    s = geo.SHELTERS["S1098"]
    info = {"id": 1, "name": "赤塚駅西線アンダーパス", "lat": 36.38, "lng": 140.4}
    original = geo.road_route
    try:
        geo.road_route = lambda a, b, c, d, mode: {
            "distance_m": 2000, "duration_s": 1800, "coords": [[a, b], [c, d]], "approximate": False,
            "underpasses": [info], "avoided_underpasses": [dict(info, id=2, name="")], "detour_s": 240}
        start_run(h, {"hazard": "flood"})
        r = call(estimate_route, from_lat=h["lat"], from_lng=h["lng"], shelter_id="S1098", mode="car")
        assert r["underpasses_on_route"] == 1 and r["underpasses_avoided"] == 1
        assert any("アンダーパス（赤塚駅西線アンダーパス）を通る" in w and "車は特に" in w for w in r["warnings"])
        assert any("1か所）を避けた経路" in w and "約4分長い" in w for w in r["warnings"])
        assert save(primary_shelter_id="S1098").get("saved")
        assert ctx()["plan"]["primary_shelter"]["underpasses"] == [{"name": info["name"], "lat": 36.38, "lng": 140.4}]
    finally:
        geo.road_route = original


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
    test_plan_checks()
    test_outdoor_is_temporary_refuge()
    test_landslide_plan_checks()
    test_input_validation()
    test_road_route_is_used_for_plan()
    test_plan_rejected_when_road_route_crosses_river()
    test_underpass_data()
    test_underpass_on_path()
    test_road_route_avoids_underpass()
    test_underpass_warning_and_plan_marker()
    test_route_and_bad_mode()
    print("tools OK")
