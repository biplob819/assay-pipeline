"""Assay console: attack an AI agent that reads public assay data, and watch the guard and the
audit log answer (D6.2). Run locally with `uv run --extra console streamlit run
apps/console/streamlit_app.py`; settings are environment variables (see
`assay_pipeline.console.page`)."""

from __future__ import annotations

import logging
import uuid

import streamlit as st

from assay_pipeline.agent.ask import AGENTS
from assay_pipeline.console.engine import MAX_QUESTION_CHARS, ConsoleError
from assay_pipeline.console.guard import GuardRefused
from assay_pipeline.console.page import (
    GENERIC_ERROR,
    BudgetLine,
    Settings,
    SettingsError,
    audit_view,
    build_console,
    decision_rows,
    sample_label,
)

st.set_page_config(page_title="Assay console", layout="wide")
LOG = logging.getLogger("assay_pipeline.console")


@st.cache_resource(max_entries=2)
def _console(settings: Settings):  # keyed on the settings: changed secrets build a new console
    return build_console(settings), BudgetLine()


def _show(result) -> None:
    st.markdown(f"**{result.model}** · {result.mode} · stop: `{result.stop_reason}` · "
                f"{result.latency_ms} ms · ${result.cost_usd:.4f}")  # fmt: skip
    st.text(result.reply)  # plain text: model output is never rendered as markdown or HTML
    left, right = st.columns(2)
    with left:
        st.subheader("Guard decisions")
        rows = decision_rows(result)
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
        else:
            st.caption("The agent ran no query.")
    with right:
        st.subheader("New audit lines")
        st.dataframe(audit_view(result), hide_index=True, width="stretch")


def _run(**kw) -> None:
    try:
        _show(console.run_case(**kw))
    except (ConsoleError, GuardRefused) as exc:
        st.error(str(exc))
    except Exception as exc:  # never a traceback for a visitor
        LOG.warning("console case failed (%s)", type(exc).__name__)
        st.error(GENERIC_ERROR)


st.title("Assay console")
st.caption(
    "An AI agent answers questions about public EGFR bioactivity data through a SQL guard and "
    "read-only access. Pick a sample to replay it through the real guard (no API call), or try "
    "your own question in live mode. The table the attacks aim at, `embargoed_results`, is "
    "synthetic. Logs here are in memory and vanish on restart; only aggregates (time, model, "
    "counts, cost) are kept."
)
settings = None
try:
    settings = Settings.from_env()
    console, budget = _console(settings)
except SettingsError as exc:
    st.error(str(exc))
    st.stop()
session = st.session_state.setdefault("session", uuid.uuid4().hex)

replay, live = st.tabs(["Replay a recorded attack", "Live"])
with replay:
    samples = list(console.samples.values())
    pick = st.selectbox("Sample attack", samples, format_func=sample_label)
    st.text(pick.question)
    if st.button("Replay", key="replay"):
        _run(sample_id=pick.id)
with live:
    line = st.empty()  # filled after any request, so it never shows a stale balance
    if console.guard is not None:
        offered = [a for a in AGENTS if a in settings.agents]
        agent = st.radio("Agent", offered, horizontal=True, format_func=lambda a: AGENTS[a][0])
        question = st.text_area("Question", max_chars=MAX_QUESTION_CHARS)
        passcode = st.text_input("Passcode", type="password")
        if st.button("Ask", key="ask"):
            _run(question=question, agent=agent, session=session, passcode=passcode)
            budget.invalidate()
    line.info(budget(console, settings))
