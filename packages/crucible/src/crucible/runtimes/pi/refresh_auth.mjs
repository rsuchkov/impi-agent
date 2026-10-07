// Refresh every OAuth login in the runtime's credential file that expires
// within a margin, with the runtime's own code: its credential store (which
// locks the file and re-reads it before it writes) and its providers.
//
// Run by the engine, which may write the file, so that a confined runtime
// process — which may only read it — never finds a login it would have to
// refresh itself.
//
//   node refresh_auth.mjs <runtime package dir> <auth.json> <margin ms>
//
// An api_key entry is never resolved here: resolving one runs a "!" value as
// a command, and nothing about keeping a login fresh needs that.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [packageDir, authPath, marginMs] = process.argv.slice(2);
const { ModelRuntime } = await import(pathToFileURL(join(packageDir, "dist", "bundle", "index.js")).href);

const entries = JSON.parse(readFileSync(authPath, "utf-8"));
const runtime = await ModelRuntime.create({ authPath, modelsPath: null, refreshOnCreate: false });
let failed = 0;
for (const [providerId, credential] of Object.entries(entries)) {
    if (credential?.type !== "oauth") continue;
    try {
        await runtime.getAuth(providerId, { minOAuthValidityMs: Number(marginMs) });
        console.log(`${providerId}: valid`);
    } catch (error) {
        failed += 1;
        console.error(`${providerId}: ${error instanceof Error ? error.message : String(error)}`);
    }
}
process.exit(failed ? 1 : 0);
