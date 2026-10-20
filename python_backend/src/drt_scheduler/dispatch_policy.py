"""Workload-independent, credit-bounded adaptive selection; no numerical imports."""

class AdaptiveDispatch:
    def __init__(self):
        self.stats={};self.sequence=0

    def _stats(self,node):
        return self.stats.setdefault(node,dict(latency=None,throughput=None,observations=0,
            accepted_units=0,assignments=0,last=-1))

    def choose(self,candidates,units=1,retry=False):
        """Candidates are (node id, admitted slots, busy slots, idle Worker ids)."""
        if not candidates:return None
        self.sequence+=1
        def rank(c):
            node,capacity,busy,_=c;s=self._stats(node)
            # A ready node is sampled immediately, then cannot starve for more
            # than 16 dispatch selections in which it has an actual free slot.
            overdue=s['last']<0 or self.sequence-s['last']>=16
            latency=.2 if s['latency'] is None else s['latency']
            service=latency if s['throughput'] is None or not s['accepted_units'] else units/s['throughput']
            # Retry pressure favors available credits; accepted throughput may
            # refine prediction only after the authoritative owner reports commits.
            cost=(busy+1+(.5*busy if retry else 0))*(latency+service)/2/capacity
            return (not overdue,cost,s['last'],node)
        selected=min(candidates,key=rank)
        s=self._stats(selected[0]);s['last']=self.sequence;s['assignments']+=1
        return selected[0],min(selected[3])

    def observe(self,node,latency,units):
        s=self._stats(node);latency=max(1e-9,latency)
        s['latency']=latency if s['latency'] is None else .25*latency+.75*s['latency']
        throughput=units/latency
        s['throughput']=throughput if s['throughput'] is None else .25*throughput+.75*s['throughput']
        s['observations']+=1

    def accepted(self,node,units):
        self._stats(node)['accepted_units']+=units
