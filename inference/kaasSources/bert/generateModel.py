#!/usr/bin/env python3
import yaml
import pathlib
import pickle
import json
from bert import createReq
import argparse
import subprocess as sp

cwd = pathlib.Path(__file__).parent.resolve()
modelDir = cwd / ".." / ".." / "models"
bertDir = modelDir / "bert"


def loadGraph():
    graph = open(bertDir / "bert_graph.json")
    return json.load(graph)


# This method is useful because the intermediate buffers in multi-kernel nodes
# aren't present in the graph, so this code is needed in 2 separate locations.
def getInfo(buf, graph):
    name = buf.name
    if "_" in name:
        name = name[:name.find("_")]
    index = int(name)
    dtype = graph["attrs"]["dltype"][1][index]
    shape = graph["attrs"]["shape"][1][index]
    return dtype, shape


def loadParams():
    path = bertDir / "bert_params.pkl"
    params = pickle.load(open(path, 'rb'))
    return {'p' + str(i): params[i] for i in range(len(params))}, params


def metaFromReq(req, graph):
    constants = []
    inputs = []
    outputs = []
    constMap = dict()
    inputMap = dict()
    for kern in req.kernels:
        for buf in kern.inputs:
            if not buf.ephemeral:
                dtype, shape = getInfo(buf, graph)
                if buf.const:
                    constMap[int(buf.name)] = buf
                else:
                    inputMap[int(buf.name)] = buf
        for buf in kern.outputs:
            if not buf.ephemeral:
                dtype, shape = getInfo(buf, graph)
                outputs.append({"name": buf.name, "type": dtype, "shape": shape})
    constant_list = list(constMap.keys())
    constant_list.sort()
    print(len(constant_list))
    for i in constant_list:
        buf = constMap[i]
        dtype, shape = getInfo(buf, graph)
        constants.append({"name": buf.name, "type": dtype, "shape": shape})
    input_list = list(inputMap.keys())
    input_list.sort()
    for i in input_list:
        buf = inputMap[i]
        dtype, shape = getInfo(buf, graph)
        inputs.append({"name": buf.name, "type": dtype, "shape": shape})
    return {"constants": constants, "inputs": inputs, "outputs": outputs}


def getParams():
    params = loadParams()
    params_list = []
    for i in range(len(params.keys())):
        params_list.append(params["p" + str(i)])
    return params, params_list


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', '--output', type=pathlib.Path, default=bertDir, help="Output Directory")
    parser.add_argument('-n', '--name', default='bert', help="Name to use for output")

    args = parser.parse_args()
    targetDir = args.output
    if not targetDir.exists():
        targetDir.mkdir()

    params_dict, params_list = loadParams()

    graph = loadGraph()

    sp.run(['make'], cwd=cwd, check=True)

    req = createReq(params_dict, bertDir / (args.name + ".cubin"))
    with open(targetDir / (args.name + "_model.yaml"), 'w') as f:
        yaml.safe_dump(req.toDict(), f)

    meta_data = metaFromReq(req, graph)
    with open(targetDir / (args.name + "_meta.yaml"), 'w') as f:
        yaml.safe_dump(meta_data, f)

    with open(targetDir / (args.name + "_params.pkl"), 'wb') as f:
        pickle.dump(params_list, f)
