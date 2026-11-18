"""Only the model-to-backend preference is hot. No account selection or transport."""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import stat
from .models import COMPAT_MODELS
from .core import BridgeError

FIELD = 'gproxy_only_models'
EXACT = COMPAT_MODELS


@dataclass(frozen=True)
class BackendPolicy:
    pins: frozenset
    revision: str

    @classmethod
    def parse(cls, pins):
        if not isinstance(pins, list) or any(not isinstance(v, str) or v not in EXACT for v in pins):
            raise ValueError('Gproxy-only policy requires exact configured model names')
        unique = frozenset(pins)
        raw = json.dumps(sorted(unique), separators=(',', ':')).encode()
        return cls(unique, sha256(raw).hexdigest()[:20])

    def mode(self, model):
        return 'gproxy_only' if model in self.pins else 'cli_first'


class FilePolicy:
    """Read one complete version via a file descriptor, not a watcher or timer.
    Operators publish the existing bridge.json using atomic rename. Invalid
    policy rejects new work; it never falls back to a less restrictive route.
    All other bridge settings keep their original startup-only lifetime.
    """
    def __init__(self, path, *, field_required=True):
        self.path, self.field_required = path, field_required

    def __call__(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
                    raise ValueError('private regular file required')
                raw = f.read(262145)
                if len(raw) > 262144:
                    raise ValueError('oversize configuration')
                doc = json.loads(raw)
                if not isinstance(doc, dict) or (self.field_required and FIELD not in doc):
                    raise ValueError('missing route policy')
                return BackendPolicy.parse(doc.get(FIELD, []))
        except (ValueError, OSError, RecursionError) as exc:
            raise BridgeError(503, 'route_policy_unavailable',
                              'Backend routing configuration is unavailable; request not submitted') from exc
