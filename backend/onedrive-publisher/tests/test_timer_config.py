import json
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from azure.core.exceptions import HttpResponseError
from config import Config, PublisherError


@pytest.mark.parametrize('name,raw', [
    ('ONEDRIVE_BATCH_SIZE', '0'), ('ONEDRIVE_BATCH_SIZE', '101'), ('ONEDRIVE_BATCH_SIZE', '257'),
    ('ONEDRIVE_MAX_ITEMS', '100001'), ('ONEDRIVE_MAX_ITEMS', '-1'),
    ('ONEDRIVE_MAX_PAGES', '501'), ('ONEDRIVE_MAX_PAGES', '0'),
    ('ONEDRIVE_RUN_SECONDS', '181'), ('ONEDRIVE_RUN_SECONDS', '59'),
    ('ONEDRIVE_RUN_SECONDS', 'NaN'), ('ONEDRIVE_RETAIN_INITIAL', 'yes'),
])
def test_limits_fail_closed(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(PublisherError, match='invalid_' + name):
        Config.from_env()


def test_limits_from_environment(monkeypatch):
    monkeypatch.setenv('ONEDRIVE_BATCH_SIZE', '32')
    monkeypatch.setenv('ONEDRIVE_RETAIN_INITIAL', 'true')
    config = Config.from_env()
    assert config.batch_size == 32
    assert config.max_items == 100000 and config.max_pages == 500
    assert config.run_seconds == 180 and config.retain_initial


@pytest.mark.parametrize('method', ['onedrive_publisher', 'onedrive_continuation'])
def test_both_disabled_timers_have_no_remote_io(monkeypatch, method):
    import function_app
    factory = Mock()
    engine = Mock()
    monkeypatch.setattr(function_app, 'BlobStore', factory)
    monkeypatch.setattr(function_app, 'run_resumable', engine)
    monkeypatch.setenv('ONEDRIVE_SYNC_ENABLED', 'false')
    getattr(function_app, method)(Mock())
    factory.assert_not_called()
    engine.assert_not_called()


@pytest.mark.parametrize('method,start_new', [('onedrive_publisher', True), ('onedrive_continuation', False)])
def test_timer_passes_integral_deadline_and_mode(monkeypatch, method, start_new):
    import function_app
    store = Mock()
    store.locked.return_value = nullcontext()
    engine = Mock(return_value={'status': 'running', 'phase': 'scan'})
    monkeypatch.setattr(function_app, 'BlobStore', lambda _: store)
    monkeypatch.setattr(function_app, 'run_resumable', engine)
    monkeypatch.setattr(function_app.time, 'monotonic', lambda: 1000)
    monkeypatch.setenv('ONEDRIVE_SYNC_ENABLED', 'true')
    getattr(function_app, method)(Mock())
    assert engine.call_args.kwargs == {'start_new': start_new, 'deadline': 1180}
    store.close.assert_called_once()


def test_overlap_does_not_log_sdk_private_error(monkeypatch, caplog):
    import function_app
    store = Mock()
    error = HttpResponseError('secret-url-token-sensitive')
    error.status_code = 409
    store.locked.side_effect = error
    monkeypatch.setattr(function_app, 'BlobStore', lambda _: store)
    monkeypatch.setenv('ONEDRIVE_SYNC_ENABLED', 'true')
    with caplog.at_level('INFO'):
        function_app.onedrive_publisher(Mock())
    assert 'already active' in caplog.text
    assert 'secret-url' not in caplog.text
    store.close.assert_called_once()


def test_indexed_timer_settings_are_configurable():
    import function_app
    functions = {f.get_function_name(): json.loads(f.get_function_json()) for f in function_app.app.get_functions()}
    assert set(functions) == {'OneDrivePublisher', 'OneDriveContinuation'}
    for name, expression in [('OneDrivePublisher', '%ONEDRIVE_SYNC_SCHEDULE%'),
                             ('OneDriveContinuation', '%ONEDRIVE_CONTINUATION_SCHEDULE%')]:
        trigger = next(b for b in functions[name]['bindings'] if b['type'] == 'timerTrigger')
        assert trigger['schedule'] == expression
        assert trigger['runOnStartup'] is False and trigger['useMonitor'] is True
