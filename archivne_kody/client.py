import pygame
import socket
import json
import select
import struct
import threading
import errno
import time
import zlib
from typing import List, Optional, Tuple, Dict
from queue import Queue
from card import Card
import re
import logging

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 720
NETWORK_EVENT = pygame.USEREVENT + 1
HIGHLIGHT_COLOR = (255, 0, 0)
WAITING_TIMEOUT = 30
CLICK_DEBOUNCE_MS = 200
PORT = 65432
CARD_WIDTH, CARD_HEIGHT = 80, 142
CARD_HIGHLIGHT_THICKNESS = 3
BACKGROUND_COLOR = (0, 0, 0)
BUTTON_COLOR = (100, 100, 100)
BUTTON_HOVER_COLOR = (150, 150, 150)
TEXT_COLOR = (255, 255, 255)
PLACEHOLDER_COLOR = (150, 150, 150)
LEADERBOARD_DURATION = 10

class CardSprite(pygame.sprite.Sprite):
    def __init__(self, card: Card, x: int, y: int, angle: float):
        super().__init__()
        self.card = card
        self.image = pygame.transform.rotate(card.image, angle)
        self.rect = self.image.get_rect(topleft=(x, y))
        self.angle = angle

class LayoutManager:
    def __init__(self, screen_width: int, screen_height: int):
        self.positions = [
            {"x": screen_width // 2, "y": screen_height - 172, "angle": 0, "offset": 84},
            {"x": screen_width - 172, "y": screen_height // 2, "angle": -90, "offset": 84},
            {"x": screen_width // 2, "y": 30, "angle": 180, "offset": 84},
            {"x": 30, "y": screen_height // 2, "angle": 90, "offset": 84}
        ]
        self.draw_pile_rect = pygame.Rect(screen_width // 2 - 53, screen_height // 2 - 53, CARD_WIDTH + 6, CARD_HEIGHT + 6)
        self.discard_pile_pos = (screen_width // 2 + 50, screen_height // 2 - 50)
        self.name_positions = [
            {"x": screen_width // 2, "y": screen_height - 190, "align": "center", "angle": 0},
            {"x": screen_width - 190, "y": screen_height // 2, "align": "center", "angle": 90},
            {"x": screen_width // 2, "y": 190, "align": "center", "angle": 180},
            {"x": 190, "y": screen_height // 2, "align": "center", "angle": -90}
        ]
        self.status_positions = [
            {"x": screen_width // 2, "y": screen_height - 150, "align": "center", "angle": 0},
            {"x": screen_width - 150, "y": screen_height // 2, "align": "center", "angle": 90},
            {"x": screen_width // 2, "y": 230, "align": "center", "angle": 180},
            {"x": 150, "y": screen_height // 2, "align": "center", "angle": -90}
        ]

    def get_player_position(self, pos_index: int, num_cards: int, card_index: int) -> Tuple[int, int, float]:
        pos = self.positions[pos_index]
        x = pos["x"] - (num_cards * pos["offset"] // 2) + (card_index * pos["offset"]) if pos_index in [0, 2] else pos["x"]
        y = pos["y"] - (num_cards * pos["offset"] // 2) + (card_index * pos["offset"]) if pos_index in [1, 3] else pos["y"]
        return x, y, pos["angle"]

class Client:
    def __init__(self):
        pygame.init()
        if not pygame.font.get_init():
            pygame.font.init()
        self.font = pygame.font.SysFont("Times New Roman", 28)
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption("Sedma bere tri")
        self.background = self._load_background("assets/background_green.png")
        self.card_back = self._load_image("assets/default/back.png", (CARD_WIDTH, CARD_HEIGHT))
        self.client_socket = None
        self.message_queue = Queue()
        self.state = "menu"
        self.local_player: Optional[int] = None
        self.game_state: Optional[Dict] = None
        self.card_sprites: Dict[int, pygame.sprite.Group] = {i: pygame.sprite.Group() for i in range(4)}
        self.card_rects: List[pygame.Rect] = []
        self.last_click_time: int = 0
        self.waiting_message: Optional[str] = None
        self.waiting_start: float = time.time()
        self.running: bool = True
        self.layout = LayoutManager(SCREEN_WIDTH, SCREEN_HEIGHT)
        self.card_cache: Dict[str, Card] = {}
        self.ip_input = ""
        self.ip_input_active = False
        self.connect_button_rect = pygame.Rect(SCREEN_WIDTH // 2 - 100, SCREEN_HEIGHT // 2 + 50, 200, 50)
        self.leave_button_rect = pygame.Rect(200, SCREEN_HEIGHT - 30, 90, 28)
        self.close_button_rect = pygame.Rect(SCREEN_WIDTH // 2 - 100, SCREEN_HEIGHT // 2 + 120, 200, 50)
        self.ip_input_rect = pygame.Rect(SCREEN_WIDTH // 2 - 150, SCREEN_HEIGHT // 2 - 50, 300, 50)
        self.leaderboard_start: float = 0
        self.leaderboard_data: Optional[List[Dict]] = None
        self.player_status: Dict[int, str] = {i: "" for i in range(4)}
        suits = ["♥", "♦", "♣", "♠"]
        values = list(range(7, 15))
        card_names = [f"{value}{suit}" for suit in suits for value in values] + ["back"]
        Card.preload_images(card_names)

    def _load_background(self, path: str) -> pygame.Surface:
        try:
            img = pygame.image.load(path)
            if img.get_size() != (SCREEN_WIDTH, SCREEN_HEIGHT):
                img = pygame.transform.scale(img, (SCREEN_WIDTH, SCREEN_HEIGHT))
            return img
        except pygame.error as e:
            logger.error(f"Error loading background {path}: {e}")
            return pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)

    def _load_image(self, path: str, size: Tuple[int, int]) -> pygame.Surface:
        try:
            return pygame.transform.scale(pygame.image.load(path), size)
        except pygame.error as e:
            logger.error(f"Error loading image {path}: {e}")
            return pygame.Surface(size)

    def _connect(self, host: str) -> Optional[socket.socket]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.connect((host, PORT))
        except socket.error as e:
            if e.errno not in (errno.EWOULDBLOCK, 10035):
                logger.error(f"Connection failed: {e}")
                sock.close()
                return None
        try:
            _, writable, _ = select.select([], [sock], [], 5.0)
            if writable and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                return sock
            else:
                sock.close()
                return None
        except socket.error as e:
            logger.error(f"Connection failed: {e}")
            sock.close()
            return None

    def _disconnect(self) -> None:
        if self.client_socket:
            try:
                self.client_socket.close()
            except socket.error as e:
                logger.error(f"Error closing socket: {e}")
            self.client_socket = None
        self.state = "menu"
        self.local_player = None
        self.game_state = None
        self.card_sprites = {i: pygame.sprite.Group() for i in range(4)}
        self.card_rects = []
        self.waiting_message = None
        self.waiting_start = time.time()
        self.message_queue = Queue()
        self.leaderboard_start = 0
        self.leaderboard_data = None
        self.player_status = {i: "" for i in range(4)}

    def _validate_ip(self, ip: str) -> bool:
        pattern = r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$'
        return bool(re.match(pattern, ip))

    def send_message(self, message: dict) -> bool:
        if not self.client_socket:
            return False
        try:
            data = zlib.compress(json.dumps(message, separators=(',', ':')).encode())
            self.client_socket.sendall(struct.pack('!I', len(data)) + data)
            return True
        except socket.error as e:
            logger.error(f"Send error: {e}")
            return False

    def receive_message(self, retries: int = 3, delay: float = 0.1) -> Optional[dict]:
        buffer = b""
        for _ in range(retries):
            try:
                if len(buffer) < 4:
                    packet = self.client_socket.recv(4)
                    if not packet:
                        return None
                    buffer += packet
                length = struct.unpack('!I', buffer[:4])[0]
                while len(buffer) - 4 < length:
                    packet = self.client_socket.recv(length - (len(buffer) - 4))
                    if not packet:
                        return None
                    buffer += packet
                data = json.loads(zlib.decompress(buffer[4:]).decode())
                return data
            except socket.error as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(delay)
                    continue
                logger.error(f"Receive error: {e}")
                return None
            except (json.JSONDecodeError, struct.error, zlib.error) as e:
                logger.error(f"Decode error: {e}")
                return None
        return None

    def validate_game_state(self, state: dict) -> bool:
        try:
            if not all(k in state for k in ["players", "current_player", "draw_pile", "discard_pile"]):
                return False
            if not (isinstance(state["players"], list) and len(state["players"]) == 4):
                return False
            for i, hand in enumerate(state["players"]):
                if not isinstance(hand, list):
                    logger.error(f"Invalid hand for player {i}")
                    return False
                for card in hand:
                    if not all(k in card for k in ["name", "value", "suit"]):
                        logger.error(f"Invalid card in player {i}: {card}")
                        return False
            return 0 <= state["current_player"] < 4
        except Exception as e:
            logger.error(f"Validation error: {e}")
            return False

    def update_card_sprites(self) -> None:
        if not self.game_state or self.local_player is None:
            return
        for i in range(4):
            hand = self.game_state["players"][i]
            if not hand:
                self.card_sprites[i].empty()
                continue
            pos_index = (i - self.local_player) % 4
            current_sprites = list(self.card_sprites[i].sprites())
            if len(current_sprites) != len(hand):
                self.card_sprites[i].empty()
                for j, card_data in enumerate(hand):
                    card_key = card_data["name"]
                    if card_key not in self.card_cache:
                        self.card_cache[card_key] = Card(card_data["name"], card_data["value"], card_data["suit"])
                    card = self.card_cache[card_key]
                    x, y, angle = self.layout.get_player_position(pos_index, len(hand), j)
                    sprite = CardSprite(card if i == self.local_player else self.card_cache.get("back", Card("back", 0, "")), x, y, angle)
                    self.card_sprites[i].add(sprite)

    def run(self) -> None:
        threading.Thread(target=self._listen, daemon=True).start()
        clock = pygame.time.Clock()
        dirty_rects = []

        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == NETWORK_EVENT:
                    self._handle_network_event(event.message)
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1:
                        self._handle_click(event.pos)
                elif event.type == pygame.KEYDOWN and self.state == "menu" and self.ip_input_active:
                    if event.key == pygame.K_RETURN:
                        self._handle_connect()
                    elif event.key == pygame.K_BACKSPACE:
                        self.ip_input = self.ip_input[:-1]
                    elif event.unicode.isprintable() and len(self.ip_input) < 15:
                        self.ip_input += event.unicode

            while not self.message_queue.empty():
                self._handle_network_event(self.message_queue.get())

            if self.state == "leaderboard" and time.time() - self.leaderboard_start > LEADERBOARD_DURATION:
                self._disconnect()

            dirty_rects.clear()
            try:
                if self.state == "menu":
                    dirty_rects.extend(self._render_menu())
                elif self.state == "leaderboard":
                    dirty_rects.extend(self._render_leaderboard())
                else:
                    dirty_rects.extend(self._render())
                pygame.display.update([rect for rect in dirty_rects if isinstance(rect, pygame.Rect)])
            except Exception as e:
                logger.error(f"Render loop error: {e}")
                continue
            clock.tick(60)

        self._cleanup()

    def _listen(self) -> None:
        while self.running and self.client_socket:
            message = self.receive_message()
            if message:
                self.message_queue.put(message)
            time.sleep(0.01)

    def _handle_network_event(self, message: dict) -> None:
        self.waiting_start = time.time()
        msg_type = message.get("t")
        if msg_type == "dc":
            self.waiting_message = "Disconnected from server"
            self.state = "game_over"
            if self.local_player is not None:
                self.player_status[self.local_player] = "Player Left"
            self._disconnect()
        elif msg_type == "w":
            self.local_player = message.get("pid")
            self.state = "waiting"
        elif msg_type == "wt":
            self.waiting_message = f"Waiting for {message.get('n', 0)} player(s)..."
            self.state = "waiting"
        elif msg_type == "waiting":
            self.waiting_message = message.get("msg", "Waiting for current game to end...")
            self.state = "waiting"
        elif msg_type == "gs":
            if self.validate_game_state(message):
                self.game_state = message
                self.waiting_message = None
                self.state = "playing"
                self.update_card_sprites()
            else:
                logger.error("Invalid game state")
                self.waiting_message = "Invalid game state received"
        elif msg_type == "e":
            self.waiting_message = f"Error: {message.get('msg', 'Unknown error')}"
            if message.get("msg") == "Game in progress":
                self.state = "waiting"
                if self.client_socket:
                    try:
                        self.client_socket.close()
                    except socket.error as e:
                        logger.error(f"Error closing socket: {e}")
                    self.client_socket = None
            elif message.get("msg") in ["Invalid card play", "Cannot draw card: draw pile empty"]:
                self.state = "playing"
            else:
                self.state = "game_over"
                self._disconnect()
        elif msg_type == "go":
            self.waiting_message = f"Game over! Winner: Player {message.get('w', 'none')}"
            self.state = "leaderboard"
            self.leaderboard_data = []
            for entry in message.get("results", []):
                pid = entry.get("pid", 0)
                status = self.player_status.get(pid, "")
                if entry.get("cards_left", -1) == 0 and status != "Player Left":
                    self.leaderboard_data.append({
                        "pid": pid,
                        "rank": 1,
                        "status": status
                    })
                    break
            self.leaderboard_start = time.time()
            self.game_state = None
            self.card_sprites = {i: pygame.sprite.Group() for i in range(4)}

    def _handle_connect(self) -> None:
        if not self._validate_ip(self.ip_input):
            self.waiting_message = "Invalid IP address"
            return
        self.client_socket = self._connect(self.ip_input)
        if not self.client_socket:
            self.waiting_message = f"Failed to connect to {self.ip_input}"
        else:
            self.state = "waiting"
            self.waiting_message = "Connecting..."
            threading.Thread(target=self._listen, daemon=True).start()

    def _handle_click(self, pos: Tuple[int, int]) -> None:
        current_time = pygame.time.get_ticks()
        if current_time - self.last_click_time < CLICK_DEBOUNCE_MS:
            return
        self.last_click_time = current_time

        if self.state == "menu":
            if self.ip_input_rect.collidepoint(pos):
                self.ip_input_active = True
            else:
                self.ip_input_active = False
            if self.connect_button_rect.collidepoint(pos):
                self._handle_connect()
            elif self.close_button_rect.collidepoint(pos):
                self.running = False
        elif self.state in ["playing", "waiting", "game_over", "leaderboard"]:
            if self.leave_button_rect.collidepoint(pos):
                self._disconnect()
            elif self.state == "playing" and self.local_player == self.game_state.get("current_player", -1):
                for i, sprite in enumerate(self.card_sprites[self.local_player].sprites()):
                    if sprite.rect.collidepoint(pos):
                        self.send_message({"t": "p", "ci": i})
                        return
                if self.layout.draw_pile_rect.collidepoint(pos):
                    self.send_message({"t": "d"})

    def _render_menu(self) -> List[pygame.Rect]:
        dirty_rects = []
        self.screen.fill(BACKGROUND_COLOR)
        self.screen.blit(self.background, (0, 0))
        dirty_rects.append(pygame.Rect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT))

        pygame.draw.rect(self.screen, BUTTON_COLOR if not self.ip_input_active else BUTTON_HOVER_COLOR, self.ip_input_rect)
        if self.ip_input:
            ip_surface = self.font.render(self.ip_input, True, TEXT_COLOR)
        else:
            ip_surface = self.font.render("Enter IP" if not self.ip_input_active else "", True, PLACEHOLDER_COLOR if not self.ip_input_active else TEXT_COLOR)
        ip_rect = ip_surface.get_rect(center=self.ip_input_rect.center)
        self.screen.blit(ip_surface, ip_rect)
        dirty_rects.append(self.ip_input_rect)

        mouse_pos = pygame.mouse.get_pos()
        connect_color = BUTTON_HOVER_COLOR if self.connect_button_rect.collidepoint(mouse_pos) else BUTTON_COLOR
        pygame.draw.rect(self.screen, connect_color, self.connect_button_rect)
        connect_text = self.font.render("Connect", True, TEXT_COLOR)
        connect_text_rect = connect_text.get_rect(center=self.connect_button_rect.center)
        self.screen.blit(connect_text, connect_text_rect)
        dirty_rects.append(self.connect_button_rect)

        close_color = BUTTON_HOVER_COLOR if self.close_button_rect.collidepoint(mouse_pos) else BUTTON_COLOR
        pygame.draw.rect(self.screen, close_color, self.close_button_rect)
        close_text = self.font.render("Close game", True, TEXT_COLOR)
        close_text_rect = close_text.get_rect(center=self.close_button_rect.center)
        self.screen.blit(close_text, close_text_rect)
        dirty_rects.append(self.close_button_rect)

        if self.waiting_message:
            text = self.font.render(self.waiting_message, True, TEXT_COLOR)
            text_rect = text.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 240))
            self.screen.blit(text, text_rect)
            dirty_rects.append(text_rect)

        return dirty_rects

    def _render_leaderboard(self) -> List[pygame.Rect]:
        dirty_rects = []
        self.screen.fill(BACKGROUND_COLOR)
        self.screen.blit(self.background, (0, 0))
        dirty_rects.append(pygame.Rect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT))

        title = self.font.render("Game Over", True, TEXT_COLOR)
        title_rect = title.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 4))
        self.screen.blit(title, title_rect)
        dirty_rects.append(title_rect)

        if self.leaderboard_data:
            for i, entry in enumerate(self.leaderboard_data):
                player_id = entry.get("pid", 0) + 1
                text = f"Winner: Player {player_id}"
                text_surface = self.font.render(text, True, TEXT_COLOR)
                text_rect = text_surface.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 3 + (i + 1) * 50))
                self.screen.blit(text_surface, text_rect)
                dirty_rects.append(text_rect)

        mouse_pos = pygame.mouse.get_pos()
        return_color = BUTTON_HOVER_COLOR if self.leave_button_rect.collidepoint(mouse_pos) else BUTTON_COLOR
        pygame.draw.rect(self.screen, return_color, self.leave_button_rect)
        return_text = self.font.render("Leave", True, TEXT_COLOR)
        return_text_rect = return_text.get_rect(center=self.leave_button_rect.center)
        self.screen.blit(return_text, return_text_rect)
        dirty_rects.append(self.leave_button_rect)

        return dirty_rects

    def _render(self) -> List[pygame.Rect]:
        dirty_rects = []
        try:
            self.screen.fill(BACKGROUND_COLOR)
            self.screen.blit(self.background, (0, 0))
            dirty_rects.append(pygame.Rect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT))

            mouse_pos = pygame.mouse.get_pos()
            return_color = BUTTON_HOVER_COLOR if self.leave_button_rect.collidepoint(mouse_pos) else BUTTON_COLOR
            pygame.draw.rect(self.screen, return_color, self.leave_button_rect)
            return_text = self.font.render("Leave", True, TEXT_COLOR)
            return_text_rect = return_text.get_rect(center=self.leave_button_rect.center)
            self.screen.blit(return_text, return_text_rect)
            dirty_rects.append(self.leave_button_rect)

            if self.state in ["waiting", "game_over"] and self.waiting_message:
                text = self.font.render(self.waiting_message, True, TEXT_COLOR)
                text_rect = text.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))
                self.screen.blit(text, text_rect)
                dirty_rects.append(text_rect)

            elif self.state == "playing" and self.game_state and self.local_player is not None:
                current_player = self.game_state.get("current_player", 0)

                for i in range(4):
                    if self.card_sprites[i]:
                        self.card_sprites[i].draw(self.screen)
                        if i == self.local_player and current_player == self.local_player:
                            for sprite in self.card_sprites[i]:
                                if sprite.rect.collidepoint(mouse_pos):
                                    pygame.draw.rect(self.screen, (0, 0, 0), sprite.rect, CARD_HIGHLIGHT_THICKNESS)
                                    dirty_rects.append(sprite.rect.inflate(CARD_HIGHLIGHT_THICKNESS * 2, CARD_HIGHLIGHT_THICKNESS * 2))
                        dirty_rects.extend([sprite.rect for sprite in self.card_sprites[i]])

                if self.layout.draw_pile_rect.collidepoint(mouse_pos):
                    pygame.draw.rect(self.screen, (0, 0, 0), self.layout.draw_pile_rect, CARD_HIGHLIGHT_THICKNESS)
                    dirty_rects.append(self.layout.draw_pile_rect.inflate(CARD_HIGHLIGHT_THICKNESS * 2, CARD_HIGHLIGHT_THICKNESS * 2))
                if self.game_state.get("draw_pile"):
                    self.screen.blit(self.card_back, (self.layout.draw_pile_rect.topleft[0] + 3, self.layout.draw_pile_rect.topleft[1] + 3))
                    dirty_rects.append(self.layout.draw_pile_rect)

                if self.game_state.get("discard_pile"):
                    card_key = self.game_state["discard_pile"][-1]["name"]
                    if card_key not in self.card_cache:
                        self.card_cache[card_key] = Card(
                            self.game_state["discard_pile"][-1]["name"],
                            self.game_state["discard_pile"][-1]["value"],
                            self.game_state["discard_pile"][-1]["suit"]
                        )
                    self.card_cache[card_key].draw(self.screen, *self.layout.discard_pile_pos)
                    dirty_rects.append(pygame.Rect(self.layout.discard_pile_pos, (CARD_WIDTH, CARD_HEIGHT)))

                for i in range(4):
                    pos_index = (i - self.local_player) % 4
                    name_pos = self.layout.name_positions[pos_index]
                    name_color = HIGHLIGHT_COLOR if i == current_player else TEXT_COLOR
                    name_text = self.font.render(f"Player {i + 1}", True, name_color)
                    rotated_name = pygame.transform.rotate(name_text, name_pos["angle"])
                    name_rect = rotated_name.get_rect(center=(name_pos["x"], name_pos["y"]))
                    self.screen.blit(rotated_name, name_rect)
                    dirty_rects.append(name_rect)

                    if self.player_status[i]:
                        status_pos = self.layout.status_positions[pos_index]
                        status_text = self.font.render(self.player_status[i], True, TEXT_COLOR)
                        rotated_status = pygame.transform.rotate(status_text, status_pos["angle"])
                        status_rect = rotated_status.get_rect(center=(status_pos["x"], status_pos["y"]))
                        self.screen.blit(rotated_status, status_rect)
                        dirty_rects.append(status_rect)

        except Exception as e:
            logger.error(f"Render error: {e}")
        return dirty_rects

    def _cleanup(self) -> None:
        self._disconnect()
        pygame.quit()

if __name__ == "__main__":
    client = Client()
    client.run()