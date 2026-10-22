(() => {
  "use strict";
  // SVG and MathML carry their own presentation and geometry attributes, and a
  // `dialog` draws itself over the document without needing any. The
  // literate-code genre emits none of the three, so their subtrees go rather
  // than being filtered attribute by attribute. Foreign tag names keep their
  // own case, so match on the folded one.
  const blocked = new Set([
    "APPLET", "BASE", "DIALOG", "EMBED", "FORM", "FRAME", "FRAMESET", "IFRAME",
    "LINK", "MATH", "META", "OBJECT", "SCRIPT", "STYLE", "SVG"
  ]);
  // Static sanitization drops every input stylesheet and injects only our own.
  // Keep those exact nodes, never a later tooltip node with matching attributes.
  // This script is deferred, so the complete sanitized head is already parsed.
  const expectedSurface = "\n      /* The frame follows the browser's light/dark preference independently\n         of its parent document. These tokens also bind Verso's colour API,\n         including token hovers, messages and proof states. Severity fills\n         keep foreground text readable in both modes. */\n      :root {\n        color-scheme: light dark;\n        --palomar-paper: #ffffff;\n        --palomar-ink: #24292e;\n        --palomar-line: #dddddd;\n        --palomar-edge: #888888;\n        --palomar-raise: #eeeeee;\n        --palomar-panel: #ffffff;\n        --palomar-rule: #e1e4e8;\n        --palomar-link: #0066cc;\n        --palomar-alarm: #b42318;\n        --palomar-alarm-paper: #ffb3b3;\n        --palomar-info: #4777ff;\n        --palomar-info-paper: #dbe8ff;\n        --palomar-warning: #8a5b00;\n        --palomar-warning-paper: #fff0cc;\n        --palomar-mark: #bbbbbb;\n        --palomar-mark-on: #999999;\n        --palomar-shadow: rgb(0 0 0 / 22%);\n      }\n      @media (prefers-color-scheme: dark) {\n        :root {\n          --palomar-paper: #101216;\n          --palomar-ink: #e8eaee;\n          --palomar-line: #686f7c;\n          --palomar-edge: #8e96a3;\n          --palomar-raise: #242933;\n          --palomar-panel: #1c2027;\n          --palomar-rule: #3a404b;\n          --palomar-link: #8ab4f8;\n          --palomar-alarm: #ff9e91;\n          --palomar-alarm-paper: #3a1c1a;\n          --palomar-info: #8ab4f8;\n          --palomar-info-paper: #1c3a6b;\n          --palomar-warning: #ffd580;\n          --palomar-warning-paper: #3b2b14;\n          --palomar-mark: #686f7c;\n          --palomar-mark-on: #8e96a3;\n          --palomar-shadow: rgb(0 0 0 / 55%);\n        }\n      }\n      /* Bind the shipped Verso stylesheet's colour API to the same palette.\n         Its more specific hover/tooltip selectors then follow the theme too. */\n      :root {\n        --verso-code-color: var(--palomar-ink);\n        --verso-text-color: var(--palomar-ink);\n        --verso-structure-color: var(--palomar-ink);\n        --verso-selected-color: var(--palomar-raise);\n        --verso-tooltip-color: var(--palomar-ink);\n        --verso-tooltip-bg-color: var(--palomar-panel);\n        --verso-tooltip-border-color: var(--palomar-edge);\n        --verso-tooltip-separator-color: var(--palomar-rule);\n        --verso-tactic-state-color: var(--palomar-ink);\n        --verso-tactic-state-bg-color: var(--palomar-panel);\n        --verso-tactic-state-border-color: var(--palomar-edge);\n        --verso-tactic-toggle-color: var(--palomar-mark);\n        --verso-tactic-toggle-checked-color: var(--palomar-mark-on);\n        --verso-code-const-color: var(--palomar-ink);\n        --verso-code-keyword-color: var(--palomar-ink);\n        --verso-code-var-color: var(--palomar-ink);\n        --verso-code-hover-bg-color: var(--palomar-raise);\n        --verso-code-info-color: var(--palomar-ink);\n        --verso-code-info-bg-color: transparent;\n        --verso-code-info-hover-color: var(--palomar-ink);\n        --verso-code-info-hover-bg-color: var(--palomar-info-paper);\n        --verso-info-indicator-color: var(--palomar-info);\n        --verso-message-info-color: var(--palomar-ink);\n        --verso-tooltip-info-color: var(--palomar-ink);\n        --verso-tooltip-info-bg-color: var(--palomar-panel);\n        --verso-tooltip-info-border-color: var(--palomar-info);\n        --verso-output-info-color: var(--palomar-info);\n        --verso-code-warning-color: var(--palomar-ink);\n        --verso-code-warning-bg-color: transparent;\n        --verso-code-warning-hover-color: var(--palomar-ink);\n        --verso-code-warning-hover-bg-color: var(--palomar-warning-paper);\n        --verso-warning-indicator-color: var(--palomar-warning);\n        --verso-message-warning-color: var(--palomar-ink);\n        --verso-tooltip-warning-color: var(--palomar-ink);\n        --verso-tooltip-warning-bg-color: var(--palomar-panel);\n        --verso-tooltip-warning-border-color: var(--palomar-warning);\n        --verso-output-warning-color: var(--palomar-warning);\n        --verso-code-error-color: var(--palomar-ink);\n        --verso-code-error-bg-color: transparent;\n        --verso-code-error-hover-color: var(--palomar-ink);\n        --verso-code-error-hover-bg-color: var(--palomar-alarm-paper);\n        --verso-error-indicator-color: var(--palomar-alarm);\n        --verso-message-error-color: var(--palomar-alarm);\n        --verso-tooltip-error-color: var(--palomar-ink);\n        --verso-tooltip-error-bg-color: var(--palomar-panel);\n        --verso-tooltip-error-border-color: var(--palomar-alarm);\n        --verso-output-error-color: var(--palomar-alarm);\n      }\n      html { height: auto !important; min-height: 100%; overflow: auto !important;\n        overscroll-behavior: contain; }\n      body { height: auto !important; min-height: 100%; margin: 0;\n        overflow: visible !important; }\n      html, body { background: var(--palomar-paper); color: var(--palomar-ink); }\n      .palomar-declaration-surface { box-sizing: border-box; padding: 1rem; }\n      .palomar-declaration-surface .code-content { margin: 0; max-width: none; padding: 0; }\n      .palomar-declaration-surface .md-text::before,\n      .palomar-declaration-surface .md-text::after { width: max-content; white-space: nowrap; }\n      /* code.css paints the code surface and its docstring frames outright,\n         and this stylesheet is the last one in the head, so restating them at\n         equal specificity is enough to take them over. */\n      .code-content { background: var(--palomar-paper); color: var(--palomar-ink); }\n      .code-content .verso-text,\n      .code-content .md-text { border-color: var(--palomar-line); }\n      /* Fallback rules cover historical markup. The shipped modern selectors,\n         including their more specific hover rules, use the aliases above. */\n      @media (hover: hover) {\n        .hl.lean .token.binding-hl,\n        .hl.lean .literal:hover,\n        .hl.lean .token.typed:hover { background-color: var(--palomar-raise); }\n        .hl.lean .has-info.error:hover { background-color: var(--palomar-alarm-paper); }\n        .hl.lean .has-info.information:hover { background-color: var(--palomar-info-paper); }\n        .hl.lean .tactic:has(> .tactic-toggle:not(:checked))\n          > label:hover:not(:has(.tactic > label:hover)) {\n          background-color: var(--palomar-raise); }\n      }\n      .hl.lean .token .hover-info,\n      .hl.lean .has-info .hover-info { background-color: var(--palomar-panel);\n        border-color: var(--palomar-edge); }\n      .hl.lean .hover-info code { color: inherit; }\n      .hl.lean .hover-info .sep { border-top-color: var(--palomar-rule); }\n      .hl.lean .hover-info.messages > code.error { background-color: var(--palomar-panel);\n        border-left-color: var(--palomar-alarm); }\n      .hl.lean .hover-info.messages > code.information { background-color: var(--palomar-panel);\n        border-left-color: var(--palomar-info); }\n      .hl.lean .has-info.error :not(.tactic-state):not(.tactic-state *) {\n        text-decoration-color: var(--palomar-alarm); }\n      /* Verso's own fallback here is the keyword `blue`, which on dark paper is\n         an underline nobody can see. */\n      .hl.lean .has-info.information :not(.tactic-state):not(.tactic-state *) {\n        text-decoration-color: var(--palomar-info); }\n      .hl.lean .tactic-state { background-color: var(--palomar-panel);\n        border-color: var(--palomar-edge); }\n      .hl.lean.popup .tactic-state { background-color: var(--palomar-panel); }\n      /* The pill that says a tactic block can be opened, and the triangle that\n         says a case can. Verso draws the pill faintly on purpose, so the two\n         marks keep their own tokens rather than borrowing the border ones; the\n         triangle it draws in `black`, which is text, so it spends the ink. */\n      .hl.lean .tactic > label::after { border-color: var(--palomar-mark); }\n      .hl.lean .tactic > label:has(+ .tactic-toggle:checked)::after {\n        border-color: var(--palomar-mark-on); background-color: var(--palomar-mark-on); }\n      .hl.lean .case-label:has(input[type=\"checkbox\"])::before {\n        background-color: var(--palomar-ink); }\n      .palomar-hover {\n        padding: .65rem !important; border: 1px solid var(--palomar-edge) !important;\n        border-radius: .35rem; background: var(--palomar-panel) !important;\n        color: var(--palomar-ink) !important;\n        box-shadow: 0 .25rem 1rem var(--palomar-shadow);\n        /* The pointer must never land on the popup. If it does, it has left\n           the token, so the popup hides, so the pointer is over the token\n           again, so it shows: a flicker that sustains itself. */\n        pointer-events: none;\n        /* Lean signatures are long and were being cut off inside a 400px box.\n           Wrap them instead, and only where they can be broken. */\n        white-space: pre-wrap;\n        overflow-wrap: break-word;\n      }\n      .palomar-hover .hl.lean, .palomar-hover .popup,\n      .palomar-hover .hover-info { white-space: pre-wrap; }\n      .palomar-hover .popup,\n      .palomar-hover .hover-info,\n      .palomar-hover .docstring { background: var(--palomar-panel) !important;\n        color: var(--palomar-ink) !important; }\n      /* Give the block docstring one separator of its own, hiding Verso's\n         empty separator span so the popup does not acquire a second rule. */\n      .palomar-hover .sep { display: block; height: 0 !important; border: none !important; }\n      .palomar-hover .docstring {\n        display: block;\n        margin-top: .55rem;\n        padding-top: .55rem;\n        border-top: 1px solid var(--palomar-rule);\n      }\n      .palomar-render-error { margin: 1rem; font-family: sans-serif; }\n      /* Verso's page shell, which a reader sees only where this document's\n         scripts do not run: the runtime replaces the whole body on both its\n         success and its failure path. It is answered broadly rather than rule\n         by rule, because normalising a shell nobody reaches is not worth\n         freezing a dozen more declarations into every published bundle. */\n      .sidebar { background: var(--palomar-raise); }\n      .title-bar, .hamburger { background: var(--palomar-paper); }\n      .sidebar, .title-bar, .hamburger { border-color: var(--palomar-line); }\n      .hamburger span { background: var(--palomar-ink); }\n      .module-tree summary a, .module-tree .leaf a,\n      .breadcrumbs a { color: var(--palomar-link); }\n      .module-tree summary:hover,\n      .breadcrumbs a:hover { background-color: var(--palomar-raise); }\n      .module-tree summary::before { color: var(--palomar-ink); }\n      .module-tree summary.current,\n      .module-tree .current { background-color: var(--palomar-link);\n        color: var(--palomar-paper); }\n      .module-tree summary.current a,\n      .module-tree .current a { color: var(--palomar-paper); }\n      .breadcrumbs .current,\n      .breadcrumbs li:not(:last-child)::after { color: var(--palomar-ink); }\n    ";
  const runtimeUrl = document.currentScript?.src;
  const expectedCss = runtimeUrl ? new URL("palomar-render.css", runtimeUrl).href : null;
  const trustedStyles = new WeakSet();
  for (const element of document.head?.querySelectorAll("style,link") ?? []) {
    const style = element.tagName.toUpperCase() === "STYLE"
      && element.attributes.length === 1
      && element.getAttribute("id") === "palomar-declaration-style"
      && element.textContent === expectedSurface;
    let link = false;
    if (element.tagName.toUpperCase() === "LINK" && element.attributes.length === 2
        && element.getAttribute("rel") === "stylesheet") {
      try {
        link = expectedCss !== null
          && new URL(element.getAttribute("href"), document.baseURI).href === expectedCss;
      } catch (_) {}
    }
    if (style || link) trustedStyles.add(element);
    else element.remove();
  }
  const activeAttributes = new Set([
    "action", "formaction", "ping", "srcdoc"
  ]);
  // Presentation and visibility without a stylesheet: the legacy colour
  // attributes, the sizing pair, the top-layer controls, and the one that
  // hides honest content so that dishonest content can stand in for it. Verso
  // emits none of them here, `<meta name="viewport">` being a meta element and
  // dropped whole.
  const presentationAttributes = new Set([
    "alink", "background", "bgcolor", "height", "hidden", "link", "popover",
    "popovertarget", "popovertargetaction", "text", "vlink", "width"
  ]);
  // Verso's literate-code output needs one inline declaration, the indent
  // custom property its stylesheet reads, whose value is the source column the
  // documentation starts at. Any other one is enough to reposition or repaint
  // a page served with `style-src 'unsafe-inline'`. The whitespace is spelled
  // out because `\s` does not mean the same thing here as it does in Python.
  const allowedStyle = /^[ \t\r\n\f]*--indent:[ \t\r\n\f]*[0-9]{1,2}[ \t\r\n\f]*;?[ \t\r\n\f]*$/;

  function safeUrl(value, attribute) {
    const trimmed = value.trim();
    if (!trimmed || trimmed.startsWith("#") || trimmed.startsWith("./")) return true;
    if (trimmed.startsWith("../")) {
      if (trimmed.includes("\\")) return false;
      try {
        return !trimmed.slice(3).split("/")
          .map((part) => decodeURIComponent(part).toLowerCase())
          .some((part) => part === "." || part === "..");
      } catch (_) {
        return false;
      }
    }
    return attribute === "src" && /^data:image\/(?:gif|jpeg|png|webp);/i.test(trimmed);
  }

  function sanitize(root) {
    for (const element of Array.from(root.querySelectorAll("*"))) {
      // Renderer-owned nodes already have trusted attributes, including the
      // relative CSS URL on root and deeply nested auxiliary pages.
      if (trustedStyles.has(element)) continue;
      if (blocked.has(element.tagName.toUpperCase())) {
        element.remove();
        continue;
      }
      for (const attribute of Array.from(element.attributes)) {
        const name = attribute.name.toLowerCase();
        if (name.startsWith("on") || activeAttributes.has(name)
            || presentationAttributes.has(name)) {
          element.removeAttribute(attribute.name);
        } else if (name === "open" && element.tagName.toUpperCase() !== "DETAILS") {
          // Verso emits `open` only on `<details>`: the module tree's, and the
          // trace ones it leaves expanded.
          element.removeAttribute(attribute.name);
        } else if (name === "src" && !safeUrl(attribute.value, name)) {
          element.removeAttribute(attribute.name);
        } else if (name === "href" && !safeUrl(attribute.value, name)) {
          element.removeAttribute(attribute.name);
        } else if (name === "style" && !allowedStyle.test(attribute.value)) {
          element.removeAttribute(attribute.name);
        } else if (name === "target") {
          element.removeAttribute(attribute.name);
        }
      }
    }
  }

  if (typeof marked !== "undefined" && typeof marked.parse === "function") {
    const parse = marked.parse.bind(marked);
    marked.parse = (...args) => {
      const template = document.createElement("template");
      template.innerHTML = parse(...args);
      sanitize(template.content);
      return template.innerHTML;
    };
  }
  window.palomarSanitize = sanitize;
  document.addEventListener("DOMContentLoaded", () => sanitize(document), {once: true});
})();
