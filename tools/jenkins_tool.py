#!/usr/bin/env python3
"""
Jenkins API client for triggering parameterized builds.

Reads JENKINS_BASE, JENKINS_USER, JENKINS_TOKEN from tools/.env.

Usage:
    # List parameters (defaults to tools/PUBLISH_GOLD_IMAGE)
    python3 tools/jenkins_tool.py params

    # Trigger a build with parameters
    python3 tools/jenkins_tool.py build \
        --param SOURCE_URL=https://hoth.corp.nutanix.com/... \
        --param DESTINATION=some_dest

    # Use a different job
    python3 tools/jenkins_tool.py build --job other/JOB_NAME --param KEY=VAL

    # Check build status
    python3 tools/jenkins_tool.py status --build-number 42
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
import base64
import ssl

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JOB = "tools/PUBLISH_GOLD_IMAGE"


def _load_env():
    """Load tools/.env into os.environ (setdefault, won't overwrite)."""
    env_path = os.path.join(TOOLS_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


def _get_env(name, default=None):
    val = os.environ.get(name, "").strip()
    if val:
        return val
    _load_env()
    val = os.environ.get(name, "").strip()
    return val if val else default


def _auth_header():
    user = _get_env("JENKINS_USER")
    token = _get_env("JENKINS_TOKEN")
    if not user or not token:
        print("ERROR: JENKINS_USER and JENKINS_TOKEN must be set in tools/.env", file=sys.stderr)
        sys.exit(1)
    creds = base64.b64encode(f"{user}:{token}".encode()).decode()
    return f"Basic {creds}"


def _jenkins_base():
    base = _get_env("JENKINS_BASE")
    if not base:
        print("ERROR: JENKINS_BASE must be set in tools/.env", file=sys.stderr)
        sys.exit(1)
    return base.rstrip("/")


def _job_url(job_path):
    """Convert slash-separated job path to Jenkins URL path.

    e.g. 'tools/PUBLISH_GOLD_IMAGE' -> '/job/tools/job/PUBLISH_GOLD_IMAGE'
    """
    parts = job_path.strip("/").split("/")
    return "/job/" + "/job/".join(parts)


def _ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request(url, method="GET", data=None, headers=None):
    hdrs = {"Authorization": _auth_header()}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    ctx = _ssl_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp.headers, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, e.headers, body


def get_job_params(job_path):
    """Fetch parameter definitions for a Jenkins job."""
    base = _jenkins_base()
    url = f"{base}{_job_url(job_path)}/api/json?tree=property[parameterDefinitions[name,type,defaultParameterValue[value],choices,description]]"
    status, _, body = _request(url)
    if status != 200:
        print(f"ERROR: Failed to fetch job info (HTTP {status}): {body[:500]}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(body)
    params = []
    for prop in data.get("property", []):
        for pdef in prop.get("parameterDefinitions", []):
            p = {
                "name": pdef.get("name"),
                "type": pdef.get("type", ""),
                "default": pdef.get("defaultParameterValue", {}).get("value", ""),
                "description": pdef.get("description", ""),
            }
            if "choices" in pdef:
                p["choices"] = pdef["choices"]
            params.append(p)
    return params


def trigger_build(job_path, params_dict):
    """Trigger a parameterized Jenkins build.

    Returns (queue_url, message) on success.
    """
    base = _jenkins_base()

    if params_dict:
        url = f"{base}{_job_url(job_path)}/buildWithParameters"
        encoded = urllib.parse.urlencode(params_dict).encode("utf-8")
    else:
        url = f"{base}{_job_url(job_path)}/build"
        encoded = None

    status, resp_headers, body = _request(url, method="POST", data=encoded,
                                          headers={"Content-Type": "application/x-www-form-urlencoded"})

    if status in (200, 201):
        queue_url = resp_headers.get("Location", "")
        return queue_url, "Build triggered successfully"
    elif status == 302:
        queue_url = resp_headers.get("Location", "")
        return queue_url, "Build queued successfully"
    else:
        return None, f"Failed to trigger build (HTTP {status}): {body[:500]}"


def get_build_status(job_path, build_number):
    """Get status of a specific build."""
    base = _jenkins_base()
    url = f"{base}{_job_url(job_path)}/{build_number}/api/json?tree=result,building,timestamp,duration,displayName,url"
    status, _, body = _request(url)
    if status != 200:
        return {"error": f"HTTP {status}: {body[:500]}"}
    return json.loads(body)


def resolve_queue_to_build(queue_url, timeout=120, poll_interval=5):
    """Poll a Jenkins queue item until it gets a build number.

    Jenkins queues builds before they start; this waits for the queue
    item to be assigned an executor and returns the build number.

    Returns build_number (int) or None if timed out.
    """
    import time
    api_url = queue_url.rstrip("/") + "/api/json"
    elapsed = 0
    while elapsed < timeout:
        status, _, body = _request(api_url)
        if status == 200:
            data = json.loads(body)
            executable = data.get("executable")
            if executable and executable.get("number"):
                return executable["number"]
            if data.get("cancelled"):
                return None
        time.sleep(poll_interval)
        elapsed += poll_interval
    return None


def wait_for_build(job_path, build_number, timeout=600, poll_interval=15):
    """Poll a Jenkins build until it completes.

    Returns a dict with keys: success (bool), result (str), duration (int),
    url (str), error (str or None).
    """
    import time
    elapsed = 0
    while elapsed < timeout:
        info = get_build_status(job_path, build_number)
        if "error" in info:
            return {"success": False, "result": None, "error": info["error"]}
        if not info.get("building", True):
            result = info.get("result", "UNKNOWN")
            return {
                "success": result == "SUCCESS",
                "result": result,
                "duration": info.get("duration", 0),
                "url": info.get("url", ""),
                "error": None if result == "SUCCESS" else f"Build finished with: {result}",
            }
        time.sleep(poll_interval)
        elapsed += poll_interval
    return {"success": False, "result": "TIMEOUT", "error": f"Build did not complete within {timeout}s"}


def get_crumb():
    """Fetch Jenkins crumb for CSRF protection (if enabled)."""
    base = _jenkins_base()
    url = f"{base}/crumbIssuer/api/json"
    status, _, body = _request(url)
    if status == 200:
        data = json.loads(body)
        return data.get("crumbRequestField"), data.get("crumb")
    return None, None


def main():
    parser = argparse.ArgumentParser(description="Jenkins API client")
    sub = parser.add_subparsers(dest="command", required=True)

    p_params = sub.add_parser("params", help="List job parameters")
    p_params.add_argument("--job", default=DEFAULT_JOB,
                          help=f"Job path (default: {DEFAULT_JOB})")

    p_build = sub.add_parser("build", help="Trigger a build")
    p_build.add_argument("--job", default=DEFAULT_JOB,
                          help=f"Job path (default: {DEFAULT_JOB})")
    p_build.add_argument("--param", "-p", action="append", default=[],
                         help="Parameter as KEY=VALUE (repeatable)")
    p_build.add_argument("--dry-run", action="store_true",
                         help="Show what would be sent without triggering")

    p_status = sub.add_parser("status", help="Check build status")
    p_status.add_argument("--job", default=DEFAULT_JOB,
                          help=f"Job path (default: {DEFAULT_JOB})")
    p_status.add_argument("--build-number", "-n", required=True, help="Build number")

    args = parser.parse_args()

    if args.command == "params":
        params = get_job_params(args.job)
        if not params:
            print("No parameters found for this job.")
            return
        print(f"\nParameters for {args.job}:\n")
        print(f"{'Name':<30} {'Type':<25} {'Default':<20} Description")
        print("-" * 100)
        for p in params:
            default = str(p.get("default", "")) or "-"
            desc = p.get("description", "") or "-"
            print(f"{p['name']:<30} {p['type']:<25} {default:<20} {desc}")
            if "choices" in p:
                print(f"{'':>30} Choices: {', '.join(p['choices'])}")
        print()

    elif args.command == "build":
        params_dict = {}
        for kv in args.param:
            if "=" not in kv:
                print(f"ERROR: Invalid param format '{kv}', expected KEY=VALUE", file=sys.stderr)
                sys.exit(1)
            k, _, v = kv.partition("=")
            params_dict[k] = v

        if args.dry_run:
            base = _jenkins_base()
            print(f"[DRY RUN] Would trigger: POST {base}{_job_url(args.job)}/buildWithParameters")
            print(f"[DRY RUN] Parameters: {json.dumps(params_dict, indent=2)}")
            return

        queue_url, msg = trigger_build(args.job, params_dict)
        print(msg)
        if queue_url:
            print(f"Queue URL: {queue_url}")

    elif args.command == "status":
        info = get_build_status(args.job, args.build_number)
        print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
