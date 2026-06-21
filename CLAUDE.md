# CLAUDE.md

Guidance for Claude Code (and other AI assistants) lives in **[AGENTS.md](AGENTS.md)** — read it first.

Quick reminders specific to this repo:
- Internal `DNS_TLD` vs public `PUBLIC_TLD` is the central concept — see [docs/dns-tld-split.md](docs/dns-tld-split.md).
- Never commit secrets; templatize to `${ENV}` (the `.gitignore` enforces this).
- One `balena push` at a time; validate `haproxy.cfg` with `haproxy -c` before reloading.
- Device VPN/SSH access has real limits — see the "Device access reality" section in AGENTS.md before attempting to "remote in and fix" a device.
