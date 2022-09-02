#!/bin/sh

APPDIR=`dirname $0`
cd ./repo
wget https://github.com/conda-forge/miniforge/releases/latest/download/Mambaforge-Linux-x86_64.sh
bash Mambaforge-Linux-x86_64.sh
mamba env create -f environment.yml
mamba activate torchmd-net
source ~/.bashrc
pip install -e .
CUDA_VISIBLE_DEVICES=1 python scripts/train.py --conf examples/ET-QM9.yaml $@
return $?

