"""Rich descriptions for every configuration value exposed by /status.

Each entry maps a constant/environment-variable name to an 80-120 word
description explaining what the value is, what it controls, and which
components depend on it.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Environment Variable Descriptions
# ---------------------------------------------------------------------------
ENV_VAR_DESCRIPTIONS: dict[str, str] = {
    "CTXGATE_VLLM_URL": (
        "Base URL of the upstream vLLM inference server that ctxgate-proxy forwards "
        "all chat-completion requests to. The proxy appends model-specific endpoints "
        "(e.g. /chat/completions) to this base. If unreachable, the circuit breaker "
        "opens after consecutive failures and all downstream calls short-circuit with "
        "a 503 until the health probe succeeds again. Default points to localhost:29000 "
        "where the local vLLM instance serves Qwen models."
    ),
    "CTXGATE_VLLM_MODEL": (
        "Model identifier sent in the model field of every OpenAI-compatible request "
        "forwarded to vLLM. Must match a model loaded on the vLLM server. Used by the "
        "/v1/models endpoint to advertise available models and by the token estimator "
        "to select the correct tokenizer. Changing this requires a corresponding vLLM "
        "reload; mismatched names cause 404s from the upstream."
    ),
    "CTXGATE_MAX_CONTEXT": (
        "Total context-window size in tokens of the underlying LLM. This is the "
        "hard ceiling that every other budget value derives from: MAX_INPUT + "
        "SAFETY_MARGIN + MAX_OUTPUT must not exceed this. It governs how much "
        "conversation history the sliding window can retain before trimming triggers. "
        "Primarily sourced from the Goose custom-provider config; falls back to this "
        "env var, then to 84000."
    ),
    "CTXGATE_MAX_OUTPUT": (
        "Maximum number of tokens the model is allowed to generate in a single "
        "completion response. Set as the max_tokens parameter forwarded to vLLM. "
        "Derived by default as 20 percent of MAX_CONTEXT. Together with SAFETY_MARGIN it "
        "determines the upper bound of MAX_INPUT. Exceeding this causes the model to "
        "stop mid-generation, triggering the continuation mechanism up to MAX_CONTINUATIONS times."
    ),
    "CTXGATE_SAFETY_MARGIN": (
        "Buffer of tokens reserved between the end of user-supplied input and the "
        "start of model output within the total context window. Absorbs estimation "
        "error from the token counter which uses a calibrated heuristic rather than "
        "exact BPE counting. Defaults to 5 percent of MAX_CONTEXT. Too small risks truncation "
        "errors from vLLM; too large wastes usable input capacity."
    ),
    "CTXGATE_MAX_INPUT": (
        "Maximum tokens of conversation history (messages plus system prompt plus tool results) "
        "that may be included in a single forward call. Computed as MAX_CONTEXT minus "
        "SAFETY_MARGIN minus MAX_OUTPUT. When the accumulated input exceeds this threshold, "
        "the window manager triggers a trim/eviction cycle to drop oldest turns while "
        "preserving structural integrity of the dialogue."
    ),
    "CTXGATE_TRIM_TARGET_TOKENS": (
        "Absolute token count target for the post-trim window. When non-zero, overrides "
        "the fractional-based TRIM_TARGET_FRACTION calculation. After a trim event, the "
        "window manager cuts messages until the remaining payload is at or below this "
        "value. Zero means use fraction-based targeting instead. Useful for pinning "
        "an exact budget regardless of context size changes."
    ),
    "CTXGATE_TRIM_TARGET_FRACTION": (
        "Fraction of MAX_INPUT that the trimmed window should occupy after an eviction "
        "cycle. For example, 0.70 means keep 70 percent of the available input budget. The "
        "actual target is max(TRIM_TARGET_FLOOR, int(MAX_INPUT * fraction)). This "
        "controls how aggressively old context is discarded. Higher values preserve more "
        "history but leave less headroom for new turns."
    ),
    "CTXGATE_TRIM_TARGET_FLOOR": (
        "Minimum absolute token floor for the post-trim window, preventing the "
        "fractional calculation from producing unreasonably small windows. Even if the "
        "fraction would yield fewer tokens than this floor, the window manager keeps at "
        "least this many tokens of context. Defaults to 25 percent of MAX_CONTEXT. Acts as a "
        "safety net against misconfigured fractions collapsing useful context."
    ),
    "CTXGATE_SUMMARY_BACKFILL_CHARS": (
        "Character budget allocated to the summary block that replaces evicted older "
        "messages during a trim operation. When the window trims N turns, their content "
        "is condensed into a summary string capped at this length and prepended to the "
        "remaining messages. Larger values preserve more semantic detail from dropped "
        "context at the cost of consuming input-token budget."
    ),
    "CTXGATE_KNOWLEDGE_EXTRACT_MIN_CHARS": (
        "Minimum character length a message body must reach before the knowledge-extraction "
        "pipeline considers it eligible for durable memory storage. Shorter messages are "
        "skipped to avoid polluting the long-term store with trivial acknowledgments or "
        "one-word replies. Guards against noise accumulation in the SQLite-backed memory DB."
    ),
    "CTXGATE_SUMMARY_CHUNK_CHARS": (
        "Maximum character size of each chunk fed to the summarization LLM when generating "
        "backfill summaries for evicted context. Content exceeding this limit is split into "
        "sequential chunks, each summarized independently, then concatenated. Controls the "
        "granularity of summary quality versus API call count."
    ),
    "CTXGATE_SUMMARY_MAX_CHUNKS": (
        "Upper limit on the number of sequential summarization chunks processed in a single "
        "trim-backfill operation. Prevents runaway token consumption when very large amounts "
        "of context are evicted at once. Chunks beyond this cap are simply dropped without "
        "summarization, trading completeness for latency and cost bounds."
    ),
    "CTXGATE_SUMMARY_TOOL_CAP_CHARS": (
        "Character cap applied to tool-call and tool-result messages before they enter the "
        "summarization pipeline. Tool outputs (file reads, command results) can be extremely "
        "long; capping them prevents a single verbose tool result from dominating the summary "
        "budget and displacing more important conversational context."
    ),
    "CTXGATE_WINDOW_TRIGGER_FRAC": (
        "Fraction of MAX_INPUT at which the proactive window-eviction trigger fires. When "
        "accumulated input tokens exceed this fraction of the budget, the trim cycle initiates "
        "before hitting the hard limit. Acts as an early-warning threshold to avoid last-moment "
        "truncation errors from vLLM. Higher values delay trimming; lower values trim earlier."
    ),
    "CTXGATE_WINDOW_TARGET_FRAC": (
        "Target fill-ratio for the window after a proactive trim completes. Distinct from "
        "TRIM_TARGET_FRACTION in that it applies specifically to the proactive pre-threshold "
        "trim path rather than reactive overflow recovery. Sets how full the window should be "
        "post-trim to balance responsiveness against context richness for upcoming turns."
    ),
    "CTXGATE_PROACTIVE_TRIM_FRAC": (
        "Threshold fraction of MAX_INPUT that triggers the proactive predictive trim pass, "
        "as opposed to the reactive trim that fires only after a vLLM context-length error. "
        "When running input crosses this ratio, the window manager preemptively trims to stay "
        "within safe operating range. Decouples timing of trims from hard failures."
    ),
    "CTXGATE_HOST": (
        "Network interface address on which the ctxgate-proxy HTTP server binds. Typically "
        "127.0.0.1 for local-only deployment behind a reverse proxy, or 0.0.0.0 when exposing "
        "directly. Combined with PROXY_PORT determines the listening socket. Binding to a "
        "non-loopback address without authentication is strongly discouraged."
    ),
    "PROXY_PORT": (
        "TCP port number on which the ctxgate-proxy FastAPI application listens. Fixed at "
        "9201 in production deployments managed by systemd. This is the primary ingress point "
        "for all agent traffic routed through the proxy. Must not conflict with the dashboard "
        "port 9202 or vLLM server port 29000."
    ),
    "CTXGATE_ALLOW_NO_AUTH": (
        "Boolean flag that disables all API-key authentication checks. When true, "
        "any client can access protected endpoints without credentials. Intended exclusively for "
        "local development and testing. In production this must remain false; the auth middleware "
        "will reject all requests lacking a valid X-API-Key header matching CTXGATE_API_KEYS."
    ),
    "CTXGATE_API_KEYS": (
        "Comma-separated list of valid API keys accepted by the authentication layer. Each key "
        "in the list grants full access to all authenticated endpoints. Keys are compared as plain "
        "strings. Empty string means no keys are valid, effectively locking out all clients unless "
        "CTXGATE_ALLOW_NO_AUTH is true."
    ),
    "CTXGATE_ADMIN_KEY": (
        "Single elevated-privilege API key that grants access to admin-only endpoints such as "
        "/metrics, /api/sessions, /api/recent-calls, and /api/errors. Separate from regular "
        "API keys to enable differentiated access control. Operators can monitor system health "
        "without giving full inference access. Checked via the require_admin dependency."
    ),
    "PI_KEY": (
        "Authentication key for the Prompt Injection detection service. "
        "Used when sending message content to the PI scoring endpoint for real-time threat "
        "assessment. Empty string disables PI checking entirely. The score influences whether "
        "incoming messages get flagged, quarantined, or passed through to the main pipeline."
    ),
    "CTXGATE_GOOSE_INTEGRATION": (
        "Boolean master switch enabling or disabling integration with the Goose agent framework"
        "s session management. When true, the proxy reads Goose session databases to resolve "
        "session identities, caches lookups, and aligns its internal session keying with Goose"
        "s UUID namespace. When false, the proxy operates standalone with its own fingerprinting."
    ),
    "CTXGATE_GOOSE_SESSION_CACHE_MAX": (
        "Maximum number of entries in the in-memory LRU cache for resolved Goose session "
        "identities. Prevents unbounded memory growth in long-running proxy processes. When the "
        "cache is full, least-recently-used entries are evicted. Sizing should account for the "
        "typical concurrent session count multiplied by expected lookup frequency per session."
    ),
    "CTXGATE_GOOSE_SESSION_CACHE_TTL": (
        "Time-to-live in seconds for individual entries in the Goose session identity cache. "
        "After expiry, the next lookup for that session key re-reads from the SQLite database. "
        "Balances freshness against database query load. Default 60 seconds provides near-real-time "
        "resolution with minimal DB pressure."
    ),
    "CTXGATE_GOOSE_SESSION_LOOKUP_WINDOW_SEC": (
        "Temporal window in seconds used when searching the Goose sessions database for a "
        "matching session record based on timestamp proximity. Narrows the SQL query scope to "
        "recent records, avoiding full-table scans on large databases. Should cover the maximum "
        "expected gap between session creation and first proxy interaction."
    ),
    "CTXGATE_GOOSE_SESSION_RESOLVE": (
        "Boolean controlling whether the proxy actively resolves incoming request metadata to "
        "canonical Goose session IDs, or merely passes through whatever session identifier the "
        "client provides. When enabled, ensures consistent session keying across multiple proxy "
        "instances sharing the same Goose backend."
    ),
    "CTXGATE_SESSION_IDENTITY": (
        "String tag identifying which agent framework generated the session identifiers being "
        "processed. Currently set to goose to distinguish from other potential frameworks. "
        "Influences the session-key derivation algorithm and which database tables are queried "
        "during identity resolution. Future-proofing allows plugging in alternative frameworks."
    ),
    "CTXGATE_LM_ENABLED": (
        "Master boolean toggle for the Long-Term Memory subsystem. When true, the proxy "
        "extracts salient facts from conversations, stores them in a vector-indexed SQLite "
        "database, and injects relevant memories into future prompts. When false, all memory "
        "write/read paths are bypassed, reducing per-request latency by eliminating embedding "
        "generation and similarity search overhead."
    ),
    "CTXGATE_MAX_TOTAL_OUTPUT": (
        "Cumulative token budget cap across all continuations in a single user turn. While "
        "MAX_OUTPUT limits each individual completion segment, this value caps the total "
        "output tokens emitted over the entire multi-segment generation sequence. Prevents "
        "pathological cases where repeated continuations produce excessively long responses."
    ),
    "CTXGATE_MIN_CONTINUATION_OUTPUT": (
        "Minimum number of new tokens a continuation segment must produce to be considered "
        "valid. If a continuation yields fewer tokens than this threshold, it is treated as a "
        "stall/loop and the continuation chain terminates early. Guards against degenerate "
        "cases where the model emits one or two tokens repeatedly without meaningful progress."
    ),
    "CTXGATE_PREFIX_DIAG_ON": (
        "Debug flag enabling detailed prefix-stability diagnostics in the token calibration "
        "system. When active, logs additional metrics about how consistently the leading "
        "portion of prompts produces stable token counts across runs. Useful for tuning the "
        "calibrated estimators confidence intervals during development."
    ),
    "CTXGATE_INJECT_EPOCH_FREEZE_ON": (
        "Boolean controlling injection of a frozen epoch/timestamp reference into system "
        "prompts. When enabled, adds a static date marker so the model has temporal grounding "
        "without requiring wall-clock time in every message. Reduces variance in model behavior "
        "across sessions while maintaining awareness of approximate recency."
    ),
    "CTXGATE_INJECT_MAX_TOKENS_CFG": (
        "Override value for the max_tokens parameter injected into standard non-recap "
        "requests forwarded to vLLM. Allows fine-tuning the effective output limit independent "
        "of the global MAX_OUTPUT setting. Useful when certain request classes benefit from "
        "shorter completions such as classification versus generation tasks."
    ),
    "CTXGATE_INJECT_MAX_TOKENS_RECAP_CFG": (
        "Override value for max_tokens specifically for recap/summary generation calls. "
        "Recaps typically need longer output to faithfully compress extended contexts, so this "
        "separate knob allows allocating a larger generation budget to summarization without "
        "inflating the limit for normal chat completions."
    ),
    "CTXGATE_TENANT_MAX_CONCURRENCY": (
        "Per-tenant maximum number of simultaneously in-flight requests. Zero disables the "
        "limit. Prevents a single noisy tenant from monopolizing GPU inference slots and starving "
        "other tenants. Enforced via an asyncio semaphore keyed by tenant identifier extracted "
        "from the API key."
    ),
    "CTXGATE_TENANT_RATE_BURST": (
        "Burst allowance in requests for the per-tenant token-bucket rate limiter. Defines "
        "how many requests a tenant can make in rapid succession before the sustained rate "
        "limit kicks in. Zero disables burst tracking. Typical values allow short bursts for "
        "interactive use while throttling automated bulk workloads."
    ),
    "CTXGATE_TENANT_RATE_PER_SEC": (
        "Sustained request rate limit (requests per second) applied per tenant after the "
        "burst allowance is exhausted. Implemented as a token bucket refilling at this rate. "
        "Zero disables rate limiting entirely. Protects the shared vLLM GPU from overload by "
        "individual clients generating excessive parallel inference load."
    ),
    "CTXGATE_TEST_ENDPOINTS": (
        "Boolean flag exposing additional diagnostic/test endpoints normally hidden in "
        "production. Enables endpoints like raw tokenizer inspection, breaker state dumps, "
        "and synthetic request generators. Should remain false in any deployment accessible "
        "beyond the local machine to prevent information disclosure."
    ),
    "CTXGATE_BLOCK_UNICODE_ESCAPES": (
        "Boolean controlling whether Unicode escape sequences in incoming JSON "
        "payloads are rejected or normalized. When true, payloads containing raw escape "
        "sequences are rejected with a 400 to prevent encoding-related tokenizer mismatches. "
        "Addresses edge cases where double-encoded UTF-8 inflates apparent token counts."
    ),
}


# ---------------------------------------------------------------------------
# Constant Descriptions (non-env-var constants in constants.py)
# ---------------------------------------------------------------------------
CONSTANT_DESCRIPTIONS: dict[str, str] = {
    "MAX_CONTEXT": (
        "The total context window capacity in tokens for the deployed LLM model. This is "
        "the fundamental constraint governing all window-management decisions. Derived from "
        "the Goose provider config with fallback to CTXGATE_MAX_CONTEXT env var, then 84000. "
        "Every other budget parameter must sum to no more than this value. Determines how much "
        "conversation history can be retained before mandatory trimming occurs."
    ),
    "MAX_OUTPUT": (
        "Maximum tokens the model generates per single completion call. Forwarded to vLLM as "
        "the max_tokens parameter. By default computed as 20 percent of MAX_CONTEXT. When the "
        "model reaches this limit mid-sentence, the continuation mechanism attempts to resume "
        "generation up to MAX_CONTINUATIONS times, each subject to MIN_CONTINUATION_OUTPUT validation."
    ),
    "SAFETY_MARGIN": (
        "Reserved token buffer accounting for imprecision in the calibrated token estimator. "
        "Since the proxy estimates token counts heuristically using EWMA-smoothed ratios rather "
        "than exact BPE tokenization for performance, this margin absorbs estimation drift. "
        "Defaults to 5 percent of MAX_CONTEXT. Without it, estimated-fit requests might actually "
        "overflow the true context limit causing vLLM 400 errors."
    ),
    "MAX_INPUT": (
        "Effective maximum input tokens per request, computed as MAX_CONTEXT minus SAFETY_MARGIN "
        "minus MAX_OUTPUT. Represents the practical ceiling for conversation history, system "
        "prompts, tool definitions, and injected memories combined. When cumulative input "
        "approaches this value, the window manager initiates trimming to maintain headroom."
    ),
    "TRIM_TARGET_TOKENS": (
        "Absolute post-trim token target overriding fractional calculations when non-zero. "
        "After an eviction cycle, the window manager removes oldest message groups until the "
        "remaining payload meets or falls below this count. Provides deterministic sizing for "
        "operators who prefer fixed budgets over percentage-based targets."
    ),
    "TRIM_TARGET_FRACTION": (
        "Proportional target for post-trim window size expressed as a fraction of MAX_INPUT. "
        "At 0.70, the trimmed window retains 70 percent of available input capacity. The actual "
        "target is clamped by TRIM_TARGET_FLOOR to prevent degradation below a minimum useful "
        "context size. Works in concert with WINDOW_TRIGGER_FRAC which determines when trimming activates."
    ),
    "TRIM_TARGET_FLOOR": (
        "Hard minimum token count preserved after any trim operation, calculated as 25 percent "
        "of MAX_CONTEXT by default. Ensures that even aggressive trimming configurations cannot "
        "reduce the active context below a level where the model loses essential conversational "
        "grounding. Acts as a floor beneath both fractional and absolute trim targets."
    ),
    "SUMMARY_BACKFILL_CHARS": (
        "Character budget for the compressed summary replacing evicted message groups. When "
        "the window trims older turns, their textual content is distilled into a narrative "
        "summary bounded by this limit. The summary is inserted as a system-level preamble to "
        "the surviving messages, preserving semantic continuity without retaining verbatim text."
    ),
    "KNOWLEDGE_EXTRACT_MIN_CHARS": (
        "Minimum message length in characters qualifying for extraction into the durable "
        "knowledge/memory store. Filters out trivial exchanges that carry no lasting informational "
        "value. Messages below this threshold are processed normally for the current session but "
        "never persisted to long-term memory, keeping the vector index focused on substantive content."
    ),
    "SUMMARY_CHUNK_CHARS": (
        "Size limit in characters for individual segments passed to the summarization model. "
        "Long evicted contexts are divided into chunks of at most this size, each independently "
        "summarized before concatenation. Balances summary quality against API call cost and latency."
    ),
    "SUMMARY_MAX_CHUNKS": (
        "Ceiling on sequential summarization chunks per single backfill operation. Bounds the "
        "worst-case latency and token cost of generating replacement summaries when large "
        "amounts of context are evicted simultaneously. Excess content beyond this limit "
        "is discarded without summarization."
    ),
    "SUMMARY_TOOL_CAP_CHARS": (
        "Character limit applied to individual tool-call and tool-result messages before they "
        "enter the summarization pipeline. Verbose tool outputs are truncated to this cap prior "
        "to summary generation. Prevents a single oversized tool result from consuming the entire "
        "summary budget and obscuring more relevant conversational context."
    ),
    "CTXGATE_WINDOW_TRIGGER_FRAC": (
        "Activation threshold (fraction of MAX_INPUT) for the proactive window trimming "
        "mechanism. When accumulated input tokens cross this ratio, the trim cycle initiates "
        "ahead of any hard overflow. Separates predictive maintenance from reactive recovery."
    ),
    "CTXGATE_WINDOW_TARGET_FRAC": (
        "Desired fill level (fraction of MAX_INPUT) for the window following a proactive trim. "
        "Distinct from TRIM_TARGET_FRACTION in applying specifically to the predictive trim path. "
        "Sets the equilibrium point where the window stabilizes after shedding excess history."
    ),
    "CUT_QUANTUM": (
        "Granularity unit in tokens for message-group cutting during trim operations. The "
        "window manager evaluates removal candidates in quantum-sized blocks aligned to "
        "message boundaries, ensuring cuts land at natural dialogue breaks rather than mid-"
        "sentence. Smaller quanta allow finer-grained preservation; larger quanta reduce overhead."
    ),
    "RHO_COLD": (
        "Importance weight multiplier applied to cold (low-engagement, rarely-referenced) "
        "tool results during the importance-ranked eviction ordering. Cold tools receive lower "
        "priority scores, making them candidates for earlier removal when trimming is needed. "
        "Combined with RHO_HI and RHO_LO to form a graduated priority scale."
    ),
    "RHO_HI": (
        "Importance weight for high-priority (hot) content during eviction ranking. Messages "
        "and tool results marked as frequently referenced or recently active receive this "
        "elevated weight, protecting them from premature removal. The ratio RHO_HI/RHO_COLD "
        "determines how strongly the system discriminates between valuable and expendable context."
    ),
    "RHO_LO": (
        "Baseline importance weight for neutral-priority content in the eviction ranking "
        "algorithm. Items neither explicitly hot nor cold fall into this middle category. "
        "Together with RHO_HI and RHO_COLD forms a three-tier priority system guiding which "
        "message groups are sacrificed first during window compression."
    ),
    "K_TOOLS_COLD": (
        "Decay constant for the exponential forgetting curve applied to tool-result importance "
        "scores over time. Implements a half-life-like decay where unused tool context gradually "
        "loses priority ranking. A value of 1.537 corresponds to approximately 63 percent retention "
        "after one normalization period. Tuned empirically to match observed relevance falloff."
    ),
    "M_CAP": (
        "Upper bound (cap) for the dynamic memory-injection proportion. The maximum fraction "
        "of available input budget that retrieved long-term memories may occupy. At 0.25, at most "
        "25 percent of MAX_INPUT can be consumed by injected memories, leaving 75 percent for live dialogue."
    ),
    "M_FLOOR": (
        "Lower bound (floor) for the memory-injection proportion, ensuring a minimum allocation "
        "for retrieved context even when few memories match the current query. Guarantees that "
        "the memory system always contributes some grounding signal. At 0.08, at least 8 percent "
        "of input budget goes to memories when any are found."
    ),
    "M_FLOOR_COLD": (
        "Elevated floor applied specifically when retrieved memories are classified as cold "
        "(low-confidence matches). At 0.5, even weak memory matches claim at least half the "
        "available memory slot, acknowledging that partial relevance is better than none for "
        "maintaining conversational continuity across long sessions."
    ),
    "EWMA_ALPHA": (
        "Smoothing factor (alpha) for the Exponentially Weighted Moving Average used in the "
        "calibrated token estimator. Determines how quickly the estimator adapts to shifts in "
        "character-to-token ratios. At 0.5, recent observations carry significant weight, "
        "allowing fast adaptation to new content types while maintaining sufficient smoothing."
    ),
    "GLOBAL_ALPHA": (
        "Global smoothing constant applied across the entire estimation pipeline, affecting "
        "both per-message ratio tracking and aggregate calibration drift correction. Lower "
        "than EWMA_ALPHA (0.1 vs 0.5) to provide a slower-moving baseline that resists "
        "transitional anomalies while still allowing gradual recalibration."
    ),
    "LOOP_CHECK_EVERY": (
        "Number of generated tokens checked between repetition-detection evaluations. The loop "
        "detector examines every Nth token window for patterns indicating the model is stuck "
        "in a repetitive cycle. At 256, detection runs frequently enough to catch loops early "
        "while amortizing the comparison cost across generation."
    ),
    "LOOP_REPEATS": (
        "Number of consecutive identical pattern matches required before declaring a "
        "generative loop. Single repetitions are normal; four consecutive identical segments "
        "strongly indicate the model has entered a degenerate cycle. Upon detection, the stream "
        "is terminated and a retry with adjusted parameters is initiated."
    ),
    "LOOP_RETRIES": (
        "Maximum number of automatic retries attempted after a detected generative loop before "
        "surfacing the failure to the client. Each retry increases temperature and presence "
        "penalty to encourage divergence from the stuck pattern. One retry balances recovery "
        "probability against added latency."
    ),
    "LOOP_SENTENCE_REPEATS": (
        "Threshold for sentence-level repetition detection, distinct from token-level loop "
        "checking. Counts how many times the same normalized sentence appears consecutively "
        "before triggering intervention. Six repetitions indicates severe degeneracy. Catches "
        "loops that token-level checking misses because the model varies surface tokens."
    ),
    "LOOP_TAIL": (
        "Character window size examined by the loop detector for pattern matching. The last "
        "N characters of generated output are analyzed for repetitive structures. At 4000 chars, "
        "covers several paragraphs of typical output, providing sufficient context to identify "
        "cyclic patterns while keeping the comparison computation tractable."
    ),
    "MAX_BODY_BYTES": (
        "Maximum allowed size in bytes for incoming HTTP request bodies. At 20MB, accommodates "
        "large multi-file upload scenarios in tool-use workflows while preventing memory exhaustion "
        "from maliciously oversized payloads. Requests exceeding this limit are rejected with 413."
    ),
    "MAX_CONTINUATIONS": (
        "Maximum number of continuation segments attempted when a single completion hits the "
        "MAX_OUTPUT token limit before reaching a natural stopping point. Each continuation "
        "resumes generation from where the previous segment ended. Five continuations allow "
        "very long-form generations while bounding worst-case latency."
    ),
    "MAX_REASONING_TOKENS": (
        "Token budget allocated specifically for reasoning/thinking tokens in models that "
        "support interleaved reasoning traces. Carved out from the total output budget to ensure "
        "reasoning does not consume all generation capacity, leaving room for the final answer."
    ),
    "MEMORY_TTL_DAYS": (
        "Time-to-live duration in days for entries in the durable long-term memory store. "
        "Memories older than this period are eligible for garbage collection, freeing vector "
        "index space and preventing unbounded database growth. 90 days covers typical project "
        "lifecycle durations while bounding storage requirements."
    ),
    "SESSION_TTL_HOURS": (
        "Expiration timeframe in hours for active session state held in the proxy in-memory "
        "structures. Sessions inactive beyond this duration have their cached window state, "
        "token estimates, and continuation context purged. 12 hours covers typical daily work "
        "patterns while releasing resources from abandoned sessions."
    ),
    "WINDOW_TTL_DAYS": (
        "Persistence duration in days for window-state records stored in the database. Surviving "
        "sessions can resume their exact context position after proxy restarts within this "
        "window. Beyond this age, stored window states are cleaned up. Seven days provides "
        "weekend/holiday resilience without indefinite accumulation of stale state."
    ),
    "SSE_HEARTBEAT_INTERVAL": (
        "Interval in seconds between heartbeat/ping events sent over Server-Sent Events streams "
        "during long-running vLLM generations. Prevents intermediate proxies and client-side "
        "timeout mechanisms from interpreting silence as connection death. Ten seconds satisfies "
        "most HTTP client defaults while minimizing bandwidth overhead."
    ),
    "STABLE_ELIDE": (
        "Boolean enabling stable elision. The practice of removing redundant or duplicated "
        "content from the context window without replacing it with a summary. Unlike trimming "
        "which summarizes removed content, stable elision silently drops structurally redundant "
        "content. Reduces token count while preserving all unique information."
    ),
    "WALL_CLOCK_MAX": (
        "Maximum wall-clock time in seconds allowed for a single complete request-response "
        "cycle including all continuations, retries, and backoff delays. At 1800 seconds (30 "
        "minutes), accommodates complex multi-tool agentic workflows while providing a hard "
        "deadline that prevents zombie requests from holding connections and GPU slots."
    ),
    "WORKER_BACKPRESSURE": (
        "Queue depth threshold at which the async worker pool begins applying backpressure "
        "to incoming requests. When pending tasks exceed this count, new requests experience "
        "increased queuing delay or are rejected with 429, protecting the worker threads from "
        "overwhelming the downstream vLLM server. 50 represents a balanced point."
    ),
    "ESTIMATOR_MODE": (
        "Selects the operational mode of the token-counting engine. Calibrated uses the "
        "adaptive EWMA-based estimator (fast, approximate). Trades roughly 2-5 percent accuracy "
        "for 10-50x speed improvement, critical for maintaining low per-request latency in the "
        "proxys hot path."
    ),
    "GOOSE_SESSIONS_DB": (
        "Filesystem path to the SQLite database maintained by the Goose agent framework "
        "containing session metadata, message histories, and identity mappings. The proxy "
        "reads this database (read-only) to resolve session identities and retrieve historical "
        "context for cross-instance continuity."
    ),
    "GOOSE_SESSION_UUID_NAMESPACE": (
        "RFC 4122 UUID namespace identifier used for deterministic generation of session "
        "namespaced UUIDs. Ensures that the same logical session always maps to the same UUID "
        "across proxy instances and restarts, enabling reliable session keying in distributed "
        "deployments."
    ),
    "DB_DSN": (
        "Database connection string for the proxys own PostgreSQL datastore, used for "
        "persisting metrics, audit logs, session indices, and delivery tracking. Empty string "
        "indicates no external database is configured (operating in memory-only mode)."
    ),
    "QWEN_TOKENIZER_PATH": (
        "Filesystem path to the HuggingFace tokenizer configuration files for the specific "
        "Qwen model variant in use. Required for exact token counting modes and for calibrating "
        "the EWMA estimator against ground-truth counts. Empty path falls back to tiktoken approximations."
    ),
    "PRESENCE_PENALTY": (
        "Base presence penalty parameter forwarded to vLLM for standard generation "
        "calls. Encourages the model to introduce new topics rather than repeating ones already "
        "mentioned. Empty string means use model default. Adjusted upward automatically during "
        "retry calls to break repetitive loops."
    ),
    "RETRY_PRESENCE_PENALTY": (
        "Elevated presence penalty applied specifically on retry attempts after loop detection "
        "or generation failure. At 1.5 (vs typical base of 0-0.5), strongly discourages the "
        "model from reproducing previously-generated content, helping it find alternative "
        "phrasings to escape the degenerative cycle."
    ),
    "RETRY_TEMPERATURE": (
        "Increased sampling temperature used on retry calls following generation failures or "
        "loop detections. At 0.7 (vs typical base of 0.3-0.5), introduces more stochastic "
        "variation in token selection, increasing the probability of diverging from the failed pattern."
    ),
    "RETRY_TOP_P": (
        "Nucleus sampling parameter (top-p) applied during retry generation. At 0.8, restricts "
        "sampling to the smallest set of tokens whose cumulative probability exceeds 80 percent. "
        "Slightly wider than typical base settings to give the model more options when escaping "
        "a repetitive loop."
    ),
    "IN_TEMPERATURE": (
        "Sampling temperature for internal/inference calls made by the proxy itself (summary "
        "generation, knowledge extraction, memory consolidation) as opposed to passthrough "
        "client requests. At 0.3, favors deterministic, factually-grounded output appropriate "
        "for background processing tasks where consistency matters more than creativity."
    ),
    "MISTRAL_API_KEY": (
        "Authentication credential for the Mistral AI API, used by the durable-memory worker "
        "subprocess for embedding generation and semantic search operations. Kept separate from "
        "the vLLM backend to allow using cloud-based embeddings alongside local inference."
    ),
    "MISTRAL_MODEL": (
        "Specific Mistral model identifier used for memory-related inference tasks. "
        "mistral-small-latest provides a good balance of quality and cost for background "
        "processing that does not require the full capability of the primary Qwen model."
    ),
    "MISTRAL_SYSTEM_PROMPT": (
        "System prompt template injected into all Mistral API calls from the memory worker. "
        "Defines the persona and behavioral constraints for the memory-processing role. "
        "Shapes the quality and format of extracted knowledge entries."
    ),
    "MISTRAL_TIMEOUT": (
        "HTTP timeout in seconds for individual Mistral API requests from the memory worker. "
        "At 120 seconds, accommodates slow cloud API responses during peak load while providing "
        "a definitive deadline. On timeout, the memory operation is skipped gracefully."
    ),
    "MISTRAL_URL": (
        "Base URL for the Mistral AI REST API endpoint. Standard production URL points to "
        "their hosted service. Can be overridden to point at a self-hosted endpoint for "
        "air-gapped deployments or to route through a corporate API gateway."
    ),
    "VLLM_URL": (
        "Complete base URL of the vLLM inference server including version prefix. All chat "
        "completion, model listing, and tokenization requests are constructed relative to this "
        "address. The circuit breaker monitors connectivity to this endpoint."
    ),
    "VLLM_MODEL": (
        "Identifier of the specific model served by the vLLM instance. Appears in the model "
        "field of all forwarded requests and must exactly match a loaded model name on the "
        "server. Also used by the /v1/models discovery endpoint and tokenizer selection."
    ),
    "BLOCK_UNBOUNDED_LOOPS": (
        "Boolean safety flag enabling the hard-stop mechanism for truly unbounded generation "
        "loops that evade softer detection thresholds. When true, if loop detection triggers "
        "multiple times despite retries, the request is forcibly terminated and an error "
        "returned. Final guardrail against pathological model behavior."
    ),
}


# ---------------------------------------------------------------------------
# Computed Value Descriptions
# ---------------------------------------------------------------------------
COMPUTED_DESCRIPTIONS: dict[str, str] = {
    "trim_target": (
        "The effective post-trim token target calculated at runtime by combining TRIM_TARGET_TOKENS, "
        "TRIM_TARGET_FRACTION, and TRIM_TARGET_FLOOR according to priority rules: explicit absolute "
        "values override fractions, and the floor acts as a minimum clamp. Formula: max(TRIM_TARGET_FLOOR, "
        "TRIM_TARGET_TOKENS if greater than 0 else int(MAX_INPUT * TRIM_TARGET_FRACTION)). This is the "
        "concrete number the window manager aims for after each eviction cycle."
    ),
    "eviction_trigger": (
        "The input-token threshold at which proactive window eviction activates, computed as "
        "int(MAX_INPUT * CTXGATE_WINDOW_TRIGGER_FRAC). When the current input token count exceeds "
        "this value, the trim pipeline engages between turns rather than reactively after a vLLM "
        "error. Represents the yellow line in the windows utilization gauge."
    ),
    "effective_max_input": (
        "Runtime-resolved maximum input token budget, equal to MAX_CONTEXT minus SAFETY_MARGIN "
        "minus MAX_OUTPUT. This is the number that actually gates incoming request assembly: the "
        "message builder accumulates history, system prompts, tool definitions, and injected memories "
        "until approaching this ceiling, then stops adding items."
    ),
}


# ---------------------------------------------------------------------------
# Fixed/Hardcoded Value Descriptions
# ---------------------------------------------------------------------------
FIXED_VALUE_DESCRIPTIONS: dict[str, str] = {
    "breaker_failure_threshold": (
        "Number of consecutive request failures to the vLLM backend required before the circuit "
        "breaker transitions from CLOSED to OPEN state. Once open, all subsequent requests fail "
        "fast with 503 without attempting connection, protecting the system from cascading "
        "timeout storms. The breaker enters HALF_OPEN after a cooldown period, allowing a single "
        "test request to probe recovery before fully closing again."
    ),
    "breaker_cooldown_seconds": (
        "Duration in seconds the circuit breaker remains in OPEN state before transitioning to "
        "HALF_OPEN for a recovery probe. During this window, all requests are immediately rejected "
        "with 503 Service Unavailable. The cooldown gives the downstream vLLM server time to "
        "recover from OOM conditions, GPU faults, or network partitioning."
    ),
    "httpx_connect_timeout": (
        "TCP connection establishment timeout in seconds for outbound HTTP calls to the vLLM "
        "backend. Distinguished from read timeout, this covers only the initial handshake phase. "
        "A short value ensures dead servers are detected quickly without waiting for full TCP stack "
        "timeouts, enabling faster circuit-breaker activation."
    ),
    "httpx_read_timeout": (
        "Maximum wait time in seconds for receiving a complete response body from the vLLM server "
        "after the connection is established. Generous values accommodate long generation times "
        "for complex prompts with extensive tool-use chains. Exceeding this triggers a timeout "
        "error that feeds into the circuit breakers failure count."
    ),
    "sse_keepalive_interval": (
        "Interval in seconds between SSE keepalive/comment frames sent during active streaming "
        "responses. Maintains the HTTP connection alive through intermediaries that enforce idle "
        "timeouts. Sent as SSE comment lines that clients ignore but networks recognize as active "
        "traffic, preventing premature connection resets on long generations."
    ),
}
