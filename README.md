# Contract Necromancer: Hermes can't say "done" until the tests you froze pass

Agents that can't finish a task tend to edit the tests instead (ImpossibleBench,
[arXiv 2510.20270](https://arxiv.org/abs/2510.20270)). Here the tests are locked: you freeze them with `/necro freeze`,
Hermes is blocked from changing them, and when Hermes tries to finish, the plugin runs them itself. A failure sends
Hermes back to work with the error; a pass ends the reply with a receipt anyone can re-check.

The same gate, tested in Kiro on a task that can't honestly be finished: agents claimed done in 11 of 11 runs without
it and 0 of 19 with it ([github.com/vijaygopalbalasa/contract-necromancer](https://github.com/vijaygopalbalasa/contract-necromancer), experiments/).

<!-- screenshot: a real-model Hermes session, the refusal and then the receipt, goes here -->

```
1. You freeze 3 contracts.                 /necro freeze
2. Hermes makes a half fix, says "Done!"   ->  refused: C2 fails (No share differs by more than a cent)
3. Hermes tries to loosen C2.              ->  blocked: contracts/test_fair.py is a frozen acceptance contract
4. Hermes writes the real fix.             ->  passes; the reply ends with "Receipt 6570eefc0447"
5. Later:                                  /necro verify   ->  CONFIRMED (or MISMATCH, naming what changed)
```

## How it differs

- **A person freezes the tests, and Hermes cannot change them.** Freezing is a slash command the agent cannot type.
  Edits, deletes and new files under a frozen folder are blocked, and any change that gets through fails its contract.
- **The plugin runs the tests itself** at Hermes's verify step and sends Hermes back to work with the failure. It does
  not depend on the agent choosing to run them or reporting the result honestly.
- **A pass leaves a receipt anyone can re-check**, later and in a fresh clone, with one command: CONFIRMED, MISMATCH
  (naming what changed) or SUPERSEDED.

## Try it

Needs Hermes 0.22.7 or newer (0.23.1 or newer recommended) and Python 3.11 or newer. The demo needs no API key.

```
hermes plugins install vijaygopalbalasa/hermes-necromancer --enable
hermes necro demo
```

`hermes necro demo` copies a small tip-splitting project with a planted bug to a temporary folder and plays the story
above through the plugin's real hook functions, with a scripted agent standing in for the model.

## Use it in your project

1. Write `necro.toml` next to your code. Each contract is a command that exits 0 when the work is right, plus the
   files it freezes:

   ```toml
   [[contract]]
   id = "C1"
   says = "Shares add up to the total"
   run = "python3 -m pytest tests/acceptance/test_split.py -q"
   files = ["tests/acceptance/**"]
   ```

   Any language works: `npm test -- tests/acceptance`, `cargo test --test acceptance`, `go test ./acceptance/...`.
   Freeze the whole folder with a glob, so a new file there (a `conftest.py`, an `__init__.py`) counts as tampering.
   If your tests write files (reports, coverage), list them: `outputs = ["reports/*.xml"]`. Hermes can draft
   `necro.toml` for you before you freeze it.
2. Read the contracts, then type **`/necro freeze`** in the Hermes chat. Freezing, approving and unfreezing are a
   person's acts and live in the slash command, because the agent cannot type into your chat.
3. Work with Hermes as usual. Commit `.necro/lock.json` and `.necro/receipts/` with your contracts.

| Command | What it does |
|---|---|
| `/necro` or `/necro status` | What is frozen here, and the latest receipt |
| `/necro freeze` | Freeze the contracts in `necro.toml` by hash |
| `/necro approve` | Accept a lock someone else froze and committed, after reading it |
| `/necro unfreeze` | Remove the lock so the contracts can change (receipts stay) |
| `/necro check` | Run every frozen contract now; write a receipt if all pass |
| `/necro verify [receipt] [--run]` | Re-check a receipt: CONFIRMED, MISMATCH or SUPERSEDED |
| `/necro receipts` | List receipts, newest first |

`status`, `check`, `verify`, `receipts` and `demo` also work as `hermes necro <command>` in a terminal. From a shell,
freezing, approving and unfreezing refuse to run, because nothing there proves a person is typing (Hermes marks every
hermes process as an agent harness); the refusal says exactly what to type instead. For CI or a reviewer without
Hermes: `python3 <plugin folder>/necro verify .necro/receipts/<hash>.json --run`.

## How it works in Hermes

- **The gate (`pre_verify`).** When Hermes edited code and is about to answer, the plugin runs the frozen contracts
  within a time budget (25 s by default, under Hermes's 30 s hook limit) and returns `continue` with the failing
  contracts and the end of their output. Hermes caps this at `agent.max_verify_nudges` (3 by default).
- **The stamp (`transform_llm_output`).** The final reply starts with `Not done.` if the agent ran out of tries, or
  ends with the receipt. It also checks a frozen project whenever its files changed, so shell edits are covered.
- **The guard (`pre_tool_call`).** `write_file`, `patch`, `terminal` and `execute_code` calls that would write,
  delete or rename a frozen file, `necro.toml`, `.necro/` or the approval store are blocked with a reason, as is a
  `find` over a frozen project that deletes, writes or runs anything but a plain reader, and any command or code
  too long to check inside one.
- **Approvals.** For each project folder the plugin keeps the one lock a person froze or approved there, outside the
  project, sealed with an HMAC. A lock that is not the approved one, that does not match `necro.toml`, or whose
  record was changed outside necro is refused; a deleted or renamed lock counts as not done.
- **Tools.** `necro_status` and `necro_check` let the agent see and re-run the contracts (Hermes lists plugin tools
  behind `tool_search`).

## Receipts

A receipt is `.necro/receipts/<sha256>.json`, named by the hash of its own contents. It pins the lock, every contract
file, each contract's result and every other project file by SHA-256, whether git ignores it or not, because an
ignored file can change a run. Pins that git ignores are marked `ignored`; credential files (`.env`, keys, `.netrc`
and the like) are marked `credential`, and their contents are hashed, never stored, logged or printed. Never walked,
pinned or checked: version control and necro's own (`.git`, `.hg`, `.svn`, `.necro`), test-tool caches
(`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`), installed dependencies
(`node_modules`, `.venv`, `venv`, `bower_components`, `.terraform`), build output (`target`, `.gradle`, `coverage`,
`.next`, `.nuxt`, `.svelte-kit`, `.turbo`, `.parcel-cache`), and `.pyc`, `.pyo` and `.DS_Store` files. A contract
run may write in those folders; any other file it creates or changes, ignored or not, fails the check unless
`necro.toml` declares it under `outputs`.

`verify` checks the receipt's hash, re-hashes every pinned file, looks for added files and checks that the lock is
exactly what freezing `necro.toml` would write. It re-runs the contracts only when a person approved that lock for
that folder, or when you add `--run` after reading `necro.toml`, because re-running executes the project's commands.
In another checkout, such as a fresh clone, a pin marked `ignored` or `credential` that is missing there is listed
as local-only, not a mismatch by itself, and the re-run decides: `CONFIRMED <hash> (1 local-only file not present
here: .env).` A tracked pin that differs, or a local-only pin that is present but differs, is a `MISMATCH`. The
answer is `CONFIRMED`, `MISMATCH` (naming what changed), `SUPERSEDED` (frozen again since), or `HASHES MATCH` (files
match but nothing was re-run; not a confirmation).

## Settings

Under `plugins.entries.contract-necromancer.settings`: `gate_budget_seconds` (25), `check_budget_seconds` (600),
`hold_stream` (false; when true you see only the stamped final reply) and `stamp_done_claims` (true).

## Limits

- **Guard rails, not a sandbox.** The guard only stops the ordinary routes early; the hash check is the backstop:
  any change to a frozen file fails its contract, and a removed lock fails closed. A process running as your user
  that sets out to evade both can still change files and approvals (it can read the HMAC key). Real isolation needs
  one of Hermes's sandboxed terminal backends (`terminal.backend: docker`, `modal` and others), untested here.
- **The person check.** Hermes runs the agent's commands on pipes, but a background command with `pty=true` gets a
  real terminal, so the plugin also checks its own `NECRO_AGENT_SESSION` marker, Hermes's `HERMES_AGENT` variable and
  whether a Hermes process started the shell. Any of them refuses.
- **Bounded.** After 3 nudges the reply goes out, stamped `Not done.` The gate fires only after `write_file` or
  `patch` edits; shell-only edits are caught when the reply is stamped, but the agent is not kept going.
- **What tests can see.** Code under test runs inside the test runner and can tamper with it. A file in the
  project that changes how a test runs (a `unittest.py` that shadows Python's own, even one git ignores) can make a
  local run pass; it is pinned and listed in the receipt, and `verify --run` in a fresh clone, which lacks it, fails.
  Contracts run as your user with a small environment (no API keys unless named under `env = [...]`).
- **Slow or huge.** Contracts slower than the gate's budget are reported as not checked (the agent is told to run
  `necro_check`, which has 10 minutes); in a very large project, list the files that matter under `sources`.
- Tested on macOS with Hermes 0.23.1 and 0.22.7, Python 3.11 and 3.14. Linux and Windows are untested here; the CI
  workflow runs the unit tests and the demo on Ubuntu and macOS on every push.

## What it touches

It runs the commands in your frozen `necro.toml` with your user's permissions and reads your project's files to hash
them, credential files such as `.env` included: their contents are hashed, never stored, logged or printed. It writes
`.necro/` in the project and, under `<HERMES_HOME>/plugin-data/`, its approval records and the key that seals them.
It adds `NECRO_AGENT_SESSION` to the environment of commands the agent runs, runs `git ls-files` (with fsmonitor and
hooks off) to mark which pins git ignores, and runs `ps` when a person-only command is typed in a shell. No network
calls, no telemetry, no credentials stored, no background processes.

## Tests

```
python3 -m unittest discover -s tests                        # 217 tests, about 90 s
python3 tests/e2e/run_in_hermes.py --hermes $(which hermes)    # the real Hermes agent loop
```

The second runs `hermes chat` in a throwaway Hermes home against a local scripted stand-in for a model (no API key),
so it checks the plugin's wiring in Hermes's real agent loop, not how a particular model behaves. Its ten checks
include: the first "Done!" is refused; `write_file` and `execute_code` writes to the frozen contract are blocked;
freezing from a script is refused while `/necro freeze` works; a spliced approve command from the agent's own shell
is refused; the real fix gets a receipt that verifies.

## License

MIT
