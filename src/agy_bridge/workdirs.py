"""Account-local runtime namespace; only the alias-to-directory index persists."""
import json
import re
import uuid
from . import account_store as accounts


def account_parent(runtime, store_root, alias):
    if not isinstance(alias, str) or not accounts.ALIAS.fullmatch(alias):
        raise accounts.AccountError('Invalid workspace account')
    index = store_root / 'workspace-index'
    accounts.private_dir(index)
    path = index / 'names.json'
    with accounts.lock(index / 'names.lock'):
        if path.is_symlink():
            raise accounts.AccountError('Unsafe workspace directory index')
        names = json.loads(path.read_bytes()) if path.exists() else {}
        if (not isinstance(names, dict)
                or not all(accounts.ALIAS.fullmatch(k) and isinstance(v, str)
                           and re.fullmatch(r'[0-9a-f]{32}', v) for k, v in names.items())
                or len(names) != len(set(names.values()))):
            raise accounts.AccountError('Invalid workspace directory index')
        if alias not in names:
            name = uuid.uuid4().hex
            while name in names.values():
                name = uuid.uuid4().hex
            names[alias] = name
            accounts.atomic_json(path, names)
        parent = runtime / ('account-' + names[alias])
    accounts.private_dir(parent)
    return parent
