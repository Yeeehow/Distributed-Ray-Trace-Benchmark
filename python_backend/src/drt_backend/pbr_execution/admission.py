"""Tiny PBR admission bounds; measured readiness precedes numerical dispatch."""
from dataclasses import asdict
from drt_backend.application.worker_policy import discover_capacity
from .codec import require, integer

MEMORY_RESERVATION = 128*1024*1024


def admit(workers):
    integer(workers, 1, 2)
    facts = discover_capacity()
    require(facts.available_cpu_capacity >= workers and facts.logical_processors >= workers,
            'Insufficient available CPU capacity for tiny PBR allocation.')
    require(facts.available_memory_bytes >= 256*1024*1024+workers*MEMORY_RESERVATION,
            'Insufficient available memory for bounded PBR allocation.')
    return dict(schema='drt.pbr_tiny_admission.v1', **asdict(facts), workers=workers,
                per_worker_reservation_bytes=MEMORY_RESERVATION,
                limitation='Only one or two tiny synthetic CPU Workers; no scale/performance claim.')


def ready(message):
    memory = message['memory']
    require(memory.get('status') == 'MEASURED' and memory.get('pid') == message['pid']
            and type(memory.get('peak_working_set_bytes')) is int
            and memory['peak_working_set_bytes'] <= MEMORY_RESERVATION,
            'PBR measured Worker memory exceeds the tiny resource profile.')
    require(message['ready_ns']-message['initialization_started_ns'] <= 15_000_000_000,
            'PBR measured preparation exceeds its bounded startup policy.')
