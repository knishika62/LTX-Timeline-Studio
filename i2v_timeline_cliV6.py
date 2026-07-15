"""キーフレームI2V方式のタイムライン生成CLI(V6、2026-07-10)。
V5(backup/i2v_timeline_cliV5.py)との違い: Pass0を「創造(INTENT/HIGHLIGHT/TEMPO)」と
「継続性記録(STATE)」の2つの専属LLM呼び出しに分割した。V5まではPass0が1回の呼び出しで
両方を担っており、実データで「STATEだけが省略(Same as N)・空欄(None mentioned)という
形で壊れ、衣装や時間帯の継続性が崩れる」事故が繰り返し発覚した。原因は性質の異なる
タスク(創造的な演出判断と、地味な継続性の転記作業)を1回の呼び出しに同居させていたこと
(ユーザーの過去の実体験——LTX書式変換+ローマ字変換を同時にやらせたら不安定だったが
2パスに分けたら確実に動いた——と同型の問題と判明)。詳細はt2v_timeline_cliV6.pyのdocstring
参照(t2v/i2v共通のPass0再設計)。V5は`backup/`へ凍結。出力prefixは i2v6_。

キーフレームI2V方式のタイムライン生成CLI(元の説明、無印版から継承):
t2v_timeline_cliV2(T2V直接生成)との違い:
  - 各シーンの1stフレーム(キーフレーム)をZ-Image(image.json)で先に生成し、LTX-2.3のI2Vで動かす
  - Pass3をKeyframe Formatter(静止画プロンプト、見た目・構図の全情報)と
    Motion Formatter(動きのみ。LTX公式I2V原則「画像に見えている静的要素は記述しない」)に分割
  - キーフレームのキャラLoRAは .env の KEYFRAME_LORA_NAME / KEYFRAME_LORA_STRENGTH で可変
    (未設定=image.jsonのzib-hinaのまま、STRENGTH=0でLoRA無効=任意キャラ)
  - 全キーフレームで同一seedを使用(LoRAなし時のキャラ一貫性向上)
"""

from __future__ import annotations

import argparse
import asyncio
import random
import subprocess
import re
import time
from datetime import datetime
from pathlib import Path

from openai import AsyncOpenAI

import pipeline_config as cfg
import prompt_generator
from comfyui_client import generate_image, generate_t2v_video, generate_video_10e, generate_video_refine_ltx23, upload_image_to_comfyui
from timeline_common import (
    _fmt_elapsed,
    _fmt_duration,
    _parse_prompt, _parse_numbered_lines, _seg_video_path, _concat_segments, _run_upscale,
    _split_direct_prompt, _write_direct_prompts_txt,
    _auto_segment_narrative, _LINT_MAX_ATTEMPTS,
    _has_word, _animals_in, _stem, _ANIMAL_ABSENT_RE,
    _ANIMAL_BEHIND_RE, _ANIMAL_BEHIND_LOOSE_RE, _enforce_animal_beside,
    _AUDIO_HEADER_RE, _enforce_animal_sound,
    _HANDS_ENTER_RE, _enforce_attached_hands,
    _enforce_dialogue_attribution,
    _HAND_PROPS, _BOTH_HANDS_RE, _hand_budget_violation,
    _RESERVED_PROP_KEYWORDS, _reserved_props_for,
    _ACTION_STOPWORDS, _ACTION_SYNONYMS, _missing_action_elements, _scene_reject_reason,
    _strip_reference_echo,
    _enforce_feet_only_framing, _enforce_closeup_scale, _strip_offscreen_ground_mentions,
    _is_tight_scale,
    _WALK_KEYWORDS, _WALK_DIRECTION_RE, _LATERAL_RE, _enforce_walking_lateral, _enforce_shot_rules,
    _CHAR_STOPWORDS, _FOOTWEAR_ITEMS, _LOWER_BODY_ITEMS,
    _extract_character_line,
    _resolve_state_continuity,
    _trim_character_for_scale,
    _GARMENT_WORD_RE, _garments_missing, _state_details_missing, _enforce_garments_present,
    _enforce_realism_default, _ANIME_STYLE_RE, _REALISM_HINT_RE,
    _GENDER_WORDS, _GENDER_SYNONYMS, _AGE_PATTERN_RE, _character_tokens_missing,
    _extract_state_from_intent,
    _plan_creative_intent, _plan_shot_directions, _audit_shot_variety, _write_scene_description,
)
import t2v_timeline_cliV6 as _t2v_pass3


_CREATIVE_DIRECTOR_SYSTEM = """\
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

Output: ONLY a numbered list, one line per segment, nothing else. Format exactly:
1. INTENT: ... | HIGHLIGHT: ... | TEMPO: calm
2. INTENT: ... | HIGHLIGHT: ... | TEMPO: lively
...\
"""

_STATE_TRACKER_SYSTEM = """\
You are a continuity tracker for a short-video timeline. This is your ONLY job — you do not judge creative quality, only track facts.

For EACH segment, decide whether the scene's persistent LOCATION (name the actual place, e.g. "residential alley", "kitchen"), time-of-day/lighting, weather, or ANYTHING about the subject's current appearance beyond her fixed identity (outfit, accessories, hairstyle, or anything else she is currently wearing/carrying/styled with) is DIFFERENT from the immediately preceding segment.

Output for each segment EXACTLY ONE of the following two things — nothing else is valid:
(a) The literal text "NO CHANGE" — use this if and only if location, time-of-day/lighting, weather, and appearance are ALL identical to the previous segment.
(b) A COMPLETE description covering location, time-of-day/lighting, weather, and everything about her current appearance — required for segment 1 (establish the baseline from the character/location/style reference), and required for any segment where the action explicitly changes any of these (e.g. "goes to sleep" → night; "changes into pajamas" → new outfit; "steps outside into the rain" → weather changes; "walks into the cafe" → location changes).

STRICT RULES:
- NEVER write a partial description, a reference ("same as segment 5", "same as before"), an abbreviation, or a placeholder ("none mentioned") — only options (a) or (b) above exist. This also includes paraphrased "nothing changed" sentences like "Same location and time; same outfit and accessories" — that is NOT option (b), it names no actual garment/location words and is just option (a) in disguise. If nothing changed, write the literal text "NO CHANGE" and nothing else.
- When you write a full description (b), restate EVERYTHING that is still true, not just what changed — the next segment's "NO CHANGE" depends on this being complete.
- This matters most for changes that are easy to miss: once night falls, every later segment stays night until something explicitly says otherwise (sunrise, an alarm, etc.) — the same for weather, for a change of clothes, and for location (a tight/close-up shot still happens somewhere — always name it, even when the shot itself barely shows background).
- ⚠️ GET THE TIMING RIGHT: if a segment's OWN action contains the change (e.g. segment 4 says "goes to sleep"), the NEW state applies STARTING AT segment 4 itself — never delay it to segment 5.
- ⚠️ ONE-DIRECTIONAL: the rule above only forbids DELAYING a stated change to a later segment than the one whose action actually describes it. It does NOT mean applying the change EARLIER. You can see every segment's action at once in the list below — a later segment's action may already describe a new outfit, location, or time-of-day. Do NOT anticipate it. Every segment BEFORE the one whose action states the change must still carry the OLD state (or "NO CHANGE"), even though the future change is visible to you in the list.

Output: ONLY a numbered list, one line per segment, nothing else. Format exactly:
1. Traditional Korean street market during light rain, early evening, oversized yellow raincoat over casual outfit, black hair in a low ponytail, clear umbrella.
2. NO CHANGE
3. NO CHANGE
4. Kitchen, night; loose ribbed tank top, cotton shorts, hair undone, glasses on, barefoot.
...\
"""

_SHOT_DIRECTOR_SYSTEM = """\
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

### GEOMETRY RULE — SOMETHING ABOVE RAISED-ARM HEIGHT MUST BE IN FRAME
Whenever the shot must show anything located ABOVE the subject's raised-arm height (an overhead line, a high shelf, a tree branch, a sign she looks up at — whatever the action), the geometry dictates the camera:
⚠️ ONE-DIRECTIONAL: this rule triggers only when the ACTION involves something overhead. The REVERSE is false — choosing a low angle does NOT mean there must be an overhead object or raised arms. A low-angle shot of someone crouching or standing simply views them from below; arms stay wherever the action puts them.
→ LOW ANGLE shooting UPWARD: camera below waist height, angled up so both the subject and the overhead thing fit in frame
→ The vertical span fills the frame (the overhead thing at the TOP, her feet/lower body at the BOTTOM)
→ Do NOT use side-on eye-level (cuts off the overhead thing). Do NOT go close-up (cuts off the reach)
→ If she ADDS an item to a surface that holds a collection (a line, a shelf, a rack), the surface already visibly holds similar items — never an empty surface

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

Output: ONLY a numbered list, one line per segment, nothing else. Format exactly:
1. [full camera direction description]
2. [full camera direction description]
...\
"""

_VARIETY_AUDITOR_SYSTEM = """\
You are a shot-variety auditor reviewing a complete shot list before filming.
Input: numbered camera directions for consecutive segments of one short film.

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
For stationary actions (touching, feeding, sipping, working at an overhead target) change the CAMERA's side (profile, over-shoulder, behind at an angle) rather than the subject's pose.

Output: ONLY the full numbered list, one line per segment, same format as the input, nothing else.\
"""

# 失敗モードチェックリスト(実生成で観測した事故の集約、Pass4 Linterが使用)。
# 全項目を一括で渡すとローカルLLMの判定精度が落ちるため、
# _build_lint_checklist() がセグメントに該当する項目だけを選んで渡す。
_LINT_CHECKS: dict[str, str] = {
    "C1": 'SINGLE SUBJECT: the subject is described at ONE position with ONE direction of movement — never small AND large, or at two frame positions, in the same prompt.',
    "C2": 'SINGLE OBJECT: an object the subject holds (cup, umbrella, fabric, phone) exists ONLY in her hands — never additionally as a separate foreground element ("the cup\'s rim dominates the foreground" + "she lifts the cup" = TWO cups get generated).',
    "C3": 'ANIMAL FIRST: the animal MUST be named with size and frame position within the FIRST TWO sentences, placed beside/ahead of the subject in clear view (never hidden behind heels/legs), with its OWN explicit fur color/pattern (otherwise it inherits the human\'s hair color/texture), and its sound must be first in the audio. The animal is ALREADY PRESENT for the whole shot — rewrite any "waiting for the cat" / "where the cat will appear" / "approaching cat" narrative so the animal is in frame eating/sitting/walking from the first frame. Late-introduced or not-yet-arrived animals are not generated.',
    "C4": 'ATTACHED HANDS: hands/arms always belong to the visible subject — "her hand reaches down", NEVER "a hand enters the frame" (an entering hand generates a third person\'s hand).',
    "C5": 'CHARACTER SCALED: the character description must match the framing — close-up/bust: NO footwear (shoes pull the framing to full body); her waist garment (pants/skirt) IS briefly named as insurance against a wider-than-planned frame; feet-only: ONLY lower-body garments; wide: full description.',
    "C6": 'ASPECT: the exact phrase "{frame_phrase}" appears; {aspect_forbidden} and "DV camcorder"/"4:3" must NOT appear.',
    "C7": 'STEAM: steam/mist is allowed only if its source visibly fills a large part of the frame (bath, large pot, tight close-up on the cup); otherwise the steam must be removed.',
    "C8": 'HAIR: hair-touching = smoothing/patting an ALREADY-TIED ponytail that stays attached; never fingers tangling/gathering/pulling loose strands.',
    "C9": 'SCALE LOCK: the shot scale and camera motion from the constraint are unchanged (close-up stays close-up; feet-only shows no face/torso; static stays static).',
    "C11": 'FACING: the subject\'s facing relative to camera (frontal / three-quarter / profile / from behind) is stated explicitly, matching the constraint.',
    "C12": 'RESERVED PROPS: props that are the centerpiece of another segment\'s action must NOT appear in this shot at all. Remove every mention of them and fill the space with other elements already present in this scene\'s location.',
    "C14": 'ACTION FIDELITY: every element written in the timeline action (see "action" in the constraint) must be visibly present in the prompt — the key action verbs, held objects, companions/animals, glances, and spoken words (quoted dialogue verbatim). Add each missing element naturally without changing shot scale or camera motion.',
    "C15": 'LATERAL CONSISTENCY: the prompt mixes two incompatible lateral shot types. Choose exactly ONE and rewrite: TRACKING alongside = the camera moves parallel with her, she stays CENTERED at constant size in FULL SIDE PROFILE (body perpendicular to the camera) while the background scrolls past, and she NEVER enters or exits the frame edges. CROSSING = the camera holds fixed and she walks in FULL SIDE PROFILE from one edge to the other. Remove all wording of the other type.',
    "C16": 'HAND BUDGET: the character has exactly TWO hands, and a persistent prop (umbrella/bag/phone) already occupies one of them. Rewrite so no third hand is needed: make the other action ONE-HANDED ("cradles the cup in her free hand") or explicitly free the hand ("the closed umbrella tucked under her arm" / "resting against her shoulder"). Remove every "with both hands" that conflicts with the held prop — the model generates a third arm otherwise.',
    "C19": 'UNGROUNDED SECONDARY MOTION: this prompt mentions an animal, or laundry/a clothesline, that the timeline action for THIS segment never calls for. These are hallucinated additions copied from the Scene Writer\'s own illustrative examples, not something that actually happens in this shot. Remove them and replace with a neutral environmental detail (light, shadow, breeze, an already-present background element) instead.',
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


# C12検出用: プロンプト内でそのプロップとみなす語
_RESERVED_PROP_DETECT: dict[str, tuple[str, ...]] = {
    "laundry": ("laundry", "clothesline", "hanging shirts", "hanging garments", "white sheet", "drying towels"),
}


# C2: 持ち物が前景の独立要素として書かれているパターン
_HELD_OBJECTS = ("cup", "mug", "umbrella", "phone", "bottle", "fabric", "garment")


def _detect_violations(ltx_prompt: str, direction: str, orientation: str, reserved_props: list[str] | None = None, action: str = "") -> list[str]:
    """完成プロンプトの違反を決定論的に検出する(判定をLLMに任せると誤検出が多いためPythonで行う)。
    検出した違反の修正のみLLM(_PROMPT_FIXER_SYSTEM)に任せる。
    ※C6(アスペクト文言)はi2v版では不要(画像サイズはgenerate_imageのAPI引数で確定するため検出しない)。"""
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
    if is_tight and any(_has_word(p, item) for item in _FOOTWEAR_ITEMS):
        ids.append("C5")

    # C3: 動物が「まだ登場していない」物語(waiting for / will appear) → 動物が生成されない
    if _ANIMAL_ABSENT_RE.search(ltx_prompt):
        ids.append("C3")

    # C14: タイムラインactionの要素(視線・セリフ・持ち物・動物等)がプロンプトから脱落
    if action and _missing_action_elements(ltx_prompt, action):
        ids.append("C14")

    # C12: 他セグメントの主役プロップが背景に混入している
    for prop in (reserved_props or []):
        if any(_has_word(p, k) for k in _RESERVED_PROP_DETECT.get(prop, ())):
            ids.append("C12")
            break

    # C16: 手の本数超過(傘で片手が塞がっているのに「both hands」) → 3本目の腕が生成される
    if _hand_budget_violation(ltx_prompt):
        ids.append("C16")

    # C19: Scene Writer/Motion FormatterのSECONDARY MOTION指示が挙げる具体例(動物・洗濯物)が、
    # このセグメントの実際のactionに無いのに出現している(2026-07-10、t2vと同じ修正)
    if action:
        if _animals_in(ltx_prompt) and not _animals_in(action):
            ids.append("C19")
        elif any(_has_word(p, kw) for kw in _RESERVED_PROP_DETECT["laundry"]) and \
                not any(_has_word(action.lower(), kw) for kw in _RESERVED_PROP_DETECT["laundry"]):
            ids.append("C19")

    return ids


_PROMPT_FIXER_SYSTEM = """\
You are an LTX-2.3 prompt fixer. You receive a prompt and a list of specific violations.
Rewrite the prompt so that every violation is fixed. Change ONLY what the violations require; preserve everything else — wording, style keywords, character sentence, audio description, one flowing paragraph, and roughly the SAME length as the original (do not expand it).

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
6. STATIONARY TASKS (working at an overhead target, touching face, drinking): Subject is at a FIXED lateral position — feet planted, subject does NOT move sideways along any surface. Only specific limbs move.
7. CAMERA MOTION (if the direction specifies push-in / pull-back / drift / tracking): describe how the framing changes over the shot because of the camera itself.

SECONDARY MOTION — MANDATORY:
Include exactly 1-2 small environmental motions appropriate to this scene so the frame feels alive,
drawn ONLY from elements THIS scene's action or location already establishes: a breeze moving her hair
or fabric/foliage already present, shifting light or shadows, a small movement FROM AN ANIMAL OR PERSON
ONLY IF one is already part of this shot's action, or a vehicle/pedestrian in the far background ONLY IF
an outdoor street is already part of this scene's location.
Rules: pick ONLY from things that exist in THIS scene's location. Keep them SUBTLE, in the BACKGROUND or on already-present subjects. NEVER introduce a new person, animal, or object that the location does not have — do not invent one just to satisfy this requirement.

⚠️ STEAM/MIST RULE: steam is allowed ONLY when its source occupies a LARGE area of the frame — a hot spring, bath, large cooking pot, or a tight close-up where the cup/bowl itself fills much of the frame. NEVER describe steam from a small, distant, or off-screen source (e.g. a coffee cup in a medium shot) — the model will emit steam from the whole scene instead of the cup. If the shot is not tight enough, omit the steam entirely.

⚠️ TIGHT SHOT = NO FOOTWEAR: in a close-up or bust shot, never mention shoes/footwear anywhere — they pull the framing out to full body or place a shoe where the subject should be. Her waist garment (pants/skirt) IS still named briefly: if the model frames wider than planned, an undescribed lower body renders as underwear.

⚠️ RESERVED PROPS: if the user message lists "props reserved for other segments", those props must NOT appear in this shot at all — not even as background decoration. Fill the space with other elements already present in this scene's location.

⚠️ HAND BUDGET RULE: the character has exactly TWO hands, and THIS SEGMENT'S ACTION has first claim on them. If a persistent prop (umbrella, bag, phone) occupies one hand, every other action must be ONE-HANDED — never "with both hands". Either write the action one-handed ("cradles the cup in her free hand") or explicitly free the hand first ("the closed umbrella tucked under her arm"). Demanding a third hand makes the model GENERATE a third arm. The same applies to feet: never describe a pose that needs a third leg.
  CARRIED ITEMS from the character reference (e.g. shoes carried in a hand) are a DEFAULT state, not part of her body: include them ONLY when this segment's action leaves that hand free — if the action uses her hands, the carried item is simply absent from this shot.

⚠️ HELD OBJECT RULE: an object the subject holds (cup, umbrella, phone, food) exists ONLY in their hands. NEVER describe it — or a part of it like "the cup's rim" — as a separate foreground element dominating the frame while the subject also holds it. Describing the same object at two positions makes the model generate TWO of it (a giant standalone cup in front + another in her hands).

⚠️ HAIR-TOUCHING RULE: when the subject fixes or adjusts hair, hands SMOOTH, PAT, or TUCK hair that stays attached to the head; palms glide along the surface of an ALREADY-TIED ponytail. NEVER describe fingers tangling in strands, gathering loose hair, pulling hair, or creating a ponytail from loose hair — the model renders this as hair detaching or being pulled out. Keep part of the face visible for context.

⚠️ ANIMAL VISIBILITY RULE: if an animal must appear in the shot, it is VISIBLE from the very first frame — positioned AHEAD OF or BESIDE the subject in clear view, described within the first two sentences with explicit size and frame position. NEVER place the animal hidden behind legs/heels or introduce it mid-shot; it will simply not be generated. EVEN IF the action says the animal "follows", render it walking BESIDE or AHEAD OF the feet where the camera can see it. NEVER write a scene where the animal has not arrived yet ("where the cat will appear", "waiting for the cat", "the approaching cat") — the animal is ALREADY in the FRAME LAYOUT, eating/sitting/walking, for the entire shot.

CRITICAL FOR STATIONARY TASKS — describe the PHYSICAL ENVIRONMENT SETUP FIRST:
- GEOMETRY (ONE-DIRECTIONAL — applies only when the ACTION involves something overhead; a low camera angle by itself does NOT imply an overhead object or raised arms): anything the shot must show that sits ABOVE the subject's raised-arm height (line, shelf, branch, sign):
  → FIRST: camera low (below the subject's waist), angled upward; the overhead thing at the TOP of the frame, her feet/lower legs at the BOTTOM.
  → SECOND: subject stands DIRECTLY UNDERNEATH it, feet planted; she does NOT move along it.
  → THEN: her arms/gaze extend up toward it at the top of the frame.
- "touching face/hair" = camera is CLOSE on the contact point; subject's full body is NOT visible; describe the hands' position relative to the head/hair
- "feeding/petting animal" = animal is PRIMARY SUBJECT: describe the animal first (position in frame, which body part is visible, how large it appears) and give the animal its OWN explicit fur color and pattern (e.g. "a short-haired brown tabby cat with white paws") — without this the model copies the human's hair color/texture onto the animal. Camera is at animal's eye level or lower. The animal occupies the center or foreground of the frame IN SHARP FOCUS. Her hand, VISIBLY CONNECTED to her arm and body, reaches toward the animal. The human face may appear partially in the background, OUT OF FOCUS.
  ⚠️ NEVER write "a hand enters the frame" while any part of the subject is visible in the shot — a hand "entering" implies an off-screen owner and the model generates a THIRD PERSON's hand. Hands belong to the visible subject: write "her hand reaches down", never "her hand enters".
- ADDING TO A COLLECTION (pinning to a line, placing on a shelf/rack): the surface ALREADY visibly holds similar items at the START of the shot; the subject adds one more — never an empty line/shelf.

Write 150–200 words in plain English. No prompt engineering language.\
"""

_KEYFRAME_FORMATTER_SYSTEM_BASE = """\
You receive an ALREADY-VERIFIED shot description: a longer passage where the character's identity, her
current outfit/accessories/hairstyle, the environment, and the camera work have all been confirmed correct
for this shot (checked against the established continuity of the whole video). Treat every detail already
stated in it as GROUND TRUTH — do not invent new ones, but do not drop or compress away any of them either
(especially the outfit/appearance details — they are easy to lose when shortening).

Your job: distill it into a NATURAL photo caption for a text-to-image model, in the style a skilled HUMAN
user writes them — not a camera direction sheet. Unnatural protocol language produces broken images.
Describe ONLY the FIRST INSTANT of the shot — drop the motion/camera-movement/audio parts of the source,
they belong to a separate video-motion prompt, not this still image.

OUTPUT: ONE paragraph, 40-80 WORDS MAXIMUM:
1. Open with the shot in plain photography terms ("Low-angle close-up photo of ...", "Wide shot of ...")
2. Describe the subject EXACTLY ONCE: weave the given character details, her pose, her facing and what her hands are doing into one natural flow
3. Surroundings in 1-2 natural clauses, then the light in a few words. ⚠️ Name the actual PLACE (e.g. "a residential alley", "a kitchen counter") — a stray prop or secondary-motion detail alone ("a potted plant", "a breeze") does NOT establish where this is, and without a real place name the model defaults to a generic indoor guess even for an outdoor scene. This still applies in tight/close-up/tracking shots — a single place-naming word fits even a short background clause.
4. End with this exact style tail: "{style_tail}"

⚠️ CURRENT STATE OVERRIDES THE REFERENCE: the "Character & Style reference" describes the DEFAULT baseline for the whole video (e.g. it might say "bright morning light" as the general premise). If a "Current established state" block is given below, THAT is the ground truth for time-of-day/lighting and weather for THIS specific shot — it may differ from the reference's default (e.g. a later shot at night). Always follow the current state, never the reference's default, when they conflict. Do not copy time-of-day or weather phrases out of the reference if they contradict the current state or this shot's scene description.

FORBIDDEN — out-of-distribution for image models, they cause broken images:
- cinematography protocol: "enters the frame", "off-camera", "off-screen", "anchors the left/right side", "at the top edge", "waist-down visible", "framed from X to Y", "the main character is"
- motion verbs (turning, entering, hovering, reaching as an act): use static pose wording instead ("seated", "crouched", "mid-stride", "arm raised toward the line")
- video terms: grain, compression artifacts, autofocus, exposure fluctuation, micro-shakes, motion blur, handheld
- a SECOND subject sentence ("The woman is..." again) — a duplicate subject description generates a second person / second face
- long adjective chains, edge-by-edge inventories, narration

COMPOSITION (express naturally inside the caption):
- hands always doing something concrete; if the sky is visible say what is in it ("empty open sky")
- STANCE: if her legs are visible, state it plainly — "standing with both feet planted on the ground" / "seated" / "crouched" / "mid-stride". "Stands" alone is not enough: an unspecified stance in a low-angle or rear view renders as a dynamic editorial pose (one leg extended back)
- use the constraint's camera height as given — never escalate "waist height" or "eye level" into "low-angle"
- a low camera angle does NOT mean raised arms: unless the action involves something overhead, her arms are down/relaxed — state their position explicitly
- close-up/bust: the framing shows nothing below the bust; NEVER mention footwear, but DO name her waist garment briefly (insurance: an undescribed lower body renders as underwear if the frame comes out wider)
- feet-only: only feet, shins and ground; no face/torso words; never a bending pose
- an animal only if the action has one, with its own fur color
- never require a third hand ("both hands" while another prop is held); carried items from the character reference appear ONLY if this shot's action leaves a hand free
- {compose}

ASPECT RATIO: {label} ({dims})\
"""

# KF3: 映像演出のプロトコル語(画像モデルのキャプション分布外 → 破綻画像の原因)の検出
_KF_JARGON_RE = re.compile(
    r"enters? (?:the )?frame|off[- ]camera|off[- ]screen|anchors? the|at the (?:top|bottom) edge|"
    r"the main character|-down visible|framed from|toward the lens",
    re.IGNORECASE,
)
_KF3_RULE = (
    'KF3 NATURAL CAPTION: rewrite as a natural photo caption a human would type into an image generator. '
    'Remove all cinematography protocol language ("enters the frame", "off-camera", "anchors the side", '
    '"the main character is") and motion verbs; describe the subject exactly once with static pose wording.'
)
# KF5: 低アングル指定でactionに頭上要素がないのに「頭上へ掲げる」記述が入る癖の検出
# (Z-Image/LLMの「低アングル=腕を上げて何かを掴む」事前分布への対抗。2026-07-05)
_KF_RAISED_OVERHEAD_RE = re.compile(
    r"up against the [^.]*sky|aloft|above (?:her|his) head|(?:arms?|hands?) raised (?:high|straight up|overhead)|"
    r"raising [^.]{0,30}(?:overhead|skyward|to the sky)|holds? [^.]{0,40}(?:skyward|toward the sky)",
    re.IGNORECASE,
)
_ACTION_OVERHEAD_RE = re.compile(
    r"rais\w+|overhead|above (?:her |his )?head|hang\w+|\bsky\b|branch|shelf|clothesline|lifts? [^ ]+ (?:up|high)",
    re.IGNORECASE,
)
_KF5_RULE = (
    'KF5 NO UNMOTIVATED RAISING: the action does not involve anything overhead — she must NOT hold or raise '
    'anything toward the sky / above her head. A low camera angle only means the camera views her from below; '
    'her arms stay where the action puts them. Rewrite the pose accordingly and state the arm position explicitly.'
)

# KF6: 脚が見えるショットでスタンス未記述 → 低アングル/後ろ姿でエディトリアル風の動的ポーズ
# (片足を後ろへ伸ばす等)が生成される。「stands」だけでは足が固定されない(2026-07-05)
_KF_STANCE_RE = re.compile(
    r"both feet (?:planted|on the ground)|feet planted|firmly on the ground|"
    r"\bseated\b|\bsitting\b|crouch\w*|kneel\w*|mid[- ]stride|walking|squat\w*",
    re.IGNORECASE,
)
_KF6_RULE = (
    'KF6 STANCE: her legs are visible in this framing but her stance is not anchored. State it plainly '
    '("standing with both feet planted on the ground" / "seated" / "mid-stride") — an unspecified stance '
    'renders as a dynamic editorial pose with a leg extended back. Do not add any other pose drama.'
)

_KF2_RULE_TEMPLATE = (
    'KF2 MISSING ATTRIBUTES: weave these character details naturally INTO the existing subject description — '
    'never add a second "the woman is..." sentence (a duplicate subject generates a second person/face): {attrs}'
)

# KF7: KF2の「二重の被写体紹介文を作るな」という指示にfixerが従わず、既存の紹介文とは別に
# もう1文「A young Korean woman, early 20s, ...」を追加してしまう事故が実データで発覚
# (2026-07-14、cleavage/体型をmust化してKF2 fixerの発動頻度が上がったことで顕在化)。
# 「顔2つ・2人目が生成される」という設計上ずっと警戒されてきた失敗モードそのものなので、
# 決定論的に検出し、既存のPass4リトライループ(検出→fixer→再検出)にそのまま乗せて直す。
_GENDER_WORD_IN_SENTENCE_RE = re.compile(r"\b(?:woman|man|girl|boy)\b", re.IGNORECASE)


def _duplicate_subject_sentences(text: str) -> bool:
    """テキストを文単位に分割し、性別語(woman/man/girl/boy)を含む文が2つ以上あればTrue。
    1人の被写体を1回だけ導入する自然な写真キャプションなら、性別語が現れる文は通常1つ
    しかない——2つ以上あれば、KF2 fixerが「織り込む」指示を無視して別の被写体紹介文を
    追加した(=2人目が生成されるリスク)可能性が高い。文頭に限定した検出だと
    「photo of a young Korean woman standing...」のように性別語が文の途中(目的語位置)に
    ある最初の紹介文を見逃すため、文全体をスキャンする方式にした(2026-07-14実データで
    最初の実装が誤ってFalseを返すことが判明し修正)。"""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    count = sum(1 for s in sentences if _GENDER_WORD_IN_SENTENCE_RE.search(s))
    return count >= 2


_KF7_RULE = (
    'KF7 DUPLICATE SUBJECT: this prompt introduces the subject with TWO separate sentences '
    '(each mentioning "woman"/"man"/"girl"/"boy" as if starting a new introduction) — this reads as '
    'two different people and will generate a second face/person. Merge them into ONE single subject '
    'sentence that keeps every unique detail from both (pose, location, character attributes), '
    'then remove the redundant second introduction entirely.'
)

# 動画専用の概念語 — 静止画プロンプトに書くとZ-Imageが「線・ノイズ・ブレ」として文字通り描画する
_KF_ARTIFACT_RE = re.compile(
    r",?\s*(?:film\s+)?grain(?:\s+texture| overlay)?|,?\s*compression artifacts?|"
    r",?\s*autofocus hunting(?:\s+artifacts?)?|,?\s*exposure fluctuations?|"
    r",?\s*(?:handheld\s+)?micro[- ]shakes?|,?\s*motion blur|,?\s*handheld shake|"
    r",?\s*no stabilization|,?\s*scan ?lines?",
    re.IGNORECASE,
)


def _strip_animal_sounds(motion_prompt: str, action: str) -> str:
    """actionに動物がいないのに動物の鳴き声が音に入っている場合、除去する。
    I2Vでは音に「cat meowing」と書くだけでLTXが猫を追加生成するため(2026-07-05)。"""
    if _animals_in(action):
        return motion_prompt
    cleaned = re.sub(
        r"[^.;,]*\b(?:cat|kitten|meow\w*|purr\w*|dog|puppy|bark\w*|woof)\b[^.,;]*[.,;]?",
        "", motion_prompt, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ,;")


# KF1: feet-onlyキーフレームに顔・上半身の語が混入すると、Z-Imageが前屈みの全身を構図に入れてしまう
_KF_FEET_UPPER_RE = re.compile(
    r"\b(face|head|hair|smil\w+|torso|shoulders?|crop top|necklace|ponytail|bangs|jawline)\b",
    re.IGNORECASE,
)
_KF1_RULE = (
    'KF1 FEET-ONLY FRAME: this keyframe shows ONLY feet, shins and the ground. The subject\'s upper body is '
    'out of frame above the top edge. Remove every mention of her face, head, hair, torso, shoulders, or upper '
    'garments (crop top, necklace). Do not pose her bending down. Legs are cropped at mid-shin or knee.'
)

_MOTION_FORMATTER_SYSTEM = """\
You are an image-to-video motion prompt writer for LTX-2.3.
You receive an ALREADY-VERIFIED shot description (character, outfit, environment, and camera work already
confirmed correct) — but the first frame is ALSO given separately as an input image, so appearance, outfit,
environment and composition are already defined visually and must NOT be repeated here.
Extract ONLY the action/camera/audio parts of the source description; ignore its appearance/environment parts entirely.
A clip of a few seconds can only perform ONE action. Write less, get more.

OUTPUT: ONE paragraph, 25-45 WORDS MAXIMUM, present tense:
1. ONE primary motion of the subject (a 2s clip = one action; 3s may add one small second beat)
2. Camera in one short clause — exactly as the constraint (static hold / slow push-in / tracking)
3. Audio: 1-2 ambient sounds; quoted dialogue verbatim if the action has any

HARD RULES:
- No clothing/outfit words, no hair styling, no environment inventory — the image already shows them (hair/fabric may MOVE)
- NEVER name an animal in the audio or motion unless the animal is in the shot — naming it makes the model ADD one
- Never describe the subject at two positions; hands are her own ("her hand reaches", never "a hand enters the frame")
- Lateral shots keep ONE type, in FULL SIDE PROFILE: crossing = fixed camera, edge to edge / tracking alongside = she stays centered, background scrolls, no edge exit
- No meta commentary, no end-state essays\
"""

_ASPECT = {
    "horizontal": {
        "aspect": "16:9 widescreen",
        "label": "16:9 WIDESCREEN LANDSCAPE",
        "dims":  "1280×720",
        "frame_phrase": "16:9 widescreen landscape",
        "compose": "Always include wide horizontal environmental elements (background scenery, left-to-right depth, side-by-side objects) so the landscape frame is naturally filled. Never compose as a narrow vertical or portrait shot.",
    },
    "vertical": {
        "aspect": "9:16 vertical",
        "label": "9:16 VERTICAL PORTRAIT",
        "dims":  "720×1280",
        "frame_phrase": "9:16 vertical portrait",
        "compose": "Always compose vertically — subject centered in a tall frame, top-to-bottom depth (sky/ceiling to ground), vertical architectural elements (poles, walls, doorways). Never compose as a wide horizontal shot.",
    },
}


def _parse_prompts_txt(path: Path) -> tuple[dict, list[dict]]:
    """既存runのprompts.txtをパースし、ヘッダーとセグメント別のKeyframe/Motionプロンプトを復元する(リトライ用)。"""
    text = path.read_text(encoding="utf-8")
    seg_head = re.compile(r"(?m)^\[(\d+)/(\d+)\]\s+(\S+)\s+\((\d+)s\)\s*$")
    heads = list(seg_head.finditer(text))
    if not heads:
        raise ValueError(f"{path.name} にセグメントが見つかりません")

    header: dict = {}
    for line in text[: heads[0].start()].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            header[k.strip()] = v.strip()

    segments: list[dict] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        block = text[m.end(): end]
        pm = re.search(r"--- LTX prompt ---\n", block)
        if not pm:
            raise ValueError(f"セグメント{m.group(1)}の '--- LTX prompt ---' が見つかりません")
        km = re.search(r"--- Keyframe prompt ---\n", block)
        kf_prompt = block[km.end(): pm.start()].strip() if km else ""
        segments.append({
            "num":       int(m.group(1)),
            "label":     m.group(3),
            "duration":  int(m.group(4)),
            "prompt":    block[pm.end():].strip(),
            "kf_prompt": kf_prompt,
        })
    return header, segments


_DEFAULT_STYLE_TAIL = "photorealistic, candid documentary style, natural light"


# style_tailはrun全体で1回だけ抽出され、Pass3(Keyframe Formatter)が「必ずこの文言で終える」と
# 固定で毎セグメントに強制する(_KEYFRAME_FORMATTER_SYSTEM_BASEの"End with this exact style tail")。
# 抽出プロンプトが「light」というキーワードを含んでいたため、Visual Style文中の時刻依存の光描写
# (例: "Bright and cheerful in the morning")まで「常時変わらないスタイル」として抽出してしまい、
# STATE(時間帯)がセグメントごとに夜へ変わっても、style_tailだけは常に「朝」を主張し続けて
# 矛盾する事故が発覚(2026-07-06、STATE継続機能の実テストで発見)。時刻/天候に紐づく光描写は
# 除外し、色調・質感等の本当に不変なスタイルだけを抽出するよう修正。
_TIME_OF_DAY_STYLE_RE = re.compile(
    r"\b(?:morning|afternoon|evening|night|noon|midnight|dawn|dusk|sunrise|sunset|"
    r"golden hour)\b", re.IGNORECASE
)


async def _extract_style_line(global_desc: str) -> str:
    """グローバル説明のVisual Style記述から、キーフレーム末尾に付けるスタイル語(3-6個)を抽出する。
    従来は「desaturated natural light」等をハードコードしており、ユーザーのVisual Style指定
    (例: pastel coastal palette)を定数が上書きしていた(2026-07-05修正)。時刻依存の光描写を
    除外する修正は2026-07-06(上記コメント参照)。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    try:
        resp = await client.chat.completions.create(
            model=cfg.LLM_MODEL,
            messages=[
                {"role": "system", "content": (
                    "From the reference, extract 3-6 comma-separated STILL-PHOTOGRAPHY style keywords "
                    "(color grading/palette character, overall look) suitable for the end of an image "
                    "prompt — qualities that hold true for the ENTIRE video regardless of time of day or "
                    "scene. Default to real-world photography: include 'photorealistic' UNLESS the "
                    "reference explicitly requests an anime/illustration/cartoon/2D-animated look — in "
                    "that case describe THAT style instead and do NOT add 'photorealistic'. EXCLUDE "
                    "video-only terms (grain, compression, autofocus, shake, motion blur, rolling shutter) "
                    "AND any time-of-day-specific or transient lighting description (e.g. 'bright morning "
                    "light', 'golden sunset glow') — those vary per scene and are handled elsewhere. "
                    "Output ONLY the comma-separated keywords."
                )},
                {"role": "user", "content": f"/no_think\n{global_desc}"},
            ],
            temperature=0.3,
            max_tokens=128,
        )
    except Exception:
        return _DEFAULT_STYLE_TAIL
    line = (resp.choices[0].message.content or "").strip()
    if not line:
        line = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()
    line = line.split("\n")[0].strip().strip(".")
    line = _KF_ARTIFACT_RE.sub("", line)
    # LLMの指示追従だけに頼らず、時刻語が残っていれば要素単位(カンマ区切り)で機械的に除去する
    parts = [p.strip() for p in line.split(",") if p.strip()]
    parts = [p for p in parts if not _TIME_OF_DAY_STYLE_RE.search(p)]
    line = ", ".join(parts)
    line = re.sub(r"\s{2,}", " ", line).strip(" ,;")
    # 明示的にアニメ/イラスト系を要求していない限り、写実系の語が抽出結果に無ければ追加する
    # (2026-07-10、ユーザー指摘: 「明示的にアニメ、イラストと書かれてない限りはリアル系で」。
    # LLMの指示追従だけに頼らないPython決定論的な最終防衛)
    if not _ANIME_STYLE_RE.search(global_desc) and not _REALISM_HINT_RE.search(line):
        line = f"{line}, photorealistic" if line else "photorealistic"
    return line if len(line) >= 10 else _DEFAULT_STYLE_TAIL


# direction側の歩行ショット指標(actionに歩行動詞がない足元トラッキング等も歩きと数える)


async def _format_keyframe_prompt(scene_desc: str, global_desc: str, duration: int, orientation: str, direction: str = "", character_line: str = "", action: str = "", reserved_props: list[str] | None = None, style_tail: str = _DEFAULT_STYLE_TAIL, state: str = "") -> str:
    """Pass 3a: 基準テキスト(Pass3の`ground_truth`、旧来のscene_desc直接ではない)を
    1stフレーム静止画プロンプト(Z-Image向け)に変換する。見た目(キャラ・衣装・背景・構図)の
    全情報はここに集約され、動画にはこの画像経由で渡る。
    scene_desc引数名は維持しているが、実際に渡されるのは`_generate_ground_truth`が
    t2vのPass3(LTX Formatter)を流用して生成した、衣装/STATE確認済みの基準テキスト
    (2026-07-08)。tightショットでKeyframe Formatterが40-80語の予算の中で衣装等を
    ゼロから再導出し損なう事故(test_t2v_market.txt seg3、黄色いレインコート脱落)を
    防ぐため、既に正しいテキストから圧縮するだけで済む設計に変更した。
    state: Pass0が決定した「このセグメント時点のSTATE」(時間帯/天気/服装)。参照文(global_desc)の
    既定値と矛盾する場合、こちらを優先させる(2026-07-06、キーフレーム本文がglobal_descの
    「Bright and cheerful in the morning」等をSTATE=夜のシーンでもそのまま書いてしまう事故が
    実テストで発覚したため追加)。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    character_line = _trim_character_for_scale(character_line, direction)
    a = _ASPECT[orientation]
    system = _KEYFRAME_FORMATTER_SYSTEM_BASE.format(label=a["label"], dims=a["dims"], compose=a["compose"], style_tail=style_tail)
    direction_constraint = (
        f"\n## Shot Director constraint (MUST match — shot scale, camera angle, facing)\n{direction}"
        if direction else ""
    )
    character_block = (
        f"## Character (copy verbatim — this is the only source for nationality/gender/age/hair color/build; "
        f"if it does NOT mention an age descriptor, do not invent one yourself — e.g. never add 'young' or "
        f"'middle-aged' on your own, since doing so inconsistently across segments changes her apparent age shot to shot)"
        f"\n{character_line}\n\n"
        if character_line else ""
    )
    state_block = (
        f"## Current established state (this is the only source for location and lighting/weather for THIS "
        f"shot, AND for everything about her current appearance beyond the fixed identity in Character above — "
        f"outfit, accessories, hairstyle, or anything else she is wearing/carrying/styled with. The location it "
        f"names MUST appear as an actual place word in your caption, even in a tight/close-up shot; "
        f"do not invent or reuse any of this from anywhere else)"
        f"\n{state}\n\n"
        if state else ""
    )
    user_msg = (
        f"/no_think\n"
        f"## Character & Style reference\n{global_desc}\n\n"
        f"{character_block}"
        f"{state_block}"
        f"## Verified shot description ({duration}s shot — character/outfit/environment already confirmed "
        f"correct; extract only the FIRST INSTANT)\n{scene_desc}{direction_constraint}"
    )
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.7,
        max_tokens=4096,
    )
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        content = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()
    content = _strip_reference_echo(content, global_desc)
    content = _KF_ARTIFACT_RE.sub("", content)
    content = re.sub(r"\s{2,}", " ", content).strip(" ,;")
    content = _enforce_animal_beside(content)
    content = _enforce_attached_hands(content)
    content = _enforce_feet_only_framing(content, direction)
    content = _enforce_closeup_scale(content, direction)
    content = _strip_offscreen_ground_mentions(content, direction)
    if len(content.split()) > 110:
        print(f"[i2v-tl6]   警告: keyframeプロンプトが長すぎます({len(content.split())}語 > 目標40-80)")

    # KF2: キャラ属性の欠落は「文を追加」ではなく「既存の被写体記述に織り込む」形でfixerに修正させる。
    # (旧: _enforce_character_lineがキャラ文を丸ごと追記 → 被写体の二重記述となり顔2つ・2人目が生成される事故)
    # 2026-07-07修正: 衣装はcharacter_lineではなくstateが持つ(2026-07-06にキャラ抽出から衣装を
    # 除外したため)。旧チェックは character_line に対して衣装欠落を判定しており、character_line
    # に衣装語が無くなった結果 _garments_missing が常に空リストを返す死んだチェックになっていた
    # (実データでseg2/7/9の衣装が完全に欠落し検出されない事故が発覚)。stateに対して判定する。
    # さらにショットスケールに応じて必要な部分だけ要求する(close-up/bust=上半身のみ・
    # feet-only=下半身のみ・それ以外=全部)。この区別が無いと、tightショットにも下半身
    # (ジーンズ等)を強制挿入してしまい、「close-up/bustは下半身を映さない」という
    # Keyframe Formatter自身のCOMPOSITIONルールと矛盾する事故になる(t2v側で発覚、2026-07-07)
    d_low = direction.lower()
    is_tight = _is_tight_scale(direction)
    is_feet_only = "feet" in d_low

    def _kf2_detect(text: str) -> list[str]:
        """KF2系(衣装/STATE詳細/キャラ)+KF3/KF4/KF5/KF6の違反ルール文を現在のtextに対して
        再計算する。リトライループで毎回呼び直せるようクロージャとして切り出した(2026-07-10、
        ユーザー指摘: 検証パスは検証結果を見て行動して初めて意味がある。一発勝負で結果を
        無条件採用するのは検証パスの定義として矛盾)。"""
        rules: list[str] = []
        missing_garments = _garments_missing(text, state) if state else []
        if is_tight:
            missing_garments = [g for g in missing_garments if not any(item in g for item in _LOWER_BODY_ITEMS)]
        elif is_feet_only:
            missing_garments = [g for g in missing_garments if any(item in g for item in _LOWER_BODY_ITEMS)]
        if missing_garments:
            rules.append(_KF2_RULE_TEMPLATE.format(attrs=state))
        # KF2汎用版: 衣装以外も含めたSTATEの全断片を検品(STATEは絶対、髪型等も対象。2026-07-07)。
        # feet-onlyは顔/髪が映らない構図のため対象外(KF1がむしろ逆に髪語の混入を除去する)
        if state and not is_feet_only and _state_details_missing(text, state):
            rules.append(_KF2_RULE_TEMPLATE.format(attrs=state))
        if character_line and _character_tokens_missing(text, character_line):
            rules.append(_KF2_RULE_TEMPLATE.format(attrs=character_line))
        # KF7: 被写体紹介文の二重化(顔2つ・2人目の生成事故、2026-07-14)
        if _duplicate_subject_sentences(text):
            rules.append(_KF7_RULE)
        # KF3: 映像演出プロトコル語(キャプション分布外)の混入
        if _KF_JARGON_RE.search(text):
            rules.append(_KF3_RULE)
        # KF5: 低アングルで頭上要素がないのに「掲げる」ポーズ(低アングル=腕上げ癖への対抗)
        if re.search(r"low[- ]angle|shooting up", direction, re.IGNORECASE) \
                and _KF_RAISED_OVERHEAD_RE.search(text) and not _ACTION_OVERHEAD_RE.search(action):
            rules.append(_KF5_RULE)
        # KF6: 脚が見えるショット(tight/feet以外)なのにスタンス語がない → 動的ポーズ事故
        if not _is_tight_scale(direction) and not _KF_STANCE_RE.search(text):
            rules.append(_KF6_RULE)
        # KF4: actionに動物がいるのにキーフレームに不在(旧_enforce_animal_firstの注入文も二重記述の一種だったため廃止)
        expected_animals = _animals_in(action)
        if expected_animals and not _animals_in(text):
            rules.append(
                f"KF4 MISSING ANIMAL: the shot includes a {expected_animals[0]} — describe it ONCE, naturally, "
                f"beside or ahead of the subject in clear view, with its own fur color.")
        return rules

    ids = _kf2_detect(content)
    if ids:
        print(f"[i2v-tl6]   Pass4 lint(keyframe) 違反検出: {', '.join(r[:3] for r in ids)}")
        for attempt in range(1, _LINT_MAX_ATTEMPTS + 1):
            # 以前は「破棄/進展なし」で即breakし残り試行を丸ごと放棄していた(2026-07-12修正、continueで次を試す)
            fixed = await _run_fixer(content, "\n".join(ids), direction, action, orientation, character_line,
                                     fixer_system=_KEYFRAME_FIXER_SYSTEM)
            if not fixed:
                print(f"[i2v-tl6]   Pass4 lint(keyframe) 修正結果を破棄(試行{attempt})")
                continue
            content = fixed
            remaining = _kf2_detect(content)
            if not remaining:
                if attempt > 1:
                    print(f"[i2v-tl6]   Pass4 lint(keyframe): 全違反解消(試行{attempt})")
                ids = []
                break
            if set(remaining) == set(ids):
                print(f"[i2v-tl6]   Pass4 lint(keyframe): 試行{attempt}で進展なし、次を試行")
            ids = remaining
        if ids:
            print(f"[i2v-tl6]   Pass4 lint(keyframe) 未解消(最大{_LINT_MAX_ATTEMPTS}回試行後): {', '.join(r[:3] for r in ids)}")

    # Pass4(キーフレーム側): 構図系チェック(C2/C5/C10/C12/C13)。actionを渡さない=動詞系C14は対象外
    # (この_lint_ltx_prompt自体が内部でリトライループを持つ、2026-07-10)
    linted = await _lint_ltx_prompt(content, direction, "", orientation, character_line, reserved_props,
                                    fixer_system=_KEYFRAME_FIXER_SYSTEM)
    if linted and linted != content:
        print("[i2v-tl6]   Pass4 lint(keyframe): 修正あり")
        content = linted

    # KF1: feet-onlyショットに顔・上半身の語が混入 → 前屈み全身の構図になるため除去
    # (顔/髪語を削る修正なので結果は元より短くなる。min_len=400のデフォルトのままだと
    # 正しく短縮された修正結果が誤って棄却され、元の混入テキストにフォールバックしてしまう
    # 事故が実データで発覚(2026-07-07)。feet-onlyの正当な短さに合わせて個別に緩めた値を指定)
    if "feet" in direction.lower():
        for attempt in range(1, _LINT_MAX_ATTEMPTS + 1):
            if not _KF_FEET_UPPER_RE.search(content):
                break
            print(f"[i2v-tl6]   Pass4 lint(keyframe) 違反検出: KF1(試行{attempt})")
            # 以前は破棄されたら即breakし残り試行を丸ごと放棄していた(2026-07-12修正、continueで次を試す)
            fixed = await _run_fixer(content, _KF1_RULE, direction, action, orientation, character_line, min_len=100)
            if not fixed:
                continue
            content = fixed
        if _KF_FEET_UPPER_RE.search(content):
            print(f"[i2v-tl6]   Pass4 lint(keyframe) 未解消(最大{_LINT_MAX_ATTEMPTS}回試行後): KF1")

    # KF2/KF1のfixerが床/地面言及を再混入させる可能性があるため、最後にもう一度Pythonで保証
    content = _strip_offscreen_ground_mentions(content, direction)
    # KF2(衣装)のLLM fixerが従わなかった場合の最終防衛(2026-07-10、t2vと同じ設計)
    content = _enforce_garments_present(content, state, direction)
    # 明示的にアニメ/イラスト系を要求していない限り写実系をデフォルトにする最終防衛(2026-07-10、
    # ユーザー指摘: 「明示的にアニメ、イラストと書かれてない限りはリアル系で」。style_tail側の
    # 対策に加え、キーフレーム本文自体にも二重で保証する)
    content = _enforce_realism_default(content, global_desc)

    # Krea2専用プロンプト補正(2026-07-13): アクティブなキーフレームワークフローがKrea2系
    # (z-image.json以外)の場合のみ、docs/Krea2.md由来の軽量補正を適用する。この関数
    # (_format_keyframe_prompt)はPhase1でasyncio.gather並列実行される既存のLLM呼び出しに
    # 1つ追加するだけなので、Phase1の既存並列パターンに影響しない。--retryはこの関数を
    # 呼び直さずprompts.txt保存済みkf_promptを再利用するため、リトライ側にも影響しない。
    if cfg.KEYFRAME_WORKFLOW_JSON != "z-image.json":
        content = await prompt_generator.expand_krea2_prompt(content)

    # .envのIMAGE_PROMPT_PREFIX(品質向上prefix)を先頭に付加。
    # 既存のprompt_generator.py(weather/news/bikiniモード)では適用済みだったが、
    # タイムラインCLI系(i2v/t2v)には未適用だったことが発覚したため追加(2026-07-07)。
    # 全ての検品・修正が終わった後に付加する(語数チェック等はLLM生成部分のみを対象にするため)
    _prefix = cfg.IMAGE_PROMPT_PREFIX.strip()
    if _prefix:
        content = f"{_prefix}, {content}"
    return content


# M1: 動きプロンプトへの静的見た目の再記述の検出 — 画像に既にある情報の重複はI2Vで有害。
# ※「Black sneakers strike the concrete」(足元ショットの動く主体)や「hand near her necklace」(位置指定)は
#   正当な動き記述なので、列挙的な外見描写(wears/dressed in/outfit)だけを対象にする
_STATIC_REDESC_RE = re.compile(
    r"\bwear(?:s|ing)?\b|\bdressed in\b|\bher outfit\b|\bstyled in\b",
    re.IGNORECASE,
)
_M1_RULE = (
    'M1 NO STATIC RE-DESCRIPTION: this is an image-to-video prompt — the input image already shows the '
    'character\'s outfit, colors, and environment. Remove all clothing/outfit descriptions ("wears...", '
    'garment names, colors of clothes). Keep only motion, camera behavior, and audio. '
    'Hair or fabric may be described as MOVING, never as appearance.'
)

_KEYFRAME_FIXER_SYSTEM = """\
You are a still-image prompt fixer. You receive a keyframe prompt and a list of violations.
Rewrite the prompt so that every violation is fixed. Change ONLY what the violations require;
preserve everything else — wording, style keywords, character sentence, roughly the same length
as the original (a natural photo caption, ~40-80 words).

VIOLATED RULES TO FIX:
{violated_rules}

STRICT OUTPUT RULES:
- Output ONLY the corrected prompt text — no commentary, no headers
- Keep the character sentence verbatim; keep it a STILL IMAGE (no motion verbs)
- Never add: grain, compression artifacts, autofocus hunting, exposure fluctuation, micro-shakes, motion blur\
"""

_MOTION_FIXER_SYSTEM = """\
You are an image-to-video motion prompt fixer. You receive a motion prompt and a list of violations.
Rewrite the prompt so every violation is fixed, as ONE natural flowing paragraph of 25-45 words, present tense.

VIOLATED RULES TO FIX:
{violated_rules}

STRICT OUTPUT RULES:
- Output ONLY the corrected prompt text — no commentary, no meta remarks ("this composition ensures..."), no headers
- NEVER paste the camera direction, the action text, or the orientation label verbatim into the prompt
- Keep only: subject motion, secondary motion, camera behavior + end state, audio (quoted dialogue verbatim if any)
- No clothing/outfit descriptions, no environment inventory — the input image already shows them\
"""

# 場所・情景の名詞は動きプロンプトの要素チェック対象外(静的情報は画像側が持つため)
_MOTION_ELEMENT_STOPWORDS = {
    "alley", "sidewalk", "terrace", "clothesline", "morning", "light", "sky",
    "street", "pavement", "background",
}
# fixerが制約(向きラベル等)をエコーした場合の除去
_CONSTRAINT_ECHO_RE = re.compile(r"(?m)^\s*(?:16:9|9:16)[^\n]*\)\s*$\n?")


async def _format_motion_prompt(scene_desc: str, ambience: str, duration: int, orientation: str, direction: str = "", action: str = "") -> str:
    """Pass 3b: 基準テキスト(Pass3の`ground_truth`)をI2V用の動きプロンプトに変換する
    (公式I2V原則: 静的要素は記述しない)。scene_desc引数名は維持しているが、実際に渡される
    のはKeyframe Formatter同様`ground_truth`(2026-07-08)。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    direction_constraint = (
        f"\n## Camera constraint (MUST match — camera motion and movement direction)\n{direction}"
        if direction else ""
    )
    user_msg = (
        f"/no_think\n"
        f"## Verified shot description ({duration}s shot; the first frame is ALSO given separately as an "
        f"image — extract ONLY action/camera/audio from this text, ignore its appearance/environment parts)"
        f"\n{scene_desc}{direction_constraint}\n\n"
        f"## Shot action\n{action}\n\n"
        f"## Available ambient sounds\n{ambience}"
    )
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": _MOTION_FORMATTER_SYSTEM},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.7,
        max_tokens=2048,
    )
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        content = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()
    content = _enforce_animal_sound(content, scene_desc)
    content = _strip_animal_sounds(content, action)
    content = _enforce_attached_hands(content)
    content = _enforce_dialogue_attribution(content, action)
    content = _CONSTRAINT_ECHO_RE.sub("", content).strip()
    if len(content.split()) > 70:
        print(f"[i2v-tl6]   警告: motionプロンプトが長すぎます({len(content.split())}語 > 目標25-45)")

    # Pass4(動き側): M1(静的再記述)/C14(アクション忠実性: 場所名詞は対象外)/C3(動物不在物語)/C15(横移動の型混在)/C16(手の本数)
    def _motion_detect(text: str) -> list[str]:
        """リトライループで毎回呼び直せるよう検出をクロージャとして切り出した(2026-07-10、
        ユーザー指摘: 検証パスは結果を見て行動して初めて検証と言える)。"""
        found: list[str] = []
        if _STATIC_REDESC_RE.search(text):
            found.append("M1")
        if action and _missing_action_elements(text, action, extra_stopwords=_MOTION_ELEMENT_STOPWORDS):
            found.append("C14")
        if _ANIMAL_ABSENT_RE.search(text):
            found.append("C3")
        if _LATERAL_TRACKING_RE.search(text) and _EDGE_CROSSING_RE.search(text):
            found.append("C15")
        if _hand_budget_violation(text):
            found.append("C16")
        return found

    ids = _motion_detect(content)
    if ids:
        print(f"[i2v-tl6]   Pass4 lint(motion) 違反検出: {', '.join(ids)}")
        for attempt in range(1, _LINT_MAX_ATTEMPTS + 1):
            rules = "\n".join(
                (_M1_RULE if i == "M1" else f"{i} " + _LINT_CHECKS[i]) for i in ids
            )
            # 以前は「破棄/進展なし」で即breakし残り試行を丸ごと放棄していた(2026-07-12修正、continueで次を試す)
            fixed = await _run_fixer(content, rules, direction, action, orientation, "",
                                     fixer_system=_MOTION_FIXER_SYSTEM, min_len=120)
            if not fixed:
                print(f"[i2v-tl6]   Pass4 lint(motion) 修正結果を破棄(試行{attempt})")
                continue
            content = _CONSTRAINT_ECHO_RE.sub("", fixed).strip()
            remaining = _motion_detect(content)
            if not remaining:
                if attempt > 1:
                    print(f"[i2v-tl6]   Pass4 lint(motion): 全違反解消(試行{attempt})")
                ids = []
                break
            if set(remaining) == set(ids):
                print(f"[i2v-tl6]   Pass4 lint(motion): 試行{attempt}で進展なし、次を試行")
            ids = remaining
        if ids:
            print(f"[i2v-tl6]   Pass4 lint(motion) 未解消(最大{_LINT_MAX_ATTEMPTS}回試行後): {', '.join(ids)}")
    # lintのLLM修正で「She says」等の話者動詞が再度剥がされる可能性があるため最後に再適用(2026-07-12)
    content = _enforce_dialogue_attribution(content, action)
    return content


async def _lint_ltx_prompt(ltx_prompt: str, direction: str, action: str, orientation: str, character_line: str, reserved_props: list[str] | None = None, fixer_system: str | None = None) -> str:
    """Pass4: 完成したLTXプロンプトをチェックリストで検品し、違反があれば最小修正して返す。
    個別バグへのルール分散追加・正規表現の増殖を止め、失敗モードの知見はチェックリスト1箇所に集約する(2026-07-04)。
    従来は検出→fix1回で終わり、直りきらなかった違反はログのみで未解消のまま採用していたが、
    実データでC16(HAND BUDGET)が未解消のまま配信される事故が発覚(2026-07-10)。ユーザー提案
    「治るまでloopすれば」を受け、最大`_LINT_MAX_ATTEMPTS`回、毎回まだ残っている違反だけを
    次のfixerに渡して繰り返す(直った分は自然に外れ、fixerの負担が試行ごとに減っていく)。"""
    # 検出フェーズ(Python・決定論的)
    ids = _detect_violations(ltx_prompt, direction, orientation, reserved_props, action)
    if not ids:
        return ltx_prompt
    print(f"[i2v-tl6]   Pass4 lint 違反検出: {', '.join(ids)}")

    a = _ASPECT[orientation]
    a_forbidden = (
        'the words "landscape", "widescreen", "16:9"' if orientation == "vertical"
        else 'the words "portrait", "vertical", "9:16"'
    )
    current = ltx_prompt
    for attempt in range(1, _LINT_MAX_ATTEMPTS + 1):
        violated_rules = "\n".join(
            f"{i} " + (_LINT_CHECKS[i].format(frame_phrase=a["frame_phrase"], aspect_forbidden=a_forbidden)
                       if "{" in _LINT_CHECKS[i] else _LINT_CHECKS[i])
            for i in ids if i in _LINT_CHECKS
        )
        # 以前は「破棄/進展なし」で即breakし残り試行を丸ごと放棄していた(2026-07-12修正、continueで次を試す)
        fixed = await _run_fixer(current, violated_rules, direction, action, orientation, character_line,
                                 fixer_system=fixer_system)
        if not fixed:
            print(f"[i2v-tl6]   Pass4 lint 修正結果を破棄(試行{attempt})")
            continue
        remaining = _detect_violations(fixed, direction, orientation, reserved_props, action)
        current = fixed
        if not remaining:
            if attempt > 1:
                print(f"[i2v-tl6]   Pass4 lint: 全違反解消(試行{attempt})")
            return current
        if set(remaining) == set(ids):
            print(f"[i2v-tl6]   Pass4 lint: 試行{attempt}で進展なし、次を試行")
        else:
            ids = remaining  # 次の試行では残っている違反だけを渡す

    if ids:
        print(f"[i2v-tl6]   Pass4 lint 未解消(最大{_LINT_MAX_ATTEMPTS}回試行後): {', '.join(ids)}")
    return current


async def _run_fixer(prompt: str, violated_rules: str, direction: str, action: str, orientation: str, character_line: str,
                     fixer_system: str | None = None, min_len: int = 400) -> str | None:
    """違反ルール文だけをLLMに渡して最小修正させる(修正フェーズの共通部)。失敗時はNone。
    fixer_system: 省略時は汎用の_PROMPT_FIXER_SYSTEM。動きプロンプトは_MOTION_FIXER_SYSTEM(短文・メタ解説禁止)を渡す。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    a = _ASPECT[orientation]
    user_msg = (
        f"/no_think\n"
        f"## Shot constraint\n"
        f"orientation: {a['label']} ({a['dims']})\n"
        f"camera direction: {direction}\n"
        f"action: {action}\n"
        f"character: {character_line or '(none)'}\n\n"
        f"## Prompt to fix\n{prompt}"
    )
    fixer_system = (fixer_system or _PROMPT_FIXER_SYSTEM).format(violated_rules=violated_rules)
    try:
        resp = await client.chat.completions.create(
            model=cfg.LLM_MODEL,
            messages=[
                {"role": "system", "content": fixer_system},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.4,
            max_tokens=4096,
        )
    except Exception as e:
        print(f"[i2v-tl6]   Pass4 fix スキップ(LLMエラー): {e}")
        return None
    fixed = (resp.choices[0].message.content or "").strip()
    if not fixed:
        fixed = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()
    # 修正文の妥当性を軽く検証: 短すぎ・解説混入は破棄
    if len(fixed) < min_len or re.match(r"^(C\d+|M\d+)\b", fixed):
        return None
    return fixed


# 「(alongside) closely behind」のように足部位を伴わない後方表現も対象


# 部分文字列誤マッチ対策("catches"の中の"cat"等)。動物判定は必ずこのregexで行う


def _keyframe_size(width: int, height: int) -> tuple[int, int]:
    """キーフレーム生成解像度(動画のwidth/heightにKEYFRAME_SIZE_SCALEを掛けたもの、アスペクト比維持)。
    I2V側ワークフロー(node 344 ResizeImageMaskNode)がキーフレームを動画の最終解像度へ自動リサイズ
    するため、キーフレームは動画本体より高解像度で生成しても問題ない(2026-07-08、Krea2対策)。"""
    return round(width * cfg.KEYFRAME_SIZE_SCALE), round(height * cfg.KEYFRAME_SIZE_SCALE)


async def _generate_i2v_video(prompt: str, kf_server_name: str, width: int, height: int, duration_s: float, bypass_likeness: bool = False) -> Path:
    """I2V動画生成の実行エンジン切替(.envのI2V_VIDEO_ENGINE、テスト用)。
    "10e"ならworkflows/10E-ltx_2_3_i2v.json(10Erosチェックポイント検証用、2026-07-09)、
    "refine"ならworkflows/refine_ltx2_3.json(顔検出+同一性アンカー付き2段サンプリング検証用、2026-07-10)、
    それ以外は従来通りgenerate_t2v_video()(2026_ltx2_3_t2v.jsonのI2Vモード)を使う。t2v_timeline_cliV5は
    このフラグを見ないため無条件に従来のまま(generate_t2v_video()を直接呼ぶ)。
    bypass_likenessは"refine"エンジンでのみ意味を持つ(`--norefine`用、顔が手/物で隠れるセグメントの
    破綻対策としてLTX Likeness Anchorだけbypassする)。"""
    if cfg.I2V_VIDEO_ENGINE == "10e":
        return await generate_video_10e(prompt, kf_server_name, width, height, duration_s)
    if cfg.I2V_VIDEO_ENGINE == "refine":
        return await generate_video_refine_ltx23(prompt, kf_server_name, width, height, duration_s, bypass_likeness=bypass_likeness)
    return await generate_t2v_video(prompt, width, height, duration_s, keyframe_server_filename=kf_server_name)


async def _run_direct(args: argparse.Namespace) -> None:
    """デバッグ用: LLMパイプライン(Pass0〜4)を一切通さず、--fのファイル内容を
    そのままComfyUIに渡してargs.direct秒の動画を1本だけ生成する(2026-07-15新設)。
    ファイルに`--- Keyframe prompt ---`区切りがあればKeyframe/Motionを分けて使い、
    無ければ全文を両方に使う。prompts.txt(1セグメント、direct: trueヘッダー)を
    通常runと同じ命名規則で書き出すため、Node.jsハーネス側は無改修でこのrunを
    表示・操作できる。"""
    prompt_path = Path(args.f)
    if not prompt_path.exists():
        print(f"[i2v-tl6] ファイルが見つかりません: {prompt_path}")
        return

    width, height = (720, 1280) if args.vertical else (1280, 720)
    orient_label = "縦 720×1280" if args.vertical else "横 1280×720"
    orientation = "vertical" if args.vertical else "horizontal"
    duration = args.direct

    text = prompt_path.read_text(encoding="utf-8")
    kf_prompt, motion_prompt = _split_direct_prompt(text)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prompts_txt = cfg.GENERATED_DIR / f"i2v6_{ts}_prompts.txt"
    _write_direct_prompts_txt(prompts_txt, prompt_path, orientation, width, height, duration,
                               motion_prompt, keyframe_prompt=kf_prompt)

    print(f"[i2v-tl6] [DIRECT] {prompt_path.name} / {orient_label} / {duration}s(LLM無し、生テキストをそのまま使用)")
    print(f"[i2v-tl6] プロンプト保存: {prompts_txt.name}")
    print(f"[i2v-tl6] keyframe prompt:\n{kf_prompt}")
    print(f"[i2v-tl6] motion prompt:\n{motion_prompt}")

    kf_path = cfg.GENERATED_DIR / f"i2v6_{ts}_seg01_kf.png"
    kf_width, kf_height = _keyframe_size(width, height)
    print(f"\n[i2v-tl6] キーフレーム生成中...")
    kf_raw = await generate_image(
        kf_prompt, kf_width, kf_height, seed=None,
        lora_name=cfg.KEYFRAME_LORA_NAME, lora_strength=cfg.KEYFRAME_LORA_STRENGTH)
    kf_raw.rename(kf_path)
    print(f"[i2v-tl6] キーフレーム保存: {kf_path.name} → アップロード中...")
    kf_server_name = await upload_image_to_comfyui(kf_path)

    seg_path = _seg_video_path(ts, 1, "direct", "i2v6")
    print(f"[i2v-tl6] 動画生成中(I2V)...")
    raw_path = await _generate_i2v_video(motion_prompt, kf_server_name, width, height, duration, bypass_likeness=args.norefine)
    raw_path.rename(seg_path)
    print(f"[i2v-tl6] 保存: {seg_path.name}")

    final = cfg.GENERATED_DIR / f"i2v6_{ts}_final.mp4"
    if _concat_segments([seg_path], final, "[i2v-tl6]"):
        print(f"\n[i2v-tl6] 完了!")
        print(f"[i2v-tl6] 最終動画: {final}")


async def _run_retry(args: argparse.Namespace) -> None:
    """既存runの指定セグメントだけ別seedで再生成し、finalを再連結する。"""
    run_id = args.retry
    # "i2v6_20260704_080510" や "..._prompts.txt" 形式でも受け付ける
    run_id = re.sub(r"^i2v6_", "", run_id)
    run_id = re.sub(r"_prompts\.txt$", "", run_id)

    prompts_path = cfg.GENERATED_DIR / f"i2v6_{run_id}_prompts.txt"
    if not prompts_path.exists():
        print(f"[i2v-tl6] prompts.txt が見つかりません: {prompts_path}")
        return

    try:
        header, segments = _parse_prompts_txt(prompts_path)
    except ValueError as e:
        print(f"[i2v-tl6] パースエラー: {e}")
        return

    # directモード(--directで作ったrun)は常にセグメント1つのみなので、--seg省略時は
    # 自動的に"1"を補う(2026-07-15新設)。通常runは従来通り--seg必須のまま。
    is_direct = header.get("direct") == "true"
    if not args.seg:
        if is_direct:
            args.seg = "1"
            print(f"[i2v-tl6] directモードのrun: --seg省略 → seg 1 を使用")
        else:
            print(f"[i2v-tl6] --retry には --seg が必要です(例: --seg 3,7)")
            return
    elif is_direct and args.seg.strip() != "1":
        print(f"[i2v-tl6] directモードのrunはセグメント1のみです(--seg {args.seg} は無効)")
        return

    # サイズ復元: --h/--vが明示指定されていればそちらを優先し、無指定ならヘッダーへ
    # フォールバックする(2026-07-14、t2vと同じ変更。ユーザー要望: 「同じ変換後プロンプトで
    # --v/--hの違いを見たい、もしくは一方が良かったのでもう一方も試したい」)。以前はヘッダー
    # 優先だったため、size記録済みのrunに--vを付けても無視されて元の向きのまま生成されていた。
    if args.vertical or args.horizontal:
        width, height = (720, 1280) if args.vertical else (1280, 720)
    else:
        size = header.get("size", "")
        m = re.match(r"^(\d+)x(\d+)$", size)
        if m:
            width, height = int(m.group(1)), int(m.group(2))
        else:
            print("[i2v-tl6] prompts.txt に size ヘッダーがありません(旧形式)。--h または --v を指定してください")
            return

    try:
        targets = sorted({int(x) for x in args.seg.split(",") if x.strip()})
    except ValueError:
        print(f"[i2v-tl6] --seg の形式が不正です: {args.seg}(例: --seg 3,7)")
        return
    seg_by_num = {s["num"]: s for s in segments}
    missing = [n for n in targets if n not in seg_by_num]
    if missing:
        print(f"[i2v-tl6] 存在しないセグメント番号: {missing}(1〜{len(segments)})")
        return

    if args.norefine and cfg.I2V_VIDEO_ENGINE != "refine":
        print(f"[i2v-tl6] 警告: --norefine が指定されましたが I2V_VIDEO_ENGINE={cfg.I2V_VIDEO_ENGINE!r} のため無視されます(refine時のみ有効)")

    print(f"[i2v-tl6] リトライ: run={run_id} / {width}x{height} / 対象セグメント: {targets}")

    if args.debug:
        for n in targets:
            seg = seg_by_num[n]
            print(f"\n[i2v-tl6] [{n}/{len(segments)}] {seg['label']} ({seg['duration']}s)")
            print(f"[i2v-tl6] prompt:\n{seg['prompt']}")
        print(f"\n[i2v-tl6] [DEBUG] 完了 — 再生成はスキップされました")
        return

    # 旧takeの退避はファイルI/Oのみで一瞬なので逐次実施(並列生成の前に完了させる必要がある)
    dests: dict[int, tuple[Path, Path]] = {}
    valid_targets = []
    for n in targets:
        seg = seg_by_num[n]
        if not seg.get("kf_prompt"):
            print(f"[i2v-tl6] エラー: セグメント{n}のKeyframeプロンプトがprompts.txtにありません — スキップ")
            continue
        kf_path = cfg.GENERATED_DIR / f"i2v6_{run_id}_seg{n:02d}_kf.png"
        if args.keep and not kf_path.exists():
            print(f"[i2v-tl6] エラー: --keep 指定ですがセグメント{n}のキーフレームが見つかりません: {kf_path.name} — スキップ")
            continue
        dest = _seg_video_path(run_id, n, seg["label"], "i2v6")
        # --keep 時はキーフレームをそのまま使うため退避対象から外す(動画側だけ退避)
        targets_to_backup = (dest,) if args.keep else (dest, kf_path)
        for old in targets_to_backup:
            if old.exists():
                k = 1
                while (backup := old.with_name(old.stem + f"_old{k}" + old.suffix)).exists():
                    k += 1
                old.rename(backup)
                print(f"[i2v-tl6] 退避: {old.name} → {backup.name}")
        dests[n] = (kf_path, dest)
        valid_targets.append(n)

    # I2Vリトライ(キーフレーム再生成→アップロード→動画生成)はLLM呼び出しが無く、他セグメントにも
    # 依存しないため並列実行する(ComfyUI自体がジョブキューを持つため無制限)
    async def _retry_one(n: int) -> None:
        seg = seg_by_num[n]
        kf_path, dest = dests[n]
        if args.keep:
            print(f"[i2v-tl6] [{n}/{len(segments)}] 既存キーフレームを使用: {kf_path.name} → アップロード中...")
        else:
            print(f"[i2v-tl6] [{n}/{len(segments)}] キーフレーム再生成中(新seed)...")
            kf_width, kf_height = _keyframe_size(width, height)
            kf_raw = await generate_image(
                seg["kf_prompt"], kf_width, kf_height, seed=None,
                lora_name=cfg.KEYFRAME_LORA_NAME, lora_strength=cfg.KEYFRAME_LORA_STRENGTH)
            kf_raw.rename(kf_path)
            print(f"[i2v-tl6]   [{n}/{len(segments)}] キーフレーム保存: {kf_path.name} → アップロード中...")
        kf_server_name = await upload_image_to_comfyui(kf_path)
        print(f"[i2v-tl6] [{n}/{len(segments)}] {seg['duration']}s 動画再生成中(I2V)...")
        raw_path = await _generate_i2v_video(seg["prompt"], kf_server_name, width, height, seg["duration"], bypass_likeness=args.norefine)
        raw_path.rename(dest)
        print(f"[i2v-tl6]   [{n}/{len(segments)}] 保存: {dest.name}")

    await asyncio.gather(*[_retry_one(n) for n in valid_targets])

    # 全セグメント(再生成分+既存分)を番号順に集めて再連結
    seg_paths: list[Path] = []
    for s in segments:
        p = _seg_video_path(run_id, s["num"], s["label"], "i2v6")
        if not p.exists():
            print(f"[i2v-tl6] エラー: セグメント動画が欠けています: {p.name} — 連結を中止")
            return
        seg_paths.append(p)

    final = cfg.GENERATED_DIR / f"i2v6_{run_id}_final.mp4"
    print(f"\n[i2v-tl6] ffmpegで再連結中 ({len(seg_paths)}クリップ)...")
    if _concat_segments(seg_paths, final, "[i2v-tl6]"):
        print(f"\n[i2v-tl6] 完了! セグメント {targets} を再生成 → final 再連結")
        print(f"[i2v-tl6] 最終動画: {final}")


async def _generate_ground_truth(
    scene_desc: str, global_desc: str, ambience: str, duration: int, orientation: str,
    direction: str, character_line: str, action: str, reserved_props: list[str] | None, state: str,
) -> str:
    """t2vのPass3(LTX Formatter、C17/C18の衣装/STATE検品・`_enforce_character_line`の
    決定論的挿入・Pass4 lint込み)をそのまま流用し、このセグメントで確実に正しい
    「見えるもの・起きること」の基準テキストを生成する(2026-07-08)。

    i2vのKeyframe/Motion Formatterがscene_descから各々独立に見た目・動きを再導出する
    設計だと、Keyframe Formatterの40-80語という短い予算の中で衣装が他の要素と競合し、
    tightショットで脱落する事故が実データ(test_t2v_market.txt seg3、黄色いレインコート
    脱落)で発覚した。t2vのPass3は140-180語の余裕+キャラクター独立段落+専用チェックで
    同じ状況でも確実に衣装を保持することを実データで確認済みのため、i2vはゼロから
    再導出せず、まずこの基準テキストを作ってからキーフレーム/モーション両方を
    そこから抽出する(ユーザー指摘: 「i2vはt2vのキーフレームを作り、映像を補完する
    もの以外の何者でもない」)。"""
    return await _t2v_pass3._format_to_ltx_prompt(
        scene_desc, global_desc, ambience, duration, orientation, direction,
        character_line, action, reserved_props, state,
    )


async def _process_segment_phase1(
    i: int, seg: dict, direction: str, intent: str, total: int,
    global_desc: str, segments: list[dict], ambience: str, style_tail: str,
    orientation: str, character_line: str, llm_sem: asyncio.Semaphore,
) -> dict:
    """フェーズ1: 1セグメント完結のテキスト確定処理(Pass2→Pass3(基準テキスト、t2vのPass3流用)→
    Pass3a/3b)。キーフレーム画像生成は含まない。
    プロンプトのテキストはLLMパスだけで確定し画像/動画生成の結果に依存しないため、
    `prompts.txt`をキーフレーム画像生成の前に書き出せるようフェーズ2(画像生成)から
    分離した(2026-07-10、ユーザー指摘: テキストが生成前に確定しているなら先に出してほしい)。
    他セグメントの結果に依存しないため`main()`から`asyncio.gather()`で並列実行できるよう
    切り出した(2026-07-07)。LLM呼び出し(Pass2/Pass3a/3b)は`llm_sem`(最大4)で同時実行数を絞る。"""
    print(f"[i2v-tl6] [{i}/{total}] {seg['duration']}s  ({direction})")
    reserved = _reserved_props_for(segments, i - 1)
    print(f"[i2v-tl6]   [{i}/{total}] Pass2: シーン記述中(LLM)...")
    async with llm_sem:
        scene_desc = await _write_scene_description(global_desc, seg["action"], direction, seg["duration"], orientation, _SCENE_WRITER_SYSTEM, intent, reserved)
    # actionの要素脱落・動物不在の物語を検出したらPass2を差し戻す。従来は1回だけ差し戻して
    # 結果を無条件採用していたが、それでは差し戻しの意味がない(2026-07-10、ユーザー指摘:
    # 検証は結果を見て行動して初めて検証と言える)。解消するか`_LINT_MAX_ATTEMPTS`回試すまで繰り返す
    reject = _scene_reject_reason(scene_desc, seg["action"])
    attempt = 1
    while reject and attempt < _LINT_MAX_ATTEMPTS:
        attempt += 1
        print(f"[i2v-tl6]   [{i}/{total}] Pass2 差し戻し(試行{attempt}): {reject}")
        async with llm_sem:
            scene_desc = await _write_scene_description(
                global_desc, seg["action"], direction, seg["duration"], orientation, _SCENE_WRITER_SYSTEM, intent, reserved,
                retry_note=f"{reject}. Every element written in the shot action must be visibly present in the scene from the first frame.")
        reject = _scene_reject_reason(scene_desc, seg["action"])
    if reject:
        print(f"[i2v-tl6]   [{i}/{total}] Pass2 未解消(最大{_LINT_MAX_ATTEMPTS}回試行後): {reject}")
    print(f"[i2v-tl6]   [{i}/{total}] scene:\n{scene_desc}")

    current_state = _extract_state_from_intent(intent)
    print(f"[i2v-tl6]   [{i}/{total}] Pass3: 基準テキスト生成中(LLM, t2vのPass3を流用)...")
    async with llm_sem:
        ground_truth = await _generate_ground_truth(scene_desc, global_desc, ambience, seg["duration"], orientation, direction, character_line, seg["action"], reserved, current_state)
    print(f"[i2v-tl6]   [{i}/{total}] ground truth:\n{ground_truth}")

    print(f"[i2v-tl6]   [{i}/{total}] Pass3a: キーフレームプロンプト化(LLM)...")
    async with llm_sem:
        kf_prompt = await _format_keyframe_prompt(ground_truth, global_desc, seg["duration"], orientation, direction, character_line, seg["action"], reserved, style_tail, current_state)
    print(f"[i2v-tl6]   [{i}/{total}] keyframe prompt:\n{kf_prompt}")

    print(f"[i2v-tl6]   [{i}/{total}] Pass3b: 動きプロンプト化(LLM)...")
    async with llm_sem:
        motion_prompt = await _format_motion_prompt(ground_truth, ambience, seg["duration"], orientation, direction, seg["action"])
    print(f"[i2v-tl6]   [{i}/{total}] motion prompt:\n{motion_prompt}")

    prompt_block = (
        f"[{i}/{total}] {seg['label']} ({seg['duration']}s)\n"
        f"Intent: {intent}\n"
        f"Camera: {direction}\n"
        f"--- Scene ---\n{scene_desc}\n"
        f"--- Ground truth (t2v Pass3流用) ---\n{ground_truth}\n"
        f"--- Keyframe prompt ---\n{kf_prompt}\n"
        f"--- LTX prompt ---\n{motion_prompt}\n"
    )
    return {"seg": seg, "num": i, "motion": motion_prompt, "kf_prompt": kf_prompt, "prompt_block": prompt_block}


async def _process_segment_phase2(
    item: dict, total: int, width: int, height: int, ts: str, out_dir: Path, run_seed: int,
) -> dict:
    """フェーズ2: 1セグメント完結のキーフレーム画像生成処理(2026-07-10、テキスト確定
    フェーズから分離)。ComfyUI自体がジョブキューを持つため、無制限に並列実行する。
    `main()`はdebug時にフェーズ1直後(このフェーズの前)でreturnするため、ここは
    常に実際に画像生成する経路としてよい(debug分岐は持たない)。"""
    seg, i = item["seg"], item["num"]
    print(f"[i2v-tl6]   [{i}/{total}] キーフレーム画像生成中(image server)...")
    kf_width, kf_height = _keyframe_size(width, height)
    kf_raw = await generate_image(
        item["kf_prompt"], kf_width, kf_height, seed=run_seed,
        lora_name=cfg.KEYFRAME_LORA_NAME, lora_strength=cfg.KEYFRAME_LORA_STRENGTH)
    kf_path = out_dir / f"i2v6_{ts}_seg{i:02d}_kf.png"
    kf_raw.rename(kf_path)
    print(f"[i2v-tl6]   [{i}/{total}] キーフレーム保存: {kf_path.name}")
    return {"seg": seg, "num": i, "motion": item["motion"], "kf_path": kf_path}


async def _process_segment_phase3(item: dict, total: int, width: int, height: int, ts: str, bypass_likeness: bool = False) -> Path:
    """フェーズ3: 1セグメント完結の非同期処理(キーフレームアップロード→I2V動画生成)。
    ComfyUI自体がジョブキューを持つため、無制限に並列実行する(2026-07-07、旧フェーズ2)。
    bypass_likenessは"refine"エンジンでのみ意味を持つ(`--norefine`、顔occlusion時の破綻対策)。"""
    seg, i = item["seg"], item["num"]
    print(f"[i2v-tl6] [{i}/{total}] キーフレームをアップロード中...")
    kf_server_name = await upload_image_to_comfyui(item["kf_path"])
    print(f"[i2v-tl6] [{i}/{total}] {seg['duration']}s 動画生成中(I2V)...")
    raw_path = await _generate_i2v_video(item["motion"], kf_server_name, width, height, seg["duration"], bypass_likeness=bypass_likeness)
    named = _seg_video_path(ts, i, seg["label"], "i2v6")
    raw_path.rename(named)
    print(f"[i2v-tl6]   [{i}/{total}] 保存: {named.name}")
    return named


async def main() -> None:
    start_time = time.monotonic()
    parser = argparse.ArgumentParser(description="キーフレームI2V方式のタイムライン生成CLI(Z-Imageで1stフレーム生成→LTX-2.3 I2V)")
    orient = parser.add_mutually_exclusive_group()
    orient.add_argument("--h", action="store_true", dest="horizontal", help="横向き 1280×720(デフォルト)")
    orient.add_argument("--v", action="store_true", dest="vertical",   help="縦向き 720×1280")
    parser.add_argument("--f", metavar="FILE.txt", help="プロンプトファイル(通常実行時必須)")
    parser.add_argument("--debug", action="store_true", help="プロンプト生成のみ、動画生成をスキップ")
    parser.add_argument("--retry", metavar="RUN_ID", help="既存runのセグメントを再生成(例: --retry 20260704_080510)。--seg 必須(directモードのrunは省略可)、--f とは排他")
    parser.add_argument("--seg", metavar="N[,N...]", help="--retry で再生成するセグメント番号(1始まり、カンマ区切り)")
    parser.add_argument("--norefine", action="store_true",
                         help="I2V_VIDEO_ENGINE=refine時、LTX Likeness Anchorを全セグメントでbypassする"
                              "(顔が手/物で隠れるセグメントで画像が破綻する対策)。--f・--retry --seg・--direct どれでも使える")
    parser.add_argument("--keep", action="store_true",
                         help="--retry --seg 専用。既存のキーフレーム画像をそのまま使い、動画生成だけをやり直す"
                              "(キーフレームは問題なく動画側だけ壊れた場合に、新seedでのキーフレーム再生成を省く)")
    parser.add_argument("--direct", metavar="SECONDS", type=float,
                         help="デバッグ用: Pass0〜4のLLMパイプラインを一切通さず、--fのファイル内容を"
                              "そのままComfyUIに渡してSECONDS秒の動画を1本だけ生成する。"
                              "ファイルに'--- Keyframe prompt ---'区切りがあればKeyframe/Motionを分離、無ければ全文を両方に使う。"
                              "--retry / --seg / --upscale / --debug とは排他。--keepは新規生成のため無効(指定時は警告のうえ無視)")
    parser.add_argument("--upscale", metavar="RUN_ID", nargs="?", const="",
                         help="既存runの最終動画(_final.mp4)をRTX Video Super ResolutionでフルHDにアップスケール。"
                              "RUN_ID省略で直近run(例: --upscale / --upscale 20260704_080510)。他の引数とは排他")
    args = parser.parse_args()

    if args.direct is not None:
        if args.retry or args.seg or args.upscale is not None or args.debug:
            parser.error("--direct は --retry / --seg / --upscale / --debug と同時に指定できません")
        if not args.f:
            parser.error("--direct には --f が必要です")
        if args.keep:
            print(f"[i2v-tl6] 警告: --keep は新規の--direct生成では無効です(既存キーフレームが無いため無視)")
        if args.norefine and cfg.I2V_VIDEO_ENGINE != "refine":
            print(f"[i2v-tl6] 警告: --norefine が指定されましたが I2V_VIDEO_ENGINE={cfg.I2V_VIDEO_ENGINE!r} のため無視されます(refine時のみ有効)")
        await _run_direct(args)
        print(f"[i2v-tl6] 経過時間: {_fmt_elapsed(start_time)}\n")
        return

    if args.keep and not args.seg and not args.retry:
        parser.error("--keep は --seg と併用してください(--retry --seg --keep、または --seg --keep で直近run)")

    if args.upscale is not None:
        if args.f or args.retry or args.seg:
            parser.error("--upscale は --f / --retry / --seg と同時に指定できません")
        await _run_upscale("i2v6", args.upscale or None, "[i2v-tl6]")
        print(f"[i2v-tl6] 経過時間: {_fmt_elapsed(start_time)}\n")
        return

    if args.seg and not args.retry:
        # --retry 省略時は直近のrunを使用
        cands = list(cfg.GENERATED_DIR.glob("i2v6_*_prompts.txt"))
        if not cands:
            parser.error("generated/ に i2v6_*_prompts.txt が見つかりません(--retry RUN_ID で指定してください)")
        latest = max(cands, key=lambda p: p.stat().st_mtime)
        args.retry = latest.name
        print(f"[i2v-tl6] --retry 省略: 直近のrunを使用 → {latest.name}")

    if args.retry:
        if args.f:
            parser.error("--retry と --f は同時に指定できません")
        # --seg必須チェックはここでは行わない(directモードのrunは省略可のため)。
        # _run_retry()がprompts.txtヘッダーを読んでから判定する。
        await _run_retry(args)
        print(f"[i2v-tl6] 経過時間: {_fmt_elapsed(start_time)}\n")
        return
    if not args.f:
        parser.error("--f が必要です(リトライは --retry RUN_ID --seg N、または --seg N のみで直近run)")

    if args.norefine and cfg.I2V_VIDEO_ENGINE != "refine":
        print(f"[i2v-tl6] 警告: --norefine が指定されましたが I2V_VIDEO_ENGINE={cfg.I2V_VIDEO_ENGINE!r} のため無視されます(refine時のみ有効)")

    width, height = (720, 1280) if args.vertical else (1280, 720)
    orient_label  = "縦 720×1280" if args.vertical else "横 1280×720"
    orientation   = "vertical" if args.vertical else "horizontal"
    debug         = args.debug

    prompt_path = Path(args.f)
    if not prompt_path.exists():
        print(f"[i2v-tl6] ファイルが見つかりません: {prompt_path}")
        return

    text = prompt_path.read_text(encoding="utf-8")

    auto_segmented = False
    try:
        global_desc, segments, ambience = _parse_prompt(text)
    except ValueError as e:
        if "ヘッダーもタイムスタンプも見つかりません" not in str(e):
            print(f"[i2v-tl6] パースエラー: {e}")
            return
        print(f"[i2v-tl6] タイムライン形式が見つかりません — Pass -1で自動分割します(目安15秒、3秒ビート基本)...")
        text = await _auto_segment_narrative(text)
        print(f"[i2v-tl6] 自動分割結果:\n{text}\n")
        try:
            global_desc, segments, ambience = _parse_prompt(text)
        except ValueError as e2:
            print(f"[i2v-tl6] 自動分割後もパースエラー: {e2}")
            return
        auto_segmented = True

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = cfg.GENERATED_DIR
    prompts_txt = out_dir / f"i2v6_{ts}_prompts.txt"

    mode_label = " [DEBUG — プロンプト確認のみ]" if debug else ""
    print(f"[i2v-tl6] {prompt_path.name} / {orient_label} / セグメント数: {len(segments)} / 合計尺: {segments[-1]['end']}秒{mode_label}")

    # キャラクター記述の抽出(全セグメントのLTXプロンプトに強制挿入するため)。
    # 髪型・メガネも固定アイデンティティとしてここに含まれる(2026-07-10、V6。
    # STATE側の部分更新でこの2つが丸ごと落ちる事故が起きたため、STATEに個別の補完
    # 機構を作るのではなく、既に確実な_enforce_character_lineの強制注入に一本化した)
    print(f"\n[i2v-tl6] キャラクター記述を抽出中(LLM)...")
    character_line = await _extract_character_line(global_desc)
    print(f"[i2v-tl6]   character: {character_line or '(なし)'}")
    style_tail = await _extract_style_line(global_desc)
    print(f"[i2v-tl6]   style: {style_tail}")

    # Pass0a/0b: 創造(INTENT/HIGHLIGHT/TEMPO)と継続性記録(STATE)を専属パスに分けて並列実行
    # (2026-07-10、V6。互いに依存しないため並列でもレイテンシはほぼ変わらない)
    print(f"\n[i2v-tl6] Pass0a/0b クリエイティブディレクター+Stateトラッカー: 並列計画中({len(segments)}セグメント)...")
    creative_lines, state_lines_raw = await asyncio.gather(
        _plan_creative_intent(global_desc, segments, orientation, _CREATIVE_DIRECTOR_SYSTEM),
        _plan_creative_intent(global_desc, segments, orientation, _STATE_TRACKER_SYSTEM),
    )
    resolved_states = _resolve_state_continuity(state_lines_raw)
    intents = [f"{c} | STATE: {s}" for c, s in zip(creative_lines, resolved_states)]
    for i, (seg, intent) in enumerate(zip(segments, intents), 1):
        print(f"[i2v-tl6]   [{i:02d}] {seg['label']:>14}  {intent}")
    print()

    # Pass1: ショットディレクターが構図を一括計画(intentを参照)
    print(f"[i2v-tl6] Pass1 ショットディレクター: 構図を計画中...")
    shot_directions = await _plan_shot_directions(global_desc, segments, orientation, intents, _SHOT_DIRECTOR_SYSTEM)
    for i, (seg, direction) in enumerate(zip(segments, shot_directions), 1):
        print(f"[i2v-tl6]   [{i:02d}] {seg['label']:>14}  {direction}")
    print()

    # Pass1.5: 多様性監査(向き・カメラ位置・接近の単調さを俯瞰チェックして書き直し)
    print(f"[i2v-tl6] Pass1.5 多様性監査: 向き・カメラ位置をチェック中...")
    audited = await _audit_shot_variety(segments, shot_directions, orientation, _VARIETY_AUDITOR_SYSTEM)
    audited = _enforce_walking_lateral(segments, audited, orientation, "[i2v-tl6]")
    for i, (before, after) in enumerate(zip(shot_directions, audited), 1):
        mark = "＊" if after != before else " "
        print(f"[i2v-tl6]  {mark}[{i:02d}] {after}")
    shot_directions = audited
    print()

    # 全キーフレームで同一seedを使用(LoRAなし運用時のキャラ一貫性向上)
    run_seed = random.randint(0, 2**32 - 1)
    print(f"[i2v-tl6] keyframe seed: {run_seed} / LoRA: {cfg.KEYFRAME_LORA_NAME or '(image.jsonのまま)'} "
          f"strength: {cfg.KEYFRAME_LORA_STRENGTH if cfg.KEYFRAME_LORA_STRENGTH >= 0 else '(そのまま)'}")

    # 各セグメントの処理は他セグメントの結果に依存しないため、asyncio.gather()で並列実行する
    # (2026-07-07)。フェーズ1(テキスト確定)・フェーズ2(キーフレーム画像生成)・フェーズ3
    # (動画生成)の境界は維持する(異なるワークフロー/モデルへのリクエストがComfyUIのキューで
    # 混在してモデル再ロードが発生するのを避けるため)。LLM呼び出しはSemaphore(4)で同時実行数を
    # 制限し、画像/動画生成(ComfyUI)は無制限のまま投げる。
    llm_sem = asyncio.Semaphore(4)

    # ===== フェーズ1: 全セグメントのテキスト確定(Pass2→Pass3→Pass3a/3b) =====
    # プロンプトのテキストはLLMパスだけで確定し画像/動画生成の結果に依存しないため、
    # キーフレーム画像生成(フェーズ2)より前にprompts.txtを書き出す(2026-07-10、
    # ユーザー指摘: テキストが生成前に確定しているなら先に出してほしい)
    phase1_tasks = [
        _process_segment_phase1(i, seg, direction, intent, len(segments), global_desc, segments,
                                 ambience, style_tail, orientation, character_line, llm_sem)
        for i, (seg, direction, intent) in enumerate(zip(segments, shot_directions, intents), 1)
    ]
    phase1_results = await asyncio.gather(*phase1_tasks)

    prompt_lines: list[str] = [
        f"source: {prompt_path.name}",
        f"orientation: {orientation}",
        f"size: {width}x{height}",
        f"keyframe_seed: {run_seed}",
        f"auto_segmented: {str(auto_segmented).lower()}",
        "",
    ]
    for r in phase1_results:
        prompt_lines.append(r["prompt_block"])

    # prompts.txt はテキストが確定した時点(キーフレーム画像生成の前)で書き出す
    prompts_txt.write_text("\n".join(prompt_lines), encoding="utf-8")
    print(f"\n[i2v-tl6] プロンプト保存: {prompts_txt.name}")

    llm_done_time = time.monotonic()

    if debug:
        print(f"\n[i2v-tl6] [DEBUG] 完了 — キーフレーム・動画生成はスキップされました")
        print(f"[i2v-tl6] プロンプト確認: {prompts_txt}")
        print(f"[i2v-tl6] LLM時間: {_fmt_duration(llm_done_time - start_time)}\n")
        return

    # ===== フェーズ2: 全セグメントのキーフレーム画像生成(image GPU) =====
    # 動画生成(video GPU)とサーバーが別なので、キーフレームを先に全部作る。
    # 副次効果: 動画フェーズの前にキーフレームを一括目視できる
    # (動画フェーズでクラッシュしてもプロンプト・キーフレームが残り --seg で再開可能)
    phase2_tasks = [
        _process_segment_phase2(r, len(phase1_results), width, height, ts, out_dir, run_seed)
        for r in phase1_results
    ]
    prepared = await asyncio.gather(*phase2_tasks)

    print(f"[i2v-tl6] キーフレーム{len(prepared)}枚 生成完了: generated/i2v6_{ts}_seg*_kf.png")
    print(f"[i2v-tl6] フェーズ1+2(プロンプト+キーフレーム)経過時間: {_fmt_elapsed(start_time)}\n")
    print(f"[i2v-tl6] (動画フェーズ中に目視確認できます。中断する場合は Ctrl+C → ダメなセグメントは --seg N で引き直し)\n")

    # ===== フェーズ3: 全セグメントの動画生成(video GPU) =====
    phase3_tasks = [_process_segment_phase3(item, len(prepared), width, height, ts, bypass_likeness=args.norefine) for item in prepared]
    seg_paths: list[Path] = list(await asyncio.gather(*phase3_tasks))

    # ffmpeg concat
    print(f"\n[i2v-tl6] ffmpegで連結中 ({len(seg_paths)}クリップ)...")
    final = out_dir / f"i2v6_{ts}_final.mp4"
    if not _concat_segments(seg_paths, final, "[i2v-tl6]"):
        return

    print(f"\n[i2v-tl6] 完了!")
    print(f"[i2v-tl6] 最終動画: {final}")
    print(f"[i2v-tl6] セグメント({len(seg_paths)}個): generated/i2v6_{ts}_seg*.mp4")
    finish_time = time.monotonic()
    print(f"[i2v-tl6] LLM時間: {_fmt_duration(llm_done_time - start_time)} / "
          f"生成時間: {_fmt_duration(finish_time - llm_done_time)} / "
          f"合計時間: {_fmt_duration(finish_time - start_time)}\n")


if __name__ == "__main__":
    asyncio.run(main())
