"""CGV 예매 오픈 감시 -> ntfy 푸시 알림

사용법
  python check.py            # 감시 (기본, 일정 시간 동안 반복)
  python check.py test       # 테스트 알림 1건 전송
  python check.py diagnose   # 접속/지점코드/영화코드 확인 (알림 없음)
"""
import json
import os
import random
import sys
import time
import datetime as dt

import requests as plain_requests          # ntfy 전송용
from curl_cffi import requests as cr      # CGV 접속용 (크롬 TLS 지문 흉내)

BASE = "https://cgv.co.kr/api/v1/booking"
CO_CD = "A420"
MOV_NO = os.environ.get("CGV_MOV_NO", "30001367")
# 형식: 지점코드:이름,지점코드:이름
SITES = dict(
    s.split(":", 1) for s in os.environ.get(
        "CGV_SITES", "0074:왕십리,0013:용산아이파크몰").split(",") if ":" in s
)
TARGET_WEEKDAYS = {2, 5}  # 월=0 -> 수=2, 토=5
WD_KR = "월화수목금토일"
TOPICS = [t.strip() for t in os.environ.get("NTFY_TOPICS", "").split(",") if t.strip()]
INTERVAL = int(os.environ.get("LOOP_INTERVAL", "90"))        # 확인 간격(초)
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "20400"))  # 한 번 실행 시간(5시간 40분)
STATE_FILE = "state.json"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://cgv.co.kr/cnm/movieBook/movie",
}


def notify(title, message, priority=4, click=None):
    if not TOPICS:
        print("[알림 생략] NTFY_TOPICS 없음:", title, message)
        return
    for topic in TOPICS:
        body = {"topic": topic, "title": title, "message": message, "priority": priority}
        if click:
            body["click"] = click
        try:
            r = plain_requests.post("https://ntfy.sh/", json=body, timeout=15)
            print(f"[ntfy] {r.status_code}")
        except Exception as e:
            print("[ntfy 실패]", e)


def get_json(path, **params):
    r = cr.get(f"{BASE}/{path}", params=params, headers=HEADERS,
               impersonate="chrome", timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} (차단 가능성) {r.text[:150]}")
    return r.json()


def fetch_dates(site_no):
    """이 극장에서 영화가 예매 가능한 날짜 목록 (YYYY-MM-DD)"""
    j = get_json("searchSiteScnscYmdListByMov", coCd=CO_CD, siteNo=site_no, movNo=MOV_NO)
    if j.get("statusCode") != 0:
        raise RuntimeError(f"API 오류 응답: {str(j)[:150]}")
    out = []
    for item in j.get("data") or []:
        ymd = item.get("scnYmd", "")
        if len(ymd) == 8:
            out.append(f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}")
    return sorted(out)


def fmt(d):
    day = dt.date.fromisoformat(d)
    return f"{day.month}/{day.day}({WD_KR[day.weekday()]})"


def is_target(d):
    return dt.date.fromisoformat(d).weekday() in TARGET_WEEKDAYS


def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)


def watch():
    state = load_state()
    fails = {}      # 지점별 연속 실패 횟수
    warned = set()  # 실패 경고를 이미 보낸 지점
    end = time.time() + LOOP_SECONDS
    print(f"감시 시작: 영화 {MOV_NO}, 지점 {SITES}, {INTERVAL}초 간격")

    while time.time() < end:
        for site_no, name in SITES.items():
            try:
                dates = fetch_dates(site_no)
                fails[site_no] = 0
                warned.discard(site_no)
            except Exception as e:
                fails[site_no] = fails.get(site_no, 0) + 1
                print(f"[{name}] 실패 {fails[site_no]}회: {e}")
                if fails[site_no] >= 10 and site_no not in warned:
                    warned.add(site_no)
                    notify("⚠️ CGV 감시 문제", f"{name} 접속이 계속 실패해요. 차단됐을 수 있어요.\n{e}", priority=3)
                continue

            if site_no not in state:
                # 첫 실행: 기준선만 저장하고 현재 상황을 한 번 알려줌
                state[site_no] = dates
                now_t = [fmt(d) for d in dates if is_target(d)]
                notify(f"✅ CGV {name} 감시 시작",
                       "현재 예매 가능한 수/토: " + (", ".join(now_t) if now_t else "없음"),
                       priority=3)
            else:
                known = set(state[site_no])
                new = [d for d in dates if d not in known]
                new_t = [d for d in new if is_target(d)]
                if new_t:
                    notify(f"🎬 CGV {name} 예매 오픈!",
                           ", ".join(fmt(d) for d in new_t) + " 열렸어요",
                           priority=5, click="https://cgv.co.kr/cnm/movieBook/movie")
                state[site_no] = sorted(known | set(dates))
            save_state(state)
            time.sleep(random.uniform(1, 3))
        time.sleep(INTERVAL + random.uniform(-10, 10))


def diagnose():
    print("영화코드:", MOV_NO)
    for site_no, name in SITES.items():
        try:
            d = fetch_dates(site_no)
            print(f"[{name} {site_no}] OK, 예매 가능 날짜 {len(d)}개:", d)
        except Exception as e:
            print(f"[{name} {site_no}] 실패:", e)
    # 지점코드 찾기 (영등포 등)
    for kw in os.environ.get("FIND_SITES", "영등포").split(","):
        try:
            j = get_json("searchRegnList", coCd=CO_CD, movNo=MOV_NO, srchKwrd=kw.strip())
            print(f"[검색 '{kw}']", json.dumps(j, ensure_ascii=False)[:1500])
        except Exception as e:
            print(f"[검색 '{kw}'] 실패:", e)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if mode == "test":
        notify("🎬 테스트 알림", "이 알림이 보이면 설정 성공이에요!", priority=3)
    elif mode == "diagnose":
        diagnose()
    else:
        watch()
