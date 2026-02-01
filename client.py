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
CUSTOMIZE_BUTTON_COLOR = (80, 80, 140)
CUSTOMIZE_HOVER_COLOR = (120, 120, 200)

ROOM_ITEM_COLOR = (60, 60, 80, 15)
ROOM_ITEM_HOVER_COLOR = (80, 80, 100)
SUCCESS_COLOR = (0, 200, 0)
ERROR_COLOR = (255, 100, 100)


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
        self.draw_pile_rect = pygame.Rect(screen_width // 2 - 53, screen_height // 2 - 53, CARD_WIDTH + 6,
                                          CARD_HEIGHT + 6)
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

    def get_player_position(self, pos_index: int, num_cards: int, card_index: int, is_local: bool = False) -> Tuple[int, int, float]:
        pos = self.positions[pos_index]
        base_offset = 84
        if is_local:
            if num_cards <= 8:
                offset = base_offset
            else:
                overlap_increase = (num_cards - 8) * 3
                offset = max(35, base_offset - overlap_increase)
        else:
            overlap_increase = max(0, (num_cards - 3) * 4)
            offset = max(30, base_offset - overlap_increase)

        x = pos["x"] - (num_cards * offset // 2) + (card_index * offset) if pos_index in [0, 2] else pos["x"]
        y = pos["y"] - (num_cards * offset // 2) + (card_index * offset) if pos_index in [1, 3] else pos["y"]
        return x, y, pos["angle"]


class NetworkManager:
    def __init__(self, port: int = PORT):
        self.client_socket: Optional[socket.socket] = None
        self.message_queue: Queue = Queue()
        self.port = port

    def connect(self, host: str) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.connect((host, self.port))
        except socket.error as e:
            if e.errno not in (errno.EWOULDBLOCK, 10035):
                logger.error(f"Connection failed: {e}")
                sock.close()
                return False
        try:
            _, writable, _ = select.select([], [sock], [], 5.0)
            if writable and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                self.client_socket = sock
                return True
            else:
                sock.close()
                return False
        except socket.error as e:
            logger.error(f"Connection failed: {e}")
            sock.close()
            return False

    def disconnect(self) -> None:
        if self.client_socket:
            try:
                self.client_socket.close()
            except socket.error as e:
                logger.error(f"Error closing socket: {e}")
            self.client_socket = None
        self.message_queue = Queue()

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
        sock = self.client_socket
        if sock is None:
            return None
        buffer = b""
        for _ in range(retries):
            try:
                if len(buffer) < 4:
                    packet = sock.recv(4)
                    if not packet:
                        return None
                    buffer += packet
                length = struct.unpack('!I', buffer[:4])[0]
                while len(buffer) - 4 < length:
                    packet = sock.recv(length - (len(buffer) - 4))
                    if not packet:
                        return None
                    buffer += packet
                data = json.loads(zlib.decompress(buffer[4:]).decode())
                return data
            except socket.error as e:
                if hasattr(e, 'winerror') and e.winerror == 10038:
                    return None
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(delay)
                    continue
                logger.error(f"Receive error: {e}")
                return None
            except (json.JSONDecodeError, struct.error, zlib.error) as e:
                logger.error(f"Decode error: {e}")
                return None
        return None

    def start_listener(self, running_flag, message_callback):
        def listen():
            while running_flag[0]:
                if self.client_socket is None:
                    break
                message = self.receive_message()
                if message:
                    self.message_queue.put(message)
                time.sleep(0.01)

        threading.Thread(target=listen, daemon=True).start()


class UIElement:
    def __init__(self, rect: pygame.Rect, text: str, font: pygame.font.Font, bg_color: Tuple[int, int, int]):
        self.rect = rect
        self.text = text
        self.font = font
        self.bg_color = bg_color
        self.surface = self.font.render(text, True, TEXT_COLOR)

    def draw(self, screen: pygame.Surface, mouse_pos: Tuple[int, int]) -> None:
        hover_color = BUTTON_HOVER_COLOR if self.rect.collidepoint(mouse_pos) else self.bg_color
        pygame.draw.rect(screen, hover_color, self.rect)
        text_rect = self.surface.get_rect(center=self.rect.center)
        screen.blit(self.surface, text_rect)

    def update_text(self, new_text: str) -> None:
        self.text = new_text
        self.surface = self.font.render(new_text, True, TEXT_COLOR)


class InputField:
    def __init__(self, rect: pygame.Rect, placeholder: str, font: pygame.font.Font, bg_color: Tuple[int, int, int],
                 max_len: int = 20):
        self.rect = rect
        self.placeholder = placeholder
        self.font = font
        self.bg_color = bg_color
        self.text = ""
        self.active = False
        self.max_len = max_len

    def draw(self, screen: pygame.Surface, mouse_pos: Tuple[int, int]) -> None:
        bg_color = BUTTON_HOVER_COLOR if self.active else self.bg_color
        pygame.draw.rect(screen, bg_color, self.rect)
        display_text = self.text or self.placeholder
        text_color = TEXT_COLOR if self.text else PLACEHOLDER_COLOR
        surface = self.font.render(display_text, True, text_color)
        text_rect = surface.get_rect(center=self.rect.center)
        screen.blit(surface, text_rect)

    def handle_key(self, event) -> bool:
        if not self.active:
            return False
        if event.key == pygame.K_RETURN:
            return True
        elif event.key == pygame.K_BACKSPACE:
            self.text = self.text[:-1]
            return True
        elif event.unicode.isprintable() and len(self.text) < self.max_len:
            self.text += event.unicode
            return True
        return False


class StateManager:
    def __init__(self):
        self.state = "menu"
        self.local_player: Optional[int] = None
        self.game_state: Optional[Dict] = None
        self.player_names: Dict[int, str] = {}
        self.num_players: int = 4               # ← added - default value
        self.waiting_message: Optional[str] = None
        self.waiting_start: float = time.time()
        self.leaderboard_start: float = 0
        self.leaderboard_data: Optional[List[Dict]] = None
        self.player_status: Dict[int, str] = {i: "" for i in range(4)}
        self.render_debug_done = False


class Renderer:
    def __init__(self, screen: pygame.Surface, layout: LayoutManager, font: pygame.font.Font,
                 title_font: pygame.font.Font, small_font: pygame.font.Font):
        self.screen = screen
        self.layout = layout
        self.font = font
        self.title_font = title_font
        self.small_font = small_font
        self.background: Optional[pygame.Surface] = None
        self.card_back: Optional[pygame.Surface] = None


        # Customization defaults
        self.background_options = [
            "background_green.png",
            "background_blue.png",
            "background_red.png",
        ]
        self.card_back_themes = [
            "default",
            "pixel"
        ]

        self.selected_background = self.background_options[0]
        self.selected_card_theme = self.card_back_themes[0]
        self.current_background_path = f"assets/backgrounds/{self.selected_background}"
        self.current_card_back_path = f"assets/cards/{self.selected_card_theme}/back.png"

    def render_customize(self, mouse_pos: Tuple[int, int]) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

        title = self.title_font.render("Customize", True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(SCREEN_WIDTH // 2, 80)))

        # Background section
        bg_title = self.font.render("Background", True, TEXT_COLOR)
        self.screen.blit(bg_title, (120, 160))

        y = 210
        for bg in self.background_options:
            rect = pygame.Rect(100, y, 340, 45)
            color = CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR
            if bg == self.selected_background:
                color = (0, 180, 0)
            pygame.draw.rect(self.screen, color, rect, border_radius=8)
            label = self.font.render(bg.replace(".png", "").replace("_", " ").title(), True, TEXT_COLOR)
            self.screen.blit(label, (130, y + 10))
            y += 55

        # Card back section
        card_title = self.font.render("Card Back", True, TEXT_COLOR)
        self.screen.blit(card_title, (SCREEN_WIDTH // 2 + 50, 160))

        y = 210
        for theme in self.card_back_themes:
            rect = pygame.Rect(SCREEN_WIDTH // 2 + 30, y, 340, 45)
            color = CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR
            if theme == self.selected_card_theme:
                color = (0, 180, 0)
            pygame.draw.rect(self.screen, color, rect, border_radius=8)
            label = self.font.render(theme.capitalize(), True, TEXT_COLOR)
            self.screen.blit(label, (SCREEN_WIDTH // 2 + 60, y + 10))
            y += 55

        # Buttons
        apply_rect = pygame.Rect(SCREEN_WIDTH // 2 - 220, SCREEN_HEIGHT - 100, 200, 60)
        cancel_rect = pygame.Rect(SCREEN_WIDTH // 2 + 40, SCREEN_HEIGHT - 100, 200, 60)

        apply_color = BUTTON_HOVER_COLOR if apply_rect.collidepoint(mouse_pos) else BUTTON_COLOR
        cancel_color = BUTTON_HOVER_COLOR if cancel_rect.collidepoint(mouse_pos) else BUTTON_COLOR

        pygame.draw.rect(self.screen, apply_color, apply_rect, border_radius=10)
        pygame.draw.rect(self.screen, cancel_color, cancel_rect, border_radius=10)

        apply_txt = self.font.render("Apply & Return", True, TEXT_COLOR)
        cancel_txt = self.font.render("Cancel", True, TEXT_COLOR)

        self.screen.blit(apply_txt, apply_txt.get_rect(center=apply_rect.center))
        self.screen.blit(cancel_txt, cancel_txt.get_rect(center=cancel_rect.center))

    def load_assets(self, background_path: str, card_back_path: str, size: Tuple[int, int]) -> None:
        try:
            self.background = pygame.image.load(background_path)
            if self.background.get_size() != (SCREEN_WIDTH, SCREEN_HEIGHT):
                self.background = pygame.transform.scale(self.background, (SCREEN_WIDTH, SCREEN_HEIGHT))
        except pygame.error as e:
            logger.warning(f"Failed to load background {background_path}: {e}")
            self.background = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT))
            self.background.fill((20, 60, 20))

        try:
            self.card_back = pygame.transform.scale(pygame.image.load(card_back_path), size)
        except pygame.error as e:
            logger.warning(f"Failed to load card back {card_back_path}: {e}")
            self.card_back = pygame.Surface(size)
            self.card_back.fill((180, 30, 30))

    def render_menu(self, ip_field: InputField, name_field: InputField, connect_btn: UIElement, close_btn: UIElement,
                    waiting_message: Optional[str]) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

        title = self.title_font.render("Sedma Bere Tri", True, TEXT_COLOR)
        title_rect = title.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 250))
        self.screen.blit(title, title_rect)

        ip_label = self.font.render("Server IP:", True, TEXT_COLOR)
        self.screen.blit(ip_label, (ip_field.rect.x, ip_field.rect.y - 30))
        ip_field.draw(self.screen, pygame.mouse.get_pos())

        name_label = self.font.render("Your Name:", True, TEXT_COLOR)
        self.screen.blit(name_label, (name_field.rect.x, name_field.rect.y - 30))
        name_field.draw(self.screen, pygame.mouse.get_pos())

        mouse_pos = pygame.mouse.get_pos()
        connect_btn.draw(self.screen, mouse_pos)
        close_btn.draw(self.screen, mouse_pos)

        if waiting_message:
            msg_color = ERROR_COLOR if "error" in waiting_message.lower() or "please" in waiting_message.lower() else TEXT_COLOR
            msg_surface = self.font.render(waiting_message, True, msg_color)
            msg_rect = msg_surface.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 120))
            self.screen.blit(msg_surface, msg_rect)

    def render_lobby(self, background_path: str, player_name: str, room_name_field: InputField, create_btn: UIElement,
                     refresh_btn: UIElement,
                     disconnect_btn: UIElement, rooms_list: List[Dict], waiting_message: Optional[str]) -> None:
        # existing setup...
        self.background = pygame.image.load(background_path)
        self.screen.blit(self.background, (0, 0))

        title = self.title_font.render(f"Playing as: {player_name}", True, TEXT_COLOR)
        self.screen.blit(title, (50, 20))

        create_label = self.font.render("Create Room:", True, TEXT_COLOR)
        self.screen.blit(create_label, (50, 120))
        room_name_field.draw(self.screen, pygame.mouse.get_pos())

        mouse_pos = pygame.mouse.get_pos()
        create_btn.draw(self.screen, mouse_pos)
        refresh_btn.draw(self.screen, mouse_pos)

        # Player count selector (2 / 3 / 4) — moved down to y=210
        pc_base_x = 50
        pc_y = 210
        pc_width = 40
        pc_height = 30
        for i, val in enumerate([2, 3, 4]):
            rect = pygame.Rect(pc_base_x + i * 50, pc_y, pc_width, pc_height)
            is_selected = getattr(self, "selected_room_max_players", 4) == val
            color = (0, 180, 0) if is_selected else (
                CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR)
            pygame.draw.rect(self.screen, color, rect, border_radius=6)
            label = self.small_font.render(str(val), True, TEXT_COLOR)
            self.screen.blit(label, label.get_rect(center=rect.center))

        self._render_room_list(rooms_list)

        disconnect_btn.draw(self.screen, mouse_pos)

        if waiting_message:
            msg_color = ERROR_COLOR if "error" in waiting_message.lower() else SUCCESS_COLOR if "joined" in waiting_message.lower() else TEXT_COLOR
            msg_surface = self.font.render(waiting_message, True, msg_color)
            msg_rect = msg_surface.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT - 100))
            self.screen.blit(msg_surface, msg_rect)

    def _render_room_list(self, rooms_list: List[Dict]) -> None:
        list_title = self.font.render("Available Rooms (click to join):", True, TEXT_COLOR)
        self.screen.blit(list_title, (300, 120))

        rooms_list_rect = pygame.Rect(300, 150, SCREEN_WIDTH - 350, SCREEN_HEIGHT - 200)
        room_item_height = 80
        pygame.draw.rect(self.screen, ROOM_ITEM_COLOR, rooms_list_rect)

        if not rooms_list:
            no_rooms_text = self.font.render("No rooms available. Create one!", True, PLACEHOLDER_COLOR)
            text_rect = no_rooms_text.get_rect(center=rooms_list_rect.center)
            self.screen.blit(no_rooms_text, text_rect)
            return

        mouse_pos = pygame.mouse.get_pos()
        y_offset = 0
        details_color = (200, 200, 200)

        for i, room in enumerate(rooms_list):
            room_rect = pygame.Rect(rooms_list_rect.x + 10, rooms_list_rect.y + y_offset + 5,
                                    rooms_list_rect.width - 20, room_item_height - 5)

            hover_color = ROOM_ITEM_HOVER_COLOR if room_rect.collidepoint(mouse_pos) else ROOM_ITEM_COLOR
            if not room.get("in_game", False) and room.get("players", 0) < room.get("max_players", 4):
                pygame.draw.rect(self.screen, hover_color, room_rect)
            else:
                pygame.draw.rect(self.screen, (60, 60, 60), room_rect)

            room_name = room.get("room_name", "Unknown")
            creator = room.get("creator", "Unknown")
            players = room.get("players", 0)
            max_players = room.get("max_players", 4)
            in_game = room.get("in_game", False)

            title_text = self.font.render(room_name, True, TEXT_COLOR)
            self.screen.blit(title_text, (room_rect.x + 10, room_rect.y + 8))

            creator_text = f"by {creator}"
            creator_surface = self.small_font.render(creator_text, True, details_color)
            self.screen.blit(creator_surface, (room_rect.x + 10, room_rect.y + 32))

            players_text = f"{players}/{max_players} players"
            if in_game:
                players_text += " (IN GAME)"
            players_surface = self.small_font.render(players_text, True, details_color)
            self.screen.blit(players_surface, (room_rect.x + 10, room_rect.y + 50))

            y_offset += room_item_height

    def render_game(self, state_manager: StateManager, card_sprites: Dict[int, pygame.sprite.Group],
                    current_room_name: str, mouse_pos: Tuple[int, int], waiting_message: Optional[str]) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

        if current_room_name:
            room_info = self.title_font.render(f"Room: {current_room_name}", True, TEXT_COLOR)
            self.screen.blit(room_info, (10, 10))

        if waiting_message:
            msg_surface = self.font.render(waiting_message, True, TEXT_COLOR)
            self.screen.blit(msg_surface, (10, 50))

        leave_rect = pygame.Rect(50, SCREEN_HEIGHT - 60, 150, 40)
        leave_color = BUTTON_HOVER_COLOR if leave_rect.collidepoint(mouse_pos) else BUTTON_COLOR
        pygame.draw.rect(self.screen, leave_color, leave_rect)
        leave_text = self.font.render("Leave Room", True, TEXT_COLOR)
        leave_rect_center = leave_text.get_rect(center=leave_rect.center)
        self.screen.blit(leave_text, leave_rect_center)

        if state_manager.state == "room_waiting":
            if state_manager.waiting_message:
                wait_text = self.title_font.render(state_manager.waiting_message, True, TEXT_COLOR)
                wait_rect = wait_text.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))
                self.screen.blit(wait_text, wait_rect)
        elif state_manager.state == "playing" and state_manager.game_state and state_manager.local_player is not None:
            current_player = state_manager.game_state.get("current_player", 0)
            player_names = state_manager.game_state.get("player_names", {})

            if not state_manager.render_debug_done:
                print("[DEBUG START] Player names at game start:")
                for i in range(state_manager.num_players):
                    player_name = player_names.get(i, f"Unknown ({i + 1})")
                    print(f"  Slot {i}: '{player_name}' (from dict: {player_names.get(i, 'MISSING')})")
                state_manager.render_debug_done = True

            for i in range(state_manager.num_players):  # ← changed
                if card_sprites[i]:
                    card_sprites[i].draw(self.screen)
                    if i == state_manager.local_player and current_player == state_manager.local_player:
                        for sprite in card_sprites[i]:
                            if sprite.rect.collidepoint(mouse_pos):
                                pygame.draw.rect(self.screen, HIGHLIGHT_COLOR, sprite.rect, CARD_HIGHLIGHT_THICKNESS)

            draw_pile_rect = self.layout.draw_pile_rect
            if draw_pile_rect.collidepoint(mouse_pos):
                pygame.draw.rect(self.screen, HIGHLIGHT_COLOR, draw_pile_rect, CARD_HIGHLIGHT_THICKNESS)

            if state_manager.game_state.get("draw_pile_count", 0) > 0:
                self.screen.blit(self.card_back, (draw_pile_rect.topleft[0] + 3, draw_pile_rect.topleft[1] + 3))

            if state_manager.game_state.get("discard_pile"):
                card_key = state_manager.game_state["discard_pile"][-1]["name"]
                card = Card(card_key, state_manager.game_state["discard_pile"][-1]["value"],
                            state_manager.game_state["discard_pile"][-1]["suit"])
                card.draw(self.screen, *self.layout.discard_pile_pos)

            for i in range(state_manager.num_players):
                pos_index = (i - state_manager.local_player) % state_manager.num_players  # ← changed %4 → %num_players
                name_pos = self.layout.name_positions[pos_index]
                name_color = HIGHLIGHT_COLOR if i == current_player else TEXT_COLOR
                player_name = player_names.get(i, f"Unknown ({i + 1})")
                name_text = self.font.render(player_name, True, name_color)
                rotated_name = pygame.transform.rotate(name_text, name_pos["angle"])
                name_rect = rotated_name.get_rect(center=(name_pos["x"], name_pos["y"]))
                self.screen.blit(rotated_name, name_rect)

    def render_leaderboard(self, state_manager: StateManager, mouse_pos: Tuple[int, int]) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

        title = self.title_font.render("Game Over", True, TEXT_COLOR)
        title_rect = title.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 4))
        self.screen.blit(title, title_rect)

        player_names = state_manager.player_names
        if state_manager.leaderboard_data:
            for i, entry in enumerate(state_manager.leaderboard_data):
                player_id = entry.get("pid", 0)
                rank = entry.get("rank", i + 1)
                player_name = player_names.get(player_id, f"Unknown ({player_id + 1})")
                disconnected = entry.get("disconnected", False)
                cards_left = entry.get("cards_left", 0)
                if disconnected:
                    text = f"{rank}. {player_name} (disconnected)"
                else:
                    text = f"{rank}. {player_name}"
                text_surface = self.font.render(text, True, TEXT_COLOR)
                text_rect = text_surface.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 3 + (i + 1) * 50))
                self.screen.blit(text_surface, text_rect)

        leave_rect = pygame.Rect(50, SCREEN_HEIGHT - 60, 150, 40)
        leave_color = BUTTON_HOVER_COLOR if leave_rect.collidepoint(mouse_pos) else BUTTON_COLOR
        pygame.draw.rect(self.screen, leave_color, leave_rect)
        leave_text = self.font.render("Back to Lobby", True, TEXT_COLOR)
        leave_rect_center = leave_text.get_rect(center=leave_rect.center)
        self.screen.blit(leave_text, leave_rect_center)


class EventHandler:
    def __init__(self, network: NetworkManager, state_manager: StateManager, renderer: Renderer, layout: LayoutManager,
                 input_fields: Dict[str, InputField], ui_elements: Dict[str, UIElement]):
        self.network = network
        self.state_manager = state_manager
        self.renderer = renderer
        self.layout = layout
        self.input_fields = input_fields
        self.ui_elements = ui_elements
        self.last_click_time: int = 0
        self.card_sprites: Dict[int, pygame.sprite.Group] = {i: pygame.sprite.Group() for i in range(4)}
        self.card_rects: List[pygame.Rect] = []
        self.card_cache: Dict[str, Card] = {}
        self.player_name = ""
        self.name_set = False
        self.default_name_counter = 1
        self.current_room_id = None
        self.current_room_name = ""
        self.rooms_list: List[Dict] = []

        # New: selected max players for create-room UI (2/3/4)
        self.selected_max_players: int = 4
        # keep renderer informed for drawing
        self.renderer.selected_room_max_players = self.selected_max_players

    def validate_ip(self, ip: str) -> bool:
        if ip.lower() in ['localhost', '127.0.0.1']:
            return True
        pattern = r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$'
        return bool(re.match(pattern, ip))

    def handle_click(self, pos: Tuple[int, int]) -> None:
        current_time = pygame.time.get_ticks()
        if current_time - self.last_click_time < CLICK_DEBOUNCE_MS:
            return
        self.last_click_time = current_time

        if self.state_manager.state == "menu":
            self._handle_menu_click(pos)
            if self.ui_elements.get("customize") and self.ui_elements["customize"].rect.collidepoint(pos):
                self.state_manager.state = "customize"
        elif self.state_manager.state == "customize":
            self._handle_customize_click(pos)
        elif self.state_manager.state == "lobby":
            self._handle_lobby_click(pos)
        elif self.state_manager.state == "room_waiting":
            self._handle_room_waiting_click(pos)
        elif self.state_manager.state == "playing":
            self._handle_game_click(pos)
        elif self.state_manager.state == "leaderboard":
            self._handle_leaderboard_click(pos)

    def _handle_menu_click(self, pos: Tuple[int, int]) -> None:
        ip_field = self.input_fields["ip"]
        name_field = self.input_fields["name"]
        connect_btn = self.ui_elements["connect"]
        close_btn = self.ui_elements["close"]

        if ip_field.rect.collidepoint(pos):
            ip_field.active = True
            name_field.active = False
        elif name_field.rect.collidepoint(pos):
            name_field.active = True
            ip_field.active = False
        else:
            ip_field.active = False
            name_field.active = False

        if connect_btn.rect.collidepoint(pos):
            self._handle_connect()
        elif close_btn.rect.collidepoint(pos):
            raise SystemExit(0)

    def _handle_customize_click(self, pos: Tuple[int, int]) -> None:
        mouse_pos = pos

        # Background selection
        y = 210
        for bg in self.renderer.background_options:
            rect = pygame.Rect(100, y, 340, 45)
            if rect.collidepoint(mouse_pos):
                self.renderer.selected_background = bg
                self.renderer.current_background_path = f"assets/backgrounds/{bg}"
                self.renderer.load_assets(
                    self.renderer.current_background_path,
                    self.renderer.current_card_back_path,
                    (CARD_WIDTH, CARD_HEIGHT)
                )
                return
            y += 55

        # Card back selection
        y = 210
        for theme in self.renderer.card_back_themes:
            rect = pygame.Rect(SCREEN_WIDTH // 2 + 30, y, 340, 45)
            if rect.collidepoint(mouse_pos):
                self.renderer.selected_card_theme = theme
                self.renderer.current_card_back_path = f"assets/cards/{theme}/back.png"
                self.renderer.load_assets(
                    self.renderer.current_background_path,
                    self.renderer.current_card_back_path,
                    (CARD_WIDTH, CARD_HEIGHT)
                )
                return
            y += 55

        # Apply & Return
        apply_rect = pygame.Rect(SCREEN_WIDTH // 2 - 220, SCREEN_HEIGHT - 100, 200, 60)
        if apply_rect.collidepoint(mouse_pos):
            self.state_manager.state = "menu"

        # Cancel
        cancel_rect = pygame.Rect(SCREEN_WIDTH // 2 + 40, SCREEN_HEIGHT - 100, 200, 60)
        if cancel_rect.collidepoint(mouse_pos):
            self.state_manager.state = "menu"

    def _handle_lobby_click(self, pos: Tuple[int, int]) -> None:
        room_name_field = self.input_fields["room_name"]
        create_btn = self.ui_elements["create"]
        refresh_btn = self.ui_elements["refresh"]
        disconnect_btn = self.ui_elements["disconnect"]

        # Player count selector rects (match renderer positions) — y moved to 210
        pc_base_x = 50
        pc_y = 210
        pc_width = 40
        pc_height = 30

        pc2_rect = pygame.Rect(pc_base_x, pc_y, pc_width, pc_height)
        pc3_rect = pygame.Rect(pc_base_x + 50, pc_y, pc_width, pc_height)
        pc4_rect = pygame.Rect(pc_base_x + 100, pc_y, pc_width, pc_height)

        if room_name_field.rect.collidepoint(pos):
            room_name_field.active = True
        elif create_btn.rect.collidepoint(pos):
            self._create_room()
        elif refresh_btn.rect.collidepoint(pos):
            self.network.send_message({"t": "refresh_rooms"})
        elif disconnect_btn.rect.collidepoint(pos):
            self.network.disconnect()
            self.state_manager.state = "menu"
            self.state_manager.waiting_message = None
        else:
            room_name_field.active = False

        # Handle player-count selection clicks
        if pc2_rect.collidepoint(pos):
            self.selected_max_players = 2
            self.renderer.selected_room_max_players = 2
            return
        if pc3_rect.collidepoint(pos):
            self.selected_max_players = 3
            self.renderer.selected_room_max_players = 3
            return
        if pc4_rect.collidepoint(pos):
            self.selected_max_players = 4
            self.renderer.selected_room_max_players = 4
            return

        if pygame.Rect(300, 150, SCREEN_WIDTH - 350, SCREEN_HEIGHT - 200).collidepoint(pos):
            self._handle_room_list_click(pos)

    def _handle_room_list_click(self, pos: Tuple[int, int]) -> None:
        relative_y = pos[1] - 150
        room_index = relative_y // 80

        if 0 <= room_index < len(self.rooms_list):
            room = self.rooms_list[room_index]
            if not room.get("in_game", False) and room.get("players", 0) < room.get("max_players", 4):
                self.network.send_message({"t": "join_room", "room_id": room["room_id"]})

    def _handle_room_waiting_click(self, pos: Tuple[int, int]) -> None:
        leave_btn_rect = self.ui_elements["leave_room"].rect
        if leave_btn_rect.collidepoint(pos):
            self.network.send_message({"t": "leave_room"})

    def _handle_game_click(self, pos: Tuple[int, int]) -> None:
        leave_btn_rect = pygame.Rect(50, SCREEN_HEIGHT - 60, 150, 40)
        if leave_btn_rect.collidepoint(pos):
            self.network.send_message({"t": "leave_room"})
        elif self.state_manager.state == "playing" and self.state_manager.game_state and self.state_manager.local_player == self.state_manager.game_state.get(
                "current_player", -1):
            for i, sprite in enumerate(self.card_sprites[self.state_manager.local_player].sprites()):
                if sprite.rect.collidepoint(pos):
                    self.network.send_message({"t": "p", "ci": i})
                    return
            if self.layout.draw_pile_rect.collidepoint(pos):
                self.network.send_message({"t": "d"})

    def _handle_leaderboard_click(self, pos: Tuple[int, int]) -> None:
        leave_btn_rect = pygame.Rect(50, SCREEN_HEIGHT - 60, 150, 40)
        if leave_btn_rect.collidepoint(pos):
            self.network.send_message({"t": "leave_room"})

    def handle_key(self, event) -> None:
        handled = False
        if self.state_manager.state == "menu":
            handled = self.input_fields["ip"].handle_key(event) or self.input_fields["name"].handle_key(event)
        elif self.state_manager.state == "lobby":
            if self.input_fields["room_name"].handle_key(event):
                handled = True
                if event.key == pygame.K_RETURN:
                    self._create_room()

        if handled and event.key == pygame.K_RETURN and self.state_manager.state == "menu":
            self._handle_connect()

    def _handle_connect(self) -> None:
        ip_field = self.input_fields["ip"]
        name_field = self.input_fields["name"]
        ip = ip_field.text or "localhost"
        name = name_field.text.strip()
        if not self.validate_ip(ip):
            self.state_manager.waiting_message = "Invalid IP address"
            return
        if not name:
            self.state_manager.waiting_message = "Username is required"
            return
        if not (3 <= len(name) <= 20):
            self.state_manager.waiting_message = "Name must be 3-20 characters"
            return

        self.player_name = name

        if self.network.connect(ip):
            # Start listener and request name assignment from server.
            # Do NOT switch to lobby locally until server returns "name_set".
            self.state_manager.waiting_message = "Setting name..."
            running_flag = [True]
            self.network.start_listener(running_flag, self._on_network_message)
            self.network.send_message({"t": "set_name", "name": self.player_name})
        else:
            self.state_manager.waiting_message = f"Failed to connect to {ip}"

    def _create_room(self) -> None:
        room_name = self.input_fields["room_name"].text.strip()
        if len(room_name) >= 3:
            self.network.send_message({
                "t": "create_room",
                "room_name": room_name,
                "max_players": self.selected_max_players
            })
            self.input_fields["room_name"].text = ""

    def _on_network_message(self, message: dict) -> None:
        self.state_manager.waiting_start = time.time()
        msg_type = message.get("t")

        if msg_type == "lobby_welcome":
            # Server welcome only confirms connection; still wait for "name_set" to enter lobby.
            self.state_manager.waiting_message = None

        elif msg_type == "name_set":
            # Server accepted the username; now enter lobby.
            assigned_name = message.get("name", self.player_name)
            self.player_name = assigned_name
            self.name_set = True
            self.state_manager.state = "lobby"
            self.state_manager.waiting_message = None
            # server will typically send room_list after name_set; no further action required here.

        elif msg_type in ["room_list", "room_list_update"]:
            self.rooms_list = message.get("rooms", [])

        elif msg_type == "room_joined":
            self.current_room_id = message.get("room_id")
            self.current_room_name = message.get("room_name", "")
            self.state_manager.local_player = message.get("player_slot", 0)
            self.state_manager.player_names[self.state_manager.local_player] = self.player_name
            print(f"[DEBUG] Set local player {self.state_manager.local_player} name: {self.player_name}")
            self.state_manager.state = "room_waiting"
            self.state_manager.waiting_message = f"Joined room: {self.current_room_name}"

        elif msg_type == "player_joined":
            player_name = message.get("player_name", "Unknown")
            player_slot = message.get("player_slot", -1)
            players_count = message.get("players_count", 1)
            if player_slot >= 0:
                self.state_manager.player_names[player_slot] = player_name
                print(f"[DEBUG] Set player {player_slot} name: {player_name}")

        elif msg_type == "player_left":
            player_name = message.get("player_name", "Unknown")
            players_count = message.get("players_count", 0)

        elif msg_type == "waiting":
            players_needed = message.get("players_needed", 0)
            self.state_manager.waiting_message = (
                f"Waiting for {players_needed} more player(s)..."
            )




        elif msg_type == "gs":
            print("[DEBUG GS RECEIVED] player_names in message:", message.get("player_names", "MISSING_KEY"))
            if "player_names" in message:
                print("[DEBUG GS RECEIVED] player_names dict:", message["player_names"])
                player_names_converted = {int(k): v for k, v in message["player_names"].items()}
                self.state_manager.player_names = player_names_converted
                message["player_names"] = player_names_converted

            self.state_manager.num_players = message.get("num_players", len(message.get("players", [])))
            print(f"[DEBUG] Set num_players to {self.state_manager.num_players}")
            self.state_manager.game_state = message
            self.state_manager.waiting_message = None
            self.state_manager.state = "playing"
            self.state_manager.render_debug_done = False
            self.update_card_sprites()

        elif msg_type == "go":
            self.state_manager.waiting_message = f"Game over! Winner: Player {message.get('w', 'none')}"
            if "player_names" in message:
                converted = {int(k): v for k, v in message["player_names"].items()}
                self.state_manager.player_names = converted
            self.state_manager.state = "leaderboard"
            self.state_manager.leaderboard_data = message.get("results", [])
            self.state_manager.leaderboard_start = time.time()
            self.state_manager.game_state = None
            self.card_sprites = {i: pygame.sprite.Group() for i in range(4)}
            self.current_room_id = None
            self.current_room_name = ""

        elif msg_type == "back_to_lobby":
            self.state_manager.state = "lobby"
            self.current_room_id = None
            self.current_room_name = ""
            self.state_manager.waiting_message = "Returned to lobby"



        elif msg_type == "e":
            # Server error (e.g., duplicate name). Stay out of lobby and show error.
            self.state_manager.waiting_message = f"Error: {message.get('msg', 'Unknown error')}"

    def update_card_sprites(self) -> None:
        if not self.state_manager.game_state or self.state_manager.local_player is None:
            return

        # Use actual number of players instead of 4
        for i in range(self.state_manager.num_players):
            hand = self.state_manager.game_state["players"][i]
            if not hand:
                self.card_sprites[i].empty()
                continue

            pos_index = (i - self.state_manager.local_player) % self.state_manager.num_players
            current_sprites = list(self.card_sprites[i].sprites())

            if len(current_sprites) != len(hand):
                self.card_sprites[i].empty()
                is_local = (i == self.state_manager.local_player)
                for j, card_data in enumerate(hand):
                    card_key = card_data["name"]
                    if card_key not in self.card_cache:
                        self.card_cache[card_key] = Card(card_data["name"], card_data["value"], card_data["suit"])
                    card = self.card_cache[card_key]
                    x, y, angle = self.layout.get_player_position(
                        pos_index, len(hand), j, is_local=is_local
                    )
                    back_card = self.card_cache.get("back", Card("back", 0, ""))
                    sprite = CardSprite(card if is_local else back_card, x, y, angle)
                    self.card_sprites[i].add(sprite)


class MultiRoomClient:
    def __init__(self):
        pygame.init()
        if not pygame.font.get_init():
            pygame.font.init()
        self.font = pygame.font.SysFont("Times New Roman", 24)
        self.title_font = pygame.font.SysFont("Times New Roman", 36, bold=True)
        self.small_font = pygame.font.SysFont("Times New Roman", 18)
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption("Sedma bere tri")

        self.layout = LayoutManager(SCREEN_WIDTH, SCREEN_HEIGHT)
        self.renderer = Renderer(self.screen, self.layout, self.font, self.title_font, self.small_font)
        self.state_manager = StateManager()
        self.network = NetworkManager()

        self._setup_ui_rects()

        self.input_fields = {
            "ip": InputField(self.ip_input_rect, "Server IP", self.font, BUTTON_COLOR, 20),
            "name": InputField(self.name_input_rect, "Username", self.font, BUTTON_COLOR, 20),
            "room_name": InputField(self.room_name_input_rect, "Room name", self.font, BUTTON_COLOR, 30)
        }
        self.ui_elements = {
            "connect": UIElement(self.connect_button_rect, "Connect", self.font, BUTTON_COLOR),
            "close": UIElement(self.close_button_rect, "Close", self.font, BUTTON_COLOR),
            "create": UIElement(self.create_room_button_rect, "Create Room", self.font, BUTTON_COLOR),
            "refresh": UIElement(self.refresh_button_rect, "Refresh Rooms", self.font, BUTTON_COLOR),
            "disconnect": UIElement(self.disconnect_button_rect, "Disconnect", self.font, BUTTON_COLOR),
            "leave_room": UIElement(self.leave_room_button_rect, "Leave Room", self.font, BUTTON_COLOR),
            "customize": UIElement(
                pygame.Rect(20, SCREEN_HEIGHT - 80, 180, 60),
                "Customize",
                self.font,
                CUSTOMIZE_BUTTON_COLOR
            )
        }

        self.event_handler = EventHandler(self.network, self.state_manager, self.renderer, self.layout,
                                          self.input_fields, self.ui_elements)

        self.card_sprites: Dict[int, pygame.sprite.Group] = self.event_handler.card_sprites
        self.running: bool = True

        # Initial asset load
        self.renderer.load_assets(
            self.renderer.current_background_path,
            self.renderer.current_card_back_path,
            (CARD_WIDTH, CARD_HEIGHT)
        )

        suits = ["♥", "♦", "♣", "♠"]
        values = list(range(7, 15))
        card_names = [f"{value}{suit}" for suit in suits for value in values] + ["back"]
        Card.preload_images(card_names)

    def _setup_ui_rects(self):
        self.ip_input_rect = pygame.Rect(SCREEN_WIDTH // 2 - 150, SCREEN_HEIGHT // 2 - 150, 300, 40)
        self.name_input_rect = pygame.Rect(SCREEN_WIDTH // 2 - 150, SCREEN_HEIGHT // 2 - 70, 300, 40)
        self.connect_button_rect = pygame.Rect(SCREEN_WIDTH // 2 - 75, SCREEN_HEIGHT // 2 - 10, 150, 40)
        self.close_button_rect = pygame.Rect(SCREEN_WIDTH // 2 - 75, SCREEN_HEIGHT // 2 + 50, 150, 40)

        # Left-side lobby controls
        self.create_room_button_rect = pygame.Rect(50, 150, 200, 40)
        # moved down to avoid overlap with create button and selector
        self.room_name_input_rect = pygame.Rect(50, 260, 200, 40)
        self.refresh_button_rect = pygame.Rect(50, 320, 200, 40)
        self.disconnect_button_rect = pygame.Rect(50, SCREEN_HEIGHT - 60, 200, 40)

        self.leave_room_button_rect = pygame.Rect(50, SCREEN_HEIGHT - 60, 150, 40)

    def run(self) -> None:
        clock = pygame.time.Clock()

        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1:
                        self.event_handler.handle_click(event.pos)
                elif event.type == pygame.KEYDOWN:
                    self.event_handler.handle_key(event)

            while not self.network.message_queue.empty():
                self.event_handler._on_network_message(self.network.message_queue.get())

            if self.state_manager.state == "leaderboard" and time.time() - self.state_manager.leaderboard_start > LEADERBOARD_DURATION:
                self.network.send_message({"t": "leave_room"})
                self.state_manager.state = "lobby"
                self.event_handler.current_room_id = None
                self.event_handler.current_room_name = ""
                self.state_manager.leaderboard_data = None
                self.state_manager.game_state = None

            mouse_pos = pygame.mouse.get_pos()

            if self.state_manager.state == "menu":
                self.renderer.render_menu(
                    self.input_fields["ip"],
                    self.input_fields["name"],
                    self.ui_elements["connect"],
                    self.ui_elements["close"],
                    self.state_manager.waiting_message
                )
                self.ui_elements["customize"].draw(self.screen, mouse_pos)

            elif self.state_manager.state == "lobby":
                self.renderer.render_lobby(
                    self.renderer.current_background_path,
                    self.event_handler.player_name,
                    self.input_fields["room_name"],
                    self.ui_elements["create"],
                    self.ui_elements["refresh"],
                    self.ui_elements["disconnect"],
                    self.event_handler.rooms_list,
                    self.state_manager.waiting_message
                )

            elif self.state_manager.state in ["room_waiting", "playing"]:
                self.renderer.render_game(
                    self.state_manager,
                    self.card_sprites,
                    self.event_handler.current_room_name,
                    mouse_pos,
                    self.state_manager.waiting_message
                )

            elif self.state_manager.state == "leaderboard":
                self.renderer.render_leaderboard(self.state_manager, mouse_pos)

            elif self.state_manager.state == "customize":
                self.renderer.render_customize(mouse_pos)

            pygame.display.flip()
            clock.tick(60)

        self._cleanup()

    def _cleanup(self) -> None:
        self.network.disconnect()
        pygame.quit()


if __name__ == "__main__":
    client = MultiRoomClient()
    client.run()