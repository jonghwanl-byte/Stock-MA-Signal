#!/usr/bin/env python3
"""
이동평균선 ±2% 교차 + 전일 방향 확인 시그널 스크리너
결과를 텔레그램으로 전송합니다.

[매수 조건] 이평선 n에 대해
  - T-2일: 종가가 MA(n) * 1.02 를 아래 -> 위로 교차
  - T-1일: 종가가 T-2일 종가보다 상승

[매도 조건]
  - T-2일: 종가가 MA(n) * 0.98 을 위 -> 아래로 교차
  - T-1일: 종가가 T-2일 종가보다 하락

n = 20, 60, 120, 200 (각각 독립 판정)

환경변수:
  TELEGRAM_BOT_TOKEN  (필수) BotFather에서 발급
  TELEGRAM_CHAT_ID    (필수) 메시지를 받을 채팅 ID
  ALWAYS_SEND         (선택) "false"면 시그널 있을 때만 전송. 기본 true
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
    "MSFT":      "마이크로소프트",
    "GOOGL":     "구글",
    "AMZN":      "아마존",
    "META":      "메타",
    "LEU":       "센트러스에너지",
    "005930.KS": "삼성전자",
}

MA_PERIODS = [20, 60, 120, 200]
BAND = 0.02            # ±2%
LOOKBACK = "2y"        # 200일선 계산에 충분한 기간
RETRIES = 4            # yfinance 재시도 횟수
TG_MAX_LEN = 3800      # 텔레그램 메시지 분할 기준
KST = timezone(timedelta(hours=9))
# =======================================================================


# ---------- 데이터 수집 -------------------------------------------------
def fetch(ticker: str):
    """yfinance에서 일봉을 받아온다. 실패 시 지수 백오프로 재시도."""
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            df = yf.download(
                ticker, period=LOOKBACK, interval="1d",
                auto_adjust=True, progress=False, threads=False,
            )
            if df is not None and not df.empty:
                return df, None
            last_err = "빈 응답"
        except Exception as e:                      # noqa: BLE001
            last_err = str(e)

        if attempt < RETRIES:
            wait = 3 * attempt
            print(f"  [{ticker}] 실패({last_err}) — {wait}초 후 재시도 "
                  f"{attempt}/{RETRIES - 1}", file=sys.stderr)
            time.sleep(wait)

    return None, last_err


# ---------- 시그널 판정 -------------------------------------------------
def analyze(ticker: str):
    """(시그널 리스트, 정보 dict) 또는 (None, 오류메시지) 반환."""
    df, err = fetch(ticker)
    if df is None:
        return None, f"데이터 수신 실패: {err}"

    close = df["Close"]
    if isinstance(close, pd.DataFrame):      # yfinance 버전별 컬럼 형태 대응
        close = close.iloc[:, 0]
    close = close.dropna()

    need = max(MA_PERIODS) + 3
    if len(close) < need:
        return None, f"데이터 부족 ({len(close)}일 / 최소 {need}일 필요)"

    # 인덱스: -1 = 전일, -2 = 전전일, -3 = 그 전날
    c_prev = float(close.iloc[-1])
    c_t2 = float(close.iloc[-2])
    c_t3 = float(close.iloc[-3])

    prev_up = c_prev > c_t2
    prev_down = c_prev < c_t2
    prev_pct = (c_prev / c_t2 - 1) * 100 if c_t2 else 0.0

    signals = []
    ma_now = {}
    for n in MA_PERIODS:
        ma = close.rolling(n).mean()
        ma_t2, ma_t3 = float(ma.iloc[-2]), float(ma.iloc[-3])
        ma_now[n] = float(ma.iloc[-1])

        crossed_up = (c_t3 <= ma_t3 * (1 + BAND)) and (c_t2 > ma_t2 * (1 + BAND))
        crossed_dn = (c_t3 >= ma_t3 * (1 - BAND)) and (c_t2 < ma_t2 * (1 - BAND))

        if crossed_up and prev_up:
            signals.append((n, "매수", ma_t2 * (1 + BAND)))
        elif crossed_dn and prev_down:
            signals.append((n, "매도", ma_t2 * (1 - BAND)))

    info = {
        "date_t2": close.index[-2].strftime("%m/%d"),
        "date_prev": close.index[-1].strftime("%m/%d"),
        "c_t2": c_t2,
        "c_prev": c_prev,
        "prev_pct": prev_pct,
        "ma": ma_now,
    }
    return signals, info


# ---------- 리포트 작성 -------------------------------------------------
def fmt(v: float) -> str:
    return f"{v:,.0f}" if v >= 1000 else f"{v:,.2f}"


def build_report():
    """(HTML 본문, 시그널 개수) 반환."""
    today = datetime.now(KST).strftime("%Y-%m-%d (%a)")
    lines = [f"<b>📊 이평선 교차 시그널</b>", f"<i>{today} KST</i>", ""]
    total = 0

    for ticker, name in TICKERS.items():
        signals, info = analyze(ticker)

        if signals is None:
            lines += [f"<b>{name}</b> ({ticker})", f"  ⚠️ {info}", ""]
            continue

        arrow = "▲" if info["prev_pct"] > 0 else ("▼" if info["prev_pct"] < 0 else "―")
        lines.append(f"<b>{name}</b> ({ticker})")
        lines.append(
            f"  전일 {info['date_prev']}  <b>{fmt(info['c_prev'])}</b>  "
            f"{arrow} {info['prev_pct']:+.2f}%"
        )

        if signals:
            total += len(signals)
            for n, side, band_price in signals:
                icon = "🔴" if side == "매수" else "🔵"
                lines.append(
                    f"  {icon} <b>{side}</b> — {n}일선 "
                    f"(밴드 {fmt(band_price)} 교차)"
                )
        else:
            ma_txt = " / ".join(f"{n}:{fmt(v)}" for n, v in info["ma"].items())
            lines.append(f"  · 시그널 없음")
            lines.append(f"  <code>MA {ma_txt}</code>")
        lines.append("")

    lines.append(f"— 총 <b>{total}</b>건")
    return "\n".join(lines), total


# ---------- 텔레그램 ----------------------------------------------------
def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없어 전송을 건너뜁니다.",
              file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # 4096자 제한 대응: 줄 단위로 분할
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
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            if r.status_code != 200:
                print(f"텔레그램 전송 실패 {r.status_code}: {r.text}", file=sys.stderr)
                ok = False
        except Exception as e:                      # noqa: BLE001
            print(f"텔레그램 전송 오류: {e}", file=sys.stderr)
            ok = False
        time.sleep(0.5)

    return ok


# ---------- 진입점 ------------------------------------------------------
def main():
    report, total = build_report()

    # 로그(Actions 화면)에도 남긴다 — 태그 제거한 평문
    plain = (report.replace("<b>", "").replace("</b>", "")
                   .replace("<i>", "").replace("</i>", "")
                   .replace("<code>", "").replace("</code>", ""))
    print(plain)

    always = os.environ.get("ALWAYS_SEND", "true").lower() != "false"
    if total == 0 and not always:
        print("\n시그널이 없어 전송하지 않았습니다 (ALWAYS_SEND=false).")
        return

    if not send_telegram(report):
        sys.exit(1)
    print("\n텔레그램 전송 완료.")


if __name__ == "__main__":
    main()
