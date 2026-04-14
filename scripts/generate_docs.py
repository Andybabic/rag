#!/usr/bin/env python3
"""
Generiert eine vollständige HTML-Projektdokumentation für die Kermit RAG-Plattform.

Datenquellen:
  1. OpenAPI/Swagger-Specs der laufenden FastAPI-Services (/openapi.json)
  2. Pydantic-Modelle aus shared/shared/interface_agreement.py
  3. Statische Metadaten (Abhängigkeiten, Umgebungsvariablen, Datenfluss)

Voraussetzung: Services müssen laufen (make dev-up).
Aufruf:        python scripts/generate_docs.py   oder   make docu
Ausgabe:       docs/interface_agreement.html
"""

from __future__ import annotations

import html
import inspect
import json
import sys
import textwrap
import urllib.request
import urllib.error
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, get_args, get_origin

# ── Projekt-Root ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))

import shared.interface_agreement as iface  # noqa: E402

# ── Service-Konfiguration ────────────────────────────────────────────────────

SERVICES_CONFIG: list[dict[str, Any]] = [
    {"name": "Cleaning Service", "key": "cleaning", "port": 8001, "color": "#059669"},
    {"name": "Data Structure Service", "key": "data_structure", "port": 8002, "color": "#0891b2"},
    {"name": "Embedding Service", "key": "embedding", "port": 8003, "color": "#7c3aed"},
    {"name": "VectorDB Service", "key": "vectordb", "port": 8004, "color": "#2563eb"},
    {"name": "Evaluation Service", "key": "evaluation", "port": 8005, "color": "#d97706"},
    {"name": "Frontend Service", "key": "frontend", "port": 3000, "color": "#dc2626"},
]

# ── Abhängigkeitsmatrix ──────────────────────────────────────────────────────

SERVICE_NAMES = ["Frontend", "Evaluation", "Embedding", "VectorDB", "Cleaning", "Data Structure"]
DEP_TARGETS = [
    "Cleaning", "Data Structure", "Embedding", "VectorDB",
    "Evaluation", "Ollama", "Qdrant", "PostgreSQL",
]

DEPS: dict[str, list[str]] = {
    "Frontend": ["Cleaning", "Data Structure", "Embedding", "VectorDB", "Evaluation"],
    "Evaluation": ["Embedding", "VectorDB", "Ollama", "PostgreSQL"],
    "Embedding": ["Ollama"],
    "VectorDB": ["Qdrant"],
    "Cleaning": [],
    "Data Structure": [],
}

# ── Umgebungsvariablen ───────────────────────────────────────────────────────

ENV_VARS = [
    ("CLEANING_SERVICE_URL", "Frontend", "http://cleaning:8001"),
    ("DATA_STRUCTURE_SERVICE_URL", "Frontend", "http://data_structure:8002"),
    ("EMBEDDING_SERVICE_URL", "Frontend, Evaluation", "http://embedding:8003"),
    ("VECTORDB_SERVICE_URL", "Frontend, Evaluation", "http://vectordb:8004"),
    ("EVALUATION_SERVICE_URL", "Frontend", "http://evaluation:8005"),
    ("OLLAMA_BASE_URL", "Embedding, Evaluation", "http://ollama:11434"),
    ("EMBEDDING_MODEL", "Embedding", "qwen3-embedding:0.6b"),
    ("LLM_MODEL", "Evaluation", "qwen2.5:14b"),
    ("QDRANT_HOST", "VectorDB", "qdrant"),
    ("QDRANT_PORT", "VectorDB", "6333"),
    ("DATABASE_URL", "Evaluation", "(PostgreSQL Connection String)"),
    ("AGENT_MAX_STEPS", "Evaluation", "5"),
]

# ── OpenAPI Fetching ─────────────────────────────────────────────────────────


def _fetch_openapi(port: int, timeout: float = 3.0) -> dict | None:
    """Holt die OpenAPI-Spec eines laufenden Services."""
    url = f"http://localhost:{port}/openapi.json"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _extract_endpoints(spec: dict, service_name: str) -> list[dict]:
    """Extrahiert Endpoint-Informationen aus einer OpenAPI-Spec."""
    endpoints = []
    paths = spec.get("paths", {})

    for path, methods in sorted(paths.items()):
        for method, details in methods.items():
            if method in ("get", "post", "put", "delete", "patch"):
                # Request Body Schema
                req_schema = None
                req_body = details.get("requestBody", {})
                json_content = (
                    req_body.get("content", {}).get("application/json", {})
                )
                if json_content:
                    schema_ref = json_content.get("schema", {})
                    req_schema = _resolve_schema_name(schema_ref)

                # Multipart (file upload)
                multipart = (
                    req_body.get("content", {}).get("multipart/form-data", {})
                )
                if multipart and not req_schema:
                    req_schema = "multipart/form-data"

                # Response Schema
                resp_schema = None
                resp_200 = details.get("responses", {}).get("200", {})
                resp_content = (
                    resp_200.get("content", {}).get("application/json", {})
                )
                if resp_content:
                    schema_ref = resp_content.get("schema", {})
                    resp_schema = _resolve_schema_name(schema_ref)

                # Parameters
                params = details.get("parameters", [])
                path_params = [
                    p for p in params if p.get("in") == "path"
                ]
                query_params = [
                    p for p in params if p.get("in") == "query"
                ]

                endpoints.append({
                    "service": service_name,
                    "method": method.upper(),
                    "path": path,
                    "summary": details.get("summary", ""),
                    "description": details.get("description", ""),
                    "request_schema": req_schema,
                    "response_schema": resp_schema,
                    "path_params": path_params,
                    "query_params": query_params,
                    "tags": details.get("tags", []),
                    "operation_id": details.get("operationId", ""),
                })
    return endpoints


def _resolve_schema_name(schema: dict) -> str | None:
    """Löst einen $ref oder Schema zu einem lesbaren Namen auf."""
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]
    if schema.get("type") == "array" and "items" in schema:
        inner = _resolve_schema_name(schema["items"])
        return f"list[{inner}]" if inner else "list"
    if "title" in schema:
        return schema["title"]
    return None


def _extract_schemas(spec: dict) -> dict[str, dict]:
    """Extrahiert alle Schema-Definitionen aus der OpenAPI-Spec."""
    components = spec.get("components", {})
    return components.get("schemas", {})


# ── Pydantic-Introspection (Fallback + Ergänzung) ───────────────────────────


def _type_name(annotation) -> str:
    if annotation is inspect.Parameter.empty or annotation is None:
        return "Any"
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is list:
        inner = _type_name(args[0]) if args else "Any"
        return f"list[{inner}]"
    if origin is dict:
        k = _type_name(args[0]) if args else "str"
        v = _type_name(args[1]) if len(args) > 1 else "Any"
        return f"dict[{k}, {v}]"
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _get_iface_field_rows(model_cls) -> list[tuple[str, str, str, str]]:
    rows = []
    for name, field in model_cls.model_fields.items():
        typ = _type_name(field.annotation)
        if field.default is not None and field.default is not ...:
            default = repr(field.default)
        elif field.default_factory is not None:
            default = "(factory)"
        else:
            default = "required"
        desc = field.description or ""
        rows.append((name, typ, default, desc))
    return rows


# ── Schema → Table Rows (OpenAPI JSON Schema) ───────────────────────────────


def _schema_field_rows(
    schema: dict, all_schemas: dict
) -> list[tuple[str, str, str, str]]:
    """Extrahiert Felder aus einem OpenAPI JSON-Schema."""
    rows = []
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    for fname, fdef in properties.items():
        typ = _json_schema_type(fdef, all_schemas)
        default = fdef.get("default", "—")
        if fname in required and default == "—":
            default = "required"
        else:
            default = repr(default) if default != "—" else "—"
        desc = fdef.get("description", fdef.get("title", ""))
        rows.append((fname, typ, default, desc))
    return rows


def _json_schema_type(schema: dict, all_schemas: dict) -> str:
    """Menschenlesbare Typdarstellung aus JSON-Schema."""
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]

    # anyOf / oneOf (Optional types)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            types = []
            has_null = False
            for s in schema[key]:
                if s.get("type") == "null":
                    has_null = True
                else:
                    types.append(_json_schema_type(s, all_schemas))
            result = " | ".join(types) if types else "Any"
            return f"Optional[{result}]" if has_null else result

    typ = schema.get("type", "")

    if typ == "array":
        items = schema.get("items", {})
        inner = _json_schema_type(items, all_schemas)
        return f"list[{inner}]"
    if typ == "object":
        additional = schema.get("additionalProperties")
        if additional and isinstance(additional, dict):
            vtype = _json_schema_type(additional, all_schemas)
            return f"dict[str, {vtype}]"
        return "dict"
    if typ == "string":
        enum = schema.get("enum")
        if enum:
            return "enum: " + " | ".join(f'"{v}"' for v in enum)
        fmt = schema.get("format", "")
        return f"str ({fmt})" if fmt else "str"
    if typ == "integer":
        return "int"
    if typ == "number":
        return "float"
    if typ == "boolean":
        return "bool"
    if typ == "null":
        return "None"

    return typ or "Any"


# ── HTML Helpers ─────────────────────────────────────────────────────────────


def _esc(text: str) -> str:
    return html.escape(str(text))


METHOD_COLORS = {
    "GET": "#16a34a",
    "POST": "#2563eb",
    "PUT": "#ea580c",
    "DELETE": "#dc2626",
    "PATCH": "#7c3aed",
}


# ── HTML Generation ──────────────────────────────────────────────────────────


def _generate_html() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Fetch all OpenAPI specs
    all_specs: dict[str, dict] = {}
    all_endpoints: list[dict] = []
    all_schemas: dict[str, dict] = {}  # merged from all services
    service_status: dict[str, bool] = {}

    for svc in SERVICES_CONFIG:
        spec = _fetch_openapi(svc["port"])
        service_status[svc["key"]] = spec is not None
        if spec:
            all_specs[svc["key"]] = spec
            eps = _extract_endpoints(spec, svc["name"])
            all_endpoints.extend(eps)
            schemas = _extract_schemas(spec)
            for sname, sdef in schemas.items():
                # Prefix mit Service-Key falls Namenskollision
                key = sname
                if key in all_schemas and all_schemas[key] != sdef:
                    key = f"{svc['key']}.{sname}"
                all_schemas[key] = sdef

    online_count = sum(1 for v in service_status.values() if v)
    total_count = len(SERVICES_CONFIG)

    parts: list[str] = []
    w = parts.append

    w("<!DOCTYPE html>")
    w('<html lang="de">')
    w("<head>")
    w('<meta charset="utf-8">')
    w('<meta name="viewport" content="width=device-width, initial-scale=1">')
    w("<title>Kermit – Projektdokumentation</title>")
    w(_css())
    w("</head>")
    w("<body>")

    # ── Header ────────────────────────────────────────────────
    w('<header class="hero">')
    w("<h1>Kermit RAG Platform</h1>")
    w("<p>Vollständige Projektdokumentation – generiert aus den OpenAPI-Specs der laufenden Services</p>")
    w(f'<p class="meta">Generiert: {ts} · '
      f'{online_count}/{total_count} Services erreichbar</p>')
    w("</header>")

    # ── Navigation ────────────────────────────────────────────
    w("<nav><ul>")
    w('<li><a href="#services">Services</a></li>')
    w('<li><a href="#endpoints">Alle Endpoints</a></li>')
    w('<li><a href="#schemas">Schemas</a></li>')
    w('<li><a href="#interface-models">Interface Models</a></li>')
    w('<li><a href="#dependencies">Abhängigkeiten</a></li>')
    w('<li><a href="#dataflow">Datenfluss</a></li>')
    w('<li><a href="#env">Konfiguration</a></li>')
    w("</ul></nav>")

    # ── 1. Service-Übersicht ──────────────────────────────────
    w('<h2 id="services">Services</h2>')
    w('<div class="service-grid">')

    for svc in SERVICES_CONFIG:
        online = service_status.get(svc["key"], False)
        spec = all_specs.get(svc["key"])
        status_class = "online" if online else "offline"
        status_text = "online" if online else "offline"

        ep_count = len([e for e in all_endpoints if e["service"] == svc["name"]])
        schema_count = len(_extract_schemas(spec)) if spec else 0
        version = spec.get("info", {}).get("version", "—") if spec else "—"
        description = spec.get("info", {}).get("description", "") if spec else ""

        w('<div class="service-card">')
        w(f'<div class="service-header" style="border-left: 4px solid {svc["color"]}">')
        w(f'<div class="service-name">{_esc(svc["name"])}</div>')
        w(f'<span class="status-badge {status_class}">{status_text}</span>')
        w("</div>")
        w(f'<div class="service-meta">Port: <strong>{svc["port"]}</strong> · '
          f'Version: <strong>{_esc(version)}</strong></div>')
        if description:
            w(f'<div class="service-desc">{_esc(description)}</div>')
        w(f'<div class="service-stats">'
          f'<span>{ep_count} Endpoints</span>'
          f'<span>{schema_count} Schemas</span>'
          f'</div>')
        w("</div>")

    w("</div>")

    # ── 2. Alle Endpoints (pro Service) ──────────────────────
    w('<h2 id="endpoints">Alle Endpoints</h2>')

    for svc in SERVICES_CONFIG:
        spec = all_specs.get(svc["key"])
        if not spec:
            w(f'<h3 class="svc-heading" style="border-color: {svc["color"]}">'
              f'{_esc(svc["name"])} <span class="offline-hint">(nicht erreichbar)</span></h3>')
            continue

        svc_endpoints = [e for e in all_endpoints if e["service"] == svc["name"]]
        if not svc_endpoints:
            continue

        w(f'<h3 id="svc-{svc["key"]}" class="svc-heading" style="border-color: {svc["color"]}">'
          f'{_esc(svc["name"])} '
          f'<span class="port-badge">:{svc["port"]}</span></h3>')

        # Gruppieren nach Tags
        tagged: dict[str, list[dict]] = {}
        for ep in svc_endpoints:
            tag = ep["tags"][0] if ep["tags"] else "default"
            tagged.setdefault(tag, []).append(ep)

        for tag, eps in tagged.items():
            if tag != "default" and len(tagged) > 1:
                w(f'<h4 class="tag-heading">{_esc(tag)}</h4>')

            for ep in eps:
                method_color = METHOD_COLORS.get(ep["method"], "#64748b")
                w('<div class="endpoint-card">')

                # Method + Path
                w('<div class="endpoint-header">')
                w(f'<span class="method-badge" style="background:{method_color}">'
                  f'{ep["method"]}</span>')
                w(f'<code class="endpoint-path">{_esc(ep["path"])}</code>')
                w("</div>")

                # Summary / Description
                summary = ep["summary"] or ep["description"]
                if summary:
                    w(f'<div class="endpoint-desc">{_esc(summary)}</div>')

                # Path params
                if ep["path_params"]:
                    w('<div class="param-section">')
                    w('<span class="param-label">Path-Parameter:</span>')
                    for p in ep["path_params"]:
                        ptype = p.get("schema", {}).get("type", "string")
                        w(f'<code>{_esc(p["name"])}: {ptype}</code>')
                    w("</div>")

                # Query params
                if ep["query_params"]:
                    w('<div class="param-section">')
                    w('<span class="param-label">Query-Parameter:</span>')
                    for p in ep["query_params"]:
                        ptype = p.get("schema", {}).get("type", "string")
                        req = " (required)" if p.get("required") else ""
                        w(f'<code>{_esc(p["name"])}: {ptype}{req}</code>')
                    w("</div>")

                # Request / Response schemas
                w('<div class="schema-links">')
                if ep["request_schema"]:
                    ref = ep["request_schema"]
                    w(f'<span class="schema-tag req">Request: '
                      f'<a href="#schema-{_esc(ref)}">{_esc(ref)}</a></span>')
                if ep["response_schema"]:
                    ref = ep["response_schema"]
                    w(f'<span class="schema-tag resp">Response: '
                      f'<a href="#schema-{_esc(ref)}">{_esc(ref)}</a></span>')
                w("</div>")

                w("</div>")  # endpoint-card

    # ── 3. Schemas (aus OpenAPI) ──────────────────────────────
    w('<h2 id="schemas">Schemas <small>(aus OpenAPI / Swagger)</small></h2>')
    w('<p class="section-desc">Automatisch aus den <code>/openapi.json</code>-Specs '
      'der laufenden Services extrahiert.</p>')

    for schema_name in sorted(all_schemas.keys()):
        schema = all_schemas[schema_name]
        # Skip schemas without properties (e.g. HTTPValidationError)
        if not schema.get("properties") and schema.get("type") != "object":
            continue

        desc = schema.get("description", "")
        rows = _schema_field_rows(schema, all_schemas)

        w(f'<div class="card" id="schema-{_esc(schema_name)}">')
        w(f'<div class="card-title"><code>{_esc(schema_name)}</code></div>')
        if desc:
            w(f'<div class="card-doc">{_esc(desc)}</div>')
        if rows:
            w("<table><thead><tr>")
            w("<th>Feld</th><th>Typ</th><th>Default</th><th>Beschreibung</th>")
            w("</tr></thead><tbody>")
            for fname, ftype, fdefault, fdesc in rows:
                w("<tr>")
                w(f"<td><code>{_esc(fname)}</code></td>")
                # Link type if it's a known schema
                base_type = ftype.split("[")[-1].rstrip("]") if "[" in ftype else ftype
                if base_type in all_schemas:
                    w(f'<td><a href="#schema-{_esc(base_type)}"><code>{_esc(ftype)}</code></a></td>')
                else:
                    w(f"<td><code>{_esc(ftype)}</code></td>")
                w(f"<td><code>{_esc(fdefault)}</code></td>")
                w(f"<td>{_esc(fdesc)}</td>")
                w("</tr>")
            w("</tbody></table>")
        w("</div>")

    # ── 4. Interface Models (aus interface_agreement.py) ──────
    w('<h2 id="interface-models">Interface Models '
      '<small>(aus interface_agreement.py)</small></h2>')
    w('<p class="section-desc">Zentrale Pydantic-Modelle, die den Vertrag zwischen '
      'den Services definieren – unabhängig davon ob die Services laufen.</p>')

    # Enums
    w("<h3>Enums</h3>")
    enums = [
        (name, obj)
        for name, obj in inspect.getmembers(iface, inspect.isclass)
        if issubclass(obj, Enum) and obj is not Enum
    ]
    for name, cls in enums:
        doc = inspect.getdoc(cls) or ""
        w('<div class="card">')
        w(f'<div class="card-title"><code>{_esc(name)}</code></div>')
        if doc:
            w(f'<div class="card-doc">{_esc(doc)}</div>')
        w('<div class="enum-values">')
        for member in cls:
            w(f'<span class="enum-val">{_esc(member.value)}</span>')
        w("</div></div>")

    # Models
    w("<h3>Modelle</h3>")
    from pydantic import BaseModel as BM

    models = [
        (name, obj)
        for name, obj in inspect.getmembers(iface, inspect.isclass)
        if issubclass(obj, BM) and obj is not BM
    ]
    src_lines = inspect.getsourcelines(iface)[0]

    def _order(item):
        name = item[0]
        try:
            return next(i for i, l in enumerate(src_lines) if f"class {name}" in l)
        except StopIteration:
            return 9999

    models.sort(key=_order)

    for name, cls in models:
        doc = inspect.getdoc(cls) or ""
        rows = _get_iface_field_rows(cls)
        w(f'<div class="card" id="model-{_esc(name)}">')
        w(f'<div class="card-title"><code>{_esc(name)}</code></div>')
        if doc:
            w(f'<div class="card-doc">{_esc(doc)}</div>')
        if rows:
            w("<table><thead><tr>")
            w("<th>Feld</th><th>Typ</th><th>Default</th><th>Beschreibung</th>")
            w("</tr></thead><tbody>")
            for fname, ftype, fdefault, fdesc in rows:
                w("<tr>")
                w(f"<td><code>{_esc(fname)}</code></td>")
                w(f"<td><code>{_esc(ftype)}</code></td>")
                w(f"<td><code>{_esc(fdefault)}</code></td>")
                w(f"<td>{_esc(fdesc)}</td>")
                w("</tr>")
            w("</tbody></table>")
        w("</div>")

    # ── 5. Abhängigkeitsmatrix ────────────────────────────────
    w('<h2 id="dependencies">Service-Abhängigkeitsmatrix</h2>')
    w('<table class="dep-matrix"><thead><tr>')
    w("<th>Aufrufer / Ziel</th>")
    for t in DEP_TARGETS:
        w(f"<th>{_esc(t)}</th>")
    w("</tr></thead><tbody>")
    for svc in SERVICE_NAMES:
        w("<tr>")
        w(f"<td>{_esc(svc)}</td>")
        for t in DEP_TARGETS:
            if t in DEPS.get(svc, []):
                w('<td class="check">&#10003;</td>')
            else:
                w("<td></td>")
        w("</tr>")
    w("</tbody></table>")

    # ── 6. Datenfluss-Diagramm ────────────────────────────────
    w('<h2 id="dataflow">Datenfluss-Diagramm</h2>')

    w('<h3>Ingestion-Pipeline (Phase 1)</h3>')
    w('<div class="diagram">')
    w(
        '<span class="hl">User Upload</span>\n'
        '<span class="arrow">      │</span>\n'
        '<span class="arrow">      ▼</span>\n'
        '<span class="svc">  Cleaning Service</span>  <span class="ep">POST /v1/clean</span>\n'
        '<span class="arrow">      │</span>  file  ──▶  markdown + metadata\n'
        '<span class="arrow">      ▼</span>\n'
        '<span class="svc">  Data Structure</span>     <span class="ep">POST /v1/structure</span>\n'
        '<span class="arrow">      │</span>  markdown  ──▶  chunks[] + routing.collection\n'
        '<span class="arrow">      ▼</span>\n'
        '<span class="svc">  Embedding Service</span>  <span class="ep">POST /v1/embed/batch</span>\n'
        '<span class="arrow">      │</span>  chunk.text[]  ──▶  vector[]\n'
        '<span class="arrow">      ▼</span>\n'
        '<span class="svc">  VectorDB Service</span>   <span class="ep">POST /v1/upsert</span>\n'
        '<span class="arrow">      │</span>  { chunk_id, vector, metadata }  ──▶  stored in Qdrant\n'
        '<span class="arrow">      ▼</span>\n'
        '<span class="hl">  ✓ Ingestion complete</span>'
    )
    w("</div>")

    w('<h3>Query-Pipeline (Phase 2–4)</h3>')
    w('<div class="diagram">')
    w(
        '<span class="hl">User Query</span>\n'
        '<span class="arrow">      │</span>\n'
        '<span class="arrow">      ▼</span>\n'
        '<span class="svc">  Evaluation Service</span>  <span class="ep">POST /v1/agent/query</span>\n'
        '<span class="arrow">      │</span>\n'
        '<span class="arrow">      ├──▶</span> <span class="hl">Agent Step 1..N</span>\n'
        '<span class="arrow">      │     │</span>\n'
        '<span class="arrow">      │     ├──</span> ACTION: <span class="ep">SEARCH</span>\n'
        '<span class="arrow">      │     │     ├──▶</span> <span class="svc">Embedding</span>  <span class="ep">/v1/embed</span>  (query → vector)\n'
        '<span class="arrow">      │     │     ├──▶</span> <span class="svc">VectorDB</span>   <span class="ep">/v1/search</span> (vector → results)\n'
        '<span class="arrow">      │     │     └──▶</span> Rerank + Evaluate (lokal)\n'
        '<span class="arrow">      │     │</span>\n'
        '<span class="arrow">      │     ├──</span> ACTION: <span class="ep">SEARCH_CNC</span> (nur GW St. Pölten)\n'
        '<span class="arrow">      │     │     ├──▶</span> <span class="svc">Embedding</span>  <span class="ep">/v1/embed</span>\n'
        '<span class="arrow">      │     │     └──▶</span> <span class="svc">VectorDB</span>   <span class="ep">/v1/search/cross</span>\n'
        '<span class="arrow">      │     │</span>\n'
        '<span class="arrow">      │     ├──</span> ACTION: <span class="ep">LOOKUP_SOURCES</span>  →  <span class="svc">VectorDB</span> <span class="ep">/v1/collections</span>\n'
        '<span class="arrow">      │     ├──</span> ACTION: <span class="ep">RECALL_MEMORY</span>   →  lokal (File/DB)\n'
        '<span class="arrow">      │     ├──</span> ACTION: <span class="ep">CLARIFY</span>         →  Rückfrage an User\n'
        '<span class="arrow">      │     └──</span> ACTION: <span class="ep">FINAL_ANSWER</span>    →  LLM generiert Antwort\n'
        '<span class="arrow">      │</span>\n'
        '<span class="arrow">      ├──▶</span> <span class="svc">Evaluation</span>  <span class="ep">/v1/citations</span>  (Quellenzuordnung)\n'
        '<span class="arrow">      │</span>\n'
        '<span class="arrow">      ▼</span>\n'
        '<span class="hl">  Frontend</span>  ──▶  Antwort + Referenzen + Agent-Schritte\n'
        '<span class="arrow">      │</span>\n'
        '<span class="arrow">      └──▶</span> <span class="svc">Evaluation</span>  <span class="ep">/v1/log</span>  (Feedback → PostgreSQL)'
    )
    w("</div>")

    # ── 7. Konfiguration ──────────────────────────────────────
    w('<h2 id="env">Konfiguration (Umgebungsvariablen)</h2>')
    w("<table><thead><tr>")
    w("<th>Variable</th><th>Service(s)</th><th>Default</th>")
    w("</tr></thead><tbody>")
    for var, svc, default in ENV_VARS:
        w(f"<tr><td><code>{_esc(var)}</code></td><td>{_esc(svc)}</td>")
        w(f"<td><code>{_esc(default)}</code></td></tr>")
    w("</tbody></table>")

    # ── Footer ────────────────────────────────────────────────
    w(f'<footer>Generiert am {ts} via <code>make docu</code> · '
      f'Quelle: OpenAPI-Specs + <code>shared/shared/interface_agreement.py</code></footer>')

    w("</body></html>")
    return "\n".join(parts)


# ── CSS ──────────────────────────────────────────────────────────────────────


def _css() -> str:
    return "<style>\n" + textwrap.dedent("""\
    :root {
        --bg: #f8fafc; --fg: #0f172a; --accent: #2563eb;
        --card: #fff; --border: #e2e8f0; --muted: #64748b;
        --green: #16a34a; --red: #dc2626; --orange: #ea580c;
        --tag-bg: #eff6ff; --tag-fg: #1e40af;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: var(--bg); color: var(--fg); line-height: 1.6;
        max-width: 1320px; margin: 0 auto; padding: 0 1.5rem 3rem;
    }

    /* Hero */
    .hero {
        background: linear-gradient(135deg, #1e293b 0%, #334155 100%);
        color: #f1f5f9; border-radius: 12px; padding: 2rem 2.5rem;
        margin: 1.5rem 0 2rem;
    }
    .hero h1 { font-size: 2rem; margin-bottom: .35rem; color: #fff; }
    .hero p { color: #cbd5e1; font-size: .95rem; }
    .hero .meta { font-size: .82rem; color: #94a3b8; margin-top: .5rem; }

    /* Nav */
    nav { margin-bottom: 2rem; position: sticky; top: 0; z-index: 10;
          background: var(--bg); padding: .75rem 0; border-bottom: 1px solid var(--border); }
    nav ul { list-style: none; display: flex; flex-wrap: wrap; gap: .4rem; }
    nav a {
        display: inline-block; padding: .35rem .75rem; border-radius: 6px;
        background: var(--tag-bg); color: var(--tag-fg); font-size: .85rem;
        font-weight: 500; text-decoration: none; transition: background .15s;
    }
    nav a:hover { background: #dbeafe; }

    /* Headings */
    h2 { font-size: 1.4rem; margin: 2.5rem 0 1rem; padding-bottom: .4rem;
         border-bottom: 2px solid var(--border); }
    h3 { font-size: 1.1rem; margin: 1.8rem 0 .75rem; }
    h4 { font-size: .95rem; margin: 1rem 0 .5rem; color: var(--muted); }
    small { font-size: .75em; color: var(--muted); font-weight: 400; }
    .section-desc { color: var(--muted); font-size: .9rem; margin-bottom: 1rem; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }

    /* Service Grid */
    .service-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
                    gap: 1rem; margin: 1rem 0; }
    .service-card { background: var(--card); border: 1px solid var(--border);
                    border-radius: 10px; padding: 1rem; }
    .service-header { display: flex; justify-content: space-between; align-items: center;
                      padding-left: .75rem; margin-bottom: .5rem; }
    .service-name { font-weight: 700; font-size: 1rem; }
    .status-badge { font-size: .72rem; padding: .2rem .5rem; border-radius: 20px;
                    font-weight: 600; text-transform: uppercase; letter-spacing: .03em; }
    .status-badge.online { background: #dcfce7; color: #166534; }
    .status-badge.offline { background: #fef2f2; color: #991b1b; }
    .service-meta { font-size: .82rem; color: var(--muted); margin-bottom: .35rem; }
    .service-desc { font-size: .85rem; color: var(--fg); margin-bottom: .5rem; }
    .service-stats { display: flex; gap: 1rem; font-size: .8rem; color: var(--muted); }

    /* Cards */
    .card { background: var(--card); border: 1px solid var(--border);
            border-radius: 10px; padding: 1.25rem; margin-bottom: 1rem;
            box-shadow: 0 1px 3px rgba(0,0,0,.04); }
    .card-title { font-size: 1rem; font-weight: 600; margin-bottom: .15rem; }
    .card-doc { font-size: .85rem; color: var(--muted); margin-bottom: .75rem;
                white-space: pre-line; }

    /* Tables */
    table { width: 100%; border-collapse: collapse; font-size: .875rem; margin-bottom: .5rem; }
    th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--border); }
    th { background: #f8fafc; font-weight: 600; white-space: nowrap; }
    td code { background: #f1f5f9; padding: .1rem .35rem; border-radius: 4px; font-size: .82rem; }

    /* Enum badges */
    .enum-values { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .25rem; }
    .enum-val { background: var(--tag-bg); color: var(--tag-fg); padding: .15rem .5rem;
                border-radius: 4px; font-size: .8rem; font-family: monospace; }

    /* Endpoints */
    .svc-heading { padding-left: .75rem; border-left: 4px solid; margin: 2rem 0 .75rem; }
    .port-badge { font-size: .75rem; color: var(--muted); font-weight: 400; }
    .offline-hint { font-size: .8rem; color: var(--red); font-weight: 400; }
    .tag-heading { color: var(--muted); font-weight: 500; text-transform: uppercase;
                   letter-spacing: .05em; font-size: .78rem; margin: 1.25rem 0 .5rem; }

    .endpoint-card { background: var(--card); border: 1px solid var(--border);
                     border-radius: 8px; padding: 1rem; margin-bottom: .6rem; }
    .endpoint-header { display: flex; align-items: center; gap: .6rem; margin-bottom: .35rem; }
    .method-badge { display: inline-block; padding: .2rem .5rem; border-radius: 4px;
                    font-size: .72rem; font-weight: 700; color: #fff; min-width: 52px;
                    text-align: center; font-family: monospace; }
    .endpoint-path { font-size: .92rem; font-weight: 500; }
    .endpoint-desc { font-size: .85rem; color: var(--muted); margin-bottom: .4rem; }

    .param-section { font-size: .82rem; margin: .25rem 0; display: flex;
                     flex-wrap: wrap; align-items: center; gap: .4rem; }
    .param-label { color: var(--muted); font-weight: 500; }
    .param-section code { background: #f1f5f9; padding: .15rem .4rem; border-radius: 4px; }

    .schema-links { display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .4rem; }
    .schema-tag { font-size: .78rem; padding: .2rem .55rem; border-radius: 4px; font-weight: 500; }
    .schema-tag.req { background: #eff6ff; color: #1e40af; }
    .schema-tag.resp { background: #f0fdf4; color: #166534; }
    .schema-tag a { text-decoration: none; }
    .schema-tag a:hover { text-decoration: underline; }

    /* Dependency matrix */
    .dep-matrix td, .dep-matrix th { text-align: center; padding: .4rem .5rem; }
    .dep-matrix .check { color: var(--green); font-weight: 700; font-size: 1.1rem; }
    .dep-matrix td:first-child, .dep-matrix th:first-child { text-align: left; font-weight: 600; }

    /* Diagram */
    .diagram { background: #1e293b; color: #e2e8f0; border-radius: 10px;
               padding: 1.25rem; overflow-x: auto;
               font-family: 'JetBrains Mono', 'Fira Code', monospace;
               font-size: .82rem; line-height: 1.7; white-space: pre; margin: 1rem 0; }
    .diagram .hl { color: #38bdf8; }
    .diagram .arrow { color: #94a3b8; }
    .diagram .svc { color: #4ade80; font-weight: 600; }
    .diagram .ep { color: #fbbf24; }

    /* Footer */
    footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--border);
             font-size: .8rem; color: var(--muted); }

    /* Print */
    @media print { nav { position: static; } .hero { background: #334155; } }
    """) + "</style>"


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    out = ROOT / "docs" / "interface_agreement.html"
    out.write_text(_generate_html(), encoding="utf-8")
    print(f"Dokumentation generiert: {out}")
