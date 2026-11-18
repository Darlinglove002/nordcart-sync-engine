from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=100)
    unit_price_cents: int = Field(ge=1, le=100_000_000)
    discount_cents: int = Field(default=0, ge=0, le=100_000_000)
    commission_bps: int = Field(ge=0, le=5_000)
    logistics_cents: int = Field(ge=0, le=10_000_000)
    status: Literal["delivered", "cancelled"] = "delivered"
    ordered_at: datetime

    @model_validator(mode="after")
    def _sane(self) -> "OrderIn":
        if self.ordered_at.tzinfo is None:
            raise ValueError("ordered_at must include a timezone offset")
        total = self.quantity * self.unit_price_cents
        if total > 2_000_000_000:
            raise ValueError("order line total is limited to 2,000,000,000 cents")
        if self.discount_cents > total:
            raise ValueError("discount_cents exceeds the order line total")
        return self


class OrdersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orders: list[OrderIn] = Field(min_length=1, max_length=1000)


class SeedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sellers: int = Field(default=5, ge=1, le=50)
    orders: int = Field(default=50_000, ge=100, le=2_000_000)
    days: int = Field(default=180, ge=7, le=730)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)


class JobAccepted(BaseModel):
    job_id: str
    status_url: str | None = None
