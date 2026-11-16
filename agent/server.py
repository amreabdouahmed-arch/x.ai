"""Chat-screen server for the EpiGuard public-health verifier.

    py agent/server.py                            # http://127.0.0.1:8733/
    py agent/server.py --host 0.0.0.0 --port 8733 # LAN: others use http://YOUR-IP:8733/
    py agent/server.py --no-browser

Standard library only. Answers come from live medical evidence or an honest
abstention.
Conversations persist per browser session under agent/conversations/.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from agent import ask  # noqa: E402

CONV = HERE / "conversations"
CONV.mkdir(exist_ok=True)
FEEDBACK = HERE / "feedback.jsonl"
_RATE = {}
_RATE_LOCK = threading.Lock()
RATE_WINDOW = 60
RATE_LIMIT = max(1, int(os.environ.get("EPIGUARD_RATE_LIMIT", "20")))
MAX_BODY_BYTES = max(1024, int(os.environ.get(
    "EPIGUARD_MAX_BODY_BYTES", "20000")))
MAX_TEXT_CHARS = max(100, int(os.environ.get(
    "EPIGUARD_MAX_TEXT_CHARS", "4000")))


def _warm_cache() -> None:
    """Fetch curated WHO pages once at startup so the first real question
    hits warm cache instead of cold network + 429 backoffs."""
    try:
        import tools
        for _label, url in tools.TRUSTED_PAGES:
            try:
                tools._fetch_text(url, timeout=10)
            except Exception:
                continue
    except Exception:
        pass


def _sid(raw: str) -> str:
    raw = (raw or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", raw):
        return raw
    return uuid.uuid4().hex[:16]


def _save_turn(sid: str, turn: dict) -> None:
    try:
        with open(CONV / f"{sid}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"when": datetime.datetime.now().isoformat(timespec="seconds"),
                                 **turn}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load_turns(sid: str, limit: int = 100) -> list:
    try:
        lines = (CONV / f"{sid}.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _list_sessions(limit: int = 20) -> list:
    rows = []
    try:
        files = sorted(CONV.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for p in files[:limit]:
        try:
            first, last, n = "", "", 0
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                n += 1
                if not first:
                    first = str(d.get("input", ""))[:60]
                last = d.get("when", "")
            rows.append({"sid": p.stem, "first": first, "turns": n, "last": last})
        except OSError:
            continue
    return rows


def _allowed(client: str) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        recent = [stamp for stamp in _RATE.get(client, []) if now - stamp < RATE_WINDOW]
        if len(recent) >= RATE_LIMIT:
            _RATE[client] = recent
            return False
        recent.append(now)
        _RATE[client] = recent
        return True


def _delete_session(sid: str) -> bool:
    path = CONV / f"{sid}.jsonl"
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


WEB = HERE / "web"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/healthz", "/readyz"):
            return self._send(200, b'{"status":"ok"}', "application/json")
        if parsed.path in ("/", "/index.html"):
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if parsed.path == "/api/sessions":
            return self._send(200, json.dumps(_list_sessions()).encode("utf-8"),
                               "application/json")
        if parsed.path == "/api/history":
            sid = _sid(parse_qs(parsed.query).get("session", [""])[0])
            return self._send(200, json.dumps({"session": sid,
                                               "turns": _load_turns(sid)}).encode("utf-8"),
                               "application/json; charset=utf-8")
        if parsed.path == "/api/privacy":
            return self._send(200, json.dumps({
                "retention": "جلسات المتصفح تحفظ محليًا على الخادم حتى يحذفها المستخدم أو المشغل.",
                "ip_storage": False,
                "delete_endpoint": "DELETE /api/history?session=<id>",
            }, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if not _allowed(self.client_address[0]):
            return self._send(429, b'{"error":"rate limit exceeded"}', "application/json")
        if self.path == "/api/feedback":
            return self._feedback()
        if self.path != "/api/chat":
            return self._send(404, b'{"error":"not found"}', "application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY_BYTES:
            return self._send(413, b'{"error":"too long"}', "application/json")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        if not isinstance(payload, dict):
            return self._send(400, b'{"error":"json object required"}', "application/json")
        text = str(payload.get("text", "")).strip()
        if len(text) > MAX_TEXT_CHARS:
            return self._send(413, b'{"error":"text too long"}', "application/json")
        sid = _sid(str(payload.get("session", "")))
        try:
            import time as _time
            t0 = _time.monotonic()
            out = ask(text,
                      web_grounding=bool(payload.get("web", True)))
            out["elapsed_s"] = round(_time.monotonic() - t0, 1)
            out["session"] = sid
        except Exception as exc:  # the loop never leaks tracebacks to the screen
            out = {"mode": "error", "answer": f"عطل داخلي: {exc}",
                   "verification": "exception guard", "session": sid}
        _save_turn(sid, {"input": text[:2000],
                         "mode": out.get("mode"), "verdict": out.get("verdict"),
                         "answer": str(out.get("answer", ""))[:4000],
                         "elapsed_s": out.get("elapsed_s")})
        return self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/history":
            return self._send(404, b'{"error":"not found"}', "application/json")
        sid = _sid(parse_qs(parsed.query).get("session", [""])[0])
        if _delete_session(sid):
            return self._send(200, b'{"ok":true}', "application/json")
        return self._send(500, b'{"error":"could not delete session"}', "application/json")

    def _feedback(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 5000:
            return self._send(413, b'{"error":"too long"}', "application/json")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        if not isinstance(payload, dict):
            return self._send(400, b'{"error":"json object required"}', "application/json")
        row = {
            "when": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "session": _sid(str(payload.get("session", ""))),
            "verdict": str(payload.get("verdict", ""))[:20],
            "correct": bool(payload.get("correct")),
            "claim": str(payload.get("claim", ""))[:1000],
            "reviewed": False,
            "accepted": False,
            "label": "",
        }
        try:
            with FEEDBACK.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            return self._send(500, b'{"error":"could not save feedback"}', "application/json")
        return self._send(201, b'{"ok":true}', "application/json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="EpiGuard public-health verifier")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8733")),
                    help="port (Render/HF/Railway inject $PORT; default 8733)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 = this PC only; 0.0.0.0 = whole local network")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-warm-cache", action="store_true",
                    help="do not fetch trusted pages at startup")
    args = ap.parse_args(argv)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    shown = args.host if args.host != "0.0.0.0" else "<YOUR-LAN-IP>"
    url = f"http://{shown}:{args.port}/"
    print(f"bot screen on {url}  (Ctrl+C stops)")
    if not args.no_warm_cache:
        threading.Thread(target=_warm_cache, daemon=True).start()
        print("warming accredited-pages cache in background...")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
