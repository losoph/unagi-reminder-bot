import math
import os
import statistics
from datetime import datetime, timedelta, timezone

from data.database import get_connection, normalize_channel_username, serialize_datetime, utc_now

PRIORITIZE_THRESHOLD_PER_DAY = int(os.getenv("PRIORITIZE_THRESHOLD_PER_DAY", "20"))
MAX_POSTS_PER_CHANNEL = int(os.getenv("MAX_POSTS_PER_CHANNEL", "5"))
FORWARD_STANDOUT_RATIO = float(os.getenv("FORWARD_STANDOUT_RATIO", "1.25"))
FORWARD_STANDOUT_MAX_EXTRA = int(os.getenv("FORWARD_STANDOUT_MAX_EXTRA", "3"))

# Configurable score weights
W_FWD = float(os.getenv("SCORE_W_FWD", "0.40"))
W_VIEWS = float(os.getenv("SCORE_W_VIEWS", "0.25"))
W_REACTIONS = float(os.getenv("SCORE_W_REACTIONS", "0.15"))
W_REPLIES = float(os.getenv("SCORE_W_REPLIES", "0.05"))
W_FRESHNESS = float(os.getenv("SCORE_W_FRESHNESS", "0.15"))


def get_channel_medians(channel_username: str, days: int = 30) -> dict:
    """Compute 30-day historical medians for channel metrics.

    Requires at least 3 posts to establish reliable medians (cold-start protection).
    """
    normalized = normalize_channel_username(channel_username)
    since = serialize_datetime(utc_now() - timedelta(days=days))
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT views, reactions_total, forwards, replies, post_time
            FROM channel_posts
            WHERE channel_username = ? AND post_time >= ?
            ''',
            (normalized, since),
        ).fetchall()

    views_list = [r["views"] for r in rows if r["views"] is not None and r["views"] > 0]
    fwd_rates = [
        r["forwards"] / max(r["views"], 1)
        for r in rows
        if r["forwards"] is not None and r["views"] is not None and r["views"] > 0
    ]
    reaction_rates = [
        r["reactions_total"] / max(r["views"], 1)
        for r in rows
        if r["reactions_total"] is not None and r["views"] is not None and r["views"] > 0
    ]

    return {
        "median_views": float(statistics.median(views_list)) if len(views_list) >= 3 else None,
        "median_fwd_rate": float(statistics.median(fwd_rates)) if len(fwd_rates) >= 3 else None,
        "median_reaction_rate": float(statistics.median(reaction_rates)) if len(reaction_rates) >= 3 else None,
        "post_count": len(rows),
    }


def score_post(
    post: dict,
    medians: dict | None = None,
    now: datetime | None = None,
    half_life_h: float = 24.0,
) -> tuple[float, float | None]:
    """Calculate ranking score for a post and return (score, rel_fwd).

    Formula priorities:
    1. Forwards: highest weight, normalized against channel median (fwd_rate / median_fwd_rate).
    2. Views: normalized against channel median with maturity curve adjustment.
    3. Reactions: lower weight with graceful degradation when reactions are disabled.
    4. Replies: optional signal with small weight.
    5. Freshness: exponential decay.
    """
    now = now or utc_now()
    medians = medians or {}

    post_time = post.get("time")
    if post_time is not None:
        if post_time.tzinfo is None:
            post_time = post_time.replace(tzinfo=timezone.utc)
        age_h = max(0.1, (now - post_time).total_seconds() / 3600.0)
    else:
        age_h = 12.0

    # 1. Freshness signal
    freshness = math.exp(-age_h / half_life_h)

    # 2. Forwards signal (rel_fwd)
    views = post.get("views")
    forwards = post.get("forwards")
    median_fwd = medians.get("median_fwd_rate")

    rel_fwd = None
    if forwards is not None and views is not None and views > 0:
        fwd_rate = forwards / views
        if median_fwd is not None and median_fwd > 0:
            rel_fwd = fwd_rate / median_fwd
            fwd_score_term = math.log2(1.0 + rel_fwd)
        else:
            # Cold start
            fwd_score_term = math.log2(1.0 + fwd_rate * 50.0)
    elif forwards is not None and forwards > 0:
        fwd_score_term = math.log2(1.0 + forwards)
    else:
        fwd_score_term = 0.0

    # 3. Views signal (with maturity adjustment)
    median_views = medians.get("median_views")
    if views is not None and views > 0:
        maturity = max(0.25, 1.0 - math.exp(-age_h / 8.0))
        views_adjusted = views / maturity
        if median_views is not None and median_views > 0:
            rel_views = views_adjusted / median_views
            views_score_term = math.log2(1.0 + rel_views)
        else:
            views_score_term = math.log10(views + 1.0) / 4.0
    else:
        views_score_term = 0.0

    # 4. Reactions signal (graceful degradation when reactions are disabled / constant)
    reactions = post.get("reactions_total")
    median_rxn = medians.get("median_reaction_rate")
    if reactions is not None and views is not None and views > 0:
        reaction_rate = reactions / views
        if median_rxn is not None and median_rxn > 0:
            rel_reactions = reaction_rate / median_rxn
            rxn_score_term = math.log2(1.0 + rel_reactions)
        else:
            rxn_score_term = math.log2(1.0 + reaction_rate * 20.0)
    elif reactions is not None and reactions > 0:
        rxn_score_term = math.log2(1.0 + reactions)
    else:
        # Disabled or missing: does not lower score, term contributes 0.0
        rxn_score_term = 0.0

    # 5. Replies signal
    replies = post.get("replies")
    if replies is not None and views is not None and views > 0:
        replies_signal = min(1.0, math.log2(1.0 + (replies / views) * 50.0))
    elif replies is not None and replies > 0:
        replies_signal = min(1.0, math.log2(1.0 + replies))
    else:
        replies_signal = 0.0

    total_score = (
        W_FWD * fwd_score_term
        + W_VIEWS * views_score_term
        + W_REACTIONS * rxn_score_term
        + W_REPLIES * replies_signal
        + W_FRESHNESS * freshness
    )
    return total_score, rel_fwd


def should_prioritize_posts(
    posts: list[dict],
    threshold_per_day: int = PRIORITIZE_THRESHOLD_PER_DAY,
) -> bool:
    """Determine whether a channel's candidate posts exceed the volume threshold."""
    if not posts or len(posts) < threshold_per_day:
        return False
    times = [p["time"] for p in posts if p.get("time")]
    if len(times) >= 2:
        span_days = max(1.0, (max(times) - min(times)).total_seconds() / 86400.0)
    else:
        span_days = 1.0
    return (len(posts) / span_days) >= threshold_per_day


def prioritize_channel_posts(
    channel_username: str,
    posts: list[dict],
    medians: dict | None = None,
    threshold_per_day: int = PRIORITIZE_THRESHOLD_PER_DAY,
    max_posts: int = MAX_POSTS_PER_CHANNEL,
    standout_ratio: float = FORWARD_STANDOUT_RATIO,
    max_extra_standout: int = FORWARD_STANDOUT_MAX_EXTRA,
    now: datetime | None = None,
) -> tuple[list[dict], int]:
    """Select posts for a channel edition.

    - Under threshold_per_day: includes all posts.
    - At or above threshold: takes top-N by score + up to max_extra_standout posts
      with forwards standout (rel_fwd >= standout_ratio).
    Returns (selected_posts, omitted_count).
    """
    if not posts:
        return [], 0

    if not should_prioritize_posts(posts, threshold_per_day=threshold_per_day):
        return list(posts), 0

    if medians is None:
        try:
            medians = get_channel_medians(channel_username)
        except Exception:
            medians = {}

    scored_posts = []
    standout_candidates = []

    for p in posts:
        score, rel_fwd = score_post(p, medians, now=now)
        scored_posts.append((score, rel_fwd, p))
        if rel_fwd is not None and rel_fwd >= standout_ratio:
            standout_candidates.append((rel_fwd, p))

    # Base selection: top max_posts by overall score
    scored_posts.sort(key=lambda item: item[0], reverse=True)
    base_selection = [p for _, _, p in scored_posts[:max_posts]]
    selected_ids = {p.get("id") for p in base_selection if p.get("id") is not None}

    # Standout selection: standout posts not already in base_selection
    extra_standouts = []
    standout_candidates.sort(key=lambda item: item[0], reverse=True)
    for _, p in standout_candidates:
        if p.get("id") not in selected_ids:
            extra_standouts.append(p)
            selected_ids.add(p.get("id"))
            if len(extra_standouts) >= max_extra_standout:
                break

    selected = base_selection + extra_standouts
    selected.sort(key=lambda p: (p.get("id") or 0))
    omitted_count = max(0, len(posts) - len(selected))
    return selected, omitted_count
