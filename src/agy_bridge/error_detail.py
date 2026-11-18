"""Readable provider error messages, not credential-bearing logs or trace dumps."""
import json
import re
from urllib.parse import urlsplit, urlunsplit

LIMIT = 4096
_KEYS = r'access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|client[_-]?secret|authorization|password|passwd|cookie|set-cookie|csrf[_-]?token|code[_-]?verifier|code[_-]?challenge'

def redact(text, secrets=()):
    if not isinstance(text, str): return '', False, False
    original = text
    # Literal values known to this isolated CLI request are the strongest filter.
    for value in sorted({x for x in secrets if isinstance(x, str) and len(x) >= 6}, key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[REDACTED]', text, flags=re.S)
    text = re.sub(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_./+=:-]+', r'\1 [REDACTED]', text)
    text = re.sub(r'(?i)(["\']?(?:'+_KEYS+r')["\']?\s*[:=]\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\s,;}\]]+)', r'\1[REDACTED]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED]', text)
    text = re.sub(r'\b(?:ya29\.[A-Za-z0-9_.-]{10,}|sk-[A-Za-z0-9_-]{12,}|4/0A[A-Za-z0-9_-]{20,})', '[REDACTED]', text)
    def url(match):
        try:
            u=urlsplit(match[0]); host=u.hostname or ''
            if ':' in host: host='['+host+']'
            if u.port is not None:host+=':'+str(u.port)
            return urlunsplit((u.scheme,host,u.path,'','')) + ('?[REDACTED]' if u.query or u.fragment else '')
        except ValueError:return '[REDACTED_URL]'
    text=re.sub(r'https?://[^\s<>"\']+', url, text)
    text=re.sub(r'/(?:home|root|tmp|mnt)/[^\s"\'<>]+', '[LOCAL_PATH]', text)
    # Preserve human-readable lines, but remove terminal escape/control sequences.
    text=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text=''.join(c for c in text if c in '\n\t' or ord(c)>=32).strip()
    changed=text != original.strip()
    truncated=len(text)>LIMIT
    if truncated:text=text[:LIMIT]+'… [truncated]'
    return text,changed,truncated


def original_message(raw, nodes):
    """Choose the actual error field, never JSON/trace wrappers around it."""
    def embedded(s):
        for match in list(re.finditer(r'\{\s*"(?:error|code)"',s))[:8]:
            try:json.JSONDecoder().raw_decode(s[match.start():]);return True
            except ValueError:pass
        return False
    candidates=[]
    for node in nodes:
        for key in ('userErrorMessage', 'shortError', 'lastStepError', 'error', 'message', 'modelErrorMessage'):
            value=node.get(key)
            if isinstance(value,str) and value.strip() and not embedded(value):
                candidates.append(({'userErrorMessage':1,'shortError':2,'lastStepError':2,'error':3,'message':4,'modelErrorMessage':5}[key],value))
    # Embedded provider JSON is more authoritative than a raw prefix/suffix.
    if candidates:value=max(enumerate(candidates),key=lambda x:(x[1][0],x[0]))[1][1]
    elif isinstance(raw,str) and not embedded(raw):value=raw
    else:return ''
    match=re.search(r'\bError \d{3},\s*Message:\s*(.*?)(?:,\s*Status:|,\s*Details:|$)',value,re.S)
    if match:return match[1].strip()
    # A CLI error may append a stack trace or debugging dump. Keep its error text.
    lines=[]
    for line in value.splitlines():
        if re.match(r'\s*(?:goroutine \d+|Traceback \(|at \S+|\S+\.go:\d+)',line):break
        lines.append(line)
    return '\n'.join(lines).strip()
