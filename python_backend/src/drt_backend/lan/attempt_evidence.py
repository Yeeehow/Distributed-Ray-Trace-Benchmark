"""Bounded diagnostic effort, never acceptance authority or an image assembler."""
from collections import OrderedDict
from hashlib import sha256
import json
from drt_backend.local_parallel import codec

class AttemptEvidence:
    def __init__(self,*,_codec=None):
        require_codec=_codec
        if require_codec is None:raise ValueError('Explicit PBR effort codec required.')
        self.reports=OrderedDict();self.codec=require_codec

    def report(self,binding,packet,ray_counts,*,current):
        key=binding['attempt_id']
        digest=sha256(json.dumps(packet,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        prior=self.reports.get(key)
        if prior is not None:
            codec.require(prior['binding']==binding and prior['packet_sha256']==digest,'Repeated attempt reported contradictory execution.')
            return
        self.reports[key]=dict(binding=binding,packet_sha256=digest,executed_rays=ray_counts,
            status=packet['status'],awaiting_commit=current and packet['status']=='completed')
        while len(self.reports)>1024:
            removable=next((k for k,v in self.reports.items() if not v['awaiting_commit']),None)
            codec.require(removable is not None,'Diagnostic attempt result window exceeded.')
            del self.reports[removable]

    def committed(self,attempt):
        if attempt.attempt_id in self.reports:self.reports[attempt.attempt_id]['awaiting_commit']=False

    def summary(self,attempts,acceptance):
        codec=self.codec
        # At most 1,024 retired authorities + 1,024 latest canonical authorities.
        # Full logical coverage/rays still belong only to the original ledger.
        authorities=OrderedDict((b['attempt_id'],b) for b in attempts.retired)
        current=[r['attempt'] for r in attempts.records.values() if r['attempt'] is not None][-1024:]
        authorities.update((a.attempt_id,a.binding) for a in current)
        accepted={(e['task_id'],e['worker_id']):e['rays'] for e in acceptance.events if e['accepted']}
        rows=[]
        for key,b in authorities.items():
            record=attempts.records[b['task_id']]
            is_current=record['attempt'] is not None and record['attempt'].attempt_id==key
            credit=accepted.get((b['task_id'],f'{b["node"]}/e{b["epoch"]}/{b["worker_id"]}')) if is_current and record['state']=='Accepted' else None
            accepted_rays=credit or codec.ray_counts(0,0)
            report=self.reports.get(key);executed=None if report is None else report['executed_rays']
            if executed is None and credit is not None:executed=credit # Original accepted event is an actual received result.
            discarded=None if executed is None else codec.ray_counts(executed['primary']-accepted_rays['primary'],
                executed['shadow']-accepted_rays['shadow'],executed['secondary']-accepted_rays['secondary'])
            rows.append(dict(**b,state=record['state'] if is_current else 'Retired',
                executed_rays=executed,accepted_rays=accepted_rays,discarded_rays=discarded,
                executed_effort='reported' if executed is not None else 'unreported; session aggregate may be known, never inferred from dispatch'))
        return dict(detail_limit=2048,received_packet_limit=1024,retained_packets=len(self.reports),attempts=rows)
