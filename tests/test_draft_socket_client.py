import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from services.draft_socket_client import DraftSocketClient
import socketio

@pytest.fixture
def mock_socketio():
    with patch('services.draft_socket_client.socketio.AsyncClient') as mock:
        yield mock

@pytest.mark.asyncio
async def test_draft_socket_client_initialization(mock_socketio):
    client = DraftSocketClient("test_id")
    assert client.resource_id == "test_id"
    assert client.sio == mock_socketio.return_value

@pytest.mark.asyncio
async def test_draft_socket_client_connect_success(mock_socketio):
    client = DraftSocketClient("test_id")
    client.sio.connected = False
    client.sio.connect = AsyncMock()
    
    result = await client.connect_with_retry("http://test.url")
    
    assert result is True
    client.sio.connect.assert_called_once_with("http://test.url", transports='websocket', wait_timeout=10)

@pytest.mark.asyncio
async def test_draft_socket_client_already_connected(mock_socketio):
    client = DraftSocketClient("test_id")
    client.sio.connected = True
    client.sio.connect = AsyncMock()
    
    result = await client.connect_with_retry("http://test.url")
    
    assert result is True
    client.sio.connect.assert_not_called()

@pytest.mark.asyncio
async def test_draft_socket_client_retry_logic(mock_socketio):
    client = DraftSocketClient("test_id")
    client.sio.connected = False
    # Fail twice, then succeed
    client.sio.connect = AsyncMock(side_effect=[
        socketio.exceptions.ConnectionError("Fail 1"),
        socketio.exceptions.ConnectionError("Fail 2"),
        None
    ])
    
    # Use small delay for test speed
    result = await client.connect_with_retry("http://test.url", base_delay=0.01)
    
    assert result is True
    assert client.sio.connect.call_count == 3

@pytest.mark.asyncio
async def test_draft_socket_client_max_retries_exceeded(mock_socketio):
    client = DraftSocketClient("test_id")
    client.sio.connected = False
    client.sio.connect = AsyncMock(side_effect=socketio.exceptions.ConnectionError("Fail"))
    
    result = await client.connect_with_retry("http://test.url", max_retries=3, base_delay=0.01)
    
    assert result is False
    assert client.sio.connect.call_count == 3

@pytest.mark.asyncio
async def test_draft_socket_client_emit_connected(mock_socketio):
    client = DraftSocketClient("test_id")
    client.sio.connected = True
    client.sio.emit = AsyncMock()

    result = await client.emit("test_event", {"data": 1})

    assert result is True
    client.sio.emit.assert_called_once_with("test_event", {"data": 1}, callback=None)


@pytest.mark.asyncio
async def test_draft_socket_client_emit_with_callback(mock_socketio):
    client = DraftSocketClient("test_id")
    client.sio.connected = True
    client.sio.emit = AsyncMock()

    callback_fn = AsyncMock()
    result = await client.emit("test_event", {"data": 1}, callback=callback_fn)

    assert result is True
    client.sio.emit.assert_called_once_with("test_event", {"data": 1}, callback=callback_fn)

@pytest.mark.asyncio
async def test_draft_socket_client_emit_disconnected(mock_socketio):
    client = DraftSocketClient("test_id")
    client.sio.connected = False
    client.sio.emit = AsyncMock()
    
    result = await client.emit("test_event", {})
    
    assert result is False
    client.sio.emit.assert_not_called()

@pytest.mark.asyncio
async def test_draft_socket_client_disconnect(mock_socketio):
    client = DraftSocketClient("test_id")
    client.sio.connected = True
    client.sio.disconnect = AsyncMock()
    
    await client.disconnect()
    
    client.sio.disconnect.assert_called_once()
