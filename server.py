import socket
import selectors
import json
import struct
import signal
import sys
import time
import os
import argparse
import zlib
import uuid
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from game_logic import Game
import logging
from datetime import datetime

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

DEFAULT_PORT = 65432
HOST = '0.0.0.0'
MAX_ROOMS = 5
MAX_PLAYERS_PER_ROOM = 4


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except socket.error as e:
        logger.error(f"Failed to get local IP: {e}")
        return "Unknown"


@dataclass
class Player:
    sock: socket.socket
    name: str
    slot: int


class GameRoom:
    def __init__(self, room_id: str, room_name: str, creator: Player, max_players: int = MAX_PLAYERS_PER_ROOM):
        self.room_id = room_id
        self.room_name = room_name
        self.creator = creator
        self.max_players = max_players
        self.game: Optional[Game] = None
        self.game_ended: bool = False
        self.players: List[Optional[Player]] = [None] * self.max_players
        self.player_names: Dict[int, str] = {}
        self.sockets: Set[socket.socket] = set()
        self.finish_order: List[int] = []
        self.disconnected: Set[int] = set()
        self.last_game_state: Optional[dict] = None
        self.created_at = datetime.now()

        self._add_player(creator)

    def _add_player(self, player: Player) -> bool:
        slot = next((i for i, p in enumerate(self.players) if p is None), None)
        if slot is None:
            return False
        self.players[slot] = player
        self.player_names[slot] = player.name
        self.sockets.add(player.sock)
        return True

    def remove_player(self, sock: socket.socket) -> bool:
        for i, player in enumerate(self.players):
            if player and player.sock == sock:
                if self.game and self.game.players[i]:
                    print(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Moving Player {i + 1}'s {len(self.game.players[i])} cards to discard pile in room {self.room_name}")
                    self.game.discard_pile.extendleft(self.game.players[i])
                    self.game.players[i].clear()
                    self.disconnected.add(i)

                    if self.game.current_player == i:
                        self.game.next_turn()

                self.players[i] = None
                self.sockets.discard(sock)
                return True
        return False

    def is_empty(self) -> bool:
        return all(p is None for p in self.players)

    def is_full(self) -> bool:
        return all(p is not None for p in self.players)

    def can_start_game(self) -> bool:
        return len([p for p in self.players if p]) == self.max_players and self.game is None and not self.game_ended

    def start_game(self):
        if self.can_start_game():
            # Create Game with the room's max_players so in-game player lists match the room size
            self.game = Game(self.max_players)
            self.game.create_deck()
            self.game.deal_cards()
            self.finish_order = []
            self.disconnected = set()
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Game started in room '{self.room_name}' with {len([p for p in self.players if p])} players")
            return True
        return False

    def get_room_info(self) -> dict:
        return {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "creator": self.creator.name,
            "players": len([p for p in self.players if p]),
            "max_players": self.max_players,
            "in_game": self.game is not None or self.game_ended,
            "created_at": self.created_at.strftime('%H:%M:%S')
        }


class LobbyManager:
    def __init__(self):
        self.clients: Set[socket.socket] = set()
        # keep mapping of sockets -> name if needed, but server already tracks names
        self.client_names: Dict[socket.socket, str] = {}
        # map creator_name -> bool to track if a player (by name) already created a room
        self.player_room_created: Dict[str, bool] = {}

    def add_client(self, sock: socket.socket):
        self.clients.add(sock)
        # do not touch player_room_created here (it's keyed by creator name)

    def remove_client(self, sock: socket.socket):
        self.clients.discard(sock)
        self.client_names.pop(sock, None)
        # do not pop player_room_created here (we clear it when the actual room closes)

    def broadcast(self, message: dict, exclude_sock: Optional[socket.socket] = None, server: 'MultiRoomServer' = None):
        if server is None:
            return
        failed_sockets = []
        for sock in list(self.clients):
            if sock != exclude_sock:
                if not server.send_message(sock, message):
                    failed_sockets.append(sock)
        for sock in failed_sockets:
            self.remove_client(sock)
            server._remove_client(sock)

    def send_room_list(self, sock: socket.socket, rooms: Dict[str, GameRoom], server: 'MultiRoomServer'):
        if server is None:
            return
        rooms_info = [room.get_room_info() for room in rooms.values() if not room.game and not room.game_ended]
        server.send_message(sock, {
            "t": "room_list",
            "rooms": rooms_info,
            "max_rooms": MAX_ROOMS,
            "current_rooms": len(rooms)
        })


class RoomManager:
    def __init__(self):
        self.rooms: Dict[str, GameRoom] = {}
        self.client_rooms: Dict[socket.socket, str] = {}

    def create_room(self, sock: socket.socket, room_name: str, creator_name: str, max_players: int = MAX_PLAYERS_PER_ROOM) -> Optional[str]:
        if len(self.rooms) >= MAX_ROOMS:
            return None
        if not (2 <= max_players <= MAX_PLAYERS_PER_ROOM):
            return None
        room_id = str(uuid.uuid4())
        creator = Player(sock, creator_name, 0)
        room = GameRoom(room_id, room_name, creator, max_players)
        self.rooms[room_id] = room
        self.client_rooms[sock] = room_id
        return room_id

    def join_room(self, sock: socket.socket, room_id: str, player_name: str) -> Optional[int]:
        if room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        if room.is_full() or room.game or room.game_ended:
            return None
        slot = next((i for i, p in enumerate(room.players) if p is None), None)
        if slot is None:
            return None
        player = Player(sock, player_name, slot)
        room.players[slot] = player
        room.player_names[slot] = player_name
        room.sockets.add(sock)
        self.client_rooms[sock] = room_id
        return slot

    def leave_room(self, sock: socket.socket, server: 'MultiRoomServer', lobby: LobbyManager) -> None:
        if sock in self.client_rooms:
            room_id = self.client_rooms[sock]
            if room_id in self.rooms:
                room = self.rooms[room_id]
                player_name = server.client_names.get(sock, "Unknown")
                room.remove_player(sock)
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {player_name} left room '{room.room_name}'")

                # Broadcast player_left to everyone in the room (including the leaving player)
                self.broadcast_to_room(room_id, {
                    "t": "player_left",
                    "player_name": player_name,
                    "players_count": len([p for p in room.players if p])
                }, exclude_sock=None, server=server)

                if room.game:
                    game_state = room.game.serialize()
                    self.broadcast_to_room(room_id, {"t": "gs", **game_state, "player_names": room.player_names}, exclude_sock=None, server=server)

                # If the room becomes empty, clear creator flag (by creator name) and delete the room
                if room.is_empty():
                    try:
                        creator_name = room.creator.name
                        server.lobby.player_room_created.pop(creator_name, None)
                    except Exception:
                        pass

                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Closing empty room: {room.room_name}")
                    del self.rooms[room_id]

            self.client_rooms.pop(sock, None)

        lobby.add_client(sock)
        server.send_message(sock, {"t": "back_to_lobby"})
        lobby.send_room_list(sock, self.rooms, server)
        lobby.broadcast(
            {"t": "room_list_update", "rooms": [r.get_room_info() for r in self.rooms.values() if not r.game and not r.game_ended]},
            server=server)

    def broadcast_to_room(self, room_id: str, message: dict, exclude_sock: Optional[socket.socket] = None,
                          server: 'MultiRoomServer' = None):
        if room_id not in self.rooms or server is None:
            return
        room = self.rooms[room_id]
        failed_sockets = []
        for sock in list(room.sockets):
            if sock != exclude_sock:
                if not server.send_message(sock, message):
                    failed_sockets.append(sock)
        for sock in failed_sockets:
            room.remove_player(sock)
            self.client_rooms.pop(sock, None)

    def get_available_rooms_info(self) -> List[dict]:
        return [room.get_room_info() for room in self.rooms.values() if not room.game and not room.game_ended]

    def end_game(self, room_id: str, server: 'MultiRoomServer', lobby: LobbyManager) -> None:
        if room_id not in self.rooms:
            return
        room = self.rooms[room_id]
        if not room.game:
            return

        winner = next((i for i, p in enumerate(room.game.players) if not p), None)

        results = []
        for pid in room.finish_order:
            results.append(
                {"pid": pid, "rank": len(results) + 1, "cards_left": 0, "disconnected": pid in room.disconnected})

        remaining = [(i, len(room.game.players[i])) for i in range(room.max_players) if i not in room.finish_order]
        remaining.sort(key=lambda x: x[1])
        for pid, cards_left in remaining:
            results.append({"pid": pid, "rank": len(results) + 1, "cards_left": cards_left,
                            "disconnected": pid in room.disconnected})

        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Game over in room '{room.room_name}', winner: Player {winner + 1 if winner is not None else 'none'}")

        self.broadcast_to_room(room_id, {
            "t": "go",
            "w": winner + 1 if winner is not None else None,
            "results": results,
            "player_names": room.player_names
        }, server=server)

        room.game = None
        room.game_ended = True
        room.last_game_state = None
        room.finish_order = []


class MessageHandler:
    def __init__(self, lobby: LobbyManager, rooms: RoomManager, server: 'MultiRoomServer'):
        self.lobby = lobby
        self.rooms = rooms
        self.server = server

    def handle_lobby_message(self, sock: socket.socket, message: dict) -> None:
        msg_type = message.get("t")

        if msg_type == "set_name":
            player_name = message.get("name", "").strip()
            if not (3 <= len(player_name) <= 20):
                self.server.send_message(sock, {"t": "e", "msg": "Name must be 3-20 characters long"})
                return

            if player_name in self.server.client_names.values():
                self.server.send_message(sock, {"t": "e", "msg": "Name already taken"})
                return

            self.server.client_names[sock] = player_name
            self.server.send_message(sock, {"t": "name_set", "name": player_name})
            self.lobby.send_room_list(sock, self.rooms.rooms, self.server)


        elif msg_type == "create_room":

            if sock not in self.server.client_names:
                self.server.send_message(sock, {"t": "e", "msg": "Please set your name first"})

                return

            creator_name = self.server.client_names[sock]

            if self.server.lobby.player_room_created.get(creator_name, False):
                self.server.send_message(sock, {"t": "e", "msg": "You can only create one room"})

                return

            room_name = message.get("room_name", "").strip()

            if not (3 <= len(room_name) <= 30):
                self.server.send_message(sock, {"t": "e", "msg": "Room name must be 3-30 characters long"})

                return

            # Read and validate requested max players (default to server constant)

            try:

                max_players = int(message.get("max_players", MAX_PLAYERS_PER_ROOM))

            except (ValueError, TypeError):

                max_players = MAX_PLAYERS_PER_ROOM

            if not (2 <= max_players <= MAX_PLAYERS_PER_ROOM):
                self.server.send_message(sock,
                                         {"t": "e", "msg": f"max_players must be between 2 and {MAX_PLAYERS_PER_ROOM}"})

                return

            room_id = self.rooms.create_room(sock, room_name, creator_name, max_players)

            if room_id:

                self.server.lobby.remove_client(sock)

                self.server.lobby.player_room_created[creator_name] = True

                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Room '{room_name}' ({max_players} players) created by {creator_name}")

                self.server.send_message(sock, {

                    "t": "room_joined",

                    "room_id": room_id,

                    "room_name": room_name,

                    "player_slot": 0,

                    "max_players": max_players

                })

                self.lobby.broadcast({"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()}, sock,
                                     self.server)

            else:

                self.server.send_message(sock, {"t": "e",
                                                "msg": f"Failed to create room (max {MAX_ROOMS} rooms or invalid parameters)"})


        elif msg_type == "join_room":

            if sock not in self.server.client_names:
                self.server.send_message(sock, {"t": "e", "msg": "Please set your name first"})

                return

            room_id = message.get("room_id")

            slot = self.rooms.join_room(sock, room_id, self.server.client_names[sock])

            if slot is not None:

                self.server.lobby.remove_client(sock)

                player_name = self.server.client_names[sock]

                room = self.rooms.rooms[room_id]

                players_count = len([p for p in room.players if p])

                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {player_name} joined room '{room.room_name}' as Player {slot + 1}")

                self.server.send_message(sock, {

                    "t": "room_joined",

                    "room_id": room_id,

                    "room_name": room.room_name,

                    "player_slot": slot,

                    "max_players": room.max_players

                })

                self.rooms.broadcast_to_room(room_id, {

                    "t": "player_joined",

                    "player_name": player_name,

                    "player_slot": slot,

                    "players_count": players_count

                }, exclude_sock=None, server=self.server)

                if room.can_start_game():

                    room.start_game()

                    self._broadcast_game_state(room_id)

                else:

                    players_needed = room.max_players - players_count

                    self.rooms.broadcast_to_room(room_id, {

                        "t": "waiting",

                        "players_needed": players_needed

                    }, exclude_sock=None, server=self.server)

                self.lobby.broadcast({"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()},

                                     server=self.server)

            else:

                self.server.send_message(sock, {"t": "e", "msg": "Failed to join room"})

        elif msg_type == "refresh_rooms":
            self.lobby.send_room_list(sock, self.rooms.rooms, self.server)

    def handle_room_message(self, sock: socket.socket, message: dict) -> None:
        room_id = self.rooms.client_rooms.get(sock)
        if room_id not in self.rooms.rooms:
            return

        room = self.rooms.rooms[room_id]
        msg_type = message.get("t")

        if msg_type == "leave_room":
            self.rooms.leave_room(sock, self.server, self.lobby)

        elif room.game:
            player_slot = next((p.slot for p in room.players if p and p.sock == sock), -1)

            if msg_type == "p" and player_slot == room.game.current_player:
                card_index = message.get("ci", -1)
                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Player {player_slot + 1} attempting to play card {card_index} in room '{room.room_name}'")

                if room.game.play_card(player_slot, card_index):
                    print(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Player {player_slot + 1} played card successfully in room '{room.room_name}'")

                    if not room.game.players[player_slot] and player_slot not in room.finish_order:
                        room.finish_order.append(player_slot)
                        print(
                            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Player {player_slot + 1} finished in room '{room.room_name}'")

                    self._broadcast_game_state(room_id)
                else:
                    self.server.send_message(sock, {"t": "e", "msg": "Invalid card play"})

            elif msg_type == "d" and player_slot == room.game.current_player:
                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Player {player_slot + 1} drawing card in room '{room.room_name}'")

                if room.game.draw_card(player_slot):
                    room.game.next_turn()
                    self._broadcast_game_state(room_id)
                else:
                    self.server.send_message(sock, {"t": "e", "msg": "Cannot draw card: draw pile empty"})
            else:
                self.server.send_message(sock, {"t": "e", "msg": "Invalid action or not your turn"})

    def _broadcast_game_state(self, room_id: str) -> None:
        room = self.rooms.rooms[room_id]
        if not room.game:
            return

        current_state = room.game.serialize()
        current_state["player_names"] = room.player_names
        print("[SERVER DEBUG GS] Broadcasting to room '{room.room_name}': player_names =", room.player_names)
        print("[SERVER DEBUG GS] Full current_state keys:", list(current_state.keys()))
        room.last_game_state = current_state
        self.rooms.broadcast_to_room(room_id, {"t": "gs", **current_state}, server=self.server)


class MultiRoomServer:
    def __init__(self, port: int):
        self.sel = selectors.DefaultSelector()
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind((HOST, port))
        except socket.error as e:
            logger.error(f"Failed to bind to {HOST}:{port}: {e}")
            sys.exit(1)

        self.server_socket.listen(20)
        self.server_socket.setblocking(False)
        self.sel.register(self.server_socket, selectors.EVENT_READ, self._accept_client)

        self.lobby = LobbyManager()
        self.rooms = RoomManager()
        self.message_handler = MessageHandler(self.lobby, self.rooms, self)
        self.client_names: Dict[socket.socket, str] = {}

        signal.signal(signal.SIGINT, self._signal_handler)
        local_ip = get_local_ip()
        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Multi-room server started on {local_ip}:{port} and localhost:{port}")

    def _signal_handler(self, sig, frame):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Shutting down multi-room server...")
        for room in self.rooms.rooms.values():
            for sock in room.sockets:
                try:
                    sock.close()
                except:
                    pass
        for sock in self.lobby.clients:
            try:
                sock.close()
            except:
                pass
        self.server_socket.close()
        self.sel.close()
        sys.exit(0)

    def send_message(self, sock: socket.socket, message: dict, retries: int = 2) -> bool:
        try:
            data = zlib.compress(json.dumps(message, separators=(',', ':')).encode())
            sock.sendall(struct.pack('!I', len(data)) + data)
            return True
        except socket.error as e:
            client_name = self.client_names.get(sock, 'unknown')
            logger.error(f"Send error to {client_name}: {e}")
            if retries > 0:
                time.sleep(0.1)
                return self.send_message(sock, message, retries - 1)
            return False

    def receive_message(self, sock: socket.socket) -> Optional[dict]:
        try:
            length_data = sock.recv(4)
            if not length_data:
                return None
            length = struct.unpack('!I', length_data)[0]
            data = b""
            while len(data) < length:
                packet = sock.recv(length - len(data))
                if not packet:
                    return None
                data += packet
            message = json.loads(zlib.decompress(data).decode())
            return message
        except (socket.error, json.JSONDecodeError, struct.error, zlib.error) as e:
            logger.error(f"Receive error: {e}")
            return None

    def start(self) -> None:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Server ready for connections")
        while True:
            events = self.sel.select(timeout=0.1)
            for key, mask in events:
                callback = key.data
                callback(key.fileobj, mask)

            for room_id in list(self.rooms.rooms.keys()):
                room = self.rooms.rooms[room_id]
                if room.game:
                    current_player = room.game.current_player
                    if len(room.game.players[current_player]) == 0 and not room.game.check_game_over():
                        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Auto-skipping turn for disconnected Player {current_player + 1} in room '{room.room_name}'")
                        room.game.next_turn()
                        self.message_handler._broadcast_game_state(room_id)

                    if room.game.check_game_over():
                        self.rooms.end_game(room_id, self, self.lobby)

                    if room.is_empty():
                        print(
                            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Removing empty room: {room.room_name}")
                        del self.rooms.rooms[room_id]

    def _accept_client(self, sock: socket.socket, mask: int) -> None:
        try:
            client_sock, addr = sock.accept()
            client_sock.setblocking(False)
            self.sel.register(client_sock, selectors.EVENT_READ, self._handle_client)

            client_ip = addr[0]
            client_display = f"localhost:{addr[1]}" if client_ip in ['127.0.0.1', 'localhost', '::1'] else f"{addr[0]}:{addr[1]}"

            self.lobby.add_client(client_sock)

            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] New client connected from {client_display}")

            self.send_message(client_sock, {
                "t": "lobby_welcome",
                "msg": "Welcome! Enter your name to continue."
            })

        except socket.error as e:
            logger.error(f"Accept error: {e}")

    def _handle_client(self, sock: socket.socket, mask: int) -> None:
        message = self.receive_message(sock)
        if message is None:
            self._remove_client(sock)
            return

        try:
            if sock in self.lobby.clients:
                self.message_handler.handle_lobby_message(sock, message)
            elif sock in self.rooms.client_rooms:
                self.message_handler.handle_room_message(sock, message)
        except Exception as e:
            logger.error(f"Error handling client message: {e}")
            self.send_message(sock, {"t": "e", "msg": f"Server error: {str(e)}"})

    def _remove_client(self, sock: socket.socket) -> None:
        client_name = self.client_names.get(sock, "Unknown")

        try:
            addr = sock.getpeername()
            client_display = f"{addr[0]}:{addr[1]}"
        except:
            client_display = "unknown"

        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Client {client_name} disconnected from {client_display}")

        if sock in self.rooms.client_rooms:
            self.rooms.leave_room(sock, self, self.lobby)

        self.lobby.remove_client(sock)
        self.client_names.pop(sock, None)
        try:
            sock.close()
            self.sel.unregister(sock)
        except:
            pass

        self.lobby.broadcast({"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()}, server=self)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-room Sedma bere tri server")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", DEFAULT_PORT)), help="Port to listen on")
    args = parser.parse_args()

    server = MultiRoomServer(args.port)
    server.start()