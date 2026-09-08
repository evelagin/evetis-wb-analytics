"""Sentiment classifier: derives sentiment from the star rating via config
thresholds. No rating -> neutral.
"""
from __future__ import annotations

from app.communication_engine.config import EngineConfig
from app.communication_engine.constants import Sentiment
from app.communication_engine.models.classification import ReviewInput


class SentimentClassifier:
    def __init__(self, config: EngineConfig):
        self._config = config

    def classify(self, review: ReviewInput) -> Sentiment:
        rating = review.rating
        if rating is None:
            return Sentiment.NEUTRAL
        if rating <= self._config.sentiment_negative_max_rating:
            return Sentiment.NEGATIVE
        if rating >= self._config.sentiment_positive_min_rating:
            return Sentiment.POSITIVE
        return Sentiment.NEUTRAL
