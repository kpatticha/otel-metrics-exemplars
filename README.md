# otel-metrics-exemplars

Two Python services that do real work, instrumented with OpenTelemetry, sending
metrics **with exemplars** through an OTel Collector into Elasticsearch's native
OTLP endpoint.

Built to exercise [elastic/elasticsearch#158350](https://github.com/elastic/elasticsearch/pull/158350)
— _Add OTLP metric exemplar ingestion_
than a hand-crafted OTLP payload.

```
k6 ──▶ checkout-service ──HTTP──▶ inventory-service
            │                          │
            └────── OTLP ───┬── OTLP ──┘
                            ▼
                     otel-collector
                            │  (the only component holding credentials)
                            ▼
              $ELASTIC_URL/_otlp/v1/{metrics,traces}
                            │
              metrics-generic.otel-default
              exemplars-generic.otel-default   ← The exemplars-index
              traces-generic.otel-default
```

# Running this locally

Five steps: Elasticsearch from the PR branch, Kibana pointed at it, an API key,
then Docker.

You need Docker with Compose, a JDK for the Elasticsearch build, and Node/Yarn
if you want Kibana.

---

## 1. SKIP THIS STEP: The PR has been merged

Elasticsearch, built from the PR

Exemplar ingestion is not merged yet, so it has to come from
[PR #158350](https://github.com/elastic/elasticsearch/pull/158350).

In your `elasticsearch` checkout:

```bash
gh pr checkout 158350 --repo elastic/elasticsearch
```

```bash
./gradlew run
```

Leave it running. It listens on `http://localhost:9200` with these credentials:

```
ES_USERNAME=elastic-admin
ES_PASSWORD=elastic-password
```

Two things are already handled for you by `./gradlew run`, and are worth knowing
so you don't go looking for them:

- **The feature flag is on.** Ingestion sits behind the `metric_exemplars`
  feature flag, and Elasticsearch enables every flag automatically on snapshot
  builds — which is what `gradlew run` produces. No system property needed.
- **The index templates install themselves**, including `exemplars-otel@template`.

## 2. Kibana, pointed at that cluster

In your `kibana` checkout, put this in `config/kibana.dev.yml`:

if you run elasticsearch locally from es snapshot command
```yaml
elasticsearch.hosts: ["http://localhost:9200"]
elasticsearch.username: kibana_system
elasticsearch.password: changeme
```

if you run elasticsearch locally with `./gradlew run` command


```yaml
elasticsearch.hosts: ["http://localhost:9200"]
elasticsearch.username: elastic-admin
elasticsearch.password: elastic-password
```

```bash
yarn start
```

Kibana comes up on `http://localhost:5601`.

If you already had a `kibana.dev.yml` pointing at a different cluster, the
password is the thing that usually catches people out — it must be the
`elastic-password` above, not whatever your previous cluster used.

## 3. An API key for the collector

```bash
cp .env.example .env
```

Create an API key from Kibana (Not the onboarding page but management page)

That writes `ELASTIC_API_KEY` into `.env`.

## 4. Start the python services

```bash
docker compose up --build
```

That brings up five containers: the two Python services, an OTel Collector, a
k6 load generator, and Grafana. The services export OTLP to the collector, and
the collector forwards to `http://localhost:9200/_otlp`.

Wait ~20 seconds for the first metric export.

## 5. Check it worked

**Kibana**, `http://localhost:5601` — Discover on `exemplars-generic.otel-default`,
or ES|QL:
