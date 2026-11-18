"""Cross-process two-slot account lease, compatible with exclusive native login/run.
Fresh credentials permit shared run leases; unknown/near-expiry credentials use an
exclusive run lease, keeping predictable OAuth refreshes serialized.
"""
import fcntl
import os
import time
from . import account_store as accounts
from .credentials import token_expiry

class Lease:
    def __init__(self,fds,mode): self.fds=tuple(fds);self.mode=mode;self.closed=False
    def close(self):
        if self.closed:return
        self.closed=True
        for fd in reversed(self.fds):os.close(fd)

class Leases:
    def __init__(self,store,per_account=2,min_token_lifetime=360):
        if per_account not in (1,2):raise ValueError('Account concurrency must be 1 or 2')
        self.store,self.per_account,self.min_token_lifetime=store,per_account,min_token_lifetime
    def parallel_capacity(self,alias):
        _,home,_=self.store._paths(alias)
        token=home/accounts.TOKEN_REL
        if token.is_symlink():raise accounts.AccountError('Unsafe credential path')
        try:expiry=token_expiry(token.read_bytes())
        except FileNotFoundError:expiry=None
        fresh=expiry is not None and expiry-time.time()>=self.min_token_lifetime
        return self.per_account if fresh else 1
    def __call__(self,alias):
        profile,home,_=self.store._paths(alias);fds=[]
        def acquire(path,mode):
            fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            try:fcntl.flock(fd,mode|fcntl.LOCK_NB)
            except BaseException:os.close(fd);raise
            return fd
        try:
            # Begin shared so native login cannot replace credentials during inspection.
            run=acquire(profile/'run.lock',fcntl.LOCK_SH);fds.append(run)
            token=home/accounts.TOKEN_REL
            if token.is_symlink():raise accounts.AccountError('Unsafe credential path')
            try:expiry=token_expiry(token.read_bytes())
            except FileNotFoundError:expiry=None
            fresh=expiry is not None and expiry-time.time()>=self.min_token_lifetime
            exclusive=self.per_account==1 or not fresh
            if exclusive:
                # Nonblocking upgrade. Competing shared/upgrading leases back off.
                fcntl.flock(run,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for i in range(self.per_account):
                try:slot=acquire(profile/f'api-slot-{i}.lock',fcntl.LOCK_EX)
                except BlockingIOError:continue
                fds.append(slot);return Lease(fds,'exclusive_refresh' if exclusive else 'shared_fresh')
            return None
        except BlockingIOError:return None
        finally:
            # A returned Lease has one run FD plus one slot FD.
            if len(fds)<2:
                for fd in reversed(fds):os.close(fd)
    def is_busy(self,alias):
        lease=self(alias)
        if lease is None:return True
        lease.close();return False
