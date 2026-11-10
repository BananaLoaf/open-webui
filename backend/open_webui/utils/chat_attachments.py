"""File references present in a saved chat snapshot."""

import re

FILE_CONTENT_URL = re.compile(r'^/api/v1/files/([^/?#]+)/content(?:[?#]|$)')


def referenced_file_ids(chat):
    history = chat.get('history')
    messages = history.get('messages') if isinstance(history, dict) else None
    messages = messages.values() if isinstance(messages, dict) and messages else chat.get('messages') or []
    result = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        for attachment in message.get('files') or []:
            if not isinstance(attachment, dict) or attachment.get('type') not in ('file', 'image'):
                continue
            if isinstance(attachment.get('id'), str) and attachment['id']:
                result.add(attachment['id'])
            url = attachment.get('url')
            if isinstance(url, str) and url:
                match = FILE_CONTENT_URL.match(url)
                if match:
                    result.add(match.group(1))
                elif '/' not in url and ':' not in url:
                    result.add(url)
    return result
