"""Explicit room registration under the existing owned PBR worker lifecycle."""
from . import codec
from drt_backend.pbr_execution.worker import worker_main as run_owned_worker


def worker_main(raw,worker_id,connection,cancellation):
    return run_owned_worker(raw,worker_id,connection,cancellation,_codec=codec)
