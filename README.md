# 이평선 히스테리시스 시그널 & 백테스트

이동평균선 히스테리시스 기반 매매 신호 시스템. 매일 새벽 텔레그램으로
포지션 비중을 알려주는 **라이브 신호 봇**과, 그 로직을 검증하는
**백테스트 엔진** 두 축으로 구성됩니다.

## 파일 구조

```
.
├── ma_signal.py              # 라이브 신호 (v1, 운영 중) — 20/60/120/200일선
├── ma_signal_v2.py           # 라이브 신호 (v2, 검증됨) — 20/120/200일선 + 거래량 필터
├── ma_signal_v1_crossover.py # 히스테리시스 이전 버전 (참고용 보존)
│
├── backtest.py                # 기본 백테스트 엔진 (v1 로직: 4개 MA)
├── backtest_v2.py             # v2 로직 백테스트 (3개 MA + 거래량/레짐 필터)
├── transition_analysis.py     # 상태 전환(예: 4→3) 이후 N일 수익률 분석
│
├── requirements.txt
├── .github/workflows/
│   ├── backtest.yml               # backtest.py 수동 실행
│   ├── backtest_v2.yml            # backtest_v2.py 수동 실행 (basic/compare)
│   └── transition_analysis.yml    # transition_analysis.py 수동 실행
│
└── (별도 관리) 매일 5:30 KST 신호 발송 워크플로 — 이 저장소 문서 범위 밖
```

> **참고**: 매일 아침 신호를 발송하는 GitHub Actions 워크플로(스케줄 실행)는
> 이번 문서화 범위에 포함되지 않았습니다. `ma_signal_v2.py`로 교체하려면
> 해당 워크플로의 실행 파일 경로만 바꿔주면 됩니다.

## 핵심 로직

### 상태 판정 (히스테리시스)

종목별로 각 이평선에 대해 ON/OFF 상태를 독립적으로 추적합니다.

- **OFF → ON**: 종가 > MA × BAND_UP **AND** 당일 상승 **AND** (v2는) 거래량 조건 충족
- **ON → OFF**: 종가 < MA × BAND_DN **AND** 당일 하락 — *거래량 조건 없음*
- 그 외: 전일 상태 유지

진입 문턱과 이탈 문턱이 다르기 때문에(히스테리시스) 그 사이 구간에서는
상태가 바뀌지 않아 횡보장 휩소를 억제합니다. 거래량 조건을 청산에는
적용하지 않은 이유는, 손절·이익실현 타이밍이 거래량 부족 때문에
지연되는 걸 막기 위함입니다.

### v1 → v2 변경 사항 (백테스트로 검증 완료)

| 항목 | v1 | v2 | 근거 |
|---|---|---|---|
| MA 구성 | 20/60/120/200 (4개) | 20/120/200 (3개) | 60일선 경계(4↔3)의 전환 후 수익률이 방향과 무관하게 거의 동일 — 예측력 없음이 전환 분석으로 확인됨 |
| 포지션 스케일 | 100/75/50/25% | 100/66/33/0% | MA 개수 대비 균등 배분(1/N) 원칙을 3개 MA에 그대로 적용한 결과 |
| 거래량 필터 | 없음 | 신규 진입에 한해 20일 평균거래량×1.5배 이상 요구 | `--compare` 백테스트에서 Sharpe 1.564→1.604로 개선, MDD도 축소 |
| 레짐 필터 | — | **채택 안 함** | 장기MA 기울기 기반 레짐 필터는 20일/60일 lookback 모두에서 Sharpe를 오히려 낮춤 (후행성 이중 적용 문제로 추정) |
| 진입 밴드 | +2% (BAND_UP=1.02) | +2% 유지 | 1.02와 1.03 비교 시 Sharpe 차이 0.007로 무의미 — 이탈 밴드 폭이 성과에 더 크게 작용한다는 기존 결론과 일치 |

포트폴리오 단위 최종 비교(동일가중, 25종목, 10년, SNDK 제외):

| 구성 | CAGR | MDD | Sharpe |
|---|---|---|---|
| v1 (60일선 포함, 필터 없음) | 25.92% | -17.67% | 1.564 |
| v2 (60일선 제외 + 거래량 필터) | 22.36% | -13.97% | **1.569~1.604** |

CAGR을 3~4%p 내주는 대신 MDD를 4%p 가까이 줄이고 Sharpe를 소폭 개선하는
트레이드오프입니다. 신호 자체는 여전히 "방어 도구"이지 "수익 증대 도구"가
아니라는 원래 결론은 v2에서도 유지됩니다 — 단순보유(개별종목 평균) CAGR
44.62%에는 크게 못 미칩니다.

## 사용법

### 로컬 실행

```bash
pip install -r requirements.txt

# 라이브 신호 1회 실행 (텔레그램 미설정 시 콘솔 출력만)
python ma_signal_v2.py

# 백테스트
python backtest.py --years 10                          # v1 로직
python backtest_v2.py --years 10 --no-regime-filter     # v2 최종 구성
python backtest_v2.py --years 10 --compare              # 필터 on/off 4조합 비교
python transition_analysis.py --years 10 --drop60       # 전환 구간 분석
```

### GitHub Actions

`.github/workflows/`에 넣고 저장소의 **Actions** 탭에서 `workflow_dispatch`로
수동 실행합니다. 각 워크플로는 Yahoo Finance 재다운로드를 줄이기 위해
가격 캐시(`.bt_cache.pkl` / `.bt_cache_v2.pkl`)를 `actions/cache`로 재사용합니다.
캐시가 서로 다른 이유는 v2가 Volume 데이터를 추가로 필요로 하기 때문입니다.

| 워크플로 | 대상 스크립트 | 주요 입력 |
|---|---|---|
| `backtest.yml` | `backtest.py` | mode(basic/sweep/stress), years, band_up/dn |
| `backtest_v2.yml` | `backtest_v2.py` | mode(basic/compare), volume_mult, volume_lookback, regime_lookback |
| `transition_analysis.yml` | `transition_analysis.py` | ma_config(full/drop60), horizons |

결과 CSV는 각 실행의 **Artifacts**에서 내려받을 수 있습니다.

### 환경변수 (라이브 신호 봇, `ma_signal_v2.py`)

| 변수 | 필수 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | ✅ | 전송 대상 채팅 ID |
| `ALWAYS_SEND` | - | `false`면 비중 변동 있을 때만 전송 (기본 `true`) |
| `KR_SOURCE` | - | `pykrx`(기본) 또는 `yfinance` |

## 배포 전 체크리스트

- [ ] `ma_signal_v2.py`를 며칠간 기존 `ma_signal.py`와 나란히 돌려 신호가
      튀는 종목이 없는지 비교
- [ ] pykrx 버전에서 `거래량` 컬럼명이 그대로 유효한지 1회 실행으로 확인
- [ ] 텔레그램 메시지 포맷(`● = 20/120/200일선 ON · 거래량≥평균1.5배 진입조건`)이
      기존 대비 어색하지 않은지 확인
- [ ] 문제 없으면 스케줄 워크플로의 실행 스크립트를 `ma_signal_v2.py`로 교체
      하고, 기존 `ma_signal.py`는 `ma_signal_v1_hysteresis.py` 등으로 이름을
      바꿔 참고용으로 보존 (crossover 버전 보존 관례와 동일)

## 알려진 한계 / 다음 검토 사항

- **표본 부족 종목**: SNDK는 상장 이력이 짧아 200일선 워밍업이 간신히 되는
  수준(10년 기준 전환 이벤트 8건). 신호 신뢰도가 낮으니 해석에 주의.
- **사이클 쏠림**: 성과가 좋은 종목 대부분이 AI반도체(NVDA, MU, AMD, SK하이닉스)
  또는 방산·에너지(LEU, LIG디펜스, 한화에어로) 두 사이클에 몰려 있어, 25종목의
  분산 효과가 통계상 보이는 것보다 작을 수 있음.
- **레버리지 ETF 검증 미완료**: 위성 자산으로 2x 상품(QLD 등)을 고려 중이라면,
  이 신호 로직을 레버리지 상품 자체 가격에 대해 별도로 백테스트 필요
  (변동성 감쇠 때문에 원지수 백테스트 결과를 그대로 적용할 수 없음).
- **거래량 배수 재검증 주기**: `VOLUME_MULT=1.5`는 현재 25종목·10년 데이터
  기준 최적값. 종목 구성이 크게 바뀌면(예: 위성 종목 교체) 재스윕 권장.

## 면책

이 저장소의 코드와 문서는 개인 백테스트/신호 참고용이며 투자 자문이 아닙니다.
과거 백테스트 성과가 미래 수익을 보장하지 않습니다.
