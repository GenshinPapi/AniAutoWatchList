.PHONY: test shellcheck gui-check package-check install-user uninstall-user export-upload-tree

PYTHON ?= $(shell if [ -x .venv/bin/python ]; then printf ".venv/bin/python"; else printf "python3"; fi)

test:
	$(PYTHON) -m pytest

shellcheck:
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck ani-cli/ani-cli; \
	else \
		echo "shellcheck not installed; skipping"; \
	fi

gui-check:
	@if [ -x .venv/bin/ani-watch-gui ]; then \
		.venv/bin/ani-watch-gui --check; \
	else \
		PYTHONPATH="$$(pwd)/src$${PYTHONPATH:+:$$PYTHONPATH}" $(PYTHON) -m ani_watchlist.gui --check; \
	fi

package-check:
	scripts/package-check.sh

install-user:
	scripts/install-user.sh

uninstall-user:
	scripts/uninstall-user.sh

export-upload-tree:
	@if [ -z "$(DEST)" ]; then \
		echo "usage: make export-upload-tree DEST=/tmp/ani-watchlist-upload"; \
		exit 2; \
	fi
	scripts/export-upload-tree.sh "$(DEST)"
