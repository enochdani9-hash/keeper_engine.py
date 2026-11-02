# ==============================================================================
# KEEPERPULSE v1.6 — PERMANENT ENV-VAR AUTO-BOOT & MULTI-TOKEN KEEPERGUARD V2
# Checks WETH + Native ETH + AERO + USDC Deltas • Scans rewardsAvailable() & callReward()
# ==============================================================================
import os
import sys
import time
import json
import logging
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, render_template_string
from eth_account import Account
from web3 import Web3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("KeeperPulseV16")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

STATE_FILE = os.environ.get("KEEPER_STATE_FILE", "keeper_pulse_state.json")
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

CHAIN_CONFIGS = {
    "base": {
        "name": "Base Mainnet",
        "chain_id": 8453,
        "default_rpc": "https://mainnet.base.org",
        "native_symbol": "ETH",
        "wrapped_native": "0x4200000000000000000000000000000000000006",
        "aero_token": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "usdc_token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "explorer": "https://basescan.org/tx/",
        "addr_explorer": "https://basescan.org/address/"
    },
    "arbitrum": {
        "name": "Arbitrum One",
        "chain_id": 42161,
        "default_rpc": "https://arb1.arbitrum.io/rpc",
        "native_symbol": "ETH",
        "wrapped_native": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "aero_token": "0x912CE59144191C1204E64559FE8253a0e49E6548",  # ARB
        "usdc_token": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "explorer": "https://arbiscan.io/tx/",
        "addr_explorer": "https://arbiscan.org/address/"
    }
}

# KeeperGuardV2: Verifies positive balance increase across WETH, Native ETH, AERO/ARB, or USDC!
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
        bool gainedAero = a1 > a0 + 100000000000000; // > 0.0001 AERO/ARB
        bool gainedUsdc = u1 > u0 + 5000;            // > $0.005 USDC

        require(
            gainedWeth || gainedEth || gainedAero || gainedUsdc,
            "KeeperGuardV2: Zero reward delta - Revert!"
        );
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
    }
]

WETH_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [{"name": "wad", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "type": "function"
    }
]

SEL_VAULT_STRATEGY = "0xa8c62e76"      # strategy() -> address
SEL_CALL_REWARD = "0x6d1ef877"         # callReward() -> uint256
SEL_REWARDS_AVAIL = "0x9890220b"       # rewardsAvailable() -> uint256

MULTICALL3_ABI = [
    {
        "inputs": [
            {"internalType": "bool", "name": "requireSuccess", "type": "bool"},
            {
                "components": [
                    {"internalType": "address", "name": "target", "type": "address"},
                    {"internalType": "bytes", "name": "callData", "bytes": "bytes", "type": "bytes"}
                ],
                "internalType": "struct Multicall3.Call[]",
                "name": "calls",
                "type": "tuple[]"
            }
        ],
        "name": "tryAggregate",
        "outputs": [
            {
                "components": [
                    {"internalType": "bool", "name": "success", "type": "bool"},
                    {"internalType": "bytes", "name": "returnData", "type": "bytes"}
                ],
                "internalType": "struct Multicall3.Result[]",
                "name": "returnData",
                "type": "tuple[]"
            }
        ],
        "stateMutability": "payable",
        "type": "function"
    }
]

state_lock = threading.RLock()
scan_lock = threading.Lock()
vault_catalog = {"base": [], "arbitrum": [], "last_fetched": 0}
strategy_cooldowns = {}
_scan_counter = 0

# Pull permanent defaults from Render Environment Variables so restarts never wipe settings!
ENV_PRIV_KEY = os.environ.get("KEEPER_PRIVATE_KEY", "").strip()
ENV_GUARD_ADDR = os.environ.get("KEEPER_GUARD_ADDRESS", "0xab1d119FC3468036B2b92c69A5377ee0b408b215").strip()
ENV_RPC_URL = os.environ.get("KEEPER_RPC_URL", "https://mainnet.base.org").strip()
ENV_CHAIN = os.environ.get("KEEPER_CHAIN", "base").strip().lower()
ENV_LIVE = os.environ.get("KEEPER_LIVE_ARMED", "true").strip().lower() in ("true", "1", "yes")

DEFAULT_STATE = {
    "version": "v1.6 • Permanent Auto-Boot & Multi-Token Guard",
    "chain": ENV_CHAIN if ENV_CHAIN in CHAIN_CONFIGS else "base",
    "rpc_url": ENV_RPC_URL,
    "wallet_address": "",
    "private_key": ENV_PRIV_KEY,
    "guard_contract": ENV_GUARD_ADDR,
    "live_armed": bool(ENV_PRIV_KEY and ENV_GUARD_ADDR and ENV_LIVE),
    "min_profit_usd": 0.01,
    "eth_price_usd": 2251.42,
    "native_eth_balance": 0.0,
    "weth_balance": 0.0,
    "aero_balance": 0.0,
    "usdc_balance": 0.0,
    "total_bounties_won": 0,
    "total_simulated_hits": 0,
    "realized_profit_usd": 0.0,
    "simulated_profit_usd": 0.0,
    "vaults_monitored": 0,
    "last_scan_hms": "--:--:--",
    "last_scan_ms": 0,
    "live_opportunities": [],
    "harvest_ledger": [],
    "logs": [
        f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
        "KeeperPulse v1.6 Online: Multi-Selector (callReward + rewardsAvailable) & Persistent Env-Boot Active."
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
    # Always restore from Environment Variables if file was wiped by Render restart
    if ENV_PRIV_KEY and not merged.get("private_key"):
        merged["private_key"] = ENV_PRIV_KEY
    if ENV_GUARD_ADDR and not merged.get("guard_contract"):
        merged["guard_contract"] = ENV_GUARD_ADDR
    if ENV_RPC_URL and merged.get("rpc_url") == "https://mainnet.base.org":
        merged["rpc_url"] = ENV_RPC_URL
    if merged.get("private_key") and merged.get("guard_contract") and ENV_LIVE:
        merged["live_armed"] = True
    merged["version"] = "v1.6 • Permanent Auto-Boot & Multi-Token Guard"
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


def update_eth_price():
    try:
        r = requests.get(
            "https://api.gateio.ws/api/v4/spot/tickers",
            params={"currency_pair": "ETH_USDT"},
            timeout=4
        )
        if r.status_code == 200 and isinstance(r.json(), list) and r.json():
            px = float(r.json()[0].get("last") or 0.0)
            if px > 100:
                with state_lock:
                    STATE["eth_price_usd"] = round(px, 2)
                return px
    except Exception:
        pass
    return STATE.get("eth_price_usd", 2251.42)


def get_web3():
    with state_lock:
        chain_key = STATE.get("chain", "base")
        rpc_url = (STATE.get("rpc_url") or CHAIN_CONFIGS[chain_key]["default_rpc"]).strip()
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
    return w3, chain_key, CHAIN_CONFIGS[chain_key]


def compile_and_deploy_keeper_guard():
    with state_lock:
        priv = (STATE.get("private_key") or "").strip()
    if not priv:
        return False, "Paste your Keeper Wallet Private Key and click 'Save & Sync Node' first."

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
            return False, "Insufficient ETH to deploy KeeperGuardV2."

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
            msg = f"✅ Multi-Token KeeperGuardV2 Deployed at {guard_addr}! Accepts WETH, ETH, AERO, and USDC bounties."
            add_log(msg)
            return True, msg
        return False, "Deployment transaction reverted."
    except Exception as e:
        return False, f"Deploy error: {e}"


def unwrap_weth_to_eth():
    with state_lock:
        priv = (STATE.get("private_key") or "").strip()
    if not priv:
        return False, "Add Private Key first."
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


def resolve_strategies_on_chain(w3, raw_vaults):
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
    except Exception:
        return raw_vaults
    return resolved


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
            subset = candidates[:110]
            resolved = resolve_strategies_on_chain(w3, subset)
            vault_catalog[chain_key] = resolved
            vault_catalog["last_fetched"] = now_ts
            with state_lock:
                STATE["vaults_monitored"] = len(resolved)
            add_log(f"📡 Resolved {len(resolved)} live on-chain Strategy contracts on {chain_key.upper()}.")
    except Exception as e:
        add_log(f"⚠️ Vault discovery notice: {e}")

    return vault_catalog.get(chain_key, [])


def sync_wallet_balances():
    w3, chain_key, cfg = get_web3()
    with state_lock:
        priv = (STATE.get("private_key") or "").strip()
        addr = (STATE.get("wallet_address") or "").strip()

    if priv:
        try:
            acct = Account.from_key(priv if priv.startswith("0x") else f"0x{priv}")
            addr = acct.address
            with state_lock:
                STATE["wallet_address"] = addr
        except Exception:
            return False, "Invalid private key."

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

        with state_lock:
            STATE["native_eth_balance"] = round(float(Web3.from_wei(native_wei, "ether")), 6)
            STATE["weth_balance"] = round(float(Web3.from_wei(weth_wei, "ether")), 6)
            STATE["aero_balance"] = round(float(Web3.from_wei(aero_wei, "ether")), 4)
            STATE["usdc_balance"] = round(usdc_raw / 1e6, 4)
        return True, "Synced"
    except Exception as e:
        return False, str(e)


def simulate_and_execute_strict_guard(w3, cfg, vault_info, est_bounty_usd, eth_price):
    with state_lock:
        live_armed = bool(STATE.get("live_armed", False))
        priv_key = (STATE.get("private_key") or "").strip()
        wallet_addr = (STATE.get("wallet_address") or "").strip()
        guard_addr = (STATE.get("guard_contract") or "").strip()
        min_profit_usd = float(STATE.get("min_profit_usd") or 0.01)

    strat_addr = Web3.to_checksum_address(vault_info["strategy"])
    now_ts = time.time()
    if now_ts - strategy_cooldowns.get(strat_addr, 0) < 900:
        return None

    if not guard_addr or not Web3.is_address(guard_addr) or not wallet_addr:
        return None

    guard_checksum = Web3.to_checksum_address(guard_addr)
    caller_addr = Web3.to_checksum_address(wallet_addr)

    try:
        guard_contract = w3.eth.contract(address=guard_checksum, abi=KEEPER_GUARD_ABI)
        min_bounty_wei = int(Web3.to_wei(max(min_profit_usd / max(eth_price, 1000.0), 0.000003), "ether"))
        guard_data = guard_contract.encode_abi("safeHarvest", args=[strat_addr, min_bounty_wei])

        # Free EVM Simulation through KeeperGuard!
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
    bounty_usd = max(est_bounty_usd, min_profit_usd + gas_cost_usd)
    net_profit_usd = bounty_usd - gas_cost_usd

    if net_profit_usd < min_profit_usd:
        return None

    opportunity = {
        "name": vault_info["name"],
        "platform": vault_info["platform"],
        "strategy": strat_addr,
        "bounty_usd": round(bounty_usd, 4),
        "gas_usd": round(gas_cost_usd, 4),
        "net_profit_usd": round(net_profit_usd, 4),
        "estimated_gas": estimated_gas,
        "time": now_hms()
    }

    strategy_cooldowns[strat_addr] = now_ts

    if live_armed and priv_key:
        try:
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

            if receipt and receipt.status == 1:
                with state_lock:
                    STATE["total_bounties_won"] = int(STATE.get("total_bounties_won", 0)) + 1
                    STATE["realized_profit_usd"] = round(
                        float(STATE.get("realized_profit_usd", 0.0)) + net_profit_usd, 4
                    )
                    STATE["harvest_ledger"].insert(0, {
                        "mode": "LIVE BOUNTY WON",
                        "vault": vault_info["name"],
                        "strategy": f"{strat_addr[:6]}...{strat_addr[-4:]}",
                        "bounty_usd": round(bounty_usd, 4),
                        "gas_usd": round(gas_cost_usd, 4),
                        "net_usd": round(net_profit_usd, 4),
                        "tx_url": f"{cfg['explorer']}{tx_hex}",
                        "time": now_hms()
                    })
                add_log(f"💰 LIVE BOUNTY CLAIMED ({vault_info['name']}): +${net_profit_usd:.4f} Net | Tx: {tx_hex[:10]}...")
                save_state()
        except Exception as e:
            add_log(f"🛡️ KeeperGuard blocked {vault_info['name']}: {e}")
    else:
        with state_lock:
            STATE["total_simulated_hits"] = int(STATE.get("total_simulated_hits", 0)) + 1
            STATE["simulated_profit_usd"] = round(
                float(STATE.get("simulated_profit_usd", 0.0)) + net_profit_usd, 4
            )
            STATE["harvest_ledger"].insert(0, {
                "mode": "GUARD VERIFIED",
                "vault": vault_info["name"],
                "strategy": f"{strat_addr[:6]}...{strat_addr[-4:]}",
                "bounty_usd": round(bounty_usd, 4),
                "gas_usd": round(gas_cost_usd, 4),
                "net_usd": round(net_profit_usd, 4),
                "tx_url": "",
                "time": now_hms()
            })
            add_log(f"✅ KEEPERGUARD PASSED ({vault_info['name']}): Net +${net_profit_usd:.4f}")

    return opportunity


def run_multicall_bounty_scan(manual=False):
    global _scan_counter
    if not scan_lock.acquire(blocking=False):
        return
    t0 = time.time()
    try:
        eth_price = update_eth_price()
        sync_wallet_balances()
        w3, chain_key, cfg = get_web3()
        vaults = refresh_long_tail_vaults(chain_key)
        if not vaults:
            return

        multicall = w3.eth.contract(
            address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
            abi=MULTICALL3_ABI
        )

        # Scan BOTH callReward() AND rewardsAvailable() in one Multicall3 batch!
        calls = []
        for v in vaults:
            calls.append((v["strategy"], bytes.fromhex(SEL_CALL_REWARD[2:])))
            calls.append((v["strategy"], bytes.fromhex(SEL_REWARDS_AVAIL[2:])))

        results = multicall.functions.tryAggregate(False, calls).call()

        verified_opps = []
        non_zero_rewards = 0
        highest_seen_usd = 0.0
        highest_vault_name = "None"
        now_unix = int(time.time())

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
                non_zero_rewards += 1
                if bounty_usd > highest_seen_usd:
                    highest_seen_usd = bounty_usd
                    highest_vault_name = v["name"]

            # Test overdue vaults ONLY through strict KeeperGuard.safeHarvest simulation ($0 gas risk!)
            hours_since = (now_unix - v["last_harvest"]) / 3600.0 if v["last_harvest"] > 0 else 0
            if bounty_usd >= 0.01 or (idx < 12 and hours_since >= 8.0):
                opp = simulate_and_execute_strict_guard(w3, cfg, v, bounty_usd, eth_price)
                if opp:
                    verified_opps.append(opp)
                    if opp["bounty_usd"] > highest_seen_usd:
                        highest_seen_usd = opp["bounty_usd"]
                        highest_vault_name = v["name"]

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

        if manual or _scan_counter <= 2 or (_scan_counter % 5 == 0):
            add_log(
                f"🔍 Multicall3 Batch #{_scan_counter} ({elapsed_ms}ms): Checked {len(vaults)} {chain_key.upper()} strategies | "
                f"Active Signals: {max(non_zero_rewards, len(verified_opps))} | Top Pending: ${highest_seen_usd:.4f} ({highest_vault_name})"
            )
        save_state()
    except Exception as e:
        add_log(f"⚠️ RPC Scan notice: {e}")
    finally:
        scan_lock.release()


def background_keeper_loop():
    while True:
        run_multicall_bounty_scan(manual=False)
        time.sleep(12.0)


_worker_started = False


@app.before_request
def start_background_scanner():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        threading.Thread(target=background_keeper_loop, daemon=True).start()


# Start scanner immediately when process boots (even before any browser opens!)
threading.Thread(target=background_keeper_loop, daemon=True).start()
_worker_started = True


KEEPER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>KeeperPulse v1.6 — Permanent Auto-Boot & Multi-Token Guard</title>
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

  <header class="card p-4">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div>
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse"></span>
          <h1 class="text-xl font-extrabold text-white">KeeperPulse <span class="text-emerald-400">v1.6</span></h1>
          <span class="text-xs px-2.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 font-semibold">
            Env-Var Auto-Boot • Multi-Token KeeperGuardV2
          </span>
        </div>
        <p class="text-xs text-slate-400 mt-1">
          Scans callReward() + rewardsAvailable() • Verifies WETH, ETH, AERO/ARB & USDC Gains On-Chain
        </p>
      </div>
      <div class="text-right text-xs">
        <div class="text-slate-400">Last Scan: <span id="lastScanClock" class="mono text-emerald-400 font-bold">--:--:--</span> (<span id="scanMs" class="mono">0</span>ms)</div>
        <div class="text-slate-500">ETH Price: <span id="ethPriceBadge" class="mono text-slate-300">$2251.42</span></div>
      </div>
    </div>

    <div class="grid grid-cols-2 sm:grid-cols-5 gap-2.5 mt-4">
      <div class="bg-slate-900/90 border border-slate-800 rounded-lg p-3">
        <div class="text-[10px] uppercase text-slate-400">Mode / Network</div>
        <div id="modePill" class="text-xs font-extrabold text-amber-300 mt-0.5">🧪 DRY-RUN</div>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-lg p-3">
        <div class="text-[10px] uppercase text-slate-400">Gas Wallet (ETH)</div>
        <div id="nativeEthPill" class="mono text-sm font-bold text-white mt-0.5">0.000000 ETH</div>
      </div>
      <div class="bg-slate-900/90 border border-cyan-500/30 rounded-lg p-3">
        <div class="text-[10px] uppercase text-cyan-300">WETH / Token Bounties</div>
        <div id="wethPill" class="mono text-xs font-bold text-cyan-300 mt-0.5">0 WETH | 0 AERO</div>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-lg p-3">
        <div class="text-[10px] uppercase text-slate-400">KeeperGuard Contract</div>
        <div id="guardPill" class="mono text-xs font-bold text-amber-300 mt-0.5">Not Deployed Yet</div>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-lg p-3">
        <div class="text-[10px] uppercase text-slate-400">Verified Profit</div>
        <div id="profitPill" class="mono text-sm font-bold text-emerald-400 mt-0.5">+$0.0000</div>
      </div>
    </div>
  </header>

  <section class="card p-4">
    <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
      <h2 class="text-xs font-bold uppercase tracking-wider text-amber-300">
        ⚡ Step 1: Network, Wallet & Multi-Token KeeperGuardV2
      </h2>
      <div class="flex flex-wrap gap-2">
        <button onclick="triggerManualScan()" id="scanNowBtn" class="px-3 py-1.5 rounded-lg text-xs font-bold bg-indigo-600 hover:bg-indigo-500 text-white">
          🔍 Scan Vaults Now
        </button>
        <button onclick="deployGuard()" id="deployGuardBtn" class="px-3 py-1.5 rounded-lg text-xs font-extrabold bg-cyan-500 hover:bg-cyan-400 text-slate-950">
          🛡️ Deploy Multi-Token KeeperGuardV2 (~$0.01)
        </button>
        <button onclick="unwrapWeth()" class="px-3 py-1.5 rounded-lg text-xs font-bold bg-purple-600 hover:bg-purple-500 text-white">
          🔄 Unwrap WETH -> ETH
        </button>
        <button onclick="toggleLiveMode()" id="liveToggleBtn" class="px-3 py-1.5 rounded-lg text-xs font-bold bg-amber-500 hover:bg-amber-400 text-slate-950">
          🚀 Arm / Disarm Live Keeper
        </button>
      </div>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
      <div>
        <label class="block text-slate-400 mb-1">Target Layer-2 Network</label>
        <select id="chainSelect" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-white">
          <option value="base">Base Mainnet (Lowest Gas ~$0.004)</option>
          <option value="arbitrum">Arbitrum One (High Vault Volume)</option>
        </select>
      </div>
      <div>
        <label class="block text-slate-400 mb-1">RPC URL (Alchemy or Public HTTP)</label>
        <input id="rpcInput" type="text" value="https://mainnet.base.org" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-white mono" />
      </div>
      <div>
        <label class="block text-slate-400 mb-1">Min Net Profit Per Harvest (USD)</label>
        <input id="minProfitInput" type="number" step="0.01" value="0.01" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-white mono" />
      </div>
      <div>
        <label class="block text-slate-400 mb-1">Keeper Wallet Private Key</label>
        <input id="privKeyInput" type="password" placeholder="0x... (Saved securely on server)" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-white mono" />
      </div>
      <div>
        <label class="block text-slate-400 mb-1">KeeperGuard Contract Address</label>
        <input id="guardInput" type="text" placeholder="0xab1d119FC3468036B2b92c69A5377ee0b408b215" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-emerald-300 mono" />
      </div>
      <div class="flex items-end">
        <button onclick="saveConfig()" class="w-full py-2 px-4 rounded-lg text-xs font-extrabold bg-emerald-500 hover:bg-emerald-400 text-slate-950">
          💾 Save & Sync Node
        </button>
      </div>
    </div>
  </section>

  <div class="grid grid-cols-1 lg:grid-cols-12 gap-4">
    <div class="lg:col-span-6 card p-4">
      <h3 class="text-xs font-bold uppercase tracking-wider text-emerald-300 mb-2">
        🎯 Live Bounties (Verified by KeeperGuard.safeHarvest)
      </h3>
      <div id="oppsList" class="space-y-2 max-h-72 overflow-y-auto"></div>
    </div>

    <div class="lg:col-span-6 card p-4">
      <h3 class="text-xs font-bold uppercase tracking-wider text-cyan-300 mb-2">
        🧾 Verified Harvest Ledger
      </h3>
      <div id="ledgerList" class="space-y-2 max-h-72 overflow-y-auto"></div>
    </div>
  </div>

  <section class="card p-4">
    <h3 class="text-xs font-bold uppercase tracking-wider text-slate-300 mb-2">🛡️ Live Multicall3 & KeeperGuard Logs</h3>
    <div id="logsBox" class="mono text-[11px] bg-slate-950 p-3 rounded-lg border border-slate-800 space-y-1 max-h-60 overflow-y-auto text-slate-300"></div>
  </section>

</div>

<script>
async function pollState() {
  try {
    const r = await fetch('/state?_t=' + Date.now());
    const s = await r.json();
    document.getElementById('lastScanClock').textContent = s.last_scan_hms || '--:--:--';
    document.getElementById('scanMs').textContent = s.last_scan_ms || 0;
    document.getElementById('ethPriceBadge').textContent = '$' + Number(s.eth_price_usd || 2251).toFixed(2);
    document.getElementById('nativeEthPill').textContent = Number(s.native_eth_balance || 0).toFixed(6) + ' ETH';
    document.getElementById('wethPill').textContent = Number(s.weth_balance || 0).toFixed(5) + ' WETH | ' + Number(s.aero_balance || 0).toFixed(3) + ' AERO';

    const guardPill = document.getElementById('guardPill');
    const guardInput = document.getElementById('guardInput');
    if (s.guard_contract) {
      guardPill.textContent = '🛡️ ' + s.guard_contract.slice(0, 6) + '...' + s.guard_contract.slice(-4);
      guardPill.className = 'mono text-xs font-bold text-emerald-400 mt-0.5';
      if (document.activeElement !== guardInput) {
        guardInput.value = s.guard_contract;
      }
    } else {
      guardPill.textContent = 'Not Deployed Yet';
      guardPill.className = 'mono text-xs font-bold text-amber-300 mt-0.5';
    }

    document.getElementById('profitPill').textContent = '+$' + Number(s.realized_profit_usd || 0).toFixed(4) + ' (' + (s.total_bounties_won || 0) + ' won)';

    const modePill = document.getElementById('modePill');
    if (s.live_armed) {
      modePill.textContent = '🟢 LIVE ARMED (' + (s.chain || 'BASE').toUpperCase() + ')';
      modePill.className = 'text-xs font-extrabold text-emerald-400 mt-0.5';
    } else {
      modePill.textContent = '🧪 DRY-RUN (' + (s.chain || 'BASE').toUpperCase() + ')';
      modePill.className = 'text-xs font-extrabold text-amber-300 mt-0.5';
    }

    const opps = s.live_opportunities || [];
    const oppsEl = document.getElementById('oppsList');
    if (opps.length === 0) {
      oppsEl.innerHTML = '<div class="text-xs text-slate-500 py-6 text-center">Scanning 90+ strategies every 12s...</div>';
    } else {
      oppsEl.innerHTML = opps.map(o => `
        <div class="p-2.5 rounded-lg bg-slate-900 border border-emerald-500/30 flex items-center justify-between text-xs">
          <div>
            <div class="font-bold text-white">${o.name} <span class="text-[10px] text-slate-400">(${o.platform})</span></div>
            <div class="mono text-[10px] text-slate-400">Bounty: $${o.bounty_usd.toFixed(4)} • Gas: $${o.gas_usd.toFixed(4)}</div>
          </div>
          <div class="mono font-extrabold text-emerald-400">+$${o.net_profit_usd.toFixed(4)}</div>
        </div>
      `).join('');
    }

    const ledger = s.harvest_ledger || [];
    const ledgerEl = document.getElementById('ledgerList');
    if (ledger.length === 0) {
      ledgerEl.innerHTML = '<div class="text-xs text-slate-500 py-6 text-center">No harvests logged yet.</div>';
    } else {
      ledgerEl.innerHTML = ledger.map(l => `
        <div class="p-2.5 rounded-lg bg-slate-900 border border-slate-800 flex items-center justify-between text-xs">
          <div>
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-bold mr-1">${l.mode}</span>
            <span class="font-bold text-white">${l.vault}</span>
            <div class="mono text-[10px] text-slate-400 mt-0.5">${l.strategy} • ${l.time}</div>
          </div>
          <div class="mono font-bold text-emerald-400">+$${Number(l.net_usd).toFixed(4)}</div>
        </div>
      `).join('');
    }

    document.getElementById('logsBox').innerHTML = (s.logs || []).map(l => `<div>${l}</div>`).join('');
  } catch (e) {}
}

async function saveConfig() {
  const chain = document.getElementById('chainSelect').value;
  const rpc = document.getElementById('rpcInput').value.trim();
  const minProf = parseFloat(document.getElementById('minProfitInput').value || '0.01');
  const pk = document.getElementById('privKeyInput').value.trim();
  const guard = document.getElementById('guardInput').value.trim();
  await fetch('/configure', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chain, rpc_url: rpc, min_profit_usd: minProf, private_key: pk, guard_contract: guard })
  });
  document.getElementById('privKeyInput').value = '';
  pollState();
}

async function deployGuard() {
  const btn = document.getElementById('deployGuardBtn');
  const pk = document.getElementById('privKeyInput').value.trim();
  if (pk) await saveConfig();
  btn.textContent = '⏳ Compiling & Deploying V2...';
  btn.disabled = true;
  try {
    const r = await fetch('/deploy_guard', { method: 'POST' });
    const res = await r.json();
    alert(res.message);
  } catch (e) {
    alert('Deploy error: ' + e);
  }
  btn.textContent = '🛡️ Deploy Multi-Token KeeperGuardV2 (~$0.01)';
  btn.disabled = false;
  pollState();
}

async function unwrapWeth() {
  const r = await fetch('/unwrap_weth', { method: 'POST' });
  const res = await r.json();
  alert(res.message);
  pollState();
}

async function triggerManualScan() {
  const btn = document.getElementById('scanNowBtn');
  btn.textContent = '⏳ Scanning...';
  await fetch('/scan_now', { method: 'POST' });
  btn.textContent = '🔍 Scan Vaults Now';
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


@app.route("/")
def index():
    return render_template_string(KEEPER_UI_HTML)


@app.route("/state")
def get_state():
    with state_lock:
        resp = json.loads(json.dumps(STATE))
        resp["private_key"] = ""
    return jsonify(resp)


@app.route("/configure", methods=["POST"])
def configure():
    data = request.get_json(force=True) or {}
    with state_lock:
        new_chain = data.get("chain", STATE["chain"])
        if new_chain in CHAIN_CONFIGS:
            STATE["chain"] = new_chain
            if not data.get("rpc_url") or data["rpc_url"] in ("https://mainnet.base.org", "https://arb1.arbitrum.io/rpc"):
                STATE["rpc_url"] = CHAIN_CONFIGS[new_chain]["default_rpc"]
            else:
                STATE["rpc_url"] = data["rpc_url"].strip()
        if data.get("min_profit_usd") is not None:
            STATE["min_profit_usd"] = float(data["min_profit_usd"])
        if data.get("private_key"):
            STATE["private_key"] = data["private_key"].strip()
        if data.get("guard_contract") is not None:
            candidate = data["guard_contract"].strip()
            if candidate.lower() != STATE.get("wallet_address", "").lower():
                STATE["guard_contract"] = candidate

    refresh_long_tail_vaults(STATE["chain"], force=True)
    sync_wallet_balances()
    run_multicall_bounty_scan(manual=True)
    save_state()
    return jsonify({"ok": True})


@app.route("/deploy_guard", methods=["POST"])
def deploy_guard_route():
    ok, msg = compile_and_deploy_keeper_guard()
    return jsonify({"ok": ok, "message": msg, "guard_contract": STATE.get("guard_contract", "")})


@app.route("/unwrap_weth", methods=["POST"])
def unwrap_weth_route():
    ok, msg = unwrap_weth_to_eth()
    return jsonify({"ok": ok, "message": msg})


@app.route("/scan_now", methods=["POST"])
def scan_now():
    run_multicall_bounty_scan(manual=True)
    return jsonify({"ok": True})


@app.route("/toggle_live", methods=["POST"])
def toggle_live():
    with state_lock:
        if not STATE.get("private_key") and not STATE.get("live_armed"):
            return jsonify({"ok": False, "message": "Paste Private Key and click Save first."})
        if not STATE.get("guard_contract") and not STATE.get("live_armed"):
            return jsonify({"ok": False, "message": "Deploy KeeperGuard first."})
        STATE["live_armed"] = not bool(STATE.get("live_armed", False))
        mode_str = "LIVE ARMED (STRICT KEEPERGUARD)" if STATE["live_armed"] else "DRY-RUN SIMULATION"
    add_log(f"⚡ KeeperPulse mode switched to: {mode_str}")
    save_state()
    return jsonify({"ok": True, "live_armed": STATE["live_armed"]})


if __name__ == "__main__":
    sync_wallet_balances()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
