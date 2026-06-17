# 🤖 Solana DEX Arbitrage Bot

Scans Jupiter, Raydium, and Orca for price gaps across token pairs and executes two-hop circular arbitrage trades via the Jupiter Aggregator v6 API.

---

## ⚙️ How It Works

```
USDC ──[Leg 1: Jupiter]──► SOL ──[Leg 2: Jupiter]──► USDC
         (cheapest route)              (best exit)
         
If output USDC > input USDC (after fees) → execute trade
```

Jupiter aggregates liquidity from **Raydium, Orca, Meteora, Phoenix**, and 20+ other Solana DEXes — so every leg gets the best possible price across all pools automatically.

---

## 🚀 Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your settings
```

### 3. Run in DRY RUN mode first (no real trades)
```bash
python arb_bot.py
```

### 4. Switch to live trading
```bash
# In .env, set:
DRY_RUN=false
SOLANA_PRIVATE_KEY=your_base58_key_here
```

---

## 📋 Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `SOLANA_PRIVATE_KEY` | — | Wallet private key (base58) |
| `RPC_URL` | mainnet-beta | Solana RPC endpoint |
| `MIN_PROFIT_PCT` | `0.5` | Min net profit % to trade |
| `SLIPPAGE_BPS` | `50` | Slippage tolerance (0.5%) |
| `SCAN_INTERVAL_SEC` | `2.0` | Seconds between scans |
| `DRY_RUN` | `true` | Simulate only (no real txs) |

---

## 🔀 Arbitrage Routes Scanned

| Route | Description |
|---|---|
| USDC → SOL → USDC | SOL price gap |
| USDC → RAY → USDC | Raydium token gap |
| USDC → BONK → USDC | BONK meme gap |
| USDC → JUP → USDC | Jupiter token gap |
| SOL → USDC → SOL | Reverse SOL gap |
| SOL → RAY → SOL | SOL/RAY spread |

Add more routes in `ARB_ROUTES` in `arb_bot.py` — just add token mint addresses to `TOKENS` dict.

---

## 💰 Fee Structure

Each arbitrage round trip costs approximately:

| Fee | Amount |
|---|---|
| DEX swap fees (×2) | ~0.25% × 2 = 0.5% |
| Solana network fee | ~$0.001 per tx |
| **Minimum needed spread** | **>0.5% to be profitable** |

---

## 🖥️ VPS Deployment (Recommended)

Run 24/7 on a cheap VPS:

```bash
# Install screen for persistent sessions
sudo apt install screen -y

# Start bot in background session
screen -S arbbot
python arb_bot.py

# Detach: Ctrl+A then D
# Reattach: screen -r arbbot
```

Or use **systemd** for auto-restart:

```ini
# /etc/systemd/system/arbbot.service
[Unit]
Description=Solana Arb Bot
After=network.target

[Service]
WorkingDirectory=/path/to/solana_arb_bot
ExecStart=/usr/bin/python3 arb_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable arbbot
sudo systemctl start arbbot
sudo systemctl status arbbot
```

---

## ⚡ Performance Tips

1. **Use a paid RPC** — Free mainnet-beta is heavily rate-limited. Helius (~$50/mo) gives 10x faster quotes.
2. **Lower scan interval** — Set `SCAN_INTERVAL_SEC=0.5` with a good RPC.
3. **Add more routes** — Wider net = more opportunities.
4. **Tune slippage** — Lower slippage = better fills but more failed txs. Start at 50 BPS.
5. **Priority fees** — Bot already uses `prioritizationFeeLamports: auto` for faster landing.

---

## ⚠️ Risks & Realities

- **Arb is competitive** — MEV bots on Solana are extremely fast. True atomic arb (sandwich-proof) requires Jito bundles.
- **Slippage** — Large trades move the market. Start small.
- **Failed transactions** — Solana congestion can cause Leg 1 to succeed and Leg 2 to fail. The bot logs this as a warning.
- **The $1→$400k story** — Almost certainly involved much larger capital, Jito MEV bundles, and months of compounding. Realistic daily arb yields: 0.1–2% on small capital.

---

## 🔧 Extending the Bot

### Add Jito MEV bundles (atomic execution)
```python
# Replace execute_swap() with Jito bundle submission
# See: https://jito-labs.gitbook.io/mev/searcher-resources/bundles
```

### Add more token pairs
```python
TOKENS["PYTH"] = "HZ1JovNiVvGqHRpkAzbkFKgg2vH1qAGBQ3m2JG4VXXXXX"
ARB_ROUTES.append(("USDC", "PYTH", "USDC"))
```

### Deploy to Railway
```bash
# Add Procfile:
echo "worker: python arb_bot.py" > Procfile
# Push to GitHub → connect Railway → add env vars in Railway dashboard
```

---

## 📁 File Structure

```
solana_arb_bot/
├── arb_bot.py          # Main bot
├── requirements.txt    # Dependencies
├── .env.example        # Config template
├── .env                # Your config (git-ignored)
└── arb_bot.log         # Trade log (auto-created)
```

---

## 🛡️ Security

- **Never commit `.env`** — add it to `.gitignore`
- Use a **dedicated hot wallet** with only the capital you're willing to risk
- Start with dry run, then small amounts ($10–$50) before scaling
