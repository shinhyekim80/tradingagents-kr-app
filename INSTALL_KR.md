# TradingAgents 한국주식 + 저비용 커스터마이징 가이드

TradingAgents(v0.2.4 기준)를 개인 투자 분석용으로 커스터마이징한 패키지입니다.

**추가된 것**
- `krx` 데이터 벤더: 한국 종목(KOSPI/KOSDAQ)을 API 키 없이 분석
  - 시세/기술지표: FinanceDataReader (네이버 차트 데이터)
  - 펀더멘털(PER/PBR/시총 등): 네이버 금융 API
  - 뉴스: 네이버 금융 한국어 기사
  - 재무제표: 야후 파이낸스(.KS/.KQ)로 자동 위임
- 티커 자동 라우팅: `005930` → krx, `AAPL` → yfinance (설정 변경 불필요)
- 한글 종목명 입력 지원 (`삼성전자`)
- 코스피/코스닥 벤치마크 자동 인식 (성과 회고용)
- 저비용 LLM 조합 + 한국어 리포트 출력 스크립트

---

## 1. 설치

```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
conda create -n tradingagents python=3.13 && conda activate tradingagents
pip install .
pip install finance-datareader        # 한국 시세용 추가 의존성
```

## 2. 커스텀 파일 적용

방법 A — 패치 적용 (권장, v0.2.4/2026-04 기준 main):

```bash
git apply kr_adapter.patch
cp krx.py tradingagents/dataflows/krx.py
```

방법 B — 파일 덮어쓰기 (패치 충돌 시):

```bash
cp krx.py            tradingagents/dataflows/krx.py
cp interface.py      tradingagents/dataflows/interface.py
cp default_config.py tradingagents/default_config.py
```

이후 `pip install .` 재실행 (또는 `pip install -e .`로 개발 모드 설치).

## 3. API 키 설정

```bash
cp .env.example .env
# .env에 아래만 채우면 됩니다
OPENAI_API_KEY=sk-...
```

한국 데이터는 키가 필요 없습니다. FRED(미국 매크로) 지표를 쓰려면 `FRED_API_KEY`도 추가 (무료 발급: fred.stlouisfed.org).

## 4. 실행

```bash
python run_analysis.py 005930.KS AAPL            # 삼성전자 + 애플
python run_analysis.py 247540.KQ --date 2026-07-06
python run_analysis.py 삼성전자 --debug
tradingagents                                     # 기존 대화형 CLI도 그대로 동작
```

- 한국 종목은 `005930.KS` / `247540.KQ` 형식 권장 (성과 회고 시 코스피/코스닥 대비 알파 자동 계산). 6자리 코드·한글명도 동작.
- 리포트 저장 위치: `~/.tradingagents/logs/`
- 과거 판단 기록·회고: `~/.tradingagents/memory/trading_memory.md`

## 5. 비용

1회 분석당 LLM 호출이 수십 회 발생합니다. `run_analysis.py` 기본 설정(gpt-5.4-mini + nano, 토론 1라운드) 기준 회당 대략 $0.05~0.15 수준. 더 줄이려면:

- `llm_provider="deepseek"` + `deepseek-v4-flash` (최저가)
- `config["news_article_limit"]`을 20 → 10으로 축소
- 애널리스트 4명 중 필요한 것만 선택 (CLI에서 선택 가능)

품질을 올리고 싶을 때만 `deep_think_llm`을 상위 모델로 교체하세요. quick 모델이 호출 횟수의 대부분을 차지하므로 quick은 nano급 유지가 이득입니다.

## 6. 픽셀 트레이딩 플로어 (실시간 시각화 대시보드)

에이전트들의 분석 과정을 픽셀아트 사무실에서 실시간으로 보여주는 웹 화면입니다.
귀여운 동물 캐릭터가 역할별로 배치되어 있고, 분석 중 발언이 말풍선으로 스트리밍됩니다.

| 캐릭터 | 역할 |
|---|---|
| TARO (고양이) | 기술적 분석 |
| NOVA (병아리) | 뉴스 분석 |
| DIANA (토끼) | 기본적 분석 |
| VIBE (선글라스 강아지) | 센티먼트 |
| BULL / BEAR | 매수·매도 논거 토론 |
| SAGE (부엉이) | 리서치 매니저 (토론 심판) |
| ACE (헤드셋 햄스터) | 수석 트레이더 |
| BLAZE·BAMBOO·SHELLY | 리스크팀 (공격·중립·보수) |
| BOSS (펭귄) | 펀드 매니저 (최종 승인) |

실행:

```bash
pip install fastapi uvicorn
cp -r dashboard <TradingAgents 레포 루트>/dashboard
cd <TradingAgents 레포 루트>
python dashboard/server.py        # → http://localhost:8000
```

브라우저에서 티커 입력(예: 005930.KS) → ▶ ANALYZE.
상단에 종가+MA20/MA60 차트, 각 캐릭터 말풍선에 실시간 분석,
우측 로그 패널에서 보고서 전문 확인, 완료 시 최종 판단 팝업이 뜹니다.
리포트는 기존과 동일하게 `~/.tradingagents/logs/`에 저장됩니다.

## 7. 한계 및 주의

- 네이버 금융 API는 비공식이라 응답 형식이 바뀌면 뉴스/펀더멘털이 yfinance로 폴백되거나 "데이터 없음" 처리됩니다 (값을 지어내지는 않음).
- 감성(Sentiment) 애널리스트의 Reddit/StockTwits 소스는 미국 중심이라 한국 종목엔 신호가 약합니다. 한국 종목은 뉴스 애널리스트 비중이 높습니다.
- 재무제표는 야후 데이터 기준이라 한국 기업은 분기 데이터가 빈약할 수 있습니다.
- 이 프레임워크는 연구용입니다. 출력은 투자 참고 자료일 뿐 투자 자문이 아니며, 실제 매매 판단과 책임은 본인에게 있습니다.

## 8. 파일 구성

| 파일 | 용도 | 복사 위치 |
|---|---|---|
| `krx.py` | 한국 데이터 벤더 (신규) | `tradingagents/dataflows/krx.py` |
| `interface.py` | krx 벤더 등록 (수정본) | `tradingagents/dataflows/interface.py` |
| `default_config.py` | 벤더 체인·벤치마크 설정 (수정본) | `tradingagents/default_config.py` |
| `kr_adapter.patch` | 위 두 수정을 git patch로 | repo 루트에서 `git apply` |
| `run_analysis.py` | 저비용 일괄 분석 스크립트 | repo 루트 |
| `dashboard/server.py` | 픽셀 플로어 백엔드 (FastAPI+WS) | repo 루트/`dashboard/` |
| `dashboard/static/index.html` | 픽셀 플로어 화면 | repo 루트/`dashboard/static/` |
