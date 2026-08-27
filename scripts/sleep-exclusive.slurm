#!/bin/bash

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

#SBATCH --job-name=sleep60
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --exclusive
#SBATCH --time=00:02:00
# Optional:
# SBATCH --partition=<your_partition>
# SBATCH --account=<your_account>

echo "Allocated node(s): $SLURM_NODELIST"
sleep 60
echo "Done."