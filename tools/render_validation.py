"""Validate one immutable browser-render bundle against its entry.

This module owns the filesystem limits, active-HTML policy, trusted runtime
hashes, manifest, and content-address checks for the render surface. It reads
only the supplied database root and entry and returns errors without mutating
either; database traversal and validation scope remain with validate.py.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import posixpath
import re
import stat
from html.parser import HTMLParser

from entry_validation import PALOMAR_ID_RE

MAX_RENDER_FILES = 2_000
MAX_RENDER_NODES = 4_000
MAX_RENDER_FILE_BYTES = 8 * 1024 * 1024
MAX_RENDER_BYTES = 25 * 1024 * 1024
ALLOWED_RENDER_EXTENSIONS = frozenset(
    {
        ".css",
        ".gif",
        ".html",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".md",
        ".png",
        ".ts",
        ".txt",
        ".webp",
        ".woff",
        ".woff2",
    }
)
RENDER_CSP = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "frame-src 'none'",
        "child-src 'none'",
        "worker-src 'none'",
        "manifest-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "navigate-to 'self'",
    )
)
RENDER_CSP_META = (
    f'<meta http-equiv="Content-Security-Policy" content="{RENDER_CSP}">'
)
TRUSTED_RENDER_SCRIPTS = {
    "palomar-sanitize.js": frozenset(
        {"d15fb1c3eca7a3eb32293cff66a913301c25fb03706004a0e27319b631c6ff60"}
    ),
    "palomar-verso.js": frozenset(
        {
            # Records already registered carry the earlier runtime, and a record
            # is immutable, so its digest stays trusted rather than being
            # replaced.
            "a44bf5ebef846fc69009c02d5617e5af1a2d70d26298ea6db4a20600cead5201",
            # Hover popups that do not cover the token they describe, and do not
            # cut off a Lean signature at 400px.
            "332773edafa3ac712ec3fba31d4c6ece2339693958873a9020a9be1bddb22538",
        }
    ),
}
ACCEPTED_RENDER_CSPS = frozenset({RENDER_CSP})
REQUIRED_RENDER_SCRIPTS = list(TRUSTED_RENDER_SCRIPTS)


class _RenderHTMLPolicyParser(HTMLParser):
    """Enforce the active HTML surface around the trusted renderer runtimes."""

    def __init__(self, relative_path: str) -> None:
        super().__init__(convert_charrefs=True)
        self.csp_values: list[str | None] = []
        self.errors: list[str] = []
        self.head_depth = 0
        self.head_count = 0
        self.template_depth = 0
        self.csp_seen = False
        self.script_depth = 0
        self.script_sources: list[str] = []
        self.relative_path = pathlib.PurePosixPath(relative_path)

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, list[str | None]]:
        values: dict[str, list[str | None]] = {}
        for name, value in attrs:
            values.setdefault(name.lower(), []).append(value)
        return values

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "head":
            self.head_depth += 1
            self.head_count += 1
            return
        if normalized_tag == "template":
            self.template_depth += 1
        values = self._attributes(attrs)
        for name, attribute_values in values.items():
            if name.startswith("on") or name == "srcdoc":
                self.errors.append(f"active HTML attribute is forbidden: {name}")
            if name in {"href", "src", "action", "formaction", "ping"}:
                for value in attribute_values:
                    normalized = (value or "").lstrip("".join(chr(code) for code in range(33)))
                    if normalized.lower().startswith("javascript:"):
                        self.errors.append(f"active javascript URL is forbidden: {name}")
        if normalized_tag in {"base", "embed", "iframe", "object"}:
            self.errors.append(f"active HTML element is forbidden: {normalized_tag}")
        if self.head_depth == 1 and not self.csp_seen and normalized_tag not in {"meta", "script"}:
            # Before a meta-delivered CSP takes effect, admit only elements that
            # cannot make an HTML5 parser leave the head or enter RCDATA/foreign
            # content. This keeps the simple parser aligned with browser state.
            self.errors.append(f"unsafe element appears before the trusted CSP: {normalized_tag}")
        if normalized_tag == "script":
            self.script_depth += 1
            src = values.get("src", [])
            defer = values.get("defer", [])
            if set(values) != {"src", "defer"} or len(src) != 1 or defer != [None]:
                self.errors.append("script element must be an empty deferred trusted runtime")
                return
            raw_source = src[0] or ""
            resolved = posixpath.normpath(
                (self.relative_path.parent / raw_source).as_posix()
            )
            if resolved not in TRUSTED_RENDER_SCRIPTS:
                self.errors.append(f"untrusted script source: {raw_source}")
            self.script_sources.append(resolved)
            return
        if normalized_tag != "meta":
            return
        http_equiv = values.get("http-equiv", [])
        if len(http_equiv) != 1 or (http_equiv[0] or "").lower() != "content-security-policy":
            if self.head_depth == 1 and not self.csp_seen and http_equiv:
                self.errors.append("non-CSP http-equiv appears before the trusted CSP")
            return
        content = values.get("content", [])
        active = len(content) == 1 and self.head_depth == 1 and self.template_depth == 0
        self.csp_values.append(content[0] if active else None)
        if active:
            self.csp_seen = True

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "script":
            if self.script_depth:
                self.script_depth -= 1
            else:
                self.errors.append("script closing tag has no matching opening tag")
        elif normalized_tag == "template" and self.template_depth:
            self.template_depth -= 1
        elif normalized_tag == "head" and self.head_depth:
            self.head_depth -= 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.script_depth and data:
            self.errors.append("inline script content is forbidden")
        if self.head_depth == 1 and not self.csp_seen and data.strip():
            self.errors.append("non-whitespace text appears before the trusted CSP")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_render(root: pathlib.Path, entry: dict[str, object], name: str) -> list[str]:
    """Return bundle errors after the caller has schema-validated the entry.

    A render shape rejected by the entry schema returns no errors here because
    there is no safe bundle identity to inspect.
    """
    render = entry.get("challenge_render")
    if not isinstance(render, dict):
        return []  # The JSON Schema reports the missing or malformed object.
    artifact_path = render.get("artifact_path")
    tree_hash = render.get("artifact_tree_sha256")
    entrypoint = render.get("entrypoint")
    identifier = entry.get("id")
    version = entry.get("version")
    if (
        not isinstance(artifact_path, str)
        or not isinstance(tree_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", tree_hash)
        or entrypoint != "Challenge/index.html"
        or not isinstance(identifier, str)
        or not PALOMAR_ID_RE.fullmatch(identifier)
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
    ):
        return []
    expected_path = f"renders/{identifier}-v{version}/{tree_hash}/"
    errors: list[str] = []
    if artifact_path != expected_path:
        errors.append(f"{name}:challenge_render.artifact_path: must be {expected_path}")
        return errors
    bundle = root / artifact_path
    current = root
    for part in pathlib.PurePosixPath(artifact_path).parts:
        current /= part
        if current.is_symlink():
            return [f"{name}:challenge_render: artifact path contains a symbolic link"]
    if bundle.is_symlink() or not bundle.is_dir():
        return [f"{name}:challenge_render: artifact directory is missing or symbolic"]
    files: list[dict[str, object]] = []
    total_bytes = 0
    paths: list[pathlib.Path] = []
    for path in bundle.rglob("*"):
        paths.append(path)
        if len(paths) > MAX_RENDER_NODES:
            errors.append(f"{name}:challenge_render: artifact exceeds the filesystem-node cap")
            break
    for path in sorted(paths):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            errors.append(f"{name}:challenge_render: symbolic link in artifact: {relative}")
            continue
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            errors.append(f"{name}:challenge_render: non-regular artifact file: {relative}")
            continue
        if relative == "artifact-manifest.json":
            if path.stat().st_size > MAX_RENDER_FILE_BYTES:
                errors.append(f"{name}:challenge_render: artifact manifest exceeds the file-size cap")
            continue
        if path.suffix.lower() not in ALLOWED_RENDER_EXTENSIONS:
            errors.append(
                f"{name}:challenge_render: disallowed artifact extension: {relative}"
            )
        size = path.stat().st_size
        if size > MAX_RENDER_FILE_BYTES:
            errors.append(f"{name}:challenge_render: artifact file exceeds the size cap: {relative}")
            continue
        if len(files) >= MAX_RENDER_FILES:
            errors.append(f"{name}:challenge_render: artifact exceeds the file-count cap")
            break
        if total_bytes + size > MAX_RENDER_BYTES:
            errors.append(f"{name}:challenge_render: artifact exceeds the total-size cap")
            break
        total_bytes += size
        files.append(
            {"path": relative, "bytes": size, "sha256": _sha256(path)}
        )
        if path.suffix.lower() == ".html":
            try:
                html = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                errors.append(
                    f"{name}:challenge_render: invalid UTF-8 HTML file {relative}: {error}"
                )
            else:
                parser = _RenderHTMLPolicyParser(relative)
                try:
                    parser.feed(html)
                    parser.close()
                except Exception as error:  # HTMLParser may reject malformed references.
                    errors.append(
                        f"{name}:challenge_render: malformed HTML file {relative}: {error}"
                    )
                else:
                    if (
                        parser.head_count != 1
                        or len(parser.csp_values) != 1
                        or parser.csp_values[0] not in ACCEPTED_RENDER_CSPS
                    ):
                        errors.append(
                            f"{name}:challenge_render: HTML must contain exactly one active trusted CSP: "
                            f"{relative}"
                        )
                    if parser.script_depth or parser.script_sources != REQUIRED_RENDER_SCRIPTS:
                        parser.errors.append(
                            "HTML must load each trusted runtime exactly once and in order"
                        )
                    errors.extend(
                        f"{name}:challenge_render: {error}: {relative}"
                        for error in parser.errors
                    )
        if path.suffix.lower() == ".js":
            accepted_script_hashes = TRUSTED_RENDER_SCRIPTS.get(relative)
            if accepted_script_hashes is None:
                errors.append(
                    f"{name}:challenge_render: untrusted JavaScript file: {relative}"
                )
            elif files[-1]["sha256"] not in accepted_script_hashes:
                errors.append(
                    f"{name}:challenge_render: trusted runtime bytes do not match: {relative}"
                )
    for script in TRUSTED_RENDER_SCRIPTS:
        script_path = bundle / script
        if script_path.is_symlink() or not script_path.is_file():
            errors.append(f"{name}:challenge_render: missing trusted runtime: {script}")
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    actual_tree_hash = hashlib.sha256(canonical).hexdigest()
    if actual_tree_hash != tree_hash:
        errors.append(
            f"{name}:challenge_render.artifact_tree_sha256: content hashes to {actual_tree_hash}"
        )
    manifest_path = bundle / "artifact-manifest.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.stat().st_size > MAX_RENDER_FILE_BYTES
    ):
        errors.append(f"{name}:challenge_render: missing, symbolic, or oversized artifact manifest")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"{name}:challenge_render: invalid artifact-manifest.json: {error}")
            manifest = None
        expected_manifest = {
            "schema_version": 1,
            "artifact_tree_sha256": actual_tree_hash,
            "files": files,
        }
        if manifest is not None and manifest != expected_manifest:
            errors.append(f"{name}:challenge_render: artifact manifest does not match its files")
    if not (bundle / entrypoint).is_file() or (bundle / entrypoint).is_symlink():
        errors.append(f"{name}:challenge_render.entrypoint: missing regular file")
    return errors
