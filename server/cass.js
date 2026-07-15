import fs from "node:fs";
import path from "node:path";
import { CASS_DIR, CASS_OUTPUT_DIR, BGM_DIR } from "./config.js";
import { condaPythonArgs, streamCommand, runBridge } from "./proc.js";
import { createJob } from "./jobs.js";

/** CASS(音声分離+BGMミックス)。Gradio版 on_cass の移植:
 * process.sh は内部で separate.py をテンポラリに対して実行して消してしまうため、
 * ステム表示用にこちらでも一度 separate.py を直接呼んで永続的な場所に保存する
 * (抽出+分離が二重に走るのは既存スクリプト無改変を優先した設計、Gradio版と同じ)。 */
export function startCass({ videoPath, bgmPath = null, volume = 0.6 }) {
  return createJob("cass", async (job) => {
    const videoName = path.parse(videoPath).name;
    const stemsDir = path.join(CASS_OUTPUT_DIR, `${videoName}_stems`);
    fs.mkdirSync(stemsDir, { recursive: true });
    const audioTmp = path.join(stemsDir, "extracted_48k_mono.wav");

    job.appendLog(`video: ${path.basename(videoPath)}\n`);
    job.appendLog(bgmPath ? `bgm: ${path.basename(bgmPath)} (volume=${volume})\n` : "bgm: none (speech+sfx only)\n");

    const run = async (cmd, args, cwd) => {
      const code = await streamCommand(cmd, args, { cwd, onLine: (l) => job.appendLog(l), signal: job.signal });
      if (code !== 0) throw new Error(`command failed (exit ${code}): ${path.basename(cmd)} ${args[0] ?? ""}`);
    };

    job.appendLog("\n[1/3] extracting audio...\n");
    await run("ffmpeg", ["-y", "-loglevel", "error", "-i", videoPath, "-vn", "-ac", "1", "-ar", "48000", audioTmp]);

    job.appendLog("\n[2/3] separating stems (BandIt v2, for display)...\n");
    const [cmd, args] = condaPythonArgs("CASS", "separate.py", ["--audio", audioTmp, "--out", stemsDir]);
    await run(cmd, args, CASS_DIR);
    job.setState({
      voice: path.join(stemsDir, "speech.wav"),
      sfx: path.join(stemsDir, "sfx.wav"),
      bgmOrig: path.join(stemsDir, "music.wav"),
    });

    job.appendLog("\n[3/3] mixing + remuxing (process.sh)...\n");
    const shArgs = [videoPath];
    if (bgmPath) shArgs.push(bgmPath, String(volume));
    await run(path.join(CASS_DIR, "process.sh"), shArgs, CASS_DIR);

    const outPath = path.join(CASS_OUTPUT_DIR, `${videoName}_remixed.mp4`);
    if (!fs.existsSync(outPath)) throw new Error(`expected output not found: ${outPath}`);
    job.appendLog(`\nDone: ${outPath}\n`);
    job.setState({ result: outPath });
  });
}

/** ACE-Step-1.5でBGMを2テイク生成(bridge経由で bgm_generate_cli.generate_bgm を呼ぶ。
 * 出力先は CASS/bgm/ = Fileモードの一覧にもそのまま出る、Gradio版と同じ)。 */
export function startBgmGenerate({ prompt, duration }) {
  return createJob("bgm", async (job) => {
    job.appendLog(`Generating BGM (${duration}s x 2 takes)...\nprompt: ${prompt}\n`);
    job.appendLog("(モデル未ロードの場合は初回のみ数分かかります)\n");
    const res = await runBridge(["bgm"], {
      stdin: JSON.stringify({ prompt, duration, takes: 2, out_dir: BGM_DIR }),
    });
    job.appendLog(`\nDone: ${res.takes.length} takes\n`);
    job.setState({ takes: res.takes });
  });
}

/** RTX Video Super Resolution アップスケール(bridge経由、{stem}_FHD.mp4 を同じ場所に保存) */
export function startUpscale({ videoPath }) {
  return createJob("upscale", async (job) => {
    job.appendLog(`Upscaling: ${path.basename(videoPath)}\n(RTX Video Super Resolution, 1.5x ULTRA)\n`);
    const res = await runBridge(["upscale", videoPath]);
    job.appendLog(`\nDone: ${res.out}\n`);
    job.setState({ result: res.out });
  });
}
