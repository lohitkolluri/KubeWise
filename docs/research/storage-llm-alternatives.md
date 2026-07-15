# KubeWise Storage & LLM Integration: Alternatives & Improvements

> Comprehensive research across 7 domains. Each section: Current Approach → Alternative Found → Key Difference → Recommendation → Effort.

---

## 1. bbolt vs Alternatives for K8s Operator Pattern

### Current Approach
`go.etcd.io/bbolt` — single-writer embedded KV with exclusive file lock. Each KubeWise agent pod gets its own bbolt DB file. **No HA** (the file lock prevents multi-pod access). Time-series ring buffer and anomaly CRUD with hand-rolled indexes.

### Alternatives

#### 1a. SQLite + WAL mode + `modernc.org/sqlite` (pure Go)
- **URL**: https://modernc.org/sqlite, https://gitlab.com/cznic/sqlite
- **Why better**: WAL mode allows **concurrent readers** while a writer is active. Pure Go (no CGO). Single-file, no server process. Rich SQL querying replaces hand-rolled bbolt index code (time indexes, open-anomaly lookups, owner queries all become SQL `WHERE` clauses). 100K+ reads/sec, thousands of writes/sec on a single node.
- **For HA**: Pair with **Litestream** (S3 continuous replication) or **rqlite**/**Hiqlite**/**dqlite** (Raft consensus across StatefulSet pods) for distributed SQLite. Hiqlite embeds Raft+SQLite in a single Go binary with transparent leader forwarding.
- **Risk**: SQLite single-writer means contention at high write throughput, but KubeWise's workload (30s scrape intervals, ~few writes/sec) is far below the threshold.
- **Recommendation**: **Must Have** for the storage layer evolution. Replace bbolt → pure-Go SQLite (WAL mode) as the single-pod backend. Immediate benefit: simpler queries, no hand-rolled indexes. Path to HA via Hiqlite when needed.

#### 1b. BadgerDB
- **URL**: https://github.com/dgraph-io/badger
- **Why better**: LSM-tree design, **10-375x faster writes** than bbolt in benchmarks. Concurrent readers, no file lock. Built-in TTL support, compression (ZSTD/Snappy), ACID transactions with SSI. Pure Go.
- **Key difference**: Badger is KV-only (like bbolt) so you still hand-roll indexes. But it's dramatically faster for writes and supports concurrent access natively. However, the KV-only API means no SQL — same indexing effort as bbolt.
- **Verdict**: Better than bbolt for write throughput, but doesn't solve the query/indexing problem. Not a big enough leap over bbolt to justify migration.
- **Recommendation**: **Skip** — better to go to SQLite which solves both the concurrency AND the querying problem.

#### 1c. CockroachDB Pebble
- **URL**: https://github.com/cockroachdb/pebble
- **Why better**: LevelDB/RocksDB-inspired LSM store, pure Go. Used by CockroachDB in production at massive scale. Write throughput significantly better than bbolt (2x in benchmarks). Concurrent readers.
- **Key difference**: KV-only, no SQL. Optimized for CockroachDB's use case, not general purpose. Minimalist API — no built-in indexes, no TTLs.
- **Verdict**: Production-grade but wrong abstraction level for KubeWise.
- **Recommendation**: **Skip** — KV-only buys nothing over bbolt for the query patterns.

#### 1d. etcd
- **URL**: https://github.com/etcd-io/etcd
- **Why better**: The Kubernetes standard for distributed coordination. Native HA via Raft. Watches, leases, transactions. Strongly consistent.
- **Key difference**: Requires a separate etcd cluster to run. Network-based (not embedded). Overkill for an operator's local state.
- **Verdict**: If KubeWise already ran etcd in the cluster, using it would be sensible. But adding etcd purely for KubeWise's storage is heavy.
- **Recommendation**: **Skip** unless KubeWise evolves to need distributed coordination (leader election, leases).

#### 1e. What other AIOps tools use
- **Robusta**: Supabase (PostgreSQL) for UI persistence, in-memory cache for runtime state. **Not embedded** — network DB.
- **K8sGPT**: No persistent storage — stateless, re-analyzes on each run.
- **VictoriaMetrics vmanomaly**: VictoriaMetrics TSDB + config files. No embedded KV store.
- **Pattern**: Most K8s-native AIOps tools use either (a) external PostgreSQL, (b) Prometheus/VictoriaMetrics as their only persistent store, or (c) are stateless. Embedded KV databases are uncommon in this space.

**Verdict**: SQLite (pure Go WAL mode) is the clear upgrade path. Keeps embedded simplicity, adds concurrent reads, eliminates hand-rolled indexes with SQL, and has a path to HA via Hiqlite/dqlite.

---

## 2. Time-Series Storage for Anomaly Data

### Current Approach
bbolt ring buffer: 10,080 samples per metric (7 days at 1-min intervals), stored as (timestamp→value) pairs in per-metric sub-buckets. Manual `TrimOlderThan` and ring-buffer eviction. Each sample is 16 bytes (8 timestamp + 8 value). No downsampling, no aggregation.

### Alternatives

#### 2a. VictoriaMetrics embedded storage library (`lib/storage`)
- **URL**: https://pkg.go.dev/github.com/VictoriaMetrics/VictoriaMetrics/lib/storage
- **Why better**: **Battle-tested TSDB** used in production. Handles millions of active time series. Built-in retention, downsampling, snapshots. The `lib/storage` package can be **embedded directly** in a Go application. PromQL/MetricsQL queryable.
- **Key difference**: Instead of a ring buffer, VM uses an LSM-tree with separate index (inverted index for label→TSID mapping) and data blocks. Memory-mapped, compression (gorilla-like for timestamps, delta-of-delta), and efficient intersection queries for labels.
- **Complexity**: VictoriaMetrics' storage is ~50KLOC+ and has its own file format. Embedding it is possible but couples KubeWise to VM's internal format and upgrade cadence.
- **Recommendation**: **Nice to Have** — evaluate embedding `lib/storage` for the metrics path. Lower effort approach: use VictoriaMetrics as an **external dependency** (run a VM single-node as a sidecar or use the cluster's existing Prometheus/VM).

#### 2b. Prometheus Remote Write Protocol + vmagent as sidecar
- **URL**: https://docs.victoriametrics.com/vmagent.html
- **Why better**: Instead of storing metrics in bbolt at all, use Prometheus remote write protocol to forward scraped metrics to an existing VictoriaMetrics or Prometheus server. `vmagent` handles buffering, retries, and back-pressure. Available as a **sidecar container** in the same pod.
- **Key difference**: Offloads all TSDB concerns. KubeWise becomes a consumer of metrics rather than a store of metrics. The cluster's existing observability stack handles retention, downsampling, querying.
- **Implementation**: KubeWise can write to vmagent's HTTP endpoint OR vmagent can scrape KubeWise's own metrics endpoint. If the cluster already has Prometheus/VM, this is zero-new-infrastructure.
- **Recommendation**: **Must Have** — decouple metric storage from the operator. Write scraped metrics via remote write to the cluster's existing Prometheus/VM. KubeWise queries them back via PromQL. This eliminates the ring buffer entirely.

#### 2c. VictoriaMetrics Anomaly Detection (`vmanomaly`)
- **URL**: https://docs.victoriametrics.com/anomaly-detection/
- **Why better**: ML-based anomaly detection that replaces KubeWise's custom scoring logic. Produces an `anomaly_score` metric. Handles seasonal decomposition, trend detection, and model training automatically.
- **Key difference**: Offloads the entire detection pipeline. Runs as a separate service (sidecar or cluster service), queries metrics from VictoriaMetrics, computes anomaly scores, and writes them back. Supports multiple model types (Facebook Prophet, custom models).
- **Caveat**: **Enterprise feature** — requires a license.
- **Recommendation**: **Skip** (cost/overhead) — KubeWise's simple scoring works for the use case. Monitor `vmanomaly` as the cluster matures.

---

## 3. Index Patterns in bbolt

### Current Approach
Three manual indexes:
- Time index: `bucketAnomalyIndex` with inverted timestamp→ID keys
- Open anomaly index: `bucketAnomalyOpen` with entity+signal→ID keys
- Status index: `bucketAnomalyStatus` (mentioned but not shown in code)
- Rebuild logic on startup (`ensureAnomalyIndexes`)

### Alternatives

#### 3a. Hand-rolled indexes → SQL
- **Why better**: Replace all of the above with SQL `WHERE` clauses. `ListAnomaliesByOwner` — currently a full `ForEach` scan — becomes `SELECT * FROM anomalies WHERE owner_kind=? AND owner_name=? ORDER BY detected_at DESC LIMIT ?`. The `FindOpenAnomaly` — currently two index lookups plus a legacy fallback full scan — becomes `SELECT * FROM anomalies WHERE entity=? AND signal=? AND status IN ('detected','active','correlated') ORDER BY detected_at DESC LIMIT 1`. Zero index code to maintain.
- **Recommendation**: **Must Have** (bundled with SQLite migration, item 1a).

#### 3b. Bolt-adjacent ORMs: Storm, Hold
- **URL**: https://github.com/asdine/storm, https://github.com/timshannon/bolthold
- **Why better**: `Storm` is an ORM for bbolt with auto-indexing, query builders, and struct storage. Define a `struct AnomalyRecord` with `storm` tags and it auto-creates indexes, handles CRUD, and supports simple queries without raw bucket operations. `Hold` adds a queryable index layer on top of bbolt.
- **Key difference**: Keeps bbolt but eliminates the manual index maintenance. You get `db.One("Entity", entity, &record)`, `db.Find("Status", status, &records)`, `db.Count(&records)`, etc. Startup index rebuild goes away.
- **Verdict**: An immediate improvement if sticking with bbolt. But doesn't solve the HA/multi-writer problem.
- **Recommendation**: **Nice to Have** as a stepping stone before SQLite migration, or **Skip** if going directly to SQLite.

#### 3c. GoKv abstraction layer
- **URL**: https://github.com/philippgille/gokv
- **Why better**: Unified KV-store interface that supports bbolt, BadgerDB, Redis, Consul, PostgreSQL, etc. Swap storage backends by changing one line.
- **Key difference**: Abstraction layer, not an index solution. Useful if you want backend flexibility but doesn't address the querying problem.
- **Recommendation**: **Skip** — KubeWise is committed to a single backend; abstraction overhead isn't justified.

---

## 4. Provider Abstraction Patterns

### Current Approach
Custom `Provider` interface with 5 methods: `Name()`, `HasAPIKey()`, `ValidateKey()`, `SetModel()`, `StructuredOutput()`. Three implementations: OpenRouter, Ollama, OpenAI. Wrapped in `Client` facade + `LLMRouter` for fallback chains.

### Alternatives

#### 4a. `promptrails/langrails`
- **URL**: https://github.com/promptrails/langrails
- **Why better**: Unified Go LLM interface supporting **25 providers** (including OpenRouter, Ollama, OpenAI, Anthropic, Gemini, DeepSeek, Groq, etc.). Built-in structured output, streaming, tool calling, prompt caching, retry/fallback decorators, cost tracking, and A2A protocol.
- **Key difference**: Instead of maintaining a custom provider interface with 3 implementations, Langrails provides a production-grade unified API with 25x the provider coverage. The `WithJSONSchema()` option handles structured output across all providers. Built-in `Retry` and `Fallback` decorators replace the custom router.
- **Adoption impact**: Would replace `internal/agent/llm/provider.go`, `client.go`, `router.go` (simplified), and the three provider backends with a single Langrails client. The LLMRouter's fallback chain maps to Langrails' `Fallback` decorator.
- **Maturity**: Active, well-documented, good Go idioms (context.Context, functional options). MIT license.
- **Recommendation**: **Must Have** — eliminates ~600 lines of custom integration code and adds 25 providers for free.

#### 4b. `code-koan/llm-sdk-go`
- **URL**: https://github.com/code-koan/llm-sdk-go
- **Why better**: Unified LLM interface with 10+ providers. Structured output, streaming, tool calling, reasoning, embeddings. Uses official provider SDKs as backends.
- **Key difference**: Similar value to Langrails but smaller provider set (10 vs 25). Also well-architected with proper Go patterns.
- **Recommendation**: **Nice to Have** — Langrails has broader coverage but either library is a significant upgrade.

#### 4c. `tmc/langchaingo`
- **URL**: https://github.com/tmc/langchaingo
- **Why better**: Full langchain port: chains, agents, vector stores, document loaders, LLM abstraction. Broad provider support.
- **Key difference**: Much heavier — includes RAG, chains, agents, memory, etc. Overkill if KubeWise only needs LLM chat completion. The LLM model abstraction is good but comes with the rest of the framework.
- **Recommendation**: **Skip** unless KubeWise adds RAG or complex chain workflows.

#### 4d. Google Genkit for Go
- **URL**: https://github.com/firebase/genkit/tree/main/go
- **Why better**: Google's framework for building AI applications. Structured output, streaming, tool calling, observability, evaluation. First-class Go support.
- **Key difference**: Tied to Google Cloud ecosystem. Emerging — less community adoption than alternatives.
- **Recommendation**: **Skip** — premature for KubeWise's needs; Google Cloud dependency limits on-prem/air-gapped deployments.

---

## 5. Prompt Engineering + Structured Output

### Current Approach
Hand-crafted system prompt (~130 lines) + JSON Schema in `schemas.go` (~210 lines) for `response_format`. Standard `json.Unmarshal` for parsing. No retry loop on parse failure — failure means the plan step fails.

### Alternatives

#### 5a. Instructor-go
- **URL**: https://github.com/instructor-ai/instructor-go (210 stars, active)
- **Why better**: **Structured output specialist library**. Define a Go struct → Instructor generates the JSON schema, sends it to the LLM, validates the response, and **auto-retries on validation failure** with the error message fed back to the model. Supports OpenAI, Anthropic, Gemini, Cohere. Streaming, nested structures, union types.
- **Key difference**: The auto-retry loop is the killer feature. Currently, if the LLM returns invalid JSON or misses a required field, KubeWise fails the entire remediation. With Instructor, it retries 3x with the specific error. The retry-with-error pattern dramatically improves reliability (reported 95%+ first-attempt parse success, with retries covering most of the remaining 5%).
- **Adoption impact**: Replace manual schema generation + unmarshaling with `instructor.FromOpenAI(client)` and `client.CreateChatCompletion(ctx, req, &remediationPlan)`. The `remediationPlan` Go struct defines the schema automatically.
- **Maturity**: Active development (latest release May 2026), 210 stars, 7 contributors. MIT license.
- **Recommendation**: **Must Have** — minimal adoption cost (adds retry loop and typed parsing) for significant reliability improvement.

#### 5b. BAML
- **URL**: https://github.com/BoundaryML/baml (8K stars, Rust compiler + Go SDK)
- **Why better**: **Prompt compiler**. Define prompts and output schemas in a `.baml` file, get generated Go client code. The compiler handles schema alignment across model providers, retry logic, streaming, and type-safe function calls. Runs its own schema-aligned parsing (SAP) algorithm that works even when models don't support native tool calling.
- **Key difference**: Moves prompt engineering into a **compiled DSL**. Version-controlled `.baml` files → generated `baml_client` package. Guarantees output types at compile time. Multi-provider with zero code changes.
- **Trade-off**: Adds a build step (`baml-cli generate`). The generated SDK communicates with a Rust runtime (CGO-ish via FFI). More complex than Instructor-go.
- **Adoption impact**: Would replace `prompts.go`, `schemas.go`, and the structured output path in the provider interface with generated Go code from `.baml` definitions. Steeper learning curve.
- **Recommendation**: **Nice to Have** — ideal if KubeWise adds more LLM functions (classification, summarization, etc.). For the single remediation_plan schema, Instructor-go is lighter.

#### 5c. Self-healing structured output pattern (DIY)
- **Reference**: https://lawzava.com/blog/2024-04-29-structured-output-patterns/
- **Why better**: A ~300-line reusable Go pattern: generate JSON Schema from struct via reflection → build rigid prompt → clean response → validate with `go-playground/validator` → retry with repair prompt. No external dependency.
- **Key difference**: Same concept as Instructor-go but DIY. More control, less magic, less maintenance.
- **Recommendation**: **Skip** — Instructor-go does this better with multi-provider support and less code to maintain.

---

## 6. Semantic Cache for LLM

### Current Approach
SHA256 hash of `entity + namespace + metricName + pattern + scoreBucket(score)`. Exact-match only. In-memory map with TTL (1h default) and LRU eviction. Optional persistence backend interface.

### Alternatives

#### 6a. Embedding-based semantic cache (Upstash / GPTCache / semcache)
- **Why better**: Catch semantically similar anomalies, not just identical ones. If the same pod OOMs twice with slightly different metric names, hash-based cache misses but semantic cache hits. LLM calls saved every time the same root cause manifests differently.
- **How it works**: On cache miss → embed the query → store embedding + response in a vector DB. On request → embed the query → cosine similarity search → if above threshold (e.g., 0.92), return cached response. This catches "restart count high for pod X" and "pod X is in CrashLoopBackOff" as the same incident.
- **Key difference**: Hash-based caching = 0% semantic overlap tolerance. Embedding-based = configurable similarity threshold. Real-world workloads see **30-60% cache hit rates** vs hash-based which may see <5% for varied-but-similar anomalies.

#### 6b. GPTCache (Zilliz)
- **URL**: https://github.com/zilliztech/GPTCache
- **Why better**: Full-control semantic caching library. Swappable components: embedder (any HuggingFace model), vector store (FAISS, Milvus, SQLite), eviction policy, similarity evaluator. MIT license.
- **Key difference**: Self-hosted, maximum flexibility. Can tune embedding model and similarity threshold per use case. But it's Python — would need to run as a sidecar or use its HTTP API.
- **Recommendation**: **Nice to Have** — pair with a small embedding model sidecar. GPTCache handles the caching logic; KubeWise calls it via HTTP.

#### 6c. `sensoris/semcache` (Go-native)
- **URL**: https://github.com/sensoris/semcache
- **Why better**: Go-native semantic caching layer. In-memory vector DB, LRU eviction, HTTP proxy mode, Prometheus metrics. Drop-in HTTP proxy that caches semantically similar LLM responses. Supports OpenAI, Anthropic, Gemini.
- **Key difference**: Could run as a **sidecar container** or be embedded as a library. The HTTP proxy mode is zero-code integration — point KubeWise's LLM calls through semcache and it caches automatically.
- **Maturity**: Newer project (73 stars). But architecture is clean and Go-native means no Python dependency.
- **Recommendation**: **Nice to Have** — simpler than GPTCache for Go-native stacks.

#### 6d. vCache (verified semantic cache)
- **URL**: https://github.com/vcache-project/vCache
- **Why better**: **Guaranteed error rate** via online-learned per-embedding thresholds instead of a global static threshold. Up to **12.5x higher hit rates** and **26x lower error rates** than GPTCache. Has academic paper backing (arXiv).
- **Key difference**: Solves the threshold tuning problem. Instead of guessing `threshold=0.92`, vCache learns the optimal threshold per cached entry online. Guarantees error rate stays below user-defined bound.
- **Limitation**: Python only — would run as an external service.
- **Recommendation**: **Watch** — cutting-edge but Python-only. If/when Go-native, adopt immediately.

#### 6e. Recommendation for KubeWise
Start by **measuring**: add cache hit/miss metrics to the existing hash cache. If hit rate >20%, hash is sufficient (anomalies are repetitive enough). If hit rate <10%, semantic cache is worth pursuing.

Short-term: Switch from SHA256 of raw fields to **normalized fingerprint** (lowercase, trim, sort labels). This catches trivial variations (whitespace, label ordering) at zero cost.

If semantic needed: Use `sensoris/semcache` as a sidecar proxy. Small embedding model (MiniLM-L6-v2, 384-dim), 0.92 cosine threshold. Monitor cache hit rate.

---

## 7. Circuit Breaker + Retry Patterns

### Current Approach
Custom 3-state circuit breaker (closed/open/half-open) with `atomic.Int32` state machine, consecutive failure threshold, cooldown-based recovery. ~120 lines. Single-failure-mode: counts consecutive failures, opens after threshold, half-opens after cooldown.

### Alternatives

#### 7a. `sony/gobreaker` v2
- **URL**: https://github.com/sony/gobreaker (2.8K stars, active)
- **Why better**: **The Go circuit breaker standard**. Battle-tested in production at Sony and others. Configurable: sliding window counting, failure ratio triggers, per-state callbacks, half-open request limits, custom success/failure/exclusion predicates. Generics support in v2. Actively maintained (latest commit days ago).
- **Key difference over current CB**:
  - **Sliding window** vs simple consecutive counter — prevents a burst of 5 failures hours ago from keeping the circuit open
  - **Failure ratio** vs absolute count — trips at 50% failure rate over 100 requests, not just "3 failures in a row"
  - **State change callbacks** for metrics/alerting — `OnStateChange` hooks for Prometheus counters
  - **IsSuccessful** and **IsExcluded** predicates — e.g., don't count 401 as a circuit-trip failure, don't count context cancellations at all
- **Adoption impact**: Replace `circuitbreaker.go` with gobreaker. The circuit breaker wraps the LLM client's `StructuredOutput` call. `cb.Execute()` replaces the manual `Allow()`/`Success()`/`Failure()` dance.
- **Effort**: ~2 hours. Drop-in replacement for the same 3-state pattern with more features.

#### 7b. `resilience4go`
- **URL**: https://github.com/resilience4j/resilience4go (1.2K stars)
- **Why better**: Full resilience suite: circuit breaker, rate limiter, retry, bulkhead, time limiter, cache. Decorator-based composition — stack them in any order. Go port of Java's Resilience4j (the successor to Hystrix).
- **Key difference**: Not just circuit breaker — also provides **rate limiting** (prevent LLM API bursts), **retry with backoff** (the current router doesn't retry on 429s), **bulkhead** (limit concurrent LLM calls). These are complementary patterns KubeWise currently lacks.
- **Adoption impact**: Could replace both the circuit breaker AND add rate limiting. Compose: `retry(3, exponentialBackoff(1s, 30s))` → `circuitBreaker()` → `bulkhead(5)` around the LLM call.
- **Recommendation**: **Nice to Have** — evaluate for the full resilience suite, not just circuit breaker. Especially the rate limiter (many LLM providers have per-minute token limits).

#### 7c. `cenkalti/backoff`
- **URL**: https://github.com/cenkalti/backoff (4.3K stars)
- **Why better**: Exponential backoff with jitter. Not a circuit breaker but complements one. Configurable: initial interval, max interval, max elapsed time, max retries. The jitter prevents thundering herd on recovery.
- **Key difference**: KubeWise currently has no exponential backoff on retries. The LLM router tries fallback models synchronously but doesn't wait. Adding backoff between fallback attempts would prevent cascading rate limits when the primary model is under load.
- **Recommendation**: **Must Have** (with gobreaker) — add jittered exponential backoff to LLM retries. Simple to integrate: `backoff.Retry(op, backoff.WithContext(backoff.NewExponentialBackOff(), ctx))`.

#### 7d. Comparison matrix

| Feature | Current CB | gobreaker | resilience4go |
|---------|-----------|-----------|---------------|
| 3-state machine | ✅ | ✅ | ✅ |
| Consecutive failure count | ✅ | ✅ | ✅ |
| Sliding window / ratio | ❌ | ✅ | ✅ |
| Half-open request limit | ❌ (1 probe) | ✅ configurable | ✅ configurable |
| State change callbacks | ❌ | ✅ | ✅ |
| Excluded errors (context cancel, etc.) | ❌ | ✅ | ✅ |
| Rate limiter | ❌ | ❌ | ✅ |
| Bulkhead | ❌ | ❌ | ✅ |
| Retry with backoff | ❌ | ❌ | ✅ |
| Generics | ❌ | ✅ (v2) | ❌ |

---

## Summary Priority Matrix

| # | Recommendation | Category | Effort | Impact | Risk |
|---|--------------|----------|--------|--------|------|
| **P0** | SQLite (pure Go WAL) replace bbolt | Storage | Medium | High — concurrent readers, SQL queries, HA path | Low |
| **P0** | Prometheus remote write for metrics | Storage | Medium | High — eliminate ring buffer, leverage existing infra | Low |
| **P0** | `sony/gobreaker` + `cenkalti/backoff` | LLM (CB) | Low | Medium — sliding window, jittered retry, callbacks | Low |
| **P0** | `promptrails/langrails` for provider abstraction | LLM (providers) | Medium | High — 25 providers, eliminate 600 LOC, built-in fallback | Low |
| **P0** | Instructor-go for structured output | LLM (prompt) | Low | Medium — auto-retry on validation failure, typed parsing | Low |
| **P1** | Embedding-based semantic cache (semcache sidecar) | LLM (cache) | Medium | Medium — catch semantically similar anomalies | Medium |
| **P1** | BAML for multi-function prompt management | LLM (prompt) | High | Medium — compile-time safety, multi-provider | Medium |
| **P2** | `resilience4go` rate limiter + bulkhead | LLM (CB) | Low | Low — add rate limiting for token budgets | Low |
| **P2** | VictoriaMetrics embedded lib/storage | Storage | High | Low — couples to VM internals | High |

## Implementation Roadmap

### Phase 1 (1-2 weeks) — High impact, low risk
1. Replace CB with `sony/gobreaker` + `cenkalti/backoff` on LLM calls
2. Add Instructor-go for structured output retry loop
3. Normalize semantic cache fingerprints (trim, lowercase, sort)

### Phase 2 (2-4 weeks) — Storage modernization
4. Migrate bbolt → pure Go SQLite (WAL mode)
5. Replace hand-rolled anomaly indexes with SQL queries
6. Implement Prometheus remote write for metrics offloading

### Phase 3 (2-4 weeks) — Provider consolidation
7. Adopt `promptrails/langrails` to replace custom provider interface
8. Simplify LLMRouter to use Langrails' built-in fallback chains

### Phase 4 (Future) — Advanced caching & resilience
9. Evaluate and deploy semantic cache (semcache sidecar)
10. Add rate limiter + bulkhead via resilience4go
11. Optionally adopt BAML for multi-function prompt management
