import {basicSetup} from "codemirror";
import {EditorState} from "@codemirror/state";
import {EditorView, keymap} from "@codemirror/view";
import {HighlightStyle, indentUnit, syntaxHighlighting} from "@codemirror/language";
import {tags} from "@lezer/highlight";
import {indentWithTab} from "@codemirror/commands";
import {lintGutter, setDiagnostics} from "@codemirror/lint";
import {css} from "@codemirror/lang-css";
import {html} from "@codemirror/lang-html";
import {javascript} from "@codemirror/lang-javascript";
import {json} from "@codemirror/lang-json";
import {markdown} from "@codemirror/lang-markdown";
import {sql} from "@codemirror/lang-sql";
import {xml} from "@codemirror/lang-xml";
import {yaml} from "@codemirror/lang-yaml";

function languageFor(filename) {
  const lower = String(filename || "").toLowerCase();
  if (lower.endsWith(".json") || lower.endsWith(".map")) return {name: "JSON", extension: json()};
  if (lower.endsWith(".yml") || lower.endsWith(".yaml")) return {name: "YAML", extension: yaml()};
  if (lower.endsWith(".html") || lower.endsWith(".htm")) return {name: "HTML", extension: html()};
  if (lower.endsWith(".xml")) return {name: "XML", extension: xml()};
  if (lower.endsWith(".css")) return {name: "CSS", extension: css()};
  if (lower.endsWith(".js")) return {name: "JavaScript", extension: javascript()};
  if (lower.endsWith(".md")) return {name: "Markdown", extension: markdown()};
  if (lower.endsWith(".sql")) return {name: "SQL", extension: sql()};
  return {name: "Plain text", extension: []};
}

const craftarrTheme = EditorView.theme({
  "&": {height: "100%", backgroundColor: "#1e1e1e", color: "#d4d4d4"},
  ".cm-content": {caretColor: "#aeafad", fontFamily: '"SFMono-Regular", Consolas, monospace'},
  ".cm-cursor, .cm-dropCursor": {borderLeftColor: "#aeafad"},
  ".cm-gutters": {backgroundColor: "#1e1e1e", color: "#858585", borderRight: "1px solid #333333"},
  ".cm-activeLine": {backgroundColor: "#2a2d2e"},
  ".cm-activeLineGutter": {backgroundColor: "#2a2d2e", color: "#c6c6c6"},
  ".cm-selectionBackground": {backgroundColor: "#3a3d41"},
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground": {backgroundColor: "#264f78"},
  ".cm-content ::selection": {backgroundColor: "#264f78", color: "#ffffff"},
  ".cm-searchMatch": {backgroundColor: "#515c6a", outline: "1px solid #ea5c00"},
  ".cm-searchMatch.cm-searchMatch-selected": {backgroundColor: "#613214"},
  ".cm-matchingBracket": {backgroundColor: "#0e639c", outline: "1px solid #888888"},
  ".cm-tooltip": {backgroundColor: "#252526", color: "#cccccc", borderColor: "#454545"},
  ".cm-diagnostic-warning": {borderLeftColor: "#cca700", backgroundColor: "#352a05"},
}, {dark: true});

const vscodeDarkHighlight = HighlightStyle.define([
  {tag: [tags.keyword, tags.controlKeyword, tags.moduleKeyword], color: "#c586c0"},
  {tag: [tags.name, tags.variableName], color: "#9cdcfe"},
  {tag: [tags.definition(tags.variableName), tags.function(tags.variableName)], color: "#dcdcaa"},
  {tag: [tags.propertyName, tags.attributeName], color: "#9cdcfe"},
  {tag: [tags.typeName, tags.className, tags.namespace], color: "#4ec9b0"},
  {tag: [tags.string, tags.special(tags.string)], color: "#ce9178"},
  {tag: [tags.number, tags.bool, tags.null], color: "#b5cea8"},
  {tag: [tags.comment, tags.lineComment, tags.blockComment], color: "#6a9955", fontStyle: "italic"},
  {tag: [tags.tagName, tags.heading], color: "#569cd6"},
  {tag: [tags.operator, tags.punctuation], color: "#d4d4d4"},
  {tag: [tags.regexp, tags.escape], color: "#d16969"},
  {tag: tags.link, color: "#4fc1ff", textDecoration: "underline"},
  {tag: tags.invalid, color: "#f44747", textDecoration: "underline wavy"},
]);

function savePosition(view, key) {
  if (!view || !key) return;
  try {
    sessionStorage.setItem(key, JSON.stringify({
      anchor: view.state.selection.main.anchor,
      head: view.state.selection.main.head,
      scrollTop: view.scrollDOM.scrollTop,
      scrollLeft: view.scrollDOM.scrollLeft,
    }));
  } catch {
    // Saving still works when browser storage is unavailable.
  }
}

function restorePosition(view, key) {
  if (!view || !key) return;
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || "null");
    if (!saved) return;
    const length = view.state.doc.length;
    view.dispatch({selection: {
      anchor: Math.max(0, Math.min(Number(saved.anchor) || 0, length)),
      head: Math.max(0, Math.min(Number(saved.head) || 0, length)),
    }});
    requestAnimationFrame(() => requestAnimationFrame(() => {
      view.scrollDOM.scrollTop = Math.max(0, Number(saved.scrollTop) || 0);
      view.scrollDOM.scrollLeft = Math.max(0, Number(saved.scrollLeft) || 0);
    }));
  } catch {
    // Ignore malformed or unavailable saved editor state.
  }
}

function create(textarea, options = {}) {
  if (!textarea || textarea.dataset.codeEditorReady === "true") return textarea?._codeEditor;
  const language = languageFor(options.filename || textarea.dataset.filename);
  const indentation = " ".repeat(options.indentSize || (language.name === "YAML" ? 2 : 4));
  const host = document.createElement("div");
  host.className = "code-editor-host";
  textarea.before(host);
  textarea.classList.add("code-editor-source");
  textarea.dataset.codeEditorReady = "true";

  const saveKey = {
    key: "Mod-s",
    preventDefault: true,
    run: () => {
      if (options.onSave) options.onSave();
      else textarea.form?.requestSubmit(textarea.form.querySelector('[data-editor-save="true"]'));
      return true;
    },
  };
  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: textarea.value,
      extensions: [
        basicSetup,
        language.extension,
        indentUnit.of(indentation),
        EditorState.tabSize.of(4),
        keymap.of([saveKey, indentWithTab]),
        lintGutter(),
        craftarrTheme,
        syntaxHighlighting(vscodeDarkHighlight),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) textarea.value = update.state.doc.toString();
        }),
      ],
    }),
  });
  textarea._codeEditor = view;
  host.dataset.language = language.name;
  if (options.languageLabel) options.languageLabel.textContent = language.name;
  if (options.warning) showWarning(view, options.warning);
  restorePosition(view, options.storageKey);
  return view;
}

function showWarning(view, warning) {
  if (!view) return;
  if (!warning?.message) {
    view.dispatch(setDiagnostics(view.state, []));
    return;
  }
  const requestedLine = Number(warning.line) || 1;
  const lineNumber = Math.max(1, Math.min(requestedLine, view.state.doc.lines));
  const line = view.state.doc.line(lineNumber);
  const column = Math.max(1, Number(warning.column) || 1);
  const from = Math.min(line.to, line.from + column - 1);
  const to = Math.min(line.to, Math.max(from + 1, from));
  view.dispatch(setDiagnostics(view.state, [{
    from,
    to,
    severity: "warning",
    message: warning.message,
  }]));
}

window.CraftarrCodeEditor = {create, showWarning, languageFor, savePosition};
window.dispatchEvent(new CustomEvent("craftarr:editor-ready"));
