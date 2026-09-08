"""Enumerations and stable string keys for the communication engine.

Every string that carries meaning lives here (or in EngineConfig), so business
logic never hard-codes magic strings.
"""
from __future__ import annotations

from enum import Enum


class Namespace(str, Enum):
    """Top-level knowledge folders."""

    BRAND = "brand"
    PRODUCTS = "products"
    CASES = "cases"
    MARKETPLACES = "marketplaces"


class Marketplace(str, Enum):
    # Wildberries-only engine: other marketplaces are handled by separate
    # systems and are deliberately not mixed in here (no shared «пульт»).
    WB = "wb"


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class Urgency(str, Enum):
    NORMAL = "normal"
    HIGH = "high"


class Language(str, Enum):
    RU = "ru"
    OTHER = "other"


class CommunicationType(str, Enum):
    """Kind of buyer communication the engine answers."""

    REVIEW = "review"
    QUESTION = "question"
    CHAT = "chat"


class SourceStatus(str, Enum):
    """Provenance of a product's facts."""

    VERIFIED = "verified"      # confirmed against ТУ / live card
    DRAFT = "draft"            # plausible but not yet confirmed
    UNVERIFIED = "unverified"  # unknown / must not be published


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"  # -> route to manual moderation


# YAML front-matter keys (single source of truth for the markdown schema).
class MetaKey(str, Enum):
    ID = "id"
    VERSION = "version"
    TAGS = "tags"
    NAME = "name"
    LINE = "line"
    SUPPLIER_ARTICLES = "supplier_articles"   # seller vendorCode(s)
    WB_NM_IDS = "wb_nm_ids"                    # WB nmID(s)
    WB_IMT_IDS = "wb_imt_ids"                  # WB imtID (parent card id)
    BARCODES = "barcodes"                      # WB barcode(s) / GTIN
    ACTIVES = "actives"
    KEYWORDS = "keywords"
    URGENCY = "urgency"
    PRIORITY = "priority"
    ALIASES = "aliases"
    ID_STATUS = "id_status"     # verified | unverified — provenance of the ids
    SOURCE_STATUS = "source_status"
    VERIFIED_FACTS = "verified_facts"      # physical facts + concentrations + regime
    VERIFIED_CLAIMS = "verified_claims"    # explicitly approved marketing claims
    PROHIBITED_CLAIMS = "prohibited_claims"
    VOLUME_ML = "volume_ml"
    WEIGHT_G = "weight_g"
    PH = "ph"
    AROMA = "aroma"
    COUNTRY = "country"
    DECLARATION = "declaration"
    AGE = "age"
    IS_BUNDLE = "is_bundle"
    BUNDLE_COMPONENTS = "bundle_components"


# Marketplace aliases seen in the `platform` field of incoming reviews.
# Wildberries-only; any other/unknown platform falls back to WB by default.
MARKETPLACE_ALIASES: dict[str, Marketplace] = {
    "wb": Marketplace.WB,
    "wildberries": Marketplace.WB,
}
