"""Registry of implemented platform adapters."""
from app.publishers.facebook import FacebookPublisher
from app.publishers.instagram import InstagramPublisher
from app.publishers.telegram import TelegramPublisher
from app.publishers.tiktok import TikTokPublisher
from app.publishers.youtube import YouTubePublisher

PUBLISHERS: dict = {
    "youtube": YouTubePublisher,
    "telegram": TelegramPublisher,
    "tiktok": TikTokPublisher,
    "instagram": InstagramPublisher,
    "facebook": FacebookPublisher,
}
