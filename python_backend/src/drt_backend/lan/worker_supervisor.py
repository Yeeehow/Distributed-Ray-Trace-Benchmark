"""Independent child watchdog for abrupt Agent death; never a graceful-stop claim."""
from dataclasses import dataclass
import multiprocessing
import os
from threading import Thread,Event
from time import monotonic

@dataclass(frozen=True)
class SupervisedWorker:
    target:object
    lifetime_seconds:int=85

    def __post_init__(self):
        if not callable(self.target):raise ValueError('Explicit supervised PBR target required.')
        if type(self.lifetime_seconds) is not int or not 1<=self.lifetime_seconds<=10840:
            raise ValueError('Invalid independent Worker lifetime.')

    def __call__(self,*args):
        parent=multiprocessing.parent_process() # Stable inherited parent sentinel, not a reused PID lookup.
        done=Event();deadline=monotonic()+self.lifetime_seconds
        def watch():
            while not done.wait(.1):
                if parent is not None and not parent.is_alive():os._exit(71)
                if monotonic()>=deadline:os._exit(72)
        monitor=Thread(target=watch,name='agent-parent-watchdog',daemon=True);monitor.start()
        try:return self.target(*args)
        finally:done.set();monitor.join(timeout=.2)
