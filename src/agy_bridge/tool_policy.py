"""Explicit native tool-selection policy, separate from tool declarations.

Strict preserves the caller constraint. Optional is an administrator-selected
compatibility policy: forced choices become AUTO, with every tool retained.
NONE always remains NONE. Gproxy receives the original request unchanged.
"""
from dataclasses import dataclass, replace


def choice_kind(body):
    choice = body.get('tool_choice', 'auto')
    return 'named' if isinstance(choice, dict) else choice or 'auto'


@dataclass(frozen=True)
class ToolDecision:
    request: object
    policy: str
    requested: str
    effective: str

    @property
    def relaxed(self):
        return self.requested != self.effective

    def headers(self):
        return {'X-AGY-Tool-Choice-Policy': self.policy,
                'X-AGY-Tool-Choice-Requested': self.requested,
                'X-AGY-Tool-Choice-Effective': self.effective}

    def audit(self):
        return {'tool_choice_policy': self.policy, 'tool_choice_requested': self.requested,
                'tool_choice_effective': self.effective, 'tool_choice_relaxed': self.relaxed}


class NativeToolPolicy:
    def __init__(self, mode='strict'):
        if mode not in ('strict', 'optional'):
            raise ValueError('native_tool_choice_policy must be strict or optional')
        self.mode = mode

    def apply(self, request):
        requested = choice_kind(request.body)
        if self.mode == 'optional' and requested in ('required', 'named'):
            effective = replace(request, body=dict(request.body, tool_choice='auto'))
        else:
            effective = request
        return ToolDecision(effective, self.mode, requested, choice_kind(effective.body))
