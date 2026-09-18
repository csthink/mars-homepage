// 站点检查：构建产物中的每个页面必须有 <title>，站内链接必须指向存在的文件。
import { readdir, readFile, stat } from "node:fs/promises";
import { join, resolve, dirname, posix } from "node:path";

const dist = resolve("dist");
const problems = [];

async function walk(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await walk(path)));
    else if (entry.name.endsWith(".html")) out.push(path);
  }
  return out;
}

async function exists(path) {
  try {
    const s = await stat(path);
    if (s.isDirectory()) return exists(join(path, "index.html"));
    return true;
  } catch {
    return false;
  }
}

const pages = await walk(dist);
if (pages.length === 0) problems.push("dist 中没有页面，请先构建。");
for (const page of pages) {
  const html = await readFile(page, "utf8");
  const rel = posix.relative(dist, page);
  if (!/<title>[^<]+<\/title>/.test(html)) problems.push(`${rel}: 缺少 <title>`);
  for (const [, url] of html.matchAll(/(?:href|src)="([^"#?]+)[^"]*"/g)) {
    if (/^(https?:|mailto:|data:)/.test(url) || url.startsWith("//")) continue;
    const target = url.startsWith("/") ? join(dist, url) : join(dirname(page), url);
    if (!(await exists(target))) problems.push(`${rel}: 链接目标不存在 ${url}`);
  }
}

if (problems.length) {
  console.error(problems.join("\n"));
  process.exit(1);
}
console.log(`站点检查通过：${pages.length} 个页面，标题与站内链接均有效。`);
