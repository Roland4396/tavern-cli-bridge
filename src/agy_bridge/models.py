"""Public aliases and exact native model tiers."""
MODELS = {'gemini-4-argon': 'gemini-4-argon-medium',
          'gemini-4-argon-medium': 'gemini-4-argon-medium',
          'gemini-4-argon-high': 'gemini-4-argon-high',
          'gemini-4-hydrogen': 'gemini-4-hydrogen-xhigh',
          'gemini-4-hydrogen-xhigh': 'gemini-4-hydrogen-xhigh'}

COMPAT_MODELS = frozenset(MODELS.values())
CARBON = frozenset('gemini-4-carbon-' + tier for tier in ('low', 'medium', 'high'))
# Native upstream transport is independent of permission to use Gproxy.
NONSTREAM_MODELS = COMPAT_MODELS | CARBON
MODELS.update({model: model for model in sorted(CARBON)})
MODELS['gemini-4-carbon'] = 'gemini-4-carbon-medium'

NEON = frozenset('gemini-4-neon-' + tier for tier in ('low','medium','high','xhigh'))
CLAUDE55 = frozenset('claude-' + family + '-5-5-' + tier for family in ('opus','sonnet')
                    for tier in ('low','medium','high'))
EXPLICIT_NATIVE = NEON | CLAUDE55 | {'gemini-4-argon-low'}
MODELS.update({model: model for model in sorted(EXPLICIT_NATIVE)})

def guard_model(model):
    # Argon tiers share quota and its durable guards; changing effort must
    # not bypass an existing High-family cooldown or permission decision.
    if model in CARBON: return 'gemini-4-carbon-high'
    if model in NEON: return 'gemini-4-neon-xhigh'
    if model in CLAUDE55: return model.rsplit('-', 1)[0] + '-high'
    return 'gemini-4-argon-high' if model in ('gemini-4-argon-medium','gemini-4-argon-low') else model

