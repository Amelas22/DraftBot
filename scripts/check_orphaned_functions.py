"""Fail a commit that leaves a function defined with nobody calling it.

The bug this exists for: a commit removes a mechanism's entry point, notices the
chain behind it is now unreachable, and deletes it as dead -- taking with it an
effect that rode on that chain and that the replacement never picked up.

That is what dcd80e3 did. It moved log posting to the publish timer, deleted the
orphaned collection chain, and with it the only call to
_handle_victory_aware_disconnect -- the teardown for a draft's Draftmancer
manager. The managers stopped being torn down, each pinned a ~500 KB draft log,
and eleven weeks later production was OOM-killed.

Nothing catches that by reading the diff: every line removed was genuinely
unreachable by then. What it leaves behind is a fingerprint -- a function still
defined that nothing calls any more -- and that IS checkable. So this compares
the reference count of every project function before and after, and objects to
any that just lost its last caller.

    pipenv run python scripts/check_orphaned_functions.py            # vs HEAD
    pipenv run python scripts/check_orphaned_functions.py --base X   # vs a ref

An intentional orphan (a public helper, something staged for a later slice) is
declared by name in ORPHANS_ALLOWED below, with a reason.
"""
import argparse
import re
import subprocess
import sys
from collections import Counter

# Directories whose functions are called by something other than Python code --
# a decorator, a test runner, a migration driver -- so "no callers" is normal.
SKIP_DIRS = ("tests/", "alembic/", "scripts/", "migrations/", ".claude/",
             "analysis/", "legacy_data/")

# Names that are entry points by nature: something outside the source tree
# invokes them. Dunders, pytest, and the bot's own event/command conventions.
ENTRY_POINT = re.compile(
    r"^(__\w+__|test_\w+|main|setup|teardown|upgrade|downgrade)$")

ORPHANS_ALLOWED: dict[str, str] = {
    # "function_name": "why it is allowed to have no callers",
}

IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
DEF = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", re.M)


def tracked_python(ref):
    """Python files in a tree. An empty ref means the INDEX, not the worktree:
    a pre-commit hook must judge what is about to be committed."""
    cmd = (["git", "ls-files"] if ref == ""
           else ["git", "ls-tree", "-r", "--name-only", ref])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines()
            if f.endswith(".py") and not f.startswith(SKIP_DIRS)]


def blobs(ref, paths):
    """Yield (path, source) for every path, in ONE git process.

    `git show` per file is a subprocess per file -- ~700 of them across two
    trees, which took five seconds and is how a pre-commit hook earns itself a
    --no-verify. cat-file --batch streams the same content from one.
    """
    spec = "".join(f"{ref}:{path}\n" for path in paths)
    proc = subprocess.run(["git", "cat-file", "--batch"], input=spec.encode(),
                          stdout=subprocess.PIPE, check=True)
    out, pos = proc.stdout, 0
    for path in paths:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        if len(header) < 3:                     # "<spec> missing"
            pos = end + 1
            continue
        size = int(header[2])
        body = out[end + 1:end + 1 + size]
        pos = end + 1 + size + 1                # trailing newline
        yield path, body.decode("utf-8", "replace")


def survey(ref):
    """(functions defined here, how often every identifier appears) for a tree.

    One pass over the files rather than a grep per name -- a search per function
    would be thousands of scans of the same tree.
    """
    defined, uses = {}, Counter()
    for path, src in blobs(ref, tracked_python(ref)):
        for name in DEF.findall(src):
            defined.setdefault(name, path)
        uses.update(IDENT.findall(src))
    return defined, uses


def callers(name, defined, uses):
    """Uses of `name` that are not its own `def` line.

    Deliberately name-based, not resolved: an attribute call, a string in a
    handler registration (`sio.on('endDraft', self._on_end_draft)`), a decorator
    reference and a plain call all count, because all of them mean somebody
    still reaches it. Over-counting only ever makes this quieter.
    """
    return uses[name] - (1 if name in defined else 0)


def main(base, head):
    old_defined, old_uses = survey(base)
    new_defined, new_uses = survey(head)   # "" -> the working tree, via :path

    orphans = []
    for name, path in sorted(new_defined.items()):
        if ENTRY_POINT.match(name) or name in ORPHANS_ALLOWED:
            continue
        if callers(name, new_defined, new_uses) == 0 \
                and callers(name, old_defined, old_uses) > 0:
            orphans.append((name, path))

    if not orphans:
        print("no function lost its last caller")
        return 0

    print(f"{len(orphans)} function(s) lost their last caller in this change:\n")
    for name, path in orphans:
        print(f"  {path}: {name}()  had callers before, has none now")
    print("\nAn effect that rode on the removed path may have gone with it.")
    print("Either delete the definition too, or re-wire it -- or add the name")
    print("to ORPHANS_ALLOWED in this script with a reason.")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="HEAD", help="ref to compare against")
    ap.add_argument("--head", default="", help="ref to check (default: working tree)")
    args = ap.parse_args()
    sys.exit(main(args.base, args.head))
