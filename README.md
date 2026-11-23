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

You need Git, Docker with Compose, and Python 3.

## 1. Elasticsearch and Kibana

Run Elasticsearch and Kibana locally from your `kibana` checkout, using a
snapshot build of Elasticsearch:

```bash
pnpm kbn es snapshot
```

```bash
pnpm start
```

Elasticsearch listens on `http://localhost:9200` and Kibana on
`http://localhost:5601`, with the credentials `elastic` / `changeme`.

Exemplar ingestion sits behind the `metric_exemplars` feature flag, which
snapshot builds turn on by default. On a release build, start Elasticsearch
with:

```bash
ES_JAVA_OPTS="-Des.metric_exemplars_feature_flag_enabled=true" pnpm kbn es snapshot
```

## 2. Clone this repo

```bash
git clone git@github.com:kpatticha/otel-metrics-exemplars.git
cd otel-metrics-exemplars
```

## 3. Configure `.env`

```bash
cp .env.example .env
```

Set the Elasticsearch credentials in `.env`:

```
ES_USERNAME=elastic
ES_PASSWORD=changeme
```

Create an API key for the collector. The script writes `ELASTIC_API_KEY` into
`.env`:

```bash
./scripts/exemplars.py api-key
```

Then check the cluster and the key:

```bash
./scripts/exemplars.py check
```

The collector reaches Elasticsearch at `http://host.docker.internal:9200`. To
use another address, add `ELASTIC_URL=...` to `.env`.

## 4. Start the python services

```bash
docker compose up --build
```

That brings up five containers: the two Python services, an OTel Collector, a
k6 load generator, and Grafana. The services export OTLP to the collector, and
the collector forwards to `http://localhost:9200/_otlp`.

Wait ~20 seconds for the first metric export.

## 5. Check it worked

```bash
./scripts/exemplars.py verify
```

**Kibana**, `http://localhost:5601` — Discover on `exemplars-generic.otel-default`,
or ES|QL:

```esql
FROM exemplars-generic.otel-default | SORT @timestamp DESC | LIMIT 20
```

## Troubleshooting

- Did the services send exemplars? `grep -c exemplars collector/out/otlp-metrics.json`
- Did Elasticsearch reject any? `docker compose logs otel-collector | grep "Partial success"`
- Changed the API key? Run `docker compose up -d` again so the collector picks it up.
