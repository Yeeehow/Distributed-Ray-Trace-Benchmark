"""Larger images use measured room resources and bounded per-task watchdogs."""
from dataclasses import asdict,dataclass
from drt_backend.application.worker_policy import discover_capacity,select_workers
from drt_backend.local_parallel.dispatcher import ParallelOptions
from drt_scheduler.attempts import AttemptPolicy
from .policy import RAW

LOCAL_WORKER_LIMIT=64
DISTRIBUTED_WORKER_LIMIT=256
MAX_AGENTS=16
LOCAL_NODE_ID='drt-local'


@dataclass(frozen=True)
class RoomOptions(ParallelOptions):
    workers:int|None=None
    run_seconds:float=10800
    startup_seconds:float=30
    task_seconds:float=30
    shutdown_seconds:float=5
    capacity_limit:int=LOCAL_WORKER_LIMIT

    def __post_init__(self):
        if self.workers is None:object.__setattr__(self,'workers',select(16384)['workers'])
        if self.capacity_limit!=LOCAL_WORKER_LIMIT:raise ValueError('Room Local safety limit is 64 Workers.')
        if type(self.run_seconds) not in (int,float) or not 1<=self.run_seconds<=10800:raise ValueError('Room image deadline.')
        checked=ParallelOptions(self.workers,self.max_in_flight,self.startup_seconds,self.task_seconds,
                                self.shutdown_seconds,min(75,self.run_seconds),self.capacity_limit)
        object.__setattr__(self,'max_in_flight',checked.max_in_flight)


@dataclass(frozen=True)
class RoomAttemptPolicy(AttemptPolicy):
    max_attempts:int=1
    max_active:int=DISTRIBUTED_WORKER_LIMIT
    max_per_node:int=LOCAL_WORKER_LIMIT
    max_pending:int=2*DISTRIBUTED_WORKER_LIMIT
    max_total_retries:int=0
    lease_seconds:float=30
    run_seconds:float=10800

    def __post_init__(self):
        if type(self.run_seconds) not in (int,float) or not 1<=self.run_seconds<=10800:
            raise ValueError('Room Distributed image deadline.')
        AttemptPolicy(self.max_attempts,self.max_active,self.max_per_node,self.max_pending,
            self.max_total_retries,self.lease_seconds,min(75,self.run_seconds),self.backoff_seconds)
        if self.max_attempts!=1 or self.max_total_retries!=0:
            raise ValueError('Room allocation forbids additional execution attempts.')


def select(task_count,configured_cap=None,*,capacity=None):
    """Default to all process-available logical CPUs within task/memory/64 limits."""
    facts=discover_capacity() if capacity is None else capacity
    allocation=select_workers(RAW,task_count,configured_cap,capacity=facts)
    return dict(allocation,schema='drt.room_admission.v2',workers=allocation['selected_workers'],
                per_worker_reservation_bytes=512*1024**2,parent_headroom_bytes=1024**3,capacity=asdict(facts))


def admit(workers):
    allocation=select(16384,workers)
    if allocation['workers']!=workers:raise ValueError('Insufficient measured room CPU/memory headroom.')
    return allocation


def ready(message):
    memory=message['memory']
    if (memory.get('status')!='MEASURED' or memory.get('pid')!=message['pid']
            or type(memory.get('peak_working_set_bytes')) is not int
            or not 1<=memory['peak_working_set_bytes']<=512*1024**2):
        raise ValueError('Room Worker measured memory exceeds its admission profile.')
    if message['ready_ns']-message['initialization_started_ns']>30_000_000_000:raise ValueError('Room preparation timeout.')
