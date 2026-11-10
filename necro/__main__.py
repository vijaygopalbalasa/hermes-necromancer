"""Standalone use without Hermes, for CI or a reviewer: python3 <plugin folder>/necro verify <receipt>.

Freezing here records no Hermes approval; inside Hermes run /necro approve once after reading the lock."""

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
    text, code = dispatch(" ".join(argv), cwd, Approvals(None))
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
