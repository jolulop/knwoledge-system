# Retrieval relevance report (ADR-0038)

- generated: 2026-06-25T15:32:27+00:00
- corpus: evals/corpus/
- embedding_model_ref: BAAI/bge-m3
- index: vector_schema=1 embed_code=1 metric=cosine
- rrf_k: 60   graph_present: false   graph_boosts: none
- cases scored: 52 source-level + 8 chunk-level   skipped: 0   negative cases: 29

## Aggregate

| metric | value |
|---|---|
| MRR | 0.968 |
| recall@5 | 0.994 |
| recall@10 | 0.994 |
| hit@5 | 1.000 |
| hit@10 | 1.000 |
| neg@5 | 0.931 |
| neg@10 | 1.000 |
| discrimination (rel<irrel, 29 neg cases) | 0.931 |

## By category

| category | n | MRR | recall@5 | recall@10 | hit@5 | hit@10 | neg@5 | neg@10 |
|---|---|---|---|---|---|---|---|---|
| conceptual | 14 | 0.964 | 1.000 | 1.000 | 1.000 | 1.000 | 0.800 | 1.000 |
| disambiguation | 24 | 0.972 | 1.000 | 1.000 | 1.000 | 1.000 | 0.958 | 1.000 |
| exact_anchor | 9 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 |
| multi_source | 5 | 0.900 | 0.933 | 0.933 | 1.000 | 1.000 | 0.000 | 0.000 |

## Per query

| id | category | first_rank | recall@5 | recall@10 | hit@5 | hit@10 | neg@5 | neg@10 |
|---|---|---|---|---|---|---|---|---|
| anchor_telematics | exact_anchor | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| anchor_storage_bucket | exact_anchor | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| anchor_carbon_report | exact_anchor | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| anchor_partner_program | exact_anchor | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| anchor_quantum | exact_anchor | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_earnings_improved | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| paraphrase_eco_friendly | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_data_breach | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_headcount | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_predictive_maintenance | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_route_planning | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_private_equity | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| multi_products_offered | multi_source | 2 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| multi_bundled_by_partners | multi_source | 1 | 0.67 | 0.67 | 1.00 | 1.00 | 0.00 | 0.00 |
| multi_agentic_status | multi_source | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| disambig_q2_guidance | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_vehicle_health | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_route_planning | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_q3_revenue_driver | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_business_processes | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_software_delivery_model | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_ceo_leadership | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_engineer_reskilling | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_gen_ai_paradox | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 |
| disambig_agentic_machine_readable_handoffs | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_gen_ai_paradox_specific | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_horizontal_vertical_deployment | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_ceo_governance_architecture | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_24_hour_delivery_cycle | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_machine_readable_artifacts_specific | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_knowledge_graphs_for_agents | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_smaller_delivery_teams | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_private_equity_rollups_vs_agentic_scaling | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_private_equity_integration_vs_ai_governance | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_private_equity_holding_periods_vs_productivity | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_quantum_secure_communications_vs_ai_agents | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_quantum_sensing_vs_software_delivery | disambiguation | 3 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_quantum_infrastructure_challenges_vs_enterprise_architecture | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| anchor_electric_vans | exact_anchor | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| anchor_route_optimization | exact_anchor | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_sales_increase | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| paraphrase_reseller_packaging | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_emissions_goal | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| multi_two_software_offerings | multi_source | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_agentic_workflows | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| anchor_gen_ai_paradox | exact_anchor | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_agentic_enterprise_change | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 |
| paraphrase_agentic_software_delivery | conceptual | 2 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| disambig_agentic_business_vs_software | disambiguation | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| multi_agentic_operating_model | multi_source | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| anchor_private_equity_m_and_a | exact_anchor | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| paraphrase_private_equity_value_creation | conceptual | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |

## Discrimination (negative cases — relevant must rank above the distractor)

| id | first_relevant | first_irrelevant | relevant_wins |
|---|---|---|---|
| paraphrase_earnings_improved | 1 | 2 | yes |
| disambig_q2_guidance | 1 | 2 | yes |
| disambig_vehicle_health | 1 | 2 | yes |
| disambig_route_planning | 1 | 2 | yes |
| disambig_q3_revenue_driver | 1 | 2 | yes |
| disambig_agentic_business_processes | 1 | 2 | yes |
| disambig_agentic_software_delivery_model | 1 | 2 | yes |
| disambig_agentic_ceo_leadership | 1 | 2 | yes |
| disambig_agentic_engineer_reskilling | 1 | 2 | yes |
| disambig_agentic_gen_ai_paradox | 1 | 6 | yes |
| disambig_agentic_machine_readable_handoffs | 1 | 2 | yes |
| disambig_agentic_gen_ai_paradox_specific | 1 | 4 | yes |
| disambig_agentic_horizontal_vertical_deployment | 1 | 2 | yes |
| disambig_agentic_ceo_governance_architecture | 1 | 2 | yes |
| disambig_agentic_24_hour_delivery_cycle | 1 | 2 | yes |
| disambig_agentic_machine_readable_artifacts_specific | 1 | 2 | yes |
| disambig_agentic_knowledge_graphs_for_agents | 1 | 2 | yes |
| disambig_agentic_smaller_delivery_teams | 1 | 2 | yes |
| disambig_private_equity_rollups_vs_agentic_scaling | 1 | 2 | yes |
| disambig_private_equity_integration_vs_ai_governance | 1 | 2 | yes |
| disambig_private_equity_holding_periods_vs_productivity | 1 | 5 | yes |
| disambig_quantum_secure_communications_vs_ai_agents | 1 | 2 | yes |
| disambig_quantum_sensing_vs_software_delivery | 3 | 1 | NO |
| disambig_quantum_infrastructure_challenges_vs_enterprise_architecture | 1 | 3 | yes |
| paraphrase_sales_increase | 1 | 2 | yes |
| paraphrase_agentic_workflows | 1 | 2 | yes |
| paraphrase_agentic_enterprise_change | 1 | 6 | yes |
| paraphrase_agentic_software_delivery | 2 | 1 | NO |
| disambig_agentic_business_vs_software | 1 | 2 | yes |

## Channel Diagnostics (failed disambiguation — fusion-balance vs semantic ambiguity)

| id | kw_rel | kw_irr | vec_rel | vec_irr | label |
|---|---|---|---|---|---|
| disambig_quantum_sensing_vs_software_delivery | - | - | 3 | 1 | vector_prefers_irrelevant_keyword_silent |
| paraphrase_agentic_software_delivery | - | - | 2 | 1 | vector_prefers_irrelevant_keyword_silent |

## Chunk-Level Aggregate (chunk_disambiguation cases only — NOT in the source headline)

- chunk cases scored: 8

| metric | value |
|---|---|
| chunk_MRR | 1.000 |
| chunk_recall@5 | 1.000 |
| chunk_recall@10 | 1.000 |
| chunk_hit@5 | 1.000 |
| chunk_hit@10 | 1.000 |
| chunk_neg@5 | 1.000 |
| chunk_neg@10 | 1.000 |
| chunk_discrimination (relevant chunk ranks above near_miss) | 1.000 |

## Chunk Source Continuity (was chunk.source retrieved at all? diagnostic only)

| id | source_found | source_rank | first_chunk_rank | first_near_miss_rank |
|---|---|---|---|---|
| chunk_cold_zone_picker_rotation | yes | 1 | 1 | 2 |
| chunk_ambient_zone_replenishment | yes | 1 | 1 | 2 |
| chunk_ambient_zone_scanning | yes | 1 | 1 | 2 |
| chunk_international_return_window | yes | 1 | 1 | 2 |
| chunk_domestic_restocking_fee | yes | 1 | 1 | 2 |
| chunk_international_refund_method | yes | 1 | 1 | 2 |
| chunk_premium_reward_points | yes | 1 | 1 | 2 |
| chunk_premium_annual_fee | yes | 1 | 1 | 2 |
