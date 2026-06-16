"""Generate a small, fully synthetic demo dataset (no real-world data of any kind).

Writes a sales CSV and two short markdown docs into data/sample/ so the app and tests have
something to chew on out of the box. Deterministic via a fixed seed.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "sample"

REGIONS = ["North", "South", "East", "West", "Central"]
CATEGORIES = {
    "Apparel": ["T-Shirt", "Jeans", "Jacket", "Socks"],
    "Home": ["Lamp", "Cushion", "Mug", "Towel"],
    "Electronics": ["Earbuds", "Charger", "Speaker", "Webcam"],
}


def write_sales(path: Path, rows: int = 4000, seed: int = 7) -> None:
    rng = random.Random(seed)
    products = [(p, cat) for cat, items in CATEGORIES.items() for p in items]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "region",
                "store_id",
                "category",
                "product",
                "week",
                "units_sold",
                "revenue",
                "revenue_last_year",
            ]
        )
        for _ in range(rows):
            product, category = rng.choice(products)
            region = rng.choice(REGIONS)
            store_id = rng.randint(100, 140)
            week = rng.randint(1, 52)
            units = rng.randint(0, 200)
            price = round(rng.uniform(5, 120), 2)
            revenue = round(units * price, 2)
            ly = round(revenue * rng.uniform(0.7, 1.4), 2)
            w.writerow([region, store_id, category, product, week, units, revenue, ly])


ONBOARDING = """# Onboarding Guide

Welcome to the analytics workspace. This tool answers questions about the sales dataset and
the reference documents loaded alongside it.

## How it works
Ask a natural-language question. The assistant decides whether to run a SQL query over the
tabular data, search the documents, or both, and then summarizes the result with sources.

## Tips
- Be specific about the metric and the grouping (for example: revenue by region for week 10).
- The assistant only runs read-only queries; it cannot modify data.
- If you need a definition, ask — definitions live in the reference documents.
"""

FORECASTING = """# Forecasting Overview

Forecasting estimates future demand from historical sales. This document defines the core terms.

## Key terms
- Baseline: the expected units sold under normal conditions.
- Lift: the incremental units attributable to a promotion or event.
- Seasonality: repeating weekly or yearly demand patterns.
- Year-over-year (YoY): this period compared with the same period last year.

## Method
A simple baseline-plus-lift model is sufficient for most categories. Review YoY revenue to
spot structural shifts before trusting a short-term forecast.
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write_sales(OUT / "sales.csv")
    (OUT / "onboarding_guide.md").write_text(ONBOARDING, encoding="utf-8")
    (OUT / "forecasting_overview.md").write_text(FORECASTING, encoding="utf-8")
    print(f"Wrote sample data to {OUT}")


if __name__ == "__main__":
    main()
