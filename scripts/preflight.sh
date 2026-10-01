#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
module load devel python/3.12.1
python3 -c 'import sys; assert sys.version_info >= (3,9), "Python 3.9 or newer is required"'
python3 scripts/validate.py
python3 scripts/backup.py --check
command -v sbatch
: "${SCRATCH:?Run on Sherlock with SCRATCH defined}"
case "$(pwd -P)/" in "$SCRATCH/"*) ;; *) echo 'Copy this package under $SCRATCH before submission.'; exit 1;; esac
while IFS= read -r name; do
    module load "$name"
done < <(python3 -c 'import json; print("\n".join(json.load(open("sherlock.json"))["modules"]))')
module list
command -v g16
command -v formchk
sinfo -o '%P %l %a'
df -h "$SCRATCH"
printf '\nRead-only preflight complete. Gaussian execution has not been tested.\n'
