"""The disposable control channel retries reads, never admitted operations."""
from pathlib import Path

import pytest

import installed_recovery_control_channel as channel


def test_transient_sharing_denial_retries_before_the_request_is_returned(tmp_path, monkeypatch):
    request = tmp_path / 'command.request.json'
    payload = b'{"operation":"start"}'
    request.write_bytes(payload)
    read = Path.read_bytes
    calls = 0
    def busy_then_read(path):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError('temporary sharing denial')
        return read(path)
    monkeypatch.setattr(Path, 'read_bytes', busy_then_read)
    assert channel._read_request_bytes(request) == payload
    assert calls == 3 and read(request) == payload


def test_permanent_read_denial_is_bounded_and_other_errors_are_not_retried(tmp_path, monkeypatch):
    def denied(_path):
        raise PermissionError('still denied')
    monkeypatch.setattr(Path, 'read_bytes', denied)
    monkeypatch.setattr(channel.time, 'sleep', lambda _: pytest.fail('expired read slept'))
    with pytest.raises(PermissionError, match='still denied'):
        channel._read_request_bytes(tmp_path / 'request', max_seconds=0)
    def missing(_path):
        raise FileNotFoundError('missing request')
    monkeypatch.setattr(Path, 'read_bytes', missing)
    with pytest.raises(FileNotFoundError, match='missing request'):
        channel._read_request_bytes(tmp_path / 'request')


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows share-mode reproduction')
def test_real_windows_exclusive_handle_can_be_released_without_reissuing_request(tmp_path):
    import ctypes
    from ctypes import wintypes
    from concurrent.futures import ThreadPoolExecutor

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    request = tmp_path / 'request.json'
    request.write_bytes(b'one immutable request')
    handle = kernel.CreateFileW(str(request), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        with pytest.raises(PermissionError):
            request.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(channel._read_request_bytes, request)
            channel.time.sleep(0.05)
            assert not result.done()
            assert kernel.CloseHandle(handle)
            handle = None
            assert result.result(timeout=6) == b'one immutable request'
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
