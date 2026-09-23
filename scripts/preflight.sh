#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/validate.py
command -v sbatch
: "${SCRATCH:?Run on Sherlock with SCRATCH defined}"
case "$(pwd -P)/" in "$SCRATCH/"*) ;; *) echo 'Copy this package under $SCRATCH before running jobs.'; exit 1;; esac
while IFS= read -r name; do module load "$name"; done < <(python3 -c 'import json;print("\n".join(json.load(open("sherlock.json"))["modules"]))')
module list
command -v g16
command -v formchk
sinfo -o '%P %l %a'
df -h "$SCRATCH"
printf '\nPreflight complete. Check your partition/account access before the pilot.\n'
