#!/usr/bin/env python3
"""Non-interactive KB initializer for the container.

Baked into the image at /usr/local/bin/openkb-init. Run it against a freshly
mounted, empty /work (or re-run on an existing KB):

    docker run --rm \
        -v "<host-folder>:/work" \
        -e LLM_API_KEY=... \
        -e OPENAI_API_BASE=https://open-claude.dainn.online/v1 \
        -e OPENKB_MODEL="openai/opencode-go/deepseek-v4-flash" \
        openkb:latest openkb-init

Environment variables:
  OPENKB_MODEL  (default openai/opencode-go/deepseek-v4-flash)
  LLM_API_KEY
  OPENAI_API_BASE
  OPENKB_LANGUAGE (default en)
"""

import os
import sys
from pathlib import Path

from openkb.cli import initialize_kb
from openkb.config import load_config, save_config

kb = Path("/work")

try:
    initialize_kb(
        kb,
        model=os.environ.get("OPENKB_MODEL") or "openai/opencode-go/deepseek-v4-flash",
        api_key=os.environ.get("LLM_API_KEY"),
        openai_api_base=os.environ.get("OPENAI_API_BASE"),
    )
    print("Initialized new knowledge base at", kb)
except FileExistsError:
    # Already initialized: load and patch the existing config instead.
    cfg = load_config(kb / ".openkb" / "config.yaml")
    if "OPENKB_MODEL" in os.environ:
        cfg.setdefault("model", os.environ["OPENKB_MODEL"])

# Workaround for the open-claude.dainn.online gateway: it 403s requests whose
# User-Agent is the official OpenAI SDK UA. Override it on every LLM call.
cfg = load_config(kb / ".openkb" / "config.yaml")
cfg["extra_headers"] = {"User-Agent": "python-urllib/3.12"}
save_config(kb / ".openkb" / "config.yaml", cfg)

# Persist LLM credentials locally so later `openkb add/query/chat` runs work.
env_path = kb / ".env"
pairs = []
if "LLM_API_KEY" in os.environ:
    pairs.append(f"LLM_API_KEY={os.environ['LLM_API_KEY']}")
if "OPENAI_API_BASE" in os.environ:
    pairs.append(f"OPENAI_API_BASE={os.environ['OPENAI_API_BASE']}")
if pairs:
    env_path.write_text("\n".join(pairs) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass

print("Config written to", kb / ".openkb/config.yaml")
sys.exit(0)