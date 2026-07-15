import fs from "node:fs";
import path from "node:path";
import { GENERATED_DIR, CASS_DIR, PROMPT_DIR, UPLOADS_DIR } from "./config.js";

// 配信を許可するルート(すべて本フォルダ直下の作業ディレクトリ)
const ALLOWED_ROOTS = [GENERATED_DIR, CASS_DIR, PROMPT_DIR, UPLOADS_DIR];

/** GET /media?p=<absolute path> — 許可ルート配下のみ配信。
 * express の res.sendFile は Range リクエスト対応なので動画のシークもそのまま効く。 */
export function mediaHandler(req, res) {
  const p = req.query.p;
  if (!p || typeof p !== "string") return res.status(400).json({ error: "missing ?p=" });
  let real;
  try {
    real = fs.realpathSync(path.resolve(p));
  } catch {
    return res.status(404).json({ error: "not found" });
  }
  const allowed = ALLOWED_ROOTS.some((root) => {
    try {
      const realRoot = fs.realpathSync(root);
      return real === realRoot || real.startsWith(realRoot + path.sep);
    } catch {
      return false;
    }
  });
  if (!allowed) return res.status(403).json({ error: "path not allowed" });
  // filename を明示しないと保存時に "media" になる / no-store: retry・commitで同名
  // ファイルが上書きされるため、ブラウザキャッシュの古い内容を出さない
  const filename = path.basename(real);
  res.sendFile(real, {
    acceptRanges: true,
    headers: {
      "Content-Disposition": `inline; filename*=UTF-8''${encodeURIComponent(filename)}`,
      "Cache-Control": "no-store",
    },
  });
}
