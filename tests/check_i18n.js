// Every translation key the app can show exists in English and Chinese, and
// the two languages have exactly the same keys.
//
//   node tests/check_i18n.js
const fs = require("fs");
const path = require("path");
const root = path.join(__dirname, "..");
const read = (f) => fs.readFileSync(path.join(root, f), "utf8");

const src = read("ui/i18n.js");
const I18N = eval(src.replace(/let LANG[\s\S]*/, "") + ";I18N");
const problems = [];

const en = Object.keys(I18N.en), zh = Object.keys(I18N.zh);
for (const k of en) if (!(k in I18N.zh)) problems.push("missing in zh: " + k);
for (const k of zh) if (!(k in I18N.en)) problems.push("missing in en: " + k);

const used = new Set();
for (const m of read("ui/app.js").matchAll(/\bt\("([a-z_][a-zA-Z0-9_.]*)"/g)) used.add(m[1]);
for (const m of read("ui/index.html").matchAll(/data-i18n="([^"]+)"/g)) used.add(m[1]);
for (const f of ["cortex_client.py", "engine.py", "spotify/auth.py", "spotify/player.py"]) {
  for (const m of read(f).matchAll(/"((?:err|status|log)\.[a-zA-Z0-9_.]+)"/g)) used.add(m[1]);
}
// Keys the code builds from a prefix plus a value.
const dynamic = {
  "step.": ["credentials", "cortex", "access", "headset", "session", "profile", "stream", "spotify"],
  "player.": ["none", "play_pause", "play", "pause", "next", "previous", "volume_up", "volume_down", "shuffle"],
  "quality.grade.": ["0", "1", "2", "3", "4"],
  "quality.verdict.": ["good", "fair", "poor"],
  "quality.help.": ["cq", "eq"],
  "log.fired.": ["mind", "click"],
};
for (const [prefix, names] of Object.entries(dynamic)) for (const n of names) used.add(prefix + n);

for (const k of used) {
  if (k.endsWith(".")) continue;
  if (!(k in I18N.en)) problems.push("used but untranslated: " + k);
}

// Nothing left over from the light this interface was built from.
for (const [lang, table] of Object.entries(I18N)) {
  for (const [k, v] of Object.entries(table)) {
    if (/\b(light|bulb|lamp)\b|灯/i.test(v)) problems.push(`mentions a light (${lang}): ${k} = ${v}`);
  }
}

if (problems.length) {
  console.error(problems.join("\n"));
  process.exit(1);
}
console.log(`i18n ok: ${en.length} keys in both languages, ${used.size} referenced`);
