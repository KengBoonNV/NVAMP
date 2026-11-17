// Builds frontend/index.html from the original NVAMP page (../app/index.html).
// Run from substrait-app/:  node tools/build-frontend.mjs
// Each replacement must match exactly the expected number of times, or the build stops,
// so a change to the source page can't silently leave a claude.ai-only path behind.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "../../app/index.html");
const out = resolve(here, "../frontend/index.html");
let html = readFileSync(src, "utf8");

const swaps = [
  // file links: claude.ai served uploads at /_blob/<id>; the backend serves them at /api/blob/<id>
  { from: "/_blob/", to: "/api/blob/", min: 5 },
  // load the runtime adapter before the page's own script so window.claude exists at startup
  { from: "</head>", to: '<script src="/nvamp-runtime.js"></script>\n</head>', count: 1 },
  { from: "Open this page on claude.ai while signed in to see and update your team's records.",
    to: "Sign in with your company Google account to see and update your team's records.", count: 1 },
  { from: "people use their own company account on claude.ai.",
    to: "people sign in with their own company Google account.", count: 1 },
  { from: "After adding, share NVAMP with them from the page's Share menu on claude.ai: Contributor for most roles, Editor for Admins, Viewer for Viewers.",
    to: "After adding, they get these roles the first time they open NVAMP with their company Google account.", count: 1 },
  { from: "Everyone except Viewers also needs <b>Contributor</b> access (Admins need <b>Editor</b>) in the page's Share menu on claude.ai. In this pilot, roles are enforced in the page. The production system must enforce them on the server.",
    to: "Anyone in the company Google account can sign in. Roles are enforced in the page; the server enforces sign-in, read-only accounts and personal records.", count: 1 },
];

for (const s of swaps) {
  const n = html.split(s.from).length - 1;
  if ((s.count !== undefined && n !== s.count) || (s.min !== undefined && n < s.min)) {
    console.error(`build-frontend: expected ${s.count ?? ">=" + s.min} of ${JSON.stringify(s.from.slice(0, 60))}, found ${n}`);
    process.exit(1);
  }
  html = html.split(s.from).join(s.to);
}
if (/claude\.ai/.test(html)) {
  console.error("build-frontend: a claude.ai reference is left in the page:", html.match(/.{60}claude\.ai.{60}/)[0]);
  process.exit(1);
}
writeFileSync(out, html);
console.log(`build-frontend: wrote ${out} (${html.length} bytes)`);
