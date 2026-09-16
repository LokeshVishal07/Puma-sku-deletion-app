"""
Marketplace SKU Deletion Finder
--------------------------------
Finds SKUs listed on Lazada / Shopee / Zalora / TikTok that qualify for
deletion because they had zero stock all through 2025 (Jan/Jun/Dec
snapshots) and today, AND are not tracked in the Content file or the
ZeCom master tracker.

Run locally:    streamlit run app.py
"""
from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from modules.io_utils import (
    detect_data_start_row,
    detect_header_row,
    get_excel_sheet_names,
    guess_column,
    load_csv_with_header,
    load_excel_with_header,
    normalize_id,
    read_raw_excel,
    read_zip_of_excels,
)
from modules.matching import (
    STOCK_CHECKPOINTS,
    MarketplaceRow,
    build_content_lookup,
    build_stock_lookup,
    build_zecom_article_set,
    evaluate,
)

st.set_page_config(page_title="Marketplace SKU Deletion Finder", layout="wide")

REGIONS = ["SG", "MY", "PH"]
PLATFORMS_BY_REGION = {
    "SG": ["Lazada", "Shopee", "Zalora"],
    "MY": ["Lazada", "Shopee", "Zalora", "TikTok"],
    "PH": ["Lazada", "Shopee", "Zalora"],
}

if "tables" not in st.session_state:
    st.session_state["tables"] = {}  # cache: key -> DataFrame


# --------------------------------------------------------------------------
# Generic upload -> header-confirm -> DataFrame helper
# --------------------------------------------------------------------------

def _read_one(uploaded_file, key_prefix: str, label_hint: str) -> pd.DataFrame | None:
    name = uploaded_file.name.lower()
    if name.endswith(".csv"):
        raw = pd.read_csv(uploaded_file, header=None, nrows=30)
        default_header = detect_header_row(raw)
        header_row = st.number_input(
            f"Header row for **{label_hint}** ({uploaded_file.name})",
            min_value=0, max_value=max(19, default_header),
            value=default_header, key=f"{key_prefix}_hdr",
        )
        default_extra = max(0, detect_data_start_row(raw, header_row) - header_row - 1)
        extra_skip = st.number_input(
            "Extra instruction/blank rows to skip right after the header "
            "(auto-detected — adjust only if the preview below looks wrong)",
            min_value=0, max_value=10, value=default_extra, key=f"{key_prefix}_skip",
        )
        return load_csv_with_header(uploaded_file, header_row, header_row + 1 + extra_skip)

    sheets = get_excel_sheet_names(uploaded_file)
    if len(sheets) == 1:
        sheet = sheets[0]
    else:
        sheet = st.selectbox(
            f"Sheet for **{label_hint}** ({uploaded_file.name})",
            sheets, key=f"{key_prefix}_sheet",
        )
    raw = read_raw_excel(uploaded_file, sheet, nrows=30)
    default_header = detect_header_row(raw)
    header_row = st.number_input(
        f"Header row (0-indexed) for **{label_hint}** / sheet `{sheet}`",
        min_value=0, max_value=max(19, default_header),
        value=default_header, key=f"{key_prefix}_hdr",
    )
    default_extra = max(0, detect_data_start_row(raw, header_row) - header_row - 1)
    extra_skip = st.number_input(
        "Extra instruction/blank rows to skip right after the header "
        "(auto-detected — adjust only if the preview below looks wrong)",
        min_value=0, max_value=10, value=default_extra, key=f"{key_prefix}_skip",
    )
    return load_excel_with_header(uploaded_file, sheet, header_row, header_row + 1 + extra_skip)


def smart_upload(
    label: str,
    key_prefix: str,
    roles: list[tuple[str, str]],
    accept_zip: bool = False,
) -> tuple[pd.DataFrame, dict] | None:
    """Show an uploader + header confirmation + column-role mapping.

    roles: list of (role_key, human_label), e.g. [("ean", "EAN column"), ...]
    Returns (dataframe, {role_key: column_name}) or None if nothing uploaded.
    """
    file_types = ["xlsx", "xls", "csv"] + (["zip"] if accept_zip else [])
    uploaded = st.file_uploader(label, type=file_types, key=f"{key_prefix}_file")
    if uploaded is None:
        return None

    with st.expander(f"⚙️ {label} — header row & column mapping", expanded=False):
        if uploaded.name.lower().endswith(".zip"):
            inner_files = read_zip_of_excels(uploaded)
            if not inner_files:
                st.error("No .xlsx/.csv files found inside the zip.")
                return None
            st.caption(f"Found {len(inner_files)} files inside the zip — consolidating.")
            frames = []
            for i, (inner_name, raw_bytes) in enumerate(inner_files):
                bio = io.BytesIO(raw_bytes)
                bio.name = inner_name
                if i == 0:
                    df0 = _read_one(bio, f"{key_prefix}_z0", f"{label} (sample: {inner_name})")
                    frames.append(df0)
                    # Re-derive header row/sheet choice for the rest silently
                    header_row = st.session_state.get(f"{key_prefix}_z0_hdr", 0)
                    sheet_key = f"{key_prefix}_z0_sheet"
                    sheet = st.session_state.get(sheet_key)
                else:
                    try:
                        sheets = get_excel_sheet_names(bio)
                        sh = sheet if sheet in sheets else sheets[0]
                        frames.append(load_excel_with_header(bio, sh, header_row))
                    except Exception as e:  # noqa: BLE001
                        st.warning(f"Skipped {inner_name}: {e}")
            df = pd.concat(frames, ignore_index=True, sort=False)
            df = df.dropna(axis=1, how="all")
        else:
            df = _read_one(uploaded, key_prefix, label)

        st.dataframe(df.head(5), use_container_width=True)
        st.caption(f"{len(df):,} rows loaded.")

        cols = list(df.columns)
        mapping = {}
        for role_key, role_label in roles:
            guess = guess_column(cols, role_key)
            options = ["-- none --"] + cols
            default_idx = options.index(guess) if guess in options else 0
            choice = st.selectbox(
                role_label, options, index=default_idx, key=f"{key_prefix}_{role_key}"
            )
            mapping[role_key] = None if choice == "-- none --" else choice

    return df, mapping


def status_from_filename_choice(key_prefix: str) -> str | None:
    return st.radio(
        "This file's listing status", ["active", "inactive"],
        key=f"{key_prefix}_status_choice", horizontal=True,
    )


# --------------------------------------------------------------------------
# Sidebar navigation
# --------------------------------------------------------------------------

st.title("🗑️ Marketplace SKU Deletion Finder")
st.caption(
    "Cross-checks SKUs listed on Lazada / Shopee / Zalora / TikTok against "
    "your Content file, ZeCom tracker, and stock history to flag SKUs with "
    "zero stock all through 2025 that aren't tracked anywhere — candidates "
    "for delisting."
)

step = st.sidebar.radio(
    "Steps",
    ["1. Master files", "2. Marketplace files", "3. Stock files", "4. Run & results"],
)

master = st.session_state.setdefault("master", {})
mkt = st.session_state.setdefault("mkt", {})
stock = st.session_state.setdefault("stock", {})

# --------------------------------------------------------------------------
# STEP 1 — Master files: Content file + ZeCom trackers
# --------------------------------------------------------------------------
if step == "1. Master files":
    st.header("1. Master files")
    st.markdown(
        "**Content file** — maps EAN to Article/Color No (shared across all regions)."
    )
    result = smart_upload(
        "Content file",
        "content",
        roles=[("ean", "EAN column"), ("article_no", "Article / Color No column")],
    )
    if result:
        master["content_df"], master["content_map"] = result
        st.success("Content file loaded.")

    st.divider()
    st.markdown(
        "**ZeCom tracker — PH** (its own workbook, sheet with PIM Article#)."
    )
    result = smart_upload(
        "ZeCom tracker — PH",
        "zecom_ph",
        roles=[("article_no", "Article No column (PIM Article# / Article#)")],
    )
    if result:
        master["zecom_ph_df"], master["zecom_ph_map"] = result
        st.success("ZeCom PH loaded.")

    st.divider()
    st.markdown(
        "**ZeCom tracker — MY & SG** (one workbook, separate `MY` and `SG` sheets — "
        "pick the sheet inside the mapping panel below for each region)."
    )
    result = smart_upload(
        "ZeCom tracker — MY",
        "zecom_my",
        roles=[("article_no", "Article No column (Style#)")],
    )
    if result:
        master["zecom_my_df"], master["zecom_my_map"] = result
        st.success("ZeCom MY loaded.")

    result = smart_upload(
        "ZeCom tracker — SG",
        "zecom_sg",
        roles=[("article_no", "Article No column (STYLE#)")],
    )
    if result:
        master["zecom_sg_df"], master["zecom_sg_map"] = result
        st.success("ZeCom SG loaded.")

# --------------------------------------------------------------------------
# STEP 2 — Marketplace files (region tabs)
# --------------------------------------------------------------------------
elif step == "2. Marketplace files":
    st.header("2. Marketplace files (the SKUs actually listed)")
    tabs = st.tabs(REGIONS)
    for region, tab in zip(REGIONS, tabs):
        with tab:
            platforms = PLATFORMS_BY_REGION[region]
            for platform in platforms:
                st.subheader(f"{platform} — {region}")
                roles = [
                    ("seller_sku", "Seller SKU column"),
                    ("ean", "EAN column (often the same as Seller SKU)"),
                    ("platform_product_id", "Platform Product ID / Listing ID column (needed to action the delete on-platform)"),
                    ("product_title", "Product title column"),
                    ("status", "Status column (active/inactive), if present"),
                ]
                if platform == "TikTok":
                    st.caption(
                        "TikTok gives separate exports per status — upload the "
                        "active-listings file and the inactive-listings file "
                        "below; each is tagged automatically."
                    )
                    r_active = smart_upload(
                        f"{platform} {region} — ACTIVE listings file",
                        f"{platform}_{region}_active",
                        roles=roles,
                        accept_zip=(platform == "Shopee"),
                    )
                    if r_active:
                        mkt[f"{platform}_{region}_active"] = (*r_active, "active")
                        st.success("Active file loaded.")
                    r_inactive = smart_upload(
                        f"{platform} {region} — INACTIVE listings file",
                        f"{platform}_{region}_inactive",
                        roles=roles,
                        accept_zip=(platform == "Shopee"),
                    )
                    if r_inactive:
                        mkt[f"{platform}_{region}_inactive"] = (*r_inactive, "inactive")
                        st.success("Inactive file loaded.")
                else:
                    result = smart_upload(
                        f"{platform} {region} file",
                        f"{platform}_{region}",
                        roles=roles,
                        accept_zip=(platform == "Shopee"),
                    )
                    if result:
                        mkt[f"{platform}_{region}"] = (*result, None)
                        st.success(f"{platform} {region} loaded.")
                st.divider()

# --------------------------------------------------------------------------
# STEP 3 — Stock files (current + 3x 2025 snapshots, per region)
# --------------------------------------------------------------------------
elif step == "3. Stock files":
    st.header("3. Inventory / stock files")
    st.caption(
        "Upload the current inventory file for each region now, plus the "
        "Jan / Jun / Dec 2025 snapshots as you get them. Missing checkpoints "
        "will just show as 'Review — missing data' instead of being guessed."
    )
    tabs = st.tabs(REGIONS)
    for region, tab in zip(REGIONS, tabs):
        with tab:
            for cp in STOCK_CHECKPOINTS:
                st.subheader(f"{region} — {cp}")
                roles = [("ean", "EAN column"), ("stock", "Stock quantity column")]
                result = smart_upload(
                    f"{region} inventory — {cp}",
                    f"stock_{region}_{cp.replace(' ', '_')}",
                    roles=roles,
                )
                if result:
                    stock[f"{region}_{cp}"] = result
                    st.success(f"{region} {cp} loaded.")
                st.divider()

# --------------------------------------------------------------------------
# STEP 4 — Run & results
# --------------------------------------------------------------------------
elif step == "4. Run & results":
    st.header("4. Run the analysis")

    region_choice = st.selectbox("Region to analyze", REGIONS)

    if st.button("▶ Run analysis", type="primary"):
        # --- Content lookup (shared) ---
        if "content_df" not in master:
            st.error("Upload the Content file in Step 1 first.")
            st.stop()
        content_map = master["content_map"]
        if not content_map.get("ean") or not content_map.get("article_no"):
            st.error("Map both the EAN and Article No columns for the Content file.")
            st.stop()
        content_lookup = build_content_lookup(
            master["content_df"], content_map["ean"], content_map["article_no"]
        )

        # --- ZeCom article set for the chosen region ---
        zecom_key = {"PH": "zecom_ph", "MY": "zecom_my", "SG": "zecom_sg"}[region_choice]
        zdf_key, zmap_key = f"{zecom_key}_df", f"{zecom_key}_map"
        if zdf_key not in master:
            st.error(f"Upload the ZeCom tracker for {region_choice} in Step 1 first.")
            st.stop()
        zmap = master[zmap_key]
        if not zmap.get("article_no"):
            st.error("Map the Article No column for the ZeCom tracker.")
            st.stop()
        zecom_set = build_zecom_article_set(master[zdf_key], zmap["article_no"])

        # --- Stock lookups for the chosen region ---
        stock_lookups = {}
        for cp in STOCK_CHECKPOINTS:
            entry = stock.get(f"{region_choice}_{cp}")
            if entry is None:
                stock_lookups[cp] = None
                continue
            sdf, smap = entry
            if not smap.get("ean") or not smap.get("stock"):
                st.warning(f"Skipping {cp} — EAN/stock column not mapped.")
                stock_lookups[cp] = None
                continue
            stock_lookups[cp] = build_stock_lookup(sdf, smap["ean"], smap["stock"])

        # --- Marketplace rows for the chosen region ---
        rows: list[MarketplaceRow] = []
        for platform in PLATFORMS_BY_REGION[region_choice]:
            matching_keys = [
                k for k in mkt
                if k.startswith(f"{platform}_{region_choice}")
            ]
            if not matching_keys:
                continue
            for k in matching_keys:
                mdf, mmap, forced_status = mkt[k]
                if not mmap.get("seller_sku"):
                    st.warning(f"Skipping {k} — Seller SKU column not mapped.")
                    continue
                for _, row in mdf.iterrows():
                    seller_sku = normalize_id(row.get(mmap["seller_sku"]))
                    if not seller_sku:
                        continue
                    ean = None
                    if mmap.get("ean"):
                        ean = normalize_id(row.get(mmap["ean"]))
                    ean = ean or seller_sku
                    title = ""
                    if mmap.get("product_title"):
                        title = str(row.get(mmap["product_title"], "") or "")
                    status_val = forced_status
                    if status_val is None and mmap.get("status"):
                        status_val = str(row.get(mmap["status"], "") or "")
                    pid_val = None
                    if mmap.get("platform_product_id"):
                        pid_val = normalize_id(row.get(mmap["platform_product_id"]))
                    rows.append(
                        MarketplaceRow(
                            platform=platform,
                            region=region_choice,
                            seller_sku=seller_sku,
                            ean=ean,
                            product_title=title,
                            status=status_val,
                            platform_product_id=pid_val,
                        )
                    )

        if not rows:
            st.error(f"No marketplace files loaded for {region_choice} yet (Step 2).")
            st.stop()

        result_df = evaluate(rows, content_lookup, zecom_set, stock_lookups)
        st.session_state["result_df"] = result_df

    if "result_df" in st.session_state:
        result_df = st.session_state["result_df"]
        st.subheader("Results")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total SKUs checked", len(result_df))
        c2.metric("Delete: YES", int((result_df["Delete"] == "YES").sum()))
        c3.metric("Delete: NO", int((result_df["Delete"] == "NO").sum()))
        c4.metric(
            "Needs review (missing data)",
            int(result_df["Delete"].str.startswith("REVIEW").sum()),
        )

        decision_filter = st.multiselect(
            "Filter by decision",
            sorted(result_df["Delete"].unique()),
            default=sorted(result_df["Delete"].unique()),
        )
        platform_filter = st.multiselect(
            "Filter by platform",
            sorted(result_df["Platform"].unique()),
            default=sorted(result_df["Platform"].unique()),
        )
        filtered = result_df[
            result_df["Delete"].isin(decision_filter)
            & result_df["Platform"].isin(platform_filter)
        ]
        st.dataframe(filtered, use_container_width=True, height=500)

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            summary = (
                filtered.groupby("Platform")["Delete"]
                .value_counts()
                .unstack(fill_value=0)
                .reset_index()
            )
            summary.to_excel(writer, index=False, sheet_name="Summary")
            for platform in sorted(filtered["Platform"].unique()):
                sheet_df = filtered[filtered["Platform"] == platform]
                # Excel sheet names cap at 31 chars and can't hold most
                # special characters - platform names are short enough
                # as-is, but keep this defensive.
                sheet_name = str(platform)[:31]
                sheet_df.to_excel(writer, index=False, sheet_name=sheet_name)
        st.download_button(
            "⬇️ Download report (.xlsx — one sheet per marketplace)",
            data=buf.getvalue(),
            file_name=f"sku_deletion_report_{region_choice}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
