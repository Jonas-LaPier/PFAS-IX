#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
module load devel python/3.12.1
exec python3 scripts/submit.py "$@"
