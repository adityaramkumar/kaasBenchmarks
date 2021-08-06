#!/bin/bash
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd $SCRIPT_DIR

set +e
git submodule status | grep "^-" > /dev/null
subsInitialized=$?
if [ $subsInitialized == 0 ]; then
    git submodule update --init --recursive
fi
set -e

pushd fakefaas/python
    python setup.py develop
popd

# pushd mlperf/loadgen
#     pip install absl-py numpy
#     CFLAGS="-std=c++14 -O3" python setup.py bdist_wheel
#     pip install --force-reinstall dist/mlperf_loadgen-1.1-cp38-cp38-linux_x86_64.whl
# popd

pushd ../inference/
    pip install -r requirements.txt

    pushd python
        python setup.py develop
    popd

    pushd tools
        ./getData.py
        ./getModels.py
    popd
popd
