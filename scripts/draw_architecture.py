"""構成図（docs/architecture.svg）を作る。AWS のアイコンは AWS Architecture Icons のパッケージから埋め込む。

使い方:
  python3 scripts/draw_architecture.py <AWS Architecture Icons のパッケージのフォルダ>
  （アイコンは https://aws.amazon.com/architecture/icons/ から入手できる）

PNG（docs/architecture.png）は、Chrome でSVGを2倍の解像度で書き出して作る:
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --window-size=1700,960 \
    --force-device-scale-factor=2 --screenshot=docs/architecture.png docs/architecture.svg
"""

import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "architecture.svg"

ICONS = {
    "cloudfront": "Architecture-Service-Icons_01302026/Arch_Networking-Content-Delivery/64/Arch_Amazon-CloudFront_64.svg",
    "s3": "Architecture-Service-Icons_01302026/Arch_Storage/64/Arch_Amazon-Simple-Storage-Service_64.svg",
    "lambda": "Architecture-Service-Icons_01302026/Arch_Compute/64/Arch_AWS-Lambda_64.svg",
    "agentcore": "Architecture-Service-Icons_01302026/Arch_Artificial-Intelligence/64/Arch_Amazon-Bedrock-AgentCore_64.svg",
    "bedrock": "Architecture-Service-Icons_01302026/Arch_Artificial-Intelligence/64/Arch_Amazon-Bedrock_64.svg",
    "location": "Architecture-Service-Icons_01302026/Arch_Front-End-Web-Mobile/64/Arch_Amazon-Location-Service_64.svg",
    "iam": "Architecture-Service-Icons_01302026/Arch_Security-Identity/64/Arch_AWS-Identity-and-Access-Management_64.svg",
    "cloudwatch": "Architecture-Service-Icons_01302026/Arch_Management-Tools/64/Arch_Amazon-CloudWatch_64.svg",
    "mobile": "Resource-Icons_01302026/Res_General-Icons/Res_48_Light/Res_Mobile-client_48_Light.svg",
    "cloud": "Architecture-Group-Icons_01302026/AWS-Cloud-logo_32.svg",
    "region": "Architecture-Group-Icons_01302026/Region_32.svg",
}

INK = "#1f2933"
SUB = "#52606d"
LINE = "#5a6b7b"
ACCENT = "#0f766e"
FONT = "'Noto Sans JP','Hiragino Sans','Hiragino Kaku Gothic ProN','Yu Gothic',sans-serif"
ICON = 104  # サービスのアイコンの大きさ（px）
R = ICON / 2 + 6  # アイコンの中心から矢印の端までの距離
HALO = 'stroke="#ffffff" stroke-width="5" stroke-linejoin="round" paint-order="stroke"'  # 文字の白いふち


def text_width(t: str, size: float) -> float:
    """おおよその文字列の幅（全角は1文字分、半角は0.6文字分）。"""
    return sum(size if ord(ch) > 0x2000 else size * 0.6 for ch in t)


def plate(x: float, y: float, t: str, size: float, anchor="start") -> str:
    """文字の後ろに敷く白い板（線や枠が文字の間を通っても読めるように）。"""
    w = text_width(t, size) + 8
    left = {"start": x - 4, "middle": x - w / 2, "end": x - w + 4}[anchor]
    return f'<rect x="{left}" y="{y - size}" width="{w}" height="{size + 6}" fill="#ffffff"/>'


def icon(pkg: Path, key: str, x: float, y: float, size: float) -> str:
    data = base64.b64encode((pkg / ICONS[key]).read_bytes()).decode()
    return f'<image href="data:image/svg+xml;base64,{data}" x="{x}" y="{y}" width="{size}" height="{size}"/>'


def text(x: float, y: float, lines: list[str], size=15, weight=400, color=INK, anchor="middle", gap=1.45, bg=True) -> str:
    out = []
    for i, line in enumerate(lines):
        w = weight if i == 0 else 400
        c = color if i == 0 else SUB
        s = size if i == 0 else size - 2
        if bg:
            out.append(plate(x, y + i * size * gap, line, s, anchor))
        out.append(
            f'<text x="{x}" y="{y + i * size * gap}" font-size="{s}" font-weight="{w}" fill="{c}" text-anchor="{anchor}" {HALO}>{line}</text>'
        )
    return "".join(out)


def service(pkg, key, cx, cy, lines, size=ICON, side="below"):
    """アイコンと名前。名前は下（below）・右（right）・左（left）に置ける。"""
    img = icon(pkg, key, cx - size / 2, cy - size / 2, size)
    if side == "right":
        return img + text(cx + size / 2 + 14, cy - 6, lines, weight=700, anchor="start")
    if side == "left":
        return img + text(cx - size / 2 - 14, cy - 6, lines, weight=700, anchor="end")
    if side == "above":
        return img + text(cx, cy - size / 2 - 14 - (len(lines) - 1) * 13 * 1.45 - 4, lines, weight=700)
    return img + text(cx, cy + size / 2 + 22, lines, weight=700)


def badge(x, y, n, label="", color=None, anchor="start"):
    """番号の丸と説明。"""
    c = color or SUB
    out = ""
    if label:
        out += plate(x - 12, y, "　" + label, 13, anchor)
        out += f'<text x="{x + 17}" y="{y}" font-size="13" fill="{c}" text-anchor="{anchor}" {HALO}>{label}</text>'
    out += (f'<circle cx="{x}" cy="{y - 5}" r="11" fill="{ACCENT}"/>'
            f'<text x="{x}" y="{y}" font-size="13" font-weight="700" fill="#fff" text-anchor="middle">{n}</text>')
    return out


def arrow(points: list[tuple[float, float]], label="", lx=None, ly=None, dashed=False, color=LINE, num=None) -> str:
    d = "M" + " L".join(f"{x},{y}" for x, y in points)
    dash = ' stroke-dasharray="6 5"' if dashed else ""
    out = f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"{dash} marker-end="url(#arrow)"/>'
    if label:
        if lx is None:
            (x1, y1), (x2, y2) = points[0], points[1]
            lx, ly = (x1 + x2) / 2, (y1 + y2) / 2 - 10
        prefix = ""
        if num is not None:
            prefix = (f'<circle cx="{lx - 2}" cy="{ly - 5}" r="11" fill="{ACCENT}"/>'
                      f'<text x="{lx - 2}" y="{ly}" font-size="13" font-weight="700" fill="#fff" text-anchor="middle">{num}</text>')
            out += prefix + f'<text x="{lx + 15}" y="{ly}" font-size="13" fill="{SUB}" text-anchor="start">{label}</text>'
        else:
            out += f'<text x="{lx}" y="{ly}" font-size="13" fill="{SUB}" text-anchor="middle">{label}</text>'
    return out


def box(x, y, w, h, stroke, dash=False, fill="none", r=0):
    d = ' stroke-dasharray="7 5"' if dash else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="1.6"{d}/>'


def external(x, y, w, title, lines):
    h = 26 + 20 * len(lines) + 14
    out = box(x, y, w, h, "#9aa5b1", fill="#ffffff", r=10)
    out += f'<text x="{x + 14}" y="{y + 26}" font-size="15" font-weight="700" fill="{INK}">{title}</text>'
    for i, line in enumerate(lines):
        out += f'<text x="{x + 14}" y="{y + 50 + i * 20}" font-size="13" fill="{SUB}">{line}</text>'
    return out, h


def main(pkg: Path) -> None:
    W, H = 1700, 960
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{LINE}"/></marker>'
        '<marker id="arrowg" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{ACCENT}"/></marker></defs>',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
        text(40, 50, ["避難計画エージェント 構成図"], size=24, weight=700, anchor="start"),
        text(40, 80, ["警報をもとに、AI エージェントが避難所データと利用者の状況から避難計画を組み立てる（茨城県・洪水／土砂災害）"],
             size=14, color=SUB, anchor="start"),
    ]

    # ---- 利用者 ----
    parts.append(box(40, 330, 220, 300, "#c3cbd4", fill="#f7f9fb", r=12))
    parts.append(icon(pkg, "mobile", 102, 338, 96))
    parts.append(text(150, 454, ["利用者のスマホ・PC", "PWA"], weight=700, bg=False))
    parts.append(text(150, 516, ["端末の localStorage に保存", "・登録した場所と家族", "・作った避難計画", "（サーバ側に DB はない）"],
                      size=13, color=SUB, bg=False))

    # ---- AWS Cloud / Region ----
    parts.append(box(320, 140, 990, 740, "#232f3e"))
    parts.append(icon(pkg, "cloud", 320, 140, 32))
    parts.append(f'<text x="360" y="162" font-size="14" font-weight="700" fill="{INK}">AWS Cloud</text>')
    parts.append(box(340, 185, 950, 675, "#147eba", dash=True))
    parts.append(icon(pkg, "region", 340, 185, 32))
    parts.append(f'<text x="380" y="207" font-size="14" font-weight="700" fill="#147eba">東京リージョン（ap-northeast-1）</text>')

    cf, s3, lam, ac, br, loc = (500, 400), (790, 400), (790, 610), (1070, 610), (1070, 400), (1170, 795)
    parts.append(service(pkg, "cloudfront", *cf, ["Amazon CloudFront", "HTTPS・セキュリティヘッダー", "OAC で S3・Lambda へ"], side="above"))
    parts.append(service(pkg, "s3", *s3, ["Amazon S3", "画面・疑似警報", "避難所・河川データ"]))
    parts.append(service(pkg, "lambda", *lam, ["AWS Lambda（agent-proxy）", "Function URL（IAM 認証）", "入力の検証・SSE の中継"]))
    parts.append(service(pkg, "agentcore", *ac, ["Bedrock AgentCore Runtime", "Strands Agents（Python）", "ツール・計画の確認"]))
    parts.append(service(pkg, "bedrock", *br, ["Amazon Bedrock", "Claude"], side="right"))
    parts.append(service(pkg, "location", *loc, ["Amazon Location Service", "Routes API（道路の経路）"], side="left"))

    parts.append(icon(pkg, "iam", 372, 778, 64))
    parts.append(text(448, 806, ["IAM", "最小権限の実行ロール"], size=13, anchor="start"))
    parts.append(icon(pkg, "cloudwatch", 605, 778, 64))
    parts.append(text(681, 806, ["CloudWatch Logs", "Lambda・AgentCore のログ"], size=13, anchor="start"))

    # ---- AWS の外のデータ ----
    ex, ew = 1350, 320
    parts.append(f'<text x="{ex}" y="196" font-size="14" font-weight="700" fill="{INK}">AWS の外のデータ</text>')
    osm, h1 = external(ex, 210, ew, "OpenStreetMap", ["主な19河川の流路（ODbL）", "→ data/rivers.json に加工"])
    ibk, h2 = external(ex, 210 + h1 + 16, ew, "茨城県 避難施設情報", ["2,378件（N2EM / G空間情報センター）", "→ ハザードを付けて data/shelters.json"])
    gy = 525
    gsi, h3 = external(ex, gy, ew, "国土地理院", ["重ねるハザードマップ（浸水想定・", "土砂災害警戒区域）のタイル", "地図・住所検索・逆ジオコーダー"])
    parts += [osm, ibk, gsi]
    osm_y, ibk_y = 210 + h1 / 2, 210 + h1 + 16 + h2 / 2

    # ---- 計画を作るときの流れ ----
    parts.append(arrow([(260, cf[1] - 15), (cf[0] - R, cf[1] - 15)]))
    parts.append(badge(282, cf[1] - 25, 1, "画面とデータを取得"))
    parts.append(arrow([(260, cf[1] + 15), (cf[0] - R, cf[1] + 15)]))
    parts.append(badge(282, cf[1] + 42, 2, "計画の依頼"))
    parts.append(text(299, cf[1] + 60, ["POST /api/agent"], size=12, color=SUB, anchor="start"))
    parts.append(text(299, cf[1] + 77, ["（場所・家族・警報）"], size=12, color=SUB, anchor="start"))
    parts.append(arrow([(cf[0] + R, cf[1]), (s3[0] - R, s3[1])]))
    parts.append(text((cf[0] + s3[0]) / 2, cf[1] - 10, ["OAC"], size=13, color=SUB))
    parts.append(arrow([(cf[0], cf[1] + R), (cf[0], lam[1]), (lam[0] - R, lam[1])]))
    parts.append(text(cf[0] + 12, cf[1] + 110, ["/api/agent（OAC）"], size=13, color=SUB, anchor="start"))
    parts.append(arrow([(lam[0] + R, lam[1]), (ac[0] - R, ac[1])]))
    parts.append(badge(lam[0] + R + 22, lam[1] - 15, 3, "呼び出す"))
    parts.append(arrow([(ac[0], ac[1] - R), (br[0], br[1] + R)]))
    parts.append(badge(ac[0] + 18, (ac[1] + br[1]) / 2 + 5, 4, "判断・計画の作成"))
    # AgentCore の名前を避けて、右側を回って Location Service の横から入る
    parts.append(arrow([(ac[0] + R, ac[1] + 15), (1245, ac[1] + 15), (1245, loc[1]), (loc[0] + R, loc[1])]))
    parts.append(badge(ac[0] + R + 22, ac[1] + 42, 5, "道路の経路"))
    parts.append(arrow([(ac[0] + R, ac[1] - 20), (ex, ac[1] - 20)]))
    parts.append(badge(1145, ac[1] - 32, 6, "ハザードのタイル"))
    # 結果を SSE で画面へ
    parts.append(arrow([(lam[0] - 20, 732), (lam[0] - 20, 756), (210, 752), (210, 636)], color=ACCENT, dashed=True).replace('url(#arrow)', 'url(#arrowg)'))
    parts.append(badge(400, 748, 7, "進み具合と計画をストリーミング（SSE）→ 端末に保存", color=ACCENT))

    # ---- 画面から直接使う外部サービス ----
    parts.append(arrow([(90, 630), (90, 912), (ex + ew / 2, 912), (ex + ew / 2, gy + h3 + 4)]))
    parts.append(text(830, 904, ["地図の表示・住所検索（画面から直接）"], size=13, color=SUB))

    # ---- 事前のデータ加工 ----
    parts.append(arrow([(ex, osm_y), (1325, osm_y)], dashed=True).replace(' marker-end="url(#arrow)"', ""))
    parts.append(arrow([(ex, ibk_y), (1325, ibk_y), (1325, 238), (s3[0], 238), (s3[0], s3[1] - R)], dashed=True))
    parts.append(text(1060, 230, ["事前に加工して配置（scripts/）"], size=13, color=SUB))

    parts.append(f'<text x="40" y="945" font-size="12" fill="{SUB}">番号は計画を作るときの流れ。点線は事前のデータ加工と、計画の結果の流れ。'
                 'AWS のアイコンは AWS Architecture Icons。</text>')
    parts.append("</svg>")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
