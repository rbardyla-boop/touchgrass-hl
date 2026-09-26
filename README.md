# TOUCHGRASS-HL v0.1.1

Deterministic Hyperliquid research and paper-trading bot. It studies perp traders and markets continuously and tests whether convergence among independently successful wallets contains a copyable signal.

Grok is not part of the running bot. The default process is ordinary Python. Jev is optional. Mainnet trading is disabled. There is no mainnet private-key path.

## What it does

```
Hyperliquid public data
  -> collector
  -> SQLite
  -> wallet history and scores
  -> behavioral groups (not an ownership claim)
  -> convergence detection
  -> hard risk vetoes
  -> optional Jev review
  -> two paper lanes
  -> optional Hyperliquid testnet orders
  -> audit log
```

Perps only: the primary perp DEX and HIP-3 builder-deployed perp DEXes. Coins are discovered. None are hard-coded.

## Engineering decisions

- Public market data uses mainnet `https://api.hyperliquid.xyz` and `wss://api.hyperliquid.xyz/ws`. REST calls go through `hyperliquid.api.API`. Testnet orders go through `hyperliquid.exchange.Exchange` and only after the URL is exactly `https://api.hyperliquid-testnet.xyz`.
- Trade identity is `(block time, coin, tid)`. Hyperliquid documents `tid` as a 50-bit hash of the two order ids, not a globally unique trade id. The stored key is `tid:<time>:<coin>:<tid>`. Reconnects cannot duplicate that key.
- `users[0]` is the buyer and `users[1]` is the seller, per the websocket trade schema. A buy is not treated as "became bullish" unless the prior signed position is known.
- Asset ids follow the docs: primary dex index in `meta`; builder dex `100000 + perp_dex_index * 10000 + index_in_meta`.
- `allDexsAssetCtxs` plus a capped trade subscription set (default 80, ranked by day notional) stays inside the 1000-subscription websocket limit. L2/BBO is fetched when a candidate is evaluated, not for every market all the time. REST `metaAndAssetCtxs` remains the market-registry source of truth.
- Info calls share one priority rate limiter (default 1000 weight/minute, under the 1200 IP limit). Live refresh outranks candidate context, which outranks verified-wallet maintenance, which outranks history hydration. `userFillsByTime` is weight 20 plus one per 20 fills returned. The client reserves the worst-case page (120) before the request and refunds the unused weight after the response so a full page cannot exceed the local budget. HTTP 429 backs off, requeues the wallet, and does not crash collection.
- Wallet history uses `userFillsByTime` (at most 2000 per page, about 10000 most recent). That is stored as partial. `lifetime_complete` is never set true. Wallet fill identity is `address + time + coin + tid` because `tid` alone is not unique.
- Realized episode PnL is the sum of Hyperliquid `closedPnl` on every attached fill, including opens and adds. An opening fill's `closedPnl` is the fee. A closing fill's `closedPnl` is the fee plus realized price PnL. Flips split that fill so the two legs sum to it. If reconstructed PnL does not match the sum of `closedPnl`, the wallet is data-quality degraded and cannot be performance-verified.
- Scores are percentile ranks with configurable weights. The profitability input is size-normalized: `(last_pnl - first_pnl) / mean(account_value)` when Hyperliquid account history is usable, otherwise `sum(closed_pnl) / sum(entry_notional)`. Raw USD PnL is stored and displayed but is not the rank input. Percentage drawdown uses that return curve, or account value when the first point is positive. It never divides by a zero peak. USD peak-to-trough drawdown is a separate statistic.
- `PERFORMANCE_VERIFIED` means the sample rules pass (30 closed trades, 14 active days, positive PnL, profit factor > 1, no single trade above 50% of positive PnL unless overridden, and PnL reconciles). `COPY_VERIFIED` also requires at least `MIN_COPYABILITY_OBSERVATIONS` (default 10) known observations. Unknown copyability is not treated as verified. Only `COPY_VERIFIED` wallets enter a convergence cluster. Performance wallets stay monitored.
- Behavioral groups require high Jaccard overlap of `(market, direction)` and repeated near-simultaneous entries. Pair records store Jaccard, eligible events, near-simultaneous count and fraction, leader/follower counts, and size ratio. A group formed only through union-find is labeled `transitive_union`; A linked to B and B linked to C is not stored as a direct A–C relationship. Groups are not a common-ownership claim. Convergence counts groups.
- Jev question types use the official lowercase API (`score`, `noul`, `choice`) on `POST /v1/systemone`. Regime is recorded and is not a veto. Cluster quality, behavior fit, contradiction, and information sufficiency are. Jev cannot place an order.
- If Jev is disabled or down, collection and the RULES_ONLY paper lane continue. RULES_PLUS_JEV records `JEV_UNAVAILABLE` and does not trade.
- Paper fills walk the book. Fees use the tier-0 base rate times the official HIP-3 formula: `scaleIfHip3 = scale+1` when `deployerFeeScale < 1`, else `scale*2`, then growth mode (`0.1` or `1`) and the aligned-collateral factor. A scale-1 non-growth tier-0 taker is 0.09%, not a flat 0.18%. Core markets stay at tier-0 until a real account exists. The full inputs and rate are frozen on each simulated fill. Collateral other than configured USDC (token 0) is `VETO_UNSUPPORTED_COLLATERAL`. New entries use mark-to-market equity; a stale open mark blocks the entry and does not force-close the position. Funding while held uses the entry funding snapshot and is labeled an estimate.
- `testnet-smoke` is the v0.1 path that places an order. It requires `EXECUTION_MODE=testnet` and a testnet agent key, and it refuses any URL other than the official testnet. `TESTNET_AUTO_TRADE` defaults to false. The research service does not mirror mainnet paper signals onto testnet; a mainnet convergence event is not a testnet order. The executor can place, query, cancel, and reduce-only close, and it has no withdrawal or transfer methods.
- Raw trades and unattached context snapshots are pruned. Candidates, risk decisions, Jev reviews, lane decisions, paper fills, testnet orders, and audit events are kept.

## 1. Install Python dependencies

Requires Python 3.12+. From this directory:

```bash
chmod +x install.sh
./install.sh
```

Or without the script:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e ".[dev]"
mkdir -p data logs
```

## 2. Create `.env`

```bash
cp .env.example .env
```

Paper mode needs no private key and no Jev key. Leave secrets empty.

## 3. Initialize the database

```bash
.venv/bin/touchgrass-hl init-db
```

Creates `data/touchgrass.db` in WAL mode and the two paper accounts (RULES_ONLY and RULES_PLUS_JEV), each starting at $75.

## 4. Doctor

```bash
.venv/bin/touchgrass-hl doctor
```

Checks configuration, SQLite WAL, mainnet info, and the mainnet websocket. Optional Jev and testnet checks print `NOT RUN` when credentials are absent. That is not a pass and not a fail. The command fails only when a required check fails.

## 5. Discover markets

```bash
.venv/bin/touchgrass-hl discover-markets
```

Prints core and HIP-3 perp markets currently returned by mainnet.

## 6. Sixty-second public-data smoke test

```bash
.venv/bin/touchgrass-hl run --duration 60
```

Connects to the public mainnet websocket, stores trades, and records wallet addresses. No AI key is used.

## 7. Run continuously in paper mode

```bash
.venv/bin/touchgrass-hl run
```

Stop with SIGTERM or Ctrl-C. The process flushes and exits. WebSocket disconnects retry with backoff and do not crash the process.

## 8. Wallet rankings

```bash
.venv/bin/touchgrass-hl wallets
```

## 9. One wallet

```bash
.venv/bin/touchgrass-hl wallet 0x0000000000000000000000000000000000000000
```

## 10. Candidates

```bash
.venv/bin/touchgrass-hl candidates
```

## 11. Paper results

```bash
.venv/bin/touchgrass-hl paper-report
```

Shows starting equity, current equity, realized and unrealized PnL, fees, wins/losses, drawdown, and both lanes.

## 12. Enable Jev

In `.env`:

```bash
JEV_ENABLED=true
JEV_API_KEY=your-typesafe-key
JEV_MODEL=jev-latest
```

Jev is called only after a candidate passes the hard market vetoes. One request asks `cluster_quality`, `behavior_fit`, `regime`, `contradiction`, and `information_sufficient`. Re-run a stored candidate without deleting older reviews:

```bash
.venv/bin/touchgrass-hl jev-review CANDIDATE_ID
```

If the key is missing the command prints `NOT RUN` and exits 3.

## 13. Configure testnet

In `.env`:

```bash
EXECUTION_MODE=testnet
TESTNET_API_URL=https://api.hyperliquid-testnet.xyz
TESTNET_ACCOUNT_ADDRESS=0xYourMaster
TESTNET_AGENT_PRIVATE_KEY=0xYourAgentKey
```

Then:

```bash
.venv/bin/touchgrass-hl testnet-smoke
```

This places one small GTC buy far below the mid, queries it, and cancels it. If it fills, the executor reduce-only closes. No withdrawals or transfers exist. If credentials or `EXECUTION_MODE=testnet` are missing, the command prints `NOT RUN` and exits 3.

`testnet-smoke` is the only command that places an order. The running service stays on public data and paper fills. A file named `data/KILL_SWITCH` or `TRADING_KILL_SWITCH=true` blocks new risk decisions and testnet actions.

## 14. systemd

```bash
sudo useradd --system --create-home --home-dir /opt/touchgrass-hl --shell /usr/sbin/nologin touchgrass || true
sudo mkdir -p /opt/touchgrass-hl
sudo rsync -a --exclude .venv ./ /opt/touchgrass-hl/
sudo chown -R touchgrass:touchgrass /opt/touchgrass-hl
sudo -u touchgrass bash -lc 'cd /opt/touchgrass-hl && ./install.sh'
sudo cp /opt/touchgrass-hl/deploy/touchgrass-hl.service /etc/systemd/system/touchgrass-hl.service
sudo systemctl daemon-reload
sudo systemctl enable --now touchgrass-hl
```

The unit starts after networking, restarts on failure after 5 seconds, and stops on SIGTERM.

Stop, start, restart:

```bash
sudo systemctl stop touchgrass-hl
sudo systemctl start touchgrass-hl
sudo systemctl restart touchgrass-hl
sudo systemctl status touchgrass-hl
```

## 15. Logs and database

Default locations, relative to the working directory (`/opt/touchgrass-hl` under systemd):

- Database: `data/touchgrass.db` (plus `data/touchgrass.db-wal`)
- Logs: `logs/touchgrass-hl.log` (rotating, JSON, secrets redacted)

Journald also receives stdout:

```bash
sudo journalctl -u touchgrass-hl -f
```

Override with `DATABASE_URL` and `LOG_DIR`.

## Tests

```bash
.venv/bin/pytest
```

Unit tests do not use the network. The live smoke test is `touchgrass-hl doctor` and `touchgrass-hl run --duration 60`.

## Safety

- `.env` is gitignored.
- Logs redact fields whose names contain key, secret, private, token, or authorization.
- V0.1 cannot place mainnet orders.
- Hard vetoes (`VETO_*`) are not bypassable by Jev.
- Stale data, a dead websocket, a missing book, an audit-write failure, or an uncertain executor means no new trade. Collection keeps trying to recover.
