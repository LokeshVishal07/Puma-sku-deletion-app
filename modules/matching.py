"""
Core "should this SKU be deleted from the marketplace" decision engine.

Delete rule (as specified by the business):
    Zero stock in ALL of: Jan-2025, Jun-2025, Dec-2025, and current inventory
    AND
    Not found in the Content file OR not found in the ZeCom tracker (master)

Everything is joined on EAN (the number that both the marketplace
"Seller SKU" and the inventory files use). The Content file maps
EAN -> Article No (PIM Article No / Style# / Color No - same thing,
different region's name for it). The ZeCom tracker's own key is that
Article No, not EAN, so "found in ZeCom" is only checkable for rows
where we could resolve an Article No via the Content file.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .io_utils import normalize_id, normalize_article_no, safe_num

STOCK_CHECKPOINTS = ["Jan 2025", "Jun 2025", "Dec 2025", "Current"]


@dataclass
class MarketplaceRow:
    platform: str
    region: str
    seller_sku: str
    ean: str
    product_title: str
    status: str | None = None
    platform_product_id: str | None = None


def build_content_lookup(content_df: pd.DataFrame, ean_col: str, article_col: str) -> dict:
    """EAN -> Article No, from the Content file."""
    lookup = {}
    for _, row in content_df.iterrows():
        ean = normalize_id(row.get(ean_col))
        art = normalize_article_no(row.get(article_col))
        if ean and art and ean not in lookup:
            lookup[ean] = art
    return lookup


def build_zecom_article_set(zecom_df: pd.DataFrame, article_col: str) -> set:
    out = set()
    for v in zecom_df.get(article_col, []):
        art = normalize_article_no(v)
        if art:
            out.add(art)
    return out


def build_stock_lookup(stock_df: pd.DataFrame, ean_col: str, stock_col: str) -> dict:
    """EAN -> summed stock qty. Sums across duplicate EAN rows (e.g. multiple
    warehouses / facilities for the same barcode)."""
    lookup: dict[str, float] = {}
    for _, row in stock_df.iterrows():
        ean = normalize_id(row.get(ean_col))
        if not ean:
            continue
        qty = safe_num(row.get(stock_col))
        lookup[ean] = lookup.get(ean, 0.0) + qty
    return lookup


def evaluate(
    marketplace_rows: list[MarketplaceRow],
    content_lookup: dict,
    zecom_article_set: set,
    stock_lookups: dict[str, dict | None],
) -> pd.DataFrame:
    """
    stock_lookups: {"Jan 2025": {ean: qty} | None, "Jun 2025": ..., ...}
    A value of None means that checkpoint file hasn't been uploaded yet -
    we do NOT silently treat that as zero stock.
    """
    records = []
    for r in marketplace_rows:
        article_no = content_lookup.get(r.ean)
        found_in_content = article_no is not None
        found_in_zecom = bool(article_no) and article_no in zecom_article_set

        stock_values = {}
        missing_checkpoints = []
        for cp in STOCK_CHECKPOINTS:
            lut = stock_lookups.get(cp)
            if lut is None:
                stock_values[cp] = None
                missing_checkpoints.append(cp)
            else:
                stock_values[cp] = lut.get(r.ean, 0.0)

        has_all_checkpoints = len(missing_checkpoints) == 0
        zero_everywhere = has_all_checkpoints and all(
            (v is not None and v <= 0) for v in stock_values.values()
        )
        not_master_listed = (not found_in_content) or (not found_in_zecom)

        reasons = []
        if has_all_checkpoints:
            if zero_everywhere:
                reasons.append("Zero stock in Jan/Jun/Dec 2025 & current inventory")
            else:
                nonzero = [
                    f"{cp}: {int(v)}"
                    for cp, v in stock_values.items()
                    if v is not None and v > 0
                ]
                if nonzero:
                    reasons.append("Has stock (" + ", ".join(nonzero) + ")")
        else:
            reasons.append(
                "Missing stock data for: " + ", ".join(missing_checkpoints)
            )

        if not found_in_content:
            reasons.append("Not found in Content file")
        if article_no and not found_in_zecom:
            reasons.append("Not found in ZeCom master")
        if found_in_content and found_in_zecom:
            reasons.append("Found in Content file & ZeCom master")

        if not has_all_checkpoints:
            decision = "REVIEW (missing stock data)"
        elif zero_everywhere and not_master_listed:
            decision = "YES"
        else:
            decision = "NO"

        records.append(
            {
                "Region": r.region,
                "Platform": r.platform,
                "Seller SKU": r.seller_sku,
                "Platform Product ID": r.platform_product_id or "",
                "EAN": r.ean,
                "Article No": article_no or "",
                "Product Title": r.product_title,
                "Status": r.status or "",
                "Stock Jan 2025": stock_values["Jan 2025"],
                "Stock Jun 2025": stock_values["Jun 2025"],
                "Stock Dec 2025": stock_values["Dec 2025"],
                "Stock Current": stock_values["Current"],
                "Found in Content File": "Yes" if found_in_content else "No",
                "Found in ZeCom Master": "Yes" if found_in_zecom else ("N/A" if not article_no else "No"),
                "Delete": decision,
                "Reason": "; ".join(reasons),
            }
        )

    return pd.DataFrame.from_records(records)
