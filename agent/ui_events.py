"""1回の依頼（計画の作成）ごとの実行状態と、進行状況（思考・ツール呼び出し・計画）の送り出し。

状態は contextvars で依頼ごとに分ける。Strands は同期のツールを asyncio.to_thread で動かすので、
ツールのスレッドにも同じ状態が引き継がれる。保存はしない（SSE で呼び出し元へ流すだけ）。
"""

import itertools
import time
from contextvars import ContextVar

_run: ContextVar[dict] = ContextVar("evac_run")


def start_run(household: dict | None, alert: dict | None, sink=print) -> dict:
    """依頼の開始時に呼ぶ。以降、同じコンテキスト（とそこから作ったタスク・スレッド）で ctx() が使える。"""
    state = {
        "household": household,
        "alert": alert,
        "plan": None,
        "sink": sink,
        "seq": itertools.count(),
        "tool_calls": 0,  # ツール呼び出しの回数
        "rejections": 0,  # 計画の保存を差し戻した回数
        "abort": None,  # 中止する理由（入っていたら次のモデル呼び出しで止める）
        "routes": {},  # estimate_route で見積もった経路（避難所ID → 経路）。計画の地図に使う
    }
    _run.set(state)
    return state


def ctx() -> dict:
    state = _run.get(None)
    if state is None:  # テストやローカルでの直接呼び出し用
        state = start_run(None, None)
    return state


def emit(kind: str, title: str, body=None) -> None:
    state = ctx()
    state["sink"]({"seq": next(state["seq"]), "ts": int(time.time() * 1000), "kind": kind, "title": title, "body": body})
