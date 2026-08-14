(() => {
  "use strict";
  const runtimeUrl = document.currentScript?.src;
  const sanitize = (root) => window.palomarSanitize && window.palomarSanitize(root);

  function isolateComparedDeclarations() {
    let declarations;
    try {
      declarations = JSON.parse(document.body.dataset.palomarDeclarations || "[]");
    } catch (_) {
      declarations = [];
    }
    if (!Array.isArray(declarations) || declarations.length === 0) return true;
    const source = document.querySelector("section.code-content");
    if (!source) return false;

    const selected = [];
    for (const name of declarations) {
      const marker = Array.from(source.querySelectorAll(".const[data-binding]"))
        .find((element) => element.dataset.binding === `const-${name}` && element.id);
      const block = marker?.closest("code.hl.lean.block");
      if (!block) return false;
      const docstring = block.previousElementSibling;
      if (docstring?.matches(".md-text:not(.mod-doc)")) selected.push(docstring);
      selected.push(block);
    }

    const main = document.createElement("main");
    main.id = "main-content";
    main.className = "palomar-declaration-surface";
    const content = document.createElement("section");
    content.className = "code-content";
    for (const element of selected) content.append(element);
    for (const anchor of content.querySelectorAll("a[href]")) {
      let fragment = "";
      try {
        fragment = new URL(anchor.href, location.href).hash.slice(1);
      } catch (_) {
        fragment = "";
      }
      if (fragment && !content.querySelector(`#${CSS.escape(fragment)}`)) {
        anchor.replaceWith(...anchor.childNodes);
      }
    }
    main.append(content);
    document.body.replaceChildren(main);
    document.title = "Compared Challenge declaration";
    return true;
  }

  function renderDocstrings(root) {
    if (typeof marked === "undefined" || typeof marked.parse !== "function") return;
    for (const source of root.querySelectorAll("code.docstring, pre.docstring")) {
      const template = document.createElement("template");
      template.innerHTML = marked.parse(source.innerText);
      sanitize(template.content);
      const rendered = document.createElement("div");
      rendered.className = "docstring";
      rendered.append(template.content.cloneNode(true));
      source.replaceWith(rendered);
    }
  }

  function installBindingHighlights() {
    let highlighted = [];
    for (const container of document.querySelectorAll(".hl.lean")) {
      container.addEventListener("mouseover", (event) => {
        const token = event.target.closest(".token[data-binding]");
        if (!token || !container.contains(token) || !token.dataset.binding) return;
        highlighted = Array.from(document.querySelectorAll(".hl.lean .token[data-binding]"))
          .filter((candidate) => candidate.dataset.binding === token.dataset.binding &&
            candidate.closest(".hl.lean")?.dataset.leanContext === container.dataset.leanContext);
        highlighted.forEach((candidate) => candidate.classList.add("binding-hl"));
      });
      container.addEventListener("mouseout", () => {
        highlighted.forEach((candidate) => candidate.classList.remove("binding-hl"));
        highlighted = [];
      });
    }
  }

  function popupContent(target, docs) {
    const content = document.createElement("span");
    content.className = "hl lean popup";
    if (target.classList.contains("tactic")) {
      const state = target.querySelector(":scope > .tactic-state");
      if (!state) return null;
      content.append(state.cloneNode(true));
      return content;
    }
    const inline = target.querySelector(".hover-info");
    if (inline) {
      content.append(inline.cloneNode(true));
      sanitize(content);
      return content;
    }
    const value = docs[String(target.dataset.versoHover || "")];
    if (typeof value !== "string") return null;
    const template = document.createElement("template");
    template.innerHTML = value;
    sanitize(template.content);
    content.append(template.content.cloneNode(true));
    renderDocstrings(content);
    return content;
  }

  function installHovers(docs) {
    const popup = document.createElement("div");
    popup.className = "palomar-hover hl lean";
    popup.hidden = true;
    document.body.append(popup);
    const selector = ".hl.lean .token[data-verso-hover], .hl.lean .has-info, .hl.lean .tactic";
    for (const target of document.querySelectorAll(selector)) {
      target.addEventListener("mouseenter", () => {
        const content = popupContent(target, docs);
        if (!content) return;
        popup.replaceChildren(content);
        popup.hidden = false;
        const box = target.getBoundingClientRect();
        popup.style.position = "fixed";
        popup.style.left = `${Math.max(8, Math.min(box.left, innerWidth - 420))}px`;
        popup.style.top = `${Math.min(innerHeight - 160, box.bottom + 6)}px`;
        popup.style.maxWidth = "400px";
        popup.style.maxHeight = "300px";
        popup.style.overflow = "auto";
        popup.style.zIndex = "2147483647";
      });
      target.addEventListener("mouseleave", () => { popup.hidden = true; });
    }
  }

  function reportSurfaceHeight() {
    const surface = document.querySelector(".palomar-declaration-surface");
    if (!surface || parent === window) return;
    const send = () => {
      const height = Math.ceil(surface.getBoundingClientRect().height);
      if (Number.isSafeInteger(height) && height > 0) {
        parent.postMessage({type: "palomar-render-height", height}, "*");
      }
    };
    requestAnimationFrame(send);
    if (typeof ResizeObserver === "function") new ResizeObserver(send).observe(surface);
  }

  document.addEventListener("DOMContentLoaded", async () => {
    sanitize(document);
    if (!isolateComparedDeclarations()) {
      const error = document.createElement("p");
      error.className = "palomar-render-error";
      error.textContent = "The compared declaration is missing from this rendering.";
      document.body.replaceChildren(error);
      return;
    }
    renderDocstrings(document);
    installBindingHighlights();
    let docs = {};
    try {
      const root = runtimeUrl ? new URL(".", runtimeUrl) : new URL("../", location.href);
      const response = await fetch(new URL("-verso-docs.json", root), {credentials: "omit"});
      if (response.ok) docs = await response.json();
    } catch (_) {
      docs = {};
    }
    installHovers(docs);
    reportSurfaceHeight();
  }, {once: true});
})();
