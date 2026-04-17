"""
Sedma Bere Tri - Dedicated Public Server
Standalone server designed for continuous 24/7 operation.

Features:
- File-based logging (server.log) + console output
- Automatic cleanup of stale/empty rooms
- Periodic stats reporting
- Graceful shutdown handling
- Auto-restart on crash
"""

import socket
import selectors
import json
import struct
import signal
import sys
import os
import zlib
import uuid
import time
import logging
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from datetime import datetime
from game_logic import Game

# --- Configuration ---
PORT = 65432
HOST = '0.0.0.0'
MAX_ROOMS = 10
MAX_PLAYERS_PER_ROOM = 4
STATS_INTERVAL = 300
ROOM_TIMEOUT = 3600
EMPTY_ROOM_TIMEOUT = 60


def setup_logging() -> logging.Logger:
    """Configure dual logging: rotating file + console."""
    log = logging.getLogger("dedicated_server")
    log.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        "server.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_format = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_format)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_format)

    log.addHandler(file_handler)
    log.addHandler(console_handler)
    return log


logger = setup_logging()


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except socket.error:
        return "0.0.0.0"


def get_public_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(("ifconfig.co", 80))
            request = "GET /ip HTTP/1.1\r\nHost: ifconfig.co\r\nConnection: close\r\n\r\n"
            s.sendall(request.encode())
            response = b""
            while True:
                data = s.recv(1024)
                if not data:
                    break
                response += data
        return response.decode().split("\r\n")[-1].strip()
    except Exception:
        return "Unknown"


@dataclass
class Player:
    sock: socket.socket
    name: str
    slot: int


class GameRoom:
    def __init__(self, room_id: str, room_name: str, creator: Player,
                 max_players: int = MAX_PLAYERS_PER_ROOM, rules: dict = None,
                 is_private: bool = False, password: str = None):
        self.room_id = room_id
        self.room_name = room_name
        self.creator = creator
        self.max_players = max_players
        self.rules = rules or {}
        self.is_private = is_private
        self.password = password
        self.game: Optional[Game] = None
        self.game_ended: bool = False
        self.players: List[Optional[Player]] = [None] * self.max_players
        self.player_names: Dict[int, str] = {}
        self.sockets: Set[socket.socket] = set()
        self.finish_order: List[int] = []
        self.disconnected: Set[int] = set()
        self.last_game_state: Optional[dict] = None
        self.created_at = datetime.now()
        self.last_activity = time.time()

        self._add_player(creator)

    def _add_player(self, player: Player) -> bool:
        slot = next((i for i, p in enumerate(self.players) if p is None), None)
        if slot is None:
            return False
        self.players[slot] = player
        self.player_names[slot] = player.name
        self.sockets.add(player.sock)
        self.last_activity = time.time()
        return True

    def remove_player(self, sock: socket.socket) -> bool:
        """Remove a player from the room.

        If a game is running and this player was the current player, their turn
        is ended cleanly so the game can continue.  Cards are discarded and the
        slot is cleared regardless.
        """
        for i, player in enumerate(self.players):
            if player and player.sock == sock:
                if self.game and self.game.players[i]:
                    logger.info(
                        f"Moving Player {i + 1}'s {len(self.game.players[i])} "
                        f"cards to discard in '{self.room_name}'"
                    )
                    # Discard the disconnecting player's hand
                    self.game.discard_pile.extendleft(self.game.players[i])
                    self.game.players[i].clear()
                    self.disconnected.add(i)

                    # If it was their turn, advance cleanly without drawing
                    if self.game.current_player == i:
                        # Force cards_played_this_turn > 0 so end_turn won't
                        # make them draw — the hand is already empty.
                        self.game.cards_played_this_turn = 1
                        self.game.end_turn(i)
                else:
                    self.player_names.pop(i, None)

                self.players[i] = None
                self.sockets.discard(sock)
                self.last_activity = time.time()
                return True
        return False

    def is_empty(self) -> bool:
        return all(p is None for p in self.players)

    def is_full(self) -> bool:
        return all(p is not None for p in self.players)

    def can_start_game(self) -> bool:
        return (len([p for p in self.players if p]) == self.max_players
                and self.game is None and not self.game_ended)

    def start_game(self) -> bool:
        if not self.can_start_game():
            return False
        self.game = Game(self.max_players, self.rules)
        self.game.create_deck()
        self.game.deal_cards()
        self.finish_order = []
        self.disconnected = set()
        self.last_activity = time.time()
        player_count = len([p for p in self.players if p])
        logger.info(f"Game started in '{self.room_name}' with {player_count} players")
        return True

    def is_stale(self) -> bool:
        if self.is_empty():
            return time.time() - self.last_activity > EMPTY_ROOM_TIMEOUT
        return time.time() - self.last_activity > ROOM_TIMEOUT

    def get_room_info(self) -> dict:
        return {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "creator": self.creator.name,
            "players": len([p for p in self.players if p]),
            "max_players": self.max_players,
            "in_game": self.game is not None or self.game_ended,
            "created_at": self.created_at.strftime('%H:%M:%S'),
            "is_private": self.is_private
        }


class LobbyManager:
    def __init__(self):
        self.clients: Set[socket.socket] = set()
        self.client_names: Dict[socket.socket, str] = {}
        self.player_room_created: Dict[str, bool] = {}

    def add_client(self, sock: socket.socket):
        self.clients.add(sock)

    def remove_client(self, sock: socket.socket):
        self.clients.discard(sock)
        self.client_names.pop(sock, None)

    def broadcast(self, message: dict, exclude_sock: Optional[socket.socket] = None,
                  server: 'DedicatedServer' = None):
        if server is None:
            return
        failed = []
        for sock in list(self.clients):
            if sock != exclude_sock and not server.send_message(sock, message):
                failed.append(sock)
        for sock in failed:
            self.remove_client(sock)
            server._remove_client(sock)

    def send_room_list(self, sock: socket.socket, rooms: Dict[str, GameRoom],
                       server: 'DedicatedServer'):
        if server is None:
            return
        rooms_info = [room.get_room_info() for room in rooms.values()
                      if not room.game and not room.game_ended]
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

    def create_room(self, sock: socket.socket, room_name: str, creator_name: str,
                    max_players: int = MAX_PLAYERS_PER_ROOM, rules: dict = None,
                    is_private: bool = False, password: str = None) -> Optional[str]:
        if len(self.rooms) >= MAX_ROOMS:
            return None
        if not (2 <= max_players <= MAX_PLAYERS_PER_ROOM):
            return None
        room_id = str(uuid.uuid4())
        creator = Player(sock, creator_name, 0)
        room = GameRoom(room_id, room_name, creator, max_players, rules, is_private, password)
        self.rooms[room_id] = room
        self.client_rooms[sock] = room_id
        return room_id

    def join_room(self, sock: socket.socket, room_id: str, player_name: str,
                  password: str = None) -> Optional[int]:
        if room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        if room.is_private and room.password != password:
            return -1
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
        room.last_activity = time.time()
        return slot

    def leave_room(self, sock: socket.socket, server: 'DedicatedServer',
                   lobby: LobbyManager) -> None:
        if sock in self.client_rooms:
            room_id = self.client_rooms[sock]
            if room_id in self.rooms:
                room = self.rooms[room_id]
                player_name = server.client_names.get(sock, "Unknown")
                room.remove_player(sock)
                logger.info(f"{player_name} left room '{room.room_name}'")

                self.broadcast_to_room(room_id, {
                    "t": "player_left",
                    "player_name": player_name,
                    "players_count": len([p for p in room.players if p]),
                    "player_names": room.player_names
                }, server=server)

                if room.game:
                    game_state = room.game.serialize()
                    self.broadcast_to_room(room_id, {
                        "t": "gs", **game_state,
                        "player_names": room.player_names
                    }, server=server)

                if room.is_empty():
                    try:
                        server.lobby.player_room_created.pop(room.creator.name, None)
                    except Exception:
                        pass
                    logger.info(f"Closing empty room: {room.room_name}")
                    del self.rooms[room_id]

            self.client_rooms.pop(sock, None)

        lobby.add_client(sock)
        server.send_message(sock, {"t": "back_to_lobby"})
        lobby.send_room_list(sock, self.rooms, server)
        lobby.broadcast(
            {"t": "room_list_update",
             "rooms": [r.get_room_info() for r in self.rooms.values()
                       if not r.game and not r.game_ended]},
            server=server
        )

    def broadcast_to_room(self, room_id: str, message: dict,
                          exclude_sock: Optional[socket.socket] = None,
                          server: 'DedicatedServer' = None):
        if room_id not in self.rooms or server is None:
            return
        room = self.rooms[room_id]
        failed = []
        for sock in list(room.sockets):
            if sock != exclude_sock and not server.send_message(sock, message):
                failed.append(sock)
        for sock in failed:
            room.remove_player(sock)
            self.client_rooms.pop(sock, None)

    def get_available_rooms_info(self) -> List[dict]:
        return [room.get_room_info() for room in self.rooms.values()
                if not room.game and not room.game_ended]

    def cleanup_stale_rooms(self, server: 'DedicatedServer', lobby: LobbyManager) -> int:
        stale_ids = [rid for rid, room in self.rooms.items() if room.is_stale()]
        for room_id in stale_ids:
            room = self.rooms[room_id]
            logger.info(f"Cleaning up stale room: '{room.room_name}' (idle {time.time() - room.last_activity:.0f}s)")
            for player in room.players:
                if player and player.sock in self.client_rooms:
                    self.client_rooms.pop(player.sock, None)
                    lobby.add_client(player.sock)
                    server.send_message(player.sock, {"t": "back_to_lobby"})
                    lobby.send_room_list(player.sock, self.rooms, server)
            try:
                server.lobby.player_room_created.pop(room.creator.name, None)
            except Exception:
                pass
            del self.rooms[room_id]

        if stale_ids:
            lobby.broadcast(
                {"t": "room_list_update", "rooms": self.get_available_rooms_info()},
                server=server
            )
        return len(stale_ids)

    def end_game(self, room_id: str, server: 'DedicatedServer', lobby: LobbyManager) -> None:
        if room_id not in self.rooms:
            return
        room = self.rooms[room_id]
        if not room.game:
            return

        winner = next((i for i, p in enumerate(room.game.players) if not p), None)

        results = []
        for pid in room.finish_order:
            results.append({
                "pid": pid, "rank": len(results) + 1,
                "cards_left": 0, "disconnected": pid in room.disconnected
            })

        remaining = [(i, len(room.game.players[i]))
                     for i in range(room.max_players) if i not in room.finish_order]
        remaining.sort(key=lambda x: x[1])
        for pid, cards_left in remaining:
            results.append({
                "pid": pid, "rank": len(results) + 1,
                "cards_left": cards_left, "disconnected": pid in room.disconnected
            })

        winner_str = f"Player {winner + 1}" if winner is not None else "none"
        logger.info(f"Game over in '{room.room_name}', winner: {winner_str}")

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
    def __init__(self, lobby: LobbyManager, rooms: RoomManager, server: 'DedicatedServer'):
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
            try:
                max_players = int(message.get("max_players", MAX_PLAYERS_PER_ROOM))
            except (ValueError, TypeError):
                max_players = MAX_PLAYERS_PER_ROOM
            if not (2 <= max_players <= MAX_PLAYERS_PER_ROOM):
                self.server.send_message(sock, {
                    "t": "e", "msg": f"max_players must be between 2 and {MAX_PLAYERS_PER_ROOM}"
                })
                return
            rules = message.get("rules", {})
            is_private = message.get("is_private", False)
            password = message.get("password")
            room_id = self.rooms.create_room(sock, room_name, creator_name, max_players, rules, is_private, password)
            if room_id:
                self.server.lobby.remove_client(sock)
                self.server.lobby.player_room_created[creator_name] = True
                logger.info(f"Room '{room_name}' ({max_players}p) created by {creator_name}")
                self.server.send_message(sock, {
                    "t": "room_joined", "room_id": room_id, "room_name": room_name,
                    "player_slot": 0, "max_players": max_players, "player_names": {0: creator_name}
                })
                self.lobby.broadcast(
                    {"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()},
                    sock, self.server
                )
            else:
                self.server.send_message(sock, {"t": "e", "msg": f"Failed to create room (max {MAX_ROOMS} rooms)"})

        elif msg_type == "join_room":
            if sock not in self.server.client_names:
                self.server.send_message(sock, {"t": "e", "msg": "Please set your name first"})
                return
            room_id = message.get("room_id")
            password = message.get("password")
            slot = self.rooms.join_room(sock, room_id, self.server.client_names[sock], password)
            if slot == -1:
                self.server.send_message(sock, {"t": "e", "msg": "Nesprávne heslo!"})
                return
            elif slot is not None:
                self.server.lobby.remove_client(sock)
                player_name = self.server.client_names[sock]
                room = self.rooms.rooms[room_id]
                players_count = len([p for p in room.players if p])
                logger.info(f"{player_name} joined '{room.room_name}' as Player {slot + 1}")
                self.server.send_message(sock, {
                    "t": "room_joined", "room_id": room_id, "room_name": room.room_name,
                    "player_slot": slot, "max_players": room.max_players, "player_names": room.player_names
                })
                self.rooms.broadcast_to_room(room_id, {
                    "t": "player_joined", "player_name": player_name, "player_slot": slot,
                    "players_count": players_count, "player_names": room.player_names
                }, server=self.server)
                if room.can_start_game():
                    room.start_game()
                    self._broadcast_game_state(room_id)
                else:
                    self.rooms.broadcast_to_room(room_id, {
                        "t": "waiting", "players_needed": room.max_players - players_count
                    }, server=self.server)
                self.lobby.broadcast(
                    {"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()},
                    server=self.server
                )
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
        room.last_activity = time.time()

        if msg_type == "leave_room":
            self.rooms.leave_room(sock, self.server, self.lobby)

        elif room.game:
            player_slot = next((p.slot for p in room.players if p and p.sock == sock), -1)

            if msg_type == "p" and player_slot == room.game.current_player:
                card_index = message.get("ci", -1)
                chosen_suit = message.get("cs", None)
                if room.game.play_card(player_slot, card_index, chosen_suit):
                    if not room.game.players[player_slot] and player_slot not in room.finish_order:
                        room.finish_order.append(player_slot)
                        logger.info(f"Player {player_slot + 1} finished in '{room.room_name}'")
                    self._broadcast_game_state(room_id)
                else:
                    self.server.send_message(sock, {"t": "e", "msg": "Invalid card play"})

            elif msg_type == "et" and player_slot == room.game.current_player:
                if room.game.end_turn(player_slot):
                    self._broadcast_game_state(room_id)
                else:
                    self.server.send_message(sock, {"t": "e", "msg": "Cannot end turn right now"})
            else:
                self.server.send_message(sock, {"t": "e", "msg": "Invalid action or not your turn"})

    def _broadcast_game_state(self, room_id: str) -> None:
        room = self.rooms.rooms[room_id]
        if not room.game:
            return
        current_state = room.game.serialize()
        current_state["player_names"] = room.player_names
        room.last_game_state = current_state
        self.rooms.broadcast_to_room(room_id, {"t": "gs", **current_state}, server=self.server)


class DedicatedServer:
    """Dedicated public server for continuous 24/7 operation."""

    def __init__(self):
        self.sel = selectors.DefaultSelector()
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind((HOST, PORT))
        except socket.error as e:
            logger.error(f"Failed to bind to {HOST}:{PORT}: {e}")
            sys.exit(1)

        self.server_socket.listen(50)
        self.server_socket.setblocking(False)
        self.sel.register(self.server_socket, selectors.EVENT_READ, self._accept_client)

        self.lobby = LobbyManager()
        self.rooms = RoomManager()
        self.message_handler = MessageHandler(self.lobby, self.rooms, self)
        self.client_names: Dict[socket.socket, str] = {}

        self.start_time = time.time()
        self.total_connections = 0
        self.total_games = 0
        self.last_stats_time = time.time()
        self.last_cleanup_time = time.time()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        local_ip = get_local_ip()
        public_ip = get_public_ip()
        logger.info("=" * 60)
        logger.info("  Sedma Bere Tri - Dedicated Server")
        logger.info("=" * 60)
        logger.info(f"  Local IP:  {local_ip}:{PORT}")
        logger.info(f"  Public IP: {public_ip}:{PORT}")
        logger.info(f"  Max rooms: {MAX_ROOMS}")
        logger.info(f"  Max players per room: {MAX_PLAYERS_PER_ROOM}")
        logger.info(f"  Room timeout: {ROOM_TIMEOUT}s")
        logger.info(f"  Log file: server.log")
        logger.info("=" * 60)

    def _signal_handler(self, sig, frame):
        logger.info("Received shutdown signal, cleaning up...")
        self._print_stats()
        for room in self.rooms.rooms.values():
            for sock in room.sockets:
                try:
                    sock.close()
                except Exception:
                    pass
        for sock in self.lobby.clients:
            try:
                sock.close()
            except Exception:
                pass
        self.server_socket.close()
        self.sel.close()
        logger.info("Server shut down gracefully.")
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
            return json.loads(zlib.decompress(data).decode())
        except (socket.error, json.JSONDecodeError, struct.error, zlib.error) as e:
            logger.error(f"Receive error: {e}")
            return None

    def _print_stats(self):
        uptime = time.time() - self.start_time
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        logger.info("--- Server Statistics ---")
        logger.info(f"  Uptime: {hours}h {minutes}m")
        logger.info(f"  Total connections: {self.total_connections}")
        logger.info(f"  Active players: {len(self.client_names)}")
        logger.info(f"  Lobby clients: {len(self.lobby.clients)}")
        logger.info(f"  Active rooms: {len(self.rooms.rooms)}")
        logger.info(f"  Total games played: {self.total_games}")
        logger.info("-------------------------")

    def start(self) -> None:
        logger.info("Server ready for connections")

        while True:
            events = self.sel.select(timeout=0.1)
            for key, mask in events:
                callback = key.data
                try:
                    callback(key.fileobj, mask)
                except Exception as e:
                    logger.error(f"Error in event callback: {e}")

            now = time.time()

            for room_id in list(self.rooms.rooms.keys()):
                room = self.rooms.rooms.get(room_id)
                if not room:
                    continue

                if room.game:
                    current_player = room.game.current_player
                    if len(room.game.players[current_player]) == 0 and not room.game.check_game_over():
                        room.game.end_turn(current_player)
                        self.message_handler._broadcast_game_state(room_id)

                    if room.game and room.game.check_game_over():
                        self.rooms.end_game(room_id, self, self.lobby)
                        self.total_games += 1

                if room.is_empty():
                    logger.info(f"Removing empty room: {room.room_name}")
                    try:
                        self.lobby.player_room_created.pop(room.creator.name, None)
                    except Exception:
                        pass
                    del self.rooms.rooms[room_id]

            if now - self.last_cleanup_time > 60:
                cleaned = self.rooms.cleanup_stale_rooms(self, self.lobby)
                if cleaned:
                    logger.info(f"Cleaned up {cleaned} stale room(s)")
                self.last_cleanup_time = now

            if now - self.last_stats_time > STATS_INTERVAL:
                self._print_stats()
                self.last_stats_time = now

    def _accept_client(self, sock: socket.socket, mask: int) -> None:
        try:
            client_sock, addr = sock.accept()
            client_sock.setblocking(False)
            self.sel.register(client_sock, selectors.EVENT_READ, self._handle_client)
            is_local = addr[0] in ('127.0.0.1', 'localhost', '::1')
            client_display = f"localhost:{addr[1]}" if is_local else f"{addr[0]}:{addr[1]}"
            self.lobby.add_client(client_sock)
            self.total_connections += 1
            logger.info(f"New client connected from {client_display} (total: {self.total_connections})")
            self.send_message(client_sock, {"t": "lobby_welcome", "msg": "Welcome! Enter your name to continue."})
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
        except Exception:
            client_display = "unknown"
        logger.info(f"Client {client_name} disconnected from {client_display}")
        if sock in self.rooms.client_rooms:
            self.rooms.leave_room(sock, self, self.lobby)
        self.lobby.remove_client(sock)
        self.client_names.pop(sock, None)
        try:
            sock.close()
            self.sel.unregister(sock)
        except Exception:
            pass
        self.lobby.broadcast(
            {"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()},
            server=self
        )


def main():
    max_restarts = 10
    restart_count = 0
    restart_delay = 5

    while restart_count < max_restarts:
        try:
            server = DedicatedServer()
            server.start()
        except SystemExit:
            break
        except KeyboardInterrupt:
            logger.info("Server stopped by user")
            break
        except Exception as e:
            restart_count += 1
            logger.error(f"Server crashed: {e}")
            logger.info(f"Restarting in {restart_delay}s... (attempt {restart_count}/{max_restarts})")
            time.sleep(restart_delay)
            restart_delay = min(restart_delay * 2, 60)

    if restart_count >= max_restarts:
        logger.error(f"Max restarts ({max_restarts}) reached. Server shutting down permanently.")


if __name__ == "__main__":
    main()
