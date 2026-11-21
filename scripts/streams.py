#!/usr/bin/env python3
"""Checks and registers Nerv event streams (streams/<domain>/<entity>/).

These checks replace an admission policy, so the Stream gate workflow must pass
before a pull request that touches streams/ can merge.

Usage:
  scripts/streams.py changed <base-ref>   Print the stream folders changed since <base-ref>.
  scripts/streams.py lint <dir>...        Local checks: Avro syntax, AsyncAPI document,
                                          topic and user rules. No network.
  scripts/streams.py check <dir>...       Test each schema against every registered version
                                          of its artifact (dry run, nothing is registered).
  scripts/streams.py register <dir>...    Register each schema. Idempotent: an unchanged
                                          schema does not create a new version.
  scripts/streams.py all                  Print every stream folder.

Registry access (check and register):
  SCHEMA_REGISTRY_URL    Base URL, for example http://nerv-registry-app-service.kafka.svc:8080
  SCHEMA_REGISTRY_TOKEN  Bearer token of a registry identity (read-only is enough for check)

Lint also runs the AsyncAPI CLI: set ASYNCAPI_CLI to its command (default: asyncapi).

Each stream folder maps to the registry artifact <domain>/<entity>-state, for
example streams/billing/customer-bill -> group billing, artifact customer-bill-state.

Needs Python 3.10+, avro and pyyaml (scripts/requirements-streams.txt).
"""

from __future__ import annotations

import json
import os
import posixpath
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import avro.schema
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
STRIMZI_API_VERSION = "kafka.strimzi.io/v1"
WRITE_OPERATIONS = {"Write", "All"}


class Failure(Exception):
    """A check failed. The message explains what to fix."""


# --- Stream folders ----------------------------------------------------------


def stream_dirs() -> list[Path]:
    root = REPO_ROOT / "streams"
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/*") if p.is_dir())


def changed_stream_dirs(base: str) -> list[Path]:
    """Stream folders with changed files since the merge base with <base>."""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD", "--", "streams/"],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout
    dirs = set()
    for name in out.splitlines():
        parts = name.split("/")
        if len(parts) >= 4:  # streams/<domain>/<entity>/<file...>
            d = REPO_ROOT.joinpath(*parts[:3])
            if d.is_dir():  # skip deleted folders
                dirs.add(d)
    return sorted(dirs)


def resolve_dir(arg: str) -> Path:
    d = (REPO_ROOT / arg).resolve() if not Path(arg).is_absolute() else Path(arg)
    parts = d.relative_to(REPO_ROOT).parts
    if len(parts) != 3 or parts[0] != "streams":
        raise SystemExit(f"Not a stream folder (streams/<domain>/<entity>): {arg}")
    return d


def rel(p: Path) -> str:
    return p.relative_to(REPO_ROOT).as_posix()


def artifact_of(d: Path) -> tuple[str, str]:
    """(groupId, artifactId) of a stream folder."""
    return d.parent.name, f"{d.name}-state"


def schema_file(d: Path) -> Path:
    found = sorted((d / "schema").glob("*.avsc"))
    if len(found) != 1:
        raise Failure(
            f"{rel(d)}/schema/ must contain exactly one .avsc file, found {len(found)}"
        )
    return found[0]


def load_yaml_docs(path: Path) -> list[dict]:
    if not path.is_file():
        raise Failure(f"{rel(path)} is missing")
    try:
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]
    except yaml.YAMLError as e:
        raise Failure(f"{rel(path)} is not valid YAML: {e}") from e
    if not all(isinstance(doc, dict) for doc in docs):
        raise Failure(f"{rel(path)} must contain only YAML mappings")
    return docs


# --- Lint: steps 1, 3 and 4 ---------------------------------------------------


def lint_schema(d: Path) -> None:
    """Step 1: the schema is valid Avro."""
    path = schema_file(d)
    try:
        avro.schema.parse(path.read_text(encoding="utf-8"))
    except (avro.errors.AvroException, json.JSONDecodeError) as e:
        raise Failure(f"{rel(path)} is not a valid Avro schema: {e}") from e


def find_refs(node, found: list[str]) -> list[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                found.append(v)
            else:
                find_refs(v, found)
    elif isinstance(node, list):
        for v in node:
            find_refs(v, found)
    return found


def lint_asyncapi(d: Path) -> None:
    """Step 3: asyncapi.yaml references the folder's schema and is valid AsyncAPI."""
    path = d / "asyncapi.yaml"
    docs = load_yaml_docs(path)
    if len(docs) != 1:
        raise Failure(f"{rel(path)} must contain exactly one document")

    schema = schema_file(d)
    expected = schema.relative_to(d).as_posix()
    file_refs = [r for r in find_refs(docs[0], []) if not r.startswith("#")]
    for ref in file_refs:
        target = posixpath.normpath(ref.split("#", 1)[0])
        if target.startswith("../") or target.startswith("/") or "://" in ref:
            raise Failure(f"{rel(path)} references {ref}, outside the stream folder")
    if not any(posixpath.normpath(r.split("#", 1)[0]) == expected for r in file_refs):
        raise Failure(f"{rel(path)} must reference ./{expected} in a message payload")

    cli = shlex.split(os.environ.get("ASYNCAPI_CLI", "asyncapi"))
    result = subprocess.run(
        [*cli, "validate", str(path), "--fail-severity=error"],
        cwd=d, capture_output=True, text=True, shell=os.name == "nt",
    )
    if result.returncode != 0:
        raise Failure(f"asyncapi validate {rel(path)} failed:\n{result.stdout}{result.stderr}")


def acl_matches_topic(resource: dict, topic: str) -> bool:
    if resource.get("type") != "topic":
        return False
    name = resource.get("name", "")
    if resource.get("patternType", "literal") == "prefix":
        return topic.startswith(name)
    return name in (topic, "*")


def lint_kafka(d: Path) -> None:
    """Step 4: one compacted topic and exactly one KafkaUser that can write to it."""
    topics = load_yaml_docs(d / "kafkatopic.yaml")
    if len(topics) != 1 or topics[0].get("kind") != "KafkaTopic":
        raise Failure(f"{rel(d)}/kafkatopic.yaml must contain exactly one KafkaTopic")
    topic_doc = topics[0]
    users = load_yaml_docs(d / "kafkausers.yaml")
    for doc in [topic_doc, *users]:
        if doc.get("apiVersion") != STRIMZI_API_VERSION:
            raise Failure(
                f"{doc.get('kind')} {doc.get('metadata', {}).get('name')} must use "
                f"apiVersion {STRIMZI_API_VERSION}, not {doc.get('apiVersion')}"
            )
    if any(u.get("kind") != "KafkaUser" for u in users):
        raise Failure(f"{rel(d)}/kafkausers.yaml must contain only KafkaUser resources")

    spec = topic_doc.get("spec") or {}
    topic = spec.get("topicName") or topic_doc.get("metadata", {}).get("name")
    policy = str((spec.get("config") or {}).get("cleanup.policy", "")).replace(" ", "")
    if policy != "compact":
        raise Failure(
            f"Topic {topic} must set cleanup.policy: compact (found {policy or 'nothing'}): "
            "state streams keep the latest snapshot of every key"
        )

    writers = []
    for user in users:
        acls = ((user.get("spec") or {}).get("authorization") or {}).get("acls") or []
        for acl in acls:
            if acl.get("type", "allow") != "allow":
                continue
            if acl_matches_topic(acl.get("resource") or {}, topic) and WRITE_OPERATIONS & set(
                acl.get("operations") or []
            ):
                writers.append(user["metadata"]["name"])
                break
    if len(writers) != 1:
        raise Failure(
            f"Exactly one KafkaUser must have Write on {topic}, found {len(writers)}"
            + (f": {', '.join(writers)}" if writers else "")
        )


def kafka_names(d: Path) -> set[tuple[str, str]]:
    names = set()
    for f in ("kafkatopic.yaml", "kafkausers.yaml"):
        path = d / f
        if path.is_file():
            for doc in load_yaml_docs(path):
                names.add((doc.get("kind"), (doc.get("metadata") or {}).get("name")))
    return names


def lint_unique_names(d: Path) -> None:
    """No other stream folder declares the same KafkaTopic or KafkaUser."""
    mine = kafka_names(d)
    for other in stream_dirs():
        if other == d:
            continue
        for kind, name in sorted(mine & kafka_names(other)):
            raise Failure(
                f"{kind} {name} is also declared in {rel(other)}. Names must be unique "
                "across streams: a consumer of several streams needs one KafkaUser per stream"
            )


def lint_catalog(d: Path) -> None:
    """The API entity points at the folder's registry artifact."""
    path = d / "catalog-info.yaml"
    group, artifact = artifact_of(d)
    for doc in load_yaml_docs(path):
        if doc.get("kind") != "API":
            continue
        annotations = (doc.get("metadata") or {}).get("annotations") or {}
        value = annotations.get("nerv.io/schema-artifact")
        if value != f"{group}/{artifact}":
            raise Failure(
                f"{rel(path)}: nerv.io/schema-artifact must be {group}/{artifact}, found {value}"
            )
        return
    raise Failure(f"{rel(path)} must contain the stream's API entity")


def lint(d: Path) -> None:
    lint_schema(d)
    lint_kafka(d)
    lint_unique_names(d)
    lint_catalog(d)
    lint_asyncapi(d)


# --- Registry: steps 2 and 5 ---------------------------------------------------


class Registry:
    def __init__(self) -> None:
        url = os.environ.get("SCHEMA_REGISTRY_URL", "").rstrip("/")
        if not url:
            raise SystemExit("SCHEMA_REGISTRY_URL is not set")
        self.base = f"{url}/apis/registry/v3"
        self.token = os.environ.get("SCHEMA_REGISTRY_TOKEN", "")

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return e.code, {"detail": raw.decode(errors="replace")}

    def artifact_path(self, group: str, artifact: str) -> str:
        q = urllib.parse.quote
        return f"/groups/{q(group, safe='')}/artifacts/{q(artifact, safe='')}"

    def version_count(self, group: str, artifact: str) -> int | None:
        """Number of versions, or None if the artifact does not exist."""
        status, body = self.call("GET", self.artifact_path(group, artifact) + "/versions",
                                 {"limit": 1})
        if status == 404:
            return None
        expect_ok(status, body, f"list versions of {group}/{artifact}")
        return body["count"]


def expect_ok(status: int, body, action: str) -> None:
    if status < 300:
        return
    detail = (body or {}).get("detail") or (body or {}).get("title") or body
    causes = (body or {}).get("causes") or []
    lines = [f"Registry rejected {action} (HTTP {status}): {detail}"]
    lines += [f"  - {c.get('context', '')}: {c.get('description', '')}" for c in causes]
    if status in (401, 403):
        lines.append("  Check SCHEMA_REGISTRY_TOKEN and the registry group mapping.")
    raise Failure("\n".join(lines))


def version_body(content: str) -> dict:
    return {"content": {"content": content, "contentType": "application/json"}}


def check(registry: Registry, d: Path) -> None:
    """Step 2: dry-run the schema against every version (FULL_TRANSITIVE)."""
    group, artifact = artifact_of(d)
    content = schema_file(d).read_text(encoding="utf-8")
    count = registry.version_count(group, artifact)
    if count is None:
        status, body = registry.call(
            "POST", f"/groups/{urllib.parse.quote(group, safe='')}/artifacts",
            {"dryRun": "true"},
            {"artifactId": artifact, "artifactType": "AVRO", "firstVersion": version_body(content)},
        )
        expect_ok(status, body, f"new artifact {group}/{artifact}")
        print(f"{rel(d)}: new artifact {group}/{artifact}, schema is valid")
        return
    status, body = registry.call(
        "POST", registry.artifact_path(group, artifact) + "/versions",
        {"dryRun": "true"}, version_body(content),
    )
    expect_ok(status, body, f"a new version of {group}/{artifact}")
    print(f"{rel(d)}: compatible with all {count} registered version(s) of {group}/{artifact}")


def register(registry: Registry, d: Path) -> None:
    """Step 5: create the artifact or a new version, unless the content exists."""
    group, artifact = artifact_of(d)
    content = schema_file(d).read_text(encoding="utf-8")
    before = registry.version_count(group, artifact)
    status, body = registry.call(
        "POST", f"/groups/{urllib.parse.quote(group, safe='')}/artifacts",
        # FIND_OR_CREATE_VERSION returns the existing version when the
        # canonicalized content is already registered.
        {"ifExists": "FIND_OR_CREATE_VERSION", "canonical": "true"},
        {"artifactId": artifact, "artifactType": "AVRO", "firstVersion": version_body(content)},
    )
    expect_ok(status, body, f"register {group}/{artifact}")
    version = (body or {}).get("version") or {}
    after = registry.version_count(group, artifact)
    what = "unchanged, already registered as" if before == after else "registered"
    print(f"{rel(d)}: {group}/{artifact} {what} version {version.get('version')} "
          f"(globalId {version.get('globalId')})")


# --- Main ----------------------------------------------------------------------


def run_each(dirs: list[Path], fn) -> int:
    failed = 0
    for d in dirs:
        try:
            fn(d)
        except Failure as e:
            failed += 1
            print(f"::error title={rel(d)}::{e}".replace("\n", "%0A"))
            print(f"FAIL {rel(d)}\n{e}", file=sys.stderr)
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "changed":
        if len(args) != 1:
            raise SystemExit("Usage: scripts/streams.py changed <base-ref>")
        print("\n".join(rel(d) for d in changed_stream_dirs(args[0])))
        return 0
    if cmd == "all":
        print("\n".join(rel(d) for d in stream_dirs()))
        return 0
    dirs = [resolve_dir(a) for a in args]
    if cmd == "lint":
        return run_each(dirs, lambda d: (lint(d), print(f"{rel(d)}: lint passed")))
    if cmd == "check":
        registry = Registry()
        return run_each(dirs, lambda d: check(registry, d))
    if cmd == "register":
        registry = Registry()
        return run_each(dirs, lambda d: register(registry, d))
    raise SystemExit(f"Unknown command: {cmd}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
