// Antigravity RU: local UI-only translation in the isolated preload world.
(() => {
  "use strict";
  if (location.protocol !== "https:" || !["127.0.0.1", "localhost"].includes(location.hostname)) return;
  const DICT = __RU_DICTIONARY__;
  const OMIT = [
    "script", "style", "noscript", "svg", "canvas", "pre", "code", "kbd",
    "textarea", "[contenteditable='true']", "[data-lexical-editor]",
    ".monaco-editor", ".cm-editor", ".xterm", ".xterm-screen",
    "[data-testid='planner-response-text']", "[data-testid='user-input-step']",
    "[data-testid='pending-user-messages']", "[data-testid='commentable-content']",
    "[data-testid='schedule-editor-prompt']", "[data-testid='sidecar-logs-content']",
    "[data-testid='terminal-surface']", "[data-testid='standalone-terminal-view']",
    "[data-testid^='terminal-pane-']", "[data-testid^='terminal-cwd-']",
    "[data-testid='markdown-frontmatter']"
  ].join(",");
  const NAMED = [
    "[data-testid^='conversation-row-']", "[data-testid^='conversation-group-']",
    "[data-testid^='sidecar-row-']", "[data-testid='project-selector-item']",
    "[data-testid^='instance-selector-item-']"
  ].join(",");
  const CONTROL = "button,[role='menuitem'],[data-tooltip-id]";
  const TIMESTAMP = "[data-testid^='conversation-row-'] span[data-screenshot-volatile].text-xs";
  const ATTRS = ["title", "aria-label", "placeholder", "data-placeholder", "data-tooltip-content"];
  function omitted(el) {
    if (!el || el.closest(OMIT)) return true;
    if (el.closest(NAMED) && !el.closest(CONTROL)) return true;
    const link = el.closest("a[href]");
    if (link && /^(?:file:|vscode:|antigravity:)/.test(link.getAttribute("href") || "")) return true;
    const section = el.closest("[data-testid='section-header']");
    if (section && !["Projects", "Conversations", "Recent", "Pinned", "Workspaces", "Other Conversations"].includes(section.getAttribute("data-title")) && !el.closest("button")) return true;
    return false;
  }
  function ru(text) {
    if (!text || text.length > 4000) return text;
    const trimmed = text.trim();
    let translated = Object.hasOwn(DICT, trimmed) ? DICT[trimmed] : undefined;
    if (translated === undefined) {
      let m;
      if ((m = /^Thought for (\d+(?:\.\d+)?)s$/.exec(trimmed))) translated = "Обдумывал " + m[1] + " с";
      else if ((m = /^Thinking for (\d+(?:\.\d+)?)s$/.exec(trimmed))) translated = "Обдумывает " + m[1] + " с";
      else if ((m = /^Exploring (\d+) searches$/.exec(trimmed))) translated = "Поиск: " + m[1] + " запросов";
      else if ((m = /^See all \((\d+)\)$/.exec(trimmed))) translated = "Показать все (" + m[1] + ")";
      else if ((m = /^(\d+) agents? running$/.exec(trimmed))) translated = "Запущено агентов: " + m[1];
      else if ((m = /^(\d+) files? changed$/.exec(trimmed))) translated = "Изменено файлов: " + m[1];
      else if ((m = /^Used (\d+(?:\.\d+)?)%$/.exec(trimmed))) translated = "Использовано " + m[1] + "%";
      else if ((m = /^(\d+) results?$/.exec(trimmed))) translated = "Результатов: " + m[1];
    }
    if (!translated || translated === trimmed) return text;
    const start = text.indexOf(trimmed);
    return text.slice(0, start) + translated + text.slice(start + trimmed.length);
  }
  function textNode(node) {
    // The timestamp shares a row with a user-supplied title. Only this known
    // metadata span may bypass NAMED; never translate the rest of the row.
    const parent = node.parentElement;
    if (parent?.matches(TIMESTAMP) && !parent.closest(OMIT)) {
      const original = node.nodeValue;
      const trimmed = original.trim();
      const match = /^(\d+)\s*(s|m|h|d|w|mo|y)$/.exec(trimmed);
      const units = {s: "с", m: "мин", h: "ч", d: "д", w: "нед", mo: "мес", y: "г"};
      const value = trimmed === "now" ? "сейчас" : match ? match[1] + " " + units[match[2]] : undefined;
      if (value) node.nodeValue = original.replace(trimmed, value);
      return;
    }
    if (omitted(node.parentElement)) return;
    const original = node.nodeValue;
    const value = ru(original);
    // Preserve nodes: React and Lexical retain references to them.
    if (value !== original) node.nodeValue = value;
  }
  function attributes(el) {
    if (omitted(el)) return;
    for (const name of ATTRS) {
      const original = el.getAttribute(name);
      if (!original) continue;
      const value = ru(original);
      if (value !== original) el.setAttribute(name, value);
    }
  }
  function subtree(root) {
    if (root.nodeType === Node.TEXT_NODE) { textNode(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
    if (root.nodeType === Node.ELEMENT_NODE) {
      if (root.closest(OMIT)) return;
      attributes(root);
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (node.nodeType === Node.ELEMENT_NODE && node.matches(OMIT)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    let node;
    while ((node = walker.nextNode())) {
      if (node.nodeType === Node.TEXT_NODE) textNode(node);
      else attributes(node);
    }
  }
  const pending = new Set();
  let scheduled = false;
  function flush() {
    scheduled = false;
    const roots = [...pending];
    pending.clear();
    for (const root of roots) {
      if (!root.isConnected) continue;
      if (roots.some(parent => parent !== root && parent.nodeType === Node.ELEMENT_NODE && parent.contains(root))) continue;
      subtree(root);
    }
  }
  const observer = new MutationObserver(records => {
    for (const record of records) {
      if (record.type === "childList") for (const node of record.addedNodes) pending.add(node);
      else pending.add(record.target);
    }
    if (!scheduled && pending.size) {
      scheduled = true;
      queueMicrotask(flush);
    }
  });
  function start() {
    if (!document.documentElement) return;
    document.documentElement.lang = "ru";
    const root = document.body || document.documentElement;
    subtree(root);
    observer.observe(root, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
  else start();
})();
