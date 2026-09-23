#!/bin/bash
#SBATCH --job-name=pfas_run2
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=40G
#SBATCH --time=48:00:00
#SBATCH --partition=normal

set -euo pipefail
project_root=${1:?Package directory is required}
jobs_file=${2:?Job-list file is required}
config_file=${3:?Configuration snapshot is required}
cd "$project_root"

module_names=$(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["modules"]))' "$config_file")
while IFS= read -r name; do
    module load "$name"
done <<< "$module_names"
export DFT_GAUSSIAN_MODULE
DFT_GAUSSIAN_MODULE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["modules"][-1])' "$config_file")

job=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$jobs_file")
python3 scripts/run_job.py "$job" --receipt "$(dirname "$jobs_file")/receipt.json"
