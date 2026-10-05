#!/usr/bin/env bash
# コマンドを実行し、失敗したら出力の最後を GitHub の注釈（::error::）に出す（ログを開かなくても原因が分かるように）.
#   .github/scripts/run.sh <コマンド...>
set -uo pipefail
log="$(mktemp)"
"$@" 2>&1 | tee "$log"
code=${PIPESTATUS[0]}
if [ "$code" -ne 0 ]; then
  grep -v '^\s*$' "$log" | tail -40 | sed -e 's/%/%25/g' -e 's/^/::error::/'
fi
exit "$code"
