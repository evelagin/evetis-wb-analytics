"""Builds the production ``Deps`` bundle (cached singletons).

Clients are created on first use; secrets are read from Secret Manager at that
point (never at import time), so `import app.main` needs no credentials.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.services.bigquery_repository import BigQueryRepository
from app.services.openai_client import OpenAIClient
from app.services.pipeline import Deps
from app.services.prompt_service import PromptService
from app.services.repository import FirestoreRepository
from app.services.telegram_client import TelegramClient
from app.services.wb_client import WBClient
from app.utils.logging import get_logger

logger = get_logger(__name__)


def build_shadow_components(settings, openai, bq):
    """Decide whether to build the Communication Engine v2 SHADOW components.

    * disabled -> (None, None).
    * PRIMARY mode -> (None, None): v2 is the real generator, so a separate shadow
      comparison run is redundant (handled by ``build_primary_engine``).
    * enabled + shadow_only -> build the shadow runner.
    * enabled + not shadow_only + not primary -> FAIL-CLOSED: the engine is NOT
      built and a CRITICAL log is emitted (misconfiguration; no silent fallback).

    Returns ``(shadow_engine, shadow_repo)``.
    """
    if not settings.communication_engine_v2_enabled:
        return None, None
    if getattr(settings, "communication_engine_v2_primary", False):
        return None, None  # primary mode owns generation; no shadow comparison
    if not settings.communication_engine_v2_shadow_only:
        logger.critical(
            "COMMUNICATION_ENGINE_V2_SHADOW_ONLY=false without _V2_PRIMARY=true is a "
            "misconfiguration: refusing to build the v2 engine (fail-closed). "
            "reviews_v1 continues unaffected; no v2 publish path exists."
        )
        return None, None
    from app.services.engine_prompt_service import EnginePromptService
    from app.services.shadow_engine import ShadowEngineService

    engine = ShadowEngineService(
        engine_service=EnginePromptService.create(), openai=openai, settings=settings
    )
    return engine, bq


def build_engine(settings):
    """Build the v2 EnginePromptService when the pipeline needs it — i.e. when v2
    is the PRIMARY review generator OR when WB questions are enabled (questions
    always use the engine, since reviews_v1 has no question template).

    Returns ``None`` when v2 is disabled or neither consumer needs it, so
    reviews_v1 stays the review generator. Publishing gates are unaffected.
    """
    if not settings.communication_engine_v2_enabled:
        return None
    primary = getattr(settings, "communication_engine_v2_primary", False)
    questions = getattr(settings, "wb_questions_enabled", False)
    if not (primary or questions):
        return None
    from app.services.engine_prompt_service import EnginePromptService

    logger.info(
        "Communication Engine v2 engine built (primary=%s, questions=%s)", primary, questions
    )
    return EnginePromptService.create()


# Back-compat alias (older name used by some tests).
build_primary_engine = build_engine


@lru_cache(maxsize=1)
def get_deps() -> Deps:
    settings = get_settings()
    secrets = settings.secrets
    openai = OpenAIClient(settings, secrets.openai_api_key)
    bq = BigQueryRepository(settings)

    shadow_engine, shadow_repo = build_shadow_components(settings, openai, bq)
    engine = build_engine(settings)

    return Deps(
        settings=settings,
        repo=FirestoreRepository(settings),
        wb=WBClient(settings, secrets.wb_api_token),
        openai=openai,
        telegram=TelegramClient(secrets.telegram_bot_token),
        bq=bq,
        prompts=PromptService(settings.prompts_dir, settings.prompt_version),
        engine=engine,
        shadow_engine=shadow_engine,
        shadow_repo=shadow_repo,
    )
