/**
 * Frontend constants mirroring core backend versions.
 *
 * Two distinct version strings live here — easy to confuse:
 *
 * - SCHEMA_VERSION: graph storage schema version
 *   (okto_pulse_core/src/okto_pulse/core/kg/schema.py::SCHEMA_VERSION).
 *   Bumped when nodes/edges/columns change. Currently displayed in
 *   KGHelpModal as "Schema version: …".
 *
 * - EXPECTED_KG_HEALTH_SCHEMA_VERSION: response contract version of
 *   GET /api/v1/kg/health (kg_health_service.py::HEALTH_SCHEMA_VERSION).
 *   Bumped when the field set or types change in the JSON payload.
 *   KGHealthView checks this to render the red "schema outdated" banner.
 *
 * Both must be kept in sync manually with their backend counterparts.
 */
export const SCHEMA_VERSION = '0.3.3';

export const EXPECTED_KG_HEALTH_SCHEMA_VERSION = '1.0';
