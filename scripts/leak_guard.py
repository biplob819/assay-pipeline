#!/usr/bin/env python3
"""Keep private words (the job-search target, OWNER D-18) out of anything git would publish.

The words live in `.leakguard` (git-ignored, one per line), so neither this script nor its
tests publish them. Matching is a substring search over a normalised letter stream, so
joined forms (`WordLabs`), URL slugs, domains, other encodings and look-alike letters match.

    python scripts/leak_guard.py          # exit 1 and list files when a word is found
    python scripts/leak_guard.py --self-test
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / ".leakguard"

# Look-alike letters folded to Latin after compatibility and accent folding (NFKD): a listed table
# of common Cyrillic, Greek, Armenian and Latin-extension look-alikes and small capitals.
# Not the full Unicode confusables set; the scope is stated in D-25.
HOMOGLYPHS = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
        "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h", "ӏ": "l", "к": "k", "м": "m", "н": "h",
        "т": "t", "в": "b", "ԛ": "q", "ԝ": "w", "ɡ": "g", "ɑ": "a", "ı": "i", "ȷ": "j",
        "ο": "o", "ρ": "p", "α": "a", "ν": "v", "κ": "k", "ι": "i", "τ": "t", "ε": "e",
        "μ": "u", "υ": "u", "χ": "x", "γ": "y", "ѵ": "v", "օ": "o", "ս": "u", "հ": "h",
        "ո": "n", "ց": "g", "ք": "p", "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e",
        "ꜰ": "f", "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m",
        "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v",
        "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
    }
)  # fmt: skip


def letters(text: str) -> str:
    """Fold compatibility forms and accents (NFKD), case and listed look-alikes, then keep
    ASCII letters and digits only (combining marks drop out with every other non-ASCII)."""
    folded = unicodedata.normalize("NFKD", text).casefold().translate(HOMOGLYPHS)
    return "".join(ch for ch in folded if ch.isascii() and ch.isalnum())


VIEWS = ("utf-8", "latin-1", "utf-16-le", "utf-16-be")


def views(data: bytes) -> list[str]:
    """Every plausible decoding of a file: UTF-8, Latin-1 and both UTF-16 byte orders (a BOM
    decodes to a character the letter filter drops), always attempted."""
    even = data[: len(data) - len(data) % 2]
    return [(even if "16" in enc else data).decode(enc, errors="replace") for enc in VIEWS]


def load_terms(path: Path = TERMS_FILE) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    terms = [letters(line) for line in lines if line.strip() and not line.lstrip().startswith("#")]
    return [t for t in terms if t]


def text_hits(label: str, data: bytes, terms: list[str]) -> bool:
    streams = [letters(v) for v in views(data)] + [letters(label)]
    return any(t in s for t in terms for s in streams)


def hits(files: list[Path], terms: list[str]) -> list[str]:
    """Files whose content or path contains a private word."""
    return [f.as_posix() for f in files if text_hits(f.as_posix(), f.read_bytes(), terms)]


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def staged_hits(terms: list[str], root: Path = ROOT) -> list[str]:
    """What the next commit would publish: staged blobs (not the working tree) and paths."""
    names = _git(root, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
    found = []
    for rel in (n for n in names.decode("utf-8").split("\0") if n):
        if text_hits(rel, _git(root, "show", f":{rel}"), terms):
            found.append(rel)
    return found


def publishable_files(root: Path = ROOT) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [root / p for p in out.split("\0") if p and (root / p).is_file()]


def self_test() -> int:
    import tempfile

    term = [letters("ExampleCorp")]
    fullwidth = "".join(chr(ord(c) + 0xFEE0) for c in "ExampleCorp")  # Ｅｘａｍｐｌｅ…
    samples = {
        # each layer has a sample that only it catches (see the mutation test)
        "plain.md": (b"applying to ExampleCorp", True),
        "joined.py": (b"x = 'ExampleCorpLabs'", True),
        "slug.txt": (b"https://jobs.example.io/examplecorplabs/jobs/1", True),
        "domain.txt": (b"mail me at hr@examplecorp-labs.com", True),
        "spaced.txt": (b"Example Corp", True),
        "utf16_le_fullwidth.txt": (fullwidth.encode("utf-16-le"), True),
        "utf16_be_fullwidth.txt": (fullwidth.encode("utf-16-be"), True),
        "utf16_bom.txt": ("ExampleCorp".encode("utf-16"), True),
        "latin1_accent.txt": ("Éxample Corp".encode("latin-1"), True),
        "nfkd_fullwidth_utf8.txt": (fullwidth.encode(), True),
        "nfkd_accents.txt": ("Exámplé Cörp".encode(), True),
        "casefold_upper.txt": (b"EXAMPLECORP", True),
        "homoglyph_cyrillic.txt": ("Ехаmple Соrp".encode(), True),
        "homoglyph_greek.txt": ("Εxample Cοrp".encode(), True),  # Greek Ε, ο
        "homoglyph_armenian.txt": ("Example Cօrp".encode(), True),
        "dotless_i.txt": ("ExampleCorp Inıtıal".encode(), True),
        "small_caps.txt": ("ᴇxᴀᴍᴘʟᴇ ᴄᴏʀᴘ".encode(), True),
        "latin_alpha.txt": ("Exɑmple Corp".encode(), True),
        "clean.md": (b"nothing to see here", False),
        "near_miss.md": (b"Example Co. and a corporate plan", False),
    }
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, (data, expect) in samples.items():
            f = Path(tmp) / name
            f.write_bytes(data)
            got = bool(hits([f], term))
            failures += got != expect
            print(f"  [{'ok' if got == expect else 'WRONG'}] {name}: {'hit' if got else 'clean'}")
    print("self-test", "PASSED" if not failures else f"FAILED ({failures})")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--staged", action="store_true", help="check what the next commit adds")
    ap.add_argument("--message", type=Path, help="check a commit message file (commit-msg hook)")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    terms = load_terms()
    if not terms:
        print("leak guard: no .leakguard file here; nothing to check")
        return 0
    if args.message:
        bad = text_hits("", args.message.read_bytes(), terms)
        print("leak guard: commit message", "FAIL" if bad else "OK")
        return 1 if bad else 0
    if args.staged:
        found = staged_hits(terms)
        scope = "staged files"
    else:
        files = publishable_files()
        found = hits(files, terms)
        scope = f"{len(files)} files"
    for f in found:
        print(f"leak guard: private word in {f}")
    print("leak guard:", "FAIL" if found else "OK", f"({scope})")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
