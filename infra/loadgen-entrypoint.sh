#!/bin/sh
# Entry point wrapper for the demo's load-generator container (see docker-compose.override.yaml).
# Disables the `ask_agent` Locust task by setting its weight to 0, because the demo's AI agent
# service is not part of this stack. Then hands over to the image's normal command.
set -eu

python3 - <<'PY'
import pathlib

path = pathlib.Path("/usr/src/app/locustfile.py")
text = path.read_text()
needle = "@task(3)\n    def ask_agent"
if needle in text:
    path.write_text(text.replace(needle, "@task(0)\n    def ask_agent"))
    print("incident-agent: disabled ask_agent task")
else:
    print("incident-agent: WARNING ask_agent task not found; upstream locustfile changed?")
PY

cd /usr/src/app
exec locust --skip-log-setup "$@"
