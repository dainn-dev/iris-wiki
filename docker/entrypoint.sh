#!/bin/sh
# Auto-register the KB mounted at /work (when present), then exec the real command
# (e.g. openkb-web, or a CLI invocation like `openkb add /work`).
set -e

if [ -d /work/.openkb ] && [ -d /work/wiki ]; then
    python -c "from openkb.config import register_kb; from pathlib import Path; register_kb(Path('/work'))" >/dev/null 2>&1 || true
fi

exec "$@"