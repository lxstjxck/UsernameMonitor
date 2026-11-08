"""Conservative market evidence and a separate, non-price commercial score."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from statistics import median

from .names import USERNAME_PATTERN

# Small hand-reviewed vocabularies; neither brand clearance nor a dictionary.
WORDS = ("amber", "atlas", "bloom", "bright", "cloud", "coral", "craft", "dawn",
         "delta", "drift", "ember", "fable", "field", "flame", "flora", "forge",
         "glide", "grove", "haven", "lunar", "maple", "minty", "mossy", "noble",
         "north", "ocean", "orbit", "pearl", "pixel", "plume", "prism", "river",
         "solar", "spark", "stone", "swift", "terra", "tiger", "vivid", "wavey",
         "acorn", "adore", "alive", "alpine", "angel", "apricot", "arcade", "arctic",
         "arrow", "artist", "autumn", "azure", "basil", "beacon", "berry", "birch",
         "bliss", "blossom", "breeze", "bronze", "brook", "canvas", "cedar", "charm",
         "cheery", "cinder", "citrus", "clover", "comet", "cosmic", "crystal", "curious",
         "dainty", "daring", "daylight", "doodle", "dove", "echoes", "elixir", "elmwood",
         "enigma", "falcon", "feather", "fern", "ferry", "fluent", "forest", "frost",
         "galaxy", "gentle", "glimmer", "golden", "harbor", "harmony", "horizon", "humble",
         "indigo", "island", "jasmine", "joyful", "juniper", "kindle", "lantern", "lavender",
         "legend", "lemon", "light", "linen", "lively", "lotus", "marble", "meadow",
         "melody", "merlin", "meteor", "mingle", "mirror", "mosaic", "motion", "mountain",
         "muse", "myrtle", "nectar", "nimble", "opaline", "orchid", "origin", "otter",
         "papaya", "pastel", "petal", "pioneer", "planet", "poetry", "pollen", "ripple",
         "robin", "rocket", "saffron", "sailor", "satin", "shadow", "silver", "simple",
         "sketch", "smooth", "sprout", "summer", "sunny", "talent", "thrive", "timber",
         "tulip", "twinkle", "velvet", "voyage", "whimsy", "willow", "winter", "wonder",
         "wooden", "yellow", "yonder", "zephyr")
THEMES = ("audio", "code", "data", "design", "dream", "garden", "game", "green",
          "learn", "logic", "music", "play", "studio", "travel", "voice")
PREFIXES = ("aur", "blu", "cora", "lumi", "mira", "nexa", "riva", "sona", "velo", "zeno")
SUFFIXES = ("ara", "ero", "ia", "io", "ora", "ova", "vio", "wise")

# Illustrative blocked terms only. This is not a legal or identity registry.
BLOCKED = ("telegram", "fragment", "toncoin", "google", "amazon", "apple", "microsoft",
           "netflix", "tiktok", "elonmusk", "durov", "openai", "chatgpt", "official",
           "support", "verified", "helpdesk")


def unsafe_name(name: str) -> bool:
    normalized = name.translate(str.maketrans("01357", "oies t".replace(" ", ""))).replace("_", "")
    return (not USERNAME_PATTERN.fullmatch(name) or any(term in normalized for term in BLOCKED)
            or name.endswith(("_official", "_support")))


def classify(name: str) -> str:
    if name in WORDS:
        return "word"
    if any(name == a + b for a in WORDS for b in THEMES if a != b):
        return "semantic"
    if any(name == a + b for a in PREFIXES for b in SUFFIXES):
        return "thematic"
    return "brand"


@dataclass(frozen=True)
class Evidence:
    status: str
    count: int = 0
    median_gram: float | None = None
    spread: float | None = None
    liquidity: str = "unknown"
    confidence: str = "none"
    reason: str = "insufficient_data"


def analyze_sales(sales, category: str, *, as_of: datetime, complete: bool) -> Evidence:
    """Only an unbiased complete sample with >=5 independent recent comparables."""
    if not complete:
        return Evidence("insufficient_data", reason="sample_not_representative")
    cutoff = as_of - timedelta(days=365)
    comparable = []
    for row in sales:
        at = datetime.fromisoformat(row["sold_at"].replace("Z", "+00:00"))
        if (row["exclusion_reason"] is None and len(row["username"]) >= 5
                and cutoff <= at <= as_of and classify(row["username"]) == category):
            comparable.append(row)
    # One sale per username per 60 days, sorted deterministically by date.
    independent = []
    seen = {}
    for row in sorted(comparable, key=lambda r: (r["sold_at"], r["username"])):
        at = datetime.fromisoformat(row["sold_at"].replace("Z", "+00:00"))
        if row["username"] not in seen or at - seen[row["username"]] >= timedelta(days=60):
            independent.append(row)
            seen[row["username"]] = at
    if len(independent) < 5:
        return Evidence("insufficient_data", len(independent), reason="few_comparables")
    prices = [row["price_gram"] for row in independent]
    baseline = median(prices)
    filtered = [p for p in prices if baseline / 8 <= p <= baseline * 8]
    if len(filtered) < 5:
        return Evidence("insufficient_data", len(filtered), reason="outliers_or_few_comparables")
    mid = median(filtered)
    spread = round((max(filtered) - min(filtered)) / mid, 3)
    liquidity = "higher" if len(filtered) >= 30 else "thin"
    confidence = "medium" if len(filtered) >= 30 and spread <= 2 else "low"
    return Evidence("observed", len(filtered), round(mid, 3), spread, liquidity, confidence,
                    "completed_sales_median")


def commercial_rating(name: str, name_score: int, evidence: Evidence, *, as_of: datetime,
                      observed_at: datetime | None) -> dict:
    category = classify(name)
    if unsafe_name(name):
        return dict(score=None, confidence="none", status="excluded", reason="identity_or_brand_risk")
    if observed_at is None or observed_at > as_of or as_of - observed_at > timedelta(days=30):
        return dict(score=None, confidence="none", status="insufficient_data",
                    reason="stale_cache" if observed_at and as_of - observed_at > timedelta(days=30)
                    else "no_verified_cache")
    if evidence.status != "observed" or evidence.confidence != "medium":
        return dict(score=None, confidence=evidence.confidence, status="insufficient_data",
                    reason=evidence.reason)
    # This 0–100 score is a priority signal, never a price/ROI prediction.
    bonus = {"word": 12, "semantic": 8, "brand": 3, "thematic": 4}[category]
    liquidity = min(10, evidence.count // 5)
    score = max(0, min(100, round(name_score * .75 + bonus + liquidity)))
    return dict(score=score, confidence=evidence.confidence, status="observed",
                reason=f"category={category}; evidence={evidence.reason}; completed_sales={evidence.count}; "
                       f"median_gram={evidence.median_gram}; spread={evidence.spread}; "
                       f"liquidity={evidence.liquidity}")
