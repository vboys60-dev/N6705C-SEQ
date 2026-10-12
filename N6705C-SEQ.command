#!/bin/bash
cd "$(dirname "$0")"
source /opt/anaconda3/etc/profile.d/conda.sh
conda activate base
python3 -m n6705c_seq.gui
