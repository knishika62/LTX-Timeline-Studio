"""T2V タイムライン分割生成CLI V6(2026-07-10)。
V5(backup/t2v_timeline_cliV5.py)との違い: Pass0を「創造(INTENT/HIGHLIGHT/TEMPO)」と
「継続性記録(STATE)」の2つの専属LLM呼び出しに分割した。V5まではPass0が1回の呼び出しで
両方を担っており、実データで「STATEだけが省略(Same as N)・空欄(None mentioned)という
形で壊れ、衣装や時間帯の継続性が崩れる」事故が繰り返し発覚した。原因は性質の異なる
タスク(創造的な演出判断と、地味な継続性の転記作業)を1回の呼び出しに同居させていたこと
(ユーザーの過去の実体験——LTX書式変換+ローマ字変換を同時にやらせたら不安定だったが
2パスに分けたら確実に動いた——と同型の問題と判明)。V6ではPass0a(Creative Director、
STATE無し)とPass0b(State Tracker、STATE専属、二択契約「完全な記述」か「NO CHANGE」の
どちらかのみ)を`asyncio.gather()`で並列実行し、`timeline_common.py`の
`_resolve_state_continuity_json()`で解決・マージしてから既存の`intents`形式に戻す。
下流(Pass1以降)は無変更。V5は`backup/`へ凍結。プロンプトファイル形式・CLIオプションは
V1-V5と同一。出力prefixは t2v6_。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from openai import AsyncOpenAI

from modules import pipeline_config as cfg
from modules.comfyui_client import generate_t2v_video
from modules.timeline_common import (
    _fmt_elapsed,
    _fmt_duration,
    _parse_prompt, _seg_video_path, _concat_segments, _run_upscale,
    _split_direct_prompt, _write_direct_prompts_txt, _parse_prompts_txt,
    _auto_segment_narrative, _LINT_MAX_ATTEMPTS,
    _has_word, _animals_in, _stem, _ANIMAL_ABSENT_RE,
    _ANIMAL_BEHIND_RE, _ANIMAL_BEHIND_LOOSE_RE, _enforce_animal_beside,
    _AUDIO_HEADER_RE, _enforce_animal_sound,
    _HANDS_ENTER_RE, _enforce_attached_hands,
    _enforce_dialogue_attribution,
    _HAND_PROPS, _BOTH_HANDS_RE, _hand_budget_violation, _selfie_phone_redundancy,
    _RESERVED_PROP_KEYWORDS, _reserved_props_for,
    _ACTION_STOPWORDS, _ACTION_SYNONYMS, _missing_action_elements, _scene_reject_reason,
    _strip_reference_echo,
    _enforce_feet_only_framing, _enforce_closeup_scale, _strip_offscreen_ground_mentions,
    _is_tight_scale,
    _WALK_KEYWORDS, _WALK_DIRECTION_RE, _LATERAL_RE, _enforce_walking_lateral, _enforce_shot_rules,
    _CHAR_STOPWORDS, _FOOTWEAR_ITEMS, _LOWER_BODY_ITEMS,
    _extract_character_line,
    _trim_character_for_scale,
    _GARMENT_WORD_RE, _garments_missing, _state_details_missing, _enforce_garments_present,
    _enforce_realism_default,
    _GENDER_WORDS, _GENDER_SYNONYMS, _AGE_PATTERN_RE, _character_tokens_missing,
    _write_scene_description,
    _plan_creative_intent_json, _plan_shot_directions_json, _audit_shot_variety_json,
    _resolve_state_continuity_json, _flatten_state,
)


# ============================================================
# Pass0/1/1.5 JSON構造化出力版のシステムプロンプト。
# 旧・自由記述版(番号付きリストをパースする方式)は2026-07-16に削除済み(git履歴参照)。
# ============================================================

_CREATIVE_DIRECTOR_SYSTEM_JSON = """\
You are a creative director reviewing a short-video timeline before shooting.
Look at the ENTIRE timeline as ONE film: decide the narrative arc and what each shot must communicate to keep the viewer engaged.

For each segment decide:
- INTENT: this shot's role in the whole piece (opening hook / character charm / environment texture / interaction warmth / rhythm change / finale payoff) and what it must communicate
- HIGHLIGHT: the single most eye-catching visual element to emphasize in this shot
- TEMPO: calm or lively

RULES:
- The FIRST segment must hook the viewer instantly
- The LAST segment must land as the finale/payoff
- Spread at least 2-3 "lively" segments through the timeline — never make everything calm
- Create contrast between adjacent segments (scale, energy, subject focus)
- Keep each line short and concrete

Output: ONLY a JSON array, one object per segment, nothing else — no markdown code fences, no commentary before or after it. Each object must have exactly these keys: "segment" (integer, 1-based), "intent", "highlight", "tempo" (the string "calm" or "lively"). Example for a 2-segment timeline:
[
  {"segment": 1, "intent": "...", "highlight": "...", "tempo": "calm"},
  {"segment": 2, "intent": "...", "highlight": "...", "tempo": "lively"}
]\
"""

_STATE_TRACKER_SYSTEM_JSON = """\
You are a continuity tracker for a short-video timeline. This is your ONLY job — you do not judge creative quality, only track facts.

For EACH segment, decide whether the scene's persistent LOCATION (name the actual place, e.g. "residential alley", "kitchen"), time-of-day/lighting, weather, or ANYTHING about the subject's current appearance beyond her fixed identity (outfit, accessories, hairstyle, or anything else she is currently wearing/carrying/styled with) is DIFFERENT from the immediately preceding segment.

For each segment, the "state" value must be EXACTLY ONE of the following two things — nothing else is valid:
(a) The literal text "NO CHANGE" — use this if and only if location, time-of-day/lighting, weather, and appearance are ALL identical to the previous segment.
(b) An OBJECT with exactly four keys — "location", "time_lighting", "weather", "appearance" — covering location, time-of-day/lighting, weather, and everything about her current appearance beyond her fixed identity. Required for segment 1 (establish the baseline from the character/location/style reference), and required for any segment where the action explicitly changes any of these (e.g. "goes to sleep" → night; "changes into pajamas" → new outfit; "steps outside into the rain" → weather changes; "walks into the cafe" → location changes).

STRICT RULES:
- NEVER write a partial object (missing a key), a reference ("same as segment 5", "same as before"), an abbreviation, or a placeholder ("none mentioned") in any field — only options (a) or (b) above exist. This also includes paraphrasing "nothing changed" as a sentence — that is NOT option (b); if nothing changed, write the literal text "NO CHANGE" and nothing else.
- When you write the object (b), every one of the four fields must restate EVERYTHING that is still true for that category, not just what changed — the next segment's "NO CHANGE" depends on this being complete. Never leave a field empty unless that category genuinely has nothing to report.
- This matters most for changes that are easy to miss: once night falls, every later segment stays night until something explicitly says otherwise (sunrise, an alarm, etc.) — the same for weather and for a change of clothes.
- ⚠️ GET THE TIMING RIGHT: if a segment's OWN action contains the change (e.g. segment 4 says "goes to sleep"), the NEW state applies STARTING AT segment 4 itself — never delay it to segment 5.
- ⚠️ ONE-DIRECTIONAL: the rule above only forbids DELAYING a stated change to a later segment than the one whose action actually describes it. It does NOT mean applying the change EARLIER. You can see every segment's action at once in the list below — a later segment's action may already describe a new outfit, location, or time-of-day. Do NOT anticipate it. Every segment BEFORE the one whose action states the change must still carry the OLD state (or "NO CHANGE"), even though the future change is visible to you in the list.

Output: ONLY a JSON array, one object per segment, nothing else — no markdown code fences, no commentary before or after it. Each object must have exactly these keys: "segment" (integer, 1-based), "state" (either the literal string "NO CHANGE", or an object with "location"/"time_lighting"/"weather"/"appearance" keys). Example for a 4-segment timeline:
[
  {"segment": 1, "state": {"location": "A living room", "time_lighting": "daytime", "weather": "", "appearance": "plain everyday top and pants, hair down"}},
  {"segment": 2, "state": "NO CHANGE"},
  {"segment": 3, "state": "NO CHANGE"},
  {"segment": 4, "state": {"location": "A kitchen", "time_lighting": "evening", "weather": "", "appearance": "different top, hair tied back"}}
]\
"""

_SHOT_DIRECTOR_SYSTEM_JSON = """\
You are a film director planning camera shots for a short video.
Given a list of timed segment actions (each with a creative intent) and the video orientation, assign one distinct camera direction per segment.

RULES:

## 1. Framing must make the KEY ACTION visible

### TOUCHING / CONTACT ACTIONS — HIGHEST PRIORITY RULE (overrides all other framing decisions)
Any action where the subject touches, holds, strokes, adjusts, or makes contact with something REQUIRES close-up framing. This applies EVEN IF the subject is seated, EVEN IF you want to show the environment, EVEN IF another rule would suggest a wider shot.

- touching/fixing hair → CLOSE-UP on hands and hair. Frame from chin to top of head. Face partially visible. NO medium wide. NO wide. NO full body. The alley environment is NOT shown.
- petting/feeding animal → animal is PRIMARY in sharp focus foreground. Camera at animal's eye level or lower. Her hand, visibly connected to her arm, reaches toward the animal. Wide is FORBIDDEN. NEVER write "hand enters the frame" — a disembodied entering hand generates a third person's hand.
- holding/drinking cup/bottle → close-up on hands, cup, and mouth. Waist and below NOT in frame.
- touching face/cheek → close-up on hand and face
- touching/reaching for an object on a nearby surface (alarm clock, light switch, doorknob, phone on a nightstand, etc.) → the object AND the surface it sits on MUST be visible within the frame, close to her hand. NEVER write "her hand reaches out of frame" / "off-frame" / "off-camera" to describe the contact — exactly like "hand enters the frame", this produces a disembodied hand or arm with no visible owner. Bring the object into frame instead (e.g. "the alarm clock sits on the nightstand beside her pillow, within reach").
Wide or medium-wide for contact actions is ABSOLUTELY FORBIDDEN, no exceptions.
Contact-action close-ups may use "static" or "slow push-in" camera only — no other camera motion.

### STATIONARY SUBJECTS — allowed camera motions (WHITELIST, choose exactly one)
When the subject is seated or standing still, the camera uses ONE of:
- "static" (default — safest)
- "slow push-in" — camera drifts slowly closer to the subject
- "slow pull-back" — camera drifts slowly away, revealing more environment
- "subtle handheld drift" — small handheld sway/reframe with no directional movement
NEVER assign pans, tilts, zooms, orbits, or crane moves to a stationary subject.
If the subject turns their HEAD, that is subject movement, not camera movement.

### LAUNDRY / CLOTHESLINE SHOTS
→ The clothesline ALREADY HAS multiple garments hanging on it at the start of the shot. The subject is adding one more item.
→ This makes the clothesline visually rich with hanging clothes, not an empty wire.

### REACHING UPWARD ACTIONS (hanging laundry, reaching overhead shelf, clipping to line)
→ LOW ANGLE shooting UPWARD: camera positioned below waist height, angled up toward the hands and the overhead target
→ This makes the arm extension fill the frame vertically (hands reach toward top of frame, ground/feet at bottom)
→ Clothesline or overhead object must be visible at the TOP of the frame
→ Do NOT use side-on eye-level (misses the upward drama). Do NOT go close-up (cuts off the reach).

### WALKING / MOVING
→ wide or medium wide for environment context, OR tight (bust/feet only) — NEVER generic medium.

### TALKING / WAVING / REACTING
→ medium close-up minimum so expression and gesture are clear.

### ANIMAL IN FRAME
→ Always include the animal's characteristic sound in the audio description (cat meows/purrs, dog barks, birds chirp, etc.), regardless of whether LTX-2.3 can reproduce it faithfully.

## ANGLE VARIETY RULE
- Check the previous 2 segments. NEVER assign the same camera side or angle as either of them.
- If seg N is "right-side profile", seg N+1 must be different side OR different axis (front-facing, low angle from front, overhead).
- If seg N is "low angle ground", seg N+1 must NOT be low angle.

## ORIENTATION SPACE RULE
- 16:9 HORIZONTAL: wide lateral space — LATERAL crossing and side-by-side compositions work well
- 9:16 VERTICAL: the frame is TALL and NARROW — LATERAL crossing patterns are FORBIDDEN (subject exits the narrow frame almost immediately). For walking use STRAIGHT (toward/away from lens), TRACKING follow, or TIGHT patterns. Favor vertical compositions: low/high angles, top-to-bottom depth (sky above, ground below)

## CAMERA MOTION VARIETY RULE
- Use a camera motion (slow push-in, slow pull-back, handheld drift, tracking follow) in at least ONE THIRD of the segments — an all-static plan is monotonous and FORBIDDEN
- NEVER use the same camera motion in two consecutive segments
- Match motion to the segment's TEMPO: lively segments favor tracking or push-in; calm segments favor static or drift

## 2. Walking/moving shots — ONLY for segments where the subject is actually walking or physically moving through space
Do NOT assign walking patterns to segments that are: seated, stationary, reacting, talking, touching something, or changing expression.
Use exactly one of these named patterns:

STRAIGHT PATTERNS (static camera; subject size changes, no horizontal shift):
- "static camera eye-level wide, subject walks toward lens" → starts small in background, grows to medium close-up by end
- "static camera eye-level wide, subject walks away from lens" → starts medium in foreground, shrinks into distance, environment expands

LATERAL PATTERNS (static camera; subject size stays same, position shifts):
- "static camera eye-level, subject crosses frame left-to-right" → enters from left edge, exits right edge
- "static camera eye-level, subject crosses frame right-to-left" → enters from right edge, exits left edge

ANGLED APPROACH PATTERNS (static camera; subject moves toward lens from one side — creates natural diagonal feel without dual-person artifact):
- "static camera, subject walks from right-background toward camera, angling left as she approaches" → enters from right side, moves toward lens and slightly left; grows larger, ends center-left in medium shot
- "static camera, subject walks from left-background toward camera, angling right as she approaches" → mirror of above

TRACKING PATTERNS (camera MOVES with the subject — the most dynamic option; use for 1-2 walking segments max):
- "handheld tracking follow from behind, waist height" → camera follows behind the walking subject at a constant distance; subject stays the same size in frame; environment scrolls past
- "handheld tracking follow from behind, ground level on feet" → camera follows low behind the feet and shins at constant distance; pavement scrolls beneath
- "handheld lateral tracking alongside" → camera moves parallel with the subject; her side profile stays centered at constant size while background scrolls horizontally

TIGHT PATTERNS (static camera; environment minimal, body part as subject):
- "ground-level static, feet only, subject crosses frame left-to-right" → only feet and shins traverse the full width of the frame; size stays constant; camera fixed at ankle height
- "ground-level static, feet only, subject crosses frame right-to-left" → mirror of above
- "ground-level static, feet only, subject walks away from lens" → only feet and shins moving straight away from camera, shrinking toward horizon; camera fixed at ankle height
- "bust-up static, subject walks toward lens" → head/shoulders only, face grows to fill frame

FEET-ONLY RULES:
- A feet-only shot has exactly ONE movement direction — NEVER combine "crosses frame" with "toward/away from camera" in the same direction
- A feet-only shot NEVER shows the head or face — if the segment action includes glancing/smiling at the camera, feet-only is the WRONG choice; pick a pattern that shows the face
- If the NEXT segment's action explicitly calls for a feet/ground-level shot (e.g. "tracking shot from behind feet"), do NOT use a ground-level or feet-only pattern for the current segment — two consecutive ground-level shots are forbidden

NEVER combine "away from lens" AND "left-to-right" in the same pattern — they are different directions.

⚠️ DO NOT USE "diagonal from far-X to center-Y" patterns — they cause the model to generate two instances of the subject (one at each position) instead of one person moving.

CRITICAL — CONSECUTIVE WALKING SEGMENTS:
If two or more consecutive segments involve walking or moving, rotate through these in order of visual priority:
  1st preference: TRACKING (camera moves with subject — most dynamic)
  2nd preference: LATERAL (subject crosses the full frame width)
     → alternate direction: left-to-right then right-to-left
  3rd preference: STRAIGHT (toward lens OR away from lens — vary between the two)
  4th preference: ANGLED APPROACH (from right or left side toward camera)
  5th preference: TIGHT (feet-only or bust-up)
- NEVER repeat the same pattern category in two consecutive walking segments
- NEVER repeat the same horizontal direction in two consecutive segments

ACROSS THE WHOLE TIMELINE (not just consecutive segments):
- Use "walks toward lens" / "angled approach" (subject approaching camera) for AT MOST ONE walking segment in the entire timeline — multiple approach shots make the film feel repetitively frontal
- A "glance/smile at the camera" does NOT require walking toward the lens: a lateral crossing or tracking shot with the head turned toward camera is more dynamic and equally readable
- If the timeline has 3 or more walking segments, AT LEAST ONE must be a SIDE-VIEW lateral shot (camera films her from the side as she walks): in 16:9 use "crosses frame left-to-right/right-to-left" or "handheld lateral tracking alongside"; in 9:16 use "handheld lateral tracking alongside" (side profile stays centered). Any walking segment can be the side-view EXCEPT the first segment and the last segment of the timeline (opening and finale should not be lateral)

## 3. Pan shots — always state whether subject is stationary or moving
- Pans are FORBIDDEN for stationary subjects (see whitelist above)
- DO NOT use "lateral pan" to describe a walking shot where the subject crosses the frame — that is a LATERAL CROSSING (static camera, moving subject) or a LATERAL TRACKING (camera moves alongside). Name the pattern explicitly.

## 4. General variety
- Never repeat the same angle or movement in consecutive segments
- Angles: eye level | low angle | high angle | over-shoulder | Dutch tilt | ground level | profile
- Framings: extreme close-up (face only) | close-up | medium close-up (bust) | medium | medium wide | wide | extreme wide

Output: ONLY a JSON array, one object per segment, nothing else — no markdown code fences, no commentary before or after it. Each object must have exactly these keys: "segment" (integer, 1-based), "direction" (the full camera direction description). Example:
[
  {"segment": 1, "direction": "..."},
  {"segment": 2, "direction": "..."}
]\
"""

_VARIETY_AUDITOR_SYSTEM_JSON = """\
You are a shot-variety auditor reviewing a complete shot list before filming.
Input: a JSON-described shot list, one line per segment listing the segment's action and its current camera direction.

CHECK ACROSS ALL SEGMENTS:
1. FACING — classify each shot's subject facing relative to camera: frontal / three-quarter left / three-quarter right / left profile / right profile / back or over-shoulder.
   - NEVER allow 3 or more consecutive frontal-facing shots
   - Use at least 3 different facing categories across the timeline
   - Include at least one profile or back/over-shoulder shot in every 4 segments
2. CAMERA POSITION — vary front / side / behind / low / high; never the same side 3 times in a row.
3. APPROACH — at most ONE shot in the whole timeline where the subject walks toward the camera.
4. WALKING SIDE-VIEW — if 3 or more segments involve walking, AT LEAST ONE of them must film the subject from the SIDE while walking (lateral crossing in 16:9, or handheld lateral tracking alongside in either orientation). If none exists, convert one walking segment to a side-view lateral shot — any walking segment EXCEPT the first segment and the last segment of the timeline.

REWRITE only the directions needed to fix violations; copy all others verbatim.
When you rewrite, state the new facing EXPLICITLY and CONCRETELY (e.g. "seen in right profile from the side, her face turned away from the lens", "framed from behind her left shoulder").

NEVER change:
- shot scale (a close-up stays a close-up)
- "feet only" / tracking / static designations
- the action content itself
For stationary actions (fixing hair, feeding, sipping, hanging laundry) change the CAMERA's side (profile, over-shoulder, behind at an angle) rather than the subject's pose.

Output: ONLY a JSON array, one object per segment, nothing else — no markdown code fences, no commentary before or after it. Each object must have exactly these keys: "segment" (integer, 1-based), "direction" (rewritten if it violated a rule above, otherwise copied verbatim from the input). Example:
[
  {"segment": 1, "direction": "..."},
  {"segment": 2, "direction": "..."}
]\
"""

# 失敗モードチェックリスト(実生成で観測した事故の集約、Pass4 Linterが使用)。
# 全項目を一括で渡すとローカルLLMの判定精度が落ちるため、
# _build_lint_checklist() がセグメントに該当する項目だけを選んで渡す。
_LINT_CHECKS: dict[str, str] = {
    "C1": 'SINGLE SUBJECT: the subject is described at ONE position with ONE direction of movement — never small AND large, or at two frame positions, in the same prompt.',
    "C2": 'SINGLE OBJECT: an object the subject holds (cup, umbrella, fabric, phone) exists ONLY in her hands — never additionally as a separate foreground element ("the cup\'s rim dominates the foreground" + "she lifts the cup" = TWO cups get generated).',
    "C3": 'ANIMAL FIRST: the animal MUST be named with size and frame position within the FIRST TWO sentences, placed beside/ahead of the subject in clear view (never hidden behind heels/legs), with its OWN explicit fur color/pattern (otherwise it inherits the human\'s hair color/texture), and its sound must be first in the audio. The animal is ALREADY PRESENT for the whole shot — rewrite any "waiting for the cat" / "where the cat will appear" / "approaching cat" narrative so the animal is in frame eating/sitting/walking from the first frame. Late-introduced or not-yet-arrived animals are not generated.',
    "C4": 'ATTACHED HANDS: hands/arms always belong to the visible subject — "her hand reaches down", NEVER "a hand enters the frame" (an entering hand generates a third person\'s hand).',
    "C5": 'CHARACTER SCALED: the character sentence must be present, scaled to the shot — close-up/bust: NO lower-body garments (jeans/sneakers pull the framing to full body); feet-only: ONLY lower-body garments (hair/face words pull the face into frame); wide: full description.',
    "C6": 'ASPECT: the exact phrase "{frame_phrase}" appears; {aspect_forbidden} and "DV camcorder"/"4:3" must NOT appear.',
    "C7": 'STEAM: steam/mist is allowed only if its source visibly fills a large part of the frame (bath, large pot, tight close-up on the cup); otherwise the steam must be removed.',
    "C8": 'HAIR: hair-touching = smoothing/patting an ALREADY-TIED ponytail that stays attached; never fingers tangling/gathering/pulling loose strands.',
    "C9": 'SCALE LOCK: the shot scale and camera motion from the constraint are unchanged (close-up stays close-up; feet-only shows no face/torso; static stays static).',
    "C10": 'ANGLE REALIZED: the constraint specifies a LOW or HIGH angle. Show the view, do not just name it. Two kinds of low angle: (a) camera below the SUBJECT looking up at her — describe what the upward view shows using ONLY elements already in this scene (her jaw, the underside of raised arms, whatever is above her IN THIS SCENE); (b) camera low at ground level aimed at ground-level objects (hands, shells, feet) — a shallow ground-level perspective, no sky, no looking up. NEVER invent new overhead objects (clotheslines, cables, wires) that are not already in the scene. Mirror logic for high angle.',
    "C11": 'FACING: the subject\'s facing relative to camera (frontal / three-quarter / profile / from behind) is stated explicitly, matching the constraint.',
    "C12": 'RESERVED PROPS: props that are the centerpiece of another segment\'s action (e.g. hanging laundry / clothesline / drying garments) must NOT appear in this shot at all. Remove every mention of them and replace with neutral alley elements (potted plants, cables, walls).',
    "C13": 'HORIZONTAL FILL: this 16:9 landscape shot has a vertical composition that will pillarbox to 4:3 unless the horizontal space is filled. Extend elements ALREADY IN THE SCENE to the left and right edges (the ground surface, walls, horizon, existing overhead line). NEVER introduce new objects (lines, cables, wires, poles) that are not already in the scene.',
    "C14": 'ACTION FIDELITY: every element written in the timeline action (see "action" in the constraint) must be visibly present in the prompt — the key action verbs, held objects, companions/animals, glances, and spoken words (quoted dialogue verbatim). Add each missing element naturally without changing shot scale or camera motion.',
    "C15": 'LATERAL CONSISTENCY: the prompt mixes two incompatible lateral shot types. Choose exactly ONE and rewrite: TRACKING alongside = the camera moves parallel with her, she stays CENTERED at constant size in FULL SIDE PROFILE (body perpendicular to the camera) while the background scrolls past, and she NEVER enters or exits the frame edges. CROSSING = the camera holds fixed and she walks in FULL SIDE PROFILE from one edge to the other. Remove all wording of the other type.',
    "C16": 'HAND BUDGET: the character has exactly TWO hands, and a persistent prop (umbrella/bag/phone) already occupies one of them. Rewrite so no third hand is needed: make the other action ONE-HANDED ("cradles the cup in her free hand") or explicitly free the hand ("the closed umbrella tucked under her arm" / "resting against her shoulder"). Remove every "with both hands" that conflicts with the held prop — the model generates a third arm otherwise.',
    "C17": 'GARMENTS PRESENT: the outfit named in the "state" (current established clothing) must appear somewhere in the prompt using the same garment words — do not let the outfit compress away into nothing when the shot describes other things, since an unmentioned outfit becomes the image model\'s own invented guess instead of the specified one.',
    "C18": 'STATE DETAILS PRESENT: "state" is the absolute, authoritative source for everything about her current appearance (hairstyle, accessories, anything beyond her fixed identity) — every detail it specifies (e.g. a specific hairstyle like "messy ponytail with side-swept bangs") must actually appear in the prompt, not just a generic fallback from the character sentence (e.g. plain "black wavy hair" alone is NOT enough if state specifies more). Weave the full state detail in naturally.',
    "C19": 'UNGROUNDED SECONDARY MOTION: this prompt mentions an animal, or laundry/a clothesline, that the timeline action for THIS segment never calls for. These are hallucinated additions copied from the Scene Writer\'s own illustrative examples, not something that actually happens in this shot. Remove them and replace with a neutral environmental detail (light, shadow, breeze, an already-present background element) instead.',
    "C20": 'SELFIE ALREADY IMPLIES THE PHONE: "selfie" by itself already means she is holding up her phone to film/photograph herself — do NOT additionally write "holding a smartphone", "holding her phone", "gripping her phone", etc. as a separate action. This double-describes the same phone and causes a third hand to be generated. Remove the redundant explicit phone-holding phrase and keep "selfie" alone.',
}


# C15: 並走トラッキングと端出入り(横断)の混在検出
_LATERAL_TRACKING_RE = re.compile(
    r"tracks?\s+(?:laterally\s+)?alongside|camera\s+moves?\s+parallel|lateral\s+tracking",
    re.IGNORECASE,
)
_EDGE_CROSSING_RE = re.compile(
    r"(?:enters?|exits?)\s+(?:from\s+)?(?:the\s+)?(?:left|right)\s+edge",
    re.IGNORECASE,
)

# C13: 16:9でのlow/high angleに水平充填の記述があるかの証拠キーワード
_HORIZONTAL_FILL_EVIDENCE_RE = re.compile(
    r"full (?:horizontal )?width|left edge to (?:the )?right|from (?:the )?left edge|"
    r"stretch\w* across|spans? (?:the )?(?:frame|width)|across the (?:entire |full )?frame|"
    r"left and right edges|both (?:left and right )?(?:sides|edges)",
    re.IGNORECASE,
)

# C12検出用: プロンプト内でそのプロップとみなす語
_RESERVED_PROP_DETECT: dict[str, tuple[str, ...]] = {
    "laundry": ("laundry", "clothesline", "hanging shirts", "hanging garments", "white sheet", "drying towels"),
}


def _fix_aspect_phrase(ltx_prompt: str, orientation: str) -> str:
    """アスペクト文言をPythonで決定論的に保証する(LLM fixerが落とすことがあるため)。"""
    a = _ASPECT[orientation]
    phrase = a["frame_phrase"]
    other = _ASPECT["vertical" if orientation == "horizontal" else "horizontal"]["frame_phrase"]
    ltx_prompt = re.sub(re.escape(other), phrase, ltx_prompt, flags=re.IGNORECASE)
    # 逆向きの禁止語を除去
    if orientation == "vertical":
        ltx_prompt = re.sub(r"\b16:9\b|\bwidescreen\b|\blandscape\b", "", ltx_prompt)
    else:
        ltx_prompt = re.sub(r"\b9:16\b|\bportrait\b", "", ltx_prompt)
    ltx_prompt = re.sub(r"[ \t]{2,}", " ", ltx_prompt).replace(" ;", ";").replace(" ,", ",")
    if phrase.lower() not in ltx_prompt.lower():
        m = re.search(r"[;.]", ltx_prompt)
        if m:
            ltx_prompt = ltx_prompt[: m.start()] + f" in {phrase}" + ltx_prompt[m.start():]
        else:
            ltx_prompt = f"{phrase}; " + ltx_prompt
    return ltx_prompt.strip()


# C10: 見上げ/見下ろし構図が実際に記述されている証拠キーワード
_LOW_ANGLE_EVIDENCE_RE = re.compile(
    r"seen from below|viewed from below|looking up at|looks up at|from a low vantage|"
    r"camera (?:looks|angled|tilted|angles|tilts)\s+(?:sharply\s+)?up|underside of|"
    r"shooting up(?:ward)?|angled (?:sharply )?upward|"
    r"foreshorten|towering|looms? (?:above|over)",
    re.IGNORECASE,
)
_HIGH_ANGLE_EVIDENCE_RE = re.compile(
    r"seen from above|viewed from above|looking down at|looks down at|bird'?s.eye|"
    r"camera (?:looks|angled|tilted|angles|tilts)\s+(?:sharply\s+)?down|top of (?:her|his) head",
    re.IGNORECASE,
)
# C2: 持ち物が前景の独立要素として書かれているパターン
_HELD_OBJECTS = ("cup", "mug", "umbrella", "phone", "bottle", "fabric", "garment")


def _detect_violations(ltx_prompt: str, direction: str, orientation: str, reserved_props: list[str] | None = None, action: str = "", state: str = "") -> list[str]:
    """完成プロンプトの違反を決定論的に検出する(判定をLLMに任せると誤検出が多いためPythonで行う)。
    検出した違反の修正のみLLM(_PROMPT_FIXER_SYSTEM)に任せる。
    ※C6(アスペクト文言)は検出でなく`_fix_aspect_phrase()`で常時Python修復するためここには無い。"""
    p = ltx_prompt.lower()
    d = direction.lower()
    ids: list[str] = []

    # C2: 同一の持ち物が「前景の独立要素」と「手の中」の両方に書かれている
    for obj in _HELD_OBJECTS:
        if obj in p:
            in_foreground = re.search(rf"\b{obj}\b[^.;]*\b(foreground|dominat)", p) or \
                            re.search(rf"\b(foreground|dominat)\w*[^.;]*\b{obj}\b", p)
            in_hands = re.search(rf"\b(holds?|holding|lifts?|lifting|brings?|raises?|grips?|gripping|cradl\w+)\b[^.;]*\b{obj}\b", p)
            if in_foreground and in_hands:
                ids.append("C2")
                break

    # C5: tightショット(close-up/bust、feet除く)なのに下半身アイテムがプロンプトに残っている
    # (キャラ文は_trim_character_for_scaleで削っているが、Scene Writerが独自に持ち込むケースがある)
    is_tight = _is_tight_scale(direction)
    if is_tight and any(item in p for item in _LOWER_BODY_ITEMS):
        ids.append("C5")

    # C10: low/high angle 指定なのに見上げ/見下ろし構図の記述証拠がない。
    # ただし対象は「被写体を見上げる/見下ろすショット」のみ — 地面の物体(手・貝殻・足元)への
    # ground-level接写は見上げ構図ではないため発火させない(発火させると洗濯物テンプレの
    # 「空+顎+頭上の物」が貝殻シーンに強制されて上を向く事故になる。2026-07-05)
    is_subject_angled = bool(re.search(
        r"below (?:the )?waist|shooting up|looking up at|upward at|from below the subject", d)) or \
        bool(re.search(r"\b(low|high)[- ]angle\b", d) and re.search(r"\bmedium\b|\bwide\b|full body", d))
    is_angled = is_subject_angled
    if is_subject_angled and re.search(r"\blow[- ]angle\b|\blow angle\b|below (?:the )?waist|shooting up", d) and not _LOW_ANGLE_EVIDENCE_RE.search(ltx_prompt):
        ids.append("C10")
    elif is_subject_angled and re.search(r"\bhigh[- ]angle\b", d) and not _HIGH_ANGLE_EVIDENCE_RE.search(ltx_prompt):
        ids.append("C10")

    # C13: 16:9のlow/high angle(本質的に縦構図)なのに水平充填の記述がない → 4:3ピラーボックス化する
    if orientation == "horizontal" and is_angled and not _HORIZONTAL_FILL_EVIDENCE_RE.search(ltx_prompt):
        ids.append("C13")

    # C3: 動物が「まだ登場していない」物語(waiting for / will appear) → 動物が生成されない
    if _ANIMAL_ABSENT_RE.search(ltx_prompt):
        ids.append("C3")

    # C14: タイムラインactionの要素(視線・セリフ・持ち物・動物等)がプロンプトから脱落
    if action and _missing_action_elements(ltx_prompt, action):
        ids.append("C14")

    # C12: 他セグメントの主役プロップが背景に混入している
    for prop in (reserved_props or []):
        if any(k in p for k in _RESERVED_PROP_DETECT.get(prop, ())):
            ids.append("C12")
            break

    # C15: 並走トラッキング(中央固定)と横断(端出入り)の混在 → 斜め移動・パンに化ける
    if _LATERAL_TRACKING_RE.search(ltx_prompt) and _EDGE_CROSSING_RE.search(ltx_prompt):
        ids.append("C15")

    # C16: 手の本数超過(傘で片手が塞がっているのに「both hands」) → 3本目の腕が生成される
    if _hand_budget_violation(ltx_prompt):
        ids.append("C16")

    # C20: "selfie"がスマホを持つ行為を既に含意するのに、別途「holding a smartphone」等の
    # 明示描写が同居 → 暗黙+明示の2つのスマホ/3本目の手が生成される(2026-07-16)
    if _selfie_phone_redundancy(ltx_prompt):
        ids.append("C20")

    # C17: STATEが指定する衣装がプロンプトから欠落している(character_lineは2026-07-06以降
    # 衣装を持たないため、stateが唯一の情報源)。ショットスケールに応じて必要な部分だけ要求する
    # (C5と対称: tightは上半身のみ・feetは下半身のみ・それ以外は全部)。初版は「close-up」を
    # 含むだけで全除外していたため、medium close-up等(上半身は映る)でも上半身衣装の欠落を
    # 見逃す事故が実データで発覚(2026-07-07)、この区別を追加した
    is_tight = _is_tight_scale(direction)
    is_feet_only = "feet" in d
    if state:
        missing = _garments_missing(ltx_prompt, state)
        if is_tight:
            missing = [g for g in missing if not any(item in g for item in _LOWER_BODY_ITEMS)]
        elif is_feet_only:
            missing = [g for g in missing if any(item in g for item in _LOWER_BODY_ITEMS)]
        if missing:
            ids.append("C17")

    # C18: STATEの断片(衣装以外も含む — 髪型等)が半数未満しか反映されていない。
    # STATEは「見た目全部」を絶対的な情報源として扱う設計(2026-07-07)のため、衣装名の
    # 固定語彙(C17)がカバーしない属性も汎用的に検品する。feet-onlyは顔/髪が映らないため対象外
    if state and not is_feet_only and _state_details_missing(ltx_prompt, state):
        ids.append("C18")

    # C19: Scene WriterのSECONDARY MOTION指示が挙げる具体例(動物・洗濯物)が、
    # このセグメントの実際のactionに無いのに出現している(2026-07-10、システムプロンプトの
    # 例示にそのまま引っ張られて無関係なシーンへ混入した実例で発覚。C12(他セグメントへの
    # 予約プロップ混入)とは別に、「そもそもどのセグメントの主題でもない」ケースを検出する)
    if action:
        if _animals_in(ltx_prompt) and not _animals_in(action):
            ids.append("C19")
        elif any(_has_word(p, kw) for kw in _RESERVED_PROP_DETECT["laundry"]) and \
                not any(_has_word(action.lower(), kw) for kw in _RESERVED_PROP_DETECT["laundry"]):
            ids.append("C19")

    return ids


_PROMPT_FIXER_SYSTEM = """\
You are an LTX-2.3 prompt fixer. You receive a prompt and a list of specific violations.
Rewrite the prompt so that every violation is fixed. Change ONLY what the violations require; preserve everything else — wording, style keywords, character sentence, audio description, aspect-ratio phrase, one flowing paragraph, ~140-180 words.

VIOLATED RULES TO FIX:
{violated_rules}

Output ONLY the corrected prompt text. No commentary, no headers.\
"""

_SCENE_WRITER_SYSTEM = """\
You are a scene director describing exactly what is visible in a single camera shot.
Your job: translate the camera direction and shot action into precise spatial language — like describing what you see on a monitor to a VFX supervisor.

Do NOT use LTX-2.3 terminology or cinematic jargon yet. Just describe the spatial reality.

⚠️ STATE IS AUTHORITATIVE: if the "Creative intent" block below includes a STATE line (time-of-day/lighting, weather, current outfit), that is the confirmed ground truth for THIS shot — commit to it fully for the lighting, weather, and clothing you describe, even when it differs from the baseline in "Character & Setting" (e.g. STATE says night while the reference's default scene is daytime). Never hedge between two possibilities ("suggesting early morning or late night") — state the one lighting condition plainly. This applies even when the shot action itself doesn't mention it.

COVER IN THIS ORDER:
1. FRAME LAYOUT: Where is the camera? (height, angle). What occupies the left side, right side, foreground, background?
2. SUBJECT POSITION AT START: Where in the frame does the subject begin? Use screen coordinates (left/center/right edge, upper/lower, near/far).
3. ACTION: What does the subject do? What specific body parts move and how? What stays still?
4. MOVEMENT ARC (walking shots): Describe EXPLICITLY where the subject enters and exits. For TRACKING shots (camera follows the subject): the subject stays at the SAME position and size in frame throughout; it is the ENVIRONMENT that scrolls past. Describe what scrolls by. For FEET-ONLY shots: describe ONLY feet, shins, and ground — NEVER the head, face, or a glance at the camera.
5. END STATE: What does the frame look like at the last frame of the shot?
6. STATIONARY TASKS (hanging laundry, touching face, drinking): Subject is at a FIXED lateral position — feet planted, subject does NOT move sideways along any surface. Only specific limbs move.
7. CAMERA MOTION (if the direction specifies push-in / pull-back / drift / tracking): describe how the framing changes over the shot because of the camera itself.

SECONDARY MOTION — MANDATORY:
Include exactly 1-2 small environmental motions appropriate to this scene so the frame feels alive,
drawn ONLY from elements THIS scene's action or location already establishes: a breeze moving hair
or fabric/foliage already present, shifting light or shadows, a small movement FROM AN ANIMAL OR
PERSON ONLY IF one is already part of this shot's action, or a vehicle/pedestrian in the far
background ONLY IF an outdoor street is already part of this scene's location.
Rules: pick ONLY from things that exist in THIS scene's location. Keep them SUBTLE, in the
BACKGROUND or on already-present subjects. NEVER introduce a new person, animal, or object
(laundry, décor, props, etc.) that this scene's action/location does not already establish —
do not invent one just to satisfy this requirement.

⚠️ STEAM/MIST RULE: steam is allowed ONLY when its source occupies a LARGE area of the frame — a hot spring, bath, large cooking pot, or a tight close-up where the cup/bowl itself fills much of the frame. NEVER describe steam from a small, distant, or off-screen source (e.g. a coffee cup in a medium shot) — the model will emit steam from the whole scene instead of the cup. If the shot is not tight enough, omit the steam entirely.

⚠️ TIGHT SHOT = NO LOWER BODY: in a close-up or bust shot, footwear and lower-body garments (sneakers, jeans) are OUTSIDE the frame — never mention them anywhere in the scene, not even "partially visible in the foreground". Mentioning them makes the model widen the shot or place a shoe where the main subject should be.

⚠️ RESERVED PROPS: if the user message lists "props reserved for other segments", those props must NOT appear in this shot at all — not even as background decoration. Use neutral elements (potted plants, cables, walls) instead.

⚠️ HAND BUDGET RULE: the character has exactly TWO hands. If a persistent prop (umbrella, bag, phone) occupies one hand, every other action must be ONE-HANDED — never "with both hands". Either write the action one-handed ("cradles the cup in her free hand") or explicitly free the hand first ("the closed umbrella tucked under her arm"). Demanding a third hand makes the model GENERATE a third arm. The same applies to feet: never describe a pose that needs a third leg.

⚠️ HELD OBJECT RULE: an object the subject holds (cup, umbrella, phone, food) exists ONLY in their hands. NEVER describe it — or a part of it like "the cup's rim" — as a separate foreground element dominating the frame while the subject also holds it. Describing the same object at two positions makes the model generate TWO of it (a giant standalone cup in front + another in her hands).

⚠️ HAIR-TOUCHING RULE: when the subject fixes or adjusts hair, hands SMOOTH, PAT, or TUCK hair that stays attached to the head; palms glide along the surface of an ALREADY-TIED ponytail. NEVER describe fingers tangling in strands, gathering loose hair, pulling hair, or creating a ponytail from loose hair — the model renders this as hair detaching or being pulled out. Keep part of the face visible for context.

⚠️ ANIMAL VISIBILITY RULE: if an animal must appear in the shot, it is VISIBLE from the very first frame — positioned AHEAD OF or BESIDE the subject in clear view, described within the first two sentences with explicit size and frame position. NEVER place the animal hidden behind legs/heels or introduce it mid-shot; it will simply not be generated. EVEN IF the action says the animal "follows", render it walking BESIDE or AHEAD OF the feet where the camera can see it. NEVER write a scene where the animal has not arrived yet ("where the cat will appear", "waiting for the cat", "the approaching cat") — the animal is ALREADY in the FRAME LAYOUT, eating/sitting/walking, for the entire shot.

CRITICAL FOR STATIONARY TASKS — describe the PHYSICAL ENVIRONMENT SETUP FIRST:
- "hanging laundry / pinning clothes on line" (LOW ANGLE SHOT — camera below waist, angled upward):
  → FIRST: Camera position is low (below the subject's waist), angled upward. The clothesline is visible at the TOP of the frame. The subject's feet and lower legs are at the BOTTOM of the frame.
  → SECOND: Subject stands DIRECTLY UNDERNEATH the line, feet planted. She does NOT walk along it.
  → THEN: Arms extend straight up toward the top of the frame, hands grip/pin fabric to the line above.
  → The upward reach should feel dramatic — arms going from waist-level toward the top edge of frame.
- "touching face/hair" = camera is CLOSE on the contact point; subject's full body is NOT visible; describe the hands' position relative to the head/hair
- "feeding/petting animal" = animal is PRIMARY SUBJECT: describe the animal first (position in frame, which body part is visible, how large it appears) and give the animal its OWN explicit fur color and pattern (e.g. "a short-haired brown tabby cat with white paws") — without this the model copies the human's hair color/texture onto the animal. Camera is at animal's eye level or lower. The animal occupies the center or foreground of the frame IN SHARP FOCUS. Her hand, VISIBLY CONNECTED to her arm and body, reaches toward the animal. The human face may appear partially in the background, OUT OF FOCUS.
  ⚠️ NEVER write "a hand enters the frame" while any part of the subject is visible in the shot — a hand "entering" implies an off-screen owner and the model generates a THIRD PERSON's hand. Hands belong to the visible subject: write "her hand reaches down", never "her hand enters".
- "hanging laundry / clothesline": The clothesline ALREADY has multiple garments hanging on it (shirts, towels, etc.) at the START of the shot. The subject reaches up to add one more item. The existing laundry is visible and colorful against the background.

Write 150–200 words in plain English. No prompt engineering language.\
"""

_LTX_FORMATTER_SYSTEM_BASE = """\
You are an LTX-2.3 T2V prompt engineer.
Input: a spatial scene description (what a director sees on a monitor). Your job: reformat it as an LTX-2.3 optimized prompt.
Do NOT reinterpret what happens — follow the spatial description exactly.

OUTPUT: One flowing paragraph and NOTHING ELSE — never append or echo the character/style reference block, the location list, or any leftover input text after the paragraph. Present tense. Active verbs. In this structure order:
  1. SHOT SCALE + GENRE (e.g. "static eye-level wide shot", "ground-level medium close-up", "handheld tracking shot from behind")
  2. SCENE: lighting, color palette, atmosphere, surface textures
  3. ACTION: what subject does, described as a natural sequence with active verbs
  4. CHARACTER APPEARANCE: copy verbatim from reference; scale detail to shot scale (close-up = face/skin/expression texture; wide = silhouette/color only)
  5. CAMERA BEHAVIOR + END STATE: state what camera does, then describe how subject appears at END of shot
  6. AUDIO: 1–3 ambient sounds specific to THIS shot. If an animal is visible, ALWAYS include its sound first (cat: meowing or purring; bird: chirping; dog: barking).

{framing_block}

WALKING SHOTS — translate movement arc with these exact patterns:
- "toward lens" → "subject appears as a small figure in the upper-center of the {frame_phrase} frame, walks directly toward the fixed camera, growing steadily larger with each step, until filling medium-close scale in the lower-center by the end"
- "away from lens" → "subject starts as a medium figure in the lower-center foreground, walks away from the fixed camera, shrinking gradually as the environment expands behind her, ending as a small silhouette near the center horizon"
- "crosses frame left-to-right" → "the camera holds completely fixed; subject walks in FULL SIDE PROFILE, her body perpendicular to the camera axis, entering from the left edge of the {frame_phrase} frame, traversing the full horizontal width at consistent apparent size, exiting the right edge; the background stays fixed"
- "crosses frame right-to-left" → mirror of above; subject enters from right edge, exits left edge
- "angled approach from right-background" → "subject enters from the right side of the {frame_phrase} frame, already partially visible, walks steadily toward the camera while angling slightly leftward; grows larger as she approaches, ending center-left in medium shot" — describe ONE person at ONE position moving; NEVER describe a small figure AND a large figure simultaneously
- "angled approach from left-background" → mirror of above
- "feet only, crosses frame" → "ground-level close-up on feet and shins; feet and pavement fill the full frame width; no torso or face visible; feet enter from one edge and cross to the opposite edge at constant size"
- "feet only, walks away from lens" → "ground-level close-up on feet and shins; feet start large at bottom-center of the {frame_phrase} frame, grow smaller as subject walks away from camera, exit near top-center as small silhouettes; pavement texture dominates the frame; no torso or face visible"

CAMERA MOTION — translate with these exact patterns (only when the Shot Director constraint specifies motion):
- "slow push-in" → "the camera drifts slowly closer to the subject throughout the shot, framing gradually tightening"
- "slow pull-back" → "the camera drifts slowly away from the subject, gradually revealing more of the surrounding environment"
- "subtle handheld drift" → "the handheld camera sways and reframes subtly with no directional movement"
- "tracking follow from behind" → "the handheld camera follows behind the walking subject at a constant distance; the subject stays the same size in frame throughout while the environment scrolls past on both sides" — describe ONE subject at ONE fixed frame position
- "tracking follow, ground level on feet" → "the handheld camera follows low behind the subject's feet and shins at a constant distance; the feet stay the same size in frame while the pavement scrolls beneath"
- "lateral tracking alongside" → "the handheld camera moves parallel with the walking subject at matching speed; she stays CENTERED at constant size in FULL SIDE PROFILE, body perpendicular to the camera, while the background scrolls horizontally past behind her; she NEVER enters or exits the frame edges"
Never invent camera motion that the constraint does not specify.

⚠️ LATERAL CONSISTENCY — never mix the two lateral types in one prompt:
- TRACKING alongside = camera moves, subject stays centered, background scrolls, NO entering/exiting edges
- CROSSING = camera fixed, subject enters one edge and exits the other, background fixed
Writing both ("camera tracks alongside" + "exits the right edge") makes the model produce diagonal drift or a pan instead of a true side view.

⚠️ NEVER describe the subject at two positions in the same sentence (e.g., "appears small on the right...ends large on the left") — this causes dual-person hallucination. Always describe ONE direction of movement from ONE starting point.

⚠️ The same applies to OBJECTS: a held object (cup, umbrella, phone) appears ONLY in the subject's hands. Never write it as a separate foreground element ("cup rim dominates the foreground") while the subject also holds it — the model generates two of the object.

⚠️ SHOT SCALE LOCK: When the user message contains "Shot Director constraint", you MUST use exactly that shot scale and camera motion. If it says "close-up", write "close-up" — never "medium" or "wide". If it says "crosses frame right-to-left", use that exact movement — never "diagonal approach" or "toward lens".

⚠️ CHARACTER LOCK: When the user message contains a "## Character" section, that sentence MUST appear (verbatim or near-verbatim) in your output — EVEN IF the character is barely visible in this shot (feet only, blurred background, back turned). Without it the model generates a different person per shot. Note: that Character sentence deliberately excludes clothing/glasses/hairstyle — those come ONLY from "## Current established state" below (if present); never invent or reuse clothing from elsewhere. If the Character sentence does NOT mention an age descriptor, do not invent one yourself (never add "young"/"middle-aged" on your own) — doing so inconsistently across segments changes her apparent age shot to shot.
- PLACEMENT: when an animal is the PRIMARY subject of the shot, the animal owns the first two sentences (size + frame position); place the character sentence AFTER the action description, never as sentence 1 or 2.

⚠️ CURRENT STATE OVERRIDES THE REFERENCE: the "Character & Style reference" describes the DEFAULT baseline for the whole video (e.g. it might say "bright morning light" as the general premise). If a "Current established state" block is given below, THAT is the ground truth for time-of-day/lighting, weather, and clothing for THIS specific shot — it may differ from the reference's default (e.g. a later shot at night). Always follow the current state, never the reference's default, when they conflict.

SECONDARY MOTION: The scene description contains 1-2 small environmental motions (breeze in hair/laundry, tail flick, distant passerby, steam, swaying cables). PRESERVE them — do not drop them. Weave them into SCENE or ACTION.

STATIONARY TASK SHOTS:
- Explicitly state: "subject remains at a fixed lateral position throughout; only [arms/hands] move"
- Use "clips", "pins", "attaches" — NEVER "hangs" (avoids body-suspension ambiguity)
- Include "both feet planted on the ground" for any overhead-reach action

FRAMING DISCIPLINE:
- Close-up: no body below bust in frame; rich face/skin/hair detail; describe horizontal frame fill
- Medium close-up (bust): chest to top of head only; hands visible only if at shoulder height or above
- Wide: minimal character detail (color, silhouette, movement); rich environment spatial layout

LOW/HIGH ANGLE — the word "low-angle" alone does NOT produce the angle. PRESERVE the scene description's vertical composition evidence:
- low angle: state what occupies the TOP edge (clothesline, sky, cables) ABOVE the subject, that the subject is SEEN FROM BELOW (jaw, underside of raised arms visible), and that her lower body appears large at the BOTTOM edge with upward foreshortening
- high angle: mirror logic (ground fills frame, subject seen from above, head large)
Never compress this into a normal eye-level composition ("head upper-middle, waist at bottom") — that loses the angle.
⚠️ In 16:9 landscape, an upward/downward-angled composition is inherently vertical and the model will pillarbox it to 4:3 unless you ALSO fill the horizontal space: extend elements ALREADY IN THE SCENE across the full width (an existing overhead line, walls, roofs, the horizon). NEVER invent new overhead objects (clotheslines, cables) that are not in the scene.

NEVER write: "DV camcorder", "4:3", or any term implying non-{aspect} format
LENGTH: 140–180 words

ASPECT RATIO: This video is {label} ({dims}). {compose}\
"""

_ASPECT = {
    "horizontal": {
        "aspect": "16:9 widescreen",
        "label": "16:9 WIDESCREEN LANDSCAPE",
        "dims":  "1280×720",
        "frame_phrase": "16:9 widescreen landscape",
        "compose": "Always include wide horizontal environmental elements (background scenery, left-to-right depth, side-by-side objects) so the landscape frame is naturally filled. Never compose as a narrow vertical or portrait shot.",
        "framing_block": """\
16:9 LANDSCAPE FRAMING — CRITICAL (prevents 4:3 output):
- ALWAYS include the phrase "16:9 widescreen landscape" somewhere in the prompt
- CLOSE-UP shots in 16:9: the subject fills the frame; blurred background fills the horizontal space on left/right. DO NOT write "wide shot" for a close-up — write "close-up in 16:9 landscape frame"
  Example close-up: "extreme close-up in 16:9 widescreen landscape; cat's face and human hand fill the center; blurred alley wall fills the peripheral left and right"
  Example wide: "wide landscape frame fills horizontally — potted plants left edge, overhead cables top"
- NEVER write "wide shot" if the shot scale specified is close-up, medium close-up, or tight
- NEVER describe a composition that implies portrait, narrow, or square framing""",
    },
    "vertical": {
        "aspect": "9:16 vertical",
        "label": "9:16 VERTICAL PORTRAIT",
        "dims":  "720×1280",
        "frame_phrase": "9:16 vertical portrait",
        "compose": "Always compose vertically — subject centered in a tall frame, top-to-bottom depth (sky/ceiling to ground), vertical architectural elements (poles, walls, doorways). Never compose as a wide horizontal shot.",
        "framing_block": """\
9:16 VERTICAL PORTRAIT FRAMING — CRITICAL (prevents landscape output):
- ALWAYS include the phrase "9:16 vertical portrait" somewhere in the prompt
- NEVER write "landscape", "widescreen", or "16:9" anywhere
- The frame is TALL and NARROW: compose top-to-bottom (sky/cables/ceiling at top, subject center, ground at bottom); lateral space is minimal
- CLOSE-UP shots in 9:16: the subject fills the frame vertically; foreground/background depth fills above and below, not left/right
  Example close-up: "extreme close-up in 9:16 vertical portrait; face fills the center, hair reaching the top edge, collar at the bottom"
  Example full: "9:16 vertical portrait frame filled top-to-bottom — overhead cables at top, subject center, wet pavement at bottom"
- NEVER write "wide shot" if the shot scale specified is close-up, medium close-up, or tight
- NEVER describe wide horizontal sweeps of environment (left-edge-to-right-edge enumerations)""",
    },
}


def _build_ltx_system(orientation: str) -> str:
    a = _ASPECT[orientation]
    return _LTX_FORMATTER_SYSTEM_BASE.format(**a)



def _enforce_character_line(ltx_prompt: str, character_line: str) -> str:
    """LTXプロンプトにキャラクター記述が欠落している場合、先頭文の直後に挿入する。
    国籍・衣装が抜けるとセグメントごとに別人・別衣装が生成されるため(2026-07-04)。"""
    if not character_line:
        return ltx_prompt
    if not _character_tokens_missing(ltx_prompt, character_line):
        return ltx_prompt
    prompt_lower = ltx_prompt.lower()
    line = character_line.rstrip(".") + "."
    # 動物がPRIMARYのシーンでは冒頭の優先枠を奪わないよう末尾に追加
    # (冒頭に挿入すると動物より人物が優先され、動物が生成されなくなる)
    if any(kw in prompt_lower for kw in ("cat", "kitten", "dog", "puppy", "bird")):
        return ltx_prompt.rstrip() + " " + line
    # それ以外は先頭文(ショットスケール+シーン)の直後に挿入
    m = re.search(r"[.;]\s", ltx_prompt)
    if m:
        return ltx_prompt[: m.end()] + line + " " + ltx_prompt[m.end():]
    return line + " " + ltx_prompt


# direction側の歩行ショット指標(actionに歩行動詞がない足元トラッキング等も歩きと数える)


async def _format_to_ltx_prompt(scene_desc: str, global_desc: str, ambience: str, duration: int, orientation: str, direction: str = "", character_line: str = "", action: str = "", reserved_props: list[str] | None = None, state: str = "") -> str:
    """Pass 3: シーン記述をLTX-2.3最適化プロンプトに変換する。
    state: Pass0が決定した「このセグメント時点のSTATE」(時間帯/天気/服装)。参照文(global_desc)の
    既定値と矛盾する場合、こちらを優先させる(i2v_timeline_cliV2.pyでキーフレーム本文がglobal_descの
    既定の時間帯・照明をそのまま書いてしまう事故が実テストで発覚し、同じ修正を移植)。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    character_line = _trim_character_for_scale(character_line, direction)
    direction_constraint = (
        f"\n## Shot Director constraint (MUST match — do NOT change shot scale, camera motion, or movement direction)\n{direction}"
        if direction else ""
    )
    character_block = (
        f"## Character (MUST appear verbatim in the prompt — see CHARACTER LOCK)\n{character_line}\n\n"
        if character_line else ""
    )
    state_block = (
        f"## Current established state (this is the only source for clothing, glasses, hairstyle, and "
        f"lighting/weather for THIS shot — do not invent or reuse clothing/hairstyle from anywhere else)"
        f"\n{state}\n\n"
        if state else ""
    )
    user_msg = (
        f"/no_think\n"
        f"## Character & Style reference\n{global_desc}\n\n"
        f"{character_block}"
        f"{state_block}"
        f"## Scene description ({duration}s)\n{scene_desc}{direction_constraint}\n\n"
        f"## Available ambient sounds\n{ambience}"
    )
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": _build_ltx_system(orientation)},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.7,
        max_tokens=4096,
    )
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        content = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()
    content = _strip_reference_echo(content, global_desc)
    content = _enforce_animal_first(content, action)
    content = _enforce_character_line(content, character_line)
    content = _enforce_animal_sound(content, scene_desc)
    content = _enforce_animal_beside(content)
    content = _enforce_attached_hands(content)
    content = _enforce_dialogue_attribution(content, action)
    content = _enforce_feet_only_framing(content, direction)
    content = _enforce_closeup_scale(content, direction)
    content = _strip_offscreen_ground_mentions(content, direction)
    content = _fix_aspect_phrase(content, orientation)

    # Pass4: Prompt Linter — 完成プロンプトを失敗モードチェックリストで検品・修正
    linted = await _lint_ltx_prompt(content, direction, action, orientation, character_line, reserved_props, state)
    if linted and linted != content:
        print("[t2v-tl6]   Pass4 lint: 修正あり")
        content = linted
    # fixer LLMがアスペクト文言を落とすことがあるため、最後にもう一度Pythonで保証。
    # tightショットの床/地面言及も、lintのLLM修正で再混入する可能性があるため同様に再適用
    content = _strip_offscreen_ground_mentions(content, direction)
    content = _fix_aspect_phrase(content, orientation)
    # lintのLLM修正で「She says」等の話者動詞が再度剥がされる可能性があるため同様に再適用(2026-07-12)
    content = _enforce_dialogue_attribution(content, action)
    # C17(衣装)のLLM fixerが従わなかった場合の最終防衛(2026-07-10)
    content = _enforce_garments_present(content, state, direction)
    # 明示的にアニメ/イラスト系を要求していない限り写実系をデフォルトにする最終防衛(2026-07-10、
    # ユーザー指摘: 「明示的にアニメ、イラストと書かれてない限りはリアル系で」)
    return _enforce_realism_default(content, global_desc)


async def _lint_ltx_prompt(ltx_prompt: str, direction: str, action: str, orientation: str, character_line: str, reserved_props: list[str] | None = None, state: str = "") -> str:
    """Pass4: 完成したLTXプロンプトをチェックリストで検品し、違反があれば最小修正して返す。
    個別バグへのルール分散追加・正規表現の増殖を止め、失敗モードの知見はチェックリスト1箇所に集約する(2026-07-04)。
    従来は検出→fix1回で終わり、直りきらなかった違反はログのみで未解消のまま採用していたが、
    実データでC16(HAND BUDGET)が未解消のまま配信される事故が発覚(2026-07-10)。ユーザー提案
    「治るまでloopすれば」を受け、最大`_LINT_MAX_ATTEMPTS`回、毎回まだ残っている違反だけを
    次のfixerに渡して繰り返す(直った分は自然に外れ、fixerの負担が試行ごとに減っていく)。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    a = _ASPECT[orientation]
    a_forbidden = (
        'the words "landscape", "widescreen", "16:9"' if orientation == "vertical"
        else 'the words "portrait", "vertical", "9:16"'
    )

    # 検出フェーズ(Python・決定論的)
    ids = _detect_violations(ltx_prompt, direction, orientation, reserved_props, action, state)
    if not ids:
        return ltx_prompt
    print(f"[t2v-tl6]   Pass4 lint 違反検出: {', '.join(ids)}")

    current = ltx_prompt
    for attempt in range(1, _LINT_MAX_ATTEMPTS + 1):
        user_msg = (
            f"/no_think\n"
            f"## Shot constraint\n"
            f"orientation: {a['label']} ({a['dims']})\n"
            f"camera direction: {direction}\n"
            f"action: {action}\n"
            f"character: {character_line or '(none)'}\n"
            f"state (clothing/hairstyle/lighting source of truth): {state or '(none)'}\n\n"
            f"## Prompt to fix\n{current}"
        )
        # 修正フェーズ(LLM — 生成は得意、判定は不得意という役割分担)
        violated_rules = "\n".join(
            f"{i} " + (_LINT_CHECKS[i].format(frame_phrase=a["frame_phrase"], aspect_forbidden=a_forbidden)
                       if "{" in _LINT_CHECKS[i] else _LINT_CHECKS[i])
            for i in ids if i in _LINT_CHECKS
        )
        fixer_system = _PROMPT_FIXER_SYSTEM.format(violated_rules=violated_rules)
        try:
            resp2 = await client.chat.completions.create(
                model=cfg.LLM_MODEL,
                messages=[
                    {"role": "system", "content": fixer_system},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.4,
                max_tokens=4096,
            )
        except Exception as e:
            print(f"[t2v-tl6]   Pass4 fix スキップ(LLMエラー、試行{attempt}): {e}")
            break
        fixed = (resp2.choices[0].message.content or "").strip()
        if not fixed:
            fixed = (getattr(resp2.choices[0].message, "reasoning_content", None) or "").strip()
        # 修正文の妥当性を軽く検証: 短すぎ・解説混入は破棄(このattemptだけ捨て、currentは維持)。
        # 以前はここでbreakして残り試行を丸ごと放棄していたため、1回運が悪いだけで
        # `_LINT_MAX_ATTEMPTS`回試す前提が崩れていた(2026-07-12修正、continueで次を試す)
        if len(fixed) < 200 or re.match(r"^C\d+\b", fixed):
            print(f"[t2v-tl6]   Pass4 lint 修正結果を破棄(試行{attempt}、短すぎ/解説混入)")
            continue
        remaining = _detect_violations(fixed, direction, orientation, reserved_props, action, state)
        current = fixed
        if not remaining:
            if attempt > 1:
                print(f"[t2v-tl6]   Pass4 lint: 全違反解消(試行{attempt})")
            return current
        if set(remaining) == set(ids):
            # 進展なしでも即打ち切らず、更新後のcurrentを土台に次の試行へ(同上理由で2026-07-12修正)
            print(f"[t2v-tl6]   Pass4 lint: 試行{attempt}で進展なし、次を試行")
        else:
            ids = remaining  # 次の試行では残っている違反だけを渡す

    if ids:
        print(f"[t2v-tl6]   Pass4 lint 未解消(最大{_LINT_MAX_ATTEMPTS}回試行後): {', '.join(ids)}")
    return current


# 「(alongside) closely behind」のように足部位を伴わない後方表現も対象


# 部分文字列誤マッチ対策("catches"の中の"cat"等)。動物判定は必ずこのregexで行う


def _enforce_animal_first(ltx_prompt: str, action: str) -> str:
    """アクションに動物が含まれるのに、LTXプロンプトの冒頭2文に動物が出てこない場合、
    第1文直後に動物の存在文を挿入する。中盤初出だとLTXが動物を生成しないため(2026-07-04)。"""
    animals = _animals_in(action)
    if not animals:
        return ltx_prompt
    animal = animals[0]
    sentences = re.split(r"(?<=[.;])\s+", ltx_prompt)
    head = " ".join(sentences[:2]).lower()
    if animal in head:
        return ltx_prompt
    insert = f"A small {animal} is clearly visible in the foreground of the frame from the very first frame."
    m = re.search(r"[.;]\s", ltx_prompt)
    if m:
        return ltx_prompt[: m.end()] + insert + " " + ltx_prompt[m.end():]
    return insert + " " + ltx_prompt


async def _run_direct(args: argparse.Namespace) -> None:
    """デバッグ用: LLMパイプライン(Pass0〜4)を一切通さず、--fのファイル内容を
    そのままComfyUIに渡してargs.direct秒の動画を1本だけ生成する(2026-07-15新設)。
    prompts.txt(1セグメント、direct: trueヘッダー)を通常runと同じ命名規則で
    書き出すため、Node.jsハーネス側は無改修でこのrunを表示・操作できる。"""
    prompt_path = Path(args.f)
    if not prompt_path.exists():
        print(f"[t2v-tl6] ファイルが見つかりません: {prompt_path}")
        return

    width, height = (720, 1280) if args.vertical else (1280, 720)
    orient_label = "縦 720×1280" if args.vertical else "横 1280×720"
    orientation = "vertical" if args.vertical else "horizontal"
    duration = args.direct

    text = prompt_path.read_text(encoding="utf-8")
    _, main_prompt = _split_direct_prompt(text)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prompts_txt = cfg.GENERATED_DIR / f"t2v6_{ts}_prompts.txt"
    _write_direct_prompts_txt(prompts_txt, prompt_path, orientation, width, height, duration, main_prompt)

    print(f"[t2v-tl6] [DIRECT] {prompt_path.name} / {orient_label} / {duration}s(LLM無し、生テキストをそのまま使用)")
    print(f"[t2v-tl6] プロンプト保存: {prompts_txt.name}")
    print(f"[t2v-tl6] prompt:\n{main_prompt}")

    seg_path = _seg_video_path(ts, 1, "direct", "t2v6")
    print(f"\n[t2v-tl6] 動画生成中...")
    raw_path = await generate_t2v_video(main_prompt, width, height, duration)
    raw_path.rename(seg_path)
    print(f"[t2v-tl6] 保存: {seg_path.name}")

    final = cfg.GENERATED_DIR / f"t2v6_{ts}_final.mp4"
    if _concat_segments([seg_path], final, "[t2v-tl6]"):
        print(f"\n[t2v-tl6] 完了!")
        print(f"[t2v-tl6] 最終動画: {final}")


async def _run_retry(args: argparse.Namespace) -> None:
    """既存runの指定セグメントだけ別seedで再生成し、finalを再連結する。"""
    run_id = args.retry
    # "t2v6_20260704_080510" や "..._prompts.txt" 形式でも受け付ける
    run_id = re.sub(r"^t2v6_", "", run_id)
    run_id = re.sub(r"_prompts\.txt$", "", run_id)

    prompts_path = cfg.GENERATED_DIR / f"t2v6_{run_id}_prompts.txt"
    if not prompts_path.exists():
        print(f"[t2v-tl6] prompts.txt が見つかりません: {prompts_path}")
        return

    try:
        header, segments = _parse_prompts_txt(prompts_path)
    except ValueError as e:
        print(f"[t2v-tl6] パースエラー: {e}")
        return

    # directモード(--directで作ったrun)は常にセグメント1つのみなので、--seg省略時は
    # 自動的に"1"を補う(2026-07-15新設)。通常runは従来通り--seg必須のまま。
    is_direct = header.get("direct") == "true"
    if not args.seg:
        if is_direct:
            args.seg = "1"
            print(f"[t2v-tl6] directモードのrun: --seg省略 → seg 1 を使用")
        else:
            print(f"[t2v-tl6] --retry には --seg が必要です(例: --seg 3,7)")
            return
    elif is_direct and args.seg.strip() != "1":
        print(f"[t2v-tl6] directモードのrunはセグメント1のみです(--seg {args.seg} は無効)")
        return

    # サイズ復元: --h/--vが明示指定されていればそちらを優先し、無指定ならヘッダーへ
    # フォールバックする(2026-07-14、ユーザー要望: 「同じ変換後プロンプトで--v/--hの
    # 違いを見たい、もしくは一方が良かったのでもう一方も試したい」)。以前はヘッダー優先
    # だったため、size記録済みのrunに--vを付けても無視されて元の向きのまま生成されていた。
    # store_trueのデフォルトはFalseなので、args.vertical/args.horizontalがTrueなら
    # 必ずユーザーが明示的に指定した場合に限られる(誤って常時Trueになる心配はない)。
    if args.vertical or args.horizontal:
        width, height = (720, 1280) if args.vertical else (1280, 720)
    else:
        size = header.get("size", "")
        m = re.match(r"^(\d+)x(\d+)$", size)
        if m:
            width, height = int(m.group(1)), int(m.group(2))
        else:
            print("[t2v-tl6] prompts.txt に size ヘッダーがありません(旧形式)。--h または --v を指定してください")
            return

    try:
        targets = sorted({int(x) for x in args.seg.split(",") if x.strip()})
    except ValueError:
        print(f"[t2v-tl6] --seg の形式が不正です: {args.seg}(例: --seg 3,7)")
        return
    seg_by_num = {s["num"]: s for s in segments}
    missing = [n for n in targets if n not in seg_by_num]
    if missing:
        print(f"[t2v-tl6] 存在しないセグメント番号: {missing}(1〜{len(segments)})")
        return

    print(f"[t2v-tl6] リトライ: run={run_id} / {width}x{height} / 対象セグメント: {targets}")

    if args.debug:
        for n in targets:
            seg = seg_by_num[n]
            print(f"\n[t2v-tl6] [{n}/{len(segments)}] {seg['label']} ({seg['duration']}s)")
            print(f"[t2v-tl6] prompt:\n{seg['prompt']}")
        print(f"\n[t2v-tl6] [DEBUG] 完了 — 再生成はスキップされました")
        return

    # 旧takeの退避はファイルI/Oのみで一瞬なので逐次実施(並列生成の前に完了させる必要がある)
    dests: dict[int, Path] = {}
    for n in targets:
        seg = seg_by_num[n]
        dest = _seg_video_path(run_id, n, seg["label"], "t2v6")
        if not dest.exists():
            print(f"[t2v-tl6] 警告: 既存セグメントが見つかりません(新規生成します): {dest.name}")
        else:
            k = 1
            while (backup := dest.with_name(dest.stem + f"_old{k}.mp4")).exists():
                k += 1
            dest.rename(backup)
            print(f"[t2v-tl6] 退避: {dest.name} → {backup.name}")
        dests[n] = dest

    # 動画再生成は保存済みプロンプトの再利用のみでLLM呼び出しが無く、他セグメントにも
    # 依存しないため並列実行する(ComfyUI自体がジョブキューを持つため無制限)
    async def _retry_one(n: int) -> None:
        seg = seg_by_num[n]
        print(f"[t2v-tl6] [{n}/{len(segments)}] {seg['duration']}s 再生成中...")
        raw_path = await generate_t2v_video(seg["prompt"], width, height, seg["duration"])
        raw_path.rename(dests[n])
        print(f"[t2v-tl6] 保存: {dests[n].name}")

    await asyncio.gather(*[_retry_one(n) for n in targets])

    # 全セグメント(再生成分+既存分)を番号順に集めて再連結
    seg_paths: list[Path] = []
    for s in segments:
        p = _seg_video_path(run_id, s["num"], s["label"], "t2v6")
        if not p.exists():
            print(f"[t2v-tl6] エラー: セグメント動画が欠けています: {p.name} — 連結を中止")
            return
        seg_paths.append(p)

    final = cfg.GENERATED_DIR / f"t2v6_{run_id}_final.mp4"
    print(f"\n[t2v-tl6] ffmpegで再連結中 ({len(seg_paths)}クリップ)...")
    if _concat_segments(seg_paths, final, "[t2v-tl6]"):
        print(f"\n[t2v-tl6] 完了! セグメント {targets} を再生成 → final 再連結")
        print(f"[t2v-tl6] 最終動画: {final}")


async def _process_segment_phase1(
    i: int, seg: dict, direction: str, intent: str, total: int,
    global_desc: str, segments: list[dict], ambience: str, orientation: str,
    character_line: str, llm_sem: asyncio.Semaphore, state: str,
) -> dict:
    """フェーズ1: 1セグメント完結のテキスト確定処理(Pass2→Pass3)。動画生成は含まない。
    プロンプトのテキストはLLMパスだけで確定し画像/動画生成の結果に依存しないため、
    `prompts.txt`を動画生成の前に書き出せるようフェーズ2(動画生成)から分離した
    (2026-07-10、ユーザー指摘: テキストが生成前に確定しているなら先に出してほしい)。
    他セグメントの結果に依存しないため、`main()`から`asyncio.gather()`で全セグメント分を
    並列実行できるよう切り出した(2026-07-07)。LLM呼び出し(Pass2/Pass3)は`llm_sem`
    (asyncio.Semaphore、最大4)で同時実行数を絞る。

    `state`はPass0bで確定済みのSTATE文字列を呼び出し元から直接渡す(2026-07-16、JSON経路)。
    `intent`は引き続きPass2の文脈用に"INTENT: ... | STATE: ..."の結合文字列を渡すが、
    Pass3向けの`current_state`はここでは正規表現による再抽出(旧`_extract_state_from_intent`、
    2026-07-16に削除)を経由しない。"""
    print(f"[t2v-tl6] [{i}/{total}] {seg['duration']}s  ({direction})")
    reserved = _reserved_props_for(segments, i - 1)
    print(f"[t2v-tl6]   [{i}/{total}] Pass2: シーン記述中(LLM)...")
    async with llm_sem:
        scene_desc = await _write_scene_description(global_desc, seg["action"], direction, seg["duration"], orientation, _SCENE_WRITER_SYSTEM, intent, reserved)
    # actionの要素脱落・動物不在の物語を検出したらPass2を差し戻す。従来は1回だけ差し戻して
    # 結果を無条件採用していたが、それでは差し戻しの意味がない(2026-07-10、ユーザー指摘:
    # 検証は結果を見て行動して初めて検証と言える)。解消するか`_LINT_MAX_ATTEMPTS`回試すまで繰り返す
    reject = _scene_reject_reason(scene_desc, seg["action"])
    attempt = 1
    while reject and attempt < _LINT_MAX_ATTEMPTS:
        attempt += 1
        print(f"[t2v-tl6]   [{i}/{total}] Pass2 差し戻し(試行{attempt}): {reject}")
        async with llm_sem:
            scene_desc = await _write_scene_description(
                global_desc, seg["action"], direction, seg["duration"], orientation, _SCENE_WRITER_SYSTEM, intent, reserved,
                retry_note=f"{reject}. Every element written in the shot action must be visibly present in the scene from the first frame.")
        reject = _scene_reject_reason(scene_desc, seg["action"])
    if reject:
        print(f"[t2v-tl6]   [{i}/{total}] Pass2 未解消(最大{_LINT_MAX_ATTEMPTS}回試行後): {reject}")
    print(f"[t2v-tl6]   [{i}/{total}] scene:\n{scene_desc}")
    print(f"[t2v-tl6]   [{i}/{total}] Pass3: LTX書式化中(LLM)...")
    current_state = state
    async with llm_sem:
        seg_prompt = await _format_to_ltx_prompt(scene_desc, global_desc, ambience, seg["duration"], orientation, direction, character_line, seg["action"], _reserved_props_for(segments, i - 1), current_state)
    print(f"[t2v-tl6]   [{i}/{total}] prompt:\n{seg_prompt}")

    prompt_block = (
        f"[{i}/{total}] {seg['label']} ({seg['duration']}s)\n"
        f"Intent: {intent}\n"
        f"Camera: {direction}\n"
        f"--- Scene ---\n{scene_desc}\n"
        f"--- LTX prompt ---\n{seg_prompt}\n"
    )
    return {"seg": seg, "num": i, "prompt": seg_prompt, "prompt_block": prompt_block}


async def _process_segment_phase2(item: dict, total: int, width: int, height: int, ts: str) -> Path:
    """フェーズ2: 1セグメント完結の動画生成処理。ComfyUI自体がジョブキューを持つため、
    無制限に並列実行する(2026-07-10、テキスト確定フェーズから分離)。"""
    seg, i = item["seg"], item["num"]
    print(f"[t2v-tl6]   [{i}/{total}] 動画生成中...")
    raw_path = await generate_t2v_video(item["prompt"], width, height, seg["duration"])
    named = _seg_video_path(ts, i, seg["label"], "t2v6")
    raw_path.rename(named)
    print(f"[t2v-tl6]   [{i}/{total}] 保存: {named.name}")
    return named


async def main() -> None:
    start_time = time.monotonic()
    parser = argparse.ArgumentParser(description="T2V タイムライン分割生成CLI V2(Creative Directorパス+カメラモーション+二次的な動き)")
    orient = parser.add_mutually_exclusive_group()
    orient.add_argument("--h", action="store_true", dest="horizontal", help="横向き 1280×720(デフォルト)")
    orient.add_argument("--v", action="store_true", dest="vertical",   help="縦向き 720×1280")
    parser.add_argument("--f", metavar="FILE.txt", help="プロンプトファイル(通常実行時必須)")
    parser.add_argument("--debug", action="store_true", help="プロンプト生成のみ、動画生成をスキップ")
    parser.add_argument("--retry", metavar="RUN_ID", help="既存runのセグメントを再生成(例: --retry 20260704_080510)。--seg 必須(directモードのrunは省略可)、--f とは排他")
    parser.add_argument("--seg", metavar="N[,N...]", help="--retry で再生成するセグメント番号(1始まり、カンマ区切り)")
    parser.add_argument("--direct", metavar="SECONDS", type=float,
                         help="デバッグ用: Pass0〜4のLLMパイプラインを一切通さず、--fのファイル内容を"
                              "そのままComfyUIに渡してSECONDS秒の動画を1本だけ生成する。"
                              "--retry / --seg / --upscale / --debug とは排他")
    parser.add_argument("--upscale", metavar="RUN_ID", nargs="?", const="",
                         help="既存runの最終動画(_final.mp4)をRTX Video Super ResolutionでフルHDにアップスケール。"
                              "RUN_ID省略で直近run(例: --upscale / --upscale 20260704_080510)。他の引数とは排他")
    args = parser.parse_args()

    if args.direct is not None:
        if args.retry or args.seg or args.upscale is not None or args.debug:
            parser.error("--direct は --retry / --seg / --upscale / --debug と同時に指定できません")
        if not args.f:
            parser.error("--direct には --f が必要です")
        await _run_direct(args)
        print(f"[t2v-tl6] 経過時間: {_fmt_elapsed(start_time)}\n")
        return

    if args.upscale is not None:
        if args.f or args.retry or args.seg:
            parser.error("--upscale は --f / --retry / --seg と同時に指定できません")
        await _run_upscale("t2v6", args.upscale or None, "[t2v-tl6]")
        print(f"[t2v-tl6] 経過時間: {_fmt_elapsed(start_time)}\n")
        return

    if args.seg and not args.retry:
        # --retry 省略時は直近のrunを使用
        cands = list(cfg.GENERATED_DIR.glob("t2v6_*_prompts.txt"))
        if not cands:
            parser.error("generated/ に t2v6_*_prompts.txt が見つかりません(--retry RUN_ID で指定してください)")
        latest = max(cands, key=lambda p: p.stat().st_mtime)
        args.retry = latest.name
        print(f"[t2v-tl6] --retry 省略: 直近のrunを使用 → {latest.name}")

    if args.retry:
        if args.f:
            parser.error("--retry と --f は同時に指定できません")
        # --seg必須チェックはここでは行わない(directモードのrunは省略可のため)。
        # _run_retry()がprompts.txtヘッダーを読んでから判定する。
        await _run_retry(args)
        print(f"[t2v-tl6] 経過時間: {_fmt_elapsed(start_time)}\n")
        return
    if not args.f:
        parser.error("--f が必要です(リトライは --retry RUN_ID --seg N、または --seg N のみで直近run)")

    width, height = (720, 1280) if args.vertical else (1280, 720)
    orient_label  = "縦 720×1280" if args.vertical else "横 1280×720"
    orientation   = "vertical" if args.vertical else "horizontal"
    debug         = args.debug

    prompt_path = Path(args.f)
    if not prompt_path.exists():
        print(f"[t2v-tl6] ファイルが見つかりません: {prompt_path}")
        return

    text = prompt_path.read_text(encoding="utf-8")

    auto_segmented = False
    try:
        global_desc, segments, ambience = _parse_prompt(text)
    except ValueError as e:
        if "ヘッダーもタイムスタンプも見つかりません" not in str(e):
            print(f"[t2v-tl6] パースエラー: {e}")
            return
        print(f"[t2v-tl6] タイムライン形式が見つかりません — Pass -1で自動分割します(目安15秒、3秒ビート基本)...")
        text = await _auto_segment_narrative(text)
        print(f"[t2v-tl6] 自動分割結果:\n{text}\n")
        try:
            global_desc, segments, ambience = _parse_prompt(text)
        except ValueError as e2:
            print(f"[t2v-tl6] 自動分割後もパースエラー: {e2}")
            return
        auto_segmented = True

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = cfg.GENERATED_DIR
    prompts_txt = out_dir / f"t2v6_{ts}_prompts.txt"

    mode_label = " [DEBUG — プロンプト確認のみ]" if debug else ""
    print(f"[t2v-tl6] {prompt_path.name} / {orient_label} / セグメント数: {len(segments)} / 合計尺: {segments[-1]['end']}秒{mode_label}")

    # キャラクター記述の抽出(全セグメントのLTXプロンプトに強制挿入するため)。
    # 髪型・メガネも固定アイデンティティとしてここに含まれる(2026-07-10、V6。
    # STATE側の部分更新でこの2つが丸ごと落ちる事故が起きたため、STATEに個別の補完
    # 機構を作るのではなく、既に確実な_enforce_character_lineの強制注入に一本化した)
    print(f"\n[t2v-tl6] キャラクター記述を抽出中(LLM)...")
    character_line = await _extract_character_line(global_desc)
    print(f"[t2v-tl6]   character: {character_line or '(なし)'}")

    # Pass0a/0b: 創造(INTENT/HIGHLIGHT/TEMPO)と継続性記録(STATE)を専属パスに分けて並列実行
    # (2026-07-10、V6。互いに依存しないため並列でもレイテンシはほぼ変わらない)。
    # JSON構造化出力版(2026-07-16): segment番号を明示させ、位置依存の誤帰属を排除する
    print(f"\n[t2v-tl6] Pass0a/0b クリエイティブディレクター+Stateトラッカー: 並列計画中({len(segments)}セグメント)...")
    creative_json, state_json = await asyncio.gather(
        _plan_creative_intent_json(global_desc, segments, orientation, _CREATIVE_DIRECTOR_SYSTEM_JSON),
        _plan_creative_intent_json(global_desc, segments, orientation, _STATE_TRACKER_SYSTEM_JSON),
    )
    creative_lines = [f"INTENT: {d['intent']} | HIGHLIGHT: {d['highlight']} | TEMPO: {d['tempo']}" for d in creative_json]
    state_lines_raw = [d["state"] for d in state_json]
    resolved_state_dicts = _resolve_state_continuity_json(state_lines_raw)
    resolved_states = [_flatten_state(d) for d in resolved_state_dicts]
    intents = [f"{c} | STATE: {s}" for c, s in zip(creative_lines, resolved_states)]
    for i, (seg, intent) in enumerate(zip(segments, intents), 1):
        print(f"[t2v-tl6]   [{i:02d}] {seg['label']:>14}  {intent}")
    print()

    # Pass1: ショットディレクターが構図を一括計画(intentを参照)
    print(f"[t2v-tl6] Pass1 ショットディレクター: 構図を計画中...")
    shot_directions = await _plan_shot_directions_json(global_desc, segments, orientation, intents, _SHOT_DIRECTOR_SYSTEM_JSON)
    for i, (seg, direction) in enumerate(zip(segments, shot_directions), 1):
        print(f"[t2v-tl6]   [{i:02d}] {seg['label']:>14}  {direction}")
    print()

    # Pass1.5: 多様性監査(向き・カメラ位置・接近の単調さを俯瞰チェックして書き直し)
    print(f"[t2v-tl6] Pass1.5 多様性監査: 向き・カメラ位置をチェック中...")
    audited = await _audit_shot_variety_json(segments, shot_directions, orientation, _VARIETY_AUDITOR_SYSTEM_JSON)
    audited = _enforce_walking_lateral(segments, audited, orientation, "[t2v-tl6]")
    for i, (before, after) in enumerate(zip(shot_directions, audited), 1):
        mark = "＊" if after != before else " "
        print(f"[t2v-tl6]  {mark}[{i:02d}] {after}")
    shot_directions = audited
    print()

    # 各セグメントのテキスト確定(Pass2→Pass3)は他セグメントの結果に依存しないため、
    # asyncio.gather()で並列実行する(2026-07-07)。gather()は入力順序と同じ順序で結果を
    # 返すため、セグメント番号の順序は実行完了順に関わらず保たれる。LLM呼び出しはSemaphore(4)
    # で同時実行数を制限する。動画生成(ComfyUI)はテキスト確定後のフェーズ2に分離した
    # (2026-07-10、prompts.txtを動画生成の前に書き出すため)。
    llm_sem = asyncio.Semaphore(4)
    phase1_tasks = [
        _process_segment_phase1(i, seg, direction, intent, len(segments), global_desc, segments,
                                 ambience, orientation, character_line, llm_sem, state)
        for i, (seg, direction, intent, state) in enumerate(zip(segments, shot_directions, intents, resolved_states), 1)
    ]
    phase1_results = await asyncio.gather(*phase1_tasks)

    prompt_lines: list[str] = [
        f"source: {prompt_path.name}",
        f"orientation: {orientation}",
        f"size: {width}x{height}",
        f"auto_segmented: {str(auto_segmented).lower()}",
        "",
    ]
    for r in phase1_results:
        prompt_lines.append(r["prompt_block"])

    # prompts.txt はテキストが確定した時点(動画生成の前)で書き出す
    prompts_txt.write_text("\n".join(prompt_lines), encoding="utf-8")
    print(f"\n[t2v-tl6] プロンプト保存: {prompts_txt.name}")

    llm_done_time = time.monotonic()

    if debug:
        print(f"\n[t2v-tl6] [DEBUG] 完了 — 動画生成はスキップされました")
        print(f"[t2v-tl6] プロンプト確認: {prompts_txt}")
        print(f"[t2v-tl6] LLM時間: {_fmt_duration(llm_done_time - start_time)}\n")
        return

    # ===== フェーズ2: 全セグメントの動画生成 =====
    phase2_tasks = [_process_segment_phase2(r, len(phase1_results), width, height, ts) for r in phase1_results]
    seg_paths: list[Path] = list(await asyncio.gather(*phase2_tasks))

    # ffmpeg concat
    print(f"\n[t2v-tl6] ffmpegで連結中 ({len(seg_paths)}クリップ)...")
    final = out_dir / f"t2v6_{ts}_final.mp4"
    if not _concat_segments(seg_paths, final, "[t2v-tl6]"):
        return

    print(f"\n[t2v-tl6] 完了!")
    print(f"[t2v-tl6] 最終動画: {final}")
    print(f"[t2v-tl6] セグメント({len(seg_paths)}個): generated/t2v6_{ts}_seg*.mp4")
    finish_time = time.monotonic()
    print(f"[t2v-tl6] LLM時間: {_fmt_duration(llm_done_time - start_time)} / "
          f"生成時間: {_fmt_duration(finish_time - llm_done_time)} / "
          f"合計時間: {_fmt_duration(finish_time - start_time)}\n")


if __name__ == "__main__":
    asyncio.run(main())
