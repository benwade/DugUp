"""Write the generated test tree into a folder, to try the program on by hand.

    python tests/make_corpus.py SOME_EMPTY_FOLDER
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corpus.tree import build_tree  # noqa: E402


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    rows = build_tree(argv[1])
    print(f"{len(rows)} files written to {os.path.abspath(argv[1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
