"""ニュース・天気・ペルソナ・時刻から画像/動画用プロンプトを生成する。"""

from __future__ import annotations

import random
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

import pipeline_config as cfg

_HOUR_SCENE = {
    range(0, 6):   ("深夜〜夜明け前", "very dark night, quiet, dim streetlight"),
    range(6, 11):  ("朝",             "bright morning light level, fresh atmosphere (light color/warmth must follow weather data: warm golden tones ONLY if weather is clear; flat cool grey-white diffused light if rain/cloud/fog — do not default to golden sunrise)"),
    range(11, 17): ("昼",             "bright midday, natural daylight, vivid colors, active atmosphere"),
    range(17, 20): ("夕方",           "warm light level appropriate for early evening (light color/warmth must follow weather data: warm orange sunset/golden hour tones ONLY if weather is clear; flat cool grey diffused light if rain/cloud — do not default to a visible sunset)"),
    range(20, 24): ("夜",             "dark night, NO natural light, NO daylight, NO twilight, artificial lighting only, dim warm indoor glow or neon signs, nighttime atmosphere"),
}


def _scene_hint(hour: int) -> tuple[str, str]:
    for r, v in _HOUR_SCENE.items():
        if hour in r:
            return v
    return ("昼", "natural daylight")


_WEATHER_VISUAL_RULES = [
    (("雨",), "wet pavement, puddles reflecting light, rain droplets on surfaces, overcast or rain-streaked sky, FLAT cool grey-white diffused light with NO visible sun. NEVER use clear/sunny/blue-sky/golden-sunlight/sunrise/sunset/warm-backlight vocabulary."),
    (("雪",), "snow-covered ground, falling snowflakes, cold muted winter light, FLAT diffused light with NO visible sun. NEVER use clear/sunny/blue-sky/golden-sunlight vocabulary."),
    (("曇",), "overcast diffused light, soft flat shadows, grayish-white sky, NO visible sun, NO golden tones. NEVER use bright sunny/clear-sky/golden-sunlight vocabulary."),
    (("霧", "もや"), "hazy atmosphere, reduced visibility, soft diffused glow, NO visible sun. NEVER use sharp clear-sky/golden-sunlight vocabulary."),
]


def _weather_warning(description: str) -> str:
    """晴天以外の天気の場合、視覚要素を強制する警告文を返す。"""
    for keywords, rule in _WEATHER_VISUAL_RULES:
        if any(kw in description for kw in keywords):
            return f"\n⚠️ 天気は「{description}」です。最優先で反映すること: {rule}"
    return ""


_IMAGE_SYSTEM_NEWS = """\
You are a professional image prompt engineer for Stable Diffusion / Flux image generation models.
Generate ONE detailed English scene prompt for a Tokyo lifestyle photo.
Do NOT describe the character's physical appearance (body type, hair color/style, glasses) — those are handled separately.

Rules:
- VARIATION: Every call must produce a meaningfully different scene. Vary location, outfit, pose, props, mood.
- NEWS INSPIRATION: Use the featured headline as the creative source for background setting and outfit theme. Do NOT depict the news literally. Examples: finance/stocks → sleek Marunouchi business district, smart tailored look; nature/animal → botanical garden, earthy natural tones; politics/law → quiet library or civic plaza, structured clean outfit.
- TIME-OF-DAY LIGHTING (strictly enforced): Follow the provided lighting hint exactly. If the hint says "dark night, NO natural light" — the scene must be dark with artificial lighting only. Never use "twilight", "sunset", or daylight vocabulary for night hours.
- COMPOSITION (strictly enforced): Use the exact shot type, angle, and pose specified. Describe it clearly in the prompt.
- Output: SINGLE paragraph in English ONLY. No Japanese. No titles, no markdown, no bullet points, no headers. Under 200 words.
- No violence, no explicit content.
"""

_IMAGE_SYSTEM_EVENT = """\
You are a professional image prompt engineer for Stable Diffusion / Flux image generation models.
Generate ONE detailed English scene prompt for a Tokyo lifestyle photo.
Do NOT describe the character's physical appearance (body type, hair color/style, glasses) — those are handled separately.

Rules:
- EVENT PRIORITY: The featured event is the PRIMARY driver of scene and outfit. The character is attending or about to attend this event — depict the venue, atmosphere, and outfit that fits the event directly. Examples: fireworks festival → yukata or summer dress, riverside or park crowd; art exhibition → smart casual, gallery interior or entrance; music festival → casual streetwear, outdoor stage area.
- Weather affects the atmosphere (rain → umbrellas, heat → light fabrics) but the event determines the location and outfit theme.
- TIME-OF-DAY LIGHTING (strictly enforced): Follow the provided lighting hint exactly.
- COMPOSITION (strictly enforced): Use the exact shot type, angle, and pose specified.
- Output: SINGLE paragraph in English ONLY. No Japanese. No titles, no markdown, no bullet points, no headers. Under 200 words.
- No violence, no explicit content.
"""

_IMAGE_SYSTEM_BIKINI = """\
You are a professional resort/swimwear photography prompt engineer for Stable Diffusion / Flux image generation models.
Generate ONE detailed English scene prompt featuring her wearing a bikini or swimsuit.
Do NOT describe the character's physical appearance (body type, hair color/style, glasses) — those are handled separately.

Rules:
- OUTFIT-FIRST: The swimwear itself is the central theme of this shot. Choose a background/location that naturally suits a swimwear photo (e.g. resort pool, infinity pool, beach, hotel terrace, hot spring, water park) — let the swimwear's color/style/vibe guide which location fits best.
- Use the provided swimwear trend reference material to decide the print, color, silhouette, and styling details of the swimsuit.
- LOCATION SCOUTING: Before deciding the pose, mentally inventory what's actually present at the chosen location (pool edge, lounge chair, railing, steps, towel, palm trees, water surface, etc.) and pick a pose that naturally fits one of those elements, rather than a generic standing pose.
- POSE & ANGLE: Vary stance (standing / sitting / leaning / walking) and camera angle. A slightly low angle is welcome for a flattering full-body resort photo (elongates the legs, emphasizes natural body lines) — keep it tasteful, editorial, not explicit.
- HAND/GESTURE DETAIL: Add one natural, tasteful hand or gesture detail to bring life to the pose, chosen freely to fit the scene (e.g. running a hand through hair, fingertips resting on cheek, holding a drink, adjusting a strap, leaning on a railing) — avoid anything explicit or suggestive of undressing, and avoid defaulting to the same prop/gesture every time.
- TEXTURE & LIGHT: Include skin/light quality keywords for realism (e.g. soft natural skin texture, warm golden-hour glow, sun-kissed skin tone) where appropriate.
- LIGHTING: Always bright, flattering, sunny resort-style lighting — golden hour or bright clear daylight. Do NOT reference any specific real-world current time or weather condition.
- COMPOSITION (strictly enforced): Use the exact shot type, angle, and pose specified.
- Output: SINGLE paragraph in English ONLY. No Japanese. No titles, no markdown, no bullet points, no headers. Under 200 words.
- No violence, no explicit content, no nudity, no focus on intimate body parts.
"""

_IMAGE_SYSTEM_WEATHER = """\
You are a professional fashion stylist and image prompt engineer for Stable Diffusion / Flux models.
Generate ONE detailed English scene prompt for a Tokyo lifestyle photo.
Do NOT describe the character's physical appearance (body type, hair color/style, glasses) — those are handled separately.

Fashion rules (CRITICAL):
- Draw on your knowledge of CURRENT Tokyo street fashion trends for the given season/month. Be specific about styles (e.g., June 2026: sheer layering, linen sets, Y2K revival accessories, quiet luxury minimalism, oversized blazers as summer outerwear).
- Outfit must match BOTH the temperature AND current Tokyo trends — not generic "summer dress". Be precise: fabric, silhouette, color palette, specific garment names, accessories.
- Weather must visibly affect the scene atmosphere (rain → wet reflections, puddles; humid haze → soft diffused light; clear hot → harsh sun and shade contrast; night rain → neon reflections on asphalt).
- TIME-OF-DAY LIGHTING (strictly enforced): Follow the provided lighting hint exactly. If the hint says "dark night, NO natural light" — the scene must be dark with artificial lighting only. Never use "twilight", "sunset", or daylight vocabulary for night hours.
- COMPOSITION (strictly enforced): Use the exact shot type, angle, and pose specified. Describe it clearly in the prompt.
Output: SINGLE paragraph in English ONLY. No Japanese. No titles, no markdown, no bullet points, no headers. Under 220 words. No violence, no explicit content.
"""

_VIDEO_SYSTEM = """\
You are writing a SHORT raw scene prompt in Japanese for a character video.
Given a persona description, context (news/weather), and time-of-day hint,
write a 1–2 sentence Japanese scene description that:
- Describes what the character is doing and where (casual daily life — vary location each time).
- Optionally includes 1 short line of cute natural dialogue (her voice, in quotes).
- Reflects the time-of-day and the featured headline or weather subtly.
- Is concrete and visual — suitable as input to a text-to-video model.
Output ONLY the raw scene prompt in Japanese. No explanations, no markdown.
"""


_SEASON = {
    (12, 1, 2): ("冬", "winter"),
    (3, 4, 5):  ("春", "spring"),
    (6, 7, 8):  ("夏", "summer"),
    (9, 10, 11):("秋", "autumn"),
}

_COMPOSITIONS = [
    "full body shot, standing, facing front",
    "full body shot, standing, side view",
    "full body shot, standing, turned away looking back over shoulder",
    "full body shot, sitting, facing front",
    "full body shot, sitting, side view",
    "thigh-up shot, standing, facing front",
    "thigh-up shot, standing, slight side angle",
    "thigh-up shot, sitting, facing front",
    "thigh-up shot, turned away looking back over shoulder",
    "bust-up shot, facing front",
    "bust-up shot, slight side angle, looking at camera",
    "bust-up shot, looking down softly",
    "bust-up shot, looking up slightly",
    "bust-up shot, turned away looking back over shoulder",
]

_HOME_COLOR_PALETTES = [
    "lavender / soft purple",
    "dusty pink / blush",
    "soft blue / powder blue",
    "terracotta / warm rust",
    "charcoal grey / black",
    "mustard yellow / ochre",
    "burgundy / wine red",
    "warm beige / camel (no green, no mint)",
    "navy / denim blue",
    "soft coral / peach",
]

_LOCATIONS_BY_HOUR: list[tuple[range, list[str]]] = [
    (range(6, 10), [
        "a convenience store in the early morning",
        "a local supermarket just after opening",
        "a morning vegetable market",
        "Nakameguro canal walkway in the morning",
        "a suburban park with benches in the morning",
        "a train platform during morning rush hour",
        "a quiet residential street in the morning",
        "a rooftop of an apartment building at sunrise",
    ]),
    (range(10, 17), [
        "a Shimokitazawa vintage clothing shop",
        "Harajuku Takeshita-dori street",
        "an Aoyama boutique interior",
        "Daikanyama Tsutaya bookstore",
        "Nakameguro canal walkway",
        "a Ginza department store cosmetics floor",
        "a community indoor pool",
        "a suburban park with benches",
        "a Shibuya crossing side street",
        "a Yanaka old-town shotengai",
        "a local supermarket",
        "a convenience store",
    ]),
    (range(17, 20), [
        "Nakameguro canal walkway at golden hour",
        "a rooftop garden bar at sunset",
        "a Shibuya crossing side street at dusk",
        "a train platform during evening rush hour",
        "a local supermarket in the evening",
        "a convenience store in the evening",
        "a Shimokitazawa vintage clothing shop before closing",
        "an underground music venue lobby",
    ]),
    (range(20, 22), [
        "a convenience store at night",
        "a quiet Shinjuku izakaya alley at night",
        "a rooftop garden bar at night",
        "an underground music venue lobby",
        "a local sento (public bath) changing room",
        "Nakameguro canal walkway at night",
    ]),
]


def _pick_location(hour: int) -> tuple[str, bool]:
    """時間帯に合ったロケーションをランダムに返す。(location, is_home) のタプル。"""
    for r, locations in _LOCATIONS_BY_HOUR:
        if hour in r:
            return random.choice(locations), False
    return "her cozy apartment room (home)", True


def _get_season(month: int) -> tuple[str, str]:
    for months, v in _SEASON.items():
        if month in months:
            return v
    return ("夏", "summer")


def _pick_news(news: list[str]) -> str:
    """ランダムに1件選ぶ。"""
    return random.choice(news) if news else ""


def _load_wardrobe_history() -> list[str]:
    """直近の衣装履歴を返す。ファイルがなければ空リスト。"""
    try:
        lines = cfg.WARDROBE_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        return [l.strip() for l in lines if l.strip()][-cfg.WARDROBE_HISTORY_MAX:]
    except FileNotFoundError:
        return []


def _save_wardrobe_history(wardrobe_line: str) -> None:
    """衣装履歴に1件追記し、最大件数を超えた分を先頭から削除する。"""
    if not wardrobe_line:
        return
    history = _load_wardrobe_history()
    history.append(wardrobe_line)
    history = history[-cfg.WARDROBE_HISTORY_MAX:]
    cfg.WARDROBE_HISTORY_PATH.write_text("\n".join(history) + "\n", encoding="utf-8")


def _extract_wardrobe(prompt: str) -> str:
    """構造化プロンプトから Wardrobe 行の値を抽出する。"""
    for line in prompt.splitlines():
        if line.startswith("Wardrobe:"):
            return line[len("Wardrobe:"):].strip()
    return ""


def _load_events_today() -> str:
    """tokyo_events.md から本日開催のイベント行だけ抽出して返す。"""
    try:
        text = cfg.TOKYO_EVENTS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"[events] not found: {cfg.TOKYO_EVENTS_PATH} (スキップ)")
        return ""

    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    m, d = now.month, now.day
    # 今日の日付にマッチするパターン群
    patterns = [
        f"{m}月{d}日",
        f"{m:02d}月{d:02d}日",
        f"{m}/{d}",
        f"{m:02d}/{d:02d}",
        f"2026/{m}/{d}",
        f"2026/{m:02d}/{d:02d}",
        f"2026.{m:02d}.{d:02d}",
        f"2026年{m}月{d}日",
    ]

    matched = [
        line for line in text.splitlines()
        if any(p in line for p in patterns)
    ]

    if matched:
        result = "\n".join(matched)
        print(f"[events] today's events ({len(matched)} items): {result[:80]}...")
        return result

    print("[events] 本日のイベントなし")
    return ""


def _load_trend_cache() -> str:
    """fashion_trends.md を読み込む。ファイルがなければ空文字を返す。"""
    try:
        text = cfg.TREND_CACHE_PATH.read_text(encoding="utf-8").strip()
        print(f"[trend] loaded: {cfg.TREND_CACHE_PATH} ({len(text)} chars)")
        return text
    except FileNotFoundError:
        print(f"[trend] not found: {cfg.TREND_CACHE_PATH} (スキップ)")
        return ""


_SWIMWEAR_TREND_PATH = Path(__file__).parent / "swimwear_trends_2026.md"


def _load_swimwear_trends() -> str:
    """swimwear_trends_2026.md を読み込む(固定パス、.env設定不要)。ファイルがなければ空文字を返す。"""
    try:
        text = _SWIMWEAR_TREND_PATH.read_text(encoding="utf-8").strip()
        print(f"[swimwear] loaded: {_SWIMWEAR_TREND_PATH} ({len(text)} chars)")
        return text
    except FileNotFoundError:
        print(f"[swimwear] not found: {_SWIMWEAR_TREND_PATH} (スキップ)")
        return ""


def _pick_swimwear_trend() -> str:
    """swimwear_trends_2026.md の「### N. ...」セクションを1つだけランダムに選んで返す。
    全トレンド+カラー表をまるごとLLMに渡すと、文書内で目立つ単語(バターイエロー等)に偏るため、
    newsモードの _pick_news() と同様にコード側で1件だけ選んでから渡す。
    """
    text = _load_swimwear_trends()
    if not text:
        return ""
    # "### N." または "## " (上位見出し) の手前で区切る。最後のトレンド項目が
    # 次の "## カラートレンド" 等の無関係な後続セクションまで取り込んでしまうバグの対策。
    sections = re.split(r"\n(?=##+ )", text)
    trend_sections = [s.strip() for s in sections if re.match(r"### \d+\.", s.strip())]
    if not trend_sections:
        return text  # セクション形式でなければ全文をフォールバックで返す
    return random.choice(trend_sections)


_FOOTWEAR_KEYWORDS = (
    "shoes", "sandals", "boots", "sneakers", "heels", "loafers", "mules",
    "flats", "pumps", "slippers", "clogs", "footwear",
    "oxfords", "derbies", "wedges", "espadrilles", "flip-flops", "thongs",
    "stilettos", "mary janes", "ballet flats", "ankle strap", "platform shoes",
    "サンダル", "靴", "ブーツ", "スニーカー", "ヒール", "パンプス",
)

_BAG_KEYWORDS = (
    "bag", "tote", "clutch", "purse", "handbag", "crossbody", "shoulder bag",
    "backpack", "satchel", "バッグ",
)


def _remove_items_from_wardrobe(prompt: str, keywords: tuple[str, ...]) -> str:
    """Wardrobe 行から指定キーワードに合致するアイテムをコードで削除する。"""
    lines = prompt.splitlines()
    result = []
    for line in lines:
        if line.startswith("Wardrobe:"):
            prefix = "Wardrobe:"
            items = [item.strip() for item in line[len(prefix):].split(",")]
            filtered = [
                item for item in items
                if not any(kw in item.lower() for kw in keywords)
            ]
            line = f"{prefix} {', '.join(filtered)}"
        result.append(line)
    return "\n".join(result)


def _remove_footwear_from_wardrobe(prompt: str) -> str:
    """full body shot でない場合、Wardrobe 行から靴・サンダル系のアイテムをコードで削除する。"""
    return _remove_items_from_wardrobe(prompt, _FOOTWEAR_KEYWORDS)


def _build_user_prompt(persona: str, context: dict, context_mode: str, hour: int) -> tuple[str, bool, str, str]:
    """ユーザープロンプト文字列、is_home フラグ、composition、featured_news を返す。"""
    now_jst = datetime.now(ZoneInfo("Asia/Tokyo"))
    month = now_jst.month
    season_jp, season_en = _get_season(month)
    composition = random.choice(_COMPOSITIONS)
    is_home = False

    if context_mode == "bikini":
        # 天気・現在時刻は無視。常に明るいリゾート的な光で生成する
        lines = [
            "/no_think",
            f"## 季節\n{month}月 / {season_jp}({season_en})",
            f"\n## 今回の構図(必ずこれを使う)\n{composition}",
        ]
        trend = _pick_swimwear_trend()
        if trend:
            lines.append(f"\n## 今回採用する2026年水着トレンド(これに基づいて水着を考案すること)\n{trend}")
        lines.append(
            f"\n## 指示\n上記のトレンドに基づいて彼女に似合う具体的な水着デザイン(色・柄・シルエット)を1つ考案し、その水着の雰囲気に合うロケーションを決めてください。"
            f"ロケーションは水着に似合う場所(プールサイド・ビーチ・温泉・リゾートテラス等)から自由に選ぶこと。\n\n"
            f"⚠️ 出力は必ず**英語の1段落テキストのみ**。日本語・マークダウン・見出し・箇条書き・提案文は絶対禁止。"
        )
        return "\n".join(lines), is_home, composition, ""

    scene_label, scene_lighting = _scene_hint(hour)
    w = context.get("weather", {})
    weather_str = f"{w.get('description', '')} / {w.get('temp_c', '?')}°C / 風{w.get('wind_kmh', '?')}km/h"

    lines = [
        "/no_think",
        f"## 現在時刻\n{context.get('datetime_jst', '')} ({scene_label}) — {month}月 / {season_jp}({season_en})",
        f"照明ヒント(英語): {scene_lighting}",
        "⚠️ 時間帯の明るさ(上記の照明ヒント)は最優先で厳守すること。指定と矛盾する明るさ表現は絶対禁止。",
    ]

    if context_mode == "weather":
        # 15〜19時はイベント優先モード判定
        today_event = ""
        if 15 <= hour < 20:
            today_event = _load_events_today()

        if hour >= 22 or hour < 6:
            location, is_home = "her cozy apartment room (home)", True
        elif hour == 7:
            location, is_home = (
                "standing in front of an open closet full of hanging clothes in her bedroom at home, "
                "holding up two different clothing items (e.g. two tops, or a dress vs a skirt set) "
                "side by side at arm's length to compare them — she is mid-decision, not yet dressed for the day"
            ), True
        elif today_event:
            location, is_home = "", False  # イベント優先: ロケーションはLLMに委ねる
        else:
            location, is_home = _pick_location(hour)

        lines.append(f"\n## 天気\n{weather_str}{_weather_warning(w.get('description', ''))}")

        if today_event:
            lines.append(f"\n## 本日の東京イベント(シーンと衣装の主要テーマ。これに合わせてロケーション・衣装を決めること)\n{today_event}")
        else:
            lines.append(f"\n## 今回のロケーション(必ずこれを使う)\n{location}")

        lines.append(f"\n## 今回の構図(必ずこれを使う)\n{composition}")

        if hour == 7:
            home_palette = random.choice(_HOME_COLOR_PALETTES)
            lines.append(
                f"\n## 指示\nこれは「コーデ選び中」のシーンです。彼女はまだ今日の服を決めていません。"
                f"バッグ・帽子・完成したアクセサリー一式は身につけないこと。下に着ているのはシンプルなラウンジウェア"
                f"(例: a plain tank top and shorts, or a simple slip dress)のみとし、"
                f"両手に持って比較している2着の服を中心に描写すること。"
                f"完成された統一感のあるコーディネートとして描写しては絶対にいけない。\n\n"
                f"⚠️ 配色は必ず「{home_palette}」を基調にすること(ラウンジウェア・比較中の2着とも)。"
                f"セージグリーン・ミントグリーン・薄緑系の色は直近で多用されたため今回は絶対に使用禁止。"
                f"白・アイボリー単体での無地コーデも避けること。\n\n"
                f"⚠️ 出力は必ず**英語の1段落テキストのみ**。日本語・マークダウン・見出し・箇条書き・提案文は絶対禁止。"
            )
        else:
            if not is_home:
                trend = _load_trend_cache()
                if trend:
                    lines.append(f"\n## 東京ファッショントレンド参考資料(衣装・小物選択の参考にすること)\n{trend}")
            wardrobe_history = _load_wardrobe_history()
            history_note = ""
            if wardrobe_history:
                history_note = (
                    f"\n## 直近の衣装履歴(これと異なる色・アイテムを選ぶこと)\n"
                    + "\n".join(f"- {w}" for w in wardrobe_history)
                )
            lines.append(history_note)
            if is_home:
                home_palette = random.choice(_HOME_COLOR_PALETTES)
                lines.append(
                    f"\n## 指示\n彼女は自宅でくつろいでいる最中です。季節({season_en}, {month}月)に合わせた、"
                    f"リラックスできるルームウェア・パジャマ・部屋着(例: soft knit loungewear, cotton pajama set, oversized t-shirt and shorts)を選んでください。"
                    f"バッグ・帽子・アウター・外出用アクセサリーは身につけないこと。"
                    f"足元は素足・室内用ソックス・ルームサンダルのみとし、靴やアウトドア用サンダル(street shoes/sandals)は絶対に描写しないこと。"
                    f"街着・外出着としての完成度の高いコーディネートにはしないこと。\n\n"
                    f"⚠️ 配色は必ず「{home_palette}」を基調にすること。セージグリーン・ミントグリーン・薄緑系の色は直近で多用されたため今回は絶対に使用禁止。"
                    f"白・アイボリー単体での無地コーデも避け、上記の配色を主役にすること。\n\n"
                    f"⚠️ 出力は必ず**英語の1段落テキストのみ**。日本語・マークダウン・見出し・箇条書き・提案文は絶対禁止。"
                )
            else:
                lines.append(
                    f"\n## 指示\n季節({season_en}, {month}月)と天気・気温に合わせた東京の最新ファッショントレンドを反映した衣装を選んでください。"
                    f"上記の衣装履歴と色・アイテムが被らないようにバリエーションを出すこと。\n\n"
                    f"⚠️ 出力は必ず**英語の1段落テキストのみ**。日本語・マークダウン・見出し・箇条書き・提案文は絶対禁止。"
                )
    else:
        news = context.get("news", [])
        featured = _pick_news(news)
        if featured:
            lines.append(f"\n## フィーチャーニュース(背景・衣装のインスピレーション源)\n{featured}")
        lines.append(f"\n## 天気(参考)\n{weather_str}{_weather_warning(w.get('description', ''))}")
        lines.append(f"\n## 季節\n{month}月 / {season_jp}({season_en})")
        lines.append(f"\n## 今回の構図(必ずこれを使う)\n{composition}")
        lines.append(
            f"\n⚠️ 出力は必ず**英語の1段落テキストのみ**。日本語・マークダウン・見出し・箇条書き・提案文は絶対禁止。"
        )
        return "\n".join(lines), is_home, composition, featured

    return "\n".join(lines), is_home, composition, today_event


_SUBJECT_SYSTEM = """\
You are generating the Subject field for a Stable Diffusion / Flux image prompt.
Read the persona definition carefully and output ONE English line describing the character's physical appearance.

Output format (single line, no label prefix):
[nationality/age], [body traits], [hair length], [hair style details], [hair color], [glasses], [expression, gaze, mood]

Rules:
- nationality/age: always "young Japanese woman"
- body traits: translate exactly from persona — do not soften or omit
- glasses: STRICTLY follow the glasses rule in persona — home/indoor → black-frame round glasses, outside → thin silver-frame round glasses
- expression/gaze/mood: infer naturally from the scene location context provided
- Output English only. One line. No explanations, no markdown, no label.
"""


async def _generate_subject_line(persona: str, is_home: bool) -> str:
    """Pass 2: persona.md を読んでSubject行1行を生成する。"""
    location_ctx = "at home / indoors (apartment room)" if is_home else "outside / away from home"
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    try:
        resp = await client.chat.completions.create(
            model=cfg.LLM_MODEL,
            messages=[
                {"role": "system", "content": _SUBJECT_SYSTEM},
                {"role": "user", "content": f"/no_think\n## Persona\n{persona.strip()}\n\n## Scene location context\n{location_ctx}"},
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        result = (resp.choices[0].message.content or "").strip()
        print(f"[prompt] pass2 subject: {result[:80]}...")
        return result
    except Exception as e:
        print(f"[prompt] subject generation failed, using fallback: {e}")
        glasses = "black-frame round glasses" if is_home else "thin silver-frame round glasses"
        return f"young Japanese woman, large breasts, skinny slender build, medium-long warm brown hair, {glasses}"


_FORMAT_SYSTEM = """\
You are a Stable Diffusion / Flux image prompt formatter.
Reformat the given scene description into this exact 7-line structure.
Output English only. Each line starts with the label. No explanations, no extra lines.

Image Type: [photo style — e.g. photorealistic editorial photography, cinematic lighting]
Scene: [specific location with key props; end with "avoid X" for unwanted elements]
Subject: [FIXED_SUBJECT_BASE], [expression, gaze, mood matching the scene]
Wardrobe: [each item as: color + material + shape/silhouette; include accessories]
Pose and Composition: [shot type, body position, hand placement, camera angle]
Lighting: [light source direction + time of day + color temperature in K + shadow quality + any bokeh/effects]
Camera Look: [lens mm, depth of field, film grain, color grading style]

Rules:
- Replace vague adjectives (beautiful/nice/lovely) with concrete physical descriptions
- Scene: always include "avoid ~" for background elements to exclude
- Subject: copy the FIXED SUBJECT BASE exactly as provided, then append expression/gaze/mood at the end — do NOT modify the fixed part
- Wardrobe: color + material + shape for EVERY garment and accessory. Only include footwear (shoes/sandals/boots) if Pose and Composition is a full body shot — omit footwear for thigh-up or bust-up shots
- Lighting: must include color temperature as K value (e.g. 3500K, 5500K)
- Camera Look: must include film grain and color grading
- Keep ALL specific details from the original (location, season, lighting condition, etc.)
"""


async def _reformat_image_prompt(raw: str, subject: str) -> str:
    """Pass 3: pass1シーン段落 + pass2 Subject行 → 7セクション構造化。失敗時は raw をそのまま返す。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    try:
        user_content = (
            f"/no_think\n"
            f"SUBJECT LINE (use this exactly for the Subject field — do not modify it):\n"
            f"{subject}\n\n"
            f"Scene description to reformat:\n{raw}"
        )
        resp = await client.chat.completions.create(
            model=cfg.LLM_MODEL,
            messages=[
                {"role": "system", "content": _FORMAT_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        result = (resp.choices[0].message.content or "").strip()
        return result if result else raw
    except Exception as e:
        print(f"[prompt] reformat failed, using raw: {e}")
        return raw


_KREA2_EXPAND_SYSTEM = """\
You are a minimal touch-up editor for a keyframe photo-caption prompt fed to the
Krea 2 Turbo image model. The input is a short natural photo caption (~40-80 words).
Preserve it as a single flowing caption — do not add labels, line breaks, or bullet
points. Do NOT change any scene fact: location, wardrobe items, props, pose, character
identity, or lighting condition. This is a light touch-up, NOT a rewrite — your edit
budget is small (add or change roughly 10-20% of the words, never more).

Apply ONLY the rules below, and ONLY when their trigger condition is already true in
the input. Never invent a condition that isn't there.

1. FULL-BODY REINFORCEMENT — trigger: the caption already describes a full-body/
   full-length shot AND has more than a brief face/hair/eye/lip description (this
   model over-weights face detail, cropping to upper body despite full-body intent).
   Action: add 1 short reinforcing phrase (pick one: "full-body shot", "full-length
   portrait", "head-to-toe visible", "both feet visible", "camera pulled back enough",
   "no crop"). Skip if equivalent wording already exists, or if the shot isn't full-body.

2. POSITIVE BODY-DESCRIPTOR REPHRASING — trigger: the caption uses body-type words like
   "very thin", "minimal", "petite", "slender" describing the subject's build (this
   model over-interprets these toward "small/frail"). Action: swap in 1 positive-framed
   equivalent (pick one: "slim but healthy", "softly curvy", "naturally balanced
   proportions", "not underweight", "realistic adult body proportions") without changing
   the character's actual body type register (still slim, just not frail).

3. LIGHT-FABRIC INTERACTION — trigger: the caption already names BOTH a lighting
   condition/source (sun, window, backlight, lamp, candlelight, etc.) AND a garment
   fabric. Action: add a short clause naming how that light physically interacts with
   that fabric (transparency, sheen, shadow-printing) — e.g. "sheer fabric glowing as
   backlight soaks through" / "satin catching a sharp highlight". Do NOT invent a light
   source or fabric that isn't already mentioned.

Output ONLY the edited caption text. No explanations, no preamble, no markdown.
"""


async def expand_krea2_prompt(prompt: str) -> str:
    """Krea2専用の軽量プロンプト補正パス(2026-07-13追加)。docs/Krea2.mdの2件のXポスト
    (設計思想: 顔説明過多で上半身クロップに寄る/体型語の過剰解釈、ライティングサンプル:
    光の衣装素材への作用を明示すると効果的)から抽出した3ルールを、条件が既に成立している
    場合のみ軽く適用する。i2v_timeline_cliV6の40-80語キーフレームキャプション専用
    (i2v_timeline_cliV6._format_keyframe_prompt()の末尾から、KEYFRAME_WORKFLOW_JSONが
    Krea2系の時のみ呼ばれる)。第14/16ラウンド(CLAUDE.md)で40-80語キーフレームの肥大化が
    実際のアーティファクト・顔2つ等の破綻を引き起こした前例があるため、_reformat_image_prompt
    と異なりここでは出力語数を入力語数比でガードし、失敗時・ガード抵触時はrawを返す(fail-open)。"""
    raw = (prompt or "").strip()
    if not raw:
        return prompt
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    try:
        resp = await client.chat.completions.create(
            model=cfg.LLM_MODEL,
            messages=[
                {"role": "system", "content": _KREA2_EXPAND_SYSTEM},
                {"role": "user", "content": f"/no_think\n{raw}"},
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        result = (resp.choices[0].message.content or "").strip()
        if not result:
            return prompt

        in_words = len(raw.split())
        out_words = len(result.split())
        max_words = max(in_words * 1.25, in_words + 20)
        if out_words > max_words or out_words < in_words * 0.9:
            print(f"[prompt] krea2 expand rejected (length guard {in_words}→{out_words} words), using original")
            return prompt
        return result
    except Exception as e:
        print(f"[prompt] krea2 expand failed, using original: {e}")
        return prompt


async def generate_image_prompt(context: dict, persona: str, context_mode: str, hour_jst: int) -> tuple[str, str, bool]:
    """詳細英語画像プロンプトを生成して (prompt, featured_news, is_home) を返す(3パス)。
    Pass1: シーン・衣装・ライティング生成(人物外見なし)
    Pass2: persona.md からSubject行生成(ロケーション渡してメガネ判定)
    Pass3: Pass1+Pass2を7セクションにフォーマット
    """
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    user_msg, is_home, composition, featured_news = _build_user_prompt(persona, context, context_mode, hour_jst)
    if context_mode == "weather":
        system = _IMAGE_SYSTEM_EVENT if featured_news else _IMAGE_SYSTEM_WEATHER
    elif context_mode == "bikini":
        system = _IMAGE_SYSTEM_BIKINI
    else:
        system = _IMAGE_SYSTEM_NEWS

    # Pass 1: シーン生成
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.95,
        max_tokens=4096,
    )
    raw = (resp.choices[0].message.content or "").strip()
    prefix = cfg.IMAGE_PROMPT_PREFIX.strip()
    pass1 = f"{prefix}, {raw}" if prefix and raw else raw or prefix

    # newsモードは pass1 テキストからロケーション判定
    if context_mode != "weather":
        home_kws = ("apartment room", "at home", "home interior", "her room",
                    "bedroom", "living room", "cozy apartment", "her apartment")
        is_home = any(kw in raw.lower() for kw in home_kws)
    print(f"[prompt] pass1 done, is_home={is_home}")

    # Pass 2: persona から Subject 行生成
    subject = await _generate_subject_line(persona, is_home)

    # Pass 3: 7セクションフォーマット
    result = await _reformat_image_prompt(pass1, subject)

    # full body shot 以外は Wardrobe から靴・サンダル系をコードで削除
    if "full body" not in composition.lower():
        result = _remove_items_from_wardrobe(result, _FOOTWEAR_KEYWORDS)
        print(f"[prompt] footwear removed (composition: {composition[:40]})")

    # 自宅シーンは構図に関わらずバッグ・屋外用フットウェアをコードで削除(LLMの指示無視への保険)
    if is_home:
        result = _remove_items_from_wardrobe(result, _FOOTWEAR_KEYWORDS + _BAG_KEYWORDS)
        print("[prompt] home scene: bag/outdoor footwear removed")

    # 衣装履歴に保存(7時のコーデ選び中シーンは未決定の服、bikiniは日常コーデと別カテゴリなので対象外)
    if not (context_mode == "bikini" or (context_mode == "weather" and hour_jst == 7)):
        wardrobe = _extract_wardrobe(result)
        if wardrobe:
            _save_wardrobe_history(wardrobe)
            print(f"[prompt] wardrobe saved: {wardrobe[:60]}...")

    return result, featured_news, is_home


_VIDEO_MOTION_SYSTEM = """\
You are writing the motion description for an image-to-video model.
The first frame is already fixed by the provided image description.
Output ONLY one English sentence describing a single natural, casual action the character performs.

Rules:
- Do NOT re-describe the scene, background, outfit, or appearance.
- 1 simple continuous action, natural and casual (not dramatic). ~10 seconds of motion.
- Example: "She slowly lifts her coffee cup and takes a gentle sip, then glances at the camera with a soft smile."
- Output: one English sentence only. No explanations, no markdown, no dialogue.
"""

_VIDEO_MOTION_SYSTEM_S2V = """\
You are writing the motion description for a speech-to-video lip-sync model.
The first frame is already fixed by the provided image description. The character's spoken dialogue is provided separately as audio, so do NOT include any actual words.
Output ONLY one English sentence describing a single natural, casual action the character performs WHILE she is speaking Japanese — the sentence must explicitly state that she is speaking Japanese as part of the action.

Rules:
- Do NOT re-describe the scene, background, outfit, or appearance.
- Do NOT quote or paraphrase the dialogue content itself.
- 1 simple continuous action, natural and casual (not dramatic). ~10 seconds of motion.
- The sentence must read like: "<action>, speaking Japanese as <continuing context>."
- Example: "She stands in her closet, holding two outfits and speaking Japanese as she hesitates over what to wear."
- Output: one English sentence only. No explanations, no markdown, no dialogue.
"""

_VIDEO_DIALOGUE_SYSTEM = """\
You are writing a single line of spoken dialogue in Japanese for a character named みー.
Read her persona carefully and write ONE line she would say in this scene, in her natural voice.

Rules:
- Strictly follow her speaking style from the persona (tone, vocabulary, sentence-ending particles, catchphrases).
- 2–3 sentences, 30–50 Japanese characters total, in 「」quotes.
- The dialogue must be about her SITUATION (what she is doing/deciding, where she is, mood, what she sees/feels) — like something she'd actually say to a friend right now.
- NEVER narrate her own physical body movement as if describing camera direction (e.g. do NOT say "頭傾けてみようかな", "ちょっと動いてみる"). This is strictly forbidden.
- DO talk about the situational content of the scene if relevant (e.g. if she's comparing two outfits, she CAN say something like "どっちにしようかなー、迷うなー" — this is about her decision/situation, not a forbidden motion narration).
- Use "ー" instead of "〜" for elongated vowel sounds (this is text-to-speech input; "〜" is read awkwardly by the TTS engine).
- Output: 「Japanese dialogue」 only. No explanations, no other text.
"""

_VIDEO_DIALOGUE_SYSTEM_S2V = """\
You are writing spoken dialogue in Japanese for a character named みー, to be turned into speech audio and lip-synced onto her face.
Read her persona carefully and write what she would say in this scene, in her natural voice.

Rules:
- Strictly follow her speaking style from the persona (tone, vocabulary, sentence-ending particles, catchphrases).
- 3–4 sentences, roughly 60–90 Japanese characters total (about 12–16 seconds of speech), in 「」quotes.
- The dialogue must be about her SITUATION (what she is doing/deciding, where she is, mood, what she sees/feels) — like something she'd actually say to a friend right now.
- NEVER narrate her own physical body movement as if describing camera direction (e.g. do NOT say "頭傾けてみようかな", "ちょっと動いてみる"). This is strictly forbidden.
- DO talk about the situational content of the scene if relevant (e.g. if she's comparing two outfits, she CAN say something like "どっちにしようかなー、迷うなー" — this is about her decision/situation, not a forbidden motion narration).
- Use "ー" instead of "〜" for elongated vowel sounds (this is text-to-speech input; "〜" is read awkwardly by the TTS engine).
- Output: 「Japanese dialogue」 only. No explanations, no other text.
"""


async def generate_video_prompt(context: dict, persona: str, context_mode: str, hour_jst: int) -> str:
    """日本語ショートシーンプロンプトを生成して返す(コンテキストベース・未使用)。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    user_msg, _ = _build_user_prompt(persona, context, context_mode, hour_jst)
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": _VIDEO_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.95,
        max_tokens=4096,
    )
    return (resp.choices[0].message.content or "").strip()


_CAPTION_SYSTEM = """\
/no_think
You are writing a social media caption in Japanese for a character named みー.
Given the scene description (from an image or video prompt) and みー's persona, write a short natural caption in HER voice.

The caption must convey:
- What she is doing (action/situation)
- Where she is (location/setting)
- Her mood, feeling, or reaction — in her natural casual tone

Rules:
- Write in みー's own voice, first person, natural and cute Japanese. Match her speaking style from the persona.
- Length: 1–3 short sentences. Not too formal, not too long.
- NO hashtags.
- Output ONLY the caption text in Japanese. No explanations, no markdown.
"""


_TREND_MENTION_NOTE = (
    "\n\n## 追加指示\nこれは2026年の水着トレンドから選んだ水着です。"
    "「2026年トレンドから選んだ」という言及に加えて、Wardrobe に書かれている水着の柄・色・スタイルの特徴(例: ゼブラ柄、カットアウト、バターイエロー等)も具体的に一言触れること。"
)

_BIKINI_TIMELESS_NOTE = (
    "\n\n## 追加指示(重要)\nこのシーンは特定の時間帯を表していません(シーン説明に「golden hour」等の光の描写があっても、それは実際の時刻を意味しません)。"
    "「おはよう」「こんにちは」「こんばんは」「おやすみ」「お疲れ様」等、時間帯を想起させる挨拶・言葉は絶対に使わないこと。"
)

_HOME_LOCATION_NOTE = (
    "\n\n## 追加指示(重要)\nこのシーンの場所は自宅(her own apartment room)です。"
    "「カフェ」「お店」「外」等、自宅以外への外出を示唆する単語や表現は絶対に使わないこと。"
    "自宅の居心地よい雰囲気は「お部屋」「ソファ」「お気に入りの場所」等、自宅であることが明確な言葉で表現すること。"
)


_TIME_GREETING_NOTE_TMPL = (
    "\n\n## 現在時刻(重要)\n現在は{label}({hour}時)です。"
    "挨拶や時間に関する言葉(おはよう/こんにちは/こんばんは/おやすみ等)を使う場合は、必ずこの時刻に合ったものにすること。"
    "時刻と矛盾する挨拶(例: 朝なのに「こんばんは」)は絶対禁止。挨拶を使わなくても問題ない。"
)


def _time_greeting_note(hour_jst: int) -> str:
    label, _ = _scene_hint(hour_jst)
    return _TIME_GREETING_NOTE_TMPL.format(label=label, hour=hour_jst)


async def generate_caption(image_prompt: str, persona: str, context_mode: str = "", is_home: bool = False, hour_jst: int = -1) -> tuple[str, str]:
    """シーンからみーのコメントを生成し (x_caption, tiktok_caption) を返す。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    user_msg = (
        f"/no_think\n"
        f"## みーのペルソナ\n{persona.strip()}\n\n"
        f"## シーン説明(英語)\n{image_prompt}"
    )
    if context_mode == "bikini":
        user_msg += _TREND_MENTION_NOTE
        user_msg += _BIKINI_TIMELESS_NOTE
    if is_home:
        user_msg += _HOME_LOCATION_NOTE
    # bikiniは天気・現在時刻を無視して常に明るいリゾート光で生成する仕様のため、実時刻の挨拶指示は入れない
    if hour_jst >= 0 and context_mode != "bikini":
        user_msg += _time_greeting_note(hour_jst)
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": _CAPTION_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.9,
        max_tokens=4096,
    )
    comment = (resp.choices[0].message.content or "").strip()
    x_caption = f"みー {comment} by Z-Image 📷"
    tiktok_caption = f"みー {comment} by LTX-2.3 🎬"
    return x_caption, tiktok_caption


async def _generate_motion_and_dialogue(
    image_prompt: str,
    persona: str,
    context_mode: str = "",
    is_home: bool = False,
    hour_jst: int = -1,
    for_s2v: bool = False,
) -> tuple[str, str]:
    """画像プロンプトから動き(英語)+セリフ(日本語)を生成する(2パス)。
    Pass1: シーンから動き(英語)のみ生成
    Pass2: persona.md からみーのセリフ(日本語)生成
    for_s2v=True の場合、Pass1は「〜しながら日本語で喋っている」を1文に自然に組み込む(S2V用)。
    """
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    frame_ctx = f"## 1stフレームの画像説明(英語)\n{image_prompt}"

    # Pass 1: 動き生成
    resp1 = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": _VIDEO_MOTION_SYSTEM_S2V if for_s2v else _VIDEO_MOTION_SYSTEM},
            {"role": "user", "content": f"/no_think\n{frame_ctx}"},
        ],
        temperature=0.95,
        max_tokens=4096,
    )
    msg1 = resp1.choices[0].message
    motion = (msg1.content or "").strip()
    if not motion:
        reasoning = getattr(msg1, "reasoning_content", "") or ""
        lines = [l.strip() for l in reasoning.strip().splitlines() if l.strip()]
        motion = lines[-1] if lines else ""
    print(f"[video] pass1 motion: {motion[:80]}...")

    # Pass 2: みーのセリフ生成(persona参照)
    dialogue_user_msg = (
        f"/no_think\n"
        f"## みーのペルソナ\n{persona.strip()}\n\n"
        f"{frame_ctx}\n\n"
        f"## みーの動作\n{motion}"
    )
    if context_mode == "bikini":
        dialogue_user_msg += _TREND_MENTION_NOTE
        dialogue_user_msg += _BIKINI_TIMELESS_NOTE
    if is_home:
        dialogue_user_msg += _HOME_LOCATION_NOTE
    # bikiniは天気・現在時刻を無視して常に明るいリゾート光で生成する仕様のため、実時刻の挨拶指示は入れない
    if hour_jst >= 0 and context_mode != "bikini":
        dialogue_user_msg += _time_greeting_note(hour_jst)
    resp2 = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": _VIDEO_DIALOGUE_SYSTEM_S2V if for_s2v else _VIDEO_DIALOGUE_SYSTEM},
            {"role": "user", "content": dialogue_user_msg},
        ],
        temperature=0.95,
        max_tokens=4096,
    )
    dialogue = (resp2.choices[0].message.content or "").strip()
    # LLMへの指示だけでは「〜」が混入することがあるため、コード側でも確実に置換する(TTSが不自然に読むため)
    dialogue = dialogue.replace("〜", "ー")
    print(f"[video] pass2 dialogue: {dialogue}")

    return motion, dialogue


async def generate_video_prompt_from_image(image_prompt: str, persona: str, context_mode: str = "", is_home: bool = False, hour_jst: int = -1) -> str:
    """画像プロンプトから動き+セリフを結合した動画プロンプトを生成する(I2V用、旧video.json向け)。"""
    motion, dialogue = await _generate_motion_and_dialogue(image_prompt, persona, context_mode, is_home, hour_jst, for_s2v=False)
    return f"{motion} {dialogue}" if dialogue else motion


async def generate_video_prompt_from_image_v2(image_prompt: str, persona: str, context_mode: str = "", is_home: bool = False, hour_jst: int = -1) -> str:
    """画像プロンプトから動き+セリフを結合した動画プロンプトを生成する(2026版I2Vワークフロー向け)。

    LTXが自前で音声生成するため、セリフ本文をプロンプトに含める必要がある。
    workflows/2026_ltx2_3_i2v.json の input1 サンプル値は "{motion文}. She speaks in Japanese.「{dialogue}」"
    だが、これは動作が終わった後に喋るという逐次的な構造に読めるため採用しない。
    動作とセリフが同時に起きていることを明示するため、motion文末の句点を外し
    ", speaking Japanese:「{dialogue}」" を1文として繋げる形にする
    (旧video.json向けの generate_video_prompt_from_image やS2V向けの
    generate_motion_and_dialogue とは結合形式のみ異なる)。
    """
    motion, dialogue = await _generate_motion_and_dialogue(image_prompt, persona, context_mode, is_home, hour_jst, for_s2v=False)
    if not dialogue:
        return motion
    motion_stripped = motion.rstrip()
    if motion_stripped.endswith("."):
        motion_stripped = motion_stripped[:-1]
    return f"{motion_stripped}, speaking Japanese:{dialogue}"


async def generate_motion_and_dialogue(image_prompt: str, persona: str, context_mode: str = "", is_home: bool = False, hour_jst: int = -1) -> tuple[str, str]:
    """画像プロンプトから動き(英語、speaking Japanese込み)とセリフ(日本語)を分離して返す(S2V用)。"""
    return await _generate_motion_and_dialogue(image_prompt, persona, context_mode, is_home, hour_jst, for_s2v=True)
