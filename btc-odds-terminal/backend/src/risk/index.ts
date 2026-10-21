// Risk controls module entry (modules/index.ts imports createRisk from here). OWNER: alerts-risk.
// The implementation lives in backend/src/alerts/risk.ts (limits, pre-trade check, daily loss auto-disable
// until the next America/New_York midnight, kill switch); see the policy notes at the top of that file.

export { createRisk, fmtEt, RISK_PERSIST_MS, RISK_REFRESH_MS, type RiskModule } from "../alerts/risk.ts";
