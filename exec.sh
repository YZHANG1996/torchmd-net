#!/bin/sh

APPDIR=`dirname $0`
cd ./repo
wget https://github.com/conda-forge/miniforge/releases/latest/download/Mambaforge-Linux-x86_64.sh
bash Mambaforge-Linux-x86_64.sh <<EOF
yes
yes
EOF
source ~/.bashrc
mamba env create -f environment.yml
mamba activate torchmd-net
pip install -e .
# mkdir logs
# cp examples/ET-QM9.yaml .
# mv ET-QM9.yaml input.yaml
# mv input.yaml logs/
CUDA_VISIBLE_DEVICES=0 python scripts/train.py --conf examples/ET-QM9.yaml $@
return $?

