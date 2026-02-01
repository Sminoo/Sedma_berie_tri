import pygame
import logging

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

class Card:
    _image_cache = {}

    @classmethod
    def preload_images(cls, card_names: list[str]) -> None:
        for name in card_names:
            if name not in cls._image_cache:
                try:
                    cls._image_cache[name] = pygame.transform.scale(
                        pygame.image.load(f"assets/cards/default/{name}.png"), (80, 140)
                    )
                except pygame.error as e:
                    logger.error(f"Error loading card image {name}: {e}")
                    cls._image_cache[name] = pygame.Surface((80, 140))

    def __init__(self, name: str, value: int, suit: str):
        self.name = name
        self.value = value
        self.suit = suit
        self.image = self._load_image()

    def _load_image(self) -> pygame.Surface:
        return self._image_cache.get(self.name, pygame.Surface((80, 140)))

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Card(name={self.name}, value={self.value}, suit={self.suit})"

    def draw(self, screen: pygame.Surface, x: float, y: float) -> None:
        screen.blit(self.image, (x, y))