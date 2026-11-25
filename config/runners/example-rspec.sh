#!/usr/bin/env bash
# Example proof test runner for the deep-audit engine: runs specs inside the project's
# Docker image on the project's dev network, so its database and other services are used.
# Copy, adjust the names below, and point target.engine_test_command / root_test_command at it.
#
# usage (cwd = the audit clone): example-rspec.sh <spec paths, relative to the clone>
#   HABEAS_RAW=<shell command>  run that instead of rspec (database setup)
# Give the audit its own test database and its own gem volume, so it never touches the
# developer's.
set -euo pipefail
WT="$(pwd)"
IMAGE=${HABEAS_IMAGE:?set HABEAS_IMAGE to the project image}
NETWORK=${HABEAS_NETWORK:?set HABEAS_NETWORK to the dev network}
DB=${HABEAS_TEST_DB:-app_test_audit}
first="${1:-}"
if [ -n "${HABEAS_RAW:-}" ]; then
  cmd="$HABEAS_RAW"
elif [[ "$first" == engines/*/* ]]; then
  engine="${first#engines/}"; engine="${engine%%/*}"
  cmd="cd engines/$engine && bundle exec rspec $(printf '%q ' "${@/#engines\/$engine\//}")"
else
  cmd="bundle exec rspec $(printf '%q ' "$@")"
fi
exec docker run --rm --network "$NETWORK" \
  -e RAILS_ENV=test -e DB_DATABASE="$DB" \
  -v "$WT":/app -v "${HABEAS_BUNDLE_VOLUME:-habeas_audit_bundle}":/bundle \
  -w /app "$IMAGE" bash -lc "$cmd"
