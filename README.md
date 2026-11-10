# Contract Necromancer for Hermes

Hermes cannot call coding work done while one of your frozen acceptance contracts fails.

You write the checks that define "done" for a task, read them, and freeze them. From then on, when Hermes
edits code and tries to finish, the plugin runs those checks itself. If one fails, Hermes is told which one and
why, and it keeps working. It cannot quietly edit the checks to make them pass: edits to frozen files are
blocked, and any change to them counts as a failure. When everything passes, the reply carries a receipt that
anyone can re-check later with one command.

```
1. You freeze 3 contracts.            /necro freeze
2. Hermes makes a half fix, says "Done!"   ->  refused: C2 fails (No share differs by more than a cent)
3. Hermes tries to loosen C2.         ->  blocked: contracts/test_fair.py is a frozen acceptance contract
4. Hermes writes the real fix.        ->  passes; the reply ends with "Receipt 22484f7adc1b"
5. Anyone, later:                     hermes necro verify   ->  CONFIRMED (or MISMATCH, naming what changed)
```

## Try it

Needs Hermes 0.22.7 or newer and Python 3.11 or newer. No API key is needed for the demo.

```
hermes plugins install vijaygopalbalasa/hermes-necromancer --enable
hermes necro demo
```

`hermes necro demo` copies a small tip-splitting project with a planted bug to a temporary folder and plays the
story above through the plugin's real hook functions, with a scripted agent standing in for the model. It ends
with a receipt that verifies, then catches a later change.

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
   `conftest.py`, say) also counts as tampering. Hermes can draft the file for you before you freeze it.
2. Read the contracts, then type `/necro freeze` in Hermes (or run `hermes necro freeze` in your own terminal).
3. Work with Hermes as usual. Commit `.necro/lock.json` with your contracts.

| Command | What it does |
|---|---|
| `/necro` or `/necro status` | What is frozen here, and the latest receipt |
| `/necro freeze` | Freeze the contracts in `necro.toml` by hash (a person's act) |
| `/necro approve` | Accept a lock someone else froze and committed, after reading it |
| `/necro unfreeze` | Remove the lock so the contracts can change (receipts stay) |
| `/necro check` | Run every frozen contract now; write a receipt if all pass |
| `/necro verify [receipt]` | Re-check a receipt: CONFIRMED, MISMATCH or SUPERSEDED |
| `/necro receipts` | List receipts, newest first |

Every command also works as `hermes necro <command>` in a terminal. Outside Hermes, for CI or a reviewer:
`python3 <plugin folder>/necro verify .necro/receipts/<hash>.json`.

## How it works in Hermes

- **The gate (`pre_verify`).** Hermes fires this hook when the agent edited code and is about to give its final
  answer. The plugin runs the frozen contracts within a time budget (25 s by default, under Hermes's 30 s hook
  limit) and returns `continue` with the failing contracts and the end of their output. Hermes caps how many
  times a turn can be kept going (`agent.max_verify_nudges`, default 3).
- **The stamp (`transform_llm_output`).** The final reply starts with `Not done.` and the failing contracts if
  the agent ran out of tries, or ends with the receipt if everything passed. It also checks a frozen project
  whenever its files changed since the last check, so edits made through the shell are covered too.
- **The guard (`pre_tool_call`).** `write_file`, `patch`, `terminal` and `execute_code` calls that would rewrite
  a frozen file, `necro.toml` or `.necro/` are blocked with a reason the agent can act on. Reading and running
  them stays allowed.
- **Person-only commands.** Freezing, approving and unfreezing are refused when they come from the agent: the
  guard blocks them, and the plugin marks every command the agent runs with `NECRO_AGENT_SESSION`
  (`ctx.register_session_env`, Hermes 0.23.1 and newer), so `necro freeze` refuses to run there.
- **Tools.** `necro_status` and `necro_check` let the agent see and re-run the contracts. Hermes lists plugin
  tools behind `tool_search` by default.

## Receipts

A receipt is `.necro/receipts/<sha256>.json`. It pins the lock, every contract file, every other project file
(by SHA-256, whatever `.gitignore` says; caches and installed dependencies are skipped) and each contract's
result. Its name is the hash of its own contents. `verify` checks that hash, re-hashes every pinned file,
looks for added files, checks that the lock is exactly what freezing `necro.toml` would write, then re-runs the
contracts and compares the results:

- `CONFIRMED <hash>.` Everything matches and every contract still passes.
- `MISMATCH.` Names what changed: files changed or added, or contracts whose result changed.
- `SUPERSEDED.` A person froze the contracts again after this receipt was written.

## Settings

Under `plugins.entries.contract-necromancer.settings` (also shown in the Desktop app's Plugins page):
`gate_budget_seconds` (25), `check_budget_seconds` (600), `hold_stream` (false: when true, you see only the
stamped final reply, without streaming) and `stamp_done_claims` (true).

## Limits

- Guard rails, not a sandbox. A determined command can still change a frozen file; the hash check then counts
  the contract as failing. The guarantee is `verify` against a committed lock: check `git diff .necro/` before
  you trust a receipt.
- The gate is bounded. Hermes keeps a turn going at most `agent.max_verify_nudges` times (3 by default). After
  that the reply goes out, stamped `Not done.`
- The gate fires only after edits made with `write_file` or `patch`. Edits made only through the shell are
  caught when the reply is stamped, but the agent is not kept going.
- Contracts run as your user, unsandboxed, with a small environment (no API keys unless a contract names them
  under `env = [...]`). Code under test runs inside the test runner and can tamper with it. A file in the
  project can change how a test runs (for example a `unittest.py` that shadows Python's own). The receipt pins
  such a file, so it shows up in the receipt's file list and in `git status`.
- Contracts slower than the gate's budget are reported as not checked; the agent is told to run `necro_check`,
  which has 10 minutes. Raise `gate_budget_seconds` and Hermes's `plugins.hook_callback_timeout` together if
  your contracts are slow.
- On Hermes 0.22.7 to 0.23.0 the agent-shell marker is not available (it needs `register_session_env`), so
  person-only commands rely on the guard alone.
- Tested on macOS with Hermes 0.23.1 and 0.22.7, Python 3.11 and 3.14. Linux and Windows are untested here;
  the CI workflow runs the unit tests and the demo on Ubuntu and macOS on every push.

## What it touches

It runs the commands in your frozen `necro.toml` with your user's permissions. It reads your project's files
to hash them, and writes only `.necro/` in the project and its own state (the approved lock per project) under
`<HERMES_HOME>/plugin-data/`. It adds `NECRO_AGENT_SESSION` to the environment of commands the agent runs. No
network calls, no telemetry, no credentials, no background processes.

## Tests

```
python3 -m unittest discover -s tests                      # 141 tests, about 40 s
python3 tests/e2e/run_in_hermes.py --hermes $(which hermes)  # the real Hermes agent loop
```

The second runs `hermes chat` in a throwaway Hermes home against a local scripted stand-in for a model (no API
key), so it checks the plugin's wiring in Hermes's real agent loop, not how a particular model behaves: the
first "Done!" is refused, the edit to the frozen contract is blocked, a disguised `necro approve` from the
agent's shell is refused, and the real fix gets a receipt that verifies.

## Background

This is the Hermes version of [Contract Necromancer](https://github.com/vijaygopalbalasa/contract-necromancer),
which does the same for Kiro specs. In that project's small experiment, Kiro marked an impossible task done in
11 of 11 runs without the gate and 0 of 19 with it. That result is about Kiro, not Hermes.

## License

MIT
