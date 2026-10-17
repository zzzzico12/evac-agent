"""避難計画エージェントが使うツール群。"""

import functools
import re

from strands import tool

import geo
from ui_events import ctx, emit

DECISIONS = ["立ち退き避難", "垂直避難", "屋内安全確保", "在宅避難（自宅待機）"]
URGENCIES = ["今すぐ", "1時間以内", "準備して待機"]
MAX_REJECTIONS = 3  # 計画の保存を差し戻す回数の上限。超えたら計画づくりを中止する
ALTERNATIVE_RADIUS_M = 5000  # 「ほかに安全な避難所があるか」を探す範囲（search_shelters の既定の半径と同じ）
# 屋外の場所を第一避難先にしてよいのは、屋内の安全な避難所がこれ以上遠いときだけ（間に合わないときは近くへ逃げる）
OUTDOOR_MARGIN_M = 1000

# 名前から屋外とわかる場所（公園・運動場など）。ただし体育館などの屋内施設は除く。
# 屋外でも災害に対して安全なら「一時的な避難場所」として候補に残し、屋内の避難所より優先度を下げる
_OUTDOOR = re.compile(r"河川敷|運動場|公園|グラウンド|グランド|広場|緑地|球場|競技場|駐車場|校庭")
_INDOOR = re.compile(r"体育館|館|センター|ハウス|ホール")


def is_outdoor(name: str) -> bool:
    return bool(_OUTDOOR.search(name)) and not _INDOOR.search(name)


def is_riverbed(name: str) -> bool:
    """河川敷は川の中なので、どの災害でも避難先にしない。"""
    return "河川敷" in name


def ui_tool(label: str):
    """ツールの呼び出しと結果の要約を画面に流すデコレーター。"""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            emit("tool_call", label, {"tool": fn.__name__, "input": kwargs})
            try:
                result = fn(*args, **kwargs)
            except Exception as e:  # エラーもモデルに返して自己修正させる
                emit("tool_error", label, {"error": str(e)})
                return {"error": str(e)}
            emit("tool_result", result.get("summary", label), {"tool": fn.__name__, "output": result})
            return result

        return wrapper

    return deco


@tool
@ui_tool("世帯情報を取得")
def get_household_profile(household_id: str) -> dict:
    """利用者が端末に登録した世帯情報（住所・座標・住宅・家族構成・要配慮事項・ペット・移動手段・連絡先）を取得する。

    Args:
        household_id: 世帯ID
    """
    h = ctx()["household"]
    if not h or h.get("id") != household_id:
        raise ValueError(f"世帯 {household_id} の情報は受け取っていません")
    members = "・".join(m["relation"] + (f"{m['age']}歳" if m.get("age") is not None else "") for m in h["members"])
    return {"summary": f"{h['name']}：{members}", "household": h}


HAZARD_LABEL = {"flood": "洪水", "landslide": "土砂災害"}


def _is_ok(s: dict, hazard: str, lat: float, lng: float) -> bool:
    """起点から見て、その災害の避難先として使えるか（安全・屋内・洪水なら川を渡らない）。"""
    if not _is_safe(s, hazard):
        return False
    return hazard != "flood" or not geo.rivers_crossed(lat, lng, s["lat"], s["lng"])


def _problem(s: dict, hazard: str, lat: float, lng: float) -> str:
    """避難先として使えない理由（なければ空文字）。"""
    if s["designated_shelter"] == "対応無し":
        return "指定避難所ではありません"
    if is_riverbed(s["name"]):
        return "河川敷（川の中）で、避難先には使えません"
    if hazard == "landslide":
        if s["landslide_rank"] > 0:
            return f"{s['landslide_zone']}の中にあります"
        if s["nearby_landslide_rank"] > 0:
            return "すぐそば（60m以内）に土砂災害警戒区域があります"
        return ""
    if s["flood_depth_rank"] > 0:
        return f"浸水想定区域（{s['flood_depth']}）の中にあります"
    if s["nearby_max_depth_rank"] >= 3:
        return "すぐそば（120m以内）に深い浸水想定区域があり、川や堤防の際にあります"
    rivers = geo.rivers_crossed(lat, lng, s["lat"], s["lng"])
    if rivers:
        return f"{'・'.join(rivers)}を渡る必要があり、洪水のときに増水した川を渡るのは危険です"
    return ""


def _check_plan(decision: str, urgency: str, primary_id: str, backup_id: str) -> None:
    """計画を保存する前に、コードで確かめる（モデルの判断ミスを差し戻す）。
    - 避難行動の種類・緊急度が決めた値か、立ち退き避難なら避難先があるか
    - 避難所 ID が実在するか
    - 避難先がその災害に対して使えるか（危険な区域の中・河川敷・洪水で川を渡る、を弾く）。
      ただし、起点の近くに使える避難所が1つもない場合は、ほかに選びようがないので通す
    - 屋外の場所を第一避難先にするのは、屋内の安全な避難所がそれより1km以上遠いときだけ
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision は {DECISIONS} のいずれかにしてください")
    if urgency not in URGENCIES:
        raise ValueError(f"urgency は {URGENCIES} のいずれかにしてください")
    if decision == "立ち退き避難" and not primary_id:
        raise ValueError("立ち退き避難のときは primary_shelter_id に避難先を指定してください")
    if primary_id and primary_id == backup_id:
        raise ValueError("予備の避難先には、第一避難先とは別の避難所を指定してください（なければ空文字）")

    state = ctx()
    hazard = (state["alert"] or {}).get("hazard", "flood")
    origin = state["household"] or {}
    for label, sid in (("第一避難先", primary_id), ("予備の避難先", backup_id)):
        if not sid:
            continue
        s = geo.SHELTERS.get(sid)
        if not s:
            raise ValueError(f"{label}の避難所ID {sid} は存在しません。search_shelters の結果にある id から選んでください")
        if "lat" not in origin:
            continue
        lat, lng = origin["lat"], origin["lng"]
        nearby = [
            (geo.distance_m(lat, lng, o["lat"], o["lng"]), o)
            for o in geo.SHELTERS.values()
            if geo.distance_m(lat, lng, o["lat"], o["lng"]) <= ALTERNATIVE_RADIUS_M
        ]
        reason = _problem(s, hazard, lat, lng)
        if reason:
            # 屋外も含めて、使える避難所がほかに1つでもあれば差し戻す（1つもなければ選びようがないので通す）
            if any(_is_ok(o, hazard, lat, lng) for _, o in nearby):
                raise ValueError(
                    f"{label}の{s['name']}は、{reason}。"
                    f"search_shelters の safe_for_{hazard} が true の避難所から選び直してください"
                )
            continue
        if label == "第一避難先" and is_outdoor(s["name"]):
            # 屋外は一時的な避難場所。近くに屋内の安全な避難所があれば、そちらを第一避難先にする
            d_out = geo.distance_m(lat, lng, s["lat"], s["lng"])
            indoor = [d for d, o in nearby if _is_ok(o, hazard, lat, lng) and not is_outdoor(o["name"])]
            if indoor and min(indoor) <= d_out + OUTDOOR_MARGIN_M:
                raise ValueError(
                    f"{s['name']}は屋外の一時的な避難場所です。近くに屋内の安全な避難所があるので、そちらを第一避難先にしてください"
                    "（屋外の場所は、予備か、屋内の避難所まで間に合わないときに使います）"
                )


def _check_hazard(hazard: str) -> None:
    if hazard not in HAZARD_LABEL:
        raise ValueError(f"hazard は {list(HAZARD_LABEL)} のいずれかを指定してください")


def _is_safe(s: dict, hazard: str) -> bool:
    """その災害に対して安全とみなす避難所か。
    14市町村は指定避難所かどうかがすべて「不明」なので、「対応無し」と明記されたものだけを外す。
    河川敷は外す。公園などの屋外の場所は、災害に対して安全なら残す（優先度は屋内より下げる）。"""
    if s["designated_shelter"] == "対応無し" or is_riverbed(s["name"]):
        return False
    if hazard == "landslide":
        return s["landslide_rank"] == 0 and s["nearby_landslide_rank"] == 0
    return s["flood_depth_rank"] == 0 and s["nearby_max_depth_rank"] < 3


@tool
@ui_tool("その場所の災害リスクを判定")
def assess_home_risk(lat: float, lng: float, home_floors: int) -> dict:
    """国土地理院のハザードマップで、指定地点（自宅、または外出先のいまいる場所）の洪水と土砂災害のリスクを調べる。
    洪水：想定浸水深（想定最大規模）・家屋倒壊等氾濫想定区域と、上の階への垂直避難が可能かの目安。
    土砂災害：土砂災害警戒区域／特別警戒区域に入っているかと、現象の種類（土石流・急傾斜地の崩壊・地すべり）。

    Args:
        lat: 自宅の緯度
        lng: 自宅の経度
        home_floors: 住める最上階の階数（平屋や1階住まいなら1）
    """
    if not 1 <= home_floors <= 60:
        raise ValueError("home_floors は1〜60で指定してください（階数がわからなければ1）")
    h = geo.flood_hazard(lat, lng)
    # n 階の床の高さを (n-1)×3m とみなし、想定最大の浸水深より高い階があれば垂直避難の候補とする
    top_floor_level_m = (home_floors - 1) * 3.0
    vertical_ok = h["depth_rank"] == 0 or (
        top_floor_level_m >= h["max_depth_m"] and not h["house_collapse_zone"]
    )
    h["vertical_evacuation_possible"] = vertical_ok
    h.update(geo.landslide_hazard(lat, lng))
    h["source"] = "国土地理院 重ねるハザードマップ（洪水浸水想定区域・想定最大規模／土砂災害警戒区域）"
    flood = f"浸水 {h['expected_depth']}" + ("（家屋倒壊等氾濫想定区域）" if h["house_collapse_zone"] else "")
    land = h["landslide_zone"] + (f"（{'・'.join(h['landslide_types'])}）" if h["landslide_types"] else "")
    h["summary"] = f"{flood}／土砂 {land}"
    return h


@tool
@ui_tool("避難所を検索")
def search_shelters(lat: float, lng: float, hazard: str = "flood", max_distance_km: float = 5.0) -> dict:
    """茨城県の避難施設オープンデータ（県内全域）から、指定地点の周辺の施設を返す。市町村の境界はまたいで検索する。
    近い施設10件に加え、指定した災害に対して安全な近い屋内の避難所10件と屋外の場所3件（指定避難所が「対応」または「不明」のもの）を必ず含める。
    outdoor が true の施設は公園・運動場などの屋外の一時的な避難場所。
    洪水のときは、起点から渡らなければならない川（rivers_to_cross）も付け、川を渡る避難所は安全とみなさない。
    各施設には、施設地点の想定浸水深と周辺120m以内の最大区分、土砂災害警戒区域かどうかと周辺60m以内の最も危険な区分を付けている。

    Args:
        lat: 起点の緯度
        lng: 起点の経度
        hazard: 対象の災害。flood（洪水）/ landslide（土砂災害）
        max_distance_km: 検索半径（km）
    """
    _check_hazard(hazard)
    within = []
    for s in geo.SHELTERS.values():
        d = geo.distance_m(lat, lng, s["lat"], s["lng"])
        if d <= max_distance_km * 1000:
            within.append((d, s))
    within.sort(key=lambda x: x[0])
    # 洪水のときは、増水した川を渡らずに行ける避難所だけを「安全」とする
    crossing = {s["id"]: geo.rivers_crossed(lat, lng, s["lat"], s["lng"]) for _, s in within} if hazard == "flood" else {}
    safe = [x for x in within if _is_safe(x[1], hazard) and not crossing.get(x[1]["id"])]
    safe_indoor = [x for x in safe if not is_outdoor(x[1]["name"])]
    safe_outdoor = [x for x in safe if is_outdoor(x[1]["name"])]
    picked = {s["id"]: (d, s) for d, s in within[:10] + safe_indoor[:10] + safe_outdoor[:3]}
    results = [
        {
            "id": s["id"],
            "city": s["city"],
            "name": s["name"],
            "address": s["address"],
            "distance_m": round(d),
            "designated_shelter": s["designated_shelter"],
            "emergency_place": s["emergency_place"],
            "welfare_shelter": s["welfare_shelter"],
            "flood_depth": s["flood_depth"],
            "house_collapse_zone": s["house_collapse_zone"],
            "nearby_max_depth_rank": s["nearby_max_depth_rank"],
            "landslide_zone": s["landslide_zone"],
            "nearby_landslide_rank": s["nearby_landslide_rank"],
            "outdoor": is_outdoor(s["name"]),
            "rivers_to_cross": crossing.get(s["id"], []),
            f"safe_for_{hazard}": _is_safe(s, hazard) and not crossing.get(s["id"]),
        }
        for d, s in sorted(picked.values(), key=lambda x: x[0])
    ]
    cities = sorted({r["city"] for r in results})
    return {
        "summary": f"{len(within)}件の中から{len(results)}件を候補に（{'・'.join(cities)}）。{HAZARD_LABEL[hazard]}に安全な避難所は屋内{len(safe_indoor)}件・屋外{len(safe_outdoor)}件",
        "depth_rank_legend": "0:なし 1:0.5m未満 2:0.5〜3m 3:3〜5m 4:5〜10m 5:10〜20m 6:20m以上",
        "landslide_rank_legend": "0:区域外 1:土砂災害警戒区域 2:土砂災害特別警戒区域",
        "data_source": "茨城県 避難施設情報（N2EM / G空間情報センター、2019年時点、県内全域）",
        "shelters": results,
    }


MODE_LABEL = {"walk": "徒歩", "walk_slow": "ゆっくり徒歩", "wheelchair": "車いす", "bicycle": "自転車", "car": "車"}


@tool
@ui_tool("避難経路を見積もり")
def estimate_route(from_lat: float, from_lng: float, shelter_id: str, mode: str) -> dict:
    """出発地から避難所までの、道路に沿った経路（Amazon Location Service）の距離・所要時間と、
    その経路が浸水想定区域・土砂災害警戒区域を通る割合、橋で渡る川を見積もる。
    災害時の通行止めや冠水は反映されない。

    Args:
        from_lat: 出発地の緯度
        from_lng: 出発地の経度
        shelter_id: 目的地の避難所ID
        mode: 移動手段。walk（徒歩）/ walk_slow（高齢者・乳幼児連れの徒歩）/ wheelchair（車いす＋介助）/ bicycle / car
    """
    if mode not in geo.SPEED_KMH:
        raise ValueError(f"mode は {list(geo.SPEED_KMH)} のいずれかを指定してください")
    s = geo.SHELTERS.get(shelter_id)
    if not s:
        raise ValueError(f"避難所ID {shelter_id} は存在しません。search_shelters の結果にある id を指定してください")
    route = geo.road_route(from_lat, from_lng, s["lat"], s["lng"], mode)
    exposure = geo.path_exposure(route["coords"])
    # 計画を保存するときに地図へ描けるよう、経路の線を覚えておく
    ctx().setdefault("routes", {})[shelter_id] = route
    km = route["distance_m"] / 1000
    minutes = round(route["duration_s"] / 60)
    rivers = exposure["rivers_on_path"]
    warnings = []
    if route["approximate"]:
        warnings.append("道路に沿った経路が取れなかったため、直線距離から見積もった目安")
    if rivers:
        warnings.append(f"この経路は{'・'.join(rivers)}を橋で渡る。洪水のときに増水した川や橋に近づくのは危険")
    if exposure["ratio_in_flood_zone"] > 0:
        warnings.append(
            f"経路の約{int(exposure['ratio_in_flood_zone'] * 100)}%が浸水想定区域"
            f"（最大 {exposure['max_depth']}）を通る。浸水が始まる前に抜けること"
        )
    if exposure["ratio_in_landslide_zone"] > 0:
        warnings.append(
            f"経路の約{int(exposure['ratio_in_landslide_zone'] * 100)}%が{exposure['max_landslide_zone']}を通る。"
            "崖や沢のそばの道はできるだけ避けること"
        )
    if mode == "car":
        warnings.append("道路が冠水（水深30cm以上）すると車は走行不能。早めの出発が前提")
    return {
        "summary": f"{s['name']}まで道なりに約{km:.1f}km・{MODE_LABEL[mode]}で約{minutes}分" + ("（要注意）" if warnings else ""),
        "shelter_id": shelter_id,
        "distance_km": round(km, 1),
        "minutes": minutes,
        "mode": mode,
        "road_route": not route["approximate"],
        "route_max_depth": exposure["max_depth"],
        "route_landslide_zone": exposure["max_landslide_zone"],
        "rivers_to_cross": rivers,
        "warnings": warnings,
    }


@tool
@ui_tool("避難計画を保存")
def save_evacuation_plan(
    decision: str,
    urgency: str,
    headline: str,
    risk_assessment: str,
    primary_shelter_id: str,
    backup_shelter_id: str,
    transport: str,
    depart_by: str,
    route_notes: str,
    member_assignments: list[dict],
    checklist: list[str],
    timeline: list[dict],
    family_message: str,
    translated_message: str = "",
) -> dict:
    """完成した避難計画を保存し、家族の画面に表示する。最後に必ず1回だけ呼ぶこと。

    Args:
        decision: 避難行動の種類。「立ち退き避難」「垂直避難」「屋内安全確保」「在宅避難（自宅待機）」のいずれか
        urgency: 緊急度。「今すぐ」「1時間以内」「準備して待機」のいずれか
        headline: 計画を一言で表す見出し（30字以内）
        risk_assessment: 自宅と経路のリスク評価（2〜3文）
        primary_shelter_id: 第一避難先の避難所ID。立ち退き避難しない場合は空文字
        backup_shelter_id: 第一避難先が使えないときの予備の避難所ID。なければ空文字
        transport: 移動手段と、その理由
        depart_by: 出発期限（例: 「警報から30分以内（14:30まで）」）
        route_notes: 経路上の注意点
        member_assignments: 家族ごとの役割。各要素は {"member": "父", "role": "祖母の車いすを介助し、車に乗せる"}
        checklist: この世帯に合わせた持ち出し品リスト（家族構成・持病・ペットを反映）
        timeline: 行動の時系列。各要素は {"time": "+0分", "action": "…"}
        family_message: 家族のLINEグループに送る想定の短いメッセージ（やさしい日本語、120字以内）
        translated_message: 世帯の言語が日本語以外のとき、family_message をその言語に訳したもの
    """

    state = ctx()
    if state["abort"]:
        raise ValueError(state["abort"])
    if state["plan"] is not None:
        raise ValueError("計画はすでに保存されています。これ以上ツールを呼ばずに終了してください")
    try:
        _check_plan(decision, urgency, primary_shelter_id, backup_shelter_id)
    except ValueError as e:
        state["rejections"] += 1
        if state["rejections"] >= MAX_REJECTIONS:
            state["abort"] = f"計画の保存が{MAX_REJECTIONS}回差し戻されたため、計画づくりを中止しました（最後の理由：{e}）"
            raise ValueError(state["abort"]) from e
        raise

    def shelter(sid: str):
        s = geo.SHELTERS.get(sid)
        if not s:
            return None
        # 地図に描く経路の線。estimate_route で見積もった経路がなければ、ここで徒歩の経路を取る
        route = state.get("routes", {}).get(sid)
        origin = state["household"] or {}
        if route is None and "lat" in origin:
            route = geo.road_route(origin["lat"], origin["lng"], s["lat"], s["lng"], "walk")
        path = geo.simplify_path(route["coords"]) if route and not route["approximate"] else None
        return {"id": sid, "city": s["city"], "name": s["name"], "lat": s["lat"], "lng": s["lng"], "path": path}

    plan = {
        "decision": decision,
        "urgency": urgency,
        "headline": headline,
        "risk_assessment": risk_assessment,
        "primary_shelter": shelter(primary_shelter_id),
        "backup_shelter": shelter(backup_shelter_id),
        "transport": transport,
        "depart_by": depart_by,
        "route_notes": route_notes,
        "member_assignments": member_assignments,
        "checklist": checklist,
        "timeline": timeline,
        "family_message": family_message,
        "translated_message": translated_message,
    }
    state["plan"] = plan
    emit("plan", headline, plan)
    return {"summary": f"計画を保存：{decision}／{urgency}", "saved": True}


ALL_TOOLS = [
    get_household_profile,
    assess_home_risk,
    search_shelters,
    estimate_route,
    save_evacuation_plan,
]
