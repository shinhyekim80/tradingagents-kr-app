#!/usr/bin/env python3
"""픽셀 트레이딩 플로어 — TradingAgents 실시간 시각화 서버.

실행:
    pip install fastapi uvicorn
    python dashboard/server.py            # http://localhost:8000

브라우저에서 티커 입력 → ANALYZE → 에이전트들의 분석 과정이
픽셀 캐릭터 말풍선으로 실시간 스트리밍됩니다.
"""

from __future__ import annotations

import asyncio
import io
import os
import json
import logging
import queue
import threading
from datetime import date
from pathlib import Path

import pandas as pd
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pixel-floor")

STATIC_DIR = Path(__file__).parent / "static"

# 그래프 노드 이름 → 프론트엔드 캐릭터 ID
NODE_TO_CHAR = {
    "Market Analyst": "taro",
    "Sentiment Analyst": "vibe",
    "News Analyst": "nova",
    "Fundamentals Analyst": "diana",
    "Bull Researcher": "bull",
    "Bear Researcher": "bear",
    "Research Manager": "owl",
    "Trader": "ace",
    "Aggressive Analyst": "risky",
    "Neutral Analyst": "neutral",
    "Conservative Analyst": "safe",
    "Portfolio Manager": "boss",
}
TOOL_NODE_TO_CHAR = {
    "tools_market": "taro",
    "tools_social": "vibe",
    "tools_news": "nova",
    "tools_fundamentals": "diana",
}
# 노드 완료 시 보고서로 뽑아낼 상태 키
REPORT_KEYS = {
    "market_report": "taro",
    "sentiment_report": "vibe",
    "news_report": "nova",
    "fundamentals_report": "diana",
    "trader_investment_plan": "ace",
    "final_trade_decision": "boss",
}


def resolve_provider() -> tuple[str, str, str]:
    """활성 LLM 제공자와 (provider, deep_model, quick_model)를 결정.

    우선순위:
      1) 환경변수 LLM_PROVIDER=openai|anthropic 가 있으면 그것을 따름
      2) 없으면 .env에 있는 키로 자동 감지 (OpenAI 우선, 없으면 Anthropic)
    키가 하나도 없으면 openai로 두고 실행 시 ⚙ 시스템 점검에서 안내한다.
    """
    forced = os.getenv("LLM_PROVIDER", "").strip().lower()
    if forced in ("openai", "anthropic"):
        provider = forced
    elif os.getenv("OPENAI_API_KEY"):
        provider = "openai"
    elif os.getenv("ANTHROPIC_API_KEY"):
        provider = "anthropic"
    else:
        provider = "openai"
    if provider == "anthropic":
        return "anthropic", "claude-sonnet-5", "claude-haiku-4-5-20251001"
    return "openai", "gpt-5.4-mini", "gpt-5.4-nano"


def build_config() -> dict:
    config = DEFAULT_CONFIG.copy()
    provider, deep_model, quick_model = resolve_provider()
    config["llm_provider"] = provider
    config["deep_think_llm"] = deep_model
    config["quick_think_llm"] = quick_model
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1
    # 초보자 눈높이 한국어 출력 (이 문자열이 프롬프트에 삽입됨)
    config["output_language"] = (
        "한국어 — 금융 초보자도 이해할 수 있게 쉽게 쓸 것. "
        "전문 용어(PER, RSI 등)는 처음 나올 때 괄호로 한 줄 설명을 붙이고, "
        "핵심 결론을 먼저 말한 뒤 근거를 3~5개로 정리할 것"
    )
    return config


def normalize_ticker(ticker: str) -> str:
    """한글 종목명·6자리 코드를 파일시스템에 안전한 표준 티커로 변환.

    예) '삼성전자' → '005930.KS', '005930' → '005930.KS'.
    미국 종목(AAPL 등)이나 이미 표준형(005930.KS)이면 그대로 반환한다.
    원본 TradingAgents는 티커를 리포트/메모리 폴더 경로로 쓰기 때문에
    한글이 그대로 들어가면 경로 생성에서 거부된다 — 여기서 미리 코드로 바꾼다.
    """
    if not ticker:
        return ticker
    try:
        from tradingagents.dataflows import krx

        resolved = krx.resolve_kr_symbol(ticker)
        if resolved:
            return resolved[1]  # (code, '005930.KS') 중 야후형 표준 티커
    except Exception:
        pass
    return ticker


def _chunk_text(content) -> str:
    """LangChain 메시지 청크의 content를 문자열로 정규화."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # Anthropic 스타일 블록
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


class RunManager:
    """한 번에 하나의 분석 실행을 관리하고 이벤트를 WS로 중계."""

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.events: queue.Queue = queue.Queue()
        self.running = False
        self._graph: TradingAgentsGraph | None = None
        # BOSS 상담용 컨텍스트
        self.last_reports: dict[str, str] = {}
        self.last_ticker: str | None = None
        self.last_date: str | None = None
        self.chat_history: list[dict] = []
        self.chat_busy = False

    # ---------------- WebSocket ----------------
    async def register(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def unregister(self, ws: WebSocket):
        self.clients.discard(ws)

    async def pump(self):
        """스레드가 쌓은 이벤트를 모든 클라이언트로 전송."""
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            dead = []
            for ws in self.clients:
                try:
                    await ws.send_text(json.dumps(event, ensure_ascii=False))
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.unregister(ws)

    def emit(self, **event):
        self.events.put(event)

    # ---------------- BOSS 상담 채팅 ----------------
    def chat(self, text: str):
        if not text:
            return
        if self.chat_busy:
            self.emit(type="chat_error", message="BOSS가 아직 답변 중입니다.")
            return
        self.chat_busy = True
        threading.Thread(target=self._chat_run, args=(text,), daemon=True).start()

    def _chat_run(self, text: str):
        try:
            cfg = build_config()
            portfolio = load_portfolio()
            pf = portfolio.get(self.last_ticker or "", {})

            reports = ""
            labels = {
                "market_report": "기술적 분석", "sentiment_report": "센티먼트",
                "news_report": "뉴스 분석", "fundamentals_report": "기본적 분석",
                "judge_decision": "리서치 매니저 결론",
                "trader_investment_plan": "트레이더 계획",
                "final_trade_decision": "최종 매매 판단",
            }
            for key, label in labels.items():
                if key in self.last_reports:
                    reports += f"\n\n## {label}\n{self.last_reports[key][:2500]}"

            sys_prompt = f"""당신은 'BOSS' — 픽셀 트레이딩 플로어의 펀드매니저 펭귄입니다.
방금 팀이 {self.last_ticker or '(아직 분석 전)'} ({self.last_date or ''}) 분석을 마쳤고, 아래가 팀 리포트 전문입니다.
사용자는 금융 초보 개인투자자입니다. 상담 규칙:
- 존댓말, 따뜻하고 침착한 베테랑 톤. 답변은 400자 이내로 간결하게.
- 전문 용어는 처음 나올 때 괄호로 한 줄 설명.
- 반드시 팀 리포트의 근거를 인용해 답하고, 리포트에 없는 수치는 지어내지 말 것.
- 사용자의 보유 현황(수량·평단가)을 반영해 시나리오별(추가매수/보유/부분매도) 장단점을 제시.
- 확정적인 매매 지시("무조건 사세요")는 금지. 항상 "최종 판단은 본인 몫"임을 자연스럽게 상기.
- 분석 리포트가 없으면 먼저 분석(ANALYZE)을 돌려달라고 안내.

[사용자 보유 현황] {json.dumps(pf, ensure_ascii=False) if pf else '미입력 — 수량과 평단가를 물어보고 화면의 보유현황 칸에 저장하도록 안내'}
[팀 리포트]{reports if reports else ' (아직 분석 실행 전)'}"""

            history = self.chat_history[-12:] + [{"role": "user", "content": text}]
            full = ""
            if cfg["llm_provider"] == "anthropic":
                from anthropic import Anthropic

                client = Anthropic()
                with client.messages.stream(
                    model=cfg["deep_think_llm"],
                    max_tokens=1024,
                    system=sys_prompt,
                    messages=history,
                ) as stream:
                    for delta in stream.text_stream:
                        if delta:
                            full += delta
                            self.emit(type="chat_token", char="boss", text=delta)
            else:
                from openai import OpenAI

                client = OpenAI()
                msgs = [{"role": "system", "content": sys_prompt}] + history
                stream = client.chat.completions.create(
                    model=cfg["deep_think_llm"], messages=msgs, stream=True)
                for ch in stream:
                    delta = ch.choices[0].delta.content or ""
                    if delta:
                        full += delta
                        self.emit(type="chat_token", char="boss", text=delta)
            self.chat_history += [{"role": "user", "content": text},
                                  {"role": "assistant", "content": full}]
            self.emit(type="chat_done", text=full)
        except Exception as e:
            logger.exception("chat failed")
            self.emit(type="chat_error", message=f"상담 실패: {e}")
        finally:
            self.chat_busy = False

    # ---------------- 분석 실행 ----------------
    def start(self, ticker: str, trade_date: str):
        if self.running:
            self.emit(type="error", message="이미 분석이 실행 중입니다.")
            return
        self.running = True
        threading.Thread(
            target=self._run, args=(ticker, trade_date), daemon=True
        ).start()

    def _run(self, ticker: str, trade_date: str):
        try:
            ticker = normalize_ticker(ticker)  # '삼성전자' → '005930.KS'
            self.emit(type="run_start", ticker=ticker, date=trade_date)
            self.last_reports = {}
            self.last_ticker = ticker
            self.last_date = trade_date
            self.chat_history = []
            if self._graph is None:
                self.emit(type="status", node="system",
                          text="에이전트 팀 출근 중... (그래프 초기화)")
                self._graph = TradingAgentsGraph(debug=False, config=build_config())
            ta = self._graph

            past_context = ""
            try:
                past_context = ta.memory_log.get_past_context(ticker)
            except Exception:
                pass
            instrument_context = ""
            try:
                instrument_context = ta.resolve_instrument_context(ticker, "stock")
            except Exception:
                pass

            state = ta.propagator.create_initial_state(
                ticker, trade_date,
                past_context=past_context,
                instrument_context=instrument_context,
            )
            args = ta.propagator.get_graph_args()
            args["stream_mode"] = ["messages", "updates"]

            final_state = dict(state)
            active_node = None

            try:
                stream = ta.graph.stream(state, **args)
                for item in stream:
                    active_node = self._handle_item(item, final_state, active_node)
            except TypeError:
                # 구버전 langgraph: 복합 stream_mode 미지원 → updates만
                args["stream_mode"] = "updates"
                for item in ta.graph.stream(state, **args):
                    self._handle_updates(item, final_state)

            decision = str(final_state.get("final_trade_decision", "")).strip()
            self.emit(type="decision", text=decision or "(결정 없음)")

            try:
                path = ta.save_reports(final_state, ticker)
                self.emit(type="status", node="system", text=f"리포트 저장: {path}")
            except Exception as e:
                logger.warning("report save failed: %s", e)

            self.emit(type="run_done")
        except Exception as e:
            logger.exception("run failed")
            self.emit(type="error", message=f"분석 실패: {e}")
        finally:
            self.running = False

    def _handle_item(self, item, final_state: dict, active_node):
        """stream_mode=["messages","updates"] 항목 처리."""
        if isinstance(item, tuple) and len(item) == 2 and item[0] in (
            "messages", "updates"
        ):
            mode, payload = item
        else:  # 단일 모드로 떨어진 경우
            mode, payload = "updates", item

        if mode == "messages":
            try:
                chunk, meta = payload
            except Exception:
                return active_node
            node = (meta or {}).get("langgraph_node", "")
            char = NODE_TO_CHAR.get(node) or TOOL_NODE_TO_CHAR.get(node)
            if not char:
                return active_node
            if node != active_node:
                active_node = node
                self.emit(type="node_active", char=char, node=node)
            # 도구 호출 중이면 상태만 표시
            tool_calls = getattr(chunk, "tool_call_chunks", None) or getattr(
                chunk, "tool_calls", None
            )
            text = _chunk_text(getattr(chunk, "content", ""))
            if text:
                self.emit(type="token", char=char, text=text)
            elif tool_calls:
                names = {t.get("name") for t in tool_calls
                         if isinstance(t, dict) and t.get("name")}
                if names:
                    self.emit(type="tool", char=char, tools=sorted(names))
            return active_node

        # mode == "updates"
        self._handle_updates(payload, final_state)
        return active_node

    def _handle_updates(self, payload, final_state: dict):
        if not isinstance(payload, dict):
            return
        for node, delta in payload.items():
            if not isinstance(delta, dict):
                continue
            # 상태 병합 (마지막에 리포트 저장용)
            for k, v in delta.items():
                if k == "messages":
                    continue
                final_state[k] = v

            char = NODE_TO_CHAR.get(node)
            # 완료된 보고서 전송
            for key, owner in REPORT_KEYS.items():
                if key in delta and str(delta[key]).strip():
                    self.last_reports[key] = str(delta[key])[:6000]
                    self.emit(type="report", char=owner, key=key,
                              text=str(delta[key])[:6000])
            # 토론 상태
            debate = delta.get("investment_debate_state")
            if isinstance(debate, dict):
                cur = str(debate.get("current_response", "")).strip()
                judge = str(debate.get("judge_decision", "")).strip()
                if cur and char in ("bull", "bear"):
                    self.emit(type="report", char=char,
                              key="debate", text=cur[:4000])
                if judge and node == "Research Manager":
                    self.last_reports["judge_decision"] = judge[:4000]
                    self.emit(type="report", char="owl",
                              key="judge_decision", text=judge[:4000])
            risk = delta.get("risk_debate_state")
            if isinstance(risk, dict) and char in ("risky", "neutral", "safe"):
                cur = str(
                    risk.get(f"current_{'aggressive' if char=='risky' else 'conservative' if char=='safe' else 'neutral'}_response", "")
                ).strip()
                if cur:
                    self.emit(type="report", char=char, key="risk", text=cur[:3000])
            if char:
                self.emit(type="node_done", char=char, node=node)


PORTFOLIO_PATH = Path(os.path.expanduser("~")) / ".tradingagents" / "portfolio.json"


def load_portfolio() -> dict:
    try:
        return json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_portfolio(data: dict):
    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


manager = RunManager()
app = FastAPI(title="Pixel Trading Floor")

# 커스텀 캐릭터 이미지 폴더: dashboard/static/sprites/{캐릭터ID}.png 를 넣으면
# 해당 캐릭터가 그 이미지로 표시됨 (taro, nova, diana, vibe, bull, bear,
# owl, ace, risky, neutral, safe, boss). 없으면 기본 픽셀 캐릭터 사용.
SPRITES_DIR = STATIC_DIR / "sprites"
SPRITES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/sprites", StaticFiles(directory=SPRITES_DIR), name="sprites")


@app.on_event("startup")
async def _startup():
    asyncio.create_task(manager.pump())


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    """데이터 소스·API 키 실동작 점검. 브라우저에서 /health 또는 UI의 점검 버튼."""
    import os

    checks = []

    def add(name, fn):
        try:
            detail = str(fn())[:200]
            checks.append({"name": name, "ok": True, "detail": detail})
        except Exception as e:
            checks.append({"name": name, "ok": False, "detail": str(e)[:300]})

    def _llm_key():
        provider, deep_model, quick_model = resolve_provider()
        if provider == "anthropic":
            key = os.getenv("ANTHROPIC_API_KEY", "")
            if not key:
                raise RuntimeError(".env에 ANTHROPIC_API_KEY 없음")
            from anthropic import Anthropic
            Anthropic().models.list()  # 키 유효성 실검증
            return f"Claude 키 유효 (...{key[-4:]}) · 모델 {deep_model}/{quick_model}"
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError(".env에 OPENAI_API_KEY 또는 ANTHROPIC_API_KEY 없음")
        from openai import OpenAI
        OpenAI().models.list()  # 키 유효성 실검증 (무료 호출)
        return f"OpenAI 키 유효 (sk-...{key[-4:]}) · 모델 {deep_model}/{quick_model}"

    def _krx_listing():
        from tradingagents.dataflows import krx as krx_mod
        listing = krx_mod._load_listing()
        if listing is None:
            raise RuntimeError("KRX 상장목록 로드 실패 (FinanceDataReader)")
        return f"{len(listing)}개 종목 로드됨"

    def _kr_price():
        import FinanceDataReader as fdr
        df = fdr.DataReader("005930", (date.today() - pd.Timedelta(days=14)).isoformat())
        if df is None or df.empty:
            raise RuntimeError("삼성전자 시세 0건")
        return f"삼성전자 최근 {len(df)}일, 종가 {df['Close'].iloc[-1]:,.0f}원"

    def _kr_fund():
        from tradingagents.dataflows import krx as krx_mod
        out = krx_mod.get_kr_fundamentals("005930")
        return out.replace("\n", " | ")[:150]

    def _kr_news():
        from tradingagents.dataflows import krx as krx_mod
        end = date.today().isoformat()
        start = (date.today() - pd.Timedelta(days=7)).isoformat()
        out = krx_mod.get_kr_news("005930", start, end)
        return out.splitlines()[0][:150]

    def _us_price():
        from tradingagents.dataflows.interface import route_to_vendor
        end = date.today().isoformat()
        start = (date.today() - pd.Timedelta(days=14)).isoformat()
        out = route_to_vendor("get_stock_data", "AAPL", start, end)
        if "NO_DATA" in out:
            raise RuntimeError(out[:200])
        return out.splitlines()[0][:150]

    add("LLM API 키 (OpenAI/Claude 자동감지)", _llm_key)
    add("한국 종목 목록 (검색용)", _krx_listing)
    add("한국 시세 (FinanceDataReader)", _kr_price)
    add("한국 펀더멘털 (네이버)", _kr_fund)
    add("한국 뉴스 (네이버)", _kr_news)
    add("미국 시세 (yfinance)", _us_price)
    return JSONResponse({"ok": all(c["ok"] for c in checks), "checks": checks})


# 미국 인기 종목 (이름 검색용 내장 목록)
US_TICKERS = [
    ("애플", "Apple", "AAPL"), ("마이크로소프트", "Microsoft", "MSFT"),
    ("엔비디아", "Nvidia", "NVDA"), ("알파벳/구글", "Alphabet Google", "GOOGL"),
    ("아마존", "Amazon", "AMZN"), ("메타", "Meta Facebook", "META"),
    ("테슬라", "Tesla", "TSLA"), ("브로드컴", "Broadcom", "AVGO"),
    ("TSMC", "Taiwan Semiconductor", "TSM"), ("일라이릴리", "Eli Lilly", "LLY"),
    ("JP모건", "JPMorgan", "JPM"), ("비자", "Visa", "V"),
    ("유나이티드헬스", "UnitedHealth", "UNH"), ("엑슨모빌", "Exxon Mobil", "XOM"),
    ("월마트", "Walmart", "WMT"), ("마스터카드", "Mastercard", "MA"),
    ("코스트코", "Costco", "COST"), ("홈디포", "Home Depot", "HD"),
    ("P&G", "Procter Gamble", "PG"), ("존슨앤존슨", "Johnson Johnson", "JNJ"),
    ("넷플릭스", "Netflix", "NFLX"), ("AMD", "Advanced Micro Devices", "AMD"),
    ("세일즈포스", "Salesforce", "CRM"), ("어도비", "Adobe", "ADBE"),
    ("코카콜라", "Coca Cola", "KO"), ("펩시", "PepsiCo", "PEP"),
    ("맥도날드", "McDonalds", "MCD"), ("나이키", "Nike", "NKE"),
    ("스타벅스", "Starbucks", "SBUX"), ("인텔", "Intel", "INTC"),
    ("퀄컴", "Qualcomm", "QCOM"), ("팔란티어", "Palantir", "PLTR"),
    ("샌디스크", "Sandisk", "SNDK"), ("마이크론", "Micron", "MU"),
    ("버크셔해서웨이", "Berkshire Hathaway", "BRK-B"),
    ("S&P500 ETF", "SPDR S&P 500", "SPY"), ("나스닥100 ETF", "Invesco QQQ", "QQQ"),
]


@app.get("/search")
async def search(q: str):
    """종목 검색: 한국(KRX 전체 상장사) + 미국 인기 종목."""
    q = q.strip()
    if not q:
        return JSONResponse([])
    results = []
    ql = q.lower()

    # 미국 종목 (이름/티커 부분일치)
    for kr, en, sym in US_TICKERS:
        if ql in kr.lower() or ql in en.lower() or ql in sym.lower():
            results.append({"symbol": sym, "name": f"{kr} ({en})", "market": "US"})

    # 한국 종목 (KRX 상장 목록) — pandas 버전과 무관하게 수동 정규화 매칭
    try:
        from tradingagents.dataflows import krx as krx_mod

        listing = krx_mod._load_listing()
        if listing is None:
            results.append({"symbol": "", "market": "-",
                            "name": "⚠ 한국 종목 목록 로드 실패 — 서버 터미널 로그를 확인하세요"})
        else:
            qn = q.replace(" ", "").casefold()
            count = 0
            for _, row in listing.iterrows():
                code = str(row["Code"]).zfill(6)
                name = str(row["Name"])
                if qn in name.replace(" ", "").casefold() or code.startswith(q):
                    market = str(row["Market"]).upper()
                    suffix = "KQ" if "KOSDAQ" in market else "KS"
                    results.append({
                        "symbol": f"{code}.{suffix}",
                        "name": name,
                        "market": "KOSDAQ" if suffix == "KQ" else "KOSPI",
                    })
                    count += 1
                    if count >= 10:
                        break
    except Exception as e:
        logger.warning("KRX search unavailable: %s", e)
        results.append({"symbol": "", "market": "-",
                        "name": f"⚠ 한국 종목 검색 오류: {e}"})

    # 이름이 검색어로 시작하는 항목 우선
    results.sort(key=lambda r: (not r["name"].lower().startswith(ql),
                                not r["symbol"].lower().startswith(ql)))
    return JSONResponse(results[:12])


@app.get("/price")
async def price(ticker: str, trade_date: str | None = None):
    """상단 차트용 시세 + MA20/MA60. krx→yfinance 라우팅 사용."""
    from tradingagents.dataflows.interface import route_to_vendor

    end = trade_date or date.today().isoformat()
    start = (pd.Timestamp(end) - pd.Timedelta(days=270)).strftime("%Y-%m-%d")
    try:
        raw = route_to_vendor("get_stock_data", ticker, start, end)
        # 헤더 주석/설명 줄을 건너뛰고 실제 CSV 헤더부터 파싱
        lines = [l for l in str(raw).splitlines() if l.strip()]
        try:
            idx = next(i for i, l in enumerate(lines)
                       if l.split(",")[0].strip().lower() in ("date", "datetime", "index"))
        except StopIteration:
            raise ValueError(f"가격 데이터 형식 인식 실패: {str(raw)[:150]}")
        df = pd.read_csv(io.StringIO("\n".join(lines[idx:])))
        date_col = df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d")
        close = pd.to_numeric(df["Close"], errors="coerce")

        def clean(series):  # NaN은 JSON 불가 → None으로
            return [None if pd.isna(v) else round(float(v), 2) for v in series]

        out = {
            "dates": df[date_col].tolist(),
            "close": clean(close),
            "ma20": clean(close.rolling(20).mean()),
            "ma60": clean(close.rolling(60).mean()),
        }
        return JSONResponse(out)
    except Exception as e:
        logger.exception("price fetch failed for %s", ticker)
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/portfolio")
async def get_portfolio(ticker: str | None = None):
    data = load_portfolio()
    return JSONResponse(data.get(ticker, {}) if ticker else data)


@app.post("/portfolio")
async def set_portfolio(item: dict):
    ticker = str(item.get("ticker", "")).strip()
    if not ticker:
        return JSONResponse({"error": "ticker 필요"}, status_code=400)
    data = load_portfolio()
    shares = float(item.get("shares") or 0)
    if shares <= 0:
        data.pop(ticker, None)
    else:
        data[ticker] = {
            "shares": shares,
            "avg_price": float(item.get("avg_price") or 0),
            "memo": str(item.get("memo", ""))[:200],
            "updated": date.today().isoformat(),
        }
    save_portfolio(data)
    return JSONResponse({"ok": True, "saved": data.get(ticker)})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.register(ws)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "analyze":
                ticker = str(msg.get("ticker", "")).strip()
                trade_date = str(msg.get("date") or date.today().isoformat())
                if ticker:
                    manager.start(ticker, trade_date)
            elif msg.get("type") == "chat":
                manager.chat(str(msg.get("text", "")).strip()[:2000])
    except WebSocketDisconnect:
        manager.unregister(ws)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
