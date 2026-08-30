"""
Article Scraper - Streamlit GUI

Run with:
    streamlit run app.py

NocoDB settings come pre-filled and are saved to config.json so you never
re-enter them. Proxy is a single on/off checkbox - the actual pool of 10
proxies lives in proxies.txt and rotates automatically, one per URL.
Browser fallback, headless mode, and request delay are fully automatic.
"""

import io

import pandas as pd
import streamlit as st

from scraper_core import (
    CSV_FIELDS,
    Settings,
    check_nocodb,
    check_proxy_pool,
    load_proxy_pool,
    load_settings,
    parse_urls,
    save_settings,
    scrape_urls_stream,
)

st.set_page_config(page_title="Article Scraper", page_icon="📰", layout="wide")

if "settings" not in st.session_state:
    st.session_state.settings = load_settings()
    # Persist immediately so config.json exists with the baked-in defaults
    # from the very first run, even before the user clicks Save.
    save_settings(st.session_state.settings)

if "results" not in st.session_state:
    st.session_state.results = []  # list of article dicts (CSV_FIELDS)

if "run_log" not in st.session_state:
    st.session_state.run_log = []

s = st.session_state.settings
proxy_pool = load_proxy_pool()


# ============================================================
# SIDEBAR - SETTINGS
# ============================================================

with st.sidebar:
    st.header("⚙️ Settings")
    st.caption("Saved automatically to config.json — you only need to touch this if something changes.")

  
     

    with st.expander("Proxy", expanded=True):
        
        s.nocodb_base_url = s.nocodb_base_url
        s.nocodb_table_id = s.nocodb_table_id
        s.nocodb_api_key = s.nocodb_api_key
        
        
        s.use_proxy = st.checkbox(f"Use proxy ({len(proxy_pool)} loaded, rotating automatically)", value=s.use_proxy)
        st.caption("Proxies come from proxies.txt. Each URL uses the next proxy in the list, round-robin.")

        if st.button("Test proxies", key="test_proxy", use_container_width=True):
            with st.spinner("Testing all proxies..."):
                ok, msg = check_proxy_pool(s)
            (st.success if ok else st.error)(msg)

    st.caption("Playwright fallback, headless mode, and request delay are all automatic — no setup needed.")

    if st.button("💾 Save settings", use_container_width=True, type="primary"):
        save_settings(s)
        st.success("Settings saved to config.json")


# ============================================================
# MAIN - INPUT
# ============================================================

st.title("📰 Article Scraper")
st.caption("Extract article metadata + content from a list of URLs, then optionally push each row to NocoDB.")

tab_run, tab_results = st.tabs(["Run", "Results"])

with tab_run:
    input_mode = st.radio("URL source", ["Paste URLs", "Upload .txt file"], horizontal=True)

    urls_text = ""
    if input_mode == "Paste URLs":
        urls_text = st.text_area(
            "One URL per line (lines starting with # are ignored)",
            height=200,
            placeholder="https://example.com/article-1\nhttps://example.com/article-2",
        )
    else:
        uploaded = st.file_uploader("Upload a .txt file with one URL per line", type=["txt"])
        if uploaded is not None:
            urls_text = uploaded.read().decode("utf-8", errors="ignore")
            st.text_area("Preview", value=urls_text, height=150, disabled=True)

    urls = parse_urls(urls_text) if urls_text else []
    st.caption(f"{len(urls)} URL(s) detected")

    upload_to_nocodb = st.checkbox(
        "Upload each result to NocoDB as it's scraped",
        value=True,
        help="Uncheck to only build the results table / CSV without touching NocoDB.",
    )

    run_disabled = len(urls) == 0
    if st.button("▶️ Start scraping", type="primary", disabled=run_disabled):
        st.session_state.results = []
        st.session_state.run_log = []

        progress_bar = st.progress(0.0)
        status_line = st.empty()
        log_box = st.empty()
        table_box = st.empty()

        for result in scrape_urls_stream(urls, s, upload_to_nocodb=upload_to_nocodb):
            st.session_state.results.append(result["article"])
            st.session_state.run_log.extend(result["log"])

            progress_bar.progress(result["index"] / result["total"])
            status_line.write(
                f"**[{result['index']}/{result['total']}]** {result['url']}  \n"
                f"method: `{result['method']}` · proxy: `{result['proxy']}` · nocodb: `{result['nocodb_status']}`"
                + (f" — {result['nocodb_error']}" if result["nocodb_error"] else "")
            )
            log_box.code("\n".join(st.session_state.run_log[-25:]), language=None)

            df_partial = pd.DataFrame(st.session_state.results, columns=CSV_FIELDS)
            table_box.dataframe(df_partial, use_container_width=True, height=300)

        st.success(f"Done. Processed {len(urls)} URL(s).")


# ============================================================
# RESULTS TAB
# ============================================================

with tab_results:
    if not st.session_state.results:
        st.info("No results yet. Run a scrape from the Run tab.")
    else:
        df = pd.DataFrame(st.session_state.results, columns=CSV_FIELDS)
        st.dataframe(df, use_container_width=True, height=450)

        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)
        st.download_button(
            "⬇️ Download CSV",
            data=csv_buffer.getvalue().encode("utf-8-sig"),
            file_name="articles.csv",
            mime="text/csv",
        )

        if st.button("🗑️ Clear results"):
            st.session_state.results = []
            st.session_state.run_log = []
            st.rerun()
