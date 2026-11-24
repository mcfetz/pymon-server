-- One-off schema change for the live pymon PostgreSQL database.
--
-- NOT executed by the application at startup: DROP INDEX CONCURRENTLY cannot
-- run inside a transaction block, and any write against the live database is
-- an ops step that needs Daniel's approval. The ORM changes in db_models.py
-- only affect newly created databases; existing ones keep their indexes until
-- this script runs.
--
-- What and why (measured on docker12, pymon-db, 2026-11-19, 25.6 M live rows
-- in a 21-day retention window):

--   ix_metrics_id            584 MB  duplicate of metrics_pkey (both btree(id))
--                                    EXPLAIN: "Index Only Scan using ix_metrics_id"
--                                    for WHERE id = ...; metrics_pkey is the same
--                                    access path, so the duplicate is pure cost.
--                                    FK checks (alarms.metrics_id, metric_notes
--                                    .metrics_id) use the unique primary key.
--
--   idx_metrics_plugin_metric 205 MB strict prefix of
--                                    idx_metrics_plugin_metric_agent_received
--                                    (pluginid, metric, agentid, received_at).
--                                    DISTINCT-metric query per plugin measured
--                                    762-1051 ms with it, 916-1015 ms without
--                                    (both dominated by heap fetches), so no
--                                    measurable regression.
--
-- Not dropped on purpose:
--   idx_metrics_agent_plugin_ts (481 MB) is not a prefix duplicate of
--   idx_metrics_agent_plugin_metric_ts: it is the only index that lets the
--   timestamp range be an index condition for agent+plugin queries without a
--   metric. Query measured at 32 ms with it and 1729 ms when forced onto the
--   4-column index. Keeping it.
--
-- Run (each statement autocommits; do not wrap in BEGIN/COMMIT):
--   docker exec -i "$(docker ps -qf name=pymon-db)" \
--     psql -U pymon -d pymon -f - < migrations/2026-11-19-drop-redundant-metrics-indexes.sql
--
-- Rollback (rebuilds, ~minutes on 25.6 M rows):
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_metrics_id ON metrics (id);
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_metrics_plugin_metric
--       ON metrics (pluginid, metric);

\set ON_ERROR_STOP on

DROP INDEX CONCURRENTLY IF EXISTS ix_metrics_id;
DROP INDEX CONCURRENTLY IF EXISTS idx_metrics_plugin_metric;

-- Expected: 5 indexes left (metrics_pkey, idx_metrics_agent_plugin_metric_ts,
-- idx_metrics_plugin_metric_agent_received, idx_metrics_agent_plugin_ts,
-- idx_metrics_timestamp), and ~789 MB less on the volume.
SELECT indexrelname,
       idx_scan,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes
WHERE relname = 'metrics'
ORDER BY pg_relation_size(indexrelid) DESC;