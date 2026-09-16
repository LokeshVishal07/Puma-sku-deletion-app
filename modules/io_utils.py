"""
File-reading helpers.

The Puma export files this app deals with are messy in a very specific,
consistent way:
  - Real headers are almost never on row 1 - there are title rows,
    "changes since <date>" rows, promo banners, merged-cell leftovers, etc.
    above the actual header.
  - Column names/positions drift slightly release to release, so nothing
    should be hardcoded to a column *index*.

Everything here is built around: detect the header row, let the caller
(the Streamlit UI) confirm/override it, then let the user map the columns
that matter (EAN, Seller SKU, Article No, Stock, ...) onto whatever the
real header text is this week.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

import pandas as pd

# Tokens we look for when guessing which row is the real header row.
HEADER_TOKENS = [
    "ean", "sku", "seller", "article", "style", "stock", "qty", "quantity",
    "soh", "avail", "product", "title", "name", "desc", "status", "gtin",
    "upc", "color", "colour", "size", "price",
]

# Keyword priority lists used to auto-guess which column plays which role.
# Order matters - first match wins.
ROLE_KEYWORDS = {
    "ean": ["ean", "gtin", "upc", "prod_code", "prodcode", "barcode"],
    "seller_sku": [
        "sellersku", "seller sku", "seller_sku", "shop sku", "shopsku",
    ],
    "generic_sku": ["sku"],
    "article_no": [
        "pim article", "article#", "article no", "articleno",
        "article_number", "style#", "style_no", "color_no", "colour_no",
        "parent sku",
    ],
    "product_title": [
        "product name", "product_name", "title", "desc", "name",
    ],
    "stock": [
        "avail_qty", "available qty", "qtyavailable", "avail qty",
        "stock", "qty", "quantity", "soh", "on hand", "onhand",
    ],
    "status": ["status"],
}


@dataclass
class LoadedTable:
    df: pd.DataFrame
    header_row: int
    sheet_name: str | None = None
    source_name: str = ""


def _score_row(values: list) -> float:
    score = 0.0
    for v in values:
        if v is None:
            continue
        raw_s = str(v).strip()
        s = raw_s.lower()
        if not s or s == "nan":
            continue
        for tok in HEADER_TOKENS:
            if tok in s:
                score += 1
                break
        # Business-facing headers read like "Product Name" / "Seller SKU":
        # capitalized, space-separated. Machine/API field names one row
        # above them ("product_name", "seller_sku") match the same
        # keywords but are snake_case - down-weight those so the
        # human header wins the tie.
        if re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)+", raw_s):
            score -= 0.5
        elif " " in raw_s and raw_s[:1].isupper():
            score += 0.25
    return score


INSTRUCTION_TOKENS = {
    "mandatory", "optional", "uneditable", "conditional", "recommended",
}


def _is_instruction_or_blank_row(values: list) -> bool:
    """Template-style exports (Lazada, TikTok) sandwich 1-3 rows of
    'Mandatory/Optional' markers and long help text between the real header
    and the first actual data row. Detect those so they don't get treated
    as product rows."""
    non_null = [
        str(v).strip() for v in values
        if v is not None and str(v).strip().lower() not in ("", "nan")
    ]
    if not non_null:
        return True  # fully blank row
    token_hits = sum(1 for v in non_null if v.lower() in INSTRUCTION_TOKENS)
    long_text_hits = sum(1 for v in non_null if len(v) > 45)
    return (token_hits / len(non_null) > 0.3) or (long_text_hits / len(non_null) > 0.3)


def detect_data_start_row(raw: pd.DataFrame, header_row: int, max_extra: int = 10) -> int:
    """Return the index of the first row after `header_row` that looks like
    real data, skipping any instruction/blank rows in between."""
    row = header_row + 1
    limit = min(len(raw), header_row + 1 + max_extra)
    while row < limit and _is_instruction_or_blank_row(list(raw.iloc[row].values)):
        row += 1
    return row


def _dedupe_columns(columns: list[str]) -> list[str]:
    """Mimic pandas' own mangle_dupe_cols behaviour: a repeated header name
    'Color_No' becomes 'Color_No', 'Color_No.1', 'Color_No.2', ... so every
    column name is unique and `df[col]` / `row.get(col)` always returns a
    single scalar/Series rather than an ambiguous multi-column slice."""
    seen: dict[str, int] = {}
    out = []
    for c in columns:
        if c not in seen:
            seen[c] = 0
            out.append(c)
        else:
            seen[c] += 1
            out.append(f"{c}.{seen[c]}")
    return out


def finalize_table(raw: pd.DataFrame, header_row: int, data_start_row: int) -> pd.DataFrame:
    """Build the final DataFrame from a header-less raw read: columns come
    from `header_row`, data starts at `data_start_row` (skipping any
    instruction rows in between)."""
    columns = _dedupe_columns([str(c).strip() for c in raw.iloc[header_row].values])
    df = raw.iloc[data_start_row:].copy()
    df.columns = columns
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
    df = df.reset_index(drop=True)
    return df


def detect_header_row(raw: pd.DataFrame, max_scan: int = 20) -> int:
    """Scan the first `max_scan` rows (no header) and return the row index
    that looks most like a real header row."""
    best_row, best_score = 0, -1
    for i in range(min(max_scan, len(raw))):
        score = _score_row(list(raw.iloc[i].values))
        # Slight preference for earlier rows on ties (less likely to be a
        # stray data row that happens to contain matching words).
        if score > best_score:
            best_score = score
            best_row = i
    return best_row


def _excel_engine_kwargs() -> dict:
    # calamine (Rust-based) tolerates the slightly-malformed XML some
    # marketplace exports (Shopee in particular) produce, where openpyxl
    # raises on things like out-of-spec comment-anchor values. Fall back
    # to the default engine if calamine isn't available in the
    # environment for some reason.
    try:
        import python_calamine  # noqa: F401
        return {"engine": "calamine"}
    except ImportError:
        return {}


def get_excel_sheet_names(file) -> list[str]:
    file.seek(0)
    try:
        xls = pd.ExcelFile(file, **_excel_engine_kwargs())
    except Exception:
        file.seek(0)
        xls = pd.ExcelFile(file)
    return xls.sheet_names


def read_raw_excel(file, sheet_name, nrows: int | None = None) -> pd.DataFrame:
    file.seek(0)
    try:
        return pd.read_excel(
            file, sheet_name=sheet_name, header=None, nrows=nrows,
            **_excel_engine_kwargs(),
        )
    except Exception:
        file.seek(0)
        return pd.read_excel(file, sheet_name=sheet_name, header=None, nrows=nrows)


def load_excel_with_header(
    file, sheet_name, header_row: int, data_start_row: int | None = None
) -> pd.DataFrame:
    """Read a full sheet and build the final table, skipping any
    instruction/blank rows between the header and the first real data row."""
    file.seek(0)
    try:
        raw = pd.read_excel(
            file, sheet_name=sheet_name, header=None, **_excel_engine_kwargs()
        )
    except Exception:
        file.seek(0)
        raw = pd.read_excel(file, sheet_name=sheet_name, header=None)
    if data_start_row is None:
        data_start_row = detect_data_start_row(raw, header_row)
    return finalize_table(raw, header_row, data_start_row)


def load_csv_with_header(
    file, header_row: int, data_start_row: int | None = None
) -> pd.DataFrame:
    file.seek(0)
    raw = pd.read_csv(file, header=None)
    if data_start_row is None:
        data_start_row = detect_data_start_row(raw, header_row)
    return finalize_table(raw, header_row, data_start_row)


def read_zip_of_excels(file) -> list[tuple[str, bytes]]:
    """Shopee exports arrive as a zip of many single-sheet .xlsx files.
    Returns a list of (inner_filename, bytes)."""
    file.seek(0)
    out = []
    with zipfile.ZipFile(file) as z:
        for name in z.namelist():
            if name.lower().endswith((".xlsx", ".xls", ".csv")):
                out.append((name, z.read(name)))
    return out


def guess_column(columns: list[str], role: str) -> str | None:
    keywords = ROLE_KEYWORDS.get(role, [])
    lower_cols = {c: str(c).strip().lower() for c in columns}
    for kw in keywords:
        for col, low in lower_cols.items():
            if kw in low:
                return col
    return None


def _unwrap_scalar(value):
    """Defensive: if a duplicate/ambiguous column name ever slips through
    and `row.get(col)` hands back a Series instead of a scalar, take the
    first value rather than silently stringifying the whole Series."""
    if isinstance(value, pd.Series):
        return value.iloc[0] if len(value) else None
    return value


def normalize_id(value) -> str | None:
    """Normalize an EAN / SKU value to a plain digit-string where possible,
    stripping the trailing '.0' pandas loves to add to numeric-looking
    strings, and any whitespace."""
    value = _unwrap_scalar(value)
    if value is None:
        return None
    if isinstance(value, float):
        if pd.isna(value):
            return None
        if value.is_integer():
            return str(int(value))
        return str(value)
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    s = re.sub(r"\.0$", "", s)
    return s


def normalize_article_no(value) -> str | None:
    """Article / style / color numbers look like '352634_03' or sometimes
    '352634-03' or plain '352634'. Normalize separators to underscore and
    strip whitespace so files from different regions line up."""
    v = normalize_id(value)
    if v is None:
        return None
    v = v.replace("-", "_").replace(" ", "_").strip("_")
    return v


def safe_num(value) -> float:
    value = _unwrap_scalar(value)
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
