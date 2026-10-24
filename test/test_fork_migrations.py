"""Exercise real Alembic upgrades without starting the application or touching deployment data.

SQLite always runs. OWUI_TEST_POSTGRES_URL enables the same cases against a disposable
PostgreSQL instance; each case creates its own schema inside that test instance.
"""

import json
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

ROOT = Path(__file__).parents[1]
COMMON = 'd4c1a8e37b62'
FORK = 'f7d8e9a0b1c2'
UPSTREAM = '0bd252b206fb'
MERGED = '92b6f0c1a7d3'


@pytest.fixture
def migration_scripts(monkeypatch, tmp_path):
    # Revision modules import the legacy JSONField, but the app itself must not start.
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "import-only.db"}')
    monkeypatch.setenv('WEBUI_SECRET_KEY', 'isolated-migration-test-not-a-production-secret')
    monkeypatch.setenv('ENABLE_DB_MIGRATIONS', 'false')
    return ScriptDirectory(str(ROOT / 'backend/open_webui/migrations'))


@pytest.fixture(params=['sqlite', 'postgresql'])
def migration_connection(request, tmp_path):
    if request.param == 'postgresql':
        url = os.environ.get('OWUI_TEST_POSTGRES_URL')
        if not url:
            pytest.skip('Set OWUI_TEST_POSTGRES_URL to run isolated PostgreSQL migration tests')
    else:
        url = f'sqlite:///{tmp_path / "migration.db"}'
    engine = sa.create_engine(url)
    with engine.connect() as connection:
        schema = None
        if request.param == 'postgresql':
            schema = 'owui_merge_test_' + uuid.uuid4().hex
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET search_path TO "{schema}"')
            connection.commit()
        try:
            yield connection
        finally:
            connection.rollback()
            if schema:
                connection.exec_driver_sql('SET search_path TO public')
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
                connection.commit()
    engine.dispose()


def _upgrade(connection, scripts, target):
    # Same revision planner/runtime as `alembic upgrade`, without importing the web app's env.py.
    with EnvironmentContext(
        Config(), scripts, fn=lambda revisions, context: scripts._upgrade_revs(target, revisions)
    ) as env:
        env.configure(connection=connection)
        with env.begin_transaction():
            env.run_migrations()


def _table(connection, name):
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def _seed_common_schema(connection):
    user = _table(connection, 'user')
    connection.execute(
        user.insert(),
        [
            {'id': 'user-1', 'email': 'one@example.invalid', 'name': 'First', 'role': 'user'},
            {'id': 'oauth-only', 'email': 'oauth@example.invalid', 'name': 'OAuth', 'role': 'user'},
        ],
    )
    connection.execute(
        _table(connection, 'auth').insert(),
        {
            'id': 'user-1',
            'email': 'one@example.invalid',
            'password': 'existing-password-hash',
            'active': True,
        },
    )
    connection.execute(
        _table(connection, 'chat').insert(),
        {
            'id': 'chat-1',
            'user_id': 'user-1',
            'title': 'Existing chat',
            'created_at': 1,
            'updated_at': 2,
            'chat': {
                'history': {
                    'messages': {
                        'assistant-1': {
                            'role': 'assistant',
                            'responseId': 'resp_existing',
                            'content': 'Existing answer',
                        }
                    }
                }
            },
        },
    )
    connection.execute(
        _table(connection, 'chat_message').insert(),
        {
            'id': 'chat-1-assistant-1',
            'chat_id': 'chat-1',
            'user_id': 'user-1',
            'role': 'assistant',
            'content': 'Existing answer',
            'output': [{'type': 'message', 'content': []}],
            'model_id': 'hermes',
            'created_at': 1,
            'updated_at': 2,
        },
    )
    connection.execute(
        _table(connection, 'skill').insert(),
        {
            'id': 'skill-1',
            'user_id': 'user-1',
            'name': 'Existing skill',
            'description': 'Keep bytes',
            'content': '# Existing skill\n\nСохранить без изменений.\n',
            'meta': {},
            'is_active': True,
            'created_at': 1,
            'updated_at': 2,
        },
    )
    connection.execute(
        _table(connection, 'model').insert(),
        {
            'id': 'hermes',
            'user_id': 'user-1',
            'name': 'Hermes',
            'base_model_id': 'hermes-base',
            'params': json.dumps({'temperature': 0.4}),
            'meta': json.dumps({'description': 'Existing model'}),
            'created_at': 1,
            'updated_at': 2,
        },
    )
    for name in ('tool', 'function'):
        connection.execute(
            _table(connection, name).insert(),
            {
                'id': name + '-1',
                'user_id': 'user-1',
                'name': 'Existing ' + name,
                'content': '# source must not execute or change\n',
                'meta': '{}',
                'created_at': 1,
                'updated_at': 2,
            },
        )
    connection.commit()


def test_single_head_keeps_original_fork_revision_parents(migration_scripts):
    assert migration_scripts.get_heads() == [MERGED]
    assert migration_scripts.get_revision('e2a43c7819b6').down_revision == COMMON
    assert migration_scripts.get_revision(FORK).down_revision == 'e2a43c7819b6'
    assert set(migration_scripts.get_revision(MERGED).down_revision) == {FORK, UPSTREAM}


@pytest.mark.parametrize('starting_point', ['fresh', 'common', 'response_id_only', 'fork', 'upstream', 'both'])
def test_upgrade_preserves_existing_data_and_can_be_repeated(migration_scripts, migration_connection, starting_point):
    connection = migration_connection
    scripts = migration_scripts
    if starting_point != 'fresh':
        _upgrade(connection, scripts, COMMON)
        _seed_common_schema(connection)
        if starting_point in {'fork', 'both', 'response_id_only'}:
            _upgrade(connection, scripts, 'e2a43c7819b6' if starting_point == 'response_id_only' else FORK)
        if starting_point in {'fork', 'both'}:
            messages = _table(connection, 'chat_message')
            connection.execute(
                messages.update().values(
                    response_id='resp_newer',
                    response_status='incomplete',
                    incomplete_details={'reason': 'max_output_tokens'},
                )
            )
            connection.commit()
        if starting_point in {'upstream', 'both'}:
            _upgrade(connection, scripts, UPSTREAM)
            auth = _table(connection, 'auth')
            connection.execute(
                auth.update()
                .where(auth.c.id == 'user-1')
                .values(
                    mfa={'enabled': True},
                    session_stamp='existing-stamp',
                )
            )
            connection.commit()

    _upgrade(connection, scripts, 'head')
    assert MigrationContext.configure(connection).get_current_heads() == (MERGED,)
    columns = {column['name'] for column in sa.inspect(connection).get_columns('chat_message')}
    assert {'response_id', 'response_status', 'incomplete_details'} <= columns
    assert {'mfa', 'session_stamp'} <= {column['name'] for column in sa.inspect(connection).get_columns('auth')}
    assert {'skill_history', 'model_history', 'tool_history', 'function_history', 'app'} <= set(
        sa.inspect(connection).get_table_names()
    )
    connection.commit()
    _upgrade(connection, scripts, 'head')
    assert MigrationContext.configure(connection).get_current_heads() == (MERGED,)

    if starting_point == 'fresh':
        return
    message = connection.execute(sa.select(_table(connection, 'chat_message'))).mappings().one()
    assert message['content'] == 'Existing answer'
    assert message['response_id'] == ('resp_newer' if starting_point in {'fork', 'both'} else 'resp_existing')
    if starting_point in {'fork', 'both'}:
        assert message['response_status'] == 'incomplete'
        assert message['incomplete_details'] == {'reason': 'max_output_tokens'}
    auth = _table(connection, 'auth')
    old_auth = connection.execute(sa.select(auth).where(auth.c.id == 'user-1')).mappings().one()
    assert old_auth['password'] == 'existing-password-hash'
    if starting_point in {'upstream', 'both'}:
        assert old_auth['mfa'] == {'enabled': True}
        assert old_auth['session_stamp'] == 'existing-stamp'
    assert connection.execute(sa.select(auth.c.id).where(auth.c.id == 'oauth-only')).scalar_one() == 'oauth-only'
    skill = connection.execute(sa.select(_table(connection, 'skill'))).mappings().one()
    assert skill['content'] == '# Existing skill\n\nСохранить без изменений.\n'
    assert skill['data']['files'][0]['content'] == skill['content']
    for name in ('skill', 'model', 'tool', 'function'):
        history = _table(connection, name + '_history')
        assert connection.execute(sa.select(sa.func.count()).select_from(history)).scalar_one() == 1
    if connection.dialect.name == 'postgresql':
        assert 'file_pending_knowledge_idx' in {index['name'] for index in sa.inspect(connection).get_indexes('file')}
