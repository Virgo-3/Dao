"""Configuration is server-side; credentials never enter conversation state."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import os


@dataclass(frozen=True)
class Config:
    provider: str = "demo"
    model: str = "gpt-4.1-mini"
    api_key: str = field(default="", repr=False)
    token_budget: int = 100_000
    max_output_tokens: int = 2048
    input_price: Decimal = Decimal(0)
    output_price: Decimal = Decimal(0)
    pricing_configured: bool = False

    @classmethod
    def from_env(cls):
        provider = os.getenv("DAO_PROVIDER", "demo")
        if provider not in {"demo", "openai"}:
            raise ValueError("DAO_PROVIDER must be demo or openai")
        key = os.getenv("OPENAI_API_KEY", "")
        if provider == "openai" and not key:
            raise ValueError("Set OPENAI_API_KEY before using DAO_PROVIDER=openai")
        budget = int(os.getenv("DAO_TOKEN_BUDGET", "100000"))
        output = int(os.getenv("DAO_MAX_OUTPUT_TOKENS", "2048"))
        if budget < 1 or not 1 <= output <= 16384:
            raise ValueError("Token budget must be positive; output limit must be 1..16384")
        prices = [os.getenv("DAO_INPUT_USD_PER_MILLION"), os.getenv("DAO_OUTPUT_USD_PER_MILLION")]
        if any(p is not None for p in prices) and not all(p is not None for p in prices):
            raise ValueError("Configure both input and output prices")
        try:
            parsed = [Decimal(p or "0") for p in prices]
        except InvalidOperation as exc:
            raise ValueError("Prices must be decimal numbers") from exc
        if any(not p.is_finite() or p < 0 for p in parsed):
            raise ValueError("Prices must be finite and nonnegative")
        return cls(provider, os.getenv("DAO_MODEL", "gpt-4.1-mini"), key, budget, output,
                   *parsed, all(p is not None for p in prices))

    def public(self):
        return {"provider": self.provider, "model": self.model if self.provider == "openai" else "demo",
                "token_budget": self.token_budget, "max_output_tokens": self.max_output_tokens,
                "pricing_configured": self.pricing_configured,
                "input_usd_per_million": str(self.input_price),
                "output_usd_per_million": str(self.output_price)}

    def cost(self, input_tokens, output_tokens):
        # USD/million tokens is numerically equal to micro-USD/token.
        return int((self.input_price * input_tokens + self.output_price * output_tokens)
                   .to_integral_value(rounding="ROUND_CEILING"))
