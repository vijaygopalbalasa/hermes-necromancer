# Contract Necromancer: how locks, receipts and the guard handle the hard cases

The README covers what the plugin does. This page keeps the exact rules: what a receipt pins, how links and
credential files are treated, what `verify` compares, and where the guard rails end.

## What a receipt pins

A receipt is `.necro/receipts/<sha256>.json`, named by the hash of its own contents. It pins the lock, every contract
file, each contract's result and every other project file by SHA-256, whether git ignores it or not, because an
ignored file can change a run. Pins that git ignores are marked `ignored`.

**Credential files** (`.env`, `.env.*`, keys, `.netrc` and the like, by their own name) are never opened, not even to
hash them: their pin, marked `credential`, holds only that the file is there, its size, modification time, inode and
change time. A name Python can import or run (`*.py`, `*.pth`, `conftest*`, `sitecustomize*`, `usercustomize*`) is
never treated as a credential file. A contract cannot freeze a credential file, so a frozen folder cannot hold one
(a `.env`, a `*.pem` or `*.key` fixture). When `/necro freeze` finds one in a test folder, it locks that folder's
other files by name instead of by glob, so a file added there later is not caught.

**Links** are never opened or followed, whatever they point to: a link's pin is its own text (what `readlink`
gives), so a link that appears, disappears or points elsewhere is a change like any other. Its target must be pinned
on its own: a link to something outside the project, in a folder never walked (`.necro`, `node_modules` and the
like), in a declared output, to a folder or to nothing fails the check, and `verify` says MISMATCH. A contract cannot
freeze a link.

**Never walked, pinned or checked:** version control and necro's own (`.git`, `.hg`, `.svn`, `.necro`), test-tool
caches (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`), installed dependencies
(`node_modules`, `.venv`, `venv`, `bower_components`, `.terraform`), build output (`target`, `.gradle`, `coverage`,
`.next`, `.nuxt`, `.svelte-kit`, `.turbo`, `.parcel-cache`), and `.pyc`, `.pyo` and `.DS_Store` files. A contract run
may write in those folders; any other file it creates or changes, ignored or not, fails the check unless `necro.toml`
declares it under `outputs`. For the same reason `/necro freeze` refuses a test command whose program lives in one of
those folders (`.venv/bin/pytest`): nothing there is locked.

## Pins: settings that shape the run

`pins = [...]` in a contract locks a setting without freezing the whole file it lives in:

```toml
pins = ["package.json#scripts.test", "pyproject.toml#tool.pytest", "setup.cfg#[tool:pytest]", "pytest.ini",
        "**/conftest.py"]
```

A field is a dotted path in a `.json` or `.toml` file (`scripts.test`, `tool.pytest`, `bin.*.test`, where `*` means
every item and a segment in double quotes may hold a dot), or a `[section]` of a `.cfg` or `.ini` file. A pin with no
field locks a whole file, or that it stays absent, so adding it later counts; a glob locks the set of files it
matches. The lock keeps a hash of what each pin sees. A change counts as tampering, with a plain reason ("the test
command in package.json changed (scripts.test)"), while edits elsewhere in the file, such as dependencies, stay
allowed. A pinned file that no longer parses fails closed, and a pin never opens a credential file or reads through a
link.

`/necro freeze` writes these pins for you: the test script in `package.json`, every script it runs and their pre and
post scripts, the `jest`, `mocha` and `ava` keys, and the jest, vitest, vite and mocha config files; pytest's
sections in `pyproject.toml`, `setup.cfg` and `tox.ini`, `pytest.ini` and its variants, and every `conftest.py`;
Cargo's `[[test]]` tables, its `test`, `harness`, `doctest`, `autotests` and `build` keys, `build.rs` and
`.cargo/config.toml`. A config file that exists is frozen whole; one that does not is pinned as absent. For your own command it also freezes the
runner's file whole (a Makefile for `make`, `tox.ini`, `noxfile.py`, a justfile, a Taskfile) and any project file the
command names, including inside `sh -c "..."`, and says plainly when it names one that cannot be locked.

## What verify compares

`verify` checks the receipt's hash, re-hashes every pinned file except credential files, looks for added files and
checks that the lock is exactly what freezing `necro.toml` would write. It re-runs the contracts only when a person
approved that lock for that folder, or when you add `--run` after reading `necro.toml`, because re-running executes
the project's commands.

In another checkout, such as a fresh clone, a pin marked `ignored` or `credential` that is missing there is listed as
local-only, not a mismatch by itself, and the re-run decides: `CONFIRMED <hash> (1 local-only file not present here:
.env).` A tracked pin that differs, or an ignored pin that is present but differs, is a `MISMATCH`. A credential
file's contents are never compared, because it is never opened: `verify` checks only its size (a different size is a
`MISMATCH`; its modification time, inode and change time differ in any copy or clone and are ignored), says so in the
verdict, `HASHES MATCH <hash> (1 local credential file not compared: .env)`, and the re-run decides. A credential file
whose size stays the same after an edit is not seen by `verify`; a contract run that changes one's size, modification
time, inode or change time fails.

A file the receipt does not pin is added, a `MISMATCH`, even when git ignores it (an ignored `unittest.py` can change
a run); only a regular file with a credential name of its own is noted instead: `(1 local credential file not in the
receipt: .env)`. The answer is `CONFIRMED`, `MISMATCH` (naming what changed), `SUPERSEDED` (frozen again since), or
`HASHES MATCH` (files match but nothing was re-run; not a confirmation).

## Where the guard rails end

- **The guard stops the ordinary routes early.** `write_file`, `patch`, `terminal` and `execute_code` calls that would
  write, delete or rename a frozen file, `necro.toml`, `.necro/` or the approval store are blocked with a reason, as
  is a `find` over a frozen project that deletes, writes or runs anything but a plain reader, and any command or code
  too long to check inside one. It does not catch, for example, `find` called by its full path, or a deleting `find`
  started outside the project (`/`, `~`, or a relative path). The hash check is the backstop: any change to a frozen
  file fails its contract, and a removed lock fails closed.
- **A process that sets out to evade both** can still change files and approvals: it runs as your user and can read
  the HMAC key that seals the approval records. Real isolation needs one of Hermes's sandboxed terminal backends
  (`terminal.backend: docker`, `modal` and others), untested here.
- **The person check.** Hermes runs the agent's commands on pipes, but a background command with `pty=true` gets a
  real terminal, so the plugin also checks its own `NECRO_AGENT_SESSION` marker, Hermes's `HERMES_AGENT` variable and
  whether a Hermes process started the shell. Any of them refuses `freeze`, `approve` and `unfreeze` from a shell.
- **What tests can see.** Code under test runs inside the test runner and can tamper with it, and settings outside the project (a `pytest.ini` in a parent folder) or in a workspace member's `Cargo.toml` are not pinned. A file in the project that changes how a test runs (a
  `unittest.py` that shadows Python's own, even one git ignores) can make a local run pass; it is pinned and listed in
  the receipt, and `verify --run` in a fresh clone, which lacks it, fails. Contracts run as your user with a small
  environment (no API keys unless named under `env = [...]`).
