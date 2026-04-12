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
HOST = "0.0.0.0"
MAX_ROOMS = 5
MAX_PLAYERS_PER_ROOM = 4


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except socket.error:
        return "127.0.0.1"


def get_public_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(("api.ipify.org", 80))
            s.sendall(b"GET /?format=text HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
            response = b""
            while True:
                chunk = s.recv(1024)
                if not chunk:
                    break
                response += chunk
        return response.decode().split("\r\n\r\n", 1)[-1].strip()
    except Exception:
        return get_local_ip()


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
        self.players: List[Optional[Player]] = [None] * max_players
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
                    print(f"[{_ts()}] Presúvam karty Hráča {i + 1} do odhadzovacieho balíčka v '{self.room_name}'")
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

    def start_game(self) -> bool:
        if not self.can_start_game():
            return False
        self.game = Game(self.max_players)
        self.game.create_deck()
        self.game.deal_cards()
        self.finish_order = []
        self.disconnected = set()
        print(f"[{_ts()}] Hra začala v miestnosti '{self.room_name}' s {self.max_players} hráčmi")
        return True

    def get_room_info(self) -> dict:
        return {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "creator": self.creator.name,
            "players": len([p for p in self.players if p]),
            "max_players": self.max_players,
            "in_game": self.game is not None or self.game_ended,
            "created_at": self.created_at.strftime("%H:%M:%S"),
        }


class LobbyManager:
    def __init__(self):
        self.clients: Set[socket.socket] = set()
        self.player_room_created: Dict[str, bool] = {}

    def add_client(self, sock: socket.socket):
        self.clients.add(sock)

    def remove_client(self, sock: socket.socket):
        self.clients.discard(sock)

    def broadcast(self, message: dict, exclude_sock: Optional[socket.socket] = None, server: "MultiRoomServer" = None):
        if server is None:
            return
        failed = []
        for sock in list(self.clients):
            if sock != exclude_sock and not server.send_message(sock, message):
                failed.append(sock)
        for sock in failed:
            self.remove_client(sock)
            server._remove_client(sock)

    def send_room_list(self, sock: socket.socket, rooms: Dict[str, GameRoom], server: "MultiRoomServer"):
        if server is None:
            return
        rooms_info = [r.get_room_info() for r in rooms.values() if not r.game and not r.game_ended]
        server.send_message(sock, {
            "t": "room_list",
            "rooms": rooms_info,
            "max_rooms": MAX_ROOMS,
            "current_rooms": len(rooms),
        })


class RoomManager:
    def __init__(self):
        self.rooms: Dict[str, GameRoom] = {}
        self.client_rooms: Dict[socket.socket, str] = {}

    def create_room(
        self, sock: socket.socket, room_name: str, creator_name: str, max_players: int = MAX_PLAYERS_PER_ROOM
    ) -> Optional[str]:
        if len(self.rooms) >= MAX_ROOMS or not (2 <= max_players <= MAX_PLAYERS_PER_ROOM):
            return None
        room_id = str(uuid.uuid4())
        creator = Player(sock, creator_name, 0)
        self.rooms[room_id] = GameRoom(room_id, room_name, creator, max_players)
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

    def leave_room(self, sock: socket.socket, server: "MultiRoomServer", lobby: LobbyManager) -> None:
        if sock not in self.client_rooms:
            lobby.add_client(sock)
            server.send_message(sock, {"t": "back_to_lobby"})
            lobby.send_room_list(sock, self.rooms, server)
            return

        room_id = self.client_rooms.pop(sock)
        if room_id in self.rooms:
            room = self.rooms[room_id]
            player_name = server.client_names.get(sock, "Unknown")
            room.remove_player(sock)
            print(f"[{_ts()}] {player_name} opustil miestnosť '{room.room_name}'")

            self.broadcast_to_room(room_id, {
                "t": "player_left",
                "player_name": player_name,
                "players_count": len([p for p in room.players if p]),
            }, server=server)

            if room.game:
                state = room.game.serialize()
                self.broadcast_to_room(room_id, {"t": "gs", **state, "player_names": room.player_names}, server=server)

            if room.is_empty():
                lobby.player_room_created.pop(room.creator.name, None)
                print(f"[{_ts()}] Zatvárám prázdnu miestnosť: {room.room_name}")
                del self.rooms[room_id]

        lobby.add_client(sock)
        server.send_message(sock, {"t": "back_to_lobby"})
        lobby.send_room_list(sock, self.rooms, server)
        lobby.broadcast(
            {"t": "room_list_update", "rooms": self.get_available_rooms_info()},
            server=server,
        )

    def broadcast_to_room(
        self,
        room_id: str,
        message: dict,
        exclude_sock: Optional[socket.socket] = None,
        server: "MultiRoomServer" = None,
    ):
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
        return [r.get_room_info() for r in self.rooms.values() if not r.game and not r.game_ended]

    def end_game(self, room_id: str, server: "MultiRoomServer", lobby: LobbyManager) -> None:
        if room_id not in self.rooms:
            return
        room = self.rooms[room_id]
        if not room.game:
            return

        winner = next((i for i, p in enumerate(room.game.players) if not p), None)
        results = []
        for pid in room.finish_order:
            results.append({"pid": pid, "rank": len(results) + 1, "cards_left": 0, "disconnected": pid in room.disconnected})
        remaining = sorted(
            [(i, len(room.game.players[i])) for i in range(room.max_players) if i not in room.finish_order],
            key=lambda x: x[1],
        )
        for pid, cards_left in remaining:
            results.append({"pid": pid, "rank": len(results) + 1, "cards_left": cards_left, "disconnected": pid in room.disconnected})

        print(f"[{_ts()}] Koniec hry v '{room.room_name}', víťaz: Hráč {winner + 1 if winner is not None else 'žiadny'}")
        self.broadcast_to_room(room_id, {
            "t": "go",
            "w": winner + 1 if winner is not None else None,
            "results": results,
            "player_names": room.player_names,
        }, server=server)

        room.game = None
        room.game_ended = True
        room.last_game_state = None
        room.finish_order = []


class MessageHandler:
    def __init__(self, lobby: LobbyManager, rooms: RoomManager, server: "MultiRoomServer"):
        self.lobby = lobby
        self.rooms = rooms
        self.server = server

    def handle_lobby_message(self, sock: socket.socket, message: dict) -> None:
        msg_type = message.get("t")

        if msg_type == "set_name":
            name = message.get("name", "").strip()
            if not (3 <= len(name) <= 20):
                self.server.send_message(sock, {"t": "e", "msg": "Meno musí mať 3-20 znakov"})
                return
            if name in self.server.client_names.values():
                self.server.send_message(sock, {"t": "e", "msg": "Meno je už obsadené"})
                return
            self.server.client_names[sock] = name
            self.server.send_message(sock, {"t": "name_set", "name": name})
            self.lobby.send_room_list(sock, self.rooms.rooms, self.server)

        elif msg_type == "create_room":
            if sock not in self.server.client_names:
                self.server.send_message(sock, {"t": "e", "msg": "Najprv nastav meno"})
                return
            creator_name = self.server.client_names[sock]
            if self.lobby.player_room_created.get(creator_name):
                self.server.send_message(sock, {"t": "e", "msg": "Môžeš vytvoriť iba jednu miestnosť"})
                return
            room_name = message.get("room_name", "").strip()
            if not (3 <= len(room_name) <= 30):
                self.server.send_message(sock, {"t": "e", "msg": "Názov miestnosti musí mať 3-30 znakov"})
                return
            try:
                max_players = int(message.get("max_players", MAX_PLAYERS_PER_ROOM))
            except (ValueError, TypeError):
                max_players = MAX_PLAYERS_PER_ROOM
            if not (2 <= max_players <= MAX_PLAYERS_PER_ROOM):
                self.server.send_message(sock, {"t": "e", "msg": f"Počet hráčov musí byť 2 až {MAX_PLAYERS_PER_ROOM}"})
                return

            room_id = self.rooms.create_room(sock, room_name, creator_name, max_players)
            if room_id:
                self.lobby.remove_client(sock)
                self.lobby.player_room_created[creator_name] = True
                print(f"[{_ts()}] Miestnosť '{room_name}' ({max_players}h) vytvoril {creator_name}")
                self.server.send_message(sock, {
                    "t": "room_joined",
                    "room_id": room_id,
                    "room_name": room_name,
                    "player_slot": 0,
                    "max_players": max_players,
                })
                self.lobby.broadcast({"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()}, sock, self.server)
            else:
                self.server.send_message(sock, {"t": "e", "msg": f"Nepodarilo sa vytvoriť miestnosť (max {MAX_ROOMS})"})

        elif msg_type == "join_room":
            if sock not in self.server.client_names:
                self.server.send_message(sock, {"t": "e", "msg": "Najprv nastav meno"})
                return
            room_id = message.get("room_id")
            player_name = self.server.client_names[sock]
            slot = self.rooms.join_room(sock, room_id, player_name)
            if slot is not None:
                self.lobby.remove_client(sock)
                room = self.rooms.rooms[room_id]
                players_count = len([p for p in room.players if p])
                print(f"[{_ts()}] {player_name} vstúpil do '{room.room_name}' ako Hráč {slot + 1}")
                self.server.send_message(sock, {
                    "t": "room_joined",
                    "room_id": room_id,
                    "room_name": room.room_name,
                    "player_slot": slot,
                    "max_players": room.max_players,
                })
                self.rooms.broadcast_to_room(room_id, {
                    "t": "player_joined",
                    "player_name": player_name,
                    "player_slot": slot,
                    "players_count": players_count,
                }, server=self.server)
                if room.can_start_game():
                    room.start_game()
                    self._broadcast_game_state(room_id)
                else:
                    self.rooms.broadcast_to_room(room_id, {
                        "t": "waiting",
                        "players_needed": room.max_players - players_count,
                    }, server=self.server)
                self.lobby.broadcast({"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()}, server=self.server)
            else:
                self.server.send_message(sock, {"t": "e", "msg": "Nepodarilo sa pripojiť do miestnosti"})

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
            return

        if not room.game:
            return

        player_slot = next((p.slot for p in room.players if p and p.sock == sock), -1)

        if msg_type == "p" and player_slot == room.game.current_player:
            card_index = message.get("ci", -1)
            print(f"[{_ts()}] Hráč {player_slot + 1} zahral kartu {card_index} v '{room.room_name}'")
            if room.game.play_card(player_slot, card_index):
                if not room.game.players[player_slot] and player_slot not in room.finish_order:
                    room.finish_order.append(player_slot)
                    print(f"[{_ts()}] Hráč {player_slot + 1} dokončil hru v '{room.room_name}'")
                self._broadcast_game_state(room_id)
            else:
                self.server.send_message(sock, {"t": "e", "msg": "Neplatný ťah"})

        elif msg_type == "d" and player_slot == room.game.current_player:
            print(f"[{_ts()}] Hráč {player_slot + 1} berie kartu v '{room.room_name}'")
            if room.game.draw_card(player_slot):
                room.game.next_turn()
                self._broadcast_game_state(room_id)
            else:
                self.server.send_message(sock, {"t": "e", "msg": "Balíček je prázdny"})
        else:
            self.server.send_message(sock, {"t": "e", "msg": "Neplatná akcia alebo nie je tvoj ťah"})

    def _broadcast_game_state(self, room_id: str) -> None:
        room = self.rooms.rooms[room_id]
        if not room.game:
            return
        state = room.game.serialize()
        state["player_names"] = room.player_names
        room.last_game_state = state
        self.rooms.broadcast_to_room(room_id, {"t": "gs", **state}, server=self.server)


class MultiRoomServer:
    def __init__(self, port: int):
        self.sel = selectors.DefaultSelector()
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind((HOST, port))
        except socket.error as e:
            logger.error(f"Nepodarilo sa spustiť server na {HOST}:{port}: {e}")
            sys.exit(1)

        self.server_socket.listen(20)
        self.server_socket.setblocking(False)
        self.sel.register(self.server_socket, selectors.EVENT_READ, self._accept_client)

        self.lobby = LobbyManager()
        self.rooms = RoomManager()
        self.message_handler = MessageHandler(self.lobby, self.rooms, self)
        self.client_names: Dict[socket.socket, str] = {}

        signal.signal(signal.SIGINT, self._signal_handler)
        public_ip = get_public_ip()
        print(f"[{_ts()}] Server spustený — verejná IP: {public_ip}:{port} | lokálna IP: {get_local_ip()}:{port}")

    def _signal_handler(self, sig, frame):
        print(f"[{_ts()}] Vypínam server...")
        for room in self.rooms.rooms.values():
            for sock in room.sockets:
                try:
                    sock.close()
                except Exception:
                    pass
        for sock in list(self.lobby.clients):
            try:
                sock.close()
            except Exception:
                pass
        self.server_socket.close()
        self.sel.close()
        sys.exit(0)

    def send_message(self, sock: socket.socket, message: dict, retries: int = 2) -> bool:
        try:
            data = zlib.compress(json.dumps(message, separators=(",", ":")).encode())
            sock.sendall(struct.pack("!I", len(data)) + data)
            return True
        except socket.error as e:
            logger.error(f"Chyba odosielania pre {self.client_names.get(sock, 'unknown')}: {e}")
            if retries > 0:
                time.sleep(0.1)
                return self.send_message(sock, message, retries - 1)
            return False

    def receive_message(self, sock: socket.socket) -> Optional[dict]:
        try:
            length_data = sock.recv(4)
            if not length_data:
                return None
            length = struct.unpack("!I", length_data)[0]
            data = b""
            while len(data) < length:
                packet = sock.recv(length - len(data))
                if not packet:
                    return None
                data += packet
            return json.loads(zlib.decompress(data).decode())
        except (socket.error, json.JSONDecodeError, struct.error, zlib.error) as e:
            logger.error(f"Chyba prijímania: {e}")
            return None

    def start(self) -> None:
        print(f"[{_ts()}] Server pripravený na pripojenia")
        while True:
            events = self.sel.select(timeout=0.1)
            for key, mask in events:
                key.data(key.fileobj, mask)

            for room_id in list(self.rooms.rooms.keys()):
                room = self.rooms.rooms[room_id]
                if not room.game:
                    continue

                current = room.game.current_player
                if not room.game.players[current] and not room.game.check_game_over():
                    print(f"[{_ts()}] Auto-preskakovanie odpojeného Hráča {current + 1} v '{room.room_name}'")
                    room.game.next_turn()
                    self.message_handler._broadcast_game_state(room_id)

                if room.game.check_game_over():
                    self.rooms.end_game(room_id, self, self.lobby)

                if room.is_empty():
                    print(f"[{_ts()}] Odstraňujem prázdnu miestnosť: {room.room_name}")
                    del self.rooms.rooms[room_id]

    def _accept_client(self, sock: socket.socket, mask: int) -> None:
        try:
            client_sock, addr = sock.accept()
            client_sock.setblocking(False)
            self.sel.register(client_sock, selectors.EVENT_READ, self._handle_client)
            self.lobby.add_client(client_sock)
            display = f"localhost:{addr[1]}" if addr[0] in ("127.0.0.1", "::1") else f"{addr[0]}:{addr[1]}"
            print(f"[{_ts()}] Pripojený klient z {display}")
            self.send_message(client_sock, {"t": "lobby_welcome"})
        except socket.error as e:
            logger.error(f"Chyba prijímania spojenia: {e}")

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
            logger.error(f"Chyba spracovania správy: {e}")
            self.send_message(sock, {"t": "e", "msg": f"Chyba servera: {str(e)}"})

    def _remove_client(self, sock: socket.socket) -> None:
        name = self.client_names.get(sock, "Unknown")
        try:
            addr = sock.getpeername()
            display = f"{addr[0]}:{addr[1]}"
        except Exception:
            display = "unknown"
        print(f"[{_ts()}] {name} sa odpojil z {display}")

        if sock in self.rooms.client_rooms:
            self.rooms.leave_room(sock, self, self.lobby)
        self.lobby.remove_client(sock)
        self.client_names.pop(sock, None)
        try:
            sock.close()
            self.sel.unregister(sock)
        except Exception:
            pass
        self.lobby.broadcast({"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()}, server=self)


def run_server(port: int = DEFAULT_PORT) -> None:
    """Vstupný bod — použiteľné aj z iného modulu (napr. z .exe cez thread)."""
    server = MultiRoomServer(port)
    server.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sedma Bere Tri — server")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", DEFAULT_PORT)))
    args = parser.parse_args()
    run_server(args.port)