"""ProspectGPT Dashboard — Streamlit frontend for the lead enrichment API."""

import os
import time

import requests
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
POLL_INTERVAL_SECONDS = 2
POLL_TIMEOUT_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 10

st.set_page_config(
    page_title="ProspectGPT Dashboard",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)


def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("# 🎯 ProspectGPT")
        st.markdown("### *B2B Lead Enrichment Engine*")
        st.markdown("---")
        st.markdown(
            """
            **ProspectGPT** turns a bare company domain into a
            ready-to-send cold outreach pitch:

            1. 🔍 **Scrapes** the company's website
            2. 🧠 **Analyzes** value prop, audience & pain points
            3. ✍️ **Drafts** a personalized PAS outreach message

            Paste a domain, hit **Analyze Company**, and grab your
            pitch in under a minute.
            """
        )
        st.markdown("---")
        st.caption(f"Backend: `{API_BASE_URL}`")


def submit_domain(domain: str) -> dict | None:
    try:
        response = requests.post(
            f"{API_BASE_URL}/api/v1/pitches/enrich",
            json={"domain": domain},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.ConnectionError:
        st.error("🔌 Could not reach the backend server. Is the API running?")
        return None
    except requests.exceptions.Timeout:
        st.error("⏱️ The backend took too long to respond. Please try again.")
        return None

    if response.status_code == 202:
        return response.json()
    if response.status_code == 422:
        st.error("⚠️ That doesn't look like a valid domain. Try something like `example.com`.")
        return None
    if response.status_code == 404:
        st.error("⚠️ The enrichment endpoint was not found. Check the backend version.")
        return None
    st.error(f"❌ The server returned an unexpected error (HTTP {response.status_code}). Please try again later.")
    return None


def fetch_pitch(pitch_id: str) -> dict | None:
    try:
        response = requests.get(
            f"{API_BASE_URL}/api/v1/pitches/{pitch_id}",
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.ConnectionError:
        st.error("🔌 Lost connection to the backend while checking status.")
        return None
    except requests.exceptions.Timeout:
        st.error("⏱️ Status check timed out. Please try again.")
        return None

    if response.status_code == 200:
        return response.json()
    if response.status_code == 404:
        st.error("⚠️ This pitch no longer exists on the server.")
        return None
    st.error(f"❌ Status check failed (HTTP {response.status_code}). Please try again later.")
    return None


def poll_until_done(pitch_id: str) -> dict | None:
    status_placeholder = st.empty()
    deadline = time.time() + POLL_TIMEOUT_SECONDS

    with st.spinner("Scraping the site and drafting your pitch — hang tight..."):
        while time.time() < deadline:
            data = fetch_pitch(pitch_id)
            if data is None:
                status_placeholder.empty()
                return None

            pitch_status = data.get("status", "unknown")
            status_placeholder.info(f"⏳ Status: **{pitch_status}**...")

            if pitch_status == "completed":
                status_placeholder.empty()
                return data
            if pitch_status == "failed":
                status_placeholder.empty()
                st.error(
                    "❌ Enrichment failed — the site may be unreachable or empty. "
                    "Try a different domain."
                )
                return None

            time.sleep(POLL_INTERVAL_SECONDS)

    status_placeholder.empty()
    st.error("⏱️ Enrichment is taking longer than expected. Please try again in a few minutes.")
    return None


def render_results(data: dict) -> None:
    company = data.get("company") or {}
    analysis = data.get("analysis") or {}
    pitch_text = data.get("generated_pitch") or ""

    st.success("✅ Analysis complete!")

    st.subheader(f"🏢 {company.get('company_name') or company.get('domain', 'Company')}")
    col1, col2, col3 = st.columns(3)
    col1.metric("Domain", company.get("domain") or "—")
    col2.metric("Industry", company.get("industry") or "Unknown")
    col3.metric("Estimated Size", company.get("company_size") or "Unknown")

    st.markdown("")

    with st.expander("💎 Company Value Proposition", expanded=True):
        st.write(analysis.get("value_proposition") or "_Not available._")
        if analysis.get("target_audience"):
            st.caption(f"**Target audience:** {analysis['target_audience']}")

    with st.expander("🔥 Business Pain Points", expanded=True):
        pain_points = analysis.get("pain_points") or []
        if pain_points:
            for point in pain_points:
                st.markdown(f"- {point}")
        else:
            st.write("_No pain points extracted._")

    st.subheader("✉️ Your Personalized Outreach Pitch")
    st.caption("Click the copy icon in the top-right corner of the block below to copy it.")
    st.code(pitch_text or "No pitch was generated.", language=None)


def main() -> None:
    render_sidebar()

    st.title("🎯 ProspectGPT Dashboard")
    st.markdown(
        "Turn any company domain into a **personalized, research-backed cold outreach pitch** — "
        "powered by live website analysis and AI copywriting."
    )
    st.markdown("---")

    st.subheader("🔍 Analyze a Company")
    col_input, col_button = st.columns([4, 1], vertical_alignment="bottom")
    with col_input:
        domain = st.text_input(
            "Company domain",
            placeholder="example.com",
            help="Paste a bare domain or full URL — we'll normalize it.",
        )
    with col_button:
        analyze_clicked = st.button("🚀 Analyze Company", type="primary", use_container_width=True)

    if analyze_clicked:
        if not domain or not domain.strip():
            st.warning("Please enter a company domain first.")
            return

        queued = submit_domain(domain.strip())
        if queued is None:
            return

        st.toast(f"Queued enrichment for {queued.get('domain', domain)}", icon="🚀")
        result = poll_until_done(queued["pitch_id"])
        if result is not None:
            render_results(result)


if __name__ == "__main__":
    main()
