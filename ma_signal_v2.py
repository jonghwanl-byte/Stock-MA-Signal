#!/usr/bin/env python3
"""
이동평균선 히스테리시스 시그널 + 포지션 비중 스케일링  (v2)

v1(ma_signal.py) 대비 변경점 — backtest_v2.py 비교 결과 반영:
  1) 60일선 제외: 20/120/200 3개 MA로 운영 (백테스트 Sharpe 개선 확인)
  2) 거래량 확인 필터 추가: 신규 진입(OFF->ON)에는 당일 거래량이
     20일 평균거래량의 1.5배 이상이어야 함. 청산(ON->OFF)은 필터 없이
     그대로 — 손절/이익실현 타이밍을 거래량 때문에 늦추지 않기 위함.
  3) 레짐 필터는 백테스트에서 Sharpe를 오히려 낮춰(lookback 20/60일 모두)
     채택하지 않음. 이 파일에는 포함되어 있지 않다.
  기존 ma_signal.py 는 그대로 두고 이 파일을 별도로 운영/검증한 뒤
  교체하는 것을 권장한다 (ma_signal_v1_crossover.py 보존 관례와 동일).

[상태 판정] 종목별로 20/120/200일선 각각 ON/OFF 상태를 추적한다.
  OFF -> ON : 종가 > MA * BAND_UP  AND  당일 상승  AND  거래량 조건 충족
  ON -> OFF : 종가 < MA * BAND_DN  AND  당일 하락               (필터 없음)
  그 외      : 전일 상태 유지

  진입 문턱과 이탈 문턱이 달라 그 사이 구간에서는 상태가 바뀌지
  않는다(히스테리시스). 횡보장 휩소를 억제한다.

[거래량 조건] 당일 거래량 >= 최근 VOLUME_LOOKBACK일 평균거래량 * VOLUME_MULT
  거래량 데이터가 없거나 워밍업 중이면(NaN) 조건을 막지 않고 통과시킨다
  (보수적으로 신호를 죽이지 않는 방향).

[포지션 비중] ON 개수에 따라 종목별로 독립 산정 (3단계)
  3개 -> 100%   2개 -> 66%   1개 -> 33%   0개 -> 0%

[데이터 소스]
  미국 종목 : yfinance (Close + Volume)
  국내 종목 : pykrx(KRX 원본, 종가+거래량) 우선, 실패 시 yfinance 로 폴백

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
    "ASML":      "ASML",
    "NFLX":      "Netflix",
    "QCOM":      "퀄컴",
    "ADBE":      "Adobe",
    "AMAT":      "어플라이드 머티어리얼즈",
    "LRCX":      "램 리서치",
    "MU":        "마이크론 테크놀로지",
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
    "373220.KS": "LG에너지솔루션",
    "207940.KS": "삼성바이오로직스",
    "005380.KS": "현대차",
    "000270.KS": "기아",
    "068270.KS": "셀트리온",
    "105560.KS": "KB금융",
    "035420.KS": "NAVER",
    "055550.KS": "신한지주",
    "006400.KS": "삼성SDI",
    "051910.KS": "LG화학",
    "028260.KS": "삼성물산",
    "035720.KS": "카카오",
    "086790.KS": "하나금융지주",
    "066570.KS": "LG전자",
    "032830.KS": "삼성생명",
    "034730.KS": "SK",
    "011200.KS": "HMM",
    "015760.KS": "한국전력",
    "000810.KS": "삼성화재",
    "034020.KS": "두산에너빌리티",
    "010130.KS": "고려아연",
    "033780.KS": "KT&G",
    "003670.KS": "포스코퓨처엠",
    "009540.KS": "HD한국조선해양",
    "450080.KS": "에코프로머티",
    "003490.KS": "대한항공",
    "010950.KS": "S-Oil",
    "042660.KS": "한화오션",
    "018260.KS": "삼성에스디에스",
    "036570.KS": "엔씨소프트",
    "047050.KS": "포스코인터내셔널",
    "090430.KS": "아모레퍼시픽",
    "086280.KS": "현대글로비스",
    "030200.KS": "KT",
    "034020.KS": "LG",
    "241560.KS": "두산밥캣",
    "096770.KS": "SK이노베이션",
    "051900.KS": "LG생활건강",
    "009830.KS": "한화솔루션"
}

MA_PERIODS = [20, 120, 200]      # 60일선 제외 (backtest_v2 결과 반영)

BAND_UP = 1.02          # 매수(ON) 문턱  MA +2%   ※ 기존 값 유지, 백테스트는 1.03으로 검증했음 — 하단 주석 참고
BAND_DN = 0.98          # 매도(OFF) 문턱 MA -2%

SCALAR_MAP = {3: 1.00, 2: 0.66, 1: 0.33, 0: 0.00}   # 3단계 (MA 3개 기준)

CONFIRM_DIRECTION = True   # 상태 전환 시 당일 등락 방향 확인

VOLUME_FILTER = True       # 신규 진입에만 적용 (청산은 필터 없음)
VOLUME_MULT = 1.5          # 평균거래량 대비 배수
VOLUME_LOOKBACK = 20       # 평균거래량 계산 기간(거래일)

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
    """여러 종목을 한 번에 받아 (close_dict, volume_dict) 반환. 실패 시 재시도."""
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            df = yf.download(
                tickers, period=LOOKBACK, interval="1d",
                auto_adjust=True, progress=False, threads=False,
                group_by="column",
            )
            if df is not None and not df.empty:
                out_c, out_v = {}, {}
                close = df["Close"]
                volume = df["Volume"] if "Volume" in df else None
                if isinstance(close, pd.Series):        # 단일 종목인 경우
                    s = _clean(close)
                    if s is not None:
                        out_c[tickers[0]] = s
                        if volume is not None:
                            v = _clean(volume).reindex(s.index) if volume is not None else None
                            out_v[tickers[0]] = v
                else:
                    for t in tickers:
                        if t in close.columns:
                            s = _clean(close[t])
                            if s is not None:
                                out_c[t] = s
                                if volume is not None and t in volume.columns:
                                    v = pd.to_numeric(volume[t], errors="coerce")
                                    out_v[t] = v.reindex(s.index)
                return out_c, out_v, None
            last_err = "빈 응답"
        except Exception as e:                          # noqa: BLE001
            last_err = str(e)

        if attempt < RETRIES:
            wait = 4 * attempt
            print(f"  배치 수신 실패({last_err}) — {wait}초 후 재시도 "
                  f"{attempt}/{RETRIES - 1}", file=sys.stderr)
            time.sleep(wait)

    return {}, {}, last_err


# ---------- 데이터 수집: 국내 (pykrx 우선) ------------------------------
def fetch_kr_pykrx(tickers: list):
    """KRX 원본에서 종목별로 조회 (종가 + 거래량).

    Yahoo 는 국내 일봉 갱신이 늦어 새벽 실행 시 전일 마감분이 빠지는 반면,
    KRX 는 마감 직후 확정된다.
    """
    from pykrx import stock                              # 지연 임포트

    now = datetime.now(KST)
    todate = now.strftime("%Y%m%d")
    fromdate = (now - timedelta(days=KR_CAL_DAYS)).strftime("%Y%m%d")

    out_c, out_v, errs = {}, {}, []
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
                out_c[ticker] = s
                if "거래량" in df.columns:
                    v = pd.to_numeric(df["거래량"], errors="coerce")
                    if getattr(v.index, "tz", None) is not None:
                        v.index = v.index.tz_localize(None)
                    out_v[ticker] = v.reindex(s.index)
        time.sleep(0.3)

    return out_c, out_v, errs


def fetch_all():
    """미국/국내를 나눠 받는다 (거래일이 달라 함께 받으면 데이터가 잘림)."""
    us = [t for t in TICKERS if not is_kr(t)]
    kr = [t for t in TICKERS if is_kr(t)]

    prices, volumes, errors = {}, {}, []

    if us:
        data_c, data_v, err = fetch_batch(us)
        prices.update(data_c)
        volumes.update(data_v)
        if err:
            errors.append(f"US 배치: {err}")
        print(f"US: {len(data_c)}/{len(us)}종목 수신")
        time.sleep(1)

    if kr:
        kr_source = env("KR_SOURCE", "pykrx").lower()
        got_c, got_v = {}, {}

        if kr_source == "pykrx":
            try:
                got_c, got_v, errs = fetch_kr_pykrx(kr)
                if errs:
                    errors.append(f"pykrx: {len(errs)}종목 오류")
                print(f"KR[pykrx]: {len(got_c)}/{len(kr)}종목 수신")
            except ImportError:
                errors.append("pykrx 미설치 — yfinance 로 폴백")
                print("KR: pykrx 미설치 — yfinance 폴백", file=sys.stderr)

        missing = [t for t in kr if t not in got_c]
        if missing:                                      # 폴백
            data_c, data_v, err = fetch_batch(missing)
            got_c.update(data_c)
            got_v.update(data_v)
            if err:
                errors.append(f"KR 배치: {err}")
            print(f"KR[yfinance 폴백]: {len(data_c)}/{len(missing)}종목 수신")

        prices.update(got_c)
        volumes.update(got_v)

    return prices, volumes, errors


# ---------- 상태 머신 ---------------------------------------------------
def compute_states(close: pd.Series, volume: pd.Series = None):
    """(정보 dict, 오류메시지) 반환.

    volume 이 주어지고 VOLUME_FILTER=True 면, 신규 진입(0->1)에 한해
    당일 거래량이 최근 VOLUME_LOOKBACK일 평균거래량의 VOLUME_MULT배
    이상이어야 한다. 청산(1->0)은 필터 없이 그대로 둔다.
    거래량 데이터가 없거나 워밍업 중(NaN)이면 조건을 막지 않는다.
    """
    need = max(MA_PERIODS) + 30      # 히스테리시스 워밍업 여유
    if len(close) < need:
        return None, f"데이터 부족 ({len(close)}일 / 최소 {need}일)"

    mas = {n: close.rolling(n).mean() for n in MA_PERIODS}

    vol_avg = None
    if volume is not None and VOLUME_FILTER:
        vol_avg = volume.rolling(VOLUME_LOOKBACK).mean().reindex(close.index)
        volume = volume.reindex(close.index)

    state = {n: 0 for n in MA_PERIODS}
    prev_snapshot = None

    for i in range(max(MA_PERIODS), len(close)):
        price = float(close.iloc[i])
        rising = price > float(close.iloc[i - 1])
        falling = price < float(close.iloc[i - 1])

        vol_ok = True
        if vol_avg is not None:
            vv, va = volume.iloc[i], vol_avg.iloc[i]
            if pd.notna(vv) and pd.notna(va):
                vol_ok = float(vv) >= float(va) * VOLUME_MULT
            # NaN(데이터 없음/워밍업)이면 막지 않음

        nxt = {}
        for n in MA_PERIODS:
            ma = mas[n].iloc[i]
            if pd.isna(ma):
                nxt[n] = 0
                continue
            ma = float(ma)
            s = state[n]
            if s == 1:
                # 청산: 거래량 조건 없이 그대로 (손절/이익실현 지연 방지)
                if price < ma * BAND_DN and (falling or not CONFIRM_DIRECTION):
                    s = 0
            else:
                # 신규 진입: 가격조건 + 방향확인 + 거래량조건 모두 충족
                if (price > ma * BAND_UP and (rising or not CONFIRM_DIRECTION)
                        and vol_ok):
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
    prices, volumes, errors = fetch_all()

    now_kst = datetime.now(KST)
    now = now_kst.strftime("%Y-%m-%d")
    rows, changes, failed = [], [], []
    last_ts = {"US": None, "KR": None}

    for ticker, name in TICKERS.items():
        close = prices.get(ticker)
        if close is None or close.empty:
            failed.append(f"{name} ({ticker}) — 데이터 없음")
            continue

        volume = volumes.get(ticker)
        info, err = compute_states(close, volume)
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
    ma_label = "/".join(str(n) for n in MA_PERIODS)
    vol_note = f" · 거래량≥평균{VOLUME_MULT:.1f}배 진입조건" if VOLUME_FILTER else ""
    lines.append(f"<i>● = {ma_label}일선 ON{vol_note}</i>")
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
