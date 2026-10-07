"""Registry of implemented platform adapters."""
from app.publishers.telegram import TelegramPublisher
from app.publishers.youtube import YouTubePublisher

PUBLISHERS: dict = {
    "telegram": TelegramPublisher,
    "youtube": YouTubePublisher,
}
