import { cp, mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const docsApp = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repository = path.resolve(docsApp, "../..");
const output = path.join(docsApp, ".pages");

await rm(output, { recursive: true, force: true });
await mkdir(path.join(output, "docs"), { recursive: true });
await cp(path.join(repository, "apps/web/public/landing.html"), path.join(output, "index.html"));
await cp(path.join(docsApp, ".vitepress/dist"), path.join(output, "docs"), { recursive: true });
await writeFile(path.join(output, ".nojekyll"), "", "utf8");

console.log(`Assembled landing and documentation in ${output}`);
