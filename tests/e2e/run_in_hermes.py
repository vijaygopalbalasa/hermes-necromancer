"""Run Contract Necromancer inside a real Hermes, end to end, with the scripted stand-in model.

    python3 tests/e2e/run_in_hermes.py --hermes /path/to/hermes

It makes a throwaway HERMES_HOME and project, installs this plugin from this git checkout at HEAD
(the clone-at-a-commit route a catalog install takes), freezes the demo contracts as a person would,
runs `hermes chat -q` against scripted_model.py, and checks what happened. Then it does the same in a
fresh project that has only code and unittest tests and no necro.toml, where the person's one
/necro freeze finds the tests, writes necro.toml and locks them. No API key is used: the "model" is
the local script, so this proves the plugin's wiring in Hermes's real agent loop, not how any
particular model behaves.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TASK = "split_bill in tipsplit.py loses cents: the shares do not add up to the total. Fix it."
# The person's freeze: Hermes's own /necro handler, called the way Hermes's CLI calls it when a person
# types /necro freeze (a -q prompt is passed to the model literally, so it cannot carry a slash command).
PERSON_SLASH = """
import sys
from hermes_cli.plugins import get_plugin_command_handler
from hermes_cli.plugin_command_dispatch import invoke_plugin_command
handler = get_plugin_command_handler("necro")
if handler is None:
    sys.exit("the /necro command is not registered")
print(invoke_plugin_command(handler, sys.argv[1], source=None, session_id="person", session_key="person", platform="cli"))
"""


def hermes_python(hermes: str) -> str:
    head = Path(hermes).read_text(errors="replace")[:600]
    found = re.search(r"exec' '([^']+)'", head) or re.search(r"^#!\s*(\S*python\S*)", head, re.M)
    if found:
        return found.group(1)
    candidate = Path(hermes).parent / "python"
    if candidate.exists():
        return str(candidate)
    raise SystemExit(f"cannot find the Python that runs {hermes}")


REAL_HOME = Path(os.path.realpath(os.path.expanduser("~/.hermes")))


def refuse_real_home(home) -> Path:
    """Never let this script touch the person's own Hermes home: refuse unless HERMES_HOME is a throwaway
    folder (Hermes migrates and rewrites the home it runs against)."""
    resolved = Path(os.path.realpath(os.path.expanduser(str(home))))
    if resolved == REAL_HOME or REAL_HOME in resolved.parents:
        raise SystemExit(f"refusing to run: HERMES_HOME resolves to {resolved}, the real Hermes home")
    return resolved


def run(cmd, env, cwd=None, timeout=600, check=True):
    refuse_real_home(env.get("HERMES_HOME") or "~/.hermes")
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


def chat(hermes, env, project, log, contract):
    """One `hermes chat -q` turn in ``project`` against the scripted model, which tries to loosen ``contract``."""
    refuse_real_home(env["HERMES_HOME"])
    model = subprocess.Popen([sys.executable, str(HERE / "scripted_model.py"), "--port", "0", "--log", str(log)],
                             stdout=subprocess.PIPE, text=True, env={**env, "NECRO_E2E_CONTRACT": contract})
    try:
        port = int(model.stdout.readline().strip())
        base = f"http://127.0.0.1:{port}/v1"
        # approvals.single_query_mode=approve: Hermes's own approval prompt lets execute_code through in -q mode,
        # so the plugin's guard is what has to stop the write to the frozen contract.
        for key, value in (("model.provider", "custom"), ("model.base_url", base), ("model.default", "scripted"),
                           ("approvals.single_query_mode", "approve")):
            run([hermes, "config", "set", key, value], env)
        return run([hermes, "chat", "-q", TASK, "--oneshot", "-Q", "--provider", "custom",
                    "--base-url", base, "--api-key", "scripted-no-key", "-m", "scripted",
                    "--in", str(project)], env, cwd=project, timeout=900, check=False)
    finally:
        model.terminate()
        model.wait(timeout=10)


def observe(log, project):
    """What the model was sent: Contract Necromancer notes, blocked writes, refused approvals, and the receipts."""
    requests = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    seen = [m for r in requests for m in (r.get("messages") or [])]
    code_calls = {tc.get("id") for m in seen for tc in (m.get("tool_calls") or [])
                  if (tc.get("function") or {}).get("name") == "execute_code"}
    return {
        "requests": requests,
        "nudges": [text_of(m) for m in seen if m.get("role") == "user" and "[Contract Necromancer]" in text_of(m)],
        "blocked": [text_of(m) for m in seen if m.get("role") == "tool" and "Blocked by Contract Necromancer" in text_of(m)],
        "code_blocked": [text_of(m) for m in seen if m.get("role") == "tool" and m.get("tool_call_id") in code_calls
                         and "Blocked by Contract Necromancer" in text_of(m)],
        "person_refusals": [text_of(m) for m in seen if m.get("role") == "tool" and "is a person's command" in text_of(m)],
        "receipts": sorted((project / ".necro" / "receipts").glob("*.json")),
    }


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
    # No keys reach Hermes. This script plays the person for the freeze step (its shell is not the agent's).
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN"))}
    env.pop("NECRO_AGENT_SESSION", None)
    env.update({"HERMES_HOME": str(refuse_real_home(home)), "NO_COLOR": "1",
                "NECRO_E2E_HERMES": str(Path(args.hermes).resolve()), "NECRO_E2E_HOME": str(home)})
    print(f"Hermes: {args.hermes}")
    version_line = run([args.hermes, "--version"], env).stdout.strip().splitlines()[0]
    print(version_line)

    print(f"Plugin: {REPO} at {sha}" + (" (uncommitted changes are NOT installed)" if dirty else ""))
    print(f"Folder: {tmp}\n")

    url = "file://" + urllib.parse.quote(str(REPO))
    run([args.hermes, "plugins", "install", url, "--ref", sha, "--enable"], env)
    refused = run([args.hermes, "necro", "freeze", "--dir", str(project)], env, check=False)
    print("`hermes necro freeze` from a script:", refused.stdout.strip().splitlines()[0][:110])
    print("/necro freeze, as the person types it:")
    print(run([hermes_python(args.hermes), "-c", PERSON_SLASH, "freeze"], {**env, "TERMINAL_CWD": str(project)},
              cwd=project).stdout.strip(), "\n")

    turn = chat(args.hermes, env, project, log, "contracts/test_fair.py")
    seen = observe(log, project)
    nudges, person_refusals, requests = seen["nudges"], seen["person_refusals"], seen["requests"]
    verify = run([args.hermes, "necro", "verify", "--dir", str(project)], env, check=False)
    contract_intact = (project / "contracts" / "test_fair.py").read_bytes() == (REPO / "demo" / "tip_split" / "contracts" / "test_fair.py").read_bytes()

    checks = [
        ("Hermes ran the turn (exit code 0)", turn.returncode == 0),
        ("The model's first 'Done!' was refused with a note naming C2", any("C2" in n for n in nudges)),
        ("The write_file to the frozen contract was blocked", bool(seen["blocked"])),
        ("The execute_code write to the frozen contract was blocked", bool(seen["code_blocked"])),
        ("The frozen contract file is unchanged", contract_intact),
        ("`hermes necro freeze` from a script refused (a person freezes with /necro)", refused.returncode == 3),
        ("The agent's own shell could not approve contracts (necro refused it)", bool(person_refusals)),
        ("A receipt was written after the real fix", bool(seen["receipts"])),
        ("The final reply carries the receipt", "frozen acceptance contracts pass" in turn.stdout and "Receipt" in turn.stdout),
        ("hermes necro verify says CONFIRMED", "CONFIRMED" in verify.stdout),
    ]
    print("What the model was told when it first said Done:")
    print("\n".join("    " + ln for ln in (nudges[0] if nudges else "(nothing)").splitlines()[:14]))
    print("\nWhat the agent's shell got back when it tried a disguised approve:")
    print("    " + (person_refusals[0][:240] if person_refusals else "(nothing)"))
    print("\nFinal reply as Hermes printed it:")
    print("\n".join("    " + ln for ln in turn.stdout.strip().splitlines()[-8:]))
    print(f"\nModel requests: {len(requests)}; tools offered on the first: {', '.join(requests[0]['tools'][:60]) if requests else '-'}")
    print(f"necro verify: {verify.stdout.strip()}\n")

    # Zero config: a fresh project with code and unittest tests and no necro.toml (the demo's files, with its
    # contracts folder named tests/). The person types /necro freeze once; the gate must then hold.
    fresh = tmp / "fresh"
    shutil.copytree(REPO / "demo" / "tip_split", fresh, ignore=shutil.ignore_patterns("necro.toml"))
    (fresh / "contracts").rename(fresh / "tests")
    script_freeze = run([args.hermes, "necro", "freeze", "--dir", str(fresh)], env, check=False)
    nothing_written = not os.path.lexists(fresh / "necro.toml") and not os.path.lexists(fresh / ".necro")
    print("Zero config, in a fresh project with tests/ and no necro.toml. /necro freeze, as the person types it:")
    reply = run([hermes_python(args.hermes), "-c", PERSON_SLASH, "freeze"], {**env, "TERMINAL_CWD": str(fresh)},
                cwd=fresh).stdout.strip()
    print(reply, "\n")
    generated = (fresh / "necro.toml").read_text() if (fresh / "necro.toml").is_file() else ""
    fresh_turn = chat(args.hermes, env, fresh, tmp / "fresh-requests.jsonl", "tests/test_fair.py")
    fresh_seen = observe(tmp / "fresh-requests.jsonl", fresh)
    fresh_verify = run([args.hermes, "necro", "verify", "--dir", str(fresh)], env, check=False)
    test_intact = (fresh / "tests" / "test_fair.py").read_bytes() == (REPO / "demo" / "tip_split" / "contracts" / "test_fair.py").read_bytes()
    checks += [
        ("Zero config: `hermes necro freeze` from a script refused and wrote no necro.toml",
         script_freeze.returncode == 3 and nothing_written),
        ("Zero config: /necro freeze locked the tests and said so plainly",
         reply.startswith("Locked your tests: `python3 -m unittest discover -q -s tests` must pass. Frozen: tests/** "
                          "(4 files).") and generated.startswith("# Written by /necro freeze")
         and (fresh / ".necro" / "lock.json").is_file()),
        ("Zero config: Hermes ran the turn (exit code 0)", fresh_turn.returncode == 0),
        ("Zero config: the first 'Done!' was refused with a note naming C1", any("C1" in n for n in fresh_seen["nudges"])),
        ("Zero config: the write_file to the locked test was blocked", bool(fresh_seen["blocked"])),
        ("Zero config: the locked test file is unchanged", test_intact),
        ("Zero config: the real fix got a receipt in the final reply",
         bool(fresh_seen["receipts"]) and "the frozen acceptance contract passes. Receipt" in fresh_turn.stdout),
        ("Zero config: hermes necro verify says CONFIRMED", "CONFIRMED" in fresh_verify.stdout),
    ]
    print("What the model was told when it first said Done:")
    print("\n".join("    " + ln for ln in (fresh_seen["nudges"][0] if fresh_seen["nudges"] else "(nothing)").splitlines()[:8]))
    print("\nFinal reply as Hermes printed it:")
    print("\n".join("    " + ln for ln in fresh_turn.stdout.strip().splitlines()[-4:]))
    print(f"\nnecro verify: {fresh_verify.stdout.strip()}\n")

    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    for label, result in (("first turn", turn), ("zero-config turn", fresh_turn)):
        if result.returncode != 0:
            print(f"\nstderr of the {label}:\n" + result.stderr[-3000:])
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
