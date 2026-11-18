"""Short credential publication lock, independent of long-lived request leases."""
import datetime
import json
import re
from pathlib import Path
from . import account_store as accounts

SCOPE_REL=Path('.config/antigravity/credential-user-scope')

def token_expiry(data):
    """Only the pinned CLI's absolute expiry is authoritative; expires_in is not."""
    try:
        value=json.loads(data)['token']['expiry']
        if not isinstance(value,str): return None
        # Go emits RFC3339 nanoseconds; Python 3.10 accepts only 3/6 digits.
        # Truncate below microsecond precision conservatively, never extend life.
        value=re.sub(r'(\d{2}:\d{2}:\d{2})\.(\d{1,9})(Z|[+-]\d{2}:\d{2})$',
                     lambda m:m[1]+'.'+m[2][:6].ljust(6,'0')+m[3], value)
        stamp=datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError): return None

def profile_of(snapshot): return snapshot.home.parent

def stage(snapshot, home, store):
    original={}
    with accounts.lock(profile_of(snapshot)/'credential-publish.lock'):
        token=snapshot.home/accounts.TOKEN_REL
        if token.is_symlink() or not token.is_file(): raise accounts.AccountError('Missing or unsafe selected credential')
        email=store._credential_email(token)
        if email and snapshot.expected_email and email.casefold()!=snapshot.expected_email.casefold():
            raise accounts.AccountError('Selected credential identity does not match the account')
        for rel in (accounts.TOKEN_REL,SCOPE_REL):
            src=snapshot.home/rel
            if src.is_symlink(): raise accounts.AccountError('Unsafe credential path')
            if not src.exists(): continue
            data=src.read_bytes();original[rel]=data
            dst=home/rel;accounts.private_dir(dst.parent);accounts.atomic_bytes(dst,data)
    return original

def publish(snapshot, home, original, store):
    """CAS under its own lock: late completion cannot overwrite a newer login/refresh."""
    if accounts.TOKEN_REL not in original: return 'not_staged'
    token=home/accounts.TOKEN_REL
    if not token.exists() or token.is_symlink(): return 'missing_or_unsafe'
    email=store._credential_email(token)
    if not email or not snapshot.expected_email or email.casefold()!=snapshot.expected_email.casefold(): return 'identity_mismatch'
    new=token.read_bytes();old=original[accounts.TOKEN_REL]
    if new==old: return 'unchanged'
    old_exp,new_exp=token_expiry(old),token_expiry(new)
    try: access=json.loads(new)['token']['access_token']
    except (ValueError,TypeError,KeyError): return 'invalid_token_skipped'
    if not isinstance(access,str) or not access or new_exp is None: return 'invalid_token_skipped'
    if old_exp is not None and new_exp is not None and new_exp<old_exp: return 'older_expiry_skipped'
    with accounts.lock(profile_of(snapshot)/'credential-publish.lock'):
        dest=snapshot.home/accounts.TOKEN_REL
        if not dest.exists() or dest.is_symlink(): return 'missing_or_unsafe'
        if dest.read_bytes()!=old: return 'concurrent_update_preserved'
        accounts.atomic_bytes(dest,new)
        return 'published'
