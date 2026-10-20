"""Additive transport hooks into existing dynamic ownership and acceptance."""
from dataclasses import dataclass
from types import SimpleNamespace
from . import codec, protocol
from .preview import attach
from .worker import worker_main


@dataclass
class Profile:
    submission: object = None
    codec = codec
    wire_version = 3
    worker_target = staticmethod(worker_main)
    attach = staticmethod(attach)

    def connect(self, config, agent, run_id, epoch):
        return protocol.connect(config, agent, run_id, epoch, self.submission.definition)

    @staticmethod
    def hello(channel, args):
        return protocol.accept_hello(channel, args)

    @staticmethod
    def verify_resource(scene, body, args):
        from drt_backend.lan.protocol import exact, require
        from .service import provisioned
        exact(body, 'file_sha256 job')
        job = codec.read_job(body['job']); w, _ = codec.workload_from_job(job)
        hello = args.pbr_hello
        require(job.job_identity == hello['job_identity'] and str(job.run_id) == hello['run_id']
                and body['file_sha256'] == w.scene_sha256, 'PBR negotiated job/resource differs.')
        codec.match_capability(hello['capability'], w)
        scene = provisioned(scene, args.resource_root)
        adapter = codec.describe(scene, job)
        return SimpleNamespace(definition=job, adapter=adapter)
