/**
 * Lets `node --test` import the app's real .jsx sources.
 *
 * Vite normally does this transform, so the test runner needs the same two things
 * it provides: JSX compiled away, and `import.meta.env` defined (api.js reads
 * VITE_API_BASE_URL at module scope and would throw on a bare `import.meta`).
 * esbuild already ships inside Vite, so this costs no new dependency.
 */
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";

import { transformSync } from "esbuild";

const SRC = new URL("../src/", import.meta.url).href;

registerHooks({
  load(url, context, nextLoad) {
    const isAppSource = url.startsWith(SRC) && (url.endsWith(".jsx") || url.endsWith(".js"));
    if (!isAppSource) {
      return nextLoad(url, context);
    }

    const { code } = transformSync(readFileSync(fileURLToPath(url), "utf8"), {
      loader: url.endsWith(".jsx") ? "jsx" : "js",
      // Vite's React plugin uses the automatic runtime; the sources have no React import.
      jsx: "automatic",
      format: "esm",
      target: "node20",
      define: { "import.meta.env": "{}" },
      sourcefile: url,
    });

    return { format: "module", shortCircuit: true, source: code };
  },
});
