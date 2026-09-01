"""Forward original OpenWebUI attachments to an upstream OpenAI Files API."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG

import aiohttp
from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
from open_webui.models.files import Files
from open_webui.storage.provider import Storage
from open_webui.utils.access_control.files import has_access_to_file
from open_webui.utils.session_pool import cleanup_response, get_session

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES_PER_REQUEST = 10
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)


class OpenAIFileForwardingError(RuntimeError):
    """A user-facing failure while forwarding an OpenWebUI attachment."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PreparedFile:
    filename: str
    path: Path
    content_type: str


def _get_current_message_files(metadata: dict | None) -> list:
    """Return only attachments submitted with the current user message."""
    metadata = metadata if isinstance(metadata, dict) else {}
    user_message = metadata.get('user_message')
    if isinstance(user_message, dict):
        files = user_message.get('files')
        if files is None:
            return []
        if not isinstance(files, list):
            raise OpenAIFileForwardingError('Current OpenWebUI message has invalid files metadata')
        return files

    files = metadata.get('files')
    if files is None:
        return []
    if not isinstance(files, list):
        raise OpenAIFileForwardingError('OpenWebUI request has invalid files metadata')
    return files


async def _response_error(response) -> str:
    try:
        payload = await response.json(content_type=None)
    except Exception:
        return (await response.text())[:500]
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict) and error.get('message'):
            return str(error['message'])[:500]
    return str(payload)[:500]


async def _upload_to_openai_files_api(
    *,
    base_url: str,
    headers: dict,
    cookies: dict | None,
    path: Path,
    filename: str,
    content_type: str,
) -> str:
    multipart_headers = {
        name: value for name, value in headers.items() if name.lower() not in {'content-type', 'content-length'}
    }

    response = None
    try:
        form = aiohttp.FormData()
        form.add_field('purpose', 'user_data')
        with path.open('rb') as handle:
            form.add_field(
                'file',
                handle,
                filename=filename,
                content_type=content_type,
            )
            session = await get_session()
            response = await session.post(
                f'{base_url.rstrip("/")}/files',
                data=form,
                headers=multipart_headers,
                cookies=cookies,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
                timeout=UPLOAD_TIMEOUT,
            )
            if response.status >= 400:
                detail = await _response_error(response)
                raise OpenAIFileForwardingError(
                    f'OpenAI Files API rejected {filename!r} with HTTP {response.status}: {detail}',
                    status_code=502,
                )
            payload = await response.json(content_type=None)
    except OpenAIFileForwardingError:
        raise
    except OSError as exc:
        raise OpenAIFileForwardingError(f'Attached file {filename!r} cannot be read from OpenWebUI storage') from exc
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise OpenAIFileForwardingError(
            f'OpenAI Files API upload failed for {filename!r}',
            status_code=502,
        ) from exc
    finally:
        await cleanup_response(response)

    file_id = payload.get('id') if isinstance(payload, dict) else None
    if not isinstance(file_id, str) or not file_id:
        raise OpenAIFileForwardingError(
            f'OpenAI Files API returned no file_id for {filename!r}',
            status_code=502,
        )
    return file_id


async def _get_accessible_file(file_id: str, user):
    stored = await Files.get_file_by_id(file_id)
    if stored is None:
        raise OpenAIFileForwardingError(f'Attachment {file_id!r} is not available in OpenWebUI file storage')
    if stored.user_id == user.id or user.role == 'admin':
        return stored
    if await has_access_to_file(file_id, 'read', user):
        return stored
    raise OpenAIFileForwardingError(
        f'Attached file {file_id!r} is unavailable to this user',
        status_code=403,
    )


async def _resolve_local_path(stored) -> Path:
    if not stored.path:
        raise OpenAIFileForwardingError(f'Attached file {stored.filename!r} has no stored content')
    try:
        local_path = Path(await asyncio.to_thread(Storage.get_file, stored.path))
        stat_result = await asyncio.to_thread(local_path.stat)
    except (OSError, RuntimeError, TypeError) as exc:
        raise OpenAIFileForwardingError(
            f'Attached file {stored.filename!r} cannot be read from OpenWebUI storage'
        ) from exc
    if not S_ISREG(stat_result.st_mode):
        raise OpenAIFileForwardingError(f'Attached file {stored.filename!r} is not a regular file')
    if stat_result.st_size > MAX_FILE_BYTES:
        raise OpenAIFileForwardingError('Each attached file must not exceed 20 MiB')
    if stat_result.st_size == 0:
        raise OpenAIFileForwardingError('Empty files cannot be attached')
    return local_path


async def _prepare_file(item: object, user) -> tuple[str, PreparedFile | None]:
    if not isinstance(item, dict):
        raise OpenAIFileForwardingError('OpenWebUI attachment metadata must be an object')
    if item.get('type') not in {None, 'file'}:
        return '', None

    file_id = item.get('id') or item.get('url')
    if not isinstance(file_id, str) or not file_id:
        raise OpenAIFileForwardingError('Attached file is missing its OpenWebUI file id')

    stored = await _get_accessible_file(file_id, user)
    content_type = str((stored.meta or {}).get('content_type') or 'application/octet-stream')
    if content_type.lower().startswith('image/'):
        return file_id, None

    return file_id, PreparedFile(
        filename=stored.filename,
        path=await _resolve_local_path(stored),
        content_type=content_type,
    )


def _get_target_user_message(payload: dict) -> dict:
    messages = payload.get('messages')
    if not isinstance(messages, list):
        raise OpenAIFileForwardingError('Cannot attach files without a messages array')
    target = next(
        (message for message in reversed(messages) if isinstance(message, dict) and message.get('role') == 'user'),
        None,
    )
    if target is None:
        raise OpenAIFileForwardingError('Cannot attach files without a user message')
    if not isinstance(target.get('content', ''), (str, list)):
        raise OpenAIFileForwardingError('User message content has an unsupported shape')
    return target


def _append_file_parts(target: dict, parts: list[dict]) -> None:
    content = target.get('content', '')
    if isinstance(content, str):
        target['content'] = ([{'type': 'text', 'text': content}] if content else []) + parts
    else:
        target['content'] = [*content, *parts]


async def forward_original_files(
    payload: dict,
    *,
    metadata: dict | None,
    user,
    base_url: str,
    headers: dict,
    cookies: dict | None,
) -> dict:
    """Upload current-message files and append provider-native file parts."""
    files = _get_current_message_files(metadata)
    if not files:
        return payload

    target = _get_target_user_message(payload)
    prepared: list[PreparedFile] = []
    seen_file_ids: set[str] = set()
    for item in files:
        file_id, candidate = await _prepare_file(item, user)
        if not candidate or file_id in seen_file_ids:
            continue
        if len(prepared) >= MAX_FILES_PER_REQUEST:
            raise OpenAIFileForwardingError(f'At most {MAX_FILES_PER_REQUEST} files can be attached to one message')
        seen_file_ids.add(file_id)
        prepared.append(candidate)

    if not prepared:
        return payload

    parts = []
    for prepared_file in prepared:
        file_id = await _upload_to_openai_files_api(
            base_url=base_url,
            headers=headers,
            cookies=cookies,
            path=prepared_file.path,
            filename=prepared_file.filename,
            content_type=prepared_file.content_type,
        )
        parts.append(
            {
                'type': 'file',
                'file': {
                    'filename': prepared_file.filename,
                    'file_id': file_id,
                },
            }
        )

    _append_file_parts(target, parts)
    return payload
