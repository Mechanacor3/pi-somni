"""Test-only harness: real Pi (in Docker) + this plugin + a scripted fake model.

FakeUpstream serves OpenAI chat-completions SSE on 127.0.0.1.  `script(body, n)`
returns {"text": ...} or {"tool_calls": [(name, args), ...]}.  PiSession starts
Pi in RPC mode with only this plugin's extension and the fixture tool loaded,
the workspace bind-mounted, and no credentials.  Requires Docker and the image
built from tests/e2e/Dockerfile (tag below); tests skip otherwise.
"""
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMAGE = os.environ.get("PI_PLUGIN_E2E_IMAGE", "pi-plugin-e2e:pi-1.0.0")
REPO = pathlib.Path(__file__).resolve().parents[2]
E2E = pathlib.Path(__file__).resolve().parent


def docker_ready():
    return bool(shutil.which("docker")) and subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


def text_of(content):
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


class FakeUpstream:
    def __init__(self, script):
        self.script, self.requests, self.lock = script, [], threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer.lock:
                    index = len(outer.requests)
                    outer.requests.append(body)
                try:
                    reply = outer.script(body, index)
                except Exception as exc:  # surface script bugs in the transcript
                    reply = {"text": f"SCRIPT ERROR {exc!r}"}
                base = {"id": f"c{index}", "object": "chat.completion.chunk", "created": 0, "model": body.get("model")}
                if "tool_calls" in reply:
                    calls = [{"index": i, "id": f"call_{index}_{i}", "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(reply["tool_calls"])]
                    delta, finish = {"role": "assistant", "tool_calls": calls}, "tool_calls"
                else:
                    delta, finish = {"role": "assistant", "content": reply["text"]}, "stop"
                chunks = [dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]),
                          dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": finish}],
                               usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})]
                payload = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def plan_script(plans):
    """Reply by the last user message's prefix and how many assistant turns followed it."""
    def script(body, _index):
        messages = body["messages"]
        users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        last = users[-1]
        prompt = text_of(messages[last].get("content"))
        k = sum(1 for m in messages[last + 1:] if m.get("role") == "assistant")
        for key, plan in plans.items():
            if prompt.startswith(key):
                return {"tool_calls": plan[k]} if k < len(plan) else {"text": "done"}
        return {"text": "ok"}
    return script


class PiSession:
    def __init__(self, extension, workspace, port, env=None):
        self.name = f"pi-plugin-e2e-{secrets.token_hex(4)}"
        models = {"providers": {"fake": {"baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
                                         "apiKey": "not-a-real-key", "models": [{"id": "m", "name": "fake"}]}}}
        setup = (f"mkdir -p /tmp/agent && printf '%s' '{json.dumps(models)}' > /tmp/agent/models.json && "
                 "echo '{}' > /tmp/agent/auth.json && export PI_CODING_AGENT_DIR=/tmp/agent && "
                 "exec pi --mode rpc --no-session --offline --no-approve --no-extensions "
                 f"--extension /plugin/{extension} --extension /plugin/tests/e2e/fixture.ts "
                 "--no-skills --no-prompt-templates --no-context-files --no-themes --provider fake --model m")
        args = ["docker", "run", "--rm", "-i", "--name", self.name, "--network", "host",
                "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--tmpfs", "/tmp:rw,exec,size=64m",
                "-e", "HOME=/tmp", "-v", f"{REPO}:/plugin:ro", "-v", f"{workspace}:/work", "-w", "/work"]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        self.proc = subprocess.Popen([*args, IMAGE, "sh", "-c", setup], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.events, self.seq = [], 0

    def send(self, command):
        self.seq += 1
        command = {"id": f"r{self.seq}", **command}
        self.proc.stdin.write(json.dumps(command).encode() + b"\n")
        self.proc.stdin.flush()
        return command["id"]

    def read(self, until, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("pi exited: " + self.proc.stderr.read().decode()[-2000:])
            record = json.loads(line)
            self.events.append(record)
            if until(record):
                return record
        raise TimeoutError("no matching pi record")

    def prompt(self, message, timeout=120):
        rid = self.send({"type": "prompt", "message": message})
        response = self.read(lambda r: r.get("type") == "response" and r.get("id") == rid, timeout)
        if not response.get("success"):
            raise RuntimeError(f"prompt refused: {response}")
        if (response.get("data") or {}).get("disposition") == "handled":
            return response
        return self.read(lambda r: r.get("type") == "agent_settled", timeout)

    def tool_ends(self):
        return [e for e in self.events if e.get("type") == "tool_execution_end"]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except Exception:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        finally:
            self.proc.stdout.close()
            self.proc.stderr.close()
