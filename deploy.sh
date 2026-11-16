#!/usr/bin/env bash
# Redeploy the stack from this checkout: fast-forward to origin/main, pull the
# images, recreate what changed. Safe to re-run.
#
# On the VPS this is also the forced command of the CI deploy key
# (command="…/deploy.sh",restrict in ~/.ssh/authorized_keys): whatever the
# client asks to run, this script is all that key can execute.
set -euo pipefail

# Everything runs from main(), parsed in full before the first line executes:
# the `git merge` below may rewrite this very file, and bash otherwise keeps
# reading a script from disk as it goes.
main() {
	cd "$(dirname "$(readlink -f "$0")")"

	# Two pushes in a row (fork image + this repo) must not interleave.
	exec 9>.deploy.lock
	flock 9

	echo "==> updating checkout"
	git fetch --quiet origin main
	git merge --ff-only --quiet origin/main
	git log -1 --format='    %h %s'

	echo "==> pulling images"
	docker compose pull --quiet

	echo "==> starting stack"
	docker compose up -d --remove-orphans --wait --wait-timeout 300

	# `main` moves on every push, leaving the previous build untagged. The
	# label keeps the prune off every other image on the machine.
	echo "==> pruning superseded Rallly images"
	docker image prune -f --filter "label=org.opencontainers.image.source=https://github.com/Wiibleyde/rallly" >/dev/null

	docker compose ps --format 'table {{.Service}}\t{{.Image}}\t{{.Status}}'
}

main "$@"
