/* eslint-disable import-x/default -- ?worker default export is virtual */

// Monaco web-worker setup for Vite, hosted in our own source.
//
// GraphiQL 5 ships ``graphiql/setup-workers/vite`` for this, but that file
// performs the ``?worker`` imports from *inside* the ``@graphiql/react``
// package. Under pnpm those modules live at a deeply nested
// ``node_modules/.pnpm/...`` path, and Vite 8's (rolldown) dependency
// optimizer fails to load them during ``vite dev``
// (``[UNLOADABLE_DEPENDENCY] ... editor.worker.js?worker``). Re-declaring
// the same ``?worker`` imports here — from the app's own module graph —
// lets Vite's first-class worker handling resolve them instead of the
// dependency scanner.
//
// ``monaco-editor`` / ``monaco-graphql`` are pinned in ``package.json`` to
// the exact versions ``@graphiql/react`` depends on, so pnpm dedupes to a
// single physical copy (no duplicate Monaco instances).
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker.js?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker.js?worker";
import GraphQLWorker from "monaco-graphql/esm/graphql.worker.js?worker";

// ``MonacoEnvironment`` is declared globally by monaco-editor's own types
// (``interface Window``); assign to it via ``self`` rather than
// redeclaring. ``getWorker`` parameter types are inferred contextually.
self.MonacoEnvironment = {
  getWorker(_workerId, label) {
    switch (label) {
      case "json":
        return new JsonWorker();
      case "graphql":
        return new GraphQLWorker();
      default:
        return new EditorWorker();
    }
  },
};
