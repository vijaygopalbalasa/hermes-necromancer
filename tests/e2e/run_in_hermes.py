"""Run Contract Necromancer inside a real Hermes, end to end, with the scripted stand-in model.

    python3 tests/e2e/run_in_hermes.py --hermes /path/to/hermes

It makes a throwaway HERMES_HOME and project, installs this plugin from this git checkout at HEAD
(the clone-at-a-commit route a catalog install takes), freezes the demo contracts as a person would,
runs `hermes chat -q` against scripted_model.py, and checks what happened. No API key is used: the
"model" is the local script, so this proves the plugin's wiring in Hermes's real agent loop, not how
any particular model behaves.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TASK = "split_bill in tipsplit.py loses cents: the shares do not add up to the total. Fix it."


def run(cmd, env, cwd=None, timeout=600, check=True):
    proc = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(map(str, cmd))}\n{proc.stdout}\n{proc.stderr}")
    return proc


def text_of(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes", default=shutil.which("hermes"), help="the hermes executable to test")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway folder")
    args = ap.parse_args()
    if not args.hermes:
        raise SystemExit("hermes not found; pass --hermes /path/to/hermes")
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True, text=True).stdout.strip()

    tmp = Path(tempfile.mkdtemp(prefix="necro-e2e-")).resolve()
    home, project, log = tmp / "home", tmp / "project", tmp / "requests.jsonl"
    home.mkdir()
    shutil.copytree(REPO / "demo" / "tip_split", project)
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN"))}
    env.update({"HERMES_HOME": str(home), "NO_COLOR": "1"})
    print(f"Hermes: {args.hermes}")
    print(run([args.hermes, "--version"], env).stdout.strip().splitlines()[0])
    print(f"Plugin: {REPO} at {sha}" + (" (uncommitted changes are NOT installed)" if dirty else ""))
    print(f"Folder: {tmp}\n")

    url = "file://" + urllib.parse.quote(str(REPO))
    run([args.hermes, "plugins", "install", url, "--ref", sha, "--enable"], env)
    print(run([args.hermes, "necro", "freeze", "--dir", str(project)], env).stdout.strip(), "\n")

    model = subprocess.Popen([sys.executable, str(HERE / "scripted_model.py"), "--port", "0", "--log", str(log)],
                             stdout=subprocess.PIPE, text=True)
    try:
        port = int(model.stdout.readline().strip())
        chat = run([args.hermes, "chat", "-q", TASK, "--oneshot", "-Q", "--provider", "custom",
                    "--base-url", f"http://127.0.0.1:{port}/v1", "--api-key", "scripted-no-key", "-m", "scripted",
                    "--in", str(project)], env, cwd=project, timeout=900, check=False)
    finally:
        model.terminate()
        model.wait(timeout=10)

    requests = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    seen = [m for r in requests for m in (r.get("messages") or [])]
    nudges = [text_of(m) for m in seen if m.get("role") == "user" and "[Contract Necromancer]" in text_of(m)]
    blocked = [text_of(m) for m in seen if m.get("role") == "tool" and "Blocked by Contract Necromancer" in text_of(m)]
    receipts = sorted((project / ".necro" / "receipts").glob("*.json"))
    verify = run([args.hermes, "necro", "verify", "--dir", str(project)], env, check=False)
    contract_intact = (project / "contracts" / "test_fair.py").read_bytes() == (REPO / "demo" / "tip_split" / "contracts" / "test_fair.py").read_bytes()

    checks = [
        ("Hermes ran the turn (exit code 0)", chat.returncode == 0),
        ("The model's first 'Done!' was refused with a note naming C2", any("C2" in n for n in nudges)),
        ("The write_file to the frozen contract was blocked", bool(blocked)),
        ("The frozen contract file is unchanged", contract_intact),
        ("A receipt was written after the real fix", bool(receipts)),
        ("The final reply carries the receipt", "frozen acceptance contracts pass" in chat.stdout and "Receipt" in chat.stdout),
        ("hermes necro verify says CONFIRMED", "CONFIRMED" in verify.stdout),
    ]
    print("What the model was told when it first said Done:")
    print("\n".join("    " + ln for ln in (nudges[0] if nudges else "(nothing)").splitlines()[:14]))
    print("\nFinal reply as Hermes printed it:")
    print("\n".join("    " + ln for ln in chat.stdout.strip().splitlines()[-8:]))
    print(f"\nModel requests: {len(requests)}; tools offered on the first: {', '.join(requests[0]['tools'][:60]) if requests else '-'}")
    print(f"necro verify: {verify.stdout.strip()}\n")
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if chat.returncode != 0:
        print("\nstderr:\n" + chat.stderr[-3000:])
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
