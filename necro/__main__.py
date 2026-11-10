"""Standalone use without Hermes, for CI or a reviewer: python3 <plugin folder>/necro verify <receipt> --run.

There are no approvals here, so verify re-runs contracts only with --run (after you read necro.toml).
freeze, approve and unfreeze refuse unless a person is typing at a terminal outside any agent; a lock
frozen here must still be accepted once inside Hermes with /necro approve."""

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from necro.commands import Approvals, dispatch  # noqa: E402


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cwd = Path.cwd()
    if "--dir" in argv:
        i = argv.index("--dir")
        cwd = Path(argv[i + 1])
        del argv[i:i + 2]
    text, code = dispatch(shlex.join(argv), cwd, Approvals(None), from_shell=True)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
