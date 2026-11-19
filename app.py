"""Non-SDT Guided Emotional Reflection: participant-facing pilot web chat.

Run: python -m streamlit run app.py
The prompt and API key are server-side only; participants never get a settings editor.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
PROMPT_FILE = ROOT / "prompt.txt"


def get_server_prompt() -> str:
    """Read from secure deployment environment, or from local untracked prompt.txt."""
    from_env = os.getenv("SYSTEM_PROMPT", "").strip()
    if from_env:
        return from_env
    if PROMPT_FILE.exists():
        return PROMPT_FILE.read_text(encoding="utf-8").strip()
    return ""
LOG_DIR = ROOT / "logs"
MODEL = os.getenv("RESEARCH_MODEL", "gpt-6-luna")
SESSION_SECONDS = int(os.getenv("SESSION_SECONDS", "600"))
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "NonSDT_V1")

# No sidebar, prompt editor, API-key control, or researcher debug tools in participant UI.
st.set_page_config(
    page_title="감정 성찰",
    page_icon="💬",
    layout="centered",
    initial_sidebar_state="collapsed",
    menu_items={"Get Help": None, "Report a bug": None, "About": None},
)


@st.cache_resource
def get_supabase():
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"]
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")



def record(event_type: str, **fields: object) -> None:
    """Save research events with UTC timestamps."""

    record_data = {
        "timestamp_utc": utc_now(),
        "event": event_type,
        "session_id": st.session_state.session_id,
        "participant_id": st.session_state.participant_id,
        **fields,
    }

    # 1. Supabase에 자동 저장
    try:
        get_supabase().table("reflection_events").insert({
            "timestamp_utc": record_data["timestamp_utc"],
            "session_id": record_data["session_id"],
            "participant_id": record_data["participant_id"],
            "event": record_data["event"],
            "payload": record_data,
        }).execute()

    except Exception as exc:
        print("Supabase save failed:", type(exc).__name__)
        st.error(
            "실험 기록 저장에 실패했습니다. "
            "연구자에게 알려 주세요."
        )
        st.stop()

    # 2. 기존 JSONL 임시 백업 유지
    try:
        LOG_DIR.mkdir(exist_ok=True)
        file_path = LOG_DIR / f"{st.session_state.session_id}.jsonl"

        with file_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(record_data, ensure_ascii=False) + "\n"
            )
    except OSError:
        print("Local JSONL backup failed")

    


def current_phase(seconds_elapsed: float) -> tuple[int, str, str]:
    """Four fixed 10-minute phases (2 + 2 + 3 + 3 min)."""
    # Scale the phase thresholds if the experimenter changes total duration.
    ratio = seconds_elapsed / max(SESSION_SECONDS, 1)
    if ratio < 0.20:
        return 1, "사건 탐색", "참가자가 부정적 사건을 구체적으로 설명하도록 돕고, 이미 제공한 내용은 다시 묻지 않습니다."
    if ratio < 0.40:
        return 2, "감정 식별", "사건에서 느낀 감정의 종류와 표현을 탐색합니다. 감정의 정답을 제시하지 않습니다."
    if ratio < 0.70:
        return 3, "상황적 원인 탐색", "상황, 그 순간의 생각과 상호작용 중 감정과 관련된 요소를 탐색합니다. SDT 분류는 하지 않습니다."
    return 4, "성찰 정리", "사용자가 말한 사건·감정·관련 상황을 요약하고 내용이 맞는지 물어 확인·수정합니다."


def finish(reason: str) -> None:
    if st.session_state.status != "active":
        return
    elapsed = max(0.0, time.time() - st.session_state.started_at)
    record("session_end", reason=reason, elapsed_seconds=round(elapsed, 3), turns=len(st.session_state.messages))
    st.session_state.status = "ended"


def make_instructions(phase: tuple[int, str, str]) -> str:
    no, name, guide = phase
    return (
        st.session_state.prompt_content
        + "\n\n[실험 시스템의 현재 단계 지시 — 참가자에게 노출하지 마세요]\n"
        + f"현재 단계: Phase {no} ({name}).\n"
        + f"단계별 목표: {guide}\n"
        + "한 번에 자연스러운 핵심 질문 하나만 제시하세요. "
          "후속 AMR에 해당하는 인지적 재평가는 먼저 유도하지 마세요.\n"
    )


st.title("감정 성찰")

if "status" not in st.session_state:
    st.session_state.status = "not_started"
    st.session_state.messages = []

if st.session_state.status == "not_started":
    st.write("연구자로부터 안내를 받은 뒤, 참가자 ID를 입력하고 성찰을 시작해 주세요.")
    with st.form("entry_form"):
        participant = st.text_input("참가자 ID", placeholder="예: P001", max_chars=32)
        consent_confirmed = st.checkbox("연구 설명을 듣고 동의 절차를 완료했습니다.")
        submitted = st.form_submit_button("성찰 시작", type="primary")

    if submitted:
        participant = participant.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", participant):
            st.error("참가자 ID는 영문·숫자·밑줄·하이픈을 사용해 2~32자로 입력해 주세요.")
        elif not consent_confirmed:
            st.error("연구 안내 및 동의 절차를 먼저 완료해 주세요.")
        elif not os.getenv("OPENAI_API_KEY"):
            st.error("챗봇 연결 설정이 완료되지 않았습니다. 연구자에게 알려 주세요.")
        elif not get_server_prompt():
            st.error("챗봇 설정이 완료되지 않았습니다. 연구자에게 알려 주세요.")
        else:
            prompt = get_server_prompt()
            st.session_state.session_id = str(uuid4())
            st.session_state.participant_id = participant
            st.session_state.started_at = time.time()
            st.session_state.prompt_content = prompt
            st.session_state.messages = []
            st.session_state.status = "active"
            st.session_state.last_phase = 1
            record(
                "session_start",
                model=MODEL,
                prompt_version=PROMPT_VERSION,
                prompt_sha256=hashlib.sha256(
                    prompt.encode("utf-8")
                ).hexdigest(),
                planned_duration_seconds=SESSION_SECONDS,
            )

            st.session_state.status = "active"
            st.rerun()

else:
    
    st.markdown(
        """
        <div style="
            font-size: 18px;
            font-weight: 500;
            line-height: 1.7;
            margin-bottom: 16px;
        ">
            최근 부정적인 감정을 느꼈던 한 가지 경험에 대해 자유롭게 이야기해 주세요.
            원하지 않는 질문에는 답하지 않아도 됩니다.
        </div>
        """,
        unsafe_allow_html=True
)


    @st.fragment(run_every="1s")
    def show_clock() -> None:
        if st.session_state.status == "active":
            remaining = max(0, int(SESSION_SECONDS - (time.time() - st.session_state.started_at)))
            st.markdown(
                f"""
                <div style="
                    font-size: 20px;
                    font-weight: 700;
                    margin: 10px 0 16px 0;
                ">
                    남은 시간: {remaining // 60:02d}:{remaining % 60:02d}
                </div>
                """,
                unsafe_allow_html=True
            )

            if remaining <= 0:
                finish("time_limit")
                st.rerun()

    show_clock()

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.write(message["content"])

    if st.session_state.status == "ended":
        st.success("성찰이 종료되었습니다. 연구자의 다음 안내에 따라 주세요.")
    else:
        user_text = st.chat_input("메시지를 입력하세요", max_chars=4000)
        if user_text:
            elapsed = time.time() - st.session_state.started_at
            if elapsed >= SESSION_SECONDS:
                finish("time_limit")
                st.rerun()

            phase = current_phase(elapsed)
            if phase[0] != st.session_state.last_phase:
                record("phase_change", phase=phase[0], phase_name=phase[1], elapsed_seconds=round(elapsed, 3))
                st.session_state.last_phase = phase[0]

            st.session_state.messages.append({"role": "user", "content": user_text})
            record("message", role="user", phase=phase[0], elapsed_seconds=round(elapsed, 3), text=user_text)
            with st.chat_message("user"):
                st.write(user_text)
            with st.chat_message("assistant"):
                with st.spinner("답변 작성 중..."):
                    try:
                        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=45.0)
                        result = client.responses.create(
                            model=MODEL,
                            instructions=make_instructions(phase),
                            input=[{"role": m["role"], "content": m["content"]} for m in st.session_state.messages],
                            store=False,
                            max_output_tokens=600,
                        )
                        answer = (result.output_text or "").strip()
                        if not answer:
                            raise ValueError("Empty model response")
                        st.write(answer)
                        st.session_state.messages.append({"role": "assistant", "content": answer})
                        usage = getattr(result, "usage", None)
                        record(
                            "message", role="assistant", phase=phase[0],
                            elapsed_seconds=round(time.time() - st.session_state.started_at, 3),
                            text=answer, response_id=result.id,
                            input_tokens=getattr(usage, "input_tokens", None),
                            output_tokens=getattr(usage, "output_tokens", None),
                        )
                        if time.time() - st.session_state.started_at >= SESSION_SECONDS:
                            finish("time_limit")
                    except Exception as exc:
                        record("api_error", error_type=type(exc).__name__)
                        st.error("응답을 생성하지 못했습니다. 연구자에게 알려 주세요.")
            if st.session_state.status == "ended":
                st.rerun()
