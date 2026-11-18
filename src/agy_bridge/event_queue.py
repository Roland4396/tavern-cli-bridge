"""Bound native full-state snapshots by bytes as well as event count.
Slow readers backpressure the local CLI stream rather than retaining hundreds
of growing output snapshots. Nothing is dropped or coalesced.
"""
import collections
import json
import queue
import threading
import time

class EventQueue:
    def __init__(self, max_events=8, max_bytes=16*1024*1024):
        self.max_events,self.max_bytes=max_events,max_bytes
        self.cv=threading.Condition();self.items=collections.deque();self.bytes=0
    def put(self,item,block=True,timeout=None,size=None):
        size=len(json.dumps(item,ensure_ascii=False).encode()) if size is None else size
        if not 0<=size<=self.max_bytes:raise ValueError('Event exceeds queue byte budget')
        deadline=None if timeout is None else time.monotonic()+timeout
        with self.cv:
            while len(self.items)>=self.max_events or self.bytes+size>self.max_bytes:
                remaining=None if deadline is None else deadline-time.monotonic()
                if not block or (remaining is not None and remaining<=0):raise queue.Full
                self.cv.wait(remaining)
            self.items.append((item,size));self.bytes+=size;self.cv.notify_all()
    def get(self,block=True,timeout=None):
        deadline=None if timeout is None else time.monotonic()+timeout
        with self.cv:
            while not self.items:
                remaining=None if deadline is None else deadline-time.monotonic()
                if not block or (remaining is not None and remaining<=0):raise queue.Empty
                self.cv.wait(remaining)
            item,size=self.items.popleft();self.bytes-=size;self.cv.notify_all();return item
    def status(self):
        with self.cv:return {'events':len(self.items),'bytes':self.bytes,'max_events':self.max_events,'max_bytes':self.max_bytes}
