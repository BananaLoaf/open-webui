import ast
import asyncio
import copy
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


def _package(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    return module


def _load_source_functions(relative_path: str, names: set[str], namespace: dict) -> SimpleNamespace:
    """Compile selected real functions without importing the full OpenWebUI runtime."""
    source = Path(__file__).parents[1] / relative_path
    tree = ast.parse(source.read_text(), filename=str(source))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            function = copy.deepcopy(node)
            function.decorator_list = []
            selected.append(function)

    missing = names.difference(node.name for node in selected)
    if missing:
        raise AssertionError(f'Missing source functions: {sorted(missing)}')

    module = ast.Module(
        body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *selected],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    compiled_namespace = dict(namespace)
    exec(compile(module, str(source), 'exec'), compiled_namespace)
    return SimpleNamespace(**{name: compiled_namespace[name] for name in names})


def _run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def openai_files(monkeypatch):
    for package_name in (
        'open_webui',
        'open_webui.models',
        'open_webui.storage',
        'open_webui.utils',
        'open_webui.utils.access_control',
    ):
        monkeypatch.setitem(sys.modules, package_name, _package(package_name))

    env = types.ModuleType('open_webui.env')
    env.AIOHTTP_CLIENT_SESSION_SSL = False
    monkeypatch.setitem(sys.modules, env.__name__, env)

    files = types.ModuleType('open_webui.models.files')
    files.Files = SimpleNamespace()
    monkeypatch.setitem(sys.modules, files.__name__, files)

    provider = types.ModuleType('open_webui.storage.provider')
    provider.Storage = SimpleNamespace()
    monkeypatch.setitem(sys.modules, provider.__name__, provider)

    access = types.ModuleType('open_webui.utils.access_control.files')
    access.has_access_to_file = None
    monkeypatch.setitem(sys.modules, access.__name__, access)

    session_pool = types.ModuleType('open_webui.utils.session_pool')
    session_pool.cleanup_response = None
    session_pool.get_session = None
    monkeypatch.setitem(sys.modules, session_pool.__name__, session_pool)

    source = Path(__file__).parents[1] / 'backend/open_webui/utils/openai_files.py'
    spec = importlib.util.spec_from_file_location('test_openai_files_module', source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_forwards_only_current_non_image_files(openai_files, monkeypatch, tmp_path):
    document = tmp_path / 'document.pdf'
    document.write_bytes(b'pdf')
    image = tmp_path / 'image.png'
    image.write_bytes(b'png')
    records = {
        'document': SimpleNamespace(
            id='document',
            user_id='user-1',
            filename='document.pdf',
            path=str(document),
            meta={'content_type': 'application/pdf'},
        ),
        'image-as-file': SimpleNamespace(
            id='image-as-file',
            user_id='user-1',
            filename='image.png',
            path=str(image),
            meta={'content_type': 'image/png'},
        ),
    }

    async def get_file_by_id(file_id):
        return records.get(file_id)

    uploads = []

    async def upload(**kwargs):
        uploads.append(kwargs)
        return 'file-upstream'

    openai_files.Files.get_file_by_id = get_file_by_id
    openai_files.Storage.get_file = lambda path: path
    monkeypatch.setattr(openai_files, '_upload_to_openai_files_api', upload)

    payload = {'messages': [{'role': 'user', 'content': 'Inspect this'}]}
    result = asyncio.run(
        openai_files.forward_original_files(
            payload,
            metadata={
                'files': [{'type': 'file', 'id': 'old-chat-file'}],
                'user_message': {
                    'files': [
                        {'type': 'file', 'id': 'document'},
                        {'type': 'file', 'id': 'document'},
                        {'type': 'image', 'id': 'inline-image'},
                        {'type': 'file', 'id': 'image-as-file'},
                    ]
                },
            },
            user=SimpleNamespace(id='user-1', role='user'),
            base_url='http://hermes/v1',
            headers={'Authorization': 'Bearer secret'},
            cookies=None,
        )
    )

    assert len(uploads) == 1
    assert uploads[0]['filename'] == 'document.pdf'
    assert result['messages'][0]['content'] == [
        {'type': 'text', 'text': 'Inspect this'},
        {
            'type': 'file',
            'file': {'filename': 'document.pdf', 'file_id': 'file-upstream'},
        },
    ]


def test_rejects_more_than_ten_files(openai_files, monkeypatch, tmp_path):
    document = tmp_path / 'document.bin'
    document.write_bytes(b'data')

    async def get_file_by_id(file_id):
        return SimpleNamespace(
            id=file_id,
            user_id='user-1',
            filename=f'{file_id}.bin',
            path=str(document),
            meta={'content_type': 'application/octet-stream'},
        )

    openai_files.Files.get_file_by_id = get_file_by_id
    openai_files.Storage.get_file = lambda path: path

    with pytest.raises(openai_files.OpenAIFileForwardingError, match='At most 10 files'):
        asyncio.run(
            openai_files.forward_original_files(
                {'messages': [{'role': 'user', 'content': 'Files'}]},
                metadata={'user_message': {'files': [{'type': 'file', 'id': f'file-{index}'} for index in range(11)]}},
                user=SimpleNamespace(id='user-1', role='user'),
                base_url='http://hermes/v1',
                headers={},
                cookies=None,
            )
        )


def test_upload_uses_multipart_and_preserves_auth_headers(openai_files, monkeypatch, tmp_path):
    source = tmp_path / 'report.txt'
    source.write_text('report')
    observed = {}

    class FormData:
        def __init__(self):
            self.fields = []

        def add_field(self, name, value, **kwargs):
            self.fields.append((name, value, kwargs))

    class Response:
        status = 200
        closed = False

        async def json(self, content_type=None):
            return {'id': 'file-123'}

        def close(self):
            self.closed = True

    class Session:
        async def post(self, url, **kwargs):
            observed['url'] = url
            observed['headers'] = kwargs['headers']
            observed['fields'] = [
                (name, value.read() if name == 'file' else value, options)
                for name, value, options in kwargs['data'].fields
            ]
            return Response()

    async def get_session():
        return Session()

    async def cleanup_response(response):
        response.close()

    monkeypatch.setattr(openai_files.aiohttp, 'FormData', FormData)
    monkeypatch.setattr(openai_files, 'get_session', get_session)
    monkeypatch.setattr(openai_files, 'cleanup_response', cleanup_response)

    file_id = asyncio.run(
        openai_files._upload_to_openai_files_api(
            base_url='http://hermes/v1/',
            headers={
                'Authorization': 'Bearer secret',
                'X-Hermes-Session-Id': 'chat-1',
                'Content-Type': 'application/json',
                'Content-Length': '10',
            },
            cookies={'session': 'cookie'},
            path=source,
            filename='report.txt',
            content_type='text/plain',
        )
    )

    assert file_id == 'file-123'
    assert observed['url'] == 'http://hermes/v1/files'
    assert observed['headers'] == {
        'Authorization': 'Bearer secret',
        'X-Hermes-Session-Id': 'chat-1',
    }
    assert observed['fields'] == [
        ('purpose', 'user_data', {}),
        ('file', b'report', {'filename': 'report.txt', 'content_type': 'text/plain'}),
    ]


@pytest.mark.parametrize(
    'metadata,error',
    [
        ({'user_message': {'files': 'invalid'}}, 'Current OpenWebUI message has invalid files metadata'),
        ({'files': 'invalid'}, 'OpenWebUI request has invalid files metadata'),
    ],
)
def test_rejects_invalid_files_metadata(openai_files, metadata, error):
    with pytest.raises(openai_files.OpenAIFileForwardingError, match=error):
        _run(
            openai_files.forward_original_files(
                {'messages': [{'role': 'user', 'content': 'Files'}]},
                metadata=metadata,
                user=SimpleNamespace(id='user-1', role='user'),
                base_url='http://hermes/v1',
                headers={},
                cookies=None,
            )
        )


def test_current_message_without_files_does_not_reuse_history_files(openai_files):
    payload = {'messages': [{'role': 'user', 'content': 'No attachment this turn'}]}

    result = _run(
        openai_files.forward_original_files(
            payload,
            metadata={
                'files': [{'type': 'file', 'id': 'historical-file'}],
                'user_message': {'files': None},
            },
            user=SimpleNamespace(id='user-1', role='user'),
            base_url='http://hermes/v1',
            headers={},
            cookies=None,
        )
    )

    assert result is payload
    assert payload['messages'][0]['content'] == 'No attachment this turn'


def test_legacy_metadata_files_fallback(openai_files):
    files = [{'type': 'file', 'id': 'legacy-file'}]

    assert openai_files._get_current_message_files({}) == []
    assert openai_files._get_current_message_files({'files': files}) is files


def test_shared_file_access_and_structured_content(openai_files, monkeypatch, tmp_path):
    document = tmp_path / 'shared.txt'
    document.write_text('shared')
    stored = SimpleNamespace(
        user_id='owner',
        filename='shared.txt',
        path=str(document),
        meta={'content_type': 'text/plain'},
    )
    access_checks = []

    async def get_file_by_id(file_id):
        return stored

    async def has_access(file_id, access_type, user):
        access_checks.append((file_id, access_type, user.id))
        return True

    async def upload(**kwargs):
        return 'shared-upstream-id'

    openai_files.Files.get_file_by_id = get_file_by_id
    openai_files.Storage.get_file = lambda path: path
    monkeypatch.setattr(openai_files, 'has_access_to_file', has_access)
    monkeypatch.setattr(openai_files, '_upload_to_openai_files_api', upload)

    payload = {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'Read this'}]}]}
    result = _run(
        openai_files.forward_original_files(
            payload,
            metadata={'user_message': {'files': [{'type': 'file', 'id': 'shared-file'}]}},
            user=SimpleNamespace(id='reader', role='user'),
            base_url='http://hermes/v1',
            headers={},
            cookies=None,
        )
    )

    assert access_checks == [('shared-file', 'read', 'reader')]
    assert result['messages'][0]['content'][-1] == {
        'type': 'file',
        'file': {'filename': 'shared.txt', 'file_id': 'shared-upstream-id'},
    }


@pytest.mark.parametrize('stored,expected_status', [(None, 400), ('denied', 403)])
def test_rejects_missing_or_inaccessible_files(openai_files, monkeypatch, stored, expected_status):
    record = (
        SimpleNamespace(user_id='owner', filename='private.txt', path='/unused', meta={'content_type': 'text/plain'})
        if stored
        else None
    )

    async def get_file_by_id(file_id):
        return record

    async def has_access(file_id, access_type, user):
        return False

    openai_files.Files.get_file_by_id = get_file_by_id
    monkeypatch.setattr(openai_files, 'has_access_to_file', has_access)

    with pytest.raises(openai_files.OpenAIFileForwardingError) as error:
        _run(openai_files._get_accessible_file('private-file', SimpleNamespace(id='reader', role='user')))

    assert error.value.status_code == expected_status


@pytest.mark.parametrize(
    'kind,error',
    [
        ('empty', 'Empty files'),
        ('oversized', 'must not exceed 20 MiB'),
        ('directory', 'not a regular file'),
    ],
)
def test_rejects_invalid_local_files(openai_files, tmp_path, kind, error):
    path = tmp_path / kind
    if kind == 'directory':
        path.mkdir()
    elif kind == 'oversized':
        with path.open('wb') as handle:
            handle.seek(openai_files.MAX_FILE_BYTES)
            handle.write(b'x')
    else:
        path.touch()

    openai_files.Storage.get_file = lambda stored_path: stored_path
    stored = SimpleNamespace(filename=path.name, path=str(path))

    with pytest.raises(openai_files.OpenAIFileForwardingError, match=error):
        _run(openai_files._resolve_local_path(stored))


def test_rejects_unreadable_storage_path(openai_files):
    openai_files.Storage.get_file = lambda path: '/missing/openwebui-file'
    stored = SimpleNamespace(filename='missing.txt', path='storage-key')

    with pytest.raises(openai_files.OpenAIFileForwardingError, match='cannot be read'):
        _run(openai_files._resolve_local_path(stored))


def test_rejects_storage_record_without_path(openai_files):
    stored = SimpleNamespace(filename='missing.txt', path=None)

    with pytest.raises(openai_files.OpenAIFileForwardingError, match='has no stored content'):
        _run(openai_files._resolve_local_path(stored))


@pytest.mark.parametrize(
    'payload,error',
    [
        ({}, 'messages array'),
        ({'messages': [{'role': 'assistant', 'content': 'done'}]}, 'without a user message'),
        ({'messages': [{'role': 'user', 'content': {'unexpected': True}}]}, 'unsupported shape'),
    ],
)
def test_rejects_payload_without_supported_user_message(openai_files, payload, error):
    with pytest.raises(openai_files.OpenAIFileForwardingError, match=error):
        openai_files._get_target_user_message(payload)


def test_rejects_invalid_attachment_items(openai_files):
    user = SimpleNamespace(id='user-1', role='user')

    with pytest.raises(openai_files.OpenAIFileForwardingError, match='must be an object'):
        _run(openai_files._prepare_file('invalid', user))
    with pytest.raises(openai_files.OpenAIFileForwardingError, match='missing its OpenWebUI file id'):
        _run(openai_files._prepare_file({'type': 'file'}, user))


def test_upload_reports_upstream_error_and_cleans_response(openai_files, monkeypatch, tmp_path):
    source = tmp_path / 'report.txt'
    source.write_text('report')
    cleaned = []

    class Response:
        status = 422

        async def json(self, content_type=None):
            raise ValueError('not json')

        async def text(self):
            return 'unsupported purpose'

    class Session:
        async def post(self, url, **kwargs):
            return Response()

    async def get_session():
        return Session()

    async def cleanup_response(response):
        cleaned.append(response)

    monkeypatch.setattr(openai_files, 'get_session', get_session)
    monkeypatch.setattr(openai_files, 'cleanup_response', cleanup_response)

    with pytest.raises(openai_files.OpenAIFileForwardingError) as error:
        _run(
            openai_files._upload_to_openai_files_api(
                base_url='http://hermes/v1',
                headers={},
                cookies=None,
                path=source,
                filename='report.txt',
                content_type='text/plain',
            )
        )

    assert error.value.status_code == 502
    assert 'HTTP 422: unsupported purpose' in str(error.value)
    assert len(cleaned) == 1


def test_upload_reports_structured_upstream_error(openai_files, monkeypatch, tmp_path):
    source = tmp_path / 'report.txt'
    source.write_text('report')

    class Response:
        status = 400

        async def json(self, content_type=None):
            return {'error': {'message': 'invalid purpose'}}

    class Session:
        async def post(self, url, **kwargs):
            return Response()

    async def get_session():
        return Session()

    async def cleanup_response(response):
        return None

    monkeypatch.setattr(openai_files, 'get_session', get_session)
    monkeypatch.setattr(openai_files, 'cleanup_response', cleanup_response)

    with pytest.raises(openai_files.OpenAIFileForwardingError, match='HTTP 400: invalid purpose'):
        _run(
            openai_files._upload_to_openai_files_api(
                base_url='http://hermes/v1',
                headers={},
                cookies=None,
                path=source,
                filename='report.txt',
                content_type='text/plain',
            )
        )


@pytest.mark.parametrize(
    'payload,expected',
    [
        ({'status': 'invalid'}, "{'status': 'invalid'}"),
        (['invalid'], "['invalid']"),
    ],
)
def test_formats_generic_upstream_error_payload(openai_files, payload, expected):
    class Response:
        async def json(self, content_type=None):
            return payload

    assert _run(openai_files._response_error(Response())) == expected


def test_upload_rejects_response_without_file_id(openai_files, monkeypatch, tmp_path):
    source = tmp_path / 'report.txt'
    source.write_text('report')

    class Response:
        status = 200

        async def json(self, content_type=None):
            return {'status': 'ok'}

    class Session:
        async def post(self, url, **kwargs):
            return Response()

    async def get_session():
        return Session()

    async def cleanup_response(response):
        return None

    monkeypatch.setattr(openai_files, 'get_session', get_session)
    monkeypatch.setattr(openai_files, 'cleanup_response', cleanup_response)

    with pytest.raises(openai_files.OpenAIFileForwardingError, match='returned no file_id'):
        _run(
            openai_files._upload_to_openai_files_api(
                base_url='http://hermes/v1',
                headers={},
                cookies=None,
                path=source,
                filename='report.txt',
                content_type='text/plain',
            )
        )


def test_upload_wraps_transport_failure(openai_files, monkeypatch, tmp_path):
    source = tmp_path / 'report.txt'
    source.write_text('report')

    class Session:
        async def post(self, url, **kwargs):
            raise openai_files.aiohttp.ClientConnectionError('offline')

    async def get_session():
        return Session()

    async def cleanup_response(response):
        assert response is None

    monkeypatch.setattr(openai_files, 'get_session', get_session)
    monkeypatch.setattr(openai_files, 'cleanup_response', cleanup_response)

    with pytest.raises(openai_files.OpenAIFileForwardingError) as error:
        _run(
            openai_files._upload_to_openai_files_api(
                base_url='http://hermes/v1',
                headers={},
                cookies=None,
                path=source,
                filename='report.txt',
                content_type='text/plain',
            )
        )

    assert error.value.status_code == 502
    assert 'upload failed' in str(error.value)


def test_upload_wraps_file_open_failure(openai_files, monkeypatch, tmp_path):
    async def cleanup_response(response):
        assert response is None

    monkeypatch.setattr(openai_files, 'cleanup_response', cleanup_response)

    with pytest.raises(openai_files.OpenAIFileForwardingError, match='cannot be read'):
        _run(
            openai_files._upload_to_openai_files_api(
                base_url='http://hermes/v1',
                headers={},
                cookies=None,
                path=tmp_path / 'missing.txt',
                filename='missing.txt',
                content_type='text/plain',
            )
        )


def test_image_only_request_keeps_vision_payload_unchanged(openai_files):
    payload = {
        'messages': [
            {
                'role': 'user',
                'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA'}}],
            }
        ]
    }

    result = _run(
        openai_files.forward_original_files(
            payload,
            metadata={'user_message': {'files': [{'type': 'image', 'id': 'image-file'}]}},
            user=SimpleNamespace(id='user-1', role='user'),
            base_url='http://hermes/v1',
            headers={},
            cookies=None,
        )
    )

    assert result is payload


def test_public_upload_route_forces_raw_storage():
    observed = {}

    async def upload_file_handler(request, **kwargs):
        observed.update(kwargs)
        return {
            'id': 'openwebui-file',
            'filename': 'report.pdf',
            'meta': {'content_type': 'application/pdf'},
        }

    published = []

    async def publish_event(request, event, **kwargs):
        published.append((event, kwargs))

    def dependency(*args, **kwargs):
        return None

    source = _load_source_functions(
        'backend/open_webui/routers/files.py',
        {'upload_file'},
        {
            'BackgroundTasks': object,
            'Depends': dependency,
            'File': dependency,
            'Form': dependency,
            'Query': dependency,
            'Request': object,
            'UploadFile': object,
            'AsyncSession': object,
            'Optional': object,
            'FileModelResponse': object,
            'get_verified_user': None,
            'get_async_session': None,
            'upload_file_handler': upload_file_handler,
            'publish_event': publish_event,
            'EVENTS': SimpleNamespace(FILE_UPLOADED='file.uploaded'),
        },
    )
    user = SimpleNamespace(id='user-1')

    result = _run(
        source.upload_file(
            request='request',
            background_tasks='tasks',
            file='file',
            metadata={'source': 'chat'},
            process=True,
            process_in_background=True,
            user=user,
            db='db',
        )
    )

    assert result['id'] == 'openwebui-file'
    assert observed == {
        'file': 'file',
        'metadata': {'source': 'chat'},
        'process': False,
        'process_in_background': True,
        'user': user,
        'background_tasks': 'tasks',
        'db': 'db',
    }
    assert published == [
        (
            'file.uploaded',
            {
                'actor': user,
                'subject_id': 'openwebui-file',
                'data': {'filename': 'report.pdf', 'content_type': 'application/pdf'},
            },
        )
    ]


def test_responses_converter_preserves_file_references_and_images():
    source = _load_source_functions(
        'backend/open_webui/routers/openai.py',
        {'convert_to_responses_payload'},
        {'_normalize_stored_item': lambda item: item},
    )

    result = source.convert_to_responses_payload(
        {
            'model': 'profile',
            'messages': [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': 'Inspect these'},
                        {
                            'type': 'file',
                            'file': {
                                'file_id': 'file-upstream',
                                'filename': 'report.pdf',
                                'ignored': 'value',
                            },
                        },
                        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA', 'detail': 'high'}},
                        {'type': 'file', 'file': {'filename': 'invalid-without-id.txt'}},
                        {
                            'type': 'file',
                            'file': {'file_data': 'data:application/pdf;base64,AA', 'filename': 'inline.pdf'},
                        },
                        {'type': 'file', 'file': 'invalid'},
                    ],
                }
            ],
        }
    )

    assert result['input'] == [
        {
            'type': 'message',
            'role': 'user',
            'content': [
                {'type': 'input_text', 'text': 'Inspect these'},
                {'type': 'input_file', 'file_id': 'file-upstream', 'filename': 'report.pdf'},
                {'type': 'input_image', 'image_url': 'data:image/png;base64,AA', 'detail': 'high'},
                {
                    'type': 'input_file',
                    'file_data': 'data:application/pdf;base64,AA',
                    'filename': 'inline.pdf',
                },
            ],
        }
    ]


class _HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _JSONCodec:
    JSONDecodeError = json.JSONDecodeError
    dumps = staticmethod(json.dumps)
    loads = staticmethod(json.loads)


def _openai_route_runtime(openai_files, api_config, forward_original_files):  # noqa: C901
    observed = {}

    class Config:
        @staticmethod
        async def get(key):
            return key == 'openai.enable'

    class Models:
        @staticmethod
        async def get_model_by_id(model_id):
            return None

    async def check_model_access(user, model_info, bypass_filter):
        return None

    async def get_openai_connection(index):
        return 'http://hermes/v1', 'api-key', api_config

    async def get_headers_and_cookies(request, url, key, config, metadata, user):
        return {'Authorization': f'Bearer {key}'}, {'session': 'cookie'}

    class Response:
        status = 200
        headers = {'Content-Type': 'application/json'}

        async def json(self, loads=None):
            return {'id': 'response-1', 'output': []}

        async def text(self):
            return ''

    class Session:
        async def request(self, **kwargs):
            observed.update(kwargs)
            return Response()

    async def get_session():
        return Session()

    async def cleanup_response(response):
        observed['cleaned'] = response is not None

    def dependency(*args, **kwargs):
        return None

    converter = _load_source_functions(
        'backend/open_webui/routers/openai.py',
        {'convert_to_responses_payload'},
        {'_normalize_stored_item': lambda item: item},
    ).convert_to_responses_payload
    source = _load_source_functions(
        'backend/open_webui/routers/openai.py',
        {'generate_chat_completion'},
        {
            'Depends': dependency,
            'Request': object,
            'get_verified_user': None,
            'Config': Config,
            'Models': Models,
            'check_model_access': check_model_access,
            'BYPASS_MODEL_ACCESS_CONTROL': False,
            'ERROR_MESSAGES': SimpleNamespace(
                MODEL_NOT_FOUND=lambda: 'model not found',
                SERVER_CONNECTION_ERROR='server connection error',
            ),
            'get_all_models': None,
            'get_openai_connection': get_openai_connection,
            'strip_provider_model_prefix': lambda model, prefix: model,
            'is_openai_new_model': lambda model: False,
            'openai_reasoning_model_handler': lambda payload: payload,
            'convert_logit_bias_input_to_json': lambda value: value,
            'JSONCodec': _JSONCodec,
            'get_headers_and_cookies': get_headers_and_cookies,
            'forward_original_files': forward_original_files,
            'OpenAIFileForwardingError': openai_files.OpenAIFileForwardingError,
            'HTTPException': _HTTPException,
            'apply_model_params_to_body_openai': lambda params, payload: payload,
            'apply_system_prompt_to_body': None,
            're': __import__('re'),
            'convert_to_responses_payload': converter,
            'convert_to_azure_payload': None,
            'get_session': get_session,
            'get_client_timeout': lambda stream: None,
            'AIOHTTP_CLIENT_SESSION_SSL': False,
            'convert_responses_result': lambda response: response,
            'publish_model_provider_request_failed': None,
            'JSONResponse': None,
            'PlainTextResponse': None,
            'StreamingResponse': None,
            'stream_wrapper': None,
            '_clean_proxy_headers': None,
            'cleanup_response': cleanup_response,
            'log': SimpleNamespace(error=lambda *args, **kwargs: None, exception=lambda *args, **kwargs: None),
        },
    )
    request = SimpleNamespace(
        state=SimpleNamespace(),
        app=SimpleNamespace(state=SimpleNamespace(OPENAI_MODELS={'profile': {'urlIdx': 0}})),
    )
    user = SimpleNamespace(id='user-1', name='User', email='user@example.com', role='user')
    return source.generate_chat_completion, request, user, observed


def test_openai_route_forwards_files_before_responses_conversion(openai_files):
    forwarded = []

    async def forward(payload, **kwargs):
        forwarded.append(kwargs)
        payload['messages'][-1]['content'] = [
            {'type': 'text', 'text': 'Inspect'},
            {'type': 'file', 'file': {'file_id': 'file-upstream', 'filename': 'report.pdf'}},
        ]
        return payload

    generate, request, user, observed = _openai_route_runtime(
        openai_files,
        {'api_type': 'responses', 'files_api': True},
        forward,
    )
    metadata = {'user_message': {'files': [{'type': 'file', 'id': 'openwebui-file'}]}}

    _run(
        generate(
            request,
            {
                'model': 'profile',
                'messages': [{'role': 'user', 'content': 'Inspect'}],
                'metadata': metadata,
            },
            user,
        )
    )

    assert forwarded == [
        {
            'metadata': metadata,
            'user': user,
            'base_url': 'http://hermes/v1',
            'headers': {'Authorization': 'Bearer api-key'},
            'cookies': {'session': 'cookie'},
        }
    ]
    assert observed['url'] == 'http://hermes/v1/responses'
    assert json.loads(observed['data'])['input'][0]['content'] == [
        {'type': 'input_text', 'text': 'Inspect'},
        {'type': 'input_file', 'file_id': 'file-upstream', 'filename': 'report.pdf'},
    ]
    assert observed['cleaned'] is True


def test_openai_route_does_not_forward_without_files_api(openai_files):
    async def unexpected_forward(payload, **kwargs):
        raise AssertionError('forward_original_files must not be called')

    generate, request, user, observed = _openai_route_runtime(
        openai_files,
        {'api_type': 'responses'},
        unexpected_forward,
    )

    _run(
        generate(
            request,
            {'model': 'profile', 'messages': [{'role': 'user', 'content': 'Hello'}]},
            user,
        )
    )

    assert json.loads(observed['data'])['input'][0]['content'] == [{'type': 'input_text', 'text': 'Hello'}]


def test_openai_route_maps_file_forwarding_errors(openai_files):
    async def fail_forward(payload, **kwargs):
        raise openai_files.OpenAIFileForwardingError('Hermes rejected the file', status_code=502)

    generate, request, user, observed = _openai_route_runtime(
        openai_files,
        {'api_type': 'responses', 'files_api': True},
        fail_forward,
    )

    with pytest.raises(_HTTPException) as error:
        _run(
            generate(
                request,
                {'model': 'profile', 'messages': [{'role': 'user', 'content': 'Inspect'}]},
                user,
            )
        )

    assert error.value.status_code == 502
    assert error.value.detail == 'Hermes rejected the file'
    assert observed == {}
