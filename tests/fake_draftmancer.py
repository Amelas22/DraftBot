"""A Draftmancer stand-in that enforces the rules the real server enforces.

The bugs this exists for are not in the bot's reaction to an event -- they are in
what Draftmancer does to a *connection*, which no amount of feeding handlers
synthetic payloads can reproduce. `Connections` in Draftmancer is keyed by userID
and is GLOBAL across sessions, so who else is connected decides whether your
socket survives. Reproducing that needs a server, not a stub.

The connection rules below are a transcription of the handler in Draftmancer's
`src/server.ts` (checked out at ~/mtg/Draftmancer), and the transcription is the
point: they are a contract with another codebase, so a test that hardcodes the
bot's own assumptions proves nothing.

    if query.userID in Connections:
        emit "stillAlive" to the previous socket, wait 3s for an ack
        if it answers:
            previous session drafting -> reject this connection outright
            otherwise               -> accept it under a NEW uuid, emit
                                       "alreadyConnected" with that id
        if it does not answer:
            close the previous socket, this one takes the userID

"Answers stillAlive" is modelled as "has a stillAlive handler registered", which
is exactly what decides it for a socket.io client: an unsubscribed event gets no
ack, and the server's 3s timer expires. That makes the bot's subscription list a
real input to these tests rather than a detail.

Deliberately NOT modelled: packs, picks, seating, the draft itself. This is about
who holds a connection.
"""
import uuid


class FakeSocket:
    """One connected client. Handlers are whatever the owner registered via `on`."""

    def __init__(self, server, user_id, session_id, user_name, handlers=None):
        self._server = server
        self.user_id = user_id
        self.session_id = session_id
        self.user_name = user_name
        self.connected = True
        # Bound before the socket exists, as socket.io does it -- otherwise events
        # the server sends during the handshake ("alreadyConnected") are missed.
        self.handlers = dict(handlers or {})
        self.emitted = []          # (event, data) the client sent to the server
        self.rejected = False      # server refused the connection outright

    def on(self, name, handler):
        self.handlers[name] = handler

    def answers_still_alive(self):
        return "stillAlive" in self.handlers

    async def deliver(self, event, data=None):
        """Server -> client. Silent when unsubscribed, exactly like socket.io."""
        handler = self.handlers.get(event)
        if handler is None:
            return
        if data is None:
            await handler()
        else:
            await handler(data)


class FakeDraftmancer:
    """The server. One instance per test; `connections` is global, as in the real one."""

    def __init__(self):
        self.connections = {}       # userID -> FakeSocket   (Draftmancer's `Connections`)
        self.sessions = {}          # sessionID -> [userID]
        self.drafting = set()       # sessionIDs with a draft in progress
        self.log = []               # what the server did, for assertions

    def users_in(self, session_id):
        return list(self.sessions.get(session_id, []))

    def socket_for(self, user_id):
        return self.connections.get(user_id)

    async def connect(self, user_id, session_id, user_name, handlers=None):
        """The connection handler from src/server.ts. Returns the accepted socket,
        or None when the server refuses the connection."""
        socket = FakeSocket(self, user_id, session_id, user_name, handlers)

        previous = self.connections.get(user_id)
        if previous is not None:
            self.log.append(f"duplicate userID {user_id}")
            await previous.deliver("stillAlive")
            if previous.answers_still_alive():
                if previous.session_id in self.drafting:
                    # "previous connection still alive and in a game. Rejecting."
                    self.log.append(f"rejected {user_id}: previous connection is drafting")
                    socket.connected = False
                    socket.rejected = True
                    return None
                # "assume the user simply opened a new tab" -- new identity for it.
                new_id = f"uuid-{uuid.uuid4().hex[:8]}"
                self.log.append(f"renamed {user_id} -> {new_id}")
                socket.user_id = new_id
                user_id = new_id
                await socket.deliver("alreadyConnected", new_id)
            else:
                # The previous socket did not respond in time; close it.
                self.log.append(f"evicted {user_id} from session {previous.session_id}")
                await self._drop(previous)

        self.connections[user_id] = socket
        self.sessions.setdefault(session_id, [])
        if user_id not in self.sessions[session_id]:
            self.sessions[session_id].append(user_id)
        await socket.deliver("connect")
        await self._broadcast_users(session_id)
        return socket

    async def _drop(self, socket):
        socket.connected = False
        self.connections.pop(socket.user_id, None)
        members = self.sessions.get(socket.session_id)
        if members and socket.user_id in members:
            members.remove(socket.user_id)
        await socket.deliver("disconnect")
        await self._broadcast_users(socket.session_id)

    async def disconnect(self, socket):
        if socket.connected:
            await self._drop(socket)

    async def _broadcast_users(self, session_id):
        payload = [
            {"userID": uid, "userName": self.connections[uid].user_name}
            for uid in self.sessions.get(session_id, [])
            if uid in self.connections
        ]
        for uid in list(self.sessions.get(session_id, [])):
            socket = self.connections.get(uid)
            if socket:
                await socket.deliver("sessionUsers", payload)

    def add_squatter(self, user_id, session_id="DBSQUATTER", answers=True):
        """Someone else already holding `user_id`, e.g. a stale connection from a
        previous bot process. `answers=True` puts the server on its rename branch."""
        socket = FakeSocket(self, user_id, session_id, user_id)
        if answers:
            async def _ack():
                return True
            socket.on("stillAlive", _ack)
        self.connections[user_id] = socket
        self.sessions.setdefault(session_id, []).append(user_id)
        return socket


class FakeSocketClient:
    """Drop-in for DraftSocketClient, wired to a FakeDraftmancer.

    Mirrors only what DraftSetupManager uses: `sio.on`, `connected`,
    `connect_with_retry`, `disconnect`, `emit`.
    """

    def __init__(self, server, resource_id=""):
        self._server = server
        self.resource_id = resource_id
        self.socket = None
        self.sio = self                 # the manager registers via socket_client.sio.on
        self._pending_handlers = {}

    def on(self, name, handler):
        self._pending_handlers[name] = handler
        if self.socket:
            self.socket.on(name, handler)

    @property
    def connected(self):
        return bool(self.socket and self.socket.connected)

    async def connect_with_retry(self, url, max_retries=5, base_delay=2):
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(url).query)
        socket = await self._server.connect(
            user_id=query["userID"][0],
            session_id=query["sessionID"][0],
            user_name=query["userName"][0],
            handlers=self._pending_handlers,
        )
        if socket is None:
            self.socket = None
            return False
        self.socket = socket
        return True

    async def disconnect(self):
        if self.socket:
            await self._server.disconnect(self.socket)

    async def emit(self, event, data=None, callback=None):
        if not self.connected:
            return False
        self.socket.emitted.append((event, data))
        return True


def attach(manager, server):
    """Point a real DraftSetupManager at the fake server, handlers and all."""
    manager.socket_client = FakeSocketClient(server, resource_id=f"DB{manager.draft_id}")
    manager._register_socket_handlers()
    return manager.socket_client
