"""Registry of implemented platform adapters."""
from app.publishers.bluesky import BlueskyPublisher
from app.publishers.dailymotion import DailymotionPublisher
from app.publishers.facebook import FacebookPublisher
from app.publishers.instagram import InstagramPublisher
from app.publishers.likee import LikeePublisher
from app.publishers.pinterest import PinterestPublisher
from app.publishers.rumble import RumblePublisher
from app.publishers.snapchat import SnapchatPublisher
from app.publishers.telegram import TelegramPublisher
from app.publishers.threads import ThreadsPublisher
from app.publishers.tiktok import TikTokPublisher
from app.publishers.vimeo import VimeoPublisher
from app.publishers.vk import VKPublisher
from app.publishers.x import XPublisher
from app.publishers.youtube import YouTubePublisher

PUBLISHERS: dict = {
    "youtube": YouTubePublisher,
    "telegram": TelegramPublisher,
    "tiktok": TikTokPublisher,
    "instagram": InstagramPublisher,
    "facebook": FacebookPublisher,
    "x": XPublisher,
    "threads": ThreadsPublisher,
    "bluesky": BlueskyPublisher,
    "pinterest": PinterestPublisher,
    "rumble": RumblePublisher,
    "dailymotion": DailymotionPublisher,
    "vimeo": VimeoPublisher,
    "snapchat": SnapchatPublisher,
    "likee": LikeePublisher,
    "vk": VKPublisher,
}
