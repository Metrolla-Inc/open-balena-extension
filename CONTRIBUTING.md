# Contributing

Contributions welcome — this toolkit exists to make self-hosted openBalena more capable for
everyone.

## Ground rules
- **Never commit secrets.** Templatize to `${ENV}`; the `.gitignore` blocks `.env`, `*.key`,
  `certs/`, images. Run the secret scan before pushing (see below).
- **Keep `components/` and `ansible/` in sync.** If you change a script, update the role that
  deploys it (and vice-versa).
- **Parameterize.** New hostnames/paths go through `dns_tld` / `public_tld` / `install_root` /
  `service_user`, not hardcoded values.
- **Update docs + AGENTS.md** when you change an invariant (the things in AGENTS.md → "Hard invariants").

## Secret scan before pushing
```bash
git grep -nIE '([0-9a-f]{32}|BEGIN .*PRIVATE KEY|_SECRET *[:=] *"?[^$" ]|ACCESS_KEY *[:=] *"?[^$" ])' \
  -- . ':!*.example' ':!.gitignore' || echo "clean"
```
Any hit that isn't a `${VAR}` placeholder or example must be removed before commit.

## Testing changes
- Compose: `docker compose config` to validate interpolation.
- haproxy: validate with `haproxy -c` inside the running container before reloading.
- Ansible: `ansible-playbook --syntax-check site.yml` and run with `--check --diff` against a test host.

## Scope
PRs that generalize away deployment-specific assumptions, add device-type coverage to the
imagemaker, or improve the Ansible roles are especially welcome.
