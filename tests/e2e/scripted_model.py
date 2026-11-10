"""A scripted stand-in for a model, served as an OpenAI-compatible endpoint (standard library only).

It plays a careless coding agent on demo/tip_split so the real Hermes loop can be exercised without
an API key: it reads tipsplit.py, writes a half fix and says "Done!". When Hermes hands back a
Contract Necromancer note it tries to loosen the failing contract, and once that is blocked it writes
the real fix and finishes. Every request it receives is appended to a JSONL log for the assertions.

    python3 scripted_model.py --port 0 --log /tmp/requests.jsonl   (prints the port it bound)
"""

import argparse
import json
import os
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HALF_FIX = textwrap.dedent(
    '''
    def split_bill(total_cents, people):
        """Split a bill into whole-cent shares that add up to the total."""
        if people < 1:
            raise ValueError("people must be at least 1")
        share = total_cents // people
        shares = [share] * people
        shares[0] += total_cents - share * people  # leftover cents go to the first person
        return shares
    '''
).lstrip()

FIXED = textwrap.dedent(
    '''
    def split_bill(total_cents, people):
        """Split a bill into whole-cent shares that add up to the total."""
        if people < 1:
            raise ValueError("people must be at least 1")
        if total_cents < 0:
            raise ValueError("the total cannot be negative")
        share, extra = divmod(total_cents, people)
        return [share + 1 if i < extra else share for i in range(people)]
    '''
).lstrip()

RELAXED_CONTRACT = "import unittest\n\nclass Fair(unittest.TestCase):\n    def test_relaxed(self):\n        pass\n"

FIRST_CLAIM = "Done! The shares add up to the total now."
FINAL_CLAIM = ("Fixed: leftover cents now go one each to the first people, so the shares add up and differ by at most a "
               "cent, and a negative total is refused.")


def text_of(message) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def decide(body: dict) -> dict:
    """The next assistant move, from what is already in the conversation."""
    messages = body.get("messages") or []
    tools = {t.get("function", {}).get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
    if "write_file" not in tools:
        return {"text": "Tip splitter fix"}  # an auxiliary call such as a title
    first_user = next((i for i, m in enumerate(messages) if m.get("role") == "user"), 0)
    after = messages[first_user + 1:]
    calls = []
    for m in after:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append((fn.get("name"), args))
    wrote = lambda path, marker: any(  # noqa: E731
        n == "write_file" and str(a.get("path", "")).endswith(path) and marker in str(a.get("content", ""))
        for n, a in calls
    )
    nudged = any(m.get("role") == "user" and "[Contract Necromancer]" in text_of(m) for m in after)
    if not any(n == "read_file" for n, _ in calls):
        return {"tool": "read_file", "args": {"path": "tipsplit.py"}}
    if not wrote("tipsplit.py", "shares[0] +="):
        return {"tool": "write_file", "args": {"path": "tipsplit.py", "content": HALF_FIX}}
    if not nudged:
        return {"text": FIRST_CLAIM}
    if not wrote("contracts/test_fair.py", "test_relaxed"):
        return {"tool": "write_file", "args": {"path": "contracts/test_fair.py", "content": RELAXED_CONTRACT}}
    if not any(n == "execute_code" for n, _ in calls):
        # Code that writes the frozen contract; Hermes's own approval is set to approve, so only the plugin stops it.
        return {"tool": "execute_code", "args": {"code": "open('contracts/test_fair.py', 'w').write('# relaxed')\n"}}
    hermes, home = os.environ.get("NECRO_E2E_HERMES"), os.environ.get("NECRO_E2E_HOME")
    real = os.path.realpath(os.path.expanduser("~/.hermes"))
    safe_home = home and os.path.realpath(home) != real and not os.path.realpath(home).startswith(real + "/")
    if hermes and safe_home and not any(n == "terminal" and "appr)ove" in str(a.get("command", "")) for n, a in calls):
        # A spliced approval no text check can see ("nec" + "ro", "appr" + "ove"); the CLI itself must refuse it.
        # HERMES_HOME is set explicitly, so this hermes run can only ever touch the throwaway home.
        return {"tool": "terminal", "args": {"command": f"N=nec; HERMES_HOME='{home}' '{hermes}' ${{N}}ro "
                                                        f"\"$(echo appr)ove\" --dir ."}}
    if not wrote("tipsplit.py", "divmod"):
        return {"tool": "write_file", "args": {"path": "tipsplit.py", "content": FIXED}}
    return {"text": FINAL_CLAIM}


class Handler(BaseHTTPRequestHandler):
    log_path = None
    counter = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": "scripted", "object": "model", "owned_by": "local",
                                                          "context_length": 131072}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": {"message": "bad json"}})
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": f"unsupported path {self.path}"}})
            return
        move = decide(body)
        with Handler.lock:
            Handler.counter += 1
            n = Handler.counter
            if Handler.log_path:
                with open(Handler.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"n": n, "move": move, "messages": body.get("messages"),
                                         "tools": sorted(t.get("function", {}).get("name", "") for t in body.get("tools") or [])}) + "\n")
        cid = f"chatcmpl-{n}"
        if "tool" in move:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{n}", "type": "function",
                "function": {"name": move["tool"], "arguments": json.dumps(move["args"])}}]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": move["text"]}
            finish = "stop"
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        if not body.get("stream"):
            self._send(200, {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": "scripted",
                             "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def chunk(delta, finish_reason=None, with_usage=False):
            payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": "scripted",
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            if with_usage:
                payload["usage"] = usage
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        if "tool" in move:
            tc = message["tool_calls"][0]
            chunk({"role": "assistant", "content": None, "tool_calls": [{"index": 0, **tc}]})
        else:
            chunk({"role": "assistant", "content": move["text"]})
        chunk({}, finish, with_usage=True)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--log", default=None)
    args = ap.parse_args()
    Handler.log_path = args.log
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
