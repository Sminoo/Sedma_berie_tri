import logging

logger = logging.getLogger(__name__)

try:
    import pygame
    _PYGAME_AVAILABLE = True
except ImportError:
    _PYGAME_AVAILABLE = False


class Card:
    _image_cache: dict = {}
    _current_theme: str = "default"

    @classmethod
    def set_theme(cls, theme: str) -> None:
        """Zmení theme a vyčistí cache, aby sa obrázky načítali znova."""
        if cls._current_theme != theme:
            cls._current_theme = theme
            cls._image_cache.clear()

    @classmethod
    def preload_images(cls, card_names: list) -> None:
        if not _PYGAME_AVAILABLE:
            return
        cls._image_cache.clear()
        for name in card_names:
            try:
                cls._image_cache[name] = pygame.transform.scale(
                    pygame.image.load(f"assets/cards/{cls._current_theme}/{name}.png"), (80, 140)
                )
            except pygame.error as e:
                logger.error(f"Error loading card image {name} (theme={cls._current_theme}): {e}")
                # Fallback na default theme
                try:
                    cls._image_cache[name] = pygame.transform.scale(
                        pygame.image.load(f"assets/cards/default/{name}.png"), (80, 140)
                    )
                except pygame.error:
                    cls._image_cache[name] = pygame.Surface((80, 140))

    def __init__(self, name: str, value: int, suit: str):
        self.name = name
        self.value = value
        self.suit = suit
        if _PYGAME_AVAILABLE:
            self.image = self._image_cache.get(name, pygame.Surface((80, 140)))

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Card(name={self.name}, value={self.value}, suit={self.suit})"

    def draw(self, screen, x: float, y: float) -> None:
        if _PYGAME_AVAILABLE:
            screen.blit(self.image, (x, y))