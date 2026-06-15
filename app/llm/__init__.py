"""Phase 3.5 LLM adapter seam (ADR-0025/0026/0027).

A thin, provider-agnostic boundary the enrichment passes call through:

    LLMClient.parse(messages, schema, model_ref) -> validated object | raises

Per-provider adapters (Anthropic native, OpenAI native, local OpenAI-compatible HTTP)
implement one uniform call; structured output is schema-constrained and validated, with a
persistent response cache for reproducibility (ADR-0027). Nothing here writes wiki pages —
enrichment output lands in a per-source artifact and the deterministic renderer composes it
(ADR-0025).
"""
