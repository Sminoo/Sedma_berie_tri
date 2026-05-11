# Sedma Bere Tri — Herný server
# Viac-miestnosťový kartový server s lobby systémom

import socket
import selectors
import json
import struct
import signal
import sys
import os
import argparse
import zlib
import uuid
import time
import random
import logging
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from datetime import datetime
from game_logic import Game

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Konfigurácia servera
DEFAULT_PORT = 65432
HOST = '0.0.0.0'
MAX_ROOMS = 5
MAX_PLAYERS_PER_ROOM = 4


def get_local_ip() -> str:
    """Zistí lokálnu IP adresu."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except socket.error as e:
        logger.error(f"Nepodarilo sa zistiť lokálnu IP: {e}")
        return "Unknown"


@dataclass
class Player:
    sock: socket.socket
    name: str
    slot: int


class GameRoom:
    """Herná miestnosť s hráčmi, pravidlami a stavom hry."""

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
        self.leader_sock: socket.socket = creator.sock  # socket of current room leader

        # Tournament mode fields
        self.tournament_mode: bool = False
        self.tournament_round: int = 0
        # penalties[slot] = number of cards fewer than 5 this player starts with
        self.tournament_penalties: Dict[int, int] = {}
        # eliminated[slot] = True when player has lost with 1 card
        self.tournament_eliminated: Set[int] = set()
        # last round's loser slot (gets +1 penalty next round)
        self.tournament_last_loser: Optional[int] = None

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
        """Odstráni hráča z miestnosti. Ak prebieha hra, vyčistí jeho karty."""
        for i, player in enumerate(self.players):
            if player and player.sock == sock:
                if self.game and self.game.players[i]:
                    # Zahodenie kariet odpoj. hráča
                    self.game.discard_pile.extendleft(self.game.players[i])
                    self.game.players[i].clear()
                    self.disconnected.add(i)

                    # Ak bol na ťahu, posunie sa ďalej bez ťahania
                    if self.game.current_player == i:
                        # Vynúti cards_played > 0, aby end_turn neťahal kartu
                        self.game.cards_played_this_turn = 1
                        self.game.end_turn(i)
                else:
                    self.player_names.pop(i, None)

                self.players[i] = None
                self.sockets.discard(sock)
                # Presun vedenia na ďalšieho hráča
                if sock == self.leader_sock:
                    remaining = [p for p in self.players if p is not None]
                    if remaining:
                        self.leader_sock = remaining[0].sock
                return True
        return False

    def is_empty(self) -> bool:
        return all(p is None for p in self.players)

    def is_full(self) -> bool:
        return all(p is not None for p in self.players)

    def can_start_game(self, manual: bool = False) -> bool:
        """Skontroluje, či je možné spustiť hru."""
        player_count = len([p for p in self.players if p])
        if manual:
            return player_count >= 2 and self.game is None and not self.game_ended
        return (player_count == self.max_players
                and self.game is None and not self.game_ended)

    def start_game(self, manual: bool = False) -> bool:
        """Spustí novú hru v miestnosti."""
        if not self.can_start_game(manual):
            return False
        # Vždy použi max_players aby indexy slotov sedeli
        self.game = Game(self.max_players, self.rules)
        self.game.create_deck()

        if self.tournament_mode:
            self.tournament_round += 1
            # hand_sizes uses room slot indices; eliminated/absent slots get 0
            hand_sizes = {}
            for i in range(self.max_players):
                if self.players[i] is None:
                    hand_sizes[i] = 0
                elif i in self.tournament_eliminated:
                    hand_sizes[i] = 0
                else:
                    hand_sizes[i] = max(0, 5 - self.tournament_penalties.get(i, 0))
            self.game.deal_cards(hand_sizes)
            logger.info(f"Tournament round {self.tournament_round} in '{self.room_name}', hand_sizes={hand_sizes}")
        else:
            # Normálny mód: prázdne sloty dostanú 0 kariet
            hand_sizes = {i: (5 if self.players[i] is not None else 0)
                          for i in range(self.max_players)}
            self.game.deal_cards(hand_sizes)

        occupied_slots = [i for i, p in enumerate(self.players)
                          if p and i not in self.tournament_eliminated]
        if occupied_slots:
            if self.tournament_mode:
                # Tournament: first round -> random start; subsequent rounds -> player after last loser
                if self.tournament_round <= 1 or self.tournament_last_loser is None:
                    self.game.current_player = random.choice(occupied_slots)
                else:
                    try:
                        idx = occupied_slots.index(self.tournament_last_loser)
                        next_idx = (idx + 1) % len(occupied_slots)
                        self.game.current_player = occupied_slots[next_idx]
                    except ValueError:
                        # fall back to random if last loser not present/active
                        self.game.current_player = random.choice(occupied_slots)
                logger.info(f"Starting player for room '{self.room_name}' is Player {self.game.current_player + 1} (tournament_mode={self.tournament_mode}, round={self.tournament_round})")
            else:
                # Normal mode: random starting player among active slots
                self.game.current_player = random.choice(occupied_slots)
                logger.info(f"Starting player for room '{self.room_name}' is Player {self.game.current_player + 1}")
        self.finish_order = []
        self.disconnected = set()
        player_count = len([p for p in self.players if p])
        logger.info(f"Game started in room '{self.room_name}' with {player_count} players")
        return True

    def get_room_info(self) -> dict:
        """Vráti informácie o miestnosti pre lobby."""
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
    """Správa hráčov v lobby (pred vstupom do miestnosti)."""

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
                  server: 'MultiRoomServer' = None):
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
                       server: 'MultiRoomServer'):
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
        room.tournament_mode = rules.get("tournament_mode", False)
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
        return slot

    def leave_room(self, sock: socket.socket, server: 'MultiRoomServer',
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
                    "player_names": room.player_names,
                    "new_leader_sock_id": id(room.leader_sock)
                }, server=server)
                # In waiting room, refresh waiting text after someone leaves
                if not room.game and not room.game_ended:
                    players_count = len([p for p in room.players if p])
                    self.broadcast_to_room(room_id, {
                        "t": "waiting",
                        "players_needed": max(0, room.max_players - players_count),
                        "leader_sock_id": id(room.leader_sock),
                        "rules": room.rules,
                        "player_names": room.player_names
                    }, server=server)
                # Notify the new leader
                if room.leader_sock in room.sockets:
                    server.send_message(room.leader_sock, {"t": "you_are_leader"})

                if room.game:
                    game_state = room.game.serialize()
                    self.broadcast_to_room(room_id, {
                        "t": "gs", **game_state,
                        "player_names": room.player_names
                    }, server=server)

                # Tournament: if only 1 player left, end as final
                remaining = [p for p in room.players if p]
                if room.tournament_mode and room.game and len(remaining) <= 1:
                    self.end_game(room_id, server, lobby)

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
                          server: 'MultiRoomServer' = None):
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

    def end_game(self, room_id: str, server: 'MultiRoomServer', lobby: LobbyManager) -> None:
        if room_id not in self.rooms:
            return
        room = self.rooms[room_id]
        if not room.game:
            return

        # In tournament final, winner = last active non-eliminated player
        # In normal game, winner = first player to empty their hand
        if room.tournament_mode and room.tournament_eliminated:
            active_now = [i for i in range(room.max_players)
                          if room.players[i] and i not in room.tournament_eliminated]
            winner = active_now[0] if len(active_now) == 1 else (
                next((i for i, p in enumerate(room.game.players) if not p and i in room.game.active_slots), None)
            )
        else:
            winner = next((i for i, p in enumerate(room.game.players) if not p), None)

        # Build results: winner first (fewest/no cards), then by cards ascending
        remaining = [(i, len(room.game.players[i]))
                     for i in range(room.max_players)
                     if i not in room.finish_order and i in room.player_names]
        remaining.sort(key=lambda x: x[1])

        results = []
        # Players who finished (emptied hand) — rank 1 first
        for pid in room.finish_order:
            results.append({
                "pid": pid, "rank": len(results) + 1,
                "cards_left": 0, "disconnected": pid in room.disconnected
            })
        # Remaining players sorted by cards left (fewest first)
        for pid, cards_left in remaining:
            results.append({
                "pid": pid, "rank": len(results) + 1,
                "cards_left": cards_left, "disconnected": pid in room.disconnected
            })

        winner_str = f"Player {winner + 1}" if winner is not None else "none"
        logger.info(f"Game over in room '{room.room_name}', winner: {winner_str}")

        room.game = None
        room.last_game_state = None
        room.finish_order = []

        if room.tournament_mode:
            # Apply penalty to last-place player FIRST, then rank
            if results:
                loser_pid = results[-1]["pid"]
                room.tournament_last_loser = loser_pid
                if loser_pid not in room.tournament_eliminated:
                    new_penalty = room.tournament_penalties.get(loser_pid, 0) + 1
                    room.tournament_penalties[loser_pid] = new_penalty
                    cards_next = max(0, 5 - new_penalty)
                    logger.info(f"Tournament: Player {loser_pid + 1} penalty={new_penalty}, next hand={cards_next}")
                    if cards_next <= 0:
                        room.tournament_eliminated.add(loser_pid)
                        logger.info(f"Tournament: Player {loser_pid + 1} eliminated!")

            # Now rank with updated penalties (fewest penalties = best rank)
            all_pids = [r["pid"] for r in results]
            all_pids.sort(key=lambda p: room.tournament_penalties.get(p, 0))
            results = [{
                "pid": p,
                "rank": i + 1,
                "cards_left": len(room.game.players[p]) if room.game and p < len(room.game.players) else 0,
                "disconnected": p in room.disconnected
            } for i, p in enumerate(all_pids)]

            # Count active (non-eliminated, still connected) players
            active = [i for i, p in enumerate(room.players)
                      if p and i not in room.tournament_eliminated]

            # Attach tournament info to results
            penalties_info = {}
            for i in range(room.max_players):
                if room.players[i]:
                    pen = room.tournament_penalties.get(i, 0)
                    penalties_info[i] = {
                        "cards_next": max(0, 5 - pen),
                        "eliminated": i in room.tournament_eliminated
                    }

            connected_active = [i for i in active if room.players[i] is not None]
            # Also end tournament if nobody is active (all eliminated this round)
            if len(connected_active) == 0 or len(connected_active) <= 1:
                # Tournament over — send final game over (clients show leaderboard)
                self.broadcast_to_room(room_id, {
                    "t": "go",
                    "w": winner + 1 if winner is not None else None,
                    "results": results,
                    "player_names": room.player_names,
                    "tournament_final": True
                }, server=server)

                # Mark game ended and schedule post-leaderboard cleanup (delay so clients can show leaderboard)
                room.game = None
                room.last_game_state = None
                room.finish_order = []
                room.game_ended = True
                room.leaderboard_show_until = time.time() + 5.0  # seconds before moving players back to lobby
            else:
                # Round over — send round results then start next round after delay
                for r in results:
                    pid = r["pid"]
                    r["cards_next"] = penalties_info.get(pid, {}).get("cards_next", 5)
                    r["eliminated"] = penalties_info.get(pid, {}).get("eliminated", False)
                self.broadcast_to_room(room_id, {
                    "t": "tournament_round_over",
                    "results": results,
                    "player_names": room.player_names,
                    "round": room.tournament_round,
                    "penalties": {str(k): v for k, v in penalties_info.items()}
                }, server=server)
                # Schedule next round start (server loop will pick it up)
                room.tournament_next_round_at = time.time() + 5.0
        else:
            self.broadcast_to_room(room_id, {
                "t": "go",
                "w": winner + 1 if winner is not None else None,
                "results": results,
                "player_names": room.player_names
            }, server=server)
            # Schedule showing leaderboard before moving players back to lobby
            room.game_ended = True
            room.leaderboard_show_until = time.time() + 5.0


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
            # tournament_mode is passed inside rules dict from client
            room_id = self.rooms.create_room(sock, room_name, creator_name, max_players, rules, is_private, password)
            if room_id:
                self.server.lobby.remove_client(sock)
                self.server.lobby.player_room_created[creator_name] = True
                logger.info(f"Room '{room_name}' ({max_players}p) created by {creator_name}")
                self.server.send_message(sock, {
                    "t": "room_joined",
                    "room_id": room_id,
                    "room_name": room_name,
                    "player_slot": 0,
                    "max_players": max_players,
                    "player_names": {0: creator_name},
                    "is_leader": True,
                    "rules": rules
                })
                self.lobby.broadcast(
                    {"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()},
                    sock, self.server
                )
            else:
                self.server.send_message(sock, {
                    "t": "e", "msg": f"Failed to create room (max {MAX_ROOMS} rooms)"
                })

        elif msg_type == "join_room":
            if sock not in self.server.client_names:
                self.server.send_message(sock, {"t": "e", "msg": "Please set your name first"})
                return

            room_id = message.get("room_id")
            password = message.get("password")
            slot = self.rooms.join_room(sock, room_id, self.server.client_names[sock], password)

            if slot == -1:
                self.server.send_message(sock, {"t": "e", "msg": "Incorrect password!"})
                return
            elif slot is not None:
                self.server.lobby.remove_client(sock)
                player_name = self.server.client_names[sock]
                room = self.rooms.rooms[room_id]
                players_count = len([p for p in room.players if p])
                logger.info(f"{player_name} joined room '{room.room_name}' as Player {slot + 1}")

                self.server.send_message(sock, {
                    "t": "room_joined",
                    "room_id": room_id,
                    "room_name": room.room_name,
                    "player_slot": slot,
                    "max_players": room.max_players,
                    "player_names": room.player_names,
                    "is_leader": False,
                    "rules": room.rules
                })

                self.rooms.broadcast_to_room(room_id, {
                    "t": "player_joined",
                    "player_name": player_name,
                    "player_slot": slot,
                    "players_count": players_count,
                    "player_names": room.player_names,
                    "rules": room.rules
                }, server=self.server)

                # Never auto-start — leader must press Start Game
                self.rooms.broadcast_to_room(room_id, {
                    "t": "waiting",
                    "players_needed": room.max_players - players_count,
                    "leader_sock_id": id(room.leader_sock),
                    "rules": room.rules,
                    "player_names": room.player_names
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

        if msg_type == "leave_room":
            self.rooms.leave_room(sock, self.server, self.lobby)

        elif msg_type == "start_game":
            # Only the current leader may start the game
            if sock != room.leader_sock:
                self.server.send_message(sock, {"t": "e", "msg": "Only the room leader can start the game"})
                return
            if room.start_game(manual=True):
                self._broadcast_game_state(room_id)
            else:
                self.server.send_message(sock, {"t": "e", "msg": "Cannot start game (need at least 2 players)"})

        elif room.game:
            player_slot = next((p.slot for p in room.players if p and p.sock == sock), -1)

            if msg_type == "p" and player_slot == room.game.current_player:
                card_index = message.get("ci", -1)
                chosen_suit = message.get("cs", None)
                if room.game.play_card(player_slot, card_index, chosen_suit):
                    if not room.game.players[player_slot] and player_slot not in room.finish_order:
                        room.finish_order.append(player_slot)
                        logger.info(f"Player {player_slot + 1} finished in room '{room.room_name}'")
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


class MultiRoomServer:
    def __init__(self, port: int, install_signal_handler: bool = True):
        # Initialize selector and listening socket
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

        # Managers and state
        self.lobby = LobbyManager()
        self.rooms = RoomManager()
        self.message_handler = MessageHandler(self.lobby, self.rooms, self)
        self.client_names: Dict[socket.socket, str] = {}

        # Install signal handler only when appropriate (main process)
        if install_signal_handler:
            try:
                signal.signal(signal.SIGINT, self._signal_handler)
            except Exception:
                logger.debug("Signal handlers not available in this environment")

        local_ip = get_local_ip()
        logger.info(f"Server started on {local_ip}:{port} and localhost:{port}")

    def _signal_handler(self, sig, frame):
        logger.info("Shutting down server...")
        self.shutdown()
        sys.exit(0)

    def shutdown(self) -> None:
        """Gracefully notify all connected clients and close sockets."""
        try:
            # Notify room members first
            for room in list(self.rooms.rooms.values()):
                for sock in list(room.sockets):
                    try:
                        self.send_message(sock, {"t": "server_disconnected", "msg": "Server shutting down"})
                    except Exception:
                        pass
            # Notify lobby clients
            for sock in list(self.lobby.clients):
                try:
                    self.send_message(sock, {"t": "server_disconnected", "msg": "Server shutting down"})
                except Exception:
                    pass
        except Exception:
            logger.exception("Error while notifying clients during shutdown")

        # Close all sockets and selector
        try:
            for room in list(self.rooms.rooms.values()):
                for sock in list(room.sockets):
                    try:
                        sock.close()
                    except Exception:
                        pass
            for sock in list(self.lobby.clients):
                try:
                    sock.close()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            self.server_socket.close()
        except Exception:
            pass
        try:
            self.sel.close()
        except Exception:
            pass

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

    def start(self) -> None:
        logger.info("Server ready for connections")
        while True:
            events = self.sel.select(timeout=0.1)
            for key, mask in events:
                callback = key.data
                callback(key.fileobj, mask)

            for room_id in list(self.rooms.rooms.keys()):
                room = self.rooms.rooms.get(room_id)
                if not room:
                    continue

                if room.game:
                    current_player = room.game.current_player
                    if len(room.game.players[current_player]) == 0 and not room.game.check_game_over():
                        logger.info(f"Auto-skipping turn for Player {current_player + 1} in room '{room.room_name}'")
                        room.game.end_turn(current_player)
                        self.message_handler._broadcast_game_state(room_id)

                    if room.game and room.game.check_game_over():
                        self.rooms.end_game(room_id, self, self.lobby)

                # Start next tournament round after delay
                if (not room.game and not room.game_ended
                        and room.tournament_mode
                        and hasattr(room, 'tournament_next_round_at')
                        and time.time() >= room.tournament_next_round_at):
                    del room.tournament_next_round_at
                    room.start_game(manual=True)
                    self.message_handler._broadcast_game_state(room_id)

                # If leaderboard display time expired for a finished game, move players back to lobby and remove room
                if getattr(room, 'game_ended', False) and getattr(room, 'leaderboard_show_until', 0) and time.time() >= room.leaderboard_show_until:
                    logger.info(f"Post-leaderboard cleanup for room: {room.room_name}")
                    # move connected players back to lobby
                    for p in list(room.players):
                        if p:
                            try:
                                self.rooms.client_rooms.pop(p.sock, None)
                            except Exception:
                                pass
                            try:
                                self.lobby.add_client(p.sock)
                                self.send_message(p.sock, {"t": "back_to_lobby"})
                                self.lobby.send_room_list(p.sock, self.rooms, self)
                            except Exception:
                                pass
                    try:
                        self.lobby.player_room_created.pop(room.creator.name, None)
                    except Exception:
                        pass
                    if room_id in self.rooms.rooms:
                        del self.rooms.rooms[room_id]
                    self.lobby.broadcast({"t": "room_list_update", "rooms": self.rooms.get_available_rooms_info()}, server=self)
                    continue

                if room.is_empty():
                    logger.info(f"Removing empty room: {room.room_name}")
                    del self.rooms.rooms[room_id]

    def _accept_client(self, sock: socket.socket, mask: int) -> None:
        try:
            client_sock, addr = sock.accept()
            client_sock.setblocking(False)
            self.sel.register(client_sock, selectors.EVENT_READ, self._handle_client)

            is_local = addr[0] in ('127.0.0.1', 'localhost', '::1')
            client_display = f"localhost:{addr[1]}" if is_local else f"{addr[0]}:{addr[1]}"

            self.lobby.add_client(client_sock)
            logger.info(f"New client connected from {client_display}")

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sedma Bere Tri - Game Server")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", DEFAULT_PORT)),
                        help="Port to listen on")
    args = parser.parse_args()

    server = MultiRoomServer(args.port)
    server.start()