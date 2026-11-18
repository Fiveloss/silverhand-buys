"""Check a candidate public snapshot without committing, publishing or using the network.

Uses tracked and non-ignored new files from the working tree, then applies the same
path exclusions, overlays and placeholder replacement as publish.sh. Runs both offline
test suites in the resulting temporary tree. Publishing still exports committed HEAD.
"""

import fnmatch
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def lines(path):
    if not path.exists():
        return []  # export rules are not shipped in the public repository
    return [s for raw in path.read_text(encoding="utf-8").splitlines()
            if (s := raw.strip()) and not s.startswith("#")]


def excluded(name, patterns):
    parts = name.split("/")
    return any(fnmatch.fnmatchcase("/".join(parts[:i]), pattern.rstrip("/"))
               for i in range(1, len(parts) + 1) for pattern in patterns)


def prepare(target: Path, slug: str):
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    ).decode("utf-8").split("\0")
    patterns = lines(ROOT / ".publicignore")
    for name in names:
        if not name or excluded(name, patterns):
            continue
        source = ROOT / name
        if source.is_symlink():
            raise ValueError(f"symlink requires review: {name}")
        if source.is_file():
            out = target / name
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, out)
    overlay = target / "public"
    if overlay.is_dir():
        for source in list(overlay.rglob("*")):
            if source.is_file():
                out = target / source.relative_to(overlay)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, out)
        shutil.rmtree(overlay)
    for path in target.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            if b"Fiveloss/silverhand-buys" in data:
                path.write_bytes(data.replace(b"Fiveloss/silverhand-buys", slug.encode("utf-8")))


def violations(target: Path):
    deny = [s.casefold() for s in lines(ROOT / ".publicdeny")]
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue  # like grep -I: binary assets are not text
        text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if any(word in line.casefold() for word in deny):
                yield f"{path.relative_to(target)}:{number}: private word"
            for match in re.finditer(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", line):
                if match[0] not in ("127.0.0.1", "0.0.0.0"):
                    yield f"{path.relative_to(target)}:{number}: IP address"


def main():
    slug = sys.argv[1] if len(sys.argv) > 1 else "Fiveloss/silverhand-buys"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        raise SystemExit("Expected owner/repo")
    with tempfile.TemporaryDirectory(prefix="buybot-public-") as folder:
        private_rules = (ROOT / ".publicdeny").exists() and (ROOT / ".publicignore").exists()
        if not private_rules:
            print("Private export rules are absent; checking the public working tree and tests only.")
        tree = Path(folder)
        prepare(tree, slug)
        bad = list(violations(tree))
        if bad:
            raise SystemExit("Public snapshot rejected:\n" + "\n".join(bad))
        for suite in ("tests.py", "tests_bot.py"):
            subprocess.run([sys.executable, suite], cwd=tree, check=True)
        print("Candidate snapshot passed the available export checks and both test suites; nothing published.")


if __name__ == "__main__":
    main()
