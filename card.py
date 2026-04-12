"""
Sedma Bere Tri - Card
Card class with image caching for Pygame rendering.
"""

import pygame
import logging
from typing import List

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)


class Card:
    """Represents a playing card with a name, value, suit, and preloaded image."""

    _image_cache = {}

    @classmethod
    def preload_images(cls, card_names: List[str], theme: str = "default") -> None:
        """Pre-load and cache card images for all given card names."""
        cls._image_cache.clear()
        for name in card_names:
            try:
                cls._image_cache[name] = pygame.transform.scale(
                    pygame.image.load(f"assets/cards/{theme}/{name}.png"), (80, 140)
                )
            except pygame.error as e:
                logger.error(f"Error loading card image {name}: {e}")
                cls._image_cache[name] = pygame.Surface((80, 140))

    def __init__(self, name: str, value: int, suit: str):
        self.name = name
        self.value = value
        self.suit = suit
        self.image = self._image_cache.get(self.name, pygame.Surface((80, 140)))

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Card(name={self.name}, value={self.value}, suit={self.suit})"

    def draw(self, screen: pygame.Surface, x: float, y: float) -> None:
        """Draw the card at the specified position."""
        screen.blit(self.image, (x, y))