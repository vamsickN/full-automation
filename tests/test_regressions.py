import os


def test_store_init_uses_character_media_alias(tmp_path):
    import store

    original_data_dir = store.DATA_DIR
    try:
        store.DATA_DIR = str(tmp_path)
        store.init()
        assert hasattr(store, 'CHARS_DIR')
        assert (tmp_path / 'characters').is_dir()
        assert (tmp_path / 'projects').is_dir()
    finally:
        store.DATA_DIR = original_data_dir
        store._refresh_compat_paths()


def test_data_path_guard_rejects_parent_escape_and_encoded_dot_segments(tmp_path, monkeypatch):
    import runtime_security
    import store

    monkeypatch.setattr(runtime_security.config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(runtime_security.config, 'AUTH_REQUIRED', True)
    email = 'alice@example.com'
    sid = store.scope_id(email)
    os.makedirs(tmp_path / 'users' / sid / 'images')

    assert runtime_security._canonical_data_path_allowed(
        f'/data/users/{sid}/images/frame.png', email)
    assert not runtime_security._canonical_data_path_allowed(
        f'/data/users/{sid}/../../.secret', email)
    assert not runtime_security._canonical_data_path_allowed(
        f'/data/users/{sid}/%2e%2e/%2e%2e/.secret', email)


def test_data_path_guard_rejects_other_tenant(tmp_path, monkeypatch):
    import runtime_security
    import store

    monkeypatch.setattr(runtime_security.config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(runtime_security.config, 'AUTH_REQUIRED', True)
    alice = 'alice@example.com'
    bob_sid = store.scope_id('bob@example.com')
    os.makedirs(tmp_path / 'users' / bob_sid / 'images')

    assert not runtime_security._canonical_data_path_allowed(
        f'/data/users/{bob_sid}/images/frame.png', alice)
