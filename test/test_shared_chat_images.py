"""Replay shared-chat images with file ACL checks, including old image attachments."""

import ast
import asyncio
import base64
import importlib.util
import logging
import mimetypes
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiofiles
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

ROOT = Path(__file__).parents[1] / 'backend/open_webui'


def load_function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text())
    node = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    tree = ast.Module(
        body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(tree), str(ROOT / path), 'exec'), namespace)
    return namespace[name]


@pytest.fixture
def references(monkeypatch):
    name = 'open_webui.utils.chat_attachments'
    spec = importlib.util.spec_from_file_location(name, ROOT / 'utils/chat_attachments.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, name, module)
    return module.referenced_file_ids


def test_snapshot_references_use_attachments_not_text(references):
    assert references(
        {
            'history': {
                'messages': {
                    'u': {
                        'content': 'secret-file',
                        'files': [
                            {'type': 'image', 'url': '/api/v1/files/photo/content?download=1'},
                            {'type': 'file', 'id': 'pdf'},
                            {'type': 'image', 'url': 'old-image'},
                            {'type': 'image', 'url': 'https://example.test/remote'},
                        ],
                    },
                }
            }
        }
    ) == {'photo', 'pdf', 'old-image'}
    assert references({'messages': [{'files': [{'type': 'image', 'id': 'photo'}]}]}) == {'photo'}


def test_legacy_shared_snapshot_lookup_checks_owner_and_real_reference(references):
    async def run():
        base = declarative_base()

        class SharedChat(base):
            __tablename__ = 'shared_chat'
            id = sa.Column(sa.Text, primary_key=True)
            chat_id = sa.Column(sa.Text)
            user_id = sa.Column(sa.Text)
            chat = sa.Column(sa.JSON)

        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def context(db=None):
            async with sessions() as session:
                yield session

        lookup = load_function(
            'models/shared_chats.py',
            'get_chat_ids_by_file_id',
            {
                'get_async_db_context': context,
                'SharedChat': SharedChat,
                'select': sa.select,
                'cast': sa.cast,
                'Text': sa.Text,
            },
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(base.metadata.create_all)
            async with sessions() as session:
                for key, owner, payload in [
                    ('valid', 'owner', {'messages': [{'files': [{'type': 'image', 'url': 'photo'}]}]}),
                    ('forged', 'attacker', {'messages': [{'files': [{'type': 'image', 'id': 'photo'}]}]}),
                    ('text-only', 'owner', {'messages': [{'content': 'photo'}]}),
                    ('prefix', 'owner', {'messages': [{'files': [{'type': 'image', 'id': 'photo-extra'}]}]}),
                ]:
                    session.add(SharedChat(id=key, chat_id=key, user_id=owner, chat=payload))
                await session.commit()
            assert await lookup(None, 'photo', 'owner') == ['valid']
            assert await lookup(None, 'missing', 'owner') == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_shared_image_read_uses_grants_and_never_confers_write_access(tmp_path):
    async def run():
        image = tmp_path / 'photo.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        file = SimpleNamespace(id='photo', user_id='owner', path=str(image), meta={})
        user = SimpleNamespace(id='reader', role='user')
        shares = SimpleNamespace(get_chat_ids_by_file_id=AsyncMock(return_value=['source-chat']))
        granted = True

        async def granted_ids(**kwargs):
            return {'source-chat'} if granted and kwargs['resource_type'] == 'shared_chat' else set()

        grants = SimpleNamespace(get_accessible_resource_ids=AsyncMock(side_effect=granted_ids))
        access = load_function(
            'utils/access_control/files.py',
            'has_access_to_file',
            {
                'Files': SimpleNamespace(get_file_by_id=AsyncMock(return_value=file)),
                'Groups': SimpleNamespace(get_groups_by_member_id=AsyncMock(return_value=[])),
                'Knowledges': SimpleNamespace(get_knowledges_by_file_id=AsyncMock(return_value=[])),
                'Channels': SimpleNamespace(get_channels_by_file_id_and_user_id=AsyncMock(return_value=[])),
                'Chats': SimpleNamespace(get_shared_chat_ids_by_file_id=AsyncMock(return_value=[])),
                'SharedChats': shares,
                'AccessGrants': grants,
                'Models': SimpleNamespace(get_model_owner_ids_by_file_id=AsyncMock(return_value={})),
                'log': logging.getLogger(__name__),
            },
        )
        assert await access('photo', 'read', user)
        shares.get_chat_ids_by_file_id.assert_awaited_once_with('photo', 'owner', db=None)
        assert not await access('photo', 'write', user)
        assert shares.get_chat_ids_by_file_id.await_count == 1
        reader = load_function(
            'utils/files.py',
            'get_image_base64_from_file_id',
            {
                'Files': SimpleNamespace(get_file_by_id=AsyncMock(return_value=file)),
                'Storage': SimpleNamespace(get_file=lambda path: path),
                'has_access_to_file': access,
                'asyncio': asyncio,
                'aiofiles': aiofiles,
                'Path': Path,
                'base64': base64,
                'mimetypes': mimetypes,
            },
        )

        async def resolve(url, user=None):
            return await reader(url, user=user)

        convert = load_function(
            'utils/middleware.py',
            'convert_url_images_to_base64',
            {
                'get_image_base64_from_url': resolve,
                'log': logging.getLogger(__name__),
            },
        )
        payload = {
            'messages': [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'image_url', 'image_url': {'url': 'photo', 'detail': 'high'}},
                    ],
                }
            ]
        }
        converted = await convert(payload, user=user)
        image_part = converted['messages'][0]['content'][0]['image_url']
        assert image_part['url'] == 'data:image/png;base64,' + base64.b64encode(image.read_bytes()).decode()
        assert image_part['detail'] == 'high'
        granted = False
        assert not await access('photo', 'read', user)
        assert await reader('photo', user=user) is None

    asyncio.run(run())
