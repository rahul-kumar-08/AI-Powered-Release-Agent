import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List
from html import escape

import streamlit as st
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


ROOT_DIR = Path(__file__).resolve().parent
RUNS_DIR = ROOT_DIR / "runs"
DATA_DIR = ROOT_DIR / "dashboard_data"
CRON_STORE = DATA_DIR / "cron_jobs.json"
RUN_STORE = DATA_DIR / "runs.json"
ENV_FILE = ROOT_DIR / "tools" / ".env"
BRANCH_OPTIONS = ["master", "ganges-7.6", "ganges-7.6.0.x", "ganges-7.5", "ganges-7.3"]
CRON_PRESETS = {
    "Every 5 minutes": "*/5 * * * *",
    "Every 15 minutes": "*/15 * * * *",
    "Hourly": "0 * * * *",
    "Daily (00:00 UTC)": "0 0 * * *",
    "Weekly (Sun 00:00 UTC)": "0 0 * * 0",
    "Monthly (1st 00:00 UTC)": "0 0 1 * *",
    "Custom": "",
}
ENV_KEYS = [
    "CURSOR_API_KEY",
    "ARTIFACTORY_USER",
    "ARTIFACTORY_TOKEN",
    "JENKINS_USER",
    "JENKINS_TOKEN",
    "GITHUB_TOKEN",
    "SOURCEGRAPH_TOKEN",
    "CONFLUENCE_TOKEN",
    "JIRA_TOKEN",
]
STORE_LOCK = threading.Lock()
ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}


@st.cache_resource
def get_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.start()
    return scheduler


def init_state() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    RUNS_DIR.mkdir(exist_ok=True)
    defaults = {
        "cron_jobs": load_json(CRON_STORE, {}),
        "selected_branches": ["master"],
        "release_count": 1,
        "latest_only": True,
        "check_confluence": True,
        "run_jenkins": False,
        "release_type": "Both",
        "chat_history": [],
        "chat_pending": None,
        "auth": {"is_authenticated": False, "role": "", "user": ""},
        "ui_density": "Comfortable",
        "cron_action_reset_requested": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def build_release_commands(params: Dict) -> List[List[str]]:
    type_map = {"PC": "pc", "AOS": "aos", "Both": "all"}
    selected_type = type_map.get(params.get("release_type", "Both"), "all")
    commands: List[List[str]] = []
    for branch in params.get("branches", []):
        cmd = ["python3", "release_query.py", "--branch", branch, "--filter", selected_type]
        if not params["latest_only"]:
            cmd += ["--count", str(params["release_count"])]
        if params["check_confluence"]:
            cmd += ["--since-confluence"]
        if not params["run_jenkins"]:
            cmd += ["--no-upload"]
        commands.append(cmd)
    return commands


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def human_time(ts: str) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return ts


def parse_iso_time(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def get_run_branches_and_type(run: Dict, cron_jobs: Dict) -> tuple[List[str], str]:
    branches = run.get("branches", [])
    run_type = run.get("release_type", "Both")
    if branches:
        return branches, run_type

    job_id = run.get("job_id", "")
    cron_job = cron_jobs.get(job_id)
    if cron_job:
        params = cron_job.get("params", {})
        return params.get("branches", []), params.get("release_type", "Both")

    if str(job_id).startswith("manual-"):
        return ["manual"], run_type
    return ["unknown"], run_type


def extract_chat_result(output: str, return_code: int) -> str:
    if not output.strip():
        return "No response received."

    lines = [ln.rstrip() for ln in output.splitlines()]
    noisy_tokens = (
        "[stage", "stage:", "executing ", "decompos", "pipeline",
        "retry", "token", "validation", "debug", "trace_id"
    )
    clean_lines = []
    for ln in lines:
        low = ln.lower().strip()
        if not low:
            continue
        if any(tok in low for tok in noisy_tokens):
            continue
        clean_lines.append(ln)

    table_block = _extract_markdown_table_block(clean_lines)
    if table_block:
        return table_block

    if return_code != 0:
        return "\n".join(clean_lines[-20:]) if clean_lines else output[-3000:]

    return "\n".join(clean_lines[-16:]) if clean_lines else output[-2000:]


def _extract_markdown_table_block(lines: List[str]) -> str:
    """Extract a single clean markdown table block from command output."""
    if not lines:
        return ""

    blocks: List[List[str]] = []
    current: List[str] = []

    def _is_table_like(line: str) -> bool:
        if "|" not in line:
            return False
        parts = [p.strip() for p in line.split("|")]
        non_empty = [p for p in parts if p]
        return len(non_empty) >= 2

    for line in lines:
        if _is_table_like(line):
            current.append(line)
        else:
            if len(current) >= 2:
                blocks.append(current)
            current = []
    if len(current) >= 2:
        blocks.append(current)

    if not blocks:
        return ""

    # Prefer the release results table when present; otherwise use the largest table.
    preferred_headers = (
        "GoldImage Version",
        "Main Ticket",
        "Change Log",
        "RPM List",
        "Merge Date",
    )
    scored_blocks = []
    for blk in blocks:
        header = blk[0]
        score = sum(1 for h in preferred_headers if h in header)
        scored_blocks.append((score, len(blk), blk))

    scored_blocks.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_block = scored_blocks[0][2]
    return "\n".join(best_block)


def _parse_markdown_table(text: str):
    """Parse a markdown table into headers and rows."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None, None
    if "|" not in lines[0] or "|" not in lines[1]:
        return None, None

    def _split_row(line: str) -> List[str]:
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]

    headers = _split_row(lines[0])
    if not headers or len(headers) < 2:
        return None, None

    rows = []
    for ln in lines[2:]:
        if "|" not in ln:
            continue
        # Skip markdown separator rows.
        if re.fullmatch(r"[\|\-\:\s]+", ln):
            continue
        cols = _split_row(ln)
        if len(cols) == len(headers):
            rows.append(cols)

    return headers, rows


def _extract_table_and_tail(text: str):
    """Split assistant content into [table, non-table tail]."""
    lines = (text or "").splitlines()
    table_start = None
    table_end = None

    for i in range(len(lines) - 1):
        if "|" in lines[i] and "|" in lines[i + 1]:
            table_start = i
            break
    if table_start is None:
        return "", text

    j = table_start
    while j < len(lines) and "|" in lines[j]:
        table_end = j
        j += 1
    if table_end is None:
        return "", text

    table_text = "\n".join(lines[table_start:table_end + 1]).strip()
    tail = "\n".join(lines[table_end + 1:]).strip()
    return table_text, tail


def _render_release_grid(table_text: str) -> bool:
    """Render release results as a wrapped single-row-per-release grid."""
    headers, rows = _parse_markdown_table(table_text)
    if not headers or not rows:
        return False

    html_lines = ['<div class="chat-release-grid-wrap"><table class="chat-release-grid"><thead><tr>']
    for h in headers:
        html_lines.append(f"<th>{escape(h)}</th>")
    html_lines.append("</tr></thead><tbody>")
    for row in rows:
        html_lines.append("<tr>")
        for cell in row:
            html_lines.append(f"<td>{escape(cell)}</td>")
        html_lines.append("</tr>")
    html_lines.append("</tbody></table></div>")
    st.markdown("".join(html_lines), unsafe_allow_html=True)
    return True


def extract_token_usage(raw_output: str, prompt: str, final_text: str) -> str:
    prompt_tokens = None
    completion_tokens = None
    total_tokens = None
    consumed = None

    m = re.search(r"prompt_tokens\s*[:=]\s*(\d+)", raw_output, flags=re.IGNORECASE)
    if m:
        prompt_tokens = int(m.group(1))
    m = re.search(r"completion_tokens\s*[:=]\s*(\d+)", raw_output, flags=re.IGNORECASE)
    if m:
        completion_tokens = int(m.group(1))
    m = re.search(r"total_tokens\s*[:=]\s*(\d+)", raw_output, flags=re.IGNORECASE)
    if m:
        total_tokens = int(m.group(1))
    m = re.search(r"tokens?\s+consumed\s*[:=]\s*(\d+)", raw_output, flags=re.IGNORECASE)
    if m:
        consumed = int(m.group(1))

    if total_tokens is not None or prompt_tokens is not None or completion_tokens is not None or consumed is not None:
        if total_tokens is None:
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
        parts = []
        if prompt_tokens is not None:
            parts.append(f"prompt={prompt_tokens}")
        if completion_tokens is not None:
            parts.append(f"completion={completion_tokens}")
        if total_tokens is not None and total_tokens > 0:
            parts.append(f"total={total_tokens}")
        if consumed is not None:
            parts.append(f"consumed={consumed}")
        return "Token usage: " + ", ".join(parts)

    # Fallback estimate when tool output doesn't expose real usage.
    est_prompt = max(1, len(prompt) // 4)
    est_completion = max(1, len(final_text) // 4)
    est_total = est_prompt + est_completion
    return f"Token usage (estimated): prompt~{est_prompt}, completion~{est_completion}, total~{est_total}"


def normalize_chat_history_item(item):
    if isinstance(item, dict):
        return {
            "role": item.get("role", "assistant"),
            "content": item.get("content", ""),
            "raw": item.get("raw", ""),
            "show_raw": bool(item.get("show_raw", False)),
        }
    if isinstance(item, tuple) and len(item) == 2:
        role, content = item
        return {"role": role, "content": content, "raw": "", "show_raw": False}
    return {"role": "assistant", "content": str(item), "raw": "", "show_raw": False}


def status_bucket(status: str) -> str:
    s = (status or "").strip().lower()
    if s.startswith("run"):
        return "running"
    if s.startswith("succ"):
        return "success"
    if s.startswith("fail"):
        return "failed"
    return s


def render_chat_toggle_hint() -> None:
    st.caption("Double-click chat header (two quick clicks) to expand/collapse.")
    now = time.time()
    if st.button("Chat Resize Area", use_container_width=True, key="chat-double-click-area"):
        last = st.session_state.get("chat_last_click_ts", 0.0)
        if now - last <= 0.6:
            current_view = st.query_params.get("chat_view", "normal")
            if isinstance(current_view, list):
                current_view = current_view[0] if current_view else "normal"
            st.query_params["chat_view"] = "normal" if current_view == "expanded" else "expanded"
            st.session_state["chat_last_click_ts"] = 0.0
            st.rerun()
        else:
            st.session_state["chat_last_click_ts"] = now


def render_chat_panel() -> None:
    st.markdown('<div class="section-title">Release Chatbot</div>', unsafe_allow_html=True)
    render_chat_toggle_hint()
    with st.container(border=True):
        st.markdown(
            '<div class="tiny-muted">Cursor-like chat flow with concise, result-focused responses.</div>',
            unsafe_allow_html=True,
        )
        default_show_raw = st.checkbox(
            "Show full raw output",
            value=False,
            key="chat-show-raw-default",
            help="When enabled, new assistant responses include full raw command output.",
        )
        chat_wrap = st.container(height=430, border=False)
        with chat_wrap:
            recent_items = [normalize_chat_history_item(i) for i in st.session_state["chat_history"][-20:]]
            for idx, item in enumerate(recent_items):
                role = item["role"]
                content = item["content"]
                with st.chat_message(role):
                    if role == "assistant" and content:
                        table_text, tail_text = _extract_table_and_tail(content)
                        rendered = _render_release_grid(table_text) if table_text else False
                        if rendered:
                            if tail_text:
                                st.markdown(tail_text)
                        else:
                            st.markdown(content)
                    else:
                        st.markdown(content if content else "(empty)")
                    if role == "assistant" and item.get("raw"):
                        show_key = f"chat-msg-show-raw-{idx}"
                        local_show = st.checkbox(
                            "Show raw output",
                            value=item.get("show_raw", False),
                            key=show_key,
                        )
                        if local_show:
                            st.code(item["raw"], language="text")

        # Pending query runner: show quick progress lines like Cursor.
        pending = st.session_state.get("chat_pending")
        if pending:
            prompt = pending.get("prompt", "")
            start_ts = pending.get("started_ts", time.time())
            with st.chat_message("assistant"):
                status_box = st.empty()
                status_box.markdown("Running agent via `agent_runner.py`...")

                process = subprocess.Popen(
                    ["python3", "agent_runner.py", "--", prompt],
                    cwd=str(ROOT_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )

                output_lines: List[str] = []
                while True:
                    line = process.stdout.readline() if process.stdout else ""
                    if line:
                        output_lines.append(line)
                    if line == "" and process.poll() is not None:
                        break
                    if not line:
                        time.sleep(0.15)
                    elapsed = int(time.time() - start_ts)
                    status_box.markdown(f"Running... ({elapsed}s)")

                return_code = process.wait()
                raw_response = "".join(output_lines)
                response = extract_chat_result(raw_response, return_code)
                token_line = extract_token_usage(raw_response, prompt, response)
                final_response = f"{response}\n\n{token_line}"

                st.session_state["chat_history"].append(
                    {
                        "role": "assistant",
                        "content": final_response.strip(),
                        "raw": raw_response.strip(),
                        "show_raw": default_show_raw,
                    }
                )
                st.session_state["chat_pending"] = None
                st.rerun()

        user_prompt = st.chat_input("Ask any release query...")
        if user_prompt:
            st.session_state["chat_history"].append({"role": "user", "content": user_prompt, "raw": "", "show_raw": False})
            st.session_state["chat_pending"] = {"prompt": user_prompt, "started_ts": time.time()}
            st.rerun()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_runs() -> Dict[str, Dict]:
    return load_json(RUN_STORE, {})


def save_runs(runs: Dict[str, Dict]) -> None:
    with STORE_LOCK:
        save_json(RUN_STORE, runs)


def run_job(job_id: str, params: Dict, source: str) -> None:
    run_id = str(uuid.uuid4())[:8]
    commands = build_release_commands(params)
    run_path = RUNS_DIR / job_id / run_id
    run_path.mkdir(parents=True, exist_ok=True)
    output_file = run_path / "output.log"
    command_lines = [" ".join(shlex.quote(x) for x in cmd) for cmd in commands]
    (run_path / "command.txt").write_text("\n".join(command_lines), encoding="utf-8")

    runs = load_runs()
    runs[run_id] = {
        "run_id": run_id,
        "job_id": job_id,
        "source": source,
        "status": "running",
        "started_at": now_utc(),
        "finished_at": "",
        "command": "\n".join(command_lines),
        "branches": params.get("branches", []),
        "release_type": params.get("release_type", "Both"),
        "output_file": str(output_file),
        "artifact_dir": str(run_path),
    }
    save_runs(runs)

    final_exit = 0
    failure_reason = ""
    try:
        with output_file.open("w", encoding="utf-8") as f:
            for i, cmd in enumerate(commands, start=1):
                f.write(f"\n===== Branch Run {i}/{len(commands)} =====\n")
                f.write(f"Command: {' '.join(cmd)}\n\n")
                f.flush()
                process = subprocess.Popen(cmd, cwd=str(ROOT_DIR), stdout=f, stderr=subprocess.STDOUT)
                exit_code = process.wait()
                if exit_code != 0:
                    final_exit = exit_code
                    if exit_code == 130:
                        failure_reason = "KeyboardInterrupt"
    except KeyboardInterrupt:
        final_exit = 130
        failure_reason = "KeyboardInterrupt"
    except Exception as e:
        final_exit = 1
        failure_reason = f"Exception: {e}"
        try:
            with output_file.open("a", encoding="utf-8") as f:
                f.write(f"\n[run_job_error] {e}\n")
        except Exception:
            pass

    runs = load_runs()
    if run_id in runs:
        runs[run_id]["status"] = "success" if final_exit == 0 else "failed"
        if failure_reason:
            runs[run_id]["failure_reason"] = failure_reason
        runs[run_id]["finished_at"] = now_utc()
        save_runs(runs)


def queue_run(job_id: str, params: Dict, source: str) -> None:
    t = threading.Thread(target=run_job, args=(job_id, params, source), daemon=True)
    t.start()


def read_env_values() -> Dict[str, str]:
    values = {k: "" for k in ENV_KEYS}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            if key in values:
                values[key] = value
    return values


def update_env_values(updates: Dict[str, str]) -> None:
    existing_lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    seen = set()
    new_lines = []

    for line in existing_lines:
        if "=" in line and not line.strip().startswith("#"):
            key, _ = line.split("=", 1)
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def load_dashboard_credentials() -> Dict[str, str]:
    env_vals = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                env_vals[key.strip()] = value.strip()

    def get_val(key: str) -> str:
        return os.environ.get(key, env_vals.get(key, ""))

    return {
        "viewer": get_val("DASHBOARD_VIEWER_PASSWORD"),
        "operator": get_val("DASHBOARD_OPERATOR_PASSWORD"),
        "admin": get_val("DASHBOARD_ADMIN_PASSWORD"),
    }


def has_role(min_role: str) -> bool:
    current = st.session_state["auth"].get("role", "")
    if current not in ROLE_ORDER:
        return False
    return ROLE_ORDER[current] >= ROLE_ORDER[min_role]


def render_login() -> None:
    st.title("Release AI Agent Dashboard")
    st.info("Sign in to continue.")
    credentials = load_dashboard_credentials()
    if not any(credentials.values()):
        st.warning(
            "Dashboard role passwords are not configured. "
            "Set DASHBOARD_VIEWER_PASSWORD / DASHBOARD_OPERATOR_PASSWORD / DASHBOARD_ADMIN_PASSWORD in tools/.env."
        )
        if st.button("Continue as Admin (insecure)", use_container_width=True):
            st.session_state["auth"] = {"is_authenticated": True, "role": "admin", "user": "local-admin"}
            st.rerun()
        return
    with st.form("login-form", clear_on_submit=False):
        username = st.text_input("Username")
        role = st.selectbox("Role", options=["viewer", "operator", "admin"])
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign In", use_container_width=True)
        if submitted:
            expected = credentials.get(role, "")
            if expected and password == expected:
                st.session_state["auth"] = {"is_authenticated": True, "role": role, "user": username or role}
                st.success(f"Signed in as {role}.")
                st.rerun()
            else:
                st.error("Invalid credentials or role password not configured.")


def open_run_details(run_id: str) -> None:
    st.query_params["view"] = "run"
    st.query_params["run_id"] = run_id


def render_run_details_page() -> None:
    run_id = st.query_params.get("run_id", "")
    runs_by_id = load_runs()
    run = runs_by_id.get(run_id)

    st.title("Run Details")
    if st.button("Back to Dashboard"):
        st.query_params.clear()
        st.rerun()

    if not run:
        st.error("Run not found.")
        return

    st.subheader(f"Run: {run_id}")
    run_branches, run_type = get_run_branches_and_type(run, st.session_state["cron_jobs"])
    st.write(
        f'**Source:** {run.get("source", "-")} | '
        f'**Status:** {run.get("status", "-")} | '
        f'**Started:** {human_time(run.get("started_at", ""))} | '
        f'**Finished:** {human_time(run.get("finished_at", ""))}'
    )
    if run.get("failure_reason"):
        st.error(f'Failure Reason: {run.get("failure_reason")}')
    st.write(f'**Branches:** {", ".join(run_branches)} | **Type:** {run_type}')
    st.code(run.get("command", ""), language="bash")

    out_path = Path(run.get("output_file", ""))
    output_text = out_path.read_text(encoding="utf-8") if out_path.exists() else "Log file not found."
    st.text_area("Terminal Output", output_text, height=360)
    if out_path.exists():
        st.download_button(
            "Download Log",
            data=out_path.read_bytes(),
            file_name=f"{run_id}-output.log",
            mime="text/plain",
            use_container_width=True,
        )

    st.markdown("**Files**")
    artifact_dir = Path(run.get("artifact_dir", ""))
    artifacts = sorted(artifact_dir.glob("*")) if artifact_dir.exists() else []
    if not artifacts:
        st.info("No files found for this run.")
    for item in artifacts:
        if item.is_file():
            st.download_button(
                f"Download {item.name}",
                data=item.read_bytes(),
                file_name=item.name,
                mime="application/octet-stream",
                key=f"run-page-dl-{run_id}-{item.name}",
            )


def _render_live_terminal_body(run_id: str, title: str, height: int) -> None:
    runs_by_id = load_runs()
    run = runs_by_id.get(run_id)
    if not run:
        st.info("Run not found.")
        return
    out_path = Path(run.get("output_file", ""))
    output_text = out_path.read_text(encoding="utf-8") if out_path.exists() else "Log file not created yet."
    st.text_area(title, output_text, height=height, key=f"live-fragment-{run_id}-{title}")
    st.caption(f"Auto-updating | Status: {run.get('status', '-')}")


@st.fragment(run_every="1s")
def render_live_terminal_fragment_1s(run_id: str, title: str, height: int = 280) -> None:
    _render_live_terminal_body(run_id, title, height)


@st.fragment(run_every="3s")
def render_live_terminal_fragment_3s(run_id: str, title: str, height: int = 280) -> None:
    _render_live_terminal_body(run_id, title, height)


@st.fragment(run_every="5s")
def render_live_terminal_fragment_5s(run_id: str, title: str, height: int = 280) -> None:
    _render_live_terminal_body(run_id, title, height)


def render_live_terminal_by_interval(run_id: str, title: str, height: int, interval: str) -> None:
    if interval == "1s":
        render_live_terminal_fragment_1s(run_id, title, height)
    elif interval == "5s":
        render_live_terminal_fragment_5s(run_id, title, height)
    else:
        render_live_terminal_fragment_3s(run_id, title, height)


def save_cron_job(name: str, cron_expr: str, params: Dict) -> None:
    scheduler = get_scheduler()
    job_id = str(uuid.uuid4())[:8]

    def scheduled_runner():
        queue_run(job_id, params, "cron")

    scheduler.add_job(
        scheduled_runner,
        CronTrigger.from_crontab(cron_expr, timezone="UTC"),
        id=job_id,
        replace_existing=True,
    )

    st.session_state["cron_jobs"][job_id] = {
        "id": job_id,
        "name": name,
        "cron_expr": cron_expr,
        "params": params,
        "created_at": now_utc(),
    }
    save_json(CRON_STORE, st.session_state["cron_jobs"])


def delete_cron_job(job_id: str) -> None:
    scheduler = get_scheduler()
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    st.session_state["cron_jobs"].pop(job_id, None)
    save_json(CRON_STORE, st.session_state["cron_jobs"])


def update_cron_job(job_id: str, new_name: str, new_expr: str, params: Dict) -> None:
    scheduler = get_scheduler()
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    def scheduled_runner():
        queue_run(job_id, params, "cron")

    scheduler.add_job(
        scheduled_runner,
        CronTrigger.from_crontab(new_expr, timezone="UTC"),
        id=job_id,
        replace_existing=True,
    )
    st.session_state["cron_jobs"][job_id]["name"] = new_name
    st.session_state["cron_jobs"][job_id]["cron_expr"] = new_expr
    st.session_state["cron_jobs"][job_id]["params"] = params
    save_json(CRON_STORE, st.session_state["cron_jobs"])


def inject_ui_style(density: str = "Comfortable") -> None:
    compact = density == "Compact"
    h1_size = "1.55rem" if compact else "1.75rem"
    h2_size = "1.18rem" if compact else "1.28rem"
    h3_size = "1.00rem" if compact else "1.08rem"
    body_size = "0.89rem" if compact else "0.95rem"
    label_size = "0.84rem" if compact else "0.9rem"
    input_size = "0.88rem" if compact else "0.93rem"
    mono_size = "0.78rem" if compact else "0.84rem"
    section_title_size = "0.92rem" if compact else "1rem"
    tiny_size = "0.76rem" if compact else "0.84rem"

    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

        :root {{
          --card-border: rgba(128, 128, 128, 0.25);
          --card-bg-light: rgba(245, 245, 245, 0.85);
          --card-bg-dark: rgba(32, 36, 44, 0.85);
          --muted: rgba(127, 127, 127, 0.95);
          --accent: #4f8df7;
        }}

        [data-testid="stAppViewContainer"] {{
          background: linear-gradient(180deg, rgba(79,141,247,0.06) 0%, rgba(0,0,0,0) 22%);
          font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
          letter-spacing: 0.1px;
        }}

        .stApp h1, .stApp h2, .stApp h3 {{
          font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
          font-weight: 700;
          letter-spacing: -0.2px;
        }}

        .stApp h1 {{ font-size: {h1_size}; }}
        .stApp h2 {{ font-size: {h2_size}; }}
        .stApp h3 {{ font-size: {h3_size}; }}

        p, label, span, div[data-testid="stCaptionContainer"] {{
          font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
          font-size: {body_size};
          line-height: 1.45;
        }}

        div[data-testid="stCaptionContainer"] {{
          font-size: 0.78rem !important;
          line-height: 1.25 !important;
          word-break: break-word;
        }}

        div[data-testid="stMarkdownContainer"] p {{
          margin-bottom: 0.25rem;
        }}

        label, .stCheckbox label, .stRadio label {{
          font-weight: 500;
        }}

        div[data-testid="stMetricLabel"] {{
          font-weight: 600;
          font-size: 0.85rem;
        }}

        div[data-testid="stMetricValue"] {{
          font-weight: 700;
          font-size: 1.1rem;
        }}

        .section-title {{
          font-weight: 700;
          font-size: {section_title_size};
          margin: 0.25rem 0 0.6rem 0;
          color: var(--accent);
          letter-spacing: 0.2px;
        }}

        .tiny-muted {{
          color: var(--muted);
          font-size: {tiny_size};
          margin-top: -0.2rem;
          margin-bottom: 0.35rem;
        }}

        div[data-testid="stButton"] > button {{
          border-radius: 9px;
          border: 1px solid rgba(79, 141, 247, 0.35);
          font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
          font-weight: 600;
          font-size: {label_size};
          padding: 0.22rem 0.45rem;
        }}

        div[data-testid="stTextInput"] input,
        div[data-testid="stTextArea"] textarea,
        div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {{
          border-radius: 9px;
          font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
          font-size: {input_size};
        }}

        div[data-testid="stMetric"] {{
          border: 1px solid var(--card-border);
          border-radius: 10px;
          padding: 0.3rem 0.55rem;
          background: rgba(79, 141, 247, 0.06);
        }}

        .status-box {{
          border-radius: 12px;
          padding: 0.55rem 0.7rem;
          border: 1px solid rgba(0,0,0,0.08);
          margin-bottom: 0.35rem;
        }}
        .status-label {{
          font-size: 0.82rem;
          font-weight: 600;
          opacity: 0.92;
          margin-bottom: 0.15rem;
        }}
        .status-value {{
          font-size: 1.25rem;
          font-weight: 700;
          line-height: 1.1;
        }}
        .status-cron {{
          background: rgba(255, 153, 51, 0.22);
          border-color: rgba(255, 153, 51, 0.45);
        }}
        .status-running {{
          background: rgba(255, 190, 92, 0.24);
          border-color: rgba(255, 190, 92, 0.45);
        }}
        .status-success {{
          background: rgba(46, 204, 113, 0.2);
          border-color: rgba(46, 204, 113, 0.45);
        }}
        .status-failed {{
          background: rgba(231, 76, 60, 0.2);
          border-color: rgba(231, 76, 60, 0.5);
        }}

        .chat-release-grid-wrap {{
          width: 100%;
          overflow-x: auto;
          border: 1px solid var(--card-border);
          border-radius: 10px;
          margin-top: 0.2rem;
        }}
        .chat-release-grid {{
          width: 100%;
          border-collapse: collapse;
          table-layout: fixed;
        }}
        .chat-release-grid th, .chat-release-grid td {{
          border-bottom: 1px solid var(--card-border);
          padding: 0.38rem 0.45rem;
          text-align: left;
          vertical-align: top;
          white-space: normal;
          word-break: break-word;
          overflow-wrap: anywhere;
          font-size: {body_size};
          line-height: 1.35;
        }}
        .chat-release-grid th {{
          font-weight: 700;
          background: rgba(79, 141, 247, 0.08);
        }}
        .chat-release-grid tr:last-child td {{
          border-bottom: none;
        }}

        pre, code, .stCodeBlock, textarea[aria-label="Terminal Output"], textarea[aria-label="Manual Run Live Output"] {{
          font-family: "JetBrains Mono", "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace !important;
          font-size: {mono_size} !important;
          line-height: 1.45 !important;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def sync_scheduler_with_store() -> None:
    scheduler = get_scheduler()
    cron_jobs = st.session_state["cron_jobs"]
    for job_id, job in cron_jobs.items():
        if scheduler.get_job(job_id):
            continue

        params = job["params"]

        def scheduled_runner(job_id=job_id, params=params):
            queue_run(job_id, params, "cron")

        scheduler.add_job(
            scheduled_runner,
            CronTrigger.from_crontab(job["cron_expr"], timezone="UTC"),
            id=job_id,
            replace_existing=True,
        )


def reset_all_cron_actions() -> None:
    # Defer actual reset to next rerun before widgets are instantiated.
    st.session_state["cron_action_reset_requested"] = True


st.set_page_config(layout="wide", page_title="Release AI Agent Dashboard")
init_state()
sync_scheduler_with_store()
inject_ui_style(st.session_state.get("ui_density", "Comfortable"))

if st.session_state["auth"].get("is_authenticated") and st.query_params.get("view") == "run":
    render_run_details_page()
    st.stop()

top_left, top_right = st.columns([5, 1])
with top_left:
    st.title("Release AI Agent Dashboard")
    runs_for_header = load_runs()
    run_values = list(runs_for_header.values())
    running_count = sum(1 for r in run_values if status_bucket(r.get("status", "")) == "running")
    success_count = sum(1 for r in run_values if status_bucket(r.get("status", "")) == "success")
    failed_count = sum(1 for r in run_values if status_bucket(r.get("status", "")) == "failed")
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.markdown(
            f'<div class="status-box status-cron"><div class="status-label">CronJobs</div><div class="status-value">{len(st.session_state["cron_jobs"])}</div></div>',
            unsafe_allow_html=True,
        )
    with m2:
        st.markdown(
            f'<div class="status-box status-running"><div class="status-label">Running</div><div class="status-value">{running_count}</div></div>',
            unsafe_allow_html=True,
        )
    with m3:
        st.markdown(
            f'<div class="status-box status-success"><div class="status-label">Success</div><div class="status-value">{success_count}</div></div>',
            unsafe_allow_html=True,
        )
    with m4:
        st.markdown(
            f'<div class="status-box status-failed"><div class="status-label">Failed</div><div class="status-value">{failed_count}</div></div>',
            unsafe_allow_html=True,
        )
with top_right:
    if st.session_state["auth"].get("is_authenticated"):
        st.caption(
            f'User: `{st.session_state["auth"].get("user")}` | Role: `{st.session_state["auth"].get("role")}`'
        )
        st.selectbox(
            "Density",
            options=["Comfortable", "Compact"],
            key="ui_density",
            help="Adjust spacing and typography density.",
        )
        if st.button("Logout", use_container_width=True):
            st.session_state["auth"] = {"is_authenticated": False, "role": "", "user": ""}
            st.rerun()
    with st.popover("Settings"):
        st.subheader("Update Environment Variables")
        if not has_role("admin"):
            st.warning("Admin role required.")
        else:
            existing = read_env_values()
            env_updates = {}
            for key in ENV_KEYS:
                env_updates[key] = st.text_input(key, value=existing.get(key, ""), type="password")
            if st.button("Apply Environment Variables", use_container_width=True):
                update_env_values(env_updates)
                st.success("Environment variables updated in tools/.env")

if not st.session_state["auth"].get("is_authenticated"):
    render_login()
    st.stop()


chat_view = st.query_params.get("chat_view", "normal")
if isinstance(chat_view, list):
    chat_view = chat_view[0] if chat_view else "normal"
chat_expanded = chat_view == "expanded"

if chat_expanded:
    render_chat_panel()
    st.info("Chat expanded view. Double-click inside chat panel to return.")
    st.stop()

left, middle, right = st.columns([1, 2, 1], gap="large")

with left:
    st.markdown('<div class="section-title">Release Parameters</div>', unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown('<div class="tiny-muted">Select branch, release scope, and execution behavior.</div>', unsafe_allow_html=True)
        st.write("**Branches**")
        selected_branches = []
        for b in BRANCH_OPTIONS:
            default_checked = b in st.session_state.get("selected_branches", ["master"])
            if st.checkbox(b, value=default_checked, key=f"branch-{b}"):
                selected_branches.append(b)
        if not selected_branches:
            st.warning("Select at least one branch.")
        st.session_state["selected_branches"] = selected_branches

        release_type = st.radio("Type", options=["PC", "AOS", "Both"], horizontal=True)
        latest_only = st.checkbox("Latest release only", value=st.session_state["latest_only"])
        release_count = st.number_input("No. of Releases", min_value=1, value=st.session_state["release_count"], step=1)
        check_confluence = st.radio("Check Confluence (lookup)", options=[True, False], horizontal=True)
        run_jenkins = st.radio(
            "Run Jenkins and update Confluence",
            options=[True, False],
            index=0 if st.session_state.get("run_jenkins", False) else 1,
            horizontal=True,
        )
        st.caption("True = run Jenkins + Confluence update, False = stage-only (no upload)")

    st.divider()
    st.markdown('<div class="section-title">CronJob</div>', unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown('<div class="tiny-muted">Create a schedule, run now, or reset values quickly.</div>', unsafe_allow_html=True)
        default_branch_name = "multi" if len(selected_branches) != 1 else selected_branches[0]
        cron_name = st.text_input("CronJob Name", value=f"release-{default_branch_name}")
        cron_preset = st.selectbox("Schedule", list(CRON_PRESETS.keys()))
        cron_expr = CRON_PRESETS[cron_preset]
        if cron_preset == "Custom":
            cron_expr = st.text_input("Custom Cron Expression", placeholder="*/30 * * * *")

        params = {
            "branches": selected_branches,
            "release_type": release_type,
            "latest_only": latest_only,
            "release_count": int(release_count),
            "check_confluence": check_confluence,
            "run_jenkins": run_jenkins,
        }

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Submit CronJob", use_container_width=True):
                if not has_role("operator"):
                    st.error("Operator or Admin role required.")
                elif not selected_branches:
                    st.error("Select at least one branch.")
                else:
                    try:
                        save_cron_job(cron_name, cron_expr, params)
                        st.success("CronJob created.")
                    except Exception as e:
                        st.error(f"Failed to create CronJob: {e}")
        with c2:
            if st.button("Run Now", use_container_width=True):
                if not has_role("operator"):
                    st.error("Operator or Admin role required.")
                elif not selected_branches:
                    st.error("Select at least one branch.")
                else:
                    manual_job_id = f"manual-{str(uuid.uuid4())[:6]}"
                    queue_run(manual_job_id, params, "manual")
                    st.success("Manual run started.")
        with c3:
            if st.button("Reset", use_container_width=True):
                st.session_state["latest_only"] = True
                st.session_state["release_count"] = 1
                st.session_state["check_confluence"] = True
                st.session_state["run_jenkins"] = False
                st.rerun()

with middle:
    st.markdown('<div class="section-title">CronJob Details</div>', unsafe_allow_html=True)
    if st.session_state.get("cron_action_reset_requested", False):
        for key in list(st.session_state.keys()):
            if key.startswith("action-"):
                st.session_state[key] = "Select"
        st.session_state["cron_action_reset_requested"] = False

    if not st.session_state["cron_jobs"]:
        st.info("No cronjobs created yet.")
    else:
        h1, h2, h3, h4, h5, h6, h7 = st.columns([1.4, 1.6, 1.2, 1.9, 1, 1.6, 2.1])
        with h1:
            st.markdown("**ID**")
        with h2:
            st.markdown("**Name**")
        with h3:
            st.markdown("**Cron**")
        with h4:
            st.markdown("**Branches**")
        with h5:
            st.markdown("**Type**")
        with h6:
            st.markdown("**Created**")
        with h7:
            st.markdown("**Actions**")

        actionable_ids = []
        for job_id, job in list(st.session_state["cron_jobs"].items()):
            action_key = f"action-{job_id}"
            if action_key not in st.session_state:
                st.session_state[action_key] = "Select"
            r1, r2, r3, r4, r5, r6, r7 = st.columns([1.4, 1.6, 1.2, 1.9, 1, 1.6, 2.1])
            with r1:
                st.caption(f"`{job_id}`")
            with r2:
                st.caption(job["name"])
            with r3:
                st.caption(job["cron_expr"])
            with r4:
                st.caption(", ".join(job["params"].get("branches", [])) or "-")
            with r5:
                st.caption(job["params"].get("release_type", "Both"))
            with r6:
                st.caption(human_time(job.get("created_at", "")))
            with r7:
                st.selectbox(
                    "Actions",
                    options=["Select", "Edit", "Run", "Delete"],
                    key=action_key,
                    label_visibility="collapsed",
                )
            if st.session_state[action_key] != "Select":
                actionable_ids.append(job_id)

        edit_ids = [jid for jid in actionable_ids if st.session_state.get(f"action-{jid}") == "Edit"]
        if len(edit_ids) > 1:
            st.warning("Choose Edit action for only one cronjob at a time.")
        if len(edit_ids) == 1:
            edit_id = edit_ids[0]
            job = st.session_state["cron_jobs"][edit_id]
            params = job.get("params", {})
            with st.container(border=True):
                st.markdown(f"**Edit CronJob Parameters:** `{edit_id}`")
                e1, e2 = st.columns(2)
                with e1:
                    edit_name = st.text_input("Name", value=job["name"], key=f"edit-name-{edit_id}")
                with e2:
                    edit_expr = st.text_input("Cron Expression", value=job["cron_expr"], key=f"edit-expr-{edit_id}")

                edit_branches = st.multiselect(
                    "Branches",
                    options=BRANCH_OPTIONS,
                    default=params.get("branches", []),
                    key=f"edit-branches-{edit_id}",
                )
                e3, e4, e5 = st.columns(3)
                with e3:
                    edit_type = st.radio(
                        "Type",
                        options=["PC", "AOS", "Both"],
                        index=["PC", "AOS", "Both"].index(params.get("release_type", "Both")),
                        key=f"edit-type-{edit_id}",
                    )
                with e4:
                    edit_latest_only = st.checkbox(
                        "Latest only",
                        value=params.get("latest_only", True),
                        key=f"edit-latest-{edit_id}",
                    )
                    edit_release_count = st.number_input(
                        "No. of Releases",
                        min_value=1,
                        value=int(params.get("release_count", 1)),
                        step=1,
                        key=f"edit-count-{edit_id}",
                    )
                with e5:
                    edit_check_confluence = st.radio(
                        "Check Confluence",
                        options=[True, False],
                        index=0 if params.get("check_confluence", True) else 1,
                        key=f"edit-confluence-{edit_id}",
                    )
                    edit_run_jenkins = st.radio(
                        "Run Jenkins",
                        options=[True, False],
                        index=0 if params.get("run_jenkins", False) else 1,
                        key=f"edit-jenkins-{edit_id}",
                    )

        if st.button("Apply", key="cron-actions-apply-bottom", use_container_width=True):
            if not has_role("operator"):
                st.error("Operator or Admin role required.")
            elif not actionable_ids:
                st.info("Choose an action from a cronjob row first.")
            elif len(actionable_ids) > 1:
                st.error("Select action for only one cronjob before applying.")
            else:
                action_id = actionable_ids[0]
                action = st.session_state.get(f"action-{action_id}", "Select")
                job = st.session_state["cron_jobs"][action_id]
                if action == "Run":
                    queue_run(action_id, job["params"], "cron")
                    st.success("CronJob triggered.")
                    reset_all_cron_actions()
                    st.rerun()
                elif action == "Delete":
                    delete_cron_job(action_id)
                    st.warning("CronJob deleted.")
                    reset_all_cron_actions()
                    st.rerun()
                elif action == "Edit":
                    updated_params = {
                        "branches": st.session_state.get(f"edit-branches-{action_id}", job["params"].get("branches", [])),
                        "release_type": st.session_state.get(f"edit-type-{action_id}", job["params"].get("release_type", "Both")),
                        "latest_only": st.session_state.get(f"edit-latest-{action_id}", job["params"].get("latest_only", True)),
                        "release_count": int(st.session_state.get(f"edit-count-{action_id}", job["params"].get("release_count", 1))),
                        "check_confluence": st.session_state.get(
                            f"edit-confluence-{action_id}",
                            job["params"].get("check_confluence", True),
                        ),
                        "run_jenkins": st.session_state.get(
                            f"edit-jenkins-{action_id}",
                            job["params"].get("run_jenkins", False),
                        ),
                    }
                    if not updated_params["branches"]:
                        st.error("At least one branch is required for Edit.")
                    else:
                        try:
                            update_cron_job(
                                action_id,
                                st.session_state.get(f"edit-name-{action_id}", job["name"]),
                                st.session_state.get(f"edit-expr-{action_id}", job["cron_expr"]),
                                updated_params,
                            )
                            st.success("CronJob updated.")
                            reset_all_cron_actions()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to update CronJob: {e}")

    st.divider()
    st.markdown('<div class="section-title">Run History</div>', unsafe_allow_html=True)
    st.markdown('<div class="tiny-muted">Open a dedicated page for any historical run.</div>', unsafe_allow_html=True)
    history_runs = sorted(load_runs().values(), key=lambda x: x.get("started_at", ""), reverse=True)
    if not history_runs:
        st.info("No historical runs yet.")
    else:
        hs1, hs2, hs3, hs4, hs5 = st.columns([1.8, 1.2, 1.2, 1.2, 1.2])
        with hs1:
            run_search = st.text_input(
                "Search runs",
                value="",
                placeholder="Search by run id, job id, source, status, command...",
                key="history-search",
            ).strip().lower()
        with hs2:
            history_status = st.selectbox(
                "History Status",
                options=["all", "running", "success", "failed"],
                index=0,
                key="history-status",
            )
        with hs3:
            history_branch = st.selectbox(
                "History Branch",
                options=["all", "manual"] + BRANCH_OPTIONS,
                index=0,
                key="history-branch",
            )
        with hs4:
            history_type = st.selectbox(
                "History Type",
                options=["all", "PC", "AOS", "Both"],
                index=0,
                key="history-type",
            )
        with hs5:
            page_size = st.selectbox("Rows per page", options=[5, 10, 20, 50], index=1, key="history-page-size")

        filtered_history = []
        for run in history_runs:
            run_branches, run_type = get_run_branches_and_type(run, st.session_state["cron_jobs"])
            if history_status != "all" and run.get("status") != history_status:
                continue
            if history_branch != "all" and history_branch not in run_branches:
                continue
            if history_type != "all" and run_type != history_type:
                continue
            blob = " ".join(
                [
                    str(run.get("run_id", "")),
                    str(run.get("job_id", "")),
                    str(run.get("source", "")),
                    str(run.get("status", "")),
                    ",".join(run_branches),
                    run_type,
                    str(run.get("command", "")),
                ]
            ).lower()
            if run_search and run_search not in blob:
                continue
            filtered_history.append(run)

        total = len(filtered_history)
        total_pages = max(1, (total + page_size - 1) // page_size)
        nav1, nav2, nav3 = st.columns([1, 2, 1])
        with nav1:
            prev_page = st.button("Prev", key="history-prev", use_container_width=True)
        with nav3:
            next_page = st.button("Next", key="history-next", use_container_width=True)
        if "history_page" not in st.session_state:
            st.session_state["history_page"] = 1
        if prev_page:
            st.session_state["history_page"] = max(1, st.session_state["history_page"] - 1)
        if next_page:
            st.session_state["history_page"] = min(total_pages, st.session_state["history_page"] + 1)
        if st.session_state["history_page"] > total_pages:
            st.session_state["history_page"] = total_pages
        with nav2:
            st.caption(f"Page {st.session_state['history_page']} of {total_pages}  |  {total} matching runs")

        start = (st.session_state["history_page"] - 1) * page_size
        end = start + page_size
        page_runs = filtered_history[start:end]
        if not page_runs:
            st.info("No runs match current history filters.")
        else:
            hh1, hh2, hh3, hh4, hh5, hh6, hh7 = st.columns([1, 1.2, 1.55, 1.2, 1.65, 1.65, 1.25])
            with hh1:
                st.markdown("*ID*")
            with hh2:
                st.markdown("**Status**")
            with hh3:
                st.markdown("**Branch**")
            with hh4:
                st.markdown("**Type**")
            with hh5:
                st.markdown("**Start**")
            with hh6:
                st.markdown("**Completed**")
            with hh7:
                st.markdown("**Open**")
        for run in page_runs:
            run_branches, run_type = get_run_branches_and_type(run, st.session_state["cron_jobs"])
            h1, h2, h3, h4, h5, h6, h7 = st.columns([1, 1.2, 1.55, 1.2, 1.65, 1.65, 1.25])
            with h1:
                st.caption(f'`{run["run_id"]}`')
            with h2:
                status_text = run.get("status", "-")
                if status_text == "failed" and run.get("failure_reason"):
                    status_text = f'{status_text} ({run.get("failure_reason")})'
                st.markdown(
                    f'<div style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 0.78rem;">{status_text}</div>',
                    unsafe_allow_html=True,
                )
            with h3:
                st.caption(", ".join(run_branches))
            with h4:
                st.caption(run_type)
            with h5:
                st.caption(human_time(run.get("started_at", "")))
            with h6:
                st.caption(human_time(run.get("finished_at", "")))
            with h7:
                if st.button("Open", key=f'open-run-{run["run_id"]}', use_container_width=True):
                    open_run_details(run["run_id"])
                    st.rerun()

    st.divider()
    st.markdown('<div class="section-title">Terminals / Logs</div>', unsafe_allow_html=True)
    st.markdown('<div class="tiny-muted">Single live terminal view for selected run (manual or cron).</div>', unsafe_allow_html=True)
    runs_by_id = load_runs()
    runs = list(runs_by_id.values())
    if not runs:
        st.info("No runs yet.")
    else:
        filtered_runs = sorted(runs, key=lambda x: x["started_at"], reverse=True)

        latest_failed = None
        failed_runs = [r for r in filtered_runs if r.get("status") == "failed"]
        if failed_runs:
            latest_failed = sorted(failed_runs, key=lambda x: x.get("started_at", ""), reverse=True)[0]["run_id"]

        quick1, quick2 = st.columns([2, 3])
        with quick1:
            use_latest_failed = st.button(
                "View Latest Failed Run",
                use_container_width=True,
                disabled=latest_failed is None,
            )
        with quick2:
            if latest_failed:
                st.caption(f"Latest failed run: `{latest_failed}`")
            else:
                st.caption("No failed runs in current filter.")

        selectable_runs = sorted(filtered_runs, key=lambda x: x["started_at"], reverse=True)
        if not selectable_runs:
            st.info("No runs match current filters.")
            st.stop()

        default_index = 0
        if use_latest_failed and latest_failed:
            for i, r in enumerate(selectable_runs):
                if r["run_id"] == latest_failed:
                    default_index = i
                    break

        run_selector = st.selectbox(
            "Switch Terminal View",
            options=[r["run_id"] for r in selectable_runs],
            index=default_index,
            format_func=lambda rid: f'{rid} | {runs_by_id[rid]["source"]} | {runs_by_id[rid]["status"]}',
        )
        selected_run = runs_by_id[run_selector]
        st.write(
            f'**Started:** {human_time(selected_run["started_at"])} | '
            f'**Finished:** {human_time(selected_run["finished_at"])} | '
            f'**Status:** {selected_run["status"]}'
        )
        if selected_run.get("failure_reason"):
            st.error(f'Failure Reason: {selected_run.get("failure_reason")}')
        lt1, lt2 = st.columns([2, 1.3])
        with lt1:
            live_tail = st.checkbox("Live tail auto-refresh", value=False)
        with lt2:
            live_interval = st.selectbox("Interval", options=["1s", "3s", "5s"], index=1, key="logs-live-interval")
        st.code(selected_run["command"], language="bash")
        out_path = Path(selected_run["output_file"])
        if live_tail and selected_run["status"] == "running":
            render_live_terminal_by_interval(
                selected_run["run_id"],
                "Terminal Output",
                height=280,
                interval=live_interval,
            )
        else:
            output_text = out_path.read_text(encoding="utf-8") if out_path.exists() else "Log file not created yet."
            st.text_area("Terminal Output", output_text, height=280)
        if out_path.exists():
            st.download_button(
                "Download Log",
                data=out_path.read_bytes(),
                file_name=f"{run_selector}-output.log",
                mime="text/plain",
            )

        artifacts = sorted(Path(selected_run["artifact_dir"]).glob("*"))
        st.write("**Files:**")
        for item in artifacts:
            if item.is_file():
                st.download_button(
                    f"Download {item.name}",
                    data=item.read_bytes(),
                    file_name=item.name,
                    mime="application/octet-stream",
                    key=f"dl-{run_selector}-{item.name}",
                )

with right:
    if not chat_expanded:
        render_chat_panel()
    else:
        st.info("Chat is expanded in center view. Double-click the toggle there to restore.")
