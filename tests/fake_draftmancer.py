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

    def __init__(self, user_id, session_id, user_name, handlers=None):
        self.user_id = user_id
        self.session_id = session_id
        self.user_name = user_name
        self.connected = True
        # The client's own dict, not a copy: socket.io keeps handlers on the client
        # across reconnects, and they must already be bound when the server sends
        # "alreadyConnected" during the handshake.
        self.handlers = handlers if handlers is not None else {}

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
        self.drafting = set()       # sessionIDs with a draft in progress
        self.owners = {}            # sessionID -> userID
        # Owners who declared themselves non-players. server.ts:585 deletes them
        # from `sess.users`, and sessionUsers is built from users + disconnectedUsers
        # (Session.ts:3747), so they are absent from that payload while still being
        # connected. Modelling this matters: the bot makes itself one.
        self.non_playing_owners = set()
        self.log = []               # what the server did, for assertions

    def users_in(self, session_id):
        """Derived, not mirrored: a second map would need a guard to stay honest,
        and this file has to read as an obviously correct transcription."""
        return [uid for uid, s in self.connections.items() if s.session_id == session_id]

    def session_user_payload(self, session_id):
        """What Draftmancer puts in `sessionUsers`: human players only."""
        return [{"userID": uid, "userName": self.connections[uid].user_name}
                for uid in self.users_in(session_id)
                if uid not in self.non_playing_owners]

    def session_of(self, user_id):
        socket = self.connections.get(user_id)
        return socket.session_id if socket else None

    def socket_for(self, user_id):
        return self.connections.get(user_id)

    async def connect(self, user_id, session_id, user_name, handlers=None):
        """The connection handler from src/server.ts. Returns the accepted socket,
        or None when the server refuses the connection."""
        socket = FakeSocket(user_id, session_id, user_name, handlers)

        previous = self.connections.get(user_id)
        if previous is not None:
            self.log.append(f"duplicate userID {user_id}")
            await previous.deliver("stillAlive")
            if previous.answers_still_alive():
                if previous.session_id in self.drafting:
                    # "previous connection still alive and in a game. Rejecting."
                    self.log.append(f"rejected {user_id}: previous connection is drafting")
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
        await socket.deliver("connect")
        await self._broadcast_users(session_id)
        return socket

    async def _drop(self, socket):
        socket.connected = False
        self.connections.pop(socket.user_id, None)
        await socket.deliver("disconnect")
        await self._broadcast_users(socket.session_id)

    async def disconnect(self, socket):
        if socket.connected:
            await self._drop(socket)

    async def _broadcast_users(self, session_id):
        payload = self.session_user_payload(session_id)
        for uid in self.users_in(session_id):
            await self.connections[uid].deliver("sessionUsers", payload)

    async def handle_emit(self, socket, event, data):
        """The client->server calls the bot actually makes. Everything else is inert."""
        if event == "setSessionOwner":
            self.owners[socket.session_id] = data
            self.log.append(f"owner of {socket.session_id} is {data}")
        elif event == "setOwnerIsPlayer":
            owner = self.owners.get(socket.session_id)
            if owner is None:
                return
            if data:
                self.non_playing_owners.discard(owner)
            else:
                # server.ts:585 -- the owner leaves `sess.users` and so vanishes
                # from every subsequent sessionUsers payload.
                self.non_playing_owners.add(owner)
            await self._broadcast_users(socket.session_id)

    def add_squatter(self, user_id, session_id="DBSQUATTER", answers=True):
        """Someone else already holding `user_id`, e.g. a stale connection from a
        previous bot process. `answers=True` puts the server on its rename branch."""
        socket = FakeSocket(user_id, session_id, user_id)
        if answers:
            async def _ack():
                return True
            socket.on("stillAlive", _ack)
        self.connections[user_id] = socket
        return socket


class FakeSocketClient:
    """Drop-in for DraftSocketClient, wired to a FakeDraftmancer.

    Mirrors only what DraftSetupManager uses: `sio.on`, `connected`,
    `connect_with_retry`, `disconnect`, `emit`.
    """

    def __init__(self, server):
        self._server = server
        self.socket = None
        self.sio = self                 # the manager registers via socket_client.sio.on
        self.handlers = {}              # shared with the socket, as socket.io does it

    def on(self, name, handler):
        self.handlers[name] = handler

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
            handlers=self.handlers,
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
        await self._server.handle_emit(self.socket, event, data)
        return True


def attach(manager, server):
    """Point a real DraftSetupManager at the fake server, handlers and all."""
    manager.socket_client = FakeSocketClient(server)
    manager._register_socket_handlers()
    return manager.socket_client


async def connect(manager, server):
    """Take a manager through its real connect path onto the fake server."""
    attach(manager, server)
    return await manager.connect_to_new_session()
