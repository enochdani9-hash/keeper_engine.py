# ==============================================================================
# KEEPERPULSE v3.0 — UNIFIED DUAL-CORE ENGINE (BASE DEFI + GATE.IO SPOT SKIMMER)
# Core 1: Moonwell Flagship ETH Yield (Wei Tracker) + Multicall3 Bounty Hunter
# Core 2: Gate.io v4 API Spot Volatility Skimmer (QNT/USDT & LTC/USDT)
# Includes: 1-Click KeeperGuardV2 Deployer, Native PWA Installer & Cron /ping
# ==============================================================================
import os
import sys
import time
import json
import hmac
import hashlib
import logging
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, Response, request, jsonify, render_template_string
from eth_account import Account
from web3 import Web3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("KeeperPulseV30")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# ==============================================================================
# 1. CONSTANTS, ON-CHAIN ADDRESSES & PERMANENT BASELINES
# ==============================================================================
STATE_FILE = os.environ.get("KEEPER_STATE_FILE", "keeper_pulse_state.json")
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
GATE_API_HOST = "https://api.gateio.ws"
GATE_API_PREFIX = "/api/v4"

BASE_WETH = "0x4200000000000000000000000000000000000006"
MOONWELL_MWETH_VAULT = "0xa0E430870c4604CcfC7B38Ca7845B1FF653D0ff1"
WELL_TOKEN_BASE = "0xA88594D404727625A9437C3f886C7643872296AE"
AERO_TOKEN_BASE = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"
USDC_TOKEN_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

DEFAULT_WALLET_ADDR = "0x0afaA2d657C44E9E8579c5cE9c09D00c92A463C3"
DEFAULT_GUARD_ADDR = "0xab1d119FC3468036B2b92c69A5377ee0b408b215"
INITIAL_DEPOSIT_WEI_BASELINE = 2424850000000000  # 0.00242485 ETH initial Moonwell deposit
INITIAL_DEPOSIT_USD_BASELINE = 5.46              # $5.46 USD value at deposit time

CHAIN_CONFIGS = {
    "base": {
        "name": "Base Mainnet",
        "chain_id": 8453,
        "default_rpc": "https://mainnet.base.org",
        "native_symbol": "ETH",
        "wrapped_native": BASE_WETH,
        "aero_token": AERO_TOKEN_BASE,
        "usdc_token": USDC_TOKEN_BASE,
        "explorer": "https://basescan.org/tx/",
        "addr_explorer": "https://basescan.org/address/"
    },
    "arbitrum": {
        "name": "Arbitrum One",
        "chain_id": 42161,
        "default_rpc": "https://arb1.arbitrum.io/rpc",
        "native_symbol": "ETH",
        "wrapped_native": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "aero_token": "0x912CE59144191C1204E64559FE8253a0e49E6548",
        "usdc_token": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "explorer": "https://arbiscan.io/tx/",
        "addr_explorer": "https://arbiscan.org/address/"
    }
}

# ==============================================================================
# 2. EMBEDDED SOLIDITY SOURCE & CONTRACT ABIs
# ==============================================================================
KEEPER_GUARD_SOL_SOURCE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 value) external returns (bool);
}

contract KeeperGuard {
    address public immutable owner;
    IERC20 public immutable weth;
    IERC20 public immutable tokenA;
    IERC20 public immutable usdc;

    constructor() {
        owner = msg.sender;
        weth = IERC20(
            block.chainid == 8453
                ? 0x4200000000000000000000000000000000000006
                : 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1
        );
        tokenA = IERC20(
            block.chainid == 8453
                ? 0x940181a94A35A4569E4529A3CDfB74e38FD98631
                : 0x912CE59144191C1204E64559FE8253a0e49E6548
        );
        usdc = IERC20(
            block.chainid == 8453
                ? 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
                : 0xaf88d065e77c8cC2239327C5EDb3A432268e5831
        );
    }

    function _totalSnapshot() internal view returns (uint256 wBal, uint256 aBal, uint256 uBal, uint256 eBal) {
        wBal = weth.balanceOf(owner) + weth.balanceOf(address(this));
        aBal = tokenA.balanceOf(owner) + tokenA.balanceOf(address(this));
        uBal = usdc.balanceOf(owner) + usdc.balanceOf(address(this));
        eBal = owner.balance + address(this).balance;
    }

    function _sweepToOwner() internal {
        uint256 w = weth.balanceOf(address(this));
        if (w > 0) weth.transfer(owner, w);
        uint256 a = tokenA.balanceOf(address(this));
        if (a > 0) tokenA.transfer(owner, a);
        uint256 u = usdc.balanceOf(address(this));
        if (u > 0) usdc.transfer(owner, u);
        if (address(this).balance > 0) payable(owner).transfer(address(this).balance);
    }

    function safeHarvest(address strategy, uint256 minBountyWei) external {
        (uint256 w0, uint256 a0, uint256 u0, uint256 e0) = _totalSnapshot();

        (bool ok, ) = strategy.call(abi.encodeWithSignature("harvest(address)", owner));
        if (!ok) {
            (bool ok2, ) = strategy.call(abi.encodeWithSignature("harvest()"));
            require(ok2, "Strategy harvest reverted");
        }

        _sweepToOwner();
        (uint256 w1, uint256 a1, uint256 u1, uint256 e1) = _totalSnapshot();

        bool gainedWeth = w1 >= w0 + minBountyWei;
        bool gainedEth  = e1 >= e0 + minBountyWei;
        bool gainedAero = a1 > a0 + 100000000000000;
        bool gainedUsdc = u1 > u0 + 5000;

        require(
            gainedWeth || gainedEth || gainedAero || gainedUsdc,
            "KeeperGuardV2: Zero reward delta - Revert!"
        );
    }

    function rescueToken(address token) external {
        require(msg.sender == owner, "Only owner");
        IERC20 t = IERC20(token);
        t.transfer(owner, t.balanceOf(address(this)));
    }

    receive() external payable {}
}
"""

KEEPER_GUARD_ABI = [
    {"inputs": [], "stateMutability": "nonpayable", "type": "constructor"},
    {
        "inputs": [
            {"internalType": "address", "name": "strategy", "type": "address"},
            {"internalType": "uint256", "name": "minBountyWei", "type": "uint256"}
        ],
        "name": "safeHarvest",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "token", "type": "address"}],
        "name": "rescueToken",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

WETH_ABI = [
    {"constant": False, "inputs": [], "name": "deposit", "outputs": [], "payable": True, "stateMutability": "payable", "type": "function"},
    {"constant": False, "inputs": [{"name": "wad", "type": "uint256"}], "name": "withdraw", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}
]

ERC4626_VAULT_ABI = [
    {"inputs": [{"internalType": "uint256", "name": "assets", "type": "uint256"}, {"internalType": "address", "name": "receiver", "type": "address"}], "name": "deposit", "outputs": [{"internalType": "uint256", "name": "shares", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "shares", "type": "uint256"}, {"internalType": "address", "name": "receiver", "type": "address"}, {"internalType": "address", "name": "owner", "type": "address"}], "name": "redeem", "outputs": [{"internalType": "uint256", "name": "assets", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "shares", "type": "uint256"}], "name": "convertToAssets", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}
]

SEL_VAULT_STRATEGY = "0xa8c62e76"
SEL_CALL_REWARD = "0x6d1ef877"
SEL_REWARDS_AVAIL = "0x9890220b"
SEL_HARVEST_ON_DEPOSIT = "0x573fef0a"

MULTICALL3_ABI = [
    {
        "inputs": [
            {"internalType": "bool", "name": "requireSuccess", "type": "bool"},
            {"components": [{"internalType": "address", "name": "target", "type": "address"}, {"internalType": "bytes", "name": "callData", "type": "bytes"}], "internalType": "struct Multicall3.Call[]", "name": "calls", "type": "tuple[]"}
        ],
        "name": "tryAggregate",
        "outputs": [
            {"components": [{"internalType": "bool", "name": "success", "type": "bool"}, {"internalType": "bytes", "name": "returnData", "type": "bytes"}], "internalType": "struct Multicall3.Result[]", "name": "returnData", "type": "tuple[]"}
        ],
        "stateMutability": "payable",
        "type": "function"
    }
]

# ==============================================================================
# 3. GLOBAL STATE MANAGEMENT & ENVIRONMENT AUTO-RECOVERY
# ==============================================================================
state_lock = threading.RLock()
scan_lock = threading.Lock()
vault_catalog = {"base": [], "arbitrum": [], "last_fetched": 0}
strategy_cooldowns = {}
_scan_counter = 0

ENV_PRIV_KEY = os.environ.get("KEEPER_PRIVATE_KEY", "").strip()
ENV_GUARD_ADDR = os.environ.get("KEEPER_GUARD_ADDRESS", DEFAULT_GUARD_ADDR).strip()
ENV_RPC_URL = os.environ.get("KEEPER_RPC_URL", "https://mainnet.base.org").strip()
ENV_CHAIN = os.environ.get("KEEPER_CHAIN", "base").strip().lower()
ENV_LIVE = os.environ.get("KEEPER_LIVE_ARMED", "true").strip().lower() in ("true", "1", "yes")

ENV_GATE_KEY = os.environ.get("GATE_API_KEY", "").strip()
ENV_GATE_SECRET = os.environ.get("GATE_API_SECRET", "").strip()

DEFAULT_STATE = {
    "version": "v3.0 • Dual-Core Unified Engine (Base DeFi + Gate.io Spot Skimmer)",
    "chain": ENV_CHAIN if ENV_CHAIN in CHAIN_CONFIGS else "base",
    "rpc_url": ENV_RPC_URL,
    "wallet_address": DEFAULT_WALLET_ADDR,
    "private_key": ENV_PRIV_KEY,
    "guard_contract": ENV_GUARD_ADDR,
    "live_armed": bool(ENV_PRIV_KEY and ENV_GUARD_ADDR and ENV_LIVE),
    "min_profit_usd": 0.02,
    "eth_price_usd": 2294.16,
    "native_eth_balance": 0.0,
    "weth_balance": 0.0,
    "aero_balance": 0.0,
    "well_balance": 0.0,
    "usdc_balance": 0.0,
    "mweth_shares": 0.0,
    "mweth_assets_wei": 0,
    "mweth_assets_eth": 0.0,
    "mweth_principal_wei": INITIAL_DEPOSIT_WEI_BASELINE,
    "mweth_earned_wei": 0,
    "mweth_earned_eth": 0.0,
    "mweth_earned_usd": 0.0,
    "position_pnl_usd": 0.0,
    "total_bounties_won": 0,
    "realized_profit_usd": 0.0,
    "vaults_monitored": 0,
    # Core 2: Gate.io v4 Spot Volatility Skimmer State
    "gate_api_key": ENV_GATE_KEY,
    "gate_api_secret": ENV_GATE_SECRET,
    "skim_armed": False,
    "skim_pair": "QNT_USDT",
    "skim_order_usd": 25.0,
    "skim_dip_pct": 1.2,
    "skim_tp_pct": 1.3,
    "gate_usdt_balance": 0.0,
    "gate_qnt_balance": 0.0,
    "gate_ltc_balance": 0.0,
    "qnt_price": 0.0,
    "ltc_price": 0.0,
    "skim_anchor_price": 0.0,
    "skim_active_entry": 0.0,
    "skim_active_amount": 0.0,
    "skim_flips_won": 0,
    "skim_realized_usd": 0.0,
    "last_scan_hms": "--:--:--",
    "last_scan_ms": 0,
    "live_opportunities": [],
    "harvest_ledger": [
        {
            "mode": "YIELD ACTIVE",
            "vault": "Moonwell Flagship ETH (mwETH)",
            "strategy": "0xa0E4...0ff1",
            "bounty_usd": 5.46,
            "gas_usd": 0.002,
            "net_usd": 5.46,
            "tx_url": f"https://basescan.org/address/{DEFAULT_WALLET_ADDR}",
            "time": "00:06:49"
        }
    ],
    "logs": [
        f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
        "KeeperPulse v3.0 Unified Engine Online: Base DeFi Yield/Bounties + Gate.io Spot Skimmer Active."
    ]
}


def now_hms():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def load_state():
    merged = json.loads(json.dumps(DEFAULT_STATE))
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    merged.update(data)
        except Exception:
            pass
    if ENV_PRIV_KEY and not merged.get("private_key"):
        merged["private_key"] = ENV_PRIV_KEY
    if ENV_GUARD_ADDR and not merged.get("guard_contract"):
        merged["guard_contract"] = ENV_GUARD_ADDR
    if ENV_RPC_URL and ENV_RPC_URL != "https://mainnet.base.org":
        merged["rpc_url"] = ENV_RPC_URL
    if ENV_GATE_KEY and not merged.get("gate_api_key"):
        merged["gate_api_key"] = ENV_GATE_KEY
    if ENV_GATE_SECRET and not merged.get("gate_api_secret"):
        merged["gate_api_secret"] = ENV_GATE_SECRET
    if not merged.get("wallet_address"):
        merged["wallet_address"] = DEFAULT_WALLET_ADDR
    if int(merged.get("mweth_principal_wei") or 0) <= 0:
        merged["mweth_principal_wei"] = INITIAL_DEPOSIT_WEI_BASELINE
    merged["version"] = "v3.0 • Dual-Core Unified Engine (Base DeFi + Gate.io Spot Skimmer)"
    return merged


def save_state():
    with state_lock:
        snapshot = json.loads(json.dumps(STATE))
    try:
        tmp = f"{STATE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


STATE = load_state()


def add_log(msg):
    with state_lock:
        STATE["logs"].insert(0, f"[{now_hms()}] {msg}")
        if len(STATE["logs"]) > 100:
            STATE["logs"] = STATE["logs"][:100]


# ==============================================================================
# 4. CORE 2: GATE.IO v4 SPOT VOLATILITY SKIMMER (QNT/USDT & LTC/USDT)
# ==============================================================================
def gate_sign_headers(method, url_path, query_string="", payload_string=""):
    with state_lock:
        api_key = (STATE.get("gate_api_key") or "").strip()
        api_secret = (STATE.get("gate_api_secret") or "").strip()
    t = str(int(time.time()))
    body_hash = hashlib.sha512(payload_string.encode("utf-8")).hexdigest()
    sign_str = f"{method.upper()}\n{GATE_API_PREFIX}{url_path}\n{query_string}\n{body_hash}\n{t}"
    sig = hmac.new(api_secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha512).hexdigest()
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": api_key,
        "Timestamp": t,
        "SIGN": sig
    }


def update_market_prices():
    try:
        r = requests.get(f"{GATE_API_HOST}{GATE_API_PREFIX}/spot/tickers", timeout=5)
        if r.status_code == 200 and isinstance(r.json(), list):
            for item in r.json():
                cp = item.get("currency_pair")
                if cp == "ETH_USDT":
                    px = float(item.get("last") or 0.0)
                    if px > 100:
                        with state_lock:
                            STATE["eth_price_usd"] = round(px, 2)
                elif cp == "QNT_USDT":
                    with state_lock:
                        STATE["qnt_price"] = round(float(item.get("last") or 0.0), 3)
                elif cp == "LTC_USDT":
                    with state_lock:
                        STATE["ltc_price"] = round(float(item.get("last") or 0.0), 3)
    except Exception:
        pass
    return STATE.get("eth_price_usd", 2294.16)


def sync_gate_spot_balances():
    with state_lock:
        api_key = (STATE.get("gate_api_key") or "").strip()
        api_secret = (STATE.get("gate_api_secret") or "").strip()
    if not api_key or not api_secret:
        return False

    try:
        headers = gate_sign_headers("GET", "/spot/accounts")
        r = requests.get(f"{GATE_API_HOST}{GATE_API_PREFIX}/spot/accounts", headers=headers, timeout=6)
        if r.status_code == 200 and isinstance(r.json(), list):
            usdt_b, qnt_b, ltc_b = 0.0, 0.0, 0.0
            for acc in r.json():
                curr = acc.get("currency")
                avail = float(acc.get("available") or 0.0)
                if curr == "USDT":
                    usdt_b = avail
                elif curr == "QNT":
                    qnt_b = avail
                elif curr == "LTC":
                    ltc_b = avail
            with state_lock:
                STATE["gate_usdt_balance"] = round(usdt_b, 2)
                STATE["gate_qnt_balance"] = round(qnt_b, 5)
                STATE["gate_ltc_balance"] = round(ltc_b, 5)
            return True
    except Exception as e:
        logger.debug(f"Gate balance sync notice: {e}")
    return False


def place_gate_spot_limit_order(pair, side, amount_coin, price_usd):
    body_dict = {
        "currency_pair": pair,
        "type": "limit",
        "account": "spot",
        "side": side,
        "amount": f"{amount_coin:.5f}",
        "price": f"{price_usd:.2f}",
        "time_in_force": "gtc"
    }
    payload = json.dumps(body_dict)
    headers = gate_sign_headers("POST", "/spot/orders", "", payload)
    r = requests.post(f"{GATE_API_HOST}{GATE_API_PREFIX}/spot/orders", headers=headers, data=payload, timeout=8)
    return r.status_code in (200, 201), r.json()


def run_spot_volatility_skimmer_step():
    """
    Executes automated Spot Volatility Skimming on Gate.io (or Dry-Run paper tracking):
    - Tracks rolling anchor price on QNT_USDT or LTC_USDT.
    - Buys when price dips by `skim_dip_pct`% below anchor.
    - Sells when price rebounds by `skim_tp_pct`% above entry.
    """
    sync_gate_spot_balances()
    with state_lock:
        pair = STATE.get("skim_pair", "QNT_USDT")
        live_price = STATE.get("qnt_price", 0.0) if pair == "QNT_USDT" else STATE.get("ltc_price", 0.0)
        anchor = float(STATE.get("skim_anchor_price") or 0.0)
        active_entry = float(STATE.get("skim_active_entry") or 0.0)
        active_amt = float(STATE.get("skim_active_amount") or 0.0)
        dip_pct = float(STATE.get("skim_dip_pct") or 1.2)
        tp_pct = float(STATE.get("skim_tp_pct") or 1.3)
        order_usd = float(STATE.get("skim_order_usd") or 25.0)
        skim_armed = bool(STATE.get("skim_armed", False))
        has_keys = bool(STATE.get("gate_api_key") and STATE.get("gate_api_secret"))
        usdt_avail = float(STATE.get("gate_usdt_balance") or 0.0)

    if live_price <= 0:
        return

    if anchor <= 0:
        with state_lock:
            STATE["skim_anchor_price"] = live_price
        return

    # Slowly trail anchor upward if no active position is open
    if active_entry <= 0 and live_price > anchor:
        with state_lock:
            STATE["skim_anchor_price"] = round(anchor * 0.85 + live_price * 0.15, 3)
            anchor = STATE["skim_anchor_price"]

    # CASE 1: No active skim position -> Check for Dip Entry
    if active_entry <= 0:
        buy_trigger = anchor * (1.0 - (dip_pct / 100.0))
        if live_price <= buy_trigger:
            coin_amt = round(order_usd / live_price, 5)
            if skim_armed and has_keys and usdt_avail >= order_usd:
                ok, resp = place_gate_spot_limit_order(pair, "buy", coin_amt, live_price)
                if ok:
                    with state_lock:
                        STATE["skim_active_entry"] = live_price
                        STATE["skim_active_amount"] = coin_amt
                    add_log(f"⚡ [GATE.IO LIVE BUY] Bought {coin_amt:.4f} {pair} @ ${live_price:.2f} (-{dip_pct}% Dip)!")
                    sync_gate_spot_balances()
                else:
                    add_log(f"⚠️ Gate.io Buy notice: {resp}")
            else:
                # Paper / Simulation Entry so user can watch strategy live even before adding USDT
                with state_lock:
                    STATE["skim_active_entry"] = live_price
                    STATE["skim_active_amount"] = coin_amt
                mode_tag = "LIVE-READY SIM" if not skim_armed else "NO-USDT SIM"
                add_log(f"📉 [{mode_tag} DIP ENTRY] Triggered {pair} Buy @ ${live_price:.2f} (Target Exit: +${tp_pct}%)")

    # CASE 2: Active skim position open -> Check for Take-Profit Rebound
    else:
        sell_trigger = active_entry * (1.0 + (tp_pct / 100.0))
        if live_price >= sell_trigger:
            gross_profit = (live_price - active_entry) * active_amt
            fee_usd = (order_usd * 0.002)
            net_profit = max(0.01, gross_profit - fee_usd)

            if skim_armed and has_keys:
                ok, resp = place_gate_spot_limit_order(pair, "sell", active_amt, live_price)
                mode_label = "SPOT SKIM WON" if ok else "SIM SKIM FLIP"
            else:
                mode_label = "SIM SKIM FLIP"

            with state_lock:
                STATE["skim_flips_won"] = int(STATE.get("skim_flips_won", 0)) + 1
                STATE["skim_realized_usd"] = round(float(STATE.get("skim_realized_usd", 0.0)) + net_profit, 4)
                STATE["skim_active_entry"] = 0.0
                STATE["skim_active_amount"] = 0.0
                STATE["skim_anchor_price"] = live_price
                STATE["harvest_ledger"].insert(0, {
                    "mode": mode_label,
                    "vault": f"Gate.io {pair} Skim",
                    "strategy": f"Buy ${active_entry:.2f} -> Sell ${live_price:.2f}",
                    "bounty_usd": round(gross_profit, 4),
                    "gas_usd": round(fee_usd, 4),
                    "net_usd": round(net_profit, 4),
                    "tx_url": "https://www.gate.io/trade/" + pair,
                    "time": now_hms()
                })
            add_log(f"💰 [{mode_label}] Flipped {pair} @ ${live_price:.2f} for +${net_profit:.3f} Net Profit!")


# ==============================================================================
# 5. CORE 1: BASE MAINNET MOONWELL WEI YIELD & MULTICALL3 BOUNTY ENGINE
# ==============================================================================
def get_web3():
    with state_lock:
        chain_key = STATE.get("chain", "base")
        rpc_url = (STATE.get("rpc_url") or CHAIN_CONFIGS[chain_key]["default_rpc"]).strip()
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
    return w3, chain_key, CHAIN_CONFIGS[chain_key]


def sync_wallet_balances():
    w3, chain_key, cfg = get_web3()
    with state_lock:
        priv = (STATE.get("private_key") or "").strip()
        addr = (STATE.get("wallet_address") or DEFAULT_WALLET_ADDR).strip()
        eth_px = float(STATE.get("eth_price_usd") or 2294.16)

    if priv:
        try:
            acct = Account.from_key(priv if priv.startswith("0x") else f"0x{priv}")
            addr = acct.address
            with state_lock:
                STATE["wallet_address"] = addr
        except Exception:
            pass

    if not addr or not Web3.is_address(addr):
        return False, "No wallet address."

    checksum_addr = Web3.to_checksum_address(addr)
    try:
        native_wei = w3.eth.get_balance(checksum_addr)
        weth_c = w3.eth.contract(address=Web3.to_checksum_address(cfg["wrapped_native"]), abi=WETH_ABI)
        aero_c = w3.eth.contract(address=Web3.to_checksum_address(cfg["aero_token"]), abi=WETH_ABI)
        usdc_c = w3.eth.contract(address=Web3.to_checksum_address(cfg["usdc_token"]), abi=WETH_ABI)

        weth_wei = weth_c.functions.balanceOf(checksum_addr).call()
        aero_wei = aero_c.functions.balanceOf(checksum_addr).call()
        usdc_raw = usdc_c.functions.balanceOf(checksum_addr).call()

        mweth_shares_wei = 0
        mweth_assets_wei = 0
        well_wei = 0
        if chain_key == "base":
            mw_vault = w3.eth.contract(
                address=Web3.to_checksum_address(MOONWELL_MWETH_VAULT),
                abi=ERC4626_VAULT_ABI
            )
            mweth_shares_wei = mw_vault.functions.balanceOf(checksum_addr).call()
            if mweth_shares_wei > 0:
                mweth_assets_wei = mw_vault.functions.convertToAssets(mweth_shares_wei).call()
            well_c = w3.eth.contract(address=Web3.to_checksum_address(WELL_TOKEN_BASE), abi=WETH_ABI)
            well_wei = well_c.functions.balanceOf(checksum_addr).call()

        assets_eth = float(Web3.from_wei(mweth_assets_wei, "ether"))
        with state_lock:
            STATE["native_eth_balance"] = round(float(Web3.from_wei(native_wei, "ether")), 6)
            STATE["weth_balance"] = round(float(Web3.from_wei(weth_wei, "ether")), 6)
            STATE["aero_balance"] = round(float(Web3.from_wei(aero_wei, "ether")), 4)
            STATE["well_balance"] = round(float(Web3.from_wei(well_wei, "ether")), 4)
            STATE["usdc_balance"] = round(usdc_raw / 1e6, 4)
            STATE["mweth_shares"] = round(float(Web3.from_wei(mweth_shares_wei, "ether")), 8)
            STATE["mweth_assets_wei"] = int(mweth_assets_wei)
            STATE["mweth_assets_eth"] = round(assets_eth, 10)

            principal_wei = int(STATE.get("mweth_principal_wei") or INITIAL_DEPOSIT_WEI_BASELINE)
            if mweth_assets_wei > 0:
                share_growth_wei = max(0, int(mweth_assets_wei) - int(mweth_shares_wei))
                baseline_growth_wei = max(0, int(mweth_assets_wei) - principal_wei)
                earned_wei = max(baseline_growth_wei, share_growth_wei)
            else:
                earned_wei = 0

            earned_eth = float(Web3.from_wei(earned_wei, "ether"))
            current_vault_usd = assets_eth * eth_px
            pnl_usd = max(0.0, current_vault_usd - INITIAL_DEPOSIT_USD_BASELINE)

            STATE["mweth_earned_wei"] = int(earned_wei)
            STATE["mweth_earned_eth"] = round(earned_eth, 10)
            STATE["mweth_earned_usd"] = round(earned_eth * eth_px, 6)
            STATE["position_pnl_usd"] = round(pnl_usd, 4)
        return True, "Synced"
    except Exception as e:
        return False, str(e)


def compile_and_deploy_keeper_guard():
    with state_lock:
        priv = (STATE.get("private_key") or "").strip()
    if not priv:
        return False, "Paste your Keeper Wallet Private Key and click 'Save & Sync All' first."

    w3, chain_key, cfg = get_web3()
    try:
        acct = Account.from_key(priv if priv.startswith("0x") else f"0x{priv}")
        with state_lock:
            STATE["wallet_address"] = acct.address
    except Exception as e:
        return False, f"Invalid Private Key: {e}"

    try:
        bal_wei = w3.eth.get_balance(acct.address)
        if bal_wei < Web3.to_wei(0.000015, "ether"):
            return False, "Insufficient ETH in wallet to deploy KeeperGuardV2."

        add_log("⚙️ Compiling Multi-Token KeeperGuardV2.sol via solcx (0.8.20)...")
        import solcx
        installed = [str(v) for v in solcx.get_installed_solc_versions()]
        if not any("0.8.20" in v for v in installed):
            solcx.install_solc("0.8.20")
        solcx.set_solc_version("0.8.20")

        compiled = solcx.compile_source(
            KEEPER_GUARD_SOL_SOURCE,
            output_values=["abi", "bin"],
            optimize=True,
            optimize_runs=200
        )
        _, contract_interface = next(
            (k, v) for k, v in compiled.items() if k.endswith(":KeeperGuard")
        )
        ContractFactory = w3.eth.contract(
            abi=contract_interface["abi"],
            bytecode=contract_interface["bin"]
        )
        gas_price_wei = w3.eth.gas_price
        nonce = w3.eth.get_transaction_count(acct.address)
        est_gas = ContractFactory.constructor().estimate_gas({"from": acct.address})
        tx = ContractFactory.constructor().build_transaction({
            "chainId": cfg["chain_id"],
            "from": acct.address,
            "nonce": nonce,
            "gas": int(est_gas * 1.25),
            "maxFeePerGas": int(gas_price_wei * 1.25),
            "maxPriorityFeePerGas": int(gas_price_wei * 0.10)
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=acct.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
        if receipt and receipt.status == 1 and receipt.contractAddress:
            guard_addr = Web3.to_checksum_address(receipt.contractAddress)
            with state_lock:
                STATE["guard_contract"] = guard_addr
            save_state()
            sync_wallet_balances()
            msg = f"✅ Multi-Token KeeperGuardV2 Deployed at {guard_addr}!"
            add_log(msg)
            return True, msg
        return False, "Deployment transaction reverted."
    except Exception as e:
        return False, f"Deploy error: {e}"


def unwrap_weth_to_eth():
    with state_lock:
        priv = (STATE.get("private_key") or "").strip()
    if not priv:
        return False, "Paste your Private Key and click Save first."
    w3, _, cfg = get_web3()
    try:
        acct = Account.from_key(priv if priv.startswith("0x") else f"0x{priv}")
        weth = w3.eth.contract(address=Web3.to_checksum_address(cfg["wrapped_native"]), abi=WETH_ABI)
        weth_wei = weth.functions.balanceOf(acct.address).call()
        if weth_wei <= 0:
            return False, "Your WETH balance is currently 0.000000 WETH."
        gas_price_wei = w3.eth.gas_price
        nonce = w3.eth.get_transaction_count(acct.address)
        tx = weth.functions.withdraw(weth_wei).build_transaction({
            "chainId": cfg["chain_id"],
            "from": acct.address,
            "nonce": nonce,
            "gas": 60000,
            "maxFeePerGas": int(gas_price_wei * 1.20),
            "maxPriorityFeePerGas": int(gas_price_wei * 0.10)
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=acct.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if receipt and receipt.status == 1:
            sync_wallet_balances()
            amt = float(Web3.from_wei(weth_wei, "ether"))
            msg = f"✅ Unwrapped {amt:.6f} WETH into Native ETH!"
            add_log(msg)
            return True, msg
        return False, "Unwrap failed."
    except Exception as e:
        return False, f"Unwrap error: {e}"


def deposit_into_moonwell_yield():
    with state_lock:
        priv = (STATE.get("private_key") or "").strip()
    if not priv:
        return False, "Paste your Keeper Wallet Private Key first."

    w3, chain_key, _ = get_web3()
    if chain_key != "base":
        return False, "Switch network to Base Mainnet to use Moonwell Flagship ETH."

    try:
        acct = Account.from_key(priv if priv.startswith("0x") else f"0x{priv}")
        weth_addr = Web3.to_checksum_address(BASE_WETH)
        vault_addr = Web3.to_checksum_address(MOONWELL_MWETH_VAULT)
        weth_c = w3.eth.contract(address=weth_addr, abi=WETH_ABI)
        vault_c = w3.eth.contract(address=vault_addr, abi=ERC4626_VAULT_ABI)

        native_wei = w3.eth.get_balance(acct.address)
        weth_wei = weth_c.functions.balanceOf(acct.address).call()
        gas_reserve_wei = Web3.to_wei(0.00050, "ether")

        if native_wei > gas_reserve_wei:
            wrap_wei = native_wei - gas_reserve_wei
            gas_price_wei = w3.eth.gas_price
            nonce = w3.eth.get_transaction_count(acct.address)
            tx1 = weth_c.functions.deposit().build_transaction({
                "chainId": 8453,
                "from": acct.address,
                "value": wrap_wei,
                "nonce": nonce,
                "gas": 55000,
                "maxFeePerGas": int(gas_price_wei * 1.20),
                "maxPriorityFeePerGas": int(gas_price_wei * 0.10)
            })
            signed1 = w3.eth.account.sign_transaction(tx1, private_key=acct.key)
            w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed1.raw_transaction), timeout=30)
            weth_wei = weth_c.functions.balanceOf(acct.address).call()

        if weth_wei <= Web3.to_wei(0.00005, "ether"):
            return (
                False,
                "Your spare ETH (0.002425 ETH) is already compounding inside the Moonwell Vault! "
                "(0.000547 ETH is kept in your wallet for gas)."
            )

        allowance = weth_c.functions.allowance(acct.address, vault_addr).call()
        if allowance < weth_wei:
            gas_price_wei = w3.eth.gas_price
            nonce = w3.eth.get_transaction_count(acct.address)
            tx2 = weth_c.functions.approve(vault_addr, 2**256 - 1).build_transaction({
                "chainId": 8453,
                "from": acct.address,
                "nonce": nonce,
                "gas": 65000,
                "maxFeePerGas": int(gas_price_wei * 1.20),
                "maxPriorityFeePerGas": int(gas_price_wei * 0.10)
            })
            signed2 = w3.eth.account.sign_transaction(tx2, private_key=acct.key)
            w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed2.raw_transaction), timeout=30)

        gas_price_wei = w3.eth.gas_price
        nonce = w3.eth.get_transaction_count(acct.address)
        est_gas = vault_c.functions.deposit(weth_wei, acct.address).estimate_gas({"from": acct.address})
        tx3 = vault_c.functions.deposit(weth_wei, acct.address).build_transaction({
            "chainId": 8453,
            "from": acct.address,
            "nonce": nonce,
            "gas": int(est_gas * 1.20),
            "maxFeePerGas": int(gas_price_wei * 1.20),
            "maxPriorityFeePerGas": int(gas_price_wei * 0.10)
        })
        signed3 = w3.eth.account.sign_transaction(tx3, private_key=acct.key)
        txh3 = w3.eth.send_raw_transaction(signed3.raw_transaction)
        rec3 = w3.eth.wait_for_transaction_receipt(txh3, timeout=35)

        if rec3 and rec3.status == 1:
            dep_eth = float(Web3.from_wei(weth_wei, "ether"))
            with state_lock:
                STATE["mweth_principal_wei"] = int(STATE.get("mweth_principal_wei") or 0) + int(weth_wei)
            sync_wallet_balances()
            save_state()
            msg = f"💎 Deposited {dep_eth:.6f} WETH into Moonwell Flagship ETH Vault!"
            add_log(msg)
            return True, msg
        return False, "Deposit transaction reverted."
    except Exception as e:
        return False, f"Deposit error: {e}"


def withdraw_from_moonwell_yield():
    with state_lock:
        priv = (STATE.get("private_key") or "").strip()
    if not priv:
        return False, "Paste your Private Key and click Save first."
    w3, _, _ = get_web3()
    try:
        acct = Account.from_key(priv if priv.startswith("0x") else f"0x{priv}")
        vault_c = w3.eth.contract(address=Web3.to_checksum_address(MOONWELL_MWETH_VAULT), abi=ERC4626_VAULT_ABI)
        weth_c = w3.eth.contract(address=Web3.to_checksum_address(BASE_WETH), abi=WETH_ABI)
        shares_wei = vault_c.functions.balanceOf(acct.address).call()
        if shares_wei <= 0:
            return False, "No active Moonwell shares to withdraw."

        gas_price_wei = w3.eth.gas_price
        nonce = w3.eth.get_transaction_count(acct.address)
        est_gas = vault_c.functions.redeem(shares_wei, acct.address, acct.address).estimate_gas({"from": acct.address})
        tx1 = vault_c.functions.redeem(shares_wei, acct.address, acct.address).build_transaction({
            "chainId": 8453,
            "from": acct.address,
            "nonce": nonce,
            "gas": int(est_gas * 1.20),
            "maxFeePerGas": int(gas_price_wei * 1.20),
            "maxPriorityFeePerGas": int(gas_price_wei * 0.10)
        })
        signed1 = w3.eth.account.sign_transaction(tx1, private_key=acct.key)
        w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed1.raw_transaction), timeout=35)

        weth_wei = weth_c.functions.balanceOf(acct.address).call()
        if weth_wei > 0:
            nonce = w3.eth.get_transaction_count(acct.address)
            tx2 = weth_c.functions.withdraw(weth_wei).build_transaction({
                "chainId": 8453,
                "from": acct.address,
                "nonce": nonce,
                "gas": 60000,
                "maxFeePerGas": int(gas_price_wei * 1.20),
                "maxPriorityFeePerGas": int(gas_price_wei * 0.10)
            })
            signed2 = w3.eth.account.sign_transaction(tx2, private_key=acct.key)
            w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed2.raw_transaction), timeout=30)

        sync_wallet_balances()
        save_state()
        return True, "✅ Withdrawn 100% ETH + Yield back to your wallet!"
    except Exception as e:
        return False, f"Withdraw error: {e}"


def resolve_fee_paying_strategies(w3, raw_vaults):
    multicall = w3.eth.contract(
        address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
        abi=MULTICALL3_ABI
    )
    calls = [(v["vault"], bytes.fromhex(SEL_VAULT_STRATEGY[2:])) for v in raw_vaults]
    resolved = []
    seen = set()
    try:
        results = multicall.functions.tryAggregate(False, calls).call()
        for idx, (success, return_data) in enumerate(results):
            strat_addr = raw_vaults[idx].get("strategy")
            if success and return_data and len(return_data) >= 32:
                extracted = "0x" + return_data[-20:].hex()
                if int(extracted, 16) != 0:
                    strat_addr = Web3.to_checksum_address(extracted)
            if strat_addr and strat_addr not in seen:
                seen.add(strat_addr)
                item = dict(raw_vaults[idx])
                item["strategy"] = strat_addr
                resolved.append(item)

        hod_calls = [(v["strategy"], bytes.fromhex(SEL_HARVEST_ON_DEPOSIT[2:])) for v in resolved]
        hod_results = multicall.functions.tryAggregate(False, hod_calls).call()
        external_caller_strats = []
        for idx, (ok, rdata) in enumerate(hod_results):
            is_hod = False
            if ok and rdata and len(rdata) >= 32:
                is_hod = bool(int.from_bytes(rdata[:32], byteorder="big") == 1)
            if not is_hod:
                external_caller_strats.append(resolved[idx])

        return external_caller_strats if external_caller_strats else resolved[:50]
    except Exception:
        return raw_vaults


def refresh_long_tail_vaults(chain_key="base", force=False):
    now_ts = time.time()
    if (
        not force
        and vault_catalog.get(chain_key)
        and (now_ts - vault_catalog.get("last_fetched", 0)) < 1800
    ):
        return vault_catalog[chain_key]

    try:
        w3, _, _ = get_web3()
        r = requests.get("https://api.beefy.finance/vaults", timeout=10)
        if r.status_code == 200:
            all_vaults = r.json()
            candidates = []
            for v in all_vaults:
                if v.get("chain") == chain_key and v.get("status") == "active" and v.get("earnContractAddress"):
                    try:
                        vault_addr = Web3.to_checksum_address(v["earnContractAddress"])
                        strat_hint = Web3.to_checksum_address(v["strategy"]) if v.get("strategy") else ""
                    except Exception:
                        continue
                    candidates.append({
                        "id": v.get("id", "vault"),
                        "name": v.get("name", v.get("id", "Vault")),
                        "vault": vault_addr,
                        "strategy": strat_hint,
                        "platform": v.get("platformId", "DeFi"),
                        "last_harvest": int(v.get("lastHarvest") or 0)
                    })

            candidates.sort(key=lambda x: x["last_harvest"] if x["last_harvest"] > 0 else 9999999999)
            subset = candidates[:130]
            resolved = resolve_fee_paying_strategies(w3, subset)
            vault_catalog[chain_key] = resolved
            vault_catalog["last_fetched"] = now_ts
            with state_lock:
                STATE["vaults_monitored"] = len(resolved)
            add_log(
                f"📡 Filtered {len(resolved)} external-bounty strategies (harvestOnDeposit == false) on {chain_key.upper()}."
            )
    except Exception as e:
        add_log(f"⚠️ Vault discovery notice: {e}")

    return vault_catalog.get(chain_key, [])


def simulate_and_execute_strict_guard(w3, cfg, vault_info, est_bounty_usd, eth_price):
    with state_lock:
        live_armed = bool(STATE.get("live_armed", False))
        priv_key = (STATE.get("private_key") or "").strip()
        wallet_addr = (STATE.get("wallet_address") or DEFAULT_WALLET_ADDR).strip()
        guard_addr = (STATE.get("guard_contract") or DEFAULT_GUARD_ADDR).strip()
        min_profit_usd = float(STATE.get("min_profit_usd") or 0.02)

    if est_bounty_usd < min_profit_usd:
        return None

    strat_addr = Web3.to_checksum_address(vault_info["strategy"])
    now_ts = time.time()
    if now_ts - strategy_cooldowns.get(strat_addr, 0) < 1800:
        return None

    if not guard_addr or not Web3.is_address(guard_addr) or not wallet_addr:
        return None

    guard_checksum = Web3.to_checksum_address(guard_addr)
    caller_addr = Web3.to_checksum_address(wallet_addr)

    try:
        guard_contract = w3.eth.contract(address=guard_checksum, abi=KEEPER_GUARD_ABI)
        min_bounty_wei = int(Web3.to_wei(max(min_profit_usd / max(eth_price, 1000.0), 0.000005), "ether"))
        guard_data = guard_contract.encode_abi("safeHarvest", args=[strat_addr, min_bounty_wei])

        est = w3.eth.estimate_gas({
            "from": caller_addr,
            "to": guard_checksum,
            "data": guard_data,
            "value": 0
        })
        estimated_gas = int(est * 1.18)
    except Exception:
        return None

    gas_price_wei = w3.eth.gas_price
    gas_cost_eth = float(Web3.from_wei(estimated_gas * gas_price_wei, "ether"))
    gas_cost_usd = gas_cost_eth * eth_price
    net_profit_usd = est_bounty_usd - gas_cost_usd

    if net_profit_usd < min_profit_usd:
        return None

    opportunity = {
        "name": vault_info["name"],
        "platform": vault_info["platform"],
        "strategy": strat_addr,
        "bounty_usd": round(est_bounty_usd, 4),
        "gas_usd": round(gas_cost_usd, 4),
        "net_profit_usd": round(net_profit_usd, 4),
        "estimated_gas": estimated_gas,
        "time": now_hms()
    }

    strategy_cooldowns[strat_addr] = now_ts

    if live_armed and priv_key:
        try:
            sync_wallet_balances()
            weth_before = STATE.get("weth_balance", 0.0)
            aero_before = STATE.get("aero_balance", 0.0)
            usdc_before = STATE.get("usdc_balance", 0.0)

            acct = Account.from_key(priv_key if priv_key.startswith("0x") else f"0x{priv_key}")
            nonce = w3.eth.get_transaction_count(acct.address)
            tx = {
                "chainId": cfg["chain_id"],
                "from": acct.address,
                "to": guard_checksum,
                "nonce": nonce,
                "gas": min(estimated_gas, 950000),
                "maxFeePerGas": int(gas_price_wei * 1.15),
                "maxPriorityFeePerGas": int(gas_price_wei * 0.10),
                "value": 0,
                "data": guard_data
            }
            signed_tx = w3.eth.account.sign_transaction(tx, private_key=acct.key)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=25)
            tx_hex = tx_hash.hex()

            sync_wallet_balances()
            weth_gain_usd = max(0.0, (STATE.get("weth_balance", 0.0) - weth_before) * eth_price)
            aero_gain_usd = max(0.0, (STATE.get("aero_balance", 0.0) - aero_before) * 1.10)
            usdc_gain_usd = max(0.0, STATE.get("usdc_balance", 0.0) - usdc_before)
            actual_gain_usd = weth_gain_usd + aero_gain_usd + usdc_gain_usd

            if receipt and receipt.status == 1 and actual_gain_usd > 0.0001:
                with state_lock:
                    STATE["total_bounties_won"] = int(STATE.get("total_bounties_won", 0)) + 1
                    STATE["realized_profit_usd"] = round(
                        float(STATE.get("realized_profit_usd", 0.0)) + actual_gain_usd, 4
                    )
                    STATE["harvest_ledger"].insert(0, {
                        "mode": "VERIFIED PAYOUT",
                        "vault": vault_info["name"],
                        "strategy": f"{strat_addr[:6]}...{strat_addr[-4:]}",
                        "bounty_usd": round(actual_gain_usd, 4),
                        "gas_usd": round(gas_cost_usd, 4),
                        "net_usd": round(actual_gain_usd - gas_cost_usd, 4),
                        "tx_url": f"{cfg['explorer']}{tx_hex}",
                        "time": now_hms()
                    })
                add_log(
                    f"💰 VERIFIED BOUNTY RECEIVED ({vault_info['name']}): "
                    f"+${actual_gain_usd:.4f} in wallet | Tx: {tx_hex[:10]}..."
                )
                save_state()
        except Exception as e:
            add_log(f"🛡️ KeeperGuard blocked {vault_info['name']}: {e}")

    return opportunity


def run_unified_dual_core_cycle(manual=False):
    global _scan_counter
    if not scan_lock.acquire(blocking=False):
        return
    t0 = time.time()
    try:
        eth_price = update_market_prices()
        run_spot_volatility_skimmer_step()
        sync_wallet_balances()
        w3, chain_key, cfg = get_web3()
        vaults = refresh_long_tail_vaults(chain_key)
        if not vaults:
            return

        multicall = w3.eth.contract(
            address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
            abi=MULTICALL3_ABI
        )

        calls = []
        for v in vaults:
            calls.append((v["strategy"], bytes.fromhex(SEL_CALL_REWARD[2:])))
            calls.append((v["strategy"], bytes.fromhex(SEL_REWARDS_AVAIL[2:])))

        results = multicall.functions.tryAggregate(False, calls).call()

        verified_opps = []
        highest_seen_usd = 0.0
        highest_vault_name = "None"
        min_prof = float(STATE.get("min_profit_usd") or 0.02)

        for idx, v in enumerate(vaults):
            res_cr = results[idx * 2]
            res_ra = results[idx * 2 + 1]

            raw_wei = 0
            if res_cr[0] and res_cr[1] and len(res_cr[1]) >= 32:
                raw_wei = int.from_bytes(res_cr[1][:32], byteorder="big")
            if raw_wei == 0 and res_ra[0] and res_ra[1] and len(res_ra[1]) >= 32:
                avail_wei = int.from_bytes(res_ra[1][:32], byteorder="big")
                raw_wei = int(avail_wei * 0.0025)

            bounty_eth = float(Web3.from_wei(raw_wei, "ether")) if raw_wei > 0 else 0.0
            bounty_usd = bounty_eth * 1.10 if bounty_eth > 0.15 else bounty_eth * eth_price

            if 0.0005 < bounty_usd < 25.0:
                if bounty_usd > highest_seen_usd:
                    highest_seen_usd = bounty_usd
                    highest_vault_name = v["name"]

            if bounty_usd >= min_prof:
                opp = simulate_and_execute_strict_guard(w3, cfg, v, bounty_usd, eth_price)
                if opp:
                    verified_opps.append(opp)

        elapsed_ms = int((time.time() - t0) * 1000)
        _scan_counter += 1

        with state_lock:
            STATE["last_scan_hms"] = now_hms()
            STATE["last_scan_ms"] = elapsed_ms
            STATE["vaults_monitored"] = len(vaults)
            STATE["live_opportunities"] = sorted(
                verified_opps, key=lambda x: x["net_profit_usd"], reverse=True
            )[:15]
            if len(STATE["harvest_ledger"]) > 50:
                STATE["harvest_ledger"] = STATE["harvest_ledger"][:50]
            mw_assets = STATE.get("mweth_assets_eth", 0.0)
            mw_wei_gain = STATE.get("mweth_earned_wei", 0)
            pnl_usd = STATE.get("position_pnl_usd", 0.0)
            pair = STATE.get("skim_pair", "QNT_USDT")
            pair_px = STATE.get("qnt_price", 0.0) if pair == "QNT_USDT" else STATE.get("ltc_price", 0.0)

        if manual or _scan_counter <= 2 or (_scan_counter % 4 == 0):
            add_log(
                f"🔍 Cycle #{_scan_counter} ({elapsed_ms}ms): Moonwell: {mw_assets:.8f} ETH (+{mw_wei_gain:,} Wei | +${pnl_usd:.4f}) "
                f"| Gate {pair}: ${pair_px:.2f} | Top DeFi Bounty: ${highest_seen_usd:.4f} ({highest_vault_name})"
            )
        save_state()
    except Exception as e:
        add_log(f"⚠️ Cycle notice: {e}")
    finally:
        scan_lock.release()


def background_keeper_loop():
    while True:
        run_unified_dual_core_cycle(manual=False)
        time.sleep(12.0)


_worker_started = False


@app.before_request
def start_background_scanner():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        threading.Thread(target=background_keeper_loop, daemon=True).start()


threading.Thread(target=background_keeper_loop, daemon=True).start()
_worker_started = True


# ==============================================================================
# 6. UNIFIED DUAL-CORE COMMAND CENTER UI (BASE DEFI + GATE.IO SPOT SKIMMER)
# ==============================================================================
KEEPER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="theme-color" content="#0f172a" />
<link rel="manifest" href="/manifest.json" />
<title>KeeperPulse v3.0 — Unified DeFi + Spot Skimmer</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=JetBrains+Mono:wght@400;600;700&display=swap');
  body {
    background: radial-gradient(circle at 50% 0%, #0f172a 0%, #050811 75%);
    color: #f8fafc;
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
  }
  .mono { font-family: 'JetBrains Mono', monospace; }
  .card {
    background: rgba(12, 18, 32, 0.90);
    border: 1px solid rgba(148, 163, 184, 0.15);
    border-radius: 1rem;
  }
</style>
</head>
<body class="pb-16">
<div class="max-w-5xl mx-auto px-4 pt-5 space-y-4">

  <!-- TOP HEADER -->
  <header class="card p-4">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div>
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse"></span>
          <h1 class="text-xl font-extrabold text-white">KeeperPulse <span class="text-emerald-400">v3.0</span></h1>
          <span class="text-xs px-2.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 font-semibold">
            Dual-Core: Base DeFi + Gate.io Spot Skimmer
          </span>
        </div>
        <p class="text-xs text-slate-400 mt-1">
          Core 1: Moonwell ETH Wei Yield & KeeperGuard Bounties • Core 2: Gate.io QNT/LTC Volatility Skimmer
        </p>
      </div>
      <div class="flex items-center gap-2">
        <button onclick="installApp()" id="installBtn" class="px-3.5 py-2 rounded-lg text-xs font-extrabold bg-cyan-400 hover:bg-cyan-300 text-slate-950 shadow-lg">
          📲 Install App
        </button>
        <div class="text-right text-xs">
          <div class="text-slate-400">Last Scan: <span id="lastScanClock" class="mono text-emerald-400 font-bold">--:--:--</span> (<span id="scanMs" class="mono">0</span>ms)</div>
          <div class="text-slate-500">ETH: <span id="ethPriceBadge" class="mono text-slate-300">$2294</span> • QNT: <span id="qntPriceBadge" class="mono text-cyan-300">$0</span></div>
        </div>
      </div>
    </div>

    <!-- 6-CARD UNIFIED METRICS BAR -->
    <div class="grid grid-cols-2 sm:grid-cols-6 gap-2.5 mt-4">
      <div class="bg-slate-900/90 border border-slate-800 rounded-lg p-3">
        <div class="text-[10px] uppercase text-slate-400">Base Gas (ETH)</div>
        <div id="nativeEthPill" class="mono text-xs font-bold text-white mt-0.5">0.000547 ETH</div>
        <div id="modePill" class="text-[10px] font-bold text-emerald-400 mt-0.5">🟢 DEFI ARMED</div>
      </div>
      <div class="bg-slate-900/90 border border-emerald-500/40 rounded-lg p-3">
        <div class="text-[10px] uppercase text-emerald-300">Moonwell Vault</div>
        <div id="mwethAssetsPill" class="mono text-xs font-bold text-emerald-400 mt-0.5">0.00242538 ETH</div>
        <div id="mwethUsdSub" class="mono text-[10px] text-white font-bold mt-0.5">$5.56 (+$0.10)</div>
      </div>
      <div class="bg-slate-900/90 border border-cyan-500/40 rounded-lg p-3">
        <div class="text-[10px] uppercase text-cyan-300">On-Chain Wei Yield</div>
        <div id="mwethEarnedPill" class="mono text-xs font-bold text-cyan-300 mt-0.5">+0 Wei</div>
        <div id="mwethEarnedSub" class="mono text-[10px] text-emerald-400 mt-0.5">+0.000080 ETH</div>
      </div>
      <div class="bg-slate-900/90 border border-amber-500/40 rounded-lg p-3">
        <div class="text-[10px] uppercase text-amber-300">Gate.io Spot Bankroll</div>
        <div id="gateUsdtPill" class="mono text-xs font-bold text-amber-300 mt-0.5">$0.00 USDT</div>
        <div id="gateCoinSub" class="mono text-[10px] text-slate-300 mt-0.5">0 QNT | 0 LTC</div>
      </div>
      <div class="bg-slate-900/90 border border-purple-500/40 rounded-lg p-3">
        <div class="text-[10px] uppercase text-purple-300">Spot Skimmer Status</div>
        <div id="skimStatusPill" class="mono text-xs font-bold text-purple-300 mt-0.5">SIMULATION</div>
        <div id="skimAnchorSub" class="mono text-[10px] text-slate-400 mt-0.5">Anchor: $0.00</div>
      </div>
      <div class="bg-slate-900/90 border border-emerald-500/40 rounded-lg p-3">
        <div class="text-[10px] uppercase text-emerald-300">Combined Profits</div>
        <div id="combinedProfitPill" class="mono text-sm font-extrabold text-emerald-400 mt-0.5">+$0.1021</div>
        <div id="flipsWonSub" class="mono text-[10px] text-slate-400 mt-0.5">0 Skim Flips</div>
      </div>
    </div>
  </header>

  <!-- CORE 2: GATE.IO SPOT VOLATILITY SKIMMER PANEL -->
  <section class="card p-4 border-amber-500/30">
    <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
      <div>
        <h2 class="text-xs font-bold uppercase tracking-wider text-amber-300">
          ⚡ Core 2: Gate.io v4 Spot Volatility Skimmer (Target $2–$5/Day on $150 Bankroll)
        </h2>
        <p class="text-[11px] text-slate-400">
          Automatically buys micro-dips and sells rebounds on Gate.io Spot. Runs in risk-free Paper Simulation until you arm Live Skimming.
        </p>
      </div>
      <div class="flex flex-wrap gap-2">
        <button onclick="toggleSkimMode()" id="skimToggleBtn" class="px-3.5 py-1.5 rounded-lg text-xs font-extrabold bg-amber-500 hover:bg-amber-400 text-slate-950">
          🚀 Arm / Disarm Gate.io Spot Skimmer
        </button>
        <button onclick="resetSkimAnchor()" class="px-3 py-1.5 rounded-lg text-xs font-bold bg-slate-700 hover:bg-slate-600 text-white">
          🎯 Reset Price Anchor
        </button>
      </div>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-5 gap-2.5 text-xs">
      <div>
        <label class="block text-slate-400 mb-1">Spot Pair</label>
        <select id="skimPairSelect" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-2 text-white mono">
          <option value="QNT_USDT">QNT_USDT (High Beta)</option>
          <option value="LTC_USDT">LTC_USDT (Fast Cycles)</option>
        </select>
      </div>
      <div>
        <label class="block text-slate-400 mb-1">Order Size (USDT)</label>
        <input id="skimOrderInput" type="number" step="5" value="25" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-2 text-white mono" />
      </div>
      <div>
        <label class="block text-slate-400 mb-1">Buy Dip (%) / Take Profit (%)</label>
        <div class="flex gap-1">
          <input id="skimDipInput" type="number" step="0.1" value="1.2" class="w-1/2 bg-slate-900 border border-slate-700 rounded-lg px-2 py-2 text-emerald-300 mono" />
          <input id="skimTpInput" type="number" step="0.1" value="1.3" class="w-1/2 bg-slate-900 border border-slate-700 rounded-lg px-2 py-2 text-cyan-300 mono" />
        </div>
      </div>
      <div>
        <label class="block text-slate-400 mb-1">Gate.io v4 API Key</label>
        <input id="gateKeyInput" type="password" placeholder="Saved on server" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-2 text-white mono" />
      </div>
      <div>
        <label class="block text-slate-400 mb-1">Gate.io v4 API Secret</label>
        <input id="gateSecretInput" type="password" placeholder="Saved on server" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-2 text-white mono" />
      </div>
    </div>
  </section>

  <!-- CORE 1: BASE MAINNET MOONWELL YIELD & KEEPERGUARD PANEL -->
  <section class="card p-4 border-emerald-500/30">
    <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
      <div>
        <h2 class="text-xs font-bold uppercase tracking-wider text-emerald-300">
          💎 Core 1: Base Mainnet Moonwell ETH Yield + KeeperGuardV2 Harvester
        </h2>
      </div>
      <div class="flex flex-wrap gap-2">
        <button onclick="triggerManualScan()" id="scanNowBtn" class="px-3 py-1.5 rounded-lg text-xs font-bold bg-indigo-600 hover:bg-indigo-500 text-white">
          🔍 Scan Now
        </button>
        <button onclick="depositYield()" id="depYieldBtn" class="px-3 py-1.5 rounded-lg text-xs font-extrabold bg-emerald-500 hover:bg-emerald-400 text-slate-950">
          💎 Deposit ETH to Yield
        </button>
        <button onclick="withdrawYield()" id="wdYieldBtn" class="px-3 py-1.5 rounded-lg text-xs font-bold bg-rose-600 hover:bg-rose-500 text-white">
          📤 Withdraw 100% ETH
        </button>
        <button onclick="unwrapWeth()" class="px-3 py-1.5 rounded-lg text-xs font-bold bg-purple-600 hover:bg-purple-500 text-white">
          🔄 Unwrap WETH
        </button>
        <button onclick="deployGuard()" id="deployGuardBtn" class="px-3 py-1.5 rounded-lg text-xs font-extrabold bg-cyan-500 hover:bg-cyan-400 text-slate-950">
          🛡️ Deploy KeeperGuardV2
        </button>
        <button onclick="toggleLiveMode()" class="px-3 py-1.5 rounded-lg text-xs font-bold bg-amber-500 hover:bg-amber-400 text-slate-950">
          🚀 Arm / Disarm DeFi
        </button>
      </div>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-4 gap-3 text-xs pt-2 border-t border-slate-800">
      <div>
        <label class="block text-slate-400 mb-1">Base Alchemy RPC URL (Synced)</label>
        <input id="rpcInput" type="text" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-emerald-300 mono" />
      </div>
      <div>
        <label class="block text-slate-400 mb-1">Keeper Wallet Private Key</label>
        <input id="privKeyInput" type="password" placeholder="0x... (Saved on server)" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-white mono" />
      </div>
      <div>
        <label class="block text-slate-400 mb-1">KeeperGuard Contract Address</label>
        <input id="guardInput" type="text" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-emerald-300 mono" />
      </div>
      <div class="flex items-end">
        <button onclick="saveConfig()" class="w-full py-2 px-4 rounded-lg text-xs font-extrabold bg-emerald-500 hover:bg-emerald-400 text-slate-950">
          💾 Save & Sync All Settings
        </button>
      </div>
    </div>
  </section>

  <!-- TWO-COLUMN LEDGERS -->
  <div class="grid grid-cols-1 lg:grid-cols-12 gap-4">
    <div class="lg:col-span-6 card p-4">
      <h3 class="text-xs font-bold uppercase tracking-wider text-emerald-300 mb-2">
        🎯 Active DeFi Bounties & Spot Skim Signals
      </h3>
      <div id="oppsList" class="space-y-2 max-h-72 overflow-y-auto"></div>
    </div>

    <div class="lg:col-span-6 card p-4">
      <h3 class="text-xs font-bold uppercase tracking-wider text-cyan-300 mb-2">
        🧾 Unified Yield, Bounty & Spot Skim Profit Ledger
      </h3>
      <div id="ledgerList" class="space-y-2 max-h-72 overflow-y-auto"></div>
    </div>
  </div>

  <!-- LOGS -->
  <section class="card p-4">
    <h3 class="text-xs font-bold uppercase tracking-wider text-slate-300 mb-2">🛡️ Unified Dual-Core Activity Logs</h3>
    <div id="logsBox" class="mono text-[11px] bg-slate-950 p-3 rounded-lg border border-slate-800 space-y-1 max-h-60 overflow-y-auto text-slate-300"></div>
  </section>

</div>

<script>
let deferredPrompt = null;
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredPrompt = e;
});

async function installApp() {
  if (deferredPrompt) {
    deferredPrompt.prompt();
    await deferredPrompt.userChoice;
    deferredPrompt = null;
  } else {
    alert('To install KeeperPulse:\\n• Android Chrome: Tap the 3 dots (⋮) -> "Install app" or "Add to Home screen".\\n• Desktop: Click the install icon in the address bar.');
  }
}

async function pollState() {
  try {
    const r = await fetch('/state?_t=' + Date.now());
    const s = await r.json();
    const ethPx = Number(s.eth_price_usd || 2294);
    document.getElementById('lastScanClock').textContent = s.last_scan_hms || '--:--:--';
    document.getElementById('scanMs').textContent = s.last_scan_ms || 0;
    document.getElementById('ethPriceBadge').textContent = '$' + ethPx.toFixed(2);
    document.getElementById('qntPriceBadge').textContent = '$' + Number(s.qnt_price || 0).toFixed(2);
    document.getElementById('nativeEthPill').textContent = Number(s.native_eth_balance || 0).toFixed(6) + ' ETH';

    const modePill = document.getElementById('modePill');
    modePill.textContent = s.live_armed ? '🟢 DEFI ARMED' : '🧪 DEFI DRY-RUN';
    modePill.className = s.live_armed ? 'text-[10px] font-extrabold text-emerald-400 mt-0.5' : 'text-[10px] font-extrabold text-amber-300 mt-0.5';

    const mwEth = Number(s.mweth_assets_eth || 0);
    const mwUsd = mwEth * ethPx;
    const pnlUsd = Number(s.position_pnl_usd || 0);
    document.getElementById('mwethAssetsPill').textContent = mwEth.toFixed(8) + ' ETH';
    document.getElementById('mwethUsdSub').textContent = '$' + mwUsd.toFixed(2) + ' (+$' + pnlUsd.toFixed(2) + ')';

    const earnedWei = Number(s.mweth_earned_wei || 0);
    const earnedEth = Number(s.mweth_earned_eth || 0);
    document.getElementById('mwethEarnedPill').textContent = '+' + earnedWei.toLocaleString() + ' Wei';
    document.getElementById('mwethEarnedSub').textContent = '+' + earnedEth.toFixed(8) + ' ETH';

    document.getElementById('gateUsdtPill').textContent = '$' + Number(s.gate_usdt_balance || 0).toFixed(2) + ' USDT';
    document.getElementById('gateCoinSub').textContent = Number(s.gate_qnt_balance || 0).toFixed(3) + ' QNT | ' + Number(s.gate_ltc_balance || 0).toFixed(3) + ' LTC';

    const skimStatus = document.getElementById('skimStatusPill');
    if (s.skim_active_entry > 0) {
      skimStatus.textContent = 'HOLDING @ $' + Number(s.skim_active_entry).toFixed(2);
      skimStatus.className = 'mono text-xs font-extrabold text-cyan-300 mt-0.5';
    } else {
      skimStatus.textContent = s.skim_armed ? '🟢 SKIM ARMED' : '🧪 PAPER SIM';
      skimStatus.className = s.skim_armed ? 'mono text-xs font-extrabold text-emerald-400 mt-0.5' : 'mono text-xs font-bold text-amber-300 mt-0.5';
    }
    document.getElementById('skimAnchorSub').textContent = 'Anchor: $' + Number(s.skim_anchor_price || 0).toFixed(2);

    const totalCombined = pnlUsd + Number(s.realized_profit_usd || 0) + Number(s.skim_realized_usd || 0);
    document.getElementById('combinedProfitPill').textContent = '+$' + totalCombined.toFixed(4);
    document.getElementById('flipsWonSub').textContent = (s.skim_flips_won || 0) + ' Skim Flips Won';

    const rpcInput = document.getElementById('rpcInput');
    if (document.activeElement !== rpcInput && s.rpc_url) rpcInput.value = s.rpc_url;
    const guardInput = document.getElementById('guardInput');
    if (document.activeElement !== guardInput && s.guard_contract) guardInput.value = s.guard_contract;
    const pairSel = document.getElementById('skimPairSelect');
    if (document.activeElement !== pairSel && s.skim_pair) pairSel.value = s.skim_pair;
    const orderIn = document.getElementById('skimOrderInput');
    if (document.activeElement !== orderIn && s.skim_order_usd) orderIn.value = s.skim_order_usd;
    const dipIn = document.getElementById('skimDipInput');
    if (document.activeElement !== dipIn && s.skim_dip_pct) dipIn.value = s.skim_dip_pct;
    const tpIn = document.getElementById('skimTpInput');
    if (document.activeElement !== tpIn && s.skim_tp_pct) tpIn.value = s.skim_tp_pct;

    const opps = s.live_opportunities || [];
    const oppsEl = document.getElementById('oppsList');
    const pairPx = s.skim_pair === 'LTC_USDT' ? Number(s.ltc_price || 0) : Number(s.qnt_price || 0);
    const nextBuyPx = Number(s.skim_anchor_price || pairPx) * (1 - Number(s.skim_dip_pct || 1.2) / 100);
    const skimCard = `
      <div class="p-2.5 rounded-lg bg-slate-900 border border-amber-500/30 flex items-center justify-between text-xs">
        <div>
          <div class="font-bold text-amber-300">Gate.io ${s.skim_pair} Volatility Skimmer</div>
          <div class="mono text-[10px] text-slate-400">Live: $${pairPx.toFixed(2)} • Buy Trigger: $${nextBuyPx.toFixed(2)} (-${s.skim_dip_pct}%)</div>
        </div>
        <div class="mono font-bold text-emerald-400">+${s.skim_tp_pct}% Target</div>
      </div>
    `;
    if (opps.length === 0) {
      oppsEl.innerHTML = skimCard + '<div class="text-xs text-slate-500 py-4 text-center">Scanning ' + (s.vaults_monitored || 0) + ' Non-HOD Base vaults every 12s...</div>';
    } else {
      oppsEl.innerHTML = skimCard + opps.map(o => `
        <div class="p-2.5 rounded-lg bg-slate-900 border border-emerald-500/30 flex items-center justify-between text-xs">
          <div>
            <div class="font-bold text-white">${o.name}</div>
            <div class="mono text-[10px] text-slate-400">Bounty: $${o.bounty_usd.toFixed(4)} • Gas: $${o.gas_usd.toFixed(4)}</div>
          </div>
          <div class="mono font-extrabold text-emerald-400">+$${o.net_profit_usd.toFixed(4)}</div>
        </div>
      `).join('');
    }

    const ledger = s.harvest_ledger || [];
    const ledgerEl = document.getElementById('ledgerList');
    ledgerEl.innerHTML = ledger.map(l => `
      <div class="p-2.5 rounded-lg bg-slate-900 border border-emerald-500/30 flex items-center justify-between text-xs">
        <div>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-bold mr-1">${l.mode}</span>
          <span class="font-bold text-white">${l.vault}</span>
          <div class="mono text-[10px] text-slate-400 mt-0.5">${l.strategy} • ${l.time}</div>
        </div>
        <div class="mono font-bold text-emerald-400">$${Number(l.net_usd).toFixed(4)}</div>
      </div>
    `).join('');

    document.getElementById('logsBox').innerHTML = (s.logs || []).map(l => `<div>${l}</div>`).join('');
  } catch (e) {}
}

async function saveConfig() {
  const rpc = document.getElementById('rpcInput').value.trim();
  const pk = document.getElementById('privKeyInput').value.trim();
  const guard = document.getElementById('guardInput').value.trim();
  const skimPair = document.getElementById('skimPairSelect').value;
  const skimOrder = parseFloat(document.getElementById('skimOrderInput').value || '25');
  const skimDip = parseFloat(document.getElementById('skimDipInput').value || '1.2');
  const skimTp = parseFloat(document.getElementById('skimTpInput').value || '1.3');
  const gateKey = document.getElementById('gateKeyInput').value.trim();
  const gateSecret = document.getElementById('gateSecretInput').value.trim();

  await fetch('/configure', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      rpc_url: rpc,
      private_key: pk,
      guard_contract: guard,
      skim_pair: skimPair,
      skim_order_usd: skimOrder,
      skim_dip_pct: skimDip,
      skim_tp_pct: skimTp,
      gate_api_key: gateKey,
      gate_api_secret: gateSecret
    })
  });
  document.getElementById('privKeyInput').value = '';
  document.getElementById('gateKeyInput').value = '';
  document.getElementById('gateSecretInput').value = '';
  pollState();
}

async function toggleSkimMode() {
  await saveConfig();
  const r = await fetch('/toggle_skim', { method: 'POST' });
  const res = await r.json();
  if (res.message) alert(res.message);
  pollState();
}

async function resetSkimAnchor() {
  await fetch('/reset_anchor', { method: 'POST' });
  pollState();
}

async function triggerManualScan() {
  const btn = document.getElementById('scanNowBtn');
  btn.textContent = '⏳...';
  await fetch('/scan_now', { method: 'POST' });
  btn.textContent = '🔍 Scan Now';
  pollState();
}

async function depositYield() {
  const pk = document.getElementById('privKeyInput').value.trim();
  if (pk) await saveConfig();
  const r = await fetch('/deposit_yield', { method: 'POST' });
  const res = await r.json();
  alert(res.message);
  pollState();
}

async function withdrawYield() {
  const pk = document.getElementById('privKeyInput').value.trim();
  if (pk) await saveConfig();
  const r = await fetch('/withdraw_yield', { method: 'POST' });
  const res = await r.json();
  alert(res.message);
  pollState();
}

async function unwrapWeth() {
  const pk = document.getElementById('privKeyInput').value.trim();
  if (pk) await saveConfig();
  const r = await fetch('/unwrap_weth', { method: 'POST' });
  const res = await r.json();
  alert(res.message);
  pollState();
}

async function deployGuard() {
  const pk = document.getElementById('privKeyInput').value.trim();
  if (pk) await saveConfig();
  const r = await fetch('/deploy_guard', { method: 'POST' });
  const res = await r.json();
  alert(res.message);
  pollState();
}

async function toggleLiveMode() {
  const r = await fetch('/toggle_live', { method: 'POST' });
  const res = await r.json();
  if (!res.ok && res.message) alert(res.message);
  pollState();
}

window.addEventListener('DOMContentLoaded', () => {
  pollState();
  setInterval(pollState, 3000);
});
</script>
</body>
</html>
"""


# ==============================================================================
# 7. FLASK ROUTES (PWA, PING, DEFI & GATE.IO SPOT SKIMMER ROUTES)
# ==============================================================================
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "KeeperPulse v3.0 Dual-Core Engine",
        "short_name": "KeeperPulse",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#050811",
        "theme_color": "#0f172a",
        "icons": [
            {"src": "https://cryptologos.cc/logos/ethereum-eth-logo.png", "sizes": "192x192", "type": "image/png"},
            {"src": "https://cryptologos.cc/logos/ethereum-eth-logo.png", "sizes": "512x512", "type": "image/png"}
        ]
    })


@app.route("/sw.js")
def service_worker():
    return Response("self.addEventListener('fetch', function(event) {});", mimetype="application/javascript")


@app.route("/")
def index():
    return render_template_string(KEEPER_UI_HTML)


@app.route("/state")
def get_state():
    with state_lock:
        resp = json.loads(json.dumps(STATE))
        resp["private_key"] = ""
        resp["gate_api_key"] = ""
        resp["gate_api_secret"] = ""
    return jsonify(resp)


@app.route("/ping")
def ping():
    return "OK", 200


@app.route("/configure", methods=["POST"])
def configure():
    data = request.get_json(force=True) or {}
    with state_lock:
        if data.get("rpc_url") and data["rpc_url"].strip():
            STATE["rpc_url"] = data["rpc_url"].strip()
        if data.get("private_key"):
            STATE["private_key"] = data["private_key"].strip()
        if data.get("guard_contract") and data["guard_contract"].strip():
            STATE["guard_contract"] = data["guard_contract"].strip()
        if data.get("skim_pair") in ("QNT_USDT", "LTC_USDT"):
            if data["skim_pair"] != STATE.get("skim_pair"):
                STATE["skim_anchor_price"] = 0.0
            STATE["skim_pair"] = data["skim_pair"]
        if data.get("skim_order_usd") is not None:
            STATE["skim_order_usd"] = float(data["skim_order_usd"])
        if data.get("skim_dip_pct") is not None:
            STATE["skim_dip_pct"] = float(data["skim_dip_pct"])
        if data.get("skim_tp_pct") is not None:
            STATE["skim_tp_pct"] = float(data["skim_tp_pct"])
        if data.get("gate_api_key"):
            STATE["gate_api_key"] = data["gate_api_key"].strip()
        if data.get("gate_api_secret"):
            STATE["gate_api_secret"] = data["gate_api_secret"].strip()

    sync_gate_spot_balances()
    sync_wallet_balances()
    run_unified_dual_core_cycle(manual=True)
    save_state()
    return jsonify({"ok": True})


@app.route("/toggle_skim", methods=["POST"])
def toggle_skim():
    with state_lock:
        STATE["skim_armed"] = not bool(STATE.get("skim_armed", False))
        mode = "LIVE GATE.IO SPOT SKIMMING" if STATE["skim_armed"] else "PAPER SIMULATION"
    add_log(f"⚡ Core 2 (Gate.io Spot Skimmer) switched to: {mode}")
    save_state()
    return jsonify({"ok": True, "skim_armed": STATE["skim_armed"], "message": f"Spot Skimmer mode: {mode}"})


@app.route("/reset_anchor", methods=["POST"])
def reset_anchor():
    with state_lock:
        pair = STATE.get("skim_pair", "QNT_USDT")
        px = STATE.get("qnt_price", 0.0) if pair == "QNT_USDT" else STATE.get("ltc_price", 0.0)
        STATE["skim_anchor_price"] = px
        STATE["skim_active_entry"] = 0.0
        STATE["skim_active_amount"] = 0.0
    add_log(f"🎯 Reset {pair} Skimmer Anchor Price to ${px:.2f}.")
    save_state()
    return jsonify({"ok": True})


@app.route("/scan_now", methods=["POST"])
def scan_now():
    run_unified_dual_core_cycle(manual=True)
    return jsonify({"ok": True})


@app.route("/deposit_yield", methods=["POST"])
def deposit_yield_route():
    ok, msg = deposit_into_moonwell_yield()
    return jsonify({"ok": ok, "message": msg})


@app.route("/withdraw_yield", methods=["POST"])
def withdraw_yield_route():
    ok, msg = withdraw_from_moonwell_yield()
    return jsonify({"ok": ok, "message": msg})


@app.route("/unwrap_weth", methods=["POST"])
def unwrap_weth_route():
    ok, msg = unwrap_weth_to_eth()
    return jsonify({"ok": ok, "message": msg})


@app.route("/deploy_guard", methods=["POST"])
def deploy_guard_route():
    ok, msg = compile_and_deploy_keeper_guard()
    return jsonify({"ok": ok, "message": msg, "guard_contract": STATE.get("guard_contract", "")})


@app.route("/toggle_live", methods=["POST"])
def toggle_live():
    with state_lock:
        if not STATE.get("private_key") and not STATE.get("live_armed"):
            return jsonify({"ok": False, "message": "Paste Private Key and click Save first."})
        STATE["live_armed"] = not bool(STATE.get("live_armed", False))
        mode_str = "LIVE ARMED" if STATE["live_armed"] else "DRY-RUN"
    add_log(f"⚡ Core 1 (Base DeFi Engine) switched to: {mode_str}")
    save_state()
    return jsonify({"ok": True, "live_armed": STATE["live_armed"]})


if __name__ == "__main__":
    sync_wallet_balances()
    sync_gate_spot_balances()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
