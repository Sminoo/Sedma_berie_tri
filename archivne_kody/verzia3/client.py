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
import os
import re
import logging

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 720
HIGHLIGHT_COLOR = (255, 0, 0)
CLICK_DEBOUNCE_MS = 200
PORT = 65432
PUBLIC_SERVER = os.getenv("PUBLIC_SERVER", "158.101.177.217")
CARD_WIDTH, CARD_HEIGHT = 80, 142
CARD_HIGHLIGHT_THICKNESS = 3

BACKGROUND_COLOR = (0, 0, 0)
BUTTON_COLOR = (100, 100, 100)
BUTTON_HOVER_COLOR = (150, 150, 150)
TEXT_COLOR = (255, 255, 255)
PLACEHOLDER_COLOR = (150, 150, 150)
SUCCESS_COLOR = (0, 200, 0)
ERROR_COLOR = (255, 100, 100)
CUSTOMIZE_BUTTON_COLOR = (80, 80, 140)
CUSTOMIZE_HOVER_COLOR = (120, 120, 200)
ROOM_ITEM_COLOR = (60, 60, 80, 15)
ROOM_ITEM_HOVER_COLOR = (80, 80, 100)
SERVER_ON_COLOR = (0, 160, 0)
SERVER_ON_HOVER_COLOR = (0, 210, 0)

LEADERBOARD_DURATION = 10


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except socket.error:
        return "127.0.0.1"


def validate_ip(ip: str) -> bool:
    if ip.lower() in ("localhost", "127.0.0.1"):
        return True
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


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
            {"x": screen_width // 2, "y": screen_height - 172, "angle": 0},
            {"x": screen_width - 172, "y": screen_height // 2, "angle": -90},
            {"x": screen_width // 2, "y": 30, "angle": 180},
            {"x": 30, "y": screen_height // 2, "angle": 90},
        ]
        self.draw_pile_rect = pygame.Rect(
            screen_width // 2 - 53, screen_height // 2 - 53, CARD_WIDTH + 6, CARD_HEIGHT + 6
        )
        self.discard_pile_pos = (screen_width // 2 + 50, screen_height // 2 - 50)
        self.name_positions = [
            {"x": screen_width // 2, "y": screen_height - 190, "angle": 0},
            {"x": screen_width - 190, "y": screen_height // 2, "angle": 90},
            {"x": screen_width // 2, "y": 190, "angle": 180},
            {"x": 190, "y": screen_height // 2, "angle": -90},
        ]

    def get_player_position(
        self, pos_index: int, num_cards: int, card_index: int, is_local: bool = False
    ) -> Tuple[int, int, float]:
        pos = self.positions[pos_index]
        base_offset = 84
        if is_local:
            offset = base_offset if num_cards <= 8 else max(35, base_offset - (num_cards - 8) * 3)
        else:
            offset = max(30, base_offset - max(0, (num_cards - 3) * 4))
        x = pos["x"] - (num_cards * offset // 2) + (card_index * offset) if pos_index in (0, 2) else pos["x"]
        y = pos["y"] - (num_cards * offset // 2) + (card_index * offset) if pos_index in (1, 3) else pos["y"]
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
                sock.close()
                return False
        try:
            _, writable, _ = select.select([], [sock], [], 5.0)
            if writable and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                self.client_socket = sock
                return True
            sock.close()
            return False
        except socket.error:
            sock.close()
            return False

    def disconnect(self) -> None:
        if self.client_socket:
            try:
                self.client_socket.close()
            except socket.error as e:
                logger.error(f"Disconnect error: {e}")
            self.client_socket = None
        self.message_queue = Queue()

    def send_message(self, message: dict) -> bool:
        if not self.client_socket:
            return False
        try:
            data = zlib.compress(json.dumps(message, separators=(",", ":")).encode())
            self.client_socket.sendall(struct.pack("!I", len(data)) + data)
            return True
        except socket.error as e:
            logger.error(f"Send error: {e}")
            return False

    def receive_message(self) -> Optional[dict]:
        """Prijme jednu správu zo socketu. Blokuje kým nie sú dáta dostupné."""
        sock = self.client_socket
        if sock is None:
            return None
        try:
            # Načítaj dĺžku správy (4 bajty)
            length_data = b""
            while len(length_data) < 4:
                chunk = sock.recv(4 - len(length_data))
                if not chunk:
                    return None
                length_data += chunk
            length = struct.unpack("!I", length_data)[0]
            # Načítaj telo správy
            data = b""
            while len(data) < length:
                chunk = sock.recv(length - len(data))
                if not chunk:
                    return None
                data += chunk
            return json.loads(zlib.decompress(data).decode())
        except socket.error as e:
            if hasattr(e, "winerror") and e.winerror == 10038:
                return None
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return None
            logger.error(f"Receive error: {e}")
            return None
        except (json.JSONDecodeError, struct.error, zlib.error) as e:
            logger.error(f"Decode error: {e}")
            return None

    def start_listener(self, running_flag):
        def listen():
            while running_flag[0] and self.client_socket:
                message = self.receive_message()
                if message:
                    self.message_queue.put(message)
                else:
                    time.sleep(0.01)

        threading.Thread(target=listen, daemon=True).start()


class LocalServerManager:
    """Spravuje lokálny server bežiaci v samostatnom vlákne — kompatibilné s PyInstaller."""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return True
        try:
            # Importujeme run_server z server.py — funguje aj v .exe (PyInstaller)
            from server import run_server
        except ImportError as e:
            logger.error(f"Nepodarilo sa importovať server: {e}")
            return False

        self._stop_event.clear()
        self._running = True

        def _run():
            try:
                run_server(PORT)
            except SystemExit:
                pass
            except Exception as e:
                logger.error(f"Server thread error: {e}")
            finally:
                self._running = False

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

        # Počkaj kým sa server skutočne spustí (max 3 sekundy)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                test = socket.create_connection(("127.0.0.1", PORT), timeout=0.2)
                test.close()
                return True
            except OSError:
                time.sleep(0.1)

        logger.error("Server sa nespustil včas")
        self._running = False
        return False

    def stop(self) -> None:
        if not self.running:
            self._running = False
            return
        # Pošleme spojenie na server aby sa odblokoval selector a ukončil sa
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=1)
            s.close()
        except OSError:
            pass
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None


class UIElement:
    def __init__(self, rect: pygame.Rect, text: str, font: pygame.font.Font, bg_color: Tuple[int, int, int]):
        self.rect = rect
        self.text = text
        self.font = font
        self.bg_color = bg_color
        self.surface = self.font.render(text, True, TEXT_COLOR)

    def draw(self, screen: pygame.Surface, mouse_pos: Tuple[int, int], hover_color: Optional[Tuple] = None) -> None:
        hc = hover_color or BUTTON_HOVER_COLOR
        color = hc if self.rect.collidepoint(mouse_pos) else self.bg_color
        pygame.draw.rect(screen, color, self.rect, border_radius=6)
        screen.blit(self.surface, self.surface.get_rect(center=self.rect.center))

    def update_text(self, new_text: str) -> None:
        self.text = new_text
        self.surface = self.font.render(new_text, True, TEXT_COLOR)


class InputField:
    def __init__(
        self,
        rect: pygame.Rect,
        placeholder: str,
        font: pygame.font.Font,
        bg_color: Tuple[int, int, int],
        max_len: int = 20,
    ):
        self.rect = rect
        self.placeholder = placeholder
        self.font = font
        self.bg_color = bg_color
        self.text = ""
        self.active = False
        self.max_len = max_len

    def draw(self, screen: pygame.Surface) -> None:
        color = BUTTON_HOVER_COLOR if self.active else self.bg_color
        pygame.draw.rect(screen, color, self.rect, border_radius=6)
        display_text = self.text or self.placeholder
        text_color = TEXT_COLOR if self.text else PLACEHOLDER_COLOR
        surface = self.font.render(display_text, True, text_color)
        screen.blit(surface, surface.get_rect(center=self.rect.center))

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
        self.state = "mode_select"
        self.selected_mode: str = "lan"
        self.local_player: Optional[int] = None
        self.game_state: Optional[Dict] = None
        self.player_names: Dict[int, str] = {}
        self.num_players: int = 4
        self.waiting_message: Optional[str] = None
        self.waiting_start: float = time.time()
        self.leaderboard_start: float = 0
        self.leaderboard_data: Optional[List[Dict]] = None


class Renderer:
    def __init__(
        self,
        screen: pygame.Surface,
        layout: LayoutManager,
        font: pygame.font.Font,
        title_font: pygame.font.Font,
        small_font: pygame.font.Font,
    ):
        self.screen = screen
        self.layout = layout
        self.font = font
        self.title_font = title_font
        self.small_font = small_font
        self.background: Optional[pygame.Surface] = None
        self.card_back: Optional[pygame.Surface] = None
        self.selected_room_max_players: int = 4

        self.background_options = ["background_green.png", "background_blue.png", "background_red.png"]
        self.card_back_themes = ["default", "pixel"]
        self.selected_background = self.background_options[0]
        self.selected_card_theme = self.card_back_themes[0]
        self.current_background_path = f"assets/backgrounds/{self.selected_background}"
        self.current_card_back_path = f"assets/cards/{self.selected_card_theme}/back.png"

    def load_assets(self, background_path: str, card_back_path: str, size: Tuple[int, int]) -> None:
        try:
            bg = pygame.image.load(background_path)
            self.background = (
                pygame.transform.scale(bg, (SCREEN_WIDTH, SCREEN_HEIGHT))
                if bg.get_size() != (SCREEN_WIDTH, SCREEN_HEIGHT)
                else bg
            )
        except pygame.error:
            self.background = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT))
            self.background.fill((20, 60, 20))
        try:
            self.card_back = pygame.transform.scale(pygame.image.load(card_back_path), size)
        except pygame.error:
            self.card_back = pygame.Surface(size)
            self.card_back.fill((180, 30, 30))

    def _blit_text(self, text: str, font: pygame.font.Font, color, center: Tuple[int, int]) -> None:
        surf = font.render(text, True, color)
        self.screen.blit(surf, surf.get_rect(center=center))

    def _draw_button(self, rect: pygame.Rect, text: str, mouse_pos: Tuple[int, int],
                     color=None, hover_color=None) -> None:
        c = (hover_color or BUTTON_HOVER_COLOR) if rect.collidepoint(mouse_pos) else (color or BUTTON_COLOR)
        pygame.draw.rect(self.screen, c, rect, border_radius=6)
        self._blit_text(text, self.font, TEXT_COLOR, rect.center)

    def _draw_waiting_message(self, message: Optional[str]) -> None:
        if not message:
            return
        is_error = any(w in message.lower() for w in ("error", "neplatný", "nepodarilo", "invalid", "failed"))
        is_success = any(w in message.lower() for w in ("joined", "spustený", "returned", "vitaj"))
        color = ERROR_COLOR if is_error else SUCCESS_COLOR if is_success else TEXT_COLOR
        surf = self.font.render(message, True, color)
        self.screen.blit(surf, surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 120)))

    def render_mode_select(self, lan_btn: UIElement, online_btn: UIElement) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))
        self._blit_text("Vyber režim hry", self.title_font, TEXT_COLOR, (SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 120))
        mouse_pos = pygame.mouse.get_pos()
        lan_btn.draw(self.screen, mouse_pos)
        online_btn.draw(self.screen, mouse_pos)

    def render_menu(
        self,
        ip_field: InputField,
        name_field: InputField,
        connect_btn: UIElement,
        close_btn: UIElement,
        waiting_message: Optional[str],
        server_manager: "LocalServerManager",
        server_btn: UIElement,
    ) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

        self._blit_text("Sedma Bere Tri", self.title_font, TEXT_COLOR,
                        (SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 250))

        for label_text, field in [("IP servera:", ip_field), ("Tvoje meno:", name_field)]:
            label = self.font.render(label_text, True, TEXT_COLOR)
            self.screen.blit(label, (field.rect.x, field.rect.y - 30))
            field.draw(self.screen)

        mouse_pos = pygame.mouse.get_pos()
        connect_btn.draw(self.screen, mouse_pos)
        close_btn.draw(self.screen, mouse_pos)

        # Server toggle tlačidlo (pravý dolný roh)
        is_running = server_manager.running
        if is_running:
            ip_surf = self.font.render(f"Tvoja IP: {get_local_ip()}:{PORT}", True, TEXT_COLOR)
            self.screen.blit(ip_surf, ip_surf.get_rect(center=(server_btn.rect.centerx, server_btn.rect.top - 24)))

        server_btn.bg_color = SERVER_ON_COLOR if is_running else BUTTON_COLOR
        server_btn.update_text("Zastaviť server" if is_running else "Spustiť server")
        server_btn.draw(self.screen, mouse_pos,
                        hover_color=SERVER_ON_HOVER_COLOR if is_running else BUTTON_HOVER_COLOR)

        self._draw_waiting_message(waiting_message)

    def render_menu_online(
        self,
        name_field: InputField,
        connect_btn: UIElement,
        close_btn: UIElement,
        waiting_message: Optional[str],
    ) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

        self._blit_text("Sedma Bere Tri — Online", self.title_font, TEXT_COLOR,
                        (SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 250))

        note = self.font.render(f"Verejný server: {PUBLIC_SERVER}", True, PLACEHOLDER_COLOR)
        self.screen.blit(note, (name_field.rect.x, name_field.rect.y - 60))

        label = self.font.render("Tvoje meno:", True, TEXT_COLOR)
        self.screen.blit(label, (name_field.rect.x, name_field.rect.y - 30))
        name_field.draw(self.screen)

        mouse_pos = pygame.mouse.get_pos()
        connect_btn.draw(self.screen, mouse_pos)
        close_btn.draw(self.screen, mouse_pos)
        self._draw_waiting_message(waiting_message)

    def render_lobby(
        self,
        player_name: str,
        room_name_field: InputField,
        create_btn: UIElement,
        refresh_btn: UIElement,
        disconnect_btn: UIElement,
        rooms_list: List[Dict],
        waiting_message: Optional[str],
    ) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

        self._blit_text(f"Hráč: {player_name}", self.title_font, TEXT_COLOR, (200, 35))

        label = self.font.render("Vytvor miestnosť:", True, TEXT_COLOR)
        self.screen.blit(label, (50, 120))
        room_name_field.draw(self.screen)

        mouse_pos = pygame.mouse.get_pos()
        create_btn.draw(self.screen, mouse_pos)
        refresh_btn.draw(self.screen, mouse_pos)

        # Výber počtu hráčov
        pc_y = 210
        for i, val in enumerate([2, 3, 4]):
            rect = pygame.Rect(50 + i * 50, pc_y, 40, 30)
            is_selected = self.selected_room_max_players == val
            color = (0, 180, 0) if is_selected else (CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR)
            pygame.draw.rect(self.screen, color, rect, border_radius=6)
            lbl = self.small_font.render(str(val), True, TEXT_COLOR)
            self.screen.blit(lbl, lbl.get_rect(center=rect.center))

        self._render_room_list(rooms_list)
        disconnect_btn.draw(self.screen, mouse_pos)

        if waiting_message:
            is_error = "error" in waiting_message.lower() or "nepodarilo" in waiting_message.lower()
            is_success = "joined" in waiting_message.lower() or "vitaj" in waiting_message.lower()
            color = ERROR_COLOR if is_error else SUCCESS_COLOR if is_success else TEXT_COLOR
            msg = self.font.render(waiting_message, True, color)
            self.screen.blit(msg, msg.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT - 100)))

    def _render_room_list(self, rooms_list: List[Dict]) -> None:
        list_title = self.font.render("Dostupné miestnosti (klikni pre vstup):", True, TEXT_COLOR)
        self.screen.blit(list_title, (300, 120))

        rooms_rect = pygame.Rect(300, 150, SCREEN_WIDTH - 350, SCREEN_HEIGHT - 200)
        pygame.draw.rect(self.screen, ROOM_ITEM_COLOR, rooms_rect)

        if not rooms_list:
            no_rooms = self.font.render("Žiadne miestnosti. Vytvor jednu!", True, PLACEHOLDER_COLOR)
            self.screen.blit(no_rooms, no_rooms.get_rect(center=rooms_rect.center))
            return

        mouse_pos = pygame.mouse.get_pos()
        item_height = 80
        details_color = (200, 200, 200)

        for i, room in enumerate(rooms_list):
            room_rect = pygame.Rect(
                rooms_rect.x + 10, rooms_rect.y + i * item_height + 5,
                rooms_rect.width - 20, item_height - 5
            )
            available = not room.get("in_game", False) and room.get("players", 0) < room.get("max_players", 4)
            color = (ROOM_ITEM_HOVER_COLOR if room_rect.collidepoint(mouse_pos) else ROOM_ITEM_COLOR) if available else (60, 60, 60)
            pygame.draw.rect(self.screen, color, room_rect)

            name_surf = self.font.render(room.get("room_name", "?"), True, TEXT_COLOR)
            self.screen.blit(name_surf, (room_rect.x + 10, room_rect.y + 8))

            creator_surf = self.small_font.render(f"vytvoril {room.get('creator', '?')}", True, details_color)
            self.screen.blit(creator_surf, (room_rect.x + 10, room_rect.y + 32))

            players_text = f"{room.get('players', 0)}/{room.get('max_players', 4)} hráčov"
            if room.get("in_game"):
                players_text += " (V HRE)"
            players_surf = self.small_font.render(players_text, True, details_color)
            self.screen.blit(players_surf, (room_rect.x + 10, room_rect.y + 50))

    def render_game(
        self,
        state_manager: StateManager,
        card_sprites: Dict[int, pygame.sprite.Group],
        current_room_name: str,
        mouse_pos: Tuple[int, int],
        waiting_message: Optional[str],
    ) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

        if current_room_name:
            self._blit_text(f"Miestnosť: {current_room_name}", self.title_font, TEXT_COLOR, (SCREEN_WIDTH // 4, 20))

        if waiting_message:
            surf = self.font.render(waiting_message, True, TEXT_COLOR)
            self.screen.blit(surf, (10, 50))

        leave_rect = pygame.Rect(50, SCREEN_HEIGHT - 60, 150, 40)
        self._draw_button(leave_rect, "Opustiť hru", mouse_pos)

        if state_manager.state == "room_waiting" and state_manager.waiting_message:
            self._blit_text(state_manager.waiting_message, self.title_font, TEXT_COLOR,
                            (SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))

        elif state_manager.state == "playing" and state_manager.game_state and state_manager.local_player is not None:
            current_player = state_manager.game_state.get("current_player", 0)
            player_names = state_manager.game_state.get("player_names", {})

            for i in range(state_manager.num_players):
                if card_sprites[i]:
                    card_sprites[i].draw(self.screen)
                    if i == state_manager.local_player == current_player:
                        for sprite in card_sprites[i]:
                            if sprite.rect.collidepoint(mouse_pos):
                                pygame.draw.rect(self.screen, HIGHLIGHT_COLOR, sprite.rect, CARD_HIGHLIGHT_THICKNESS)

            draw_pile_rect = self.layout.draw_pile_rect
            if draw_pile_rect.collidepoint(mouse_pos):
                pygame.draw.rect(self.screen, HIGHLIGHT_COLOR, draw_pile_rect, CARD_HIGHLIGHT_THICKNESS)
            if state_manager.game_state.get("draw_pile_count", 0) > 0:
                self.screen.blit(self.card_back, (draw_pile_rect.x + 3, draw_pile_rect.y + 3))

            if state_manager.game_state.get("discard_pile"):
                top = state_manager.game_state["discard_pile"][-1]
                Card(top["name"], top["value"], top["suit"]).draw(self.screen, *self.layout.discard_pile_pos)

            for i in range(state_manager.num_players):
                pos_index = (i - state_manager.local_player) % state_manager.num_players
                name_pos = self.layout.name_positions[pos_index]
                color = HIGHLIGHT_COLOR if i == current_player else TEXT_COLOR
                name_text = self.font.render(player_names.get(i, f"Hráč {i + 1}"), True, color)
                rotated = pygame.transform.rotate(name_text, name_pos["angle"])
                self.screen.blit(rotated, rotated.get_rect(center=(name_pos["x"], name_pos["y"])))

    def render_leaderboard(self, state_manager: StateManager, mouse_pos: Tuple[int, int]) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

        self._blit_text("Koniec hry", self.title_font, TEXT_COLOR, (SCREEN_WIDTH // 2, SCREEN_HEIGHT // 4))

        if state_manager.leaderboard_data:
            for i, entry in enumerate(state_manager.leaderboard_data):
                pid = entry.get("pid", 0)
                name = state_manager.player_names.get(pid, f"Hráč {pid + 1}")
                text = f"{entry.get('rank', i + 1)}. {name}"
                if entry.get("disconnected"):
                    text += " (odpojený)"
                surf = self.font.render(text, True, TEXT_COLOR)
                self.screen.blit(surf, surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 3 + (i + 1) * 50)))

        leave_rect = pygame.Rect(50, SCREEN_HEIGHT - 60, 150, 40)
        self._draw_button(leave_rect, "Späť do lobby", mouse_pos)

    def render_customize(self, mouse_pos: Tuple[int, int]) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

        self._blit_text("Prispôsobenie", self.title_font, TEXT_COLOR, (SCREEN_WIDTH // 2, 80))

        # Pozadie
        self.screen.blit(self.font.render("Pozadie", True, TEXT_COLOR), (120, 160))
        y = 210
        for bg in self.background_options:
            rect = pygame.Rect(100, y, 340, 45)
            color = (0, 180, 0) if bg == self.selected_background else (
                CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR)
            pygame.draw.rect(self.screen, color, rect, border_radius=8)
            self.screen.blit(self.font.render(
                bg.replace(".png", "").replace("_", " ").title(), True, TEXT_COLOR), (130, y + 10))
            y += 55

        # Dizajn kariet
        self.screen.blit(self.font.render("Dizajn kariet", True, TEXT_COLOR), (SCREEN_WIDTH // 2 + 30, 160))
        y = 210
        for theme in self.card_back_themes:
            rect = pygame.Rect(SCREEN_WIDTH // 2 + 30, y, 340, 45)
            color = (0, 180, 0) if theme == self.selected_card_theme else (
                CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR)
            pygame.draw.rect(self.screen, color, rect, border_radius=8)
            self.screen.blit(self.font.render(theme.capitalize(), True, TEXT_COLOR),
                             (SCREEN_WIDTH // 2 + 60, y + 10))
            y += 55

        # Tlačidlá
        for rect, text in [
            (pygame.Rect(SCREEN_WIDTH // 2 - 220, SCREEN_HEIGHT - 100, 200, 60), "Použiť a späť"),
            (pygame.Rect(SCREEN_WIDTH // 2 + 40, SCREEN_HEIGHT - 100, 200, 60), "Zrušiť"),
        ]:
            self._draw_button(rect, text, mouse_pos)


class EventHandler:
    def __init__(
        self,
        network: NetworkManager,
        state_manager: StateManager,
        renderer: Renderer,
        layout: LayoutManager,
        input_fields: Dict[str, InputField],
        ui_elements: Dict[str, UIElement],
        server_manager: LocalServerManager,
    ):
        self.network = network
        self.state_manager = state_manager
        self.renderer = renderer
        self.layout = layout
        self.input_fields = input_fields
        self.ui_elements = ui_elements
        self.server_manager = server_manager
        self.last_click_time: int = 0
        self.card_sprites: Dict[int, pygame.sprite.Group] = {i: pygame.sprite.Group() for i in range(4)}
        self.card_cache: Dict[str, Card] = {}
        self.player_name = ""
        self.name_set = False
        self.current_room_id = None
        self.current_room_name = ""
        self.rooms_list: List[Dict] = []
        self.selected_max_players: int = 4
        self.renderer.selected_room_max_players = self.selected_max_players
        self._listener_flag = [False]

    def handle_click(self, pos: Tuple[int, int]) -> None:
        now = pygame.time.get_ticks()
        if now - self.last_click_time < CLICK_DEBOUNCE_MS:
            return
        self.last_click_time = now

        dispatch = {
            "mode_select": self._handle_mode_select_click,
            "menu": self._handle_menu_click,
            "menu_online": self._handle_menu_click,
            "customize": self._handle_customize_click,
            "lobby": self._handle_lobby_click,
            "room_waiting": self._handle_room_waiting_click,
            "playing": self._handle_game_click,
            "leaderboard": self._handle_leaderboard_click,
        }
        handler = dispatch.get(self.state_manager.state)
        if handler:
            handler(pos)

    def _handle_mode_select_click(self, pos: Tuple[int, int]) -> None:
        if self.ui_elements["mode_lan"].rect.collidepoint(pos):
            self.state_manager.selected_mode = "lan"
            self.state_manager.state = "menu"
        elif self.ui_elements["mode_online"].rect.collidepoint(pos):
            self.state_manager.selected_mode = "online"
            self.state_manager.state = "menu_online"

    def _handle_menu_click(self, pos: Tuple[int, int]) -> None:
        if self.ui_elements["customize"].rect.collidepoint(pos):
            self.state_manager.state = "customize"
            return

        if self.state_manager.state == "menu" and self.ui_elements["server_toggle"].rect.collidepoint(pos):
            if self.server_manager.running:
                self.server_manager.stop()
                self.state_manager.waiting_message = "Server zastavený."
            else:
                if self.server_manager.start():
                    self.state_manager.waiting_message = f"Server spustený — {get_local_ip()}:{PORT}"
                    self.input_fields["ip"].text = "localhost"
                else:
                    self.state_manager.waiting_message = "Nepodarilo sa spustiť server."
            return

        ip_field = self.input_fields["ip"]
        name_field = self.input_fields["name"]
        if ip_field.rect.collidepoint(pos):
            ip_field.active, name_field.active = True, False
        elif name_field.rect.collidepoint(pos):
            name_field.active, ip_field.active = True, False
        else:
            ip_field.active = name_field.active = False

        if self.ui_elements["connect"].rect.collidepoint(pos):
            if self.state_manager.state == "menu_online":
                ip_field.text = PUBLIC_SERVER
            self._handle_connect()
        elif self.ui_elements["close"].rect.collidepoint(pos):
            self.state_manager.state = "__quit__"

    def _handle_customize_click(self, pos: Tuple[int, int]) -> None:
        y = 210
        for bg in self.renderer.background_options:
            if pygame.Rect(100, y, 340, 45).collidepoint(pos):
                self.renderer.selected_background = bg
                self.renderer.current_background_path = f"assets/backgrounds/{bg}"
                self.renderer.load_assets(
                    self.renderer.current_background_path,
                    self.renderer.current_card_back_path,
                    (CARD_WIDTH, CARD_HEIGHT)
                )
                return
            y += 55

        y = 210
        for theme in self.renderer.card_back_themes:
            if pygame.Rect(SCREEN_WIDTH // 2 + 30, y, 340, 45).collidepoint(pos):
                self.renderer.selected_card_theme = theme
                self.renderer.current_card_back_path = f"assets/cards/{theme}/back.png"
                self.renderer.load_assets(
                    self.renderer.current_background_path,
                    self.renderer.current_card_back_path,
                    (CARD_WIDTH, CARD_HEIGHT)
                )
                Card.set_theme(theme)
                card_names = [f"{v}{s}" for s in ["♥", "♦", "♣", "♠"] for v in range(7, 15)] + ["back"]
                Card.preload_images(card_names)
                self.card_cache.clear()
                return
            y += 55

        for rect in [
            pygame.Rect(SCREEN_WIDTH // 2 - 220, SCREEN_HEIGHT - 100, 200, 60),
            pygame.Rect(SCREEN_WIDTH // 2 + 40, SCREEN_HEIGHT - 100, 200, 60),
        ]:
            if rect.collidepoint(pos):
                self.state_manager.state = "menu_online" if self.state_manager.selected_mode == "online" else "menu"
                return

    def _handle_lobby_click(self, pos: Tuple[int, int]) -> None:
        pc_y = 210
        for i, val in enumerate([2, 3, 4]):
            if pygame.Rect(50 + i * 50, pc_y, 40, 30).collidepoint(pos):
                self.selected_max_players = val
                self.renderer.selected_room_max_players = val
                return

        room_name_field = self.input_fields["room_name"]
        if room_name_field.rect.collidepoint(pos):
            room_name_field.active = True
        elif self.ui_elements["create"].rect.collidepoint(pos):
            self._create_room()
        elif self.ui_elements["refresh"].rect.collidepoint(pos):
            self.network.send_message({"t": "refresh_rooms"})
        elif self.ui_elements["disconnect"].rect.collidepoint(pos):
            self._listener_flag[0] = False
            self.network.disconnect()
            self.state_manager.state = "menu_online" if self.state_manager.selected_mode == "online" else "menu"
            self.state_manager.waiting_message = None
        else:
            room_name_field.active = False
            if pygame.Rect(300, 150, SCREEN_WIDTH - 350, SCREEN_HEIGHT - 200).collidepoint(pos):
                room_index = (pos[1] - 150) // 80
                if 0 <= room_index < len(self.rooms_list):
                    room = self.rooms_list[room_index]
                    if not room.get("in_game") and room.get("players", 0) < room.get("max_players", 4):
                        self.network.send_message({"t": "join_room", "room_id": room["room_id"]})

    def _handle_room_waiting_click(self, pos: Tuple[int, int]) -> None:
        if self.ui_elements["leave_room"].rect.collidepoint(pos):
            self.network.send_message({"t": "leave_room"})

    def _handle_game_click(self, pos: Tuple[int, int]) -> None:
        if pygame.Rect(50, SCREEN_HEIGHT - 60, 150, 40).collidepoint(pos):
            self.network.send_message({"t": "leave_room"})
            return
        gs = self.state_manager.game_state
        if gs and self.state_manager.local_player == gs.get("current_player", -1):
            for i, sprite in enumerate(self.card_sprites[self.state_manager.local_player].sprites()):
                if sprite.rect.collidepoint(pos):
                    self.network.send_message({"t": "p", "ci": i})
                    return
            if self.layout.draw_pile_rect.collidepoint(pos):
                self.network.send_message({"t": "d"})

    def _handle_leaderboard_click(self, pos: Tuple[int, int]) -> None:
        if pygame.Rect(50, SCREEN_HEIGHT - 60, 150, 40).collidepoint(pos):
            self.network.send_message({"t": "leave_room"})

    def handle_key(self, event) -> None:
        state = self.state_manager.state
        handled = False
        if state in ("menu", "menu_online"):
            handled = self.input_fields["ip"].handle_key(event) or self.input_fields["name"].handle_key(event)
        elif state == "lobby":
            if self.input_fields["room_name"].handle_key(event):
                handled = True
                if event.key == pygame.K_RETURN:
                    self._create_room()
        if handled and event.key == pygame.K_RETURN and state in ("menu", "menu_online"):
            self._handle_connect()

    def _handle_connect(self) -> None:
        ip = self.input_fields["ip"].text.strip() or "localhost"
        name = self.input_fields["name"].text.strip()
        if not validate_ip(ip):
            self.state_manager.waiting_message = "Neplatná IP adresa"
            return
        if not name:
            self.state_manager.waiting_message = "Meno je povinné"
            return
        if not (3 <= len(name) <= 20):
            self.state_manager.waiting_message = "Meno musí mať 3-20 znakov"
            return
        self.player_name = name
        if self.network.connect(ip):
            self.state_manager.waiting_message = "Nastavujem meno..."
            self._listener_flag = [True]
            self.network.start_listener(self._listener_flag)
            self.network.send_message({"t": "set_name", "name": self.player_name})
        else:
            self.state_manager.waiting_message = f"Nepodarilo sa pripojiť na {ip}"

    def _create_room(self) -> None:
        room_name = self.input_fields["room_name"].text.strip()
        if len(room_name) >= 3:
            self.network.send_message({
                "t": "create_room",
                "room_name": room_name,
                "max_players": self.selected_max_players,
            })
            self.input_fields["room_name"].text = ""

    def _on_network_message(self, message: dict) -> None:
        self.state_manager.waiting_start = time.time()
        msg_type = message.get("t")

        if msg_type == "lobby_welcome":
            self.state_manager.waiting_message = None
        elif msg_type == "name_set":
            self.player_name = message.get("name", self.player_name)
            self.name_set = True
            self.state_manager.state = "lobby"
            self.state_manager.waiting_message = None
        elif msg_type in ("room_list", "room_list_update"):
            self.rooms_list = message.get("rooms", [])
        elif msg_type == "room_joined":
            self.current_room_id = message.get("room_id")
            self.current_room_name = message.get("room_name", "")
            self.state_manager.local_player = message.get("player_slot", 0)
            self.state_manager.player_names[self.state_manager.local_player] = self.player_name
            self.update_card_sprites()
            self.state_manager.state = "room_waiting"
            self.state_manager.waiting_message = f"Vstúpil si do: {self.current_room_name}"
        elif msg_type == "player_joined":
            slot = message.get("player_slot", -1)
            if slot >= 0:
                self.state_manager.player_names[slot] = message.get("player_name", "Unknown")
        elif msg_type == "waiting":
            n = message.get("players_needed", 0)
            self.state_manager.waiting_message = f"Čakáme na {n} ďalšieho hráča..."
        elif msg_type == "gs":
            if "player_names" in message:
                converted = {int(k): v for k, v in message["player_names"].items()}
                self.state_manager.player_names = converted
                message["player_names"] = converted
            self.state_manager.num_players = message.get("num_players", len(message.get("players", [])))
            self.state_manager.game_state = message
            self.state_manager.waiting_message = None
            self.state_manager.state = "playing"
            self.update_card_sprites()
        elif msg_type == "go":
            if "player_names" in message:
                self.state_manager.player_names = {int(k): v for k, v in message["player_names"].items()}
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
            self.state_manager.waiting_message = "Vrátil si sa do lobby"
        elif msg_type == "e":
            self.state_manager.waiting_message = f"Chyba: {message.get('msg', 'Neznáma chyba')}"

    def update_card_sprites(self) -> None:
        if not self.state_manager.game_state or self.state_manager.local_player is None:
            return
        num_players = self.state_manager.num_players
        for i in range(num_players):
            self.card_sprites[i].empty()
        for i in range(num_players):
            hand = self.state_manager.game_state.get("players", [])[i]
            if not hand:
                continue
            pos_index = (i - self.state_manager.local_player) % num_players
            is_local = i == self.state_manager.local_player
            for j, card_data in enumerate(hand):
                key = card_data["name"]
                if key not in self.card_cache:
                    self.card_cache[key] = Card(card_data["name"], card_data["value"], card_data["suit"])
                x, y, angle = self.layout.get_player_position(pos_index, len(hand), j, is_local=is_local)
                display = self.card_cache[key] if is_local else self.card_cache.get("back", Card("back", 0, ""))
                self.card_sprites[i].add(CardSprite(display, x, y, angle))


class MultiRoomClient:
    def __init__(self):
        pygame.init()
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
        self.server_manager = LocalServerManager()

        self._setup_ui()

        self.event_handler = EventHandler(
            self.network,
            self.state_manager,
            self.renderer,
            self.layout,
            self.input_fields,
            self.ui_elements,
            self.server_manager,
        )

        self.running = True
        self.renderer.load_assets(
            self.renderer.current_background_path,
            self.renderer.current_card_back_path,
            (CARD_WIDTH, CARD_HEIGHT)
        )
        Card.set_theme(self.renderer.selected_card_theme)
        card_names = [f"{v}{s}" for s in ["♥", "♦", "♣", "♠"] for v in range(7, 15)] + ["back"]
        Card.preload_images(card_names)

    def _setup_ui(self):
        cx, cy = SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2
        self.input_fields = {
            "ip": InputField(pygame.Rect(cx - 150, cy - 150, 300, 40), "IP servera", self.font, BUTTON_COLOR, 20),
            "name": InputField(pygame.Rect(cx - 150, cy - 70, 300, 40), "Tvoje meno", self.font, BUTTON_COLOR, 20),
            "room_name": InputField(pygame.Rect(50, 260, 200, 40), "Názov miestnosti", self.font, BUTTON_COLOR, 30),
        }
        self.ui_elements = {
            "connect": UIElement(pygame.Rect(cx - 75, cy - 10, 150, 40), "Pripojiť", self.font, BUTTON_COLOR),
            "close": UIElement(pygame.Rect(cx - 75, cy + 50, 150, 40), "Zavrieť", self.font, BUTTON_COLOR),
            "create": UIElement(pygame.Rect(50, 150, 200, 40), "Vytvoriť miestnosť", self.font, BUTTON_COLOR),
            "refresh": UIElement(pygame.Rect(50, 320, 200, 40), "Obnoviť zoznam", self.font, BUTTON_COLOR),
            "disconnect": UIElement(pygame.Rect(50, SCREEN_HEIGHT - 60, 200, 40), "Odpojiť sa", self.font, BUTTON_COLOR),
            "leave_room": UIElement(pygame.Rect(50, SCREEN_HEIGHT - 60, 150, 40), "Opustiť hru", self.font, BUTTON_COLOR),
            "customize": UIElement(pygame.Rect(20, SCREEN_HEIGHT - 80, 180, 60), "Prispôsobiť", self.font, CUSTOMIZE_BUTTON_COLOR),
            "mode_lan": UIElement(pygame.Rect(cx - 220, cy - 40, 200, 80), "Hrať LAN", self.title_font, BUTTON_COLOR),
            "mode_online": UIElement(pygame.Rect(cx + 20, cy - 40, 200, 80), "Hrať Online", self.title_font, BUTTON_COLOR),
            "server_toggle": UIElement(pygame.Rect(SCREEN_WIDTH - 230, SCREEN_HEIGHT - 60, 220, 44), "Spustiť server", self.font, BUTTON_COLOR),
        }

    def run(self) -> None:
        clock = pygame.time.Clock()
        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self.event_handler.handle_click(event.pos)
                elif event.type == pygame.KEYDOWN:
                    self.event_handler.handle_key(event)

            if self.state_manager.state == "__quit__":
                self.running = False

            while not self.network.message_queue.empty():
                self.event_handler._on_network_message(self.network.message_queue.get())

            if (
                self.state_manager.state == "leaderboard"
                and time.time() - self.state_manager.leaderboard_start > LEADERBOARD_DURATION
            ):
                self.network.send_message({"t": "leave_room"})
                self.state_manager.state = "lobby"
                self.event_handler.current_room_id = None
                self.event_handler.current_room_name = ""
                self.state_manager.leaderboard_data = None
                self.state_manager.game_state = None

            mouse_pos = pygame.mouse.get_pos()
            state = self.state_manager.state

            if state == "mode_select":
                self.renderer.render_mode_select(self.ui_elements["mode_lan"], self.ui_elements["mode_online"])
            elif state == "menu":
                self.renderer.render_menu(
                    self.input_fields["ip"],
                    self.input_fields["name"],
                    self.ui_elements["connect"],
                    self.ui_elements["close"],
                    self.state_manager.waiting_message,
                    self.server_manager,
                    self.ui_elements["server_toggle"],
                )
                self.ui_elements["customize"].draw(self.screen, mouse_pos)
            elif state == "menu_online":
                self.renderer.render_menu_online(
                    self.input_fields["name"],
                    self.ui_elements["connect"],
                    self.ui_elements["close"],
                    self.state_manager.waiting_message,
                )
                self.ui_elements["customize"].draw(self.screen, mouse_pos)
            elif state == "lobby":
                self.renderer.render_lobby(
                    self.event_handler.player_name,
                    self.input_fields["room_name"],
                    self.ui_elements["create"],
                    self.ui_elements["refresh"],
                    self.ui_elements["disconnect"],
                    self.event_handler.rooms_list,
                    self.state_manager.waiting_message,
                )
            elif state in ("room_waiting", "playing"):
                self.renderer.render_game(
                    self.state_manager,
                    self.event_handler.card_sprites,
                    self.event_handler.current_room_name,
                    mouse_pos,
                    self.state_manager.waiting_message,
                )
            elif state == "leaderboard":
                self.renderer.render_leaderboard(self.state_manager, mouse_pos)
            elif state == "customize":
                self.renderer.render_customize(mouse_pos)

            pygame.display.flip()
            clock.tick(60)

        self._cleanup()

    def _cleanup(self) -> None:
        self.server_manager.stop()
        self.network.disconnect()
        pygame.quit()


if __name__ == "__main__":
    MultiRoomClient().run()