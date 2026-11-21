# -*- coding: utf-8 -*-
"""
장전 브리핑 (news_watch.py) — 수출 발표 + DART 주요 공시 → 텔레그램
──────────────────────────────────────────────────────────
① 수출 발표: 매월 1일(산업부 월간), 11일(관세청 1~10일), 21일(관세청 1~20일)
   뉴스 제목에서 수출액·증가율·반도체 증가율을 뽑아 기록(news/exports.csv)하고,
   기사에 나온 업종의 대표 종목을 함께 보낸다.
   ※ 백테스트(2021.12~2026.11): 발표만으로 업종 방향이 정해지진 않음(전 업종 t<2).
     다만 1일 발표일은 업종 변동폭이 평소의 1.1~1.4배 → 장중 매매 대상 고르기에 활용.
     기록이 쌓이면 '예상 대비 서프라이즈'로 다시 검증한다.
② DART 공시: 전날 장 마감 이후 ~ 지금까지 상장사 공시 중 주가에 영향이 큰 종류만
   (공급계약·자사주·무상증자·실적 / 유상증자·CB·감자·거래정지 등)
③ 오늘의 거래량 급증 후보(surge/)와 겹치는 종목은 ★ 표시

필요: 시크릿 DART_API_KEY (quant_daily 와 같은 키), 텔레그램 설정
실행: python news_watch.py [--no-telegram] [--force-exports]
"""
import argparse
import csv
import glob
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
NEWS_DIR = os.path.join(BASE, "news")
KST = ZoneInfo("Asia/Seoul")
UA = {"User-Agent": "Mozilla/5.0 (quant-briefing)"}

# 업종 키워드 → 대표 종목 (예시, 추천 아님)
SECTOR_MAP = {
    "반도체": (["반도체", "D램", "디램", "낸드", "HBM", "메모리"], "삼성전자·SK하이닉스·한미반도체·DB하이텍"),
    "전력기기·전선": (["변압기", "전력기기", "전선", "송배전", "데이터센터"], "HD현대일렉트릭·효성중공업·LS ELECTRIC·대한전선·LS"),
    "비철금속": (["구리", "알루미늄", "아연", "비철"], "고려아연·풍산"),
    "컴퓨터·저장장치": (["컴퓨터", "SSD", "저장장치", "서버"], "삼성전자·SK하이닉스(SSD)"),
    "정유": (["석유제품", "정유"], "S-Oil·SK이노베이션·GS"),
    "자동차": (["자동차", "승용차", "車"], "현대차·기아·현대모비스"),
    "철강": (["철강"], "POSCO홀딩스·현대제철"),
    "화장품": (["화장품", "K뷰티", "뷰티"], "아모레퍼시픽·코스맥스·한국콜마·에이피알"),
    "음식료": (["농수산", "라면", "식품", "K푸드"], "삼양식품·CJ제일제당·오리온·농심"),
    "바이오": (["바이오", "의약품", "헬스"], "삼성바이오로직스·셀트리온"),
    "조선": (["선박", "조선"], "HD한국조선해양·삼성중공업·한화오션"),
    "2차전지": (["이차전지", "2차전지", "배터리"], "LG에너지솔루션·삼성SDI"),
}

DART_GOOD = ["단일판매", "공급계약", "자기주식취득", "자기주식 취득", "무상증자", "영업(잠정)실적", "잠정실적",
             "신규시설투자", "타법인주식및출자증권취득", "현금ㆍ현물배당", "주식소각", "특허"]
DART_BAD = ["유상증자", "전환사채", "신주인수권부사채", "교환사채", "감자", "거래정지", "매매거래정지",
            "불성실공시", "관리종목", "상장폐지", "소송", "횡령", "배임", "회생"]


def now():
    return datetime.now(KST)


def load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ───────────────────────────── ① 수출 발표
def google_news(query, hours=36):
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    items = []
    cutoff = now() - timedelta(hours=hours)
    for it in ET.fromstring(r.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        link = it.findtext("link") or ""
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST)
        except Exception:  # noqa
            continue
        if pub >= cutoff:
            items.append({"title": title, "link": link, "pub": pub})
    return items


NUM = r"(\d[\d,]*(?:\.\d+)?)"


def parse_export_title(t):
    """제목에서 수치 추출: 수출액(억달러), 증감률(%), 반도체 증감률(%)"""
    out = {}
    m = re.search(r"수출(?:액|은|이)?\s*" + NUM + r"\s*억\s*(?:달러|弗|\$)", t)
    if m:
        out["export_100m_usd"] = float(m.group(1).replace(",", ""))
    head = t.split("반도체")[0]                      # 반도체 수치와 섞이지 않게 앞부분에서만
    m = re.search(r"수출.{0,30}?" + NUM + r"\s*%\s*(증가|감소|↑|↓|늘|줄)", head)
    if m:
        v = float(m.group(1).replace(",", ""))
        out["export_yoy_pct"] = -v if m.group(2) in ("감소", "↓", "줄") else v
    m = re.search(r"반도체.{0,15}?" + NUM + r"\s*%\s*(증가|감소|↑|↓|늘|줄|급증)?", t)
    if m and "비중" not in m.group(0):              # '반도체 비중 42.7%' 는 증가율이 아님
        v = float(m.group(1).replace(",", ""))
        out["semi_yoy_pct"] = -v if (m.group(2) or "") in ("감소", "↓", "줄") else v
    return out


def sectors_in(text):
    return [s for s, (kws, _) in SECTOR_MAP.items() if any(k in text for k in kws)]


def export_section(force=False):
    d = now().day
    if not force and d not in (1, 2, 3, 11, 12, 13, 21, 22, 23):
        return []
    seen = load_json(os.path.join(NEWS_DIR, "seen.json"), {"links": []})
    queries = ["관세청 수출 1~20일", "관세청 수출 1~10일", "산업통상부 수출입동향 수출", "수출 반도체 증가 관세청"]
    items, links = [], set(seen["links"])
    for q in queries:
        try:
            for it in google_news(q):
                if "수출" in it["title"] and it["link"] not in links:
                    items.append(it)
                    links.add(it["link"])
        except Exception as e:  # noqa
            print(f"  ⚠ 뉴스 조회 실패({q}): {e}")
    if not items:
        return []
    # 수치가 많이 잡히는 제목 우선
    items.sort(key=lambda x: (-len(parse_export_title(x["title"])), -x["pub"].timestamp()))
    lines = ["📦 수출 발표"]
    rec_path = os.path.join(NEWS_DIR, "exports.csv")
    new_file = not os.path.exists(rec_path)
    os.makedirs(NEWS_DIR, exist_ok=True)
    with open(rec_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["seen_at", "pub", "title", "export_100m_usd", "export_yoy_pct", "semi_yoy_pct", "link"])
        all_text = ""
        for it in items[:6]:
            p = parse_export_title(it["title"])
            w.writerow([now().strftime("%Y-%m-%d %H:%M"), it["pub"].strftime("%Y-%m-%d %H:%M"), it["title"],
                        p.get("export_100m_usd", ""), p.get("export_yoy_pct", ""), p.get("semi_yoy_pct", ""), it["link"]])
            nums = " · ".join(f"{k}={v}" for k, v in p.items())
            lines.append(f" • {it['title'][:80]}" + (f"  [{nums}]" if nums else ""))
            all_text += it["title"]
    secs = sectors_in(all_text)
    if secs:
        lines.append(" 관련 업종: " + " / ".join(f"{s}({SECTOR_MAP[s][1]})" for s in secs))
    if d in (1, 2, 3):
        lines.append(" ※ 1일 발표 반응일은 업종 변동폭이 평소 1.1~1.4배 (방향은 예상 대비 서프라이즈가 결정)")
    lines.append(" ※ 반도체가 발표일에 크게 오르면 다음날 평균 -0.85% 되돌림 (2021~2026, 28회 — 표본 작음)")
    seen["links"] = list(links)[-500:]
    save_json(os.path.join(NEWS_DIR, "seen.json"), seen)
    return lines


# ───────────────────────────── ② DART 공시
def dart_list(key, bgn, end):
    out, page = [], 1
    while True:
        r = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=20,
                         params={"crtfc_key": key, "bgn_de": bgn, "end_de": end, "page_no": page, "page_count": 100})
        j = r.json()
        if j.get("status") not in ("000",):
            if j.get("status") != "013":                     # 013 = 조회된 데이터 없음
                print(f"  ⚠ DART: {j.get('status')} {j.get('message')}")
            break
        out += j.get("list", [])
        if page >= int(j.get("total_page", 1)) or page >= 20:
            break
        page += 1
    return out


def dart_section(key, watch=set()):
    if not key:
        return ["🏛 DART: DART_API_KEY 없음 — 공시 생략"]
    t = now()
    since = (t - timedelta(days=3 if t.weekday() == 0 else 1))      # 월요일이면 금요일부터
    rows = dart_list(key, since.strftime("%Y%m%d"), t.strftime("%Y%m%d"))
    seen = load_json(os.path.join(NEWS_DIR, "dart_seen.json"), {"rcept": []})
    done = set(seen["rcept"])
    good, bad = [], []
    for x in rows:
        if not x.get("stock_code") or x["rcept_no"] in done:
            continue
        nm = x.get("report_nm", "")
        tag = "good" if any(k in nm for k in DART_GOOD) else ("bad" if any(k in nm for k in DART_BAD) else None)
        if not tag or "기재정정" in nm and tag == "good":
            continue
        star = "★" if x["stock_code"] in watch else ""
        line = f" {star}{x['corp_name']}({x['stock_code']}) {nm.strip()[:40]}"
        (good if tag == "good" else bad).append((star != "", line))
        done.add(x["rcept_no"])
    seen["rcept"] = list(done)[-5000:]
    save_json(os.path.join(NEWS_DIR, "dart_seen.json"), seen)
    lines = [f"🏛 DART 주요 공시 ({since:%m-%d} 이후, 호재성 {len(good)} · 악재성 {len(bad)})"]
    for title, arr in (("🟢 호재성", good), ("🔴 악재성", bad)):
        if arr:
            arr.sort(key=lambda z: not z[0])                # ★ 먼저
            lines.append(title)
            lines += [l for _, l in arr[:15]]
            if len(arr) > 15:
                lines.append(f"  … 외 {len(arr) - 15}건")
    return lines


def surge_watch():
    files = sorted(glob.glob(os.path.join(BASE, "surge", "candidates_*.json")))
    if not files:
        return set(), []
    J = load_json(files[-1], {})
    c = J.get("candidates", [])
    lines = [f"🔥 오늘 거래량 급증 후보 ({J.get('base_date', '')} 일봉 기준)"] + \
            [f" {x['rank']}. {x['name']}({x['code']}) 거래량 {x['vol_ratio']}배" for x in c[:10]]
    return {x["code"] for x in c}, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--force-exports", action="store_true")
    a = ap.parse_args()
    watch, surge_lines = surge_watch()
    parts = [f"📰 장전 브리핑 {now():%m-%d}({'월화수목금토일'[now().weekday()]}) {now():%H:%M}"]
    for fn in (lambda: export_section(a.force_exports), lambda: dart_section(os.environ.get("DART_API_KEY", "").strip(), watch)):
        try:
            sec = fn()
            if sec:
                parts.append("\n".join(sec))
        except Exception as e:  # noqa
            parts.append(f"⚠ 수집 오류: {e}")
    if surge_lines:
        parts.append("\n".join(surge_lines))
    text = "\n\n".join(parts)
    print(text)
    if not a.no_telegram:
        from box_paper_trader import telegram
        telegram(text[:4000])


if __name__ == "__main__":
    main()
