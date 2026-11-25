"""No customer-name list is tracked in this repository, and no image ships .ci/.

Customer-name checks run on the maintainers' machines before a push; no list is kept in this
repository. This test fails when a name list file is tracked again: .ci/name-denylist.txt,
.ci/name-allowlist.txt (at any depth) or any file whose name contains "denylist". It is a
file-name check by design: a content check would need the list, which is never kept here.
It also fails when a Dockerfile at the root copies the whole build context while .ci/ exists and
.dockerignore does not leave .ci/ out.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIST_FILES = (".ci/name-denylist.txt", ".ci/name-allowlist.txt")
COPY_ALL = re.compile(r"^\s*(COPY|ADD)\s+(--\S+\s+)*\.\/?\s+\S+", re.I | re.M)


def name_list_files(paths):
    """The paths in PATHS that are a customer-name list."""
    hits = []
    for p in paths:
        base = p.rsplit("/", 1)[-1].lower()
        if any(p == f or p.endswith("/" + f) for f in LIST_FILES) or "denylist" in base:
            hits.append(p)
    return hits


def tracked_files():
    """Tracked paths of the checkout at ROOT, or None when ROOT is not the top of a git checkout
    (a package build tree, an unpacked source archive)."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT,
                             capture_output=True, text=True)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != ROOT:
            return None
        ls = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in ls.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def ci_left_out(dockerignore_text):
    lines = {l.strip() for l in dockerignore_text.splitlines()}
    return bool(lines & {".ci", ".ci/", "/.ci", "/.ci/", ".ci/**", "/.ci/**"})


class NoNameListTracked(unittest.TestCase):
    def test_matcher(self):
        paths = [".ci/name-denylist.txt", ".ci/name-allowlist.txt", "src/x/.ci/name-allowlist.txt",
                 "docs/site-DenyList.yaml", "src/node.py", ".ci/authorship_check.py",
                 "scripts/public_param_allowlist.txt"]
        self.assertEqual(name_list_files(paths), paths[:4])
        self.assertTrue(COPY_ALL.search("FROM x\nCOPY . /opt/app\n"))
        self.assertTrue(COPY_ALL.search("COPY --chown=0:0 ./ ./src\n"))
        self.assertFalse(COPY_ALL.search("COPY config.yaml /config.yaml\nCOPY src ./src\n"))
        self.assertTrue(ci_left_out(".git\n.ci/\n"))
        self.assertFalse(ci_left_out(".git\ntests\n"))

    def test_no_name_list_is_tracked(self):
        files = tracked_files()
        if files is None:
            self.skipTest("not a git checkout")
        self.assertEqual(name_list_files(files), [],
                         "customer-name lists are not kept in the repository")

    def test_images_do_not_ship_ci(self):
        """A root Dockerfile that copies the whole build context leaves .ci/ out (.dockerignore)."""
        files = tracked_files()
        if files is None:
            self.skipTest("not a git checkout")
        if not any(p.startswith(".ci/") for p in files):
            self.skipTest("no .ci/ in this repository")
        for name in sorted(p for p in files if "/" not in p and p.startswith("Dockerfile")):
            if COPY_ALL.search((ROOT / name).read_text(encoding="utf-8", errors="replace")):
                ignore = ROOT / ".dockerignore"
                self.assertTrue(ignore.is_file() and ci_left_out(ignore.read_text()),
                                f"{name} copies the whole context: .dockerignore must leave out .ci/")


if __name__ == "__main__":
    unittest.main()
