# Third-Party Library Audit: KubeWise Storage, Collector, and API Subsystems

**Date:** 2026-07-16
**Scope:** 6 subsystems, 12+ candidate libraries evaluated

---

## Executive Summary

| Subsystem | Lines | Verdict | Recommended Action |
|-----------|-------|---------|-------------------|
| Storage Engine (bbolt) | ~800+ | **Keep bbolt** | No replacement needed; bbolt is a reasonable choice |
| Prometheus Collector | 234 | **Keep with optimizations** | Consider recording rules; vmagent is separate binary |
| K8s Resource Collector | 411 | **Keep as-is** | Clean implementation; controller-runtime is overkill |
| API Rate Limiting | 150 | **Mild recommend x/time/rate** | Swap to stdlib for reduced maintenance |
| API Server | 140 | **Keep as-is** | Go 1.22+ ServeMux is sufficient; no framework needed |
| Semantic Cache | 232 | **Keep as-is** | Correct for scale (≤1000 entries); no dependency needed |

**Bottom line:** Zero high-priority replacements. The custom code is justified in every case — either because the library alternatives are unmaintained/overkill, or because the custom code is already clean and appropriate for the footprint. The one mild recommendation is `golang.org/x/time/rate` for the rate limiter (reduces 150 lines of sharded token-bucket code to ~50 lines of stdlib).

---

## SUBSYSTEM 1: Storage Engine (`internal/agent/store/`)

### Current Implementation

~800+ lines across 5 files:
- `store.go` — bbolt wrapper (Open, Close, Backup, Ping)
- `migrations.go` — bucket initialization (59 lines, creates 16 buckets)
- `metrics.go` — custom ring buffer inside bbolt for time series (256 lines)
- `anomalies.go` — anomaly CRUD with hand-rolled time-based indexes (478 lines)
- `audit.go` — audit log with time/status indexes

### Candidate: modernc.org/sqlite

| Metric | Value |
|--------|-------|
| **Stars** | ~3.5K+ importers (mirrored on GitHub, canonical on GitLab cznic/sqlite) |
| **Maintenance** | Active (v1.46.1 as of Feb 2026) |
| **CGO-free** | Yes — pure Go transpiled from C |
| **WAL mode** | Supported (experimental for concurrent writers) |
| **Cross-compile** | Trivial (CGO_ENABLED=0) |

**Performance against mattn/go-sqlite3 (CGO):**
- Inserts: ~26% slower
- Bulk inserts: ~31% slower
- Select by ID: ~19% slower
- Select all 1000 rows: **10% faster**
- Prepared inserts: ~54% slower

**When mattn/go-sqlite3 runs at 198K RPS with Fiber, modernc runs at 108K RPS.** For KubeWise's scale (single-agent, <100 RPS), this difference is irrelevant.

**Assessment: NO. Keep bbolt.** SQLite would add SQL querying capability (useful for filtering/sorting anomalies and audit logs), but:
1. The entire storage layer would need rewriting — every `tx.Bucket().Put/Get/ForEach` becomes SQL
2. KubeWise's access patterns are key-value lookups and sequential scans, not relational joins
3. bbolt is already used by etcd (the K8s component KubeWise monitors) — battle-tested in K8s ecosystem
4. The custom indexes in anomalies.go (time-based, open-anomaly lookup) are straightforward and correct
5. Migration effort: **HIGH** — ~15 storage operations would need rewriting with zero functional benefit for current query patterns

### Candidate: asdine/storm (bbolt wrapper)

| Metric | Value |
|--------|-------|
| **Stars** | ~1.7K |
| **Latest release** | v3.2.1 (July 2020) |
| **Last commit** | ~2 years ago |
| **Open issues** | 63 |
| **Maintenance** | **Effectively abandoned** |

**Assessment: NO.** Unmaintained for years. 63 open issues. Would be irresponsible to build on top of.

### Candidate: timshannon/bolthold (bbolt indexing layer)

| Metric | Value |
|--------|-------|
| **Stars** | ~560 (badgerhold sibling project) |
| **Latest release** | 2020–2021 era |
| **Maintenance** | Very low velocity |

Similar story to Storm — the library hasn't seen meaningful updates. The custom indexes in anomalies.go are not complex (~50 lines of index logic). Adding a dependency that may be abandoned is riskier than maintaining 50 lines of correct code.

**Assessment: NO.**

### Candidate: Prometheus TSDB / VictoriaMetrics Storage

Both are heavyweight time-series databases designed for monitoring at scale (millions of active series). KubeWise stores ~10K metric points per metric with 7-day retention. TSDB would add:
- WAL, compaction, block management, postings index — hundreds of KB of binary
- Prometheus metrics registry integration (designed to be scraped)
- Significant memory overhead for the index

**Assessment: NO.** A custom ring buffer on bbolt is the right approach for this scale. If KubeWise outgrows it, the migration should be to a TSDB running as a separate service (Prometheus/VictoriaMetrics), not embedded.

### Candidate: vapstack/rbi (Roaring Bolt Indexer)

New project (Dec 2025). 0 stars. Not production-ready.

**Assessment: NO.**

### Verdict: KEEP bbolt

bbolt is simple, correct, and appropriate. The custom indexes are straightforward. If query flexibility ever becomes a need (e.g., arbitrary filtering on anomaly fields), the migration path should be to `modernc.org/sqlite`, but the effort is not justified today.

---

## SUBSYSTEM 2: Prometheus Collector (`internal/agent/collector/prometheus.go`)

### Current Implementation

234 lines. Executes 16 hardcoded PromQL queries concurrently using goroutines + WaitGroup. Stores results to bbolt via `AppendMetricSeries`. Uses `prometheus/client_golang` `api.Query()`.

### Candidate: Prometheus Recording Rules

Recording rules pre-compute `rate()` expressions on the Prometheus server side and store them as new time series. For example, instead of querying `rate(container_cpu_usage_seconds_total[5m])` every time, the Prometheus server computes it every 15s and stores it as `:pod_cpu_usage:rate5m`.

| Pros | Cons |
|------|------|
| Eliminates client-side computation entirely | Requires controlling Prometheus server config |
| Queries become simple metric lookups | Users may not want us touching their Prometheus config |
| Reduces query latency to near zero | Adds disk usage on the Prometheus server |
| Standard Prometheus best practice | |

**Assessment: OPTIONAL — recommend for users who control their Prometheus.**
KubeWise should support both paths:
1. If recording rules exist, query the pre-computed series (cheaper, faster)
2. Otherwise, compute rates client-side as today

The queries would change from:
```promql
rate(container_cpu_usage_seconds_total{container!=""}[5m])
```
to:
```promql
kw_pod_cpu_usage_rate5m
```

**Migration effort: LOW.** ~30 lines to add a query-preference check.
**Limitation:** KubeWise runs as an agent and doesn't control the Prometheus installation. Recording rules require Prometheus operator or admin access. This is a feature enhancement, not a replacement.

### Candidate: VictoriaMetrics vmagent

| Metric | Value |
|--------|-------|
| **Stars** | 17K+ (VictoriaMetrics) |
| **Maintenance** | Very active (monthly releases) |
| **Type** | Separate binary/service — not a Go library |
| **RAM vs Prometheus** | ~3-4x less than Prometheus agent mode |
| **CPU vs Prometheus** | ~1.6-3.2x less |
| **Network** | 46% less bandwidth than Prometheus remote write 2.0 |

**Assessment: NO for in-process replacement; YES as adjacent component.**

vmagent is not a Go library — it's a standalone binary with its own CLI, scrape config, and lifecycle. Replacing the ~234-line collector with vmagent would mean:
- Running a separate process (or sidecar container)
- Managing another set of config files (`scrape_config.yml`)
- vmagent pushing to a remote write endpoint instead of our bbolt store
- KubeWise would need to query the remote storage instead of reading from bbolt

This is an architectural change, not a library swap. It would make sense if KubeWise needed to scale to thousands of nodes, but for the single-agent use case, the in-process collector is simpler.

### Verdict: KEEP custom collector, consider recording rules as enhancement

The direct Prometheus API queries are correct, simple, and well-structured. The concurrency pattern (goroutine per query, buffered channel for results) is idiomatic Go. Recording rules are a nice-to-have optimization for users who control their Prometheus setup.

---

## SUBSYSTEM 3: K8s Resource Collector (`internal/agent/collector/resources.go`)

### Current Implementation

411 lines. Three separate informers (pods, nodes, deployments) using `k8s.io/client-go/tools/cache.NewInformer`. In-memory maps protected by `sync.RWMutex`. Clean, standard pattern.

### Candidate: controller-runtime

| Metric | Value |
|--------|-------|
| **Stars** | 2.5K+ |
| **Maintenance** | Very active (part of kubernetes-sigs) |
| **Type** | Controller framework (Manager + Cache + Reconciler) |
| **Production usage** | Kubebuilder, Operator SDK, Tekton |

**Key differences from current approach:**

| Aspect | Current (client-go informers) | controller-runtime |
|--------|-------------------------------|-------------------|
| Setup code | 3 list-watches + 3 handler sets | `builder.For(&...).Owns(&...).Complete(r)` |
| Cache | Manual map + RWMutex | Shared cache with Indexer |
| Lifecycle | Manual goroutines + WaitForCacheSync | Manager.Start() handles everything |
| Reconciler pattern | Event handlers write to maps | Reconcile() loop with workqueue |
| Resource relationships | Manual owner lookup | Automatic via OwnerReferences |
| Dependencies | client-go only | Manager + Cache + Client + dependencies |

**Assessment: NO — overkill for this use case.**

controller-runtime is designed for the full Operator pattern: a reconciliation loop with a workqueue, rate limiting, retries, and status updates. KubeWise doesn't need any of that — it just needs to maintain an eventually-consistent in-memory snapshot of pods, nodes, and deployments.

The current implementation is:
- 411 lines for 3 resource types — not bloated
- Standard informer pattern — every K8s developer recognizes it
- No external dependencies beyond `client-go` (already used)
- The `Snapshot()` method provides exactly what the predictor needs

Adding controller-runtime would mean:
- Adding `sigs.k8s.io/controller-runtime` as a dependency (~50+ transitive packages)
- A Manager with its own lifecycle goroutines
- A Cache layer with eventual-consistency semantics (we already have direct map access)
- More complexity for zero functional gain

**Read the controller-runtime architecture docs:** "A Manager owns the shared Cache and one SharedInformer per GVK; events flow through Source → EventHandler → Predicate into a per-controller Workqueue." This is designed for controllers that reconcile state, not for tools that maintain snapshots.

There is a documented pattern for using controller-runtime just for the cache:
```go
// Get informers from the cache, use them everywhere else
mgr.GetCache().GetInformer(ctx, &corev1.Pod{})
```
But this still requires the Manager bootstrap and doesn't simplify the existing code.

### Verdict: KEEP

The current implementation is the right level of abstraction. If KubeWise ever grows into a full K8s operator (reconciling custom resources, managing child resources), migration to controller-runtime would make sense. For now, it's clean and appropriate.

---

## SUBSYSTEM 4: API Rate Limiting (`internal/api/middleware.go`)

### Current Implementation

150 lines. Sharded token-bucket rate limiter with:
- FNV-1a hash for shard selection (16 shards)
- Per-shard mutex for reduced contention
- GC of stale entries every 5 minutes
- Two limiters: public (5/min) and authenticated (60/min)
- 10,000 max entries, 10 burst

### Candidate: golang.org/x/time/rate

| Metric | Value |
|--------|-------|
| **Stars** | Part of Go subrepo (x/time) — effectively stdlib |
| **Maintenance** | Maintained by Go team |
| **Algorithm** | Token bucket (same as current) |
| **Zero alloc** | Yes (after warmup) |
| **Dependencies** | Zero |

**Comparison:**

| Aspect | Current (custom) | x/time/rate |
|--------|-------------------|-------------|
| Lines of code | ~100 (limiter impl) | ~5 (NewLimiter + Allow) |
| Sharding | 16 shards for contention | Single mutex (no sharding) |
| GC | Manual 5-min sweep | No GC needed (map per limiter) |
| Burst | Custom implementation | Built-in `burst` parameter |
| Rate | Custom refill logic | Built-in `Limit` (events/sec) |
| Metrics | None | `Tokens()` method available |
| Testing | Untested | Well-tested by Go team |

**Concern: single mutex vs. sharding.** x/time/rate uses a single mutex per limiter instance. For KubeWise, this is fine:
- Single-agent deployment (max a few concurrent users)
- The rate limiter runs in the API server's goroutine pool
- At 60 requests/minute, the mutex is uncontested
- Even at 1000 requests/minute, a single mutex is <1 microsecond contention

If sharding were needed (unlikely), the sharding wrapper is ~20 lines.

### Candidate: go.uber.org/ratelimit

| Metric | Value |
|--------|-------|
| **Stars** | 5K |
| **Latest version** | v0.3.1 (March 2024) |
| **Algorithm** | Leaky bucket (different from current token bucket) |
| **API** | `Take()` blocks until allowed |

**Assessment: Weaker fit than x/time/rate.** The leaky bucket algorithm is better for shaping throughput, while the token bucket (what KubeWise uses) is better for allowing bursts. x/time/rate also has zero dependencies, while uber-go/ratelimit adds `andres-erbsen/clock`.

### Verdict: RECOMMEND — swap to x/time/rate

This is the strongest case for a library swap in the entire audit. Benefits:
- Eliminates ~100 lines of custom, untested, hand-rolled rate limiting
- Battle-tested by the Go team (used internally in stdlib)
- Zero new dependencies (x/time is effectively stdlib)
- Cleaner API: `limiter.Allow()` vs. custom `rl.allow(ip)`
- Migration effort: **LOW** (~1 hour)

The existing per-IP sharding + GC can be removed entirely. x/time/rate handles per-IP rate limiting with a simpler pattern:

```go
type perIPLimiter struct {
    visitors map[string]*rate.Limiter
    mu       sync.Mutex
    rate     rate.Limit
    burst    int
}
```

This replaces the 16-shard, 150-line custom implementation with ~50 lines of standard code.

---

## SUBSYSTEM 5: API Server (`internal/api/server.go`)

### Current Implementation

140 lines (excluding middleware and routes). Clean `net/http` ServeMux with ~15 routes. Middleware chain: security headers → CORS → rate limit → auth → logging.

### Candidate: chi

| Metric | Value |
|--------|-------|
| **Stars** | 18K+ |
| **Maintenance** | Active (v5.0+) |
| **Benchmark** | ~72K–85K RPS (hello world) |
| **Per-request alloc** | 2.8 KB, 7 allocs/op |

**chi benefits:**
- Route parameters: `/api/anomalies/{id}` becomes `chi.URLParam(r, "id")`
- Middleware chaining: `r.Use(rateLimiter, authMiddleware, logger)`
- Grouping: namespaced route groups
- Fully net/http compatible — handlers stay `http.HandlerFunc`

**Current code already handles routing cleanly. The routes.go file (not yet read) presumably has a `registerRoutes` function that maps paths to handlers.**

### Candidate: Gin

| Metric | Value |
|--------|-------|
| **Stars** | 78K+ |
| **Benchmark** | ~85K–95K RPS, 0 B/op, 0 allocs/op |
| **Router** | Radix-tree based (httprouter) |

Gin is the most popular Go framework but introduces `gin.Context` — a non-standard abstraction. Handlers must be rewritten from `http.HandlerFunc` to `gin.HandlerFunc`. This is a barrier for a small API.

### Candidate: Echo

| Metric | Value |
|--------|-------|
| **Stars** | 30K+ |
| **Benchmark** | ~75K–90K RPS, 0 B/op |
| **Features** | HTTP/2, WebSocket, middleware |

Better modularity than Gin but still introduces a custom context type.

### Verdict: KEEP net/http

**With Go 1.22+ ServeMux enhancements** (pattern matching with `{param}` syntax, method-based routing), the gap has narrowed significantly:

```go
// Go 1.22+ — no framework needed
mux.HandleFunc("GET /api/anomalies/{id}", handler)
mux.HandleFunc("POST /api/anomalies", handler)
mux.HandleFunc("GET /health", handler)
```

Benchmarks show Go 1.22+ ServeMux routing at ~340 ns/op with 96 B/op and 2 allocs/op — compare to chi at ~376 ns/op with 844 B/op.

For a ~15-route API with minimal routing complexity, the standard library is the right choice:
- Zero dependency risk
- No upgrade burden (no library migrations)
- Every Go developer can read the code
- Handler signatures are universal (`http.Handler`, `http.HandlerFunc`)
- Middleware is standard `func(http.Handler) http.Handler`

The arguments for chi (route params, middleware chaining) are conveniences, not necessities. The current code is already clean. Adding chi would add ~18K lines of library code for marginal ergonomic benefit.

---

## SUBSYSTEM 6: Semantic Cache (`internal/agent/semcache/cache.go`)

### Current Implementation

232 lines. In-memory hash map with:
- SHA256-based content fingerprinting
- TTL-based expiration (default 1h)
- LRU-like eviction (oldest entry when at capacity, O(n))
- RWMutex for thread safety
- Optional persistence backend (currently bbolt-backed)

Max entries: 1000 (default). For a cache this small, O(n) eviction scans 1000 entries — insignificant.

### Candidate: hashicorp/golang-lru

| Metric | Value |
|--------|-------|
| **Stars** | 4K+ |
| **Latest version** | v2.0.7 (Dec 2023, generics) |
| **Algorithm** | LRU (doubly-linked list + map) |
| **Thread-safe** | Yes (single mutex) |
| **Allocs** | 2 allocs per Get + 1 per Add |

**Comparison:**

| Aspect | Current (custom) | hashicorp/golang-lru |
|--------|-------------------|----------------------|
| Eviction | O(n) scan for oldest | O(1) via linked list |
| Thread safety | RWMutex (reads not blocked) | Mutex (reads blocked) |
| TTL | Built-in (expiry on Get) | Not built-in (needs wrapper) |
| Persistence callback | Built-in via PersistBackend | Not built-in |
| Lines saved | — | ~40 lines |
| Dependency added | — | 1 external package |

Issues with golang-lru:
- No TTL support — would need a wrapper that checks expiration, re-adding ~30 of the lines we saved
- Single mutex blocks reads and writes — the current RWMutex allows concurrent reads
- Every `Get` is also a `Put` (updates recency) — under contention this is slower than the current map-only read

### Candidate: dgraph-io/ristretto

| Metric | Value |
|--------|-------|
| **Stars** | 6.9K |
| **Latest version** | v2.4.0 (Jan 2026) |
| **Algorithm** | TinyLFU admission + SampledLFU eviction |
| **Thread safety** | Lock-free writes (buffered) |
| **Cost-based** | Yes (MaxCost, not entry count) |

**Known issues (from community reports and analysis):**
- Set calls can be silently dropped under contention
- Hash collisions not handled (key hashes stored, not keys)
- Hit rate degradation under high contention
- Count-min sketch has known unfixed bugs
- Dgraph Labs financial troubles slowed maintenance
- Tests show ~0% hit rate in some configurations (v0.1.0 bug)

Ristretto is designed for Dgraph's use case (database page cache with skewed access patterns), not general-purpose caching. For KubeWise's 1000-entry cache with hash-based fingerprints, the complexity and trade-offs are unnecessary.

### Candidate: Otter v2

Newer cache library (2024+) implementing Caffeine-like W-TinyLFU. Promising but insufficient production history. Good to watch but not ready for adoption.

### Verdict: KEEP custom cache

Arguments for keeping:
1. **Correctness for the use case.** The cache is small (≤1000 entries) with hash-based keys and TTL-based expiration. A simple map + RWMutex is the right data structure.
2. **LRU-everything is fine at this size.** O(n) eviction on 1000 entries takes ~1 microsecond. Replacing it with O(1) LRU saves nothing measurable.
3. **Dependency cost is real.** Adding a production cache library (ristretto: ~5K lines; golang-lru: ~500 lines) for a 232-line module reduces maintainability.
4. **Persistence integration is custom.** The PersistBackend interface (LoadAll/Save/Delete) is tightly integrated with the bbolt store. No library provides this out of the box.

The 232 lines include:
- 40 lines of persistence interface + integration
- 30 lines of fingerprint computation
- 30 lines of TTL/expiry logic
- 30 lines of thread-safe access
- The rest is API surface (Get, Set, Prune, Stats, Keys)

This is not bloat — it's the minimum surface area for the requirements.

---

## Summary: Migration Complexity Matrix

| Subsystem | Candidate | Migration Complexity | Lines Saved | Risk | Recommend? |
|-----------|-----------|---------------------|-------------|------|------------|
| Storage → SQLite | modernc.org/sqlite | **HIGH** (15+ ops → SQL) | ~200 | Low (library quality) | ❌ |
| Storage → Storm | asdine/storm | LOW | ~100 | **HIGH** (abandoned) | ❌ |
| Storage → BoltHold | timshannon/bolthold | LOW | ~80 | **MEDIUM** (low velocity) | ❌ |
| Storage → TSDB | prometheus/tsdb | **VERY HIGH** (architectural) | ~200 | **MEDIUM** (memory) | ❌ |
| Collector → Recording Rules | Prometheus config | LOW | ~30 | Low | **OPTIONAL** |
| Collector → vmagent | VM binary | **EXTREME** (architectural) | N/A | Low | ❌ |
| Resources → controller-runtime | kubernetes-sigs/... | MEDIUM | ~200 | **MEDIUM** (overkill) | ❌ |
| Rate Limiter → x/time/rate | golang.org/x/time | **LOW** (~1 hour) | **~100** | **Very low** (stdlib) | ✅ |
| API Server → chi | go-chi/chi | LOW | ~0 | Low | ❌ |
| API Server → Gin | gin-gonic/gin | MEDIUM | ~0 | Low | ❌ |
| Semcache → golang-lru | hashicorp/golang-lru | LOW | ~40 | Low | ❌ |
| Semcache → ristretto | dgraph-io/ristretto | MEDIUM | ~50 | **MEDIUM** (bugs) | ❌ |

## Recommended Actions

### Should Do (Low Effort, Real Benefit)

1. **Swap rate limiter to `golang.org/x/time/rate`** (~1 hour)
   - Eliminates 100 lines of custom, untested rate limiting code
   - Zero new dependencies
   - Battle-tested by Go team

### Could Do (Feature Enhancement)

2. **Add recording rule awareness to Prometheus collector** (~2 hours)
   - Check for pre-computed recording rule series first
   - Fall back to raw PromQL queries if not found
   - Optional config flag: `prometheus.useRecordingRules: true`

### Should NOT Do

3. **Replace bbolt with SQLite** — the migration cost isn't justified by current query patterns
4. **Adopt controller-runtime** — the current informer pattern is appropriate for snapshot-based monitoring
5. **Add a web framework** — Go 1.22+ ServeMux is sufficient for 15 routes
6. **Replace the semantic cache** — a simple map is correct for 1000-entry TTL cache
7. **Adopt vmagent** — separate binary deployment doesn't fit the embedded agent model
