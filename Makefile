# open-balena-extension — one-stop entry point.
# `make` (no target) prints this help.
.DEFAULT_GOAL := help

INVENTORY ?= ansible/inventory.ini

.PHONY: help bootstrap ansible-config doctor deploy deploy-% secret-scan lint

help: ## Show this help
	@grep -hE '^[a-zA-Z_%-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Scaffold .env + generate secrets (safe to re-run)
	./scripts/bootstrap.sh

ansible-config: ## Scaffold .env AND ansible/group_vars/all.yml
	./scripts/bootstrap.sh --ansible

doctor: ## Preflight + health checks (tooling, DNS wildcard, live endpoints)
	./scripts/doctor.sh

deploy: ## Full Ansible deploy (core + all extensions)
	cd ansible && ansible-playbook -i $(notdir $(INVENTORY)) site.yml

deploy-%: ## Deploy one component, e.g. `make deploy-builder`
	cd ansible && ansible-playbook -i $(notdir $(INVENTORY)) site.yml --tags $*

lint: ## Lint shell + node + scan for secrets (what CI runs)
	@for f in $$(git ls-files '*.sh'); do bash -n "$$f" && echo "bash -n  $$f"; done
	@node --check components/imagemaker/server.js && echo "node --check  components/imagemaker/server.js"
	@if command -v shellcheck >/dev/null; then shellcheck -S error $$(git ls-files '*.sh') && echo "shellcheck  ok"; \
	  else echo "shellcheck not installed — skipped (CI runs it)"; fi
	@$(MAKE) --no-print-directory secret-scan

secret-scan: ## Fail if a real secret looks committed (placeholders/examples ignored)
	@hits=$$(git grep --untracked -nIE '(BEGIN [A-Z ]*PRIVATE KEY|[0-9a-f]{24,})' \
	  -- . ':!*.example' ':!.gitignore' ':!docs/*' ':!Makefile' ':!scripts/*' ':!CONTRIBUTING.md' \
	  | grep -vE '\{\{|\$$\{' || true); \
	if [ -n "$$hits" ]; then echo "$$hits"; echo "^ potential secret — remove before commit"; exit 1; \
	else echo "clean"; fi
