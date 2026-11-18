"""Render validated messages into native CLI system context and dialogue."""
import re


_PREFIX = re.compile(r'</?st([0-9]*)-(?:rules|guide|system|activate)\b', re.I)


def _namespace(messages):
    # Literal examples, including transcripts from a previous request, remain
    # literal. Choose a deterministic unused prefix without rewriting input.
    used = set()
    for msg in messages:
        texts = [msg['content']]
        for call in msg.get('tool_calls', ()):
            texts.extend((call['id'], call['function']['arguments']))
        for text in texts:
            used.update(match[1] for match in _PREFIX.finditer(text))
    suffix = ''
    n = 0
    while suffix in used:
        n += 1
        suffix = str(n)
    return 'st' + suffix


def _label(index):
    # A, B, … Z, AA, … are opaque references, never message positions.
    label = ''
    while index:
        index, digit = divmod(index - 1, 26)
        label = chr(ord('A') + digit) + label
    return label


def _xml_body(text):
    # Keep markup-looking caller text readable and literal, without treating
    # nested markup as part of the bridge's XML vocabulary.
    if '<' in text or '&' in text or ']]>' in text:
        return '<![CDATA[' + text.replace(']]>', ']]]]><![CDATA[>') + ']]>'
    return text


def split_system_messages(messages):
    """Hoist bodies once; retain activation boundaries without adding turns."""
    if not any(msg['role'] == 'system' for msg in messages):
        return '', list(messages)

    ns = _namespace(messages)
    blocks, dialog, pending = [], [], []
    for msg in messages:
        role = msg['role']
        if role == 'system':
            label = _label(len(blocks) + 1)
            if dialog:
                pending.append(f'<{ns}-activate ref="{label}"/>')
                scope = 'marker'
            else:
                scope = 'start'
            blocks.append(f'<{ns}-system id="{label}" from="{scope}">{_xml_body(msg["content"])}</{ns}-system>')
            continue

        if pending:
            # Validation forbids a system message inside an unfinished tool
            # group. Never decorate a tool result, or synthesize a user turn.
            assert role in ('user', 'assistant')
            markers = '\n'.join(pending)
            if role == 'assistant' and msg.get('tool_calls') and dialog[-1]['role'] == 'user':
                # Keep the following tool-call-only assistant completely intact.
                prev = dialog[-1]
                dialog[-1] = {**prev, 'content': prev['content'] + '\n' + markers}
            else:
                msg = {**msg, 'content': markers + '\n' + msg['content']}
            pending.clear()
        dialog.append(msg)

    # Public validation requires a terminal user turn or resolved tool result.
    if pending:
        raise ValueError('Unanchored trailing system block in native dialogue')
    legend = (f'<{ns}-guide>from="start" 的规则从开场生效。from="marker" 的规则在这里预告，'
              f'仅从对话中 {ns}-activate 的 ref 与规则 id 相同的位置起生效，不追溯。'
              f'位置标记不是相邻角色的发言，也不是新的请求。</{ns}-guide>')
    return '\n'.join([f'<{ns}-rules>', legend, *blocks, f'</{ns}-rules>']), dialog
