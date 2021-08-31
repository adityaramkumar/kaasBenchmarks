import ray
import ray.util.queue
import infbench
import threading
import os
import pickle
import signal
import pathlib
import json
import random

from tornado.ioloop import IOLoop
import zmq
from zmq.eventloop.zmqstream import ZMQStream

import mlperf_loadgen
import libff.kaas.kaasRay as kaasRay

import util

# There is a bug in ray where if actors ever go out of scope, any reference
# held elsewhere can break. We hack around that issue by preventing actors from
# ever leaving scope with this global.
permanentScope = []

# All steps (pre/run/post) take in multiple arguments (even if there's one
# argument, it's passed as a tuple). If we passed a list of futures, we would
# need to ray.get() each one seperately. This would prevent Ray from doing full
# lazy evaluation, it would instantiate a million functions each waiting on
# ray.get(), wasting a ton of resources and eventually crashing. Instead, we
# pass each input directly as an argument using the *batch syntax (this groups
# remaining function arguments into a list). This way Ray waits until all
# inputs are ready before instantiating the function.


def _unMarshalArgs(argMap, args):
    """Due to Ray's requirement that all references be passed as arguments, we
    are forced to marshal all variable-length arguments into a single list.
    This unMarshals it back into constants and batched inputs."""
    if argMap.const is None:
        return ([], list(args))
    else:
        nConst = len(argMap.const)
        constants = list(args[:nConst])
        inputs = list(args[nConst:])
        return (constants, inputs)


def maybeDereference(res):
    # KaaS will place the result directly into the object store and return a
    # reference to it. The router also wraps outputs of run. Other inputs (e.g.
    # from pre()) will already be dereferenced by ray.

    if not isinstance(res, list):
        if isinstance(res, ray._raylet.ObjectRef):
            return ray.get(res)
        else:
            return res
    else:
        for i in range(len(res)):
            if isinstance(res[i], ray._raylet.ObjectRef):
                res[i] = ray.get(res[i])

        return res


def mergePerClientStats(base, delta):
    for cID, deltaClient in delta.items():
        if cID in base:
            base[cID].merge(deltaClient)
        else:
            base[cID] = deltaClient


@ray.remote
def pre(modelSpec, *inputs):
    mClass = modelSpec.modelClass
    constants, data = _unMarshalArgs(mClass.preMap, inputs)

    res = mClass.pre(constants + list(data))
    if len(res) == 1:
        return res[0]
    else:
        return res


# Not sure how to have a truly per-worker cache, but this dict maps PID to the initialized model (if any).
# From what I can tell, Ray will create a pool of processes for each unique
# task. Each task will get its own pool. Since these require a GPU, I would not
# expect the pool to exceed 2 and I would expect ray to kill workers when more
# than two unique tasks require a GPU.
# We assume that clients can only register one model.
# {pid -> {clientID -> model}}
modelCache = {}


def _run(model, inputs, completionQ, queryId, stats=None):
    """Internal run function"""
    constants, data = _unMarshalArgs(model.runMap, inputs)

    with infbench.timer('t_model_run', stats):
        results = model.run(constants + list(data), stats=stats)

    if completionQ is not None:
        completionQ.put((results, queryId))

    # Ray will interpret the return value as tuple if there are multiple
    # returns, but if there is one return, it will treat it as a scalar.
    if len(results) == 1:
        return results[0]
    else:
        return results


@ray.remote(num_gpus=1)
def runKaasTask(req, queryId=None, completionQ=None):
    results = kaasRay.kaasServeRay(req.toDict())

    if completionQ is not None:
        completionQ.put((results, queryId))

    return results


@ray.remote(num_gpus=1)
def runTask(modelSpec, modelArg, *inputs, completionQ=None, queryId=None, cacheModel=False, clientID=None):
    """Run the request as a Ray task"""
    if cacheModel:
        pid = os.getpid()
        if pid not in modelCache:
            modelCache[pid] = {}

        nodeCache = modelCache[pid]
        if clientID in nodeCache:
            model = nodeCache[clientID]
        else:
            model = modelSpec.modelClass(modelArg)
            nodeCache[clientID] = model
    else:
        model = modelSpec.modelClass(modelArg)

    return _run(model, inputs, completionQ, queryId)


@ray.remote
def post(modelSpec, *inputs, completionQ=None, queryId=None):
    mClass = modelSpec.modelClass
    constants, rawData = _unMarshalArgs(mClass.postMap, inputs)

    # The router actor wraps data in an additional reference
    data = maybeDereference(rawData)

    if modelSpec.modelType == 'kaas':
        data = maybeDereference(data)

    results = modelSpec.modelClass.post(constants + list(data))

    # In mlperf mode, we need to asynchronously report completions to a worker
    # through this queue. Otherwise we can return a ray future.
    if completionQ is not None:
        completionQ.put((results, queryId))

    if len(results) == 1:
        return results[0]
    else:
        return results


@ray.remote(num_gpus=1)
def runInline(modelSpec, modelArg, *refs, completionQ=None, queryId=None):
    """Run model with inlined pre and post-processing"""
    model = modelSpec.modelClass(modelArg)

    constants = refs[:model.nConst]
    inputs = refs[model.nConst:]

    preInp = util.packInputs(model.preMap, const=constants, inp=inputs)
    preOut = model.pre(preInp)

    runInp = util.packInputs(model.runMap, const=constants, inp=inputs, pre=preOut)
    modOut = model.run(runInp)

    if model.noPost:
        postOut = modOut
    else:
        postInp = util.packInputs(model.postMap, const=constants, inp=inputs,
                                  pre=preOut, run=modOut)
        postOut = model.post(postInp)

    if completionQ is not None:
        completionQ.put((postOut, queryId))

    return postOut


@ray.remote(num_gpus=1)
class runActor():
    """A persistent actor for running model requests. Actors will cache models
    as needed and run them natively. It is possible to run out of GPU memory
    with actors since they cache every model they are passed."""
    def __init__(self):
        self.modelCache = {}
        # {clientID -> infbench.profCollection}
        self.stats = {}

    def runNative(self, modelSpec, modelArg, *inputs, completionQ=None, queryId=None,
                  cacheModel=False, clientID=None):
        if clientID not in self.stats:
            self.stats[clientID] = infbench.profCollection()

        # The runActor must cache the model, if you wan't to reset, you must
        # kill and restart the actor. cacheModel is kept for consistency with
        # runTask but is ignored here.
        if clientID in self.modelCache:
            model = self.modelCache[clientID]
        else:
            model = modelSpec.modelClass(modelArg)
            self.modelCache[clientID] = model

        result = _run(model, inputs, completionQ, queryId, stats=self.stats[clientID])
        return result

    def runKaas(self, req, queryId=None, completionQ=None, clientID=None):
        if clientID not in self.stats:
            self.stats[clientID] = infbench.profCollection()

        with infbench.timer('t_model_run', self.stats[clientID]):
            results = kaasRay.kaasServeRay(req, stats=self.stats[clientID].mod('kaas'))

        if completionQ is not None:
            completionQ.put((results, queryId))

        return results

    def terminate(self):
        ray.actor.exit_actor()

    def getStats(self):
        """Returns any stats collected so far and resets them internally"""
        stats = self.stats
        self.stats = {}
        return stats


class Policy():
    def __init__(self, nRunner):
        pass

    def getRunner(self, clientID, *args):
        """Return the next actor to send a request to"""
        pass

    def update(self, *args):
        """Update the policy with any additional metadata from the last runner used"""
        pass


class PolicyRR(Policy):
    """A simple round-robin policy with no affinity"""
    def __init__(self, nRunner):
        self.lock = threading.Lock()
        self.last = 0
        self.actors = []
        for i in range(nRunner):
            newActor = runActor.remote()
            permanentScope.append(newActor)
            self.actors.append(newActor)

    def getRunner(self, clientID, *args):
        with self.lock:
            self.last = (self.last + 1) % len(self.actors)
            actor = self.actors[self.last]

        return actor

    def update(self, *args):
        pass


class actorStatus():
    RESERVED = 0
    IDLE = 1
    BUSY = 2

    def __init__(self):
        self.state = actorStatus.IDLE
        self.ref = None


class statusList():
    def __init__(self):
        self.statuses = []
        self.nReserved = 0
        self.lock = threading.Lock()
        self.reservedCv = threading.Condition(lock=self.lock)

    def updateState(self, status, newState):
        assert self.lock.locked()
        if status.state == actorStatus.RESERVED:
            self.nReserved -= 1
            status.state = newState
        elif newState == actorStatus.RESERVED:
            self.nReserved += 1
            status.state = newState
        else:
            status.state = newState


def pickActorBalanced(slist, timeout=None):
    """Given a list of actor statuses, return the first idle actor. Statuses
    are either ray references for busy actors, or None for idle. If timeout is
    provided, None may be returned if there are no free actors within
    timeout."""

    while True:
        with slist.reservedCv:
            if len(slist.statuses) == 0:
                return None

            while slist.nReserved == len(slist.statuses):
                slist.reservedCv.wait()

            outstanding = []
            for i, status in enumerate(slist.statuses):
                if status.state == actorStatus.IDLE:
                    # Found an idle worker
                    slist.updateState(status, actorStatus.RESERVED)
                    return i
                else:
                    if status.state == actorStatus.BUSY:
                        outstanding.append(status.ref)

            assert len(outstanding) != 0

        # Block until at least one actor is idle
        done, notReady = ray.wait(outstanding, fetch_local=False, timeout=timeout)

        with slist.reservedCv:
            if len(done) == 0:
                # There aren't any free workers within the timeout.  This could
                # theoretically be stale, but it probably isn't and we'll let
                # the policy decide if it's worth trying again
                return None
            else:
                idleRunner = None
                for ref in done:
                    for i, status in enumerate(slist.statuses):
                        if status.state == actorStatus.IDLE:
                            # Someone may have processed the actor while we
                            # waited on the lock
                            idleRunner = i
                        elif status.ref == ref:
                            assert status.state == actorStatus.BUSY
                            slist.updateState(status, actorStatus.IDLE)
                            status.ref = None
                            idleRunner = i

                if idleRunner is None:
                    # Our done list is stale, try again
                    continue

                slist.updateState(slist.statuses[idleRunner], actorStatus.RESERVED)
                return idleRunner

    return idleRunner


class PolicyBalance(Policy):
    """Routes requests to actors with potentially multiple clients per
    actor. It will attempt to balance load across the actors based on
    estimated outstanding work."""
    def __init__(self, nRunner):
        # List of Ray references representing stats from dead actors
        self.pendingActorStats = []

        self.actors = []
        for i in range(nRunner):
            newActor = runActor.remote()
            permanentScope.append(newActor)
            self.actors.append(newActor)

        # List of futures to the first return value of the runner. We assume
        # that if any returns are ready, then the runner is done. If None, then
        # the runner is idle.
        self.sList = statusList()
        self.sList.statuses = [actorStatus() for i in range(nRunner)]

    def scaleUp(self):
        """Add a worker to this policy"""
        with self.sList.reservedCv:
            self.sList.statuses.append(actorStatus())
            newActor = runActor.remote()
            permanentScope.append(newActor)
            self.actors.append(newActor)

    def scaleDown(self):
        """Remove a worker from this policy"""
        with self.sList.reservedCv:
            self.sList.statuses.pop()
            toKill = self.actors.pop()

            self.pendingActorStats.append(toKill.getStats.remote())
            toKill.terminate.remote()

    def getRunner(self, clientID, **kwargs):
        """Returns an actor suitable for running a request and an opaque handle
        that must be passed to update() along with the clientID and
        respFutures"""
        timeout = kwargs.get('timeout', None)
        idx = pickActorBalanced(self.sList, timeout=timeout)
        if idx is None:
            return None, None
        else:
            with self.sList.reservedCv:
                actor = self.actors[idx]
                status = self.sList.statuses[idx]
            return actor, status

    def update(self, clientID, handle, respFutures):
        status = handle
        if isinstance(respFutures, list):
            status.ref = respFutures[0]
        else:
            status.ref = respFutures
        with self.sList.reservedCv:
            self.sList.updateState(handle, actorStatus.BUSY)
            self.sList.reservedCv.notify()

    def getStats(self):
        """Return a map of clientIDs to profCollection. Resets stats."""
        stats = {}

        for actor in self.actors:
            self.pendingActorStats.append(actor.getStats.remote())

        actorStats = ray.get(self.pendingActorStats)
        for actorStat in actorStats:
            mergePerClientStats(stats, actorStat)

        self.pendingActorStats = []
        return stats


class PolicyExclusive(Policy):
    def __init__(self, nRunner):
        self.maxRunners = nRunner
        self.nRunners = 0

        self.lock = threading.Lock()

        # {clientID -> PolicyBalance()}
        self.clientPools = {}

    def _makeRoom(self, clientID):
        while True:
            with self.lock:
                clientPool = self.clientPools[clientID]
                clientLength = len(clientPool.actors)

                if self.nRunners < self.maxRunners:
                    clientPool.scaleUp()
                    self.nRunners += 1
                    # This is guaranteed to return without blocking since there's a new
                    # idle worker
                    return clientPool.getRunner(clientID)

                # Pick a candidate for eviction. This will be the client with the most
                # actors (ties are broken randomly).
                maxLength = 0
                for cID, pool in self.clientPools.items():
                    if len(pool.actors) > maxLength:
                        maxLength = len(pool.actors)

                candidates = []
                for cID, pool in self.clientPools.items():
                    if len(pool.actors) == maxLength:
                        candidates.append(cID)

                if clientLength < maxLength:
                    # Gotta be somewhat fair. Real fairness is a problem for
                    # another day
                    lot = random.randrange(0, len(candidates))
                    candidate = candidates[lot]
                    # candidate = candidates[0]
                    # print(f"EVICTING {candidate} for {clientID} ({lot} from choices {candidates})")
                    victimPool = self.clientPools[candidate]
                    victimPool.scaleDown()

                    clientPool.scaleUp()
                    # Won't block because we just scalued up clientPool
                    return clientPool.getRunner(clientID)

            # Wouldn't be fair to kill anyone, just block until something frees
            # up. Warning, this may block.
            runner = clientPool.getRunner(clientID)
            if runner[0] is None:
                # Something went wrong. Probably someone scaled our pool down to
                # zero while we were waiting. Try again.
                continue
            else:
                return runner

    def getRunner(self, clientID, **kwargs):
        with self.lock:
            if clientID in self.clientPools:
                clientPool = self.clientPools[clientID]
            else:
                clientPool = PolicyBalance(0)
                self.clientPools[clientID] = clientPool

        runner, handle = clientPool.getRunner(clientID, timeout=0.01)

        if runner is not None:
            return runner, handle
        else:
            runner = self._makeRoom(clientID)
            if runner[0] is None:
                raise RuntimeError("Couldn't find runner")
            return runner

    def update(self, clientID, handle, respFutures):
        with self.lock:
            self.clientPools[clientID].update(clientID, handle, respFutures)

    def getStats(self):
        stats = {}
        for pool in self.clientPools.values():
            poolStats = pool.getStats()
            mergePerClientStats(stats, poolStats)

        return stats


@ray.remote
class runnerPool():
    def __init__(self, nRunner, benchConfig):
        """RunnerPool is responsible for launching run requests.
                - nRunner: If using actors, number of actors to allocate
                - policy: Scheduling policy (when using actors)
                - mode: ['actors', 'kaas', 'task']. Actors and KaaS will run in
                actors while 'task' will use ray tasks instead.
        """
        self.maxRunners = nRunner
        self.mode = benchConfig['runner_mode']
        benchConfig['runner_policy']

        if self.mode not in ['task', 'actor', 'kaas']:
            raise ValueError("Unrecognized mode: " + self.mode)

        if self.mode != 'task':
            if benchConfig['runner_policy'] == 'rr':
                self.policy = PolicyRR(nRunner)
            elif benchConfig['runner_policy'] == 'exclusive':
                self.policy = PolicyExclusive(nRunner)
            elif benchConfig['runner_policy'] == 'balance':
                self.policy = PolicyBalance(nRunner)
            else:
                raise ValueError("Unrecognized policy: " + benchConfig['runner_policy'])

    def getStats(self):
        return self.policy.getStats()

    def run(self, nReturn, clientID, inputRefs, args, kwargs={}):
        """Run a model. Args and kwargs will be passed to the appropriate runner"""
        if self.mode == 'task':
            respFutures = runTask.options(num_returns=nReturn).remote(*args, **kwargs)
        else:
            # Block until the inputs are ready
            ray.wait(inputRefs, num_returns=len(inputRefs), fetch_local=False)

            # Get a free runner (may block)
            runActor, handle = self.policy.getRunner(clientID)
            assert runActor is not None

            if self.mode == 'actor':
                respFutures = runActor.runNative.options(num_returns=nReturn).remote(*args, **kwargs)
            elif self.mode == 'kaas':
                respFutures = runActor.runKaas.options(num_returns=nReturn).remote(*args, **kwargs)
            else:
                raise RuntimeError("Unrecognized mode: ", self.mode)

            self.policy.update(clientID, handle, respFutures)

        # Wait until the runner is done before returning, this ensures that
        # anyone waiting on our response (e.g. post()) can immediately
        # ray.get the answer without blocking.
        if nReturn == 1:
            ray.wait([respFutures], num_returns=1, fetch_local=False)
        else:
            ray.wait(respFutures, num_returns=len(respFutures), fetch_local=False)
        return respFutures


def _runOne(modelSpec, specRef, modelArg, constRefs, inputRefs, inline=False,
            completionQ=None, queryId=None, cacheModel=False, clientID=None,
            runPool=None, stats=None):
    """Issue one query asynchronously to ray, returns a future. inline will run
       all data processing steps in the same function as the model."""
    mClass = modelSpec.modelClass

    if inline:
        assert not modelSpec.modelType == 'kaas', "KaaS is not compatible with inline"
        assert runPool is None, "Cannot use run actors in inline mode"

        # We can't pass lists of references to ray functions because ray can't
        # statically determine the dataflow. All refs have to be first-class
        # arguments so we pack them all into a list and then expand it with
        # *varArgs
        if constRefs is None:
            varArgs = list(inputRefs)
        else:
            varArgs = list(constRefs) + list(inputRefs)

        if completionQ is not None:
            runInline.options(num_returns=mClass.nOutPost) \
                .remote(specRef, modelArg, *varArgs, completionQ=completionQ,
                        queryId=queryId)
            postOut = None
        else:
            postOut = runInline.options(num_returns=mClass.nOutPost) \
                .remote(specRef, modelArg, *varArgs)
    else:
        # Pre
        preInp = util.packInputs(mClass.preMap, const=constRefs, inp=inputRefs)

        preOut = pre.options(num_returns=mClass.nOutPre).remote(specRef, *preInp)
        if mClass.nOutPre == 1:
            preOut = [preOut]

        # Run
        runInp = util.packInputs(mClass.runMap, const=constRefs, inp=inputRefs, pre=preOut)

        if modelSpec.modelType == "kaas":
            model = modelArg
            req = model.run(runInp, stats=stats)

            if completionQ is not None and mClass.noPost:
                runOut = runPool.run.options(num_returns=mClass.nOutRun). \
                    remote(mClass.nOutRun, clientID, runInp, [req], {"queryId": queryId, "completionQ": completionQ, "clientID": clientID})
            else:
                runOut = runPool.run.options(num_returns=mClass.nOutRun).remote(mClass.nOutRun, clientID, runInp, [req], {"clientID": clientID})
        else:
            if completionQ is not None and mClass.noPost:
                runOut = runPool.run.options(num_returns=mClass.nOutRun). \
                    remote(mClass.nOutRun, clientID, runInp,
                           [specRef, modelArg] + runInp,
                           {"completionQ": completionQ, "queryId": queryId,
                            "cacheModel": cacheModel, "clientID": clientID})

            else:
                runOut = runPool.run.options(num_returns=mClass.nOutRun). \
                    remote(mClass.nOutRun, clientID, runInp,
                           [specRef, modelArg] + runInp, {"cacheModel": cacheModel})

        if mClass.nOutRun == 1:
            runOut = [runOut]

        # Post
        if mClass.noPost:
            postOut = runOut
        else:
            postInp = util.packInputs(mClass.postMap, const=constRefs,
                                      inp=inputRefs, pre=preOut, run=runOut)
            postOut = post.options(num_returns=mClass.nOutPost) \
                .remote(specRef, *postInp, completionQ=completionQ, queryId=queryId)

            if mClass.nOutPost == 1:
                postOut = [postOut]
    return postOut


def _nShotAsync(n, loader, modelSpec, specRef, modelArg, constRefs, pool, benchConfig, stats):
    refs = []
    for i in range(n):
        idx = i % loader.ndata
        inp = loader.get(idx)

        # Ray is lazy and asynchronous so it's difficult to collect more
        # detailed metrics than e2e. Details within the remote functions
        # should match localBench results anyway.
        refs.append(_runOne(modelSpec, specRef, modelArg, constRefs, inp,
                    inline=benchConfig['inline'], runPool=pool,
                    cacheModel=benchConfig['cache'], stats=stats))

    # This isn't super accurate, but _runOne should return instantly and the
    # real work only happens when ray.get is called
    with infbench.timer('t_e2e', stats):
        results = []
        for i, ref in enumerate(refs):
            idx = i % loader.ndata

            # Dereference answer from post or the router's reference from run
            # (if nopost)
            res = ray.get(ref)
            if modelSpec.modelClass.noPost:
                # Dereference the answer from run itself
                res = ray.get(res)

                if modelSpec.modelType == 'kaas':
                    res = maybeDereference(res)

            results.append((idx, res))

    return results


def _nShotSync(n, loader, modelSpec, specRef, modelArg, constRefs, pool, benchConfig, stats):
    results = []
    for i in range(n):
        idx = i % loader.ndata
        inp = loader.get(idx)

        with infbench.timer('t_e2e', stats):
            # Ray is lazy and asynchronous so it's difficult to collect more
            # detailed metrics than e2e. Details within the remote functions
            # should match localBench results anyway.
            res = _runOne(modelSpec, specRef, modelArg, constRefs, inp,
                          inline=benchConfig['inline'], runPool=pool,
                          cacheModel=benchConfig['cache'], stats=stats)

            # Dereference answer from post or the router's reference from run
            # (if nopost)
            res = ray.get(res)
            if modelSpec.modelClass.noPost:
                # Dereference the answer from run itself
                res = ray.get(res)

                if modelSpec.modelType == 'kaas':
                    res = maybeDereference(res)

        results.append((idx, res))

    return results


def nShot(modelSpec, n, benchConfig, reportPath="results.json"):
    ray.init()

    coldStats = infbench.profCollection()
    warmStats = infbench.profCollection()

    specRef = ray.put(modelSpec)

    with infbench.timer("t_registerModel", warmStats):
        if modelSpec.modelType == "kaas":
            modelArg = modelSpec.getModelArg()
        else:
            modelArg = ray.put(modelSpec.getModelArg())

        constants = modelSpec.modelClass.getConstants(modelSpec.modelPath.parent)

        if constants is None:
            constRefs = None
        else:
            constRefs = []
            for const in constants:
                constRefs.append(ray.put(const))

    with infbench.timer("t_initLoader", warmStats):
        loader = modelSpec.loader(modelSpec.dataDir)
        loader.preLoad(range(min(max(n, util.getNGpu()*2), loader.ndata)))

    pool = runnerPool.options(max_concurrency=10).remote(util.getNGpu(), benchConfig)

    # Cold Start, done async to maximize the chances of everything getting warm
    # when there are multiple GPUs
    print(f"Running {2*util.getNGpu()} warmup passes")
    results = _nShotAsync(util.getNGpu()*2, loader, modelSpec, specRef, modelArg, constRefs, pool, benchConfig, coldStats)
    # getting stats resets them for the warm runs
    ray.get(pool.getStats.remote())

    # Warm Runs
    print("Beginning warm runs")
    # results = _nShotAsync(n, loader, modelSpec, specRef, modelArg, constRefs, pool, benchConfig, stats)
    results = _nShotSync(n, loader, modelSpec, specRef, modelArg, constRefs, pool, benchConfig, warmStats)
    warmPoolStats = ray.get(pool.getStats.remote())

    if loader.checkAvailable:
        accuracies = [loader.check(res, idx) for idx, res in results]
        print("Accuracy = ", sum([int(res) for res in accuracies]) / n)
    else:
        print("Accuracy checking not supported by this dataset")

    warmStats.merge(warmPoolStats[None])

    print("\nDetailed Stats: ")
    report = warmStats.report()
    util.analyzeStats(report)
    # analyzeStats(warmPoolStats[None].report())

    if not isinstance(reportPath, pathlib.Path):
        reportPath = pathlib.Path(reportPath).resolve()

    print("Saving results to: ", reportPath)
    if reportPath.exists():
        with open(reportPath, 'r') as f:
            fullReport = json.load(f)
    else:
        fullReport = []

    record = {
        "config": benchConfig,
        "metrics": report
    }
    fullReport.append(record)

    with open(reportPath, 'w') as f:
        json.dump(fullReport, f)

    # print("Ray Profiling:")
    # ray.timeline(filename="rayProfile.json")

    return results


# =============================================================================
# MLPERF INFERENCE STUFF
# =============================================================================


def handleCompletion(modelSpec, queue):
    """Handle responses from mlperf model serving functions. Each response is a
    list of query IDs that are now complete. To signal completion, the user
    must push an integer to the queue representing the total number of
    responses to expect (including all those already completed).

    Note: For now we ignore return values, the client does not care about the
    result of the prediction in this benchmark."""

    # number of batches we've received.
    ncomplete = 0

    # Once set, this is the total number of responses we're expecting
    targetComplete = None
    wrapItUp = False
    while not wrapItUp or ncomplete != targetComplete:
        # One batch at a time
        resp, qid = queue.get()

        if isinstance(resp, int):
            # The driver is asking us to wrap up and exit after we've seen
            # 'resps' many responses.
            targetComplete = resp

            if ncomplete == targetComplete:
                break
            else:
                wrapItUp = True
        else:
            # Normal completion message from a worker

            # Technically we should do this to load the result to the handler.
            # Unfortunately this causes an error in ray that I can't figure out
            # so we skip it.
            # if modelSpec.modelType == 'kaas' and modelSpec.modelClass.noPost:
            #     resp = handleKaasResult(resp)

            completion = mlperf_loadgen.QuerySampleResponse(qid, 0, 0)
            mlperf_loadgen.QuerySamplesComplete([completion])
            ncomplete += 1


class mlperfRunner():
    def __init__(self, modelSpec, loader, constantRefs, benchConfig):
        self.modelSpec = modelSpec
        self.loader = loader
        self.constants = constantRefs
        self.benchConfig = benchConfig

        self.coldStats = infbench.profCollection()
        self.warmStats = infbench.profCollection()

        if modelSpec.modelType == "kaas":
            self.modelArg = modelSpec.getModelArg()
        else:
            self.modelArg = ray.put(modelSpec.getModelArg())

        # Total number of queries issued
        self.nIssued = 0

        self.specRef = ray.put(self.modelSpec)

        self.nGpu = util.getNGpu()

        self.pool = runnerPool.options(max_concurrency=2*self.nGpu).remote(self.nGpu, benchConfig)

    def start(self, preWarm=True):
        self.completionQueue = ray.util.queue.Queue()
        self.completionHandler = threading.Thread(
                target=handleCompletion, args=[self.modelSpec, self.completionQueue])
        self.completionHandler.start()

        # This is very important for Ray because the cold start is multiple
        # seconds and mlperf is based on SLOs which we violate immediately.
        if preWarm:
            self.loader.preLoad([0])
            inputs = self.loader.get(0)
            results = []

            # We don't control how ray handles workers, but we assume that
            # sending a burst of nGpu*2 should be enough to trigger all the
            # cold starts.
            for i in range(self.nGpu*2):
                results.append(_runOne(self.modelSpec, self.specRef, self.modelArg,
                               self.constants, inputs, inline=self.benchConfig['inline'],
                               runPool=self.pool, stats=self.coldStats))
            for res in results:
                ray.get(res)

            coldPoolStats = ray.get(self.pool.getStats.remote())
            self.coldStats.merge(coldPoolStats[None])

    def runBatch(self, queryBatch):
        for q in queryBatch:
            inp = self.loader.get(q.index)

            _runOne(self.modelSpec, self.specRef, self.modelArg,
                    self.constants, inp, inline=self.benchConfig['inline'],
                    completionQ=self.completionQueue, queryId=q.id,
                    cacheModel=self.benchConfig['cache'], runPool=self.pool, stats=self.warmStats)

        self.nIssued += len(queryBatch)

    def processLatencies(self, latencies):
        self.latMetrics = infbench.model.processLatencies(self.benchConfig, latencies)

    def stop(self):
        self.completionQueue.put((self.nIssued, None))
        print("Waiting for completion handler to finish")
        self.completionHandler.join()

        warmPoolStats = ray.get(self.pool.getStats.remote())
        self.warmStats.merge(warmPoolStats[None])

        return (self.coldStats, self.warmStats)


def mlperfBench(modelSpec, benchConfig):
    """Run the mlperf loadgen version"""
    ray.init()

    gpuType = util.getGpuType()
    settings = modelSpec.modelClass.getMlPerfCfg(gpuType, benchConfig)
    loader = modelSpec.loader(modelSpec.dataDir)

    constants = modelSpec.modelClass.getConstants(modelSpec.modelPath.parent)
    if constants is None:
        constRefs = None
    else:
        constRefs = []
        for const in constants:
            constRefs.append(ray.put(const))

    runner = mlperfRunner(modelSpec, loader, constRefs, benchConfig)

    runner.start(preWarm=True)
    sut = mlperf_loadgen.ConstructSUT(
        runner.runBatch, infbench.model.flushQueries, runner.processLatencies)

    qsl = mlperf_loadgen.ConstructQSL(
        loader.ndata, infbench.model.mlperfNquery, loader.preLoad, loader.unLoad)

    mlperf_loadgen.StartTest(sut, qsl, settings)
    mlperf_loadgen.DestroyQSL(qsl)
    mlperf_loadgen.DestroySUT(sut)

    coldStats, warmStats = runner.stop()

    print("\nResults:")
    mlPerfMetrics = infbench.model.parseMlPerf('mlperf_log_')

    print("\nStats:")
    report = warmStats.report()
    util.analyzeStats(report)

    infbench.model.saveReport({**runner.latMetrics, **mlPerfMetrics}, benchConfig, 'results.json')


# =============================================================================
# Server Mode
# =============================================================================


class clientState():
    def __init__(self, modelName):
        self.modelSpec = util.getModelSpec(modelName)
        self.specRef = ray.put(self.modelSpec)

        if self.modelSpec.modelType == "kaas":
            self.modelArg = self.modelSpec.getModelArg()
        else:
            self.modelArg = ray.put(self.modelSpec.getModelArg())

        constants = self.modelSpec.modelClass.getConstants(self.modelSpec.modelPath.parent)
        if constants is None:
            constRefs = None
        else:
            constRefs = []
            for const in constants:
                constRefs.append(ray.put(const))
        self.constRefs = constRefs


# { clientID -> clientState }
# For now, we only support one model per client
clients = {}


class serverLoop():
    """ServerTask"""
    def __init__(self, clientSock, barrierSock, benchConfig):
        self.loop = IOLoop.instance()
        self.benchConfig = benchConfig

        self.clientStream = ZMQStream(clientSock)
        self.clientStream.on_recv(self.handleClients)

        self.barrierStream = ZMQStream(barrierSock)
        self.barrierStream.on_recv(self.handleBarrier)
        self.readyClients = []

        IOLoop.current().add_callback(self.handleWorker)

        self.nGpu = util.getNGpu()

        self.clientStats = {}
        self.pool = runnerPool.options(max_concurrency=2*self.nGpu).remote(self.nGpu, benchConfig)

        self.rayQ = ray.util.queue.Queue()

    def handleBarrier(self, msg):
        clientID = msg[0]

        print("Recieved Ready from: ", clientID.decode("utf-8"))
        self.readyClients.append(clientID)
        if len(self.readyClients) == self.benchConfig['numClient']:
            # Get cold-start stats (if any) and reset for main warm passes
            poolStats = ray.get(self.pool.getStats.remote())
            self.coldStats = self.clientStats
            mergePerClientStats(self.coldStats, poolStats)
            self.clientStats = {}

            print("Releasing Barrier")
            for cID in self.readyClients:
                self.barrierStream.send_multipart([cID, b'', b'GO'])

    async def handleWorker(self):
        result, reqData = await self.rayQ.get_async()
        clientID = reqData[0]
        reqID = reqData[1]

        # Ideally, ray would handle this in their Queue implementation but they
        # can't recurse into datastructures so we have to fetch the result
        # here. It's guaranteed to be ready (since any references come from
        # KaaS which already put them into the kv store) but we do have to wait
        # for the data transfer.
        result = maybeDereference(result)

        self.clientStream.send_multipart([clientID, reqID, pickle.dumps(result)])
        IOLoop.current().add_callback(self.handleWorker)

    def handleClients(self, msg):
        clientID, reqID, data = msg

        cState = clients.get(clientID, None)

        if clientID not in self.clientStats:
            self.clientStats[clientID] = infbench.profCollection()

        if cState is None:
            # Registration
            print("Registering ", clientID)
            modelName = reqID.decode('utf-8')
            cState = clientState(modelName)
            clients[clientID] = cState
        else:
            # Normal Request
            # XXX ray.put is just going to re-pickle the data. We should really
            # require models to only pass bytes as inputs and outputs.
            data = pickle.loads(data)

            _runOne(cState.modelSpec, cState.specRef, cState.modelArg,
                    cState.constRefs, data, completionQ=self.rayQ,
                    queryId=(clientID, reqID), clientID=clientID,
                    cacheModel=self.benchConfig['cache'],
                    inline=self.benchConfig['inline'], runPool=self.pool,
                    stats=self.clientStats[clientID])

    def shutdown(self):
        self.clientStream.stop_on_recv()
        IOLoop.instance().stop()

        poolStats = ray.get(self.pool.getStats.remote())
        self.warmStats = self.clientStats
        mergePerClientStats(self.warmStats, poolStats)
        self.clientStats = {}


def serveRequests(benchConfig):
    ray.init()
    context = zmq.Context()

    clientSock = context.socket(zmq.ROUTER)
    clientSock.bind(util.clientUrl)

    barrierSock = context.socket(zmq.ROUTER)
    barrierSock.bind(util.barrierUrl)

    # IOLoop uses a global context, when you instantiate a serverLoop object,
    # it registers itself with IOLoop. The returned object isn't used here.
    looper = serverLoop(clientSock, barrierSock, benchConfig)

    signal.signal(signal.SIGINT, lambda s, f: IOLoop.instance().add_callback_from_signal(looper.shutdown))

    print("Beginning serving loop")
    IOLoop.instance().start()
    print("Server Exiting")

    print("Stats:")
    for cID, stats in looper.warmStats.items():
        print("Client: ", cID)
        util.analyzeStats(stats.report())
