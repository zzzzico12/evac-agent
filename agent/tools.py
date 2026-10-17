"""避難計画エージェントが使うツール群。"""

import functools

from strands import tool

import geo
from ui_events import CTX, emit


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
    h = CTX["household"]
    if not h or h.get("id") != household_id:
        raise ValueError(f"世帯 {household_id} の情報は受け取っていません")
    members = "・".join(f"{m['relation']}{m['age']}歳" for m in h["members"])
    return {"summary": f"{h['name']}：{members}", "household": h}


HAZARD_LABEL = {"flood": "洪水", "landslide": "土砂災害"}


def _check_plan(primary_id: str, backup_id: str) -> None:
    """計画を保存する前に、避難先をコードで確かめる（モデルの判断ミスを差し戻す）。
    洪水のとき、川を渡らずに行ける安全な避難所があるのに、川を渡る避難先を選んでいたら保存しない。"""
    alert = CTX.get("alert") or {}
    origin = CTX.get("household") or {}
    if alert.get("hazard") != "flood" or "lat" not in origin:
        return
    lat, lng = origin["lat"], origin["lng"]
    for label, sid in (("第一避難先", primary_id), ("予備の避難先", backup_id)):
        s = geo.SHELTERS.get(sid)
        if not s:
            continue
        rivers = geo.rivers_crossed(lat, lng, s["lat"], s["lng"])
        if not rivers:
            continue
        has_alternative = any(
            _is_safe(o, "flood") and not geo.rivers_crossed(lat, lng, o["lat"], o["lng"])
            for o in geo.SHELTERS.values()
            if geo.distance_m(lat, lng, o["lat"], o["lng"]) <= 5000
        )
        if has_alternative:
            raise ValueError(
                f"{label}の{s['name']}へは{'・'.join(rivers)}を渡る必要があります。洪水のときに増水した川を渡るのは危険です。"
                "search_shelters の rivers_to_cross が空の避難所から選び直してください。"
            )


def _check_hazard(hazard: str) -> None:
    if hazard not in HAZARD_LABEL:
        raise ValueError(f"hazard は {list(HAZARD_LABEL)} のいずれかを指定してください")


def _is_safe(s: dict, hazard: str) -> bool:
    """その災害に対して安全とみなす避難所か。
    14市町村は指定避難所かどうかがすべて「不明」なので、「対応無し」と明記されたものだけを外す。"""
    if s["designated_shelter"] == "対応無し":
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
    近い施設10件に加え、指定した災害に対して安全な近い避難所10件（指定避難所が「対応」または「不明」のもの）を必ず含める。
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
    picked = {s["id"]: (d, s) for d, s in within[:10] + safe[:10]}
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
            "rivers_to_cross": crossing.get(s["id"], []),
            f"safe_for_{hazard}": _is_safe(s, hazard) and not crossing.get(s["id"]),
        }
        for d, s in sorted(picked.values(), key=lambda x: x[0])
    ]
    cities = sorted({r["city"] for r in results})
    return {
        "summary": f"{len(within)}件の中から{len(results)}件を候補に（{'・'.join(cities)}）。{HAZARD_LABEL[hazard]}に安全な避難所は{len(safe)}件",
        "depth_rank_legend": "0:なし 1:0.5m未満 2:0.5〜3m 3:3〜5m 4:5〜10m 5:10〜20m 6:20m以上",
        "landslide_rank_legend": "0:区域外 1:土砂災害警戒区域 2:土砂災害特別警戒区域",
        "data_source": "茨城県 避難施設情報（N2EM / G空間情報センター、2019年時点、県内全域）",
        "shelters": results,
    }


MODE_LABEL = {"walk": "徒歩", "walk_slow": "ゆっくり徒歩", "wheelchair": "車いす", "bicycle": "自転車", "car": "車"}


@tool
@ui_tool("避難経路を見積もり")
def estimate_route(from_lat: float, from_lng: float, shelter_id: str, mode: str) -> dict:
    """自宅から避難所までの距離・所要時間と、直線経路上の浸水想定区域・土砂災害警戒区域の通過状況、渡らなければならない川を見積もる。

    Args:
        from_lat: 出発地の緯度
        from_lng: 出発地の経度
        shelter_id: 目的地の避難所ID
        mode: 移動手段。walk（徒歩）/ walk_slow（高齢者・乳幼児連れの徒歩）/ wheelchair（車いす＋介助）/ bicycle / car
    """
    if mode not in geo.SPEED_KMH:
        raise ValueError(f"mode は {list(geo.SPEED_KMH)} のいずれかを指定してください")
    s = geo.SHELTERS[shelter_id]
    km = geo.distance_m(from_lat, from_lng, s["lat"], s["lng"]) * geo.ROAD_FACTOR / 1000
    minutes = round(km / geo.SPEED_KMH[mode] * 60)
    exposure = geo.route_flood_exposure(from_lat, from_lng, s["lat"], s["lng"])
    landslide = geo.route_landslide_exposure(from_lat, from_lng, s["lat"], s["lng"])
    rivers = geo.rivers_crossed(from_lat, from_lng, s["lat"], s["lng"])
    warnings = []
    if rivers:
        warnings.append(f"{'・'.join(rivers)}を渡る必要がある。洪水のときに増水した川や橋に近づくのは危険")
    if exposure["ratio_in_flood_zone"] > 0:
        warnings.append(
            f"経路の約{int(exposure['ratio_in_flood_zone'] * 100)}%が浸水想定区域"
            f"（最大 {exposure['max_depth']}）を通過。浸水が始まる前に抜けること"
        )
    if landslide["ratio_in_landslide_zone"] > 0:
        warnings.append(
            f"経路の約{int(landslide['ratio_in_landslide_zone'] * 100)}%が{landslide['max_landslide_zone']}を通過。"
            "崖や沢のそばの道はできるだけ避けること"
        )
    if mode == "car":
        warnings.append("道路が冠水（水深30cm以上）すると車は走行不能。早めの出発が前提")
    return {
        "summary": f"{s['name']}まで約{km:.1f}km・{MODE_LABEL[mode]}で約{minutes}分" + ("（要注意）" if warnings else ""),
        "shelter_id": shelter_id,
        "distance_km": round(km, 1),
        "minutes": minutes,
        "mode": mode,
        "route_max_depth": exposure["max_depth"],
        "route_landslide_zone": landslide["max_landslide_zone"],
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

    _check_plan(primary_shelter_id, backup_shelter_id)

    def shelter(sid: str):
        s = geo.SHELTERS.get(sid)
        return {"id": sid, "city": s["city"], "name": s["name"], "lat": s["lat"], "lng": s["lng"]} if s else None

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
    CTX["plan"] = plan
    emit("plan", headline, plan)
    return {"summary": f"計画を保存：{decision}／{urgency}", "saved": True}


ALL_TOOLS = [
    get_household_profile,
    assess_home_risk,
    search_shelters,
    estimate_route,
    save_evacuation_plan,
]
