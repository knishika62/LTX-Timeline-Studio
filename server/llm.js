import OpenAI from "openai";
import { readEnv } from "./config.js";
import { validatePrompt } from "./prompts.js";

// ============================================================
// システムプロンプト5本は v6_harness_v2.py から忠実にコピー(挙動同一性のため変更しない)
// ============================================================

const SETUP_SYSTEM = `You are setting up the fixed elements of a scene for an AI video generation pipeline.
Given a short Japanese or English keyword/idea from the user, decide ONLY the following
— do not write any timeline/action beats yet, that is a separate step done later:

Main Subject:
<1-2 sentences: nationality/age/hair/build, and her CURRENT/STARTING wardrobe only. If
the story will include a costume change later, do NOT mention the later outfit here at
all — only describe what she is wearing at the very start.>

Location:
<1-2 sentences: where the scene takes place, including any secondary locations if she
moves during the story>

Visual Style:
<short phrase, e.g. "Ultra-realistic observational documentary, natural lighting">

Camera Style:
<short phrase, e.g. "Handheld, natural micro-shake, occasional soft refocus">

Audio:
<comma-separated ambient sounds fitting the scene overall>

Example output:

Main Subject:
Young Japanese woman, mid-20s, black bob hair, wearing a white crop tank top and denim shorts, warm natural smile, sun-kissed skin.

Location:
A sunlit seaside boardwalk in early afternoon, transitioning to a small rooftop terrace overlooking the ocean at sunset.

Visual Style:
Ultra-realistic observational documentary, warm summer color palette, candid slice-of-life mood, natural lighting.

Camera Style:
Handheld, natural micro-shake, occasional soft refocus, no cinematic gimbal moves.

Audio:
Rolling waves, distant seagulls, café chatter, warm evening breeze, footsteps on wood

If the user didn't specify appearance/wardrobe/location details, invent reasonable,
concrete ones (not abstract). Output ONLY these five labeled fields, nothing else —
no timeline, no commentary.
`;

const BEAT_PLAN_SYSTEM = `You are planning the shot-by-shot beat structure of a short scene for an AI video
generation pipeline, given the scene setup below and a target total duration. Decide
WHAT happens in each beat and HOW LONG it should run — do not write final prose yet,
just a rough one-line gist per beat.

Output ONLY a numbered list, one beat per line, in this exact format:
1. (Xs) one-line gist of what happens in this beat
2. (Ys) ...

Example (for the seaside-boardwalk-to-terrace setup):
1. (3s) She walks along the sunlit beach boardwalk, drink in hand, hair lifting in the sea breeze.
2. (4s) She stops at the railing, takes a photo, laughs and says something about how nice the shot came out.
3. (3s) She climbs the stairs to a rooftop terrace, glancing back at the ocean.
4. (3s) She sits at a terrace table, sipping her drink under the sunset.
5. (6s) She stands at the railing, watching the sunset, and quietly says something about how beautiful it is — this is the emotional highlight, give it more time.

Rules:
- You will be given the user's ORIGINAL idea/keywords in addition to the scene setup.
  The setup only covers fixed elements (identity/location/style) — it deliberately does
  NOT include story events like a costume change or an animal appearing. Make sure EVERY
  concrete story element from the original keywords (an action, a costume change, an
  animal, a prop, an emotional beat, etc.) ends up as its own beat somewhere in your
  list — if it isn't in the setup, it's your job to place it, not skip it.
- Each beat is at least 3 seconds.
- Do NOT split the total duration evenly across beats (notice the example above uses
  3/4/3/3/6 second beats, not uniform ones). Allocate time by importance: quick
  transitions, establishing beats, or simple actions can stay at the 3s minimum; a beat
  carrying a highlight moment, dialogue, an emotional payoff, or the story's climax/
  finale should run noticeably longer (5-8s+ if it needs room to land).
- Total duration should be close to the requested target, unless the story clearly needs
  more beats to make sense — don't pad with filler beats just to hit the number.
- If a costume/wardrobe change happens, it must be its own beat's gist (e.g. "she
  changes into a navy yukata"), stated nowhere else.
- If there's spoken dialogue, mention that in the gist (the exact final wording is
  written in a later step).
Output ONLY the numbered list, no commentary.
`;

const BEAT_WRITE_SYSTEM = `You are writing the final action-description prose for each beat of a scene, given the
scene setup and a list of beats (each already has its duration and a one-line gist —
your job is only to expand each gist into a vivid, concrete action description, one or
two sentences, matching its duration).

Example:
Input gist: "She stops at the railing, takes a photo, laughs and says something about how nice the shot came out. (duration 4s)"
Output line: "She stops at the railing, snaps a photo of the waves with her phone, then laughs and says, \\"いい写真撮れた!\\" (\\"Got a great shot!\\")."

Rules:
- Output ONLY a numbered list, one line per beat, matching the input beat numbers:
  1. <final prose for beat 1>
  2. <final prose for beat 2>
  ...
- Any spoken dialogue must be written in Japanese in quotes, with an English gloss in
  parentheses right after, exactly like the example.
- Do not restate Main Subject/Location details already established in the setup unless
  something changes in this specific beat (e.g. a costume change).
- Keep it natural and concrete, not abstract. No commentary, no extra headers.
`;

const SUMMARY_SYSTEM = `You are given an English scene prompt (with timestamped beats) for an AI video
generation pipeline. Summarize the flow of the scene in natural Japanese, in 2-4
sentences: what happens, in what order, across this shot sequence. Describe the overall
flow as if explaining it casually to a colleague — do not translate line-by-line.
Output ONLY the Japanese summary text, no preamble, no markdown.
`;

const BGM_DRAFT_SYSTEM = `You are given the global scene setup (Main Subject / Location / Visual Style / Camera Style)
of a short AI-generated video. Write ONE short description (1-2 sentences, English) of an
instrumental background music track that would fit this scene's mood: genre, key instruments,
emotional tone, and tempo (calm/mid/upbeat). No vocals, no lyrics. Output ONLY the description
text, no preamble, no markdown, no quotes.
`;

// ============================================================
// LLM呼び出し(.env は都度読む——ハーネス起動中の .env 変更を即反映)
// ============================================================

async function runLLM(system, user, temperature = 0.7) {
  const env = readEnv();
  const client = new OpenAI({ baseURL: env.LLM_BASE_URL, apiKey: env.LLM_API_KEY || "none" });
  const resp = await client.chat.completions.create({
    model: env.LLM_MODEL,
    messages: [
      { role: "system", content: system },
      // Qwen系のthinkingモード抑制はユーザーメッセージ先頭に /no_think を付与する
      { role: "user", content: `/no_think\n${user}` },
    ],
    temperature,
    max_tokens: 4096,
  });
  return (resp.choices[0]?.message?.content || "").trim();
}

function fmtTs(sec) {
  const m = String(Math.floor(sec / 60)).padStart(2, "0");
  const s = String(sec % 60).padStart(2, "0");
  return `${m}:${s}`;
}

/** "N. text" 形式の番号付きリストをパース(timeline_common._parse_numbered_lines 相当)。
 * 欠番は fallback(gist)で埋める。 */
function parseNumberedLines(raw, count) {
  const map = new Map();
  for (const line of raw.split("\n")) {
    const m = /^(\d+)[.:]\s*(.+)/.exec(line.trim());
    if (m) map.set(parseInt(m[1], 10), m[2].trim());
  }
  return Array.from({ length: count }, (_, i) => map.get(i + 1) || "");
}

/** キーワード → Timeline形式プロンプト(3パス構成、Gradio版 draft_timeline_prompt の移植)。
 * タイムスタンプはLLMに計算させず、Pass2の秒数をこちらで積算する(「事実はコード、文章はLLM」)。 */
export async function draftTimelinePrompt(keywords, durationHintS, minBeatS = 3) {
  const setupRaw = await runLLM(SETUP_SYSTEM, keywords.trim(), 0.8);

  // Pass2にはSetup結果だけでなく元キーワードも渡す(Setupは固定要素しか決めないため、
  // 「浴衣に着替える」「猫」のようなビート要素が silently 消える事故の再発防止、Gradio版と同じ)
  const planUser =
    `Original idea/keywords from the user:\n${keywords.trim()}\n\n` +
    `Scene setup (already decided):\n${setupRaw}\n\n` +
    `Target total duration: about ${durationHintS} seconds.`;
  const planRaw = await runLLM(BEAT_PLAN_SYSTEM, planUser, 0.8);

  const beats = [];
  for (const line of planRaw.split("\n")) {
    const m = /^\d+[.:]\s*\((\d+)s\)\s*(.+)/.exec(line.trim());
    if (m) beats.push({ dur: Math.max(minBeatS, parseInt(m[1], 10)), gist: m[2].trim() });
  }
  if (!beats.length) throw new Error(`Beat planning pass produced no parseable beats:\n${planRaw}`);

  const gistList = beats.map((b, i) => `${i + 1}. ${b.gist} (duration ${b.dur}s)`).join("\n");
  const writeRaw = await runLLM(BEAT_WRITE_SYSTEM, `Scene setup:\n${setupRaw}\n\nBeats to write:\n${gistList}`, 0.7);
  const prose = parseNumberedLines(writeRaw, beats.length);

  const lines = [];
  let t = 0;
  beats.forEach((b, i) => {
    lines.push(`${fmtTs(t)}–${fmtTs(t + b.dur)} → ${prose[i] || b.gist}`);
    t += b.dur;
  });

  // "Audio:" 以降を分離(ラベル直後・次行どちらに内容が来ても対応、Gradio版のバグ修正を踏襲)
  const parts = setupRaw.split(/^Audio:\s*/im);
  const header = parts[0].trim();
  const audioLine = parts.length >= 2 ? parts.slice(1).join("").trim() : "";

  return `${header}\n\n---\n\n${lines.join("\n")}\n\nAudio:\n${audioLine}`;
}

/** Timelineプロンプトの日本語要約。合計尺/セグメント数ヘッダーはパース結果から確定値を付ける。 */
export async function summarizePromptJa(promptText) {
  const summary = await runLLM(SUMMARY_SYSTEM, promptText.trim(), 0.5);
  const v = await validatePrompt(promptText);
  if (v.ok) return `合計尺: ${v.totalSeconds}秒 / セグメント数: ${v.segments}\n\n${summary}`;
  return summary;
}

/** タイムラインプロンプトの全体設定(global_desc)からBGM説明文を下書き */
export async function draftBgmPrompt(globalDesc) {
  return runLLM(BGM_DRAFT_SYSTEM, globalDesc.trim(), 0.8);
}
