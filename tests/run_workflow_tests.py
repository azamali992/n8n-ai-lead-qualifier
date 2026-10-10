"""End-to-end test of the real workflow in a real n8n, without any real accounts.

    python tests/run_workflow_tests.py

What it does:
  1. Starts a mock Gemini API on localhost that answers like the real one
     (and fails on purpose for some leads).
  2. Builds a test copy of the workflow from scripts/build_workflow.py where
     only Slack, Gmail and Google Sheets are swapped for HTTP calls to the
     mock (same node names), so the test sees exactly what each would have
     received. The webhook, validation, AI request, parsing, routing,
     fallback and responses are the production nodes, unchanged.
  3. Imports and publishes it into a fresh n8n, starts the server, POSTs
     7 leads to the real webhook, and checks responses and side effects.

Needs n8n on PATH (npm install -g n8n). Uses a throwaway N8N_USER_FOLDER and
its own ports, so it doesn't touch your normal n8n.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from build_workflow import build  # noqa: E402

TEST_KEY = "test-key-123"

LEADS = [
    # name, email, message, expected outcome
    ("Dana Ruiz", "dana@harbor-dental.com",
     "We run 6 dental clinics and need a patient portal with online booking and an AI assistant for FAQs. "
     "Budget is around $25,000 and we'd like to launch by March.", "hot"),
    ("Mark Ellis", "mark@ellisplumbing.co",
     "Thinking about maybe redoing our website at some point. Not sure on budget yet.", "warm"),
    ("Sam", "sam@gmail.com", "just curious how much a one page site costs, I'm a student", "cold"),
    ("Link Builder", "outreach@seo-boost.biz",
     "Hi! We offer high DA backlinks and guest posts to boost your SEO. Reply for our price list.", "spam"),
    ("Priya Nair", "priya@nairlogistics.in",
     "We need our order intake automated, FAIL_AI. Budget 10k, ASAP.", "review"),
    ("Tom Becker", "tom@beckerhvac.com",
     "Automate our quote follow ups please, we lose leads every week. BAD_JSON", "review"),
    ("No Email", "not-an-email", "Please call me about a website project", "invalid"),
]
SHEET_COLUMNS = ["Lead ID", "Received", "Category", "Score", "Name", "Email", "Company", "Website", "Intent",
                 "Budget", "Timeline", "Summary", "Reasons", "Message", "Source", "Qualified by"]


# --------------------------------------------------------------------------- mock Gemini + sinks

class Mock(BaseHTTPRequestHandler):
    calls: list[dict] = []
    sinks: dict[str, list[dict]] = {"slack": [], "gmail": [], "sheets": []}

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/sink/"):
            Mock.sinks[self.path.split("/")[2]].append(body)
            return self._send(200, {"ok": True})

        Mock.calls.append({"path": self.path, "key": self.headers.get("x-goog-api-key"), "body": body})
        lead_text = body["contents"][0]["parts"][0]["text"]
        if self.headers.get("x-goog-api-key") != TEST_KEY:
            return self._send(401, {"error": {"message": "API key not valid"}})
        if "FAIL_AI" in lead_text:
            return self._send(503, {"error": {"message": "The model is overloaded"}})
        if "BAD_JSON" in lead_text:
            return self._send(200, {"candidates": [{"content": {"parts": [{"text": "Sure! The score is high"}]}}]})

        if "backlinks" in lead_text:
            result = dict(is_spam=True, score=3, intent="selling SEO backlinks")
        elif "$25,000" in lead_text:
            result = dict(is_spam=False, score=91, intent="patient portal with booking and AI assistant")
        elif "maybe" in lead_text:
            result = dict(is_spam=False, score=58, intent="possible website redesign")
        else:
            result = dict(is_spam=False, score=12, intent="one page site price check")
        result.update(budget="not stated", timeline="not stated", summary=result["intent"],
                      reasons=["test reason"], reply_draft="Hi, thanks for reaching out.")
        self._send(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(result)}]}}]})

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# --------------------------------------------------------------------------- test variant

SINKS = {"Slack: hot lead alert": "slack", "Gmail: draft reply": "gmail", "Log to Google Sheets": "sheets"}


def sink_node(original: dict, url: str) -> dict:
    """HTTP node that posts what the real Slack/Gmail node would have sent (same expressions)."""
    p = original["parameters"]
    if original["name"].startswith("Slack"):
        fields = {"channel": p["channelId"]["value"], "text": p["text"]}
        body = {"specifyBody": "keypair", "bodyParameters": {"parameters": [
            {"name": k, "value": v} for k, v in fields.items()]}}
    elif original["name"].startswith("Gmail"):
        fields = {"to": p["options"]["sendTo"], "subject": p["subject"], "message": p["message"]}
        body = {"specifyBody": "keypair", "bodyParameters": {"parameters": [
            {"name": k, "value": v} for k, v in fields.items()]}}
    else:  # Sheets: the row is the incoming item
        body = {"specifyBody": "json", "jsonBody": "={{ JSON.stringify($json) }}"}
    return {"id": original["id"], "name": original["name"], "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
            "position": original["position"], "onError": original.get("onError", "stopWorkflow"),
            "parameters": {"method": "POST", "url": url, "sendBody": True, **body, "options": {}}}


def test_variant(mock_url: str) -> dict:
    wf = copy.deepcopy(build())
    wf["name"] = "AI lead qualifier (test)"
    wf["id"] = "leadQualifierTest"
    nodes = []
    for n in wf["nodes"]:
        if n["name"] in SINKS:
            n = sink_node(n, f"{mock_url}/sink/{SINKS[n['name']]}")
        elif n["name"] == "Config":
            for a in n["parameters"]["assignments"]["assignments"]:
                if a["name"] == "geminiBaseUrl":
                    a["value"] = mock_url
        elif n["name"] == "Qualify with Gemini":
            n["retryOnFail"] = False  # keep the test fast; retries are production behaviour
        nodes.append(n)
    wf["nodes"] = nodes
    return wf


# --------------------------------------------------------------------------- helpers

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def n8n_bin() -> str:
    # On Windows npm installs n8n as n8n.cmd, which subprocess can't find by bare name.
    path = shutil.which("n8n")
    if not path:
        raise SystemExit("n8n not found on PATH. Install it with: npm install -g n8n")
    return path


def run_n8n(args: list[str], env: dict) -> str:
    proc = subprocess.run([n8n_bin(), *args], env=env, capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        raise RuntimeError(f"n8n {' '.join(args)} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    return proc.stdout


def post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode(errors="replace")[:300]}


def wait_ready(base: str, proc: subprocess.Popen) -> None:
    for _ in range(150):
        if proc.poll() is not None:
            raise RuntimeError("n8n exited during startup")
        try:
            with urllib.request.urlopen(base + "/healthz/readiness", timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1)
    raise RuntimeError("n8n did not become ready")


# --------------------------------------------------------------------------- main

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # arrows in output on Windows consoles
    mock = HTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_url = f"http://127.0.0.1:{mock.server_port}"
    port = free_port()
    responses: dict[str, tuple[int, dict]] = {}

    # ignore_cleanup_errors: on Windows n8n can hold its sqlite file for a moment after exit
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
        env = {**os.environ, "N8N_USER_FOLDER": home, "N8N_DIAGNOSTICS_ENABLED": "false",
               "N8N_RUNNERS_ENABLED": "true", "N8N_LOG_LEVEL": "error", "N8N_LISTEN_ADDRESS": "127.0.0.1",
               "N8N_ENCRYPTION_KEY": "test-encryption-key-not-secret",
               "N8N_PORT": str(port), "N8N_RUNNERS_BROKER_PORT": str(free_port())}
        wf_file, cred_file = Path(home) / "wf.json", Path(home) / "cred.json"
        wf_file.write_text(json.dumps(test_variant(mock_url)))
        cred_file.write_text(json.dumps([{"id": "geminiHeaderAuth", "name": "Gemini API key",
                                          "type": "httpHeaderAuth",
                                          "data": {"name": "x-goog-api-key", "value": TEST_KEY}}]))
        print("importing into a fresh n8n…")
        run_n8n(["import:credentials", f"--input={cred_file}"], env)
        run_n8n(["import:workflow", f"--input={wf_file}"], env)
        run_n8n(["publish:workflow", "--id=leadQualifierTest"], env)

        print("starting n8n…")
        proc = subprocess.Popen([n8n_bin(), "start"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = f"http://127.0.0.1:{port}"
            wait_ready(base, proc)
            time.sleep(2)  # let the webhook register after startup
            print("sending 7 leads to the webhook…\n")
            for name, email, message, _ in LEADS:
                payload = {"name": name, "email": email, "message": message, "company": email.split("@")[-1]}
                responses[email] = post(f"{base}/webhook/lead-intake", payload)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
    mock.shutdown()

    return report(responses)


def report(responses: dict[str, tuple[int, dict]]) -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = ""):
        checks.append((name, ok, detail))

    expected = {e: exp for _, e, _, exp in LEADS}
    status = {e: r[0] for e, r in responses.items()}
    body = {e: r[1] for e, r in responses.items()}

    check("invalid email → HTTP 400 with reason",
          status.get("not-an-email") == 400 and "valid email" in json.dumps(body.get("not-an-email")),
          str(responses.get("not-an-email")))
    check("AI called once per valid lead", len(Mock.calls) == 6, f"{len(Mock.calls)} calls")
    check("API key sent in x-goog-api-key header", all(c["key"] == TEST_KEY for c in Mock.calls))
    check("model from Config used in URL", all("gemini-flash-latest:generateContent" in c["path"] for c in Mock.calls))
    first = Mock.calls[0]["body"] if Mock.calls else {}
    gen = first.get("generationConfig", {})
    check("structured JSON output requested",
          gen.get("responseMimeType") == "application/json" and "score" in gen.get("responseSchema", {}).get("properties", {}))
    check("company profile in system prompt", "Northbeam Studio" in json.dumps(first.get("systemInstruction", {})))
    check("full email address not sent to the AI (domain only)",
          all("dana@harbor-dental.com" not in json.dumps(c["body"]) for c in Mock.calls))

    sinks = Mock.sinks
    check("hot lead → one Slack alert with score",
          len(sinks["slack"]) == 1 and "Hot lead (91/100)" in sinks["slack"][0].get("text", "")
          and sinks["slack"][0].get("channel") == "#sales-leads", f"{sinks['slack']}"[:300])
    draft = sinks["gmail"][0] if sinks["gmail"] else {}
    check("hot lead → one Gmail draft to the lead, with the AI reply",
          len(sinks["gmail"]) == 1 and draft.get("to") == "dana@harbor-dental.com"
          and draft.get("message") == "Hi, thanks for reaching out." and "harbor-dental.com" in draft.get("subject", ""),
          str(sinks["gmail"])[:300])
    check("spam → 200 OK, nothing logged or alerted",
          status.get("outreach@seo-boost.biz") == 200 and body.get("outreach@seo-boost.biz") == {"ok": True}
          and all(r.get("Email") != "outreach@seo-boost.biz" for r in sinks["sheets"]),
          str(responses.get("outreach@seo-boost.biz")))

    rows = sinks["sheets"]
    logged = {r.get("Email"): r for r in rows}
    want = {e for e, c in expected.items() if c in {"hot", "warm", "cold", "review"}}
    check("every real lead logged to the sheet exactly once",
          sorted(logged) == sorted(want) and len(rows) == len(want), str(sorted(map(str, logged))))
    for email in sorted(want):
        got = (logged.get(email) or {}).get("Category")
        resp = body.get(email, {}).get("category")
        check(f"{email}: sheet + response say '{expected[email]}'",
              got == expected[email] and resp == expected[email], f"sheet={got} response={body.get(email)}")
    check("AI outage → kept for manual review", logged.get("priya@nairlogistics.in", {}).get("Qualified by") == "fallback")
    check("garbage AI output → kept for manual review", logged.get("tom@beckerhvac.com", {}).get("Qualified by") == "fallback")
    check("score passed through as a number", logged.get("dana@harbor-dental.com", {}).get("Score") == 91)
    check("sheet rows have exactly the expected columns", bool(rows) and all(list(r) == SHEET_COLUMNS for r in rows))
    check("every response carries the lead id", all(str(body.get(e, {}).get("id", "")).startswith("L-") for e in want))

    width = max(len(n) for n, _, _ in checks)
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {'' if ok else detail}")
    failed = sum(not ok for _, ok, _ in checks)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
