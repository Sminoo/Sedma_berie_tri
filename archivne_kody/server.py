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
from typing import Dict, List
from game_logic import Game
import logging
from datetime import datetime

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

DEFAULT_PORT = 65432
HOST = '0.0.0.0'

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

class Server:
    def __init__(self, port: int):
        self.sel = selectors.DefaultSelector()
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server_socket.bind((HOST, port))
        except socket.error as e:
            logger.error(f"Failed to bind to {HOST}:{port}: {e}")
            sys.exit(1)
        self.server_socket.listen(4)
        self.server_socket.setblocking(False)
        self.sel.register(self.server_socket, selectors.EVENT_READ, self._accept_client)
        self.clients: Dict[socket.socket, int] = {}
        self.sockets: List[socket.socket] = []
        self.player_slots: List[bool] = [False, False, False, False]  # Track which slots are occupied
        self.game: Game = None
        self.last_game_state: dict = None
        self.player_count: int = 0
        self.state_cache: Dict[str, bytes] = {}
        self.finish_order: List[int] = []
        signal.signal(signal.SIGINT, self._signal_handler)
        local_ip = get_local_ip()
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Server started on {local_ip}:{port} and localhost:{port}")

    def _signal_handler(self, sig, frame):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Shutting down server...")
        for sock in list(self.clients.keys()):
            sock.close()
        self.server_socket.close()
        self.sel.close()
        sys.exit(0)

    def send_message(self, sock: socket.socket, message: dict, retries: int = 2) -> bool:
        try:
            data = zlib.compress(json.dumps(message, separators=(',', ':')).encode())
            sock.sendall(struct.pack('!I', len(data)) + data)
            return True
        except socket.error as e:
            logger.error(f"Send error to player {self.clients.get(sock, 'unknown')}: {e}")
            if retries > 0:
                time.sleep(0.1)
                return self.send_message(sock, message, retries - 1)
            return False

    def receive_message(self, sock: socket.socket) -> dict:
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
        while True:
            events = self.sel.select(timeout=0.1)
            for key, mask in events:
                callback = key.data
                callback(key.fileobj, mask)
            if self.game and self.game.check_game_over():
                self._end_game()

    def _get_available_slot(self) -> int:
        """Get the next available player slot (0-3)"""
        for i in range(4):
            if not self.player_slots[i]:
                return i
        return -1  # No slots available

    def _accept_client(self, sock: socket.socket, mask: int) -> None:
        try:
            client_sock, addr = sock.accept()
            client_sock.setblocking(False)
            self.sel.register(client_sock, selectors.EVENT_READ, self._handle_client)
            
            # Normalize IP address (treat 127.0.0.1 and localhost the same as LAN IP)
            client_ip = addr[0]
            if client_ip in ['127.0.0.1', 'localhost', '::1']:
                client_display = f"localhost:{addr[1]}"
            else:
                client_display = f"{addr[0]}:{addr[1]}"
            
            if self.game:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Rejected client from {client_display}: Game in progress")
                if self.send_message(client_sock, {"t": "e", "msg": "Game in progress"}):
                    time.sleep(0.1)
                client_sock.close()
                self.sel.unregister(client_sock)
            elif self.player_count >= 4:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Rejected client from {client_display}: Game full")
                if self.send_message(client_sock, {"t": "e", "msg": "Game full"}):
                    time.sleep(0.1)
                client_sock.close()
                self.sel.unregister(client_sock)
            else:
                # Get the next available slot instead of using player_count
                slot = self._get_available_slot()
                if slot == -1:
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] No available slots for client from {client_display}")
                    if self.send_message(client_sock, {"t": "e", "msg": "Game full"}):
                        time.sleep(0.1)
                    client_sock.close()
                    self.sel.unregister(client_sock)
                    return
                
                # Assign player to the available slot
                self.clients[client_sock] = slot
                self.sockets.append(client_sock)
                self.player_slots[slot] = True
                self.player_count += 1
                
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Player {slot + 1} connected from {client_display}, total players: {self.player_count}")
                
                if self.send_message(client_sock, {"t": "w", "pid": slot}):
                    self._broadcast({"t": "wt", "n": 4 - self.player_count})
                    if self.game and self.last_game_state:
                        self.send_message(client_sock, {"t": "gs", **self.last_game_state})
                    if self.player_count == 4 and not self.game:
                        self._start_game()
                else:
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Failed to send welcome to player {slot + 1}, removing")
                    self._remove_client(client_sock)
        except socket.error as e:
            logger.error(f"Accept error: {e}")

    def _handle_client(self, sock: socket.socket, mask: int) -> None:
        if sock not in self.clients:
            return
        message = self.receive_message(sock)
        if message is None:
            self._remove_client(sock)
            return
        if self.game:
            try:
                self._handle_action(sock, message)
                self.last_game_state = self.game.serialize()
            except Exception as e:
                logger.error(f"Error handling action: {e}")
                self.send_message(sock, {"t": "e", "msg": f"Server error: {str(e)}"})

    def _remove_client(self, sock: socket.socket) -> None:
        if sock not in self.clients:
            return
        player_id = self.clients[sock]
        
        # Get client address safely
        try:
            addr = sock.getpeername()
            client_display = f"{addr[0]}:{addr[1]}"
        except:
            client_display = "unknown"
            
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Player {player_id + 1} disconnected from {client_display}")
        
        # Handle game state if game is running
        if self.game and self.game.players[player_id]:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Moving Player {player_id + 1}'s {len(self.game.players[player_id])} cards to discard pile")
            self.game.discard_pile.extendleft(self.game.players[player_id])
            self.game.players[player_id].clear()
            if self.game.current_player == player_id:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Advancing turn from Player {player_id + 1}")
                self.game.next_turn()
        
        # Clean up client references
        del self.clients[sock]
        if sock in self.sockets:
            self.sockets.remove(sock)
        
        # Free up the player slot
        if 0 <= player_id < 4:
            self.player_slots[player_id] = False
        
        self.player_count -= 1
        
        # Close and unregister socket
        try:
            sock.close()
            self.sel.unregister(sock)
        except:
            pass  # Socket might already be closed
        
        # Broadcast updated state
        if self.game:
            self._broadcast_game_state()
        else:
            self._broadcast({"t": "wt", "n": 4 - self.player_count})

    def _handle_action(self, sock: socket.socket, action: dict) -> None:
        """Handle client actions."""
        player_id = self.clients[sock]
        if action.get("t") == "p" and player_id == self.game.current_player:
            card_index = action.get("ci", -1)
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Player {player_id + 1} attempting to play card at index {card_index}")
            if self.game.play_card(player_id, card_index):
                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Player {player_id + 1} played card successfully")
                if not self.game.players[player_id] and player_id not in self.finish_order:
                    self.finish_order.append(player_id)
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Player {player_id + 1} finished (no cards left)")
                self._broadcast_game_state()
            else:
                self.send_message(sock, {"t": "e", "msg": "Invalid card play"})
        elif action.get("t") == "d" and player_id == self.game.current_player:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Player {player_id + 1} drawing card")
            if self.game.draw_card(player_id):
                self.game.next_turn()
                self._broadcast_game_state()
            else:
                self.send_message(sock, {"t": "e", "msg": "Cannot draw card: draw pile empty"})
        else:
            self.send_message(sock, {"t": "e", "msg": "Invalid action or not your turn"})

    def _start_game(self) -> None:
        self.game = Game()
        self.game.create_deck()
        self.game.deal_cards()
        self.finish_order = []
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Game started with {self.player_count} players")
        self._broadcast_game_state()
        self.last_game_state = self.game.serialize()

    def _end_game(self) -> None:
        winner = next((i for i, p in enumerate(self.game.players) if not p), None)
        results = []
        for pid in self.finish_order:
            results.append({"pid": pid, "rank": len(results) + 1, "cards_left": 0})
        remaining = [(i, len(self.game.players[i])) for i in range(4) if i not in self.finish_order]
        remaining.sort(key=lambda x: x[1])
        for pid, cards_left in remaining:
            results.append({"pid": pid, "rank": len(results) + 1, "cards_left": cards_left})
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Game over, winner: Player {winner + 1 if winner is not None else 'none'}")
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Final rankings: {results}")
        self._broadcast({"t": "go", "w": winner + 1 if winner is not None else None, "results": results})
        
        # Clean up all client connections
        for sock in list(self.clients.keys()):
            try:
                sock.close()
                self.sel.unregister(sock)
            except:
                pass  # Socket might already be closed
            if sock in self.sockets:
                self.sockets.remove(sock)
        
        # Reset server state for new game
        self.clients.clear()
        self.sockets.clear()
        self.player_slots = [False, False, False, False]
        self.player_count = 0
        self.game = None
        self.last_game_state = None
        self.finish_order = []
        self._broadcast({"t": "wt", "n": 4 - self.player_count})

    def _broadcast(self, message: dict) -> None:
        failed = []
        for sock in list(self.clients.keys()):
            if not self.send_message(sock, message):
                failed.append(sock)
        for sock in failed:
            self._remove_client(sock)

    def _broadcast_game_state(self) -> None:
        if not self.game:
            return
        current_state = self.game.serialize()
        if current_state != self.last_game_state:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                  f"Broadcasting game state: current_player={current_state['current_player'] + 1}")
            self._broadcast({"t": "gs", **current_state})
            self.last_game_state = current_state

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sedma bere tri server")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", DEFAULT_PORT)), help="Port to listen on")
    args = parser.parse_args()
    server = Server(args.port)
    server.start()