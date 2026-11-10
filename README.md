# Contract Necromancer for Hermes

Hermes cannot call coding work done while one of your frozen acceptance contracts fails.

You write the checks that define "done" for a task, read them, and freeze them. From then on, when Hermes
edits code and tries to finish, the plugin runs those checks itself. If one fails, Hermes is told which one and
why, and it keeps working. It cannot quietly edit the checks to make them pass: edits to frozen files are
blocked, and any change to them counts as a failure. When everything passes, the reply carries a receipt that
anyone can re-check later.

```
1. You freeze 3 contracts.                 /necro freeze
2. Hermes makes a half fix, says "Done!"   ->  refused: C2 fails (No share differs by more than a cent)
3. Hermes tries to loosen C2.              ->  blocked: contracts/test_fair.py is a frozen acceptance contract
4. Hermes writes the real fix.             ->  passes; the reply ends with "Receipt 22484f7adc1b"
5. Later:                                  /necro verify   ->  CONFIRMED (or MISMATCH, naming what changed)
```

## Try it

Needs Hermes 0.22.7 or newer (0.23.1 or newer recommended) and Python 3.11 or newer. The demo needs no API key.

```
hermes plugins install vijaygopalbalasa/hermes-necromancer --enable
hermes necro demo
```

`hermes necro demo` copies a small tip-splitting project with a planted bug to a temporary folder and plays the
story above through the plugin's real hook functions, with a scripted agent standing in for the model.

## Use it in your project

1. Write `necro.toml` next to your code. Each contract is a command that exits 0 when the work is right, plus
   the files it freezes:

   ```toml
   [[contract]]
   id = "C1"
   says = "Shares add up to the total"
   run = "python3 -m pytest tests/acceptance/test_split.py -q"
   files = ["tests/acceptance/test_split.py"]
   ```

   Any language works: `npm test -- tests/acceptance`, `cargo test --test acceptance`, `go test ./acceptance/...`.
   Use a glob such as `files = ["tests/acceptance/**"]` to freeze a whole folder, so a new file there (a
   `conftest.py`, say) also counts as tampering. If your tests write files (reports, coverage), list them:
   `outputs = ["reports/*.xml"]`. Any other file a contract run changes fails the check. Hermes can draft
   `necro.toml` for you before you freeze it.
2. Read the contracts, then type **`/necro freeze`** in Hermes. Freezing, approving and unfreezing are a
   person's acts, and the slash command is how you do them: it comes from your own chat input, which the agent
   cannot type.
3. Work with Hermes as usual. Commit `.necro/lock.json` with your contracts.

| Command | What it does |
|---|---|
| `/necro` or `/necro status` | What is frozen here, and the latest receipt |
| `/necro freeze` | Freeze the contracts in `necro.toml` by hash |
| `/necro approve` | Accept a lock someone else froze and committed, after reading it |
| `/necro unfreeze` | Remove the lock so the contracts can change (receipts stay) |
| `/necro check` | Run every frozen contract now; write a receipt if all pass |
| `/necro verify [receipt] [--run]` | Re-check a receipt: CONFIRMED, MISMATCH or SUPERSEDED |
| `/necro receipts` | List receipts, newest first |

`status`, `check`, `verify`, `receipts` and `demo` also work as `hermes necro <command>` in a terminal.
`hermes necro freeze`, `approve` and `unfreeze` refuse to run, because nothing in a shell proves a person is
typing (Hermes marks every `hermes` process as an agent harness). A standalone copy for CI or a reviewer needs no
Hermes: `python3 <plugin folder>/necro verify .necro/receipts/<hash>.json --run`.

## How it works in Hermes

- **The gate (`pre_verify`).** Hermes fires this hook when the agent edited code and is about to give its final
  answer. The plugin runs the frozen contracts within a time budget (25 s by default, under Hermes's 30 s hook
  limit) and returns `continue` with the failing contracts and the end of their output. Hermes caps how many
  times a turn can be kept going (`agent.max_verify_nudges`, default 3).
- **The stamp (`transform_llm_output`).** The final reply starts with `Not done.` and the failing contracts if
  the agent ran out of tries, or ends with the receipt if everything passed. It also checks a frozen project
  whenever its files changed since the last check, so edits made through the shell are covered too.
- **The guard (`pre_tool_call`).** `write_file`, `patch`, `terminal` and `execute_code` calls that would write,
  delete or rename a frozen file, `necro.toml`, `.necro/` or the approval store are blocked with a reason the
  agent can act on. Reading and running the contracts stays allowed.
- **Approvals.** For each project folder the plugin keeps the one lock a person froze or approved there, outside
  the project, sealed with an HMAC. The gate refuses a lock that is not the approved one for its folder, a lock
  that does not match `necro.toml`, and a record changed outside necro. A deleted or renamed lock counts as not
  done until a person unfreezes.
- **Person-only commands in a shell** refuse unless stdin is a terminal, no agent marker is set (the plugin
  marks every command the agent runs with `NECRO_AGENT_SESSION` through `ctx.register_session_env`, Hermes
  0.23.1 and newer), no Hermes process started the shell, and the marker could be registered.
- **Tools.** `necro_status` and `necro_check` let the agent see and re-run the contracts. Hermes lists plugin
  tools behind `tool_search` by default.

## Receipts

A receipt is `.necro/receipts/<sha256>.json`. It pins the lock, every contract file, every other project file
(by SHA-256, whatever `.gitignore` says) and each contract's result. Its name is the hash of its own contents.
`verify` checks that hash, re-hashes every pinned file, looks for added files and checks that the lock is exactly
what freezing `necro.toml` would write. It re-runs the contracts only when a person approved that lock for that
folder, or when you add `--run` after reading `necro.toml`, because re-running executes the project's commands:

- `CONFIRMED <hash>.` Everything matches and every contract still passes.
- `MISMATCH.` Names what changed: files changed or added, contracts whose result changed, or files a run changed.
- `SUPERSEDED.` A person froze the contracts again after this receipt was written.
- `HASHES MATCH.` Every file matches, but nothing was re-run (the lock is not approved here). Not a confirmation.

## Settings

Under `plugins.entries.contract-necromancer.settings` (also shown in the Desktop app's Plugins page):
`gate_budget_seconds` (25), `check_budget_seconds` (600), `hold_stream` (false: when true, you see only the
stamped final reply, without streaming) and `stamp_done_claims` (true).

## Limits

**What it stops.** The agent's ordinary routes to moving the goalposts: writing, deleting or renaming a frozen
file or the lock with `write_file`, `patch` or plain shell commands (`rm`, `mv`, `sed -i`, redirects,
`find -delete`, `git rm`), running `necro freeze`, `approve` or `unfreeze` (from its shell they refuse), and
finishing a coding turn while a contract fails, up to Hermes's limit of 3 nudges.

**What it detects.** Anything that gets past the guard and changes what was frozen: a changed, missing or added
contract file or `necro.toml` fails its contract (the lock pins every hash); a deleted or renamed lock counts as
not done; a forged or rolled-back lock is refused; an approval record changed outside necro fails its HMAC;
a contract run that changes project files fails; and `verify` reports any file changed after a receipt.

**What it cannot stop.** A process running as your user that sets out to evade the guard can still change
files and approvals: it can read the HMAC key, rewrite the store and the lock together, or change a file the
gate does not pin. Some of that shows up in `git diff .necro/` and in the receipts, some does not. Specifically:

- Code under test runs inside the test runner and can tamper with it. A file in the project that changes how a
  test runs (a `unittest.py` that shadows Python's own) is pinned and listed in the receipt, but it still passes.
- Caches and installed dependencies (`node_modules`, `.venv` and the like) are not pinned.
- After 3 nudges the reply goes out, stamped `Not done.` The gate fires only after `write_file` or `patch`
  edits; shell-only edits are caught when the reply is stamped, but the agent is not kept going.
- Contracts run as your user, unsandboxed, with a small environment (no API keys unless a contract names them
  under `env = [...]`). Contracts slower than the gate's budget are reported as not checked, and in a very large
  project hashing alone can outrun it; list the files that matter under `sources = [...]` then.

Real isolation needs the agent's commands to run away from your files and from Hermes's data folder, for
example with one of Hermes's sandboxed terminal backends (`terminal.backend: docker`, `modal` and others). This
plugin has not been tested with those backends.

Tested on macOS with Hermes 0.23.1 and 0.22.7, Python 3.11 and 3.14. Linux and Windows are untested here; the CI
workflow runs the unit tests and the demo on Ubuntu and macOS on every push.

## What it touches

It runs the commands in your frozen `necro.toml` with your user's permissions. It reads your project's files to
hash them. It writes `.necro/` in the project and, under `<HERMES_HOME>/plugin-data/`, its approval records and
the key that seals them. It adds `NECRO_AGENT_SESSION` to the environment of commands the agent runs, and when a
person-only command is typed in a shell it runs `ps` to see which programs started that shell. No network
calls, no telemetry, no credentials, no background processes.

## Tests

```
python3 -m unittest discover -s tests                        # 171 tests, about 50 s
python3 tests/e2e/run_in_hermes.py --hermes $(which hermes)    # the real Hermes agent loop
```

The second runs `hermes chat` in a throwaway Hermes home against a local scripted stand-in for a model (no API
key), so it checks the plugin's wiring in Hermes's real agent loop, not how a particular model behaves. Its
nine checks include:
- the first "Done!" is refused;
- the edit to the frozen contract is blocked;
- `hermes necro freeze` from a script is refused, while `/necro freeze` works;
- a spliced `necro approve` from the agent's own shell is refused;
- the real fix gets a receipt that verifies.

## Background

This is the Hermes version of [Contract Necromancer](https://github.com/vijaygopalbalasa/contract-necromancer),
which does the same for Kiro specs. In that project's small experiment, Kiro marked an impossible task done in
11 of 11 runs without the gate and 0 of 19 with it. That result is about Kiro, not Hermes.

## License

MIT
