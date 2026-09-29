#!/bin/bash
# Run the checks CI runs, before pushing.
#
# CI's lint and test jobs are cheap and fast, and finding out about a failure
# from a red tick on a pull request wastes a round trip for something that
# takes a minute here. An import-sorting error has already cost one.
#
# Runs inside the same bookworm chroot as tools/build-iso-chroot.sh, because
# the interpreter and the installed packages are what CI has - a lint that
# passes against a different ruff, or a test suite that passes on a different
# Python, is not the question being asked.
#
# What this does NOT cover, and cannot:
#
#   The runner is not this machine. Its root filesystem is /dev/sda1, and a
#   test fixture that names a fake disk /dev/sda has already passed here and
#   failed there - the protection logic correctly refused to erase what it
#   believed was the running system. Nothing local reproduces that. Watch the
#   run on the pull request as well; this reduces the round trips, it does not
#   remove them.
set -u

CHROOT="${CHROOT:-/srv/zeroize-bookworm}"
SOURCE="${SOURCE:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK="$CHROOT/build/zeroize"

if [ "$(id -u)" -ne 0 ]; then
    echo "This needs root: it chroots. Try:  sudo bash $0" >&2
    exit 1
fi

if [ ! -x "$CHROOT/bin/bash" ]; then
    echo "No chroot at $CHROOT." >&2
    echo "Run tools/build-iso-chroot.sh once to create it." >&2
    exit 1
fi

mountpoint -q "$CHROOT/proc" || mount -t proc proc "$CHROOT/proc"

echo "==> staging $SOURCE"
rsync -a --delete \
    --exclude '.git/' \
    --exclude 'build/' \
    --exclude 'dist/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    "$SOURCE/" "$WORK/"

# Installed once and kept, so repeat runs cost nothing. Both --break-system-packages
# and the plain form are tried: which one is needed depends on the pip version,
# and guessing wrong fails the whole script over a flag.
chroot "$CHROOT" bash -c '
    command -v ruff >/dev/null 2>&1 || python3 -m ruff --version >/dev/null 2>&1 || {
        pip install --quiet --break-system-packages "ruff>=0.5" 2>/dev/null ||
        pip install --quiet "ruff>=0.5" 2>/dev/null
    }
    python3 -c "import pytest" 2>/dev/null || {
        apt-get install -y -qq python3-pytest >/dev/null 2>&1
    }
' || true

STATUS=0

echo
echo "==> ruff check            (CI: the Lint job)"
chroot "$CHROOT" bash -c 'cd /build/zeroize && python3 -m ruff check .' || STATUS=1

echo
echo "==> pytest                (CI: the Test job, on 3.11)"
chroot "$CHROOT" bash -c 'cd /build/zeroize && python3 -m pytest -q' || STATUS=1

echo
echo "==> shell syntax          (CI: the Shell scripts job)"
# -n parses without executing. It checks the outer script only: a quoted
# heredoc is opaque text to the parser, so a generated script inside one is
# checked separately by whatever generates it.
while IFS= read -r script; do
    bash -n "$script" || { echo "  FAIL $script"; STATUS=1; }
done < <(find "$SOURCE" -name '*.sh' -not -path '*/.git/*' -not -path '*/build/*')
echo "   done"

echo
if [ "$STATUS" -eq 0 ]; then
    echo "==> all local checks passed"
    echo "    The runner still differs from this machine - watch the PR run too."
else
    echo "==> FAILED" >&2
fi
exit "$STATUS"
