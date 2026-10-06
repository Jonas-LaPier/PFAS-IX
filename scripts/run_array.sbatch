#!/bin/bash
set -euo pipefail
project_root=${1:?Package snapshot is required}
jobs_file=${2:?Job list is required}
results_root=${3:?Results directory is required}
cd "$project_root"
export PYTHONDONTWRITEBYTECODE=1
receipt="$(dirname "$jobs_file")/receipt.json"
job="array_task_${SLURM_ARRAY_TASK_ID}"
trap 'python3 scripts/health.py "$receipt" "$job" --failure "Batch setup failed" || true' ERR
module load devel python/3.12.1
job=$(python3 scripts/health.py "$receipt" "$SLURM_ARRAY_TASK_ID" --claim)
python3 scripts/health.py "$receipt" "$job"
python3 scripts/validate.py
while IFS= read -r name; do
    module load "$name"
done < <(python3 -c 'import json; print("\n".join(json.load(open("sherlock.json"))["modules"]))')
command -v g16
command -v formchk
export DFT_GAUSSIAN_MODULE=gaussian/g16.B01
trap - ERR
exec python3 scripts/run_job.py "$job" --receipt "$receipt" --results-root "$results_root"
