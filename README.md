# Contract Necromancer: Hermes can't say "done" until the tests you froze pass, and it can't edit them

You freeze the tests that define done; when Hermes tries to finish, the plugin runs them itself, sends Hermes back to
work while one fails, and ends a passing reply with a receipt anyone can re-check.

![In a real run Hermes said "The fix is complete" while frozen test C4 still failed; the plugin refused that done](docs/img/not-done.png)

## Quick start

1. Install it:

   ```
   hermes plugins install vijaygopalbalasa/hermes-necromancer --enable
   ```

2. In your project, type **`/necro freeze`** in the Hermes chat.

That finds your tests and the command that runs them (pytest, unittest, `npm test`, `go test` or `cargo test`), locks
the test files by hash, and from then on Hermes can't say "done" until that command passes, and can't edit those files.

To watch the whole story first, with no API key: `hermes necro demo`.

## How this differs

| Kind of plugin | What they do | What this one does |
|---|---|---|
| Claim checkers | Compare the agent's reply with the session's tool logs. | Runs the frozen tests itself at Hermes's verify step (`pre_verify`), so "done" is checked against a fresh run, not against what the agent says it ran. |
| Check runners | Ask the model to run the checks, so an edited test only makes an old pass stale. | A person freezes the tests by hash and Hermes is blocked from editing them; any change to one makes it fail. |
| Push gates | Hold back `git push` or a pull request until a check passes. | Gates "done" itself, before the reply reaches you, and leaves a receipt that re-verifies in a fresh clone. |

A real run in Hermes 0.23.1 with a local model and no API key: asked only to fix the lost cents, Hermes said "The fix is complete. What's left: None." while a frozen test was still failing. The plugin refused that done and named the test; Hermes fixed it and finished with all four tests passing and a receipt ([the log](docs/real-run-2026-11-11.md), with a fairer comparison).

The same gate in Kiro, not Hermes: on a task that can't honestly be finished, agents claimed done in 11 of 11 runs
without it and 0 of 19 with it ([github.com/vijaygopalbalasa/contract-necromancer](https://github.com/vijaygopalbalasa/contract-necromancer), experiments/).

## What happens

Agents that can't finish a task tend to edit the tests instead (ImpossibleBench,
[arXiv 2510.20270](https://arxiv.org/abs/2510.20270)). Here each frozen test is a contract: a command and the files it
freezes, listed in `necro.toml`. `hermes necro demo` plays this story on a small tip-splitting project with a planted
bug, in a temporary folder, through the plugin's real hook functions, with a scripted agent standing in for the model:

```
1. You freeze 4 tests.                     /necro freeze
2. Hermes makes a half fix, says "Done!"   ->  refused: C2 and C4 fail (uneven shares; a negative total is accepted)
3. Hermes tries to loosen C2.              ->  blocked: contracts/test_fair.py is a frozen acceptance contract
4. Hermes writes the real fix.             ->  passes; the reply ends with "Receipt c9ca9cd8bf8c"
5. Later:                                  /necro verify   ->  CONFIRMED (or MISMATCH, naming what changed)
```

Needs Hermes 0.22.7 or newer (0.23.1 or newer recommended) and Python 3.11 or newer; the demo needs no API key. The
plugin installs under the name `contract-necromancer`.

## Use it in your project

Type **`/necro freeze`** in the Hermes chat, in your project's folder. With no `necro.toml` there, it looks for test
files (`tests/`, `test/`, `__tests__/`, `test_*.py`, `*_test.py`, `conftest.py`, `*.test.*`, `*.spec.*`, `*_test.go`,
`testdata/`) and for the command that runs them, in this order: pytest (named in your settings, a `conftest.py`, or a
test file that uses it), unittest, `npm test` (when `package.json` has a real test script), `go test ./...` and
`cargo test`. It writes `necro.toml` with one contract, C1, locks it, and says what it locked:

```
Locked your tests: `python3 -m pytest -q` must pass. Frozen: tests/** (12 files). Hermes can't say "done" until
this passes, and it can't edit these files. Undo: /necro unfreeze. Change what counts as done: edit necro.toml,
then /necro freeze. See where it stands: /necro check.
```

If it finds no test files, can't tell how to run them, or the command can't run here (say `python3` cannot import
pytest), it locks nothing and says what to do instead. To use your own test command, put it after freeze:
`/necro freeze npm run test:unit`. A script the command names, such as `./run-tests.sh`, is locked with it.
Locked means fixed for everyone: Hermes can't add a test file to a frozen folder either (a new file in `tests/`
counts as tampering), and your own edits to the tests count as tampering until you type `/necro freeze` again.
Freezing, approving and unfreezing are a person's acts and live in the slash command, because the agent cannot type
into your chat. Work with Hermes as usual, and commit `necro.toml`, `.necro/lock.json` and `.necro/receipts/` with
your code.

### Custom contracts

For more than one check, or to choose exactly what is frozen, write `necro.toml` yourself before you type
`/necro freeze`. Each contract is one frozen test: a command that exits 0 when the work is right, plus the files it
freezes:

```toml
[[contract]]
id = "C1"
says = "Shares add up to the total"
run = "python3 -m pytest tests/acceptance/test_split.py -q"
files = ["tests/acceptance/**"]
```

Any language works: `npm test -- tests/acceptance`, `cargo test --test acceptance`, `go test ./acceptance/...`.
Freeze the whole folder with a glob, so a new file there (a `conftest.py`, an `__init__.py`) counts as tampering.
A frozen folder cannot hold a credential file (a `.env`, a `*.pem` or `*.key` fixture), because those are never
opened. If your tests write files (reports, coverage), list them: `outputs = ["reports/*.xml"]`. Hermes can draft
`necro.toml` for you; read it, then type `/necro freeze`.

| Command | What it does |
|---|---|
| `/necro` or `/necro status` | What is frozen here, and the latest receipt; before a freeze, the tests it would lock |
| `/necro freeze` | Lock the contracts in `necro.toml` by hash; with no `necro.toml`, find the tests and write one first |
| `/necro freeze <command>` | With no `necro.toml`: lock the tests it finds, with your command as the one that must pass |
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
ignored file can change a run. Pins that git ignores are marked `ignored`. Credential files (`.env`, `.env.*`, keys,
`.netrc` and the like, by their own name) are never opened, not even to hash them: their pin, marked `credential`,
holds only that the file is there, its size, modification time, inode and change time. A name Python can import or run
(`*.py`, `*.pth`, `conftest*`, `sitecustomize*`, `usercustomize*`) is never treated as a credential file. A link is
never opened or followed, whatever it points to: its pin is its own text (what `readlink` gives), so a link that
appears, disappears or points elsewhere is a change like any other. Its target must be pinned on its own: a link to
something outside the project, in a folder never walked (`.necro`, `node_modules` and the like), in a declared output,
to a folder or to nothing fails the check, and `verify` says MISMATCH. A contract cannot freeze a credential file or a
link. Never walked, pinned or checked: version control and necro's own (`.git`, `.hg`, `.svn`, `.necro`), test-tool
caches (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`), installed dependencies
(`node_modules`, `.venv`, `venv`, `bower_components`, `.terraform`), build output (`target`, `.gradle`, `coverage`,
`.next`, `.nuxt`, `.svelte-kit`, `.turbo`, `.parcel-cache`), and `.pyc`, `.pyo` and `.DS_Store` files. A contract run
may write in those folders; any other file it creates or changes, ignored or not, fails the check unless `necro.toml`
declares it under `outputs`.

`verify` checks the receipt's hash, re-hashes every pinned file except credential files, looks for added files and
checks that the lock is exactly what freezing `necro.toml` would write. It re-runs the contracts only when a person
approved that lock for that folder, or when you add `--run` after reading `necro.toml`, because re-running executes
the project's commands. In another checkout, such as a fresh clone, a pin marked `ignored` or `credential` that is
missing there is listed as local-only, not a mismatch by itself, and the re-run decides: `CONFIRMED <hash> (1
local-only file not present here: .env).` A tracked pin that differs, or an ignored pin that is present but differs,
is a `MISMATCH`. A credential file's contents are never compared, because it is never opened: `verify` checks only its
size (a different size is a `MISMATCH`; its modification time, inode and change time differ in any copy or clone and
are ignored), says so in the verdict, `HASHES MATCH <hash> (1 local credential file not compared: .env)`, and the
re-run decides. A file the receipt does not pin is added, a `MISMATCH`, even when git ignores it (an ignored
`unittest.py` can change a run); only a regular file with a credential name of its own is noted instead: `(1 local
credential file not in the receipt: .env)`. The answer is `CONFIRMED`, `MISMATCH` (naming what changed), `SUPERSEDED`
(frozen again since), or `HASHES MATCH` (files match but nothing was re-run; not a confirmation).

## Settings

Under `plugins.entries.contract-necromancer.settings`: `gate_budget_seconds` (25), `check_budget_seconds` (600),
`hold_stream` (false; when true you see only the stamped final reply) and `stamp_done_claims` (true).

## Limits

- **Guard rails, not a sandbox.** The guard only stops the ordinary routes early (it does not catch, for example,
  `find` called by its full path); the hash check is the backstop: any change to a frozen file fails its contract,
  and a removed lock fails closed. A process running as your user that sets out to evade both can still change
  files and approvals (it can read the HMAC key). Real isolation needs one of Hermes's sandboxed terminal backends
  (`terminal.backend: docker`, `modal` and others), untested here.
- **A deleting find started outside the project** (`/`, `~`, or a relative path) is not blocked by the guard; any
  change to a frozen file still fails its tests, and a removed lock fails closed.
- **The person check.** Hermes runs the agent's commands on pipes, but a background command with `pty=true` gets a
  real terminal, so the plugin also checks its own `NECRO_AGENT_SESSION` marker, Hermes's `HERMES_AGENT` variable and
  whether a Hermes process started the shell. Any of them refuses.
- **Bounded.** After 3 nudges the reply goes out, stamped `Not done.` The gate fires only after `write_file` or
  `patch` edits; shell-only edits are caught when the reply is stamped, but the agent is not kept going.
- **`/necro freeze` locks test files, not settings.** Without a `necro.toml`, it freezes the test files it finds, not
  `package.json`, `pyproject.toml`, `pytest.ini`, `setup.cfg` or `Cargo.toml`, which can change which tests run (an
  edited `scripts.test`, or pytest `addopts`). Such an edit is pinned in the receipt but not blocked. To lock one, add
  it to `files` in `necro.toml` and type `/necro freeze` again. When a test folder holds a link or a credential file
  (which are never frozen), its other files are locked by name, so a file added there later is not caught.
- **What tests can see.** Code under test runs inside the test runner and can tamper with it. A file in the
  project that changes how a test runs (a `unittest.py` that shadows Python's own, even one git ignores) can make a
  local run pass; it is pinned and listed in the receipt, and `verify --run` in a fresh clone, which lacks it, fails.
  One that is only in the checkout that verifies is reported as added.
  Contracts run as your user with a small environment (no API keys unless named under `env = [...]`).
- **Credential files are never opened.** A contract run that changes one's size, modification time, inode or change
  time fails. `verify` compares only the size, so an edit after the receipt that keeps the size is not seen there; the
  verdict says the file was not compared, and the re-run decides.
- **Slow or huge.** Contracts slower than the gate's budget are reported as not checked (the agent is told to run
  `necro_check`, which has 10 minutes); in a very large project, list the files that matter under `sources`.
- Tested on macOS with Hermes 0.23.1 and 0.22.7, Python 3.11 and 3.14. Linux and Windows are untested here; the CI
  workflow runs the unit tests and the demo on Ubuntu and macOS on every push.

## What it touches

It runs the commands in your frozen `necro.toml` with your user's permissions and reads your project's files to hash
them, except credential files such as `.env`, which it never opens: it records only their size and file times. Links
are never opened or followed; it records their text. When you type `/necro freeze` where there is no `necro.toml`, it
reads the folder's file list, `package.json`, `pyproject.toml`, `setup.cfg`, `tox.ini`, `requirements*.txt` and the
first lines of Python test files (never through a link, never a credential file), looks the test command's program
up on PATH (for pytest it runs `python3 -c "import pytest"` in an empty folder), and writes `necro.toml`; `/necro
status` reads the same files. It writes `.necro/` in the project and, under
`<HERMES_HOME>/plugin-data/`, its approval records and the key that seals them. It adds `NECRO_AGENT_SESSION` to the
environment of commands the agent runs, runs `git ls-files` (with fsmonitor and hooks off) to mark which pins git
ignores, and runs `ps` when a person-only command is typed in a shell. No network calls, no telemetry, no credentials
stored, no background processes.

## Tests

```
python3 -m unittest discover -s tests                        # 310 tests, about 90 s
python3 tests/e2e/run_in_hermes.py --hermes $(which hermes)    # the real Hermes agent loop
```

The second runs `hermes chat` in a throwaway Hermes home against a local scripted stand-in for a model (no API key),
so it checks the plugin's wiring in Hermes's real agent loop, not how a particular model behaves. Its eighteen checks
include: the first "Done!" is refused; `write_file` and `execute_code` writes to the frozen contract are blocked;
freezing from a script is refused while `/necro freeze` works; a spliced approve command from the agent's own shell
is refused; the real fix gets a receipt that verifies. Then, in a fresh project with only code and unittest tests
and no `necro.toml`, one `/necro freeze` locks the tests, a freeze from a script writes nothing, and the gate holds
the same way.

## License

MIT
