"""
Solana DEX Arb Bot — Simulation Backtest + Parameter Optimization
=================================================================
Uses historical SOL/USDC price data to MODEL spread-opportunity frequency.

NOTE: True arb backtesting requires historical per-DEX pool data (unavailable
publicly). This simulation models spread size as a function of price volatility,
calibrated to known Solana DEX behaviour (0.1-2% daily yield on small capital).
Use results for parameter sensitivity analysis, NOT exact P&L projection.

Usage:
    python backtest.py                # 2yr simulation + sweep
    python backtest.py --period 1y   # 1yr quick run
"""

from __future__ import annotations

import sys
import argparse
import warnings
import numpy as np
import pandas as pd
import yfinance as yf
from itertools import product
from datetime import datetime

warnings.filterwarnings("ignore", category=FutureWarning)

# ── Simulation constants (calibrated to Solana DEX reality) ──────────────────
INITIAL_CAPITAL    = 1_000.0   # USDC
SCANS_PER_HOUR     = 1800      # 1 scan per 2s
DEX_FEE_PCT        = 0.005     # two swaps × 0.25%
NETWORK_FEE_USDC   = 0.002     # Solana priority fee (~$0.002)
VOL_SPREAD_MULT    = 10.0      # how much ATR vol amplifies spread
BASE_SPREAD_MU     = 0.0020    # lognormal mean (0.2% base spread)
BASE_SPREAD_SIGMA  = 0.0035    # lognormal std


# ── Data ──────────────────────────────────────────────────────────────────────
def download_data(period: str = "2y") -> pd.DataFrame:
    print(f"Downloading SOL-USD {period} hourly data...")
    raw = yf.download("SOL-USD", period=period, interval="1h",
                      progress=False, auto_adjust=True)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    print(f"  {len(df)} bars  {df.index[0].date()} -> {df.index[-1].date()}")
    return df


def compute_vol(df: pd.DataFrame, window: int = 24) -> pd.Series:
    atr    = (df["High"] - df["Low"]).rolling(window).mean()
    vol_pct = atr / df["Close"]
    return vol_pct.fillna(vol_pct.median())


# ── Core simulation ───────────────────────────────────────────────────────────
def simulate(
    df: pd.DataFrame,
    vol_pct: pd.Series,
    min_profit_pct: float,
    slippage_bps: int,
    trade_size_usdc: float,
    seed: int = 42,
) -> tuple[pd.DataFrame, list[float]]:
    rng           = np.random.default_rng(seed)
    slippage_pct  = slippage_bps / 10_000
    threshold     = min_profit_pct / 100 + slippage_pct + DEX_FEE_PCT

    equity        = INITIAL_CAPITAL
    equity_curve  = [equity]
    records: list[dict] = []

    # Pre-sample all random draws for speed
    n_total = len(df) * SCANS_PER_HOUR
    vol_arr = vol_pct.values
    # Lognormal parameters for spread distribution
    ln_mu  = np.log(BASE_SPREAD_MU) - 0.5 * np.log(1 + (BASE_SPREAD_SIGMA/BASE_SPREAD_MU)**2)
    ln_sig = np.sqrt(np.log(1 + (BASE_SPREAD_SIGMA/BASE_SPREAD_MU)**2))

    for bar_i, (ts, row) in enumerate(df.iterrows()):
        vp            = float(vol_arr[bar_i])
        vol_multiplier = 1.0 + VOL_SPREAD_MULT * vp

        # Vectorised spread draw for all scans this hour
        spreads = rng.lognormal(
            mean  = ln_mu + np.log(vol_multiplier),
            sigma = ln_sig,
            size  = SCANS_PER_HOUR,
        )

        # Opportunities: spread exceeds threshold
        triggered = spreads[spreads >= threshold]
        for raw_spread in triggered:
            # Actual fill is 60-100% of modeled spread (partial fills / timing)
            actual_net = (raw_spread - DEX_FEE_PCT - slippage_pct) * rng.uniform(0.60, 1.00)
            profit     = trade_size_usdc * actual_net - NETWORK_FEE_USDC

            equity = max(equity + profit, 0.01)
            records.append({"ts": ts, "profit": profit, "equity": equity})

        equity_curve.append(equity)

    trades_df = pd.DataFrame(records)
    return trades_df, equity_curve


# ── Metrics ───────────────────────────────────────────────────────────────────
def metrics(
    trades_df: pd.DataFrame,
    equity_curve: list[float],
    period_years: float,
) -> dict:
    if trades_df.empty or period_years <= 0:
        return {k: 0.0 for k in ("n", "n_yr", "wr", "cagr", "max_dd", "sharpe", "pf", "total_ret")}

    eq  = np.array(equity_curve)
    fin = eq[-1]
    tot = (fin - INITIAL_CAPITAL) / INITIAL_CAPITAL
    cagr = ((1 + tot) ** (1 / period_years) - 1) * 100

    peak  = np.maximum.accumulate(eq)
    dd    = (eq - peak) / peak
    max_dd = float(dd.min() * 100)

    wins   = trades_df["profit"][trades_df["profit"] > 0]
    losses = trades_df["profit"][trades_df["profit"] <= 0]
    pf     = (wins.sum() / abs(losses.sum())) if losses.sum() != 0 else 9.99
    wr     = len(wins) / len(trades_df) * 100

    # Daily returns for Sharpe
    daily = trades_df.groupby(trades_df["ts"].dt.date)["profit"].sum() / INITIAL_CAPITAL
    sharpe = (daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0

    return {
        "n":        len(trades_df),
        "n_yr":     len(trades_df) / period_years,
        "wr":       round(wr,     2),
        "cagr":     round(cagr,   2),
        "max_dd":   round(max_dd, 2),
        "sharpe":   round(sharpe, 3),
        "pf":       round(min(pf, 9.99), 2),
        "total_ret": round(tot * 100, 2),
    }


# ── Composite score ───────────────────────────────────────────────────────────
def composite_scores(rows: list[dict]) -> list[float]:
    df = pd.DataFrame(rows)

    def norm(s: pd.Series) -> pd.Series:
        mn, mx = s.min(), s.max()
        return (s - mn) / (mx - mn) if mx > mn else pd.Series([0.5] * len(s))

    scores = (
        0.30 * norm(df["cagr"])            +
        0.25 * norm(df["sharpe"])          +
        0.20 * norm(-df["max_dd"])         +
        0.10 * norm(df["pf"].clip(upper=9))+
        0.10 * norm(df["wr"])              +
        0.05 * norm(df["n"])
    ) * 100
    return scores.round(1).tolist()


# ── Monte Carlo ───────────────────────────────────────────────────────────────
def monte_carlo(pnl_list: list[float], n_sims: int = 5_000) -> dict:
    pnl = np.array(pnl_list)
    finals, worst_dds = [], []
    for _ in range(n_sims):
        shuffled = np.random.choice(pnl, size=len(pnl), replace=True)
        eq       = INITIAL_CAPITAL + np.cumsum(shuffled)
        eq       = np.insert(eq, 0, INITIAL_CAPITAL)
        pk       = np.maximum.accumulate(eq)
        finals.append(eq[-1])
        worst_dds.append(float(((eq - pk) / pk).min()))

    finals    = np.array(finals)
    worst_dds = np.array(worst_dds)
    return {
        "median_ret_%":  round((np.median(finals) - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1),
        "p5_ret_%":      round((np.percentile(finals, 5) - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1),
        "p95_ret_%":     round((np.percentile(finals, 95) - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1),
        "worst_dd_%":    round(worst_dds.min() * 100, 1),
        "median_dd_%":   round(np.median(worst_dds) * 100, 1),
        "prob_ruin_%":   round((finals < INITIAL_CAPITAL * 0.5).mean() * 100, 1),
    }


# ── Year-by-year ──────────────────────────────────────────────────────────────
def yearly_table(trades_df: pd.DataFrame) -> pd.DataFrame:
    if trades_df.empty:
        return pd.DataFrame()
    trades_df = trades_df.copy()
    trades_df["year"] = trades_df["ts"].dt.year
    return (
        trades_df.groupby("year")
        .agg(
            n=("profit", "count"),
            profit_usdc=("profit", "sum"),
            wr_pct=("profit", lambda x: round((x > 0).mean() * 100, 1)),
        )
        .assign(ret_pct=lambda d: (d["profit_usdc"] / INITIAL_CAPITAL * 100).round(1))
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="2y",
                        choices=["6mo", "1y", "2y"], help="Data window (default 2y)")
    args = parser.parse_args()

    df          = download_data(args.period)
    period_yrs  = (df.index[-1] - df.index[0]).days / 365.25
    vol_pct     = compute_vol(df)

    # ── Parameter grid ────────────────────────────────────────────────────────
    MIN_PROFIT_PCTS  = [0.3, 0.5, 0.7, 1.0, 1.5]
    SLIPPAGE_BPS_VALS= [25, 50, 75, 100]
    TRADE_SIZES      = [100, 500, 1_000]   # USDC

    combos = list(product(MIN_PROFIT_PCTS, SLIPPAGE_BPS_VALS, TRADE_SIZES))
    print(f"\nOptimization sweep: {len(combos)} combos ...", flush=True)

    results: list[dict] = []
    for i, (mpp, sbps, ts) in enumerate(combos):
        trades_df, eq_curve = simulate(df, vol_pct, mpp, sbps, ts)
        m = metrics(trades_df, eq_curve, period_yrs)
        m.update({"min_profit_pct": mpp, "slippage_bps": sbps, "trade_size": ts})
        results.append(m)
        if (i + 1) % 15 == 0 or i + 1 == len(combos):
            print(f"  {i+1}/{len(combos)}", flush=True)

    scores = composite_scores(results)
    for r, s in zip(results, scores):
        r["score"] = s

    res_df = pd.DataFrame(results).sort_values("score", ascending=False)

    # ── Top 10 ────────────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print("TOP 10 CONFIGURATIONS")
    print(f"{'='*72}")
    cols = ["min_profit_pct", "slippage_bps", "trade_size",
            "cagr", "max_dd", "sharpe", "wr", "n", "score"]
    print(res_df[cols].head(10).to_string(index=False))

    # ── RRR-analogue table (min_profit_pct sweep at best slippage/size) ───────
    best_sbps = int(res_df.iloc[0]["slippage_bps"])
    best_ts   = int(res_df.iloc[0]["trade_size"])
    sub       = res_df[(res_df["slippage_bps"] == best_sbps) &
                       (res_df["trade_size"]   == best_ts)].sort_values("min_profit_pct")
    print(f"\nMin-profit sweep (slippage={best_sbps}bps, trade=${best_ts} USDC):")
    print(sub[["min_profit_pct", "cagr", "wr", "pf", "max_dd", "sharpe", "score"]].to_string(index=False))

    # ── Best config full analysis ─────────────────────────────────────────────
    best     = res_df.iloc[0]
    b_trades, b_eq = simulate(df, vol_pct,
                               float(best["min_profit_pct"]),
                               int(best["slippage_bps"]),
                               float(best["trade_size"]))
    b_metrics = metrics(b_trades, b_eq, period_yrs)

    print(f"\n{'='*72}")
    print(f"BEST CONFIG: min_profit={best.min_profit_pct}%  "
          f"slippage={best.slippage_bps}bps  trade=${best.trade_size} USDC")
    print(f"{'='*72}")
    print(f"  Total trades    : {b_metrics['n']}")
    print(f"  Trades / year   : {b_metrics['n_yr']:.0f}")
    print(f"  Win rate        : {b_metrics['wr']}%")
    print(f"  CAGR            : {b_metrics['cagr']}%")
    print(f"  Max drawdown    : {b_metrics['max_dd']}%")
    print(f"  Sharpe          : {b_metrics['sharpe']}")
    print(f"  Profit factor   : {b_metrics['pf']}")
    print(f"  Total return    : {b_metrics['total_ret']}%")
    print(f"  Final equity    : ${INITIAL_CAPITAL * (1 + b_metrics['total_ret']/100):.2f}")

    # Year-by-year
    yt = yearly_table(b_trades)
    if not yt.empty:
        print(f"\nYEAR-BY-YEAR:")
        print(yt.to_string())

    # Monte Carlo
    if not b_trades.empty:
        mc = monte_carlo(b_trades["profit"].tolist())
        print(f"\nMONTE CARLO (5 000 sims on best config):")
        for k, v in mc.items():
            print(f"  {k:20s}: {v}")

    # ── Three setups ──────────────────────────────────────────────────────────
    viable = res_df[res_df["cagr"] > 0]
    if viable.empty:
        aggressive = balanced = conservative = res_df.iloc[0]
    else:
        aggressive   = viable[viable["max_dd"] > -40].nlargest(1, "cagr").iloc[0] \
                       if not viable[viable["max_dd"] > -40].empty else viable.iloc[0]
        balanced     = viable.iloc[0]
        conservative = viable.nsmallest(1, "max_dd").iloc[0]

    print(f"\n{'='*72}")
    print("THREE SETUPS")
    print(f"{'='*72}")
    for label, cfg in [("AGGRESSIVE", aggressive),
                       ("BALANCED",   balanced),
                       ("CONSERVATIVE", conservative)]:
        print(f"\n  {label}")
        print(f"    min_profit_pct : {cfg.min_profit_pct}%")
        print(f"    slippage_bps   : {int(cfg.slippage_bps)}")
        print(f"    trade_size     : ${int(cfg.trade_size)} USDC")
        print(f"    CAGR           : {cfg.cagr:.1f}%")
        print(f"    Max DD         : {cfg.max_dd:.1f}%")
        print(f"    Sharpe         : {cfg.sharpe:.2f}")
        print(f"    Score          : {cfg.score:.1f}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    sh   = float(balanced["sharpe"])
    cagr = float(balanced["cagr"])
    dd   = float(balanced["max_dd"])

    print(f"\n{'='*72}")
    if sh >= 2.0 and cagr >= 15 and dd > -25:
        verdict      = "PASS (simulation)"
        suitability  = "Paper test first — validate live spread frequency before scaling"
    elif sh >= 1.0 and cagr >= 5:
        verdict     = "MARGINAL"
        suitability = "Paper test with $100-500 capital — real DEX spreads may be thinner"
    else:
        verdict     = "FAIL"
        suitability = "Model shows insufficient edge — widen min_profit or reduce slippage"

    print(f"VERDICT     : {verdict}")
    print(f"Suitability : {suitability}")
    print(f"\nCAVEAT: Simulation uses synthetic spread model (no historical DEX pool data).")
    print(f"        Run DRY_RUN=true on live bot for 48h to validate real opportunity frequency.")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
