#!/usr/bin/env python3
"""
Education Counselor RAG System — Streamlit Workstation (gui.py)
Interactive UI for degree searching, university dossier inspection, side-by-side comparison,
pipeline execution, and system health auditing.
"""
import sys
import json
import asyncio
from pathlib import Path
from typing import Dict, Any, List

import pandas as pd
import streamlit as st

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import importlib
import src.config
import src.state
importlib.reload(src.config)
importlib.reload(src.state)

from src.config import config
from src.state import StateManager
from src.inspect_cli import (
    iter_all_records,
    find_university_record,
    search_programs,
    extract_numeric_fee,
    export_dataset,
)


# ------------------------------------------------------------------------------
# PAGE CONFIG & STYLING
# ------------------------------------------------------------------------------

st.set_page_config(
    page_title="Education Counselor Workstation",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1E3A8A;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        font-size: 1.05rem;
        color: #4B5563;
        margin-bottom: 1.5rem;
    }
    .metric-card {
        background-color: #F3F4F6;
        padding: 1rem;
        border-radius: 8px;
        border-left: 4px solid #3B82F6;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------------------
# DATA CACHING & LOADERS
# ------------------------------------------------------------------------------

@st.cache_data(ttl=10)
def get_cached_records() -> List[Dict[str, Any]]:
    """Cached list of normalized university records."""
    return list(iter_all_records())


def get_system_metrics():
    """Calculates dataset health and quota metrics."""
    sm = StateManager()
    records = get_cached_records()
    total_unis = len(records)
    total_ug = sum(len(r.get("programs", {}).get("undergraduate", [])) for r in records)
    total_gr = sum(len(r.get("programs", {}).get("graduate", [])) for r in records)
    total_phd = sum(len(r.get("programs", {}).get("postgraduate_and_phd", [])) for r in records)
    total_programs = total_ug + total_gr + total_phd
    
    portals = sum(1 for r in records if r.get("main_info", {}).get("key_links", {}).get("application_portal_url"))
    portal_cov = round((portals / max(1, total_unis)) * 100, 1)

    used_today = sm.queries_used_today()
    rem_budget = sm.remaining_query_budget()
    used_5h = sm.queries_used_in_window(5.0) if hasattr(sm, "queries_used_in_window") else 0
    rem_5h = sm.remaining_5h_budget() if hasattr(sm, "remaining_5h_budget") else getattr(config, "budget_5_hours", 75)
    notebook_mode = getattr(config, "notebook_mode", "reusable")
    pacing_delay = getattr(config, "query_pacing_delay_sec", 2.0)

    return {
        "total_unis": total_unis,
        "total_programs": total_programs,
        "portal_coverage": portal_cov,
        "used_today": used_today,
        "rem_budget": rem_budget,
        "used_5h": used_5h,
        "rem_5h": rem_5h,
        "notebook_mode": notebook_mode,
        "pacing_delay": pacing_delay,
    }


# ------------------------------------------------------------------------------
# SIDEBAR
# ------------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### 🎓 **Counselor Hub**")
    st.markdown("Official University Data Extraction & Multi-Agent RAG System")
    st.divider()

    metrics = get_system_metrics()
    st.metric("Total Universities", metrics["total_unis"])
    st.metric("Total Programs Indexed", metrics["total_programs"])
    st.metric("Portal Link Coverage", f"{metrics['portal_coverage']}%")
    st.metric("5h Rolling Budget Left", f"{metrics['rem_5h']} / {config.budget_5_hours}")
    st.metric("Daily Budget Left", f"{metrics['rem_budget']} / {config.daily_query_budget}")
    st.caption(f"⚙️ Mode: **{metrics['notebook_mode'].upper()}** | Pacing: **{metrics['pacing_delay']}s**")

    st.divider()
    if st.button("🔄 Refresh Data Cache"):
        st.cache_data.clear()
        st.rerun()


# ------------------------------------------------------------------------------
# MAIN WORKSTATION TABS
# ------------------------------------------------------------------------------

st.markdown('<div class="main-header">🎓 Education Counselor RAG Workstation</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Search degrees, audit university dossiers, launch crawls, and monitor ingestion pipelines.</div>', unsafe_allow_html=True)

tabs = st.tabs([
    "🔍 Program Finder",
    "🏛️ University Dossier",
    "⚔️ Compare Universities",
    "🚀 Run Crawler Pipeline",
    "📊 System & Audit State",
])


# ==============================================================================
# TAB 1: PROGRAM FINDER
# ==============================================================================

with tabs[0]:
    st.subheader("🔍 Global Degree Program Finder")
    st.write("Search all indexed undergraduate, graduate, and doctoral degrees with real-time fee filtering.")

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        search_kw = st.text_input("Search Keyword (e.g., Computer Science, Data, Economics, MBA)", value="")
    with col2:
        level_filter = st.selectbox("Degree Level", ["All", "undergraduate", "graduate", "postgraduate_phd"])
    with col3:
        max_fee_input = st.number_input("Max Tuition Fee (PKR)", min_value=0, value=0, step=25000, help="Leave 0 for no fee limit")

    lvl_arg = None if level_filter == "All" else level_filter
    fee_arg = float(max_fee_input) if max_fee_input > 0 else None

    if search_kw.strip():
        matches = search_programs(search_kw.strip(), level=lvl_arg, max_fee=fee_arg)
        if matches:
            st.success(f"Found **{len(matches)}** matching degree program(s):")
            df = pd.DataFrame(matches)
            # Reorder columns for optimal readability
            preferred_cols = ["program_name", "degree_level", "university", "department", "tuition_fee", "duration", "application_deadline", "info_link"]
            available_cols = [c for c in preferred_cols if c in df.columns] + [c for c in df.columns if c not in preferred_cols]
            st.dataframe(df[available_cols], width="stretch", hide_index=True)

            with st.expander("📖 Program Details & 3-Line Summaries"):
                for m in matches:
                    st.markdown(f"#### {m.get('program_name')} — *{m.get('university')}*")
                    st.markdown(f"**Level:** `{m.get('degree_level')}` | **Fee:** `{m.get('tuition_fee')}` | **Duration:** `{m.get('duration')}`")
                    if m.get("info_link"):
                        st.markdown(f"🔗 [Official Program Page]({m.get('info_link')})")
                    st.divider()
        else:
            st.warning("No matching degree programs found. Try broadening your keyword or fee filter.")
    else:
        st.info("Enter a keyword above to search through indexed degree programs.")


# ==============================================================================
# TAB 2: UNIVERSITY DOSSIER
# ==============================================================================

with tabs[1]:
    st.subheader("🏛️ University Dossier & Inspection")
    records = get_cached_records()

    if not records:
        st.warning("No university data found in `data/outputs/`. Run the pipeline to harvest data.")
    else:
        uni_names = sorted(list({r.get("main_info", {}).get("name", "") for r in records if r.get("main_info", {}).get("name")}))
        selected_uni = st.selectbox("Select Institution", uni_names)

        if selected_uni:
            uni_data = find_university_record(selected_uni)
            if uni_data:
                main_info = uni_data.get("main_info", {})
                progs = uni_data.get("programs", {})
                faculties = uni_data.get("faculties", [])
                contact = uni_data.get("contact", {})
                key_links = main_info.get("key_links", {})

                # Header cards
                col_a, col_b, col_c, col_d = st.columns(4)
                col_a.metric("Type", (main_info.get("type") or "Public").capitalize())
                col_b.metric("City", main_info.get("city") or "N/A")
                col_c.metric("Country", main_info.get("country") or "Pakistan")
                col_d.metric("Domain Verified", "✅ Yes" if main_info.get("domain_verified") else "ℹ️ Standard")

                st.markdown(f"**Overview:** {main_info.get('description', 'No description available.')}")

                # Quick action links
                link_cols = st.columns(3)
                if key_links.get("application_portal_url"):
                    link_cols[0].link_button("🚀 Application Portal", key_links["application_portal_url"])
                if key_links.get("admissions_url"):
                    link_cols[1].link_button("📋 Admissions Desk", key_links["admissions_url"])
                if main_info.get("website"):
                    link_cols[2].link_button("🌐 Main Website", main_info["website"])

                st.divider()

                # Program breakdown tabs
                prog_tab1, prog_tab2, prog_tab3, fac_tab, contact_tab = st.tabs([
                    f"Undergraduate ({len(progs.get('undergraduate', []))})",
                    f"Graduate ({len(progs.get('graduate', []))})",
                    f"PhD / Postgrad ({len(progs.get('postgraduate_and_phd', []))})",
                    f"Faculties ({len(faculties)})",
                    "📞 Contact & Helpdesk",
                ])

                with prog_tab1:
                    ug = progs.get("undergraduate", [])
                    if ug:
                        st.dataframe(pd.DataFrame(ug)[["name", "department", "duration", "tuition_fee", "summary_3_lines"]], width="stretch", hide_index=True)
                    else:
                        st.write("No undergraduate programs recorded.")

                with prog_tab2:
                    gr = progs.get("graduate", [])
                    if gr:
                        st.dataframe(pd.DataFrame(gr)[["name", "department", "duration", "tuition_fee", "summary_3_lines"]], width="stretch", hide_index=True)
                    else:
                        st.write("No graduate programs recorded.")

                with prog_tab3:
                    phd = progs.get("postgraduate_and_phd", [])
                    if phd:
                        st.dataframe(pd.DataFrame(phd)[["name", "department", "duration", "tuition_fee", "summary_3_lines"]], width="stretch", hide_index=True)
                    else:
                        st.write("No postgraduate/PhD programs recorded.")

                with fac_tab:
                    if faculties:
                        for f in faculties:
                            st.markdown(f"#### 🏫 {f.get('faculty_name')}")
                            if f.get("description"):
                                st.write(f.get("description"))
                            depts = f.get("departments", [])
                            if depts:
                                st.markdown("**Departments:** " + ", ".join(f"`{d}`" for d in depts))
                            st.divider()
                    else:
                        st.write("No faculties recorded.")

                with contact_tab:
                    st.markdown(f"**Official Email:** `{contact.get('official_email', 'N/A')}`")
                    phones = contact.get("phone_numbers", [])
                    if phones:
                        st.markdown("**Phone Numbers:** " + ", ".join(f"`{p}`" for p in phones))
                    st.markdown(f"**Physical Address:** {contact.get('physical_address', 'N/A')}")
                    st.markdown(f"**Admissions Office:** {contact.get('admissions_office_location', 'N/A')}")


# ==============================================================================
# TAB 3: COMPARE UNIVERSITIES
# ==============================================================================

with tabs[2]:
    st.subheader("⚔️ Side-by-Side University Comparison")
    records = get_cached_records()

    if len(records) < 2:
        st.info("At least two universities must be present in the dataset to compare.")
    else:
        uni_names = sorted(list({r.get("main_info", {}).get("name", "") for r in records if r.get("main_info", {}).get("name")}))
        c1, c2 = st.columns(2)
        with c1:
            uni1_name = st.selectbox("Select First University", uni_names, index=0)
        with c2:
            uni2_name = st.selectbox("Select Second University", uni_names, index=min(1, len(uni_names) - 1))

        if uni1_name and uni2_name:
            u1 = find_university_record(uni1_name)
            u2 = find_university_record(uni2_name)

            if u1 and u2:
                m1, m2 = u1.get("main_info", {}), u2.get("main_info", {})
                p1, p2 = u1.get("programs", {}), u2.get("programs", {})

                col_left, col_right = st.columns(2)

                with col_left:
                    st.markdown(f"### 🏛️ {m1.get('name')}")
                    st.markdown(f"**Type:** {(m1.get('type') or 'N/A').capitalize()} | **City:** {m1.get('city', 'N/A')}")
                    if m1.get("key_links", {}).get("application_portal_url"):
                        st.markdown(f"🔗 [Application Portal]({m1['key_links']['application_portal_url']})")
                    st.metric("Undergraduate Programs", len(p1.get("undergraduate", [])))
                    st.metric("Graduate Programs", len(p1.get("graduate", [])))
                    st.metric("PhD Programs", len(p1.get("postgraduate_and_phd", [])))

                with col_right:
                    st.markdown(f"### 🏛️ {m2.get('name')}")
                    st.markdown(f"**Type:** {(m2.get('type') or 'N/A').capitalize()} | **City:** {m2.get('city', 'N/A')}")
                    if m2.get("key_links", {}).get("application_portal_url"):
                        st.markdown(f"🔗 [Application Portal]({m2['key_links']['application_portal_url']})")
                    st.metric("Undergraduate Programs", len(p2.get("undergraduate", [])))
                    st.metric("Graduate Programs", len(p2.get("graduate", [])))
                    st.metric("PhD Programs", len(p2.get("postgraduate_and_phd", [])))


# ==============================================================================
# TAB 4: RUN PIPELINE
# ==============================================================================

with tabs[3]:
    st.subheader("🚀 Master 4-Phase Pipeline Runner")
    st.write("Launch live crawling, ingestion, schema extraction, and audit verification directly.")

    run_mode = st.radio("Pipeline Mode", ["Single University Crawl", "Multi-Country Batch Crawl"], horizontal=True)

    if run_mode == "Single University Crawl":
        target_url = st.text_input("Target University URL", "https://nust.edu.pk")
        target_name = st.text_input("University Full Name Override (Optional)", "")
        max_links = st.slider("Max Sources to Retain", min_value=15, max_value=150, value=60, step=5)
        exclude_kw = st.text_input("Exclude Keyword Patterns", "news|events")
        c_mode, c_pdf = st.columns(2)
        with c_mode:
            ingest_mode = st.selectbox("Ingestion Mode", ["auto", "text", "url"], index=0, help="Auto detects Cloudflare/WAF and switches to local text capture automatically")
        with c_pdf:
            prospectus_pdf_input = st.text_input("Official Prospectus PDF (Optional)", "")
        uptodate = st.checkbox("Apply 2026 Recency Filter", value=True)

        if st.button("🚀 Start Single University Pipeline"):
            if not target_url.strip():
                st.error("Please enter a valid university URL.")
            else:
                with st.status(f"Executing 4-phase pipeline for {target_url}...", expanded=True) as status:
                    st.write("Initializing Crawl4AI browser & NotebookLM session...")
                    try:
                        from src.pipeline import run_master_pipeline, close_shared_crawler, close_http_client
                        
                        async def _run():
                            try:
                                await run_master_pipeline(
                                    url=target_url.strip(),
                                    uni_name_override=target_name.strip() or None,
                                    max_links=max_links,
                                    exclude_keywords=exclude_kw,
                                    uptodate=uptodate,
                                    mode=ingest_mode,
                                    prospectus_pdf=prospectus_pdf_input.strip() or None,
                                )
                            finally:
                                await close_shared_crawler()
                                await close_http_client()

                        asyncio.run(_run())
                        status.update(label=f"Pipeline Completed for {target_url}!", state="complete", expanded=False)
                        st.success(f"Successfully processed and indexed {target_url}!")
                        st.cache_data.clear()
                    except Exception as e:
                        status.update(label=f"Pipeline Error: {e}", state="error")
                        st.error(f"Pipeline error occurred: {e}")

    else:
        config_path = st.text_input("Batch Configuration File", "config.json")
        force_rerun = st.checkbox("Force Rerun All Configured Universities", value=False)

        if st.button("⚡ Start Multi-Country Batch Pipeline"):
            with st.status("Executing Multi-Country Batch Pipeline...", expanded=True) as status:
                try:
                    from src.pipeline import run_batch_pipeline
                    asyncio.run(run_batch_pipeline(config_file_path=Path(config_path), force_rerun_all=force_rerun))
                    status.update(label="Batch Pipeline Finished!", state="complete", expanded=False)
                    st.success("Batch pipeline finished successfully!")
                    st.cache_data.clear()
                except Exception as e:
                    status.update(label=f"Batch Error: {e}", state="error")
                    st.error(f"Batch execution error: {e}")


# ==============================================================================
# TAB 5: SYSTEM STATE & AUDIT
# ==============================================================================

with tabs[4]:
    st.subheader("📊 System State & Audit Manifest")

    sm = StateManager()
    state_rows = sm.list_all()
    audit_rows = sm.list_notebook_audits(limit=50)

    tab_used_5h = sm.queries_used_in_window(5.0) if hasattr(sm, "queries_used_in_window") else 0
    tab_rem_5h = sm.remaining_5h_budget() if hasattr(sm, "remaining_5h_budget") else getattr(config, "budget_5_hours", 75)

    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    col_m1.metric("5h Rolling Used", tab_used_5h)
    col_m2.metric("5h Rolling Left", f"{tab_rem_5h} / {getattr(config, 'budget_5_hours', 75)}")
    col_m3.metric("Daily Queries (UTC)", sm.queries_used_today())
    col_m4.metric("Daily Budget Left", f"{sm.remaining_query_budget()} / {config.daily_query_budget}")

    st.info(
        f"🤖 **Workspace Strategy**: `{getattr(config, 'notebook_mode', 'reusable').upper()}` (Single worker with source purge to protect weekly limits) &nbsp;|&nbsp; "
        f"⏱️ **Query Pacing**: `{getattr(config, 'query_pacing_delay_sec', 2.0)}s` &nbsp;|&nbsp; "
        f"🛡️ **Rate-Limit Policy**: `{'Auto-Sleep & Resume' if getattr(config, 'auto_sleep_on_rate_limit', True) else 'Strict Fail'}`"
    )

    st.markdown("### 🗄️ SQLite Pipeline State (`pipeline_state`)")
    if state_rows:
        df_state = pd.DataFrame(state_rows)
        st.dataframe(df_state, width="stretch", hide_index=True)
    else:
        st.write("No pipeline states recorded in SQLite database.")

    st.markdown("### ☁️ NotebookLM Lifecycle Audit Log (`notebook_audit`)")
    if audit_rows:
        df_audit = pd.DataFrame(audit_rows)
        st.dataframe(df_audit[["event_type", "notebook_id", "uni_slug", "created_at", "details"]], width="stretch", hide_index=True)
    else:
        st.write("No audit records found.")

    st.divider()
    st.markdown("### 📤 Dataset Export (CSV / JSON)")
    exp_col1, exp_col2 = st.columns(2)

    with exp_col1:
        if st.button("Export Master CSV"):
            csv_path = config.data_outputs_dir / "counseling_programs.csv"
            export_dataset(format_type="csv", output_path=csv_path)
            st.success(f"Exported to {csv_path.name}")

    with exp_col2:
        if st.button("Export Country-Grouped JSON"):
            json_path = config.data_outputs_dir / "university_counseling_data.json"
            export_dataset(format_type="json", output_path=json_path)
            st.success(f"Exported to {json_path.name}")

