(() => {
  "use strict";
  const blocked = new Set([
    "APPLET", "BASE", "EMBED", "FORM", "FRAME", "FRAMESET", "IFRAME",
    "META", "OBJECT", "SCRIPT"
  ]);
  const activeAttributes = new Set([
    "action", "formaction", "ping", "srcdoc"
  ]);

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
      if (blocked.has(element.tagName)) {
        element.remove();
        continue;
      }
      for (const attribute of Array.from(element.attributes)) {
        const name = attribute.name.toLowerCase();
        if (name.startsWith("on") || activeAttributes.has(name)) {
          element.removeAttribute(attribute.name);
        } else if (name === "src" && !safeUrl(attribute.value, name)) {
          element.removeAttribute(attribute.name);
        } else if (name === "href" && !safeUrl(attribute.value, name)) {
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
