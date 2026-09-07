#!/usr/bin/env python3
"""Elasticsearch-side helper for the exemplar demo. Standard library only.

    ./scripts/exemplars.py api-key    # create an API key with the right privileges
    ./scripts/exemplars.py verify     # prove exemplars ingested, end to end
    ./scripts/exemplars.py check      # cluster preconditions only

`verify` runs the checks itself when it finds no exemplars, so `check` is only
there for when you want the preconditions without waiting for a load run.

This script never creates an index, data stream or template. Apart from
`api-key` (which writes to the security API), every request is a read.
Elasticsearch creates each data stream itself on first write, which is why the
API key needs `auto_configure`.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

METRICS_DS = "metrics-generic.otel-default"
EXEMPLARS_DS = "exemplars-generic.otel-default"
TRACES_DS = "traces-generic.otel-default"

# The class PR #158350 adds. Its presence at a given commit is what proves a
# build can ingest exemplars at all.
PR_MARKER_PATH = (
    "x-pack/plugin/otel-data/src/main/java/org/elasticsearch/xpack/oteldata/"
    "otlp/docbuilder/ExemplarDocumentBuilder.java"
)
PR_URL = "https://github.com/elastic/elasticsearch/pull/158350"

ENV_OVERRIDES = {"ELASTIC_URL", "ELASTIC_API_KEY", "ES_USERNAME", "ES_PASSWORD", "ES_URL"}


def c(code: str, text: str) -> str:
    return text if os.getenv("NO_COLOR") else "\033[" + code + "m" + text + "\033[0m"


def bold(t):
    return c("1", t)


def red(t):
    return c("31", t)


def green(t):
    return c("32", t)


def yellow(t):
    return c("33", t)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def load_env():
    """Read .env, letting real environment variables win."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    env.update({k: v for k, v in os.environ.items() if k in ENV_OVERRIDES})
    return env


def es_url(env):
    """The cluster address as reachable from *this machine*.

    ELASTIC_URL is written from a container's point of view, so it normally
    names the host gateway. That name may not resolve here, so translate it.
    """
    url = env.get("ES_URL") or env.get("ELASTIC_URL") or "http://localhost:9200"
    return url.replace("host.docker.internal", "localhost").rstrip("/")


def request(env, method, path, body=None, admin=False, timeout=30):
    """Return (status, parsed_json_or_text). Never raises on HTTP errors."""
    url = es_url(env) + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")

    password = env.get("ES_PASSWORD", "")
    api_key = env.get("ELASTIC_API_KEY", "")
    # Only api-key creation needs admin credentials; everything else works with
    # the API key, so prefer whichever suits the task at hand.
    if admin or (password and not api_key):
        if not password:
            print(red("ES_PASSWORD is not set, and this action needs admin credentials."))
            print("Pass it inline, e.g.:")
            print("  ES_USERNAME=elastic-admin ES_PASSWORD=elastic-password "
                  "./scripts/exemplars.py api-key")
            sys.exit(1)
        raw = (env.get("ES_USERNAME", "elastic-admin") + ":" + password).encode()
        req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode())
    elif api_key:
        req.add_header("Authorization", "ApiKey " + api_key)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)}


def _parse(raw):
    try:
        return json.loads(raw)
    except Exception:
        return raw.decode(errors="replace")


# --------------------------------------------------------------------------- #
# api-key
# --------------------------------------------------------------------------- #

ROLE = {
    "otlp_ingest": {
        "cluster": ["monitor"],
        "indices": [
            {
                # exemplars-* is the one people miss. A key without it ingests
                # metrics perfectly while Elasticsearch rejects every exemplar
                # with FORBIDDEN on indices:admin/auto_create.
                "names": ["metrics-*", "exemplars-*", "traces-*", "logs-*"],
                "privileges": ["create_doc", "auto_configure", "view_index_metadata", "read"],
            }
        ],
    }
}


def cmd_api_key(env):
    print(bold("Creating API key on " + es_url(env)))
    status, body = request(
        env, "POST", "/_security/api_key",
        {"name": "otel-metrics-exemplars", "role_descriptors": ROLE},
        admin=True,
    )
    if status != 200 or not isinstance(body, dict) or "encoded" not in body:
        print(red("Failed (HTTP %s): %s" % (status, json.dumps(body)[:400])))
        return 1

    print(green("API key created with create_doc + auto_configure on "
                "metrics-*, exemplars-*, traces-*"))
    write_env_key(body["encoded"])
    return 0


def write_env_key(key):
    if not ENV_FILE.exists():
        print(yellow("\nNo .env found. Copy .env.example to .env and add:"))
        print("\nELASTIC_API_KEY=" + key + "\n")
        return
    text = ENV_FILE.read_text()
    if re.search(r"(?m)^ELASTIC_API_KEY=.*$", text):
        text = re.sub(r"(?m)^ELASTIC_API_KEY=.*$", "ELASTIC_API_KEY=" + key, text)
    else:
        text += "\nELASTIC_API_KEY=" + key + "\n"
    ENV_FILE.write_text(text)
    print(green("Wrote ELASTIC_API_KEY into .env."))
    print("\nRestart so the collector picks it up:  docker compose up -d")


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #

def cmd_check(env, quiet=False):
    failures = [0]

    def ok(msg):
        print(green("  ok    " + msg))

    def warn(msg):
        print(yellow("  warn  " + msg))

    def fail(msg):
        failures[0] += 1
        print(red("  FAIL  " + msg))

    print(bold("Cluster: " + es_url(env)))
    status, root = request(env, "GET", "/")
    if status != 200 or not isinstance(root, dict) or "version" not in root:
        fail("unreachable or unauthorized (HTTP %s): %s" % (status, json.dumps(root)[:200]))
        return 1
    version = root["version"]
    ok("version " + str(version.get("number")))

    # Feature flags enable themselves on snapshot builds; a release build needs
    # the system property.
    if version.get("build_snapshot"):
        ok("snapshot build -- the metric_exemplars feature flag is on automatically")
    else:
        warn('release build -- start ES with ES_JAVA_OPTS='
             '"-Des.metric_exemplars_feature_flag_enabled=true"')

    # The index template is not evidence: exemplars-otel@template reached main
    # before ingestion did, so a cluster can have it and still drop exemplars.
    # The build hash is the real test.
    build_hash = version.get("build_hash", "")
    if not build_hash or build_hash == "unknown":
        warn("no build_hash, cannot confirm the ingestion code is present")
    else:
        present = commit_has_pr_marker(build_hash)
        if present is None:
            warn("could not reach GitHub to check build " + build_hash[:12])
        elif present:
            ok("build " + build_hash[:12] + " contains the exemplar ingestion code")
        else:
            fail("build %s predates %s -- check out that PR and rebuild"
                 % (build_hash[:12], PR_URL))

    for tpl in ("exemplars-otel@template", "metrics-otel@template", "traces-otel@template"):
        status, _ = request(env, "GET", "/_index_template/" + tpl)
        if status == 200:
            ok(tpl + " present")
        else:
            fail(tpl + " missing (HTTP %s)" % status)

    print(bold("API key"))
    if not env.get("ELASTIC_API_KEY"):
        fail("ELASTIC_API_KEY is empty -- run ./scripts/exemplars.py api-key")
    else:
        status, auth = request(env, "GET", "/_security/_authenticate")
        if status == 200 and isinstance(auth, dict):
            ok("authenticates as " + str(auth.get("username")))
        else:
            fail("rejected (HTTP %s)" % status)

        status, privs = request(
            env, "POST", "/_security/user/_has_privileges",
            {"index": [{"names": [ds], "privileges": ["create_doc", "auto_configure"]}
                       for ds in (METRICS_DS, EXEMPLARS_DS, TRACES_DS)]},
        )
        granted = privs.get("index", {}) if isinstance(privs, dict) else {}
        for ds in (METRICS_DS, EXEMPLARS_DS, TRACES_DS):
            perms = granted.get(ds, {})
            missing = sorted(k for k, v in perms.items() if not v)
            if not perms:
                warn(ds + ": could not determine privileges")
            elif not missing:
                ok(ds + ": create_doc + auto_configure")
            elif ds == EXEMPLARS_DS:
                fail(ds + ": " + ", ".join(missing) + " NOT granted. Metrics will ingest "
                     "fine while every exemplar is rejected with FORBIDDEN.\n"
                     "        Fix: ./scripts/exemplars.py api-key")
            else:
                fail(ds + ": " + ", ".join(missing) + " NOT granted")

    if not quiet:
        print(bold("Documents so far"))
        for ds in (METRICS_DS, EXEMPLARS_DS, TRACES_DS):
            count = doc_count(env, ds)
            print("  %-34s %s" % (ds, count if count is not None else "does not exist yet"))

    print()
    if failures[0]:
        print(red("%d check(s) failed. Exemplars will not appear until they are fixed."
                  % failures[0]))
        return 1
    print(green("All checks passed."))
    return 0


def commit_has_pr_marker(build_hash):
    """True/False if GitHub answered, None if unreachable."""
    url = ("https://api.github.com/repos/elastic/elasticsearch/contents/"
           + PR_MARKER_PATH + "?ref=" + build_hash)
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except Exception:
        return None


def doc_count(env, ds):
    status, body = request(env, "GET", "/" + ds + "/_count")
    return body.get("count") if status == 200 and isinstance(body, dict) else None


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

def cmd_verify(env):
    print(bold("Documents"))
    counts = {}
    for ds in (METRICS_DS, EXEMPLARS_DS, TRACES_DS):
        counts[ds] = doc_count(env, ds)
        print("  %-34s %s" % (ds, counts[ds] if counts[ds] is not None else "does not exist"))
    print()

    if not counts[EXEMPLARS_DS]:
        print(red("No exemplar documents in " + EXEMPLARS_DS + "."))
        print("\nRunning the cluster checks to find out why:\n")
        cmd_check(env, quiet=True)
        print("If every check passed, the exemplars may never have left the SDK.")
        print("Compare against the wire:")
        print("  grep -c exemplars collector/out/otlp-metrics.json")
        print("And look for rejections the backend reported:")
        print('  docker compose logs otel-collector | grep "Partial success"')
        return 1

    print(green(str(counts[EXEMPLARS_DS]) + " exemplar document(s) ingested."))
    print()

    status, found = request(
        env, "GET", "/" + EXEMPLARS_DS + "/_search",
        {"size": 1, "sort": [{"@timestamp": "desc"}], "query": {"exists": {"field": "trace_id"}}},
    )
    hits = found.get("hits", {}).get("hits", []) if isinstance(found, dict) else []
    if not hits:
        print(yellow("Exemplars exist, but none carry a trace_id -- check that spans are sampled."))
        return 1

    src = hits[0]["_source"]
    metric_name, value = next(iter(src.get("metrics", {}).items()), ("(none)", None))
    trace_id = src.get("trace_id")

    print(bold("A real exemplar document"))
    print("  index              " + hits[0]["_index"])
    print("  @timestamp         " + str(src.get("@timestamp")))
    print("  metric             %s = %s" % (metric_name, value))
    print("  trace_id           " + str(trace_id))
    print("  span_id            " + str(src.get("span_id")))
    print("  service            "
          + str(src.get("resource", {}).get("attributes", {}).get("service.name")))
    print("  scope              " + str(src.get("scope", {}).get("name")))
    print("  _metric_names_hash " + str(src.get("_metric_names_hash")))
    for label, key in (("dimensions", "attributes"),
                       ("filtered_attributes", "filtered_attributes")):
        values = src.get(key) or {}
        if values:
            print("  " + label + ":")
            for k, v in sorted(values.items()):
                print("    %s = %s" % (k, v))
    print()

    ok_parent = verify_parent(env, src, metric_name)
    ok_trace = verify_trace(env, trace_id)

    print()
    if ok_parent and ok_trace:
        print(green("Chain verified: aggregated metric -> exemplar -> trace -> spans "
                    "across both services."))
        return 0
    return 1


def verify_parent(env, exemplar_src, metric_name):
    """Match the exemplar to its parent metric document.

    Joins on dimensions rather than _metric_names_hash. That hash is a TSDB
    dimension, not a join key: an exemplar document holds exactly one exemplar
    and hashes one metric name, while a metric document may group several
    metrics and hash all of their names. The two are equal only when the parent
    carries a single metric. Dimensions are guaranteed to line up, because the
    exemplar data stream reuses the metrics mappings for exactly this reason.
    """
    print(bold("Parent metric series (joined on dimensions)"))
    must = [{"term": {"attributes." + k: v}}
            for k, v in (exemplar_src.get("attributes") or {}).items()]
    service = (exemplar_src.get("resource", {}).get("attributes", {}) or {}).get("service.name")
    if service:
        must.append({"term": {"resource.attributes.service.name": service}})
    if metric_name != "(none)":
        must.append({"exists": {"field": "metrics." + metric_name}})

    status, body = request(env, "GET", "/" + METRICS_DS + "/_search",
                           {"size": 1, "query": {"bool": {"must": must}}})
    hits = body.get("hits", {}).get("hits", []) if isinstance(body, dict) else []
    if not hits:
        print(yellow("  no metric document matched those dimensions yet -- metric and"))
        print(yellow("  exemplar documents come from the same export; retry shortly."))
        return False

    parent = hits[0]["_source"]
    print(green("  matched %d metric document(s)" % body["hits"]["total"]["value"]))
    print("  parent metrics     " + (", ".join(sorted(parent.get("metrics", {}))) or "(none)"))
    print("  parent hash        " + str(parent.get("_metric_names_hash")))
    if parent.get("_metric_names_hash") == exemplar_src.get("_metric_names_hash"):
        print(green("  hashes match (this parent carries a single metric)"))
    else:
        print("  hashes differ, as expected when the parent groups several metrics")
    return True


def verify_trace(env, trace_id):
    print(bold("Trace " + str(trace_id)))
    status, body = request(env, "GET", "/" + TRACES_DS + "/_search",
                           {"size": 20, "query": {"term": {"trace_id": trace_id}},
                            "sort": [{"@timestamp": "asc"}]})
    hits = body.get("hits", {}).get("hits", []) if isinstance(body, dict) else []
    if not hits:
        print(yellow("  no spans found. OTLP trace ingest is in preview from 9.5, so on an"))
        print(yellow("  older build the exemplar's trace_id is right but the spans are absent."))
        return False

    print(green("  matched %d span(s)" % body["hits"]["total"]["value"]))
    services = set()
    for hit in hits:
        s = hit["_source"]
        service = s.get("resource", {}).get("attributes", {}).get("service.name", "?")
        services.add(service)
        print("    %-20s %-12s %s" % (service, s.get("kind", ""), s.get("name", "")))
    if len(services) > 1:
        print(green("  trace spans %d services: %s" % (len(services), ", ".join(sorted(services)))))
    return True


# --------------------------------------------------------------------------- #

COMMANDS = {"api-key": cmd_api_key, "verify": cmd_verify, "check": cmd_check}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("commands: " + ", ".join(COMMANDS))
        return 2
    return COMMANDS[sys.argv[1]](load_env())


if __name__ == "__main__":
    sys.exit(main())
