#!/usr/bin/env bash
# Build a scannable audit-target git repo from the fixture sources.
#
# The fixture sources live as plain files in fixtures/audit-target/ (no nested
# .git, which the outer repo could not track). This script copies them to a
# destination directory and constructs the git history the plan requires:
# a base commit, then one commit per planted defect with message "PLANT: <n>".
# Each plant lives in its own file, so the plant commit is the commit that
# introduces the defective file (see fixtures/PLANTS.md for ground truth).
#
# Usage: build_fixture.sh <dest-dir>
set -euo pipefail

src="$(cd "$(dirname "$0")/audit-target" && pwd)"
dest="${1:?usage: build_fixture.sh <dest-dir>}"

mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"
if [ -n "$(ls -A "$dest")" ]; then
  echo "build_fixture.sh: refusing to build into non-empty directory: $dest" >&2
  exit 1
fi

cp -R "$src/." "$dest/"
cd "$dest"

git init -q -b main
git config user.name "habeas-fixture"
git config user.email "habeas-fixture@localhost"

commit() { # commit <message> <path...>
  local msg="$1"
  shift
  git add "$@"
  git commit -q -m "$msg"
}

base_files=(
  Gemfile .rspec README.md
  lib/audit_target.rb
  lib/audit_target/logging.rb
  lib/audit_target/account.rb
  lib/audit_target/fake_db.rb
  lib/audit_target/ledger.rb
  spec/spec_helper.rb
  spec/account_spec.rb
  spec/ledger_spec.rb
)
[ -f Gemfile.lock ] && base_files+=(Gemfile.lock)

commit "base: toy payments library" "${base_files[@]}"
commit "PLANT: 1" lib/audit_target/reporting.rb spec/reporting_spec.rb
commit "PLANT: 2" lib/audit_target/pagination.rb spec/pagination_spec.rb
commit "PLANT: 3" lib/audit_target/transfer.rb spec/transfer_spec.rb
commit "PLANT: 4" lib/audit_target/token_auth.rb spec/token_auth_spec.rb
commit "PLANT: 5" lib/audit_target/fees.rb spec/fees_spec.rb

if [ -n "$(git status --porcelain)" ]; then
  echo "build_fixture.sh: leftover files not covered by the commit plan:" >&2
  git status --porcelain >&2
  exit 1
fi

echo "fixture built at $dest"
git log --oneline
