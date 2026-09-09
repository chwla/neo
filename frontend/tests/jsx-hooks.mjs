/**
 * Lets `node --test` import the app's real .jsx sources.
 *
 * Vite normally does this transform, so the test runner needs the same three things
 * it provides: JSX compiled away, `import.meta.env` defined (api.js reads
 * VITE_API_BASE_URL at module scope and would throw on a bare `import.meta`), and
 * `?raw` imports resolved to the file's text (the audio worklet is loaded that way,
 * because it has to reach the browser as source rather than as a bundled module).
 * esbuild already ships inside Vite, so this costs no new dependency.
 */
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";

import { transformSync } from "esbuild";

const SRC = new URL("../src/", import.meta.url).href;

const RAW = "?raw";

registerHooks({
  resolve(specifier, context, nextResolve) {
    // Node has no idea what `?raw` means; resolve the file itself and keep the
    // suffix on the resulting URL so `load` below knows to return its text.
    if (specifier.endsWith(RAW)) {
      const resolved = nextResolve(specifier.slice(0, -RAW.length), context);
      return { ...resolved, url: `${resolved.url}${RAW}`, shortCircuit: true };
    }
    return nextResolve(specifier, context);
  },

  load(url, context, nextLoad) {
    if (url.endsWith(RAW)) {
      const source = readFileSync(fileURLToPath(url.slice(0, -RAW.length)), "utf8");
      return {
        format: "module",
        shortCircuit: true,
        source: `export default ${JSON.stringify(source)};`,
      };
    }

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
