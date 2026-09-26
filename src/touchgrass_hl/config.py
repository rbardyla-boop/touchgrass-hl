"""Runtime configuration. Secrets come only from the environment or a gitignored .env."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from touchgrass_hl.util import D

TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_API_URL = "https://api.hyperliquid.xyz"
MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"


def official_perp_fee(
    *,
    base_rate: Decimal,
    role: str,
    deployer_fee_scale: Decimal | None,
    growth_mode: bool,
    referral_discount: Decimal = Decimal(0),
    aligned_quote: bool = False,
    hip3: bool = False,
) -> tuple[Decimal | None, dict[str, Any]]:
    """Official HIP-3 fee as a fraction, not a percent.

    scaleIfHip3 = scale+1 when scale < 1, else scale*2.
    growthModeScale = 0.1 when growth mode is enabled, else 1.
    Core markets use scaleIfHip3 = 1 and deployer share 0.
    A missing HIP-3 deployerFeeScale is unknown and must not be invented.
    Aligned quote collateral is applied only when the caller says it is aligned.
    Paper defaults that flag to false.
    """
    growth = Decimal("0.1") if growth_mode else Decimal(1)
    if not hip3:
        scale_if = Decimal(1)
        share = Decimal(0)
        scale_value = None
    elif deployer_fee_scale is None:
        return None, {
            "known": False,
            "hip3": True,
            "role": role,
            "reason": "missing_deployer_fee_scale",
        }
    else:
        scale_value = D(deployer_fee_scale)
        if scale_value < 1:
            scale_if = scale_value + Decimal(1)
            share = scale_value / (Decimal(1) + scale_value)
        else:
            scale_if = scale_value * Decimal(2)
            share = Decimal("0.5")
    referral = Decimal(1) - D(referral_discount)
    if role == "taker":
        rate = D(base_rate) * scale_if * growth * referral
        if aligned_quote:
            rate *= (Decimal(1) - share) * Decimal("0.8") + share
    else:
        preliminary = D(base_rate) * growth
        if preliminary > 0:
            rate = preliminary * scale_if * referral
        else:
            rebate = ((Decimal(1) - share) * Decimal("1.5") + share) if aligned_quote else Decimal(1)
            rate = preliminary * rebate
    inputs: dict[str, Any] = {
        "known": True,
        "role": role,
        "base_rate": format(D(base_rate), "f"),
        "deployer_fee_scale": None if scale_value is None else format(scale_value, "f"),
        "scale_if_hip3": format(scale_if, "f"),
        "deployer_share": format(share, "f"),
        "growth_mode": bool(growth_mode),
        "growth_mode_scale": format(growth, "f"),
        "referral_discount": format(D(referral_discount), "f"),
        "aligned_quote": bool(aligned_quote),
        "hip3": bool(hip3),
        "rate": format(rate, "f"),
        "formula": (
            "taker: base * scaleIfHip3 * growthModeScale * (1-referral) "
            "* aligned((1-share)*0.8+share); "
            "maker>0: base * growth * scaleIfHip3 * (1-referral); "
            "maker rebate: base * growth * aligned((1-share)*1.5+share)"
        ),
    }
    return rate, inputs


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = "sqlite:///data/touchgrass.db"
    log_dir: str = "logs"
    log_level: str = "INFO"
    log_max_bytes: int = 10_485_760
    log_backup_count: int = 5

    hl_info_url: str = MAINNET_API_URL
    hl_ws_url: str = MAINNET_WS_URL
    http_timeout_s: float = 20.0

    # paper is the only default. testnet is the only execution mode that can sign.
    execution_mode: Literal["paper", "testnet"] = "paper"
    testnet_api_url: str = TESTNET_API_URL
    testnet_account_address: str = ""
    testnet_agent_private_key: str = ""
    testnet_auto_trade: bool = False
    testnet_max_notional_usd: Decimal = Decimal("15")
    trading_kill_switch: bool = False

    jev_enabled: bool = False
    jev_api_key: str = ""
    jev_model: str = "jev-latest"
    jev_base_url: str = "https://api.typesafe.ai"
    jev_timeout_s: float = 30.0
    jev_min_cluster_quality: Decimal = Decimal("2")
    jev_min_behavior_fit: Decimal = Decimal("0.55")
    jev_max_contradiction: Decimal = Decimal("0.45")
    jev_min_information_sufficient: Decimal = Decimal("0.55")

    max_trade_subscriptions: int = 80
    market_refresh_seconds: int = 120
    status_interval_seconds: int = 60
    scoring_interval_seconds: int = 300
    retention_interval_seconds: int = 600
    paper_monitor_seconds: float = 2.0

    trade_retention_days: int = 14
    snapshot_retention_hours: int = 24
    hydration_max_per_hour: int = 30
    min_trades_to_hydrate: int = 8
    min_notional_to_hydrate: Decimal = Decimal("5000")
    hydration_lookback_days: int = 90
    verified_baseline_batch: int = 10

    info_weight_per_minute: int = 1000
    info_window_seconds: float = 60.0

    min_closed_trades: int = 30
    min_active_days: int = 14
    max_profit_concentration: Decimal = Decimal("0.50")
    allow_profit_concentration_override: bool = False
    min_copyability_observations: int = 10
    supported_collateral_tokens: str = "0"

    weight_profitability: Decimal = Decimal("0.20")
    weight_profit_factor: Decimal = Decimal("0.15")
    weight_consistency: Decimal = Decimal("0.15")
    weight_copyability: Decimal = Decimal("0.15")
    weight_win_rate: Decimal = Decimal("0.10")
    weight_inverse_drawdown: Decimal = Decimal("0.10")
    weight_inverse_concentration: Decimal = Decimal("0.10")
    weight_data_quality: Decimal = Decimal("0.05")

    copy_delays_s: str = "2,5,15,30"

    independence_window_days: int = 14
    independence_jaccard_min: Decimal = Decimal("0.75")
    independence_min_simultaneous: int = 4
    independence_proximity_ms: int = 3000
    independence_min_events: int = 5
    independence_max_wallets: int = 400

    cluster_window_seconds: int = 120
    cluster_min_groups: int = 3
    signal_cooldown_seconds: int = 900

    paper_starting_equity: Decimal = Decimal("75")
    paper_target_notional: Decimal = Decimal("5")
    paper_max_positions: int = 2
    paper_stop_loss_pct: Decimal = Decimal("0.01")
    paper_take_profit_pct: Decimal = Decimal("0.02")
    paper_max_hold_seconds: int = 3600

    fee_taker_rate: Decimal = Decimal("0.00045")
    fee_maker_rate: Decimal = Decimal("0.00015")
    fee_hip3_growth_taker_rate: Decimal = Decimal("0.00009")
    fee_hip3_growth_maker_rate: Decimal = Decimal("0.00003")
    fee_hip3_standard_taker_rate: Decimal = Decimal("0.00180")
    fee_hip3_standard_maker_rate: Decimal = Decimal("0.00060")

    max_spread_bps: Decimal = Decimal("15")
    max_slippage_bps: Decimal = Decimal("20")
    max_price_move_since_first_bps: Decimal = Decimal("30")
    stale_market_seconds: int = 15
    stale_exit_seconds: int = 30
    min_book_notional_usd: Decimal = Decimal("5")
    daily_loss_limit_usd: Decimal = Decimal("5")
    max_drawdown_pct: Decimal = Decimal("0.20")
    ws_stale_seconds: int = 20

    market_allowlist: str = ""
    market_denylist: str = ""

    @field_validator("execution_mode", mode="before")
    @classmethod
    def _execution_mode(cls, value: str) -> str:
        text = str(value or "paper").strip().lower()
        if text not in ("paper", "testnet"):
            raise ValueError(
                "execution_mode must be 'paper' or 'testnet'. "
                "Mainnet trading is disabled in v0.1.1 and there is no mainnet execution path."
            )
        return text

    @field_validator("log_level")
    @classmethod
    def _level(cls, value: str) -> str:
        return str(value or "INFO").upper()

    @model_validator(mode="after")
    def _weights(self) -> Settings:
        total = (
            self.weight_profitability
            + self.weight_profit_factor
            + self.weight_consistency
            + self.weight_copyability
            + self.weight_win_rate
            + self.weight_inverse_drawdown
            + self.weight_inverse_concentration
            + self.weight_data_quality
        )
        if abs(total - Decimal("1")) > Decimal("0.0000001"):
            raise ValueError(f"score weights must sum to 1, got {total}")
        if self.cluster_min_groups < 1:
            raise ValueError("cluster_min_groups must be >= 1")
        if self.max_trade_subscriptions < 1:
            raise ValueError("max_trade_subscriptions must be >= 1")
        if self.paper_max_positions < 1:
            raise ValueError("paper_max_positions must be >= 1")
        if "testnet" not in self.testnet_api_url:
            raise ValueError("testnet_api_url must be a Hyperliquid testnet URL")
        return self

    def copy_delay_list(self) -> list[int]:
        out = []
        for part in self.copy_delays_s.split(","):
            part = part.strip()
            if part:
                out.append(int(part))
        return out or [2, 5, 15, 30]

    def allow_coins(self) -> set[str]:
        return {c.strip() for c in self.market_allowlist.split(",") if c.strip()}

    def deny_coins(self) -> set[str]:
        return {c.strip() for c in self.market_denylist.split(",") if c.strip()}

    def redacted(self) -> dict:
        from touchgrass_hl.util import redact

        return redact(self.model_dump(mode="json"))

    def supported_collateral(self) -> frozenset[int]:
        out: set[int] = set()
        for part in self.supported_collateral_tokens.split(","):
            part = part.strip()
            if part:
                out.add(int(part))
        return frozenset(out or {0})

    def fee_for(
        self,
        *,
        dex: str,
        growth_mode: str | None,
        role: str,
        deployer_fee_scale: Decimal | str | None = None,
        aligned_quote: bool = False,
        referral_discount: Decimal = Decimal(0),
    ) -> tuple[Decimal | None, str, dict[str, Any]]:
        """Tier-0 base rate times the official HIP-3 multiplier.

        Returns (rate, label, inputs). rate is None when a HIP-3 market has no
        deployerFeeScale. The inputs dict is what a simulated fill must store.
        """
        hip3 = dex not in ("", "core")
        growth = str(growth_mode or "").lower() == "enabled"
        base = self.fee_taker_rate if role == "taker" else self.fee_maker_rate
        scale = None if deployer_fee_scale in (None, "") else D(deployer_fee_scale)
        rate, inputs = official_perp_fee(
            base_rate=base,
            role=role,
            deployer_fee_scale=scale,
            growth_mode=growth,
            referral_discount=referral_discount,
            aligned_quote=aligned_quote,
            hip3=hip3,
        )
        if rate is None:
            return None, "hip3_fee_scale_unknown", inputs
        label = f"official_{'hip3' if hip3 else 'core'}_{role}_{'growth' if growth else 'base'}"
        inputs["label"] = label
        return rate, label, inputs


def score_weights(settings: Settings) -> dict[str, Decimal]:
    return {
        "profitability": D(settings.weight_profitability),
        "profit_factor": D(settings.weight_profit_factor),
        "consistency": D(settings.weight_consistency),
        "copyability": D(settings.weight_copyability),
        "win_rate": D(settings.weight_win_rate),
        "inverse_drawdown": D(settings.weight_inverse_drawdown),
        "inverse_concentration": D(settings.weight_inverse_concentration),
        "data_quality": D(settings.weight_data_quality),
    }
