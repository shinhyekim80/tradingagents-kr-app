#!/usr/bin/env python3
"""저비용 설정으로 한국/미국 종목을 일괄 분석하는 스크립트.

사용법:
    python run_analysis.py 005930.KS AAPL          # 오늘 날짜 기준
    python run_analysis.py 247540.KQ --date 2026-07-06
    python run_analysis.py 삼성전자                  # 한글 종목명도 가능

한국 종목은 벤치마크(코스피/코스닥) 자동 인식을 위해
"005930.KS" / "247540.KQ" 형식을 권장합니다. 6자리 코드만 넣어도 동작합니다.
"""

import argparse
from datetime import date

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


def resolve_provider() -> tuple[str, str, str]:
    """활성 LLM 제공자와 (provider, deep_model, quick_model)를 결정.

    LLM_PROVIDER=openai|anthropic 로 강제 지정 가능. 없으면 .env에 있는 키로
    자동 감지(OpenAI 우선, 없으면 Anthropic). 둘 다 없으면 openai로 둔다.
    """
    import os
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

    # --- 저비용 LLM 조합 (.env 키로 자동 감지: OpenAI / Anthropic) ---
    provider, deep_model, quick_model = resolve_provider()
    config["llm_provider"] = provider
    config["deep_think_llm"] = deep_model    # 토론·최종판단용
    config["quick_think_llm"] = quick_model  # 데이터 요약용 (호출 횟수 多)

    # 토론 라운드 1회 = 비용 최소화. 품질 우선이면 2로.
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1

    # 리포트/최종 판단을 한국어로 출력 (내부 토론은 영어 유지)
    config["output_language"] = "한국어"

    # 데이터 벤더: krx(한국) -> yfinance(그 외) 체인은 default_config에 이미 설정됨
    # config["data_vendors"]["core_stock_apis"] = "krx,yfinance"

    # 매크로 뉴스 검색어에 한국 항목 추가
    config["global_news_queries"] = [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings economic outlook",
        "Bank of Korea interest rate policy 한국은행 기준금리",
        "KOSPI foreign investor flows 코스피 외국인 수급",
        "Korea semiconductor exports 반도체 수출",
        "geopolitical risk trade tariffs",
    ]
    return config


def normalize_ticker(ticker: str) -> str:
    """한글 종목명·6자리 코드를 파일시스템에 안전한 표준 티커로 변환.

    예) '삼성전자' → '005930.KS', '005930' → '005930.KS'. 미국 종목은 그대로.
    원본 TradingAgents가 티커를 리포트/메모리 폴더 경로로 쓰기 때문에
    한글이 그대로 들어가면 경로 생성에서 거부된다 — 미리 코드로 바꾼다.
    """
    if not ticker:
        return ticker
    try:
        from tradingagents.dataflows import krx

        resolved = krx.resolve_kr_symbol(ticker)
        if resolved:
            return resolved[1]
    except Exception:
        pass
    return ticker


def main() -> None:
    parser = argparse.ArgumentParser(description="TradingAgents 일괄 분석")
    parser.add_argument("tickers", nargs="+", help="종목: 005930.KS, AAPL, 삼성전자 ...")
    parser.add_argument("--date", default=date.today().isoformat(), help="분석 기준일 YYYY-MM-DD")
    parser.add_argument("--debug", action="store_true", help="에이전트 대화 전체 출력")
    args = parser.parse_args()

    config = build_config()
    ta = TradingAgentsGraph(debug=args.debug, config=config)

    results = {}
    for raw_ticker in args.tickers:
        ticker = normalize_ticker(raw_ticker)  # '삼성전자' → '005930.KS'
        label = ticker if ticker == raw_ticker else f"{ticker} ({raw_ticker})"
        print(f"\n{'=' * 60}\n분석 시작: {label} ({args.date})\n{'=' * 60}")
        try:
            _, decision = ta.propagate(ticker, args.date)
            results[ticker] = decision
            print(f"\n[{ticker}] 최종 판단:\n{decision}")
        except Exception as e:
            results[ticker] = f"실패: {e}"
            print(f"[{ticker}] 분석 실패: {e}")

    print(f"\n{'=' * 60}\n요약\n{'=' * 60}")
    for ticker, decision in results.items():
        first_line = str(decision).strip().splitlines()[0] if decision else "?"
        print(f"  {ticker}: {first_line}")
    print(f"\n상세 리포트: ~/.tradingagents/logs/ 에 저장됨")


if __name__ == "__main__":
    main()
