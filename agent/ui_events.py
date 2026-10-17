"""エージェントの進行状況（思考・ツール呼び出し・計画）を、呼び出し元へストリーミングで返す。

保存はしない。main.py が送り先（sink）を差し込み、AgentCore Runtime の SSE レスポンスとして流す。
"""

import itertools
import time

_seq = itertools.count()
_sink = print

# AgentCore Runtime はセッションごとに microVM が分かれるため、
# 1プロセス = 1世帯の実行として扱う。
CTX: dict = {"household": None, "alert": None, "plan": None}


def set_sink(fn) -> None:
    global _sink
    _sink = fn


def emit(kind: str, title: str, body=None) -> None:
    _sink({"seq": next(_seq), "ts": int(time.time() * 1000), "kind": kind, "title": title, "body": body})
