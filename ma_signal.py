#!/usr/bin/env python3
"""
이동평균선 히스테리시스 시그널 + 포지션 비중 스케일링

[상태 판정] 종목별로 20/60/120/200일선 각각 ON/OFF 상태를 추적한다.
  OFF -> ON : 종가 > MA * 1.02  AND  당일 상승
  ON -> OFF : 종가 < MA * 0.98  AND  당일 하락
  그 외      : 전일 상태 유지

  진입 문턱(+2%)과 이탈 문턱(-2%)이 달라 그 사이 구간에서는
  상태가 바뀌지 않는다(히스테리시스). 횡보장 휩소를 억제한다.

[포지션 비중] ON 개수에 따라 종목별로 독립 산정
  4개 -> 100%   3개 -> 75%   2개 -> 50%   1개 -> 25%   0개 -> 0%

[데이터 소스]
  미국 종목 : yfinance
  국내 종목 : pykrx(KRX 원본) 우선, 실패 시 yfinance 로 폴백

  새벽 6시(KST)에 돌리면 미국은 1시간 전 마감분이 이미 반영되지만,
  Yahoo 의 KRX 일봉은 전일 마감분이 아직 안 붙어 있는 경우가 많아
  국내 종목만 하루 뒤처진 값이 나온다. KRX 에서 직접 받으면 마감 직후
  바로 반영되므로 이 시차가 사라진다.

환경변수:
  TELEGRAM_BOT_TOKEN  (필수)
  TELEGRAM_CHAT_ID    (필수)
  ALWAYS_SEND         (선택) "false"면 비중 변동이 있을 때만 전송. 기본 true
  KR_SOURCE           (선택) "pykrx"(기본) 또는 "yfinance"
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
import yfinance as yf

# ===== 설정 ============================================================
TICKERS = {
    "NVDA":      "엔비디아",
    "AAPL":      "애플",
    "GOOGL":     "구글",
    "MSFT":      "마이크로소프트",
    "MU":        "마이크론",
    "AMZN":      "아마존",
    "AMD":       "AMD",
    "AVGO":      "브로드컴",
    "META":      "메타",
    "TSLA":      "테슬라",
    "SNDK":      "샌디스크",
    "MRVL":      "마벨테크놀로지",
    "PLTR":      "팔란티어",
    "GEV":       "GE버노바",
    "ETN":       "이튼",
    "LEU":       "센트러스에너지",
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "012330.KS": "현대모비스",
    "009150.KS": "삼성전기",
    "017670.KS": "SK텔레콤",
    "079550.KS": "LIG디펜스앤에어로스페이스",
    "012450.KS": "한화에어로스페이스",
    "016360.KS": "삼성증권",
    "003230.KS": "삼양식품",
}

MA_PERIODS = [20, 60, 120, 200]

BAND_UP = 1.02          # 매수(ON) 문턱  MA +2%
BAND_DN = 0.98          # 매도(OFF) 문턱 MA -2%

SCALAR_MAP = {4: 1.00, 3: 0.75, 2: 0.50, 1: 0.25, 0: 0.00}

CONFIRM_DIRECTION = True   # 상태 전환 시 당일 등락 방향 확인

LOOKBACK = "3y"         # 200일선 + 히스테리시스 워밍업에 충분한 기간
KR_CAL_DAYS = 1200      # pykrx 조회 기간(달력일). 3년 상당
RETRIES = 4
STALE_KR_DAYS = 4       # 국내 최신봉이 이보다 오래되면 경고
TG_MAX_LEN = 3800
KST = timezone(timedelta(hours=9))
KR_SUFFIX = (".KS", ".KQ")
# =======================================================================


def env(key: str, default: str = "") -> str:
    """빈 문자열로 주입된 환경변수도 미설정으로 취급한다.

    GitHub Actions 는 정의되지 않은 vars.* 를 빈 문자열로 넘기므로
    os.environ.get(key, default) 만으로는 기본값이 적용되지 않는다.
    """
    return (os.environ.get(key) or default).strip()


def esc(s) -> str:
    """텔레그램 HTML 파싱 오류 방지."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"


def is_kr(ticker: str) -> bool:
    return ticker.endswith(KR_SUFFIX)


def cur(ticker: str) -> str:
    return "₩" if is_kr(ticker) else "$"


def _clean(s) -> pd.Series:
    """Close 시리즈 정규화. 0/NaN 제거, tz 제거."""
    if s is None or len(s) == 0:
        return None
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = pd.to_numeric(s, errors="coerce").dropna()
    s = s[s > 0]
    if len(s) == 0:
        return None
    if getattr(s.index, "tz", None) is not None:
        s.index = s.index.tz_localize(None)
    return s.sort_index()


# ---------- 데이터 수집: 미국 (yfinance 배치) ---------------------------
def fetch_batch(tickers: list):
    """여러 종목을 한 번에 받아 {ticker: Series} 반환. 실패 시 재시도."""
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            df = yf.download(
                tickers, period=LOOKBACK, interval="1d",
                auto_adjust=True, progress=False, threads=False,
                group_by="column",
            )
            if df is not None and not df.empty:
                out = {}
                close = df["Close"]
                if isinstance(close, pd.Series):        # 단일 종목인 경우
                    out[tickers[0]] = _clean(close)
                else:
                    for t in tickers:
                        if t in close.columns:
                            s = _clean(close[t])
                            if s is not None:
                                out[t] = s
                return out, None
            last_err = "빈 응답"
        except Exception as e:                          # noqa: BLE001
            last_err = str(e)

        if attempt < RETRIES:
            wait = 4 * attempt
            print(f"  배치 수신 실패({last_err}) — {wait}초 후 재시도 "
                  f"{attempt}/{RETRIES - 1}", file=sys.stderr)
            time.sleep(wait)

    return {}, last_err


# ---------- 데이터 수집: 국내 (pykrx 우선) ------------------------------
def fetch_kr_pykrx(tickers: list):
    """KRX 원본에서 종목별로 조회.

    Yahoo 는 국내 일봉 갱신이 늦어 새벽 실행 시 전일 마감분이 빠지는 반면,
    KRX 는 마감 직후 확정된다.
    """
    from pykrx import stock                              # 지연 임포트

    now = datetime.now(KST)
    todate = now.strftime("%Y%m%d")
    fromdate = (now - timedelta(days=KR_CAL_DAYS)).strftime("%Y%m%d")

    out, errs = {}, []
    for ticker in tickers:
        code = ticker.split(".")[0]
        df = None
        try:
            try:
                df = stock.get_market_ohlcv_by_date(
                    fromdate, todate, code, adjusted=True)
            except TypeError:                            # 구버전 시그니처
                df = stock.get_market_ohlcv_by_date(fromdate, todate, code)
        except Exception as e:                           # noqa: BLE001
            errs.append(f"{ticker}: {e}")

        if df is not None and len(df) and "종가" in df.columns:
            s = _clean(df["종가"])
            if s is not None:
                out[ticker] = s
        time.sleep(0.3)

    return out, errs


def fetch_all():
    """미국/국내를 나눠 받는다 (거래일이 달라 함께 받으면 데이터가 잘림)."""
    us = [t for t in TICKERS if not is_kr(t)]
    kr = [t for t in TICKERS if is_kr(t)]

    prices, errors = {}, []

    if us:
        data, err = fetch_batch(us)
        prices.update(data)
        if err:
            errors.append(f"US 배치: {err}")
        print(f"US: {len(data)}/{len(us)}종목 수신")
        time.sleep(1)

    if kr:
        kr_source = env("KR_SOURCE", "pykrx").lower()
        got = {}

        if kr_source == "pykrx":
            try:
                got, errs = fetch_kr_pykrx(kr)
                if errs:
                    errors.append(f"pykrx: {len(errs)}종목 오류")
                print(f"KR[pykrx]: {len(got)}/{len(kr)}종목 수신")
            except ImportError:
                errors.append("pykrx 미설치 — yfinance 로 폴백")
                print("KR: pykrx 미설치 — yfinance 폴백", file=sys.stderr)

        missing = [t for t in kr if t not in got]
        if missing:                                      # 폴백
            data, err = fetch_batch(missing)
            got.update(data)
            if err:
                errors.append(f"KR 배치: {err}")
            print(f"KR[yfinance 폴백]: {len(data)}/{len(missing)}종목 수신")

        prices.update(got)

    return prices, errors


# ---------- 상태 머신 ---------------------------------------------------
def compute_states(close: pd.Series):
    """(정보 dict, 오류메시지) 반환."""
    need = max(MA_PERIODS) + 30      # 히스테리시스 워밍업 여유
    if len(close) < need:
        return None, f"데이터 부족 ({len(close)}일 / 최소 {need}일)"

    mas = {n: close.rolling(n).mean() for n in MA_PERIODS}
    state = {n: 0 for n in MA_PERIODS}
    prev_snapshot = None

    for i in range(max(MA_PERIODS), len(close)):
        price = float(close.iloc[i])
        rising = price > float(close.iloc[i - 1])
        falling = price < float(close.iloc[i - 1])

        nxt = {}
        for n in MA_PERIODS:
            ma = mas[n].iloc[i]
            if pd.isna(ma):
                nxt[n] = 0
                continue
            ma = float(ma)
            s = state[n]
            if s == 1:
                if price < ma * BAND_DN and (falling or not CONFIRM_DIRECTION):
                    s = 0
            else:
                if price > ma * BAND_UP and (rising or not CONFIRM_DIRECTION):
                    s = 1
            nxt[n] = s

        if i == len(close) - 1:
            prev_snapshot = dict(state)     # 마지막 봉 직전 상태
        state = nxt

    if prev_snapshot is None:
        return None, "상태 계산 실패"

    last = float(close.iloc[-1])
    before = float(close.iloc[-2])
    return {
        "today": dict(state),
        "yesterday": prev_snapshot,
        "price": last,
        "pct": (last / before - 1) * 100 if before else 0.0,
        "date": close.index[-1].strftime("%m/%d"),
        "ts": pd.Timestamp(close.index[-1]),
    }, None


# ---------- 리포트 ------------------------------------------------------
def build_report():
    prices, errors = fetch_all()

    now_kst = datetime.now(KST)
    now = now_kst.strftime("%Y-%m-%d")
    rows, changes, failed = [], [], []
    last_ts = {"US": None, "KR": None}

    for ticker, name in TICKERS.items():
        close = prices.get(ticker)
        if close is None or close.empty:
            failed.append(f"{name} ({ticker}) — 데이터 없음")
            continue

        info, err = compute_states(close)
        if info is None:
            failed.append(f"{name} ({ticker}) — {err}")
            continue

        mkt = "KR" if is_kr(ticker) else "US"
        if last_ts[mkt] is None or info["ts"] > last_ts[mkt]:
            last_ts[mkt] = info["ts"]

        t_on = sum(info["today"].values())
        y_on = sum(info["yesterday"].values())
        t_w, y_w = SCALAR_MAP[t_on], SCALAR_MAP[y_on]

        if t_w != y_w:
            flips = []
            for n in MA_PERIODS:
                if info["today"][n] > info["yesterday"][n]:
                    flips.append(f"{n}일↑")
                elif info["today"][n] < info["yesterday"][n]:
                    flips.append(f"{n}일↓")
            mark = "🔴" if t_w > y_w else "🔵"
            changes.append(
                f"{mark} <b>{esc(name)}</b>  {y_w:.0%} → <b>{t_w:.0%}</b>"
                f"  ({', '.join(flips)})"
            )

        dots = "".join("●" if info["today"][n] else "○" for n in MA_PERIODS)
        arrow = "▲" if info["pct"] > 0 else ("▼" if info["pct"] < 0 else "―")
        rows.append(
            f"{dots} <b>{t_w:.0%}</b>  {esc(name)}\n"
            f"     {cur(ticker)}{fmt(info['price'])} {arrow}{abs(info['pct']):.1f}%"
            f"  <code>{esc(ticker)}</code>"
        )

    # 국내 데이터가 뒤처졌는지 확인
    kr_ts = last_ts["KR"]
    if kr_ts is not None:
        gap = (pd.Timestamp(now_kst.date()) - kr_ts.normalize()).days
        if gap > STALE_KR_DAYS:
            errors.append(f"국내 최신봉이 {kr_ts.strftime('%m/%d')} — "
                          f"{gap}일 전 (갱신 지연 의심)")

    lines = ["<b>📊 이평선 히스테리시스 시그널</b>", f"<i>{now} KST</i>"]
    stamp = []
    if last_ts["US"] is not None:
        stamp.append(f"US {last_ts['US'].strftime('%m/%d')}")
    if kr_ts is not None:
        stamp.append(f"KR {kr_ts.strftime('%m/%d')}")
    if stamp:
        lines.append(f"<i>기준: {' · '.join(stamp)} 마감</i>")
    lines.append("")

    if changes:
        lines.append(f"<b>■ 비중 변동 {len(changes)}건</b>")
        lines += changes
    else:
        lines.append("<b>■ 비중 변동 없음</b>")
    lines.append("")

    lines.append("<b>■ 목표 비중</b>")
    lines.append("<i>● = 20/60/120/200일선 ON</i>")
    lines += rows

    if failed:
        lines += ["", "<b>⚠️ 처리 실패</b>"] + [f"· {esc(f)}" for f in failed]
    if errors:
        lines += ["", "<b>⚠️ 수신 경고</b>"] + [f"· {esc(e)}" for e in errors]

    return "\n".join(lines), len(changes), len(rows)


# ---------- 텔레그램 ----------------------------------------------------
def send_telegram(text: str) -> bool:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — 전송 생략.",
              file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > TG_MAX_LEN:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf)

    ok = True
    for chunk in chunks:
        try:
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=20,
            )
            if r.status_code != 200:
                print(f"텔레그램 전송 실패 {r.status_code}: {r.text}",
                      file=sys.stderr)
                ok = False
        except Exception as e:                          # noqa: BLE001
            print(f"텔레그램 전송 오류: {e}", file=sys.stderr)
            ok = False
        time.sleep(0.5)
    return ok


# ---------- 진입점 ------------------------------------------------------
def main():
    report, n_changes, n_ok = build_report()

    plain = report
    for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
        plain = plain.replace(tag, "")
    print(plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))

    if n_ok == 0:
        print("\n전 종목 처리 실패.", file=sys.stderr)
        send_telegram(report)
        sys.exit(1)

    always = env("ALWAYS_SEND", "true").lower() != "false"
    if n_changes == 0 and not always:
        print("\n변동이 없어 전송하지 않았습니다 (ALWAYS_SEND=false).")
        return

    if not send_telegram(report):
        sys.exit(1)
    print("\n텔레그램 전송 완료.")


if __name__ == "__main__":
    main()
