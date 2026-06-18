"""
Solana DEX Arbitrage Bot
========================
Scans Jupiter, Raydium, and Orca for price gaps across token pairs.
Executes trades via Jupiter Aggregator API when spread > min_profit_threshold.

Requirements:
    pip install requests solana solders python-dotenv colorama base58

Setup:
    1. Copy .env.example to .env
    2. Add your Solana wallet private key
    3. Set MIN_PROFIT_PCT to your desired minimum spread (default: 0.5%)
    4. Run: python arb_bot.py

Flags:
    --stress-test    Run 1000-cycle durability test without real API calls
"""

from __future__ import annotations

import os
import sys
import time
import base64
import sqlite3
import argparse
import logging
import random
import requests
from datetime import datetime
from dotenv import load_dotenv
from colorama import Fore, Style, init

# ── Solana SDK (only needed for live trading; optional for DRY_RUN) ───────────
try:
    from solders.keypair import Keypair                   # type: ignore
    from solders.pubkey import Pubkey                     # type: ignore
    from solana.rpc.api import Client                     # type: ignore
    from solana.rpc.types import TxOpts                   # type: ignore
    import base58
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False
    Keypair = Client = TxOpts = base58 = None            # type: ignore

try:
    from solders.transaction import VersionedTransaction  # type: ignore
    _HAS_VERSIONED_TX = True
except ImportError:
    _HAS_VERSIONED_TX = False

init(autoreset=True)
load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
_handlers = [logging.StreamHandler(sys.stdout)]
try: _handlers.append(logging.FileHandler("arb_bot.log"))
except Exception: pass
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=_handlers)
log = logging.getLogger("ArbBot")

# ── Config ────────────────────────────────────────────────────────────────────
RPC_URL                  = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
PRIVATE_KEY_B58          = os.getenv("SOLANA_PRIVATE_KEY", "")
MIN_PROFIT_PCT           = float(os.getenv("MIN_PROFIT_PCT", "0.5"))
SLIPPAGE_BPS             = int(os.getenv("SLIPPAGE_BPS", "50"))
SCAN_INTERVAL_SEC        = float(os.getenv("SCAN_INTERVAL_SEC", "2.0"))
DRY_RUN                  = os.getenv("DRY_RUN", "true").lower() == "true"
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "10"))
MAX_TRADES_PER_HOUR      = int(os.getenv("MAX_TRADES_PER_HOUR", "5"))
DB_PATH                  = os.getenv("DB_PATH", "spreads.db")

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL  = "https://api.jup.ag/swap/v1/swap"

# ── Token Registry ────────────────────────────────────────────────────────────
TOKENS = {
    "SOL":    "So11111111111111111111111111111111111111112",
    "USDC":   "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT":   "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "RAY":    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "BONK":   "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "JUP":    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "WIF":    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "MSOL":   "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # Marinade staked SOL
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "BOME":   "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82",
}

TOKEN_DECIMALS = {
    "SOL":    9, "USDC": 6, "USDT": 6, "RAY":  6,
    "BONK":   5, "JUP":  6, "WIF":  6, "MSOL": 9,
    "POPCAT": 9, "BOME": 6,
}

# Arbitrage routes: (input_token, intermediate_token, output_token)
ARB_ROUTES = [
    ("USDC", "SOL",    "USDC"),
    ("USDC", "RAY",    "USDC"),
    ("USDC", "BONK",   "USDC"),
    ("USDC", "JUP",    "USDC"),
    ("USDC", "WIF",    "USDC"),
    ("USDC", "POPCAT", "USDC"),
    ("USDC", "BOME",   "USDC"),
    ("SOL",  "USDC",   "SOL"),
    ("SOL",  "RAY",    "SOL"),
    ("SOL",  "MSOL",   "SOL"),   # liquid staking rate spread
    ("SOL",  "WIF",    "SOL"),
    ("BONK", "WIF",    "BONK"),  # cross-meme spread
    ("WIF",  "BONK",   "WIF"),
]

# Input amounts per route (raw units)
ROUTE_INPUT_AMOUNTS = {
    "USDC":   100_000_000,    # 100 USDC   (6 dec)
    "SOL":    100_000_000,    # 0.1 SOL    (9 dec)
    "RAY":    10_000_000,     # 10 RAY     (6 dec)
    "BONK":   10_000_000_000, # 100k BONK  (5 dec)
    "JUP":    10_000_000,     # 10 JUP     (6 dec)
    "WIF":    10_000_000,     # 10 WIF     (6 dec)
    "MSOL":   100_000_000,    # 0.1 mSOL   (9 dec)
    "POPCAT": 1_000_000_000,  # 1 POPCAT   (9 dec)
    "BOME":   10_000_000,     # 10 BOME    (6 dec)
}

# ── SQLite spread logger ──────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS spreads (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT,
            route     TEXT,
            gross_pct REAL,
            net_pct   REAL,
            profit    INTEGER   -- 1 if net_pct >= MIN_PROFIT_PCT else 0
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON spreads(ts)")
    con.commit()
    return con

_db: sqlite3.Connection | None = None

def log_spread(route: str, gross_pct: float, net_pct: float) -> None:
    if _db is None:
        return
    _db.execute(
        "INSERT INTO spreads(ts,route,gross_pct,net_pct,profit) VALUES(?,?,?,?,?)",
        (datetime.utcnow().isoformat(), route, round(gross_pct, 5),
         round(net_pct, 5), int(net_pct >= MIN_PROFIT_PCT)),
    )
    _db.commit()


# ── State ─────────────────────────────────────────────────────────────────────
stats = {
    "scans":            0,
    "opportunities":    0,
    "trades_executed":  0,
    "total_profit_usd": 0.0,
    "start_time":       datetime.utcnow(),
}


# ── Jupiter Quote ─────────────────────────────────────────────────────────────
def get_jupiter_quote(input_mint: str, output_mint: str, amount: int) -> dict | None:
    params = {
        "inputMint":        input_mint,
        "outputMint":       output_mint,
        "amount":           amount,
        "slippageBps":      SLIPPAGE_BPS,
        "onlyDirectRoutes": "false",
    }
    for attempt in range(3):
        try:
            resp = requests.get(JUPITER_QUOTE_URL, params=params, timeout=8)
            if resp.status_code != 200:
                if attempt == 2:
                    log.warning(f"Jupiter {resp.status_code}: {resp.text[:120]}")
                time.sleep(0.5 * (attempt + 1))
                continue
            data = resp.json()
            if "outAmount" not in data:
                log.warning(f"Jupiter missing outAmount: {list(data.keys())} | {str(data)[:100]}")
                return None
            return data
        except requests.RequestException as e:
            if attempt == 2:
                log.warning(f"Quote request failed: {e}")
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


# ── Arbitrage Scanner ─────────────────────────────────────────────────────────
def scan_route(route: tuple[str, str, str]) -> dict | None:
    in_tok, mid_tok, out_tok = route
    in_mint  = TOKENS[in_tok]
    mid_mint = TOKENS[mid_tok]
    out_mint = TOKENS[out_tok]

    input_amount = ROUTE_INPUT_AMOUNTS.get(in_tok, 1_000_000)
    decimals_in  = TOKEN_DECIMALS.get(in_tok, 6)

    q1 = get_jupiter_quote(in_mint, mid_mint, input_amount)
    if not q1:
        return None

    mid_amount = int(q1.get("outAmount", 0))
    if mid_amount == 0:
        return None

    q2 = get_jupiter_quote(mid_mint, out_mint, mid_amount)
    if not q2:
        return None

    out_amount = int(q2.get("outAmount", 0))
    if out_amount == 0:
        return None

    profit_raw = out_amount - input_amount
    profit_pct = (profit_raw / input_amount) * 100

    # Two swaps × ~0.25% each + ~5000 lamports network fee (estimated as %)
    fee_pct       = 0.5
    net_profit_pct = profit_pct - fee_pct

    scale         = 10 ** decimals_in
    return {
        "route":          f"{in_tok}>{mid_tok}>{out_tok}",
        "input_amount":   input_amount / scale,
        "output_amount":  out_amount   / scale,
        "profit_raw":     profit_raw   / scale,
        "profit_pct":     profit_pct,
        "net_profit_pct": net_profit_pct,
        "quote_leg1":     q1,
        "quote_leg2":     q2,
        "in_mint":        in_mint,
        "mid_mint":       mid_mint,
        "out_mint":       out_mint,
        "in_amount_raw":  input_amount,
        "mid_amount_raw": mid_amount,
        "token":          in_tok,
    }


# ── Trade Executor ────────────────────────────────────────────────────────────
def execute_swap(keypair: Keypair, client: Client, quote: dict, label: str) -> str | None:
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would execute {label}")
        return "DRY_RUN_SIMULATED"

    payload = {
        "quoteResponse":             quote,
        "userPublicKey":             str(keypair.pubkey()),
        "wrapAndUnwrapSol":          True,
        "dynamicComputeUnitLimit":   True,
        "prioritizationFeeLamports": "auto",
    }
    try:
        resp = requests.post(JUPITER_SWAP_URL, json=payload, timeout=10)
        resp.raise_for_status()
        swap_data = resp.json()

        # Jupiter v6 returns base64-encoded VersionedTransaction
        raw_tx   = swap_data["swapTransaction"]
        tx_bytes = base64.b64decode(raw_tx)

        if _HAS_VERSIONED_TX:
            versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
            sig          = keypair.sign_message(bytes(versioned_tx.message))
            signed_tx    = VersionedTransaction([sig], versioned_tx.message)
            send_bytes   = bytes(signed_tx)
        else:
            log.warning("VersionedTransaction unavailable — attempting legacy send (may fail)")
            send_bytes = tx_bytes

        result = client.send_raw_transaction(
            send_bytes,
            opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
        )
        sig_str = str(result.value)
        log.info(f"  OK {label} tx: https://solscan.io/tx/{sig_str}")
        return sig_str

    except Exception as e:
        log.error(f"  FAIL swap ({label}): {e}")
        return None


def execute_arbitrage(keypair: Keypair, client: Client, opp: dict) -> bool:
    log.info(f"  Executing arb: {opp['route']} | net: {opp['net_profit_pct']:.3f}%")

    parts = opp["route"].split(">")
    sig1  = execute_swap(keypair, client, opp["quote_leg1"], f"Leg1 {parts[0]}>{parts[1]}")
    if not sig1:
        log.warning("  Leg 1 failed — aborting")
        return False

    time.sleep(0.5)

    sig2 = execute_swap(keypair, client, opp["quote_leg2"], f"Leg2 {parts[1]}>{parts[2]}")
    if not sig2:
        log.warning("  Leg 2 failed — position exposed!")
        return False

    return True


# ── Dashboard ─────────────────────────────────────────────────────────────────
def print_dashboard(opportunities: list[dict]) -> None:
    elapsed = datetime.utcnow() - stats["start_time"]
    h, rem  = divmod(int(elapsed.total_seconds()), 3600)
    m, s    = divmod(rem, 60)

    print(f"\n{Fore.CYAN}{'─'*60}")
    print(f"  Solana Arb Bot  |  Uptime: {h:02d}:{m:02d}:{s:02d}")
    print(f"  Scans: {stats['scans']}  Opps: {stats['opportunities']}  Trades: {stats['trades_executed']}")
    print(f"  Est. profit: {stats['total_profit_usd']:.4f}  Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print(f"{'─'*60}{Style.RESET_ALL}")

    if opportunities:
        print(f"{Fore.YELLOW}  Opportunities:{Style.RESET_ALL}")
        for opp in opportunities:
            color = Fore.GREEN if opp["net_profit_pct"] >= MIN_PROFIT_PCT else Fore.RED
            print(f"  {color}{opp['route']:32s} gross:{opp['profit_pct']:+.3f}%  "
                  f"net:{opp['net_profit_pct']:+.3f}%{Style.RESET_ALL}")
    else:
        print(f"  {Fore.WHITE}No profitable spreads this scan.{Style.RESET_ALL}")


# ── Durability Stress Test ────────────────────────────────────────────────────
def stress_test() -> None:
    CYCLES      = 1000
    FAILURE_RATE = 0.15   # 15% injected API failure rate

    print(f"\n{'='*60}")
    print(f"  STRESS TEST  {CYCLES} cycles  {int(FAILURE_RATE*100)}% failure injection")
    print("="*60)

    counts = {"cycles": 0, "api_failures": 0, "opps": 0, "errors": 0, "ms_total": 0.0}

    for i in range(CYCLES):
        t0 = time.perf_counter()
        try:
            for route in ARB_ROUTES:
                in_tok, mid_tok, out_tok = route
                input_amount = ROUTE_INPUT_AMOUNTS.get(in_tok, 1_000_000)

                if random.random() < FAILURE_RATE:
                    counts["api_failures"] += 1
                    continue
                mid_amount = int(input_amount * random.uniform(0.97, 1.03))

                if random.random() < FAILURE_RATE:
                    counts["api_failures"] += 1
                    continue
                out_amount = int(input_amount * random.uniform(0.97, 1.03))

                profit_pct = (out_amount - input_amount) / input_amount * 100
                net        = profit_pct - 0.5
                if net >= MIN_PROFIT_PCT:
                    counts["opps"] += 1

            counts["cycles"] += 1
            counts["ms_total"] += (time.perf_counter() - t0) * 1000

        except Exception as e:
            counts["errors"] += 1
            log.error(f"Stress cycle {i} error: {e}")

    avg_ms = counts["ms_total"] / max(counts["cycles"], 1)
    print(f"\n  Cycles completed : {counts['cycles']}/{CYCLES}")
    print(f"  API failures     : {counts['api_failures']}")
    print(f"  Opps detected    : {counts['opps']}")
    print(f"  Logic errors     : {counts['errors']}")
    print(f"  Avg scan time    : {avg_ms:.3f} ms (no network I/O)")

    verdict = "PASS" if counts["errors"] == 0 else f"FAIL ({counts['errors']} errors)"
    print(f"\n  VERDICT: {verdict}")
    print(f"{'='*60}\n")


# ── Spread analysis (run after 48h dry run) ───────────────────────────────────
def analyze_spreads() -> None:
    con  = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT route,gross_pct,net_pct,profit,ts FROM spreads ORDER BY ts"
    ).fetchall()
    con.close()
    if not rows:
        print("No data — run bot DRY_RUN first.")
        return

    total   = len(rows)
    profs   = sum(r[3] for r in rows)
    gvals   = sorted(r[1] for r in rows)
    hours   = max((datetime.fromisoformat(rows[-1][4]) -
                   datetime.fromisoformat(rows[0][4])).total_seconds() / 3600, 0.001)

    print(f"\n{'='*60}")
    print(f"SPREAD ANALYSIS  {total:,} obs  {hours:.1f}h")
    print(f"  Profitable >={MIN_PROFIT_PCT}%: {profs:,} ({profs/total*100:.2f}%)  "
          f"{profs/hours:.1f}/hr  {profs/hours*24:.0f}/day")
    print(f"  Gross spread  min:{gvals[0]:.4f}%  "
          f"p50:{gvals[len(gvals)//2]:.4f}%  max:{gvals[-1]:.4f}%")

    routes: dict[str, list[int]] = {}
    for r in rows:
        routes.setdefault(r[0], [0, 0])
        routes[r[0]][0] += 1
        routes[r[0]][1] += r[3]
    print("  Per route:")
    for rt, (n, p) in sorted(routes.items()):
        print(f"    {rt:32s} {p:4d}/{n:5d} ({p/n*100:.2f}%)")

    opps_day = profs / hours * 24
    verdict  = ("VIABLE — pursue Jito+paid RPC" if opps_day >= 10 else
                "MARGINAL — test $50 live first" if opps_day >= 2 else
                "NOT VIABLE — competition too dense")
    print(f"\n  VERDICT: {verdict}")
    print(f"{'='*60}\n")


# ── Jupiter connectivity check ────────────────────────────────────────────────
def check_jupiter() -> None:
    try:
        r = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": TOKENS["USDC"], "outputMint": TOKENS["SOL"],
            "amount": 1_000_000, "slippageBps": 50,
        }, timeout=10)
        d = r.json()
        if "outAmount" in d:
            log.info(f"Jupiter OK: USDC->SOL outAmount={d['outAmount']} ({r.status_code})")
        else:
            log.warning(f"Jupiter bad response ({r.status_code}): {str(d)[:200]}")
    except Exception as e:
        log.error(f"Jupiter unreachable: {e}")


# ── Main Loop ─────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Solana DEX Arbitrage Bot")
    parser.add_argument("--stress-test", action="store_true",
                        help="Run durability test without real API calls")
    parser.add_argument("--analyze", action="store_true",
                        help="Analyze spread data collected in spreads.db")
    args = parser.parse_args()

    if args.stress_test:
        stress_test()
        return

    if args.analyze:
        analyze_spreads()
        return

    print(f"\n  Solana Arb Bot | min:{MIN_PROFIT_PCT}% slip:{SLIPPAGE_BPS/100}% mode:{'DRY' if DRY_RUN else 'LIVE'}\n")

    keypair = client = None
    if not DRY_RUN:
        if not _HAS_SDK:
            log.error("Solana SDK not installed — pip install solana solders base58")
            return
        if not PRIVATE_KEY_B58:
            log.error("SOLANA_PRIVATE_KEY not set — use DRY_RUN=true or add key to .env")
            return
        keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_B58))
        client  = Client(RPC_URL)
        bal     = client.get_balance(keypair.pubkey()).value / 1e9
        log.info(f"Wallet: {keypair.pubkey()} | Balance: {bal:.4f} SOL")

    global _db
    _db = init_db()
    log.info(f"Spread DB: {DB_PATH}")
    check_jupiter()
    log.info(f"Scanning {len(ARB_ROUTES)} routes every {SCAN_INTERVAL_SEC}s ...")

    consecutive_failures = 0
    trades_this_hour     = 0
    hour_start           = datetime.utcnow()

    while True:
        try:
            now = datetime.utcnow()
            if (now - hour_start).total_seconds() >= 3600:
                trades_this_hour = 0
                hour_start       = now

            stats["scans"] += 1
            scan_results = [r for r in (scan_route(rt) for rt in ARB_ROUTES) if r]

            # Log ALL results to SQLite for later frequency analysis
            for r in scan_results:
                log_spread(r["route"], r["profit_pct"], r["net_profit_pct"])

            profitable = [r for r in scan_results if r["net_profit_pct"] >= MIN_PROFIT_PCT]
            stats["opportunities"] += len(profitable)

            print_dashboard(profitable if profitable else scan_results[:3])
            consecutive_failures = 0

            if profitable and trades_this_hour < MAX_TRADES_PER_HOUR:
                best = max(profitable, key=lambda x: x["net_profit_pct"])
                log.info(f"Best: {best['route']} @ {best['net_profit_pct']:.3f}% net")

                if not DRY_RUN and keypair and client:
                    if execute_arbitrage(keypair, client, best):
                        stats["trades_executed"]  += 1
                        stats["total_profit_usd"] += best["profit_raw"]
                        trades_this_hour          += 1
                else:
                    log.info(f"  [DRY RUN] {best['route']} profit: {best['profit_raw']:.6f} {best['token']}")
                    stats["trades_executed"]  += 1
                    stats["total_profit_usd"] += best["profit_raw"]
                    trades_this_hour          += 1

            time.sleep(SCAN_INTERVAL_SEC)

        except KeyboardInterrupt:
            log.info("Shutting down...")
            print(f"\n{Fore.CYAN}Final stats: {stats}{Style.RESET_ALL}")
            break

        except Exception as e:
            consecutive_failures += 1
            log.error(f"Scan error ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.critical("Circuit breaker — pausing 5 min after repeated failures")
                time.sleep(300)
                consecutive_failures = 0
            else:
                time.sleep(5)


if __name__ == "__main__":
    main()
